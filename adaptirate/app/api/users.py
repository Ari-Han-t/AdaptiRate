"""User management. Admin only."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import AdminPrincipal, SessionDep, client_ip
from ..models import RefreshToken, User
from ..schemas import UserCreate, UserOut, UserUpdate
from ..security import hash_password
from ..services import audit

router = APIRouter(prefix="/users", tags=["Users"])


async def _other_active_admins(session: AsyncSession, user_id: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(User).where(User.role == "admin", User.is_active, User.id != user_id)
    )
    return int(result.scalar_one())


@router.get("", response_model=list[UserOut])
async def list_users(_: AdminPrincipal, session: SessionDep) -> list[User]:
    return list((await session.execute(select(User).order_by(User.created_at))).scalars().all())


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate, principal: AdminPrincipal, session: SessionDep, request: Request
) -> User:
    user = User(email=body.email.lower(), password_hash=hash_password(body.password), role=body.role)
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        raise HTTPException(status.HTTP_409_CONFLICT, "A user with this email already exists") from None
    audit.record(
        session,
        principal,
        "user.create",
        resource_type="user",
        resource_id=user.id,
        detail={"email": user.email, "role": user.role},
        ip=client_ip(request),
    )
    await session.commit()
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(
    user_id: str, body: UserUpdate, principal: AdminPrincipal, session: SessionDep, request: Request
) -> User:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    losing_admin = user.role == "admin" and (
        (body.role is not None and body.role != "admin") or body.is_active is False
    )
    if losing_admin and await _other_active_admins(session, user.id) == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "The last active admin cannot be demoted or disabled")

    changed: list[str] = []
    if body.role is not None and body.role != user.role:
        user.role = body.role
        changed.append("role")
    if body.is_active is not None and body.is_active != user.is_active:
        user.is_active = body.is_active
        changed.append("is_active")
    if body.password is not None:
        user.password_hash = hash_password(body.password)
        changed.append("password")
    if changed:
        # Any of these changes ends existing sessions; the access token lapses within minutes.
        await session.execute(delete(RefreshToken).where(RefreshToken.user_id == user.id))
        audit.record(
            session,
            principal,
            "user.update",
            resource_type="user",
            resource_id=user.id,
            detail={"changed": changed},
            ip=client_ip(request),
        )
    await session.commit()
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: str, principal: AdminPrincipal, session: SessionDep, request: Request) -> None:
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if principal.kind == "user" and principal.id == user.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "You cannot delete your own account")
    if user.role == "admin" and user.is_active and await _other_active_admins(session, user.id) == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, "The last active admin cannot be deleted")
    await session.delete(user)
    audit.record(
        session,
        principal,
        "user.delete",
        resource_type="user",
        resource_id=user_id,
        detail={"email": user.email},
        ip=client_ip(request),
    )
    await session.commit()
