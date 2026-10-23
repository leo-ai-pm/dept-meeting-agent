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

"""Feishu writes are idempotent: success cannot be replayed, failure can.

The scripted adapter does not remember successful keys. A second create of
the same todo is a service bug, not an adapter cache hit.
"""

import pytest

from app.meeting.adapters.feishu import FeishuWriteRequest
from app.meeting.adapters.minutes_llm import MockMinutesLLM
from app.meeting.adapters.transcriber import MockTranscriber
from app.meeting.errors import HardReject
from app.meeting.review_token import ReviewTokenService
from app.meeting.service import MeetingService
from app.meeting.store import SqliteRunStore
from tests.app.meeting.conftest import make_client, make_service

_TRANSCRIPT = "会议结论与两条待办。"


class ScriptedFeishu:
    """Counts every create. Fails one key once, or raises once."""

    name = "scripted"

    def __init__(
        self, *, fail_key: str | None = None, raise_once: bool = False
    ) -> None:
        self.fail_key = fail_key
        self.raise_once = raise_once
        self._raised = False
        self._failed_keys: set[str] = set()
        self.calls = 0
        self.creates: list[str] = []

    def ready(self) -> bool:
        return True

    def write_tasks(self, request: FeishuWriteRequest) -> list[dict]:
        self.calls += 1
        if self.raise_once and not self._raised:
            self._raised = True
            raise RuntimeError("feishu transport error")
        receipts: list[dict] = []
        for todo in request.todos:
            key = str(todo["idempotency_key"])
            if key == self.fail_key and key not in self._failed_keys:
                self._failed_keys.add(key)
                receipts.append(
                    {
                        "ok": False,
                        "error": "upstream timeout",
                        "idempotency_key": key,
                    }
                )
                continue
            self.creates.append(key)
            receipts.append(
                {
                    "ok": True,
                    "feishu_task_id": f"task-{key[:12]}-{len(self.creates)}",
                    "idempotency_key": key,
                }
            )
        return receipts


def _approve(service: MeetingService) -> tuple[str, str, list[str]]:
    run_id = service.create_run()["id"]
    imported = service.import_transcript(run_id, _TRANSCRIPT)
    assert imported["status"] == "awaiting_review"
    approved = service.review(run_id, "approve")
    keys = [
        todo["idempotency_key"] for todo in service.get_draft(run_id)["todos"]
    ]
    return run_id, approved["review_token"], keys


def _service(feishu) -> MeetingService:
    service, _adapter = make_service(feishu=feishu)
    return service


def test_success_then_replay_is_consumed_and_creates_nothing_else():
    service, feishu = make_service()
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    imported = client.post(
        f"/meeting/runs/{run_id}/import/transcript",
        json={"text": _TRANSCRIPT},
    )
    assert imported.json()["status"] == "awaiting_review"
    token = client.post(
        f"/meeting/runs/{run_id}/review",
        json={"decision": "approve"},
    ).json()["review_token"]

    written = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": token},
    )
    assert written.status_code == 200
    body = written.json()
    assert body["status"] == "succeeded"
    assert feishu.calls == 1
    ids = [item["feishu_task_id"] for item in body["feishu_receipts"]]
    assert len(ids) == 2
    assert len(set(ids)) == 2

    again = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": token},
    )
    assert again.status_code == 403
    detail = again.json()["detail"]
    assert detail["reason"] == "consumed"
    assert detail["feishu_called"] is False
    assert feishu.calls == 1
    assert [item["feishu_task_id"] for item in feishu._by_key.values()] == ids

    snapshot = client.get(f"/meeting/runs/{run_id}").json()
    assert snapshot["status"] == "succeeded"
    assert [
        item["feishu_task_id"] for item in snapshot["feishu_receipts"]
    ] == (ids)
    replay = client.get(f"/meeting/runs/{run_id}/replay").json()
    assert token not in str(replay)
    rejects = [
        event
        for event in replay["events"]
        if event["type"] == "permission.hard_reject"
    ]
    assert rejects[-1]["payload"]["reason"] == "consumed"
    assert rejects[-1]["payload"]["feishu_called"] is False


def test_transport_error_is_on_replay_and_retry_creates_once():
    adapter = ScriptedFeishu(raise_once=True)
    service = _service(adapter)
    client = make_client(service)
    run_id, token, keys = _approve(service)
    assert len(keys) == 2

    failed = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": token},
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert adapter.creates == []
    assert adapter.calls == 1
    replay = client.get(f"/meeting/runs/{run_id}/replay").json()
    assert token not in str(replay)
    assert replay["status"] == "failed"
    failures = [
        event for event in replay["events"] if event["type"] == "run.failed"
    ]
    assert failures[-1]["payload"]["error"] == "feishu transport error"
    receipt_events = [
        event
        for event in replay["events"]
        if event["type"] == "tool.feishu_receipts"
    ]
    assert receipt_events[-1]["payload"]["ok"] is False
    assert receipt_events[-1]["payload"]["retryable"] is True
    assert receipt_events[-1]["payload"]["error"] == "feishu transport error"

    retried = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": token},
    )
    assert retried.status_code == 200
    assert retried.json()["status"] == "succeeded"
    assert adapter.creates == keys
    assert adapter.calls == 2
    ids = [
        item["feishu_task_id"] for item in retried.json()["feishu_receipts"]
    ]
    assert len(ids) == len(set(ids)) == len(keys)

    consumed = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": token},
    )
    assert consumed.status_code == 403
    assert consumed.json()["detail"]["reason"] == "consumed"
    assert adapter.creates == keys
    assert adapter.calls == 2


def test_partial_failure_retries_after_restart_without_a_second_create(
    tmp_path,
):
    path = tmp_path / "retry.db"
    store = SqliteRunStore(path)
    first = ScriptedFeishu()
    try:
        service = MeetingService(
            transcriber=MockTranscriber(),
            minutes_llm=MockMinutesLLM(),
            feishu=first,
            tokens=ReviewTokenService(b"retry-secret", ledger=store),
            store=store,
        )
        run_id, token, keys = _approve(service)
        first.fail_key = keys[1]
        failed = service.write_feishu(run_id, token)
        assert failed["status"] == "failed"
        assert first.creates == [keys[0]]
        assert first.calls == 1
        kept = next(
            item["feishu_task_id"]
            for item in failed["feishu_receipts"]
            if item.get("ok")
        )
        replay = service.replay(run_id)
        assert token not in str(replay)
        assert any(event["type"] == "run.failed" for event in replay["events"])
        receipt_events = [
            event
            for event in replay["events"]
            if event["type"] == "tool.feishu_receipts"
        ]
        assert any(
            item.get("ok") is False
            for item in receipt_events[-1]["payload"]["receipts"]
        )
        assert token.encode() not in path.read_bytes()
    finally:
        store.close()

    reopened = SqliteRunStore(path)
    second = ScriptedFeishu()
    try:
        service = MeetingService(
            transcriber=MockTranscriber(),
            minutes_llm=MockMinutesLLM(),
            feishu=second,
            tokens=ReviewTokenService(b"retry-secret", ledger=reopened),
            store=reopened,
        )
        written = service.write_feishu(run_id, token)
        assert written["status"] == "succeeded"
        assert second.creates == [keys[1]]
        assert second.calls == 1
        ids = [item["feishu_task_id"] for item in written["feishu_receipts"]]
        assert kept in ids
        assert len(ids) == len(set(ids)) == len(keys)
        write_events = [
            event
            for event in service.replay(run_id)["events"]
            if event["type"] == "tool.write_feishu_tasks"
        ]
        assert write_events[-1]["payload"]["retry"] is True
        assert write_events[-1]["payload"]["idempotency_keys"] == [keys[1]]
        assert write_events[-1]["payload"]["skipped_idempotency_keys"] == [
            keys[0]
        ]
        with pytest.raises(HardReject) as consumed:
            service.write_feishu(run_id, token)
        assert consumed.value.reason == "consumed"
        assert second.creates == [keys[1]]
        assert second.calls == 1
    finally:
        reopened.close()


def test_reimport_after_failed_write_retires_the_token():
    adapter = ScriptedFeishu()
    service = _service(adapter)
    run_id, token, keys = _approve(service)
    adapter.fail_key = keys[1]
    failed = service.write_feishu(run_id, token)
    assert failed["status"] == "failed"
    assert adapter.creates == [keys[0]]

    again = service.import_transcript(run_id, "另一段会议记录。")
    assert again["status"] == "awaiting_review"
    assert again["approved"] is False
    with pytest.raises(HardReject) as denied:
        service.write_feishu(run_id, token)
    assert denied.value.reason in {"consumed", "hash_mismatch"}
    assert adapter.creates == [keys[0]]
    assert adapter.calls == 1


def test_interrupted_writing_feishu_finishes_once():
    service, feishu = make_service()
    run_id, token, keys = _approve(service)
    stored = service.store.get(run_id)
    assert stored is not None
    stored.status = "writing_feishu"
    service.store.put(stored)

    written = service.write_feishu(run_id, token)
    assert written["status"] == "succeeded"
    assert feishu.calls == 1
    assert len(written["feishu_receipts"]) == len(keys)
    with pytest.raises(HardReject) as consumed:
        service.write_feishu(run_id, token)
    assert consumed.value.reason == "consumed"
    assert feishu.calls == 1
