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
    must_reset_password: bool = False


class RegisterResponse(BaseModel):
    id: int
    email: str
    full_name: str
    role: str


# ══════════════════════════════════════════════════════════
# CREDENTIALS & PASSWORD MANAGEMENT
# ══════════════════════════════════════════════════════════

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class GenerateCredentialsRequest(BaseModel):
    notes: Optional[str] = None


class GenerateCredentialsResponse(BaseModel):
    employee_id: int
    email: str
    full_name: str
    temporary_password: str
    must_reset_on_login: bool
    generated_at: str
    expires_in_days: int
    message: str


class ResetPasswordRequest(BaseModel):
    new_password: str

    @field_validator("new_password")
    @classmethod
    def password_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class CredentialAuditRow(BaseModel):
    id: int
    action: str
    is_temporary: bool
    performed_by_id: Optional[int] = None
    performed_by_name: Optional[str] = None
    notes: Optional[str] = None
    created_at: Optional[str] = None


class CredentialAuditResponse(BaseModel):
    user_id: int
    email: str
    full_name: str
    must_reset_password: bool
    last_password_change: Optional[str] = None
    last_login: Optional[str] = None
    credential_history: list[CredentialAuditRow]


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


# ══════════════════════════════════════════════════════════════
# REGULARIZATION
# ══════════════════════════════════════════════════════════════

class RegularizationRequestCreate(BaseModel):
    work_date: date
    actual_worked_minutes: int
    requested_minutes: int
    reason: str

    @field_validator("requested_minutes")
    @classmethod
    def validate_requested(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("Requested minutes must be greater than 0")
        if v > 480:  # max 8 hours
            raise ValueError("Cannot request more than 8 hours")
        return v

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 3:
            raise ValueError("Reason must be at least 3 characters")
        if len(v) > 500:
            raise ValueError("Reason must be at most 500 characters")
        return v


class RegularizationApprovalRequest(BaseModel):
    comment: Optional[str] = None

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > 500:
            raise ValueError("Comment must be at most 500 characters")
        return v


class RegularizationRejectionRequest(BaseModel):
    comment: str

    @field_validator("comment")
    @classmethod
    def validate_comment(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 5:
            raise ValueError("Comment must be at least 5 characters")
        if len(v) > 500:
            raise ValueError("Comment must be at most 500 characters")
        return v


class RegularizationRequestRow(BaseModel):
    request_id: int
    work_date: date
    actual_worked_minutes: int
    actual_worked: str  # "7h 30m"
    requested_minutes: int
    requested_display: str  # "1h 30m"
    reason: str
    submitted_at: datetime
    l1_status: str
    l1_manager_name: Optional[str] = None
    l1_approved_at: Optional[datetime] = None
    l2_status: Optional[str] = None
    l2_manager_name: Optional[str] = None
    l2_approved_at: Optional[datetime] = None
    final_status: str
    payroll_impact: str  # "present" or "absent"


class RegularizationRequestsListResponse(BaseModel):
    month: str  # "2024-12"
    total: int
    approved: int
    rejected: int
    pending: int
    monthly_limit_hours: int
    approved_hours_this_month: int
    requests: list[RegularizationRequestRow]


class RegularizationRequestDetail(BaseModel):
    request_id: int
    employee_id: int
    employee_name: str
    work_date: date
    actual_worked_minutes: int
    actual_worked_display: str
    shift_minutes: int
    shift_display: str
    gap_minutes: int
    gap_display: str
    requested_minutes: int
    requested_display: str
    reason: str
    submitted_at: datetime
    
    l1_manager_id: Optional[int] = None
    l1_manager_name: Optional[str] = None
    l1_status: str
    l1_comment: Optional[str] = None
    l1_approved_at: Optional[datetime] = None
    
    l2_manager_id: Optional[int] = None
    l2_manager_name: Optional[str] = None
    l2_status: Optional[str] = None
    l2_comment: Optional[str] = None
    l2_approved_at: Optional[datetime] = None
    
    requires_l2_approval: bool
    final_status: str
    is_regularized: bool
    payroll_status: str
    payroll_notes: Optional[str] = None


class PendingApprovalRow(BaseModel):
    request_id: int
    employee_id: int
    employee_name: str
    work_date: date
    actual_worked_minutes: int
    actual_worked_display: str
    requested_minutes: int
    requested_display: str
    reason: str
    submitted_at: datetime
    request_number_this_month: int
    requires_l2: bool
    l1_manager_name: Optional[str] = None  # For L2 view


class PendingApprovalsResponse(BaseModel):
    pending_count: int
    pending_requests: list[PendingApprovalRow]


class RegularizationApprovalResponse(BaseModel):
    request_id: int
    status: str  # approved or rejected
    approved_by_role: str  # l1 or l2
    approved_at: datetime
    final_status: str
    message: str


class CalendarDayView(BaseModel):
    date: date
    punch_in: Optional[str] = None  # "09:15"
    punch_out: Optional[str] = None  # "17:45"
    actual_worked_minutes: int
    actual_worked: str  # "8h 30m"
    shift_start: str  # "09:00"
    shift_end: str  # "18:00"
    shift_minutes: int
    shift_hours: str  # "9h"
    status: str  # "present", "absent", "leave"
    is_late: bool
    late_by_minutes: int
    gap_minutes: Optional[int] = None
    gap_hours: Optional[str] = None
    
    regularization: Optional[dict] = None  # {request_id, status, requested_minutes, l1_status, l2_status}


class AttendanceCalendarResponse(BaseModel):
    month: str  # "2024-12"
    days: list[CalendarDayView]

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, field_validator


class LeaveRequestCreate(BaseModel):
    date_from:  date
    date_to:    date
    leave_type: str   # 'paid' | 'unpaid'
    reason:     str

    @field_validator("leave_type")
    @classmethod
    def valid_type(cls, v: str) -> str:
        if v not in ("paid", "unpaid"):
            raise ValueError("leave_type must be 'paid' or 'unpaid'")
        return v

    @field_validator("reason")
    @classmethod
    def valid_reason(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 5:
            raise ValueError("Reason must be at least 5 characters")
        if len(v) > 500:
            raise ValueError("Reason must be at most 500 characters")
        return v


class LeaveApprovalRequest(BaseModel):
    comment: Optional[str] = None

    @field_validator("comment")
    @classmethod
    def valid_comment(cls, v: Optional[str]) -> Optional[str]:
        if v and len(v) > 500:
            raise ValueError("Comment must be at most 500 characters")
        return v


class LeaveRejectionRequest(BaseModel):
    comment: str

    @field_validator("comment")
    @classmethod
    def valid_comment(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 5:
            raise ValueError("Rejection comment must be at least 5 characters")
        if len(v) > 500:
            raise ValueError("Comment must be at most 500 characters")
        return v


class LeaveRequestRow(BaseModel):
    request_id:       int
    date_from:        date
    date_to:          date
    num_days:         int
    leave_type:       str
    reason:           str
    submitted_at:     datetime
    l1_status:        str
    l1_manager_name:  Optional[str] = None
    l1_comment:       Optional[str] = None
    l1_approved_at:   Optional[datetime] = None
    l2_status:        Optional[str] = None
    l2_manager_name:  Optional[str] = None
    l2_comment:       Optional[str] = None
    l2_approved_at:   Optional[datetime] = None
    final_status:     str
    cancelled_at:     Optional[datetime] = None
    payroll_impact:   str   # 'present' (paid) | 'absent' (unpaid)


class LeaveRequestsListResponse(BaseModel):
    year:                   int
    total:                  int
    approved:               int
    rejected:               int
    pending:                int
    cancelled:              int
    paid_balance_total:     int
    paid_balance_used:      int
    paid_balance_remaining: int
    requests:               list[LeaveRequestRow]


class LeavePendingApprovalRow(BaseModel):
    request_id:      int
    employee_id:     int
    employee_name:   str
    date_from:       date
    date_to:         date
    num_days:        int
    leave_type:      str
    reason:          str
    submitted_at:    datetime
    l1_status:       str
    l1_manager_name: Optional[str] = None
    awaiting_role:   str   # 'l1' | 'l2'


class LeavePendingApprovalsResponse(BaseModel):
    pending_count:    int
    pending_requests: list[LeavePendingApprovalRow]


class LeaveApprovalResponse(BaseModel):
    request_id:     int
    status:         str       # 'approved' | 'rejected'
    approved_by_role: str     # 'l1' | 'l2'
    approved_at:    datetime
    final_status:   str
    message:        str


class LeaveBalanceResponse(BaseModel):
    employee_id:         int
    year:                int
    total_paid_days:     int
    used_paid_days:      int
    remaining_paid_days: int


class HolidayRow(BaseModel):
    id:           int
    holiday_date: date
    name:         str
    holiday_type: str   # 'national' | 'regional' | 'optional'
    is_active:    bool


class HolidayCreate(BaseModel):
    holiday_date: date
    name:         str
    holiday_type: str = "national"

    @field_validator("holiday_type")
    @classmethod
    def valid_type(cls, v: str) -> str:
        if v not in ("national", "regional", "optional"):
            raise ValueError("holiday_type must be national, regional, or optional")
        return v

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("Holiday name must be at least 2 characters")
        return v


class LeaveBalanceAdjust(BaseModel):
    total_paid_days: int
    year:            Optional[int] = None

    @field_validator("total_paid_days")
    @classmethod
    def valid_days(cls, v: int) -> int:
        if v < 0 or v > 365:
            raise ValueError("total_paid_days must be between 0 and 365")
        return v