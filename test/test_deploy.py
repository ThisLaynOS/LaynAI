import sys
import tempfile
import unittest
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
                patch.object(updater, 'restart') as restart:
            updater.deploy()
        self.assertEqual(updater.DEPLOYED_REVISION.read_text(), OLD + '\n')
        restart.assert_not_called()

    def test_new_commit_is_tested_backed_up_and_restarted(self):
        git = self.mock_git()
        with patch.object(updater, 'git', git), \
                patch.object(updater, 'ensure_fast_forward'), \
                patch.object(updater, 'ensure_same_dependencies'), \
                patch.object(updater, 'validate') as validate, \
                patch.object(updater, 'backup_database') as backup, \
                patch.object(updater, 'restart') as restart:
            updater.deploy()
        validate.assert_called_once_with(NEW)
        backup.assert_called_once_with(OLD)
        git.assert_any_call('merge', '--ff-only', NEW)
        restart.assert_called_once_with()
        self.assertEqual(updater.DEPLOYED_REVISION.read_text(), NEW + '\n')

    def test_failed_restart_restores_previous_commit_and_skips_retry(self):
        git = self.mock_git()
        with patch.object(updater, 'git', git), \
                patch.object(updater, 'ensure_fast_forward'), \
                patch.object(updater, 'ensure_same_dependencies'), \
                patch.object(updater, 'validate'), \
                patch.object(updater, 'backup_database'), \
                patch.object(updater, 'restart', side_effect=RuntimeError('bad')), \
                patch.object(updater, 'restore_previous') as restore:
            with self.assertRaisesRegex(RuntimeError, 'serviço restaurado'):
                updater.deploy()
        restore.assert_called_once_with('main', OLD)
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

    def test_restart_checks_openrc_supervision(self):
        with patch.object(updater, 'command') as command, \
                patch.object(updater, 'ready', return_value=True):
            updater.restart()
        command.assert_any_call('rc-service', 'laynai', 'restart', new_session=True)
        command.assert_any_call('rc-service', 'laynai', 'status')


if __name__ == '__main__':
    unittest.main()
