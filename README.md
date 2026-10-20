# ReKlik.shop

Serbian refurbished electronics storefront: complete crawlable HTML on GitHub Pages, with inventory, orders, payment verification and administration on the owner's PC.

## Running services

`reklik-server.service` serves the application on `127.0.0.1:4340`; `reklik-tunnel.service` provides an isolated HTTPS Cloudflare quick tunnel. The tunnel is separate from the existing GCC Help, Topkvote and Mayaki tunnels. On a tunnel restart its endpoint is automatically republished in `docs/backend.json`.

The production server service sets `REKLIK_SECURE_COOKIES=1`: its loopback listener is exposed exclusively by the HTTPS tunnel, so external origin checks and partitioned Secure cookies use HTTPS even if cloudflared omits the forwarded scheme. Embedded browsers that block cookies receive a first-party window link before form entry.

The GitHub Pages entry point is `/docs`. Checkout, confirmation and admin pages frame the PC application. Public category and product pages contain full HTML and JSON-LD outside frames so they remain crawlable. A frame keeps the storefront address in the browser; it does not conceal the underlying network endpoint.

```sh
systemctl --user status reklik-server reklik-tunnel
journalctl --user -u reklik-server -u reklik-tunnel -n 80
/home/bg/.local/state/reklik/venv/bin/python tests/test_store.py
/home/bg/.local/state/reklik/venv/bin/python publisher.py
```

## Private state

All live state is outside the repository in `~/.local/state/reklik/`:

- `store.sqlite3`: inventory, categories, orders, sessions and audit records; SQLite WAL and transactional stock reservations.
- `private.json`: salted administrator password hash and signing secret, mode 0600. No plaintext admin password is stored in the repository.
- `uploads/`: original published WebP photographs, with metadata stripped.
- `addresses.sqlite3`: local RGZ address register, not customer data.
- `backups/`: rotating hourly database snapshots, last 48 snapshots, private permissions. Photograph files remain in uploads; these local snapshots are not an offsite backup.
- `ai-auth/`, `ai/`, `ai-rollback/`: isolated editor credentials, working copies and rollback copies. Never publish these.

Whop credentials are read at runtime from the existing `~/.secrets/electricians.env` (override with `REKLIK_PAYMENT_ENV`). They are never placed in browser JavaScript, public HTML, Git or the AI workspace. Card numbers are entered only on Whop hosted checkout; this server never receives them.

## Product browsing and item pages

Desktop browsing puts brand, large contact number, search, categories and filters in a side rail; products begin near the top edge. Mobile uses a compact two-column header and floating filter control. Product pages show a photo carousel, main specifications, price, Buy now and Add to cart together. Mobile purchase controls stay fixed at the bottom. Whole-number prices omit zero decimals while genuine fractional amounts remain exact.

The current eight inventory entries are temporary fixtures requested by the owner for testing the full shopping flow. They are ordinary saleable stock, without special example labels or checkout restrictions. Card checkout is live and charges the shown amount if confirmed; replace/unpublish these fixtures before the intended commercial launch. Indexing remains off pending the domain cutover.

Browser QA uses temporary state and explicitly disables publishing. It must never export its temporary catalogue into the live repository.

## Item view statistics

Opening an individual item page makes a cookieless request to the PC backend. Admin shows distinct IP counts per item across all time. The database stores a product-specific keyed HMAC of the normalized address and first-seen time, never a raw IP for this counter. Cloudflare's connecting-IP header supplies the address; IPv4-mapped IPv6 forms normalize together. Repeat visits cannot increment the item twice. Known crawler user agents are excluded. A shared IP counts once; an IP change counts separately. Counts are private, excluded from public catalogue JSON, and removed with product deletion. Privacy text describes this first-party feature; no third-party analytics service is used.

## Automatic inventory specifications

The first fields are photos, brand, model and price. Brand/model changes trigger authenticated Codex CLI manufacturer research. Saving never waits for this lookup: the item and photographs are stored immediately, and a durable `product_ai` record binds the background job to that inventory item. The admin can start the next item or close the page; completion fills unchanged automatic specifications, description and category, then republishes. Manual edits, photos, price, quantity and activation are preserved. Pending inventory jobs resume after server or PC restart; failures remain visible beside the item and can be retried from Edit. The admin list refreshes job status without reloading a partially entered next item. Advanced inputs remain editable; explicit manual edits are preserved. Results include primary manufacturer links and are cached for 30 days. Model-family variants are identified rather than inventing a unit's RAM/storage/configuration. The research worker uses separate CLI state, Bubblewrap, a web-only trusted hook, schema-constrained output and no access to production inventory, orders or payment secrets. A live Dell Latitude 5420 research job passed and returned Dell support documents. No API key is copied to the browser.

## Admin operation

Use the Admin button at the bottom of the home page, or `/admin/` on the storefront. Inventory supports creating, editing, unpublishing and duplicating products; camera capture and gallery uploads work from phones. A published product needs a real photograph. The interfaces were exercised at 320, 390, 768 and 1440 CSS pixels; form fields use at least 16px text and 48px controls. Rear-camera capture, repeated photos, removal, upload, AI research and save were tested on the USB-connected Android 15 phone in Chrome 137. Automated mobile tests also cover saving during a slow lookup and later enrichment after leaving the form. A real isolated manufacturer lookup queued in 0.039 seconds and completed after 24 seconds without changing price or stock. New categories receive stable public URLs and are added to the sitemap automatically. Stock revisions prevent an old editing form from overwriting a newer sale.

Prices are stored as EUR minor units. Changing the site currency converts the storefront using the editable EUR/RSD rate. Existing orders retain their original currency and total. Warranty prices and courier tariffs are entered in RSD and converted for an EUR storefront.

Shipping settings include Post Express, AKS and D Express independently. Each has its published weight bands; admin can update the bands, enable/disable or rename a carrier, and change an optional coefficient. Category fixed prices override the carrier calculation for that category; a blank field uses the weight tariff. In a mixed cart, the highest applicable fixed price and automatic combined-weight tariff apply. A fixed value of zero is free shipping when all cart categories have fixed prices. Shipping uses packed product weight, otherwise the category's estimated weight. Enter the carrier's chargeable weight if volumetric weight is higher.

Domestic defaults are published base carriage tariffs, not carrier bookings or contracted merchant discounts. COD-specific charges are absorbed by ReKlik; do not instruct a courier to collect more than the customer's agreed order total. Declared value, oversize and other carrier fees must be absorbed when not already included in a displayed total. Automatic Post Express calculation stops above 20 kg; AKS and D Express stop above 50 kg rather than assuming acceptance.

Order addresses are free text. Serbian Latin, Cyrillic, common ASCII spellings and villages without a street are supported. Postcode is optional but, if supplied for Serbia, must contain five digits. The order remains NEW until a person calls the customer and marks it CONFIRMED; dispatch is blocked before that confirmation. Card orders also need processor-confirmed payment. Orders and printable/downloadable confirmations are stored durably; the site does not currently send customer emails or book shipments automatically.

## Payments and stock

Buy now skips the cart. COD checkout is one guest page with no account or marketing signup. Card checkout sends the customer directly to Whop after the delivery form. A returned URL never marks an order paid: company, plan, currency, total, payment status and unique payment ID are verified server-side. A background worker reconciles pending/expired card orders; the order page can request verification as well. Signed webhook support is available when `WHOP_WEBHOOK_SECRET` is configured, but reconciliation does not depend on webhooks or an unchanging tunnel URL.

Card orders reserve stock for 30 minutes. Expiry/cancellation releases stock once. A late payment with unavailable stock enters manual review and is never automatically fulfilled. Refunds are handled in the merchant dashboard; cancelling a paid order does not itself issue a refund. Never cancel shipped stock back onto the shelf before a return is received.

The admin payment check creates a **real charge**, EUR 1.00 or RSD 120.00. It is explicitly labelled, contains no physical inventory and is not a sandbox. No charge occurs until the payer enters a real card and confirms the processor's payment. Sandbox/test card numbers must not be entered in this live checkout. The existing shared processor account currently displays “Nikola’s Electric”; the store does not rename that shared account. ReKlik checkout configurations disable adaptive currency conversion, accept cards only, limit each order to one purchase, and use the processor-required `WHOP*REKLIK SHOP` statement descriptor.

## International shipping

There is one international switch, OFF by default. No per-country admin controls. Do not enable it until the Serbian exporter/invoice/customs arrangement, destination requirements, sanctions/export controls and the carrier's acceptance of refurbished electronics with installed batteries have been resolved. The switch is not a legal clearance or a guarantee that every destination is served. International COD is disabled. Estimates use DHL Express service-point references with a 25% fuel/packing buffer, weight adjustment and editable category multiplier. Destination duties/taxes are excluded and disclosed before purchase. Pošta air services must not be used automatically for battery devices.

Research references, checked 20 October 2026:

- Post Express 2026 tariff: https://www.posta.rs/DocumentViewer.aspx?Dokument=privreda-paketske-srbija-post-ekspres-01-01-2026-lat.pdf&IdDokument=1002727
- AKS July 2026 tariff and YUBC location: https://www.aks.rs/cenovnik/ and https://www.aks.rs/regionalni-centri/
- D Express 13 March 2026 tariff (fuel already included): https://www.dexpress.rs/rs/cenovnik/rs
- DHL estimate reference: https://dhlservicepoint.rs/en/express-easy/
- Serbia export procedures: https://www.carina.rs/privreda/carinski-postupci/izvoz-robe.html
- Pošta prohibited articles: https://www.posta.rs/DocumentViewer.aspx?Dokument=spisak-zabranjenih-artikala-opste-zabrane-lat.pdf&IdDokument=1000932
- DHL electronics: https://www.dhl.com/discover/en-us/ship-with-dhl/start-shipping/how-to-ship-electronics
- EU WEEE Annex VI: https://eur-lex.europa.eu/eli/dir/2012/19/2018-07-04/eng/pdf
- US entity sanctions scope: https://ofac.treasury.gov/faqs/11 and https://www.bis.gov/learn-support/reexports-and-exports-from-abroad

## Address register

Source: Republic Geodetic Authority (RGZ), Address Register, downloaded 2026-10-20, 2,491,067 active rows. Licence: Serbian Open Data Licence, https://data.gov.rs/sr/terms/ . Attribution is included on the privacy page.

Dataset: https://data.gov.rs/sr/datasets/adresni-registar/

Original ZIP URL: https://download.geosrbija.rs/download-api/opendata-proxy/export?category=ar&layer=kucni_broj_ar&geometry=true&fileName=kucni_br_csv&format=csv

Import with `python addresses.py /path/to/archive.zip`. Import is atomic. Only active records and canonical address fields are retained, with script/diacritic normalization for exact matching. Suggestions require one unique normalized street + complete house number + locality/municipality match. Ambiguous matches return no suggestion. No fuzzy matching, silent correction, street dropdown or external customer-address lookup is used. RGZ has no postcode field; no postcode verification is claimed.

## Warranty and seller details

ReKlik.shop is the visible brand. The user-supplied legal seller Nikola's Electric is limited to required seller/checkout/receipt disclosures. The local operating address, phone and email use the supplied local reference; there is no outbound link to that reference site.

Optional warranty defaults use the published local benchmark of RSD 600/month, so 1/2/3 additional months cost 600/1200/1800 RSD; each is editable. The competitor's current promotional cart included a longer warranty, so these are extrapolated ReKlik prices, not claims about an equivalent currently chargeable competitor offer. The commercial extension starts after applicable statutory conformity rights and never replaces them. Battery exclusion is confined to the optional warranty terms/documentation. Seller registration/tax particulars and fiscal invoicing arrangements must be supplied by the operator where applicable; do not invent them.

Primary legal sources: Serbian Consumer Protection Act 35/2026 https://www.parlament.gov.rs/upload/archive/files/cir/pdf/zakoni/14_saziv/1317-26.pdf and Electronic Commerce Act https://must.gov.rs/extfile/sr/2208/zakon_o_elektronskoj_trgovini.pdf .

## AI editor

The authenticated AI box edits an isolated copy of this site's source through the installed Codex CLI. Bubblewrap does not mount customer data, inventory, the production payment environment or other projects. A trusted hook restricts tools and file writes. Trusted tests run before edits are copied; publication failure restores source copies. Infrastructure and tests cannot be changed by the editor. Backend code changes trigger a server restart after publication; the tunnel is separate. Validate hook operation after CLI upgrades. The editor uses the existing local Codex login, with separate private CLI state.

## Domain and indexing

`reklik.shop` has been purchased. Until its Namecheap parking DNS is replaced, the live preview remains the GitHub Pages project URL. The enabled `reklik-domain-activation.service` checks DNS every 45 seconds and completes the authorized cutover once the apex and www records point at GitHub Pages. It registers the custom domain, publishes root-based links, waits for a valid GitHub HTTPS certificate, enforces HTTPS, checks public catalog/admin/checkout/backend/sitemap, then enables indexing and verifies the published robots/meta tags. It exits after success and is safe to restart. Status is private in `~/.local/state/reklik/domain-activation.json`; no account keys are written to the repository. Canonicals point at the intended domain, and indexing is OFF by default. The activation service updates the public URL and indexing automatically after the corresponding checks; these remain editable in admin. The exporter creates `CNAME`, `robots.txt`, `sitemap.xml`, unique category/product URLs, Product/Offer/Breadcrumb JSON-LD and responsive original photographs. Private routes remain noindex. Google decides when crawling/indexing happens; this code does not claim an indexing guarantee or Search Console verification.

GitHub Pages DNS: apex A records `185.199.108.153`, `185.199.109.153`, `185.199.110.153`, `185.199.111.153`; `www` CNAME to `nbulatovi.github.io`. Verify current GitHub Pages domain checks and HTTPS before final cutover.
