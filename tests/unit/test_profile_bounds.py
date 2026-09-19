"""숫자 입력의 범위 가드.

이 테스트가 지키는 것: 단위가 어긋난 값이 '계산 가능한 값'으로 통과하지 않는 것.
타입 검사로는 못 잡는다 — 원 단위 3000000 도 int 라서 `<= 150` 비교가 정상 수행되고,
소득 조건이 붙은 정책이 전부 조용히 '부적격'으로 나간다.
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
