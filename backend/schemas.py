"""
schemas.py — Pydantic request/response models for the Attendance System.

Keeps main.py clean: endpoints declare return types explicitly,
the API is self-documenting, and frontend contract breakage is caught at runtime.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, field_validator, ConfigDict


# ─── Shared config ────────────────────────────────────────
class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)   # lets us do Model.model_validate(dict(row))


# ══════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════

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


class UserPublic(_Base):
    """Returned on /auth/me and embedded in login response."""
    id: int
    email: str
    full_name: str
    role: str
    branch_id: Optional[int] = None
    # Shift times serialised as "HH:MM" strings by the endpoint
    shift_start: Optional[str] = None
    shift_end: Optional[str] = None
    branch_name: Optional[str] = None
    branch_city: Optional[str] = None
    branch_lat: Optional[float] = None
    branch_lng: Optional[float] = None
    radius_meters: Optional[int] = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserPublic


class RegisterResponse(BaseModel):
    id: int
    email: str
    full_name: str
    role: str


# ══════════════════════════════════════════════════════════
# ATTENDANCE
# ══════════════════════════════════════════════════════════

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


class PunchInResponse(BaseModel):
    success: bool
    message: str
    time: str                       # "09:07 AM"
    distance: int                   # metres
    is_late: bool
    late_by_minutes: int


class PunchOutResponse(BaseModel):
    success: bool
    message: str
    time: str                       # "06:12 PM"
    total_hours: str                # "8h 12m"


class DailySummaryOut(BaseModel):
    """Embedded inside StatusResponse."""
    user_id: int
    work_date: date
    first_punch_in: Optional[str] = None   # "09:07 AM"
    last_punch_out: Optional[str] = None   # "06:12 PM"
    total_minutes: Optional[int] = None
    is_late: bool = False
    late_by_minutes: int = 0
    status: str = "present"


class LastPunch(BaseModel):
    punch_type: str
    punched_at: str


class StatusResponse(BaseModel):
    is_punched_in: bool
    state: str                             # "none" | "punched_in" | "completed"
    last_punch: Optional[LastPunch] = None
    summary: Optional[DailySummaryOut] = None


class AttendanceLogOut(BaseModel):
    punch_type: str
    punched_at: datetime
    punched_at_local: str                  # "09:07 AM"
    distance_meters: int


# ══════════════════════════════════════════════════════════
# HR
# ══════════════════════════════════════════════════════════

class BranchOut(_Base):
    id: int
    name: str
    city: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    radius_meters: int


class EmployeeReportRow(BaseModel):
    """One row in the daily HR report."""
    id: int
    email: str
    full_name: str
    shift_start: Optional[str] = None     # "09:00"
    shift_end: Optional[str] = None       # "18:00"
    branch_name: Optional[str] = None
    city: Optional[str] = None
    first_punch_in: Optional[str] = None  # ISO datetime string
    last_punch_out: Optional[str] = None
    total_minutes: Optional[int] = None
    is_late: Optional[bool] = None
    late_by_minutes: Optional[int] = None
    status: str = "absent"


class ReportStats(BaseModel):
    total: int
    present: int
    absent: int
    late: int


class DailyReportResponse(BaseModel):
    date: str
    stats: ReportStats
    employees: list[EmployeeReportRow]