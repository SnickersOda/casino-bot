# ============================================================
#  bot.py — основная логика казино-бота
#  Запуск: python bot.py
# ============================================================
import asyncio
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


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def fmt_coins(n: int) -> str:
    """Форматирование числа с разделителями тысяч."""
    return f"{n:,}".replace(",", " ")


def ensure_registered(func):
    """Декоратор: автоматически регистрирует пользователя.
    functools.wraps сохраняет сигнатуру — aiogram не путается с kwargs."""
    import functools
    @functools.wraps(func)
    async def wrapper(message: Message, **kwargs):
        u = message.from_user
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
@ensure_registered
async def cmd_start(message: Message):
    u        = message.from_user
    user     = db.get_user(u.id)
    vip      = "⭐ VIP" if user["is_vip"] else ""
    bot_info = await bot.get_me()
    bot_username = bot_info.username

    text = (
        f"🎰 <b>Добро пожаловать в Casino Bot!</b> {vip}\n\n"
        f"Привет, <b>{u.full_name}</b>!\n"
        f"На твоём счету: <b>{fmt_coins(user['coins'])} 🪙</b>\n\n"
        "🃏 <b>Игры:</b>\n"
        "  /slots — Слоты\n"
        "  /dice  — Кости\n"
        "  /roulette — Рулетка\n"
        "  /blackjack — Блэкджек\n"
        "  /crash — Краш\n\n"
        "📋 <b>Меню:</b>\n"
        "  /profile — Профиль\n"
        "  /balance — Баланс\n"
        "  /daily   — Ежедневный бонус\n"
        "  /tasks   — Задания\n"
        "  /top     — Топ игроков\n"
        "  /donate  — Магазин ⭐\n"
        "  /help    — Помощь\n"
    )

    # Кнопка добавления в группу сразу с правами администратора
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="➕ Добавить в группу (с правами админа)",
                url=(
                    f"https://t.me/{bot_username}?startgroup=true"
                    "&admin=change_info+delete_messages+restrict_members"
                    "+invite_users+pin_messages+manage_video_chats+manage_chat"
                )
            )
        ],
        [
            InlineKeyboardButton(text="🎮 Быстрая игра",  callback_data="quick_play"),
            InlineKeyboardButton(text="⭐ Магазин",       callback_data="open_shop"),
        ]
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(F.data == "quick_play")
async def cb_quick_play(callback: CallbackQuery):
    await callback.message.answer(
        "🎮 <b>Выбери игру и введи ставку:</b>\n\n"
        "🎰 /slots 100\n"
        "🎲 /dice 100\n"
        "🎡 /roulette red 100\n"
        "🃏 /blackjack 100\n"
        "🚀 /crash 100",
        parse_mode="HTML"
    )
    await callback.answer()


@dp.callback_query(F.data == "open_shop")
async def cb_open_shop(callback: CallbackQuery):
    text = "⭐ <b>Магазин Telegram Stars</b>\n\nПоддержи казино и получи бонусы!\n\n"
    for item in config.SHOP_ITEMS.values():
        text += f"  • {item['title']} — ⭐ {item['stars']} Stars\n"
        text += f"    <i>{item['desc']}</i>\n\n"
    await callback.message.answer(text, reply_markup=shop_keyboard(), parse_mode="HTML")
    await callback.answer()


@dp.message(Command("help"))
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


@dp.message(Command("profile"))
@ensure_registered
async def cmd_profile(message: Message):
    user  = db.get_user(message.from_user.id)
    vip   = "⭐ VIP" if user["is_vip"] else "Обычный"
    lname = config.LEVEL_NAMES.get(user["level"], "???")
    total = user["wins"] + user["losses"]
    wr    = f"{user['wins']/total*100:.1f}%" if total else "—"
    bar   = level_progress_bar(user["xp"], user["level"])

    text = (
        f"👤 <b>Профиль: {user['full_name']}</b>\n"
        f"{'─'*28}\n"
        f"🏅 Статус: {vip}\n"
        f"🎖 Уровень: {user['level']} {lname}\n"
        f"📊 Прогресс: {bar}\n"
        f"{'─'*28}\n"
        f"🪙 Монеты:  <b>{fmt_coins(user['coins'])}</b>\n"
        f"🏆 Побед:   {user['wins']}\n"
        f"💀 Поражений: {user['losses']}\n"
        f"📈 Winrate: {wr}\n"
        f"💸 Всего поставлено: {fmt_coins(user['total_bet'])}\n"
    )
    await message.answer(text, parse_mode="HTML")


@dp.message(Command("balance"))
@ensure_registered
async def cmd_balance(message: Message):
    user = db.get_user(message.from_user.id)
    await message.answer(
        f"💰 Твой баланс: <b>{fmt_coins(user['coins'])} 🪙</b>",
        parse_mode="HTML"
    )


@dp.message(Command("daily"))
@ensure_registered
async def cmd_daily(message: Message):
    result = db.claim_daily(message.from_user.id)
    if result["ok"]:
        db.update_task_progress(message.from_user.id, "play5")
        user = db.get_user(message.from_user.id)
        await message.answer(
            f"🎁 Ежедневный бонус получен!\n"
            f"+<b>{fmt_coins(result['amount'])} 🪙</b>\n"
            f"Баланс: {fmt_coins(user['coins'])} 🪙",
            parse_mode="HTML"
        )
    else:
        h = result["seconds_left"] // 3600
        m = (result["seconds_left"] % 3600) // 60
        await message.answer(
            f"⏳ Бонус уже получен сегодня.\n"
            f"Следующий через: <b>{h}ч {m}мин</b>",
            parse_mode="HTML"
        )


@dp.message(Command("tasks"))
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


@dp.message(Command("top"))
@ensure_registered
async def cmd_top(message: Message):
    rows  = db.get_top(10)
    lines = ["🏆 <b>Топ-10 игроков</b>\n"]
    medals = ["🥇","🥈","🥉"] + ["🔸"] * 7
    for i, r in enumerate(rows):
        vip = "⭐" if db.get_user(r["user_id"])["is_vip"] else ""
        lines.append(
            f"{medals[i]} <b>{r['full_name']}</b> {vip}\n"
            f"   💰 {fmt_coins(r['coins'])} | Ур.{r['level']} | 🏆{r['wins']}\n"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  СЛОТЫ  🎰  (с анимацией прокрутки!)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(Command("slots"))
@ensure_registered
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
@dp.message(Command("dice"))
@ensure_registered
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
@dp.message(Command("roulette"))
@ensure_registered
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

@dp.message(Command("blackjack"))
@ensure_registered
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


@dp.message(Command("bjroom"))
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


@dp.message(Command("bjjoin"))
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


@dp.message(Command("bjleave"))
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


@dp.message(Command("crash"))
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

        bar_idx = min(int((mult - 1.0) / max(sess["crash_at"] - 1.0, 0.1) * 10), 10)
        bar     = BARS[bar_idx]
        pot     = int(sess["bet"] * mult)
        fire    = "🔥" if mult > 3 else ("⚡" if mult > 6 else "")
        rocket  = "💥" if crashed else "🚀"

        try:
            await sess["msg"].edit_text(
                f"{rocket} <b>{'КРАШ!' if crashed else 'Ракета летит!'}</b> {fire}\n\n"
                f"📈 Коэффициент: <b>x{mult:.2f}</b>\n"
                f"{bar}\n\n"
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


@dp.message(Command("donate"))
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


@dp.message(Command("testdonate"))
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
#  АДМИН-ПАНЕЛЬ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Выдать монеты",   callback_data="adm_give")],
        [InlineKeyboardButton(text="💸 Забрать монеты",  callback_data="adm_take")],
        [InlineKeyboardButton(text="⭐ Выдать VIP",      callback_data="adm_vip")],
        [InlineKeyboardButton(text="📊 Статистика бота", callback_data="adm_stats")],
        [InlineKeyboardButton(text="🎲 Изменить шанс",   callback_data="adm_chance")],
        [InlineKeyboardButton(text="📢 Рассылка",        callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🏆 Топ-5 игроков",  callback_data="adm_top")],
    ])


@dp.message(Command("admin"))
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
    await callback.message.answer(
        f"📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей: {s['total_users']}\n"
        f"🆕 Новых сегодня: {s['new_today']}\n"
        f"⭐ VIP игроков: {s['vip_count']}\n"
        f"🪙 Монет в обращении: {fmt_coins(s['total_coins'])}\n"
        f"🎮 Игр сыграно: {total_games}\n"
        f"🏆 Побед / 💀 Поражений: {s['total_wins']} / {s['total_losses']}\n"
        f"📈 Общий WR игроков: {wr}",
        parse_mode="HTML"
    )
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


@dp.callback_query(F.data == "adm_give")
async def adm_give_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await state.set_state(AdminStates.wait_give_uid)
    await callback.message.answer("Введи user_id игрока:")
    await callback.answer()


@dp.message(AdminStates.wait_give_uid)
async def adm_give_uid(message: Message, state: FSMContext):
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




@dp.message(Command("notify"))
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
]


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
    elif action == "shop":
        t2 = "⭐ <b>Магазин Stars</b>\n\n"
        for item in config.SHOP_ITEMS.values():
            t2 += f"• {item['title']} — ⭐{item['stars']}\n  <i>{item['desc']}</i>\n\n"
        await message.answer(t2, reply_markup=shop_keyboard(), parse_mode="HTML")
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
    links = [("📊","Статистика","stats"),("👥","Игроки","players"),
             ("💰","Монеты","coins"),("⭐","VIP","vip"),
             ("🎲","Шансы игр","chances"),("📢","Рассылка","broadcast")]
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
        </div>
        <div class="section"><div class="sh">🏆 Топ-10</div><div class="sb" style="padding:0">
        <table><tr><th>#</th><th>Игрок</th><th>VIP</th><th>Монеты</th><th>Ур.</th><th>Победы</th></tr>{tr}</table>
        </div></div>"""

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

    elif tab == "broadcast":
        body = f"""<h1>📢 Рассылка</h1><p class="sub">Сообщение всем игрокам ({s["total_users"]} чел.)</p>{alert}
        <div class="section"><div class="sh">Написать</div><div class="sb">
          <form method="GET" action="/admin/action">
            <input type="hidden" name="pass" value="{pwd}"><input type="hidden" name="action" value="broadcast"><input type="hidden" name="tab" value="broadcast">
            <textarea name="text" rows="5" placeholder="Текст сообщения... (поддерживается HTML)"></textarea>
            <button class="btn btn-gold">📢 Разослать всем</button></form></div></div>"""
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
        BotCommand(command="notify",     description="🔔 Уведомления о бонусе"),
    ]
    await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    # Дополнительные команды для каждого из админов
    admin_extra = user_commands + [
        BotCommand(command="admin",      description="👑 Админ-панель"),
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
    await on_startup()
    asyncio.create_task(vip_checker())
    asyncio.create_task(daily_notifier())
    await start_web_panel()
    await dp.start_polling(bot, skip_updates=True)


if __name__ == "__main__":
    asyncio.run(main())
