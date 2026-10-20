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

import pytest

from app.meeting.state_machine import (
    ALL_STATES,
    ALLOWED,
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


def test_locked_state_names():
    assert ALL_STATES == {
        "created",
        "ingesting",
        "transcribing",
        "drafting",
        "awaiting_review",
        "writing_feishu",
        "succeeded",
        "failed",
        "rejected",
    }


def test_happy_path_and_transcript_skip():
    assert_transition(CREATED, INGESTING)
    assert_transition(INGESTING, TRANSCRIBING)
    assert_transition(TRANSCRIBING, DRAFTING)
    assert_transition(DRAFTING, AWAITING_REVIEW)
    assert_transition(AWAITING_REVIEW, WRITING_FEISHU)
    assert_transition(WRITING_FEISHU, SUCCEEDED)
    assert_transition(INGESTING, DRAFTING)


def test_failure_and_reject_edges():
    assert_transition(AWAITING_REVIEW, REJECTED)
    assert_transition(REJECTED, DRAFTING)
    assert_transition(REJECTED, AWAITING_REVIEW)
    assert_transition(TRANSCRIBING, FAILED)
    assert_transition(FAILED, INGESTING)
    assert_transition(FAILED, WRITING_FEISHU)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (CREATED, AWAITING_REVIEW),
        (CREATED, SUCCEEDED),
        (SUCCEEDED, WRITING_FEISHU),
        (REJECTED, WRITING_FEISHU),
        (AWAITING_REVIEW, SUCCEEDED),
        (DRAFTING, WRITING_FEISHU),
    ],
)
def test_illegal_transitions(source, target):
    with pytest.raises(InvalidRunTransition):
        assert_transition(source, target)


def test_every_state_has_an_allow_list():
    assert set(ALLOWED) == set(ALL_STATES)
