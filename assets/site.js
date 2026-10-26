/* ReKlik: progressively enhanced HTML. No ads or third-party analytics scripts. */
(() => {
  'use strict';
  const base = document.body.dataset.base || '';
  const live = document.body.dataset.live === '1';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const readCart = () => { try { const x = JSON.parse(localStorage.getItem('reklik-cart') || '[]'); return Array.isArray(x) ? x.slice(0, 30) : []; } catch { return []; } };
  const countCart = () => $$('[data-cart-count]').forEach(x => { x.textContent = readCart().reduce((n, v) => n + (Number(v.qty) || 0), 0); });
  const saveCart = items => { try { localStorage.setItem('reklik-cart', JSON.stringify(items)); } catch { /* Shopping still works with Buy now. */ } countCart(); };
  const money = (minor, currency) => new Intl.NumberFormat('sr-Latn-RS', {style: 'currency', currency, minimumFractionDigits: minor % 100 === 0 ? 0 : 2, maximumFractionDigits: 2}).format(minor / 100);
  const node = (tag, attributes = {}, text = '') => { const e = document.createElement(tag); Object.entries(attributes).forEach(([k,v]) => e.setAttribute(k, v)); e.textContent = text; return e; };
  countCart();
  window.addEventListener('storage', countCart);

  const viewedProduct = $('[data-product-view]');
  if (viewedProduct) {
    const record = origin => fetch(origin+'/views/product/'+encodeURIComponent(viewedProduct.dataset.productView),{mode:'no-cors',credentials:'omit',cache:'no-store',keepalive:true}).catch(()=>{});
    if (live) record(location.origin);
    else fetch(base+'/backend.json?t='+Date.now(),{cache:'no-store'}).then(r=>r.json()).then(data=>{
      const backend=new URL(data.backend);
      if(backend.protocol==='https:' && /^[a-z0-9-]+\.trycloudflare\.com$/.test(backend.hostname))record(backend.origin);
    }).catch(()=>{});
  }

  $$('[data-add-cart]').forEach(button => button.addEventListener('click', () => {
    const id = Number(button.dataset.addCart), warranty = Number($('#warranty')?.value || 0), items = readCart();
    const item = items.find(x => x.id === id && x.warranty === warranty);
    if (item) item.qty = Math.min(20, item.qty + 1); else items.push({id, warranty, qty: 1});
    saveCart(items);
    const message = $('[data-cart-feedback]'); if (message) message.textContent = 'Dodato u korpu. Možete nastaviti kupovinu.';
  }));
  $$('[data-thumbnail]').forEach(button => button.addEventListener('click', () => {
    const img = $('[data-main-image]'); if (!img) return;
    img.src = button.dataset.thumbnail; img.parentElement.href = button.dataset.thumbnail;
  }));

  const gallery=$('[data-gallery]');
  if(gallery){
    let urls=[],full=[];try{urls=JSON.parse(gallery.dataset.gallery);full=JSON.parse(gallery.dataset.galleryFull||gallery.dataset.gallery);}catch{}
    let position=0;const dialog=$('[data-photo-zoom]'),zoom=$('[data-zoom-image]'),scroller=$('[data-zoom-scroll]');
    const show=index=>{
      if(!urls.length)return;position=(index+urls.length)%urls.length;
      const src=full[position]||urls[position];$('[data-gallery-image]',gallery).src=urls[position];
      $('[data-gallery-image]',gallery).alt=document.querySelector('h1').textContent+' — fotografija '+(position+1);
      $('[data-gallery-position]',gallery).textContent=(position+1)+' / '+urls.length;
      $('[data-open-zoom]',gallery).href=src;
      $$('[data-gallery-go]',gallery).forEach(b=>b.setAttribute('aria-pressed',String(Number(b.dataset.galleryGo)===position)));
      if(dialog?.open){zoom.src=src;$('[data-zoom-original]').href=src;$('[data-zoom-position]').textContent=(position+1)+' / '+urls.length;scroller.scrollTo(0,0);}
    };
    $('[data-gallery-prev]',gallery)?.addEventListener('click',()=>show(position-1));
    $('[data-gallery-next]',gallery)?.addEventListener('click',()=>show(position+1));
    $$('[data-gallery-go]',gallery).forEach(b=>b.addEventListener('click',()=>show(Number(b.dataset.galleryGo))));
    const arrows=e=>{if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();show(position+(e.key==='ArrowLeft'?-1:1));}};
    gallery.addEventListener('keydown',arrows);dialog?.addEventListener('keydown',arrows);
    $('[data-open-zoom]',gallery)?.addEventListener('click',event=>{if(!dialog?.showModal||event.ctrlKey||event.metaKey||event.shiftKey)return;event.preventDefault();dialog.showModal();document.documentElement.classList.add('photo-zoom-open');show(position);});
    $('[data-close-zoom]')?.addEventListener('click',()=>dialog.close());
    dialog?.addEventListener('close',()=>document.documentElement.classList.remove('photo-zoom-open'));
    $('[data-zoom-prev]')?.addEventListener('click',()=>show(position-1));
    $('[data-zoom-next]')?.addEventListener('click',()=>show(position+1));
    const toggleSize=()=>{const original=scroller.classList.toggle('original-size');const button=$('[data-zoom-size]');button.textContent=original?'Uklopi u ekran':'Prikaži 100%';button.setAttribute('aria-pressed',String(original));};
    $('[data-zoom-size]')?.addEventListener('click',toggleSize);zoom?.addEventListener('click',toggleSize);
    let start;gallery.addEventListener('touchstart',e=>{start=[e.touches[0].clientX,e.touches[0].clientY];},{passive:true});
    gallery.addEventListener('touchend',e=>{if(!start)return;const x=e.changedTouches[0].clientX-start[0],y=e.changedTouches[0].clientY-start[1];if(Math.abs(x)>50&&Math.abs(x)>Math.abs(y)*1.5)show(position+(x>0?-1:1));start=null;},{passive:true});
  }

  const fold = value => { const table = {'а':'a','б':'b','в':'v','г':'g','д':'d','ђ':'dj','е':'e','ж':'z','з':'z','и':'i','ј':'j','к':'k','л':'l','љ':'lj','м':'m','н':'n','њ':'nj','о':'o','п':'p','р':'r','с':'s','т':'t','ћ':'c','у':'u','ф':'f','х':'h','ц':'c','ч':'c','џ':'dz','ш':'s','đ':'dj'}; return [...String(value).toLowerCase()].map(c=>table[c]||c).join('').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/sh/g,'s').replace(/ch/g,'c').replace(/zh/g,'z'); };
  const search = $('[data-catalog-search]'), grid = $('[data-product-grid]');
  if (grid) {
    const cards = $$('.product-card', grid);
    const brand = $('[data-brand-filter]'), min = $('[data-price-min]'), max = $('[data-price-max]');
    const panel = $('[data-filter-panel]'), desktop = window.matchMedia('(min-width: 901px)');
    const layoutFilters = () => { if (panel) panel.open = desktop.matches; };
    layoutFilters(); desktop.addEventListener('change',layoutFilters);
    const filter = () => {
      const query = fold(search?.value || '').trim();
      const tokens = query.split(/\s+/).filter(Boolean);
      const floor = min?.value ? Number(min.value) : 0, ceiling = max?.value ? Number(max.value) : Infinity;
      cards.forEach(c => { const price = Number(c.dataset.price); c.hidden = !tokens.every(t => fold(c.dataset.search).includes(t)) || (brand?.value && c.dataset.brand !== brand.value) || price < floor || price > ceiling; });
      const n = cards.filter(c => !c.hidden).length;
      if ($('[data-results-count]')) $('[data-results-count]').textContent = n;
      if ($('[data-empty]')) $('[data-empty]').hidden = n > 0;
      const active = [brand?.value,min?.value,max?.value].filter(Boolean).length;
      if ($('[data-filter-count]')) $('[data-filter-count]').textContent = active ? '('+active+')' : '';
    };
    if (search) {
      search.value = new URLSearchParams(location.search).get('q') || search.value;
      search.addEventListener('input', filter);
      search.form?.addEventListener('submit',event=>{event.preventDefault();filter();grid.scrollIntoView({block:'start',behavior:'smooth'});});
    }
    [brand,min,max].filter(Boolean).forEach(input=>input.addEventListener(input.tagName === 'SELECT' ? 'change' : 'input',filter));
    $$('[data-reset-filters]').forEach(button=>button.addEventListener('click',()=>{if(search)search.value='';if(brand)brand.value='';if(min)min.value='';if(max)max.value='';filter();}));
    $('[data-sort]')?.addEventListener('change', e => {
      const mode = e.target.value;
      [...cards].sort((a,b) => mode === 'low' ? Number(a.dataset.price)-Number(b.dataset.price) : mode === 'high' ? Number(b.dataset.price)-Number(a.dataset.price) : mode === 'name' ? a.dataset.search.localeCompare(b.dataset.search) : cards.indexOf(a)-cards.indexOf(b)).forEach(c => grid.append(c));
    });
    filter();
  }

  $('[data-category-select]')?.addEventListener('change',event=>{const target=new URL(event.target.value,location.origin);if(target.origin===location.origin)location.assign(target.href);});

  if ($('[data-cart-items]')) {
    fetch(base + '/catalog.json', {cache: 'no-store'}).then(r => { if (!r.ok) throw Error(); return r.json(); }).then(data => {
      const render = () => {
        const before=readCart(),items=before.filter(line=>data.products.some(p=>p.id===Number(line.id)));
        if(items.length!==before.length)saveCart(items);
        const host = $('[data-cart-items]'); host.replaceChildren(); let total = 0;
        items.forEach((line, index) => {
          const p = data.products.find(p => p.id === Number(line.id)); if (!p) return;
          const row = node('article', {class: 'cart-item'}), img = node('img', {src: p.images.length ? base + '/media/' + p.images[0] : p.catalog_image ? base+'/assets/'+p.catalog_image : base + '/assets/favicon.svg', alt: p.title, width: '90', height: '80'});
          const details = node('div'), title = node('h3'); title.append(node('a', {href: base + '/proizvod/' + p.slug + '/'}, p.title)); details.append(title);
          const qty = node('input', {type: 'number', min: '1', max: String(Math.max(1, Math.min(20,p.quantity))), value: String(line.qty), 'aria-label': 'Količina za ' + p.title});
          qty.addEventListener('change', () => { const next = readCart(); next[index].qty = Math.max(1, Math.min(20,Number(qty.value)||1)); saveCart(next); render(); });
          const choices = node('select', {'aria-label': 'Dodatna garancija za ' + p.title});
          for (let n=0;n<=3;n++) { const option = node('option', {value: String(n)}, n ? '+' + n + ' mes. — ' + money(data.warranty_minor[n], data.currency) : (data.included_warranty_days||14)+' dana — uključeno u cenu'); option.selected = Number(line.warranty) === n; choices.append(option); }
          choices.addEventListener('change', () => { const next = readCart(); next[index].warranty = Number(choices.value); saveCart(next); render(); });
          details.append(qty, document.createTextNode(' '), choices);
          const actions = node('div'); const amount = (p.price_minor + (data.warranty_minor[line.warranty] || 0)) * line.qty; total += amount;
          actions.append(node('strong', {}, money(amount, data.currency)), node('br'));
          const remove = node('button', {type: 'button'}, 'Ukloni'); remove.addEventListener('click', () => { saveCart(readCart().filter((_, i) => i !== index)); render(); }); actions.append(remove);
          if (p.quantity < line.qty) details.append(node('p', {class:'settings-note'}, 'Raspoloživa količina je promenjena. Proverite uređaj pre kupovine.'));
          row.append(img, details, actions); host.append(row);
        });
        const has = host.children.length > 0; $('[data-cart-empty]').hidden = has; $('[data-cart-totals]').hidden = !has;
        $('[data-cart-total]').textContent = money(total, data.currency);
        $('[data-cart-checkout]').href = base + '/checkout/?items=' + encodeURIComponent(JSON.stringify(items));
      }; render();
    }).catch(() => { $('[data-cart-empty] p').textContent = 'Katalog trenutno nije dostupan. Osvežite stranicu ili koristite Kupi sada uz uređaj.'; });
  }

  const frameHost = $('[data-frame-kind]');
  if (frameHost) {
    fetch(base + '/backend.json?t=' + Date.now(), {cache: 'no-store'}).then(r => {if (!r.ok) throw Error(); return r.json();}).then(data => {
      const backend = new URL(data.backend);
      if (backend.protocol !== 'https:' || !/^[a-z0-9-]+\.trycloudflare\.com$/.test(backend.hostname)) throw Error();
      const kind = frameHost.dataset.frameKind, params = new URLSearchParams(location.search), target = new URL(backend.origin);
      if (kind === 'admin') { target.pathname = '/admin/'; ['tab','edit','copy'].forEach(k => {if (params.has(k)) target.searchParams.set(k,params.get(k));}); }
      else if (kind === 'confirmation') { const token = params.get('order') || ''; if (!/^[A-Za-z0-9_-]{30,90}$/.test(token)) throw Error(); target.pathname = '/order/' + token + '/'; target.searchParams.set('return','1'); }
      else { target.pathname = '/checkout/'; ['buy','qty','warranty','items'].forEach(k => {if (params.has(k)) target.searchParams.set(k,params.get(k));}); }
      const frame = $('iframe', frameHost), fallback = $('.frame-fallback', frameHost), loading = $('.frame-loading', frameHost);
      $('[data-direct-frame]').href = target.href; frame.src = target.href; frame.hidden = false;
      frame.addEventListener('load', () => { loading.hidden = true; fallback.hidden = false; });
      setTimeout(() => { fallback.hidden = false; }, 8000);
      window.addEventListener('message', e => {
        if (e.origin !== backend.origin || e.source !== frame.contentWindow) return;
        if (e.data?.type === 'reklik-height') frame.style.height = Math.min(25000,Math.max(500,Number(e.data.height)||900)) + 'px';
        if (e.data?.type === 'reklik-order' && /^[A-Za-z0-9_-]{30,90}$/.test(e.data.token)) {
          history.replaceState({}, '', base + '/potvrda/?order=' + encodeURIComponent(e.data.token));
          if (!params.has('buy')) saveCart([]);
        }
      });
    }).catch(() => { $('.frame-loading',frameHost).replaceChildren(node('strong',{},'Sigurna veza trenutno nije dostupna.'),node('p',{},'Pokušajte ponovo za trenutak ili pozovite +381 63 697 007.')); });
  }

  const checkout = $('[data-checkout-form]');
  if (checkout) {
    let sequence = 0, pending = false;
    const refresh = async () => {
      const form = new FormData(checkout), pickup = form.get('delivery_method') === 'pickup', international = form.get('country') !== 'RS';
      $('[data-shipping-fields]').hidden = pickup;
      checkout.elements.city.required = !pickup; checkout.elements.address.required = false; checkout.elements.postal_code.required = false;
      $('[data-domestic-courier]').hidden = international || pickup;
      $('[data-international-note]').hidden = !international || pickup;
      const pickupOption = $('[data-pickup-option]'); if (pickupOption) { pickupOption.hidden = international; $('input',pickupOption).disabled = international; }
      const cod = $('[data-cod-option]'), storePay = $('[data-store-pay-option]'); cod.hidden = international || pickup; storePay.hidden = !pickup;
      $('input',cod).disabled = international || pickup; $('input',storePay).disabled = !pickup;
      const selected = $('input[name=payment_method]:checked',checkout);
      if (!selected || selected.disabled) { const next = pickup ? $('input[value=pickup]',storePay) : international ? $('input[name=payment_method][value=card]',checkout) : $('input',cod); if (next) next.checked = true; }
      pending = true; const seq = ++sequence; $('[data-order-submit]').disabled = true;
      try {
        const res = await fetch('/api/quote', {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':form.get('csrf')},body:JSON.stringify({items:JSON.parse(form.get('items')),country:form.get('country'),method:form.get('delivery_method'),courier:form.get('courier')})});
        const data = await res.json(); if (seq !== sequence) return; if (!res.ok) throw Error(data.error || 'Proverite dostavu.');
        $$('[data-quote]').forEach(el => {el.textContent = data.formatted[el.dataset.quote];});
        data.items.forEach((line,index)=>{const amount=$('[data-item-quote="'+index+'"]',checkout),eur=$('[data-item-eur="'+index+'"]',checkout);if(amount)amount.textContent=money(line.unit_minor*line.qty,data.currency);if(eur)eur.textContent=money(line.unit_eur_cents*line.qty,'EUR');});
        $('[data-delivery-days]').textContent = data.days;
        if($('[data-pricing-explanation]'))$('[data-pricing-explanation]').textContent=data.pricing_explanation;
        checkout.elements.quoted_total.value = data.total_minor; checkout.elements.quoted_currency.value = data.currency;
        $('[data-quote-error]').hidden = true; $('[data-order-submit]').disabled = false;
      } catch(e) { if (seq !== sequence) return; $('[data-quote-error]').hidden = false; $('[data-quote-error]').textContent = e.message || 'Obračun nije dostupan. Osvežite stranicu.'; }
      finally { if (seq === sequence) pending = false; }
    };
    $$('input[name=delivery_method],select[name=country],select[name=courier]',checkout).forEach(el => el.addEventListener('change',refresh));
    checkout.addEventListener('submit', e => { if (pending) { e.preventDefault(); return; } if ($('input[name=payment_method]:checked',checkout)?.value === 'card') checkout.target = '_top'; $('[data-order-submit]').disabled = true; $('[data-order-submit]').textContent = 'Čuvamo vašu porudžbinu…'; });
    let addressTimer, proposal;
    const suggest = async () => {
      const f = new FormData(checkout), address = f.get('address'), city = f.get('city');
      if (!address || !city || f.get('country') !== 'RS') return;
      try {
        const response = await fetch('/api/address-suggestion',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':f.get('csrf')},body:JSON.stringify({address,city,country:'RS'})});
        if (!response.ok) return;
        const data = await response.json(); proposal = data.suggestion;
        if (checkout.elements.address.value !== address || checkout.elements.city.value !== city) return;
        $('[data-address-suggestion]').hidden = !proposal;
        if (proposal) $('[data-address-proposal]').textContent = 'Predlog tačnog zapisa iz registra: ' + proposal.address + ', ' + proposal.city + '.';
      } catch { /* Free entry always works even when address suggestions are unavailable. */ }
    };
    ['address','city'].forEach(name => checkout.elements[name].addEventListener('input',()=>{clearTimeout(addressTimer); $('[data-address-suggestion]').hidden=true; addressTimer=setTimeout(suggest,700);}));
    $('[data-accept-address]').addEventListener('click',()=>{if(proposal){checkout.elements.address.value=proposal.address;checkout.elements.city.value=proposal.city;} $('[data-address-suggestion]').hidden=true;});
    $('[data-keep-address]').addEventListener('click',()=>{$('[data-address-suggestion]').hidden=true;});
    refresh();
  }

  const photoPreview=$('[data-photo-preview]'),photoForm=$('[data-inventory-form]');
  if(photoPreview&&photoForm&&typeof DataTransfer!=='undefined'){
    const inputs=$$('[data-photo-input]'),status=$('[data-photo-status]');
    const upload=node('input',{type:'file',name:'photos',multiple:'',hidden:'','data-staged-photos':''});
    photoForm.append(upload);inputs.forEach(input=>input.removeAttribute('name'));
    const photos=[];let pending=0;
    const kept=()=>$$('[name=keep_images]:checked',photoForm).length;
    const sync=()=>{const transfer=new DataTransfer();photos.forEach(p=>transfer.items.add(p.file));upload.files=transfer.files;};
    const count=()=>{const total=kept()+photos.length;status.textContent=pending?'Pripremamo fotografije…':total+' / 10 fotografija.'+(total<10?' Možete snimiti još.':' Uklonite neku da dodate novu.');};
    const add=async files=>{
      pending++;photoForm.dataset.photosBusy='1';count();
      try{
        for(const original of files){
          if(kept()+photos.length>=10){status.textContent='Najviše 10 fotografija. Uklonite neku da dodate novu.';break;}
          if(original.type&&!original.type.startsWith('image/'))continue;
          const key=original.name+'|'+original.size+'|'+original.lastModified;
          if(photos.some(p=>p.key===key))continue;
          const file=original,url=URL.createObjectURL(file),entry={key,file,url};photos.push(entry);
          const figure=node('div',{class:'photo-entry'}),img=node('img',{class:'new-photo',src:url,alt:'Nova fotografija uređaja',width:'110',height:'90'});
          const remove=node('button',{type:'button',class:'photo-remove','aria-label':'Ukloni fotografiju'},'×');
          remove.addEventListener('click',()=>{photos.splice(photos.indexOf(entry),1);URL.revokeObjectURL(url);figure.remove();sync();count();});
          figure.append(img,remove);photoPreview.append(figure);sync();
        }
      }finally{pending--;if(!pending)delete photoForm.dataset.photosBusy;count();}
    };
    let queue=Promise.resolve();inputs.forEach(input=>input.addEventListener('change',()=>{const files=[...input.files];input.value='';queue=queue.then(()=>add(files)).catch(()=>{status.textContent='Fotografija nije dodata. Pokušajte ponovo.';});}));
    $$('[name=keep_images]',photoForm).forEach(input=>input.addEventListener('change',count));
    photoForm.addEventListener('submit',event=>{if(pending){event.preventDefault();status.textContent='Sačekajte pripremu fotografija pre čuvanja.';}});
    if(navigator.mediaDevices?.getUserMedia){
      const open=$('[data-open-camera]'),panel=$('[data-camera-panel]'),video=$('[data-camera-video]'),capture=$('[data-capture-photo]'),cameraStatus=$('[data-camera-status]');
      let stream=null,cameraVersion=0;
      const stop=()=>{cameraVersion++;stream?.getTracks().forEach(track=>track.stop());stream=null;video.srcObject=null;panel.hidden=true;open.disabled=false;};
      open.hidden=false;$('[data-native-camera]').hidden=true;
      open.addEventListener('click',async()=>{
        if(stream)return;const current=++cameraVersion;open.disabled=true;panel.hidden=false;capture.disabled=true;cameraStatus.textContent='Otvaramo zadnju kameru…';
        try{
          const requested=await navigator.mediaDevices.getUserMedia({audio:false,video:{facingMode:{ideal:'environment'},width:{ideal:3840},height:{ideal:2160}}});
          if(panel.hidden||current!==cameraVersion){requested.getTracks().forEach(track=>track.stop());return;}
          stream=requested;video.srcObject=stream;await video.play();capture.disabled=false;cameraStatus.textContent='Snimite više fotografija, pa dodirnite „Gotovo“.';panel.scrollIntoView({block:'center',behavior:'smooth'});
        }catch{
          if(current!==cameraVersion)return;
          stop();$('[data-native-camera]').hidden=false;status.textContent='Kamera u stranici nije dostupna. Upotrebite dugme kamere ispod ili izaberite fotografije iz galerije.';
        }
      });
      capture.addEventListener('click',async()=>{
        if(!stream||!video.videoWidth)return;
        if(kept()+photos.length>=10){cameraStatus.textContent='Dodato je 10 fotografija. Uklonite neku pre novog snimka.';return;}
        capture.disabled=true;photoForm.dataset.photosBusy='1';
        try{
          let blob;
          if(typeof ImageCapture!=='undefined'){
            try{blob=await new ImageCapture(stream.getVideoTracks()[0]).takePhoto();}catch{}
          }
          if(!blob){
            const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
            canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);
            blob=await new Promise(resolve=>canvas.toBlob(resolve,'image/jpeg',.95));
          }
          if(!blob)throw Error();
          const file=new File([blob],'reklik-'+Date.now()+'.jpg',{type:'image/jpeg'});
          queue=queue.then(()=>add([file]));await queue;cameraStatus.textContent='Dodato '+(kept()+photos.length)+' / 10 fotografija. Možete snimiti sledeću.';
        }catch{cameraStatus.textContent='Snimak nije dodat. Pokušajte ponovo.';}finally{capture.disabled=false;if(!pending)delete photoForm.dataset.photosBusy;}
      });
      $('[data-close-camera]').addEventListener('click',stop);
      window.addEventListener('pagehide',stop);
      document.addEventListener('visibilitychange',()=>{if(document.hidden)stop();});
    }
  }

  $$('[data-delete-product]').forEach(form=>form.addEventListener('submit',event=>{if(!confirm('Trajno obrisati ovaj uređaj iz inventara? Sačuvane porudžbine ostaju dostupne.'))event.preventDefault();}));
  const aiForm = $('[data-ai-form]');
  const inventory=$('[data-inventory-form]');
  if(inventory){
    const price=inventory.elements.price,listing=$('[data-list-price]');
    const previewPrice=()=>{
      const match=/^(\d+)(?:[.,](\d{1,2}))?$/.exec(price.value.trim());
      if(!match){listing.value='';return;}
      const cents=Number(match[1])*100+Number((match[2]||'').padEnd(2,'0'));
      listing.value=cents>0?money(Math.ceil(cents*105/50000)*500,'EUR'):'';
    };
    price.addEventListener('input',previewPrice);previewPrice();
    const status=$('[data-spec-status]'),save=$('[data-inventory-save]');
    inventory.elements.category_id.addEventListener('change',()=>{inventory.elements.auto_category_id.value='0';});
    $('[data-fetch-specs]').addEventListener('click',()=>{
      inventory.elements.lookup_requested.value='1';
      inventory.requestSubmit(save);
    });
    inventory.addEventListener('submit',event=>{
      if(inventory.dataset.photosBusy){event.preventDefault();$('[data-photo-status]').textContent='Sačekajte pripremu fotografija pre čuvanja.';return;}
      if(event.defaultPrevented)return;
      const bytes=[...inventory.querySelectorAll('input[type=file]')].reduce((n,input)=>n+[...input.files].reduce((s,file)=>s+file.size,0),0);
      if(bytes>34*1024*1024){event.preventDefault();$('[data-photo-status]').textContent='Fotografije su ukupno prevelike. Uklonite neku ili izaberite manje datoteke.';return;}
      if(inventory.dataset.submitting){event.preventDefault();return;}
      inventory.dataset.submitting='1';save.disabled=true;save.setAttribute('aria-busy','true');save.textContent='Čuvamo uređaj…';
    });
    window.addEventListener('pageshow',event=>{if(event.persisted){delete inventory.dataset.submitting;save.disabled=false;save.removeAttribute('aria-busy');save.textContent='Sačuvaj uređaj';}});
  }
  if($('[data-product-ai][data-state="pending"]')){
    const refresh=async()=>{
      try{
        const response=await fetch('/admin/inventory-status',{cache:'no-store'});if(!response.ok)return;
        for(const task of await response.json()){
          const label=$('[data-product-ai="'+task.product_id+'"]');if(!label)continue;
          label.dataset.state=task.status;
          label.textContent=task.status==='pending'?'AI obrađuje podatke u pozadini…':task.status==='completed'?'AI podaci su spremni.':task.status==='failed'?'AI nije dovršen. Otvorite Uredi → Sačuvaj i dopuni nedostajuće specifikacije.':'';
        }
      }catch{}
      if($('[data-product-ai][data-state="pending"]'))setTimeout(refresh,2500);
    };setTimeout(refresh,2000);
  }
  if (aiForm) aiForm.addEventListener('submit', async e => {
    e.preventDefault(); const button = $('[data-ai-submit]'), status = $('[data-ai-status]'), log = $('[data-ai-log]');
    button.disabled = true; status.textContent = 'Pokrećemo uređivanje…'; log.hidden = false; log.textContent = '';
    try {
      const res = await fetch('/admin/ai',{method:'POST',body:new FormData(aiForm)}), job = await res.json(); if (!res.ok) throw Error(job.error);
      let finished = false;
      while (!finished) { await new Promise(resolve => setTimeout(resolve,2200)); const response = await fetch('/admin/ai/'+encodeURIComponent(job.id),{cache:'no-store'}); if (!response.ok) throw Error('Veza je prekinuta. Osvežite stranicu radi statusa.'); const data = await response.json(); log.textContent = data.output; log.scrollTop = log.scrollHeight; finished = ['completed','failed'].includes(data.status); status.textContent = data.status === 'completed' ? 'Izmene su proverene i objavljene.' : data.status === 'failed' ? 'Izmene nisu objavljene; pogledajte detalje.' : 'AI uređuje i proverava sajt…'; }
    } catch(e) {status.textContent = e.message || 'Uređivanje trenutno nije dostupno.';} finally {button.disabled = false;}
  });

  if (window.parent !== window) {
    if (document.querySelector('form')) fetch('/api/session',{cache:'no-store'}).then(r=>r.json()).then(data=>{
      if (data.ready) return;
      const notice=node('div',{class:'notice cookie-fallback',role:'alert'});
      notice.append(node('p',{},'Za nastavak otvorite sigurnu stranicu u posebnom prozoru. Pregledač ovde ne može da sačuva vašu prijavu i porudžbinu.'));
      notice.append(node('a',{href:location.href,target:'_blank',rel:'noopener',class:'button button-pink'},'Otvori sigurnu stranicu'));
      document.querySelector('main').prepend(notice);
      document.querySelectorAll('form').forEach(form=>{form.inert=true;});
      document.querySelectorAll('form button[type=submit],form button:not([type]),[data-order-submit]').forEach(button=>{button.disabled=true;});
    }).catch(()=>{});
    const origins = ['https://nbulatovi.github.io','https://reklik.shop','https://www.reklik.shop'];
    const notify = () => { const data = {type:'reklik-height',height:Math.ceil(document.body.getBoundingClientRect().bottom+window.scrollY)+25}; origins.forEach(origin => parent.postMessage(data,origin)); const order = $('[data-order-token]'); if (order) origins.forEach(origin => parent.postMessage({type:'reklik-order',token:order.dataset.orderToken},origin)); };
    new ResizeObserver(notify).observe(document.body); window.addEventListener('load',notify);
  }
})();
