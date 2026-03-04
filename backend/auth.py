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
    Extract and validate JWT token, return current user with profile data.
    
    Depends on: HTTPBearer security, database connection
    Returns: User dict with profile, branch, shift info
    """
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError):
        raise HTTPException(401, "Invalid token")

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
        "id": user["id"],
        "email": user["email"],
        "full_name": user["full_name"],
        "role": user["role"],
        "branch_id": user["branch_id"],
        "shift_start": user["shift_start"],
        "shift_end": user["shift_end"],
        "branch_name": user["branch_name"],
        "branch_city": user["branch_city"],
        "branch_lat": float(user["latitude"]) if user["latitude"] else None,
        "branch_lng": float(user["longitude"]) if user["longitude"] else None,
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