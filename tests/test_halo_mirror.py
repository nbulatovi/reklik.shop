"""Inventory lifecycle reconciliation; no live browser, ads, orders or payments."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_store as fixture
import halo_mirror as mirror


class HaloSubmissionUncertain(Exception):pass
class HaloFeeRequired(Exception):pass
class HaloAuthRequired(Exception):pass


class FakeBrowser:
    def __init__(self,status='active'):
        self.calls=[]
        self.status=status
        self.remote='5425647999999'
        self.create_error=None
        self.on_create=None
        self.found=None
        self.find_error=None

    def result(self,status=None):
        return {'status':status or self.status,'remote_id':self.remote,
                'url':'https://www.halooglasi.com/racunari/laptop-racunari/test/'+self.remote,
                'auto_renew':True,'expires_at':mirror.clock()+86400*30,'fee_rsd':0}

    def inspect(self,remote,url=None):
        self.calls.append(('inspect',remote));return self.result()

    def create(self,payload):
        self.calls.append(('create',payload['id']))
        if self.on_create:self.on_create()
        if self.create_error:raise self.create_error
        return self.result('active')

    def update(self,remote,payload):
        self.calls.append(('update',remote));return self.result()

    def deactivate(self,remote,url=None):
        self.calls.append(('deactivate',remote));value=self.result('paused');value['auto_renew']=False;return value

    def activate(self,remote):
        self.calls.append(('activate',remote));return self.result('active')

    def ensure_renewal(self,remote):
        self.calls.append(('renew',remote));return self.result('active')

    def find_existing(self,payload):
        self.calls.append(('find_existing',payload['id']))
        if self.find_error:raise self.find_error
        return self.found


class HaloMirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='reklik-halo-core-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.cfg=copy.deepcopy(fixture.store.DEFAULTS)
        self.cfg.update(site_url='https://reklik.shop',canonical_url='https://reklik.shop',indexing_enabled=True)
        for item in (patch.object(mirror,'ROOT',self.root),patch.object(fixture.store,'settings',return_value=self.cfg)):
            item.start();self.addCleanup(item.stop)
        self.product={'id':1,'active':1,'quantity':1,'title':'Dell test laptop','brand':'Dell','category_id':1,
             'category_slug':'laptopovi','category_name':'Laptopovi','icon':'laptop','price_cents':15000,
             'condition':'Refabrikovano - odlično stanje (Grade A)','description':'LCD nov. Poklopac ima ogrebotina.',
             'specs':{'RAM':'8 GB','Disk':'256 GB SSD'},'images':[],'updated_at':1,'revision':1}
        self.products={1:self.product}
        self.browser=FakeBrowser()
        mirror.atomic_json(self.root/'state.json',{'version':1,'bindings':{}})

    def catalog(self,products=None):
        return {p['id']:{'id':p['id'],'title':p['title'],'price_minor':p['price_cents'],'quantity':p['quantity'],
               'condition':p['condition'],'images':p['images']} for p in (products or self.products).values()}

    def binding(self,status='active',fingerprint=None):
        result=self.browser.result(status)
        result.update(product_id=1,owned_verified=True,fingerprint=fingerprint or mirror.fingerprint(self.product),
                      last_checked_at=mirror.clock(),desired='visible')
        mirror.atomic_json(self.root/'state.json',{'version':1,'bindings':{'1':result}})
        return result

    def sync(self,catalog=None,force=False):
        return mirror.synchronize(self.browser,catalog=self.catalog() if catalog is None else catalog,
                                   current_products=self.products,force=force)

    def test_new_item_waits_for_real_publication_then_creates_once(self):
        self.sync(catalog={})
        self.assertEqual(self.browser.calls,[])
        self.assertEqual(mirror.item_status(1)['status'],'waiting_public')
        self.sync();self.sync()
        self.assertEqual([x[0] for x in self.browser.calls].count('create'),1)
        self.assertTrue(mirror.public_listing(self.product))

    def test_imported_existing_ad_is_reused_without_creation(self):
        self.binding()
        self.sync(force=True)
        self.assertEqual(self.browser.calls,[('inspect',self.browser.remote)])
        self.assertEqual(mirror.state()['bindings']['1']['remote_id'],self.browser.remote)

    def test_quantity_above_zero_does_not_change_ad_or_fingerprint(self):
        self.binding();before=mirror.fingerprint(self.product)
        self.product.update(quantity=4,revision=77,updated_at=999)
        self.assertEqual(mirror.fingerprint(self.product),before)
        self.sync();self.assertEqual(self.browser.calls,[])

    def test_hidden_sold_and_fully_deleted_items_deactivate_without_losing_id(self):
        for kind in ('hidden','sold','deleted'):
            with self.subTest(kind=kind):
                self.product.update(active=1,quantity=1);self.products[1]=self.product;self.binding();self.browser.calls=[]
                if kind=='hidden':self.product['active']=0
                elif kind=='sold':self.product['quantity']=0
                else:self.products.pop(1)
                self.sync(catalog={})
                self.assertEqual(self.browser.calls,[('deactivate',self.browser.remote)])
                binding=mirror.state()['bindings']['1']
                self.assertEqual(binding['remote_id'],self.browser.remote)
                self.assertEqual(binding['status'],'paused');self.assertFalse(binding['auto_renew'])

    def test_visible_again_reactivates_same_ad(self):
        self.binding('paused');self.browser.status='paused'
        self.sync()
        self.assertIn(('activate',self.browser.remote),self.browser.calls)
        self.assertFalse(any(c[0]=='create' for c in self.browser.calls))

    def test_price_or_manual_specs_update_same_id_after_publication(self):
        self.binding();old_catalog=self.catalog()
        self.product['price_cents']=18000;self.product['specs']['RAM']='16 GB'
        self.sync(catalog=old_catalog)
        self.assertFalse(any(c[0]=='update' for c in self.browser.calls))
        # Waiting for static deployment is a local gate; force only bypasses retry time.
        self.sync(force=True)
        self.assertIn(('update',self.browser.remote),self.browser.calls)
        self.assertFalse(any(c[0]=='create' for c in self.browser.calls))

    def test_expired_or_missing_known_ad_is_never_recreated(self):
        for status in ('expired','missing','stopped'):
            with self.subTest(status=status):
                self.binding(status);self.browser.status=status;self.browser.calls=[]
                self.sync(force=True)
                self.assertFalse(any(c[0]=='create' for c in self.browser.calls))
                if status=='expired':self.assertIn(('renew',self.browser.remote),self.browser.calls)
                if status=='stopped':self.assertFalse(any(c[0]=='activate' for c in self.browser.calls))

    def test_uncertain_publication_is_not_retried_and_can_reconcile(self):
        self.browser.create_error=HaloSubmissionUncertain('No reply after external POST')
        self.sync();self.sync(force=True)
        self.assertEqual([c[0] for c in self.browser.calls].count('create'),1)
        self.assertEqual(mirror.item_status(1)['status'],'uncertain')
        self.browser.found=self.browser.result()
        self.sync(force=True)
        self.assertEqual(mirror.state()['bindings']['1']['remote_id'],self.browser.remote)
        self.assertEqual([c[0] for c in self.browser.calls].count('create'),1)

    def test_hidden_during_publication_is_immediately_withdrawn(self):
        self.browser.on_create=lambda:self.product.update(active=0)
        self.sync()
        self.assertEqual([c[0] for c in self.browser.calls],['create','deactivate'])
        self.assertEqual(mirror.state()['bindings']['1']['status'],'paused')

    def test_uncertain_submission_stays_protected_through_login_errors(self):
        self.browser.create_error=HaloSubmissionUncertain('Interrupted publication')
        self.sync()
        self.browser.find_error=HaloAuthRequired('Login needed for lookup')
        self.sync(force=True)
        self.browser.find_error=None
        self.sync(force=True)
        self.assertEqual([c[0] for c in self.browser.calls].count('create'),1)
        self.assertTrue(mirror.state()['bindings']['1']['create_attempt_started'])

    def test_deleted_during_uncertain_submission_is_recovered_then_withdrawn(self):
        self.browser.create_error=HaloSubmissionUncertain('Interrupted publication')
        self.sync()
        self.products.pop(1)
        self.browser.found=self.browser.result()
        self.sync(catalog={},force=True)
        self.assertEqual([c[0] for c in self.browser.calls],['create','find_existing','deactivate'])
        self.assertEqual(mirror.state()['bindings']['1']['status'],'paused')

    def test_edits_during_uncertain_submission_are_not_falsely_marked_synced(self):
        self.browser.create_error=HaloSubmissionUncertain('Interrupted publication')
        self.sync();submitted_fingerprint=mirror.fingerprint(self.product)
        self.product['price_cents']=18000
        self.browser.found=self.browser.result()
        self.sync(force=True)
        self.assertEqual(mirror.state()['bindings']['1']['fingerprint'],submitted_fingerprint)
        self.assertIsNone(mirror.public_listing(self.product))
        self.sync(force=True)
        self.assertIn(('update',self.browser.remote),self.browser.calls)
        self.assertEqual([c[0] for c in self.browser.calls].count('create'),1)

    def test_hidden_during_existing_activation_is_withdrawn(self):
        self.binding('paused');self.browser.status='paused'
        original=self.browser.activate
        def activate(remote):
            value=original(remote);self.product['active']=0;return value
        self.browser.activate=activate
        self.sync()
        self.assertEqual([c[0] for c in self.browser.calls],['inspect','activate','deactivate'])

    def test_authentication_failure_stops_batch_instead_of_repeated_logins(self):
        self.binding();self.product['price_cents']=18000
        self.products[2]=dict(self.product,id=2,title='Second laptop')
        def fail(*args):
            self.browser.calls.append(('auth',1));raise HaloAuthRequired('Login needed')
        self.browser.inspect=fail
        self.sync(force=True)
        self.assertEqual(self.browser.calls,[('auth',1)])

    def test_product_links_hide_stale_or_unsafe_mappings(self):
        binding=self.binding()
        self.assertEqual(mirror.public_listing(self.product)['url'],binding['url'])
        self.product['price_cents']=18000
        self.assertIsNone(mirror.public_listing(self.product))
        self.product['price_cents']=15000
        binding['url']='https://www.halooglasi.com.evil.example/racunari/item/'+self.browser.remote
        mirror.atomic_json(self.root/'state.json',{'version':1,'bindings':{'1':binding}})
        self.assertIsNone(mirror.public_listing(self.product))
        self.assertIsNone(mirror.item_status(1)['url'])

    def test_date_only_expiry_does_not_hide_an_observed_active_listing_early(self):
        binding=self.binding()
        binding.update(expires_at=mirror.clock()-60,expires_precision='date')
        mirror.atomic_json(self.root/'state.json',{'version':1,'bindings':{'1':binding}})
        self.assertIsNotNone(mirror.public_listing(self.product))
        binding['status']='expired'
        mirror.atomic_json(self.root/'state.json',{'version':1,'bindings':{'1':binding}})
        self.assertIsNone(mirror.public_listing(self.product))

    def test_corrupt_or_duplicate_identity_state_fails_closed(self):
        (self.root/'state.json').write_text('{broken')
        with self.assertRaises(ValueError):self.sync()
        self.assertEqual(self.browser.calls,[])
        binding=self.browser.result();binding['product_id']=1
        duplicate=dict(binding,product_id=2)
        mirror.atomic_json(self.root/'state.json',{'version':1,'bindings':{'1':binding,'2':duplicate}})
        with self.assertRaises(ValueError):self.sync()

    def test_fee_hold_does_not_publish_or_hide_provider_error_secrets_in_admin(self):
        self.browser.create_error=HaloFeeRequired('password=private fee confirmation')
        self.sync()
        value=mirror.item_status(1)
        self.assertEqual(value['status'],'fee_required')
        self.assertNotIn('private',json.dumps(value))

    def test_copy_preserves_manual_defects_without_contacts_or_battery_claims(self):
        self.product['description']='Nov displej i baterija,poklopac baterije ima ogrebotina,baterija dobra,128GB skladište. Prodaje se samo telefon bez punjača. https://example.com info@example.com +38163697007 100 EUR'
        original=copy.deepcopy(self.product)
        value=mirror.payload(self.product)
        self.assertIn('poklopac ima ogrebotina',value['description'])
        self.assertIn('128GB',value['description'])
        self.assertIn('bez punjača',value['description'])
        self.assertNotIn('baterij',value['description'].lower())
        self.assertNotIn('example',value['description'])
        self.assertNotIn('100 EUR',value['description'])
        self.assertLessEqual(len(value['description']),1500)
        self.assertEqual(self.product,original)

    def test_lazy_adapter_does_not_open_browser_for_unchanged_inventory(self):
        from unittest.mock import Mock
        factory=Mock()
        with mirror.LazyAdapter(factory):pass
        factory.assert_not_called()

    def test_sync_request_is_local_and_nonblocking(self):
        self.assertTrue(mirror.request_sync())
        first=(self.root/'request').read_text();mirror.request_sync()
        self.assertNotEqual(first,(self.root/'request').read_text())
        self.assertEqual(self.browser.calls,[])

    def test_factory_stays_on_storefront_and_actual_photo_changes_update_same_ad(self):
        from unittest.mock import Mock
        from PIL import Image
        manual='a'*24+'.webp';factory='b'*24+'.webp';extra='c'*24+'.webp'
        for name in (manual,factory,extra):
            Image.new('RGB',(10,10),'white').save(fixture.store.UPLOADS/name,'WEBP')
        self.product['images']=[manual]
        binding=self.binding()
        binding['images_fingerprint']=mirror.images_fingerprint(self.product)
        mirror.atomic_json(self.root/'state.json',{'version':1,'bindings':{'1':binding}})
        self.product.update(images=[factory,manual],factory_photo=factory)
        self.browser.update=Mock(wraps=self.browser.update)
        self.sync()
        self.browser.update.assert_not_called()
        self.assertEqual([Path(path).name for path in mirror.payload(self.product,self.cfg)['photos']],[manual])
        self.product['images']=[factory,manual,extra]
        self.sync()
        remote,payload=self.browser.update.call_args.args
        self.assertEqual(remote,binding['remote_id'])
        self.assertTrue(payload['replace_photos'])
        self.assertEqual([Path(path).name for path in payload['photos']],[manual,extra])
        self.product.update(images=[manual,extra],factory_photo='')
        self.browser.update.reset_mock();self.sync()
        self.browser.update.assert_not_called()
        self.assertFalse(any(call[0]=='create' for call in self.browser.calls))

    def test_existing_mapping_import_cannot_reset_or_recreate_ads(self):
        import hashlib
        (self.root/'state.json').unlink()
        canonical=lambda x:json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'))
        digest=lambda x:hashlib.sha256(x.encode()).hexdigest()
        old={'id':1,'remote_id':self.browser.remote,'fee_rsd':0,
             'source_title':self.product['title'],'source_brand':self.product['brand'],
             'source_category_id':self.product['category_id'],'condition':self.product['condition'],
             'price_eur':'150','source_specs_sha256':digest(canonical(self.product['specs'])),
             'source_description_sha256':digest(self.product['description']),
             'source_images_sha256':digest(canonical(self.product['images'])),
             'phone':self.cfg['phone'],'email':self.cfg['email']}
        legacy=self.root/'old.json';observed=self.root/'observed.json'
        mirror.atomic_json(legacy,{'items':{'1':old}})
        mirror.atomic_json(observed,{'items':{'1':self.browser.result()}})
        with patch.object(fixture.store,'products',return_value=[self.product]):
            result=mirror.import_existing(legacy,observed)
            self.assertEqual(result,{'imported_existing':1,'new_ads_created':0})
            with self.assertRaises(FileExistsError):mirror.import_existing(legacy,observed)
        self.assertIsNotNone(mirror.public_listing(self.product))
        self.sync(force=True)
        self.assertFalse(any(c[0]=='create' for c in self.browser.calls))

    def test_existing_import_requires_matching_observed_id(self):
        (self.root/'state.json').unlink()
        legacy=self.root/'old.json';observed=self.root/'observed.json'
        mirror.atomic_json(legacy,{'items':{'1':{'id':1,'remote_id':self.browser.remote,'fee_rsd':0}}})
        mirror.atomic_json(observed,{'items':{'1':dict(self.browser.result(),remote_id='5425647000000')}})
        with self.assertRaises(ValueError):mirror.import_existing(legacy,observed)
        self.assertFalse((self.root/'state.json').exists())


if __name__=='__main__':unittest.main()
