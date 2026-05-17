"""SQLite-backed task state registry.

Records each magnet processed by `pipeline.process_magnet` so /status and
/history can answer "what's running?" and "what failed?" across restarts.

One row per magnet. `stage` advances through: queued -> offline -> download ->
cleanup -> complete (or failed at any point). The /retry button on a failure
notification looks up the original magnet here.
"""
import logging
import os
import sqlite3
import threading
import time
import uuid

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'state.db')

STAGE_QUEUED = 'queued'
STAGE_OFFLINE = 'offline'      # PikPak offline downloading
STAGE_DOWNLOAD = 'download'    # Aria2 downloading
STAGE_CLEANUP = 'cleanup'      # deleting cloud files
STAGE_COMPLETE = 'complete'
STAGE_FAILED = 'failed'
STAGE_CANCELED = 'canceled'

TERMINAL_STAGES = (STAGE_COMPLETE, STAGE_FAILED, STAGE_CANCELED)

_lock = threading.Lock()
_conn = None


def _init_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            magnet TEXT,
            name TEXT,
            account TEXT,
            stage TEXT NOT NULL,
            progress INTEGER DEFAULT 0,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            completed_at REAL
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_stage ON tasks(stage)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_updated ON tasks(updated_at)')
    conn.commit()
    return conn


def _conn_ref():
    global _conn
    if _conn is None:
        _conn = _init_conn()
    return _conn


def create_task(magnet=None, name=None, account=None, stage=STAGE_QUEUED):
    task_id = uuid.uuid4().hex[:8]
    now = time.time()
    with _lock:
        c = _conn_ref()
        c.execute(
            'INSERT INTO tasks (task_id, magnet, name, account, stage, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (task_id, magnet, name, account, stage, now, now),
        )
        c.commit()
    return task_id


def update_task(task_id, **fields):
    if not fields or not task_id:
        return
    fields['updated_at'] = time.time()
    if fields.get('stage') in TERMINAL_STAGES and 'completed_at' not in fields:
        fields['completed_at'] = time.time()
    cols = ', '.join(f'{k} = ?' for k in fields)
    vals = list(fields.values()) + [task_id]
    with _lock:
        c = _conn_ref()
        c.execute(f'UPDATE tasks SET {cols} WHERE task_id = ?', vals)
        c.commit()


def get_task(task_id):
    with _lock:
        c = _conn_ref()
        row = c.execute('SELECT * FROM tasks WHERE task_id = ?', (task_id,)).fetchone()
    return dict(row) if row else None


def list_active():
    """Tasks that are still running (any non-terminal stage)."""
    placeholders = ', '.join('?' for _ in TERMINAL_STAGES)
    with _lock:
        c = _conn_ref()
        rows = c.execute(
            f'SELECT * FROM tasks WHERE stage NOT IN ({placeholders}) ORDER BY created_at',
            TERMINAL_STAGES,
        ).fetchall()
    return [dict(r) for r in rows]


def list_recent(limit=20, stage_filter=None):
    """Most recently updated tasks, optionally filtered by stage."""
    if stage_filter:
        sql = 'SELECT * FROM tasks WHERE stage = ? ORDER BY updated_at DESC LIMIT ?'
        args = (stage_filter, limit)
    else:
        sql = 'SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?'
        args = (limit,)
    with _lock:
        c = _conn_ref()
        rows = c.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def mark_failed_if_not_terminal(task_id, error_message):
    """Set stage=failed only if the task isn't already in a terminal stage.

    Used in `finally` blocks so we don't downgrade a recorded success.
    """
    if not task_id:
        return
    row = get_task(task_id)
    if not row or row['stage'] in TERMINAL_STAGES:
        return
    update_task(task_id, stage=STAGE_FAILED, error=error_message)
    logging.info(f"任務 {task_id} 因例外被標記為失敗: {error_message}")


def sweep_interrupted():
    """At boot, mark all non-terminal tasks as failed.

    process_magnet's `finally` block calls mark_failed_if_not_terminal, but
    that only runs if python can unwind the thread normally. systemctl
    restart sends SIGTERM and the threads die mid-execution, so any task
    that was in OFFLINE/DOWNLOAD/CLEANUP when the bot died stays that way
    in state.db forever. /status then shows phantom 'in progress' rows.

    Preserves each task's original updated_at as completed_at so a task
    interrupted weeks ago doesn't suddenly look like a 'recent failure' in
    the presence/history views.

    Returns the number of rows updated.
    """
    actives = list_active()
    if not actives:
        return 0
    for t in actives:
        completed_at = t.get('updated_at') or time.time()
        update_task(
            t['task_id'],
            stage=STAGE_FAILED,
            error='bot restarted while task was in flight',
            completed_at=completed_at,
        )
    return len(actives)
