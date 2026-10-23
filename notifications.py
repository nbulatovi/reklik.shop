"""Durable merchant order notifications, sent without delaying checkout."""
import json
import re
import threading
from urllib.parse import urlparse
import requests
import store

WAKE=threading.Event()


def content(order,event):
    cfg=store.settings()
    paid=order['status'] in ('paid','confirmed','shipped','fulfilled') and order['payment_method']=='card'
    method={'cod':'Pouzeće — kupac plaća pri isporuci','pickup':'Plaćanje pri ličnom preuzimanju',
            'card':'Kartica — uplata potvrđena' if paid else 'Kartica — uplata NIJE potvrđena; ne šaljite uređaj.'}[order['payment_method']]
    subject=f"{cfg['brand']} — {'Uplata potvrđena' if event=='payment_paid' else 'Nova porudžbina'} {order['id']}"
    customer=order['customer'];delivery=order['delivery']
    lines=[subject,'',f"Status: {order['status']}",method,'',
           'KUPAC',customer.get('name',''),f"Telefon: {customer.get('phone','')}",f"Email: {customer.get('email','')}",'', 'UREĐAJI']
    for item in order['items']:
        lines.append(f"{item['qty']} × {item['title']} — {store.display_money(item['unit_minor']*item['qty'],order['currency'])}")
        if item.get('warranty'):lines.append(f"  Dodatno pokriće: {item['warranty']} mes.")
    for label,key in [('Uređaji','subtotal_minor'),('Dodatna garancija','warranty_minor'),('Dostava','shipping_minor'),('UKUPNO','total_minor')]:
        lines.append(f"{label}: {store.display_money(order[key],order['currency'])}")
    if order.get('pricing'):
        fx=order['pricing'];lines+=['Protivvrednost: približno '+store.display_money(fx['total_eur_cents'],'EUR')+'; srednji kurs NBS '+fx['rate']+' ('+fx['date']+').']
    lines+=['','ISPORUKA']
    if delivery['method']=='pickup':lines.append('Lično preuzimanje — lokaciju i termin dogovoriti telefonom.')
    else:
        courier=next((c['name'] for c in cfg['couriers'] if c['id']==delivery.get('courier')),delivery.get('courier',''))
        lines += [courier,delivery.get('address',''),f"{delivery.get('postal_code','')} {delivery.get('city','')}, {delivery.get('country','')}"]
    lines+=['Pozovite kupca da potvrdite adresu/lokaciju i termin pre slanja.']
    if customer.get('note'):lines+=['','NAPOMENA KUPCA',customer['note']]
    lines+=['','Porudžbine u administraciji: '+cfg['site_url'].rstrip('/')+'/admin/']
    return {'to':cfg['email'],'subject':subject,'text':'\n'.join(lines),
            'fromName':cfg['brand'],'reply_to':customer.get('email') or cfg['email']}


def deliver(payload):
    """Use the PC's existing authenticated email relay; credentials stay private."""
    try:config=json.loads((store.STATE/'notifications.json').read_text())
    except (OSError,ValueError):raise ValueError('Email servis nije podešen.') from None
    if urlparse(config.get('url','')).scheme!='https' or not config.get('secret'):
        raise ValueError('Email servis nije ispravno podešen.')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',payload['to']):
        raise ValueError('Kontakt email nije ispravan.')
    try:
        response=requests.post(config['url'],json=dict(payload,**{'from':config['from']}),
                               headers={'X-Webhook-Secret':config['secret'],'User-Agent':'ReKlik-Order-Mail/1.0'},
                               timeout=20,allow_redirects=False)
        if response.status_code!=200:raise ValueError(f'Email servis: HTTP {response.status_code}.')
        result=response.json()
        if result.get('status')!='sent' or not result.get('id'):raise ValueError('Email servis nije potvrdio slanje.')
        return result['id']
    except requests.RequestException:raise ValueError('Email servis je privremeno nedostupan.') from None


def run_once(limit=10):
    sent=0
    for _ in range(limit):
        with store.connect(True) as c:
            row=c.execute("SELECT * FROM order_notifications WHERE status='pending' AND next_attempt<=? AND locked_until<=? ORDER BY id LIMIT 1",(store.now(),store.now())).fetchone()
            if not row:break
            row=dict(row)
            c.execute('UPDATE order_notifications SET attempts=attempts+1,locked_until=? WHERE id=?',(store.now()+90,row['id']))
            order=store.unpack(c.execute('SELECT * FROM orders WHERE id=?',(row['order_id'],)).fetchone())
        recipient=store.settings()['email']
        try:
            payload=content(order,row['event']);recipient=payload['to']
            provider_id=deliver(payload)
            with store.connect(True) as c:
                c.execute("UPDATE order_notifications SET status='sent',recipient=?,provider_id=?,error='',sent_at=?,locked_until=0 WHERE id=?",(recipient,provider_id,store.now(),row['id']))
            store.audit('order_email_sent',row['order_id'],row['event']+' -> '+recipient)
            sent+=1
        except Exception as error:
            delay=min(3600,30*2**min(row['attempts'],7))
            message=str(error)[:250] if isinstance(error,ValueError) else 'Slanje emaila nije uspelo.'
            with store.connect(True) as c:
                c.execute('UPDATE order_notifications SET recipient=?,error=?,next_attempt=?,locked_until=0 WHERE id=?',(recipient,message,store.now()+delay,row['id']))
            store.audit('order_email_retry',row['order_id'],message)
    return sent


def run():
    while True:
        try:run_once()
        except Exception:store.audit('notification_worker_error',detail='Slanje će biti ponovljeno.')
        WAKE.wait(5);WAKE.clear()
