"""스냅샷 보관소 — API 프로세스가 RAM 에 들고 있는 정책 전체.

핵심 규칙 하나: **새 스냅샷 적재에 실패해도 서비스는 계속 돌아야 한다.**
야간 배치가 깨진 스냅샷을 내놓았을 때 API 가 같이 죽으면, 데이터 품질 문제가
서비스 장애로 번진다. 그래서 적재는 항상 '만들어 본 뒤 통째로 바꾸기'다.
검증에 실패하면 직전 스냅샷이 그대로 살아 있는다.

교체는 참조 한 줄을 바꾸는 것으로 끝난다. 파이썬에서 참조 대입은 원자적이라,
교체 중인 요청도 낡은 스냅샷을 일관되게 읽는다 (반쯤 바뀐 상태가 없다).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import UTC, datetime

import msgspec

from app.engine.compile import Snapshot, compile_snapshot
from app.schemas.policy import PolicySchema


class SnapshotNotReady(RuntimeError):
    """아직 한 번도 적재되지 않았다. 부팅 직후 요청이 올 때 발생한다."""


class SnapshotHolder:
    """현재 활성 스냅샷을 들고 있는 단일 지점."""

    def __init__(self) -> None:
        self._snapshot: Snapshot | None = None
        self._loaded_at: datetime | None = None
        self._lock = threading.Lock()  # 동시에 두 번 적재하지 않도록

    @property
    def ready(self) -> bool:
        return self._snapshot is not None

    def get(self) -> Snapshot:
        snapshot = self._snapshot
        if snapshot is None:
            raise SnapshotNotReady("스냅샷이 아직 적재되지 않았습니다")
        return snapshot

    @property
    def info(self) -> dict[str, object]:
        snapshot = self._snapshot
        if snapshot is None:
            return {"ready": False}
        return {
            "ready": True,
            "version": snapshot.version,
            "policy_count": snapshot.size,
            "rule_count": len(snapshot.rule_refs),
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
        }

    def load(self, build: Callable[[], Snapshot]) -> Snapshot:
        """새 스냅샷을 만들어 교체한다. 실패하면 예외를 올리고 기존 것을 유지한다."""
        with self._lock:
            candidate = build()  # 여기서 터지면 self._snapshot 은 손대지 않았다
            _verify(candidate)
            self._snapshot = candidate
            self._loaded_at = datetime.now(UTC)
            return candidate


def _verify(snapshot: Snapshot) -> None:
    """교체 전 최소 건전성 검사.

    빈 스냅샷으로 바꾸는 것이 가장 위험하다. 수집이 조용히 0건을 돌려주면
    모든 사용자가 '해당 정책 없음'을 보게 되는데, 이는 장애가 아니라
    정상 응답처럼 보여서 발견이 늦는다.
    """
    if snapshot.size == 0:
        raise ValueError("정책이 0건인 스냅샷으로는 교체할 수 없습니다")
    if len(snapshot.policy_ids) != len(set(snapshot.policy_ids)):
        raise ValueError("policy_id 가 중복된 스냅샷입니다")
    if not snapshot.rule_refs:
        raise ValueError("룰이 하나도 없는 스냅샷으로는 교체할 수 없습니다")


def load_from_json(holder: SnapshotHolder, raw: bytes, version: str | None = None) -> Snapshot:
    """PolicySchema 배열(JSON)에서 적재한다. 배치 산출물의 표준 형식."""
    policies = msgspec.json.decode(raw, type=list[PolicySchema])
    resolved = version or _version_from(raw, len(policies))
    return holder.load(lambda: compile_snapshot(policies, version=resolved))


def _version_from(raw: bytes, count: int) -> str:
    """내용 해시로 버전을 만든다 — 같은 데이터면 같은 버전이어야 ETag 가 의미를 가진다."""
    digest = hashlib.sha256(raw).hexdigest()[:12]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%MZ")
    return f"{stamp}-{count}p-{digest}"


# 프로세스 전역 보관소.
#
# ⚠️ 쓰는 쪽은 `from app.engine.snapshot import holder` 대신
#    `from app.engine import snapshot as snapshot_store` 후 `snapshot_store.holder` 로 쓴다.
#    전자는 모듈마다 별도의 이름 바인딩을 만들어서, 보관소를 통째로 교체하면
#    일부 모듈만 새 것을 보고 나머지는 옛 것을 계속 보는 상태가 된다.
#    (테스트에서 먼저 드러났지만, 운영에서 보관소를 교체할 때도 같은 문제가 난다.)
holder = SnapshotHolder()
