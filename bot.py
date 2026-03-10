# ============================================================
#  bot.py — основная логика казино-бота
#  Запуск: python bot.py
# ============================================================
import asyncio
from functools import wraps
import random
import time
import json
from datetime import datetime

from aiogram              import Bot, Dispatcher, F
from aiogram.types        import (Message, CallbackQuery,
                                  LabeledPrice, PreCheckoutQuery,
                                  InlineKeyboardMarkup, InlineKeyboardButton,
                                  InlineQuery, InlineQueryResultArticle,
                                  InputTextMessageContent)
from aiogram.filters      import Command, CommandStart
from aiogram.fsm.context  import FSMContext
from aiogram.fsm.state    import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp              import web

import config
import database as db

# ───────── инициализация ─────────
bot = Bot(token=config.BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

# Debug middleware — логирует все входящие обновления
from aiogram import BaseMiddleware
from typing import Callable, Awaitable, Any
from aiogram.types import TelegramObject

class DebugMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable, event: TelegramObject, data: dict) -> Any:
        if hasattr(event, "text"):
            print(f"📨 {event.from_user.id if hasattr(event,'from_user') else '?'}: {event.text!r}")
        return await handler(event, data)

dp.message.middleware(DebugMiddleware())



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  FSM-состояния
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class AdminStates(StatesGroup):
    wait_give_uid    = State()
    wait_give_amount = State()
    wait_take_uid    = State()
    wait_take_amount = State()
    wait_vip_uid     = State()
    wait_broadcast   = State()
    wait_chance_game = State()
    wait_chance_val  = State()


class BetStates(StatesGroup):
    wait_bet = State()  # ждём ставку, game сохранён в data

# Словари активных игровых сессий (нужны ГЛОБАЛЬНО до всех хендлеров)
reaction_sessions: dict = {}  # uid -> {bet, start_time, msg_id}
guess_sessions: dict    = {}  # uid -> {bet, number}
math_sessions: dict     = {}  # uid -> {bet, answer}
rps_sessions: dict      = {}  # uid -> {bet}

GAME_NAMES = {
    "slots": "🎰 Слоты", "dice": "🎲 Кости", "roulette": "🎡 Рулетка",
    "blackjack": "🃏 Блэкджек", "crash": "🚀 Краш", "mines": "💣 Мины",
    "reaction": "⚡ Реакция", "rps": "✂️ КНБ",
    "guess": "🧠 Угадай число", "math": "🔢 Математика",
}

def game_bet_kb(game: str) -> InlineKeyboardMarkup:
    """Клавиатура быстрых ставок для игры."""
    bets = [100, 500, 1000, 5000, 10000, 50000, 100000, 500000]
    rows = []
    row = []
    for b2 in bets:
        row.append(InlineKeyboardButton(text=f"{fmt_coins(b2)}", callback_data=f"bet_{game}_{b2}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Своя ставка", callback_data=f"custombet_{game}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS

def is_mod(user_id: int) -> bool:
    return user_id in config.MOD_IDS or user_id in config.ADMIN_IDS


def fmt_coins(n: int) -> str:
    """Форматирование числа с разделителями тысяч."""
    return f"{n:,}".replace(",", " ")


def display_name(user) -> str:
    """Имя игрока с prestige титулом — безопасная версия."""
    if not isinstance(user, dict):
        return str(user)
    name = user.get("full_name", "") or ""
    try:
        title = user.get("title", "") or ""
        if not title:
            lvl = int(user.get("prestige", 0) or 0)
            title = db.PRESTIGE_LEVELS.get(lvl, {}).get("title", "") or ""
    except Exception:
        title = ""
    return (title + " " + name).strip() if title else name


def apply_prestige(uid: int, payout: int) -> int:
    """Применяет prestige бонус к выигрышу."""
    bonus = db.get_prestige_bonus(uid)
    if bonus > 0:
        payout = int(payout * (1 + bonus))
    return payout

def game_cooldown(game_name: str):
    """Декоратор кулдауна для игр."""
    def decorator(func):
        @wraps(func)
        async def wrapper(message: Message, *args, **kwargs):
            uid = message.from_user.id
            cd  = db.get_game_cooldown(uid, game_name)
            if cd > 0:
                await message.answer(f"⏳ Подожди <b>{cd} сек.</b> перед следующей игрой!", parse_mode="HTML")
                return
            db.set_game_cooldown(uid, game_name)
            return await func(message, *args, **kwargs)
        return wrapper
    return decorator


def ensure_registered(func):
    """Декоратор: регистрирует пользователя и проверяет бан."""
    import functools
    @functools.wraps(func)
    async def wrapper(message: Message, **kwargs):
        u = message.from_user
        if db.is_banned(u.id):
            await message.answer("🚫 Ваш аккаунт заблокирован.")
            return
        db.register_user(u.id, u.username, u.full_name)
        return await func(message, **kwargs)
    return wrapper


def validate_bet(user, bet_str: str) -> tuple[int | None, str]:
    """
    Разбирает строку ставки, проверяет лимиты и баланс.
    Возвращает (bet_int, "") или (None, "сообщение об ошибке").
    """
    try:
        bet = int(bet_str)
    except (ValueError, TypeError):
        return None, "❌ Ставка должна быть числом."

    if bet < config.MIN_BET:
        return None, f"❌ Минимальная ставка: {fmt_coins(config.MIN_BET)} монет."
    if bet > config.MAX_BET:
        return None, f"❌ Максимальная ставка: {fmt_coins(config.MAX_BET)} монет."
    if user["coins"] < bet:
        return None, f"❌ Недостаточно монет. У тебя: {fmt_coins(user['coins'])} 🪙"
    return bet, ""


def level_progress_bar(xp: int, level: int) -> str:
    """Полоса прогресса XP."""
    needed  = config.LEVELS.get(level, 1)
    pct     = min(xp / needed, 1.0)
    filled  = int(pct * 10)
    bar     = "█" * filled + "░" * (10 - filled)
    return f"[{bar}] {xp}/{needed} XP"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  АНИМАЦИЯ СЛОТОВ  🎰
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REEL_SYMBOLS = config.SLOT_SYMBOLS          # все символы
SPIN_FRAMES  = 6                             # количество промежуточных кадров
SPIN_DELAY   = 0.55                          # секунд между кадрами


def _random_reel() -> list[str]:
    """Случайный столбец из 3 символов (видимое окно барабана)."""
    return random.choices(REEL_SYMBOLS, k=3)


def _build_slot_frame(reels: list[list[str]], locked: list[bool],
                      spinning_cols: list[int]) -> str:
    """
    Формирует кадр слотов без псевдографики — только эмодзи.
    Крутящиеся барабаны показываем через 🌀, остановившиеся — сам символ.
    Центральная (выигрышная) строка выделяется стрелками.
    """
    # Для крутящихся барабанов показываем анимационный символ
    SPIN_ANIM = ["🌀", "⚡", "💫"]   # меняется каждый кадр случайно

    def cell(col_i, row_i):
        sym = reels[col_i][row_i]
        if col_i in spinning_cols:
            return random.choice(SPIN_ANIM)
        return sym

    # Строим 3 строки × 3 колонки
    r0 = f"{cell(0,0)}  {cell(1,0)}  {cell(2,0)}"
    r1 = f"{cell(0,1)}  {cell(1,1)}  {cell(2,1)}"   # выигрышная
    r2 = f"{cell(0,2)}  {cell(1,2)}  {cell(2,2)}"

    # Статус барабанов снизу
    status = ""
    for i in range(3):
        if i not in spinning_cols:
            status += "🔒"
        else:
            status += "🔄"

    frame = (
        f"╔═══════════════╗\n"
        f"║  {r0}  ║\n"
        f"╠═══════════════╣\n"
        f"║▶ {r1} ◀║\n"
        f"╠═══════════════╣\n"
        f"║  {r2}  ║\n"
        f"╚═══════════════╝\n"
        f"  {status[0]}        {status[1]}        {status[2]}\n"
        f"  1️⃣      2️⃣      3️⃣"
    )
    return frame


async def animate_slots(message: Message, final_reels: list[list[str]]) -> Message:
    """
    Отправляет анимацию вращения слотов.
    final_reels — итоговые 3 барабана (список столбцов).
    Возвращает последнее сообщение.
    """
    # Генерируем промежуточные кадры: все барабаны крутятся
    spinning = [0, 1, 2]
    current_reels = [_random_reel(), _random_reel(), _random_reel()]

    header = "🎰 <b>КРУТИМ БАРАБАНЫ...</b>\n<i>Выигрышная линия — средняя строка</i>\n\n"
    msg    = await message.answer(header + _build_slot_frame(current_reels, [], spinning),
                                  parse_mode="HTML")

    # Фазы анимации: постепенно останавливаем барабаны
    phases = [
        # (кадров, какие барабаны крутятся, какой фиксируем в конце)
        (3, [0, 1, 2], None),    # все крутятся
        (2, [1, 2],    0),       # фиксируем 1-й
        (2, [2],       1),       # фиксируем 2-й
        (1, [],        2),       # фиксируем 3-й
    ]

    fixed   = [None, None, None]   # зафиксированные значения
    for frame_count, still_spinning, fix_idx in phases:
        for _ in range(frame_count):
            for col_i in still_spinning:
                current_reels[col_i] = _random_reel()

            # Подставляем уже зафиксированные барабаны
            display = [
                fixed[i] if fixed[i] is not None else current_reels[i]
                for i in range(3)
            ]

            status_line = ""
            if fix_idx is not None:
                icons = ["1️⃣", "2️⃣", "3️⃣"]
                status_line = f"\n🔒 Барабан {icons[fix_idx]} остановился!"

            try:
                await msg.edit_text(
                    header + _build_slot_frame(display, [], still_spinning) + status_line,
                    parse_mode="HTML"
                )
            except Exception:
                pass
            await asyncio.sleep(SPIN_DELAY)

        if fix_idx is not None:
            fixed[fix_idx] = final_reels[fix_idx]

    # Финальный кадр — все остановились
    result_line = "\n\n✨ <b>Барабаны остановились!</b>"
    try:
        await msg.edit_text(
            header + _build_slot_frame(final_reels, [], []) + result_line,
            parse_mode="HTML"
        )
    except Exception:
        pass

    await asyncio.sleep(0.4)
    return msg


def spin_slots(win_forced: bool) -> tuple[list[list[str]], str]:
    """
    Генерирует итоговые барабаны слотов.
    Возвращает (reels, combo_type):
      combo_type: 'jackpot' | 'triple' | 'double' | 'normal' | 'loss'
    """
    weights = config.SLOT_WEIGHTS
    symbols = config.SLOT_SYMBOLS

    def pick() -> str:
        return random.choices(symbols, weights=weights, k=1)[0]

    if win_forced:
        r = random.random()
        if r < 0.03:            # 3% — джекпот (три 🎰)
            sym    = symbols[-1]
            result = [[sym, pick(), pick()],
                      [sym, pick(), pick()],
                      [sym, pick(), pick()]]
            # центральная строка — всё одинаковое
            result[0][1] = sym
            result[1][1] = sym
            result[2][1] = sym
            return result, "jackpot"
        elif r < 0.15:          # тройное совпадение
            sym = random.choices(symbols[:-2], weights=weights[:-2], k=1)[0]
            result = [[pick(), sym, pick()],
                      [pick(), sym, pick()],
                      [pick(), sym, pick()]]
            return result, "triple"
        elif r < 0.50:          # двойное совпадение
            sym  = random.choices(symbols[:-1], weights=weights[:-1], k=1)[0]
            sym2 = pick()
            result = [[pick(), sym,  pick()],
                      [pick(), sym,  pick()],
                      [pick(), sym2, pick()]]
            return result, "double"
        else:                   # обычная победа (пара по строке)
            sym  = random.choices(symbols[:-1], weights=weights[:-1], k=1)[0]
            result = [[pick(), sym,  pick()],
                      [pick(), sym,  pick()],
                      [pick(), pick(), pick()]]
            return result, "normal"
    else:
        # Поражение: убеждаемся, что нет трёх одинаковых в центре
        while True:
            r0 = [pick(), pick(), pick()]
            r1 = [pick(), pick(), pick()]
            r2 = [pick(), pick(), pick()]
            # центральная строка: r0[1], r1[1], r2[1]
            if not (r0[1] == r1[1] == r2[1]):
                return [r0, r1, r2], "loss"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  КОМАНДЫ — ОСНОВНЫЕ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(CommandStart())
async def cmd_start(message: Message):
    u = message.from_user
    db.register_user(u.id, u.username, u.full_name)
    # Реферальная ссылка
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("ref"):
        try:
            ref_id = int(args[1][3:])
            if ref_id != u.id:
                refs = db.get_referrals(ref_id)
                already = any(r["referee_id"] == u.id for r in refs)
                if not already:
                    db.add_referral(ref_id, u.id)
                    db.set_vip(ref_id, 1)
                    try:
                        await bot.send_message(ref_id,
                            f"👫 <b>Новый реферал!</b>\n"
                            f"{u.full_name} присоединился по твоей ссылке.\n"
                            f"⭐ +1 день VIP тебе!", parse_mode="HTML")
                    except: pass
        except: pass
    u        = message.from_user
    user     = db.get_user(u.id)
    vip      = "⭐ VIP" if user["is_vip"] else ""
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    usdt_bal = db.get_usdt(u.id)
    pres_lvl, _ = db.get_prestige(u.id)
    pres_name = db.PRESTIGE_LEVELS.get(pres_lvl, {}).get("name", "")

    text = (
        f"🎰 <b>Casino Bot</b> {vip}\n"
        f"Привет, <b>{display_name(user)}</b>!\n"
        f"💰 {fmt_coins(user['coins'])} 🪙  |  💵 {usdt_bal} USDT"
        + (f"  |  👑 {pres_name}" if pres_name else "") + "\n"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="━━━ 🎰 КАЗИНО ━━━",       callback_data="noop")],
        [InlineKeyboardButton(text="🎰 Слоты",    callback_data="play_slots"),
         InlineKeyboardButton(text="🎲 Кости",    callback_data="play_dice"),
         InlineKeyboardButton(text="🎡 Рулетка",  callback_data="play_roulette")],
        [InlineKeyboardButton(text="🃏 Блэкджек", callback_data="play_blackjack"),
         InlineKeyboardButton(text="🚀 Краш",     callback_data="play_crash"),
         InlineKeyboardButton(text="💣 Мины",     callback_data="play_mines")],
        [InlineKeyboardButton(text="━━━ 🎮 СКИЛЛ ━━━",        callback_data="noop")],
        [InlineKeyboardButton(text="⚡ Реакция",  callback_data="play_reaction"),
         InlineKeyboardButton(text="✂️ КНБ",      callback_data="play_rps"),
         InlineKeyboardButton(text="🧠 Угадай",   callback_data="play_guess")],
        [InlineKeyboardButton(text="🔢 Математика", callback_data="play_math")],
        [InlineKeyboardButton(text="━━━ 💰 ЭКОНОМИКА ━━━",    callback_data="noop")],
        [InlineKeyboardButton(text="🎁 Кейсы",    callback_data="menu_cases"),
         InlineKeyboardButton(text="💱 USDT",     callback_data="menu_exchange"),
         InlineKeyboardButton(text="👑 Prestige", callback_data="menu_prestige")],
        [InlineKeyboardButton(text="👤 Профиль",  callback_data="menu_profile"),
         InlineKeyboardButton(text="🏆 Турнир",   callback_data="menu_tournament"),
         InlineKeyboardButton(text="🎁 Бонус",    callback_data="menu_daily")],
        [InlineKeyboardButton(text="⭐ Магазин",  callback_data="open_shop"),
         InlineKeyboardButton(text="➕ Добавить в группу",
            url=f"https://t.me/{bot_username}?startgroup=true&admin=change_info+delete_messages+restrict_members+invite_users+pin_messages+manage_video_chats+manage_chat")],
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data == "quick_play")
async def cb_quick_play(callback: CallbackQuery):
    await callback.answer()

# Клавиатура быстрой ставки
def bet_keyboard(game: str) -> InlineKeyboardMarkup:
    bets = [100, 1000, 5000, 10000, 50000, 100000, 500000]
    rows = []
    row  = []
    for bv in bets:
        row.append(InlineKeyboardButton(text=fmt_coins(bv), callback_data=f"quickbet_{game}_{bv}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="✏️ Своя ставка", callback_data=f"custombet_{game}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# Для рулетки — отдельная клавиатура с выбором цвета
def roulette_color_kb(bet: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔴 Красное", callback_data=f"roulette_red_{bet}"),
        InlineKeyboardButton(text="⚫ Чёрное",  callback_data=f"roulette_black_{bet}"),
    ]])

# Для кейсов — отдельное подменю
def cases_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🥉 Бронза  — 1 USDT",   callback_data="case_bronze")],
        [InlineKeyboardButton(text="🥈 Серебро — 5 USDT",   callback_data="case_silver")],
        [InlineKeyboardButton(text="🥇 Золото  — 20 USDT",  callback_data="case_gold")],
        [InlineKeyboardButton(text="💎 Алмаз  — 100 USDT",  callback_data="case_diamond")],
    ])

# Для prestige — кнопки покупки
def prestige_keyboard(current_lvl: int) -> InlineKeyboardMarkup:
    levels = db.PRESTIGE_LEVELS
    rows = []
    for i in range(1, 6):
        p = levels[i]
        if i <= current_lvl:
            rows.append([InlineKeyboardButton(text=f"✅ {p['name']} — куплен", callback_data="prestige_owned")])
        else:
            cost = p["price_usdt"] - levels[current_lvl]["price_usdt"]
            rows.append([InlineKeyboardButton(text=f"🔒 {p['name']} — {cost} USDT → Купить", callback_data=f"prestige_buy_{i}")])
    rows.append([InlineKeyboardButton(text="✏️ Изменить приписку", callback_data="prestige_title")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("play_") | F.data.startswith("menu_"))
async def cb_start_buttons(callback: CallbackQuery, state: FSMContext):
    action = callback.data
    game_map = {
        "play_slots": "slots", "play_dice": "dice", "play_roulette": "roulette",
        "play_blackjack": "blackjack", "play_crash": "crash", "play_mines": "mines",
        "play_reaction": "reaction", "play_rps": "rps", "play_guess": "guess",
        "play_math": "math",
    }
    if action in game_map:
        game = game_map[action]
        name = GAME_NAMES[game]
        user = db.get_user(callback.from_user.id)
        await callback.message.answer(
            f"{name}\n\n💰 Баланс: <b>{fmt_coins(user['coins'])} 🪙</b>\n\nВыбери ставку:",
            parse_mode="HTML",
            reply_markup=bet_keyboard(game)
        )
    elif action == "menu_cases":
        usdt = db.get_usdt(callback.from_user.id)
        await callback.message.answer(
            f"🎁 <b>Кейсы</b> (покупка за USDT)\n\n💵 Твой баланс: <b>{usdt} USDT</b>\n\n💱 Нет USDT? /exchange",
            parse_mode="HTML", reply_markup=cases_keyboard()
        )
    elif action == "menu_exchange":
        await callback.message.answer(f"💵 Курс USDT: <b>{db.get_usdt_rate()} 🪙 = 1 USDT</b>\n\n/exchange 5 — обменять 5 USDT\n/usdt — текущий курс", parse_mode="HTML")
    elif action == "menu_prestige":
        uid2 = callback.from_user.id
        lvl2, title2 = db.get_prestige(uid2)
        usdt2 = db.get_usdt(uid2)
        levels = db.PRESTIGE_LEVELS
        bonus2 = int(db.get_prestige_bonus(uid2) * 100)
        pname2 = levels[lvl2]["name"] or "Нет"
        lines2 = [f"👑 <b>Prestige</b>\n\n💵 USDT: <b>{usdt2}</b>\nТекущий: <b>{pname2}</b>"]
        if bonus2: lines2.append(f"+{bonus2}% к выигрышам")
        if title2: lines2.append(f"🏷 {title2}")
        lines2.append("\n<b>Доступные статусы:</b>")
        for i in range(1, 6):
            p = levels[i]
            mark = "✅" if i == lvl2 else ("✓" if i < lvl2 else "🔒")
            cost = p["price_usdt"] - levels[lvl2]["price_usdt"]
            cost_str = f"({cost} USDT)" if i > lvl2 else ""
            lines2.append(f"{mark} {p['name']} {cost_str} | +{int(p['bonus']*100)}% | {p['title']}")
        await callback.message.answer("\n".join(lines2), parse_mode="HTML",
                                      reply_markup=prestige_keyboard(lvl2))
    elif action == "menu_profile":
        await _send_profile(callback.from_user.id, callback.message.answer)
    elif action == "menu_tournament":
        await cmd_tournament(callback.message)
    elif action == "menu_daily":
        import types as _types
        fake_d = _types.SimpleNamespace(
            from_user=callback.from_user,
            answer=callback.message.answer,
            chat=callback.message.chat,
            message_id=callback.message.message_id,
            bot=callback.message.bot,
        )
        await cmd_daily(fake_d)
    await callback.answer()


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "menu_top")
async def cb_menu_top(callback: CallbackQuery):
    # Вызываем top как будто это сообщение
    import types
    fake = types.SimpleNamespace(
        from_user=callback.from_user,
        text="/top",
        answer=callback.message.answer,
        chat=callback.message.chat,
        message_id=callback.message.message_id,
        bot=callback.message.bot,
    )
    await cmd_top(fake)
    await callback.answer()


@dp.callback_query(F.data.startswith("quickbet_"))
async def cb_quickbet(callback: CallbackQuery):
    """Быстрая ставка — сразу запускаем игру."""
    parts = callback.data.split("_")
    game = parts[1]
    bet  = int(parts[2])
    uid  = callback.from_user.id
    user = db.get_user(uid)

    # Проверяем кулдаун
    cd = db.get_game_cooldown(uid, game)
    if cd > 0:
        await callback.answer(f"⏳ Подожди {cd} сек!", show_alert=True); return

    if user["coins"] < bet:
        await callback.answer(f"❌ Недостаточно монет! У тебя {fmt_coins(user['coins'])} 🪙", show_alert=True); return

    await callback.answer()
    # Создаём фейковое сообщение с командой и вызываем хендлер
    fake_text = f"/{game} {bet}"
    if game == "roulette":
        # Для рулетки нужен цвет — показываем выбор
        await callback.message.answer(
            f"🎡 Рулетка — ставка {fmt_coins(bet)} 🪙\nВыбери цвет:",
            parse_mode="HTML",
            reply_markup=roulette_color_kb(bet)
        )
        return
    # Для остальных — инжектируем через event
    import types
    fake = types.SimpleNamespace(
        from_user=callback.from_user,
        text=fake_text,
        answer=callback.message.answer,
        chat=callback.message.chat,
        message_id=callback.message.message_id,
        bot=callback.message.bot,
    )
    # Словарь обработчиков
    handlers = {
        "slots": cmd_slots, "dice": cmd_dice,
        "blackjack": cmd_blackjack, "crash": cmd_crash,
        "mines": cmd_mines, "reaction": cmd_reaction,
        "rps": cmd_rps, "guess": cmd_guess, "math": cmd_mathgame,
    }
    if game in handlers:
        await handlers[game](fake)
    elif game == "roulette":
        await callback.message.answer(
            f"🎡 Рулетка — выбери цвет:",
            reply_markup=roulette_color_kb(bet)
        )


@dp.callback_query(F.data.startswith("custombet_"))
async def cb_custombet(callback: CallbackQuery, state: FSMContext):
    """Своя ставка — просим написать число."""
    game = callback.data.split("_")[1]
    await state.set_state(BetStates.wait_bet)
    await state.update_data(game=game)
    user = db.get_user(callback.from_user.id)
    await callback.message.answer(
        f"{GAME_NAMES.get(game, game)} — введи ставку:\n"
        f"💰 Баланс: {fmt_coins(user['coins'])} 🪙\n"
        f"(от {config.MIN_BET} до {fmt_coins(config.MAX_BET)})"
    )
    await callback.answer()


@dp.message(BetStates.wait_bet)
async def handle_custom_bet(message: Message, state: FSMContext):
    """Получаем свою ставку и запускаем игру."""
    data = await state.get_data()
    game = data.get("game")
    await state.clear()

    import types
    user = db.get_user(message.from_user.id)
    bet, err = validate_bet(user, message.text.strip())
    if bet is None:
        await message.answer(err); return

    if game == "roulette":
        await message.answer(
            f"🎡 Рулетка — ставка {fmt_coins(bet)} 🪙\nВыбери цвет:",
            parse_mode="HTML",
            reply_markup=roulette_color_kb(bet)
        )
        return

    fake = types.SimpleNamespace(
        from_user=message.from_user,
        text=f"/{game} {bet}",
        answer=message.answer,
        chat=message.chat,
        message_id=message.message_id,
        bot=message.bot,
    )
    handlers = {
        "slots": cmd_slots, "dice": cmd_dice,
        "blackjack": cmd_blackjack, "crash": cmd_crash,
        "mines": cmd_mines, "reaction": cmd_reaction,
        "rps": cmd_rps, "guess": cmd_guess, "math": cmd_mathgame,
    }
    if game == "prestige_title":
        custom = message.text.strip()[:20]
        uid2 = message.from_user.id
        lvl2, _ = db.get_prestige(uid2)
        if lvl2 == 0:
            await message.answer("❌ Сначала купи статус Prestige (/prestige)")
        else:
            db.set_custom_title(uid2, custom)
            await message.answer(f"✅ Приписка изменена: <b>{custom}</b>", parse_mode="HTML")
        return

    if game in handlers:
        await handlers[game](fake)


@dp.callback_query(F.data.startswith("roulette_"))
async def cb_roulette_color(callback: CallbackQuery):
    """Обрабатываем выбор цвета рулетки из кнопки."""
    parts = callback.data.split("_")
    color = parts[1]  # red или black
    bet   = int(parts[2])
    uid   = callback.from_user.id

    cd = db.get_game_cooldown(uid, "roulette")
    if cd > 0:
        await callback.answer(f"⏳ Подожди {cd} сек!", show_alert=True); return

    user = db.get_user(uid)
    if user["coins"] < bet:
        await callback.answer("❌ Недостаточно монет!", show_alert=True); return

    await callback.answer()
    import types
    fake = types.SimpleNamespace(
        from_user=callback.from_user,
        text=f"/roulette {color} {bet}",
        answer=callback.message.answer,
        chat=callback.message.chat,
        message_id=callback.message.message_id,
        bot=callback.message.bot,
    )
    await cmd_roulette(fake)


@dp.callback_query(F.data.startswith("case_"))
async def cb_case_btn(callback: CallbackQuery):
    """Открыть кейс по кнопке."""
    case_key = callback.data.split("_")[1]  # bronze/silver/gold/diamond
    uid = callback.from_user.id

    cd = db.get_game_cooldown(uid, "case")
    if cd > 0:
        await callback.answer(f"⏳ Подожди {cd} сек!", show_alert=True); return

    await callback.answer()
    import types
    fake = types.SimpleNamespace(
        from_user=callback.from_user,
        text=f"/case {case_key}",
        answer=callback.message.answer,
        chat=callback.message.chat,
        message_id=callback.message.message_id,
        bot=callback.message.bot,
    )
    await cmd_case(fake)


@dp.callback_query(F.data.startswith("prestige_buy_"))
async def cb_prestige_buy(callback: CallbackQuery):
    """Покупка prestige через кнопку."""
    uid = callback.from_user.id
    raw = callback.data  # "prestige_buy_1"
    # Надёжный парсинг: берём последний сегмент
    try:
        lvl_target = int(raw.rsplit("_", 1)[-1])
    except (ValueError, IndexError):
        await callback.answer("❌ Ошибка данных", show_alert=True); return

    usdt   = db.get_usdt(uid)
    lvl, _ = db.get_prestige(uid)
    levels = db.PRESTIGE_LEVELS

    if not (1 <= lvl_target <= 5):
        await callback.answer("❌ Неверный уровень", show_alert=True); return
    if lvl_target <= lvl:
        await callback.answer("✅ Уже куплен!", show_alert=True); return

    p          = levels[lvl_target]
    prev_price = levels[lvl]["price_usdt"]
    cost       = p["price_usdt"] - prev_price

    if usdt < cost:
        await callback.answer(f"❌ Нужно {cost} USDT, у тебя {usdt}", show_alert=True); return

    db.update_usdt(uid, -cost)
    db.set_prestige(uid, lvl_target)
    await callback.answer("✅ Куплено!")
    await callback.message.answer(
        f"🎉 <b>Prestige получен!</b>\n\n"
        f"👑 {p['name']} | +{int(p['bonus']*100)}% к выигрышам\n"
        f"🏷 Дефолт приписка: <b>{p['title']}</b>\n\n"
        f"✏️ Изменить: /prestige title МойТекст",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "prestige_owned")
async def cb_prestige_owned(callback: CallbackQuery):
    await callback.answer("✅ Этот уровень уже куплен!", show_alert=False)


@dp.callback_query(F.data == "prestige_title")
async def cb_prestige_title(callback: CallbackQuery, state: FSMContext):
    await state.set_state(BetStates.wait_bet)
    await state.update_data(game="prestige_title")
    await callback.message.answer("✏️ Введи новую приписку (до 20 символов):")
    await callback.answer()








@dp.callback_query(F.data.startswith("bet_"))
async def cb_bet_quick(callback: CallbackQuery):
    """Быстрая ставка — сразу запускает игру."""
    parts = callback.data.split("_")
    game = parts[1]
    bet  = int(parts[2])
    uid  = callback.from_user.id

    import types
    fake = types.SimpleNamespace(
        from_user=callback.from_user,
        text=f"/{game} {bet}",
        answer=callback.message.answer,
        chat=callback.message.chat,
        message_id=callback.message.message_id,
        bot=callback.message.bot,
    )
    handlers = {
        "slots": cmd_slots, "dice": cmd_dice, "blackjack": cmd_blackjack,
        "crash": cmd_crash, "mines": cmd_mines,
        "reaction": cmd_reaction, "rps": cmd_rps,
        "guess": cmd_guess, "math": cmd_mathgame,
    }
    if game == "roulette":
        await callback.message.answer(
            f"🎡 Рулетка — ставка {fmt_coins(bet)} 🪙\nВыбери цвет:",
            parse_mode="HTML",
            reply_markup=roulette_color_kb(bet)
        )
    elif game in handlers:
        await handlers[game](fake)
    await callback.answer()


@dp.callback_query(F.data == "open_shop")
async def cb_open_shop(callback: CallbackQuery):
    text = "⭐ <b>Магазин Telegram Stars</b>\n\nПоддержи казино и получи бонусы!\n\n"
    for item in config.SHOP_ITEMS.values():
        text += f"  • {item['title']} — ⭐ {item['stars']} Stars\n"
        text += f"    <i>{item['desc']}</i>\n\n"
    await callback.message.answer(text, reply_markup=shop_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.message(Command("help", ignore_mention=True))
@ensure_registered
async def cmd_help(message: Message):
    text = (
        "📖 <b>Справка по командам</b>\n\n"
        "<b>Игры:</b>\n"
        "  /slots &lt;ставка&gt; — Барабаны с анимацией\n"
        "  /dice &lt;ставка&gt; — Кинь кости (1–6)\n"
        "  /roulette &lt;red|black&gt; &lt;ставка&gt;\n"
        "  /blackjack &lt;ставка&gt; — Карты 21\n"
        "  /crash &lt;ставка&gt; — Ракета-краш\n\n"
        "<b>Профиль:</b>\n"
        "  /profile — Статистика и уровень\n"
        "  /balance — Текущий баланс\n"
        "  /daily   — Бонус раз в сутки\n"
        "  /tasks   — Ежедневные задания\n"
        "  /top     — Топ-10 по монетам\n\n"
        "<b>Магазин:</b>\n"
        "  /donate  — Купить монеты, VIP, кейсы за ⭐ Stars\n\n"
        f"<i>Мин. ставка: {fmt_coins(config.MIN_BET)} | Макс: {fmt_coins(config.MAX_BET)}</i>"
    )
    await message.answer(text, parse_mode="HTML")


async def _send_profile(uid: int, answer_fn):
    """Отправляет профиль игрока напрямую по uid."""
    user = db.get_user(uid)
    if not user:
        await answer_fn("❌ Профиль не найден."); return
    vip   = "⭐ VIP" if user["is_vip"] else "Обычный"
    lname = config.LEVEL_NAMES.get(user["level"], "???")
    total = user["wins"] + user["losses"]
    wr    = f"{user['wins']/total*100:.1f}%" if total else "—"
    bar   = level_progress_bar(user["xp"], user["level"])
    pres_lvl, pres_title = db.get_prestige(uid)
    pres_name  = db.PRESTIGE_LEVELS.get(pres_lvl, {}).get("name", "")
    pres_bonus = int(db.get_prestige_bonus(uid) * 100)
    usdt_bal   = db.get_usdt(uid)
    dname      = display_name(user)
    text = (
        f"👤 <b>Профиль: {dname}</b>\n"
        f"{'─'*28}\n"
        f"🏅 VIP: {vip}\n"
        f"👑 Prestige: {pres_name or 'Нет'}"
        + (f" (+{pres_bonus}% к выигрышам)" if pres_bonus else "") + "\n"
        + (f"🏷 Приписка: <i>{pres_title}</i>\n" if pres_title else "")
        + f"🎖 Уровень: {user['level']} {lname}\n"
        f"📊 Прогресс: {bar}\n"
        f"{'─'*28}\n"
        f"🪙 Монеты: <b>{fmt_coins(user['coins'])}</b>\n"
        f"💵 USDT: <b>{usdt_bal} USDT</b>\n"
        f"🏆 Побед: {user['wins']}\n"
        f"💀 Поражений: {user['losses']}\n"
        f"📈 Winrate: {wr}\n"
        f"💸 Поставлено: {fmt_coins(user['total_bet'])}\n"
    )
    await answer_fn(text, parse_mode="HTML")


@dp.message(Command("profile", ignore_mention=True))
@ensure_registered
async def cmd_profile(message: Message):
    await _send_profile(message.from_user.id, message.answer)


@dp.message(Command("balance", ignore_mention=True))
@ensure_registered
async def cmd_balance(message: Message):
    user = db.get_user(message.from_user.id)
    await message.answer(
        f"💰 Твой баланс: <b>{fmt_coins(user['coins'])} 🪙</b>",
        parse_mode="HTML"
    )


@dp.message(Command("daily", ignore_mention=True))
@ensure_registered
async def cmd_daily(message: Message):
    uid    = message.from_user.id
    result = db.claim_daily(uid)
    if result["ok"]:
        db.update_task_progress(uid, "play5")
        # Стрик
        streak       = db.update_streak(uid)
        streak_bonus = _streak_bonus(streak)
        if streak_bonus > 0:
            db.update_coins(uid, streak_bonus)
        user = db.get_user(uid)
        streak_line = f"\n🔥 Стрик: <b>{streak} дней</b>! +{fmt_coins(streak_bonus)} 🪙" if streak_bonus > 0 else f"\n🔥 Стрик: <b>{streak} дней</b>"
        await message.answer(
            f"🎁 <b>Ежедневный бонус получен!</b>\n"
            f"+{fmt_coins(result['amount'])} 🪙"
            f"{streak_line}\n\n"
            f"💼 Баланс: {fmt_coins(user['coins'])} 🪙",
            parse_mode="HTML"
        )
    else:
        h = result["seconds_left"] // 3600
        m = (result["seconds_left"] % 3600) // 60
        streak = db.get_streak(uid)
        await message.answer(
            f"⏳ <b>Бонус уже получен сегодня.</b>\n"
            f"Следующий через: <b>{h}ч {m}мин</b>\n\n"
            f"🔥 Текущий стрик: <b>{streak} дней</b>",
            parse_mode="HTML"
        )


@dp.message(Command("tasks", ignore_mention=True))
@ensure_registered
async def cmd_tasks(message: Message):
    tasks = db.get_tasks(message.from_user.id)
    lines = ["📋 <b>Ежедневные задания</b>\n"]
    for t in config.DAILY_TASKS:
        entry    = tasks.get(t["id"], {"progress": 0, "done": False})
        progress = entry.get("progress", 0)
        done     = entry.get("done", False)
        status   = "✅" if done else "🔲"
        bar_len  = 8
        filled   = int(min(progress / t["target"], 1.0) * bar_len)
        bar      = "▓" * filled + "░" * (bar_len - filled)
        lines.append(
            f"{status} {t['desc']}\n"
            f"   [{bar}] {min(progress, t['target'])}/{t['target']}  🎁 +{fmt_coins(t['reward'])} 🪙\n"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("top", ignore_mention=True))
@ensure_registered
async def cmd_top(message: Message):
    rows  = db.get_top(10)
    lines = ["🏆 <b>Топ-10 игроков</b>\n"]
    medals = ["🥇","🥈","🥉"] + ["🔸"] * 7
    for i, r in enumerate(rows):
        try:
            full  = db.get_user(r["user_id"]) or r
            vip   = "⭐" if full.get("is_vip") else ""
            dname = display_name(full) if isinstance(full, dict) and full.get("full_name") else r.get("full_name","?")
            rc = fmt_coins(r["coins"]); rl = r["level"]; rw = r["wins"]
            lines.append(f"{medals[i]} <b>{dname}</b> {vip}\n   💰 {rc} | Ур.{rl} | 🏆{rw}\n")
        except Exception:
            lines.append(f"{medals[i]} {r.get('full_name','?')} — {fmt_coins(r['coins'])} 🪙\n")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  СЛОТЫ  🎰  (с анимацией прокрутки!)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(Command("slots", ignore_mention=True))
@ensure_registered
@game_cooldown("slots")
async def cmd_slots(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("🎰 Использование: /slots <ставка>\nПример: /slots 100")
        return

    user = db.get_user(message.from_user.id)
    bet, err = validate_bet(user, args[1])
    if err:
        await message.answer(err)
        return

    db.update_coins(message.from_user.id, -bet)

    win_chance = db.get_win_chance("slots")
    is_vip     = bool(user["is_vip"])
    if is_vip:
        win_chance = min(win_chance * 1.15, 0.80)

    won        = random.random() < win_chance
    reels, combo = spin_slots(won)

    # Запускаем анимацию
    slot_msg = await animate_slots(message, reels)

    # Вычисляем выигрыш по центральной строке
    center = [reels[0][1], reels[1][1], reels[2][1]]

    mult_map = {
        "jackpot": config.MULTIPLIERS["slots_jackpot"],
        "triple":  config.MULTIPLIERS["slots_triple"],
        "double":  config.MULTIPLIERS["slots_double"],
        "normal":  config.MULTIPLIERS["slots_normal"],
        "loss":    0,
    }
    mult    = mult_map.get(combo, 0)
    payout  = int(bet * mult)
    profit  = payout - bet

    if combo != "loss":
        db.update_coins(message.from_user.id, payout)
        db.add_xp(message.from_user.id, bet // 10 + 20)
        db.record_game(message.from_user.id, True, bet)
        db.update_task_progress(message.from_user.id, "play5")
        db.update_task_progress(message.from_user.id, "win3")
        db.update_task_progress(message.from_user.id, "bet1000", bet)
        db.update_task_progress(message.from_user.id, "slots3")
        if combo == "jackpot":
            db.update_task_progress(message.from_user.id, "jackpot")

        combo_labels = {
            "jackpot": "💎💎💎 ДЖЕКПОТ!!!",
            "triple":  "🎊 ТРОЙНОЕ СОВПАДЕНИЕ!",
            "double":  "✨ Двойное совпадение!",
            "normal":  "🎉 Выигрыш!",
        }
        result_text = (
            f"\n{'═'*24}\n"
            f"🎰 {combo_labels[combo]}\n"
            f"✅ Линия: {center[0]} {center[1]} {center[2]}\n"
            f"💸 Ставка: {fmt_coins(bet)} 🪙\n"
            f"💰 Выплата: {fmt_coins(payout)} 🪙  (x{mult})\n"
            f"📈 Профит: +{fmt_coins(profit)} 🪙\n"
        )
    else:
        db.add_xp(message.from_user.id, 5)
        db.record_game(message.from_user.id, False, bet)
        db.update_task_progress(message.from_user.id, "play5")
        db.update_task_progress(message.from_user.id, "bet1000", bet)
        db.update_task_progress(message.from_user.id, "slots3")
        result_text = (
            f"\n{'═'*24}\n"
            f"❌ Не повезло!\n"
            f"Линия: {center[0]} {center[1]} {center[2]}\n"
            f"💸 Проигрыш: -{fmt_coins(bet)} 🪙\n"
        )

    user_after = db.get_user(message.from_user.id)
    result_text += f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙"

    try:
        await slot_msg.edit_text(
            slot_msg.text + result_text,
            parse_mode="HTML"
        )
    except Exception:
        await message.answer(result_text, parse_mode="HTML")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  КОСТИ  🎲
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(Command("dice", ignore_mention=True))
@ensure_registered
@game_cooldown("dice")
async def cmd_dice(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("🎲 Использование: /dice <ставка>\nПобеда если твои кости > кости казино")
        return

    user = db.get_user(message.from_user.id)
    bet, err = validate_bet(user, args[1])
    if err:
        await message.answer(err)
        return

    db.update_coins(message.from_user.id, -bet)

    player_roll = random.randint(1, 6)
    casino_roll = random.randint(1, 6)
    won         = player_roll > casino_roll

    if won:
        payout = int(bet * config.MULTIPLIERS["dice"])
        db.update_coins(message.from_user.id, payout)
        db.record_game(message.from_user.id, True, bet)
        db.add_xp(message.from_user.id, bet // 10 + 15)
        db.update_task_progress(message.from_user.id, "win3")
        result = f"🎉 <b>Победа!</b> +{fmt_coins(payout - bet)} 🪙"
    else:
        db.record_game(message.from_user.id, False, bet)
        db.add_xp(message.from_user.id, 5)
        tie = " (ничья)" if player_roll == casino_roll else ""
        result = f"❌ <b>Поражение{tie}.</b> -{fmt_coins(bet)} 🪙"

    db.update_task_progress(message.from_user.id, "play5")
    db.update_task_progress(message.from_user.id, "bet1000", bet)
    user_after = db.get_user(message.from_user.id)

    dice_faces = ["", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
    await message.answer(
        f"🎲 <b>Кости</b>\n\n"
        f"Ты:    {dice_faces[player_roll]} ({player_roll})\n"
        f"Казино: {dice_faces[casino_roll]} ({casino_roll})\n\n"
        f"{result}\n"
        f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  РУЛЕТКА  🔴⚫
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(Command("roulette", ignore_mention=True))
@ensure_registered
@game_cooldown("roulette")
async def cmd_roulette(message: Message):
    args = message.text.split()
    if len(args) < 3:
        await message.answer("🎡 Использование: /roulette <red|black> <ставка>")
        return

    choice = args[1].lower()
    if choice not in ("red", "black", "красное", "чёрное", "red", "black"):
        await message.answer("❌ Выбери: red или black")
        return

    user = db.get_user(message.from_user.id)
    bet, err = validate_bet(user, args[2])
    if err:
        await message.answer(err)
        return

    db.update_coins(message.from_user.id, -bet)

    number  = random.randint(0, 36)
    # Красные числа в европейской рулетке
    red_numbers = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    if number == 0:
        color = "green"
    elif number in red_numbers:
        color = "red"
    else:
        color = "black"

    color_emoji = {"red": "🔴", "black": "⚫", "green": "🟢"}

    normalized = "red" if choice in ("red","красное") else "black"
    won        = (normalized == color)

    if won:
        payout = int(bet * config.MULTIPLIERS["roulette"])
        db.update_coins(message.from_user.id, payout)
        db.record_game(message.from_user.id, True, bet)
        db.add_xp(message.from_user.id, bet // 10 + 15)
        db.update_task_progress(message.from_user.id, "win3")
        result = f"🎉 <b>Победа!</b> +{fmt_coins(payout - bet)} 🪙"
    else:
        db.record_game(message.from_user.id, False, bet)
        db.add_xp(message.from_user.id, 5)
        result = f"❌ <b>Поражение.</b> -{fmt_coins(bet)} 🪙"

    db.update_task_progress(message.from_user.id, "play5")
    db.update_task_progress(message.from_user.id, "bet1000", bet)
    user_after = db.get_user(message.from_user.id)

    await message.answer(
        f"🎡 <b>Рулетка</b>\n\n"
        f"Выпало: <b>{color_emoji[color]} {number}</b>\n"
        f"Твоя ставка: {'🔴 Красное' if normalized=='red' else '⚫ Чёрное'}\n\n"
        f"{result}\n"
        f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ОДИНОЧНЫЙ БЛЭКДЖЕК  🃏  (против казино)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DECK_VALUES = [
    ("2",2),("3",3),("4",4),("5",5),("6",6),("7",7),
    ("8",8),("9",9),("10",10),("J",10),("Q",10),("K",10),("A",11)
]
SUITS = ["♠","♥","♦","♣"]

def _bj_card():
    v = random.choice(DECK_VALUES)
    s = random.choice(SUITS)
    return (f"{v[0]}{s}", v[1])

def _bj_val(cards):
    total = sum(v for _,v in cards)
    aces  = sum(1 for n,_ in cards if "A" in n)
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total

def _bj_hand_str(cards):
    return "  ".join(n for n,_ in cards)

@dp.message(Command("blackjack", ignore_mention=True))
@ensure_registered
@game_cooldown("blackjack")
async def cmd_blackjack(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "🃏 <b>Блэкджек против казино</b>\n\n"
            "/blackjack &lt;ставка&gt; — одиночная игра\n"
            "/bjroom — создать комнату для игры с другими\n\n"
            "В одиночной игре можно добирать карты кнопками.",
            parse_mode="HTML"
        )
        return
    user = db.get_user(message.from_user.id)
    bet, err = validate_bet(user, args[1])
    if err:
        await message.answer(err); return

    db.update_coins(message.from_user.id, -bet)

    p = [_bj_card(), _bj_card()]
    d = [_bj_card(), _bj_card()]

    # Сохраняем состояние одиночной игры
    bj_solo_sessions[message.from_user.id] = {
        "bet": bet, "player": p, "dealer": d, "done": False
    }

    await _bj_solo_show(message.from_user.id, message.chat.id)


# Хранилище одиночных BJ сессий
bj_solo_sessions: dict[int, dict] = {}


async def _bj_solo_show(uid: int, chat_id: int, edit_msg=None):
    sess = bj_solo_sessions.get(uid)
    if not sess: return
    p_val = _bj_val(sess["player"])
    d_val = _bj_val(sess["dealer"])
    p_str = _bj_hand_str(sess["player"])
    # Дилер показывает только первую карту
    d_str = f"{sess['dealer'][0][0]}  🂠"

    status = ""
    if p_val == 21 and len(sess["player"]) == 2:
        status = "\n🎉 <b>БЛЭКДЖЕК!</b>"

    text = (
        f"🃏 <b>Блэкджек</b> — ставка {fmt_coins(sess['bet'])} 🪙\n"
        f"{'─'*28}\n"
        f"🏦 Дилер:  {d_str}  (? очков)\n"
        f"👤 Ты:     {p_str}  = <b>{p_val}</b>{status}\n"
        f"{'─'*28}\n"
    )

    if p_val == 21 and len(sess["player"]) == 2:
        # Автоматический блэкджек
        await _bj_solo_finish(uid, chat_id, edit_msg, auto_bj=True)
        return
    if p_val > 21:
        await _bj_solo_finish(uid, chat_id, edit_msg)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Ещё карту", callback_data=f"bj_hit_{uid}"),
        InlineKeyboardButton(text="✋ Хватит",    callback_data=f"bj_stand_{uid}"),
    ]])

    if edit_msg:
        try:
            await edit_msg.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except: pass
    else:
        msg = await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb)
        bj_solo_sessions[uid]["msg"] = msg


async def _bj_solo_finish(uid: int, chat_id: int, edit_msg=None, auto_bj=False):
    sess = bj_solo_sessions.pop(uid, None)
    if not sess: return

    p     = sess["player"]
    d     = sess["dealer"]
    bet   = sess["bet"]
    p_val = _bj_val(p)

    # Дилер добирает до 17
    while _bj_val(d) < 17:
        d.append(_bj_card())
    d_val = _bj_val(d)

    p_str = _bj_hand_str(p)
    d_str = _bj_hand_str(d)

    if p_val > 21:
        result = f"💥 Перебор! -{fmt_coins(bet)} 🪙"
        won = False
    elif auto_bj:
        payout = int(bet * 2.5)  # блэкджек платит 3:2
        db.update_coins(uid, payout)
        result = f"🎉 БЛЭКДЖЕК! +{fmt_coins(payout - bet)} 🪙"
        won = True
    elif d_val > 21:
        payout = int(bet * 2)
        db.update_coins(uid, payout)
        result = f"🎉 Перебор у дилера! +{fmt_coins(payout - bet)} 🪙"
        won = True
    elif p_val > d_val:
        payout = int(bet * 2)
        db.update_coins(uid, payout)
        result = f"🏆 Победа! +{fmt_coins(payout - bet)} 🪙"
        won = True
    elif p_val == d_val:
        db.update_coins(uid, bet)
        result = "🤝 Ничья — ставка возвращена"
        won = False
    else:
        result = f"❌ Поражение. -{fmt_coins(bet)} 🪙"
        won = False

    db.record_game(uid, won, bet)
    db.add_xp(uid, bet // 10 + 20 if won else 5)
    db.update_task_progress(uid, "play5")
    db.update_task_progress(uid, "bet1000", bet)
    if won: db.update_task_progress(uid, "win3")

    user_after = db.get_user(uid)
    text = (
        f"🃏 <b>Блэкджек — итог</b>\n"
        f"{'─'*28}\n"
        f"🏦 Дилер:  {d_str}  = {d_val}\n"
        f"👤 Ты:     {p_str}  = {p_val}\n"
        f"{'─'*28}\n"
        f"{result}\n"
        f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙"
    )

    if edit_msg:
        try: await edit_msg.edit_text(text, parse_mode="HTML")
        except: pass
    else:
        msg = bj_solo_sessions.get(uid, {}).get("msg")
        try: await bot.send_message(chat_id, text, parse_mode="HTML")
        except: pass


@dp.callback_query(F.data.startswith("bj_hit_"))
async def cb_bj_hit(callback: CallbackQuery):
    uid = int(callback.data.replace("bj_hit_", ""))
    if callback.from_user.id != uid:
        await callback.answer("❌ Это не твоя игра!", show_alert=True); return
    sess = bj_solo_sessions.get(uid)
    if not sess or sess["done"]:
        await callback.answer("Игра уже завершена!"); return
    sess["player"].append(_bj_card())
    await callback.answer()
    await _bj_solo_show(uid, callback.message.chat.id, edit_msg=callback.message)


@dp.callback_query(F.data.startswith("bj_stand_"))
async def cb_bj_stand(callback: CallbackQuery):
    uid = int(callback.data.replace("bj_stand_", ""))
    if callback.from_user.id != uid:
        await callback.answer("❌ Это не твоя игра!", show_alert=True); return
    sess = bj_solo_sessions.get(uid)
    if not sess or sess["done"]:
        await callback.answer("Игра уже завершена!"); return
    await callback.answer()
    await _bj_solo_finish(uid, callback.message.chat.id, edit_msg=callback.message)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МУЛЬТИПЛЕЕРНЫЙ БЛЭКДЖЕК  🃏👥
#  /bjroom — создать комнату
#  /bjjoin <код> — войти
#  /bjstart — начать игру
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Комнаты: код → {host, players, bets, hands, dealer, state, msg_ids, bet}
bj_rooms: dict[str, dict] = {}
# uid → код комнаты
bj_player_room: dict[int, str] = {}


def _bj_make_code():
    return "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=4))


def _bj_room_text(room: dict) -> str:
    lines = [
        f"🃏 <b>Блэкджек — Комната #{room['code']}</b>\n",
        f"👑 Хост: {room['host_name']}\n",
        f"💸 Ставка: {fmt_coins(room['bet'])} 🪙\n",
        f"{'─'*24}\n",
        f"👥 Игроки ({len(room['players'])}/7):\n",
    ]
    for uid in room["players"]:
        name = room["names"][uid]
        lines.append(f"  • {name}\n")
    if room["state"] == "waiting":
        lines.append(f"\n<i>Ожидаем игроков...\nКод для входа: <code>{room['code']}</code></i>")
    return "".join(lines)


def _bj_room_kb(room: dict) -> InlineKeyboardMarkup:
    if room["state"] == "waiting":
        rows = [[InlineKeyboardButton(text="▶️ Начать игру", callback_data=f"bjr_start_{room['code']}")]]
        return InlineKeyboardMarkup(inline_keyboard=rows)
    return InlineKeyboardMarkup(inline_keyboard=[])


def _bj_hand_text(room: dict) -> str:
    """Строит текст игрового стола для всех."""
    d_card1 = room["dealer"][0][0]
    lines = [
        f"🃏 <b>Блэкджек — Комната #{room['code']}</b>\n",
        f"{'─'*24}\n",
        f"🏦 <b>Дилер:</b>  {d_card1}  🂠\n",
        f"{'─'*24}\n",
    ]
    for uid in room["players"]:
        hand  = room["hands"][uid]
        val   = _bj_val(hand)
        name  = room["names"][uid]
        cards = _bj_hand_str(hand)
        done  = room["done_players"].get(uid, False)
        bust  = val > 21
        bj    = val == 21 and len(hand) == 2

        if bust:   icon = "💥"
        elif bj:   icon = "🎉"
        elif done: icon = "✋"
        else:      icon = "🎮"

        current = " ◀ ходит" if uid == room.get("current_turn") and not done else ""
        lines.append(f"{icon} <b>{name}</b>{current}\n   {cards} = {val}\n")

    return "".join(lines)


def _bj_turn_kb(uid: int, code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Ещё карту", callback_data=f"bjm_hit_{code}_{uid}"),
        InlineKeyboardButton(text="✋ Хватит",    callback_data=f"bjm_stand_{code}_{uid}"),
    ]])


async def _bj_broadcast(room: dict, text: str, kb=None):
    """Отправить/обновить сообщение всем игрокам комнаты."""
    for uid in room["players"]:
        try:
            old_msg = room["msg_ids"].get(uid)
            if old_msg:
                try:
                    await bot.edit_message_text(
                        text, chat_id=uid, message_id=old_msg,
                        parse_mode="HTML",
                        reply_markup=kb or InlineKeyboardMarkup(inline_keyboard=[])
                    )
                    continue
                except: pass
            msg = await bot.send_message(uid, text, parse_mode="HTML",
                                         reply_markup=kb or InlineKeyboardMarkup(inline_keyboard=[]))
            room["msg_ids"][uid] = msg.message_id
        except: pass


async def _bj_next_turn(room: dict):
    """Переключить ход на следующего игрока."""
    code    = room["code"]
    players = room["players"]
    current = room.get("current_turn")

    # Ищем следующего кто ещё не закончил
    idx     = players.index(current) if current in players else -1
    next_uid = None
    for i in range(1, len(players) + 1):
        candidate = players[(idx + i) % len(players)]
        if not room["done_players"].get(candidate):
            val = _bj_val(room["hands"][candidate])
            if val <= 21:
                next_uid = candidate
                break

    if next_uid is None:
        # Все закончили — ход дилера
        await _bj_dealer_turn(room)
        return

    room["current_turn"] = next_uid
    text = _bj_hand_text(room)

    for uid in players:
        kb = _bj_turn_kb(uid, code) if uid == next_uid else InlineKeyboardMarkup(inline_keyboard=[])
        try:
            old = room["msg_ids"].get(uid)
            if old:
                try:
                    await bot.edit_message_text(text, chat_id=uid, message_id=old,
                                                parse_mode="HTML", reply_markup=kb)
                    continue
                except: pass
            msg = await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
            room["msg_ids"][uid] = msg.message_id
        except: pass

    # Уведомляем чей ход
    name = room["names"][next_uid]
    try:
        await bot.send_message(next_uid, f"👆 <b>Твой ход!</b>\nНажми «Ещё карту» или «Хватит».", parse_mode="HTML")
    except: pass


async def _bj_dealer_turn(room: dict):
    """Дилер добирает карты, подводим итоги."""
    # Раскрываем карты дилера
    while _bj_val(room["dealer"]) < 17:
        room["dealer"].append(_bj_card())

    d_val  = _bj_val(room["dealer"])
    d_str  = _bj_hand_str(room["dealer"])
    code   = room["code"]

    results_lines = [
        f"🃏 <b>Блэкджек — Итоги #{code}</b>\n",
        f"{'─'*24}\n",
        f"🏦 Дилер: {d_str} = <b>{d_val}</b>{'  💥 Перебор!' if d_val > 21 else ''}\n",
        f"{'─'*24}\n",
    ]

    for uid in room["players"]:
        hand  = room["hands"][uid]
        val   = _bj_val(hand)
        name  = room["names"][uid]
        bet   = room["bets"][uid]
        cards = _bj_hand_str(hand)

        bj21  = val == 21 and len(hand) == 2

        if val > 21:
            result = f"💥 Перебор — -{fmt_coins(bet)} 🪙"
            won    = False
            payout = 0
        elif bj21:
            payout = int(bet * 2.5)
            db.update_coins(uid, payout)
            result = f"🎉 БЛЭКДЖЕК! +{fmt_coins(payout - bet)} 🪙"
            won    = True
        elif d_val > 21 or val > d_val:
            payout = int(bet * 2)
            db.update_coins(uid, payout)
            result = f"🏆 Победа! +{fmt_coins(payout - bet)} 🪙"
            won    = True
        elif val == d_val:
            db.update_coins(uid, bet)
            result = f"🤝 Ничья — ставка возвращена"
            won    = False
            payout = bet
        else:
            result = f"❌ Поражение — -{fmt_coins(bet)} 🪙"
            won    = False
            payout = 0

        db.record_game(uid, won, bet)
        db.add_xp(uid, bet // 10 + 20 if won else 5)
        db.update_task_progress(uid, "play5")
        db.update_task_progress(uid, "bet1000", bet)
        if won: db.update_task_progress(uid, "win3")

        user_after = db.get_user(uid)
        results_lines.append(
            f"{'🏆' if won else '❌'} <b>{name}</b>: {cards} = {val}\n"
            f"   {result} | Баланс: {fmt_coins(user_after['coins'])} 🪙\n"
        )

    final_text = "".join(results_lines)
    await _bj_broadcast(room, final_text)

    # Убираем комнату
    for uid in room["players"]:
        bj_player_room.pop(uid, None)
    bj_rooms.pop(code, None)


@dp.message(Command("bjroom", ignore_mention=True))
@ensure_registered
async def cmd_bjroom(message: Message):
    """Создать комнату мультиплеерного блэкджека."""
    args = message.text.split()
    uid  = message.from_user.id

    if uid in bj_player_room:
        await message.answer("⚠️ Ты уже в комнате! Выйди командой /bjleave")
        return

    if len(args) < 2:
        await message.answer(
            "🃏 Использование: /bjroom &lt;ставка&gt;\n"
            "Пример: /bjroom 500\n\n"
            "Затем другие игроки могут войти через /bjjoin &lt;код&gt;",
            parse_mode="HTML"
        )
        return

    user = db.get_user(uid)
    bet, err = validate_bet(user, args[1])
    if err:
        await message.answer(err); return

    code = _bj_make_code()
    while code in bj_rooms:
        code = _bj_make_code()

    room = {
        "code":         code,
        "host":         uid,
        "host_name":    message.from_user.full_name,
        "players":      [uid],
        "names":        {uid: message.from_user.full_name},
        "bets":         {uid: bet},
        "hands":        {},
        "dealer":       [],
        "done_players": {},
        "current_turn": None,
        "msg_ids":      {},
        "state":        "waiting",
        "bet":          bet,
    }
    bj_rooms[code]         = room
    bj_player_room[uid]    = code

    # Резервируем ставку
    db.update_coins(uid, -bet)

    msg = await message.answer(
        _bj_room_text(room),
        parse_mode="HTML",
        reply_markup=_bj_room_kb(room)
    )
    room["msg_ids"][uid] = msg.message_id

    await message.answer(
        f"✅ Комната создана!\n\n"
        f"Код для приглашения: <code>{code}</code>\n"
        f"Поделись кодом с друзьями — они войдут через:\n"
        f"<code>/bjjoin {code}</code>\n\n"
        f"Когда все зайдут — нажми <b>«▶️ Начать игру»</b>",
        parse_mode="HTML"
    )


@dp.message(Command("bjjoin", ignore_mention=True))
@ensure_registered
async def cmd_bjjoin(message: Message):
    """Войти в комнату по коду."""
    args = message.text.split()
    uid  = message.from_user.id

    if uid in bj_player_room:
        await message.answer("⚠️ Ты уже в комнате! Выйди командой /bjleave")
        return

    if len(args) < 2:
        await message.answer("🃏 Использование: /bjjoin &lt;код&gt;\nПример: /bjjoin ABCD", parse_mode="HTML")
        return

    code = args[1].upper()
    room = bj_rooms.get(code)

    if not room:
        await message.answer("❌ Комната не найдена. Проверь код.")
        return
    if room["state"] != "waiting":
        await message.answer("❌ Игра уже началась!")
        return
    if len(room["players"]) >= 7:
        await message.answer("❌ Комната заполнена (макс. 7 игроков).")
        return

    bet  = room["bet"]
    user = db.get_user(uid)
    if user["coins"] < bet:
        await message.answer(f"❌ Не хватает монет. Нужно {fmt_coins(bet)} 🪙, у тебя {fmt_coins(user['coins'])} 🪙")
        return

    db.update_coins(uid, -bet)
    room["players"].append(uid)
    room["names"][uid]  = message.from_user.full_name
    room["bets"][uid]   = bet
    bj_player_room[uid] = code

    await message.answer(f"✅ Ты вошёл в комнату <b>#{code}</b>!\nСтавка: {fmt_coins(bet)} 🪙\nОжидай начала игры.", parse_mode="HTML")

    # Обновляем лобби у всех
    text = _bj_room_text(room)
    for p_uid in room["players"]:
        try:
            old = room["msg_ids"].get(p_uid)
            kb  = _bj_room_kb(room) if p_uid == room["host"] else InlineKeyboardMarkup(inline_keyboard=[])
            if old:
                await bot.edit_message_text(text, chat_id=p_uid, message_id=old, parse_mode="HTML", reply_markup=kb)
            else:
                msg = await bot.send_message(p_uid, text, parse_mode="HTML", reply_markup=kb)
                room["msg_ids"][p_uid] = msg.message_id
        except: pass


@dp.message(Command("bjleave", ignore_mention=True))
@ensure_registered
async def cmd_bjleave(message: Message):
    """Покинуть комнату (только до начала игры)."""
    uid  = message.from_user.id
    code = bj_player_room.get(uid)
    if not code:
        await message.answer("Ты не в комнате.")
        return
    room = bj_rooms.get(code)
    if not room or room["state"] != "waiting":
        await message.answer("❌ Нельзя выйти после начала игры!")
        return

    # Возврат ставки
    db.update_coins(uid, room["bets"].get(uid, 0))
    room["players"].remove(uid)
    room["names"].pop(uid, None)
    room["bets"].pop(uid, None)
    bj_player_room.pop(uid, None)

    await message.answer("👋 Ты вышел из комнаты. Ставка возвращена.")

    if uid == room["host"]:
        # Хост вышел — закрываем комнату
        for p_uid in room["players"]:
            db.update_coins(p_uid, room["bets"].get(p_uid, 0))
            bj_player_room.pop(p_uid, None)
            try: await bot.send_message(p_uid, "⚠️ Хост покинул комнату. Ставки возвращены.")
            except: pass
        bj_rooms.pop(code, None)
    else:
        # Обновляем лобби
        text = _bj_room_text(room)
        for p_uid in room["players"]:
            try:
                old = room["msg_ids"].get(p_uid)
                kb  = _bj_room_kb(room) if p_uid == room["host"] else InlineKeyboardMarkup(inline_keyboard=[])
                if old:
                    await bot.edit_message_text(text, chat_id=p_uid, message_id=old, parse_mode="HTML", reply_markup=kb)
            except: pass


@dp.callback_query(F.data.startswith("bjr_start_"))
async def cb_bjr_start(callback: CallbackQuery):
    """Хост нажал «Начать игру»."""
    code = callback.data.replace("bjr_start_", "")
    room = bj_rooms.get(code)

    if not room:
        await callback.answer("Комната не найдена!", show_alert=True); return
    if callback.from_user.id != room["host"]:
        await callback.answer("Только хост может начать!", show_alert=True); return
    if len(room["players"]) < 1:
        await callback.answer("Нужен хотя бы 1 игрок!", show_alert=True); return
    if room["state"] != "waiting":
        await callback.answer("Игра уже идёт!"); return

    room["state"] = "playing"

    # Раздаём карты
    room["dealer"] = [_bj_card(), _bj_card()]
    for uid in room["players"]:
        room["hands"][uid]        = [_bj_card(), _bj_card()]
        room["done_players"][uid] = False

    await callback.answer("🃏 Игра началась!")

    # Показываем стол и начинаем с первого игрока
    room["current_turn"] = room["players"][0]
    text = _bj_hand_text(room)

    for uid in room["players"]:
        kb = _bj_turn_kb(uid, code) if uid == room["players"][0] else InlineKeyboardMarkup(inline_keyboard=[])
        try:
            msg = await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
            room["msg_ids"][uid] = msg.message_id
        except: pass

    name = room["names"][room["players"][0]]
    for uid in room["players"]:
        if uid != room["players"][0]:
            try: await bot.send_message(uid, f"👆 Сейчас ходит <b>{name}</b>", parse_mode="HTML")
            except: pass


@dp.callback_query(F.data.startswith("bjm_hit_"))
async def cb_bjm_hit(callback: CallbackQuery):
    parts = callback.data.split("_")  # bjm_hit_CODE_UID
    code  = parts[2]
    uid   = int(parts[3])

    if callback.from_user.id != uid:
        await callback.answer("❌ Не твой ход!", show_alert=True); return

    room = bj_rooms.get(code)
    if not room or room.get("current_turn") != uid:
        await callback.answer("Сейчас не твой ход!", show_alert=True); return

    room["hands"][uid].append(_bj_card())
    val = _bj_val(room["hands"][uid])
    await callback.answer(f"Карта взята! Сумма: {val}")

    if val >= 21:
        room["done_players"][uid] = True
        if val > 21:
            try: await bot.send_message(uid, f"💥 <b>Перебор!</b> У тебя {val} очков.", parse_mode="HTML")
            except: pass
        await _bj_next_turn(room)
    else:
        # Обновляем стол
        text = _bj_hand_text(room)
        kb   = _bj_turn_kb(uid, code)
        for p_uid in room["players"]:
            kb2 = kb if p_uid == uid else InlineKeyboardMarkup(inline_keyboard=[])
            try:
                old = room["msg_ids"].get(p_uid)
                if old:
                    await bot.edit_message_text(text, chat_id=p_uid, message_id=old, parse_mode="HTML", reply_markup=kb2)
            except: pass


@dp.callback_query(F.data.startswith("bjm_stand_"))
async def cb_bjm_stand(callback: CallbackQuery):
    parts = callback.data.split("_")
    code  = parts[2]
    uid   = int(parts[3])

    if callback.from_user.id != uid:
        await callback.answer("❌ Не твой ход!", show_alert=True); return

    room = bj_rooms.get(code)
    if not room or room.get("current_turn") != uid:
        await callback.answer("Сейчас не твой ход!", show_alert=True); return

    room["done_players"][uid] = True
    await callback.answer("✋ Хватит!")
    await _bj_next_turn(room)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  КРАШ  🚀  (реальный реалтайм)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

crash_sessions: dict[int, dict] = {}


def crash_cashout_kb(uid: int, mult: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"💰 ЗАБРАТЬ  x{mult:.2f}",
            callback_data=f"crash_cashout_{uid}"
        )
    ]])


@dp.message(Command("crash", ignore_mention=True))
@ensure_registered
async def cmd_crash(message: Message):
    uid  = message.from_user.id
    args = message.text.split()

    if len(args) < 2:
        await message.answer(
            "🚀 <b>Краш</b>\n\n"
            "Использование: /crash &lt;ставка&gt;\n\n"
            "Ракета взлетает — нажми <b>«ЗАБРАТЬ»</b> пока не упала!\n"
            "Коэффициент растёт каждые 1.5 сек. Не успел — теряешь ставку 💥",
            parse_mode="HTML"
        )
        return

    sess = crash_sessions.get(uid)
    if sess and not sess.get("done"):
        await message.answer("⚠️ У тебя уже активная игра! Нажми <b>«ЗАБРАТЬ»</b>.", parse_mode="HTML")
        return

    # Анти-абуз: кулдаун 15 сек после завершения предыдущей игры
    last_crash = db.get_setting(f"crash_last_{uid}")
    if last_crash and int(time.time()) - int(last_crash) < 15:
        wait = 15 - (int(time.time()) - int(last_crash))
        await message.answer(f"⏱ Подожди ещё <b>{wait} сек.</b> перед следующей игрой в краш.", parse_mode="HTML")
        return

    user = db.get_user(uid)
    bet, err = validate_bet(user, args[1])
    if err:
        await message.answer(err); return

    db.update_coins(uid, -bet)

    # Честный краш: экспоненциальное распределение
    # ~45% упадёт до x2, ~25% до x4, ~15% до x7, ~15% выше
    crash_at = round(min(random.expovariate(0.4) + 1.0, 30.0), 2)
    crash_at = max(crash_at, 1.05)  # минимум x1.05

    msg = await message.answer(
        f"🚀 <b>Ракета взлетела!</b>\n\n"
        f"📈 Коэффициент: <b>x1.00</b>\n"
        f"▱▱▱▱▱▱▱▱▱▱\n\n"
        f"💸 Ставка: {fmt_coins(bet)} 🪙\n"
        f"💰 Получишь: <b>{fmt_coins(bet)} 🪙</b>\n\n"
        f"⏱ <i>Нажми кнопку чтобы забрать!</i>",
        parse_mode="HTML",
        reply_markup=crash_cashout_kb(uid, 1.00)
    )

    crash_sessions[uid] = {
        "bet":      bet,
        "crash_at": crash_at,
        "current":  1.00,
        "msg":      msg,
        "done":     False,
    }

    asyncio.create_task(_crash_loop(uid))


async def _crash_loop(uid: int):
    """Цикл краша — обновляет коэффициент каждые 1.5 сек."""
    await asyncio.sleep(1.5)  # первая задержка перед стартом

    BARS = [
        "▱▱▱▱▱▱▱▱▱▱", "▰▱▱▱▱▱▱▱▱▱", "▰▰▱▱▱▱▱▱▱▱",
        "▰▰▰▱▱▱▱▱▱▱", "▰▰▰▰▱▱▱▱▱▱", "▰▰▰▰▰▱▱▱▱▱",
        "▰▰▰▰▰▰▱▱▱▱", "▰▰▰▰▰▰▰▱▱▱", "▰▰▰▰▰▰▰▰▱▱",
        "▰▰▰▰▰▰▰▰▰▱", "▰▰▰▰▰▰▰▰▰▰",
    ]

    mult = 1.00

    while True:
        if uid not in crash_sessions:
            return
        sess = crash_sessions[uid]
        if sess.get("done"):
            return

        # Рост коэффициента
        if mult < 2.0:
            mult = round(mult + random.uniform(0.10, 0.20), 2)
        elif mult < 5.0:
            mult = round(mult + random.uniform(0.20, 0.40), 2)
        else:
            mult = round(mult + random.uniform(0.40, 0.80), 2)

        crashed = mult >= sess["crash_at"]
        if crashed:
            mult = sess["crash_at"]

        sess["current"] = mult

        # Полоса не показывает реальный прогресс до краша
        bar_idx = min(int((mult - 1.0) / 4.0 * 10), 9)  # растёт просто с коэффициентом
        bar     = BARS[bar_idx]
        pot     = int(sess["bet"] * mult)
        fire    = "🔥" if mult > 3 else ("⚡" if mult > 6 else "")
        rocket  = "💥" if crashed else "🚀"

        try:
            await sess["msg"].edit_text(
                f"{rocket} <b>{'КРАШ!' if crashed else 'Ракета летит!'}</b> {fire}\n\n"
                f"📈 Коэффициент: <b>x{mult:.2f}</b>\n\n"
                f"💸 Ставка: {fmt_coins(sess['bet'])} 🪙\n"
                f"💰 {'Потерял:' if crashed else 'Получишь:'} <b>{fmt_coins(pot) if not crashed else fmt_coins(sess['bet'])} 🪙</b>"
                + ("" if crashed else "\n\n⏱ <i>Нажми кнопку чтобы забрать!</i>"),
                parse_mode="HTML",
                reply_markup=crash_cashout_kb(uid, mult) if not crashed else InlineKeyboardMarkup(inline_keyboard=[])
            )
        except Exception:
            pass

        if crashed:
            sess["done"] = True
            db.record_game(uid, False, sess["bet"])
            db.add_xp(uid, 5)
            db.update_task_progress(uid, "play5")
            db.update_task_progress(uid, "bet1000", sess["bet"])
            user_after = db.get_user(uid)
            try:
                await sess["msg"].edit_text(
                    f"💥 <b>КРАШ на x{mult:.2f}!</b>\n\n"
                    f"▰▰▰▰▰▰▰▰▰▰  💥\n\n"
                    f"Ракета взорвалась 😢\n"
                    f"Потерял: -{fmt_coins(sess['bet'])} 🪙\n\n"
                    f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
                    parse_mode="HTML"
                )
            except: pass
            crash_sessions.pop(uid, None)
            return

        await asyncio.sleep(1.5)


@dp.callback_query(F.data.startswith("crash_cashout_"))
async def cb_crash_cashout(callback: CallbackQuery):
    uid = int(callback.data.replace("crash_cashout_", ""))

    if callback.from_user.id != uid:
        await callback.answer("❌ Это не твоя игра!", show_alert=True); return

    sess = crash_sessions.get(uid)
    if not sess:
        await callback.answer("⚠️ Игра уже завершена!", show_alert=True); return
    if sess.get("done"):
        await callback.answer("💥 Ракета уже упала!", show_alert=True); return

    sess["done"] = True
    mult   = sess["current"]
    bet    = sess["bet"]
    payout = int(bet * mult)

    db.update_coins(uid, payout)
    db.record_game(uid, True, bet)
    db.add_xp(uid, bet // 10 + 20)
    db.update_task_progress(uid, "play5")
    db.update_task_progress(uid, "win3")
    db.update_task_progress(uid, "bet1000", bet)
    db.set_setting(f"crash_last_{uid}", str(int(time.time())))

    user_after = db.get_user(uid)
    crash_sessions.pop(uid, None)

    await callback.answer(f"✅ Забрал x{mult:.2f}!", show_alert=False)

    try:
        await callback.message.edit_text(
            f"✅ <b>Забрал на x{mult:.2f}!</b>\n\n"
            f"Краш был на x{sess['crash_at']:.2f}\n"
            f"💰 Выплата: {fmt_coins(payout)} 🪙  (+{fmt_coins(payout - bet)})\n\n"
            f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
            parse_mode="HTML"
        )
    except: pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ДОНАТ-МАГАЗИН  ⭐
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def shop_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    for item_id, item in config.SHOP_ITEMS.items():
        buttons.append([
            InlineKeyboardButton(
                text=f"{item['title']} — ⭐{item['stars']}",
                callback_data=f"buy_{item_id}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("donate", ignore_mention=True))
@ensure_registered
async def cmd_donate(message: Message):
    text = (
        "⭐ <b>Магазин Telegram Stars</b>\n\n"
        "Поддержи казино и получи бонусы!\n\n"
    )
    for item in config.SHOP_ITEMS.values():
        text += f"  • {item['title']} — ⭐ {item['stars']} Stars\n"
        text += f"    <i>{item['desc']}</i>\n\n"

    await message.answer(text, reply_markup=shop_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("buy_"))
async def cb_buy(callback: CallbackQuery):
    item_id = callback.data.replace("buy_", "")
    item    = config.SHOP_ITEMS.get(item_id)
    if not item:
        await callback.answer("Товар не найден!", show_alert=True)
        return

    await callback.message.answer_invoice(
        title       = item["title"],
        description = item["desc"],
        payload     = f"{item_id}:{callback.from_user.id}",
        currency    = "XTR",
        prices      = [LabeledPrice(label=item["title"], amount=item["stars"])],
    )
    await callback.answer()


@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)


@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payload   = message.successful_payment.invoice_payload
    parts     = payload.split(":")
    item_id   = parts[0]
    user_id   = int(parts[1]) if len(parts) > 1 else message.from_user.id

    item      = config.SHOP_ITEMS.get(item_id)
    if not item:
        return

    rewards = []
    if item["coins"] > 0:
        db.update_coins(user_id, item["coins"])
        rewards.append(f"+{fmt_coins(item['coins'])} 🪙")

    if "vip" in item_id:
        db.set_vip(user_id, 7)
        rewards.append("⭐ VIP на 7 дней")

    if "case_rare" in item_id:
        prize = random.randint(1_000, 10_000)
        db.update_coins(user_id, prize)
        rewards.append(f"🎁 Кейс: +{fmt_coins(prize)} 🪙")

    if "case_epic" in item_id:
        prize = random.randint(5_000, 50_000)
        db.update_coins(user_id, prize)
        rewards.append(f"🎁 Эпик кейс: +{fmt_coins(prize)} 🪙")

    reward_text = "\n".join(rewards) or "Спасибо!"
    await message.answer(
        f"✅ <b>Оплата прошла успешно!</b>\n\n{reward_text}\n\nСпасибо за поддержку! 🎉",
        parse_mode="HTML"
    )




# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ТЕСТИРОВАНИЕ ДОНАТА (только для админов)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def test_shop_keyboard() -> InlineKeyboardMarkup:
    """Кнопки тестового магазина — симулируют покупку без Stars."""
    buttons = []
    for item_id, item in config.SHOP_ITEMS.items():
        buttons.append([
            InlineKeyboardButton(
                text=f"[ТЕСТ] {item['title']}",
                callback_data=f"test_buy_{item_id}"
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.message(Command("testdonate", ignore_mention=True))
async def cmd_test_donate(message: Message):
    """Тестовый магазин — только для админов, Stars не списываются."""
    if not is_admin(message.from_user.id):
        await message.answer("❌ Только для администраторов.")
        return
    await message.answer(
        "🧪 <b>Тестовый магазин</b>\n\n"
        "Симулирует покупку <b>без списания Stars</b>.\n"
        "Только для админов — для проверки работы наград.\n\n"
        "Выбери товар:",
        parse_mode="HTML",
        reply_markup=test_shop_keyboard()
    )


@dp.callback_query(F.data.startswith("test_buy_"))
async def cb_test_buy(callback: CallbackQuery):
    """Симулирует успешную покупку без реальной оплаты."""
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Только для администраторов!", show_alert=True)
        return

    item_id = callback.data.replace("test_buy_", "")
    item    = config.SHOP_ITEMS.get(item_id)
    if not item:
        await callback.answer("Товар не найден!", show_alert=True)
        return

    user_id = callback.from_user.id
    rewards = []

    if item["coins"] > 0:
        db.update_coins(user_id, item["coins"])
        rewards.append(f"+{fmt_coins(item['coins'])} 🪙")

    if "vip" in item_id:
        db.set_vip(user_id, 7)
        rewards.append("⭐ VIP на 7 дней")

    if "case_rare" in item_id:
        prize = random.randint(1_000, 10_000)
        db.update_coins(user_id, prize)
        rewards.append(f"🎁 Редкий кейс: +{fmt_coins(prize)} 🪙")

    if "case_epic" in item_id:
        prize = random.randint(5_000, 50_000)
        db.update_coins(user_id, prize)
        rewards.append(f"🎁 Эпик кейс: +{fmt_coins(prize)} 🪙")

    user_after  = db.get_user(user_id)
    reward_text = "\n".join(rewards) or "—"

    await callback.answer("✅ Тест успешен!", show_alert=False)
    await callback.message.answer(
        f"🧪 <b>Тест доната — успешно!</b>\n\n"
        f"Товар: {item['title']}\n"
        f"Stars потрачено: <i>0 (тест)</i>\n\n"
        f"Награды:\n{reward_text}\n\n"
        f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
        parse_mode="HTML"
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  МОДЕРАТОР-ПАНЕЛЬ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def moder_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика",       callback_data="mod_stats"),
         InlineKeyboardButton(text="🔍 Найти игрока",     callback_data="mod_search")],
        [InlineKeyboardButton(text="💬 Написать игроку",  callback_data="mod_msg"),
         InlineKeyboardButton(text="📢 Рассылка",         callback_data="mod_broadcast")],
        [InlineKeyboardButton(text="⚠️ Предупреждение",   callback_data="mod_warn"),
         InlineKeyboardButton(text="📋 Топ нарушителей",  callback_data="mod_warns_top")],
        [InlineKeyboardButton(text="🎮 Игры игрока",      callback_data="mod_gamelog"),
         InlineKeyboardButton(text="💰 Баланс игрока",    callback_data="mod_balance")],
    ])


@dp.message(Command("moder", ignore_mention=True))
async def cmd_moder(message: Message):
    if not is_mod(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return
    await message.answer("🛡 <b>Панель модератора</b>", reply_markup=moder_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("mod_"))
async def mod_callbacks(callback: CallbackQuery, state: FSMContext):
    uid = callback.from_user.id
    if not is_mod(uid):
        await callback.answer("Нет доступа.", show_alert=True); return

    action = callback.data

    if action == "mod_stats":
        s = db.get_stats()
        total = s["total_wins"] + s["total_losses"]
        wr = f"{s['total_wins']/total*100:.1f}%" if total else "—"
        rate = db.get_usdt_rate()
        await callback.message.answer(
            f"📊 <b>Статистика казино</b>\n\n"
            f"👥 Игроков: {s['total_users']} (+{s['new_today']} сегодня)\n"
            f"⭐ VIP: {s['vip_count']}\n"
            f"🪙 Монет в обращении: {fmt_coins(s['total_coins'])}\n"
            f"💵 Курс USDT: {rate} 🪙\n"
            f"🎮 Игр сыграно: {total}\n"
            f"📈 WR игроков: {wr}",
            parse_mode="HTML"
        )

    elif action == "mod_search":
        await state.set_state(AdminStates.wait_broadcast)
        await state.update_data(action="mod_search")
        await callback.message.answer("🔍 Введи имя, @username или ID игрока:")

    elif action == "mod_msg":
        await state.set_state(AdminStates.wait_give_uid)
        await state.update_data(action="msg")
        await callback.message.answer("💬 Введи user_id игрока которому написать:")

    elif action == "mod_broadcast":
        await state.set_state(AdminStates.wait_broadcast)
        await state.update_data(action="broadcast")
        await callback.message.answer("📢 Введи текст рассылки (уйдёт всем игрокам):")

    elif action == "mod_warn":
        await state.set_state(AdminStates.wait_give_uid)
        await state.update_data(action="mod_warn")
        await callback.message.answer("⚠️ Введи user_id игрока для предупреждения:")

    elif action == "mod_warns_top":
        # Топ по предупреждениям
        rows = db.get_top(20)
        lines = ["📋 <b>Последние зарегистрированные игроки (топ по монетам):</b>\n"]
        for r in rows[:10]:
            warns = int(db.get_setting(f"warns_{r['user_id']}") or 0)
            w_str = f" ⚠️x{warns}" if warns else ""
            lines.append(f"• {r['full_name']}{w_str} — {fmt_coins(r['coins'])} 🪙")
        await callback.message.answer("\n".join(lines), parse_mode="HTML")

    elif action == "mod_gamelog":
        await state.set_state(AdminStates.wait_give_uid)
        await state.update_data(action="mod_gamelog")
        await callback.message.answer("🎮 Введи user_id игрока для просмотра статистики:")

    elif action == "mod_balance":
        await state.set_state(AdminStates.wait_give_uid)
        await state.update_data(action="mod_balance")
        await callback.message.answer("💰 Введи user_id игрока для просмотра баланса:")

    await callback.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  АДМИН-ПАНЕЛЬ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика",      callback_data="adm_stats"),
         InlineKeyboardButton(text="🏆 Топ-5",           callback_data="adm_top")],
        [InlineKeyboardButton(text="💰 Выдать монеты",   callback_data="adm_give"),
         InlineKeyboardButton(text="💸 Забрать монеты",  callback_data="adm_take")],
        [InlineKeyboardButton(text="⭐ Выдать VIP",      callback_data="adm_vip"),
         InlineKeyboardButton(text="🚫 Забрать VIP",     callback_data="adm_delvip")],
        [InlineKeyboardButton(text="🔍 Найти игрока",    callback_data="adm_search"),
         InlineKeyboardButton(text="💬 Написать игроку", callback_data="adm_msg")],
        [InlineKeyboardButton(text="🎁 Подарить монеты", callback_data="adm_gift"),
         InlineKeyboardButton(text="🎟 Промокод",        callback_data="adm_promo")],
        [InlineKeyboardButton(text="🎲 Изменить шанс",   callback_data="adm_chance"),
         InlineKeyboardButton(text="📢 Рассылка",        callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔄 Обнулить игрока", callback_data="adm_reset_one"),
         InlineKeyboardButton(text="☢️ Обнулить всех",   callback_data="adm_reset_all")],
        [InlineKeyboardButton(text="🚫 Забанить",        callback_data="adm_ban"),
         InlineKeyboardButton(text="✅ Разбанить",       callback_data="adm_unban")],
    ])


@dp.message(Command("admin", ignore_mention=True))
async def cmd_admin(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа.")
        return
    await message.answer("👑 <b>Админ-панель</b>", reply_markup=admin_keyboard(), parse_mode="HTML")


@dp.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    s = db.get_stats()
    total_games = s["total_wins"] + s["total_losses"]
    wr = f"{s['total_wins']/total_games*100:.1f}%" if total_games else "—"
    bank   = db.get_bank_total()
    refs   = db.get_referral_count()
    promos = db.get_promo_total_uses()
    tu = s["total_users"]; nt = s["new_today"]; vc = s["vip_count"]
    tc = fmt_coins(s["total_coins"]); tw = s["total_wins"]; tl = s["total_losses"]
    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей: {tu}\n"
        f"🆕 Новых сегодня: {nt}\n"
        f"⭐ VIP игроков: {vc}\n"
        f"🪙 Монет в обращении: {tc}\n"
        f"🏦 В банке: {fmt_coins(bank)}\n"
        f"👫 Рефералов: {refs}\n"
        f"🎟 Промо активировано: {promos}\n"
        f"🎮 Игр сыграно: {total_games}\n"
        f"🏆 Побед / 💀 Поражений: {tw} / {tl}\n"
        f"📈 WR игроков: {wr}"
    )
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "adm_top")
async def adm_top(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return
    rows  = db.get_top(5)
    lines = ["🏆 <b>Топ-5 (Админ)</b>\n"]
    for i, r in enumerate(rows, 1):
        lines.append(f"{i}. {r['full_name']} — {fmt_coins(r['coins'])} 🪙 | Ур.{r['level']}")
    await callback.message.answer("\n".join(lines), parse_mode="HTML")
    await callback.answer()


@dp.callback_query(F.data == "adm_delvip")
async def adm_delvip(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.wait_take_uid)
    await state.update_data(action="delvip")
    await callback.message.answer("Введи user_id игрока для снятия VIP:")
    await callback.answer()


@dp.callback_query(F.data == "adm_search")
async def adm_search(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.wait_broadcast)
    await state.update_data(action="search")
    await callback.message.answer("🔍 Введи имя, @username или ID игрока:")
    await callback.answer()


@dp.callback_query(F.data == "adm_msg")
async def adm_msg(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.wait_give_uid)
    await state.update_data(action="msg")
    await callback.message.answer("💬 Введи user_id игрока которому написать:")
    await callback.answer()


@dp.callback_query(F.data == "adm_gift")
async def adm_gift(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.wait_give_uid)
    await state.update_data(action="gift")
    await callback.message.answer("🎁 Введи user_id игрока для подарка:")
    await callback.answer()


@dp.callback_query(F.data == "adm_promo")
async def adm_promo_cb(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.wait_broadcast)
    await state.update_data(action="promo")
    await callback.message.answer(
        "🎟 Введи параметры промокода через пробел:\n"
        "<code>КОД МОНЕТЫ VIP_ДНЕЙ МАКС_АКТИВАЦИЙ</code>\n\n"
        "Пример: <code>NEWUSER 1000 0 100</code>",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_reset_one")
async def adm_reset_one(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.wait_give_uid)
    await state.update_data(action="reset_one")
    await callback.message.answer("🔄 Введи user_id игрока для обнуления:")
    await callback.answer()


@dp.callback_query(F.data == "adm_reset_all")
async def adm_reset_all_cb(callback: CallbackQuery):
    if not is_admin(callback.from_user.id): return
    await callback.message.answer(
        "☢️ <b>Обнулить ВСЕХ игроков?</b>\nЭто сбросит монеты, USDT, prestige, статистику у ВСЕХ!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ ДА, ОБНУЛИТЬ ВСЕХ", callback_data="adm_reset_all_confirm"),
            InlineKeyboardButton(text="❌ Отмена",             callback_data="adm_reset_all_cancel"),
        ]])
    )
    await callback.answer()


@dp.callback_query(F.data == "adm_reset_all_confirm")
async def adm_reset_all_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет доступа", show_alert=True); return
    try:
        db.reset_all_users()
        await callback.message.edit_text("✅ <b>Все игроки обнулены!</b>\n\nМонеты: 1000, USDT: 0, Prestige: 0, статистика: 0", parse_mode="HTML")
        await callback.answer("✅ Готово!")
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)


@dp.callback_query(F.data == "adm_reset_all_cancel")
async def adm_reset_all_cancel(callback: CallbackQuery):
    try:
        await callback.message.edit_text("❌ Отменено.")
    except: pass
    await callback.answer()


@dp.callback_query(F.data == "adm_ban")
async def adm_ban_cb(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.wait_give_uid)
    await state.update_data(action="mod_ban")
    await callback.message.answer("🚫 Введи user_id игрока для бана:")
    await callback.answer()


@dp.callback_query(F.data == "adm_unban")
async def adm_unban_cb(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminStates.wait_give_uid)
    await state.update_data(action="mod_unban")
    await callback.message.answer("✅ Введи user_id игрока для разбана:")
    await callback.answer()


@dp.callback_query(F.data == "adm_give")
async def adm_give_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.wait_give_uid)
    await callback.message.answer("Введи user_id игрока:")
    await callback.answer()


@dp.message(AdminStates.wait_give_uid)
async def adm_give_uid(message: Message, state: FSMContext):
    data = await state.get_data()
    action = data.get("action", "give")

    # Поиск игрока — обрабатываем здесь
    if action == "search":
        found = db.search_users(message.text.strip())
        if not found:
            await message.answer("❌ Игрок не найден.")
        else:
            lines = ["🔍 <b>Результаты поиска:</b>\n"]
            for u in found[:10]:
                vip = "⭐" if u["is_vip"] else ""
                fn = u["full_name"]; sid = u["user_id"]; un = u["username"] or chr(8212)
                co = fmt_coins(u["coins"]); lv = u["level"]
                lines.append(f"• <b>{fn}</b> {vip}  ID: <code>{sid}</code> @{un} | {co} 🪙 Ур.{lv}")
            await message.answer("\n".join(lines), parse_mode="HTML")
        await state.clear()
        return

    # msg — спрашиваем текст
    if action == "msg":
        try:
            uid = int(message.text.strip())
            await state.update_data(target_uid=uid)
            await state.set_state(AdminStates.wait_broadcast)
            await state.update_data(action="msg_send", target_uid=uid)
            await message.answer(f"Введи текст сообщения для игрока {uid}:")
        except ValueError:
            await message.answer("❌ Неверный ID")
            await state.clear()
        return

    # gift — спрашиваем сумму
    if action == "gift":
        try:
            uid = int(message.text.strip())
            await state.update_data(target_uid=uid, action="gift_amount")
            await state.set_state(AdminStates.wait_give_amount)
            await message.answer(f"Введи количество монет для подарка игроку {uid}:")
        except ValueError:
            await message.answer("❌ Неверный ID")
            await state.clear()
        return

    # mod_warn — отправляем предупреждение
    if action == "mod_warn":
        try:
            wid = int(message.text.strip())
            count = int(db.get_setting(f"warns_{wid}") or 0) + 1
            db.set_setting(f"warns_{wid}", str(count))
            try:
                await bot.send_message(wid,
                    f"⚠️ <b>Предупреждение от модератора</b>\n\n"
                    f"Это твоё предупреждение #{count}.\n"
                    f"При повторных нарушениях последуют санкции.",
                    parse_mode="HTML")
            except: pass
            await message.answer(f"✅ Предупреждение #{count} отправлено игроку {wid}.")
        except ValueError:
            await message.answer("❌ Неверный ID")
        await state.clear()
        return

    # mod_gamelog — статистика игр
    if action == "mod_gamelog":
        try:
            gid = int(message.text.strip())
            u2 = db.get_user(gid)
            if not u2:
                await message.answer("❌ Игрок не найден."); await state.clear(); return
            total_g = u2["wins"] + u2["losses"]
            wr2 = f"{u2['wins']/total_g*100:.1f}%" if total_g else "—"
            warns_cnt = db.get_setting(f"warns_{gid}") or "0"
            await message.answer(
                f"🎮 <b>Статистика игрока {u2['full_name']}</b>\n\n"
                f"ID: <code>{gid}</code>\n"
                f"Уровень: {u2['level']} | XP: {u2['xp']}\n"
                f"🏆 Побед: {u2['wins']}\n"
                f"💀 Поражений: {u2['losses']}\n"
                f"📈 Winrate: {wr2}\n"
                f"💸 Поставлено всего: {fmt_coins(u2['total_bet'])} 🪙\n"
                f"⚠️ Предупреждений: {warns_cnt}",
                parse_mode="HTML"
            )
        except ValueError:
            await message.answer("❌ Неверный ID")
        await state.clear()
        return

    # mod_balance — баланс игрока
    if action == "mod_balance":
        try:
            bid2 = int(message.text.strip())
            u3 = db.get_user(bid2)
            if not u3:
                await message.answer("❌ Игрок не найден."); await state.clear(); return
            usdt3 = db.get_usdt(bid2)
            pres3 = db.PRESTIGE_LEVELS.get(u3.get("prestige", 0), {}).get("name", "нет")
            vip3 = "⭐ VIP" if u3["is_vip"] else "Обычный"
            await message.answer(
                f"💰 <b>Баланс: {u3['full_name']}</b>\n\n"
                f"🪙 Монеты: {fmt_coins(u3['coins'])}\n"
                f"💵 USDT: {usdt3}\n"
                f"👑 Prestige: {pres3}\n"
                f"🏅 Статус: {vip3}",
                parse_mode="HTML"
            )
        except ValueError:
            await message.answer("❌ Неверный ID")
        await state.clear()
        return

    # reset_one
    if action == "reset_one":
        try:
            rid = int(message.text.strip())
            db.reset_user(rid)
            await message.answer(f"✅ Игрок {rid} обнулён (монеты 1000, всё остальное — 0).")
        except ValueError:
            await message.answer("❌ Неверный ID")
        await state.clear()
        return

    # mod_ban
    if action == "mod_ban":
        try:
            bid = int(message.text.strip())
            db.set_setting(f"banned_{bid}", "1")
            try:
                await bot.send_message(bid, "🚫 Ваш аккаунт заблокирован администрацией.")
            except: pass
            await message.answer(f"✅ Игрок {bid} забанен.")
        except ValueError:
            await message.answer("❌ Неверный ID")
        await state.clear()
        return

    # mod_unban
    if action == "mod_unban":
        try:
            ubid = int(message.text.strip())
            db.set_setting(f"banned_{ubid}", "0")
            try:
                await bot.send_message(ubid, "✅ Ваш аккаунт разблокирован.")
            except: pass
            await message.answer(f"✅ Игрок {ubid} разбанен.")
        except ValueError:
            await message.answer("❌ Неверный ID")
        await state.clear()
        return

    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный ID.")
        return
    await state.update_data(target_uid=uid)
    await state.set_state(AdminStates.wait_give_amount)
    await message.answer("Введи количество монет:")


@dp.message(AdminStates.wait_give_amount)
async def adm_give_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return
    data = await state.get_data()
    uid  = data["target_uid"]
    db.update_coins(uid, amount)
    await message.answer(f"✅ Выдано {fmt_coins(amount)} 🪙 игроку {uid}.")
    await state.clear()


@dp.callback_query(F.data == "adm_take")
async def adm_take_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.wait_take_uid)
    await callback.message.answer("Введи user_id игрока (у кого забрать):")
    await callback.answer()


@dp.message(AdminStates.wait_take_uid)
async def adm_take_uid(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный ID.")
        return
    await state.update_data(target_uid=uid)
    await state.set_state(AdminStates.wait_take_amount)
    await message.answer("Введи количество монет для изъятия:")


@dp.message(AdminStates.wait_take_amount)
async def adm_take_amount(message: Message, state: FSMContext):
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверная сумма.")
        return
    data = await state.get_data()
    uid  = data["target_uid"]
    db.update_coins(uid, -amount)
    await message.answer(f"✅ Изъято {fmt_coins(amount)} 🪙 у игрока {uid}.")
    await state.clear()


@dp.callback_query(F.data == "adm_vip")
async def adm_vip_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.wait_vip_uid)
    await callback.message.answer("Введи user_id для выдачи VIP:")
    await callback.answer()


@dp.message(AdminStates.wait_vip_uid)
async def adm_vip_uid(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный ID.")
        return
    db.set_vip(uid, 7)
    await message.answer(f"✅ VIP на 7 дней выдан игроку {uid}.")
    await state.clear()


@dp.callback_query(F.data == "adm_chance")
async def adm_chance_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.wait_chance_game)
    current = "\n".join(
        f"  {g}: {db.get_win_chance(g)*100:.0f}%"
        for g in ["slots","dice","roulette","blackjack","crash"]
    )
    await callback.message.answer(
        f"Текущие шансы:\n{current}\n\n"
        "Введи название игры (slots/dice/roulette/blackjack/crash):"
    )
    await callback.answer()


@dp.message(AdminStates.wait_chance_game)
async def adm_chance_game(message: Message, state: FSMContext):
    game = message.text.strip().lower()
    if game not in ("slots","dice","roulette","blackjack","crash"):
        await message.answer("❌ Неверная игра.")
        return
    await state.update_data(chance_game=game)
    await state.set_state(AdminStates.wait_chance_val)
    await message.answer(f"Введи новый шанс для {game} (0–100)%:")


@dp.message(AdminStates.wait_chance_val)
async def adm_chance_val(message: Message, state: FSMContext):
    try:
        val = float(message.text.strip().replace("%",""))
        assert 0 <= val <= 100
    except Exception:
        await message.answer("❌ Введи число от 0 до 100.")
        return
    data = await state.get_data()
    game = data["chance_game"]
    db.set_setting(f"win_chance_{game}", str(val / 100))
    await message.answer(f"✅ Шанс победы в {game} установлен: {val:.1f}%")
    await state.clear()


@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.wait_broadcast)
    await callback.message.answer("Введи сообщение для рассылки всем пользователям:")
    await callback.answer()


@dp.message(AdminStates.wait_broadcast)
async def adm_broadcast_send(message: Message, state: FSMContext):
    await state.clear()
    uids    = db.get_all_user_ids()
    success = 0
    for uid in uids:
        try:
            await bot.send_message(uid, f"📢 <b>Сообщение от администрации:</b>\n\n{message.text}", parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)   # задержка антифлуд
        except Exception:
            pass
    await message.answer(f"✅ Рассылка завершена: {success}/{len(uids)} доставлено.")




@dp.message(Command("notify", ignore_mention=True))
@ensure_registered
async def cmd_notify(message: Message):
    """Включить/выключить напоминание о бонусе."""
    uid  = message.from_user.id
    cur  = db.get_setting(f"notify_{uid}") or "on"
    new  = "off" if cur == "on" else "on"
    db.set_setting(f"notify_{uid}", new)
    if new == "on":
        await message.answer("🔔 Уведомления включены — напомню когда бонус готов!")
    else:
        await message.answer("🔕 Уведомления выключены.")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ПРОМОКОДЫ  🎟
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(Command("promo", ignore_mention=True))
@ensure_registered
async def cmd_promo(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer(
            "🎟 <b>Промокод</b>\n\n"
            "Использование: /promo &lt;КОД&gt;\n"
            "Пример: /promo CASINO2024",
            parse_mode="HTML"
        )
        return
    code = args[1].strip().upper()
    uid  = message.from_user.id
    res  = db.use_promo(uid, code)
    if not res["ok"]:
        await message.answer(f"❌ {res['err']}", parse_mode="HTML")
        return
    parts = []
    if res["coins"] > 0:
        parts.append(f"💰 +{fmt_coins(res['coins'])} монет")
    if res["vip_days"] > 0:
        parts.append(f"⭐ VIP на {res['vip_days']} дней")
    reward_text = "\n".join(parts) or "🎁 Бонус активирован"
    user_after  = db.get_user(uid)
    await message.answer(
        f"✅ <b>Промокод активирован!</b>\n\n"
        f"{reward_text}\n\n"
        f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
        parse_mode="HTML"
    )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  РЕФЕРАЛЫ  👫
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dp.message(Command("usdt", ignore_mention=True))
@ensure_registered
async def cmd_usdt(message: Message):
    rate = db.get_usdt_rate()
    diff = rate - 1000
    trend = ("📈 +" if diff > 0 else ("📉 " if diff < 0 else "➡️ ")) + (str(abs(diff)) if diff != 0 else "0")
    await message.answer(
        f"💵 <b>Курс USDT</b>\n\n"
        f"Сейчас: <b>1 USDT = {rate} 🪙</b>\n"
        f"Изменение: {trend} монет от базы (1000)\n\n"
        f"Курс меняется каждый день с шансом 5%\n"
        f"💱 Обменять: /exchange",
        parse_mode="HTML"
    )


@dp.message(Command("exchange", ignore_mention=True))
@ensure_registered
async def cmd_exchange(message: Message):
    """Обмен монет → USDT"""
    uid  = message.from_user.id
    args = message.text.split()
    user = db.get_user(uid)
    usdt = db.get_usdt(uid)
    rate = db.get_usdt_rate()  # динамический курс

    if len(args) == 1:
        # Показываем баланс и курс
        await message.answer(
            f"💱 <b>Обмен монет → USDT</b>\n\n"
            f"💰 Твои монеты: <b>{fmt_coins(user['coins'])} 🪙</b>\n"
            f"💵 Твои USDT: <b>{usdt} USDT</b>\n\n"
            f"📊 Курс: <b>1 USDT = {rate} 🪙</b>\n\n"
            f"Использование: <code>/exchange 5</code> — обменять 5 USDT\n"
            f"(минимум 1 USDT = 1 000 монет)",
            parse_mode="HTML"
        )
        return

    try:
        amount_usdt = int(args[1])
    except ValueError:
        await message.answer("❌ Укажи количество USDT: <code>/exchange 5</code>", parse_mode="HTML"); return

    if amount_usdt < 1:
        await message.answer("❌ Минимум 1 USDT"); return

    coins_needed = amount_usdt * rate
    if user["coins"] < coins_needed:
        await message.answer(
            f"❌ Недостаточно монет.\n"
            f"Нужно: <b>{fmt_coins(coins_needed)} 🪙</b>\n"
            f"У тебя: <b>{fmt_coins(user['coins'])} 🪙</b>",
            parse_mode="HTML"
        ); return

    db.update_coins(uid, -coins_needed)
    db.update_usdt(uid, amount_usdt)
    new_coins = db.get_user(uid)["coins"]
    new_usdt  = db.get_usdt(uid)

    await message.answer(
        f"✅ <b>Обмен выполнен!</b>\n\n"
        f"📤 Списано: <b>{fmt_coins(coins_needed)} 🪙</b>\n"
        f"📥 Получено: <b>{amount_usdt} USDT</b>\n\n"
        f"💰 Остаток монет: {fmt_coins(new_coins)} 🪙\n"
        f"💵 Баланс USDT: {new_usdt} USDT",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  PRESTIGE / СТАТУС ЗА USDT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(Command("prestige", ignore_mention=True))
@ensure_registered
async def cmd_prestige(message: Message):
    uid    = message.from_user.id
    args   = message.text.split()
    usdt   = db.get_usdt(uid)
    lvl, title = db.get_prestige(uid)
    levels = db.PRESTIGE_LEVELS

    # Показ магазина
    if len(args) == 1:
        lines = [f"👑 <b>Prestige — статусы за USDT</b>\n\n"
                 f"💵 Твой USDT: <b>{usdt}</b>\n"
                 f"⭐ Текущий статус: <b>{levels[lvl]['name'] or 'Нет'}</b>\n"
                 f"🏷 Приписка: <b>{title or 'нет'}</b>\n"
                 f"+{int(db.get_prestige_bonus(uid)*100)}% к выигрышам\n\n"
                 f"<b>Уровни статуса:</b>\n"]
        for i in range(1, 6):
            p = levels[i]
            mark = "✅ " if i == lvl else ("🔒 " if i > lvl else "✓ ")
            lines.append(f"{mark}<b>{p['name']}</b> — {p['price_usdt']} USDT\n"
                         f"   🏷 {p['title']} | +{int(p['bonus']*100)}% к выигрышам")
        lines.append("\n✏️ Изменить приписку: <code>/prestige title Мой текст</code>")
        await message.answer("\n".join(lines), parse_mode="HTML",
                             reply_markup=prestige_keyboard(lvl))
        return

    if args[1] == "buy" and len(args) >= 3:
        try:
            target_lvl = int(args[2])
        except ValueError:
            await message.answer("❌ Укажи уровень: /prestige buy 1"); return

        if target_lvl < 1 or target_lvl > 5:
            await message.answer("❌ Уровень от 1 до 5"); return
        if target_lvl <= lvl:
            await message.answer("❌ У тебя уже есть этот или более высокий статус"); return

        # Цена — разница от текущего
        p = levels[target_lvl]
        prev_price = levels[lvl]["price_usdt"]
        cost = p["price_usdt"] - prev_price

        if usdt < cost:
            await message.answer(
                f"❌ Недостаточно USDT.\n"
                f"Нужно: <b>{cost} USDT</b>\n"
                f"У тебя: <b>{usdt} USDT</b>\n\n"
                f"💱 Обменяй монеты: /exchange",
                parse_mode="HTML"
            ); return

        db.update_usdt(uid, -cost)
        db.set_prestige(uid, target_lvl)
        new_title = p["title"]

        await message.answer(
            f"🎉 <b>Статус получен!</b>\n\n"
            f"👑 {p['name']}\n"
            f"🏷 Приписка: <b>{new_title}</b>\n"
            f"💰 Бонус к выигрышам: <b>+{int(p['bonus']*100)}%</b>\n\n"
            f"Твоя приписка теперь везде отображается рядом с именем!\n"
            f"✏️ Изменить: <code>/prestige title Свой текст</code>",
            parse_mode="HTML"
        )
        return

    if args[1] == "title":
        if lvl == 0:
            await message.answer("❌ Сначала купи статус Prestige (/prestige)"); return
        if len(args) < 3:
            await message.answer("Укажи текст: <code>/prestige title Мой текст</code>", parse_mode="HTML"); return
        custom = " ".join(args[2:])[:20]  # макс 20 символов
        db.set_custom_title(uid, custom)
        await message.answer(f"✅ Приписка изменена: <b>{custom}</b>", parse_mode="HTML")
        return

    await message.answer("Использование: /prestige | /prestige buy 1 | /prestige title Текст")


@dp.message(Command("ref", ignore_mention=True))
@ensure_registered
async def cmd_ref(message: Message):
    uid      = message.from_user.id
    bot_info = await bot.get_me()
    link     = f"https://t.me/{bot_info.username}?start=ref{uid}"
    refs     = db.get_referrals(uid)
    total    = len(refs)
    earned   = total * 500

    await message.answer(
        f"👫 <b>Реферальная программа</b>\n\n"
        f"Приглашай друзей — получай <b>⭐ 1 день VIP</b> за каждого!\n\n"
        f"🔗 Твоя ссылка:\n<code>{link}</code>\n\n"
        f"👥 Приглашено: <b>{total}</b> игроков\n"
        f"⭐ VIP дней получено: <b>{total}</b>",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ПЕРЕВОД МОНЕТ  💸
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dp.message(Command("send", ignore_mention=True))
@ensure_registered
async def cmd_send(message: Message):
    args = message.text.split()
    uid  = message.from_user.id

    if len(args) < 3:
        await message.answer(
            "💸 <b>Перевод монет</b>\n\n"
            "Использование: /send &lt;@username или ID&gt; &lt;сумма&gt;\n"
            "Пример: /send @friend 1000\n\n"
            "Комиссия: <b>5%</b>",
            parse_mode="HTML"
        )
        return

    target_str = args[1].lstrip("@")
    amount_str = args[2]

    try:
        amount = int(amount_str)
    except ValueError:
        await message.answer("❌ Сумма должна быть числом."); return

    if amount < 100:
        await message.answer("❌ Минимальный перевод: 100 🪙"); return

    sender = db.get_user(uid)
    commission = max(50, int(amount * 0.05))
    total_cost = amount + commission

    if sender["coins"] < total_cost:
        await message.answer(
            f"❌ Недостаточно монет.\n"
            f"Нужно: {fmt_coins(total_cost)} 🪙 (включая комиссию {fmt_coins(commission)} 🪙)\n"
            f"У тебя: {fmt_coins(sender['coins'])} 🪙"
        ); return

    # Ищем получателя
    target = db.find_user_by_username(target_str) or db.get_user_by_id_safe(target_str)
    if not target:
        await message.answer(f"❌ Игрок @{target_str} не найден. Он должен быть зарегистрирован в боте."); return

    target_uid = target["user_id"]
    if target_uid == uid:
        await message.answer("❌ Нельзя переводить самому себе."); return

    db.update_coins(uid, -total_cost)
    db.update_coins(target_uid, amount)
    # Комиссия идёт первому админу
    if config.ADMIN_IDS:
        db.update_coins(config.ADMIN_IDS[0], commission)

    sender_after = db.get_user(uid)
    await message.answer(
        f"✅ <b>Перевод выполнен!</b>\n\n"
        f"Получатель: <b>{target['full_name']}</b>\n"
        f"Сумма: <b>{fmt_coins(amount)} 🪙</b>\n"
        f"Комиссия: <b>{fmt_coins(commission)} 🪙</b>\n\n"
        f"💼 Твой баланс: {fmt_coins(sender_after['coins'])} 🪙",
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            target_uid,
            f"💸 <b>Входящий перевод!</b>\n\n"
            f"От: <b>{message.from_user.full_name}</b>\n"
            f"Сумма: <b>{fmt_coins(amount)} 🪙</b>",
            parse_mode="HTML"
        )
    except: pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  БАНК (ВКЛАД)  🏦
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dp.message(Command("bank", ignore_mention=True))
@ensure_registered
async def cmd_bank(message: Message):
    args = message.text.split()
    uid  = message.from_user.id

    deposit_info = db.get_deposit(uid)
    user         = db.get_user(uid)

    if len(args) == 1:
        # Показать состояние вклада
        if deposit_info and deposit_info["amount"] > 0:
            deposited = deposit_info["amount"]
            days_left = max(0, deposit_info["unlock_at"] - int(time.time()))
            hours_left = days_left // 3600
            pct   = deposit_info["rate"]
            payout = int(deposited * (1 + pct))
            await message.answer(
                f"🏦 <b>Твой вклад</b>\n\n"
                f"💰 Вложено: <b>{fmt_coins(deposited)} 🪙</b>\n"
                f"📈 Процент: <b>{int(pct*100)}%</b>\n"
                f"💎 Получишь: <b>{fmt_coins(payout)} 🪙</b>\n"
                f"⏱ Осталось: <b>{hours_left} ч.</b>\n\n"
                f"<i>/bank забрать — снять вклад досрочно (без процентов)</i>",
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"🏦 <b>Банк — вклад под проценты</b>\n\n"
                f"Положи монеты на хранение и получи прибыль!\n\n"
                f"📅 <b>Тарифы:</b>\n"
                f"• 1 день  → +5%\n"
                f"• 3 дня   → +15%\n"
                f"• 7 дней  → +40%\n\n"
                f"💼 Баланс: {fmt_coins(user['coins'])} 🪙\n\n"
                f"<b>Использование:</b>\n"
                f"/bank 1000 1  — вложить 1000 на 1 день\n"
                f"/bank 5000 7  — вложить 5000 на 7 дней",
                parse_mode="HTML"
            )
        return

    if args[1] == "забрать":
        if not deposit_info or deposit_info["amount"] == 0:
            await message.answer("❌ У тебя нет активного вклада."); return
        # Досрочное снятие — только сумма без процентов
        db.update_coins(uid, deposit_info["amount"])
        db.clear_deposit(uid)
        await message.answer(
            f"🏦 Вклад снят досрочно.\n"
            f"Возвращено: {fmt_coins(deposit_info['amount'])} 🪙 (без процентов)",
            parse_mode="HTML"
        )
        return

    if len(args) < 3:
        await message.answer("❌ Укажи сумму и срок. Пример: /bank 1000 3"); return

    try:
        amount = int(args[1])
        days   = int(args[2])
    except ValueError:
        await message.answer("❌ Неверный формат. Пример: /bank 1000 3"); return

    if days not in (1, 3, 7):
        await message.answer("❌ Срок: 1, 3 или 7 дней."); return
    if amount < 100:
        await message.answer("❌ Минимальный вклад: 100 🪙"); return
    if user["coins"] < amount:
        await message.answer(f"❌ Недостаточно монет. У тебя: {fmt_coins(user['coins'])} 🪙"); return
    if deposit_info and deposit_info["amount"] > 0:
        await message.answer("❌ У тебя уже есть активный вклад. Сначала забери его: /bank забрать"); return

    rates = {1: 0.02, 3: 0.05, 7: 0.10}
    rate  = rates[days]
    db.update_coins(uid, -amount)
    db.create_deposit(uid, amount, rate, days)

    payout = int(amount * (1 + rate))
    await message.answer(
        f"🏦 <b>Вклад открыт!</b>\n\n"
        f"💰 Вложено: {fmt_coins(amount)} 🪙\n"
        f"📈 Процент: {int(rate*100)}% за {days} дн.\n"
        f"💎 Получишь: <b>{fmt_coins(payout)} 🪙</b>\n\n"
        f"<i>Заберёшь через {days} дн. командой /bank</i>",
        parse_mode="HTML"
    )


# Фоновая задача — выплата вкладов
async def bank_checker():
    while True:
        await asyncio.sleep(300)  # каждые 5 минут
        try:
            payouts = db.get_ready_deposits()
            for dep in payouts:
                uid    = dep["user_id"]
                payout = int(dep["amount"] * (1 + dep["rate"]))
                db.update_coins(uid, payout)
                db.clear_deposit(uid)
                try:
                    await bot.send_message(
                        uid,
                        f"🏦 <b>Вклад созрел!</b>\n\n"
                        f"💎 Начислено: <b>{fmt_coins(payout)} 🪙</b>\n"
                        f"(вклад {fmt_coins(dep['amount'])} + {int(dep['rate']*100)}%)",
                        parse_mode="HTML"
                    )
                except: pass
        except: pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  КЕЙСЫ  🎁
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Цены кейсов — в USDT!
CASES = {
    "bronze": {
        "name": "🥉 Бронзовый кейс", "price": 1,   # 1 USDT
        "prizes": [(500,35),(1500,30),(3000,20),(7000,10),(15000,4),(25000,1)],
    },
    "silver": {
        "name": "🥈 Серебряный кейс", "price": 5,   # 5 USDT
        "prizes": [(3000,32),(8000,28),(20000,22),(50000,12),(100000,5),(150000,1)],
    },
    "gold": {
        "name": "🥇 Золотой кейс", "price": 20,     # 20 USDT
        "prizes": [(10000,35),(30000,28),(80000,20),(200000,11),(500000,5),(1000000,1)],
    },
    "diamond": {
        "name": "💎 Алмазный кейс", "price": 100,   # 100 USDT
        "prizes": [(50000,38),(150000,28),(400000,20),(1000000,10),(2500000,3),(5000000,1)],
    },
}


def _open_case(case_key: str) -> int:
    prizes = CASES[case_key]["prizes"]
    total  = sum(w for _, w in prizes)
    r = random.randint(1, total)
    cumul = 0
    for amount, weight in prizes:
        cumul += weight
        if r <= cumul:
            return amount
    return prizes[0][0]


@dp.message(Command("case", ignore_mention=True))
@ensure_registered
@game_cooldown("case")
async def cmd_case(message: Message):
    args = message.text.split()
    uid  = message.from_user.id
    user = db.get_user(uid)

    if len(args) < 2:
        lines = ["🎁 <b>Кейсы</b>\n"]
        for key, c in CASES.items():
            mn = min(p for p,_ in c["prizes"])
            mx = max(p for p,_ in c["prizes"])
            lines.append(f"{c['name']} — {fmt_coins(c['price'])} 🪙\n  Призы: {fmt_coins(mn)}–{fmt_coins(mx)} 🪙")
        lines.append(f"\n💼 Баланс: {fmt_coins(user['coins'])} 🪙")
        lines.append("\n<b>Использование:</b> /case bronze | silver | gold")
        await message.answer("\n".join(lines), parse_mode="HTML"); return

    key = args[1].lower()
    if key not in CASES:
        await message.answer("❌ Кейс не найден. Доступны: bronze, silver, gold"); return

    case       = CASES[key]
    usdt_price = case["price"]
    user_usdt  = db.get_usdt(uid)

    if user_usdt < usdt_price:
        avail = "\n".join(f"• {c['name']} — {c['price']} USDT" for c in CASES.values())
        await message.answer(
            f"❌ Недостаточно USDT для {case['name']}\n"
            f"Нужно: <b>{usdt_price} USDT</b>, у тебя: <b>{user_usdt} USDT</b>\n\n"
            f"Доступные кейсы:\n{avail}\n\n"
            f"💱 Обменяй монеты: /exchange (1 000 монет = 1 USDT)",
            parse_mode="HTML"
        ); return

    db.update_usdt(uid, -usdt_price)

    # Анимация открытия
    msg = await message.answer(f"🎁 Открываем {case['name']}...\n\n🔒 🔒 🔒")
    await asyncio.sleep(0.8)
    await msg.edit_text(f"🎁 Открываем {case['name']}...\n\n🔓 🔒 🔒")
    await asyncio.sleep(0.8)
    await msg.edit_text(f"🎁 Открываем {case['name']}...\n\n🔓 🔓 🔒")
    await asyncio.sleep(0.8)

    prize = _open_case(key)
    prize = apply_prestige(uid, prize)
    db.update_coins(uid, prize)
    db.add_xp(uid, 30)
    user_after = db.get_user(uid)

    profit = prize - price
    emoji  = "🤑" if profit > 0 else "😔"

    await msg.edit_text(
        f"🎁 <b>{case['name']} открыт!</b>\n\n"
        f"🔓 🔓 🔓\n\n"
        f"{emoji} Приз: <b>{fmt_coins(prize)} 🪙</b>\n"
        f"{'📈' if profit > 0 else '📉'} {'Прибыль' if profit > 0 else 'Убыток'}: "
        f"{'+'if profit>0 else ''}{fmt_coins(profit)} 🪙\n\n"
        f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
        parse_mode="HTML"
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  СТРИКИ  🔥
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Встроены в cmd_daily — при получении бонуса подсчитываем стрик
# Отдельная команда /streak для просмотра

@dp.message(Command("streak", ignore_mention=True))
@ensure_registered
async def cmd_streak(message: Message):
    uid    = message.from_user.id
    streak = db.get_streak(uid)
    bonus  = _streak_bonus(streak)
    next_b = _streak_bonus(streak + 1)

    bars = min(streak, 30)
    bar  = "🔥" * bars + "▱" * (30 - bars)

    milestones = "\n<b>Уровни стрика:</b>\n2д → +500 🪙\n3д → +2 000 🪙\n7д → +5 000 🪙\n14д → +10 000 🪙\n30д → +25 000 🪙\n60д → +50 000 🪙\n100д → +100 000 🪙"

    await message.answer(
        f"🔥 <b>Стрик ежедневных бонусов</b>\n\n"
        f"{bar}\n\n"
        f"📅 Текущий стрик: <b>{streak} дней</b>\n"
        f"💰 Бонус сегодня: <b>+{fmt_coins(bonus)} 🪙</b>\n"
        f"⬆️ Следующий: <b>+{fmt_coins(next_b)} 🪙</b> ({streak+1} дней)"
        f"{milestones}",
        parse_mode="HTML"
    )


def _streak_bonus(streak: int) -> int:
    """Бонус стрика в монетах (начисляется поверх обычного дейли)."""
    if streak >= 100: return 5000
    if streak >= 60:  return 2000
    if streak >= 30:  return 1000
    if streak >= 14:  return 500
    if streak >= 7:   return 200
    if streak >= 3:   return 100
    if streak >= 2:   return 50
    return 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ИГРА МИНЫ  💣
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

mines_sessions: dict[int, dict] = {}

MINES_GRID = 25  # 5x5

def _mines_multiplier(opened: int, mines: int) -> float:
    """Честный мультипликатор на основе теории вероятности."""
    safe  = MINES_GRID - mines
    mult  = 1.0
    for i in range(opened):
        mult *= (MINES_GRID - i) / (safe - i)
    return round(mult * 0.97, 2)  # 3% комиссия казино


def _mines_kb(uid: int, revealed: set, mine_positions: set, exploded=False, cashed_out=False) -> InlineKeyboardMarkup:
    rows = []
    for row in range(5):
        btns = []
        for col in range(5):
            idx = row * 5 + col
            if idx in revealed:
                if idx in mine_positions:
                    btns.append(InlineKeyboardButton(text="💣", callback_data="mines_noop"))
                else:
                    btns.append(InlineKeyboardButton(text="💎", callback_data="mines_noop"))
            elif exploded or cashed_out:
                if idx in mine_positions:
                    btns.append(InlineKeyboardButton(text="💣", callback_data="mines_noop"))
                else:
                    btns.append(InlineKeyboardButton(text="✅", callback_data="mines_noop"))
            else:
                btns.append(InlineKeyboardButton(text="⬜", callback_data=f"mines_open_{uid}_{idx}"))
        rows.append(btns)
    if not exploded and not cashed_out and revealed:
        rows.append([InlineKeyboardButton(
            text=f"💰 Забрать x{_mines_multiplier(len(revealed), mines_sessions[uid]['mines'])}",
            callback_data=f"mines_cashout_{uid}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(Command("mines", ignore_mention=True))
@ensure_registered
@game_cooldown("mines")
async def cmd_mines(message: Message):
    args = message.text.split()
    uid  = message.from_user.id

    if uid in mines_sessions and not mines_sessions[uid].get("done"):
        await message.answer("⚠️ У тебя уже есть активная игра в мины! Заверши её сначала.")
        return

    if len(args) < 2:
        await message.answer(
            "💣 <b>Мины</b>\n\n"
            "Открывай клетки — избегай мин!\n"
            "Каждая открытая клетка увеличивает выигрыш.\n\n"
            "Использование: /mines &lt;ставка&gt; [мины 1-24]\n"
            "Пример: /mines 1000 5\n\n"
            "По умолчанию 3 мины. Больше мин = выше множитель!",
            parse_mode="HTML"
        ); return

    user   = db.get_user(uid)
    bet, err = validate_bet(user, args[1])
    if err:
        await message.answer(err); return

    mines_count = 3
    if len(args) >= 3:
        try:
            mines_count = max(1, min(24, int(args[2])))
        except: pass

    db.update_coins(uid, -bet)

    # Расставляем мины
    mine_pos = set(random.sample(range(MINES_GRID), mines_count))

    mines_sessions[uid] = {
        "bet":      bet,
        "mines":    mines_count,
        "mine_pos": mine_pos,
        "revealed": set(),
        "done":     False,
    }

    kb  = _mines_kb(uid, set(), mine_pos)
    msg = await message.answer(
        f"💣 <b>Мины</b> — {mines_count} мин на поле 5×5\n"
        f"💸 Ставка: {fmt_coins(bet)} 🪙\n"
        f"📈 Множитель: x1.00\n\n"
        f"Открывай клетки ⬜ — избегай 💣!",
        parse_mode="HTML",
        reply_markup=kb
    )
    mines_sessions[uid]["msg"] = msg


@dp.callback_query(F.data.startswith("mines_open_"))
async def cb_mines_open(callback: CallbackQuery):
    parts = callback.data.split("_")
    uid   = int(parts[2])
    idx   = int(parts[3])

    if callback.from_user.id != uid:
        await callback.answer("❌ Это не твоя игра!", show_alert=True); return

    sess = mines_sessions.get(uid)
    if not sess or sess["done"]:
        await callback.answer("Игра завершена!"); return

    sess["revealed"].add(idx)

    if idx in sess["mine_pos"]:
        # Взрыв!
        sess["done"] = True
        db.record_game(uid, False, sess["bet"])
        db.add_xp(uid, 5)
        kb = _mines_kb(uid, sess["revealed"], sess["mine_pos"], exploded=True)
        user_after = db.get_user(uid)
        mines_sessions.pop(uid, None)
        await callback.answer("💥 МИНА!", show_alert=False)
        try:
            await callback.message.edit_text(
                f"💥 <b>МИНА! Взрыв!</b>\n\n"
                f"Потерял: -{fmt_coins(sess['bet'])} 🪙\n"
                f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
                parse_mode="HTML",
                reply_markup=kb
            )
        except: pass
    else:
        opened = len(sess["revealed"])
        mult   = _mines_multiplier(opened, sess["mines"])
        pot    = int(sess["bet"] * mult)
        kb     = _mines_kb(uid, sess["revealed"], sess["mine_pos"])
        await callback.answer(f"💎 Безопасно! x{mult}")
        try:
            await callback.message.edit_text(
                f"💣 <b>Мины</b> — открыто {opened} клеток\n"
                f"💸 Ставка: {fmt_coins(sess['bet'])} 🪙\n"
                f"📈 Множитель: <b>x{mult}</b>\n"
                f"💰 Сейчас получишь: <b>{fmt_coins(pot)} 🪙</b>\n\n"
                f"Продолжай или забирай!",
                parse_mode="HTML",
                reply_markup=kb
            )
        except: pass


@dp.callback_query(F.data.startswith("mines_cashout_"))
async def cb_mines_cashout(callback: CallbackQuery):
    uid  = int(callback.data.replace("mines_cashout_", ""))
    if callback.from_user.id != uid:
        await callback.answer("❌ Не твоя игра!", show_alert=True); return

    sess = mines_sessions.get(uid)
    if not sess or sess["done"]:
        await callback.answer("Игра уже завершена!"); return

    opened = len(sess["revealed"])
    if opened == 0:
        await callback.answer("Сначала открой хотя бы одну клетку!", show_alert=True); return

    sess["done"] = True
    mult   = _mines_multiplier(opened, sess["mines"])
    payout = int(sess["bet"] * mult)

    db.update_coins(uid, payout)
    db.record_game(uid, True, sess["bet"])
    db.add_xp(uid, sess["bet"] // 10 + 20)
    db.update_task_progress(uid, "win3")

    user_after = db.get_user(uid)
    kb = _mines_kb(uid, sess["revealed"], sess["mine_pos"], cashed_out=True)
    mines_sessions.pop(uid, None)

    await callback.answer(f"✅ Забрал x{mult}!", show_alert=False)
    try:
        await callback.message.edit_text(
            f"✅ <b>Забрал выигрыш!</b>\n\n"
            f"💎 Открыто клеток: {opened}\n"
            f"📈 Множитель: x{mult}\n"
            f"💰 Выплата: {fmt_coins(payout)} 🪙  (+{fmt_coins(payout-sess['bet'])})\n\n"
            f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
            parse_mode="HTML",
            reply_markup=kb
        )
    except: pass


@dp.callback_query(F.data == "mines_noop")
async def cb_mines_noop(callback: CallbackQuery):
    await callback.answer()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ТУРНИР  🏆
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dp.message(Command("tournament", ignore_mention=True))
@ensure_registered
async def cmd_tournament(message: Message):
    uid    = message.from_user.id
    top    = db.get_tournament_top(10)
    my_pos = db.get_tournament_position(uid)
    my_pts = db.get_tournament_points(uid)

    prizes = {1: 5000, 2: 2500, 3: 1000, 4: 500, 5: 250}
    ends   = db.get_tournament_end()
    now    = int(time.time())
    left   = max(0, ends - now)
    d, rem = divmod(left, 86400)
    h, _   = divmod(rem, 3600)

    lines = [
        f"🏆 <b>Еженедельный турнир</b>\n",
        f"⏱ До конца: <b>{d}д {h}ч</b>\n",
        f"{'─'*24}\n",
        f"<b>Призы:</b>\n",
        f"🥇 1 место — {fmt_coins(prizes[1])} 🪙\n",
        f"🥈 2 место — {fmt_coins(prizes[2])} 🪙\n",
        f"🥉 3 место — {fmt_coins(prizes[3])} 🪙\n",
        f"4-5 место — {fmt_coins(prizes[4])}-{fmt_coins(prizes[5])} 🪙\n",
        f"{'─'*24}\n",
        f"<b>Топ-10:</b>\n",
    ]
    medals = ["🥇","🥈","🥉","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
    for i, row in enumerate(top):
        me = " ← ты" if row["user_id"] == uid else ""
        lines.append(f"{medals[i]} {row['full_name']}: {fmt_coins(row['points'])} очков{me}\n")

    if my_pos and my_pos > 10:
        lines.append(f"\n👤 Твоя позиция: #{my_pos} ({fmt_coins(my_pts)} очков)")
    elif not top:
        lines.append("\n<i>Ещё никто не участвует. Будь первым!</i>")

    lines.append("\n<i>Очки начисляются за выигрыши. 1 монета выигрыша = 1 очко.</i>")

    await message.answer("".join(lines), parse_mode="HTML")


# Фоновая задача — раз в неделю подводить итоги турнира
async def tournament_checker():
    while True:
        await asyncio.sleep(3600)  # каждый час проверяем
        try:
            ends = db.get_tournament_end()
            if int(time.time()) >= ends:
                await _finish_tournament()
        except: pass


async def _finish_tournament():
    top = db.get_tournament_top(5)
    prizes = {0: 5000, 1: 2500, 2: 1000, 3: 500, 4: 250}
    for i, row in enumerate(top):
        prize = prizes.get(i, 0)
        if prize:
            db.update_coins(row["user_id"], prize)
            try:
                await bot.send_message(
                    row["user_id"],
                    f"🏆 <b>Турнир завершён!</b>\n\n"
                    f"Ты занял <b>#{i+1} место</b>!\n"
                    f"Приз: <b>{fmt_coins(prize)} 🪙</b>",
                    parse_mode="HTML"
                )
            except: pass
    db.reset_tournament()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  РУССКИЕ КЛЮЧЕВЫЕ СЛОВА
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Простая таблица: если сообщение НАЧИНАЕТСЯ с ключевого слова → действие
_KW = [
    # игры
    (("слоты","слот","барабан","крути","🎰"),          "slots"),
    (("кости","кубик","бросай","кидай","кинь"),        "dice"),
    (("рулетка","рулетку","колесо"),                   "roulette"),
    (("карты","блэкджек","блек","двадцать один","21"),  "blackjack"),
    (("комната","bjroom","блэк комната"),               "bjroom"),
    (("краш","крэш","ракета"),                         "crash"),
    # меню
    (("баланс","бабки","счёт","деньги"),               "balance"),
    (("профиль","стата","статистика","инфо"),           "profile"),
    (("бонус","дейли","ежедневный","дай бонус"),        "daily"),
    (("задания","задание","квест","таски"),             "tasks"),
    (("топ","рейтинг","лидеры"),                       "top"),
    (("помощь","справка","хелп"),                      "help"),
    (("магазин","донат","купить","звёзды"),             "shop"),
    (("меню","казино","игры","привет","старт","хай"),   "menu"),
    (("мины","mines","бомба","сапёр"),                  "mines"),
    (("кейс","case","ящик","открыть кейс"),             "case"),
    (("банк","bank","вклад","депозит"),                 "bank"),
    (("перевод","отправить","send","передать"),         "send"),
    (("рефер","реферал","ref","пригласить"),            "ref"),
    (("турнир","tournament","соревнование"),            "tournament"),
    (("стрик","streak","серия"),                        "streak"),
    (("промо","промокод","promo","активировать"),        "promo"),
]


@dp.message(lambda m: m.text and m.text.strip().isdigit() and m.from_user.id in guess_sessions)
async def guess_answer(message: Message):
    uid = message.from_user.id
    sess = guess_sessions.get(uid)
    if not sess: return

    try:
        guess = int(message.text.strip())
    except:
        return

    if not 1 <= guess <= 100:
        await message.answer("Число должно быть от 1 до 100!"); return

    sess["attempts"] += 1
    number = sess["number"]
    attempts = sess["attempts"]

    if guess == number:
        bet = sess["bet"]
        mult = {1: 5.0, 2: 3.0}.get(attempts, 1.5)
        payout = apply_prestige(uid, int(bet * mult))
        db.update_coins(uid, payout)
        db.record_game(uid, True, payout)
        del guess_sessions[uid]
        await message.answer(
            f"🎉 <b>Угадал!</b> Число было {number}\n"
            f"Попытка #{attempts} → x{mult} → +{fmt_coins(payout)} 🪙",
            parse_mode="HTML"
        )
    elif attempts >= sess["max"]:
        db.record_game(uid, False, sess["bet"])
        del guess_sessions[uid]
        await message.answer(f"💀 Попытки кончились. Число было <b>{number}</b>. -{fmt_coins(sess['bet'])} 🪙", parse_mode="HTML")
    else:
        hint = "🔥 Горячо!" if abs(guess - number) <= 5 else ("♨️ Тепло" if abs(guess - number) <= 15 else "🧊 Холодно")
        direction = "📈 Больше" if guess < number else "📉 Меньше"
        left = sess["max"] - attempts
        await message.answer(f"{hint} | {direction} | Осталось попыток: {left}")


@dp.message(Command("mathgame", ignore_mention=True))
@ensure_registered
@game_cooldown("math")
async def cmd_mathgame(message: Message):
    """Реши пример быстро — получи награду."""
    uid = message.from_user.id
    args = message.text.split()
    user = db.get_user(uid)
    bet, err = validate_bet(user, args[1] if len(args) > 1 else "")
    if bet is None:
        await message.answer(err); return

    # Генерация примера
    ops = ["+", "-", "*"]
    op = random.choice(ops)
    if op == "*":
        a, b2 = random.randint(2, 12), random.randint(2, 12)
    else:
        a, b2 = random.randint(10, 99), random.randint(10, 99)
    answer = eval(f"{a}{op}{b2}")
    expr = f"{a} {op} {b2}"

    db.update_coins(uid, -bet)
    math_sessions[uid] = {"bet": bet, "answer": answer, "start": time.time()}
    await message.answer(
        f"🔢 <b>Математика</b>\n\n"
        f"Сколько будет: <b>{expr} = ?</b>\n\n"
        f"Ставка: {fmt_coins(bet)} 🪙\n"
        f"⚡ Быстрее — больше множитель! (<10с → x2, <20с → x1.5, иначе x1.2)\n\n"
        f"Введи ответ числом:",
        parse_mode="HTML"
    )


@dp.message(lambda m: m.text and m.text.strip().lstrip("-").isdigit() and m.from_user.id in math_sessions)
async def math_answer(message: Message):
    uid = message.from_user.id
    sess = math_sessions.get(uid)
    if not sess: return

    try:
        ans = int(message.text.strip())
    except:
        return

    elapsed = time.time() - sess["start"]
    correct = sess["answer"]
    bet = sess["bet"]
    del math_sessions[uid]

    if ans == correct:
        mult = 2.0 if elapsed < 10 else (1.5 if elapsed < 20 else 1.2)
        payout = apply_prestige(uid, int(bet * mult))
        db.update_coins(uid, payout)
        db.record_game(uid, True, payout)
        await message.answer(
            f"✅ <b>Правильно!</b> ({correct})\n"
            f"⏱ {elapsed:.1f}с → x{mult} → +{fmt_coins(payout)} 🪙",
            parse_mode="HTML"
        )
    else:
        db.record_game(uid, False, bet)
        await message.answer(
            f"❌ Неверно. Правильный ответ: <b>{correct}</b>\n"
            f"Потерял {fmt_coins(bet)} 🪙",
            parse_mode="HTML"
        )


async def usdt_rate_checker():
    """Каждый день с 5% шансом курс USDT меняется на ±5-20%."""
    await asyncio.sleep(5)
    while True:
        now = datetime.now()
        # Ждём следующего дня 12:00
        next_run = now.replace(hour=12, minute=0, second=0, microsecond=0)
        if next_run <= now:
            next_run = next_run.replace(day=now.day + 1)
        await asyncio.sleep((next_run - now).total_seconds())

        if random.random() < 0.05:  # 5% шанс
            direction = random.choice([-1, 1])
            pct = random.randint(5, 20)
            base = 1000
            change = int(base * pct / 100) * direction
            new_rate = max(500, min(2000, base + change))
            db.set_usdt_rate(new_rate)
            diff_str = f"+{change}" if change > 0 else str(change)
            # Уведомляем всех активных игроков
            uids = db.get_all_user_ids()
            emoji = "📈" if direction > 0 else "📉"
            for uid in uids:
                try:
                    await bot.send_message(uid,
                        f"{emoji} <b>Курс USDT изменился!</b>\n\n"
                        f"1 USDT теперь = <b>{new_rate} 🪙</b> ({diff_str} монет)\n"
                        f"💱 /exchange — обменять",
                        parse_mode="HTML")
                    await asyncio.sleep(0.05)
                except: pass


async def on_startup():
    db.init_db()
    print("✅ База данных инициализирована")

    # ── Регистрируем меню команд (то самое всплывающее меню "/" в Telegram) ──
    from aiogram.types import BotCommand, BotCommandScopeDefault, BotCommandScopeChat
    from aiogram.exceptions import TelegramBadRequest

    # Команды для обычных пользователей
    user_commands = [
        BotCommand(command="start",      description="🏠 Главное меню"),
        BotCommand(command="slots",      description="🎰 Слоты (с анимацией)"),
        BotCommand(command="dice",       description="🎲 Кости — угадай больше"),
        BotCommand(command="roulette",   description="🎡 Рулетка red/black"),
        BotCommand(command="blackjack",  description="🃏 Блэкджек против казино"),
        BotCommand(command="bjroom",     description="🃏👥 Создать комнату блэкджека"),
        BotCommand(command="bjjoin",     description="🃏 Войти в комнату по коду"),
        BotCommand(command="bjleave",    description="🚪 Выйти из комнаты"),
        BotCommand(command="crash",      description="🚀 Краш — не упусти момент"),
        BotCommand(command="profile",    description="👤 Мой профиль"),
        BotCommand(command="balance",    description="💰 Текущий баланс"),
        BotCommand(command="daily",      description="🎁 Ежедневный бонус"),
        BotCommand(command="tasks",      description="📋 Ежедневные задания"),
        BotCommand(command="top",        description="🏆 Топ-10 игроков"),
        BotCommand(command="donate",     description="⭐ Магазин Stars"),
        BotCommand(command="help",       description="❓ Помощь по командам"),
        BotCommand(command="promo",      description="🎟 Активировать промокод"),
        BotCommand(command="ref",        description="👫 Реферальная программа"),
        BotCommand(command="send",       description="💸 Перевести монеты игроку"),
        BotCommand(command="bank",       description="🏦 Вклад под проценты"),
        BotCommand(command="case",       description="🎁 Открыть кейс"),
        BotCommand(command="mines",      description="💣 Игра Мины"),
        BotCommand(command="streak",     description="🔥 Мой стрик"),
        BotCommand(command="tournament", description="🏆 Еженедельный турнир"),
        BotCommand(command="exchange",     description="💱 Обмен монет в USDT"),
        BotCommand(command="prestige",     description="👑 Статус за USDT"),
        BotCommand(command="usdt",         description="💵 Курс USDT"),
        BotCommand(command="reaction",     description="⚡ Реакция"),
        BotCommand(command="rps",          description="✂️ КНБ"),
        BotCommand(command="guess",        description="🧠 Угадай число"),
        BotCommand(command="mathgame",     description="🔢 Математика"),
        BotCommand(command="notify",     description="🔔 Уведомления о бонусе"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    # Дополнительные команды для каждого из админов
    admin_extra = user_commands + [
        BotCommand(command="admin",      description="👑 Админ-панель"),
        BotCommand(command="moder",      description="🛡 Панель модератора"),
    ]
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(
                admin_extra,
                scope=BotCommandScopeChat(chat_id=admin_id)
            )
        except TelegramBadRequest:
            pass   # если админ ещё ни разу не писал боту — пропускаем

    print("✅ Меню команд зарегистрировано")
    print("🤖 Бот запущен!")


async def vip_checker():
    """Фоновая задача: каждый час снимает истёкший VIP."""
    while True:
        await asyncio.sleep(3600)
        db.check_vip_expired()


async def main():
    import os
    await on_startup()
    asyncio.create_task(vip_checker())
    asyncio.create_task(daily_notifier())
    asyncio.create_task(bank_checker())
    asyncio.create_task(usdt_rate_checker())
    asyncio.create_task(tournament_checker())

    webhook_url = os.environ.get("WEBHOOK_URL", "")

    if webhook_url:
        # ── WEBHOOK режим (Railway / production) ──
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
        WEBHOOK_PATH = "/webhook"
        await bot.set_webhook(
            url=f"{webhook_url}{WEBHOOK_PATH}",
            drop_pending_updates=True
        )
        print(f"🌐 Webhook: {webhook_url}{WEBHOOK_PATH}")

        app = web.Application()

        # Веб-панель
        if os.environ.get("WEB_ADMIN") == "1":
            app.router.add_get("/admin",        web_admin_handler)
            app.router.add_get("/admin/action", web_action_handler)
            app.router.add_get("/",             web_admin_handler)

        # Webhook handler — без secret_token
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)

        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        await web.TCPSite(runner, "0.0.0.0", port).start()
        print(f"🚀 Webhook сервер запущен на порту {port}")
        await asyncio.Event().wait()  # держим процесс живым
    else:
        # ── POLLING режим (локальная разработка) ──
        await bot.delete_webhook(drop_pending_updates=True)
        print("🚀 Запуск polling (локально)...")
        await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())


@dp.message(F.text)
async def keyword_handler(message: Message, state: FSMContext):
    txt = (message.text or "").strip()

    # пропускаем команды
    if txt.startswith("/"):
        return

    # пропускаем если юзер в FSM диалоге (например ввод для /admin)
    cur_state = await state.get_state()
    if cur_state is not None:
        return

    # определяем действие
    t      = txt.lower()
    parts  = t.split()
    action = None
    for keywords, act in _KW:
        for kw in keywords:
            if t == kw or t.startswith(kw + " "):
                action = act
                break
        if action:
            break

    if not action:
        # Авто-проверка: вдруг написали промокод напрямую (4-16 символов, только буквы и цифры)
        if txt.replace("-","").replace("_","").isalnum() and 4 <= len(txt) <= 16:
            db.register_user(u.id, u.username, u.full_name)
            res = db.use_promo(uid, txt.upper())
            if res["ok"]:
                parts_r = []
                if res["coins"]:    parts_r.append(f"💰 +{fmt_coins(res['coins'])} монет")
                if res["vip_days"]: parts_r.append(f"⭐ VIP на {res['vip_days']} дней")
                reward_text = "\n".join(parts_r) or "🎁 Бонус активирован"
                user_after  = db.get_user(uid)
                await message.answer(
                    f"✅ <b>Промокод {txt.upper()} активирован!</b>\n\n"
                    f"{reward_text}\n\n"
                    f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
                    parse_mode="HTML"
                )
        return

    # регистрируем
    u = message.from_user
    db.register_user(u.id, u.username, u.full_name)
    user = db.get_user(u.id)

    # ставка — первое число в тексте
    bet_str = next((p for p in parts if p.isdigit()), None)

    # ── ИГРЫ ──────────────────────────────────────────────
    if action in ("slots", "dice", "roulette", "blackjack", "crash"):
        if not bet_str:
            hints = {
                "slots":     "слоты 100",
                "dice":      "кости 100",
                "roulette":  "рулетка 100",
                "blackjack": "карты 100",
                "crash":     "краш 100",
            }
            await message.answer(f"💬 Укажи ставку, например: <code>{hints[action]}</code>", parse_mode="HTML")
            return

        if action == "slots":
            message.text = f"/slots {bet_str}"
            await cmd_slots(message)
        elif action == "dice":
            message.text = f"/dice {bet_str}"
            await cmd_dice(message)
        elif action == "roulette":
            color = "black" if any(w in t for w in ("чёрн","черн","black")) else "red"
            message.text = f"/roulette {color} {bet_str}"
            await cmd_roulette(message)
        elif action == "blackjack":
            message.text = f"/blackjack {bet_str}"
            await cmd_blackjack(message)
        elif action == "bjroom":
            message.text = f"/bjroom {bet_str}"
            await cmd_bjroom(message)
        elif action == "crash":
            message.text = f"/crash {bet_str}"
            await cmd_crash(message)
        return

    # ── ОСТАЛЬНОЕ ─────────────────────────────────────────
    if action == "balance":
        await message.answer(f"💰 Баланс: <b>{fmt_coins(user['coins'])} 🪙</b>", parse_mode="HTML")
    elif action == "profile":
        await cmd_profile(message)
    elif action == "daily":
        await cmd_daily(message)
    elif action == "tasks":
        await cmd_tasks(message)
    elif action == "top":
        await cmd_top(message)
    elif action == "help":
        await cmd_help(message)
    elif action == "promo":
        # Если после слова "промокод" сразу написан код — активируем
        parts2 = txt.split()
        code_candidate = next((p.upper() for p in parts2 if p.upper() not in 
            ("ПРОМО","ПРОМОКОД","PROMO","АКТИВИРОВАТЬ")), None)
        if code_candidate:
            res = db.use_promo(uid, code_candidate)
            if res["ok"]:
                parts_r = []
                if res["coins"]:    parts_r.append(f"💰 +{fmt_coins(res['coins'])} монет")
                if res["vip_days"]: parts_r.append(f"⭐ VIP на {res['vip_days']} дней")
                reward_text = "\n".join(parts_r) or "🎁 Бонус активирован"
                user_after  = db.get_user(uid)
                await message.answer(
                    f"✅ <b>Промокод активирован!</b>\n\n{reward_text}\n\n"
                    f"💼 Баланс: {fmt_coins(user_after['coins'])} 🪙",
                    parse_mode="HTML"
                )
            else:
                await message.answer(f"❌ {res['err']}", parse_mode="HTML")
        else:
            await message.answer("🎟 Напиши: <code>промокод КОД</code>\nНапример: <code>промокод TEST</code>", parse_mode="HTML")
    elif action == "shop":
        t2 = "⭐ <b>Магазин Stars</b>\n\n"
        for item in config.SHOP_ITEMS.values():
            t2 += f"• {item['title']} — ⭐{item['stars']}\n  <i>{item['desc']}</i>\n\n"
        await message.answer(t2, reply_markup=shop_keyboard(), parse_mode="HTML")
    elif action == "mines":
        if bet_str:
            message.text = f"/mines {bet_str}"
        else:
            message.text = "/mines"
        await cmd_mines(message)
        return
    elif action == "case":
        message.text = f"/case {bet_str or 'bronze'}"
        await cmd_case(message)
        return
    elif action == "bank":
        message.text = "/bank"
        await cmd_bank(message)
        return
    elif action == "send":
        await message.answer("💸 Используй: /send @username сумма", parse_mode="HTML")
        return
    elif action == "ref":
        await cmd_ref(message)
        return
    elif action == "tournament":
        await cmd_tournament(message)
        return
    elif action == "streak":
        await cmd_streak(message)
        return
    elif action == "menu":
        bot_info = await bot.get_me()
        vip = "⭐ VIP" if user["is_vip"] else ""
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="➕ Добавить в группу (с правами админа)",
                url=f"https://t.me/{bot_info.username}?startgroup=true&admin=change_info+delete_messages+restrict_members+invite_users+pin_messages+manage_video_chats+manage_chat"
            )],
            [InlineKeyboardButton(text="🎮 Быстрая игра", callback_data="quick_play"),
             InlineKeyboardButton(text="⭐ Магазин",      callback_data="open_shop")]
        ])
        await message.answer(
            f"🎰 <b>Casino Bot</b> {vip}\n"
            f"💰 Баланс: <b>{fmt_coins(user['coins'])} 🪙</b>\n\n"
            "<b>Ключевые слова:</b>\n"
            "🎰 <code>слоты 100</code>  🎲 <code>кости 100</code>\n"
            "🎡 <code>рулетка 100</code>  🃏 <code>карты 100</code>\n"
            "🚀 <code>краш 100</code>\n\n"
            "💰 <code>баланс</code>  👤 <code>профиль</code>\n"
            "🎁 <code>бонус</code>  📋 <code>задания</code>\n"
            "🏆 <code>топ</code>  🛒 <code>магазин</code>",
            parse_mode="HTML", reply_markup=kb
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  INLINE-РЕЖИМ  @бот в любом чате
#  Активировать: @BotFather → /setinline
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dp.inline_query()
async def inline_handler(query: InlineQuery):
    uid  = query.from_user.id
    db.register_user(uid, query.from_user.username, query.from_user.full_name)
    user = db.get_user(uid)
    q    = query.query.strip().lower()

    results = []

    # ── Профиль ────────────────────────────────
    lname = config.LEVEL_NAMES.get(user["level"], "")
    vip   = "⭐ VIP" if user["is_vip"] else ""
    total = user["wins"] + user["losses"]
    wr    = f"{user['wins']/total*100:.1f}%" if total else "—"

    results.append(InlineQueryResultArticle(
        id="profile",
        title="👤 Мой профиль",
        description=f"Ур.{user['level']} | 💰 {fmt_coins(user['coins'])} | WR {wr}",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"👤 <b>Профиль игрока {query.from_user.full_name}</b> {vip}\n"
                f"{'─'*28}\n"
                f"🎖 Уровень: {user['level']} {lname}\n"
                f"🪙 Монеты: <b>{fmt_coins(user['coins'])}</b>\n"
                f"🏆 Побед: {user['wins']} | 💀 Поражений: {user['losses']}\n"
                f"📈 Winrate: {wr}\n"
                f"💸 Поставлено всего: {fmt_coins(user['total_bet'])} 🪙"
            ),
            parse_mode="HTML"
        )
    ))

    # ── Баланс ─────────────────────────────────
    results.append(InlineQueryResultArticle(
        id="balance",
        title="💰 Показать баланс",
        description=f"{fmt_coins(user['coins'])} монет",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"💰 У <b>{query.from_user.full_name}</b> на счету:\n"
                f"<b>{fmt_coins(user['coins'])} 🪙</b>"
            ),
            parse_mode="HTML"
        )
    ))

    # ── Топ-5 ──────────────────────────────────
    top_rows = db.get_top(5)
    top_text = "🏆 <b>Топ-5 игроков Casino Bot</b>\n\n"
    medals   = ["🥇","🥈","🥉","🔸","🔸"]
    for i, r in enumerate(top_rows):
        top_text += f"{medals[i]} <b>{r['full_name']}</b> — {fmt_coins(r['coins'])} 🪙 | Ур.{r['level']}\n"

    results.append(InlineQueryResultArticle(
        id="top",
        title="🏆 Топ-5 игроков",
        description="Показать рейтинг в чате",
        input_message_content=InputTextMessageContent(
            message_text=top_text,
            parse_mode="HTML"
        )
    ))

    # ── Последний выигрыш (симуляция слотов) ────
    symbols = config.SLOT_SYMBOLS
    weights = config.SLOT_WEIGHTS
    s1 = random.choices(symbols, weights=weights, k=1)[0]
    s2 = random.choices(symbols, weights=weights, k=1)[0]
    s3 = random.choices(symbols, weights=weights, k=1)[0]

    results.append(InlineQueryResultArticle(
        id="slots_demo",
        title="🎰 Показать прокрутку слотов",
        description=f"Демо: {s1} {s2} {s3}",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"🎰 <b>{query.from_user.full_name}</b> крутит барабаны!\n\n"
                f"┌──────────────────┐\n"
                f"│  {s1}    {s2}    {s3}  │\n"
                f"└──────────────────┘\n\n"
                f"💬 Хочешь сыграть? Напиши боту: @{(await bot.get_me()).username}"
            ),
            parse_mode="HTML"
        )
    ))

    # ── Пригласить играть ───────────────────────
    bot_info = await bot.get_me()
    results.append(InlineQueryResultArticle(
        id="invite",
        title="🎲 Пригласить играть в казино",
        description="Отправить приглашение в чат",
        input_message_content=InputTextMessageContent(
            message_text=(
                f"🎰 <b>Казино-бот — играй прямо в Telegram!</b>\n\n"
                f"🎮 Слоты, Кости, Рулетка, Блэкджек, Краш\n"
                f"💰 Ежедневные бонусы и задания\n"
                f"🏆 Рейтинг игроков\n"
                f"⭐ VIP и магазин Stars\n\n"
                f"👉 @{bot_info.username}"
            ),
            parse_mode="HTML"
        )
    ))

    await query.answer(results, cache_time=30, is_personal=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  УВЕДОМЛЕНИЯ О БОНУСЕ  🔔
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def daily_notifier():
    """Фоновая задача: каждый час проверяет у кого готов бонус и шлёт уведомление."""
    from datetime import date
    while True:
        await asyncio.sleep(3600)
        try:
            uids = db.get_all_user_ids()
            today = str(date.today())
            for uid in uids:
                # Пропускаем если уведомления выключены
                if db.get_setting(f"notify_{uid}") == "off":
                    continue
                user = db.get_user(uid)
                if not user:
                    continue
                # Если сегодня ещё не получал бонус — напомнить
                if user["daily_last"] != today:
                    # Не спамить — ставим флаг что уже напомнили сегодня
                    notif_key = f"notif_sent_{uid}_{today}"
                    if db.get_setting(notif_key):
                        continue
                    db.set_setting(notif_key, "1")
                    try:
                        bonus = config.DAILY_BONUS * 2 if user["is_vip"] else config.DAILY_BONUS
                        await bot.send_message(
                            uid,
                            f"🔔 <b>Ежедневный бонус готов!</b>\n\n"
                            f"Напиши <code>бонус</code> или /daily\n"
                            f"и получи <b>{fmt_coins(bonus)} 🪙</b>!",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception:
            pass


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ВЕБ-ПАНЕЛЬ АДМИНА  🌐  (полное управление)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

WEB_PASSWORD = "casino_admin_2024"
WEB_PORT     = int(__import__("os").environ.get("PORT", 8080))

_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0a0a14; color: #e0e0e0; }
.sidebar { position: fixed; left: 0; top: 0; width: 220px; height: 100vh; background: #111122; border-right: 1px solid #222240; padding: 20px 0; z-index: 10; overflow-y: auto; }
.sidebar h2 { color: #ffd700; font-size: 17px; padding: 0 20px 18px; border-bottom: 1px solid #222240; }
.sidebar a { display: block; padding: 11px 20px; color: #aaa; text-decoration: none; font-size: 14px; }
.sidebar a:hover,.sidebar a.active { background: #1a1a2e; color: #ffd700; border-left: 3px solid #ffd700; padding-left: 17px; }
.main { margin-left: 220px; padding: 30px; min-height: 100vh; }
h1 { color: #ffd700; font-size: 24px; margin-bottom: 6px; }
.sub { color: #666; font-size: 13px; margin-bottom: 24px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px,1fr)); gap: 12px; margin-bottom: 28px; }
.card { background: #111122; border-radius: 12px; padding: 16px; border: 1px solid #222240; }
.card .val { font-size: 26px; font-weight: bold; color: #ffd700; margin-top: 6px; }
.card .lbl { font-size: 12px; color: #777; }
.section { background: #111122; border-radius: 12px; border: 1px solid #222240; margin-bottom: 22px; overflow: hidden; }
.sh { padding: 14px 20px; background: #16162a; border-bottom: 1px solid #222240; font-weight: 600; color: #ffd700; font-size: 14px; }
.sb { padding: 20px; }
table { width: 100%; border-collapse: collapse; }
th { padding: 9px 12px; text-align: left; color: #777; font-size: 11px; text-transform: uppercase; letter-spacing: .5px; border-bottom: 1px solid #1e1e30; }
td { padding: 10px 12px; border-bottom: 1px solid #16162a; font-size: 13px; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #13132a; }
.bv { display:inline-block; padding:2px 7px; border-radius:8px; font-size:11px; background:#2a2a00; color:#ffd700; }
input[type=text],input[type=number],input[type=password],select,textarea {
  background:#0a0a14; border:1px solid #333360; color:#e0e0e0;
  padding:10px 13px; border-radius:8px; font-size:14px; width:100%; margin-bottom:12px; outline:none; }
input:focus,select:focus,textarea:focus { border-color:#ffd700; }
.btn { display:inline-block; padding:10px 22px; border-radius:8px; font-size:14px; font-weight:600; cursor:pointer; border:none; }
.btn-gold { background:#ffd700; color:#000; } .btn-red { background:#c0392b; color:#fff; }
.btn-green { background:#27ae60; color:#fff; } .btn-blue { background:#2980b9; color:#fff; }
.r2 { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
.alert { padding:11px 15px; border-radius:8px; margin-bottom:16px; font-size:13px; }
.a-ok { background:#0a2a10; border:1px solid #0a0; color:#0d0; }
.a-err { background:#2a0a0a; border:1px solid #a00; color:#f88; }
.cr { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.cr label { width:130px; font-size:13px; color:#aaa; }
.cr input { width:80px; margin:0; }
.cr span { color:#ffd700; font-size:13px; }
@media(max-width:680px){.sidebar{display:none}.main{margin-left:0}.r2{grid-template-columns:1fr}}
"""

def _auth_page(error=""):
    e = f'<div class="alert a-err">{error}</div>' if error else ""
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Casino Admin</title>
<style>body{{background:#0a0a14;color:#e0e0e0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh}}
.box{{background:#111122;border:1px solid #222240;border-radius:16px;padding:40px;width:340px;text-align:center}}
h2{{color:#ffd700;margin-bottom:24px}}
input{{background:#0a0a14;border:1px solid #333360;color:#e0e0e0;padding:12px;border-radius:8px;font-size:15px;width:100%;margin-bottom:14px;outline:none}}
button{{background:#ffd700;color:#000;padding:12px;border-radius:8px;font-size:15px;font-weight:700;width:100%;border:none;cursor:pointer}}</style></head>
<body><div class="box"><h2>🔒 Casino Admin</h2>{e}
<form method="GET" action="/admin"><input type="password" name="pass" placeholder="Пароль" autofocus>
<button type="submit">Войти</button></form></div></body></html>"""


def _page(sidebar_html, body_html):
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Casino Admin</title>
<style>{_CSS}</style></head><body>{sidebar_html}<div class="main">{body_html}</div></body></html>"""


def _sidebar(pwd, active):
    links = [("📊","Статистика","stats"),("🔍","Поиск","search"),
             ("👥","Игроки","players"),("💰","Монеты","coins"),
             ("⭐","VIP","vip"),("💬","Сообщение","message"),
             ("🎲","Шансы","chances"),("📢","Рассылка","broadcast"),
             ("🎟","Промокоды","promos")]
    s = '<div class="sidebar"><h2>🎰 Casino Admin</h2>'
    for icon, label, tab in links:
        cls = ' class="active"' if tab == active else ""
        s += f'<a href="/admin?pass={pwd}&tab={tab}"{cls}>{icon} {label}</a>'
    return s + "</div>"


async def web_admin_handler(request: web.Request):
    pwd = request.rel_url.query.get("pass", "")
    if pwd != WEB_PASSWORD:
        return web.Response(text=_auth_page("" if not pwd else "❌ Неверный пароль"), content_type="text/html")

    tab = request.rel_url.query.get("tab", "stats")
    msg = request.rel_url.query.get("msg", "")
    err = request.rel_url.query.get("err", "")
    s   = db.get_stats()
    tg  = s["total_wins"] + s["total_losses"]
    wr  = f"{s['total_wins']/tg*100:.1f}%" if tg else "—"
    sb  = _sidebar(pwd, tab)
    alert = (f'<div class="alert a-ok">✅ {msg}</div>' if msg else "") + (f'<div class="alert a-err">❌ {err}</div>' if err else "")

    if tab == "stats":
        top = db.get_top(10)
        tr  = "".join(f"<tr><td>{i+1}</td><td>{r['full_name']}</td>"
                      f"<td>{'<span class=\"bv\">⭐</span>' if db.get_user(r['user_id'])['is_vip'] else '—'}</td>"
                      f"<td>{fmt_coins(r['coins'])} 🪙</td><td>{r['level']}</td><td>{r['wins']}</td></tr>"
                      for i, r in enumerate(top))
        body = f"""<h1>📊 Статистика</h1><p class="sub">🔄 {datetime.now().strftime("%d.%m.%Y %H:%M:%S")} — <a href="/admin?pass={pwd}&tab=stats" style="color:#ffd700">обновить</a></p>
        <div class="grid">
          <div class="card"><div class="lbl">👥 Игроков</div><div class="val">{s["total_users"]}</div></div>
          <div class="card"><div class="lbl">🆕 Новых сегодня</div><div class="val">{s["new_today"]}</div></div>
          <div class="card"><div class="lbl">⭐ VIP</div><div class="val">{s["vip_count"]}</div></div>
          <div class="card"><div class="lbl">🪙 Монет</div><div class="val">{fmt_coins(s["total_coins"])}</div></div>
          <div class="card"><div class="lbl">🎮 Игр сыграно</div><div class="val">{tg}</div></div>
          <div class="card"><div class="lbl">📈 Winrate</div><div class="val">{wr}</div></div>
          <div class="card"><div class="lbl">🏆 Побед</div><div class="val">{s["total_wins"]}</div></div>
          <div class="card"><div class="lbl">💀 Поражений</div><div class="val">{s["total_losses"]}</div></div>
          <div class="card"><div class="lbl">🏦 В банке</div><div class="val">{fmt_coins(db.get_bank_total())} 🪙</div></div>
          <div class="card"><div class="lbl">👫 Рефералов</div><div class="val">{db.get_referral_count()}</div></div>
          <div class="card"><div class="lbl">🎟 Промо активировано</div><div class="val">{db.get_promo_total_uses()}</div></div>
        </div>
        <div class="section"><div class="sh">🏆 Топ-10</div><div class="sb" style="padding:0">
        <table><tr><th>#</th><th>Игрок</th><th>VIP</th><th>Монеты</th><th>Ур.</th><th>Победы</th></tr>{tr}</table>
        </div></div>"""

    elif tab == "search":
        q2     = request.rel_url.query.get("q", "").strip()
        found  = db.search_users(q2) if q2 else []
        res_rows = ""
        for u in found:
            vb  = "⭐" if u["is_vip"] else "—"
            uid_val = u["user_id"]
            ban_url = f"/admin/action?pass={pwd}&action=ban_user&uid={uid_val}&tab=search"
            res_rows += (f"<tr><td>{uid_val}</td><td><b>{u['full_name']}</b></td>"
                         f"<td>@{u['username'] or '—'}</td><td>{fmt_coins(u['coins'])} 🪙</td>"
                         f"<td>{u['level']}</td><td>{vb}</td><td>{u['wins']}/{u['losses']}</td>"
                         f'<td><a href="{ban_url}" style="color:#f44">🚫</a></td></tr>')
        res_html = ""
        if q2:
            res_html = (f'<div class="section"><div class="sh">Результаты ({len(found)})</div>'
                        f'<div class="sb" style="padding:0"><table>'
                        f'<tr><th>ID</th><th>Имя</th><th>@</th><th>Монеты</th><th>Ур.</th><th>VIP</th><th>В/П</th><th></th></tr>'
                        f'{res_rows or "<tr><td colspan=8 style=\"text-align:center;padding:20px;color:#555\">Не найдено</td></tr>"}'
                        f'</table></div></div>')
        body = (f'<h1>🔍 Поиск игрока</h1><p class="sub">По имени, нику или ID</p>{alert}'
                f'<div class="section"><div class="sh">Поиск</div><div class="sb">'
                f'<form method="GET" action="/admin">'
                f'<input type="hidden" name="pass" value="{pwd}">'
                f'<input type="hidden" name="tab" value="search">'
                f'<div style="display:flex;gap:8px">'
                f'<input type="text" name="q" value="{q2}" placeholder="Имя, @username или ID" style="flex:1">'
                f'<button class="btn btn-gold">🔍 Найти</button></div>'
                f'</form></div></div>{res_html}')

    elif tab == "players":
        conn  = db.get_conn()
        users = conn.execute("SELECT * FROM users ORDER BY coins DESC").fetchall()
        conn.close()
        tr = "".join(f"<tr><td>{u['user_id']}</td><td>{u['full_name']}</td>"
                     f"<td>@{u['username'] or '—'}</td><td>{fmt_coins(u['coins'])} 🪙</td>"
                     f"<td>{u['level']}</td>"
                     f"<td>{'<span class=\"bv\">⭐ VIP</span>' if u['is_vip'] else '—'}</td>"
                     f"<td>{u['wins']}/{u['losses']}</td></tr>" for u in users)
        body = f"""<h1>👥 Игроки</h1><p class="sub">Всего: {s["total_users"]}</p>
        <div class="section"><div class="sh">Список</div><div class="sb" style="padding:0">
        <table><tr><th>ID</th><th>Имя</th><th>@username</th><th>Монеты</th><th>Ур.</th><th>VIP</th><th>В/П</th></tr>
        {tr}</table></div></div>"""

    elif tab == "coins":
        body = f"""<h1>💰 Монеты</h1><p class="sub">Выдача и изъятие монет</p>{alert}
        <div class="r2">
          <div class="section"><div class="sh">➕ Выдать монеты</div><div class="sb">
            <form method="GET" action="/admin/action">
              <input type="hidden" name="pass" value="{pwd}"><input type="hidden" name="action" value="give_coins"><input type="hidden" name="tab" value="coins">
              <input type="number" name="uid" placeholder="Telegram ID игрока" required>
              <input type="number" name="amount" placeholder="Количество монет" required>
              <button class="btn btn-green">➕ Выдать</button></form></div></div>
          <div class="section"><div class="sh">➖ Забрать монеты</div><div class="sb">
            <form method="GET" action="/admin/action">
              <input type="hidden" name="pass" value="{pwd}"><input type="hidden" name="action" value="take_coins"><input type="hidden" name="tab" value="coins">
              <input type="number" name="uid" placeholder="Telegram ID игрока" required>
              <input type="number" name="amount" placeholder="Количество монет" required>
              <button class="btn btn-red">➖ Забрать</button></form></div></div>
          <div class="section"><div class="sh">🔧 Установить баланс</div><div class="sb">
            <form method="GET" action="/admin/action">
              <input type="hidden" name="pass" value="{pwd}"><input type="hidden" name="action" value="set_coins"><input type="hidden" name="tab" value="coins">
              <input type="number" name="uid" placeholder="Telegram ID игрока" required>
              <input type="number" name="amount" placeholder="Новый баланс" required>
              <button class="btn btn-blue">🔧 Установить</button></form></div></div>
        </div>"""

    elif tab == "vip":
        body = f"""<h1>⭐ VIP</h1><p class="sub">Управление VIP статусом</p>{alert}
        <div class="r2">
          <div class="section"><div class="sh">⭐ Выдать VIP</div><div class="sb">
            <form method="GET" action="/admin/action">
              <input type="hidden" name="pass" value="{pwd}"><input type="hidden" name="action" value="give_vip"><input type="hidden" name="tab" value="vip">
              <input type="number" name="uid" placeholder="Telegram ID игрока" required>
              <select name="days"><option value="1">1 день</option><option value="3">3 дня</option>
              <option value="7" selected>7 дней</option><option value="30">30 дней</option><option value="365">1 год</option></select>
              <button class="btn btn-gold">⭐ Выдать VIP</button></form></div></div>
          <div class="section"><div class="sh">❌ Снять VIP</div><div class="sb">
            <form method="GET" action="/admin/action">
              <input type="hidden" name="pass" value="{pwd}"><input type="hidden" name="action" value="remove_vip"><input type="hidden" name="tab" value="vip">
              <input type="number" name="uid" placeholder="Telegram ID игрока" required>
              <button class="btn btn-red">❌ Снять VIP</button></form></div></div>
        </div>"""

    elif tab == "chances":
        names = {"slots":"🎰 Слоты","dice":"🎲 Кости","roulette":"🎡 Рулетка","blackjack":"🃏 Блэкджек","crash":"🚀 Краш"}
        rows  = "".join(f'<div class="cr"><label>{names[g]}</label>'
                        f'<input type="number" name="{g}" value="{float(db.get_win_chance(g))*100:.0f}" min="1" max="95">'
                        f'<span>{float(db.get_win_chance(g))*100:.0f}%</span></div>'
                        for g in names)
        body = f"""<h1>🎲 Шансы игр</h1><p class="sub">Вероятность победы игрока (%)</p>{alert}
        <div class="section"><div class="sh">Настройка</div><div class="sb">
          <form method="GET" action="/admin/action">
            <input type="hidden" name="pass" value="{pwd}"><input type="hidden" name="action" value="set_chances"><input type="hidden" name="tab" value="chances">
            {rows}<button class="btn btn-gold" style="margin-top:6px">💾 Сохранить</button></form></div></div>"""

    elif tab == "message":
        body = (f'<h1>💬 Сообщение игроку</h1><p class="sub">Личное сообщение или подарок</p>{alert}'
                f'<div class="r2">'
                f'<div class="section"><div class="sh">✉️ Написать игроку</div><div class="sb">'
                f'<form method="GET" action="/admin/action">'
                f'<input type="hidden" name="pass" value="{pwd}">'
                f'<input type="hidden" name="action" value="send_message">'
                f'<input type="hidden" name="tab" value="message">'
                f'<input type="number" name="uid" placeholder="Telegram ID игрока" required>'
                f'<textarea name="text" placeholder="Текст сообщения..." rows="4" '
                f'style="width:100%;background:#1a1a2e;color:#fff;border:1px solid #333;'
                f'border-radius:8px;padding:10px;margin:8px 0;resize:vertical"></textarea>'
                f'<button class="btn btn-gold">💬 Отправить</button>'
                f'</form></div></div>'
                f'<div class="section"><div class="sh">🎁 Подарить монеты + уведомление</div><div class="sb">'
                f'<form method="GET" action="/admin/action">'
                f'<input type="hidden" name="pass" value="{pwd}">'
                f'<input type="hidden" name="action" value="gift_coins">'
                f'<input type="hidden" name="tab" value="message">'
                f'<input type="number" name="uid" placeholder="Telegram ID игрока" required>'
                f'<input type="number" name="amount" placeholder="Количество монет" required>'
                f'<input type="text" name="reason" placeholder="Причина (отобразится игроку)">'
                f'<button class="btn btn-gold">🎁 Выдать с уведомлением</button>'
                f'</form></div></div></div>')

    elif tab == "broadcast":
        body = f"""<h1>📢 Рассылка</h1><p class="sub">Сообщение всем игрокам ({s["total_users"]} чел.)</p>{alert}
        <div class="section"><div class="sh">Написать</div><div class="sb">
          <form method="GET" action="/admin/action">
            <input type="hidden" name="pass" value="{pwd}"><input type="hidden" name="action" value="broadcast"><input type="hidden" name="tab" value="broadcast">
            <textarea name="text" rows="5" placeholder="Текст сообщения... (поддерживается HTML)"></textarea>
            <button class="btn btn-gold">📢 Разослать всем</button></form></div></div>"""
    elif tab == "promos":
        promos = db.get_all_promos()
        now    = int(__import__("time").time())
        rows   = ""
        for p in promos:
            exp   = datetime.fromtimestamp(p["expires_at"]).strftime("%d.%m.%Y") if p["expires_at"] else "∞"
            bonus = ""
            if p["coins"]:    bonus += f"💰 {fmt_coins(p['coins'])}"
            if p["vip_days"]: bonus += f" ⭐{p['vip_days']}д"
            expired = p["expires_at"] > 0 and p["expires_at"] < now
            style   = 'style="opacity:.45"' if expired or p["uses"] >= p["max_uses"] else ""
            code_val = p["code"]
            uses_val = p["uses"]
            max_val  = p["max_uses"]
            note_val = p["note"] or "—"
            del_url  = f"/admin/action?pass={pwd}&action=del_promo&code={code_val}&tab=promos"
            rows += (
                f"<tr {style}>"
                f"<td><code>{code_val}</code></td>"
                f"<td>{bonus}</td>"
                f"<td>{uses_val}/{max_val}</td>"
                f"<td>{exp}</td>"
                f"<td>{note_val}</td>"
                f'<td><a href="{del_url}" style="color:#f44;text-decoration:none">🗑</a></td>'
                f"</tr>"
            )
        body = f"""<h1>🎟 Промокоды</h1><p class="sub">Создание и управление промокодами</p>{alert}
        <div class="section"><div class="sh">➕ Создать промокод</div><div class="sb">
          <form method="GET" action="/admin/action">
            <input type="hidden" name="pass" value="{pwd}">
            <input type="hidden" name="action" value="create_promo">
            <input type="hidden" name="tab" value="promos">
            <div class="r2">
              <div>
                <label style="color:#888;font-size:12px;display:block;margin-bottom:4px">КОД (оставь пустым — сгенерируется)</label>
                <input type="text" name="code" placeholder="CASINO2024" style="text-transform:uppercase">
              </div>
              <div>
                <label style="color:#888;font-size:12px;display:block;margin-bottom:4px">Заметка</label>
                <input type="text" name="note" placeholder="Для новых игроков">
              </div>
              <div>
                <label style="color:#888;font-size:12px;display:block;margin-bottom:4px">💰 Монет</label>
                <input type="number" name="coins" value="1000" min="0">
              </div>
              <div>
                <label style="color:#888;font-size:12px;display:block;margin-bottom:4px">⭐ VIP дней</label>
                <input type="number" name="vip_days" value="0" min="0">
              </div>
              <div>
                <label style="color:#888;font-size:12px;display:block;margin-bottom:4px">Макс. активаций</label>
                <input type="number" name="max_uses" value="1" min="1">
              </div>
              <div>
                <label style="color:#888;font-size:12px;display:block;margin-bottom:4px">Срок (дней, 0 = бессрочно)</label>
                <input type="number" name="expires_days" value="0" min="0">
              </div>
            </div>
            <button class="btn btn-gold" style="margin-top:8px">🎟 Создать промокод</button>
          </form>
        </div></div>
        <div class="section"><div class="sh">Все промокоды ({len(promos)})</div>
        <div class="sb" style="padding:0">
        <table><tr><th>Код</th><th>Бонус</th><th>Использований</th><th>Истекает</th><th>Заметка</th><th></th></tr>
        {rows if rows else "<tr><td colspan=6 style=\"text-align:center;color:#555;padding:20px\">Промокодов нет</td></tr>"}
        </table></div></div>"""
    else:
        body = "<h1>404</h1>"

    return web.Response(text=_page(sb, body), content_type="text/html")


async def web_action_handler(request: web.Request):
    q   = request.rel_url.query
    pwd = q.get("pass", "")
    if pwd != WEB_PASSWORD:
        raise web.HTTPFound("/admin")
    action = q.get("action", "")
    tab    = q.get("tab", "stats")

    async def rd(msg="", err=""):
        raise web.HTTPFound(f"/admin?pass={pwd}&tab={tab}&msg={msg}&err={err}")

    try:
        if action == "give_coins":
            db.update_coins(int(q["uid"]), int(q["amount"]))
            await rd(msg=f"Выдано+{q['amount']}+монет+игроку+{q['uid']}")
        elif action == "take_coins":
            db.update_coins(int(q["uid"]), -int(q["amount"]))
            await rd(msg=f"Изъято+{q['amount']}+монет+у+{q['uid']}")
        elif action == "set_coins":
            db.set_coins(int(q["uid"]), int(q["amount"]))
            await rd(msg=f"Баланс+{q['uid']}+установлен:+{q['amount']}")
        elif action == "give_vip":
            db.set_vip(int(q["uid"]), int(q.get("days",7)))
            try: await bot.send_message(int(q["uid"]), f"⭐ <b>Вам выдан VIP на {q.get('days',7)} дней!</b>", parse_mode="HTML")
            except: pass
            await rd(msg=f"VIP+выдан+игроку+{q['uid']}")
        elif action == "remove_vip":
            conn = db.get_conn(); conn.execute("UPDATE users SET is_vip=0,vip_until=0 WHERE user_id=?", (int(q["uid"]),)); conn.commit(); conn.close()
            await rd(msg=f"VIP+снят+у+{q['uid']}")
        elif action == "set_chances":
            for g in ["slots","dice","roulette","blackjack","crash"]:
                db.set_setting(f"win_chance_{g}", str(max(0.01, min(0.95, float(q.get(g,40))/100))))
            await rd(msg="Шансы+сохранены")
        elif action == "broadcast":
            text = q.get("text","").strip()
            if not text: await rd(err="Пустое+сообщение")
            uids = db.get_all_user_ids(); ok = 0
            for uid in uids:
                try: await bot.send_message(uid, f"📢 <b>Рассылка:</b>\n\n{text}", parse_mode="HTML"); ok += 1; await asyncio.sleep(0.05)
                except: pass
            await rd(msg=f"Разослано+{ok}+из+{len(uids)}")
        elif action == "create_promo":
            import random, string
            code = q.get("code","").strip().upper()
            if not code:
                code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
            db.create_promo(
                code=code,
                coins=int(q.get("coins",0)),
                vip_days=int(q.get("vip_days",0)),
                max_uses=int(q.get("max_uses",1)),
                expires_days=int(q.get("expires_days",0)),
                note=q.get("note","")
            )
            await rd(msg=f"Промокод+{code}+создан")
        elif action == "del_promo":
            code = q.get("code","")
            db.delete_promo(code)
            await rd(msg=f"Промокод+{code}+удалён")
        elif action == "send_message":
            uid2 = int(q["uid"]); txt2 = q.get("text","").strip()
            if not txt2: await rd(err="Пустое+сообщение"); return
            try:
                await bot.send_message(uid2, f"📨 <b>Сообщение от администратора:</b>\n\n{txt2}", parse_mode="HTML")
                await rd(msg=f"Сообщение+отправлено+{uid2}")
            except Exception as ex: await rd(err=str(ex)[:50])
        elif action == "gift_coins":
            uid2 = int(q["uid"]); amt2 = int(q["amount"])
            reason2 = q.get("reason","") or "подарок от администратора"
            db.update_coins(uid2, amt2)
            try:
                await bot.send_message(uid2,
                    f"🎁 <b>Администратор выдал монеты!</b>\n\n💰 +{fmt_coins(amt2)} 🪙\n📝 {reason2}",
                    parse_mode="HTML")
            except: pass
            await rd(msg=f"Выдано+{amt2}+монет+{uid2}+с+уведомлением")
        elif action == "ban_user":
            uid2 = int(q["uid"])
            db.set_setting(f"banned_{uid2}", "1")
            db.set_coins(uid2, 0)
            try: await bot.send_message(uid2, "🚫 <b>Вы заблокированы администратором.</b>", parse_mode="HTML")
            except: pass
            await rd(msg=f"Игрок+{uid2}+забанен")
        else:
            await rd(err="Неизвестное+действие")
    except web.HTTPFound: raise
    except Exception as e: await rd(err=str(e)[:60])


async def start_web_panel():
    app = web.Application()
    app.router.add_get("/admin",        web_admin_handler)
    app.router.add_get("/admin/action", web_action_handler)
    app.router.add_get("/",             web_admin_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    print(f"🌐 Веб-панель: http://localhost:{WEB_PORT}?pass={WEB_PASSWORD}")



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ЗАПУСК
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  СКИЛОВЫЕ ИГРЫ (реакция, угадайка, КНБ, математика)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Хранилища состояний
# (session dicts moved to top)

@dp.message(Command("reaction", ignore_mention=True))
@ensure_registered
@game_cooldown("reaction")
async def cmd_reaction(message: Message):
    """Нажми кнопку быстрее всех! Чем быстрее — тем больше множитель."""
    uid = message.from_user.id
    args = message.text.split()
    user = db.get_user(uid)
    bet, err = validate_bet(user, args[1] if len(args) > 1 else "")
    if bet is None:
        await message.answer(err); return

    db.update_coins(uid, -bet)
    delay = random.uniform(2.0, 6.0)
    msg = await message.answer(
        f"⚡ <b>Игра на реакцию!</b>\n\n"
        f"Ставка: {fmt_coins(bet)} 🪙\n\n"
        f"👀 Жди сигнала... и нажми кнопку <b>КАК МОЖНО БЫСТРЕЕ!</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⏳ Жди...", callback_data="reaction_wait")
        ]])
    )
    reaction_sessions[uid] = {
        "bet": bet, "start": None, "active": False,
        "msg_id": msg.message_id, "delay": delay
    }
    await asyncio.sleep(delay)
    if uid not in reaction_sessions:
        return
    reaction_sessions[uid]["active"] = True
    reaction_sessions[uid]["start"]  = time.time()
    try:
        await msg.edit_text(
            f"⚡ <b>ЖМИИИ!!!</b> 🟢\n\nСтавка: {fmt_coins(bet)} 🪙",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🟢 ЖМИ СЕЙЧАС!", callback_data=f"reaction_go_{uid}")
            ]])
        )
    except: pass


@dp.callback_query(F.data == "reaction_wait")
async def reaction_wait(callback: CallbackQuery):
    await callback.answer("Ещё не время! Жди зелёного сигнала.", show_alert=False)


@dp.callback_query(F.data.startswith("reaction_go_"))
async def reaction_go(callback: CallbackQuery):
    target_uid = int(callback.data.split("_")[2])
    uid = callback.from_user.id
    if uid != target_uid:
        await callback.answer("Это не твоя игра!", show_alert=True); return

    sess = reaction_sessions.pop(uid, None)
    if not sess or not sess["active"]:
        await callback.answer("Игра уже завершена."); return

    elapsed = time.time() - sess["start"]
    bet = sess["bet"]

    # Множитель: <0.3с → x3, <0.5с → x2.5, <0.8с → x2, <1.2с → x1.5, иначе x1.1
    if elapsed < 0.3:
        mult, grade = 3.0, "🏆 МГНОВЕННО!"
    elif elapsed < 0.5:
        mult, grade = 2.5, "⚡ Молния!"
    elif elapsed < 0.8:
        mult, grade = 2.0, "🔥 Быстро!"
    elif elapsed < 1.2:
        mult, grade = 1.5, "👍 Неплохо"
    else:
        mult, grade = 1.1, "🐢 Медленно..."

    payout = apply_prestige(uid, int(bet * mult))
    db.update_coins(uid, payout)
    db.record_game(uid, True, payout)

    await callback.message.edit_text(
        f"⚡ <b>Результат реакции</b>\n\n"
        f"{grade}\n"
        f"⏱ Время: <b>{elapsed:.3f} сек</b>\n"
        f"💰 x{mult} → +{fmt_coins(payout)} 🪙",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(Command("rps", ignore_mention=True))
@ensure_registered
@game_cooldown("rps")
async def cmd_rps(message: Message):
    """Камень-ножницы-бумага против бота."""
    uid = message.from_user.id
    args = message.text.split()
    user = db.get_user(uid)
    bet, err = validate_bet(user, args[1] if len(args) > 1 else "")
    if bet is None:
        await message.answer(err); return

    db.update_coins(uid, -bet)
    rps_sessions[uid] = {"bet": bet}
    await message.answer(
        f"✂️ <b>Камень-Ножницы-Бумага</b>\n\nСтавка: {fmt_coins(bet)} 🪙\n\nВыбери:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🪨 Камень",   callback_data=f"rps_rock_{uid}"),
            InlineKeyboardButton(text="✂️ Ножницы",  callback_data=f"rps_scissors_{uid}"),
            InlineKeyboardButton(text="📄 Бумага",   callback_data=f"rps_paper_{uid}"),
        ]])
    )


@dp.callback_query(F.data.startswith("rps_"))
async def rps_choice(callback: CallbackQuery):
    parts = callback.data.split("_")
    choice = parts[1]
    target_uid = int(parts[2])
    uid = callback.from_user.id
    if uid != target_uid:
        await callback.answer("Это не твоя игра!", show_alert=True); return

    sess = rps_sessions.pop(uid, None)
    if not sess:
        await callback.answer("Игра устарела."); return

    bet = sess["bet"]
    options = ["rock", "scissors", "paper"]
    bot_choice = random.choice(options)
    names = {"rock": "🪨 Камень", "scissors": "✂️ Ножницы", "paper": "📄 Бумага"}

    beats = {"rock": "scissors", "scissors": "paper", "paper": "rock"}
    if choice == bot_choice:
        result, payout = "🤝 Ничья!", bet
        db.update_coins(uid, payout)
    elif beats[choice] == bot_choice:
        payout = apply_prestige(uid, int(bet * 1.9))
        db.update_coins(uid, payout)
        db.record_game(uid, True, payout)
        result = f"🏆 Ты победил! +{fmt_coins(payout)} 🪙"
    else:
        payout = 0
        db.record_game(uid, False, bet)
        result = f"💀 Ты проиграл! -{fmt_coins(bet)} 🪙"

    await callback.message.edit_text(
        f"✂️ <b>КНБ</b>\n\n"
        f"Ты: {names[choice]}  |  Бот: {names[bot_choice]}\n\n"
        f"{result}",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.message(Command("guess", ignore_mention=True))
@ensure_registered
@game_cooldown("guess")
async def cmd_guess(message: Message):
    """Угадай число 1-100 за 3 попытки."""
    uid = message.from_user.id
    args = message.text.split()
    user = db.get_user(uid)
    bet, err = validate_bet(user, args[1] if len(args) > 1 else "")
    if bet is None:
        await message.answer(err); return

    number = random.randint(1, 100)
    db.update_coins(uid, -bet)
    guess_sessions[uid] = {"bet": bet, "number": number, "attempts": 0, "max": 5}
    await message.answer(
        f"🧠 <b>Угадай число от 1 до 100</b>\n\n"
        f"Ставка: {fmt_coins(bet)} 🪙\n"
        f"У тебя <b>5 попыток</b>. Угадай с 1-й — x5, с 2-й — x3, далее x1.5\n\n"
        f"Введи число:",
        parse_mode="HTML"
    )



