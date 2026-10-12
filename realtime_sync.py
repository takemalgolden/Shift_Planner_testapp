"""Local SQLite operator sync: 2-second polling, persistent snapshots, soft deletion.

Runs in the shift planner server process. Both databases must be on same host.
"""
import hashlib
import json
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone

INTERVAL = 2
SKILL_TO_BREAK = {'consumer':'Consumer','merchant':'Merchant','PP':'PP'}
SKILL_TO_SHIFT = {v:k for k,v in SKILL_TO_BREAK.items()}
_lock = threading.Lock()
_status = {'last_success':None,'last_error':None,'changes':0}

def status():
    with _lock:
        return dict(_status)

def _connect(path):
    c=sqlite3.connect(path, timeout=8)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA busy_timeout=8000')
    return c

def _rows(s,b):
    sr={r['id']:dict(r) for r in s.execute("""SELECT e.id,e.name,e.skills,e.active,e.source_user_id,e.source_username,u.username
          FROM employees e JOIN users u ON u.employee_id=e.id AND u.role='operator'""")}
    br={r['id']:dict(r) for r in b.execute("SELECT id,username,display_name,skills,active FROM users WHERE role='operator'")}
    return sr,br

def _sig(row, side):
    if side=='s':
        fields=(row['username'],row['name'],row['skills'],row['active'])
    else:
        try: skills=','.join(sorted(json.loads(row['skills'] or '[]')))
        except (ValueError,TypeError): skills=row['skills']
        fields=(row['username'],row['display_name'],skills,row['active'])
    return hashlib.sha256(json.dumps(fields,ensure_ascii=False).encode()).hexdigest()

def _password():
    return 'T'+secrets.token_urlsafe(9)[:11]+'7!'

def _apply_break_to_shift(s, r):
    eid_row=s.execute('SELECT id FROM employees WHERE source_user_id=?',(r['id'],)).fetchone()
    eid=eid_row['id'] if eid_row else None
    if not eid:
        byname=s.execute("SELECT e.id FROM employees e JOIN users u ON u.employee_id=e.id AND u.role='operator' WHERE u.username=?",(r['username'],)).fetchone()
        eid=byname['id'] if byname else None
    skills=','.join(dict.fromkeys(SKILL_TO_SHIFT[x] for x in json.loads(r['skills'] or '[]') if x in SKILL_TO_SHIFT)) or 'consumer'
    if eid:
        s.execute('UPDATE employees SET name=?,skills=?,active=?,source_user_id=?,source_username=? WHERE id=?',(r['display_name'],skills,r['active'],r['id'],r['username'],eid))
        s.execute("UPDATE users SET username=? WHERE employee_id=? AND role='operator'",(r['username'],eid))
    else:
        cur=s.execute('INSERT INTO employees(name,skills,active,source_user_id,source_username) VALUES(?,?,?,?,?)',(r['display_name'],skills,r['active'],r['id'],r['username']))
        s.execute("INSERT INTO users(username,password_hash,role,employee_id,must_change_password) VALUES(?,?,'operator',?,1)",(r['username'],'!external-auth-only!',cur.lastrowid))

def _apply_shift_to_break(s,b,r):
    target=b.execute("SELECT id FROM users WHERE id=? AND role='operator'",(r['source_user_id'],)).fetchone() if r['source_user_id'] else None
    if not target:
        target=b.execute('SELECT id FROM users WHERE username=? AND role=\'operator\'',(r['username'],)).fetchone()
    skills=[SKILL_TO_BREAK[x.strip()] for x in r['skills'].split(',') if x.strip() in SKILL_TO_BREAK] or ['Consumer']
    if target:
        uid=target['id']
        b.execute('UPDATE users SET username=?,display_name=?,skills=?,active=? WHERE id=?',(r['username'],r['name'],json.dumps(skills),r['active'],uid))
    else:
        password=_password()
        salt=secrets.token_hex(16)
        digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),240000).hex()
        uid=b.execute("INSERT INTO users(username,display_name,role,salt,password_hash,active,skills,shift_start,shift_end) VALUES(?,?,'operator',?,?,?,?,?,?)",(r['username'],r['name'],salt,digest,r['active'],json.dumps(skills),'09:00','18:00')).lastrowid
        # No unattended password disclosure: admin must use password reset to provision new account.
    s.execute('UPDATE employees SET source_user_id=?,source_username=? WHERE id=?',(uid,r['username'],r['id']))

def sync_once(shift_path,break_path):
    if not os.path.isfile(break_path) or os.path.realpath(shift_path)==os.path.realpath(break_path):
        raise ValueError('休憩管理DBのパスを確認してください')
    with _lock:
        s=_connect(shift_path)
        try:
            s.execute('CREATE TABLE IF NOT EXISTS operator_sync_snapshots(side TEXT NOT NULL,record_id INTEGER NOT NULL,signature TEXT NOT NULL,PRIMARY KEY(side,record_id))')
            s.commit()
            b=_connect(break_path)
            try:
                # ATTACH gives one consistent transaction for both files; do not share SQLite via SMB.
                b.close()
                s.execute('ATTACH DATABASE ? AS breakdb',(break_path,))
                s.execute('BEGIN IMMEDIATE')
                sr,br=_rows(s,_BreakView(s))
                old={(r['side'],r['record_id']):r['signature'] for r in s.execute('SELECT * FROM operator_sync_snapshots')}
                initialized=bool(old)
                changed_s={i for i,r in sr.items() if old.get(('s',i))!=_sig(r,'s')}
                changed_b={i for i,r in br.items() if old.get(('b',i))!=_sig(r,'b')}
                missing_s={i for side,i in old if side=='s' and i not in sr}
                missing_b={i for side,i in old if side=='b' and i not in br}
                count=0
                # Hard deletes are mirrored as deactivation, preserving shift history.
                for eid in missing_s:
                    linked=s.execute('SELECT source_user_id FROM operator_sync_links WHERE employee_id=?',(eid,)).fetchone() if _has_links(s) else None
                    if linked:
                        s.execute("UPDATE breakdb.users SET active=0 WHERE id=? AND role='operator'",(linked['source_user_id'],));count+=1
                for uid in missing_b:
                    s.execute('UPDATE employees SET active=0 WHERE source_user_id=?',(uid,));count+=1
                # Existing linked records: a changed side wins; simultaneous edits resolve to break side.
                handled_s=set();handled_b=set()
                for eid,r in sr.items():
                    uid=r['source_user_id']
                    if uid in br:
                        handled_s.add(eid);handled_b.add(uid)
                        if uid in changed_b and (eid not in changed_s or initialized):
                            _apply_break_to_shift(s,br[uid]);count+=1
                        elif eid in changed_s:
                            _apply_shift_to_break(s,_BreakView(s),r);count+=1
                prior_linked = {row["source_user_id"] for row in s.execute("SELECT source_user_id FROM operator_sync_links")} if _has_links(s) else set()
                # New/unlinked profiles are matched by username, break side wins on collision.
                for uid,r in br.items():
                    if uid not in handled_b and uid not in missing_b and uid not in prior_linked and (uid in changed_b or not initialized):
                        _apply_break_to_shift(s,r);count+=1
                sr,br=_rows(s,_BreakView(s))
                for eid,r in sr.items():
                    if r['source_user_id'] not in br and eid not in missing_s and r['source_user_id'] not in missing_b and (eid in changed_s or not initialized):
                        _apply_shift_to_break(s,_BreakView(s),r);count+=1
                sr,br=_rows(s,_BreakView(s))
                s.execute('DELETE FROM operator_sync_snapshots')
                s.executemany('INSERT INTO operator_sync_snapshots VALUES(?,?,?)', [('s',i,_sig(r,'s')) for i,r in sr.items()]+[('b',i,_sig(r,'b')) for i,r in br.items()])
                s.execute('CREATE TABLE IF NOT EXISTS operator_sync_links(employee_id INTEGER PRIMARY KEY,source_user_id INTEGER NOT NULL)')
                s.execute('DELETE FROM operator_sync_links')
                s.executemany('INSERT INTO operator_sync_links VALUES(?,?)',[(i,r['source_user_id']) for i,r in sr.items() if r['source_user_id']])
                s.commit()
                _status.update(last_success=datetime.now(timezone.utc).isoformat(timespec='seconds'),last_error=None,changes=count)
                return count
            except Exception:
                s.rollback();raise
            finally:
                try:s.execute('DETACH DATABASE breakdb')
                except sqlite3.Error:pass
        except Exception as exc:
            _status['last_error']=str(exc)
            raise
        finally:s.close()

class _BreakView:
    def __init__(self,conn):self.conn=conn
    def execute(self,sql,args=()):
        return self.conn.execute(sql.replace('FROM users','FROM breakdb.users').replace('INTO users','INTO breakdb.users').replace('UPDATE users','UPDATE breakdb.users'),args)

def _has_links(c):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operator_sync_links'").fetchone())

def start_worker(shift_path,get_break_path,enabled):
    def loop():
        while True:
            try:
                if enabled():sync_once(shift_path,get_break_path())
            except Exception as exc:
                print('[operator-sync]',type(exc).__name__,str(exc),flush=True)
            time.sleep(INTERVAL)
    threading.Thread(target=loop,daemon=True,name='operator-sync').start()
