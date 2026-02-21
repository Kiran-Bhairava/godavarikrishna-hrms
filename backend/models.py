"""
models.py — SQLAlchemy ORM definitions
Alembic reads this to autogenerate migrations.
The app itself uses asyncpg for all queries.

Tables:
  users            — auth only (email, password, role, active)
  branches         — office locations with geofence
  employees        — HR profile: one per user, all people-data lives here
  attendance_logs  — every punch-in / punch-out
  daily_summary    — aggregated per-user per-day rollup
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    Column, Integer, String, Text, Boolean,
    Numeric, Date, Time, TIMESTAMP,
    CheckConstraint, ForeignKey, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, relationship

convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Base = declarative_base(metadata=sa.MetaData(naming_convention=convention))


# ── branches ──────────────────────────────────────────────────
class Branch(Base):
    __tablename__ = "branches"

    id            = Column(Integer, primary_key=True)
    name          = Column(String(150), nullable=False)
    city          = Column(String(80),  nullable=False)
    address       = Column(Text)
    latitude      = Column(Numeric(10, 7), nullable=False)
    longitude     = Column(Numeric(10, 7), nullable=False)
    radius_meters = Column(Integer, nullable=False, server_default="200")
    is_active     = Column(Boolean, server_default="true")
    created_at    = Column(TIMESTAMP(timezone=True), server_default=sa.func.now())

    attendance_logs = relationship("AttendanceLog", back_populates="branch")


Index("idx_branches_active", Branch.is_active)


# ── users  (auth only — keep it lean) ─────────────────────────
class User(Base):
    __tablename__ = "users"

    id            = Column(Integer, primary_key=True)
    email         = Column(String(180), nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    full_name     = Column(String(120), nullable=False)
    role          = Column(String(20),  nullable=False, server_default="employee")
    is_active     = Column(Boolean, server_default="true")
    created_at    = Column(TIMESTAMP(timezone=True), server_default=sa.func.now())
    last_login    = Column(TIMESTAMP(timezone=True))

    employee        = relationship("Employee", back_populates="user", uselist=False)
    attendance_logs = relationship("AttendanceLog", back_populates="user")
    daily_summaries = relationship("DailySummary", back_populates="user")

    __table_args__ = (
        CheckConstraint("role IN ('employee','hr','admin')", name="role_values"),
    )


Index("idx_users_email",  User.email)
Index("idx_users_active", User.is_active)


# ── employees  (HR profile — one row per user) ─────────────────
class Employee(Base):
    """
    All people-data in one place.
    Linked 1-to-1 to users via user_id.

    L1 / L2 managers are self-referential FKs back to employees.
    Any role can be a manager — no restriction.
    """
    __tablename__ = "employees"

    id      = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True)

    # ── Identity ──────────────────────────────────────────────
    emp_id         = Column(String(20), unique=True)          # EMP-00042
    phone          = Column(String(20))
    personal_email = Column(String(180))
    dob            = Column(Date)
    gender         = Column(String(20))
    blood_group    = Column(String(5))
    nationality    = Column(String(60))
    home_address   = Column(Text)

    # ── Job ───────────────────────────────────────────────────
    branch_id      = Column(Integer, ForeignKey("branches.id", ondelete="SET NULL"))
    job_title      = Column(String(120))
    designation    = Column(String(120))
    department     = Column(String(80))
    sub_department = Column(String(80))
    grade          = Column(String(30))        # e.g. "L3 - Senior"
    date_of_joining = Column(Date)
    cost_centre    = Column(String(40))

    # ── Reporting (L1 = direct, L2 = skip-level) ──────────────
    # Both point to employees.id — any role can be a manager
    l1_manager_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"))
    l2_manager_id = Column(Integer, ForeignKey("employees.id", ondelete="SET NULL"))

    # ── Employment terms ──────────────────────────────────────
    employment_type = Column(String(30))   # Full-Time / Part-Time / Contract / Intern
    contract_end    = Column(Date)
    probation_end   = Column(Date)
    notice_period   = Column(String(20))   # "30 Days"

    # ── Work schedule ─────────────────────────────────────────
    shift_start = Column(Time, nullable=False, server_default="09:00")
    shift_end   = Column(Time, nullable=False, server_default="18:00")
    work_mode   = Column(String(20), server_default="On-Site")
    weekly_off  = Column(String(40), server_default="Saturday & Sunday")
    work_location = Column(String(80))
    asset_id      = Column(String(40))

    # ── Emergency contact ─────────────────────────────────────
    emg_name  = Column(String(120))
    emg_phone = Column(String(20))
    emg_rel   = Column(String(40))

    # ── Compensation ──────────────────────────────────────────
    annual_ctc      = Column(Numeric(14, 2))
    pay_frequency   = Column(String(20), server_default="Monthly")
    pf_enrolled     = Column(Boolean, server_default="true")
    esic_applicable = Column(Boolean, server_default="true")

    # ── Bank / payroll ────────────────────────────────────────
    bank_name    = Column(String(100))
    bank_account = Column(String(30))
    bank_ifsc    = Column(String(15))
    pan_number   = Column(String(10))

    # ── Onboarding ────────────────────────────────────────────
    onboarding_status = Column(String(20), nullable=False, server_default="awaiting")

    created_at = Column(TIMESTAMP(timezone=True), server_default=sa.func.now())
    updated_at = Column(TIMESTAMP(timezone=True), server_default=sa.func.now(),
                        onupdate=sa.func.now())

    # ── Relationships ─────────────────────────────────────────
    user   = relationship("User",   back_populates="employee")
    branch = relationship("Branch")
    l1     = relationship("Employee", foreign_keys=[l1_manager_id], remote_side="Employee.id")
    l2     = relationship("Employee", foreign_keys=[l2_manager_id], remote_side="Employee.id")

    __table_args__ = (
        CheckConstraint(
            "onboarding_status IN ('awaiting','in-progress','completed')",
            name="ob_status_values",
        ),
    )


Index("idx_employees_user",   Employee.user_id)
Index("idx_employees_branch", Employee.branch_id)
Index("idx_employees_dept",   Employee.department)
Index("idx_employees_l1",     Employee.l1_manager_id)
Index("idx_employees_l2",     Employee.l2_manager_id)
Index("idx_employees_ob",     Employee.onboarding_status)


# ── attendance_logs ───────────────────────────────────────────
class AttendanceLog(Base):
    __tablename__ = "attendance_logs"

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("users.id",    ondelete="CASCADE"),  nullable=False)
    branch_id       = Column(Integer, ForeignKey("branches.id", ondelete="SET NULL"))
    punch_type      = Column(String(10), nullable=False)
    punched_at      = Column(TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    latitude        = Column(Numeric(10, 7), nullable=False)
    longitude       = Column(Numeric(10, 7), nullable=False)
    distance_meters = Column(Integer, nullable=False, server_default="0")
    is_valid        = Column(Boolean, server_default="true")

    user   = relationship("User",   back_populates="attendance_logs")
    branch = relationship("Branch", back_populates="attendance_logs")

    __table_args__ = (
        CheckConstraint("punch_type IN ('in','out')", name="punch_type_values"),
    )


Index("idx_attendance_user",   AttendanceLog.user_id)
Index("idx_attendance_branch", AttendanceLog.branch_id)
Index("idx_attendance_punched_at", AttendanceLog.punched_at)


# ── daily_summary ─────────────────────────────────────────────
class DailySummary(Base):
    __tablename__ = "daily_summary"

    id              = Column(Integer, primary_key=True)
    user_id         = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    work_date       = Column(Date, nullable=False)
    first_punch_in  = Column(TIMESTAMP(timezone=True))
    last_punch_out  = Column(TIMESTAMP(timezone=True))
    total_minutes   = Column(Integer, server_default="0")
    is_late         = Column(Boolean, server_default="false")
    late_by_minutes = Column(Integer, server_default="0")
    status          = Column(String(20), server_default="present")

    user = relationship("User", back_populates="daily_summaries")

    __table_args__ = (
        UniqueConstraint("user_id", "work_date", name="uq_daily_summary_user_date"),
        CheckConstraint("status IN ('present','leave')", name="status_values"),
    )


Index("idx_summary_user_date", DailySummary.user_id, DailySummary.work_date)