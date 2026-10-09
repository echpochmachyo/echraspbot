from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InaccessibleMessage,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv

from schedule_bot.drive import DriveScheduleSource
from schedule_bot.parser import parse_schedule
from schedule_bot.storage import ScheduleStore

logger = logging.getLogger(__name__)
DEFAULT_FOLDER_ID = "1kUYiSAafghhYR0ARyXwPW1HZPpHcFIag"


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    service_account_file: str
    drive_folder_id: str
    database_path: str
    poll_interval_seconds: int

    @classmethod
    def from_environment(cls) -> Settings:
        load_dotenv()
        bot_token = os.getenv("BOT_TOKEN", "").strip()
        service_account_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        if not bot_token:
            raise RuntimeError("BOT_TOKEN is required in the .env file")
        if not service_account_file:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_FILE is required in the .env file"
            )

        try:
            poll_interval_seconds = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
        except ValueError as error:
            raise RuntimeError("POLL_INTERVAL_SECONDS must be an integer") from error
        if poll_interval_seconds < 30:
            raise RuntimeError("POLL_INTERVAL_SECONDS must be at least 30")

        return cls(
            bot_token=bot_token,
            service_account_file=service_account_file,
            drive_folder_id=os.getenv("DRIVE_FOLDER_ID", DEFAULT_FOLDER_ID).strip(),
            database_path=os.getenv("DATABASE_PATH", "schedule_bot.sqlite3").strip(),
            poll_interval_seconds=poll_interval_seconds,
        )


def _notification_text(row: sqlite3.Row) -> str:
    lesson_date = datetime.strptime(row["lesson_date"], "%Y-%m-%d").strftime(
        "%d.%m.%Y"
    )
    return (
        "Добавлена или изменена пара в расписании:\n"
        f"Дата: {lesson_date}\n"
        f"Группа: {row['group_name']}\n"
        f"Пара: {row['pair_number']}\n"
        f"Кабинет: {row['room']}\n"
        f"Преподаватель: {row['teacher']}"
    )


async def send_pending_notifications(bot: Bot, store: ScheduleStore) -> None:
    for notification in store.pending_notifications():
        try:
            await bot.send_message(
                chat_id=notification["chat_id"],
                text=_notification_text(notification),
            )
        except TelegramAPIError:
            logger.exception(
                "Failed to send schedule notification %s to chat %s",
                notification["id"],
                notification["chat_id"],
            )
            continue
        store.mark_notification_delivered(notification["id"])
        await asyncio.sleep(0.05)


async def synchronize(
    source: DriveScheduleSource, store: ScheduleStore, bot: Bot
) -> None:
    drive_files = await asyncio.to_thread(source.list_schedule_files)
    current_file_ids = {item["id"] for item in drive_files}
    first_scan = not store.baseline_complete()
    scan_succeeded = True

    for item in drive_files:
        file_id = item["id"]
        name = item["name"]
        revision = item["revision"]
        if store.is_file_current(file_id, name, revision):
            continue

        try:
            content = await asyncio.to_thread(source.download, file_id)
            lessons = parse_schedule(name, content)
        except Exception:
            scan_succeeded = False
            logger.exception("Could not process schedule file %s (%s)", name, file_id)
            continue

        store.store_file(
            file_id=file_id,
            name=name,
            revision=revision,
            lessons=lessons,
            notify_changes=not first_scan,
        )
        logger.info("Indexed %d lessons from %s", len(lessons), name)

    store.remove_missing_files(current_file_ids)
    if first_scan and scan_succeeded:
        store.mark_baseline_complete()
        logger.info("Initial schedule baseline created; no historical alerts sent")

    await send_pending_notifications(bot, store)


async def _sync_loop(
    source: DriveScheduleSource,
    store: ScheduleStore,
    bot: Bot,
    interval_seconds: int,
) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await synchronize(source, store, bot)
        except Exception:
            logger.exception("Google Drive synchronization failed")


def _command_argument(message: Message) -> str:
    parts = (message.text or "").split(maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


GROUPS_PER_PAGE = 8


def _menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📅 Расписание", callback_data="open:schedule:0"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🗓 Расписание моих групп",
                    callback_data="open:my_schedule:0",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✅ Подписаться", callback_data="open:subscribe:0"
                )
            ],
            [
                InlineKeyboardButton(
                    text="👥 Мои подписки", callback_data="open:subscriptions:0"
                )
            ],
        ]
    )


def _groups_keyboard(
    groups: list[str],
    action: str,
    page: int,
    subscribed: set[str] | None = None,
) -> InlineKeyboardMarkup:
    start = page * GROUPS_PER_PAGE
    rows = []
    for index, group in enumerate(groups[start : start + GROUPS_PER_PAGE], start=start):
        if action == "schedule":
            text = group
            callback_action = action
        elif action == "subscriptions":
            text = f"❌ {group}"
            callback_action = "unsubscribe"
        else:
            is_subscribed = group.casefold() in (subscribed or set())
            text = f"{'✅' if is_subscribed else '➕'} {group}"
            callback_action = "toggle"
        rows.append(
            [
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"group:{callback_action}:{page}:{index}",
                )
            ]
        )

    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                text="⬅️", callback_data=f"open:{action}:{page - 1}"
            )
        )
    if start + GROUPS_PER_PAGE < len(groups):
        navigation.append(
            InlineKeyboardButton(
                text="➡️", callback_data=f"open:{action}:{page + 1}"
            )
        )
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data="open:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _replace_callback_message(
    callback: CallbackQuery, text: str, keyboard: InlineKeyboardMarkup
) -> None:
    await callback.answer()
    message = callback.message
    if message is not None:
        chat_id = message.chat.id
        if isinstance(message, Message):
            await message.delete()
        elif isinstance(message, InaccessibleMessage):
            logger.warning(
                "Could not delete inaccessible menu message in chat %s", chat_id
            )
        else:
            raise TypeError(f"Unsupported callback message type: {type(message)!r}")
        await callback.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    else:
        logger.error("Callback %s has no message to replace", callback.id)


def _schedule_text(store: ScheduleStore, groups: list[str]) -> str:
    today = date.today()
    lessons = store.lessons_for(groups, today, today + timedelta(days=6))
    if not lessons:
        return "Для выбранных групп на ближайшие 7 дней пар нет."

    lines = []
    current_date = ""
    for lesson in lessons:
        if lesson["lesson_date"] != current_date:
            current_date = lesson["lesson_date"]
            day = datetime.strptime(current_date, "%Y-%m-%d").strftime("%d.%m.%Y")
            lines.append(f"\n{day}")
        lines.append(
            f"{lesson['pair_number']} пара — {lesson['group_name']}, "
            f"каб. {lesson['room']}, {lesson['teacher']}"
        )
    return "\n".join(lines).strip()


def _my_schedule_text(store: ScheduleStore, chat_id: int) -> str:
    groups = store.subscriptions_for(chat_id)
    if not groups:
        return "У вас пока нет подписок. Выберите «Подписаться» в меню."
    return _schedule_text(store, groups)


def _register_handlers(dispatcher: Dispatcher, store: ScheduleStore) -> None:
    @dispatcher.message(Command("start", "help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Я слежу за расписанием занятий и сообщаю о новых парах "
            "для выбранных групп.\n\nВыберите действие:",
            reply_markup=_menu_keyboard(),
        )

    @dispatcher.callback_query()
    async def menu_callback(callback: CallbackQuery) -> None:
        data = callback.data or ""
        if callback.message is None:
            await callback.answer()
            logger.error("Menu callback %s has no chat message", callback.id)
            return
        chat_id = callback.message.chat.id
        today = date.today()

        if data == "open:menu":
            await _replace_callback_message(
                callback,
                "Я слежу за расписанием занятий. Выберите действие:",
                _menu_keyboard(),
            )
            return

        if data.startswith("open:"):
            parts = data.split(":")
            if len(parts) != 3:
                await callback.answer()
                return
            action = parts[1]
            try:
                page = max(0, int(parts[2]))
            except ValueError:
                await callback.answer()
                return

            subscribed: set[str] = set()
            if action == "schedule":
                groups = store.available_groups(today)
                title = "Выберите группу, чтобы посмотреть расписание:"
            elif action == "my_schedule":
                await _replace_callback_message(
                    callback, _my_schedule_text(store, chat_id), _menu_keyboard()
                )
                return
            elif action == "subscribe":
                groups = store.available_groups(today)
                subscribed = {
                    group.casefold()
                    for group in store.subscriptions_for(chat_id)
                }
                title = "Выберите группы для подписки (нажмите ещё раз, чтобы отменить):"
            elif action == "subscriptions":
                groups = store.subscriptions_for(chat_id)
                subscribed = set()
                title = "Нажмите на группу, чтобы отписаться:"
            else:
                await callback.answer()
                return

            page_count = max(1, (len(groups) + GROUPS_PER_PAGE - 1) // GROUPS_PER_PAGE)
            page = min(page, page_count - 1)
            await _replace_callback_message(
                callback,
                title if groups else "Список групп пуст.",
                _groups_keyboard(groups, action, page, subscribed),
            )
            return

        if data.startswith("group:"):
            parts = data.split(":")
            if len(parts) != 4:
                await callback.answer()
                return
            _, action, page_value, index_value = parts
            try:
                page = int(page_value)
                index = int(index_value)
            except ValueError:
                await callback.answer()
                return

            if action == "unsubscribe":
                groups = store.subscriptions_for(chat_id)
            else:
                groups = store.available_groups(today)
            if index < 0 or index >= len(groups) or index // GROUPS_PER_PAGE != page:
                await _replace_callback_message(
                    callback, "Список обновился. Выберите действие:", _menu_keyboard()
                )
                return

            group = groups[index]
            if action == "schedule":
                text = _schedule_text(store, [group])
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="⬅️ К группам",
                                callback_data=f"open:schedule:{page}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="🏠 В меню", callback_data="open:menu"
                            )
                        ],
                    ]
                )
            elif action == "toggle":
                current = {
                    name.casefold()
                    for name in store.subscriptions_for(chat_id)
                }
                if group.casefold() in current:
                    store.unsubscribe(chat_id, group)
                else:
                    store.subscribe(chat_id, group)
                current = {
                    name.casefold()
                    for name in store.subscriptions_for(chat_id)
                }
                text = "Выберите группы для подписки (нажмите ещё раз, чтобы отменить):"
                keyboard = _groups_keyboard(groups, "subscribe", page, current)
            elif action == "unsubscribe":
                store.unsubscribe(chat_id, group)
                groups = store.subscriptions_for(chat_id)
                page = min(page, max(0, (len(groups) - 1) // GROUPS_PER_PAGE))
                text = (
                    "Подписка отменена. Нажмите на группу, чтобы отписаться:"
                    if groups
                    else "У вас больше нет подписок."
                )
                keyboard = _groups_keyboard(groups, "subscriptions", page)
            else:
                await callback.answer()
                return

            await _replace_callback_message(callback, text, keyboard)
            return

        await callback.answer()

    @dispatcher.message(Command("groups"))
    async def groups_command(message: Message) -> None:
        groups = store.available_groups(date.today())
        if not groups:
            await message.answer("В расписании пока нет доступных групп.")
            return
        await message.answer("Группы:\n" + "\n".join(groups))

    @dispatcher.message(Command("subscribe"))
    async def subscribe_command(message: Message) -> None:
        group_name = _command_argument(message)
        if not group_name:
            await message.answer("Укажите группу: /subscribe СА-15")
            return

        groups = store.available_groups(date.today())
        canonical = {group.casefold(): group for group in groups}
        group = canonical.get(group_name.casefold())
        if group is None:
            await message.answer("Не нашёл такую группу. Проверьте список: /groups")
            return

        store.subscribe(message.chat.id, group)
        await message.answer(f"Подписка на группу {group} включена.")

    @dispatcher.message(Command("unsubscribe"))
    async def unsubscribe_command(message: Message) -> None:
        group_name = _command_argument(message)
        if not group_name:
            await message.answer("Укажите группу: /unsubscribe СА-15")
            return
        if store.unsubscribe(message.chat.id, group_name):
            await message.answer(f"Подписка на группу {group_name} отключена.")
        else:
            await message.answer(f"Подписка на группу {group_name} не найдена.")

    @dispatcher.message(Command("mygroups"))
    async def my_groups_command(message: Message) -> None:
        groups = store.subscriptions_for(message.chat.id)
        if not groups:
            await message.answer("У вас пока нет подписок. Список групп: /groups")
            return
        await message.answer("Ваши подписки:\n" + "\n".join(groups))

    @dispatcher.message(Command("schedule"))
    async def schedule_command(message: Message) -> None:
        argument = _command_argument(message)
        if argument:
            groups = [
                group
                for group in store.available_groups(date.today())
                if group.casefold() == argument.casefold()
            ]
            if not groups:
                await message.answer("Не нашёл такую группу. Проверьте список: /groups")
                return
        else:
            groups = store.subscriptions_for(message.chat.id)
            if not groups:
                await message.answer(
                    "Укажите группу (/schedule СА-15) или сначала подпишитесь."
                )
                return

        await message.answer(_schedule_text(store, groups))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_environment()
    source = DriveScheduleSource(
        settings.service_account_file, settings.drive_folder_id
    )
    store = ScheduleStore(settings.database_path)
    bot = Bot(token=settings.bot_token)
    dispatcher = Dispatcher()
    _register_handlers(dispatcher, store)

    try:
        await synchronize(source, store, bot)
        sync_task = asyncio.create_task(
            _sync_loop(source, store, bot, settings.poll_interval_seconds)
        )
        try:
            await dispatcher.start_polling(bot)
        finally:
            sync_task.cancel()
            await asyncio.gather(sync_task, return_exceptions=True)
    finally:
        await bot.session.close()
        store.close()


if __name__ == "__main__":
    asyncio.run(main())