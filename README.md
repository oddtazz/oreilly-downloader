# O'Reilly epub downloader

O'Reilly provides all of their books in epub format, but only through their own
reader.

This script allows you to download all the individual files and assemble them
back into a full epub. This allows you to use other readers, e.g. for
accessibility reasons.

You need to have a valid JWT to download content. If you do not provide one,
each chapter will be cut short. You can get it by logging in with your browser
and extracting the `orm-jwt` cookie using the developer tools.

# Usage

```
$ python3 -m venv .venv && .venv/bin/pip install aiohttp lxml
$ cp oreilly.conf.example oreilly.conf && chmod 600 oreilly.conf   # paste ORM_RT and ORM_JWT
$ .venv/bin/python oreilly_downloader.py 9781491958698 [more ids] [--force] [--json] [--no-alert]
OK   9781491958698  ~/.hermes/nas/books/oreilly/Test-Driven-Development-with-Python-2nd-Edition_9781491958698.epub
```

This fork is set up for one operator (Lola / Hermes):

- **Active cookie only.** The token is read from `--jwt`, `$ORM_JWT` or
  `oreilly.conf` in the repo folder (gitignored; template: `oreilly.conf.example`). The script checks the JWT `exp` and makes a live
  auth call before it downloads anything. A missing, expired or rejected
  token stops the run (exit 2).
- **Verified books only.** Every file is checked against the size the API
  reports, so the silent per-chapter truncation is caught. The epub is then
  checked (zip CRC, all API files present, all OPF manifest items present,
  non-empty spine, every chapter closes `</html>`). The file is moved into
  place only after it passes. A failed book leaves no file and does not
  replace an existing good copy.
- **Token renewal.** Put `ORM_RT` and the last `ORM_JWT` in `oreilly.conf`
  (or `$ORM_RT` / `$ORM_JWT`). When `orm-jwt` has under 3 minutes left, the
  script waits for it to expire, then sends `orm-rt` + the old `orm-jwt` to
  `https://learning.oreilly.com/profile/`. O'Reilly answers with a new
  `orm-jwt` (Set-Cookie), which is written back to `oreilly.conf` (mode 600). This is the same call a
  browser makes on a page load. O'Reilly does not renew a token that is still
  live, and `orm-rt` alone is not enough. `--check-auth` renews and verifies
  without downloading.
- **Full books only.** For titles the account cannot read, O'Reilly serves a
  preview: every chapter is cut short but still returns HTTP 200. The script
  fails the book if any chapter is short, if all chapters together are under
  93% of the declared size, if the file list does not cover the book's
  declared total size, or if the item is not a book. Unfinished Early Release
  (rough cut) books are refused unless `--allow-early-release` is given.
- **Alert on failure only.** A failure sends a message with
  `hermes send -t $OREILLY_ALERT_TARGET` (default `telegram`). Success is quiet.
- Output dir: `$OREILLY_DL_DIR` (default `~/.hermes/nas/books/oreilly`).
  Books already on disk are skipped unless `--force`.
- Exit codes: 0 all OK, 1 a book failed, 2 token problem.

# Contributing

I am not really interested in adding any major features to this project. I will
accept fixes, but nothing that adds a significant amount of new code.

If you feel like something is missing, feel free to fork. You may also look at
rejected pull requests, maybe someone already worked on something similar.

# Similar Projects

-   <https://github.com/lorenzodifuccia/safaribooks> (python)
-   <https://github.com/hurlenko/orly> (rust)
-   <https://github.com/jenni/obooks> (javascript)
-   <https://github.com/rahulvramesh/oreilly-books-grabber> (go)
