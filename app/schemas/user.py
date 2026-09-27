"""UserProfile — 판정 입력.

개인정보 원칙 (PRD §8.3): 이름·주민번호·연락처를 받지 않는다.
주소는 법정동 코드까지만, 상세주소는 스키마에 자리 자체가 없다.
저장 시 AES-256-GCM 으로 암호화되며 평문 컬럼은 존재하지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, get_args

import msgspec

from app.schemas.enums import (
    Education,
    EmploymentStatus,
    IncomeBasis,
    MaritalStatus,
)

# 숫자 입력의 허용 범위. 단위가 어긋난 값을 '계산 가능한 값'으로 받아주지 않는다.
#
# 왜 필요한가: 룰은 `household_income_ratio_median <= 150` 처럼 비교만 한다.
# FE 가 실수로 원 단위(3000000)를 보내면 비교는 정상적으로 수행되고, 소득 조건이
# 붙은 정책이 전부 '부적격'으로 나간다. 400 도 NEEDS_INFO 도 아니라 그럴듯한
# 부적격 목록이라 아무도 이상하다고 신고하지 않는다. 타입 검사로는 못 잡는다 —
# 3000000 도 int 이기 때문이다.
#
# 상한은 실제 공고문의 최댓값(중위소득 150~200%)보다 한참 위로 잡았다. 정상 입력을
# 막는 것보다 단위 실수를 놓치는 쪽이 훨씬 나쁘지만, 상한을 200 같이 빡빡하게 잡으면
# 언젠가 300% 짜리 공고가 나왔을 때 멀쩡한 사용자가 막힌다.
FIELD_BOUNDS: dict[str, tuple[int, int]] = {
    # 기준 중위소득 대비 퍼센트 정수. 150% → 150 (1.5 아님, 원 단위 아님)
    "household_income_ratio_median": (0, 1000),
    # 본인 포함 가구원 수. 0명인 가구는 없다.
    "household_size": (1, 20),
    # 개월 수. 100년을 넘기면 연/월 단위를 혼동한 것이다.
    "residence_months_continuous": (0, 1200),
    "employment_months": (0, 1200),
}

# 단위를 사람이 읽을 수 있는 말로. 계약(docs/contracts/rule_fields.json)으로 나가서
# FE 가 "정수 비율"을 퍼센트로 읽을지 배수로 읽을지 헷갈리지 않게 한다.
FIELD_UNITS: dict[str, str] = {
    "household_income_ratio_median": "기준 중위소득 대비 퍼센트 정수 (150% → 150)",
    "household_size": "명 (본인 포함)",
    "residence_months_continuous": "개월",
    "employment_months": "개월",
}


def check_bounds(field: str, value: object) -> None:
    """범위표에 있는 필드면 정수인지와 범위를 검사한다. 어기면 ValueError.

    msgspec 이 `__post_init__` 의 ValueError 를 ValidationError 로 바꾸면서 필드
    경로를 붙이고, API 는 그걸 이미 invalid-profile 로 잡는다.

    '모르는 형태는 건너뛴다'로 만들면 안 된다. 건너뛰기는 곧 통과이고, 여기서
    통과한 값은 룰 비교에 그대로 들어간다. 특히:

      - `bool`: 파이썬에서 `isinstance(True, int)` 가 참이라 True 가 1 로 샌다.
      - `float`: `answers` 는 float 을 허용하는데, 소득을 비율로 착각해 1.5 를
        보내면 0~1000 범위 안이라 통과하고 `1.5 <= 150` 이 참이 되어 **부적격이
        적격으로** 뒤집힌다. 범위 검사만으로는 절대 못 잡는 종류의 실수다.

    범위표에 있는 필드는 전부 정수(퍼센트·명·개월)라 정수만 받는다.
    """
    lo_hi = FIELD_BOUNDS.get(field)
    if lo_hi is None or value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} 는 정수여야 합니다 (받은 값: {value!r})")
    lo, hi = lo_hi
    if not lo <= value <= hi:
        raise ValueError(
            f"{field} 는 {lo}~{hi} 범위여야 합니다 (받은 값: {value}). "
            f"단위를 확인하세요"
        )


# 값 집합이 정해진 답변 필드. FIELD_BOUNDS 의 enum 짝이다.
#
# 왜 따로 필요한가: `Core` 의 같은 필드는 Literal 로 타입이 잡혀 있어 msgspec 이
# 디코드 단계에서 막는다. 그런데 `answers` 는 `dict[str, bool|int|float|str|None]`
# 이라 **아무 문자열이나 들어온다.** 온보딩으로 들어온 값은 막히고 역질문으로 들어온
# 같은 값은 통과하는 구멍이었다.
#
# 빠져 있으면 어떻게 조용히 틀리는가: 사용자가 "혼자 살아요"라고 답한 걸 그대로
# 보내면 룰은 `marital_status == "single"` 과 비교해 **불일치**로 읽는다. 미혼이라고
# 답한 사람이 미혼 조건에서 부적격이 되고, 422 도 NEEDS_INFO 도 아닌 그럴듯한
# 부적격이라 아무도 신고하지 않는다. 데모 corpus 로 재현했을 때 가평 월세가 실제로
# NEEDS_INFO → INELIGIBLE 로 뒤집혔다. 숫자 필드는 FIELD_BOUNDS 가 막고 있었고
# enum 필드만 구멍이었다.
FIELD_CHOICES: dict[str, frozenset[str]] = {
    "education": frozenset(get_args(Education)),
    "employment_status": frozenset(get_args(EmploymentStatus)),
    "marital_status": frozenset(get_args(MaritalStatus)),
}

# 불리언만 받는 답변 필드. `History` 쪽은 `bool | None` 으로 타입이 잡혀 있다.
BOOL_ANSWER_FIELDS: frozenset[str] = frozenset({"similar_program_participation_2y"})


def check_choices(field: str, value: object) -> None:
    """값 집합이 정해진 필드면 그 집합에 있는지 검사한다. 어기면 ValueError.

    자연어를 코드값으로 바꾸는 일은 `app.llm.answers.normalize_answer` 가 하고,
    못 바꾼 답은 아예 빼서 UNKNOWN 으로 남긴다. 판정 입력에 도달하기 전에 끝나야
    하는 일이다 — 여기서 받아주면 그 설계가 무의미해진다.
    """
    if value is None:
        return
    if field in BOOL_ANSWER_FIELDS:
        if not isinstance(value, bool):
            raise ValueError(f"{field} 는 true/false 여야 합니다 (받은 값: {value!r})")
        return
    allowed = FIELD_CHOICES.get(field)
    if allowed is None:
        return
    # bool 을 먼저 걸러낸다 — 문자열 필드에 True 가 오면 isinstance 만으로는 안 잡힌다
    if isinstance(value, bool) or not isinstance(value, str) or value not in allowed:
        raise ValueError(
            f"{field} 는 {', '.join(sorted(allowed))} 중 하나여야 합니다 "
            f"(받은 값: {value!r}). 사용자가 말한 그대로가 아니라 코드값으로 보내세요"
        )


def region_chain(region_code: str) -> list[str]:
    """법정동 코드를 접두 체인으로 확장한다.

    지역 조건은 '전국 / 시도 / 시군구'의 계층이라 접두 매칭이 필요한데,
    LIKE '41%' 는 인덱스를 타지 못한다. 체인 배열로 펼쳐 집합 교집합으로 바꾼다.

    >>> region_chain("41465")
    ['00', '41', '41465']
    >>> region_chain("41")
    ['00', '41']
    >>> region_chain("00")
    ['00']
    """
    # '00'(전국)이 입력으로 들어오면 시도 단계와 겹치므로 중복을 제거한다.
    # 중복 코드가 남으면 스냅샷 컴파일 시 비트를 두 번 세우게 된다.
    candidates = ["00", region_code[:2], region_code]
    chain: list[str] = []
    for c in candidates:
        if len(c) >= 2 and c not in chain:
            chain.append(c)
    return chain


class Core(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """온보딩에서 받는 기본조건 (S1, 8~10필드)."""

    birth_date: date
    region_code: str

    residence_start_date: date | None = None
    residence_continuous: bool = True
    education: Education | None = None
    employment_status: EmploymentStatus | None = None
    employment_start_date: date | None = None
    marital_status: MaritalStatus | None = None
    household_size: int | None = None
    income_basis: IncomeBasis | None = None
    household_income_ratio_median: int | None = None

    def __post_init__(self) -> None:
        # msgspec 은 디코드 직후 이걸 부르고, ValueError 를 ValidationError 로
        # 바꾸면서 필드 경로를 붙여 준다. API 는 그걸 이미 invalid-profile 로
        # 잡고 있어서 별도 핸들러가 필요 없다.
        check_bounds("household_size", self.household_size)
        check_bounds(
            "household_income_ratio_median", self.household_income_ratio_median
        )


class History(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    """기수혜 이력. None 은 '미확인'이며 역질문 대상이 된다 (False 와 구분)."""

    received_policy_ids: list[str] = msgspec.field(default_factory=list)
    similar_program_participation_2y: bool | None = None


class Consent(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    terms_version: str = "1.0"
    privacy_agreed_at: str | None = None
    retention_days: int = 90


class UserProfile(msgspec.Struct, kw_only=True, forbid_unknown_fields=True):
    core: Core
    history: History = msgspec.field(default_factory=History)
    # 역질문으로 채워지는 영역. 키는 룰의 field 명과 일치한다.
    answers: dict[str, bool | int | float | str | None] = msgspec.field(
        default_factory=dict
    )
    consent: Consent = msgspec.field(default_factory=Consent)

    def __post_init__(self) -> None:
        # 역질문 답변도 같은 값으로 룰에 들어간다. core 만 검사하면 온보딩으로
        # 들어온 값은 막히고 역질문으로 들어온 같은 값은 통과하는 구멍이 생긴다.
        for field, value in self.answers.items():
            check_bounds(field, value)
            check_choices(field, value)

    # ---- 파생값 (룰 엔진이 평가 직전에 계산) -------------------------------

    def age(self, on: date) -> int:
        """만 나이. 생일이 지나지 않았으면 1을 뺀다."""
        d = self.core.birth_date
        return on.year - d.year - ((on.month, on.day) < (d.month, d.day))

    def residence_months(self, on: date) -> int | None:
        """연속 거주 개월 수. 거주 시작일이 없거나 연속이 끊겼으면 None(미확인)."""
        start = self.core.residence_start_date
        if start is None or not self.core.residence_continuous:
            return None
        return _months_between(start, on)

    def employment_months(self, on: date) -> int | None:
        start = self.core.employment_start_date
        if start is None:
            return None
        return _months_between(start, on)

    def region_chain(self) -> list[str]:
        return region_chain(self.core.region_code)

    def _counted_or_answered(self, field: str, on: date) -> int | None:
        """날짜에서 센 값이 우선, 없으면 역질문 답변으로 메운다.

        이 폴백이 없으면 **물어본 답을 버린다.** `questions.py` 는 근속·거주
        개월수를 실제로 질문하는데(`_ANSWER_TYPES` 에 있다), 여기서 답변을 보지
        않으면 사용자가 답해도 값이 여전히 None 이라 그 정책은 계속 NEEDS_INFO 에
        남는다. 화면에서는 답을 입력했는데 아무 일도 일어나지 않는 것으로 보이고,
        틀린 판정이 아니라 판정이 아예 안 나오는 쪽이라 신고도 안 들어온다.

        센 값을 먼저 보는 이유는 나머지 필드와 같다 — 역질문 답변이 온보딩에서
        받은 값을 덮어쓰면 안 된다.

        `age` 에는 같은 폴백을 두지 않는다. `birth_date` 가 필수라 age 는 절대
        None 이 되지 않으므로 질문 자체가 나가지 않고, 답변으로 덮을 수 있게 하면
        생년월일과 나이가 어긋난 프로필이 만들어진다.
        """
        counted = (
            self.residence_months(on)
            if field == "residence_months_continuous"
            else self.employment_months(on)
        )
        if counted is not None:
            return counted
        answered = self.answers.get(field)
        # 범위 가드(check_bounds)가 이 두 필드를 정수로 강제하므로 여기서
        # 다시 형 변환하지 않는다. 통과하지 못한 값은 애초에 들어오지 못한다.
        return answered if isinstance(answered, int) else None

    def resolve(self, field: str, on: date) -> Any:
        """룰의 field 명으로 사용자 값을 꺼낸다. None 이면 '미확인' → NEEDS_INFO.

        반환형이 Any 인 이유: 필드마다 타입이 다르다 (나이 int, 지역 list[str],
        중복수혜 bool, 학력 str). 좁히려면 필드별 오버로드가 필요한데, 룰
        평가기(`evaluate_rule`)는 어차피 런타임에 타입을 보고 분기한다.
        """
        if field == "age":
            return self.age(on)
        if field == "residence_months_continuous":
            return self._counted_or_answered("residence_months_continuous", on)
        if field == "employment_months":
            return self._counted_or_answered("employment_months", on)
        if field == "region_code":
            # 원본 코드가 아니라 접두 체인을 돌려준다.
            # 지역 조건은 '전국 / 시도 / 시군구' 계층이라, 원본만 비교하면
            # 전국 대상 정책이 모든 사용자에게 부적격으로 나온다.
            # 집합끼리의 교집합으로 바뀌므로 룰 평가기가 그대로 처리한다.
            return self.region_chain()
        if field == "similar_program_participation_2y":
            v = self.history.similar_program_participation_2y
            return v if v is not None else self.answers.get(field)
        if field == "received_policy_ids":
            return self.history.received_policy_ids
        # 역질문 답변이 core 값을 덮어쓰지 않도록, core 를 먼저 본다
        v = getattr(self.core, field, None)
        return v if v is not None else self.answers.get(field)


def _months_between(start: date, on: date) -> int:
    """경과 개월 수. 일(day)이 아직 안 찼으면 한 달을 빼서 보수적으로 센다."""
    months = (on.year - start.year) * 12 + (on.month - start.month)
    if on.day < start.day:
        months -= 1
    return max(months, 0)
