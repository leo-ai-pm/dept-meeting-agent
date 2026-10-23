# Meeting efficiency module

Thin FastAPI slice for PRD v0.1. It does not modify the CAMEL / Eigent kernel.
Kernel run routes stay on `/runs`. These routes are under `/meeting`.

Product story (before / after, harness, metrics, dual entry, non-goals):
[docs/meeting-efficiency-harness.md](../../../docs/meeting-efficiency-harness.md).
This file remains the operator reference for flags and vendor behavior.

## Run locally

A clone can open the acceptance page on this machine. No cloud host and
no API keys. `MOCK_ASR`, `MOCK_LLM`, and `MOCK_FEISHU` stay on until you
change them.

Prerequisites: `git`, `curl`, and [uv](https://docs.astral.sh/uv/). The
backend requires Python 3.11 (`>=3.11,<3.12`). `uv sync` downloads that
interpreter when it is missing.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/leo-ai-pm/dept-meeting-agent.git
cd dept-meeting-agent
cp backend/app/meeting/env.example .env
scripts/meeting-dev.sh
```

`scripts/meeting-dev.sh` creates the SQLite directory (default
`backend/data/`), runs `uv sync` in `backend/`, and starts uvicorn in the
foreground on `127.0.0.1:5001`. The command it runs is
`uv run uvicorn main:api --host 127.0.0.1 --port 5001` from `backend/`.

Stop that terminal with Ctrl+C. The database file is kept. If you
backgrounded the script, the process writes `backend/runtime/run.pid`
after startup; `kill` that pid. `backend/data/` and `backend/runtime/`
are gitignored.

Acceptance page: <http://127.0.0.1:5001/meeting/ui>

`GET /health` is the existing Eigent health route. It returns
`{"status":"ok","service":"eigent"}` when this process is up. `GET /`
points at that route. `GET /meeting/ui` is the acceptance page. This
slice does not add another health service.

The page copy is Simplified Chinese first. Buttons keep the English names
Create run, Import transcript, Start live, Stop live, Approve, Reject,
Write with token, Write without token, and Replay.

The script sets each of `MOCK_ASR`, `MOCK_LLM`, and `MOCK_FEISHU` to `1`
only when that variable is unset in the shell and unset in `.env`,
`backend/.env`, and the `.env.development` file next to either of those.
A value already in the shell wins over the file. Other variables stay in
the file. The API loads those files on startup, and also `~/.eigent/.env`
at lower precedence. The copied `.env` is gitignored. Do not commit it.
CI and the acceptance page use the fixtures in `fixtures/` while the mock
flags stay on.

A missing vendor key does not stop the process from booting. Set a
`MOCK_*` flag to `0` only when that vendor's key is in `.env`. The run
fails and does not substitute the fixtures. This script does not check
keys at startup.

Optional mock demo, while `scripts/meeting-dev.sh` is still up and still
on the default mocks:

```bash
MEETING_BASE_URL=http://127.0.0.1:5001 scripts/meeting-mock-demo.sh
```

Without `MEETING_BASE_URL`, that script starts its own short-lived
server, forces the three mock flags on, imports `fixtures/transcript.txt`,
prints the draft title, todo count, and replay event types, and exits
non-zero on failure. It does not leave a server running.

Live vendors (Volcano speech, Ark minutes, `lark-cli`) are in the flags
table below and in
[docs/meeting-efficiency-harness.md](../../../docs/meeting-efficiency-harness.md).

## Local store

Runs survive a process restart. The API (`build_meeting_service`) stores
them in SQLite, not in process memory. After uvicorn comes back, the same
`run_id` still supports `GET /meeting/runs/{id}`, replay, approve/reject,
and a Feishu write that carries a still-valid review token.

The default file is `backend/data/meeting.db`. The parent directory is
created if it is missing. That path is next to this backend package, so it
does not follow the process working directory. Set `MEETING_DB_PATH` to
move the file. A relative value is resolved from the working directory.
`backend/data/` is gitignored.

The file holds the run snapshot (status, transcript text, minutes, todos,
frozen draft, `content_hash`, approval, active review-token id), the replay
event log, review-token rows (id, content hash, expiry, consumed flag), and
Feishu receipts. Audio bytes are not stored; the snapshot keeps the
filename, byte count, and sha256. The review-token HMAC secret is not
stored. Set `MEETING_REVIEW_TOKEN_SECRET` if a token issued before a
restart must still verify. When that variable is unset, the process still
mints an ephemeral secret and does not write it, so those tokens fail
after a restart even though the run row remains.

`MOCK_ASR`, `MOCK_LLM`, and `MOCK_FEISHU` still default to on. The store
does not change adapter selection or the human gate: a Feishu write without
a valid token is still a hard reject. Unit tests may pass an in-memory
`RunStore`. There is no Postgres and no multi-user auth in this slice.

## Flags

| Variable | Default | Effect |
| --- | --- | --- |
| `MOCK_ASR` | `1` | `1` returns `fixtures/transcript.txt`. `0` calls the live ASR backend |
| `ASR_BACKEND` | `volcano` | Live backend when `MOCK_ASR=0`. `volcano` calls 豆包录音文件识别标准版 (submit, then query). `openai` or `whisper` calls OpenAI Whisper |
| `VOLCANO_ASR_API_KEY` | unset | Preferred 豆包语音控制台 API Key (`X-Api-Key`). Not the Ark minutes key. Never commit a real key |
| `VOLCANO_OPENSPEECH_API_KEY` | unset | Alias of `VOLCANO_ASR_API_KEY` when that variable is unset or blank |
| `VOLCANO_ASR_APP_KEY` | unset | Older-console APP ID (`X-Api-App-Key`). Used only when the speech API Key is unset. `VOLCANO_ASR_APP_ID` is an alias |
| `VOLCANO_ASR_ACCESS_KEY` | unset | Older-console Access Token (`X-Api-Access-Key`). Must be set with the APP ID. `VOLCANO_ASR_ACCESS_TOKEN` is an alias |
| `VOLCANO_ASR_RESOURCE_ID` | `volc.seedasr.auc` | 录音文件识别模型 2.0 standard. `volc.bigasr.auc` is model 1.0. Flash `volc.bigasr.auc_turbo` is not called |
| `VOLCANO_ASR_UID` | `dept-meeting-agent` | `user.uid` sent with the request. Not a secret |
| `VOLCANO_ASR_HTTP_TIMEOUT` | `60` | Seconds for each submit or query HTTP call |
| `VOLCANO_ASR_POLL_INTERVAL` | `2` | First delay after a pending query. Each later delay doubles |
| `VOLCANO_ASR_POLL_MAX_INTERVAL` | `15` | Cap on that delay. Values below the initial interval are raised to it |
| `VOLCANO_ASR_POLL_TIMEOUT` | `1800` | Overall seconds of query polling after a successful submit |
| `MOCK_LLM` | `1` | `1` returns `fixtures/minutes.json` and `fixtures/todos.json`. `0` calls Volcano Ark when a key is set |
| `MOCK_FEISHU` | `1` | `1` returns in-process receipts. `0` writes one Feishu task per todo through `lark-cli` |
| `OPENAI_API_KEY` | unset | Required only when `MOCK_ASR=0` and `ASR_BACKEND` is `openai` or `whisper`. Never commit a real key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API root, including `/v1`. Whisper path only |
| `MEETING_ASR_MODEL` | `whisper-1` | `audio/transcriptions` model. Whisper path only |
| `VOLCANO_API_KEY` | unset | Preferred key for real minutes when `MOCK_LLM=0`. Never commit a real key |
| `ARK_API_KEY` | unset | Alias of `VOLCANO_API_KEY`. Used only when `VOLCANO_API_KEY` is unset or blank |
| `VOLCANO_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | Ark data-plane root. The client posts `{base}/chat/completions` |
| `ARK_BASE_URL` | same default | Alias of `VOLCANO_BASE_URL` when that variable is unset or blank |
| `MEETING_LLM_MODEL` | `doubao-seed-2-1-turbo-260628` | Ark `model` field: a Model ID or an inference endpoint ID (`ep-...`). Empty uses this default |
| `MEETING_LLM_TIMEOUT` | `300` | Seconds for each Ark chat completions HTTP call when `MOCK_LLM=0`. Blank, non-numeric, or non-positive values keep 300. Not the ASR poll timeout |
| `MEETING_DB_PATH` | `backend/data/meeting.db` | SQLite file for runs, replay, review-token gate state, and Feishu receipts. Parent directory is created. Unset uses the default next to this backend package. A relative path is cwd-relative |
| `MEETING_REVIEW_TOKEN_TTL_SECONDS` | `3600` | Review token lifetime |
| `MEETING_REVIEW_TOKEN_SECRET` | unset | HMAC secret. If unset, the process mints an ephemeral secret and does not write it. Set this so review tokens still verify after a restart. The secret is not stored in the SQLite file |
| `FEISHU_BACKEND` | `lark_cli` | Live backend when `MOCK_FEISHU=0`. `lark_cli` (also `lark-cli` or `cli`) runs `lark-cli`. `enterprise` selects the custom-app fallback |
| `LARK_CLI_BIN` | `lark-cli` | Binary name or absolute path for the lark-cli adapter. Ignored when `MOCK_FEISHU=1` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | unset | Enterprise custom app. Used only when `MOCK_FEISHU=0` and `FEISHU_BACKEND=enterprise`. Not read by the lark-cli path |
| `FEISHU_API_BASE` | `https://open.feishu.cn` | Feishu Open API origin. Enterprise backend only |

Set `MOCK_ASR=0` to call a live ASR backend. The default is Volcano Doubao
speech recognition (豆包语音), not OpenAI Whisper. `ASR_BACKEND=volcano`
uses 豆包录音文件识别标准版, model 2.0:

1. `POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit`
2. `POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/query`

Flash is not the live default. The openspeech account behind this module
cannot enable resource `volc.bigasr.auc_turbo` (status `45000030`, resource
not granted). The console only offers 录音文件识别 2.0 标准版, which is
already open. That standard resource is `volc.seedasr.auc`. The old flash
URL `.../auc/bigmodel/recognize/flash` is not called. There is no
`volcano_flash` backend. `VOLCANO_ASR_TIMEOUT` from the flash client is
not read.

The submit body is JSON. `audio.data` is standard base64 of the uploaded
bytes, so the file does not need a public URL. `audio.url` is not sent.
The published standard field table lists `audio.url` as the audio source.
On this request schema, `audio.url` and `audio.data` are the two sources
(the flash request docs call them 二选一 and say the other request fields
match the standard API). Inline `audio.data` is what this process sends,
because the meeting import only has bytes and this service has no object
storage. A filename suffix of wav, mp3, ogg/opus, spx, amr, aac, or m4a
is sent as `audio.format` (`ogg` and `opus` also set `codec` to `opus`).
Unknown suffixes omit `format` and let the vendor reject the container.
The body also sends `user.uid` and `request.model_name=bigmodel`.

Submit headers are `X-Api-Resource-Id` (default `volc.seedasr.auc`),
`X-Api-Request-Id` (a new UUID, reused as the task id), and
`X-Api-Sequence: -1`. Query uses the same key headers and the same
request id, with a JSON body of `{}`, and does not send `X-Api-Sequence`.
The new speech console authenticates with `X-Api-Key`. An older console
can omit that key and set `X-Api-App-Key` plus `X-Api-Access-Key` instead.
When `VOLCANO_ASR_API_KEY` is set, the older pair is not sent. Set
`VOLCANO_ASR_RESOURCE_ID=volc.bigasr.auc` only for model 1.0 standard.
Do not point this path at `volc.bigasr.auc_turbo`.

This key comes from the 豆包语音控制台 (openspeech). It is a different
product from the Ark chat key. `VOLCANO_API_KEY` / `ARK_API_KEY` stay on
the minutes path and are not tried as a speech key. A missing or blank
speech key raises `ProviderNotConfigured` and does not invent a transcript.
The message points at the 豆包语音控制台, not at OpenAI.

Standard recognition accepts wav, mp3, ogg, spx, amr, aac, and m4a, up to
512MB and 5 hours. The hard cap enforced here is 512MB. Base64 adds about
a third to the JSON body and holds the file in memory. Volcano's inline
upload note still asks to keep a raw binary near 20MB when egress is
tight. Larger meeting files stay inline; a vendor rejection fails the run.

Submit must return `X-Api-Status-Code: 20000000`. An empty submit body is
normal. The task id is the request id we generated. Query then polls.
`20000001` (processing) and `20000002` (queued) wait and retry. They do
not become transcript text. The first wait is `VOLCANO_ASR_POLL_INTERVAL`
(default 2 seconds). Each later wait doubles until
`VOLCANO_ASR_POLL_MAX_INTERVAL` (default 15 seconds). Polling stops at
`VOLCANO_ASR_POLL_TIMEOUT` (default 1800 seconds, 30 minutes) and fails
the run. A multi-hour recording can raise that toward 10800 seconds. The
vendor says a task usually finishes within 3 hours. Each HTTP call uses
`VOLCANO_ASR_HTTP_TIMEOUT` (default 60 seconds). A dropped query
connection retries until the poll budget. A dropped submit connection
fails immediately. Import and live stop still block the request for the
whole poll.

A finished query returns `X-Api-Status-Code: 20000000` and `result.text`
(or utterance text when the top-level text is absent). Other status
codes, including silence `20000003`, fail the run. HTTP errors fail the
run. The client does not substitute `fixtures/transcript.txt`.

`ASR_BACKEND=openai` or `ASR_BACKEND=whisper` keeps the OpenAI path:
`POST /audio/transcriptions` with the audio file, `MEETING_ASR_MODEL`, and
`Authorization: Bearer`. `OPENAI_BASE_URL` may point at any compatible
OpenAI-HTTP endpoint. A missing or blank `OPENAI_API_KEY` on that path
raises `ProviderNotConfigured` and does not invent a transcript. The
default `MOCK_ASR=1` stays on the fixture even if a speech key or an
OpenAI key is present. Whisper keeps its own 120 second timeout.

Set `MOCK_LLM=0` to call Volcano Ark
`POST https://ark.cn-beijing.volces.com/api/v3/chat/completions`. The body is
OpenAI-compatible chat completions: `model`, `messages`, and
`response_format.type=json_object`, with `Authorization: Bearer`. The client
makes two calls, one for minutes and one for todos, and maps the model JSON
onto the fixture shapes (`title`, `decisions`, `summary`, `body`, and a todo
list of `title`, `description`, `assignee`, `due_date`, `source_quote`). A
missing or blank key raises `ProviderNotConfigured` and does not invent
minutes. HTTP failures and invalid or empty model JSON fail the run and do
not fall back to the fixtures. `MOCK_LLM=1` stays on the fixtures even if a
key is present. `VOLCANO_API_KEY` wins over `ARK_API_KEY` when both are set.
The same preference applies to `VOLCANO_BASE_URL` and `ARK_BASE_URL`.

The default model is the activated Ark Model ID
`doubao-seed-2-1-turbo-260628`. Leave `MEETING_LLM_MODEL` unset or empty
to use that ID. Set `MEETING_LLM_MODEL` to another Model ID or an inference
endpoint ID (`ep-...`) to override it. A rejected model fails the run.
Deep-thinking replies can run past two minutes. Live `extract_todos`
took about 173 seconds and hit the previous 180 second client timeout,
so the minutes client waits 300 seconds. Set `MEETING_LLM_TIMEOUT` to
override that wait. Blank, non-numeric, or non-positive values keep
300. Feishu keeps its own timeout. This does not change ASR timeouts.

Set `MOCK_FEISHU=0` to create one Feishu task per approved todo with the
official Lark/Feishu CLI (`lark-cli`, npm package `@larksuite/cli`). Install
the CLI, run `lark-cli config init` when the app is not configured yet, then
`lark-cli auth login` so a user identity with the task and contact domains is
ready. The adapter prefers `--as user` so writes follow that login instead of
an enterprise app secret.

Readiness is local. `lark-cli` must be on `PATH` (or `LARK_CLI_BIN`), and
`lark-cli auth status --json` must show `identities.user.status` of `ready`
or `needs_refresh` (`needs_refresh` still has a usable token; the CLI
refreshes it on the next user call). The adapter does not pass `--verify`,
because that flag calls the Feishu API. A missing binary or a user identity
that is not ready raises a not-configured error before any task create and
does not consume the review token.

Each todo becomes `lark-cli task +create --json --as user` with `--summary`,
`--description`, `--idempotency-key` (the todo idempotency key), `--due` when
the todo has an ISO date, and `--assignee` only when the assignee is already
an `ou_…` open_id. Display names are not resolved to open_ids. The command
stdout is the CLI envelope `{ok, data:{guid, url, ...}}`. Receipts use
`feishu_task_id` for `data.guid` and include `url` when it is present.

`FEISHU_APP_ID` and `FEISHU_APP_SECRET` are not required on this path, and the
CLI child process does not receive them. Set `FEISHU_BACKEND=enterprise` to
use `EnterpriseFeishuTaskAdapter` instead. That fallback still needs the
enterprise custom app credentials and does not call `lark-cli`. A write on
that backend with missing credentials is refused before any HTTP call and
does not consume the review token.

## Paths

| Method | Path | Result |
| --- | --- | --- |
| POST | `/meeting/runs` | `created` |
| POST | `/meeting/runs/{id}/import/audio` | ingest, then automatic pipeline |
| POST | `/meeting/runs/{id}/import/transcript` | skip ASR, then drafting |
| POST | `/meeting/runs/{id}/live/start` | capture marker; status stays `created` |
| POST | `/meeting/runs/{id}/live/stop` | recorded audio, then the same pipeline as import/audio |
| GET | `/meeting/runs/{id}` | snapshot |
| GET | `/meeting/runs/{id}/replay` | event log |
| GET | `/meeting/runs/{id}/draft` | minutes and todos |
| POST | `/meeting/runs/{id}/review` | `approve`, `reject`, or `edit_approve` |
| POST | `/meeting/runs/{id}/write_feishu_tasks` | requires `review_token` |
| POST | `/meeting/runs/{id}/export_feishu` | same gate, alias |

`/meeting/ui` records in the browser. Start live calls `getUserMedia` and
`MediaRecorder`. Stop live posts JSON `filename` and `content_base64`, the
same shape as audio import. The page prefers `audio/ogg;codecs=opus`
(`.ogg`) or `audio/mp4` (`.m4a`), which standard Volcano ASR accepts, and
otherwise `audio/webm`. Those bytes follow the import/audio ASR path
(`MOCK_ASR`, `ASR_BACKEND`, Volcano standard submit/query, or Whisper) and
stop at `awaiting_review`. Feishu writes still require human review.

`MOCK_ASR=1` may call live stop with an empty body. The service then uses
`mock-live:{id}` so CI can run without a microphone. `MOCK_ASR=0` rejects
empty or missing audio with HTTP 422 `empty_audio` and does not invent a
transcript.

States: `created` → `ingesting` → `transcribing` → `drafting` →
`awaiting_review` → `writing_feishu` → `succeeded`, plus `failed` and
`rejected`. Import and live stop run the pipeline in the request. There is no
separate generate action.

`write_feishu_tasks` / `export_feishu` without a valid one-time token, or with
a hash mismatch, or with a consumed or expired token, returns HTTP 403
`hard_reject`, appends `permission.hard_reject`, and does not call Feishu.

## Tests

```bash
cd backend
uv run pytest tests/app/meeting -q
```
