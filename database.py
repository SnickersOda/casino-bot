# ============================================================
#  database.py — вся работа с SQLite
# ============================================================
import sqlite3
import json
import time
from datetime import datetime, date
from config import DB_FILE, START_COINS, LEVELS, DAILY_TASKS, WIN_CHANCE


def get_conn():
    """Открыть соединение с базой данных."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ИНИЦИАЛИЗАЦИЯ ТАБЛИЦ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def init_db():
    conn = get_conn()
    c = conn.cursor()

    # Основная таблица пользователей
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            coins       INTEGER DEFAULT 0,
            stars       INTEGER DEFAULT 0,
            level       INTEGER DEFAULT 1,
            xp          INTEGER DEFAULT 0,
            wins        INTEGER DEFAULT 0,
            losses      INTEGER DEFAULT 0,
            total_bet   INTEGER DEFAULT 0,
            is_vip      INTEGER DEFAULT 0,
            vip_until   INTEGER DEFAULT 0,
            daily_last  TEXT    DEFAULT '',
            tasks_date  TEXT    DEFAULT '',
            tasks_json  TEXT    DEFAULT '{}',
            registered  INTEGER DEFAULT 0
        )
    """)

    # Настройки бота (шансы, сообщения и т.д.)
    c.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    # Лог транзакций
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            amount      INTEGER,
            type        TEXT,
            description TEXT,
            created_at  INTEGER
        )
    """)

    # Заполнить дефолтные настройки шансов
    for game, chance in WIN_CHANCE.items():
        c.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (f"win_chance_{game}", str(chance))
        )

    conn.commit()
    conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ПОЛЬЗОВАТЕЛИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_user(user_id: int):
    conn = get_conn()
    user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return user


def register_user(user_id: int, username: str, full_name: str):
    """Зарегистрировать нового пользователя."""
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO users
            (user_id, username, full_name, coins, registered)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, username or "", full_name or "Игрок", START_COINS, int(time.time())))
    conn.commit()
    conn.close()


def update_coins(user_id: int, amount: int):
    """Изменить баланс монет (amount может быть отрицательным)."""
    conn = get_conn()
    conn.execute("UPDATE users SET coins = MAX(0, coins + ?) WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()


def set_coins(user_id: int, amount: int):
    conn = get_conn()
    conn.execute("UPDATE users SET coins = ? WHERE user_id = ?", (max(0, amount), user_id))
    conn.commit()
    conn.close()


def add_xp(user_id: int, xp: int):
    """Добавить XP и повысить уровень при необходимости."""
    conn = get_conn()
    user = conn.execute("SELECT level, xp FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        return

    new_xp    = user["xp"] + xp
    new_level = user["level"]

    # Проверяем, нужно ли повысить уровень
    while new_level < max(LEVELS.keys()) and new_xp >= LEVELS[new_level]:
        new_xp   -= LEVELS[new_level]
        new_level += 1

    conn.execute(
        "UPDATE users SET xp = ?, level = ? WHERE user_id = ?",
        (new_xp, new_level, user_id)
    )
    conn.commit()
    conn.close()


def record_game(user_id: int, won: bool, bet: int):
    """Записать результат игры."""
    conn = get_conn()
    if won:
        conn.execute(
            "UPDATE users SET wins = wins + 1, total_bet = total_bet + ? WHERE user_id = ?",
            (bet, user_id)
        )
    else:
        conn.execute(
            "UPDATE users SET losses = losses + 1, total_bet = total_bet + ? WHERE user_id = ?",
            (bet, user_id)
        )
    conn.commit()
    conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ЕЖЕДНЕВНЫЙ БОНУС
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def claim_daily(user_id: int) -> dict:
    """
    Попытка получить ежедневный бонус.
    Возвращает {"ok": True, "amount": N} или {"ok": False, "seconds_left": N}
    """
    from config import DAILY_BONUS
    conn = get_conn()
    user = conn.execute("SELECT daily_last, is_vip FROM users WHERE user_id = ?", (user_id,)).fetchone()

    today = str(date.today())
    if user["daily_last"] == today:
        # Считаем сколько секунд до следующего дня
        now       = datetime.now()
        tomorrow  = datetime(now.year, now.month, now.day) if now.hour < 0 else \
                    datetime(now.year, now.month, now.day + 1) if now.day < 28 else datetime.now()
        # Простой расчёт: секунды до полуночи
        seconds_to_midnight = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
        conn.close()
        return {"ok": False, "seconds_left": seconds_to_midnight}

    bonus = DAILY_BONUS * 2 if user["is_vip"] else DAILY_BONUS
    conn.execute("UPDATE users SET daily_last = ?, coins = coins + ? WHERE user_id = ?",
                 (today, bonus, user_id))
    conn.commit()
    conn.close()
    return {"ok": True, "amount": bonus}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  VIP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def set_vip(user_id: int, days: int):
    now       = int(time.time())
    vip_until = now + days * 86400
    conn = get_conn()
    conn.execute(
        "UPDATE users SET is_vip = 1, vip_until = ? WHERE user_id = ?",
        (vip_until, user_id)
    )
    conn.commit()
    conn.close()


def check_vip_expired():
    """Снять VIP у истёкших пользователей (вызывается по таймеру)."""
    now  = int(time.time())
    conn = get_conn()
    conn.execute(
        "UPDATE users SET is_vip = 0 WHERE is_vip = 1 AND vip_until > 0 AND vip_until < ?",
        (now,)
    )
    conn.commit()
    conn.close()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ЗАДАНИЯ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_tasks(user_id: int) -> dict:
    """Вернуть прогресс заданий на сегодня."""
    conn = get_conn()
    user = conn.execute("SELECT tasks_date, tasks_json FROM users WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()

    today = str(date.today())
    if user["tasks_date"] != today:
        # Новый день — сброс заданий
        fresh = {t["id"]: {"progress": 0, "done": False} for t in DAILY_TASKS}
        _save_tasks(user_id, today, fresh)
        return fresh

    return json.loads(user["tasks_json"] or "{}")


def _save_tasks(user_id: int, day: str, tasks: dict):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET tasks_date = ?, tasks_json = ? WHERE user_id = ?",
        (day, json.dumps(tasks), user_id)
    )
    conn.commit()
    conn.close()


def update_task_progress(user_id: int, task_id: str, amount: int = 1) -> int:
    """
    Обновить прогресс задания.
    Возвращает награду, если задание только что выполнено, иначе 0.
    """
    tasks  = get_tasks(user_id)
    today  = str(date.today())
    task_meta = next((t for t in DAILY_TASKS if t["id"] == task_id), None)
    if not task_meta:
        return 0

    entry = tasks.get(task_id, {"progress": 0, "done": False})
    if entry["done"]:
        return 0

    entry["progress"] = entry.get("progress", 0) + amount

    reward = 0
    if entry["progress"] >= task_meta["target"]:
        entry["done"]     = True
        entry["progress"] = task_meta["target"]
        reward            = task_meta["reward"]
        update_coins(user_id, reward)

    tasks[task_id] = entry
    _save_tasks(user_id, today, tasks)
    return reward


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ТОП ИГРОКОВ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_top(limit: int = 10) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT user_id, full_name, coins, level, wins FROM users ORDER BY coins DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return rows


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  НАСТРОЙКИ (шансы и т.д.)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_setting(key: str, default=None):
    conn = get_conn()
    row  = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def get_win_chance(game: str) -> float:
    val = get_setting(f"win_chance_{game}")
    if val is None:
        return WIN_CHANCE.get(game, 0.4)
    return float(val)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  СТАТИСТИКА ДЛЯ АДМИНА
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def get_stats() -> dict:
    conn = get_conn()
    total_users  = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    total_coins  = conn.execute("SELECT SUM(coins) FROM users").fetchone()[0] or 0
    total_wins   = conn.execute("SELECT SUM(wins) FROM users").fetchone()[0] or 0
    total_losses = conn.execute("SELECT SUM(losses) FROM users").fetchone()[0] or 0
    vip_count    = conn.execute("SELECT COUNT(*) FROM users WHERE is_vip = 1").fetchone()[0]

    today = str(date.today())
    new_today = conn.execute(
        "SELECT COUNT(*) FROM users WHERE registered > ?",
        (int(time.time()) - 86400,)
    ).fetchone()[0]

    conn.close()
    return {
        "total_users":  total_users,
        "total_coins":  total_coins,
        "total_wins":   total_wins,
        "total_losses": total_losses,
        "vip_count":    vip_count,
        "new_today":    new_today,
    }


def get_all_user_ids() -> list:
    conn = get_conn()
    rows = conn.execute("SELECT user_id FROM users").fetchall()
    conn.close()
    return [r["user_id"] for r in rows]
