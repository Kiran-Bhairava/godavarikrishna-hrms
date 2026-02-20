"""
Attendance System — FastAPI Backend
Production-ready internal app
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
import math
import os
import pytz

from datetime import datetime, timedelta, date
from io import BytesIO
from typing import Optional, Annotated

import dotenv
from fastapi import FastAPI, Depends, HTTPException, Query, status
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

# ─── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger("attendance")


# ─── Settings ─────────────────────────────────────────────
class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:postgres@localhost:5432/attendance_db"
    secret_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_hours: int = 8
    office_timezone: str = "Asia/Kolkata"
    # Comma-separated allowed origins; empty = same-origin only
    cors_origins: str = ""
    # Grace period (minutes) before marking late
    late_grace_minutes: int = 0
    db_pool_min: int = 5
    db_pool_max: int = 20

    model_config = {"env_file": ".env", "case_sensitive": False}

    @property
    def allowed_origins(self) -> list[str]:
        if not self.cors_origins:
            return []
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()

if not settings.secret_key:
    raise RuntimeError(
        "SECRET_KEY env var is required and must not be empty. "
        "Set it in your .env file or environment."
    )


######kiran#########

# ─── App (lifespan) ───────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool
    logger.info("Connecting to database…")
    db_pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
    logger.info("Database pool ready.")
    yield
    if db_pool:
        await db_pool.close()
        logger.info("Database pool closed.")


app = FastAPI(
    title="Attendance System",
    docs_url=None,       # Disable Swagger in production; enable if needed
    redoc_url=None,
    lifespan=lifespan,
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()

# CORS — restrict to configured origins only (empty = no CORS headers)
if settings.allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )

# ─── Database ─────────────────────────────────────────────
db_pool: Optional[asyncpg.Pool] = None


async def get_db():
    if db_pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    async with db_pool.acquire() as conn:
        yield conn


# ─── Pydantic Models ──────────────────────────────────────
class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def password_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Password must not be empty")
        return v


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str

    @field_validator("password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class PunchRequest(BaseModel):
    latitude: float
    longitude: float

    @field_validator("latitude")
    @classmethod
    def valid_latitude(cls, v: float) -> float:
        if not (-90 <= v <= 90):
            raise ValueError("Latitude must be between -90 and 90")
        return v

    @field_validator("longitude")
    @classmethod
    def valid_longitude(cls, v: float) -> float:
        if not (-180 <= v <= 180):
            raise ValueError("Longitude must be between -180 and 180")
        return v


# ─── Helpers ──────────────────────────────────────────────
def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return distance in metres between two GPS coordinates."""
    R = 6_371_000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def create_token(user_id: int, email: str, role: str) -> str:
    expire = datetime.now(tz=pytz.utc) + timedelta(hours=settings.access_token_expire_hours)
    return jwt.encode(
        {"sub": str(user_id), "email": email, "role": role, "exp": expire},
        settings.secret_key,
        algorithm=settings.algorithm,
    )


def get_local_now() -> datetime:
    """Current datetime in office timezone (aware)."""
    return datetime.now(pytz.timezone(settings.office_timezone))


def utc_to_local(dt: Optional[datetime]) -> Optional[datetime]:
    """Convert a UTC-aware or naive datetime to office local time."""
    if dt is None:
        return None
    tz = pytz.timezone(settings.office_timezone)
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(tz)


def parse_date_param(date_str: Optional[str], param_name: str = "date_str") -> date:
    """Parse a YYYY-MM-DD string; raise 400 on bad format."""
    if not date_str:
        return date.today()
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {param_name} format. Expected YYYY-MM-DD.",
        )


def serialize_employee_row(e: dict) -> dict:
    """Convert DB row fields to JSON-safe types."""
    if e.get("first_punch_in"):
        e["first_punch_in"] = utc_to_local(e["first_punch_in"]).isoformat()
    if e.get("last_punch_out"):
        e["last_punch_out"] = utc_to_local(e["last_punch_out"]).isoformat()
    if e.get("shift_start"):
        e["shift_start"] = e["shift_start"].strftime("%H:%M")
    if e.get("shift_end"):
        e["shift_end"] = e["shift_end"].strftime("%H:%M")
    return e


# ─── Auth Dependency ──────────────────────────────────────
async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: asyncpg.Connection = Depends(get_db),
) -> dict:
    try:
        payload = jwt.decode(
            credentials.credentials, settings.secret_key, algorithms=[settings.algorithm]
        )
        user_id = payload.get("sub")
        if not user_id:
            raise HTTPException(status_code=401, detail="Invalid token")
        user_id = int(user_id)
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = await db.fetchrow(
        """SELECT u.id, u.email, u.full_name, u.role, u.branch_id,
                  u.shift_start, u.shift_end,
                  b.name  AS branch_name,  b.city    AS branch_city,
                  b.latitude  AS branch_lat, b.longitude AS branch_lng,
                  b.radius_meters
           FROM users u
           LEFT JOIN branches b ON u.branch_id = b.id
           WHERE u.id = $1 AND u.is_active = TRUE""",
        user_id,
    )

    if not user:
        raise HTTPException(status_code=401, detail="User not found or deactivated")

    return dict(user)


async def require_hr(user: dict = Depends(get_current_user)) -> dict:
    if user["role"] not in ("hr", "admin"):
        raise HTTPException(status_code=403, detail="HR/Admin access required")
    return user


# ══════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════

@app.post("/api/auth/register", status_code=201)
async def register(req: RegisterRequest, db: asyncpg.Connection = Depends(get_db)):
    """
    Self-registration endpoint.
    In a fully locked-down internal deployment, disable this and provision
    users via an admin panel or direct DB seeding.
    """
    existing = await db.fetchrow("SELECT id FROM users WHERE email = $1", req.email.lower())
    if existing:
        raise HTTPException(status_code=409, detail="Email already registered")

    full_name = req.email.split("@")[0].replace(".", " ").replace("_", " ").title()
    user = await db.fetchrow(
        """INSERT INTO users (email, password_hash, full_name, role, is_active)
           VALUES ($1, $2, $3, 'employee', TRUE)
           RETURNING id, email, full_name, role""",
        req.email.lower(),
        hash_password(req.password),
        full_name,
    )
    logger.info("New user registered: %s", req.email)
    return dict(user)


@app.post("/api/auth/login")
async def login(req: LoginRequest, db: asyncpg.Connection = Depends(get_db)):
    user = await db.fetchrow(
        """SELECT id, email, password_hash, full_name, role, is_active
           FROM users WHERE email = $1""",
        req.email.lower(),
    )

    if not user or not verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user["is_active"]:
        raise HTTPException(status_code=403, detail="Account deactivated. Contact HR.")

    await db.execute("UPDATE users SET last_login = NOW() WHERE id = $1", user["id"])
    logger.info("Login: user_id=%s email=%s role=%s", user["id"], user["email"], user["role"])

    token = create_token(user["id"], user["email"], user["role"])
    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "email": user["email"],
            "full_name": user["full_name"],
            "role": user["role"],
        },
    }


@app.post("/api/auth/logout")
async def logout():
    # Stateless JWT — client discards token. Extend with a token denylist if needed.
    return {"message": "Logged out successfully"}


@app.get("/api/auth/me")
async def get_me(user: dict = Depends(get_current_user)):
    result = dict(user)
    if result.get("shift_start"):
        result["shift_start"] = result["shift_start"].strftime("%H:%M")
    if result.get("shift_end"):
        result["shift_end"] = result["shift_end"].strftime("%H:%M")
    for field in ("branch_lat", "branch_lng"):
        if result.get(field) is not None:
            result[field] = float(result[field])
    return result


# ══════════════════════════════════════════════════════════
# ATTENDANCE ROUTES
# ══════════════════════════════════════════════════════════

async def _get_today_punch_types(db: asyncpg.Connection, user_id: int) -> list[str]:
    """Fetch punch types for the current local day, ordered chronologically."""
    rows = await db.fetch(
        """SELECT punch_type FROM attendance_logs
           WHERE user_id = $1
             AND (punched_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date
           ORDER BY punched_at ASC""",
        user_id, settings.office_timezone,
    )
    return [r["punch_type"] for r in rows]


@app.post("/api/attendance/punch-in")
async def punch_in(
    req: PunchRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    if not user["branch_id"]:
        raise HTTPException(status_code=400, detail="No branch assigned. Contact HR.")

    # ── Geofence check ──────────────────────────────────────
    distance = haversine(
        req.latitude, req.longitude,
        float(user["branch_lat"]), float(user["branch_lng"]),
    )
    radius = user["radius_meters"] or 200
    if distance > radius:
        raise HTTPException(
            status_code=403,
            detail=f"You are {int(distance)}m from office. Must be within {radius}m to punch in.",
        )

    # ── Prevent duplicate punch-in ───────────────────────────
    punch_types = await _get_today_punch_types(db, user["id"])

    if punch_types and punch_types[-1] == "in":
        raise HTTPException(status_code=409, detail="Already punched in. Please punch out first.")

    if "in" in punch_types and "out" in punch_types:
        raise HTTPException(
            status_code=409,
            detail="Attendance already completed for today. See you tomorrow!",
        )

    # ── Late calculation (with configurable grace period) ────
    local_now = get_local_now()
    punch_time_local = local_now.time()
    shift_start = user["shift_start"]  # datetime.time from DB

    is_late = False
    late_minutes = 0
    if shift_start:
        delta_seconds = (
            datetime.combine(date.today(), punch_time_local)
            - datetime.combine(date.today(), shift_start)
        ).total_seconds()
        grace_seconds = settings.late_grace_minutes * 60
        if delta_seconds > grace_seconds:
            is_late = True
            late_minutes = max(0, int((delta_seconds - grace_seconds) / 60))

    async with db.transaction():
        log = await db.fetchrow(
            """INSERT INTO attendance_logs
               (user_id, branch_id, punch_type, latitude, longitude, distance_meters, is_valid)
               VALUES ($1, $2, 'in', $3, $4, $5, TRUE)
               RETURNING id, punched_at""",
            user["id"], user["branch_id"],
            req.latitude, req.longitude, int(distance),
        )

        await db.execute(
            """INSERT INTO daily_summary
               (user_id, work_date, first_punch_in, is_late, late_by_minutes, status)
               VALUES ($1, (NOW() AT TIME ZONE $2)::date, $3, $4, $5, 'present')
               ON CONFLICT (user_id, work_date) DO UPDATE SET
                 first_punch_in  = EXCLUDED.first_punch_in,
                 is_late         = EXCLUDED.is_late,
                 late_by_minutes = EXCLUDED.late_by_minutes,
                 status          = 'present'""",
            user["id"], settings.office_timezone, log["punched_at"], is_late, late_minutes,
        )

    local_punch = utc_to_local(log["punched_at"])
    late_msg = f" — late by {late_minutes} min" if is_late else ""
    logger.info("Punch-in: user_id=%s dist=%dm late=%s", user["id"], int(distance), is_late)

    return {
        "success": True,
        "message": f"Punched in successfully{late_msg}",
        "time": local_punch.strftime("%I:%M %p"),
        "distance": int(distance),
        "is_late": is_late,
        "late_by_minutes": late_minutes,
    }


@app.post("/api/attendance/punch-out")
async def punch_out(
    req: PunchRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    if not user["branch_id"]:
        raise HTTPException(status_code=400, detail="No branch assigned. Contact HR.")

    # Record GPS distance (no geofence enforced on punch-out)
    distance = 0.0
    if user["branch_lat"] and user["branch_lng"]:
        distance = haversine(
            req.latitude, req.longitude,
            float(user["branch_lat"]), float(user["branch_lng"]),
        )

    punch_types = await _get_today_punch_types(db, user["id"])

    if not punch_types or punch_types[-1] != "in":
        raise HTTPException(status_code=409, detail="Must punch in before punching out.")

    async with db.transaction():
        log = await db.fetchrow(
            """INSERT INTO attendance_logs
               (user_id, branch_id, punch_type, latitude, longitude, distance_meters, is_valid)
               VALUES ($1, $2, 'out', $3, $4, $5, TRUE)
               RETURNING id, punched_at""",
            user["id"], user["branch_id"],
            req.latitude, req.longitude, int(distance),
        )

        summary = await db.fetchrow(
            """SELECT first_punch_in FROM daily_summary
               WHERE user_id = $1 AND work_date = (NOW() AT TIME ZONE $2)::date""",
            user["id"], settings.office_timezone,
        )

        total_minutes = 0
        if summary and summary["first_punch_in"]:
            delta = log["punched_at"] - summary["first_punch_in"]
            total_minutes = max(0, int(delta.total_seconds() / 60))

        await db.execute(
            """UPDATE daily_summary
               SET last_punch_out = $2, total_minutes = $3
               WHERE user_id = $1 AND work_date = (NOW() AT TIME ZONE $4)::date""",
            user["id"], log["punched_at"], total_minutes, settings.office_timezone,
        )

    local_punch = utc_to_local(log["punched_at"])
    hours_str = f"{total_minutes // 60}h {total_minutes % 60}m"
    logger.info("Punch-out: user_id=%s total_minutes=%d", user["id"], total_minutes)

    return {
        "success": True,
        "message": "Punched out successfully. Have a great day!",
        "time": local_punch.strftime("%I:%M %p"),
        "total_hours": hours_str,
    }


@app.get("/api/attendance/status")
async def get_status(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    today_logs = await db.fetch(
        """SELECT punch_type, punched_at FROM attendance_logs
           WHERE user_id = $1
             AND (punched_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date
           ORDER BY punched_at ASC""",
        user["id"], settings.office_timezone,
    )

    summary = await db.fetchrow(
        """SELECT * FROM daily_summary
           WHERE user_id = $1 AND work_date = (NOW() AT TIME ZONE $2)::date""",
        user["id"], settings.office_timezone,
    )

    punch_types = [r["punch_type"] for r in today_logs]
    last = today_logs[-1] if today_logs else None

    state = "none"
    if punch_types:
        if punch_types[-1] == "in":
            state = "punched_in"
        elif "in" in punch_types and punch_types[-1] == "out":
            state = "completed"

    summary_dict = None
    if summary:
        s = dict(summary)
        if s.get("first_punch_in"):
            s["first_punch_in"] = utc_to_local(s["first_punch_in"]).strftime("%I:%M %p")
        if s.get("last_punch_out"):
            s["last_punch_out"] = utc_to_local(s["last_punch_out"]).strftime("%I:%M %p")
        summary_dict = s

    return {
        "is_punched_in": state == "punched_in",
        "state": state,
        "last_punch": (
            {"punch_type": last["punch_type"], "punched_at": str(last["punched_at"])}
            if last else None
        ),
        "summary": summary_dict,
    }


@app.get("/api/attendance/today")
async def get_today_logs(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    logs = await db.fetch(
        """SELECT punch_type, punched_at, distance_meters
           FROM attendance_logs
           WHERE user_id = $1
             AND (punched_at AT TIME ZONE $2)::date = (NOW() AT TIME ZONE $2)::date
           ORDER BY punched_at ASC""",
        user["id"], settings.office_timezone,
    )
    result = []
    for log in logs:
        d = dict(log)
        d["punched_at_local"] = utc_to_local(d["punched_at"]).strftime("%I:%M %p")
        result.append(d)
    return result


# ══════════════════════════════════════════════════════════
# HR ROUTES
# ══════════════════════════════════════════════════════════

@app.get("/api/hr/branches")
async def get_branches(
    user: dict = Depends(get_current_user),   # any authenticated user (employee needs it too)
    db: asyncpg.Connection = Depends(get_db),
):
    branches = await db.fetch(
        """SELECT id, name, city, address, latitude, longitude, radius_meters
           FROM branches WHERE is_active = TRUE ORDER BY city, name"""
    )
    return [
        {**dict(b), "latitude": float(b["latitude"]), "longitude": float(b["longitude"])}
        for b in branches
    ]


@app.get("/api/hr/daily-report")
async def daily_report(
    date_str: Annotated[Optional[str], Query()] = None,
    branch_id: Annotated[Optional[int], Query()] = None,
    user: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
):
    target_date = parse_date_param(date_str)

    query = """
        SELECT
            u.id, u.email, u.full_name, u.shift_start, u.shift_end,
            b.name AS branch_name, b.city,
            s.first_punch_in, s.last_punch_out, s.total_minutes,
            s.is_late, s.late_by_minutes, COALESCE(s.status, 'absent') AS status
        FROM users u
        LEFT JOIN branches b ON u.branch_id = b.id
        LEFT JOIN daily_summary s ON s.user_id = u.id AND s.work_date = $1
        WHERE u.is_active = TRUE AND u.role = 'employee'
    """
    params: list = [target_date]

    if branch_id:
        query += " AND u.branch_id = $2"
        params.append(branch_id)

    query += " ORDER BY b.name NULLS LAST, u.full_name"
    rows = await db.fetch(query, *params)

    employees = [serialize_employee_row(dict(row)) for row in rows]

    stats = {
        "total": len(employees),
        "present": sum(1 for e in employees if e["status"] == "present"),
        "absent": sum(1 for e in employees if e["status"] == "absent"),
        "late": sum(1 for e in employees if e.get("is_late")),
    }

    return {"date": target_date.isoformat(), "stats": stats, "employees": employees}


@app.get("/api/hr/export")
async def export_excel(
    date_str: Annotated[str, Query()],
    branch_id: Annotated[Optional[int], Query()] = None,
    user: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
):
    target_date = parse_date_param(date_str)

    query = """
        SELECT u.email, u.full_name, b.name AS branch_name, b.city,
               s.first_punch_in, s.last_punch_out, s.total_minutes,
               s.is_late, s.late_by_minutes, COALESCE(s.status, 'absent') AS status
        FROM users u
        LEFT JOIN branches b ON u.branch_id = b.id
        LEFT JOIN daily_summary s ON s.user_id = u.id AND s.work_date = $1
        WHERE u.is_active = TRUE AND u.role = 'employee'
    """
    params: list = [target_date]
    if branch_id:
        query += " AND u.branch_id = $2"
        params.append(branch_id)
    query += " ORDER BY b.name NULLS LAST, u.full_name"

    rows = await db.fetch(query, *params)

    wb = Workbook()
    ws = wb.active
    ws.title = "Attendance"

    headers = ["Email", "Name", "Branch", "City", "Punch In", "Punch Out", "Hours", "Status", "Late By"]
    header_fill = PatternFill(start_color="3B63F6", end_color="3B63F6", fill_type="solid")
    header_font = Font(bold=True, color="FFFFFF")
    center = Alignment(horizontal="center")

    for col, header in enumerate(headers, 1):
        cell = ws.cell(1, col, header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center

    for row_idx, row in enumerate(rows, 2):
        status = row["status"]
        fill_color = "ECFDF5" if status == "present" else "FEF2F2"
        row_fill = PatternFill(start_color=fill_color, end_color=fill_color, fill_type="solid")

        pin = utc_to_local(row["first_punch_in"]).strftime("%I:%M %p") if row["first_punch_in"] else "—"
        pout = utc_to_local(row["last_punch_out"]).strftime("%I:%M %p") if row["last_punch_out"] else "—"
        mins = row["total_minutes"] or 0
        hours = f"{mins // 60}h {mins % 60}m" if mins else "—"
        late_label = f"+{row['late_by_minutes']}m" if row["is_late"] else "On Time"
        status_label = ("LATE — " if row["is_late"] else "") + status.upper()

        values = [
            row["email"], row["full_name"],
            row["branch_name"] or "—", row["city"] or "—",
            pin, pout, hours, status_label, late_label,
        ]
        for col, val in enumerate(values, 1):
            cell = ws.cell(row_idx, col, val)
            cell.fill = row_fill

    for col_letter, width in zip("ABCDEFGHI", [26, 22, 26, 16, 12, 12, 10, 14, 10]):
        ws.column_dimensions[col_letter].width = width

    output = BytesIO()
    wb.save(output)
    output.seek(0)

    filename = f"attendance_{target_date.isoformat()}.xlsx"
    # RFC 5987-safe filename
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=\"{filename}\""},
    )


# ─── Health ───────────────────────────────────────────────
@app.get("/health")
async def health():
    tz = pytz.timezone(settings.office_timezone)
    db_ok = db_pool is not None and not db_pool._closed
    return {
        "status": "healthy" if db_ok else "degraded",
        "utc": datetime.now(pytz.utc).isoformat(),
        "local": datetime.now(tz).isoformat(),
        "timezone": settings.office_timezone,
    }


# ─── Frontend Static Files ────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
# Original layout: main.py lives in backend/, frontend/ is a sibling folder at root
# i.e.  project/
#           backend/main.py
#           frontend/index.html  employee.html  hr.html
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

# Catch-all static mount — must come last, after all /api and page routes
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )