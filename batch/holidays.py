"""공휴일 동기화 — 한국천문연구원(KASI) 특일 정보 API → `holiday` 테이블.

이 배치가 있어야 `app/planner/businessday.py` 의 내장 표(2025~2026)를 넘어서는
연도를 계획할 수 있다. 내장 표는 폴백이고, 여기서 받은 값이 권위다.

**받은 값을 통째로 믿지 않는다.**
  공휴일표가 잘못되면 "준비 시작일이 하루 이르거나 늦다"로만 나타나서 아무도
  눈치채지 못한다. API 가 빈 배열이나 반쪽 응답을 주는 날이 반드시 온다.
  그래서 쓰기 전에 두 가지를 본다:
    1. 연도당 최소 건수 — 한국 공휴일은 대체공휴일 없이도 15일 이상이다.
    2. 고정 공휴일 전수 — 신정·삼일절·어린이날·현충일·광복절·개천절·한글날·
       성탄절은 날짜가 법으로 고정되어 있다. 하나라도 빠졌으면 응답이 깨진 것이다.
  음력 명절(설·추석)과 부처님오신날, 대체공휴일, 임시공휴일은 검사하지 않는다 —
  날짜를 우리가 알 수 없으니 검사할 수도 없고, 그게 애초에 API 를 쓰는 이유다.

**검증에 실패하면 기존 표를 건드리지 않는다.**
  스냅샷 핫스왑과 같은 원칙이다(ADR-001). 깨진 데이터로 덮어쓰느니 어제 것으로
  계속 도는 편이 낫다.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import date

import httpx

# 특일 정보 서비스. 공휴일 여부(isHoliday=Y)만 쓰고 24절기·잡절은 받지 않는다.
DEFAULT_BASE_URL = (
    "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
)
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0

# 연도당 이만큼도 안 되면 응답이 깨진 것이다.
# 2026년 기준 21일(대체공휴일 포함), 대체가 하나도 없는 해도 15일은 넘는다.
MIN_HOLIDAYS_PER_YEAR = 15

# 날짜가 법으로 고정된 공휴일 (월, 일). 음력 명절과 선거일은 여기 없다.
FIXED_HOLIDAYS: tuple[tuple[int, int], ...] = (
    (1, 1),    # 신정
    (3, 1),    # 삼일절
    (5, 5),    # 어린이날
    (6, 6),    # 현충일
    (8, 15),   # 광복절
    (10, 3),   # 개천절
    (10, 9),   # 한글날
    (12, 25),  # 성탄절
)


class HolidaySyncError(RuntimeError):
    """받은 공휴일표를 신뢰할 수 없다. 기존 표를 유지해야 한다."""


@dataclass
class HolidayConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = 20.0
    # data.go.kr 은 키를 이미 인코딩해 주기도 하고 아니기도 한다.
    # httpx 가 한 번 더 인코딩하면 인증이 실패하므로 원문 그대로 붙인다.
    key_param: str = "serviceKey"

    @classmethod
    def from_env(cls) -> HolidayConfig:
        key = os.environ.get("KASI_API_KEY") or os.environ.get("DATA_GO_KR_KEY")
        if not key:
            raise HolidaySyncError(
                "KASI_API_KEY 가 없습니다 — 공휴일 동기화에는 공공데이터포털 인증키가 필요합니다"
            )
        return cls(
            api_key=key,
            base_url=os.environ.get("KASI_BASE_URL", DEFAULT_BASE_URL),
            timeout_seconds=float(os.environ.get("KASI_TIMEOUT", "20")),
        )


@dataclass
class SyncResult:
    year: int
    holidays: dict[date, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.holidays)


async def fetch_year(client: httpx.AsyncClient, config: HolidayConfig, year: int) -> SyncResult:
    """한 해치 공휴일을 받는다. 월별로 12번 부르는 대신 연 단위로 한 번 부른다."""
    params = {
        config.key_param: config.api_key,
        "solYear": str(year),
        "numOfRows": "100",
        "_type": "json",
    }
    payload = await _get_with_retry(client, config.base_url, params)
    holidays = _parse_payload(payload, year)
    result = SyncResult(year=year, holidays=holidays)
    result.warnings = _check(holidays, year)
    return result


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, params: dict[str, str]
) -> object:
    """일시적 실패는 물러섰다 다시 시도한다. 배치는 밤에 무인으로 돈다."""
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as e:
            last = e
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
    raise HolidaySyncError(f"공휴일 API 호출이 {MAX_RETRIES}회 모두 실패했습니다: {last}")


def _parse_payload(payload: object, year: int) -> dict[date, str]:
    """응답에서 (날짜 → 이름)을 뽑는다.

    공공데이터포털 응답은 item 이 1건일 때 배열이 아니라 객체로 온다.
    이 차이를 흡수하지 않으면 공휴일이 1건인 달에만 조용히 터진다.
    """
    body = _dig(payload, "response", "body")
    if body is None:
        raise HolidaySyncError("공휴일 응답에 body 가 없습니다 — 인증키나 엔드포인트를 확인하세요")

    items = _dig(body, "items", "item")
    if items is None:
        return {}
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        raise HolidaySyncError(f"공휴일 item 의 형태가 예상과 다릅니다: {type(items).__name__}")

    out: dict[date, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        if str(item.get("isHoliday", "Y")).strip().upper() != "Y":
            continue  # 24절기·잡절은 공휴일이 아니다
        raw = str(item.get("locdate", "")).strip()
        if len(raw) != 8 or not raw.isdigit():
            continue
        day = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        if day.year != year:
            continue
        out[day] = str(item.get("dateName", "공휴일")).strip() or "공휴일"
    return out


def _dig(obj: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _check(holidays: dict[date, str], year: int) -> list[str]:
    """받은 표가 쓸 만한지 본다. 문제가 있으면 사람이 읽을 문장으로 돌려준다."""
    problems: list[str] = []

    if len(holidays) < MIN_HOLIDAYS_PER_YEAR:
        problems.append(
            f"{year}년 공휴일이 {len(holidays)}일뿐입니다 "
            f"(최소 {MIN_HOLIDAYS_PER_YEAR}일 기대) — 응답이 잘렸을 수 있습니다"
        )

    missing = [
        f"{m}/{d}" for m, d in FIXED_HOLIDAYS if date(year, m, d) not in holidays
    ]
    if missing:
        problems.append(
            f"{year}년 고정 공휴일이 빠졌습니다: {', '.join(missing)} — 응답이 불완전합니다"
        )

    return problems


async def sync_years(years: list[int], config: HolidayConfig | None = None) -> list[SyncResult]:
    """여러 해를 받아 검증까지 마친 결과를 돌려준다.

    한 해라도 검증에 실패하면 전체를 거부한다. 부분 적용은 "2027년은 맞고
    2028년은 틀린" 표를 만드는데, 그 상태를 사람이 알아차릴 방법이 없다.
    """
    cfg = config or HolidayConfig.from_env()
    async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
        results = [await fetch_year(client, cfg, y) for y in years]

    failed = [r for r in results if r.warnings]
    if failed:
        lines = [f"  {y.year}: {w}" for y in failed for w in y.warnings]
        raise HolidaySyncError(
            "공휴일표를 신뢰할 수 없어 기존 표를 유지합니다:\n" + "\n".join(lines)
        )
    return results


def to_rows(results: list[SyncResult]) -> list[tuple[date, str]]:
    """`holiday` 테이블 INSERT 용 행. `calendar_from_rows()` 에 그대로 넣을 수 있다."""
    rows: list[tuple[date, str]] = []
    for result in results:
        rows.extend(sorted(result.holidays.items()))
    return rows


def upsert_sql() -> str:
    """이미 있는 날짜는 이름만 갱신한다 — 대체공휴일 지정이 나중에 바뀌기도 한다."""
    return (
        "INSERT INTO holiday (d, name, source) VALUES ($1, $2, 'kasi') "
        "ON CONFLICT (d) DO UPDATE SET name = EXCLUDED.name, synced_at = now()"
    )
