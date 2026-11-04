"""
BidWatch discovery agent — finds the bids / procurement pages of public agencies and firms
and emails them to you as a CSV that BidWatch's "Import CSV…" accepts directly.

Where the list of organizations comes from
  1. The official registry of every .gov website (CISA "dotgov-data"): cities, towns, villages,
     counties, special districts (ports, utilities, water/sewer, transit), states, tribes.
  2. Wikidata (optional): US cities, counties and ports whose website is not a .gov address.
  3. Your own list (optional): a CSV with  name,website,state,type  — use this for engineering firms,
     utilities, contractors or anything else not on a public list.
  4. Brave Search API (optional, needs a key): searches like "Washington engineering firm
     subconsultant opportunities" to find organizations that are on no list at all.

For each organization the agent opens the home page, follows procurement-looking links
("Bids & RFPs", "Purchasing", "Solicitations", Bids.aspx …), checks common addresses, confirms the page
really lists bids, and notes any outside bid portal it uses (BidNet, Bonfire, DemandStar, PlanetBids …).
It works through the country a batch at a time and remembers what it has already checked.
"""
import concurrent.futures as cf
import csv
import datetime as dt
import io
import logging
import os
import re
import sqlite3
import threading
import time
from urllib import robotparser
from urllib.parse import urljoin, urlparse

from config import APP_DIR, CLOUD, REPORT_DIR
from fetcher import fetch, make_session

log = logging.getLogger("bidwatch")

DISCOVERY_DB = os.path.join(APP_DIR, "discovery.db")
DOTGOV_URL = "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-full.csv"
WIKIDATA_URL = "https://query.wikidata.org/sparql"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

ALL_TYPES = ["city", "county", "port", "utility", "transit", "special_district", "state", "tribal",
             "school_district", "engineering_firm", "other"]
DEFAULT_TYPES = ["city", "county", "port", "utility", "engineering_firm"]

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico", "GU": "Guam",
    "VI": "U.S. Virgin Islands", "AS": "American Samoa", "MP": "Northern Mariana Islands",
}

# Outside bid portals many agencies use
PORTAL_HOSTS = {
    "bidnetdirect.com": "BidNet Direct", "bonfirehub.com": "Bonfire", "demandstar.com": "DemandStar",
    "publicpurchase.com": "Public Purchase", "procurement.opengov.com": "OpenGov Procurement",
    "planetbids.com": "PlanetBids", "questcdn.com": "QuestCDN", "ionwave.net": "IonWave",
    "bidexpress.com": "Bid Express", "bidsync.com": "BidSync", "periscopeholdings.com": "Periscope",
    "mercell.com": "Mercell", "negometrix.com": "Negometrix", "vendorregistry.com": "Vendor Registry",
    "bidsandtenders.com": "Bids&Tenders", "civicbid.com": "CivicBid", "infotechinc.net": "Bid Express",
    "ebidexchange.com": "eBid Exchange", "biddingo.com": "Biddingo", "vendorlink.com": "VendorLink",
    "procurenow.com": "ProcureNow", "jaggaer.com": "Jaggaer", "sciquest.com": "Jaggaer",
    "buyspeed.com": "BuySpeed", "ebuy.com": "eBuy", "bidbuy.com": "BidBuy", "pdq.net": "Plan Room",
    "planhouse.com": "Plan Room", "builderspace.com": "BuilderSpace", "isqft.com": "iSqFt",
    "constructconnect.com": "ConstructConnect", "smartprocure.us": "SmartProcure",
    "govspend.com": "GovSpend", "bidcontender.com": "BidContender", "pantera1.com": "Plan Room",
}
NOT_BIDS_HOSTS = ("govdeals.com", "publicsurplus.com", "facebook.com", "twitter.com", "x.com",
                  "linkedin.com", "youtube.com", "instagram.com", "governmentjobs.com", "neogov.com")

TEXT_RULES = [
    (re.compile(r"\b(current|open|active|available)\s+(bids|solicitations|rfps?|rfqs?|opportunities|projects)\b", re.I), 10),
    (re.compile(r"\bbid(ding)?\s*(opportunit\w*|postings?|board|list|notices?|documents)\b", re.I), 10),
    (re.compile(r"\bbids?\s*(&|and|/|,)\s*(rfps?|rfqs?|proposals|quotes|solicitations|contracts)\b", re.I), 10),
    (re.compile(r"\b(rfps?|proposals)\s*(&|and|/|,)\s*(bids|rfqs?)\b", re.I), 10),
    (re.compile(r"\b(solicitations?|procurement opportunit\w*|contracting opportunit\w*|"
                r"business opportunit\w*|vendor opportunit\w*|subcontract\w* opportunit\w*|"
                r"subconsultant opportunit\w*)\b", re.I), 9),
    (re.compile(r"^\s*(bids|bidding|bid info\w*|rfps?)\s*$", re.I), 9),
    (re.compile(r"\brequests? for (proposals|qualifications|bids|quotes)\b", re.I), 8),
    (re.compile(r"\b(invitations?|notices?) (to|for) bid(ders)?\b", re.I), 8),
    (re.compile(r"\bplan ?room\b", re.I), 7),
    (re.compile(r"\b(procurement|purchasing|contracting)\b", re.I), 6),
    (re.compile(r"\b(rfps?|rfqs?|itbs?|ifbs?)\b", re.I), 6),
    (re.compile(r"\b(doing business with|vendors?|suppliers?)\b", re.I), 3),
    (re.compile(r"\b(finance|business|departments?)\b", re.I), 1),
]
URL_RULE = re.compile(r"(bids?\.aspx|/bids?(/|$|\?|\.)|bid-?opportunit|/solicitations?|/procurement|"
                      r"/purchasing|/rfps?(/|$|-)|rfp-?rfq|/bidding|/contracting-opportunit|planroom)", re.I)
NEGATIVE = re.compile(r"\b(jobs?|employment|careers?|bid farewell|surplus|auctions?|elections?|"
                      r"results? of|awarded|tabulation|login|sign in|register to vote|budget)\b", re.I)
VERIFY_WORDS = re.compile(r"\b(bids?|rfps?|rfqs?|solicitations?|proposals?|procurement|purchasing|"
                          r"addend(um|a)|due date|closing date|bid opening|submittals?|specifications|"
                          r"pre-?bid|vendors?)\b", re.I)
COMMON_PATHS = ["/Bids.aspx", "/bids", "/procurement", "/purchasing", "/bids-rfps", "/rfps",
                "/bid-opportunities", "/business/bids"]
HUB_TEXT = re.compile(r"\b(doing business|business|finance|departments?|vendors?|government|services|"
                      r"administration|public works|engineering)\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orgs (
    website TEXT PRIMARY KEY,         -- host name, e.g. cityoftacoma.gov
    name TEXT, type TEXT, state TEXT, city TEXT, source TEXT,
    added_on TEXT, last_scanned TEXT, status TEXT,   -- found | none | unreachable | blocked
    page_url TEXT, page_text TEXT, portal_url TEXT, portal_name TEXT,
    score INTEGER, confidence TEXT, note TEXT, emailed_on TEXT
);
CREATE INDEX IF NOT EXISTS orgs_scan ON orgs(last_scanned);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DiscoveryDB:
    def __init__(self, path=DISCOVERY_DB):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def upsert_org(self, website, name, typ, state, city="", source=""):
        with self.lock:
            self.conn.execute(
                "INSERT INTO orgs (website, name, type, state, city, source, added_on) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(website) DO UPDATE SET name=COALESCE(NULLIF(orgs.name,''), excluded.name)",
                (website, name, typ, state, city, source, now()))

    def commit(self):
        with self.lock:
            self.conn.commit()

    def to_scan(self, types, states, rescan_days, limit):
        cutoff = (dt.datetime.now() - dt.timedelta(days=rescan_days)).strftime("%Y-%m-%d %H:%M:%S")
        q = "SELECT * FROM orgs WHERE (last_scanned IS NULL OR last_scanned < ?)"
        args = [cutoff]
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        if states:
            q += f" AND state IN ({','.join('?' * len(states))})"
            args += states
        q += " ORDER BY last_scanned IS NOT NULL, last_scanned, state, name LIMIT ?"
        args.append(limit)
        return self.conn.execute(q, args).fetchall()

    def save_result(self, website, res):
        with self.lock:
            self.conn.execute(
                "UPDATE orgs SET last_scanned=?, status=?, page_url=?, page_text=?, portal_url=?, portal_name=?, "
                "score=?, confidence=?, note=? WHERE website=?",
                (now(), res["status"], res.get("page_url", ""), res.get("page_text", ""),
                 res.get("portal_url", ""), res.get("portal_name", ""), res.get("score", 0),
                 res.get("confidence", ""), res.get("note", ""), website))
            self.conn.commit()

    def unsent_finds(self):
        return self.conn.execute("SELECT * FROM orgs WHERE status='found' AND emailed_on IS NULL "
                                 "ORDER BY state, type, name").fetchall()

    def mark_emailed(self, websites):
        with self.lock:
            self.conn.executemany("UPDATE orgs SET emailed_on=? WHERE website=?",
                                  [(now(), w) for w in websites])
            self.conn.commit()

    def get_meta(self, k):
        r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else None

    def set_meta(self, k, v):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?,?)", (k, v))
            self.conn.commit()

    def counts(self):
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM orgs GROUP BY status").fetchall()
        names = {"found": "bid page found", "none": "no bid page", "unreachable": "website down",
                 None: "not checked yet"}
        return {names.get(r["status"], r["status"]): r["n"] for r in rows}


# ================================================================== sources
def classify_district(name):
    n = name.lower()
    if re.search(r"\bports?\b|port authority|marine terminal|navigation district", n):
        return "port"
    if re.search(r"utilit|\bpud\b|water|sewer|wastewater|sanitar|electric|power|light|energy|\bgas\b|"
                 r"irrigation|reclamation|aqueduct|drainage|stormwater|solid waste|recycling|public service", n):
        return "utility"
    if re.search(r"harbou?r|waterway", n):
        return "port"
    if re.search(r"transit|transportation|airport|aviation|\brail|bus\b|toll|turnpike|bridge", n):
        return "transit"
    if re.search(r"school|college|education|university", n):
        return "school_district"
    return "special_district"


def load_dotgov(db, session, cfg):
    log.info("Downloading the .gov website registry…")
    resp = session.get(DOTGOV_URL, timeout=120)
    resp.raise_for_status()
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig", "ignore"))))
    skip_words = re.compile(r"\b(police|sheriff|fire|ems|court|clerk of|judge|library|election|vote|"
                            r"museum|tourism|visit|parks? and rec|animal|jail|da\b|attorney|911|"
                            r"health department|coroner)\b", re.I)
    n = 0
    for r in rows:
        dtype = (r.get("Domain type") or "").strip()
        name = (r.get("Organization name") or "").strip()
        sub = (r.get("Suborganization name") or "").strip()
        domain = (r.get("Domain name") or "").strip().lower()
        state = (r.get("State") or "").strip().upper()
        if not domain or state not in STATE_NAMES or "Election" in dtype:
            continue
        typ = {"City": "city", "County": "county", "Special district": None, "State or territory": "state",
               "Tribal": "tribal", "School district": "school_district", "Interstate": None}.get(dtype, "skip")
        if typ == "skip":
            continue
        if typ is None:
            typ = classify_district(name + " " + sub)
        if skip_words.search(f"{sub} {domain}") or (typ in ("city", "county") and skip_words.search(name)):
            continue  # a police/library/court site, not the main government site
        label = name if not sub or sub.lower() in name.lower() else f"{name} — {sub}"
        db.upsert_org(domain, label, typ, state, (r.get("City") or "").strip(), "dotgov")
        n += 1
    db.commit()
    log.info(".gov registry: %d organizations", n)
    return n


WIKIDATA_CLASSES = {  # class item -> our type. Each is queried separately; a bad class just returns nothing.
    "Q1093829": "city",     # city in the United States
    "Q15127012": "city",    # town in the United States
    "Q47168": "county",     # county of the United States
    "Q44782": "port",       # port
}


def load_wikidata(db, session, cfg):
    added = 0
    for qid, typ in WIKIDATA_CLASSES.items():
        query = f"""
SELECT DISTINCT ?itemLabel ?website ?iso WHERE {{
  ?item wdt:P31/wdt:P279* wd:{qid} ; wdt:P17 wd:Q30 ; wdt:P856 ?website .
  OPTIONAL {{ ?item wdt:P131* ?st . ?st wdt:P300 ?iso . FILTER(STRSTARTS(?iso, "US-")) }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}"""
        try:
            resp = session.get(WIKIDATA_URL, params={"query": query, "format": "json"}, timeout=120,
                               headers={"Accept": "application/sparql-results+json"})
            resp.raise_for_status()
            for b in resp.json()["results"]["bindings"]:
                site = b["website"]["value"]
                host = urlparse(site).netloc.lower().removeprefix("www.")
                state = b.get("iso", {}).get("value", "")[3:]
                if not host or host.endswith(".gov") or state not in STATE_NAMES:
                    continue  # .gov sites already come from the registry
                db.upsert_org(host, b["itemLabel"]["value"], typ, state, "", "wikidata")
                added += 1
            db.commit()
        except Exception as e:  # noqa: BLE001
            log.warning("Wikidata (%s) skipped: %s", typ, e.__class__.__name__)
        time.sleep(2)
    log.info("Wikidata: %d organizations", added)
    return added


def load_seed_csv(db, path):
    """Your own list: name,website,state,type (type e.g. engineering_firm, utility, port, city)."""
    if not path or not os.path.exists(path):
        return 0
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for rec in csv.reader(f):
            if len(rec) < 2 or rec[0].strip().lower() == "name":
                continue
            site = rec[1].strip()
            if not site:
                continue
            host = urlparse(site if "://" in site else "https://" + site).netloc.lower().removeprefix("www.")
            state = (rec[2].strip().upper() if len(rec) > 2 else "")
            typ = (rec[3].strip().lower().replace(" ", "_") if len(rec) > 3 and rec[3].strip() else "other")
            db.upsert_org(host, rec[0].strip(), typ if typ in ALL_TYPES else "other", state, "", "your list")
            n += 1
    db.commit()
    log.info("Your list: %d organizations", n)
    return n


BRAVE_TOPICS = {
    "engineering_firm": ["{state} engineering firm subconsultant opportunities",
                         "{state} engineering firm invitation to bid subcontractors"],
    "port": ["port of {state} bids procurement", "{state} port district solicitations"],
    "utility": ["{state} public utility district bids", "{state} water district bid opportunities",
                "{state} electric cooperative requests for proposals"],
    "city": ["{state} city bids and rfps", "{state} town bid opportunities"],
    "county": ["{state} county purchasing bids"],
}


def load_brave(db, session, cfg, types, states):
    dc = cfg["discovery"]
    key = dc.get("brave_api_key", "").strip()
    if not key:
        return 0
    budget = int(dc.get("brave_max_queries_per_run", 50))
    done = set((db.get_meta("brave_done") or "").split("|"))
    n = 0
    for st in (states or list(STATE_NAMES)):
        for typ in types:
            for tmpl in BRAVE_TOPICS.get(typ, []):
                q = tmpl.format(state=STATE_NAMES.get(st, st))
                if q in done or budget <= 0:
                    continue
                budget -= 1
                try:
                    resp = session.get(BRAVE_URL, params={"q": q, "count": 20, "country": "us"},
                                       headers={"X-Subscription-Token": key, "Accept": "application/json"},
                                       timeout=30)
                    if resp.status_code != 200:
                        log.warning("Brave search returned HTTP %s", resp.status_code)
                        if resp.status_code in (401, 403, 429):
                            budget = 0
                        continue
                    for item in (resp.json().get("web", {}).get("results") or []):
                        host = urlparse(item.get("url", "")).netloc.lower().removeprefix("www.")
                        if not host or any(h in host for h in NOT_BIDS_HOSTS) or _portal_name(host):
                            continue
                        db.upsert_org(host, item.get("title", host)[:120], typ, st, "", "search")
                        n += 1
                    done.add(q)
                except Exception as e:  # noqa: BLE001
                    log.warning("Brave search error: %s", e.__class__.__name__)
                time.sleep(1.1)
    db.set_meta("brave_done", "|".join(sorted(done)))
    db.commit()
    log.info("Search: %d organizations", n)
    return n


def refresh_sources(db, session, cfg, types, states):
    dc = cfg["discovery"]
    last = db.get_meta("sources_loaded")
    stale = (not last) or (dt.datetime.fromisoformat(last) < dt.datetime.now() - dt.timedelta(days=30))
    if stale:
        if dc.get("use_dotgov", True):
            try:
                load_dotgov(db, session, cfg)
            except Exception as e:  # noqa: BLE001
                log.warning("Could not load the .gov registry: %s", e)
        if dc.get("use_wikidata", True):
            load_wikidata(db, session, cfg)
        db.set_meta("sources_loaded", dt.datetime.now().isoformat(timespec="seconds"))
    load_seed_csv(db, dc.get("seed_csv", ""))
    load_brave(db, session, cfg, types, states)


# ================================================================== crawling one website
def _portal_name(host):
    host = host.lower()
    for h, name in PORTAL_HOSTS.items():
        if host == h or host.endswith("." + h):
            return name
    return ""


def _same_site(a, b):
    ha, hb = urlparse(a).netloc.lower().removeprefix("www."), urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def score_link(text, url):
    text = (text or "").strip()
    host = urlparse(url).netloc.lower()
    if any(h in host for h in NOT_BIDS_HOSTS):
        return 0
    s = 0
    for rx, w in TEXT_RULES:
        if rx.search(text):
            s = max(s, w)
    if URL_RULE.search(urlparse(url).path + "?" + urlparse(url).query):
        s += 5 if s >= 3 else 4
    if _portal_name(host):
        s += 10
    if NEGATIVE.search(text) or re.search(r"(job|career|employment|surplus|auction)", url, re.I):
        s -= 8
    if len(text) > 80:
        s -= 2
    return s


def verify_page(r):
    """Does this page look like it lists bids/solicitations?"""
    if not r.ok or not r.text:
        return False
    hits = len(VERIFY_WORDS.findall(r.text[:60000]))
    return hits >= 4


class SiteBudget:
    """Caps how many pages are opened on one website."""

    def __init__(self, n):
        self.left = n


def scan_site(org, cfg):
    """Find the bids page for one organization. Returns a result dict."""
    dc = cfg["discovery"]
    session = make_session(cfg)
    local_cfg = dict(cfg, request_timeout=dc.get("timeout", 20), max_document_mb=5)
    budget = SiteBudget(int(dc.get("max_requests_per_site", 14)))
    host = org["website"]

    def get(url):
        if budget.left <= 0:
            return None
        if robots and not robots_ok(url):
            return None
        budget.left -= 1
        time.sleep(float(dc.get("delay_seconds", 0.5)))
        return fetch(session, url, local_cfg)

    robots = None
    home = None
    for start in (f"https://{host}/", f"http://{host}/", f"https://www.{host}/"):
        budget.left -= 1
        home = fetch(session, start, local_cfg)
        if home.ok:
            break
    if not home or not home.ok:
        return {"status": "unreachable", "note": (home.error if home else "")[:200]}

    base = home.final_url or f"https://{host}/"
    rp = robotparser.RobotFileParser()
    try:
        rr = session.get(urljoin(base, "/robots.txt"), timeout=10)
        if rr.status_code == 200 and "html" not in rr.headers.get("Content-Type", ""):
            rp.parse(rr.text.splitlines())
            robots = rp
    except Exception:  # noqa: BLE001
        pass
    ua = cfg["user_agent"]

    def robots_ok(url):
        return robots.can_fetch(ua, url) if robots else True

    # candidate links from the home page
    cands = {}

    def add_links(page, depth):
        for text, url in page.links:
            if not url.startswith("http"):
                continue
            portal = _portal_name(urlparse(url).netloc)
            if not portal and not _same_site(url, base):
                continue
            s = score_link(text, url) - depth
            if s > cands.get(url, ("", -99))[1]:
                cands[url] = (text, s)

    add_links(home, 0)

    def best(min_score):
        return sorted(((u, t, s) for u, (t, s) in cands.items() if s >= min_score), key=lambda x: -x[2])

    # follow a couple of "hub" pages (Business, Departments, Finance) if nothing strong yet
    if not best(9):
        hubs = sorted(((u, t, s) for u, (t, s) in cands.items()
                       if 3 <= s < 9 or (HUB_TEXT.search(t or "") and _same_site(u, base))),
                      key=lambda x: -x[2])[:3]
        for u, _t, _s in hubs:
            page = get(u)
            if page and page.ok:
                add_links(page, 1)
            if best(9):
                break

    # try the usual addresses (CivicPlus sites use /Bids.aspx, etc.)
    if not best(8):
        for path in COMMON_PATHS:
            url = urljoin(base, path)
            if url in cands:
                continue
            page = get(url)
            if page and page.ok and _same_site(page.final_url, base) and verify_page(page):
                title = page.soup.title.get_text(" ", strip=True)[:80] if page.soup and page.soup.title else path
                cands[page.final_url] = (title, 9)
                break

    # confirm the best candidates
    portal_url, portal_name = "", ""
    for url, text, s in best(6)[:4]:
        pname = _portal_name(urlparse(url).netloc)
        if pname:
            if not portal_url:
                portal_url, portal_name = url, pname
            continue
        page = get(url)
        if page is None:
            continue
        if page.ok and verify_page(page):
            for t2, u2 in page.links:  # outside portal linked from the bids page?
                pn = _portal_name(urlparse(u2).netloc)
                if pn and not portal_url:
                    portal_url, portal_name = u2, pn
            conf = "high" if s >= 9 else "medium"
            return {"status": "found", "page_url": page.final_url or url, "page_text": (text or "")[:120],
                    "portal_url": portal_url, "portal_name": portal_name, "score": s, "confidence": conf}
    if portal_url:
        return {"status": "found", "page_url": portal_url, "page_text": f"{portal_name} bid portal",
                "portal_url": portal_url, "portal_name": portal_name, "score": 10, "confidence": "high",
                "note": "agency posts bids on an outside portal (may need a free vendor account)"}
    return {"status": "none", "note": "no bids page found on the website"}


# ================================================================== run + email
CSV_HEADER = ["url", "label", "state", "type", "organization", "website", "bid_portal", "portal_name",
              "confidence", "link_text", "found_on"]


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for r in rows:
            label = r["name"] + (f" — {r['type'].replace('_', ' ')}" if r["type"] else "")
            w.writerow([r["page_url"], label[:120], r["state"], r["type"], r["name"], r["website"],
                        r["portal_url"] or "", r["portal_name"] or "", r["confidence"], r["page_text"] or "",
                        (r["last_scanned"] or "")[:10]])


def email_body(rows, stats, cfg):
    import html
    total = sum(stats.values())
    h = ['<div style="font-family:Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#222;max-width:900px;">',
         f"<h2>BidWatch discovery: {len(rows)} new bid page(s) found</h2>",
         ("<p>To watch any of these, copy their rows from the attached CSV into <b>urls.csv</b> in your GitHub "
          "repository (BidWatch reads the first three columns: url, label, state). They're watched from the "
          "next daily run.</p>" if CLOUD else
          "<p>The attached CSV is ready for BidWatch: open BidWatch → <b>Watched URLs</b> → <b>Import CSV…</b> "
          "and pick the file. (BidWatch reads the first three columns: url, label, state.)</p>"),
         f'<p style="color:#555;font-size:13px;">Progress so far: {total:,} organizations on the list — ' +
         ", ".join(f"{v:,} {html.escape(k)}" for k, v in sorted(stats.items())) + ".</p>",
         '<table style="border-collapse:collapse;font-size:13px;">'
         '<tr style="background:#eee;"><th align="left" style="padding:4px;">State</th>'
         '<th align="left" style="padding:4px;">Organization</th><th align="left" style="padding:4px;">Type</th>'
         '<th align="left" style="padding:4px;">Bids page</th><th align="left" style="padding:4px;">Portal</th></tr>']
    for r in rows[:300]:
        portal = (f'<a href="{html.escape(r["portal_url"])}">{html.escape(r["portal_name"] or "portal")}</a>'
                  if r["portal_url"] and r["portal_url"] != r["page_url"] else "")
        h.append(f'<tr><td style="padding:3px 4px;">{html.escape(r["state"] or "")}</td>'
                 f'<td style="padding:3px 4px;">{html.escape(r["name"])}</td>'
                 f'<td style="padding:3px 4px;">{html.escape(r["type"].replace("_", " "))}</td>'
                 f'<td style="padding:3px 4px;"><a href="{html.escape(r["page_url"])}">'
                 f'{html.escape((r["page_text"] or r["page_url"])[:60])}</a></td>'
                 f'<td style="padding:3px 4px;">{portal}</td></tr>')
    h.append("</table>")
    if len(rows) > 300:
        h.append(f"<p>…and {len(rows) - 300} more in the attached CSV.</p>")
    h.append('<p style="color:#999;font-size:11px;">Sent by the BidWatch discovery agent.</p></div>')
    return "".join(h)


def run_discovery(send=True, max_sites=None, types=None, states=None, progress=None):
    from config import load_config
    from db import DB
    from report import save_html, send_email

    cfg = load_config()
    dc = cfg["discovery"]
    types = types or dc.get("types") or DEFAULT_TYPES
    if states is None:
        states = cfg["states"] if dc.get("use_bidwatch_states", True) else []
    max_sites = max_sites or int(dc.get("max_sites_per_run", 500))

    db = DiscoveryDB()
    session = make_session(cfg)
    refresh_sources(db, session, cfg, types, states)

    todo = db.to_scan(types, states, int(dc.get("rescan_days", 90)), max_sites)
    log.info("Discovery: scanning %d websites (types %s, %s)", len(todo), ",".join(types),
             ",".join(states) if states else "nationwide")
    done = 0
    with cf.ThreadPoolExecutor(max_workers=int(dc.get("workers", 8))) as pool:
        futures = {pool.submit(scan_site, org, cfg): org for org in todo}
        for fut in cf.as_completed(futures):
            org = futures[fut]
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                res = {"status": "unreachable", "note": f"{e.__class__.__name__}"}
            db.save_result(org["website"], res)
            done += 1
            if res["status"] == "found":
                log.info("  FOUND %s (%s): %s", org["name"], org["state"], res["page_url"])
            if progress:
                progress(done, len(todo))

    # skip pages already in BidWatch's master list
    bw = DB()
    rows = [r for r in db.unsent_finds() if not bw.url_exists(r["page_url"])]
    if dc.get("auto_add_to_bidwatch"):
        for r in rows:
            bw.add_url(r["page_url"], r["name"][:120], r["state"] or "", source="discovered")
    bw.close()

    stats = db.counts()
    path = ""
    if rows:
        os.makedirs(REPORT_DIR, exist_ok=True)
        path = os.path.join(REPORT_DIR, f"bid_pages_{dt.datetime.now():%Y-%m-%d_%H%M%S}.csv")
        write_csv(rows, path)
        body = email_body(rows, stats, cfg)
        save_html("discovery", body)
        if send:
            try:
                send_email(cfg, f"BidWatch discovery: {len(rows)} new bid page(s) to add", body,
                           attachments=[path])
                db.mark_emailed([r["website"] for r in rows])
                log.info("Discovery email sent with %d pages", len(rows))
            except Exception as e:  # noqa: BLE001
                log.error("Could not send discovery email: %s", e)
        else:
            db.mark_emailed([r["website"] for r in rows])
    log.info("Discovery finished: %d scanned, %d new pages. Totals: %s", done, len(rows), stats)
    return {"scanned": done, "new": len(rows), "csv": path, "stats": stats}

