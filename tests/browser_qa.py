"""Mobile inventory and COD browser checks, using disposable state and no card API.

Run with a Python containing Playwright; application dependencies use the service venv.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(Path.home()/'.local/state/reklik/venv/bin/python')
with tempfile.TemporaryDirectory(prefix='reklik-browser-') as state:
    env = dict(os.environ, REKLIK_STATE=state, REKLIK_PORT='4341')
    fixture = '''
import json
from pathlib import Path
from PIL import Image
from werkzeug.security import generate_password_hash
import store
store.init_db()
(store.STATE/'private.json').write_text(json.dumps({'secret':'browser-test-secret','password_hash':generate_password_hash('browser-test-password')}))
Image.new('RGB',(1200,900),(218,220,223)).save(store.STATE/'camera.jpg')
Image.new('RGB',(3200,2400),(190,193,198)).save(store.STATE/'camera-large.jpg')
'''
    subprocess.run([PYTHON,'-c',fixture],cwd=ROOT,env=env,check=True)
    log = open(Path(state)/'server.log','w')
    launcher='import app; from waitress import serve; app.queue_publish=lambda:None; serve(app.app,host="127.0.0.1",port=4341)'
    server = subprocess.Popen([PYTHON,'-c',launcher],cwd=ROOT,env=env,stdout=log,stderr=log)
    try:
        origin='http://127.0.0.1:4341'
        for _ in range(80):
            try:
                if urlopen(origin+'/health',timeout=1).status==200: break
            except OSError: time.sleep(.1)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox','--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream'])
            context=browser.new_context(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
            page=context.new_page()
            errors=[]
            page.on('pageerror',lambda error: errors.append(str(error)))
            def layout(label):
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), label+' horizontal overflow'
                controls=page.locator('input:not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea').evaluate_all('(els)=>els.filter(e=>e.getClientRects().length).map(e=>({size:parseFloat(getComputedStyle(e).fontSize),height:e.getBoundingClientRect().height}))')
                assert all(c['size']>=16 and c['height']>=44 for c in controls), (label,controls)

            page.goto(origin+'/admin/')
            page.locator('[name=password]').fill('browser-test-password')
            page.get_by_role('button',name='Prijavi se').click()
            page.wait_for_url('**/admin/')
            layout('mobile inventory')
            assert page.locator('input[capture=environment]').count()==1
            page.locator('.inventory-advanced summary').click()
            page.route('**/admin/specifications',lambda route:route.fulfill(json={'status':'completed','result':{'specs':{'Procesor':'Intel Core i5','Memorija':'16 GB RAM','Skladište':'512 GB SSD'},'description':'Automatski opis uređaja.','category_slug':'laptopovi','sources':[],'notes':''}}))
            page.locator('[name=title]').fill('Lenovo ThinkPad T14 — mobilni test')
            page.locator('[name=brand]').fill('Lenovo')
            page.locator('[name=price]').fill('300')
            page.locator('[name=quantity]').fill('3')
            page.locator('[name=specs]').fill('Procesor: Intel Core i5\nMemorija: 16 GB\nSkladište: 512 GB SSD')
            page.locator('textarea[name=description]').fill('Testiran i spreman za rad. Fotografija je isključivo testni uzorak.')
            page.locator('input[capture=environment]').set_input_files(str(Path(state)/'camera.jpg'))
            page.locator('[data-photo-preview] img').first.wait_for()
            page.locator('input[capture=environment]').set_input_files(str(Path(state)/'camera-large.jpg'))
            page.wait_for_function('() => document.querySelectorAll("[data-photo-preview] img").length===2')
            # Repeated camera captures accumulate; a discarded shot can be retaken.
            page.locator('[data-photo-preview] button').first.click()
            assert page.locator('[data-photo-preview] img').count()==1
            page.locator('input[capture=environment]').set_input_files(str(Path(state)/'camera.jpg'))
            page.wait_for_function('() => document.querySelector("[data-staged-photos]").files.length===2')
            assert '2 / 10' in page.locator('[data-photo-status]').inner_text()
            page.locator('[name=active]').check()
            page.get_by_role('button',name='Sačuvaj uređaj').click()
            page.wait_for_url('**/admin/?**')
            assert page.locator('.error-message').count()==0, page.locator('body').inner_text()
            page.locator('.row-actions').get_by_role('link',name='Uredi',exact=True).click()
            assert page.locator('[name=keep_images]').count()==2
            assert page.locator('[name=price]').input_value()=='300.00'
            page.locator('.inventory-advanced summary').click()
            page.locator('textarea[name=description]').fill('Uređen opis sa telefona. Sertifikovano testiran uređaj.')
            page.get_by_role('button',name='Sačuvaj uređaj').click()
            page.wait_for_url('**/admin/?**')
            page.locator('.row-actions').get_by_role('link',name='Napravi sličan').click()
            assert page.locator('textarea[name=description]').input_value()=='Uređen opis sa telefona. Sertifikovano testiran uređaj.'
            assert page.locator('.photo-preview img').count()==0
            page.goto(origin+'/admin/?tab=categories')
            form=page.locator('form[action="/admin/category"]').last
            form.locator('[name=name]').fill('Monitori')
            form.locator('[name=description]').fill('Testirani monitori')
            form.locator('[name=shipping_post-express]').fill('700')
            form.get_by_role('button',name='Dodaj kategoriju').click()
            page.wait_for_url('**/admin/?**')
            assert page.locator('input[value=Monitori]').count()==1
            for width in (320,390,768,1440):
                page.set_viewport_size({'width':width,'height':900})
                for path in ('/','/katalog/','/proizvod/','/admin/','/admin/?tab=settings','/admin/?tab=categories','/admin/?tab=orders','/admin/?tab=ai'):
                    if path=='/proizvod/':
                        page.goto(origin+'/katalog/')
                        page.locator('.product-card h3 a').first.click()
                    else: page.goto(origin+path)
                    layout(str(width)+path)
                    if width==390 and path in ('/','/admin/','/admin/?tab=settings'):
                        page.screenshot(path='/tmp/reklik-'+{'/':'home','/admin/':'admin','/admin/?tab=settings':'settings'}[path]+'-mobile.png',full_page=True)
                if width==1440:
                    page.goto(origin+'/');page.screenshot(path='/tmp/reklik-home-desktop.png',full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.goto(origin+'/katalog/')
            page.locator('.product-card h3 a').first.click()
            page.get_by_role('button',name='Kupi sada').click()
            page.wait_for_url('**/checkout/**')
            page.locator('[data-order-submit]').wait_for(state='visible')
            layout('mobile COD checkout')
            page.locator('[name=name]').fill('Ђорђе Живковић')
            page.locator('[name=phone]').fill('+381 63 123 4567')
            page.locator('[name=email]').fill('buyer@example.test')
            page.locator('[name=city]').fill('Горња Добриња')
            page.locator('[name=courier]').select_option('aks')
            page.wait_for_function('() => !document.querySelector("[data-order-submit]").disabled')
            assert page.locator('[data-quote=shipping_minor]').inner_text().replace('\xa0',' ').replace('\u202f',' ') == '7 €'
            page.locator('[name=payment_method][value=cod]').check()
            page.locator('[name=terms]').check()
            page.screenshot(path='/tmp/reklik-checkout-mobile.png',full_page=True)
            page.locator('[data-order-submit]').click()
            page.wait_for_url('**/order/**')
            assert 'Pozvaćemo vas' in page.locator('body').inner_text()
            assert 'Горња Добриња' in page.locator('body').inner_text()
            assert '307 €' in page.locator('.summary-lines .total').inner_text()
            layout('mobile COD confirmation')
            page.screenshot(path='/tmp/reklik-order-mobile.png',full_page=True)
            confirmation=page.url
            page.goto(origin+'/admin/?tab=orders')
            assert 'Ђорђе Живковић' in page.locator('body').inner_text()
            page.locator('[name=status]').select_option('confirmed')
            page.get_by_role('button',name='Sačuvaj status').click()
            page.wait_for_url('**/admin/?**')
            assert 'confirmed' in page.locator('.status-tag').inner_text()
            page.goto(origin+'/admin/')
            page.locator('[name=brand]').fill('Dell')
            page.locator('[name=title]').fill('Latitude 5420')
            page.locator('[name=price]').fill('250')
            page.locator('input[capture=environment]').set_input_files(str(Path(state)/'camera.jpg'))
            page.wait_for_function('() => document.querySelector("[data-spec-status]").textContent.includes("su popunjeni")')
            assert 'Intel Core i5' in page.locator('[name=specs]').input_value()
            assert not page.locator('.inventory-advanced').evaluate('e=>e.open')
            page.get_by_role('button',name='Sačuvaj uređaj').click()
            page.wait_for_url('**/admin/?**')
            assert 'Latitude 5420' in page.locator('.inventory-row').first.inner_text()
            assert 'Objavljeno' in page.locator('.inventory-row').first.inner_text()
            page.goto(origin+'/admin/')
            context.grant_permissions(['camera'],origin=origin)
            page.locator('[data-open-camera]').click()
            page.wait_for_function('() => document.querySelector("[data-camera-video]").videoWidth>0')
            for n in (1,2):
                page.locator('[data-capture-photo]').click()
                page.wait_for_function('(n) => document.querySelector("[data-staged-photos]").files.length===n',arg=n)
            page.locator('[data-close-camera]').click()
            assert page.locator('[data-camera-video]').evaluate('e=>e.srcObject===null')
            page.locator('[name=brand]').fill('Dell')
            page.locator('[name=title]').fill('Latitude sa kamerom u stranici')
            page.locator('[name=price]').fill('250')
            page.wait_for_function('() => document.querySelector("[data-spec-status]").textContent.includes("su popunjeni")')
            page.locator('[data-inventory-save]').click()
            page.wait_for_url('**/admin/?**')
            page.locator('.inventory-row').first.get_by_role('link',name='Uredi',exact=True).click()
            assert page.locator('[name=keep_images]').count()==2
            assert not errors,errors
            browser.close()
            print(json.dumps({'result':'passed','widths':[320,390,768,1440],'flows':['multiple camera uploads, removal and retake','in-page camera capture and stream release','inventory edit','reuse text','create category','COD village Cyrillic no street/postcode','AKS quote','phone-confirm order'],'javascript_errors':errors}))
    finally:
        server.terminate()
        server.wait(timeout=15)
        log.close()
