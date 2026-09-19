"""FastAPI 진입점.

스냅샷은 부팅 시 1회 적재한다 (ADR-001). 적재 이후 판정 경로는 DB 를 쓰지 않는다.

헬스체크를 둘로 나눈 이유:
  /healthz  프로세스가 살아 있는가            → 무료 호스팅의 keep-alive 핑 대상
  /readyz   요청을 처리할 수 있는가(스냅샷)   → 로드밸런서/배포 판정 대상
둘을 합치면, 스냅샷 적재에 실패한 인스턴스가 '살아있음'으로 보고되어
트래픽을 받고 전부 503 을 내는 상태가 된다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import judge as judge_api
from app.api.v1 import sessions as sessions_api
from app.core.config import settings
from app.db import pool as db_pool
from app.engine import snapshot as snapshot_store
from app.engine.snapshot import load_from_json

log = logging.getLogger("ypc")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if settings.snapshot_path and settings.snapshot_path.exists():
        try:
            snapshot = load_from_json(snapshot_store.holder, settings.snapshot_path.read_bytes())
            log.info("스냅샷 적재 완료: %s (%d건)", snapshot.version, snapshot.size)
        except Exception:
            # 여기서 죽이지 않는다. /readyz 가 미준비를 알리고, 운영자가 스냅샷을
            # 고쳐 다시 올리면 된다. 부팅 실패로 재시작 루프에 빠지는 편이 더 나쁘다.
            log.exception("스냅샷 적재 실패 — 미준비 상태로 기동합니다")
    else:
        log.warning("SNAPSHOT_PATH 가 없습니다 — 미준비 상태로 기동합니다")

    # DB 는 판정 경로에 없다 (ADR-001). 연결에 실패해도 기동을 막지 않는다 —
    # 스냅샷만 있으면 판정은 되고, 세션 저장만 503 이 된다.
    await db_pool.db.connect()

    yield

    await db_pool.db.close()


app = FastAPI(
    title="YPC Backend",
    description="청년정책 자격 판정 및 조합 최적화 API",
    version="0.1.0",
    lifespan=lifespan,
)
# 브라우저에서 부를 수 있게 하는 헤더들. 무엇을 여는지 한 곳에 모아둔다.
#
# **ETag 를 expose 하지 않으면 캐시 설계가 조용히 무력화된다.** 판정·목록 응답은
# ETag 와 If-None-Match 로 304 를 내도록 만들어져 있는데, 브라우저는 노출 목록에
# 없는 응답 헤더를 자바스크립트에 넘기지 않는다. FE 는 ETag 를 읽지 못하고, 서버는
# 매번 전체 응답을 다시 만든다 — 에러가 아니라서 아무도 눈치채지 못한다.
#
# 쿠키를 쓰지 않으므로 allow_credentials 는 False 다. 세션은 URL 의 UUID 와
# X-Session-Id 헤더로만 식별되며, 그래서 그 헤더가 허용 목록에 있어야 한다.
_CORS_EXPOSE = ["ETag", "X-Snapshot-Version"]
_CORS_ALLOW_HEADERS = ["Content-Type", "If-None-Match", "X-Session-Id"]

if settings.cors_origins:
    if "*" in settings.cors_origins:
        log.warning("CORS 가 모든 출처에 열려 있습니다 — 운영에서는 출처를 지정하세요")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=_CORS_ALLOW_HEADERS,
        expose_headers=_CORS_EXPOSE,
        max_age=600,
    )
else:
    log.warning("CORS_ORIGINS 가 없습니다 — 브라우저에서는 이 API 를 부를 수 없습니다")

app.include_router(judge_api.router)
app.include_router(sessions_api.router)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    """프로세스 생존 확인. 스냅샷 상태와 무관하게 200 을 준다."""
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
async def readyz(response: Response) -> dict[str, object]:
    """요청 처리 가능 여부.

    준비 판정의 기준은 **스냅샷뿐이다.** DB 가 죽었다고 503 을 내면 로드밸런서가
    인스턴스를 빼버리는데, 그 인스턴스는 판정을 멀쩡히 할 수 있다. 저장만 안 될
    뿐인 상태를 '서비스 불가'로 보고하면 장애가 아닌 것을 장애로 만든다.
    DB 상태는 진단용으로 함께 싣되 판정에는 넣지 않는다.
    """
    info = dict(snapshot_store.holder.info)
    info["database"] = db_pool.db.info
    if not info.get("ready"):
        response.status_code = 503
    return info
