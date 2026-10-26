"""First-party, cookie-free item funnel counters. No browser analytics JavaScript."""
import hashlib
import hmac
import ipaddress
import re
import store

STAGES={'view','buy_now','cart_click','checkout_open','checkout_attempt','checkout_failed','cod_attempt','cod_failed','cod_order','card_attempt','card_failed','card_order','card_paid','pickup_attempt','pickup_failed','pickup_order','order_created'}
REASONS={
 'missing_name':'Nedostaje ime i prezime','missing_phone':'Nedostaje telefon','invalid_phone':'Neispravan telefon',
 'missing_email':'Nedostaje email','invalid_email':'Neispravan email','missing_city':'Nedostaje mesto za dostavu',
 'invalid_postcode':'Neispravan poštanski broj','terms':'Uslovi kupovine nisu potvrđeni','quote_changed':'Iznos je promenjen',
 'session':'Istekla ili blokirana sesija','security':'Provera bezbednosti nije prošla','stock':'Uređaj više nije dostupan',
 'shipping':'Dostava nije dostupna','processor':'Platni servis nije otvorio naplatu','form':'Druga greška u podacima',
}


def actor(address,secret,pid):
    try:
        ip=ipaddress.ip_address(str(address))
        if isinstance(ip,ipaddress.IPv6Address) and ip.ipv4_mapped:ip=ip.ipv4_mapped
    except ValueError:return None
    return hmac.new(secret.encode(),('item-view:'+str(pid)+':'+str(ip)).encode(),hashlib.sha256).hexdigest()


def visitors(items,request,secret,admin=False):
    if admin or re.search(r'bot|spider|crawler|preview',request.headers.get('User-Agent',''),re.I):return {}
    if 'prefetch' in (request.headers.get('Purpose','')+' '+request.headers.get('Sec-Purpose','')).lower():return {}
    if request.form.get('website'):return {}
    ip=request.headers.get('CF-Connecting-IPv6') or request.headers.get('CF-Connecting-IP') or request.remote_addr
    identities={}
    for item in items[:30]:
        try:pid=int(item['id'])
        except (KeyError,ValueError,TypeError):continue
        if pid<1:continue
        identity=actor(ip,secret,pid)
        if identity:identities[pid]=identity
    return identities


def write(c,identities,stage,reason=''):
    if stage not in STAGES:return
    reason=reason if reason in REASONS else ''
    now=store.now()
    for pid,identity in identities.items():
        if not c.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone():continue
        c.execute('''INSERT INTO item_events(product_id,stage,visitor_hash,reason,first_seen,last_seen,hits)
          VALUES(?,?,?,?,?,?,1) ON CONFLICT(product_id,stage,visitor_hash,reason)
          DO UPDATE SET last_seen=excluded.last_seen,hits=hits+1''',(pid,stage,identity,reason,now,now))
        if stage=='view':c.execute('INSERT OR IGNORE INTO item_views(product_id,visitor_hash,first_seen) VALUES(?,?,?)',(pid,identity,now))


def record(identities,stage,reason=''):
    if not identities:return
    with store.connect(True) as c:write(c,identities,stage,reason)


def bind_order(c,order_id,identities,method):
    if not identities:return
    write(c,identities,'order_created');write(c,identities,method+'_order')
    for pid,identity in identities.items():
        c.execute('INSERT OR IGNORE INTO order_visitors(order_id,product_id,visitor_hash) VALUES(?,?,?)',(order_id,pid,identity))


def paid(c,order_id):
    identities={r['product_id']:r['visitor_hash'] for r in c.execute('SELECT product_id,visitor_hash FROM order_visitors WHERE order_id=?',(order_id,))}
    write(c,identities,'card_paid')


def failure_reason(message):
    value=store.fold(message)
    if 'popunite polje' in value:
        return 'missing_email' if 'email' in value else 'missing_phone' if 'telefon' in value else 'missing_name'
    for words,reason in [(['email'],'invalid_email'),(['telefon'],'invalid_phone'),(['grad','mesto','selo'],'missing_city'),
                         (['postanski'],'invalid_postcode'),(['uslov'],'terms'),(['iznos','kurs'],'quote_changed'),
                         (['prodat','dostupan uredjaj','raspoloziv'],'stock'),(['dostav','tezina','postarin'],'shipping')]:
        if any(word in value for word in words):return reason
    return 'form'


def summary(pid):
    with store.connect() as c:
        counts={r['stage']:r['count'] for r in c.execute('SELECT stage,count(DISTINCT visitor_hash) count FROM item_events WHERE product_id=? GROUP BY stage',(pid,))}
        # Keep all historical item-view counts, including those collected before funnel tracking.
        counts['view']=c.execute('SELECT count(*) FROM item_views WHERE product_id=?',(pid,)).fetchone()[0]
        uncompleted=c.execute("SELECT count(DISTINCT a.visitor_hash) FROM item_events a WHERE a.product_id=? AND a.stage='cod_attempt' AND NOT EXISTS(SELECT 1 FROM item_events b WHERE b.product_id=a.product_id AND b.visitor_hash=a.visitor_hash AND b.stage='cod_order')",(pid,)).fetchone()[0]
        abandoned=c.execute("SELECT count(DISTINCT a.visitor_hash) FROM item_events a WHERE a.product_id=? AND a.stage='checkout_open' AND NOT EXISTS(SELECT 1 FROM item_events b WHERE b.product_id=a.product_id AND b.visitor_hash=a.visitor_hash AND (b.stage='order_created' OR (b.stage IN ('checkout_open','checkout_attempt') AND b.last_seen>=?)))",(pid,store.now()-1800)).fetchone()[0]
        errors=[{'label':REASONS.get(r['reason'],'Druga greška'),'count':r['count']} for r in c.execute("SELECT reason,count(DISTINCT visitor_hash) count FROM item_events WHERE product_id=? AND stage IN ('cod_failed','card_failed','pickup_failed','checkout_failed') AND reason!='' GROUP BY reason ORDER BY count DESC,reason",(pid,))]
        started=c.execute("SELECT applied_at FROM migrations WHERE name='item-funnel-v1'").fetchone()
    rows=[('view','Otvorili uređaj'),('buy_now','Kliknuli Kupi sada'),('cart_click','Kliknuli Dodaj u korpu'),
          ('checkout_open','Otvorili naplatu'),('checkout_attempt','Poslali podatke za porudžbinu'),('cod_attempt','Pouzeće: pokušali da poruče'),('cod_failed','Pouzeće: naišli na grešku'),
          ('cod_order','Pouzeće: uspešno poručili'),('card_attempt','Kartica: pokušali da poruče'),('card_failed','Kartica: naišli na grešku'),('card_order','Kartica: porudžbina kreirana'),('card_paid','Kartica: uplata potvrđena'),
          ('pickup_order','Poručili za preuzimanje')]
    metrics=[{'key':key,'label':label,'count':counts.get(key,0)} for key,label in rows]
    metrics.extend([{'key':'cod_uncompleted','label':'Pokušaji pouzeća bez porudžbine','count':uncompleted},
                    {'key':'checkout_uncompleted','label':'Bez porudžbine posle 30 minuta','count':abandoned}])
    return {'metrics':metrics,'counts':counts,'errors':errors,'started_at':started['applied_at'] if started else store.now()}
