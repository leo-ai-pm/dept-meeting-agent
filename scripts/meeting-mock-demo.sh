#!/usr/bin/env bash
# Mock demo for the /meeting slice. Forces MOCK_*=1, creates a run, imports
# fixtures/transcript.txt, and exits non-zero unless status is awaiting_review.
# Prints the draft title, todo count, and replay event types.
#
# Does not leave a server running. To click through the acceptance page:
#   cd backend && uv run uvicorn main:api --port 5001
# Then open http://127.0.0.1:5001/meeting/ui
#
# Reuse a server that is already on the default mocks:
#   MEETING_BASE_URL=http://127.0.0.1:5001 scripts/meeting-mock-demo.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
FIXTURE_DIR="${BACKEND_DIR}/app/meeting/fixtures"
TRANSCRIPT="${FIXTURE_DIR}/transcript.txt"
MINUTES="${FIXTURE_DIR}/minutes.json"
TODOS="${FIXTURE_DIR}/todos.json"
TMP_DIR="$(mktemp -d)"
SERVER_PID=""

terminate_process_tree() {
  local process_id="$1"
  local child_id

  while IFS= read -r child_id; do
    if [[ -n "${child_id}" ]]; then
      terminate_process_tree "${child_id}"
    fi
  done < <(pgrep -P "${process_id}" 2>/dev/null || true)

  if ! kill -0 "${process_id}" >/dev/null 2>&1; then
    return
  fi

  kill "${process_id}" >/dev/null 2>&1 || true
  for _ in {1..10}; do
    if ! kill -0 "${process_id}" >/dev/null 2>&1; then
      return
    fi
    sleep 0.1
  done
  kill -KILL "${process_id}" >/dev/null 2>&1 || true
}

cleanup() {
  local exit_code=$?
  if [[ -n "${SERVER_PID}" ]]; then
    terminate_process_tree "${SERVER_PID}"
    wait "${SERVER_PID}" 2>/dev/null || true
  fi
  if [[ ${exit_code} -ne 0 && -f "${TMP_DIR}/uvicorn.log" ]]; then
    echo "[meeting-mock-demo] server log:" >&2
    cat "${TMP_DIR}/uvicorn.log" >&2 || true
  fi
  rm -rf "${TMP_DIR}"
}
trap cleanup EXIT INT TERM

export MOCK_ASR=1
export MOCK_LLM=1
export MOCK_FEISHU=1

for required in "${TRANSCRIPT}" "${MINUTES}" "${TODOS}"; do
  if [[ ! -f "${required}" ]]; then
    echo "meeting-mock-demo: missing ${required}" >&2
    exit 1
  fi
done

BASE="${MEETING_BASE_URL:-}"
if [[ -z "${BASE}" ]]; then
  PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
  BASE="http://127.0.0.1:${PORT}"
  cat > "${TMP_DIR}/meeting_demo_app.py" << 'PY'
from fastapi import FastAPI

from app.meeting.router import router

app = FastAPI()
app.include_router(router)
PY
  (
    cd "${BACKEND_DIR}"
    export PYTHONPATH="${TMP_DIR}:${BACKEND_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
    if command -v uv >/dev/null 2>&1; then
      exec uv run python -m uvicorn meeting_demo_app:app \
        --host 127.0.0.1 --port "${PORT}" --log-level warning
    fi
    exec python3 -m uvicorn meeting_demo_app:app \
      --host 127.0.0.1 --port "${PORT}" --log-level warning
  ) >"${TMP_DIR}/uvicorn.log" 2>&1 &
  SERVER_PID=$!

  ready=0
  for _ in $(seq 1 60); do
    if curl -sf "${BASE}/meeting/ui" -o /dev/null; then
      ready=1
      break
    fi
    if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
  if [[ "${ready}" -ne 1 ]]; then
    echo "meeting-mock-demo: server did not become ready at ${BASE}" >&2
    exit 1
  fi
  echo "meeting-mock-demo: short-lived server ${BASE}"
else
  BASE="${BASE%/}"
  echo "meeting-mock-demo: using ${BASE}"
fi

python3 - "${TRANSCRIPT}" "${TMP_DIR}/import-body.json" << 'PY'
import json
import pathlib
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
if not text.strip():
    raise SystemExit("meeting-mock-demo: fixture transcript is empty")
pathlib.Path(sys.argv[2]).write_text(
    json.dumps({"text": text}, ensure_ascii=False),
    encoding="utf-8",
)
PY

request() {
  local method="$1"
  local url="$2"
  local outfile="$3"
  shift 3
  curl -sS -o "${outfile}" -w "%{http_code}" -X "${method}" "${url}" "$@"
}

create_code="$(
  request POST "${BASE}/meeting/runs" "${TMP_DIR}/create.json" \
    -H "Content-Type: application/json" \
    --data '{"title":"Mock demo"}'
)" || create_code="000"
if [[ "${create_code}" != "201" ]]; then
  echo "meeting-mock-demo: create run failed: HTTP ${create_code}" >&2
  if [[ -f "${TMP_DIR}/create.json" ]]; then
    cat "${TMP_DIR}/create.json" >&2
  fi
  exit 1
fi

run_id="$(
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["id"])' \
    "${TMP_DIR}/create.json"
)"

import_code="$(
  request POST "${BASE}/meeting/runs/${run_id}/import/transcript" \
    "${TMP_DIR}/import.json" \
    -H "Content-Type: application/json" \
    --data-binary @"${TMP_DIR}/import-body.json"
)" || import_code="000"
if [[ "${import_code}" != "200" ]]; then
  echo "meeting-mock-demo: import transcript failed: HTTP ${import_code}" >&2
  if [[ -f "${TMP_DIR}/import.json" ]]; then
    cat "${TMP_DIR}/import.json" >&2
  fi
  exit 1
fi

draft_code="$(
  request GET "${BASE}/meeting/runs/${run_id}/draft" "${TMP_DIR}/draft.json"
)" || draft_code="000"
if [[ "${draft_code}" != "200" ]]; then
  echo "meeting-mock-demo: draft failed: HTTP ${draft_code}" >&2
  if [[ -f "${TMP_DIR}/draft.json" ]]; then
    cat "${TMP_DIR}/draft.json" >&2
  fi
  exit 1
fi

replay_code="$(
  request GET "${BASE}/meeting/runs/${run_id}/replay" "${TMP_DIR}/replay.json"
)" || replay_code="000"
if [[ "${replay_code}" != "200" ]]; then
  echo "meeting-mock-demo: replay failed: HTTP ${replay_code}" >&2
  if [[ -f "${TMP_DIR}/replay.json" ]]; then
    cat "${TMP_DIR}/replay.json" >&2
  fi
  exit 1
fi

python3 - \
  "${TMP_DIR}/import.json" \
  "${TMP_DIR}/draft.json" \
  "${TMP_DIR}/replay.json" \
  "${MINUTES}" \
  "${TODOS}" << 'PY'
import json
import pathlib
import sys

imported = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
draft = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
replay = json.loads(pathlib.Path(sys.argv[3]).read_text(encoding="utf-8"))
expected_title = json.loads(
    pathlib.Path(sys.argv[4]).read_text(encoding="utf-8")
)["title"]
expected_todos = len(
    json.loads(pathlib.Path(sys.argv[5]).read_text(encoding="utf-8"))
)


def fail(message: str) -> None:
    print(f"meeting-mock-demo: {message}", file=sys.stderr)
    raise SystemExit(1)


if imported.get("status") != "awaiting_review":
    fail(
        "import status is "
        f"{imported.get('status')!r}, expected 'awaiting_review'"
    )
if draft.get("status") != "awaiting_review":
    fail(f"draft status is {draft.get('status')!r}")

minutes = draft.get("minutes") or {}
title = minutes.get("title")
todos = draft.get("todos")
if title != expected_title:
    fail(f"draft title is {title!r}, expected {expected_title!r}")
if not isinstance(todos, list) or len(todos) != expected_todos:
    found = len(todos) if isinstance(todos, list) else None
    fail(f"todo count is {found}, expected {expected_todos}")

events = replay.get("events")
if not isinstance(events, list) or not events:
    fail("replay events are missing")
types = []
for event in events:
    event_type = event.get("type") if isinstance(event, dict) else None
    if not isinstance(event_type, str) or not event_type:
        fail("replay event is missing type")
    types.append(event_type)

required = (
    "run.created",
    "tool.import_transcript",
    "tool.draft_minutes",
    "tool.extract_todos",
    "review.pending",
)
missing = [name for name in required if name not in types]
if missing:
    fail("replay missing " + ", ".join(missing))
if "tool.transcribe" in types:
    fail("transcript import recorded tool.transcribe")

print(f"run_id={imported.get('id')}")
print("status=awaiting_review")
print(f"draft_title={title}")
print(f"todo_count={len(todos)}")
print("replay_event_types=" + ",".join(types))
PY
