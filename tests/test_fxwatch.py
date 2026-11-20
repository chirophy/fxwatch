from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from fxwatch.config import load
from fxwatch.sources import CN, CURRENCIES, parse, quote, stamp, SourceError, URLS
from fxwatch.engine import collect, connect, snapshot, deliver, get_state, send_telegram
from fxwatch.report import render
from fxwatch.__main__ import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures"
NOW = datetime(2026, 11, 16, 10, 0, tzinfo=CN)


class ParserTests(unittest.TestCase):
    def test_actual_bank_samples_and_units(self):
        expected = {"cgb": ("6.6835", "7.3762"), "hxb": ("6.6831", "7.3802"),
                    "boc": ("6.6869", "7.3801"), "citic": ("6.6895", "7.3782"),
                    "cmb": ("6.68958", "7.38295"), "icbc": ("6.6883", "7.3834")}
        for bank, prices in expected.items():
            with self.subTest(bank=bank):
                rows = {q.currency: q for q in parse(bank, (FIXTURES / (bank+'.txt')).read_text(encoding='utf8'))}
                self.assertEqual([rows[c].sell for c in ('USD', 'EUR')], list(map(Decimal, prices)))
                self.assertEqual(rows['USD'].unit, '1' if bank == 'cgb' else '100')
                self.assertEqual(rows['USD'].quoted_at.utcoffset(), timedelta(hours=8))
                self.assertEqual(set(rows),set(CURRENCIES))
                self.assertTrue(Decimal('0.8') < rows['HKD'].sell < Decimal('0.9'))
                self.assertTrue(Decimal('0.04') < rows['JPY'].sell < Decimal('0.05'))

    def test_jpy_and_hkd_units_are_exact(self):
        cgb={q.currency:q for q in parse('cgb',(FIXTURES/'cgb.txt').read_text(encoding='utf8'))}
        cmb={q.currency:q for q in parse('cmb',(FIXTURES/'cmb.txt').read_text(encoding='utf8'))}
        self.assertEqual(cgb['JPY'].sell,Decimal('0.041888'))
        self.assertEqual(cgb['JPY'].unit,'100')
        self.assertEqual(cgb['HKD'].sell,Decimal('0.8518'))
        self.assertEqual(cgb['HKD'].unit,'1')
        self.assertEqual(cmb['JPY'].sell,Decimal('0.041938'))
        for code,value,unit in [('JPY','4.19',1),('JPY','0.0419',100),('HKD','85.2',1),('HKD','0.852',100)]:
            with self.assertRaises(SourceError): quote('cgb',code,value,unit,'2026-11-16 10:00:00')

    def test_cash_sell_never_substitutes_cashless_sell(self):
        text = (FIXTURES/'boc.txt').read_text(encoding='utf8').replace('668.69</td>', '--</td>', 1)
        with self.assertRaises(SourceError):
            parse('boc', text)

    def test_layout_change_rejected(self):
        text = (FIXTURES/'cgb.txt').read_text(encoding='utf8').replace('现汇卖出价', '未知价格')
        with self.assertRaises(SourceError):
            parse('cgb', text)

    def test_missing_date_never_assumes_today(self):
        with self.assertRaises(SourceError):
            stamp('09:35:00')
        data = json.loads((FIXTURES/'cmb.txt').read_text(encoding='utf8'))
        data['body'][0]['ratDat'] = ''
        with self.assertRaises(SourceError):
            parse('cmb', json.dumps(data))

    def test_zero_nan_unknown_unit_and_scaling_errors_rejected(self):
        for price, unit in [('0',1), ('NaN',1), ('Infinity',1), ('668',1), ('6.68',100), ('6.68',10)]:
            with self.subTest(price=price, unit=unit), self.assertRaises(SourceError):
                quote('cgb','USD',price,unit,'2026-11-16 10:00:00')

    def test_incomplete_or_error_response_rejected(self):
        for bank in ('cmb','icbc','citic'):
            data = json.loads((FIXTURES/(bank+'.txt')).read_text(encoding='utf8'))
            if bank == 'cmb': data['body'] = data['body'][:1]
            elif bank == 'icbc': data['code'] = 1
            else: data['content']['resultList'] = []
            with self.subTest(bank=bank), self.assertRaises(SourceError):
                parse(bank,json.dumps(data))


class EngineTests(unittest.TestCase):
    def setUp(self):
        temp_root = ROOT / 'artifacts' / 'testtmp'
        temp_root.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.tmp.name)
        assert self.root.resolve().is_relative_to(temp_root.resolve())
        self.c = load(ROOT/'config.example.toml')
        self.c.update(database=self.root/'db.sqlite3', raw_directory=self.root/'raw',
                      telegram_token='test-token', telegram_chat_id='test-chat',
                      targets={}, banks=['cgb','hxb','boc'])
        self.db = connect(self.c['database'])

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fetcher(self, prices=None, timestamp=None, fail=None):
        prices = prices or {'cgb':'6.68','hxb':'6.69','boc':'6.70'}
        def fake(bank, *_):
            if bank == fail:
                raise OSError('simulated offline')
            date = (timestamp or NOW).strftime('%Y-%m-%d %H:%M:%S')
            return [quote(bank,cur,Decimal(prices[bank])/({'HKD':8,'JPY':160}.get(cur,1)),1,date)
                    for cur in CURRENCIES], b'test fixture'
        return fake

    def run_cycle(self, now=NOW, **kw):
        return collect(self.db,self.c,now,self.fetcher(**kw),force=True)

    def count_alerts(self):
        return self.db.execute('SELECT COUNT(*) FROM alerts').fetchone()[0]

    def test_stale_and_future_excluded(self):
        for offset, expected in [(timedelta(hours=-1),'stale'),(timedelta(minutes=5),'future')]:
            with self.subTest(expected=expected):
                data = self.run_cycle(timestamp=NOW+offset)
                self.assertEqual(data['rankings']['USD'],[])
                self.assertTrue(all(r['quality']==expected for r in data['rows']))

    def test_stopped_collector_cannot_look_live(self):
        self.run_cycle()
        data = snapshot(self.db,self.c,NOW+timedelta(seconds=700))
        self.assertEqual(data['rankings']['USD'],[])
        self.assertTrue(all(r['quality']=='not_recently_fetched' for r in data['rows']))

    def test_outage_excludes_old_quotes_and_backs_off(self):
        self.run_cycle()
        data = self.run_cycle(NOW+timedelta(seconds=300),fail='cgb')
        self.assertEqual(len(data['rankings']['USD']),2)
        source = self.db.execute("SELECT * FROM sources WHERE bank='cgb'").fetchone()
        self.assertEqual(source['failures'],1)
        calls=[]
        def counted(bank,*args):
            calls.append(bank)
            return self.fetcher()(bank,*args)
        collect(self.db,self.c,NOW+timedelta(seconds=400),counted)
        self.assertNotIn('cgb',calls)

    def test_regression_stays_rejected_across_repeated_fetches(self):
        self.run_cycle()
        for seconds in (300,600):
            data=self.run_cycle(NOW+timedelta(seconds=seconds),timestamp=NOW-timedelta(minutes=1))
            self.assertEqual(data['rankings']['USD'],[])
            self.assertTrue(all(r['quality']=='regressed_timestamp' for r in data['rows']))

    def test_outlier_is_saved_but_not_ranked(self):
        data=self.run_cycle(prices={'cgb':'2','hxb':'6.69','boc':'6.70'})
        self.assertEqual(len(data['rankings']['USD']),2)
        self.assertEqual(self.db.execute("SELECT quality FROM observations WHERE bank='cgb'").fetchone()[0],'outlier')

    def test_first_baseline_silent_then_daily_low_cooldown(self):
        self.run_cycle()
        self.assertEqual(self.count_alerts(),0)
        self.run_cycle(NOW+timedelta(minutes=5),prices={'cgb':'6.67','hxb':'6.69','boc':'6.70'})
        self.assertEqual(self.count_alerts(),4)
        self.run_cycle(NOW+timedelta(minutes=10),prices={'cgb':'6.66','hxb':'6.69','boc':'6.70'})
        self.assertEqual(self.count_alerts(),4)
        self.run_cycle(NOW+timedelta(days=1),timestamp=NOW+timedelta(days=1))
        self.assertEqual(self.count_alerts(),4)

    def test_target_persists_restart_and_rearms_after_recovery(self):
        self.c['daily_low_alert']=False
        self.c['targets']={'USD':Decimal('6.685')}
        self.run_cycle()
        self.assertEqual(self.count_alerts(),1)
        self.db.close(); self.db=connect(self.c['database'])
        self.run_cycle(NOW+timedelta(minutes=1))
        self.assertEqual(self.count_alerts(),1)
        self.run_cycle(NOW+timedelta(minutes=2),fail='cgb')
        self.run_cycle(NOW+timedelta(minutes=3))
        self.assertEqual(self.count_alerts(),1)
        self.run_cycle(NOW+timedelta(minutes=4),prices={'cgb':'6.69','hxb':'6.70','boc':'6.71'})
        self.run_cycle(NOW+timedelta(minutes=5))
        self.assertEqual(self.count_alerts(),2)

    def test_unconfigured_notification_does_not_consume_target(self):
        self.c['targets']={'USD':Decimal('6.685')}
        self.c['telegram_token']=''
        self.run_cycle()
        self.c['telegram_token']='test-token'
        self.run_cycle(NOW+timedelta(minutes=1))
        self.assertEqual(self.count_alerts(),1)

    def test_single_bank_cannot_trigger_target(self):
        self.c['targets']={'USD':Decimal('10')}
        self.c['banks']=['cgb','hxb']
        self.run_cycle(fail='hxb')
        self.assertEqual(self.count_alerts(),0)

    def test_delivery_retry_expiry_and_dedup(self):
        self.c['targets']={'USD':Decimal('10')}
        self.run_cycle()
        def broken(*_):raise RuntimeError('https://secret-token')
        deliver(self.db,self.c,NOW,sender=broken)
        event=self.db.execute('SELECT * FROM alerts').fetchone()
        self.assertIsNone(event['sent_at'])
        self.assertNotIn('secret',event['error'])
        sent=[]
        deliver(self.db,self.c,NOW+timedelta(minutes=1),sender=lambda *x:sent.append(x))
        deliver(self.db,self.c,NOW+timedelta(minutes=2),sender=lambda *x:sent.append(x))
        self.assertEqual(len(sent),1)
        self.db.execute('UPDATE alerts SET sent_at=NULL');self.db.commit()
        deliver(self.db,self.c,NOW+timedelta(hours=1),sender=lambda *x:sent.append(x))
        self.assertEqual(len(sent),1)

    def test_pending_alert_expires_with_bank_quote_not_collection(self):
        self.c['targets']={'USD':Decimal('10')}
        self.run_cycle(timestamp=NOW-timedelta(minutes=14))
        event=self.db.execute('SELECT expires_at FROM alerts').fetchone()
        self.assertEqual(datetime.fromisoformat(event[0]),NOW+timedelta(minutes=1))
        sent=[]
        deliver(self.db,self.c,NOW+timedelta(minutes=2),sender=lambda *x:sent.append(x))
        self.assertEqual(sent,[])

    def test_telegram_exception_cannot_expose_token(self):
        with patch('urllib.request.urlopen',side_effect=OSError('URL with secret-token')):
            with self.assertRaises(RuntimeError) as error:
                send_telegram('secret-token','chat','message')
        self.assertNotIn('secret-token',str(error.exception))

    def test_report_and_online_backup(self):
        self.run_cycle()
        output=self.root/'report.html'
        render(self.db,self.c,output,NOW)
        page=output.read_text(encoding='utf8')
        self.assertIn('<svg',page)
        self.assertNotIn('test-token',page)
        self.assertIn('6.68',page)
        backup=connect(self.root/'backup.sqlite3')
        self.db.backup(backup)
        self.assertEqual(backup.execute('SELECT COUNT(*) FROM observations').fetchone()[0],12)
        backup.close()

    def test_lock_excludes_second_collector(self):
        with ProcessLock(self.c['database']):
            with self.assertRaises(RuntimeError):
                with ProcessLock(self.c['database']):pass
        with ProcessLock(self.c['database']):pass


if __name__ == '__main__':
    unittest.main()
