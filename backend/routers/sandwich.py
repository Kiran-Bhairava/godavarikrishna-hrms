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
# DETECTION HELPERS
# ══════════════════════════════════════════════════════════════

async def detect_sandwich_for_employee(
    db: asyncpg.Connection,
    employee_id: int,
    user_id: int,
    year: int,
    month: int,
) -> dict:
    """
    Detect sandwich leave pattern for one employee in the given month.
    
    Sandwich Rule:
    If employee has approved leave on days with ONLY Sundays/holidays between them,
    those non-working days are also counted as leave (sandwich).
    
    CRITICAL: If gap contains ANY working day → NOT sandwich.
    
    Returns:
        {
            "sandwich_days": int,
            "pattern": [
                {
                    "date": "2024-03-17",
                    "reason": "Sunday (weekly off)",
                    "between_leaves": ["2024-03-16", "2024-03-18"]
                }
            ]
        }
    """
    month_start = date(year, month, 1)
    cal_days = calendar.monthrange(year, month)[1]
    month_end = date(year, month, cal_days)
    
    # Fetch all approved leaves in this month (both paid and unpaid)
    leave_rows = await db.fetch(
        """
        SELECT date_from, date_to, leave_type
        FROM leave_requests
        WHERE employee_id = $1
          AND final_status = 'approved'
          AND (
              (date_from BETWEEN $2 AND $3)
              OR (date_to BETWEEN $2 AND $3)
              OR (date_from <= $2 AND date_to >= $3)
          )
        ORDER BY date_from
        """,
        employee_id, month_start, month_end,
    )
    
    if len(leave_rows) < 2:
        # Need at least 2 leave periods for sandwich to occur
        return {"sandwich_days": 0, "pattern": []}
    
    # Get all public holidays in this month
    holiday_rows = await db.fetch(
        """
        SELECT holiday_date, name
        FROM holiday_calendar
        WHERE is_active = TRUE
          AND holiday_date BETWEEN $1 AND $2
        """,
        month_start, month_end,
    )
    holidays = {r["holiday_date"]: r["name"] for r in holiday_rows}
    
    # Build continuous leave calendar (set of all leave dates)
    leave_dates = set()
    for lr in leave_rows:
        d = max(lr["date_from"], month_start)
        end = min(lr["date_to"], month_end)
        while d <= end:
            leave_dates.add(d)
            d += timedelta(days=1)
    
    # Detect sandwich: find gaps between leaves that are ONLY Sundays/holidays
    sandwich_days = []
    all_dates_sorted = sorted(leave_dates)
    
    for i in range(len(all_dates_sorted) - 1):
        current_date = all_dates_sorted[i]
        next_date = all_dates_sorted[i + 1]
        
        gap = (next_date - current_date).days - 1
        
        if gap == 0:
            continue  # Consecutive leave days, no gap
        
        # Collect all dates in the gap
        gap_dates = []
        d = current_date + timedelta(days=1)
        while d < next_date:
            gap_dates.append(d)
            d += timedelta(days=1)
        
        # CRITICAL CHECK: ALL gap days must be ONLY Sundays or holidays
        # If ANY day is a working day (not Sunday, not holiday) → NOT sandwich
        all_non_working = True
        for gd in gap_dates:
            is_sunday = gd.weekday() == 6
            is_holiday = gd in holidays
            
            if not (is_sunday or is_holiday):
                # Found a working day in gap → breaks sandwich
                all_non_working = False
                break
        
        # Only if gap contains EXCLUSIVELY non-working days → sandwich
        if all_non_working and gap_dates:
            for gd in gap_dates:
                is_sunday = gd.weekday() == 6
                is_holiday = gd in holidays
                
                if is_sunday:
                    reason = "Sunday (weekly off)"
                elif is_holiday:
                    reason = f"{holidays[gd]} (public holiday)"
                else:
                    # Should never reach here due to check above
                    continue
                
                sandwich_days.append({
                    "date": gd.isoformat(),
                    "reason": reason,
                    "between_leaves": [current_date.isoformat(), next_date.isoformat()],
                })
    
    return {
        "sandwich_days": len(sandwich_days),
        "pattern": sandwich_days,
    }


async def get_sandwich_decision(
    db: asyncpg.Connection,
    employee_id: int,
    year: int,
    month: int,
) -> Optional[dict]:
    """
    Get existing sandwich decision from payroll_sandwich_reviews table.
    
    Returns None if no decision exists yet.
    Returns dict with decision details if exists.
    """
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
    
    Returns:
        int - Number of sandwich days to add (0 if not applied or no sandwich)
    
    Called by: payroll.py/_fetch_employees()
    """
    decision = await get_sandwich_decision(db, employee_id, year, month)
    
    if not decision:
        return 0
    
    if not decision["sandwich_applied"]:
        return 0
    
    return decision["sandwich_days_detected"] or 0


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@router.get("/review", response_model=SandwichReviewResponse)
async def get_sandwich_review(
    year:         int            = Query(..., ge=2020, le=2100),
    month:        int            = Query(..., ge=1,    le=12),
    branch_id:    Optional[int]  = Query(None),
    employee_ids: Optional[str]  = Query(None),
    _hr: dict    = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
) -> SandwichReviewResponse:
    """
    GET /api/sandwich/review
    
    Detect sandwich leave patterns for all employees in payroll batch.
    Must be called BEFORE processing payroll to allow HR to review and decide.
    
    Query params:
        year, month      - Payroll period
        branch_id        - Filter by branch (optional)
        employee_ids     - Comma-separated employee IDs (optional)
    
    Returns:
        List of employees with sandwich patterns detected,
        showing pattern details and leave balance impact.
    """
    # Parse employee filter (same logic as payroll export)
    emp_id_list = None
    if employee_ids:
        try:
            emp_id_list = [int(x.strip()) for x in employee_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "employee_ids must be comma-separated integers")
    
    # Build filter
    filters = ["e.is_active = TRUE", "e.annual_ctc IS NOT NULL"]
    params  = []
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
    
    # Fetch employees
    emp_rows = await db.fetch(
        f"""
        SELECT
            e.id AS employee_id,
            u.id AS user_id,
            u.full_name AS employee_name
        FROM employees e
        JOIN users u ON u.id = e.user_id
        WHERE {where}
        ORDER BY u.full_name
        """,
        *params,
    )
    
    employees_with_sandwich = []
    
    for emp in emp_rows:
        # Detect sandwich pattern
        detection = await detect_sandwich_for_employee(
            db, emp["employee_id"], emp["user_id"], year, month
        )
        
        if detection["sandwich_days"] == 0:
            continue
        
        # Get current leave balance
        balance_row = await db.fetchrow(
            """
            SELECT remaining_paid_days
            FROM leave_balances
            WHERE employee_id = $1 AND year = $2
            """,
            emp["employee_id"], year,
        )
        current_balance = balance_row["remaining_paid_days"] if balance_row else 0
        
        # Check if HR has already decided for this employee
        existing_decision = await get_sandwich_decision(db, emp["employee_id"], year, month)
        
        employees_with_sandwich.append(
            SandwichEmployeeReview(
                employee_id=emp["employee_id"],
                employee_name=emp["employee_name"],
                sandwich_days=detection["sandwich_days"],
                pattern=[
                    SandwichDayDetail(**day) for day in detection["pattern"]
                ],
                leave_balance_current=current_balance,
                leave_balance_after=max(0, current_balance - detection["sandwich_days"]),
                sandwich_already_decided=existing_decision is not None,
                sandwich_decision=existing_decision["sandwich_applied"] if existing_decision else None,
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
    
    Apply or skip sandwich leave for multiple employees at once.
    HR can choose to apply to all detected employees or specific list.
    
    Body:
        year, month       - Payroll period
        employee_ids      - "all" or list of IDs
        apply_sandwich    - true to apply, false to skip
        reason            - Optional reason for decision
    
    Returns:
        Summary of employees processed and action taken.
    """
    payroll_month = date(body.year, body.month, 1)
    
    # Determine employee IDs
    if body.employee_ids == "all":
        # Get all employees with detected sandwich for this month
        review_response = await get_sandwich_review(
            year=body.year,
            month=body.month,
            _hr=hr,
            db=db,
        )
        target_emp_ids = [e.employee_id for e in review_response.employees_with_sandwich]
    else:
        target_emp_ids = body.employee_ids
    
    if not target_emp_ids:
        raise HTTPException(400, "No employees to process")
    
    processed_count = 0
    
    async with db.transaction():
        for emp_id in target_emp_ids:
            # Re-detect to get fresh pattern
            user_id = await db.fetchval(
                "SELECT user_id FROM employees WHERE id = $1", emp_id
            )
            if not user_id:
                continue
                
            detection = await detect_sandwich_for_employee(
                db, emp_id, user_id, body.year, body.month
            )
            
            if detection["sandwich_days"] == 0:
                continue
            
            # Upsert decision
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
                emp_id,
                payroll_month,
                detection["sandwich_days"],
                detection["pattern"],
                body.apply_sandwich,
                hr["id"],
                body.reason,
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
    
    Apply or skip sandwich leave for a single employee.
    Allows HR to make individual decisions.
    
    Path:
        employee_id - Employee to apply/skip for
    
    Body:
        year, month       - Payroll period
        apply_sandwich    - true to apply, false to skip
        reason            - Optional reason for decision
    
    Returns:
        Confirmation of action taken.
    """
    payroll_month = date(body.year, body.month, 1)
    
    # Get employee user_id
    user_id = await db.fetchval(
        "SELECT user_id FROM employees WHERE id = $1 AND is_active = TRUE",
        employee_id,
    )
    if not user_id:
        raise HTTPException(404, "Employee not found")
    
    # Detect sandwich pattern
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
            employee_id,
            payroll_month,
            detection["sandwich_days"],
            detection["pattern"],
            body.apply_sandwich,
            hr["id"],
            body.reason,
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