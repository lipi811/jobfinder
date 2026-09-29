"""Polite career-page scraper.
Order of attempts per page:
 1. schema.org JobPosting JSON-LD (most sites publish it for Google Jobs)
 2. links that look like job titles, opened one by one to look for JSON-LD
 3. one hop into "careers/jobs/openings" listing pages
It obeys robots.txt, limits concurrency, and identifies itself with a User-Agent.
"""
import asyncio, json, re
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

UA = "JobFinderBot/1.0 (personal job search project)"
INDIA = re.compile(r"india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|pune|chennai|kolkata|ahmedabad|kochi|jaipur|remote", re.I)
LIST_HINT = re.compile(r"job|career|opening|position|vacanc|opportunit|openings", re.I)
TITLE_HINT = re.compile(r"engineer|developer|analyst|manager|executive|associate|trainee|intern|officer|specialist|designer|consultant|scientist|accountant|teacher|lecturer|nurse|technician|coordinator|assistant|architect|administrator|representative|graduate|fresher", re.I)
_robots: dict = {}


async def _allowed(client, url):
    o = urlparse(url)
    root = f"{o.scheme}://{o.netloc}"
    rp = _robots.get(root)
    if rp is None:
        rp = RobotFileParser()
        try:
            r = await client.get(root + "/robots.txt", timeout=8, headers={"User-Agent": UA})
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        _robots[root] = rp
    return rp.can_fetch(UA, url)


async def _fetch(client, url):
    if not await _allowed(client, url):
        return None
    try:
        r = await client.get(url, timeout=15, headers={"User-Agent": UA})
        if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
            await asyncio.sleep(0.4)          # be polite to the site
            return r.text
    except Exception:
        pass
    return None


def _walk(x):
    if isinstance(x, list):
        for i in x:
            yield from _walk(i)
    elif isinstance(x, dict):
        t = x.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            yield x
        for k in ("@graph", "itemListElement", "item"):
            if k in x:
                yield from _walk(x[k])


def _place(j):
    loc = j.get("jobLocation")
    parts = []
    for l in (loc if isinstance(loc, list) else [loc] if loc else []):
        a = l.get("address", {}) if isinstance(l, dict) else {}
        if isinstance(a, dict):
            parts += [a.get("addressLocality"), a.get("addressRegion"), a.get("addressCountry") if isinstance(a.get("addressCountry"), str) else None]
    if j.get("jobLocationType") == "TELECOMMUTE":
        parts.append("Remote")
    return ", ".join(p for p in parts if p)


def _mk(title, company, loc, url, posted, desc=""):
    return {"title": title.strip(), "company": company, "location": loc, "url": url, "posted": posted,
            "source": "Company site", "snippet": re.sub(r"<[^>]+>|\s+", " ", desc or "")[:220].strip()}


def _jsonld(soup, page_url, company):
    out = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except Exception:
            continue
        for j in _walk(data):
            org = j.get("hiringOrganization")
            name = org.get("name") if isinstance(org, dict) else org
            if j.get("title"):
                out.append(_mk(j["title"], name or company, _place(j), j.get("url") or page_url,
                               j.get("datePosted"), j.get("description", "")))
    return out


def _company(url):
    host = urlparse(url).netloc.lower().replace("www.", "")
    return re.sub(r"^(careers|jobs|career)\.", "", host).split(".")[0].title()


async def _scan(client, url, depth=1):
    html = await _fetch(client, url)
    if not html:
        return []
    soup, company = BeautifulSoup(html, "lxml"), _company(url)
    found = _jsonld(soup, url, company)
    if found:
        return found
    host = urlparse(url).netloc
    titles, listings = {}, set()
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if urlparse(href).scheme not in ("http", "https"):
            continue
        text = a.get_text(" ", strip=True)
        if 5 <= len(text) <= 90 and TITLE_HINT.search(text):
            titles[href] = text
        elif depth and urlparse(href).netloc == host and LIST_HINT.search(href) and href != url:
            listings.add(href)
    jobs = []
    for href, text in list(titles.items())[:25]:          # open a few detail pages for rich data
        detail = await _fetch(client, href)
        rich = _jsonld(BeautifulSoup(detail, "lxml"), href, company) if detail else []
        jobs += rich or [_mk(text, company, "", href, None)]
    jobs += [_mk(t, company, "", h, None) for h, t in list(titles.items())[25:60]]
    if not jobs and depth:
        for sub in list(listings)[:5]:
            jobs += await _scan(client, sub, depth - 1)
    return jobs


async def crawl_all(client, path="careers.txt", concurrency=6):
    try:
        urls = [l.strip() for l in open(path, encoding="utf-8") if l.strip() and not l.startswith("#")]
    except FileNotFoundError:
        return []
    sem = asyncio.Semaphore(concurrency)

    async def one(u):
        async with sem:
            try:
                return await asyncio.wait_for(_scan(client, u), timeout=90)
            except Exception:
                return []

    seen, out = set(), []
    for batch in await asyncio.gather(*(one(u) for u in urls)):
        for j in batch:
            if j["location"] and not INDIA.search(j["location"]):
                continue                                      # India only; blank location kept
            k = j["url"]
            if k not in seen:
                seen.add(k)
                out.append(j)
    return out
