"""온통청년 OPEN API 수집기.

엔드포인트·파라미터명은 2026-09-14 실제 응답으로 확정했다 (전체 2,774건 기준):

  GET https://www.youthcenter.go.kr/go/ythip/getPlcy
      apiKeyNm=<키>  rtnType=json  pageNum=1..  pageSize=<=1000
      zipCd=41000   ← 법정동 코드 5자리. 시도 전체는 <시도코드>000 이고,
                      전국 정책도 함께 나온다 (클라이언트 접두사 필터와 건수 일치)

  응답: {"resultCode":200, "result":{"pagging":{"totCount":N,...},
                                     "youthPolicyList":[{...60개 필드}]}}
  잘못된 키: HTTP 403 {"errorCode":"e001","errorMsg":"invalid api key."}

구 엔드포인트(/opi/youthPlcyList.do, openApiVlak)는 응답하지 않으며(타임아웃),
구 지역 파라미터(srchPolyBizSecd)는 신 API 가 무시하고 전체를 돌려준다 — 넘겨도
에러가 아니라 필터가 조용히 풀리므로 쓰지 말 것.
환경변수 덮어쓰기는 명세가 다시 바뀔 때를 위해 남겨둔다.

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

DEFAULT_BASE_URL = "https://www.youthcenter.go.kr/go/ythip/getPlcy"
DEFAULT_PAGE_SIZE = 1000  # 실측 상한. 전체가 3페이지로 끝난다
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0


@dataclass
class CollectConfig:
    """수집 설정. 파라미터명이 확정되기 전까지 전부 바깥에서 바꿀 수 있어야 한다."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL

    key_param: str = "apiKeyNm"
    page_param: str = "pageNum"
    size_param: str = "pageSize"
    region_param: str = "zipCd"

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
            key_param=os.environ.get("ONTONG_KEY_PARAM", "apiKeyNm"),
            page_param=os.environ.get("ONTONG_PAGE_PARAM", "pageNum"),
            size_param=os.environ.get("ONTONG_SIZE_PARAM", "pageSize"),
            region_param=os.environ.get("ONTONG_REGION_PARAM", "zipCd"),
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
        """1페이지를 받는다. 네트워크 오류만 재시도하고 4xx(잘못된 키 = 403) 는 즉시 올린다."""
        cfg = self.config
        params: dict[str, str | int] = {
            cfg.key_param: cfg.api_key,
            cfg.page_param: page,
            cfg.size_param: cfg.page_size,
            "rtnType": "json",
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
