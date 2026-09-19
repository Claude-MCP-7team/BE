"""A2 구조화 — 검증·병합 로직을 네트워크 없이 검사한다.

LLM 은 가짜 객체로 대체한다. 여기서 보려는 건 모델의 품질이 아니라 **모델이 틀렸을 때
파이프라인이 그것을 통과시키지 않는가** 다.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path
from typing import Any

from app.schemas.enums import (
    KNOWN_FIELDS,
    BenefitType,
    Category,
    Confidence,
    ConflictType,
    Operator,
)
from app.schemas.validate import validate_policy
from batch.agents.cli import CachedLLM
from batch.agents.contract import A2_OUTPUT_SCHEMA, CONDITION_SCHEMA, STRUCTURABLE_FIELDS
from batch.agents.questions import DEFAULT_QUESTION_TEMPLATES
from batch.agents.structure import (
    UNREPRESENTABLE_MARKER,
    merge,
    resolve_conflict_targets,
    structure_policy,
)
from batch.agents.text import assemble_text, quote_found
from batch.collect.normalize import record_to_policy

ANNOUNCEMENT = """\
지원대상: 신청일 기준 용인시에 6개월 이상 계속 거주하고 있는 만 19세 이상 34세 이하 청년
가구소득이 기준 중위소득 150% 이하인 자
※ 최근 2년 이내 유사한 청년 지원사업에 참여한 자는 제외
※ 무주택 세대주에 한함
지원내용: 월 20만원, 최대 12개월 지원
중복수혜: 청년월세 한시 특별지원과 중복 수혜 불가
제출서류: 주민등록등본 1부, 건강보험료 납부확인서
문의: 청년정책과 031-123-4567
"""


def rec(**over: str) -> dict[str, str]:
    base = {
        "plcyNo": "R1",
        "plcyNm": "용인시 청년 월세 지원",
        "lclsfNm": "주거",
        "pvsnInstGroupCd": "0054002",
        "rgtrInstCd": "1",
        "rgtrUpInstCd": "2",
        "sprvsnInstCdNm": "용인시",
        "zipCd": "41460",
        "sprtTrgtMinAge": "19",
        "sprtTrgtMaxAge": "39",  # 텍스트(34)와 일부러 다르게 — 불일치 검사용
        "earnCndSeCd": "0043002",  # 소득 조건 있음 → normalize 가 needs_review 로 남긴다
        "aplyPrdSeCd": "0057002",
        "refUrlAddr1": "https://example.kr/r1",
        "plcyExplnCn": ANNOUNCEMENT,
    }
    base.update(over)
    return base


def cond(**over: Any) -> dict[str, Any]:
    c: dict[str, Any] = {
        "kind": "eligibility",
        "field": "residence_months_continuous",
        "op": ">=",
        "value_number": 6,
        "value_text": None,
        "value_bool": None,
        "value_list": None,
        "unit": "개월",
        "source_quote": "용인시에 6개월 이상 계속 거주",
        "time_satisfiable": True,
        "askable": True,
        "question_template": None,
        "ambiguous": False,
        "confidence": "CONFIRMED",
        "note": None,
    }
    c.update(over)
    return c


def output(**over: Any) -> dict[str, Any]:
    o: dict[str, Any] = {
        "conditions": [],
        "unrepresentable_conditions": [],
        "benefit": {
            "type": None,
            "amount_krw": None,
            "duration_months": None,
            "estimated_total_krw": None,
            "amount_confidence": "ESTIMATED",
            "source_quote": None,
        },
        "period": {
            "apply_start": None,
            "apply_end": None,
            "is_rolling": False,
            "source_quote": None,
        },
        "documents": [],
        "conflicts": [],
        "dept": {"name": None, "tel": None, "source_quote": None},
    }
    o.update(over)
    return o


class FakeLLM:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[dict[str, Any]] = []
        self.last_usage = {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0}

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self.data


def base_policy():
    return record_to_policy(rec(), crawled_at="20260919T000000Z")


def fields(rules):
    return {r.field: r for r in rules}


# --- 인용문 대조 -------------------------------------------------------------


def test_원문에_있는_인용문만_룰이_된다():
    text = assemble_text(rec())
    good = cond()
    bad = cond(
        field="household_income_ratio_median",
        op="<=",
        value_number=150,
        source_quote="기준 중위소득 150퍼센트 이하",  # 원문은 '150% 이하' — 다듬어 쓴 인용
    )
    merged, report = merge(base_policy(), output(conditions=[good, bad]), text)

    r = fields(merged.eligibility)
    assert r["residence_months_continuous"].value == 6
    assert r["residence_months_continuous"].basis == "A2 공고문 구조화"
    assert "household_income_ratio_median" not in r
    assert [x.code for x in report.rejected] == ["QUOTE_NOT_VERBATIM"]
    assert "household_income_ratio_median" in merged.quality.needs_review_fields
    assert report.accepted_conditions == 1 and report.proposed_conditions == 2
    assert validate_policy(merged) == []


def test_인용문_대조는_공백만_관대하다():
    text = "만 19세 이상\n  34세 이하 청년"
    assert quote_found(text, "만 19세 이상 34세 이하")
    assert not quote_found(text, "만 19세이상 34세 이하")
    assert not quote_found(text, "")


# --- API 룰과의 관계 ----------------------------------------------------------


def test_텍스트가_API_코드와_다르게_말하면_어느_쪽도_확정하지_않는다():
    text = assemble_text(rec())
    age = cond(
        field="age",
        op="between",
        value_number=None,
        value_list=[19, 34],
        unit="세",
        source_quote="만 19세 이상 34세 이하 청년",
        askable=False,
    )
    base = base_policy()
    merged, report = merge(base, output(conditions=[age]), text)

    assert fields(merged.eligibility)["age"].value == [19, 39]  # API 룰 유지
    assert report.disagreements == ["age: API between [19, 39] vs 텍스트 between [19, 34]"]
    assert "age" in merged.quality.needs_review_fields


def test_텍스트가_API_코드와_같으면_중복_룰을_만들지_않는다():
    text = assemble_text(rec(sprtTrgtMaxAge="34"))
    age = cond(
        field="age",
        op="between",
        value_number=None,
        value_list=[19, 34],
        source_quote="만 19세 이상 34세 이하 청년",
    )
    merged, report = merge(
        record_to_policy(rec(sprtTrgtMaxAge="34")), output(conditions=[age]), text
    )
    assert [r.field for r in merged.eligibility].count("age") == 1
    assert report.disagreements == [] and report.rejected == []
    assert report.agreed_conditions == 1 and merged.quality.parse_confidence == 1.0


def test_텍스트로_확정된_소득_조건은_needs_review_에서_빠진다():
    base = base_policy()
    assert "household_income_ratio_median" in base.quality.needs_review_fields
    income = cond(
        field="household_income_ratio_median",
        op="<=",
        value_number=150,
        unit="%",
        source_quote="기준 중위소득 150% 이하",
    )
    merged, _ = merge(base, output(conditions=[income]), assemble_text(rec()))
    assert fields(merged.eligibility)["household_income_ratio_median"].value == 150
    assert "household_income_ratio_median" not in merged.quality.needs_review_fields


# --- 부속 플래그 ---------------------------------------------------------------


def test_제외조항은_exclusions_로_가고_역질문_문구가_채워진다():
    excl = cond(
        kind="exclusion",
        field="similar_program_participation_2y",
        op="==",
        value_number=None,
        value_bool=False,
        unit=None,
        source_quote="최근 2년 이내 유사한 청년 지원사업에 참여한 자는 제외",
        time_satisfiable=False,
        question_template=None,
    )
    merged, _ = merge(base_policy(), output(conditions=[excl]), assemble_text(rec()))
    assert merged.eligibility == base_policy().eligibility
    r = merged.exclusions[0]
    assert r.value is False and r.askable
    assert r.question_template == DEFAULT_QUESTION_TEMPLATES["similar_program_participation_2y"]
    assert validate_policy(merged) == []


def test_모델이_준_질문_문구가_있으면_그것을_쓴다():
    c = cond(question_template="용인시에 전입한 날짜가 언제인가요?")
    merged, _ = merge(base_policy(), output(conditions=[c]), assemble_text(rec()))
    assert fields(merged.eligibility)["residence_months_continuous"].question_template == (
        "용인시에 전입한 날짜가 언제인가요?"
    )


def test_시간으로_충족되지_않는_필드의_time_satisfiable_은_무시된다():
    c = cond(
        field="household_income_ratio_median",
        op="<=",
        value_number=150,
        source_quote="기준 중위소득 150% 이하",
        time_satisfiable=True,
    )
    merged, report = merge(base_policy(), output(conditions=[c]), assemble_text(rec()))
    assert fields(merged.eligibility)["household_income_ratio_median"].time_satisfiable is False
    assert report.rejected == []


def test_ambiguous_는_CONFIRMED_로_남지_않는다():
    c = cond(ambiguous=True, confidence="CONFIRMED")
    merged, _ = merge(base_policy(), output(conditions=[c]), assemble_text(rec()))
    r = fields(merged.eligibility)["residence_months_continuous"]
    assert r.ambiguous and r.confidence == "ESTIMATED"


def test_같은_룰을_두_번_내면_하나만_남는다():
    merged, report = merge(base_policy(), output(conditions=[cond(), cond()]), assemble_text(rec()))
    assert [r.field for r in merged.eligibility].count("residence_months_continuous") == 1
    assert report.accepted_conditions == 1


def test_op_에_맞는_value_칸이_비면_버린다():
    c = cond(op="between", value_number=6, value_list=None)
    merged, report = merge(base_policy(), output(conditions=[c]), assemble_text(rec()))
    assert "residence_months_continuous" not in fields(merged.eligibility)
    assert report.rejected[0].code == "BAD_VALUE_SHAPE"


# --- 규칙화 불가 · 혜택 · 서류 · 상충 · 담당부서 ---------------------------------


def test_규칙화_불가_조건은_표식으로_남는다():
    u = {"summary": "무주택 세대주", "source_quote": "무주택 세대주에 한함", "reason": "필드 없음"}
    fake = {"summary": "x", "source_quote": "원문에 없는 문장", "reason": "y"}
    merged, report = merge(
        base_policy(), output(unrepresentable_conditions=[u, fake]), assemble_text(rec())
    )
    assert report.unrepresentable == [u]
    assert UNREPRESENTABLE_MARKER in merged.quality.needs_review_fields


def test_혜택은_인용이_확인될_때만_채운다():
    text = assemble_text(rec())
    b = {
        "type": "cash_monthly",
        "amount_krw": 200000,
        "duration_months": 12,
        "estimated_total_krw": 2400000,
        "amount_confidence": "ESTIMATED",
        "source_quote": "월 20만원, 최대 12개월 지원",
    }
    merged, _ = merge(base_policy(), output(benefit=b), text)
    assert merged.benefit.amount_krw == 200000 and merged.benefit.estimated_total_krw == 2400000

    merged, report = merge(
        base_policy(), output(benefit={**b, "source_quote": "월 이십만원"}), text
    )
    assert merged.benefit.amount_krw is None
    assert report.rejected[0].path == "benefit"


def test_서류와_상충은_검증을_거쳐_붙는다():
    text = assemble_text(rec())
    docs = [
        {"name": "주민등록등본", "issuer": None, "source_quote": "주민등록등본 1부"},
        {"name": "주민등록등본", "issuer": None, "source_quote": "주민등록등본 1부"},  # 중복
        {"name": "소득금액증명원", "issuer": None, "source_quote": "소득금액증명원"},  # 원문에 없음
    ]
    conflicts = [
        {
            "type": "explicit_policy",
            "target_policy_name": "청년월세 한시 특별지원",
            "target_category": None,
            "target_authority": None,
            "source_quote": "청년월세 한시 특별지원과 중복 수혜 불가",
            "confidence": "CONFIRMED",
        },
        {  # 분류 상충인데 대상 분류가 없다 → BE 밸리데이터가 거부
            "type": "category_overlap",
            "target_policy_name": None,
            "target_category": None,
            "target_authority": None,
            "source_quote": "중복 수혜 불가",
            "confidence": "ESTIMATED",
        },
    ]
    merged, report = merge(base_policy(), output(documents=docs, conflicts=conflicts), text)
    assert [d.name for d in merged.documents] == ["주민등록등본"]
    assert [c.target_policy_name for c in merged.conflicts] == ["청년월세 한시 특별지원"]
    assert {x.code for x in report.rejected} == {"QUOTE_NOT_VERBATIM", "CONFLICT_TARGET_MISSING"}
    assert validate_policy(merged) == []


def test_분류_상충의_target_category_는_솔버가_비교하는_enum_이다():
    # graph.py 는 target_category == meta.category 로 간선을 만든다. 자유 문구면 간선 0개.
    schema = A2_OUTPUT_SCHEMA["properties"]["conflicts"]["items"]["properties"]
    assert set(schema["target_category"]["enum"]) == {*typing.get_args(Category), None}
    assert set(schema["target_benefit_type"]["enum"]) == {*typing.get_args(BenefitType), None}

    c = {
        "type": "category_overlap",
        "target_policy_name": None,
        "target_category": "housing",
        "target_benefit_type": "cash_monthly",
        "target_authority": None,
        "source_quote": "청년월세 한시 특별지원과 중복 수혜 불가",
        "confidence": "ESTIMATED",
    }
    merged, _ = merge(base_policy(), output(conflicts=[c]), assemble_text(rec()))
    assert merged.conflicts[0].target_category == "housing"
    assert merged.conflicts[0].target_benefit_type == "cash_monthly"


def test_명시_상충의_정책명은_같은_묶음의_제목과_이어진다():
    text = assemble_text(rec())
    c = {
        "type": "explicit_policy",
        "target_policy_name": "청년월세 한시 특별지원",
        "target_category": None,
        "target_benefit_type": None,
        "target_authority": None,
        "source_quote": "청년월세 한시 특별지원과 중복 수혜 불가",
        "confidence": "CONFIRMED",
    }
    a, _ = merge(base_policy(), output(conflicts=[c]), text)
    b = record_to_policy(rec(plcyNo="R2", plcyNm="2026년 청년월세 한시 특별지원 (국토교통부)"))
    b2 = record_to_policy(rec(plcyNo="R3", plcyNm="청년월세 한시 특별지원 2차"))

    policies = [a, b]
    assert resolve_conflict_targets(policies) == {}
    assert policies[0].conflicts[0].target_policy_ids == ["R2"]

    # 후보가 둘이면 잇지 않는다 — 틀린 간선은 받을 수 있는 조합을 몰래 지운다
    policies = [a, b, b2]
    assert resolve_conflict_targets(policies) == {"R1": ["청년월세 한시 특별지원"]}
    assert policies[0].conflicts[0].target_policy_ids == []


def test_담당부서_전화번호는_인용_없이_받지_않는다():
    text = assemble_text(rec())
    merged, _ = merge(
        base_policy(),
        output(
            dept={
                "name": "청년정책과",
                "tel": "031-123-4567",
                "source_quote": "청년정책과 031-123-4567",
            }
        ),
        text,
    )
    assert merged.meta.dept.tel == "031-123-4567"
    assert merged.meta.dept.name == "용인시"  # API 값이 있으면 유지

    merged, _ = merge(
        base_policy(), output(dept={"name": None, "tel": "02-000-0000", "source_quote": None}), text
    )
    assert merged.meta.dept.tel is None


# --- 오케스트레이션 ------------------------------------------------------------


def test_structure_policy_는_텍스트와_확정된_조건을_함께_보낸다():
    fake = FakeLLM(output(conditions=[cond()]))
    merged, report = structure_policy(base_policy(), rec(), fake, system_prompt="SYS")
    user = fake.calls[0]["user"]
    assert "[정책 설명]" in user and "[이미 확정된 조건]" in user and "age between" in user
    assert "region_code" not in user  # 지역은 모델이 만들지 않는다
    assert fake.calls[0]["schema"] is A2_OUTPUT_SCHEMA
    assert report.usage["input_tokens"] == 10
    assert fields(merged.eligibility)["residence_months_continuous"].value == 6


def test_내용_필드가_없으면_호출하지_않는다():
    fake = FakeLLM(output())
    record = {k: v for k, v in rec().items() if k != "plcyExplnCn"}  # 정책명·기관명만 남는다
    merged, report = structure_policy(record_to_policy(record), record, fake, system_prompt="SYS")
    assert fake.calls == [] and report.text_chars == 0
    assert merged == record_to_policy(record)


def test_응답_캐시는_같은_입력을_다시_과금하지_않는다(tmp_path: Path):
    fake = FakeLLM(output(conditions=[cond()]))
    llm = CachedLLM(fake, tmp_path)
    a = llm.complete_json(system="S", user="U", schema={})
    b = llm.complete_json(system="S", user="U", schema={})
    llm.complete_json(system="S2", user="U", schema={})
    assert a == b and len(fake.calls) == 2 and llm.hits == 1
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))["conditions"]


# --- 계약 드리프트 -------------------------------------------------------------


def test_A2_계약의_enum_은_BE_enum_과_같다():
    assert set(STRUCTURABLE_FIELDS) <= KNOWN_FIELDS
    assert {"region_code", "received_policy_ids"}.isdisjoint(STRUCTURABLE_FIELDS)
    assert set(CONDITION_SCHEMA["properties"]["op"]["enum"]) <= set(typing.get_args(Operator))
    assert set(CONDITION_SCHEMA["properties"]["confidence"]["enum"]) == set(
        typing.get_args(Confidence)
    )
    conflict_schema = A2_OUTPUT_SCHEMA["properties"]["conflicts"]["items"]
    assert set(conflict_schema["properties"]["type"]["enum"]) == set(typing.get_args(ConflictType))
    # 구조화 출력 제약: 모든 객체가 additionalProperties=False + required 전체
    assert A2_OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(A2_OUTPUT_SCHEMA["required"]) == set(A2_OUTPUT_SCHEMA["properties"])


def test_역질문_템플릿은_물을_수_있는_모든_필드를_덮는다():
    askable = set(STRUCTURABLE_FIELDS) - {"age"}
    assert set(DEFAULT_QUESTION_TEMPLATES) == askable


def test_텍스트_조립은_Cn_접미사_필드를_놓치지_않는다():
    text = assemble_text({"plcyNm": "A", "newFieldCn": "새 항목", "zipCd": "41460", "etcCd": "x"})
    assert "[정책명]\nA" in text and "[newFieldCn]\n새 항목" in text
    assert "41460" not in text and "etcCd" not in text
