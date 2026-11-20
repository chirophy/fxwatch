from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import io
import json
import sqlite3
import tempfile
import unittest
from fxwatch.config import load
from fxwatch.engine import collect, connect
from fxwatch.sources import CN, parse
from fxwatch.web import App, history

ROOT=Path(__file__).resolve().parents[1]
NOW=datetime(2026,11,16,10,20,tzinfo=CN)


class WebTests(unittest.TestCase):
    def setUp(self):
        temp_root=ROOT/'artifacts'/'testtmp'
        temp_root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp_root)
        self.root=Path(self.temp.name)
        assert self.root.resolve().is_relative_to(temp_root.resolve())
        self.c=load(ROOT/'config.example.toml')
        self.c.update(database=self.root/'test.db',raw_directory=self.root/'raw',telegram_token='',telegram_chat_id='')
        self.app=App(self.c)
        self.db=connect(self.c['database'])
        def fake(bank,*_):
            quotes=parse(bank,(ROOT/'tests'/'fixtures'/(bank+'.txt')).read_text(encoding='utf8'))
            return [replace(q,quoted_at=NOW) for q in quotes],b'web test'
        collect(self.db,self.c,NOW,fake)

    def tearDown(self):
        self.db.close();self.temp.cleanup()

    def request(self,path,query='',method='GET',now=NOW):
        captured={}
        def start(status,headers):captured.update(status=status,headers=dict(headers))
        with patch('fxwatch.web.now_cn',return_value=now):
            body=b''.join(self.app({'PATH_INFO':path,'QUERY_STRING':query,'REQUEST_METHOD':method},start))
        return captured['status'],captured['headers'],body

    def test_home_and_assets(self):
        for path in ('/','/app.js','/style.css','/favicon.svg'):
            with self.subTest(path=path):
                status,headers,body=self.request(path)
                self.assertEqual(status,'200 OK')
                self.assertGreater(len(body),100)
                self.assertEqual(headers['X-Content-Type-Options'],'nosniff')
                self.assertIn("script-src 'self'",headers['Content-Security-Policy'])

    def test_four_currency_summary_keeps_precision_and_omits_secrets(self):
        self.c['telegram_token']='hidden-token'
        status,_,body=self.request('/api/summary')
        data=json.loads(body)
        self.assertEqual(status,'200 OK')
        self.assertEqual(len(data['rows']),24)
        self.assertEqual([c['code'] for c in data['currencies']],['USD','EUR','HKD','JPY'])
        jpy=next(c for c in data['currencies'] if c['code']=='JPY')
        self.assertEqual(jpy['best'],'0.041888')
        self.assertEqual(jpy['best_banks'],['cgb'])
        self.assertNotIn(b'hidden-token',body)
        self.assertNotIn(b'raw_directory',body)
        self.assertNotIn(str(self.root).encode(),body)
        self.assertNotIn('error',data['rows'][0])

    def test_stale_data_is_not_ranked_and_health_degrades(self):
        _,_,body=self.request('/api/summary',now=NOW+timedelta(hours=1))
        self.assertTrue(all(c['best'] is None for c in json.loads(body)['currencies']))
        self.assertEqual(self.request('/api/health')[0],'200 OK')
        self.assertEqual(self.request('/api/health',now=NOW+timedelta(hours=1))[0],'503 Service Unavailable')

    def test_history_currency_isolation_and_gap_segments(self):
        status,_,body=self.request('/api/history','currency=JPY&hours=6')
        data=json.loads(body)
        self.assertEqual(status,'200 OK')
        self.assertEqual(data['currency'],'JPY')
        for series in data['series']:
            self.assertEqual(len(series['points']),1)
            self.assertLess(Decimal(series['points'][0]['price']),Decimal('.1'))
        self.db.execute("INSERT INTO observations(bank,currency,sell,quoted_at,fetched_at,raw_sell,unit,source,sha256,quality) SELECT bank,currency,sell,quoted_at,?,raw_sell,unit,source,sha256,quality FROM observations WHERE bank='cgb' AND currency='JPY'",
                        ((NOW+timedelta(minutes=30)).isoformat(),))
        self.db.commit()
        result=history(self.db,self.c,'JPY',6,NOW+timedelta(hours=1))
        p=result['series'][0]['points']
        self.assertNotEqual(p[0]['segment'],p[1]['segment'])

    def test_csv_download_and_parameter_bounds(self):
        status,headers,body=self.request('/api/export.csv','currency=HKD&hours=24')
        self.assertEqual(status,'200 OK')
        self.assertIn('fxwatch-HKD-24h.csv',headers['Content-Disposition'])
        lines=body.decode('utf-8-sig').splitlines()
        self.assertEqual(len(lines),7)
        self.assertTrue(all(',HKD,' in s for s in lines[1:]))
        for query in ('currency=GBP','currency=USD%27','hours=-1','hours=999999','hours=banana'):
            with self.subTest(query=query):self.assertEqual(self.request('/api/history',query)[0],'400 Bad Request')

    def test_read_only_routes_and_no_path_traversal(self):
        before=self.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0]
        self.assertEqual(self.request('/api/summary',method='POST')[0],'405 Method Not Allowed')
        self.assertEqual(self.request('/../config.toml')[0],'404 Not Found')
        self.assertEqual(self.request('/config.toml')[0],'404 Not Found')
        self.assertEqual(self.request('/api/summary',method='HEAD')[2],b'')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM observations').fetchone()[0],before)

    def test_before_first_collection_shows_empty_state(self):
        self.c['database']=self.root/'not-created.db'
        status,_,body=self.request('/api/summary')
        self.assertEqual(status,'200 OK')
        data=json.loads(body)
        self.assertEqual(len(data['rows']),24)
        self.assertIsNone(data['last_cycle'])
        self.assertTrue(all(c['best'] is None for c in data['currencies']))
        self.assertFalse(self.c['database'].exists())

    def test_hkd_jpy_target_configuration(self):
        file=self.root/'config.toml'
        file.write_text('[targets]\nHKD="0.85"\nJPY="0.041"\n',encoding='utf8')
        self.assertEqual(load(file)['targets']['JPY'],Decimal('.041'))
        file.write_text('[targets]\nJPY="4.1"\n',encoding='utf8')
        with self.assertRaises(ValueError):load(file)


if __name__=='__main__':unittest.main()
