import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient

from .config import Settings, get_settings

# Prepband's Supabase project signs Auth JWTs asymmetrically (ES256) and
# publishes the public key via JWKS - confirmed live against this project's
# own /auth/v1/.well-known/jwks.json, not assumed. PyJWKClient fetches and
# caches that key set and re-fetches on an unknown `kid` (e.g. after
# rotation), so no manual key management is needed here.
_jwks_clients: dict[str, PyJWKClient] = {}


def _get_jwks_client(jwks_url: str) -> PyJWKClient:
    client = _jwks_clients.get(jwks_url)
    if client is None:
        client = PyJWKClient(jwks_url)
        _jwks_clients[jwks_url] = client
    return client


def get_current_user_id(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    """Verifies a Supabase access token's signature, issuer, audience and
    expiry, and returns its subject (the caller's user id).

    This is the real verification FR-AUTH-07 requires: identity is always
    derived from a signature-checked token, never from a client-supplied
    user id or an unverified decode of the claims.
    """

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    token = authorization.removeprefix("Bearer ").strip()

    try:
        signing_key = _get_jwks_client(settings.supabase_jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["ES256"],
            audience=settings.supabase_jwt_audience,
            issuer=settings.supabase_jwt_issuer,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token.") from exc

    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Token has no subject.")
    return user_id
