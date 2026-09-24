#!/usr/bin/env bash
# Recertia soak calendar: 2026-09-18 through 2026-10-18 (Home2 / WSL).
# One live Goal most days. Backup every run day. soak record ONLY Mondays.
# Does not declare GA. Does not promote. Does not use Anthropic.
# Does not pass --workspace-id (WSL cannot register D:\ roots).
#
# Usage:
#   ./soak-2026-09-18-to-10-18.sh              # today, if in range
#   ./soak-2026-09-18-to-10-18.sh 2026-09-21   # that day's block
#   ./soak-2026-09-18-to-10-18.sh --print      # print the calendar
#
# Copy onto the soak host:
#   cp soak-2026-09-18-to-10-18.sh /mnt/d/recertia/
#   chmod +x /mnt/d/recertia/soak-2026-09-18-to-10-18.sh

set -u
set -o pipefail

ROOT="${RECERTIA_SOAK_ROOT:-/mnt/d/recertia}"
START=2026-09-18
END=2026-10-18
MODEL="openai:z-ai/glm-5.3-flash"
VERIFIER="openai:z-ai/glm-5.3-flash"

# Cycle these after the first pass. add-editorconfig is the only goal.json.
SPECS=(
  "spec:evals/golden/repo-chore/add-gitignore-entry/task.json"
  "spec:evals/golden/repo-chore/add-license-mit/task.json"
  "spec:evals/golden/repo-chore/add-makefile-target/task.json"
  "spec:evals/golden/repo-chore/add-pytest-config/task.json"
  "spec:evals/golden/repo-chore/add-readme-section/task.json"
  "spec:evals/golden/repo-chore/bump-action-checkout/task.json"
  "spec:evals/golden/repo-chore/ensure-src-layout/task.json"
  "spec:evals/golden/repo-chore/pin-python-version/task.json"
  "spec:evals/golden/repo-chore/strip-trailing-whitespace/task.json"
  "goal:evals/golden/repo-chore/add-editorconfig/goal.json"
)

die() { echo "error: $*" >&2; exit 2; }

iso_week() {
  # GNU date (WSL / GNU coreutils).
  date -d "$1" +%G-W%V
}

dow() {
  date -d "$1" +%u
}

env_up() {
  cd "$ROOT" || die "missing $ROOT"
  if [[ -f .venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
  fi
  if [[ -f "${HOME}/.recertia-secrets" ]]; then
    # shellcheck disable=SC1091
    source "${HOME}/.recertia-secrets"
  fi
  unset ANTHROPIC_API_KEY ZAI_API_KEY RECERTIA_ALLOW_STUB_MODEL || true
  export RECERTIA_EXECUTION_BACKEND="${RECERTIA_EXECUTION_BACKEND:-container}"
  export RECERTIA_MODEL_PROVIDER=openai
  export RECERTIA_MODEL_ID=z-ai/glm-5.3-flash
  export RECERTIA_VERIFIER_MODEL_ID=z-ai/glm-5.3-flash
  export RECERTIA_OPENAI_BASE_URL=https://openrouter.ai/api/v1/chat/completions
  export RECERTIA_OPENAI_HTTP_REFERER=https://github.com/recertia/recertia
  export RECERTIA_OPENAI_TITLE=Recertia
  export RECERTIA_API_KEY_ENV="${RECERTIA_API_KEY_ENV:-OPENROUTER_KEY}"
  command -v recertia >/dev/null || die "recertia not on PATH; pip install -e . in $ROOT"
  python3 -c "import os,sys; sys.exit(0 if os.environ.get('OPENROUTER_KEY') else 1)" \
    || die "OPENROUTER_KEY missing in this shell"
}

live_run() {
  local kind="$1" path="$2"
  echo "== recertia run --${kind} ${path}"
  mkdir -p artifacts
  # Unsolved / budget exits 1. That is a soak datapoint — keep going.
  local out rc=0
  out=$(recertia run \
    --"${kind}" "${path}" \
    --runs-root .recertia \
    --model "${MODEL}" \
    --verifier "${VERIFIER}" 2>&1) || rc=$?
  printf '%s\n' "$out"
  echo "$(date -u +%FT%TZ) ${DAY:-} rc=${rc} --${kind} ${path} ${out}" \
    >> artifacts/soak-days.tsv
  if [[ "$rc" -ne 0 ]]; then
    echo "run finished non-zero (logged; continuing)"
  fi
}

spec_for_date() {
  local day="$1"
  local epoch start_epoch idx
  epoch=$(date -d "$day" +%s)
  start_epoch=$(date -d "$START" +%s)
  idx=$(( (epoch - start_epoch) / 86400 ))
  idx=$(( idx % ${#SPECS[@]} ))
  echo "${SPECS[$idx]}"
}

backup_now() {
  mkdir -p backups artifacts
  recertia backup --root .recertia
}

monday_ops() {
  local week="$1"
  mkdir -p artifacts backups .recertia
  recertia probes run \
    --probes evals/probes/repo-chore.json \
    --skills-root skills \
    --eval-db .recertia/evals.db \
    --output artifacts/probes.json \
    || echo "probes non-zero (continuing)"

  RECERTIA_EXECUTION_BACKEND=local recertia eval run \
    --task-class repo-chore \
    --golden-root evals/golden \
    --skills-root skills \
    --runs-root .recertia/eval-runs \
    --eval-db .recertia/evals.db \
    --snapshot-id "${week}" \
    || echo "eval run non-zero (continuing)"

  python3 scripts/weekly_metrics_report.py \
    --eval-db .recertia/evals.db \
    --output artifacts/weekly-metrics.json

  recertia canary --output artifacts/canary.json \
    || echo "canary non-zero (continuing)"

  if [[ ! -f artifacts/tabletop-2026-09-14.json ]]; then
    local archive
    archive=$(ls -1t backups/recertia-*.tar.gz 2>/dev/null | head -1 || true)
    if [[ -n "${archive}" ]]; then
      recertia tabletop 070a977fb56a \
        --runs-root .recertia \
        --restore-from "${archive}" \
        --follow-up "Home2 soak calendar ${week}" \
        --output artifacts/tabletop-2026-09-14.json \
        || echo "tabletop non-zero (continuing)"
    fi
  fi

  backup_now

  recertia soak record \
    --metrics artifacts/weekly-metrics.json \
    --probes artifacts/probes.json \
    --canary artifacts/canary.json \
    --log artifacts/soak-log.json \
    --week "${week}" \
    || echo "soak record not counted or failed (continuing)"

  recertia soak status --log artifacts/soak-log.json \
    || echo "soak status gate not ready (expected until 4 counted weeks)"
}

jobs_dry() {
  recertia jobs run curator --dry-run || true
  recertia jobs run recertify --dry-run || true
  recertia jobs run practice --dry-run || true
}

print_calendar() {
  local d="$START"
  echo "date        dow  week      block"
  echo "----------  ---  --------  -----"
  while [[ "$d" < "$END" || "$d" == "$END" ]]; do
    local u week block pair
    u=$(dow "$d")
    week=$(iso_week "$d")
    pair=$(spec_for_date "$d")
    case "$u" in
      1) block="MONDAY ops + soak record ${week}" ;;
      3) block="live ${pair} + canary --live" ;;
      5) block="live ${pair} + metrics peek (no record)" ;;
      *) block="live ${pair} + backup" ;;
    esac
    printf "%s  %s    %s  %s\n" "$d" "$(date -d "$d" +%a)" "$week" "$block"
    d=$(date -d "$d + 1 day" +%F)
  done
}

run_day() {
  local day="$1"
  local u week pair kind path
  [[ "$day" < "$START" || "$day" > "$END" ]] && die "$day out of range $START..$END"
  u=$(dow "$day")
  week=$(iso_week "$day")
  pair=$(spec_for_date "$day")
  kind="${pair%%:*}"
  path="${pair#*:}"

  echo "== $day ($(date -d "$day" +%A)) ${week}"
  DAY="$day"
  export DAY
  env_up

  case "$u" in
    1)
      monday_ops "$week"
      live_run "$kind" "$path"
      backup_now
      ;;
    3)
      live_run "$kind" "$path"
      recertia canary --live --output "artifacts/canary-live-${day}.json" \
        || echo "live canary non-zero — write it down; do not swap providers"
      jobs_dry
      backup_now
      ;;
    5)
      live_run "$kind" "$path"
      recertia canary --output "artifacts/canary-${day}.json" || true
      python3 scripts/weekly_metrics_report.py \
        --eval-db .recertia/evals.db \
        --output "artifacts/weekly-metrics-peek-${day}.json" || true
      recertia soak status --log artifacts/soak-log.json || true
      recertia gc --older-than-days 14 --dry-run || true
      backup_now
      echo "Friday: do NOT soak record. Next record is the following Monday."
      ;;
    *)
      live_run "$kind" "$path"
      backup_now
      ;;
  esac
}

main() {
  if [[ "${1:-}" == "--print" ]]; then
    print_calendar
    exit 0
  fi
  local day="${1:-$(date +%F)}"
  run_day "$day"
}

main "$@"
