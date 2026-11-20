from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
import io
import json
import sqlite3
import tempfile
import unittest

from fxwatch.__main__ import main
from fxwatch.config import load
from fxwatch.engine import connect
from fxwatch.web import App

ROOT = Path(__file__).resolve().parents[1]


class InitTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / 'artifacts' / 'testtmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.toml'
        self.config.write_text('database="data/fxwatch.sqlite3"\n', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def run_init(self):
        with patch('sys.argv', ['fxwatch', '--config', str(self.config), 'init']), \
             patch.dict('os.environ', {'TELEGRAM_BOT_TOKEN': '', 'TELEGRAM_CHAT_ID': ''}), \
             patch('fxwatch.__main__.collect', side_effect=AssertionError('init must not collect')), \
             patch('fxwatch.__main__.deliver', side_effect=AssertionError('init must not notify')), \
             redirect_stdout(io.StringIO()):
            return main()

    def test_first_boot_database_is_ready_for_web_before_collection(self):
        self.assertEqual(self.run_init(), 0)
        c = load(self.config)
        self.assertTrue(c['database'].is_file())
        captured = {}
        def start(status, _headers):
            captured['status'] = status
        body = b''.join(App(c)({'PATH_INFO': '/api/summary'}, start))
        self.assertEqual(captured['status'], '200 OK')
        data = json.loads(body)
        self.assertEqual(len(data['rows']), 24)
        self.assertEqual(len(data['currencies']), 4)

    def test_reinitialization_keeps_history_and_alert_state_and_restores_index(self):
        c = load(self.config)
        db = connect(c['database'])
        db.execute("INSERT INTO state VALUES ('target:USD', 'true')")
        db.execute("INSERT INTO observations(bank,currency,sell,quoted_at,fetched_at,raw_sell,unit,source,sha256,quality) VALUES ('boc','USD','7','2026-11-16T10:00:00+08:00','2026-11-16T10:00:00+08:00','700',100,'https://example.test','test','ok')")
        db.execute('DROP INDEX obs_currency_time')
        db.commit()
        db.close()
        self.assertEqual(self.run_init(), 0)
        self.assertEqual(self.run_init(), 0)
        db = sqlite3.connect(c['database'])
        try:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT value FROM state').fetchone()[0], 'true')
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='obs_currency_time'").fetchone())
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
