"""Manufacturer-photo controls and image preservation use disposable inventory only."""
import io
import json
import unittest
from unittest.mock import patch
from PIL import Image
import test_store as fixture
import factory_photos

store=fixture.store
web=fixture.web


class FactoryPhotoUITests(unittest.TestCase):
    setUp=fixture.StoreTests.setUp
    tearDown=fixture.StoreTests.tearDown
    hidden=fixture.StoreTests.hidden
    login=fixture.StoreTests.login
    photo_product=fixture.StoreTests.photo_product

    def photographed(self):
        csrf,product=self.photo_product()
        factory=store.secrets.token_hex(12)+'.webp'
        Image.new('RGB',(1200,900),'white').save(store.UPLOADS/factory,'WEBP')
        Image.new('RGB',(2400,1800),'white').save(store.ORIGINALS/factory.replace('.webp','.jpg'),'JPEG')
        with store.connect(True) as c:
            c.execute('UPDATE products SET images=?,factory_photo=?,factory_photo_identity=?,factory_photo_source=? WHERE id=?',
                      (json.dumps([factory]+product['images']),factory,factory_photos.identity(product),
                       'https://www.dell.com/support/manuals/example',product['id']))
        return csrf,store.product(product['id'],admin=True),factory

    def form(self,p,csrf,**overrides):
        data={'csrf':csrf,'id':p['id'],'revision':p['revision'],'title':p['title'],'brand':p['brand'],
              'category_id':p['category_id'],'price':str(p['base_price_cents']/100),'quantity':p['quantity'],
              'condition':p['condition'],'description':p['description'],'specs':'\n'.join(f'{k}: {v}' for k,v in p['specs'].items()),
              'keep_images':p['images'],'active':'yes','factory_settings_present':'1','factory_photo_enabled':'yes'}
        data.update(overrides)
        return data

    def test_delete_factory_button_preserves_real_photos_manual_specs_and_opt_out(self):
        csrf,p,factory=self.photographed()
        response=self.client.post('/admin/product',data=self.form(p,csrf,remove_factory_photo='yes'))
        self.assertNotIn('error=',response.location)
        after=store.product(p['id'],admin=True)
        self.assertEqual(after['images'],p['images'][1:])
        self.assertEqual(after['factory_photo_enabled'],0)
        self.assertEqual(after['factory_photo'],'')
        self.assertFalse((store.UPLOADS/factory).exists())
        self.assertFalse((store.ORIGINALS/factory.replace('.webp','.jpg')).exists())
        for name in ('description','specs','price_cents','base_price_cents','quantity','condition'):
            self.assertEqual(after[name],p[name])
        factory_photos.queue_product(p['id'])
        self.assertEqual(store.product(p['id'],admin=True)['images'],p['images'][1:])

    def test_unchecking_factory_keep_image_disables_automatic_readdition(self):
        csrf,p,factory=self.photographed()
        response=self.client.post('/admin/product',data=self.form(p,csrf,keep_images=p['images'][1:]))
        self.assertNotIn('error=',response.location)
        after=store.product(p['id'],admin=True)
        self.assertFalse(after['factory_photo_enabled'])
        self.assertNotIn(factory,after['images'])

    def test_renaming_device_clears_stale_factory_but_keeps_real_photos(self):
        csrf,p,factory=self.photographed()
        response=self.client.post('/admin/product',data=self.form(p,csrf,title='Dell Latitude 5590'))
        self.assertNotIn('error=',response.location)
        after=store.product(p['id'],admin=True)
        self.assertEqual(after['images'],p['images'][1:])
        self.assertTrue(after['factory_photo_enabled'])
        self.assertEqual(after['factory_photo'],'')

    def test_save_keeps_factory_first_and_does_not_count_it_against_upload_limit(self):
        csrf,p,factory=self.photographed()
        extra=[]
        for _ in range(8):
            name=store.secrets.token_hex(12)+'.webp';extra.append(name)
            Image.new('RGB',(20,20),'orange').save(store.UPLOADS/name,'WEBP')
        images=p['images']+extra
        with store.connect(True) as c:c.execute('UPDATE products SET images=? WHERE id=?',(json.dumps(images),p['id']))
        p=store.product(p['id'],admin=True)
        upload=io.BytesIO();Image.new('RGB',(80,60),'green').save(upload,'JPEG');upload.seek(0)
        response=self.client.post('/admin/product',data=self.form(p,csrf,keep_images=images[1:]+[factory],photos=(upload,'last.jpg')))
        self.assertNotIn('error=',response.location)
        after=store.product(p['id'],admin=True)
        self.assertEqual(len(after['images']),11)
        self.assertEqual(after['images'][0],factory)
        self.assertTrue(set(images).issubset(after['images']))

    def test_public_and_admin_markup_identify_factory_photo_and_full_resolution(self):
        csrf,p,factory=self.photographed()
        html=self.client.get('/proizvod/'+str(p['id'])+'/').get_data(as_text=True)
        self.assertIn('data-factory-photo-index="0"',html)
        self.assertIn('Fotografija proizvođača',html)
        self.assertIn('/media/full/'+factory.replace('.webp','.jpg'),html)
        admin=self.client.get('/admin/?edit='+str(p['id'])).get_data(as_text=True)
        self.assertIn('Obriši fabričku fotografiju',admin)
        self.assertIn('name="factory_settings_present"',admin)
        self.assertIn('data-factory-photo',admin)

    def test_duplicate_preserves_factory_and_user_disabled_choice(self):
        csrf,p,factory=self.photographed()
        with store.connect(True) as c:c.execute('UPDATE products SET factory_photo_enabled=0 WHERE id=?',(p['id'],))
        response=self.client.post('/admin/product/'+str(p['id'])+'/duplicate',data={'csrf':csrf})
        self.assertNotIn('error=',response.location)
        new=next(item for item in store.products(admin=True) if item['title']==p['title'] and item['id']!=p['id'])
        self.assertFalse(new['factory_photo_enabled'])
        self.assertEqual(new['images'],p['images'])
        self.assertEqual(new['specs'],p['specs'])


if __name__=='__main__':unittest.main()
