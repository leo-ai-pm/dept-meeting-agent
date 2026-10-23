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

import base64
import json
import uuid

import httpx
import pytest

from app.meeting.adapters.transcriber import (
    VOLCANO_ASR_DEFAULT_HTTP_TIMEOUT,
    VOLCANO_ASR_DEFAULT_POLL_INTERVAL,
    VOLCANO_ASR_DEFAULT_POLL_MAX_INTERVAL,
    VOLCANO_ASR_DEFAULT_POLL_TIMEOUT,
    VOLCANO_ASR_DEFAULT_RESOURCE_ID,
    VOLCANO_ASR_DEFAULT_UID,
    VOLCANO_ASR_MAX_BYTES,
    VOLCANO_ASR_QUERY_URL,
    VOLCANO_ASR_SUBMIT_URL,
    VOLCANO_ASR_SUCCESS_CODE,
    ProviderNotConfigured,
    VolcanoDoubaoTranscriber,
    _byte_limit,
)
from app.meeting.errors import MeetingServiceError
from app.meeting.schema import sha256_text
from app.meeting.service import build_meeting_service
from tests.app.meeting.conftest import make_service

_FIXTURE_MARK = "增长组周会"
_VENDOR = "本周增长组决定上线豆包语音转写。"
_SPEECH_KEY = "speech-secret-key"
_NOT_TRANSCRIPT = "SHOULD_NOT_BE_TRANSCRIPT"
_ASR_ENV = (
    "ASR_BACKEND",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "MEETING_ASR_MODEL",
    "VOLCANO_API_KEY",
    "ARK_API_KEY",
    "VOLCANO_ASR_API_KEY",
    "VOLCANO_OPENSPEECH_API_KEY",
    "VOLCANO_ASR_APP_KEY",
    "VOLCANO_ASR_APP_ID",
    "VOLCANO_ASR_ACCESS_KEY",
    "VOLCANO_ASR_ACCESS_TOKEN",
    "VOLCANO_ASR_RESOURCE_ID",
    "VOLCANO_ASR_UID",
    "VOLCANO_ASR_TIMEOUT",
    "VOLCANO_ASR_HTTP_TIMEOUT",
    "VOLCANO_ASR_POLL_TIMEOUT",
    "VOLCANO_ASR_POLL_INTERVAL",
    "VOLCANO_ASR_POLL_MAX_INTERVAL",
)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _clear_asr_env(monkeypatch) -> None:
    for name in _ASR_ENV:
        monkeypatch.delenv(name, raising=False)


def _accepted() -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "X-Api-Status-Code": VOLCANO_ASR_SUCCESS_CODE,
            "X-Api-Message": "OK",
            "X-Tt-Logid": "log-submit",
        },
    )


def _success(text: str = _VENDOR, **extra) -> httpx.Response:
    body = {"result": {"text": text}}
    body.update(extra)
    return httpx.Response(
        200,
        json=body,
        headers={
            "X-Api-Status-Code": VOLCANO_ASR_SUCCESS_CODE,
            "X-Api-Message": "OK",
            "X-Tt-Logid": "log-success",
        },
    )


def _pending(code: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"result": {"text": _NOT_TRANSCRIPT}},
        headers={
            "X-Api-Status-Code": code,
            "X-Api-Message": "OK",
            "X-Tt-Logid": "log-pending",
        },
    )


def _script(*queries: httpx.Response):
    pending = list(queries)

    def handler(request: httpx.Request) -> httpx.Response:
        handler.requests.append(request)
        if request.url.path.endswith("/submit"):
            return _accepted()
        if request.url.path.endswith("/query"):
            if not pending:
                raise AssertionError("unexpected extra query")
            return pending.pop(0)
        raise AssertionError(str(request.url))

    handler.requests = []
    return handler


def _transcriber(handler, **kwargs) -> VolcanoDoubaoTranscriber:
    return VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY,
        client=_client(handler),
        **kwargs,
    )


def test_standard_posts_base64_then_queries_vendor_text():
    audio = b"RIFFfake-audio"
    handler = _script(_success())
    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY,
        app_key="old-app",
        access_key="old-access",
        client=_client(handler),
    )
    service, feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="../clip.wav", content=audio
    )
    assert result["status"] == "awaiting_review"
    assert result["transcript_sha256"] == sha256_text(_VENDOR)
    assert len(handler.requests) == 2
    submit, query = handler.requests
    assert str(submit.url) == VOLCANO_ASR_SUBMIT_URL
    assert str(query.url) == VOLCANO_ASR_QUERY_URL
    assert "flash" not in str(submit.url)
    assert "auc_turbo" not in str(submit.url)
    assert submit.headers["x-api-key"] == _SPEECH_KEY
    assert query.headers["x-api-key"] == _SPEECH_KEY
    assert (
        submit.headers["x-api-resource-id"]
        == VOLCANO_ASR_DEFAULT_RESOURCE_ID
        == "volc.seedasr.auc"
    )
    assert query.headers["x-api-resource-id"] == "volc.seedasr.auc"
    assert submit.headers["x-api-sequence"] == "-1"
    assert "x-api-sequence" not in query.headers
    assert (
        submit.headers["x-api-request-id"] == query.headers["x-api-request-id"]
    )
    uuid.UUID(submit.headers["x-api-request-id"])
    assert "x-api-app-key" not in submit.headers
    assert "x-api-access-key" not in submit.headers
    assert "authorization" not in submit.headers
    assert submit.headers["content-type"].startswith("application/json")
    payload = json.loads(submit.content)
    assert payload["user"] == {"uid": VOLCANO_ASR_DEFAULT_UID}
    assert payload["request"] == {"model_name": "bigmodel"}
    assert payload["audio"]["format"] == "wav"
    assert "codec" not in payload["audio"]
    assert "url" not in payload["audio"]
    assert payload["audio"]["data"] == base64.b64encode(audio).decode("ascii")
    assert b"RIFFfake-audio" not in submit.content
    assert b"../" not in submit.content
    assert _SPEECH_KEY.encode() not in submit.content
    assert b"old-app" not in submit.content
    assert json.loads(query.content) == {}
    events = service.replay(run["id"])["events"]
    transcribed = next(
        event for event in events if event["type"] == "tool.transcribe"
    )
    assert transcribed["payload"]["provider"] == "volcano_doubao"
    assert transcribed["payload"]["sha256"] == sha256_text(_VENDOR)
    assert feishu.calls == 0
    assert _FIXTURE_MARK not in _VENDOR


def test_submit_body_text_is_not_the_transcript():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return httpx.Response(
                200,
                json={"result": {"text": _NOT_TRANSCRIPT}},
                headers={
                    "X-Api-Status-Code": VOLCANO_ASR_SUCCESS_CODE,
                    "X-Api-Message": "OK",
                },
            )
        return _success("来自查询。")

    transcriber = _transcriber(handler)
    assert transcriber.transcribe(b"abc", filename="clip.wav") == "来自查询。"


def test_filename_suffix_sets_format_hints():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            seen.append(json.loads(request.content))
            return _accepted()
        return _success("一句。")

    transcriber = _transcriber(handler)
    assert transcriber.transcribe(b"abc", filename="note.mp3") == "一句。"
    assert transcriber.transcribe(b"abc", filename="room.ogg") == "一句。"
    assert transcriber.transcribe(b"abc", filename="clip.m4a") == "一句。"
    assert transcriber.transcribe(b"abc", filename="clip.webm") == "一句。"
    assert seen[0]["audio"]["format"] == "mp3"
    assert "codec" not in seen[0]["audio"]
    assert seen[1]["audio"]["format"] == "ogg"
    assert seen[1]["audio"]["codec"] == "opus"
    assert seen[2]["audio"]["format"] == "m4a"
    assert "format" not in seen[3]["audio"]
    assert "url" not in seen[0]["audio"]


def test_utterances_are_used_when_result_text_is_missing():
    handler = _script(
        httpx.Response(
            200,
            json={
                "result": {
                    "utterances": [
                        {"text": "第一句。"},
                        {"text": "  "},
                        {"text": "第二句。"},
                    ]
                }
            },
            headers={"X-Api-Status-Code": VOLCANO_ASR_SUCCESS_CODE},
        )
    )
    transcriber = _transcriber(handler)
    assert (
        transcriber.transcribe(b"abc", filename="clip.wav")
        == "第一句。第二句。"
    )


def test_result_text_wins_over_utterances():
    handler = _script(
        _success(
            "完整文本。",
            result={"text": "完整文本。", "utterances": [{"text": "分句。"}]},
        )
    )
    transcriber = _transcriber(handler)
    assert transcriber.transcribe(b"abc", filename="clip.wav") == "完整文本。"


def test_pending_codes_wait_then_return_the_finished_text():
    clock = _Clock()
    handler = _script(
        _pending("20000001"),
        _pending("20000002"),
        _success(),
    )
    transcriber = _transcriber(
        handler,
        poll_timeout=1800,
        poll_interval=2,
        poll_max_interval=15,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert transcriber.transcribe(b"abc", filename="clip.wav") == _VENDOR
    assert clock.sleeps == [2, 4]
    assert len(handler.requests) == 4


def test_pending_until_deadline_does_not_invent_transcript():
    clock = _Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return _accepted()
        return _pending("20000001")

    transcriber = _transcriber(
        handler,
        poll_timeout=5,
        poll_interval=2,
        poll_max_interval=15,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    with pytest.raises(RuntimeError, match="timed out after 5s") as raised:
        transcriber.transcribe(b"abc", filename="clip.wav")
    assert _NOT_TRANSCRIPT not in str(raised.value)
    assert _FIXTURE_MARK not in str(raised.value)
    assert clock.sleeps == [2, 3]
    assert clock.now == 5


def test_query_transport_error_retries_until_success():
    clock = _Clock()
    calls = {"query": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return _accepted()
        calls["query"] += 1
        if calls["query"] == 1:
            raise httpx.ConnectError(f"connection refused {_SPEECH_KEY}")
        return _success("重试后的文本。")

    transcriber = _transcriber(
        handler,
        poll_interval=2,
        poll_max_interval=15,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    assert (
        transcriber.transcribe(b"abc", filename="clip.wav") == "重试后的文本。"
    )
    assert clock.sleeps == [2]
    assert calls["query"] == 2


def test_query_transport_error_timeout_redacts_the_key():
    clock = _Clock()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/submit"):
            return _accepted()
        raise httpx.ConnectError(f"connection refused {_SPEECH_KEY}")

    transcriber = _transcriber(
        handler,
        poll_timeout=5,
        poll_interval=2,
        poll_max_interval=15,
        clock=clock.monotonic,
        sleeper=clock.sleep,
    )
    service, feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    error = result["error"] or ""
    assert result["status"] == "failed"
    assert result["transcript_sha256"] is None
    assert "timed out after 5s" in error
    assert "[redacted]" in error
    assert _SPEECH_KEY not in error
    assert _FIXTURE_MARK not in error
    assert feishu.calls == 0
    assert clock.now == 5


def test_status_failure_does_not_return_body_text_or_fixture():
    handler = _script(
        httpx.Response(
            200,
            json={
                "message": "ignored body message",
                "result": {"text": _NOT_TRANSCRIPT},
            },
            headers={
                "X-Api-Status-Code": "45000151",
                "X-Api-Message": "audio format invalid",
                "X-Tt-Logid": "log-format",
            },
        )
    )
    transcriber = _transcriber(handler)
    service, feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    error = result["error"] or ""
    assert result["status"] == "failed"
    assert result["transcript_sha256"] is None
    assert "45000151" in error
    assert "audio format invalid" in error
    assert "log-format" in error
    assert _NOT_TRANSCRIPT not in error
    assert _FIXTURE_MARK not in error
    assert feishu.calls == 0
    with pytest.raises(MeetingServiceError):
        service.get_draft(run["id"])


def test_submit_rejection_does_not_query():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"result": {"text": _NOT_TRANSCRIPT}},
            headers={
                "X-Api-Status-Code": "45000030",
                "X-Api-Message": "requested resource not granted",
                "X-Tt-Logid": "log-grant",
            },
        )

    transcriber = _transcriber(handler)
    with pytest.raises(RuntimeError, match="45000030") as raised:
        transcriber.transcribe(b"abc", filename="clip.wav")
    assert calls == ["/api/v3/auc/bigmodel/submit"]
    assert "not granted" in str(raised.value)
    assert _NOT_TRANSCRIPT not in str(raised.value)


def test_submit_missing_status_does_not_query():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"result": {"text": _NOT_TRANSCRIPT}},
        )

    transcriber = _transcriber(handler)
    with pytest.raises(RuntimeError, match="submit missing") as raised:
        transcriber.transcribe(b"abc", filename="clip.wav")
    assert calls == ["/api/v3/auc/bigmodel/submit"]
    assert _NOT_TRANSCRIPT not in str(raised.value)


def test_silence_status_does_not_invent_transcript():
    handler = _script(
        httpx.Response(
            200,
            json={"message": "静音音频", "result": {"text": ""}},
            headers={"X-Api-Status-Code": "20000003"},
        )
    )
    transcriber = _transcriber(handler)
    with pytest.raises(RuntimeError, match="20000003") as raised:
        transcriber.transcribe(b"abc", filename="clip.wav")
    assert "静音音频" in str(raised.value)
    assert _FIXTURE_MARK not in str(raised.value)


def test_http_error_redacts_the_key_and_does_not_fall_back():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, text=f"rejected {_SPEECH_KEY}")

    transcriber = _transcriber(handler)
    service, feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    error = result["error"] or ""
    assert result["status"] == "failed"
    assert result["transcript_sha256"] is None
    assert "HTTP 401" in error
    assert "[redacted]" in error
    assert _SPEECH_KEY not in error
    assert _FIXTURE_MARK not in error
    assert feishu.calls == 0
    assert calls == ["/api/v3/auc/bigmodel/submit"]


def test_missing_transcript_field_does_not_invent_text():
    handler = _script(
        httpx.Response(
            200,
            json={"audio_info": {"duration": 10}, "result": {}},
            headers={"X-Api-Status-Code": VOLCANO_ASR_SUCCESS_CODE},
        )
    )
    transcriber = _transcriber(handler)
    service, _feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    assert result["status"] == "failed"
    assert "missing transcript" in (result["error"] or "")
    assert result["transcript_sha256"] is None
    assert _FIXTURE_MARK not in (result["error"] or "")


def test_non_json_success_does_not_invent_text():
    handler = _script(
        httpx.Response(
            200,
            text="not-json",
            headers={"X-Api-Status-Code": VOLCANO_ASR_SUCCESS_CODE},
        )
    )
    transcriber = _transcriber(handler)
    with pytest.raises(RuntimeError, match="not JSON"):
        transcriber.transcribe(b"abc", filename="clip.wav")


def test_connection_error_does_not_fall_back_to_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("connection refused")

    transcriber = _transcriber(handler)
    service, feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    assert result["status"] == "failed"
    assert result["transcript_sha256"] is None
    assert _FIXTURE_MARK not in (result["error"] or "")
    assert "connection refused" in (result["error"] or "")
    assert feishu.calls == 0


def test_blank_key_does_not_call_http():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("blank key must not call Doubao ASR")

    transcriber = VolcanoDoubaoTranscriber(
        api_key="  ", client=_client(handler)
    )
    with pytest.raises(ProviderNotConfigured, match="VOLCANO_ASR_API_KEY"):
        transcriber.transcribe(b"abc", filename="clip.wav")


def test_empty_and_oversize_audio_do_not_call_http():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("local rejection must not call Doubao ASR")

    transcriber = _transcriber(handler, max_bytes=4)
    with pytest.raises(RuntimeError, match="audio is empty"):
        transcriber.transcribe(b"", filename="clip.wav")
    with pytest.raises(RuntimeError, match="4 bytes"):
        transcriber.transcribe(b"12345", filename="clip.wav")
    assert VOLCANO_ASR_MAX_BYTES == 512 * 1024 * 1024
    assert _byte_limit(VOLCANO_ASR_MAX_BYTES) == "512MB"


def test_older_console_headers_are_used_when_api_key_is_absent():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/submit"):
            return _accepted()
        return _success("旧版控制台。")

    transcriber = VolcanoDoubaoTranscriber(
        app_key="app-id",
        access_key="access-token",
        resource_id="volc.bigasr.auc",
        uid="meeting-user",
        client=_client(handler),
    )
    assert (
        transcriber.transcribe(b"abc", filename="clip.wav") == "旧版控制台。"
    )
    assert len(seen) == 2
    for request in seen:
        assert request.headers["x-api-app-key"] == "app-id"
        assert request.headers["x-api-access-key"] == "access-token"
        assert request.headers["x-api-resource-id"] == "volc.bigasr.auc"
        assert "x-api-key" not in request.headers
    assert seen[0].headers["x-api-sequence"] == "-1"
    assert "x-api-sequence" not in seen[1].headers
    assert json.loads(seen[0].content)["user"] == {"uid": "meeting-user"}


def test_factory_switches_mock_volcano_and_openai(monkeypatch):
    _clear_asr_env(monkeypatch)
    monkeypatch.delenv("MOCK_ASR", raising=False)
    monkeypatch.setenv("VOLCANO_ASR_API_KEY", _SPEECH_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ASR_BACKEND", "openai")
    assert build_meeting_service().transcriber.name == "mock"

    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.delenv("ASR_BACKEND", raising=False)
    monkeypatch.setenv("VOLCANO_ASR_TIMEOUT", "1")
    volcano = build_meeting_service().transcriber
    assert volcano.name == "volcano_doubao"
    assert volcano._submit_url == VOLCANO_ASR_SUBMIT_URL
    assert volcano._query_url == VOLCANO_ASR_QUERY_URL
    assert volcano._api_key == _SPEECH_KEY
    assert volcano._resource_id == VOLCANO_ASR_DEFAULT_RESOURCE_ID
    assert volcano._uid == VOLCANO_ASR_DEFAULT_UID
    assert volcano._http_timeout == VOLCANO_ASR_DEFAULT_HTTP_TIMEOUT
    assert volcano._poll_timeout == VOLCANO_ASR_DEFAULT_POLL_TIMEOUT
    assert volcano._poll_interval == VOLCANO_ASR_DEFAULT_POLL_INTERVAL
    assert volcano._poll_max_interval == VOLCANO_ASR_DEFAULT_POLL_MAX_INTERVAL
    assert volcano._poll_timeout != 1

    monkeypatch.setenv("ASR_BACKEND", "openai")
    assert build_meeting_service().transcriber.name == "openai_whisper"
    monkeypatch.setenv("ASR_BACKEND", "whisper")
    assert build_meeting_service().transcriber.name == "openai_whisper"
    monkeypatch.setenv("ASR_BACKEND", "Volcano")
    assert build_meeting_service().transcriber.name == "volcano_doubao"
    monkeypatch.setenv("ASR_BACKEND", "volcano_flash")
    assert build_meeting_service().transcriber.name == "unconfigured"


def test_factory_reads_speech_key_aliases_and_options(monkeypatch):
    _clear_asr_env(monkeypatch)
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.setenv("VOLCANO_ASR_API_KEY", "  ")
    monkeypatch.setenv("VOLCANO_OPENSPEECH_API_KEY", "openspeech-key")
    monkeypatch.setenv("VOLCANO_API_KEY", "ark-minutes-key")
    monkeypatch.setenv("VOLCANO_ASR_RESOURCE_ID", " volc.custom ")
    monkeypatch.setenv("VOLCANO_ASR_UID", " meeting-user ")
    monkeypatch.setenv("VOLCANO_ASR_HTTP_TIMEOUT", "45")
    monkeypatch.setenv("VOLCANO_ASR_POLL_TIMEOUT", "90")
    monkeypatch.setenv("VOLCANO_ASR_POLL_INTERVAL", "1.5")
    monkeypatch.setenv("VOLCANO_ASR_POLL_MAX_INTERVAL", "8")
    transcriber = build_meeting_service().transcriber
    assert transcriber.name == "volcano_doubao"
    assert transcriber._api_key == "openspeech-key"
    assert transcriber._resource_id == "volc.custom"
    assert transcriber._uid == "meeting-user"
    assert transcriber._http_timeout == 45.0
    assert transcriber._poll_timeout == 90.0
    assert transcriber._poll_interval == 1.5
    assert transcriber._poll_max_interval == 8.0

    monkeypatch.setenv("VOLCANO_ASR_API_KEY", "preferred-key")
    preferred = build_meeting_service().transcriber
    assert preferred._api_key == "preferred-key"

    monkeypatch.setenv("VOLCANO_ASR_POLL_TIMEOUT", "nope")
    assert (
        build_meeting_service().transcriber._poll_timeout
        == VOLCANO_ASR_DEFAULT_POLL_TIMEOUT
    )
    monkeypatch.setenv("VOLCANO_ASR_POLL_TIMEOUT", "0")
    assert (
        build_meeting_service().transcriber._poll_timeout
        == VOLCANO_ASR_DEFAULT_POLL_TIMEOUT
    )
    monkeypatch.setenv("VOLCANO_ASR_HTTP_TIMEOUT", "0")
    assert (
        build_meeting_service().transcriber._http_timeout
        == VOLCANO_ASR_DEFAULT_HTTP_TIMEOUT
    )
    monkeypatch.setenv("VOLCANO_ASR_POLL_INTERVAL", "10")
    monkeypatch.setenv("VOLCANO_ASR_POLL_MAX_INTERVAL", "4")
    capped = build_meeting_service().transcriber
    assert capped._poll_interval == 10.0
    assert capped._poll_max_interval == 10.0


def test_factory_older_console_pair_and_partial_pair(monkeypatch):
    _clear_asr_env(monkeypatch)
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.setenv("VOLCANO_ASR_APP_ID", "app-id")
    monkeypatch.setenv("VOLCANO_ASR_ACCESS_TOKEN", "access-token")
    transcriber = build_meeting_service().transcriber
    assert transcriber.name == "volcano_doubao"
    assert transcriber._api_key == ""
    assert transcriber._app_key == "app-id"
    assert transcriber._access_key == "access-token"

    monkeypatch.delenv("VOLCANO_ASR_ACCESS_TOKEN", raising=False)
    missing = build_meeting_service().transcriber
    assert missing.name == "unconfigured"
    with pytest.raises(ProviderNotConfigured, match="VOLCANO_ASR_ACCESS_KEY"):
        missing.transcribe(b"abc", filename="clip.wav")


def test_ark_minutes_key_does_not_select_doubao(monkeypatch):
    _clear_asr_env(monkeypatch)
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.setenv("VOLCANO_API_KEY", "ark-minutes-key")
    monkeypatch.setenv("ARK_API_KEY", "ark-alias")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    service = build_meeting_service()
    assert service.transcriber.name == "unconfigured"
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    error = result["error"] or ""
    assert result["status"] == "failed"
    assert "豆包语音" in error
    assert "openspeech.bytedance.com" in error
    assert "env.example" in error
    assert "OPENAI_API_KEY" not in error
    assert "ark-minutes-key" not in error
    assert _FIXTURE_MARK not in error
    assert result["transcript_sha256"] is None


def test_unknown_backend_is_unconfigured_even_with_keys(monkeypatch):
    _clear_asr_env(monkeypatch)
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.setenv("ASR_BACKEND", "siliconflow")
    monkeypatch.setenv("VOLCANO_ASR_API_KEY", _SPEECH_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    service = build_meeting_service()
    assert service.transcriber.name == "unconfigured"
    with pytest.raises(ProviderNotConfigured, match="siliconflow"):
        service.transcriber.transcribe(b"abc", filename="clip.wav")


def test_body_status_code_rejects_a_transcript():
    handler = _script(
        httpx.Response(
            200,
            json={
                "code": 45000001,
                "message": "bad parameter",
                "result": {"text": _NOT_TRANSCRIPT},
            },
        )
    )
    transcriber = _transcriber(handler)
    with pytest.raises(RuntimeError, match="45000001") as raised:
        transcriber.transcribe(b"abc", filename="clip.wav")
    assert _NOT_TRANSCRIPT not in str(raised.value)
    assert "bad parameter" in str(raised.value)
