"""Narrow incoming-mail bridge for the owner's authorized ReKlik correspondence."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
from email import policy
from email.parser import Parser
from email.utils import parseaddr
from email.header import decode_header, make_header


def save_ads_key(message,config,root):
    """Accept only the specific service-account JSON authorized by the owner."""
    if not config.get('allow_keys_by_email'):return False
    expected=config.get('ads_setup',{})
    for part in message.walk():
        if part.is_multipart() or not str(part.get_filename() or '').lower().endswith('.json'):continue
        try:
            data=part.get_payload(decode=True)
            if not data or len(data)>30000:continue
            key=json.loads(data)
            if key.get('type')!='service_account' or key.get('project_id')!=expected.get('project_id') or key.get('client_email')!=expected.get('service_account'):continue
            from cryptography.hazmat.primitives.serialization import load_pem_private_key
            from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
            if not isinstance(load_pem_private_key(key['private_key'].encode(),password=None),RSAPrivateKey):continue
            if key.get('token_uri')!='https://oauth2.googleapis.com/token':continue
            folder=root/'credentials';folder.mkdir(parents=True,exist_ok=True,mode=0o700)
            target=folder/'google-ads-service-account.json'
            if target.exists():
                return json.loads(target.read_text()).get('private_key_id')==key.get('private_key_id')
            temporary=folder/'google-ads-service-account.tmp'
            temporary.write_text(json.dumps(key));temporary.chmod(0o600);temporary.replace(target)
            return True
        except (ValueError,TypeError,KeyError,UnicodeError):continue
    return False

def settings(state=None):
    root=Path(state or os.environ.get('REKLIK_STATE',str(Path.home()/'.local/state/reklik')))/'correspondence'
    try:return json.loads((root/'settings.json').read_text())
    except (OSError,ValueError):return {}



def redact(text):
    text=re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','[privatni ključ izostavljen]',str(text),flags=re.S)
    text=re.sub(r'(?im)^([^\n]*(?:password|lozinka|private_key|client_secret|refresh_token|api[_ ]?key|access_token)["\s]*[:=]\s*).+$',r'\1[privatni podatak izostavljen]',text)
    text=re.sub(r'\b(?:sk-|re_)[A-Za-z0-9_-]{18,}\b','[ključ izostavljen]',text)
    def card(match):
        digits=re.sub(r'\D','',match.group())
        if not 13<=len(digits)<=19:return match.group()
        total=sum((int(n)*2//10+int(n)*2%10) if i%2 else int(n) for i,n in enumerate(reversed(digits)))
        return '[broj kartice izostavljen]' if total%10==0 else match.group()
    return re.sub(r'(?<!\d)(?:\d[ -]?){13,19}(?!\d)',card,text)


def capture(data,state=None):
    config=settings(state)
    if not config.get('enabled'):return None
    helper=config.get('correspondent','').lower();owner=config.get('owner','').lower()
    sender=parseaddr(str(data.get('from','')))[1].lower()
    if sender not in (helper,owner):return None
    recipient=parseaddr(str(data.get('to','')))[1].lower()
    if recipient!=config.get('mailbox','').lower():return None
    raw=Parser(policy=policy.default).parsestr(str(data.get('raw','')))
    if raw.get('Auto-Submitted','no').lower()!='no' or raw.get('Precedence','').lower() in ('bulk','list','junk'):return None
    subject=str(make_header(decode_header(str(raw.get('Subject') or data.get('subject') or '(bez naslova)'))))[:250]
    if sender==owner and not re.search(r'reklik|google.?ads',subject,re.I):return None
    text=''
    if raw:
        part=raw.get_body(preferencelist=('plain',)) if raw.is_multipart() else raw
        if part and part.get_content_type()=='text/plain':
            try:text=part.get_content()
            except (LookupError,UnicodeError):pass
    if not text:text=str(data.get('text',''))
    if not text:text=re.sub(r'<[^>]+>',' ',str(data.get('html','')))
    message_id=str(raw.get('Message-ID',''))
    identity=hashlib.sha256((sender+'|'+message_id+'|'+subject+'|'+text).encode()).hexdigest()
    root=Path(state or os.environ.get('REKLIK_STATE',str(Path.home()/'.local/state/reklik')))/'correspondence'
    # Keep the authorized original before the normal mailbox delivery strips MIME
    # parts. This also retains billing documents without exposing them to the AI.
    original=str(data.get('raw',''))
    if original:
        originals=root/'originals';originals.mkdir(parents=True,exist_ok=True,mode=0o700)
        archive=originals/(identity+'.eml')
        if not archive.exists():
            temporary=originals/(identity+'.tmp')
            temporary.write_text(original);temporary.chmod(0o600);temporary.replace(archive)
    credentials_saved=save_ads_key(raw,config,root)
    incoming=root/'incoming';incoming.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=incoming/(identity+'.json')
    if target.exists():return identity
    payload={'id':identity,'from':sender,'to':recipient,'subject':subject,'text':redact(text)[:18000],
             'message_id':message_id,'received_at':int(time.time()),'dry_run':bool(data.get('reklik_dry_run')),'credentials_saved':credentials_saved,
             'original_saved':bool(original),'attachment_count':sum(1 for part in raw.walk() if part.get_filename())}
    temporary=incoming/(identity+'.tmp');temporary.write_text(json.dumps(payload,ensure_ascii=False));temporary.chmod(0o600);temporary.replace(target)
    return identity
