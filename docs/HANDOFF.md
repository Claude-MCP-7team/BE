# 인수인계 — YPC 백엔드

> 이 문서만 읽고 이어서 작업할 수 있도록 쓴다.
> 마지막 갱신: `ec2a29b` 시점. 작업 재개 시 `git log --oneline -5` 로 실제 HEAD 를 먼저 확인할 것.

---

## 0. 30초 요약

청년정책 자격 판정 백엔드. **B(룰 엔진) · D(조합 솔버) · E(일정 역산) + DB · 배치**가 역할 범위다.
프롬프트 설계는 AI 역할, 화면은 FE 역할이며 경계는 `docs/ARCHITECTURE.md` §4 에 있다.

**핵심 구조 한 줄:** 배치가 만든 정책 스냅샷을 API 가 부팅 시 RAM 에 통째로 올리고, 판정 요청은 DB 를 한 번도 건드리지 않는다 (ADR-001).

현재 **판정 · 역질문 · 조합 · 일정 · 세션 저장이 모두 동작**한다. 남은 건 크롤러와 AI 오케스트레이션이다.

---

## 1. 지금 상태

| 항목 | 값 |
| --- | --- |
| HEAD | `ec2a29b` Store profiles so that a database dump reveals nothing |
| 팀 저장소 | `Claude-MCP-7team/BE` 의 **`dev`** 브랜치 (= `team` 리모트) |
| 개인 백업 | `seo99126-debug/MCP` 의 `main` (= `origin` 리모트) |
| 미푸시 커밋 | 0 |
| 미커밋 변경 | 0 |
| 테스트 | **384건 통과** (DB 통합 28건 포함) |
| CI | 통과 (lint · 마이그레이션 · 제약조건 · 테스트 · 계약 드리프트 · 벤치마크) |

> ⚠️ `main` 브랜치는 아직 `5a991da`(README 1개)에 멈춰 있다. 작업물은 전부 `dev` 에 있다.
> `dev` → `main` 머지는 팀 합의 후 PR 로 한다.

### 로컬에서 이어받기

```bash
git clone --branch dev https://github.com/Claude-MCP-7team/BE.git ypc-backend
cd ypc-backend

python -m venv .venv && . .venv/Scripts/activate   # Windows: .venv/Scripts/activate
pip install -e ".[dev,batch]"

pytest            # 356 passed, 28 skipped (DB 없으면 통합 테스트는 skip)
ruff check .
python bench/engine_bench.py
```

`DATABASE_URL` 이 있으면 384건 전부 돈다. 없으면 DB 통합 28건이 skip 된다 — **skip 된 걸 통과로 착각하지 말 것.**

---

## 2. 작업 순서 제안

의존성과 리스크를 고려한 순서다.

### ① FE 이슈 #2 답변 확인 (제일 먼저)
https://github.com/Claude-MCP-7team/FE/issues/2

BE 답변은 이미 달아뒀다 ([코멘트](https://github.com/Claude-MCP-7team/FE/issues/2#issuecomment-5658413977)).
**FE 결정을 기다리는 항목이 2개 있고, 둘 다 BE 코드를 바꾼다:**

1. **소득 입력 단위** — 현재는 중위소득 비율(정수 %)을 받는다. FE 가 원 단위로 받겠다면 BE 가 가구원수별 중위소득 표로 환산해야 하고, 그러면 `household_size` 가 필수가 되며 환산 기준연도를 응답에 실어야 한다.
2. **경로 접두사** — BE 는 `/v1/*`, FE 제안은 `/api/*`. `/api/v1/*` 로 통일 제안해둔 상태.

답변이 왔으면 그것부터 반영하고, 안 왔으면 아래로 진행한다.

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

**A2 부속 세 가지** (전부 키 없이 테스트됨):

- `batch/agents/documents.py` — 서류 표기 꼬리("1부", "(필수)", "등")를 걷어내고 플래너의 `resolve()` 로 doc_code 를 찾는다.
  모델이 준 `canonical_name` 은 마스터 정식 명칭과 **글자 그대로** 같을 때만 쓴다 (별칭·유사 이름 불인정). 마스터 목록은
  사용자 메시지 끝에 붙어 프롬프트 캐시를 깨지 않는다. 마스터에 없는 서류는 §8 #14.
- `batch/agents/crosscheck.py` — `--cross-check` (기본 두 번째 모델 `claude-sonnet-5`, `--model-b`). 같은 (field, op, value) 면 유지(confidence 는 낮은 쪽),
  한쪽에만 있으면 유지+NEEDS_REVIEW, 값이 다르면 A 유지+NEEDS_REVIEW+ambiguous, 서류·상충은 합집합. **룰을 지우는 경우는 없다.**
  `quality.cross_check` 가 AGREE/DISAGREE 로 채워진다. `--responses` 모드에서는 `<plcyNo>.b.json` 이 있는 정책만 교차검증한다.
- `batch/agents/golden.py` + `tests/golden/a2_expected.json` — 데모 5건의 정답 룰·금지 필드·doc_code·상충 하한·표식.
  `python -m batch.agents.golden <policies.json> tests/golden/a2_expected.json` 이 정책별 재현율을 낸다. 실제 모델을 처음 돌릴 때
  이 숫자를 기준으로 프롬프트를 고친다.

**C2 설명문도 생겼다** (`app/llm/explain.py`, 프롬프트 `app/llm/prompts/c2_explain.md`):

- `POST /v1/judge` 가 기본으로 결과마다 `explanation` 을 채운다 — **결정론 템플릿**이라 LLM 없이 항상 동작하고 비용이 없다.
  미충족은 "내 값 / 공고 기준 / 인용문 / 언제부터 되는지(또는 영구 불가)"를, 확인 필요는 질문을 그대로, 추정 판정은 담당부서 전화를 붙인다.
- `?explain=llm` 이면 결과 전체를 **한 번의 호출**로 다듬는다. 설명 속 숫자가 결과·초안에 없으면 그 항목은 템플릿으로 되돌린다
  (금액·기간·조건을 지어내는 것이 C2 의 유일한 실패 모드다, PRD §23). 키가 없으면 `template` 과 같다. `?explain=none` 은 생략.
- ETag 에 explain 모드가 들어간다. `app/api/v1/judge.py` 를 손댄 유일한 이유다. 첫 실행은 `--limit 5` 로
  하고 리포트의 `rejected`/`disagreements` 를 읽어 프롬프트를 손보는 것이 AI-M2-3(수동 정답표 대조)이다.

### ③ 서류 마스터 검증 (BE-M5-1 잔여)
`data/documents/master_v2.csv` 36종이 **전부 `검증상태=확인필요`** 다.
실제로 확인된 항목만 `확인완료` 로 바꾸면 `master_unverified` 가 false 가 되고 화면의 추정치 표시가 사라진다. 코드 변경은 필요 없다 — CSV 만 고치면 된다.

### ④ mypy strict 에러 11건
CI 가 mypy 를 돌리지 않아서 통과는 하지만, `app/engine/evaluate.py` · `app/engine/questions.py` 중심으로 타입 어노테이션 누락 11건이 남아 있다. 블로커는 아니다.

```bash
mypy   # files = ["app", "batch"], strict = true
```

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

---

## 4. 코드 지도

```
app/
├─ api/v1/
│   ├─ judge.py       POST /v1/judge, /v1/questions, /v1/combinations, /v1/plan, /v1/plan.ics
│   │                 GET  /v1/policies/{id}, /v1/meta/snapshot
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
│   └─ prompts/       ← AI 역할 소유. a2_structure.md
├─ core/
│   ├─ config.py      환경변수 설정
│   └─ crypto.py      AES-256-GCM
└─ schemas/           계약면 (코드가 원본, JSON Schema 는 자동 생성)

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

### 계약면 — 코드가 원본이다
`docs/contracts/*.json` 은 `tools/export_contract.py` 가 생성한다.
**CI 가 매 PR 에서 드리프트를 검사**하므로, 스키마를 고쳤으면 반드시:

```bash
python tools/export_contract.py && git add docs/contracts/
```

`docs/contracts/rule_fields.json` 이 룰이 참조할 수 있는 사용자 필드의 **전체 집합**이다.
**Profile 에 필드를 추가하려면 `app/schemas/enums.py` 의 `KNOWN_FIELDS` 에도 추가해야 한다** — FE·AI·BE 세 곳이 동시에 맞아야 하는 유일한 지점이다.

---

## 5. 개발 환경

### 필수 환경변수

| 변수 | 용도 | 없으면 |
| --- | --- | --- |
| `SNAPSHOT_PATH` | 부팅 시 적재할 스냅샷 JSON | 미준비 상태로 기동 (`/readyz` 503) |
| `DATABASE_URL` | asyncpg DSN | 세션 API 만 503, 판정은 정상 |
| `PROFILE_ENC_KEYS` | `1:<base64 32바이트>` | 세션 API 만 503 (평문 폴백 없음) |
| `YPC_FIXED_TODAY` | 판정 기준일 고정 (테스트용) | 실제 KST 오늘 |

```bash
# 암호화 키 생성
python -c "from app.core.crypto import generate_key; print(generate_key())"
```

### 로컬 PostgreSQL 주의사항 (Windows)

이전 환경에서 겪은 것들이다. 같은 함정에 빠지지 말 것:

1. **initdb 는 한글 경로에서 실패한다** — `C:\Users\서주완\...` 아래에 데이터 디렉터리를 만들면 `invalid byte sequence for encoding "UTF8"` 가 난다. `C:\ypcpg\data` 처럼 ASCII 경로를 쓸 것.
2. **initdb 에 `-E SQL_ASCII` 를 쓰고, 테스트 DB 는 UTF8 로 따로 만든다:**
   ```sql
   CREATE DATABASE ypc_test ENCODING 'UTF8' TEMPLATE template0;
   ```
3. **asyncpg 는 홈 디렉터리에서 SSL 인증서를 찾는다** — 홈 경로에 한글이 있으면 `OSError: [Errno 42] Illegal byte sequence` 가 난다. 로컬 DSN 에 **`?sslmode=disable`** 를 붙일 것. (CI 는 해당 없음)

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

### 커밋 전 반드시

```bash
pytest && ruff check . && python tools/export_contract.py && git diff --exit-code docs/contracts/
```

### 브랜치·푸시
- 작업 브랜치는 `dev` 에서 분기, PR 대상도 `dev`
- 푸시: `git push team <branch>` (team = `Claude-MCP-7team/BE`)
- `main` 직접 푸시 금지 — 팀 합의 후 PR

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
| 신청 계획 (3,000 정책 / 715 적격) | 10.1 ms | p95 ≤ 50 ms |
| 영업일 역산 1회 | 1.5 µs | — |
| MWIS 정점 30개 | 3.5 ms | — |
| 벡터화 ↔ 기준 구현 대조 | 7,200건 전부 일치 | — |
| 2026 공휴일 (대체공휴일 포함 21일) | 전수 일치 | G5: 100% |
| MWIS 정확성 | 200/200 완전탐색 일치 | G4: 100% |

**비동기 분석(`analysis_id` + polling)을 만들지 말자고 FE 에 제안한 근거가 이 표다.** 동기 응답이 수십 ms 라서, 작업 ID·상태·폴링·timeout·무효화 규칙은 전부 없는 대기시간을 관리하는 상태가 된다.

---

## 8. 미결 사항 정리

| # | 항목 | 막는 사람 | 비고 |
| --- | --- | --- | --- |
| 1 | 소득 입력 단위 (원 vs 비율) | FE | BE 코드 변경 필요 |
| 2 | 경로 접두사 (`/v1` vs `/api/v1`) | FE | BE 가 맞출 수 있음 |
| 3 | `personal_income`·`employment_type` 필요 여부 | FE | 현재 룰이 참조 안 함 |
| 4 | 정책 수준 `future_eligibility_date` 제공 여부 | FE | 현재는 조건별 날짜만 |
| 5 | 인증 방식 (익명 세션 vs 로그인) | 팀 | 현재 익명 세션 |
| 6 | 응답 envelope (`{data, request_id}` 래핑) | FE | 현재 페이로드 직접 반환 |
| 7 | 온통청년 API 명세 | **외부** | ② 작업을 막고 있음 |
| 8 | 서류 마스터 36종 검증 | 사람 | CSV 수정만 필요 |
| 9 | 배포 Base URL · CORS | 팀 | Render Free 예정 |
| 10 | `needs_review_fields` 의 `unrepresentable_conditions` 표식을 판정 confidence 에 반영할지 | BE | 엔진이 못 보는 조건이 있는 정책이 CONFIRMED·ELIGIBLE 로 나간다 |
| 11 | ~~A2 실행용 `ANTHROPIC_API_KEY`~~ → **팀 결정(2026-09-21): 키를 쓰지 않는다** | 결정됨 | 제출 범위(P0 3~5건)는 손으로 옮긴 11건 + 같은 검증 경로로 충족. 실제 모델 실행·교차검증·A/B 는 미실행으로 남긴다. 도구(`--limit`, `--cross-check`, 골든 채점)는 그대로 있어 키가 생기면 30분에 돌릴 수 있다. 대화형 시연은 MCP + Claude 구독으로 |
| 12 | 충족 예상일이 다른 룰과 모순될 때 | BE | 24세 사용자의 청년기본소득: 거주 36개월은 "2029-05-15부터 가능"인데 그때는 27세라 나이 룰이 깨진다. 룰별 날짜만 내고 정책 수준 교차검증은 없음 (`tools/demo_scenario.py` A-1) |
| 14 | 서류 마스터에 없는 서류 (실제 공고에서 자주 나옴) | 사람 | `지방세(재산세) 미과세증명서`(위택스, 가평 월세), `본인신용정보조회서`(크레딧포유), `소득·재산 신고서`(서식). 마스터에 행을 추가해야 계획 소요일이 잡힌다. A2 는 목록에 없는 서류를 추측으로 잇지 않는다 (`batch/agents/documents.py`) |
| 15 | **지역 코드 단위 불일치 — 시 코드 사용자가 경기도 정책 대부분에서 부적격** | BE + FE | API 는 구가 있는 시를 **구 코드**로 준다(용인 = `41461,41463,41465`). 지역 제한 정책 2,364건 중 **801건**. 사용자가 시 코드 `41460` 을 넣으면 접두 체인 `['00','41','41460']` 이 어느 구 코드와도 안 겹쳐 청년기본소득(경기 전역)까지 INELIGIBLE 이 된다. 반대로 사용자 `41465` 는 시 코드 `41460` 으로 적힌 정책과 안 맞는다. 방향: (a) `region_chain` 이 구 코드에서 시 코드(앞 4자리+`0`)도 만들고, (b) FE 는 구가 있는 시에서 구를 고르게 하거나 BE 가 시→구 전개표를 갖는다 |
| 13 | `apply_end` 가 지난 정책이 `ELIGIBLE`·조합 후보로 나온다 | BE | `aplyPrdSeCd=0057001`(기간) 이면서 종료일이 과거인 정책은 `published` 로 남는다(normalize 는 `0057003` 만 expired). 국토부 청년월세(5/29 마감)가 9/19 판정에서 적격이고 조합에 480만원으로 들어간다. 판정은 두더라도 조합·계획에서는 빼거나 '마감' 표시가 필요 (`demo_scenario.py` D-2) |

---

## 9. 참고 문서

| 문서 | 내용 |
| --- | --- |
| `README.md` | 진행 상태 · 실측 성능 · 시작하기 |
| `docs/ARCHITECTURE.md` | 전체 아키텍처 · ADR 6건 · 무료 티어 실사 · 성능 예산 |
| `docs/DB_SCHEMA.md` | 테이블 14종 설계 · 인덱스 전략 · 스토리지 예산 |
| [FE 이슈 #2](https://github.com/Claude-MCP-7team/FE/issues/2) | 계약 확정 논의 — **BE 답변 이미 등록됨** |

커밋 메시지는 **무엇을 했는지가 아니라 왜 그렇게 했는지**를 적는 형식을 유지하고 있다. `git log` 를 읽으면 설계 판단의 근거가 나온다.
