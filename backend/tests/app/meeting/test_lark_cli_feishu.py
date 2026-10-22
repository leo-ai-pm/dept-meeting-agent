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

import pytest

from app.meeting.adapters.feishu import (
    FeishuNotConfigured,
    FeishuWriteRequest,
    LarkCliFeishuTaskAdapter,
)
from app.meeting.schema import compose_task_description


def _summary():
    return {
        "title": "weekly",
        "decisions": ["one", "two", "three"],
        "run_id": "run",
    }


def _todo(**overrides):
    todo = {
        "title": "ship notes",
        "description": "write the notes",
        "assignee": "张三",
        "due_date": None,
        "source_quote": "张三 will do it",
        "run_id": "run",
        "idempotency_key": "idem-1",
    }
    todo.update(overrides)
    return todo


def _request(*todos):
    return FeishuWriteRequest(
        run_id="run",
        minutes_summary=_summary(),
        todos=list(todos),
    )


def _user_status(status="ready", *, available=True, identity=None):
    if identity is None:
        identity = "user" if available else "bot"
    return {
        "identity": identity,
        "identities": {
            "user": {"status": status, "available": available},
            "bot": {"status": "ready", "available": True},
        },
    }


def _completed(args, code, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args, code, stdout=stdout, stderr=stderr
    )


class FakeCli:
    def __init__(self, auth=None, create_error=None):
        self.auth = auth if auth is not None else _user_status()
        self.create_error = create_error
        self.calls = []
        self.kwargs = []
        self._seq = 0

    def __call__(self, args, **kwargs):
        argv = list(args)
        self.calls.append(argv)
        self.kwargs.append(kwargs)
        if argv[1:3] == ["auth", "status"]:
            return _completed(argv, 0, stdout=json.dumps(self.auth))
        self._seq += 1
        if self.create_error is not None:
            code, stdout, stderr = self.create_error
            return _completed(argv, code, stdout=stdout, stderr=stderr)
        guid = f"guid-{self._seq}"
        body = {
            "ok": True,
            "identity": "user",
            "data": {
                "guid": guid,
                "url": (
                    "https://applink.larkoffice.com/client/todo/detail"
                    f"?guid={guid}"
                ),
                "summary": "ship notes",
            },
        }
        return _completed(argv, 0, stdout=json.dumps(body))


def _adapter(fake, **kwargs):
    return LarkCliFeishuTaskAdapter(
        binary=kwargs.get("binary"),
        runner=fake,
        which=kwargs.get("which", lambda _name: "/usr/bin/lark-cli"),
        timeout=kwargs.get("timeout", 30),
    )


def _creates(fake):
    return [argv for argv in fake.calls if argv[1:3] == ["task", "+create"]]


def test_ready_requires_binary_and_user_identity():
    missing = LarkCliFeishuTaskAdapter(which=lambda _name: None)
    assert missing.ready() is False
    message = missing.not_ready_message()
    assert "lark-cli" in message
    assert "LARK_CLI_BIN" in message
    assert missing.commands == []
    with pytest.raises(FeishuNotConfigured, match="lark-cli"):
        missing.write_tasks(_request(_todo()))
    assert missing.calls == 0

    logged_out = _adapter(
        FakeCli(auth=_user_status("missing", available=False))
    )
    assert logged_out.ready() is False
    assert "auth login" in logged_out.not_ready_message()
    assert "--verify" not in logged_out.commands[0]


def test_needs_refresh_and_legacy_user_token_count_as_ready():
    refresh = _adapter(
        FakeCli(auth=_user_status("needs_refresh", available=True))
    )
    assert refresh.ready() is True

    legacy = _adapter(
        FakeCli(
            auth={
                "identity": "user",
                "tokenStatus": "valid",
                "userOpenId": "ou_self",
            }
        )
    )
    assert legacy.ready() is True

    expired = _adapter(
        FakeCli(auth={"identity": "bot", "tokenStatus": "expired"})
    )
    assert expired.ready() is False


def test_auth_status_failure_and_non_json_are_not_ready():
    def runner(args, **kwargs):
        del kwargs
        return _completed(
            args,
            1,
            stderr=json.dumps(
                {"ok": False, "error": {"message": "config missing"}}
            ),
        )

    adapter = _adapter(runner)
    assert adapter.ready() is False
    assert "config missing" in adapter.not_ready_message()

    def garbage(args, **kwargs):
        del kwargs
        return _completed(args, 0, stdout="not-json")

    broken = _adapter(garbage)
    assert broken.ready() is False
    assert "JSON" in broken.not_ready_message()


def test_explicit_binary_path_skips_path_search(tmp_path):
    binary = tmp_path / "lark-cli"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)

    def which(_name):
        raise AssertionError("PATH search is not used for an explicit path")

    fake = FakeCli()
    adapter = _adapter(fake, binary=str(binary), which=which)
    assert adapter.ready() is True
    assert adapter.commands[0][0] == str(binary)

    absent = _adapter(
        fake,
        binary=str(tmp_path / "missing-lark-cli"),
        which=which,
    )
    assert absent.ready() is False
    assert "missing-lark-cli" in absent.not_ready_message()


def test_create_maps_one_todo_and_assigns_only_open_ids(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "cli_should_not_pass")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret_should_not_pass")
    fake = FakeCli()
    adapter = _adapter(fake)
    named = _todo()
    identified = _todo(
        assignee="ou_abc123",
        due_date="2026-10-28",
        idempotency_key="idem-2",
        title="file the report",
    )
    receipts = adapter.write_tasks(_request(named, identified))
    creates = _creates(fake)
    assert len(creates) == 2
    assert receipts[0] == {
        "ok": True,
        "feishu_task_id": "guid-1",
        "idempotency_key": "idem-1",
        "url": (
            "https://applink.larkoffice.com/client/todo/detail?guid=guid-1"
        ),
    }
    assert receipts[1]["feishu_task_id"] == "guid-2"
    assert "--assignee" not in creates[0]
    named_description = compose_task_description(named, _summary())
    assert "ou_" not in named_description
    assert creates[0] == [
        "lark-cli",
        "task",
        "+create",
        "--json",
        "--as",
        "user",
        "--summary",
        "ship notes",
        "--description",
        named_description,
        "--idempotency-key",
        "idem-1",
    ]
    assert "负责人：张三" in named_description
    identified_description = compose_task_description(identified, _summary())
    assert creates[1] == [
        "lark-cli",
        "task",
        "+create",
        "--json",
        "--as",
        "user",
        "--summary",
        "file the report",
        "--description",
        identified_description,
        "--assignee",
        "ou_abc123",
        "--due",
        "2026-10-28",
        "--idempotency-key",
        "idem-2",
    ]
    assert "未解析" not in identified_description
    for options in fake.kwargs:
        assert options["check"] is False
        assert options["text"] is True
        assert options["capture_output"] is True
        assert "shell" not in options
        assert "FEISHU_APP_ID" not in options["env"]
        assert "FEISHU_APP_SECRET" not in options["env"]
        assert options["env"]["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"
        assert options["env"]["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] == "1"


def test_idempotency_key_skips_repeat_creates():
    fake = FakeCli()
    adapter = _adapter(fake)
    todo = _todo(assignee="ou_abc123")
    first = adapter.write_tasks(_request(todo, dict(todo)))
    assert first[0]["feishu_task_id"] == first[1]["feishu_task_id"]
    assert len(_creates(fake)) == 1
    again = adapter.write_tasks(_request(todo))
    assert again[0]["feishu_task_id"] == "guid-1"
    assert len(_creates(fake)) == 1
    assert adapter.calls == 2


def test_cli_error_is_a_receipt_and_is_not_cached():
    fake = FakeCli(
        create_error=(
            1,
            "",
            json.dumps(
                {
                    "ok": False,
                    "identity": "user",
                    "error": {"type": "api", "message": "forbidden"},
                }
            ),
        )
    )
    adapter = _adapter(fake)
    receipts = adapter.write_tasks(_request(_todo()))
    assert receipts[0]["ok"] is False
    assert receipts[0]["idempotency_key"] == "idem-1"
    assert "forbidden" in receipts[0]["error"]
    assert "feishu_task_id" not in receipts[0]
    adapter.write_tasks(_request(_todo()))
    assert len(_creates(fake)) == 2


def test_missing_guid_and_timeout_fail_the_todo():
    def runner(args, **kwargs):
        del kwargs
        argv = list(args)
        if argv[1:3] == ["auth", "status"]:
            return _completed(argv, 0, stdout=json.dumps(_user_status()))
        if "--summary" in argv and argv[argv.index("--summary") + 1] == "slow":
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
        return _completed(
            argv,
            0,
            stdout=json.dumps({"ok": True, "data": {"summary": "no guid"}}),
        )

    adapter = _adapter(runner)
    receipts = adapter.write_tasks(
        _request(
            _todo(title="no guid"),
            _todo(title="slow", idempotency_key="idem-slow"),
        )
    )
    assert receipts[0]["ok"] is False
    assert "guid" in receipts[0]["error"]
    assert receipts[1]["ok"] is False
    assert "timed out" in receipts[1]["error"]


def test_nested_task_guid_is_accepted():
    def runner(args, **kwargs):
        del kwargs
        argv = list(args)
        if argv[1:3] == ["auth", "status"]:
            return _completed(argv, 0, stdout=json.dumps(_user_status()))
        return _completed(
            argv,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "data": {
                        "task": {
                            "guid": "nested-guid",
                            "url": "https://example.test/tasks/nested-guid",
                        }
                    },
                }
            ),
        )

    receipts = _adapter(runner).write_tasks(_request(_todo()))
    assert receipts == [
        {
            "ok": True,
            "feishu_task_id": "nested-guid",
            "idempotency_key": "idem-1",
            "url": "https://example.test/tasks/nested-guid",
        }
    ]
