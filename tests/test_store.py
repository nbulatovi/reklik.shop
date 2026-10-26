"""Financial and order invariants; all state is temporary and all payments are mocked."""
import copy
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(os.environ.get('REKLIK_TEST_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(SOURCE))
TEMP = tempfile.TemporaryDirectory(prefix='reklik-tests-')
os.environ['REKLIK_STATE'] = TEMP.name
from werkzeug.security import generate_password_hash
import store
(store.STATE/'private.json').write_text(json.dumps({'secret':'test-only-secret','password_hash':generate_password_hash('test-only-password')}))
import app as web
import payments
import addresses
import specifications
import inventory_rules
import rates
from datetime import datetime
from zoneinfo import ZoneInfo
import notifications


class StoreTests(unittest.TestCase):
    def setUp(self):
        with store.connect(True) as c:
            for table in ['orders','products','settings','sessions','model_specs','jobs','audit']:
                c.execute('DELETE FROM '+table)
            c.execute('DELETE FROM categories WHERE id>4')
            c.execute("UPDATE categories SET shipping_overrides='{}',weight_kg=5 WHERE id=1")
            c.execute('''INSERT INTO products(id,category_id,title,slug,brand,price_cents,quantity,condition,
              description,specs,images,weight_kg,active,created_at,updated_at) VALUES(1,1,?,?,?,?,?,?,?,?,?,5,1,?,?)''',
              ('Test ThinkPad Čačak','test-thinkpad','Lenovo',30000,1,inventory_rules.GRADES[1],'Testirani uređaj',
               json.dumps({'Procesor':'Core i5','Memorija':'16 GB'}),'[]',store.now(),store.now()))
        store.set_settings({'eur_rsd':'117.2','eur_rsd_date':datetime.now(ZoneInfo('Europe/Belgrade')).date().isoformat(),'eur_rsd_source':rates.SOURCE})
        with store.connect(True) as c:c.execute('UPDATE products SET base_price_cents=28500 WHERE id=1')
        web.login_attempts.clear()
        self.client=web.app.test_client()
        self.publish=patch.object(web,'queue_publish');self.publish.start()
        self.ai_worker=patch.object(specifications.WORKERS,'submit');self.ai_worker.start()

    def tearDown(self):
        self.publish.stop()
        self.ai_worker.stop()

    def hidden(self,body,name):
        match=re.search(r'name="'+re.escape(name)+r'" value="([^"]*)"',body)
        self.assertIsNotNone(match,name)
        return match.group(1)

    def checkout_form(self,**overrides):
        response=self.client.get('/checkout/?buy=1')
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        data={key:self.hidden(html,key) for key in ['csrf','idem','idem_signature','quoted_total','quoted_currency']}
        data.update(items=json.dumps([{'id':1,'qty':1,'warranty':0}]),name='Петар Петровић',phone='+381 63 123 4567',
                    email='buyer@example.test',country='RS',delivery_method='shipping',courier='post-express',
                    address='Булевар Михаила Пупина 10З',city='Нови Београд',postal_code='11070',payment_method='cod',terms='yes')
        data.update(overrides)
        return data

    def order(self,method='cod',idem='one',**overrides):
        delivery={'country':'RS','method':'shipping','courier':'post-express','address':'','city':'Selo','postal_code':''}
        delivery.update(overrides)
        return store.create_order([{'id':1,'qty':1,'warranty':0}],{'name':'Test','phone':'063123456','email':'buyer@example.test'},delivery,method,idem)

    def login(self):
        html=self.client.get('/admin/login/').get_data(as_text=True)
        token=self.hidden(html,'csrf')
        response=self.client.post('/admin/login/',data={'csrf':token,'password':'test-only-password'})
        self.assertEqual(response.status_code,302)
        return self.client.get('/admin/').get_data(as_text=True)

    def test_cod_full_cyrillic_address(self):
        response=self.client.post('/checkout/',data=self.checkout_form())
        self.assertEqual(response.status_code,302)
        with store.connect() as c:
            order=store.unpack(c.execute('SELECT * FROM orders').fetchone())
        self.assertEqual(order['payment_method'],'cod')
        self.assertEqual(order['status'],'new')
        self.assertEqual(order['customer']['name'],'Петар Петровић')
        self.assertEqual(order['delivery']['address'],'Булевар Михаила Пупина 10З')
        self.assertEqual(order['delivery']['address_confirmation'],'phone_required')
        self.assertEqual(order['total_minor'],3516000+60000)
        self.assertEqual(store.product(1)['quantity'],0)
        page=self.client.get(response.location).get_data(as_text=True)
        self.assertIn('Pozvaćemo vas',page)
        self.assertIn('Sačuvaj potvrdu',page)

    def test_village_without_street_or_postcode(self):
        response=self.client.post('/checkout/',data=self.checkout_form(address='',city='Горња Добриња',postal_code=''))
        self.assertEqual(response.status_code,302)
        with store.connect() as c: order=store.unpack(c.execute('SELECT * FROM orders').fetchone())
        self.assertEqual(order['delivery']['city'],'Горња Добриња')
        self.assertEqual(order['delivery']['address'],'')

    def test_repeated_web_submission_returns_same_order(self):
        data=self.checkout_form()
        first=self.client.post('/checkout/',data=data)
        second=self.client.post('/checkout/',data=data)
        self.assertEqual(first.status_code,302)
        self.assertEqual(second.status_code,303)
        self.assertEqual(first.location,second.location)
        self.assertEqual(store.product(1)['quantity'],0)
        with store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],1)

    def test_card_goes_directly_to_processor_without_marking_paid(self):
        with patch.object(payments,'create_checkout',return_value='https://whop.com/checkout/ch_test/'):
            response=self.client.post('/checkout/',data=self.checkout_form(payment_method='card'))
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.location,'https://whop.com/checkout/ch_test/')
        with store.connect() as c:order=dict(c.execute('SELECT * FROM orders').fetchone())
        self.assertEqual(order['status'],'awaiting_payment')
        self.assertIsNone(order['payment_id'])

    def test_card_service_failure_retains_order_for_retry(self):
        with patch.object(payments,'create_checkout',side_effect=ValueError('Temporarily unavailable')):
            response=self.client.post('/checkout/',data=self.checkout_form(payment_method='card'))
        self.assertEqual(response.status_code,303)
        self.assertIn('/potvrda/?order=',response.location)
        with store.connect() as c:order=dict(c.execute('SELECT * FROM orders').fetchone())
        self.assertEqual(order['status'],'awaiting_payment')
        self.assertEqual(store.product(1)['quantity'],0)

    def test_checkout_price_change_requires_confirmation(self):
        data=self.checkout_form()
        with store.connect(True) as c:c.execute('UPDATE products SET price_cents=31000 WHERE id=1')
        response=self.client.post('/checkout/',data=data)
        self.assertEqual(response.status_code,422)
        self.assertIn('Iznos je ažuriran',response.get_data(as_text=True))
        self.assertEqual(store.product(1)['quantity'],1)

    def test_transaction_rechecks_accepted_price_before_reserving(self):
        q=store.quote([{'id':1}])
        with store.connect(True) as c:c.execute('UPDATE products SET price_cents=31000 WHERE id=1')
        with self.assertRaises(ValueError):
            store.create_order([{'id':1}],{}, {'country':'RS','method':'shipping','courier':'post-express'},'cod','race',expected=(q['currency'],q['total_minor']))
        self.assertEqual(store.product(1)['quantity'],1)

    def test_embedded_cookie_probe_detects_missing_cookie(self):
        self.client.get('/admin/login/')
        self.assertTrue(self.client.get('/api/session').json['ready'])
        fresh=web.app.test_client(use_cookies=False)
        self.assertFalse(fresh.get('/api/session').json['ready'])

    def test_checkout_configuration_keeps_currency_and_card_only(self):
        order=self.order(method='card')
        with patch.object(payments,'credentials',return_value={'WHOP_COMPANY_ID':'biz_test'}),patch.object(payments,'api',return_value={'id':'ch_test','plan':{'id':'plan_test'},'purchase_url':'https://whop.com/checkout/ch_test/'}) as api:
            payments.create_checkout(order,'https://example.test')
        payload=api.call_args.kwargs['json']
        self.assertFalse(payload['plan']['adaptive_pricing_enabled'])
        self.assertEqual(payload['plan']['stock'],1)
        self.assertEqual(payload['payment_method_configuration']['enabled'],['card'])
        self.assertFalse(payload['payment_method_configuration']['include_platform_defaults'])
        self.assertTrue(payload['plan']['product']['custom_statement_descriptor'].startswith('WHOP*'))

    def test_ascii_serbian_names_accepted(self):
        response=self.client.post('/checkout/',data=self.checkout_form(name='Djordje Zivkovic',address='Vojvode Stepe 8',city='Cacak'))
        self.assertEqual(response.status_code,302)

    def test_invalid_supplied_postcode_is_correctable(self):
        response=self.client.post('/checkout/',data=self.checkout_form(postal_code='123'))
        self.assertEqual(response.status_code,422)
        self.assertIn('ostavite polje prazno',response.get_data(as_text=True))
        self.assertEqual(store.product(1)['quantity'],1)

    def test_carrier_specific_weight_bands(self):
        cfg=store.settings();cfg['currency']='RSD'
        expected={'post-express':60000,'aks':82000,'d-express':92400}
        for courier,fee in expected.items():
            q=store.quote([{'id':1}],courier=courier,cfg=cfg)
            self.assertEqual(q['shipping_minor'],fee,courier)
        with store.connect(True) as c:c.execute('UPDATE products SET weight_kg=1 WHERE id=1')
        for courier,fee in {'post-express':45000,'aks':52000,'d-express':50000}.items():
            self.assertEqual(store.quote([{'id':1}],courier=courier,cfg=cfg)['shipping_minor'],fee)

    def test_carrier_boundaries_and_fuel_not_added_twice(self):
        cfg=store.settings();cfg['currency']='RSD'
        for weight,carrier,fee in [(2,'post-express',50000),(5.01,'post-express',80000),(20,'post-express',110000),(15,'aks',135000),(50,'d-express',239880)]:
            with store.connect(True) as c:c.execute('UPDATE products SET weight_kg=? WHERE id=1',(weight,))
            self.assertEqual(store.quote([{'id':1}],courier=carrier,cfg=cfg)['shipping_minor'],fee)
        with store.connect(True) as c:c.execute('UPDATE products SET weight_kg=21 WHERE id=1')
        with self.assertRaises(ValueError):store.quote([{'id':1}],courier='post-express',cfg=cfg)

    def test_category_overrides_and_free_shipping(self):
        with store.connect(True) as c:c.execute('UPDATE categories SET shipping_overrides=? WHERE id=1',(json.dumps({'post-express':350,'aks':0}),))
        cfg=store.settings();cfg['currency']='RSD'
        self.assertEqual(store.quote([{'id':1}],courier='post-express',cfg=cfg)['shipping_minor'],35000)
        self.assertEqual(store.quote([{'id':1}],courier='aks',cfg=cfg)['shipping_minor'],0)
        self.assertEqual(store.quote([{'id':1}],courier='d-express',cfg=cfg)['shipping_minor'],92400)

    def test_cod_has_no_surcharge(self):
        cod=self.order()
        self.assertEqual(cod['total_minor'],cod['subtotal_minor']+cod['shipping_minor'])

    def test_pickup_free_and_switch_respected(self):
        q=store.quote([{'id':1}],delivery='pickup')
        self.assertEqual(q['shipping_minor'],0)
        store.set_settings({'pickup_enabled':False})
        with self.assertRaises(ValueError):store.quote([{'id':1}],delivery='pickup')

    def test_international_off_default_and_server_enforced(self):
        self.assertFalse(store.settings()['international_enabled'])
        with self.assertRaises(ValueError):store.quote([{'id':1}],country='DE')
        with self.assertRaises(ValueError):self.order(country='DE')
        self.assertEqual(store.product(1)['quantity'],1)

    def test_international_toggle_does_not_allow_cod(self):
        store.set_settings({'international_enabled':True})
        self.assertGreater(store.quote([{'id':1}],country='DE')['shipping_minor'],0)
        with self.assertRaises(ValueError):self.order(country='DE')

    def test_stock_race_only_one_order(self):
        def buy(i):
            try:self.order(idem='buyer-'+str(i));return True
            except ValueError:return False
        with ThreadPoolExecutor(max_workers=6) as pool:results=list(pool.map(buy,range(12)))
        self.assertEqual(sum(results),1)
        self.assertEqual(store.product(1)['quantity'],0)

    def test_idempotency_does_not_reduce_stock_twice(self):
        a=self.order(idem='same');b=self.order(idem='same')
        self.assertEqual(a['id'],b['id'])
        with store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],1)

    def test_aggregate_same_product_quantity_rejected(self):
        with self.assertRaises(ValueError):store.quote([{'id':1,'qty':1,'warranty':0},{'id':1,'qty':1,'warranty':1}])

    def test_expired_reservation_released_once(self):
        order=self.order(method='card')
        with store.connect(True) as c:c.execute('UPDATE orders SET expires_at=? WHERE id=?',(store.now()-1,order['id']))
        store.release_expired();store.release_expired()
        self.assertEqual(store.product(1)['quantity'],1)

    def test_cancel_releases_once(self):
        order=self.order()
        store.change_order_status(order['id'],'cancelled');store.change_order_status(order['id'],'cancelled')
        self.assertEqual(store.product(1)['quantity'],1)

    def test_dispatch_requires_phone_confirmation(self):
        order=self.order()
        with self.assertRaises(ValueError):store.change_order_status(order['id'],'shipped')
        store.change_order_status(order['id'],'confirmed');store.change_order_status(order['id'],'shipped')
        with self.assertRaises(ValueError):store.change_order_status(order['id'],'cancelled')

    def test_currency_conversion_and_order_snapshot(self):
        store.set_settings({'currency':'RSD','eur_rsd':'117.2'})
        order=self.order()
        self.assertEqual(order['subtotal_minor'],3516000)
        self.assertEqual(order['currency'],'RSD')
        store.set_settings({'currency':'EUR','eur_rsd':'120'})
        self.assertEqual(store.order_by_token(order['token'])['total_minor'],order['total_minor'])
        self.assertEqual(store.product(1)['price_cents'],30000)

    def test_warranty_is_optional_and_itemized(self):
        cfg=store.settings();cfg['currency']='RSD'
        q=store.quote([{'id':1,'warranty':3}],cfg=cfg)
        self.assertEqual(q['warranty_minor'],351600)
        self.assertEqual(store.quote([{'id':1}],cfg=cfg)['warranty_minor'],0)

    def test_warranty_is_14_days_plus_fixed_eur_monthly_fees(self):
        for months in (1,2,3):
            before=store.quote([{'id':1,'warranty':months}])
            self.assertEqual(before['items'][0]['included_warranty_days'],14)
            self.assertEqual(before['items'][0]['warranty_eur_cents'],months*1000)
            self.assertEqual(before['warranty_minor'],months*117200)
            with store.connect(True) as c:c.execute('UPDATE products SET price_cents=990000 WHERE id=1')
            self.assertEqual(store.quote([{'id':1,'warranty':months}])['warranty_minor'],before['warranty_minor'])
        import publisher
        self.assertEqual(publisher.public_catalog()['warranty_minor'],{'1':1000,'2':2000,'3':3000})
        self.assertEqual(store.quote([{'id':1}])['pricing']['included_warranty_days'],14)

    def test_saved_warranty_coverage_and_cost_do_not_change_with_settings(self):
        delivery={'country':'RS','method':'shipping','courier':'post-express','city':'Selo'}
        order=store.create_order([{'id':1,'warranty':2}],{},delivery,'cod','warranty-snapshot')
        store.set_settings({'included_warranty_days':30,'warranty_eur_cents':{'1':5000,'2':6000,'3':7000}})
        saved=store.order_by_token(order['token'])
        self.assertEqual(saved,order);self.assertEqual(saved['pricing']['included_warranty_days'],14)
        self.assertEqual(saved['items'][0]['warranty_eur_cents'],2000)

    def test_public_admin_and_post_protection(self):
        self.assertEqual(self.client.get('/admin/').status_code,302)
        self.assertEqual(self.client.post('/admin/product',data={'title':'Intruder'}).status_code,403)
        self.assertEqual(self.client.post('/admin/settings',data={}).status_code,403)
        self.assertEqual(self.client.post('/webhooks/whop',json={'type':'payment.succeeded'}).status_code,401)

    def test_login_and_mobile_camera_controls(self):
        html=self.login()
        self.assertIn('capture="environment"',html)
        self.assertIn('Dupliraj',html);self.assertIn('Obriši trajno',html)
        self.assertEqual(self.client.get('/admin/?edit=1').status_code,200)
        self.assertEqual(self.client.get('/admin/?tab=settings').status_code,200)

    def test_admin_revision_prevents_overwriting_new_sale(self):
        html=self.login();csrf=self.hidden(html,'csrf')
        self.order()
        response=self.client.post('/admin/product',data={'csrf':csrf,'id':1,'revision':1,'title':'Edited title','price':'300','category_id':1,'quantity':1,'condition':'Refurbished — odlično'})
        self.assertEqual(response.status_code,302)
        self.assertIn('error=',response.location)
        self.assertEqual(store.product(1)['quantity'],0)

    def test_private_confirmation_not_in_public_catalog(self):
        order=self.order()
        data=self.client.get('/catalog.json').get_data(as_text=True)
        self.assertNotIn(order['token'],data);self.assertNotIn('buyer@example.test',data)
        self.assertIn('noindex',self.client.get('/order/'+order['token']+'/').get_data(as_text=True))

    def test_forged_return_does_not_mark_paid(self):
        order=self.order(method='card')
        with patch.object(payments,'reconcile',return_value=False):self.client.get('/order/'+order['token']+'/?return=1&paid=true')
        self.assertEqual(store.order_by_token(order['token'])['status'],'awaiting_payment')

    def payment(self,order):
        return {'id':'pay_test','status':'paid','currency':order['currency'].lower(),'total':order['total_minor']/100,
                'company':{'id':'biz_test'},'plan':{'id':'plan_test'},'metadata':{'order_id':order['id']}}

    def test_payment_verifies_all_financial_fields(self):
        order=self.order(method='card')
        with store.connect(True) as c:c.execute("UPDATE orders SET plan_id='plan_test' WHERE id=?",(order['id'],))
        order=store.order_by_token(order['token']);valid=self.payment(order)
        with patch.object(payments,'credentials',return_value={'WHOP_COMPANY_ID':'biz_test'}):
            for field,value in [('total',1),('currency','usd'),('status','pending'),('plan',{'id':'wrong'}),('company',{'id':'wrong'})]:
                wrong=copy.deepcopy(valid);wrong[field]=value
                self.assertFalse(payments.verify_payment(order,wrong),field)
            self.assertTrue(payments.verify_payment(order,valid))
            self.assertTrue(payments.verify_payment(order,valid))
        self.assertEqual(store.order_by_token(order['token'])['status'],'paid')

    def test_unpaid_card_cannot_be_dispatched(self):
        order=self.order(method='card')
        with self.assertRaises(ValueError):store.change_order_status(order['id'],'confirmed')

    def test_confirmed_card_order_never_says_pay_on_delivery(self):
        order=self.order(method='card')
        with store.connect(True) as c:c.execute("UPDATE orders SET status='paid',payment_id='pay_test' WHERE id=?",(order['id'],))
        store.change_order_status(order['id'],'confirmed')
        html=self.client.get('/order/'+order['token']+'/').get_data(as_text=True)
        self.assertIn('Vaša uplata je primljena',html)
        self.assertNotIn('Plaćate prilikom isporuke',html)

    def test_cancel_payment_test_does_not_touch_inventory(self):
        order=payments.test_order()
        with store.connect(True) as c:c.execute("UPDATE orders SET status='paid',payment_id='pay_test',stock_held=1 WHERE id=?",(order['id'],))
        store.change_order_status(order['id'],'cancelled')
        self.assertEqual(store.product(1)['quantity'],1)

    def test_refurbished_seo_and_xss(self):
        store.set_settings({'indexing_enabled':True})
        with store.connect(True) as c:c.execute('UPDATE products SET title=? WHERE id=1',('<script>alert(1)</script>',))
        html=self.client.get('/proizvod/test-thinkpad/').get_data(as_text=True)
        self.assertIn('RefurbishedCondition',html);self.assertIn('application/ld+json',html)
        self.assertNotIn('<h1><script>',html);self.assertIn('&lt;script&gt;',html)
        self.assertNotIn('name="robots" content="noindex',html)

    def test_search_accepts_scripts_and_ascii(self):
        for query in ['Čačak','Cacak','Чачак','Chachak']:
            self.assertEqual(len(store.products(query=query)),1,query)

    def test_whole_prices_drop_zero_decimals_but_keep_real_cents(self):
        self.assertEqual(store.display_money(34900,'EUR'),'349 €')
        self.assertEqual(store.display_money(34950,'EUR'),'349,50 €')
        self.assertEqual(store.display_money(239880,'RSD'),'2.398,80 RSD')

    def test_item_views_are_unique_per_ip_and_product(self):
        first={'CF-Connecting-IP':'203.0.113.7','User-Agent':'Mozilla/5.0'}
        second={'CF-Connecting-IP':'203.0.113.8','User-Agent':'Mozilla/5.0'}
        for _ in range(3):
            response=self.client.get('/views/product/1',headers=first)
            self.assertEqual(response.status_code,204)
            self.assertNotIn('Set-Cookie',response.headers)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.client.get('/views/product/1',headers=second)
        self.assertEqual(store.products(admin=True)[0]['unique_views'],2)
        with store.connect() as c:
            records=[dict(row) for row in c.execute('SELECT * FROM item_views')]
        self.assertEqual(len(records),2)
        self.assertTrue(all(len(r['visitor_hash'])==64 for r in records))
        self.assertNotIn('203.0.113',json.dumps(records))
        self.assertNotIn('unique_views',self.client.get('/catalog.json').get_data(as_text=True))

    def test_item_views_normalize_ips_and_ignore_bots(self):
        for address in ['192.0.2.9','::ffff:192.0.2.9']:
            self.client.get('/views/product/1',headers={'CF-Connecting-IP':address,'User-Agent':'Mozilla/5.0'})
        self.client.get('/views/product/1',headers={'CF-Connecting-IP':'192.0.2.10','User-Agent':'Googlebot'})
        self.client.get('/views/product/1',headers={'CF-Connecting-IP':'invalid','User-Agent':'Mozilla/5.0'})
        self.assertEqual(store.products(admin=True)[0]['unique_views'],1)
        self.assertEqual(self.client.get('/views/product/9999').status_code,404)

    def test_inventory_specifications_require_admin_and_csrf(self):
        import specifications
        self.assertEqual(self.client.post('/admin/specifications',json={'model':'T14'}).status_code,403)
        html=self.login();csrf=self.hidden(html,'csrf')
        with patch.object(specifications,'start',return_value={'status':'completed','result':{'specs':{'Ekran':'14 inča'}}}) as start:
            result=self.client.post('/admin/specifications',json={'model':'T14','brand':'Lenovo'},headers={'X-CSRF-Token':csrf})
        self.assertEqual(result.status_code,200)
        start.assert_called_once_with('T14','Lenovo')

    def test_specification_results_require_official_sources(self):
        import specifications
        data={'title':'ThinkPad T14','brand':'Lenovo','description':'Opis','category_slug':'laptopovi','notes':'',
          'specs':[{'name':'Ekran','value':'14 inča'},{'name':'RAM','value':'16 GB'}],
          'sources':[{'title':'Source','url':'https://untrusted.example/specs'}]}
        with self.assertRaises(ValueError):specifications.validate(data)
        data['sources'][0]['url']='https://psref.lenovo.com/Product/ThinkPad/ThinkPad_T14_Gen_2_Intel'
        result=specifications.validate(data)
        self.assertEqual(result['specs']['RAM'],'16 GB')

    def test_specs_cache_prevents_repeated_cli_jobs(self):
        import specifications,hashlib
        key=hashlib.sha256(('missing-v2|'+store.fold('Dell')+'|'+store.fold('Latitude 5420')+'|{}').encode()).hexdigest()
        with store.connect(True) as c:c.execute('INSERT INTO model_specs VALUES(?,?,?,NULL)',(key,json.dumps({'specs':{'Ekran':'14 inča'}}),store.now()))
        with patch.object(specifications.WORKERS,'submit') as submit:
            result=specifications.start('Latitude 5420','Dell')
        self.assertEqual(result['status'],'completed');submit.assert_not_called()

    def ai_result(self):
        return {'status':'completed','message':'Specifikacije su pronađene.','result':{
            'specs':{'Ekran':'6,1 inča','Procesor':'Apple A15'},'description':'Opis iz dokumentacije proizvođača.',
            'category_slug':'telefoni','notes':'','sources':[{'url':'https://www.apple.com/iphone/','title':'Apple'}]}}

    def test_inventory_saves_without_waiting_and_enriches_after_navigation(self):
        html=self.login()
        data={'csrf':self.hidden(html,'csrf'),'title':'Apple iPhone 13','brand':'Apple','price':'250',
              'quantity':'2','category_id':'1','specs':'','description':'','auto_specs':'1','auto_description':'1','auto_category_id':'1'}
        response=self.client.post('/admin/product',data=data)
        self.assertEqual(response.status_code,302)
        with store.connect() as c:
            task=dict(c.execute('SELECT * FROM product_ai').fetchone())
        pid=task['product_id']
        self.assertEqual(task['status'],'pending')
        self.assertEqual(store.product(pid,admin=True)['specs'],{})
        # The next-entry page and a second session do not control the worker.
        self.client.get('/admin/')
        result=self.ai_result();specifications.update(task['job_id'],'completed',{'result':result['result'],'message':result['message']})
        product=store.product(pid,admin=True)
        self.assertEqual(product['specs'],result['result']['specs'])
        self.assertEqual(product['category_slug'],'telefoni')
        self.assertEqual(product['price_cents'],26500);self.assertEqual(product['base_price_cents'],25000);self.assertEqual(product['quantity'],2)
        self.assertEqual(self.client.get('/admin/inventory-status').json[0]['status'],'completed')

    def test_background_ai_preserves_manual_edits_and_order_stock(self):
        specifications.queue_product(1,['specs','description','category_id'])
        with store.connect(True) as c:
            job=c.execute('SELECT job_id FROM product_ai WHERE product_id=1').fetchone()[0]
            c.execute("UPDATE products SET description='Moj ručni opis',specs=?,price_cents=41000,quantity=0,revision=revision+1 WHERE id=1",(json.dumps({'RAM':'32 GB'}),))
        specifications.finish_product(1,self.ai_result(),job)
        product=store.product(1,admin=True)
        self.assertEqual(product['description'],'Moj ručni opis');self.assertEqual(product['specs']['RAM'],'32 GB');self.assertEqual(product['specs']['Ekran'],'6,1 inča')
        self.assertEqual(product['price_cents'],41000);self.assertEqual(product['quantity'],0)

    def test_background_ai_does_not_overwrite_new_model_or_manual_fields(self):
        specifications.queue_product(1,[])
        with store.connect() as c:job=c.execute('SELECT job_id FROM product_ai').fetchone()[0]
        specifications.finish_product(1,self.ai_result(),job)
        self.assertEqual(store.product(1)['specs'],{'Procesor':'Core i5','Memorija':'16 GB'})
        specifications.queue_product(1,['specs','description'])
        with store.connect(True) as c:
            job=c.execute('SELECT job_id FROM product_ai').fetchone()[0]
            c.execute("UPDATE products SET title='Drugi model' WHERE id=1")
        specifications.finish_product(1,self.ai_result(),job)
        self.assertEqual(store.product(1)['description'],'Testirani uređaj')
        with store.connect() as c:self.assertEqual(c.execute('SELECT status FROM product_ai').fetchone()[0],'cancelled')

    def test_background_ai_cached_completion_and_restart_recovery(self):
        data=self.ai_result()
        with store.connect(True) as c:c.execute("UPDATE products SET description='' WHERE id=1")
        with patch.object(specifications,'start',return_value=data):specifications.queue_product(1,['description'])
        self.assertEqual(store.product(1)['description'],data['result']['description'])
        specifications.queue_product(1,['specs'])
        with store.connect() as c:old=c.execute('SELECT job_id FROM product_ai').fetchone()[0]
        specifications.recover()
        with store.connect() as c:
            new=c.execute('SELECT job_id FROM product_ai').fetchone()[0]
        self.assertNotEqual(old,new)
        self.assertEqual(specifications.status(old)['status'],'failed')
        specifications.update(new,'completed',data)
        self.assertEqual(store.product(1)['specs'],{'Procesor':'Core i5','RAM':'16 GB','Ekran':'6,1 inča'})

    def test_ai_inventory_status_is_private_and_admin_button_is_at_footer(self):
        self.assertEqual(self.client.get('/admin/inventory-status').status_code,302)
        html=self.client.get('/').get_data(as_text=True)
        self.assertIn('footer-admin',html)
        self.assertLess(html.index('data-product-grid'),html.index('footer-admin'))

    def test_specs_merge_only_missing_with_consistent_labels(self):
        manual=inventory_rules.parse_specs('cpu: Intel Core i5\nmemorija: 16 GB\ndisk: 512 GB SSD\nWi-Fi: Moj unos')
        merged=inventory_rules.merge_missing(manual,{'Procesor':'Drugi CPU','RAM':'8 GB','SSD':'128 GB','Display':'14 inča','Wi-Fi':'Drugo'})
        self.assertEqual(merged,{'Procesor':'Intel Core i5','RAM':'16 GB','Skladište':'512 GB SSD','Ekran':'14 inča','Wi-Fi':'Moj unos'})
        repeated=inventory_rules.parse_specs('RAM: 8 GB\nRAM: 16 GB\nRAM: 32 GB\nPodatak bez dvotačke')
        self.assertEqual(len(repeated),4)
        self.assertIn('32 GB',repeated.values())

    def test_spec_jobs_are_specific_to_manual_configuration(self):
        first=specifications.start('ThinkPad T14','Lenovo',{'RAM':'8 GB'})
        second=specifications.start('ThinkPad T14','Lenovo',{'RAM':'32 GB'})
        same=specifications.start('ThinkPad T14','Lenovo',{'Memorija':'8 GB'})
        self.assertNotEqual(first['id'],second['id']);self.assertEqual(first['id'],same['id'])

    def test_condition_migration_is_conservative_and_repeatable(self):
        pairs=[('Novo','',0),('Novo-sitni tragovi na kućištu','',2),('Refurbished — odlično','',1),
               ('U veoma dobrom stanju','',2),('Obnovljeno-B grade','Par ogrebotina na staklu',3),
               ('Refurbished — veoma dobro','Telefon ima tragova upotrebe',4),('Grade C','',5)]
        for old,description,index in pairs:
            self.assertEqual(inventory_rules.grade(old,description),inventory_rules.GRADES[index])
        with store.connect(True) as c:
            c.execute("DELETE FROM migrations WHERE name='refabrikovani-grades-v1'")
            c.execute("UPDATE products SET condition=?,description=? WHERE id=1",('Novo-sitni tragovi na kućištu','Obnovljen uređaj, moj opis.'))
        before=store.product(1,admin=True)
        store.init_db();after=store.product(1,admin=True)
        for key in ('price_cents','base_price_cents','quantity','images','specs','active'):
            self.assertEqual(before[key],after[key])
        self.assertEqual(after['condition'],inventory_rules.GRADES[2]);self.assertIn('Refabrikovan',after['description'])
        revision=after['revision'];store.init_db()
        self.assertEqual(store.product(1,admin=True)['revision'],revision)

    def photo_product(self):
        from PIL import Image
        html=self.login();csrf=self.hidden(html,'csrf')
        image=io.BytesIO();Image.new('RGB',(3200,2400),'orange').save(image,'JPEG');image.seek(0)
        response=self.client.post('/admin/product',data={'csrf':csrf,'title':'Photo device','brand':'Dell','price':'200','quantity':'2','category_id':'1','active':'yes','specs':'RAM: 16 GB','photos':(image,'camera.jpg')})
        self.assertEqual(response.status_code,302)
        return csrf,next(p for p in store.products(admin=True) if p['title']=='Photo device')

    def test_photo_resolution_and_visibility(self):
        from PIL import Image
        csrf,product=self.photo_product();image=product['images'][0]
        with Image.open(store.UPLOADS/image) as im:self.assertEqual(im.size,(1600,1200))
        with Image.open(store.ORIGINALS/image.replace('.webp','.jpg')) as im:self.assertEqual(im.size,(3200,2400))
        self.assertEqual(product['active'],1);self.assertEqual(product['condition'],inventory_rules.GRADES[1])
        page=self.client.get('/proizvod/'+product['slug']+'/').get_data(as_text=True)
        self.assertIn('/media/full/',page);self.assertIn('data-photo-zoom',page)
        with self.client.get('/media/full/'+image.replace('.webp','.jpg')) as response:self.assertEqual(response.status_code,200)
        self.client.post('/admin/product/'+str(product['id'])+'/visibility',data={'csrf':csrf,'visible':'0'})
        self.assertIsNone(store.product(product['id']));self.assertIsNotNone(store.product(product['id'],admin=True))
        self.assertEqual(self.client.get('/proizvod/'+product['slug']+'/').status_code,404)
        import publisher
        self.assertNotIn(product['id'],[p['id'] for p in publisher.public_catalog()['products']])
        self.client.post('/admin/product/'+str(product['id'])+'/visibility',data={'csrf':csrf,'visible':'1'})
        self.assertIsNotNone(store.product(product['id']))

    def test_duplicate_and_delete_preserve_shared_photos_and_order_history(self):
        csrf,product=self.photo_product();pid=product['id'];image=product['images'][0]
        # Capture an immutable order before removing the inventory entry.
        order=store.create_order([{'id':pid,'qty':1,'warranty':0}],{'name':'Buyer','phone':'063123456','email':''},
                  {'country':'RS','method':'pickup','courier':'post-express','city':'','address':'','postal_code':''},'cod','delete-history')
        self.client.post('/admin/product/'+str(pid)+'/duplicate',data={'csrf':csrf})
        duplicate=next(p for p in store.products(admin=True) if p['title']==product['title'] and p['id']!=pid)
        self.assertEqual(duplicate['quantity'],1);self.assertEqual(duplicate['active'],1)
        for key in ('images','specs','base_price_cents','price_cents','condition','description'):
            self.assertEqual(duplicate[key],product[key])
        self.assertNotEqual(duplicate['slug'],product['slug'])
        self.client.post('/admin/product/'+str(pid)+'/delete',data={'csrf':csrf,'revision':store.product(pid,admin=True)['revision']})
        self.assertIsNone(store.product(pid,admin=True));self.assertTrue((store.UPLOADS/image).is_file())
        self.assertEqual(store.order_by_token(order['token'])['items'],order['items'])
        self.client.post('/admin/product/'+str(duplicate['id'])+'/delete',data={'csrf':csrf,'revision':duplicate['revision']})
        self.assertFalse((store.UPLOADS/image).exists());self.assertFalse((store.ORIGINALS/image.replace('.webp','.jpg')).exists())
        with store.connect(True) as c:new_id=store.next_product_id(c)
        self.assertGreater(new_id,max(pid,duplicate['id']))
        store.change_order_status(order['id'],'cancelled')
        self.assertIsNone(store.product(pid,admin=True))
        with store.connect() as c:
            self.assertIsNone(c.execute('SELECT * FROM product_ai WHERE product_id=?',(pid,)).fetchone())

    def test_inventory_mutations_require_admin_csrf_and_current_revision(self):
        for action in ('visibility','duplicate','delete'):
            self.assertEqual(self.client.post('/admin/product/1/'+action,data={}).status_code,403)
        html=self.login();csrf=self.hidden(html,'csrf')
        for action in ('visibility','duplicate','delete'):
            self.assertEqual(self.client.post('/admin/product/1/'+action,data={'csrf':'forged'}).status_code,403)
        response=self.client.post('/admin/product/1/delete',data={'csrf':csrf,'revision':999})
        self.assertIn('error=',response.location);self.assertIsNotNone(store.product(1,admin=True))
        page=self.client.get('/admin/').get_data(as_text=True)
        self.assertEqual(page.count('<option value="Refabrikovano -'),5)
        with store.connect(True) as c:c.execute("UPDATE products SET condition='Novo' WHERE id=1")
        self.assertIn('https://schema.org/NewCondition',self.client.get('/proizvod/test-thinkpad/').get_data(as_text=True))

    def test_domain_does_not_cut_over_while_dns_points_to_parking(self):
        import domain,publisher
        before=store.settings()
        with patch.object(domain,'dns_ready',return_value=False),patch.object(domain,'state'),patch.object(domain,'github') as github,patch.object(publisher,'publish') as publish:
            self.assertFalse(domain.step())
        github.assert_not_called();publish.assert_not_called()
        self.assertEqual(store.settings(),before)

    def test_domain_rejects_conflicting_dns_records(self):
        import domain
        cases=[({'192.64.119.23'},set(),set(),False),(domain.GITHUB_IPS,{'nbulatovi.github.io'},set(),True),
               (domain.GITHUB_IPS,{'parkingpage.namecheap.com'},set(),False),
               (domain.GITHUB_IPS,{'nbulatovi.github.io'},{'2001:db8::1'},False)]
        for apex,www,ipv6,expected in cases:
            with patch.object(domain,'records',side_effect=lambda host,kind:apex if kind=='A' else www if kind=='CNAME' else ipv6):
                self.assertEqual(domain.dns_ready(),expected)

    def test_domain_enables_indexing_only_after_https_and_live_checks(self):
        import domain,publisher
        pages={'cname':'reklik.shop','https_certificate':{'state':'pending'},'https_enforced':False}
        with patch.object(domain,'dns_ready',return_value=True),patch.object(domain,'state'),patch.object(domain,'github',return_value=pages),patch.object(publisher,'publish'),patch.object(domain,'healthy') as healthy:
            self.assertFalse(domain.step());healthy.assert_not_called()
        self.assertEqual(store.settings()['site_url'],'https://reklik.shop')
        self.assertFalse(store.settings()['indexing_enabled'])
        pages.update(https_certificate={'state':'approved'},https_enforced=True)
        with patch.object(domain,'dns_ready',return_value=True),patch.object(domain,'state'),patch.object(domain,'github',return_value=pages),patch.object(publisher,'publish'),patch.object(domain,'healthy') as healthy:
            self.assertTrue(domain.step())
        self.assertTrue(store.settings()['indexing_enabled'])
        self.assertEqual(healthy.call_count,2)
        healthy.assert_called_with(indexed=True)

    def test_domain_health_requires_root_links_live_backend_and_indexing(self):
        import domain
        origin='https://reklik.shop'
        bodies={origin+'/':'<body data-base=""><a class="footer-admin">Admin</a>',
                'https://www.reklik.shop/':'<a class="footer-admin">Admin</a>',
                origin+'/catalog.json':json.dumps({'products':[]}),
                origin+'/backend.json':json.dumps({'backend':'https://qa.trycloudflare.com'}),
                origin+'/admin/':'data-frame-kind="admin"',origin+'/checkout/':'data-frame-kind="checkout"',
                origin+'/sitemap.xml':'<loc>https://reklik.shop/kategorija/telefoni/</loc>',
                origin+'/robots.txt':'User-agent: *\nDisallow: /admin/\n',
                'https://qa.trycloudflare.com/health':json.dumps({'ok':True})}
        with patch.object(domain,'read',side_effect=lambda url:bodies[url]):domain.healthy(indexed=True)
        bodies[origin+'/robots.txt']='User-agent: *\nDisallow: /\n'
        with patch.object(domain,'read',side_effect=lambda url:bodies[url]),self.assertRaises(ValueError):domain.healthy(indexed=True)
        bodies['https://qa.trycloudflare.com/health']=json.dumps({'ok':False})
        with patch.object(domain,'read',side_effect=lambda url:bodies[url]),self.assertRaises(ValueError):domain.healthy()

    def test_order_email_is_durable_and_idempotent(self):
        order=self.order(idem='email-once')
        self.order(idem='email-once')
        with store.connect() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM order_notifications')]
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['order_id'],order['id'])
        self.assertEqual(rows[0]['status'],'pending')
        with patch.object(notifications,'deliver',return_value='mail-provider-id') as deliver:
            self.assertEqual(notifications.run_once(),1)
            self.assertEqual(notifications.run_once(),0)
        self.assertEqual(deliver.call_count,1)
        with store.connect() as c:
            row=dict(c.execute('SELECT * FROM order_notifications').fetchone())
        self.assertEqual(row['status'],'sent');self.assertEqual(row['provider_id'],'mail-provider-id')

    def test_failed_order_transaction_does_not_queue_email(self):
        delivery={'country':'RS','method':'shipping','courier':'post-express','city':'Selo'}
        with self.assertRaises(ValueError):store.create_order([{'id':1,'qty':1,'warranty':0}],{'name':'Test'},delivery,'cod','failed-mail',expected=('EUR',1))
        with store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM order_notifications').fetchone()[0],0)

    def test_notification_uses_front_page_email_and_keeps_cyrillic_details(self):
        delivery={'country':'RS','method':'shipping','courier':'post-express','address':'','city':'Горња Добриња','postal_code':''}
        order=store.create_order([{'id':1,'qty':1,'warranty':0}],{'name':'Ђорђе Живковић','phone':'0631234567','email':'buyer@example.test'},delivery,'cod','unicode-mail')
        store.set_settings({'email':'main@example.test'})
        with patch.object(notifications,'deliver',return_value='sent') as deliver:notifications.run_once()
        payload=deliver.call_args.args[0]
        self.assertEqual(payload['to'],'main@example.test');self.assertEqual(payload['reply_to'],'buyer@example.test')
        for text in ['Ђорђе Живковић','Горња Добриња','Post Express',order['id'],'Pouzeće','0631234567']:
            self.assertIn(text,payload['text'])
        self.assertIn('main@example.test',self.client.get('/').get_data(as_text=True))

    def test_notification_retries_after_failure_without_affecting_order(self):
        order=self.order()
        with patch.object(notifications,'deliver',side_effect=ValueError('Privremena greška')):
            self.assertEqual(notifications.run_once(),0)
        with store.connect() as c:row=dict(c.execute('SELECT * FROM order_notifications').fetchone())
        self.assertEqual(row['status'],'pending');self.assertEqual(row['attempts'],1)
        self.assertGreater(row['next_attempt'],store.now())
        self.assertEqual(store.order_by_token(order['token'])['status'],'new');self.assertEqual(store.product(1)['quantity'],0)
        with patch.object(notifications,'deliver',return_value='later') as deliver:
            notifications.run_once();deliver.assert_not_called()
            with store.connect(True) as c:c.execute('UPDATE order_notifications SET next_attempt=0')
            self.assertEqual(notifications.run_once(),1)

    def test_notification_lease_prevents_two_workers_sending_same_email(self):
        self.order()
        with store.connect(True) as c:c.execute('UPDATE order_notifications SET locked_until=?',(store.now()+90,))
        with patch.object(notifications,'deliver') as deliver:
            self.assertEqual(notifications.run_once(),0);deliver.assert_not_called()
        with store.connect(True) as c:c.execute('UPDATE order_notifications SET locked_until=0')
        with patch.object(notifications,'deliver',return_value='resumed'):self.assertEqual(notifications.run_once(),1)

    def test_card_notification_distinguishes_unpaid_and_verified_payment(self):
        order=self.order(method='card')
        with patch.object(notifications,'deliver',return_value='pending-mail') as deliver:notifications.run_once()
        self.assertIn('NIJE potvrđena',deliver.call_args.args[0]['text'])
        with store.connect(True) as c:c.execute("UPDATE orders SET plan_id='plan_test' WHERE id=?",(order['id'],))
        order=store.order_by_token(order['token'])
        with patch.object(payments,'credentials',return_value={'WHOP_COMPANY_ID':'biz_test'}):
            self.assertTrue(payments.verify_payment(order,self.payment(order)))
            self.assertTrue(payments.verify_payment(order,self.payment(order)))
        with store.connect() as c:self.assertEqual(c.execute("SELECT count(*) FROM order_notifications WHERE event='payment_paid'").fetchone()[0],1)
        with patch.object(notifications,'deliver',return_value='paid-mail') as deliver:self.assertEqual(notifications.run_once(),1)
        self.assertIn('Uplata potvrđena',deliver.call_args.args[0]['subject'])
        self.assertNotIn('NIJE potvrđena',deliver.call_args.args[0]['text'])

    def test_payment_tests_do_not_send_real_order_alerts(self):
        payments.test_order()
        with store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM order_notifications').fetchone()[0],0)

    def test_admin_contact_email_controls_public_contact_and_alerts(self):
        html=self.login()
        data={'csrf':self.hidden(html,'csrf'),'currency':'EUR','eur_rsd':'117.2','courier_post-express':'yes','email':'orders@example.test'}
        result=self.client.post('/admin/settings',data=data)
        self.assertEqual(result.status_code,302);self.assertEqual(store.settings()['email'],'orders@example.test')
        self.assertIn('mailto:orders@example.test',self.client.get('/').get_data(as_text=True))
        data['email']='invalid\nBcc: other@example.test'
        self.client.post('/admin/settings',data=data)
        self.assertEqual(store.settings()['email'],'orders@example.test')

    def test_list_price_markup_rounds_up_without_rounding_intermediate_fee(self):
        for base,listed in [(1,500),(476,500),(477,1000),(10000,10500),(10001,11000),(10100,11000),(20000,21000),(25000,26500),(99999,105000)]:
            self.assertEqual(store.list_price(base),listed,(base,listed))

    def test_admin_saves_base_and_immutable_list_but_public_sees_only_list(self):
        html=self.login()
        data={'csrf':self.hidden(html,'csrf'),'title':'Price controlled device','brand':'Dell','price':'101.01',
              'category_id':'1','quantity':'1','list_price':'1','price_cents':'1','list_price_cents':'1'}
        self.assertEqual(self.client.post('/admin/product',data=data).status_code,302)
        product=store.products(admin=True)[0]
        self.assertEqual(product['base_price_cents'],10101);self.assertEqual(product['price_cents'],11000)
        page=self.client.get('/admin/?edit='+str(product['id'])).get_data(as_text=True)
        self.assertIn('value="101.01"',page);self.assertRegex(page,r'id="list-price"[^>]*readonly')
        import publisher
        catalog=json.dumps(publisher.public_catalog())
        self.assertNotIn('base_price',catalog)
        self.assertNotIn('10101',catalog)

    def test_price_migration_is_once_and_preserves_existing_orders(self):
        self.order()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'legacy.sqlite3'
            with sqlite3.connect(store.DB) as src,sqlite3.connect(path) as dst:src.backup(dst)
            with patch.object(store,'DB',path):
                with store.connect(True) as c:
                    c.execute('ALTER TABLE products DROP COLUMN base_price_cents')
                    c.execute('UPDATE products SET price_cents=10001')
                    original=dict(c.execute('SELECT * FROM orders').fetchone())
                    c.execute("CREATE TRIGGER fail_migration BEFORE UPDATE OF price_cents ON products BEGIN SELECT RAISE(ABORT,'interrupted'); END")
                with self.assertRaises(sqlite3.IntegrityError):store.init_db()
                with store.connect(True) as c:
                    self.assertNotIn('base_price_cents',{r['name'] for r in c.execute('PRAGMA table_info(products)')})
                    self.assertEqual(c.execute('SELECT price_cents FROM products').fetchone()[0],10001)
                    c.execute('DROP TRIGGER fail_migration')
                store.init_db()
                self.assertEqual(store.product(1,admin=True)['base_price_cents'],10001)
                self.assertEqual(store.product(1,admin=True)['price_cents'],11000)
                store.init_db()
                self.assertEqual(store.product(1,admin=True)['price_cents'],11000)
                with store.connect() as c:self.assertEqual(dict(c.execute('SELECT * FROM orders').fetchone()),original)

    def test_nbs_middle_parser_and_failed_refresh_keep_verified_cache(self):
        today=datetime.now(ZoneInfo('Europe/Belgrade')).strftime('%d.%m.%Y')
        document=f'<h6>FORMIRANA NA DAN {today}</h6><table><tr><td>EUR</td><td>978</td><td>EMU</td><td>1</td><td>117,4822</td></tr></table>'
        parsed=rates.parse(document)
        self.assertEqual(parsed['rate'],'117.4822');self.assertEqual(parsed['source'],rates.SOURCE)
        with self.assertRaises(ValueError):rates.parse(document.replace('EUR','USD'))
        before=rates.current()
        with patch.object(rates.requests,'get',side_effect=OSError('offline')):
            with self.assertRaises(OSError):rates.refresh()
        self.assertEqual(rates.current(),before)

    def test_checkout_uses_rsd_with_eur_explanation_and_immutable_rate_snapshot(self):
        store.set_settings({'eur_rsd':'117.4822'})
        order=self.order()
        self.assertEqual(order['currency'],'RSD');self.assertEqual(order['subtotal_minor'],3524466)
        self.assertEqual(order['pricing']['rate'],'117.4822');self.assertEqual(order['items'][0]['unit_eur_cents'],30000)
        page=self.client.get('/order/'+order['token']+'/').get_data(as_text=True)
        self.assertIn('Srednji kurs NBS',page);self.assertIn('RSD',page);self.assertIn('€',page)
        before=copy.deepcopy(order)
        store.set_settings({'eur_rsd':'119.0000'})
        self.assertEqual(store.order_by_token(order['token']),before)

    def test_unverified_or_stale_exchange_rate_cannot_create_new_order(self):
        for value in [{'eur_rsd_source':''},{'eur_rsd_source':rates.SOURCE,'eur_rsd_date':'2000-01-01'}]:
            store.set_settings(value)
            with self.assertRaises(ValueError):self.order()
        with store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],0)
        self.assertEqual(store.product(1)['quantity'],1)

    def test_rate_change_at_submit_requires_customer_to_review_new_total(self):
        quote=store.quote([{'id':1}])
        store.set_settings({'eur_rsd':'118.0000'})
        with self.assertRaises(ValueError):store.create_order([{'id':1}],{}, {'country':'RS','method':'shipping','courier':'post-express'},'cod','rate-race',expected=(quote['currency'],quote['total_minor']))
        self.assertEqual(store.product(1)['quantity'],1)

    def test_currency_controls_removed_and_old_submissions_cannot_change_rate(self):
        html=self.login();page=self.client.get('/admin/?tab=settings').get_data(as_text=True)
        self.assertNotIn('name="currency"',page);self.assertNotIn('name="eur_rsd"',page)
        self.client.post('/admin/settings',data={'csrf':self.hidden(html,'csrf'),'currency':'USD','eur_rsd':'1','courier_post-express':'yes'})
        self.assertEqual(store.settings()['currency'],'EUR');self.assertEqual(store.settings()['eur_rsd'],'117.2')

    def test_homepage_browsing_precedes_marketing(self):
        html=self.client.get('/').get_data(as_text=True)
        self.assertLess(html.index('data-product-grid'),html.index('class="hero"'))
        for marker in ('data-catalog-search','data-brand-filter','data-price-min','data-price-max','data-sort'):
            self.assertIn(marker,html)

    def test_address_exact_match_only(self):
        address_db=Path(TEMP.name)/'test-addresses.sqlite3'
        address_db.unlink(missing_ok=True)
        con=sqlite3.connect(address_db)
        con.execute('CREATE TABLE addresses(id TEXT,street_key TEXT,house_key TEXT,locality TEXT,municipality TEXT,street TEXT,house TEXT)')
        con.execute('INSERT INTO addresses VALUES(?,?,?,?,?,?,?)',('a',addresses.key('SLAVKA ŠLANDERA'),addresses.key('130'),'BEOGRAD (NOVI BEOGRAD)','NOVI BEOGRAD','SLAVKA ŠLANDERA','130'))
        con.commit();con.close()
        with patch.object(addresses,'DATABASE',address_db):
            for text,city in [('Slavka Slandera 130','Novi Beograd'),('Славка Шландера 130','Нови Београд'),('Slavka Shlandera 130','Novi Beograd')]:
                suggestion=addresses.suggest(text,city)
                self.assertIsNotNone(suggestion);self.assertEqual(suggestion['address'],'Slavka Šlandera 130')
            self.assertIsNone(addresses.suggest('Slavka Slandera 131','Novi Beograd'))
            self.assertIsNone(addresses.suggest('','Selo'))
            self.assertIsNone(addresses.suggest('Slavka Slandera 130','Novi Beograd','DE'))
            con=sqlite3.connect(address_db);con.execute('INSERT INTO addresses SELECT ?,street_key,house_key,locality,municipality,street,house FROM addresses',('b',));con.commit();con.close()
            self.assertIsNone(addresses.suggest('Slavka Slandera 130','Novi Beograd'))


if __name__=='__main__':
    unittest.main(verbosity=2)
