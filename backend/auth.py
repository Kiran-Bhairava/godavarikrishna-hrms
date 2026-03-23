"""Authentication and authorization utilities."""
import asyncpg
from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from db import get_db
from config import settings

security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: asyncpg.Connection = Depends(get_db),
) -> dict:
    """
    Validate JWT and return current user dict.

    Branch / shift data is embedded in the token at login time — no DB hit
    needed on every request. The DB is only queried if the token is missing
    branch data (e.g. old tokens issued before this change) so existing
    sessions keep working without forcing a re-login.

    is_active is NOT re-checked on every request. If HR deactivates a user,
    their token stays valid until expiry (max 8h). That's acceptable for an
    internal HRMS — the tradeoff eliminates a DB round-trip on every call.
    If you need instant deactivation, revert to the DB check here.
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError):
        raise HTTPException(401, "Invalid token")

    if payload.get("must_reset"):
        raise HTTPException(
            403,
            "Password reset required. Please change your password before continuing.",
        )

    # Fast path: all data embedded in token — zero DB queries
    if payload.get("branch_id") is not None or payload.get("role") == "admin":
        return {
            "id":           user_id,
            "email":        payload.get("email", ""),
            "full_name":    payload.get("full_name", ""),
            "role":         payload.get("role", "employee"),
            "branch_id":    payload.get("branch_id"),
            "shift_start":  payload.get("shift_start"),
            "shift_end":    payload.get("shift_end"),
            "branch_name":  payload.get("branch_name"),
            "branch_city":  payload.get("branch_city"),
            "branch_lat":   payload.get("branch_lat"),
            "branch_lng":   payload.get("branch_lng"),
            "radius_meters": payload.get("radius_meters"),
        }

    # Slow path: old token without branch data — fetch from DB once
    # This only runs for tokens issued before this deployment; disappears
    # after all users re-login (within access_token_expire_hours).
    user = await db.fetchrow(
        """
        SELECT u.id, u.email, u.full_name, u.role, u.is_active,
               e.branch_id,
               e.shift_start, e.shift_end,
               b.name AS branch_name,
               b.city AS branch_city,
               b.latitude, b.longitude, b.radius_meters
        FROM users u
        LEFT JOIN employees e ON e.user_id = u.id
        LEFT JOIN branches b ON b.id = e.branch_id
        WHERE u.id = $1
        """,
        user_id,
    )

    if not user or not user["is_active"]:
        raise HTTPException(401, "User not found or inactive")

    return {
        "id":           user["id"],
        "email":        user["email"],
        "full_name":    user["full_name"],
        "role":         user["role"],
        "branch_id":    user["branch_id"],
        "shift_start":  user["shift_start"],
        "shift_end":    user["shift_end"],
        "branch_name":  user["branch_name"],
        "branch_city":  user["branch_city"],
        "branch_lat":   float(user["latitude"]) if user["latitude"] else None,
        "branch_lng":   float(user["longitude"]) if user["longitude"] else None,
        "radius_meters": user["radius_meters"],
    }


async def require_hr(user: dict = Depends(get_current_user)) -> dict:
    """Dependency to enforce HR/admin-only access."""
    if user["role"] not in ("hr", "admin"):
        raise HTTPException(403, "HR access required")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Dependency to enforce admin-only access."""
    if user["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return user