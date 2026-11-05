"""Durable Halo Oglasi mirroring from the shop's authoritative inventory.

The web app only reads status and touches a wake-up marker. A separate worker
does browser work, preserving mappings after product deletion and checkpointing
publication before its external side effect. Known ads are never recreated.
"""
from contextlib import ExitStack
import argparse
from decimal import Decimal
import fcntl
import hashlib
from html import unescape
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

import requests
import store

ROOT = store.STATE / 'halo-mirror'
LABELS = {
    'active':'Aktivan oglas', 'pending_review':'Čeka proveru Halo oglasa',
    'pending':'Čeka sinhronizaciju', 'waiting_public':'Čeka objavu na sajtu',
    'creating':'Predaja je u toku', 'uncertain':'Potrebna provera pre ponovne predaje',
    'paused':'Pauziran', 'expired':'Čeka besplatnu obnovu',
    'auth_required':'Potrebna prijava na Halo oglase',
    'fee_required':'Zaustavljeno: traži se naplata',
    'needs_review':'Potrebna provera', 'missing':'Postojeći oglas nije pronađen',
    'error':'Ponovni pokušaj sinhronizacije', 'unverified':'Čeka proveru postojećeg oglasa',
}
PUBLIC_PREFIXES = ('/racunari/', '/telefoni/', '/audio-tv-foto/', '/kucni-aparati/')


def clock():
    return int(time.time())


def read_json(path, default=None, strict=False):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        if strict:
            raise ValueError('Mirror state is unreadable; refusing to risk duplicate ads.') from None
        return default


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)
    path.chmod(0o600)


def state(strict=False):
    value = read_json(ROOT / 'state.json', {'version':1,'bindings':{}}, strict=strict)
    valid=isinstance(value,dict) and value.get('version')==1 and isinstance(value.get('bindings'),dict)
    if valid:
        remote_ids=[]
        for key,binding in value['bindings'].items():
            if (not re.fullmatch(r'[1-9][0-9]*',str(key)) or not isinstance(binding,dict)
                or binding.get('product_id')!=int(key)):
                valid=False;break
            remote=binding.get('remote_id')
            if remote:
                if not re.fullmatch(r'[1-9][0-9]{5,19}',str(remote)):
                    valid=False;break
                remote_ids.append(str(remote))
        valid=valid and len(remote_ids)==len(set(remote_ids))
    if not valid:
        if strict:
            raise ValueError('Invalid mirror state; no external changes attempted.')
        return {'version':1,'bindings':{}}
    return value


def configuration():
    value = read_json(ROOT / 'config.json', {})
    if not isinstance(value,dict):
        value = {}
    return {'enabled':False,'poll_seconds':30,'inspect_seconds':21600,
            'max_fee_rsd':0,'publish_site':True,
            'cdp_url':'http://127.0.0.1:9336',
            'profile_dir':str(store.STATE / 'halo-account-browser'),
            'credentials_path':str(Path.home() / '.secrets/reklik-halooglasi.json'),
            'lock_path':str(store.STATE / 'halo-ui.lock'),
            'display':':21','browser_state_dir':str(store.STATE / 'halo-browser'),
            'category_routes':{},
            **value}


def valid_url(url, remote_id):
    if not isinstance(url,str) or not re.fullmatch(r'[1-9][0-9]{5,19}',str(remote_id or '')):
        return False
    try:
        p = urlsplit(url)
        return (p.scheme == 'https' and p.netloc in ('www.halooglasi.com','halooglasi.com')
                and not p.query and not p.fragment and p.path.startswith(PUBLIC_PREFIXES)
                and p.path.rstrip('/').endswith('/'+str(remote_id))
                and re.fullmatch(r'/[a-zA-Z0-9/_-]+',p.path) is not None)
    except ValueError:
        return False


def visible(product):
    return bool(product and product.get('active') and int(product.get('quantity',0)) > 0)


def fingerprint(product, cfg=None):
    cfg = cfg or store.settings()
    names = ('id','title','brand','category_id','category_slug','category_name','icon',
             'condition','price_cents','description','specs','images')
    facts = {key:product.get(key) for key in names}
    facts['contacts'] = {key:cfg.get(key) for key in ('phone','email','pickup_enabled')}
    return hashlib.sha256(json.dumps(facts,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def public_listing(product):
    if not visible(product):
        return None
    binding = state()['bindings'].get(str(product['id']),{})
    url = binding.get('url')
    if (binding.get('status') != 'active' or not binding.get('owned_verified')
            or not valid_url(url,binding.get('remote_id'))
            or binding.get('fingerprint') != fingerprint(product)):
        return None
    # Halo exposes date-only expiry in its table. That is a renewal hint,
    # not proof that an observed-active ad expired at midnight that morning.
    if (binding.get('expires_at') and binding.get('expires_precision')!='date'
            and binding['expires_at'] <= clock()):
        return None
    return {'url':url,'remote_id':binding['remote_id'],'status':'active'}


def images_fingerprint(product):
    return hashlib.sha256(json.dumps(product.get('images',[]),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def import_existing(legacy_path,observed_path):
    """Import actual owned IDs once; never synthesize or overwrite them."""
    if (ROOT/'state.json').exists():
        raise FileExistsError('Mirror mappings already exist; refusing to reset them.')
    legacy=read_json(legacy_path,strict=True)
    observed=read_json(observed_path,strict=True)
    if not isinstance(legacy,dict) or not isinstance(observed,dict):
        raise ValueError('Existing listing and observed metadata are required.')
    current={p['id']:p for p in store.products(admin=True)}
    cfg=store.settings()
    data={'version':1,'bindings':{},'imported_at':clock()}
    remote_seen=set()
    def digest(value):return hashlib.sha256(value.encode()).hexdigest()
    for key,old in legacy['items'].items():
        ident=int(key);remote=str(old['remote_id'])
        if remote in remote_seen or old.get('id')!=ident or old.get('fee_rsd')!=0:
            raise ValueError('Existing mapping ownership or fee validation failed.')
        remote_seen.add(remote)
        observation=observed['items'].get(str(ident))
        if not observation or str(observation.get('remote_id'))!=remote:
            raise ValueError('Every existing ad needs matching observed ownership.')
        binding={'product_id':ident,'remote_id':remote,'imported_existing':True,
                 'status':'unverified','created_at':old.get('submitted_at',clock())}
        observe(binding,observation)
        product=current.get(ident)
        if product:
            canonical=lambda value:json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))
            same=(old.get('source_title')==product['title'] and old.get('source_brand')==product['brand']
                  and old.get('source_category_id')==product['category_id']
                  and old.get('condition')==product['condition']
                  and Decimal(str(old.get('price_eur','-1')))*100==product['price_cents']
                  and old.get('source_specs_sha256')==digest(canonical(product['specs']))
                  and old.get('source_description_sha256')==digest(product['description'])
                  and old.get('source_images_sha256')==digest(canonical(product['images']))
                  and old.get('phone')==cfg['phone'] and old.get('email')==cfg['email'])
            if same:
                binding.update(fingerprint=fingerprint(product,cfg),images_fingerprint=images_fingerprint(product),
                               last_synced_at=old.get('verified_at',clock()))
        data['bindings'][str(ident)]=binding
    atomic_json(ROOT/'state.json',data)
    state(strict=True)
    return {'imported_existing':len(data['bindings']),'new_ads_created':0}


def item_status(product_id):
    binding = state()['bindings'].get(str(product_id),{})
    status = binding.get('status','pending')
    return {'status':status,'label':LABELS.get(status,'Čeka proveru'),
            'url':binding.get('url') if valid_url(binding.get('url'),binding.get('remote_id')) else None,
            'error':binding.get('message','') if binding.get('message') in LABELS.values() else '',
            'last_synced_at':binding.get('last_synced_at',0),
            'last_checked_at':binding.get('last_checked_at',0),
            'auto_renew':binding.get('auto_renew')}


def request_sync():
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    # A unique value lets the worker avoid losing a newer request during a run.
    marker = ROOT / 'request'
    temporary = ROOT / ('request-'+os.urandom(8).hex()+'.tmp')
    temporary.write_text(str(time.time_ns()))
    temporary.chmod(0o600)
    temporary.replace(marker)
    return True


def clean_description(value):
    """Adapt only the marketplace copy; the inventory remains untouched."""
    text = unescape(re.sub(r'<[^>]*>',' ',str(value)))
    text = re.sub(r'https?://\S+|www\.\S+','',text,flags=re.I)
    text = re.sub(r'\b[\w.+-]+@[\w.-]+\b','',text)
    text = re.sub(r'\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?','',text,flags=re.I)
    text = re.sub(r'(?<!\w)(?:\+381|00381|0[6-9])(?:[\s/()-]*\d){7,10}(?!\d)','',text)
    text = re.sub(r'\b\d+(?:[.,]\d+)*\s*(?:EUR|RSD|din(?:ara)?|€)(?!\w)','',text,flags=re.I)
    # Preserve cosmetic cover defects while omitting battery claims.
    text = re.sub(r'\b(poklop(?:ac|ca|cu))\s+baterije(?:\s+koja\s+je\s+u\s+dobrom\s+stanju)?',r'\1',text,flags=re.I)
    text = re.sub(r'\s+i\s+baterij[aeu]\b','',text,flags=re.I)
    clauses = re.split(r'\n+|(?<!\d),|,(?!\d)|;|(?<=[.!?])\s*(?=[A-ZČĆŽŠĐ])',text)
    result=[]
    for clause in clauses:
        clause = ' '.join(clause.split()).strip(' •,;')
        if not clause or re.search(r'baterij|battery|akumulator',clause,re.I):
            continue
        result.append(clause.rstrip('.')+'.')
    return result


DISCLOSURE = re.compile(r'ogrebot|ošteć|ostec|trag|fle[kč]|tačk|tack|napuk|pukot|ne radi|neisprav|nedostat|bez |samo telefon|švajcar|svajcar|displej|zamen|zamijen|menjan|poprav|hab|ulublj|poklop',re.I)


def payload(product, cfg=None):
    cfg = cfg or store.settings()
    ident = int(product['id'])
    title = ' '.join(product['title'].split())[:50].rstrip()
    clauses = clean_description(product.get('description',''))
    critical = [s for s in clauses if DISCLOSURE.search(s)]
    ordinary = [s for s in clauses if s not in critical]
    ending = '\n\nSlanje širom Srbije. Poštarina se obračunava posebno. Plaćanje pouzećem bez dodatne naknade.'
    if cfg.get('pickup_enabled'):
        ending += ' Lično preuzimanje po dogovoru.'
    ending += '\nOznaka uređaja: RK-'+str(ident)
    header = 'Stanje: '+product['condition']+'.\n\n'
    content=[]
    for clause in critical:
        if len(header+'\n'.join(content+[clause])+ending)>1500:
            raise ValueError('Too many essential disclosures for the Halo description limit.')
        content.append(clause)
    specs=[]
    for key,value in product.get('specs',{}).items():
        if re.search(r'baterij|battery|akumulator',str(key),re.I):
            continue
        specs.extend(clean_description(str(key)+': '+str(value)))
    for clause in ordinary+specs:
        if clause not in content and len(header+'\n'.join(content+[clause])+ending)<=1500:
            content.append(clause)
    description=header+'\n'.join(content)+ending
    photos=[]
    for name in product.get('images',[])[:20]:
        if not re.fullmatch(r'[a-f0-9]{24}\.webp',name):
            raise ValueError('Unexpected inventory photo name.')
        original=store.ORIGINALS/name.replace('.webp','.jpg')
        candidate=original if original.is_file() else store.UPLOADS/name
        if not candidate.is_file():
            raise ValueError('Inventory photo is missing.')
        photos.append(str(candidate))
    return {'id':ident,'sku':'RK-'+str(ident),'title':title,'description':description,
            'condition':product['condition'],'price_eur':format(Decimal(product['price_cents'])/100,'f').rstrip('0').rstrip('.') if product['price_cents']%100 else str(product['price_cents']//100),
            'category_slug':product['category_slug'],'category_name':product['category_name'],
            'category_icon':product.get('icon',''),'brand':product.get('brand',''),
            'photos':photos,'phone':cfg['phone'],'email':cfg['email']}


def fetch_public_catalog():
    response=requests.get('https://reklik.shop/catalog.json',timeout=(6,15),allow_redirects=False)
    response.raise_for_status()
    if len(response.content)>2_000_000:
        raise ValueError('Unexpected public catalog size.')
    value=response.json()
    if value.get('currency')!='EUR' or not isinstance(value.get('products'),list):
        raise ValueError('Unexpected public catalog format.')
    return {int(p['id']):p for p in value['products']}


def published(product, catalog):
    live=catalog.get(int(product['id']))
    return bool(live and int(live.get('quantity',0))>0
        and live.get('title')==product['title'] and live.get('price_minor')==product['price_cents']
        and live.get('condition')==product['condition'] and live.get('images')==product['images'])


def observe(binding, observation):
    remote=str(observation.get('remote_id') or binding.get('remote_id') or '')
    if not re.fullmatch(r'[1-9][0-9]{5,19}',remote):
        raise ValueError('No verified remote listing ID returned.')
    if binding.get('remote_id') and binding['remote_id']!=remote:
        raise ValueError('Existing listing ID changed; refusing recreation.')
    if observation.get('fee_rsd',0)!=0:
        raise ValueError('Nonzero marketplace fee is not authorized.')
    binding['remote_id']=remote
    url=observation.get('url')
    if url is not None:
        if not valid_url(url,remote):
            raise ValueError('Invalid observed public listing URL.')
        binding['url']=url
    status=observation.get('status')
    if status not in ('active','paused','expired','pending_review','missing','stopped','auth_required'):
        raise ValueError('Unexpected marketplace status.')
    binding.update(status='needs_review' if status=='stopped' else status,
                   owned_verified=True,last_checked_at=clock())
    for key in ('auto_renew','expires_at','expires_precision'):
        if key in observation:
            binding[key]=observation[key]
    binding.pop('message',None)
    binding['next_attempt_at']=0


def synchronize(adapter, catalog=None, force=False, current_products=None):
    """One durable reconciliation. All browser work is delegated to the adapter."""
    cfg=store.settings()
    settings=configuration()
    data=state(strict=True)
    before=json.dumps(data['bindings'],sort_keys=True)
    projection=lambda values:{key:{field:value.get(field) for field in ('remote_id','url','status','fingerprint')}
                              for key,value in values.items()}
    before_public=projection(data['bindings'])
    data['connection_status']='ready'
    products=current_products if current_products is not None else {p['id']:p for p in store.products(admin=True)}
    current=lambda ident:store.product(ident,admin=True) if current_products is None else current_products.get(ident)
    if catalog is None:
        try:catalog=fetch_public_catalog()
        except Exception:catalog={}
    bindings=data['bindings']
    ids=set(products)|{int(k) for k in bindings}
    # Withdraw unavailable items before changing or creating any other ad.
    ordered=sorted(ids,key=lambda pid:(visible(products.get(pid)),pid))
    for ident in ordered:
        product=products.get(ident)
        binding=bindings.get(str(ident))
        recovering=bool(binding and (binding.get('create_attempt_started') or binding.get('status') in ('creating','uncertain')))
        if not visible(product) and not (binding and binding.get('remote_id')) and not recovering:
            continue
        if binding and binding.get('next_attempt_at',0)>clock() and not force:
            continue
        if not binding:
            binding={'product_id':ident,'status':'pending','created_at':clock()}
            bindings[str(ident)]=binding
        wanted=fingerprint(product,cfg) if product else None
        try:
            remote=binding.get('remote_id')
            if remote:
                if not visible(product):
                    if (binding.get('status') not in ('paused','expired','missing')
                        or binding.get('auto_renew') is not False or force
                        or binding.get('last_checked_at',0)<clock()-int(settings['inspect_seconds'])):
                        observe(binding,adapter.deactivate(remote,binding.get('url')))
                    binding['desired']='hidden'
                    continue
                if (force or binding.get('status')!='active' or binding.get('fingerprint')!=wanted
                    or binding.get('last_checked_at',0)<clock()-int(settings['inspect_seconds'])):
                    observe(binding,adapter.inspect(remote,binding.get('url')))
                if binding['status'] in ('missing','needs_review','auth_required'):
                    binding['message']=LABELS.get(binding['status'],LABELS['needs_review'])
                    binding['next_attempt_at']=clock()+900
                    continue
                if not published(product,catalog):
                    binding['next_attempt_at']=clock()+30
                    continue
                fresh=current(ident)
                if not visible(fresh) or fingerprint(fresh,cfg)!=wanted:
                    if not visible(fresh):observe(binding,adapter.deactivate(remote,binding.get('url')))
                    continue
                # Existing and expired IDs are reused forever; never call create here.
                if binding.get('fingerprint')!=wanted:
                    fresh=current(ident)
                    if not visible(fresh) or fingerprint(fresh,cfg)!=wanted:
                        continue
                    prepared=payload(product,cfg)
                    prepared['replace_photos']=binding.get('images_fingerprint')!=images_fingerprint(product)
                    observe(binding,adapter.update(remote,prepared))
                    binding['fingerprint']=wanted
                    binding['images_fingerprint']=images_fingerprint(product)
                    binding['last_synced_at']=clock()
                if binding['status']=='paused':
                    observe(binding,adapter.activate(remote))
                if binding['status']=='expired' or binding.get('auto_renew') is not True:
                    observe(binding,adapter.ensure_renewal(remote))
                binding['desired']='visible'
                fresh=current(ident)
                if not visible(fresh) or fingerprint(fresh,cfg)!=wanted:
                    observe(binding,adapter.deactivate(remote,binding.get('url')))
            else:
                if binding.get('create_attempt_started') or binding.get('status') in ('creating','uncertain'):
                    # An interrupted external POST must not result in a second ad.
                    finder=getattr(adapter,'find_existing',None)
                    submitted=binding.get('submitted_payload')
                    if not submitted and product:submitted=payload(product,cfg)
                    found=finder(submitted) if finder and submitted else None
                    if found:
                        found_remote=str(found.get('remote_id',''))
                        if any(other is not binding and other.get('remote_id')==found_remote for other in bindings.values()):
                            raise ValueError('Recovered ad belongs to another product mapping.')
                        observe(binding,found)
                        # The recovered ad contains the submitted snapshot, which
                        # may differ from edits made while its response was lost.
                        binding.update(fingerprint=binding.get('submitted_fingerprint'),
                                       images_fingerprint=binding.get('submitted_images_fingerprint'),
                                       last_synced_at=clock())
                        if not visible(current(ident)):
                            observe(binding,adapter.deactivate(binding['remote_id'],binding.get('url')))
                    else:
                        binding['status']='uncertain'
                        binding['message']=LABELS['uncertain']
                    continue
                if not published(product,catalog):
                    binding['status']='waiting_public'
                    continue
                prepared=payload(product,cfg)
                fresh=current(ident)
                if not visible(fresh) or fingerprint(fresh,cfg)!=wanted:
                    continue
                creator=adapter.create  # Establish the authenticated session before checkpointing publication.
                binding.update(status='creating',submitted_at=clock(),create_attempt_started=True,
                               submitted_payload=prepared,submitted_fingerprint=wanted,
                               submitted_images_fingerprint=images_fingerprint(product))
                atomic_json(ROOT/'state.json',data)
                try:
                    observation=creator(prepared)
                except Exception as error:
                    # The adapter guarantees these typed holds precede submission;
                    # every failure after clicking Publish is SubmissionUncertain.
                    error_name=type(error).__name__.lower()
                    if any(kind in error_name for kind in ('auth','fee','needsreview')):
                        binding['create_attempt_started']=False
                    raise
                new_remote=str(observation.get('remote_id',''))
                if any(other is not binding and other.get('remote_id')==new_remote for other in bindings.values()):
                    raise ValueError('Remote ad is already mapped to another product.')
                observe(binding,observation)
                binding.update(fingerprint=wanted,images_fingerprint=images_fingerprint(product),last_synced_at=clock(),desired='visible')
                fresh=current(ident)
                if not visible(fresh) or fingerprint(fresh,cfg)!=wanted:
                    observe(binding,adapter.deactivate(binding['remote_id'],binding.get('url')))
                elif binding.get('auto_renew') is not True:
                    observe(binding,adapter.ensure_renewal(binding['remote_id']))
        except Exception as error:
            name=type(error).__name__.lower()
            if 'auth' in name:status='auth_required'
            elif 'fee' in name:status='fee_required'
            elif 'uncertain' in name or binding.get('status')=='creating':status='uncertain'
            elif 'review' in name or isinstance(error,ValueError):status='needs_review'
            else:status='error'
            binding.update(status=status,message=LABELS[status],last_error_type=type(error).__name__,
                           next_attempt_at=clock()+(300 if status in ('error','auth_required') else 900))
            if status=='auth_required':
                data['connection_status']='auth_required'
                break
        finally:
            atomic_json(ROOT/'state.json',data)
    data['last_run_at']=clock()
    atomic_json(ROOT/'state.json',data)
    return {'changed':before!=json.dumps(bindings,sort_keys=True),
            'publish_changed':before_public!=projection(bindings),'bindings':len(bindings),
            'active':sum(b.get('status')=='active' for b in bindings.values())}


class LazyAdapter:
    """Unchanged inventory does not open a browser or touch the marketplace."""
    def __init__(self,factory):
        self.factory=factory
        self.stack=ExitStack()
        self.adapter=None
        self.failure=None

    def __enter__(self):
        return self

    def __exit__(self,*args):
        return self.stack.__exit__(*args)

    def __getattr__(self,name):
        if self.failure is not None:
            raise self.failure
        if self.adapter is None:
            try:self.adapter=self.stack.enter_context(self.factory())
            except Exception as error:
                self.failure=error
                raise
        return getattr(self.adapter,name)


def run_once(force=False):
    settings=configuration()
    if settings.get('enabled') is not True:
        return {'status':'disabled'}
    if settings.get('max_fee_rsd')!=0:
        raise ValueError('Only zero-fee mirroring is enabled.')
    ROOT.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not (ROOT/'state.json').is_file():
        raise ValueError('Existing listing mappings must be initialized before enabling mirroring.')
    with (ROOT/'worker.lock').open('a') as lock:
        fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        from halo_browser import HaloBrowser
        existing=state(strict=True)
        known=[b['remote_id'] for b in existing['bindings'].values() if b.get('remote_id')]
        factory=lambda:HaloBrowser(cdp_url=settings['cdp_url'],profile_dir=Path(settings['profile_dir']),
                         credentials_path=Path(settings['credentials_path']),
                         lock_path=Path(settings['lock_path']),owned_ids=known,
                         display=settings['display'],state_dir=Path(settings['browser_state_dir']),
                         category_routes=settings['category_routes'])
        with LazyAdapter(factory) as adapter:
            result=synchronize(adapter,force=force)
        if result['publish_changed'] and settings['publish_site']:
            import publisher
            publisher.publish()
        return result


def serve():
    last=0
    while True:
        marker=ROOT/'request'
        requested=marker.read_text() if marker.is_file() else None
        if requested or clock()-last>=max(15,int(configuration()['poll_seconds'])):
            try:
                result=run_once(force=bool(requested))
                atomic_json(ROOT/'last-result.json',{'at':clock(),**result})
                if requested and marker.is_file() and marker.read_text()==requested:
                    marker.unlink()
            except BlockingIOError:
                pass
            except Exception as error:
                atomic_json(ROOT/'last-result.json',{'at':clock(),'status':'error','error_type':type(error).__name__})
            last=clock()
        time.sleep(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--serve',action='store_true')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--force',action='store_true')
    parser.add_argument('--import-existing',type=Path)
    parser.add_argument('--observed',type=Path)
    args=parser.parse_args()
    if args.import_existing:
        if not args.observed:parser.error('--observed is required with --import-existing')
        print(json.dumps(import_existing(args.import_existing,args.observed)))
    elif args.serve:serve()
    else:print(json.dumps(run_once(force=args.force)))
