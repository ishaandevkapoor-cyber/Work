"""Offline tests for jobpoll.py: every network call is served from fixtures below."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import jobpoll  # noqa: E402

TODAY = dt.date(2026, 9, 17)


class FakeResponse:
    def __init__(self, status=200, text="", data=None, headers=None):
        self.status_code = status
        self._data = data
        self.text = text if text else (json.dumps(data) if data is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("not json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise jobpoll.requests.HTTPError(f"{self.status_code}")


GH = {"jobs": [
    {"title": "FX Strategist, G10", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1?gh_jid=1",
     "location": {"name": "London, United Kingdom"}, "first_published": "2026-09-10T00:00:00-04:00",
     "content": "&lt;p&gt;We want 3-5 years of experience in FX strategy.&lt;/p&gt;"},
    {"title": "Software Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
     "location": {"name": "London"}, "first_published": "2026-09-10T00:00:00-04:00", "content": "macro strategist tooling"},
    {"title": "Macro Strategist", "absolute_url": "https://boards.greenhouse.io/acme/jobs/3",
     "location": {"name": "New York, NY"}, "first_published": "2026-09-10T00:00:00-04:00", "content": ""},
    {"title": "Senior Macro Strategist", "absolute_url": "https://boards.greenhouse.io/acme/jobs/4",
     "location": {"name": "Dubai, UAE"}, "first_published": "2026-03-01T00:00:00-04:00",
     "content": "Minimum of 8 years of relevant experience."},
    {"title": "Private Equity Associate, Public Markets", "absolute_url": "https://boards.greenhouse.io/acme/jobs/5",
     "location": {"name": "Toronto"}, "first_published": "2026-09-12T00:00:00-04:00", "content": "asset allocation across multi-asset portfolios"},
    {"title": "Private Equity Associate", "absolute_url": "https://boards.greenhouse.io/acme/jobs/6",
     "location": {"name": "Toronto"}, "first_published": "2026-09-12T00:00:00-04:00", "content": "asset allocation"},
]}

LEVER = [
    {"text": "Geopolitical Risk Analyst", "hostedUrl": "https://jobs.lever.co/acme/abc", "createdAt": 1788998400000,
     "categories": {"location": "Paris", "allLocations": ["Paris", "Geneva"]}, "descriptionPlain": "2+ years experience"},
    {"text": "Operations Analyst", "hostedUrl": "https://jobs.lever.co/acme/def", "createdAt": 1788998400000,
     "categories": {"location": "London"}, "descriptionPlain": "currency management"},
]

ASHBY = {"jobs": [
    {"title": "Investment Strategist", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "location": "Remote",
     "isRemote": True, "secondaryLocations": [{"location": "Remote - EMEA"}], "publishedAt": "2026-09-01T00:00:00Z",
     "descriptionPlain": "no years given"},
]}

WD_LIST = {"total": 2, "jobPostings": [
    {"title": "Associate Portfolio Manager, Multi-Asset", "externalPath": "/job/London/APM_R1", "locationsText": "London, United Kingdom", "postedOn": "Posted 3 Days Ago"},
    {"title": "Currency Overlay Analyst", "externalPath": "/job/Two/CO_R2", "locationsText": "2 Locations", "postedOn": "Posted 30+ Days Ago"},
]}
WD_EMPTY = {"total": 0, "jobPostings": []}
WD_DETAIL_1 = {"jobPostingInfo": {"jobDescription": "<p>Requires 4-6 years of experience.</p>", "location": "London, United Kingdom", "startDate": "2026-09-14", "externalUrl": "https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/London/APM_R1"}}
WD_DETAIL_2 = {"jobPostingInfo": {"jobDescription": "<p>Currency overlay.</p>", "location": "Geneva, Switzerland", "additionalLocations": ["Zurich"], "startDate": "2026-04-01"}}

WORKABLE = {"results": [
    {"title": "Associate Portfolio Manager Programme", "shortcode": "ABC", "location": {"city": "London", "country": "United Kingdom"}, "published": "2026-09-01T00:00:00Z"},
]}
WORKABLE_DETAIL = {"description": "<p>Global macro.</p>", "requirements": "<p>3-10 years experience</p>"}

BREEZY = [{"name": "Economist, Macro Research", "url": "https://oxford-economics.breezy.hr/p/1", "location": {"name": "London, UK"}, "published_date": "2026-09-05T00:00:00Z"}]
BREEZY_PAGE = "<html><body><main><h1>Economist</h1><p>Posted: 5 September 2026. Macro research. 2+ years of experience.</p></main></body></html>"

RIPPLING = {"items": [{"name": "Geoeconomics Analyst", "url": "https://ats.rippling.com/eurasia-group/jobs/x1", "workLocation": {"label": "London"}}]}
RIPPLING_PAGE = "<html><body><main><h1>Geoeconomics Analyst</h1><p>at least 2 years of experience</p></main></body></html>"

JSONLD_PAGE = """<html><head><script type="application/ld+json">
{"@context":"https://schema.org","@graph":[{"@type":"JobPosting","title":"FX Institutional Sales","url":"https://rec.example/j/1",
 "datePosted":"2026-09-10","description":"Hedge fund FX sales, 3-7 years","jobLocation":{"@type":"Place","address":{"addressLocality":"London","addressCountry":"UK"}}}]}
</script></head><body></body></html>"""

SELECTOR_PAGE = """<html><body><ul>
<li class="job-card"><h3><a href="/jobs/101">Macro Strategist</a></h3><span class="job-location">Dubai</span></li>
<li class="job-card"><h3><a href="/jobs/102">Head of Payments</a></h3><span class="job-location">London</span></li>
<li class="job-card"><h3><a href="/jobs/103">Global Macro Analyst</a></h3><span class="job-location">New York</span></li>
<li class="job-card"><h3><a href="/jobs/104">Political Risk Analyst</a></h3><span class="job-location">Toronto</span></li>
</ul></body></html>"""
DETAIL_101 = "<html><body><main>Macro Strategist. Posted 1 September 2026. 2-4 years experience.</main></body></html>"

HEURISTIC_PAGE = """<html><body><nav><a href="/jobs">Jobs</a></nav><div>
<div class="row"><a href="/job/fx-sales-london-123">FX Sales - Institutional</a><p>London | Permanent</p></div>
<div class="row"><a href="/job/kyc-999">KYC Analyst</a><p>London</p></div>
<div class="row"><a href="/job/em-strat-55">Emerging Markets Strategist</a><p>Abu Dhabi</p></div>
<div class="row"><a href="/job/em-strat-56">Emerging Markets Strategist</a><p>Singapore</p></div>
</div></body></html>"""

CAREERS_WITH_LINK = '<html><body><a href="https://job-boards.greenhouse.io/acme">See our jobs</a></body></html>'
HOME_WITH_CAREERS = '<html><body><a href="/careers">Careers</a></body></html>'
CAREERS_PLAIN = SELECTOR_PAGE

ROUTES = {
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": FakeResponse(data=GH),
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs": FakeResponse(data={"jobs": []}),
    "https://api.lever.co/v0/postings/acme?mode=json": FakeResponse(data=LEVER),
    "https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=false": FakeResponse(data=ASHBY),
    "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Ext/job/London/APM_R1": FakeResponse(data=WD_DETAIL_1),
    "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/Ext/job/Two/CO_R2": FakeResponse(data=WD_DETAIL_2),
    "https://apply.workable.com/api/v3/accounts/acme/jobs": FakeResponse(data=WORKABLE),
    "https://apply.workable.com/api/v2/accounts/acme/jobs/ABC": FakeResponse(data=WORKABLE_DETAIL),
    "https://oxford-economics.breezy.hr/json": FakeResponse(data=BREEZY),
    "https://oxford-economics.breezy.hr/p/1": FakeResponse(text=BREEZY_PAGE),
    "https://api.rippling.com/platform/api/ats/v1/board/eurasia-group/jobs": FakeResponse(data=RIPPLING),
    "https://ats.rippling.com/eurasia-group/jobs/x1": FakeResponse(text=RIPPLING_PAGE),
    "https://rec.example/jobs": FakeResponse(text=JSONLD_PAGE),
    "https://sel.example/jobs": FakeResponse(text=SELECTOR_PAGE),
    "https://sel.example/jobs/101": FakeResponse(text=DETAIL_101),
    "https://sel.example/jobs/104": FakeResponse(text="<html><body><main>Political risk. 1-3 years experience</main></body></html>"),
    "https://heur.example/careers": FakeResponse(text=HEURISTIC_PAGE),
    "https://heur.example/job/fx-sales-london-123": FakeResponse(text="<html><body><main>FX sales 3+ years of experience</main></body></html>"),
    "https://heur.example/job/em-strat-55": FakeResponse(text="<html><body><main>EM 5 years' experience</main></body></html>"),
    "https://blocked.example/jobs": FakeResponse(text=SELECTOR_PAGE),
    "https://blocked.example/robots.txt": FakeResponse(text="User-agent: *\nDisallow: /jobs\n"),
    "https://resolve1.example/careers": FakeResponse(text=CAREERS_WITH_LINK),
    "https://resolve2.example": FakeResponse(text=HOME_WITH_CAREERS),
    "https://resolve2.example/careers": FakeResponse(text=CAREERS_PLAIN),
}


class FakeSession:
    """Serves ROUTES; POSTs to workday return search results only for one term."""
    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        if url.endswith("/robots.txt"):
            return ROUTES.get(url, FakeResponse(status=404))
        return ROUTES.get(url, FakeResponse(status=404))

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        if method == "POST" and "wday/cxs/acme/Ext/jobs" in url:
            term = kw["json"]["searchText"]
            return FakeResponse(data=WD_LIST if term in ("multi-asset", "currency") else WD_EMPTY)
        if method == "POST" and "workable.com/api/v3/accounts/acme/jobs" in url:
            return ROUTES[url]
        if method == "POST":
            return FakeResponse(status=404)
        return ROUTES.get(url, FakeResponse(status=404))


RealHttp = jobpoll.Http


def fake_http():
    http = RealHttp(min_interval=0)
    http.session = FakeSession()
    return http


def poll(target):
    profile = json.loads(json.dumps(jobpoll.DEFAULT_PROFILE))
    return jobpoll.poll_target(fake_http(), target, profile, TODAY)


class Helpers(unittest.TestCase):
    def test_years(self):
        self.assertEqual(jobpoll.min_years_required("3-5 years of experience"), 3)
        self.assertEqual(jobpoll.min_years_required("7+ years' experience in FX"), 7)
        self.assertEqual(jobpoll.min_years_required("Minimum of 8 years of relevant experience"), 8)
        self.assertEqual(jobpoll.min_years_required("at least 2 years of macro experience"), 2)
        self.assertIsNone(jobpoll.min_years_required("The firm has over 30 years of history."))
        self.assertIsNone(jobpoll.min_years_required("no years stated"))

    def test_geo(self):
        p = jobpoll.DEFAULT_PROFILE
        self.assertEqual(jobpoll.geo_label("London, United Kingdom", p), "London")
        self.assertEqual(jobpoll.geo_label("Dubai, UAE", p), "Dubai")
        self.assertEqual(jobpoll.geo_label("Abu Dhabi", p), "Abu Dhabi")
        self.assertEqual(jobpoll.geo_label("Remote - EMEA", p), "Remote – UK/EMEA")
        self.assertEqual(jobpoll.geo_label("Middle East", p), "Middle East")
        self.assertEqual(jobpoll.geo_label("United Kingdom", p), "UK")
        self.assertIsNone(jobpoll.geo_label("New York, NY", p))
        self.assertIsNone(jobpoll.geo_label("Remote - US", p))
        self.assertIsNone(jobpoll.geo_label("Londonderry", p))

    def test_keywords_and_excludes(self):
        p = jobpoll.DEFAULT_PROFILE
        self.assertEqual(jobpoll.title_excluded("FX Corporate Salesperson", "", p), "fx corporate")
        self.assertIn(jobpoll.title_excluded("2027 Summer Internship - Trading", "", p), ("intern", "internship", "summer"))
        self.assertEqual(jobpoll.keyword_hits("Multi Asset Strategist", p["keywords"]), ["multi-asset"])
        self.assertTrue(jobpoll.keyword_hits("G10 FX Strategist", p["keywords"]))
        self.assertFalse(jobpoll.keyword_hits("FX Salesforce admin", p["keywords"]))
        self.assertEqual(jobpoll.title_excluded("Corporate FX Sales", "", p), "corporate fx")
        self.assertIsNone(jobpoll.title_excluded("Private Equity, Public Markets", "", p))
        self.assertEqual(jobpoll.title_excluded("Private Equity Associate", "", p), "private equity")

    def test_normalise_url(self):
        self.assertEqual(jobpoll.normalise_url("https://Boards.greenhouse.io/x/jobs/1/?gh_jid=1&utm=z"),
                         "https://boards.greenhouse.io/x/jobs/1")

    def test_relative_posted(self):
        self.assertEqual(jobpoll.parse_relative_posted("Posted Today", TODAY), TODAY)
        self.assertEqual(jobpoll.parse_relative_posted("Posted 3 Days Ago", TODAY), TODAY - dt.timedelta(days=3))
        self.assertEqual(jobpoll.parse_relative_posted("Posted 30+ Days Ago", TODAY), TODAY - dt.timedelta(days=31))


class Levels(unittest.TestCase):
    def test_target_level_titles_never_senior(self):
        p = jobpoll.DEFAULT_PROFILE
        j = jobpoll.Job(employer="x", title="FX Institutional Sales - AVP", url="https://e/1", location="London",
                        description="8+ years of experience in FX sales")
        self.assertIsNone(jobpoll.evaluate(j, p, TODAY))
        self.assertFalse(j.senior)
        k = jobpoll.Job(employer="x", title="Head of FX Sales", url="https://e/2", location="London",
                        description="10+ years of experience in FX sales")
        self.assertIsNone(jobpoll.evaluate(k, p, TODAY))
        self.assertTrue(k.senior)
        self.assertTrue(jobpoll.keyword_hits("Macro Sales, Senior Associate", p["keywords"]))
        self.assertTrue(jobpoll.keyword_hits("FX Options Sales – AD", p["keywords"]))


class DescriptionRule(unittest.TestCase):
    def test_description_only_needs_two_hits(self):
        p = jobpoll.DEFAULT_PROFILE
        one = jobpoll.Job(employer="x", title="Analyst", url="https://e/1", location="London",
                          description="supports asset allocation reviews")
        two = jobpoll.Job(employer="x", title="Analyst", url="https://e/2", location="London",
                          description="asset allocation and global macro research")
        self.assertIsNotNone(jobpoll.evaluate(one, p, TODAY))
        self.assertIsNone(jobpoll.evaluate(two, p, TODAY))

    def test_nav_links_ignored(self):
        html = """<html><body><nav><a href="/how-we-invest#assetAllocation">Asset Allocation</a></nav>
        <ul class="site-menu"><li><a href="/careers/macro-strategist-jobs">Macro Strategist</a></li></ul>
        <div class="card"><a href="/jobs/9">Macro Strategist</a><span class="location">London</span></div></body></html>"""
        soup = jobpoll.BeautifulSoup(html, "html.parser")
        jobs = jobpoll.scrape_listing(soup, "https://x.example/", {"employer": "X"}, jobpoll.DEFAULT_PROFILE)
        self.assertEqual([j.url for j in jobs], ["https://x.example/jobs/9"])


class Boards(unittest.TestCase):
    def test_greenhouse(self):
        jobs = poll({"employer": "Acme", "board": "greenhouse", "slug": "acme"})
        titles = sorted(j.title for j in jobs)
        self.assertEqual(titles, ["FX Strategist, G10", "Private Equity Associate, Public Markets", "Senior Macro Strategist"])
        by = {j.title: j for j in jobs}
        self.assertEqual(by["FX Strategist, G10"].city, "London")
        self.assertFalse(by["FX Strategist, G10"].senior)
        self.assertTrue(by["Senior Macro Strategist"].senior)
        self.assertTrue(by["Senior Macro Strategist"].stale)
        self.assertEqual(by["FX Strategist, G10"].key, "https://boards.greenhouse.io/acme/jobs/1")

    def test_lever(self):
        jobs = poll({"employer": "Acme", "board": "lever", "slug": "acme"})
        self.assertEqual([j.title for j in jobs], ["Geopolitical Risk Analyst"])
        self.assertEqual(jobs[0].posted, dt.date(2026, 9, 10))
        self.assertEqual(jobs[0].city, "Paris")

    def test_ashby(self):
        jobs = poll({"employer": "Acme", "board": "ashby", "slug": "acme"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].city, "Remote – UK/EMEA")
        self.assertFalse(jobs[0].senior)

    def test_workday(self):
        jobs = poll({"employer": "Acme", "board": "workday", "slug": "acme.wd3/Ext"})
        by = {j.title: j for j in jobs}
        self.assertEqual(set(by), {"Associate Portfolio Manager, Multi-Asset", "Currency Overlay Analyst"})
        apm = by["Associate Portfolio Manager, Multi-Asset"]
        self.assertEqual(apm.posted, dt.date(2026, 9, 14))
        self.assertEqual(apm.url, "https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/London/APM_R1")
        co = by["Currency Overlay Analyst"]
        self.assertEqual(co.city, "Geneva")
        self.assertTrue(co.stale)

    def test_workable(self):
        jobs = poll({"employer": "Caxton", "board": "workable", "slug": "acme"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].url, "https://apply.workable.com/acme/j/ABC/")
        self.assertFalse(jobs[0].senior)  # 3-10 years -> lower bound 3

    def test_breezy(self):
        jobs = poll({"employer": "OE", "board": "breezy", "slug": "oxford-economics"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].city, "London")

    def test_rippling(self):
        jobs = poll({"employer": "Eurasia Group", "board": "rippling", "slug": "eurasia-group"})
        self.assertEqual([j.title for j in jobs], ["Geoeconomics Analyst"])

    def test_scrape_jsonld(self):
        jobs = poll({"employer": "Rec", "board": "scrape", "url": "https://rec.example/jobs"})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].posted, dt.date(2026, 9, 10))
        self.assertEqual(jobs[0].city, "London")

    def test_scrape_selector(self):
        jobs = poll({"employer": "Sel", "board": "scrape", "url": "https://sel.example/jobs", "selector": "li.job-card"})
        self.assertEqual(sorted(j.title for j in jobs), ["Macro Strategist", "Political Risk Analyst"])
        by = {j.title: j for j in jobs}
        self.assertEqual(by["Macro Strategist"].city, "Dubai")
        self.assertEqual(by["Macro Strategist"].posted, dt.date(2026, 9, 1))

    def test_scrape_heuristic(self):
        jobs = poll({"employer": "Heur", "board": "scrape", "url": "https://heur.example/careers"})
        titles = sorted((j.title, j.city) for j in jobs)
        # no explicit location element: a title match with an unrecognised location is kept as "?"
        self.assertEqual(titles, [("Emerging Markets Strategist", "?"), ("Emerging Markets Strategist", "Abu Dhabi"),
                                  ("FX Sales - Institutional", "London")])

    def test_robots_blocked(self):
        with self.assertRaises(jobpoll.Blocked):
            poll({"employer": "B", "board": "scrape", "url": "https://blocked.example/jobs"})

    def test_linkedin_never_fetched(self):
        self.assertFalse(fake_http().allowed("https://www.linkedin.com/jobs/view/1"))


class Resolve(unittest.TestCase):
    def test_hint(self):
        t = jobpoll.resolve_target(fake_http(), {"employer": "Acme", "board": "auto", "hints": ["greenhouse:acme"]})
        self.assertEqual((t["board"], t["slug"]), ("greenhouse", "acme"))

    def test_link_on_careers_page(self):
        t = jobpoll.resolve_target(fake_http(), {"employer": "Zzz Corp", "board": "auto", "url": "https://resolve1.example/careers"})
        self.assertEqual((t["board"], t["slug"]), ("greenhouse", "acme"))

    def test_scrape_fallback_with_selector(self):
        t = jobpoll.resolve_target(fake_http(), {"employer": "Zzz Corp", "board": "auto", "homepage": "https://resolve2.example"})
        self.assertEqual(t["board"], "scrape")
        self.assertEqual(t["selector"], "li.job-card")
        self.assertEqual(t["url"], "https://resolve2.example/careers")

    def test_slug_candidates(self):
        self.assertIn("point72", jobpoll.slug_candidates("Point72"))
        self.assertIn("mangroup", jobpoll.slug_candidates("Man Group"))
        self.assertNotIn("man", jobpoll.slug_candidates("Man Group"))  # fragments collide with other firms
        self.assertEqual(jobpoll.slug_candidates("Caxton (APM programme)")[0], "caxton")


class EndToEnd(unittest.TestCase):
    def run_main(self, tmp, *extra):
        argv = ["--targets", str(tmp / "targets.json"), "--seen", str(tmp / "seen.json"),
                "--out-dir", str(tmp / "reports"), "--today", TODAY.isoformat(), *extra]
        with mock.patch.object(jobpoll, "Http", lambda **kw: fake_http()):
            from io import StringIO
            out = StringIO()
            with mock.patch("sys.stdout", out):
                rc = jobpoll.main(argv)
        return rc, out.getvalue()

    def test_report_dedupe_and_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "targets.json").write_text(json.dumps({"targets": [
                {"employer": "Acme", "board": "greenhouse", "slug": "acme"},
                {"employer": "Acme L", "board": "lever", "slug": "acme"},
            ]}))
            rc, out = self.run_main(tmp, "--dry-run")
            self.assertEqual(rc, 0)
            self.assertIn("[dry-run] 3 new, 1 stale", out)
            self.assertFalse((tmp / "seen.json").exists())
            self.assertFalse((tmp / "reports").exists())

            rc, out = self.run_main(tmp)
            self.assertIn("3 new, 1 stale", out)
            report = (tmp / "reports" / f"new_jobs_{TODAY.isoformat()}.md").read_text()
            self.assertIn("| First seen | Employer | Title | City | Board | Status | Link |", report)
            # stale postings are remembered but not listed
            self.assertNotIn("stale (reposted)", report)
            rows = [l for l in report.splitlines() if l.startswith("| 2026")]
            self.assertEqual(len(rows), 3)
            seen = json.loads((tmp / "seen.json").read_text())
            self.assertEqual(len(seen), 4)
            stale = [v for v in seen.values() if v.get("reported") is False]
            self.assertEqual(len(stale), 1)
            self.assertIn("Senior Macro Strategist", stale[0]["title"])

            # rerun: silent, no new file content
            rc, out = self.run_main(tmp)
            self.assertEqual(out, "")
            self.assertEqual(len(json.loads((tmp / "seen.json").read_text())), 4)

    def test_include_stale_lists_them_last(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "targets.json").write_text(json.dumps({"targets": [
                {"employer": "Acme", "board": "greenhouse", "slug": "acme"}]}))
            rc, out = self.run_main(tmp, "--include-stale")
            self.assertIn("2 new, 1 stale", out)
            report = (tmp / "reports" / f"new_jobs_{TODAY.isoformat()}.md").read_text()
            rows = [l for l in report.splitlines() if l.startswith("| 2026")]
            self.assertEqual(len(rows), 3)
            self.assertIn("stale (reposted) · senior", rows[-1])

    def test_lazy_resolve_writes_targets(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "targets.json").write_text(json.dumps({"targets": [
                {"employer": "Acme", "board": "auto", "hints": ["greenhouse:acme"]}]}))
            rc, out = self.run_main(tmp)
            self.assertEqual(rc, 0)
            saved = json.loads((tmp / "targets.json").read_text())
            self.assertEqual(saved["targets"][0]["board"], "greenhouse")
            self.assertIn("2 new, 1 stale", out)

    def test_resolve_flag(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            (tmp / "targets.json").write_text(json.dumps({"targets": [
                {"employer": "Acme", "board": "auto", "url": "https://resolve1.example/careers"},
                {"employer": "Done", "board": "lever", "slug": "acme"}]}))
            rc, out = self.run_main(tmp, "--resolve")
            self.assertIn("Acme", out)
            self.assertNotIn("Done ", out)
            saved = json.loads((tmp / "targets.json").read_text())
            self.assertEqual(saved["targets"][0]["slug"], "acme")


if __name__ == "__main__":
    unittest.main()


class Budgets(unittest.TestCase):
    def test_out_of_time_keeps_partial_results(self):
        http = fake_http()
        profile = json.loads(json.dumps(jobpoll.DEFAULT_PROFILE))
        # a budget of zero seconds: the first request raises OutOfTime
        with self.assertRaises(jobpoll.OutOfTime):
            jobpoll.poll_target(http, {"employer": "Acme", "board": "greenhouse", "slug": "acme"}, profile, TODAY,
                                time_budget=0.0)
        self.assertIsNone(http.deadline)  # reset afterwards

    def test_host_dropped_after_repeated_429(self):
        http = fake_http()
        calls = {"n": 0}

        def always_429(method, url, **kw):
            calls["n"] += 1
            return FakeResponse(status=429, headers={"Retry-After": "0"})
        http.session.request = always_429
        with mock.patch.object(jobpoll.time, "sleep", lambda s: None):
            with self.assertRaises(jobpoll.requests.HTTPError):
                http.get_json("https://limited.example/api")
            n = calls["n"]
            with self.assertRaises(jobpoll.Blocked):
                http.get_json("https://limited.example/api/other")
        self.assertEqual(calls["n"], n)  # no further requests to that host
