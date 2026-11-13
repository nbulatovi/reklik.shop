"""Conservative Halo Oglasi UI adapter for the owner's free inventory mirror.

Only normal authenticated browser UI is used. Existing IDs are never recreated;
unknown fees, verification challenges and ambiguous submissions fail closed.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

ORIGIN = 'https://www.halooglasi.com'
STATE = Path.home() / '.local/state/reklik'
REMOTE_ID = re.compile(r'[1-9][0-9]{5,19}')
ZONE = ZoneInfo('Europe/Belgrade')
FREE_CATEGORIES = ('/racunari', '/telefoni')


class HaloError(RuntimeError):
    code = 'browser_error'


class HaloAuthRequired(HaloError):
    code = 'auth_required'


class HaloNeedsReview(HaloError):
    code = 'needs_review'


class HaloFeeRequired(HaloNeedsReview):
    code = 'fee_required'


class HaloSubmissionUncertain(HaloError):
    code = 'submission_uncertain'
    may_have_submitted = True


NeedsReview = HaloNeedsReview
FeeRequired = HaloFeeRequired


def validate_remote_id(remote_id):
    value = str(remote_id)
    if isinstance(remote_id, bool) or REMOTE_ID.fullmatch(value) is None:
        raise HaloNeedsReview('invalid_remote_id')
    return value


def own_origin(url):
    parsed = urlsplit(url)
    return parsed.scheme == 'https' and parsed.netloc in ('www.halooglasi.com', 'halooglasi.com')


def observed_public_url(href, remote_id):
    """Accept an observed link, never construct a product slug."""
    remote_id = validate_remote_id(remote_id)
    parsed = urlsplit(urljoin(ORIGIN, href or ''))
    if (not own_origin(urlunsplit(parsed)) or not parsed.path.startswith(('/racunari/', '/telefoni/'))
            or not parsed.path.rstrip('/').endswith('/' + remote_id)):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))


def edit_id(href):
    parsed = urlsplit(urljoin(ORIGIN, href or ''))
    match = re.fullmatch(r'/izmeni-oglas/([1-9][0-9]{5,19})/?', parsed.path)
    return match[1] if own_origin(urlunsplit(parsed)) and match and not parsed.query and not parsed.fragment else None


def same_phone(actual, expected):
    def normalized(value):
        digits = re.sub(r'\D', '', str(value))
        if digits.startswith('00381'):
            return '0' + digits[5:]
        if digits.startswith('381'):
            return '0' + digits[3:]
        return digits
    return normalized(actual) == normalized(expected)


def expiry_from_text(text):
    match = re.search(r'Ističe\s+(\d{1,2}\.\d{1,2}\.\d{4})', text)
    if not match:
        return None
    # Provider exposes a date, not an exact expiry time. Use local midnight
    # conservatively and return expires_precision='date' alongside it.
    return int(datetime.strptime(match[1], '%d.%m.%Y').replace(tzinfo=ZONE).timestamp())


def require_free_card(text, category_hrefs):
    if not re.search(r'\bTip\s+Standardni\b', text):
        raise HaloFeeRequired('only_standard_ads_may_be_managed_automatically')
    if not any(any(urlsplit(urljoin(ORIGIN, href)).path.rstrip('/') == prefix or urlsplit(urljoin(ORIGIN, href)).path.startswith(prefix + '/') for prefix in FREE_CATEGORIES)
               and own_origin(urljoin(ORIGIN, href)) for href in category_hrefs):
        raise HaloFeeRequired('category_is_not_verified_free')
    if re.search(r'Čeka plaćanje|Izvrši plaćanje|\b(?:Top|Premium)\s+oglas', text, re.I):
        raise HaloFeeRequired('paid_listing_or_payment_due')


def classify_card(text, pause=False, activate=False):
    lowered = text.casefold()
    if 'stopiran' in lowered:
        raise HaloNeedsReview('provider_stopped_listing')
    if any(word in lowered for word in ('čeka korekturu', 'čeka proveru', 'na proveri', 'moderacij', 'čeka odobren')):
        return 'pending_review'
    if 'istekao' in lowered or 'istekli oglas' in lowered:
        return 'expired'
    if activate and not pause:
        return 'paused'
    if pause and not activate and 'aktivan oglas' in lowered:
        return 'active'
    if 'pauziran' in lowered:
        return 'paused'
    raise HaloNeedsReview('unrecognized_listing_state')


def prepare_payload(payload):
    result = dict(payload)
    ident = result.get('id')
    if isinstance(ident, bool) or not isinstance(ident, int) or ident < 1:
        raise HaloNeedsReview('invalid_local_product_id')
    title = str(result.get('title', '')).strip()
    body = str(result.get('description', '')).strip()
    if not title or len(title) > 50 or not body or len(body) > 1500:
        raise HaloNeedsReview('title_or_description_length')
    if re.search(r'https?://|www\.|reklik\.shop|[\w.+-]+@', title + '\n' + body, re.I):
        raise HaloNeedsReview('external_address_in_ad_copy')
    marker = 'RK-' + str(ident)
    if not re.search(r'\b' + re.escape(marker) + r'\b', body):
        body += '\n\nŠifra: ' + marker
    if len(body) > 1500:
        raise HaloNeedsReview('description_has_no_room_for_unique_sku')
    try:
        price = Decimal(str(result['price_eur']))
        if not price.is_finite() or price <= 0 or price != price.quantize(Decimal('.01')):
            raise ValueError()
    except (KeyError, InvalidOperation, ValueError):
        raise HaloNeedsReview('invalid_public_list_price') from None
    if not result.get('condition') or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', str(result.get('email', ''))):
        raise HaloNeedsReview('missing_condition_or_valid_contact')
    if not 7 <= len(re.sub(r'\D', '', str(result.get('phone', '')))) <= 15:
        raise HaloNeedsReview('invalid_contact_phone')
    photos = result.get('photos')
    if not isinstance(photos, list) or not photos or len(photos) > 20:
        raise HaloNeedsReview('missing_or_excessive_photos')
    result.update(title=title, description=body, price_eur=format(price, 'f'), sku=marker)
    return result


def plain_text(value):
    return ' '.join(html.unescape(re.sub(r'<[^>]*>', ' ', value)).split()).casefold()


class HaloBrowser:
    def __init__(self, *, owned_ids=(), cdp_url='http://127.0.0.1:9336',
                 profile_dir=None, credentials_path=None, lock_path=None,
                 acquire_lock=True, allowed_photo_roots=None, category_routes=None,
                 display=':20', browser_executable=None, state_dir=None, allow_login=True):
        parsed = urlsplit(cdp_url)
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost') or not parsed.port:
            raise HaloNeedsReview('cdp_must_be_loopback')
        self.owned_ids = {validate_remote_id(value) for value in owned_ids}
        self.cdp_url, self.cdp_port = cdp_url, parsed.port
        self.profile_dir = Path(profile_dir or STATE / 'halo-account-browser')
        self.credentials_path = Path(credentials_path or Path.home() / '.secrets/reklik-halooglasi.json')
        self.lock_path = Path(lock_path or STATE / 'halo-ui.lock')
        self.acquire_lock = acquire_lock
        self.allowed_photo_roots = tuple(Path(path).resolve() for path in (allowed_photo_roots or [STATE]))
        self.category_routes = category_routes or {}
        self.display, self.executable = display, browser_executable
        self.state_dir = Path(state_dir or STATE / 'halo-browser')
        self.allow_login = allow_login
        self._lock_fd = self._manager = self._browser = self.page = None
        self._profile_loaded = False
        self._created = {}

    def __enter__(self):
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.acquire_lock:
                self.lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._lock_fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise HaloNeedsReview('halo_ui_busy') from None
            from playwright.sync_api import sync_playwright
            self._manager = sync_playwright().start()
            try:
                self._browser = self._manager.chromium.connect_over_cdp(self.cdp_url, timeout=5000)
            except Exception:
                self._start_browser()
            if not self._browser.contexts:
                raise HaloAuthRequired('persistent_browser_context_missing')
            self.page = self._browser.contexts[0].new_page()
            self.page.set_default_timeout(12000)
            self._profile(force=True)
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.page is not None:
            try:
                self.page.close()
            except Exception:
                pass
            self.page = None
        if self._manager is not None:
            self._manager.stop()  # Disconnect; never browser.close() the owner's Chrome.
            self._manager = None
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def _start_browser(self):
        executable = self.executable or shutil.which('google-chrome') or shutil.which('chromium')
        if not executable:
            raise HaloAuthRequired('normal_browser_unavailable')
        self.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        logfile = self.state_dir / 'chrome.log'
        environment=dict(os.environ, DISPLAY=self.display)
        authority=self.state_dir/'Xauthority'
        if authority.is_file():environment['XAUTHORITY']=str(authority)
        with logfile.open('ab') as output:
            subprocess.Popen([executable, '--user-data-dir=' + str(self.profile_dir),
                              '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=' + str(self.cdp_port),
                              '--no-first-run', '--no-default-browser-check', ORIGIN + '/profil'],
                             env=environment, stdout=output, stderr=output, start_new_session=True)
        logfile.chmod(0o600)
        for _ in range(20):
            try:
                self._browser = self._manager.chromium.connect_over_cdp(self.cdp_url, timeout=1000)
                return
            except Exception:
                time.sleep(.5)
        raise HaloAuthRequired('persistent_browser_could_not_start')

    def _guard_origin(self):
        if not own_origin(self.page.url):
            raise HaloAuthRequired('unexpected_browser_origin')

    def _check_challenge(self):
        title = self.page.title().casefold()
        text = self.page.locator('body').inner_text().casefold()
        if ('just a moment' in title or any(value in text for value in ('performing security verification', 'verify you are human', 'proverite da niste robot'))):
            raise HaloAuthRequired('human_verification_required')

    def _login(self):
        if not self.allow_login:
            raise HaloAuthRequired('login_required')
        self._check_challenge()
        if not self.credentials_path.is_file() or self.credentials_path.stat().st_mode & 0o077:
            raise HaloAuthRequired('private_saved_credentials_unavailable')
        credentials = json.loads(self.credentials_path.read_text())
        username = credentials.get('username') or credentials.get('email')
        if not username or not credentials.get('password'):
            raise HaloAuthRequired('saved_credentials_incomplete')
        try:
            self.page.locator('#EMailOrUsername:visible').fill(username)
            self.page.locator('#Password:visible').fill(credentials['password'])
            self.page.get_by_role('button', name='Uloguj me', exact=True).click()
            self.page.wait_for_url('**/profil**', timeout=20000)
        except Exception:
            self._check_challenge()
            raise HaloAuthRequired('login_or_email_code_required') from None
        finally:
            credentials.clear()

    def _profile(self, force=False):
        if self._profile_loaded and not force and own_origin(self.page.url) and urlsplit(self.page.url).path.startswith('/profil'):
            return
        # goto(the same URL with #tabMyAds) can be a same-document navigation;
        # use a real reload when verifying an AJAX mutation persisted remotely.
        if force and own_origin(self.page.url) and urlsplit(self.page.url).path.startswith('/profil'):
            self.page.reload(wait_until='domcontentloaded', timeout=45000)
        else:
            self.page.goto(ORIGIN + '/profil#tabMyAds', wait_until='domcontentloaded', timeout=45000)
        self._guard_origin()
        self._check_challenge()
        if urlsplit(self.page.url).path.startswith('/prijava'):
            self._login()
        self._guard_origin()
        try:
            self.page.locator('.product-item').first.wait_for(state='visible', timeout=20000)
        except Exception:
            self._check_challenge()
            # An empty account is only accepted when explicitly rendered as empty.
            if not re.search(r'0\s+oglasa|Nemate.*oglas', self.page.locator('body').inner_text(), re.I):
                raise HaloNeedsReview('profile_listing_table_unavailable') from None
        for span in self.page.locator('span').all():
            if span.is_visible() and span.inner_text().strip() == '100' and ''.join(span.locator('..').inner_text().split()) == '20/50/100':
                previous = self.page.locator('.product-item').count()
                span.click()
                if previous == 20:
                    try:
                        self.page.wait_for_function("document.querySelectorAll('.product-item').length > 20", timeout=12000)
                    except Exception:
                        # Twenty could also be the full count; verify no >20 total is advertised.
                        totals = re.findall(r'\b(\d+)\s+oglasa\b', self.page.locator('body').inner_text())
                        if totals and max(map(int, totals)) > 20:
                            raise HaloNeedsReview('profile_pagination_incomplete') from None
                else:
                    self.page.wait_for_timeout(500)
                break
        totals = re.findall(r'\b(\d+)\s+oglasa\b', self.page.locator('body').inner_text())
        if totals and max(map(int, totals)) > 100:
            raise HaloNeedsReview('more_than_100_account_ads_require_pagination_review')
        self._profile_loaded = True

    def _owned(self, remote_id):
        remote_id = validate_remote_id(remote_id)
        if remote_id not in self.owned_ids:
            raise HaloNeedsReview('remote_id_outside_owned_allowlist')
        return remote_id

    def _card_id(self, card):
        ids = {value for link in card.locator('a[href]').all() if (value := edit_id(link.get_attribute('href')))}
        if len(ids) != 1:
            raise HaloNeedsReview('card_has_ambiguous_owned_edit_id')
        return next(iter(ids))

    def _card(self, remote_id):
        self._profile()
        matches = [card for card in self.page.locator('.product-item').all()
                   if card.is_visible() and any(edit_id(link.get_attribute('href')) == remote_id for link in card.locator('a[href]').all())]
        if len(matches) > 1:
            raise HaloNeedsReview('duplicate_owned_card')
        return matches[0] if matches else None

    @staticmethod
    def _controls(card, selector, text=None):
        return [node for node in card.locator(selector).all()
                if node.is_visible() and (text is None or ' '.join(node.inner_text().split()) == text)]

    def _inspect_card(self, card, remote_id):
        text = card.inner_text()
        hrefs = [link.get_attribute('href') for link in card.locator('a[href]').all()]
        require_free_card(text, [href for href in hrefs if href])
        pause = self._controls(card, 'a.toggle-deactivate', 'Pauziraj')
        activate = self._controls(card, 'a.toggle-deactivate', 'Aktiviraj')
        status = classify_card(text, bool(pause), bool(activate))
        urls = {url for href in hrefs if (url := observed_public_url(href, remote_id))}
        if len(urls) > 1:
            raise HaloNeedsReview('ambiguous_observed_public_url')
        renewal = self._controls(card, 'a.toggle-auto-renewal')
        auto = 'active' in (renewal[0].get_attribute('class') or '').split() if len(renewal) == 1 else None
        result = {'status': status, 'remote_id': remote_id, 'url': next(iter(urls)) if urls else None,
                  'auto_renew': auto, 'expires_at': expiry_from_text(text), 'expires_precision': 'date', 'fee_rsd': 0}
        return result

    def inspect(self, remote_id, public_url=None):
        remote_id = self._owned(remote_id)
        card = self._card(remote_id)
        if card is None:
            return {'status': 'missing', 'remote_id': remote_id, 'url': None, 'auto_renew': None, 'expires_at': None}
        return self._inspect_card(card, remote_id)

    def _require_free(self, card):
        require_free_card(card.inner_text(), [link.get_attribute('href') for link in card.locator('a[href]').all() if link.get_attribute('href')])

    def _click_owned(self, remote_id, selector, text=None):
        self._guard_origin()
        card = self._card(remote_id)
        if card is None:
            raise HaloNeedsReview('known_listing_missing_never_recreate')
        self._require_free(card)
        controls = self._controls(card, selector, text)
        if len(controls) != 1:
            raise HaloNeedsReview('owned_action_is_missing_or_ambiguous')
        dialogs = []
        def refuse(dialog):
            dialogs.append(dialog.message)
            dialog.dismiss()
        self.page.on('dialog', refuse)
        try:
            controls[0].click()
            self.page.wait_for_timeout(900)
        finally:
            self.page.remove_listener('dialog', refuse)
        if dialogs:
            raise HaloNeedsReview('unexpected_action_confirmation')
        self._profile(force=True)

    def _set_auto_renew(self, remote_id, enabled):
        state = self.inspect(remote_id)
        if state['auto_renew'] is enabled:
            return state
        card = self._card(remote_id)
        if card is None:
            raise HaloNeedsReview('known_listing_missing_never_recreate')
        self._require_free(card)
        if len(self._controls(card, 'a.toggle-auto-renewal')) != 1:
            raise HaloNeedsReview('auto_renewal_control_unavailable')
        self._click_owned(remote_id, 'a.toggle-auto-renewal')
        after = self.inspect(remote_id)
        if after['auto_renew'] is not enabled:
            raise HaloNeedsReview('auto_renewal_change_not_verified')
        return dict(after, fee_rsd=0)

    def deactivate(self, remote_id, url=None):
        remote_id = self._owned(remote_id)
        state = self.inspect(remote_id, url)
        if state['status'] == 'missing':
            return state
        if state['auto_renew'] is True:
            state = self._set_auto_renew(remote_id, False)
        if state['status'] in ('paused', 'expired'):
            return state
        if state['status'] == 'pending_review':
            return dict(state, pending_pause=True)
        self._click_owned(remote_id, 'a.toggle-deactivate', 'Pauziraj')
        state = self.inspect(remote_id)
        if state['status'] != 'paused':
            raise HaloNeedsReview('pause_not_verified')
        return dict(state, fee_rsd=0)

    def activate(self, remote_id):
        remote_id = self._owned(remote_id)
        state = self.inspect(remote_id)
        if state['status'] == 'active':
            return state
        if state['status'] != 'paused':
            raise HaloNeedsReview('only_paused_owned_ads_can_be_reactivated')
        self._click_owned(remote_id, 'a.toggle-deactivate', 'Aktiviraj')
        state = self.inspect(remote_id)
        if state['status'] not in ('active', 'pending_review'):
            raise HaloNeedsReview('activation_not_verified')
        return dict(state, fee_rsd=0)

    def ensure_renewal(self, remote_id):
        remote_id = self._owned(remote_id)
        state = self.inspect(remote_id)
        if state['status'] == 'active':
            return self._set_auto_renew(remote_id, True)
        if state['status'] == 'pending_review':
            return state
        if state['status'] != 'expired':
            raise HaloNeedsReview('renewal_requires_active_or_expired_owned_ad')
        card = self._card(remote_id)
        self._require_free(card)
        previous_expiry = state['expires_at']
        extend = self._controls(card, 'a', 'Produži')
        if len(extend) != 1:
            raise HaloNeedsReview('expired_ad_has_no_verified_extend_action')
        href = extend[0].get_attribute('href') or ''
        target = urljoin(ORIGIN, href)
        if href.startswith(('javascript:', '#')) or not own_origin(target):
            raise HaloNeedsReview('expired_renewal_has_no_verified_zero_cost_preview')
        self._guard_origin()
        extend[0].click()
        self._profile_loaded = False
        self._free_step(auto_renew=True)
        self._submit_free(remote_id=remote_id)
        state = self.inspect(remote_id)
        if state['status'] not in ('active', 'pending_review') or (state['status'] == 'active' and state['expires_at'] == previous_expiry):
            raise HaloNeedsReview('same_id_expiry_extension_not_verified')
        return dict(state, fee_rsd=0)

    def _visible_text(self, text):
        matches = [node for node in self.page.get_by_text(text, exact=True).all() if node.is_visible()]
        if len(matches) != 1:
            raise HaloNeedsReview('expected_category_or_button_missing')
        matches[0].click()

    def _open_create_form(self, payload):
        self.page.goto(ORIGIN + '/postavite-oglas', wait_until='domcontentloaded', timeout=45000)
        self._guard_origin()
        self._check_challenge()
        self.page.locator('#next').wait_for(state='attached', timeout=20000)
        category = payload.get('category_slug', '')
        if category in self.category_routes:
            route = self.category_routes[category]
        elif category == 'telefoni' or payload.get('category_icon') == 'phone':
            brand = str(payload.get('brand', '')).split(' ')[0].casefold()
            manufacturers = {'apple':'Apple iPhone','samsung':'Samsung','huawei':'Huawei','xiaomi':'Xiaomi','redmi':'Xiaomi','poco':'Xiaomi','nokia':'Nokia','motorola':'Motorola','sony':'Sony','zte':'ZTE','lenovo':'Lenovo','lg':'LG','htc':'HTC','alcatel':'Alcatel'}
            route = ['Telefoni', manufacturers.get(brand, 'Ostali mobilni telefoni')]
        elif category == 'laptopovi' or payload.get('category_icon') == 'laptop':
            route = ['Računari', 'Laptop računari']
        elif category == 'tableti' or payload.get('category_icon') == 'tablet':
            route = ['Računari', 'Tablet računari']
        elif re.search(r'dock', payload['title'], re.I):
            route = ['Računari', 'Oprema za računare', 'Ostala oprema za računare']
        elif re.search(r'media player|medija plejer|digital signage', payload['title'], re.I) or payload.get('category_icon') == 'desktop':
            route = ['Računari', 'Desktop računari']
        elif category == 'oprema':
            # The shop's general technology-accessory category has an honest
            # catch-all; an administrator can override it for a narrower group.
            route = ['Računari', 'Oprema za računare', 'Ostala oprema za računare']
        else:
            raise HaloNeedsReview('category_needs_an_explicit_ui_mapping')
        for category_name in route:
            self._visible_text(category_name)
            self.page.wait_for_timeout(250)
        for _ in range(3):
            if self.page.locator('#next').is_visible():
                break
            fallback = self.page.locator('a:visible').filter(has_text=re.compile(r'^\s*Ostali.*(?:mobilni|telefoni)\s*$'))
            if not fallback.count():
                raise HaloNeedsReview('no_accurate_catch_all_model_category')
            fallback.last.click()
            self.page.wait_for_timeout(250)
        self.page.locator('#next:visible').click()
        self.page.locator('input[name=Title]').wait_for(state='visible', timeout=25000)
        self._profile_loaded = False

    def _photos(self, paths):
        from PIL import Image
        output = []
        for raw in paths:
            path = Path(raw)
            if not path.is_absolute():
                raise HaloNeedsReview('photo_path_must_be_absolute')
            path = path.resolve()
            if not any(path.is_relative_to(root) for root in self.allowed_photo_roots) or not path.is_file():
                raise HaloNeedsReview('photo_outside_owned_media_roots')
            with Image.open(path) as source:
                if source.format not in ('JPEG', 'PNG', 'WEBP'):
                    raise HaloNeedsReview('unsupported_photo_type')
                source.load()
                if source.format != 'WEBP':
                    output.append(str(path))
                    continue
                directory = self.state_dir / 'converted-photos'
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                converted = directory / (hashlib.sha256(path.read_bytes()).hexdigest() + '.png')
                if not converted.exists():
                    source.save(converted, format='PNG')
                    converted.chmod(0o600)
                with Image.open(converted) as target:
                    target.load()
                    if target.mode != source.mode or target.size != source.size or target.tobytes() != source.tobytes():
                        raise HaloNeedsReview('photo_conversion_changed_decoded_pixels')
                output.append(str(converted))
        return output

    def _optional_select(self, field, label):
        control = self.page.locator('select[name="' + field + '"]')
        if control.count() and control.is_visible() and label in control.locator('option').all_text_contents():
            control.select_option(label=label)

    def _email_control(self):
        # Edit pages also contain a hidden registration widget with id=Email.
        # Bind the contact email to this ad's unique PhoneNumber1 section.
        phone = self.page.locator('#PhoneNumber1')
        if phone.count() != 1:
            raise HaloNeedsReview('ambiguous_ad_phone_section')
        section = phone.locator('xpath=ancestor::*[.//input[@id="Email"]][1]')
        email = section.locator('input#Email')
        if email.count() != 1:
            raise HaloNeedsReview('ambiguous_ad_contact_email')
        return email

    def _remove_photos(self):
        buttons=self.page.locator('#dropzone-previews a.btn-remove-image:visible')
        # Removing an item shifts subsequent locator indices; always remove the
        # current first item instead of retaining a list of nth() locators.
        for _ in range(20):
            if not buttons.count():break
            buttons.first.click()
        self.page.wait_for_function("Array.from(document.querySelectorAll('#dropzone-previews .uploaded-image-item')).filter(e=>e.offsetWidth||e.offsetHeight).length === 0",timeout=20000)

    def _fill(self, payload, editing=False):
        self._guard_origin()
        replace_photos = not editing or payload.get('replace_photos', True)
        photos = self._photos(payload['photos']) if replace_photos else []
        if replace_photos and self.page.locator('input[type=file]').count() != 1:
            raise HaloNeedsReview('photo_upload_control_is_ambiguous')
        self._email_control()  # Validate the contact section before changing any field.
        self.page.locator('input[name=Title]').fill(payload['title'])
        brand = str(payload.get('brand', '')).strip().split(' ')[0]
        for field in ('proizvodjac', 'marka', 'brend'):
            self._optional_select(field, brand)
        self._optional_select('stanje', 'Novo' if payload['condition'] == 'Novo' else 'Polovno')
        self.page.locator('[contenteditable=true]').first.fill(payload['description'])
        self.page.locator('[contenteditable=true]').first.press('Tab')
        self.page.locator('input[name=cena]').fill(payload['price_eur'])
        self.page.locator('select[name=cena_unit]').select_option(label='EUR')
        if not editing:
            location = self.page.locator('input[name=grad]')
            container = location.evaluate('(e)=>e.previousElementSibling?.id')
            if not container or not container.startswith('s2id_'):
                raise HaloNeedsReview('location_widget_unrecognized')
            self.page.locator('#' + container).click()
            self.page.locator('[role=combobox]:visible').last.fill('Beograd')
            self.page.locator('.select2-result-selectable').filter(has_text=re.compile(r'^Beograd$')).first.click()
        if replace_photos:
            if editing:
                self._remove_photos()
            self.page.locator('input[type=file]').set_input_files(photos, timeout=90000)
            self.page.wait_for_function('''count => {
              const nodes=Array.from(document.querySelectorAll('#dropzone-previews .uploaded-image-item'));
              const visible=nodes.filter(e=>e.offsetWidth||e.offsetHeight);
              return visible.length===count && document.querySelectorAll('#dropzone-previews .dz-error').length===0
                && document.querySelectorAll('#dropzone-previews .dz-processing:not(.dz-complete)').length===0;
            }''', arg=len(photos), timeout=120000)
        self.page.locator('#PhoneNumber1').fill(payload['phone'])
        self.page.locator('#PhoneNumber2').fill('')
        self._email_control().fill(payload['email'])
        show = self.page.locator('#show-phone-auto')
        if show.count() and not show.is_checked():
            self.page.locator('label[for=show-phone-auto]').click()
        self._verify_form(payload)

    def _verify_form(self, payload):
        if (self.page.locator('input[name=Title]').input_value() != payload['title']
                or not same_phone(self.page.locator('#PhoneNumber1').input_value(), payload['phone'])
                or self._email_control().input_value() != payload['email']
                or plain_text(payload['description']) != plain_text(self.page.locator('textarea[name=TextHtml]').input_value())
                or Decimal(self.page.locator('input[name=cena]').input_value().replace(',', '.')) != Decimal(payload['price_eur'])):
            raise HaloNeedsReview('form_did_not_retain_verified_item_and_contacts')

    def _verify_saved_edit(self,payload):
        # The title is server rendered, but the saved gallery/editor hydrate
        # asynchronously. A visible title alone does not prove readback is ready.
        deadline=time.monotonic()+20
        failure='edited_photo_count_not_verified'
        while True:
            try:
                self._verify_form(payload)
                if self.page.locator('#dropzone-previews .uploaded-image-item:visible').count()==len(payload['photos']):
                    return
                failure='edited_photo_count_not_verified'
            except HaloNeedsReview as error:
                if str(error)!='form_did_not_retain_verified_item_and_contacts':
                    raise
                failure=str(error)
            if time.monotonic()>=deadline:
                raise HaloNeedsReview(failure)
            self.page.wait_for_timeout(150)

    def _free_step(self, auto_renew=False):
        self._guard_origin()
        standard = self.page.locator('label[for=IsBought3]')
        standard.wait_for(state='visible', timeout=25000)
        if standard.inner_text().strip() != 'Standardni oglas':
            raise HaloFeeRequired('standard_tier_not_verified')
        standard.click()
        self.page.wait_for_timeout(350)
        total = self.page.locator('.total-amount:visible').inner_text().strip()
        if total not in ('0,00', '0.00', '0') or not self.page.locator('#IsBought3').is_checked():
            raise HaloFeeRequired('nonzero_or_unknown_final_fee')
        for identifier in ('IsBought1', 'IsBought2'):
            node = self.page.locator('#' + identifier)
            if node.count() and node.is_checked():
                raise HaloFeeRequired('paid_tier_selected')
        for option in self.page.locator('input[name=HasAutomaticRenewal]').all():
            if option.is_visible():
                option.set_checked(auto_renew)
            elif option.is_checked() != auto_renew:
                label = self.page.locator('label[for="' + option.get_attribute('id') + '"]')
                if label.count() != 1:
                    raise HaloNeedsReview('renewal_checkbox_cannot_be_verified')
                label.click()
            if option.is_checked() != auto_renew:
                raise HaloNeedsReview('renewal_checkbox_not_retained')
        trader = self.page.locator('input[name=IsAdvertiserTrader][value=true]')
        if trader.count() and trader.is_visible():
            trader.check()
        terms = self.page.locator('#AcceptTermsAndCondition')
        if terms.count():
            terms.check()
        if self.page.locator('.total-amount:visible').inner_text().strip() not in ('0,00', '0.00', '0'):
            raise HaloFeeRequired('fee_changed_after_options')

    def _submit_free(self, remote_id=None):
        self._guard_origin()
        if self.page.locator('.total-amount:visible').inner_text().strip() not in ('0,00', '0.00', '0'):
            raise HaloFeeRequired('last_moment_nonzero_fee')
        try:
            self._submission_attempted = True
            self.page.locator('button.submit-product:visible').click()
            self.page.get_by_text('Uspešno ste predali internet oglas.', exact=False).wait_for(timeout=35000)
        except Exception:
            raise HaloSubmissionUncertain('submission_may_have_succeeded_never_repeat_creation') from None
        try:
            self._profile_loaded = False
            self._profile(force=True)
        except Exception:
            raise HaloSubmissionUncertain('post_submission_lookup_needs_reconciliation') from None

    def _existing_candidate(self, payload):
        self._profile()
        matching = []
        for card in self.page.locator('.product-item').all():
            titles = [node for node in card.get_by_text(payload['title'], exact=True).all() if node.is_visible()]
            if titles:
                matching.append(self._card_id(card))
        if not matching:
            return None
        if len(set(matching)) != 1:
            raise HaloNeedsReview('ambiguous_existing_title_never_create_duplicate')
        remote = matching[0]
        # Read the ordinary edit UI to match the stable SKU, rather than adopting
        # another physical device that happens to have the same model/title.
        self.page.goto(ORIGIN + '/izmeni-oglas/' + remote, wait_until='domcontentloaded', timeout=45000)
        self._guard_origin()
        self.page.locator('input[name=Title]').wait_for(state='visible', timeout=25000)
        text = self.page.locator('textarea[name=TextHtml]').input_value()
        self._profile_loaded = False
        if not re.search(r'\b' + re.escape(payload['sku']) + r'\b', text):
            raise HaloNeedsReview('existing_title_has_different_or_unconfirmed_sku')
        self.owned_ids.add(remote)
        return dict(self.inspect(remote), adopted=True, fee_rsd=0)

    def find_existing(self, payload):
        """Read-only reconciliation; never submit when an earlier attempt is uncertain."""
        return self._existing_candidate(prepare_payload(payload))

    def create(self, payload):
        payload = prepare_payload(payload)
        if payload.get('remote_id') is not None:
            raise HaloNeedsReview('known_id_must_be_updated_or_renewed_never_created')
        if payload['id'] in self._created:
            return dict(self.inspect(self._created[payload['id']]), adopted=True, fee_rsd=0)
        existing = self._existing_candidate(payload)
        if existing:
            self._created[payload['id']] = existing['remote_id']
            return existing
        self._open_create_form(payload)
        self._fill(payload)
        self.page.locator('a.next-step.go-to-step-3:visible').click()
        self._free_step(auto_renew=False)
        self._verify_form(payload)
        self._submission_attempted = False
        try:
            self._submit_free()
            matches = []
            for card in self.page.locator('.product-item').all():
                if any(node.is_visible() for node in card.get_by_text(payload['title'], exact=True).all()):
                    matches.append(self._card_id(card))
            if len(set(matches)) != 1:
                raise HaloSubmissionUncertain('submitted_listing_id_not_unambiguously_observed')
            remote = matches[0]
            self.owned_ids.add(remote)
            self._created[payload['id']] = remote
            return dict(self.inspect(remote), fee_rsd=0)
        except HaloSubmissionUncertain:
            raise
        except Exception:
            if self._submission_attempted:
                raise HaloSubmissionUncertain('post_submission_reconciliation_failed') from None
            raise

    def update(self, remote_id, payload):
        remote_id = self._owned(remote_id)
        payload = prepare_payload(payload)
        state = self.inspect(remote_id)
        if state['status'] == 'missing':
            raise HaloNeedsReview('known_id_missing_never_recreate')
        card = self._card(remote_id)
        self._require_free(card)
        self.page.goto(ORIGIN + '/izmeni-oglas/' + remote_id, wait_until='domcontentloaded', timeout=45000)
        self._guard_origin()
        self.page.locator('input[name=Title]').wait_for(state='visible', timeout=25000)
        ids = self.page.locator('input[name=Id]').all()
        if not any(node.input_value() == remote_id for node in ids):
            raise HaloNeedsReview('edit_form_remote_id_mismatch')
        # The exact edit-save control is verified against a live unchanged item
        # before this adapter is deployed; no fallback to a create button.
        save = self.page.locator('button.save-changes:visible')
        save.first.wait_for(state='visible', timeout=25000)
        if not save.count() or any(node.inner_text().strip() != 'Snimi izmene' for node in save.all()):
            raise HaloNeedsReview('edit_save_control_requires_review')
        self._profile_loaded = False
        self._fill(payload, editing=True)
        try:
            save.first.click()
            self.page.wait_for_timeout(900)
            self._profile(force=True)
        except Exception:
            raise HaloSubmissionUncertain('known_id_update_requires_reconciliation') from None
        after = self.inspect(remote_id)
        if after['status'] == 'missing':
            raise HaloNeedsReview('edited_same_id_not_observed')
        self.page.goto(ORIGIN + '/izmeni-oglas/' + remote_id, wait_until='domcontentloaded', timeout=45000)
        self._guard_origin()
        self.page.locator('input[name=Title]').wait_for(state='visible', timeout=25000)
        self._verify_saved_edit(payload)
        self._profile_loaded = False
        return dict(after, fee_rsd=0)
