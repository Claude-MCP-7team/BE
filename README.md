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
| 테스트 | **327건** | — |
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
SNAPSHOT_PATH=snapshot.json uvicorn app.main:app --reload
#   POST /v1/judge               조건 → 전 정책 일괄 판정 (요약 + 적격/확인필요)
#   POST /v1/judge?include=all   부적격 근거까지
#   POST /v1/questions           역질문 큐 (필드당 1질문 · 상한 10)
#   POST /v1/combinations        조합 추천 (보수/최대 2안 × 상위 3개)
#   POST /v1/plan                신청 계획 (권장 착수일 · 서류 기준 할 일)
#   POST /v1/plan.ics            같은 계획을 캘린더(.ics)로
#   GET  /v1/policies/{id}       정책 상세 (공고 원문)
#   GET  /v1/meta/snapshot       스냅샷 버전·건수
#   GET  /healthz  /readyz       프로세스 생존 / 서비스 가능

# 5. 스냅샷 빌드 (검증 관문 통과분만 출력)
python -m batch.build_snapshot data/policies.json -o snapshot.json
#   --check-only     쓰지 않고 검사만
#   --allow-partial  검증 실패분을 빼고 빌드 (누락은 사용자에게 안 보인다)
#   --force          직전 대비 급감 검사를 건너뛴다

# 6. M0 데이터 정합성 조사 (G0 게이트 판정)
ONTONG_API_KEY=... python -m batch.collect.cli fetch --region 41
python -m batch.collect.cli survey data/raw/<타임스탬프>   # 원본으로 재조사
```

> ⚠️ 온통청년 API 의 엔드포인트·파라미터명은 **아직 미확정**이다. 공식 명세 페이지가
> 현재 개발 환경에서 접근 차단되어 원문 확인을 하지 못했다. 기본값은 2차 자료 기준이며
> 전부 환경변수로 덮어쓸 수 있다 (`ONTONG_BASE_URL`, `ONTONG_KEY_PARAM`, ...).
> 실제 응답이 다르더라도 코드 수정 없이 조사를 시작할 수 있도록 만들어 두었다.

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
- [ ] `app/db/` — asyncpg 풀 + 리포지토리 · 세션 저장 (BE-M1-2)
- [x] `batch/build_snapshot.py` — 스냅샷 빌더 · 검증 관문 (BE-M1-6~7)
- [x] `batch/holidays.py` — 한국천문연구원 특일 API 동기화 (BE-M5-2 데이터원)
- [ ] `batch/crawl` · `batch/agents` — 원문 크롤러 · A1/A2 오케스트레이션 (BE-M1-4~5)

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
