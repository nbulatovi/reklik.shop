"""Whop hosted checkout. Card data never enters this server."""
import base64
import hashlib
import hmac
import json
import os
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse
import requests
from dotenv import dotenv_values
import store


def credentials():
    source = dotenv_values(os.environ.get('REKLIK_PAYMENT_ENV', str(store.Path.home() / '.secrets/electricians.env')))
    return {key: os.environ.get(key) or source.get(key, '') for key in ('WHOP_API_KEY', 'WHOP_COMPANY_ID', 'WHOP_WEBHOOK_SECRET')}


def api(method, path, **kwargs):
    auth = credentials()
    if not auth['WHOP_API_KEY'] or not auth['WHOP_COMPANY_ID']:
        raise ValueError('Kartično plaćanje trenutno nije dostupno. Izaberite pouzeće ili preuzimanje.')
    try:
        response = requests.request(method, 'https://api.whop.com/api/v1/' + path,
            headers={'Authorization': 'Bearer ' + auth['WHOP_API_KEY'], 'Content-Type': 'application/json'},
            timeout=25, **kwargs)
        data = response.json()
    except (requests.RequestException, ValueError):
        raise ValueError('Veza sa platnim servisom nije uspela. Pokušajte ponovo; porudžbina je sačuvana.')
    if not response.ok:
        store.audit('payment_api_error', path, str(response.status_code))
        raise ValueError('Platni servis trenutno ne može da otvori naplatu. Porudžbina nije naplaćena.')
    return data


def create_checkout(order, origin):
    auth = credentials()
    payload = {
        'mode': 'payment', 'redirect_url': store.settings()['site_url'].rstrip('/') + '/potvrda/?order=' + order['token'],
        'plan': {'company_id': auth['WHOP_COMPANY_ID'], 'currency': order['currency'].lower(),
                 'plan_type': 'one_time', 'initial_price': order['total_minor'] / 100,
                 'title': ('ReKlik ' + order['id'])[:60], 'force_create_new_plan': True,
                 'adaptive_pricing_enabled': False, 'stock': 1,
                 'payment_method_configuration': {'enabled':['card','apple_pay'],'disabled':[],'include_platform_defaults':False},
                 'product': {'title': 'ReKlik — test plaćanja' if order['test_order'] else 'ReKlik — refabrikovana tehnologija',
                             'external_identifier': 'reklik-' + order['id'],
                             'custom_statement_descriptor': 'WHOP*REKLIK SHOP',
                             'description': 'Jednokratna kupovina. ' + order['id']}},
        'metadata': {'store': 'reklik', 'order_id': order['id'], 'test_order': str(bool(order['test_order'])).lower()},
        'checkout_styling': {'button_color': '#fb7701', 'background_color': '#ffffff'},
    }
    data = api('POST', 'checkout_configurations', json=payload)
    url = data.get('purchase_url', '')
    if url.startswith('/'):
        url = 'https://whop.com' + url
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('whop.com', 'checkout.whop.com'):
        raise ValueError('Platni servis nije vratio ispravan link za plaćanje.')
    plan = data.get('plan') or {}
    with store.connect(True) as c:
        c.execute('UPDATE orders SET checkout_id=?,checkout_url=?,plan_id=? WHERE id=?',
                  (data.get('id'), url, plan.get('id'), order['id']))
    return url


def verify_payment(order, payment):
    """Bind every successful payment to merchant, plan, currency, amount and order."""
    auth = credentials()
    company = payment.get('company') or payment.get('account') or {}
    company_id = company.get('id') if isinstance(company, dict) else company
    company_id = company_id or payment.get('company_id') or payment.get('account_id')
    plan = payment.get('plan') or {}
    plan_id = plan.get('id') if isinstance(plan, dict) else plan
    plan_id = plan_id or payment.get('plan_id')
    meta = payment.get('metadata') or {}
    state = payment.get('status')
    if state not in ('paid', 'succeeded', 'completed'):
        return False
    if company_id != auth['WHOP_COMPANY_ID'] or not order.get('plan_id') or plan_id != order['plan_id']:
        return False
    if meta.get('order_id') and meta.get('order_id') != order['id']:
        return False
    if str(payment.get('currency', '')).upper() != order['currency']:
        return False
    amount = payment.get('total')
    if amount is None:
        amount = payment.get('amount')
    if amount is None:
        return False
    amount_minor = int((Decimal(str(amount)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if amount_minor != order['total_minor'] or not payment.get('id'):
        return False
    with store.connect(True) as c:
        current = c.execute('SELECT * FROM orders WHERE id=?', (order['id'],)).fetchone()
        if current['payment_id'] == payment['id'] and current['status'] in ('paid', 'confirmed', 'shipped', 'fulfilled'):
            return True
        if current['payment_id'] and current['payment_id'] != payment['id']:
            c.execute('INSERT INTO audit(action,target,detail,created_at) VALUES(?,?,?,?)',
                      ('duplicate_payment_review',order['id'],payment['id'],store.now()))
            return False
        if current['status'] in ('cancelled', 'refunded', 'payment_review'):
            return False
        if not current['stock_held'] and not current['test_order']:
            # An expired checkout can still be paid: never oversell the last device.
            for line in json.loads(current['items']):
                available = c.execute('SELECT quantity FROM products WHERE id=?', (line['id'],)).fetchone()
                if not available or available['quantity'] < line['qty']:
                    c.execute("UPDATE orders SET status='payment_review',payment_id=? WHERE id=?", (payment['id'], order['id']))
                    return False
            for line in json.loads(current['items']):
                c.execute('UPDATE products SET quantity=quantity-?,revision=revision+1 WHERE id=?', (line['qty'], line['id']))
        c.execute("UPDATE orders SET status='paid',payment_id=?,stock_held=1,expires_at=NULL WHERE id=?",
                  (payment['id'], order['id']))
        if not current['test_order']:
            import analytics
            analytics.paid(c,order['id'])
            c.execute("INSERT OR IGNORE INTO order_notifications(order_id,event,created_at) VALUES(?,'payment_paid',?)",(order['id'],store.now()))
    store.audit('payment_verified', order['id'], payment['id'])
    return True


def reconcile(order):
    if not order.get('plan_id') or order['status'] not in ('awaiting_payment', 'payment_error', 'expired'):
        return False
    data = api('GET', 'payments', params={'company_id': credentials()['WHOP_COMPANY_ID'], 'plan_ids[]': order['plan_id'], 'first': 25})
    for item in data.get('data', []):
        if verify_payment(order, item):
            return True
    return False


def verify_webhook(body, headers):
    secret = credentials().get('WHOP_WEBHOOK_SECRET')
    if not secret:
        return False
    timestamp = headers.get('webhook-timestamp', '')
    try:
        if abs(store.now() - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    message = headers.get('webhook-id', '') + '.' + timestamp + '.'
    # Current Whop docs specify the raw ws_ secret as the HMAC key.
    signature = base64.b64encode(hmac.new(secret.encode(), message.encode() + body, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(signature, part.split(',', 1)[1])
               for part in headers.get('webhook-signature', '').split() if part.startswith('v1,'))


def test_order(currency='EUR'):
    oid = 'TEST-' + store.secrets.token_hex(5).upper()
    token = store.secrets.token_urlsafe(32)
    amount = 100 if currency == 'EUR' else 12000
    with store.connect(True) as c:
        c.execute('''INSERT INTO orders(id,token,idem,status,payment_method,currency,items,customer,delivery,
          subtotal_minor,warranty_minor,shipping_minor,total_minor,stock_held,created_at,expires_at,test_order)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)''',
          (oid, token, oid, 'awaiting_payment', 'card', currency,
           json.dumps([{'title': 'Test kartičnog plaćanja — stvarna naplata', 'qty': 1, 'unit_minor': amount, 'warranty_minor': 0, 'warranty': 0}]),
           '{}', json.dumps({'method': 'test', 'country': 'RS'}), amount, 0, 0, amount, 0, store.now(), store.now()+1800))
    return store.order_by_token(token)
