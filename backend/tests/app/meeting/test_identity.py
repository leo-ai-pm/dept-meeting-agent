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

"""Default single-user identity seam.

v1 always stores ``local``. A replacement provider is exercised only
to show that ``X-Meeting-User`` reaches ``IdentityProvider.current``.
"""

import json
import sqlite3

from app.meeting.adapters.feishu import MockFeishuTaskAdapter
from app.meeting.identity import (
    LOCAL_USER_ID,
    MEETING_USER_ENV,
    MEETING_USER_HEADER,
    SingleUserIdentity,
    UserContext,
    build_identity,
    ignore_user_scope,
)
from app.meeting.review_token import ReviewTokenService
from app.meeting.service import build_meeting_service
from app.meeting.store import InMemoryRunStore, Run, SqliteRunStore
from tests.app.meeting.conftest import make_client, make_service


def test_seam_is_importable_and_defaults_to_local(monkeypatch):
    monkeypatch.setenv(MEETING_USER_ENV, "alice")
    provider = build_identity()
    assert isinstance(provider, SingleUserIdentity)
    assert provider.name == "single_user"
    assert provider.current() == UserContext(user_id=LOCAL_USER_ID)
    assert provider.current(presented="bob") == UserContext(
        user_id=LOCAL_USER_ID
    )
    ignore_user_scope(None)
    ignore_user_scope("alice")
    assert MEETING_USER_HEADER == "X-Meeting-User"
    assert LOCAL_USER_ID == "local"


def test_factory_uses_single_user_identity(monkeypatch):
    monkeypatch.setenv(MEETING_USER_ENV, "alice")
    monkeypatch.setenv("MOCK_ASR", "1")
    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("MOCK_FEISHU", "1")
    service = build_meeting_service()
    try:
        assert isinstance(service.identity, SingleUserIdentity)
        created = service.create_run(title="weekly")
        assert created["user_id"] == LOCAL_USER_ID
    finally:
        service.store.close()


def test_http_header_and_env_stay_on_the_local_user(monkeypatch):
    monkeypatch.setenv(MEETING_USER_ENV, "alice")
    service, _adapter = make_service()
    client = make_client(service)
    created = client.post(
        "/meeting/runs",
        json={"title": "weekly"},
        headers={MEETING_USER_HEADER: "bob"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["user_id"] == LOCAL_USER_ID
    replay = client.get(
        f"/meeting/runs/{body['id']}/replay",
        headers={MEETING_USER_HEADER: "carol"},
    )
    assert replay.status_code == 200
    event = replay.json()["events"][0]
    assert event["type"] == "run.created"
    assert event["payload"]["user_id"] == LOCAL_USER_ID
    empty = client.post(
        "/meeting/runs",
        json={},
        headers={MEETING_USER_HEADER: ""},
    )
    assert empty.status_code == 201
    assert empty.json()["user_id"] == LOCAL_USER_ID


def test_replacement_provider_receives_the_reserved_header():
    """The header is delivered. Only a replacement provider may use it."""

    class RecordingProvider:
        name = "recording"

        def __init__(self) -> None:
            self.presented: list[str | None] = []

        def current(self, presented: str | None = None) -> UserContext:
            self.presented.append(presented)
            if presented and presented.strip():
                return UserContext(user_id=presented.strip())
            return UserContext(user_id=LOCAL_USER_ID)

    service, _adapter = make_service()
    provider = RecordingProvider()
    service.identity = provider
    client = make_client(service)
    created = client.post(
        "/meeting/runs",
        json={},
        headers={MEETING_USER_HEADER: "bob"},
    )
    assert created.status_code == 201
    assert created.json()["user_id"] == "bob"
    assert "bob" in provider.presented


def test_store_accepts_user_id_and_does_not_filter():
    run = Run(
        id="run-local",
        status="created",
        created_at="2026-10-23T00:00:00+00:00",
        updated_at="2026-10-23T00:00:00+00:00",
        user_id=LOCAL_USER_ID,
    )
    store = InMemoryRunStore()
    store.put(run, user_id=LOCAL_USER_ID)
    loaded = store.get("run-local", user_id="someone-else")
    assert loaded is not None
    assert loaded.user_id == LOCAL_USER_ID
    assert [item.id for item in store.list(user_id="someone-else")] == [
        "run-local"
    ]


def test_legacy_snapshot_without_user_id_loads_as_local(tmp_path):
    path = tmp_path / "legacy.db"
    store = SqliteRunStore(path)
    try:
        store.put(
            Run(
                id="old",
                status="created",
                created_at="2026-10-23T00:00:00+00:00",
                updated_at="2026-10-23T00:00:00+00:00",
            )
        )
    finally:
        store.close()

    raw = sqlite3.connect(path)
    try:
        blob = raw.execute(
            "SELECT snapshot_json FROM runs WHERE id = ?",
            ("old",),
        ).fetchone()[0]
        payload = json.loads(blob)
        payload.pop("user_id", None)
        raw.execute(
            "UPDATE runs SET snapshot_json = ? WHERE id = ?",
            (json.dumps(payload), "old"),
        )
        raw.commit()
    finally:
        raw.close()

    reopened = SqliteRunStore(path)
    try:
        loaded = reopened.get("old", user_id="someone-else")
        assert loaded is not None
        assert loaded.user_id == LOCAL_USER_ID
        assert [item.id for item in reopened.list(user_id="other")] == ["old"]
    finally:
        reopened.close()


def test_review_token_omits_user_id_and_feishu_request_carries_local():
    tokens = ReviewTokenService(b"test-secret", clock=lambda: 1_000.0)
    token, jti, _exp = tokens.issue("run-1", "hash-a", user_id="not-in-token")
    assert "not-in-token" not in token
    record = tokens.validate(
        token,
        run_id="run-1",
        content_hash="hash-a",
        active_jti=jti,
        user_id="someone-else",
    )
    assert record.run_id == "run-1"
    assert "local" not in token

    class Capture(MockFeishuTaskAdapter):
        def write_tasks(self, request):
            self.seen = request
            return super().write_tasks(request)

    adapter = Capture()
    service, _adapter = make_service(feishu=adapter)
    created = service.create_run(title="weekly")
    service.import_transcript(created["id"], "transcript")
    approved = service.review(created["id"], "approve")
    assert LOCAL_USER_ID not in approved["review_token"]
    written = service.write_feishu(created["id"], approved["review_token"])
    assert written["status"] == "succeeded"
    assert adapter.seen.user_id == LOCAL_USER_ID
