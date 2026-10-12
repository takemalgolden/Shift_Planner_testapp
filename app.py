from flask import Flask, request, redirect, url_for, render_template, flash, session, send_file
import sqlite3
import re, os, csv, io, json, calendar, hashlib, hmac, secrets
from datetime import date, datetime, timedelta
from functools import wraps
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "shift_planner.db")
app = Flask(__name__)
app.secret_key = os.environ.get("SHIFT_APP_SECRET", "change-this-secret-before-use")

DEFAULT_SHIFTS = [
    ("早番", "09:20", "18:00", 60),
    ("中番", "11:00", "20:00", 60),
    ("遅番", "14:00", "23:00", 60),
    ("半日午前", "09:20", "13:00", 0),
    ("半日午後", "18:00", "23:00", 0),
]

def valid_password(value):
    if not 8 <= len(value) <= 16:
        return False
    return sum((any(ch.isupper() for ch in value), any(ch.islower() for ch in value),
                any(ch.isdigit() for ch in value), any(not ch.isalnum() for ch in value))) >= 3

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'operator', employee_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS employees(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
          skills TEXT NOT NULL DEFAULT 'consumer', monthly_days INTEGER DEFAULT 20,
          min_hours REAL DEFAULT 4, max_hours REAL DEFAULT 9, max_consecutive INTEGER DEFAULT 5,
          early_ok INTEGER DEFAULT 1, mid_ok INTEGER DEFAULT 1, late_ok INTEGER DEFAULT 1,
          notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS shift_patterns(
          id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, start_time TEXT NOT NULL,
          end_time TEXT NOT NULL, break_minutes INTEGER NOT NULL DEFAULT 60, active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS coverage(
          id INTEGER PRIMARY KEY, weekday INTEGER NOT NULL, start_time TEXT NOT NULL,
          end_time TEXT NOT NULL, skill TEXT NOT NULL, min_count INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS requests(
          id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, request_date TEXT NOT NULL,
          request_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', note TEXT DEFAULT '',
          FOREIGN KEY(employee_id) REFERENCES employees(id)
        );
        CREATE TABLE IF NOT EXISTS assignments(
          id INTEGER PRIMARY KEY, employee_id INTEGER NOT NULL, work_date TEXT NOT NULL,
          pattern_id INTEGER, skill TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
          note TEXT DEFAULT '', UNIQUE(employee_id, work_date),
          FOREIGN KEY(employee_id) REFERENCES employees(id),
          FOREIGN KEY(pattern_id) REFERENCES shift_patterns(id)
        );
        CREATE TABLE IF NOT EXISTS approvals(
          id INTEGER PRIMARY KEY, work_month TEXT NOT NULL, admin_username TEXT NOT NULL,
          action TEXT NOT NULL, reason TEXT DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        user_columns = {row[1] for row in c.execute("PRAGMA table_info(users)").fetchall()}
        if "must_change_password" not in user_columns:
            c.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        c.execute("""CREATE TABLE IF NOT EXISTS password_reset_audit (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, username_at_reset TEXT NOT NULL,
            reset_by TEXT NOT NULL, reset_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS operator_registration_requests (
          id INTEGER PRIMARY KEY, username TEXT NOT NULL, name TEXT NOT NULL,
          password_hash TEXT NOT NULL, skills TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
          reviewed_at TEXT, reviewed_by TEXT
        )""")
        # Migration for call-center employee linkage.
        employee_columns = {row[1] for row in c.execute("PRAGMA table_info(employees)").fetchall()}
        if "source_user_id" not in employee_columns:
            c.execute("ALTER TABLE employees ADD COLUMN source_user_id INTEGER")
        if "source_username" not in employee_columns:
            c.execute("ALTER TABLE employees ADD COLUMN source_username TEXT")
        if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
            c.execute("INSERT INTO users(username,password_hash,role) VALUES(?,?,?)",
                      ("admin", generate_password_hash("Admin123!"), "admin"))
        if c.execute("SELECT COUNT(*) FROM shift_patterns").fetchone()[0] == 0:
            c.executemany("INSERT INTO shift_patterns(name,start_time,end_time,break_minutes) VALUES(?,?,?,?)", DEFAULT_SHIFTS)
        if c.execute("SELECT COUNT(*) FROM coverage").fetchone()[0] == 0:
            # Example defaults; admins should replace these with actual staffing rules.
            c.executemany("INSERT INTO coverage(weekday,start_time,end_time,skill,min_count) VALUES(?,?,?,?,?)",
                          [(wd,"09:20","23:00",skill,1) for wd in range(7) for skill in ("merchant","consumer","PP")])

def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("username"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if session.get("role") != "admin":
            flash("管理者権限が必要です。", "error")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return wrapped

def month_days(month):
    y, m = map(int, month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return [date(y, m, d) for d in range(1, last+1)]

def mins(t):
    h, m = map(int, t.split(":"))
    return h*60+m

def overlaps(a1, a2, b1, b2):
    return max(mins(a1), mins(b1)) < min(mins(a2), mins(b2))

def get_month():
    return request.args.get("month") or date.today().strftime("%Y-%m")

def _callcenter_db_path():
    configured = os.environ.get("CALLCENTER_DB_PATH")
    if configured:
        return os.path.abspath(configured)
    try:
        with db() as c:
            row = c.execute("SELECT value FROM settings WHERE key='callcenter_db_path'").fetchone()
        if row and os.path.isfile(row["value"]):
            return row["value"]
    except sqlite3.Error:
        pass
    local_app_data = os.environ.get("LOCALAPPDATA")
    candidates = []
    if local_app_data:
        candidates.append(os.path.abspath(os.path.join(local_app_data, "CallCenterLocal", "data", "callcenter.sqlite3")))
    candidates.extend([
        os.path.abspath(os.path.join(APP_DIR, "..", "callcenter_local_app", "data", "callcenter.sqlite3")),
        os.path.abspath(os.path.join(APP_DIR, "callcenter_local_app", "data", "callcenter.sqlite3")),
    ])
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def _employee_is_linked(employee_id):
    with db() as c:
        row = c.execute("SELECT source_user_id FROM employees WHERE id=?", (employee_id,)).fetchone()
    return bool(row and row["source_user_id"] is not None)


def _authenticate_callcenter_user(username, password):
    path = _callcenter_db_path()
    if not os.path.isfile(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT id,username,role,salt,password_hash,active FROM users WHERE username=?", (username,)).fetchone()
        con.close()
        if not row or not row["active"] or row["role"] != "operator":
            return None
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(row["salt"]), 240000).hex()
        return dict(row) if hmac.compare_digest(digest, row["password_hash"]) else None
    except (sqlite3.Error, OSError, ValueError, KeyError):
        return None


@app.before_request
def enforce_password_change():
    if not session.get("username") or request.endpoint in ("password_page", "logout", "static", "login", "register_operator"):
        return None
    if session.get("callcenter_linked"):
        return None
    with db() as c:
        row = c.execute("SELECT must_change_password FROM users WHERE username=?", (session["username"],)).fetchone()
    if row and row["must_change_password"]:
        return redirect(url_for("password_page"))

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        with db() as c:
            u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if u and u["employee_id"]:
            with db() as c:
                emp_active = c.execute("SELECT active FROM employees WHERE id=?", (u["employee_id"],)).fetchone()
            if not emp_active or not emp_active["active"]:
                u = None
        if u and check_password_hash(u["password_hash"], password):
            session["username"], session["role"], session["employee_id"] = u["username"], u["role"], u["employee_id"]
            session["callcenter_linked"] = bool(u["employee_id"] and _employee_is_linked(u["employee_id"]))
            if u["must_change_password"]:
                return redirect(url_for("password_page"))
            return redirect(url_for("index"))
        # Linked operators may authenticate with the account used in the break-placement app.
        linked = _authenticate_callcenter_user(username, password)
        if linked:
            with db() as c:
                emp = c.execute("SELECT id FROM employees WHERE source_user_id=? AND active=1", (linked["id"],)).fetchone()
                local_user = c.execute("SELECT * FROM users WHERE username=? AND role='operator'", (username,)).fetchone()
            if emp and local_user:
                session["username"], session["role"], session["employee_id"] = username, "operator", emp["id"]
                session["callcenter_linked"] = True
                return redirect(url_for("index"))
        flash("ユーザー名またはパスワードが違います。", "error")
    return render_template("login.html")

@app.route("/register", methods=["GET", "POST"])
def register_operator():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        skills = [s for s in ("consumer", "merchant", "PP") if s in request.form.getlist("skills")]
        error = None
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
            error = "ログインIDは半角英数字・_・.・- の3～32文字で入力してください。"
        elif not name or len(name) > 100:
            error = "氏名を100文字以内で入力してください。"
        elif not valid_password(password):
            error = "パスワードは8～16文字で、大文字・小文字・数字・記号のうち3種類以上が必要です。"
        elif password != confirm:
            error = "確認用パスワードが一致しません。"
        elif not skills:
            error = "保有スキルを1つ以上選択してください。"
        else:
            with db() as c:
                if c.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone() or c.execute(
                    "SELECT 1 FROM operator_registration_requests WHERE username=? AND status='pending'", (username,)).fetchone():
                    error = "このログインIDは登録済み、または承認待ちです。"
                else:
                    c.execute("""INSERT INTO operator_registration_requests
                        (username,name,password_hash,skills,created_at) VALUES(?,?,?,?,?)""",
                        (username,name,generate_password_hash(password),",".join(skills),datetime.now().isoformat(timespec="seconds")))
                    flash("登録申請を受け付けました。管理者の承認後にログインできます。")
                    return redirect(url_for("login"))
        flash(error, "error")
    return render_template("register.html")

@app.route("/registration-requests", methods=["GET", "POST"])
@login_required
@admin_required
def registration_requests_page():
    if request.method == "POST":
        try:
            request_id = int(request.form.get("request_id", "0"))
            action = request.form.get("action")
            if action not in ("approve", "reject"):
                raise ValueError("不正な操作です。")
            with db() as c:
                row = c.execute("SELECT * FROM operator_registration_requests WHERE id=? AND status='pending'", (request_id,)).fetchone()
                if row is None:
                    raise ValueError("申請が見つからないか、すでに処理されています。")
                if action == "approve":
                    if c.execute("SELECT 1 FROM users WHERE username=?", (row["username"],)).fetchone():
                        raise ValueError("同じログインIDがすでに登録されています。")
                    cur = c.execute("INSERT INTO employees(name,skills,active) VALUES(?,?,1)", (row["name"],row["skills"]))
                    c.execute("""INSERT INTO users(username,password_hash,role,employee_id,must_change_password)
                        VALUES(?,?, 'operator', ?, 0)""", (row["username"],row["password_hash"],cur.lastrowid))
                c.execute("""UPDATE operator_registration_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?""",
                          ("approved" if action == "approve" else "rejected",datetime.now().isoformat(timespec="seconds"),session["username"],request_id))
                flash("申請を承認しました。" if action == "approve" else "申請を却下しました。")
        except (ValueError,sqlite3.Error) as exc:
            flash(f"処理できませんでした: {exc}", "error")
        return redirect(url_for("registration_requests_page"))
    with db() as c:
        rows = c.execute("SELECT * FROM operator_registration_requests WHERE status='pending' ORDER BY created_at,id").fetchall()
    return render_template("registration_requests.html", rows=rows)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    month = get_month()
    with db() as c:
        employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
        patterns = c.execute("SELECT * FROM shift_patterns WHERE active=1 ORDER BY start_time").fetchall()
        assignments = c.execute("""SELECT a.*,e.name,p.name pattern_name,p.start_time,p.end_time
          FROM assignments a JOIN employees e ON e.id=a.employee_id
          LEFT JOIN shift_patterns p ON p.id=a.pattern_id
          WHERE substr(a.work_date,1,7)=? ORDER BY a.work_date,e.name""", (month,)).fetchall()
        if session.get("role") == "admin":
            requests = c.execute("""SELECT r.*,e.name FROM requests r JOIN employees e ON e.id=r.employee_id
              WHERE substr(r.request_date,1,7)=? ORDER BY r.request_date,e.name""", (month,)).fetchall()
        else:
            requests = c.execute("""SELECT r.*,e.name FROM requests r JOIN employees e ON e.id=r.employee_id
              WHERE substr(r.request_date,1,7)=? AND r.employee_id=? ORDER BY r.request_date,e.name""",
              (month, session.get("employee_id"))).fetchall()
    by_day = {}
    for a in assignments:
        by_day.setdefault(a["work_date"], []).append(a)
    warnings = validate_month(month) if session.get("role") == "admin" else []
    with db() as c:
        first = c.execute("SELECT * FROM approvals WHERE work_month=? AND action='警告確認' ORDER BY id DESC LIMIT 1", (month,)).fetchone() if session.get("role") == "admin" else None
    return render_template("index.html", month=month, employees=employees, patterns=patterns,
                           by_day=by_day, days=month_days(month), requests=requests,
                           warnings=warnings, first=first)

@app.route("/employees", methods=["GET", "POST"])
@login_required
@admin_required
def employees_page():
    if request.method == "POST":
        action = request.form.get("action")
        try:
            with db() as c:
                if action == "reset_password":
                    eid = int(request.form["employee_id"])
                    target = c.execute("""SELECT u.id,u.username,e.source_user_id
                        FROM users u JOIN employees e ON e.id=u.employee_id
                        WHERE e.id=? AND u.role='operator'""", (eid,)).fetchone()
                    if not target:
                        raise ValueError("従業員アカウントが見つかりません。")
                    if target["source_user_id"] is not None:
                        raise ValueError("休憩時間配置アプリ連携アカウントのパスワードは連携元で変更してください。")
                    new = request.form.get("new_password", "")
                    if not valid_password(new):
                        raise ValueError("パスワードは8〜16文字、大文字・小文字・数字・記号のうち3種類以上を含めてください。")
                    c.execute("UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?",
                              (generate_password_hash(new), target["id"]))
                    c.execute("""INSERT INTO password_reset_audit(user_id,username_at_reset,reset_by,reset_at)
                                 VALUES(?,?,?,?)""", (target["id"],target["username"],session["username"],datetime.now().isoformat(timespec="seconds")))
                    flash("パスワードを再設定しました。対象従業員は次回ログイン時に変更が必要です。")
                elif action in ("add", "edit"):
                    name = request.form.get("name", "").strip()
                    skills_list = [x for x in request.form.getlist("skills") if x in ("consumer", "merchant", "PP")]
                    allowed = request.form.getlist("allowed")
                    days = int(request.form.get("monthly_days", 20))
                    minimum = float(request.form.get("min_hours", 4))
                    maximum = float(request.form.get("max_hours", 9))
                    consecutive = int(request.form.get("max_consecutive", 5))
                    if not name or not skills_list:
                        raise ValueError("氏名と保有スキルを指定してください。")
                    if not (0 <= days <= 31 and 0 <= minimum <= maximum <= 24 and 1 <= consecutive <= 31):
                        raise ValueError("勤務条件の数値を確認してください。")
                    values = (name, ",".join(skills_list), days, minimum, maximum, consecutive,
                              int("早番" in allowed), int("中番" in allowed), int("遅番" in allowed))
                    username = request.form.get("username", "").strip()
                    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
                        raise ValueError("ログインIDは半角英数字・_・.・- の3～32文字で指定してください。")
                    existing = c.execute("SELECT employee_id FROM users WHERE username=?", (username,)).fetchone()
                    if existing and (action == "add" or existing["employee_id"] != int(request.form["employee_id"])):
                        raise ValueError("このログインIDはすでに使用されています。")
                    if action == "add":
                        cur = c.execute("""INSERT INTO employees
                            (name,skills,monthly_days,min_hours,max_hours,max_consecutive,early_ok,mid_ok,late_ok)
                            VALUES(?,?,?,?,?,?,?,?,?)""", values)
                        eid = cur.lastrowid
                        c.execute("INSERT INTO users(username,password_hash,role,employee_id) VALUES(?,?,?,?)",
                                  (username, generate_password_hash("ChangeMe123!"), "operator", eid))
                        c.execute("UPDATE users SET must_change_password=1 WHERE employee_id=?", (eid,))
                        flash(f"{name}さんを登録しました。ログインID: {username} / 初期PW: ChangeMe123!（必ず変更してください）")
                    else:
                        eid = int(request.form["employee_id"])
                        old = c.execute("SELECT id,source_user_id FROM employees WHERE id=?", (eid,)).fetchone()
                        if old is None:
                            raise ValueError("対象の従業員が見つかりません。")
                        user_row = c.execute("SELECT id,username FROM users WHERE employee_id=? AND role='operator'", (eid,)).fetchone()
                        if user_row is None:
                            raise ValueError("対象従業員のログインアカウントが見つかりません。")
                        c.execute("""UPDATE employees SET name=?,skills=?,monthly_days=?,min_hours=?,max_hours=?,
                                     max_consecutive=?,early_ok=?,mid_ok=?,late_ok=? WHERE id=?""", values + (eid,))
                        c.execute("UPDATE users SET username=? WHERE id=?", (username, user_row["id"]))
                        flash("従業員情報とログインIDを更新しました。" +
                              (" ※連携元の氏名・スキルは次回同期で上書きされます。" if old["source_user_id"] is not None else ""))
                elif action in ("delete", "restore"):
                    eid = int(request.form["employee_id"])
                    row = c.execute("SELECT id,source_user_id FROM employees WHERE id=?", (eid,)).fetchone()
                    if row is None:
                        raise ValueError("対象の従業員が見つかりません。")
                    c.execute("UPDATE employees SET active=? WHERE id=?", (int(action == "restore"), eid))
                    flash(("従業員を有効化しました。" if action == "restore" else
                           "従業員を無効化しました。シフト・希望休・ログイン情報は保持されます。") +
                          (" ※連携元の在籍状態は次回同期で反映されます。" if row["source_user_id"] is not None else ""))
                else:
                    raise ValueError("不明な操作です。")
        except (ValueError, KeyError, sqlite3.Error) as exc:
            flash(f"操作できませんでした: {exc}", "error")
        return redirect(url_for("employees_page"))
    with db() as c:
        rows = c.execute("""SELECT e.*, (SELECT u.username FROM users u WHERE u.employee_id=e.id AND u.role='operator' ORDER BY u.id LIMIT 1) AS login_username
                            FROM employees e ORDER BY e.active DESC,e.name""").fetchall()
    return render_template("employees.html", employees=rows)


IMPORT_COLUMNS = ["ログインID", "氏名", "スキル", "月間勤務日数", "最小勤務時間", "最大勤務時間", "最大連勤", "早番", "中番", "遅番"]

def parse_import_file(upload):
    filename = (upload.filename or "").lower()
    data = upload.stream.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("ファイルは2MB以下にしてください。")
    if filename.endswith(".csv"):
        try:
            content = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            content = data.decode("cp932")
        records = list(csv.reader(io.StringIO(content)))
    elif filename.endswith(".xlsx"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        try:
            records = list(wb.active.iter_rows(values_only=True, max_row=502))
        finally:
            wb.close()
    else:
        raise ValueError(".xlsx または .csv ファイルを選択してください。")
    if not records:
        raise ValueError("ファイルが空です。")
    header = [str(v).strip() if v is not None else "" for v in records[0]]
    required = ["ログインID", "氏名", "スキル"]
    if any(h not in header for h in required):
        raise ValueError("必須列がありません: " + ", ".join(required))
    if len(records) > 501:
        raise ValueError("一度に取り込めるのは500名までです。")
    results = []
    for lineno, record in enumerate(records[1:], 2):
        if not any(v is not None and str(v).strip() for v in record):
            continue
        raw = {h: record[i] if i < len(record) else None for i,h in enumerate(header) if h}
        try:
            username = str(raw.get("ログインID") or "").strip()
            name = str(raw.get("氏名") or "").strip()
            skills = [x.strip() for x in re.split(r"[,、，/]+", str(raw.get("スキル") or "")) if x.strip()]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
                raise ValueError("ログインIDは半角英数字・_.- の3〜32文字")
            if not name or len(name) > 100:
                raise ValueError("氏名は1〜100文字")
            if not skills or any(x not in ("consumer", "merchant", "PP") for x in skills):
                raise ValueError("スキルはconsumer,merchant,PPのいずれか")
            def number(field, default, typ):
                value = raw.get(field)
                return typ(value) if value is not None and str(value).strip() else default
            days = number("月間勤務日数", 20, int)
            min_h = number("最小勤務時間", 4, float)
            max_h = number("最大勤務時間", 9, float)
            consecutive = number("最大連勤", 5, int)
            if not (0 <= days <= 31 and 0 <= min_h <= max_h <= 24 and 1 <= consecutive <= 31):
                raise ValueError("勤務条件の範囲が不正")
            def flag(field):
                value = str(raw.get(field) if raw.get(field) is not None else "1").strip().lower()
                if value in ("1", "yes", "true", "可", "○", "〇", "はい"): return 1
                if value in ("0", "no", "false", "不可", "×", "いいえ"): return 0
                raise ValueError(field + "は1/0または可/不可")
            values = [username, name, ",".join(dict.fromkeys(skills)), days, min_h, max_h, consecutive,
                      flag("早番"), flag("中番"), flag("遅番")]
            results.append({"line":lineno,"values":values,"error":""})
        except (ValueError, TypeError) as exc:
            results.append({"line":lineno,"values":[],"error":str(exc)})
    if not results:
        raise ValueError("従業員データがありません。")
    seen=set()
    for item in results:
        if item["values"]:
            key=item["values"][0]
            if key in seen: item["error"]="ファイル内でログインIDが重複"
            seen.add(key)
    return results

@app.route("/employees/import", methods=["GET", "POST"])
@login_required
@admin_required
def employees_import_page():
    rows=[]; token=None; mode="new"
    signer=URLSafeTimedSerializer(app.secret_key, salt="employee-import-v1")
    if request.method == "POST":
        mode=request.form.get("mode", "new")
        if mode not in ("new", "update"): mode="new"
        if request.form.get("action")=="preview":
            try:
                rows=parse_import_file(request.files["file"])
                with db() as c:
                    existing={r["username"]:r for r in c.execute("SELECT username,employee_id FROM users")}
                for row in rows:
                    if row["error"]: continue
                    username=row["values"][0]
                    user=existing.get(username)
                    row["status"]="更新" if user else "新規"
                    if user and user["employee_id"] is None: row["error"]="管理者ID等は更新不可"
                    elif user and mode=="new": row["error"]="登録済みID（新規登録モード）"
                    elif not user and mode=="update": row["error"]="未登録ID（更新モード）"
                token=signer.dumps({"rows":rows,"mode":mode})
            except Exception as exc:
                flash("ファイルを読み込めませんでした: " + str(exc), "error")
        elif request.form.get("action")=="commit":
            try:
                payload=signer.loads(request.form.get("token", ""), max_age=900)
                rows=payload["rows"]; mode=payload["mode"]
                if len(rows)>500 or mode not in ("new", "update") or any(r["error"] for r in rows):
                    raise ValueError("エラー行があります。ファイルを修正して再度プレビューしてください。")
                with db() as c:
                    for row in rows:
                        username,name,skills,days,min_h,max_h,consecutive,early,mid,late=row["values"]
                        existing=c.execute("SELECT employee_id FROM users WHERE username=?",(username,)).fetchone()
                        if mode=="new":
                            if existing: raise ValueError(username+" は既に登録されています。")
                            cur=c.execute("""INSERT INTO employees(name,skills,monthly_days,min_hours,max_hours,max_consecutive,early_ok,mid_ok,late_ok)
                              VALUES(?,?,?,?,?,?,?,?,?)""",(name,skills,days,min_h,max_h,consecutive,early,mid,late))
                            c.execute("INSERT INTO users(username,password_hash,role,employee_id,must_change_password) VALUES(?,?,?,?,1)",
                                      (username,generate_password_hash("ChangeMe123!"),"operator",cur.lastrowid))
                        else:
                            if not existing or existing["employee_id"] is None: raise ValueError(username+" は更新できません。")
                            c.execute("""UPDATE employees SET name=?,skills=?,monthly_days=?,min_hours=?,max_hours=?,max_consecutive=?,
                              early_ok=?,mid_ok=?,late_ok=? WHERE id=?""",
                              (name,skills,days,min_h,max_h,consecutive,early,mid,late,existing["employee_id"]))
                flash(f"{len(rows)}件の{'新規登録' if mode=='new' else '更新'}が完了しました。")
                return redirect(url_for("employees_page"))
            except (BadSignature, SignatureExpired):
                flash("確認データの期限が切れました。再度ファイルを読み込んでください。", "error")
            except (ValueError, sqlite3.Error, KeyError, TypeError) as exc:
                flash("取り込みを中止しました（データは変更されていません）: " + str(exc), "error")
    return render_template("employees_import.html", rows=rows, token=token, mode=mode)

@app.route("/employees/import/template.csv")
@login_required
@admin_required
def employees_import_template():
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(IMPORT_COLUMNS)
    writer.writerow(["tanaka01","田中太郎","consumer,PP",20,4,9,5,1,1,1])
    content=io.BytesIO(("\ufeff"+out.getvalue()).encode("utf-8"))
    return send_file(content, mimetype="text/csv", as_attachment=True, download_name="employees_template.csv")

@app.route("/patterns", methods=["GET", "POST"])
@login_required
@admin_required
def patterns_page():
    time_options = [f"{hour:02d}:{minute:02d}" for hour in range(24) for minute in range(0, 60, 10)]
    if request.method == "POST":
        action = request.form.get("action")
        try:
            with db() as c:
                if action in ("add", "update"):
                    name = request.form.get("name", "").strip()
                    start = request.form.get("start_time", "")
                    end = request.form.get("end_time", "")
                    break_minutes = int(request.form.get("break_minutes", 60))
                    if not name or start not in time_options or end not in time_options:
                        raise ValueError("名称と10分刻みの開始・終了時刻を指定してください。")
                    if mins(end) <= mins(start):
                        raise ValueError("終了時刻は開始時刻より後にしてください。")
                    if not 0 <= break_minutes <= 240 or break_minutes >= mins(end) - mins(start):
                        raise ValueError("休憩時間を確認してください。")
                    if action == "add":
                        c.execute("INSERT INTO shift_patterns(name,start_time,end_time,break_minutes) VALUES(?,?,?,?)",
                                  (name, start, end, break_minutes))
                        flash("勤務パターンを登録しました。")
                    else:
                        pattern_id = int(request.form["pattern_id"])
                        active = int(request.form.get("active", "1"))
                        if active not in (0, 1):
                            raise ValueError("状態が不正です。")
                        cur = c.execute("""UPDATE shift_patterns SET name=?,start_time=?,end_time=?,break_minutes=?,active=?
                                           WHERE id=?""", (name, start, end, break_minutes, active, pattern_id))
                        if cur.rowcount == 0:
                            raise ValueError("勤務パターンが見つかりません。")
                        flash("勤務パターンを更新しました。")
                elif action == "delete":
                    c.execute("UPDATE shift_patterns SET active=0 WHERE id=?", (int(request.form["pattern_id"]),))
                    flash("勤務パターンを無効化しました。")
                else:
                    raise ValueError("不明な操作です。")
        except (ValueError, sqlite3.IntegrityError) as exc:
            flash(f"勤務パターンを保存できませんでした: {exc}", "error")
        return redirect(url_for("patterns_page"))
    with db() as c:
        patterns = c.execute("SELECT * FROM shift_patterns ORDER BY active DESC,start_time").fetchall()
    return render_template("patterns.html", patterns=patterns, time_options=time_options)

@app.route("/coverage", methods=["GET","POST"])
@login_required
@admin_required
def coverage_page():
    if request.method == "POST":
        with db() as c:
            action = request.form.get("action")
            if action == "add":
                c.execute("INSERT INTO coverage(weekday,start_time,end_time,skill,min_count) VALUES(?,?,?,?,?)",
                          (int(request.form["weekday"]), request.form["start_time"], request.form["end_time"],
                           request.form["skill"], int(request.form["min_count"])))
                flash("必要人数ルールを追加しました。")
            elif action == "delete":
                c.execute("DELETE FROM coverage WHERE id=?", (int(request.form["coverage_id"]),))
                flash("ルールを削除しました。")
    with db() as c:
        rows = c.execute("SELECT * FROM coverage ORDER BY weekday,start_time,skill").fetchall()
    return render_template("coverage.html", rows=rows, weekdays=["月","火","水","木","金","土","日"])

def _sync_skills_from_callcenter(value):
    raw = json.loads(value or "[]")
    if not isinstance(raw, list):
        raise ValueError("スキル形式が不正です")
    mapped = []
    for x in raw:
        v = {"Consumer":"consumer", "Merchant":"merchant", "PP":"PP", "consumer":"consumer", "merchant":"merchant"}.get(x)
        if v and v not in mapped:
            mapped.append(v)
    return ",".join(mapped or ["consumer"])


def _sync_skills_to_callcenter(value):
    mapping = {"consumer":"Consumer", "merchant":"Merchant", "PP":"PP"}
    return json.dumps([mapping[x] for x in (value or "").split(",") if x in mapping], ensure_ascii=False)


@app.route("/integration", methods=["GET", "POST"])
@login_required
@admin_required
def integration_page():
    path = _callcenter_db_path()
    preview_count = None
    if request.method == "POST":
        path = os.path.abspath(request.form.get("db_path", "").strip())
        if not os.path.isfile(path):
            flash("指定されたSQLiteファイルが見つかりません。", "error")
            return redirect(url_for("integration_page"))
        action = request.form.get("action", "save_path")
        try:
            with db() as c:
                c.execute("INSERT INTO settings(key,value) VALUES('callcenter_db_path',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (path,))
            if action == "save_path":
                flash("連携先データベースのパスを保存しました。")
                return redirect(url_for("integration_page"))
            if action != "sync":
                raise ValueError("不明な操作です")
            policy = request.form.get("conflict_policy", "skip")
            if policy not in ("skip", "shift", "break"):
                raise ValueError("競合時の設定が不正です")
            # Attach both databases to a single connection for coordinated writes.
            # A local disk and both apps stopped are strongly recommended for reliable SQLite transactions.
            with db() as c:
                c.execute("ATTACH DATABASE ? AS callcenter", (path,))
                columns = {r[1] for r in c.execute("PRAGMA callcenter.table_info(users)")}
                required = {"id","username","display_name","role","active","skills","salt","password_hash","shift_start","shift_end"}
                if not required.issubset(columns):
                    raise ValueError("休憩アプリのusersテーブル形式が一致しません")
                c.execute("BEGIN IMMEDIATE")
                remote = {r["id"]:r for r in c.execute("SELECT * FROM callcenter.users WHERE role='operator'")}
                local = c.execute("""SELECT e.*,u.id AS account_id,u.username FROM employees e
                    LEFT JOIN users u ON u.employee_id=e.id AND u.role='operator' ORDER BY e.id""").fetchall()
                remote_by_name = {r["username"]:r for r in remote.values()}
                linked_remote_ids = set()
                counts = {"remote_to_local":0,"local_to_remote":0,"conflicts":0,"skipped":0}
                # Local records already linked to a remote account are updated in the selected direction.
                for e in local:
                    rid = e["source_user_id"]
                    r = remote.get(rid) if rid is not None else None
                    if r is None and e["username"]:
                        r = remote_by_name.get(e["username"])
                    if r is None:
                        continue
                    if rid is not None and rid != r["id"]:
                        counts["skipped"] += 1
                        continue
                    if r["id"] in linked_remote_ids:
                        counts["skipped"] += 1
                        continue
                    if not e["account_id"] or not e["username"]:
                        counts["skipped"] += 1
                        continue
                    linked_remote_ids.add(r["id"])
                    local_data = (e["name"], int(e["active"]), e["skills"])
                    remote_data = (r["display_name"], int(r["active"]), _sync_skills_from_callcenter(r["skills"]))
                    # A conflict is any difference on shared fields. Never silently overwrite.
                    if local_data != remote_data:
                        counts["conflicts"] += 1
                        if policy == "skip":
                            counts["skipped"] += 1
                            continue
                    if policy == "shift":
                        c.execute("UPDATE callcenter.users SET display_name=?,active=?,skills=? WHERE id=?",
                                  (e["name"],e["active"],_sync_skills_to_callcenter(e["skills"]),r["id"]))
                        counts["local_to_remote"] += 1
                    else:
                        c.execute("UPDATE employees SET name=?,active=?,skills=?,source_user_id=?,source_username=? WHERE id=?",
                                  (r["display_name"],r["active"],remote_data[2],r["id"],r["username"],e["id"]))
                        counts["remote_to_local"] += 1
                    # Keep the remote username authoritative for linked logins.
                    if e["username"] != r["username"]:
                        collision = c.execute("SELECT id FROM users WHERE username=? AND id<>?",(r["username"],e["account_id"])).fetchone()
                        if collision:
                            raise ValueError("ログインIDが衝突しています: " + r["username"])
                        c.execute("UPDATE users SET username=? WHERE id=?",(r["username"],e["account_id"]))
                    c.execute("UPDATE employees SET source_user_id=?,source_username=? WHERE id=?",(r["id"],r["username"],e["id"]))
                # Import remote-only operators, without overwriting local accounts with matching IDs.
                for r in remote.values():
                    if r["id"] in linked_remote_ids:
                        continue
                    if c.execute("SELECT 1 FROM employees WHERE source_user_id=?",(r["id"],)).fetchone():
                        counts["skipped"] += 1
                        continue
                    if c.execute("SELECT 1 FROM users WHERE username=?",(r["username"],)).fetchone():
                        counts["skipped"] += 1
                        continue
                    cur=c.execute("INSERT INTO employees(name,active,skills,source_user_id,source_username) VALUES(?,?,?,?,?)",
                                  (r["display_name"],r["active"],_sync_skills_from_callcenter(r["skills"]),r["id"],r["username"]))
                    c.execute("INSERT INTO users(username,password_hash,role,employee_id) VALUES(?,?,'operator',?)",
                              (r["username"],generate_password_hash(secrets.token_urlsafe(32)),cur.lastrowid))
                    counts["remote_to_local"] += 1
                # Export local-only operators. Passwords are deliberately NOT copied between systems.
                for e in local:
                    if e["source_user_id"] is not None or not e["username"]:
                        continue
                    if e["username"] in remote_by_name:
                        continue
                    if c.execute("SELECT 1 FROM employees WHERE id=? AND source_user_id IS NOT NULL",(e["id"],)).fetchone():
                        continue
                    salt=secrets.token_hex(16)
                    temp=secrets.token_urlsafe(18)
                    digest=hashlib.pbkdf2_hmac("sha256",temp.encode(),bytes.fromhex(salt),240000).hex()
                    cur=c.execute("""INSERT INTO callcenter.users(username,display_name,role,salt,password_hash,active,skills)
                         VALUES(?,?,'operator',?,?,?,?)""",(e["username"],e["name"],salt,digest,e["active"],_sync_skills_to_callcenter(e["skills"])))
                    c.execute("UPDATE employees SET source_user_id=?,source_username=? WHERE id=?",(cur.lastrowid,e["username"],e["id"]))
                    counts["local_to_remote"] += 1
                # Commit through the same connection; passwords remain independently managed.
            flash("双方向同期が完了しました。休憩→シフト: {remote_to_local}件、シフト→休憩: {local_to_remote}件、差異: {conflicts}件、保留: {skipped}件。休憩アプリに新規作成されたアカウントは休憩アプリ側でパスワード再設定が必要です。".format(**counts))
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            flash("同期に失敗しました（変更はロールバックされます）: " + str(exc), "error")
        return redirect(url_for("integration_page"))
    if os.path.isfile(path):
        try:
            con=sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro",uri=True,timeout=5)
            preview_count=con.execute("SELECT COUNT(*) FROM users WHERE role='operator'").fetchone()[0]
            con.close()
        except sqlite3.Error:
            pass
    return render_template("integration.html",db_path=path,preview_count=preview_count)


@app.route("/requests", methods=["GET","POST"])
@login_required
def requests_page():
    if session.get("role") != "admin":
        return redirect(url_for("my_requests_page"))
    if request.method == "POST":
        action = request.form.get("action", "add")
        with db() as c:
            if action == "status":
                status = request.form.get("status", "pending")
                if status in ("pending", "approved", "rejected"):
                    c.execute("UPDATE requests SET status=? WHERE id=?", (status, int(request.form["request_id"])))
                    flash("希望休の審査状態を更新しました。")
            else:
                c.execute("INSERT INTO requests(employee_id,request_date,request_type,status,note) VALUES(?,?,?,?,?)",
                          (int(request.form["employee_id"]), request.form["request_date"], request.form["request_type"],
                           request.form.get("status","pending"), request.form.get("note","").strip()))
                flash("休暇・休み希望を登録しました。")
        return redirect(url_for("requests_page"))
    with db() as c:
        emps = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
        rows = c.execute("""SELECT r.*,e.name FROM requests r JOIN employees e ON e.id=r.employee_id
                            ORDER BY request_date DESC,e.name LIMIT 500""").fetchall()
    return render_template("requests.html", employees=emps, rows=rows)


@app.route("/my-requests", methods=["GET", "POST"])
@login_required
def my_requests_page():
    if session.get("role") == "admin":
        return redirect(url_for("requests_page"))
    employee_id = session.get("employee_id")
    if not employee_id:
        flash("このアカウントに従業員情報が紐づいていません。管理者に連携設定を依頼してください。", "error")
        return redirect(url_for("index"))
    if request.method == "POST":
        request_date = request.form.get("request_date", "")
        request_type = request.form.get("request_type", "通常の休み希望")
        note = request.form.get("note", "").strip()[:500]
        try:
            datetime.strptime(request_date, "%Y-%m-%d")
            if request_type not in ["有給休暇", "確定休", "通常の休み希望", "できれば休み"]:
                raise ValueError()
            with db() as c:
                c.execute("""INSERT INTO requests(employee_id,request_date,request_type,status,note)
                             VALUES(?,?,?,'pending',?)""",
                          (employee_id, request_date, request_type, note))
            flash("希望休を提出しました。管理者の承認をお待ちください。")
            return redirect(url_for("my_requests_page"))
        except ValueError:
            flash("日付または希望区分を確認してください。", "error")
    with db() as c:
        rows = c.execute("""SELECT * FROM requests WHERE employee_id=?
                            ORDER BY request_date DESC LIMIT 200""", (employee_id,)).fetchall()
        emp = c.execute("SELECT name FROM employees WHERE id=?", (employee_id,)).fetchone()
    return render_template("my_requests.html", rows=rows, employee_name=emp["name"] if emp else session["username"])


@app.route("/requests/export", methods=["GET"])
@login_required
@admin_required
def export_requests_csv():
    """Export requests to an Excel-friendly UTF-8 BOM CSV, with optional month/status filters."""
    month = request.args.get("month", "").strip()
    status = request.args.get("status", "all").strip()
    if month:
        try:
            datetime.strptime(month, "%Y-%m")
            if len(month) != 7:
                raise ValueError()
        except ValueError:
            flash("対象月の形式を確認してください。", "error")
            return redirect(url_for("import_page"))
    if status not in ("all", "pending", "approved", "rejected"):
        flash("申請状態が不正です。", "error")
        return redirect(url_for("import_page"))
    sql = """SELECT r.request_date, e.name, r.request_type, r.status, r.note,
                    u.username AS login_id
             FROM requests r
             JOIN employees e ON e.id = r.employee_id
             LEFT JOIN users u ON u.employee_id = e.id AND u.role = 'operator'
             WHERE 1=1"""
    params = []
    if month:
        sql += " AND substr(r.request_date,1,7)=?"
        params.append(month)
    if status != "all":
        sql += " AND r.status=?"
        params.append(status)
    sql += " ORDER BY r.request_date,e.name,r.id"
    with db() as c:
        records = c.execute(sql, params).fetchall()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(["従業員名", "日付", "休暇区分", "状態", "備考", "ログインID"])
    def safe_cell(value):
        value = "" if value is None else str(value)
        # Prevent spreadsheet formula injection in user-entered cells.
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n")):
            return "'" + value
        return value
    for r in records:
        writer.writerow([safe_cell(r["name"]), r["request_date"], safe_cell(r["request_type"]),
                         r["status"], safe_cell(r["note"]), safe_cell(r["login_id"])])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    filename = f"leave_requests_{month or 'all'}.csv"
    return send_file(data, mimetype="text/csv; charset=utf-8", as_attachment=True,
                     download_name=filename)


@app.route("/import", methods=["GET","POST"])
@login_required
@admin_required
def import_page():
    preview = None
    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("CSVファイルを選択してください。", "error")
        else:
            try:
                raw = file.stream.read().decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(raw))
                headers = reader.fieldnames or []
                rows = list(reader)
                preview = {"headers": headers, "rows": rows[:20], "count": len(rows)}
                session["import_rows"] = rows[:5000]
                session["import_headers"] = headers
            except Exception as e:
                flash("CSVを読み込めませんでした。UTF-8またはExcelでCSV UTF-8保存をお試しください。", "error")
    if request.method == "POST" and request.form.get("import_action") == "apply":
        rows = session.get("import_rows", [])
        name_col, date_col, type_col = request.form.get("name_col"), request.form.get("date_col"), request.form.get("type_col")
        status_col = request.form.get("status_col")
        added, errors = 0, []
        with db() as c:
            emp_map = {r["name"]:r["id"] for r in c.execute("SELECT id,name FROM employees WHERE active=1")}
            for i, row in enumerate(rows, start=2):
                name = (row.get(name_col) or "").strip()
                dt = (row.get(date_col) or "").strip()
                typ = (row.get(type_col) or "通常の休み希望").strip()
                stat = (row.get(status_col) or "pending").strip() if status_col else "pending"
                # Accept ISO dates or Japanese slash dates.
                parsed = None
                for fmt in ("%Y-%m-%d","%Y/%m/%d","%Y年%m月%d日"):
                    try:
                        parsed = datetime.strptime(dt, fmt).date().isoformat(); break
                    except ValueError:
                        pass
                if name not in emp_map:
                    errors.append(f"{i}行目: 未登録の従業員「{name}」")
                elif not parsed:
                    errors.append(f"{i}行目: 日付形式を確認してください（{dt}）")
                else:
                    allowed_types = ["有給休暇","確定休","通常の休み希望","できれば休み","申請中"]
                    if typ not in allowed_types: typ = "通常の休み希望"
                    if stat not in ["pending","approved","rejected"]: stat = "pending"
                    c.execute("INSERT INTO requests(employee_id,request_date,request_type,status,note) VALUES(?,?,?,?,?)",
                              (emp_map[name], parsed, typ, stat, "CSV取込"))
                    added += 1
        flash(f"{added}件を取り込みました。エラー {len(errors)}件。")
        if errors:
            session["import_errors"] = errors[:200]
        return redirect(url_for("import_page"))
    errors = session.pop("import_errors", [])
    with db() as c:
        employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
    return render_template("import.html", preview=preview, employees=employees, errors=errors,
                           headers=session.get("import_headers", []))

def optimize_month(month):
    """CP-SAT optimization; retain finalized rows, solve only drafts, never silently fall back."""
    from ortools.sat.python import cp_model
    days = month_days(month)
    with db() as c:
        employees = [dict(r) for r in c.execute("SELECT * FROM employees WHERE active=1 ORDER BY id")]
        patterns = [dict(r) for r in c.execute("SELECT * FROM shift_patterns WHERE active=1")]
        reqs = [dict(r) for r in c.execute("SELECT * FROM requests WHERE substr(request_date,1,7)=?",(month,))]
        rules = [dict(r) for r in c.execute("SELECT * FROM coverage")]
        locked = [dict(r) for r in c.execute("""SELECT a.*,p.start_time,p.end_time FROM assignments a
                 JOIN shift_patterns p ON p.id=a.pattern_id WHERE substr(a.work_date,1,7)=? AND a.status='finalized'""",(month,))]
    emp_by_id = {e['id']:e for e in employees}
    locked_by_day = {(a['employee_id'],a['work_date']):a for a in locked}
    approved = {(r['employee_id'],r['request_date']) for r in reqs if r['status']=='approved' and r['request_type'] in ('有給休暇','確定休')}
    if any((a['employee_id'],a['work_date']) in approved for a in locked):
        raise ValueError('確定済みシフトと承認済み休暇が重複しています。先に修正してください。')
    if not employees or not patterns:
        raise ValueError('有効な従業員と勤務パターンを登録してください。')
    model = cp_model.CpModel()
    choices = {}  # (employee id, ISO day, pattern id, assigned skill) -> boolean
    per_emp_day = {}
    day_skill = {}
    penalties = []
    for e in employees:
        skills = [x.strip() for x in (e['skills'] or '').split(',') if x.strip() in ('consumer','merchant','PP')]
        for d in days:
            ds=d.isoformat(); key=(e['id'],ds)
            if key in locked_by_day: continue
            if key in approved: continue
            opts=[]
            for pat in patterns:
                if pat['name'].startswith('早') and not e['early_ok']:continue
                if pat['name'].startswith('中') and not e['mid_ok']:continue
                if pat['name'].startswith('遅') and not e['late_ok']:continue
                duration=(mins(pat['end_time'])-mins(pat['start_time'])-int(pat['break_minutes'] or 0))/60
                if duration < float(e['min_hours'] if e['min_hours'] is not None else 0) or duration > float(e['max_hours'] if e['max_hours'] is not None else 24):continue
                for skill in skills:
                    var=model.NewBoolVar(f"x_{e['id']}_{ds}_{pat['id']}_{skill}")
                    choices[e['id'],ds,pat['id'],skill]=var
                    opts.append(var)
                    day_skill.setdefault((ds,skill),[]).append((var,pat))
                    for r in reqs:
                        if r['employee_id']==e['id'] and r['request_date']==ds:
                            if r['request_type'] in ('通常の休み希望','有給休暇','確定休'): penalties.append(40*var)
                            elif r['request_type']=='できれば休み': penalties.append(12*var)
            per_emp_day[key]=opts
            model.Add(sum(opts)<=1)
    for e in employees:
        eid=e['id']
        all_days=[]
        for d in days:
            ds=d.isoformat(); fixed=int((eid,ds) in locked_by_day)
            all_days.append(fixed if fixed else sum(per_emp_day.get((eid,ds),[])))
        target=max(0,int(e['monthly_days'] if e['monthly_days'] is not None else 20))
        fixed_count=sum((eid,d.isoformat()) in locked_by_day for d in days)
        if fixed_count > target: target=fixed_count
        model.Add(sum(all_days)<=target)
        under=model.NewIntVar(0,len(days),f'under_{eid}')
        model.Add(under>=target-sum(all_days))
        penalties.append(6*under)
        consecutive=max(1,int(e['max_consecutive'] or 5))
        for i in range(len(days)-consecutive):
            model.Add(sum(all_days[i:i+consecutive+1])<=consecutive)
    # Every requirement interval must be covered by a shift across the entire interval.
    # Existing validator only checks overlap, so use the stronger full-interval criterion here.
    for d in days:
        ds=d.isoformat()
        for r in rules:
            if r['weekday']!=d.weekday():continue
            need=int(r['min_count'])
            if need<=0:continue
            fixed=sum(1 for a in locked if a['work_date']==ds and a['skill']==r['skill'] and
                      mins(a['start_time'])<=mins(r['start_time']) and mins(a['end_time'])>=mins(r['end_time']))
            eligible=[v for v,p in day_skill.get((ds,r['skill']),[]) if
                      mins(p['start_time'])<=mins(r['start_time']) and mins(p['end_time'])>=mins(r['end_time'])]
            short=model.NewIntVar(0,need,f"short_{ds}_{r['id']}")
            model.Add(sum(eligible)+fixed+short>=need)
            penalties.append(1000*short)
    # Mild penalty on extra shifts; helps avoid overscheduling when staffing is satisfied.
    penalties.extend(choices.values())
    model.Minimize(sum(penalties))
    solver=cp_model.CpSolver()
    solver.parameters.max_time_in_seconds=25.0
    solver.parameters.num_search_workers=4
    result=solver.Solve(model)
    if result not in (cp_model.OPTIMAL,cp_model.FEASIBLE):
        raise ValueError('制約を満たすシフトを作成できませんでした。確定済み勤務や連勤条件を確認してください。')
    selected=[(eid,ds,pid,skill) for (eid,ds,pid,skill),v in choices.items() if solver.Value(v)]
    with db() as c:
        # Transactional replacement: failed solve never erases previous drafts.
        c.execute("DELETE FROM assignments WHERE substr(work_date,1,7)=? AND status='draft'",(month,))
        c.executemany("INSERT INTO assignments(employee_id,work_date,pattern_id,skill,status) VALUES(?,?,?,?, 'draft')",selected)
    return len(selected),result==cp_model.OPTIMAL

@app.route("/generate", methods=["POST"])
@login_required
@admin_required
def generate():
    month=request.form.get('month') or date.today().strftime('%Y-%m')
    try:
        count,optimal=optimize_month(month)
        flash(f'{month} の仮シフトをOR-Toolsで{count}件作成しました。' + (' 最適解です。' if optimal else ' 制限時間内の暫定解です。') + ' 警告・必要人数を確認してください。')
    except ImportError:
        flash('OR-Toolsが組み込まれていません。requirements.txtからインストールし、EXE版は再ビルドしてください。','error')
    except (ValueError,sqlite3.Error) as exc:
        flash(f'シフトを作成できませんでした: {exc}','error')
    return redirect(url_for('index',month=month))

def validate_month(month):
    warnings = []
    with db() as c:
        assigns = c.execute("""SELECT a.*,e.name,e.skills,p.start_time,p.end_time,p.break_minutes
            FROM assignments a JOIN employees e ON e.id=a.employee_id
            JOIN shift_patterns p ON p.id=a.pattern_id
            WHERE substr(a.work_date,1,7)=?""", (month,)).fetchall()
        rules = c.execute("SELECT * FROM coverage").fetchall()
        requests = c.execute("SELECT * FROM requests WHERE substr(request_date,1,7)=?", (month,)).fetchall()
    by_day = {}
    for a in assigns: by_day.setdefault(a["work_date"], []).append(a)
    for d in month_days(month):
        day_rows = by_day.get(d.isoformat(), [])
        day_rules = [r for r in rules if r["weekday"] == d.weekday()]
        for r in day_rules:
            # Count skill-qualified assignments overlapping each requirement block.
            count = 0
            for a in day_rows:
                if a["skill"] != r["skill"]: continue
                if overlaps(a["start_time"],a["end_time"],r["start_time"],r["end_time"]):
                    count += 1
            if count < r["min_count"]:
                warnings.append({"date":d.isoformat(),"kind":"必要人数不足","detail":f'{r["skill"]} {r["start_time"]}–{r["end_time"]}: 必要 {r["min_count"]}人 / 配置 {count}人'})
    assigned = {(a["employee_id"],a["work_date"]) for a in assigns}
    for r in requests:
        if r["request_type"] in ("通常の休み希望","できれば休み","申請中","有給休暇","確定休"):
            if (r["employee_id"],r["request_date"]) in assigned:
                # Only approved leave/confirmed day off is a hard error.
                if r["request_type"] in ("有給休暇","確定休") and r["status"] == "approved":
                    warnings.append({"date":r["request_date"],"kind":"確定休と勤務が重複","detail":"確定済みの休暇・休み希望とシフトが重複しています"})
                else:
                    warnings.append({"date":r["request_date"],"kind":"休み希望未反映","detail":f'{r["request_type"]}が未反映の可能性があります'})
    return warnings

@app.route("/validate")
@login_required
def validate():
    month = get_month()
    warnings = validate_month(month)
    return redirect(url_for("index", month=month, modal="warnings"))

@app.route("/finalize", methods=["POST"])
@login_required
@admin_required
def finalize():
    month = request.form.get("month")
    stage = request.form.get("stage")
    reason = request.form.get("reason","").strip()
    warnings = validate_month(month)
    hard = [w for w in warnings if w["kind"] == "確定休と勤務が重複"]
    if hard:
        flash("確定済み休暇と勤務が重複しているため確定できません。先にシフトを修正してください。", "error")
        return redirect(url_for("index", month=month, modal="warnings"))
    with db() as c:
        if stage == "review":
            c.execute("INSERT INTO approvals(work_month,admin_username,action,reason,created_at) VALUES(?,?,?,?,?)",
                      (month,session["username"],"警告確認",reason,datetime.now().isoformat(timespec="seconds")))
            flash("1段階目の確認を記録しました。内容を再確認し、確定する場合は2段階目へ進んでください。")
            return redirect(url_for("index", month=month, modal="final"))
        if stage != "final":
            flash("不正な確定操作です。", "error")
            return redirect(url_for("index", month=month, modal="warnings"))
        first = c.execute("SELECT id FROM approvals WHERE work_month=? AND action='警告確認' ORDER BY id DESC LIMIT 1", (month,)).fetchone()
        if not first:
            flash("先に1段階目の確認を行ってください。", "error")
            return redirect(url_for("index", month=month, modal="warnings"))
        if not reason:
            flash("確定理由を入力してください。", "error")
            return redirect(url_for("index", month=month, modal="final"))
        c.execute("UPDATE assignments SET status='finalized' WHERE substr(work_date,1,7)=?", (month,))
        c.execute("INSERT INTO approvals(work_month,admin_username,action,reason,created_at) VALUES(?,?,?,?,?)",
                  (month,session["username"],"最終確定",reason,datetime.now().isoformat(timespec="seconds")))
        flash("シフトを確定しました。警告がある場合も履歴に記録されています。")
    return redirect(url_for("index", month=month, modal="result"))

@app.route("/finalize")
@login_required
@admin_required
def finalize_page():
    month = get_month()
    with db() as c:
        first = c.execute("SELECT * FROM approvals WHERE work_month=? AND action='警告確認' ORDER BY id DESC LIMIT 1", (month,)).fetchone()
    return redirect(url_for("index", month=month, modal="final"))

@app.route("/export")
@login_required
@admin_required
def export_csv():
    """Export only finalized assignments for the selected month."""
    month = get_month()
    file_format = request.args.get("format", "xlsx").lower()
    if file_format not in ("xlsx", "csv"):
        flash("出力形式を確認してください。", "error")
        return redirect(url_for("index", month=month))
    with db() as c:
        rows = c.execute("""SELECT a.work_date, e.name, COALESCE(u.username, '') AS login_id,
            p.name AS pattern_name, p.start_time, p.end_time, a.skill, a.note
            FROM assignments a
            JOIN employees e ON e.id=a.employee_id
            JOIN shift_patterns p ON p.id=a.pattern_id
            LEFT JOIN users u ON u.employee_id=e.id AND u.role='operator'
            WHERE substr(a.work_date,1,7)=? AND a.status='finalized'
            ORDER BY a.work_date,p.start_time,e.name,a.id""", (month,)).fetchall()
    if not rows:
        flash("対象月に確定済みシフトがありません。シフトを確定してから出力してください。", "error")
        return redirect(url_for("index", month=month))
    headers = ["勤務日", "従業員名", "ログインID", "勤務パターン", "開始時刻", "終了時刻", "担当スキル", "備考"]
    values = [[r[k] or "" for k in ("work_date", "name", "login_id", "pattern_name", "start_time", "end_time", "skill", "note")] for r in rows]
    if file_format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(headers)
        writer.writerows(values)
        data = io.BytesIO(("\ufeff" + output.getvalue()).encode("utf-8"))
        return send_file(data, mimetype="text/csv; charset=utf-8", as_attachment=True,
                         download_name=f"finalized_shifts_{month}.csv")
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    ws = wb.active
    ws.title = "確定シフト"
    ws.append(headers)
    for row in values:
        ws.append(row)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{len(values)+1}"
    for cell in ws[1]:
        cell.fill = PatternFill("solid", fgColor="17365D")
        cell.font = Font(color="FFFFFF", bold=True)
        cell.alignment = Alignment(horizontal="center")
    for i, width in enumerate((16, 20, 19, 19, 14, 14, 19, 36), 1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="center")
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return send_file(output, as_attachment=True,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     download_name=f"finalized_shifts_{month}.xlsx")

@app.route("/assignments/<int:assignment_id>/edit", methods=["GET","POST"])
@login_required
@admin_required
def edit_assignment(assignment_id):
    with db() as c:
        a = c.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY name").fetchall()
        patterns = c.execute("SELECT * FROM shift_patterns WHERE active=1 ORDER BY start_time").fetchall()
        if not a: return redirect(url_for("index"))
        if request.method == "POST":
            c.execute("UPDATE assignments SET employee_id=?,pattern_id=?,skill=?,note=?,status='draft' WHERE id=?",
                      (int(request.form["employee_id"]), int(request.form["pattern_id"]), request.form["skill"],
                       request.form.get("note",""), assignment_id))
            flash("シフトを更新しました。必要人数を再確認してください。")
            return redirect(url_for("index", month=a["work_date"][:7]))
    return render_template("edit_assignment.html", a=a, employees=employees, patterns=patterns)

@app.route("/assignments/<int:assignment_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_assignment(assignment_id):
    with db() as c:
        a = c.execute("SELECT work_date FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        c.execute("DELETE FROM assignments WHERE id=?", (assignment_id,))
    flash("シフトを削除しました。")
    return redirect(url_for("index", month=a["work_date"][:7] if a else date.today().strftime("%Y-%m")))

@app.route("/password", methods=["GET","POST"])
@login_required
def password_page():
    if session.get("callcenter_linked"):
        flash("このアカウントは休憩時間配置アプリと連携しています。パスワード変更は休憩時間配置アプリ側で行ってください。", "error")
        return redirect(url_for("index"))
    forced = False
    with db() as c:
        account = c.execute("SELECT * FROM users WHERE username=?", (session["username"],)).fetchone()
    if account is None:
        session.clear()
        return redirect(url_for("login"))
    forced = bool(account["must_change_password"])
    if request.method == "POST":
        old, new = request.form.get("old_password", ""), request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not valid_password(new):
            flash("新しいパスワードは8〜16文字で、4種類の文字種のうち3種類以上を含めてください。", "error")
        elif new != confirm:
            flash("新しいパスワードの確認が一致しません。", "error")
        elif not check_password_hash(account["password_hash"], old):
            flash("現在のパスワードが違います。", "error")
        elif check_password_hash(account["password_hash"], new):
            flash("現在とは異なるパスワードを設定してください。", "error")
        else:
            with db() as c:
                c.execute("UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
                          (generate_password_hash(new),account["id"]))
            flash("パスワードを変更しました。")
            return redirect(url_for("index"))
    return render_template("password.html", forced=forced)

if __name__ == "__main__":
    init_db()
    # Bind only to LAN interfaces when explicitly configured; default local-only for safe first run.
    host = os.environ.get("SHIFT_HOST", "0.0.0.0")
    port = int(os.environ.get("SHIFT_PORT", "5050"))
    app.run(host=host, port=port, debug=False)
