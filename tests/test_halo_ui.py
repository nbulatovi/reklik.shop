"""Marketplace links and queued admin requests; no live accounts or production state."""
import sys
import types
import unittest
from unittest.mock import Mock, patch

from flask import render_template
import test_store as fixture


URL='https://www.halooglasi.com/racunari/laptop-racunari/test-uredjaj/5425647927006'


class HaloUITests(unittest.TestCase):
    hidden=fixture.StoreTests.hidden
    login=fixture.StoreTests.login

    def setUp(self):
        fixture.StoreTests.setUp(self)
        self.halo=types.ModuleType('halo_mirror')
        self.halo.public_listing=Mock(return_value=None)
        self.halo.item_status=Mock(return_value={'status':'pending','label':'Čeka objavljivanje','url':None})
        self.halo.request_sync=Mock()
        self.module_patch=patch.dict(sys.modules,{'halo_mirror':self.halo})
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()
        fixture.StoreTests.tearDown(self)

    def active_listing(self):
        return {'status':'active','url':URL,'remote_id':'5425647927006'}

    def product_html(self):
        return self.client.get('/proizvod/1/').get_data(as_text=True)

    def test_public_and_static_contexts_render_verified_link_with_safe_target(self):
        self.halo.public_listing.return_value=self.active_listing()
        body=self.product_html()
        self.assertIn('class="device-halo-link"',body)
        self.assertIn('href="'+URL+'" target="_blank" rel="noopener noreferrer"',body)
        self.assertIn('Pogledaj na Halo oglasima',body)
        self.assertIn('Kupi sada',body)
        self.assertIn('Dodaj u korpu',body)
        with fixture.web.app.test_request_context('/proizvod/1/'):
            exported=render_template('product.html',product=fixture.store.product(1),
                                     live=False,analytics_origin=None,page_title='Test')
        self.assertIn('href="'+URL+'"',exported)

    def test_pending_sold_or_malformed_external_links_are_not_published(self):
        for listing in [None,{'status':'pending','url':URL,'remote_id':'5425647927006'},
                        {'status':'active','url':URL}]:
            with self.subTest(listing=listing):
                self.halo.public_listing.return_value=listing
                self.assertNotIn('class="device-halo-link"',self.product_html())
        for url in ['javascript:alert(1)','http://www.halooglasi.com/test/5425647927006',
                    'https://www.halooglasi.com.evil.invalid/test/5425647927006',
                    'https://www.halooglasi.com@evil.invalid/test/5425647927006',
                    URL+'?redirect=https://evil.invalid',URL+'#fragment']:
            with self.subTest(url=url):
                self.halo.public_listing.return_value=dict(self.active_listing(),url=url)
                self.assertNotIn('class="device-halo-link"',self.product_html())
        self.halo.public_listing.return_value=dict(self.active_listing(),remote_id='9999999999999')
        self.assertNotIn('class="device-halo-link"',self.product_html())
        self.halo.public_listing.return_value=self.active_listing()
        with fixture.store.connect(True) as c:c.execute('UPDATE products SET quantity=0 WHERE id=1')
        self.assertNotIn('class="device-halo-link"',self.product_html())

    def test_admin_status_is_escaped_and_bad_urls_are_suppressed(self):
        self.halo.item_status.return_value={'status':'error','label':'<img src=x onerror=alert(1)>',
            'error':'<script>alert(2)</script>','url':'javascript:alert(3)'}
        body=self.login()
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;',body)
        self.assertIn('&lt;script&gt;alert(2)&lt;/script&gt;',body)
        self.assertNotIn('<img src=x',body)
        self.assertNotIn('href="javascript:',body)
        self.assertIn('Sinhronizuj Halo oglase',body)
        self.halo.item_status.return_value={'status':'active','label':'Aktivan','url':URL,'remote_id':'5425647927006'}
        body=self.client.get('/admin/?edit=1').get_data(as_text=True)
        self.assertIn('Otvori oglas ↗',body)
        self.assertIn('href="'+URL+'"',body)

    def test_sync_requires_admin_and_csrf_and_only_enqueues(self):
        body=self.client.get('/admin/login/').get_data(as_text=True)
        response=self.client.post('/admin/halo-sync',data={'csrf':self.hidden(body,'csrf')})
        self.assertEqual(response.status_code,302)
        self.assertIn('/admin/login/',response.location)
        self.halo.request_sync.assert_not_called()
        body=self.login()
        self.assertEqual(self.client.get('/admin/halo-sync').status_code,405)
        self.assertEqual(self.client.post('/admin/halo-sync',data={'csrf':'incorrect'}).status_code,403)
        self.halo.request_sync.assert_not_called()
        before=fixture.store.product(1,admin=True)
        response=self.client.post('/admin/halo-sync',data={'csrf':self.hidden(body,'csrf')})
        self.assertEqual(response.status_code,302)
        self.halo.request_sync.assert_called_once_with()
        self.assertEqual(fixture.store.product(1,admin=True),before)
        self.assertIn('Halo',response.location)

    def test_sync_failure_does_not_expose_exception_or_break_storefront(self):
        body=self.login()
        self.halo.request_sync.side_effect=RuntimeError('api_key=private-test-only')
        response=self.client.post('/admin/halo-sync',data={'csrf':self.hidden(body,'csrf')},follow_redirects=True)
        body=response.get_data(as_text=True)
        self.assertEqual(response.status_code,200)
        self.assertIn('trenutno nije dostupna',body)
        self.assertNotIn('private-test-only',body)
        self.halo.public_listing.side_effect=OSError('unavailable')
        self.halo.item_status.side_effect=ValueError('bad state')
        response=self.client.get('/proizvod/1/')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('class="device-halo-link"',response.get_data(as_text=True))
        self.assertEqual(self.client.get('/admin/').status_code,200)

    def test_templates_remain_usable_during_backend_code_rollout(self):
        # The old server may briefly see new templates before its restart.
        with fixture.web.app.test_request_context('/proizvod/1/'):
            context=fixture.web.template_context()
            context.pop('halo_listing');context.pop('halo_status')
            context.update(product=fixture.store.product(1),page_title='Test',analytics_origin=None)
            with patch.dict(fixture.web.app.template_context_processors,{None:[]}):
                body=render_template('product.html',**context)
                self.assertIn('Kupi sada',body)
                self.assertNotIn('class="device-halo-link"',body)
                context.update(products=fixture.store.products(admin=True),selected=None,
                               tab='inventory',orders=[],message='',error='',jobs=[])
                body=render_template('admin.html',**context)
                self.assertIn('Vaš inventar',body)
                self.assertNotIn('action="/admin/halo-sync"',body)


if __name__=='__main__':
    unittest.main()
