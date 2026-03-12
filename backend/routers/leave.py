"""
routers/leave.py — Leave Management Module

Handles:
  - Employee: apply leave, view requests, view balance, cancel
  - Manager (L1): approve / reject
  - HR/Admin (L2): final approve / reject
  - HR: manage holiday calendar, adjust leave balances

Leave types: paid | unpaid
Approval: EVERY request goes L1 → L2 (unconditional, unlike regularization)

On final L2 approval → daily_summary rows updated:
  status        = 'leave'
  payroll_status = 'present'  (paid)  |  'absent'  (unpaid)

Holiday calendar blocks leave on those dates.
Paid balance deducted only on final L2 approval.
"""
import asyncpg
import logging
import pytz
from datetime import datetime, date, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from auth import get_current_user, require_hr
from db import get_db
from config import settings

from schemas import (
    LeaveRequestCreate,
    LeaveApprovalRequest,
    LeaveRejectionRequest,
    LeaveRequestRow,
    LeaveRequestsListResponse,
    LeavePendingApprovalRow,
    LeavePendingApprovalsResponse,
    LeaveApprovalResponse,
    LeaveBalanceResponse,
    HolidayRow,
    HolidayCreate,
    LeaveBalanceAdjust,
)

logger = logging.getLogger("leave")
router = APIRouter(prefix="/api/leave", tags=["leave"])


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

async def _get_employee(user_id: int, db: asyncpg.Connection) -> dict:
    """Get employee record from user_id. Raises 404 if not found/inactive."""
    emp = await db.fetchrow(
        """
        SELECT e.id, e.l1_manager_id, e.l2_manager_id, e.weekly_off
        FROM employees e
        WHERE e.user_id = $1 AND e.is_active = TRUE
        """,
        user_id,
    )
    if not emp:
        raise HTTPException(404, "Employee profile not found")
    return dict(emp)


def _parse_weekly_off(weekly_off_str: str | None) -> set[int]:
    """
    Parse employee's weekly_off string into weekday integers (Mon=0, Sun=6).
    Handles: "Sunday", "Saturday & Sunday", "Saturday, Sunday", any day name.
    Defaults to {6} (Sunday only) if blank or unparseable — matches payroll default.

    Kept in sync with payroll._parse_weekly_off — both must use the same logic
    so leave working-day counts match payroll present-day counts.
    """
    WEEKDAY_MAP = {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
    if not weekly_off_str:
        return {6}
    days: set[int] = set()
    for token in weekly_off_str.lower().replace("&", " ").replace(",", " ").split():
        if token in WEEKDAY_MAP:
            days.add(WEEKDAY_MAP[token])
    return days if days else {6}


async def _get_working_days(
    db: asyncpg.Connection,
    employee_id: int,
    date_from: date,
    date_to: date,
) -> list[date]:
    """
    Return working days in range (inclusive), excluding holidays + weekly off.

    Uses _parse_weekly_off (handles all 7 days) — same logic as payroll so
    leave day counts always match what payroll counts as present days.
    """
    holidays_raw = await db.fetch(
        """
        SELECT holiday_date FROM holiday_calendar
        WHERE holiday_date BETWEEN $1 AND $2 AND is_active = TRUE
        """,
        date_from, date_to,
    )
    holidays = {r["holiday_date"] for r in holidays_raw}

    weekly_off_str = await db.fetchval(
        "SELECT weekly_off FROM employees WHERE id = $1",
        employee_id,
    )
    off_days = _parse_weekly_off(weekly_off_str)

    working = []
    cur = date_from
    while cur <= date_to:
        if cur.weekday() not in off_days and cur not in holidays:
            working.append(cur)
        cur += timedelta(days=1)
    return working


async def _get_or_init_balance(
    db: asyncpg.Connection, employee_id: int, year: int
) -> dict:
    """
    Fetch leave_balances row; lazy-creates from policy if missing.
    Uses employee-specific policy first, then company default.
    """
    row = await db.fetchrow(
        "SELECT * FROM leave_balances WHERE employee_id = $1 AND year = $2",
        employee_id, year,
    )
    if row:
        return dict(row)

    # Pull from policy (employee-specific beats company default)
    paid_days = await db.fetchval(
        """
        SELECT paid_days_per_year FROM leave_policies
        WHERE (employee_id = $1 OR employee_id IS NULL)
        ORDER BY employee_id NULLS LAST LIMIT 1
        """,
        employee_id,
    ) or 12

    row = await db.fetchrow(
        """
        INSERT INTO leave_balances
            (employee_id, year, total_paid_days, used_paid_days, remaining_paid_days)
        VALUES ($1, $2, $3, 0, $3)
        ON CONFLICT (employee_id, year) DO UPDATE
            SET updated_at = NOW()
        RETURNING *
        """,
        employee_id, year, paid_days,
    )
    return dict(row)


async def _sync_daily_summary(
    db: asyncpg.Connection,
    *,
    user_id: int,
    working_days: list[date],
    leave_type: str,
    final_status: str,
    leave_request_id: int,
) -> None:
    """
    Upsert daily_summary for each working day in a leave request.

    approved  → status='leave', payroll_status='present'(paid)/'absent'(unpaid)

    cancelled/rejected → revert ONLY rows owned by this leave request
      (leave_request_id must match — prevents touching rows owned by other requests).
      Revert logic restores the correct state based on what else is on the row:
        - If day was regularized (is_regularized=TRUE) → restore to 'present' + keep reg fields
        - Else if punches exist (total_minutes > 0)    → restore to 'present'
        - Else                                          → restore to 'absent'
    """
    if not working_days:
        return

    if final_status == "approved":
        p_status = "present" if leave_type == "paid" else "absent"
        p_notes  = f"{'Paid' if leave_type == 'paid' else 'Unpaid'} leave approved"
        for d in working_days:
            await db.execute(
                """
                INSERT INTO daily_summary
                    (user_id, work_date, total_minutes, status,
                     payroll_status, payroll_minutes, payroll_notes, leave_request_id)
                VALUES ($1, $2, 0, 'leave', $3, 0, $4, $5)
                ON CONFLICT (user_id, work_date) DO UPDATE
                    SET status           = 'leave',
                        payroll_status   = $3,
                        payroll_notes    = $4,
                        leave_request_id = $5
                """,
                user_id, d, p_status, p_notes, leave_request_id,
            )
    else:
        # Revert only rows owned by this leave request.
        # Priority on revert: regularization > raw attendance > absent.
        # If is_regularized is TRUE, the day was approved-regularized before
        # leave was stamped — restore to regularized-present state.
        # Otherwise fall back to punch-based status.
        await db.execute(
            """
            UPDATE daily_summary
            SET
                status           = CASE
                                     WHEN is_regularized THEN 'present'
                                     WHEN total_minutes > 0 THEN 'present'
                                     ELSE 'absent'
                                   END,
                payroll_status   = CASE
                                     WHEN is_regularized THEN 'present'
                                     WHEN total_minutes > 0 THEN 'present'
                                     ELSE 'absent'
                                   END,
                payroll_notes    = CASE
                                     WHEN is_regularized THEN 'Regularization approved — hours credited'
                                     ELSE NULL
                                   END,
                leave_request_id = NULL
            WHERE user_id = $1
              AND work_date = ANY($2::date[])
              AND leave_request_id = $3
            """,
            user_id, working_days, leave_request_id,
        )


async def _adjust_balance(
    db: asyncpg.Connection, *, employee_id: int, year: int, delta: int
) -> None:
    """delta > 0 = deduct, delta < 0 = refund. Only for paid leave."""
    await db.execute(
        """
        UPDATE leave_balances
        SET used_paid_days      = used_paid_days + $3,
            remaining_paid_days = remaining_paid_days - $3,
            updated_at          = NOW()
        WHERE employee_id = $1 AND year = $2
        """,
        employee_id, year, delta,
    )


# ══════════════════════════════════════════════════════════════
# EMPLOYEE ENDPOINTS
# ══════════════════════════════════════════════════════════════

@router.post("/request")
async def apply_leave(
    req: LeaveRequestCreate,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    POST /api/leave/request
    Employee applies for paid or unpaid leave.

    Validations:
    - date_from <= date_to
    - At least 1 working day in range
    - No overlapping pending/approved leave
    - Sufficient paid balance (paid leave only)
    - L1 and L2 managers must be assigned
    """
    emp = await _get_employee(user["id"], db)

    if req.date_from > req.date_to:
        raise HTTPException(400, "date_from must be on or before date_to")
    # Block cross-year requests — balance is tracked per-year; a request spanning
    # Dec→Jan would have to deduct from two separate year balances which we don't support.
    if req.date_from.year != req.date_to.year:
        raise HTTPException(400, "Leave request cannot span across calendar years. Submit separate requests for each year.")
    if not emp["l1_manager_id"]:
        raise HTTPException(400, "No L1 manager assigned. Contact HR.")
    if not emp["l2_manager_id"]:
        raise HTTPException(400, "No L2 manager assigned. Contact HR.")

    working_days = await _get_working_days(db, emp["id"], req.date_from, req.date_to)
    if not working_days:
        raise HTTPException(400, "No working days in selected range (holidays/weekly-off only).")

    num_days = len(working_days)

    # Overlap check — single query
    overlap = await db.fetchval(
        """
        SELECT COUNT(*) FROM leave_requests
        WHERE employee_id  = $1
          AND final_status IN ('pending', 'approved')
          AND date_from   <= $3
          AND date_to     >= $2
        """,
        emp["id"], req.date_from, req.date_to,
    )
    if overlap:
        raise HTTPException(409, "Overlapping pending/approved leave exists for these dates.")

    # Balance check for paid leave
    if req.leave_type == "paid":
        bal = await _get_or_init_balance(db, emp["id"], req.date_from.year)
        if bal["remaining_paid_days"] < num_days:
            raise HTTPException(
                400,
                f"Insufficient paid leave. Available: {bal['remaining_paid_days']}d, "
                f"Requested: {num_days}d.",
            )

    async with db.transaction():
        request_id = await db.fetchval(
            """
            INSERT INTO leave_requests (
                employee_id, date_from, date_to, num_days,
                leave_type, reason,
                l1_manager_id, l2_manager_id,
                l1_status, l2_status, final_status,
                submitted_by_user_id
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'pending','pending','pending',$9)
            RETURNING id
            """,
            emp["id"], req.date_from, req.date_to, num_days,
            req.leave_type, req.reason,
            emp["l1_manager_id"], emp["l2_manager_id"],
            user["id"],
        )

    logger.info(
        "Leave request created: id=%s emp=%s %s→%s %dd type=%s",
        request_id, emp["id"], req.date_from, req.date_to, num_days, req.leave_type,
    )

    return {
        "request_id": request_id,
        "date_from": req.date_from,
        "date_to": req.date_to,
        "num_days": num_days,
        "leave_type": req.leave_type,
        "final_status": "pending",
        "message": f"Leave request submitted for {num_days} working day(s). Awaiting L1 approval.",
    }


@router.get("/requests")
async def list_my_leave_requests(
    year: Optional[int] = Query(None),
    status: str = Query("all"),
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> LeaveRequestsListResponse:
    """GET /api/leave/requests — Employee's own requests."""
    emp = await _get_employee(user["id"], db)
    year = year or date.today().year

    params: list = [emp["id"], date(year, 1, 1), date(year, 12, 31)]
    status_clause = ""
    if status != "all":
        params.append(status)
        status_clause = f"AND lr.final_status = ${len(params)}"

    rows = await db.fetch(
        f"""
        SELECT
            lr.id, lr.date_from, lr.date_to, lr.num_days,
            lr.leave_type, lr.reason, lr.submitted_at,
            lr.l1_status, lr.l1_comment, lr.l1_approved_at,
            lr.l2_status, lr.l2_comment, lr.l2_approved_at,
            lr.final_status, lr.cancelled_at,
            l1_u.full_name AS l1_manager_name,
            l2_u.full_name AS l2_manager_name
        FROM leave_requests lr
        LEFT JOIN employees l1e ON l1e.id = lr.l1_manager_id
        LEFT JOIN users     l1_u ON l1_u.id = l1e.user_id
        LEFT JOIN employees l2e ON l2e.id = lr.l2_manager_id
        LEFT JOIN users     l2_u ON l2_u.id = l2e.user_id
        WHERE lr.employee_id = $1
          AND lr.date_from BETWEEN $2 AND $3
          {status_clause}
        ORDER BY lr.submitted_at DESC
        """,
        *params,
    )

    bal = await _get_or_init_balance(db, emp["id"], year)

    return LeaveRequestsListResponse(
        year=year,
        total=len(rows),
        approved=sum(1 for r in rows if r["final_status"] == "approved"),
        rejected=sum(1 for r in rows if r["final_status"] == "rejected"),
        pending=sum(1 for r in rows if r["final_status"] == "pending"),
        cancelled=sum(1 for r in rows if r["final_status"] == "cancelled"),
        paid_balance_total=bal["total_paid_days"],
        paid_balance_used=bal["used_paid_days"],
        paid_balance_remaining=bal["remaining_paid_days"],
        requests=[
            LeaveRequestRow(
                request_id=r["id"],
                date_from=r["date_from"],
                date_to=r["date_to"],
                num_days=r["num_days"],
                leave_type=r["leave_type"],
                reason=r["reason"],
                submitted_at=r["submitted_at"],
                l1_status=r["l1_status"],
                l1_manager_name=r["l1_manager_name"],
                l1_comment=r["l1_comment"],
                l1_approved_at=r["l1_approved_at"],
                l2_status=r["l2_status"],
                l2_manager_name=r["l2_manager_name"],
                l2_comment=r["l2_comment"],
                l2_approved_at=r["l2_approved_at"],
                final_status=r["final_status"],
                cancelled_at=r["cancelled_at"],
                payroll_impact=(
                    "pending" if r["final_status"] == "pending"
                    else "absent" if r["final_status"] in ("rejected", "cancelled")
                    else "present" if r["leave_type"] == "paid"
                    else "absent"
                ),
            )
            for r in rows
        ],
    )


@router.get("/balance")
async def get_my_leave_balance(
    year: Optional[int] = Query(None),
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> LeaveBalanceResponse:
    """GET /api/leave/balance — Employee's own balance."""
    emp = await _get_employee(user["id"], db)
    year = year or date.today().year
    bal = await _get_or_init_balance(db, emp["id"], year)
    return LeaveBalanceResponse(
        employee_id=emp["id"],
        year=year,
        total_paid_days=bal["total_paid_days"],
        used_paid_days=bal["used_paid_days"],
        remaining_paid_days=bal["remaining_paid_days"],
    )


@router.post("/requests/{request_id}/cancel")
async def cancel_leave(
    request_id: int,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    POST /api/leave/requests/{id}/cancel
    - Pending: just cancel, no balance touch.
    - Approved + future: cancel + refund paid days + revert daily_summary.
    - Approved + already started: blocked.
    """
    emp = await _get_employee(user["id"], db)

    req = await db.fetchrow(
        "SELECT * FROM leave_requests WHERE id = $1 AND employee_id = $2",
        request_id, emp["id"],
    )
    if not req:
        raise HTTPException(404, "Leave request not found")
    if req["final_status"] == "cancelled":
        raise HTTPException(400, "Already cancelled")
    if req["final_status"] == "rejected":
        raise HTTPException(400, "Cannot cancel a rejected request")
    if req["final_status"] == "approved" and req["date_from"] <= date.today():
        raise HTTPException(400, "Cannot cancel leave that has already started")
    # Block pending cancellation for past-start dates too —
    # manager may still approve it which would stamp leave on days already worked
    if req["final_status"] == "pending" and req["date_from"] < date.today():
        raise HTTPException(400, "Cannot cancel pending leave whose start date has passed. Contact HR.")

    async with db.transaction():
        await db.execute(
            "UPDATE leave_requests SET final_status='cancelled', cancelled_at=NOW() WHERE id=$1",
            request_id,
        )
        if req["final_status"] == "approved":
            working_days = await _get_working_days(
                db, emp["id"], req["date_from"], req["date_to"]
            )
            await _sync_daily_summary(
                db,
                user_id=user["id"],
                working_days=working_days,
                leave_type=req["leave_type"],
                final_status="cancelled",
                leave_request_id=request_id,
            )
            if req["leave_type"] == "paid":
                # Refund exactly what was deducted at approval time.
                # num_days was updated to the live count at approval, so this is
                # always the exact amount that was deducted — no leak possible.
                await _adjust_balance(
                    db, employee_id=emp["id"],
                    year=req["date_from"].year,
                    delta=-req["num_days"],
                )

    return {"request_id": request_id, "status": "cancelled"}


# ══════════════════════════════════════════════════════════════
# MANAGER / HR APPROVAL ENDPOINTS
# ══════════════════════════════════════════════════════════════

@router.get("/pending")
async def get_pending_approvals(
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> LeavePendingApprovalsResponse:
    """
    GET /api/leave/pending
    L1 and L2 are both plain employees — role is irrelevant.
    HR/admin users without an employee profile get an empty list, not a 404.
    """
    # Soft lookup — HR users may not have an employees row
    emp = await db.fetchrow(
        "SELECT id FROM employees WHERE user_id = $1 AND is_active = TRUE",
        user["id"],
    )
    if not emp:
        return LeavePendingApprovalsResponse(pending_count=0, pending_requests=[])

    rows = await db.fetch(
        """
        SELECT
            lr.id AS request_id, lr.employee_id,
            lr.date_from, lr.date_to, lr.num_days,
            lr.leave_type, lr.reason, lr.submitted_at,
            lr.l1_status, lr.l1_comment, lr.l2_status,
            u.full_name AS employee_name,
            l1_u.full_name AS l1_manager_name
        FROM leave_requests lr
        JOIN employees e   ON e.id = lr.employee_id
        JOIN users u       ON u.id = e.user_id
        LEFT JOIN employees l1e ON l1e.id = lr.l1_manager_id
        LEFT JOIN users l1_u    ON l1_u.id = l1e.user_id
        WHERE lr.final_status = 'pending'
          AND (
              (lr.l1_manager_id = $1 AND lr.l1_status = 'pending')
              OR
              (lr.l2_manager_id = $1 AND lr.l2_status = 'pending' AND lr.l1_status = 'approved')
          )
        ORDER BY lr.submitted_at ASC
        """,
        emp["id"],
    )

    return LeavePendingApprovalsResponse(
        pending_count=len(rows),
        pending_requests=[
            LeavePendingApprovalRow(
                request_id=r["request_id"],
                employee_id=r["employee_id"],
                employee_name=r["employee_name"],
                date_from=r["date_from"],
                date_to=r["date_to"],
                num_days=r["num_days"],
                leave_type=r["leave_type"],
                reason=r["reason"],
                submitted_at=r["submitted_at"],
                l1_status=r["l1_status"],
                l1_manager_name=r["l1_manager_name"],
                # L1-pending → this employee acts as L1; L2-pending → acts as L2
                awaiting_role="l2" if r["l2_status"] == "pending" and r["l1_status"] == "approved" else "l1",
            )
            for r in rows
        ],
    )


@router.post("/requests/{request_id}/l1-approve")
async def l1_approve(
    request_id: int,
    body: LeaveApprovalRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> LeaveApprovalResponse:
    """L1 approves → moves request to HR queue."""
    emp = await _get_employee(user["id"], db)

    req = await db.fetchrow(
        "SELECT * FROM leave_requests WHERE id=$1 AND l1_manager_id=$2",
        request_id, emp["id"],
    )
    if not req:
        raise HTTPException(404, "Request not found or not your assignment")
    if req["l1_status"] != "pending":
        raise HTTPException(400, f"Already actioned by L1: {req['l1_status']}")
    if req["final_status"] != "pending":
        raise HTTPException(400, f"Request is {req['final_status']}")

    async with db.transaction():
        await db.execute(
            """
            UPDATE leave_requests
            SET l1_status = 'approved', l1_approved_at = NOW(),
                l1_approved_by_user_id = $2, l1_comment = $3
            WHERE id = $1
            """,
            request_id, user["id"], body.comment,
        )

    return LeaveApprovalResponse(
        request_id=request_id, status="approved", approved_by_role="l1",
        approved_at=datetime.now(), final_status="pending",
        message="L1 approved. Forwarded to HR for final approval.",
    )


@router.post("/requests/{request_id}/l1-reject")
async def l1_reject(
    request_id: int,
    body: LeaveRejectionRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> LeaveApprovalResponse:
    """L1 rejects → final rejection, no HR step needed."""
    emp = await _get_employee(user["id"], db)

    req = await db.fetchrow(
        "SELECT * FROM leave_requests WHERE id=$1 AND l1_manager_id=$2",
        request_id, emp["id"],
    )
    if not req:
        raise HTTPException(404, "Request not found or not your assignment")
    if req["l1_status"] != "pending":
        raise HTTPException(400, f"Already actioned by L1: {req['l1_status']}")

    async with db.transaction():
        await db.execute(
            """
            UPDATE leave_requests
            SET l1_status = 'rejected', l1_approved_at = NOW(),
                l1_approved_by_user_id = $2, l1_comment = $3,
                l2_status = 'na', final_status = 'rejected'
            WHERE id = $1
            """,
            request_id, user["id"], body.comment,
        )

    return LeaveApprovalResponse(
        request_id=request_id, status="rejected", approved_by_role="l1",
        approved_at=datetime.now(), final_status="rejected",
        message="Leave rejected by L1 manager.",
    )


@router.post("/requests/{request_id}/l2-approve")
async def l2_approve(
    request_id: int,
    body: LeaveApprovalRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> LeaveApprovalResponse:
    """
    L2 (assigned employee manager) final approval.
    - Updates daily_summary for each working day in range
    - Deducts paid leave balance
    - Only the employee assigned as l2_manager_id can approve
    """
    emp_id = await db.fetchval(
        "SELECT id FROM employees WHERE user_id = $1 AND is_active = TRUE",
        user["id"],
    )
    if not emp_id:
        raise HTTPException(403, "No employee profile found.")

    req = await db.fetchrow(
        """
        SELECT lr.*, e.user_id AS employee_user_id
        FROM leave_requests lr
        JOIN employees e ON e.id = lr.employee_id
        WHERE lr.id = $1 AND lr.l2_manager_id = $2
        """,
        request_id, emp_id,
    )
    if not req:
        raise HTTPException(404, "Leave request not found or not your assignment")
    if req["l1_status"] != "approved":
        raise HTTPException(400, "L1 approval required before HR can act")
    if req["l2_status"] != "pending":
        raise HTTPException(400, f"Already actioned by L2: {req['l2_status']}")

    working_days = await _get_working_days(
        db, req["employee_id"], req["date_from"], req["date_to"]
    )
    # live_days = working days recalculated right now (inside transaction).
    # We use this SAME count for:
    #   1. Stamping daily_summary rows (via _sync_daily_summary)
    #   2. Deducting paid leave balance
    # This guarantees deduct == refund always, even if a holiday was added
    # between submission and approval (which would change the count).
    # num_days stored on the request is for display only.
    live_days = len(working_days)

    async with db.transaction():
        await db.execute(
            """
            UPDATE leave_requests
            SET l2_status = 'approved', l2_approved_at = NOW(),
                l2_approved_by_user_id = $2, l2_comment = $3,
                final_status = 'approved',
                num_days = $4
            WHERE id = $1
            """,
            request_id, user["id"], body.comment, live_days,
        )
        await _sync_daily_summary(
            db,
            user_id=req["employee_user_id"],
            working_days=working_days,
            leave_type=req["leave_type"],
            final_status="approved",
            leave_request_id=request_id,
        )
        if req["leave_type"] == "paid":
            await _adjust_balance(
                db, employee_id=req["employee_id"],
                year=req["date_from"].year,
                delta=live_days,   # same value stamped on DS rows — always consistent
            )

    logger.info(
        "Leave %s fully approved by L2 user %s. days=%s type=%s",
        request_id, user["id"], live_days, req["leave_type"],
    )

    return LeaveApprovalResponse(
        request_id=request_id, status="approved", approved_by_role="l2",
        approved_at=datetime.now(), final_status="approved",
        message=(
            f"Leave approved. {live_days} day(s) marked as "
            f"{'paid leave' if req['leave_type'] == 'paid' else 'unpaid leave'}."
        ),
    )


@router.post("/requests/{request_id}/l2-reject")
async def l2_reject(
    request_id: int,
    body: LeaveRejectionRequest,
    user: dict = Depends(get_current_user),
    db: asyncpg.Connection = Depends(get_db),
) -> LeaveApprovalResponse:
    """L2 (assigned employee manager) rejects leave. Only the assigned l2_manager_id can reject."""
    emp_id = await db.fetchval(
        "SELECT id FROM employees WHERE user_id = $1 AND is_active = TRUE",
        user["id"],
    )
    if not emp_id:
        raise HTTPException(403, "No employee profile found.")

    req = await db.fetchrow(
        "SELECT * FROM leave_requests WHERE id=$1 AND l2_manager_id=$2",
        request_id, emp_id,
    )
    if not req:
        raise HTTPException(404, "Leave request not found or not your assignment")
    if req["l1_status"] != "approved":
        raise HTTPException(400, "L1 approval required before HR can act")
    if req["l2_status"] != "pending":
        raise HTTPException(400, f"Already actioned by L2: {req['l2_status']}")

    async with db.transaction():
        await db.execute(
            """
            UPDATE leave_requests
            SET l2_status = 'rejected', l2_approved_at = NOW(),
                l2_approved_by_user_id = $2, l2_comment = $3,
                final_status = 'rejected'
            WHERE id = $1
            """,
            request_id, user["id"], body.comment,
        )

    return LeaveApprovalResponse(
        request_id=request_id, status="rejected", approved_by_role="l2",
        approved_at=datetime.now(), final_status="rejected",
        message="Leave rejected by HR.",
    )


# ══════════════════════════════════════════════════════════════
# HR — HOLIDAY CALENDAR
# ══════════════════════════════════════════════════════════════

async def _sync_holiday_to_daily_summary(
    db: asyncpg.Connection,
    *,
    holiday_id: int,
    holiday_date: date,
    holiday_name: str,
    activate: bool,           # True = mark as holiday, False = revert
) -> int:
    """
    Sync a holiday date across all active employees' daily_summary.

    activate=True:
        Bulk-upsert one row per active employee for that date.
        Sets status='present', payroll_status='present', payroll_notes='Holiday: <n>',
        holiday_id=<id> (used for reliable revert).
        Rows that already have an approved leave (leave_request_id IS NOT NULL) are
        left untouched — leave takes priority over holiday.

    activate=False (holiday removed):
        Bulk-update rows where holiday_id matches — immune to name changes/renames.
        Rows with punch data revert to 'present'; rows without revert to 'absent'.
        Rows owned by a leave request are left untouched.

    Returns count of employees affected.
    Single bulk query — no per-employee loop.
    """
    note = f"Holiday: {holiday_name}"

    if activate:
        result = await db.execute(
            """
            INSERT INTO daily_summary
                (user_id, work_date, total_minutes, status,
                 payroll_status, payroll_minutes, payroll_notes, holiday_id)
            SELECT e.user_id, $1, 0, 'present', 'present', 0, $2, $3
            FROM employees e
            JOIN users u ON u.id = e.user_id
            WHERE e.is_active = TRUE AND u.is_active = TRUE
            ON CONFLICT (user_id, work_date) DO UPDATE
                SET status         = CASE
                        WHEN daily_summary.leave_request_id IS NOT NULL THEN daily_summary.status
                        ELSE 'present'
                    END,
                    payroll_status = CASE
                        WHEN daily_summary.leave_request_id IS NOT NULL THEN daily_summary.payroll_status
                        ELSE 'present'
                    END,
                    payroll_notes  = CASE
                        WHEN daily_summary.leave_request_id IS NOT NULL THEN daily_summary.payroll_notes
                        ELSE $2
                    END,
                    holiday_id     = CASE
                        WHEN daily_summary.leave_request_id IS NOT NULL THEN daily_summary.holiday_id
                        ELSE $3
                    END
            """,
            holiday_date, note, holiday_id,
        )
    else:
        # Match by holiday_id — survives renames and is always exact
        result = await db.execute(
            """
            UPDATE daily_summary
            SET status         = CASE WHEN total_minutes > 0 THEN 'present' ELSE 'absent' END,
                payroll_status = CASE WHEN total_minutes > 0 THEN 'present' ELSE 'absent' END,
                payroll_notes  = NULL,
                holiday_id     = NULL
            WHERE work_date = $1
              AND holiday_id = $2
              AND leave_request_id IS NULL
            """,
            holiday_date, holiday_id,
        )

    # asyncpg returns "INSERT N" or "UPDATE N" — extract the count
    try:
        affected = int(result.split()[-1])
    except (IndexError, ValueError):
        affected = 0

    return affected




@router.get("/holidays")
async def list_holidays(
    year: Optional[int] = Query(None),
    db: asyncpg.Connection = Depends(get_db),
) -> list[HolidayRow]:
    """GET /api/leave/holidays — no auth, anyone can read."""
    year = year or date.today().year
    rows = await db.fetch(
        """
        SELECT id, holiday_date, name, holiday_type, is_active
        FROM holiday_calendar
        WHERE EXTRACT(YEAR FROM holiday_date) = $1
        ORDER BY holiday_date ASC
        """,
        year,
    )
    return [HolidayRow(**dict(r)) for r in rows]


@router.post("/holidays")
async def add_holiday(
    body: HolidayCreate,
    hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
) -> dict:
    """
    POST /api/leave/holidays — HR/Admin only.

    Upserts holiday_calendar and syncs daily_summary for all active employees.
    Employees who already have approved leave on that date are unaffected.
    """
    async with db.transaction():
        row = await db.fetchrow(
            """
            INSERT INTO holiday_calendar (holiday_date, name, holiday_type)
            VALUES ($1, $2, $3)
            ON CONFLICT (holiday_date) DO UPDATE
                SET name         = EXCLUDED.name,
                    holiday_type = EXCLUDED.holiday_type,
                    is_active    = TRUE
            RETURNING id, holiday_date, name, holiday_type, is_active
            """,
            body.holiday_date, body.name, body.holiday_type,
        )

        affected = await _sync_holiday_to_daily_summary(
            db,
            holiday_id=row["id"],
            holiday_date=body.holiday_date,
            holiday_name=body.name,
            activate=True,
        )

    logger.info(
        "Holiday added: %s (%s) on %s — synced to %d employees",
        body.name, body.holiday_type, body.holiday_date, affected,
    )

    return {
        **HolidayRow(**dict(row)).model_dump(),
        "employees_synced": affected,
        "message": f"Holiday declared. {affected} employee(s) marked present for payroll.",
    }


@router.delete("/holidays/{holiday_id}")
async def remove_holiday(
    holiday_id: int,
    hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    DELETE /api/leave/holidays/{id} — soft delete.

    Reverts daily_summary rows that were set by this holiday.
    Rows with approved leave or actual punch data are preserved correctly.
    """
    # Fetch before deactivating so we have name + date for the sync
    holiday = await db.fetchrow(
        "SELECT id, holiday_date, name FROM holiday_calendar WHERE id = $1 AND is_active = TRUE",
        holiday_id,
    )
    if not holiday:
        raise HTTPException(404, "Holiday not found or already removed")

    async with db.transaction():
        await db.execute(
            "UPDATE holiday_calendar SET is_active = FALSE WHERE id = $1",
            holiday_id,
        )

        affected = await _sync_holiday_to_daily_summary(
            db,
            holiday_id=holiday["id"],
            holiday_date=holiday["holiday_date"],
            holiday_name=holiday["name"],
            activate=False,
        )

    logger.info(
        "Holiday removed: %s on %s — reverted %d employee rows",
        holiday["name"], holiday["holiday_date"], affected,
    )

    return {
        "id": holiday_id,
        "status": "removed",
        "employees_reverted": affected,
        "message": f"Holiday removed. {affected} employee(s) reverted to attendance-based status.",
    }


@router.put("/holidays/{holiday_id}")
async def update_holiday(
    holiday_id: int,
    body: HolidayCreate,
    hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
) -> dict:
    """
    PUT /api/leave/holidays/{id} — HR/Admin only.

    Updates holiday name/type. Re-syncs daily_summary with new name
    (so payroll_notes stays accurate).
    """
    # Fetch current to check it exists and get old name for revert
    existing = await db.fetchrow(
        "SELECT id, holiday_date, name FROM holiday_calendar WHERE id = $1 AND is_active = TRUE",
        holiday_id,
    )
    if not existing:
        raise HTTPException(404, "Holiday not found or inactive")

    async with db.transaction():
        row = await db.fetchrow(
            """
            UPDATE holiday_calendar
            SET name = $2, holiday_type = $3
            WHERE id = $1
            RETURNING id, holiday_date, name, holiday_type, is_active
            """,
            holiday_id, body.name, body.holiday_type,
        )

        # Revert by holiday_id (immune to name change), then re-apply with new name
        await _sync_holiday_to_daily_summary(
            db,
            holiday_id=holiday_id,
            holiday_date=existing["holiday_date"],
            holiday_name=existing["name"],
            activate=False,
        )
        affected = await _sync_holiday_to_daily_summary(
            db,
            holiday_id=holiday_id,
            holiday_date=existing["holiday_date"],
            holiday_name=body.name,
            activate=True,
        )

    return {
        **HolidayRow(**dict(row)).model_dump(),
        "employees_synced": affected,
        "message": f"Holiday updated. {affected} employee(s) re-synced.",
    }


# ══════════════════════════════════════════════════════════════
# HR — LEAVE BALANCE MANAGEMENT
# ══════════════════════════════════════════════════════════════

@router.get("/hr/balances/{employee_id}")
async def hr_get_balance(
    employee_id: int,
    year: Optional[int] = Query(None),
    hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
) -> LeaveBalanceResponse:
    """HR views any employee's leave balance."""
    year = year or date.today().year
    bal = await _get_or_init_balance(db, employee_id, year)
    return LeaveBalanceResponse(
        employee_id=employee_id, year=year,
        total_paid_days=bal["total_paid_days"],
        used_paid_days=bal["used_paid_days"],
        remaining_paid_days=bal["remaining_paid_days"],
    )


@router.patch("/hr/balances/{employee_id}")
async def hr_adjust_balance(
    employee_id: int,
    body: LeaveBalanceAdjust,
    hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
) -> LeaveBalanceResponse:
    """HR sets total_paid_days for an employee (carry-forward, corrections)."""
    year = body.year or date.today().year
    bal = await _get_or_init_balance(db, employee_id, year)  # ensure row exists

    # Guard: can't set total below already-used days — would make remaining negative
    if body.total_paid_days < bal["used_paid_days"]:
        raise HTTPException(
            400,
            f"Cannot set total to {body.total_paid_days} — employee has already used {bal['used_paid_days']} day(s).",
        )

    row = await db.fetchrow(
        """
        UPDATE leave_balances
        SET total_paid_days     = $3,
            remaining_paid_days = $3 - used_paid_days,
            updated_at          = NOW()
        WHERE employee_id = $1 AND year = $2
        RETURNING *
        """,
        employee_id, year, body.total_paid_days,
    )
    return LeaveBalanceResponse(
        employee_id=employee_id, year=year,
        total_paid_days=row["total_paid_days"],
        used_paid_days=row["used_paid_days"],
        remaining_paid_days=row["remaining_paid_days"],
    )


@router.get("/hr/requests")
async def hr_list_requests(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    status: str = Query("all"),
    employee_id: Optional[int] = Query(None),
    hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
):
    """GET /api/leave/hr/requests — filtered view for HR."""
    year = year or date.today().year
    conditions = ["EXTRACT(YEAR FROM lr.date_from) = $1"]
    params: list = [year]

    if month:
        params.append(month)
        conditions.append(f"EXTRACT(MONTH FROM lr.date_from) = ${len(params)}")
    if status != "all":
        params.append(status)
        conditions.append(f"lr.final_status = ${len(params)}")
    if employee_id:
        params.append(employee_id)
        conditions.append(f"lr.employee_id = ${len(params)}")

    rows = await db.fetch(
        f"""
        SELECT
            lr.id, lr.employee_id,
            lr.date_from, lr.date_to, lr.num_days,
            lr.leave_type, lr.reason, lr.submitted_at,
            lr.l1_status, lr.l1_comment, lr.l1_approved_at,
            lr.l2_status, lr.l2_comment, lr.l2_approved_at,
            lr.final_status, lr.cancelled_at,
            u.full_name AS employee_name,
            l1_u.full_name AS l1_manager_name,
            l2_u.full_name AS l2_manager_name
        FROM leave_requests lr
        JOIN employees e   ON e.id = lr.employee_id
        JOIN users u       ON u.id = e.user_id
        LEFT JOIN employees l1e ON l1e.id = lr.l1_manager_id
        LEFT JOIN users l1_u    ON l1_u.id = l1e.user_id
        LEFT JOIN employees l2e ON l2e.id = lr.l2_manager_id
        LEFT JOIN users l2_u    ON l2_u.id = l2e.user_id
        WHERE {' AND '.join(conditions)}
        ORDER BY lr.submitted_at DESC
        """,
        *params,
    )

    return {
        "year": year,
        "total": len(rows),
        "requests": [dict(r) for r in rows],
    }