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
from aiogram.types import Message
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


def _register_handlers(dispatcher: Dispatcher, store: ScheduleStore) -> None:
    @dispatcher.message(Command("start", "help"))
    async def help_command(message: Message) -> None:
        await message.answer(
            "Я слежу за расписанием занятий и сообщаю о новых парах для выбранных групп.\n\n"
            "/groups — группы в текущем расписании\n"
            "/subscribe ГРУППА — подписаться\n"
            "/unsubscribe ГРУППА — отписаться\n"
            "/mygroups — мои подписки\n"
            "/schedule [ГРУППА] — пары на ближайшие 7 дней"
        )

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

        today = date.today()
        lessons = store.lessons_for(groups, today, today + timedelta(days=6))
        if not lessons:
            await message.answer("Для выбранных групп на ближайшие 7 дней пар нет.")
            return

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
        await message.answer("\n".join(lines).strip())


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