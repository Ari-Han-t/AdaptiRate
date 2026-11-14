"""Password hashing, JWTs and API key material."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .config import get_settings

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "adaptirate"
API_KEY_PREFIX = "ar_"

_hasher = PasswordHasher()
# Verified against when the account does not exist, so response time does not reveal it.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        _hasher.verify(password_hash or _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return password_hash is not None


class TokenError(Exception):
    pass


def create_access_token(user_id: str, role: str, email: str) -> tuple[str, int]:
    settings = get_settings()
    now = int(time.time())
    expires = now + settings.access_token_ttl_seconds
    payload = {
        "iss": JWT_ISSUER,
        "sub": user_id,
        "role": role,
        "email": email,
        "typ": "access",
        "iat": now,
        "exp": expires,
    }
    token = jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)
    return token, settings.access_token_ttl_seconds


def decode_access_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],  # pinned: never trust the header's alg
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("typ") != "access":
        raise TokenError("wrong token type")
    return payload


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_refresh_token() -> tuple[str, str]:
    """Returns (token for the client, hash for the database)."""
    token = secrets.token_urlsafe(48)
    return token, sha256_hex(token)


@dataclass(frozen=True, slots=True)
class NewApiKey:
    secret: str
    prefix: str
    key_hash: str


def new_api_key() -> NewApiKey:
    # 256 bits of randomness, so a plain SHA-256 is sufficient: there is nothing to brute-force.
    secret = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return NewApiKey(secret=secret, prefix=secret[:11], key_hash=sha256_hex(secret))


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
