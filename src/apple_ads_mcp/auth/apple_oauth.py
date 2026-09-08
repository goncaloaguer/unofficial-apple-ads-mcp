"""Apple Ads OAuth: ES256 client-secret JWT -> client_credentials access token.

Apple's flow (PLAN.md §3.2/§9.1): sign a short-lived JWT with the private key
whose public half was uploaded in Apple Ads → Account Settings → API, then
exchange it at appleid.apple.com for a one-hour bearer token with the single
scope ``searchadsorg``. Tokens and client secrets live only in memory.

Third-party libraries (pyjwt, httpx) are imported lazily so the claim/response
helpers stay stdlib-testable.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from apple_ads_mcp.config import REQUIRED_APPLE_SCOPE, Settings

AUDIENCE = "https://appleid.apple.com"
ALGORITHM = "ES256"
CLIENT_SECRET_TTL_SECONDS = 3600  # Apple allows up to 180 days; we sign on demand.
_EXPIRY_MARGIN_SECONDS = 120


class OAuthError(RuntimeError):
    pass


@dataclass
class _Token:
    value: str
    expires_at: float


def build_client_secret_claims(settings: Settings, now: int | None = None) -> tuple[dict, dict]:
    """Return (headers, payload) for the client-secret JWT exactly as Apple documents."""
    issued = int(time.time()) if now is None else now
    headers = {"alg": ALGORITHM, "kid": settings.apple_key_id}
    payload = {
        "iss": settings.apple_team_id,
        "sub": settings.apple_client_id,
        "aud": AUDIENCE,
        "iat": issued,
        "exp": issued + CLIENT_SECRET_TTL_SECONDS,
    }
    return headers, payload


def create_client_secret(settings: Settings, now: int | None = None) -> str:
    import jwt  # PyJWT with the `crypto` extra

    headers, payload = build_client_secret_claims(settings, now)
    try:
        return jwt.encode(payload, settings.apple_private_key_pem, algorithm=ALGORITHM, headers=headers)
    except Exception as exc:  # never echo key material
        raise OAuthError(f"could not sign client secret: {exc.__class__.__name__}") from exc


def parse_token_response(payload: dict) -> tuple[str, float]:
    """Validate a token response. Returns (token, ttl_seconds)."""
    token = payload.get("access_token")
    if not token or not isinstance(token, str):
        raise OAuthError("token response missing access_token")
    ttl = payload.get("expires_in")
    if not isinstance(ttl, (int, float)) or ttl <= 0:
        raise OAuthError("token response missing valid expires_in")
    if str(payload.get("token_type", "")).lower() != "bearer":
        raise OAuthError("token response token_type is not Bearer")
    scopes = set(str(payload.get("scope", "")).replace(",", " ").split())
    if scopes and scopes != {REQUIRED_APPLE_SCOPE}:
        raise OAuthError(
            f"granted scope {sorted(scopes)} is not exactly '{REQUIRED_APPLE_SCOPE}'"
        )
    return token, float(ttl)


class TokenManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._token: _Token | None = None
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        cached = self._token
        if cached and cached.expires_at - time.monotonic() > _EXPIRY_MARGIN_SECONDS:
            return cached.value
        async with self._lock:
            cached = self._token
            if cached and cached.expires_at - time.monotonic() > _EXPIRY_MARGIN_SECONDS:
                return cached.value
            payload = await self._fetch()
            token, ttl = parse_token_response(payload)
            self._token = _Token(value=token, expires_at=time.monotonic() + ttl)
            return token

    def invalidate(self) -> None:
        self._token = None

    async def _fetch(self) -> dict:
        import httpx  # deferred so policy modules stay dependency-free

        data = {
            "grant_type": "client_credentials",
            "client_id": self._settings.apple_client_id,
            "client_secret": create_client_secret(self._settings),
            "scope": REQUIRED_APPLE_SCOPE,
        }
        headers = {"User-Agent": self._settings.user_agent}
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            resp = await client.post(self._settings.token_url, data=data, headers=headers)
        if resp.status_code != 200:
            # Never echo the response body: it may contain sensitive detail.
            raise OAuthError(
                f"Apple token request failed with HTTP {resp.status_code}; check "
                "clientId/teamId/keyId, that the public key is uploaded, and that "
                "the API user is active"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise OAuthError("token endpoint returned non-JSON response") from exc
