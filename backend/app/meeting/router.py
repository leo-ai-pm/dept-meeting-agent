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

"""Meeting-domain routes. Mounted at /meeting so kernel /runs stays intact."""

from __future__ import annotations

import base64
import binascii
import threading
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.meeting.errors import HardReject, MeetingServiceError
from app.meeting.service import MeetingService, build_meeting_service

router = APIRouter(prefix="/meeting")

_service: MeetingService | None = None
_service_lock = threading.Lock()
_UI = Path(__file__).resolve().parent / "static" / "index.html"


def get_meeting_service() -> MeetingService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = build_meeting_service()
    return _service


ServiceDep = Annotated[MeetingService, Depends(get_meeting_service)]


def reset_meeting_service(
    service: MeetingService | None = None,
) -> MeetingService:
    global _service
    with _service_lock:
        _service = service or build_meeting_service()
        return _service


class CreateRunBody(BaseModel):
    title: str | None = None


class ImportTranscriptBody(BaseModel):
    text: str = Field(min_length=1)


class ImportAudioBody(BaseModel):
    filename: str = "meeting-audio.bin"
    content_base64: str | None = None


class LiveStopBody(BaseModel):
    filename: str = "live-recording.bin"
    content_base64: str | None = None


class ReviewEdits(BaseModel):
    minutes: dict[str, Any] | None = None
    todos: list[dict[str, Any]] | None = None


class ReviewBody(BaseModel):
    decision: str
    edits: ReviewEdits | None = None


class WriteFeishuBody(BaseModel):
    review_token: str | None = None


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except MeetingServiceError as exc:
        detail: dict[str, Any] = {
            "error": exc.code,
            "message": exc.message,
        }
        if isinstance(exc, HardReject):
            detail["reason"] = exc.reason
            detail["feishu_called"] = False
        raise HTTPException(
            status_code=exc.status_code, detail=detail
        ) from exc


def _decode_base64(value: str | None) -> bytes:
    if not value:
        return b""
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_base64",
                "message": "content_base64 is not valid base64",
            },
        ) from exc


@router.get("/ui", include_in_schema=False)
def meeting_ui() -> HTMLResponse:
    if not _UI.is_file():
        raise HTTPException(status_code=404, detail="meeting ui missing")
    return HTMLResponse(_UI.read_text(encoding="utf-8"))


@router.post("/runs", status_code=201)
def create_run(
    service: ServiceDep,
    body: CreateRunBody | None = None,
) -> dict[str, Any]:
    title = body.title if body else None
    return _call(service.create_run, title)


@router.post("/runs/{run_id}/import/audio")
def import_audio(
    run_id: str,
    body: ImportAudioBody,
    service: ServiceDep,
) -> dict[str, Any]:
    content = _decode_base64(body.content_base64)
    return _call(
        service.import_audio,
        run_id,
        filename=body.filename,
        content=content,
    )


@router.post("/runs/{run_id}/import/transcript")
def import_transcript(
    run_id: str,
    body: ImportTranscriptBody,
    service: ServiceDep,
) -> dict[str, Any]:
    return _call(service.import_transcript, run_id, body.text)


@router.post("/runs/{run_id}/live/start")
def live_start(
    run_id: str,
    service: ServiceDep,
) -> dict[str, Any]:
    return _call(service.live_start, run_id)


@router.post("/runs/{run_id}/live/stop")
def live_stop(
    service: ServiceDep,
    run_id: str,
    body: LiveStopBody | None = None,
) -> dict[str, Any]:
    filename = body.filename if body else "live-recording.bin"
    content = _decode_base64(body.content_base64 if body else None)
    return _call(service.live_stop, run_id, filename=filename, content=content)


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    service: ServiceDep,
) -> dict[str, Any]:
    return _call(service.get_run, run_id)


@router.get("/runs/{run_id}/replay")
def replay_run(
    run_id: str,
    service: ServiceDep,
) -> dict[str, Any]:
    return _call(service.replay, run_id)


@router.get("/runs/{run_id}/draft")
def get_draft(
    run_id: str,
    service: ServiceDep,
) -> dict[str, Any]:
    return _call(service.get_draft, run_id)


@router.post("/runs/{run_id}/review")
def review_run(
    run_id: str,
    body: ReviewBody,
    service: ServiceDep,
) -> dict[str, Any]:
    edits = body.edits.model_dump() if body.edits else None
    return _call(service.review, run_id, body.decision, edits)


@router.post("/runs/{run_id}/write_feishu_tasks")
def write_feishu_tasks(
    service: ServiceDep,
    run_id: str,
    body: WriteFeishuBody | None = None,
) -> dict[str, Any]:
    token = body.review_token if body else None
    return _call(
        service.write_feishu,
        run_id,
        token,
        tool_name="write_feishu_tasks",
    )


@router.post("/runs/{run_id}/export_feishu")
def export_feishu(
    service: ServiceDep,
    run_id: str,
    body: WriteFeishuBody | None = None,
) -> dict[str, Any]:
    token = body.review_token if body else None
    return _call(
        service.write_feishu, run_id, token, tool_name="export_feishu"
    )
