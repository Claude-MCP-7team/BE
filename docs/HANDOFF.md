# 인수인계 — YPC 백엔드

> 이 문서만 읽고 이어서 작업할 수 있도록 쓴다.
> 마지막 갱신: `3904cf9` 시점.
>
> **재개할 때 이 순서로 먼저 확인할 것.** 이 문서가 오래돼서 다음 세션이
> 틀린 전제로 시작한 적이 있다 — 클론이 7커밋 뒤처져 있는데 문서를 믿고
> "동기화됨"으로 판단했고, 문서에 "CI 통과"라고 적힌 동안 CI 는 나흘째 빨간불이었다.
>
> ```bash
> git fetch origin && git log --oneline -5 && git status -sb   # 실제 HEAD·뒤처짐
> gh run list --branch dev --limit 3                           # 실제 CI 상태
> ```

---

## 0. 30초 요약

청년정책 자격 판정 백엔드. **B(룰 엔진) · D(조합 솔버) · E(일정 역산) + DB · 배치**가 역할 범위다.
프롬프트 설계는 AI 역할, 화면은 FE 역할이며 경계는 `docs/ARCHITECTURE.md` §4 에 있다.

**핵심 구조 한 줄:** 배치가 만든 정책 스냅샷을 API 가 부팅 시 RAM 에 통째로 올리고, 판정 요청은 DB 를 한 번도 건드리지 않는다 (ADR-001).

현재 **판정 · 역질문 · 조합 · 일정 · 세션 저장 · 정책 목록 · A2 구조화 · C2 설명문이 모두 동작**한다.
제출용 E2E 시나리오 6종(`tests/e2e/`)이 키 없이 돌고, 고정 데모 데이터(`data/demo/`)가 커밋되어 있다.

BE 는 배포되어 있고 (`https://be-27y9.onrender.com`), FE 계약 3건(소득 단위·경로
접두사·불필요 필드)은 2026-09-19 FE 회신으로 확정됐다 (§8 '결정된 것').

남은 것은 **코드보다 정렬**이다: FE 배포 주소(→ `CORS_ORIGINS`)와 서류 마스터 검증.
자세한 것은 §8.

---

## 1. 지금 상태

| 항목 | 값 |
| --- | --- |
| HEAD | `dev` 최신 — `git log -1` 로 확인할 것 |
| 팀 저장소 | `Claude-MCP-7team/BE` 의 **`dev`** 브랜치 |
| 배포 | `https://be-27y9.onrender.com` (Render, `render.yaml`). **데모 스냅샷 5건**으로 떠 있다 — 실데이터가 아니다 |
| CORS | **비어 있다.** FE 주소가 정해지면 Render → Environment → `CORS_ORIGINS` 에 넣는다. 그때까지 브라우저에서 호출 불가 |
| 리모트 이름 | **기계마다 다르다.** `git remote -v` 로 확인할 것 — 이 문서가 `team` 이라고 적어둔 탓에 `origin` 이 팀 저장소인 환경에서 혼선이 있었다 |
| 테스트 | **652건 통과** (DB 통합 포함, skip 0) |
| CI | 통과 (lint · **타입** · 마이그레이션 · 제약조건 · 테스트 · 계약 드리프트 · 벤치마크 · cp949 · 이미지 빌드) |

> ⚠️ 위 숫자는 갱신 시점의 값이다. **믿지 말고 직접 돌려볼 것.**

> ⚠️ `main` 브랜치는 아직 `5a991da`(README 1개)에 멈춰 있다. 작업물은 전부 `dev` 에 있다.
> `dev` → `main` 머지는 팀 합의 후 PR 로 한다.

### 로컬에서 이어받기

```bash
git clone --branch dev https://github.com/Claude-MCP-7team/BE.git ypc-backend
cd ypc-backend

python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv/Scripts/activate
pip install -e ".[dev,batch]"

pytest            # 442 passed, 28 skipped (DB 없으면 통합 테스트는 skip)
ruff check .
python bench/engine_bench.py
pytest tests/e2e -v   # 제출용 시나리오 6종 — 데모가 살아 있는지 30초 확인
```

`DATABASE_URL` 과 `PROFILE_ENC_KEYS` 가 있으면 652건 전부 돈다. 없으면 DB 통합 28건이 skip 된다 — **skip 된 걸 통과로 착각하지 말 것.**

**API 키 없이도 전 경로가 돈다.** `data/demo/` 의 고정 정책 5건이 그 바닥을 받친다:

```bash
python -m batch.build_snapshot data/demo/policies.demo.json -o data/snapshot.json
SNAPSHOT_PATH=data/snapshot.json YPC_FIXED_TODAY=2026-10-01 uvicorn app.main:app
```

---

## 2. 작업 순서 제안

의존성 순서다. **막고 있는 것은 대부분 코드가 아니라 팀 간 정렬과 외부 접근권**이라,
혼자 더 짜는 것보다 ①·③을 닫는 쪽이 남은 위험을 더 줄인다.

### ① FE 계약 — 확정됨, 남은 건 CORS 주소 하나
https://github.com/Claude-MCP-7team/FE/issues/2

**2026-09-19 FE 회신으로 세 건이 닫혔다.** 회신 내용을 코드와 대조했고 전부
BE 현재 동작과 일치해서 BE 변경은 없었다 (§8 '결정된 것' 에 표로 있다).

| | 확정 |
| --- | --- |
| 경로 접두사 | `/v1/*` — `/api/v1` 로 옮기지 않는다 |
| 소득 단위 | 기준 중위소득 대비 **퍼센트 정수** (150% → `150`) |
| `personal_income`·`employment_type` | 안 받는다 |

남은 항목 두 개는 BE 가 이미 낸 것을 FE 가 받기만 하면 된다:

- **에러 유형** — RFC 9457 로 맞췄고 `type` 으로 분기하면 된다. `detail` 은 그대로라
  기존 코드는 안 깨지지만, 같은 상태 코드의 다른 원인을 구분하려면 `type` 을 봐야 한다.
  유형 표: `docs/contracts/problems.json`.
- **네 번째 판정 상태** — BE 는 `verdict` 3값 + `future_eligible_from` 으로 냈다 (§3).
  Design 이 4상태 배지를 전제로 만들고 있으니 이 매핑을 공유해야 한다.

**실제로 막혀 있는 건 `CORS_ORIGINS` 하나다** (§8 #9). FE 공개 배포 주소가 아직 없어
CORS 가 비어 있고, 그동안은 브라우저에서 BE 를 못 부른다 (서버 간 호출은 된다).
주소가 나오면 Render → Environment 에 넣으면 끝이다.

### ② `batch/crawl` + `batch/agents` (BE-M1-4~5)
**API 는 확정됐다** (2026-09-14 실응답 기준, 상세는 `batch/collect/client.py` 도크스트링).
`/go/ythip/getPlcy` · JSON · 1,000건/페이지 · 지역은 `zipCd=41000`(법정동 5자리, 전국 정책 포함).
구 엔드포인트는 죽었고 구 지역 파라미터는 조용히 무시된다.

```bash
ONTONG_API_KEY=... python -m batch.collect.cli fetch --region 41000
python -m batch.collect.cli survey data/raw/<타임스탬프>   # 원본으로 재조사, G0 게이트 판정
```

**G0 실측 (경기 544건):** 정책 건수 ✅ · 원문 링크 단일필드 65.3% ❌ (3개 URL 필드 합집합은 79.2%) ·
수혜액 명시율 ❌ — 구조화된 수혜액 필드가 없고 `plcySprtCn` 본문 금액 패턴이 24%.
→ 링크 게이트를 합집합으로 볼지, 조합 가중치를 금액→건수로 바꿀지(Q2)는 **팀 결정** 후 크롤러 작업.
레코드는 60개 필드 전부 문자열이며 빈 값은 `""` 또는 공백(`"        "`)이다 — `.strip()` 없이 비교하면 틀린다.

**LLM 없이 실데이터 스냅샷이 나온다** (`batch/collect/normalize.py`, 코드값 의미는 그 도크스트링):

```bash
python -m batch.collect.cli fetch                       # 전국 2,774건, 3페이지
python -m batch.collect.cli normalize data/raw/<타임스탬프> -o data/policies.json
python -m batch.build_snapshot data/policies.json -o data/snapshot.json
SNAPSHOT_PATH=data/snapshot.json uvicorn app.main:app
```

실측(2026-09-14): 2,774건 전부 검증 통과, 룰 5,636개, published 1,555 / expired 914 / draft 305(원문 링크 없음).
25세 부천 사용자 → 적격 322 · 확인필요 135 · 부적격 2,317, 판정 3ms. 역질문은 3필드로 병합.
구조화된 것(지역·나이·결혼·취업·학력·기간)만 룰이 되고 소득·서류·중복수혜는 `needs_review_fields` 에 남는다 — A2(LLM) 몫.

엔진·API 는 `status` 를 보지 않으므로 **빌더가 published 만 내보낸다** (리포트에 상태별 건수 표시).
스냅샷은 1,555건 · 룰 3,253개. 같은 사용자 → 적격 134 · 확인필요 74 · 부적격 1,347.
처음 이 필터를 적용할 때는 직전 스냅샷 대비 44% 급감이라 `--force` 가 필요하다 (가드가 맞게 동작한 것).

**A2(LLM 구조화)가 생겼다** (`batch/agents/`, 프롬프트는 `app/llm/prompts/a2_structure.md` — AI 역할 소유):

```bash
pip install -e ".[batch]"                                                 # anthropic SDK
python -m batch.agents.cli structure data/raw/<타임스탬프> --dry-run          # 대상·글자수만
ANTHROPIC_API_KEY=... python -m batch.agents.cli structure data/raw/<타임스탬프> -o data/policies.json --limit 5
python -m batch.build_snapshot data/policies.json -o data/snapshot.json    # 이후는 동일
```

normalize 가 만든 PolicySchema 를 **base** 로 받아, 자유 텍스트(`*Cn` 필드 전부 + `--announcements` 원문)에서
소득 비율·연속 거주 개월·재직 개월·가구원 수·유사사업 참여 제외·서류·중복수혜·금액·담당부서를 얹는다.
`--limit`/`--ids` 밖의 정책은 base 그대로 출력되므로 빌더 입력은 항상 전체 집합이다.

무엇을 믿고 무엇을 버리는지 (`batch/agents/structure.py` 도크스트링이 원본):

- **인용문이 원문의 연속 구간이 아니면 그 항목은 통째로 버린다** (공백 차이만 허용). 모델이 문장을 다듬은 경우다.
  버린 항목은 `docs/a2/a2-report-*.json` 에 `QUOTE_NOT_VERBATIM` 으로 남고 그 필드는 `needs_review_fields` 로 간다.
- **API 코드 룰과 텍스트가 다르게 말하면 어느 쪽도 확정하지 않는다** — API 룰 유지 + `needs_review_fields` + 리포트 `disagreements`.
  (실제로 `sprtTrgtMaxAge=39` 인데 본문이 "34세 이하"인 정책이 있다. 이 불일치가 검토 큐의 1순위다.)
- 모델은 `region_code`·`received_policy_ids` 룰을 만들 수 없다 (스키마 enum 에서 제외). 지역은 API 가 권위, 정책 ID 는 모델이 모른다.
- 표로 못 옮기는 조건(무주택·세대주·자산·원 단위 소득 등)은 `unrepresentable_conditions` 로 받아 리포트에 남기고
  `needs_review_fields` 에 `"unrepresentable_conditions"` 표식을 넣는다. **엔진은 아직 이 표식을 읽지 않는다** —
  그런 정책이 ELIGIBLE 로 나올 때 confidence 를 낮출지는 BE 결정 사항 (§8 미결 #10).
- `askable` 룰의 `question_template` 은 모델 문구 → 없으면 `batch/agents/questions.py` 표준 문구. 필드별 병합(G3) 때문에
  같은 필드는 같은 문구가 낫다.
- 응답은 `data/a2-cache/` 에 (프롬프트+텍스트) 해시로 캐시된다. 프롬프트를 고치면 자동으로 다시 호출된다.
- 아직 **실제 Anthropic 키로 돌려보지 못했다.** 테스트 21건은 가짜 LLM 으로 검증·병합 로직만 본다.
- 실데이터(2026-09-19 수집, 2,819건)로 텍스트 쪽은 확인했다: `plcyExplnCn`·`plcySprtCn` 은 전건, `sbmsnDcmntCn` 962·
  `addAplyQlfcCndCn` 926·`ptcpPrpTrgtCn` 667·`earnEtcCn` 336건. 목록에 없던 `bizPrdEtcCn`(1,185건)은 `*Cn` 자동 포착으로 들어온다.
  텍스트 길이 중앙값 410자, 최대 3,673자 — 정책당 1회 호출로 충분하다.
  published 1,596건 중 키워드 빈도: 중위소득 118 · 무주택 67 · 유사사업 45 · 기초수급 41 · 중복수혜 40 · 세대주 19 · 1인가구 19 ·
  거주 N개월 17 · 재직 N개월 16. 즉 A2 가 새로 만들 룰의 대부분은 **소득 비율**이고, 무주택·재산은 규칙화 불가로 남는다 — 이 둘을
  판정 confidence 에 어떻게 반영할지가 §8 #10 의 무게다.

**C2 설명문도 생겼다** (`app/llm/explain.py`, 프롬프트 `app/llm/prompts/c2_explain.md`):

- `POST /v1/judge` 가 기본으로 결과마다 `explanation` 을 채운다 — **결정론 템플릿**이라 LLM 없이 항상 동작하고 비용이 없다.
  미충족은 "내 값 / 공고 기준 / 인용문 / 언제부터 되는지(또는 영구 불가)"를, 확인 필요는 질문을 그대로, 추정 판정은 담당부서 전화를 붙인다.
- `?explain=llm` 이면 결과 전체를 **한 번의 호출**로 다듬는다. 설명 속 숫자가 결과·초안에 없으면 그 항목은 템플릿으로 되돌린다
  (금액·기간·조건을 지어내는 것이 C2 의 유일한 실패 모드다, PRD §23). 키가 없으면 `template` 과 같다. `?explain=none` 은 생략.
- ETag 에 explain 모드가 들어간다. `app/api/v1/judge.py` 를 손댄 유일한 이유다. 첫 실행은 `--limit 5` 로
  하고 리포트의 `rejected`/`disagreements` 를 읽어 프롬프트를 손보는 것이 AI-M2-3(수동 정답표 대조)이다.

### ③ 서류 마스터 — 21/36 검증됨, 유효기간은 통째로 남았다

**2026-09-20 검증 결과가 반영되어 있다.** 36종 중 21종의 소요일·수수료를 실제
민원안내 페이지에서 확인했고, **CSV 값이 틀렸던 6행을 고쳤다.**

| 수정 | 내용 |
| --- | --- |
| D007 본인서명사실확인서 | 방문 수수료 600 → **0** (2028-12-31까지 면제) |
| D011 납세증명서(국세) | 방문 1000 → **0** |
| D012 지방세 납세증명서 | 방문 800 → **0** |
| D027 출입국 사실증명 | 방문 0 → **2000** |
| D025 병적증명서 | 소요 최대 0 → **1** |
| D006 인감증명서 | 소요 최대 1 → **0** |

**수수료가 반대로 틀린 행이 4건이었다.** 전부 `확인필요` 라서 화면에 "[마스터 미검증]"
이 붙어 있던 행이다 — 링크 규칙이 없었으면 그대로 확정으로 나갔다.

#### 검증은 두 축이다 (중요)

정부24 민원안내 페이지는 **유효기간을 적지 않는다.** 소요일·수수료만 확인된다.
그런데 유효기간은 "너무 일찍 떼면 제출일에 만료된다"는 **하한**을 정한다
(`backplan.py`). 납세증명서의 30일이 틀리면 준비 시작일이 반대로 어긋난다.

그래서 `DocumentSpec` 에 축이 둘이다:

| | 뜻 | 현재 |
| --- | --- | --- |
| `verified` | 소요일·수수료를 페이지에서 확인 | 21/36 |
| `validity_grounded` | 유효기간에 근거(법령 조문)가 있다 | **0/36** |
| `is_fully_grounded` | 둘 다 — 화면의 '추정치' 딱지가 떨어지는 조건 | **0/36** |

`유효기간_근거` 컬럼이 비어 있는 한 화면은 계속 추정치라고 말한다. 소요일만 확인하고
딱지를 떼면 근거 없는 30일이 검증된 값처럼 보인다 — 가장 조용한 종류의 오류다.
→ **다음 사람이 할 일**: 법령 조문으로 유효기간 근거 확보 (예: 국세징수법 시행령상
납세증명서 유효기간). 근거를 적으면 그 행부터 딱지가 떨어진다.

#### 확인완료로 올리는 조건 (`검증유형` 별로 다르다)

"아직 검증 안 함"과 "검증할 출처가 원래 없음"을 구분한다. 뭉개 두면 다음 사람이
재직증명서의 정부 페이지를 찾으러 다닌다 — 그런 페이지는 없다.

| 검증유형 | 대상 | `확인완료` 조건 |
| --- | --- | --- |
| `공적출처` | D001~D031 (31종) | 구체 페이지 링크 + `근거문구` + `검증일` |
| `기관자율` | D032·D033 재직·퇴직증명서 | 링크 면제. 대신 **소요일이 범위**여야 한다 |
| `본인보관` | D034~D036 계약서·통장 사본 | 링크 면제. **소요일 0** |

**`근거문구` 는 링크와 별개로 필요하다.** 링크가 있어도 그 페이지가 CSV 값을
말해주지 않을 수 있다. 실제 사례가 **D020(대학 졸업증명서)** 이다 — 원본에서 유일하게
딥링크를 갖고 있던 행인데, 그 페이지는 처리기간을 '즉시'라고 적어 CSV 의 1~3일과
어긋났고 수수료는 금액 자체를 적지 않았다. 링크 존재만 요구하면 이 행이 통과한다.
페이지 문구를 옮겨 적게 하면 CSV 와 다를 때 눈에 띈다.

`tests/unit/test_document_master.py` 가 위 조건을 전부 강제한다.

#### 아직 `확인필요` 인 15행

| 이유 | 행 |
| --- | --- |
| **값이 근거와 충돌 — 값부터 고쳐야** | D018 국민연금(페이지는 3일, CSV 0일) · D020·D022·D023 대학 제증명(페이지 '즉시', 수수료 금액 미표기) |
| **D007 — 로그 내부 모순** | 검증로그가 '즉시'라고 인용했는데 소요max=1 로 남아 있다. D006 은 같은 문구로 1→0 을 고쳤다. 둘 중 하나가 틀렸다 |
| 딥링크는 확보, 본문 확인 미완 | D016 건강보험 자격확인서 · D019 4대보험 가입내역(발급 중단 이력 있음 — 가용성 리스크) |
| 공식 근거 페이지 미확보 | D013 원천징수영수증(홈택스 직접) · D030 등기사항증명서(**CSV 700원은 '열람' 수수료로 보임 — '발급'은 별도**) · D031 확정일자 부여현황 |
| 출처가 존재할 수 없음 | D032~D036 (위 표의 `기관자율`·`본인보관` 조건으로 승격 가능) |

**마스터에 없는 실재 서류 2종** (실공고에서 발견): `지방세(재산세)미과세증명서`,
`본인신용정보조회서`.

**DB 쪽 잠재 충돌 2건** (지금은 마스터를 DB 에 넣는 코드가 없어 드러나지 않는다):
`document.source_ref` 가 `NOT NULL` 인데 D032~D036 은 출처가 없고, `verified_at` 도
`NOT NULL` 이다 — 이제 CSV 에 `검증일` 컬럼이 생겼으니 그걸 쓰면 된다.

### ④ 타입 검사 — 이제 CI 가 돌린다
`mypy strict` 가 CI 의 `test` 잡에 들어갔다. 0 을 유지한다.

```bash
mypy   # files = ["app", "batch"], strict = true
```

**`pip install -e ".[dev,batch]"` 로 설치할 것.** `app/llm/client.py` 의 `TYPE_CHECKING`
블록이 `anthropic.types` 를 참조해서, `[dev]` 만 깔면 그 두 줄이 `import-not-found` 로
뜬다 — 코드 문제가 아니라 설치 범위 문제다.

`asyncpg` 는 타입 정보를 배포하지 않아 `pyproject.toml` 에 **그 모듈만** 예외를 뒀다.
전역으로 풀면 오타 난 import 까지 조용히 통과한다.

### ⑤ 제출물 (M5)
- **배포** — `Dockerfile`·`render.yaml`·CORS 준비됨. `docs/DEPLOY.md` 참고.
  남은 것은 실제로 띄우고 URL 을 FE 에 주는 것 (§8 #9)
- **E2E 는 있다** — `pytest tests/e2e` 가 시나리오 6종을 돌린다. 발표 대본이기도 하다
- **데모 데이터도 고정돼 있다** — `data/demo/`. 기준일은 `YPC_FIXED_TODAY=2026-10-01`

---

## 3. 반드시 알아야 할 설계 결정

코드를 고치기 전에 이것부터 읽어야 한다. **전부 "이렇게 안 하면 조용히 틀린다"** 는 이유로 그렇게 되어 있다.

### ADR-001 — 판정은 DB 를 쓰지 않는다
`app/engine`, `app/solver`, `app/planner` 는 `app.db` 와 `app.llm` 을 **import 할 수 없다.** ruff 의 `flake8-tidy-imports.banned-api` 규칙이 CI 에서 강제한다.

이 규칙 덕분에 DB 가 죽어도 판정은 계속된다. `/readyz` 의 준비 판정 기준도 **스냅샷뿐**이다 — DB 가 죽었다고 503 을 내면 로드밸런서가 판정 가능한 인스턴스를 빼버린다.

### 조용한 누락이 틀린 답보다 나쁘다
스냅샷 빌더(`batch/build_snapshot.py`)가 **위반 1건이라도 있으면 빌드 전체를 세우는** 이유다.
600건 중 3건을 빼고 내보내면 사용자는 그 정책이 '없는' 것으로 본다 — 부적격도 확인필요도 아닌 무(無)로. 틀린 판정은 신고라도 들어오지만 누락은 안 들어온다.

빌더가 막는 것 4가지: 근거 없는 룰 · `policy_id` 중복 · 빈 스냅샷 · 직전 대비 30% 이상 급감.

### `source_quote` 100% 는 DB 제약이다
문서상 약속이 아니라 `NOT NULL` + `CHECK(length(trim())>0)` 이다. 근거 없는 룰은 INSERT 자체가 실패한다. 조건 배열의 모든 원소에 `source_quote` 가 있다고 가정해도 된다.

### 서류 소요일은 병렬이라 max 를 쓴다 (합이 아님)
등본(즉시) + 소득증명(3일) + 가족관계증명(즉시) = 4일이 아니라 **3일**이다. 한 번 가서 셋을 같이 신청하고 가장 오래 걸리는 하나를 기다린다.
합으로 계산하면 모든 정책이 동시에 '급함'으로 떠서 **무엇이 진짜 급한지 구별이 사라진다.**

### 유효기간 — 너무 일찍 떼도 틀린 계획이다
납세증명서는 30일이다. 마감 두 달 전에 떼면 제출일엔 만료된 종이다.
그래서 계획이 마감이 아니라 **구간**이다: `recommended_start_date`(언제까지) + `issue_not_before_date`(언제 이후에).
같은 서류를 여러 정책이 쓰는데 마감 간격이 유효기간보다 넓으면 `single_issue_covers_all: false` 로 알린다.

### 공휴일표는 계산하지 않고 적어둔다
음력으로 설·추석을 유도하는 코드는 그럴듯하지만 **임시공휴일이 끼는 순간 조용히 틀린다** (2025년 대선일, 1/27 등). 대체공휴일 규칙도 해마다 바뀐다.
그래서 2025~2026년을 전수로 싣고, 표 밖 연도는 `outside_calendar_coverage: true` 로 표시해서 확정값인 척하지 않는다.
`batch/holidays.py` 가 한국천문연구원 API 로 표를 갱신하되, **고정 공휴일 8개가 하나라도 빠지면 그 해 전체를 거부**한다.

### 영업일 산술은 색인이다 (하루씩 훑지 않는다)
처음 구현이 3,000 정책에서 300ms 였다(목표 50ms). prefix sum 색인으로 **10ms** 가 됐다.
빠른 경로가 조용히 하루씩 어긋나는 게 이 모듈이 막으려는 실패 자체라서, **2025~2026 전 구간을 하루씩 훑는 기준 구현과 전수 대조**하는 테스트가 붙어 있다. 여기 손대면 그 테스트를 꼭 확인할 것.

### 프로필 암호화 — 평문이 나갈 경로가 없다
`user_session` 에 평문 프로필 컬럼이 **존재하지 않는다.** AES-256-GCM 이며:
- **GCM 인 이유는 무결성.** 일반 모드는 변조된 암호문도 복호화에 '성공'하고 쓰레기를 낸다. 그게 JSON 파싱을 통과하면 가짜 프로필로 판정해놓고 아무도 모른다.
- **세션 ID 를 AAD 로 묶는다.** 안 묶으면 A 세션 암호문을 B 세션 행에 옮겨도 키가 같아 복호화가 된다.
- **nonce 는 매번 새로 뽑고 호출자가 넘길 수 없다.** 재사용하면 GCM 인증이 무너진다.
- **키가 없으면 저장 기능을 끈다.** 평문 폴백을 만들면 설정 실수 한 번으로 개인정보가 평문으로 쌓인다.
- 통계 컬럼은 평문이지만 **생년(연도만) · 시도 2자리 · 취업상태**뿐이다. 개인을 특정할 수 없다.

### 질문은 정책별이 아니라 필드별로 병합된다
세 정책이 소득을 물으면 질문은 **1개**고 `source_policy_ids` 에 셋이 담긴다. 중복 질문 0건이 게이트 G3 다.
그래서 `question_id` 가 없고 **`field` 가 키**다. 답변은 `{"answers": {"<field>": value}}` 로 `/v1/judge` 에 다시 보내면 된다.

화면 문구("이 답변으로 N개 완료")에는 **`resolves` 만** 써야 한다. `affects` 를 쓰면 미확인이 2개 남은 정책까지 세어 "5개 완료" 해놓고 3개만 끝난다.

### 조합은 2안을 모두 낸다
공고문이 "동일 목적의 타 사업과 중복 수혜 불가"라고만 쓰고 '동일 목적'을 정의하지 않는다. 한쪽으로 단정하면 사용자가 손해를 본다 — 지키면 받을 걸 놓치고, 무시하면 반려된다.
그래서 보수/최대 둘 다 계산하고 제외된 정책마다 **원문 인용 + 담당부서 전화번호**를 붙인다.

### 에러는 상태 코드가 아니라 유형으로 구분한다

FastAPI 기본 에러(`{"detail": "..."}`)는 FE 에게 상태 코드밖에 주지 않는데, 같은 코드가
다른 뜻인 경우가 있다:

| 상태 | `type` | 뜻 |
| --- | --- | --- |
| 503 | `/problems/snapshot-not-ready` | 아직 아무것도 안 된다. 잠시 후 다시 |
| 503 | `/problems/session-store-unavailable` | **저장만** 안 된다. 판정은 정상이다 |

뒤쪽에 "잠시 후 다시 시도하세요"를 띄우면, 멀쩡히 쓸 수 있는 기능을 앞에 두고 사용자를
돌려보내게 된다. 그래서 RFC 9457 `application/problem+json` 으로 내보내고 `type` 에
기계가 읽는 식별자를 싣는다. 유형 표는 `docs/contracts/problems.json` 이고 코드가
원본(`app/core/problem.py`)이라 CI 드리프트 검사가 따라온다.

**`detail` 은 자리를 그대로 유지한다.** RFC 9457 에도 `detail` 멤버가 있어서, FastAPI
기본형에서 `body.detail` 을 읽던 FE 코드는 규격이 바뀌어도 계속 동작한다. 형식을 바꾸면서
소비자를 깨뜨리지 않는 드문 경우라, 이것이 이 형식을 고른 이유 중 하나다.

`type` 은 상대 URI(`/problems/<code>`)다. RFC 9457 이 허용하고, 배포 주소가 정해지지 않은
상태에서 절대 URI 를 박으면 주소가 바뀔 때 계약이 따라 깨진다.

처리되지 않은 예외는 예외 문자열을 내보내지 않는다 — 경로·쿼리·내부 상태가 섞여 나올 수
있고, 그걸 읽는 사람이 사용자라는 보장이 없다. 로그에만 전문이 남는다.

### 마감 여부는 status 가 아니라 날짜로 판단한다

빌더가 `status != "published"` 로 거르는 것과 별개로, **`period.apply_end` 가 지났으면
status 가 뭐라고 적혀 있든 뺀다.**

status 는 생산자가 채우는 값이라 틀릴 수 있고 실제로 틀렸다. 수집기가
`aplyPrdSeCd=0057003`(마감 코드)만 expired 로 적어서, 기간제 정책은 종료일이 지나도
published 로 남았다. 국토부 청년월세(5/29 마감)가 9/19 판정에서 적격으로 나오고
조합 추천에 480만원으로 들어갔다 — 사용자가 받을 수 없는 돈이 추천 총액에 섞인다.

`apply_end` 는 공고문에서 온 날짜다. 그 날짜가 지났는지 빌더가 직접 보는 편이,
모든 생산자가 status 를 정확히 채우기를 바라는 것보다 확실하다. `normalize` 도
같은 판단을 해서 status 자체를 바로잡는다 — 리포트와 관리자 큐를 읽는 사람이
status 를 믿고 엉뚱한 곳을 보지 않도록.

빼지 않는 경우: 상시모집, 마감일 없음, **날짜를 못 읽는 경우**. 마지막은 경고만
남기고 남긴다 — 형식 오류 하나가 정책 하나의 실종이 되면 안 된다.

기준일은 `app/core/clock.py` 의 `today_kst()` 하나다. 판정과 빌더가 각자 오늘을
계산하면 자정 근처에서 '적격인데 스냅샷에 없음'이 생긴다.

### 네 번째 판정 상태는 verdict 가 아니라 날짜다

마일스톤은 PASS / FAIL / UNKNOWN / **FUTURE_PASS** 네 상태를 말하는데 `Verdict` 는
세 값이다. 늘리지 않은 이유: verdict 는 "오늘 자격이 있는가"이고 FUTURE_PASS 는
"언제부터인가"라, 한 필드에 섞으면 FE 가 그 값을 **필터로 쓸지 배지로 쓸지** 정할 수
없게 된다. 대신 신호를 따로 준다.

- `JudgementResult.future_eligible_from` — 가능해지는 날 (없으면 `null`)
- `summary.future_eligible` — 배지 건수. **부적격의 부분집합**이라 총계에 더하면 안 된다

화면은 이렇게 그린다:

| verdict | future_eligible_from | 배지 |
| --- | --- | --- |
| `ELIGIBLE` | — | 신청 가능 |
| `NEEDS_INFO` | — | 확인 필요 |
| `INELIGIBLE` | 날짜 있음 | **FUTURE_PASS** (그 날짜 표시) |
| `INELIGIBLE` | `null` | 부적격 |

**기본 응답(`include=default`)에서 빼는 것은 '영영 안 되는' 부적격뿐이다.** 원래
부적격을 전부 뺀 이유는 응답 크기였는데, 시간이 지나면 가능한 정책은 그 범주가
아니다 — 사용자가 지금 행동을 정하는 데 쓰는 정보다.

### 날짜는 약속이라, 약속할 수 없으면 주지 않는다

`future_eligible_from` 은 다음 중 하나라도 걸리면 `null` 이다:

- 미충족 중 시간과 무관한 조건이 있다 (소득은 기다린다고 해결되지 않는다)
- 영구 불가가 있다 (연령 상한 초과)
- **미확인 조건이 남아 있다** — 오늘 답을 모르는데 그날 적격이라고 할 수 없다
- **그날 다른 조건이 깨진다** — 이게 제일 잡기 어렵다

마지막이 실제로 있었다: 25세 사용자에게 거주 36개월을 요구하면서 연령 상한이
26세인 정책은, 거주 요건을 채우는 2029년에 이미 28세다. 미충족 조건만 보면
날짜가 나오고 사용자는 3년을 기다렸다 반려된다. `timeline.py` 는 **한 룰 안에서**
하한을 넘다 상한을 지나치는 경우만 막는다 (룰 단위로 호출되니까). 그래서 정책을
조립하는 곳에서 **그 날짜로 전 조건을 다시 평가**한다 — 만료일을 따로 계산하면
한쪽만 고쳐질 때 조용히 어긋난다.

또 하나: 날짜를 낼 때는 `quality.needs_review_fields` 가 다시 신뢰도를 낮춘다.
"오늘 안 된다"는 못 본 조건이 있어도 유효하지만, "그날 된다"는 얼마든지 뒤집힌다.

### 못 본 조건이 있으면 확정으로 내보내지 않는다

A2 가 룰로 옮기지 못한 조건(무주택·세대주·보증금 등)은
`quality.needs_review_fields` 에 남는다. 엔진이 그 조건을 **평가하지 않았는데**
`CONFIRMED` · `ELIGIBLE` 로 내보내면, 사용자는 그 조건 때문에 반려될 수 있다는 걸
모른 채 서류를 준비한다.

그래서 confidence 를 `NEEDS_REVIEW` 로 낮춘다. 그러면 `validate.py` 규칙이 담당부서
연락처를 강제하므로 "확인 필요"가 **확인할 수단**과 함께 나간다. `JudgementResult.
needs_review_fields` 로 무엇을 못 봤는지도 함께 준다 — confidence 만 낮추면 사용자가
뭘 확인해야 할지 모른다.

**단순 부적격에는 적용하지 않는다.** 조건은 전부 충족해야 하는 관계라, 확인 못 한
조건이 더 있다고 해서 이미 확인된 미충족이 뒤집히지 않는다. 명확한 탈락에 '확인
필요'를 붙이면 진짜 확인이 필요한 판정과 구별이 사라진다.

### 금액은 '있다'와 '확정이다'가 다르다

`amount_estimated` 는 원래 `estimated_total_krw is None` 이었다. 그러면 A2 가
월액 × 개월로 **계산한** 총액이 공고에 적힌 금액과 화면에서 똑같아 보인다. 지금은
`benefit.amount_confidence != "CONFIRMED"` 도 추정으로 표시한다.

`amount_confidence` 의 기본값이 `ESTIMATED` 라는 점에 주의 — 생산자가 명시하지
않으면 추정으로 취급된다. 보수적인 쪽이 기본값이어야 맞다.

### exclusions 는 '통과하려면 참이어야 하는 형태'로 뒤집어 쓴다

엔진은 `eligibility` 와 `exclusions` 를 **똑같이** 평가한다 — 두 배열 모두 "모든 룰이
충족되어야 적격"이고, `kind` 는 기록만 된다. 그래서 "최근 2년 내 유사사업 참여자
제외"는 `similar_program_participation_2y == false` 로 적는다.

`== true` 로 적으면 엔진은 '참여한 적이 있어야 통과'로 읽어 **판정이 정확히
뒤집히는데**, 스키마도 DB 제약도 이걸 잡지 못한다. 예외도 경고도 없다. 그래서 이
규칙은 `PolicySchema` 도크스트링에 있고 `docs/contracts/policy_schema.json` 에 실려
나간다 — 프롬프트에만 적으면 그 프롬프트를 쓰는 생산자에게만 닿는다.

엔진이 스스로 부정하게 만들면 안 된다. 이미 부정형으로 들어온 룰이 두 번 뒤집힌다.

### 예외는 잡으려는 것의 **부모**를 확인하고 잡는다

이 저장소에서 같은 실수가 세 번 났다. 전부 "자식 클래스만 잡아서 부모가 빠져나간"
경우다. 증상은 매번 **500 인데 원인은 잘못된 입력**이었다.

| 잡던 것 | 실제로 날아온 것 | 결과 |
| --- | --- | --- |
| `fastapi.HTTPException` | `starlette.exceptions.HTTPException` (부모) | 루트 404 가 `{"detail":"Not Found"}` |
| `LLMError` | `APITimeoutError` 등 SDK 계층 | 판정 응답 전체가 500 |
| `msgspec.ValidationError` | `msgspec.DecodeError` (부모) | POST 5개가 깨진 JSON 에 500 |

```python
issubclass(msgspec.ValidationError, msgspec.DecodeError)  # True
```

**5xx 가 나쁜 이유는 FE 가 자기 버그를 BE 장애로 읽기 때문이다.** 잘못된 요청은
"네가 보낸 게 틀렸다"(4xx)여야 고칠 사람이 고친다. 발표 중에 나오면 서버가 죽은
것처럼 보인다.

**디코드는 `app/api/decode.py` 한 군데서만 한다.** 엔드포인트마다 try/except 를 쓰면
다음에 추가하는 사람이 둘 중 하나를 빠뜨린다. `judge.py`·`sessions.py` 가 본문을 직접
디코드하지 않는지 테스트가 검사한다.

두 실패는 구분해서 내보낸다 — FE 가 고칠 곳이 다르다:

| type | 뜻 | FE 가 볼 곳 |
| --- | --- | --- |
| `invalid-request` | 본문이 JSON 이 아니다 | 직렬화·전송 |
| `invalid-profile` | JSON 은 맞는데 값이 틀렸다 | `detail` 이 필드명을 준다 |

### 전 엔드포인트 훑기 (`tests/unit/test_endpoint_sweep.py`)

마일스톤 M5 의 "API 누락·500 Error 점검"을 사람이 한 번 하고 끝내지 않기 위한
테스트다. 손으로 훑으면 그 시점의 엔드포인트만 보게 된다.

- **라우트를 OpenAPI 스키마에서 읽는다.** `app.routes` 는 못 쓴다 — 이 FastAPI 버전은
  include 한 라우터를 `_IncludedRouter` 로 감싸 두고 펼치지 않아서, 훑기가 조용히
  0건을 검사하게 된다. 그래서 "훑을 대상이 실제로 있다"는 단언을 따로 뒀다.
- **기준은 '5xx 금지'가 아니라 '미처리 예외 금지'다.** 503 `session-store-unavailable`
  처럼 **선언된** 5xx 는 정상이다 — 무슨 일인지 FE 에 말하고 있으니 처리된 것이다.
  `internal-error` 는 catch-all 이 마지막에 붙이는 딱지라, 그게 보이면 아무도 그
  입력을 예상 못 했다는 뜻이다.
- `POST /v1/sessions` 의 빈 본문 201 은 정상이다 (프로필 없는 익명 세션 발급).
  `EMPTY_BODY_OK` 에 명시했고, 목록이 낡으면 테스트가 잡는다.

**CI 는 `PROFILE_ENC_KEYS` 를 준다.** 없으면 `/v1/sessions` 가 저장소 없음(503)으로
짧게 끝나 그 엔드포인트 훑기가 전부 skip 된다. 안 도는 테스트는 없는 테스트다.
`DATABASE_URL` 을 주는 이유와 같다.

### 요청 본문은 문서에 직접 붙여 넣는다 (`app/api/schema.py`)

본문을 Pydantic 파라미터가 아니라 `await request.body()` + msgspec 으로 읽는다.
내부 모델이 msgspec 이라 변환이 한 번 줄고 오류 메시지도 필드 경로까지 나오는데,
대가가 하나 있었다 — **FastAPI 가 본문을 모른다.** `/docs` 에 POST 엔드포인트가
'본문 없는 엔드포인트'로 그려졌고, 스키마로 클라이언트를 만들면 빈 요청을 보냈다.
에러가 아니라 **문서가 틀린 것**이라, 연동하는 쪽이 한참 헤맨 뒤에야 안다.

디코드 방식은 그대로 두고 스키마만 바로잡았다. `openapi_extra=profile_body()` 로
라우트에 본문을 선언하고, `install_request_schemas(app)` 이 msgspec 컴포넌트를
OpenAPI 문서에 합친다. **런타임 동작에는 영향이 없다** — 문서 생성에만 관여한다.

- `msgspec.json.schema()` 가 아니라 `schema_components(ref_template=...)` 를 쓴다.
  전자는 `$defs` 를 쓰는데 OpenAPI 는 `#/components/schemas/` 를 본다. 맞춰 주지
  않으면 `/docs` 가 참조를 못 찾아 본문을 **빈 객체로** 그린다 — 증상이 '본문이
  없다'에서 '본문이 비어 있다'로 바뀔 뿐 고쳐진 게 아니다.
- `required` 는 라우트마다 정한다. `POST /v1/sessions` 는 본문 없이 부르면 프로필
  없는 익명 세션이 발급되므로 `required=False` 다. 전부 필수로 적으면 문서가 또
  틀린다 — 방향만 반대일 뿐 같은 종류의 거짓말이다.

**테스트는 '스키마가 붙어 있다'가 아니라 '문서대로 보내면 통과한다'를 본다.**
엉뚱한 모델을 가리켜도 붙어는 있기 때문이다 (`tests/unit/test_endpoint_sweep.py`):

| 검사 | 막는 것 |
| --- | --- |
| 데모 프로필(전 필드)이 200 | 문서가 선언한 필드를 서버가 거부하는 경우 |
| 문서의 `Core` 필드 == 모델 필드 | 한쪽만 바뀌어 FE 가 없는 필드를 보내는 경우 (→ 전체 422) |
| `required` 를 빼면 422 | 필수를 선택으로 잘못 적어 FE 가 안 보내는 경우 |


### 설명문이 실패해도 판정은 나간다

`explain_all()` 은 LLM 호출에서 **무엇이 터지든** 템플릿 설명문으로 떨어진다
(`except Exception`). 한동안 `LLMError` 만 잡았는데, 그건 `app/llm/client.py` 가 직접
던지는 것(거부·토큰 상한·JSON 파싱 실패)뿐이다. 정작 흔한 실패인 SDK 의 타임아웃·연결
끊김·레이트리밋은 `LLMError` 가 아니라서 그대로 빠져나갔고, **이미 계산이 끝난 판정
응답 전체가 500** 이 됐다. 설명문은 덤이고 템플릿이라는 대안이 항상 있다.

`except BaseException` 으로 넓히면 안 된다 — KeyboardInterrupt·SystemExit 까지 먹으면
서버를 못 끈다. 테스트가 양쪽을 다 잡는다 (`tests/unit/test_llm_failure.py`).

실패는 삼키지 않고 `log.warning(..., exc_info=True)` 로 남긴다. 로그가 없으면 LLM 이 한
번도 성공하지 않는 배포를 아무도 눈치채지 못한다 — 템플릿이 그럴듯해서 화면은 멀쩡하다.

**요청 경로와 배치 경로는 클라이언트 설정이 다르다** (`app/llm/client.py`):

| | 타임아웃 | 재시도 | 이유 |
| --- | --- | --- | --- |
| 요청 (`get_llm()`) | 8초 | 0 | 워커 1개짜리 배포다. 하나가 LLM 을 기다리면 남의 판정까지 멈춘다 |
| 배치 (`AnthropicLLM()`) | SDK 기본 (10분) | SDK 기본 (2) | 수백 건을 돌리므로 레이트리밋 재시도가 이득 |

SDK 기본값을 그냥 두면 **재시도마다 타임아웃을 다시 쓰기 때문에 한 호출이 최악 30분**을
붙잡는다 (10분 × 3). 요청 경로에서 재시도를 0으로 둔 건 최악 wall-clock 을 타임아웃
그 자체로 묶기 위해서다.

### 역질문 답변은 '메우기'지 '덮어쓰기'가 아니다

`UserProfile.resolve()` 의 규칙: **온보딩에서 받은 값이 우선이고, 역질문 답변은
그 값이 없을 때만 쓴다.** 근속·거주 개월수(`_counted_or_answered`)도 같다 — 시작일이
있으면 거기서 센 값을 쓰고, 없을 때만 답변을 본다.

한동안 이 두 필드는 답변을 **아예 보지 않았다.** 질문은 나가는데
(`_ANSWER_TYPES` 에 있다) 답변이 버려져서 값이 계속 None 이었고, 그 정책은 답을
해도 NEEDS_INFO 에 남았다. 화면에서는 입력했는데 아무 일도 안 일어나는 것으로
보인다. 틀린 판정이 아니라 판정이 안 나오는 쪽이라 신고가 안 들어온다.
`tests/unit/test_answered_months.py` 가 회귀를 막는다.

`age` 에는 폴백이 없다. `birth_date` 가 필수라 age 는 None 이 될 수 없어 질문이
나가지 않고, 답변으로 덮을 수 있게 하면 생년월일과 나이가 어긋난 프로필이 생긴다.

**개월수 답변으로는 충족 예상일을 만들지 않는다.** `timeline._counter_for` 는
시작일이 없으면 여전히 None 을 돌려준다. "24개월째"는 [24, 25) 개월 어딘가라
역산한 시작일이 최대 한 달 틀리고, 그 날짜가 "○월 ○일부터 가능"으로 화면에 나간
뒤 그날 판정하면 여전히 부적격일 수 있다 — `timeline.py` 가 계산식 복제를
금지하면서까지 막으려는 상황이다. 날짜가 필요하면 개월수가 아니라 시작일을 받아야
한다. 이 판단은 `_counter_for` 주석에도 적어 뒀다.

### 숫자 입력 범위 가드 (`FIELD_BOUNDS`)

`app/schemas/user.py` 의 `FIELD_BOUNDS` / `check_bounds()`. 계약으로도 나간다
(`docs/contracts/rule_fields.json` 의 `numeric_bounds`).

**왜 타입 검사로 부족한가.** 룰은 `household_income_ratio_median <= 150` 처럼 비교만 한다.
FE 가 원 단위(`3000000`)를 보내면 그것도 `int` 라서 타입 검사를 통과하고, 비교가 정상
수행되어 소득 조건이 붙은 정책이 **전부 그럴듯한 '부적격'으로** 나간다. 400 도
`NEEDS_INFO` 도 아니라서 아무도 신고하지 않는다.

**더 나쁜 쪽은 반대 방향이었다.** 소득을 배수로 착각해 `1.5` 를 보내면 범위(0~1000) 안이라
경계 검사로는 못 잡는데, `1.5 <= 150` 이 참이 되어 **부적격이어야 할 사용자가 적격으로**
나간다. 그래서 범위표에 있는 필드는 범위뿐 아니라 **정수인지**도 검사한다.
`bool` 도 거부한다 — 파이썬에서 `isinstance(True, int)` 가 참이라 그냥 두면 `True` 가
`household_size: 1` 로 샌다.

검사 위치는 API 가 아니라 스키마(`__post_init__`)다. `/v1/judge` 와 `/v1/sessions` 가
각각 디코드하는데, 한쪽에만 걸면 다른 쪽으로 같은 값이 들어온다. `answers`(역질문 답변)도
같이 검사한다 — `core` 만 보면 온보딩은 막히고 역질문은 통과하는 구멍이 남는다.

상한은 넉넉하다 (소득 1000%, 가구원 20명, 개월 1200). 실제 공고문 최댓값(중위소득
150~200%)보다 한참 위다. 빡빡하게 잡으면 언젠가 300% 짜리 공고가 나왔을 때 멀쩡한
사용자가 막힌다.

위반 시 `422 invalid-profile` 로, 어떤 필드에 뭐가 들어왔는지 `detail` 에 담아 나간다:

```
household_income_ratio_median 는 0~1000 범위여야 합니다 (받은 값: 3000000). 단위를 확인하세요
```


---

## 4. 코드 지도

```
app/
├─ api/v1/
│   ├─ judge.py       POST /v1/judge, /v1/questions, /v1/combinations, /v1/plan, /v1/plan.ics
│   │                 GET  /v1/policies, /v1/policies/{id}, /v1/meta/snapshot
│   └─ sessions.py    POST/GET/PUT/DELETE /v1/sessions   ← FE 의 GET/PUT /api/profile 대응
├─ engine/            🔵 B 레이어 (결정론, LLM·DB import 금지)
│   ├─ snapshot.py    스냅샷 보관소 · 핫스왑 (실패 시 직전 것 유지)
│   ├─ compile.py     PolicySchema → numpy 열 배열. 연산자를 4형태로 일반화
│   ├─ evaluate.py    벡터화 판정 (600정책 0.25ms)
│   ├─ questions.py   역질문 큐 (필드별 병합)
│   └─ timeline.py    충족 예상일 — 세는 함수의 '역함수'로 정의 (계산식 복제 안 함)
├─ solver/            🔵 D 레이어 — bitset 분기한정 MWIS (완전탐색과 200/200 일치)
├─ planner/           🔵 E 레이어
│   ├─ businessday.py 공휴일표 + 영업일 색인
│   ├─ documents.py   서류 마스터 36종 로더 + 별칭 매칭
│   ├─ backplan.py    착수일 역산 · 유효기간 구간 · 서류 기준 뷰
│   └─ ics.py         RFC 5545 직접 생성 (75옥텟 줄 접기)
├─ db/                asyncpg 풀 + 세션 리포지토리  ← engine/solver/planner 는 import 금지
├─ llm/               🟡 C 레이어 — engine/solver/planner 는 import 금지
│   ├─ client.py      Anthropic 구조화 출력 호출 (SDK 는 지연 import — 서버 이미지에 불필요)
│   ├─ explain.py     C2 판정 설명문 (결정론 템플릿 + 선택적 LLM 다듬기)
│   └─ prompts/       ← AI 역할 소유. a2_structure.md · c2_explain.md
├─ core/
│   ├─ config.py      환경변수 설정
│   └─ crypto.py      AES-256-GCM
└─ schemas/           계약면 (코드가 원본, JSON Schema 는 자동 생성)
    ├─ user.py        UserProfile. resolve() 우선순위 + FIELD_BOUNDS 범위 가드 (§3)
    ├─ validate.py    PolicySchema 게이트 (source_quote 100% 등)
    └─ catalog.py     목록 응답 — BE↔FE 표현면. C1 Freeze 와 분리해 두었다

data/
├─ demo/              🟢 고정 데모 5건 + 사용자 1명 (합성). 키 없이 전 경로가 돈다
│   └─ responses/     🟢 FE Mock — 실제 응답 12건 녹화본 (아래 참고)
├─ manual/            실제 공고 손입력 + A2 응답 (`--responses` 로 키 없이 검증)
└─ documents/         서류 마스터 36종 CSV

tests/
├─ unit/              계층별
└─ e2e/               🟢 제출용 시나리오 6종 — 데모 대본이자 회귀 감시

scripts/
└─ preflight.sh       🔴 푸시 전 검사 — CI 세 잡의 조건을 로컬에서 재현 (§6)

batch/
├─ collect/           온통청년 수집기 + G0 조사 하네스 + normalize(코드값 → 룰)
├─ agents/            A2 구조화 (AI 역할)
│   ├─ contract.py    모델 출력 JSON 스키마 (enum 은 app.schemas.enums 와 테스트로 묶임)
│   ├─ text.py        텍스트 조립 · 인용문 원문 대조
│   ├─ questions.py   필드별 역질문 표준 문구
│   ├─ structure.py   검증 · 병합 · 리포트 (순수 함수 merge)
│   └─ cli.py         structure 명령 + 응답 캐시
├─ build_snapshot.py  🔴 스냅샷 빌더 (검증 관문)
└─ holidays.py        한국천문연구원 특일 API 동기화
```

### FE Mock — `data/demo/responses/`

데모 데이터로 실제 API 를 호출해 받은 응답 12건을 커밋해 뒀다. FE 가 BE 없이 화면을
만들 수 있다. `docs/contracts/` 는 스키마지 인스턴스가 아니라서, 필드가 실제로 어떤
값으로 채워지는지(빈 배열인지 `null` 인지, 날짜 형식이 무엇인지)를 알 수 없다.

`judge.all.json` 한 건에 **네 가지 판정 상태가 전부** 들어 있다 (적격 2 · 부적격 2 ·
확인필요 1 · 충족예상일 1). 에러 4종도 녹화돼 있다 — **에러에도 화면이 있다.**

**손으로 고치지 않는다.** `python tools/record_mock_responses.py` 로 갱신하고 함께
커밋한다 (`docs/contracts/` 와 같은 방식).

녹화본의 유일한 실패 모드는 **낡는 것**이다. 응답 모양이 바뀌어도 파일은 그대로라
FE 가 없어진 필드를 믿고 화면을 만든다. 손으로 쓴 예시였다면 아무도 못 잡는다.
그래서 `tests/unit/test_mock_responses.py` 가 매번 앱을 다시 호출해 대조한다.

기준일은 `2026-10-01` 고정이다. 나이·충족예상일·신청일정이 오늘 날짜를 타면 매일
diff 가 나고, 그러면 아무도 diff 를 읽지 않게 된다. `latency_ms` 와 `loaded_at` 도
같은 이유로 고정값이며, 녹화기와 대조 테스트가 **같은 `stabilize()` 함수**를 써서
둘이 갈라질 수 없게 했다.

### 계약면 — 코드가 원본이다
`docs/contracts/*.json` 은 `tools/export_contract.py` 가 생성한다.
**CI 가 매 PR 에서 드리프트를 검사**하므로, 스키마를 고쳤으면 반드시:

```bash
python tools/export_contract.py && git add docs/contracts/
```

`docs/contracts/rule_fields.json` 이 룰이 참조할 수 있는 사용자 필드의 **전체 집합**이다.
**Profile 에 필드를 추가하려면 `app/schemas/enums.py` 의 `KNOWN_FIELDS` 에도 추가해야 한다** — FE·AI·BE 세 곳이 동시에 맞아야 하는 유일한 지점이다.

같은 파일의 `numeric_bounds` 가 숫자 필드의 **허용 범위와 단위**다 (`FIELD_BOUNDS` 에서
뽑는다). 숫자 필드를 추가하면 범위와 단위 설명을 같이 넣는다 — 둘 중 하나만 있으면
테스트가 실패한다. 단위 없이 경계만 나가면 FE 가 `150` 을 퍼센트로 읽을지 배수로 읽을지
알 수 없고, 그건 조용히 틀리는 쪽이다.

---

## 5. 개발 환경

### 필수 환경변수

| 변수 | 용도 | 없으면 |
| --- | --- | --- |
| `SNAPSHOT_PATH` | 부팅 시 적재할 스냅샷 JSON | 미준비 상태로 기동 (`/readyz` 503) |
| `DATABASE_URL` | asyncpg DSN | 세션 API 만 503, 판정은 정상 |
| `PROFILE_ENC_KEYS` | `1:<base64 32바이트>` | 세션 API 만 503 (평문 폴백 없음) |
| `YPC_FIXED_TODAY` | 판정 기준일 고정 (테스트용) | 실제 KST 오늘 |
| `CORS_ORIGINS` | 브라우저에서 부를 수 있는 출처 (쉼표 구분) | 브라우저에서 호출 불가 |

```bash
# 암호화 키 생성
python -c "from app.core.crypto import generate_key; print(generate_key())"
```

### DB 통합 테스트를 로컬에서 돌리는 법 (Linux·컨테이너)

`DATABASE_URL` 이 없으면 28건이 **skip 되고 초록불이 뜬다.** 그 상태로 푸시했다가
CI 에서만 깨진 적이 있다 — `TestClient` 를 중첩해 열면 나중에 닫히는 쪽이 다른
이벤트 루프에 붙은 asyncpg 풀을 닫으려다 터지는데, 풀이 없는 기계에서는 아무 일도
일어나지 않는다. **DB 경로를 건드렸으면 한 번은 실제 DB 로 돌릴 것.**

```bash
export PGDATA=/tmp/ypcpg PGBIN=/usr/lib/postgresql/16/bin PGPORT=55432
mkdir -p $PGDATA && id -u postgres >/dev/null 2>&1 || useradd postgres
chown postgres $PGDATA && chmod 700 $PGDATA
su postgres -c "$PGBIN/initdb -D $PGDATA -U postgres -A trust"
su postgres -c "$PGBIN/pg_ctl -D $PGDATA -o '-p $PGPORT -k /tmp' -l $PGDATA/server.log start"

psql -h /tmp -p $PGPORT -U postgres -c "CREATE DATABASE ypc_test ENCODING 'UTF8' TEMPLATE template0;"
psql -h /tmp -p $PGPORT -U postgres -d ypc_test -v ON_ERROR_STOP=1 -f db/migrations/0001_init.sql

DATABASE_URL="postgresql://postgres@127.0.0.1:$PGPORT/ypc_test" pytest   # skip 0 이어야 한다
```

데이터 디렉터리는 **postgres 사용자가 통과할 수 있는 경로**여야 한다. 홈 아래나 권한이
좁은 임시 디렉터리에 만들면 `initdb: could not access directory` 로 죽는다.

끝나면 `su postgres -c "$PGBIN/pg_ctl -D $PGDATA stop" && rm -rf $PGDATA`.

### 로컬 PostgreSQL 주의사항 (Windows)

이전 환경에서 겪은 것들이다. 같은 함정에 빠지지 말 것:

1. **initdb 는 한글 경로에서 실패한다** — `C:\Users\서주완\...` 아래에 데이터 디렉터리를 만들면 `invalid byte sequence for encoding "UTF8"` 가 난다. `C:\ypcpg\data` 처럼 ASCII 경로를 쓸 것.
2. **initdb 에 `-E SQL_ASCII` 를 쓰고, 테스트 DB 는 UTF8 로 따로 만든다:**
   ```sql
   CREATE DATABASE ypc_test ENCODING 'UTF8' TEMPLATE template0;
   ```
3. **asyncpg 는 홈 디렉터리에서 SSL 인증서를 찾는다** — 홈 경로에 한글이 있으면 `OSError: [Errno 42] Illegal byte sequence` 가 난다. 로컬 DSN 에 **`?sslmode=disable`** 를 붙일 것. (CI 는 해당 없음)

### 계약 드리프트가 뜨는데 스키마는 안 건드렸다면 파이썬 버전이다

`docs/contracts/*.json` 의 `description` 은 구조체 docstring 에서 나오는데,
**Python 3.13 부터 컴파일러가 docstring 의 공통 들여쓰기를 제거한다.** 3.11 은 그대로
둔다. 그래서 3.14 기계에서 내보낸 JSON 을 커밋하면 3.11 인 CI 가 매 푸시마다
드리프트로 실패하고, 반대로 3.11 출력을 커밋하면 3.13+ 사용자가 실패한다.

실제로 한 번 그렇게 됐다. `tools/export_contract.py` 가 모든 `description` 을
`inspect.cleandoc` 으로 정규화해서 두 버전이 같은 바이트를 내게 막아뒀고,
`tests/unit/test_contract_export.py` 가 3.11 출력을 합성해 넣어 회귀를 잡는다
(3.14 에서 돌리면 진짜 docstring 은 이미 dedent 되어 있어 정규화가 빠져도 통과한다).

**드리프트가 뜨면 먼저 `python -V` 를 볼 것.** 계약이 바뀐 게 아닐 수 있다.

### 파일 입출력에는 반드시 `encoding="utf-8"` 을 쓴다

한국어 Windows 의 기본 인코딩은 **cp949** 다. `Path.read_text()` 처럼 인코딩을 생략하면
같은 코드가 개발자 기계에 따라 동작하거나 `UnicodeDecodeError` 로 죽는다. 이 저장소는
SQL·JSON·리포트에 전부 한글이 들어가므로 생략하면 언젠가 반드시 걸린다.

**출력도 마찬가지다.** CLI 가 한글을 `print` 하면 콘솔 코드페이지(cp949 / cp1252)가
표현하지 못해 `UnicodeEncodeError` 로 죽는다. 파일과 달리 `encoding=` 을 줄 자리가 없어서
`app/core/console.py` 의 `force_utf8_console()` 을 CLI 진입점에서 먼저 부른다.

ruff 의 `PLW1514` 규칙과 `tests/unit/test_locale_safety.py`, 그리고 CI 의 `windows-cp949`
잡이 함께 잡는다. 로컬에서 미리 확인하려면:

```bash
PYTHONUTF8=0 pytest        # cp949 로캘 흉내 (git-bash)
$env:PYTHONUTF8=0; pytest  # PowerShell
```

### `windows-cp949` 잡은 인코딩만 다른 게 아니다 — 의존성도 다르다

이 잡은 `pip install -e ".[dev]"` 만 한다. **`anthropic` SDK 가 없다.** 서버 이미지에
배치 의존성을 넣지 않는다는 원칙(§3)을 CI 가 한 칸에서 실제로 지키는 셈이다.

그래서 테스트가 SDK 를 전제하면 **여기서만 깨진다.** 실제로 그렇게 깨진 적이 있다 —
로컬 venv 에는 `[batch]` 가 깔려 있어 cp949 로 돌려도 통과했고, CI 에서만 5연속
실패했다. cp949 를 흉내 내는 것만으로는 부족하다는 뜻이다.

SDK 를 실제로 만들어 보는 테스트에는 `@needs_sdk` 를 붙인다
(`tests/unit/test_llm_failure.py`). 폴백·로깅처럼 SDK 없이 검증되는 것은 어디서나 돈다.
SDK 가 **없을 때** `get_llm()` 이 None 이라는 것 자체도 계약이라, 그건 반대로
SDK 가 있는 환경에서 skip 된다 — 두 환경이 서로 다른 절반을 검사한다.

로컬에서 그 환경을 흉내 내려면 패키지를 잠시 치운다:

```bash
ANT=$(python -c "import anthropic,os;print(os.path.dirname(anthropic.__file__))")
mv "$ANT" "$ANT.hidden"
env -u DATABASE_URL -u PROFILE_ENC_KEYS PYTHONUTF8=0 pytest   # windows-cp949 와 같은 조건
mv "$ANT.hidden" "$ANT"
```

### 실행

```bash
# DB 스키마
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/0001_init.sql
psql "$DATABASE_URL" -f db/verify_schema.sql      # [MUST FAIL] 항목은 에러가 나야 정상

# API
SNAPSHOT_PATH=snapshot.json \
  DATABASE_URL=postgresql://... \
  PROFILE_ENC_KEYS="1:<base64>" \
  uvicorn app.main:app --reload

# 스냅샷 빌드
python -m batch.build_snapshot data/policies.json -o snapshot.json
#   --check-only     쓰지 않고 검사만
#   --allow-partial  검증 실패분 제외 (누락은 사용자에게 안 보인다)
#   --force          직전 대비 급감 검사 건너뛰기
```

---

## 6. 작업 규칙

### 푸시 전 반드시

```bash
DATABASE_URL="postgresql://postgres@127.0.0.1:55432/ypc_test" ./scripts/preflight.sh
```

CI 세 잡의 조건을 로컬에서 그대로 돌린다. **`pytest && ruff && mypy` 만으로는
부족하다** — CI 잡마다 환경이 다르고, 실제로 그 차이 때문에 5커밋 연속 빨간불이
난 적이 있다 (로컬에는 `[batch]` 가 깔려 있는데 `windows-cp949` 잡에는 없었다).

| 검사 | 대응 CI 잡 |
| --- | --- |
| 린트 · 타입 · 계약 드리프트 · 벤치마크 | `test` |
| 전체 테스트 (DB 통합 포함) | `test` |
| DB 통합 테스트가 실제로 돌았나 (skip 금지) | `test` |
| SDK 없음 + DB 없음 + cp949 | `windows-cp949` |

`docker` 잡만 빠져 있다 — 도커 데몬이 없는 기계가 있어서다. Dockerfile 이나
`render.yaml` 을 건드렸으면 그건 CI 에서 확인한다.

`DATABASE_URL` 없이 돌리면 DB 검사를 건너뛰고 **종료코드 2** 로 끝난다.
건너뛴 검사는 CI 가 대신 돌리므로, 초록불을 본 게 아니라는 뜻이다.

> cp949 조건을 만들려고 `anthropic` 패키지를 잠시 치웠다가 되돌린다. 중단되어
> 숨겨진 채로 남으면 다음 실행이 알아서 복구한다.

### 커밋 전 최소한

```bash
pytest && ruff check . && mypy && python tools/export_contract.py && git diff --exit-code docs/contracts/
```

### 브랜치·푸시
- 작업 브랜치는 `dev` 에서 분기, PR 대상도 `dev`
- **푸시 전에 `git remote -v` 로 리모트 이름을 확인할 것.** 환경마다 다르다 —
  어떤 기계는 `origin` 이 팀 저장소이고, 어떤 기계는 `team` 이 따로 있다.
  이 문서가 예전에 `team` 으로 단정해둔 탓에 실제로 혼선이 있었다
- `main` 직접 푸시 금지 — 팀 합의 후 PR
- **머지된 브랜치에 계속 쌓지 말 것.** PR 이 머지되면 그 브랜치 커밋은 `dev` 에 있다.
  이어서 작업하려면 `dev` 에서 새로 분기한다

### 성능 가드
`bench/engine_bench.py` 가 CI 에서 돌며 **기준 초과 시 빌드를 실패시킨다**:
- 판정 > 50ms → 인메모리 설계 전제가 깨짐
- 계획 생성 > 50ms → 영업일 색인이 깨짐
- 영업일 역산 1회 > 50µs → 색인 경로가 깨짐
- MWIS 완전탐색 대조 20/20 미달 → G4 게이트 미달

### 테스트 작성 시
- **async 테스트는 `@pytest_asyncio.fixture`** 를 써야 한다. `@pytest.fixture` 로 쓰면 pytest-asyncio 1.x 에서 "async fixture with no plugin that handled it" 에러가 난다.
- DB 통합 테스트는 `DATABASE_URL` 이 없으면 skip 되지만, **CI 는 skip 을 실패로 처리**한다 (`.github/workflows/ci.yml` 의 "DB 통합 테스트가 실제로 실행됐는지 확인" 스텝).

---

## 7. 실측 성능 (참고 기준선)

| 항목 | 측정값 | 기준 |
| --- | --- | --- |
| 룰 평가 (600 정책 / 2,442 룰) | 0.25 ms | — |
| 룰 평가 (3,000 정책 / 12,208 룰) | 0.74 ms | — |
| 판정 API 1건 (예산 합계) | ~57 ms | p95 ≤ 5,000 ms |
| `/v1/judge` 기본 응답 (600 정책) | 9.4 ms · 231 KB | p95 ≤ 5,000 ms |
| `/v1/judge` 기본 응답 (3,000 정책) | 42.9 ms · 1,188 KB | p95 ≤ 5,000 ms |
| 신청 계획 (3,000 정책 / 715 적격) | 10.1 ms | p95 ≤ 50 ms |
| 영업일 역산 1회 | 1.5 µs | — |
| MWIS 정점 30개 | 3.5 ms | — |
| 벡터화 ↔ 기준 구현 대조 | 7,200건 전부 일치 | — |
| 2026 공휴일 (대체공휴일 포함 21일) | 전수 일치 | G5: 100% |
| MWIS 정확성 | 200/200 완전탐색 일치 | G4: 100% |

기본 응답 수치는 **정책별 상세 조립 + 설명문 생성까지 포함**한 값이다. 부적격도
상세를 만들어 봐야 충족 예상일이 있는지 알 수 있어서 전건을 조립한다(40.7ms), 설명문
템플릿은 그 위에 2.2ms 를 더한다. 실스냅샷은 1,555건이라 대략 절반이다.

**비동기 분석(`analysis_id` + polling)을 만들지 말자고 FE 에 제안한 근거가 이 표다.** 동기 응답이 수십 ms 라서, 작업 ID·상태·폴링·timeout·무효화 규칙은 전부 없는 대기시간을 관리하는 상태가 된다.

---

## 8. 미결 사항 정리

### 아직 막혀 있는 것

| # | 항목 | 막는 사람 | 비고 |
| --- | --- | --- | --- |
| 5 | 인증 방식 (익명 세션 vs 로그인) | 팀 | 현재 익명 세션 |
| 6 | 응답 envelope (`{data, request_id}` 래핑) | FE | 현재 페이로드 직접 반환 |
| 8 | 서류 마스터 — 유효기간 근거 (법령 조문) | 사람 | 소요일·수수료는 21/36 확인됨. **유효기간은 0/36** 이라 화면은 계속 추정치다 (§2-③) |
| 16 | 실공고로 중복수혜 조합(시나리오 5)을 보여줄 데이터 | 데이터 | 상충 쌍이던 국토부 청년월세가 2026-05-29 에 마감됐다. 신청기간이 열려 있는 전국·경기 단위 주거 정책 1건이 더 필요하다 (`data/manual/README.md`). 합성 데이터로는 `data/demo/` 에서 돌고 `tests/e2e` 가 단언한다 |
| 9 | FE 공개 배포 주소 (→ `CORS_ORIGINS`) | FE | BE 는 떠 있다 (아래 '결정된 것'). FE 주소가 없어 CORS 가 비어 있고, **그래서 지금은 브라우저에서 BE 를 못 부른다**. 주소가 나오면 Render 환경변수에 넣는 것만 남았다 |
| 11 | A2 실행용 `ANTHROPIC_API_KEY` (누구 계정, 예산) | 팀 | 아래 비용 추정 참고 |

### 결정된 것 (2026-09-19, FE 회신)

FE 회신 내용을 코드와 대조해 확인했다. 세 건 모두 BE 현재 동작과 일치해서 BE 변경은 없었다.

| 항목 | 확정 | 코드 근거 |
| --- | --- | --- |
| 경로 접두사 | `/v1/*` (`/api/v1` 아님) | `app/api/v1/judge.py` 라우터 `prefix="/v1"` |
| 소득 단위 | 기준 중위소득 대비 **퍼센트 정수** (150% → `150`) | `Core.household_income_ratio_median: int` |
| `personal_income`·`employment_type` | 안 받는다 | 스키마에 없고 `KNOWN_FIELDS` 에도 없어 룰이 참조 불가 |

FE 에 되짚어 둔 두 가지:

- **"정수 비율"은 퍼센트다.** `1.5` 가 아니라 `150`. 역질문 문구도 "몇 %인가요?" 이다.
- **필드명은 `employment_type` 이 아니라 `employment_status`** 다 (`employed` / `job_seeking` /
  `student` / `founder` / `neet`). 지금은 룰이 안 쓰지만, 나중에 `employment_type` 으로
  보내면 `forbid_unknown_fields` 때문에 프로필 전체가 422 로 튕긴다.

이 회신을 확인하다 두 가지를 더 발견해서 같이 고쳤다. 둘 다 §3 에 있다:

- **숫자 입력에 범위 검사가 없었다** — 원 단위(3000000)도 배수(1.5)도 통과했다.
  배수 쪽은 `1.5 <= 150` 이 참이라 **부적격이 적격으로** 뒤집혔다.
- **역질문으로 받은 근속·거주 개월수가 버려지고 있었다** — 질문은 나가는데
  답변이 반영되지 않아, 답해도 그 정책은 계속 NEEDS_INFO 에 남았다.

FE 에 이미 전달한 것 (화면에 영향이 있다):

- 범위 밖·비정수 숫자는 이제 `422 invalid-profile` 이다 (`detail` 에 필드명과 받은 값)
- 거주 질문 문구가 `"언제부터 계속 살고 계신가요?"` → `"몇 개월째 살고 계신가요?"` 로 바뀌었다
  (답변 형태가 `number` 인데 날짜를 묻고 있었다 — 사용자가 연도를 적게 된다)
- 개월수 **답변**으로는 `future_eligible_from` 이 나오지 않는다. 날짜 배지가 필요하면
  개월수가 아니라 시작일(`employment_start_date` / `residence_start_date`)을 받아야 한다

### #11 — A2 비용 추정 (실데이터 기준)

프롬프트 7.8KB · 정책당 원문 약 4KB · 응답 평균 3.7KB 로, Opus 5($5/$25 per MTok,
시스템 프롬프트는 `cache_control` 로 10%) 기준:

| 범위 | 대략 |
| --- | --- |
| 5건 (`--limit 5`) | $0.5 미만 |
| published 1,555건 | $150 내외 |
| 전체 2,819건 | $270 내외 |

한글 토크나이저 비율을 보수적으로 잡은 추정이다. **P0 가 요구하는 3~5건은 커피값도
안 된다** — 돈이 문제가 되는 건 전수 실행뿐이다.

**키 없이 가는 길이 이미 있다.** `data/manual/` + `--responses` 가 손으로 쓴 A2 응답을
모델 응답과 **똑같은 검증 파이프라인**(인용문 원문 대조 → 불일치 검사 →
`validate_policy` → 병합 → 리포트)에 태운다. 파이프라인은 출처를 구분하지 않고,
품질을 보증하는 건 출처가 아니라 원문 대조다.

### 해결된 것 (기록)

| # | 항목 | 어떻게 |
| --- | --- | --- |
| 4 | 정책 수준 `future_eligibility_date` | `future_eligible_from` + `summary.future_eligible` 로 제공 (§3) |
| 7 | 온통청년 API 명세 | 실호출로 확정. `/go/ythip/getPlcy`, `zipCd` 5자리, 1,000건/페이지 |
| 10 | `needs_review_fields` 를 confidence 에 반영 | 반영한다. 단순 부적격은 제외 (§3) |
| 12 | 충족 예상일이 다른 룰과 모순 | 후보 날짜로 전 조건을 재평가해 막는다 (§3) |
| 13 | `apply_end` 가 지난 정책이 `ELIGIBLE`·조합 후보로 나온다 | 빌더가 마감 기간을 직접 보고 거른다 + `normalize` 가 status 를 바로잡는다 (§3) |
| 14 | 에러 응답 규격 | RFC 9457 `problem+json` 으로 맞췄다. 유형 표는 `docs/contracts/problems.json` (§3) |
| 15 | FE Mock JSON | `data/demo/responses/` 에 실제 응답 12건을 녹화해 커밋했다 (§4) |
| 19 | `/docs` 에 POST 요청 본문이 안 나옴 | `openapi_extra` + msgspec 컴포넌트 병합으로 채웠다 (§3) |
| 20 | POST 5개가 깨진 JSON 에 500 | 디코드를 한 군데로 모으고 `DecodeError` 까지 잡는다 (§3) |
| 1·2·3 | 소득 단위 · 경로 접두사 · 불필요 필드 | FE 회신으로 확정. 셋 다 BE 현재 동작과 일치해 변경 없었다 (위 '결정된 것') |
| 9 | 배포 Base URL | `https://be-27y9.onrender.com` (§1). 남은 건 반대 방향 — FE 주소다 |
| 17 | 역질문 답변(근속·거주 개월수)이 판정에 반영되지 않음 | `resolve()` 가 `answers` 로 폴백한다. 미래 날짜는 일부러 주지 않는다 (§3) |
| 18 | 숫자 입력의 단위 실수를 아무것도 잡지 않음 | `FIELD_BOUNDS` + `check_bounds()`, 계약에 `numeric_bounds` 로 실려 나간다 (§3) |

---

## 9. 참고 문서

| 문서 | 내용 |
| --- | --- |
| `README.md` | 진행 상태 · 실측 성능 · 시작하기 |
| `docs/ARCHITECTURE.md` | 전체 아키텍처 · ADR 6건 · 무료 티어 실사 · 성능 예산 |
| `docs/DB_SCHEMA.md` | 테이블 14종 설계 · 인덱스 전략 · 스토리지 예산 |
| `docs/DEPLOY.md` | 배포 — 환경변수 · 헬스체크 두 종류 · CORS 의 ETag 함정 |
| [FE 이슈 #2](https://github.com/Claude-MCP-7team/FE/issues/2) | 계약 확정 논의 — **BE 답변 이미 등록됨** |

커밋 메시지는 **무엇을 했는지가 아니라 왜 그렇게 했는지**를 적는 형식을 유지하고 있다. `git log` 를 읽으면 설계 판단의 근거가 나온다.
