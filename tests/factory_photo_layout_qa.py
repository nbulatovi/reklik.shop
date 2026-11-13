"""Desktop/mobile factory-photo controls, zoom and removal in isolated inventory."""
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
QA.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='reklik-factory-layout-',dir=QA) as temporary:
    state=Path(temporary);env=dict(os.environ,REKLIK_STATE=temporary)
    fixture='''
import json,store,inventory_rules,factory_photos
from PIL import Image
from werkzeug.security import generate_password_hash
(store.STATE/'private.json').write_text(json.dumps({'secret':'factory-layout-only','password_hash':generate_password_hash('factory-layout-only')}))
real='a'*24+'.webp';factory='b'*24+'.webp'
for name,color in ((real,'orange'),(factory,'white')):
 Image.new('RGB',(1600,1200),color).save(store.UPLOADS/name,'WEBP')
 Image.new('RGB',(3200,2400),color).save(store.ORIGINALS/name.replace('.webp','.jpg'),'JPEG')
with store.connect(True) as c:
 c.execute("INSERT INTO products(id,category_id,title,slug,brand,price_cents,base_price_cents,quantity,condition,description,specs,images,active,created_at,updated_at,factory_photo) VALUES(1,1,'Dell Latitude 5590','qa-factory','Dell',30000,28500,1,?,?,?,?,1,?,?,?)",(inventory_rules.GRADES[3],'Ručno unet opis. '+('Dodatni detalji uređaja. '*75),json.dumps({'Procesor':'Intel Core i5','RAM':'16 GB','SSD':'512 GB'}),json.dumps([factory,real]),store.now(),store.now(),factory))
 p=store.unpack(c.execute('SELECT * FROM products WHERE id=1').fetchone())
 c.execute('UPDATE products SET factory_photo_identity=? WHERE id=1',(factory_photos.identity(p),))
'''
    subprocess.run([PYTHON,'-c',fixture],cwd=ROOT,env=env,check=True)
    log=open(state/'server.log','w')
    process=subprocess.Popen([PYTHON,'-c','import app,factory_photos;from waitress import serve;app.queue_publish=lambda:None;factory_photos.WORKERS.submit=lambda *a,**k:None;serve(app.app,host="127.0.0.1",port=4347)'],cwd=ROOT,env=env,stdout=log,stderr=log)
    try:
        origin='http://127.0.0.1:4347'
        for _ in range(80):
            try:
                if urlopen(origin+'/health',timeout=1).status==200:break
            except OSError:time.sleep(.1)
        checks=[];errors=[]
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path='/usr/bin/google-chrome',headless=True,args=['--no-sandbox'])
            for width in (320,390,768,1440):
                for javascript in (True,False):
                    context=browser.new_context(viewport={'width':width,'height':900},java_script_enabled=javascript)
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(origin+'/proizvod/1/')
                    assert page.locator('[data-gallery-image]').get_attribute('src').endswith('/'+'b'*24+'.webp')
                    assert page.locator('[data-gallery-kind]').inner_text()=='Fotografija proizvođača'
                    assert page.locator('[data-open-zoom]').get_attribute('href').endswith('/full/'+'b'*24+'.jpg')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),('product overflow',width)
                    assert page.locator('.device-condition').evaluate('e=>e.scrollHeight<=e.clientHeight+1')
                    for y in (0,600,10000):
                        page.evaluate('(y)=>scrollTo(0,y)',y)
                        for label in ('Kupi sada','Dodaj u korpu'):
                            button=page.get_by_role('button',name=label)
                            rect=button.bounding_box();assert rect and rect['y']>=0 and rect['y']+rect['height']<=901,(width,label,rect)
                    if javascript:
                        page.evaluate('scrollTo(0,0)');page.locator('[data-gallery-next]').click()
                        assert page.locator('[data-gallery-kind]').inner_text()=='Fotografija uređaja'
                        page.locator('[data-gallery-prev]').click()
                        page.locator('[data-open-zoom]').click()
                        assert page.locator('[data-photo-zoom]').evaluate('e=>e.open')
                        page.locator('[data-zoom-image]').wait_for(state='visible')
                        page.wait_for_function('document.querySelector("[data-zoom-image]").naturalWidth===3200')
                        page.locator('[data-close-zoom]').click()
                    page.goto(origin+'/admin/login/')
                    page.locator('[name=password]').fill('factory-layout-only');page.get_by_role('button',name='Prijavi se').click()
                    page.goto(origin+'/admin/?edit=1')
                    assert page.locator('[name=factory_photo_enabled]').is_checked()
                    assert page.get_by_role('button',name='Obriši fabričku fotografiju i sačuvaj uređaj').is_visible()
                    assert page.locator('[data-factory-photo]').count()==1
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),('admin overflow',width)
                    if width in (390,1440) and javascript:
                        page.locator('.inventory-factory-choice').screenshot(path=str(QA/f'factory-photo-admin-{width}-20261113.png'))
                    checks.append({'width':width,'javascript':javascript,'passed':True})
                    context.close()
            # Exercise the native no-JavaScript delete button, then a second save.
            context=browser.new_context(viewport={'width':390,'height':900},java_script_enabled=False)
            page=context.new_page();page.goto(origin+'/admin/login/')
            page.locator('[name=password]').fill('factory-layout-only');page.get_by_role('button',name='Prijavi se').click()
            page.goto(origin+'/admin/?edit=1');page.get_by_role('button',name='Obriši fabričku fotografiju i sačuvaj uređaj').click()
            page.wait_for_url('**/admin/?**');assert not page.locator('.notice-error').count()
            page.goto(origin+'/admin/?edit=1');assert not page.locator('[name=factory_photo_enabled]').is_checked()
            assert page.locator('[data-factory-photo]').count()==0
            page.locator('[data-inventory-save]').click();page.wait_for_url('**/admin/?**')
            page.goto(origin+'/proizvod/1/')
            assert page.locator('[data-gallery-image]').get_attribute('src').endswith('/'+'a'*24+'.webp')
            assert page.locator('[data-gallery-kind]').is_hidden()
            assert 'Ručno unet opis' in page.locator('.device-description').inner_text()
            assert page.locator('.device-key-specs').get_by_text('16 GB',exact=True).count()==1
            assert not errors,errors
            context.close();browser.close()
        report={'passed':True,'cases':checks,'deletion_without_javascript':True,'full_resolution_zoom':True,
                'manual_photos_specs_description_preserved':True,'sticky_buy_buttons':True,'js_errors':errors,'production_modified':False}
        (QA/'factory-photo-layout-20261113.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False))
    finally:
        process.terminate();process.wait(timeout=15);log.close()
