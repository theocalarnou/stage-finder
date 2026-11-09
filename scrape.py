"""Cherche les offres de stage sur les sites des entreprises et écrit offers.json.
Lancé automatiquement par GitHub Actions (voir .github/workflows/scrape.yml)."""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
import yaml
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "StageFinder/1.0 (outil personnel de recherche de stage)",
    "Accept-Language": "fr,en;q=0.8",
}
TIMEOUT = 25
MAX_OFFERS_PER_COMPANY = 300
OUTPUT = os.environ.get("OUTPUT", "offers.json")
_robots_cache = {}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm(text):
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def clean_text(html_or_text, limit=4000):
    text = BeautifulSoup(html_or_text or "", "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:limit]


class Profile:
    def __init__(self, cfg):
        self.internship = [norm(t) for t in cfg.get("internship_terms", [])]
        self.field = [norm(t) for t in cfg.get("field_keywords", [])]
        self.exclude = [norm(t) for t in cfg.get("exclude_terms", [])]

    @staticmethod
    def _has(term, text):
        if len(term) <= 5:  # mots courts : frontière de mot obligatoire
            return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
        return term in text

    def is_internship(self, text):
        t = norm(text)
        return any(self._has(k, t) for k in self.internship)

    def excluded(self, text):
        t = norm(text)
        return any(k in t for k in self.exclude)

    def score(self, title, description=""):
        t, d = norm(title), norm(description)
        total = 0
        for k in self.field:
            if self._has(k, t):
                total += 3
            elif self._has(k, d):
                total += 1
        return total


def allowed_by_robots(url, respect=True):
    if not respect:
        return True
    parts = urlparse(url)
    base = f"{parts.scheme}://{parts.netloc}"
    rp = _robots_cache.get(base)
    if rp is None:
        rp = RobotFileParser()
        try:
            resp = requests.get(base + "/robots.txt", headers=HEADERS, timeout=10)
            rp.parse(resp.text.splitlines() if resp.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots_cache[base] = rp
    return rp.can_fetch(HEADERS["User-Agent"], url)


def fetch(url, settings):
    if not allowed_by_robots(url, settings.get("respect_robots_txt", True)):
        raise RuntimeError("accès refusé par le robots.txt du site")
    time.sleep(settings.get("request_delay_seconds", 2))
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    if "charset" not in resp.headers.get("content-type", "").lower():
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def scrape_greenhouse(company, profile, settings):
    url = f"https://boards-api.greenhouse.io/v1/boards/{company['token']}/jobs?content=true"
    time.sleep(settings.get("request_delay_seconds", 2))
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return [{
        "title": j.get("title", "").strip(),
        "url": j.get("absolute_url", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "description": clean_text(j.get("content", "")),
    } for j in resp.json().get("jobs", [])]


def scrape_lever(company, profile, settings):
    url = f"https://api.lever.co/v0/postings/{company['token']}?mode=json"
    time.sleep(settings.get("request_delay_seconds", 2))
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    return [{
        "title": j.get("text", "").strip(),
        "url": j.get("hostedUrl", ""),
        "location": (j.get("categories") or {}).get("location", "") or "",
        "description": clean_text(j.get("descriptionPlain", "")),
    } for j in resp.json()]


def _jsonld_jobs(soup, base_url):
    jobs = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            t = node.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                loc = node.get("jobLocation")
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = (loc or {}).get("address", {}) if isinstance(loc, dict) else {}
                place = ", ".join(
                    str(addr.get(k)) for k in ("addressLocality", "addressCountry")
                    if isinstance(addr, dict) and addr.get(k)
                )
                jobs.append({
                    "title": clean_text(node.get("title", ""), 300),
                    "url": urljoin(base_url, node.get("url") or base_url),
                    "location": place,
                    "description": clean_text(node.get("description", "")),
                })
            for v in node.values():
                if isinstance(v, (list, dict)):
                    walk(v)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (ValueError, TypeError):
            continue
    return jobs


def scrape_generic(company, profile, settings):
    base = company["url"]
    soup = BeautifulSoup(fetch(base, settings), "html.parser")
    results = _jsonld_jobs(soup, base)
    seen = {r["url"] for r in results}
    anchors = soup.select(company["link_selector"]) if company.get("link_selector") else soup.find_all("a", href=True)
    for a in anchors:
        href = a.get("href")
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        link = urljoin(base, href).split("#")[0]
        text = re.sub(r"\s+", " ", a.get_text(" ", strip=True))
        if link in seen or len(text) < 6 or len(text) > 250:
            continue
        if company.get("link_selector") or profile.is_internship(text) or profile.is_internship(link):
            seen.add(link)
            results.append({"title": text, "url": link, "location": "", "description": ""})
        if len(results) >= MAX_OFFERS_PER_COMPANY:
            break
    if company.get("fetch_details"):
        for r in results:
            if r["description"]:
                continue
            try:
                page = BeautifulSoup(fetch(r["url"], settings), "html.parser")
                r["description"] = clean_text(page.get_text(" ", strip=True))
            except Exception:
                pass
    return results


SOURCES = {"generic": scrape_generic, "greenhouse": scrape_greenhouse, "lever": scrape_lever}


def filter_and_score(raw, profile):
    out = []
    for r in raw:
        if not r.get("title") or not r.get("url"):
            continue
        blob = f"{r['title']} {r.get('description', '')} {r['url']}"
        if not profile.is_internship(blob) or profile.excluded(r["title"]):
            continue
        r["score"] = profile.score(r["title"], r.get("description", ""))
        out.append(r)
    return out


def offer_id(company, url):
    return hashlib.sha1(f"{company}|{url}".encode()).hexdigest()[:16]


def main(config_path="config.yaml", companies_path="companies.yaml"):
    settings = load_yaml(config_path)
    profile = Profile(settings)
    companies = [c for c in load_yaml(companies_path).get("companies", []) if c.get("enabled", True)]

    previous = {}
    if os.path.exists(OUTPUT):
        try:
            with open(OUTPUT, encoding="utf-8") as f:
                previous = {o["id"]: o for o in json.load(f).get("offers", [])}
        except (ValueError, KeyError):
            previous = {}

    ts = now()
    offers = dict(previous)  # on part de l'existant, on met à jour entreprise par entreprise
    runs = []
    for c in companies:
        name = c["name"]
        try:
            if c.get("type", "generic") not in SOURCES:
                raise ValueError(f"type inconnu : {c.get('type')}")
            raw = SOURCES[c.get("type", "generic")](c, profile, settings)
            found = filter_and_score(raw, profile)
            seen_ids = set()
            for o in found:
                oid = offer_id(name, o["url"])
                seen_ids.add(oid)
                old = previous.get(oid, {})
                offers[oid] = {
                    "id": oid, "company": name, "title": o["title"], "url": o["url"],
                    "location": o.get("location", ""),
                    "description": o.get("description", "")[:600],
                    "score": o["score"],
                    "first_seen": old.get("first_seen", ts), "last_seen": ts, "active": True,
                }
            for oid, o in offers.items():  # offres disparues du site de cette entreprise
                if o["company"] == name and oid not in seen_ids:
                    o["active"] = False
            runs.append({"company": name, "ok": True, "message": f"{len(raw)} annonces lues", "found": len(found), "last_run": ts})
            print(f"OK  {name}: {len(found)} offres de stage sur {len(raw)} annonces")
        except Exception as e:  # une entreprise en échec ne bloque pas les autres (ses anciennes offres sont conservées)
            runs.append({"company": name, "ok": False, "message": str(e)[:300], "found": 0, "last_run": ts})
            print(f"ERR {name}: {e}", file=sys.stderr)

    data = {
        "updated": ts,
        "offers": sorted(offers.values(), key=lambda o: (not o["active"], -o["score"], o["first_seen"])),
        "runs": runs,
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"{len(data['offers'])} offres au total écrites dans {OUTPUT}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
