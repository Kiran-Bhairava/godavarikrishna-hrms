"""
Routers package for HRMS API
"""
from .regularization import router as regularization_router
from .leave import router as leave_router
from .payroll import router as payroll_router
from .sandwich import router as sandwich_router

__all__ = ["regularization_router", "leave_router", "payroll_router", "sandwich_router"]