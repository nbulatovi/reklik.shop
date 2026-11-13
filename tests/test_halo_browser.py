"""Offline Halo UI fixtures. No live account, credential or external requests."""
import copy
from datetime import datetime
from decimal import Decimal
import html
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import halo_browser as halo

ID = '5425647927006'
OTHER = '5425647927999'


def payload(photo='/owned/photo.jpg'):
    return {'id':27,'title':'Dell Latitude 3590','description':'Testirani uređaj.\nŠifra: RK-27',
            'condition':'Refabrikovano - odlično stanje (Grade A)','price_eur':'150',
            'category_slug':'laptopovi','category_name':'Laptopovi','category_icon':'laptop',
            'brand':'Dell','photos':[str(photo)],'phone':'+38163697007','email':'sales@example.test'}


class HaloPureTests(unittest.TestCase):
    def test_only_observed_matching_halo_urls_are_returned(self):
        actual='/racunari/laptop-racunari/observed-slug/'+ID+'?kid=1'
        self.assertEqual(halo.observed_public_url(actual,ID),halo.ORIGIN+actual.split('?')[0])
        for bad in ['https://evil.test/racunari/laptop/'+ID,'//evil.test/racunari/'+ID,
                    '/izmeni-oglas/'+ID,'/racunari/laptop/'+ID+'0']:
            self.assertIsNone(halo.observed_public_url(bad,ID),bad)
        self.assertEqual(halo.edit_id('/izmeni-oglas/'+ID),ID)
        self.assertIsNone(halo.edit_id('https://evil.test/izmeni-oglas/'+ID))

    def test_expiry_uses_serbian_local_date_not_us_order(self):
        expected=int(datetime(2026,12,4,tzinfo=ZoneInfo('Europe/Belgrade')).timestamp())
        self.assertEqual(halo.expiry_from_text('Ističe 4.12.2026. Pregleda 14'),expected)
        self.assertIsNone(halo.expiry_from_text('Datum nije dostupan'))

    def test_paid_tiers_and_non_device_categories_cannot_enable_renewal(self):
        halo.require_free_card('Tip Standardni Aktivan oglas',['/racunari/laptop-racunari'])
        halo.require_free_card('Tip Standardni Aktivan oglas',['/telefoni/samsung'])
        for text,paths in [('Tip Premium',['/racunari']),('Tip Standardni Čeka plaćanje',['/racunari']),('Tip Standardni',['/nekretnine']),('Tip Standardni',['https://evil.test/racunari'])]:
            with self.subTest(text=text,paths=paths),self.assertRaises(halo.HaloFeeRequired):
                halo.require_free_card(text,paths)

    def test_provider_stopped_is_not_treated_as_paused_or_missing(self):
        with self.assertRaisesRegex(halo.HaloNeedsReview,'provider_stopped'):
            halo.classify_card('Stopiran oglas')
        self.assertEqual(halo.classify_card('Čeka korekturu'),'pending_review')
        self.assertEqual(halo.classify_card('Istekao oglas'),'expired')
        self.assertEqual(halo.classify_card('Pauziran oglas',activate=True),'paused')
        self.assertEqual(halo.classify_card('Aktivan oglas',pause=True),'active')

    def test_payload_requires_public_price_and_unique_sku_without_mutating_input(self):
        source=payload();source['description']='Sačuvane ručno unete karakteristike.'
        original=copy.deepcopy(source);prepared=halo.prepare_payload(source)
        self.assertIn('RK-27',prepared['description'])
        self.assertEqual(source,original)
        for bad in ['NaN','Infinity','-1','0','1.001']:
            with self.subTest(price=bad),self.assertRaises(halo.HaloNeedsReview):
                halo.prepare_payload(dict(source,price_eur=bad))

    def test_external_contacts_in_copy_and_overlength_are_rejected(self):
        for body in ['https://reklik.shop/proizvod/27/','Kontakt sales@example.test','www.example.test','ReKlik.shop']:
            with self.subTest(body=body),self.assertRaises(halo.HaloNeedsReview):
                halo.prepare_payload(dict(payload(),description=body))
        with self.assertRaises(halo.HaloNeedsReview):halo.prepare_payload(dict(payload(),title='x'*51))
        with self.assertRaises(halo.HaloNeedsReview):halo.prepare_payload(dict(payload(),description='x'*1500))

    def test_owned_allowlist_refuses_other_account_ads(self):
        client=halo.HaloBrowser(owned_ids=[ID])
        self.assertEqual(client._owned(ID),ID)
        for bad in [OTHER,'javascript:void(0)',True]:
            with self.assertRaises(halo.HaloNeedsReview):client._owned(bad)
        with self.assertRaises(halo.HaloNeedsReview):halo.HaloBrowser(cdp_url='https://external.test:9336')

    def test_known_remote_id_never_enters_creation_flow(self):
        client=halo.HaloBrowser(owned_ids=[ID])
        with patch.object(client,'_existing_candidate',side_effect=AssertionError('Must refuse before UI')):
            with self.assertRaisesRegex(halo.HaloNeedsReview,'known_id'):
                client.create(dict(payload(),remote_id=ID))

    def test_photo_conversion_preserves_decoded_pixels_and_rejects_other_files(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'actual.webp'
            Image.new('RGB',(4,3),(52,101,219)).save(source,format='WEBP',lossless=True)
            client=halo.HaloBrowser(state_dir=root/'adapter',allowed_photo_roots=[root])
            converted=Path(client._photos([str(source)])[0])
            self.assertEqual(converted.suffix,'.png')
            with Image.open(source) as a,Image.open(converted) as b:
                self.assertEqual((a.mode,a.size,a.tobytes()),(b.mode,b.size,b.tobytes()))
            secret=root/'not-an-image.json';secret.write_text('{"private":"not for upload"}')
            with self.assertRaises(Exception):client._photos([str(secret)])
            with self.assertRaises(halo.HaloNeedsReview):client._photos(['/etc/passwd'])

    def test_creation_reconciles_existing_and_does_not_submit(self):
        client=halo.HaloBrowser(owned_ids=[ID])
        existing={'status':'active','remote_id':ID,'url':halo.ORIGIN+'/racunari/laptop/dell/'+ID,'auto_renew':True,'expires_at':1}
        with patch.object(client,'_existing_candidate',return_value=existing),patch.object(client,'_open_create_form',side_effect=AssertionError('Duplicate')):
            self.assertEqual(client.create(payload())['remote_id'],ID)

    def test_auth_failure_after_publish_is_uncertain_and_cannot_be_retried_as_create(self):
        client=halo.HaloBrowser(owned_ids=[ID]);client.page=Mock()
        def submitted_then_disconnected():
            client._submission_attempted=True
            raise halo.HaloAuthRequired('post_submit_login_required')
        with patch.object(client,'_existing_candidate',return_value=None),patch.object(client,'_open_create_form'),patch.object(client,'_fill'),patch.object(client,'_free_step'),patch.object(client,'_verify_form'),patch.object(client,'_submit_free',side_effect=submitted_then_disconnected):
            with self.assertRaises(halo.HaloSubmissionUncertain):client.create(payload())

    def test_missing_known_listing_is_not_recreated_or_activated(self):
        client=halo.HaloBrowser(owned_ids=[ID])
        missing={'status':'missing','remote_id':ID,'url':None,'auto_renew':None,'expires_at':None}
        with patch.object(client,'inspect',return_value=missing),patch.object(client,'create',side_effect=AssertionError('Duplicate')):
            self.assertEqual(client.deactivate(ID)['status'],'missing')
            with self.assertRaises(halo.HaloNeedsReview):client.activate(ID)
            with self.assertRaises(halo.HaloNeedsReview):client.update(ID,payload())
            with self.assertRaises(halo.HaloNeedsReview):client.ensure_renewal(ID)


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is unavailable in this Python environment')
class HaloOfflineUIFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright=sync_playwright().start()
        try:
            cls.browser=cls.playwright.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True)
        except Exception as error:
            cls.playwright.stop();raise unittest.SkipTest('Isolated fixture Chrome unavailable: '+type(error).__name__)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.playwright.stop()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.context=self.browser.new_context();self.addCleanup(self.context.close)
        self.page=self.context.new_page()
        self.state={'active':True,'auto':True,'title':'Dell Latitude 3590','body':'Testirani uređaj.\nŠifra: RK-27','price':'150','phone':'+38163697007','email':'sales@example.test','status':'active'}
        self.actions=[];self.paid_total='0,00'
        self.context.route('**/*',self.route)
        self.client=halo.HaloBrowser(owned_ids=[ID],state_dir=Path(self.temp.name))
        self.client.page=self.page;self.page.set_default_timeout(3000)
        self.client._profile(force=True)

    def route(self,route):
        from urllib.parse import urlsplit
        path=urlsplit(route.request.url).path
        if path=='/fixture/toggle':
            self.state['active']=not self.state['active'];self.actions.append('toggle')
            route.fulfill(json={'ok':True});return
        if path=='/fixture/auto':
            self.state['auto']=not self.state['auto'];self.actions.append('auto')
            route.fulfill(json={'ok':True});return
        if path=='/fixture/save':
            data=route.request.post_data_json
            self.state.update(data);self.actions.append('save')
            route.fulfill(json={'ok':True});return
        if path.startswith('/izmeni-oglas/'):
            route.fulfill(content_type='text/html',body=self.edit_html());return
        if path.startswith('/profil'):
            route.fulfill(content_type='text/html',body=self.profile_html());return
        route.fulfill(status=404,body='Offline fixture: external request blocked')

    def profile_html(self):
        action='Pauziraj' if self.state['active'] else 'Aktiviraj'
        state='Aktivan oglas' if self.state['active'] else 'Pauziran oglas'
        if self.state['status']=='pending_review':state='Čeka korekturu';action=''
        toggle_html=f'''<a class="toggle-deactivate" href="javascript:void(0)" onclick="fetch('/fixture/toggle',{{method:'POST'}})">{action}</a>''' if action else ''
        return f'''<!doctype html><html><meta charset="utf-8"><body><div>2 oglasa</div>
        <div class="product-item"><a href="/racunari/laptop/dell/{ID}?kid=1">{html.escape(self.state['title'])}</a>
        <div>Tip Standardni</div><div>Ističe 4.12.2026.</div><div>{state}</div>
        <a href="/racunari/laptop-racunari">Market &gt; Računari</a>
        <a href="/izmeni-oglas/{ID}">Izmeni</a>
        <a class="auto toggle-auto-renewal {'active' if self.state['auto'] else ''}" href="javascript:void(0)" onclick="fetch('/fixture/auto',{{method:'POST'}})">Auto. obnova</a>
        {toggle_html}
        </div><div class="product-item"><div>Tip Standardni Aktivan oglas</div>
        <a href="/izmeni-oglas/{OTHER}">Izmeni</a><a class="toggle-deactivate" href="javascript:void(0)" onclick="throw Error('Unrelated ad touched')">Pauziraj</a>
        </div></body></html>'''

    def edit_html(self):
        s=self.state
        return f'''<!doctype html><html><meta charset="utf-8"><body><input name="Id" value="{ID}" type="hidden">
        <input name="Title" value="{html.escape(s['title'],quote=True)}">
        <select name="proizvodjac"><option>Dell</option></select><select name="stanje"><option>Polovno</option><option>Novo</option></select>
        <div contenteditable="true" onblur="document.querySelector('textarea').value=this.innerText">{html.escape(s['body'])}</div>
        <textarea name="TextHtml">{html.escape(s['body'])}</textarea><input name="cena" value="{s['price']}"><select name="cena_unit"><option>EUR</option></select>
        <section><input id="PhoneNumber1" value="{s['phone']}"><input id="PhoneNumber2"><input id="Email" value="{s['email']}"><input id="show-phone-auto" type="checkbox" checked></section>
        <section style="display:none"><input id="Email" value="hidden-registration@example.test"></section>
        <div id="dropzone-previews"><div class="uploaded-image-item" style="width:40px;height:40px">Actual photo</div></div>
        <button class="save-changes" onclick="saveFixture()">Snimi izmene</button><button class="save-changes" onclick="saveFixture()">Snimi izmene</button>
        <script>function saveFixture(){{fetch('/fixture/save',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{title:document.querySelector('[name=Title]').value,body:document.querySelector('textarea').value,price:document.querySelector('[name=cena]').value,phone:document.querySelector('#PhoneNumber1').value,email:document.querySelector('#Email').value}})}})}}</script>
        </body></html>'''

    def test_read_only_inspection_has_observed_url_expiry_and_auto_state(self):
        before=list(self.actions);state=self.client.inspect(ID)
        self.assertEqual(state['status'],'active');self.assertTrue(state['auto_renew'])
        self.assertTrue(state['url'].endswith('/'+ID));self.assertEqual(self.actions,before)

    def test_deactivate_switches_off_auto_first_and_only_pauses_owned_id(self):
        state=self.client.deactivate(ID)
        self.assertEqual(state['status'],'paused');self.assertFalse(state['auto_renew'])
        self.assertEqual(self.actions,['auto','toggle'])

    def test_reactivate_then_enable_free_renewal_preserves_same_id(self):
        self.state.update(active=False,auto=False);self.client._profile(force=True)
        state=self.client.activate(ID);self.assertEqual(state['status'],'active')
        state=self.client.ensure_renewal(ID)
        self.assertEqual(state['remote_id'],ID);self.assertTrue(state['auto_renew'])
        self.assertEqual(self.actions,['toggle','auto'])

    def test_provider_pending_review_is_queued_not_recreated_or_activated(self):
        self.state['status']='pending_review';self.client._profile(force=True)
        state=self.client.deactivate(ID)
        self.assertEqual(state['status'],'pending_review');self.assertTrue(state['pending_pause'])
        self.assertEqual(self.actions,['auto'])

    def test_find_existing_reads_sku_and_returns_mapping_without_submission(self):
        existing=self.client.find_existing(payload())
        self.assertEqual(existing['remote_id'],ID);self.assertTrue(existing['adopted'])
        self.assertEqual(self.actions,[])

    def test_find_existing_wrong_sku_never_adopts_or_publishes(self):
        self.state['body']='Different device. RK-99'
        with self.assertRaises(halo.HaloNeedsReview):self.client.find_existing(payload())
        self.assertEqual(self.actions,[])

    def test_edit_uses_same_remote_id_and_verifies_readback(self):
        changed=payload();changed.update(description='Ručna specifikacija ostaje. Šifra: RK-27',price_eur='155',replace_photos=False)
        result=self.client.update(ID,changed)
        self.assertEqual(result['remote_id'],ID);self.assertEqual(self.state['price'],'155')
        self.assertEqual(self.actions,['save']);self.assertIn('Ručna specifikacija',self.state['body'])

    def test_photo_removal_does_not_skip_items_when_dom_indices_shift(self):
        self.client.page.set_content('''<div id="dropzone-previews">
          <div class="uploaded-image-item"><a class="btn-remove-image" onclick="this.parentNode.remove()">Remove one</a></div>
          <div class="uploaded-image-item"><a class="btn-remove-image" onclick="this.parentNode.remove()">Remove two</a></div>
          <div class="uploaded-image-item"><a class="btn-remove-image" onclick="this.parentNode.remove()">Remove three</a></div>
          </div>''')
        self.client._remove_photos()
        self.assertEqual(self.client.page.locator('.uploaded-image-item').count(),0)

    def test_saved_edit_waits_for_asynchronous_gallery_hydration(self):
        source=payload();self.state.update(title=source['title'],body=source['description'])
        self.client.page.set_content(self.edit_html())
        self.client.page.evaluate('''()=>{
            const photos=document.querySelector('#dropzone-previews');
            const saved=photos.innerHTML;photos.innerHTML='';
            setTimeout(()=>photos.innerHTML=saved,250);
        }''')
        self.client._verify_saved_edit(source)
        self.assertEqual(self.client.page.locator('.uploaded-image-item').count(),1)

    def test_saved_edit_refreshes_stale_readback_without_resubmitting(self):
        source=payload();self.state.update(title=source['title'],body='Previous cached body')
        self.client.page.goto(halo.ORIGIN+'/izmeni-oglas/'+ID)
        self.state['body']=source['description']
        before=list(self.actions)
        self.client._verify_saved_edit(source,refresh=True)
        self.assertEqual(self.actions,before)
        self.assertEqual(halo.plain_text(self.client.page.locator('textarea').input_value()),halo.plain_text(source['description']))

    def test_span_100_control_exposes_all_26_cards(self):
        cards=''.join(f'<div class="product-item">Tip Standardni {n}</div>' for n in range(20))
        script="for(let n=20;n<26;n++){let d=document.createElement('div');d.className='product-item';d.innerText='item '+n;document.body.appendChild(d)}"
        fixture=f'<body>26 oglasa<div><span>20</span>/<span>50</span>/<span onclick="{script}">100</span></div>{cards}</body>'
        self.context.unroute('**/*');self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=fixture))
        self.client._profile(force=True)
        self.assertEqual(self.page.locator('.product-item').count(),26)

    def test_nonzero_checkout_total_is_rejected_before_submit(self):
        fixture='''<body><label for="IsBought3">Standardni oglas</label><input type="checkbox" id="IsBought3"><input type="checkbox" id="IsBought1"><input type="checkbox" id="IsBought2"><span class="total-amount">600,00</span></body>'''
        self.context.unroute('**/*');self.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=fixture))
        self.page.goto(halo.ORIGIN+'/postavite-oglas/korak-3')
        with self.assertRaises(halo.HaloFeeRequired):self.client._free_step()
        self.assertEqual(self.actions,[])


if __name__=='__main__':
    unittest.main()
