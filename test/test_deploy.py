import sys
import sqlite3
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_from_git as updater


OLD = 'a' * 40
NEW = 'b' * 40


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / '.env').write_text('DISCORD_BOT_TOKEN=test\n')
        for name, value in (
            ('ROOT', root),
            ('STATE_DIR', root / 'data'),
            ('DEPLOYED_REVISION', root / 'data/.deployed-revision'),
            ('FAILED_REVISION', root / 'data/.failed-revision'),
            ('BACKUP_DIR', root / 'backups/restorepoints'),
        ):
            patcher = patch.object(updater, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def mock_git(self, target=NEW, dirty=False):
        def git(*args, capture=False):
            if args[0] == 'symbolic-ref':
                return 'main'
            if args[0] == 'status':
                return ' M src/bot.py' if dirty else ''
            if args[:2] == ('rev-parse', 'HEAD'):
                return OLD
            if args[:2] == ('rev-parse', 'FETCH_HEAD'):
                return target
            return None
        return Mock(side_effect=git)

    def test_current_commit_is_marked_without_restart(self):
        git = self.mock_git(target=OLD)
        with patch.object(updater, 'git', git), patch.object(updater, 'ready', return_value=True), \
                patch.object(updater, 'start_service') as start:
            updater.deploy()
        self.assertEqual(updater.DEPLOYED_REVISION.read_text(), OLD + '\n')
        start.assert_not_called()

    def test_new_commit_is_tested_backed_up_and_started(self):
        git = self.mock_git()
        original_git = git.side_effect
        events = []

        def traced_git(*args, capture=False):
            if args[0] == 'merge':
                events.append('merge')
            return original_git(*args, capture=capture)

        git.side_effect = traced_git
        with patch.object(updater, 'git', git), \
                patch.object(updater, 'ensure_fast_forward'), \
                patch.object(updater, 'ensure_same_dependencies'), \
                patch.object(updater, 'validate') as validate, \
                patch.object(updater, 'stop_service') as stop, \
                patch.object(updater, 'backup_state', side_effect=lambda revision: (events.append('backup'), (Path(self.temp.name), Path(self.temp.name) / 'db'))[1]) as backup, \
                patch.object(updater, 'start_service') as start:
            stop.side_effect = lambda: events.append('stop')
            start.side_effect = lambda: events.append('start')
            updater.deploy()
        validate.assert_called_once_with(NEW)
        backup.assert_called_once_with(OLD)
        stop.assert_called_once_with()
        git.assert_any_call('merge', '--ff-only', NEW)
        start.assert_called_once_with()
        self.assertEqual(events, ['stop', 'backup', 'merge', 'start'])
        self.assertEqual(updater.DEPLOYED_REVISION.read_text(), NEW + '\n')

    def test_failed_restart_restores_previous_commit_and_skips_retry(self):
        git = self.mock_git()
        with patch.object(updater, 'git', git), \
                patch.object(updater, 'ensure_fast_forward'), \
                patch.object(updater, 'ensure_same_dependencies'), \
                patch.object(updater, 'validate'), \
                patch.object(updater, 'stop_service'), \
                patch.object(updater, 'backup_state', return_value=(Path(self.temp.name), Path(self.temp.name) / 'db')), \
                patch.object(updater, 'start_service', side_effect=RuntimeError('bad')), \
                patch.object(updater, 'restore_previous') as restore:
            with self.assertRaisesRegex(RuntimeError, 'código e banco restaurados'):
                updater.deploy()
        restore.assert_called_once_with('main', OLD, Path(self.temp.name), Path(self.temp.name) / 'db')
        self.assertEqual(updater.FAILED_REVISION.read_text(), NEW + '\n')
        self.assertFalse(updater.DEPLOYED_REVISION.exists())

        with patch.object(updater, 'git', self.mock_git()), \
                patch.object(updater, 'ensure_fast_forward'), \
                patch.object(updater, 'validate') as validate:
            with self.assertRaisesRegex(RuntimeError, 'aguardando novo commit'):
                updater.deploy()
        validate.assert_not_called()

    def test_local_edit_blocks_fetch(self):
        git = self.mock_git(dirty=True)
        with patch.object(updater, 'git', git):
            with self.assertRaisesRegex(RuntimeError, 'alterações locais'):
                updater.deploy()
        self.assertFalse(any(call.args[0] == 'fetch' for call in git.call_args_list))

    def test_start_checks_openrc_supervision(self):
        with patch.object(updater, 'command') as command, \
                patch.object(updater, 'ready', return_value=True):
            updater.start_service()
        command.assert_any_call('rc-service', 'laynai', 'start', new_session=True)
        command.assert_any_call('rc-service', 'laynai', 'status')

    def test_backup_failure_restarts_old_service_without_merging(self):
        git = self.mock_git()
        with patch.object(updater, 'git', git), \
                patch.object(updater, 'ensure_fast_forward'), \
                patch.object(updater, 'ensure_same_dependencies'), \
                patch.object(updater, 'validate'), \
                patch.object(updater, 'stop_service'), \
                patch.object(updater, 'backup_state', side_effect=RuntimeError('disco cheio')), \
                patch.object(updater, 'start_service') as start:
            with self.assertRaisesRegex(RuntimeError, 'disco cheio'):
                updater.deploy()
        start.assert_called_once_with()
        self.assertFalse(any(call.args[0] == 'merge' for call in git.call_args_list))

    def test_restore_database_replaces_wal_database(self):
        root = Path(self.temp.name)
        snapshot = root / 'snapshot'
        snapshot.mkdir()
        source = snapshot / 'bot.sqlite'
        destination = root / 'bot.sqlite'
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute('CREATE TABLE value (n INTEGER)')
            connection.execute('INSERT INTO value VALUES (1)')
        with closing(sqlite3.connect(destination)) as connection, connection:
            connection.execute('CREATE TABLE value (n INTEGER)')
            connection.execute('INSERT INTO value VALUES (2)')
        Path(str(destination) + '-wal').write_bytes(b'stale')
        Path(str(destination) + '-shm').write_bytes(b'stale')
        updater.restore_database(snapshot, destination)
        with closing(sqlite3.connect(destination)) as connection:
            self.assertEqual(connection.execute('SELECT n FROM value').fetchone()[0], 1)
        self.assertFalse(Path(str(destination) + '-wal').exists())
        self.assertFalse(Path(str(destination) + '-shm').exists())

    def test_backup_state_saves_code_env_and_database_before_deploy(self):
        root = Path(self.temp.name)
        database = root / 'bot.sqlite'
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute('CREATE TABLE value (n INTEGER)')

        def git_archive(*args, capture=False):
            self.assertEqual(args[0], 'archive')
            Path(args[2].split('=', 1)[1]).write_bytes(b'archive')

        with patch.object(updater, 'git', side_effect=git_archive), \
                patch.object(updater, 'database_path', return_value=database), \
                patch.object(updater, 'backup_database', side_effect=lambda source, destination: shutil.copy2(source, destination)):
            snapshot, path = updater.backup_state(OLD)
        self.assertEqual(path, database)
        self.assertEqual((snapshot / 'code.tar').read_bytes(), b'archive')
        self.assertEqual((snapshot / '.env').read_text(), (root / '.env').read_text())
        self.assertEqual((snapshot / 'revision').read_text(), OLD + '\n')
        with closing(sqlite3.connect(snapshot / 'bot.sqlite')) as connection:
            self.assertEqual(connection.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o700)
        self.assertEqual((snapshot / '.env').stat().st_mode & 0o777, 0o600)

    def test_rollback_restores_env_and_database(self):
        root = Path(self.temp.name)
        snapshot = root / 'snapshot'
        snapshot.mkdir()
        (snapshot / '.env').write_text('DISCORD_BOT_TOKEN=old\n')
        (root / '.env').write_text('DISCORD_BOT_TOKEN=new\n')
        database = root / 'bot.sqlite'
        for path, number in ((snapshot / 'bot.sqlite', 1), (database, 2)):
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute('CREATE TABLE value (n INTEGER)')
                connection.execute('INSERT INTO value VALUES (?)', (number,))
        with patch.object(updater, 'git', return_value='') as git, \
                patch.object(updater, 'stop_service') as stop, \
                patch.object(updater, 'start_service') as start:
            updater.restore_previous('main', OLD, snapshot, database)
        stop.assert_called_once_with()
        start.assert_called_once_with()
        git.assert_any_call('branch', '--force', 'main', OLD)
        self.assertEqual((root / '.env').read_text(), 'DISCORD_BOT_TOKEN=old\n')
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(connection.execute('SELECT n FROM value').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
