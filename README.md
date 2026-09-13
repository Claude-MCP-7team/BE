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
| 룰 평가 (3,000 정책) | **0.068 ms** | — |
| 판정 API 1건 (예산 합계) | **~43 ms** | p95 ≤ 5,000 ms |
| MWIS 솔버 (정점 30) | **2.1 ms** | p95 ≤ 2,000 ms |
| MWIS 정확성 (정점 ≤12, 20건) | **20/20 완전탐색 일치** | 100% |
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

# 4. M0 데이터 정합성 조사 (G0 게이트 판정)
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
- [ ] `app/db/` — asyncpg 풀 + 리포지토리 (BE-M1-2)
- [ ] `app/engine/` — 룰 엔진 코어 (BE-M2-3)
- [ ] `0002_seed_document.sql` — 서류 마스터 30~50종 (BE-M5-1, 선행 권장)

## 계약면

| 계약 | 당사자 | 확정 | 위치 |
| --- | --- | --- | --- |
| C1 `PolicySchema` | AI ↔ BE | G1 (10/02 Freeze) | `app/schemas/policy.py` · `docs/contracts/policy_schema.json` |
| C2 `JudgementResult` | BE ↔ FE | 10/07 | `app/schemas/judgement.py` · `docs/contracts/judgement_result.json` |

룰이 참조할 수 있는 사용자 필드 목록은 `docs/contracts/rule_fields.json` 에 있다.
AI 역할이 이 목록에 없는 `field` 를 만들면 BE 밸리데이터가 `UNKNOWN_FIELD` 로 거부한다.
