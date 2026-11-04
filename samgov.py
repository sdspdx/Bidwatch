"""Nationwide / by-state federal solicitations from the SAM.gov Get Opportunities API (v2)."""
import datetime as dt
import json
import logging

from fetcher import fetch, html_to_text
from highlight import find_keywords
from bs4 import BeautifulSoup

API_URL = "https://api.sam.gov/opportunities/v2/search"
SERVICE = "sam.gov"
log = logging.getLogger("bidwatch")


def _budget_left(db, cfg):
    return cfg["sam_gov"]["max_api_calls_per_day"] - db.api_calls_today(SERVICE)


def _with_key(url, key):
    if "api_key=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "api_key=" + key


def run_sam(session, db, cfg, kw_re, report):
    sc = cfg["sam_gov"]
    key = sc.get("api_key", "").strip()
    if not sc.get("enabled"):
        return
    if not key:
        report.warnings.append("SAM.gov search is turned on but no API key is set (Settings → SAM.gov).")
        return
    if not cfg["keywords"]:
        report.warnings.append("No keywords are set, so SAM.gov results were not filtered or emailed.")
        return

    today = dt.date.today()
    posted_from = (today - dt.timedelta(days=max(1, int(sc.get("days_back", 1))))).strftime("%m/%d/%Y")
    posted_to = today.strftime("%m/%d/%Y")
    excluded = {t.lower() for t in sc.get("exclude_types", [])}
    states = cfg["states"] or [None]

    new_items = []
    for state in states:
        offset = 0
        while True:
            if _budget_left(db, cfg) <= 0:
                report.warnings.append("SAM.gov daily API call limit reached; some results may be missing. "
                                       "Choose fewer states or raise the limit if your key allows it.")
                break
            params = {"api_key": key, "postedFrom": posted_from, "postedTo": posted_to,
                      "limit": 1000, "offset": offset}
            if state:
                params["state"] = state
            try:
                resp = session.get(API_URL, params=params, timeout=max(60, cfg["request_timeout"]))
                db.count_api_call(SERVICE)
            except Exception as e:  # noqa: BLE001
                report.warnings.append(f"SAM.gov could not be reached: {e.__class__.__name__}")
                return
            if resp.status_code == 429:
                report.warnings.append("SAM.gov says the API key's rate limit is used up for today.")
                break
            if resp.status_code in (401, 403):
                report.warnings.append("SAM.gov rejected the API key (it may be wrong or expired; "
                                       "keys must be renewed every 90 days).")
                return
            if resp.status_code != 200:
                report.warnings.append(f"SAM.gov returned HTTP {resp.status_code}.")
                break
            data = resp.json()
            items = data.get("opportunitiesData") or []
            total = int(data.get("totalRecords") or 0)
            for it in items:
                if (it.get("type") or "").lower() in excluded:
                    continue
                if db.solicitation_seen(it.get("noticeId")):
                    continue
                new_items.append(it)
            offset += len(items)
            report.sam_scanned += len(items)
            if not items or offset >= total:
                break

    # de-duplicate across states
    unique = {}
    for it in new_items:
        unique.setdefault(it.get("noticeId"), it)
    log.info("SAM.gov: %d new notices to scan", len(unique))

    attachments_left = int(sc.get("max_attachments_per_run", 300))
    for it in unique.values():
        sol = _shape(it)
        where = {}          # keyword -> list of places it was found
        snippets = []

        def note(found, place, text):
            for k in found:
                where.setdefault(k, []).append(place)
            if found:
                snippets.append((place, text))

        note(find_keywords(sol["title"], kw_re, cfg["keywords"]), "title", "")

        desc_url = it.get("description") or ""
        if sc.get("fetch_descriptions") and desc_url.startswith("http") and _budget_left(db, cfg) > 0:
            text = _description(session, desc_url, key, cfg)
            db.count_api_call(SERVICE)
            if text:
                note(find_keywords(text, kw_re, cfg["keywords"]), "description", text)

        if sc.get("scan_attachments"):
            for link in (it.get("resourceLinks") or []):
                if attachments_left <= 0:
                    break
                attachments_left -= 1
                res = fetch(session, link, cfg)
                if not res.ok and res.status in ("401", "403"):
                    res = fetch(session, _with_key(link, key), cfg)
                if not res.ok or not res.text:
                    continue
                name = _filename(res) or "attachment"
                note(find_keywords(res.text, kw_re, cfg["keywords"]), f"attachment: {name}", res.text)

        matched = list(where.keys())
        db.save_solicitation(sol, matched)
        if matched:
            prev = db.previous_match_for_number(sol["number"]) if sol["number"] else None
            sol["is_update"] = bool(prev and prev["notice_id"] != sol["notice_id"])
            sol["where"] = where
            sol["snippets"] = snippets
            report.sam_matches.append(sol)


def _shape(it):
    pop = it.get("placeOfPerformance") or {}
    state = ((pop.get("state") or {}).get("code") or "") if isinstance(pop, dict) else ""
    return {
        "notice_id": it.get("noticeId", ""),
        "number": it.get("solicitationNumber") or "",
        "title": it.get("title") or "(no title)",
        "agency": it.get("fullParentPathName") or it.get("department") or "",
        "type": it.get("type") or "",
        "posted": it.get("postedDate") or "",
        "deadline": it.get("responseDeadLine") or "",
        "state": state,
        "link": it.get("uiLink") or f"https://sam.gov/opp/{it.get('noticeId', '')}/view",
        "set_aside": it.get("typeOfSetAsideDescription") or "",
        "naics": it.get("naicsCode") or "",
    }


def _description(session, url, key, cfg):
    try:
        resp = session.get(_with_key(url, key), timeout=cfg["request_timeout"])
        if resp.status_code != 200:
            return ""
        try:
            body = resp.json().get("description", "")
        except (json.JSONDecodeError, ValueError, AttributeError):
            body = resp.text
        return html_to_text(BeautifulSoup(body, "html.parser")) if "<" in body else body
    except Exception:  # noqa: BLE001
        return ""


def _filename(res):
    import re
    from urllib.parse import unquote, urlparse
    cd = res.headers.get("Content-Disposition", "") or res.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd, re.I)
    if m:
        return unquote(m.group(1))
    path = urlparse(res.final_url or res.url).path
    name = unquote(path.rsplit("/", 1)[-1])
    return name if name and name != "download" else ""
