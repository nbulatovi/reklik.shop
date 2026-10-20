"""Finish the authorized domain cutover when the owner's DNS records are ready."""
import fcntl
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import urlopen
from urllib.parse import urlparse
import store

DOMAIN='reklik.shop'
PAGES='repos/nbulatovi/reklik.shop/pages'
GITHUB_IPS={f'185.199.{n}.153' for n in (108,109,110,111)}
GITHUB_IPV6={f'2606:50c0:800{n}::153' for n in range(4)}


def records(host,kind):
    # Ask the domain's authoritative Namecheap server, avoiding stale parking caches.
    result=subprocess.run(['dig','@dns1.registrar-servers.com','+short','+time=3','+tries=1',host,kind],capture_output=True,text=True,timeout=5,check=True)
    return {line.strip().rstrip('.') for line in result.stdout.splitlines() if line.strip()}


def dns_ready():
    apex=records(DOMAIN,'A')
    return (bool(apex) and apex<=GITHUB_IPS and records('www.'+DOMAIN,'CNAME')=={'nbulatovi.github.io'}
            and records(DOMAIN,'AAAA')<=GITHUB_IPV6)


def github(values=None):
    args=['gh','api',PAGES]
    if values is not None:args+=['--method','PUT','--input','-']
    result=subprocess.run(args,input=json.dumps(values) if values is not None else None,
                          cwd=store.ROOT,capture_output=True,text=True,timeout=30)
    if result.returncode:raise RuntimeError('GitHub Pages podešavanje nije uspelo; biće ponovljeno.')
    return json.loads(result.stdout) if result.stdout.strip() else {}


def state(phase,message):
    report={'domain':DOMAIN,'phase':phase,'message':message,'updated_at':store.now()}
    destination=store.STATE/'domain-activation.json'
    temporary=destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(report,ensure_ascii=False));temporary.replace(destination)
    print(phase+': '+message,flush=True)


def read(url):
    if urlparse(url).hostname in (DOMAIN,'www.'+DOMAIN):
        # Check the configured origin with its real hostname/SNI and certificate.
        # Recursive resolvers may retain Namecheap parking records during cutover.
        result=subprocess.run(['curl','--fail','--silent','--show-error','--location','--max-time','12',
                               '--proto','=https','--proto-redir','=https',
                               '--resolve',DOMAIN+':443:185.199.108.153',
                               '--resolve','www.'+DOMAIN+':443:185.199.108.153',url],
                              capture_output=True,text=True,timeout=15,check=True)
        return result.stdout
    with urlopen(url,timeout=12) as response:
        if response.status!=200:raise ValueError('Stranica još nije spremna.')
        return response.read().decode()


def healthy(indexed=False):
    origin='https://'+DOMAIN
    paths=['/','/catalog.json','/backend.json','/admin/','/checkout/','/sitemap.xml','/robots.txt']
    with ThreadPoolExecutor(max_workers=4) as pool:
        bodies=dict(zip(paths,pool.map(lambda path:read(origin+path),paths)))
    if ('footer-admin' not in bodies['/'] or 'data-base=""' not in bodies['/']
            or not isinstance(json.loads(bodies['/catalog.json']).get('products'),list)):
        raise ValueError('Novi katalog još nije objavljen.')
    if 'data-frame-kind="admin"' not in bodies['/admin/'] or 'data-frame-kind="checkout"' not in bodies['/checkout/']:
        raise ValueError('Administracija ili kupovina još nisu objavljene.')
    endpoint=json.loads(bodies['/backend.json'])['backend']
    if 'footer-admin' not in read('https://www.'+DOMAIN+'/'):raise ValueError('www adresa još nije spremna.')
    if json.loads(read(endpoint+'/health')).get('ok') is not True:raise ValueError('Backend nije spreman.')
    if 'https://'+DOMAIN+'/kategorija/' not in bodies['/sitemap.xml']:raise ValueError('Mapa sajta nije spremna.')
    if indexed and ('Disallow: /\n' in bodies['/robots.txt'] or 'name="robots" content="noindex' in bodies['/']):
        raise ValueError('SEO postavke se još objavljuju.')


def step():
    import publisher
    if not dns_ready():
        state('waiting_dns','Čekamo da Namecheap DNS vodi na GitHub Pages. Postojeći sajt ostaje dostupan.')
        return False
    pages=github()
    if pages.get('cname')!=DOMAIN:
        state('configuring','Povezujemo kupljeni domen sa postojećim hostingom.')
        # The API can create a CNAME commit. Serialize it with all catalog publishers.
        with publisher.LOCK,(store.STATE/'publish.lock').open('a') as lock:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX)
            if publisher.git('status','--porcelain').stdout.strip():
                raise RuntimeError('Čekamo završetak trenutnog objavljivanja izvornog koda.')
            github({'cname':DOMAIN})
            publisher.git('fetch','origin','main')
            publisher.git('merge','--ff-only','origin/main')
        pages=github()
    cfg=store.settings()
    if cfg['site_url']!='https://'+DOMAIN or cfg['canonical_url']!='https://'+DOMAIN:
        store.set_settings({'site_url':'https://'+DOMAIN,'canonical_url':'https://'+DOMAIN,'indexing_enabled':False})
    # Retry an interrupted export/push as well as the initial cutover.
    publisher.publish()
    pages=github()
    certificate=pages.get('https_certificate') or {}
    if certificate.get('state')!='approved':
        state('waiting_https','GitHub priprema HTTPS sertifikat za novi domen.')
        return False
    if not pages.get('https_enforced'):github({'https_enforced':True})
    healthy()
    if not store.settings()['indexing_enabled']:
        store.set_settings({'indexing_enabled':True})
        publisher.publish()
    healthy(indexed=True)
    state('ready','Domen, HTTPS, katalog, administracija, kupovina i SEO su povezani i provereni.')
    store.audit('domain_activated',DOMAIN)
    return True


def run():
    with (store.STATE/'domain.lock').open('a') as lock:
        try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return
        while True:
            try:
                if step():return
            except Exception as error:
                state('retrying',str(error)[:300])
            time.sleep(45)


if __name__=='__main__':run()
