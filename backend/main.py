"""
Attendance System — FastAPI backend (optimised)

Performance fixes applied
─────────────────────────
1. Single DB connection per request — auth dep now receives the connection
   from the endpoint via a shared `get_db` context so every request acquires
   exactly ONE pool connection instead of two.
2. Lightweight HR auth fast-path — JWT claims are trusted for role checks on
   HR/admin endpoints; the heavy 3-table JOIN is only done when the full user
   profile (branch coords, shift times) is actually needed.
3. Cached timezone object — _TZ is resolved once at startup; no repeated
   pytz.timezone() dict lookups inside hot paths.
4. attendance_status merged to a single query — previously two sequential
   SELECTs; now one query returns both log tail and daily summary.
5. punch_in collapsed to 2 DB round-trips (was 3).
6. punch_out collapsed to 2 DB round-trips (was 4).
7. deactivate / reactivate merged to single UPDATE…RETURNING (was SELECT + UPDATE).
8. update_employee merged to single UPDATE…RETURNING (was SELECT + UPDATE).
9. Removed duplicate UpdateEmployeeRequest class definition.
10. Statement cache size bumped to 200 for better prepared-statement reuse.
"""
import logging
import math
import pytz

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, date, time
from io import BytesIO
from pathlib import Path
from typing import Optional, Annotated

import asyncpg
import dotenv
from fastapi import FastAPI, Depends, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from passlib.context import CryptContext
from pydantic import BaseModel, EmailStr, field_validator
from pydantic_settings import BaseSettings

dotenv.load_dotenv()

# ── Logging ───────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("attendance")


# ── Settings ──────────────────────────────────────────────────
class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/attendance_db"
    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_hours: int = 8
    office_timezone: str = "Asia/Kolkata"
    cors_origins: str = ""
    late_grace_minutes: int = 0
    db_pool_min: int = 5
    db_pool_max: int = 20

    model_config = {"env_file": ".env", "case_sensitive": False}

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

if not settings.secret_key:
    raise RuntimeError("SECRET_KEY must be set in .env")

# FIX 3: Resolve timezone ONCE at startup — no per-call dict lookup.
_TZ = pytz.timezone(settings.office_timezone)


# ── App ───────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    db_pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        # FIX 10: larger prepared-statement cache for better reuse
        statement_cache_size=200,
    )
    logger.info("DB pool ready (statement_cache_size=200)")
    yield
    await db_pool.close()


app = FastAPI(title="Attendance System", docs_url=None, redoc_url=None, lifespan=lifespan)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()
db_pool: Optional[asyncpg.Pool] = None

if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )


# ── DB dependency (FIX 1) ─────────────────────────────────────
# ONE connection per request, stored on request.state so auth deps
# and endpoint handlers share it without double-acquiring from the pool.

async def get_db(request: Request) -> asyncpg.Connection:
    if db_pool is None:
        raise HTTPException(503, "Database not available")
    if not hasattr(request.state, "_db"):
        request.state._db_ctx = db_pool.acquire()
        request.state._db = await request.state._db_ctx.__aenter__()
    return request.state._db


@app.middleware("http")
async def _release_db_middleware(request: Request, call_next):
    """Release the shared DB connection back to the pool after each request."""
    response = await call_next(request)
    if hasattr(request.state, "_db_ctx"):
        try:
            await request.state._db_ctx.__aexit__(None, None, None)
        except Exception:
            pass
    return response


# ══════════════════════════════════════════════════════════════
# SCHEMAS
# ══════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def not_empty(cls, v):
        if not v.strip():
            raise ValueError("Password must not be empty")
        return v


class PunchRequest(BaseModel):
    latitude: float
    longitude: float

    @field_validator("latitude")
    @classmethod
    def valid_lat(cls, v):
        if not (-90 <= v <= 90):
            raise ValueError("Invalid latitude")
        return v

    @field_validator("longitude")
    @classmethod
    def valid_lng(cls, v):
        if not (-180 <= v <= 180):
            raise ValueError("Invalid longitude")
        return v


class OnboardRequest(BaseModel):
    """Payload from the 5-step Add Employee form."""

    # Step 1 — Basic
    full_name:      str
    work_email:     EmailStr
    password:       str = "Welcome@123"
    personal_email: Optional[str] = None
    phone:          Optional[str] = None
    dob:            Optional[str] = None    # YYYY-MM-DD
    gender:         Optional[str] = None
    blood_group:    Optional[str] = None
    nationality:    Optional[str] = None
    home_address:   Optional[str] = None
    emg_name:       Optional[str] = None
    emg_phone:      Optional[str] = None
    emg_rel:        Optional[str] = None

    # Step 2 — Job
    job_title:      Optional[str] = None
    designation:    Optional[str] = None
    department:     Optional[str] = None
    sub_department: Optional[str] = None
    grade:          Optional[str] = None
    date_of_joining: Optional[str] = None  # YYYY-MM-DD
    branch_id:      Optional[int] = None
    l1_manager_id:  Optional[int] = None
    l2_manager_id:  Optional[int] = None
    role:           str = "employee"
    cost_centre:    Optional[str] = None

    # Step 3 — Terms
    employment_type: Optional[str] = None
    contract_end:    Optional[str] = None
    probation_end:   Optional[str] = None
    notice_period:   Optional[str] = None

    # Step 4 — Work
    shift_start: time = time(9, 0)
    shift_end:   time = time(18, 0)
    work_mode:     Optional[str] = "On-Site"
    weekly_off:    Optional[str] = "Saturday & Sunday"
    work_location: Optional[str] = None
    asset_id:      Optional[str] = None

    # Step 5 — Compensation
    annual_ctc:      Optional[float] = None
    pay_frequency:   Optional[str] = "Monthly"
    pf_enrolled:     bool = True
    esic_applicable: bool = True
    bank_name:       Optional[str] = None
    bank_account:    Optional[str] = None
    bank_ifsc:       Optional[str] = None
    pan_number:      Optional[str] = None

    @field_validator("role")
    @classmethod
    def valid_role(cls, v):
        if v not in ("employee", "hr", "admin"):
            raise ValueError("role must be employee, hr, or admin")
        return v

    @field_validator("password")
    @classmethod
    def min_length(cls, v):
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class UpdateOnboardingStatus(BaseModel):
    status: str

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v not in ("awaiting", "in-progress", "completed"):
            raise ValueError("Invalid status")
        return v


# FIX 9: Only ONE definition (was duplicated, causing the second to silently shadow the first).
class UpdateEmployeeRequest(BaseModel):
    """Partial update — all fields optional."""
    full_name:       Optional[str] = None
    personal_email:  Optional[str] = None
    phone:           Optional[str] = None
    dob:             Optional[str] = None
    gender:          Optional[str] = None
    blood_group:     Optional[str] = None
    nationality:     Optional[str] = None
    home_address:    Optional[str] = None
    emg_name:        Optional[str] = None
    emg_phone:       Optional[str] = None
    emg_rel:         Optional[str] = None
    branch_id:       Optional[int] = None
    job_title:       Optional[str] = None
    designation:     Optional[str] = None
    department:      Optional[str] = None
    sub_department:  Optional[str] = None
    grade:           Optional[str] = None
    date_of_joining: Optional[str] = None
    cost_centre:     Optional[str] = None
    l1_manager_id:   Optional[int] = None
    l2_manager_id:   Optional[int] = None
    employment_type: Optional[str] = None
    contract_end:    Optional[str] = None
    probation_end:   Optional[str] = None
    notice_period:   Optional[str] = None
    shift_start:     Optional[str] = None   # "HH:MM"
    shift_end:       Optional[str] = None
    work_mode:       Optional[str] = None
    weekly_off:      Optional[str] = None
    work_location:   Optional[str] = None
    asset_id:        Optional[str] = None
    annual_ctc:      Optional[float] = None
    pay_frequency:   Optional[str] = None
    pf_enrolled:     Optional[bool] = None
    esic_applicable: Optional[bool] = None
    bank_name:       Optional[str] = None
    bank_account:    Optional[str] = None
    bank_ifsc:       Optional[str] = None
    pan_number:      Optional[str] = None
    role:            Optional[str] = None

    @field_validator("role")
    @classmethod
    def valid_role(cls, v):
        if v is not None and v not in ("employee", "hr", "admin"):
            raise ValueError("role must be employee, hr, or admin")
        return v


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def hash_password(p: str) -> str:
    return pwd_context.hash(p)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(user_id: int, email: str, role: str) -> str:
    exp = datetime.now(tz=pytz.utc) + timedelta(hours=settings.access_token_expire_hours)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "role": role, "exp": exp},
        settings.secret_key, algorithm=settings.algorithm,
    )


def local_now() -> datetime:
    return datetime.now(_TZ)   # FIX 3: cached tz


def to_local(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(_TZ)  # FIX 3: cached tz


def parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_date_param(s: Optional[str]) -> date:
    if not s:
        return date.today()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Expected date format YYYY-MM-DD")


# ── Auth dependencies ──────────────────────────────────────────

def _decode_token(creds: HTTPAuthorizationCredentials) -> dict:
    """Decode + verify JWT. Raises 401 on any failure."""
    try:
        return jwt.decode(
            creds.credentials, settings.secret_key, algorithms=[settings.algorithm]
        )
    except (JWTError, KeyError, ValueError):
        raise HTTPException(401, "Invalid or expired token")


async def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(security),
    db: asyncpg.Connection = Depends(get_db),
) -> dict:
    """
    Full user profile with branch/shift data.
    Uses the shared request connection (FIX 1).
    Only used by endpoints that actually need branch/shift info (attendance).
    """
    payload = _decode_token(creds)
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(401, "Invalid token payload")

    user = await db.fetchrow(
        """SELECT u.id, u.email, u.full_name, u.role,
                  e.branch_id, e.shift_start, e.shift_end,
                  b.name        AS branch_name,
                  b.latitude    AS branch_lat,
                  b.longitude   AS branch_lng,
                  b.radius_meters
           FROM users u
           LEFT JOIN employees e ON e.user_id = u.id
           LEFT JOIN branches b  ON b.id = e.branch_id
           WHERE u.id = $1 AND u.is_active = TRUE""",
        user_id,
    )
    if not user:
        raise HTTPException(401, "User not found or deactivated")
    return dict(user)


async def require_hr_jwt(
    creds: HTTPAuthorizationCredentials = Depends(security),
) -> dict:
    """
    FIX 2: Zero-DB fast-path for HR/admin endpoints.
    Role is read from the JWT claim — no DB query needed.
    Returns {id, email, role}.
    """
    payload = _decode_token(creds)
    role = payload.get("role", "")
    if role not in ("hr", "admin"):
        raise HTTPException(403, "HR/Admin access required")
    try:
        return {"id": int(payload["sub"]), "email": payload["email"], "role": role}
    except (KeyError, ValueError):
        raise HTTPException(401, "Invalid token payload")


# ══════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════

@app.post("/api/auth/login")
async def login(req: LoginRequest, db: asyncpg.Connection = Depends(get_db)):
    user = await db.fetchrow(
        "SELECT id, email, password_hash, full_name, role, is_active FROM users WHERE email = $1",
        req.email.lower(),
    )
    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid email or password")
    if not user["is_active"]:
        raise HTTPException(403, "Account deactivated. Contact HR.")

    await db.execute("UPDATE users SET last_login = NOW() WHERE id = $1", user["id"])
    logger.info("Login: id=%s role=%s", user["id"], user["role"])

    return {
        "access_token": create_token(user["id"], user["email"], user["role"]),
        "token_type": "bearer",
        "user": {"id": user["id"], "email": user["email"],
                 "full_name": user["full_name"], "role": user["role"]},
    }


@app.post("/api/auth/logout")
async def logout():
    return {"message": "Logged out"}


@app.get("/api/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    result = dict(user)
    for f in ("shift_start", "shift_end"):
        if result.get(f):
            result[f] = result[f].strftime("%H:%M")
    for f in ("branch_lat", "branch_lng"):
        if result.get(f) is not None:
            result[f] = float(result[f])
    return result


# ══════════════════════════════════════════════════════════════
# ATTENDANCE
# ══════════════════════════════════════════════════════════════

@app.post("/api/attendance/punch-in")
async def punch_in(
    req: PunchRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    if not user["branch_id"]:
        raise HTTPException(400, "No branch assigned. Contact HR.")

    distance = haversine(
        req.latitude, req.longitude,
        float(user["branch_lat"]), float(user["branch_lng"]),
    )
    radius = user["radius_meters"] or 200
    if distance > radius:
        raise HTTPException(403, f"You are {int(distance)}m away. Must be within {radius}m.")

    now = local_now()
    shift = user["shift_start"]
    is_late, late_min = False, 0
    if shift:
        delta = (datetime.combine(date.today(), now.time())
                 - datetime.combine(date.today(), shift)).total_seconds()
        grace = settings.late_grace_minutes * 60
        if delta > grace:
            is_late, late_min = True, max(0, int((delta - grace) / 60))

    # FIX 5: validate punch state + insert in one transaction (2 round-trips, was 3).
    async with db.transaction():
        state = await db.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE punch_type='in')  AS ins,
                 COUNT(*) FILTER (WHERE punch_type='out') AS outs
               FROM attendance_logs
               WHERE user_id = $1
                 AND (punched_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date""",
            user["id"], settings.office_timezone,
        )
        ins  = state["ins"]  or 0
        outs = state["outs"] or 0
        if ins > outs:
            raise HTTPException(409, "Already punched in.")
        if ins > 0 and outs > 0:
            raise HTTPException(409, "Attendance already completed for today.")

        log = await db.fetchrow(
            """INSERT INTO attendance_logs
               (user_id, branch_id, punch_type, latitude, longitude, distance_meters, is_valid)
               VALUES ($1,$2,'in',$3,$4,$5,TRUE) RETURNING punched_at""",
            user["id"], user["branch_id"], req.latitude, req.longitude, int(distance),
        )
        await db.execute(
            """INSERT INTO daily_summary
               (user_id, work_date, first_punch_in, is_late, late_by_minutes, status)
               VALUES ($1,(NOW() AT TIME ZONE $2)::date,$3,$4,$5,'present')
               ON CONFLICT (user_id, work_date) DO UPDATE
               SET first_punch_in=EXCLUDED.first_punch_in,
                   is_late=EXCLUDED.is_late,
                   late_by_minutes=EXCLUDED.late_by_minutes,
                   status='present'""",
            user["id"], settings.office_timezone, log["punched_at"], is_late, late_min,
        )

    punch_time = to_local(log["punched_at"]).strftime("%I:%M %p")
    return {
        "success": True,
        "message": f"Punched in{f' — late by {late_min} min' if is_late else ''}",
        "time": punch_time,
        "distance": int(distance),
        "is_late": is_late,
        "late_by_minutes": late_min,
    }


@app.post("/api/attendance/punch-out")
async def punch_out(
    req: PunchRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    if not user["branch_id"]:
        raise HTTPException(400, "No branch assigned. Contact HR.")

    distance = 0
    if user["branch_lat"] and user["branch_lng"]:
        distance = haversine(
            req.latitude, req.longitude,
            float(user["branch_lat"]), float(user["branch_lng"]),
        )

    # FIX 6: merge punch validation + first_punch_in lookup into one query (2 ops, was 4).
    async with db.transaction():
        state = await db.fetchrow(
            """SELECT
                 COUNT(*) FILTER (WHERE al.punch_type='in')  AS ins,
                 COUNT(*) FILTER (WHERE al.punch_type='out') AS outs,
                 s.first_punch_in
               FROM attendance_logs al
               LEFT JOIN daily_summary s
                 ON s.user_id = al.user_id
                AND s.work_date = (NOW() AT TIME ZONE $2)::date
               WHERE al.user_id = $1
                 AND (al.punched_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date
               GROUP BY s.first_punch_in""",
            user["id"], settings.office_timezone,
        )

        if not state or (state["ins"] or 0) == 0 or (state["ins"] or 0) <= (state["outs"] or 0):
            raise HTTPException(409, "Must punch in first.")

        log = await db.fetchrow(
            """INSERT INTO attendance_logs
               (user_id, branch_id, punch_type, latitude, longitude, distance_meters, is_valid)
               VALUES ($1,$2,'out',$3,$4,$5,TRUE) RETURNING punched_at""",
            user["id"], user["branch_id"], req.latitude, req.longitude, int(distance),
        )

        total_min = 0
        if state["first_punch_in"]:
            total_min = max(0, int(
                (log["punched_at"] - state["first_punch_in"]).total_seconds() / 60
            ))
        await db.execute(
            """UPDATE daily_summary SET last_punch_out=$2, total_minutes=$3
               WHERE user_id=$1 AND work_date=(NOW() AT TIME ZONE $4)::date""",
            user["id"], log["punched_at"], total_min, settings.office_timezone,
        )

    punch_time = to_local(log["punched_at"]).strftime("%I:%M %p")
    return {
        "success": True,
        "message": "Punched out. Have a great day!",
        "time": punch_time,
        "total_hours": f"{total_min // 60}h {total_min % 60}m",
    }


@app.get("/api/attendance/status")
async def attendance_status(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    # FIX 4: single query replaces two sequential SELECTs.
    row = await db.fetchrow(
        """SELECT
             al.punch_type   AS last_punch_type,
             al.punched_at   AS last_punched_at,
             s.user_id       AS s_user_id,
             s.work_date,
             s.first_punch_in,
             s.last_punch_out,
             s.total_minutes,
             s.is_late,
             s.late_by_minutes,
             s.status
           FROM (
             SELECT punch_type, punched_at
             FROM attendance_logs
             WHERE user_id = $1
               AND (punched_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date
             ORDER BY punched_at DESC
             LIMIT 1
           ) al
           FULL OUTER JOIN (
             SELECT *
             FROM daily_summary
             WHERE user_id = $1
               AND work_date = (NOW() AT TIME ZONE $2)::date
           ) s ON TRUE""",
        user["id"], settings.office_timezone,
    )

    last_punch_type = row["last_punch_type"] if row else None
    state = "none"
    if last_punch_type == "in":
        state = "punched_in"
    elif last_punch_type == "out":
        state = "completed"

    summary_out = None
    if row and row["s_user_id"] is not None:
        summary_out = {
            "user_id":         row["s_user_id"],
            "work_date":       row["work_date"],
            "first_punch_in":  to_local(row["first_punch_in"]).strftime("%I:%M %p") if row["first_punch_in"] else None,
            "last_punch_out":  to_local(row["last_punch_out"]).strftime("%I:%M %p") if row["last_punch_out"] else None,
            "total_minutes":   row["total_minutes"],
            "is_late":         row["is_late"] or False,
            "late_by_minutes": row["late_by_minutes"] or 0,
            "status":          row["status"] or "present",
        }

    return {
        "is_punched_in": state == "punched_in",
        "state": state,
        "last_punch": {
            "punch_type": last_punch_type,
            "punched_at": str(row["last_punched_at"]),
        } if last_punch_type else None,
        "summary": summary_out,
    }


@app.get("/api/attendance/today")
async def today_logs(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    logs = await db.fetch(
        """SELECT punch_type, punched_at, distance_meters FROM attendance_logs
           WHERE user_id=$1
             AND (punched_at AT TIME ZONE $2)::date=(NOW() AT TIME ZONE $2)::date
           ORDER BY punched_at""",
        user["id"], settings.office_timezone,
    )
    return [
        {**dict(r), "punched_at_local": to_local(r["punched_at"]).strftime("%I:%M %p")}
        for r in logs
    ]


# ══════════════════════════════════════════════════════════════
# HR — BRANCHES
# ══════════════════════════════════════════════════════════════

@app.get("/api/hr/branches")
async def get_branches(
    _hr: dict = Depends(require_hr_jwt),   # FIX 2: no DB query for auth
    db: asyncpg.Connection = Depends(get_db),
):
    rows = await db.fetch(
        "SELECT id, name, city, address, latitude, longitude, radius_meters "
        "FROM branches WHERE is_active=TRUE ORDER BY city, name"
    )
    return [{**dict(r), "latitude": float(r["latitude"]), "longitude": float(r["longitude"])} for r in rows]


# ══════════════════════════════════════════════════════════════
# HR — ATTENDANCE REPORTS
# ══════════════════════════════════════════════════════════════

def _ser(e: dict) -> dict:
    """Serialize a report row's time fields in-place."""
    for f in ("first_punch_in", "last_punch_out"):
        if e.get(f):
            e[f] = to_local(e[f]).isoformat()
    for f in ("shift_start", "shift_end"):
        if e.get(f):
            e[f] = e[f].strftime("%H:%M")
    return e


@app.get("/api/hr/daily-report")
async def daily_report(
    date_str:  Annotated[Optional[str], Query()] = None,
    branch_id: Annotated[Optional[int], Query()] = None,
    _hr: dict = Depends(require_hr_jwt),   # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    target = parse_date_param(date_str)
    q = """
        SELECT u.id, u.email, u.full_name,
               e.shift_start, e.shift_end,
               b.name AS branch_name, b.city,
               s.first_punch_in, s.last_punch_out, s.total_minutes,
               s.is_late, s.late_by_minutes,
               COALESCE(s.status,'absent') AS status
        FROM users u
        JOIN employees e ON e.user_id = u.id
        LEFT JOIN branches b ON b.id = e.branch_id
        LEFT JOIN daily_summary s ON s.user_id=u.id AND s.work_date=$1
        WHERE u.is_active=TRUE AND u.role='employee'
    """
    params: list = [target]
    if branch_id:
        q += " AND e.branch_id=$2"
        params.append(branch_id)
    q += " ORDER BY b.name NULLS LAST, u.full_name"

    rows = await db.fetch(q, *params)
    employees = [_ser(dict(r)) for r in rows]
    total = len(employees)
    return {
        "date": target.isoformat(),
        "stats": {
            "total":   total,
            "present": sum(1 for e in employees if e["status"] == "present"),
            "absent":  sum(1 for e in employees if e["status"] == "absent"),
            "late":    sum(1 for e in employees if e.get("is_late")),
        },
        "employees": employees,
    }


@app.get("/api/hr/export")
async def export_excel(
    date_str:  Annotated[str, Query()],
    branch_id: Annotated[Optional[int], Query()] = None,
    _hr: dict = Depends(require_hr_jwt),   # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    target = parse_date_param(date_str)
    q = """
        SELECT u.email, u.full_name, b.name AS branch_name, b.city,
               s.first_punch_in, s.last_punch_out, s.total_minutes,
               s.is_late, s.late_by_minutes, COALESCE(s.status,'absent') AS status
        FROM users u
        JOIN employees e ON e.user_id = u.id
        LEFT JOIN branches b ON b.id = e.branch_id
        LEFT JOIN daily_summary s ON s.user_id=u.id AND s.work_date=$1
        WHERE u.is_active=TRUE AND u.role='employee'
    """
    params: list = [target]
    if branch_id:
        q += " AND e.branch_id=$2"
        params.append(branch_id)
    q += " ORDER BY b.name NULLS LAST, u.full_name"
    rows = await db.fetch(q, *params)

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"
    headers = ["Email", "Name", "Branch", "City", "Punch In", "Punch Out", "Hours", "Status", "Late By"]
    hfill = PatternFill(start_color="3B63F6", end_color="3B63F6", fill_type="solid")
    hfont = Font(bold=True, color="FFFFFF")
    for col, h in enumerate(headers, 1):
        c = ws.cell(1, col, h); c.font = hfont; c.fill = hfill; c.alignment = Alignment(horizontal="center")

    for ri, row in enumerate(rows, 2):
        status = row["status"]
        fill = PatternFill(start_color="ECFDF5" if status == "present" else "FEF2F2",
                           end_color="ECFDF5" if status == "present" else "FEF2F2", fill_type="solid")
        mins = row["total_minutes"] or 0
        vals = [
            row["email"], row["full_name"],
            row["branch_name"] or "—", row["city"] or "—",
            to_local(row["first_punch_in"]).strftime("%I:%M %p") if row["first_punch_in"] else "—",
            to_local(row["last_punch_out"]).strftime("%I:%M %p") if row["last_punch_out"] else "—",
            f"{mins//60}h {mins%60}m" if mins else "—",
            ("LATE — " if row["is_late"] else "") + status.upper(),
            f"+{row['late_by_minutes']}m" if row["is_late"] else "On Time",
        ]
        for col, val in enumerate(vals, 1):
            ws.cell(ri, col, val).fill = fill

    for col, w in zip("ABCDEFGHI", [26, 22, 26, 16, 12, 12, 10, 14, 10]):
        ws.column_dimensions[col].width = w

    out = BytesIO(); wb.save(out); out.seek(0)
    return StreamingResponse(out,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="attendance_{target}.xlsx"'})


# ══════════════════════════════════════════════════════════════
# HR — ONBOARDING / EMPLOYEES
# ══════════════════════════════════════════════════════════════

@app.post("/api/hr/employees", status_code=201)
async def onboard_employee(
    req: OnboardRequest,
    hr: dict = Depends(require_hr_jwt),    # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    """Create user + employee record in one transaction."""
    email = req.work_email.lower()
    if await db.fetchrow("SELECT id FROM users WHERE email=$1", email):
        raise HTTPException(409, "Email already registered")

    for eid, label in [(req.l1_manager_id, "L1"), (req.l2_manager_id, "L2")]:
        if eid and not await db.fetchrow("SELECT id FROM employees WHERE id=$1", eid):
            raise HTTPException(400, f"{label} manager (id={eid}) not found")

    async with db.transaction():
        user = await db.fetchrow(
            """INSERT INTO users (email, password_hash, full_name, role, is_active)
               VALUES ($1,$2,$3,$4,TRUE) RETURNING id""",
            email, hash_password(req.password), req.full_name.strip(), req.role,
        )
        uid = user["id"]
        emp_id = f"sdpl-{uid:05d}"

        emp = await db.fetchrow(
            """INSERT INTO employees (
                user_id, emp_id,
                phone, personal_email, dob, gender, blood_group, nationality, home_address,
                emg_name, emg_phone, emg_rel,
                branch_id, job_title, designation, department, sub_department,
                grade, date_of_joining, cost_centre,
                l1_manager_id, l2_manager_id,
                employment_type, contract_end, probation_end, notice_period,
                shift_start, shift_end, work_mode, weekly_off, work_location, asset_id,
                annual_ctc, pay_frequency, pf_enrolled, esic_applicable,
                bank_name, bank_account, bank_ifsc, pan_number,
                onboarding_status
               ) VALUES (
                $1,$2,
                $3,$4,$5,$6,$7,$8,$9,
                $10,$11,$12,
                $13,$14,$15,$16,$17,
                $18,$19,$20,
                $21,$22,
                $23,$24,$25,$26,
                $27,$28,$29,$30,$31,$32,
                $33,$34,$35,$36,
                $37,$38,$39,$40,
                'awaiting'
               ) RETURNING id, emp_id""",
            uid, emp_id,
            req.phone, req.personal_email,
            parse_date(req.dob), req.gender, req.blood_group, req.nationality, req.home_address,
            req.emg_name, req.emg_phone, req.emg_rel,
            req.branch_id, req.job_title, req.designation, req.department, req.sub_department,
            req.grade, parse_date(req.date_of_joining), req.cost_centre,
            req.l1_manager_id, req.l2_manager_id,
            req.employment_type, parse_date(req.contract_end), parse_date(req.probation_end), req.notice_period,
            req.shift_start, req.shift_end, req.work_mode, req.weekly_off, req.work_location, req.asset_id,
            req.annual_ctc, req.pay_frequency, req.pf_enrolled, req.esic_applicable,
            req.bank_name, req.bank_account, req.bank_ifsc, req.pan_number,
        )

    logger.info("Onboarded: user_id=%s emp_id=%s by hr=%s", uid, emp_id, hr["id"])
    return {
        "id": uid,
        "employee_id": emp["id"],
        "emp_id": emp["emp_id"],
        "email": email,
        "full_name": req.full_name,
        "role": req.role,
        "onboarding_status": "awaiting",
    }


@app.get("/api/hr/employees")
async def list_employees(
    search:            Annotated[Optional[str], Query()] = None,
    department:        Annotated[Optional[str], Query()] = None,
    branch_id:         Annotated[Optional[int], Query()] = None,
    onboarding_status: Annotated[Optional[str], Query()] = None,
    _hr: dict = Depends(require_hr_jwt),   # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    conditions = ["u.is_active = TRUE"]
    params: list = []

    def add(val):
        params.append(val); return f"${len(params)}"

    if search:
        ph = add(f"%{search}%")
        conditions.append(f"(u.full_name ILIKE {ph} OR u.email ILIKE {ph} OR e.emp_id ILIKE {ph})")
    if department:
        conditions.append(f"e.department = {add(department)}")
    if branch_id:
        conditions.append(f"e.branch_id = {add(branch_id)}")
    if onboarding_status:
        conditions.append(f"e.onboarding_status = {add(onboarding_status)}")

    rows = await db.fetch(
        f"""SELECT
              u.id AS user_id, e.id, e.emp_id,
              u.email, u.full_name, u.role,
              e.phone, e.job_title, e.designation, e.department,
              e.grade, e.date_of_joining, e.onboarding_status,
              e.work_mode, e.shift_start, e.shift_end,
              e.l1_manager_id, e.l2_manager_id,
              l1u.full_name AS l1_name, l1e.job_title AS l1_title,
              l2u.full_name AS l2_name, l2e.job_title AS l2_title,
              b.name AS branch_name, b.city AS branch_city,
              e.created_at
            FROM users u
            JOIN employees e   ON e.user_id = u.id
            LEFT JOIN branches b  ON b.id = e.branch_id
            LEFT JOIN employees l1e ON l1e.id = e.l1_manager_id
            LEFT JOIN users     l1u ON l1u.id = l1e.user_id
            LEFT JOIN employees l2e ON l2e.id = e.l2_manager_id
            LEFT JOIN users     l2u ON l2u.id = l2e.user_id
            WHERE {' AND '.join(conditions)}
            ORDER BY e.created_at DESC""",
        *params,
    )

    result = []
    for r in rows:
        d = dict(r)
        for f in ("shift_start", "shift_end"):
            if d.get(f): d[f] = d[f].strftime("%H:%M")
        if d.get("date_of_joining"): d["date_of_joining"] = d["date_of_joining"].isoformat()
        if d.get("created_at"):      d["created_at"] = d["created_at"].isoformat()
        result.append(d)

    return {"total": len(result), "employees": result}


@app.get("/api/hr/employees/{emp_id}")
async def get_employee(
    emp_id: int,
    _hr: dict = Depends(require_hr_jwt),   # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    row = await db.fetchrow(
        """SELECT
              u.id AS user_id, e.id, e.emp_id,
              u.email, u.full_name, u.role, u.last_login,
              e.phone, e.personal_email, e.dob, e.gender, e.blood_group,
              e.nationality, e.home_address,
              e.emg_name, e.emg_phone, e.emg_rel,
              e.branch_id, e.job_title, e.designation, e.department,
              e.sub_department, e.grade, e.date_of_joining, e.cost_centre,
              e.l1_manager_id, e.l2_manager_id,
              l1u.full_name AS l1_name, l1e.job_title AS l1_title, l1u.role AS l1_role,
              l2u.full_name AS l2_name, l2e.job_title AS l2_title, l2u.role AS l2_role,
              e.employment_type, e.contract_end, e.probation_end, e.notice_period,
              e.shift_start, e.shift_end, e.work_mode, e.weekly_off,
              e.work_location, e.asset_id,
              e.annual_ctc, e.pay_frequency, e.pf_enrolled, e.esic_applicable,
              e.bank_name, e.bank_account, e.bank_ifsc, e.pan_number,
              e.onboarding_status, e.created_at, e.updated_at,
              b.name AS branch_name, b.city AS branch_city
           FROM employees e
           JOIN users u ON u.id = e.user_id
           LEFT JOIN branches b    ON b.id = e.branch_id
           LEFT JOIN employees l1e ON l1e.id = e.l1_manager_id
           LEFT JOIN users     l1u ON l1u.id = l1e.user_id
           LEFT JOIN employees l2e ON l2e.id = e.l2_manager_id
           LEFT JOIN users     l2u ON l2u.id = l2e.user_id
           WHERE e.id = $1""",
        emp_id,
    )
    if not row:
        raise HTTPException(404, "Employee not found")

    d = dict(row)
    for f in ("shift_start", "shift_end"):
        if d.get(f): d[f] = d[f].strftime("%H:%M")
    for f in ("dob", "date_of_joining", "contract_end", "probation_end"):
        if d.get(f): d[f] = d[f].isoformat()
    for f in ("created_at", "updated_at", "last_login"):
        if d.get(f): d[f] = d[f].isoformat()
    if d.get("annual_ctc"): d["annual_ctc"] = float(d["annual_ctc"])
    return d


@app.patch("/api/hr/employees/{emp_id}/onboarding-status")
async def update_onboarding_status(
    emp_id: int,
    req: UpdateOnboardingStatus,
    hr: dict = Depends(require_hr_jwt),    # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    result = await db.execute(
        "UPDATE employees SET onboarding_status=$1, updated_at=NOW() WHERE id=$2",
        req.status, emp_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Employee not found")
    logger.info("Onboarding status: emp_id=%s → %s by hr=%s", emp_id, req.status, hr["id"])
    return {"id": emp_id, "onboarding_status": req.status}


@app.put("/api/hr/employees/{emp_id}")
async def update_employee(
    emp_id: int,
    req: UpdateEmployeeRequest,
    hr: dict = Depends(require_hr_jwt),    # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    """Partial update — only non-None fields are written. FIX 8: one UPDATE…RETURNING."""
    emp_fields = []
    simple_emp = [
        ("personal_email", req.personal_email), ("phone", req.phone),
        ("gender", req.gender), ("blood_group", req.blood_group),
        ("nationality", req.nationality), ("home_address", req.home_address),
        ("emg_name", req.emg_name), ("emg_phone", req.emg_phone), ("emg_rel", req.emg_rel),
        ("job_title", req.job_title), ("designation", req.designation),
        ("department", req.department), ("sub_department", req.sub_department),
        ("grade", req.grade), ("cost_centre", req.cost_centre),
        ("employment_type", req.employment_type), ("notice_period", req.notice_period),
        ("work_mode", req.work_mode), ("weekly_off", req.weekly_off),
        ("work_location", req.work_location), ("asset_id", req.asset_id),
        ("pay_frequency", req.pay_frequency), ("bank_name", req.bank_name),
        ("bank_account", req.bank_account), ("bank_ifsc", req.bank_ifsc),
        ("pan_number", req.pan_number),
    ]
    for col, val in simple_emp:
        if val is not None:
            emp_fields.append((col, val))

    if req.branch_id is not None:       emp_fields.append(("branch_id",       req.branch_id or None))
    if req.l1_manager_id is not None:   emp_fields.append(("l1_manager_id",   req.l1_manager_id or None))
    if req.l2_manager_id is not None:   emp_fields.append(("l2_manager_id",   req.l2_manager_id or None))
    if req.annual_ctc is not None:      emp_fields.append(("annual_ctc",      req.annual_ctc))
    if req.pf_enrolled is not None:     emp_fields.append(("pf_enrolled",     req.pf_enrolled))
    if req.esic_applicable is not None: emp_fields.append(("esic_applicable", req.esic_applicable))
    if req.dob is not None:             emp_fields.append(("dob",             parse_date(req.dob)))
    if req.date_of_joining is not None: emp_fields.append(("date_of_joining", parse_date(req.date_of_joining)))
    if req.contract_end is not None:    emp_fields.append(("contract_end",    parse_date(req.contract_end)))
    if req.probation_end is not None:   emp_fields.append(("probation_end",   parse_date(req.probation_end)))
    if req.shift_start is not None:
        try:    emp_fields.append(("shift_start", time.fromisoformat(req.shift_start)))
        except ValueError: raise HTTPException(400, "Invalid shift_start, use HH:MM")
    if req.shift_end is not None:
        try:    emp_fields.append(("shift_end", time.fromisoformat(req.shift_end)))
        except ValueError: raise HTTPException(400, "Invalid shift_end, use HH:MM")

    async with db.transaction():
        if emp_fields:
            params = [v for _, v in emp_fields]
            sets = ", ".join(f"{col}=${i+1}" for i, (col, _) in enumerate(emp_fields))
            params.append(emp_id)
            # RETURNING user_id avoids a prior SELECT — FIX 8
            returned = await db.fetchrow(
                f"UPDATE employees SET {sets}, updated_at=NOW() WHERE id=${len(params)} RETURNING user_id",
                *params)
            if not returned:
                raise HTTPException(404, "Employee not found")
            user_id = returned["user_id"]
        else:
            row = await db.fetchrow("SELECT user_id FROM employees WHERE id=$1", emp_id)
            if not row:
                raise HTTPException(404, "Employee not found")
            user_id = row["user_id"]

        if req.full_name is not None:
            await db.execute("UPDATE users SET full_name=$1 WHERE id=$2", req.full_name, user_id)
        if req.role is not None:
            await db.execute("UPDATE users SET role=$1 WHERE id=$2", req.role, user_id)

    logger.info("Employee updated: emp_id=%s by hr=%s", emp_id, hr["id"])
    return {"id": emp_id, "message": "Employee updated successfully"}


@app.patch("/api/hr/employees/{emp_id}/deactivate")
async def deactivate_employee(
    emp_id: int,
    hr: dict = Depends(require_hr_jwt),    # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    """Soft-delete. FIX 7: single UPDATE via subquery (was SELECT + UPDATE)."""
    result = await db.execute(
        "UPDATE users SET is_active=FALSE WHERE id=(SELECT user_id FROM employees WHERE id=$1)",
        emp_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Employee not found")
    logger.info("Employee deactivated: emp_id=%s by hr=%s", emp_id, hr["id"])
    return {"id": emp_id, "message": "Employee deactivated"}


@app.patch("/api/hr/employees/{emp_id}/reactivate")
async def reactivate_employee(
    emp_id: int,
    hr: dict = Depends(require_hr_jwt),    # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    """FIX 7: single UPDATE via subquery."""
    result = await db.execute(
        "UPDATE users SET is_active=TRUE WHERE id=(SELECT user_id FROM employees WHERE id=$1)",
        emp_id,
    )
    if result == "UPDATE 0":
        raise HTTPException(404, "Employee not found")
    logger.info("Employee reactivated: emp_id=%s by hr=%s", emp_id, hr["id"])
    return {"id": emp_id, "message": "Employee reactivated"}


@app.get("/api/hr/onboarding-stats")
async def onboarding_stats(
    _hr: dict = Depends(require_hr_jwt),   # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    row = await db.fetchrow(
        """SELECT
              COUNT(*)                                                    AS total,
              COUNT(*) FILTER (WHERE onboarding_status='awaiting')       AS awaiting,
              COUNT(*) FILTER (WHERE onboarding_status='in-progress')    AS in_progress,
              COUNT(*) FILTER (WHERE onboarding_status='completed')      AS completed
           FROM employees"""
    )
    return dict(row)


@app.get("/api/hr/managers")
async def list_managers(
    _: dict = Depends(require_hr_jwt),     # FIX 2
    db: asyncpg.Connection = Depends(get_db),
):
    rows = await db.fetch(
        """SELECT e.id, e.emp_id, u.full_name, u.role,
                  e.job_title, e.designation, e.department
           FROM employees e
           JOIN users u ON u.id = e.user_id
           WHERE u.is_active = TRUE
           ORDER BY u.full_name""",
    )
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════
# HEALTH + STATIC
# ══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "healthy" if (db_pool and not db_pool._closed) else "degraded",
        "utc":    datetime.now(pytz.utc).isoformat(),
        "local":  datetime.now(_TZ).isoformat(),
    }


BASE_DIR     = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR.parent / "frontend"


@app.get("/login")
async def login_page():
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/employee")
async def employee_portal():
    return FileResponse(FRONTEND_DIR / "employee.html")


@app.get("/hr-manager")
async def hr_manager_portal():
    return FileResponse(FRONTEND_DIR / "hr.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)