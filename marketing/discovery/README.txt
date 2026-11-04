ReKlik server-side search discovery

indexnow_sync.py reads the LIVE canonical sitemap and public HTML from reklik.shop.
It submits only changed/new URLs and verifies 404/410 before notifying removals.
The first run registers the current public catalog; subsequent runs compare hashes
and use conditional HTTP requests. HTTP 200/202 means receipt, not indexing.

The public ownership proof is /indexnow.txt (exported from assets/indexnow.txt).
It must be at the domain root to cover product pages in other folders. It is public;
it is not an account credential. No customer information, admin URLs, cookies,
advertising scripts, raw IP addresses or private inventory prices are sent.

Run using the project Python environment. A five-minute systemd user timer on
the backend PC invokes the script. Private accepted snapshots and last result:
~/.local/state/reklik/search-discovery/. Disable the timer to stop notifications.
Normal storefront publication copies assets, preserving the proof automatically.

Protocol: https://www.indexnow.org/documentation
Participating engines: https://www.indexnow.org/faq
Google Ads approval and Google indexing are separate from this mechanism.
