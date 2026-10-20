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

"""Locked run states from PRD v0.1 §11."""

from __future__ import annotations

CREATED = "created"
INGESTING = "ingesting"
TRANSCRIBING = "transcribing"
DRAFTING = "drafting"
AWAITING_REVIEW = "awaiting_review"
WRITING_FEISHU = "writing_feishu"
SUCCEEDED = "succeeded"
FAILED = "failed"
REJECTED = "rejected"

ALL_STATES = frozenset(
    {
        CREATED,
        INGESTING,
        TRANSCRIBING,
        DRAFTING,
        AWAITING_REVIEW,
        WRITING_FEISHU,
        SUCCEEDED,
        FAILED,
        REJECTED,
    }
)

# Import of a transcript skips transcribing (ingesting → drafting).
# failed may retry the pipeline (→ ingesting) or a Feishu write.
# rejected may return to drafting or awaiting_review after a revision.
ALLOWED: dict[str, frozenset[str]] = {
    CREATED: frozenset({INGESTING, FAILED}),
    INGESTING: frozenset({TRANSCRIBING, DRAFTING, FAILED}),
    TRANSCRIBING: frozenset({DRAFTING, FAILED}),
    DRAFTING: frozenset({AWAITING_REVIEW, FAILED}),
    AWAITING_REVIEW: frozenset({WRITING_FEISHU, REJECTED, FAILED}),
    WRITING_FEISHU: frozenset({SUCCEEDED, FAILED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset({INGESTING, WRITING_FEISHU}),
    REJECTED: frozenset({DRAFTING, AWAITING_REVIEW}),
}


class InvalidRunTransition(Exception):
    def __init__(self, source: str, target: str) -> None:
        self.source = source
        self.target = target
        super().__init__(f"illegal transition {source} -> {target}")


def assert_transition(source: str, target: str) -> None:
    if source not in ALL_STATES or target not in ALL_STATES:
        raise InvalidRunTransition(source, target)
    if target not in ALLOWED[source]:
        raise InvalidRunTransition(source, target)
