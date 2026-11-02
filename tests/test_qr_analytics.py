"""QR link counters use temporary state; no orders, network or production writes."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_store as fixture
import analytics


class QRAnalyticsTests(unittest.TestCase):
    hidden=fixture.StoreTests.hidden
    login=fixture.StoreTests.login

    def setUp(self):
        fixture.StoreTests.setUp(self)
        with fixture.store.connect(True) as c:c.execute('DELETE FROM qr_scans')

    def tearDown(self):
        fixture.StoreTests.tearDown(self)

    def scan(self,campaign='a6-a',address='192.0.2.51',**headers):
        return self.client.get('/r/'+campaign,headers={'CF-Connecting-IP':address,'User-Agent':'Mozilla/5.0',**headers})

    def rows(self):
        return {row['code']:row for row in analytics.qr_summary()}

    def test_redirect_counts_requests_and_normalized_unique_ips_without_cookies(self):
        for address in ['192.0.2.51','::ffff:192.0.2.51','192.0.2.52']:
            response=self.scan(address=address)
            self.assertEqual(response.status_code,302)
            self.assertEqual(response.location,'https://reklik.shop/?src=a6-a')
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertEqual(response.headers['X-Robots-Tag'],'noindex, nofollow')
            self.assertNotIn('Set-Cookie',response.headers)
        row=self.rows()['a6-a']
        self.assertEqual((row['total_scans'],row['unique_ips']),(3,2))
        with fixture.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
            saved=[dict(r) for r in c.execute('SELECT * FROM qr_scans')]
        self.assertNotIn('192.0.2.',json.dumps(saved))
        self.assertTrue(all(len(r['visitor_hash'])==64 for r in saved))

    def test_campaign_hashes_and_counts_are_separate(self):
        self.scan();self.scan('a6-b');self.scan('a6-b')
        rows=self.rows()
        self.assertEqual((rows['a6-a']['total_scans'],rows['a6-a']['unique_ips']),(1,1))
        self.assertEqual((rows['a6-b']['total_scans'],rows['a6-b']['unique_ips']),(2,1))
        with fixture.store.connect() as c:
            hashes=[r['visitor_hash'] for r in c.execute('SELECT visitor_hash FROM qr_scans')]
        self.assertEqual(len(set(hashes)),2)
        self.assertNotIn(analytics.actor('192.0.2.51','test-only-secret',1),hashes)

    def test_bots_prefetch_invalid_ips_and_non_get_requests_are_not_counted(self):
        for headers in [{'User-Agent':'Googlebot'},{'User-Agent':'Social preview'},
                        {'Purpose':'prefetch'},{'Sec-Purpose':'prefetch;prerender'}]:
            self.assertEqual(self.scan(**headers).status_code,302)
        self.scan(address='invalid')
        self.assertEqual(self.client.head('/r/a6-a').status_code,302)
        self.assertEqual(self.client.options('/r/a6-a').status_code,200)
        self.assertEqual(self.rows()['a6-a']['total_scans'],0)

    def test_redirect_target_cannot_be_changed_by_query_or_settings(self):
        fixture.store.set_settings({'site_url':'https://example.invalid'})
        response=self.client.get('/r/a6-b?url=https://example.invalid&next=//example.invalid&src=other')
        self.assertEqual(response.location,'https://reklik.shop/?src=a6-b')
        for path in ['/r/unknown','/r/https://example.invalid','/r/a6-a/']:
            self.assertEqual(self.client.get(path).status_code,404)
        response=self.client.post('/r/a6-a')
        self.assertEqual(response.status_code,405)
        self.assertNotIn('Set-Cookie',response.headers)

    def test_recognized_admin_session_is_ignored_without_renewing_cookie(self):
        self.login()
        response=self.scan()
        self.assertEqual(response.status_code,302)
        self.assertNotIn('Set-Cookie',response.headers)
        self.assertEqual(self.rows()['a6-a']['total_scans'],0)
        with fixture.store.connect(True) as c:c.execute('UPDATE sessions SET expires_at=0')
        self.scan()
        self.assertEqual(self.rows()['a6-a']['total_scans'],1)

    def test_counter_failure_does_not_block_customer_redirect(self):
        with patch.object(analytics,'record_qr',side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertLogs(fixture.web.app.logger,level='WARNING') as logs:
                response=self.scan()
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.location,'https://reklik.shop/?src=a6-a')
        self.assertEqual(logs.output,['WARNING:app:QR scan counter temporarily unavailable'])

    def test_admin_summary_requires_login_and_never_claims_purchase_attribution(self):
        self.scan();self.scan();self.scan('a6-b')
        response=self.client.get('/admin/?tab=settings')
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.location,'/admin/login/')
        self.login()
        response=self.client.get('/admin/?tab=settings')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('QR kampanje — A6 leci',html)
        self.assertIn('data-qr-total="a6-a">2<',html)
        self.assertIn('data-qr-unique="a6-a">1<',html)
        self.assertIn('data-qr-total="a6-b">1<',html)
        self.assertIn('Ovo nisu potvrđene kupovine.',html)
        self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertNotIn('qr-analytics',self.client.get('/').get_data(as_text=True))
        self.assertNotIn('qr_scans',self.client.get('/catalog.json').get_data(as_text=True))


if __name__=='__main__':unittest.main()
