"""
BidWatch — daily solicitation watcher.

    python bidwatch.py run              check everything and email the results
    python bidwatch.py run --no-email   same, but only save the report in the reports folder
    python bidwatch.py add URL [--label "City of Tacoma bids"] [--state WA] [--selector "#bids"]
    python bidwatch.py import urls.csv  add many URLs (each line: url,label,state)
    python bidwatch.py list             show the master URL list
    python bidwatch.py remove ID
    python bidwatch.py login ID         save a username/password for a site that needs sign-in
    python bidwatch.py schedule 06:00   run automatically every day (Windows Task Scheduler / Mac launchd)
    python bidwatch.py unschedule
    python bidwatch.py discover [--states WA,OR | --states ALL] [--types city,county,port,utility] [--max-sites 500]
                                        find bids/procurement pages and email them as a CSV
    python bidwatch.py schedule 01:30 --job discover   run the discovery agent every night
    python bidwatch.py test-email
    python bidwatch.py                  (no arguments) opens the settings window
"""
import argparse
import datetime as dt
import io
import json
import logging
import os
import sys
import subprocess
import traceback

import auth
from config import CLOUD, LOG_DIR, URLS_CSV, load_config
from db import DB
from fetcher import (content_hash, fetch, find_forward, is_document_link, make_session,
                     normalize_for_compare, _same_page)
from highlight import Painter, build_keyword_regex, diff_html, find_keywords
from report import Report, build_bad_links_html, build_daily_html, save_html, send_email
from samgov import run_sam

log = logging.getLogger("bidwatch")
TRANSIENT = {"timeout", "no-connection", "error", "500", "502", "503", "504", "429"}
PERMANENT = {"404", "410", "soft-404"}


def setup_logging():
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(os.path.join(LOG_DIR, "bidwatch.log"), encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)])


# ------------------------------------------------------------------ watched pages
def check_url(row, session, db, cfg, kw_re, painter, report, depth=0):
    url, url_id = row["url"], row["id"]
    log.info("Checking %s", url)
    r = fetch(session, url, cfg, selector=row["selector"] or "")

    # ---- sign-in wall?
    if auth.needs_login(r, url):
        cred = auth.get_credentials(db, url_id)
        if not cred:
            auth.set_status(db, url_id, "needed", "this site asks for a username and password",
                            login_url=r.final_url or url)
            report.login_needed.append({"id": url_id, "label": row["label"], "url": url,
                                        "detail": "Sign-in required — no username/password saved yet."})
            log.info("  sign-in required; waiting for credentials")
            return
        ok, msg, basic = auth.login(session, cred, cfg, url, first_result=r)
        if ok:
            r = fetch(session, url, cfg, selector=row["selector"] or "", auth=basic)
        if not ok or auth.needs_login(r, url):
            detail = msg if not ok else "signed in, but the page still asks for a sign-in"
            auth.set_status(db, url_id, "failed", detail)
            report.login_needed.append({"id": url_id, "label": row["label"], "url": url,
                                        "detail": f"Automatic sign-in failed: {detail}"})
            log.info("  sign-in failed: %s", detail)
            return
        auth.set_status(db, url_id, "ok", msg)
        log.info("  %s", msg)

    if not r.ok:
        new_url, how, suggestions = find_forward(session, url, cfg, first_result=r)
        if new_url and depth == 0 and not db.url_exists(new_url):
            log.info("  forwarded: %s -> %s (%s)", url, new_url, how)
            db.move_url(url_id, url, new_url, how)
            report.moves.append((row["label"], url, new_url, how))
            fresh = db.conn.execute("SELECT * FROM urls WHERE id=?", (url_id,)).fetchone()
            return check_url(fresh, session, db, cfg, kw_re, painter, report, depth=1)
        if new_url and db.url_exists(new_url):
            db.move_url(url_id, url, new_url, how)
            report.moves.append((row["label"], url, new_url, how + " (already on your list)"))
            return
        status = r.status or "error"
        first_failure = not row["bad_since"]
        db.mark_bad(url_id, status)
        # One-off outages are common; wait for a second failed day unless the page is clearly gone.
        if first_failure and status not in PERMANENT and not status.startswith("4"):
            log.info("  temporary failure (%s); will confirm tomorrow", r.error)
            return
        last = row["bad_notified_on"]
        due = (not last) or (dt.date.fromisoformat(last) <=
                             dt.date.today() - dt.timedelta(days=cfg["bad_link_reminder_days"]))
        if due:
            report.bad_links.append({"id": url_id, "label": row["label"], "url": url, "state": row["state"],
                                     "error": r.error or status, "since": (row["bad_since"] or "")[:10],
                                     "suggestions": suggestions})
        return

    report.pages_checked += 1
    # Site forwarded us somewhere else (301/302) — record the new address.
    if r.final_url and not _same_page(r.final_url, url) and depth == 0:
        if not db.url_exists(r.final_url):
            db.move_url(url_id, url, r.final_url, "the site forwards (redirects) to a new address")
            report.moves.append((row["label"], url, r.final_url, "the site forwards (redirects) to a new address"))
            url = r.final_url

    text = normalize_for_compare(r.text, cfg["ignore_patterns"])
    h = content_hash(text)
    first_time = row["content_hash"] is None
    old_links = json.loads(row["links_json"]) if row["links_json"] else None
    old_urls = {l[1] for l in old_links} if old_links is not None else set()

    new_links, seen = [], set()
    if old_links is not None:
        for t, u in r.links:
            if u not in old_urls and u not in seen and u != url:
                seen.add(u)
                new_links.append((t, u))

    # Documents linked from the page (only ones never opened before)
    documents = []
    if cfg["check_documents"]:
        budget = cfg["max_documents_per_page"]
        for t, u in r.links:
            if budget <= 0:
                break
            if not is_document_link(u) or db.document_seen(u):
                continue
            budget -= 1
            d = fetch(session, u, cfg)
            kws = find_keywords(d.text, kw_re, cfg["keywords"]) if d.ok else []
            db.save_document(u, url_id, kws)
            if kws:
                name = t or u.rsplit("/", 1)[-1]
                documents.append({"url": u, "name": name, "keywords": kws,
                                  "snippets": painter.snippets(d.text, max_snips=3)})

    changed = (not first_time) and (h != row["content_hash"] or new_links)
    diff, added_text = "", ""
    if changed and h != row["content_hash"]:
        diff, added_text = diff_html(painter, row["content_text"] or "", text)

    kw_found = set(find_keywords(added_text, kw_re, cfg["keywords"]))
    for t, _ in new_links:
        kw_found.update(find_keywords(t, kw_re, cfg["keywords"]))
    for d in documents:
        kw_found.update(d["keywords"])
    if first_time:
        kw_found.update(find_keywords(text, kw_re, cfg["keywords"]))

    # Add newly discovered keyword links to the master list
    if cfg["auto_add_keyword_links"]:
        candidates = r.links if first_time else new_links
        for t, u in candidates:
            if len(report.auto_added) >= cfg["max_auto_add_per_run"]:
                break
            if is_document_link(u) or not u.startswith("http") or not find_keywords(t, kw_re, cfg["keywords"]):
                continue
            if db.add_url(u, label=t[:120], state=row["state"], source="discovered"):
                report.auto_added.append((t, u))

    if changed or documents or (first_time and kw_found):
        report.page_changes.append({
            "label": row["label"], "url": url, "state": row["state"], "diff_html": diff,
            "new_links": new_links, "documents": documents, "keywords": sorted(kw_found, key=str.lower),
            "first_time": first_time, "page_text": text if first_time else ""})

    note = "baseline" if first_time else ("changed" if changed else "same")
    db.save_check(url_id, r.status, h, text, r.links, note)
    log.info("  %s%s", note, f" — keywords: {', '.join(sorted(kw_found))}" if kw_found else "")



# ------------------------------------------------------------------ urls.csv (cloud watch list)
def _read_csv_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig") as f:
        return f.read().splitlines()


def _csv_row(line):
    import csv
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    rec = next(csv.reader([s]), [])
    if not rec or not rec[0].strip().lower().startswith("http"):
        return None
    rec = [x.strip() for x in rec] + ["", ""]
    return rec[0], rec[1], rec[2].upper()


def sync_urls_csv(db, path=URLS_CSV):
    """Make the watch list match urls.csv: add new lines, pause URLs that were removed from it."""
    rows = [r for r in (_csv_row(line) for line in _read_csv_lines(path)) if r]
    if not os.path.exists(path):
        return 0, 0
    wanted = {u for u, _, _ in rows}
    added = 0
    for url, label, state in rows:
        cur = db.conn.execute("SELECT id FROM urls WHERE url=?", (url,)).fetchone()
        if cur is None:
            moved = db.conn.execute("SELECT url_id FROM url_moves WHERE old_url=? ORDER BY id DESC LIMIT 1",
                                    (url,)).fetchone()
            if moved:
                db.update_url_fields(moved["url_id"], active=1)
                continue
            added += db.add_url(url, label, state)
        else:
            fields = {"active": 1}
            if label:
                fields["label"] = label
            if state:
                fields["state"] = state
            db.update_url_fields(cur["id"], **fields)
    paused = 0
    for r in db.conn.execute("SELECT id, url FROM urls WHERE active=1 AND source='manual'").fetchall():
        olds = {m["old_url"] for m in db.conn.execute("SELECT old_url FROM url_moves WHERE url_id=?", (r["id"],))}
        if r["url"] not in wanted and not (olds & wanted):
            db.update_url_fields(r["id"], active=0)
            paused += 1
    return added, paused


def write_back_csv(db, path=URLS_CSV):
    """After a run: swap in forwarding addresses and list newly added keyword links in urls.csv."""
    import csv
    lines = _read_csv_lines(path) or ["url,label,state"]
    out, seen, changed = [], set(), False
    for line in lines:
        row = _csv_row(line)
        if row:
            url, label, state = row
            cur = db.conn.execute("SELECT url FROM urls WHERE url=?", (url,)).fetchone()
            if cur is None:
                m = db.conn.execute("SELECT u.url FROM url_moves m JOIN urls u ON u.id=m.url_id "
                                    "WHERE m.old_url=? ORDER BY m.id DESC LIMIT 1", (url,)).fetchone()
                if m and m["url"] != url:
                    buf = io.StringIO()
                    csv.writer(buf).writerow([m["url"], label, state])
                    line = buf.getvalue().rstrip("\r\n")
                    out.append(f"# moved {dt.date.today()}: {url}")
                    url, changed = m["url"], True
            seen.add(url)
        out.append(line)
    new = db.conn.execute("SELECT id, url, label, state FROM urls WHERE active=1 AND source='discovered'").fetchall()
    new = [r for r in new if r["url"] not in seen]
    if new:
        out.append(f"# added automatically {dt.date.today()} (new links containing your keywords)")
        for r in new:
            buf = io.StringIO()
            csv.writer(buf).writerow([r["url"], r["label"], r["state"]])
            out.append(buf.getvalue().rstrip("\r\n"))
            db.update_url_fields(r["id"], source="manual")
        changed = True
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("\n".join(out) + "\n")
    return changed


# ------------------------------------------------------------------ main run
def run(send=True):
    cfg = load_config()
    db = DB()
    session = make_session(cfg)
    kw_re = build_keyword_regex(cfg["keywords"])
    painter = Painter(kw_re, cfg["change_color"], cfg["keyword_color"])
    report = Report()
    if CLOUD:
        added, paused = sync_urls_csv(db)
        log.info("urls.csv: %d added, %d paused", added, paused)

    rows = db.urls_to_check(cfg["states"])
    log.info("BidWatch run: %d watched URLs, scope %s", len(rows), cfg["states"] or "nationwide")
    for row in rows:
        try:
            check_url(row, session, db, cfg, kw_re, painter, report)
        except Exception as e:  # noqa: BLE001
            log.error("Error on %s: %s\n%s", row["url"], e, traceback.format_exc())
            report.warnings.append(f"Error while checking {row['url']}: {e.__class__.__name__}")

    try:
        run_sam(session, db, cfg, kw_re, report)
    except Exception as e:  # noqa: BLE001
        log.error("SAM.gov error: %s\n%s", e, traceback.format_exc())
        report.warnings.append(f"SAM.gov search failed: {e.__class__.__name__}: {e}")

    # ---- daily report
    body = build_daily_html(report, cfg, painter)
    path = save_html("report", body)
    log.info("Report saved: %s", path)
    if report.has_keywords:
        n = len(report.sam_matches) + sum(1 for c in report.page_changes if c["keywords"])
        subject = f"★ KEYWORD MATCH — BidWatch: {n} hit(s) — {dt.date.today():%b %d}"
    else:
        subject = f"BidWatch: {len(report.page_changes)} page change(s) — {dt.date.today():%b %d}"
    want = report.has_news or report.warnings or cfg["email"].get("send_when_nothing_found")
    if send and want:
        try:
            send_email(cfg, subject, body, high_priority=report.has_keywords)
            log.info("Email sent: %s", subject)
        except Exception as e:  # noqa: BLE001
            log.error("Could not send email: %s", e)

    # ---- bad link notice (separate email)
    if report.bad_links:
        bad_body = build_bad_links_html(report.bad_links, cfg)
        save_html("bad_links", bad_body)
        if send:
            try:
                send_email(cfg, f"BidWatch: {len(report.bad_links)} link(s) need a new URL", bad_body,
                           high_priority=True)
                for b in report.bad_links:
                    db.mark_bad_notified(b["id"])
            except Exception as e:  # noqa: BLE001
                log.error("Could not send bad-link email: %s", e)
    if CLOUD:
        write_back_csv(db)
    db.close()

    # Ask for any missing passwords right away if someone is at the PC.
    if (report.login_needed and send and not CLOUD and sys.platform in ("win32", "darwin")
            and cfg.get("prompt_for_logins_on_desktop", True)):
        try:
            exe = sys.executable.replace("python.exe", "pythonw.exe") if sys.platform == "win32" else sys.executable
            subprocess.Popen([exe, os.path.abspath(__file__), "gui", "--logins"],
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                             start_new_session=(sys.platform != "win32"))
        except Exception as e:  # noqa: BLE001
            log.info("Could not open the sign-in window: %s", e)
    return report, path


# ------------------------------------------------------------------ command line
def main():
    if len(sys.argv) == 1 and not CLOUD:
        import settings_gui
        settings_gui.main(prompt_logins=True)
        return
    p = argparse.ArgumentParser(description="BidWatch solicitation watcher")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--no-email", action="store_true")
    a = sub.add_parser("add")
    a.add_argument("url")
    a.add_argument("--label", default="")
    a.add_argument("--state", default="")
    a.add_argument("--selector", default="")
    i = sub.add_parser("import")
    i.add_argument("file")
    sub.add_parser("list")
    sub.add_parser("sync-urls", help="make the watch list match urls.csv")
    rm = sub.add_parser("remove")
    rm.add_argument("id", type=int)
    sub.add_parser("test-email")
    sc = sub.add_parser("schedule", help="run automatically every day at HH:MM")
    sc.add_argument("time")
    sc.add_argument("--job", choices=["run", "discover"], default="run")
    us = sub.add_parser("unschedule")
    us.add_argument("--job", choices=["run", "discover"], default="run")
    ds = sub.add_parser("discover", help="find bids/procurement pages and email them as a CSV")
    ds.add_argument("--no-email", action="store_true")
    ds.add_argument("--max-sites", type=int)
    ds.add_argument("--states", help="e.g. WA,OR (default: the states in settings; ALL = nationwide)")
    ds.add_argument("--types", help="e.g. city,county,port,utility,engineering_firm")
    lg = sub.add_parser("login", help="save a username/password for a watched URL")
    lg.add_argument("id", type=int)
    g = sub.add_parser("gui")
    g.add_argument("--logins", action="store_true", help="open straight to the sign-in prompts")
    args = p.parse_args()

    setup_logging()
    if args.cmd == "run":
        run(send=not args.no_email)
    elif args.cmd == "add":
        db = DB()
        print("Added." if db.add_url(args.url, args.label, args.state, args.selector) else "Already on the list.")
    elif args.cmd == "sync-urls":
        db = DB()
        added, paused = sync_urls_csv(db)
        db.close()
        print(f"urls.csv: {added} added, {paused} paused.")
    elif args.cmd == "import":
        import csv
        db, n = DB(), 0
        with open(args.file, newline="", encoding="utf-8-sig") as f:
            for rec in csv.reader(f):
                if rec and rec[0].strip().startswith("http"):
                    rec += ["", ""]
                    n += db.add_url(rec[0], rec[1].strip(), rec[2].strip())
        print(f"Added {n} URL(s).")
    elif args.cmd == "list":
        for row in DB().all_urls():
            flag = "" if row["active"] else " (paused)"
            print(f'{row["id"]:>4}  {row["state"] or "--":<3} {row["last_status"] or "new":<14} '
                  f'{row["label"][:35]:<36} {row["url"]}{flag}')
    elif args.cmd == "remove":
        DB().delete_url(args.id)
        print("Removed.")
    elif args.cmd == "test-email":
        cfg = load_config()
        painter = Painter(build_keyword_regex(cfg["keywords"]), cfg["change_color"], cfg["keyword_color"])
        body = (f"<p>BidWatch test email. Keywords: {painter.kw(', '.join(cfg['keywords']))}</p>"
                f"<p>{painter.changed('This is how changed text looks; keywords inside it: ' + ' '.join(cfg['keywords']))}</p>")
        send_email(cfg, "BidWatch test email", body, high_priority=True)
        print("Test email sent.")
    elif args.cmd == "schedule":
        import scheduler
        print(scheduler.schedule_daily(args.time, job=args.job))
    elif args.cmd == "unschedule":
        import scheduler
        print(scheduler.remove_schedule(job=args.job))
    elif args.cmd == "discover":
        import discover
        states = None
        if args.states:
            states = [] if args.states.upper() == "ALL" else [x.strip().upper() for x in args.states.split(",")]
        types = [x.strip() for x in args.types.split(",")] if args.types else None
        res = discover.run_discovery(send=not args.no_email, max_sites=args.max_sites, types=types, states=states)
        print(f"Checked {res['scanned']} websites, {res['new']} new bid pages."
              + (f" CSV: {res['csv']}" if res["csv"] else ""))
    elif args.cmd == "login":
        import getpass
        db = DB()
        row = db.conn.execute("SELECT * FROM urls WHERE id=?", (args.id,)).fetchone()
        if not row:
            print("No URL with that ID (see: python bidwatch.py list).")
            return
        print(f"Sign-in for {row['label'] or row['url']}")
        login_url = input("Sign-in page address (Enter to detect automatically): ").strip()
        user = input("Username: ").strip()
        pw = getpass.getpass("Password: ")
        auth.save_credentials(db, args.id, user, pw, login_url=login_url)
        print("Saved. BidWatch will sign in automatically on each run.")
    elif args.cmd == "gui":
        import settings_gui
        settings_gui.main(prompt_logins=args.logins)


if __name__ == "__main__":
    main()
