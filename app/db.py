"""SQLite connection helpers and schema initialization."""
import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "editor.db"))


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS scripts (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                content    TEXT NOT NULL DEFAULT '{"start_node_id": null, "nodes": []}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shares (
                token      TEXT PRIMARY KEY,
                script_id  INTEGER,
                script_name TEXT NOT NULL,
                content    TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                mode            TEXT NOT NULL,            -- 'script' | 'share'
                script_id       INTEGER,
                share_token     TEXT,
                current_node_id TEXT,
                steps           TEXT NOT NULL DEFAULT '[]',
                finished        INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL
            );
            """
        )
