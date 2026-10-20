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

from pathlib import Path
from typing import Protocol

_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "transcript.txt"
)


class ProviderNotConfigured(RuntimeError):
    pass


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: bytes, *, filename: str) -> str: ...


class MockTranscriber:
    """Default ASR. Returns a fixed transcript fixture. Ignores audio bytes."""

    name = "mock"

    def __init__(self, fixture: Path | None = None) -> None:
        self._fixture = fixture or _FIXTURE

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        del audio, filename
        return self._fixture.read_text(encoding="utf-8").strip()


class UnconfiguredTranscriber:
    name = "unconfigured"

    def transcribe(self, audio: bytes, *, filename: str) -> str:
        del audio, filename
        raise ProviderNotConfigured(
            "ASR provider is not configured. Set MOCK_ASR=1. "
            "A real vendor is pending confirmation."
        )
