#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/.." && pwd)"
dataset_id=""
output_path=""
max_rows=""
dialect=""
dry_run=false
override=true
foreground=false
log_level=""

print_usage() {
  cat <<'EOF'
Usage: scripts/run_spider_hardness.sh --dataset-id ID --output DIR [--max-rows N] [--dialect NAME] [--dry-run] [--no-override] [--foreground] [--log-level LEVEL]

--dataset-id is mandatory. SPIDER_DATASET may be set instead of that flag.
--output is mandatory unless SPIDER_OUTPUT is set. If neither the flag nor
the variable is set, output defaults to a unique directory per run.

Remote full runs start with nohup in the background. --foreground and
dry-runs stay in this process. --max-rows defaults to all rows. --dialect
defaults to generic. Dry-run classifies at most 20 rows and writes no result
files. Existing result files are replaced. --no-override refuses when any
of them already exist.
--log-level defaults to INFO. Values: DEBUG, INFO, WARNING, ERROR, CRITICAL.

Environment overrides (used when the matching flag is omitted):
  SPIDER_DATASET  Hugging Face dataset ID, saved dataset directory, or local data file
  SPIDER_OUTPUT   Result directory (default: a unique directory per run)
  SPIDER_MAX_ROWS Maximum rows across all splits
  SPIDER_DIALECT  SQL dialect
  SPIDER_PYTHON   Python executable (default: active or project virtual environment)

Relative output paths are resolved from the project root. Relative dataset
paths are resolved by the Python program after changing to the project root.

Multi-word flags also accept underscores, matching the Python program
(--dataset_id, --max_rows, --dry_run, --log_level).
EOF
}

require_value() {
  local flag="$1"
  if [[ $# -lt 2 || -z "${2:-}" || "$2" == --* ]]; then
    echo "$flag requires a value." >&2
    exit 2
  fi
}

while (($#)); do
  case "$1" in
    --dataset-id|--dataset_id)
      require_value "$1" "${2:-}"
      dataset_id="$2"
      shift 2
      ;;
    --output)
      require_value "$1" "${2:-}"
      output_path="$2"
      shift 2
      ;;
    --max-rows|--max_rows)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        echo "$1 requires a non-negative integer." >&2
        exit 2
      fi
      max_rows="$2"
      shift 2
      ;;
    --dialect)
      require_value "$1" "${2:-}"
      dialect="$2"
      shift 2
      ;;
    --dry-run|--dry_run) dry_run=true; shift ;;
    --override) override=true; shift ;;
    --no-override) override=false; shift ;;
    --foreground) foreground=true; shift ;;
    --log-level|--log_level)
      if [[ $# -lt 2 || -z "$2" || "$2" == --* ]]; then
        echo "$1 requires one of DEBUG, INFO, WARNING, ERROR, CRITICAL." >&2
        exit 2
      fi
      log_level="$(printf '%s' "$2" | tr '[:lower:]' '[:upper:]')"
      case "$log_level" in
        DEBUG|INFO|WARNING|ERROR|CRITICAL) ;;
        *) echo "$1 requires one of DEBUG, INFO, WARNING, ERROR, CRITICAL." >&2; exit 2 ;;
      esac
      shift 2
      ;;
    --help|-h) print_usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; print_usage >&2; exit 2 ;;
  esac
done

dataset_id="${dataset_id:-${SPIDER_DATASET:-}}"
output_path="${output_path:-${SPIDER_OUTPUT:-}}"
dialect="${dialect:-${SPIDER_DIALECT:-generic}}"
if [[ -z "$max_rows" && -n "${SPIDER_MAX_ROWS:-}" ]]; then
  if [[ ! "$SPIDER_MAX_ROWS" =~ ^[0-9]+$ ]]; then
    echo "SPIDER_MAX_ROWS requires a non-negative integer." >&2
    exit 2
  fi
  max_rows="$SPIDER_MAX_ROWS"
fi
if [[ -z "$dataset_id" ]]; then
  echo "--dataset-id is required." >&2
  print_usage >&2
  exit 2
fi

cd "$project_root"
python_executable="${SPIDER_PYTHON:-}"
if [[ -z "$python_executable" ]]; then
  for environment in "${VIRTUAL_ENV:-}" "$project_root/venv314" \
    "$project_root/.venv" "$project_root/.venv314" "$project_root/venv"; do
    if [[ -n "$environment" && -x "$environment/bin/python" ]]; then
      python_executable="$environment/bin/python"
      break
    fi
  done
fi
if [[ -z "$python_executable" || ! -x "$python_executable" ]]; then
  echo "No Python executable found. Set SPIDER_PYTHON to its full path." >&2
  exit 1
fi

run_id="$(date +%Y%m%d_%H%M%S)_$$"
output_path="${output_path:-data/sql/spider_hardness_$run_id}"
if [[ "$output_path" != /* ]]; then
  output_path="$project_root/$output_path"
fi
command=("$python_executable" -u "$project_root/src/sql_tools/run_spider_hardness.py"
  --dataset-id "$dataset_id" --output "$output_path" --dialect "$dialect")
if [[ -n "$max_rows" ]]; then
  command+=(--max-rows "$max_rows")
fi
if "$dry_run"; then
  command+=(--dry-run)
fi
if "$override"; then
  command+=(--override)
else
  command+=(--no-override)
fi
if "$foreground"; then
  command+=(--foreground)
fi
if [[ -n "$log_level" ]]; then
  command+=(--log-level "$log_level")
fi

printf 'Command:'
printf ' %q' "${command[@]}"
printf '\n'
if ! "$dry_run" && ! "$foreground"; then
  mkdir -p "$output_path"
  log_path="$output_path/run_$run_id.log"
  nohup "${command[@]}" </dev/null >"$log_path" 2>&1 &
  process_id=$!
  printf '%s\n' "$process_id" >"$output_path/run_$run_id.pid"
  printf 'Started in background. PID: %s\nOutput: %s\nLog: %s\n' \
    "$process_id" "$output_path" "$log_path"
  printf 'Follow log: tail -f %q\n' "$log_path"
else
  exec "${command[@]}"
fi
