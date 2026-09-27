"""Database connection and manager for SQLite."""

from contextlib import contextmanager
import logging
import os
import sqlite3
import threading
from typing import Generator, Optional

from piso_wifi.config import AppConfig

logger = logging.getLogger(__name__)


class DatabaseManager:
    """Thread-safe SQLite database manager and connection provider.

    Supports file-based SQLite databases as well as in-memory (:memory:)
    instances with PRAGMA foreign_keys = ON and sqlite3.Row factory.
    """

    def __init__(self, db_path: Optional[str] = None):
        """Initialize the DatabaseManager.

        Args:
            db_path: Path to the SQLite database file, or ':memory:'.
                     If None, defaults to AppConfig().db_path.
        """
        if db_path is None:
            db_path = AppConfig().db_path

        self.db_path = db_path
        self._is_memory = (self.db_path == ":memory:" or "mode=memory" in self.db_path)
        self._lock = threading.RLock()
        self._local = threading.local()
        self._mem_conn: Optional[sqlite3.Connection] = None

        if not self._is_memory:
            parent_dir = os.path.dirname(self.db_path)
            if parent_dir and not os.path.exists(parent_dir):
                try:
                    os.makedirs(parent_dir, exist_ok=True)
                except OSError as e:
                    logger.warning(f"Could not create database directory {parent_dir}: {e}")

    def _configure_connection(self, conn: sqlite3.Connection) -> None:
        """Apply required pragmas and row_factory to connection."""
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")

    def _get_memory_connection(self) -> sqlite3.Connection:
        """Get or initialize the persistent in-memory connection."""
        if self._mem_conn is None:
            self._mem_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._configure_connection(self._mem_conn)
        return self._mem_conn

    @contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Provide a thread-safe connection context manager.

        Automatically commits on successful block exit, rolls back on
        exceptions, and closes non-memory connections.

        Yields:
            sqlite3.Connection: Configured connection with Row factory and FKs enabled.
        """
        with self._lock:
            if self._is_memory:
                conn = self._get_memory_connection()
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            else:
                conn = sqlite3.connect(self.db_path, timeout=30.0)
                self._configure_connection(conn)
                try:
                    yield conn
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
                finally:
                    conn.close()

    # Alias for get_connection
    connection = get_connection

    def __enter__(self) -> sqlite3.Connection:
        """Enter context manager directly on DatabaseManager instance."""
        cm = self.get_connection()
        if not hasattr(self._local, "stack"):
            self._local.stack = []
        self._local.stack.append(cm)
        return cm.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager on DatabaseManager instance."""
        if hasattr(self._local, "stack") and self._local.stack:
            cm = self._local.stack.pop()
            return cm.__exit__(exc_type, exc_val, exc_tb)

    def close(self) -> None:
        """Close any persistent connections."""
        with self._lock:
            if self._mem_conn is not None:
                self._mem_conn.close()
                self._mem_conn = None
