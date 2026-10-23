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

"""Single-user identity seam for the meeting slice.

v1 is one person on one machine. ``SingleUserIdentity`` is the only
provider. It does not authenticate. It does not turn ``MEETING_USER_ID``
or the ``X-Meeting-User`` header into a caller.

# Extension: multi-user
See docs/multi-user-extension.md. Replace ``build_identity`` first.
Do not add login, a user table, or a cloud host in this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

LOCAL_USER_ID = "local"
MEETING_USER_ENV = "MEETING_USER_ID"
MEETING_USER_HEADER = "X-Meeting-User"


@dataclass(frozen=True)
class UserContext:
    """Opaque meeting caller. Not an authenticated principal.

    ``user_id`` is a stable string the store and a Feishu write can
    carry. v1 only produces ``LOCAL_USER_ID``.
    """

    user_id: str


class IdentityProvider(Protocol):
    """Resolve the caller for one meeting request.

    ``presented`` is the raw ``X-Meeting-User`` value, or None when
    the header was not sent. A v1 provider must not trust it.

    # Extension: multi-user
    """

    name: str

    def current(self, presented: str | None = None) -> UserContext: ...


class SingleUserIdentity:
    """Process-local provider. Always ``LOCAL_USER_ID``.

    # Extension: multi-user
    ``build_identity`` returns this class. A later provider can replace
    that factory. Until then, ``presented`` and ``MEETING_USER_ID`` are
    read and discarded so a stray header cannot split the SQLite file.
    """

    name = "single_user"

    def current(self, presented: str | None = None) -> UserContext:
        _discard_untrusted_user(
            presented,
            os.environ.get(MEETING_USER_ENV),
        )
        return UserContext(user_id=LOCAL_USER_ID)


def build_identity() -> IdentityProvider:
    """Provider installed by ``build_meeting_service``.

    # Extension: multi-user
    Keep returning ``SingleUserIdentity`` until a second provider
    exists. Do not branch on ``MEETING_USER_ID`` here.
    """

    return SingleUserIdentity()


def ignore_user_scope(user_id: str | None) -> None:
    """Accept an opaque user id and do not filter on it.

    # Extension: multi-user
    ``RunStore``, review-token issue/validate, and the Feishu request
    call this so a later change can scope by ``user_id`` without
    hunting for the argument. v1 still returns every run on this
    machine. See docs/multi-user-extension.md.
    """

    if user_id is None or isinstance(user_id, str):
        return
    raise TypeError("user_id must be a str or None")


def _discard_untrusted_user(*values: str | None) -> None:
    """Reserved inputs are inspected and dropped. They are not an id."""

    for value in values:
        if value is not None and not isinstance(value, str):
            raise TypeError("presented user id must be a str or None")
