"""Password hashing and random tokens (STACK.md S6)."""

import base64
import hashlib
import hmac
import os
import secrets

# scrypt cost: 2^14 rounds, 8 MB of memory per hash. Slow for an attacker, ~50 ms for us.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)
    return "$".join(
        [
            "scrypt",
            str(_N),
            str(_R),
            str(_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
    )
    # Constant-time comparison: the time taken says nothing about how close a guess was
    return hmac.compare_digest(digest, expected)


def new_token(prefix: str = "") -> str:
    """A random token with 256 bits of entropy, URL-safe."""
    return prefix + secrets.token_urlsafe(32)


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
