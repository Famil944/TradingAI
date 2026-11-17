"""Subscriber read-only menus and owner-only suggestion moderation."""
import asyncio
import time
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot import otc_commands
from config.settings import ROOT, settings
from services.otc_feedback import FeedbackStore, LABELS, STATUSES
from services.otc_subscribers import Subscribers
from services.otc_strategies_live import load_live_signals
from services.otc_public_stats import (
    METHOD_LABELS, describe, main_signals, metrics,
)

router = Router(name="community")
MZ = ZoneInfo("Europe/Moscow")
METHODS = ("bb_reentry", "break_retest", "ema_pullback")
SECONDS = {"2": 120, "5": 300, "15": 900, "all": None}


@lru_cache(maxsize=1)
def feedback():
    return FeedbackStore(ROOT / "data" / "community-feedback.db")


@lru_cache(maxsize=1)
def subscribers():
    return Subscribers(ROOT / "data" / "subscribers.db")


def is_owner(user):
    return bool(user and user.id == settings.admin_chat_id)


def private(message):
    return bool(message and message.chat.type == "private")


def keyboard(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=action)
             for label, action in row]
            for row in rows
        ]
    )


def home(owner=False):
    rows = [
        [("📊 Проверить винрейт", "c:sources")],
        [("💡 Предложить улучшение", "c:suggest")],
    ]
    if not owner:
        rows.append([("🔕 Отписаться от сигналов", "c:unsubscribe")])
    if owner:
        rows.append([("📥 Предложения пользователей", "c:queue")])
        rows.append([("🔧 Административное меню", "c:owner")])
    return keyboard(rows)


STRATEGIES = (
    ("main", "① Боллинджер ×3"),
    ("bb_reentry", "② Возврат внутрь"),
    ("break_retest", "③ Пробой/ретест"),
    ("ema_pullback", "④ EMA9/19"),
)


def sources():
    """One 2×2 grid, not a forest of strategy submenus."""
    return keyboard([
        [(STRATEGIES[0][1], "c:source:main"),
         (STRATEGIES[1][1], "c:source:bb_reentry")],
        [(STRATEGIES[2][1], "c:source:break_retest"),
         (STRATEGIES[3][1], "c:source:ema_pullback")],
        [("💡 Предложение", "c:suggest"), ("◀ Меню", "c:home")],
    ])


def overview_text():
    lines = [
        "📊 ЧЕТЫРЕ СТРАТЕГИИ · M2/M5/M15",
        "WIN/LOSS — подтверждённые исходы опубликованных прогнозов",
        "",
    ]
    for i, (code, _) in enumerate(STRATEGIES, 1):
        rows = snapshots(code)
        m = metrics(rows)
        rate = f"{m['wr']:.1f}%" if m["wr"] is not None else "—"
        title = "Боллинджер ×3" if code == "main" else METHOD_LABELS[code]
        lines.append(
            f"{i}. {title}: ✅{m['win']} ❌{m['loss']} · WR {rate}"
            + (f" · ❔{m['unknown']}" if m["unknown"] else "")
        )
    lines.extend([
        "",
        "У каждой стратегии своя история и винрейт ×3/×6.",
        "Это статистика OTCharts, не подтверждённые сделки брокера.",
        "Выбери стратегию:",
    ])
    return "\n".join(lines)


def metrics_menu(source, timeframe="all", metric="B", limit=10, period="all"):
    def action(m=metric, n=limit, p=period):
        return f"c:stats:{source}:{timeframe}:{m}:{n}:{p}"

    return keyboard([
        [("M2", f"c:source_tf:{source}:2"),
         ("M5", f"c:source_tf:{source}:5"),
         ("M15", f"c:source_tf:{source}:15"),
         ("Все", f"c:source_tf:{source}:all")],
        [("Входы", action("A")), ("×3", action("B")), ("×6", action("C"))],
        [("10", action(n=10)), ("50", action(n=50)), ("100", action(n=100))],
        [("Сегодня", action(p="today")), ("7 дней", action(p="7d")),
         ("Всё", action(p="all"))],
        [("◀ 4 стратегии", "c:sources")],
    ])


def source_health(source):
    current = otc_commands.service
    if current is None or not current.running():
        return "Остановлен / нет работающего сеанса"
    age = time.time() - current.last_tick if current.last_tick is not None else None
    threshold = getattr(getattr(current, "config", None), "max_tick_age", 8.0)
    if age is None or age < 0 or age > threshold:
        return "Нет свежих котировок"
    if getattr(current, "phase", "") != "поток активен":
        return "Подключение / ожидание данных"
    if source == "main":
        strategy = getattr(current, "cycle_preview", None)
        if not strategy or not getattr(strategy, "enabled", False):
            return "Основной алгоритм не активен"
    return "Данные свежие"


def source_title(source):
    return "Боллинджер ×3" if source == "main" else METHOD_LABELS[source]


def snapshots(source):
    if source == "main":
        return main_signals(ROOT / "data" / "bollinger-cycles.db")
    return [
        row for row in load_live_signals(ROOT / "data" / "community-live.db")
        if row["strategy"] == source
    ]


def report_for(source, timeframe, metric, limit=10, period="all"):
    now = time.time()
    if period == "today":
        since = datetime.now(MZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    elif period == "7d":
        since = now - 7 * 86400
    else:
        since = 0
    rows = [r for r in snapshots(source) if r["created_at"] >= since]
    seconds = SECONDS[timeframe]
    label = source_title(source)
    versions = sorted({row["version"] for row in rows})
    text = label + "\nСтатус: " + source_health(source) + "\nОпубликованные прогнозы OTCharts (не сделки брокера)."
    text += "\nПериод: " + {"today": "сегодня МСК", "7d": "7 дней", "all": "вся история"}[period]
    if not rows:
        return text + "\n\nПока нет сохранённых исходов."
    if len(versions) > 1:
        text += "\nВерсии учитываются раздельно:"
    for version in versions:
        group = [row for row in rows if row["version"] == version]
        text += "\n\nВерсия: " + version[:64] + "\n"
        text += describe(group, metric, seconds, since=since, limit=limit, now=now)
    return text

def split_messages(text, limit=3500):
    """Keep Telegram messages within limits without hiding requested rows."""
    while text:
        if len(text) <= limit:
            yield text
            return
        end = text.rfind("\n", 0, limit)
        if end < limit // 2:
            end = limit
        yield text[:end]
        text = text[end:].lstrip("\n")


def timestamp(value):
    return datetime.fromtimestamp(value, MZ).strftime("%d.%m %H:%M МСК")


@router.message(Command("start"))
async def start(message: types.Message):
    if not private(message):
        return
    if is_owner(message.from_user):
        await otc_commands.show_menu(message)
        return
    await asyncio.to_thread(subscribers().subscribe, message.chat.id)
    await message.answer(
        "✅ Подписка на сигналы включена.\n"
        "Когда владелец запустит бота и появится новый сигнал, "
        "ты получишь его автоматически — нажимать «Запустить» не нужно.\n"
        "Прогнозы OTCharts не являются подтверждёнными сделками брокера.",
        reply_markup=home(), parse_mode=None,
    )


@router.message(Command("help"))
async def help_menu(message: types.Message):
    if not private(message):
        return
    if is_owner(message.from_user):
        await otc_commands.show_menu(message)
    else:
        await message.answer(
            "Здесь можно смотреть результаты и предлагать улучшения. "
            "Чтобы получать новые сигналы автоматически — /start.",
            reply_markup=home(), parse_mode=None,
        )


@router.message(Command("stop_signals"))
async def stop_signals(message: types.Message):
    if not private(message) or is_owner(message.from_user):
        return
    await asyncio.to_thread(subscribers().unsubscribe, message.chat.id)
    await message.answer(
        "🔕 Подписка отключена. Новые личные сигналы больше не отправляются. "
        "Чтобы подписаться снова, используй /start.",
        reply_markup=keyboard([
            [("🔔 Подписаться снова", "c:subscribe")],
            [("📊 Винрейт", "c:sources")],
            [("💡 Предложение", "c:suggest")],
        ]), parse_mode=None,
    )


@router.message(Command("signal_stats"))
async def signal_stats(message: types.Message):
    if private(message):
        text = await asyncio.to_thread(overview_text)
        await message.answer(text, reply_markup=sources(), parse_mode=None)


@router.message(Command("suggest", "feedback"))
async def suggest(message: types.Message):
    if not private(message):
        return
    feedback().begin(message.from_user.id)
    await message.answer(
        "💡 Напиши предложение одним сообщением (5–1000 символов).\n"
        "Текст увидит владелец бота. Для отмены — /cancel.",
        parse_mode=None,
    )


@router.message(Command("cancel"))
async def cancel(message: types.Message):
    if private(message):
        feedback().cancel(message.from_user.id)
        await message.answer("Ввод отменён.", reply_markup=home(is_owner(message.from_user)))


@router.message(Command("suggestions"))
async def suggestions(message: types.Message):
    if not private(message) or not is_owner(message.from_user):
        return
    await show_queue(message)


async def show_queue(message):
    rows = await asyncio.to_thread(feedback().list, 15)
    if not rows:
        await message.answer("Предложений пока нет.")
        return
    lines = ["📥 Последние предложения"]
    for r in rows:
        lines.append(
            f"№{r['id']} · {LABELS[r['status']]} · {timestamp(r['created_at'])}\n"
            + r["text"][:120]
        )
    buttons = [[(f"№{r['id']}", f"c:item:{r['id']}")]
               for r in rows[:10]]
    buttons.append([("Обновить", "c:queue")])
    await message.answer("\n\n".join(lines)[:3800], reply_markup=keyboard(buttons), parse_mode=None)


@router.callback_query(F.data.startswith("c:"))
async def callbacks(query: types.CallbackQuery):
    if not query.message or not private(query.message):
        await query.answer("Доступно только в личном чате")
        return
    action = (query.data or "").split(":")
    owner = is_owner(query.from_user)
    target = action[1] if len(action) > 1 else ""
    if target == "owner":
        if not owner:
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        await otc_commands.show_menu(query.message)
        return
    if target == "home":
        await query.answer()
        await query.message.answer("Меню:", reply_markup=home(owner))
        return
    if target == "unsubscribe" and not owner:
        await asyncio.to_thread(subscribers().unsubscribe, query.message.chat.id)
        await query.answer("Личные сигналы отключены")
        await query.message.answer(
            "🔕 Новые личные сигналы отключены. "
            "Чтобы снова получать их, нажми кнопку:",
            reply_markup=keyboard([[("🔔 Подписаться", "c:subscribe")]]),
            parse_mode=None,
        )
        return
    if target == "subscribe" and not owner:
        await asyncio.to_thread(subscribers().subscribe, query.message.chat.id)
        await query.answer("Подписка включена")
        await query.message.answer(
            "✅ Подписка включена. Запускает торговую систему только владелец.",
            reply_markup=home(), parse_mode=None,
        )
        return
    if target == "sources":
        await query.answer()
        text = await asyncio.to_thread(overview_text)
        await query.message.answer(text, reply_markup=sources(), parse_mode=None)
        return
    if target == "suggest":
        await query.answer()
        feedback().begin(query.from_user.id)
        await query.message.answer(
            "💡 Напиши предложение одним сообщением (5–1000 символов). "
            "Его увидит владелец. Отмена: /cancel.",
            parse_mode=None,
        )
        return
    if target in ("source", "source_tf"):
        source = action[2] if len(action) > 2 else ""
        tf = action[3] if target == "source_tf" and len(action) == 4 else "all"
        if source not in ("main",) + METHODS or tf not in SECONDS:
            await query.answer("Некорректный выбор")
            return
        await query.answer()
        text = await asyncio.to_thread(report_for, source, tf, "B")
        await query.message.answer(
            text, reply_markup=metrics_menu(source, tf), parse_mode=None
        )
        return
    if target == "stats":
        if (len(action) not in (5, 7) or action[2] not in ("main",) + METHODS
                or action[3] not in SECONDS or action[4] not in ("A", "B", "C")
                or (len(action) == 7 and
                    (action[5] not in ("10", "50", "100")
                     or action[6] not in ("all", "today", "7d")))):
            await query.answer("Некорректный выбор")
            return
        limit = int(action[5]) if len(action) == 7 else 10
        period = action[6] if len(action) == 7 else "all"
        await query.answer()
        text = await asyncio.to_thread(
            report_for, action[2], action[3], action[4], limit, period
        )
        messages = list(split_messages(text))
        for index, part in enumerate(messages):
            await query.message.answer(
                part,
                reply_markup=(
                    metrics_menu(action[2], action[3], action[4], limit, period)
                    if index == len(messages) - 1 else None
                ),
                parse_mode=None,
            )
        return
    if not owner:
        await query.answer("Нет доступа", show_alert=True)
        return
    if target in ("shadow", "toggle"):
        await query.answer(
            "Старое тестовое меню отключено. Все 4 стратегии включаются вместе "
            "при запуске владельцем.", show_alert=True
        )
        return
    if target == "queue":
        await query.answer()
        await show_queue(query.message)
        return
    if target == "item" and len(action) == 3 and action[2].isdigit():
        item = feedback().get(int(action[2]))
        if item is None:
            await query.answer("Не найдено")
            return
        await query.answer()
        text = (
            f"💡 Предложение №{item['id']} · {LABELS[item['status']]}\n"
            f"{timestamp(item['created_at'])}\n\n{item['text']}"
        )
        buttons = [[(label, f"c:set:{item['id']}:{status}")]
                   for status, label in LABELS.items() if status != item["status"]]
        buttons.append([("Ответить", f"c:reply:{item['id']}")])
        await query.message.answer(text, reply_markup=keyboard(buttons), parse_mode=None)
        return
    if target == "set" and len(action) == 4 and action[2].isdigit():
        if action[3] not in STATUSES:
            await query.answer("Недопустимый статус")
            return
        changed = feedback().set_status(int(action[2]), action[3])
        await query.answer("Статус обновлён" if changed else "Не найдено")
        return
    if target == "reply" and len(action) == 3 and action[2].isdigit():
        if not feedback().get(int(action[2])):
            await query.answer("Не найдено")
            return
        feedback().begin(query.from_user.id, "REPLY", int(action[2]))
        await query.answer()
        await query.message.answer("Напиши ответ пользователю одним сообщением. Отмена: /cancel")
        return
    await query.answer("Неизвестная команда")


def awaiting_text(message):
    return (
        bool(message.from_user and private(message))
        and feedback().state(message.from_user.id) is not None
    )


@router.message(F.text, awaiting_text)
async def receive_text(message: types.Message):
    state = feedback().state(message.from_user.id)
    if not state:
        return
    if state["mode"] == "REPLY":
        if not is_owner(message.from_user):
            return
        item = feedback().get(state["suggestion_id"])
        if not item:
            feedback().cancel(message.from_user.id)
            await message.answer("Предложение не найдено.")
            return
        body = (message.text or "").strip()
        if not 1 <= len(body) <= 1000:
            await message.answer("Ответ должен содержать 1–1000 символов.")
            return
        try:
            await message.bot.send_message(
                item["chat_id"], f"💬 Ответ на предложение №{item['id']}:\n{body}",
                parse_mode=None,
            )
        except Exception:
            await message.answer("Не удалось доставить ответ. Повтори попытку.")
            return
        feedback().cancel(message.from_user.id)
        await message.answer("✅ Ответ доставлен.")
        return
    try:
        identifier = await asyncio.to_thread(
            feedback().submit, message.from_user.id, message.chat.id,
            message.message_id, message.text or "",
        )
    except ValueError as exc:
        await message.answer(str(exc), parse_mode=None)
        return
    await message.answer(f"✅ Предложение №{identifier} отправлено администратору. Спасибо!")
    if not is_owner(message.from_user):
        item = feedback().get(identifier)
        try:
            await message.bot.send_message(
                settings.admin_chat_id,
                f"💡 Новое предложение №{identifier} · {timestamp(item['created_at'])}\n"
                + item["text"],
                reply_markup=keyboard([[("Открыть", f"c:item:{identifier}")]]),
                parse_mode=None,
            )
        except Exception:
            # Stored even if Telegram delivery fails; owner can read /suggestions.
            pass
