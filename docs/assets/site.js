/* ReKlik: progressively enhanced HTML. No analytics, ads, or third-party scripts. */
(() => {
  'use strict';
  const base = document.body.dataset.base || '';
  const live = document.body.dataset.live === '1';
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const readCart = () => { try { const x = JSON.parse(localStorage.getItem('reklik-cart') || '[]'); return Array.isArray(x) ? x.slice(0, 30) : []; } catch { return []; } };
  const countCart = () => $$('[data-cart-count]').forEach(x => { x.textContent = readCart().reduce((n, v) => n + (Number(v.qty) || 0), 0); });
  const saveCart = items => { try { localStorage.setItem('reklik-cart', JSON.stringify(items)); } catch { /* Shopping still works with Buy now. */ } countCart(); };
  const money = (minor, currency) => new Intl.NumberFormat('sr-Latn-RS', {style: 'currency', currency, minimumFractionDigits: currency === 'RSD' && minor % 100 === 0 ? 0 : 2, maximumFractionDigits: 2}).format(minor / 100);
  const node = (tag, attributes = {}, text = '') => { const e = document.createElement(tag); Object.entries(attributes).forEach(([k,v]) => e.setAttribute(k, v)); e.textContent = text; return e; };
  countCart();
  window.addEventListener('storage', countCart);

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

  const fold = value => { const table = {'а':'a','б':'b','в':'v','г':'g','д':'d','ђ':'dj','е':'e','ж':'z','з':'z','и':'i','ј':'j','к':'k','л':'l','љ':'lj','м':'m','н':'n','њ':'nj','о':'o','п':'p','р':'r','с':'s','т':'t','ћ':'c','у':'u','ф':'f','х':'h','ц':'c','ч':'c','џ':'dz','ш':'s','đ':'dj'}; return [...String(value).toLowerCase()].map(c=>table[c]||c).join('').normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/sh/g,'s').replace(/ch/g,'c').replace(/zh/g,'z'); };
  const search = $('[data-catalog-search]'), grid = $('[data-product-grid]');
  if (grid) {
    const cards = $$('.product-card', grid);
    const brand = $('[data-brand-filter]'), min = $('[data-price-min]'), max = $('[data-price-max]'), stock = $('[data-in-stock]');
    const panel = $('[data-filter-panel]'), desktop = window.matchMedia('(min-width: 701px)');
    const layoutFilters = () => { if (panel) panel.open = desktop.matches; };
    layoutFilters(); desktop.addEventListener('change',layoutFilters);
    const filter = () => {
      const query = fold(search?.value || '').trim();
      const tokens = query.split(/\s+/).filter(Boolean);
      const floor = min?.value ? Number(min.value) : 0, ceiling = max?.value ? Number(max.value) : Infinity;
      cards.forEach(c => { const price = Number(c.dataset.price); c.hidden = !tokens.every(t => fold(c.dataset.search).includes(t)) || (brand?.value && c.dataset.brand !== brand.value) || price < floor || price > ceiling || (stock?.checked && Number(c.dataset.stock) < 1); });
      const n = cards.filter(c => !c.hidden).length;
      if ($('[data-results-count]')) $('[data-results-count]').textContent = n;
      if ($('[data-empty]')) $('[data-empty]').hidden = n > 0;
      const active = [brand?.value,min?.value,max?.value,stock?.checked].filter(Boolean).length;
      if ($('[data-filter-count]')) $('[data-filter-count]').textContent = active ? '('+active+')' : '';
    };
    if (search) {
      search.value = new URLSearchParams(location.search).get('q') || search.value;
      search.addEventListener('input', filter);
      search.form?.addEventListener('submit',event=>{event.preventDefault();filter();grid.scrollIntoView({block:'start',behavior:'smooth'});});
    }
    [brand,min,max,stock].filter(Boolean).forEach(input=>input.addEventListener(input.tagName === 'SELECT' || input.type === 'checkbox' ? 'change' : 'input',filter));
    $$('[data-reset-filters]').forEach(button=>button.addEventListener('click',()=>{if(search)search.value='';if(brand)brand.value='';if(min)min.value='';if(max)max.value='';if(stock)stock.checked=false;filter();}));
    $('[data-sort]')?.addEventListener('change', e => {
      const mode = e.target.value;
      [...cards].sort((a,b) => mode === 'low' ? Number(a.dataset.price)-Number(b.dataset.price) : mode === 'high' ? Number(b.dataset.price)-Number(a.dataset.price) : mode === 'name' ? a.dataset.search.localeCompare(b.dataset.search) : cards.indexOf(a)-cards.indexOf(b)).forEach(c => grid.append(c));
    });
    filter();
  }

  if ($('[data-cart-items]')) {
    fetch(base + '/catalog.json', {cache: 'no-store'}).then(r => { if (!r.ok) throw Error(); return r.json(); }).then(data => {
      const render = () => {
        const items = readCart(), host = $('[data-cart-items]'); host.replaceChildren(); let total = 0;
        items.forEach((line, index) => {
          const p = data.products.find(p => p.id === Number(line.id)); if (!p) return;
          const row = node('article', {class: 'cart-item'}), img = node('img', {src: p.images.length ? base + '/media/' + p.images[0] : base + '/assets/favicon.svg', alt: p.title, width: '90', height: '80'});
          const details = node('div'), title = node('h3'); title.append(node('a', {href: base + '/proizvod/' + p.slug + '/'}, p.title)); details.append(title);
          const qty = node('input', {type: 'number', min: '1', max: String(Math.max(1, Math.min(20,p.quantity))), value: String(line.qty), 'aria-label': 'Količina za ' + p.title});
          qty.addEventListener('change', () => { const next = readCart(); next[index].qty = Math.max(1, Math.min(20,Number(qty.value)||1)); saveCart(next); render(); });
          const choices = node('select', {'aria-label': 'Dodatna garancija za ' + p.title});
          for (let n=0;n<=3;n++) { const option = node('option', {value: String(n)}, n ? '+' + n + ' mes. — ' + money(data.warranty_minor[n], data.currency) : 'Bez dodatne garancije'); option.selected = Number(line.warranty) === n; choices.append(option); }
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
    }).catch(() => { $('[data-cart-empty] p').textContent = 'Katalog trenutno nije dostupan. Osvežite stranicu ili koristite Kupi odmah uz uređaj.'; });
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
        $('[data-delivery-days]').textContent = data.days;
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

  $$('[data-photo-input]').forEach(input => input.addEventListener('change', () => {
    const preview = $('[data-photo-preview]'); if (!preview) return;
    $$('.new-photo', preview).forEach(el => { URL.revokeObjectURL(el.src); el.remove(); });
    $$('[data-photo-input]').forEach(field => [...field.files].slice(0,10).forEach(file => {if (file.type.startsWith('image/')) preview.append(node('img',{class:'new-photo',src:URL.createObjectURL(file),alt:'Nova fotografija',width:'85',height:'70'}));}));
  }));

  const aiForm = $('[data-ai-form]');
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
