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

"""Feishu task-center adapters.

One todo becomes one task. Idempotency uses the todo idempotency_key as
the Feishu client token (`--idempotency-key` for lark-cli). Assignee
strings that are not already open_ids are left unassigned. This module
never maps a display name to an open_id.

`LarkCliFeishuTaskAdapter` is the default live backend. It shells out to
the official `lark-cli` (`@larksuite/cli`) as the logged-in user.
`EnterpriseFeishuTaskAdapter` remains an opt-in fallback that uses an
enterprise custom app's tenant token.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.meeting.schema import (
    build_task_body,
    compose_task_description,
    open_id_or_none,
)

logger = logging.getLogger("meeting.feishu")

_MAX_ATTEMPTS = 3
_DEFAULT_LARK_CLI = "lark-cli"
_DEFAULT_LARK_TIMEOUT = 30.0
_ENTERPRISE_NOT_CONFIGURED = (
    "FEISHU_APP_ID and FEISHU_APP_SECRET are required when "
    "FEISHU_BACKEND=enterprise. Copy backend/app/meeting/env.example "
    "to .env and fill both. Mock receipts are not used."
)
_LARK_MISSING_BINARY = (
    "lark-cli was not found. Install @larksuite/cli and ensure lark-cli "
    "is on PATH, or set LARK_CLI_BIN in the env file copied from "
    "backend/app/meeting/env.example. Mock receipts are not used."
)
_LARK_USER_NOT_READY = (
    "lark-cli user identity is not ready. Run `lark-cli auth login` "
    "for the task and contact domains, then check "
    "`lark-cli auth status --json` "
    "(identities.user.status should be ready). "
    "See backend/app/meeting/env.example. Mock receipts are not used."
)


def unknown_feishu_backend_message(backend: str) -> str:
    shown = (backend or "")[:80]
    return (
        "FEISHU_BACKEND must be lark_cli (default) or enterprise. "
        f"Got {shown!r}. Copy backend/app/meeting/env.example to .env. "
        "Mock receipts are not used."
    )


_USER_READY_STATUSES = {"ready", "needs_refresh"}
_LEGACY_TOKEN_OK = {"", "valid", "ready", "needs_refresh"}


class FeishuNotConfigured(RuntimeError):
    pass


@dataclass
class FeishuWriteRequest:
    run_id: str
    minutes_summary: dict[str, Any]
    todos: list[dict[str, Any]]


class FeishuTaskAdapter(Protocol):
    name: str

    def ready(self) -> bool: ...

    def write_tasks(
        self, request: FeishuWriteRequest
    ) -> list[dict[str, Any]]: ...


class MockFeishuTaskAdapter:
    """In-process receipts. Does not open a network connection."""

    name = "mock"

    def __init__(self) -> None:
        self.calls = 0
        self.last_bodies: list[dict[str, Any]] = []
        self._by_key: dict[str, dict[str, Any]] = {}

    def ready(self) -> bool:
        return True

    def write_tasks(self, request: FeishuWriteRequest) -> list[dict[str, Any]]:
        self.calls += 1
        receipts: list[dict[str, Any]] = []
        bodies: list[dict[str, Any]] = []
        for todo in request.todos:
            key = todo["idempotency_key"]
            body = build_task_body(todo, request.minutes_summary)
            bodies.append(body)
            cached = self._by_key.get(key)
            if cached is not None:
                receipts.append(dict(cached))
                continue
            receipt = {
                "ok": True,
                "feishu_task_id": f"mock_{key[:16]}",
                "idempotency_key": key,
            }
            self._by_key[key] = receipt
            receipts.append(dict(receipt))
        self.last_bodies = bodies
        return receipts


class EnterpriseFeishuTaskAdapter:
    """Creates tasks with tenant_access_token from an enterprise custom app.

    Credentials are read by the caller from the environment and passed in.
    This class does not embed secrets.
    """

    name = "feishu_enterprise"

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        client: httpx.Client | None = None,
        base_url: str = "https://open.feishu.cn",
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._app_id = app_id.strip()
        self._app_secret = app_secret.strip()
        root = base_url.rstrip("/")
        self._auth_url = (
            f"{root}/open-apis/auth/v3/tenant_access_token/internal"
        )
        self._task_url = f"{root}/open-apis/task/v2/tasks"
        self._client = client or httpx.Client(timeout=10.0)
        self._sleep = sleep or _default_sleep
        self._token: str | None = None
        self._by_key: dict[str, dict[str, Any]] = {}
        self.calls = 0
        self.http_posts = 0

    def ready(self) -> bool:
        return bool(self._app_id and self._app_secret)

    def not_ready_message(self) -> str:
        if self.ready():
            return ""
        return _ENTERPRISE_NOT_CONFIGURED

    def write_tasks(self, request: FeishuWriteRequest) -> list[dict[str, Any]]:
        if not self.ready():
            raise FeishuNotConfigured(self.not_ready_message())
        self.calls += 1
        token = self._tenant_token()
        receipts: list[dict[str, Any]] = []
        for todo in request.todos:
            key = todo["idempotency_key"]
            cached = self._by_key.get(key)
            if cached is not None:
                receipts.append(dict(cached))
                continue
            body = build_task_body(todo, request.minutes_summary)
            receipt = self._create_task(token, key, body)
            if receipt["ok"] and receipt.get("feishu_task_id"):
                self._by_key[key] = receipt
            receipts.append(receipt)
        return receipts

    def _tenant_token(self) -> str:
        if self._token:
            return self._token
        response = self._post(
            self._auth_url,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            headers={"Content-Type": "application/json"},
        )
        payload = _json_or_empty(response)
        token = payload.get("tenant_access_token")
        if (
            response.status_code >= 400
            or not token
            or payload.get("code")
            not in (
                0,
                None,
            )
        ):
            raise FeishuNotConfigured("Feishu tenant token request failed")
        self._token = str(token)
        return self._token

    def _create_task(
        self, token: str, key: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        response = self._post(
            self._task_url,
            json=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        payload = _json_or_empty(response)
        if response.status_code >= 400 or payload.get("code") not in (0, None):
            message = payload.get("msg") or response.text or "feishu error"
            return {
                "ok": False,
                "error": str(message)[:500],
                "idempotency_key": key,
            }
        task = (payload.get("data") or {}).get("task") or {}
        task_id = task.get("guid") or task.get("id")
        if not task_id:
            return {
                "ok": False,
                "error": "feishu response missing task id",
                "idempotency_key": key,
            }
        return {
            "ok": True,
            "feishu_task_id": str(task_id),
            "idempotency_key": key,
        }

    def _post(
        self, url: str, *, json: dict[str, Any], headers: dict[str, str]
    ):
        delay = 0.2
        response: httpx.Response | None = None
        for attempt in range(_MAX_ATTEMPTS):
            self.http_posts += 1
            response = self._client.post(url, json=json, headers=headers)
            if response.status_code < 500:
                return response
            logger.warning(
                "Feishu HTTP %s on attempt %s",
                response.status_code,
                attempt + 1,
            )
            if attempt < _MAX_ATTEMPTS - 1:
                self._sleep(delay)
                delay *= 2
        assert response is not None
        return response


class LarkCliFeishuTaskAdapter:
    """Creates tasks by invoking the official lark-cli as the logged-in user.

    The binary is `lark-cli` or `LARK_CLI_BIN`. Readiness is local:
    the binary must exist, and `lark-cli auth status --json` must show a
    user identity of `ready` or `needs_refresh`. `needs_refresh` is ready
    because the CLI refreshes that token on the next user call. This check
    does not pass `--verify` and does not open a network connection.

    Task writes always pass `--as user`. `--assignee` is set only when the
    todo assignee is already an `ou_…` open_id.
    """

    name = "lark_cli"

    def __init__(
        self,
        *,
        binary: str | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        which: Callable[[str], str | None] | None = None,
        timeout: float = _DEFAULT_LARK_TIMEOUT,
    ) -> None:
        chosen = (binary or "").strip() or _DEFAULT_LARK_CLI
        self._binary = chosen
        self._runner = runner or subprocess.run
        self._which = which or shutil.which
        self._timeout = (
            timeout if timeout and timeout > 0 else _DEFAULT_LARK_TIMEOUT
        )
        self._by_key: dict[str, dict[str, Any]] = {}
        self._not_ready: str | None = None
        self.calls = 0
        self.commands: list[list[str]] = []

    def ready(self) -> bool:
        self._not_ready = self._readiness_error()
        return self._not_ready is None

    def not_ready_message(self) -> str:
        if self._not_ready is None and self.ready():
            return ""
        return self._not_ready or _LARK_USER_NOT_READY

    def write_tasks(self, request: FeishuWriteRequest) -> list[dict[str, Any]]:
        if not self.ready():
            raise FeishuNotConfigured(self.not_ready_message())
        self.calls += 1
        receipts: list[dict[str, Any]] = []
        for todo in request.todos:
            key = str(todo.get("idempotency_key") or "")
            cached = self._by_key.get(key) if key else None
            if cached is not None:
                receipts.append(dict(cached))
                continue
            receipt = self._create_todo(request, todo, key)
            if receipt["ok"] and receipt.get("feishu_task_id"):
                self._by_key[key] = receipt
            receipts.append(receipt)
        return receipts

    def _readiness_error(self) -> str | None:
        if not self._binary_available():
            if self._binary == _DEFAULT_LARK_CLI:
                return _LARK_MISSING_BINARY
            return (
                f"lark-cli binary {self._binary!r} was not found. "
                "Set LARK_CLI_BIN to an installed @larksuite/cli binary. "
                "See backend/app/meeting/env.example. "
                "Mock receipts are not used."
            )
        try:
            completed = self._run([self._binary, "auth", "status", "--json"])
        except subprocess.TimeoutExpired:
            return "lark-cli auth status timed out."
        except OSError as exc:
            return f"lark-cli auth status failed: {exc}"[:500]
        if completed.returncode != 0:
            detail = _cli_error_text(completed)
            return (
                "lark-cli auth status failed. Confirm lark-cli is "
                f"installed and the user is logged in. {detail}"
            )[:500]
        payload = _parse_json_blob(_process_text(completed.stdout))
        if not payload:
            return (
                "lark-cli auth status did not return JSON. "
                "Re-run `lark-cli auth status --json`."
            )
        if _user_identity_ready(payload):
            return None
        return _user_not_ready_message(payload)

    def _binary_available(self) -> bool:
        binary = self._binary
        if os.path.isabs(binary) or os.sep in binary:
            return os.path.isfile(binary) and os.access(binary, os.X_OK)
        return self._which(binary) is not None

    def _create_todo(
        self,
        request: FeishuWriteRequest,
        todo: dict[str, Any],
        key: str,
    ) -> dict[str, Any]:
        summary = str(todo.get("title") or "").strip()
        if not summary or not key:
            return {
                "ok": False,
                "error": "todo title and idempotency_key are required",
                "idempotency_key": key,
            }
        description = compose_task_description(todo, request.minutes_summary)
        assignee = open_id_or_none(str(todo.get("assignee") or "") or None)
        due_raw = todo.get("due_date")
        due = str(due_raw).strip() if due_raw else ""
        argv = self._create_argv(
            summary=summary,
            description=description,
            assignee=assignee,
            due=due or None,
            idempotency_key=key,
        )
        try:
            completed = self._run(argv)
        except subprocess.TimeoutExpired:
            return _failure(key, "lark-cli task +create timed out")
        except OSError as exc:
            return _failure(key, f"lark-cli task +create failed: {exc}")
        return _receipt_from_create(completed, key)

    def _create_argv(
        self,
        *,
        summary: str,
        description: str,
        assignee: str | None,
        due: str | None,
        idempotency_key: str,
    ) -> list[str]:
        argv = [
            self._binary,
            "task",
            "+create",
            "--json",
            "--as",
            "user",
            "--summary",
            summary,
        ]
        if description:
            argv.extend(["--description", description])
        if assignee:
            argv.extend(["--assignee", assignee])
        if due:
            argv.extend(["--due", due])
        argv.extend(["--idempotency-key", idempotency_key])
        return argv

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(args))
        env = os.environ.copy()
        # Live writes must not depend on the enterprise app secret. Drop
        # those variables so the CLI child never receives them.
        env.pop("FEISHU_APP_ID", None)
        env.pop("FEISHU_APP_SECRET", None)
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        return self._runner(
            args,
            capture_output=True,
            text=True,
            timeout=self._timeout,
            check=False,
            env=env,
        )


class UnconfiguredFeishuTaskAdapter:
    """Selected backend name is not lark_cli or enterprise."""

    name = "unconfigured"

    def __init__(self, message: str) -> None:
        self._message = message
        self.calls = 0

    def ready(self) -> bool:
        return False

    def not_ready_message(self) -> str:
        return self._message

    def write_tasks(self, request: FeishuWriteRequest) -> list[dict[str, Any]]:
        del request
        raise FeishuNotConfigured(self._message)


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _default_sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _process_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_json_blob(text: str) -> dict[str, Any]:
    raw = text.strip()
    if not raw:
        return {}
    payload = _loads_object(raw)
    if payload is not None:
        return payload
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    loaded = _loads_object(raw[start : end + 1])
    return loaded or {}


def _loads_object(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _user_identity_ready(payload: dict[str, Any]) -> bool:
    identities = payload.get("identities")
    if isinstance(identities, dict) and "user" in identities:
        user = identities.get("user")
        if not isinstance(user, dict):
            return False
        status = str(user.get("status") or "").strip().lower()
        if status in _USER_READY_STATUSES:
            return True
        return user.get("available") is True
    if str(payload.get("identity") or "").strip().lower() != "user":
        return False
    token_status = str(payload.get("tokenStatus") or "").strip().lower()
    return token_status in _LEGACY_TOKEN_OK


def _user_not_ready_message(payload: dict[str, Any]) -> str:
    extra = str(payload.get("note") or "").strip()
    identities = payload.get("identities")
    if not extra and isinstance(identities, dict):
        user = identities.get("user")
        if isinstance(user, dict):
            extra = str(user.get("message") or "").strip()
    if extra:
        return f"{_LARK_USER_NOT_READY} {extra}"[:500]
    return _LARK_USER_NOT_READY


def _cli_error_text(completed: subprocess.CompletedProcess[str]) -> str:
    for blob in (
        _process_text(completed.stderr),
        _process_text(completed.stdout),
    ):
        payload = _parse_json_blob(blob)
        if not payload:
            continue
        error = payload.get("error")
        if isinstance(error, dict):
            message = (
                error.get("message") or error.get("hint") or error.get("type")
            )
            if message:
                return str(message)
        if payload.get("ok") is False and payload.get("msg"):
            return str(payload["msg"])
    text = (
        _process_text(completed.stderr) or _process_text(completed.stdout)
    ).strip()
    if text:
        return text[:500]
    return f"lark-cli exited {completed.returncode}"


def _task_guid(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    guid = data.get("guid") or data.get("id")
    if guid:
        return str(guid)
    task = data.get("task")
    if isinstance(task, dict):
        nested = task.get("guid") or task.get("id")
        if nested:
            return str(nested)
    return ""


def _task_url(payload: dict[str, Any]) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        return ""
    url = data.get("url")
    if url:
        return str(url)
    task = data.get("task")
    if isinstance(task, dict) and task.get("url"):
        return str(task["url"])
    return ""


def _receipt_from_create(
    completed: subprocess.CompletedProcess[str], key: str
) -> dict[str, Any]:
    payload = _parse_json_blob(_process_text(completed.stdout))
    if not payload:
        payload = _parse_json_blob(_process_text(completed.stderr))
    guid = _task_guid(payload)
    succeeded = payload.get("ok") is True and completed.returncode == 0
    if not succeeded:
        logger.warning(
            "lark-cli task +create failed for key %s: %s",
            key[:12],
            _cli_error_text(completed)[:200],
        )
        return _failure(key, _cli_error_text(completed))
    if not guid:
        return _failure(key, "lark-cli response missing task guid")
    receipt: dict[str, Any] = {
        "ok": True,
        "feishu_task_id": guid,
        "idempotency_key": key,
    }
    url = _task_url(payload)
    if url:
        receipt["url"] = url
    return receipt


def _failure(key: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": str(message)[:500],
        "idempotency_key": key,
    }
