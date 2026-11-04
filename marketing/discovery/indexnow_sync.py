"""Notify IndexNow about public Pages changes, without browser tracking.

Read the deployed sitemap and HTML, never the inventory database or customer data.
Only acknowledge snapshots after HTTP 200/202 so failed submissions retry safely.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import requests

ORIGIN = 'https://reklik.shop'
KEY_URL = ORIGIN + '/assets/indexnow.txt'
ENDPOINT = 'https://api.indexnow.org/indexnow'
ALLOWED_PATH = re.compile(r'/(?:|katalog/|proizvod/[1-9][0-9]*/|kategorija/[a-z0-9-]+/|o-nama/|dostava/|garancija/|uslovi/|privatnost/|povracaj/|kontakt/)')
MAX_URLS = 1000
HEADERS = {'User-Agent': 'ReKlik-SearchDiscovery/1.0 (+https://reklik.shop/kontakt/)'}


def public_url(url):
    parsed = urlsplit(url)
    return (parsed.scheme == 'https' and parsed.netloc == 'reklik.shop'
            and not parsed.query and not parsed.fragment
            and ALLOWED_PATH.fullmatch(parsed.path) is not None)


def sitemap_urls(document):
    root = ET.fromstring(document)
    urls = [node.text or '' for node in root.findall('{http://www.sitemaps.org/schemas/sitemap/0.9}url/{http://www.sitemaps.org/schemas/sitemap/0.9}loc')]
    if not urls or len(urls) > MAX_URLS or any(not public_url(u) for u in urls):
        raise ValueError('Public sitemap contains an unexpected URL or size.')
    return sorted(set(urls))


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)
    path.chmod(0o600)


def read_response(session, url, *, headers=None, maximum=2_000_000):
    response = session.get(url, headers=headers or {}, timeout=(8, 20),
                           allow_redirects=False, stream=True)
    data = bytearray()
    for chunk in response.iter_content(65536):
        data.extend(chunk)
        if len(data) > maximum:
            response.close()
            raise ValueError('Unexpected public response size.')
    response.close()
    return response, bytes(data)


def synchronize(directory, session=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    session = session or requests.Session()
    session.headers.update(HEADERS)
    state_path = directory / 'accepted.json'
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    if previous.get('last_attempt', 0) > time.time() - 300:
        return {'status': 'debounced'}
    if previous and previous.get('origin') != ORIGIN:
        raise ValueError('Unexpected saved origin.')

    response, key_bytes = read_response(session, KEY_URL, maximum=256)
    key = key_bytes.decode('ascii').strip()
    if response.status_code != 200 or not re.fullmatch(r'[a-zA-Z0-9-]{8,128}', key):
        raise ValueError('Published IndexNow ownership file is unavailable.')
    response, document = read_response(session, ORIGIN + '/sitemap.xml', maximum=1_000_000)
    if response.status_code != 200:
        raise ValueError('Public sitemap is unavailable.')
    urls = sitemap_urls(document)
    old_pages = previous.get('pages', {})
    if any(not public_url(u) for u in old_pages):
        raise ValueError('Unexpected URL in saved public snapshot.')
    pages = {}
    changed = []
    for url in urls:
        old = old_pages.get(url, {})
        conditional = {'If-None-Match': old['etag']} if old.get('etag') else {}
        response, content = read_response(session, url, headers=conditional)
        if response.status_code == 304 and old:
            pages[url] = old
            continue
        if response.status_code != 200 or 'text/html' not in response.headers.get('Content-Type', '').lower():
            raise ValueError('A canonical public page is not ready; retry after deployment.')
        snapshot = {'sha256': hashlib.sha256(content).hexdigest(), 'etag': response.headers.get('ETag', '')}
        pages[url] = snapshot
        if snapshot['sha256'] != old.get('sha256'):
            changed.append(url)
    for url in sorted(set(old_pages) - set(urls)):
        response, _ = read_response(session, url)
        if response.status_code not in (404, 410):
            raise ValueError('Removed page has not disappeared from the public deployment yet.')
        changed.append(url)

    report = {'checked_at': int(time.time()), 'public_pages': len(pages),
              'changed_urls': changed, 'status': 'unchanged'}
    if changed:
        response = session.post(ENDPOINT, json={'host': 'reklik.shop', 'key': key,
                                'keyLocation': KEY_URL, 'urlList': changed},
                                timeout=(8, 30), allow_redirects=False)
        report.update(http_status=response.status_code, status='received' if response.status_code in (200, 202) else 'retry')
        if response.status_code not in (200, 202):
            # Keep acknowledged content snapshots untouched on rejection/rate limit.
            write_json(directory / 'last-result.json', report)
            return report
    write_json(state_path, {'origin': ORIGIN, 'last_attempt': int(time.time()), 'pages': pages})
    write_json(directory / 'last-result.json', report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/reklik/search-discovery')
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state / 'sync.lock').open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = synchronize(args.state)
        except Exception as error:
            # Do not echo HTTP exception URLs, credentials or response bodies.
            result = {'status': 'error', 'error_type': type(error).__name__, 'checked_at': int(time.time())}
            write_json(args.state / 'last-result.json', result)
        print(json.dumps(result))
        return 0 if result['status'] in ('received', 'unchanged', 'debounced') else 1


if __name__ == '__main__':
    raise SystemExit(main())
