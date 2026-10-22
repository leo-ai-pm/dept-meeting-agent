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

"""Meeting run orchestration. In-memory, process-local, mock by default."""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, NoReturn

from app.meeting.adapters.feishu import (
    EnterpriseFeishuTaskAdapter,
    FeishuTaskAdapter,
    FeishuWriteRequest,
    LarkCliFeishuTaskAdapter,
    MockFeishuTaskAdapter,
    UnconfiguredFeishuTaskAdapter,
)
from app.meeting.adapters.minutes_llm import (
    VOLCANO_DEFAULT_BASE_URL,
    VOLCANO_DEFAULT_MODEL,
    MinutesLLM,
    MockMinutesLLM,
    UnconfiguredMinutesLLM,
    VolcanoMinutesLLM,
)
from app.meeting.adapters.transcriber import (
    OPENAI_ASR_MISSING_KEY,
    VOLCANO_ASR_DEFAULT_TIMEOUT,
    VOLCANO_ASR_MISSING_KEY,
    MockTranscriber,
    OpenAIWhisperTranscriber,
    Transcriber,
    UnconfiguredTranscriber,
    VolcanoDoubaoTranscriber,
)
from app.meeting.errors import HardReject, MeetingServiceError
from app.meeting.review_token import (
    ReviewTokenError,
    ReviewTokenService,
    load_review_secret,
)
from app.meeting.schema import (
    SchemaError,
    content_hash,
    normalize_minutes,
    normalize_todo,
    sha256_bytes,
    sha256_text,
)
from app.meeting.state_machine import (
    AWAITING_REVIEW,
    CREATED,
    DRAFTING,
    FAILED,
    INGESTING,
    REJECTED,
    SUCCEEDED,
    TRANSCRIBING,
    WRITING_FEISHU,
    InvalidRunTransition,
    assert_transition,
)

logger = logging.getLogger("meeting.service")


def env_flag(name: str, default: str = "1") -> bool:
    raw = os.environ.get(name, default)
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Run:
    id: str
    status: str
    created_at: str
    updated_at: str
    title: str | None = None
    live_active: bool = False
    input_kind: str | None = None
    input_hash: str | None = None
    audio_filename: str | None = None
    audio_bytes: int = 0
    transcript: str | None = None
    transcript_hash: str | None = None
    minutes: dict[str, Any] | None = None
    todos: list[dict[str, Any]] | None = None
    final_minutes: dict[str, Any] | None = None
    final_todos: list[dict[str, Any]] | None = None
    content_hash: str | None = None
    approved: bool = False
    active_jti: str | None = None
    feishu_receipts: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    seq: int = 0


class MeetingService:
    def __init__(
        self,
        *,
        transcriber: Transcriber,
        minutes_llm: MinutesLLM,
        feishu: FeishuTaskAdapter,
        tokens: ReviewTokenService,
    ) -> None:
        self.transcriber = transcriber
        self.minutes_llm = minutes_llm
        self.feishu = feishu
        self.tokens = tokens
        self._runs: dict[str, Run] = {}
        self._lock = threading.Lock()

    def create_run(self, title: str | None = None) -> dict[str, Any]:
        now = _now()
        run = Run(
            id=str(uuid.uuid4()),
            status=CREATED,
            created_at=now,
            updated_at=now,
            title=title.strip() if title else None,
        )
        with self._lock:
            self._runs[run.id] = run
            self._event(
                run,
                "run.created",
                "Run created",
                {"status": CREATED, "title": run.title},
            )
            return self._snapshot(run)

    def import_audio(
        self, run_id: str, *, filename: str, content: bytes
    ) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            self._ensure_importable(run)
            digest = sha256_bytes(content)
            self._transition(run, INGESTING)
            run.input_kind = "audio"
            run.input_hash = digest
            run.audio_filename = filename or "meeting-audio.bin"
            run.audio_bytes = len(content)
            self._event(
                run,
                "tool.import_audio",
                "Imported audio",
                {
                    "filename": run.audio_filename,
                    "sha256": digest,
                    "bytes": len(content),
                },
            )
            self._auto_pipeline(
                run, audio=content, filename=run.audio_filename, skip_asr=False
            )
            return self._snapshot(run)

    def import_transcript(self, run_id: str, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        if not cleaned:
            raise MeetingServiceError(
                "empty_transcript",
                "Transcript text is required",
                status_code=422,
            )
        with self._lock:
            run = self._require(run_id)
            self._ensure_importable(run)
            digest = sha256_text(cleaned)
            self._transition(run, INGESTING)
            run.input_kind = "transcript"
            run.input_hash = digest
            run.transcript = cleaned
            run.transcript_hash = digest
            self._event(
                run,
                "tool.import_transcript",
                "Imported transcript",
                {"sha256": digest, "chars": len(cleaned)},
            )
            self._auto_pipeline(
                run,
                audio=b"",
                filename="",
                skip_asr=True,
                transcript_text=cleaned,
            )
            return self._snapshot(run)

    def live_start(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            if run.status != CREATED or run.live_active:
                raise MeetingServiceError(
                    "conflict",
                    "Live recording can start only from created",
                    status_code=409,
                )
            run.live_active = True
            self._event(
                run,
                "tool.start_live_recording",
                "Live capture started (batch transcription after stop)",
                {},
            )
            return self._snapshot(run)

    def live_stop(
        self, run_id: str, *, filename: str, content: bytes | None
    ) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            if not run.live_active or run.status != CREATED:
                raise MeetingServiceError(
                    "conflict",
                    "Live recording is not active",
                    status_code=409,
                )
            payload = content if content else f"mock-live:{run.id}".encode()
            run.live_active = False
            self._event(
                run,
                "tool.stop_live_recording",
                "Live capture stopped; starting automatic pipeline",
                {"bytes": len(payload)},
            )
            digest = sha256_bytes(payload)
            self._transition(run, INGESTING)
            run.input_kind = "live_audio"
            run.input_hash = digest
            run.audio_filename = filename or "live-recording.bin"
            run.audio_bytes = len(payload)
            self._auto_pipeline(
                run,
                audio=payload,
                filename=run.audio_filename,
                skip_asr=False,
            )
            return self._snapshot(run)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            return self._snapshot(self._require(run_id))

    def replay(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            return {
                "run_id": run.id,
                "status": run.status,
                "events": [dict(event) for event in run.events],
            }

    def get_draft(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            if run.minutes is None or run.todos is None:
                raise MeetingServiceError(
                    "draft_not_ready",
                    "Draft is not ready",
                    status_code=404,
                )
            return {
                "run_id": run.id,
                "status": run.status,
                "content_hash": run.content_hash,
                "approved": run.approved,
                "minutes": run.minutes,
                "todos": run.todos,
            }

    def review(
        self,
        run_id: str,
        decision: str,
        edits: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject", "edit_approve"}:
            raise MeetingServiceError(
                "invalid_decision",
                "decision must be approve, reject, or edit_approve",
                status_code=422,
            )
        with self._lock:
            run = self._require(run_id)
            if run.status != AWAITING_REVIEW:
                raise MeetingServiceError(
                    "conflict",
                    f"Review requires {AWAITING_REVIEW}, got {run.status}",
                    status_code=409,
                )
            if decision == "reject":
                return self._reject(run)
            return self._approve(run, decision, edits or {})

    def write_feishu(
        self,
        run_id: str,
        review_token: str | None,
        *,
        tool_name: str = "write_feishu_tasks",
    ) -> dict[str, Any]:
        with self._lock:
            run = self._require(run_id)
            token = (review_token or "").strip()
            if not token:
                self._hard_reject(run, "missing_token", tool_name)
            current_hash = run.content_hash or ""
            try:
                record = self.tokens.validate(
                    token,
                    run_id=run.id,
                    content_hash=current_hash,
                    active_jti=run.active_jti,
                )
            except ReviewTokenError as exc:
                self._hard_reject(run, exc.reason, tool_name)
            if run.status == REJECTED:
                self._hard_reject(run, "rejected", tool_name)
            if run.status != AWAITING_REVIEW or not run.approved:
                self._hard_reject(run, "invalid_state", tool_name)
            if run.final_minutes is None or run.final_todos is None:
                self._hard_reject(run, "hash_mismatch", tool_name)
            if not self.feishu.ready():
                raise MeetingServiceError(
                    "feishu_not_configured",
                    _feishu_not_ready_message(self.feishu),
                    status_code=503,
                )
            self._transition(run, WRITING_FEISHU)
            self.tokens.consume(record.jti)
            run.active_jti = None
            summary = {
                "title": run.final_minutes["title"],
                "decisions": list(run.final_minutes["decisions"])[:5],
                "run_id": run.id,
            }
            request = FeishuWriteRequest(
                run_id=run.id,
                minutes_summary=summary,
                todos=list(run.final_todos),
            )
            self._event(
                run,
                "tool.write_feishu_tasks",
                f"Authorized {tool_name}",
                {
                    "tool": tool_name,
                    "idempotency_keys": [
                        todo["idempotency_key"] for todo in run.final_todos
                    ],
                },
            )
            try:
                receipts = self.feishu.write_tasks(request)
            except Exception as exc:
                logger.exception("Feishu adapter failed")
                self._fail(run, str(exc))
                return self._snapshot(run)
            run.feishu_receipts = receipts
            self._event(
                run,
                "tool.feishu_receipts",
                "Feishu task receipts",
                {"receipts": receipts},
            )
            if receipts and all(item.get("ok") for item in receipts):
                self._transition(run, SUCCEEDED)
            else:
                self._fail(run, "One or more Feishu tasks failed")
            return self._snapshot(run)

    def _approve(
        self, run: Run, decision: str, edits: dict[str, Any]
    ) -> dict[str, Any]:
        previous_hash = run.content_hash
        try:
            minutes_raw = run.minutes or {}
            todos_raw = run.todos or []
            if decision == "edit_approve":
                if edits.get("minutes") is not None:
                    minutes_raw = edits["minutes"]
                if edits.get("todos") is not None:
                    todos_raw = edits["todos"]
            minutes = normalize_minutes(minutes_raw)
            todos = [normalize_todo(item, run.id) for item in todos_raw]
        except SchemaError as exc:
            raise MeetingServiceError(
                "invalid_draft", str(exc), status_code=422
            ) from exc
        digest = content_hash(minutes, todos)
        run.minutes = minutes
        run.todos = todos
        run.final_minutes = minutes
        run.final_todos = todos
        run.content_hash = digest
        run.approved = True
        token, jti, exp = self.tokens.issue(run.id, digest)
        run.active_jti = jti
        event_type = (
            "review.edit_approved"
            if decision == "edit_approve"
            else "review.approved"
        )
        self._event(
            run,
            event_type,
            "Human review accepted; review token issued",
            {
                "decision": decision,
                "content_hash": digest,
                "previous_hash": previous_hash,
                "token_id": jti,
                "expires_at": exp,
            },
        )
        return {
            "run_id": run.id,
            "status": run.status,
            "decision": decision,
            "content_hash": digest,
            "review_token": token,
            "expires_at": exp,
        }

    def _reject(self, run: Run) -> dict[str, Any]:
        self.tokens.consume_run(run.id)
        run.approved = False
        run.active_jti = None
        run.final_minutes = None
        run.final_todos = None
        run.content_hash = None
        self._transition(run, REJECTED)
        self._event(
            run,
            "review.rejected",
            "Human review rejected; no Feishu write",
            {"feishu_called": False},
        )
        return {
            "run_id": run.id,
            "status": run.status,
            "decision": "reject",
            "content_hash": None,
            "review_token": None,
        }

    def _auto_pipeline(
        self,
        run: Run,
        *,
        audio: bytes,
        filename: str,
        skip_asr: bool,
        transcript_text: str | None = None,
    ) -> None:
        try:
            if skip_asr:
                text = transcript_text or ""
                run.transcript = text
                run.transcript_hash = sha256_text(text)
                self._transition(run, DRAFTING)
            else:
                self._transition(run, TRANSCRIBING)
                started = time.perf_counter()
                text = self.transcriber.transcribe(audio, filename=filename)
                duration_ms = int((time.perf_counter() - started) * 1000)
                run.transcript = text
                run.transcript_hash = sha256_text(text)
                self._event(
                    run,
                    "tool.transcribe",
                    "Transcribed audio",
                    {
                        "provider": self.transcriber.name,
                        "sha256": run.transcript_hash,
                        "duration_ms": duration_ms,
                    },
                )
                self._transition(run, DRAFTING)
            started = time.perf_counter()
            minutes = normalize_minutes(
                self.minutes_llm.draft_minutes(run.transcript or "")
            )
            draft_ms = int((time.perf_counter() - started) * 1000)
            self._event(
                run,
                "tool.draft_minutes",
                "Drafted minutes",
                {
                    "provider": self.minutes_llm.name,
                    "title": minutes["title"],
                    "duration_ms": draft_ms,
                },
            )
            started = time.perf_counter()
            raw_todos = self.minutes_llm.extract_todos(
                run.transcript or "", minutes
            )
            todos = [normalize_todo(item, run.id) for item in raw_todos]
            extract_ms = int((time.perf_counter() - started) * 1000)
            self._event(
                run,
                "tool.extract_todos",
                "Extracted todos",
                {
                    "provider": self.minutes_llm.name,
                    "count": len(todos),
                    "duration_ms": extract_ms,
                },
            )
            run.minutes = minutes
            run.todos = todos
            self._transition(run, AWAITING_REVIEW)
            self._event(
                run,
                "review.pending",
                "Automatic pipeline finished; awaiting human review",
                {"todo_count": len(todos)},
            )
        except Exception as exc:
            logger.exception("Meeting pipeline failed")
            self._fail(run, str(exc))

    def _hard_reject(self, run: Run, reason: str, tool_name: str) -> NoReturn:
        self._event(
            run,
            "permission.hard_reject",
            f"Hard reject {tool_name}: {reason}",
            {
                "tool": tool_name,
                "reason": reason,
                "feishu_called": False,
            },
        )
        raise HardReject(reason)

    def _fail(self, run: Run, message: str) -> None:
        run.error = message
        if run.status != FAILED:
            try:
                assert_transition(run.status, FAILED)
            except InvalidRunTransition:
                run.status = FAILED
                run.updated_at = _now()
            else:
                self._transition(run, FAILED)
        self._event(run, "run.failed", message, {"error": message})

    def _ensure_importable(self, run: Run) -> None:
        if run.live_active:
            raise MeetingServiceError(
                "conflict",
                "Stop live recording before importing",
                status_code=409,
            )
        if run.status not in {CREATED, FAILED}:
            raise MeetingServiceError(
                "conflict",
                f"Cannot import while status is {run.status}",
                status_code=409,
            )

    def _transition(self, run: Run, target: str) -> None:
        assert_transition(run.status, target)
        source = run.status
        run.status = target
        run.updated_at = _now()
        self._event(
            run,
            "state.changed",
            f"{source} -> {target}",
            {"from": source, "to": target},
        )

    def _event(
        self,
        run: Run,
        event_type: str,
        summary: str,
        payload: dict[str, Any],
    ) -> None:
        run.seq += 1
        run.events.append(
            {
                "seq": run.seq,
                "at": _now(),
                "type": event_type,
                "summary": summary,
                "payload": payload,
            }
        )

    def _require(self, run_id: str) -> Run:
        run = self._runs.get(run_id)
        if run is None:
            raise MeetingServiceError(
                "run_not_found", "Run not found", status_code=404
            )
        return run

    def _snapshot(self, run: Run) -> dict[str, Any]:
        draft_summary = None
        if run.minutes is not None and run.todos is not None:
            draft_summary = {
                "title": run.minutes.get("title"),
                "decision_count": len(run.minutes.get("decisions") or []),
                "todo_count": len(run.todos),
            }
        return {
            "id": run.id,
            "status": run.status,
            "title": run.title,
            "live_active": run.live_active,
            "error": run.error,
            "input": {
                "kind": run.input_kind,
                "sha256": run.input_hash,
                "filename": run.audio_filename,
                "bytes": run.audio_bytes,
            },
            "transcript_sha256": run.transcript_hash,
            "draft_summary": draft_summary,
            "content_hash": run.content_hash,
            "approved": run.approved,
            "feishu_receipts": list(run.feishu_receipts),
            "created_at": run.created_at,
            "updated_at": run.updated_at,
        }


_OPENAI_ASR_BACKENDS = frozenset({"openai", "whisper"})
_VOLCANO_ASR_BACKENDS = frozenset({"volcano"})


def _build_transcriber() -> Transcriber:
    if env_flag("MOCK_ASR", "1"):
        return MockTranscriber()
    backend = os.environ.get("ASR_BACKEND", "volcano").strip().lower()
    backend = backend or "volcano"
    if backend in _OPENAI_ASR_BACKENDS:
        return _build_openai_transcriber()
    if backend in _VOLCANO_ASR_BACKENDS:
        return _build_volcano_transcriber()
    shown = backend[:80]
    return UnconfiguredTranscriber(
        "ASR_BACKEND must be volcano (default), openai, or whisper. "
        f"Got {shown!r}. Set MOCK_ASR=1 to use the fixture."
    )


def _build_openai_transcriber() -> Transcriber:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        return UnconfiguredTranscriber(OPENAI_ASR_MISSING_KEY)
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    model = os.environ.get("MEETING_ASR_MODEL", "").strip()
    return OpenAIWhisperTranscriber(
        api_key=api_key,
        base_url=base_url or "https://api.openai.com/v1",
        model=model or "whisper-1",
    )


def _build_volcano_transcriber() -> Transcriber:
    """Select 豆包语音 flash ASR.

    ``VOLCANO_API_KEY`` is the Ark minutes key and is intentionally not
    read here. Speech ASR uses the openspeech console key.
    """

    api_key = _first_env("VOLCANO_ASR_API_KEY", "VOLCANO_OPENSPEECH_API_KEY")
    app_key = _first_env("VOLCANO_ASR_APP_KEY", "VOLCANO_ASR_APP_ID")
    access_key = _first_env(
        "VOLCANO_ASR_ACCESS_KEY", "VOLCANO_ASR_ACCESS_TOKEN"
    )
    if not api_key and not (app_key and access_key):
        return UnconfiguredTranscriber(VOLCANO_ASR_MISSING_KEY)
    return VolcanoDoubaoTranscriber(
        api_key=api_key,
        app_key=app_key,
        access_key=access_key,
        resource_id=os.environ.get("VOLCANO_ASR_RESOURCE_ID", ""),
        uid=os.environ.get("VOLCANO_ASR_UID", ""),
        timeout=_positive_float(
            os.environ.get("VOLCANO_ASR_TIMEOUT", ""),
            VOLCANO_ASR_DEFAULT_TIMEOUT,
        ),
    )


def _positive_float(raw: str, default: float) -> float:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _build_minutes_llm() -> MinutesLLM:
    if env_flag("MOCK_LLM", "1"):
        return MockMinutesLLM()
    api_key = _first_env("VOLCANO_API_KEY", "ARK_API_KEY")
    if not api_key:
        return UnconfiguredMinutesLLM()
    return VolcanoMinutesLLM(
        api_key=api_key,
        base_url=_first_env("VOLCANO_BASE_URL", "ARK_BASE_URL")
        or VOLCANO_DEFAULT_BASE_URL,
        model=os.environ.get("MEETING_LLM_MODEL", "").strip()
        or VOLCANO_DEFAULT_MODEL,
    )


def build_meeting_service() -> MeetingService:
    transcriber = _build_transcriber()
    minutes_llm = _build_minutes_llm()
    feishu = _build_feishu()
    ttl_raw = os.environ.get("MEETING_REVIEW_TOKEN_TTL_SECONDS", "3600")
    try:
        ttl = int(ttl_raw)
    except ValueError:
        ttl = 3600
    tokens = ReviewTokenService(load_review_secret(), ttl_seconds=ttl)
    return MeetingService(
        transcriber=transcriber,
        minutes_llm=minutes_llm,
        feishu=feishu,
        tokens=tokens,
    )


def _build_feishu() -> FeishuTaskAdapter:
    """Select the task backend.

    `MOCK_FEISHU=1` (the default) stays in-process. `MOCK_FEISHU=0` uses
    lark-cli and does not read `FEISHU_APP_ID` or `FEISHU_APP_SECRET`.
    `FEISHU_BACKEND=enterprise` opts into the custom-app tenant token.
    """

    if env_flag("MOCK_FEISHU", "1"):
        return MockFeishuTaskAdapter()
    backend = os.environ.get("FEISHU_BACKEND", "lark_cli").strip().lower()
    backend = backend or "lark_cli"
    if backend in {"enterprise", "feishu_enterprise"}:
        return EnterpriseFeishuTaskAdapter(
            app_id=os.environ.get("FEISHU_APP_ID", ""),
            app_secret=os.environ.get("FEISHU_APP_SECRET", ""),
            base_url=os.environ.get(
                "FEISHU_API_BASE", "https://open.feishu.cn"
            ),
        )
    if backend in {"lark_cli", "lark-cli", "cli"}:
        binary = os.environ.get("LARK_CLI_BIN", "").strip() or "lark-cli"
        return LarkCliFeishuTaskAdapter(binary=binary)
    shown = backend[:80]
    return UnconfiguredFeishuTaskAdapter(
        "FEISHU_BACKEND must be lark_cli (default) or enterprise. "
        f"Got {shown!r}."
    )


def _feishu_not_ready_message(feishu: FeishuTaskAdapter) -> str:
    fallback = "Feishu task backend is not configured"
    reporter = getattr(feishu, "not_ready_message", None)
    if not callable(reporter):
        return fallback
    reported = reporter()
    if isinstance(reported, str) and reported.strip():
        return reported.strip()
    return fallback


def _now() -> str:
    return datetime.now(UTC).isoformat()
