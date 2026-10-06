from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Iterable

from schedule_bot.parser import Lesson


class ScheduleStore:
    def __init__(self, database_path: str) -> None:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS files (
                file_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                revision TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lessons (
                file_id TEXT NOT NULL REFERENCES files(file_id) ON DELETE CASCADE,
                signature TEXT NOT NULL,
                lesson_date TEXT NOT NULL,
                pair_number INTEGER NOT NULL,
                room TEXT NOT NULL,
                group_name TEXT NOT NULL,
                teacher TEXT NOT NULL,
                PRIMARY KEY (file_id, signature)
            );
            CREATE INDEX IF NOT EXISTS lessons_by_date_group
                ON lessons (lesson_date, group_name);
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id INTEGER NOT NULL,
                group_key TEXT NOT NULL,
                group_name TEXT NOT NULL,
                PRIMARY KEY (chat_id, group_key)
            );
            CREATE TABLE IF NOT EXISTS notification_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                file_id TEXT NOT NULL,
                signature TEXT NOT NULL,
                lesson_date TEXT NOT NULL,
                pair_number INTEGER NOT NULL,
                room TEXT NOT NULL,
                group_name TEXT NOT NULL,
                teacher TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                UNIQUE (chat_id, file_id, signature)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def baseline_complete(self) -> bool:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key = 'baseline_complete'"
        ).fetchone()
        return row is not None and row["value"] == "1"

    def mark_baseline_complete(self) -> None:
        self.connection.execute(
            """
            INSERT INTO settings (key, value) VALUES ('baseline_complete', '1')
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """
        )
        self.connection.commit()

    def is_file_current(self, file_id: str, name: str, revision: str) -> bool:
        row = self.connection.execute(
            "SELECT name, revision FROM files WHERE file_id = ?", (file_id,)
        ).fetchone()
        return row is not None and row["name"] == name and row["revision"] == revision

    def store_file(
        self,
        file_id: str,
        name: str,
        revision: str,
        lessons: Iterable[Lesson],
        notify_changes: bool,
    ) -> None:
        new_lessons = list(lessons)
        with self.connection:
            previous = {
                row["signature"]
                for row in self.connection.execute(
                    "SELECT signature FROM lessons WHERE file_id = ?", (file_id,)
                )
            }
            self.connection.execute(
                """
                INSERT INTO files (file_id, name, revision) VALUES (?, ?, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    name = excluded.name, revision = excluded.revision
                """,
                (file_id, name, revision),
            )
            self.connection.execute("DELETE FROM lessons WHERE file_id = ?", (file_id,))
            self.connection.executemany(
                """
                INSERT INTO lessons (
                    file_id, signature, lesson_date, pair_number, room, group_name, teacher
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        file_id,
                        lesson.signature,
                        lesson.lesson_date.isoformat(),
                        lesson.pair_number,
                        lesson.room,
                        lesson.group_name,
                        lesson.teacher,
                    )
                    for lesson in new_lessons
                ],
            )

            if notify_changes:
                added = [
                    lesson
                    for lesson in new_lessons
                    if lesson.signature not in previous
                ]
                for lesson in added:
                    subscribers = self.connection.execute(
                        "SELECT chat_id FROM subscriptions WHERE group_key = ?",
                        (lesson.group_name.casefold(),),
                    ).fetchall()
                    self.connection.executemany(
                        """
                        INSERT OR IGNORE INTO notification_queue (
                            chat_id, file_id, signature, lesson_date,
                            pair_number, room, group_name, teacher
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                subscriber["chat_id"],
                                file_id,
                                lesson.signature,
                                lesson.lesson_date.isoformat(),
                                lesson.pair_number,
                                lesson.room,
                                lesson.group_name,
                                lesson.teacher,
                            )
                            for subscriber in subscribers
                        ],
                    )

    def remove_missing_files(self, current_file_ids: set[str]) -> None:
        existing = self.connection.execute("SELECT file_id FROM files").fetchall()
        removed = [
            row["file_id"] for row in existing if row["file_id"] not in current_file_ids
        ]
        with self.connection:
            self.connection.executemany(
                "DELETE FROM files WHERE file_id = ?", [(file_id,) for file_id in removed]
            )
            self.connection.executemany(
                "DELETE FROM lessons WHERE file_id = ?", [(file_id,) for file_id in removed]
            )

    def subscribe(self, chat_id: int, group_name: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO subscriptions (chat_id, group_key, group_name)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, group_key) DO UPDATE SET
                    group_name = excluded.group_name
                """,
                (chat_id, group_name.casefold(), group_name),
            )

    def unsubscribe(self, chat_id: int, group_name: str) -> bool:
        with self.connection:
            cursor = self.connection.execute(
                "DELETE FROM subscriptions WHERE chat_id = ? AND group_key = ?",
                (chat_id, group_name.casefold()),
            )
        return cursor.rowcount > 0

    def subscriptions_for(self, chat_id: int) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT group_name FROM subscriptions
            WHERE chat_id = ? ORDER BY group_name
            """,
            (chat_id,),
        ).fetchall()
        return [row["group_name"] for row in rows]

    def available_groups(self, today: date) -> list[str]:
        rows = self.connection.execute(
            """
            SELECT DISTINCT group_name FROM lessons
            WHERE lesson_date >= ? ORDER BY group_name COLLATE NOCASE
            """,
            (today.isoformat(),),
        ).fetchall()
        return [row["group_name"] for row in rows]

    def lessons_for(
        self, group_names: Iterable[str], start_date: date, end_date: date
    ) -> list[sqlite3.Row]:
        names = list(group_names)
        if not names:
            return []
        placeholders = ",".join("?" for _ in names)
        return self.connection.execute(
            f"""
            SELECT DISTINCT lesson_date, pair_number, room, group_name, teacher
            FROM lessons
            WHERE group_name IN ({placeholders})
              AND lesson_date BETWEEN ? AND ?
            ORDER BY lesson_date, group_name, pair_number, room
            """,
            (*names, start_date.isoformat(), end_date.isoformat()),
        ).fetchall()

    def pending_notifications(self) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM notification_queue WHERE delivered = 0 ORDER BY id"
        ).fetchall()

    def mark_notification_delivered(self, notification_id: int) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE notification_queue SET delivered = 1 WHERE id = ?",
                (notification_id,),
            )

    def close(self) -> None:
        self.connection.close()
