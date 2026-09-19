# 수동 구조화 데모 정책 (API 비용 0)

실제 공고문을 손으로 옮긴 데이터. 온통청년 API 키나 Anthropic API 키 없이 A2 → 빌더 →
판정 API 까지 실데이터로 돌려보기 위한 것이다.

```
raw/page-001.json   온통청년 API 응답과 같은 모양의 레코드 (출처는 각 refUrlAddr1)
a2/<plcyNo>.json    A2 출력 계약(batch/agents/contract.py)에 맞춰 사람이 작성한 구조화 응답
```

```bash
python -m batch.agents.cli structure data/manual/raw --responses data/manual/a2 -o data/manual/policies.json
python -m batch.build_snapshot data/manual/policies.json -o data/manual/snapshot.json
SNAPSHOT_PATH=data/manual/snapshot.json uvicorn app.main:app
```

`a2/` 의 응답은 모델 응답과 **완전히 같은 검증**(인용문 원문 대조 · API 코드 룰과의 불일치 ·
`validate_policy`)을 거친다. 손으로 썼다고 통과가 보장되지 않으며, 그래야 이 데이터로
검증 경로 자체를 시연할 수 있다.

| plcyNo | 정책 | 왜 골랐나 |
| --- | --- | --- |
| GG-12048 | 포천청년비전센터 10월 프로그램 | "거주 **또는** 재학·재직" OR 조건 — 룰로 못 옮기는 조건이 `unrepresentable_conditions` 로 남는 예. 서류 3종이 마스터(D002/D022/D032)에 매칭된다 |
| GG-12010 | 가평군 청년 1인가구 월세 지원 (3분기) | 소득 150%·1인 가구·미혼 → 역질문 3개. 청년월세 한시 특별지원과 `explicit_policy` 상충 + 타 지자체 주거지원 `category_overlap`. 서류 10종. 취업 상태에 따라 소득 산정 기준이 달라 소득 룰은 `ambiguous/ESTIMATED` |
| GG-12023 | 용인청년 중개보수·이사비 지원 (추가 모집) | 소득 180% 역질문 → 답변 후 재판정(시나리오 4). "2026.1.1~6.30 사이 전입" 처럼 **기간 창** 조건은 `residence_months_continuous(>=)` 로 표현 불가 → `unrepresentable` |

데모 흐름은 `python tools/demo_scenario.py` 로 재현한다 (판정 → 역질문 → 재판정 → 조합 → 계획).
