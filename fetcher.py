"""Downloading pages/documents, turning them into text, and finding forwarding addresses."""
import hashlib
import io
import re
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

DOC_EXTENSIONS = (".pdf", ".docx", ".xlsx", ".txt", ".csv", ".rtf")
SOFT_404 = re.compile(r"(page (was )?not found|404 (error|not found)|no longer (available|exists)|"
                      r"page (you requested |you are looking for )?(could not|cannot|can't) be found|"
                      r"has (been )?(moved|relocated))", re.I)
MOVED_LINK_TEXT = re.compile(r"(moved|new (site|location|address|page|website)|relocated|redirect|"
                             r"click here|go to|visit (our|the) new|has changed)", re.I)


class FetchResult:
    def __init__(self):
        self.ok = False
        self.status = ""
        self.url = ""
        self.final_url = ""
        self.content = b""
        self.content_type = ""
        self.text = ""
        self.headers = {}
        self.links = []      # list of [text, absolute_url]
        self.soup = None
        self.error = ""


def make_session(cfg):
    s = requests.Session()
    s.headers.update({"User-Agent": cfg["user_agent"],
                      "Accept": "text/html,application/xhtml+xml,application/pdf,*/*;q=0.8",
                      "Accept-Language": "en-US,en;q=0.9"})
    return s


def fetch(session, url, cfg, selector="", max_mb=None, auth=None):
    r = FetchResult()
    r.url = url
    max_bytes = int((max_mb or cfg["max_document_mb"]) * 1024 * 1024)
    try:
        resp = session.get(url, timeout=cfg["request_timeout"], allow_redirects=True, stream=True,
                           auth=auth)
        r.status = str(resp.status_code)
        r.final_url = resp.url
        r.headers = dict(resp.headers)
        r.content_type = resp.headers.get("Content-Type", "").lower()
        chunks, size = [], 0
        for chunk in resp.iter_content(65536):
            size += len(chunk)
            if size > max_bytes:
                r.error = f"file larger than {max_mb or cfg['max_document_mb']} MB, skipped"
                break
            chunks.append(chunk)
        r.content = b"".join(chunks)
        resp.close()
        if resp.status_code >= 400:
            r.error = f"HTTP {resp.status_code}"
            parse_content(r, selector)  # still parse: error pages often hold a forwarding link
            return r
        if r.error:
            return r
        parse_content(r, selector)
        if is_soft_404(r):
            r.error = "page says it was not found or has moved"
            r.status = "soft-404"
            return r
        r.ok = True
    except requests.exceptions.SSLError as e:
        r.error = f"SSL error: {e.__class__.__name__}"
        r.status = "ssl-error"
    except requests.exceptions.ConnectionError:
        r.error = "could not connect (site down or address no longer exists)"
        r.status = "no-connection"
    except requests.exceptions.Timeout:
        r.error = "timed out"
        r.status = "timeout"
    except Exception as e:  # noqa: BLE001
        r.error = f"{e.__class__.__name__}: {e}"
        r.status = "error"
    return r


def is_soft_404(r):
    if r.soup is None:
        return False
    title = (r.soup.title.get_text(" ", strip=True) if r.soup.title else "")
    if SOFT_404.search(title):
        return True
    return len(r.text) < 1500 and bool(SOFT_404.search(r.text))


def sniff_kind(content, content_type, url):
    path = urlparse(url).path.lower()
    if content[:5] == b"%PDF-" or "pdf" in content_type or path.endswith(".pdf"):
        return "pdf"
    if content[:2] == b"PK":
        if path.endswith(".xlsx") or "spreadsheet" in content_type:
            return "xlsx"
        return "docx"
    if "html" in content_type or content.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        return "html"
    if path.endswith(".rtf") or content[:5] == b"{\\rtf":
        return "rtf"
    return "text"


def parse_content(r, selector=""):
    kind = sniff_kind(r.content, r.content_type, r.final_url or r.url)
    if kind == "html":
        soup = BeautifulSoup(r.content, "html.parser")
        r.soup = soup
        base = r.final_url or r.url
        r.links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith(("mailto:", "javascript:", "tel:", "#")):
                continue
            r.links.append([" ".join(a.get_text(" ", strip=True).split()), urljoin(base, href).split("#")[0]])
        area = soup
        if selector:
            picked = soup.select(selector)
            if picked:
                area = BeautifulSoup("".join(str(p) for p in picked), "html.parser")
        r.text = html_to_text(area)
    else:
        r.text = document_to_text(r.content, kind)


def html_to_text(soup):
    soup = BeautifulSoup(str(soup), "html.parser")
    for tag in soup(["script", "style", "noscript", "template", "svg", "head"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"]):
        block.insert_before("\n")
        block.insert_after("\n")
    for cell in soup.find_all(["td", "th"]):
        cell.insert_after(" | ")
    lines = [" ".join(line.split()) for line in soup.get_text().splitlines()]
    return "\n".join(line.strip(" |") for line in lines if line.strip(" |"))


def document_to_text(content, kind):
    try:
        if kind == "pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
        if kind == "docx":
            import docx
            d = docx.Document(io.BytesIO(content))
            parts = [p.text for p in d.paragraphs]
            for t in d.tables:
                for row in t.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            return "\n".join(parts)
        if kind == "xlsx":
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
            parts = []
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    vals = [str(v) for v in row if v is not None]
                    if vals:
                        parts.append(" | ".join(vals))
            return "\n".join(parts)
        if kind == "rtf":
            txt = content.decode("latin-1", errors="ignore")
            txt = re.sub(r"\\[a-z]+-?\d* ?|[{}]", "", txt)
            return txt
        return content.decode("utf-8", errors="ignore")
    except Exception as e:  # noqa: BLE001
        return f"[could not read document: {e.__class__.__name__}]"


def normalize_for_compare(text, ignore_patterns):
    for pat in ignore_patterns or []:
        try:
            text = re.sub(pat, "", text, flags=re.I)
        except re.error:
            pass
    return "\n".join(" ".join(l.split()) for l in text.splitlines() if l.strip())


def content_hash(text):
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def is_document_link(url):
    path = urlparse(url).path.lower()
    return path.endswith(DOC_EXTENSIONS)


# ------------------------------------------------------------------ forwarding search
def _same_page(a, b):
    def norm(u):
        p = urlparse(u)
        host = p.netloc.lower().removeprefix("www.")
        return host, p.path.rstrip("/").lower(), p.query
    return norm(a) == norm(b)


def _variants(url):
    p = urlparse(url)
    out = []
    scheme2 = "http" if p.scheme == "https" else "https"
    out.append(urlunparse(p._replace(scheme=scheme2)))
    host = p.netloc
    host2 = host[4:] if host.lower().startswith("www.") else "www." + host
    out.append(urlunparse(p._replace(netloc=host2)))
    out.append(urlunparse(p._replace(scheme="https", netloc=host2)))
    path2 = p.path[:-1] if p.path.endswith("/") and len(p.path) > 1 else p.path + "/"
    out.append(urlunparse(p._replace(path=path2)))
    if p.path != p.path.lower():
        out.append(urlunparse(p._replace(path=p.path.lower())))
    seen, uniq = {url}, []
    for v in out:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def _forward_hints(r):
    """Look inside a broken/old page for a pointer to the new address."""
    hints = []
    if r.soup is None:
        return hints
    base = r.final_url or r.url
    for meta in r.soup.find_all("meta"):
        if meta.get("http-equiv", "").lower() == "refresh":
            m = re.search(r"url\s*=\s*['\"]?([^'\";]+)", meta.get("content", ""), re.I)
            if m:
                hints.append((urljoin(base, m.group(1).strip()), "meta refresh"))
    for script in r.soup.find_all("script"):
        m = re.search(r"(?:window\.|document\.)?location(?:\.href)?\s*(?:=|\.replace\(|\.assign\()\s*['\"]([^'\"]+)",
                      script.string or "")
        if m:
            hints.append((urljoin(base, m.group(1)), "script redirect"))
    canon = r.soup.find("link", rel=lambda v: v and "canonical" in v)
    if canon and canon.get("href"):
        hints.append((urljoin(base, canon["href"]), "canonical link"))
    if len(r.links) <= 40:  # a short "we have moved" page
        for text, href in r.links:
            if MOVED_LINK_TEXT.search(text or ""):
                hints.append((href, f'link "{text[:40]}"'))
    return hints


PROCUREMENT_WORDS = re.compile(r"(bid|solicitation|procurement|purchasing|rfp|rfq|rfi|itb|contract opportunit|"
                               r"business opportunit|vendor)", re.I)


def find_forward(session, url, cfg, first_result=None):
    """
    Returns (new_url, method, suggestions).
    new_url is a confident forwarding address (safe to swap in automatically), or None.
    suggestions is a list of (url, reason) worth looking at when nothing confident was found.
    """
    checked = set()

    def works(u):
        if u in checked:
            return None
        checked.add(u)
        res = fetch(session, u, cfg)
        return res if res.ok else None

    # 1) pointers on the broken page itself
    if first_result is not None:
        for hint, how in _forward_hints(first_result):
            if _same_page(hint, url):
                continue
            res = works(hint)
            if res:
                return res.final_url, f"found {how} on the old page", []

    # 2) common address variations (http/https, www, trailing slash, lowercase)
    for v in _variants(url):
        res = works(v)
        if res:
            return res.final_url, "address variation works", []

    # 3) walk up the site looking for a procurement page (suggestions only)
    suggestions = []
    p = urlparse(url)
    parts = [x for x in p.path.split("/") if x]
    parents = []
    for i in range(len(parts) - 1, -1, -1):
        parents.append(urlunparse((p.scheme, p.netloc, "/" + "/".join(parts[:i]) + ("/" if i else ""), "", "", "")))
    for parent in parents[:4]:
        res = works(parent)
        if not res:
            continue
        hits = []
        for text, href in res.links:
            if text and PROCUREMENT_WORDS.search(text) and urlparse(href).netloc:
                hits.append((href, f'link "{text[:50]}" on {parent}'))
        if hits:
            suggestions.extend(hits[:5])
            break
        suggestions.append((parent, "this higher-level page still works"))
        break
    return None, "", suggestions
