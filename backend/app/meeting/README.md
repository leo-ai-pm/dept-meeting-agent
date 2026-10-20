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
| `MOCK_ASR` | `1` | `Transcriber` returns `fixtures/transcript.txt` |
| `MOCK_LLM` | `1` | `MinutesLLM` returns `fixtures/minutes.json` and `fixtures/todos.json` |
| `MOCK_FEISHU` | `1` | Task adapter returns in-process receipts and does not call Feishu |
| `MEETING_REVIEW_TOKEN_TTL_SECONDS` | `3600` | Review token lifetime |
| `MEETING_REVIEW_TOKEN_SECRET` | unset | HMAC secret. If unset, the process mints an ephemeral secret and does not write it |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | unset | Enterprise custom app. Used only when `MOCK_FEISHU=0` |
| `FEISHU_API_BASE` | `https://open.feishu.cn` | Feishu Open API origin |

Set a flag to `0` to leave the mock path. Real ASR and LLM vendors are not
wired yet; those calls fail the run instead of inventing a provider. A Feishu
write with `MOCK_FEISHU=0` and missing credentials is refused before any HTTP
call and does not consume the review token.

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
