from __future__ import annotations

from pathlib import Path
import sqlite3
from contextlib import contextmanager


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL,
                    file_hash TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    error TEXT,
                    horizontal_output TEXT,
                    vertical_output TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def is_done(self, file_hash: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state FROM files WHERE file_hash = ?", (file_hash,)
            ).fetchone()
            return row is not None and row[0] == "done"

    def get_by_hash(self, file_hash: str):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, file_path, state FROM files WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()
            return row

    def insert_file(self, file_path: str, file_hash: str, state: str = "pending") -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO files (file_path, file_hash, state) VALUES (?, ?, ?)",
                (file_path, file_hash, state),
            )
            return cur.lastrowid

    def update_state(self, file_id: int, state: str, error: str | None = None):
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE files SET state = ?, error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (state, error, file_id),
            )

    def set_outputs(self, file_id: int, horizontal: str, vertical: str):
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE files SET horizontal_output = ?, vertical_output = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (horizontal, vertical, file_id),
            )
