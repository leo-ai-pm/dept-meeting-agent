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

import json

import httpx

from app.meeting.adapters.feishu import (
    EnterpriseFeishuTaskAdapter,
    FeishuWriteRequest,
    MockFeishuTaskAdapter,
)
from app.meeting.adapters.minutes_llm import MockMinutesLLM
from app.meeting.adapters.transcriber import MockTranscriber
from app.meeting.schema import (
    build_task_body,
    idempotency_key,
    open_id_or_none,
)
from app.meeting.service import build_meeting_service, env_flag
from tests.app.meeting.conftest import make_service


def test_mock_transcriber_and_llm_return_fixtures():
    transcript = MockTranscriber().transcribe(b"not-used", filename="a.wav")
    assert "增长组周会" in transcript
    minutes = MockMinutesLLM().draft_minutes(transcript)
    todos = MockMinutesLLM().extract_todos(transcript, minutes)
    assert 3 <= len(minutes["decisions"]) <= 5
    assert todos[0]["title"]
    assert todos[1]["assignee"] is None
    assert todos[1]["due_date"] is None


def test_missing_assignee_is_marked_and_open_id_is_not_invented():
    service, feishu = make_service()
    run = service.create_run()
    service.import_transcript(run["id"], "transcript")
    draft = service.get_draft(run["id"])
    assignees = [todo["assignee"] for todo in draft["todos"]]
    assert assignees[0] == "张三"
    assert assignees[1] == "待确认"
    assert open_id_or_none("张三") is None
    assert open_id_or_none("待确认") is None
    assert open_id_or_none("ou_abc123") == "ou_abc123"

    approved = service.review(run["id"], "approve")
    service.write_feishu(run["id"], approved["review_token"])
    assert feishu.calls == 1
    for body in feishu.last_bodies:
        assert "members" not in body
        assert "ou_" not in body["description"]
    second = feishu.last_bodies[1]
    assert "截止日期：待确认" in second["description"]
    assert "负责人：待确认" in second["description"]
    assert "run_id:" in second["description"]


def test_idempotency_key_is_stable_and_mock_replays_task_id():
    key = idempotency_key("run", "title", "待确认", "")
    assert key == idempotency_key("run", "title", "待确认", "")
    adapter = MockFeishuTaskAdapter()
    todo = {
        "title": "title",
        "description": "desc",
        "assignee": "待确认",
        "due_date": None,
        "source_quote": None,
        "run_id": "run",
        "idempotency_key": key,
    }
    request = FeishuWriteRequest(
        run_id="run",
        minutes_summary={
            "title": "weekly",
            "decisions": ["a", "b", "c"],
            "run_id": "run",
        },
        todos=[todo, todo],
    )
    receipts = adapter.write_tasks(request)
    assert receipts[0]["feishu_task_id"] == receipts[1]["feishu_task_id"]
    again = adapter.write_tasks(request)
    assert again[0]["feishu_task_id"] == receipts[0]["feishu_task_id"]


def test_named_assignee_is_not_sent_as_open_id_but_real_open_id_is():
    summary = {
        "title": "weekly",
        "decisions": ["a", "b", "c"],
        "run_id": "run",
    }
    named = build_task_body(
        {
            "title": "t",
            "description": "d",
            "assignee": "张三",
            "due_date": "2026-10-28",
            "run_id": "run",
            "idempotency_key": "k1",
        },
        summary,
    )
    assert "members" not in named
    identified = build_task_body(
        {
            "title": "t",
            "description": "d",
            "assignee": "ou_abc123",
            "due_date": None,
            "run_id": "run",
            "idempotency_key": "k2",
        },
        summary,
    )
    assert identified["members"] == [
        {"id": "ou_abc123", "role": "assignee", "type": "user"}
    ]


def test_default_builders_use_mocks(monkeypatch):
    monkeypatch.delenv("MOCK_ASR", raising=False)
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.delenv("MOCK_FEISHU", raising=False)
    assert env_flag("MOCK_ASR", "1") is True
    service = build_meeting_service()
    assert service.transcriber.name == "mock"
    assert service.minutes_llm.name == "mock"
    assert service.feishu.name == "mock"


def test_enterprise_adapter_retries_5xx_and_skips_open_id_invention():
    posts = {"auth": 0, "task": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            posts["auth"] += 1
            body = request.read()
            assert b"app_secret_value" in body
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "tenant-token"}
            )
        posts["task"] += 1
        payload = json.loads(request.content)
        assert "members" not in payload
        assert "张三" not in json.dumps(payload.get("members", []))
        if posts["task"] < 3:
            return httpx.Response(
                500, json={"code": 500, "msg": "unavailable"}
            )
        return httpx.Response(
            200,
            json={"code": 0, "data": {"task": {"guid": "task-guid-1"}}},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = EnterpriseFeishuTaskAdapter(
        app_id="cli_test",
        app_secret="app_secret_value",
        client=client,
        sleep=lambda _seconds: None,
    )
    receipts = adapter.write_tasks(
        FeishuWriteRequest(
            run_id="run",
            minutes_summary={
                "title": "weekly",
                "decisions": ["one", "two", "three"],
                "run_id": "run",
            },
            todos=[
                {
                    "title": "ship notes",
                    "description": "write the notes",
                    "assignee": "张三",
                    "due_date": None,
                    "source_quote": "张三 will do it",
                    "run_id": "run",
                    "idempotency_key": "idem-1",
                }
            ],
        )
    )
    assert posts["task"] == 3
    assert receipts == [
        {
            "ok": True,
            "feishu_task_id": "task-guid-1",
            "idempotency_key": "idem-1",
        }
    ]
    replay = adapter.write_tasks(
        FeishuWriteRequest(
            run_id="run",
            minutes_summary={
                "title": "weekly",
                "decisions": ["one", "two", "three"],
                "run_id": "run",
            },
            todos=[
                {
                    "title": "ship notes",
                    "description": "write the notes",
                    "assignee": "张三",
                    "due_date": None,
                    "source_quote": None,
                    "run_id": "run",
                    "idempotency_key": "idem-1",
                }
            ],
        )
    )
    assert replay[0]["feishu_task_id"] == "task-guid-1"
    assert posts["task"] == 3


def test_enterprise_adapter_does_not_retry_4xx():
    posts = {"task": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "tenant-token"}
            )
        posts["task"] += 1
        return httpx.Response(403, json={"code": 403, "msg": "forbidden"})

    adapter = EnterpriseFeishuTaskAdapter(
        app_id="cli_test",
        app_secret="secret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )
    receipts = adapter.write_tasks(
        FeishuWriteRequest(
            run_id="run",
            minutes_summary={
                "title": "weekly",
                "decisions": ["one", "two", "three"],
                "run_id": "run",
            },
            todos=[
                {
                    "title": "t",
                    "description": "d",
                    "assignee": "待确认",
                    "due_date": None,
                    "source_quote": None,
                    "run_id": "run",
                    "idempotency_key": "idem-4xx",
                }
            ],
        )
    )
    assert posts["task"] == 1
    assert receipts[0]["ok"] is False
    assert "forbidden" in receipts[0]["error"]
