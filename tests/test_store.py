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


class StoreTests(unittest.TestCase):
    def setUp(self):
        with store.connect(True) as c:
            for table in ['orders','products','settings','sessions','jobs','audit']:
                c.execute('DELETE FROM '+table)
            c.execute('DELETE FROM categories WHERE id>4')
            c.execute("UPDATE categories SET shipping_overrides='{}',weight_kg=5 WHERE id=1")
            c.execute('''INSERT INTO products(id,category_id,title,slug,brand,price_cents,quantity,condition,
              description,specs,images,weight_kg,active,created_at,updated_at) VALUES(1,1,?,?,?,?,?,?,?,?,?,5,1,?,?)''',
              ('Test ThinkPad Čačak','test-thinkpad','Lenovo',30000,1,'Refurbished — odlično','Testirani uređaj',
               json.dumps({'Procesor':'Core i5','Memorija':'16 GB'}),'[]',store.now(),store.now()))
        web.login_attempts.clear()
        self.client=web.app.test_client()
        self.publish=patch.object(web,'queue_publish');self.publish.start()

    def tearDown(self):
        self.publish.stop()

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
        self.assertEqual(order['total_minor'],30000+store.rsd_to_minor(600,store.settings()))
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
        self.assertEqual(q['warranty_minor'],180000)
        self.assertEqual(store.quote([{'id':1}],cfg=cfg)['warranty_minor'],0)

    def test_public_admin_and_post_protection(self):
        self.assertEqual(self.client.get('/admin/').status_code,302)
        self.assertEqual(self.client.post('/admin/product',data={'title':'Intruder'}).status_code,403)
        self.assertEqual(self.client.post('/admin/settings',data={}).status_code,403)
        self.assertEqual(self.client.post('/webhooks/whop',json={'type':'payment.succeeded'}).status_code,401)

    def test_login_and_mobile_camera_controls(self):
        html=self.login()
        self.assertIn('capture="environment"',html)
        self.assertIn('Napravi sličan',html)
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
