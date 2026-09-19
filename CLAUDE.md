# YPC 백엔드 — 작업 규칙

청년정책 자격 판정 백엔드. 상세한 인수인계는 **`docs/HANDOFF.md`** 를 먼저 읽을 것.

## 이 코드베이스의 기본 태도

이 프로젝트에서 가장 나쁜 실패는 **틀린 답이 아니라 조용한 누락**이다.
정책이 목록에서 빠지면 사용자는 '부적격'도 '확인 필요'도 아닌 아무것도 못 본다.
틀린 판정은 신고가 들어오지만 누락은 안 들어온다.

그래서 코드 곳곳이 "의심스러우면 멈춘다"로 되어 있다. 이 성질을 약화시키는 방향의
변경(검증 건너뛰기, 실패를 경고로 낮추기, 기본값으로 메우기)은 하기 전에 이유를 확인할 것.

## 절대 깨면 안 되는 것

1. **판정 경로는 DB·LLM 을 쓰지 않는다** (ADR-001).
   `app/engine`, `app/solver`, `app/planner` 에서 `app.db` / `app.llm` 을 import 하면
   ruff 가 CI 에서 실패시킨다. 이 규칙 때문에 DB 가 죽어도 판정이 계속된다.

2. **`source_quote` 없는 룰은 저장되지 않는다.**
   DB 의 `NOT NULL` + `CHECK` 제약이다. 근거 없는 판정은 내보내지 않는다는 뜻.

3. **스냅샷 빌드는 전부 아니면 전무.**
   검증 위반이 1건이라도 있으면 빌드 전체가 멈춘다. `--allow-partial` 은 사람이
   명시적으로 선택할 때만.

4. **키 없이 프로필을 평문 저장하는 경로를 만들지 않는다.**
   `PROFILE_ENC_KEYS` 가 없으면 세션 API 가 503 이 되고, 판정은 계속된다.

5. **계약 스키마는 코드가 원본.**
   `app/schemas/*.py` 를 고쳤으면 `python tools/export_contract.py` 를 돌려
   `docs/contracts/` 를 함께 커밋한다. CI 가 드리프트를 검사한다.

## 커밋 전 체크

```bash
pytest && ruff check . && mypy && python tools/export_contract.py && git diff --exit-code docs/contracts/
```

성능 회귀는 `python bench/engine_bench.py` 가 잡는다 (기준 초과 시 CI 실패).

## 커밋 메시지

**무엇을 했는지가 아니라 왜 그렇게 했는지**를 쓴다.
특히 "다른 방법도 있었는데 왜 이걸 골랐는가"와 "안 그러면 어떻게 조용히 틀리는가"를 남긴다.
`git log` 를 읽으면 설계 판단의 근거가 나오도록 유지하고 있다.

## 브랜치

- 작업 브랜치는 `dev` 에서 분기, PR 대상도 `dev`
- `main` 직접 푸시 금지
- 팀 저장소: `Claude-MCP-7team/BE`

## 테스트

- async 테스트는 `@pytest_asyncio.fixture` 를 쓴다 (`@pytest.fixture` 는 동작하지 않음)
- DB 통합 테스트는 `DATABASE_URL` 없으면 skip 되지만 **CI 는 skip 을 실패로 처리**한다
- 검증할 값이 실제 동작에서 나온 것인지 확인할 것 — 서버를 띄워 HTTP 로 받아보는 게
  이 프로젝트의 기본 검증 방식이다

## 파일 입출력

`open()` · `read_text()` · `write_text()` 에 **반드시 `encoding="utf-8"`** 을 쓴다.
한국어 Windows 의 기본값은 cp949 라서, 생략하면 같은 코드가 개발자 기계에 따라
동작하거나 `UnicodeDecodeError` 로 죽는다. ruff `PLW1514` 가 CI 에서 잡는다.

CLI 진입점(`if __name__ == "__main__"`)에서는 `force_utf8_console()` 을 먼저 부른다.
한글을 `print` 하면 콘솔 코드페이지 때문에 `UnicodeEncodeError` 로 죽는데, 출력에는
`encoding=` 을 줄 자리가 없어서 스트림 자체를 바꿔야 한다.

cp949 환경을 흉내 내 미리 확인: `PYTHONUTF8=0 pytest`

## Windows 로컬 PostgreSQL 함정

- initdb 는 한글 경로에서 실패한다 → `C:\ypcpg\data` 같은 ASCII 경로 사용
- asyncpg 가 홈 디렉터리에서 SSL 인증서를 찾다 실패한다 → 로컬 DSN 에 `?sslmode=disable`
