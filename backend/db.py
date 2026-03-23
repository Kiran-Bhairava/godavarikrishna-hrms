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
        # Hard limit per query — prevents slow Neon queries from hanging forever
        command_timeout=30,
        # Release idle connections after 5 min — keeps Neon free tier connection
        # count low during off-hours
        max_inactive_connection_lifetime=300,
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
    try:
        # Fail fast if all 20 connections are busy — don't let requests pile up
        async with db_pool.acquire(timeout=10) as conn:
            yield conn
    except asyncpg.exceptions.TooManyConnectionsError:
        raise HTTPException(503, "Database busy, please retry")
    except TimeoutError:
        raise HTTPException(503, "Database connection timeout, please retry")