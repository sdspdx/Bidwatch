"""Builds the HTML emails and sends them."""
import datetime as dt
import html
import os
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from config import CLOUD, REPORT_DIR


class Report:
    def __init__(self):
        self.page_changes = []      # dicts: label, url, state, diff_html, new_links, documents, keywords, first_time
        self.sam_matches = []       # dicts from samgov._shape plus where/snippets/is_update
        self.moves = []             # (label, old, new, method)
        self.bad_links = []         # dicts: label, url, state, error, since, suggestions
        self.auto_added = []        # (text, url)
        self.login_needed = []      # dicts: id, label, url, detail (no credentials saved yet / sign-in failed)
        self.warnings = []
        self.pages_checked = 0
        self.sam_scanned = 0

    @property
    def has_keywords(self):
        return any(c["keywords"] for c in self.page_changes) or bool(self.sam_matches)

    @property
    def has_news(self):
        return bool(self.page_changes or self.sam_matches or self.moves or self.auto_added or self.login_needed)


FONT = "font-family:Segoe UI,Arial,Helvetica,sans-serif;"


def _a(url, text=None):
    return f'<a href="{html.escape(url)}" style="color:#0645ad;">{html.escape(text or url)}</a>'


def _legend(cfg):
    return (f'<p style="font-size:13px;">'
            f'<span style="background-color:{cfg["change_color"]};padding:1px 6px;">Changed / new text</span> '
            f'&nbsp; <span style="background-color:{cfg["keyword_color"]};padding:1px 6px;font-weight:bold;">'
            f'Keyword</span></p>')


def build_daily_html(report, cfg, painter):
    date = dt.date.today().strftime("%A, %B %d, %Y")
    scope = "Nationwide" if not cfg["states"] else "States: " + ", ".join(cfg["states"])
    h = [f'<div style="{FONT}font-size:14px;color:#222;max-width:900px;">',
         f'<h2 style="margin-bottom:2px;">BidWatch report — {date}</h2>',
         f'<div style="color:#666;font-size:13px;">{html.escape(scope)} · Keywords: '
         f'{painter.kw(", ".join(cfg["keywords"])) or "(none set)"}</div>',
         _legend(cfg)]

    kw_pages = sum(1 for c in report.page_changes if c["keywords"])
    h.append(f'<p><b>{len(report.sam_matches)}</b> federal solicitation(s) matched keywords · '
             f'<b>{len(report.page_changes)}</b> watched page(s) changed ({kw_pages} with keywords) · '
             f'{report.pages_checked} pages checked · {report.sam_scanned} SAM.gov notices scanned</p>')

    if report.warnings:
        h.append('<div style="background:#fff4e5;border-left:4px solid #f0a020;padding:8px 12px;margin:10px 0;">'
                 + "<br>".join(html.escape(w) for w in report.warnings) + "</div>")

    # ---- SAM.gov
    if report.sam_matches:
        h.append('<h3 style="border-bottom:2px solid #333;padding-bottom:3px;">New federal solicitations '
                 'matching your keywords (SAM.gov)</h3>')
        for s in sorted(report.sam_matches, key=lambda x: x["deadline"] or "9999"):
            title = painter.changed(s["title"]) if s.get("is_update") else painter.kw(s["title"])
            badge = (' <span style="font-size:11px;background:#333;color:#fff;padding:1px 5px;'
                     'border-radius:3px;">UPDATE / AMENDMENT</span>') if s.get("is_update") else ""
            where = "; ".join(f'{painter.kw(k)} ({", ".join(html.escape(p) for p in v)})'
                              for k, v in s["where"].items())
            h.append('<div style="border:1px solid #ddd;border-radius:6px;padding:10px 12px;margin:10px 0;">')
            h.append(f'<div style="font-size:15px;font-weight:bold;">{title}{badge}</div>')
            h.append(f'<div style="margin:3px 0;">{_a(s["link"], "Open on SAM.gov")}</div>')
            meta = [("Agency", s["agency"]), ("Type", s["type"]), ("Solicitation #", s["number"]),
                    ("Posted", s["posted"]), ("Response due", s["deadline"]),
                    ("Place of performance", s["state"]), ("Set-aside", s["set_aside"]), ("NAICS", s["naics"])]
            h.append('<div style="font-size:13px;color:#444;">' +
                     " · ".join(f"<b>{k}:</b> {html.escape(str(v))}" for k, v in meta if v) + "</div>")
            h.append(f'<div style="font-size:13px;margin-top:4px;"><b>Found:</b> {where}</div>')
            for place, text in s["snippets"][:4]:
                for snip in painter.snippets(text, max_snips=2):
                    h.append(f'<div style="font-size:13px;margin:4px 0 0 12px;color:#333;">'
                             f'<i>{html.escape(place)}:</i> {snip}</div>')
            h.append("</div>")

    # ---- watched pages
    if report.page_changes:
        h.append('<h3 style="border-bottom:2px solid #333;padding-bottom:3px;">Watched pages that changed</h3>')
        ordered = sorted(report.page_changes, key=lambda c: (not c["keywords"], c["label"].lower()))
        for c in ordered:
            border = cfg["keyword_color"] if c["keywords"] else "#ddd"
            h.append(f'<div style="border:2px solid {border};border-radius:6px;padding:10px 12px;margin:10px 0;">')
            star = "★ " if c["keywords"] else ""
            h.append(f'<div style="font-size:15px;font-weight:bold;">{star}{html.escape(c["label"] or c["url"])}'
                     f'{" (" + html.escape(c["state"]) + ")" if c["state"] else ""}</div>')
            h.append(f'<div style="font-size:13px;">{_a(c["url"])}</div>')
            if c["keywords"]:
                h.append(f'<div style="margin:4px 0;"><b>Keywords:</b> '
                         f'{painter.kw(", ".join(c["keywords"]))}</div>')
            if c.get("first_time"):
                h.append('<div style="font-size:13px;color:#666;">First check of this page — '
                         'saved as the starting point for tomorrow\'s comparison.</div>')
                for snip in painter.snippets(c.get("page_text", ""), max_snips=3):
                    h.append(f'<div style="font-size:13px;margin:4px 0 0 12px;">{snip}</div>')
            if c["new_links"]:
                h.append('<div style="margin-top:6px;"><b>New links on the page:</b></div><ul style="margin:2px 0;">')
                for text, url in c["new_links"][:40]:
                    h.append(f'<li style="font-size:13px;">{painter.changed(text or url)} — '
                             f'{_a(url, "open")}</li>')
                h.append("</ul>")
            if c["documents"]:
                h.append('<div style="margin-top:6px;"><b>New documents with keywords:</b></div>')
                for d in c["documents"]:
                    h.append(f'<div style="font-size:13px;margin:4px 0 0 12px;">{_a(d["url"], d["name"])} — '
                             f'{painter.kw(", ".join(d["keywords"]))}</div>')
                    for snip in d["snippets"]:
                        h.append(f'<div style="font-size:13px;margin:2px 0 0 24px;">{snip}</div>')
            if c.get("diff_html"):
                h.append('<div style="margin-top:6px;"><b>What changed:</b></div>'
                         '<div style="font-family:Consolas,monospace;font-size:12px;background:#fafafa;'
                         f'border:1px solid #eee;padding:6px;">{c["diff_html"]}</div>')
            h.append("</div>")

    if report.login_needed:
        h.append('<h3 style="border-bottom:2px solid #333;padding-bottom:3px;">Sign-in needed</h3>'
                 '<p style="font-size:13px;">' + (
                     'Add the username and password for these sites to the <b>SITE_LOGINS</b> secret in your '
                     'GitHub repository (see the setup guide); BidWatch will then sign in automatically every day.'
                     if CLOUD else 'Open BidWatch on your PC — it will ask for the username and '
                     'password for these sites, then sign in automatically every day.') + '</p><ul>')
        for x in report.login_needed:
            h.append(f'<li style="font-size:13px;"><b>{html.escape(x["label"] or "")}</b> {_a(x["url"])}'
                     f'<br><span style="color:#b00;">{html.escape(x["detail"])}</span></li>')
        h.append("</ul>")

    if report.auto_added:
        h.append('<h3>New URLs added to your master list</h3><ul>')
        for text, url in report.auto_added:
            h.append(f'<li style="font-size:13px;">{painter.kw(text)} — {_a(url)}</li>')
        h.append("</ul>")

    if report.moves:
        h.append('<h3>Watched URLs that moved (updated automatically)</h3><ul>')
        for label, old, new, how in report.moves:
            h.append(f'<li style="font-size:13px;"><b>{html.escape(label or "")}</b><br>Old: {html.escape(old)}'
                     f'<br>New: {_a(new)}<br><span style="color:#666;">{html.escape(how)}</span></li>')
        h.append("</ul>")

    if not report.has_news:
        h.append("<p>No changes or keyword matches today.</p>")
    h.append('<p style="color:#999;font-size:11px;margin-top:20px;">Sent by BidWatch.</p></div>')
    return "".join(h)


def build_bad_links_html(bad, cfg):
    h = [f'<div style="{FONT}font-size:14px;color:#222;max-width:900px;">',
         '<h2>BidWatch: new URL needed</h2>',
         '<p>These watched links no longer work and no forwarding address could be found. '
         'Please find the new address and update it in ' +
         ('<b>urls.csv</b> in your GitHub repository.' if CLOUD else 'BidWatch (Watched URLs tab).') + '</p>']
    for b in bad:
        h.append('<div style="border:2px solid #e33;border-radius:6px;padding:10px 12px;margin:10px 0;">')
        h.append(f'<div style="font-weight:bold;">{html.escape(b["label"] or b["url"])}'
                 f'{" (" + html.escape(b["state"]) + ")" if b["state"] else ""}</div>')
        h.append(f'<div style="font-size:13px;">{html.escape(b["url"])}</div>')
        h.append(f'<div style="font-size:13px;color:#b00;">Problem: {html.escape(b["error"])}'
                 f' · broken since {html.escape(b["since"] or "today")}</div>')
        if b["suggestions"]:
            h.append('<div style="font-size:13px;margin-top:4px;"><b>Possible new locations:</b></div><ul>')
            for url, why in b["suggestions"]:
                h.append(f'<li style="font-size:13px;">{_a(url)} <span style="color:#666;">— '
                         f'{html.escape(why)}</span></li>')
            h.append("</ul>")
        h.append("</div>")
    h.append("</div>")
    return "".join(h)


def save_html(name, body):
    os.makedirs(REPORT_DIR, exist_ok=True)
    path = os.path.join(REPORT_DIR, f"{dt.datetime.now():%Y-%m-%d_%H%M%S}_{name}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<html><head><meta charset='utf-8'></head><body>{body}</body></html>")
    return path


def send_email(cfg, subject, html_body, high_priority=False, attachments=None):
    ec = cfg["email"]
    to = [t for t in ec.get("to_addresses", []) if t.strip()]
    if not (ec.get("smtp_server") and to):
        raise RuntimeError("Email is not set up (SMTP server and To address are required).")
    sender = ec.get("from_address") or ec.get("username")
    outer = MIMEMultipart("mixed")
    msg = MIMEMultipart("alternative")
    outer["Subject"] = subject
    outer["From"] = sender
    outer["To"] = ", ".join(to)
    outer["Date"] = formatdate(localtime=True)
    outer["Message-ID"] = make_msgid(domain="bidwatch.local")
    if high_priority:
        outer["X-Priority"] = "1 (Highest)"
        outer["X-MSMail-Priority"] = "High"
        outer["Importance"] = "High"
        outer["Priority"] = "urgent"
    msg.attach(MIMEText("This message is best viewed in an email program that shows HTML.", "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    outer.attach(msg)
    for path in attachments or []:
        with open(path, "rb") as f:
            part = MIMEApplication(f.read(), Name=os.path.basename(path))
        part["Content-Disposition"] = f'attachment; filename="{os.path.basename(path)}"'
        outer.attach(part)
    port = int(ec.get("smtp_port", 587))
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(ec["smtp_server"], port, timeout=60, context=context)
    else:
        server = smtplib.SMTP(ec["smtp_server"], port, timeout=60)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    try:
        if ec.get("username"):
            server.login(ec["username"], ec.get("password", ""))
        server.sendmail(sender, to, outer.as_string())
    finally:
        server.quit()
