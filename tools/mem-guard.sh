#!/usr/bin/env bash
# mem-guard —— 跑测试；没过就把"这次改过的文件的已知坑"打出来。
#
# 为什么要它：记忆能不能帮上忙，取决于"检索有没有被触发"。
# 靠人自觉记着去 mem preflight，迟早忘；把触发点钉在"测试失败"这个必然发生的事件上，
# 失败的那一刻雷区自动上门。
#
#   ./tools/mem-guard.sh              # 跑全部测试
#   PYTHON=python3.11 ./tools/mem-guard.sh
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MEM="${MEM:-$ROOT/mem}"
PY="${PYTHON:-python3}"

cd "$ROOT" || exit 1
OUT="$("$PY" -m unittest discover -s tests 2>&1)"
CODE=$?

echo "$OUT" | tail -60

if [ "$CODE" -ne 0 ]; then
  echo
  echo "──── 测试没过，先对照这些文件的已知坑 ────"
  FILES="$(git status --porcelain 2>/dev/null \
    | awk '{print $2}' \
    | grep -E '\.(py|html|js|mjs|cjs|ts|tsx|yml|yaml|json|sh)$' \
    | head -5)"
  if [ -n "$FILES" ]; then
    # shellcheck disable=SC2086
    "$MEM" preflight $FILES || true
  else
    "$MEM" preflight --topic "测试失败" || true
  fi
  echo
  echo "修完记得写回去：mem learn --file <文件> -t \"一句话\" --what 现象 --fix 修法"
fi

exit "$CODE"
