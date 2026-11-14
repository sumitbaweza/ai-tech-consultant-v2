"""Encryption for stored secrets: API keys and Google refresh tokens (Task 1.2, STACK.md S6).

AES-256-GCM: the key comes from the SECRET_KEY environment variable and never touches the
database. Each value gets a random 12-byte nonce. The business id is bound in as "associated
data", so a secret copied into another business's row fails to decrypt.
"""

import base64
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import get_settings

VERSION = b"\x01"  # first byte of every blob, so the format can change later


class SecretKeyError(RuntimeError):
    """SECRET_KEY is missing or isn't 32 bytes of base64."""


class DecryptError(RuntimeError):
    """The blob was changed, belongs to another business, or the key changed."""


def _aesgcm() -> AESGCM:
    raw = get_settings().secret_key.get_secret_value()
    try:
        key = base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise SecretKeyError("SECRET_KEY must be base64") from exc
    if len(key) != 32:
        raise SecretKeyError("SECRET_KEY must decode to 32 bytes (see .env.example)")
    return AESGCM(key)


def encrypt(plaintext: str, *, business_id: uuid.UUID) -> bytes:
    nonce = os.urandom(12)
    sealed = _aesgcm().encrypt(nonce, plaintext.encode("utf-8"), business_id.bytes)
    return VERSION + nonce + sealed


def decrypt(blob: bytes, *, business_id: uuid.UUID) -> str:
    if not blob or blob[:1] != VERSION:
        raise DecryptError("unknown secret format")
    nonce, sealed = blob[1:13], blob[13:]
    try:
        return _aesgcm().decrypt(nonce, sealed, business_id.bytes).decode("utf-8")
    except InvalidTag as exc:
        raise DecryptError("secret could not be decrypted for this business") from exc


def mask(secret: str) -> str:
    """What the screens and logs show instead of a key: the start and the last 4 characters."""
    secret = secret.strip()
    if len(secret) <= 12:
        return "****"
    return f"{secret[:6]}...{secret[-4:]}"
