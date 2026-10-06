from datetime import date
from io import BytesIO
import unittest

from openpyxl import Workbook

from schedule_bot.parser import parse_schedule


def make_schedule() -> bytes:
    workbook = Workbook()
    first = workbook.active
    first.title = "Пара 1"
    first.append([date(2026, 10, 6), "Пара 1", "Примечание"])
    first.append([316, "СА-15", "Путинцева Ю.М.", 318, "РУП-14", "Фищук А.И."])
    first.append([110, "ремонт", "ремонт", 320, "СА-13", "Наливайко Е.П."])

    second = workbook.create_sheet("Пара 2")
    second.append([date(2026, 10, 6), "Пара 2", "Примечание"])
    second.append(["110а", "ИКС-14", "Пустоветова С.Ю."])

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


class ParseScheduleTests(unittest.TestCase):
    def test_reads_both_blocks_and_pair_tabs(self) -> None:
        lessons = parse_schedule("06.10.2026.xlsx", make_schedule())

        self.assertEqual(
            [
                (lesson.lesson_date, lesson.pair_number, lesson.room, lesson.group_name)
                for lesson in lessons
            ],
            [
                (date(2026, 10, 6), 1, "316", "СА-15"),
                (date(2026, 10, 6), 1, "318", "РУП-14"),
                (date(2026, 10, 6), 1, "320", "СА-13"),
                (date(2026, 10, 6), 2, "110а", "ИКС-14"),
            ],
        )

    def test_rejects_filename_without_a_date(self) -> None:
        with self.assertRaisesRegex(ValueError, "has no date"):
            parse_schedule("schedule.xlsx", make_schedule())

    def test_rejects_invalid_calendar_date(self) -> None:
        with self.assertRaises(ValueError):
            parse_schedule("31.02.2026.xlsx", make_schedule())


if __name__ == "__main__":
    unittest.main()
