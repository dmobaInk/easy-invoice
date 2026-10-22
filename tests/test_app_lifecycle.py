from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.config import Settings

try:
    from app.main import app, health, lifespan
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@unittest.skipUnless(HAS_FASTAPI, 'Install project dependencies for lifecycle tests')
class AppLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_listener_and_database_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory) / 'test.db')
            with patch('app.main.Settings.from_env', return_value=settings):
                async with lifespan(app):
                    self.assertTrue(settings.database_path.exists())
                    self.assertEqual(health()['feishu'], 'disabled')

    async def test_start_health_failure_and_stop_child(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(Path(directory) / 'test.db', True, 'test_id', 'test_secret')
            process = Mock()
            process.is_alive.return_value = True
            context = Mock()
            context.Process.return_value = process
            with patch('app.main.Settings.from_env', return_value=settings), \
                    patch('app.main.multiprocessing.get_context', return_value=context):
                async with lifespan(app):
                    process.start.assert_called_once()
                    self.assertEqual(health()['feishu'], 'process_running')
                    process.is_alive.return_value = False
                    self.assertEqual(health().status_code, 503)
                    process.is_alive.return_value = True
                process.terminate.assert_called_once()
                process.kill.assert_called_once()
                process.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
