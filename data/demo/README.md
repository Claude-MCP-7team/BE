# 데모 고정 데이터

발표·E2E 전용으로 **저장소에 고정해 둔** 공고 31건과 사용자 1명이다.
`tests/e2e/test_demo_scenarios.py` 가 이 파일들을 읽어 시나리오를 검증한다.

| 파일 | 내용 |
| --- | --- |
| `policies.demo.json` | `PolicySchema` 배열 31건 — **실제 공고다** (아래 출처) |
| `profile.demo.json` | `UserProfile` — 23세·가평·1인가구·소득 미입력 |
| `responses/*.json` | 위 둘로 실제 API 를 호출해 **녹화한 응답** (FE Mock) |

## 실제 공고다 — 2026-09-24 수집

두 갈래에서 왔다. 둘 다 `data/manual/raw/` 에 원본이 있고, `policy_id` 와
`source.origin_url` 로 원문을 확인할 수 있다.

| 출처 | 건수 | 방법 |
| --- | --- | --- |
| `page-001~003` | 11건 | 경기청년포털·잡아바 공고문을 손으로 옮긴 것 + 온통청년 API |
| `page-004` | 20건 | 온통청년 OPEN API 수집 (`batch.collect.cli fetch --region 41000`) |

기준일 `2026-09-24` 에 **23건이 게시**되고 8건은 신청기간이 지나 빠진다.

### 이 목록은 시간이 지나면 줄어든다

신청기간이 지난 공고는 빌더가 걸러낸다. seed 는 31건이지만 **스냅샷에 들어가는
건 그 중 기간이 열려 있는 것뿐**이다:

```
2026-09-24 → 23건    2026-10-01 → 17건    (제출일)
2026-11-01 → 12건    2026-12-01 →  2건
```

0건이 되면 `batch.build_snapshot` 이 거부하고 종료코드 1 을 내므로, Docker 이미지
빌드가 실패한다 (빈 목록이 배포되는 경로는 없다 — `Dockerfile` 주석 참고).
**그때가 공고를 새로 수집해야 하는 시점이다.**

수집은 이렇게 한다 (`ONTONG_API_KEY` 필요):

```bash
python -m batch.collect.cli fetch --region 41000
python tools/pick_open_notices.py data/raw/<타임스탬프> --region 41 --nationwide --limit 20 \
    -o data/manual/raw/page-005.json
python -m batch.agents.cli structure data/manual/raw --responses data/manual/a2 \
    -o data/demo/policies.demo.json
```

기준일은 `2026-09-24` 로 고정했다 (`tests/e2e/test_demo_scenarios.py` 의
`DEMO_TODAY`, `tools/record_mock_responses.py` 의 `FIXED_TODAY`). 둘이 갈라지면
FE 가 보는 Mock 과 발표에서 도는 화면이 달라진다.

## 사용자 한 명이 네 배지를 다 본다

| 판정 | 건수 | 대표 |
| --- | --- | --- |
| 적격 | 7 | 매장유산 미정리유물 (`20260317005400112169`) |
| 확인필요 | 2 | 가평 월세 · 농식품 바우처 — 소득을 안 밝혀서 |
| 충족예상 | 1 | 경기도 청년기본소득 — **2027-03-15** 부터 (24세) |
| 부적격 | 14 | 포천 프로그램 (`GG-12048`) — 지역 불일치 |

**역질문 하나가 정책 둘을 반대로 가른다.** 소득을 `85` 로 답하면 가평 월세는
적격(150% 이하), 농식품 바우처는 부적격(32% 이하)이 된다. 질문을 정책별이 아니라
필드별로 병합하는 이유가 여기서 보인다 — 정책마다 물었다면 같은 소득을 두 번 답한다.

## 서류·금액·연락처는 A2 를 거친 공고만 가지고 있다

API 구조화 필드에는 **서류·금액·연락처가 없다.** 공고문 본문을 읽어야 나오고,
그게 A2(LLM)가 하는 일이다. `ANTHROPIC_API_KEY` 가 없어서 `data/manual/a2/` 에
**손으로 쓴 응답 15건**을 두고 `--responses` 로 태웠다. 손으로 썼다고 통과가
보장되지는 않는다 — 인용문을 원문과 대조하는 같은 검증을 지난다.

게시 23건 중:

| | 가진 공고 |
| --- | --- |
| 서류 | 3건 (`GG-12048`, `GG-12010`, `20260317005400112169`) |
| 금액 | 4건 |
| 담당부서 연락처 | 4건 — **나머지 19건은 `null` 이다** |

**FE 는 `dept_tel` 을 필수로 그리면 안 된다.** 카드 대부분이 깨진다.
`tests/unit/test_mock_responses.py` 가 빈 것과 있는 것이 둘 다 Mock 에 들어
있는지 확인한다.

## 시나리오 5(상충 조합)는 실데이터로 재현되지 않는다

가평 월세(`GG-12010`)는 국토부 청년월세를 **명시적으로 배제한다** — 상충 해소를
보여줄 진짜 재료다. 그런데 국토부 공고는 5월에 마감돼서, 두 공고가 한 스냅샷에
같이 설 수 없다. 그래서 보수 조합과 최대 조합이 같은 결과로 나온다.

`test_시나리오5_상충_선언은_있지만_기간이_겹치지_않는다` 가 이 사실을 단언한다.
신청기간이 겹치는 주거 공고를 새로 수집하면 그 테스트가 깨지고, 그때 시나리오 5 의
두 갈래를 복원하면 된다 (HANDOFF §8 #16).

## FE Mock — `responses/`

BE 없이 화면을 만들 수 있게, 위 데이터로 실제 API 를 호출해 받은 응답을
그대로 커밋해 둔 것이다. `docs/contracts/` 는 스키마지 인스턴스가 아니라서, 필드가
실제로 어떤 값으로 채워지는지(빈 배열인지 `null` 인지, 날짜 형식이 무엇인지)를
알 수 없다.

| 파일 | 대응 호출 | 프로필 |
| --- | --- | --- |
| `judge.json` / `judge.all.json` | `POST /v1/judge` (기본 / `?include=all`) | 그대로 |
| `questions.json` | `POST /v1/questions` | 그대로 |
| `combinations.json` | `POST /v1/combinations` | + 소득 답변 |
| `plan.json` | `POST /v1/plan` | + 소득 답변 |
| `policies.json` / `policy.detail.json` | `GET /v1/policies` / `/v1/policies/{id}` | — |
| `meta.snapshot.json` | `GET /v1/meta/snapshot` | — |
| `error.*.json` | 에러 4종 (`problem+json`) — **에러에도 화면이 있다** | — |

조합·일정은 **역질문에 답한 뒤**로 녹화했다. 미확인 상태로 부르면 조합에 담길 게
적고 서류 배열이 비어서, FE 가 서류 화면을 한 번도 못 그려 본 채 제출하게 된다.

**손으로 고치지 말 것.** 갱신은 이렇게 한다:

```bash
python tools/record_mock_responses.py
```

`tests/unit/test_mock_responses.py` 가 매번 앱을 다시 호출해 커밋된 것과 비교한다.
응답 모양이 바뀌었는데 녹화본이 그대로면 CI 가 실패한다 — 녹화본의 유일한 실패
모드가 '낡는 것'이고, 손으로 쓴 예시였다면 아무도 못 잡는다.

녹화기는 seed 를 **빌더에 통과시킨 뒤** 적재한다. 운영이 반드시 빌더를 지나므로,
녹화본도 같은 문을 지나야 FE 가 보는 것과 배포된 것이 같아진다. 빼먹으면 마감된
공고까지 목록에 실려, FE 는 실제로는 오지 않는 데이터로 화면을 만들게 된다.

`latency_ms` 와 `loaded_at` 은 고정값이다 — 실제 응답에서는 측정값이 들어온다.

## 이전 버전 (합성 데이터)

2026-09-24 이전에는 `DEMO-` 로 시작하는 지어낸 공고 5건이 들어 있었다.
`git log data/demo/policies.demo.json` 에서 볼 수 있다. 합성 데이터로 돌아가면
`test_데모_seed_는_실공고다` 와 `test_녹화본이_실공고에서_나왔다` 가 실패한다 —
합성 공고를 쓰는 것 자체는 선택이지만, 실데이터인 줄 알고 쓰는 건 아니다.
