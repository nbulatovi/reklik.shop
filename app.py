import csv
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import pycountry
from flask import Flask, Response, abort, g, jsonify, make_response, redirect, render_template, request, send_from_directory, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
import store
import payments

app = Flask(__name__, static_folder='assets', static_url_path='/assets')
app.json.sort_keys = False
register_heif_opener()
app.config.update(MAX_CONTENT_LENGTH=35*1024*1024, TEMPLATES_AUTO_RELOAD=True)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)
if os.environ.get('REKLIK_SECURE_COOKIES') == '1':
    # The loopback service is exposed exclusively through our HTTPS tunnel.
    # cloudflared does not consistently forward the external URL scheme.
    proxy_application = app.wsgi_app
    def https_tunnel(environ, start_response):
        environ['wsgi.url_scheme'] = 'https'
        environ['HTTP_X_FORWARDED_PROTO'] = 'https'
        return proxy_application(environ,start_response)
    app.wsgi_app = https_tunnel
CONFIG = store.STATE / 'private.json'
if not CONFIG.exists():
    raise RuntimeError('Run setup.py first to initialize the private admin credentials.')
PRIVATE = json.loads(CONFIG.read_text())
login_attempts = defaultdict(list)
attempt_lock = threading.Lock()
countries = sorted([(c.alpha_2, c.name) for c in pycountry.countries], key=lambda c: c[1])
country_codes = {code for code, name in countries}

app.jinja_env.filters['truncate_specs'] = lambda values: ' · '.join(str(v) for v in values[:3])
app.jinja_env.filters['image_urls'] = lambda values, origin: [origin + '/media/' + x for x in values]
app.jinja_env.filters['asset_urls'] = lambda values, origin: [origin + '/assets/' + x for x in values]
app.jinja_env.filters['converted_price'] = lambda cents, cfg: f'{cents / 100:.2f}'
app.jinja_env.filters['order_money'] = store.display_money


def queue_publish():
    import publisher
    publisher.schedule()


@app.before_request
def load_session():
    g.session = None
    if request.path.startswith(('/assets/', '/media/', '/health', '/webhooks/', '/views/')):
        return
    sid = request.cookies.get('rk_session', '')
    with store.connect(True) as c:
        row = c.execute('SELECT * FROM sessions WHERE id=? AND expires_at>?', (sid, store.now())).fetchone()
        if not row:
            sid = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            c.execute('INSERT INTO sessions VALUES(?,?,0,?,?)', (sid, csrf, store.now(), store.now()+43200))
            row = c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        g.session = dict(row)
    if request.method == 'POST':
        token = request.form.get('csrf') or request.headers.get('X-CSRF-Token', '')
        if not token or not hmac.compare_digest(g.session['csrf'], token):
            abort(403, 'Sesija je istekla. Osvežite stranicu i pokušajte ponovo.')
        origin = request.headers.get('Origin')
        if origin and origin != request.host_url.rstrip('/'):
            abort(403, 'Neispravan izvor zahteva.')


@app.after_request
def response_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Permissions-Policy'] = 'camera=(self), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-src https://whop.com https://*.whop.com; frame-ancestors 'self' https://nbulatovi.github.io https://reklik.shop https://www.reklik.shop; object-src 'none'; base-uri 'self'; form-action 'self' https://whop.com https://nbulatovi.github.io https://reklik.shop https://www.reklik.shop"
    if request.path.startswith(('/admin', '/checkout', '/order', '/api/', '/views/')):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    if getattr(g, 'session', None):
        secure = request.is_secure or os.environ.get('REKLIK_SECURE_COOKIES') == '1'
        response.set_cookie('rk_session', g.session['id'], max_age=43200, httponly=True,
                            secure=secure, samesite='None' if secure else 'Lax', partitioned=secure)
    return response


def admin_only(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not g.session or not g.session['admin']:
            return redirect('/admin/login/')
        return fn(*args, **kwargs)
    return wrapped


@app.context_processor
def template_context():
    cfg = store.settings()
    return {'cfg': cfg, 'categories': store.categories(), 'base': '', 'live': True,
            'asset_version': str(max((store.ROOT/'assets/style.css').stat().st_mtime_ns,(store.ROOT/'assets/site.js').stat().st_mtime_ns)),
            'csrf': g.session['csrf'] if getattr(g, 'session', None) else '',
            'money': lambda cents: store.display_money(cents,'EUR'),
            'minor_money': lambda cents: store.display_money(cents,'RSD'),
            'pricing_explanation': pricing_explanation,
            'warranty_money': lambda months: store.display_money(store.rsd_to_minor(cfg['warranty_rsd'][str(months)], cfg), cfg['currency']),
            'current_path': request.path, 'year': time.strftime('%Y'), 'admin': bool(getattr(g, 'session', None) and g.session['admin'])}


def pricing_explanation(pricing):
    return ('Približno '+store.display_money(pricing['total_eur_cents'],'EUR')+
            '. Srednji kurs NBS: 1 EUR = '+pricing['rate'].replace('.',',')+
            ' RSD; kursna lista '+datetime_date(pricing['date'])+'. Plaćanje je u dinarima.')


def datetime_date(value):
    return '.'.join(reversed(value.split('-')))


@app.route('/health')
def health():
    return jsonify(ok=True, service='reklik', inventory_count=len(store.products()))


@app.route('/')
def home():
    return render_template('home.html', products=store.products(), category=None, query='', page_title='Sertifikovano obnovljeni laptopovi, telefoni i tableti')


@app.route('/katalog/')
def catalog():
    query = request.args.get('q', '')[:100]
    return render_template('catalog.html', products=store.products(query=query), category=None, query=query, page_title='Katalog obnovljenih uređaja')


@app.route('/catalog.json')
def catalog_data():
    import publisher
    return jsonify(publisher.public_catalog())


@app.route('/korpa/')
def cart_page():
    return render_template('cart.html', page_title='Vaša korpa', private_page=True)


@app.route('/kategorija/<slug>/')
def category(slug):
    cat = next((c for c in store.categories() if c['slug'] == slug), None)
    if not cat:
        abort(404)
    return render_template('catalog.html', products=store.products(category=slug), category=cat, query='', page_title=cat['name'] + ' — sertifikovano obnovljeno')


@app.route('/proizvod/<slug>/')
def product(slug):
    p = store.product(slug)
    if not p:
        abort(404)
    return render_template('product.html', product=p, page_title=p['title'])


@app.route('/views/product/<int:pid>')
def count_item_view(pid):
    # Called only when an item detail page opens; no cookie or raw-IP record.
    p = store.product(pid)
    if not p:
        return '',404
    if re.search(r'bot|spider|crawler|preview',request.headers.get('User-Agent',''),re.I):
        return '',204
    raw_ip = request.headers.get('CF-Connecting-IPv6') or request.headers.get('CF-Connecting-IP') or request.remote_addr
    try:
        address = ipaddress.ip_address(raw_ip)
        if isinstance(address,ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
    except ValueError:
        return '',204
    visitor = hmac.new(PRIVATE['secret'].encode(),('item-view:'+str(pid)+':'+str(address)).encode(),hashlib.sha256).hexdigest()
    with store.connect(True) as c:
        c.execute('INSERT OR IGNORE INTO item_views(product_id,visitor_hash,first_seen) VALUES(?,?,?)',(pid,visitor,store.now()))
    return '',204


@app.route('/<page>/')
def information(page):
    titles = {'o-nama': 'Nova prilika za odličnu tehnologiju', 'dostava': 'Dostava i preuzimanje',
              'garancija': 'Kvalitet, prava kupaca i dodatna garancija', 'uslovi': 'Podaci o prodavcu i uslovi kupovine',
              'privatnost': 'Privatnost i podaci', 'povracaj': 'Povraćaj i reklamacije', 'kontakt': 'Tu smo za vas'}
    if page not in titles:
        abort(404)
    return render_template('information.html', info=page, page_title=titles[page])


@app.route('/media/<name>')
def media(name):
    if not re.fullmatch(r'[a-f0-9]{24}\.webp', name):
        abort(404)
    return send_from_directory(store.UPLOADS, name, max_age=86400)


def decode_cart():
    if request.args.get('buy'):
        return store.normalize_items([{'id': request.args['buy'], 'qty': request.args.get('qty', 1), 'warranty': request.args.get('warranty', 0)}])
    return store.normalize_items(json.loads(request.args.get('items') or request.form.get('items') or '[]'))


@app.route('/checkout/', methods=['GET', 'POST'])
def checkout():
    cfg = store.settings()
    error = None
    q = None
    raw = []
    values = request.form.to_dict() if request.method == 'POST' else {}
    try:
        raw = decode_cart()
        if request.method == 'POST':
            prior_idem = values.get('idem','')
            signature = hmac.new(PRIVATE['secret'].encode(),(g.session['id']+prior_idem).encode(),hashlib.sha256).hexdigest()
            if re.fullmatch(r'[a-f0-9]{64}',prior_idem) and hmac.compare_digest(signature,values.get('idem_signature','')):
                with store.connect() as c:
                    prior = c.execute('SELECT * FROM orders WHERE idem=?',(prior_idem,)).fetchone()
                if prior:
                    if prior['status'] == 'awaiting_payment' and prior['checkout_url']:
                        return redirect(prior['checkout_url'],code=303)
                    return redirect('/order/'+prior['token']+'/',code=303)
        country = values.get('country', 'RS').upper()
        if country not in country_codes:
            raise ValueError('Izaberite važeću zemlju isporuke.')
        method = values.get('delivery_method', 'shipping')
        available = [c for c in cfg['couriers'] if c['enabled']]
        courier = values.get('courier') or (available[0]['id'] if available else '')
        q = store.quote(raw, country, method, courier, cfg)
        if request.method == 'POST':
            customer = {}
            for key, label, maxlen in [('name', 'Ime i prezime', 120), ('phone', 'Telefon', 40), ('email', 'Email', 180)]:
                value = values.get(key, '').strip()
                if not value or len(value) > maxlen:
                    raise ValueError('Popunite polje: ' + label + '.')
                customer[key] = value
            if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', customer['email']):
                raise ValueError('Unesite ispravnu email adresu.')
            if not re.fullmatch(r'[+\d\s()/-]{6,40}', customer['phone']):
                raise ValueError('Unesite ispravan broj telefona.')
            delivery = {'country': country, 'method': method, 'courier': courier}
            for key in ['address', 'city', 'postal_code']:
                delivery[key] = values.get(key, '').strip()[:180]
                if method != 'pickup' and key == 'city' and len(delivery[key]) < 2:
                    raise ValueError('Unesite grad, mesto ili selo za dostavu.')
            if country == 'RS' and delivery['postal_code'] and not re.fullmatch(r'\d{5}', delivery['postal_code']):
                raise ValueError('Poštanski broj u Srbiji ima pet cifara. Ako ga ne znate, ostavite polje prazno; proverićemo ga telefonom.')
            delivery['address_confirmation'] = 'phone_required'
            customer['note'] = values.get('note', '').strip()[:1000]
            if values.get('website'):
                abort(400)
            if values.get('terms') != 'yes':
                raise ValueError('Potvrdite da prihvatate uslove kupovine.')
            if values.get('quoted_currency') != q['currency'] or int(values.get('quoted_total', -1)) != q['total_minor']:
                raise ValueError('Iznos je ažuriran. Proverite novi sažetak porudžbine i potvrdite ponovo.')
            idem = values.get('idem', '')
            if not re.fullmatch(r'[a-f0-9]{64}', idem):
                raise ValueError('Osvežite stranicu i pokušajte ponovo.')
            signature = values.get('idem_signature', '')
            expected = hmac.new(PRIVATE['secret'].encode(), (g.session['id'] + idem).encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                abort(403)
            order = store.create_order(raw, customer, delivery, values.get('payment_method', 'cod'), idem,
                                       expected=(q['currency'],q['total_minor']))
            import notifications
            notifications.WAKE.set()
            queue_publish()
            if order['payment_method'] == 'card' and not order.get('checkout_url'):
                try:
                    purchase_url = payments.create_checkout(order, request.host_url)
                    return redirect(purchase_url, code=303)
                except ValueError as e:
                    store.audit('checkout_failed', order['id'], str(e))
                    return redirect(store.settings()['site_url'] + '/potvrda/?order=' + order['token'], code=303)
            elif order['payment_method'] == 'card' and order.get('checkout_url'):
                return redirect(order['checkout_url'], code=303)
            queue_publish()
            return redirect('/order/' + order['token'] + '/')
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        error = str(e) if isinstance(e, ValueError) else 'Porudžbina nije ispravna. Pokušajte ponovo.'
    idem = values.get('idem') or secrets.token_hex(32)
    signature = hmac.new(PRIVATE['secret'].encode(), (g.session['id'] + idem).encode(), hashlib.sha256).hexdigest()
    return render_template('checkout.html', quote=q, items=raw, error=error, values=values, countries=countries,
                           idem=idem, idem_signature=signature, page_title='Završite kupovinu', private_page=True), 422 if error else 200


@app.route('/api/quote', methods=['POST'])
def shipping_quote():
    try:
        data = request.get_json()
        country = str(data.get('country', 'RS')).upper()
        if country not in country_codes:
            raise ValueError('Nepoznata zemlja.')
        q = store.quote(data['items'], country, data.get('method', 'shipping'), data.get('courier', 'post-express'))
        q['formatted'] = {key: store.display_money(q[key], q['currency']) for key in ['subtotal_minor', 'warranty_minor', 'shipping_minor', 'total_minor']}
        q['pricing_explanation']=pricing_explanation(q['pricing'])
        return jsonify(q)
    except (ValueError, KeyError, TypeError) as e:
        return jsonify(error=str(e)), 422


@app.route('/api/address-suggestion', methods=['POST'])
def address_suggestion():
    import addresses
    data = request.get_json(silent=True) or {}
    return jsonify(suggestion=addresses.suggest(str(data.get('address','')),str(data.get('city','')),str(data.get('country','RS'))))


@app.route('/api/session')
def session_ready():
    # Safari and some privacy modes block embedded cookies. Detect this before
    # a buyer fills a form, then offer the same page in a first-party window.
    return jsonify(ready=hmac.compare_digest(request.cookies.get('rk_session',''),g.session['id']))


@app.route('/order/<token>/')
def order_detail(token):
    order = store.order_by_token(token)
    if not order:
        abort(404)
    if request.args.get('return'):
        try:
            payments.reconcile(order)
            order = store.order_by_token(token)
        except ValueError:
            pass
    return render_template('order.html', order=order, page_title='Porudžbina ' + order['id'], private_page=True)


@app.route('/order/<token>/check', methods=['POST'])
def order_check(token):
    order = store.order_by_token(token)
    if not order:
        abort(404)
    try:
        payments.reconcile(order)
    except ValueError:
        pass
    return redirect('/order/' + token + '/')


@app.route('/order/<token>/retry', methods=['POST'])
def order_retry(token):
    order = store.order_by_token(token)
    if not order or order['status'] != 'awaiting_payment':
        abort(409)
    if not order.get('checkout_url'):
        try:
            payments.create_checkout(order, request.host_url)
        except ValueError:
            pass
    return redirect('/order/' + token + '/')


@app.route('/order/<token>/receipt')
def receipt(token):
    order = store.order_by_token(token)
    if not order:
        abort(404)
    response = make_response(render_template('receipt.html', order=order))
    response.headers['Content-Disposition'] = 'attachment; filename="' + order['id'] + '.html"'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Robots-Tag'] = 'noindex'
    return response


@app.route('/webhooks/whop', methods=['POST'])
def whop_webhook():
    body = request.get_data()
    if not payments.verify_webhook(body, request.headers):
        abort(401)
    try:
        event = json.loads(body)
        data = event.get('data') or {}
        pid = data.get('id')
        if event.get('type') == 'payment.succeeded' and pid:
            payment = payments.api('GET', 'payments/' + pid)
            plan = payment.get('plan') or {}
            plan_id = plan.get('id') if isinstance(plan, dict) else plan
            with store.connect() as c:
                row = c.execute('SELECT * FROM orders WHERE plan_id=?', (plan_id,)).fetchone()
            if row:
                payments.verify_payment(store.unpack(row), payment)
                queue_publish()
    except (ValueError, KeyError):
        return jsonify(ok=False), 503
    return jsonify(ok=True)


@app.route('/admin/login/', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        ip = request.headers.get('CF-Connecting-IP') or request.remote_addr
        with attempt_lock:
            login_attempts[ip] = [t for t in login_attempts[ip] if t > store.now()-900]
            limited = len(login_attempts[ip]) >= 5
        if limited:
            error = 'Previše pokušaja. Pokušajte ponovo za 15 minuta.'
        elif check_password_hash(PRIVATE['password_hash'], request.form.get('password', '')):
            with store.connect(True) as c:
                c.execute('DELETE FROM sessions WHERE id=?', (g.session['id'],))
                sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                c.execute('INSERT INTO sessions VALUES(?,?,1,?,?)', (sid, csrf, store.now(), store.now()+43200))
                g.session = dict(c.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone())
            store.audit('admin_login')
            return redirect('/admin/')
        else:
            with attempt_lock:
                login_attempts[ip].append(store.now())
            error = 'Lozinka nije ispravna.'
    return render_template('login.html', error=error, page_title='Administracija', private_page=True), 429 if error and 'Previše' in error else 200


@app.route('/admin/logout', methods=['POST'])
@admin_only
def logout():
    with store.connect(True) as c:
        c.execute('UPDATE sessions SET admin=0 WHERE id=?', (g.session['id'],))
    g.session['admin'] = 0
    return redirect('/admin/login/')


@app.route('/admin/')
@admin_only
def admin_dashboard():
    tab = request.args.get('tab', 'inventory')
    selected = store.product(request.args.get('edit', 0), admin=True)
    if request.args.get('copy'):
        selected = store.product(request.args['copy'], admin=True)
        if selected:
            selected = dict(selected, id='', slug='', quantity=1, images=[])
    with store.connect() as c:
        orders = [store.unpack(r) for r in c.execute("SELECT o.*, (SELECT status FROM order_notifications n WHERE n.order_id=o.id AND n.event='new_order') email_status, (SELECT recipient FROM order_notifications n WHERE n.order_id=o.id AND n.event='new_order') email_recipient FROM orders o ORDER BY o.created_at DESC LIMIT 100")]
        jobs = [dict(r) for r in c.execute("SELECT * FROM jobs WHERE kind!='specifications' ORDER BY created_at DESC LIMIT 5")]
    return render_template('admin.html', products=store.products(admin=True), selected=selected, orders=orders,
                           jobs=jobs, tab=tab, message=request.args.get('message', ''), error=request.args.get('error', ''),
                           page_title='ReKlik administracija', private_page=True)


def back_admin(tab='inventory', message='', error=''):
    return redirect(url_for('admin_dashboard', tab=tab, message=message, error=error))


@app.route('/admin/product', methods=['POST'])
@admin_only
def save_product():
    try:
        f = request.form
        title, desc = f.get('title', '').strip()[:160], f.get('description', '').strip()[:12000]
        if len(title) < 3:
            raise ValueError('Unesite naziv uređaja.')
        category_id, quantity = int(f['category_id']), int(f.get('quantity', 1))
        cfg = store.settings()
        value = store.Decimal(f['price'].replace(',', '.'))
        base_cents = int((value * 100).quantize(store.Decimal('1'), rounding=store.ROUND_HALF_UP))
        if not 1 <= base_cents <= 100000000 or not 0 <= quantity <= 10000:
            raise ValueError('Proverite cenu i količinu.')
        cents=store.list_price(base_cents)
        images = [i for i in f.getlist('keep_images') if re.fullmatch(r'[a-f0-9]{24}\.webp', i) and (store.UPLOADS / i).is_file()]
        for upload in request.files.getlist('photos'):
            if not upload.filename:
                continue
            if len(images) >= 10:
                raise ValueError('Najviše 10 fotografija po uređaju.')
            try:
                im = Image.open(upload.stream)
                if im.width * im.height > 50000000:
                    raise ValueError('Fotografija je prevelika.')
                im = ImageOps.exif_transpose(im).convert('RGB')
                im.thumbnail((1600, 1600))
                filename = secrets.token_hex(12) + '.webp'
                im.save(store.UPLOADS / filename, 'WEBP', quality=87, method=4)
                images.append(filename)
            except (UnidentifiedImageError, OSError):
                raise ValueError('Fotografija nije ispravna. Koristite JPG, PNG ili WebP.')
        specs = {}
        for line in f.get('specs', '').splitlines()[:30]:
            if ':' in line:
                key, value = line.split(':', 1)
                if key.strip() and value.strip():
                    specs[key.strip()[:60]] = value.strip()[:300]
        weight = float(f.get('weight_kg') or 0)
        if not 0 <= weight <= 100:
            raise ValueError('Težina mora biti između 0 i 100 kg.')
        active = int(f.get('active') == 'yes')
        pid = int(f.get('id') or 0)
        existing = store.product(pid,admin=True) if pid else None
        if active and not images and not (existing and existing['catalog_image']):
            raise ValueError('Dodajte bar jednu stvarnu fotografiju pre objavljivanja uređaja.')
        pid = int(f.get('id') or 0)
        with store.connect(True) as c:
            if not c.execute('SELECT 1 FROM categories WHERE id=?', (category_id,)).fetchone():
                raise ValueError('Izaberite kategoriju.')
            values = (category_id, title, f.get('brand', '').strip()[:80], cents, quantity,
                      f.get('condition', 'Refurbished — odlično').strip()[:120], desc,
                      json.dumps(specs, ensure_ascii=False), json.dumps(images), weight, active, store.now())
            if pid:
                current = c.execute('SELECT revision FROM products WHERE id=?', (pid,)).fetchone()
                if not current:
                    raise ValueError('Uređaj nije pronađen.')
                if int(f.get('revision', 0)) != current['revision']:
                    raise ValueError('Inventar se promenio dok ste uređivali uređaj (moguća nova porudžbina). Otvorite uređaj ponovo i proverite količinu.')
                c.execute('''UPDATE products SET category_id=?,title=?,brand=?,price_cents=?,quantity=?,condition=?,
                  description=?,specs=?,images=?,weight_kg=?,active=?,updated_at=?,revision=revision+1 WHERE id=?''', values + (pid,))
                task=c.execute("SELECT fields FROM product_ai WHERE product_id=? AND status='pending'",(pid,)).fetchone()
                if task:
                    fields=[name for name in json.loads(task['fields']) if f.get('auto_'+name)!='0']
                    c.execute('UPDATE product_ai SET fields=? WHERE product_id=?',(json.dumps(fields),pid))
            else:
                slug = store.slugify(title) + '-' + secrets.token_hex(2)
                c.execute('''INSERT INTO products(category_id,title,brand,price_cents,quantity,condition,description,
                  specs,images,weight_kg,active,updated_at,created_at,slug) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  values + (store.now(), slug))
                pid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute('UPDATE products SET base_price_cents=? WHERE id=?',(base_cents,pid))
        store.audit('product_saved', pid)
        import specifications
        identity_changed=not existing or existing['title']!=title or existing['brand']!=f.get('brand','').strip()[:80]
        if identity_changed or f.get('lookup_requested')=='1':
            fields=[name for name in ('specs','description','category_id') if f.get('auto_'+name,'1' if name=='category_id' or not (specs if name=='specs' else desc) else '0')=='1']
            specifications.queue_product(pid,fields)
        queue_publish()
        return back_admin(message='Uređaj je sačuvan. Možete odmah uneti sledeći; AI dopunjava podatke u pozadini.' if identity_changed or f.get('lookup_requested')=='1' else 'Uređaj je sačuvan. Javne stranice se automatski objavljuju.')
    except (ValueError, KeyError, store.sqlite3.IntegrityError, ArithmeticError) as e:
        return back_admin(error=str(e))


@app.route('/admin/category', methods=['POST'])
@admin_only
def save_category():
    try:
        f = request.form
        name = f.get('name', '').strip()[:70]
        if len(name) < 2:
            raise ValueError('Unesite naziv kategorije.')
        fee, weight, multiplier = int(f.get('shipping_rsd', 600)), float(f.get('weight_kg', 5)), float(f.get('international_multiplier', 1))
        if not 0 <= fee <= 100000 or not .01 <= weight <= 100 or not .1 <= multiplier <= 10:
            raise ValueError('Proverite cenu dostave i težinu.')
        cid = int(f.get('id') or 0)
        overrides = {}
        for courier in store.settings()['couriers']:
            raw = f.get('shipping_' + courier['id'], '').strip()
            if raw:
                fee_override = int(raw)
                if not 0 <= fee_override <= 100000:
                    raise ValueError('Proverite kategoričnu poštarinu.')
                overrides[courier['id']] = fee_override
        values = (name, f.get('description', '').strip()[:1000], f.get('icon', 'laptop'), fee, weight, multiplier)
        with store.connect(True) as c:
            if cid:
                c.execute('UPDATE categories SET name=?,description=?,icon=?,shipping_rsd=?,weight_kg=?,international_multiplier=? WHERE id=?', values + (cid,))
            else:
                c.execute('INSERT INTO categories(name,description,icon,shipping_rsd,weight_kg,international_multiplier,slug) VALUES(?,?,?,?,?,?,?)', values + (store.slugify(name),))
                cid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
            c.execute('UPDATE categories SET shipping_overrides=? WHERE id=?', (json.dumps(overrides),cid))
        queue_publish()
        return back_admin('categories', message='Kategorija je sačuvana.')
    except (ValueError, KeyError, store.sqlite3.IntegrityError) as e:
        return back_admin('categories', error=str(e))


@app.route('/admin/settings', methods=['POST'])
@admin_only
def save_settings():
    try:
        f = request.form
        cfg = store.settings()
        couriers = []
        for courier in cfg['couriers']:
            cid = courier['id']
            mult = float(f.get('courier_multiplier_' + cid, 1))
            if not .1 <= mult <= 10:
                raise ValueError('Proverite koeficijent dostave.')
            couriers.append(dict(courier, name=f.get('courier_name_' + cid, courier['name'])[:70],
                                 enabled=f.get('courier_' + cid) == 'yes', multiplier=mult))
            new_rates = []
            for limit, price in courier['rates']:
                amount = float(store.Decimal(str(f.get('tariff_' + cid + '_' + str(limit), price))))
                if not 0 <= amount <= 100000:
                    raise ValueError('Proverite težinski cenovnik kurira.')
                new_rates.append([limit, amount])
            couriers[-1]['rates'] = new_rates
        if not any(c['enabled'] for c in couriers):
            raise ValueError('Ostavite bar jednu kurirsku službu uključenu.')
        warranty = {str(n): int(f.get('warranty_' + str(n), n*600)) for n in (1, 2, 3)}
        if any(v < 0 or v > 1000000 for v in warranty.values()):
            raise ValueError('Proverite cenu dodatne garancije.')
        site_url = f.get('site_url', cfg['site_url']).rstrip('/')
        parsed = urlparse(site_url)
        if parsed.scheme != 'https' or parsed.hostname not in ('reklik.shop', 'www.reklik.shop', 'nbulatovi.github.io'):
            raise ValueError('Javni URL mora biti HTTPS adresa ReKlik prodavnice.')
        rates = {zone: int(f.get('rate_' + zone, value)) for zone, value in cfg['international_rates_rsd'].items()}
        if any(value <= 0 or value > 1000000 for value in rates.values()):
            raise ValueError('Proverite međunarodnu poštarinu.')
        email=f.get('email',cfg['email']).strip()
        if len(email)>180 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
            raise ValueError('Unesite ispravan kontakt email za obaveštenja o porudžbinama.')
        store.set_settings({'brand': f.get('brand', cfg['brand']).strip()[:60], 'email':email,
          'pickup_enabled': f.get('pickup_enabled') == 'yes', 'international_enabled': f.get('international_enabled') == 'yes',
          'payment_enabled': f.get('payment_enabled') == 'yes', 'indexing_enabled': f.get('indexing_enabled') == 'yes',
          'warranty_rsd': warranty, 'couriers': couriers, 'international_rates_rsd': rates,
          'shipping_days': f.get('shipping_days', cfg['shipping_days'])[:100], 'international_days': f.get('international_days', cfg['international_days'])[:150],
          'site_url': site_url, 'hero_title': f.get('hero_title', cfg['hero_title'])[:160], 'hero_text': f.get('hero_text', cfg['hero_text'])[:600]})
        store.audit('settings_changed')
        queue_publish()
        return back_admin('settings', message='Podešavanja su sačuvana.')
    except (ValueError, ArithmeticError) as e:
        return back_admin('settings', error=str(e))


@app.route('/admin/order', methods=['POST'])
@admin_only
def admin_order():
    try:
        store.change_order_status(request.form['id'], request.form['status'])
        queue_publish()
        return back_admin('orders', message='Status porudžbine je sačuvan.')
    except (ValueError, KeyError) as e:
        return back_admin('orders', error=str(e))


@app.route('/admin/payment-test', methods=['POST'])
@admin_only
def payment_test():
    try:
        order = payments.test_order('RSD')
        payments.create_checkout(order, request.host_url)
        return redirect('/order/' + order['token'] + '/')
    except ValueError as e:
        return back_admin('settings', error=str(e))


@app.route('/admin/password', methods=['POST'])
@admin_only
def password_change():
    password = request.form.get('new_password', '')
    if len(password) < 6 or len(password) > 200:
        return back_admin('settings', error='Lozinka mora imati najmanje 6 znakova.')
    PRIVATE['password_hash'] = generate_password_hash(password)
    temp = CONFIG.with_suffix('.tmp')
    temp.write_text(json.dumps(PRIVATE))
    os.chmod(temp, 0o600)
    temp.replace(CONFIG)
    with store.connect(True) as c:
        c.execute('DELETE FROM sessions WHERE id!=?', (g.session['id'],))
    return back_admin('settings', message='Lozinka je promenjena.')


@app.route('/admin/ai', methods=['POST'])
@admin_only
def ai_start():
    import ai_editor
    try:
        job = ai_editor.start(request.form.get('prompt', ''))
        return jsonify(id=job)
    except ValueError as e:
        return jsonify(error=str(e)), 409


@app.route('/admin/specifications',methods=['POST'])
@admin_only
def inventory_specifications():
    import specifications
    data=request.get_json(silent=True) or request.form
    try:return jsonify(specifications.start(data.get('model',''),data.get('brand','')))
    except ValueError as error:return jsonify(error=str(error)),422


@app.route('/admin/inventory-status')
@admin_only
def inventory_ai_status():
    import specifications
    return jsonify(specifications.inventory_status())


@app.route('/admin/specifications/<job>')
@admin_only
def inventory_specification_status(job):
    import specifications
    result=specifications.status(job)
    if not result:abort(404)
    return jsonify(result)


@app.route('/admin/ai/<job>')
@admin_only
def ai_status(job):
    with store.connect() as c:
        row = c.execute('SELECT id,status,output FROM jobs WHERE id=?', (job,)).fetchone()
    if not row:
        abort(404)
    return jsonify(dict(row))


@app.route('/admin/publish', methods=['POST'])
@admin_only
def admin_publish():
    queue_publish()
    return back_admin('settings', message='Objavljivanje je pokrenuto.')


@app.errorhandler(404)
@app.errorhandler(403)
@app.errorhandler(413)
def friendly_error(error):
    return render_template('error.html', error=error, page_title='Stranica nije dostupna', private_page=True), error.code


if __name__ == '__main__':
    from waitress import serve
    serve(app, host='127.0.0.1', port=int(os.environ.get('REKLIK_PORT', 4340)), threads=12, max_request_body_size=35*1024*1024)
