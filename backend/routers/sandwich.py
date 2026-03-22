"""
routers/sandwich.py — Sandwich Leave Detection & Management

Handles sandwich leave rule: When employee takes leave before AND after 
a non-working day (Sunday or public holiday), that day is also counted as leave.

Example:
    Friday    - Paid Leave (applied)
    Saturday  - Working day
    Sunday    - Weekly off (SANDWICH - counted as leave)
    Monday    - Paid Leave (applied)
    
    Without sandwich: 2 days deducted
    With sandwich: 3 days deducted

HR reviews and decides whether to apply sandwich during payroll processing.

Endpoints:
    GET  /api/sandwich/review         - Detect patterns for payroll batch
    POST /api/sandwich/apply-bulk     - Apply/skip for multiple employees
    POST /api/sandwich/apply/{emp_id} - Apply/skip for one employee

Integration:
    - Called by payroll.py during payroll processing
    - Decisions stored in payroll_sandwich_reviews table
    - Applied to leaves_taken calculation
"""

import calendar
import logging
from datetime import date, timedelta
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_hr
from db import get_db
from schemas import (
    SandwichReviewResponse,
    SandwichEmployeeReview,
    SandwichDayDetail,
    SandwichApplyBulkRequest,
    SandwichApplyIndividualRequest,
)

logger = logging.getLogger("sandwich")
router = APIRouter(prefix="/api/sandwich", tags=["sandwich"])


# ══════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════

def _parse_weekly_off(weekly_off_str: Optional[str]) -> set[int]:
    """
    Parse employee's weekly_off string into weekday integers (Mon=0, Sun=6).
    Defaults to {6} (Sunday only) — kept in sync with payroll and leave modules.
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


# ══════════════════════════════════════════════════════════════
# DETECTION HELPERS
# ══════════════════════════════════════════════════════════════

def _detect_sandwich_from_data(
    weekly_off_str: Optional[str],
    leave_rows: list,
    holidays: dict,
    month_start: date,
    month_end: date,
) -> dict:
    """
    Pure-Python sandwich detection — no DB calls.
    All data passed in from callers that batch-fetched it.

    Same logic as before, just decoupled from DB.
    """
    off_days = _parse_weekly_off(weekly_off_str)

    if len(leave_rows) < 2:
        return {"sandwich_days": 0, "pattern": []}

    # Build continuous leave calendar (set of all leave dates in month)
    leave_dates = set()
    for lr in leave_rows:
        d   = max(lr["date_from"], month_start)
        end = min(lr["date_to"],   month_end)
        while d <= end:
            leave_dates.add(d)
            d += timedelta(days=1)

    sandwich_days = []
    all_dates_sorted = sorted(leave_dates)

    for i in range(len(all_dates_sorted) - 1):
        current_date = all_dates_sorted[i]
        next_date    = all_dates_sorted[i + 1]

        gap = (next_date - current_date).days - 1
        if gap == 0:
            continue  # consecutive leave days, no gap

        # Collect gap dates
        gap_dates = []
        d = current_date + timedelta(days=1)
        while d < next_date:
            gap_dates.append(d)
            d += timedelta(days=1)

        # CRITICAL: ALL gap days must be non-working (weekly-off or holiday)
        all_non_working = all(
            gd.weekday() in off_days or gd in holidays
            for gd in gap_dates
        )

        if all_non_working and gap_dates:
            for gd in gap_dates:
                if gd.weekday() in off_days:
                    reason = "Sunday (weekly off)" if gd.weekday() == 6 else "Weekly off"
                elif gd in holidays:
                    reason = f"{holidays[gd]} (public holiday)"
                else:
                    continue
                sandwich_days.append({
                    "date": gd.isoformat(),
                    "reason": reason,
                    "between_leaves": [current_date.isoformat(), next_date.isoformat()],
                })

    return {"sandwich_days": len(sandwich_days), "pattern": sandwich_days}


async def detect_sandwich_for_employee(
    db: asyncpg.Connection,
    employee_id: int,
    user_id: int,
    year: int,
    month: int,
) -> dict:
    """
    Detect sandwich for ONE employee — used by individual apply endpoint.
    Fetches its own data (3 queries) — acceptable for single-employee calls.
    """
    month_start = date(year, month, 1)
    month_end   = date(year, month, calendar.monthrange(year, month)[1])

    weekly_off_str = await db.fetchval(
        "SELECT weekly_off FROM employees WHERE id = $1", employee_id
    )
    leave_rows = await db.fetch(
        """
        SELECT date_from, date_to, leave_type
        FROM leave_requests
        WHERE employee_id = $1
          AND final_status = 'approved'
          AND (
              (date_from BETWEEN $2 AND $3)
              OR (date_to   BETWEEN $2 AND $3)
              OR (date_from <= $2 AND date_to >= $3)
          )
        ORDER BY date_from
        """,
        employee_id, month_start, month_end,
    )
    holiday_rows = await db.fetch(
        """
        SELECT holiday_date, name FROM holiday_calendar
        WHERE is_active = TRUE AND holiday_date BETWEEN $1 AND $2
        """,
        month_start, month_end,
    )
    holidays = {r["holiday_date"]: r["name"] for r in holiday_rows}

    return _detect_sandwich_from_data(weekly_off_str, leave_rows, holidays, month_start, month_end)


async def get_sandwich_decision(
    db: asyncpg.Connection,
    employee_id: int,
    year: int,
    month: int,
) -> Optional[dict]:
    """Get existing sandwich decision. Returns None if not decided yet."""
    payroll_month = date(year, month, 1)
    row = await db.fetchrow(
        """
        SELECT sandwich_applied, sandwich_days_detected,
               decision_by_user_id, decision_at, decision_reason
        FROM payroll_sandwich_reviews
        WHERE employee_id = $1 AND payroll_month = $2
        """,
        employee_id, payroll_month,
    )
    return dict(row) if row else None


# ══════════════════════════════════════════════════════════════
# PUBLIC API - Used by payroll.py
# ══════════════════════════════════════════════════════════════

async def get_sandwich_days_for_payroll(
    db: asyncpg.Connection,
    employee_id: int,
    year: int,
    month: int,
) -> int:
    """
    Public helper for payroll.py to get sandwich days to add to leaves_taken.
    Returns 0 if not applied or no sandwich.
    """
    decision = await get_sandwich_decision(db, employee_id, year, month)
    if not decision or not decision["sandwich_applied"]:
        return 0
    return decision["sandwich_days_detected"] or 0


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@router.get("/review", response_model=SandwichReviewResponse)
async def get_sandwich_review(
    year:         int           = Query(..., ge=2020, le=2100),
    month:        int           = Query(..., ge=1,    le=12),
    branch_id:    Optional[int] = Query(None),
    employee_ids: Optional[str] = Query(None),
    _hr: dict    = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
) -> SandwichReviewResponse:
    """
    GET /api/sandwich/review

    Detect sandwich patterns for all employees in payroll batch.
    Optimized: batch-fetches holidays, leaves, balances, and decisions
    in 4 queries total — no per-employee queries.
    """
    emp_id_list = None
    if employee_ids:
        try:
            emp_id_list = [int(x.strip()) for x in employee_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "employee_ids must be comma-separated integers")

    filters = ["e.is_active = TRUE", "e.annual_ctc IS NOT NULL"]
    params: list = []
    p = 1

    if branch_id:
        filters.append(f"e.branch_id = ${p}")
        params.append(branch_id)
        p += 1
    if emp_id_list:
        filters.append(f"e.id = ANY(${p})")
        params.append(emp_id_list)
        p += 1

    where = " AND ".join(filters)

    # ── Query 1: all employees with weekly_off ─────────────────
    emp_rows = await db.fetch(
        f"""
        SELECT e.id AS employee_id, u.id AS user_id,
               u.full_name AS employee_name, e.weekly_off
        FROM employees e
        JOIN users u ON u.id = e.user_id
        WHERE {where}
        ORDER BY u.full_name
        """,
        *params,
    )

    if not emp_rows:
        return SandwichReviewResponse(year=year, month=month,
                                      total_employees=0, employees_with_sandwich=[])

    month_start   = date(year, month, 1)
    month_end     = date(year, month, calendar.monthrange(year, month)[1])
    all_emp_ids   = [r["employee_id"] for r in emp_rows]
    payroll_month = date(year, month, 1)

    # ── Query 2: all approved leaves for all employees in month ─
    leave_rows_all = await db.fetch(
        """
        SELECT employee_id, date_from, date_to, leave_type
        FROM leave_requests
        WHERE employee_id = ANY($1::int[])
          AND final_status = 'approved'
          AND (
              (date_from BETWEEN $2 AND $3)
              OR (date_to   BETWEEN $2 AND $3)
              OR (date_from <= $2 AND date_to >= $3)
          )
        ORDER BY employee_id, date_from
        """,
        all_emp_ids, month_start, month_end,
    )
    # Group by employee_id
    leaves_by_emp: dict[int, list] = {}
    for lr in leave_rows_all:
        leaves_by_emp.setdefault(lr["employee_id"], []).append(lr)

    # ── Query 3: holidays for the month (shared across all employees) ──
    holiday_rows = await db.fetch(
        """
        SELECT holiday_date, name FROM holiday_calendar
        WHERE is_active = TRUE AND holiday_date BETWEEN $1 AND $2
        """,
        month_start, month_end,
    )
    holidays = {r["holiday_date"]: r["name"] for r in holiday_rows}

    # ── Query 4: leave balances for all employees ──────────────
    bal_rows = await db.fetch(
        """
        SELECT employee_id, cl_remaining, sl_remaining
        FROM leave_balances
        WHERE employee_id = ANY($1::int[]) AND year = $2
        """,
        all_emp_ids, year,
    )
    bal_by_emp = {r["employee_id"]: (r["cl_remaining"] or 0) + (r["sl_remaining"] or 0)
                  for r in bal_rows}

    # ── Query 5: existing decisions for all employees ──────────
    decision_rows = await db.fetch(
        """
        SELECT employee_id, sandwich_applied, sandwich_days_detected
        FROM payroll_sandwich_reviews
        WHERE employee_id = ANY($1::int[]) AND payroll_month = $2
        """,
        all_emp_ids, payroll_month,
    )
    decision_by_emp = {r["employee_id"]: r for r in decision_rows}

    # ── Detect sandwich for each employee (pure Python, no DB) ─
    employees_with_sandwich = []

    for emp in emp_rows:
        emp_id      = emp["employee_id"]
        emp_leaves  = leaves_by_emp.get(emp_id, [])

        detection = _detect_sandwich_from_data(
            emp["weekly_off"], emp_leaves, holidays, month_start, month_end
        )

        if detection["sandwich_days"] == 0:
            continue

        current_balance  = bal_by_emp.get(emp_id, 0)
        existing         = decision_by_emp.get(emp_id)

        employees_with_sandwich.append(
            SandwichEmployeeReview(
                employee_id=emp_id,
                employee_name=emp["employee_name"],
                sandwich_days=detection["sandwich_days"],
                pattern=[SandwichDayDetail(**day) for day in detection["pattern"]],
                leave_balance_current=current_balance,
                leave_balance_after=max(0, current_balance - detection["sandwich_days"]),
                sandwich_already_decided=existing is not None,
                sandwich_decision=existing["sandwich_applied"] if existing else None,
            )
        )

    logger.info(
        "Sandwich review: year=%d month=%d found=%d employees",
        year, month, len(employees_with_sandwich),
    )

    return SandwichReviewResponse(
        year=year,
        month=month,
        total_employees=len(employees_with_sandwich),
        employees_with_sandwich=employees_with_sandwich,
    )


@router.post("/apply-bulk")
async def apply_sandwich_bulk(
    body: SandwichApplyBulkRequest,
    hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    POST /api/sandwich/apply-bulk

    Apply or skip sandwich for multiple employees.
    Optimized: batch-fetches all data before looping, single upsert per employee.
    """
    payroll_month = date(body.year, body.month, 1)

    # Resolve "all" → run review to get employees with sandwich
    if body.employee_ids == "all":
        review = await get_sandwich_review(
            year=body.year, month=body.month, _hr=hr, db=db
        )
        target_emp_ids = [e.employee_id for e in review.employees_with_sandwich]
    else:
        target_emp_ids = body.employee_ids

    if not target_emp_ids:
        raise HTTPException(400, "No employees to process")

    month_start = date(body.year, body.month, 1)
    month_end   = date(body.year, body.month, calendar.monthrange(body.year, body.month)[1])

    # ── Batch fetch all data needed for detection ──────────────
    emp_rows = await db.fetch(
        """
        SELECT id AS employee_id, user_id, weekly_off
        FROM employees
        WHERE id = ANY($1::int[]) AND is_active = TRUE
        """,
        target_emp_ids,
    )
    emp_map = {r["employee_id"]: r for r in emp_rows}

    leave_rows_all = await db.fetch(
        """
        SELECT employee_id, date_from, date_to, leave_type
        FROM leave_requests
        WHERE employee_id = ANY($1::int[])
          AND final_status = 'approved'
          AND (
              (date_from BETWEEN $2 AND $3)
              OR (date_to   BETWEEN $2 AND $3)
              OR (date_from <= $2 AND date_to >= $3)
          )
        ORDER BY employee_id, date_from
        """,
        target_emp_ids, month_start, month_end,
    )
    leaves_by_emp: dict[int, list] = {}
    for lr in leave_rows_all:
        leaves_by_emp.setdefault(lr["employee_id"], []).append(lr)

    holiday_rows = await db.fetch(
        """
        SELECT holiday_date, name FROM holiday_calendar
        WHERE is_active = TRUE AND holiday_date BETWEEN $1 AND $2
        """,
        month_start, month_end,
    )
    holidays = {r["holiday_date"]: r["name"] for r in holiday_rows}

    # ── Upsert decisions in one transaction ───────────────────
    processed_count = 0

    async with db.transaction():
        for emp_id in target_emp_ids:
            emp = emp_map.get(emp_id)
            if not emp:
                continue

            detection = _detect_sandwich_from_data(
                emp["weekly_off"],
                leaves_by_emp.get(emp_id, []),
                holidays,
                month_start,
                month_end,
            )

            if detection["sandwich_days"] == 0:
                continue

            await db.execute(
                """
                INSERT INTO payroll_sandwich_reviews
                    (employee_id, payroll_month, sandwich_days_detected,
                     sandwich_pattern, sandwich_applied, decision_by_user_id,
                     decision_at, decision_reason)
                VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7)
                ON CONFLICT (employee_id, payroll_month) DO UPDATE
                SET sandwich_days_detected = EXCLUDED.sandwich_days_detected,
                    sandwich_pattern       = EXCLUDED.sandwich_pattern,
                    sandwich_applied       = EXCLUDED.sandwich_applied,
                    decision_by_user_id    = EXCLUDED.decision_by_user_id,
                    decision_at            = NOW(),
                    decision_reason        = EXCLUDED.decision_reason,
                    updated_at             = NOW()
                """,
                emp_id, payroll_month, detection["sandwich_days"],
                detection["pattern"], body.apply_sandwich, hr["id"], body.reason,
            )
            processed_count += 1

    action = "applied" if body.apply_sandwich else "skipped"

    logger.info(
        "Sandwich bulk %s: year=%d month=%d employees=%d by_user=%d",
        action, body.year, body.month, processed_count, hr["id"],
    )

    return {
        "year": body.year,
        "month": body.month,
        "employees_processed": processed_count,
        "sandwich_applied": body.apply_sandwich,
        "message": f"Sandwich {action} for {processed_count} employee(s)",
    }


@router.post("/apply/{employee_id}")
async def apply_sandwich_individual(
    employee_id: int,
    body: SandwichApplyIndividualRequest,
    hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    POST /api/sandwich/apply/{employee_id}
    Apply or skip sandwich for a single employee.
    """
    payroll_month = date(body.year, body.month, 1)

    user_id = await db.fetchval(
        "SELECT user_id FROM employees WHERE id = $1 AND is_active = TRUE",
        employee_id,
    )
    if not user_id:
        raise HTTPException(404, "Employee not found")

    detection = await detect_sandwich_for_employee(
        db, employee_id, user_id, body.year, body.month
    )

    if detection["sandwich_days"] == 0:
        raise HTTPException(400, "No sandwich pattern detected for this employee")

    async with db.transaction():
        await db.execute(
            """
            INSERT INTO payroll_sandwich_reviews
                (employee_id, payroll_month, sandwich_days_detected,
                 sandwich_pattern, sandwich_applied, decision_by_user_id,
                 decision_at, decision_reason)
            VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7)
            ON CONFLICT (employee_id, payroll_month) DO UPDATE
            SET sandwich_days_detected = EXCLUDED.sandwich_days_detected,
                sandwich_pattern       = EXCLUDED.sandwich_pattern,
                sandwich_applied       = EXCLUDED.sandwich_applied,
                decision_by_user_id    = EXCLUDED.decision_by_user_id,
                decision_at            = NOW(),
                decision_reason        = EXCLUDED.decision_reason,
                updated_at             = NOW()
            """,
            employee_id, payroll_month, detection["sandwich_days"],
            detection["pattern"], body.apply_sandwich, hr["id"], body.reason,
        )

    action = "applied" if body.apply_sandwich else "skipped"

    logger.info(
        "Sandwich individual %s: emp=%d year=%d month=%d days=%d by_user=%d",
        action, employee_id, body.year, body.month,
        detection["sandwich_days"], hr["id"],
    )

    return {
        "employee_id": employee_id,
        "year": body.year,
        "month": body.month,
        "sandwich_days": detection["sandwich_days"],
        "sandwich_applied": body.apply_sandwich,
        "message": f"Sandwich {action} for employee {employee_id}",
    }