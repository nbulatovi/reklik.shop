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
    key=str(key).strip()
    return LABELS.get(fold(key),key[:1].upper()+key[1:])


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
