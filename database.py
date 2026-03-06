# ============================================================
#  database.py — PostgreSQL (Railway) + SQLite (локально)
#  Автоматически выбирает БД по DATABASE_URL
# ============================================================
import os, json, time
from datetime import datetime, date
from config import START_COINS, LEVELS, DAILY_TASKS, WIN_CHANCE

DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_PG = bool(DATABASE_URL)

if USE_PG:
    import psycopg2, psycopg2.extras
else:
    import sqlite3
    from config import DB_FILE


def get_conn():
    if USE_PG:
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def _q(sql):
    return sql.replace("?", "%s") if USE_PG else sql

def _exec(conn, sql, params=()):
    c = conn.cursor(); c.execute(_q(sql), params); return c

def _one(conn, sql, params=()):
    row = _exec(conn, sql, params).fetchone()
    return dict(row) if row else None

def _all(conn, sql, params=()):
    return [dict(r) for r in _exec(conn, sql, params).fetchall()]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ИНИЦИАЛИЗАЦИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def init_db():
    conn = get_conn()
    if USE_PG:
        _exec(conn, """CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY, username TEXT, full_name TEXT,
            coins BIGINT DEFAULT 0, stars INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
            total_bet BIGINT DEFAULT 0, is_vip INTEGER DEFAULT 0,
            vip_until BIGINT DEFAULT 0, daily_last TEXT DEFAULT '',
            tasks_date TEXT DEFAULT '', tasks_json TEXT DEFAULT '{}',
            registered BIGINT DEFAULT 0)""")
        _exec(conn, """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT)""")
        _exec(conn, """CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY, user_id BIGINT, amount BIGINT,
            type TEXT, description TEXT, created_at BIGINT)""")
        _exec(conn, """CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY, coins INTEGER DEFAULT 0,
            vip_days INTEGER DEFAULT 0, max_uses INTEGER DEFAULT 1,
            uses INTEGER DEFAULT 0, created_at BIGINT DEFAULT 0,
            expires_at BIGINT DEFAULT 0, note TEXT DEFAULT '')""")
        _exec(conn, """CREATE TABLE IF NOT EXISTS promo_used (
            code TEXT, user_id BIGINT, used_at BIGINT,
            PRIMARY KEY (code, user_id))""")
    else:
        _exec(conn, """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT,
            coins INTEGER DEFAULT 0, stars INTEGER DEFAULT 0,
            level INTEGER DEFAULT 1, xp INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0,
            total_bet INTEGER DEFAULT 0, is_vip INTEGER DEFAULT 0,
            vip_until INTEGER DEFAULT 0, daily_last TEXT DEFAULT '',
            tasks_date TEXT DEFAULT '', tasks_json TEXT DEFAULT '{}',
            registered INTEGER DEFAULT 0)""")
        _exec(conn, """CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY, value TEXT)""")
        _exec(conn, """CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
            amount INTEGER, type TEXT, description TEXT, created_at INTEGER)""")
        _exec(conn, """CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY, coins INTEGER DEFAULT 0,
            vip_days INTEGER DEFAULT 0, max_uses INTEGER DEFAULT 1,
            uses INTEGER DEFAULT 0, created_at INTEGER DEFAULT 0,
            expires_at INTEGER DEFAULT 0, note TEXT DEFAULT '')""")
        _exec(conn, """CREATE TABLE IF NOT EXISTS promo_used (
            code TEXT, user_id INTEGER, used_at INTEGER,
            PRIMARY KEY (code, user_id))""")

    for game, chance in WIN_CHANCE.items():
        if USE_PG:
            _exec(conn, "INSERT INTO settings (key,value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING",
                  (f"win_chance_{game}", str(chance)))
        else:
            _exec(conn, "INSERT OR IGNORE INTO settings (key,value) VALUES (?,?)",
                  (f"win_chance_{game}", str(chance)))

    conn.commit(); conn.close()
    print(f"✅ БД: {'PostgreSQL' if USE_PG else 'SQLite'}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ПОЛЬЗОВАТЕЛИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_user(user_id):
    conn = get_conn()
    r = _one(conn, "SELECT * FROM users WHERE user_id=?", (user_id,))
    conn.close(); return r

def register_user(user_id, username, full_name):
    conn = get_conn()
    if USE_PG:
        _exec(conn, "INSERT INTO users (user_id,username,full_name,coins,registered) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING",
              (user_id, username or "", full_name or "Игрок", START_COINS, int(time.time())))
    else:
        _exec(conn, "INSERT OR IGNORE INTO users (user_id,username,full_name,coins,registered) VALUES (?,?,?,?,?)",
              (user_id, username or "", full_name or "Игрок", START_COINS, int(time.time())))
    conn.commit(); conn.close()

def update_coins(user_id, amount):
    conn = get_conn()
    sql = "UPDATE users SET coins=GREATEST(0,coins+?) WHERE user_id=?" if USE_PG else "UPDATE users SET coins=MAX(0,coins+?) WHERE user_id=?"
    _exec(conn, sql, (amount, user_id))
    conn.commit(); conn.close()

def set_coins(user_id, amount):
    conn = get_conn()
    _exec(conn, "UPDATE users SET coins=? WHERE user_id=?", (max(0,amount), user_id))
    conn.commit(); conn.close()

def add_xp(user_id, xp):
    conn = get_conn()
    row = _one(conn, "SELECT level,xp FROM users WHERE user_id=?", (user_id,))
    if not row: conn.close(); return
    new_xp, new_level = row["xp"] + xp, row["level"]
    while new_level < max(LEVELS.keys()) and new_xp >= LEVELS[new_level]:
        new_xp -= LEVELS[new_level]; new_level += 1
    _exec(conn, "UPDATE users SET xp=?,level=? WHERE user_id=?", (new_xp, new_level, user_id))
    conn.commit(); conn.close()

def record_game(user_id, won, bet):
    conn = get_conn()
    if won:
        _exec(conn, "UPDATE users SET wins=wins+1,total_bet=total_bet+? WHERE user_id=?", (bet, user_id))
    else:
        _exec(conn, "UPDATE users SET losses=losses+1,total_bet=total_bet+? WHERE user_id=?", (bet, user_id))
    conn.commit(); conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ЕЖЕДНЕВНЫЙ БОНУС
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def claim_daily(user_id):
    from config import DAILY_BONUS
    conn = get_conn()
    user = _one(conn, "SELECT daily_last,is_vip FROM users WHERE user_id=?", (user_id,))
    today = str(date.today())
    if user["daily_last"] == today:
        now = datetime.now()
        secs = 86400 - (now.hour*3600 + now.minute*60 + now.second)
        conn.close(); return {"ok": False, "seconds_left": secs}
    bonus = DAILY_BONUS * 2 if user["is_vip"] else DAILY_BONUS
    _exec(conn, "UPDATE users SET daily_last=?,coins=coins+? WHERE user_id=?", (today, bonus, user_id))
    conn.commit(); conn.close()
    return {"ok": True, "amount": bonus}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  VIP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def set_vip(user_id, days):
    conn = get_conn()
    _exec(conn, "UPDATE users SET is_vip=1,vip_until=? WHERE user_id=?",
          (int(time.time()) + days*86400, user_id))
    conn.commit(); conn.close()

def check_vip_expired():
    conn = get_conn()
    _exec(conn, "UPDATE users SET is_vip=0 WHERE is_vip=1 AND vip_until>0 AND vip_until<?", (int(time.time()),))
    conn.commit(); conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ЗАДАНИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_tasks(user_id):
    conn = get_conn()
    user = _one(conn, "SELECT tasks_date,tasks_json FROM users WHERE user_id=?", (user_id,))
    conn.close()
    today = str(date.today())
    if not user or user["tasks_date"] != today:
        fresh = {t["id"]: {"progress":0,"done":False} for t in DAILY_TASKS}
        _save_tasks(user_id, today, fresh); return fresh
    return json.loads(user["tasks_json"] or "{}")

def _save_tasks(user_id, day, tasks):
    conn = get_conn()
    _exec(conn, "UPDATE users SET tasks_date=?,tasks_json=? WHERE user_id=?",
          (day, json.dumps(tasks), user_id))
    conn.commit(); conn.close()

def update_task_progress(user_id, task_id, amount=1):
    tasks = get_tasks(user_id)
    today = str(date.today())
    meta  = next((t for t in DAILY_TASKS if t["id"] == task_id), None)
    if not meta: return 0
    entry = tasks.get(task_id, {"progress":0,"done":False})
    if entry["done"]: return 0
    entry["progress"] = entry.get("progress",0) + amount
    reward = 0
    if entry["progress"] >= meta["target"]:
        entry["done"] = True; entry["progress"] = meta["target"]
        reward = meta["reward"]; update_coins(user_id, reward)
    tasks[task_id] = entry; _save_tasks(user_id, today, tasks)
    return reward


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ПРОМОКОДЫ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def create_promo(code, coins=0, vip_days=0, max_uses=1, expires_days=0, note=""):
    conn = get_conn()
    expires_at = int(time.time()) + expires_days*86400 if expires_days > 0 else 0
    if USE_PG:
        _exec(conn, """INSERT INTO promocodes (code,coins,vip_days,max_uses,uses,created_at,expires_at,note)
              VALUES (%s,%s,%s,%s,0,%s,%s,%s) ON CONFLICT (code) DO NOTHING""",
              (code.upper(), coins, vip_days, max_uses, int(time.time()), expires_at, note))
    else:
        _exec(conn, """INSERT OR IGNORE INTO promocodes (code,coins,vip_days,max_uses,uses,created_at,expires_at,note)
              VALUES (?,?,?,?,0,?,?,?)""",
              (code.upper(), coins, vip_days, max_uses, int(time.time()), expires_at, note))
    conn.commit(); conn.close()

def use_promo(user_id, code) -> dict:
    """Активировать промокод. Возвращает результат."""
    conn = get_conn()
    promo = _one(conn, "SELECT * FROM promocodes WHERE code=?", (code.upper(),))
    if not promo:
        conn.close(); return {"ok": False, "err": "Промокод не найден"}

    if promo["expires_at"] > 0 and promo["expires_at"] < int(time.time()):
        conn.close(); return {"ok": False, "err": "Промокод истёк"}

    if promo["uses"] >= promo["max_uses"]:
        conn.close(); return {"ok": False, "err": "Промокод уже использован максимальное число раз"}

    already = _one(conn, "SELECT 1 FROM promo_used WHERE code=? AND user_id=?", (code.upper(), user_id))
    if already:
        conn.close(); return {"ok": False, "err": "Ты уже использовал этот промокод"}

    # Применяем
    if USE_PG:
        _exec(conn, "INSERT INTO promo_used (code,user_id,used_at) VALUES (%s,%s,%s)",
              (code.upper(), user_id, int(time.time())))
        _exec(conn, "UPDATE promocodes SET uses=uses+1 WHERE code=%s", (code.upper(),))
    else:
        _exec(conn, "INSERT INTO promo_used (code,user_id,used_at) VALUES (?,?,?)",
              (code.upper(), user_id, int(time.time())))
        _exec(conn, "UPDATE promocodes SET uses=uses+1 WHERE code=?", (code.upper(),))

    if promo["coins"] > 0:
        update_coins(user_id, promo["coins"])
    if promo["vip_days"] > 0:
        set_vip(user_id, promo["vip_days"])

    conn.commit(); conn.close()
    return {"ok": True, "coins": promo["coins"], "vip_days": promo["vip_days"]}

def get_all_promos():
    conn = get_conn()
    rows = _all(conn, "SELECT * FROM promocodes ORDER BY created_at DESC")
    conn.close(); return rows

def delete_promo(code):
    conn = get_conn()
    _exec(conn, "DELETE FROM promocodes WHERE code=?", (code.upper(),))
    _exec(conn, "DELETE FROM promo_used WHERE code=?", (code.upper(),))
    conn.commit(); conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ТОП / СТАТИСТИКА / НАСТРОЙКИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_top(limit=10):
    conn = get_conn()
    rows = _all(conn, "SELECT user_id,full_name,coins,level,wins FROM users ORDER BY coins DESC LIMIT ?", (limit,))
    conn.close(); return rows

def get_setting(key, default=None):
    conn = get_conn()
    row = _one(conn, "SELECT value FROM settings WHERE key=?", (key,))
    conn.close(); return row["value"] if row else default

def set_setting(key, value):
    conn = get_conn()
    if USE_PG:
        _exec(conn, "INSERT INTO settings (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value",
              (key, value))
    else:
        _exec(conn, "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    conn.commit(); conn.close()

def get_win_chance(game):
    val = get_setting(f"win_chance_{game}")
    return float(val) if val is not None else WIN_CHANCE.get(game, 0.4)

def get_stats():
    conn = get_conn()
    r = {
        "total_users":  _one(conn,"SELECT COUNT(*) as c FROM users")["c"],
        "total_coins":  _one(conn,"SELECT COALESCE(SUM(coins),0) as c FROM users")["c"],
        "total_wins":   _one(conn,"SELECT COALESCE(SUM(wins),0) as c FROM users")["c"],
        "total_losses": _one(conn,"SELECT COALESCE(SUM(losses),0) as c FROM users")["c"],
        "vip_count":    _one(conn,"SELECT COUNT(*) as c FROM users WHERE is_vip=1")["c"],
        "new_today":    _one(conn,"SELECT COUNT(*) as c FROM users WHERE registered>?",
                             (int(time.time())-86400,))["c"],
        "promo_count":  _one(conn,"SELECT COUNT(*) as c FROM promocodes")["c"],
    }
    conn.close(); return r

def get_all_user_ids():
    conn = get_conn()
    rows = _all(conn, "SELECT user_id FROM users")
    conn.close(); return [r["user_id"] for r in rows]
