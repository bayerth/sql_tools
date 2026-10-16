#!/bin/bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "$script_dir/.." && pwd)"
dry_run=false
local_mode=false
override=true
foreground=false
openai_format=false
input_path=""
output_path=""
batch_size=0
max_rows=0
checkpoint_size=""
context_length=""
model=""
log_level=""

print_usage() {
  cat <<'EOF'
Usage: scripts/run_expand_sql_statements.sh --input PATH --output DIR [--dry-run] [--batch-size N] [--max-rows N] [--local] [--no-override] [--foreground] [--checkpoint-size N] [--context-length N] [--model NAME] [--log-level LEVEL] [--openai-format]

--input and --output are mandatory. EXPANSION_INPUT and EXPANSION_OUTPUT
may be set instead of those flags. If neither the flag nor the variable is
set, input defaults to data/sql_validation/correct_rows.json and output to
a unique directory per run.

Remote full runs start with nohup in the background. --foreground, dry-runs,
and local runs stay in this process. Batch size defaults to 0 (individual requests).
Max rows defaults to 0 (all rows). Batch mode rounds up to a full batch;
the Python program's dry-run cap still applies. Existing result and checkpoint
files are replaced. --no-override refuses when any of them already exist.
--model overrides the local 20B or remote 120B model.
--log-level defaults to INFO. Values: DEBUG, INFO, WARNING, ERROR, CRITICAL.

Environment overrides:
  EXPANSION_INPUT   Input file or dataset (default: data/sql_validation/correct_rows.json)
  EXPANSION_OUTPUT  Output directory (default: a unique directory per run)
  EXPANSION_PYTHON  Python executable (default: active or project virtual environment)

Relative input/output paths are resolved from the project root.
LMSTUDIO_MAC_URL, MAC_STUDIO_API_KEY, LMSTUDIO_BASE_URL and LMSTUDIO_API_KEY
are passed through to the Python program.

Multi-word flags also accept underscores, matching the Python program
(--dry_run, --batch_size, --max_rows, --checkpoint_size, --context_length,
--openai_format, --log_level).
EOF
}

require_number() {
  local flag="$1"
  local value="$2"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    echo "$flag requires a non-negative integer." >&2
    exit 2
  fi
}

while (($#)); do
  case "$1" in
    --input)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "--input requires a path." >&2
        exit 2
      fi
      input_path="$2"
      shift 2
      ;;
    --output)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "--output requires a directory." >&2
        exit 2
      fi
      output_path="$2"
      shift 2
      ;;
    --dry-run|--dry_run) dry_run=true; shift ;;
    --local) local_mode=true; shift ;;
    --override) override=true; shift ;;
    --no-override) override=false; shift ;;
    --foreground) foreground=true; shift ;;
    --openai-format|--openai_format) openai_format=true; shift ;;
    --batch-size|--batch_size)
      if [[ $# -lt 2 ]]; then
        echo "$1 requires a non-negative integer." >&2
        exit 2
      fi
      require_number "$1" "$2"
      batch_size="$2"
      shift 2
      ;;
    --max-rows|--max_rows)
      if [[ $# -lt 2 ]]; then
        echo "$1 requires a non-negative integer." >&2
        exit 2
      fi
      require_number "$1" "$2"
      max_rows="$2"
      shift 2
      ;;
    --checkpoint-size|--checkpoint_size)
      if [[ $# -lt 2 ]]; then
        echo "$1 requires a positive integer." >&2
        exit 2
      fi
      require_number "$1" "$2"
      if [[ "$2" -eq 0 ]]; then
        echo "$1 requires a positive integer." >&2
        exit 2
      fi
      checkpoint_size="$2"
      shift 2
      ;;
    --context-length|--context_length)
      if [[ $# -lt 2 ]]; then
        echo "$1 requires a positive integer." >&2
        exit 2
      fi
      require_number "$1" "$2"
      if [[ "$2" -eq 0 ]]; then
        echo "$1 requires a positive integer." >&2
        exit 2
      fi
      context_length="$2"
      shift 2
      ;;
    --model)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "--model requires a model name." >&2
        exit 2
      fi
      model="$2"
      shift 2
      ;;
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

cd "$project_root"
python_executable="${EXPANSION_PYTHON:-}"
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
  echo "No Python executable found. Set EXPANSION_PYTHON to its full path." >&2
  exit 1
fi

run_id="$(date +%Y%m%d_%H%M%S)_$$"
input_path="${input_path:-${EXPANSION_INPUT:-data/sql_validation/correct_rows.json}}"
output_path="${output_path:-${EXPANSION_OUTPUT:-data/sql_validation/expansion_$run_id}}"
if [[ "$input_path" != /* ]]; then
  input_path="$project_root/$input_path"
fi
if [[ "$output_path" != /* ]]; then
  output_path="$project_root/$output_path"
fi
command=("$python_executable" -u "$project_root/src/sql_tools/run_expand_sql_statements.py"
  --input "$input_path" --output "$output_path" --batch-size "$batch_size"
  --max-rows "$max_rows")
if "$dry_run"; then
  command+=(--dry-run)
else
  command+=(--checkpoints)
fi
if "$local_mode"; then
  command+=(--local)
fi
if "$override"; then
  command+=(--override)
else
  command+=(--no-override)
fi
if "$foreground"; then
  command+=(--foreground)
fi
if "$openai_format"; then
  command+=(--openai-format)
fi
if [[ -n "$checkpoint_size" ]]; then
  command+=(--checkpoint-size "$checkpoint_size")
fi
if [[ -n "$context_length" ]]; then
  command+=(--context-length "$context_length")
fi
if [[ -n "$model" ]]; then
  command+=(--model "$model")
fi
if [[ -n "$log_level" ]]; then
  command+=(--log-level "$log_level")
fi

printf 'Command:'
printf ' %q' "${command[@]}"
printf '\n'
if ! "$dry_run" && ! "$local_mode" && ! "$foreground"; then
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
