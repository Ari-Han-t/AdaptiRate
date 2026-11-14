"""Dashboard sign-in. Access tokens are short-lived JWTs held in memory by the browser;
the refresh token lives in an HttpOnly, SameSite=Strict cookie scoped to this router."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings
from ..deps import EngineDep, ReadPrincipal, SessionDep, SettingsDep, client_ip
from ..limiter.engine import Algorithm, Check
from ..models import RefreshToken, User
from ..schemas import LoginRequest, TokenResponse, UserOut
from ..security import create_access_token, new_refresh_token, sha256_hex, verify_password
from ..services import audit

router = APIRouter(prefix="/auth", tags=["Auth"])

REFRESH_COOKIE = "ar_refresh"
COOKIE_PATH = "/api/v1/auth"
# Per account, and a looser cap per address so one office NAT is not locked out together.
LOGIN_ATTEMPTS_PER_ACCOUNT = 8
LOGIN_ATTEMPTS_PER_ADDRESS = 40
LOGIN_WINDOW_MS = 5 * 60 * 1000
REFRESH_REUSE_GRACE = timedelta(seconds=10)


def _set_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_ttl_seconds,
        path=COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


async def _issue(session: AsyncSession, user: User, response: Response, settings: Settings) -> TokenResponse:
    token, token_hash = new_refresh_token()
    session.add(
        RefreshToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    access, expires_in = create_access_token(user.id, user.role, user.email)
    _set_cookie(response, token, settings)
    return TokenResponse(access_token=access, expires_in=expires_in, user=UserOut.model_validate(user))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    settings: SettingsDep,
    engine: EngineDep,
) -> TokenResponse:
    ip = client_ip(request)
    email = body.email.lower()
    # Brute-force protection, by address and by account, using the limiter itself.
    for subject, attempts in (
        (f"ip:{ip}", LOGIN_ATTEMPTS_PER_ADDRESS),
        (f"email:{email}", LOGIN_ATTEMPTS_PER_ACCOUNT),
    ):
        decision = await engine.check(
            Check(
                policy_id="_login",
                policy_name="_login",
                algorithm=Algorithm.SLIDING_WINDOW,
                limit=attempts,
                window_ms=LOGIN_WINDOW_MS,
                burst=0,
                subject=subject,
                record=False,
            )
        )
        if not decision.allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "Too many sign-in attempts. Try again later.",
                headers={"Retry-After": str(max(1, decision.retry_after_ms // 1000))},
            )

    user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
    valid = verify_password(body.password, user.password_hash if user else None)
    if user is None or not valid or not user.is_active:
        audit.record(session, None, "auth.login_failed", resource_type="user", label=email, ip=ip)
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")

    user.last_login_at = datetime.now(UTC)
    tokens = await _issue(session, user, response, settings)
    audit.record(session, None, "auth.login", resource_type="user", resource_id=user.id, label=email, ip=ip)
    await session.commit()
    return tokens


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request, response: Response, session: SessionDep, settings: SettingsDep
) -> TokenResponse:
    token = request.cookies.get(REFRESH_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in")
    row = (
        await session.execute(
            select(RefreshToken).where(RefreshToken.token_hash == sha256_hex(token)).with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None or row.expires_at <= now:
        response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired")
    # `revoked_at` is set only by rotation (below). Logout and admin revocation delete the
    # row outright, so they can never fall into the grace window.
    if row.revoked_at is not None and now - row.revoked_at <= REFRESH_REUSE_GRACE:
        # The same cookie arriving twice within moments is two browser tabs (or a retry)
        # refreshing at once, not theft. Issue this caller a session too.
        user = await session.get(User, row.user_id)
        if user is None or not user.is_active:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
        tokens = await _issue(session, user, response, settings)
        await session.commit()
        return tokens
    if row.revoked_at is not None:
        # A rotated token was presented again later: assume it leaked and end every session.
        await session.execute(delete(RefreshToken).where(RefreshToken.user_id == row.user_id))
        audit.record(session, None, "auth.refresh_reuse", resource_type="user", resource_id=row.user_id)
        await session.commit()
        response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session revoked")

    user = await session.get(User, row.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
    row.revoked_at = now
    tokens = await _issue(session, user, response, settings)
    await session.commit()
    return tokens


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, session: SessionDep) -> Response:
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        await session.execute(delete(RefreshToken).where(RefreshToken.token_hash == sha256_hex(token)))
        await session.commit()
    response.delete_cookie(REFRESH_COOKIE, path=COOKIE_PATH)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.get("/me")
async def me(principal: ReadPrincipal) -> dict[str, object]:
    return {
        "kind": principal.kind,
        "id": principal.id,
        "label": principal.label,
        "role": principal.role,
        "scopes": sorted(principal.scopes),
    }
