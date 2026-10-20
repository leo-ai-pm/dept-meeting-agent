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

from app.meeting.adapters.minutes_llm import UnconfiguredMinutesLLM
from app.meeting.adapters.transcriber import UnconfiguredTranscriber
from tests.app.meeting.conftest import make_client, make_service


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
    live_states = _states(client.get(f"/meeting/runs/{live_id}/replay").json())
    assert live_states == [
        "ingesting",
        "transcribing",
        "drafting",
        "awaiting_review",
    ]


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
