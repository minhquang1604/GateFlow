"""Database session factory and engine management."""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from mlops_framework.config.settings import get_settings
from mlops_framework.database.base import Base


class DatabaseManager:
    """Manages database engine and session lifecycle."""

    def __init__(self, database_url: str | None = None) -> None:
        """Initialize database manager.

        Args:
            database_url: Optional database URL override. If not provided,
                         uses settings.database_url.
        """
        settings = get_settings()
        self._database_url = database_url or settings.database_url
        self._engine: Engine | None = None
        self._session_factory: sessionmaker[Session] | None = None

    @property
    def engine(self) -> Engine:
        """Get or create the database engine."""
        if self._engine is None:
            settings = get_settings()
            self._engine = create_engine(
                self._database_url,
                echo=settings.database_echo,
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
                pool_timeout=settings.database_pool_timeout,
            )
        return self._engine

    @property
    def session_factory(self) -> sessionmaker[Session]:
        """Get or create the session factory."""
        if self._session_factory is None:
            self._session_factory = sessionmaker(
                bind=self.engine,
                expire_on_commit=False,
            )
        return self._session_factory

    def create_all(self) -> None:
        """Create all tables (for testing purposes)."""
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        """Drop all tables (for testing purposes)."""
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """Get a database session with automatic cleanup.

        Yields:
            Session: Database session

        Example:
            with db_manager.get_session() as session:
                session.query(Dataset).all()
        """
        session = self.session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        """Dispose of the engine and close all connections."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
        self._session_factory = None


# Global database manager instance
_db_manager: DatabaseManager | None = None


def get_db_manager() -> DatabaseManager:
    """Get the global database manager instance.

    Returns:
        DatabaseManager: The global database manager
    """
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager


def set_db_manager(manager: DatabaseManager) -> None:
    """Set the global database manager instance.

    Args:
        manager: Database manager to set as global
    """
    global _db_manager
    _db_manager = manager


def get_session() -> Generator[Session, None, None]:
    """Get a database session using the global database manager.

    Yields:
        Session: Database session
    """
    db_manager = get_db_manager()
    yield from db_manager.get_session()
