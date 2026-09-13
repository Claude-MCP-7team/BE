"""온통청년 OPEN API 수집기.

⚠️ 엔드포인트와 파라미터명은 **미확정**이다.
   공식 명세 페이지(youthcenter.go.kr, data.go.kr)가 현재 개발 환경의 네트워크
   정책에서 차단되어 원문 확인을 하지 못했다. 아래 기본값은 2차 자료에서 얻은
   것이므로 BE-M0-1 에서 실제 응답으로 검증한 뒤 확정한다.
   전부 환경변수로 덮어쓸 수 있게 해두었으므로, 값이 다르더라도 코드 수정 없이
   조사를 시작할 수 있다.

응답 원본은 항상 디스크에 남긴다. 같은 데이터를 다시 받지 않고 여러 번
분석할 수 있어야 조사 비용이 줄고, 조사 결과를 나중에 재현할 수 있다.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from batch.collect.parse import Record, parse_payload

DEFAULT_BASE_URL = "https://www.youthcenter.go.kr/opi/youthPlcyList.do"
DEFAULT_PAGE_SIZE = 100
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0


@dataclass
class CollectConfig:
    """수집 설정. 파라미터명이 확정되기 전까지 전부 바깥에서 바꿀 수 있어야 한다."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL

    # 요청 파라미터명 — 실제 응답으로 확인 후 확정
    key_param: str = "openApiVlak"
    page_param: str = "pageIndex"
    size_param: str = "display"
    region_param: str = "srchPolyBizSecd"

    page_size: int = DEFAULT_PAGE_SIZE
    max_pages: int = 50
    timeout_seconds: float = 20.0
    raw_dir: Path = Path("data/raw")

    @classmethod
    def from_env(cls) -> CollectConfig:
        api_key = os.environ.get("ONTONG_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "ONTONG_API_KEY 가 설정되지 않았습니다. "
                "온통청년 마이페이지 > OPEN API 에서 인증키를 발급받으세요."
            )
        return cls(
            api_key=api_key,
            base_url=os.environ.get("ONTONG_BASE_URL", DEFAULT_BASE_URL),
            key_param=os.environ.get("ONTONG_KEY_PARAM", "openApiVlak"),
            page_param=os.environ.get("ONTONG_PAGE_PARAM", "pageIndex"),
            size_param=os.environ.get("ONTONG_SIZE_PARAM", "display"),
            region_param=os.environ.get("ONTONG_REGION_PARAM", "srchPolyBizSecd"),
            page_size=int(os.environ.get("ONTONG_PAGE_SIZE", DEFAULT_PAGE_SIZE)),
            raw_dir=Path(os.environ.get("ONTONG_RAW_DIR", "data/raw")),
        )


class CollectError(RuntimeError):
    pass


class YouthCenterClient:
    """페이지네이션 + 재시도 + 원본 보존."""

    def __init__(self, config: CollectConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> YouthCenterClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout_seconds)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def fetch_page(self, page: int, region: str | None = None) -> bytes:
        """1페이지를 받는다. 네트워크 오류만 재시도하고 4xx 는 즉시 올린다."""
        cfg = self.config
        params: dict[str, str | int] = {
            cfg.key_param: cfg.api_key,
            cfg.page_param: page,
            cfg.size_param: cfg.page_size,
        }
        if region:
            params[cfg.region_param] = region

        assert self._client is not None, "async with 블록 안에서 사용하세요"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.get(cfg.base_url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_error = e
            else:
                if response.status_code < 400:
                    return response.content
                # 인증키 오류·잘못된 파라미터는 재시도해도 같은 답이 온다
                if response.status_code < 500:
                    raise CollectError(
                        f"{response.status_code} 응답 (page={page}). "
                        f"인증키와 파라미터명을 확인하세요: {response.text[:200]}"
                    )
                last_error = CollectError(f"{response.status_code} 서버 오류")

            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(BACKOFF_BASE_SECONDS * (2**attempt))

        raise CollectError(f"page={page} 수집 실패 ({MAX_RETRIES}회 시도)") from last_error

    async def collect(self, region: str | None = None) -> tuple[list[Record], Path]:
        """전 페이지를 수집하고 (레코드, 원본 저장 경로) 를 돌려준다."""
        cfg = self.config
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        out_dir = cfg.raw_dir / f"{stamp}{'-' + region if region else ''}"
        out_dir.mkdir(parents=True, exist_ok=True)

        records: list[Record] = []
        for page in range(1, cfg.max_pages + 1):
            raw = await self.fetch_page(page, region)
            page_records, fmt = parse_payload(raw)

            (out_dir / f"page-{page:03d}.{fmt}").write_bytes(raw)

            if not page_records:
                break
            records += page_records

            # 마지막 페이지는 요청한 개수보다 적게 온다
            if len(page_records) < cfg.page_size:
                break
        else:
            # max_pages 를 다 쓰고도 끝나지 않았다 = 데이터가 더 있다
            raise CollectError(
                f"max_pages({cfg.max_pages})에 도달했습니다. "
                "1차 범위가 예상보다 넓습니다 — ONTONG_MAX_PAGES 를 올리거나 범위를 좁히세요"
            )

        return records, out_dir


def load_raw(directory: Path) -> list[Record]:
    """이미 받아둔 원본에서 레코드를 복원한다 (재조사 시 네트워크 불필요)."""
    records: list[Record] = []
    for path in sorted(directory.glob("page-*")):
        page_records, _ = parse_payload(path.read_bytes())
        records += page_records
    return records
