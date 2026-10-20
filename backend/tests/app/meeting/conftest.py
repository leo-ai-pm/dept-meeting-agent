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

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.meeting.adapters.feishu import MockFeishuTaskAdapter
from app.meeting.adapters.minutes_llm import MockMinutesLLM
from app.meeting.adapters.transcriber import MockTranscriber
from app.meeting.review_token import ReviewTokenService
from app.meeting.router import get_meeting_service, router
from app.meeting.service import MeetingService


def make_service(
    *,
    feishu=None,
    transcriber=None,
    minutes_llm=None,
    ttl_seconds=3600,
    clock=None,
) -> tuple[MeetingService, MockFeishuTaskAdapter]:
    adapter = feishu if feishu is not None else MockFeishuTaskAdapter()
    service = MeetingService(
        transcriber=transcriber or MockTranscriber(),
        minutes_llm=minutes_llm or MockMinutesLLM(),
        feishu=adapter,
        tokens=ReviewTokenService(
            b"test-secret",
            ttl_seconds=ttl_seconds,
            clock=clock or time.time,
        ),
    )
    return service, adapter


def make_client(service: MeetingService) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_meeting_service] = lambda: service
    return TestClient(app)
