"""Subscriber read-only menus and owner-only suggestion moderation."""
import asyncio
import time
from html import escape
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from bot import otc_commands
from config.settings import ROOT, settings
from services.otc_feedback import FeedbackStore, LABELS, STATUSES
from services.otc_subscribers import Subscribers
from services.otc_winrate_reset import WinrateReset
from services.otc_strategies_live import load_live_signals
from services.otc_public_stats import (
    METHOD_LABELS, describe, main_signals, metrics,
)

router = Router(name="community")
MZ = ZoneInfo("Europe/Moscow")
METHODS = ("bb_reentry", "break_retest", "ema_pullback", "exhaustion")
SECONDS = {"2": 120, "5": 300, "15": 900, "all": None}
PERIODS = {
    "today": "Сегодня (МСК)",
    "7d": "Последние 7 дней",
    "30d": "Последние 30 дней",
    "all": "Вся история",
}


def period_start(period, now=None):
    """Today in Moscow, rolling 7/30 days, or all time."""
    if period not in PERIODS:
        raise ValueError("Invalid statistics period")
    now = time.time() if now is None else now
    if period == "today":
        return datetime.fromtimestamp(now, MZ).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
    if period == "7d":
        return now - 7 * 86400
    if period == "30d":
        return now - 30 * 86400
    return 0



@lru_cache(maxsize=1)
def feedback():
    return FeedbackStore(ROOT / "data" / "community-feedback.db")


@lru_cache(maxsize=1)
def subscribers():
    return Subscribers(ROOT / "data" / "subscribers.db")


@lru_cache(maxsize=1)
def winrate_reset():
    return WinrateReset(ROOT / "data" / "winrate-reset.db")


def stats_since(period, now=None):
    return max(period_start(period, now), winrate_reset().cutoff())


def stats_timestamp(row):
    return float(row.get("sent_at") or row.get("created_at") or 0)


def reset_note():
    since = winrate_reset().cutoff()
    if not since:
        return ""
    date = datetime.fromtimestamp(since, MZ).strftime("%d.%m.%Y %H:%M МСК")
    return "\nНовый отсчёт с " + date + " · прежние результаты сохранены"


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


async def render_menu(message, text, *, reply_markup=None, parse_mode=None):
    """Navigate within one menu message instead of filling the chat with copies."""
    if reply_markup is None:
        reply_markup = keyboard([[("◀ Меню", "c:home")]])
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except TelegramBadRequest as exc:
        reason = str(exc).lower()
        if "message is not modified" in reason:
            return
        if "message to edit not found" not in reason and "message can't be edited" not in reason:
            raise
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


def with_admin_contact(markup):
    markup.inline_keyboard.append([InlineKeyboardButton(
        text="✉️ Написать администратору", url="https://t.me/Feymll"
    )])
    return markup


def home(owner=False):
    if not owner:
        return with_admin_contact(keyboard([
            [("▶️ Сигналы на 1 час", "c:session:start"),
             ("⏸ Пауза", "c:session:stop")],
            [("🎛 Мои сигналы", "c:prefs"), ("📊 Винрейт циклов", "c:cycles:all:today:all:global")],
            [("💎 Подписка", "c:subscription"), ("💡 Предложение", "c:suggest")],
            [("ℹ️ Справка", "c:info")],
        ]))
    return keyboard([
        [("📊 Стратегии", "c:cycles:all:today:all:global"), ("👥 Пользователи", "c:users")],
        [("💡 Предложения", "c:queue"), ("🔧 OTC", "c:owner")],
    ])


def preferences_menu(store, user_id):
    selected = store.source_preferences(user_id)
    timeframes = store.timeframe_preferences(user_id)
    rows = [
        [(("✅ " if selected[code] else "❌ ") + label, "c:pref:" + code)]
        for code, label in STRATEGIES
    ]
    rows.append([
        (("✅ " if timeframes[seconds] else "❌ ") + f"M{seconds // 60}",
         f"c:tfpref:{seconds // 60}")
        for seconds in (120,)
    ])
    rows.append([("◀ Меню", "c:home")])
    return keyboard(rows)


def preferences_text(store, user_id):
    selected = store.source_preferences(user_id)
    timeframes = store.timeframe_preferences(user_id)
    count = sum(selected.values())
    enabled_times = [f"M{seconds // 60}" for seconds, enabled
                     in timeframes.items() if enabled and seconds == 120]
    return (
        f"🎛 Мои сигналы · стратегий {count}/5\n"
        "Нажми на стратегию или экспирацию, чтобы включить или отключить.\n"
        "Сейчас доступна только экспирация M2 (2 минуты).\n"
        "Включённые экспирации: "
        + (", ".join(enabled_times) if enabled_times else "ни одной") + "\n"
        "Основные сигналы других пользователей не изменяются."
    )


STRATEGIES = (
    ("main", "① Стратегия 1"),
    ("bb_reentry", "② Стратегия 2"),
    ("break_retest", "③ Стратегия 3"),
    ("ema_pullback", "④ Стратегия 4"),
    ("exhaustion", "⑤ S5 · Свой метод"),
)

IDS = {"main": "S1", "bb_reentry": "S2", "break_retest": "S3", "ema_pullback": "S4", "exhaustion": "S5"}
TERMS = (
    "💎 TRADINGAI · ДОСТУП К СИГНАЛАМ\n\n"
    "Бот публикует автоматические прогнозы движения цены. "
    "Это не гарантия выигрыша и не автоматическое размещение сделок. "
    "Торговля бинарными опционами связана с высоким риском потери средств.\n\n"
    "🧪 Тестовый режим: первые 7 дней после одобрения бесплатны. "
    "Для получения сигналов нужно включать личный часовой сеанс.\n\n"
    "Нажимая «Принимаю условия», вы запрашиваете доступ и соглашаетесь "
    "получать уведомления. Владелец видит ваше имя Telegram, @username "
    "и количество действий в боте, но не содержание личных сообщений "
    "или время чтения сигналов."
)

INFO = (
    "📘 TRADINGAI · ПОМОЩЬ\n\n"
    "Бот присылает прогноз с названием инструмента, направлением "
    "и точным временем предполагаемого входа по Москве.\n\n"
    "🟢 ВВЕРХ — прогноз роста цены\n"
    "🔴 ВНИЗ — прогноз снижения цены\n"
    "M2 — длительность одной попытки: 2 минуты.\n\n"
    "S1–S5 — пять независимых способа анализа, а не уровни сложности.\n\n"
    "Выбери раздел ниже, чтобы узнать подробности."
)

GUIDE_TRADE = (
    "📖 КАК ЧИТАТЬ СИГНАЛ\n\n"
    "1. Получи одобрение подписки и нажми «▶️ Сигналы на 1 час».\n\n"
    "2. Найди на своей торговой платформе инструмент, который указан "
    "в сообщении. Выбирай именно его OTC-версию.\n\n"
    "3. Смотри направление: 🟢 ВВЕРХ или 🔴 ВНИЗ. "
    "M2 означает длительность 2 минуты, M5 — 5 минут, M15 — 15 минут.\n\n"
    "4. «Первый вход» — запланированное время по Москве. "
    "Отправка сигнала — за 15–20 секунд до него; задержка сети может сократить запас. "
    "Если оно уже прошло, не открывай запоздалую сделку. "
    "Проверь настройки времени и экспирации на платформе.\n\n"
    "5. Вторая попытка предусмотрена только после ❌ первой; "
    "третья — только после ❌ второй. После ✅, ➖ или ❔ продолжать "
    "последовательность не нужно. Если время следующего входа уже прошло, "
    "пропусти его.\n\n"
    "⚠️ Бот не открывает сделки за вас. Не повышайте автоматически "
    "ставку после проигрыша: риск убытков резко возрастает."
)

GUIDE_CODES = (
    "🔎 ЧТО ЗНАЧАТ ОБОЗНАЧЕНИЯ\n\n"
    "S1, S2, S3, S4, S5 — номера пяти отдельных систем анализа. "
    "Каждая формирует свои прогнозы.\n\n"
    "M2 = 2 минуты\n"
    "Это длительность ОДНОЙ попытки, не количество сигналов.\n\n"
    "1 / 2 / 3 — запланированные попытки в одном прогнозе.\n"
    "✅ WIN — прогноз завершён выигрышем\n"
    "❌ LOSS — прогноз завершён проигрышем\n"
    "⏳ PENDING — результат ещё ожидается\n"
    "➖ DRAW — ничья\n"
    "❔ UNKNOWN — данных недостаточно для определения итога\n"
    "— — попытка не выполнялась или остановлена\n\n"
    "WR (винрейт) — доля WIN среди известных WIN и LOSS "
    "опубликованных прогнозов. Это не доходность счёта."
)

GUIDE_ACCESS = (
    "💎 ДОСТУП И БЕЗОПАСНОСТЬ\n\n"
    "• Доступ выдаёт владелец после заявки. "
    "Тестовая подписка — 7 дней.\n"
    "• Для новых сигналов нажми «▶️ Сигналы на 1 час». "
    "За 10 минут до окончания придёт предложение продлить сеанс.\n"
    "• Если час истёк или наступило 00:00 МСК, получение новых "
    "сигналов приостанавливается до следующего запуска. "
    "Подписка при этом сохраняется.\n"
    "• В «Мои сигналы» можно включить или выключить S1–S5 по отдельности.\n"
    "• В «Винрейт» доступны результаты за сегодня, 7 и 30 дней.\n\n"
    "⚠️ Прогнозы не гарантируют прибыль. Бинарные опционы сопряжены "
    "с высоким риском. Рассчитывайте возможный убыток до любой сделки."
)

GUIDES = {
    "trade": GUIDE_TRADE,
    "codes": GUIDE_CODES,
    "access": GUIDE_ACCESS,
}


def guide_menu():
    return keyboard([
        [("📖 Как следовать сигналам", "c:guide:trade")],
        [("🔎 Обозначения S / M / WIN", "c:guide:codes")],
        [("💎 Подписка и риски", "c:guide:access")],
        [("◀ Меню", "c:home")],
    ])


def compact_overview(timeframe="all", period="all"):
    """Compatibility entrypoint for old menus; one canonical cycle dashboard."""
    return cycle_dashboard(period=period, timeframe=timeframe)


def compact_periods(timeframe="all", period="today"):
    return cycle_menu(period=period, timeframe=timeframe)


def cycle_menu(source="all", period="today", timeframe="all", scope="global", owner=False):
    def target(s=source, p=period, tf=timeframe, sc=scope):
        return f"c:cycles:{s}:{p}:{tf}:{sc}"
    rows = [
        [(("✓ " if source == code else "") + IDS[code], target(s=code))
         for code, _ in STRATEGIES],
        [(("✓ " if period == key else "") + label, target(p=key))
         for key, label in (("today", "Сегодня"), ("7d", "7 дней"),
                            ("30d", "30 дней"), ("all", "Всё время"))],
        [(("✓ " if timeframe == key else "") + label, target(tf=key))
         for key, label in (("all", "Все сроки"), ("2", "M2"))],
    ]
    if not owner:
        rows.append([(("✓ " if scope == "global" else "") + "Общая", target(sc="global")),
                     (("✓ " if scope == "personal" else "") + "Мои сигналы", target(sc="personal"))])
    if source != "all":
        rows.append([("Все стратегии", target(s="all"))])
        if owner:
            rows.append([("📋 Журнал циклов", f"c:stats:{source}:{timeframe}:B:10:{period}")])
    rows.append([("🕒 По часам МСК", f"c:hours:{source}:{period}:{timeframe}:{scope}")])
    rows.append([("🔄 Обновить", target()), ("◀ Меню", "c:home")])
    return keyboard(rows)


def cycle_dashboard(source="all", period="today", timeframe="all", scope="global", user_id=None, owner=False):
    from services.otc_cycle_dashboard import cycle_block, cycle_compact, expiry_lines
    since = stats_since(period)
    delivered = subscribers().delivered_ids(user_id) if scope == "personal" else None
    title = "📊 Мой винрейт" if scope == "personal" else "📊 Винрейт"
    expiry = "Все экспирации" if timeframe == "all" else "M" + timeframe
    lines = [title, f"{PERIODS[period]} · {expiry}", "WR по полному циклу · до 3 попыток", ""]
    for code, _ in STRATEGIES:
        if source not in ("all", code):
            continue
        rows = [r for r in snapshots(code)
                if stats_timestamp(r) >= since
                and (timeframe == "all" or r["seconds"] == SECONDS[timeframe])
                and (delivered is None or r["id"] in delivered)]
        label = IDS[code] + (" · " + source_title(code) if owner or code == "exhaustion" else " · Стратегия " + IDS[code][1:])
        lines.append(cycle_compact(IDS[code], rows) if source == "all"
                     else cycle_block(label, rows, detailed=True))
        if source != "all" and timeframe == "all":
            lines.extend(["", expiry_lines(rows)])
        lines.append("")
    if source == "all":
        lines.extend(["WR = ✅ / (✅ + ❌). ⏳ в работе · ➖ ничья · ❔ нет данных.",
                      "Нажми S1–S5 для подробностей.",
                      "Результаты прогнозов, не сделок на счёте."])
    else:
        lines.extend(["① / ② / ③ — на какой попытке выигран цикл.",
                  "WR = победы / (победы + поражения) циклов.",
                  "Ничьи, ожидание и нет данных в WR не входят.",
                  "Результаты прогнозов, не подтверждённые сделки на счёте."])
    if scope == "personal":
        lines.append("Только карточки с подтверждённой доставкой в твой чат.")
    return "\n".join(lines) + reset_note()


def hourly_dashboard(source="all", period="7d", timeframe="2", scope="global", user_id=None, owner=False):
    from services.otc_hourly_stats import hourly_text
    since = stats_since(period)
    delivered = subscribers().delivered_ids(user_id) if scope == "personal" else None
    rows = [r for code, _ in STRATEGIES if source in ("all", code)
            for r in snapshots(code)
            if stats_timestamp(r) >= since
            and (timeframe == "all" or r["seconds"] == SECONDS[timeframe])
            and (delivered is None or r["id"] in delivered)]
    label = "S1–S5 вместе" if source == "all" else IDS[source]
    label += " · " + PERIODS[period] + " · " + ("все сроки" if timeframe == "all" else "M"+timeframe)
    label += " · " + ("мои сигналы" if scope == "personal" else "общая")
    return hourly_text(rows,label) + reset_note()


async def show_hours(message, *, source, period, timeframe, scope, owner):
    text = await asyncio.to_thread(hourly_dashboard,source,period,timeframe,scope,message.chat.id,owner)
    markup = cycle_menu(source,period,timeframe,scope,owner)
    filtered=[]
    for row in markup.inline_keyboard:
        kept=[]
        for button in row:
            action=button.callback_data or ""
            if action.startswith(("c:hours:","c:stats:")):
                continue
            if action.startswith("c:cycles:"):
                button.callback_data=action.replace("c:cycles:","c:hours:",1)
            kept.append(button)
        if kept:
            filtered.append(kept)
    markup.inline_keyboard=filtered
    markup.inline_keyboard.append([InlineKeyboardButton(text="◀ Общий винрейт",callback_data=f"c:cycles:{source}:{period}:{timeframe}:{scope}")])
    await render_menu(message,text,reply_markup=markup)


async def show_cycles(message, *, source="all", period="today", timeframe="all", scope="global", owner=False, edit=False):
    text = await asyncio.to_thread(cycle_dashboard, source, period, timeframe, scope, message.chat.id, owner)
    markup = cycle_menu(source, period, timeframe, scope, owner)
    if edit:
        await render_menu(message, text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup, parse_mode=None)


def access_text(store, user_id):
    status = store.status(user_id)
    state = status["state"]
    if state == "approved":
        session = store.session_status(user_id)
        if session["active"]:
            return (f"💎 Подписка до {timestamp(status['expires_at'])}\n"
                    f"✅ Сигналы включены до {timestamp(session['expires_at'])}.\n"
                    "Нажми «Сигналы на 1 час», чтобы продлить сеанс.")
        return (f"💎 Подписка до {timestamp(status['expires_at'])}\n"
                "⏸ Сигналы на паузе. Нажми «Сигналы на 1 час».")
    if state == "paused":
        return f"🔕 Уведомления отключены · доступ до {timestamp(status['expires_at'])}"
    if state == "pending":
        return "🕓 Заявка принята. Ожидайте решения владельца."
    if state == "expired":
        return "⌛ Доступ закончился. Можно запросить продление."
    if state == "blocked":
        return "⛔ Доступ ограничен владельцем."
    return "💎 Доступ ещё не оформлен. Примите условия, чтобы подать заявку."


def subscriber_home_text(user_id):
    store = subscribers()
    body = "TradingAI · Личный кабинет\n\n" + access_text(store, user_id)
    if store.status(user_id)["state"] != "approved":
        return body
    selected = store.source_preferences(user_id)
    frames = store.timeframe_preferences(user_id)
    sources_text = ", ".join(IDS[k] for k, enabled in selected.items() if enabled)
    frames_text = ", ".join(f"M{k // 60}" for k, enabled in frames.items() if enabled and k == 120)
    body += f"\n\nСтратегии: {sources_text or 'не выбраны'}\nЭкспирации: {frames_text or 'не выбраны'}"
    if not sources_text or not frames_text:
        return body + "\nВыбери хотя бы одну стратегию и экспирацию в «Мои сигналы»."
    current = otc_commands.service
    if current is None or not current.running():
        body += "\n\nПоток сигналов сейчас остановлен владельцем или недоступен."
    elif (getattr(current, "last_tick", None) is None
          or not 0 <= time.time() - current.last_tick <= 8):
        body += "\n\nОжидаем свежие котировки. Новые сигналы временно недоступны."
    else:
        body += "\n\nКотировки поступают. Новый сигнал появится при подходящих условиях."
    return body


def subscription_buttons(store, user_id):
    state = store.status(user_id)["state"]
    if state == "new":
        return with_admin_contact(keyboard([[("✅ Принимаю условия · запросить", "c:accept")],
                         [("ℹ️ О сигналах", "c:info")]]))
    if state == "expired":
        return with_admin_contact(keyboard([[("📩 Запросить продление", "c:request")],
                         [("◀ Меню", "c:home")]]))
    if state == "paused":
        return with_admin_contact(keyboard([[("🔔 Возобновить уведомления", "c:request")]]))
    return home()


def sources(period="today"):
    return cycle_menu(period=period, owner=True)


def overview_text(period="today"):
    return cycle_dashboard(period=period, owner=True)


def metrics_menu(source, timeframe="all", metric="B", limit=10, period="all"):
    def action(m=metric, n=limit, p=period):
        return f"c:stats:{source}:{timeframe}:{m}:{n}:{p}"

    return keyboard([
        [("M2", f"c:stats:{source}:2:{metric}:{limit}:{period}"),
         ("M5", f"c:stats:{source}:5:{metric}:{limit}:{period}"),
         ("M15", f"c:stats:{source}:15:{metric}:{limit}:{period}"),
         ("Все", f"c:stats:{source}:all:{metric}:{limit}:{period}")],
        [("✓ Циклы ×3" if metric == "B" else "Вернуться к циклам ×3", action("B"))],
        [("10", action(n=10)), ("50", action(n=50)), ("100", action(n=100))],
        [("Сегодня", action(p="today")), ("7 дней", action(p="7d")),
         ("30 дней", action(p="30d")), ("Всё", action(p="all"))],
        [("◀ Статистика циклов", f"c:cycles:{source}:{period}:{timeframe}:global"),
         ("🧹 Сброс WR", "c:wr_reset:ask")],
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
    since = stats_since(period, now)
    rows = [r for r in snapshots(source) if stats_timestamp(r) >= since]
    seconds = SECONDS[timeframe]
    label = source_title(source)
    versions = sorted({row["version"] for row in rows})
    text = label + "\nСтатус: " + source_health(source) + "\nОпубликованные прогнозы OTCharts (не сделки брокера)."
    text += "\nПериод: " + PERIODS[period] + reset_note()
    if not rows:
        return text + "\n\nПока нет сохранённых исходов."
    if len(versions) > 1:
        text += "\nВерсии учитываются раздельно:"
    for version in versions:
        group = [row for row in rows if row["version"] == version]
        text += "\n\nВерсия: " + version[:64] + "\n"
        text += describe(group, metric, seconds, since=0, limit=limit, now=now)
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


async def clear_legacy_keyboard(message: types.Message):
    """Remove an old Telegram reply-keyboard without touching inline menus."""
    await message.answer(
        "✅ Старое меню команд скрыто.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=None,
    )


@router.message(Command("clear_keyboard"))
async def clear_keyboard(message: types.Message):
    if private(message):
        await clear_legacy_keyboard(message)


@router.message(Command("start"))
async def start(message: types.Message):
    if not private(message):
        return
    await clear_legacy_keyboard(message)
    if is_owner(message.from_user):
        await otc_commands.show_menu(message)
        return
    store = subscribers()
    status = await asyncio.to_thread(store.status, message.chat.id)
    body = TERMS if status["state"] == "new" else await asyncio.to_thread(subscriber_home_text, message.chat.id)
    await message.answer(body, reply_markup=subscription_buttons(store, message.chat.id),
                         parse_mode=None)


@router.message(Command("help"))
async def help_menu(message: types.Message):
    if not private(message):
        return
    if is_owner(message.from_user):
        await message.answer(
            "ℹ️ Управление TradingAI\n"
            "/start или /otc — главное меню и управление потоком.\n"
            "/signal_stats — статистика S1–S5.\n"
            "/otc_stats — виртуальная статистика.\n"
            "/access — пользователи и подписки.\n"
            "/suggestions — предложения подписчиков.\n"
            "Команды управления и подробные отчёты доступны в главном меню.",
            reply_markup=home(owner=True), parse_mode=None,
        )
    else:
        await message.answer(INFO, reply_markup=guide_menu(), parse_mode=None)


@router.message(Command("stop_signals"))
async def stop_signals(message: types.Message):
    if not private(message) or is_owner(message.from_user):
        return
    await asyncio.to_thread(subscribers().unsubscribe, message.chat.id)
    await message.answer(
        "🔕 Уведомления остановлены. Возобновить можно через /start, "
        "пока действует одобренный срок.",
        reply_markup=home(), parse_mode=None,
    )


def reset_confirmation_menu(token):
    return keyboard([
        [("✅ Да, начать винрейт заново", "c:wr_reset:confirm:" + token)],
        [("❌ Отмена", "c:wr_reset:cancel:" + token)],
    ])


@router.message(Command("reset_winrate"))
async def reset_winrate_command(message: types.Message):
    if not private(message) or not is_owner(message.from_user):
        return
    if message.chat.id != settings.admin_chat_id:
        return
    token = await asyncio.to_thread(winrate_reset().prepare)
    await message.answer(
        "🧹 Начать новый отсчёт статистики S1–S5?\n"
        "Периоды «Сегодня», «7 дней», «30 дней» и «Всё» будут считать "
        "только прогнозы, отправленные после подтверждения.\n\n"
        "Все старые WIN/LOSS сохранятся в журналах. "
        "Торговые стратегии и подписки не меняются.",
        reply_markup=reset_confirmation_menu(token), parse_mode=None,
    )


@router.message(Command("signal_stats"))
async def signal_stats(message: types.Message):
    if not private(message):
        return
    owner = is_owner(message.from_user)
    if owner or subscribers().status(message.chat.id)["state"] == "approved":
        await show_cycles(message, owner=owner)
    else:
        await message.answer("Сначала оформите доступ: /start", reply_markup=home())


@router.message(Command("access", "requests"))
async def access_commands(message: types.Message):
    if private(message) and is_owner(message.from_user):
        await show_access(message)


def profile_name(row):
    name = " ".join(filter(None, [row.get("first_name"), row.get("last_name")])).strip()
    return name or ("@" + row["username"] if row.get("username") else "Пользователь")


def profile_url(row):
    username = row.get("username")
    return ("https://t.me/" + username) if username else "tg://user?id=" + str(int(row["chat_id"]))


async def show_access(message, page=0, edit=False):
    size = 8
    users = await asyncio.to_thread(subscribers().member_profiles, size + 1, page * size)
    counts = await asyncio.to_thread(subscribers().dashboard_counts)
    lines = [f"👥 Пользователи · {page + 1}",
             f"✅ {counts['active']} · 📨 {counts['pending']} · ▶️ {counts['sessions']}"]
    states = {"approved": "✅", "pending": "📨", "expired": "⌛",
              "paused": "⏸", "blocked": "⛔", "new": "▫️"}
    buttons = []
    for person in users[:size]:
        chat_id = int(person["chat_id"])
        state = await asyncio.to_thread(subscribers().status, chat_id)
        label = f"{states.get(state['state'], '▫️')} {profile_name(person)[:20]}"
        if not buttons or len(buttons[-1]) == 2:
            buttons.append([])
        buttons[-1].append((label, f"c:user:{chat_id}:{page}"))
    if not users:
        lines.append("На этой странице пользователей нет.")
    nav = []
    if page:
        nav.append(("← Назад", f"c:access:{page - 1}"))
    if len(users) > size:
        nav.append(("Далее →", f"c:access:{page + 1}"))
    if nav:
        buttons.append(nav)
    buttons.append([("🔄 Обновить", f"c:access:{page}"), ("◀ Меню", "c:home")])
    markup = keyboard(buttons)
    body = "\n".join(lines)
    if edit:
        await render_menu(message, body, reply_markup=markup)
    else:
        await message.answer(body, reply_markup=markup, parse_mode=None)


async def show_user(message, user_id, page=0):
    info = await asyncio.to_thread(subscribers().member_activity, user_id)
    if info is None:
        await render_menu(message, "Пользователь не найден.", reply_markup=keyboard([[("◀ Пользователи", f"c:access:{page}")]]))
        return
    title = escape(profile_name(info))
    state = subscribers().status(user_id)["state"]
    session_end = info.get("session_expires_at")
    session = (
        f"🟢 До {timestamp(session_end)}" if session_end and session_end > time.time()
        else "⏸ Неактивен"
    )
    last = timestamp(info["last_interaction"]) if info.get("last_interaction") else "Нет данных"
    expiry = timestamp(info["expires_at"]) if info.get("expires_at") else "Не назначен"
    text = (
        f'👤 <a href="{profile_url(info)}">{title}</a>\n'
        f"Доступ: { {'approved': '✅', 'pending': '📨 Заявка', 'expired': '⌛ Истёк', 'paused': '⏸', 'blocked': '⛔'}.get(state, '—')} · {expiry}\n"
        f"Сеанс: {session}\n"
        f"Действия / 7д: {info['actions_7d']} · сигналы: {info['delivered']}\n"
        f"Был в боте: {last}\n"
        f"ID <code>{user_id}</code>"
    )
    action = ("Разблокировать", f"c:unblock:{user_id}") if state == "blocked" else (
        "⛔ Блокировать", f"c:block:{user_id}"
    )
    grant_label = "➕ Продлить на 7 дней" if state in ("approved", "paused") else "✅ Одобрить 7 дней"
    grant_action = "grant" if state in ("approved", "paused") else "approve"
    await render_menu(message,
        text, parse_mode="HTML",
        reply_markup=keyboard([
            [(grant_label, f"c:{grant_action}:{user_id}:7"), action],
            [("◀ Пользователи", f"c:access:{page}")],
        ]),
    )


@router.message(Command("grant"))
async def grant_command(message: types.Message):
    if not private(message) or not is_owner(message.from_user):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        await message.answer("Формат: /grant TELEGRAM_ID ДНЕЙ (1–365)")
        return
    await grant_access(message, int(parts[1]), int(parts[2]))


async def grant_access(message, chat_id, days, *, request_key=None, approval_only=False, edit=False):
    try:
        expiry = await asyncio.to_thread(subscribers().grant, chat_id, days, request_key=request_key, approval_only=approval_only)
    except ValueError as exc:
        if edit:
            await render_menu(message, str(exc), reply_markup=keyboard([[("◀ Пользователь", f"c:user:{chat_id}")]]))
        else:
            await message.answer(str(exc))
        return
    if edit:
        await show_user(message, chat_id)
    else:
        await message.answer(f"✅ {chat_id}: доступ до {timestamp(expiry)}")
    try:
        await message.bot.send_message(
            chat_id, f"✅ Доступ одобрен до {timestamp(expiry)}. "
            "Для получения личных сигналов нажми /start и кнопку «Сигналы на 1 час».",
            reply_markup=home(), parse_mode=None,
        )
    except Exception:
        # Approval persisted; subscriber will see the status upon /start.
        pass


@router.message(Command("block", "unblock"))
async def block_command(message: types.Message):
    if not private(message) or not is_owner(message.from_user):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /block ID или /unblock ID")
        return
    action = subscribers().unblock if parts[0].startswith("/unblock") else subscribers().block
    changed = await asyncio.to_thread(action, int(parts[1]))
    await message.answer("✅ Обновлено" if changed else "Пользователь не найден")


@router.message(Command("suggest", "feedback"))
async def suggest(message: types.Message):
    if not private(message):
        return
    if is_owner(message.from_user):
        # Legacy /suggest or /feedback: owner reads/moderates, never submits.
        await show_queue(message)
        return
    if subscribers().status(message.chat.id)["state"] != "approved":
        await message.answer("Сначала оформите доступ: /start", reply_markup=home())
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


async def show_queue(message, edit=False):
    send = (lambda text, **kw: render_menu(message, text, **kw)) if edit else message.answer
    rows = await asyncio.to_thread(feedback().list, 8)
    if not rows:
        await send("Предложений пока нет.", reply_markup=home(owner=True))
        return
    lines = ["📥 Последние предложения"]
    for r in rows:
        lines.append(
            f"№{r['id']} · {LABELS[r['status']]} · " + r["text"].replace("\n", " ")[:60]
        )
    buttons = [[(f"№{r['id']}", f"c:item:{r['id']}") for r in rows[i:i+4]]
               for i in range(0, len(rows), 4)]
    buttons.append([("Обновить", "c:queue"), ("◀ Меню", "c:home")])
    await send("\n".join(lines), reply_markup=keyboard(buttons), parse_mode=None)


@router.callback_query(F.data.startswith("c:"))
async def callbacks(query: types.CallbackQuery):
    if not query.message or not private(query.message):
        await query.answer("Доступно только в личном чате")
        return
    action = (query.data or "").split(":")
    owner = is_owner(query.from_user)
    admin_chat = owner and query.message.chat.id == settings.admin_chat_id
    target = action[1] if len(action) > 1 else ""
    if target in ("cycles", "hours"):
        if len(action) != 6 or action[2] not in ("all", *IDS) or action[3] not in PERIODS or action[4] not in SECONDS or action[5] not in ("global", "personal"):
            await query.answer("Некорректный фильтр", show_alert=True)
            return
        if not owner and subscribers().status(query.from_user.id)["state"] != "approved":
            await query.answer("Нет активного доступа", show_alert=True)
            return
        await query.answer()
        if target == "hours":
            await show_hours(query.message, source=action[2], period=action[3],
                             timeframe=action[4], scope=action[5], owner=owner)
        else:
            await show_cycles(query.message, source=action[2], period=action[3],
                              timeframe=action[4], scope=action[5], owner=owner, edit=True)
        return
    if target in ("grant", "approve", "block", "unblock", "access", "user"):
        if not admin_chat:
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        if target == "access":
            page = int(action[2]) if len(action) == 3 and action[2].isdigit() else 0
            await show_access(query.message, page=min(page, 100000), edit=True)
        elif target == "user" and len(action) in (3, 4) and action[2].isdigit():
            page = int(action[3]) if len(action) == 4 and action[3].isdigit() else 0
            await show_user(query.message, int(action[2]), page=page)
        elif target == "unblock" and len(action) == 3 and action[2].isdigit():
            await asyncio.to_thread(subscribers().unblock, int(action[2]))
            await show_user(query.message, int(action[2]))
        elif target in ("grant", "approve") and len(action) == 4 and action[2].isdigit() and action[3].isdigit():
            await grant_access(query.message, int(action[2]), int(action[3]),
                               request_key=f"{query.message.chat.id}:{query.message.message_id}:{action[2]}:{action[3]}",
                               approval_only=target == "approve", edit=True)
        elif target == "block" and len(action) == 3 and action[2].isdigit():
            await asyncio.to_thread(subscribers().block, int(action[2]))
            await show_user(query.message, int(action[2]))
        return
    if target == "wr_reset":
        if not admin_chat:
            await query.answer("Нет доступа", show_alert=True)
            return
        if len(action) == 3 and action[2] == "ask":
            token = await asyncio.to_thread(winrate_reset().prepare)
            await query.answer()
            await render_menu(query.message,
                "🧹 Сбросить отображаемый винрейт всех S1–S5?\n"
                "Новый отсчёт начнётся с момента подтверждения. "
                "Старые исходы останутся в журналах, но не войдут "
                "в текущие отчёты.",
                reply_markup=reset_confirmation_menu(token), parse_mode=None,
            )
            return
        if len(action) == 4 and action[2] in ("confirm", "cancel"):
            token = action[3]
            if action[2] == "cancel":
                await asyncio.to_thread(winrate_reset().cancel, token)
                await query.answer("Сброс отменён")
                await render_menu(query.message,"Сброс винрейта отменён.", parse_mode=None)
                return
            success = await asyncio.to_thread(winrate_reset().confirm, token)
            if not success:
                await query.answer(
                    "Подтверждение устарело или уже использовано.",
                    show_alert=True,
                )
                return
            await query.answer("Новый отсчёт начат")
            await render_menu(query.message,
                "✅ Новый отсчёт винрейта S1–S5 начат.\n"
                "До новых завершённых прогнозов WR будет равен «—». "
                "Прежняя история WIN/LOSS сохранена.",
                reply_markup=sources(), parse_mode=None,
            )
            return
        await query.answer("Некорректная команда", show_alert=True)
        return
    if target == "session":
        if owner or len(action) != 3 or action[2] not in ("start", "stop"):
            await query.answer("Недоступно", show_alert=True)
            return
        store = subscribers()
        if action[2] == "start":
            try:
                expiry = await asyncio.to_thread(store.start_session, query.message.chat.id)
            except ValueError as exc:
                await query.answer(str(exc), show_alert=True)
                return
            await query.answer("Сигналы включены")
            await render_menu(query.message,
                f"✅ Сигналы включены до {timestamp(expiry)}.\n"
                "Продли сеанс кнопкой «Сигналы на 1 час» или напоминанием. "
                "После окончания новые сигналы перестанут приходить.",
                reply_markup=home(), parse_mode=None,
            )
        else:
            await asyncio.to_thread(store.stop_session, query.message.chat.id)
            await query.answer("Сеанс остановлен")
            await render_menu(query.message,
                "⏸ Личные сигналы приостановлены. Подписка сохранена. "
                "Для возобновления нажми «Сигналы на 1 час».",
                reply_markup=home(), parse_mode=None,
            )
        return
    if target == "prefs":
        if not owner and subscribers().status(query.message.chat.id)["state"] != "approved":
            await query.answer("Сначала оформите доступ", show_alert=True)
            return
        await query.answer()
        store = subscribers()
        await render_menu(query.message,
            preferences_text(store, query.message.chat.id),
            reply_markup=preferences_menu(store, query.message.chat.id),
            parse_mode=None,
        )
        return
    if target == "pref":
        if len(action) != 3 or action[2] not in IDS:
            await query.answer("Некорректный источник")
            return
        if not owner and subscribers().status(query.message.chat.id)["state"] != "approved":
            await query.answer("Нет доступа", show_alert=True)
            return
        store = subscribers()
        user_id = query.message.chat.id
        current = store.source_preferences(user_id)[action[2]]
        await asyncio.to_thread(store.set_source, user_id, action[2], not current)
        await query.answer("Включено" if not current else "Отключено")
        await query.message.edit_text(
            preferences_text(store, user_id),
            reply_markup=preferences_menu(store, user_id),
            parse_mode=None,
        )
        return
    if target == "tfpref":
        if (owner or len(action) != 3 or action[2] not in ("2",)
                or query.from_user.id != query.message.chat.id):
            await query.answer("Недоступно", show_alert=True)
            return
        store = subscribers()
        if store.status(query.message.chat.id)["state"] != "approved":
            await query.answer("Нет активного доступа", show_alert=True)
            return
        seconds = int(action[2]) * 60
        old_value = store.timeframe_preferences(query.message.chat.id)[seconds]
        await asyncio.to_thread(
            store.set_timeframe, query.message.chat.id, seconds, not old_value
        )
        await query.answer(
            f"M{seconds // 60} " + ("включён" if not old_value else "отключён")
        )
        await query.message.edit_text(
            preferences_text(store, query.message.chat.id),
            reply_markup=preferences_menu(store, query.message.chat.id),
            parse_mode=None,
        )
        return
    if target == "users":
        if not admin_chat:
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        await show_access(query.message, edit=True)
        return
    if target in ("grant_help", "block_help"):
        if not admin_chat:
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        await render_menu(query.message,
            "Выдать/продлить: /grant ID ДНИ" if target == "grant_help"
            else "Блокировать: /block ID · Разблокировать: /unblock ID"
        )
        return
    if target == "info":
        await query.answer()
        await render_menu(query.message, INFO, reply_markup=guide_menu())
        return
    if target == "guide":
        if len(action) != 3 or action[2] not in GUIDES:
            await query.answer("Раздел не найден")
            return
        await query.answer()
        await render_menu(
            query.message, GUIDES[action[2]], reply_markup=guide_menu(),
        )
        return
    if target == "subscription" and not owner:
        await query.answer()
        store = subscribers()
        state = store.status(query.message.chat.id)["state"]
        await render_menu(query.message,
            TERMS if state == "new" else access_text(store, query.message.chat.id),
            reply_markup=subscription_buttons(store, query.message.chat.id),
            parse_mode=None,
        )
        return
    if target in ("accept", "request", "subscribe") and not owner:
        store = subscribers()
        previous = store.status(query.message.chat.id)["state"]
        if target == "accept" and previous != "new":
            await query.answer("Заявка уже оформлена")
            return
        if target != "accept" and previous == "new":
            await query.answer("Сначала примите условия через /start", show_alert=True)
            return
        result = await asyncio.to_thread(store.request, query.message.chat.id)
        await query.answer("Заявка отправлена" if result == "pending" else "Статус обновлён")
        await render_menu(query.message,access_text(store, query.message.chat.id),
                                   reply_markup=home(), parse_mode=None)
        if result == "pending" and previous != "pending":
            try:
                await query.bot.send_message(
                    settings.admin_chat_id,
                    f"🔐 Заявка на доступ: {query.from_user.id}"
                    + (f" (@{query.from_user.username})" if getattr(query.from_user, "username", None) else "")
                    + (" · продление" if previous == "expired" else " · 7 дней тест"),
                    reply_markup=keyboard([
                        [("✅ Одобрить 7 дней", f"c:approve:{query.from_user.id}:7")],
                        [("⛔ Заблокировать", f"c:block:{query.from_user.id}")],
                    ]), parse_mode=None,
                )
            except Exception:
                pass  # Owner can still find the persisted request via /access.
        return
    if target == "wr":
        if not owner and subscribers().status(query.message.chat.id)["state"] != "approved":
            await query.answer("Сначала оформите доступ через /start", show_alert=True)
            return
        # Old c:wr:<timeframe> buttons remain usable.
        if len(action) == 4:
            period, tf = action[2], action[3]
        elif len(action) == 3:
            period, tf = "today", action[2]
        elif len(action) == 2:
            period, tf = "today", "all"
        else:
            await query.answer("Некорректный период")
            return
        if tf not in SECONDS or period not in PERIODS:
            await query.answer("Некорректный период")
            return
        await query.answer()
        await show_cycles(query.message, period=period, timeframe=tf, owner=owner, edit=True)
        return
    if target == "owner":
        if not owner:
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        await render_menu(query.message, await asyncio.to_thread(otc_commands.panel_text),
                          reply_markup=otc_commands.menu(query.message.chat.id))
        return
    if target == "home":
        await query.answer()
        if owner:
            await render_menu(query.message, await asyncio.to_thread(otc_commands.panel_text),
                              reply_markup=otc_commands.menu(query.message.chat.id))
        else:
            await render_menu(query.message, await asyncio.to_thread(subscriber_home_text, query.message.chat.id), reply_markup=home())
        return
    if target == "unsubscribe" and not owner:
        await asyncio.to_thread(subscribers().unsubscribe, query.message.chat.id)
        await query.answer("Личные сигналы отключены")
        await render_menu(query.message,
            "🔕 Новые личные сигналы отключены. "
            "Чтобы снова получать их, нажми кнопку:",
            reply_markup=keyboard([[("🔔 Подписаться", "c:subscribe")]]),
            parse_mode=None,
        )
        return
    # Advanced owner reports must not leak source versions, health or history.
    if target in ("sources", "source", "source_tf", "stats") and not owner:
        await query.answer("Нет доступа", show_alert=True)
        return
    if target == "sources":
        period = action[2] if len(action) == 3 else "today"
        if period not in PERIODS:
            await query.answer("Некорректный период")
            return
        await query.answer()
        await show_cycles(query.message, period=period, owner=True, edit=True)
        return
    if target == "suggest":
        if owner:
            # Old owner keyboards also lead to the read-only inbox.
            await query.answer()
            await show_queue(query.message, edit=True)
            return
        if subscribers().status(query.message.chat.id)["state"] != "approved":
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        feedback().begin(query.from_user.id)
        await render_menu(query.message,
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
        await show_cycles(query.message, source=source, timeframe=tf, owner=True, edit=True)
        return
    if target == "stats":
        if (len(action) not in (5, 7) or action[2] not in ("main",) + METHODS
                or action[3] not in SECONDS or action[4] not in ("A", "B", "C")
                or (len(action) == 7 and
                    (action[5] not in ("10", "50", "100")
                     or action[6] not in PERIODS))):
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
            send = (lambda text, **kw: render_menu(query.message, text, **kw)) if len(messages) == 1 else query.message.answer
            await send(
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
        await show_queue(query.message, edit=True)
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
        buttons.append([("◀ Предложения", "c:queue")])
        await render_menu(query.message,text, reply_markup=keyboard(buttons), parse_mode=None)
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
        await render_menu(query.message,"Напиши ответ пользователю одним сообщением. Отмена: /cancel")
        return
    await query.answer("Неизвестная команда")


def awaiting_text(message):
    return (
        bool(message.from_user and private(message))
        and feedback().state(message.from_user.id) is not None
    )


@router.message(F.text, awaiting_text)
async def receive_text(message: types.Message):
    if not is_owner(message.from_user) and subscribers().status(message.chat.id)["state"] != "approved":
        feedback().cancel(message.from_user.id)
        await message.answer("Доступ закончился. Обновите заявку: /start")
        return
    state = feedback().state(message.from_user.id)
    if not state:
        return
    if is_owner(message.from_user) and state["mode"] != "REPLY":
        feedback().cancel(message.from_user.id)
        await message.answer(
            "Владелец не отправляет предложения. Список: /suggestions",
            reply_markup=home(owner=True), parse_mode=None,
        )
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
