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

"""Todo schema, content hash, and Feishu task body. No invented open_id."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

OPEN_ID_RE = re.compile(r"^ou_[A-Za-z0-9]+$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNASSIGNED = "待确认"


class SchemaError(ValueError):
    pass


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_text(content: str) -> str:
    return sha256_bytes(content.encode())


def idempotency_key(
    run_id: str, title: str, assignee: str, due_date: str
) -> str:
    raw = f"{run_id}\n{title}\n{assignee}\n{due_date}"
    return hashlib.sha256(raw.encode()).hexdigest()


def content_hash(minutes: dict[str, Any], todos: list[dict[str, Any]]) -> str:
    payload = {"minutes": minutes, "todos": todos}
    raw = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def open_id_or_none(assignee: str | None) -> str | None:
    """Return a Feishu open_id only when the value already is one.

    Names such as 「张三」 and the marker 「待确认」 are not open_ids.
    This function never synthesizes an id.
    """

    if not assignee:
        return None
    value = assignee.strip()
    if OPEN_ID_RE.fullmatch(value):
        return value
    return None


def normalize_minutes(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    summary = str(raw.get("summary") or "").strip()
    body = str(raw.get("body") or "").strip()
    decisions = [
        str(item).strip()
        for item in (raw.get("decisions") or [])
        if str(item).strip()
    ]
    if not title or not summary:
        raise SchemaError("minutes title and summary are required")
    if not 3 <= len(decisions) <= 5:
        raise SchemaError("minutes decisions must contain 3 to 5 items")
    return {
        "title": title,
        "decisions": decisions,
        "summary": summary,
        "body": body,
    }


def normalize_todo(raw: dict[str, Any], run_id: str) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    description = str(raw.get("description") or "").strip()
    if not title or not description:
        raise SchemaError("todo title and description are required")
    assignee_raw = str(raw.get("assignee") or "").strip()
    assignee = assignee_raw or UNASSIGNED
    due_raw = raw.get("due_date")
    if due_raw in (None, "", UNASSIGNED):
        due_date = ""
    else:
        due_date = str(due_raw).strip()
        if not ISO_DATE_RE.fullmatch(due_date):
            raise SchemaError("due_date must be an ISO date (YYYY-MM-DD)")
    quote_raw = raw.get("source_quote")
    source_quote = str(quote_raw).strip() if quote_raw else None
    return {
        "title": title,
        "description": description,
        "assignee": assignee,
        "due_date": due_date or None,
        "source_quote": source_quote or None,
        "run_id": run_id,
        "idempotency_key": idempotency_key(run_id, title, assignee, due_date),
    }


def compose_task_description(
    todo: dict[str, Any], minutes_summary: dict[str, Any]
) -> str:
    decisions = list(minutes_summary.get("decisions") or [])[:5]
    lines = [
        str(todo.get("description") or ""),
        "",
        f"会议：{minutes_summary.get('title') or ''}",
        "关键决议：",
    ]
    lines.extend(f"{index}. {item}" for index, item in enumerate(decisions, 1))
    lines.append(
        f"run_id: {minutes_summary.get('run_id') or todo.get('run_id')}"
    )
    if not todo.get("due_date"):
        lines.append("截止日期：待确认")
    if open_id_or_none(todo.get("assignee")) is None:
        who = todo.get("assignee") or UNASSIGNED
        lines.append(f"负责人：{who}（未解析 open_id，不臆造）")
    if todo.get("source_quote"):
        lines.append(f"来源摘录：{todo['source_quote']}")
    return "\n".join(lines)


def build_task_body(
    todo: dict[str, Any], minutes_summary: dict[str, Any]
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "summary": todo["title"],
        "description": compose_task_description(todo, minutes_summary),
        "client_token": todo["idempotency_key"],
    }
    open_id = open_id_or_none(todo.get("assignee"))
    if open_id:
        body["members"] = [{"id": open_id, "role": "assignee", "type": "user"}]
    if todo.get("due_date"):
        body["due"] = {"date": todo["due_date"], "is_all_day": True}
    return body
