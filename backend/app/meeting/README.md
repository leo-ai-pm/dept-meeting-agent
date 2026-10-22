# Meeting efficiency module

Thin FastAPI slice for PRD v0.1. It does not modify the CAMEL / Eigent kernel.
Kernel run routes stay on `/runs`. These routes are under `/meeting`.

## Run locally

From `backend/`, with the project virtualenv:

```bash
export MOCK_ASR=1
export MOCK_LLM=1
export MOCK_FEISHU=1
uv run uvicorn main:api --port 5001
```

Acceptance page: <http://127.0.0.1:5001/meeting/ui>

`MOCK_ASR`, `MOCK_LLM`, and `MOCK_FEISHU` default to on when unset. CI and the
acceptance page use the fixtures in `fixtures/`. No API keys are required.

## Flags

| Variable | Default | Effect |
| --- | --- | --- |
| `MOCK_ASR` | `1` | `1` returns `fixtures/transcript.txt`. `0` calls OpenAI Whisper when `OPENAI_API_KEY` is set |
| `MOCK_LLM` | `1` | `1` returns `fixtures/minutes.json` and `fixtures/todos.json`. `0` calls Volcano Ark when a key is set |
| `MOCK_FEISHU` | `1` | `1` returns in-process receipts. `0` writes one Feishu task per todo through `lark-cli` |
| `OPENAI_API_KEY` | unset | Required for real ASR when `MOCK_ASR=0`. Never commit a real key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API root, including `/v1`. Used only when `MOCK_ASR=0` |
| `MEETING_ASR_MODEL` | `whisper-1` | `audio/transcriptions` model. Used only when `MOCK_ASR=0` |
| `VOLCANO_API_KEY` | unset | Preferred key for real minutes when `MOCK_LLM=0`. Never commit a real key |
| `ARK_API_KEY` | unset | Alias of `VOLCANO_API_KEY`. Used only when `VOLCANO_API_KEY` is unset or blank |
| `VOLCANO_BASE_URL` | `https://ark.cn-beijing.volces.com/api/v3` | Ark data-plane root. The client posts `{base}/chat/completions` |
| `ARK_BASE_URL` | same default | Alias of `VOLCANO_BASE_URL` when that variable is unset or blank |
| `MEETING_LLM_MODEL` | `doubao-seed-2-1-turbo-260628` | Ark `model` field: a Model ID or an inference endpoint ID (`ep-...`). Empty uses this default |
| `MEETING_REVIEW_TOKEN_TTL_SECONDS` | `3600` | Review token lifetime |
| `MEETING_REVIEW_TOKEN_SECRET` | unset | HMAC secret. If unset, the process mints an ephemeral secret and does not write it |
| `FEISHU_BACKEND` | `lark_cli` | Live backend when `MOCK_FEISHU=0`. `lark_cli` (also `lark-cli` or `cli`) runs `lark-cli`. `enterprise` selects the custom-app fallback |
| `LARK_CLI_BIN` | `lark-cli` | Binary name or absolute path for the lark-cli adapter. Ignored when `MOCK_FEISHU=1` |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | unset | Enterprise custom app. Used only when `MOCK_FEISHU=0` and `FEISHU_BACKEND=enterprise`. Not read by the lark-cli path |
| `FEISHU_API_BASE` | `https://open.feishu.cn` | Feishu Open API origin. Enterprise backend only |

Set `MOCK_ASR=0` to call OpenAI `POST /audio/transcriptions`. The request
sends the audio file and `MEETING_ASR_MODEL` with `Authorization: Bearer`.
`OPENAI_BASE_URL` may point at any compatible OpenAI-HTTP endpoint. A missing
or blank `OPENAI_API_KEY` raises `ProviderNotConfigured` and does not invent
a transcript. The default `MOCK_ASR=1` stays on the fixture even if a key is
present.

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
Deep-thinking replies can run past two minutes, so the minutes client
waits 180 seconds. Whisper ASR and Feishu keep their own timeouts.

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
| POST | `/meeting/runs/{id}/live/stop` | automatic pipeline |
| GET | `/meeting/runs/{id}` | snapshot |
| GET | `/meeting/runs/{id}/replay` | event log |
| GET | `/meeting/runs/{id}/draft` | minutes and todos |
| POST | `/meeting/runs/{id}/review` | `approve`, `reject`, or `edit_approve` |
| POST | `/meeting/runs/{id}/write_feishu_tasks` | requires `review_token` |
| POST | `/meeting/runs/{id}/export_feishu` | same gate, alias |

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
