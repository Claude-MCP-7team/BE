"""판정 입력 값 가드 — 숫자의 범위와 enum 의 값 집합.

이 테스트가 지키는 것: **비교는 되지만 뜻이 다른 값**이 통과하지 않는 것. 타입
검사로는 못 잡는다 — 원 단위 3000000 도 int 라서 `<= 150` 비교가 정상 수행되고,
"혼자 살아요"도 str 이라 `== "single"` 비교가 정상 수행된다. 둘 다 예외가 아니라
그럴듯한 부적격 목록으로 나가서 아무도 신고하지 않는다.

`answers` 가 자유 dict 인 탓에 온보딩(`core`, Literal 타입)으로 들어온 값은 막히고
역질문으로 들어온 같은 값은 통과하는 구멍이 두 번 생겼다. 여기서 양쪽을 함께 본다.
"""

from __future__ import annotations

import msgspec
import pytest

from app.schemas.user import FIELD_BOUNDS, UserProfile

CORE = {"birth_date": "1998-03-14", "region_code": "41465"}


def _decode(raw: dict[str, object]) -> UserProfile:
    return msgspec.json.decode(msgspec.json.encode(raw), type=UserProfile)


@pytest.mark.parametrize(
    "value",
    [
        3_000_000,  # 원 단위를 그대로 보낸 경우 — 이게 진짜 막고 싶은 값
        3_000_000_000,
        1001,
        -1,
    ],
)
def test_소득_범위밖은_거부된다(value: int) -> None:
    with pytest.raises(msgspec.ValidationError) as e:
        _decode({"core": {**CORE, "household_income_ratio_median": value}})
    # 메시지에 필드명과 받은 값이 들어가야 FE 가 뭘 잘못 보냈는지 안다
    assert "household_income_ratio_median" in str(e.value)
    assert str(value) in str(e.value)


@pytest.mark.parametrize("value", [0, 50, 120, 150, 200, 1000])
def test_정상_소득값은_통과한다(value: int) -> None:
    p = _decode({"core": {**CORE, "household_income_ratio_median": value}})
    assert p.core.household_income_ratio_median == value


def test_역질문_답변에도_같은_범위가_걸린다() -> None:
    """core 만 검사하면 온보딩은 막히고 역질문은 통과하는 구멍이 남는다."""
    with pytest.raises(msgspec.ValidationError):
        _decode({"core": CORE, "answers": {"household_income_ratio_median": 3_000_000}})
    p = _decode({"core": CORE, "answers": {"household_income_ratio_median": 120}})
    assert p.answers["household_income_ratio_median"] == 120


def test_가구원수는_1명부터() -> None:
    with pytest.raises(msgspec.ValidationError):
        _decode({"core": {**CORE, "household_size": 0}})
    assert _decode({"core": {**CORE, "household_size": 1}}).core.household_size == 1


def test_개월수_필드는_역질문으로만_들어온다() -> None:
    """core 에는 자리가 없고 answers 로만 들어오는 필드도 범위가 걸려야 한다."""
    with pytest.raises(msgspec.ValidationError):
        _decode({"core": CORE, "answers": {"employment_months": 2026}})
    assert _decode({"core": CORE, "answers": {"employment_months": 24}})


def test_bool_은_숫자로_통과하지_않는다() -> None:
    """파이썬에서 isinstance(True, int) 는 참이라 그냥 두면 True 가 1 로 샌다.

    household_size 의 하한이 1 이라 True 는 '가구원 1명'으로 통과해 버린다.
    숫자 필드에 bool 이 오면 FE 가 잘못 보낸 것이므로 통과시키면 안 된다.
    """
    with pytest.raises(msgspec.ValidationError):
        _decode({"core": CORE, "answers": {"household_size": True}})


def test_범위가_없는_필드는_건드리지_않는다() -> None:
    p = _decode({"core": CORE, "answers": {"similar_program_participation_2y": True}})
    assert p.answers["similar_program_participation_2y"] is True


# --- enum 필드: 사용자가 말한 그대로를 받으면 조용히 틀린다 -----------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("marital_status", "혼자 살아요"),  # 미혼이라고 답한 사람을 부적격으로 만든다
        ("marital_status", "미혼"),  # 한국어 표기도 코드값이 아니다
        ("employment_status", "취준생이에요"),
        ("education", "대학 졸업했어요"),
        ("marital_status", "SINGLE"),  # 대소문자도 다른 값이다
        ("marital_status", True),  # 문자열 필드에 bool
    ],
)
def test_코드값이_아닌_답변은_거부된다(field: str, value: object) -> None:
    """정규화를 건너뛴 답변이 통과하면 룰 비교가 불일치로 읽혀 부적격이 된다.

    데모 corpus 로 재현했을 때 `marital_status: "혼자 살아요"` 하나로 가평 월세가
    NEEDS_INFO → INELIGIBLE 로 뒤집혔다. 숫자 필드는 FIELD_BOUNDS 가 막고 있었는데
    enum 필드는 아무 문자열이나 들어왔다. 422 로 막으면 FE·MCP 가 정규화를 빼먹은
    걸 즉시 알게 된다 — 그럴듯한 부적격 목록보다 낫다.
    """
    with pytest.raises(msgspec.ValidationError) as e:
        _decode({"core": CORE, "answers": {field: value}})
    assert field in str(e.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("marital_status", "single"),
        ("employment_status", "job_seeking"),
        ("education", "university_graduated"),
    ],
)
def test_정규화된_코드값은_통과한다(field: str, value: str) -> None:
    assert _decode({"core": CORE, "answers": {field: value}}).answers[field] == value


def test_불리언_필드에_문자열이_오면_거부된다() -> None:
    """'없어요'를 그대로 보내면 bool 룰과 비교되어 항상 불일치가 된다."""
    with pytest.raises(msgspec.ValidationError):
        _decode({"core": CORE, "answers": {"similar_program_participation_2y": "없어요"}})
    with pytest.raises(msgspec.ValidationError):
        _decode({"core": CORE, "answers": {"similar_program_participation_2y": 1}})


def test_값_집합과_범위표는_겹치지_않는다() -> None:
    """한 필드가 두 가드에 동시에 걸리면 어느 메시지가 나갈지 읽는 사람이 모른다."""
    from app.schemas.user import BOOL_ANSWER_FIELDS, FIELD_CHOICES

    assert not (set(FIELD_CHOICES) & set(FIELD_BOUNDS))
    assert not (BOOL_ANSWER_FIELDS & set(FIELD_BOUNDS))
    assert not (BOOL_ANSWER_FIELDS & set(FIELD_CHOICES))


def test_값_집합도_룰이_아는_필드만_담는다() -> None:
    from app.schemas.enums import KNOWN_FIELDS
    from app.schemas.user import BOOL_ANSWER_FIELDS, FIELD_CHOICES

    assert set(FIELD_CHOICES) <= KNOWN_FIELDS
    assert BOOL_ANSWER_FIELDS <= KNOWN_FIELDS


def test_값_집합은_enum_정의에서_뽑아_온다() -> None:
    """손으로 적어 두면 enums.py 에 값을 추가할 때 한쪽만 바뀐다.

    그러면 멀쩡한 새 값이 422 로 막히고, 원인이 스키마가 아니라 이 표에 있어서
    찾는 데 오래 걸린다.
    """
    from typing import get_args

    from app.schemas.enums import Education, EmploymentStatus, MaritalStatus
    from app.schemas.user import FIELD_CHOICES

    assert FIELD_CHOICES["education"] == frozenset(get_args(Education))
    assert FIELD_CHOICES["employment_status"] == frozenset(get_args(EmploymentStatus))
    assert FIELD_CHOICES["marital_status"] == frozenset(get_args(MaritalStatus))


def test_범위표는_룰이_아는_필드만_담는다() -> None:
    """KNOWN_FIELDS 에 없는 이름에 범위를 걸어 두면 아무도 안 쓰는 죽은 규칙이 된다."""
    from app.schemas.enums import KNOWN_FIELDS

    assert set(FIELD_BOUNDS) <= KNOWN_FIELDS


def test_소득을_비율로_착각한_값은_거부된다() -> None:
    """1.5 는 0~1000 범위 안이라 범위 검사만으로는 못 잡는다.

    그냥 두면 `1.5 <= 150` 이 참이 되어 부적격이어야 할 사용자가 적격으로 나간다.
    범위를 벗어나 부적격이 되는 것(원 단위)보다 이쪽이 더 나쁘다.
    """
    with pytest.raises(msgspec.ValidationError):
        _decode({"core": CORE, "answers": {"household_income_ratio_median": 1.5}})


def test_범위가_있으면_단위_설명도_있다() -> None:
    """단위 설명 없이 경계만 계약에 나가면 FE 가 150 을 배수로 읽을 수 있다."""
    from app.schemas.user import FIELD_UNITS

    assert set(FIELD_BOUNDS) == set(FIELD_UNITS)
