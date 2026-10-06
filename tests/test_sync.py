from datetime import date
from io import BytesIO
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from main import synchronize
from schedule_bot.storage import ScheduleStore


def make_schedule(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "Пара 1"
    worksheet.append([date(2026, 10, 7), "Пара 1", "Примечание"])
    for row in rows:
        worksheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class FakeDriveSource:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.revision = "revision-1"

    def list_schedule_files(self) -> list[dict[str, str]]:
        return [
            {
                "id": "file-1",
                "name": "07.10.2026.xlsx",
                "revision": self.revision,
            }
        ]

    def download(self, file_id: str) -> bytes:
        self.assert_file_id(file_id)
        return self.content

    @staticmethod
    def assert_file_id(file_id: str) -> None:
        if file_id != "file-1":
            raise ValueError(f"Unexpected file ID: {file_id}")


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


class SynchronizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_scan_is_quiet_and_later_additions_are_sent_once(self) -> None:
        first_row = [316, "СА-15", "Преподаватель"]
        added_row = [318, "СА-15", "Другой преподаватель"]
        source = FakeDriveSource(make_schedule([first_row]))
        bot = FakeBot()

        with tempfile.TemporaryDirectory() as temp_dir:
            store = ScheduleStore(str(Path(temp_dir) / "schedule.sqlite3"))
            try:
                await synchronize(source, store, bot)
                self.assertEqual(bot.messages, [])

                store.subscribe(123, "СА-15")
                source.content = make_schedule([first_row, added_row])
                source.revision = "revision-2"
                await synchronize(source, store, bot)
                await synchronize(source, store, bot)

                self.assertEqual(len(bot.messages), 1)
                self.assertEqual(bot.messages[0][0], 123)
                self.assertIn("Кабинет: 318", bot.messages[0][1])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
