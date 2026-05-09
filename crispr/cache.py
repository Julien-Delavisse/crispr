"""SQLite-backed cache for incremental mutation testing."""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

DB_NAME = ".crispr-cache.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_hashes (
    filepath   TEXT PRIMARY KEY,
    source_sha TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS test_hash (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    tests_sha TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mutations (
    mutation_id TEXT PRIMARY KEY,
    filepath    TEXT NOT NULL,
    lineno      INTEGER NOT NULL,
    col_offset  INTEGER NOT NULL,
    operator    TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT,
    duration_s  REAL DEFAULT 0,
    output      TEXT DEFAULT '',
    source_sha  TEXT NOT NULL,
    tests_sha   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_mut_file   ON mutations(filepath);
CREATE INDEX IF NOT EXISTS idx_mut_status ON mutations(status);

CREATE TABLE IF NOT EXISTS coverage_baseline (
    key     TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
"""


def mutation_id(filepath: str, operator: str, lineno: int, col_offset: int, description: str) -> str:
    raw = f"{filepath}:{operator}:{lineno}:{col_offset}:{description}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def hash_source(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hash_tests(test_files: Sequence[Path]) -> str:
    h = hashlib.sha256()
    for f in sorted(test_files):
        h.update(str(f).encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def discover_test_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for py in sorted(root.rglob("*.py")):
        name = py.name
        if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
            if "__pycache__" not in py.parts:
                files.append(py)
    return files


@dataclass
class CachedResult:
    mutation_id: str
    filepath: str
    lineno: int
    col_offset: int
    operator: str
    description: str
    status: str | None
    duration_s: float
    output: str


class MutationCache:

    def __init__(self, project_root: Path) -> None:
        self.db_path = project_root / DB_NAME
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def get_source_sha(self, filepath: str) -> str | None:
        row = self._conn.execute(
            "SELECT source_sha FROM file_hashes WHERE filepath = ?", (filepath,)
        ).fetchone()
        return row[0] if row else None

    def set_source_sha(self, filepath: str, sha: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO file_hashes (filepath, source_sha) VALUES (?, ?)",
            (filepath, sha),
        )
        self._conn.commit()

    def get_tests_sha(self) -> str | None:
        row = self._conn.execute(
            "SELECT tests_sha FROM test_hash WHERE id = 1"
        ).fetchone()
        return row[0] if row else None

    def set_tests_sha(self, sha: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO test_hash (id, tests_sha) VALUES (1, ?)", (sha,)
        )
        self._conn.commit()

    def is_fresh(self, mid: str, source_sha: str, tests_sha: str) -> bool:
        row = self._conn.execute(
            "SELECT status, source_sha, tests_sha FROM mutations WHERE mutation_id = ?",
            (mid,),
        ).fetchone()
        if row is None or row[0] is None:
            return False
        return row[1] == source_sha and row[2] == tests_sha

    def get_result(self, mid: str) -> CachedResult | None:
        row = self._conn.execute(
            "SELECT mutation_id, filepath, lineno, col_offset, operator, "
            "description, status, duration_s, output FROM mutations "
            "WHERE mutation_id = ?",
            (mid,),
        ).fetchone()
        return CachedResult(*row) if row else None

    def store_result(
        self, mid: str, filepath: str, lineno: int, col_offset: int,
        operator: str, description: str, status: str, duration_s: float,
        output: str, source_sha: str, tests_sha: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO mutations "
            "(mutation_id, filepath, lineno, col_offset, operator, description, "
            "status, duration_s, output, source_sha, tests_sha) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, filepath, lineno, col_offset, operator, description,
             status, duration_s, output, source_sha, tests_sha),
        )
        self._conn.commit()

    def survivors(self) -> list[CachedResult]:
        rows = self._conn.execute(
            "SELECT mutation_id, filepath, lineno, col_offset, operator, "
            "description, status, duration_s, output FROM mutations "
            "WHERE status = 'survived' ORDER BY filepath, lineno",
        ).fetchall()
        return [CachedResult(*r) for r in rows]

    def all_results(self, filepath: str | None = None) -> list[CachedResult]:
        if filepath:
            rows = self._conn.execute(
                "SELECT mutation_id, filepath, lineno, col_offset, operator, "
                "description, status, duration_s, output FROM mutations "
                "WHERE filepath = ? ORDER BY lineno",
                (filepath,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT mutation_id, filepath, lineno, col_offset, operator, "
                "description, status, duration_s, output FROM mutations "
                "ORDER BY filepath, lineno",
            ).fetchall()
        return [CachedResult(*r) for r in rows]

    def get_coverage_payload(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT payload FROM coverage_baseline WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def set_coverage_payload(self, key: str, payload: str) -> None:
        self._conn.execute(
            "DELETE FROM coverage_baseline WHERE key != ?", (key,)
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO coverage_baseline (key, payload) VALUES (?, ?)",
            (key, payload),
        )
        self._conn.commit()

    def stats(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) FROM mutations GROUP BY status"
        ).fetchall()
        return {s: c for s, c in rows}

    def clear(self) -> None:
        self._conn.executescript(
            "DELETE FROM mutations; DELETE FROM file_hashes; "
            "DELETE FROM test_hash; DELETE FROM coverage_baseline;"
        )
