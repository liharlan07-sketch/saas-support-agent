"""Explicit SQLite transactions. Importing this module performs no I/O."""

from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'disabled'))
);
CREATE TABLE IF NOT EXISTS workspaces (
    workspace_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'suspended')),
    plan_code TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1)
);
CREATE TABLE IF NOT EXISTS memberships (
    membership_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
    user_id TEXT NOT NULL REFERENCES users(user_id),
    role TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    status TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    version INTEGER NOT NULL CHECK (version >= 1),
    UNIQUE (workspace_id, user_id)
);
CREATE TABLE IF NOT EXISTS notification_preferences (
    preference_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    notification_type TEXT NOT NULL CHECK (notification_type IN ('task_assigned', 'comment_mentioned')),
    channel TEXT NOT NULL CHECK (channel IN ('email', 'in_app')),
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    version INTEGER NOT NULL CHECK (version >= 1),
    FOREIGN KEY (workspace_id, user_id) REFERENCES memberships(workspace_id, user_id),
    UNIQUE (workspace_id, user_id, notification_type, channel)
);
CREATE TABLE IF NOT EXISTS operation_receipts (
    receipt_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    pending_action_id TEXT NOT NULL,
    action_revision INTEGER NOT NULL CHECK (action_revision >= 1),
    action_type TEXT NOT NULL CHECK (action_type = 'update_notification_preferences'),
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('succeeded', 'failed')),
    result_snapshot TEXT NOT NULL,
    error_code TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (workspace_id, user_id) REFERENCES memberships(workspace_id, user_id),
    UNIQUE (workspace_id, user_id, operation_id)
);
"""


class Database:
    def __init__(self, path: str | Path, *, timeout: float = 2.0):
        # Each call gets a new connection. In-memory databases would lose state.
        if str(path) == ":memory:":
            raise ValueError("Use a file-backed SQLite database, including in tests")
        self.path = Path(path)
        self.timeout = timeout

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout, autocommit=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        """Explicit bootstrap for a new database; no fixtures or credentials."""
        connection = self.connect()
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        finally:
            connection.close()

    @staticmethod
    def commit(connection: sqlite3.Connection) -> None:
        # With autocommit=True, connection.commit() is a no-op. Use explicit SQL.
        connection.execute("COMMIT")
