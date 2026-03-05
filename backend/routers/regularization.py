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


async def get_employee_from_user(user_id: int, db: asyncpg.Connection) -> dict:
    """Get employee record from user_id."""
    emp = await db.fetchrow(
        "SELECT id, user_id, l1_manager_id, l2_manager_id, shift_start, shift_end FROM employees WHERE user_id=$1",
        user_id
    )
    if not emp:
        raise HTTPException(403, "Employee profile not found")
    return emp


async def get_approved_request_count_this_month(employee_id: int, db: asyncpg.Connection) -> int:
    """
    Get count of APPROVED regularization requests in current month.
    This determines if request needs L2 approval (>3 approved requests).
    """
    result = await db.fetchval(
        """
        SELECT COUNT(*) FROM regularization_requests
        WHERE employee_id = $1
          AND EXTRACT(YEAR FROM work_date) = EXTRACT(YEAR FROM NOW())
          AND EXTRACT(MONTH FROM work_date) = EXTRACT(MONTH FROM NOW())
          AND final_status = 'approved'
        """,
        employee_id
    )
    return result or 0


async def get_approved_minutes_this_month(employee_id: int, db: asyncpg.Connection) -> int:
    """Get total approved regularization minutes in current month."""
    result = await db.fetchval(
        """
        SELECT COALESCE(SUM(requested_minutes), 0)
        FROM regularization_requests
        WHERE employee_id = $1
          AND EXTRACT(YEAR FROM work_date) = EXTRACT(YEAR FROM NOW())
          AND EXTRACT(MONTH FROM work_date) = EXTRACT(MONTH FROM NOW())
          AND final_status = 'approved'
        """,
        employee_id
    )
    return result or 0


def determine_approval_requirement(approved_count: int) -> dict:
    """
    Determine if request needs L2 approval.
    Rules:
    - First 3 approved requests: L1 only
    - 4th approved request onwards: L1 + L2
    """
    if approved_count < 3:
        return {"requires_l2": False, "escalated": False}
    else:
        return {"requires_l2": True, "escalated": True}


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
    
    # Validate work_date
    if req.work_date > date.today():
        raise HTTPException(400, "Cannot request regularization for future dates")
    
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
    
    # ✅ RELAXED VALIDATION: Allow negative actual_worked_minutes for forgot cases
    if req.actual_worked_minutes < 0:
        raise HTTPException(400, "Invalid worked minutes (cannot be negative)")
    
    if req.requested_minutes <= 0:
        raise HTTPException(400, "Requested minutes must be greater than 0")
    
    # Validate shift hours (total cannot exceed shift + reasonable buffer)
    shift_minutes = (emp["shift_end"].hour * 60 + emp["shift_end"].minute) - \
                    (emp["shift_start"].hour * 60 + emp["shift_start"].minute)
    
    # ✅ RELAXED: Allow up to shift_minutes + 2h buffer for forgot cases
    max_allowed = shift_minutes + 120  # shift + 2h buffer
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
    
    # Check monthly limit (20h = 1200 minutes) - only approved requests
    approved_minutes = await get_approved_minutes_this_month(employee_id, db)
    if approved_minutes + req.requested_minutes > 1200:
        remaining = 1200 - approved_minutes
        raise HTTPException(
            400,
            f"Monthly limit exceeded. You have {minutes_to_display(remaining)} remaining this month"
        )
    
    # Get approved request count and determine approval tier
    approved_count = await get_approved_request_count_this_month(employee_id, db)
    approval_info = determine_approval_requirement(approved_count)
    
    # Always get L2 manager from profile (may be None)
    l2_manager_id = emp["l2_manager_id"]
    
    # If L2 approval required but no L2 manager assigned
    if approval_info["requires_l2"] and not l2_manager_id:
        raise HTTPException(
            403,
            f"This is your {approved_count + 1}th request. L2 manager approval required but not assigned. Contact HR."
        )
    
    # Validate L2 manager is active (if required)
    if approval_info["requires_l2"] and l2_manager_id:
        if not await validate_manager_active(l2_manager_id, db):
            raise HTTPException(403, "Your L2 manager is inactive. Contact HR.")
    
    # ✅ CHANGED: Check if daily_summary exists, but don't require punch out
    daily_summary = await db.fetchrow(
        """
        SELECT id, first_punch_in, last_punch_out, total_minutes 
        FROM daily_summary 
        WHERE user_id=$1 AND work_date=$2
        """,
        user["id"], req.work_date
    )
    
    if not daily_summary:
        # ✅ Allow request even without daily_summary (for edge cases)
        # Create a placeholder summary
        async with db.transaction():
            await db.execute(
                """
                INSERT INTO daily_summary (user_id, work_date, total_minutes, status)
                VALUES ($1, $2, $3, 'absent')
                ON CONFLICT (user_id, work_date) DO NOTHING
                """,
                user["id"], req.work_date, 0
            )
    
    # Create request
    async with db.transaction():
        request_id = await db.fetchval(
            """
            INSERT INTO regularization_requests (
                employee_id, work_date, actual_worked_minutes, requested_minutes, reason,
                submitted_by_user_id, l1_manager_id, l1_status,
                l2_manager_id, l2_status, escalation_required, final_status
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9, $10, 'pending')
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
            "pending" if approval_info["requires_l2"] else None,
            approval_info["escalated"],
        )
        
        # Link to daily_summary
        await db.execute(
            """
            UPDATE daily_summary
            SET regularization_request_id=$1, regularization_status='pending'
            WHERE user_id=$2 AND work_date=$3
            """,
            request_id, user["id"], req.work_date
        )
        
        logger.info(
            f"Regularization request created: id={request_id}, emp_id={employee_id}, "
            f"date={req.work_date}, actual={req.actual_worked_minutes}, "
            f"requested={req.requested_minutes}, approved_count={approved_count}, "
            f"requires_l2={approval_info['requires_l2']}"
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
        "l2_status": "pending" if approval_info["requires_l2"] else None,
        "requires_l2": approval_info["requires_l2"],
        "final_status": "pending",
        "message": "Request submitted to " + ("L1 and L2 managers" if approval_info["requires_l2"] else "L1 manager") + " for approval",
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
    
    # Build query with manager names included
    where_clauses = ["employee_id = $1"]
    params = [employee_id]
    
    if status != "all":
        where_clauses.append(f"final_status = ${len(params) + 1}")
        params.append(status)
    
    # ✅ OPTIMIZED: Include manager names in main query
    query = f"""
        SELECT 
            r.id, r.work_date, r.actual_worked_minutes, r.requested_minutes, r.reason,
            r.submitted_at, r.l1_status, r.l2_status, r.final_status,
            r.l1_manager_id, r.l2_manager_id,
            l1_mgr.full_name as l1_manager_name,
            l2_mgr.full_name as l2_manager_name
        FROM regularization_requests r
        LEFT JOIN employees l1_emp ON l1_emp.id = r.l1_manager_id
        LEFT JOIN users l1_mgr ON l1_mgr.id = l1_emp.user_id
        LEFT JOIN employees l2_emp ON l2_emp.id = r.l2_manager_id
        LEFT JOIN users l2_mgr ON l2_mgr.id = l2_emp.user_id
        WHERE {' AND '.join(where_clauses)}
          AND EXTRACT(YEAR FROM r.work_date) = {year}
          AND EXTRACT(MONTH FROM r.work_date) = {month_num}
        ORDER BY r.work_date DESC
    """
    
    rows = await db.fetch(query, *params)
    
    # ✅ No more loop queries for manager names!
    approved_count = sum(1 for r in rows if r["final_status"] == "approved")
    rejected_count = sum(1 for r in rows if r["final_status"] == "rejected")
    pending_count = sum(1 for r in rows if r["final_status"] == "pending")
    
    approved_minutes = await get_approved_minutes_this_month(employee_id, db)
    
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
            payroll_impact="present" if r["final_status"] == "approved" else "absent",
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
    req = await db.fetchrow(
        """
        SELECT r.*, e.shift_start, e.shift_end, u.full_name as employee_name
        FROM regularization_requests r
        JOIN employees e ON e.id = r.employee_id
        JOIN users u ON u.id = e.user_id
        WHERE r.id = $1
        """,
        request_id
    )
    
    if not req:
        raise HTTPException(404, "Request not found")
    
    # Verify access
    # Employee can see own requests
    # L1/L2 managers can see their assigned requests
    # HR can see all requests
    emp = await get_employee_from_user(user["id"], db)
    is_owner = emp["id"] == req["employee_id"]
    is_assigned_manager = emp["id"] in (req["l1_manager_id"], req["l2_manager_id"])
    is_hr = user["role"] in ("hr", "admin")
    
    if not (is_owner or is_assigned_manager or is_hr):
        raise HTTPException(403, "Access denied")
    
    # Get shift minutes
    shift_minutes = (req["shift_end"].hour * 60 + req["shift_end"].minute) - \
                    (req["shift_start"].hour * 60 + req["shift_start"].minute)
    
    gap_minutes = shift_minutes - req["actual_worked_minutes"]
    
    # Get manager names
    l1_name = None
    l2_name = None
    if req["l1_manager_id"]:
        l1_rec = await db.fetchrow(
            "SELECT u.full_name FROM employees e JOIN users u ON u.id = e.user_id WHERE e.id = $1",
            req["l1_manager_id"]
        )
        l1_name = l1_rec["full_name"] if l1_rec else None
    
    if req["l2_manager_id"]:
        l2_rec = await db.fetchrow(
            "SELECT u.full_name FROM employees e JOIN users u ON u.id = e.user_id WHERE e.id = $1",
            req["l2_manager_id"]
        )
        l2_name = l2_rec["full_name"] if l2_rec else None
    
    return RegularizationRequestDetail(
        request_id=req["id"],
        employee_id=req["employee_id"],
        employee_name=req["employee_name"],
        work_date=req["work_date"],
        actual_worked_minutes=req["actual_worked_minutes"],
        actual_worked_display=minutes_to_display(req["actual_worked_minutes"]),
        shift_minutes=shift_minutes,
        shift_display=minutes_to_display(shift_minutes),
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
        requires_l2_approval=req["l2_status"] is not None,
        final_status=req["final_status"],
        is_regularized=req["final_status"] == "approved",
        payroll_status="present" if req["final_status"] == "approved" else "absent",
        payroll_notes=f"{'Regularized' if req['final_status'] == 'approved' else 'Not regularized'} {minutes_to_display(req['requested_minutes'])}",
    )


# ══════════════════════════════════════════════════════════════
# ENDPOINTS: MANAGER APPROVALS
# ══════════════════════════════════════════════════════════════

@router.get("/pending")
async def get_pending_approvals(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> PendingApprovalsResponse:
    emp = await get_employee_from_user(user["id"], db)
    manager_id = emp["id"]
    is_hr = user["role"] in ("hr", "admin")
    
    if is_hr:
        # ✅ OPTIMIZED: Get pending requests WITH approved counts in single query
        query = """
            SELECT 
                r.id as request_id, r.employee_id, r.work_date,
                r.actual_worked_minutes, r.requested_minutes, r.reason,
                r.submitted_at, r.l1_manager_id, r.l2_manager_id,
                r.l1_status, r.l2_status,
                u.full_name as employee_name,
                l1_mgr.full_name as l1_manager_name,
                -- ✅ Calculate approved count in same query
                (
                    SELECT COUNT(*) 
                    FROM regularization_requests r2
                    WHERE r2.employee_id = r.employee_id
                      AND r2.work_date <= r.work_date
                      AND r2.final_status = 'approved'
                ) as approved_count_before
            FROM regularization_requests r
            JOIN employees e ON e.id = r.employee_id
            JOIN users u ON u.id = e.user_id
            LEFT JOIN employees l1_emp ON l1_emp.id = r.l1_manager_id
            LEFT JOIN users l1_mgr ON l1_mgr.id = l1_emp.user_id
            WHERE r.final_status = 'pending'
              AND (
                  (r.l1_status = 'pending')
                  OR
                  (r.l2_status = 'pending' AND r.l1_status = 'approved')
              )
            ORDER BY r.submitted_at ASC
        """
        rows = await db.fetch(query)
    else:
        # Same optimization for regular managers
        query = """
            SELECT 
                r.id as request_id, r.employee_id, r.work_date,
                r.actual_worked_minutes, r.requested_minutes, r.reason,
                r.submitted_at, r.l1_manager_id, r.l2_manager_id,
                r.l1_status, r.l2_status,
                u.full_name as employee_name,
                NULL as l1_manager_name,
                -- ✅ Calculate approved count in same query
                (
                    SELECT COUNT(*) 
                    FROM regularization_requests r2
                    WHERE r2.employee_id = r.employee_id
                      AND r2.work_date <= r.work_date
                      AND r2.final_status = 'approved'
                ) as approved_count_before
            FROM regularization_requests r
            JOIN employees e ON e.id = r.employee_id
            JOIN users u ON u.id = e.user_id
            WHERE r.final_status = 'pending'
              AND (
                  (r.l1_manager_id = $1 AND r.l1_status = 'pending')
                  OR
                  (r.l2_manager_id = $1 AND r.l2_status = 'pending' AND r.l1_status = 'approved')
              )
            ORDER BY r.submitted_at ASC
        """
        rows = await db.fetch(query, manager_id)
    
    # ✅ No more loop queries!
    pending_list = []
    for row in rows:
        approved_count = row["approved_count_before"]  # ✅ Already calculated
        requires_l2 = row["l2_status"] is not None
        
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


@router.post("/requests/{request_id}/approve")
async def approve_regularization_request(
    request_id: int,
    req: RegularizationApprovalRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> RegularizationApprovalResponse:
    """
    POST /api/attendance/regularization/requests/{request_id}/approve
    
    Manager approves a regularization request.
    
    Approval flow:
    - L1 approves: l1_status = 'approved'
    - If L2 not required: final_status = 'approved'
    - If L2 required: wait for L2 approval
    - L2 approves (after L1): final_status = 'approved'
    """
    reg_req = await db.fetchrow("SELECT * FROM regularization_requests WHERE id=$1", request_id)
    if not reg_req:
        raise HTTPException(404, "Request not found")
    
    if reg_req["final_status"] in ("approved", "rejected"):
        raise HTTPException(409, f"Request already {reg_req['final_status']}")
    
    # Get manager employee record
    emp = await get_employee_from_user(user["id"], db)
    manager_id = emp["id"]
    
    # Validate: is this manager assigned to this request?
    is_l1 = manager_id == reg_req["l1_manager_id"]
    is_l2 = manager_id == reg_req["l2_manager_id"]
    
    if not (is_l1 or is_l2):
        raise HTTPException(403, "You are not assigned as approver for this request")
    
    # Validate approval order
    if is_l1:
        if reg_req["l1_status"] != "pending":
            raise HTTPException(409, f"L1 approval already {reg_req['l1_status']}")
    
    if is_l2:
        if reg_req["l1_status"] != "approved":
            raise HTTPException(400, "L1 manager must approve before L2 can review")
        
        if reg_req["l2_status"] != "pending":
            raise HTTPException(409, f"L2 approval already {reg_req['l2_status']}")
    
    approved_role = "l1" if is_l1 else "l2"
    
    # Update approval status
    async with db.transaction():
        if is_l1:
            await db.execute(
                """
                UPDATE regularization_requests
                SET l1_status='approved', l1_approved_at=NOW(), l1_approved_by_user_id=$1, l1_comment=$2
                WHERE id=$3
                """,
                user["id"], req.comment, request_id
            )
        else:  # is_l2
            await db.execute(
                """
                UPDATE regularization_requests
                SET l2_status='approved', l2_approved_at=NOW(), l2_approved_by_user_id=$1, l2_comment=$2
                WHERE id=$3
                """,
                user["id"], req.comment, request_id
            )
        
        # Fetch updated record to determine final status
        updated = await db.fetchrow("SELECT * FROM regularization_requests WHERE id=$1", request_id)
        
        # Determine final status
        final_status = "pending"
        if updated["l1_status"] == "approved":
            if updated["l2_status"] is None:
                # L2 not required
                final_status = "approved"
            elif updated["l2_status"] == "approved":
                # Both approved
                final_status = "approved"
            elif updated["l2_status"] == "pending":
                # Waiting for L2
                final_status = "pending"
        
        if final_status != updated["final_status"]:
            await db.execute(
                "UPDATE regularization_requests SET final_status=$1 WHERE id=$2",
                final_status, request_id
            )
        
        # Update daily_summary if fully approved
        if final_status == "approved":
            total_payroll_minutes = updated["actual_worked_minutes"] + updated["requested_minutes"]
            
            await db.execute(
                """
                UPDATE daily_summary
                SET is_regularized=TRUE, regularization_status='approved',
                    payroll_status='present', payroll_minutes=$1,
                    regularization_minutes=$2,
                    payroll_notes=$3
                WHERE user_id IN (SELECT user_id FROM employees WHERE id=$4)
                  AND work_date=$5
                """,
                total_payroll_minutes,
                updated["requested_minutes"],
                f"Regularized +{minutes_to_display(updated['requested_minutes'])} (L1+L2 approved)",
                updated["employee_id"],
                updated["work_date"],
            )
        
        logger.info(
            f"Regularization request approved: id={request_id}, "
            f"approved_by_role={approved_role}, final_status={final_status}"
        )
    
    return RegularizationApprovalResponse(
        request_id=request_id,
        status="approved",
        approved_by_role=approved_role,
        approved_at=datetime.now(),
        final_status=final_status,
        message=f"Request approved by {approved_role.upper()}" + (
            ". Final approval obtained." if final_status == "approved" else ". Awaiting L2 approval."
        ),
    )


@router.post("/requests/{request_id}/reject")
async def reject_regularization_request(
    request_id: int,
    req: RegularizationRejectionRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> RegularizationApprovalResponse:
    """
    POST /api/attendance/regularization/requests/{request_id}/reject
    
    Manager rejects a regularization request.
    Rejection by either L1 or L2 = final rejection.
    """
    reg_req = await db.fetchrow("SELECT * FROM regularization_requests WHERE id=$1", request_id)
    if not reg_req:
        raise HTTPException(404, "Request not found")
    
    if reg_req["final_status"] in ("approved", "rejected"):
        raise HTTPException(409, f"Request already {reg_req['final_status']}")
    
    # Get manager
    emp = await get_employee_from_user(user["id"], db)
    manager_id = emp["id"]
    
    is_l1 = manager_id == reg_req["l1_manager_id"]
    is_l2 = manager_id == reg_req["l2_manager_id"]
    
    if not (is_l1 or is_l2):
        raise HTTPException(403, "You are not assigned as approver for this request")
    
    # Validate rejection order
    if is_l2 and reg_req["l1_status"] != "approved":
        raise HTTPException(400, "L1 must approve before L2 can reject")
    
    if is_l1 and reg_req["l1_status"] != "pending":
        raise HTTPException(409, f"L1 approval already {reg_req['l1_status']}")
    
    if is_l2 and reg_req["l2_status"] != "pending":
        raise HTTPException(409, f"L2 approval already {reg_req['l2_status']}")
    
    rejected_role = "l1" if is_l1 else "l2"
    
    async with db.transaction():
        if is_l1:
            await db.execute(
                """
                UPDATE regularization_requests
                SET l1_status='rejected', l1_approved_at=NOW(), l1_approved_by_user_id=$1, l1_comment=$2,
                    final_status='rejected'
                WHERE id=$3
                """,
                user["id"], req.comment, request_id
            )
        else:
            await db.execute(
                """
                UPDATE regularization_requests
                SET l2_status='rejected', l2_approved_at=NOW(), l2_approved_by_user_id=$1, l2_comment=$2,
                    final_status='rejected'
                WHERE id=$3
                """,
                user["id"], req.comment, request_id
            )
        
        # Update daily_summary (mark as regularization rejected)
        await db.execute(
            """
            UPDATE daily_summary
            SET is_regularized=FALSE, regularization_status='rejected',
                payroll_status='absent',
                payroll_notes=$1
            WHERE user_id IN (SELECT user_id FROM employees WHERE id=$2)
              AND work_date=$3
            """,
            f"Regularization rejected by {rejected_role.upper()}: {req.comment}. Worked {minutes_to_display(reg_req['actual_worked_minutes'])}",
            reg_req["employee_id"],
            reg_req["work_date"],
        )
        
        logger.info(f"Regularization request rejected: id={request_id}, rejected_by_role={rejected_role}")
    
    return RegularizationApprovalResponse(
        request_id=request_id,
        status="rejected",
        approved_by_role=rejected_role,
        approved_at=datetime.now(),
        final_status="rejected",
        message=f"Request rejected by {rejected_role.upper()}. Day marked as absent.",
    )


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
    
    # Get employee shift times
    emp = await get_employee_from_user(user["id"], db)
    shift_minutes = (emp["shift_end"].hour * 60 + emp["shift_end"].minute) - \
                    (emp["shift_start"].hour * 60 + emp["shift_start"].minute)
    
    # Get daily summaries for the month
    summaries = await db.fetch(
        """
        SELECT ds.*, r.id as reg_req_id, r.requested_minutes, r.l1_status, r.l2_status, r.final_status
        FROM daily_summary ds
        LEFT JOIN regularization_requests r ON r.id = ds.regularization_request_id
        WHERE ds.user_id = $1
          AND EXTRACT(YEAR FROM ds.work_date) = $2
          AND EXTRACT(MONTH FROM ds.work_date) = $3
        ORDER BY ds.work_date
        """,
        user["id"], year, month_num
    )
    
    days = []
    for s in summaries:
        gap_minutes = None
        gap_hours = None
        
        if s["total_minutes"] < shift_minutes:
            gap_minutes = shift_minutes - s["total_minutes"]
            gap_hours = minutes_to_display(gap_minutes)
        
        reg_data = None
        if s["reg_req_id"]:
            reg_data = {
                "request_id": s["reg_req_id"],
                "status": s["final_status"],
                "requested_minutes": s["requested_minutes"],
                "l1_status": s["l1_status"],
                "l2_status": s["l2_status"],
            }
        
        # Format punch times - FIXED: Convert to local timezone
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
                actual_worked_minutes=s["total_minutes"],
                actual_worked=minutes_to_display(s["total_minutes"]),
                shift_start=emp["shift_start"].strftime("%H:%M"),
                shift_end=emp["shift_end"].strftime("%H:%M"),
                shift_minutes=shift_minutes,
                shift_hours=minutes_to_display(shift_minutes),
                status=s["status"],
                is_late=s["is_late"],
                late_by_minutes=s["late_by_minutes"],
                gap_minutes=gap_minutes,
                gap_hours=gap_hours,
                regularization=reg_data,
            )
        )
    
    return AttendanceCalendarResponse(month=month, days=days)