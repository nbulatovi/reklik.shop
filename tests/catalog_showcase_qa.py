"""Category rotation, filters and consistent admin actions with disposable products."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.request import urlopen
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PYTHON=str(Path.home()/'.local/state/reklik/venv/bin/python')
QA=Path.home()/'.local/state/reklik/qa'
with tempfile.TemporaryDirectory(prefix='reklik-showcase-') as temporary:
    state=Path(temporary);env=dict(os.environ,REKLIK_STATE=temporary)
    fixture='''
import store,json,inventory_rules
from PIL import Image
from werkzeug.security import generate_password_hash
(store.STATE/'private.json').write_text(json.dumps({'secret':'showcase-only','password_hash':generate_password_hash('showcase-only')}))
image='abcdef0123456789abcdef01.webp';Image.new('RGB',(1000,750),'silver').save(store.UPLOADS/image)
with store.connect(True) as c:
 for i,category in enumerate((1,2,3,4,1,2,4,2),1):
  c.execute('INSERT INTO products(id,category_id,title,slug,brand,price_cents,base_price_cents,quantity,condition,specs,images,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,?,?,?,1,?,?)',
   (i,category,'QA device '+str(i),'qa-showcase-'+str(i),'Lenovo' if i<5 else 'Samsung',i*10000,i*9500,inventory_rules.GRADES[1],json.dumps({'RAM':'8 GB'}),json.dumps([image]),store.now()+i,store.now()+i))
'''
    subprocess.run([PYTHON,'-c',fixture],cwd=ROOT,env=env,check=True)
    log=open(state/'server.log','w')
    server=subprocess.Popen([PYTHON,'-c','import app;from waitress import serve;app.queue_publish=lambda:None;serve(app.app,host="127.0.0.1",port=4345)'],cwd=ROOT,env=env,stdout=log,stderr=log)
    try:
        origin='http://127.0.0.1:4345'
        for attempt in range(80):
            try:
                if urlopen(origin+'/health',timeout=1).status==200:break
            except OSError:time.sleep(.1)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
            for enabled in (False,True):
                context=browser.new_context(java_script_enabled=enabled)
                page=context.new_page()
                errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                ids=lambda:[int(x) for x in page.locator('.product-card:not([hidden])').evaluate_all('els=>els.map(e=>e.dataset.id)')]
                for width in (320,390,768,1440):
                    page.set_viewport_size({'width':width,'height':900})
                    for path in ('/','/katalog/'):
                        page.goto(origin+path)
                        assert ids()==[5,8,3,7,1,6,4,2],(enabled,width,path,ids())
                        assert 'Radnja u Novom Beogradu' not in page.locator('body').inner_text()
                        assert 'Pomoć pri izboru i preuzimanje' not in page.locator('body').inner_text()
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    if enabled:
                        if not page.locator('[data-filter-panel]').evaluate('e=>e.open'):page.locator('[data-filter-panel] summary').click()
                        page.locator('[data-brand-filter]').select_option('Lenovo')
                        assert ids()==[1,2,3,4],ids()
                        page.locator('[data-brand-filter]').select_option('')
                        page.locator('[data-price-max]').fill('500')
                        assert ids()==[5,2,3,4,1],ids()
                        page.locator('[data-price-max]').fill('')
                        page.locator('[data-sort]').select_option('low');assert ids()==list(range(1,9))
                        page.locator('[data-sort]').select_option('recent');assert ids()==list(range(8,0,-1))
                        page.locator('[data-sort]').select_option('featured');assert ids()==[5,8,3,7,1,6,4,2]
                    page.goto(origin+'/kategorija/laptopovi/')
                    assert ids()==[5,1]
                    page.goto(origin+'/admin/')
                    if page.locator('[name=password]').count():
                        page.locator('[name=password]').fill('showcase-only');page.get_by_role('button',name='Prijavi se').click()
                    page.locator('.row-actions').first.wait_for()
                    actions=page.locator('.row-actions .inventory-action').evaluate_all('els=>els.map(e=>{const s=getComputedStyle(e);return {font:s.fontSize,weight:s.fontWeight,padding:s.padding,border:s.borderRadius,height:e.getBoundingClientRect().height,scroll:e.scrollWidth,width:e.clientWidth}})')
                    assert len(actions)==40,actions
                    assert all(a['font']=='16px' and a['height']>=44 and a['scroll']<=a['width']+1 for a in actions),actions
                    assert len({(a['weight'],a['padding'],a['border']) for a in actions})==1
                    metrics=page.evaluate('({sw:document.documentElement.scrollWidth,iw:innerWidth})')
                    if metrics['sw']>metrics['iw']+1:
                        bad=page.locator('body *').evaluate_all('els=>els.map(e=>({tag:e.tagName,cls:e.className,left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right,sw:e.scrollWidth,cw:e.clientWidth})).filter(e=>e.right>innerWidth+1||e.sw>e.cw+2).slice(0,20)')
                        page.screenshot(path=str(QA/'admin-showcase-failure.png'),full_page=True)
                        raise AssertionError((enabled,width,metrics,bad))
                    if enabled and width in (390,1440):page.locator('.inventory-row').first.screenshot(path=str(QA/('admin-actions-'+str(width)+'-2026-10-26.png')))
                assert not errors,errors
                context.close()
            browser.close()
        report={'passed':True,'widths':[320,390,768,1440],'javascript_enabled_and_disabled':True,'sequence':'laptop-phone-tablet-other, skipping exhausted groups','filters_and_sorting':True,'admin_action_styles_consistent':True,'removed_shop_notice':True,'production_inventory_modified':False}
        (QA/'catalog-showcase-2026-10-26.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
    finally:
        server.terminate();server.wait(timeout=15);log.close()
