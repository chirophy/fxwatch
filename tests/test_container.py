from pathlib import Path
from unittest.mock import patch
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from fxwatch.container import child_commands, main, prepare_config, supervise

ROOT = Path(__file__).resolve().parents[1]


class ContainerTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / 'artifacts' / 'testtmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)
        self.template = self.root / 'default.toml'
        self.template.write_text('database="fxwatch.sqlite3"\n', encoding='utf-8')
        self.config = self.root / 'data' / 'config.toml'

    def tearDown(self):
        self.temp.cleanup()

    def test_config_seed_preserves_user_changes(self):
        prepare_config(self.config, self.template)
        self.config.write_text('interval_seconds=600\n', encoding='utf-8')
        prepare_config(self.config, self.template)
        self.assertEqual(self.config.read_text(), 'interval_seconds=600\n')

    def test_missing_template_does_not_leave_empty_config(self):
        with self.assertRaises(FileNotFoundError):
            prepare_config(self.config, self.root / 'missing')
        self.assertFalse(self.config.exists())

    def test_web_does_not_receive_telegram_credentials(self):
        prepare_config(self.config, self.template)
        with patch.dict(os.environ, TELEGRAM_BOT_TOKEN='secret', TELEGRAM_CHAT_ID='123'):
            collector, web = child_commands(self.config)
        self.assertEqual(collector[2]['TELEGRAM_BOT_TOKEN'], 'secret')
        self.assertNotIn('TELEGRAM_BOT_TOKEN', web[2])
        self.assertNotIn('TELEGRAM_CHAT_ID', web[2])
        self.assertEqual(web[2]['FXWATCH_CONFIG'], str(self.config))

    def test_database_ready_before_child_start(self):
        prepare_config(self.config, self.template)
        def inspect_database(commands, stop):
            db = sqlite3.connect(self.config.parent / 'fxwatch.sqlite3')
            try:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)
            finally:
                db.close()
            self.assertEqual(len(commands), 2)
            return 0
        with patch.dict(os.environ, FXWATCH_CONFIG=str(self.config), TELEGRAM_BOT_TOKEN='', TELEGRAM_CHAT_ID=''), \
             patch('fxwatch.container.supervise', side_effect=inspect_database):
            self.assertEqual(main(), 0)

    def real_supervision(self, failing_code=None, request_stop=False, invalid_second=False):
        children = []
        real_popen = subprocess.Popen
        stop = threading.Event()
        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            children.append(process)
            if request_stop and len(children) == 2:
                stop.set()
            return process
        sleeper = [sys.executable, '-c', 'import time; time.sleep(60)']
        second = [sys.executable, '-c', f'raise SystemExit({failing_code})'] if failing_code is not None else sleeper
        if invalid_second:
            second = [str(self.root / 'nonexistent-program')]
        commands = [('collector', sleeper, os.environ.copy()), ('web', second, os.environ.copy())]
        # Test safeguard prevents any regression from leaving a 60-second child.
        watchdog = threading.Timer(8, stop.set)
        watchdog.start()
        try:
            with patch('fxwatch.container.subprocess.Popen', side_effect=start):
                if invalid_second:
                    with self.assertRaises(OSError):
                        supervise(commands, stop, shutdown_timeout=1)
                else:
                    code = supervise(commands, stop, shutdown_timeout=1)
                    self.assertEqual(code, 0 if request_stop else 1)
        finally:
            watchdog.cancel()
            leaked = []
            for process in children:
                if process.poll() is None:
                    leaked.append(process.pid)
                    process.kill()
                    process.wait()
            self.assertFalse(leaked, 'Supervision leaked running children')
        return children

    def test_child_crash_stops_sibling(self):
        self.assertEqual(len(self.real_supervision(failing_code=7)), 2)

    def test_unexpected_clean_exit_also_stops_sibling(self):
        self.real_supervision(failing_code=0)

    def test_stop_request_terminates_both_children(self):
        self.real_supervision(request_stop=True)

    def test_partial_start_failure_cleans_first_child(self):
        self.assertEqual(len(self.real_supervision(invalid_second=True)), 1)


if __name__ == '__main__':
    unittest.main()
