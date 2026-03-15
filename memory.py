"""Persistent SQLite memory for conversations, tasks, and code snippets."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "memory.db")


def _get_conn() -> sqlite3.Connection:
    """Get a database connection with row factory."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize the database and create tables if they don't exist."""
    os.makedirs(DB_DIR, exist_ok=True)

    conn = _get_conn()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            prompt TEXT NOT NULL,
            project_name TEXT,
            model_used TEXT,
            status TEXT DEFAULT 'pending',
            output_path TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS snippets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag TEXT NOT NULL,
            language TEXT,
            content TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Create indexes for faster lookups
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_conversations_chat_id
        ON conversations(chat_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_tasks_chat_id
        ON tasks(chat_id)
    """)

    conn.commit()
    conn.close()


def add_message(chat_id: str, role: str, content: str):
    """Store a conversation message."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO conversations (chat_id, role, content) VALUES (?, ?, ?)",
        (str(chat_id), role, content),
    )
    conn.commit()
    conn.close()


def get_history(chat_id: str, limit: int = 10) -> list[dict]:
    """Get recent conversation history for a chat, oldest first.

    Returns:
        List of dicts with 'role' and 'content' keys.
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT role, content FROM conversations
        WHERE chat_id = ?
        ORDER BY timestamp DESC, id DESC
        LIMIT ?
        """,
        (str(chat_id), limit),
    ).fetchall()
    conn.close()

    # Reverse to get chronological order (oldest first)
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def clear_history(chat_id: str):
    """Delete all conversation history for a chat."""
    conn = _get_conn()
    conn.execute("DELETE FROM conversations WHERE chat_id = ?", (str(chat_id),))
    conn.commit()
    conn.close()


def log_task(
    chat_id: str,
    prompt: str,
    project_name: str,
    model: str,
    status: str,
    output_path: str,
):
    """Log a build task."""
    conn = _get_conn()
    conn.execute(
        """
        INSERT INTO tasks (chat_id, prompt, project_name, model_used, status, output_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (str(chat_id), prompt, project_name, model, status, output_path),
    )
    conn.commit()
    conn.close()


def get_recent_tasks(chat_id: str, limit: int = 5) -> list[dict]:
    """Get recent build tasks for a chat.

    Returns:
        List of dicts with prompt, project, model, status, path, time keys.
    """
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT prompt, project_name, model_used, status, output_path, timestamp
        FROM tasks
        WHERE chat_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (str(chat_id), limit),
    ).fetchall()
    conn.close()

    return [
        {
            "prompt": row["prompt"],
            "project": row["project_name"],
            "model": row["model_used"],
            "status": row["status"],
            "path": row["output_path"],
            "time": row["timestamp"],
        }
        for row in rows
    ]


def save_snippet(tag: str, language: str, content: str):
    """Save a code snippet."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO snippets (tag, language, content) VALUES (?, ?, ?)",
        (tag, language, content),
    )
    conn.commit()
    conn.close()
