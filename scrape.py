"""Cherche les offres de stage sur les sites des entreprises et écrit offers.json.
Lancé automatiquement par GitHub Actions (voir .github/workflows/scrape.yml)."""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
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


_pw = {}  # navigateur Playwright, démarré à la demande et réutilisé


def _browser():
    if "browser" not in _pw:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError("Playwright n'est pas installé")
        _pw["ctx"] = sync_playwright().start()
        _pw["browser"] = _pw["ctx"].chromium.launch()
    return _pw["browser"]


def close_browser():
    if "browser" in _pw:
        _pw["browser"].close()
        _pw["ctx"].stop()
        _pw.clear()


def fetch_rendered(url):
    """Charge la page dans un vrai navigateur (pour les sites 100 % JavaScript)."""
    page = _browser().new_page(extra_http_headers={"Accept-Language": HEADERS["Accept-Language"]},
                               user_agent=HEADERS["User-Agent"])
    try:
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception:
            pass  # certains sites ne "se calment" jamais : on prend ce qui est affiché
        # fait défiler la page pour déclencher le chargement des offres paresseux
        for _ in range(6):
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(600)
        return page.content()
    finally:
        page.close()


def fetch(url, settings, render_js=False):
    if not allowed_by_robots(url, settings.get("respect_robots_txt", True)):
        raise RuntimeError("accès refusé par le robots.txt du site")
    time.sleep(settings.get("request_delay_seconds", 2))
    if render_js:
        return fetch_rendered(url)
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


# Mots qu'on trouve dans les liens de menu ("Nos offres de stage", "Internships", "Voir toutes les offres"...)
# Un lien composé UNIQUEMENT de ces mots n'est pas une offre mais une rubrique du site.
GENERIC_WORDS = set("""
stage stages stagiaire stagiaires internship internships intern interns trainee trainees praktikum praktika
praktikant praktikanten werkstudent thesis theses placement placements alternance alternances apprentissage
apprenti apprentice apprenticeship apprenticeships vie v i e offre offres offer offers job jobs emploi emplois
career careers carriere carrieres opportunity opportunities poste postes student students etudiant etudiants
graduate graduates jeune jeunes diplome diplomes young programme programmes program programs de des d du la le
les l et en nos vos notre votre toutes tous tout voir our your all see view and for the with us nous rejoindre
rejoignez join recrutement recruitment postuler apply here ici plus more savoir learn about decouvrir discover
search recherche rechercher find trouver annee year summer ete fin d etudes end of studies spontaneous
candidature spontanee application
""".split())


def is_navigation(text, link, base):
    if link.rstrip("/") == base.rstrip("/"):
        return True
    words = re.findall(r"[a-z0-9]+", norm(text))
    return all(w in GENERIC_WORDS or w.isdigit() for w in words)


def scrape_generic(company, profile, settings):
    base = company["url"]
    js = company.get("render") == "js"
    soup = BeautifulSoup(fetch(base, settings, render_js=js), "html.parser")
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
        if is_navigation(text, link, base):
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
                page = BeautifulSoup(fetch(r["url"], settings, render_js=js), "html.parser")
                r["description"] = clean_text(page.get_text(" ", strip=True))
            except Exception:
                pass
    return results


def scrape_workday(company, profile, settings):
    """Sites Workday (xxx.wdN.myworkdayjobs.com) : on interroge leur moteur de recherche public
    avec des mots-clés de stage, au lieu de lire la page (100 % JavaScript)."""
    u = urlparse(company["url"])
    host = u.netloc
    tenant = company.get("tenant") or host.split(".")[0]
    parts = [p for p in u.path.split("/") if p and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
    if not parts:
        raise ValueError("adresse Workday incomplète : il manque le nom du site après le domaine")
    site = parts[0]
    root = f"{u.scheme}://{host}"
    api = f"{root}/wday/cxs/{tenant}/{site}/jobs"
    if not allowed_by_robots(api, settings.get("respect_robots_txt", True)):
        raise RuntimeError("accès refusé par le robots.txt du site")
    terms = company.get("search_terms") or ["stage", "stagiaire", "internship", "intern", "praktikum"]
    headers = {**HEADERS, "Accept": "application/json", "Content-Type": "application/json"}
    results, seen = [], set()
    for term in terms:
        offset = 0
        while offset < MAX_OFFERS_PER_COMPANY:
            time.sleep(settings.get("request_delay_seconds", 2))
            resp = requests.post(api, headers=headers, timeout=TIMEOUT,
                                 json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
            resp.raise_for_status()
            data = resp.json()
            posts = data.get("jobPostings") or []
            for p in posts:
                path = p.get("externalPath") or ""
                link = f"{root}/{site}{path}"
                if not path or link in seen:
                    continue
                seen.add(link)
                results.append({"title": (p.get("title") or "").strip(), "url": link,
                                "location": p.get("locationsText") or "", "description": "", "_path": path})
            offset += 20
            if not posts or offset >= (data.get("total") or 0):
                break
    if company.get("fetch_details"):
        for r in results:
            try:
                time.sleep(settings.get("request_delay_seconds", 2))
                d = requests.get(f"{root}/wday/cxs/{tenant}/{site}{r['_path']}", headers=headers, timeout=TIMEOUT).json()
                r["description"] = clean_text((d.get("jobPostingInfo") or {}).get("jobDescription", ""))
            except Exception:
                pass
    for r in results:
        r.pop("_path", None)
    return results


ADZUNA_API = os.environ.get("ADZUNA_API", "https://api.adzuna.com/v1/api")


def scrape_adzuna(company, profile, settings):
    """Agrégateur Adzuna, via son API officielle (clés gratuites sur developer.adzuna.com).
    Les clés sont lues dans les secrets GitHub ADZUNA_APP_ID et ADZUNA_APP_KEY."""
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise RuntimeError("clés Adzuna manquantes (secrets ADZUNA_APP_ID et ADZUNA_APP_KEY)")
    pages = int(company.get("pages", 1))
    results, seen, errors, ok = [], set(), [], 0
    for country, queries in (company.get("searches") or {}).items():
        for q in queries:
            for page in range(1, pages + 1):
                time.sleep(max(3, settings.get("request_delay_seconds", 2)))  # quota gratuit : rester sous ~20 requêtes/min
                try:
                    params ={"app_id": app_id, "app_key": app_key, "what": q,
                              "results_per_page": 50, "content-type": "application/json"}
                    if company.get("max_days_old"):
                        params["max_days_old"] = company["max_days_old"]
                    resp = requests.get(
                        f"{ADZUNA_API}/jobs/{country}/search/{page}", params=params,
                        headers={"User-Agent": HEADERS["User-Agent"]}, timeout=TIMEOUT)
                    resp.raise_for_status()
                    items = resp.json().get("results") or []
                    ok += 1
                except Exception as e:
                    errors.append(f"{country}/{q}: {str(e).replace(app_key, '***').replace(app_id, '***')[:80]}")
                    break
                for j in items:
                    link = j.get("redirect_url") or ""
                    if not link or link in seen:
                        continue
                    seen.add(link)
                    results.append({
                        "title": clean_text(j.get("title", ""), 300),
                        "url": link,
                        "employer": ((j.get("company") or {}).get("display_name") or "").strip(),
                        "location": (j.get("location") or {}).get("display_name", ""),
                        "description": clean_text(j.get("description", "")),
                    })
                if len(items) < 50:
                    break
    if not ok and errors:
        raise RuntimeError("; ".join(errors[:3]))
    if errors:
        print(f"    Adzuna, recherches en erreur : {'; '.join(errors[:5])}", file=sys.stderr)
    return results


SOURCES = {"generic": scrape_generic, "greenhouse": scrape_greenhouse, "lever": scrape_lever,
           "workday": scrape_workday, "adzuna": scrape_adzuna}


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
    try:
        settings = load_yaml(config_path)
        companies = [c for c in load_yaml(companies_path).get("companies", []) if c.get("enabled", True)]
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (vers la ligne {mark.line + 1})" if mark else ""
        sys.exit(f"ERREUR de mise en forme dans un fichier .yaml{where} : vérifiez les espaces en début de ligne.\n{e}")
    profile = Profile(settings)

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
                    "id": oid, "source": name, "company": o.get("employer") or name,
                    "title": o["title"], "url": o["url"],
                    "location": o.get("location", ""),
                    "description": o.get("description", "")[:600],
                    "score": o["score"],
                    "first_seen": old.get("first_seen", ts), "last_seen": ts, "active": True,
                }
            # offres disparues de cette source. Pour un agrégateur (Adzuna), une offre peut juste être
            # sortie des premiers résultats : on ne la retire qu'après 14 jours sans la revoir.
            grace = 14 if c.get("type") == "adzuna" else 0
            limit = (datetime.now(timezone.utc) - timedelta(days=grace)).isoformat(timespec="seconds")
            for oid, o in offers.items():
                if o.get("source", o["company"]) == name and oid not in seen_ids and o.get("last_seen", "") <= limit:
                    o["active"] = False
            runs.append({"company": name, "ok": True, "message": f"{len(raw)} annonces lues", "found": len(found), "last_run": ts})
            print(f"OK  {name}: {len(found)} offres de stage sur {len(raw)} annonces")
        except Exception as e:  # une entreprise en échec ne bloque pas les autres (ses anciennes offres sont conservées)
            runs.append({"company": name, "ok": False, "message": str(e)[:300], "found": 0, "last_run": ts})
            print(f"ERR {name}: {e}", file=sys.stderr)

    close_browser()
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
