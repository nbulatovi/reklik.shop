"""All six grades and complete hide/show publication, with disposable inventory."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
PYTHON=str(Path.home()/'.local/state/reklik/venv/bin/python')
QA=Path.home()/'.local/state/reklik/qa'
with tempfile.TemporaryDirectory(prefix='reklik-grade-layout-',dir=QA) as temporary:
    state=Path(temporary);env=dict(os.environ,REKLIK_STATE=temporary)
    fixture='''
import json,store,inventory_rules
from PIL import Image
from werkzeug.security import generate_password_hash
(store.STATE/'private.json').write_text(json.dumps({'secret':'layout-only','password_hash':generate_password_hash('layout-only')}))
store.set_settings({'site_url':'https://reklik.shop'})
image='abcdef0123456789abcdef01.webp'
Image.new('RGB',(1200,900),(218,220,223)).save(store.UPLOADS/image)
with store.connect(True) as c:
 for n,condition in enumerate(inventory_rules.GRADES,1):
  c.execute('INSERT INTO products(id,category_id,title,slug,brand,price_cents,base_price_cents,quantity,condition,specs,images,active,created_at,updated_at) VALUES(?,1,?,?,?,?,?,3,?,?,?,1,?,?)',
   (n,'QA uređaj '+str(n),'qa-grade-'+str(n),'Lenovo',30000,28500,condition,json.dumps({'Procesor':'Core i5','RAM':'16 GB','SSD':'512 GB'}),json.dumps([image]),store.now(),store.now()))
'''
    subprocess.run([PYTHON,'-c',fixture],cwd=ROOT,env=env,check=True)
    def export():
        result=subprocess.run([PYTHON,'-c',"import publisher,store;publisher.PUBLIC=store.STATE/'public';print(publisher.export())"],cwd=ROOT,env=env,check=True,capture_output=True,text=True)
        return result.stdout.strip()
    export()
    log=open(state/'server.log','w')
    processes=[subprocess.Popen([PYTHON,'-c','import app;from waitress import serve;app.queue_publish=lambda:None;serve(app.app,host="127.0.0.1",port=4343)'],cwd=ROOT,env=env,stdout=log,stderr=log),
               subprocess.Popen([PYTHON,'-m','http.server','4344','--bind','127.0.0.1','--directory',str(state/'public')],cwd=ROOT,env=env,stdout=log,stderr=log)]
    try:
        for port in (4343,4344):
            for attempt in range(80):
                try:
                    if urlopen('http://127.0.0.1:'+str(port)+'/',timeout=1).status==200:break
                except OSError:time.sleep(.1)
        backend='http://127.0.0.1:4343';public='http://127.0.0.1:4344'
        screenshots=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
            page=browser.new_page();page.set_default_timeout(10000)
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            def readable(locator,size):
                facts=locator.evaluate_all('''els=>els.map(e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);return {text:e.textContent.trim(),width:r.width,height:r.height,scrollWidth:e.scrollWidth,clientWidth:e.clientWidth,scrollHeight:e.scrollHeight,clientHeight:e.clientHeight,font:parseFloat(s.fontSize),whiteSpace:s.whiteSpace,overflow:s.overflow,textOverflow:s.textOverflow}})''')
                for e in facts:
                    assert e['height']>0 and e['width']>0,e
                    assert e['scrollWidth']<=e['clientWidth']+1,e
                    assert e['scrollHeight']<=e['clientHeight']+1,e
                    assert e['font']>=size,e
                    assert e['whiteSpace']!='nowrap' and e['textOverflow']!='ellipsis',e
                return facts
            def layout(label):
                page.evaluate('() => new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                initial=page.evaluate('({sw:document.documentElement.scrollWidth,iw:innerWidth})')
                if not page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'):
                    page.screenshot(path=str(QA/'grade-layout-failure.png'),full_page=True)
                    nodes=page.locator('body *').evaluate_all('els=>els.map(e=>({tag:e.tagName,cls:e.className,left:e.getBoundingClientRect().left,right:e.getBoundingClientRect().right})).filter(e=>e.right>innerWidth+1||e.left< -1).slice(0,25)')
                    extra=page.locator('body *').evaluate_all('els=>els.filter(e=>e.clientWidth&&e.scrollWidth>e.clientWidth+1).map(e=>({tag:e.tagName,cls:e.className,scroll:e.scrollWidth,client:e.clientWidth,text:e.textContent.slice(0,150)})).slice(0,30)')
                    raise AssertionError((label,initial,nodes,extra))
            for width in (320,390,768,1440):
                page.set_viewport_size({'width':width,'height':900})
                for path in ('/','/katalog/','/kategorija/laptopovi/'):
                    page.goto(public+path)
                    assert page.locator('.product-card').count()==6
                    assert page.locator('.product-card').first.evaluate('e=>getComputedStyle(e).borderRadius')=='12px'
                    facts=readable(page.locator('.condition-tag'),14)
                    assert len(facts)==6
                    assert page.locator('.product-card').evaluate_all('els=>els.every(e=>e.querySelector(".condition-tag").getBoundingClientRect().top>=e.querySelector(".product-image").getBoundingClientRect().bottom-1)')
                    layout(str(width)+page.url)
                for n in range(1,7):
                    page.goto(public+'/proizvod/'+str(n)+'/')
                    readable(page.locator('.device-condition'),14)
                    assert page.locator('.device-condition').evaluate('e=>e.getBoundingClientRect().top>=document.querySelector(".device-main-image").getBoundingClientRect().bottom-1')
                    layout(str(width)+page.url)
                    if n==4 and width in (390,1440):
                        shot=QA/('grades-product-'+str(width)+'-2026-10-26.png');page.screenshot(path=str(shot),full_page=True);screenshots.append(str(shot))
                page.goto(backend+'/admin/')
                if page.locator('[name=password]').count():
                    page.locator('[name=password]').fill('layout-only');page.get_by_role('button',name='Prijavi se').click()
                page.locator('.condition-choices').wait_for()
                assert page.locator('[name=condition]').count()==6
                assert page.locator('[name=condition]:checked').input_value()=='Refabrikovano - odlično stanje (Grade A)'
                labels=readable(page.locator('.grade-option span'),16)
                assert len(labels)==6
                readable(page.locator('.inventory-condition'),14)
                for radio in page.locator('[name=condition]').all():
                    radio.check();assert radio.is_checked()
                layout(str(width)+page.url)
                if width in (390,1440):
                    page.locator('.condition-choices').screenshot(path=str(QA/('grades-admin-'+str(width)+'-2026-10-26.png')))

            # Keep a cart containing the soon-to-be-hidden item and another visible item.
            page.goto(public+'/korpa/')
            page.evaluate('localStorage.setItem("reklik-cart",JSON.stringify([{id:4,qty:1,warranty:0},{id:1,qty:1,warranty:0}]))')
            page.goto(backend+'/admin/')
            page.locator('form[action="/admin/product/4/visibility"] button').click()
            page.wait_for_url('**/admin/?**')
            export()
            for origin in (backend,public):
                for path in ('/','/katalog/','/kategorija/laptopovi/','/catalog.json'):
                    text=urlopen(origin+path).read().decode()
                    assert 'QA uređaj 4' not in text and '/proizvod/4/' not in text,(origin,path)
                try:urlopen(origin+'/proizvod/4/')
                except HTTPError as e:assert e.code==404
                else:raise AssertionError('Hidden product URL still exists: '+origin)
            assert '/proizvod/4/' not in (state/'public/sitemap.xml').read_text()
            assert not (state/'public/proizvod/4').exists()
            assert not (state/'public/proizvod/qa-grade-4').exists()
            for document in (state/'public').rglob('*.html'):
                assert '/proizvod/4/' not in document.read_text(),document
            page.goto(public+'/korpa/')
            page.locator('.cart-item').wait_for()
            assert page.locator('.cart-item').count()==1
            assert 'QA uređaj 4' not in page.locator('body').inner_text()
            assert page.evaluate('JSON.parse(localStorage.getItem("reklik-cart")).map(x=>x.id)')==[1]
            href=page.locator('[data-cart-checkout]').get_attribute('href')
            from urllib.parse import urlparse,parse_qs
            assert [x['id'] for x in json.loads(parse_qs(urlparse(href).query)['items'][0])]==[1]
            page.goto(backend+'/katalog/?q=QA%20uređaj%204')
            assert page.locator('.product-card').count()==0
            page.goto(backend+'/admin/')
            assert 'Sakriven' in page.locator('.inventory-row').filter(has=page.locator('form[action="/admin/product/4/visibility"]')).inner_text()
            page.locator('form[action="/admin/product/4/visibility"] button').click()
            page.wait_for_url('**/admin/?**')
            export()
            page.goto(public+'/proizvod/4/')
            assert page.locator('h1').inner_text()=='QA uređaj 4'
            page.goto(public+'/katalog/');assert page.locator('.product-card').count()==6
            assert '/proizvod/4/' in (state/'public/sitemap.xml').read_text()
            assert not errors,errors
            browser.close()
        report={'passed':True,'widths':[320,390,768,1440],'all_six_grades_checked':True,'surfaces':['home','catalog','category','product','admin choices','admin list'],
                'hide_show':['removed from backend and exported HTML/JSON','old product URL 404','removed from search and sitemap','removed from old cart and checkout payload','unhide restores page, listing and sitemap'],'production_inventory_modified':False,'js_errors':errors,'screenshots':screenshots}
        (QA/'inventory-layout-2026-10-26.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
    finally:
        for process in processes:process.terminate()
        for process in processes:process.wait(timeout=15)
        log.close()
