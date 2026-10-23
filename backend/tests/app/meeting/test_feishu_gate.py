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
import subprocess

from app.meeting.adapters.feishu import (
    EnterpriseFeishuTaskAdapter,
    LarkCliFeishuTaskAdapter,
)
from tests.app.meeting.conftest import make_client, make_service


def _ready_run():
    service, feishu = make_service()
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    imported = client.post(
        f"/meeting/runs/{run_id}/import/transcript",
        json={"text": "会议结论与两条待办。"},
    )
    assert imported.json()["status"] == "awaiting_review"
    return client, service, feishu, run_id


def test_missing_token_is_hard_reject_and_never_calls_feishu():
    client, _service, feishu, run_id = _ready_run()
    denied = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={},
    )
    assert denied.status_code == 403
    detail = denied.json()["detail"]
    assert detail["error"] == "hard_reject"
    assert detail["reason"] == "missing_token"
    assert detail["feishu_called"] is False
    assert feishu.calls == 0
    assert client.get(f"/meeting/runs/{run_id}").json()["status"] == (
        "awaiting_review"
    )
    replay = client.get(f"/meeting/runs/{run_id}/replay").json()
    rejects = [
        event
        for event in replay["events"]
        if event["type"] == "permission.hard_reject"
    ]
    assert rejects[-1]["payload"]["feishu_called"] is False
    assert rejects[-1]["payload"]["reason"] == "missing_token"


def test_export_alias_also_hard_rejects_without_token():
    client, _service, feishu, run_id = _ready_run()
    denied = client.post(f"/meeting/runs/{run_id}/export_feishu", json={})
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "missing_token"
    assert feishu.calls == 0


def test_reject_issues_no_token_and_blocks_writes():
    client, _service, feishu, run_id = _ready_run()
    review = client.post(
        f"/meeting/runs/{run_id}/review",
        json={"decision": "reject"},
    )
    assert review.status_code == 200
    body = review.json()
    assert body["status"] == "rejected"
    assert body["review_token"] is None
    denied = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": "v1.forged"},
    )
    assert denied.status_code == 403
    assert feishu.calls == 0
    assert (
        client.get(f"/meeting/runs/{run_id}").json()["feishu_receipts"] == []
    )


def test_hash_mismatch_and_consumed_token_never_call_feishu():
    client, _service, feishu, run_id = _ready_run()
    approved = client.post(
        f"/meeting/runs/{run_id}/review",
        json={"decision": "approve"},
    ).json()
    token = approved["review_token"]
    draft = client.get(f"/meeting/runs/{run_id}/draft").json()
    edited = client.post(
        f"/meeting/runs/{run_id}/review",
        json={
            "decision": "edit_approve",
            "edits": {
                "minutes": draft["minutes"],
                "todos": [
                    {**draft["todos"][0], "title": "改过的待办标题"},
                    draft["todos"][1],
                ],
            },
        },
    )
    assert edited.status_code == 200
    new_token = edited.json()["review_token"]
    assert new_token != token

    mismatch = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": token},
    )
    assert mismatch.status_code == 403
    assert mismatch.json()["detail"]["reason"] == "hash_mismatch"
    assert feishu.calls == 0

    written = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": new_token},
    )
    assert written.status_code == 200
    assert written.json()["status"] == "succeeded"
    assert feishu.calls == 1
    assert len(written.json()["feishu_receipts"]) == 2

    again = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": new_token},
    )
    assert again.status_code == 403
    assert again.json()["detail"]["reason"] == "consumed"
    assert feishu.calls == 1
    replay = client.get(f"/meeting/runs/{run_id}/replay").json()
    blob = str(replay)
    assert new_token not in blob
    assert token not in blob


def test_expired_token_is_hard_reject():
    box = {"t": 5_000}
    service, feishu = make_service(ttl_seconds=5, clock=lambda: box["t"])
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    client.post(
        f"/meeting/runs/{run_id}/import/transcript",
        json={"text": "transcript"},
    )
    token = client.post(
        f"/meeting/runs/{run_id}/review", json={"decision": "approve"}
    ).json()["review_token"]
    box["t"] = 5_010
    denied = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={"review_token": token},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "expired"
    assert feishu.calls == 0
    assert client.get(f"/meeting/runs/{run_id}").json()["status"] == (
        "awaiting_review"
    )


def test_unconfigured_feishu_does_not_consume_token_or_call_network():
    adapter = EnterpriseFeishuTaskAdapter(app_id="", app_secret="")
    service, _feishu = make_service(feishu=adapter)
    run = service.create_run()
    service.import_transcript(run["id"], "transcript")
    approved = service.review(run["id"], "approve")
    try:
        service.write_feishu(run["id"], approved["review_token"])
        raised = False
    except Exception as exc:
        raised = exc
    assert getattr(raised, "code", None) == "feishu_not_configured"
    assert "FEISHU_APP_ID" in str(raised)
    assert "FEISHU_BACKEND=enterprise" in str(raised)
    assert "env.example" in str(raised)
    assert "enterprise app credentials" not in str(raised)
    assert adapter.calls == 0
    assert adapter.http_posts == 0
    snapshot = service.get_run(run["id"])
    assert snapshot["status"] == "awaiting_review"
    assert snapshot["approved"] is True


def _lark_adapter(runner):
    return LarkCliFeishuTaskAdapter(
        runner=runner,
        which=lambda _name: "/usr/bin/lark-cli",
    )


def test_missing_token_never_spawns_lark_cli():
    spawned = []

    def runner(args, **kwargs):
        del kwargs
        spawned.append(list(args))
        raise AssertionError("lark-cli must not run without a review token")

    adapter = _lark_adapter(runner)
    service, feishu = make_service(feishu=adapter)
    client = make_client(service)
    run_id = client.post("/meeting/runs").json()["id"]
    client.post(
        f"/meeting/runs/{run_id}/import/transcript",
        json={"text": "会议结论与两条待办。"},
    )
    denied = client.post(
        f"/meeting/runs/{run_id}/write_feishu_tasks",
        json={},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"]["reason"] == "missing_token"
    assert denied.json()["detail"]["feishu_called"] is False
    assert spawned == []
    assert feishu.calls == 0
    assert adapter.calls == 0


def test_unready_lark_cli_does_not_consume_token_or_create_tasks():
    state = {"user_ready": False}
    creates = []

    def runner(args, **kwargs):
        del kwargs
        if list(args)[1:3] == ["auth", "status"]:
            user = {
                "status": "ready" if state["user_ready"] else "missing",
                "available": state["user_ready"],
            }
            payload = {
                "identity": "user" if state["user_ready"] else "bot",
                "identities": {"user": user},
            }
            return subprocess.CompletedProcess(
                args, 0, stdout=json.dumps(payload), stderr=""
            )
        creates.append(list(args))
        guid = f"guid-{len(creates)}"
        body = {
            "ok": True,
            "identity": "user",
            "data": {
                "guid": guid,
                "url": (
                    "https://applink.larkoffice.com/client/todo/detail"
                    f"?guid={guid}"
                ),
            },
        }
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps(body), stderr=""
        )

    adapter = _lark_adapter(runner)
    service, _feishu = make_service(feishu=adapter)
    run = service.create_run()
    service.import_transcript(run["id"], "transcript")
    approved = service.review(run["id"], "approve")
    token = approved["review_token"]
    try:
        service.write_feishu(run["id"], token)
        raised = False
    except Exception as exc:
        raised = exc
    assert getattr(raised, "code", None) == "feishu_not_configured"
    message = str(raised)
    assert "lark-cli" in message
    assert "auth login" in message
    assert "FEISHU_APP_ID" not in message
    assert "enterprise app credentials" not in message
    assert creates == []
    assert adapter.calls == 0
    snapshot = service.get_run(run["id"])
    assert snapshot["status"] == "awaiting_review"
    assert snapshot["approved"] is True

    state["user_ready"] = True
    written = service.write_feishu(run["id"], token)
    assert written["status"] == "succeeded"
    assert adapter.calls == 1
    assert len(creates) == len(written["feishu_receipts"])
    assert all(item["ok"] for item in written["feishu_receipts"])
    for command in creates:
        assert command[1:6] == ["task", "+create", "--json", "--as", "user"]
        assert "--assignee" not in command
        assert "FEISHU_APP_SECRET" not in command
