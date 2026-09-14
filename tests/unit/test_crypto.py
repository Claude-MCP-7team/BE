"""프로필 암호화 (AES-256-GCM) — PRD §8.3.

여기서 지키는 것은 "암호화했다"가 아니라 **"평문이 나갈 경로가 없다"** 이다.
그래서 실패해야 하는 경우를 성공하는 경우보다 많이 시험한다.
"""

from __future__ import annotations

import base64
import os
from datetime import date

import msgspec
import pytest

from app.core.crypto import (
    KEY_BYTES,
    NONCE_BYTES,
    CryptoError,
    DecryptionFailed,
    ProfileCipher,
    generate_key,
    load_keys_from_env,
    session_aad,
)
from app.schemas.user import Core, UserProfile

SESSION_A = "11111111-1111-4111-8111-111111111111"
SESSION_B = "22222222-2222-4222-8222-222222222222"


def _key(seed: int = 1) -> bytes:
    return bytes([seed]) * KEY_BYTES


def _cipher(versions: dict[int, bytes] | None = None, current: int | None = None) -> ProfileCipher:
    return ProfileCipher(versions or {1: _key(1)}, current)


def _profile() -> UserProfile:
    return UserProfile(
        core=Core(
            birth_date=date(2001, 3, 14),
            region_code="41465",
            household_income_ratio_median=120,
            employment_status="job_seeking",
        )
    )


# --- 기본 왕복 -------------------------------------------------------------


def test_암호화한_것을_다시_읽을_수_있다() -> None:
    cipher = _cipher()
    aad = session_aad(SESSION_A, "profile")
    box = cipher.seal(b"hello", aad=aad)
    assert cipher.open(box, aad=aad) == b"hello"


def test_암호문에_평문이_남지_않는다() -> None:
    cipher = _cipher()
    raw = msgspec.json.encode(_profile())
    box = cipher.seal(raw, aad=session_aad(SESSION_A, "profile"))
    assert b"41465" not in box.ciphertext
    assert b"2001-03-14" not in box.ciphertext


def test_프로필_전체가_왕복한다() -> None:
    cipher = _cipher()
    aad = session_aad(SESSION_A, "profile")
    original = _profile()
    box = cipher.seal(msgspec.json.encode(original), aad=aad)
    back = msgspec.json.decode(cipher.open(box, aad=aad), type=UserProfile)
    assert back.core.birth_date == original.core.birth_date
    assert back.core.household_income_ratio_median == 120


# --- nonce ---------------------------------------------------------------


def test_같은_평문도_매번_다른_암호문이_된다() -> None:
    """nonce 를 재사용하면 GCM 인증이 무너진다 (두 암호문 XOR 로 평문 복원)."""
    cipher = _cipher()
    aad = session_aad(SESSION_A, "profile")
    boxes = [cipher.seal(b"same", aad=aad) for _ in range(50)]
    assert len({b.nonce for b in boxes}) == 50
    assert len({b.ciphertext for b in boxes}) == 50


def test_nonce_길이는_GCM_표준_12바이트() -> None:
    box = _cipher().seal(b"x", aad=session_aad(SESSION_A, "profile"))
    assert len(box.nonce) == NONCE_BYTES == 12


# --- 실패해야 하는 경우 ----------------------------------------------------


def test_변조된_암호문은_복호화에_실패한다() -> None:
    """CBC 라면 쓰레기를 성공적으로 내놓는다. GCM 은 예외를 던진다."""
    cipher = _cipher()
    aad = session_aad(SESSION_A, "profile")
    box = cipher.seal(b"hello world", aad=aad)
    tampered = type(box)(
        ciphertext=bytes([box.ciphertext[0] ^ 0xFF]) + box.ciphertext[1:],
        nonce=box.nonce,
        key_version=box.key_version,
    )
    with pytest.raises(DecryptionFailed):
        cipher.open(tampered, aad=aad)


def test_nonce_가_바뀌면_실패한다() -> None:
    cipher = _cipher()
    aad = session_aad(SESSION_A, "profile")
    box = cipher.seal(b"hello", aad=aad)
    swapped = type(box)(
        ciphertext=box.ciphertext, nonce=os.urandom(NONCE_BYTES), key_version=box.key_version
    )
    with pytest.raises(DecryptionFailed):
        cipher.open(swapped, aad=aad)


def test_다른_세션의_암호문을_붙여넣을_수_없다() -> None:
    """AAD 가 없으면 A 세션 프로필을 B 세션 행에 옮겨도 복호화가 성공한다."""
    cipher = _cipher()
    box = cipher.seal(b"secret", aad=session_aad(SESSION_A, "profile"))
    with pytest.raises(DecryptionFailed):
        cipher.open(box, aad=session_aad(SESSION_B, "profile"))


def test_같은_세션_안에서도_칸을_바꿔치기할_수_없다() -> None:
    """세션 ID 만 묶으면 profile 암호문을 answers 칸에 옮길 수 있다."""
    cipher = _cipher()
    box = cipher.seal(b"secret", aad=session_aad(SESSION_A, "profile"))
    with pytest.raises(DecryptionFailed):
        cipher.open(box, aad=session_aad(SESSION_A, "answers"))


def test_다른_키로는_읽을_수_없다() -> None:
    aad = session_aad(SESSION_A, "profile")
    box = _cipher({1: _key(1)}).seal(b"secret", aad=aad)
    other = ProfileCipher({1: _key(9)})
    with pytest.raises(DecryptionFailed):
        other.open(box, aad=aad)


def test_가지고_있지_않은_키_버전은_명확히_실패한다() -> None:
    cipher = _cipher({1: _key(1)})
    box = cipher.seal(b"x", aad=session_aad(SESSION_A, "profile"))
    stale = type(box)(ciphertext=box.ciphertext, nonce=box.nonce, key_version=7)
    with pytest.raises(DecryptionFailed, match="키 버전 7"):
        cipher.open(stale, aad=session_aad(SESSION_A, "profile"))


# --- 키 로테이션 ----------------------------------------------------------


def test_쓰기는_항상_최신_키로_한다() -> None:
    cipher = _cipher({1: _key(1), 2: _key(2)})
    assert cipher.current_version == 2
    assert cipher.seal(b"x", aad=session_aad(SESSION_A, "profile")).key_version == 2


def test_옛_키로_잠긴_것도_읽을_수_있다() -> None:
    """읽기까지 최신 키만 쓰면, 키 교체 때 전체 재암호화를 위해 서비스를 멈춰야 한다."""
    aad = session_aad(SESSION_A, "profile")
    old = ProfileCipher({1: _key(1)})
    box = old.seal(b"secret", aad=aad)

    rotated = ProfileCipher({1: _key(1), 2: _key(2)})
    assert rotated.open(box, aad=aad) == b"secret"
    assert rotated.needs_rotation(box)


def test_최신_키로_잠긴_것은_재암호화가_필요없다() -> None:
    cipher = _cipher({1: _key(1), 2: _key(2)})
    box = cipher.seal(b"x", aad=session_aad(SESSION_A, "profile"))
    assert not cipher.needs_rotation(box)


def test_현재_버전을_명시할_수_있다() -> None:
    cipher = _cipher({1: _key(1), 2: _key(2)}, current=1)
    assert cipher.seal(b"x", aad=session_aad(SESSION_A, "profile")).key_version == 1


# --- 설정 오류는 조용히 넘어가지 않는다 ------------------------------------


def test_키가_없으면_거부한다() -> None:
    with pytest.raises(CryptoError, match="하나도 없습니다"):
        ProfileCipher({})


def test_길이가_틀린_키는_거부한다() -> None:
    """AES-256 이 아닌 키를 조용히 받으면 암호 강도가 말없이 낮아진다."""
    with pytest.raises(CryptoError, match="32바이트"):
        ProfileCipher({1: b"too-short"})


def test_없는_버전을_현재로_지정하면_거부한다() -> None:
    with pytest.raises(CryptoError, match="키 목록에 없습니다"):
        ProfileCipher({1: _key(1)}, current_version=5)


# --- 환경변수 파싱 --------------------------------------------------------


def _env(*keys: bytes) -> str:
    return ",".join(
        f"{i}:{base64.b64encode(k).decode()}" for i, k in enumerate(keys, start=1)
    )


def test_환경변수에서_키를_읽는다() -> None:
    keys = load_keys_from_env(_env(_key(1), _key(2)))
    assert set(keys) == {1, 2}
    assert keys[1] == _key(1)


def test_빈_환경변수는_거부한다() -> None:
    for raw in (None, "", "   "):
        with pytest.raises(CryptoError, match="PROFILE_ENC_KEYS"):
            load_keys_from_env(raw)


def test_형식이_틀린_키는_거부한다() -> None:
    with pytest.raises(CryptoError, match="키 형식"):
        load_keys_from_env("nocolonhere")


def test_버전이_숫자가_아니면_거부한다() -> None:
    with pytest.raises(CryptoError, match="숫자가 아닙니다"):
        load_keys_from_env(f"v1:{base64.b64encode(_key(1)).decode()}")


def test_base64_가_아니면_거부한다() -> None:
    with pytest.raises(CryptoError, match="base64"):
        load_keys_from_env("1:!!!not-base64!!!")


def test_버전_중복은_거부한다() -> None:
    encoded = base64.b64encode(_key(1)).decode()
    with pytest.raises(CryptoError, match="중복"):
        load_keys_from_env(f"1:{encoded},1:{encoded}")


def test_생성한_키는_바로_쓸_수_있다() -> None:
    raw = f"1:{generate_key()}"
    cipher = ProfileCipher(load_keys_from_env(raw))
    aad = session_aad(SESSION_A, "profile")
    assert cipher.open(cipher.seal(b"ok", aad=aad), aad=aad) == b"ok"


def test_생성한_키는_매번_다르다() -> None:
    assert len({generate_key() for _ in range(20)}) == 20


# --- 설정 연동 ------------------------------------------------------------


def test_키가_없으면_암호기가_None_이다(monkeypatch) -> None:
    """키 없이 평문 저장으로 폴백하지 않는다 — 저장 기능만 꺼진다."""
    from app.core.config import Settings

    s = Settings(snapshot_path=None, admin_token=None, fixed_today=None, profile_enc_keys=None)
    assert s.profile_cipher() is None


def test_키가_잘못돼도_부팅을_막지_않는다() -> None:
    """설정 실수로 프로세스가 죽으면, 스냅샷만으로 가능한 판정까지 멈춘다."""
    from app.core.config import Settings

    s = Settings(
        snapshot_path=None, admin_token=None, fixed_today=None, profile_enc_keys="1:쓰레기"
    )
    assert s.profile_cipher() is None


def test_정상_키는_암호기를_만든다() -> None:
    from app.core.config import Settings

    s = Settings(
        snapshot_path=None,
        admin_token=None,
        fixed_today=None,
        profile_enc_keys=f"1:{generate_key()}",
    )
    cipher = s.profile_cipher()
    assert cipher is not None
    aad = session_aad(SESSION_A, "profile")
    assert cipher.open(cipher.seal(b"ok", aad=aad), aad=aad) == b"ok"
