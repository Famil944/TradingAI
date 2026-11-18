"""Owner-only personal announcements and configurable subscriber polls."""
import asyncio
import logging

from aiogram import F, Router, types
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config.settings import ROOT, settings
from services.otc_announcements import AnnouncementStore
from services.otc_subscribers import Subscribers

router = Router(name="announcements")
LOG = logging.getLogger(__name__)
drafts = {}
running = {}
_store = None
_subscribers = None


def store():
    global _store
    if _store is None:
        _store = AnnouncementStore(ROOT / "data" / "announcements.db")
    return _store


def subscribers():
    global _subscribers
    if _subscribers is None:
        _subscribers = Subscribers(ROOT / "data" / "subscribers.db")
    return _subscribers


def owner(user, message):
    return bool(user and message and message.chat.type == "private"
                and user.id == settings.admin_chat_id
                and message.chat.id == settings.admin_chat_id)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title, callback_data=action) for title, action in line]
        for line in rows
    ])


def menu():
    return kb([
        [("✉️ Написать всем", "a:new:notice")],
        [("📊 Создать голосование", "a:new:poll")],
        [("📋 История и результаты", "a:list")],
    ])


async def open_menu(message):
    await message.answer(
        "📢 Сообщения подписчикам\n"
        "Ты сам пишешь текст и варианты голосования. Получатели — только "
        "одобренные активные подписчики, даже если их часовой сеанс сигналов "
        "не включён. Торговые сигналы не меняются.",
        reply_markup=menu(), parse_mode=None,
    )


def vote_keyboard(aid, options, selected=None):
    return kb([
        [(("✅ " if index == selected else "☐ ") + f"{index + 1}. " + option[:51],
          f"a:vote:{aid}:{index}")]
        for index, option in enumerate(options)
    ])


def display(entry):
    if entry["kind"] == "notice":
        return "📢 ОБЪЯВЛЕНИЕ TRADINGAI\n\n" + entry["body"]
    choices = "\n".join(f"{i}. {option}" for i, option in enumerate(entry["options"], 1))
    return ("📊 ОПРОС TRADINGAI\n\n" + entry["body"]
            + "\n\n" + choices
            + "\n\nВыбери один вариант ответа. Свой голос можно изменить.")


def summary_text(info):
    if not info:
        return "Не найдено."
    counts = info["delivery"]
    title = "📊 Голосование" if info["kind"] == "poll" else "✉️ Объявление"
    lines = [
        f"{title} №{info['id']}",
        info["body"],
        "",
        "Доставка: ✅{sent} · ожидание {pending} · ошибки {failed} · пропущено {skipped}".format(
            **{name: counts.get(name, 0) for name in ("sent", "pending", "failed", "skipped")}
        ),
    ]
    if info["kind"] == "poll":
        total = sum(info["votes"])
        lines.append(f"Проголосовали: {total}")
        for option, count in zip(info["options"], info["votes"]):
            pct = 100 * count / total if total else 0
            lines.append(f"• {option}: {count} ({pct:.0f}%)")
    return "\n".join(lines)


async def active_ids():
    """Snapshot recipients once; later subscribers never inherit older messages."""
    ids, offset, now = [], 0, __import__("time").time()
    while True:
        part = await asyncio.to_thread(subscribers().active, 200, offset, now)
        if not part:
            return ids
        ids.extend(int(chat_id) for chat_id, _ in part)
        offset += len(part)


async def deliver(bot, aid):
    entry = await asyncio.to_thread(store().get, aid)
    if not entry:
        return
    try:
        while True:
            batch = await asyncio.to_thread(store().pending, aid, 100)
            if not batch:
                break
            for chat_id in batch:
                # Subscription can expire during a large broadcast.
                status = await asyncio.to_thread(subscribers().status, chat_id)
                if status["state"] != "approved":
                    await asyncio.to_thread(store().delivery_state, aid, chat_id, "skipped")
                    continue
                for attempt in range(2):
                    try:
                        await bot.send_message(
                            chat_id, display(entry),
                            reply_markup=(vote_keyboard(aid, entry["options"])
                                          if entry["kind"] == "poll" else None),
                            parse_mode=None,
                        )
                        await asyncio.to_thread(store().delivery_state, aid, chat_id, "sent")
                        break
                    except TelegramRetryAfter as exc:
                        if attempt == 0:
                            await asyncio.sleep(min(float(exc.retry_after) + 1, 30))
                            continue
                        LOG.warning("Announcement %d rate-limited", aid)
                        await asyncio.to_thread(store().delivery_state, aid, chat_id, "failed")
                    except TelegramForbiddenError:
                        await asyncio.to_thread(store().delivery_state, aid, chat_id, "skipped")
                        break
                    except (TelegramBadRequest, Exception) as exc:
                        LOG.warning("Announcement %d delivery failed: %s", aid, type(exc).__name__)
                        await asyncio.to_thread(store().delivery_state, aid, chat_id, "failed")
                        break
                # Low background send rate to avoid crowding live trading signals.
                await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.exception("Announcement worker crashed: %d; pending recipients preserved", aid)
    finally:
        try:
            info = await asyncio.to_thread(store().summary, aid)
            if info:
                await bot.send_message(
                    settings.admin_chat_id,
                    summary_text(info),
                    reply_markup=kb([[("📋 Результаты", f"a:result:{aid}")]]),
                    parse_mode=None,
                )
        except Exception:
            LOG.exception("Cannot report announcement delivery")


def spawn(bot, aid):
    if aid in running and not running[aid].done():
        return

    async def work():
        try:
            await deliver(bot, aid)
        finally:
            running.pop(aid, None)

    running[aid] = asyncio.create_task(work())


async def resume_pending(bot):
    for aid in await asyncio.to_thread(store().pending_ids):
        spawn(bot, aid)


async def stop_workers():
    tasks = list(running.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def option_controls():
    """Buttons for the owner's step-by-step poll composer."""
    return kb([
        [("✅ Готово — посмотреть опрос", "a:done")],
        [("↩️ Удалить последний ответ", "a:undo")],
        [("✏️ Изменить вопрос", "a:question")],
        [("❌ Отмена", "a:cancel")],
    ])


def preview_controls(kind):
    rows = [[("📤 Отправить подписчикам", "a:send")],
            [("✏️ Изменить текст вопроса" if kind == "poll"
              else "✏️ Изменить сообщение", "a:question")]]
    if kind == "poll":
        rows.append([("↩️ Вернуться к вариантам", "a:back")])
    rows.append([("❌ Отмена", "a:cancel")])
    return kb(rows)


def options_status(draft):
    """Clear instruction with every collected answer visible."""
    options = draft.get("options", [])
    listing = "\n".join(f"{i}. {item}" for i, item in enumerate(options, 1))
    return (
        f"📊 Шаг 2 из 3. Ответов добавлено: {len(options)}/8\n\n"
        + (listing + "\n\n" if listing else "")
        + "Отправь следующий вариант отдельным сообщением.\n"
        + "Или вставь несколько вариантов, каждый с новой строки — "
        + "пустые строки не мешают.\n\n"
        + "Когда добавишь хотя бы 2 ответа, нажми «✅ Готово». "
        + "Отправка подписчикам будет только после предпросмотра."
    )


def append_options(draft, content):
    """Accept one answer, multiline paste, and blank-separated answers."""
    incoming = [line.strip() for line in content.splitlines() if line.strip()]
    if not incoming:
        raise ValueError("Напиши хотя бы один вариант ответа.")
    options = draft.setdefault("options", [])
    merged = options + incoming
    if len(merged) > 8:
        raise ValueError(
            f"Можно добавить максимум 8 ответов. Уже есть {len(options)}, "
            f"новых в сообщении — {len(incoming)}."
        )
    if any(len(item) > 75 for item in merged):
        raise ValueError("Один из ответов длиннее 75 символов. Сократи его.")
    if len({item.casefold() for item in merged}) != len(merged):
        raise ValueError("Есть одинаковые ответы. Напиши разные варианты.")
    options.extend(incoming)
    return len(options)


async def show_preview(message, draft):
    draft["phase"] = "confirm"
    await message.answer(
        "👁 Шаг 3 из 3. Проверь текст и ответы.\n"
        "Подписчики пока ничего не получили.\n\n" + display(draft),
        reply_markup=preview_controls(draft["kind"]),
        parse_mode=None,
    )


@router.message(Command("announce"))
async def announce_command(message: types.Message):
    if owner(message.from_user, message):
        await open_menu(message)


@router.message(Command("cancel"), lambda m: owner(m.from_user, m)
                and m.from_user.id in drafts)
async def cancel_command(message: types.Message):
    drafts.pop(message.from_user.id, None)
    await message.answer("Отменено.", reply_markup=menu(), parse_mode=None)


@router.message(F.text, lambda m: owner(m.from_user, m)
                and m.from_user.id in drafts and not m.text.startswith("/"))
async def compose(message: types.Message):
    draft = drafts[message.from_user.id]
    content = (message.text or "").strip()
    phase = draft["phase"]
    if phase == "body":
        if not 1 <= len(content) <= 1000:
            await message.answer("Текст должен содержать от 1 до 1000 символов.")
            return
        draft["body"] = content
        if draft["kind"] == "poll":
            draft["phase"] = "options"
            draft.setdefault("options", [])
            await message.answer(options_status(draft),
                                 reply_markup=option_controls(), parse_mode=None)
        else:
            draft["options"] = []
            await show_preview(message, draft)
        return
    if phase == "options":
        try:
            append_options(draft, content)
        except ValueError as exc:
            await message.answer("⚠️ " + str(exc) + "\n\n" + options_status(draft),
                                 reply_markup=option_controls(), parse_mode=None)
            return
        await message.answer(
            "✅ Ответ сохранён.\n\n" + options_status(draft),
            reply_markup=option_controls(), parse_mode=None,
        )
        return
    if phase == "confirm":
        await message.answer(
            "Предпросмотр уже готов. Нажми «📤 Отправить» "
            "или «✏️ Изменить», если хочешь исправить текст.",
            reply_markup=preview_controls(draft["kind"]), parse_mode=None,
        )


@router.callback_query(F.data.startswith("a:"))
async def callbacks(query: types.CallbackQuery):
    message = query.message
    if not message or message.chat.type != "private":
        await query.answer("Только в личном чате", show_alert=True)
        return
    fields = (query.data or "").split(":")
    action = fields[1] if len(fields) > 1 else ""
    if action == "vote":
        if (len(fields) != 4 or not fields[2].isdigit() or not fields[3].isdigit()
                or query.from_user.id != message.chat.id):
            await query.answer("Недоступно", show_alert=True)
            return
        aid, index = int(fields[2]), int(fields[3])
        status = await asyncio.to_thread(subscribers().status, query.from_user.id)
        if status["state"] != "approved":
            await query.answer("Доступ закончился", show_alert=True)
            return
        ok = await asyncio.to_thread(store().vote, aid, query.from_user.id, index)
        if not ok:
            await query.answer("Опрос недоступен", show_alert=True)
            return
        await query.answer("✅ Голос учтён")
        entry = await asyncio.to_thread(store().get, aid)
        try:
            await message.edit_reply_markup(
                reply_markup=vote_keyboard(aid, entry["options"], selected=index)
            )
        except TelegramBadRequest:
            pass
        return

    if not owner(query.from_user, message):
        await query.answer("Только для владельца", show_alert=True)
        return
    user_id = query.from_user.id
    if action == "menu":
        await query.answer()
        await open_menu(message)
    elif action == "new" and len(fields) == 3 and fields[2] in ("notice", "poll"):
        drafts[user_id] = {"kind": fields[2], "phase": "body"}
        await query.answer()
        await message.answer(
            ("📊 Шаг 1 из 3. Напиши вопрос для голосования одним сообщением.\n"
             "Например: «Стоит ли продолжать развивать TradingAI?»"
             if fields[2] == "poll" else
             "✉️ Шаг 1 из 2. Напиши объявление одним сообщением.")
            + "\n\nОтмена: /cancel",
            reply_markup=kb([[("❌ Отмена", "a:cancel")]]), parse_mode=None,
        )
    elif action == "done":
        draft = drafts.get(user_id)
        if not draft or draft.get("kind") != "poll" or draft.get("phase") != "options":
            await query.answer("Сначала создай голосование", show_alert=True)
            return
        if len(draft.get("options", [])) < 2:
            await query.answer("Добавь ещё ответы: нужно минимум 2", show_alert=True)
            return
        await query.answer()
        await show_preview(message, draft)
    elif action == "undo":
        draft = drafts.get(user_id)
        if not draft or draft.get("kind") != "poll" or draft.get("phase") != "options":
            await query.answer("Сейчас нечего удалять", show_alert=True)
            return
        if not draft.get("options"):
            await query.answer("Ответов пока нет", show_alert=True)
            return
        removed = draft["options"].pop()
        await query.answer("Последний ответ удалён")
        await message.answer(
            "↩️ Удалено: " + removed + "\n\n" + options_status(draft),
            reply_markup=option_controls(), parse_mode=None,
        )
    elif action == "back":
        draft = drafts.get(user_id)
        if not draft or draft.get("kind") != "poll" or draft.get("phase") != "confirm":
            await query.answer("Черновик не найден", show_alert=True)
            return
        draft["phase"] = "options"
        await query.answer()
        await message.answer(options_status(draft),
                             reply_markup=option_controls(), parse_mode=None)
    elif action == "question":
        draft = drafts.get(user_id)
        if not draft:
            await query.answer("Черновик не найден", show_alert=True)
            return
        draft["phase"] = "body"
        await query.answer()
        await message.answer(
            "✏️ Отправь новый текст вопроса или объявления одним сообщением. "
            "Уже добавленные варианты ответа сохранятся.\nОтмена: /cancel",
            reply_markup=kb([[("❌ Отмена", "a:cancel")]]), parse_mode=None,
        )
    elif action == "cancel":
        drafts.pop(user_id, None)
        await query.answer("Отменено")
        await message.answer("Создание отменено.", reply_markup=menu(), parse_mode=None)
    elif action == "send":
        draft = drafts.get(user_id)
        if not draft or draft.get("phase") != "confirm":
            await query.answer("Предпросмотр устарел", show_alert=True)
            return
        # Remove before any await: duplicate taps must never send two copies.
        drafts.pop(user_id, None)
        targets = await active_ids()
        if not targets:
            drafts[user_id] = draft
            await query.answer("Нет активных подписчиков", show_alert=True)
            return
        try:
            aid = await asyncio.to_thread(
                store().create, draft["kind"], draft["body"], draft["options"], targets
            )
        except ValueError as exc:
            drafts[user_id] = draft
            await query.answer(str(exc), show_alert=True)
            return
        await query.answer("Отправка началась")
        await message.answer(
            f"✅ Рассылка №{aid} запущена для {len(targets)} подписчиков. "
            "Итоги доставки придут сюда.",
            parse_mode=None,
        )
        spawn(query.bot, aid)
    elif action == "list":
        entries = await asyncio.to_thread(store().recent, 10)
        await query.answer()
        if not entries:
            await message.answer("Рассылок пока нет.", reply_markup=menu())
            return
        await message.answer(
            "📋 Последние объявления и опросы:",
            reply_markup=kb(
                [[(f"№{e['id']} · " + ("📊 " if e["kind"] == "poll" else "✉️ ")
                    + e["body"][:28], f"a:result:{e['id']}")] for e in entries]
                + [[("◀ Назад", "a:menu")]]
            ),
        )
    elif action == "result" and len(fields) == 3 and fields[2].isdigit():
        aid = int(fields[2])
        info = await asyncio.to_thread(store().summary, aid)
        await query.answer()
        if not info:
            await message.answer("Не найдено.")
            return
        await message.answer(
            summary_text(info),
            reply_markup=kb([
                [("🔁 Повторить ошибки доставки", f"a:retry:{aid}")],
                [("◀ История", "a:list")],
            ]), parse_mode=None,
        )
    elif action == "retry" and len(fields) == 3 and fields[2].isdigit():
        aid = int(fields[2])
        if await asyncio.to_thread(store().get, aid) is None:
            await query.answer("Не найдено", show_alert=True)
            return
        count = await asyncio.to_thread(store().retry_failed, aid)
        spawn(query.bot, aid)
        await query.answer(f"Повторных получателей: {count}")
    else:
        await query.answer("Неизвестное действие")
