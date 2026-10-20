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

from app.meeting.review_token import ReviewTokenError, ReviewTokenService


def _clock_at(box):
    return lambda: box["t"]


def test_token_binds_run_hash_and_expiry():
    box = {"t": 1_000}
    tokens = ReviewTokenService(
        b"test-secret", ttl_seconds=30, clock=_clock_at(box)
    )
    token, jti, exp = tokens.issue("run-1", "hash-a")
    assert exp == 1_030
    assert jti in token
    record = tokens.validate(
        token, run_id="run-1", content_hash="hash-a", active_jti=jti
    )
    assert record.content_hash == "hash-a"
    assert record.run_id == "run-1"


def test_hash_mismatch_consumed_and_expired():
    box = {"t": 1_000}
    tokens = ReviewTokenService(
        b"test-secret", ttl_seconds=10, clock=_clock_at(box)
    )
    token, jti, _exp = tokens.issue("run-1", "hash-a")

    with pytest.raises(ReviewTokenError) as mismatch:
        tokens.validate(
            token, run_id="run-1", content_hash="hash-b", active_jti=jti
        )
    assert mismatch.value.reason == "hash_mismatch"

    tokens.consume(jti)
    fresh, fresh_jti, _exp = tokens.issue("run-1", "hash-a")
    with pytest.raises(ReviewTokenError) as consumed:
        tokens.validate(
            token, run_id="run-1", content_hash="hash-a", active_jti=fresh_jti
        )
    assert consumed.value.reason == "consumed"

    box["t"] = 2_000
    with pytest.raises(ReviewTokenError) as expired:
        tokens.validate(
            fresh, run_id="run-1", content_hash="hash-a", active_jti=fresh_jti
        )
    assert expired.value.reason == "expired"


def test_bad_signature_is_invalid():
    tokens = ReviewTokenService(b"test-secret", ttl_seconds=30)
    token, jti, _exp = tokens.issue("run-1", "hash-a")
    broken = token[:-4] + ("0000" if not token.endswith("0000") else "1111")
    with pytest.raises(ReviewTokenError) as exc:
        tokens.validate(
            broken, run_id="run-1", content_hash="hash-a", active_jti=jti
        )
    assert exc.value.reason == "invalid_token"
