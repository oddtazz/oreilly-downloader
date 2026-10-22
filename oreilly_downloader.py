# /// script
# dependencies = [
#   "aiohttp",
#   "lxml",
# ]
# ///
"""Download O'Reilly books as verified epub files.

Customised for Lola (Hermes) as the only user:

* Runs only with an active orm-jwt. It checks the token expiry and does a
  live auth call before any download. No token, or a dead token, means exit 2.
* Each file is checked against the size the API reports. HTML chapters that
  come back short (the silent truncation O'Reilly does for unauthenticated
  requests) fail the book.
* The epub is written to a temp file and moved into place only after it
  passes verification. A failed book leaves no file behind.
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
JWT_FILE = Path.home() / '.config/oreilly/jwt'
OUT_DIR = Path(os.environ.get('OREILLY_DL_DIR', Path.home() / '.hermes/nas/books/oreilly'))
ALERT_TARGET = os.environ.get('OREILLY_ALERT_TARGET', 'telegram')
MIN_TOKEN_SECS = 60         # refuse a token with less life left than this
CONCURRENCY = 4             # parallel file downloads; higher trips rate limits
RETRIES = 5
HTML_MIN_RATIO = 0.85       # downloaded/declared size for HTML chapters
HTML_SMALL = 4096           # below this, HTML size varies too much to compare

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

def load_jwt(arg):
    jwt = arg or os.environ.get('ORM_JWT')
    if not jwt and JWT_FILE.exists():
        jwt = JWT_FILE.read_text()
    jwt = (jwt or '').strip()
    if not jwt:
        raise TokenError(f'no orm-jwt (pass --jwt, set ORM_JWT, or write {JWT_FILE})')
    return jwt


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
            if status in (403, 429) or status >= 500:
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


async def fetch_book(book_id, session):
    root_path = f'/api/v2/epubs/urn:orm:book:{book_id}/files/'
    meta = await get(session, f'{BASE_URL}/api/v2/epubs/urn:orm:book:{book_id}/', as_json=True)

    entries, expected, url = [], None, BASE_URL + root_path + '?limit=200'
    while url:
        page = await get(session, url, as_json=True)
        expected = page.get('count')
        entries.extend(page.get('results', []))
        url = page.get('next')
    if expected is None or len(entries) != expected:
        raise BookError(f'file list incomplete: {len(entries)}/{expected}')

    sem = asyncio.Semaphore(CONCURRENCY)
    files, problems = {}, []

    async def one(entry):
        async with sem:
            raw = await get(session, entry['url'])
        reason = size_problem(entry, raw)
        if reason:
            problems.append(f"{entry['full_path']}: {reason}")
            return
        path = entry['full_path']
        files[path] = to_xhtml(raw, root_path) if path.endswith(('.html', '.xhtml')) else raw

    await asyncio.gather(*[one(e) for e in entries])
    if problems:
        raise BookError(f'{len(problems)} bad file(s): ' + '; '.join(sorted(problems)[:5]))
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
    p.add_argument('book_ids', nargs='+', help="ISBN / O'Reilly id, e.g. 9781491958698")
    p.add_argument('--jwt', help=f'orm-jwt cookie (default: $ORM_JWT, then {JWT_FILE})')
    p.add_argument('--no-alert', action='store_true', help='do not send failure alerts')
    p.add_argument('--force', action='store_true', help='re-download books already on disk')
    p.add_argument('--json', action='store_true', help='print one JSON result per book')
    args = p.parse_args()
    alerts = not args.no_alert

    try:
        jwt = load_jwt(args.jwt)
        exp = check_expiry(jwt)
    except TokenError as e:
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

        results = []
        for book_id in args.book_ids:
            existing = list(OUT_DIR.glob(f'*_{book_id}.epub'))
            if existing and not args.force:
                results.append({'id': book_id, 'ok': True, 'path': str(existing[0]), 'skipped': True})
                continue
            t0 = time.time()
            try:
                if exp - time.time() < MIN_TOKEN_SECS:
                    raise TokenError(f'orm-jwt expired at {fmt_ts(exp)}')
                meta, entries, files = await fetch_book(book_id, session)
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
