#!/usr/bin/env bash
# 푸시 전에 CI 세 잡의 조건을 로컬에서 그대로 돌린다.
#
# 왜 필요한가: `pytest && ruff && mypy` 만 돌리고 푸시했다가 CI 가 5커밋 연속
# 빨간불이 된 적이 있다. 로컬 venv 에는 `[batch]` 가 깔려 있어 통과했는데,
# windows-cp949 잡은 `[dev]` 만 설치해서 anthropic SDK 가 없었다.
#
# 즉 "로컬에서 pytest 가 통과한다"는 CI 통과를 뜻하지 않는다. CI 잡마다 **다른
# 환경**을 쓰기 때문이다:
#
#   test          ubuntu · [dev,batch] · PostgreSQL · 암호화 키
#   windows-cp949 windows · [dev] 만   · DB 없음     · cp949 로캘
#   docker        이미지 빌드 후 실제로 띄워서 확인 (여기서는 안 돌린다)
#
# 안 돈 검사는 없는 검사라, 건너뛴 게 있으면 실패로 끝낸다.

set -uo pipefail
cd "$(dirname "$0")/.."

PASS=0; FAIL=0; SKIP=0
RESULTS=()

# windows-cp949 조건을 흉내 내려고 anthropic 패키지를 잠시 치운다. 아래에 트랩을
# 걸어 두지만 SIGKILL 이나 부모 프로세스가 죽는 경우에는 트랩도 못 돈다. 그러면
# venv 에 SDK 가 없는 채로 남아 "왜 갑자기 import 가 안 되지"가 된다.
# 다음 실행에서 스스로 고친다 — 사람이 원인을 추적하게 두지 않는다.
for stray in "$PWD"/.venv/lib/python*/site-packages/anthropic.hidden; do
  [[ -d "$stray" ]] || continue
  restored="${stray%.hidden}"
  if [[ ! -e "$restored" ]]; then
    mv "$stray" "$restored"
    printf '\033[33m이전 실행이 중단되어 숨겨져 있던 anthropic 을 되돌렸다.\033[0m\n'
  fi
done

step() {  # step <이름> <명령...>
  local name="$1"; shift
  printf '\n\033[1m▶ %s\033[0m\n' "$name"
  if "$@"; then
    RESULTS+=("  ok    $name"); PASS=$((PASS+1))
  else
    RESULTS+=("  실패  $name"); FAIL=$((FAIL+1))
  fi
}

skip() { RESULTS+=("  건너뜀 $1 — $2"); SKIP=$((SKIP+1)); }

# --- 1. 어디서나 같아야 하는 것 --------------------------------------------
step "린트 (역할 경계 포함)" ruff check .
step "타입 검사 (mypy strict)" mypy
step "계약 스키마 드리프트" bash -c \
  'python tools/export_contract.py >/dev/null && git diff --exit-code docs/contracts/'
step "엔진 벤치마크 (성능 회귀)" bash -c 'python bench/engine_bench.py >/dev/null'

# --- 2. test 잡: DB 와 암호화 키가 있어야 전부 돈다 -------------------------
# 없으면 세션·DB 테스트가 skip 되고 초록불이 뜬다. 그 상태로 푸시했다가 CI 에서만
# 깨진 적이 있다 (HANDOFF §5).
if [[ -n "${DATABASE_URL:-}" ]]; then
  export PROFILE_ENC_KEYS="${PROFILE_ENC_KEYS:-1:dGVzdC1vbmx5LWtleS1kby1ub3QtdXNlLTMyYnl0ZXM=}"
  step "전체 테스트 (DB 통합 포함)" bash -c \
    'out=$(pytest 2>&1); code=$?
     echo "$out" | grep -E "passed|failed|error" | tail -1; exit $code'
  # CI 와 같은 규칙: 전역 skip 은 허용한다(환경에 따라 갈리는 테스트가 있다).
  # DB 통합 테스트만은 skip 을 실패로 친다 — skip 된 채 초록불이 뜨면 DB 계층이
  # 검증되지 않은 채로 배포된다 (HANDOFF §5).
  step "DB 통합 테스트가 실제로 돌았나" bash -c \
    'out=$(pytest tests/unit/test_db_sessions.py 2>&1); code=$?
     echo "$out" | grep -E "passed|failed|error|skipped" | tail -1
     (( code == 0 )) && ! echo "$out" | grep -qE "[0-9]+ skipped"'
else
  skip "전체 테스트 (DB 통합 포함)" "DATABASE_URL 이 없다 — HANDOFF §5 의 로컬 PostgreSQL 절차"
fi

# --- 3. windows-cp949 잡: SDK 없음 + DB 없음 + cp949 ------------------------
ANT="$(python -c 'import anthropic,os;print(os.path.dirname(anthropic.__file__))' 2>/dev/null || true)"
if [[ -n "$ANT" && -d "$ANT" ]]; then
  # 스크립트가 중간에 죽어도 venv 를 망가진 채로 두지 않는다.
  trap '[[ -d "$ANT.hidden" ]] && mv "$ANT.hidden" "$ANT"' EXIT INT TERM
  mv "$ANT" "$ANT.hidden"
  step "windows-cp949 조건 (SDK·DB 없음 + cp949)" bash -c \
    'out=$(env -u DATABASE_URL -u PROFILE_ENC_KEYS PYTHONUTF8=0 pytest 2>&1)
     code=$?; echo "$out" | grep -E "passed|failed|error" | tail -1; exit $code'
  mv "$ANT.hidden" "$ANT"
  trap - EXIT INT TERM
else
  # SDK 가 애초에 없으면 그 조건이 이미 기본값이다.
  step "cp949 조건 (SDK 는 원래 없음)" bash -c \
    'out=$(env -u DATABASE_URL -u PROFILE_ENC_KEYS PYTHONUTF8=0 pytest 2>&1)
     code=$?; echo "$out" | grep -E "passed|failed|error" | tail -1; exit $code'
fi

# --- 정리 -------------------------------------------------------------------
printf '\n\033[1m결과\033[0m\n'
printf '%s\n' "${RESULTS[@]}"
printf '\n통과 %d · 실패 %d · 건너뜀 %d\n' "$PASS" "$FAIL" "$SKIP"

if (( FAIL > 0 )); then
  printf '\n\033[31m푸시하지 말 것 — CI 도 같은 이유로 실패한다.\033[0m\n'
  exit 1
fi
if (( SKIP > 0 )); then
  printf '\n\033[33m건너뛴 검사가 있다. CI 는 그것도 돌린다 — 안 돈 검사는 없는 검사다.\033[0m\n'
  exit 2
fi
printf '\n\033[32m전부 통과. 푸시해도 된다.\033[0m\n'
