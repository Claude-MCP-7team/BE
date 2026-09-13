"""런타임 설정. 전부 환경변수로 주입되며 기본값은 로컬 개발 기준이다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    # 부팅 시 적재할 스냅샷 파일 (PolicySchema 배열 JSON)
    snapshot_path: Path | None
    # 관리자 엔드포인트 토큰. 없으면 관리자 API 를 아예 열지 않는다.
    admin_token: str | None
    # 판정 기준일. 테스트에서 시간을 고정하기 위해서만 쓴다.
    fixed_today: str | None

    @classmethod
    def from_env(cls) -> Settings:
        raw = os.environ.get("SNAPSHOT_PATH")
        return cls(
            snapshot_path=Path(raw) if raw else None,
            admin_token=os.environ.get("ADMIN_TOKEN") or None,
            fixed_today=os.environ.get("YPC_FIXED_TODAY") or None,
        )


settings = Settings.from_env()
