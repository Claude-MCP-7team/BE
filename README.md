# YPC Backend — 청년정책 AI 코디네이터

> 청년정책을 추천하는 서비스가 아니라, **실제 공고문과 사용자 상황을 대조해 자격을 판정하고, 부적격 이유와 향후 가능 시점을 설명하며, 최적 조합과 신청 일정까지 제시하는** 서비스의 백엔드.

**역할 범위**: B(룰 엔진) · D(조합 솔버) · E(일정 역산) + DB · 배치 · 인프라
(프롬프트 설계는 AI 역할, 화면은 FE 역할 — 경계는 `docs/ARCHITECTURE.md` §4 참조)

---

## 문서

| 문서 | 내용 |
| --- | --- |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 전체 아키텍처 · ADR 6건 · 무료 티어 실사 · 성능 예산(실측) |
| [`docs/DB_SCHEMA.md`](docs/DB_SCHEMA.md) | 테이블 14종 설계 · 인덱스 전략 · 스토리지 예산 |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | **인수인계** — 현재 상태 · 작업 순서 · 설계 결정 · 개발 환경 함정 |

---

## 한 장 요약

**스냅샷 인메모리 아키텍처** — 배치가 만든 컴파일 정책 스냅샷을 API가 RAM에 통째로 올린다. 판정 요청은 DB를 한 번도 건드리지 않는다.

```
GitHub Actions (Nightly 02:00 KST, 무료)
  수집 → 크롤링 → LLM 구조화 → Neon Postgres → snapshot.msgpack.zst (~2MB)
                                                        │
                                                        ▼ 부팅 시 1회 로드
FastAPI 단일 컨테이너 (Render Free / Oracle Always Free)
  RAM 스냅샷 → 룰 엔진(numpy 벡터화) → 역질문 / 설명 / MWIS 솔버 / 일정 역산
```

### 실측 성능 (PostgreSQL 16.13 + Python 3.11 / numpy 2.4)

| 항목 | 측정값 | PRD 요구 |
| --- | --- | --- |
| 룰 평가 (600 정책 / 2,442 룰) | **0.14 ms** | — |
| 룰 평가 (3,000 정책 / 12,208 룰) | **0.51 ms** | — |
| 판정 API 1건 (예산 합계) | **~57 ms** | p95 ≤ 5,000 ms |
| 벡터화 ↔ 기준 구현 대조 | **7,200건 전부 일치** | — |
| 중복 질문 | **0건** (필드당 1질문) | G3: 0건 |
| 신청 계획 (3,000 정책 / 715 적격) | **10.1 ms** | p95 ≤ 50 ms |
| 영업일 역산 1회 | **1.5 µs** | — |
| 2026 공휴일 (대체공휴일 포함 21일) | **전수 일치** | G5: 100% |
| 테스트 | **384건** (DB 통합 28건 포함) | — |
| MWIS 정확성 (정점 3~13, 200건 상위3개) | **200/200 완전탐색 일치** | G4: 100% |
| DB 물리 크기 (3,000 정책) | **1.5 MB** | Neon Free 500 MB |

### 무료 인프라 구성

| 레이어 | 선택 | 근거 |
| --- | --- | --- |
| DB | **Neon Postgres Free** (0.5GB, 100 CU-h/월) | 판정이 DB를 안 쓰므로 scale-to-zero가 무해 |
| API | **Render Free** (1차) → Oracle Always Free (부하 시) | Docker 단일 이미지 = 이전 30분 |
| 배치 | **GitHub Actions cron** | API 프로세스와 분리, 시크릿·로그·재실행 무료 |
| 스토리지 | **Cloudflare R2** (10GB) | 스냅샷 · 원문 아카이브 |

---

## 시작하기

```bash
# 1. DB 스키마 적용
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f db/migrations/0001_init.sql

# 2. 제약조건 검증 ([MUST FAIL] 표기 항목은 에러가 나야 정상)
psql "$DATABASE_URL" -f db/verify_schema.sql

# 3. 엔진 성능 벤치마크
pip install numpy && python bench/engine_bench.py

# 4. API 띄우기
#    세션 저장을 쓰려면 DATABASE_URL 과 PROFILE_ENC_KEYS 가 함께 필요하다.
#    키가 없으면 세션 API 만 503 이고 판정은 정상 동작한다 (평문 저장 폴백 없음).
#    키 생성: python -c "from app.core.crypto import generate_key; print(generate_key())"
SNAPSHOT_PATH=snapshot.json \
  DATABASE_URL=postgresql://... \
  PROFILE_ENC_KEYS="1:<base64-32bytes>" \
  uvicorn app.main:app --reload
#   POST /v1/judge               조건 → 전 정책 일괄 판정 (요약 + 적격/확인필요)
#   POST /v1/judge?include=all   부적격 근거까지
#   POST /v1/questions           역질문 큐 (필드당 1질문 · 상한 10)
#   POST /v1/combinations        조합 추천 (보수/최대 2안 × 상위 3개)
#   POST /v1/plan                신청 계획 (권장 착수일 · 서류 기준 할 일)
#   POST /v1/plan.ics            같은 계획을 캘린더(.ics)로
#   POST   /v1/sessions          익명 세션 발급 (+프로필 저장)
#   GET    /v1/sessions/{id}     저장된 프로필 조회
#   PUT    /v1/sessions/{id}     프로필 전체 수정
#   DELETE /v1/sessions/{id}     즉시 파기
#   GET  /v1/policies/{id}       정책 상세 (공고 원문)
#   GET  /v1/meta/snapshot       스냅샷 버전·건수
#   GET  /healthz  /readyz       프로세스 생존 / 서비스 가능

# 5. 스냅샷 빌드 (검증 관문 통과분만 출력)
python -m batch.build_snapshot data/policies.json -o snapshot.json
#   --check-only     쓰지 않고 검사만
#   --allow-partial  검증 실패분을 빼고 빌드 (누락은 사용자에게 안 보인다)
#   --force          직전 대비 급감 검사를 건너뛴다

# 6. M0 데이터 정합성 조사 (G0 게이트 판정)
ONTONG_API_KEY=... python -m batch.collect.cli fetch --region 41000   # 41000=경기 전체
python -m batch.collect.cli survey data/raw/<타임스탬프>   # 원본으로 재조사

# 7. A2 공고문 구조화 (AI 역할) — 원본 → LLM → PolicySchema (build_snapshot 입력)
pip install -e ".[batch]"                                   # anthropic SDK 포함
python -m batch.agents.cli structure data/raw/<타임스탬프> --dry-run          # 대상 확인
ANTHROPIC_API_KEY=... python -m batch.agents.cli structure data/raw/<타임스탬프> \
    -o data/policies.json --limit 5                          # 5건만 과금
#   --ids A,B           특정 plcyNo 만
#   --announcements DIR <plcyNo>.txt 원문 공고문을 텍스트에 덧붙인다
#   응답은 data/a2-cache/ 에 캐시되고, 채택/거부 내역은 docs/a2/a2-report-*.json 에 남는다
```

> 온통청년 API 는 2026-09-14 실제 응답으로 확정했다 (`/go/ythip/getPlcy`, JSON, 전체 2,774건 =
> 1,000건 × 3페이지). 지역은 `zipCd` 에 법정동 5자리(`41000` = 경기 전체)로 준다.
> 상세는 `batch/collect/client.py` 도크스트링. 명세가 또 바뀌면 환경변수(`ONTONG_BASE_URL`,
> `ONTONG_KEY_PARAM`, ...)로 코드 수정 없이 덮어쓸 수 있다.

## 디렉터리

```
app/        실시간 API (engine / solver / planner / llm / api)
batch/      야간 배치 (collect / crawl / parse / agents / build_snapshot)
db/         마이그레이션 + 스키마 검증
bench/      성능 벤치마크
docs/       아키텍처 · DB 설계
tests/      unit / golden(정확도 하네스) / e2e
```

## 진행 상태

- [x] 아키텍처 설계 + ADR 6건
- [x] DB 스키마 설계 · `0001_init.sql` (PostgreSQL 16 검증 완료)
- [x] 성능 가설 프로토타입 검증 (룰 엔진 / MWIS 솔버)
- [x] `app/schemas/` — PolicySchema / UserProfile / JudgementResult + 밸리데이터 (테스트 53건)
- [x] `docs/contracts/` — JSON Schema 계약서 (AI 역할 자체검증용)
- [x] CI — 린트(역할 경계) · 마이그레이션 · 제약조건 · 테스트 · 계약 드리프트 · 벤치마크
- [x] `batch/collect/` — 수집기 + G0 게이트 조사 하네스 (BE-M0-1~4)
- [x] `app/engine/` — 룰 엔진 코어 · 충족 예상일 · 근거 조립 (BE-M2-3~5)
- [x] `app/api/` — 판정 API · 스냅샷 로더 · 헬스체크 (BE-M2-7)
- [x] `app/engine/questions.py` — 역질문 큐 병합·정렬·상한 (BE-M3-1~5)
- [x] `app/solver/` — 상충 그래프 · MWIS 정확해 · 보수/최대 2안 (BE-M4-1~7)
- [x] `app/planner/` — 영업일 달력 · 권장 착수일 역산 · ICS 내보내기 (BE-M5-2~4)
- [x] 서류 마스터 36종 · 유효기간 구간 계산 (BE-M5-1, `data/documents/master_v2.csv`)
- [ ] 서류 마스터 검증 — 전 항목이 아직 `확인필요` 상태
- [x] `app/db/` — asyncpg 풀 · 세션 저장 (AES-256-GCM) · `/v1/sessions` (BE-M1-2)
- [x] `batch/build_snapshot.py` — 스냅샷 빌더 · 검증 관문 (BE-M1-6~7)
- [x] `batch/holidays.py` — 한국천문연구원 특일 API 동기화 (BE-M5-2 데이터원)
- [x] `batch/collect/normalize` — API 구조화 필드 → PolicySchema (LLM 없이 실데이터 스냅샷)
- [x] `batch/agents/` — A2 공고문 구조화 · 인용문 원문 대조 · 병합 · 리포트 (AI-M1~M3, `app/llm/prompts/a2_structure.md`)
- [ ] `batch/crawl` — 원문 공고문 크롤러 (BE-M1-4). 지금은 API 자유 텍스트 필드만 구조화한다
- [ ] A2 실제 공고문 3~5건 정확도 검증 (AI-M2-3) — API 키 확보 후 `--limit 5` 로 실행

## 계약면

| 계약 | 당사자 | 확정 | 위치 |
| --- | --- | --- | --- |
| C1 `PolicySchema` | AI ↔ BE | G1 (10/02 Freeze) | `app/schemas/policy.py` · `docs/contracts/policy_schema.json` |
| C2 `JudgementResult` | BE ↔ FE | 10/07 | `app/schemas/judgement.py` · `docs/contracts/judgement_result.json` |
| — 역질문 큐 | BE ↔ FE | 10/07 | `app/schemas/question.py` |
| — 조합 추천 | BE ↔ FE | 10/07 | `app/schemas/combination.py` |
| — 신청 계획 | BE ↔ FE | 10/07 | `app/schemas/plan.py` |

서류 발급 소요일·수수료·유효기간의 권위는 `data/documents/master_v2.csv` 다 (36종).
공고문이 다른 값을 적어도 마스터가 이긴다 — 공고는 수백 건이 제각각 틀리지만
마스터는 한 곳에서 고치면 전부 고쳐진다. 마스터에 없는 서류만 공고 값으로 떨어진다.

룰이 참조할 수 있는 사용자 필드 목록은 `docs/contracts/rule_fields.json` 에 있다.
AI 역할이 이 목록에 없는 `field` 를 만들면 BE 밸리데이터가 `UNKNOWN_FIELD` 로 거부한다.
