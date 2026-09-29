"""JobFinder India: one search box over job APIs plus company career boards."""
import asyncio, os, re, time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ADZUNA_ID, ADZUNA_KEY = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY")

# Company career boards with public JSON APIs. Add or remove slugs freely;
# a wrong slug just fails silently. Find slugs in URLs like
# boards.greenhouse.io/<slug> or jobs.lever.co/<slug>.
COMPANIES = {
    "greenhouse": ["stripe", "airbnb", "databricks", "cloudflare", "coinbase", "postman", "phonepe", "groww"],
    "lever": ["cred", "meesho", "paytm", "zeta"],
}

INDIA = re.compile(r"india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|pune|chennai|kolkata|ahmedabad|kochi|jaipur|remote", re.I)
FRESHER = re.compile(r"fresher|entry.level|graduate|trainee|junior|intern|campus|0\s*[-–to]+\s*[12]\s*(yrs|years)|new grad", re.I)

# Stream -> keywords, so one dropdown covers every kind of job seeker.
STREAMS = {
    "software": "software developer engineer programmer",
    "data": "data analyst data scientist machine learning",
    "mechanical": "mechanical engineer design manufacturing",
    "civil": "civil engineer site structural",
    "electrical": "electrical electronics engineer embedded",
    "finance": "accountant finance analyst audit banking",
    "hr": "human resources recruiter talent",
    "marketing": "marketing digital marketing seo content",
    "sales": "sales business development executive",
    "design": "designer ui ux graphic",
    "healthcare": "nurse pharmacist doctor clinical healthcare",
    "teaching": "teacher lecturer trainer education",
    "legal": "legal advocate compliance paralegal",
    "operations": "operations supply chain logistics",
    "support": "customer support service associate",
}

_cache: dict = {}          # query cache (10 min)
_boards: list = []         # company board jobs (refreshed every 3 h)
_client: httpx.AsyncClient


def _job(title, company, location, url, posted, source, desc=""):
    return {"title": (title or "").strip(), "company": (company or "").strip(),
            "location": (location or "").strip(), "url": url, "posted": posted,
            "source": source, "snippet": re.sub(r"<[^>]+>|\s+", " ", desc or "")[:220].strip()}


async def _get(url, **kw):
    try:
        r = await _client.get(url, timeout=12, **kw)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


async def adzuna(what, where, page):
    if not ADZUNA_ID:
        return []
    d = await _get(f"https://api.adzuna.com/v1/api/jobs/in/search/{page}", params={
        "app_id": ADZUNA_ID, "app_key": ADZUNA_KEY, "results_per_page": 30,
        "what": what, "where": where, "sort_by": "date"})
    return [_job(j["title"], j["company"]["display_name"], j["location"]["display_name"],
                 j["redirect_url"], j.get("created"), "Adzuna", j.get("description"))
            for j in (d or {}).get("results", [])]


async def jsearch(what, where):
    if not RAPIDAPI_KEY:
        return []
    d = await _get("https://jsearch.p.rapidapi.com/search",
                   params={"query": f"{what} in {where or 'India'}", "country": "in", "num_pages": 2, "date_posted": "month"},
                   headers={"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
    return [_job(j["job_title"], j["employer_name"],
                 ", ".join(x for x in (j.get("job_city"), j.get("job_state")) if x) or "India",
                 j["job_apply_link"], j.get("job_posted_at_datetime_utc"), "Google Jobs", j.get("job_description"))
            for j in (d or {}).get("data", [])]


async def _greenhouse(slug):
    d = await _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [_job(j["title"], slug.title(), j["location"]["name"], j["absolute_url"], j.get("updated_at"), "Company site")
            for j in (d or {}).get("jobs", []) if INDIA.search(j["location"]["name"])]


async def _lever(slug):
    d = await _get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    out = []
    for j in d or []:
        loc = (j.get("categories") or {}).get("location") or ""
        if INDIA.search(loc):
            ts = j.get("createdAt")
            out.append(_job(j["text"], slug.title(), loc, j["hostedUrl"],
                            datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None,
                            "Company site", j.get("descriptionPlain")))
    return out


async def refresh_boards():
    global _boards
    tasks = [_greenhouse(s) for s in COMPANIES["greenhouse"]] + [_lever(s) for s in COMPANIES["lever"]]
    _boards = [j for batch in await asyncio.gather(*tasks) for j in batch]


async def _loop():
    while True:
        await refresh_boards()
        await asyncio.sleep(3 * 3600)


@asynccontextmanager
async def lifespan(_):
    global _client
    _client = httpx.AsyncClient(follow_redirects=True)
    task = asyncio.create_task(_loop())
    yield
    task.cancel()
    await _client.aclose()


app = FastAPI(lifespan=lifespan)


def _match(job, words):
    hay = f"{job['title']} {job['company']} {job['snippet']}".lower()
    return any(w in hay for w in words)


@app.get("/api/search")
async def search(q: str = "", location: str = "", stream: str = "", level: str = "any",
                 page: int = Query(1, ge=1), size: int = Query(20, le=50)):
    q, location = q.strip()[:80], location.strip()[:60]
    what = " ".join(x for x in (q, "fresher" if level == "fresher" else "") if x) or STREAMS.get(stream, "jobs")
    if stream in STREAMS and q:
        what = f"{what} {STREAMS[stream].split()[0]}"
    key = (what, location.lower(), stream, level)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < 600:
        jobs = hit[1]
    else:
        api = await asyncio.gather(adzuna(what, location, 1), adzuna(what, location, 2), jsearch(what, location))
        words = [w for w in re.split(r"\W+", f"{q} {STREAMS.get(stream, '')}".lower()) if len(w) > 2]
        boards = [j for j in _boards if (not words or _match(j, words))
                  and (not location or location.lower() in j["location"].lower())]
        jobs, seen = [], set()
        for j in boards + [x for batch in api for x in batch]:      # company-site jobs first
            if level == "fresher" and not FRESHER.search(f"{j['title']} {j['snippet']}") and j["source"] == "Company site":
                continue
            k = re.sub(r"\W", "", f"{j['title']}{j['company']}{j['location']}".lower())
            if k not in seen:
                seen.add(k)
                jobs.append(j)
        jobs.sort(key=lambda j: j["posted"] or "", reverse=True)
        _cache[key] = (time.time(), jobs)
        if len(_cache) > 500:
            _cache.pop(next(iter(_cache)))
    s = (page - 1) * size
    return {"total": len(jobs), "page": page, "jobs": jobs[s:s + size],
            "sources": {"adzuna": bool(ADZUNA_ID), "jsearch": bool(RAPIDAPI_KEY), "company_boards": len(_boards)}}


@app.get("/api/streams")
async def streams():
    return list(STREAMS)


@app.get("/healthz")
async def health():
    return {"ok": True}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def index():
    return FileResponse("static/index.html")
