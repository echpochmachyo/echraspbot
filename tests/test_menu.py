import unittest
from datetime import date, timedelta
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from main import (
    GROUPS_PER_PAGE,
    _groups_keyboard,
    _menu_keyboard,
    _my_schedule_text,
    _replace_callback_message,
)
from schedule_bot.parser import Lesson
from schedule_bot.storage import ScheduleStore


class InlineMenuTests(unittest.TestCase):
    def test_main_menu_offers_schedule_subscribe_and_subscriptions(self) -> None:
        keyboard = _menu_keyboard()
        callback_data = [
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        ]

        self.assertEqual(
            callback_data,
            [
                "open:schedule:0",
                "open:my_schedule:0",
                "open:subscribe:0",
                "open:subscriptions:0",
            ],
        )

    def test_group_selection_pages_and_marks_active_subscriptions(self) -> None:
        groups = [f"ГРУППА-{index}" for index in range(GROUPS_PER_PAGE + 1)]
        keyboard = _groups_keyboard(groups, "subscribe", 0, {"группа-2"})
        first_page_buttons = [
            row[0] for row in keyboard.inline_keyboard[:GROUPS_PER_PAGE]
        ]

        self.assertEqual(first_page_buttons[2].text, "✅ ГРУППА-2")
        self.assertEqual(first_page_buttons[3].text, "➕ ГРУППА-3")
        self.assertEqual(
            keyboard.inline_keyboard[-2][0].callback_data, "open:subscribe:1"
        )
        self.assertEqual(
            keyboard.inline_keyboard[-1][0].callback_data, "open:menu"
        )

    def test_my_schedule_includes_every_subscribed_group_without_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScheduleStore(str(Path(temp_dir) / "schedule.sqlite3"))
            try:
                tomorrow = date.today() + timedelta(days=1)
                lessons = [
                    Lesson(tomorrow, 1, "316", "СА-15", "Петров П.П."),
                    Lesson(tomorrow, 2, "318", "СА-16", "Иванов И.И."),
                ]
                store.store_file(
                    "file", "schedule.xlsx", "revision", lessons, notify_changes=False
                )
                store.subscribe(123, "СА-15")
                store.subscribe(123, "СА-16")

                text = _my_schedule_text(store, 123)

                self.assertIn("СА-15", text)
                self.assertIn("СА-16", text)
                self.assertIn("Петров П.П.", text)
                self.assertIn("Иванов И.И.", text)
            finally:
                store.close()

    def test_my_schedule_prompts_user_to_subscribe_when_no_subscriptions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScheduleStore(str(Path(temp_dir) / "schedule.sqlite3"))
            try:
                self.assertIn("Подписаться", _my_schedule_text(store, 123))
            finally:
                store.close()


class CallbackReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def test_click_silently_answers_deletes_and_replaces_old_message(self) -> None:
        events: list[str] = []

        class FakeMessage:
            chat = SimpleNamespace(id=123)

            async def delete(self) -> None:
                events.append("delete")

        class FakeBot:
            async def send_message(self, **kwargs: object) -> None:
                events.append("send")

        class FakeCallback:
            id = "callback-id"
            message = FakeMessage()
            bot = FakeBot()

            async def answer(self) -> None:
                events.append("silent-answer")

        with patch("main.Message", FakeMessage):
            await _replace_callback_message(
                FakeCallback(), "Следующий экран", _menu_keyboard()
            )

        self.assertEqual(events, ["silent-answer", "delete", "send"])


if __name__ == "__main__":
    unittest.main()
