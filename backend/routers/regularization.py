"""
routers/regularization.py — Attendance Regularization Module (v2)

Handles employee requests for attendance shortfalls:
  - Case 1: Early logout (has both punches)
  - Case 2: Late login (has both punches)
  - Case 3: Forgot to punch in (only punch out exists)
  - Case 4: Forgot to punch out (only punch in exists)

Approval workflow:
  - Requests 1-3/month (APPROVED): L1 manager only
  - Requests 4+/month (APPROVED): Both L1 and L2 (HR) managers

CHANGES IN V2:
- Allow requests even without complete punch records
- Support "forgot punch in/out" scenarios
- Relaxed validation for manual entry cases
- Manager sees request type for better context
"""
import asyncpg
import logging
import pytz
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from auth import get_current_user
from db import get_db
from config import settings

from schemas import (
    RegularizationRequestCreate,
    RegularizationApprovalRequest,
    RegularizationRejectionRequest,
    RegularizationRequestsListResponse,
    RegularizationRequestDetail,
    RegularizationRequestRow,
    PendingApprovalRow,
    PendingApprovalsResponse,
    RegularizationApprovalResponse,
    AttendanceCalendarResponse,
    CalendarDayView,
)

logger = logging.getLogger("regularization")
router = APIRouter(prefix="/api/attendance/regularization", tags=["regularization"])

# Required working hours per day.
# Office window is 09:45–18:30 (shift stored in DB = 525 min) but the
# contractual obligation is 8h 30m. Grace period (30 min) only affects
# the late marker — it does NOT reduce the required hours.
REQUIRED_WORK_MINUTES = 510  # 8h 30m


# ══════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════

def to_local(dt):
    """Convert UTC datetime to local timezone (IST)."""
    if dt is None:
        return None
    tz = pytz.timezone(settings.office_timezone)
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(tz)


def minutes_to_display(minutes: int) -> str:
    """Convert minutes to "Xh Ym" format."""
    if minutes <= 0:
        return "0m"
    h = minutes // 60
    m = minutes % 60
    if h == 0:
        return f"{m}m"
    elif m == 0:
        return f"{h}h"
    else:
        return f"{h}h {m}m"
    
async def _write_audit_log(
    db: asyncpg.Connection,
    *,
    request_id: int,
    action_by_user_id: int,
    action_role: str,          # 'l1' | 'l2' | 'system'
    action_type: str,          # 'submitted' | 'resubmitted_after_rejection' | 'l1_approved' | 'l1_rejected' | 'l2_approved' | 'l2_rejected'
    note: Optional[str],
    minutes_before: int,       # daily_summary.total_minutes BEFORE this action
    minutes_after: int,        # daily_summary.total_minutes AFTER this action
    payroll_status_before: str,
    payroll_status_after: str,
) -> None:
    """
    Write one row to regularization_audit_logs.
    Captures before/after snapshot of daily_summary so HR can see
    exactly what changed and when.

    Called inside the caller's transaction — no separate commit needed.
    """
    await db.execute(
        """
        INSERT INTO regularization_audit_logs
            (request_id, action_by_user_id, action_role, action_type, note,
             minutes_before, payroll_status_before,
             minutes_after,  payroll_status_after,
             created_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
        """,
        request_id,
        action_by_user_id,
        action_role,
        action_type,
        note,
        minutes_before,
        payroll_status_before,
        minutes_after,
        payroll_status_after,
    )


async def _sync_daily_summary(
    db: asyncpg.Connection,
    *,
    request_id: int,
    final_status: str,         # 'approved' | 'rejected'
) -> tuple[int, int, str, str]:
    """
    After a regularization reaches final_status, sync daily_summary so
    total_minutes, payroll_minutes, payroll_status, is_regularized all
    reflect the correct state. This is what the clock page reads.

    Returns (minutes_before, minutes_after, payroll_status_before, payroll_status_after)
    so the caller can pass them straight to _write_audit_log.

    Called inside the caller's transaction — no separate commit needed.
    """
    # Single query — get everything we need in one round-trip
    req = await db.fetchrow(
        """
        SELECT
            r.work_date,
            r.actual_worked_minutes,
            r.requested_minutes,
            e.user_id,
            COALESCE(ds.total_minutes, 0)   AS cur_total,
            COALESCE(ds.payroll_status, 'absent') AS cur_payroll_status
        FROM regularization_requests r
        JOIN employees e ON e.id = r.employee_id
        LEFT JOIN daily_summary ds
               ON ds.user_id = e.user_id AND ds.work_date = r.work_date
        WHERE r.id = $1
        """,
        request_id,
    )

    if not req:
        # Should never happen — request must exist before approval
        return (0, 0, "absent", "absent")

    minutes_before       = req["cur_total"]
    payroll_before       = req["cur_payroll_status"]
    actual               = req["actual_worked_minutes"]
    requested            = req["requested_minutes"]
    user_id              = req["user_id"]
    work_date            = req["work_date"]

    if final_status == "approved":
        # Credited = actual worked + approved gap
        credited         = actual + requested
        payroll_after    = "present"

        # UPSERT — guarantees hours are credited even if daily_summary row is
        # missing (e.g. fully-absent day whose placeholder was never committed).
        await db.execute(
            """
            INSERT INTO daily_summary
                (user_id, work_date, total_minutes, payroll_minutes,
                 payroll_status, regularization_status, regularization_minutes,
                 is_regularized, payroll_notes, status)
            VALUES ($1, $2, $3, $3, 'present', 'approved', $4, TRUE,
                    'Regularization approved — hours credited', 'present')
            ON CONFLICT (user_id, work_date) DO UPDATE
            SET
                total_minutes         = EXCLUDED.total_minutes,
                payroll_minutes       = EXCLUDED.payroll_minutes,
                status                = 'present',
                payroll_status        = 'present',
                regularization_status = 'approved',
                regularization_minutes= EXCLUDED.regularization_minutes,
                is_regularized        = TRUE,
                payroll_notes         = 'Regularization approved — hours credited'
            """,
            user_id, work_date, credited, requested,
        )
        minutes_after = credited

    else:  # rejected
        # Revert to actual worked only — no bonus minutes.
        # CRITICAL: If the day has an approved leave stamped on it (leave_request_id IS NOT NULL),
        # we must NOT overwrite it. Leave always wins over regularization.
        # This handles the edge case where leave was approved AFTER regularization was submitted.
        payroll_after = "present" if actual > 0 else "absent"
        status_after  = "present" if actual > 0 else "absent"

        await db.execute(
            """
            INSERT INTO daily_summary
                (user_id, work_date, total_minutes, payroll_minutes,
                 payroll_status, regularization_status, regularization_minutes,
                 is_regularized, payroll_notes, status)
            VALUES ($1, $2, $3, $3, $4, 'rejected', 0, FALSE,
                    'Regularization rejected — original hours retained', $5)
            ON CONFLICT (user_id, work_date) DO UPDATE
            SET
                total_minutes         = EXCLUDED.total_minutes,
                payroll_minutes       = EXCLUDED.payroll_minutes,
                -- Only revert status/payroll_status if leave hasn't taken ownership.
                -- If leave_request_id is set, leave owns this day — don't touch it.
                status                = CASE WHEN daily_summary.leave_request_id IS NOT NULL
                                             THEN daily_summary.status
                                             ELSE $5 END,
                payroll_status        = CASE WHEN daily_summary.leave_request_id IS NOT NULL
                                             THEN daily_summary.payroll_status
                                             ELSE $4 END,
                regularization_status = 'rejected',
                regularization_minutes= 0,
                is_regularized        = FALSE,
                payroll_notes         = CASE WHEN daily_summary.leave_request_id IS NOT NULL
                                             THEN daily_summary.payroll_notes
                                             ELSE 'Regularization rejected — original hours retained' END
            """,
            user_id, work_date, actual, payroll_after, status_after,
        )
        minutes_after = actual

    return (minutes_before, minutes_after, payroll_before, payroll_after)

async def get_employee_from_user(user_id: int, db: asyncpg.Connection) -> dict:
    """Get employee record from user_id."""
    emp = await db.fetchrow(
        "SELECT id, user_id, l1_manager_id, l2_manager_id, shift_start, shift_end, date_of_joining FROM employees WHERE user_id=$1",
        user_id
    )
    if not emp:
        raise HTTPException(403, "Employee profile not found")
    return emp


async def get_approved_request_count_for_month(
    employee_id: int, year: int, month: int, db: asyncpg.Connection
) -> int:
    """
    Get count of APPROVED regularization requests for a specific month.
    Must use the request's work_date month — not the current calendar month —
    because employees can submit for past months (within 30-day window).
    """
    result = await db.fetchval(
        """
        SELECT COUNT(*) FROM regularization_requests
        WHERE employee_id = $1
          AND EXTRACT(YEAR  FROM work_date) = $2
          AND EXTRACT(MONTH FROM work_date) = $3
          AND final_status = 'approved'
        """,
        employee_id, year, month
    )
    return result or 0


async def get_approved_minutes_for_month(
    employee_id: int, year: int, month: int, db: asyncpg.Connection
) -> int:
    """Get total approved regularization minutes for a specific month."""
    result = await db.fetchval(
        """
        SELECT COALESCE(SUM(requested_minutes), 0)
        FROM regularization_requests
        WHERE employee_id = $1
          AND EXTRACT(YEAR  FROM work_date) = $2
          AND EXTRACT(MONTH FROM work_date) = $3
          AND final_status = 'approved'
        """,
        employee_id, year, month
    )
    return result or 0


def needs_l2_approval(approved_count: int) -> bool:
    """
    Return True if this request requires L2 approval.

    Rule: first 3 approved requests in a month → L1 only.
          4th approved request onwards           → L1 + L2.

    approved_count = number of ALREADY-approved requests BEFORE this one.
    So count >= 3 means this will be the 4th (or later) → needs L2.

    Called ONLY inside the L1-approval transaction (with a row lock held),
    so the count is always authoritative — no race condition possible.
    """
    return approved_count >= 3


async def validate_manager_active(manager_id: int, db: asyncpg.Connection) -> bool:
    """Check if manager is an active employee."""
    result = await db.fetchval(
        """
        SELECT EXISTS(
            SELECT 1 FROM employees e
            JOIN users u ON u.id = e.user_id
            WHERE e.id = $1 AND e.is_active = TRUE AND u.is_active = TRUE
        )
        """,
        manager_id
    )
    return result or False


def determine_request_type(punch_in: bool, punch_out: bool) -> str:
    """
    Determine regularization request type based on punch status.
    
    Returns:
    - "early_logout" or "late_login": Both punches exist
    - "forgot_punch_in": Only punch out exists
    - "forgot_punch_out": Only punch in exists
    - "both_missing": Neither punch exists (edge case)
    """
    if punch_in and punch_out:
        return "partial_hours"  # Early/Late
    elif not punch_in and punch_out:
        return "forgot_punch_in"
    elif punch_in and not punch_out:
        return "forgot_punch_out"
    else:
        return "both_missing"



# ══════════════════════════════════════════════════════════════
# ENDPOINTS: EMPLOYEE
# ══════════════════════════════════════════════════════════════

@router.post("/request")
async def create_regularization_request(
    req: RegularizationRequestCreate,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    POST /api/attendance/regularization/request
    
    Employee raises regularization request for a specific day.
    
    Supports 4 cases:
    1. Early logout / Late login (both punches exist)
    2. Forgot to punch in (only punch out)
    3. Forgot to punch out (only punch in)
    4. Both missing (rare, but allowed)
    """
    # Get employee record
    emp = await get_employee_from_user(user["id"], db)
    employee_id = emp["id"]
    
    # Validate work_date — today is blocked (day not complete, punch-out may still happen)
    if req.work_date >= date.today():
        raise HTTPException(400, "Cannot request regularization for today or future dates")
    
    if (date.today() - req.work_date).days > 30:
        raise HTTPException(400, "Can only request regularization within 30 days")
    
    # Check if already requested for this date
    existing = await db.fetchrow(
        "SELECT id, final_status FROM regularization_requests WHERE employee_id = $1 AND work_date = $2",
        employee_id, req.work_date
    )
    if existing:
        if existing["final_status"] in ("pending", "approved"):
            raise HTTPException(400, f"Already have a {existing['final_status']} request for {req.work_date}")
        # Allow resubmission if previously rejected

    # Block regularization on days with approved leave.
    # Leave always wins — a day marked as leave cannot be regularized.
    # (The employee shouldn't have been at work that day at all.)
    leave_conflict = await db.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM daily_summary
            WHERE user_id  = $1
              AND work_date = $2
              AND status    = 'leave'
              AND leave_request_id IS NOT NULL
        )
        """,
        user["id"], req.work_date,
    )
    if leave_conflict:
        raise HTTPException(
            400,
            f"Cannot raise a regularization request for {req.work_date} — "
            "that day has an approved leave. Cancel the leave first if needed."
        )
    
    # ✅ RELAXED VALIDATION: Allow negative actual_worked_minutes for forgot cases
    if req.actual_worked_minutes < 0:
        raise HTTPException(400, "Invalid worked minutes (cannot be negative)")
    
    if req.requested_minutes <= 0:
        raise HTTPException(400, "Requested minutes must be greater than 0")
    
    # Validate: total claimed hours cannot exceed required hours + 2h buffer.
    # Max buffer covers "forgot punch-out" cases where punch-in was early.
    # We use REQUIRED_WORK_MINUTES (510) not the shift window (525) —
    # the 15-min difference is intentional (grace window absorbed by shift start).
    max_allowed = REQUIRED_WORK_MINUTES + 120  # 510 + 2h buffer
    total_claimed = req.actual_worked_minutes + req.requested_minutes
    
    if total_claimed > max_allowed:
        raise HTTPException(
            400,
            f"Total hours ({minutes_to_display(total_claimed)}) exceeds maximum allowed "
            f"({minutes_to_display(max_allowed)})"
        )
    
    # Check L1 manager assigned
    if not emp["l1_manager_id"]:
        raise HTTPException(403, "No L1 manager assigned to you. Contact HR.")

    # Validate L1 manager is active
    if not await validate_manager_active(emp["l1_manager_id"], db):
        raise HTTPException(403, "Your L1 manager is inactive. Contact HR.")

    # Always store l2_manager_id from profile (may be None — that's fine).
    # Whether L2 approval is actually NEEDED is decided at L1-approval time,
    # inside a locked transaction, so the count is always authoritative.
    # We do NOT decide the tier here — avoids the race where other requests
    # get approved between submission and L1 action, causing a tier mismatch.
    l2_manager_id = emp["l2_manager_id"]

    # ── Single transaction: ensure daily_summary exists, create request, link it ──
    async with db.transaction():

        # Ensure daily_summary row exists for fully-absent days (no punch record).
        await db.execute(
            """
            INSERT INTO daily_summary (user_id, work_date, total_minutes, status)
            VALUES ($1, $2, 0, 'absent')
            ON CONFLICT (user_id, work_date) DO NOTHING
            """,
            user["id"], req.work_date,
        )

        # Resubmission of a previously-rejected request: UPDATE the row in place.
        # Keeps 1 row per (employee, work_date); daily_summary FK stays clean.
        if existing and existing["final_status"] == "rejected":
            # Read the actual current daily_summary state BEFORE rewriting anything.
            # The day may have punch data (total_minutes > 0) even though the
            # request was rejected — hardcoding 0 would produce a false audit snapshot.
            ds_snap = await db.fetchrow(
                """
                SELECT COALESCE(total_minutes, 0) AS total_minutes,
                       COALESCE(payroll_status, 'absent') AS payroll_status
                FROM daily_summary
                WHERE user_id = $1 AND work_date = $2
                """,
                user["id"], req.work_date,
            )
            snap_minutes = ds_snap["total_minutes"] if ds_snap else 0
            snap_status  = ds_snap["payroll_status"] if ds_snap else "absent"

            # Write audit entry BEFORE the UPDATE so the previous rejection state
            # is still readable when this log entry is created.
            await _write_audit_log(
                db,
                request_id            = existing["id"],
                action_by_user_id     = user["id"],
                action_role           = "employee",
                action_type           = "resubmitted_after_rejection",
                note                  = f"Resubmitted after rejection. "
                                        f"New minutes: actual={req.actual_worked_minutes}, "
                                        f"requested={req.requested_minutes}",
                minutes_before        = snap_minutes,
                minutes_after         = snap_minutes,   # resubmission itself changes nothing yet
                payroll_status_before = snap_status,
                payroll_status_after  = snap_status,    # status unchanged until manager acts
            )
            request_id = await db.fetchval(
                """
                UPDATE regularization_requests
                SET actual_worked_minutes  = $3,
                    requested_minutes      = $4,
                    reason                 = $5,
                    submitted_by_user_id   = $6,
                    l1_manager_id          = $7,
                    l1_status              = 'pending',
                    l1_approved_at         = NULL,
                    l1_approved_by_user_id = NULL,
                    l1_comment             = NULL,
                    l2_manager_id          = $8,
                    l2_status              = NULL,
                    l2_approved_at         = NULL,
                    l2_approved_by_user_id = NULL,
                    l2_comment             = NULL,
                    escalation_required    = FALSE,
                    final_status           = 'pending',
                    submitted_at           = NOW(),
                    updated_at             = NOW()
                WHERE employee_id = $1 AND work_date = $2
                RETURNING id
                """,
                employee_id,
                req.work_date,
                req.actual_worked_minutes,
                req.requested_minutes,
                req.reason,
                user["id"],
                emp["l1_manager_id"],
                l2_manager_id,
            )
        else:
            # Fresh request — l2_status starts NULL (tier decided at L1 approval).
            request_id = await db.fetchval(
                """
                INSERT INTO regularization_requests (
                    employee_id, work_date, actual_worked_minutes, requested_minutes, reason,
                    submitted_by_user_id, l1_manager_id, l1_status,
                    l2_manager_id, l2_status, escalation_required, final_status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, NULL, FALSE, 'pending')
                RETURNING id
                """,
                employee_id,
                req.work_date,
                req.actual_worked_minutes,
                req.requested_minutes,
                req.reason,
                user["id"],
                emp["l1_manager_id"],
                l2_manager_id,
            )

        # Link request to daily_summary.
        await db.execute(
            """
            UPDATE daily_summary
            SET regularization_request_id=$1, regularization_status='pending'
            WHERE user_id=$2 AND work_date=$3
            """,
            request_id, user["id"], req.work_date,
        )

        logger.info(
            f"Regularization request created: id={request_id}, emp_id={employee_id}, "
            f"date={req.work_date}, actual={req.actual_worked_minutes}, "
            f"requested={req.requested_minutes}"
        )

    return {
        "request_id": request_id,
        "employee_id": employee_id,
        "work_date": req.work_date,
        "actual_worked_minutes": req.actual_worked_minutes,
        "requested_minutes": req.requested_minutes,
        "reason": req.reason,
        "submitted_at": datetime.now().isoformat(),
        "l1_manager_id": emp["l1_manager_id"],
        "l1_status": "pending",
        "l2_status": None,          # tier not known yet — set at L1 approval
        "requires_l2": None,        # unknown until L1 approves
        "final_status": "pending",
        "message": "Request submitted to L1 manager for approval.",
    }

@router.get("/requests")
async def list_regularization_requests(
    month: Optional[str] = Query(None),
    status: str = Query("all"),
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> RegularizationRequestsListResponse:
    emp = await get_employee_from_user(user["id"], db)
    employee_id = emp["id"]
    
    if not month:
        now = datetime.now()
        month = f"{now.year}-{now.month:02d}"
    
    year, month_num = int(month.split("-")[0]), int(month.split("-")[1])
    
    # Build fully-parameterized query — year/month as $N params, never interpolated.
    params: list = [employee_id]

    if status != "all":
        status_clause = f"AND r.final_status = ${len(params) + 1}"
        params.append(status)
    else:
        status_clause = ""

    params.extend([year, month_num])
    year_param  = len(params) - 1
    month_param = len(params)

    query = f"""
        SELECT
            r.id, r.work_date, r.actual_worked_minutes, r.requested_minutes, r.reason,
            r.submitted_at, r.l1_status, r.l2_status, r.final_status,
            r.l1_manager_id, r.l2_manager_id,
            l1_mgr.full_name AS l1_manager_name,
            l2_mgr.full_name AS l2_manager_name
        FROM regularization_requests r
        LEFT JOIN employees l1_emp ON l1_emp.id = r.l1_manager_id
        LEFT JOIN users     l1_mgr ON l1_mgr.id = l1_emp.user_id
        LEFT JOIN employees l2_emp ON l2_emp.id = r.l2_manager_id
        LEFT JOIN users     l2_mgr ON l2_mgr.id = l2_emp.user_id
        WHERE r.employee_id = $1
          {{status_clause}}
          AND EXTRACT(YEAR  FROM r.work_date) = ${year_param}
          AND EXTRACT(MONTH FROM r.work_date) = ${month_param}
        ORDER BY r.work_date DESC
    """.format(status_clause=status_clause)

    rows = await db.fetch(query, *params)
    
    # ✅ No more loop queries for manager names!
    approved_count = sum(1 for r in rows if r["final_status"] == "approved")
    rejected_count = sum(1 for r in rows if r["final_status"] == "rejected")
    pending_count = sum(1 for r in rows if r["final_status"] == "pending")
    
    approved_minutes = await get_approved_minutes_for_month(employee_id, year, month_num, db)
    
    requests_list = [
        RegularizationRequestRow(
            request_id=r["id"],
            work_date=r["work_date"],
            actual_worked_minutes=r["actual_worked_minutes"],
            actual_worked=minutes_to_display(r["actual_worked_minutes"]),
            requested_minutes=r["requested_minutes"],
            requested_display=minutes_to_display(r["requested_minutes"]),
            reason=r["reason"],
            submitted_at=r["submitted_at"],
            l1_status=r["l1_status"],
            l1_manager_name=r["l1_manager_name"],  # ✅ Already fetched
            l1_approved_at=None,
            l2_status=r["l2_status"],
            l2_manager_name=r["l2_manager_name"],  # ✅ Already fetched
            l2_approved_at=None,
            final_status=r["final_status"],
            payroll_impact=(
                "present" if r["final_status"] == "approved"
                else "pending" if r["final_status"] == "pending"
                else "absent"
            ),
        )
        for r in rows
    ]
    
    return RegularizationRequestsListResponse(
        month=month,
        total=len(rows),
        approved=approved_count,
        rejected=rejected_count,
        pending=pending_count,
        monthly_limit_hours=20,
        approved_hours_this_month=approved_minutes // 60,
        approved_minutes_this_month=approved_minutes,
        requests=requests_list,
    )


@router.get("/requests/{request_id}")
async def get_regularization_detail(
    request_id: int,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> RegularizationRequestDetail:
    """
    GET /api/attendance/regularization/requests/{request_id}
    
    Get detailed information about a regularization request.
    """
    # Single query — fetch request + employee shift + both manager names together.
    # Eliminates the original N+1 (2 extra round-trips for L1/L2 names).
    req = await db.fetchrow(
        """
        SELECT
            r.*,
            e.shift_start, e.shift_end,
            e.id            AS emp_row_id,
            u.full_name     AS employee_name,
            l1_u.full_name  AS l1_manager_name,
            l2_u.full_name  AS l2_manager_name,
            ds.payroll_status AS ds_payroll_status,
            ds.payroll_notes  AS ds_payroll_notes
        FROM regularization_requests r
        JOIN  employees e    ON e.id    = r.employee_id
        JOIN  users     u    ON u.id    = e.user_id
        LEFT JOIN employees l1_e ON l1_e.id = r.l1_manager_id
        LEFT JOIN users     l1_u ON l1_u.id = l1_e.user_id
        LEFT JOIN employees l2_e ON l2_e.id = r.l2_manager_id
        LEFT JOIN users     l2_u ON l2_u.id = l2_e.user_id
        LEFT JOIN daily_summary ds ON ds.user_id = e.user_id AND ds.work_date = r.work_date
        WHERE r.id = $1
        """,
        request_id,
    )

    if not req:
        raise HTTPException(404, "Request not found")

    # Access control — owner, assigned manager, or HR/admin
    emp = await get_employee_from_user(user["id"], db)
    is_owner            = emp["id"] == req["employee_id"]
    is_assigned_manager = emp["id"] in (req["l1_manager_id"], req["l2_manager_id"])
    is_hr               = user["role"] in ("hr", "admin")

    if not (is_owner or is_assigned_manager or is_hr):
        raise HTTPException(403, "Access denied")

    # shift_minutes = raw window (525) — informational display only.
    # gap = shortfall against REQUIRED_WORK_MINUTES (510) — must match calendar view.
    shift_minutes = (req["shift_end"].hour * 60 + req["shift_end"].minute) - \
                    (req["shift_start"].hour * 60 + req["shift_start"].minute)

    # Clamp to 0 — employee may have worked more than required (e.g. overtime)
    gap_minutes = max(0, REQUIRED_WORK_MINUTES - req["actual_worked_minutes"])

    l1_name = req["l1_manager_name"]
    l2_name = req["l2_manager_name"]
    
    return RegularizationRequestDetail(
        request_id=req["id"],
        employee_id=req["employee_id"],
        employee_name=req["employee_name"],
        work_date=req["work_date"],
        actual_worked_minutes=req["actual_worked_minutes"],
        actual_worked_display=minutes_to_display(req["actual_worked_minutes"]),
        shift_minutes=REQUIRED_WORK_MINUTES,
        shift_display=minutes_to_display(REQUIRED_WORK_MINUTES),
        gap_minutes=gap_minutes,
        gap_display=minutes_to_display(gap_minutes),
        requested_minutes=req["requested_minutes"],
        requested_display=minutes_to_display(req["requested_minutes"]),
        reason=req["reason"],
        submitted_at=req["submitted_at"],
        l1_manager_id=req["l1_manager_id"],
        l1_manager_name=l1_name,
        l1_status=req["l1_status"],
        l1_comment=req["l1_comment"],
        l1_approved_at=req["l1_approved_at"],
        l2_manager_id=req["l2_manager_id"],
        l2_manager_name=l2_name,
        l2_status=req["l2_status"],
        l2_comment=req["l2_comment"],
        l2_approved_at=req["l2_approved_at"],
        requires_l2_approval=req["escalation_required"] or False,
        final_status=req["final_status"],
        is_regularized=req["final_status"] == "approved",
        payroll_status=req["ds_payroll_status"] or "absent",
        payroll_notes=req["ds_payroll_notes"] or f"{'Regularized' if req['final_status'] == 'approved' else 'Not regularized'} {minutes_to_display(req['requested_minutes'])}",
    )


# ══════════════════════════════════════════════════════════════
# ENDPOINTS: MANAGER APPROVALS
# ══════════════════════════════════════════════════════════════

@router.get("/pending")
async def get_pending_approvals(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> PendingApprovalsResponse:
    # L1 and L2 are both plain employees — role is irrelevant here.
    # Show requests where this employee is assigned as L1 (l1 pending)
    # OR assigned as L2 (l1 already approved, l2 still pending).
    emp = await get_employee_from_user(user["id"], db)
    manager_id = emp["id"]

    rows = await db.fetch(
        """
        SELECT
            r.id as request_id, r.employee_id, r.work_date,
            r.actual_worked_minutes, r.requested_minutes, r.reason,
            r.submitted_at, r.l1_manager_id, r.l2_manager_id,
            r.l1_status, r.l2_status,
            u.full_name as employee_name,
            l1_mgr.full_name as l1_manager_name,
            (
                SELECT COUNT(*)
                FROM regularization_requests r2
                WHERE r2.employee_id = r.employee_id
                  AND EXTRACT(YEAR  FROM r2.work_date) = EXTRACT(YEAR  FROM r.work_date)
                  AND EXTRACT(MONTH FROM r2.work_date) = EXTRACT(MONTH FROM r.work_date)
                  AND r2.final_status = 'approved'
            ) as approved_count_before
        FROM regularization_requests r
        JOIN employees e ON e.id = r.employee_id
        JOIN users u ON u.id = e.user_id
        LEFT JOIN employees l1_emp ON l1_emp.id = r.l1_manager_id
        LEFT JOIN users l1_mgr ON l1_mgr.id = l1_emp.user_id
        WHERE r.final_status = 'pending'
          AND (
              (r.l1_manager_id = $1 AND r.l1_status = 'pending')
              OR
              (r.l2_manager_id = $1 AND r.l2_status = 'pending' AND r.l1_status = 'approved')
          )
        ORDER BY r.submitted_at ASC
        """,
        manager_id,
    )
    
    # ✅ No more loop queries!
    pending_list = []
    for row in rows:
        approved_count = row["approved_count_before"]  # ✅ Already calculated
        # requires_l2 is unknown at submission time; l2_status is NULL until L1 approves.
        # For L1-pending rows: show a preview based on count (informational only).
        # For L2-pending rows: escalation_required is already set TRUE on the row.
        requires_l2 = row["l2_status"] == "pending" or approved_count >= 3
        
        pending_list.append(
            PendingApprovalRow(
                request_id=row["request_id"],
                employee_id=row["employee_id"],
                employee_name=row["employee_name"],
                work_date=row["work_date"],
                actual_worked_minutes=row["actual_worked_minutes"],
                actual_worked_display=minutes_to_display(row["actual_worked_minutes"]),
                requested_minutes=row["requested_minutes"],
                requested_display=minutes_to_display(row["requested_minutes"]),
                reason=row["reason"],
                submitted_at=row["submitted_at"],
                request_number_this_month=approved_count + 1,
                requires_l2=requires_l2,
                l1_manager_name=row.get("l1_manager_name"),
            )
        )
    
    return PendingApprovalsResponse(
        pending_count=len(pending_list),
        pending_requests=pending_list,
    )


@router.get("/team-history")
async def get_team_regularization_history(
    month: Optional[str] = Query(None),
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    GET /api/attendance/regularization/team-history?month=2026-03

    Returns regularization requests this manager has already actioned
    (final_status = approved or rejected). Works for both L1 and L2 roles.
    """
    emp = await get_employee_from_user(user["id"], db)
    manager_id = emp["id"]

    if not month:
        now = datetime.now()
        month = f"{now.year}-{now.month:02d}"
    year, month_num = int(month.split("-")[0]), int(month.split("-")[1])

    rows = await db.fetch(
        """
        SELECT
            r.id                    AS request_id,
            r.employee_id,
            r.work_date,
            r.actual_worked_minutes,
            r.requested_minutes,
            r.reason,
            r.submitted_at,
            r.final_status,
            r.l1_status,
            r.l1_comment,
            r.l1_approved_at,
            r.l2_status,
            r.l2_comment,
            r.l2_approved_at,
            r.escalation_required,
            u.full_name             AS employee_name,
            CASE
                WHEN r.l1_manager_id = $1 AND r.l2_manager_id = $1 THEN 'both'
                WHEN r.l1_manager_id = $1 THEN 'l1'
                ELSE 'l2'
            END                     AS my_role,
            CASE
                WHEN r.l1_manager_id = $1 THEN r.l1_status
                ELSE r.l2_status
            END                     AS my_action
        FROM regularization_requests r
        JOIN employees e ON e.id = r.employee_id
        JOIN users u     ON u.id = e.user_id
        WHERE r.final_status IN ('approved', 'rejected')
          AND (r.l1_manager_id = $1 OR r.l2_manager_id = $1)
          AND EXTRACT(YEAR  FROM r.work_date) = $2
          AND EXTRACT(MONTH FROM r.work_date) = $3
        ORDER BY r.work_date DESC
        """,
        manager_id, year, month_num,
    )

    return {
        "month": month,
        "total": len(rows),
        "requests": [
            {
                "request_id":           r["request_id"],
                "employee_name":        r["employee_name"],
                "work_date":            r["work_date"].isoformat(),
                "actual_worked_minutes": r["actual_worked_minutes"],
                "actual_worked_display": minutes_to_display(r["actual_worked_minutes"]),
                "requested_minutes":    r["requested_minutes"],
                "requested_display":    minutes_to_display(r["requested_minutes"]),
                "reason":               r["reason"],
                "submitted_at":         r["submitted_at"].isoformat() if r["submitted_at"] else None,
                "final_status":         r["final_status"],
                "my_role":              r["my_role"],
                "my_action":            r["my_action"],
                "l1_status":            r["l1_status"],
                "l1_comment":           r["l1_comment"],
                "l1_approved_at":       r["l1_approved_at"].isoformat() if r["l1_approved_at"] else None,
                "l2_status":            r["l2_status"],
                "l2_comment":           r["l2_comment"],
                "l2_approved_at":       r["l2_approved_at"].isoformat() if r["l2_approved_at"] else None,
                "escalation_required":  r["escalation_required"],
            }
            for r in rows
        ],
    }


@router.post("/requests/{request_id}/approve")
async def approve_regularization(
    request_id: int,
    body: RegularizationApprovalRequest = RegularizationApprovalRequest(),
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    L1 or L2 manager approves a regularization request.

    Tier decision flow (single authoritative point):
      - L1 approves → locked count read here determines tier:
          count < 3  → final_status = 'approved'  (L1-only path)
          count >= 3 → final_status = 'pending', l2_status = 'pending'  (escalated to L2)
      - L2 approves (only after l2_status = 'pending') → final_status = 'approved'
      - On final approval: sync daily_summary + write audit log
    """
    comment = (body.comment or "").strip() or None

    # ── 1. Load request + manager assignments in one query ────
    req = await db.fetchrow(
        """
        SELECT
            r.id, r.employee_id, r.work_date,
            r.actual_worked_minutes, r.requested_minutes,
            r.l1_manager_id, r.l2_manager_id,
            r.l1_status, r.l2_status, r.final_status,
            l1e.user_id AS l1_user_id,
            l2e.user_id AS l2_user_id
        FROM regularization_requests r
        LEFT JOIN employees l1e ON l1e.id = r.l1_manager_id
        LEFT JOIN employees l2e ON l2e.id = r.l2_manager_id
        WHERE r.id = $1
        """,
        request_id,
    )

    if not req:
        raise HTTPException(404, "Request not found")
    if req["final_status"] != "pending":
        raise HTTPException(409, f"Request already {req['final_status']}")

    current_user_id = user["id"]
    is_l1 = req["l1_user_id"] == current_user_id
    is_l2 = req["l2_user_id"] == current_user_id

    if not is_l1 and not is_l2:
        raise HTTPException(403, "Not authorised to approve this request")

    # Guard: same person cannot be both L1 and L2 — would allow self-approval
    if is_l1 and is_l2:
        raise HTTPException(400, "You are assigned as both L1 and L2 for this request. Contact HR.")

    # ── 2. All writes in one transaction ──────────────────────
    async with db.transaction():

        if is_l1 and req["l1_status"] == "pending":
            # ── Authoritative tier decision ────────────────────
            # Lock ALL regularization rows for this employee+month BEFORE
            # reading the count. This prevents two concurrent L1 approvals
            # from both seeing count=2 and both skipping L2 escalation.
            await db.execute(
                """
                SELECT id FROM regularization_requests
                WHERE employee_id = $1
                  AND EXTRACT(YEAR  FROM work_date) = $2
                  AND EXTRACT(MONTH FROM work_date) = $3
                FOR UPDATE
                """,
                req["employee_id"], req["work_date"].year, req["work_date"].month,
            )

            # Count already-approved requests for this employee in this month.
            # This is the ONLY place where tier is decided — never at submission.
            approved_count = await get_approved_request_count_for_month(
                req["employee_id"], req["work_date"].year, req["work_date"].month, db
            )

            # Rule: first 3 approved → L1 only.  4th onwards → L1 + L2.
            # approved_count >= 3 means this will become the 4th (or later) approved.
            l2_needed = needs_l2_approval(approved_count)

            # L2 required but employee has no L2 manager assigned → block here.
            # We couldn't check this at submission because tier wasn't known then.
            if l2_needed and not req["l2_manager_id"]:
                raise HTTPException(
                    400,
                    f"This is request #{approved_count + 1} for the month — L2 approval is required "
                    f"but no L2 manager is assigned to this employee. Contact HR to assign one before approving."
                )

            new_final   = "pending" if l2_needed else "approved"
            action_type = "l1_approved"

            await db.execute(
                """
                UPDATE regularization_requests
                SET l1_status              = 'approved',
                    l1_approved_at         = NOW(),
                    l1_approved_by_user_id = $2,
                    l1_comment             = $3,
                    final_status           = $4,
                    escalation_required    = $5,
                    -- Activate L2 only when escalation is needed
                    l2_status              = CASE WHEN $5 THEN 'pending' ELSE NULL END,
                    updated_at             = NOW()
                WHERE id = $1
                """,
                request_id, current_user_id, comment, new_final, l2_needed,
            )

        elif is_l2 and req["l2_status"] == "pending":
            # Guard: L1 must have approved first
            if req["l1_status"] != "approved":
                raise HTTPException(400, "L1 manager must approve before you can act")

            # L2 approval always finalises
            new_final   = "approved"
            action_type = "l2_approved"

            await db.execute(
                """
                UPDATE regularization_requests
                SET l2_status             = 'approved',
                    l2_approved_at        = NOW(),
                    l2_approved_by_user_id= $2,
                    l2_comment            = $3,
                    final_status          = 'approved',
                    updated_at            = NOW()
                WHERE id = $1
                """,
                request_id, current_user_id, comment,
            )

        else:
            raise HTTPException(409, "Already actioned at your level")

        # ── 3. If fully approved → sync daily_summary ─────────
        mins_before = mins_after = 0
        p_before = p_after = "absent"

        if new_final == "approved":
            mins_before, mins_after, p_before, p_after = await _sync_daily_summary(
                db, request_id=request_id, final_status="approved"
            )

        # ── 4. Write audit log (always, even for partial L1 approve) ──
        await _write_audit_log(
            db,
            request_id         = request_id,
            action_by_user_id  = current_user_id,
            action_role        = "l1" if is_l1 else "l2",
            action_type        = action_type,
            note               = comment,
            minutes_before     = mins_before,
            minutes_after      = mins_after,
            payroll_status_before = p_before,
            payroll_status_after  = p_after,
        )

    return {
        "request_id"     : request_id,
        "actioned_by"    : "l1" if is_l1 else "l2",
        "final_status"   : new_final,
        "minutes_credited": mins_after if new_final == "approved" else None,
        "message"        : (
            "Approved. Awaiting L2 approval." if new_final == "pending"
            else "Fully approved. Hours credited to employee."
        ),
    }

@router.post("/requests/{request_id}/reject")
async def reject_regularization(
    request_id: int,
    body: RegularizationRejectionRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    L1 or L2 manager rejects a regularization request.
    Either manager rejecting immediately sets final_status = rejected.
    daily_summary is reverted to actual worked hours.
    """
    comment = (body.comment or "").strip()
    if len(comment) < 5:
        raise HTTPException(400, "Rejection reason must be at least 5 characters")

    # ── 1. Load request ───────────────────────────────────────
    req = await db.fetchrow(
        """
        SELECT
            r.id, r.final_status,
            r.l1_status, r.l2_status,
            l1e.user_id AS l1_user_id,
            l2e.user_id AS l2_user_id
        FROM regularization_requests r
        LEFT JOIN employees l1e ON l1e.id = r.l1_manager_id
        LEFT JOIN employees l2e ON l2e.id = r.l2_manager_id
        WHERE r.id = $1
        """,
        request_id,
    )

    if not req:
        raise HTTPException(404, "Request not found")
    if req["final_status"] != "pending":
        raise HTTPException(409, f"Request already {req['final_status']}")

    current_user_id = user["id"]
    is_l1 = req["l1_user_id"] == current_user_id
    is_l2 = req["l2_user_id"] == current_user_id

    if not is_l1 and not is_l2:
        raise HTTPException(403, "Not authorised to reject this request")

    # Guard: same person cannot be both L1 and L2
    if is_l1 and is_l2:
        raise HTTPException(400, "You are assigned as both L1 and L2 for this request. Contact HR.")

    action_type = "l1_rejected" if is_l1 else "l2_rejected"

    # ── 2. All writes in one transaction ──────────────────────
    async with db.transaction():

        if is_l1:
            await db.execute(
                """
                UPDATE regularization_requests
                SET l1_status             = 'rejected',
                    l1_approved_at        = NOW(),
                    l1_approved_by_user_id= $2,
                    l1_comment            = $3,
                    final_status          = 'rejected',
                    updated_at            = NOW()
                WHERE id = $1
                """,
                request_id, current_user_id, comment,
            )
        else:  # l2
            await db.execute(
                """
                UPDATE regularization_requests
                SET l2_status             = 'rejected',
                    l2_approved_at        = NOW(),
                    l2_approved_by_user_id= $2,
                    l2_comment            = $3,
                    final_status          = 'rejected',
                    updated_at            = NOW()
                WHERE id = $1
                """,
                request_id, current_user_id, comment,
            )

        # ── 3. Revert daily_summary ───────────────────────────
        mins_before, mins_after, p_before, p_after = await _sync_daily_summary(
            db, request_id=request_id, final_status="rejected"
        )

        # ── 4. Write audit log ────────────────────────────────
        await _write_audit_log(
            db,
            request_id            = request_id,
            action_by_user_id     = current_user_id,
            action_role           = "l1" if is_l1 else "l2",
            action_type           = action_type,
            note                  = comment,
            minutes_before        = mins_before,
            minutes_after         = mins_after,
            payroll_status_before = p_before,
            payroll_status_after  = p_after,
        )

    return {
        "request_id"  : request_id,
        "actioned_by" : "l1" if is_l1 else "l2",
        "final_status": "rejected",
        "message"     : "Request rejected. Employee notified.",
    }



# ══════════════════════════════════════════════════════════════
# ENDPOINTS: CALENDAR VIEW
# ══════════════════════════════════════════════════════════════

@router.get("/calendar")
async def get_attendance_calendar(
    month: str = Query(...),  # "2024-12"
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> AttendanceCalendarResponse:
    """
    GET /api/attendance/regularization/calendar?month=2024-12
    
    Get calendar view of attendance with regularization status.
    """
    # Parse month
    try:
        year, month_num = int(month.split("-")[0]), int(month.split("-")[1])
    except (ValueError, IndexError):
        raise HTTPException(400, "Invalid month format. Use YYYY-MM")
    
    # Get employee shift times (shift window used for display only)
    emp = await get_employee_from_user(user["id"], db)
    # Gap/shortfall is measured against REQUIRED_WORK_MINUTES (510 = 8h 30m),
    # not the stored shift window (525 = 8h 45m).
    
    # Cap the series end at today — don't show future dates in the calendar.
    # For past months the cap is the last day of that month naturally.
    today = date.today()

    # Generate every calendar day in the month up to today, then LEFT JOIN
    # daily_summary so fully-absent days (no punch row) still appear.
    # This lets employees raise a regularization request for any past working day,
    # not just days where attendance was already recorded.
    summaries = await db.fetch(
        """
        SELECT
            d.work_date,
            COALESCE(ds.first_punch_in,    NULL)    AS first_punch_in,
            COALESCE(ds.last_punch_out,    NULL)    AS last_punch_out,
            COALESCE(ds.total_minutes,     0)       AS total_minutes,
            COALESCE(ds.status,            'absent') AS status,
            COALESCE(ds.is_late,           FALSE)   AS is_late,
            COALESCE(ds.late_by_minutes,   0)       AS late_by_minutes,
            ds.regularization_request_id,
            r.id            AS reg_req_id,
            r.requested_minutes,
            r.l1_status,
            r.l2_status,
            r.final_status  AS reg_final_status
        FROM generate_series(
            DATE_TRUNC('month', MAKE_DATE($2, $3, 1))::date,
            LEAST(
                (DATE_TRUNC('month', MAKE_DATE($2, $3, 1)) + INTERVAL '1 month - 1 day')::date,
                $4::date
            ),
            '1 day'::interval
        ) AS d(work_date)
        LEFT JOIN daily_summary ds
               ON ds.user_id = $1 AND ds.work_date = d.work_date
        LEFT JOIN regularization_requests r
               ON r.id = ds.regularization_request_id
        ORDER BY d.work_date
        """,
        user["id"], year, month_num, today,
    )

    days = []
    for s in summaries:
        total_mins = s["total_minutes"]
        gap_minutes = None
        gap_hours = None

        # Don't show a gap if employee already has a pending or approved request —
        # gap implies "action needed", which is false if request is in flight or done.
        reg_status = s["reg_final_status"]
        show_gap = reg_status not in ("pending", "approved")
        if total_mins < REQUIRED_WORK_MINUTES and show_gap:
            gap_minutes = REQUIRED_WORK_MINUTES - total_mins
            gap_hours = minutes_to_display(gap_minutes)

        reg_data = None
        if s["reg_req_id"]:
            reg_data = {
                "request_id": s["reg_req_id"],
                "status": s["reg_final_status"],
                "requested_minutes": s["requested_minutes"],
                "l1_status": s["l1_status"],
                "l2_status": s["l2_status"],
            }

        # Convert UTC punch timestamps to local timezone for display
        punch_in_str = None
        punch_out_str = None
        if s["first_punch_in"]:
            punch_in_str = to_local(s["first_punch_in"]).strftime("%H:%M")
        if s["last_punch_out"]:
            punch_out_str = to_local(s["last_punch_out"]).strftime("%H:%M")

        days.append(
            CalendarDayView(
                date=s["work_date"],
                punch_in=punch_in_str,
                punch_out=punch_out_str,
                actual_worked_minutes=total_mins,
                actual_worked=minutes_to_display(total_mins),
                shift_start=emp["shift_start"].strftime("%H:%M"),
                shift_end=emp["shift_end"].strftime("%H:%M"),
                shift_minutes=REQUIRED_WORK_MINUTES,
                shift_hours=minutes_to_display(REQUIRED_WORK_MINUTES),
                status=s["status"],
                is_late=s["is_late"],
                late_by_minutes=s["late_by_minutes"],
                gap_minutes=gap_minutes,
                gap_hours=gap_hours,
                regularization=reg_data,
            )
        )

    return AttendanceCalendarResponse(
        month=month,
        days=days,
        joining_date=emp["date_of_joining"],  # May be None if not set — frontend handles gracefully
    )