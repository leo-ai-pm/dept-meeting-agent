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
    VOLCANO_ASR_DEFAULT_RESOURCE_ID,
    VOLCANO_ASR_DEFAULT_TIMEOUT,
    VOLCANO_ASR_DEFAULT_UID,
    VOLCANO_ASR_FLASH_URL,
    VOLCANO_ASR_MAX_BYTES,
    VOLCANO_ASR_SUCCESS_CODE,
    ProviderNotConfigured,
    VolcanoDoubaoTranscriber,
)
from app.meeting.errors import MeetingServiceError
from app.meeting.schema import sha256_text
from app.meeting.service import build_meeting_service
from tests.app.meeting.conftest import make_service

_FIXTURE_MARK = "增长组周会"
_VENDOR = "本周增长组决定上线豆包语音转写。"
_SPEECH_KEY = "speech-secret-key"
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
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _clear_asr_env(monkeypatch) -> None:
    for name in _ASR_ENV:
        monkeypatch.delenv(name, raising=False)


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


def test_flash_posts_base64_audio_and_returns_vendor_text():
    audio = b"RIFFfake-audio"
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = request.content
        return _success()

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
    assert seen["url"] == VOLCANO_ASR_FLASH_URL
    headers = seen["headers"]
    assert isinstance(headers, httpx.Headers)
    assert headers["x-api-key"] == _SPEECH_KEY
    assert headers["x-api-resource-id"] == VOLCANO_ASR_DEFAULT_RESOURCE_ID
    assert headers["x-api-sequence"] == "-1"
    uuid.UUID(headers["x-api-request-id"])
    assert "x-api-app-key" not in headers
    assert "x-api-access-key" not in headers
    assert "authorization" not in headers
    assert headers["content-type"].startswith("application/json")
    raw = seen["body"]
    assert isinstance(raw, bytes)
    payload = json.loads(raw)
    assert payload["user"] == {"uid": VOLCANO_ASR_DEFAULT_UID}
    assert payload["request"] == {"model_name": "bigmodel"}
    assert payload["audio"]["format"] == "wav"
    assert "codec" not in payload["audio"]
    assert "url" not in payload["audio"]
    assert payload["audio"]["data"] == base64.b64encode(audio).decode("ascii")
    assert b"RIFFfake-audio" not in raw
    assert b"../" not in raw
    assert _SPEECH_KEY.encode() not in raw
    assert b"old-app" not in raw
    events = service.replay(run["id"])["events"]
    transcribed = next(
        event for event in events if event["type"] == "tool.transcribe"
    )
    assert transcribed["payload"]["provider"] == "volcano_doubao"
    assert transcribed["payload"]["sha256"] == sha256_text(_VENDOR)
    assert feishu.calls == 0
    assert _FIXTURE_MARK not in _VENDOR


def test_filename_suffix_sets_mp3_and_ogg_hints():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return _success("一句。")

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
    assert transcriber.transcribe(b"abc", filename="note.mp3") == "一句。"
    assert transcriber.transcribe(b"abc", filename="room.ogg") == "一句。"
    assert transcriber.transcribe(b"abc", filename="clip.webm") == "一句。"
    assert seen[0]["audio"]["format"] == "mp3"
    assert "codec" not in seen[0]["audio"]
    assert seen[1]["audio"]["format"] == "ogg"
    assert seen[1]["audio"]["codec"] == "opus"
    assert "format" not in seen[2]["audio"]


def test_utterances_are_used_when_result_text_is_missing():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
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

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
    assert (
        transcriber.transcribe(b"abc", filename="clip.wav")
        == "第一句。第二句。"
    )


def test_result_text_wins_over_utterances():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return _success(
            "完整文本。",
            result={"text": "完整文本。", "utterances": [{"text": "分句。"}]},
        )

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
    assert transcriber.transcribe(b"abc", filename="clip.wav") == "完整文本。"


def test_status_failure_does_not_return_body_text_or_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "message": "ignored body message",
                "result": {"text": "SHOULD_NOT_BE_TRANSCRIPT"},
            },
            headers={
                "X-Api-Status-Code": "45000151",
                "X-Api-Message": "audio format invalid",
                "X-Tt-Logid": "log-format",
            },
        )

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
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
    assert "SHOULD_NOT_BE_TRANSCRIPT" not in error
    assert _FIXTURE_MARK not in error
    assert feishu.calls == 0
    with pytest.raises(MeetingServiceError):
        service.get_draft(run["id"])


def test_silence_status_does_not_invent_transcript():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"message": "静音音频", "result": {"text": ""}},
            headers={"X-Api-Status-Code": "20000003"},
        )

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
    with pytest.raises(RuntimeError, match="20000003") as raised:
        transcriber.transcribe(b"abc", filename="clip.wav")
    assert "静音音频" in str(raised.value)
    assert _FIXTURE_MARK not in str(raised.value)


def test_http_error_redacts_the_key_and_does_not_fall_back():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, text=f"rejected {_SPEECH_KEY}")

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
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


def test_missing_transcript_field_does_not_invent_text():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={"audio_info": {"duration": 10}, "result": {}},
            headers={"X-Api-Status-Code": VOLCANO_ASR_SUCCESS_CODE},
        )

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
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
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            text="not-json",
            headers={"X-Api-Status-Code": VOLCANO_ASR_SUCCESS_CODE},
        )

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
    with pytest.raises(RuntimeError, match="not JSON"):
        transcriber.transcribe(b"abc", filename="clip.wav")


def test_connection_error_does_not_fall_back_to_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("connection refused")

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
    service, feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    assert result["status"] == "failed"
    assert result["transcript_sha256"] is None
    assert _FIXTURE_MARK not in (result["error"] or "")
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

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY,
        client=_client(handler),
        max_bytes=4,
    )
    with pytest.raises(RuntimeError, match="audio is empty"):
        transcriber.transcribe(b"", filename="clip.wav")
    with pytest.raises(RuntimeError, match="4 bytes"):
        transcriber.transcribe(b"12345", filename="clip.wav")
    assert VOLCANO_ASR_MAX_BYTES == 100 * 1024 * 1024


def test_older_console_headers_are_used_when_api_key_is_absent():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["app"] = request.headers["x-api-app-key"]
        seen["access"] = request.headers["x-api-access-key"]
        seen["resource"] = request.headers["x-api-resource-id"]
        assert "x-api-key" not in request.headers
        return _success("旧版控制台。")

    transcriber = VolcanoDoubaoTranscriber(
        app_key="app-id",
        access_key="access-token",
        resource_id="volc.bigasr.auc_turbo",
        uid="meeting-user",
        client=_client(handler),
    )
    assert (
        transcriber.transcribe(b"abc", filename="clip.wav") == "旧版控制台。"
    )
    assert seen == {
        "app": "app-id",
        "access": "access-token",
        "resource": "volc.bigasr.auc_turbo",
    }


def test_factory_switches_mock_volcano_and_openai(monkeypatch):
    _clear_asr_env(monkeypatch)
    monkeypatch.delenv("MOCK_ASR", raising=False)
    monkeypatch.setenv("VOLCANO_ASR_API_KEY", _SPEECH_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ASR_BACKEND", "openai")
    assert build_meeting_service().transcriber.name == "mock"

    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.delenv("ASR_BACKEND", raising=False)
    volcano = build_meeting_service().transcriber
    assert volcano.name == "volcano_doubao"
    assert volcano._url == VOLCANO_ASR_FLASH_URL
    assert volcano._api_key == _SPEECH_KEY
    assert volcano._resource_id == VOLCANO_ASR_DEFAULT_RESOURCE_ID
    assert volcano._uid == VOLCANO_ASR_DEFAULT_UID
    assert volcano._timeout == VOLCANO_ASR_DEFAULT_TIMEOUT

    monkeypatch.setenv("ASR_BACKEND", "openai")
    assert build_meeting_service().transcriber.name == "openai_whisper"
    monkeypatch.setenv("ASR_BACKEND", "whisper")
    assert build_meeting_service().transcriber.name == "openai_whisper"
    monkeypatch.setenv("ASR_BACKEND", "Volcano")
    assert build_meeting_service().transcriber.name == "volcano_doubao"


def test_factory_reads_speech_key_aliases_and_options(monkeypatch):
    _clear_asr_env(monkeypatch)
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.setenv("VOLCANO_ASR_API_KEY", "  ")
    monkeypatch.setenv("VOLCANO_OPENSPEECH_API_KEY", "openspeech-key")
    monkeypatch.setenv("VOLCANO_API_KEY", "ark-minutes-key")
    monkeypatch.setenv("VOLCANO_ASR_RESOURCE_ID", " volc.custom ")
    monkeypatch.setenv("VOLCANO_ASR_UID", " meeting-user ")
    monkeypatch.setenv("VOLCANO_ASR_TIMEOUT", "90")
    transcriber = build_meeting_service().transcriber
    assert transcriber.name == "volcano_doubao"
    assert transcriber._api_key == "openspeech-key"
    assert transcriber._resource_id == "volc.custom"
    assert transcriber._uid == "meeting-user"
    assert transcriber._timeout == 90.0

    monkeypatch.setenv("VOLCANO_ASR_API_KEY", "preferred-key")
    preferred = build_meeting_service().transcriber
    assert preferred._api_key == "preferred-key"

    monkeypatch.setenv("VOLCANO_ASR_TIMEOUT", "nope")
    assert (
        build_meeting_service().transcriber._timeout
        == VOLCANO_ASR_DEFAULT_TIMEOUT
    )
    monkeypatch.setenv("VOLCANO_ASR_TIMEOUT", "0")
    assert (
        build_meeting_service().transcriber._timeout
        == VOLCANO_ASR_DEFAULT_TIMEOUT
    )


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
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "code": 45000001,
                "message": "bad parameter",
                "result": {"text": "SHOULD_NOT_BE_TRANSCRIPT"},
            },
        )

    transcriber = VolcanoDoubaoTranscriber(
        api_key=_SPEECH_KEY, client=_client(handler)
    )
    with pytest.raises(RuntimeError, match="45000001") as raised:
        transcriber.transcribe(b"abc", filename="clip.wav")
    assert "SHOULD_NOT_BE_TRANSCRIPT" not in str(raised.value)
    assert "bad parameter" in str(raised.value)
