"""Cached official NBS middle EUR rate; network refresh never runs in checkout."""
from datetime import datetime, timedelta
from decimal import Decimal
import html
import re
import threading
from zoneinfo import ZoneInfo
import requests
import store

SOURCE='https://webappcenter.nbs.rs/ExchangeRateWebApp/ExchangeRate/CurrentMiddleRate'


def parse(document):
    text=html.unescape(re.sub(r'<[^>]+>',' ',document))
    day=re.search(r'(\d{2}\.\d{2}\.\d{4})',text)
    rate=None
    for row in re.findall(r'<tr\b[^>]*>(.*?)</tr>',document,re.S|re.I):
        cells=[html.unescape(re.sub(r'<[^>]+>','',c)).strip() for c in re.findall(r'<td\b[^>]*>(.*?)</td>',row,re.S|re.I)]
        if len(cells)==5 and cells[0]=='EUR' and cells[1]=='978' and cells[3]=='1':
            rate=Decimal(cells[4].replace('.','').replace(',','.'))
    if not day or rate is None or not rate.is_finite() or not Decimal('50')<rate<Decimal('250'):
        raise ValueError('NBS kursna lista nije prepoznata.')
    date=datetime.strptime(day[1],'%d.%m.%Y').date()
    today=datetime.now(ZoneInfo('Europe/Belgrade')).date()
    if not today-timedelta(days=7)<=date<=today:raise ValueError('NBS lista nije važeća.')
    return {'rate':str(rate),'date':date.isoformat(),'source':SOURCE}


def current(cfg=None):
    cfg=cfg or store.settings()
    try:
        rate=Decimal(str(cfg['eur_rsd']))
        date=datetime.strptime(cfg['eur_rsd_date'],'%Y-%m-%d').date()
        today=datetime.now(ZoneInfo('Europe/Belgrade')).date()
        if cfg['eur_rsd_source']!=SOURCE or not rate.is_finite() or not 50<rate<250 or not today-timedelta(days=7)<=date<=today:raise ValueError()
    except (ValueError,KeyError,ArithmeticError):
        raise ValueError('Obračun po srednjem kursu NBS trenutno nije dostupan. Pokušajte ponovo uskoro ili nas pozovite.') from None
    return {'rate':str(rate),'date':date.isoformat(),'source':SOURCE}


def refresh():
    response=requests.get(SOURCE,timeout=15)
    response.raise_for_status()
    value=parse(response.text)
    store.set_settings({'eur_rsd':value['rate'],'eur_rsd_date':value['date'],
                        'eur_rsd_source':SOURCE,'eur_rsd_checked_at':store.now()})
    return value


def run():
    while True:
        delay=3600
        try:
            before=store.settings()['eur_rsd']
            result=refresh()
            if before!=result['rate']:
                from app import queue_publish
                queue_publish()
            now=datetime.now(ZoneInfo('Europe/Belgrade'))
            next_publication=now.replace(hour=8,minute=0,second=15,microsecond=0)
            if next_publication<=now:next_publication+=timedelta(days=1)
            delay=min(delay,max(1,(next_publication-now).total_seconds()))
        except Exception:
            store.audit('nbs_rate_refresh_failed',detail='Sačuvan je poslednji potvrđeni kurs; ponovni pokušaj za pet minuta.')
            delay=300
        threading.Event().wait(delay)
