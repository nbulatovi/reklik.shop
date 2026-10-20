"""Durable inventory, pricing and transactional stock reservations."""
import copy
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get('REKLIK_STATE', str(Path.home() / '.local/state/reklik')))
STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
UPLOADS = STATE / 'uploads'
UPLOADS.mkdir(exist_ok=True, mode=0o700)
DB = STATE / 'store.sqlite3'

DEFAULTS = {
    'brand': 'ReKlik.shop', 'currency': 'EUR', 'eur_rsd': '117.2',
    'phone': '+38163697007', 'email': 'support@phonerepairteam.com',
    'pickup_address': '',
    'pickup_enabled': True, 'international_enabled': False,
    'shipping_days': '1–3 radna dana', 'handling_days': '1–2 radna dana',
    'warranty_rsd': {'1': 600, '2': 1200, '3': 1800},
    'couriers': [
        {'id': 'post-express', 'name': 'Post Express', 'enabled': True, 'multiplier': 1,
         'rates': [[.5,420],[1,450],[2,500],[5,600],[10,800],[20,1100]], 'tariff_date':'2026-01-01'},
        {'id': 'aks', 'name': 'AKS', 'enabled': True, 'multiplier': 1,
         'rates': [[.5,450],[1,520],[2,600],[5,820],[10,1050],[15,1350],[20,1450],[30,1650],[50,2500]], 'tariff_date':'2026-07-01'},
        {'id': 'd-express', 'name': 'D Express', 'enabled': True, 'multiplier': 1,
         'rates': [[.5,450],[1,500],[2,600],[5,924],[10,1020],[20,1386],[30,1600],[50,2398.8]], 'tariff_date':'2026-03-13'},
    ],
    'international_rates_rsd': {'region': 6357, 'europe': 15719, 'north-america': 17935, 'world': 18990},
    'international_days': '5–12 radnih dana, bez vremena carinjenja',
    'site_url': 'https://nbulatovi.github.io/reklik.shop',
    'canonical_url': 'https://reklik.shop', 'indexing_enabled': False,
    'payment_enabled': True, 'payment_test_enabled': True,
    'hero_title': 'Nova prilika.\nOdlična tehnologija.',
    'hero_text': 'Sertifikovano obnovljeni laptopovi, telefoni i tableti. Pažljivo testirani, odlično očuvani i spremni za novi početak.',
}


def now():
    return int(time.time())


@contextmanager
def connect(write=False):
    con = sqlite3.connect(DB, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=30000')
    try:
        if write:
            con.execute('BEGIN IMMEDIATE')
        yield con
        if write:
            con.commit()
    except Exception:
        if write:
            con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS categories(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT UNIQUE NOT NULL,
          description TEXT NOT NULL DEFAULT '', icon TEXT NOT NULL DEFAULT 'laptop',
          shipping_rsd INTEGER NOT NULL DEFAULT 600, weight_kg REAL NOT NULL DEFAULT 5,
          international_multiplier REAL NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY, category_id INTEGER NOT NULL REFERENCES categories(id),
          title TEXT NOT NULL, slug TEXT UNIQUE NOT NULL, brand TEXT NOT NULL DEFAULT '',
          price_cents INTEGER NOT NULL CHECK(price_cents>0), quantity INTEGER NOT NULL CHECK(quantity>=0),
          condition TEXT NOT NULL DEFAULT 'Refurbished — odlično', description TEXT NOT NULL DEFAULT '',
          specs TEXT NOT NULL DEFAULT '{}', images TEXT NOT NULL DEFAULT '[]',
          weight_kg REAL NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS orders(
          id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, idem TEXT UNIQUE NOT NULL,
          status TEXT NOT NULL, payment_method TEXT NOT NULL, currency TEXT NOT NULL,
          items TEXT NOT NULL, customer TEXT NOT NULL, delivery TEXT NOT NULL,
          subtotal_minor INTEGER NOT NULL, warranty_minor INTEGER NOT NULL,
          shipping_minor INTEGER NOT NULL, total_minor INTEGER NOT NULL,
          checkout_id TEXT, checkout_url TEXT, plan_id TEXT, payment_id TEXT UNIQUE,
          stock_held INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL, expires_at INTEGER,
          notes TEXT NOT NULL DEFAULT '', test_order INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sessions(
          id TEXT PRIMARY KEY, csrf TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,
          created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, action TEXT NOT NULL, target TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
          output TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, finished_at INTEGER);
        CREATE TABLE IF NOT EXISTS item_views(
          product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
          visitor_hash TEXT NOT NULL, first_seen INTEGER NOT NULL,
          PRIMARY KEY(product_id,visitor_hash));
        CREATE TABLE IF NOT EXISTS model_specs(
          cache_key TEXT PRIMARY KEY,result TEXT,updated_at INTEGER NOT NULL,job_id TEXT);
        CREATE TABLE IF NOT EXISTS product_ai(
          product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
          job_id TEXT, status TEXT NOT NULL, baseline TEXT NOT NULL, fields TEXT NOT NULL,
          message TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL);
        ''')
        product_columns = {row['name'] for row in c.execute('PRAGMA table_info(products)')}
        if 'revision' not in product_columns:
            c.execute('ALTER TABLE products ADD COLUMN revision INTEGER NOT NULL DEFAULT 1')
        category_columns = {row['name'] for row in c.execute('PRAGMA table_info(categories)')}
        if 'shipping_overrides' not in category_columns:
            c.execute("ALTER TABLE categories ADD COLUMN shipping_overrides TEXT NOT NULL DEFAULT '{}'")
        if not c.execute('SELECT 1 FROM categories LIMIT 1').fetchone():
            for n, (name, slug, icon, fee, weight) in enumerate([
                ('Laptopovi', 'laptopovi', 'laptop', 600, 5),
                ('Telefoni', 'telefoni', 'phone', 450, 1),
                ('Tableti', 'tableti', 'tablet', 500, 2),
                ('Oprema', 'oprema', 'headphones', 420, .5),
            ]):
                c.execute('INSERT INTO categories(name,slug,icon,shipping_rsd,weight_kg,sort_order) VALUES(?,?,?,?,?,?)',
                          (name, slug, icon, fee, weight, n))
    os.chmod(DB, 0o600)


def settings():
    result = copy.deepcopy(DEFAULTS)
    with connect() as c:
        for row in c.execute('SELECT key,value FROM settings'):
            result[row['key']] = json.loads(row['value'])
    return result


def set_settings(values):
    with connect(True) as c:
        for key, value in values.items():
            if key in DEFAULTS:
                c.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                          (key, json.dumps(value, ensure_ascii=False)))


def audit(action, target='', detail=''):
    with connect(True) as c:
        c.execute('INSERT INTO audit(action,target,detail,created_at) VALUES(?,?,?,?)',
                  (action, str(target), str(detail)[:2000], now()))


def slugify(value):
    value = str(value).replace('đ', 'dj').replace('Đ', 'Dj')
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '-', value).strip('-')[:100] or secrets.token_hex(4)


def fold(value):
    """Serbian scripts and common ASCII spellings; never alter stored customer text."""
    cyr = dict(zip('абвгдђежзијклљмнњопрстћуфхцчџш',
                   ['a','b','v','g','d','dj','e','z','z','i','j','k','l','lj','m','n','nj','o','p','r','s','t','c','u','f','h','c','c','dz','s']))
    text = ''.join(cyr.get(ch, ch) for ch in str(value).lower()).replace('đ','dj')
    text = unicodedata.normalize('NFKD',text).encode('ascii','ignore').decode()
    text = text.replace('sh','s').replace('ch','c').replace('zh','z')
    return re.sub(r'[^a-z0-9]+',' ',text).strip()


def categories():
    with connect() as c:
        rows = [dict(r) for r in c.execute('''SELECT c.*, (SELECT count(*) FROM products p
          WHERE p.category_id=c.id AND p.active=1 AND p.quantity>0) product_count
          FROM categories c ORDER BY sort_order,name''')]
    for row in rows:
        row['shipping_overrides'] = json.loads(row['shipping_overrides'])
    return rows


def unpack(row):
    if not row:
        return None
    row = dict(row)
    for k in ['specs', 'images', 'items', 'customer', 'delivery', 'shipping_overrides']:
        if k in row:
            row[k] = json.loads(row[k])
    if 'slug' in row and 'price_cents' in row:
        illustration = row['slug'] + '.svg'
        row['catalog_image'] = illustration if (ROOT / 'assets' / illustration).is_file() else None
        row['catalog_images'] = [name for name in [illustration,row['slug']+'-2.svg',row['slug']+'-3.svg'] if (ROOT/'assets'/name).is_file()]
    return row


def products(admin=False, category=None, query=''):
    sql = 'SELECT p.*, c.name category_name,c.slug category_slug,c.icon FROM products p JOIN categories c ON c.id=p.category_id WHERE 1=1'
    if admin:
        sql = sql.replace('SELECT p.*,','SELECT p.*, (SELECT count(*) FROM item_views v WHERE v.product_id=p.id) unique_views, (SELECT status FROM product_ai a WHERE a.product_id=p.id) ai_status,',1)
    args = []
    if not admin:
        sql += ' AND p.active=1'
    if category:
        sql += ' AND c.slug=?'
        args.append(category)
    with connect() as c:
        rows = [unpack(r) for r in c.execute(sql + ' ORDER BY p.updated_at DESC,p.id DESC', args)]
    if query:
        tokens = fold(query).split()
        rows = [p for p in rows if all(token in fold(p['title']+' '+p['brand']+' '+p['description']+' '+' '.join(p['specs'].values())) for token in tokens)]
    return rows


def product(identity, admin=False):
    with connect() as c:
        row = c.execute('''SELECT p.*,c.name category_name,c.slug category_slug,c.icon,c.shipping_rsd,
          c.weight_kg category_weight,c.international_multiplier,c.shipping_overrides FROM products p
          JOIN categories c ON c.id=p.category_id WHERE p.id=? OR p.slug=?''', (identity, identity)).fetchone()
    p = unpack(row)
    return p if p and (admin or p['active']) else None


def money_minor(eur_cents, cfg=None):
    cfg = cfg or settings()
    amount = Decimal(int(eur_cents))
    if cfg['currency'] == 'RSD':
        amount *= Decimal(str(cfg['eur_rsd']))
    return int(amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def rsd_to_minor(rsd, cfg):
    amount = Decimal(str(rsd)) * 100
    if cfg['currency'] == 'EUR':
        amount /= Decimal(str(cfg['eur_rsd']))
    return int(amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def display_money(minor, currency='EUR'):
    amount = Decimal(int(minor)) / 100
    if currency == 'RSD':
        if int(minor) % 100:
            return f'{amount:,.2f}'.replace(',', '_').replace('.', ',').replace('_','.') + ' RSD'
        return f'{amount:,.0f}'.replace(',', '.') + ' RSD'
    decimals = 2 if int(minor) % 100 else 0
    return f'{amount:,.{decimals}f}'.replace(',', ' ').replace('.', ',') + ' €'


def normalize_items(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 30:
        raise ValueError('Izaberite najmanje jedan uređaj.')
    merged = {}
    for line in raw:
        pid, quantity, months = int(line['id']), int(line.get('qty', 1)), int(line.get('warranty', 0))
        if not 1 <= quantity <= 20 or months not in (0, 1, 2, 3):
            raise ValueError('Neispravna količina ili garancija.')
        key = (pid, months)
        merged[key] = merged.get(key, 0) + quantity
        if merged[key] > 20:
            raise ValueError('Prevelika količina.')
    return [{'id': k[0], 'qty': v, 'warranty': k[1]} for k, v in merged.items()]


REGION = {'BA', 'ME', 'MK', 'AL', 'HR', 'SI', 'BG', 'RO', 'HU'}
EUROPE = set('AT BE CY CZ DK EE FI FR DE GR IE IT LV LT LU MT NL PL PT SK ES SE GB CH NO IS LI AD MC SM VA'.split())


def quote(raw, country='RS', delivery='shipping', courier='post-express', cfg=None):
    cfg = cfg or settings()
    raw = normalize_items(raw)
    if country != 'RS' and not cfg['international_enabled']:
        raise ValueError('Međunarodna dostava trenutno nije dostupna. Isporuka u Srbiji i preuzimanje u radnji su dostupni.')
    if delivery == 'pickup' and (not cfg['pickup_enabled'] or country != 'RS'):
        raise ValueError('Preuzimanje u radnji trenutno nije dostupno.')
    if delivery not in ('pickup', 'shipping'):
        raise ValueError('Izaberite način isporuke.')
    lines, subtotal, warranty, weight = [], 0, 0, 0
    override_fees, auto_tariff_needed, international_factor = [], False, Decimal('1')
    product_quantities = {}
    for item in raw:
        p = product(item['id'])
        product_quantities[item['id']] = product_quantities.get(item['id'], 0) + item['qty']
        if not p or p['quantity'] < product_quantities[item['id']]:
            raise ValueError('Uređaj više nije dostupan u izabranoj količini. Osvežite porudžbinu.')
        unit = money_minor(p['price_cents'], cfg)
        extra = rsd_to_minor(cfg['warranty_rsd'][str(item['warranty'])], cfg) if item['warranty'] else 0
        lines.append(dict(item, title=p['title'], slug=p['slug'], unit_minor=unit, warranty_minor=extra,
                          image=p['images'][0] if p['images'] else None, condition=p['condition']))
        subtotal += unit * item['qty']
        warranty += extra * item['qty']
        weight += (p['weight_kg'] or p['category_weight']) * item['qty']
        override = p['shipping_overrides'].get(courier)
        if override is None:
            auto_tariff_needed = True
        else:
            override_fees.append(int(override))
        international_factor = max(international_factor, Decimal(str(p['international_multiplier'])))
    shipping = 0
    if delivery != 'pickup':
        if country == 'RS':
            carrier = next((x for x in cfg['couriers'] if x['id'] == courier and x['enabled']), None)
            if not carrier:
                raise ValueError('Izabrana kurirska služba trenutno nije dostupna.')
            weight_fee = next((fee for limit, fee in carrier['rates'] if weight <= limit), None)
            if weight_fee is None:
                raise ValueError('Za ovu težinu kontaktirajte nas radi dogovora o dostavi.')
            base = max([weight_fee if auto_tariff_needed else 0] + override_fees)
            shipping = rsd_to_minor(Decimal(base) * Decimal(str(carrier['multiplier'])), cfg)
        else:
            zone = 'region' if country in REGION else 'europe' if country in EUROPE else 'north-america' if country in {'US', 'CA'} else 'world'
            # Merchant estimates, not live carrier tariffs. Fuel/packing buffer included.
            base = Decimal(str(cfg['international_rates_rsd'][zone])) * Decimal('1.25') * international_factor
            if weight > 5:
                base *= Decimal(str(weight / 5))
            shipping = rsd_to_minor(base, cfg)
    return {'items': lines, 'subtotal_minor': subtotal, 'warranty_minor': warranty,
            'shipping_minor': shipping, 'total_minor': subtotal + warranty + shipping,
            'currency': cfg['currency'], 'country': country, 'method': delivery,
            'courier': courier if delivery != 'pickup' else 'pickup', 'weight_kg': weight,
            'days': cfg['shipping_days'] if country == 'RS' else cfg['international_days']}


def release_expired():
    with connect(True) as c:
        expired = c.execute("SELECT * FROM orders WHERE status IN ('awaiting_payment','payment_error') AND stock_held=1 AND expires_at<?", (now(),)).fetchall()
        for row in expired:
            for line in json.loads(row['items']):
                c.execute('UPDATE products SET quantity=quantity+?,revision=revision+1 WHERE id=?', (line['qty'], line['id']))
            c.execute("UPDATE orders SET status='expired',stock_held=0 WHERE id=?", (row['id'],))
        return len(expired)


def create_order(raw, customer, delivery, method, idem, test=False, expected=None):
    if method not in ('cod', 'card', 'pickup'):
        raise ValueError('Izaberite način plaćanja.')
    cfg = settings()
    if method == 'cod' and delivery['country'] != 'RS':
        raise ValueError('Pouzeće je dostupno samo za isporuke u Srbiji.')
    if method == 'pickup' and delivery['method'] != 'pickup':
        raise ValueError('Plaćanje u radnji je dostupno uz lično preuzimanje.')
    if method == 'card' and not cfg['payment_enabled']:
        raise ValueError('Kartično plaćanje trenutno nije dostupno.')
    release_expired()
    # The write transaction covers repricing AND inventory reduction; never trust client totals.
    with connect(True) as c:
        existing = c.execute('SELECT * FROM orders WHERE idem=?', (idem,)).fetchone()
        if existing:
            return unpack(existing)
        q = quote(raw, delivery['country'], delivery['method'], delivery['courier'], cfg)
        if expected and (q['currency'],q['total_minor']) != expected:
            raise ValueError('Iznos je ažuriran. Proverite novi sažetak porudžbine i potvrdite ponovo.')
        for line in q['items']:
            result = c.execute('UPDATE products SET quantity=quantity-?,revision=revision+1 WHERE id=? AND quantity>=? AND active=1',
                               (line['qty'], line['id'], line['qty']))
            if result.rowcount != 1:
                raise ValueError('Uređaj je upravo prodat. Izaberite drugi uređaj.')
        oid = 'RK-' + time.strftime('%y%m%d') + '-' + secrets.token_hex(3).upper()
        token = secrets.token_urlsafe(32)
        state = 'awaiting_payment' if method == 'card' else 'new'
        c.execute('''INSERT INTO orders(id,token,idem,status,payment_method,currency,items,customer,delivery,
          subtotal_minor,warranty_minor,shipping_minor,total_minor,created_at,expires_at,test_order)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
          (oid, token, idem, state, method, q['currency'], json.dumps(q['items'], ensure_ascii=False),
           json.dumps(customer, ensure_ascii=False), json.dumps(dict(delivery, days=q['days']), ensure_ascii=False),
           q['subtotal_minor'], q['warranty_minor'], q['shipping_minor'], q['total_minor'], now(),
           now() + 1800 if method == 'card' else None, int(test)))
        return unpack(c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone())


def order_by_token(token):
    with connect() as c:
        return unpack(c.execute('SELECT * FROM orders WHERE token=?', (token,)).fetchone())


def change_order_status(oid, status):
    allowed = {'new', 'confirmed', 'shipped', 'fulfilled', 'cancelled'}
    if status not in allowed:
        raise ValueError('Nepoznat status.')
    with connect(True) as c:
        row = c.execute('SELECT * FROM orders WHERE id=?', (oid,)).fetchone()
        if not row:
            raise ValueError('Porudžbina nije pronađena.')
        if row['payment_method'] == 'card' and row['status'] not in ('paid', 'confirmed', 'shipped', 'fulfilled') and status != 'cancelled':
            raise ValueError('Kartična uplata još nije potvrđena. Ne šaljite uređaj.')
        if row['status'] == 'cancelled' and status != 'cancelled':
            raise ValueError('Otkazana porudžbina ne može ponovo da rezerviše uređaje.')
        if status in ('shipped','fulfilled') and row['status'] not in ('confirmed','shipped','fulfilled'):
            raise ValueError('Prvo telefonom potvrdite adresu i označite porudžbinu kao potvrđenu.')
        if row['status'] in ('shipped','fulfilled') and status == 'cancelled':
            raise ValueError('Poslata roba se vraća u inventar tek nakon prijema povrata. Kontaktirajte administratora.')
        held = row['stock_held']
        if status == 'cancelled' and held:
            if not row['test_order']:
                for line in json.loads(row['items']):
                    c.execute('UPDATE products SET quantity=quantity+?,revision=revision+1 WHERE id=?', (line['qty'], line['id']))
            held = 0
        c.execute('UPDATE orders SET status=?,stock_held=? WHERE id=?', (status, held, oid))
    audit('order_status', oid, status)


init_db()
