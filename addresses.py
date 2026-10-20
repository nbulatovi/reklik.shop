"""Exact, unambiguous matches against a local copy of the official RGZ register."""
import csv
import io
import json
import re
import sqlite3
import sys
import time
import zipfile
from functools import lru_cache
import store

DATABASE = store.STATE / 'addresses.sqlite3'
SOURCE = 'https://data.gov.rs/sr/datasets/adresni-registar/'
DOWNLOAD = 'https://download.geosrbija.rs/download-api/opendata-proxy/export?category=ar&layer=kucni_broj_ar&geometry=true&fileName=kucni_br_csv&format=csv'


@lru_cache(maxsize=100000)
def key(value):
    return store.fold(value).replace(' ', '')


def import_zip(path):
    temporary = DATABASE.with_suffix('.importing')
    temporary.unlink(missing_ok=True)
    con = sqlite3.connect(temporary)
    con.executescript('''PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE TABLE addresses(id TEXT PRIMARY KEY, street_key TEXT NOT NULL, house_key TEXT NOT NULL,
        locality TEXT NOT NULL, municipality TEXT NOT NULL, street TEXT NOT NULL, house TEXT NOT NULL);
      CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);''')
    count, batch = 0, []
    with zipfile.ZipFile(path) as archive:
        with archive.open('kucni_broj.csv') as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw,encoding='utf-8-sig'))
            for row in reader:
                if row['vrsta_stanja_lat'] != 'AKTIVAN' or row['retired']:
                    continue
                street, house = row['ulica_ime_lat'].strip(), row['kucni_broj_lat'].strip()
                if not street or not house:
                    continue
                batch.append((row['kucni_broj_id'],key(street),key(house),row['naselje_ime_lat'],row['opstina_ime_lat'],street,house))
                if len(batch) >= 10000:
                    con.executemany('INSERT OR IGNORE INTO addresses VALUES(?,?,?,?,?,?,?)',batch)
                    count += len(batch); batch.clear()
                    if count % 500000 == 0:
                        print(json.dumps({'imported':count}),flush=True)
            if batch:
                con.executemany('INSERT OR IGNORE INTO addresses VALUES(?,?,?,?,?,?,?)',batch)
                count += len(batch)
    con.execute('CREATE INDEX address_exact ON addresses(street_key,house_key)')
    meta = {'source':SOURCE,'download':DOWNLOAD,'downloaded':time.strftime('%Y-%m-%d'),
            'licence':'Serbian Open Data Licence — https://data.gov.rs/sr/terms/',
            'transforms':'Active rows only; Serbian script/diacritic equivalence; no fuzzy matching; no postal codes',
            'records':str(count)}
    con.executemany('INSERT INTO metadata VALUES(?,?)',meta.items())
    con.commit(); con.close()
    temporary.chmod(0o600)
    temporary.replace(DATABASE)
    return meta


def suggest(address, city, country='RS'):
    if country != 'RS' or not DATABASE.exists() or len(address)>250 or len(city)>180:
        return None
    # Never infer or change a house number, and never guess for villages without a street.
    match = re.fullmatch(r'\s*(.+?)\s+(\d+[A-Za-zА-Яа-яЉЊЂЋЏљњђћџ]*(?:[/\-]\d+[A-Za-zА-Яа-я]*)?)(\s*,\s*(?:stan|sprat|lokal|ulaz|apt)\b.*)?\s*',address,re.I)
    if not match or not city.strip():
        return None
    street, house, detail = match.group(1),match.group(2),match.group(3) or ''
    con = sqlite3.connect('file:' + str(DATABASE) + '?mode=ro',uri=True,timeout=2)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute('SELECT * FROM addresses WHERE street_key=? AND house_key=? LIMIT 1000',(key(street),key(house))).fetchall()
    finally:
        con.close()
    city_key = key(city)
    matches = []
    for row in rows:
        aliases = [row['locality'],row['municipality']]
        if '(' in row['locality']:
            aliases += [row['locality'].split('(')[0], row['locality'].split('(')[1].rstrip(')')]
        if city_key in {key(x) for x in aliases}:
            matches.append(row)
    if len(matches) != 1:
        return None
    row = matches[0]
    canonical_address = row['street'].title() + ' ' + row['house'] + detail
    canonical_city = row['locality'].title()
    if address.strip() == canonical_address and city.strip() == canonical_city:
        return None
    return {'address':canonical_address,'city':canonical_city,'record_id':row['id'],
            'source':'RGZ Adresni registar','source_url':SOURCE,'match':'exact_unique'}


if __name__ == '__main__':
    print(json.dumps(import_zip(sys.argv[1]),ensure_ascii=False),flush=True)
