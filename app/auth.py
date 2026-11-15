"""Application authentication based on bearer-key digests."""

from __future__ import annotations

import hashlib
import hmac

from .config import ApplicationConfig, GatewayConfig
from .errors import invalid_api_key


def digest_key(key: str) -> str:
    """Return the lowercase SHA-256 hex digest of an application key."""
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def authenticate(header: str | None, config: GatewayConfig) -> ApplicationConfig:
    """Resolve the application owning the presented bearer key.

    Every failure mode (missing header, wrong scheme, non-ASCII value, unknown
    key) returns the same public error so the gateway does not reveal which
    applications exist.
    """
    if header is None:
        raise invalid_api_key()

    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise invalid_api_key()

    key = parts[1]
    # Non-ASCII input cannot be a configured digest; reject it before hashing so
    # a malformed header never becomes an encoding error or a 500.
    if not key or not key.isascii():
        raise invalid_api_key()

    presented = digest_key(key)
    matched: ApplicationConfig | None = None
    for application in config.applications:
        # Compare against every configured digest to keep the work independent
        # of which application (if any) matches.
        if hmac.compare_digest(presented, application.key_sha256) and matched is None:
            matched = application
    if matched is None:
        raise invalid_api_key()
    return matched
