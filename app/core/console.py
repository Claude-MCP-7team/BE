"""콘솔 출력 인코딩 보정.

CLI 스크립트가 한글을 출력할 때, 파이썬은 stdout 인코딩을 **콘솔 코드페이지**에서
가져온다. 한국어 Windows 는 cp949, 영어 Windows 는 cp1252 이고 둘 다 한글의 일부
또는 전부를 표현하지 못해 `UnicodeEncodeError` 로 죽는다.

파일 입출력과 달리 이건 `encoding=` 인자를 줄 자리가 없다. 출력 스트림 자체를
바꿔야 한다.

`errors="replace"` 를 쓰는 이유: 로그 한 줄이 깨지는 것과 배치 작업 전체가 죽는 것은
전혀 다른 문제다. 스냅샷 빌드가 정책 600건을 다 처리해놓고 완료 메시지를 찍다가
죽으면, 실패 원인이 데이터에 있는 것처럼 보인다.
"""

from __future__ import annotations

import contextlib
import sys


def force_utf8_console() -> None:
    """stdout/stderr 를 UTF-8 로 강제한다. CLI 진입점에서 가장 먼저 호출할 것."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            # 파이프로 리다이렉트된 경우 등 재설정이 불가능한 스트림도 있다.
            # 출력 인코딩 때문에 프로그램이 죽는 걸 막는 게 목적이므로 조용히 넘어간다.
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
