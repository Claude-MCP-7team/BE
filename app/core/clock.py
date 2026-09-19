"""기준일 — 판정과 배치가 같은 '오늘'을 봐야 한다.

한 프로세스 안에서 두 군데가 각자 오늘을 계산하면, 자정 근처에서 판정은 어제
기준이고 빌더는 오늘 기준인 상태가 생긴다. 마감일이 걸린 정책 하나가 그 틈에서
'적격인데 스냅샷에 없음'이 된다. 정의는 한 곳에 둔다.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta


def today_kst() -> date:
    """오늘(KST). 연령과 마감일은 한국 시간으로 세야 한다.

    UTC 로 세면 매일 09시간 동안 날짜가 하루 어긋나, 생일 당일인 사용자가
    하루 늦게 자격을 얻거나 마감 당일 정책이 하루 일찍 사라진다.
    한국은 서머타임이 없어 고정 +9 로 충분하다.
    """
    from app.core.config import settings

    # 오버라이드는 호출 시점에 읽는다. import 시점에만 읽으면 테스트나 데모에서
    # 기준일을 바꿔도 이미 굳어진 설정이 이겨버린다.
    override = os.environ.get("YPC_FIXED_TODAY") or settings.fixed_today
    if override:
        return date.fromisoformat(override)
    return (datetime.now(UTC) + timedelta(hours=9)).date()
