#!/usr/bin/env python3
"""jobpoll.py - poll employer job boards and recruiter sites for roles matching a profile.

Usage:
    python jobpoll.py                 # daily run: fetch, filter, dedupe, report
    python jobpoll.py --dry-run       # show what would be reported, write nothing
    python jobpoll.py --resolve       # discover board type/slug for unresolved targets
    python jobpoll.py --resolve-all   # re-resolve every target
    python jobpoll.py --verbose       # log each target to stderr

Files (all next to this script unless overridden):
    targets.json   employers to poll (board type, slug/URL, optional CSS selector)
    seen.json      URL -> first-seen date
    reports/new_jobs_YYYY-MM-DD.md  written only when there is something new

Requires Python 3.11+, requests, beautifulsoup4.
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import re
import sys
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import requests
from bs4 import BeautifulSoup

__version__ = "1.0.0"

HERE = Path(__file__).resolve().parent
USER_AGENT = f"jobpoll/{__version__} (+personal job search poller; contact via GitHub)"
REQUEST_TIMEOUT = 30
MIN_INTERVAL_PER_HOST = 1.0  # seconds
STALE_DAYS = 90
SENIOR_YEARS = 7
DETAIL_BUDGET = 40  # max per-job detail fetches per target
TARGET_TIME_BUDGET = 150.0  # seconds per employer before we move on with what we have
RUN_TIME_BUDGET = 45 * 60.0  # seconds for the whole run; later employers are skipped past this

log = logging.getLogger("jobpoll")

# --------------------------------------------------------------------------------------
# Profile (can be overridden by a "profile" object in targets.json)
# --------------------------------------------------------------------------------------

DEFAULT_PROFILE: dict[str, Any] = {
    # label -> aliases matched (case-insensitive) against the location string
    "geographies": {
        "London": ["london"],
        "Dubai": ["dubai", "difc"],
        "Abu Dhabi": ["abu dhabi", "adgm"],
        "Paris": ["paris"],
        "Geneva": ["geneva", "genève", "geneve"],
        "Toronto": ["toronto"],
        # "Middle East" bucket. Doha/Qatar included so QIA postings are not filtered out;
        # remove those aliases if you only want the UAE.
        "Middle East": ["middle east", "gulf", "gcc", "mena", "uae", "united arab emirates",
                        "doha", "qatar"],
        # Country-only strings (no city given) are accepted and labelled "UK".
        "UK": ["united kingdom", "uk", "england", "great britain"],
    },
    # "Remote" locations are accepted when they also mention one of these
    "remote_regions": ["uk", "united kingdom", "emea", "europe", "england", "britain",
                       "middle east", "gulf"],
    # matched case-insensitively against title OR description; hyphens/whitespace normalised
    "keywords": [
        "macro strategist", "fx strategist", "currency strategist", "g10 fx", "fx strategy",
        "currency overlay", "currency management", "associate portfolio manager", "multi-asset",
        "asset allocation", "investment strategist", "geopolitical analyst", "geopolitical risk",
        "geoeconomics", "political risk analyst", "macro research", "global macro",
        "emerging markets strategist", "fx institutional sales", "fx sales",
    ],
    # drop when the TITLE contains any of these
    "exclude_title": [
        "corporate fx", "fx broker", "cfd", "payments", "sales trader", "product manager",
        "client service", "operations", "middle office", "kyc", "private equity",
        "quantitative developer", "software",
    ],
    # exclusions that are waived when this phrase appears in title or description
    "exclude_unless": {"private equity": ["public markets"]},
    "senior_years": SENIOR_YEARS,
    "stale_days": STALE_DAYS,
    # Workday / Oracle style boards are searched by these terms (full-text) rather than
    # paging through thousands of postings.
    "search_terms": ["FX", "macro", "currency", "geopolitical", "multi-asset", "asset allocation",
                     "investment strategist", "emerging markets", "political risk",
                     "portfolio manager", "geoeconomics"],
}

# --------------------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------------------


@dataclass
class Job:
    employer: str
    title: str
    url: str
    location: str = ""
    posted: Optional[dt.date] = None
    description: str = ""
    board: str = ""
    group: str = ""
    # filled in by the filter
    city: str = ""
    senior: bool = False
    stale: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return normalise_url(self.url)


class Blocked(Exception):
    """Raised when robots.txt or policy forbids a fetch."""


class OutOfTime(Blocked):
    """Raised when the per-target or whole-run time budget is spent."""


# --------------------------------------------------------------------------------------
# HTTP layer: rate limit, robots, back-off
# --------------------------------------------------------------------------------------


class Http:
    def __init__(self, min_interval: float = MIN_INTERVAL_PER_HOST, respect_robots: bool = True):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*",
                                     "Accept-Language": "en-GB,en;q=0.9"})
        self.min_interval = min_interval
        self.respect_robots = respect_robots
        self._last: dict[str, float] = {}
        self._robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._exhausted: dict[str, str] = {}  # host -> reason we stopped talking to it this run
        self.deadline: Optional[float] = None  # time.monotonic() value; requests past it raise OutOfTime
        self.requests_made = 0

    def check_time(self) -> None:
        if self.deadline is not None and time.monotonic() > self.deadline:
            raise OutOfTime("time budget exceeded")

    # -- policy ------------------------------------------------------------------------
    def _throttle(self, host: str) -> None:
        last = self._last.get(host)
        if last is not None:
            wait = self.min_interval - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last[host] = time.monotonic()

    def _robots_for(self, scheme: str, host: str) -> Optional[urllib.robotparser.RobotFileParser]:
        if host in self._robots:
            return self._robots[host]
        rp: Optional[urllib.robotparser.RobotFileParser] = None
        try:
            self._throttle(host)
            r = self.session.get(f"{scheme}://{host}/robots.txt", timeout=REQUEST_TIMEOUT)
            if r.status_code == 200 and r.text.strip():
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
        except requests.RequestException as e:  # pragma: no cover - network
            log.debug("robots fetch failed for %s: %s", host, e)
        self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        p = urllib.parse.urlsplit(url)
        if "linkedin.com" in p.netloc.lower():
            return False
        if not self.respect_robots:
            return True
        rp = self._robots_for(p.scheme, p.netloc)
        if rp is None:
            return True
        return rp.can_fetch(USER_AGENT.split("/")[0], url) and rp.can_fetch("*", url)

    # -- fetch -------------------------------------------------------------------------
    def request(self, method: str, url: str, *, check_robots: bool = True, retries: int = 2,
                **kw) -> requests.Response:
        self.check_time()
        host = urllib.parse.urlsplit(url).netloc
        if host in self._exhausted:
            raise Blocked(f"{host} skipped for the rest of this run: {self._exhausted[host]}")
        if check_robots and not self.allowed(url):
            raise Blocked(f"robots.txt or policy disallows {url}")
        kw.setdefault("timeout", REQUEST_TIMEOUT)
        delay = 5.0
        for attempt in range(retries + 1):
            self._throttle(host)
            self.requests_made += 1
            try:
                r = self.session.request(method, url, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                # a host that will not answer gets one more try, then is dropped for this run
                if attempt >= 1:
                    self._exhausted[host] = f"unreachable ({type(e).__name__})"
                    raise
                time.sleep(delay)
                continue
            if r.status_code == 429 or r.status_code in (502, 503, 504):
                if attempt == retries:
                    self._exhausted[host] = f"kept answering {r.status_code}"
                    r.raise_for_status()
                ra = r.headers.get("Retry-After")
                wait = delay
                if ra and ra.isdigit():
                    wait = min(max(float(ra), delay), 60.0)
                log.info("%s from %s; backing off %.0fs", r.status_code, host, wait)
                time.sleep(wait)
                delay *= 3
                continue
            return r
        return r  # pragma: no cover

    def get(self, url: str, **kw) -> requests.Response:
        return self.request("GET", url, **kw)

    def get_json(self, url: str, **kw) -> Any:
        r = self.request("GET", url, check_robots=False, **kw)
        r.raise_for_status()
        return r.json()

    def post_json(self, url: str, payload: Any, **kw) -> Any:
        r = self.request("POST", url, check_robots=False, json=payload, **kw)
        r.raise_for_status()
        return r.json()

    def get_html(self, url: str, **kw) -> BeautifulSoup:
        r = self.get(url, **kw)
        r.raise_for_status()
        return BeautifulSoup(r.text, "html.parser")


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def normalise_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    path = re.sub(r"/+$", "", p.path) or "/"
    # keep query only for boards that need it to identify a job (rare); drop trackers
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if k.lower() in ("id", "jobid", "job_id", "reqid", "req")]
    query = urllib.parse.urlencode(q) if q else ""
    return urllib.parse.urlunsplit((p.scheme.lower() or "https", p.netloc.lower(), path, query, ""))


def norm_text(s: str) -> str:
    s = html.unescape(s or "").lower()
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    s = re.sub(r"[-_/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_html(s: str) -> str:
    if not s:
        return ""
    s = html.unescape(s)
    if "<" in s:
        soup = BeautifulSoup(s, "html.parser")
        return re.sub(r"\s+", " ", soup.get_text(" ")).strip()
    return re.sub(r"\s+", " ", s).strip()


def parse_date(v: Any) -> Optional[dt.date]:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        if v > 1e11:  # ms epoch
            v = v / 1000
        try:
            return dt.datetime.fromtimestamp(v, dt.timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try:
            return dt.date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def parse_relative_posted(s: str, today: dt.date) -> Optional[dt.date]:
    """Workday-style 'Posted 3 Days Ago', 'Posted Today', 'Posted 30+ Days Ago'."""
    t = (s or "").lower()
    if not t:
        return None
    if "today" in t:
        return today
    if "yesterday" in t:
        return today - dt.timedelta(days=1)
    m = re.search(r"(\d+)\s*\+?\s*(day|week|month)", t)
    if m:
        n = int(m[1])
        unit = {"day": 1, "week": 7, "month": 30}[m[2]]
        days = n * unit + (1 if "+" in t else 0)
        return today - dt.timedelta(days=days)
    return None


_YEARS_RE = re.compile(
    r"(?<!\d)(\d{1,2})(?:\s*(?:\+|-|to|or more))?\s*(?:-\s*)?(\d{1,2})?\s*\+?\s*"
    r"(?:years?|yrs?)(?:['’]s?)?\s+(?:of\s+)?(?:[\w/,-]+\s+){0,5}?experience",
    re.I,
)
_YEARS_RE2 = re.compile(
    r"(?:minimum|min\.?|at least|over|more than)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)"
    r"(?:['’]s?)?\s+(?:of\s+)?(?:[\w/,-]+\s+){0,5}?experience", re.I)


def min_years_required(text: str) -> Optional[int]:
    """Lowest lower-bound of a 'N years ... experience' phrase, or None if no years stated."""
    t = norm_text(text)
    found: list[int] = []
    for m in _YEARS_RE.finditer(t):
        found.append(int(m[1]))
    for m in _YEARS_RE2.finditer(t):
        found.append(int(m[1]))
    found = [n for n in found if 0 < n <= 30]
    return min(found) if found else None


def geo_label(location: str, profile: dict) -> Optional[str]:
    """Return the matching geography label, or None if the location is outside scope."""
    loc = norm_text(location)
    if not loc:
        return None
    for label, aliases in profile["geographies"].items():
        for a in aliases:
            a = norm_text(a)
            if re.search(rf"(?<![a-z]){re.escape(a)}(?![a-z])", loc):
                return label
    if "remote" in loc:
        for reg in profile["remote_regions"]:
            if re.search(rf"(?<![a-z]){re.escape(norm_text(reg))}(?![a-z])", loc):
                return "Remote – UK/EMEA" if reg not in ("middle east", "gulf") else "Middle East"
    return None


def keyword_hits(text: str, keywords: Iterable[str]) -> list[str]:
    t = norm_text(text)
    hits = []
    for k in keywords:
        nk = norm_text(k)
        if re.search(rf"(?<![a-z0-9]){re.escape(nk)}(?![a-z0-9])", t):
            hits.append(k)
    return hits


def title_excluded(title: str, description: str, profile: dict) -> Optional[str]:
    t = norm_text(title)
    body = norm_text(description)
    for ex in profile["exclude_title"]:
        nex = norm_text(ex)
        if re.search(rf"(?<![a-z0-9]){re.escape(nex)}(?![a-z0-9])", t):
            waivers = profile.get("exclude_unless", {}).get(ex, [])
            if any(norm_text(w) in t or norm_text(w) in body for w in waivers):
                continue
            return ex
    return None


def jsonld_jobposting(soup: BeautifulSoup) -> list[dict]:
    out: list[dict] = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            d = stack.pop()
            if isinstance(d, list):
                stack.extend(d)
            elif isinstance(d, dict):
                if "@graph" in d:
                    stack.extend(d["@graph"] if isinstance(d["@graph"], list) else [d["@graph"]])
                t = d.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    out.append(d)
                for k in ("itemListElement", "mainEntity"):
                    if k in d:
                        stack.append(d[k])
    return out


def jsonld_location(d: dict) -> str:
    locs = d.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    parts = []
    for l in locs:
        addr = l.get("address") if isinstance(l, dict) else None
        if isinstance(addr, dict):
            parts.append(", ".join(str(addr.get(k)) for k in ("addressLocality", "addressRegion",
                                                              "addressCountry") if addr.get(k)))
        elif isinstance(addr, str):
            parts.append(addr)
        elif isinstance(l, dict) and l.get("name"):
            parts.append(str(l["name"]))
    if d.get("jobLocationType") == "TELECOMMUTE":
        parts.append("Remote")
    return "; ".join(p for p in parts if p)


def page_text(soup: BeautifulSoup) -> str:
    for t in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        t.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return re.sub(r"\s+", " ", main.get_text(" ")).strip()


# --------------------------------------------------------------------------------------
# Board fetchers.  Each returns list[Job] with as much as the list endpoint provides.
# --------------------------------------------------------------------------------------


def fetch_greenhouse(http: Http, t: dict, profile: dict) -> list[Job]:
    slug = t["slug"]
    data = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "") or ""
        offices = [o.get("name", "") for o in j.get("offices") or [] if o.get("name")]
        if offices:
            loc = loc + "; " + "; ".join(offices) if loc else "; ".join(offices)
        jobs.append(Job(
            employer=t["employer"], title=j.get("title", ""), url=j.get("absolute_url", ""),
            location=loc, posted=parse_date(j.get("first_published") or j.get("updated_at")),
            description=strip_html(j.get("content", "")), board="greenhouse", group=t.get("group", ""),
        ))
    return jobs


def fetch_lever(http: Http, t: dict, profile: dict) -> list[Job]:
    slug = t["slug"]
    data = http.get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locs = [cats.get("location", "")] + list(cats.get("allLocations") or [])
        if j.get("workplaceType") == "remote":
            locs.append("Remote")
        desc = j.get("descriptionPlain", "") or strip_html(j.get("description", ""))
        for lst in j.get("lists") or []:
            desc += " " + strip_html(lst.get("text", "")) + " " + strip_html(lst.get("content", ""))
        jobs.append(Job(
            employer=t["employer"], title=j.get("text", ""), url=j.get("hostedUrl", ""),
            location="; ".join(l for l in locs if l), posted=parse_date(j.get("createdAt")),
            description=desc, board="lever", group=t.get("group", ""),
        ))
    return jobs


def fetch_ashby(http: Http, t: dict, profile: dict) -> list[Job]:
    slug = t["slug"]
    data = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=false")
    jobs = []
    for j in data.get("jobs", []):
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        if j.get("isRemote"):
            locs.append("Remote")
        jobs.append(Job(
            employer=t["employer"], title=j.get("title", ""), url=j.get("jobUrl", ""),
            location="; ".join(l for l in locs if l), posted=parse_date(j.get("publishedAt")),
            description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
            board="ashby", group=t.get("group", ""),
        ))
    return jobs


def workday_parts(t: dict) -> tuple[str, str, str]:
    """Return (host, tenant, site) from a target with slug 'tenant.wdN/site' or a URL."""
    slug = t.get("slug") or ""
    m = re.match(r"^([a-z0-9-]+)\.(wd\d+)/([^/]+)$", slug, re.I)
    if m:
        return f"{m[1]}.{m[2]}.myworkdayjobs.com", m[1], m[3]
    url = t.get("url") or slug
    m = re.match(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", url)
    if m:
        return f"{m[1]}.{m[2]}.myworkdayjobs.com", m[1], m[3]
    raise ValueError(f"cannot parse workday slug/url for {t.get('employer')}: {slug or url}")


def fetch_workday(http: Http, t: dict, profile: dict, today: Optional[dt.date] = None) -> list[Job]:
    today = today or dt.date.today()
    host, tenant, site = workday_parts(t)
    base = f"https://{host}/wday/cxs/{tenant}/{site}"
    terms = t.get("search_terms") or profile["search_terms"]
    seen: dict[str, Job] = {}
    max_results = int(t.get("max_results", 60))
    for term in terms:
        offset = 0
        while True:
            payload = {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term}
            try:
                data = http.post_json(f"{base}/jobs", payload,
                                      headers={"Content-Type": "application/json", "Accept": "application/json"})
            except OutOfTime:
                log.info("  %s: out of time during search; keeping %d found so far", t["employer"], len(seen))
                return list(seen.values())
            postings = data.get("jobPostings") or []
            for p in postings:
                path = p.get("externalPath") or ""
                if not path:
                    continue
                url = f"https://{host}/{site}{path}"
                if url in seen:
                    continue
                loc = p.get("locationsText", "") or ""
                if re.fullmatch(r"\s*\d+\s+locations?\s*", loc, re.I):
                    loc = ""  # unknown until the detail is fetched
                seen[url] = Job(
                    employer=t["employer"], title=p.get("title", ""), url=url, location=loc,
                    posted=parse_relative_posted(p.get("postedOn", ""), today),
                    board="workday", group=t.get("group", ""),
                    extra={"detail": f"{base}{path}", "bullets": p.get("bulletFields") or []},
                )
            total = data.get("total") or 0
            offset += 20
            if not postings or offset >= min(total, max_results):
                break
    return list(seen.values())


def workday_detail(http: Http, job: Job) -> None:
    data = http.get_json(job.extra["detail"], headers={"Accept": "application/json"})
    info = data.get("jobPostingInfo") or {}
    job.description = strip_html(info.get("jobDescription", ""))
    locs = [info.get("location", "")] + list(info.get("additionalLocations") or [])
    if info.get("remoteType"):
        locs.append(str(info["remoteType"]))
    job.location = "; ".join(l for l in locs if l) or job.location
    d = parse_date(info.get("startDate")) or parse_date(info.get("postedOn"))
    if d:
        job.posted = d
    if info.get("externalUrl"):
        job.url = info["externalUrl"]


def fetch_rippling(http: Http, t: dict, profile: dict) -> list[Job]:
    slug = t["slug"]
    jobs: list[Job] = []
    try:
        data = http.get_json(f"https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs")
        items = data if isinstance(data, list) else data.get("items") or data.get("jobs") or []
        for j in items:
            loc = j.get("workLocation") or j.get("location") or {}
            if isinstance(loc, dict):
                loc = loc.get("label") or loc.get("name") or ", ".join(
                    str(loc.get(k)) for k in ("city", "state", "country") if loc.get(k))
            locs = [str(loc)] if loc else []
            for l in j.get("locations") or []:
                locs.append(l.get("label") or l.get("name") if isinstance(l, dict) else str(l))
            url = j.get("url") or j.get("jobUrl") or f"https://ats.rippling.com/{slug}/jobs/{j.get('id')}"
            jobs.append(Job(employer=t["employer"], title=j.get("name") or j.get("title", ""), url=url,
                            location="; ".join(l for l in locs if l),
                            posted=parse_date(j.get("createdAt") or j.get("publishedAt")),
                            description=strip_html(j.get("description", "")), board="rippling",
                            group=t.get("group", "")))
        if jobs:
            return jobs
    except (requests.RequestException, ValueError) as e:
        log.debug("rippling api failed for %s (%s); scraping page", slug, e)
    soup = http.get_html(f"https://ats.rippling.com/{slug}/jobs")
    return scrape_listing(soup, f"https://ats.rippling.com/{slug}/jobs", t, profile, board="rippling")


def fetch_workable(http: Http, t: dict, profile: dict) -> list[Job]:
    slug = t["slug"]
    jobs: list[Job] = []
    payload = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
    data = http.post_json(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", payload,
                          headers={"Content-Type": "application/json", "Accept": "application/json"})
    for j in data.get("results", []):
        loc = j.get("location") or {}
        locs = []
        if isinstance(loc, dict):
            locs.append(", ".join(str(loc.get(k)) for k in ("city", "region", "country") if loc.get(k)))
        for l in j.get("locations") or []:
            if isinstance(l, dict):
                locs.append(", ".join(str(l.get(k)) for k in ("city", "region", "country") if l.get(k)))
        if j.get("remote") or (isinstance(loc, dict) and loc.get("workplaceType") == "remote"):
            locs.append("Remote")
        code = j.get("shortcode")
        jobs.append(Job(employer=t["employer"], title=j.get("title", ""),
                        url=f"https://apply.workable.com/{slug}/j/{code}/",
                        location="; ".join(l for l in locs if l), posted=parse_date(j.get("published")),
                        board="workable", group=t.get("group", ""),
                        extra={"detail": f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{code}"}))
    return jobs


def workable_detail(http: Http, job: Job) -> None:
    d = http.get_json(job.extra["detail"], headers={"Accept": "application/json"})
    job.description = " ".join(strip_html(d.get(k, "")) for k in ("description", "requirements", "benefits"))
    if d.get("published"):
        job.posted = parse_date(d["published"]) or job.posted


def fetch_breezy(http: Http, t: dict, profile: dict) -> list[Job]:
    slug = t["slug"]
    data = http.get_json(f"https://{slug}.breezy.hr/json")
    jobs = []
    for j in data:
        loc = j.get("location") or {}
        locs = [loc.get("name", "")] if isinstance(loc, dict) else [str(loc)]
        if isinstance(loc, dict) and loc.get("is_remote"):
            locs.append("Remote")
        jobs.append(Job(employer=t["employer"], title=j.get("name", ""), url=j.get("url", ""),
                        location="; ".join(l for l in locs if l), posted=parse_date(j.get("published_date")),
                        board="breezy", group=t.get("group", "")))
    return jobs


# -- generic scraper -------------------------------------------------------------------

JOB_HREF_RE = re.compile(r"(job|vacanc|career|position|opening|opportunit|/role|/jobs?/|apply|posting)", re.I)
LOCATION_CLASS_RE = re.compile(r"(location|city|place|office|region)", re.I)


def _el_text(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ")).strip()


def _container_for(a, max_up: int = 4):
    """Climb from an anchor to the smallest ancestor that still holds only this job link."""
    el = a
    for _ in range(max_up):
        parent = el.parent
        if parent is None or parent.name in ("body", "html", "main", "ul", "ol", "table", "tbody"):
            break
        links = [x for x in parent.find_all("a", href=True) if JOB_HREF_RE.search(x["href"]) and len(_el_text(x)) >= 4]
        if len(links) > 1:
            break
        el = parent
    return el


def _job_from_element(el, base_url: str, t: dict, board: str) -> Optional[Job]:
    a = el if el.name == "a" else el.find("a", href=True)
    if a is None or not a.get("href"):
        return None
    url = urllib.parse.urljoin(base_url, a["href"])
    if "linkedin.com" in url:
        return None
    heading = el.find(["h1", "h2", "h3", "h4", "h5"])
    title = _el_text(heading) if heading else _el_text(a)
    if not title or len(title) > 160:
        title = _el_text(a)
    if not title:
        return None
    loc = ""
    loc_el = el.find(attrs={"class": LOCATION_CLASS_RE}) or el.find(attrs={"data-location": True})
    if loc_el is not None:
        loc = loc_el.get("data-location") or _el_text(loc_el)
    context = ""
    if not loc and el.name != "a":
        # no explicit location element: keep the surrounding text so the filter can look for
        # a geography in it, without treating it as a definite location
        context = _el_text(el).replace(title, " ").strip()[:300]
    posted = None
    time_el = el.find("time")
    if time_el is not None:
        posted = parse_date(time_el.get("datetime") or _el_text(time_el))
    return Job(employer=t["employer"], title=title, url=url, location=loc, posted=posted,
               board=board, group=t.get("group", ""), extra={"context": context} if context else {})


def scrape_listing(soup: BeautifulSoup, base_url: str, t: dict, profile: dict,
                   board: str = "scrape") -> list[Job]:
    jobs: list[Job] = []
    # 1. structured data
    for d in jsonld_jobposting(soup):
        url = d.get("url")
        mep = d.get("mainEntityOfPage")
        if not url and isinstance(mep, dict):
            url = mep.get("@id")
        elif not url and isinstance(mep, str):
            url = mep
        if not url:
            continue
        jobs.append(Job(employer=t["employer"], title=str(d.get("title", "")),
                        url=urllib.parse.urljoin(base_url, str(url)), location=jsonld_location(d),
                        posted=parse_date(d.get("datePosted")),
                        description=strip_html(str(d.get("description", ""))), board=board,
                        group=t.get("group", "")))
    if jobs:
        return jobs
    # 2. explicit selector
    selector = t.get("selector")
    if selector:
        for el in soup.select(selector):
            j = _job_from_element(el, base_url, t, board)
            if j:
                jobs.append(j)
        if jobs:
            return jobs
    # 3. heuristic: anchors that look like job links
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = _el_text(a)
        if not text or len(text) < 4 or href.startswith(("#", "mailto:", "javascript:")):
            continue
        if not JOB_HREF_RE.search(href):
            continue
        if re.fullmatch(r"(apply( now)?|view( all)?( jobs)?|see (all|more)|read more|careers?|jobs?|"
                        r"learn more|more|search|all (jobs|vacancies|roles)|current (openings|vacancies))",
                        text, re.I):
            continue
        container = _container_for(a)
        j = _job_from_element(container, base_url, t, board)
        if j is None:
            continue
        j.url = urllib.parse.urljoin(base_url, href)
        if len(text) <= 160:
            j.title = text
        jobs.append(j)
    # de-duplicate by url
    out: dict[str, Job] = {}
    for j in jobs:
        out.setdefault(normalise_url(j.url), j)
    return list(out.values())


def fetch_scrape(http: Http, t: dict, profile: dict) -> list[Job]:
    url = t["url"]
    soup = http.get_html(url)
    return scrape_listing(soup, url, t, profile)


def html_detail(http: Http, job: Job) -> None:
    """Fill description/posted/location from a job's own page (JSON-LD first, then page text)."""
    soup = http.get_html(job.url)
    posts = jsonld_jobposting(soup)
    if posts:
        d = posts[0]
        job.description = strip_html(str(d.get("description", ""))) or job.description
        job.posted = parse_date(d.get("datePosted")) or job.posted
        loc = jsonld_location(d)
        if loc:
            job.location = loc
        return
    job.description = page_text(soup)[:20000]
    m = re.search(r"(?:posted|published|date)[^\n:]{0,20}:?\s*(\d{1,2}\s+\w+\s+\d{4}|\d{4}-\d{2}-\d{2}|\w+ \d{1,2}, \d{4})",
                  job.description, re.I)
    if m and not job.posted:
        job.posted = parse_date(m[1])


FETCHERS: dict[str, Callable[[Http, dict, dict], list[Job]]] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "rippling": fetch_rippling,
    "workable": fetch_workable,
    "breezy": fetch_breezy,
    "scrape": fetch_scrape,
}

DETAIL: dict[str, Callable[[Http, Job], None]] = {
    "workday": workday_detail,
    "workable": workable_detail,
    "rippling": html_detail,
    "breezy": html_detail,
    "scrape": html_detail,
}

# --------------------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------------------


def evaluate(job: Job, profile: dict, today: dt.date) -> Optional[str]:
    """Apply profile rules. Returns None if the job is wanted, else the reason it was dropped."""
    label = geo_label(job.location, profile)
    if job.location and label is None:
        return f"location '{job.location[:60]}' out of scope"
    if not job.location and job.extra.get("context"):
        label = geo_label(job.extra["context"], profile)
    job.city = label or "?"
    ex = title_excluded(job.title, job.description, profile)
    if ex:
        return f"title excluded ({ex})"
    hits = keyword_hits(job.title, profile["keywords"])
    if not hits:
        hits = keyword_hits(job.description, profile["keywords"])
        if not hits:
            return "no keyword match"
        if not job.location:
            # unknown location AND only the description matched: too weak
            return "no title match and no location"
    job.extra["hits"] = hits
    yrs = min_years_required(job.description)
    job.senior = yrs is not None and yrs >= profile.get("senior_years", SENIOR_YEARS)
    job.stale = job.posted is not None and (today - job.posted).days > profile.get("stale_days", STALE_DAYS)
    return None


def needs_detail(job: Job, profile: dict) -> bool:
    return not job.description and job.board in DETAIL


def poll_target(http: Http, t: dict, profile: dict, today: dt.date,
                time_budget: float = TARGET_TIME_BUDGET, hard_deadline: Optional[float] = None) -> list[Job]:
    board = t.get("board", "auto")
    fetcher = FETCHERS.get(board)
    if fetcher is None:
        raise ValueError(f"unknown board type '{board}'")
    started = time.monotonic()
    http.deadline = started + float(t.get("time_budget", time_budget))
    if hard_deadline is not None:
        http.deadline = min(http.deadline, hard_deadline)
    try:
        return _poll_target(http, t, profile, today, board, fetcher, started)
    finally:
        http.deadline = None


def _poll_target(http: Http, t: dict, profile: dict, today: dt.date, board: str, fetcher, started: float) -> list[Job]:
    jobs = fetcher(http, t, profile) if board != "workday" else fetch_workday(http, t, profile, today)
    log.info("%-32s %-10s %4d postings  (%.0fs)", t["employer"], board, len(jobs), time.monotonic() - started)
    # geography pre-filter before any per-job detail fetch
    candidates = [j for j in jobs if not j.location or geo_label(j.location, profile)]
    # Only spend detail fetches where the list endpoint gave no description.
    detail = DETAIL.get(board)
    if detail is not None:
        budget = int(t.get("detail_budget", DETAIL_BUDGET))
        pending = [j for j in candidates if needs_detail(j, profile)]
        if len(pending) > budget:
            # prioritise title matches, then the rest until the budget is spent
            pending.sort(key=lambda j: 0 if keyword_hits(j.title, profile["keywords"]) else 1)
            pending = pending[:budget]
        for j in pending:
            try:
                detail(http, j)
            except OutOfTime:
                log.info("  %s: out of time; skipping remaining detail fetches", t["employer"])
                break
            except (requests.RequestException, Blocked, ValueError) as e:
                log.info("  detail fetch failed for %s: %s", j.url, e)
    kept: list[Job] = []
    for j in candidates:
        why = evaluate(j, profile, today)
        if why is None:
            kept.append(j)
        else:
            log.debug("  drop %-60s %s", j.title[:60], why)
    return kept


# --------------------------------------------------------------------------------------
# Resolve: discover board type + slug for a target
# --------------------------------------------------------------------------------------

BOARD_LINK_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)")),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)")),
    ("workday", re.compile(r"https?://([a-z0-9-]+\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    ("rippling", re.compile(r"ats\.rippling\.com/([A-Za-z0-9_-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
    ("breezy", re.compile(r"https?://([a-z0-9-]+)\.breezy\.hr")),
]


def slug_candidates(employer: str) -> list[str]:
    base = re.sub(r"[^a-z0-9 ]", "", employer.lower())
    words = base.split()
    cands = ["".join(words), "-".join(words)]
    if len(words) > 1:
        cands += [words[0], "".join(words[:2]), "-".join(words[:2])]
    for stop in ("group", "management", "investments", "investment", "partners", "capital", "the"):
        w = [x for x in words if x != stop]
        if w and w != words:
            cands += ["".join(w), "-".join(w)]
    out: list[str] = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def probe_board(http: Http, board: str, slug: str) -> bool:
    try:
        if board == "greenhouse":
            d = http.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            return isinstance(d, dict) and "jobs" in d
        if board == "lever":
            d = http.get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            return isinstance(d, list)
        if board == "ashby":
            d = http.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
            return isinstance(d, dict) and "jobs" in d
        if board == "workable":
            d = http.post_json(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
                               {"query": "", "location": [], "department": [], "worktype": [], "remote": []})
            return isinstance(d, dict) and "results" in d
        if board == "breezy":
            d = http.get_json(f"https://{slug}.breezy.hr/json")
            return isinstance(d, list)
        if board == "rippling":
            r = http.get(f"https://ats.rippling.com/{slug}/jobs", check_robots=False)
            return r.status_code == 200
        if board == "workday":
            host, tenant, site = workday_parts({"slug": slug})
            d = http.post_json(f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                               {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                               headers={"Content-Type": "application/json", "Accept": "application/json"})
            return isinstance(d, dict) and "jobPostings" in d
    except OutOfTime:
        raise
    except (requests.RequestException, ValueError, Blocked):
        return False
    return False


def find_board_links(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for board, pat in BOARD_LINK_PATTERNS:
        for m in pat.finditer(text):
            slug = m[1] if board != "workday" else f"{m[1]}/{m[2]}"
            if slug.lower() in ("embed", "api", "v0", "v1", "static", "assets"):
                continue
            if (board, slug) not in found:
                found.append((board, slug))
    return found


def derive_selector(soup: BeautifulSoup) -> Optional[str]:
    """Pick the repeated container that holds the most job-looking links."""
    groups: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        if not JOB_HREF_RE.search(a["href"]) or len(_el_text(a)) < 4:
            continue
        el = _container_for(a)
        classes = [c for c in (el.get("class") or []) if re.match(r"^[A-Za-z][\w-]*$", c)]
        if not classes and el.name == "a":
            continue
        sel = el.name + ("." + ".".join(classes[:2]) if classes else "")
        groups[sel] = groups.get(sel, 0) + 1
    if not groups:
        return None
    sel, n = max(groups.items(), key=lambda kv: kv[1])
    return sel if n >= 3 else None


def resolve_target(http: Http, t: dict) -> dict:
    """Return an updated copy of the target with board/slug/url/selector filled in."""
    t = dict(t)
    employer = t["employer"]
    hints = list(t.get("hints") or [])
    # 1. hinted or already-set slugs, e.g. "greenhouse:point72" / "workday:cppib.wd10/cppinvestments"
    for h in hints:
        if ":" in h:
            board, slug = h.split(":", 1)
            if board in FETCHERS and probe_board(http, board, slug):
                t.update(board=board, slug=slug)
                t["resolved"] = f"hint {h}"
                return t
    # 2. careers page (or homepage) link discovery, following "careers"/"jobs" links one hop
    pages = [u for u in (t.get("url"), t.get("careers_url"), t.get("homepage")) if u]
    queue: list[tuple[str, int]] = [(p, 0) for p in pages]
    visited: set[str] = set()
    fetched: list[tuple[str, BeautifulSoup]] = []  # pages we could load, for the scrape fallback
    while queue:
        page, depth = queue.pop(0)
        if page in visited or len(visited) >= 8:
            continue
        visited.add(page)
        try:
            r = http.get(page)
            if r.status_code != 200:
                continue
        except (requests.RequestException, Blocked) as e:
            log.info("  %s: cannot fetch %s (%s)", employer, page, e)
            continue
        for board, slug in find_board_links(r.text):
            if probe_board(http, board, slug):
                t.update(board=board, slug=slug)
                t["resolved"] = f"link on {page}"
                return t
        soup = BeautifulSoup(r.text, "html.parser")
        fetched.append((page, soup))
        if depth >= 1:
            continue
        hops = 0
        for a in soup.find_all("a", href=True):
            if hops >= 3:
                break
            if re.search(r"(career|job|vacanc|opportunit|openings)", (_el_text(a) + " " + a["href"]), re.I):
                nxt = urllib.parse.urljoin(page, a["href"]).split("#")[0]
                if nxt in visited or nxt == page or "linkedin.com" in nxt:
                    continue
                queue.append((nxt, depth + 1))
                hops += 1
    # 3. guessed slugs on the JSON boards
    for slug in slug_candidates(employer):
        for board in ("greenhouse", "lever", "ashby", "workable"):
            if probe_board(http, board, slug):
                t.update(board=board, slug=slug)
                t["resolved"] = f"guessed slug {slug}"
                return t
    # 4. fall back to scraping: prefer a fetched page where a repeated job-card selector is found
    for page, soup in fetched:
        sel = derive_selector(soup)
        if sel:
            t.update(board="scrape", url=page, selector=sel)
            t["resolved"] = f"scrape selector={sel}"
            return t
    if fetched:
        t.update(board="scrape", url=fetched[0][0])
        t.pop("selector", None)
        t["resolved"] = "scrape (no selector found; link heuristics)"
        return t
    t["board"] = "unresolved"
    t["resolved"] = "could not resolve; set board/slug/url by hand"
    return t


# --------------------------------------------------------------------------------------
# State + report
# --------------------------------------------------------------------------------------


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def load_targets(path: Path) -> tuple[dict, list[dict], Any]:
    raw = load_json(path, {"targets": []})
    if isinstance(raw, list):
        targets, profile_over, raw_obj = raw, {}, {"targets": raw}
    else:
        targets, profile_over, raw_obj = raw.get("targets", []), raw.get("profile", {}), raw
    profile = json.loads(json.dumps(DEFAULT_PROFILE))
    profile.update(profile_over or {})
    return profile, targets, raw_obj


def md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ").strip()


def status_of(j: Job) -> str:
    parts = ["stale (reposted)" if j.stale else "new"]
    if j.senior:
        parts.append("senior")
    return " · ".join(parts)


def sort_key(j: Job) -> tuple:
    return (j.senior, j.stale, j.employer.lower(), j.title.lower())


def render_report(jobs: list[Job], today: dt.date, first_seen: dict[str, str]) -> str:
    new = [j for j in jobs if not j.stale]
    stale = [j for j in jobs if j.stale]
    lines = [f"# New jobs - {today.isoformat()}", "",
             f"{len(new)} new, {len(stale)} stale (reposted). Senior (7+ years) roles are listed last.", "",
             "| First seen | Employer | Title | City | Board | Status | Link |",
             "|---|---|---|---|---|---|---|"]
    for j in sorted(jobs, key=sort_key):
        posted = f" (posted {j.posted.isoformat()})" if j.posted else ""
        lines.append("| {fs} | {emp} | {title} | {city} | {board} | {status}{posted} | [link]({url}) |".format(
            fs=first_seen.get(j.key, today.isoformat()), emp=md_escape(j.employer), title=md_escape(j.title),
            city=md_escape(j.city), board=j.board, status=status_of(j), posted=posted, url=j.url))
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def run_resolve(http: Http, targets: list[dict], only_unresolved: bool) -> list[dict]:
    out = []
    for t in targets:
        if only_unresolved and t.get("board") not in (None, "", "auto", "unresolved"):
            out.append(t)
            continue
        log.info("resolving %s", t["employer"])
        http.deadline = time.monotonic() + TARGET_TIME_BUDGET
        try:
            nt = resolve_target(http, t)
        except OutOfTime:
            nt = dict(t, resolved="out of time; still auto")
        finally:
            http.deadline = None
        print(f"{t['employer']:32s} -> {nt.get('board')}  {nt.get('slug') or nt.get('url') or ''}  [{nt.get('resolved')}]")
        out.append(nt)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--targets", type=Path, default=HERE / "targets.json")
    ap.add_argument("--seen", type=Path, default=HERE / "seen.json")
    ap.add_argument("--out-dir", type=Path, default=HERE / "reports")
    ap.add_argument("--dry-run", action="store_true", help="print the report; write no state or files")
    ap.add_argument("--resolve", action="store_true", help="discover board type/slug for unresolved targets and exit")
    ap.add_argument("--resolve-all", action="store_true", help="re-resolve every target and exit")
    ap.add_argument("--only", help="poll only employers whose name contains this text (case-insensitive)")
    ap.add_argument("--today", help="override today's date (YYYY-MM-DD), for testing")
    ap.add_argument("--target-budget", type=float, default=TARGET_TIME_BUDGET,
                    help="seconds allowed per employer (default %(default)s)")
    ap.add_argument("--run-budget", type=float, default=RUN_TIME_BUDGET,
                    help="seconds allowed for the whole run (default %(default)s)")
    ap.add_argument("--no-robots", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--verbose", "-v", action="count", default=0)
    args = ap.parse_args(argv)

    logging.basicConfig(stream=sys.stderr, format="%(message)s",
                        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING)
    today = parse_date(args.today) if args.today else dt.date.today()
    if today is None:
        ap.error("--today must be YYYY-MM-DD")

    profile, targets, raw = load_targets(args.targets)
    http = Http(respect_robots=not args.no_robots)
    run_started = time.monotonic()
    hard_deadline = run_started + args.run_budget

    if args.resolve or args.resolve_all:
        targets = run_resolve(http, targets, only_unresolved=not args.resolve_all)
        raw["targets"] = targets
        if not args.dry_run:
            save_json(args.targets, raw)
        return 0

    if args.only:
        targets = [t for t in targets if args.only.lower() in t["employer"].lower()]

    # lazily resolve anything still marked auto so a first run needs no separate step
    unresolved = [t for t in targets if t.get("board") in (None, "", "auto")]
    if unresolved:
        log.warning("%d target(s) unresolved; resolving now", len(unresolved))
        resolved = {}
        for t in unresolved:
            http.deadline = min(time.monotonic() + args.target_budget, hard_deadline)
            try:
                resolved[t["employer"]] = resolve_target(http, t)
            except OutOfTime:
                log.warning("%s: out of time while resolving; left as auto", t["employer"])
            finally:
                http.deadline = None
            log.info("resolved %-32s -> %s %s", t["employer"], resolved.get(t["employer"], {}).get("board"),
                     resolved.get(t["employer"], {}).get("resolved", ""))
        targets = [resolved.get(t["employer"], t) for t in targets]
        raw["targets"] = [resolved.get(t["employer"], t) for t in raw.get("targets", [])]
        if not args.dry_run:
            save_json(args.targets, raw)

    seen: dict[str, dict] = load_json(args.seen, {})
    found: list[Job] = []
    errors: list[str] = []
    for t in targets:
        if t.get("board") in ("unresolved", "skip"):
            log.info("%-32s skipped (%s)", t["employer"], t.get("board"))
            continue
        if time.monotonic() > hard_deadline:
            errors.append(f"{t['employer']}: skipped, run time budget ({args.run_budget:.0f}s) spent")
            continue
        try:
            found.extend(poll_target(http, t, profile, today, args.target_budget, hard_deadline))
        except OutOfTime as e:
            errors.append(f"{t['employer']}: {e} (no results kept)")
        except Blocked as e:
            errors.append(f"{t['employer']}: {e}")
        except (requests.RequestException, ValueError, KeyError) as e:
            errors.append(f"{t['employer']}: {type(e).__name__}: {e}")

    report_jobs: list[Job] = []
    dedup: set[str] = set()
    for j in found:
        k = j.key
        if k in seen or k in dedup:
            continue
        dedup.add(k)
        report_jobs.append(j)

    for e in errors:
        log.warning("warning: %s", e)
    log.info("done: %d requests in %.0fs", http.requests_made, time.monotonic() - run_started)

    if not report_jobs:
        if args.dry_run:
            print("0 new, 0 stale")
        return 0

    first_seen = {j.key: today.isoformat() for j in report_jobs}
    report = render_report(report_jobs, today, first_seen)
    n_new = sum(1 for j in report_jobs if not j.stale)
    n_stale = len(report_jobs) - n_new

    if args.dry_run:
        print(report)
        print(f"[dry-run] {n_new} new, {n_stale} stale - nothing written")
        return 0

    args.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = args.out_dir / f"new_jobs_{today.isoformat()}.md"
    if out_path.exists():  # a second run the same day appends rather than overwrites
        existing = out_path.read_text(encoding="utf-8")
        body = report.split("\n", 6)[-1]  # rows only
        out_path.write_text(existing.rstrip("\n") + "\n" + body, encoding="utf-8")
    else:
        out_path.write_text(report, encoding="utf-8")
    for j in report_jobs:
        seen[j.key] = {"first_seen": today.isoformat(), "employer": j.employer, "title": j.title,
                       "posted": j.posted.isoformat() if j.posted else None,
                       "status": status_of(j)}
    save_json(args.seen, seen)
    print(f"{n_new} new, {n_stale} stale -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
