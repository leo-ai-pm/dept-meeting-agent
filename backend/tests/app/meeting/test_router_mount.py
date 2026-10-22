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

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.router import register_routers


def test_meeting_routes_mount_beside_kernel_runs():
    app = FastAPI()
    register_routers(app)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/meeting/runs" in paths
    assert "/meeting/runs/{run_id}/write_feishu_tasks" in paths
    assert "/meeting/ui" in paths
    assert "/runs" in paths
    assert "/runs/{run_id}/import/transcript" not in paths

    client = TestClient(app)
    created = client.post("/meeting/runs", json={})
    assert created.status_code == 201
    run_id = created.json()["id"]
    imported = client.post(
        f"/meeting/runs/{run_id}/import/transcript",
        json={"text": "部门周会纪要草稿。"},
    )
    assert imported.status_code == 200
    assert imported.json()["status"] == "awaiting_review"
    page = client.get("/meeting/ui")
    assert page.status_code == 200
    assert "Meeting run acceptance" in page.text
    assert "会议运行验收" in page.text
    assert "Create run" in page.text
    assert "Import transcript" in page.text
    assert "Start live" in page.text
    assert "Stop live" in page.text
    assert "Approve" in page.text
    assert "Reject" in page.text
    assert "Write with token" in page.text
    assert "Write without token" in page.text
    assert "Replay" in page.text
    assert 'aria-pressed="false"' in page.text
    assert 'aria-live="polite"' in page.text
    assert "getUserMedia" in page.text
    assert "MediaRecorder" in page.text
    assert "content_base64" in page.text
    assert "audio/ogg;codecs=opus" in page.text
    assert "live-recording.ogg" in page.text
    denied = client.post(
        f"/meeting/runs/{run_id}/export_feishu",
        json={},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "missing_token"
