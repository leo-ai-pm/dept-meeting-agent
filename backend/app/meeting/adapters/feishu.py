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

"""Feishu task-center adapter for an enterprise custom app.

One todo becomes one task. Idempotency uses the todo idempotency_key as
Feishu client_token. Assignee strings that are not already open_ids are
left unassigned.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.meeting.schema import build_task_body

logger = logging.getLogger("meeting.feishu")

_MAX_ATTEMPTS = 3


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

    def write_tasks(self, request: FeishuWriteRequest) -> list[dict[str, Any]]:
        if not self.ready():
            raise FeishuNotConfigured(
                "FEISHU_APP_ID and FEISHU_APP_SECRET are required when "
                "MOCK_FEISHU=0"
            )
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
