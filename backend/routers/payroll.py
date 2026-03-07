"""
payroll.py — Payroll Engine

Flow:
  1. HR picks month + branch/employees
  2. GET /api/payroll/export  → pulls DB data, returns pre-filled Excel
  3. HR reviews, corrects if needed
  4. POST /api/payroll/process → accepts corrected Excel, calculates, returns:
       - Excel summary (all employees)
       - ZIP of individual PDF payslips

Salary formula (all derived from annual_ctc stored in employees table):
  fixed_monthly = annual_ctc / 12
  basic         = fixed_monthly * 0.40
  hra           = basic * 0.50
  ca            = basic * 0.15
  sa            = fixed_monthly - (basic + hra + ca)
  gross         = fixed_monthly  (always)

Deductions:
  lop     = gross / calendar_days * absent_days
  pf      = min(basic * 0.12, 1800)  [if pf_enrolled]
  esi     = TBD                       [if esic_applicable AND gross <= 15000]
  pt      = TBD
  net_pay = gross - lop - pf - esi - pt

Attendance source: Excel wins over DB on conflict (HR's correction is final).

Locking: Once payroll is processed and payslips generated, it is final.
         No re-run endpoint — HR must export again for a new month.
"""

import io
import logging
import calendar
import zipfile
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from auth import require_hr
from db import get_db

logger = logging.getLogger("payroll")
router = APIRouter(prefix="/api/payroll", tags=["payroll"])


# ══════════════════════════════════════════════════════════════
# CONSTANTS
# ══════════════════════════════════════════════════════════════

PF_RATE        = Decimal("0.12")
PF_CAP         = Decimal("1800")    # max PF deduction per month
ESI_THRESHOLD  = Decimal("15000")   # gross <= this → ESI applicable
ESI_EMP_RATE   = Decimal("0.0075")  # employee side (rates TBD, placeholder)

# Excel column order — MUST stay in sync between export and process
COLUMNS = [
    "emp_id",
    "employee_name",
    "designation",
    "department",
    "branch",
    "bank_name",
    "bank_account",
    "bank_ifsc",
    "pan_number",
    "pf_enrolled",
    "esic_applicable",
    "annual_ctc",
    "fixed_monthly",
    "basic",
    "hra",
    "ca",
    "sa",
    "gross",
    "calendar_days",
    "present_days",
    "absent_days",
    "lop",
    "pf_deduction",
    "esi_deduction",
    "net_pay",
]

# Human-readable headers for the Excel sheet
HEADERS = [
    "Emp ID",
    "Employee Name",
    "Designation",
    "Department",
    "Branch",
    "Bank Name",
    "Bank Account",
    "Bank IFSC",
    "PAN",
    "PF Enrolled",
    "ESI Applicable",
    "Annual CTC",
    "Fixed Monthly",
    "Basic",
    "HRA",
    "Conveyance",
    "Special Allowance",
    "Gross",
    "Calendar Days",
    "Present Days",
    "Absent Days",
    "LOP Deduction",
    "PF Deduction",
    "ESI Deduction",
    "Net Pay",
]

# Columns HR is allowed to edit (zero-indexed positions in COLUMNS list)
# Everything else is protected/informational
EDITABLE_COLS = {
    COLUMNS.index("present_days"),
    COLUMNS.index("absent_days"),
    COLUMNS.index("lop"),
    COLUMNS.index("pf_deduction"),
    COLUMNS.index("esi_deduction"),
}

# Currency columns (for formatting)
CURRENCY_COLS = {
    "annual_ctc", "fixed_monthly", "basic", "hra", "ca", "sa", "gross",
    "lop", "pf_deduction", "esi_deduction", "net_pay",
}


# ══════════════════════════════════════════════════════════════
# SALARY CALCULATION HELPERS
# ══════════════════════════════════════════════════════════════

def _round2(val: Decimal) -> Decimal:
    """Round to 2 decimal places, half-up."""
    return val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_salary_components(annual_ctc: Decimal) -> dict:
    """
    Derive all salary components from annual CTC.
    Returns dict with Decimal values rounded to 2dp.
    """
    fixed   = _round2(annual_ctc / 12)
    basic   = _round2(fixed * Decimal("0.40"))
    hra     = _round2(basic * Decimal("0.50"))
    ca      = _round2(basic * Decimal("0.15"))
    sa      = _round2(fixed - basic - hra - ca)
    gross   = fixed  # always equals fixed by construction
    return {
        "fixed_monthly": fixed,
        "basic":         basic,
        "hra":           hra,
        "ca":            ca,
        "sa":            sa,
        "gross":         gross,
    }


def compute_deductions(
    gross: Decimal,
    basic: Decimal,
    calendar_days: int,
    absent_days: int,
    pf_enrolled: bool,
    esic_applicable: bool,
) -> dict:
    """
    Compute LOP, PF, ESI deductions.
    ESI rates are placeholder — update when confirmed.
    """
    # LOP: gross / calendar_days * absent_days
    if calendar_days > 0 and absent_days > 0:
        lop = _round2(gross / calendar_days * absent_days)
    else:
        lop = Decimal("0.00")

    # PF: 12% of basic, capped at 1800
    if pf_enrolled:
        pf = _round2(min(basic * PF_RATE, PF_CAP))
    else:
        pf = Decimal("0.00")

    # ESI: only if gross <= 15000 and esic_applicable
    # Rates TBD — using 0.75% placeholder
    if esic_applicable and gross <= ESI_THRESHOLD:
        esi = _round2(gross * ESI_EMP_RATE)
    else:
        esi = Decimal("0.00")

    net_pay = _round2(gross - lop - pf - esi)

    return {
        "lop":          lop,
        "pf_deduction": pf,
        "esi_deduction": esi,
        "net_pay":      net_pay,
    }


# ══════════════════════════════════════════════════════════════
# DB QUERIES
# ══════════════════════════════════════════════════════════════

async def _fetch_employees(
    db: asyncpg.Connection,
    year: int,
    month: int,
    branch_id: Optional[int],
    employee_ids: Optional[list[int]],
) -> list[dict]:
    """
    Pull all required payroll data for the given month.
    Attendance pulled from daily_summary using payroll_status.
    """
    cal_days = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end   = date(year, month, cal_days)

    # Build WHERE clause
    filters = ["e.is_active = TRUE", "e.annual_ctc IS NOT NULL"]
    params  = [month_start, month_end]
    p = 3  # next param index

    if branch_id:
        filters.append(f"e.branch_id = ${p}")
        params.append(branch_id)
        p += 1

    if employee_ids:
        filters.append(f"e.id = ANY(${p})")
        params.append(employee_ids)
        p += 1

    where = " AND ".join(filters)

    rows = await db.fetch(
        f"""
        SELECT
            COALESCE(e.emp_id, 'EMP-' || e.id::text)    AS emp_id,
            e.id                                        AS employee_id,
            u.full_name                                 AS employee_name,
            e.designation,
            e.department,
            b.name                                      AS branch,
            e.bank_name,
            e.bank_account,
            e.bank_ifsc,
            e.pan_number,
            e.pf_enrolled,
            e.esic_applicable,
            e.annual_ctc,

            -- Attendance from daily_summary (payroll_status is source of truth)
            COUNT(ds.id) FILTER (
                WHERE ds.payroll_status = 'present'
            )                                           AS present_days,
            COUNT(ds.id) FILTER (
                WHERE ds.payroll_status = 'absent'
            )                                           AS absent_days

        FROM employees e
        JOIN users u       ON u.id = e.user_id
        LEFT JOIN branches b ON b.id = e.branch_id
        LEFT JOIN daily_summary ds
               ON ds.user_id = e.user_id
              AND ds.work_date BETWEEN $1 AND $2

        WHERE {where}
        GROUP BY e.id, e.emp_id, u.full_name, e.designation, e.department,
                 b.name, e.bank_name, e.bank_account, e.bank_ifsc,
                 e.pan_number, e.pf_enrolled, e.esic_applicable, e.annual_ctc
        ORDER BY u.full_name
        """,
        *params,
    )

    result = []
    for r in rows:
        emp       = dict(r)
        annual    = Decimal(str(emp["annual_ctc"]))
        sal       = compute_salary_components(annual)
        cal_days_int  = cal_days
        present   = int(emp["present_days"] or 0)
        absent    = int(emp["absent_days"]  or 0)

        ded = compute_deductions(
            gross         = sal["gross"],
            basic         = sal["basic"],
            calendar_days = cal_days_int,
            absent_days   = absent,
            pf_enrolled   = bool(emp["pf_enrolled"]),
            esic_applicable = bool(emp["esic_applicable"]),
        )

        emp.update(sal)
        emp.update(ded)
        emp["calendar_days"] = cal_days_int
        emp["present_days"]  = present
        emp["absent_days"]   = absent
        emp["annual_ctc"]    = annual
        result.append(emp)

    return result


# ══════════════════════════════════════════════════════════════
# EXCEL HELPERS
# ══════════════════════════════════════════════════════════════

def _style_header(ws, num_cols: int):
    """Apply header row styling."""
    header_fill   = PatternFill("solid", fgColor="041553")
    header_font   = Font(bold=True, color="FFFFFF", size=10)
    center_align  = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for col_idx, header in enumerate(HEADERS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=header)
        cell.fill      = header_fill
        cell.font      = header_font
        cell.alignment = center_align

    ws.row_dimensions[1].height = 32


def _style_data_row(ws, row_idx: int, num_cols: int, editable_fill, locked_fill):
    """Apply alternating row colours; highlight editable cells."""
    alt_fill = PatternFill("solid", fgColor="F8F9FC") if row_idx % 2 == 0 else None

    thin = Side(style="thin", color="DDDDDD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for col_idx in range(1, num_cols + 1):
        cell = ws.cell(row=row_idx, column=col_idx)
        cell.border    = border
        cell.alignment = Alignment(horizontal="center", vertical="center")

        col_key = COLUMNS[col_idx - 1]
        if (col_idx - 1) in EDITABLE_COLS:
            cell.fill = editable_fill
        elif alt_fill:
            cell.fill = alt_fill


def _build_export_workbook(
    employees: list[dict],
    year: int,
    month: int,
) -> Workbook:
    wb = Workbook()
    ws = wb.active
    ws.title = f"Payroll {year}-{month:02d}"

    editable_fill = PatternFill("solid", fgColor="FFF9E6")  # light yellow = editable
    locked_fill   = PatternFill("solid", fgColor="F0F0F0")

    _style_header(ws, len(COLUMNS))

    for emp in employees:
        row_data = []
        for col in COLUMNS:
            val = emp.get(col)
            if isinstance(val, Decimal):
                val = float(val)
            elif isinstance(val, bool):
                val = "Yes" if val else "No"
            row_data.append(val)
        ws.append(row_data)

    # Style data rows
    for row_idx in range(2, len(employees) + 2):
        _style_data_row(ws, row_idx, len(COLUMNS), editable_fill, locked_fill)

    # Column widths
    col_widths = {
        "emp_id": 10, "employee_name": 22, "designation": 18, "department": 16,
        "branch": 14, "bank_name": 16, "bank_account": 18, "bank_ifsc": 14,
        "pan_number": 12, "pf_enrolled": 10, "esic_applicable": 12,
        "annual_ctc": 14, "fixed_monthly": 13, "basic": 10, "hra": 10,
        "ca": 10, "sa": 16, "gross": 12, "calendar_days": 12,
        "present_days": 11, "absent_days": 10, "lop": 13,
        "pf_deduction": 12, "esi_deduction": 12, "net_pay": 13,
    }
    for idx, col in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = col_widths.get(col, 12)

    # Add legend note below data
    legend_row = len(employees) + 3
    ws.cell(row=legend_row, column=1,
            value="🟡 Yellow columns = HR editable | All others are calculated from DB")
    ws.cell(row=legend_row, column=1).font = Font(italic=True, color="888888", size=9)

    # Freeze header row
    ws.freeze_panes = "A2"

    return wb


# ══════════════════════════════════════════════════════════════
# PDF PAYSLIP HELPER
# ══════════════════════════════════════════════════════════════

def _generate_payslip_pdf(emp: dict, year: int, month: int) -> bytes:
    """
    Generate a single employee payslip as a real PDF using reportlab.
    Returns raw PDF bytes.
    """
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, HRFlowable
    )
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT

    # ── Colours ───────────────────────────────────────────────
    NAVY   = colors.HexColor("#041553")
    LIGHT  = colors.HexColor("#EFF3FF")
    WHITE  = colors.white
    GREY   = colors.HexColor("#6B7280")
    LGREY  = colors.HexColor("#F8F9FC")
    GREEN  = colors.HexColor("#10B981")
    BLACK  = colors.HexColor("#111827")

    month_name = calendar.month_name[month]

    def fmt(val):
        if val is None:
            return "—"
        try:
            return f"\u20b9{float(val):,.2f}"
        except Exception:
            return str(val)

    def fval(key):
        return float(emp.get(key) or 0)

    gross   = fval("gross")
    basic   = fval("basic")
    hra     = fval("hra")
    ca      = fval("ca")
    sa      = fval("sa")
    lop     = fval("lop")
    pf      = fval("pf_deduction")
    esi     = fval("esi_deduction")
    net     = fval("net_pay")
    present = int(emp.get("present_days") or 0)
    absent  = int(emp.get("absent_days")  or 0)
    cal     = int(emp.get("calendar_days") or 0)
    total_ded = lop + pf + esi

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15*mm, rightMargin=15*mm,
        topMargin=12*mm, bottomMargin=12*mm,
    )

    styles = getSampleStyleSheet()
    W = A4[0] - 30*mm  # usable width

    def style(name, **kw):
        s = ParagraphStyle(name, parent=styles["Normal"], **kw)
        return s

    bold12  = style("b12",  fontSize=12, textColor=BLACK, fontName="Helvetica-Bold")
    bold10  = style("b10",  fontSize=10, textColor=BLACK, fontName="Helvetica-Bold")
    norm9   = style("n9",   fontSize=9,  textColor=GREY)
    norm10  = style("n10",  fontSize=10, textColor=BLACK)
    white10 = style("w10",  fontSize=10, textColor=WHITE, fontName="Helvetica-Bold")
    white14 = style("w14",  fontSize=14, textColor=WHITE, fontName="Helvetica-Bold", alignment=TA_RIGHT)
    navy9   = style("nav9", fontSize=9,  textColor=NAVY,  fontName="Helvetica-Bold")
    cent9   = style("c9",   fontSize=9,  textColor=BLACK, alignment=TA_CENTER)
    cent9b  = style("c9b",  fontSize=9,  textColor=NAVY,  fontName="Helvetica-Bold", alignment=TA_CENTER)
    centg   = style("cg",   fontSize=8,  textColor=GREY,  alignment=TA_CENTER)

    story = []

    # ── Header bar ────────────────────────────────────────────
    header_data = [[
        Paragraph("GodavariKrishna Group", style("co", fontSize=16, textColor=WHITE, fontName="Helvetica-Bold")),
        Paragraph(f"Payslip — {month_name} {year}", style("pt", fontSize=10, textColor=colors.HexColor("#93C5FD"), alignment=TA_RIGHT)),
    ]]
    header_tbl = Table(header_data, colWidths=[W*0.6, W*0.4])
    header_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), NAVY),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("LEFTPADDING",  (0,0), (0,0), 12),
        ("RIGHTPADDING", (1,0), (1,0), 12),
        ("TOPPADDING",   (0,0), (-1,-1), 10),
        ("BOTTOMPADDING",(0,0), (-1,-1), 10),
        ("ROUNDEDCORNERS", [6]),
    ]))
    story.append(header_tbl)
    story.append(Spacer(1, 5*mm))

    # ── Employee + Payment info ───────────────────────────────
    def info_row(label, value):
        return [
            Paragraph(label, norm9),
            Paragraph(str(value) if value else "—", norm10),
        ]

    emp_data = [
        [Paragraph("EMPLOYEE DETAILS", navy9), "", Paragraph("PAYMENT DETAILS", navy9), ""],
        info_row("Employee ID",  emp.get("emp_id") or "—") +
        info_row("Bank",         emp.get("bank_name") or "—"),
        info_row("Name",         emp.get("employee_name") or "—") +
        info_row("Account No.",  emp.get("bank_account") or "—"),
        info_row("Designation",  emp.get("designation") or "—") +
        info_row("IFSC Code",    emp.get("bank_ifsc") or "—"),
        info_row("Department",   emp.get("department") or "—") +
        info_row("PAN",          emp.get("pan_number") or "—"),
        info_row("Branch",       emp.get("branch") or "—") +
        info_row("Pay Period",   f"{month_name} {year}"),
    ]
    cw = W / 4
    info_tbl = Table(emp_data, colWidths=[cw*0.55, cw*0.95, cw*0.55, cw*0.95])
    info_tbl.setStyle(TableStyle([
        ("SPAN",        (0,0), (1,0)),
        ("SPAN",        (2,0), (3,0)),
        ("BACKGROUND",  (0,0), (-1,0), LIGHT),
        ("TOPPADDING",  (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0),(-1,-1), 4),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("LINEBELOW",   (0,0), (-1,-2), 0.5, colors.HexColor("#E5E7EB")),
        ("LINEBEFORE",  (2,0), (2,-1), 0.5, colors.HexColor("#E5E7EB")),
        ("BOX",         (0,0), (-1,-1), 0.5, colors.HexColor("#E5E7EB")),
    ]))
    story.append(info_tbl)
    story.append(Spacer(1, 4*mm))

    # ── Attendance boxes ──────────────────────────────────────
    att_data = [[
        Paragraph(f"<b>{cal}</b>",     style("ac", fontSize=18, textColor=NAVY, fontName="Helvetica-Bold", alignment=TA_CENTER)),
        Paragraph(f"<b>{present}</b>", style("ap", fontSize=18, textColor=colors.HexColor("#10B981"), fontName="Helvetica-Bold", alignment=TA_CENTER)),
        Paragraph(f"<b>{absent}</b>",  style("aa", fontSize=18, textColor=colors.HexColor("#EF4444"), fontName="Helvetica-Bold", alignment=TA_CENTER)),
        Paragraph(f"<b>{fmt(lop)}</b>",style("al", fontSize=14, textColor=colors.HexColor("#F59E0B"), fontName="Helvetica-Bold", alignment=TA_CENTER)),
    ], [
        Paragraph("Total Days",    centg),
        Paragraph("Present",       centg),
        Paragraph("Absent",        centg),
        Paragraph("LOP Deduction", centg),
    ]]
    att_tbl = Table(att_data, colWidths=[W/4]*4)
    att_tbl.setStyle(TableStyle([
        ("BOX",          (0,0), (-1,-1), 0.5, colors.HexColor("#E5E7EB")),
        ("LINEBEFORE",   (1,0), (-1,-1), 0.5, colors.HexColor("#E5E7EB")),
        ("TOPPADDING",   (0,0), (-1,-1), 8),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
        ("BACKGROUND",   (0,0), (-1,-1), LGREY),
    ]))
    story.append(att_tbl)
    story.append(Spacer(1, 4*mm))

    # ── Earnings / Deductions table ───────────────────────────
    sal_header = [
        Paragraph("Earnings",        white10),
        Paragraph("Amount",          style("wamt", fontSize=10, textColor=WHITE, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
        Paragraph("Deductions",      white10),
        Paragraph("Amount",          style("wamt2", fontSize=10, textColor=WHITE, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
    ]
    def earn_row(label, val, ded_label="", ded_val=None):
        return [
            Paragraph(label, norm10),
            Paragraph(fmt(val), style("ra", fontSize=10, textColor=BLACK, alignment=TA_RIGHT)),
            Paragraph(ded_label, norm10),
            Paragraph(fmt(ded_val) if ded_val is not None else "", style("ra2", fontSize=10, textColor=BLACK, alignment=TA_RIGHT)),
        ]

    sal_data = [
        sal_header,
        earn_row("Basic Salary",          basic,  "Loss of Pay (LOP)",   lop),
        earn_row("HRA",                   hra,    "Provident Fund (PF)", pf),
        earn_row("Conveyance Allowance",  ca,     "ESI",                 esi),
        earn_row("Special Allowance",     sa,     "",                    None),
        [
            Paragraph("Gross Salary", bold10),
            Paragraph(fmt(gross), style("rgb", fontSize=10, textColor=NAVY, fontName="Helvetica-Bold", alignment=TA_RIGHT)),
            Paragraph("Total Deductions", bold10),
            Paragraph(fmt(total_ded), style("rdb", fontSize=10, textColor=colors.HexColor("#EF4444"), fontName="Helvetica-Bold", alignment=TA_RIGHT)),
        ],
    ]
    cw2 = W / 4
    sal_tbl = Table(sal_data, colWidths=[cw2*1.1, cw2*0.9, cw2*1.1, cw2*0.9])
    sal_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,0),  NAVY),
        ("BACKGROUND",   (0,-1),(-1,-1), LIGHT),
        ("LINEBELOW",    (0,0), (-1,-2), 0.5, colors.HexColor("#E5E7EB")),
        ("LINEBEFORE",   (2,0), (2,-1),  0.5, colors.HexColor("#E5E7EB")),
        ("BOX",          (0,0), (-1,-1), 0.5, colors.HexColor("#E5E7EB")),
        ("TOPPADDING",   (0,0), (-1,-1), 5),
        ("BOTTOMPADDING",(0,0), (-1,-1), 5),
        ("LEFTPADDING",  (0,0), (-1,-1), 8),
        ("ROWBACKGROUNDS",(0,1),(-1,-2), [WHITE, LGREY]),
    ]))
    story.append(sal_tbl)
    story.append(Spacer(1, 4*mm))

    # ── Net pay bar ───────────────────────────────────────────
    net_data = [[
        Paragraph("NET PAY (TAKE HOME)", white10),
        Paragraph(fmt(net), white14),
    ]]
    net_tbl = Table(net_data, colWidths=[W*0.5, W*0.5])
    net_tbl.setStyle(TableStyle([
        ("BACKGROUND",   (0,0), (-1,-1), NAVY),
        ("TOPPADDING",   (0,0), (-1,-1), 12),
        ("BOTTOMPADDING",(0,0), (-1,-1), 12),
        ("LEFTPADDING",  (0,0), (0,0),   12),
        ("RIGHTPADDING", (1,0), (1,0),   12),
        ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(net_tbl)
    story.append(Spacer(1, 5*mm))

    # ── Footer ────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#E5E7EB")))
    story.append(Spacer(1, 2*mm))
    story.append(Paragraph(
        "This is a computer-generated payslip and does not require a signature.  |  Generated by GodavariKrishna HRMS",
        style("ft", fontSize=8, textColor=GREY, alignment=TA_CENTER)
    ))

    doc.build(story)
    buf.seek(0)
    return buf.read()


# ══════════════════════════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════════════════════════

@router.get("/export")
async def export_payroll(
    year:         int            = Query(..., ge=2020, le=2100),
    month:        int            = Query(..., ge=1,    le=12),
    branch_id:    Optional[int]  = Query(None),
    employee_ids: Optional[str]  = Query(None, description="Comma-separated employee IDs"),
    _hr: dict    = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Export pre-filled payroll Excel for the given month.
    HR cross-checks, corrects if needed, then uploads back via /process.

    Query params:
      year, month      — payroll period
      branch_id        — filter by branch (optional)
      employee_ids     — comma-separated employee IDs (optional, overrides branch_id)
    """
    # Parse employee_ids
    emp_id_list = None
    if employee_ids:
        try:
            emp_id_list = [int(x.strip()) for x in employee_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "employee_ids must be comma-separated integers")

    employees = await _fetch_employees(db, year, month, branch_id, emp_id_list)

    if not employees:
        raise HTTPException(404, "No employees found for the given filters")

    wb = _build_export_workbook(employees, year, month)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    filename = f"payroll_{year}_{month:02d}.xlsx"
    logger.info(
        "Payroll export: year=%d month=%d employees=%d by hr=%s",
        year, month, len(employees), _hr.get("id"),
    )

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/process")
async def process_payroll(
    year:  int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1,    le=12),
    file:  UploadFile = File(...),
    _hr: dict = Depends(require_hr),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Accept the corrected payroll Excel, recalculate using HR's values, and return:
      - A ZIP file containing:
          payroll_summary_YYYY_MM.xlsx  — full summary sheet
          payslips/EMP-XXXX.pdf        — individual PDF payslip per employee

    Excel wins over DB — whatever HR has put in the sheet is used as-is.
    Recalculates: net_pay = gross - lop - pf_deduction - esi_deduction
    """
    if not file.filename.endswith(".xlsx"):
        raise HTTPException(400, "Only .xlsx files accepted")

    content = await file.read()
    try:
        wb_in = load_workbook(io.BytesIO(content), data_only=True)
    except Exception as e:
        raise HTTPException(400, f"Invalid Excel file: {e}")

    ws = wb_in.active
    rows = list(ws.iter_rows(values_only=True))

    if len(rows) < 2:
        raise HTTPException(400, "Excel has no data rows")

    # Validate header matches expected COLUMNS
    actual_headers = [str(h).strip() if h else "" for h in rows[0]]
    expected_headers = HEADERS
    if actual_headers != expected_headers:
        raise HTTPException(
            400,
            f"Excel header mismatch. Expected columns: {expected_headers}. "
            f"Got: {actual_headers}"
        )

    processed = []
    errors    = []

    for row_num, row in enumerate(rows[1:], start=2):
        # Skip rows with no employee name (empty/legend rows)
        # emp_id can be NULL in DB so don't use it as the skip check
        if not row[1]:  # employee_name is col index 1
            continue

        emp = dict(zip(COLUMNS, row))

        # Basic validation
        try:
            gross        = Decimal(str(emp.get("gross") or 0))
            lop          = Decimal(str(emp.get("lop")   or 0))
            pf           = Decimal(str(emp.get("pf_deduction")  or 0))
            esi          = Decimal(str(emp.get("esi_deduction") or 0))
            present_days = int(emp.get("present_days") or 0)
            absent_days  = int(emp.get("absent_days")  or 0)
            cal_days     = int(emp.get("calendar_days") or calendar.monthrange(year, month)[1])
        except (ValueError, TypeError) as e:
            errors.append(f"Row {row_num} ({emp.get('emp_id')}): invalid number — {e}")
            continue

        # Sanity checks
        if present_days + absent_days > cal_days:
            errors.append(
                f"Row {row_num} ({emp.get('emp_id')}): "
                f"present ({present_days}) + absent ({absent_days}) > calendar days ({cal_days})"
            )
            continue

        if lop < 0 or pf < 0 or esi < 0:
            errors.append(f"Row {row_num} ({emp.get('emp_id')}): deductions cannot be negative")
            continue

        # Recalculate net pay from HR's numbers (Excel is truth)
        net_pay = _round2(gross - lop - pf - esi)

        emp["gross"]          = gross
        emp["lop"]            = lop
        emp["pf_deduction"]   = pf
        emp["esi_deduction"]  = esi
        emp["net_pay"]        = net_pay
        emp["present_days"]   = present_days
        emp["absent_days"]    = absent_days
        emp["calendar_days"]  = cal_days

        processed.append(emp)

    if errors:
        raise HTTPException(422, {"message": "Validation errors in Excel", "errors": errors})

    if not processed:
        raise HTTPException(400, "No valid employee rows found in Excel")

    # ── Build output ZIP ──────────────────────────────────────
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:

        # 1. Summary Excel
        summary_wb = _build_summary_workbook(processed, year, month)
        summary_buf = io.BytesIO()
        summary_wb.save(summary_buf)
        zf.writestr(f"payroll_summary_{year}_{month:02d}.xlsx", summary_buf.getvalue())

        # 2. Individual payslips (HTML → print as PDF)
        for emp in processed:
            payslip_bytes = _generate_payslip_pdf(emp, year, month)
            safe_name = str(emp.get("emp_id") or emp.get("employee_name") or "unknown").replace("/", "-")
            zf.writestr(f"payslips/{safe_name}.pdf", payslip_bytes)

    zip_buf.seek(0)
    filename = f"payroll_{year}_{month:02d}_processed.zip"

    logger.info(
        "Payroll processed: year=%d month=%d employees=%d by hr=%s",
        year, month, len(processed), _hr.get("id"),
    )

    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_summary_workbook(employees: list[dict], year: int, month: int) -> Workbook:
    """Build the final payroll summary Excel (same format as export)."""
    wb = Workbook()
    ws = wb.active
    ws.title = f"Summary {year}-{month:02d}"

    editable_fill = PatternFill("solid", fgColor="FFFFFF")
    locked_fill   = PatternFill("solid", fgColor="F8F9FC")

    _style_header(ws, len(COLUMNS))

    for emp in employees:
        row_data = []
        for col in COLUMNS:
            val = emp.get(col)
            if isinstance(val, Decimal):
                val = float(val)
            row_data.append(val)
        ws.append(row_data)

    for row_idx in range(2, len(employees) + 2):
        _style_data_row(ws, row_idx, len(COLUMNS), editable_fill, locked_fill)

    # Totals row
    total_row = len(employees) + 2
    ws.cell(row=total_row, column=1, value="TOTAL")
    ws.cell(row=total_row, column=1).font = Font(bold=True)

    currency_col_indices = [i + 1 for i, col in enumerate(COLUMNS) if col in CURRENCY_COLS]
    for col_idx in currency_col_indices:
        col_letter = get_column_letter(col_idx)
        ws.cell(
            row=total_row, column=col_idx,
            value=f"=SUM({col_letter}2:{col_letter}{total_row - 1})",
        ).font = Font(bold=True)

    ws.freeze_panes = "A2"
    return wb