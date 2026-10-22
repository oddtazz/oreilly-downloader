# O'Reilly epub downloader

Downloads complete O'Reilly books as epub files for reading in another app.
Partial (preview) books are refused, so every file it saves is the full book.

## First-time setup

```
./oreilly-dl --setup
```

It asks for two cookies and saves them in `oreilly.conf` in this folder.
The file is gitignored and mode 600.

To find the cookies, log in to learning.oreilly.com, then open DevTools:

- Chrome: Application → Cookies → `https://learning.oreilly.com`
- Firefox: Storage → Cookies

Copy `orm-rt` and `orm-jwt`.

The first run creates `.venv` and installs `aiohttp` and `lxml`.

## Download books

```
./oreilly-dl 9781491903063                     # by ISBN / O'Reilly id
./oreilly-dl https://learning.oreilly.com/library/view/designing-data-intensive-applications/9781491903063/
./oreilly-dl -f reading-list.txt               # ids or URLs from a file
./oreilly-dl --check                           # is my login still good?
./oreilly-dl -h                                # all options
```

A book file lists one id or URL per line. Blank lines and `# comments` are
ignored:

```
# reading-list.txt
9781491903063        # Designing Data-Intensive Applications
https://learning.oreilly.com/library/view/fluent-python-2nd/9781492056348/
```

Output looks like this:

```
Logged in as you. Token valid until 16:10; renews itself.
2 book(s) -> /home/you/Books/oreilly
[1/2] 9781491903063  downloading...
[1/2] 9781491903063  OK  Designing Data-Intensive Applications  (20.4 MB, 9.1s)
[2/2] 9781492056348  already have it, skipped

Done: 1 downloaded, 1 already there, 0 failed.
```

Books already in the output folder are skipped. Use `--force` to download
them again. Ctrl-C is safe: finished books stay, and unfinished ones leave no
file.

## Config: `oreilly.conf`

```
ORM_RT=...                      # refresh cookie, lasts ~30 days
ORM_JWT=...                     # access cookie, ~15 min; renewed automatically
OUT_DIR=~/Books/oreilly         # optional; default ./books
ALERT_TARGET=telegram           # optional; failure alert via `hermes send`
```

To replace the cookies, run `./oreilly-dl --setup` again or edit the file.
The template is `oreilly.conf.example`.

The environment variables `ORM_RT`, `ORM_JWT`, `OREILLY_DL_DIR`,
`OREILLY_ALERT_TARGET` and `OREILLY_CONFIG` override the file. `-o DIR` and
`--alert TARGET` override both.

## How it works

- **Login renewal.** When `orm-jwt` is close to expiry, the script waits for
  it to expire. It then sends `orm-rt` and the old `orm-jwt` to
  `learning.oreilly.com/profile/`. O'Reilly answers with a new `orm-jwt`,
  which the script saves to `oreilly.conf`. This is the same request a
  browser makes on a page load. O'Reilly does not renew a token that is still
  live, and `orm-rt` alone is not enough. If renewal fails, `orm-rt` has been
  revoked (logout, password change or new login). Run `--setup` again.
- **Full books only.** Each file is checked against the size O'Reilly
  reports. The book fails if:
  - any chapter is short;
  - all chapters together are under 93% of the declared size;
  - the file list does not cover the book;
  - the item is not a book.

  Early Release (unfinished) books are refused unless you add
  `--allow-early-release`.
- **Verified epub.** The epub is checked before it is saved: zip CRC,
  manifest, spine, and complete chapters. It is written to a temporary file
  and renamed only when it passes.
- **Exit codes.** 0 all OK, 1 a book failed, 2 login problem.

Please use this for books you are reading, one at a time. Bulk downloading
breaks O'Reilly's terms and can get the account suspended.

## Similar projects

-   <https://github.com/lorenzodifuccia/safaribooks> (python)
-   <https://github.com/hurlenko/orly> (rust)
-   <https://github.com/jenni/obooks> (javascript)
-   <https://github.com/rahulvramesh/oreilly-books-grabber> (go)
