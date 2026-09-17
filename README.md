# jobpoll

Polls employer job boards and recruiter listing pages once a day for roles that match a
macro / FX / geopolitics profile, remembers what it has already shown you, and writes a
Markdown table of anything new.

```
targets.json   who to poll (employer, board type, slug or URL, optional CSS selector)
jobpoll.py     fetch -> filter -> dedupe -> report
seen.json      URL -> first-seen date (the dedupe state; committed by the daily run)
reports/new_jobs_YYYY-MM-DD.md   written only on days with something new
```

## Zero-effort mode (GitHub Actions)

`.github/workflows/jobpoll.yml` runs the poller every day at **08:00 London** on GitHub's
runners and commits `seen.json`, any resolved `targets.json` changes and the day's report back
to the repo. Nothing to install. The only requirement is that the workflow file lives on the
repository's default branch (`main`), because GitHub only schedules workflows from there.

- Results: the `reports/` folder, and the run summary under **Actions → jobpoll**.
- Manual run: **Actions → jobpoll → Run workflow**, mode `run`, `dry-run`, `resolve` or `resolve-all`.
- The first scheduled run resolves every target still marked `"board": "auto"` and commits the result.

## Local use

```
pip install -r requirements.txt        # Python 3.11+, requests, beautifulsoup4
python jobpoll.py --resolve            # first run: discover board type + slug for "auto" targets
python jobpoll.py                      # daily: prints "N new, M stale -> reports/new_jobs_...md"
python jobpoll.py --dry-run            # show what would be reported; writes nothing
python jobpoll.py --only "Point72" -vv # one employer, with per-posting drop reasons
```

Reruns print nothing unless something is new. Warnings for targets that could not be fetched go to
stderr (always) so cron mail shows them.

Cron (08:00 London):

```
0 8 * * * cd /path/to/Work && TZ=Europe/London /usr/bin/python3 jobpoll.py >> jobpoll.log 2>&1
```

Windows: Task Scheduler, daily 08:00, action `python C:\path\to\Work\jobpoll.py`.

## What gets reported

A posting is listed when all of the following hold:

1. **Geography** – location mentions London, Dubai, Abu Dhabi, Paris, Geneva, Toronto, "Middle East"
   (incl. UAE/GCC/Doha), a bare "United Kingdom", or "Remote" plus UK/EMEA/Europe. Postings with no
   location at all (some scraped listings) are kept only when the *title* matches and show city `?`.
2. **Keywords** – title or description contains any profile keyword (macro strategist, FX strategist,
   G10 FX, currency overlay, multi-asset, asset allocation, geopolitical risk, FX institutional sales, …).
3. **Not excluded** – title does not contain corporate FX, FX broker, CFD, payments, sales trader,
   product manager, client service, operations, middle office, KYC, private equity (unless "public
   markets" is also present), quantitative developer, software.
4. **Not seen before** – URL not in `seen.json`.

Then it is flagged:

- `senior` when the description states 7+ years of experience (lowest stated lower bound). Listed last.
- `stale (reposted)` when the original post date is more than 90 days old. Counted separately in the
  "N new, M stale" summary.

The report table: First seen | Employer | Title | City | Board | Status | Link.

## Board types

| board | how it is read | slug format |
|---|---|---|
| `greenhouse` | public Job Board API (`boards-api.greenhouse.io`, with descriptions) | `point72` |
| `lever` | `api.lever.co/v0/postings` | `company` |
| `ashby` | `api.ashbyhq.com/posting-api/job-board` | `company` |
| `workday` | `wday/cxs/.../jobs` search, one query per profile search term, then per-job detail | `tenant.wdN/site` e.g. `cppib.wd10/cppinvestments` |
| `rippling` | `api.rippling.com/platform/api/ats/v1/board/<slug>/jobs`, falls back to the HTML page | `eurasia-group` |
| `workable` | `apply.workable.com/api/v3/accounts/<slug>/jobs` + per-job detail | `caxton` |
| `breezy` | `<slug>.breezy.hr/json` | `oxford-economics` |
| `scrape` | the page's JSON-LD `JobPosting` data, else the CSS `selector`, else link heuristics | needs `url` |
| `auto` | resolved on the next run (see below) | |
| `skip` / `unresolved` | ignored | |

Where the listing has no description (Workday, Workable, Rippling, Breezy, scrape), the poller fetches
each candidate's own page for the description, post date and location, up to `detail_budget`
(default 40) per target, title matches first.

## `--resolve`

For each target marked `auto` (or all, with `--resolve-all`), in order:

1. `hints` such as `"greenhouse:point72"` or `"workday:td.wd3/TD_Bank_Careers"` are probed.
2. The `url`, `careers_url` and `homepage` pages are fetched and scanned for links to Greenhouse,
   Lever, Ashby, Workday, Rippling, Workable or Breezy (one hop of "careers"/"jobs" links is followed).
3. Slugs derived from the employer name are probed on Greenhouse, Lever, Ashby and Workable.
4. Otherwise the target becomes `scrape` with the best fetched page as `url`, plus a `selector`
   (e.g. `li.job-card`) if a repeated job-card pattern is found.

The result is written back to `targets.json` with a `resolved` note saying which step matched.
Edit `board`/`slug`/`url`/`selector` by hand any time; `--resolve` leaves resolved targets alone.

## Etiquette and limits

- `robots.txt` is honoured for every HTML page; one request per second per host; 429/5xx back off
  (Retry-After, then 5s → 15s → 45s). LinkedIn is never fetched.
- Only `requests` and `beautifulsoup4` are used, so JavaScript-only careers sites (several banks) will
  yield little or nothing via `scrape`. Those targets are the ones most worth a hand-set `selector`
  or a switch to a proper board once one is known.

## Tests

```
python -m unittest tests.test_jobpoll -v
```

All fixtures are inline; no network access is needed.
