# 데모 고정 데이터

발표·E2E 전용으로 **저장소에 고정해 둔** 정책 5건과 사용자 1명이다.
`tests/e2e/test_demo_scenarios.py` 가 이 파일들을 읽어 시나리오 6종을 검증한다.

| 파일 | 내용 |
| --- | --- |
| `policies.demo.json` | `PolicySchema` 배열 5건 |
| `profile.demo.json` | `UserProfile` 1건 (25세·부천·미취업·중위소득 85%) |
| `responses/*.json` | 위 둘로 실제 API 를 호출해 **녹화한 응답** (FE Mock) |

## FE Mock — `responses/`

BE 없이 화면을 만들 수 있게, 위 데모 데이터로 실제 API 를 호출해 받은 응답을
그대로 커밋해 둔 것이다. `docs/contracts/` 는 스키마지 인스턴스가 아니라서, 필드가
실제로 어떤 값으로 채워지는지(빈 배열인지 `null` 인지, 날짜 형식이 무엇인지)를
알 수 없다.

| 파일 | 대응 호출 |
| --- | --- |
| `judge.json` / `judge.all.json` | `POST /v1/judge` (기본 / `?include=all`) |
| `questions.json` | `POST /v1/questions` |
| `combinations.json` | `POST /v1/combinations` |
| `plan.json` | `POST /v1/plan` |
| `policies.json` / `policy.detail.json` | `GET /v1/policies` / `/v1/policies/{id}` |
| `meta.snapshot.json` | `GET /v1/meta/snapshot` |
| `error.*.json` | 에러 4종 (`problem+json`) — **에러에도 화면이 있다** |

`judge.all.json` 한 건에 **네 가지 판정 상태가 전부** 들어 있다 (적격 2 · 부적격 2 ·
확인필요 1 · 충족예상일 1). 배지 4종을 한 응답으로 다 그려볼 수 있다.

**손으로 고치지 말 것.** 갱신은 이렇게 한다:

```bash
python tools/record_mock_responses.py
```

`tests/unit/test_mock_responses.py` 가 매번 앱을 다시 호출해 커밋된 것과 비교한다.
응답 모양이 바뀌었는데 녹화본이 그대로면 CI 가 실패한다 — 녹화본의 유일한 실패
모드가 '낡는 것'이고, 손으로 쓴 예시였다면 아무도 못 잡는다.

기준일은 `2026-10-01` 로 고정했다. 나이·충족예상일·신청일정이 오늘 날짜를 타면
매일 diff 가 나서 아무도 diff 를 읽지 않게 된다. 같은 이유로 `latency_ms` 와
`loaded_at` 도 고정값이다 — 실제 응답에서는 측정값이 들어온다.

## ⚠️ 합성 데이터다 — 실제 공고문이 아니다

**여기 실린 정책·문구·금액·담당부서 연락처는 전부 지어낸 것이다.**
실제 정부·지자체 공고를 인용한 것이 아니며, 사용자에게 보이는 서비스에
그대로 내보내면 안 된다. 구별할 수 있게 이렇게 표시해 두었다:

- `policy_id` 가 전부 `DEMO-` 로 시작한다
- 제목이 전부 `[데모]` 로 시작한다
- `source.origin_url` 이 `https://demo.invalid/...` 다 (`.invalid` 는 예약 TLD라 실제로 열리지 않는다)
- `source.api` 가 `"DEMO"` 다
- 전화번호가 `031-000-0000` 형태의 명백한 더미다

실제 공고문에서 추출한 데이터(A2 산출물)가 들어오면 이 파일을 **교체**한다.
교체 후에는 위 표시가 사라지므로, 실데이터인지 합성인지는 `source.api` 로 구분하면 된다.

## 왜 저장소에 고정하는가

실수집 결과(`data/policies.json`, `data/snapshot.json`)는 `.gitignore` 에 있다.
용량 때문이기도 하지만, 매일 바뀌는 데이터를 커밋하면 판정 결과가 커밋마다
달라져 회귀를 구분할 수 없기 때문이다.

그 대가로 **`ONTONG_API_KEY` 가 없는 기계에서는 스냅샷을 만들 수 없다.** 새 개발
환경이나 CI 에서 "정책 0건"으로 시작하면 API 는 `/readyz` 503 이고 화면은 빈
목록이다 — 장애가 아니라 정상 응답처럼 보여서 원인을 찾는 데 시간이 걸린다.
이 5건이 그 바닥을 받친다. 키 없이도 전 경로가 돈다.

## 5건이 각각 무엇을 보여주는가

시나리오 하나에 정책 하나를 대응시켜, 데모 중 어떤 정책을 눌러야 하는지가
분명하도록 골랐다.

| 정책 | 시나리오 | 이 사용자에게 |
| --- | --- | --- |
| `DEMO-GG-2026-0001` 경기도 청년 면접수당 | S1 신청 가능 | ELIGIBLE (50만원) |
| `DEMO-MOLIT-2026-0002` 청년 월세 한시 특별지원 | S2 부적격 + 사유 | INELIGIBLE (소득 85% > 60%) |
| `DEMO-BUCHEON-2026-0003` 부천시 청년 자립지원금 | S3 향후 가능 시점 | 거주 30/36개월 → **2027-04-01** 부터 |
| `DEMO-GG-2026-0004` 경기도 청년 자격증 응시료 지원 | S4 역질문 → 재판정 | NEEDS_INFO → 답변 후 ELIGIBLE |
| `DEMO-MOEL-2026-0005` 청년 구직활동 지원금 | S5 중복수혜 · S6 서류·일정 | ELIGIBLE (300만원), 상충 2건의 출처 |

**조합(S5)은 역질문에 답한 뒤에 갈린다.** 답하기 전에는 보수·최대가 모두
`{MOEL}` 300만원이고, 답한 뒤 최대안이 `{MOEL, GG-0004}` 320만원이 된다.
"질문에 답했더니 받을 수 있는 조합이 늘었다"가 그대로 보인다.

- 명시 상충(CONFIRMED): MOEL ↔ GG-0001 — 양쪽 안 모두에서 피한다
- 추정 상충(ESTIMATED): MOEL ↔ GG-0004 — 보수안만 피한다

**일정(S6)은 MOEL 에서 본다.** 서류 5종의 소요일이 0·0·0·0·3영업일인데 준비
기간은 합(3)이 아니라 **최댓값 3 + 제출 1 = 4영업일**이다. 같이 신청하고 가장
오래 걸리는 하나를 기다리기 때문이다. 납세증명서(D011)는 유효기간 30일이라
`issue_not_before = 2026-11-11` 로 "이 날 이후에 떼라"가 함께 나온다.

## 쓰는 법

```bash
python -m batch.build_snapshot data/demo/policies.demo.json -o data/snapshot.json
SNAPSHOT_PATH=data/snapshot.json YPC_FIXED_TODAY=2026-10-01 uvicorn app.main:app
```

`YPC_FIXED_TODAY` 를 고정하는 이유: 나이·마감·영업일 역산이 전부 오늘에
의존한다. 고정하지 않으면 시연 날짜에 따라 화면의 '남은 영업일'이 달라져
발표 내용과 어긋난다. 기준일은 제출일(`2026-10-01`)로 맞춰 두었다.

```bash
# 시나리오 6종 검증
pytest tests/e2e -v
```

## 고칠 때

정책을 더하거나 고치면 `tests/e2e/test_demo_scenarios.py` 가 먼저 깨진다.
그 테스트는 데모 대본이기도 해서, **숫자를 먼저 테스트에서 고치고 seed 를
맞추는 편**이 대본과 데이터가 어긋나지 않는다.
