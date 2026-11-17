"""Owner-only Telegram menu and callbacks."""

import asyncio
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import BaseMiddleware, F, Router, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import ROOT, settings
from services.otc_cycles import format_cycles, read_stats
from services.otc_evaluation import compact_stats
from services.otc_market import ACTIVE_TIMEFRAMES as TIMEFRAMES
from services.otc_signal_report import read_journal
from services.otc_signal_report import report as signal_quality_report

router = Router()
service = None
choices = {}
wallet_edits = {}
STATS_EPOCH = ROOT / "data" / "signal-stats-epoch.txt"
SHADOW_STATS_EPOCH = ROOT / "data" / "shadow-stats-epoch.txt"
SOURCES = (
    ("Основной", "main_ready"),
    ("Без чрезмерного движения M5", "main_broad"),
)


def stats_epoch():
    try:
        return float(STATS_EPOCH.read_text(encoding="ascii"))
    except FileNotFoundError:
        return 0.0


def shadow_stats_epoch():
    try:
        return max(stats_epoch(), float(SHADOW_STATS_EPOCH.read_text(encoding="ascii")))
    except FileNotFoundError:
        return stats_epoch()


class OwnerOnly(BaseMiddleware):
    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, types.CallbackQuery) else event
        user = event.from_user
        permitted = (
            isinstance(message, types.Message)
            and user is not None
            and message.chat.type == "private"
            and allowed(message.chat.id)
            and user.id == settings.admin_chat_id
        )
        if not permitted:
            if isinstance(event, types.CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            elif isinstance(event, types.Message):
                await event.answer(
                    "Команды управления доступны только владельцу. "
                    "Подписчики могут смотреть /signal_stats и писать /suggest.",
                    parse_mode=None,
                )
            return
        return await handler(event, data)


router.message.outer_middleware(OwnerOnly())
router.callback_query.outer_middleware(OwnerOnly())


def allowed(chat_id):
    return settings.admin_chat_id > 0 and chat_id == settings.admin_chat_id


def menu(chat_id, expanded=False):
    if service and getattr(getattr(service, "config", None), "cycles_only", False):
        selected = choices.get(chat_id, TIMEFRAMES)
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=("✅ " if selected == value else "") + label,
                        callback_data="otc:tf:" + key,
                    )
                    for key, label, value in (
                        ("all", "Все", TIMEFRAMES),
                        ("2", "M2", (120,)),
                        ("5", "M5", (300,)),
                        ("15", "M15", (900,)),
                    )
                ],
                [
                    InlineKeyboardButton(text="Запустить", callback_data="otc:start:0"),
                    InlineKeyboardButton(text="Остановить", callback_data="otc:stop"),
                ],
                [
                    InlineKeyboardButton(
                        text="Винрейт циклов", callback_data="otc:cyclestats:all:all"
                    ),
                    InlineKeyboardButton(
                        text="Обновить", callback_data="otc:panel:show"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Виртуальный баланс", callback_data="otc:wallet:show"
                    ),
                    InlineKeyboardButton(
                        text="📊 Стратегии", callback_data="c:sources"
                    ),
                ],
            ]
        )
    chosen = choices.get(chat_id, TIMEFRAMES)
    duration = (
        getattr(service, "session_minutes", None)
        if service and service.running()
        else None
    )
    buttons = []
    for key, label, value in [
        ("all", "Все M2/M5/M15", TIMEFRAMES),
        ("2", "M2", (120,)),
        ("5", "M5", (300,)),
        ("15", "M15", (900,)),
    ]:
        buttons.append(
            InlineKeyboardButton(
                text=("✅ " if chosen == value else "") + label,
                callback_data=f"otc:tf:{key}",
            )
        )
    rows = [buttons]
    for pair in [
        [(30, "30 минут"), (60, "1 час")],
        [(120, "2 часа"), (240, "4 часа")],
        [(480, "8 часов"), (0, "До остановки")],
    ]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=("✅ " if duration == value else "") + label,
                    callback_data=f"otc:start:{value}",
                )
                for value, label in pair
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="Остановить", callback_data="otc:stop"),
            InlineKeyboardButton(
                text="▴ Скрыть ботов" if expanded else "▾ Боты",
                callback_data="otc:panel:hide" if expanded else "otc:panel:show",
            ),
        ]
    )
    rows.append(
        [InlineKeyboardButton(text="📊 4 стратегии", callback_data="c:sources")]
    )
    if expanded:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Строгий M5 · WR", callback_data="otc:primarystats:5:all"
                ),
                InlineKeyboardButton(
                    text="Без чрезмерного движения · WR",
                    callback_data="otc:broadstats:5:all",
                ),
            ]
        )
        rows.append(
            [InlineKeyboardButton(text="Groq recovery", callback_data="otc:groqrecovery")]
        )
        rows.append(
            [InlineKeyboardButton(text="Обновить", callback_data="otc:panel:show")]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def strategy_status(current):
    if current and getattr(getattr(current, "config", None), "cycles_only", False):
        running = bool(current.running() and current.phase == "поток активен")
        preview = getattr(current, "cycle_preview", None)
        text = (
            "🟢" if running and preview and preview.enabled else "🔴"
        ) + " Боллинджер ×3"
        text += (
            "\n" + preview.journal.stats(since=stats_epoch())
            if preview
            else " · запуск"
        )
        recovery = getattr(current, "unknown_recovery", None)
        if recovery is not None:
            status = (
                recovery.groq.status()
                if recovery.groq is not None
                else "Groq recovery · без API-ключа"
            )
            text += (
                f"\n🟣 Recovery · восстановлено за проход {recovery.last_recovered}"
                f" · осталось символов {recovery.last_pending}\n{status}"
            )
            if getattr(recovery, "paused_for_signals", False):
                text += "\n⏸ Исторический UNKNOWN: пауза ради новых сигналов"
        return text
    streaming = bool(current and current.running() and current.phase == "поток активен")
    active = {
        "Основной": streaming,
        "Без чрезмерного движения M5": bool(
            streaming
            and getattr(current, "main_broad", None)
            and current.main_broad.enabled
        ),
    }
    since, now = stats_epoch(), time.time()
    lines = ["После сброса статистики · только отправленные сигналы"]
    for label, attr in SOURCES:
        preview = getattr(current, attr, None)
        records = read_journal(preview.journal, since, now) if preview else []
        count = len(records)
        wins = sum(r["outcome"] == "WIN" for r in records)
        losses = sum(r["outcome"] == "LOSS" for r in records)
        rate = f"{100 * wins / (wins + losses):.1f}%" if wins + losses else "—"
        detail = ""
        lines.append(
            f"{'🟢' if active[label] else '🔴'} {label} · {count} · WIN {wins} / LOSS {losses} · {rate}{detail}"
        )
    return "\n".join(lines)


def wallet_menu():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Изменить баланс", callback_data="otc:wallet:balance"
                ),
                InlineKeyboardButton(text="Ставки", callback_data="otc:wallet:stakes"),
                InlineKeyboardButton(
                    text="Выплата %", callback_data="otc:wallet:payout"
                ),
            ],
            [
                InlineKeyboardButton(text="Обновить", callback_data="otc:wallet:show"),
                InlineKeyboardButton(text="Меню", callback_data="otc:panel:hide"),
            ],
        ]
    )


@router.message(Command("virtual"))
async def virtual_account(message: types.Message):
    if not service or not hasattr(service, "cycle_preview"):
        await message.answer("Виртуальный счёт ещё не запущен.")
        return
    text = await asyncio.to_thread(service.cycle_preview.wallet.text)
    await message.answer(text, reply_markup=wallet_menu(), parse_mode=None)


@router.message(
    F.text,
    lambda message: (
        message.chat.id in wallet_edits and not message.text.startswith("/")
    ),
)
async def edit_virtual_account(message: types.Message):
    kind = wallet_edits.get(message.chat.id)
    if message.text.strip().lower() in ("отмена", "cancel"):
        wallet_edits.pop(message.chat.id, None)
        await virtual_account(message)
        return
    try:
        await asyncio.to_thread(
            service.cycle_preview.wallet.change, kind, message.text.strip()
        )
    except ValueError as exc:
        await message.answer(
            str(exc) + "\nДля отмены напишите «отмена».", parse_mode=None
        )
        return
    wallet_edits.pop(message.chat.id, None)
    await virtual_account(message)


MENU_TITLE = "OTC · Боллинджер ×3"


@router.message(Command("start", "help", "otc"))
async def show_menu(message: types.Message):
    await message.answer(MENU_TITLE, reply_markup=menu(message.chat.id))


@router.message(Command("otc_status"))
async def status(message: types.Message):
    indicators = await asyncio.to_thread(strategy_status, service)
    await message.answer(
        MENU_TITLE + "\n\n" + indicators, reply_markup=menu(message.chat.id, True)
    )


@router.message(Command("otc_stop"))
async def stop(message: types.Message):
    service.user_stopped = True
    (ROOT / "data" / "observer.stop").parent.mkdir(parents=True, exist_ok=True)
    (ROOT / "data" / "observer.stop").touch()
    stopped = await service.stop()
    await message.answer(
        ("OTC остановлен." if stopped else "OTC уже остановлен.")
        + " Будущие планы отменены; начавшиеся учитываются."
    )


@router.message(Command("otc_quality"))
async def quality(message: types.Message):
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if service and getattr(getattr(service, "config", None), "cycles_only", False):
        from services.otc_cycle_report import format_report, report

        data = await asyncio.to_thread(
            report,
            ROOT / "data",
            max(since.timestamp(), stats_epoch()),
            now.timestamp(),
        )
        text = format_report(data)
        for offset in range(0, len(text), 3500):
            await message.answer(text[offset : offset + 3500], parse_mode=None)
        return
    journals = {}
    for label, attr in (
        ("Основной", "main_ready"),
        ("Без чрезмерного движения M5", "main_broad"),
    ):
        preview = getattr(service, attr, None)
        if preview:
            journals[label] = preview.journal
    if not journals:
        await message.answer("Журналы сигналов недоступны.")
        return
    lines = await asyncio.to_thread(
        signal_quality_report,
        journals,
        max(since.timestamp(), stats_epoch()),
        now.timestamp(),
    )
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk, parse_mode=None)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk, parse_mode=None)


@router.message(Command("otc_research"))
async def research_quality(message: types.Message):
    from services.otc_candidate_report import format_report, report

    args = (message.text or "").split()
    period = args[1] if len(args) > 1 else "7d"
    periods = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
    if period not in periods:
        await message.answer("Период: 24h, 7d, 30d или all.")
        return
    now = time.time()
    since = now - periods[period] if periods[period] else 0
    if service and getattr(getattr(service, "config", None), "cycles_only", False):
        from services.otc_community_cycles import report as cycle_report
        from services.otc_community_cycles import text_report

        data = await asyncio.to_thread(
            cycle_report,
            ROOT / "data/community-cycles.db",
            max(since, shadow_stats_epoch()),
            now,
        )
        for seconds in (120, 300, 900):
            await message.answer(text_report(data, seconds), parse_mode=None)
        return
    data = await asyncio.to_thread(
        report,
        ROOT / "data/ready-candidates.db",
        ROOT / "data/market-history.db",
        since,
        now,
    )
    text = format_report(data)
    from services.otc_shadow_report import format_report as format_variants
    from services.otc_shadow_report import report as variants_report

    variants = await asyncio.to_thread(
        variants_report,
        ROOT / "data/ready-candidates.db",
        max(since, shadow_stats_epoch()),
        now,
    )
    text += "\n\n" + format_variants(variants)
    for offset in range(0, len(text), 3500):
        await message.answer(text[offset : offset + 3500], parse_mode=None)


@router.message(Command("otc_stats"))
async def stats(message: types.Message):
    if service and getattr(getattr(service, "config", None), "cycles_only", False):
        text = await asyncio.to_thread(
            service.cycle_preview.journal.stats, None, stats_epoch()
        )
        await message.answer(text, reply_markup=menu(message.chat.id), parse_mode=None)
        return
    args = (message.text or "").split()
    period = args[1] if len(args) > 1 else "all"
    durations = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
    if period not in durations:
        await message.answer("Используйте /otc_stats [24h|7d|30d|all]")
        return
    await message.answer("Выбери экспирацию:", reply_markup=stats_menu(period))


async def send_confirmations(message, period="all"):
    from services.otc_confirmation_report import format_report, read_records

    durations = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}
    if period not in durations:
        await message.answer("Используйте /otc_confirmations [24h|7d|30d|all]")
        return
    since = time.time() - durations[period] if durations[period] else 0
    records = await asyncio.to_thread(read_records, ROOT / "data", since)
    text = await asyncio.to_thread(format_report, records)
    # Reports can grow with strategy versions; keep Telegram messages bounded.
    chunk = ""
    for line in text.splitlines():
        if len(chunk) + len(line) + 1 > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await message.answer(chunk)


@router.message(Command("otc_confirmations"))
async def confirmations(message: types.Message):
    args = (message.text or "").split()
    await send_confirmations(message, args[1] if len(args) > 1 else "all")


def stats_menu(period="all", cycles=False):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"M{m}",
                    callback_data=f"otc:{'cycles' if cycles else 'stats'}:{m}:{period}",
                )
                for m in ((1, 5, 15) if cycles else (1, 2, 3, 5, 15))
            ],
            *(
                []
                if cycles
                else [
                    [
                        InlineKeyboardButton(
                            text="Все экспирации",
                            callback_data=f"otc:stats:all:{period}",
                        )
                    ]
                ]
            ),
            [
                InlineKeyboardButton(
                    text="Отдельные сигналы"
                    if cycles
                    else "Циклы ×4 · архив условных"
                    if settings.primary_mode == "ready"
                    else "Циклы ×4 · виртуально",
                    callback_data=f"otc:{'stats' if cycles else 'cycles'}:1:{period}",
                )
            ],
        ]
    )


@router.message(Command("otc_diagnostics"))
async def diagnostics(message: types.Message):
    # No API call: inspecting diagnostics must not consume the account quota.
    await message.answer(
        service.status()
        + ("\n" + service.ml_runtime.status() if hasattr(service, "ml_runtime") else "")
        + "\n\nOTCharts SDK 0.7.0; Pocket Option venue otc; время UTC+2 преобразуется в UTC. "
        "OTCharts передаёт названия, текущие выплаты и статус открытия. Время изменения выплаты API не передаёт; время получения хранится отдельно. "
        "В тестовом режиме эти неизвестные данные не блокируют сигнал; он получает явную пометку. "
        "Строгий режим требует подтверждения. Без live-потока тест использует редкие снимки свечей с бюджетом запросов. "
        "Ключ и права проверяются при запуске сеанса. "
        "При отсутствии доступа: «Нет доступа к потоковым котировкам/выплатам». "
        "Независимых цен платформы нет: совпадение не подтверждено, исход UNKNOWN. "
        "Просадка и walk-forward доступны в отчёте replay. Секреты не выводятся."
    )


@router.callback_query(F.data.startswith("otc:"))
async def callback(query: types.CallbackQuery):
    parts = (query.data or "").split(":")
    if len(parts) < 2:
        await query.answer("Неизвестное действие")
        return
    action = parts[1]
    chat = query.message.chat.id
    if action == "wallet" and len(parts) == 3:
        await query.answer()
        kind = parts[2]
        if kind in ("show", "cancel"):
            wallet_edits.pop(chat, None)
            await virtual_account(query.message)
        elif kind in ("balance", "stakes", "payout"):
            wallet_edits[chat] = kind
            prompt = {
                "balance": "Введите новый текущий виртуальный баланс в рублях, например 5000. Это ручная корректировка, а не торговая прибыль.",
                "stakes": "Введите три виртуальные ставки через пробел, например 100 250 625. Уже начатые циклы сохранят прежние ставки.",
                "payout": "Введите прибыль при WIN в процентах, например 92. Уже начатые циклы сохранят прежнюю выплату.",
            }[kind]
            await query.message.answer(
                prompt,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="Отмена", callback_data="otc:wallet:cancel"
                            )
                        ]
                    ]
                ),
            )
        return
    if action == "shadowcycles":
        # Older Telegram messages can still contain legacy buttons.
        await query.answer()
        from bot.community import overview_text, sources
        await query.message.answer(
            await asyncio.to_thread(overview_text),
            reply_markup=sources(), parse_mode=None,
        )
        return
    if action == "validation":
        await query.answer("XGBoost отключён", show_alert=True)
        return
    if action == "groqrecovery":
        await query.answer()
        recovery = getattr(service, "unknown_recovery", None)
        if recovery is None:
            await query.message.answer("Groq recovery ещё не запущен.")
            return
        status = (
            recovery.groq.status()
            if recovery.groq is not None
            else "Groq recovery · API-ключ не подключён"
        )
        await query.message.answer(
            "🟣 UNKNOWN recovery\n"
            f"Последний проход: восстановлено {recovery.last_recovered}\n"
            f"Осталось символов с неизвестными исходами: {recovery.last_pending}\n"
            + ("⏸ Архивная проверка на паузе до завершения торгового сеанса\n"
               if getattr(recovery, "paused_for_signals", False) else "")
            + status,
            parse_mode=None,
        )
        return
    if action == "shadow":
        await query.answer()
        from bot.community import overview_text, sources
        await query.message.answer(
            await asyncio.to_thread(overview_text),
            reply_markup=sources(), parse_mode=None,
        )
        return
    if action == "panel" and len(parts) == 3 and parts[2] in ("show", "hide"):
        await query.answer()
        expanded = parts[2] == "show"
        text = MENU_TITLE
        if expanded:
            text += "\n\n" + await asyncio.to_thread(strategy_status, service)
        keyboard = menu(chat, expanded)
        if query.message.text != text or query.message.reply_markup != keyboard:
            await query.message.edit_text(text, reply_markup=keyboard)
        return
    if action == "confirmations" and len(parts) == 2:
        await query.answer()
        await send_confirmations(query.message)
        return
    if action == "mlstats":
        await query.answer("XGBoost отключён", show_alert=True)
        return
    if action in ("groqstats", "primarystats", "broadstats", "cyclestats"):
        preview_name = {
            "cyclestats": "cycle_preview",
            "groqstats": "groq_preview",
            "primarystats": "main_ready",
            "broadstats": "main_broad",
        }[action]
        if (
            len(parts) not in (3, 4)
            or parts[2] not in ("all", "1", "2", "3", "5", "15")
            or (len(parts) == 4 and parts[3] not in ("today", "all"))
            or not hasattr(service, preview_name)
        ):
            await query.answer("Тест ещё не запущен")
            return
        await query.answer()
        period = parts[3] if len(parts) == 4 else "all"
        since = stats_epoch()
        if period == "today":
            midnight = datetime.now(ZoneInfo("Europe/Moscow")).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            since = max(since, midnight.timestamp())
        seconds = None if parts[2] == "all" else int(parts[2]) * 60
        text = await asyncio.to_thread(
            getattr(service, preview_name).journal.stats, seconds, since
        )
        if action == "groqstats":
            text += "\n" + await asyncio.to_thread(service.groq_preview.client.status)
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Все" if key == "all" else key + " мин",
                        callback_data="otc:" + action + ":" + key + ":" + period,
                    )
                    for key in (
                        ("all", "2", "5", "15")
                        if action == "cyclestats"
                        else ("all", "1", "2", "3", "5", "15")
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=("✅ " if period == key else "") + label,
                        callback_data=f"otc:{action}:{parts[2]}:{key}",
                    )
                    for key, label in (
                        ("today", "Сегодня МСК"),
                        ("all", "После сброса"),
                    )
                ],
            ]
        )
        if query.message.text != text:
            await query.message.answer(text, reply_markup=keyboard)
        return
    if action in ("stats", "cycles"):
        cycles = action == "cycles"
        if len(parts) == 2:
            await query.answer()
            await query.message.answer(
                "Выбери экспирацию:", reply_markup=stats_menu(cycles=cycles)
            )
        elif (
            len(parts) == 4
            and parts[2]
            in (("1", "5", "15") if cycles else ("all", "1", "2", "3", "5", "15"))
            and parts[3] in ("24h", "7d", "30d", "all")
        ):
            await query.answer()
            duration = {"24h": 86400, "7d": 604800, "30d": 2592000, "all": None}[
                parts[3]
            ]
            since = time.time() - duration if duration else 0
            if not cycles:
                since = max(since, stats_epoch())
            rows = await asyncio.to_thread(
                service.db.virtual_stats,
                since,
                None if parts[2] == "all" else int(parts[2]) * 60,
            )
            if cycles:
                cycle_stats = await asyncio.to_thread(
                    read_stats, service.db, int(parts[2]) * 60, since
                )
                text = format_cycles(cycle_stats, int(parts[2]))
                if settings.primary_mode == "ready":
                    text = "Архив условных планов\n" + text
            else:
                if getattr(
                    settings, "primary_mode", "conditional"
                ) == "ready" and hasattr(service, "main_ready"):
                    text = await asyncio.to_thread(
                        service.main_ready.journal.stats,
                        None if parts[2] == "all" else int(parts[2]) * 60,
                        since,
                    )
                else:
                    text = compact_stats(
                        rows, None if parts[2] == "all" else int(parts[2])
                    )
            if query.message.text != text:
                await query.message.edit_text(
                    text, reply_markup=stats_menu(parts[3], cycles=cycles)
                )
        else:
            await query.answer("Недопустимый выбор")
        return
    if action == "tf" and len(parts) == 3 and parts[2] in ("all", "2", "5", "15"):
        choices[chat] = TIMEFRAMES if parts[2] == "all" else (int(parts[2]) * 60,)
        if getattr(getattr(service, "config", None), "cycles_only", False) and hasattr(
            service, "cycle_preview"
        ):
            service.cycle_preview.allowed_timeframes = choices[chat]
        await query.answer("Экспирации выбраны")
        await query.message.edit_reply_markup(
            reply_markup=menu(chat, query.message.text != MENU_TITLE)
        )
    elif (
        action == "start"
        and len(parts) == 3
        and parts[2] in ("0", "30", "60", "120", "240", "480")
    ):
        started = await service.start(
            query.bot, chat, int(parts[2]), choices.get(chat, TIMEFRAMES)
        )
        await query.answer("Проверяю доступ" if started else "Сеанс уже запущен")
        keyboard = menu(chat, query.message.text != MENU_TITLE)
        if query.message.reply_markup != keyboard:
            await query.message.edit_reply_markup(reply_markup=keyboard)

    elif action == "observer_stop" and len(parts) == 2:
        (ROOT / "data" / "observer.stop").parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "observer.stop").touch()
        await query.answer("Наблюдатель останавливается")
    elif action == "stop" and len(parts) == 2:
        (ROOT / "data" / "observer.stop").parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "observer.stop").touch()
        service.user_stopped = True
        stopped = await service.stop()
        await query.answer("Остановлено" if stopped else "Уже остановлен")
        keyboard = menu(chat, query.message.text != MENU_TITLE)
        if query.message.reply_markup != keyboard:
            await query.message.edit_reply_markup(reply_markup=keyboard)
    elif action == "status" and len(parts) == 2:
        await query.answer()
        indicators = await asyncio.to_thread(strategy_status, service)
        await query.message.answer(
            MENU_TITLE + "\n\n" + indicators, reply_markup=menu(chat, True)
        )
    else:
        await query.answer("Недопустимый выбор")
