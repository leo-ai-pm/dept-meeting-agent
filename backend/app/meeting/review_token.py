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

"""One-time review tokens bound to run_id + content hash + TTL."""

from __future__ import annotations

import hmac
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256

TOKEN_PREFIX = "v1"

_ephemeral_secret: bytes | None = None
_ephemeral_lock = threading.Lock()


class ReviewTokenError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class TokenRecord:
    jti: str
    run_id: str
    content_hash: str
    exp: int
    consumed: bool = False


def load_review_secret() -> bytes:
    """Secret comes from the environment. If unset, the process mints one.

    The minted value is not written to disk and is not a committed key.
    """

    raw = os.environ.get("MEETING_REVIEW_TOKEN_SECRET", "").strip()
    if raw:
        return raw.encode()
    global _ephemeral_secret
    with _ephemeral_lock:
        if _ephemeral_secret is None:
            _ephemeral_secret = secrets.token_bytes(32)
        return _ephemeral_secret


class ReviewTokenService:
    def __init__(
        self,
        secret: bytes,
        *,
        ttl_seconds: int = 3600,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._secret = secret
        self._ttl = ttl_seconds
        self._clock = clock or _unix_now
        self._records: dict[str, TokenRecord] = {}
        self._lock = threading.Lock()

    def issue(self, run_id: str, content_hash: str) -> tuple[str, str, int]:
        jti = secrets.token_hex(16)
        exp = int(self._clock()) + int(self._ttl)
        token = self._sign(run_id, content_hash, exp, jti)
        with self._lock:
            self._records[jti] = TokenRecord(
                jti=jti,
                run_id=run_id,
                content_hash=content_hash,
                exp=exp,
            )
        return token, jti, exp

    def validate(
        self,
        token: str,
        *,
        run_id: str,
        content_hash: str,
        active_jti: str | None,
    ) -> TokenRecord:
        record = self._parse(token)
        if record.run_id != run_id:
            raise ReviewTokenError("run_mismatch")
        if record.content_hash != content_hash:
            raise ReviewTokenError("hash_mismatch")
        if record.consumed or record.jti != active_jti:
            raise ReviewTokenError("consumed")
        return record

    def consume(self, jti: str) -> None:
        with self._lock:
            record = self._records.get(jti)
            if record is None:
                raise ReviewTokenError("invalid_token")
            record.consumed = True

    def consume_run(self, run_id: str) -> None:
        with self._lock:
            for record in self._records.values():
                if record.run_id == run_id:
                    record.consumed = True

    def _parse(self, token: str) -> TokenRecord:
        parts = token.split(".")
        if len(parts) != 6 or parts[0] != TOKEN_PREFIX:
            raise ReviewTokenError("invalid_token")
        _, run_id, content_hash, exp_raw, jti, sig = parts
        expected = self._signature(run_id, content_hash, exp_raw, jti)
        if not hmac.compare_digest(expected, sig):
            raise ReviewTokenError("invalid_token")
        try:
            exp = int(exp_raw)
        except ValueError as exc:
            raise ReviewTokenError("invalid_token") from exc
        if self._clock() >= exp:
            raise ReviewTokenError("expired")
        with self._lock:
            record = self._records.get(jti)
            if record is None:
                raise ReviewTokenError("invalid_token")
            if (
                record.run_id != run_id
                or record.content_hash != content_hash
                or record.exp != exp
            ):
                raise ReviewTokenError("invalid_token")
            return record

    def _sign(self, run_id: str, content_hash: str, exp: int, jti: str) -> str:
        exp_raw = str(exp)
        sig = self._signature(run_id, content_hash, exp_raw, jti)
        return f"{TOKEN_PREFIX}.{run_id}.{content_hash}.{exp_raw}.{jti}.{sig}"

    def _signature(
        self, run_id: str, content_hash: str, exp_raw: str, jti: str
    ) -> str:
        body = f"{TOKEN_PREFIX}.{run_id}.{content_hash}.{exp_raw}.{jti}"
        return hmac.new(self._secret, body.encode(), sha256).hexdigest()


def _unix_now() -> float:
    import time

    return time.time()
