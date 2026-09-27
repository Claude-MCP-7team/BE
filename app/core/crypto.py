"""프로필 암호화 — AES-256-GCM (PRD §8.3).

`user_session` 에는 평문 프로필 컬럼이 존재하지 않는다. 생년월일·소득·거주지는
따로 보면 평범하지만 묶이면 개인을 특정한다. DB 덤프 하나가 유출됐을 때
"암호화했어야 했다"가 되지 않도록, 애플리케이션이 암호화한 뒤 넣는다.

**GCM 을 쓰는 이유는 무결성이다.**
  CBC 같은 순수 암호화 모드는 암호문이 바뀌어도 복호화가 '성공'하고 쓰레기를
  내놓는다. 그 쓰레기가 JSON 파싱을 통과할 확률은 낮지만 0이 아니고, 통과하면
  잘못된 프로필로 판정해 놓고 아무도 모른다. GCM 은 변조를 복호화 단계에서
  예외로 잡는다.

**nonce 는 절대 재사용하지 않는다.**
  같은 키로 같은 nonce 를 두 번 쓰면 GCM 의 인증이 무너진다 — 두 암호문을
  XOR 해서 평문을 복원할 수 있다. 그래서 nonce 는 매 암호화마다 새로 뽑고
  (os.urandom), 호출자가 넘길 수 없게 막는다. 12바이트는 GCM 표준 길이다.

**세션 ID 를 AAD 로 묶는다.**
  암호문만 옮기면 A 세션의 프로필을 B 세션 행에 붙여넣을 수 있다. 키가 같으니
  복호화는 성공한다. 세션 ID 를 추가 인증 데이터로 넣으면 그 순간 인증이
  실패해서, 행을 바꿔치기하는 공격이 막힌다.

**키 버전을 함께 저장한다.**
  키를 교체할 때 기존 행을 전부 재암호화하려면 서비스를 멈춰야 한다.
  행마다 어느 키로 잠겼는지 적어두면 읽을 때 그 키를 골라 쓰고, 쓸 때는 항상
  최신 키를 쓴다. 교체가 점진적으로 끝난다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# GCM 표준 nonce 길이. 96비트가 아닌 값을 쓰면 내부적으로 해싱이 한 번 더 들어가
# 성능과 보안 마진이 모두 나빠진다.
NONCE_BYTES = 12
KEY_BYTES = 32  # AES-256


class CryptoError(RuntimeError):
    """암호화 설정이 잘못됐거나 복호화에 실패했다."""


class DecryptionFailed(CryptoError):
    """암호문이 변조됐거나, 다른 키·다른 세션의 것이다.

    평문을 돌려주는 대신 반드시 실패해야 한다 — 잘못된 프로필로 판정하는 것이
    판정하지 못하는 것보다 나쁘다.
    """


@dataclass(frozen=True, slots=True)
class SealedBox:
    """암호문 한 덩어리. DB 의 (ct, nonce, key_version) 세 컬럼에 대응한다."""

    ciphertext: bytes
    nonce: bytes
    key_version: int


class ProfileCipher:
    """키 버전별 AES-256-GCM.

    쓰기는 항상 최신 버전으로, 읽기는 행에 적힌 버전으로 한다.
    """

    __slots__ = ("_current", "_keys")

    def __init__(self, keys: dict[int, bytes], current_version: int | None = None) -> None:
        if not keys:
            raise CryptoError("암호화 키가 하나도 없습니다")
        for version, key in keys.items():
            if len(key) != KEY_BYTES:
                raise CryptoError(
                    f"키 버전 {version} 의 길이가 {len(key)}바이트입니다 "
                    f"(AES-256 은 {KEY_BYTES}바이트)"
                )
        resolved = max(keys) if current_version is None else current_version
        if resolved not in keys:
            raise CryptoError(f"현재 키 버전 {resolved} 이 키 목록에 없습니다")
        self._keys = {v: AESGCM(k) for v, k in keys.items()}
        self._current = resolved

    @property
    def current_version(self) -> int:
        return self._current

    def seal(self, plaintext: bytes, *, aad: bytes) -> SealedBox:
        """암호화. nonce 는 매번 새로 뽑으며 호출자가 정할 수 없다."""
        nonce = os.urandom(NONCE_BYTES)
        cipher = self._keys[self._current]
        return SealedBox(
            ciphertext=cipher.encrypt(nonce, plaintext, aad),
            nonce=nonce,
            key_version=self._current,
        )

    def open(self, box: SealedBox, *, aad: bytes) -> bytes:
        """복호화. 변조·키 불일치·세션 불일치는 전부 예외다."""
        cipher = self._keys.get(box.key_version)
        if cipher is None:
            raise DecryptionFailed(
                f"키 버전 {box.key_version} 을 가지고 있지 않습니다 — "
                "키가 교체됐는데 옛 키를 내렸을 수 있습니다"
            )
        try:
            return cipher.decrypt(box.nonce, box.ciphertext, aad)
        except InvalidTag as e:
            raise DecryptionFailed(
                "복호화에 실패했습니다 (변조되었거나 다른 세션의 암호문입니다)"
            ) from e

    def needs_rotation(self, box: SealedBox) -> bool:
        """이 암호문이 옛 키로 잠겨 있는가. 읽을 때 다시 써 두면 점진 교체가 된다."""
        return box.key_version != self._current


def session_aad(session_id: str, field: str) -> bytes:
    """추가 인증 데이터 — 이 암호문이 '어느 세션의 어느 칸'인지 묶는다.

    필드 이름까지 넣는 이유: 세션 ID 만 묶으면 같은 세션 안에서 profile 암호문을
    answers 칸에 옮겨 붙이는 것이 여전히 가능하다. 실익이 큰 공격은 아니지만
    막는 비용이 문자열 하나라서 막는다.
    """
    return f"{session_id}:{field}".encode()


def load_keys_from_env(raw: str | None) -> dict[int, bytes]:
    """환경변수에서 키를 읽는다. 형식: `1:<base64>,2:<base64>`.

    키를 코드나 설정 파일이 아니라 환경변수로만 받는 이유는, 실수로 저장소에
    커밋되는 경로를 아예 없애기 위해서다.
    """
    import base64
    import binascii

    if not raw or not raw.strip():
        raise CryptoError(
            "PROFILE_ENC_KEYS 가 비어 있습니다 — 프로필 암호화 키 없이는 세션을 저장할 수 없습니다"
        )

    keys: dict[int, bytes] = {}
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if ":" not in item:
            raise CryptoError(f"키 형식이 잘못됐습니다 (버전:base64 여야 합니다): {item[:12]}…")
        version_text, encoded = item.split(":", 1)
        try:
            version = int(version_text)
        except ValueError as e:
            raise CryptoError(f"키 버전이 숫자가 아닙니다: {version_text!r}") from e
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as e:
            raise CryptoError(f"키 버전 {version} 의 base64 디코딩에 실패했습니다") from e
        if version in keys:
            raise CryptoError(f"키 버전 {version} 이 중복입니다")
        keys[version] = key

    if not keys:
        raise CryptoError("PROFILE_ENC_KEYS 에서 키를 하나도 읽지 못했습니다")
    return keys


def generate_key() -> str:
    """새 키를 만들어 base64 로 돌려준다 (운영자용 보조 도구)."""
    import base64

    return base64.b64encode(os.urandom(KEY_BYTES)).decode()
