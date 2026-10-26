"""Shared presentation and data rules for refabrikovani inventory."""
import re
import unicodedata

GRADES=(
    'Novo',
    'Refabrikovano - odlično stanje (Grade A)',
    'Refabrikovano - odlično stanje sa minimalnim tragovima (Grade A-)',
    'Refabrikovano - standardni tragovi upotrebe, par ogrebotina (Grade B)',
    'Refabrikovano - vidljivi tragovi upotrebe (Grade B)',
    'Refabrikovano - značajno korišćen uređaj (Grade C)',
)


def fold(text):
    cyr=dict(zip('абвгдђежзијклљмнњопрстћуфхцчџш',
        ['a','b','v','g','d','dj','e','z','z','i','j','k','l','lj','m','n','nj','o','p','r','s','t','c','u','f','h','c','c','dz','s']))
    text=''.join(cyr.get(ch,ch) for ch in str(text).lower()).replace('đ','dj')
    return unicodedata.normalize('NFKD',text).encode('ascii','ignore').decode()


def grade(previous,description=''):
    if previous in GRADES:return previous
    old=fold(previous);text=old+' '+fold(description)
    if old.strip()=='novo':return GRADES[0]
    if 'grade c' in old or 'znacajno korisc' in text:return GRADES[5]
    minor=any(x in text for x in ('minimaln','sitni trag','sitnim trag','sitni tragovi','neznatn'))
    scratches=any(x in text for x in ('ogrebot','ostecenj'))
    if 'novo' in old and (minor or scratches or 'trag' in old):return GRADES[2]
    if 'vidljiv' in text or ('ima tragova upotrebe' in text and not minor):return GRADES[4]
    if scratches:return GRADES[3]
    if 'veoma dobro' in old or 'veoma dobrom' in old:return GRADES[2]
    if 'odlic' in old:return GRADES[1]
    if 'grade b' in old or 'b grade' in old:return GRADES[3]
    return GRADES[4]


LABELS={
 'cpu':'Procesor','procesor':'Procesor','processor':'Procesor',
 'ram':'RAM','memorija':'RAM','ram memorija':'RAM','radna memorija':'RAM',
 'ssd':'SSD','hdd':'HDD','disk':'Skladište','skladiste':'Skladište','storage':'Skladište',
 'gpu':'Grafika','grafika':'Grafika','graficka kartica':'Grafika',
 'ekran':'Ekran','display':'Ekran','screen':'Ekran','rezolucija':'Rezolucija',
 'os':'Operativni sistem','operativni sistem':'Operativni sistem','operating system':'Operativni sistem',
}


def label(key):
    key=re.sub(r'^[\s•●▪*–—-]+','',str(key)).strip()
    aliases={'procesor (cpu)':'Procesor','grafika (gpu)':'Grafika','radna memorija (ram)':'RAM','interna memorija (ssd)':'SSD','interna memorija':'Skladište'}
    return aliases.get(fold(key),LABELS.get(fold(key),key[:1].upper()+key[1:]))


def family(key):
    key=label(key)
    return 'storage' if key in ('SSD','HDD','Skladište') else fold(key)


def normalize_specs(specs):
    result={}
    for key,value in specs.items():
        name=label(key);value=str(value).strip()
        if not name or not value:continue
        # Preserve both legacy entries if two aliases contain different values.
        if name in result:
            if result[name]==value:continue
            raw=str(key).strip();name=raw if raw not in result else label(key)
            original=name;number=2
            while name in result:
                name=original+' ('+str(number)+')';number+=1
        result[name]=value
    order={'Procesor':0,'RAM':1,'SSD':2,'HDD':3,'Skladište':4,'Grafika':5,'Ekran':6,'Rezolucija':7,'Operativni sistem':8}
    return dict(sorted(result.items(),key=lambda pair:(order.get(pair[0],20),fold(pair[0]))))


def merge_missing(manual,extra):
    result=normalize_specs(manual);occupied={family(k) for k in result}
    for key,value in normalize_specs(extra).items():
        if family(key) not in occupied:
            result[key]=value;occupied.add(family(key))
    return normalize_specs(result)


def parse_specs(text):
    specs={};notes=0
    for line in str(text).splitlines():
        if not line.strip():continue
        if ':' in line:key,value=line.split(':',1)
        else:
            notes+=1;key,value='Dodatno '+str(notes),line
        key=label(re.sub(r'^\s*\d+[.)]\s*','',key));value=value.strip()
        if key in specs and specs[key]!=value:
            original=key;number=2
            while key in specs:
                key=original+' ('+str(number)+')';number+=1
        if key and value:specs[key]=value
    return normalize_specs(specs)


def terminology(text):
    forms={'obnovljenih':'refabrikovanih','obnovljenim':'refabrikovanim','obnovljenog':'refabrikovanog',
           'obnovljene':'refabrikovane','obnovljeni':'refabrikovani','obnovljeno':'refabrikovano',
           'obnovljena':'refabrikovana','obnovljen':'refabrikovan','refurbished':'refabrikovano','ferubished':'refabrikovano'}
    def replace(match):
        original=match.group();new=forms[original.lower()]
        return new.upper() if original.isupper() else new.capitalize() if original[0].isupper() else new
    return re.sub(r'\b(?:'+ '|'.join(forms)+r')\b',replace,str(text),flags=re.I)


def description_specs(description):
    """Extract explicit owner facts only; never infer a device configuration."""
    found={}
    for line in str(description).splitlines():
        if ':' not in line:continue
        key,value=line.split(':',1);key=label(key)
        if key in ('Procesor','RAM','SSD','HDD','Skladište','Ekran','Operativni sistem') and value.strip():found[key]=value.strip()
    text=fold(description)
    ram=re.search(r'(\d+(?:[.,]\d+)?)\s*gb\s*(?:ram(?:\s+memorije?)?|radne\s+memorije)',text)
    if not ram:ram=re.search(r'(?:ram|radna memorija)\s*(?:je|kapaciteta|od|:)?\s*(\d+(?:[.,]\d+)?)\s*gb',text)
    if ram:found.setdefault('RAM',ram[1].replace('.',',')+' GB')
    storage=re.search(r'(\d+(?:[.,]\d+)?)\s*(gb|tb)\s*(?:skladisnog prostora|skladisni prostor)',text)
    if not storage:storage=re.search(r'(?:skladisni prostor|(?:m[.]?2\s+)?disk)\s*(?:je\s*)?(?:kapaciteta\s*)?(\d+(?:[.,]\d+)?)\s*(gb|tb)',text)
    if storage:found.setdefault('Skladište',storage[1].replace('.',',')+' '+storage[2].upper())
    system=re.search(r'\b(android\s+\d+(?:\.\d+)*|windows\s+(?:10|11)(?:\s+pro)?)\b',text)
    if system:found.setdefault('Operativni sistem',system[1].title().replace(' Pro',' Pro'))
    screen=re.search(r'(\d+(?:[.,]\d+)?)\s*[- ]?(?:inca|incni)',text)
    if screen:found.setdefault('Ekran',screen[1].replace('.',',')+' inča'+(', Full HD' if 'full hd' in text else ''))
    cpu=re.search(r'\bi([3579])[-\s]*(\d{1,2})[.\s-]*(?:gen|generacij)',text)
    if cpu:found.setdefault('Procesor','Intel Core i'+cpu[1]+', '+cpu[2]+'. generacija')
    return normalize_specs(found)
