"""로캘이 달라도 CLI 가 죽지 않는지 검증한다.

이 테스트가 있는 이유:
  한국어 Windows 는 콘솔 코드페이지가 cp949, 영어 Windows 는 cp1252 다. 둘 다
  한글을 표현하지 못하거나 일부만 표현한다. ubuntu CI 러너는 UTF-8 이라
  "출력하다 죽는" 버그가 초록불 뒤에 숨는다. 실제로 팀원의 Windows 에서
  `pytest` 가 수집 단계부터 못 돌았고, 그 다음엔 `export_contract.py` 가
  계약 파일을 쓰다 죽었다.

  후자가 더 나쁘다. docs/contracts/*.json 은 FE 와 AI 역할이 검증에 쓰는
  공유 계약이라, 깨진 인코딩으로 기록되면 실패가 아니라 오염으로 끝난다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent

# 한글을 표현하지 못하는 대표적인 콘솔 인코딩들.
# cp949 는 한국어 Windows, cp1252 는 영어 Windows 기본값이다.
NON_UTF8_CONSOLES = ["cp949", "cp1252", "ascii"]


def _run(args: list[str], console_encoding: str) -> subprocess.CompletedProcess[bytes]:
    """UTF-8 모드를 끄고 콘솔 인코딩을 강제한 하위 프로세스로 실행한다."""
    import os

    env = dict(os.environ)
    env["PYTHONUTF8"] = "0"
    env["PYTHONIOENCODING"] = console_encoding
    return subprocess.run(
        [sys.executable, *args], cwd=REPO, env=env, capture_output=True, timeout=300
    )


@pytest.mark.parametrize("console", NON_UTF8_CONSOLES)
def test_계약_내보내기가_한글_못쓰는_콘솔에서도_돈다(console: str) -> None:
    """계약 스키마 생성은 FE·AI 와의 공유 파일을 쓴다. 로캘 때문에 깨지면 안 된다."""
    result = _run(["tools/export_contract.py"], console)
    assert result.returncode == 0, (
        f"{console} 콘솔에서 실패했습니다:\n{result.stderr.decode('utf-8', 'replace')}"
    )


@pytest.mark.parametrize("console", NON_UTF8_CONSOLES)
def test_벤치마크가_한글_못쓰는_콘솔에서도_돈다(console: str) -> None:
    """벤치마크는 CI 성능 게이트다. 출력 때문에 죽으면 성능 회귀와 구별되지 않는다."""
    result = _run(["bench/engine_bench.py"], console)
    assert result.returncode == 0, (
        f"{console} 콘솔에서 실패했습니다:\n{result.stderr.decode('utf-8', 'replace')}"
    )


def test_한글_파일을_읽을_때_인코딩을_지정한다() -> None:
    """마이그레이션 SQL 에는 한글 주석이 있다.

    `read_text()` 를 인코딩 없이 부르면 cp949 기계에서 UnicodeDecodeError 가 난다.
    실제로 그렇게 터졌던 경로라 명시적으로 지킨다.
    """
    migration = REPO / "db" / "migrations" / "0001_init.sql"
    text = migration.read_text(encoding="utf-8")
    assert "한글" in text or any("\uac00" <= ch <= "\ud7a3" for ch in text), (
        "이 테스트는 파일에 한글이 있다는 전제 위에 있습니다"
    )


def test_force_utf8_console이_스트림을_바꾼다() -> None:
    """보정 함수 자체가 동작하는지 — 재설정 불가 스트림에서도 죽지 않아야 한다."""
    code = (
        "import sys, io\n"
        "sys.stdout = io.StringIO()\n"  # reconfigure 가 없는 스트림
        "from app.core.console import force_utf8_console\n"
        "force_utf8_console()\n"  # 여기서 죽으면 안 된다
        "sys.stdout = sys.__stdout__\n"
        "force_utf8_console()\n"
        "print('\\uacc4\\uc57d')\n"  # 한글 출력이 성공해야 한다
    )
    result = _run(["-c", code], "cp1252")
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
