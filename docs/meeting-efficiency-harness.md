# Meeting efficiency harness / 会议效率 Harness

Interview from this page. It describes the thin FastAPI slice under
`backend/app/meeting` on the dept-meeting-agent fork (Apache-2.0 Eigent).
It does not replace the Eigent product README.

Operator flags, vendor URLs, and the env table stay in
[backend/app/meeting/README.md](../backend/app/meeting/README.md).
This page does not copy that table and does not contain secrets. Names
below are the variables already documented there.

Runs live in process memory. A restart drops them. This is not an Eigent
Space, Session, or Task, and it does not use those words as aliases for a
meeting run.

## 会前 / Before

会后靠人听录音、写纪要、拆待办，再手工建飞书任务。录音和文字稿是两条
手工流程，停录之后没有一份可审的草稿，任务也可能在没人确认时就写出去。

After a meeting, someone re-listens, writes minutes, splits todos, and
creates Feishu tasks by hand. A recording and a transcript are two manual
flows. Nothing is ready at stop, and a task can be written before a person
has accepted the draft.

## 会后 / After

双入口，停在同一条流水线。会前或会后可以导入录音或文字稿；会中可以在
验收页一键开始 / 停止录音。导入或停止的同一次请求里跑完识别（文字稿
跳过识别）、纪要和待办，状态停在 `awaiting_review`。人批准后才签发
一次性 `review_token`。飞书任务只在带着该 token 的写请求里创建。拒绝
不发 token，也不写飞书。

Two entries, one pipeline. Import a recording or a transcript, or use
in-meeting one-click record on the acceptance page. The import or stop
request itself runs ASR (skipped for a transcript), minutes, and todos,
then stops at `awaiting_review`. A person approves before a one-time
`review_token` is issued. Feishu tasks are created only by a write that
carries that token. Reject issues no token and writes nothing.

Acceptance UI: [http://127.0.0.1:5001/meeting/ui](http://127.0.0.1:5001/meeting/ui)
when the API is served from `backend/` on port 5001. The page is
Simplified Chinese first. The controls still read Create run, Import
transcript, Start live, Stop live, Approve, Reject, Write with token,
Write without token, and Replay. `MOCK_ASR`, `MOCK_LLM`, and
`MOCK_FEISHU` default to on, so that page and CI need no API keys. There
is no separate generate action.

## Harness 五要素 / Five parts

| Part | What it guarantees | Where it lives |
| --- | --- | --- |
| Entry | Create a run, then import or record. Kernel `/runs` is untouched. | Mounted in `backend/app/router.py` as `meeting_router` (prefix `/meeting`). Routes in `backend/app/meeting/router.py`. Page in `backend/app/meeting/static/index.html` at `GET /meeting/ui`. |
| Tools / adapters | Swap mock fixtures and live vendors without a second pipeline. | `backend/app/meeting/adapters/transcriber.py` (mock, Volcano standard, or Whisper), `adapters/minutes_llm.py` (mock or Volcano Ark), `adapters/feishu.py` (mock, `lark-cli`, or enterprise custom-app fallback). Selected in `build_meeting_service()` in `service.py`. |
| State machine + replay | Illegal jumps fail. Every step is an event you can read back. | States in `backend/app/meeting/state_machine.py`. Events appended in `MeetingService` (`backend/app/meeting/service.py`). `GET /meeting/runs/{id}/replay`. |
| Human gate | Feishu is unreachable until this run’s approved content is signed. | `POST /meeting/runs/{id}/review` with `approve`, `reject`, or `edit_approve`. Tokens in `backend/app/meeting/review_token.py` (HMAC, run id, content hash, TTL, one active id). |
| Export sink | One Feishu task per approved todo, or an in-process mock receipt. | `POST /meeting/runs/{id}/write_feishu_tasks`. `POST /meeting/runs/{id}/export_feishu` is the same function. |

Default live adapters, when the matching mock flag is `0`:

- ASR: Volcano 豆包录音文件识别标准版, resource `volc.seedasr.auc`
  (submit, then query). Not flash. The speech key is not the Ark minutes key.
- Minutes: Volcano Ark chat completions, default model
  `doubao-seed-2-1-turbo-260628`, two calls (minutes, then todos).
- Feishu: `lark-cli task +create` as the logged-in user. Display names are
  not resolved to open_ids. Only an assignee that is already `ou_…` is
  passed as `--assignee`. `FEISHU_BACKEND=enterprise` is the explicit
  custom-app fallback.

Mock adapters read `backend/app/meeting/fixtures/transcript.txt`,
`fixtures/minutes.json`, and `fixtures/todos.json`.

### States

```text
created
  live/start          stays created (live_active=true)
  import or live/stop → ingesting
    transcript        → drafting
    audio or live     → transcribing → drafting
  drafting            → awaiting_review
  approve / edit_approve   stays awaiting_review, issues review_token
  reject              → rejected (review_token is null)
  write + valid token → writing_feishu → succeeded
  pipeline or write error → failed
```

`import/audio`, `import/transcript`, and `live/stop` run that pipeline
inside the request. `GET /meeting/runs/{id}` is the snapshot.
`GET /meeting/runs/{id}/draft` returns minutes and todos once they exist
(`title`, `decisions` of 3–5 items, `summary`, `body`; todos with `title`,
`description`, `assignee`, `due_date`, `source_quote`, `idempotency_key`).
A missing assignee is stored as `待确认`. The content hash covers the
normalized minutes and todos.

Replay event types used on this path include `run.created`,
`state.changed`, `tool.import_audio`, `tool.import_transcript`,
`tool.start_live_recording`, `tool.stop_live_recording`, `tool.transcribe`,
`tool.draft_minutes`, `tool.extract_todos`, `review.pending`,
`review.approved`, `review.edit_approved`, `review.rejected`,
`permission.hard_reject`, `tool.write_feishu_tasks`, `tool.feishu_receipts`,
and `run.failed`.

A missing, bad, mismatched, consumed, or expired token returns HTTP 403
`hard_reject` with `feishu_called: false` and appends
`permission.hard_reject`. The Feishu adapter is not called. Rejecting a
draft does the same for any later write. `edit_approve` changes the hash
and issues a new token; the previous token no longer matches.

## 成功标准 / Success metrics

These are the v1 bars. No millisecond SLO is hard-coded.

| Metric | Pass | How to read it |
| --- | --- | --- |
| Time-to-draft after stop or import | The same HTTP response is `awaiting_review` or `failed`. No second generate call. | Wall clock of `POST …/import/audio`, `POST …/import/transcript`, or `POST …/live/stop`. Replay `duration_ms` on `tool.transcribe`, `tool.draft_minutes`, and `tool.extract_todos` is the provider time inside that request. |
| Review gate never bypassed | A write with no valid token does not create a task. Status stays `awaiting_review` after a missing-token reject. | `tests/app/meeting/test_feishu_gate.py`. UI button “Write without token” posts `{}`. |
| Feishu write requires the token | Body field `review_token`, bound to this run id and this content hash, TTL `MEETING_REVIEW_TOKEN_TTL_SECONDS` (default 3600), consumed on a write that gets past the readiness check. | `backend/app/meeting/review_token.py`. Unconfigured Feishu returns 503 and does not consume the token. |
| Mock CI green | `cd backend && uv run pytest tests/app/meeting -q` with the default mocks. | Includes `test_router_mount.py` (meeting routes exist, `/runs/{id}/import/transcript` does not) and `test_state_machine.py`. |
| Live smoke | Manual checklist below. Not a CI job. | Vendor calls happen only when the matching `MOCK_*=0` and that provider is configured. Missing keys fail the run and do not substitute fixtures. |

作品集目标 / Portfolio targets, for an interview only. They are not CI
gates, and this page does not record a measured baseline. 短会议在停止后到可用草稿
≤10 分钟；批准前待办的手工修改率 ≤10%；负责人已经是 `ou_…`，或不用改名就填对，
≥70%。 Time-to-usable draft after stop on a short meeting ≤10 min; manual
edit rate on todos before approve ≤10%; assignee already `ou_…` or
correctly filled without a rename ≥70%.

Live time-to-draft is dominated by standard ASR polling. The default poll
budget is `VOLCANO_ASR_POLL_TIMEOUT` (1800 seconds). Each Ark call uses a
300 second client timeout (`MEETING_LLM_TIMEOUT`), and the minutes path
makes two calls. v1 does not promise a sub-minute live draft. The mock
path returns the fixture draft in that same response, which is what CI
asserts.

### Live smoke checklist

Mock (default, no keys):

1. From `backend/`, run `uv run uvicorn main:api --port 5001` with
   `MOCK_ASR`, `MOCK_LLM`, and `MOCK_FEISHU` unset or `1`.
2. Open `/meeting/ui`. Create run. Import the sample transcript.
   Status becomes `awaiting_review` and Draft shows minutes and todos.
3. Reject. Replay includes `review.rejected`. Write with token is
   impossible because `review_token` is null. A write without a token is
   HTTP 403.
4. New run, import again, Approve, then “Write without token” (403,
   still `awaiting_review`) and “Write with token” (mock receipts,
   `succeeded`). Replay.

Microphone on the mock page: Start live / Stop live uses `getUserMedia`
and `MediaRecorder`, then posts `filename` and `content_base64` to
`live/stop`. CI may `POST live/stop` with an empty body; with
`MOCK_ASR=1` the service uses `mock-live:{id}` and does not invent a
transcript from silence. The page itself does not send an empty body.

Live vendors (only when you mean to leave the fixtures):

1. `MOCK_ASR=0` uses Volcano standard ASR unless `ASR_BACKEND` is
   `openai` or `whisper`. The speech key is the openspeech key
   (`VOLCANO_ASR_API_KEY` or the documented alias), not `VOLCANO_API_KEY`.
2. `MOCK_LLM=0` uses Ark via `VOLCANO_API_KEY` or `ARK_API_KEY`.
3. `MOCK_FEISHU=0` uses `lark-cli` after `lark-cli auth login`. Readiness
   is `lark-cli auth status --json` showing a user identity of `ready` or
   `needs_refresh`.
4. Stop with real audio reaches `awaiting_review` or `failed`.
   `MOCK_ASR=0` plus an empty live stop is HTTP 422 `empty_audio`.
5. Feishu still waits for Approve and a `review_token`.

## 模拟演示 / Mock demo

`scripts/meeting-mock-demo.sh` sets `MOCK_ASR`, `MOCK_LLM`, and
`MOCK_FEISHU` to `1`. It creates a run, imports
`backend/app/meeting/fixtures/transcript.txt`, and exits non-zero unless
the status is `awaiting_review`. On success it prints the draft title,
the todo count, and the replay event types. The script starts a
short-lived meeting server for those curl calls and stops it on exit. It
does not leave uvicorn running.

From the repository root:

```bash
scripts/meeting-mock-demo.sh
```

To click through `/meeting/ui`, start the API yourself and leave it up:

```bash
cd backend && uv run uvicorn main:api --port 5001
```

Open [http://127.0.0.1:5001/meeting/ui](http://127.0.0.1:5001/meeting/ui).
Unset mock flags stay on.
`MEETING_BASE_URL=http://127.0.0.1:5001 scripts/meeting-mock-demo.sh`
sends the same curl calls to that server instead of starting one. That
server must already be on the default mocks; the script cannot change
its flags.

## 双入口 / Dual entry

After ingest, audio import and live stop share the ASR path. Transcript
import joins at drafting. Review and Feishu do not branch on entry.

| Entry | Route | Acceptance UI | After ingest |
| --- | --- | --- | --- |
| Import audio | `POST /meeting/runs/{id}/import/audio` with JSON `filename` and `content_base64` | No file picker. API only. | `input.kind=audio`. States `ingesting` → `transcribing` → `drafting` → `awaiting_review`. Event `tool.transcribe`. |
| Import transcript | `POST /meeting/runs/{id}/import/transcript` with JSON `text` | Transcript textarea, “Import transcript”. | `input.kind=transcript`. States `ingesting` → `drafting` → `awaiting_review`. No `tool.transcribe`. |
| Live start | `POST /meeting/runs/{id}/live/start` | “Start live”. `getUserMedia` + `MediaRecorder`. | Marker only. Status stays `created`, `live_active=true`. No ASR. |
| Live stop | `POST /meeting/runs/{id}/live/stop` with the same JSON shape as audio import | “Stop live” posts the blob. Preferred MIME `audio/ogg;codecs=opus` (`.ogg`) or `audio/mp4` (`.m4a`); otherwise WebM. | `input.kind=live_audio`, then the same states as import audio. |

Live start is allowed only from `created` while `live_active` is false.
Import is allowed from `created` or `failed`, and is rejected while a live
capture is active. Standard Volcano accepts wav, mp3, ogg, spx, amr, aac,
and m4a. WebM is only the browser fallback; an unknown suffix does not
send `audio.format`, and the vendor can reject that file.

## 非目标 / Non-goals for v1

- No DingTalk and no WeCom. The only export sink is Feishu (mock,
  `lark-cli`, or the enterprise fallback).
- No Eigent / CAMEL kernel rewrite. Meeting routes stay under `/meeting`.
  Kernel run routes stay on `/runs`.
- No Feishu write without a human review token. Approve does not write.
  Stop does not write. Mock mode does not skip the gate.
- No streaming ASR. Transcription starts when audio import or live stop
  submits the bytes.
- No durable run store, no open_id lookup from a person’s name, and no
  flash resource `volc.bigasr.auc_turbo`.

## Where to read next

- Flags and vendor behavior:
  [backend/app/meeting/README.md](../backend/app/meeting/README.md)
- Routes: `backend/app/meeting/router.py`
- Pipeline: `backend/app/meeting/service.py`
- Tests: `backend/tests/app/meeting/`
