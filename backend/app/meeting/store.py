# ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ========= Copyright 2025-2026 @ Eigent.ai All Rights Reserved. =========

"""Local persistence for meeting runs.

`put` writes the run snapshot and does not replace the event log.
`append_events` is the only writer of replay events. Review-token gate
rows live in the same SQLite file as the runs; the HMAC secret does not.

`get`, `put`, `list`, and `append_events` accept ``user_id``. v1 stores
that id on the run and does not filter by it.

# Extension: multi-user
See docs/multi-user-extension.md.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Protocol

from app.meeting.identity import LOCAL_USER_ID, ignore_user_scope
from app.meeting.review_token import TokenRecord

logger = logging.getLogger("meeting.store")

MEETING_DB_ENV = "MEETING_DB_PATH"


@dataclass
class Run:
    id: str
    status: str
    created_at: str
    updated_at: str
    title: str | None = None
    # Extension: multi-user — v1 writes LOCAL_USER_ID only.
    user_id: str = LOCAL_USER_ID
    live_active: bool = False
    input_kind: str | None = None
    input_hash: str | None = None
    audio_filename: str | None = None
    audio_bytes: int = 0
    transcript: str | None = None
    transcript_hash: str | None = None
    minutes: dict[str, Any] | None = None
    todos: list[dict[str, Any]] | None = None
    final_minutes: dict[str, Any] | None = None
    final_todos: list[dict[str, Any]] | None = None
    content_hash: str | None = None
    approved: bool = False
    active_jti: str | None = None
    feishu_receipts: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    seq: int = 0


class RunStore(Protocol):
    def get(
        self, run_id: str, *, user_id: str | None = None
    ) -> Run | None: ...

    def put(self, run: Run, *, user_id: str | None = None) -> None: ...

    def list(self, *, user_id: str | None = None) -> list[Run]: ...

    def append_events(
        self,
        run_id: str,
        events: list[dict[str, Any]],
        *,
        user_id: str | None = None,
    ) -> None: ...


def meeting_db_path() -> Path:
    """SQLite file for meeting runs.

    ``MEETING_DB_PATH`` overrides the location. A relative override is
    resolved from the process working directory. When unset, the file is
    ``data/meeting.db`` under the backend package directory.
    """

    raw = os.environ.get(MEETING_DB_ENV, "").strip()
    if raw:
        return Path(raw).expanduser()
    backend_dir = Path(__file__).resolve().parents[2]
    return backend_dir / "data" / "meeting.db"


def _snapshot_payload(run: Run) -> dict[str, Any]:
    return {
        field_info.name: getattr(run, field_info.name)
        for field_info in fields(run)
        if field_info.name != "events"
    }


def _run_from_parts(
    payload: dict[str, Any], events: list[dict[str, Any]]
) -> Run:
    data = dict(payload)
    data.pop("events", None)
    data["events"] = events
    if events:
        data["seq"] = max(
            int(data.get("seq") or 0),
            max(int(event["seq"]) for event in events),
        )
    return Run(**data)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class InMemoryRunStore:
    """Process-local store. Tests use this; the API uses SQLite."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def get(self, run_id: str, *, user_id: str | None = None) -> Run | None:
        # Extension: multi-user — user_id is not a filter in v1.
        ignore_user_scope(user_id)
        with self._lock:
            stored = self._runs.get(run_id)
            if stored is None:
                return None
            return copy.deepcopy(stored)

    def put(self, run: Run, *, user_id: str | None = None) -> None:
        # Extension: multi-user — user_id is not a filter in v1.
        ignore_user_scope(user_id)
        with self._lock:
            existing = self._runs.get(run.id)
            saved = copy.deepcopy(run)
            if existing is None:
                saved.events = []
            else:
                saved.events = list(existing.events)
            self._runs[run.id] = saved

    def list(self, *, user_id: str | None = None) -> list[Run]:
        # Extension: multi-user — user_id is not a filter in v1.
        ignore_user_scope(user_id)
        with self._lock:
            ordered = sorted(
                self._runs.values(),
                key=lambda run: (run.created_at, run.id),
            )
            return [copy.deepcopy(run) for run in ordered]

    def append_events(
        self,
        run_id: str,
        events: list[dict[str, Any]],
        *,
        user_id: str | None = None,
    ) -> None:
        # Extension: multi-user — user_id is not a filter in v1.
        ignore_user_scope(user_id)
        if not events:
            return
        with self._lock:
            stored = self._runs.get(run_id)
            if stored is None:
                raise KeyError(run_id)
            stored.events.extend(copy.deepcopy(events))
            stored.seq = max(
                stored.seq, max(int(event["seq"]) for event in events)
            )


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        seq INTEGER NOT NULL,
        snapshot_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        event_json TEXT NOT NULL,
        PRIMARY KEY (run_id, seq),
        FOREIGN KEY (run_id) REFERENCES runs(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review_tokens (
        jti TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        exp INTEGER NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_review_tokens_run_id
        ON review_tokens(run_id)
    """,
)


class SqliteRunStore:
    """Default run store. One local SQLite file, created on open."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL").fetchone()
        self._conn.execute("PRAGMA foreign_keys=ON").fetchone()
        self._conn.execute("PRAGMA busy_timeout=5000").fetchone()
        for statement in _SCHEMA:
            self._conn.execute(statement)
        logger.info("Meeting run store: %s", self.path)

    def close(self) -> None:
        with self._lock:
            conn = self._conn
            self._conn = None
            if conn is not None:
                conn.close()

    def __enter__(self) -> SqliteRunStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def get(self, run_id: str, *, user_id: str | None = None) -> Run | None:
        # Extension: multi-user — user_id is not a filter in v1.
        ignore_user_scope(user_id)
        with self._lock:
            conn = self._connection()
            row = conn.execute(
                "SELECT snapshot_json FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            event_rows = conn.execute(
                """
                SELECT event_json FROM events
                WHERE run_id = ?
                ORDER BY seq ASC
                """,
                (run_id,),
            ).fetchall()
        payload = json.loads(row["snapshot_json"])
        events = [json.loads(item["event_json"]) for item in event_rows]
        return _run_from_parts(payload, events)

    def put(self, run: Run, *, user_id: str | None = None) -> None:
        # Extension: multi-user — user_id is not a filter in v1.
        ignore_user_scope(user_id)
        blob = _dump(_snapshot_payload(run))
        with self._lock:
            self._connection().execute(
                """
                INSERT INTO runs (
                    id, created_at, updated_at, seq, snapshot_json
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    seq = excluded.seq,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    run.id,
                    run.created_at,
                    run.updated_at,
                    int(run.seq),
                    blob,
                ),
            )

    def list(self, *, user_id: str | None = None) -> list[Run]:
        # Extension: multi-user — user_id is not a filter in v1.
        ignore_user_scope(user_id)
        with self._lock:
            rows = (
                self._connection()
                .execute("SELECT id FROM runs ORDER BY created_at ASC, id ASC")
                .fetchall()
            )
            ids = [row["id"] for row in rows]
        runs: list[Run] = []
        for run_id in ids:
            loaded = self.get(run_id, user_id=user_id)
            if loaded is not None:
                runs.append(loaded)
        return runs

    def append_events(
        self,
        run_id: str,
        events: list[dict[str, Any]],
        *,
        user_id: str | None = None,
    ) -> None:
        # Extension: multi-user — user_id is not a filter in v1.
        ignore_user_scope(user_id)
        if not events:
            return
        with self._lock:
            conn = self._connection()
            with self._immediate(conn):
                for event in events:
                    conn.execute(
                        """
                        INSERT INTO events (run_id, seq, event_json)
                        VALUES (?, ?, ?)
                        """,
                        (run_id, int(event["seq"]), _dump(event)),
                    )

    def get_token(self, jti: str) -> TokenRecord | None:
        with self._lock:
            row = (
                self._connection()
                .execute(
                    """
                SELECT jti, run_id, content_hash, exp, consumed
                FROM review_tokens
                WHERE jti = ?
                """,
                    (jti,),
                )
                .fetchone()
            )
        if row is None:
            return None
        return TokenRecord(
            jti=row["jti"],
            run_id=row["run_id"],
            content_hash=row["content_hash"],
            exp=int(row["exp"]),
            consumed=bool(row["consumed"]),
        )

    def put_token(self, record: TokenRecord) -> None:
        with self._lock:
            self._connection().execute(
                """
                INSERT INTO review_tokens (
                    jti, run_id, content_hash, exp, consumed
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(jti) DO UPDATE SET
                    run_id = excluded.run_id,
                    content_hash = excluded.content_hash,
                    exp = excluded.exp,
                    consumed = excluded.consumed
                """,
                (
                    record.jti,
                    record.run_id,
                    record.content_hash,
                    int(record.exp),
                    int(record.consumed),
                ),
            )

    def consume_token(self, jti: str) -> bool:
        with self._lock:
            conn = self._connection()
            with self._immediate(conn):
                row = conn.execute(
                    "SELECT 1 FROM review_tokens WHERE jti = ?",
                    (jti,),
                ).fetchone()
                if row is None:
                    return False
                conn.execute(
                    "UPDATE review_tokens SET consumed = 1 WHERE jti = ?",
                    (jti,),
                )
            return True

    def consume_run_tokens(self, run_id: str) -> None:
        with self._lock:
            self._connection().execute(
                "UPDATE review_tokens SET consumed = 1 WHERE run_id = ?",
                (run_id,),
            )

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.ProgrammingError("SQLite run store is closed")
        return self._conn

    @contextmanager
    def _immediate(self, conn: sqlite3.Connection):
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
