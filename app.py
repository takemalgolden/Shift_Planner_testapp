from flask import Flask, request, redirect, url_for, render_template, flash, session, send_file
import sqlite3, os, csv, io, json, calendar, hashlib, hmac, secrets
from datetime import date, datetime, timedelta
from functools import wraps
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


@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","")
        with db() as c:
            u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if u and check_password_hash(u["password_hash"], password):
            session["username"], session["role"], session["employee_id"] = u["username"], u["role"], u["employee_id"]
            session["callcenter_linked"] = bool(u["employee_id"] and _employee_is_linked(u["employee_id"]))
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
                if action in ("add", "edit"):
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
                    if action == "add":
                        cur = c.execute("""INSERT INTO employees
                            (name,skills,monthly_days,min_hours,max_hours,max_consecutive,early_ok,mid_ok,late_ok)
                            VALUES(?,?,?,?,?,?,?,?,?)""", values)
                        eid = cur.lastrowid
                        uname = "op" + str(eid)
                        c.execute("INSERT OR IGNORE INTO users(username,password_hash,role,employee_id) VALUES(?,?,?,?)",
                                  (uname, generate_password_hash("ChangeMe123!"), "operator", eid))
                        flash(f"{name}さんを登録しました。ログインID: {uname} / 初期PW: ChangeMe123!（必ず変更してください）")
                    else:
                        eid = int(request.form["employee_id"])
                        old = c.execute("SELECT id,source_user_id FROM employees WHERE id=?", (eid,)).fetchone()
                        if old is None:
                            raise ValueError("対象の従業員が見つかりません。")
                        c.execute("""UPDATE employees SET name=?,skills=?,monthly_days=?,min_hours=?,max_hours=?,
                                     max_consecutive=?,early_ok=?,mid_ok=?,late_ok=? WHERE id=?""", values + (eid,))
                        flash("従業員情報を更新しました。" +
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
        rows = c.execute("SELECT * FROM employees ORDER BY active DESC,name").fetchall()
    return render_template("employees.html", employees=rows)

@app.route("/patterns", methods=["GET","POST"])
@login_required
@admin_required
def patterns_page():
    if request.method == "POST":
        with db() as c:
            if request.form.get("action") == "add":
                try:
                    c.execute("INSERT INTO shift_patterns(name,start_time,end_time,break_minutes) VALUES(?,?,?,?)",
                              (request.form["name"].strip(), request.form["start_time"], request.form["end_time"],
                               int(request.form.get("break_minutes",60))))
                    flash("勤務パターンを登録しました。")
                except sqlite3.IntegrityError:
                    flash("同じ勤務パターン名が既にあります。", "error")
            elif request.form.get("action") == "delete":
                c.execute("UPDATE shift_patterns SET active=0 WHERE id=?", (int(request.form["pattern_id"]),))
                flash("勤務パターンを無効化しました。")
    with db() as c:
        patterns = c.execute("SELECT * FROM shift_patterns ORDER BY active DESC,start_time").fetchall()
    return render_template("patterns.html", patterns=patterns)

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

@app.route("/integration", methods=["GET", "POST"])
@login_required
@admin_required
def integration_page():
    path = _callcenter_db_path()
    if request.method == "POST":
        path = os.path.abspath(request.form.get("db_path", "").strip())
        if not path or not os.path.isfile(path):
            flash("休憩時間配置アプリのSQLiteファイルが見つかりません。パスを確認してください。", "error")
            return render_template("integration.html", db_path=path, preview_count=None)
        try:
            src = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True, timeout=5)
            src.row_factory = sqlite3.Row
            cols = {r[1] for r in src.execute("PRAGMA table_info(users)").fetchall()}
            if not {"id","username","display_name","role","active","skills"}.issubset(cols):
                src.close()
                raise ValueError("usersテーブルの構造が想定と異なります。")
            source_users = src.execute("SELECT id,username,display_name,role,active,skills,shift_start,shift_end FROM users WHERE role='operator' ORDER BY id").fetchall()
            src.close()
            if request.form.get("action") == "save_path":
                with db() as c:
                    c.execute("INSERT INTO settings(key,value) VALUES('callcenter_db_path',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (path,))
                flash("データベースの参照先を保存しました。")
            else:
                synced = 0
                with db() as c:
                    c.execute("INSERT INTO settings(key,value) VALUES('callcenter_db_path',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (path,))
                    for su in source_users:
                        # Use only active operator accounts; map skill labels to shift-planner labels.
                        skill_list = json.loads(su["skills"] or "[]")
                        mapped = []
                        for skill in skill_list:
                            mapped_skill = {"Consumer":"consumer","Merchant":"merchant","PP":"PP",
                                            "consumer":"consumer","merchant":"merchant"}.get(skill, skill)
                            if mapped_skill in ("consumer","merchant","PP") and mapped_skill not in mapped:
                                mapped.append(mapped_skill)
                        if not mapped:
                            mapped = ["consumer"]
                        emp = c.execute("SELECT id FROM employees WHERE source_user_id=?", (su["id"],)).fetchone()
                        if emp:
                            eid = emp["id"]
                            c.execute("""UPDATE employees SET name=?,active=?,skills=?,source_username=?
                                         WHERE id=?""",
                                      (su["display_name"], int(su["active"]), ",".join(mapped), su["username"], eid))
                        else:
                            c.execute("""INSERT INTO employees(name,active,skills,source_user_id,source_username)
                                         VALUES(?,?,?,?,?)""",
                                      (su["display_name"], int(su["active"]), ",".join(mapped), su["id"], su["username"]))
                            eid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                        # Local placeholder hash is never used for linked accounts; login checks source DB.
                        placeholder = generate_password_hash(secrets.token_urlsafe(32))
                        c.execute("""INSERT INTO users(username,password_hash,role,employee_id)
                                     VALUES(?,?, 'operator', ?)
                                     ON CONFLICT(username) DO UPDATE SET role='operator', employee_id=excluded.employee_id""",
                                  (su["username"], placeholder, eid))
                        synced += 1
                flash(f"従業員データを同期しました（対象オペレーター {synced}名）。希望休提出時は休憩時間配置アプリと同じID・パスワードを使用できます。")
            return redirect(url_for("integration_page"))
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
            flash(f"連携に失敗しました: {exc}", "error")
    preview_count = None
    if os.path.isfile(path):
        try:
            src = sqlite3.connect(f"file:{path.replace(os.sep, '/')}?mode=ro", uri=True, timeout=5)
            preview_count = src.execute("SELECT COUNT(*) FROM users WHERE role='operator' AND active=1").fetchone()[0]
            src.close()
        except sqlite3.Error:
            pass
    return render_template("integration.html", db_path=path, preview_count=preview_count)


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

@app.route("/generate", methods=["POST"])
@login_required
@admin_required
def generate():
    month = request.form.get("month") or date.today().strftime("%Y-%m")
    days = month_days(month)
    with db() as c:
        employees = c.execute("SELECT * FROM employees WHERE active=1 ORDER BY monthly_days DESC,name").fetchall()
        patterns = c.execute("SELECT * FROM shift_patterns WHERE active=1 ORDER BY start_time").fetchall()
        reqs = c.execute("SELECT * FROM requests WHERE substr(request_date,1,7)=?", (month,)).fetchall()
        existing = c.execute("SELECT * FROM assignments WHERE substr(work_date,1,7)=?", (month,)).fetchall()
        # Replace only draft schedule for selected month.
        c.execute("DELETE FROM assignments WHERE substr(work_date,1,7)=? AND status='draft'", (month,))
        approved = {(r["employee_id"],r["request_date"]):r for r in reqs
                    if r["request_type"] in ("有給休暇","確定休") and r["status"] == "approved"}
        requests_by_emp = {}
        for r in reqs:
            requests_by_emp.setdefault(r["employee_id"], []).append(r)
        counts = {e["id"]:0 for e in employees}
        last_dates = {e["id"]:[] for e in employees}
        # Greedy baseline generator. It prioritizes approved time-off and monthly target.
        for d in days:
            weekday = d.weekday()
            candidates = []
            for e in employees:
                if (e["id"], d.isoformat()) in approved:
                    continue
                rlist = requests_by_emp.get(e["id"], [])
                if any(r["request_date"] == d.isoformat() and r["request_type"] in ("有給休暇","確定休") and r["status"] == "approved" for r in rlist):
                    continue
                # Avoid days explicitly marked ordinary/desired where possible.
                penalty = 0
                for r in rlist:
                    if r["request_date"] == d.isoformat():
                        if r["request_type"] in ("有給休暇","確定休") and r["status"] != "approved":
                            penalty += 100
                        elif r["request_type"] == "通常の休み希望": penalty += 50
                        elif r["request_type"] == "できれば休み": penalty += 15
                        elif r["request_type"] == "申請中": penalty += 30
                consecutive = 0
                for x in reversed(last_dates[e["id"]]):
                    if x == d - timedelta(days=consecutive+1): consecutive += 1
                    else: break
                if consecutive >= int(e["max_consecutive"] or 5):
                    continue
                if counts[e["id"]] >= int(e["monthly_days"] or 20):
                    continue
                eligible = []
                for p in patterns:
                    if p["name"].startswith("早") and not e["early_ok"]: continue
                    if p["name"].startswith("中") and not e["mid_ok"]: continue
                    if p["name"].startswith("遅") and not e["late_ok"]: continue
                    duration = (mins(p["end_time"])-mins(p["start_time"])-int(p["break_minutes"] or 0))/60
                    if duration < float(e["min_hours"] or 0) or duration > float(e["max_hours"] or 24): continue
                    eligible.append(p)
                if eligible:
                    candidates.append((penalty + counts[e["id"]]*3 + consecutive*5, e, eligible))
            # Aim for coverage on all 3 skills; this first prototype uses one primary skill per shift.
            daily_assigned = []
            rules = c.execute("SELECT * FROM coverage WHERE weekday=?", (weekday,)).fetchall()
            # Work out per-skill rough target based on overlap, assigning under-covered skill first.
            target = {s: max([r["min_count"] for r in rules if r["skill"] == s] or [1]) for s in ("merchant","consumer","PP")}
            skill_counts = {s:0 for s in target}
            candidates.sort(key=lambda x: x[0])
            for _, e, eligible in candidates:
                if len(daily_assigned) >= max(1, len(employees)-2):
                    break
                skills = [s for s in (e["skills"] or "").split(",") if s]
                if not skills: continue
                skill = min(skills, key=lambda s: skill_counts.get(s,0)/max(target.get(s,1),1))
                p = eligible[0]
                # Avoid giving everyone a shift every day: cap around requested monthly workdays.
                c.execute("INSERT OR IGNORE INTO assignments(employee_id,work_date,pattern_id,skill,status) VALUES(?,?,?,?,?)",
                          (e["id"], d.isoformat(), p["id"], skill, "draft"))
                if c.total_changes:
                    counts[e["id"]] += 1
                    last_dates[e["id"]].append(d)
                    skill_counts[skill] = skill_counts.get(skill,0)+1
                    daily_assigned.append(e["id"])
        flash(f"{month}の仮シフトを作成しました。自動作成は初期版の簡易方式です。必ず警告・必要人数を確認してください。")
    return redirect(url_for("index", month=month))

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
    month = get_month()
    with db() as c:
        rows = c.execute("""SELECT a.work_date,e.name,p.name pattern_name,p.start_time,p.end_time,a.skill,a.status,a.note
        FROM assignments a JOIN employees e ON e.id=a.employee_id JOIN shift_patterns p ON p.id=a.pattern_id
        WHERE substr(a.work_date,1,7)=? ORDER BY a.work_date,p.start_time,e.name""",(month,)).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["日付","従業員","勤務パターン","開始","終了","担当スキル","状態","備考"])
    for r in rows: writer.writerow(list(r))
    data = io.BytesIO(("\ufeff"+output.getvalue()).encode("utf-8"))
    return send_file(data, mimetype="text/csv; charset=utf-8", as_attachment=True, download_name=f"shift_{month}.csv")

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
    if request.method == "POST":
        old, new = request.form.get("old_password",""), request.form.get("new_password","")
        if len(new) < 8 or len(new) > 16 or sum(bool(any(ch.isupper() for ch in new),) for _ in []) > 3:
            flash("新しいパスワードは8〜16文字にしてください。", "error")
        else:
            categories = [any(ch.isupper() for ch in new), any(ch.islower() for ch in new),
                          any(ch.isdigit() for ch in new), any(not ch.isalnum() for ch in new)]
            if sum(categories) < 3:
                flash("大文字・小文字・数字・記号のうち3種類以上を含めてください。", "error")
            else:
                with db() as c:
                    u = c.execute("SELECT * FROM users WHERE username=?", (session["username"],)).fetchone()
                    if not check_password_hash(u["password_hash"], old):
                        flash("現在のパスワードが違います。", "error")
                    else:
                        c.execute("UPDATE users SET password_hash=? WHERE username=?",
                                  (generate_password_hash(new),session["username"]))
                        flash("パスワードを変更しました。")
                        return redirect(url_for("index"))
    return render_template("password.html")

if __name__ == "__main__":
    init_db()
    # Bind only to LAN interfaces when explicitly configured; default local-only for safe first run.
    host = os.environ.get("SHIFT_HOST", "0.0.0.0")
    port = int(os.environ.get("SHIFT_PORT", "5050"))
    app.run(host=host, port=port, debug=False)
