import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import update_from_git as updater


class DeployTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / '.env').write_text('DISCORD_BOT_TOKEN=test\n')
        self.state = root / 'data'
        for name, value in (
            ('ROOT', root),
            ('STATE_DIR', self.state),
            ('DEPLOYED_REVISION', self.state / '.deployed-revision'),
        ):
            patcher = patch.object(updater, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def mock_git(self, old='a' * 40, new='b' * 40, dirty=False):
        def git(*args, capture=False):
            if args[:2] == ('symbolic-ref', '--quiet'):
                return 'main'
            if args[0] == 'status':
                return ' M src/bot.py' if dirty else ''
            if args[:2] == ('rev-parse', 'HEAD'):
                return old
            if args[:2] == ('rev-parse', 'FETCH_HEAD'):
                return new
            return None
        return Mock(side_effect=git)

    def test_new_revision_is_deployed_and_recorded_only_after_ready(self):
        git = self.mock_git()
        compose = Mock(side_effect=lambda *args, **kwargs: 'container-id' if args[:2] == ('ps', '-q') else None)
        command = Mock(side_effect=lambda *args, **kwargs: 'old-image-id' if args[:2] == ('docker', 'inspect') else None)
        with patch.object(updater, 'git', git), patch.object(updater, 'compose', compose), \
                patch.object(updater, 'command', command), patch.object(updater, 'ready', return_value=True), \
                patch.object(updater.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            updater.deploy('main')

        self.assertEqual(updater.DEPLOYED_REVISION.read_text(), 'b' * 40 + '\n')
        compose.assert_any_call('build', 'bot')
        compose.assert_any_call('up', '-d', '--no-build', 'bot')
        command.assert_any_call('docker', 'image', 'tag', 'old-image-id', updater.ROLLBACK_IMAGE)

    def test_unhealthy_update_restores_previous_image(self):
        git = self.mock_git()
        compose = Mock(side_effect=lambda *args, **kwargs: 'container-id' if args[:2] == ('ps', '-q') else None)
        command = Mock(side_effect=lambda *args, **kwargs: 'old-image-id' if args[:2] == ('docker', 'inspect') else None)
        with patch.object(updater, 'git', git), patch.object(updater, 'compose', compose), \
                patch.object(updater, 'command', command), patch.object(updater, 'ready', side_effect=(False, True)), \
                patch.object(updater.subprocess, 'run', return_value=SimpleNamespace(returncode=0)):
            with self.assertRaisesRegex(RuntimeError, 'não ficou pronto'):
                updater.deploy('main')

        self.assertFalse(updater.DEPLOYED_REVISION.exists())
        command.assert_any_call('docker', 'image', 'tag', updater.ROLLBACK_IMAGE, updater.IMAGE)
        compose.assert_any_call('up', '-d', '--no-build', '--force-recreate', 'bot')

    def test_local_edit_blocks_fetch_and_deploy(self):
        git = self.mock_git(dirty=True)
        with patch.object(updater, 'git', git):
            with self.assertRaisesRegex(RuntimeError, 'alterações locais'):
                updater.deploy('main')
        self.assertFalse(any(call.args[0] == 'fetch' for call in git.call_args_list))


if __name__ == '__main__':
    unittest.main()
