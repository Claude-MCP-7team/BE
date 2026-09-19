"""UserProfile — 판정 입력.

개인정보 원칙 (PRD §8.3): 이름·주민번호·연락처를 받지 않는다.
주소는 법정동 코드까지만, 상세주소는 스키마에 자리 자체가 없다.
저장 시 AES-256-GCM 으로 암호화되며 평문 컬럼은 존재하지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import msgspec

from app.schemas.enums import (
    Education,
    EmploymentStatus,
    IncomeBasis,
    MaritalStatus,
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

    def resolve(self, field: str, on: date) -> Any:
        """룰의 field 명으로 사용자 값을 꺼낸다. None 이면 '미확인' → NEEDS_INFO.

        반환형이 Any 인 이유: 필드마다 타입이 다르다 (나이 int, 지역 list[str],
        중복수혜 bool, 학력 str). 좁히려면 필드별 오버로드가 필요한데, 룰
        평가기(`evaluate_rule`)는 어차피 런타임에 타입을 보고 분기한다.
        """
        if field == "age":
            return self.age(on)
        if field == "residence_months_continuous":
            return self.residence_months(on)
        if field == "employment_months":
            return self.employment_months(on)
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
