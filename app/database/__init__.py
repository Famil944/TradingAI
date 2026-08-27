"""Database initialization and connection management."""

import aiosqlite
import sqlite3
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
import logging

logger = logging.getLogger(__name__)


class Database:
    """Database connection manager."""
    
    def __init__(self, db_url: str):
        self.db_url = db_url
        self.engine = None
        self.async_session_maker = None
    
    async def init(self):
        """Initialize async database engine."""
        self.engine = create_async_engine(
            self.db_url,
            echo=False,
            future=True,
            connect_args={"timeout": 10}
        )
        self.async_session_maker = sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False
        )
        await self.create_tables()
        logger.info("Database initialized successfully")
    
    async def create_tables(self):
        """Create all database tables."""
        from app.database.models import Base
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    
    async def get_session(self) -> AsyncSession:
        """Get async database session."""
        if self.async_session_maker is None:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self.async_session_maker()
    
    async def close(self):
        """Close database connection."""
        if self.engine:
            await self.engine.dispose()
            logger.info("Database connection closed")
