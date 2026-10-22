# /// script
# dependencies = [
#   "aiohttp",
#   "lxml",
# ]
# ///
"""Download O'Reilly books as verified epub files.

Customised for Lola (Hermes) as the only user:

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
import json
import os
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
OUT_DIR = Path(os.environ.get('OREILLY_DL_DIR', Path.home() / '.hermes/nas/books/oreilly'))
ALERT_TARGET = os.environ.get('OREILLY_ALERT_TARGET', 'telegram')
MIN_TOKEN_SECS = 60         # refuse a token with less life left than this
CONCURRENCY = 4             # parallel file downloads; higher trips rate limits
RETRIES = 5
THROTTLE_WAITS = (15, 30, 60, 90, 120)  # O'Reilly throttles with 403 for minutes
BOOK_PAUSE = 8                          # seconds between books in a batch
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
        raise TokenError(f'no orm-jwt (set ORM_JWT in {CONFIG_FILE})')
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
        raise TokenError(f'orm-jwt refresh failed (HTTP {status}, no new cookie); '
                         'orm-rt may be revoked: log in again and copy orm-rt + orm-jwt')
    jwt_expiry(new_jwt)  # raises if the server sent something odd
    write_conf('ORM_JWT', new_jwt)
    if got.get('orm-rt') and got['orm-rt'] != rt:
        write_conf('ORM_RT', got['orm-rt'])
    return new_jwt, got.get('orm-rt') or rt


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
        print(f'orm-jwt expires in {int(left)}s; waiting to renew', file=sys.stderr)
        await asyncio.sleep(left + 3)
    for attempt in range(4):
        try:
            jwt, rt = await refresh_jwt(jwt, rt)
            break
        except TokenError:
            if attempt == 3:
                raise
            await asyncio.sleep(15)
    exp = check_expiry(jwt)
    print(f'orm-jwt renewed, valid until {fmt_ts(exp)}', file=sys.stderr)
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
    raise TokenError(f'auth check failed: HTTP {status}; get a fresh cookie')


# --- download --------------------------------------------------------------

async def get(session, url, *, as_json=False):
    """GET with retry on rate limits and server errors. Auth errors are fatal."""
    status = None
    for attempt in range(RETRIES):
        async with session.get(url) as r:
            status = r.status
            if status == 200:
                return await (r.json() if as_json else r.read())
            if status == 401:
                raise TokenError(f'HTTP 401 on {url}; token died mid-run')
            if status in (403, 429):
                await asyncio.sleep(THROTTLE_WAITS[min(attempt, len(THROTTLE_WAITS) - 1)])
                continue
            if status >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            raise BookError(f'HTTP {status} on {url}')
    raise BookError(f'rate-limited: gave up after {RETRIES} tries on {url} (last HTTP {status})')


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


async def fetch_book(book_id, session, *, allow_early=False):
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'
    meta = await get(session, f'{BASE_URL}/api/v2/epubs/urn:orm:book:{book_id}/', as_json=True)
    if meta.get('content_format') not in (None, 'book'):
        raise BookError(f"not a book (content_format={meta.get('content_format')})")
    if meta.get('roughcut') and not allow_early:
        raise BookError('Early Release (rough cut): book is unfinished; '
                        'use --allow-early-release to take it anyway')

    entries, expected, url = [], None, BASE_URL + root_path + '?limit=200'
    while url:
        page = await get(session, url, as_json=True)
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

    async def one(entry):
        async with sem:
            raw = await get(session, entry['url'])
        raw_sizes[entry['full_path']] = len(raw)
        reason = size_problem(entry, raw)
        if reason:
            problems.append(f"{entry['full_path']}: {reason}")
            return
        path = entry['full_path']
        files[path] = to_xhtml(raw, root_path) if path.endswith(('.html', '.xhtml')) else raw

    await asyncio.gather(*[one(e) for e in entries])
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


def write_epub(book_id, meta, entries, files):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    final = OUT_DIR / f"{slug(meta.get('title', ''))}_{book_id}.epub"
    fd, tmp = tempfile.mkstemp(suffix='.epub.part', dir=OUT_DIR)
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

def alert(lines, enabled):
    msg = "O'Reilly download FAILED\n" + '\n'.join(lines)
    print(msg, file=sys.stderr)
    if not enabled:
        return
    try:
        subprocess.run(['hermes', 'send', '-q', '-t', ALERT_TARGET, msg],
                       timeout=60, check=True, capture_output=True)
    except Exception as e:
        print(f'alert send failed: {e}', file=sys.stderr)


# --- main ------------------------------------------------------------------

async def amain():
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('book_ids', nargs='*', help="ISBN / O'Reilly id, e.g. 9781491958698")
    p.add_argument('--jwt', help=f'orm-jwt cookie (default: $ORM_JWT, then {CONFIG_FILE})')
    p.add_argument('--no-alert', action='store_true', help='do not send failure alerts')
    p.add_argument('--force', action='store_true', help='re-download books already on disk')
    p.add_argument('--json', action='store_true', help='print one JSON result per book')
    p.add_argument('--allow-early-release', action='store_true',
                   help='accept unfinished Early Release (rough cut) books')
    p.add_argument('--check-auth', action='store_true',
                   help='renew the token if needed, verify it, download nothing')
    args = p.parse_args()
    if not args.book_ids and not args.check_auth:
        p.error('give at least one book id, or --check-auth')
    alerts = not args.no_alert

    rt = load_rt()
    try:
        jwt = load_jwt(args.jwt)
        jwt, rt, exp = await ensure_fresh(jwt, rt)
    except (TokenError, aiohttp.ClientError, asyncio.TimeoutError) as e:
        alert([f'token: {e}', f"books: {' '.join(args.book_ids)}"], alerts)
        return 2

    timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
    async with aiohttp.ClientSession(cookies={'orm-jwt': jwt}, timeout=timeout) as session:
        try:
            user = await check_auth(session)
        except TokenError as e:
            alert([f'token: {e}', f"books: {' '.join(args.book_ids)}"], alerts)
            return 2
        print(f'auth ok: {user}, token valid until {fmt_ts(exp)}', file=sys.stderr)
        if args.check_auth:
            return 0

        results = []
        for book_id in args.book_ids:
            existing = list(OUT_DIR.glob(f'*_{book_id}.epub'))
            if existing and not args.force:
                results.append({'id': book_id, 'ok': True, 'path': str(existing[0]), 'skipped': True})
                continue
            if results and not results[-1].get('skipped'):
                await asyncio.sleep(BOOK_PAUSE)
            t0 = time.time()
            try:
                jwt, rt, exp = await ensure_fresh(jwt, rt)
                session.cookie_jar.update_cookies({'orm-jwt': jwt})
                meta, entries, files = await fetch_book(
                    book_id, session, allow_early=args.allow_early_release)
                path, manifest, spine = write_epub(book_id, meta, entries, files)
                results.append({
                    'id': book_id, 'ok': True, 'path': str(path), 'title': meta.get('title'),
                    'files': len(entries), 'manifest': manifest, 'spine': spine,
                    'mb': round(path.stat().st_size / 1e6, 1), 'secs': round(time.time() - t0, 1),
                })
            except (BookError, TokenError, aiohttp.ClientError, asyncio.TimeoutError) as e:
                results.append({'id': book_id, 'ok': False, 'error': f'{type(e).__name__}: {e}'})

    for r in results:
        if args.json:
            print(json.dumps(r))
        elif r['ok']:
            tag = 'exists' if r.get('skipped') else f"{r['files']} files, {r['mb']} MB, {r['secs']}s"
            print(f"OK   {r['id']}  {r['path']}  ({tag})")
        else:
            print(f"FAIL {r['id']}  {r['error']}")

    failed = [r for r in results if not r['ok']]
    if failed:
        alert([f"{r['id']}: {r['error']}" for r in failed], alerts)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(amain()))
