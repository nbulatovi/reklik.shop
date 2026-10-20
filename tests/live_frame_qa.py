"""Exercise public HTTPS framing with an intercepted QA backend and mocked card redirect.

The browser alone substitutes the test backend endpoint; public inventory is untouched.
"""
import json
from html import escape
from urllib.parse import urljoin
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PYTHON=str(Path.home()/'.local/state/reklik/venv/bin/python')
with tempfile.TemporaryDirectory(prefix='reklik-frame-') as temporary:
    state=Path(temporary)
    env=dict(os.environ,REKLIK_STATE=temporary,REKLIK_PORT='4342',REKLIK_SECURE_COOKIES='1')
    fixture='''
import store,json
from PIL import Image
from werkzeug.security import generate_password_hash
store.init_db()
(store.STATE/'private.json').write_text(json.dumps({'secret':'frame-qa-secret','password_hash':generate_password_hash('frame-qa-password')}))
Image.new('RGB',(1200,900),(218,220,223)).save(store.STATE/'camera.jpg')
Image.new('RGB',(2000,1500),(194,197,199)).save(store.STATE/'camera-2.jpg')
with store.connect(True) as c:
 c.execute("INSERT INTO products(category_id,title,slug,price_cents,quantity,active,created_at,updated_at) VALUES(1,'QA uređaj','qa-device',30000,3,1,?,?)",(store.now(),store.now()))
'''
    subprocess.run([PYTHON,'-c',fixture],cwd=ROOT,env=env,check=True)
    launcher='''
import app,payments,store,specifications
specifications.WORKERS.submit=lambda *args:None
from waitress import serve
def fake_checkout(order,origin):
 with store.connect(True) as c:c.execute("UPDATE orders SET checkout_url='https://whop.com/checkout/ch_qa_stub/',plan_id='plan_qa' WHERE id=?",(order['id'],))
 return 'https://whop.com/checkout/ch_qa_stub/'
payments.create_checkout=fake_checkout
app.queue_publish=lambda:None
serve(app.app,host='127.0.0.1',port=4342)
'''
    log=open(state/'server.log','w')
    server=subprocess.Popen([PYTHON,'-c',launcher],cwd=ROOT,env=env,stdout=log,stderr=log)
    try:
        for _ in range(60):
            try:
                if urlopen('http://127.0.0.1:4342/health',timeout=1).status==200:break
            except OSError:time.sleep(.1)
        backend='https://reklik-qa.trycloudflare.com'
        with sync_playwright() as p:
            # Keep the proxy's HTTP cookie jar separate from browser CHIPS cookies.
            proxy_client=p.request.new_context()
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
            page.set_default_timeout(10000)
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            def proxy(route):
                request=route.request
                headers=request.all_headers()
                headers['host']='reklik-qa.trycloudflare.com'
                response=proxy_client.fetch('http://127.0.0.1:4342'+request.url.removeprefix(backend),method=request.method,headers=headers,data=request.post_data_buffer,max_redirects=0)
                if response.status in (301,302,303,307,308):
                    # Playwright intercepts only the first URL of a redirect chain.
                    # Reissue the destination as a fresh navigation within this QA browser.
                    target=escape(urljoin(request.url,response.headers['location']),quote=True)
                    headers={key:value for key,value in response.headers.items() if key not in ('location','content-length','content-type','content-encoding')}
                    route.fulfill(status=200,headers=headers,content_type='text/html',body='<meta http-equiv="refresh" content="0;url='+target+'">')
                else:route.fulfill(response=response)
            page.route(backend+'/**',proxy)
            page.route('https://nbulatovi.github.io/reklik.shop/backend.json?*',lambda route:route.fulfill(json={'backend':backend}))
            page.route('https://whop.com/checkout/ch_qa_stub/',lambda route:route.fulfill(body='<title>QA payment redirect</title><p>No payment occurs.</p>',content_type='text/html'))
            def fill(method):
                page.goto('https://nbulatovi.github.io/reklik.shop/checkout/?buy=1',wait_until='networkidle',timeout=60000)
                frame=page.frame_locator('.store-frame')
                frame.locator('[name=name]').fill('Петар Петровић')
                frame.locator('[name=phone]').fill('+381 63 123 4567')
                frame.locator('[name=email]').fill('buyer@example.test')
                frame.locator('[name=city]').fill('Горња Добриња')
                frame.locator('[name=terms]').check()
                frame.locator('[name=payment_method][value='+method+']').check()
                assert frame.locator('.cookie-fallback').count()==0
                return frame
            frame=fill('cod')
            frame.locator('[data-order-submit]').click()
            try:page.wait_for_url('**/potvrda/?order=*',timeout=10000)
            except Exception:
                print('COD failure',[(f.url,f.locator('body').inner_text()[:2400]) for f in page.frames],flush=True)
                print('JS errors',errors,flush=True)
                raise
            frame.get_by_role('heading',name='Hvala na poverenju.').wait_for()
            assert 'Горња Добриња' in frame.locator('body').inner_text()
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
            page.screenshot(path='/tmp/reklik-frame-cod-mobile.png',full_page=True)
            frame=fill('card')
            frame.locator('[data-order-submit]').click()
            page.wait_for_url('https://whop.com/checkout/ch_qa_stub/',timeout=30000)
            assert page.title()=='QA payment redirect'
            page.route(backend+'/admin/specifications',lambda route:route.fulfill(json={'status':'completed','result':{'specs':{'Procesor':'Intel Core i5','Memorija':'16 GB RAM','Skladište':'512 GB SSD'},'description':'Automatski opis uređaja.','category_slug':'laptopovi','sources':[],'notes':''}}))
            page.goto('https://nbulatovi.github.io/reklik.shop/admin/',wait_until='networkidle',timeout=60000)
            frame=page.frame_locator('.store-frame')
            frame.locator('[name=password]').fill('frame-qa-password')
            frame.get_by_role('button',name='Prijavi se').click()
            try:frame.locator('[data-inventory-form]').wait_for()
            except Exception:
                print('Admin frame failure',[(f.url,f.locator('body').inner_text()[:1400]) for f in page.frames],flush=True)
                raise
            frame.locator('input[capture=environment]').set_input_files(str(state/'camera.jpg'))
            frame.locator('[data-photo-preview] img').first.wait_for()
            frame.locator('input[capture=environment]').set_input_files(str(state/'camera-2.jpg'))
            frame.locator('[name=brand]').fill('Dell')
            frame.locator('[name=title]').fill('Latitude 5420')
            frame.locator('[name=price]').fill('249')
            page.wait_for_function('() => document.querySelector(".store-frame")!==null')
            frame.locator('[data-spec-status]').filter(has_text='su popunjeni').wait_for()
            assert frame.locator('[data-photo-preview] img').count()==2
            assert 'Intel Core i5' in frame.locator('[name=specs]').input_value()
            assert not errors,errors
            browser.close()
            print(json.dumps({'result':'passed','flows':['public storefront iframe with intercepted QA backend','partitioned session cookie','mobile Cyrillic village COD','outer confirmation URL','top-level card redirect (mock, no charge)','framed admin login, repeated camera inputs and AI autofill'],'javascript_errors':errors}))
    finally:
        server.terminate();server.wait(timeout=15);log.close()
