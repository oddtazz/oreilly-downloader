# /// script
# dependencies = [
#   "aiohttp",
#   "lxml",
# ]
# ///
"""Download O'Reilly books as verified epub files.

Quick start:  oreilly-dl --setup   then   oreilly-dl <isbn|url> ...   or   oreilly-dl -f list.txt
Run `oreilly-dl -h` for all options.

* Runs only with an active orm-jwt. If the token has under 5 minutes left
  and an orm-rt is stored, it waits for expiry (O'Reilly will not renew a
  live token), renews orm-jwt itself and saves it. It then makes a live auth call before any download. No usable
  token means exit 2.
* Each file is checked against the size the API reports. HTML chapters that
  come back short (the silent truncation O'Reilly does for unauthenticated
  requests) fail the book.
* The epub is written to a temp file and moved into place only after it
  passes verification. A failed book leaves no file behind.
* Full books only. O'Reilly serves a preview (each chapter cut short, still
  HTTP 200) for titles the account cannot read. A book passes only if every
  chapter is complete, the HTML total is >= 93% of the declared size, the file
  list matches the book's declared total size, and it is not an Early Release
  (rough cut) unless --allow-early-release is given.
* Alerts go out only when a book fails (Telegram DM via `hermes send`).
  Success is silent apart from stdout.

Exit codes: 0 all books OK, 1 one or more books failed, 2 token problem.
"""

import argparse
import asyncio
import base64
import fcntl
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import aiohttp
from lxml import etree
from lxml import html as lhtml

BASE_URL = 'https://learning.oreilly.com'
REPO_DIR = Path(__file__).resolve().parent
CONFIG_FILE = Path(os.environ.get('OREILLY_CONFIG', REPO_DIR / 'oreilly.conf'))  # gitignored
REFRESH_URL = BASE_URL + '/profile/'   # returns new orm-jwt when sent orm-rt + last orm-jwt
REFRESH_MARGIN = int(os.environ.get('OREILLY_REFRESH_MARGIN', 180))  # renew below this
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/141.0 Safari/537.36')
OUT_DIR = REPO_DIR / 'books'   # overridden by -o, $OREILLY_DL_DIR or OUT_DIR= in oreilly.conf
ALERT_TARGET = 'telegram'      # overridden by $OREILLY_ALERT_TARGET or ALERT_TARGET= in oreilly.conf
MIN_TOKEN_SECS = 60         # refuse a token with less life left than this
CONCURRENCY = 2             # parallel file downloads per book; a reader loads ~1-2 at a time
RETRIES = 5
BOOK_PAUSE = (45, 120)      # random pause between books, seconds (like a person moving on)
MAX_BOOKS = 10              # books per run unless --max-books says otherwise
COOL_OFF = 300              # first throttle (HTTP 403/429): stop everything for 5 minutes


class Throttled(Exception):
    """O'Reilly is rate-limiting us. Stop the run instead of pushing through."""


class Pace:
    """Shared rate-limit state for a run.

    Polite by design: few parallel requests, long random gaps between books,
    a cap on books per run. On the first throttle response, everything pauses
    for COOL_OFF and the book is retried once. A second throttle in the same
    run ends the run; remaining books are left for a later run (books on disk
    are skipped, so re-running resumes).
    """

    def __init__(self):
        self.throttles = 0
        self.cool_end = 0.0
        self.lock = asyncio.Lock()

    async def hit(self, url, status, sent_at):
        async with self.lock:
            if sent_at < self.cool_end:
                return      # same throttle event as a parallel request; cool-off already done
            self.throttles += 1
            if self.throttles > 1:
                raise Throttled(f'HTTP {status} again; O\'Reilly is rate-limiting this account')
            say(f'O\'Reilly answered HTTP {status} (rate limit). Pausing all downloads for '
                f'{COOL_OFF // 60} min, then retrying once.')
            await countdown(COOL_OFF, 'cool-off')
            self.cool_end = time.time()


PACE = Pace()
HTML_MIN_RATIO = 0.85       # downloaded/declared size for HTML chapters
HTML_SMALL = 4096           # below this, HTML size varies too much to compare
BOOK_MIN_RATIO = 0.93       # downloaded/declared size for all HTML in the book
TOTAL_SIZE_SLACK = 0.02     # file list vs book total_size tolerance

CONTAINER = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
    <rootfiles>
        <rootfile full-path="EPUB/content.opf" media-type="application/oebps-package+xml"/>
    </rootfiles>
</container>
"""  # noqa


class TokenError(Exception):
    pass


class BookError(Exception):
    pass




# --- token -----------------------------------------------------------------

def read_conf(key):
    """Read KEY=value from oreilly.conf. Blank lines and # comments are ignored."""
    if not CONFIG_FILE.exists():
        return ''
    for line in CONFIG_FILE.read_text().splitlines():
        k, sep, v = line.partition('=')
        if sep and not line.lstrip().startswith('#') and k.strip() == key:
            return v.strip().strip('"').strip("'")
    return ''


def write_conf(key, value):
    """Set KEY=value in oreilly.conf, keeping comments and other lines. Mode 600."""
    lines = CONFIG_FILE.read_text().splitlines() if CONFIG_FILE.exists() else []
    out, done = [], False
    for line in lines:
        k, sep, _ = line.partition('=')
        if sep and not line.lstrip().startswith('#') and k.strip() == key:
            out.append(f'{key}={value}')
            done = True
        else:
            out.append(line)
    if not done:
        out.append(f'{key}={value}')
    tmp = CONFIG_FILE.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(out) + '\n')
    os.replace(tmp, CONFIG_FILE)


def load_jwt(arg):
    jwt = (arg or os.environ.get('ORM_JWT') or read_conf('ORM_JWT')).strip()
    if not jwt:
        raise TokenError(f'no ORM_JWT found. Paste it into {CONFIG_FILE} '
                         '(copy oreilly.conf.example if the file does not exist)')
    return jwt


def load_rt():
    return (os.environ.get('ORM_RT') or read_conf('ORM_RT')).strip()


async def refresh_jwt(jwt, rt):
    """Swap orm-rt + last orm-jwt (expired is fine) for a new orm-jwt.

    learning.oreilly.com renews the session on a normal page load: send both
    cookies to /profile/ and it answers with Set-Cookie orm-jwt (and orm-rt).
    orm-rt alone is not enough; the server needs the previous orm-jwt too.
    """
    if not rt:
        raise TokenError(f'orm-jwt needs renewal but no orm-rt (set ORM_RT in {CONFIG_FILE})')
    headers = {'Cookie': f'orm-rt={rt}; orm-jwt={jwt}', 'User-Agent': UA,
               'Accept': 'text/html'}
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        async with s.get(REFRESH_URL, headers=headers, allow_redirects=False) as r:
            status = r.status
            got = {}
            for h in r.headers.getall('Set-Cookie', []):
                name, _, rest = h.partition('=')
                if name.strip() in ('orm-jwt', 'orm-rt'):
                    got[name.strip()] = rest.split(';', 1)[0]
    new_jwt = got.get('orm-jwt')
    if not new_jwt:
        raise TokenError(f'could not renew the login (HTTP {status}). orm-rt is probably '
                         'revoked (logout, password change or new login). Log in to '
                         'learning.oreilly.com, copy the orm-rt and orm-jwt cookies, and '
                         f'paste them into {CONFIG_FILE}')
    jwt_expiry(new_jwt)  # raises if the server sent something odd
    write_conf('ORM_JWT', new_jwt)
    if got.get('orm-rt') and got['orm-rt'] != rt:
        write_conf('ORM_RT', got['orm-rt'])
    return new_jwt, got.get('orm-rt') or rt


class Tokens:
    """One place that owns orm-jwt/orm-rt for a run.

    Safe against the token changing under a long run:
    * renewal holds an exclusive lock on oreilly.conf.lock, so two runs never
      renew at the same time;
    * before renewing, it re-reads oreilly.conf. If someone else already
      renewed (another run) or pasted new cookies (--setup / hand edit), it
      uses those instead of its in-memory copy;
    * the download session's cookie is updated in place after every change.
    """

    def __init__(self, jwt, rt):
        self.jwt, self.rt = jwt, rt
        self.exp = _exp_or_zero(jwt)
        self.session = None
        self.changes = 0            # bumps every time the jwt in use changes
        self._lock = asyncio.Lock()

    def _apply(self, jwt, rt):
        if jwt != self.jwt:
            self.changes += 1
        self.jwt, self.rt, self.exp = jwt, rt, _exp_or_zero(jwt)
        if self.session is not None:
            self.session.cookie_jar.update_cookies({'orm-jwt': jwt})

    def _adopt_from_conf(self, margin):
        """Use cookies from oreilly.conf if they are newer than ours."""
        if os.environ.get('ORM_JWT'):
            return False
        cj, cr = read_conf('ORM_JWT'), read_conf('ORM_RT')
        changed = False
        if cr and cr != self.rt:
            say('orm-rt changed in the config; using the new one')
            self.rt, changed = cr, True
        if cj and cj != self.jwt and _exp_or_zero(cj) > self.exp:
            self._apply(cj, self.rt)
            changed = True
            if self.exp - time.time() >= margin:
                say(f'Picked up a newer login token from the config (valid until {fmt_ts(self.exp)}).')
        return changed

    async def fresh(self, margin=REFRESH_MARGIN):
        """Make sure the token has at least `margin` seconds left."""
        async with self._lock:
            if self.exp - time.time() >= margin:
                return
            with _conf_lock():
                self._adopt_from_conf(margin)
                if self.exp - time.time() >= margin:
                    return
                jwt, rt, exp = await ensure_fresh(self.jwt, self.rt, margin)
                self._apply(jwt, rt)



def _exp_or_zero(jwt):
    try:
        return jwt_expiry(jwt)
    except TokenError:
        return 0


class _conf_lock:
    """Exclusive flock on oreilly.conf.lock across processes."""

    def __enter__(self):
        path = CONFIG_FILE.with_name(CONFIG_FILE.name + '.lock')
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)


async def ensure_fresh(jwt, rt, margin=REFRESH_MARGIN):
    """Return (jwt, rt, exp) with at least `margin` seconds of life left.

    O'Reilly only issues a new orm-jwt once the current one has expired
    (seen: exp 15:38:35, no renewal at 15:38:17, renewed at 15:39:18). So if
    the token is close to expiry, wait it out, then renew.
    """
    try:
        exp = jwt_expiry(jwt)
    except TokenError:
        exp = 0
    left = exp - time.time()
    if left >= margin:
        return jwt, rt, exp
    if left > 0:
        if not rt:
            raise TokenError(f'orm-jwt expires in {int(left)}s and no orm-rt to renew it')
        say(f'Login token expires in {int(left)}s. O\'Reilly only renews an expired '
            'token, so waiting for it...')
        await countdown(left + 3, 'token renewal')
    for attempt in range(4):
        try:
            jwt, rt = await refresh_jwt(jwt, rt)
            break
        except TokenError:
            if attempt == 3:
                raise
            await asyncio.sleep(15)
    exp = check_expiry(jwt)
    say(f'Login renewed, valid until {fmt_ts(exp)}.')
    return jwt, rt, exp


def jwt_expiry(jwt):
    try:
        payload = jwt.split('.')[1]
        payload += '=' * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))['exp'])
    except Exception as e:
        raise TokenError(f'orm-jwt is not a valid JWT: {e}') from None


def fmt_ts(ts):
    return time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(ts))


def check_expiry(jwt):
    exp = jwt_expiry(jwt)
    left = exp - time.time()
    if left < MIN_TOKEN_SECS:
        state = 'expired' if left <= 0 else f'expires in {int(left)}s'
        raise TokenError(f'orm-jwt {state} (exp {fmt_ts(exp)}); get a fresh cookie')
    return exp


async def check_auth(session):
    """401 = dead token. 403/429 = usually rate limiting, so retry first."""
    url = BASE_URL + '/api/v1/user-preferences/'
    status = None
    for attempt in range(RETRIES):
        async with session.get(url) as r:
            status = r.status
            if status == 200:
                return (await r.json()).get('username', '?')
            if status == 401:
                break
            await asyncio.sleep(2 ** attempt)
    raise TokenError(f'O\'Reilly rejected the login (HTTP {status}). Paste fresh orm-rt '
                     f'and orm-jwt cookies into {CONFIG_FILE}')



# --- output ----------------------------------------------------------------
# Everything human-facing goes to stdout, unbuffered, so it shows in a
# terminal, through pipes and in log files. --quiet silences it; --json
# sends it to stderr so stdout stays machine-readable.

OUT = sys.stdout
QUIET = False


def say(msg):
    if not QUIET:
        print(msg, file=OUT, flush=True)


def _fmt_secs(s):
    s = int(s)
    return f'{s // 60}m{s % 60:02d}s' if s >= 60 else f'{s}s'


async def countdown(secs, what):
    """Sleep, showing time left on a live line (or a line every 30 s if not a tty)."""
    end = time.time() + secs
    tty = OUT.isatty() and not QUIET
    last = 0
    while (left := end - time.time()) > 0:
        if tty:
            print(f'\r  waiting {_fmt_secs(left)} ({what})   ', end='', file=OUT, flush=True)
        elif not QUIET and time.time() - last >= 30:
            say(f'  waiting {_fmt_secs(left)} ({what})')
            last = time.time()
        await asyncio.sleep(min(1, left))
    if tty:
        print('\r' + ' ' * 50 + '\r', end='', file=OUT, flush=True)


class Progress:
    """Per-book progress: live bar on a terminal, 25% steps otherwise."""

    def __init__(self, files, total_bytes, label):
        self.files, self.total, self.label = files, max(total_bytes, 1), label
        self.n = self.got = 0
        self.t0 = time.time()
        self.tty = OUT.isatty() and not QUIET
        self.next_pct = 25

    def step(self, nbytes):
        self.n += 1
        self.got += nbytes
        pct = min(100, self.got * 100 // self.total)
        if self.tty:
            bar = '#' * (pct // 5) + '-' * (20 - pct // 5)
            print(f'\r{self.label}  [{bar}] {pct:3d}%  {self.n}/{self.files} files  '
                  f'{self.got / 1e6:.1f} MB', end='', file=OUT, flush=True)
        elif not QUIET and pct >= self.next_pct:
            say(f'{self.label}  {pct}%  ({self.n}/{self.files} files)')
            self.next_pct = (pct // 25 + 1) * 25

    def done(self):
        if self.tty:
            print('\r' + ' ' * 90 + '\r', end='', file=OUT, flush=True)


# --- download --------------------------------------------------------------

REQUEST_MARGIN = 20   # renew before any request if the token has less than this left


async def get(session, url, *, as_json=False, tokens=None):
    """GET with retry on rate limits and server errors. Auth errors are fatal.

    O'Reilly does not reject an expired orm-jwt on book files: it answers 200
    with a cut-short preview. So the token is checked before every request
    and renewed if it is about to expire.
    """
    status = None
    for attempt in range(RETRIES):
        if tokens is not None:
            await tokens.fresh(REQUEST_MARGIN)
        sent_at = time.time()
        async with session.get(url) as r:
            status = r.status
            if status == 200:
                return await (r.json() if as_json else r.read())
            if status == 401:
                raise TokenError(f'HTTP 401 on {url}; token died mid-run')
            if status in (403, 429):
                await PACE.hit(url, status, sent_at)
                continue
            if status >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            raise BookError(f'HTTP {status} on {url}')
    raise BookError(f'gave up after {RETRIES} tries on {url} (last HTTP {status})')


def to_xhtml(s, root_path):
    tree = lhtml.fromstring(s, parser=lhtml.HTMLParser(encoding='utf-8'))

    for el in list(tree.iter()):
        for attr in ['href', 'src']:
            if (el.get(attr) or '').startswith(root_path):
                el.set(attr, el.get(attr).removeprefix(root_path))

    if tree.tag != 'html':
        wrapper = etree.Element('html', nsmap={
            None: 'http://www.w3.org/1999/xhtml',
            'epub': 'http://www.idpf.org/2007/ops',
        })

        h1 = tree.find('.//h1')
        if h1 is not None:
            head = etree.SubElement(wrapper, 'head')
            title = etree.SubElement(head, 'title')
            title.text = ''.join(h1.itertext()).strip()

        body = etree.SubElement(wrapper, 'body')
        body.append(tree)
        tree = wrapper

    return etree.tostring(
        tree,
        xml_declaration=True,
        doctype='<!DOCTYPE html>',
        pretty_print=True,
        encoding='utf-8',
    )


def size_problem(entry, raw):
    """Return a reason string if the downloaded bytes look incomplete."""
    want, got = entry.get('file_size') or 0, len(raw)
    if got == 0:
        return 'empty'
    if entry['full_path'].endswith(('.html', '.xhtml')):
        if want >= HTML_SMALL and got < want * HTML_MIN_RATIO:
            return f'truncated ({got}/{want} bytes)'
    elif want and got != want:
        return f'size {got} != {want}'
    return None


async def fetch_book(book_id, session, *, allow_early=False, tokens=None, label=''):
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'
    meta = await get(session, f'{BASE_URL}/api/v2/epubs/urn:orm:book:{book_id}/',
                     as_json=True, tokens=tokens)
    if meta.get('content_format') not in (None, 'book'):
        raise BookError(f"not a book (content_format={meta.get('content_format')})")
    if meta.get('roughcut') and not allow_early:
        raise BookError('Early Release (rough cut): book is unfinished; '
                        'use --allow-early-release to take it anyway')

    entries, expected, url = [], None, BASE_URL + root_path + '?limit=200'
    while url:
        page = await get(session, url, as_json=True, tokens=tokens)
        expected = page.get('count')
        entries.extend(page.get('results', []))
        url = page.get('next')
    if expected is None or len(entries) != expected:
        raise BookError(f'file list incomplete: {len(entries)}/{expected}')
    declared = sum(e.get('file_size') or 0 for e in entries)
    total = meta.get('total_size') or 0
    if total and declared < total * (1 - TOTAL_SIZE_SLACK):
        raise BookError(f'file list covers {declared}/{total} bytes of the book')
    if not any(e['full_path'].endswith(('.html', '.xhtml')) for e in entries):
        raise BookError('no chapters in file list')

    sem = asyncio.Semaphore(CONCURRENCY)
    files, problems, raw_sizes = {}, [], {}
    prog = Progress(len(entries), sum(e.get('file_size') or 0 for e in entries), label)

    async def one(entry):
        async with sem:
            raw = await get(session, entry['url'], tokens=tokens)
        raw_sizes[entry['full_path']] = len(raw)
        prog.step(len(raw))
        reason = size_problem(entry, raw)
        if reason:
            problems.append(f"{entry['full_path']}: {reason}")
            return
        path = entry['full_path']
        files[path] = to_xhtml(raw, root_path) if path.endswith(('.html', '.xhtml')) else raw

    try:
        await asyncio.gather(*[one(e) for e in entries])
    finally:
        prog.done()
    if problems:
        raise BookError(f'partial book, {len(problems)} bad file(s): '
                        + '; '.join(sorted(problems)[:5]))
    html = [e for e in entries if e['full_path'].endswith(('.html', '.xhtml'))]
    want = sum(e.get('file_size') or 0 for e in html)
    got = sum(raw_sizes[e['full_path']] for e in html)
    if want and got < want * BOOK_MIN_RATIO:
        raise BookError(f'partial book: chapters total {got}/{want} bytes '
                        f'({got / want:.0%}); account likely has preview access only')
    return meta, entries, files


# --- verify + write --------------------------------------------------------

def verify_epub(path, entries):
    with zipfile.ZipFile(path) as z:
        if z.testzip() is not None:
            raise BookError('zip CRC check failed')
        names = set(z.namelist())
        missing = [e['full_path'] for e in entries if f"EPUB/{e['full_path']}" not in names]
        if missing:
            raise BookError(f'{len(missing)} file(s) missing from epub: {missing[:5]}')
        if 'EPUB/content.opf' not in names:
            raise BookError('content.opf missing')
        opf = etree.fromstring(z.read('EPUB/content.opf'))
        ns = {'o': 'http://www.idpf.org/2007/opf'}
        hrefs = [i.get('href') for i in opf.findall('.//o:manifest/o:item', ns)]
        absent = [h for h in hrefs if f'EPUB/{h}' not in names]
        if absent:
            raise BookError(f'{len(absent)} manifest item(s) missing: {absent[:5]}')
        spine = opf.findall('.//o:spine/o:itemref', ns)
        if not spine:
            raise BookError('empty spine')
        for n in names:
            if n.endswith(('.html', '.xhtml')) and b'</html>' not in z.read(n):
                raise BookError(f'{n} has no closing </html>')
        return len(hrefs), len(spine)


def slug(s):
    return re.sub(r'[^A-Za-z0-9]+', '-', s).strip('-')[:80] or 'book'


def write_epub(book_id, meta, entries, files, out_dir=OUT_DIR):
    out_dir.mkdir(parents=True, exist_ok=True)
    final = out_dir / f"{slug(meta.get('title', ''))}_{book_id}.epub"
    fd, tmp = tempfile.mkstemp(suffix='.epub.part', dir=out_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zfh:
            zfh.writestr('mimetype', b'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            zfh.writestr('META-INF/container.xml', CONTAINER)
            for path, data in files.items():
                zfh.writestr(f'EPUB/{path}', data)
        manifest, spine = verify_epub(tmp, entries)
        os.chmod(tmp, 0o644)
        os.replace(tmp, final)
        return final, manifest, spine
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- alert -----------------------------------------------------------------

def alert(lines, target):
    msg = "O'Reilly download FAILED\n" + '\n'.join(lines)
    print(msg, file=sys.stderr)
    if not target:
        return
    try:
        subprocess.run(['hermes', 'send', '-q', '-t', target, msg],
                       timeout=60, check=True, capture_output=True)
    except Exception as e:
        print(f'alert send failed: {e}', file=sys.stderr)


# --- input helpers ---------------------------------------------------------

def parse_book_id(text):
    """Accept an ISBN/O'Reilly id or a learning.oreilly.com book URL."""
    text = text.strip()
    m = re.search(r'/library/view/[^/]+/(\w+)', text) or re.search(r'urn:orm:book:(\w+)', text)
    if m:
        return m.group(1)
    if re.fullmatch(r'[0-9X]{9,13}', text, re.I):
        return text.upper()
    return None


def read_id_file(path):
    """One id or URL per line. Blank lines, '# comments' and trailing text are ignored."""
    src = sys.stdin if path == '-' else open(path, encoding='utf-8')
    ids, bad = [], []
    with src:
        for n, line in enumerate(src, 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            token = line.split()[0]
            book_id = parse_book_id(token)
            (ids.append(book_id) if book_id else bad.append(f'line {n}: {token}'))
    return ids, bad


def setup_config():
    """Ask for the two cookies and write oreilly.conf."""
    import getpass
    print(f'Config file: {CONFIG_FILE}')
    print('In your browser, open learning.oreilly.com while logged in, then')
    print('DevTools -> Application (Chrome) / Storage (Firefox) -> Cookies.')
    rt = getpass.getpass('Paste orm-rt  (input hidden): ').strip()
    jwt = getpass.getpass('Paste orm-jwt (input hidden): ').strip()
    if not rt or not jwt:
        print('Both values are needed. Nothing saved.')
        return 2
    try:
        jwt_expiry(jwt)
    except TokenError as e:
        print(f'That orm-jwt does not look right: {e}. Nothing saved.')
        return 2
    if not CONFIG_FILE.exists() and (REPO_DIR / 'oreilly.conf.example').exists():
        CONFIG_FILE.write_text((REPO_DIR / 'oreilly.conf.example').read_text())
        os.chmod(CONFIG_FILE, 0o600)
    write_conf('ORM_RT', rt)
    write_conf('ORM_JWT', jwt)
    print('Saved. Checking the login...')
    return None


# --- main ------------------------------------------------------------------

EPILOG = """examples:
  oreilly-dl 9781491903063                 download one book (ISBN)
  oreilly-dl https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781491903063/
  oreilly-dl -f books.txt                  ids/URLs from a file, one per line (# comments OK)
  oreilly-dl -f - < books.txt              same, from stdin
  oreilly-dl --setup                       paste your cookies (first-time setup)
  oreilly-dl --check                       is my login still good?

pacing (to stay polite and avoid scraping flags):
  at most 2 files at a time, a random 45-120 s pause between books, and
  10 books per run (--max-books). On a rate-limit answer it pauses 5 min and
  retries once; a second one ends the run. Re-run later to resume: books
  already downloaded are skipped.

exit codes: 0 all OK, 1 a book failed, 2 login problem, 3 rate-limited (run again later)
"""


async def amain():
    p = argparse.ArgumentParser(
        prog='oreilly-dl',
        description="Download complete O'Reilly books as epub. Partial/preview books are refused.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('books', nargs='*', metavar='BOOK',
                   help='ISBN, O\'Reilly id, or learning.oreilly.com book URL')
    p.add_argument('-f', '--file', action='append', default=[], metavar='FILE',
                   help='read book ids/URLs from FILE, one per line ("-" = stdin). Repeatable.')
    p.add_argument('-o', '--out', metavar='DIR', help=f'save epubs here (default: {OUT_DIR})')
    p.add_argument('--setup', action='store_true', help='paste orm-rt and orm-jwt into the config')
    p.add_argument('--check', '--check-auth', dest='check', action='store_true',
                   help='check (and renew) the login, download nothing')
    p.add_argument('--force', action='store_true', help='download again even if the epub exists')
    p.add_argument('--allow-early-release', action='store_true',
                   help='accept unfinished Early Release (rough cut) books')
    p.add_argument('--json', action='store_true', help='print one JSON result per book')
    p.add_argument('--alert', metavar='TARGET',
                   help='on failure, send a message via `hermes send -t TARGET` (e.g. telegram)')
    p.add_argument('--no-alert', action='store_true', help='never send a failure alert')
    p.add_argument('--max-books', type=int, default=MAX_BOOKS, metavar='N',
                   help=f'download at most N books this run (default {MAX_BOOKS}); the rest wait for the next run')
    p.add_argument('--pause', type=int, nargs=2, default=list(BOOK_PAUSE), metavar=('MIN', 'MAX'),
                   help=f'random pause between books in seconds (default {BOOK_PAUSE[0]} {BOOK_PAUSE[1]})')
    p.add_argument('-q', '--quiet', action='store_true', help='only print the final summary and errors')
    p.add_argument('--jwt', help=argparse.SUPPRESS)
    args = p.parse_args()

    global OUT, QUIET
    QUIET = args.quiet
    if args.json:
        OUT = sys.stderr

    if args.setup:
        rc = setup_config()
        if rc is not None:
            return rc
        args.check = True

    book_ids, bad = [], []
    for raw in args.books:
        b = parse_book_id(raw)
        (book_ids.append(b) if b else bad.append(raw))
    for f in args.file:
        try:
            ids, b = read_id_file(f)
        except OSError as e:
            p.error(f'cannot read {f}: {e.strerror}')
        book_ids += ids
        bad += [f'{f} {x}' for x in b]
    if bad:
        p.error('not a book id or URL: ' + ', '.join(bad[:5]))
    book_ids = list(dict.fromkeys(book_ids))   # dedupe, keep order
    if not book_ids and not args.check:
        p.print_help()
        return 2
    pmin, pmax = sorted(max(0, x) for x in args.pause)

    out_dir = Path(args.out or os.environ.get('OREILLY_DL_DIR') or read_conf('OUT_DIR') or OUT_DIR)
    out_dir = out_dir.expanduser()
    target = None if args.no_alert else (
        args.alert or os.environ.get('OREILLY_ALERT_TARGET') or read_conf('ALERT_TARGET') or None)

    say('Checking login...')
    try:
        tokens = Tokens(load_jwt(args.jwt), load_rt())
        await tokens.fresh()
    except (TokenError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        alert([f'login: {e}', f"books: {' '.join(book_ids)}"], target)
        return 2

    timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
    async with aiohttp.ClientSession(cookies={'orm-jwt': tokens.jwt}, timeout=timeout,
                                     headers={'User-Agent': UA}) as session:
        try:
            user = await check_auth(session)
        except TokenError as e:
            alert([f'login: {e}', f"books: {' '.join(book_ids)}"], target)
            return 2
        say(f'Logged in as {user}. Token valid until {fmt_ts(tokens.exp)}; renews itself.')
        if args.check:
            return 0

        todo = [b for b in book_ids if args.force or not list(out_dir.glob(f'*_{b}.epub'))]
        have = len(book_ids) - len(todo)
        later = todo[args.max_books:] if args.max_books > 0 else []
        todo = todo[:args.max_books] if args.max_books > 0 else todo
        say(f'{len(book_ids)} book(s) in list: {have} already downloaded, {len(todo)} to get now'
            + (f', {len(later)} left for the next run (--max-books {args.max_books})' if later else '')
            + f'.\nSaving to {out_dir}')
        if len(todo) > 1:
            est = (len(todo) - 1) * (pmin + pmax) / 2
            say(f'Pausing {pmin}-{pmax}s between books; expect roughly {_fmt_secs(est)} of pauses.')

        tokens.session = session
        results, n, stop = [], len(todo), None
        for b in book_ids:
            if b not in todo and b not in later:
                results.append({'id': b, 'ok': True, 'skipped': True,
                                'path': str(next(out_dir.glob(f'*_{b}.epub')))})
        for i, book_id in enumerate(todo, 1):
            if stop:
                results.append({'id': book_id, 'ok': False, 'not_tried': True, 'error': f'not tried: {stop}'})
                continue
            tag = f'[{i}/{n}] {book_id}'
            if i > 1:
                await countdown(random.uniform(pmin, pmax), 'pause between books')
            say(f'{tag}  fetching book info...')
            t0 = time.time()
            try:
                for attempt in (1, 2):
                    await tokens.fresh()
                    before, thr = tokens.changes, PACE.throttles
                    try:
                        meta, entries, files = await fetch_book(
                            book_id, session, allow_early=args.allow_early_release,
                            tokens=tokens, label=tag)
                        break
                    except BookError as e:
                        # A token swap or a cool-off during the book can leave files
                        # cut short. Retry the whole book once.
                        if attempt == 1 and 'partial' in str(e) and (
                                tokens.changes != before or PACE.throttles != thr):
                            say(f'{tag}  interrupted mid-book; downloading it again')
                            continue
                        raise
                path, manifest, spine = write_epub(book_id, meta, entries, files, out_dir)
                r = {'id': book_id, 'ok': True, 'path': str(path), 'title': meta.get('title'),
                     'files': len(entries), 'manifest': manifest, 'spine': spine,
                     'mb': round(path.stat().st_size / 1e6, 1), 'secs': round(time.time() - t0, 1)}
                say(f"{tag}  OK  {r['title']}  ({r['mb']} MB, {r['secs']}s)")
            except Throttled as e:
                stop = 'rate-limited; run again later to resume'
                r = {'id': book_id, 'ok': False, 'error': f'Throttled: {e}'}
                say(f'{tag}  STOPPED  {e}. Ending the run so the account is not flagged.')
            except TokenError as e:
                stop = 'login lost'
                r = {'id': book_id, 'ok': False, 'error': f'TokenError: {e}'}
                say(f'{tag}  FAILED  {e}\nStopping: the login cannot be renewed.')
            except (BookError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                r = {'id': book_id, 'ok': False, 'error': f'{type(e).__name__}: {e}'}
                say(f'{tag}  FAILED  {e}')
            results.append(r)
        for b in later:
            results.append({'id': b, 'ok': True, 'deferred': True})

    ok = [r for r in results if r['ok'] and r.get('path') and not r.get('skipped')]
    skipped = [r for r in results if r.get('skipped')]
    deferred = [r for r in results if r.get('deferred')]
    not_tried = [r for r in results if r.get('not_tried')]
    failed = [r for r in results if not r['ok'] and not r.get('not_tried')]
    if args.json:
        for r in results:
            print(json.dumps(r), flush=True)
    else:
        line = (f'\nDone: {len(ok)} downloaded, {len(skipped)} already there, {len(failed)} failed')
        if not_tried or deferred:
            line += f', {len(not_tried) + len(deferred)} left for next run'
        print(line + '.', flush=True)
        for r in ok:
            print(f"  OK    {r['path']}")
        for r in failed:
            print(f"  FAIL  {r['id']}  {r['error']}")
        if not_tried or deferred:
            print('  Run the same command again later to continue; finished books are skipped.')
    if failed:
        alert([f"{r['id']}: {r['error']}" for r in failed], target)
    if stop and stop.startswith('rate'):
        return 3
    if stop:
        return 2
    return 1 if failed else 0


if __name__ == '__main__':
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        print('\nStopped. Finished books are saved; unfinished ones left no files.', flush=True)
        sys.exit(130)
