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
import logging

import httpx
import pytest

from app.meeting.adapters.minutes_llm import (
    VOLCANO_DEFAULT_BASE_URL,
    VOLCANO_DEFAULT_MODEL,
    VolcanoMinutesLLM,
)
from app.meeting.adapters.transcriber import ProviderNotConfigured
from app.meeting.errors import MeetingServiceError
from app.meeting.service import build_meeting_service
from tests.app.meeting.conftest import make_service

_FIXTURE_MARK = "增长组周会"
_TRANSCRIPT = "李四：我来写纪要，下周五前完成。"
_KEY = "vk-live-secret-value"
_VENDOR_MINUTES = {
    "title": "Vendor weekly",
    "decisions": [
        "Ship the notes",
        "Keep the review gate",
        "Do not use the fixture",
    ],
    "summary": "Vendor summary, not the fixture",
    "body": "Vendor body",
}
_VENDOR_TODOS = {
    "todos": [
        {
            "title": "Vendor task",
            "description": "Write the vendor notes",
            "assignee": "李四",
            "due_date": "2026-11-01",
            "source_quote": "李四：我来写纪要。",
        }
    ]
}


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


def _clear_llm_env(monkeypatch) -> None:
    for name in (
        "VOLCANO_API_KEY",
        "ARK_API_KEY",
        "VOLCANO_BASE_URL",
        "ARK_BASE_URL",
        "MEETING_LLM_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_documented_ark_chat_endpoint():
    assert VOLCANO_DEFAULT_BASE_URL == (
        "https://ark.cn-beijing.volces.com/api/v3"
    )
    assert VOLCANO_DEFAULT_MODEL == "doubao-seed-2-1-turbo-260628"


def test_mock_llm_ignores_key_and_returns_fixtures(monkeypatch):
    monkeypatch.delenv("MOCK_LLM", raising=False)
    monkeypatch.setenv("VOLCANO_API_KEY", _KEY)
    monkeypatch.setenv("ARK_API_KEY", "ark-other-secret")
    monkeypatch.setenv("MEETING_LLM_MODEL", "doubao-seed-2-1-turbo-260628")
    service = build_meeting_service()
    assert service.minutes_llm.name == "mock"
    minutes = service.minutes_llm.draft_minutes(_TRANSCRIPT)
    todos = service.minutes_llm.extract_todos(_TRANSCRIPT, minutes)
    assert minutes["title"] == _FIXTURE_MARK
    assert todos[0]["title"] == "整理落地页转化实验数据"


def test_missing_key_does_not_invent_minutes(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "0")
    _clear_llm_env(monkeypatch)
    service = build_meeting_service()
    assert service.minutes_llm.name == "unconfigured"
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert result["status"] == "failed"
    error = result["error"] or ""
    assert "MOCK_LLM" in error
    assert "VOLCANO_API_KEY" in error
    assert _FIXTURE_MARK not in error
    assert result["draft_summary"] is None
    with pytest.raises(MeetingServiceError) as raised:
        service.get_draft(run["id"])
    assert raised.value.status_code == 404


def test_blank_keys_are_unconfigured_and_do_not_call_http(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.setenv("VOLCANO_API_KEY", "   ")
    monkeypatch.setenv("ARK_API_KEY", "\n")
    service = build_meeting_service()
    assert service.minutes_llm.name == "unconfigured"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("blank key must not call Volcano Ark")

    llm = VolcanoMinutesLLM(api_key="  ", client=_client(handler))
    with pytest.raises(ProviderNotConfigured, match="VOLCANO_API_KEY"):
        llm.draft_minutes(_TRANSCRIPT)


def test_volcano_key_selects_ark_and_prefers_volcano_names(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.setenv("VOLCANO_API_KEY", "volcano-key")
    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    monkeypatch.setenv("VOLCANO_BASE_URL", "https://ark.example/api/v3/")
    monkeypatch.setenv("ARK_BASE_URL", "https://ignored.example/api/v3")
    monkeypatch.setenv("MEETING_LLM_MODEL", "ep-demo")
    service = build_meeting_service()
    assert service.minutes_llm.name == "volcano_ark"
    assert service.minutes_llm._api_key == "volcano-key"
    assert service.minutes_llm._url == (
        "https://ark.example/api/v3/chat/completions"
    )
    assert service.minutes_llm._model == "ep-demo"


def test_ark_aliases_used_when_volcano_names_are_blank(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.setenv("VOLCANO_API_KEY", "  ")
    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    monkeypatch.setenv("VOLCANO_BASE_URL", "")
    monkeypatch.setenv("ARK_BASE_URL", "https://alias.example/api/v3")
    monkeypatch.setenv("MEETING_LLM_MODEL", "")
    service = build_meeting_service()
    assert service.minutes_llm._api_key == "ark-key"
    assert service.minutes_llm._url == (
        "https://alias.example/api/v3/chat/completions"
    )
    assert service.minutes_llm._model == "doubao-seed-2-1-turbo-260628"


def test_default_ark_target_when_optional_env_is_blank(monkeypatch):
    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.setenv("VOLCANO_API_KEY", "volcano-key")
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    monkeypatch.setenv("VOLCANO_BASE_URL", "  ")
    monkeypatch.delenv("ARK_BASE_URL", raising=False)
    monkeypatch.setenv("MEETING_LLM_MODEL", "")
    service = build_meeting_service()
    assert service.minutes_llm._url == (
        "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
    )
    assert service.minutes_llm._model == "doubao-seed-2-1-turbo-260628"
    assert service.minutes_llm._client.timeout == httpx.Timeout(180.0)


def test_chat_completions_maps_vendor_json_and_skips_fixtures():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        user = body["messages"][1]["content"]
        if "Minutes JSON:" not in user:
            content = json.dumps(_VENDOR_MINUTES, ensure_ascii=False)
        else:
            content = json.dumps(_VENDOR_TODOS, ensure_ascii=False)
        return httpx.Response(200, json=_completion(content))

    llm = VolcanoMinutesLLM(
        api_key=_KEY,
        base_url="https://ark.cn-beijing.volces.com/api/v3/",
        model="doubao-seed-2-1-turbo-260628",
        client=_client(handler),
    )
    service, feishu = make_service(minutes_llm=llm)
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert result["status"] == "awaiting_review"
    assert result["draft_summary"]["title"] == "Vendor weekly"
    assert result["draft_summary"]["todo_count"] == 1
    draft = service.get_draft(run["id"])
    assert draft["minutes"]["summary"] == "Vendor summary, not the fixture"
    assert draft["minutes"]["decisions"] == _VENDOR_MINUTES["decisions"]
    assert draft["todos"][0]["assignee"] == "李四"
    assert draft["todos"][0]["due_date"] == "2026-11-01"
    assert _FIXTURE_MARK not in json.dumps(draft, ensure_ascii=False)
    assert len(seen) == 2
    assert feishu.calls == 0
    for request in seen:
        assert str(request.url) == (
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        )
        assert request.headers["authorization"] == f"Bearer {_KEY}"
        body = json.loads(request.content)
        assert body["model"] == "doubao-seed-2-1-turbo-260628"
        assert body["temperature"] == 0.2
        assert body["response_format"] == {"type": "json_object"}
        assert [item["role"] for item in body["messages"]] == [
            "system",
            "user",
        ]
        assert _TRANSCRIPT in body["messages"][1]["content"]
        assert _KEY not in request.content.decode()
    events = service.replay(run["id"])["events"]
    drafted = next(
        event for event in events if event["type"] == "tool.draft_minutes"
    )
    assert drafted["payload"]["provider"] == "volcano_ark"
    assert drafted["payload"]["title"] == "Vendor weekly"


def test_fenced_json_and_minutes_wrapper_are_accepted():
    minutes = {
        "minutes": _VENDOR_MINUTES,
        "ignored": "not copied",
    }
    fenced = "```json\n" + json.dumps(minutes) + "\n```"
    todos = (
        "```\n"
        + json.dumps(
            [
                {
                    "title": "Wrapped task",
                    "description": "From a bare array",
                    "assignee": "",
                    "due_date": None,
                    "source_quote": None,
                }
            ]
        )
        + "\n```"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][1]["content"]
        content = fenced if "Minutes JSON:" not in user else todos
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": content},
                            ]
                        }
                    }
                ]
            },
        )

    llm = VolcanoMinutesLLM(api_key=_KEY, client=_client(handler))
    service, _feishu = make_service(minutes_llm=llm)
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert result["status"] == "awaiting_review"
    draft = service.get_draft(run["id"])
    assert draft["minutes"]["title"] == "Vendor weekly"
    assert "ignored" not in draft["minutes"]
    assert draft["todos"][0]["assignee"] == "待确认"
    assert draft["todos"][0]["due_date"] is None
    assert _FIXTURE_MARK not in draft["minutes"]["title"]


def test_http_error_fails_closed_and_redacts_the_key(caplog):
    caplog.set_level(logging.ERROR, logger="meeting.service")

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401,
            json={"error": {"message": f"rejected {_KEY}"}},
        )

    llm = VolcanoMinutesLLM(api_key=_KEY, client=_client(handler))
    service, feishu = make_service(minutes_llm=llm)
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert result["status"] == "failed"
    error = result["error"] or ""
    assert "HTTP 401" in error
    assert "[redacted]" in error
    assert _KEY not in error
    assert _FIXTURE_MARK not in error
    assert _KEY not in caplog.text
    assert "[redacted]" in caplog.text
    assert feishu.calls == 0
    with pytest.raises(MeetingServiceError):
        service.get_draft(run["id"])


def test_invalid_minutes_does_not_call_todos_or_use_fixture():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        calls["n"] += 1
        return httpx.Response(200, json=_completion("{}"))

    llm = VolcanoMinutesLLM(api_key=_KEY, client=_client(handler))
    service, feishu = make_service(minutes_llm=llm)
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert calls["n"] == 1
    assert result["status"] == "failed"
    assert "minutes title" in (result["error"] or "")
    assert _FIXTURE_MARK not in (result["error"] or "")
    assert result["draft_summary"] is None
    assert feishu.calls == 0


def test_empty_todos_do_not_fall_back_to_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][1]["content"]
        if "Minutes JSON:" not in user:
            return httpx.Response(
                200,
                json=_completion(json.dumps(_VENDOR_MINUTES)),
            )
        return httpx.Response(200, json=_completion('{"todos": []}'))

    llm = VolcanoMinutesLLM(api_key=_KEY, client=_client(handler))
    service, _feishu = make_service(minutes_llm=llm)
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert result["status"] == "failed"
    assert "todos JSON was empty" in (result["error"] or "")
    assert _FIXTURE_MARK not in (result["error"] or "")
    with pytest.raises(MeetingServiceError):
        service.get_draft(run["id"])


def test_non_json_model_output_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, json=_completion("here are the minutes, not json")
        )

    llm = VolcanoMinutesLLM(api_key=_KEY, client=_client(handler))
    service, _feishu = make_service(minutes_llm=llm)
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert result["status"] == "failed"
    assert "not JSON" in (result["error"] or "")
    assert _FIXTURE_MARK not in (result["error"] or "")
    assert result["draft_summary"] is None


def test_connection_error_does_not_fall_back_to_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError(f"connection refused for {_KEY}")

    llm = VolcanoMinutesLLM(api_key=_KEY, client=_client(handler))
    service, feishu = make_service(minutes_llm=llm)
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert result["status"] == "failed"
    error = result["error"] or ""
    assert "connection refused" in error
    assert "[redacted]" in error
    assert _KEY not in error
    assert _FIXTURE_MARK not in error
    assert feishu.calls == 0


def test_invalid_due_date_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][1]["content"]
        if "Minutes JSON:" not in user:
            return httpx.Response(
                200, json=_completion(json.dumps(_VENDOR_MINUTES))
            )
        payload = {
            "todos": [
                {
                    "title": "Vendor task",
                    "description": "Write the vendor notes",
                    "assignee": "李四",
                    "due_date": "next Friday",
                    "source_quote": None,
                }
            ]
        }
        return httpx.Response(200, json=_completion(json.dumps(payload)))

    llm = VolcanoMinutesLLM(api_key=_KEY, client=_client(handler))
    service, _feishu = make_service(minutes_llm=llm)
    run = service.create_run()
    result = service.import_transcript(run["id"], _TRANSCRIPT)
    assert result["status"] == "failed"
    assert "due_date" in (result["error"] or "")
    assert _FIXTURE_MARK not in (result["error"] or "")
    assert result["draft_summary"] is None
