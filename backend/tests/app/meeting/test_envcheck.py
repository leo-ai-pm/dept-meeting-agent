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
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.meeting.adapters.feishu import LarkCliFeishuTaskAdapter
from app.meeting.envcheck import (
    check_meeting_env,
    default_lark_probe,
    main,
)
from app.meeting.errors import MeetingServiceError
from app.meeting.service import build_meeting_service

_BACKEND = Path(__file__).resolve().parents[3]
_REPO = _BACKEND.parent
_SCRIPT = _REPO / "scripts" / "meeting-dev.sh"
_SECRET = "vk-live-secret-value"
_FIXTURE_MARK = "增长组周会"


def _ready(_environ):
    return True, ""


def _boom(_environ):
    raise AssertionError("lark-cli probe must not run")


def _names(report, requirement=None):
    items = report.items
    if requirement is not None:
        items = [item for item in items if item.requirement == requirement]
    return [item.name for item in items]


def _find(report, name):
    matches = [item for item in report.items if item.name == name]
    assert matches, name
    return matches[0]


def test_default_mocks_list_optional_keys_and_pass():
    report = check_meeting_env({}, feishu_probe=_boom)
    text = report.render()
    assert report.ok is True
    assert "meeting-check: ok" in text
    assert "no required keys" in text
    assert "optional" in text
    assert "required" in text
    assert "VOLCANO_API_KEY" in text
    assert "VOLCANO_ASR_API_KEY" in text
    assert "OPENAI_API_KEY" in text
    assert "FEISHU_APP_ID" in text
    assert "lark-cli user" not in _names(report, "required")
    assert "env.example" in text
    assert _FIXTURE_MARK not in text
    assert not any(item.blocking for item in report.items)


def test_mock_llm_off_without_ark_key_names_env_example_and_hides_secrets():
    report = check_meeting_env(
        {
            "MOCK_ASR": "1",
            "MOCK_LLM": "0",
            "MOCK_FEISHU": "1",
            "VOLCANO_API_KEY": "   ",
            "ARK_API_KEY": "",
            "FEISHU_APP_SECRET": _SECRET,
        },
        feishu_probe=_boom,
    )
    text = report.render()
    assert report.ok is False
    item = _find(report, "VOLCANO_API_KEY or ARK_API_KEY")
    assert item.requirement == "required"
    assert item.state == "missing"
    assert "VOLCANO_API_KEY" in item.detail
    assert "ARK_API_KEY" in item.detail
    assert "env.example" in item.detail
    assert "Fixture minutes are not used" in item.detail
    assert "meeting-check: failed" in text
    assert _SECRET not in text
    assert _FIXTURE_MARK not in text


def test_ark_alias_satisfies_live_minutes_without_printing_the_key():
    report = check_meeting_env(
        {
            "MOCK_LLM": "0",
            "MOCK_ASR": "1",
            "MOCK_FEISHU": "1",
            "VOLCANO_API_KEY": "  ",
            "ARK_API_KEY": _SECRET,
        },
        feishu_probe=_boom,
    )
    text = report.render()
    item = _find(report, "VOLCANO_API_KEY or ARK_API_KEY")
    assert report.ok is True
    assert item.state == "set"
    assert "ARK_API_KEY" in item.detail
    assert _SECRET not in text


def test_blank_mock_flag_is_live_like_env_flag():
    report = check_meeting_env(
        {"MOCK_LLM": "", "MOCK_ASR": "1", "MOCK_FEISHU": "yes"},
        feishu_probe=_boom,
    )
    assert report.ok is False
    assert _find(report, "VOLCANO_API_KEY or ARK_API_KEY").state == "missing"
    assert any(item.group.startswith("MOCK_FEISHU=1") for item in report.items)


def test_live_volcano_asr_rejects_the_ark_minutes_key():
    report = check_meeting_env(
        {
            "MOCK_ASR": "0",
            "MOCK_LLM": "1",
            "MOCK_FEISHU": "1",
            "VOLCANO_API_KEY": _SECRET,
            "ARK_API_KEY": "ark-alias-secret",
            "OPENAI_API_KEY": "sk-test-whisper",
        },
        feishu_probe=_boom,
    )
    text = report.render()
    assert report.ok is False
    missing = [item for item in report.items if item.blocking]
    assert len(missing) == 1
    assert "VOLCANO_ASR_API_KEY" in missing[0].name
    assert "env.example" in missing[0].detail
    assert "OPENAI_API_KEY" not in missing[0].detail
    assert _SECRET not in text
    assert "ark-alias-secret" not in text
    assert "sk-test-whisper" not in text


def test_speech_aliases_and_partial_console_pair():
    alias = check_meeting_env(
        {
            "MOCK_ASR": "0",
            "VOLCANO_ASR_API_KEY": " ",
            "VOLCANO_OPENSPEECH_API_KEY": "openspeech-key",
        },
        feishu_probe=_boom,
    )
    assert alias.ok is True
    assert (
        "VOLCANO_OPENSPEECH_API_KEY"
        in _find(
            alias,
            "VOLCANO_ASR_API_KEY or VOLCANO_ASR_APP_KEY+VOLCANO_ASR_ACCESS_KEY",
        ).detail
    )

    pair = check_meeting_env(
        {
            "MOCK_ASR": "0",
            "VOLCANO_ASR_APP_ID": "app-id",
            "VOLCANO_ASR_ACCESS_TOKEN": "access-token",
        },
        feishu_probe=_boom,
    )
    assert pair.ok is True
    assert "access-token" not in pair.render()
    assert "app-id" not in pair.render()

    partial = check_meeting_env(
        {"MOCK_ASR": "0", "VOLCANO_ASR_APP_KEY": "app-id"},
        feishu_probe=_boom,
    )
    assert partial.ok is False
    assert "VOLCANO_ASR_ACCESS_KEY" in partial.render()


def test_whisper_backend_requires_openai_key_only():
    missing = check_meeting_env(
        {
            "MOCK_ASR": "0",
            "ASR_BACKEND": "whisper",
            "VOLCANO_ASR_API_KEY": "speech-key-not-used",
        },
        feishu_probe=_boom,
    )
    assert missing.ok is False
    assert _find(missing, "OPENAI_API_KEY").state == "missing"
    assert "env.example" in _find(missing, "OPENAI_API_KEY").detail
    assert "speech-key-not-used" not in missing.render()

    ready = check_meeting_env(
        {
            "MOCK_ASR": "0",
            "ASR_BACKEND": "openai",
            "OPENAI_API_KEY": "sk-test",
        },
        feishu_probe=_boom,
    )
    assert ready.ok is True
    assert "sk-test" not in ready.render()


def test_unknown_asr_backend_is_not_ready_even_with_keys():
    report = check_meeting_env(
        {
            "MOCK_ASR": "0",
            "ASR_BACKEND": "siliconflow",
            "VOLCANO_ASR_API_KEY": "speech-key",
            "OPENAI_API_KEY": "sk-test",
        },
        feishu_probe=_boom,
    )
    assert report.ok is False
    item = _find(report, "ASR_BACKEND")
    assert item.state == "not_ready"
    assert "siliconflow" in item.detail
    assert "env.example" in item.detail
    assert "speech-key" not in report.render()


def test_live_lark_uses_probe_and_redacts_secrets():
    def probe(_environ):
        return False, f"lark-cli auth status failed {_SECRET}"

    report = check_meeting_env(
        {
            "MOCK_ASR": "1",
            "MOCK_LLM": "1",
            "MOCK_FEISHU": "0",
            "VOLCANO_API_KEY": _SECRET,
        },
        feishu_probe=probe,
    )
    text = report.render()
    item = _find(report, "lark-cli user")
    assert report.ok is False
    assert item.state == "not_ready"
    assert "(redacted)" in item.detail
    assert _SECRET not in text
    assert "FEISHU_APP_ID" in _names(report, "optional")


def test_ready_lark_user_does_not_require_enterprise_creds():
    report = check_meeting_env(
        {"MOCK_FEISHU": "0", "FEISHU_BACKEND": "Lark-CLI"},
        feishu_probe=_ready,
    )
    assert report.ok is True
    item = _find(report, "lark-cli user")
    assert item.state == "ready"
    assert _find(report, "FEISHU_APP_ID").requirement == "optional"


def test_enterprise_feishu_requires_both_creds_and_skips_lark_probe():
    missing = check_meeting_env(
        {
            "MOCK_FEISHU": "0",
            "FEISHU_BACKEND": "enterprise",
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": " ",
        },
        feishu_probe=_boom,
    )
    text = missing.render()
    assert missing.ok is False
    assert _find(missing, "FEISHU_APP_ID").state == "set"
    secret = _find(missing, "FEISHU_APP_SECRET")
    assert secret.state == "missing"
    assert "env.example" in secret.detail
    assert "lark-cli" not in secret.detail
    assert "cli_test" not in text

    ready = check_meeting_env(
        {
            "MOCK_FEISHU": "0",
            "FEISHU_BACKEND": "enterprise",
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": _SECRET,
        },
        feishu_probe=_boom,
    )
    assert ready.ok is True
    assert _SECRET not in ready.render()
    assert "cli_test" not in ready.render()


def test_unknown_feishu_backend_is_not_ready():
    report = check_meeting_env(
        {"MOCK_FEISHU": "0", "FEISHU_BACKEND": "tenant-token"},
        feishu_probe=_boom,
    )
    item = _find(report, "FEISHU_BACKEND")
    assert report.ok is False
    assert item.state == "not_ready"
    assert "lark_cli" in item.detail
    assert "enterprise" in item.detail
    assert "env.example" in item.detail


def test_default_lark_probe_missing_binary_points_at_env_example():
    adapter = LarkCliFeishuTaskAdapter(which=lambda _name: None)
    ready, message = default_lark_probe({}, adapter=adapter)
    assert ready is False
    assert "lark-cli" in message
    assert "env.example" in message
    assert adapter.commands == []


def test_default_lark_probe_ready_user_does_not_verify():
    def runner(args, **kwargs):
        del kwargs
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps(
                {"identities": {"user": {"status": "needs_refresh"}}}
            ),
            stderr="",
        )

    adapter = LarkCliFeishuTaskAdapter(
        runner=runner,
        which=lambda _name: "/usr/bin/lark-cli",
    )
    ready, message = default_lark_probe({}, adapter=adapter)
    assert ready is True
    assert message == ""
    assert adapter.commands == [["lark-cli", "auth", "status", "--json"]]


def test_check_agrees_with_minutes_and_asr_factories(monkeypatch):
    monkeypatch.setenv("MOCK_ASR", "1")
    monkeypatch.setenv("MOCK_FEISHU", "1")
    monkeypatch.setenv("MOCK_LLM", "0")
    monkeypatch.delenv("VOLCANO_API_KEY", raising=False)
    monkeypatch.delenv("ARK_API_KEY", raising=False)
    report = check_meeting_env(feishu_probe=_boom)
    service = build_meeting_service()
    assert service.minutes_llm.name == "unconfigured"
    assert report.ok is False

    monkeypatch.setenv("ARK_API_KEY", "ark-key")
    report = check_meeting_env(feishu_probe=_boom)
    assert build_meeting_service().minutes_llm.name == "volcano_ark"
    assert report.ok is True
    assert "ark-key" not in report.render()

    monkeypatch.setenv("MOCK_LLM", "1")
    monkeypatch.setenv("MOCK_ASR", "0")
    monkeypatch.delenv("ASR_BACKEND", raising=False)
    for name in (
        "VOLCANO_ASR_API_KEY",
        "VOLCANO_OPENSPEECH_API_KEY",
        "VOLCANO_ASR_APP_KEY",
        "VOLCANO_ASR_APP_ID",
        "VOLCANO_ASR_ACCESS_KEY",
        "VOLCANO_ASR_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    report = check_meeting_env(feishu_probe=_boom)
    service = build_meeting_service()
    assert service.transcriber.name == "unconfigured"
    assert report.ok is False
    run = service.create_run()
    result = service.import_audio(
        run["id"], filename="clip.wav", content=b"abc"
    )
    error = result["error"] or ""
    assert result["status"] == "failed"
    assert result["transcript_sha256"] is None
    assert "VOLCANO_ASR_API_KEY" in error
    assert "env.example" in error
    assert "fixtures/transcript.txt is not used" in error
    with pytest.raises(MeetingServiceError):
        service.get_draft(run["id"])


def test_module_cli_fails_closed_without_printing_repo_secrets(tmp_path):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "MOCK_ASR": "1",
        "MOCK_LLM": "0",
        "MOCK_FEISHU": "1",
    }
    failed = subprocess.run(
        [sys.executable, "-m", "app.meeting.envcheck"],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode == 1
    assert "VOLCANO_API_KEY" in failed.stdout
    assert "env.example" in failed.stdout
    assert "meeting-check: failed" in failed.stdout
    assert "dummy_secret_server_key" not in failed.stdout
    assert "dummy_secret_server_key" not in failed.stderr

    env["MOCK_LLM"] = "1"
    passed = subprocess.run(
        [sys.executable, "-m", "app.meeting.envcheck"],
        cwd=_BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert passed.returncode == 0
    assert "meeting-check: ok" in passed.stdout
    assert "no required keys" in passed.stdout
    assert "optional" in passed.stdout

    usage = main(["--nope"])
    assert usage == 2


def test_meeting_dev_help_and_rejected_args():
    syntax = subprocess.run(
        ["bash", "-n", str(_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0
    help_run = subprocess.run(
        [str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_run.returncode == 0
    assert "--check" in help_run.stdout
    rejected = subprocess.run(
        [str(_SCRIPT), "--nope"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rejected.returncode == 2
    extra = subprocess.run(
        [str(_SCRIPT), "--check", "extra"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert extra.returncode == 2
