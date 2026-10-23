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

"""Required and optional meeting env for the current MOCK_* flags.

``scripts/meeting-dev.sh --check`` runs this module. It prints names and
states only. Secret values are redacted. Selection matches
``_build_transcriber``, ``_build_minutes_llm``, and ``_build_feishu``.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.meeting.adapters.feishu import (
    _ENTERPRISE_NOT_CONFIGURED,
    _LARK_USER_NOT_READY,
    LarkCliFeishuTaskAdapter,
    unknown_feishu_backend_message,
)
from app.meeting.adapters.minutes_llm import (
    _MISSING_KEY as MINUTES_MISSING_KEY,
)
from app.meeting.adapters.transcriber import (
    OPENAI_ASR_MISSING_KEY,
    VOLCANO_ASR_MISSING_KEY,
    unknown_asr_backend_message,
)

ENV_EXAMPLE = "backend/app/meeting/env.example"
_ON = frozenset({"1", "true", "yes", "on"})
_OPENAI_ASR = frozenset({"openai", "whisper"})
_LARK_BACKENDS = frozenset({"lark_cli", "lark-cli", "cli"})
_ENTERPRISE_BACKENDS = frozenset({"enterprise", "feishu_enterprise"})
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD)", re.IGNORECASE)
_BLOCKING = frozenset({"missing", "not_ready"})

FeishuProbe = Callable[[Mapping[str, str]], tuple[bool, str]]


@dataclass(frozen=True)
class CheckItem:
    group: str
    requirement: str
    state: str
    name: str
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.requirement == "required" and self.state in _BLOCKING


@dataclass(frozen=True)
class CheckReport:
    items: tuple[CheckItem, ...]

    @property
    def ok(self) -> bool:
        return not any(item.blocking for item in self.items)

    def render(self) -> str:
        lines = [
            "meeting-check: required vs optional for the current MOCK_* flags",
            f"meeting-check: copy {ENV_EXAMPLE} to .env",
        ]
        header = None
        for item in self.items:
            if item.group != header:
                header = item.group
                lines.append(header)
            lines.append(
                f"  {item.requirement:<8}  {item.state:<9}  {item.name}"
            )
            if item.detail:
                lines.append(f"             {item.detail}")
        if self.ok:
            lines.append("meeting-check: ok")
        else:
            lines.append("meeting-check: failed")
            missing = [item.name for item in self.items if item.blocking]
            lines.append("missing: " + "; ".join(missing))
        return "\n".join(lines) + "\n"


def check_meeting_env(
    environ: Mapping[str, str] | None = None,
    *,
    feishu_probe: FeishuProbe | None = None,
) -> CheckReport:
    """Classify env for the flags in ``environ`` (default: ``os.environ``)."""

    env = os.environ if environ is None else environ
    probe = feishu_probe or default_lark_probe
    items: list[CheckItem] = []
    items.extend(_asr_items(env))
    items.extend(_llm_items(env))
    items.extend(_feishu_items(env, probe))
    items.extend(_store_items(env))
    return CheckReport(tuple(items))


def default_lark_probe(
    environ: Mapping[str, str],
    *,
    adapter: LarkCliFeishuTaskAdapter | None = None,
) -> tuple[bool, str]:
    """Local lark-cli readiness. Does not pass ``--verify``."""

    if adapter is None:
        binary = _value(environ, "LARK_CLI_BIN") or "lark-cli"
        adapter = LarkCliFeishuTaskAdapter(binary=binary)
    if adapter.ready():
        return True, ""
    message = adapter.not_ready_message().strip() or _LARK_USER_NOT_READY
    return False, message


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        print("usage: python -m app.meeting.envcheck", file=sys.stderr)
        return 2
    _load_runtime_env()
    report = check_meeting_env()
    sys.stdout.write(report.render())
    return 0 if report.ok else 1


def _load_runtime_env() -> None:
    """Load the API dotenv files. Process env stays authoritative."""

    from app.component.environment import (
        _load_initial_env_files,
        _resolve_initial_env_paths,
    )

    _load_initial_env_files(_resolve_initial_env_paths())


def _asr_items(environ: Mapping[str, str]) -> list[CheckItem]:
    on = _mock_on(environ, "MOCK_ASR")
    group = _group("MOCK_ASR", on)
    backend = _value(environ, "ASR_BACKEND").lower() or "volcano"
    if on:
        return _asr_optional(group, environ, backend)
    if backend in _OPENAI_ASR:
        return _asr_openai(group, environ, backend)
    if backend != "volcano":
        return [
            _item(
                group,
                "required",
                "not_ready",
                "ASR_BACKEND",
                unknown_asr_backend_message(backend),
                environ,
            )
        ]
    return _asr_volcano(group, environ)


def _asr_optional(
    group: str, environ: Mapping[str, str], backend: str
) -> list[CheckItem]:
    return [
        _optional_name(
            group,
            environ,
            "ASR_BACKEND",
            detail=(
                "volcano, openai, or whisper. Ignored while MOCK_ASR=1. "
                f"Empty means {backend}."
            ),
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "VOLCANO_ASR_API_KEY",
            detail=(
                "Speech key. Required only when MOCK_ASR=0 and "
                "ASR_BACKEND=volcano. Not the Ark key VOLCANO_API_KEY."
            ),
        ),
        _optional_name(
            group,
            environ,
            "VOLCANO_OPENSPEECH_API_KEY",
            detail="Alias of VOLCANO_ASR_API_KEY when that value is blank.",
        ),
        _optional_any(
            group,
            environ,
            ("VOLCANO_ASR_APP_KEY", "VOLCANO_ASR_APP_ID"),
            "VOLCANO_ASR_APP_KEY or VOLCANO_ASR_APP_ID",
            detail="Older console app id. Pair with the access token.",
        ),
        _optional_any(
            group,
            environ,
            ("VOLCANO_ASR_ACCESS_KEY", "VOLCANO_ASR_ACCESS_TOKEN"),
            "VOLCANO_ASR_ACCESS_KEY or VOLCANO_ASR_ACCESS_TOKEN",
            detail="Older console access token. Pair with the app id.",
        ),
        _optional_name(
            group,
            environ,
            "OPENAI_API_KEY",
            detail=(
                "Required only when MOCK_ASR=0 and ASR_BACKEND is "
                "openai or whisper."
            ),
        ),
        _optional_name(
            group,
            environ,
            "OPENAI_BASE_URL",
            detail="Whisper API root. Default https://api.openai.com/v1.",
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "MEETING_ASR_MODEL",
            detail="Whisper model. Default whisper-1.",
            default=True,
        ),
        _asr_tuning(group, environ),
    ]


def _asr_volcano(group: str, environ: Mapping[str, str]) -> list[CheckItem]:
    speech = _first(
        environ, "VOLCANO_ASR_API_KEY", "VOLCANO_OPENSPEECH_API_KEY"
    )
    app = _first(environ, "VOLCANO_ASR_APP_KEY", "VOLCANO_ASR_APP_ID")
    access = _first(
        environ, "VOLCANO_ASR_ACCESS_KEY", "VOLCANO_ASR_ACCESS_TOKEN"
    )
    if speech or (app and access):
        state = "set"
        detail = _speech_source(environ)
    else:
        state = "missing"
        detail = VOLCANO_ASR_MISSING_KEY
    name = "VOLCANO_ASR_API_KEY or VOLCANO_ASR_APP_KEY+VOLCANO_ASR_ACCESS_KEY"
    return [
        _optional_name(
            group,
            environ,
            "ASR_BACKEND",
            detail="Empty means volcano.",
            default=True,
        ),
        _item(group, "required", state, name, detail, environ),
        _optional_name(
            group,
            environ,
            "VOLCANO_ASR_RESOURCE_ID",
            detail="Default volc.seedasr.auc. Do not set the flash resource.",
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "VOLCANO_ASR_UID",
            detail="Default dept-meeting-agent. Not a secret.",
            default=True,
        ),
        _asr_tuning(group, environ),
        _optional_name(
            group,
            environ,
            "OPENAI_API_KEY",
            detail="Not used while ASR_BACKEND is volcano.",
        ),
    ]


def _asr_openai(
    group: str, environ: Mapping[str, str], backend: str
) -> list[CheckItem]:
    key = _value(environ, "OPENAI_API_KEY")
    if key:
        state = "set"
        detail = ""
    else:
        state = "missing"
        detail = OPENAI_ASR_MISSING_KEY
    return [
        _item(
            group,
            "optional",
            "set",
            "ASR_BACKEND",
            f"Using {backend}.",
            environ,
        ),
        _item(group, "required", state, "OPENAI_API_KEY", detail, environ),
        _optional_name(
            group,
            environ,
            "OPENAI_BASE_URL",
            detail="Default https://api.openai.com/v1.",
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "MEETING_ASR_MODEL",
            detail="Default whisper-1.",
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "VOLCANO_ASR_API_KEY",
            detail="Not used while ASR_BACKEND is openai or whisper.",
        ),
    ]


def _asr_tuning(group: str, environ: Mapping[str, str]) -> CheckItem:
    names = (
        "VOLCANO_ASR_HTTP_TIMEOUT",
        "VOLCANO_ASR_POLL_INTERVAL",
        "VOLCANO_ASR_POLL_MAX_INTERVAL",
        "VOLCANO_ASR_POLL_TIMEOUT",
    )
    present = any(_value(environ, name) for name in names)
    return _item(
        group,
        "optional",
        "set" if present else "default",
        ", ".join(names),
        "Defaults 60, 2, 15, and 1800 seconds. Blank or non-positive keeps them.",
        environ,
    )


def _llm_items(environ: Mapping[str, str]) -> list[CheckItem]:
    on = _mock_on(environ, "MOCK_LLM")
    group = _group("MOCK_LLM", on)
    tuning = [
        _optional_any(
            group,
            environ,
            ("VOLCANO_BASE_URL", "ARK_BASE_URL"),
            "VOLCANO_BASE_URL or ARK_BASE_URL",
            detail="Default https://ark.cn-beijing.volces.com/api/v3.",
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "MEETING_LLM_MODEL",
            detail="Default doubao-seed-2-1-turbo-260628.",
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "MEETING_LLM_TIMEOUT",
            detail="Default 300 seconds. Not the ASR poll timeout.",
            default=True,
        ),
    ]
    if on:
        return [
            _optional_name(
                group,
                environ,
                "VOLCANO_API_KEY",
                detail="Ark minutes key. Required only when MOCK_LLM=0.",
            ),
            _optional_name(
                group,
                environ,
                "ARK_API_KEY",
                detail="Alias of VOLCANO_API_KEY when that value is blank.",
            ),
            *tuning,
        ]
    key = _first(environ, "VOLCANO_API_KEY", "ARK_API_KEY")
    if key:
        state = "set"
        detail = _minutes_source(environ)
    else:
        state = "missing"
        detail = MINUTES_MISSING_KEY
    return [
        _item(
            group,
            "required",
            state,
            "VOLCANO_API_KEY or ARK_API_KEY",
            detail,
            environ,
        ),
        *tuning,
    ]


def _feishu_items(
    environ: Mapping[str, str], probe: FeishuProbe
) -> list[CheckItem]:
    on = _mock_on(environ, "MOCK_FEISHU")
    group = _group("MOCK_FEISHU", on)
    if on:
        return [
            _optional_name(
                group,
                environ,
                "FEISHU_BACKEND",
                detail="lark_cli or enterprise. Ignored while MOCK_FEISHU=1.",
                default=True,
            ),
            _optional_name(
                group,
                environ,
                "LARK_CLI_BIN",
                detail="Default lark-cli. Ignored while MOCK_FEISHU=1.",
                default=True,
            ),
            _optional_name(
                group,
                environ,
                "FEISHU_APP_ID",
                detail="Enterprise app id. Ignored while MOCK_FEISHU=1.",
            ),
            _optional_name(
                group,
                environ,
                "FEISHU_APP_SECRET",
                detail="Enterprise app secret. Ignored while MOCK_FEISHU=1.",
            ),
            _optional_name(
                group,
                environ,
                "FEISHU_API_BASE",
                detail="Default https://open.feishu.cn. Enterprise backend only.",
                default=True,
            ),
        ]
    backend = _value(environ, "FEISHU_BACKEND").lower() or "lark_cli"
    if backend in _ENTERPRISE_BACKENDS:
        return _feishu_enterprise(group, environ)
    if backend in _LARK_BACKENDS:
        return _feishu_lark(group, environ, probe)
    return [
        _item(
            group,
            "required",
            "not_ready",
            "FEISHU_BACKEND",
            unknown_feishu_backend_message(backend),
            environ,
        )
    ]


def _feishu_enterprise(
    group: str, environ: Mapping[str, str]
) -> list[CheckItem]:
    items = []
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET"):
        if _value(environ, name):
            items.append(_item(group, "required", "set", name, "", environ))
        else:
            items.append(
                _item(
                    group,
                    "required",
                    "missing",
                    name,
                    _ENTERPRISE_NOT_CONFIGURED,
                    environ,
                )
            )
    items.append(
        _optional_name(
            group,
            environ,
            "FEISHU_API_BASE",
            detail="Default https://open.feishu.cn.",
            default=True,
        )
    )
    items.append(
        _optional_name(
            group,
            environ,
            "LARK_CLI_BIN",
            detail="Ignored when FEISHU_BACKEND=enterprise.",
            default=True,
        )
    )
    return items


def _feishu_lark(
    group: str, environ: Mapping[str, str], probe: FeishuProbe
) -> list[CheckItem]:
    ready, message = probe(environ)
    if ready:
        state = "ready"
        detail = (
            "identities.user.status is ready or needs_refresh. "
            "Enterprise app credentials are not read."
        )
    else:
        state = "not_ready"
        detail = message or _LARK_USER_NOT_READY
    return [
        _item(group, "required", state, "lark-cli user", detail, environ),
        _optional_name(
            group,
            environ,
            "LARK_CLI_BIN",
            detail="Default lark-cli on PATH.",
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "FEISHU_APP_ID",
            detail="Not read on the lark-cli path.",
        ),
        _optional_name(
            group,
            environ,
            "FEISHU_APP_SECRET",
            detail="Not read on the lark-cli path.",
        ),
    ]


def _store_items(environ: Mapping[str, str]) -> list[CheckItem]:
    group = "store (optional)"
    return [
        _optional_name(
            group,
            environ,
            "MEETING_DB_PATH",
            detail=(
                "Unset uses backend/data/meeting.db beside the backend package."
            ),
            default=True,
        ),
        _optional_name(
            group,
            environ,
            "MEETING_REVIEW_TOKEN_SECRET",
            detail=(
                "Unset mints an ephemeral HMAC secret. "
                "Review tokens then fail after a restart."
            ),
        ),
        _optional_name(
            group,
            environ,
            "MEETING_REVIEW_TOKEN_TTL_SECONDS",
            detail="Default 3600. Non-integers fall back to 3600.",
            default=True,
        ),
    ]


def _speech_source(environ: Mapping[str, str]) -> str:
    if _value(environ, "VOLCANO_ASR_API_KEY"):
        return "Using VOLCANO_ASR_API_KEY."
    if _value(environ, "VOLCANO_OPENSPEECH_API_KEY"):
        return (
            "VOLCANO_ASR_API_KEY is unset. Using VOLCANO_OPENSPEECH_API_KEY."
        )
    return "Using the older console app id and access token."


def _minutes_source(environ: Mapping[str, str]) -> str:
    if _value(environ, "VOLCANO_API_KEY"):
        return "Using VOLCANO_API_KEY."
    return "VOLCANO_API_KEY is unset. Using ARK_API_KEY."


def _group(flag: str, on: bool) -> str:
    if on:
        return f"{flag}=1 (fixtures; no required keys)"
    return f"{flag}=0 (live)"


def _optional_name(
    group: str,
    environ: Mapping[str, str],
    name: str,
    *,
    detail: str,
    default: bool = False,
) -> CheckItem:
    return _optional_any(
        group,
        environ,
        (name,),
        name,
        detail=detail,
        default=default,
    )


def _optional_any(
    group: str,
    environ: Mapping[str, str],
    names: tuple[str, ...],
    label: str,
    *,
    detail: str,
    default: bool = False,
) -> CheckItem:
    present = any(_value(environ, name) for name in names)
    if present:
        state = "set"
    elif default:
        state = "default"
    else:
        state = "unset"
    return _item(group, "optional", state, label, detail, environ)


def _item(
    group: str,
    requirement: str,
    state: str,
    name: str,
    detail: str,
    environ: Mapping[str, str],
) -> CheckItem:
    return CheckItem(
        group=group,
        requirement=requirement,
        state=state,
        name=name,
        detail=_redact(detail, environ),
    )


def _redact(text: str, environ: Mapping[str, str]) -> str:
    if not text:
        return ""
    secrets: list[str] = []
    for name, raw in environ.items():
        if not _SECRET_NAME.search(str(name)):
            continue
        secret = str(raw).strip()
        if len(secret) < 6:
            continue
        secrets.append(secret)
    redacted = text
    for secret in sorted(set(secrets), key=len, reverse=True):
        redacted = redacted.replace(secret, "(redacted)")
    return redacted


def _mock_on(environ: Mapping[str, str], name: str) -> bool:
    raw = environ.get(name, "1")
    return str(raw).strip().lower() in _ON


def _value(environ: Mapping[str, str], name: str) -> str:
    return str(environ.get(name, "")).strip()


def _first(environ: Mapping[str, str], *names: str) -> str:
    for name in names:
        value = _value(environ, name)
        if value:
            return value
    return ""


if __name__ == "__main__":
    raise SystemExit(main())
