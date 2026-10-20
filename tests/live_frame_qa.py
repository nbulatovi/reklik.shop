"""Exercise real HTTPS framing with disposable inventory and a mocked card redirect.

The browser alone substitutes the test backend endpoint; public inventory is untouched.
"""
import json
import os
from pathlib import Path
import re
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
from werkzeug.security import generate_password_hash
store.init_db()
(store.STATE/'private.json').write_text(json.dumps({'secret':'frame-qa-secret','password_hash':generate_password_hash('frame-qa-password')}))
with store.connect(True) as c:
 c.execute("INSERT INTO products(category_id,title,slug,price_cents,quantity,active,created_at,updated_at) VALUES(1,'QA uređaj','qa-device',30000,3,1,?,?)",(store.now(),store.now()))
'''
    subprocess.run([PYTHON,'-c',fixture],cwd=ROOT,env=env,check=True)
    launcher='''
import app,payments,store
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
    tunnel=None
    try:
        for _ in range(60):
            try:
                if urlopen('http://127.0.0.1:4342/health',timeout=1).status==200:break
            except OSError:time.sleep(.1)
        (state/'empty.yml').touch()
        tunnel_log=open(state/'tunnel.log','w')
        tunnel=subprocess.Popen(['/usr/bin/cloudflared','tunnel','--no-autoupdate','--config',str(state/'empty.yml'),'--url','http://127.0.0.1:4342'],env=dict(os.environ,HOME=temporary),stdout=tunnel_log,stderr=tunnel_log)
        backend=None
        for _ in range(120):
            match=re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',(state/'tunnel.log').read_text())
            if match:backend=match.group(0);break
            time.sleep(.4)
        assert backend,'QA tunnel not established'
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True,has_touch=True)
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
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
            page.wait_for_url('**/potvrda/?order=*',timeout=30000)
            frame.get_by_role('heading',name='Hvala na poverenju.').wait_for()
            assert 'Горња Добриња' in frame.locator('body').inner_text()
            assert not page.evaluate('document.documentElement.scrollWidth>innerWidth')
            page.screenshot(path='/tmp/reklik-frame-cod-mobile.png',full_page=True)
            frame=fill('card')
            frame.locator('[data-order-submit]').click()
            page.wait_for_url('https://whop.com/checkout/ch_qa_stub/',timeout=30000)
            assert page.title()=='QA payment redirect'
            assert not errors,errors
            browser.close()
            print(json.dumps({'result':'passed','flows':['public HTTPS iframe','partitioned session cookie','mobile Cyrillic village COD','outer confirmation URL','top-level card redirect (mock, no charge)'],'javascript_errors':errors}))
    finally:
        if tunnel:tunnel.terminate();tunnel.wait(timeout=15);tunnel_log.close()
        server.terminate();server.wait(timeout=15);log.close()
