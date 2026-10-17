import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import config
from pikpakbot import pipeline, state
from pikpakbot.bot import telegram


def tg():
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=123),
        callback_query=SimpleNamespace(data='', answer=Mock(), edit_message_reply_markup=Mock(),
                                       message=SimpleNamespace(chat_id=123)))
    context = SimpleNamespace(bot=Mock(), args=[])
    return update, context


def test_telegram_uses_shared_parser_and_preserves_custom_path(monkeypatch, magnet):
    update, context = tg()
    context.args = ['/movies', magnet + magnet.replace('a' * 40, 'b' * 40)]
    submit = Mock()
    monkeypatch.setattr(telegram, 'submit_magnet', submit)
    telegram.pikpak(update, context)
    assert submit.call_count == 2
    assert submit.call_args.args[2] == '/movies'


def test_telegram_retry_uses_durable_child_without_deleting_files(magnet):
    update, context = tg()
    tid = state.create_task(magnet=magnet, stage=state.STAGE_FAILED)
    update.callback_query.data = 'retry:' + tid
    telegram.handle_callback(update, context)
    update.callback_query.answer.assert_called_once()
    assert state.get_task(tid)['retry_child']
    assert pipeline._executor.submit.call_count == 1


def test_telegram_handlers_register_with_real_library():
    dispatcher = Mock()
    telegram.register_handlers(dispatcher)
    assert dispatcher.add_handler.call_count > 5


def test_config_round_trip_keeps_writer_and_new_settings(tmp_path):
    path = tmp_path / 'config_copy.py'
    path.write_text(Path(config.__file__).read_text(encoding='utf-8'), encoding='utf-8')
    spec = importlib.util.spec_from_file_location('config_copy', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.DISCORD_ADMIN_IDS = [123]
    module.PASSWORD = ['quote" and newline\n']
    module.record_config()
    module.record_config()
    spec.loader.exec_module(module)
    assert module.DISCORD_ADMIN_IDS == [123]
    assert module.PASSWORD == ['quote" and newline\n']
    assert callable(module.record_config)


def test_import_main_does_not_initialize_clients(monkeypatch):
    import telegram.ext
    updater = Mock(side_effect=AssertionError('Client initialized during import'))
    monkeypatch.setattr(telegram.ext, 'Updater', updater)
    import main
    assert callable(main.main)
    updater.assert_not_called()
