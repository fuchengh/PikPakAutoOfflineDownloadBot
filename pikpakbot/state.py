"""SQLite-backed task state registry.

Records each source and retry attempt so /status and
/history can answer "what's running?" and "what failed?" across restarts.

One row per attempt. `stage` advances through: queued -> offline -> download ->
cleanup -> complete (or failed at any point). The /retry button on a failure
notification looks up persisted source IDs and per-file state here.
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

# Additive migration: preserve every old row, including failures without IDs.
_EXTRA_COLUMNS = {
    'source_kind': "TEXT DEFAULT 'magnet'", 'offline_path': 'TEXT',
    'cloud_task_id': 'TEXT', 'cloud_file_id': 'TEXT',
    'retry_of': 'TEXT', 'retry_child': 'TEXT', 'retry_requested': 'INTEGER DEFAULT 0',
    'auto_retries': 'INTEGER DEFAULT 0', 'last_progress_at': 'REAL',
    'discord_channel_id': 'INTEGER', 'discord_message_id': 'INTEGER',
    'manifest_ready': 'INTEGER DEFAULT 0',
    'submission_started': 'INTEGER DEFAULT 0', 'interrupted_stage': 'TEXT',
    'last_retry_at': 'REAL',
    'retry_inflight': 'INTEGER DEFAULT 0',
}


class TaskConflict(ValueError):
    pass


UNCERTAIN_MESSAGE = 'PikPak 提交或重試結果尚未確認；請先檢查原任務，確認 file_id 後用 /dl 接續，禁止重新提交'


def uncertain_mutation(row):
    return bool(row and (row.get('retry_inflight') or
                (row.get('submission_started') and not row.get('cloud_task_id') and not row.get('cloud_file_id'))))


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
    columns = {r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
    for name, definition in _EXTRA_COLUMNS.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE tasks ADD COLUMN {name} {definition}')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cloud_task ON tasks(account, cloud_task_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS claims (
        resource TEXT PRIMARY KEY, task_id TEXT NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS resource_history (
        resource TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(resource, task_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS task_files (
        task_id TEXT NOT NULL, file_id TEXT NOT NULL, name TEXT NOT NULL,
        directory TEXT NOT NULL, gid TEXT, status TEXT NOT NULL DEFAULT 'pending',
        size INTEGER NOT NULL DEFAULT 0, error TEXT,
        PRIMARY KEY(task_id, file_id))''')
    file_columns = {r[1] for r in conn.execute('PRAGMA table_info(task_files)')}
    if 'destination_owned' not in file_columns:
        conn.execute('ALTER TABLE task_files ADD COLUMN destination_owned INTEGER NOT NULL DEFAULT 0')
    conn.execute('''CREATE TABLE IF NOT EXISTS observations (
        account TEXT NOT NULL, cloud_task_id TEXT NOT NULL,
        progress INTEGER NOT NULL, phase TEXT, changed_at REAL NOT NULL,
        seen_at REAL NOT NULL, PRIMARY KEY(account, cloud_task_id))''')
    conn.commit()
    return conn


def _conn_ref():
    global _conn
    if _conn is None:
        _conn = _init_conn()
    return _conn


def create_task(magnet=None, name=None, account=None, stage=STAGE_QUEUED, **fields):
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
    if fields:
        update_task(task_id, **fields)
    return task_id


def update_task(task_id, **fields):
    if not fields or not task_id:
        return
    allowed = set(_EXTRA_COLUMNS) | {'magnet', 'name', 'account', 'stage', 'progress',
                                     'error', 'completed_at', 'updated_at'}
    if not set(fields) <= allowed:
        raise ValueError('Unknown task field')
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

    The worker's `finally` block calls mark_failed_if_not_terminal, but
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
    for t in actives:
        completed_at = t.get('updated_at') or time.time()
        update_task(
            t['task_id'],
            stage=STAGE_FAILED,
            error='bot restarted while task was in flight',
            completed_at=completed_at,
            interrupted_stage=t['stage'],
        )
    with _lock:
        c = _conn_ref()
        for claim_row in c.execute('SELECT DISTINCT task_id FROM claims').fetchall():
            row = c.execute('SELECT * FROM tasks WHERE task_id=?', (claim_row[0],)).fetchone()
            row = dict(row) if row else None
            _release_claims(c, claim_row[0],
                            preserve_files=bool(row and row['stage'] != STAGE_COMPLETE),
                            preserve_mutation=uncertain_mutation(row))
        c.commit()
    return len(actives)


def claim(task_id, resource):
    """One owner per source, provider ID or output path, across all entry points."""
    with _lock:
        c = _conn_ref()
        row = c.execute('SELECT task_id FROM claims WHERE resource=?', (resource,)).fetchone()
        if row and row[0] != task_id:
            # Failed downloads can still be running in aria2 after an RPC outage.
            # Transfer their persistent source/file/path reservations only to a retry child.
            ancestor = task_id
            while ancestor:
                parent = c.execute('SELECT retry_of FROM tasks WHERE task_id=?', (ancestor,)).fetchone()
                ancestor = parent[0] if parent else None
                if ancestor == row[0] and resource.startswith(('path:', 'cloud-file:', 'source:')):
                    c.execute('UPDATE claims SET task_id=? WHERE resource=?', (task_id, resource))
                    break
            else:
                raise TaskConflict(f'此資源由任務 {row[0]} 處理中或保留供重試')
        c.execute('INSERT OR IGNORE INTO claims VALUES (?, ?)', (resource, task_id))
        if resource.startswith('cloud-task:'):
            c.execute('INSERT OR IGNORE INTO resource_history VALUES (?, ?)', (resource, task_id))
        c.commit()


def _release_claims(c, task_id, *, preserve_files, preserve_mutation):
    prefixes = []
    if preserve_files:
        prefixes.extend(('path:', 'cloud-file:', 'source:'))
    if preserve_mutation:
        prefixes.extend(('source:', 'cloud-task:'))
    exclusions = ''.join(' AND resource NOT LIKE ?' for _ in prefixes)
    c.execute('DELETE FROM claims WHERE task_id=?' + exclusions,
              [task_id, *(prefix + '%' for prefix in prefixes)])


def release(task_id, *, preserve_files=False):
    with _lock:
        c = _conn_ref()
        uncertain = preserve_files and c.execute(
            "SELECT 1 FROM task_files WHERE task_id=? AND gid IS NOT NULL AND status!='complete'",
            (task_id,)).fetchone()
        row = c.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
        _release_claims(c, task_id, preserve_files=bool(uncertain),
                        preserve_mutation=uncertain_mutation(dict(row) if row else None))
        c.commit()


def claim_retry(task_id, *, recover=False):
    """Atomically create a single child attempt; stale buttons stay consumed."""
    with _lock:
        c = _conn_ref()
        with c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM tasks WHERE task_id=?', (task_id,)).fetchone()
            if not old:
                raise TaskConflict('找不到任務')
            old = dict(old)
            if old['stage'] not in (STAGE_FAILED, STAGE_CANCELED) or old['retry_child']:
                raise TaskConflict('任務仍在執行、已完成，或已經重試；請使用最新任務的按鈕')
            if uncertain_mutation(old):
                raise TaskConflict(UNCERTAIN_MESSAGE)
            if c.execute("SELECT 1 FROM claims WHERE task_id=? AND resource NOT LIKE 'path:%' "
                         "AND resource NOT LIKE 'cloud-file:%' AND resource NOT LIKE 'source:%'",
                         (task_id,)).fetchone():
                raise TaskConflict('上一個任務仍在收尾，請稍後再試')
            if not any(old[k] for k in ('magnet', 'cloud_task_id', 'cloud_file_id')):
                raise TaskConflict('舊記錄沒有來源 ID；請重新貼上 magnet 或使用 /dl file_id')
            child = uuid.uuid4().hex[:8]
            now = time.time()
            fields = {k: old[k] for k in ('magnet', 'name', 'account', 'source_kind',
                      'offline_path', 'cloud_task_id', 'cloud_file_id', 'manifest_ready',
                      'discord_channel_id', 'discord_message_id', 'submission_started',
                      'retry_requested', 'retry_inflight', 'last_retry_at')}
            if not recover and old['cloud_task_id'] and not old['cloud_file_id']:
                fields['retry_requested'] = 1
            fields['auto_retries'] = old['auto_retries'] if recover else 0
            fields.update(task_id=child, stage=STAGE_QUEUED, retry_of=task_id,
                          created_at=now, updated_at=now)
            cols = ','.join(fields)
            c.execute(f'INSERT INTO tasks ({cols}) VALUES ({",".join("?" for _ in fields)})',
                      list(fields.values()))
            c.execute('UPDATE tasks SET retry_child=? WHERE task_id=?', (child, task_id))
            c.execute('''INSERT INTO task_files
                SELECT ?, file_id, name, directory, gid, status, size, error, destination_owned
                FROM task_files WHERE task_id=?''', (child, task_id))
    return child


def latest_cloud_task(account, cloud_task_id):
    with _lock:
        row = _conn_ref().execute('''SELECT * FROM tasks WHERE (account=? AND cloud_task_id=?)
                    OR task_id IN (SELECT task_id FROM resource_history WHERE resource=?)
                    ORDER BY created_at DESC LIMIT 1''',
                    (account, cloud_task_id, f'cloud-task:{account}:{cloud_task_id}')).fetchone()
    return dict(row) if row else None


def request_offline_retry(task_id):
    """Coalesce repeated /retry requests while the existing owner is working."""
    with _lock:
        c = _conn_ref()
        result = c.execute('''UPDATE tasks SET retry_requested=1 WHERE task_id=? AND stage=?
                              AND retry_requested=0 AND retry_inflight=0 AND COALESCE(last_retry_at,0)<=?''',
                           (task_id, STAGE_OFFLINE, time.time() - 60))
        c.commit()
        return result.rowcount == 1


def save_file(task_id, file_id, name, directory, **fields):
    with _lock:
        c = _conn_ref()
        c.execute('''INSERT OR IGNORE INTO task_files(task_id,file_id,name,directory)
                     VALUES (?,?,?,?)''', (task_id, file_id, name, directory))
        if fields:
            if not set(fields) <= {'gid', 'status', 'size', 'error', 'destination_owned'}:
                raise ValueError('Unknown file field')
            c.execute(f'UPDATE task_files SET {",".join(k + "=?" for k in fields)} '
                      'WHERE task_id=? AND file_id=?', [*fields.values(), task_id, file_id])
        c.commit()


def files(task_id):
    with _lock:
        return [dict(r) for r in _conn_ref().execute(
            'SELECT * FROM task_files WHERE task_id=?', (task_id,))]


def set_manifest(task_id, name, file_id, entries):
    with _lock:
        c = _conn_ref()
        with c:
            c.execute('DELETE FROM task_files WHERE task_id=?', (task_id,))
            c.executemany('INSERT INTO task_files(task_id,file_id,name,directory) VALUES (?,?,?,?)',
                          [(task_id, *entry) for entry in entries])
            c.execute('UPDATE tasks SET name=?,cloud_file_id=?,manifest_ready=1 WHERE task_id=?',
                      (name, file_id, task_id))


def observe(account, tasks, now=None):
    """Return seconds unchanged, requiring continuous successful observations."""
    now = time.time() if now is None else now
    ages = {}
    with _lock:
        c = _conn_ref()
        for task in tasks:
            key = task.get('id')
            if not key:
                continue
            progress = int(task.get('progress') or 0)
            phase = task.get('phase')
            old = c.execute('SELECT * FROM observations WHERE account=? AND cloud_task_id=?',
                            (account, key)).fetchone()
            changed = (not old or old['progress'] != progress or old['phase'] != phase
                       or now - old['seen_at'] > 600)
            changed_at = now if changed else old['changed_at']
            c.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?)',
                      (account, key, progress, phase, changed_at, now))
            ages[key] = now - changed_at
        c.commit()
    return ages
