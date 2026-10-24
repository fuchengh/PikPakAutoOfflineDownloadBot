from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pikpakbot import cloud_guard, desktop, pikpak_client as pk, pipeline, state
from pikpakbot.notifier import NullNotifier


def test_cooldown_blocks_before_submission_marker(monkeypatch, magnet):
    monkeypatch.setattr(pipeline, 'USER', ['a'])
    pk.guard.fail('a', 'rate_limit')
    upload = Mock()
    monkeypatch.setattr(pipeline, 'magnet_upload', upload)
    task = pipeline.submit_magnet(NullNotifier(), magnet)
    pipeline._run_task(task, NullNotifier())
    row = state.get_task(task)
    assert row['stage'] == state.STAGE_FAILED
    assert row['submission_started'] == 0
    assert not state.uncertain_mutation(row)
    upload.assert_not_called()


def test_thirteen_uncertain_records_and_claims_survive_background_checks(monkeypatch):
    ids = []
    for index in range(13):
        task = state.create_task(magnet=f'magnet:?xt=urn:btih:{index:040x}',
            account='a', stage=state.STAGE_FAILED, submission_started=1,
            error=state.UNCERTAIN_MESSAGE)
        state.claim(task, f'source:{index}')
        ids.append(task)

    def snapshot():
        return ([state.get_task(task) for task in ids],
                [tuple(row) for row in state._conn_ref().execute('SELECT * FROM claims ORDER BY resource')])

    before = snapshot()
    monkeypatch.setattr(pipeline, 'get_stuck_tasks', lambda *a, **kw: [
        {'id': f'untracked-{i}', 'name': 'fixture'} for i in range(13)])
    pipeline.startup_recovery(NullNotifier())
    pipeline.retry_offline_candidates('a', 0, automatic=True)
    for task in ids:
        with pytest.raises(state.TaskConflict):
            pipeline.retry_task(NullNotifier(), task)
    assert snapshot() == before
    pipeline._executor.submit.assert_not_called()


def test_watchdog_does_not_probe_during_cooldown(monkeypatch):
    monkeypatch.setattr(pipeline, 'USER', ['a'])
    cloud_guard.guard.fail('a', 'rate_limit')
    query = Mock()
    monkeypatch.setattr(pipeline, 'retry_offline_candidates', query)
    monkeypatch.setattr(pipeline, 'sleep', Mock(side_effect=[None, StopIteration]))
    with pytest.raises(StopIteration):
        pipeline.stuck_task_watchdog(NullNotifier())
    query.assert_not_called()


def test_monitor_start_is_idempotent_when_adapter_also_starts_it(monkeypatch):
    monkeypatch.setattr(pk, '_cloud_monitor_started', False, raising=False)
    thread = Mock()
    monkeypatch.setattr(pk.threading, 'Thread', thread)
    pk.start_cloud_monitor()
    pk.start_cloud_monitor()
    assert thread.call_count == 1
    thread.return_value.start.assert_called_once()


def test_bot_starts_cloud_monitor_without_desktop_adapter(monkeypatch):
    import main
    updater = Mock()
    monkeypatch.setattr(main, '_configure_logging', Mock())
    monkeypatch.setattr(main, 'build_telegram', lambda: updater)
    monkeypatch.setattr(main, 'register_handlers', Mock())
    monkeypatch.setattr(main, '_maybe_start_discord', lambda: False)
    monkeypatch.setattr(main, '_build_admin_notifier', lambda *args: NullNotifier())
    monkeypatch.setattr(main.threading, 'Thread', Mock())
    monkeypatch.setattr(desktop, 'import_module', Mock(side_effect=ModuleNotFoundError(name='desktop_api')))
    start = Mock()
    monkeypatch.setattr(main, 'start_cloud_monitor', start)
    main.main()
    start.assert_called_once()
    updater.start_polling.assert_called_once()


def test_missing_optional_adapter_preserves_normal_notifier(monkeypatch):
    monkeypatch.setattr(desktop, 'import_module', Mock(side_effect=ModuleNotFoundError(name='desktop_api')))
    desktop.start_api()
    fallback = Mock()
    assert pipeline._restore_notifier({'name': 'fixture'}, fallback) is fallback.create_task_channel.return_value


def test_installed_adapter_keeps_desktop_recovery_route(monkeypatch):
    adapter = SimpleNamespace(start_api=Mock(), restore_notifier=Mock(return_value=object()))
    monkeypatch.setattr(desktop, 'import_module', lambda name: adapter)
    desktop.start_api()
    adapter.start_api.assert_called_once()
    fallback = Mock()
    assert pipeline._restore_notifier({'task_id': 'fixture'}, fallback) is adapter.restore_notifier.return_value
    fallback.create_task_channel.assert_not_called()


def test_broken_adapter_dependency_does_not_silently_change_recovery_route(monkeypatch):
    monkeypatch.setattr(desktop, 'import_module', Mock(side_effect=ModuleNotFoundError(name='missing_dependency')))
    with pytest.raises(ModuleNotFoundError):
        desktop.restore_notifier({'task_id': 'fixture'})
