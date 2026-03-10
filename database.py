# ============================================================
#  database.py — PostgreSQL (Railway) + SQLite (локально)
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

# ── Для SQLite: одно соединение на весь процесс (без блокировок) ──
_sqlite_conn = None

def get_conn():
    global _sqlite_conn
    if USE_PG:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        return conn
    # SQLite: переиспользуем одно соединение с таймаутом
    if _sqlite_conn is None:
        _sqlite_conn = sqlite3.connect(
            DB_FILE,
            check_same_thread=False,
            timeout=30,
            isolation_level=None   # autocommit mode
        )
        _sqlite_conn.row_factory = sqlite3.Row
        _sqlite_conn.execute("PRAGMA journal_mode=WAL")   # WAL — без блокировок
        _sqlite_conn.execute("PRAGMA synchronous=NORMAL")
    return _sqlite_conn

def _commit(conn):
    if USE_PG:
        conn.commit()
    # SQLite в autocommit — коммит не нужен

def _close(conn):
    if USE_PG:
        conn.close()
    # SQLite соединение не закрываем — переиспользуем

def _q(sql):
    return sql.replace("?", "%s") if USE_PG else sql

def _exec(conn, sql, params=()):
    c = conn.cursor()
    c.execute(_q(sql), params)
    return c

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

    # Коммитим основные таблицы
    _commit(conn)

    # Новые таблицы — каждая в своей транзакции
    init_referral(conn)
    _commit(conn)
    init_deposits(conn)
    _commit(conn)
    init_tournament(conn)
    _commit(conn)

    # Добавляем новые колонки если не существуют
    for col, default in [("usdt", "0"), ("prestige", "0"), ("title", "''")]:
        try:
            if USE_PG:
                _exec(conn, f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            else:
                _exec(conn, f"ALTER TABLE users ADD COLUMN {col} INTEGER DEFAULT 0")
            _commit(conn)
        except: pass
    # title — текстовая колонка
    try:
        if USE_PG:
            _exec(conn, "ALTER TABLE users ADD COLUMN title TEXT DEFAULT ''")
        else:
            _exec(conn, "ALTER TABLE users ADD COLUMN title TEXT DEFAULT ''")
        _commit(conn)
    except: pass

    _close(conn)
    print(f"✅ БД: {'PostgreSQL' if USE_PG else 'SQLite'}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ПОЛЬЗОВАТЕЛИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_user(user_id):
    conn = get_conn()
    r = _one(conn, "SELECT * FROM users WHERE user_id=?", (user_id,))
    _close(conn); return r

def register_user(user_id, username, full_name):
    conn = get_conn()
    if USE_PG:
        _exec(conn, """INSERT INTO users (user_id,username,full_name,coins,registered)
              VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id) DO NOTHING""",
              (user_id, username or "", full_name or "Игрок", START_COINS, int(time.time())))
    else:
        _exec(conn, """INSERT OR IGNORE INTO users (user_id,username,full_name,coins,registered)
              VALUES (?,?,?,?,?)""",
              (user_id, username or "", full_name or "Игрок", START_COINS, int(time.time())))
    _commit(conn); _close(conn)

def update_coins(user_id, amount):
    conn = get_conn()
    sql = ("UPDATE users SET coins=GREATEST(0,coins+%s) WHERE user_id=%s" if USE_PG
           else "UPDATE users SET coins=MAX(0,coins+?) WHERE user_id=?")
    _exec(conn, sql, (amount, user_id))
    _commit(conn); _close(conn)

def set_coins(user_id, amount):
    conn = get_conn()
    _exec(conn, "UPDATE users SET coins=? WHERE user_id=?", (max(0,amount), user_id))
    _commit(conn); _close(conn)

def add_xp(user_id, xp):
    conn = get_conn()
    row = _one(conn, "SELECT level,xp FROM users WHERE user_id=?", (user_id,))
    if not row: _close(conn); return
    new_xp, new_level = row["xp"] + xp, row["level"]
    while new_level < max(LEVELS.keys()) and new_xp >= LEVELS[new_level]:
        new_xp -= LEVELS[new_level]; new_level += 1
    _exec(conn, "UPDATE users SET xp=?,level=? WHERE user_id=?", (new_xp, new_level, user_id))
    _commit(conn); _close(conn)

def record_game(user_id, won, bet):
    conn = get_conn()
    if won:
        _exec(conn, "UPDATE users SET wins=wins+1,total_bet=total_bet+? WHERE user_id=?", (bet,user_id))
    else:
        _exec(conn, "UPDATE users SET losses=losses+1,total_bet=total_bet+? WHERE user_id=?", (bet,user_id))
    _commit(conn); _close(conn)
    # Турнирные очки за победу
    if won and bet > 0:
        try:
            row = get_user(user_id)
            if row:
                add_tournament_points(user_id, row["full_name"], bet)
        except: pass


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
        _close(conn); return {"ok": False, "seconds_left": secs}
    bonus = DAILY_BONUS * 2 if user["is_vip"] else DAILY_BONUS
    _exec(conn, "UPDATE users SET daily_last=?,coins=coins+? WHERE user_id=?", (today,bonus,user_id))
    _commit(conn); _close(conn)
    return {"ok": True, "amount": bonus}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  VIP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def set_vip(user_id, days):
    conn = get_conn()
    _exec(conn, "UPDATE users SET is_vip=1,vip_until=? WHERE user_id=?",
          (int(time.time())+days*86400, user_id))
    _commit(conn); _close(conn)

def check_vip_expired():
    conn = get_conn()
    _exec(conn, "UPDATE users SET is_vip=0 WHERE is_vip=1 AND vip_until>0 AND vip_until<?",
          (int(time.time()),))
    _commit(conn); _close(conn)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ЗАДАНИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_tasks(user_id):
    conn = get_conn()
    user = _one(conn, "SELECT tasks_date,tasks_json FROM users WHERE user_id=?", (user_id,))
    _close(conn)
    today = str(date.today())
    if not user or user["tasks_date"] != today:
        fresh = {t["id"]: {"progress":0,"done":False} for t in DAILY_TASKS}
        _save_tasks(user_id, today, fresh); return fresh
    return json.loads(user["tasks_json"] or "{}")

def _save_tasks(user_id, day, tasks):
    conn = get_conn()
    _exec(conn, "UPDATE users SET tasks_date=?,tasks_json=? WHERE user_id=?",
          (day, json.dumps(tasks), user_id))
    _commit(conn); _close(conn)

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
        _exec(conn, """INSERT OR IGNORE INTO promocodes
              (code,coins,vip_days,max_uses,uses,created_at,expires_at,note)
              VALUES (?,?,?,?,0,?,?,?)""",
              (code.upper(), coins, vip_days, max_uses, int(time.time()), expires_at, note))
    _commit(conn); _close(conn)

def use_promo(user_id, code) -> dict:
    conn = get_conn()
    promo = _one(conn, "SELECT * FROM promocodes WHERE code=?", (code.upper(),))
    if not promo:
        _close(conn); return {"ok": False, "err": "Промокод не найден"}
    if promo["expires_at"] > 0 and promo["expires_at"] < int(time.time()):
        _close(conn); return {"ok": False, "err": "Промокод истёк"}
    if promo["uses"] >= promo["max_uses"]:
        _close(conn); return {"ok": False, "err": "Промокод уже использован максимальное число раз"}
    already = _one(conn, "SELECT 1 as x FROM promo_used WHERE code=? AND user_id=?",
                   (code.upper(), user_id))
    if already:
        _close(conn); return {"ok": False, "err": "Ты уже использовал этот промокод"}

    if USE_PG:
        _exec(conn, "INSERT INTO promo_used (code,user_id,used_at) VALUES (%s,%s,%s)",
              (code.upper(), user_id, int(time.time())))
        _exec(conn, "UPDATE promocodes SET uses=uses+1 WHERE code=%s", (code.upper(),))
    else:
        _exec(conn, "INSERT INTO promo_used (code,user_id,used_at) VALUES (?,?,?)",
              (code.upper(), user_id, int(time.time())))
        _exec(conn, "UPDATE promocodes SET uses=uses+1 WHERE code=?", (code.upper(),))

    coins    = promo["coins"]
    vip_days = promo["vip_days"]
    _commit(conn); _close(conn)

    if coins > 0:    update_coins(user_id, coins)
    if vip_days > 0: set_vip(user_id, vip_days)

    return {"ok": True, "coins": coins, "vip_days": vip_days}

def get_all_promos():
    conn = get_conn()
    rows = _all(conn, "SELECT * FROM promocodes ORDER BY created_at DESC")
    _close(conn); return rows

def delete_promo(code):
    conn = get_conn()
    _exec(conn, "DELETE FROM promocodes WHERE code=?", (code.upper(),))
    _exec(conn, "DELETE FROM promo_used WHERE code=?", (code.upper(),))
    _commit(conn); _close(conn)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ТОП / СТАТИСТИКА / НАСТРОЙКИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_top(limit=10):
    conn = get_conn()
    rows = _all(conn,
        "SELECT user_id,full_name,coins,level,wins FROM users ORDER BY coins DESC LIMIT ?",
        (limit,))
    _close(conn); return rows

def get_setting(key, default=None):
    conn = get_conn()
    row = _one(conn, "SELECT value FROM settings WHERE key=?", (key,))
    _close(conn); return row["value"] if row else default

def set_setting(key, value):
    conn = get_conn()
    if USE_PG:
        _exec(conn, """INSERT INTO settings (key,value) VALUES (%s,%s)
              ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value""", (key, value))
    else:
        _exec(conn, "INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))
    _commit(conn); _close(conn)

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
    _close(conn); return r

def get_all_user_ids():
    conn = get_conn()
    rows = _all(conn, "SELECT user_id FROM users")
    _close(conn); return [r["user_id"] for r in rows]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  РЕФЕРАЛЫ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def init_referral(conn):
    _exec(conn, """CREATE TABLE IF NOT EXISTS referrals (
        referrer_id BIGINT, referee_id BIGINT PRIMARY KEY,
        created_at BIGINT DEFAULT 0)""")

def add_referral(referrer_id, referee_id):
    conn = get_conn()
    try:
        if USE_PG:
            _exec(conn, "INSERT INTO referrals (referrer_id,referee_id,created_at) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                  (referrer_id, referee_id, int(time.time())))
        else:
            _exec(conn, "INSERT OR IGNORE INTO referrals (referrer_id,referee_id,created_at) VALUES (?,?,?)",
                  (referrer_id, referee_id, int(time.time())))
        _commit(conn)
    except: pass
    _close(conn)

def get_referrals(user_id):
    conn = get_conn()
    rows = _all(conn, "SELECT * FROM referrals WHERE referrer_id=?", (user_id,))
    _close(conn); return rows

def find_user_by_username(username):
    conn = get_conn()
    row = _one(conn, "SELECT * FROM users WHERE LOWER(username)=LOWER(?)", (username.lstrip("@"),))
    _close(conn); return row

def get_user_by_id_safe(uid_str):
    try:
        conn = get_conn()
        row = _one(conn, "SELECT * FROM users WHERE user_id=?", (int(uid_str),))
        _close(conn); return row
    except: return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  БАНК (ВКЛАДЫ)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def init_deposits(conn):
    _exec(conn, """CREATE TABLE IF NOT EXISTS deposits (
        user_id BIGINT PRIMARY KEY, amount BIGINT DEFAULT 0,
        rate REAL DEFAULT 0, unlock_at BIGINT DEFAULT 0)""")

def get_deposit(user_id):
    conn = get_conn()
    row = _one(conn, "SELECT * FROM deposits WHERE user_id=?", (user_id,))
    _close(conn); return row

def create_deposit(user_id, amount, rate, days):
    conn = get_conn()
    unlock = int(time.time()) + days * 86400
    if USE_PG:
        _exec(conn, """INSERT INTO deposits (user_id,amount,rate,unlock_at) VALUES (%s,%s,%s,%s)
              ON CONFLICT (user_id) DO UPDATE SET amount=%s,rate=%s,unlock_at=%s""",
              (user_id, amount, rate, unlock, amount, rate, unlock))
    else:
        _exec(conn, "INSERT OR REPLACE INTO deposits (user_id,amount,rate,unlock_at) VALUES (?,?,?,?)",
              (user_id, amount, rate, unlock))
    _commit(conn); _close(conn)

def clear_deposit(user_id):
    conn = get_conn()
    _exec(conn, "DELETE FROM deposits WHERE user_id=?", (user_id,))
    _commit(conn); _close(conn)

def get_ready_deposits():
    conn = get_conn()
    rows = _all(conn, "SELECT * FROM deposits WHERE unlock_at>0 AND unlock_at<=? AND amount>0",
                (int(time.time()),))
    _close(conn); return rows


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  СТРИКИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_streak(user_id):
    val = get_setting(f"streak_{user_id}")
    return int(val) if val else 0

def update_streak(user_id):
    """Вызывать при получении ежедневного бонуса. Возвращает новый стрик."""
    last_str  = get_setting(f"streak_date_{user_id}", "")
    today     = str(date.today())
    yesterday = str(date.today().replace(day=date.today().day - 1)) if date.today().day > 1 else ""
    streak    = get_streak(user_id)

    if last_str == today:
        return streak  # уже получили сегодня
    if last_str == yesterday:
        streak += 1    # продолжаем стрик
    else:
        streak = 1     # сброс

    set_setting(f"streak_{user_id}", str(streak))
    set_setting(f"streak_date_{user_id}", today)
    return streak


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ТУРНИР
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def init_tournament(conn):
    _exec(conn, """CREATE TABLE IF NOT EXISTS tournament (
        user_id BIGINT PRIMARY KEY, full_name TEXT, points BIGINT DEFAULT 0)""")

def add_tournament_points(user_id, full_name, points):
    if points <= 0: return
    conn = get_conn()
    if USE_PG:
        _exec(conn, """INSERT INTO tournament (user_id,full_name,points) VALUES (%s,%s,%s)
              ON CONFLICT (user_id) DO UPDATE SET points=tournament.points+%s, full_name=%s""",
              (user_id, full_name, points, points, full_name))
    else:
        existing = _one(conn, "SELECT points FROM tournament WHERE user_id=?", (user_id,))
        if existing:
            _exec(conn, "UPDATE tournament SET points=points+?,full_name=? WHERE user_id=?",
                  (points, full_name, user_id))
        else:
            _exec(conn, "INSERT INTO tournament (user_id,full_name,points) VALUES (?,?,?)",
                  (user_id, full_name, points))
    _commit(conn); _close(conn)

def get_tournament_top(limit=10):
    conn = get_conn()
    rows = _all(conn, "SELECT * FROM tournament ORDER BY points DESC LIMIT ?", (limit,))
    _close(conn); return rows

def get_tournament_position(user_id):
    conn = get_conn()
    if USE_PG:
        row = _one(conn, "SELECT COUNT(*)+1 as pos FROM tournament WHERE points>(SELECT COALESCE(points,0) FROM tournament WHERE user_id=%s)", (user_id,))
    else:
        row = _one(conn, "SELECT COUNT(*)+1 as pos FROM tournament WHERE points>(SELECT COALESCE(points,0) FROM tournament WHERE user_id=?)", (user_id,))
    _close(conn)
    return row["pos"] if row else None

def get_tournament_points(user_id):
    conn = get_conn()
    row = _one(conn, "SELECT points FROM tournament WHERE user_id=?", (user_id,))
    _close(conn); return row["points"] if row else 0

def get_tournament_end():
    val = get_setting("tournament_end")
    if not val:
        # Устанавливаем конец на следующий понедельник
        now  = int(time.time())
        end  = now + (7 - datetime.fromtimestamp(now).weekday()) * 86400
        set_setting("tournament_end", str(end))
        return end
    return int(val)

def reset_tournament():
    conn = get_conn()
    _exec(conn, "DELETE FROM tournament")
    _commit(conn); _close(conn)
    # Следующий турнир через 7 дней
    set_setting("tournament_end", str(int(time.time()) + 7*86400))


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ФУНКЦИИ ДЛЯ АДМИН ПАНЕЛИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def search_users(query: str):
    conn = get_conn()
    q = f"%{query}%"
    try:
        uid = int(query)
        rows = _all(conn, "SELECT * FROM users WHERE user_id=? OR LOWER(username) LIKE LOWER(?) OR LOWER(full_name) LIKE LOWER(?) ORDER BY coins DESC LIMIT 20", (uid, q, q))
    except ValueError:
        rows = _all(conn, "SELECT * FROM users WHERE LOWER(username) LIKE LOWER(?) OR LOWER(full_name) LIKE LOWER(?) ORDER BY coins DESC LIMIT 20", (q, q))
    _close(conn)
    return rows

def get_bank_total():
    conn = get_conn()
    row = _one(conn, "SELECT COALESCE(SUM(amount),0) as c FROM deposits WHERE amount>0")
    _close(conn)
    return row["c"] if row else 0

def get_active_deposits_count():
    conn = get_conn()
    row = _one(conn, "SELECT COUNT(*) as c FROM deposits WHERE amount>0")
    _close(conn)
    return row["c"] if row else 0

def get_promo_total_uses():
    conn = get_conn()
    row = _one(conn, "SELECT COALESCE(SUM(uses),0) as c FROM promocodes")
    _close(conn)
    return row["c"] if row else 0

def get_referral_count():
    conn = get_conn()
    try:
        row = _one(conn, "SELECT COUNT(*) as c FROM referrals")
        _close(conn)
        return row["c"] if row else 0
    except:
        _close(conn)
        return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  USDT & PRESTIGE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
USDT_RATE = 1000  # монет за 1 USDT

PRESTIGE_LEVELS = {
    0: {"name": "",        "title": "",              "bonus": 0,    "price_usdt": 0},
    1: {"name": "🥉 Bronze","title": "💼 Трейдер",    "bonus": 0.02, "price_usdt": 5},
    2: {"name": "🥈 Silver","title": "📈 Инвестор",   "bonus": 0.05, "price_usdt": 15},
    3: {"name": "🥇 Gold",  "title": "💎 Акула",      "bonus": 0.08, "price_usdt": 35},
    4: {"name": "💠 Platinum","title": "🚀 Магнат",   "bonus": 0.12, "price_usdt": 75},
    5: {"name": "💎 Diamond","title": "👑 Легенда",   "bonus": 0.20, "price_usdt": 150},
}

def get_usdt(user_id):
    conn = get_conn()
    r = _one(conn, "SELECT usdt FROM users WHERE user_id=?", (user_id,))
    _close(conn)
    return r["usdt"] if r else 0

def update_usdt(user_id, amount):
    """amount может быть отрицательным"""
    conn = get_conn()
    if USE_PG:
        _exec(conn, "UPDATE users SET usdt=usdt+%s WHERE user_id=%s", (amount, user_id))
    else:
        _exec(conn, "UPDATE users SET usdt=usdt+? WHERE user_id=?", (amount, user_id))
    _commit(conn); _close(conn)

def get_prestige(user_id):
    conn = get_conn()
    r = _one(conn, "SELECT prestige, title FROM users WHERE user_id=?", (user_id,))
    _close(conn)
    return (r["prestige"] if r else 0), (r["title"] if r else "")

def set_prestige(user_id, level, title=""):
    conn = get_conn()
    if not title:
        title = PRESTIGE_LEVELS.get(level, {}).get("title", "")
    if USE_PG:
        _exec(conn, "UPDATE users SET prestige=%s, title=%s WHERE user_id=%s", (level, title, user_id))
    else:
        _exec(conn, "UPDATE users SET prestige=?, title=? WHERE user_id=?", (level, title, user_id))
    _commit(conn); _close(conn)

def set_custom_title(user_id, title):
    conn = get_conn()
    if USE_PG:
        _exec(conn, "UPDATE users SET title=%s WHERE user_id=%s", (title, user_id))
    else:
        _exec(conn, "UPDATE users SET title=? WHERE user_id=?", (title, user_id))
    _commit(conn); _close(conn)

def get_prestige_bonus(user_id):
    """Возвращает множитель бонуса (0.0 = нет бонуса, 0.20 = +20%)"""
    conn = get_conn()
    r = _one(conn, "SELECT prestige FROM users WHERE user_id=?", (user_id,))
    _close(conn)
    lvl = r["prestige"] if r else 0
    return PRESTIGE_LEVELS.get(lvl, {}).get("bonus", 0.0)


def is_banned(user_id):
    val = get_setting(f"banned_{user_id}")
    return val == "1"

def get_usdt_rate():
    """Текущий курс USDT в монетах (базовый 1000, может колебаться)."""
    val = get_setting("usdt_rate")
    return int(val) if val else 1000

def set_usdt_rate(rate: int):
    set_setting("usdt_rate", str(rate))

def reset_user(user_id):
    """Обнуление игрока."""
    conn = get_conn()
    if USE_PG:
        _exec(conn, "UPDATE users SET coins=1000, usdt=0, prestige=0, title='', wins=0, losses=0, xp=0, level=1, total_bet=0 WHERE user_id=%s", (user_id,))
    else:
        _exec(conn, "UPDATE users SET coins=1000, usdt=0, prestige=0, title='', wins=0, losses=0, xp=0, level=1, total_bet=0 WHERE user_id=?", (user_id,))
    _commit(conn); _close(conn)

def reset_all_users():
    """Обнуление всех игроков."""
    conn = get_conn()
    _exec(conn, "UPDATE users SET coins=1000, usdt=0, prestige=0, title='', wins=0, losses=0, xp=0, level=1, total_bet=0")
    _commit(conn); _close(conn)

def get_game_cooldown(user_id, game: str) -> int:
    """Возвращает секунды до конца кулдауна (0 = можно играть)."""
    COOLDOWNS = {"slots": 30, "dice": 30, "roulette": 30,
                 "blackjack": 30, "crash": 30, "mines": 30,
                 "case": 30, "reaction": 30, "guess": 30,
                 "rps": 30, "math": 30}
    cd = COOLDOWNS.get(game, 30)
    last = get_setting(f"cd_{game}_{user_id}")
    if not last:
        return 0
    elapsed = int(time.time()) - int(last)
    return max(0, cd - elapsed)

def set_game_cooldown(user_id, game: str):
    set_setting(f"cd_{game}_{user_id}", str(int(time.time())))
