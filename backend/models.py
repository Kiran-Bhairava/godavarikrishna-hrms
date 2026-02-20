from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Boolean,
    Numeric,
    Date,
    Time,
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import declarative_base, relationship

# Naming convention for Alembic
convention = {
    "ix":  "ix_%(column_0_label)s",
    "uq":  "uq_%(table_name)s_%(column_0_name)s",
    "ck":  "ck_%(table_name)s_%(constraint_name)s",
    "fk":  "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk":  "pk_%(table_name)s",
}

Base = declarative_base(metadata=sa.MetaData(naming_convention=convention))


# ───────────────── branches ─────────────────
class Branch(Base):
    __tablename__ = "branches"

    id = Column(Integer, primary_key=True)
    name = Column(String(150), nullable=False)
    city = Column(String(80), nullable=False)
    address = Column(Text)
    latitude = Column(Numeric(10, 7), nullable=False)
    longitude = Column(Numeric(10, 7), nullable=False)
    radius_meters = Column(Integer, nullable=False, server_default="200")
    is_active = Column(Boolean, server_default="true")
    created_at = Column(TIMESTAMP(timezone=True), server_default=sa.func.now())

    users = relationship("User", back_populates="branch")
    attendance_logs = relationship("AttendanceLog", back_populates="branch")


Index("idx_branches_active", Branch.is_active)


# ───────────────── users ─────────────────
class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    email = Column(String(180), nullable=False, unique=True)
    password_hash = Column(Text, nullable=False)
    full_name = Column(String(120), nullable=False)
    role = Column(String(20), nullable=False, server_default="employee")
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="SET NULL"))
    shift_start = Column(Time, nullable=False, server_default="09:00")
    shift_end = Column(Time, nullable=False, server_default="18:00")
    is_active = Column(Boolean, server_default="true")
    created_at = Column(TIMESTAMP(timezone=True), server_default=sa.func.now())
    last_login = Column(TIMESTAMP(timezone=True))

    branch = relationship("Branch", back_populates="users")
    attendance_logs = relationship("AttendanceLog", back_populates="user")
    summaries = relationship("DailySummary", back_populates="user")

    __table_args__ = (
        CheckConstraint("role IN ('employee','hr','admin')", name="role_values"),
    )


Index("idx_users_email", User.email)
Index("idx_users_branch", User.branch_id)
Index("idx_users_active", User.is_active)


# ───────────────── attendance_logs ─────────────────
class AttendanceLog(Base):
    __tablename__ = "attendance_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    branch_id = Column(Integer, ForeignKey("branches.id", ondelete="SET NULL"))
    punch_type = Column(String(10), nullable=False)
    punched_at = Column(TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now())
    latitude = Column(Numeric(10, 7), nullable=False)
    longitude = Column(Numeric(10, 7), nullable=False)
    distance_meters = Column(Integer, nullable=False, server_default="0")
    is_valid = Column(Boolean, server_default="true")

    user = relationship("User", back_populates="attendance_logs")
    branch = relationship("Branch", back_populates="attendance_logs")

    __table_args__ = (
        CheckConstraint("punch_type IN ('in','out')", name="punch_type_values"),
    )


Index("idx_attendance_user", AttendanceLog.user_id)
Index("idx_attendance_branch", AttendanceLog.branch_id)


# ───────────────── daily_summary ─────────────────
class DailySummary(Base):
    __tablename__ = "daily_summary"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    work_date = Column(Date, nullable=False)
    first_punch_in = Column(TIMESTAMP(timezone=True))
    last_punch_out = Column(TIMESTAMP(timezone=True))
    total_minutes = Column(Integer, server_default="0")
    is_late = Column(Boolean, server_default="false")
    late_by_minutes = Column(Integer, server_default="0")
    status = Column(String(20), server_default="present")

    user = relationship("User", back_populates="summaries")

    __table_args__ = (
        UniqueConstraint("user_id", "work_date", name="uq_daily_summary_user_date"),
        CheckConstraint("status IN ('present','leave')", name="status_values"),
    )


Index("idx_summary_date", DailySummary.work_date)
Index("idx_summary_user", DailySummary.user_id)