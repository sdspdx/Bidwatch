"""Settings handling for BidWatch. Settings live in config.json next to this file."""
import copy
import json
import os
import re

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
DB_PATH = os.path.join(APP_DIR, "bidwatch.db")
REPORT_DIR = os.path.join(APP_DIR, "reports")
LOG_DIR = os.path.join(APP_DIR, "logs")
URLS_CSV = os.path.join(APP_DIR, "urls.csv")

# Set by the GitHub Actions workflow. In the cloud, secrets come from environment variables,
# the watch list lives in urls.csv, and website sign-ins come from the SITE_LOGINS secret.
CLOUD = os.environ.get("BIDWATCH_CLOUD", "").strip() not in ("", "0", "false")

# environment variable -> setting it fills in (blank variables are ignored)
ENV_SETTINGS = {
    "SMTP_SERVER": ("email", "smtp_server"),
    "SMTP_PORT": ("email", "smtp_port"),
    "SMTP_USERNAME": ("email", "username"),
    "SMTP_PASSWORD": ("email", "password"),
    "EMAIL_FROM": ("email", "from_address"),
    "EMAIL_TO": ("email", "to_addresses"),
    "SAM_API_KEY": ("sam_gov", "api_key"),
    "BRAVE_API_KEY": ("discovery", "brave_api_key"),
}

US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY", "PR", "GU", "VI", "AS", "MP",
]

DEFAULTS = {
    # Words or phrases to look for (case-insensitive, whole-word).
    "keywords": [],
    # Empty list = nationwide. Otherwise two-letter codes, e.g. ["WA", "OR"].
    "states": [],

    # Highlight colors used in the email.
    "change_color": "#FFFF00",    # fluorescent yellow = what changed
    "keyword_color": "#15F4EE",   # fluorescent blue   = keyword hits

    # Watched web pages
    "check_documents": True,          # open PDF/Word/Excel/text files linked from watched pages
    "max_documents_per_page": 15,     # new documents opened per page per run
    "max_document_mb": 25,
    "auto_add_keyword_links": True,   # add newly found links whose text has a keyword to the master list
    "max_auto_add_per_run": 25,
    "ignore_patterns": [],            # regexes of text to ignore when comparing pages (e.g. visitor counters)
    "request_timeout": 30,
    "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 BidWatch/1.0",
    "bad_link_reminder_days": 7,           # re-send the "new URL needed" notice every N days while still broken
    "prompt_for_logins_on_desktop": True,  # pop up the sign-in window after a run if a site needs a password

    # Federal solicitations from SAM.gov (free API key from sam.gov > Account Details)
    "sam_gov": {
        "enabled": True,
        "api_key": "",
        "days_back": 1,
        # Personal keys get roughly 10 calls/day; system-account keys about 1,000.
        "max_api_calls_per_day": 10,
        "fetch_descriptions": True,     # each description costs 1 API call
        "scan_attachments": True,       # attachment downloads do not use API calls
        "max_attachments_per_run": 300,
        "exclude_types": ["Award Notice", "Justification", "Sale of Surplus Property"],
    },

    # Discovery agent: finds bids/procurement pages and emails them as a CSV
    "discovery": {
        "types": ["city", "county", "port", "utility", "engineering_firm"],
        "use_bidwatch_states": True,      # same states as the Keywords & area tab (none = nationwide)
        "max_sites_per_run": 500,         # websites checked per run; it continues where it left off
        "rescan_days": 90,                # re-check a website after this many days
        "workers": 8,                     # websites checked at the same time
        "delay_seconds": 0.5,             # pause between pages on the same website
        "max_requests_per_site": 14,
        "timeout": 20,
        "use_dotgov": True,               # official registry of all .gov websites
        "use_wikidata": True,             # cities/counties/ports with non-.gov websites
        "seed_csv": "",                   # your own list: name,website,state,type
        "brave_api_key": "",              # optional Brave Search API key
        "brave_max_queries_per_run": 50,
        "auto_add_to_bidwatch": False,    # also add finds straight to the watch list
    },

    # Email (Gmail: smtp.gmail.com, port 587, and a Google "App Password")
    "email": {
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "",
        "password": "",
        "from_address": "",
        "to_addresses": [],
        "send_when_nothing_found": False,
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path=CONFIG_PATH):
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    cfg = _merge(DEFAULTS, data)
    for var, (section, key) in ENV_SETTINGS.items():
        val = os.environ.get(var, "").strip()
        if not val:
            continue
        if key == "to_addresses":
            val = [x.strip() for x in re.split(r"[,;\s]+", val) if x.strip()]
        elif key == "smtp_port":
            val = int(val)
        cfg[section][key] = val
    cfg["states"] = [s.strip().upper() for s in cfg.get("states", []) if s.strip()]
    cfg["keywords"] = [k.strip() for k in cfg.get("keywords", []) if k.strip()]
    return cfg


def save_config(cfg, path=CONFIG_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
