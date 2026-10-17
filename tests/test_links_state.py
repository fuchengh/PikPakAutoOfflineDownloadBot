from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from pikpakbot import state
from pikpakbot.links import parse_links, source_key


@pytest.mark.parametrize('separator', ['', ' ', '\n', ',', '，', ';'])
def test_joined_magnets(separator, magnet):
    other = magnet.replace('a' * 40, 'b' * 40)
    assert parse_links(magnet + separator + other) == [magnet, other]


def test_wrappers_trackers_and_hash_dedup(magnet):
    link = magnet + '&tr=https://tracker.example/announce&dn=some%20file'
    assert parse_links(f'<{link}> `{magnet.upper()}`') == [link]


def test_base32_and_hex_are_same_source():
    assert source_key('magnet:?xt=urn:btih:' + 'A' * 32) == 'btih:' + '0' * 40


def test_regular_urls_unchanged():
    links = ['https://example.com/file?a=1&b=2', 'ed2k://|file|name|1|hash|/']
    assert parse_links('\n'.join(links)) == links


@pytest.mark.parametrize('text', ['hello', 'magnet:?dn=nohash', 'magnet:?xt=urn:btih:abcd'])
def test_invalid_input_rejected(text):
    with pytest.raises(ValueError):
        parse_links(text)


def test_legacy_database_additive_migration():
    conn = sqlite3.connect(state.DB_PATH)
    conn.execute('''CREATE TABLE tasks(task_id TEXT PRIMARY KEY, magnet TEXT,name TEXT,account TEXT,
        stage TEXT NOT NULL,progress INTEGER DEFAULT 0,error TEXT,created_at REAL NOT NULL,
        updated_at REAL NOT NULL,completed_at REAL)''')
    conn.execute("INSERT INTO tasks VALUES ('old',NULL,'old name','a','failed',99,'old error',1,2,3)")
    conn.commit()
    conn.close()
    row = state.get_task('old')
    assert row['error'] == 'old error' and row['completed_at'] == 3
    assert row['cloud_task_id'] is None
    with pytest.raises(state.TaskConflict, match='來源 ID'):
        state.claim_retry('old')
    assert len(state.list_recent()) == 1


def test_concurrent_retry_has_single_child_and_preserves_history(magnet):
    parent = state.create_task(magnet=magnet, stage=state.STAGE_FAILED, account='a',
                               offline_path='/custom', cloud_task_id='cloud', discord_channel_id=10,
                               discord_message_id=20)
    state.save_file(parent, 'file', 'file.bin', '/downloads', gid='gid', status='complete', size=10)
    def click(_):
        try:
            return state.claim_retry(parent)
        except state.TaskConflict:
            return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(click, range(8)))
    children = [child for child in results if child]
    assert len(children) == 1
    row = state.get_task(children[0])
    assert row['offline_path'] == '/custom' and row['discord_message_id'] == 20
    assert state.files(children[0])[0]['gid'] == 'gid'
    assert state.get_task(parent)['stage'] == state.STAGE_FAILED


def test_finishing_worker_cannot_be_retried(magnet):
    parent = state.create_task(magnet=magnet, stage=state.STAGE_FAILED)
    state.claim(parent, 'resource')
    with pytest.raises(state.TaskConflict, match='收尾'):
        state.claim_retry(parent)


def test_claims_exclude_other_tasks_but_not_same_owner():
    state.claim('one', 'same')
    state.claim('one', 'same')
    with pytest.raises(state.TaskConflict):
        state.claim('two', 'same')
    state.release('one')
    state.claim('two', 'same')


def test_stall_requires_continuous_observation_and_resets_on_progress():
    tasks = [{'id': 'cloud', 'phase': 'PHASE_TYPE_RUNNING', 'progress': 99}]
    assert state.observe('a', tasks, now=0)['cloud'] == 0
    assert state.observe('a', tasks, now=300)['cloud'] == 300
    tasks[0]['progress'] = 100
    assert state.observe('a', tasks, now=400)['cloud'] == 0
    assert state.observe('a', tasks, now=2000)['cloud'] == 0  # No treating downtime as a stall.


def test_restart_releases_claims_preserves_files_and_terminal_history(magnet):
    task = state.create_task(magnet=magnet, cloud_task_id='cloud', stage=state.STAGE_DOWNLOAD)
    done = state.create_task(magnet=magnet, stage=state.STAGE_COMPLETE)
    state.claim(task, 'cloud')
    state.save_file(task, 'file', 'file.bin', '/downloads', gid='saved-gid')
    assert state.sweep_interrupted() == 1
    assert state.get_task(done)['stage'] == state.STAGE_COMPLETE
    assert state.files(task)[0]['gid'] == 'saved-gid'
    state.claim_retry(task)
