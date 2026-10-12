#!/usr/bin/env bash
# Alidade 评测一键脚本（macOS / Linux / Git Bash）。Windows 推荐用 eval.ps1 或 eval.bat。
#
#   ./eval/eval.sh smoke | new | old | compare | all | report [dir] | help
#
# 配置写在 eval/.env（模板 eval/.env.example）；终端里已设置的同名环境变量优先：
#   EVAL_API_KEY（都没有时会提示输入）  EVAL_MODEL（默认 deepseek-chat）
#   EVAL_API_BASE（默认 https://api.deepseek.com）  EVAL_ENDPOINT（默认 openai）
#   EVAL_TAG  EVAL_JOBS=3  EVAL_REPEAT=2  EVAL_ONLY  EVAL_NO_JUDGE=1  EVAL_TIMEOUT=600  EVAL_USER_SIM=1  EVAL_FAST_MODEL  EVAL_STRONG_EFFORT  EVAL_FAST_EFFORT
#   LEGACY_COMMIT（默认：上游 5477f0e2，找不到就用仓库第一个提交）  LEGACY_DIR=../alidade-legacy
set -euo pipefail

EVAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$EVAL_DIR")"
RESULTS="$EVAL_DIR/results"
ENV_FILE="${EVAL_ENV_FILE:-$EVAL_DIR/.env}"
KEY_PLACEHOLDER="sk-your-key-here"

# 读取 .env：KEY=VALUE，# 开头为注释；终端里已设置的变量不覆盖（不用 source，避免执行文件内容）
load_env_file() {
  [ -f "$1" ] || return 0
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"; line="${line#$'\xef\xbb\xbf'}"
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in ''|'#'*) continue ;; esac
    [[ "$line" == *=* ]] || continue
    key="${line%%=*}"; key="${key#export }"; key="${key//[[:space:]]/}"
    value="${line#*=}"; value="${value#"${value%%[![:space:]]*}"}"; value="${value%"${value##*[![:space:]]}"}"
    if [[ "$value" =~ ^\"(.*)\"$ ]] || [[ "$value" =~ ^\'(.*)\'$ ]]; then
      value="${BASH_REMATCH[1]}"
    else
      value="${value%% #*}"; value="${value%"${value##*[![:space:]]}"}"
    fi
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [ -z "${!key+x}" ]; then export "$key=$value"; fi
  done < "$1"
}
load_env_file "$ENV_FILE"

MODEL="${EVAL_MODEL:-deepseek-chat}"
API_BASE="${EVAL_API_BASE-https://api.deepseek.com}"   # .env 里写空值 = 不用自定义地址
ENDPOINT="${EVAL_ENDPOINT:-openai}"
TAG="${EVAL_TAG:-$(printf '%s' "$MODEL" | tr -c 'A-Za-z0-9._-' '_')}"
JOBS="${EVAL_JOBS:-3}"
REPEAT="${EVAL_REPEAT:-2}"
ONLY="${EVAL_ONLY:-}"
TIMEOUT="${EVAL_TIMEOUT:-600}"
LEGACY_COMMIT="${LEGACY_COMMIT:-}"
LEGACY_DIR="${LEGACY_DIR:-$(dirname "$ROOT")/alidade-legacy}"

if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"
elif [ -x "$ROOT/.venv/Scripts/python.exe" ]; then PY="$ROOT/.venv/Scripts/python.exe"
else PY="python3"; fi
export PYTHONIOENCODING=utf-8

NEW_DIR="$RESULTS/new-$TAG"
OLD_DIR="$RESULTS/old-$TAG"
COMPARE_FILE="$RESULTS/compare-$TAG.md"

step() { printf '\n\033[36m==> %s\033[0m\n' "$*"; }

ensure_key() {
  if [ "$ENDPOINT" = "ollama" ]; then return; fi
  if [ -n "${EVAL_API_KEY:-}" ] && [ "$EVAL_API_KEY" != "$KEY_PLACEHOLDER" ]; then return; fi
  echo "没有找到 API Key。可以在 $ENV_FILE 里写 EVAL_API_KEY=...（模板：eval/.env.example）。" >&2
  read -r -s -p "请输入 $MODEL 的 API Key（不会显示，也不会保存）: " EVAL_API_KEY; echo
  [ -n "$EVAL_API_KEY" ] || { echo "没有提供 API Key。" >&2; exit 1; }
  export EVAL_API_KEY
}

run_eval() {  # run_eval <out> <package> <src|""> <only|""> <repeat> <judge 0/1>
  ensure_key
  local args=("$EVAL_DIR/run_eval.py" --env-file "$ENV_FILE" --endpoint "$ENDPOINT" --model "$MODEL" --package "$2"
              --jobs "$JOBS" --repeat "$5" --timeout "$TIMEOUT" --out "$1")
  [ -n "$API_BASE" ] && args+=(--api-base "$API_BASE")
  [ -n "$3" ] && args+=(--src "$3")
  [ -n "$4" ] && args+=(--only "$4")
  [ "$6" = "1" ] && args+=(--judge)
  case "${EVAL_USER_SIM:-0}" in 1|true|yes) args+=(--user-sim) ;; esac
  [ -n "${EVAL_FAST_MODEL:-}" ] && args+=(--fast-model "$EVAL_FAST_MODEL")
  [ -n "${EVAL_STRONG_EFFORT:-}" ] && args+=(--strong-effort "$EVAL_STRONG_EFFORT")
  [ -n "${EVAL_FAST_EFFORT:-}" ] && args+=(--fast-effort "$EVAL_FAST_EFFORT")
  "$PY" "${args[@]}"
}

judge_flag() { case "${EVAL_NO_JUDGE:-0}" in 1|true|yes) echo 0 ;; *) echo 1 ;; esac; }

resolve_legacy_commit() {
  if [ -n "$LEGACY_COMMIT" ]; then echo "$LEGACY_COMMIT"; return; fi
  # 上游 Data Formulator 0.8.0b1（克隆自 GitHub 的仓库里才有）
  if git -C "$ROOT" rev-parse --verify --quiet '5477f0e2^{commit}' >/dev/null; then echo 5477f0e2; return; fi
  # 否则用仓库最早的提交：从压缩包/复制的代码 git init 时，它就是改造前的版本
  git -C "$ROOT" rev-list --max-parents=0 HEAD | tail -n 1
}

ensure_legacy() {
  [ -d "$LEGACY_DIR/py-src/data_formulator" ] && return
  local commit; commit="$(resolve_legacy_commit)"
  [ -n "$commit" ] || { echo "找不到改造前的提交，请设置 LEGACY_COMMIT。" >&2; exit 1; }
  step "创建改造前版本的 worktree：$LEGACY_DIR ← $(git -C "$ROOT" log -1 --format='%h %s (%ad)' --date=short "$commit")"
  git -C "$ROOT" worktree add --detach "$LEGACY_DIR" "$commit"
  [ -d "$LEGACY_DIR/py-src/data_formulator" ] || {
    echo "提交 $commit 里没有 py-src/data_formulator，不是改造前的版本。请 git worktree remove \"$LEGACY_DIR\" 后设置 LEGACY_COMMIT。" >&2; exit 1; }
}

cmd_smoke()   { step "冒烟测试（2 道题）：$MODEL"; run_eval "$RESULTS/smoke-$TAG" alidade "" "gas-grade-compare,gas-ambiguous" 1 0; }
cmd_new()     { step "评测当前版本 → $NEW_DIR"; run_eval "$NEW_DIR" alidade "" "$ONLY" "$REPEAT" "$(judge_flag)"; }
cmd_old()     { ensure_legacy; step "评测改造前的版本 → $OLD_DIR"
                run_eval "$OLD_DIR" data_formulator "$LEGACY_DIR/py-src" "$ONLY" "$REPEAT" "$(judge_flag)"; }
cmd_compare() {
  for d in "$OLD_DIR" "$NEW_DIR"; do
    [ -f "$d/runs.jsonl" ] || { echo "找不到 $d/runs.jsonl，请先运行 old 和 new。" >&2; exit 1; }
  done
  step "生成对比报告 → $COMPARE_FILE"
  "$PY" "$EVAL_DIR/report.py" "$OLD_DIR" "$NEW_DIR" --labels 改造前 改造后 --out "$COMPARE_FILE"
}
cmd_report()  { "$PY" "$EVAL_DIR/report.py" "${1:-$NEW_DIR}"; }
cmd_help()    { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
                if [ -f "$ENV_FILE" ]; then echo "配置文件：已读取 $ENV_FILE"; else echo "配置文件：未找到 $ENV_FILE（可从 .env.example 复制）"; fi
                local k="未设置"; [ -n "${EVAL_API_KEY:-}" ] && [ "$EVAL_API_KEY" != "$KEY_PLACEHOLDER" ] && k="已设置"
                echo "当前配置：endpoint=$ENDPOINT model=$MODEL api-base=$API_BASE tag=$TAG API Key：$k"; }

cd "$ROOT"
case "${1:-help}" in
  smoke) cmd_smoke ;;
  new) cmd_new ;;
  old) cmd_old ;;
  compare) cmd_compare ;;
  all) cmd_new; cmd_old; cmd_compare ;;
  report) cmd_report "${2:-}" ;;
  help|-h|--help) cmd_help ;;
  *) echo "未知子命令：$1" >&2; cmd_help; exit 1 ;;
esac
