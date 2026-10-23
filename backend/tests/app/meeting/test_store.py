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

"""SQLite durability for meeting runs.

Closing a store and opening another on the same file is the restart
stand-in: a new MeetingService, a new ReviewTokenService, same path,
same MEETING_REVIEW_TOKEN_SECRET.
"""

import sqlite3
from pathlib import Path

import pytest

import app.meeting.store as store_mod
from app.meeting.adapters.feishu import (
    EnterpriseFeishuTaskAdapter,
    MockFeishuTaskAdapter,
)
from app.meeting.adapters.minutes_llm import MockMinutesLLM
from app.meeting.adapters.transcriber import MockTranscriber
from app.meeting.errors import HardReject, MeetingServiceError
from app.meeting.review_token import ReviewTokenError, ReviewTokenService
from app.meeting.service import MeetingService, build_meeting_service
from app.meeting.store import (
    InMemoryRunStore,
    Run,
    SqliteRunStore,
    meeting_db_path,
)
from tests.app.meeting.conftest import make_service

_SECRET = "restart-secret"
_TRANSCRIPT = "会议结论与两条待办。"


def _event(seq: int, event_type: str) -> dict:
    return {
        "seq": seq,
        "at": "2026-10-23T00:00:00+00:00",
        "type": event_type,
        "summary": event_type,
        "payload": {"n": seq},
    }


def _open(monkeypatch, path: Path) -> MeetingService:
    monkeypatch.setenv("MEETING_DB_PATH", str(path))
    monkeypatch.setenv("MEETING_REVIEW_TOKEN_SECRET", _SECRET)
    monkeypatch.setenv("MOCK_ASR", "1")
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("MOCK_FEISHU", "1")
    service = build_meeting_service()
    assert service.transcriber.name == "mock"
    assert service.minutes_llm.name == "mock"
    assert service.feishu.name == "mock"
    assert isinstance(service.store, SqliteRunStore)
    return service


def _close(service: MeetingService) -> None:
    service.store.close()


def _db_bytes(path: Path) -> bytes:
    chunks = [path.read_bytes()]
    for suffix in ("-wal", "-shm"):
        extra = Path(str(path) + suffix)
        if extra.is_file():
            chunks.append(extra.read_bytes())
    return b"".join(chunks)


def _seqs(events: list[dict]) -> list[int]:
    return [event["seq"] for event in events]


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield InMemoryRunStore()
        return
    sqlite_store = SqliteRunStore(tmp_path / "contract.db")
    try:
        yield sqlite_store
    finally:
        sqlite_store.close()


def test_meeting_db_path_default_and_override(monkeypatch, tmp_path):
    backend = Path(store_mod.__file__).resolve().parents[2]
    default = backend / "data" / "meeting.db"
    monkeypatch.delenv("MEETING_DB_PATH", raising=False)
    assert meeting_db_path() == default
    monkeypatch.setenv("MEETING_DB_PATH", "   ")
    assert meeting_db_path() == default
    custom = tmp_path / "custom.db"
    monkeypatch.setenv("MEETING_DB_PATH", f"  {custom}  ")
    assert meeting_db_path() == custom


def test_service_without_store_stays_in_memory(monkeypatch, tmp_path):
    monkeypatch.setenv("MEETING_DB_PATH", str(tmp_path / "absent.db"))
    service, _adapter = make_service()
    assert isinstance(service.store, InMemoryRunStore)
    created = service.create_run(title="memory")
    assert service.get_run(created["id"])["title"] == "memory"
    assert not (tmp_path / "absent.db").exists()


def test_put_does_not_replace_the_event_log(store):
    first = Run(
        id="r",
        status="created",
        created_at="2026-10-23T00:00:01+00:00",
        updated_at="2026-10-23T00:00:01+00:00",
        title="weekly",
    )
    store.put(first)
    store.append_events("r", [_event(1, "run.created")])
    first.status = "ingesting"
    first.seq = 1
    first.updated_at = "2026-10-23T00:00:02+00:00"
    store.put(first)
    loaded = store.get("r")
    assert loaded is not None
    assert loaded.status == "ingesting"
    assert loaded.title == "weekly"
    assert [event["type"] for event in loaded.events] == ["run.created"]
    assert loaded.seq == 1

    earlier = Run(
        id="a",
        status="created",
        created_at="2026-10-23T00:00:00+00:00",
        updated_at="2026-10-23T00:00:00+00:00",
    )
    store.put(earlier)
    assert [run.id for run in store.list()] == ["a", "r"]
    assert store.get("missing") is None


def test_append_requires_an_existing_run(tmp_path):
    event = _event(1, "run.created")
    memory = InMemoryRunStore()
    with pytest.raises(KeyError):
        memory.append_events("missing", [event])
    sqlite_store = SqliteRunStore(tmp_path / "missing-parent.db")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            sqlite_store.append_events("missing", [event])
    finally:
        sqlite_store.close()


def test_sqlite_roundtrip_covers_run_fields_and_token_rows(tmp_path):
    path = tmp_path / "nested" / "meeting.db"
    original = Run(
        id="run-1",
        status="awaiting_review",
        created_at="2026-10-23T00:00:00+00:00",
        updated_at="2026-10-23T00:01:00+00:00",
        title="weekly",
        live_active=True,
        input_kind="transcript",
        input_hash="abc",
        audio_filename="live.ogg",
        audio_bytes=12,
        transcript="张三会后整理",
        transcript_hash="def",
        minutes={"title": "增长组周会"},
        todos=[{"title": "整理", "assignee": "张三"}],
        final_minutes={"title": "增长组周会"},
        final_todos=[{"title": "整理"}],
        content_hash="hash-a",
        approved=True,
        active_jti="jti-1",
        feishu_receipts=[{"ok": True, "feishu_task_id": "mock_1"}],
        error="none-yet",
        seq=2,
    )
    events = [_event(1, "run.created"), _event(2, "review.pending")]
    store = SqliteRunStore(path)
    try:
        assert path.is_file()
        assert store.list() == []
        store.put(original)
        store.append_events(original.id, events)
        tokens = ReviewTokenService(b"restart-secret", ledger=store)
        token, jti, exp = tokens.issue("run-1", "hash-a")
        record = tokens.validate(
            token, run_id="run-1", content_hash="hash-a", active_jti=jti
        )
        assert record.exp == exp
    finally:
        store.close()

    assert b"restart-secret" not in _db_bytes(path)
    reopened = SqliteRunStore(path)
    try:
        loaded = reopened.get("run-1")
        assert loaded is not None
        for name in original.__dataclass_fields__:
            if name == "events":
                continue
            assert getattr(loaded, name) == getattr(original, name), name
        assert loaded.events == events
        revived = ReviewTokenService(b"restart-secret", ledger=reopened)
        revived.validate(
            token, run_id="run-1", content_hash="hash-a", active_jti=jti
        )
        revived.consume(jti)
    finally:
        reopened.close()

    consumed = SqliteRunStore(path)
    try:
        again = ReviewTokenService(b"restart-secret", ledger=consumed)
        with pytest.raises(ReviewTokenError) as exc:
            again.validate(
                token,
                run_id="run-1",
                content_hash="hash-a",
                active_jti=jti,
            )
        assert exc.value.reason == "consumed"
        blob = _db_bytes(path)
        assert token.encode() not in blob
        assert token.rsplit(".", 1)[-1].encode() not in blob
        raw = sqlite3.connect(path)
        try:
            snapshot = raw.execute(
                "SELECT snapshot_json FROM runs WHERE id = ?",
                ("run-1",),
            ).fetchone()[0]
        finally:
            raw.close()
        assert '"events"' not in snapshot
    finally:
        consumed.close()


def test_restart_continues_review_then_feishu_write(monkeypatch, tmp_path):
    path = tmp_path / "nested" / "meeting.db"
    first = _open(monkeypatch, path)
    try:
        created = first.create_run(title="weekly")
        run_id = created["id"]
        imported = first.import_transcript(run_id, _TRANSCRIPT)
        assert imported["status"] == "awaiting_review"
        assert path.is_file()
    finally:
        _close(first)

    second = _open(monkeypatch, path)
    try:
        snapshot = second.get_run(run_id)
        assert snapshot["status"] == "awaiting_review"
        assert snapshot["title"] == "weekly"
        assert snapshot["approved"] is False
        replay = second.replay(run_id)
        types = [event["type"] for event in replay["events"]]
        assert "tool.import_transcript" in types
        assert "tool.transcribe" not in types
        assert "review.pending" in types
        assert _seqs(replay["events"]) == list(range(1, len(types) + 1))
        draft = second.get_draft(run_id)
        assert draft["minutes"]["title"] == "增长组周会"
        assert draft["todos"][1]["assignee"] == "待确认"
        assert [run.id for run in second.store.list()] == [run_id]
        approved = second.review(run_id, "approve")
        token = approved["review_token"]
        content_hash = approved["content_hash"]
        assert token
        assert content_hash
    finally:
        _close(second)

    assert _SECRET.encode() not in _db_bytes(path)
    assert token.encode() not in _db_bytes(path)

    third = _open(monkeypatch, path)
    try:
        restored = third.get_run(run_id)
        assert restored["status"] == "awaiting_review"
        assert restored["approved"] is True
        assert restored["content_hash"] == content_hash
        assert third.get_draft(run_id)["content_hash"] == content_hash
        replay = third.replay(run_id)
        assert any(
            event["type"] == "review.approved" for event in replay["events"]
        )
        assert token not in str(replay)
        written = third.write_feishu(run_id, token)
        assert written["status"] == "succeeded"
        assert third.feishu.calls == 1
        receipts = written["feishu_receipts"]
        assert receipts and all(item["ok"] for item in receipts)
    finally:
        _close(third)

    fourth = _open(monkeypatch, path)
    try:
        final = fourth.get_run(run_id)
        assert final["status"] == "succeeded"
        assert final["feishu_receipts"] == receipts
        replay = fourth.replay(run_id)
        types = [event["type"] for event in replay["events"]]
        assert "tool.write_feishu_tasks" in types
        assert "tool.feishu_receipts" in types
        assert _seqs(replay["events"]) == list(range(1, len(types) + 1))
        with pytest.raises(HardReject) as consumed:
            fourth.write_feishu(run_id, token)
        assert consumed.value.reason == "consumed"
        assert fourth.feishu.calls == 0
        assert fourth.get_run(run_id)["status"] == "succeeded"
    finally:
        _close(fourth)


def test_restart_reject_still_blocks_feishu(monkeypatch, tmp_path):
    path = tmp_path / "reject.db"
    first = _open(monkeypatch, path)
    try:
        run_id = first.create_run()["id"]
        first.import_transcript(run_id, _TRANSCRIPT)
    finally:
        _close(first)

    second = _open(monkeypatch, path)
    try:
        rejected = second.review(run_id, "reject")
        assert rejected["status"] == "rejected"
        assert rejected["review_token"] is None
        assert rejected["content_hash"] is None
        with pytest.raises(HardReject) as denied:
            second.write_feishu(run_id, None)
        assert denied.value.reason == "missing_token"
        assert second.feishu.calls == 0
        assert second.get_run(run_id)["status"] == "rejected"
    finally:
        _close(second)

    third = _open(monkeypatch, path)
    try:
        snapshot = third.get_run(run_id)
        assert snapshot["status"] == "rejected"
        assert snapshot["approved"] is False
        assert snapshot["content_hash"] is None
        assert snapshot["feishu_receipts"] == []
        replay = third.replay(run_id)
        assert any(
            event["type"] == "review.rejected" for event in replay["events"]
        )
        assert any(
            event["type"] == "permission.hard_reject"
            for event in replay["events"]
        )
        with pytest.raises(HardReject) as denied:
            third.write_feishu(run_id, None)
        assert denied.value.reason == "missing_token"
        assert third.feishu.calls == 0
    finally:
        _close(third)


def test_restart_edit_approve_rotates_hash_before_write(monkeypatch, tmp_path):
    path = tmp_path / "edit.db"
    first = _open(monkeypatch, path)
    try:
        run_id = first.create_run()["id"]
        first.import_transcript(run_id, _TRANSCRIPT)
        approved = first.review(run_id, "approve")
        old_token = approved["review_token"]
        old_hash = approved["content_hash"]
    finally:
        _close(first)

    second = _open(monkeypatch, path)
    try:
        draft = second.get_draft(run_id)
        assert draft["content_hash"] == old_hash
        edited = second.review(
            run_id,
            "edit_approve",
            {
                "minutes": draft["minutes"],
                "todos": [
                    {**draft["todos"][0], "title": "改过的待办标题"},
                    *draft["todos"][1:],
                ],
            },
        )
        assert edited["review_token"] != old_token
        assert edited["content_hash"] != old_hash
        new_token = edited["review_token"]
        new_hash = edited["content_hash"]
        with pytest.raises(HardReject) as mismatch:
            second.write_feishu(run_id, old_token)
        assert mismatch.value.reason == "hash_mismatch"
        assert second.feishu.calls == 0
        assert second.get_run(run_id)["status"] == "awaiting_review"
    finally:
        _close(second)

    third = _open(monkeypatch, path)
    try:
        assert third.get_run(run_id)["content_hash"] == new_hash
        written = third.write_feishu(run_id, new_token)
        assert written["status"] == "succeeded"
        assert third.feishu.calls == 1
        receipts = written["feishu_receipts"]
        assert old_token not in str(third.replay(run_id))
        assert new_token not in str(third.replay(run_id))
    finally:
        _close(third)

    fourth = _open(monkeypatch, path)
    try:
        assert fourth.get_run(run_id)["feishu_receipts"] == receipts
        with pytest.raises(HardReject) as consumed:
            fourth.write_feishu(run_id, new_token)
        assert consumed.value.reason == "consumed"
        assert fourth.feishu.calls == 0
    finally:
        _close(fourth)


def test_unready_feishu_does_not_consume_token_across_reopen(tmp_path):
    path = tmp_path / "gate.db"
    store = SqliteRunStore(path)
    try:
        service = MeetingService(
            transcriber=MockTranscriber(),
            minutes_llm=MockMinutesLLM(),
            feishu=EnterpriseFeishuTaskAdapter(app_id="", app_secret=""),
            tokens=ReviewTokenService(b"restart-secret", ledger=store),
            store=store,
        )
        run_id = service.create_run()["id"]
        service.import_transcript(run_id, _TRANSCRIPT)
        token = service.review(run_id, "approve")["review_token"]
        with pytest.raises(MeetingServiceError) as blocked:
            service.write_feishu(run_id, token)
        assert blocked.value.code == "feishu_not_configured"
        assert service.feishu.calls == 0
        assert service.get_run(run_id)["status"] == "awaiting_review"
        assert service.get_run(run_id)["approved"] is True
    finally:
        store.close()

    ready = SqliteRunStore(path)
    try:
        adapter = MockFeishuTaskAdapter()
        service = MeetingService(
            transcriber=MockTranscriber(),
            minutes_llm=MockMinutesLLM(),
            feishu=adapter,
            tokens=ReviewTokenService(b"restart-secret", ledger=ready),
            store=ready,
        )
        written = service.write_feishu(run_id, token)
        assert written["status"] == "succeeded"
        assert adapter.calls == 1
    finally:
        ready.close()
