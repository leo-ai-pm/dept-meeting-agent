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

"""Pluggable Transcriber, MinutesLLM, and Feishu task adapters."""

from app.meeting.adapters.feishu import (
    EnterpriseFeishuTaskAdapter,
    FeishuWriteRequest,
    LarkCliFeishuTaskAdapter,
    MockFeishuTaskAdapter,
    UnconfiguredFeishuTaskAdapter,
)
from app.meeting.adapters.minutes_llm import (
    MinutesLLM,
    MockMinutesLLM,
    UnconfiguredMinutesLLM,
    VolcanoMinutesLLM,
)
from app.meeting.adapters.transcriber import (
    MockTranscriber,
    OpenAIWhisperTranscriber,
    Transcriber,
    UnconfiguredTranscriber,
)

__all__ = [
    "EnterpriseFeishuTaskAdapter",
    "FeishuWriteRequest",
    "LarkCliFeishuTaskAdapter",
    "MinutesLLM",
    "MockFeishuTaskAdapter",
    "UnconfiguredFeishuTaskAdapter",
    "MockMinutesLLM",
    "MockTranscriber",
    "OpenAIWhisperTranscriber",
    "Transcriber",
    "UnconfiguredMinutesLLM",
    "UnconfiguredTranscriber",
    "VolcanoMinutesLLM",
]
