from datetime import date
import tempfile
import unittest
from pathlib import Path

from schedule_bot.parser import Lesson
from schedule_bot.storage import ScheduleStore


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ScheduleStore(
            str(Path(self.temp_dir.name) / "schedule.sqlite3")
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_new_lessons_queue_notifications_for_subscribed_group(self) -> None:
        first = Lesson(date(2026, 10, 7), 1, "316", "СА-15", "Преподаватель")
        added = Lesson(date(2026, 10, 7), 2, "318", "СА-15", "Другой преподаватель")
        self.store.subscribe(123, "СА-15")

        self.store.store_file("file", "07.10.2026.xlsx", "rev1", [first], False)
        self.store.mark_baseline_complete()
        self.store.store_file(
            "file", "07.10.2026.xlsx", "rev2", [first, added], True
        )

        pending = self.store.pending_notifications()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["chat_id"], 123)
        self.assertEqual(pending[0]["group_name"], "СА-15")
        self.assertEqual(
            [row["group_name"] for row in self.store.lessons_for(
                ["СА-15"], date(2026, 10, 7), date(2026, 10, 7)
            )],
            ["СА-15", "СА-15"],
        )

    def test_initial_index_does_not_queue_historical_lessons(self) -> None:
        lesson = Lesson(date(2026, 10, 7), 1, "316", "СА-15", "Преподаватель")
        self.store.subscribe(123, "СА-15")

        self.store.store_file("file", "07.10.2026.xlsx", "rev1", [lesson], False)

        self.assertEqual(self.store.pending_notifications(), [])


if __name__ == "__main__":
    unittest.main()
