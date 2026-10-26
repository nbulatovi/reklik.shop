"""Native HTML analytics with JavaScript disabled; isolated COD and mobile admin QA."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from urllib.parse import urlsplit, urljoin
from html import escape
from urllib.request import urlopen
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PYTHON=str(Path.home()/'.local/state/reklik/venv/bin/python')
QA=Path.home()/'.local/state/reklik/qa'
BACKEND='https://reklik-analytics-qa.trycloudflare.com'
with tempfile.TemporaryDirectory(prefix='reklik-analytics-',dir=QA) as temporary:
    state=Path(temporary);env=dict(os.environ,REKLIK_STATE=temporary,REKLIK_SECURE_COOKIES='1')
    fixture='''
import json,store,rates,inventory_rules
from datetime import datetime
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash
store.set_settings({'site_url':'https://reklik.shop','eur_rsd':'117.2','eur_rsd_date':datetime.now(ZoneInfo('Europe/Belgrade')).date().isoformat(),'eur_rsd_source':rates.SOURCE})
(store.STATE/'private.json').write_text(json.dumps({'secret':'analytics-qa','password_hash':generate_password_hash('analytics-qa')}))
(store.STATE/'backend.json').write_text(json.dumps({'backend':'https://reklik-analytics-qa.trycloudflare.com'}))
with store.connect(True) as c:
 c.execute("INSERT INTO products(id,category_id,title,slug,price_cents,base_price_cents,quantity,condition,description,specs,active,created_at,updated_at) VALUES(1,1,'Analitika QA','qa',30000,28500,10,?,?,?,1,?,?)",(inventory_rules.GRADES[2],'Opis uređaja. '*100,json.dumps({'RAM':'16 GB','Skladište':'512 GB'}),store.now(),store.now()))
import publisher
publisher.PUBLIC=store.STATE/'public';publisher.export()
'''
    subprocess.run([PYTHON,'-c',fixture],cwd=ROOT,env=env,check=True)
    source=(state/'public/assets/site.js').read_text()
    assert '/analytics/' not in source and '/views/product/' not in source
    log=open(state/'server.log','w')
    servers=[subprocess.Popen([PYTHON,'-c','import app;from waitress import serve;app.queue_publish=lambda:None;serve(app.app,host="127.0.0.1",port=4348)'],cwd=ROOT,env=env,stdout=log,stderr=log),
             subprocess.Popen([PYTHON,'-m','http.server','4349','--bind','127.0.0.1','--directory',str(state/'public')],stdout=log,stderr=log)]
    try:
        for port in (4348,4349):
            for _ in range(80):
                try:
                    if urlopen('http://127.0.0.1:'+str(port)+'/').status==200:break
                except OSError:time.sleep(.1)
        with sync_playwright() as p:
            proxy=p.request.new_context()
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
            cases=[]
            for scripts in (False,True):
                context=browser.new_context(java_script_enabled=scripts,viewport={'width':390,'height':844})
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                events=[]
                def forward(route):
                    req=route.request;parsed=urlsplit(req.url)
                    is_backend=parsed.hostname==urlsplit(BACKEND).hostname
                    if not is_backend and parsed.hostname!='reklik.shop':return route.abort()
                    headers={k:v for k,v in req.headers.items() if k.lower() not in ('host','content-length')}
                    headers.update({'Host':parsed.netloc,'CF-Connecting-IP':'192.0.2.'+('41' if scripts else '40')})
                    url='http://127.0.0.1:'+('4348' if is_backend else '4349')+parsed.path+('?' + parsed.query if parsed.query else '')
                    response=proxy.fetch(url,method=req.method,headers=headers,data=req.post_data_buffer,max_redirects=0)
                    if '/analytics/' in parsed.path:events.append(parsed.path)
                    if response.status in (301,302,303,307,308):
                        # Playwright bypasses interception after the first URL in
                        # some redirect chains; reissue this QA navigation.
                        target=escape(urljoin(req.url,response.headers['location']),quote=True)
                        forwarded={k:v for k,v in response.headers.items() if k not in ('location','content-length','content-type','content-encoding')}
                        route.fulfill(status=200,headers=forwarded,content_type='text/html',body='<meta http-equiv="refresh" content="0;url='+target+'">')
                    else:route.fulfill(response=response)
                context.route('**/*',forward)
                page.goto('https://reklik.shop/proizvod/1/')
                page.wait_for_timeout(200)
                assert '/analytics/view/1.gif' in events,(scripts,events)
                page.reload();page.wait_for_timeout(100)
                page.locator('.warranty-options summary').click();page.locator('#warranty').select_option('2')
                for fraction in (0,1,.5,0):
                    page.evaluate('(f)=>{document.documentElement.style.scrollBehavior="auto";scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*f)}',fraction)
                    page.wait_for_timeout(30)
                    assert page.locator('.device-buy-actions').evaluate('e=>e.getBoundingClientRect().bottom<=innerHeight+1')
                page.get_by_role('button',name='Dodaj u korpu').click();page.wait_for_timeout(200)
                assert '/analytics/cart/1' in events,(scripts,events)
                assert page.url=='https://reklik.shop/proizvod/1/'
                if scripts:
                    assert page.evaluate('JSON.parse(localStorage.getItem("reklik-cart"))[0].warranty')==2
                    assert page.locator('[data-cart-feedback]').inner_text()=='Dodato u korpu.'
                page.get_by_role('button',name='Kupi sada').click()
                page.wait_for_url('https://reklik.shop/checkout/?buy=1&warranty=2')
                assert '/analytics/buy/1' in events
                if scripts:
                    checkout=page.frame_locator('iframe.store-frame')
                    checkout.locator('[name=name]').wait_for()
                else:
                    # The existing public checkout shell requires essential JS; its
                    # server form itself submits and records attempts without it.
                    page.goto(BACKEND+'/checkout/?buy=1&warranty=2');checkout=page
                checkout.locator('html').evaluate("e=>e.style.scrollBehavior='auto'")
                checkout.locator('[name=name]').fill('Петар Петровић')
                checkout.locator('[name=email]').fill('buyer@example.test')
                checkout.locator('[name=city]').fill('Горња Добриња')
                checkout.locator('[name=terms]').check()
                checkout.locator('[data-order-submit]').click()
                checkout.get_by_role('alert').filter(has_text='Popunite polje: Telefon.').wait_for()
                assert checkout.locator('[name=name]').input_value()=='Петар Петровић'
                checkout.locator('html').evaluate("e=>e.style.scrollBehavior='auto'")
                checkout.locator('[name=phone]').fill('+381 63 1234567')
                checkout.locator('[data-order-submit]').click()
                try:checkout.locator('[data-order-token]').wait_for(timeout=10000)
                except Exception:
                    print({'javascript':scripts,'frames':[(f.url,f.locator('body').inner_text()[:900]) for f in page.frames]})
                    raise
                assert not errors,errors
                cases.append({'javascript':scripts,'native_events':sorted(set(events)),'cod_failed_then_completed':True})
                context.close()
            context=browser.new_context(viewport={'width':390,'height':844});context.route('**/*',forward)
            page=context.new_page();page.goto(BACKEND+'/admin/login/')
            page.locator('[name=password]').fill('analytics-qa');page.get_by_role('button',name='Prijavi se').click();page.wait_for_url(BACKEND+'/admin/')
            page.goto(BACKEND+'/admin/?edit=1#item-analytics')
            for width in (320,390,768,1440):
                page.set_viewport_size({'width':width,'height':900})
                page.locator('#item-analytics').scroll_into_view_if_needed()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
                assert page.locator('.analytics-metrics dt').evaluate_all('els=>els.every(e=>e.scrollWidth<=e.clientWidth+1&&e.scrollHeight<=e.clientHeight+1)'),width
                if width in (390,1440):page.screenshot(path=str(QA/('item-analytics-'+str(width)+'-2026-10-26.png')))
            browser.close();proxy.dispose()
        with sqlite3.connect(state/'store.sqlite3') as c:
            counts=dict(c.execute('SELECT stage,count(DISTINCT visitor_hash) FROM item_events GROUP BY stage'))
            for stage in ('view','buy_now','cart_click','checkout_open','cod_attempt','cod_failed','cod_order'):
                assert counts[stage]==2,(stage,counts)
            assert c.execute('SELECT count(*) FROM orders').fetchone()[0]==2
            records=json.dumps(c.execute('SELECT * FROM item_events').fetchall())
            assert '192.0.2.' not in records and 'buyer@example.test' not in records
        report={'result':'passed','cases':cases,'admin_widths':[320,390,768,1440],'counts':counts,'production_orders_created':0}
        (QA/'item-analytics-2026-10-26.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
    finally:
        for server in servers:server.terminate();server.wait(timeout=15)
        log.close()
