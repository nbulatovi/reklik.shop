"""Pinned product actions, warranty/cart behavior and numeric public URLs. No live orders."""
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
with tempfile.TemporaryDirectory(prefix='reklik-purchase-',dir=QA) as directory:
    state=Path(directory);env=dict(os.environ,REKLIK_STATE=directory)
    fixture='''
import json,store,rates,inventory_rules
from PIL import Image
from werkzeug.security import generate_password_hash
from datetime import datetime
from zoneinfo import ZoneInfo
store.set_settings({'site_url':'http://127.0.0.1:4346','eur_rsd':'117.2','eur_rsd_date':datetime.now(ZoneInfo('Europe/Belgrade')).date().isoformat(),'eur_rsd_source':rates.SOURCE})
(store.STATE/'private.json').write_text(json.dumps({'secret':'purchase-qa','password_hash':generate_password_hash('purchase-qa')}))
image='abcdef0123456789abcdef01.webp';Image.new('RGB',(1200,900),'silver').save(store.UPLOADS/image)
specs={'Procesor':'Intel Core i5','RAM':'16 GB','SSD':'512 GB','Ekran':'14 inča, Full HD'}
specs.update({'Specifikacija '+str(i):'Duga specifikacija za proveru prikaza i kupovine prilikom skrolovanja. '*2 for i in range(1,17)})
with store.connect(True) as c:
 c.execute('INSERT INTO products(id,category_id,title,slug,brand,price_cents,base_price_cents,quantity,condition,description,specs,images,active,created_at,updated_at) VALUES(1,1,?,?,?,30000,28500,3,?,?,?,?,1,?,?)',
  ('QA uređaj sa dugim opisom','dell-latitude-5420','Lenovo',inventory_rules.GRADES[1],('Opis sa svim podacima o uređaju. '*12+'\\n')*5,json.dumps(specs),json.dumps([image]),store.now(),store.now()))
import publisher
publisher.PUBLIC=store.STATE/'public';publisher.export()
'''
    subprocess.run([PYTHON,'-c',fixture],cwd=ROOT,env=env,check=True)
    # The old export is a small redirect page; only ID URLs appear in the index.
    sitemap=(state/'public/sitemap.xml').read_text()
    assert '/proizvod/1/' in sitemap and '/proizvod/dell-latitude-5420/' not in sitemap
    alias=(state/'public/proizvod/dell-latitude-5420/index.html').read_text()
    assert 'noindex, follow' in alias and '0;url=/proizvod/1/' in alias
    assert json.loads((state/'public/catalog.json').read_text())['products'][0]['slug']=='1'
    log=open(state/'server.log','w')
    processes=[subprocess.Popen([PYTHON,'-c','import app;from waitress import serve;app.queue_publish=lambda:None;serve(app.app,host="127.0.0.1",port=4346)'],cwd=ROOT,env=env,stdout=log,stderr=log),
               subprocess.Popen([PYTHON,'-m','http.server','4347','--bind','127.0.0.1','--directory',str(state/'public')],cwd=ROOT,env=env,stdout=log,stderr=log)]
    try:
        for port in (4346,4347):
            for attempt in range(80):
                try:
                    if urlopen('http://127.0.0.1:'+str(port)+'/').status==200:break
                except OSError:time.sleep(.1)
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
            cases=[]
            for scripts in (False,True):
                context=browser.new_context(java_script_enabled=scripts)
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                for port in (4346,4347):
                    origin='http://127.0.0.1:'+str(port)
                    page.goto(origin+'/proizvod/dell-latitude-5420/')
                    page.wait_for_url('**/proizvod/1/')
                    assert page.locator('h1').inner_text()=='QA uređaj sa dugim opisom'
                    for width,height in ((320,700),(390,844),(768,1024),(1440,900),(1920,1080),(844,390)):
                        page.set_viewport_size({'width':width,'height':height})
                        page.goto(origin+'/proizvod/1/')
                        for fraction in (0,.25,.5,1,.5,0):
                            page.evaluate('(f)=>{document.documentElement.style.scrollBehavior="auto";scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*f)}',fraction)
                            if scripts:page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                            else:page.wait_for_timeout(50)
                            facts=page.locator('.device-buy-actions .button').evaluate_all('els=>els.map(e=>{const r=e.getBoundingClientRect();return {text:e.textContent.trim(),top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:r.height,sw:e.scrollWidth,cw:e.clientWidth,hit:e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))}})')
                            assert len(facts)==2
                            assert all(b['top']>=0 and b['bottom']<=height+1 and b['left']>=0 and b['right']<=width+1 and b['height']>=44 and b['sw']<=b['cw']+1 and b['hit'] for b in facts),(scripts,port,width,height,fraction,facts)
                            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                            if fraction==1:
                                assert page.locator('.footer-admin').evaluate('e=>e.getBoundingClientRect().bottom<=document.querySelector(".device-buy-actions").getBoundingClientRect().top'),(width,height)
                        if scripts and port==4347 and width in (390,1440):page.screenshot(path=str(QA/('persistent-purchase-'+str(width)+'-2026-10-26.png')))
                        cases.append({'javascript':scripts,'port':port,'viewport':[width,height],'scroll_checks':6})
                    page.locator('.warranty-options summary').click();page.locator('#warranty').select_option('2')
                    page.evaluate('document.documentElement.style.scrollBehavior="auto";scrollTo(0,document.documentElement.scrollHeight)')
                    if scripts:
                        page.get_by_role('button',name='Dodaj u korpu').click()
                        assert page.locator('[data-cart-feedback]').inner_text()=='Dodato u korpu.'
                        assert page.locator('[data-cart-feedback]').evaluate('e=>e.getBoundingClientRect().top>=0&&e.getBoundingClientRect().bottom<=innerHeight')
                        assert page.evaluate('JSON.parse(localStorage.getItem("reklik-cart"))')==[{'id':1,'warranty':2,'qty':1}]
                        page.goto(origin+'/korpa/')
                        assert page.locator('.cart-item h3 a').get_attribute('href')=='/proizvod/1/'
                        page.goto(origin+'/proizvod/1/');page.locator('.warranty-options summary').click();page.locator('#warranty').select_option('2')
                    page.evaluate('document.documentElement.style.scrollBehavior="auto";scrollTo(0,document.documentElement.scrollHeight)')
                    page.get_by_role('button',name='Kupi sada').click()
                    page.wait_for_url('**/checkout/?buy=1&warranty=2')
                    # Navigation only: no order or payment is submitted.
                assert not errors,errors
                context.close()
            browser.close()
        report={'passed':True,'cases':cases,'checks':['visible and clickable at top, middle and footer in both scroll directions','native buy-now submits selected warranty','add-to-cart feedback visible with correct item and warranty','cart uses numeric item link','legacy redirects and numeric SEO URLs'],'production_orders_submitted':0}
        path=QA/'persistent-purchase-2026-10-26.json';path.write_text(json.dumps(report,indent=2));path.chmod(0o600);print(json.dumps(report))
    finally:
        for process in processes:process.terminate()
        for process in processes:process.wait(timeout=15)
        log.close()
