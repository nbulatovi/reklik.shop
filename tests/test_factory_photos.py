"""Manufacturer image preservation and durable opt-out; no live network or inventory."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from PIL import Image
import test_store as fixture
import factory_photos as photos

store = fixture.store


class FactoryPhotoTests(unittest.TestCase):
    def setUp(self):
        with store.connect(True) as connection:
            connection.execute('DELETE FROM products')
            connection.execute('''INSERT INTO products(id,category_id,title,slug,brand,price_cents,quantity,
                description,specs,images,active,created_at,updated_at) VALUES(1,1,?,?,?,?,?,?,?,?,1,?,?)''',
                ('Dell Latitude 7490', 'dell7490', 'Dell Latitude 7490', 30000, 1, 'Ručno: ekran ima trag.',
                 json.dumps({'RAM': '16 GB', 'SSD': '512 GB'}), json.dumps(['a'*24+'.webp']), store.now(), store.now()))
        self.temp = tempfile.TemporaryDirectory(prefix='reklik-factory-photo-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.uploads = self.root/'uploads'; self.uploads.mkdir()
        self.originals = self.uploads/'full'; self.originals.mkdir()
        for item in (patch.object(store, 'UPLOADS', self.uploads), patch.object(store, 'ORIGINALS', self.originals),
                     patch.object(store, 'ROOT', self.root), patch.object(photos.WORKERS, 'submit'), patch.object(photos, '_publish')):
            mock = item.start(); self.addCleanup(item.stop)
            if hasattr(item, 'attribute') and item.attribute == 'submit':self.worker = mock
        with photos._LOCK:photos._INFLIGHT.clear()
        image = Image.new('RGB', (800, 600), '#dedede'); output = io.BytesIO();image.save(output, 'PNG')
        self.data = output.getvalue()
        self.candidate = {'model': 'Dell Latitude 7490', 'page_url': 'https://www.dell.com/support/latitude-7490',
                          'image_url': 'https://i.dell.com/images/7490.png', 'view': 'front', 'color': ''}

    def product(self):return store.product(1, admin=True)

    def queue(self, force=False):
        photos.queue_product(1, force=force)
        with store.connect() as connection:return dict(connection.execute('SELECT * FROM factory_photo_jobs WHERE product_id=1').fetchone())

    def install(self, task):return photos.install(1, task['token'], task['identity'], self.candidate, self.data)

    def test_queue_is_nonblocking_and_independent_of_ai_specs(self):
        self.assertEqual(self.product()['ai_specs_enabled'], 0)
        with patch.object(photos, 'lookup', side_effect=AssertionError('network on HTTP thread')):
            task = self.queue()
        self.assertEqual(task['status'], 'queued')
        self.worker.assert_called_once()
        self.assertEqual(self.product()['images'], ['a'*24+'.webp'])
        self.assertEqual(photos.status(1)['status'], 'queued')

    def test_exact_factory_image_first_all_manual_values_preserved(self):
        before = copy.deepcopy(self.product()); task = self.queue()
        self.assertTrue(self.install(task))
        after = self.product()
        self.assertEqual(after['images'][1:], before['images'])
        self.assertEqual(after['images'][0], after['factory_photo'])
        self.assertEqual(after['factory_photo_source'], self.candidate['page_url'])
        self.assertTrue((self.uploads/after['factory_photo']).is_file())
        self.assertTrue((self.originals/(after['factory_photo'][:-5]+'.jpg')).is_file())
        for key in ('title','brand','price_cents','base_price_cents','description','specs','condition','quantity','active'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(photos.status(1)['status'], 'completed')

    def test_ten_owner_images_are_retained_plus_factory(self):
        images = [format(index, '024x')+'.webp' for index in range(10)]
        with store.connect(True) as connection:connection.execute('UPDATE products SET images=? WHERE id=1', (json.dumps(images),))
        self.assertTrue(self.install(self.queue()))
        self.assertEqual(self.product()['images'][1:], images)
        self.assertEqual(len(self.product()['images']), 11)

    def test_uploads_added_during_lookup_are_retained(self):
        task = self.queue(); images = self.product()['images'] + ['b'*24+'.webp']
        with store.connect(True) as connection:connection.execute('UPDATE products SET images=? WHERE id=1', (json.dumps(images),))
        self.assertTrue(self.install(task))
        self.assertEqual(self.product()['images'][1:], images)

    def test_disabled_photo_cannot_be_resurrected_by_inflight_job(self):
        task = self.queue()
        with store.connect(True) as connection:connection.execute('UPDATE products SET factory_photo_enabled=0 WHERE id=1')
        self.assertFalse(self.install(task))
        photos.recover();photos.tick();photos.queue_product(1, force=True)
        self.assertEqual(self.product()['images'], ['a'*24+'.webp'])
        self.assertEqual(self.product()['factory_photo'], '')
        self.assertEqual(list(self.uploads.glob('*.webp')), [])
        self.assertEqual(photos.status(1)['status'], 'cancelled')
        self.worker.assert_called_once()

    def test_deleted_product_cannot_receive_inflight_photo(self):
        task = self.queue()
        with store.connect(True) as connection:connection.execute('DELETE FROM products WHERE id=1')
        self.assertFalse(self.install(task))
        self.assertEqual(list(self.uploads.glob('*.webp')), [])
        with store.connect() as connection:self.assertEqual(connection.execute('SELECT count(*) FROM factory_photo_jobs').fetchone()[0], 0)

    def test_explicit_reenable_starts_a_fresh_job_after_cancellation(self):
        task = self.queue()
        with store.connect(True) as connection:connection.execute('UPDATE products SET factory_photo_enabled=0 WHERE id=1')
        photos.queue_product(1)
        with store.connect(True) as connection:connection.execute('UPDATE products SET factory_photo_enabled=1 WHERE id=1')
        next_task = self.queue()
        self.assertNotEqual(next_task['token'], task['token'])
        self.assertFalse(self.install(task))
        self.assertTrue(self.install(next_task))

    def test_missing_generated_file_is_repaired_by_background_tick(self):
        task = self.queue();self.assertTrue(self.install(task))
        (self.uploads/self.product()['factory_photo']).unlink()
        self.worker.reset_mock()
        photos.tick()
        self.worker.assert_called_once()
        with store.connect() as connection:
            next_task = dict(connection.execute('SELECT * FROM factory_photo_jobs WHERE product_id=1').fetchone())
        self.assertNotEqual(next_task['token'], task['token'])
        self.assertTrue(self.install(next_task))
        self.assertEqual(len(self.product()['images']), 2)

    def test_identity_race_and_old_token_cannot_install(self):
        task = self.queue()
        with store.connect(True) as connection:connection.execute("UPDATE products SET title='Dell Latitude 5490' WHERE id=1")
        self.assertFalse(self.install(task))
        newer = self.queue()
        self.assertNotEqual(task['token'], newer['token'])
        self.assertFalse(self.install(task))
        self.assertEqual(self.product()['factory_photo'], '')

    def test_manual_spec_enrichment_does_not_invalidate_identity(self):
        before = self.product(); after = copy.deepcopy(before)
        after['specs']['Procesor'] = 'Intel Core i5'
        after['description'] += ' Napomena.'
        self.assertEqual(photos.identity(before), photos.identity(after))
        after['specs']['Boja'] = 'plava'
        self.assertNotEqual(photos.identity(before), photos.identity(after))
        after = copy.deepcopy(before); after['title'] = 'Dell Latitude 5490'
        self.assertNotEqual(photos.identity(before), photos.identity(after))

    def test_identity_changes_for_replaced_first_real_photo_not_factory_or_extra_angles(self):
        product = self.product();before = photos.identity(product)
        product['images'].append('b'*24+'.webp')
        self.assertEqual(photos.identity(product), before)
        product['factory_photo'] = 'c'*24+'.webp';product['images'].insert(0,product['factory_photo'])
        self.assertEqual(photos.identity(product), before)
        raw = dict(product, images=json.dumps(product['images']))
        self.assertEqual(photos.identity(raw), before)
        product['images'].remove('a'*24+'.webp')
        self.assertNotEqual(photos.identity(product), before)

    def test_completed_lookup_is_not_repeated_on_save_or_tick(self):
        task = self.queue(); self.assertTrue(self.install(task)); self.worker.reset_mock()
        photos.queue_product(1); photos.tick();photos.recover()
        self.worker.assert_not_called()

    def test_recover_resumes_running_durable_job(self):
        task = self.queue()
        with store.connect(True) as connection:connection.execute("UPDATE factory_photo_jobs SET status='running' WHERE product_id=1")
        with photos._LOCK:photos._INFLIGHT.clear()
        self.worker.reset_mock(); photos.recover()
        self.worker.assert_called_once()
        with store.connect() as connection:
            row = connection.execute('SELECT token,status FROM factory_photo_jobs WHERE product_id=1').fetchone()
        self.assertEqual(row['token'], task['token'])
        self.assertEqual(row['status'], 'queued')

    def test_backfill_skips_hidden_examples_and_disabled_items(self):
        with store.connect(True) as connection:connection.execute('UPDATE products SET active=0 WHERE id=1')
        photos.recover();self.worker.assert_not_called()
        with store.connect(True) as connection:connection.execute('UPDATE products SET active=1,factory_photo_enabled=0 WHERE id=1')
        photos.recover();self.worker.assert_not_called()

    def test_transient_failures_retry_but_not_found_does_not_loop(self):
        task = self.queue();photos._finish(1, task['token'], 'failed')
        self.worker.reset_mock();photos.tick();self.worker.assert_not_called()
        with store.connect(True) as connection:connection.execute('UPDATE factory_photo_jobs SET retry_at=? WHERE product_id=1', (store.now()-1,))
        with photos._LOCK:photos._INFLIGHT.clear()
        photos.tick();self.worker.assert_called_once()
        with store.connect() as connection:new = dict(connection.execute('SELECT * FROM factory_photo_jobs WHERE product_id=1').fetchone())
        self.assertEqual(new['attempts'], 2)
        photos._finish(1, new['token'], 'not_found');self.worker.reset_mock()
        photos.tick();self.worker.assert_not_called()

    def test_research_fails_safely_without_replacing_owner_images(self):
        task = self.queue()
        with patch.object(photos, 'lookup', side_effect=LookupError('No exact model')):
            photos.run(1, task['token'], task['identity'])
        self.assertEqual(photos.status(1)['status'], 'not_found')
        self.assertEqual(self.product()['images'], ['a'*24+'.webp'])
        self.assertEqual(self.product()['specs']['RAM'], '16 GB')

    def test_precise_proplus_matching_and_malformed_brand(self):
        catalog = [dict(self.candidate, brand='Xiaomi',model='Redmi Note 13 Pro+ 5G',match_regex=r'\bredmi note 13 pro\s*\+\s*5g\b',
                        page_url='https://www.mi.com/global/redmi-note-13-pro-plus-5g/',image_url='https://i02.appmifile.com/a.png',view='front')]
        (self.root/'factory_photo_catalog.json').write_text(json.dumps(catalog))
        product = dict(self.product(), title='Redmi Note 13 Pro+ 5G', brand='Redmi Note 13 Pro+ 5G 12/512GB',category_slug='telefoni')
        self.assertEqual(photos.manufacturer(product), 'xiaomi')
        self.assertEqual(len(photos.curated_candidates(product)), 1)
        product['title'] = 'Redmi Note 13 Pro 5G';product['brand']='Xiaomi'
        self.assertEqual(photos.curated_candidates(product), [])

    def test_wrong_color_or_unknown_color_back_view_is_rejected(self):
        product = dict(self.product(), title='Samsung Galaxy A14 plava', brand='Samsung', category_slug='telefoni')
        candidate = dict(self.candidate, model='Samsung Galaxy A14',view='back',color='purple',
                         page_url='https://www.samsung.com/galaxy-a14/',image_url='https://images.samsung.com/a14.png')
        with self.assertRaises(LookupError):photos._candidate(product, candidate, curated=True)
        product['title'] = 'Samsung Galaxy A14'
        with self.assertRaises(LookupError):photos._candidate(product, candidate, curated=True)
        candidate.update(color='',view='front')
        self.assertEqual(photos._candidate(product, candidate, curated=True)['view'], 'front')

    def test_reviewed_color_is_scoped_to_exact_real_unit_photo(self):
        product = dict(self.product(), title='Samsung Galaxy A14', brand='Samsung', category_slug='telefoni')
        candidate = dict(self.candidate,model='Samsung Galaxy A14',view='front_back',color='purple',
            page_url='https://www.samsung.com/galaxy-a14/',image_url='https://images.samsung.com/a14.png',
            verified_unit_photo='a'*24+'.webp',verified_unit_color='purple')
        self.assertEqual(photos._candidate(product,candidate,curated=True)['color'],'purple')
        product['images']=['b'*24+'.webp']
        with self.assertRaises(LookupError):photos._candidate(product,candidate,curated=True)
        product['images']=['a'*24+'.webp'];product['specs']['Boja']='plava'
        with self.assertRaises(LookupError):photos._candidate(product,candidate,curated=True)

    def test_https_domains_credentials_and_private_dns_are_checked(self):
        for url in ('http://i.dell.com/a.png','https://user:secret@i.dell.com/a.png','https://i.dell.com.evil.test/a.png',
                    'https://127.0.0.1/a.png','https://images.samsung.com/a.png','https://i.dell.com:8000/a.png'):
            with self.subTest(url=url), self.assertRaises(ValueError):photos.safe_url(url, 'dell', image=True,check_dns=False)
        with patch.object(photos.socket,'getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(ValueError):photos.safe_url('https://i.dell.com/a.png','dell', image=True)
        with patch.object(photos.socket,'getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]):
            self.assertEqual(photos.safe_url('https://i.dell.com/a.png','dell', image=True),'https://i.dell.com/a.png')
        self.assertEqual(photos.safe_url('https://docs.lenovocdrt.com/ref/docks/#40an','lenovo',check_dns=False),
                         'https://docs.lenovocdrt.com/ref/docks/')
        with self.assertRaises(ValueError):photos.safe_url('https://cdrt.github.io/unreviewed.png','lenovo',image=True,check_dns=False)

    def test_official_page_image_provenance_accepts_relative_and_escaped_urls(self):
        page='https://www.hp.com/product/model/'
        image='https://www.hp.com/content/dam/model/main.jpg?width=1600'
        for html in ('<img src="/content/dam/model/main.jpg">',
                     '<meta content="https://www.hp.com/content/dam/model/main.jpg">',
                     '<img src="//www.hp.com/content/dam/model/main.jpg?width=800">',
                     '<div data-image="&#34;https:\\/\\/www.hp.com\\/content\\/dam\\/model\\/main.jpg&#34;">'):
            self.assertTrue(photos.page_references_image(html,page,image),html)
        self.assertFalse(photos.page_references_image('<img src="/content/dam/model/main.jpg-wrong">',page,image))
        self.assertFalse(photos.page_references_image('<img src="/content/dam/model/main.jpg">',page,'https://images.hp.com/content/dam/model/main.jpg'))

    def test_invalid_and_tiny_images_never_write_inventory(self):
        task = self.queue()
        for data in (b'<svg/>', b'not an image'):
            with self.assertRaises(Exception):photos.install(1, task['token'], task['identity'], self.candidate, data)
        output = io.BytesIO();Image.new('RGB',(32,32)).save(output,'PNG')
        with self.assertRaises(ValueError):photos.install(1, task['token'], task['identity'], self.candidate, output.getvalue())
        self.assertEqual(self.product()['factory_photo'], '')
        self.assertEqual(list(self.uploads.glob('*.webp')), [])

    def test_duplicate_factory_job_submission_is_suppressed(self):
        self.queue();self.queue();self.queue();self.worker.assert_called_once()


if __name__ == '__main__':unittest.main()
