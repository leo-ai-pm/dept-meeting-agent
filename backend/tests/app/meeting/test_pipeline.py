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

from app.meeting.adapters.minutes_llm import UnconfiguredMinutesLLM
from app.meeting.adapters.transcriber import UnconfiguredTranscriber
from app.meeting.schema import sha256_bytes
from tests.app.meeting.conftest import make_client, make_service


class _CapturingTranscriber:
    name = "capture"

    def __init__(self) -> None:
        self.audio: list[tuple[bytes, str]] = []

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        self.audio.append((audio, filename))
        return "captured from the microphone"


def _states(replay):
    return [
        event["payload"]["to"]
        for event in replay["events"]
        if event["type"] == "state.changed"
    ]


def test_import_transcript_skips_asr_and_awaits_review():
    service, feishu = make_service()
    client = make_client(service)
    created = client.post("/meeting/runs", json={"title": "weekly"})
    assert created.status_code == 201
    assert created.json()["status"] == "created"
    run_id = created.json()["id"]

    imported = client.post(
        f"/meeting/runs/{run_id}/import/transcript",
        json={"text": "张三会后整理实验数据。"},
    )
    assert imported.status_code == 200
    body = imported.json()
    assert body["status"] == "awaiting_review"
    assert body["draft_summary"]["todo_count"] == 2
    assert feishu.calls == 0

    replay = client.get(f"/meeting/runs/{run_id}/replay").json()
    assert _states(replay) == ["ingesting", "drafting", "awaiting_review"]
    types = [event["type"] for event in replay["events"]]
    assert "tool.transcribe" not in types
    assert "tool.draft_minutes" in types
    assert "tool.extract_todos" in types
    assert "review.pending" in types


def test_import_audio_and_live_stop_run_transcribe():
    service, _feishu = make_service()
    client = make_client(service)

    audio_id = client.post("/meeting/runs").json()["id"]
    audio = client.post(
        f"/meeting/runs/{audio_id}/import/audio",
        json={"filename": "weekly.wav", "content_base64": "bW9jay1hdWRpbw=="},
    )
    assert audio.status_code == 200
    assert audio.json()["status"] == "awaiting_review"
    audio_states = _states(
        client.get(f"/meeting/runs/{audio_id}/replay").json()
    )
    assert audio_states == [
        "ingesting",
        "transcribing",
        "drafting",
        "awaiting_review",
    ]

    live_id = client.post("/meeting/runs").json()["id"]
    started = client.post(f"/meeting/runs/{live_id}/live/start")
    assert started.status_code == 200
    assert started.json()["status"] == "created"
    assert started.json()["live_active"] is True
    stopped = client.post(f"/meeting/runs/{live_id}/live/stop", json={})
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "awaiting_review"
    assert stopped.json()["input"]["bytes"] == len(
        f"mock-live:{live_id}".encode()
    )
    assert stopped.json()["input"]["filename"] == "live-recording.bin"
    live_states = _states(client.get(f"/meeting/runs/{live_id}/replay").json())
    assert live_states == [
        "ingesting",
        "transcribing",
        "drafting",
        "awaiting_review",
    ]


def test_live_stop_with_audio_uses_those_bytes():
    transcriber = _CapturingTranscriber()
    service, feishu = make_service(transcriber=transcriber)
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    started = client.post(f"/meeting/runs/{run_id}/live/start")
    assert started.status_code == 200

    raw = b"RIFF\x00live-meeting-audio"
    stopped = client.post(
        f"/meeting/runs/{run_id}/live/stop",
        json={
            "filename": "live-recording.ogg",
            "content_base64": base64.b64encode(raw).decode("ascii"),
        },
    )
    assert stopped.status_code == 200
    body = stopped.json()
    assert body["status"] == "awaiting_review"
    assert body["live_active"] is False
    assert body["input"]["kind"] == "live_audio"
    assert body["input"]["filename"] == "live-recording.ogg"
    assert body["input"]["bytes"] == len(raw)
    assert body["input"]["sha256"] == sha256_bytes(raw)
    assert transcriber.audio == [(raw, "live-recording.ogg")]
    assert feishu.calls == 0
    replay = client.get(f"/meeting/runs/{run_id}/replay").json()
    assert _states(replay) == [
        "ingesting",
        "transcribing",
        "drafting",
        "awaiting_review",
    ]
    assert "tool.transcribe" in [event["type"] for event in replay["events"]]


def test_live_stop_without_audio_fails_closed_for_live_asr():
    transcriber = _CapturingTranscriber()
    service, feishu = make_service(transcriber=transcriber)
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    assert client.post(f"/meeting/runs/{run_id}/live/start").status_code == 200

    missing = client.post(f"/meeting/runs/{run_id}/live/stop")
    assert missing.status_code == 422
    assert missing.json()["detail"]["error"] == "empty_audio"
    assert "MOCK_ASR=0" in missing.json()["detail"]["message"]

    empty = client.post(
        f"/meeting/runs/{run_id}/live/stop",
        json={"filename": "live-recording.ogg", "content_base64": ""},
    )
    assert empty.status_code == 422
    assert empty.json()["detail"]["error"] == "empty_audio"

    snapshot = client.get(f"/meeting/runs/{run_id}").json()
    assert snapshot["status"] == "created"
    assert snapshot["live_active"] is True
    assert snapshot["transcript_sha256"] is None
    assert snapshot["input"]["sha256"] is None
    assert transcriber.audio == []
    assert feishu.calls == 0
    assert client.get(f"/meeting/runs/{run_id}/draft").status_code == 404

    raw = b"ogg-audio"
    recovered = client.post(
        f"/meeting/runs/{run_id}/live/stop",
        json={
            "filename": "live-recording.ogg",
            "content_base64": base64.b64encode(raw).decode("ascii"),
        },
    )
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "awaiting_review"
    assert transcriber.audio == [(raw, "live-recording.ogg")]


def test_live_stop_with_audio_uses_configured_asr_failure():
    service, feishu = make_service(transcriber=UnconfiguredTranscriber())
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    client.post(f"/meeting/runs/{run_id}/live/start")
    stopped = client.post(
        f"/meeting/runs/{run_id}/live/stop",
        json={"filename": "live-recording.ogg", "content_base64": "YQ=="},
    )
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "failed"
    assert stopped.json()["input"]["bytes"] == 1
    assert "MOCK_ASR" in (stopped.json()["error"] or "")
    assert client.get(f"/meeting/runs/{run_id}/draft").status_code == 404
    assert feishu.calls == 0


def test_live_stop_without_start_does_not_draft():
    service, feishu = make_service()
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    stopped = client.post(f"/meeting/runs/{run_id}/live/stop", json={})
    assert stopped.status_code == 409
    assert client.get(f"/meeting/runs/{run_id}").json()["status"] == "created"
    assert feishu.calls == 0


def test_unconfigured_asr_fails_closed():
    service, feishu = make_service(transcriber=UnconfiguredTranscriber())
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    imported = client.post(
        f"/meeting/runs/{run_id}/import/audio",
        json={"content_base64": "YQ=="},
    )
    assert imported.status_code == 200
    assert imported.json()["status"] == "failed"
    assert "MOCK_ASR" in (imported.json()["error"] or "")
    draft = client.get(f"/meeting/runs/{run_id}/draft")
    assert draft.status_code == 404
    assert feishu.calls == 0


def test_unconfigured_llm_fails_closed():
    service, feishu = make_service(minutes_llm=UnconfiguredMinutesLLM())
    run = service.create_run()
    result = service.import_transcript(run["id"], "hello meeting")
    assert result["status"] == "failed"
    assert "MOCK_LLM" in (result["error"] or "")
    assert feishu.calls == 0
