"""Master database (SQLite) for watched URLs, documents, solicitations and history."""
import datetime as dt
import json
import sqlite3

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE NOT NULL,
    label TEXT DEFAULT '',
    state TEXT DEFAULT '',          -- two-letter code, blank = national
    selector TEXT DEFAULT '',       -- optional CSS selector: only watch this part of the page
    source TEXT DEFAULT 'manual',   -- manual | discovered
    active INTEGER DEFAULT 1,
    added_on TEXT,
    last_checked TEXT,
    last_status TEXT,
    content_hash TEXT,
    content_text TEXT,
    links_json TEXT,
    bad_since TEXT,
    bad_notified_on TEXT
);
CREATE TABLE IF NOT EXISTS url_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id INTEGER, checked_on TEXT, status TEXT, content_hash TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS url_moves (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url_id INTEGER, old_url TEXT, new_url TEXT, moved_on TEXT, method TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    url TEXT PRIMARY KEY, parent_url_id INTEGER, first_seen TEXT, keywords_found TEXT
);
CREATE TABLE IF NOT EXISTS solicitations (
    notice_id TEXT PRIMARY KEY,
    solicitation_number TEXT, title TEXT, agency TEXT, type TEXT,
    posted TEXT, deadline TEXT, state TEXT, link TEXT,
    matched_keywords TEXT, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS api_usage (
    day TEXT, service TEXT, calls INTEGER DEFAULT 0, PRIMARY KEY (day, service)
);
"""


def now():
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return dt.date.today().isoformat()


class DB:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()

    # ---------- watched URLs ----------
    def add_url(self, url, label="", state="", selector="", source="manual"):
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO urls (url, label, state, selector, source, added_on) VALUES (?,?,?,?,?,?)",
            (url.strip(), label, state.upper().strip(), selector, source, now()))
        self.conn.commit()
        return cur.rowcount > 0

    def update_url_fields(self, url_id, **fields):
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE urls SET {cols} WHERE id=?", (*fields.values(), url_id))
        self.conn.commit()

    def delete_url(self, url_id):
        self.conn.execute("DELETE FROM urls WHERE id=?", (url_id,))
        self.conn.commit()

    def url_exists(self, url):
        return self.conn.execute("SELECT 1 FROM urls WHERE url=?", (url,)).fetchone() is not None

    def all_urls(self):
        return self.conn.execute("SELECT * FROM urls ORDER BY state, label, url").fetchall()

    def urls_to_check(self, states):
        rows = self.conn.execute("SELECT * FROM urls WHERE active=1 ORDER BY id").fetchall()
        if not states:
            return rows
        return [r for r in rows if not r["state"] or r["state"] in states]

    def save_check(self, url_id, status, content_hash, text, links, note=""):
        self.conn.execute(
            "UPDATE urls SET last_checked=?, last_status=?, content_hash=?, content_text=?, links_json=?, "
            "bad_since=NULL, bad_notified_on=NULL WHERE id=?",
            (now(), status, content_hash, text, json.dumps(links), url_id))
        self.conn.execute(
            "INSERT INTO url_history (url_id, checked_on, status, content_hash, note) VALUES (?,?,?,?,?)",
            (url_id, now(), status, content_hash, note))
        self.conn.commit()

    def mark_bad(self, url_id, status):
        row = self.conn.execute("SELECT bad_since FROM urls WHERE id=?", (url_id,)).fetchone()
        bad_since = row["bad_since"] if row and row["bad_since"] else now()
        self.conn.execute("UPDATE urls SET last_checked=?, last_status=?, bad_since=? WHERE id=?",
                          (now(), status, bad_since, url_id))
        self.conn.execute("INSERT INTO url_history (url_id, checked_on, status, note) VALUES (?,?,?,?)",
                          (url_id, now(), status, "bad link"))
        self.conn.commit()

    def mark_bad_notified(self, url_id):
        self.conn.execute("UPDATE urls SET bad_notified_on=? WHERE id=?", (today(), url_id))
        self.conn.commit()

    def move_url(self, url_id, old_url, new_url, method):
        """Replace a URL with its forwarding address, keeping the history."""
        clash = self.conn.execute("SELECT id FROM urls WHERE url=? AND id<>?", (new_url, url_id)).fetchone()
        if clash:  # new address is already watched separately; retire the old entry
            self.conn.execute("UPDATE urls SET active=0, last_status=? WHERE id=?",
                              (f"moved to {new_url} (already watched)", url_id))
        else:
            self.conn.execute("UPDATE urls SET url=?, bad_since=NULL, bad_notified_on=NULL WHERE id=?",
                              (new_url, url_id))
        self.conn.execute("INSERT INTO url_moves (url_id, old_url, new_url, moved_on, method) VALUES (?,?,?,?,?)",
                          (url_id, old_url, new_url, now(), method))
        self.conn.commit()

    # ---------- documents ----------
    def document_seen(self, url):
        return self.conn.execute("SELECT 1 FROM documents WHERE url=?", (url,)).fetchone() is not None

    def save_document(self, url, parent_id, keywords):
        self.conn.execute("INSERT OR REPLACE INTO documents (url, parent_url_id, first_seen, keywords_found) "
                          "VALUES (?,?,?,?)", (url, parent_id, now(), ", ".join(keywords)))
        self.conn.commit()

    # ---------- solicitations ----------
    def solicitation_seen(self, notice_id):
        return self.conn.execute("SELECT 1 FROM solicitations WHERE notice_id=?", (notice_id,)).fetchone() is not None

    def previous_match_for_number(self, sol_number):
        if not sol_number:
            return None
        return self.conn.execute(
            "SELECT * FROM solicitations WHERE solicitation_number=? AND matched_keywords<>'' "
            "ORDER BY first_seen DESC LIMIT 1", (sol_number,)).fetchone()

    def save_solicitation(self, s, keywords):
        self.conn.execute(
            "INSERT OR REPLACE INTO solicitations (notice_id, solicitation_number, title, agency, type, posted, "
            "deadline, state, link, matched_keywords, first_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (s["notice_id"], s["number"], s["title"], s["agency"], s["type"], s["posted"],
             s["deadline"], s["state"], s["link"], ", ".join(keywords), now()))
        self.conn.commit()

    def matched_solicitations(self, limit=500):
        return self.conn.execute("SELECT * FROM solicitations WHERE matched_keywords<>'' "
                                 "ORDER BY first_seen DESC LIMIT ?", (limit,)).fetchall()

    # ---------- API budget ----------
    def api_calls_today(self, service):
        row = self.conn.execute("SELECT calls FROM api_usage WHERE day=? AND service=?",
                                (today(), service)).fetchone()
        return row["calls"] if row else 0

    def count_api_call(self, service):
        self.conn.execute("INSERT INTO api_usage (day, service, calls) VALUES (?,?,1) "
                          "ON CONFLICT(day, service) DO UPDATE SET calls = calls + 1", (today(), service))
        self.conn.commit()
