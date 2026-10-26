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
    subject=str(data.get('subject') or raw.get('Subject') or '(bez naslova)')[:250]
    if sender==owner and not re.search(r'reklik|google.?ads',subject,re.I):return None
    text=str(data.get('text',''))
    if not text and raw:
        part=raw.get_body(preferencelist=('plain',)) if raw.is_multipart() else raw
        if part and part.get_content_type()=='text/plain':
            try:text=part.get_content()
            except (LookupError,UnicodeError):pass
    if not text:text=re.sub(r'<[^>]+>',' ',str(data.get('html','')))
    message_id=str(raw.get('Message-ID',''))
    identity=hashlib.sha256((sender+'|'+message_id+'|'+subject+'|'+text).encode()).hexdigest()
    root=Path(state or os.environ.get('REKLIK_STATE',str(Path.home()/'.local/state/reklik')))/'correspondence'
    incoming=root/'incoming';incoming.mkdir(parents=True,exist_ok=True,mode=0o700)
    target=incoming/(identity+'.json')
    if target.exists():return identity
    payload={'id':identity,'from':sender,'to':recipient,'subject':subject,'text':redact(text)[:18000],
             'message_id':message_id,'received_at':int(time.time()),'dry_run':bool(data.get('reklik_dry_run'))}
    temporary=incoming/(identity+'.tmp');temporary.write_text(json.dumps(payload,ensure_ascii=False));temporary.chmod(0o600);temporary.replace(target)
    return identity
