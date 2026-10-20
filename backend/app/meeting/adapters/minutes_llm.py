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

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from app.meeting.adapters.transcriber import ProviderNotConfigured

_ROOT = Path(__file__).resolve().parent.parent / "fixtures"
_MINUTES = _ROOT / "minutes.json"
_TODOS = _ROOT / "todos.json"


class MinutesLLM(Protocol):
    name: str

    def draft_minutes(self, transcript: str) -> dict[str, Any]: ...

    def extract_todos(
        self, transcript: str, minutes: dict[str, Any]
    ) -> list[dict[str, Any]]: ...


class MockMinutesLLM:
    """Default minutes model. Returns fixed minutes and todo fixtures."""

    name = "mock"

    def __init__(
        self,
        minutes_path: Path | None = None,
        todos_path: Path | None = None,
    ) -> None:
        self._minutes_path = minutes_path or _MINUTES
        self._todos_path = todos_path or _TODOS

    def draft_minutes(self, transcript: str) -> dict[str, Any]:
        del transcript
        return json.loads(self._minutes_path.read_text(encoding="utf-8"))

    def extract_todos(
        self, transcript: str, minutes: dict[str, Any]
    ) -> list[dict[str, Any]]:
        del transcript, minutes
        return json.loads(self._todos_path.read_text(encoding="utf-8"))


class UnconfiguredMinutesLLM:
    name = "unconfigured"

    def draft_minutes(self, transcript: str) -> dict[str, Any]:
        del transcript
        raise ProviderNotConfigured(
            "Minutes LLM is not configured. Set MOCK_LLM=1. "
            "A real vendor is pending confirmation."
        )

    def extract_todos(
        self, transcript: str, minutes: dict[str, Any]
    ) -> list[dict[str, Any]]:
        del transcript, minutes
        raise ProviderNotConfigured(
            "Minutes LLM is not configured. Set MOCK_LLM=1. "
            "A real vendor is pending confirmation."
        )
