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

import httpx
import pytest

from app.meeting.adapters.minutes_llm import UnconfiguredMinutesLLM
from app.meeting.adapters.transcriber import (
    OpenAIWhisperTranscriber,
    ProviderNotConfigured,
)
from app.meeting.errors import MeetingServiceError
from app.meeting.schema import sha256_text
from app.meeting.service import build_meeting_service
from tests.app.meeting.conftest import make_service

_FIXTURE_MARK = "增长组周会"
_VENDOR = "vendor transcript, not the fixture"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_mock_asr_stays_default_when_openai_key_is_set(monkeypatch):
    monkeypatch.delenv("MOCK_ASR", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    service = build_meeting_service()
    assert service.transcriber.name == "mock"
    assert service.minutes_llm.name == "mock"


def test_missing_key_with_mock_asr_off_does_not_invent_transcript(monkeypatch):
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    service = build_meeting_service()
    assert service.transcriber.name == "unconfigured"
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    assert result["status"] == "failed"
    assert result["transcript_sha256"] is None
    assert "MOCK_ASR" in (result["error"] or "")
    assert _FIXTURE_MARK not in (result["error"] or "")
    with pytest.raises(MeetingServiceError) as raised:
        service.get_draft(run["id"])
    assert raised.value.status_code == 404


def test_blank_key_is_unconfigured_and_does_not_call_http(monkeypatch):
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    service = build_meeting_service()
    assert service.transcriber.name == "unconfigured"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise AssertionError("blank key must not call Whisper")

    transcriber = OpenAIWhisperTranscriber(
        api_key="  ", client=_client(handler)
    )
    with pytest.raises(ProviderNotConfigured, match="OPENAI_API_KEY"):
        transcriber.transcribe(b"abc", filename="clip.wav")


def test_openai_key_does_not_enable_minutes_llm(monkeypatch):
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://asr.example/v1")
    monkeypatch.setenv("MEETING_ASR_MODEL", "whisper-large-v3")
    service = build_meeting_service()
    assert service.transcriber.name == "openai_whisper"
    assert service.transcriber._url == (
        "https://asr.example/v1/audio/transcriptions"
    )
    assert service.transcriber._model == "whisper-large-v3"
    assert isinstance(service.minutes_llm, UnconfiguredMinutesLLM)
    assert service.minutes_llm.name == "unconfigured"


def test_whisper_posts_audio_transcriptions_and_returns_vendor_text():
    audio = b"RIFFfake-audio"
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        return httpx.Response(200, json={"text": _VENDOR})

    transcriber = OpenAIWhisperTranscriber(
        api_key="sk-test-key",
        base_url="https://asr.example/v1/",
        model="whisper-large-v3",
        client=_client(handler),
    )
    service, feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="../clip.wav", content=audio
    )
    assert result["status"] == "awaiting_review"
    assert result["transcript_sha256"] == sha256_text(_VENDOR)
    assert seen["url"] == "https://asr.example/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer sk-test-key"
    body = seen["body"]
    assert isinstance(body, bytes)
    assert b'name="model"' in body
    assert b"whisper-large-v3" in body
    assert b'name="response_format"' in body
    assert b"json" in body
    assert b'filename="clip.wav"' in body
    assert b"../" not in body
    assert audio in body
    assert b"audio/wav" in body
    assert b"sk-test-key" not in body
    events = service.replay(run["id"])["events"]
    transcribed = next(
        event for event in events if event["type"] == "tool.transcribe"
    )
    assert transcribed["payload"]["provider"] == "openai_whisper"
    assert transcribed["payload"]["sha256"] == sha256_text(_VENDOR)
    assert feishu.calls == 0
    assert _FIXTURE_MARK not in _VENDOR


def test_plain_text_compatible_endpoint_is_returned_as_is():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            text="plain vendor text",
            headers={"content-type": "text/plain"},
        )

    transcriber = OpenAIWhisperTranscriber(
        api_key="sk-test", client=_client(handler)
    )
    assert (
        transcriber.transcribe(b"abc", filename="note.mp3")
        == "plain vendor text"
    )


def test_http_error_fails_closed_and_redacts_the_key():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            401, json={"error": {"message": "rejected sk-secret"}}
        )

    transcriber = OpenAIWhisperTranscriber(
        api_key="sk-secret", client=_client(handler)
    )
    service, feishu = make_service(transcriber=transcriber)
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    assert result["status"] == "failed"
    assert result["transcript_sha256"] is None
    error = result["error"] or ""
    assert "HTTP 401" in error
    assert "[redacted]" in error
    assert "sk-secret" not in error
    assert _FIXTURE_MARK not in error
    assert feishu.calls == 0
    with pytest.raises(MeetingServiceError):
        service.get_draft(run["id"])


def test_missing_transcript_field_does_not_invent_text():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"text": None})

    transcriber = OpenAIWhisperTranscriber(
        api_key="sk-test", client=_client(handler)
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


def test_connection_error_does_not_fall_back_to_fixture():
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ConnectError("connection refused")

    transcriber = OpenAIWhisperTranscriber(
        api_key="sk-test", client=_client(handler)
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


def test_default_whisper_target_when_optional_env_is_blank(monkeypatch):
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "  ")
    monkeypatch.setenv("MEETING_ASR_MODEL", "")
    service = build_meeting_service()
    assert service.transcriber._url == (
        "https://api.openai.com/v1/audio/transcriptions"
    )
    assert service.transcriber._model == "whisper-1"
