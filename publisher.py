"""Export complete crawlable HTML and publish it to GitHub Pages."""
import json
from html import escape as html_escape
import fcntl
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape
import store

PUBLIC = store.ROOT / 'docs'
LOCK = threading.Lock()
SCHEDULE_LOCK = threading.Lock()
scheduled = False
generation = 0


def public_catalog(cfg=None):
    cfg = cfg or store.settings()
    rows = []
    for p in store.products():
        rows.append({key: p[key] for key in ['id','title','brand','category_name','category_slug','condition','images','quantity','catalog_image']} |
                    {'price_minor': p['price_cents'],'slug':str(p['id'])})
    return {'currency': cfg['currency'], 'products': rows,
            'included_warranty_days':cfg['included_warranty_days'],
            'warranty_minor':cfg['warranty_eur_cents']}


def export():
    from app import app
    from flask import render_template
    cfg = store.settings()
    temp = store.ROOT / '.public-build'
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir()
    shutil.copytree(store.ROOT / 'assets', temp / 'assets')
    (temp / 'media').mkdir()
    (temp / 'media/full').mkdir()
    for p in store.products():
        for image in p['images']:
            if (store.UPLOADS / image).is_file():
                shutil.copy2(store.UPLOADS / image, temp / 'media' / image)
                original=store.ORIGINALS/image.replace('.webp','.jpg')
                if original.is_file():shutil.copy2(original,temp/'media/full'/original.name)
    base = urlparse(cfg['site_url']).path.rstrip('/')
    urls = []
    def page(path, template, **context):
        out = temp / path.strip('/') / 'index.html'
        out.parent.mkdir(parents=True, exist_ok=True)
        with app.test_request_context(path):
            html = render_template(template, base=base, live=False, current_path=path, **context)
        out.write_text(html)
        if not context.get('private_page'):
            urls.append(path)
    all_products = store.products()
    page('/', 'home.html', products=all_products, category=None, query='', page_title='Sertifikovano refabrikovani laptopovi, telefoni i tableti')
    page('/katalog/', 'catalog.html', products=all_products, category=None, query='', page_title='Katalog refabrikovanih uređaja')
    for cat in store.categories():
        page('/kategorija/' + cat['slug'] + '/', 'catalog.html', products=store.products(category=cat['slug']), category=cat,
             query='', page_title=cat['name'] + ' — sertifikovano refabrikovano')
    for p in all_products:
        path='/proizvod/'+str(p['id'])+'/'
        page(path,'product.html',product=p,page_title=p['title'])
        if p['slug']!=str(p['id']):
            alias=temp/'proizvod'/p['slug']/'index.html';alias.parent.mkdir(parents=True,exist_ok=True)
            target=html_escape(base+path,quote=True);canonical=html_escape(cfg['canonical_url']+path,quote=True)
            alias.write_text('<!doctype html><html lang="sr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex, follow"><meta http-equiv="refresh" content="0;url='+target+'"><link rel="canonical" href="'+canonical+'"><title>Otvaranje proizvoda — ReKlik.shop</title></head><body><p><a href="'+target+'">Otvori proizvod</a></p></body></html>')
    titles = {'o-nama':'Nova prilika za odličnu tehnologiju','dostava':'Dostava i preuzimanje',
              'garancija':'Kvalitet, prava kupaca i dodatna garancija','uslovi':'Podaci o prodavcu i uslovi kupovine',
              'privatnost':'Privatnost i podaci','povracaj':'Povraćaj i reklamacije','kontakt':'Tu smo za vas'}
    for slug,title in titles.items():
        page('/' + slug + '/', 'information.html', info=slug, page_title=title)
    for path,kind,title in [('/checkout/','checkout','Sigurna kupovina'),('/admin/','admin','Administracija'),('/potvrda/','confirmation','Potvrda porudžbine')]:
        page(path,'frame.html',frame_kind=kind,page_title=title,private_page=True)
    page('/korpa/','cart.html',page_title='Vaša korpa',private_page=True)
    (temp / 'catalog.json').write_text(json.dumps(public_catalog(cfg), ensure_ascii=False, separators=(',',':')))
    endpoint = store.STATE / 'backend.json'
    (temp / 'backend.json').write_text(endpoint.read_text() if endpoint.exists() else '{"backend":null}')
    (temp / '.nojekyll').touch()
    if urlparse(cfg['site_url']).hostname in ('reklik.shop','www.reklik.shop'):
        (temp / 'CNAME').write_text(urlparse(cfg['site_url']).hostname + '\n')
    robots = 'User-agent: *\n'
    if cfg['indexing_enabled']:
        robots += '\n'.join('Disallow: ' + base + x for x in ['/admin/','/checkout/','/korpa/','/potvrda/','/order/','/api/']) + '\n'
        robots += 'Sitemap: ' + cfg['canonical_url'] + '/sitemap.xml\n'
    else:
        robots += 'Disallow: /\n'
    (temp / 'robots.txt').write_text(robots)
    sitemap = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    for path in urls:
        sitemap += '<url><loc>' + escape(cfg['canonical_url'] + path) + '</loc></url>'
    (temp / 'sitemap.xml').write_text(sitemap + '</urlset>\n')
    (temp / '404.html').write_text('<!doctype html><html lang="sr"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex"><title>ReKlik — Stranica nije pronađena</title><h1>Stranica nije pronađena.</h1><p><a href="'+base+'/katalog/">Pogledajte katalog uređaja</a></p></html>')
    backup = store.ROOT / '.public-previous'
    if backup.exists():
        shutil.rmtree(backup)
    if PUBLIC.exists():
        PUBLIC.rename(backup)
    temp.rename(PUBLIC)
    if backup.exists():
        shutil.rmtree(backup)
    return len(urls)


def git(*args, check=True):
    result = subprocess.run(['git', *args], cwd=store.ROOT, capture_output=True, text=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError('Git operacija nije uspela: ' + result.stderr[-700:])
    return result


def publish(push=True):
    # The server and tunnel run in separate processes and may publish together.
    with LOCK, (store.STATE / 'publish.lock').open('a') as process_lock:
        fcntl.flock(process_lock.fileno(),fcntl.LOCK_EX)
        count = export()
        if not (store.ROOT / '.git').exists():
            return {'pages': count, 'pushed': False}
        git('add','--','docs','assets','templates','app.py','store.py','payments.py','publisher.py',
            'ai_editor.py','editor_guard.mjs','specifications.py','specs_guard.mjs','addresses.py','runtime.py','domain.py','notifications.py','rates.py','inventory_rules.py','setup.py','requirements.txt','.gitignore','README.md','tests')
        changed = git('diff','--cached','--quiet',check=False).returncode != 0
        if changed:
            git('commit','-m','Update ReKlik storefront and catalog')
        pushed = False
        if push and git('remote','get-url','origin',check=False).returncode == 0:
            git('-c','credential.helper=!gh auth git-credential','push','origin','main')
            pushed = True
        return {'pages': count, 'pushed': pushed}


def schedule():
    global scheduled,generation
    with SCHEDULE_LOCK:
        generation += 1
        if scheduled:
            return
        scheduled = True
    def run():
        global scheduled
        time.sleep(1)
        while True:
            with SCHEDULE_LOCK:
                observed = generation
            try:
                result = publish()
                store.audit('published', detail=json.dumps(result))
            except Exception as error:
                store.audit('publish_failed', detail=str(error))
            with SCHEDULE_LOCK:
                if observed == generation:
                    scheduled = False
                    break
    threading.Thread(target=run, daemon=True).start()


if __name__ == '__main__':
    print(json.dumps(publish(push='--no-push' not in __import__('sys').argv)))
