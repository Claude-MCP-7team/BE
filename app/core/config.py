"""런타임 설정. 전부 환경변수로 주입되며 기본값은 로컬 개발 기준이다."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("ypc.config")


@dataclass(frozen=True, slots=True)
class Settings:
    # 부팅 시 적재할 스냅샷 파일 (PolicySchema 배열 JSON)
    snapshot_path: Path | None
    # 관리자 엔드포인트 토큰. 없으면 관리자 API 를 아예 열지 않는다.
    admin_token: str | None
    # 판정 기준일. 테스트에서 시간을 고정하기 위해서만 쓴다.
    fixed_today: str | None
    # 프로필 암호화 키 (`1:<base64>,2:<base64>`). 없으면 세션 저장을 하지 않는다.
    profile_enc_keys: str | None
    # 브라우저에서 이 API 를 부를 수 있는 출처. 기본값은 비어 있다 —
    # 설정하지 않은 배포가 조용히 전부 열린 상태가 되지 않도록.
    cors_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> Settings:
        raw = os.environ.get("SNAPSHOT_PATH")
        return cls(
            snapshot_path=Path(raw) if raw else None,
            admin_token=os.environ.get("ADMIN_TOKEN") or None,
            fixed_today=os.environ.get("YPC_FIXED_TODAY") or None,
            profile_enc_keys=os.environ.get("PROFILE_ENC_KEYS") or None,
            cors_origins=_origins(os.environ.get("CORS_ORIGINS")),
        )

    def profile_cipher(self):
        """프로필 암호기. 키가 없으면 None — 저장 기능만 꺼지고 판정은 돈다.

        키가 없는데 평문으로 저장하는 경로는 만들지 않는다. 그런 경로가 있으면
        설정 실수 한 번으로 개인정보가 평문으로 쌓이고, 아무도 눈치채지 못한다.
        """
        return _cipher_from(self.profile_enc_keys)


def _origins(raw: str | None) -> tuple[str, ...]:
    """`CORS_ORIGINS` 파싱. 쉼표로 구분한 출처 목록.

    기본값은 비어 있다 — 아무 출처도 열지 않는다. 브라우저에서 못 부르는 것은
    눈에 보이는 실패(콘솔의 CORS 에러)라 배포 때 바로 드러나지만, 전부 열어두는
    것은 아무 증상이 없어서 그대로 남는다.
    """
    return tuple(o.strip().rstrip("/") for o in (raw or "").split(",") if o.strip())


@lru_cache(maxsize=4)
def _cipher_from(raw: str | None):
    """키 문자열 → 암호기. 같은 키로 반복 호출해도 한 번만 만든다."""
    if not raw:
        return None
    from app.core.crypto import CryptoError, ProfileCipher, load_keys_from_env

    try:
        return ProfileCipher(load_keys_from_env(raw))
    except CryptoError:
        # 키가 잘못됐다고 부팅을 막지 않는다. 저장 엔드포인트가 503 을 내고
        # 판정은 계속된다. 다만 운영자가 알아야 하므로 크게 남긴다.
        log.exception("PROFILE_ENC_KEYS 를 읽지 못했습니다 — 세션 저장이 비활성화됩니다")
        return None


settings = Settings.from_env()
