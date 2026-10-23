#!/usr/bin/env bash
# Foreground local server for the /meeting slice.
#
#   cp backend/app/meeting/env.example .env   # optional until you add keys
#   scripts/meeting-dev.sh
#   # open http://127.0.0.1:5001/meeting/ui
#
# Creates the SQLite directory, runs `uv sync` in backend/, and execs
# uvicorn on 127.0.0.1:5001. Stop with Ctrl+C. The database file is kept.
# A backgrounded process writes backend/runtime/run.pid after startup;
# kill that pid.
#
# MOCK_ASR, MOCK_LLM, and MOCK_FEISHU become 1 only when that variable is
# unset in the shell and unset in .env, backend/.env, and the
# .env.development file beside either of those. A shell value wins.
# Other keys stay in the file; this script does not print them.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_DIR="${ROOT_DIR}/backend"
HOST="127.0.0.1"
PORT="5001"

declare -A SHELL_SET=()
declare -A FILE_HAS=()
declare -A FILE_VAL=()

for key in MOCK_ASR MOCK_LLM MOCK_FEISHU MEETING_DB_PATH; do
  if [[ -n "${!key+x}" ]]; then
    SHELL_SET["${key}"]=1
  fi
done

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

strip_quotes() {
  local value="$1"
  local len=${#value}
  if (( len >= 2 )); then
    local first="${value:0:1}"
    local last="${value:len-1:1}"
    if [[ "${first}" == '"' && "${last}" == '"' ]]; then
      value="${value:1:len-2}"
    elif [[ "${first}" == "'" && "${last}" == "'" ]]; then
      value="${value:1:len-2}"
    fi
  fi
  printf '%s' "${value}"
}

load_env_file() {
  local file="$1"
  local line key value
  [[ -f "${file}" ]] || return 0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%$'\r'}"
    line="$(trim "${line}")"
    [[ -z "${line}" || "${line}" == \#* ]] && continue
    if [[ "${line}" == export[[:space:]]* ]]; then
      line="$(trim "${line#export}")"
    fi
    if [[ ! "${line}" =~ ^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      continue
    fi
    key="${BASH_REMATCH[1]}"
    value="$(strip_quotes "$(trim "${BASH_REMATCH[2]}")")"
    FILE_HAS["${key}"]=1
    FILE_VAL["${key}"]="${value}"
  done < "${file}"
}

# Same later-wins order as backend/app/component/environment.py, except
# ~/.eigent/.env. That global file must not turn live vendors on for a
# meeting checkout that did not set MOCK_* itself.
load_env_file "${BACKEND_DIR}/.env"
load_env_file "${BACKEND_DIR}/.env.development"
load_env_file "${ROOT_DIR}/.env"
load_env_file "${ROOT_DIR}/.env.development"

apply_mock_default() {
  local key="$1"
  if [[ -n "${SHELL_SET[${key}]+x}" ]]; then
    return 0
  fi
  if [[ -n "${FILE_HAS[${key}]+x}" ]]; then
    return 0
  fi
  printf -v "${key}" '%s' "1"
  export "${key}"
}

show_assignment() {
  local key="$1"
  local where value
  if [[ -n "${SHELL_SET[${key}]+x}" ]]; then
    where="shell"
    value="${!key}"
  elif [[ -n "${FILE_HAS[${key}]+x}" ]]; then
    where="env file"
    value="${FILE_VAL[${key}]}"
  else
    where="default"
    value="${!key}"
  fi
  if (( ${#value} > 16 )); then
    value="(set)"
  fi
  printf 'meeting-dev: %s=%s (%s)\n' "${key}" "${value}" "${where}"
}

apply_mock_default MOCK_ASR
apply_mock_default MOCK_LLM
apply_mock_default MOCK_FEISHU
show_assignment MOCK_ASR
show_assignment MOCK_LLM
show_assignment MOCK_FEISHU

db_raw=""
if [[ -n "${SHELL_SET[MEETING_DB_PATH]+x}" ]]; then
  db_raw="${MEETING_DB_PATH}"
elif [[ -n "${FILE_HAS[MEETING_DB_PATH]+x}" ]]; then
  db_raw="${FILE_VAL[MEETING_DB_PATH]}"
fi
db_raw="$(trim "${db_raw}")"
if [[ "${db_raw}" == "~" ]]; then
  db_raw="${HOME}"
elif [[ "${db_raw}" == "~/"* ]]; then
  db_raw="${HOME}/${db_raw:2}"
fi

if [[ -z "${db_raw}" ]]; then
  DATA_DIR="${BACKEND_DIR}/data"
else
  case "${db_raw}" in
    /*)
      DATA_DIR="$(dirname "${db_raw}")"
      ;;
    *)
      DATA_DIR="$(dirname "${BACKEND_DIR}/${db_raw}")"
      ;;
  esac
fi

mkdir -p "${DATA_DIR}"
printf 'meeting-dev: sqlite directory %s\n' "${DATA_DIR}"

if ! command -v uv >/dev/null 2>&1; then
  echo "meeting-dev: uv is not on PATH." >&2
  echo "meeting-dev: install it, then rerun this script:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

printf 'meeting-dev: uv sync in %s\n' "${BACKEND_DIR}"
(
  cd "${BACKEND_DIR}"
  uv sync
)

if command -v timeout >/dev/null 2>&1 \
  && timeout 1 bash -c "echo >/dev/tcp/${HOST}/${PORT}" >/dev/null 2>&1
then
  echo "meeting-dev: ${HOST}:${PORT} is already in use." >&2
  echo "meeting-dev: stop that server with Ctrl+C, or kill the pid in backend/runtime/run.pid." >&2
  exit 1
fi

printf 'meeting-dev: http://%s:%s/meeting/ui\n' "${HOST}" "${PORT}"
printf 'meeting-dev: health http://%s:%s/health\n' "${HOST}" "${PORT}"
echo "meeting-dev: stop with Ctrl+C. The SQLite file is kept."

cd "${BACKEND_DIR}"
exec uv run uvicorn main:api --host "${HOST}" --port "${PORT}"
