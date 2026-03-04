"""Database connection pooling and lifecycle management."""
import asyncpg
from typing import Optional, AsyncGenerator
from fastapi import HTTPException

from config import settings

db_pool: Optional[asyncpg.Pool] = None


async def init_db():
    """Initialize database connection pool on app startup."""
    global db_pool
    db_pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
    print("✓ Database pool initialized")


async def close_db():
    """Close database connection pool on app shutdown."""
    if db_pool:
        await db_pool.close()
        print("✓ Database pool closed")


async def get_db() -> AsyncGenerator[asyncpg.Connection, None]:
    """Dependency to get database connection for endpoints."""
    if db_pool is None:
        raise HTTPException(503, "Database not available")
    async with db_pool.acquire() as conn:
        yield conn