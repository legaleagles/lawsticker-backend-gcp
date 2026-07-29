"""
LawSticker AI Backend — Google Cloud Run (Flask)
Batch 1 of 2: wall-of-fame, update-gold-rate, pulse, site-activity-digest,
site-watchers. Ask Durga Bro / Scam-Ed / Scam-Moderate follow in Batch 2,
once this batch is confirmed working live.

Required environment variables (set these in Cloud Run's
"Variables & Secrets" tab when you deploy — see the manual):
  SITE_REPO_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, NEWSDATA_API_KEY
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import os
import re
import base64
import hashlib
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

app = Flask(__name__)
CORS(app, origins=["https://lawsticker-ai.com"])

REPO = "legaleagles/LabourLaw2"
GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# Shared helpers — identical to what every function on Vercel already used,
# just kept once here instead of duplicated across files.
# ---------------------------------------------------------------------------

def github_get(path, token):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            content = base64.b64decode(data["content"]).decode()
            return json.loads(content), data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def github_put(path, token, content_obj, sha, message):
    body = json.dumps(content_obj, indent=2, ensure_ascii=False).encode()
    payload = {"message": message, "content": base64.b64encode(body).decode(), "branch": "main"}
    if sha:
        payload["sha"] = sha
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req) as resp:
        return resp.status


def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status


def send_telegram_to_all(bot_token, chat_id_config, text):
    results = {}
    for cid in [c.strip() for c in chat_id_config.split(",") if c.strip()]:
        try:
            send_telegram(bot_token, cid, text)
            results[cid] = "sent"
        except Exception as e:
            results[cid] = f"failed: {e}"
    return results


# ---------------------------------------------------------------------------
# 1. Wall of Fame
# ---------------------------------------------------------------------------

WALL_FILE = "wall-of-fame.json"
FLAGGED_FILE = "wall-of-fame-flagged.json"

VALID_STORIES = [
    "The Starch Test for Paneer, Khoya & Milk", "Honey Authenticity Investigation",
    "The Fruit Ripening Inspection", "The Silver vs Aluminium Test",
    "Mustard Oil Safety Awareness", "Reading the Bold Nutrition Label",
    "Hidden Sugar Detection", "Ice Cream vs Frozen Dessert Labelling",
    "Decoding Advertising Tricks",
]
BLOCKED_PATTERNS = [
    r"\bfuck\b", r"\bshit\b", r"\bbitch\b", r"\bass+hole\b", r"\bcunt\b",
    r"\bnigg\w*", r"\bslut\b", r"\bwhore\b", r"\bretard\b", r"\bpussy\b",
    r"\brape\b", r"admin", r"<script", r"http[s]?://", r"\bnull\b", r"\btest\b",
]
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z\s.\-']{0,19}$")


def is_clean(text):
    lowered = text.lower()
    return not any(re.search(p, lowered) for p in BLOCKED_PATTERNS)


@app.route('/api/wall-of-fame', methods=['POST'])
def wall_of_fame():
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "Invalid request body."}), 400

    first_name = str(data.get("first_name", "")).strip()
    last_initial = str(data.get("last_initial", "")).strip().rstrip(".")
    story = str(data.get("story", "")).strip()
    consent = bool(data.get("consent", False))

    if not consent:
        return jsonify({"ok": False, "error": "Parent/guardian consent is required."}), 400
    if not NAME_RE.match(first_name) or len(first_name) < 1:
        return jsonify({"ok": False, "error": "Please enter a valid first name."}), 400
    if not last_initial or not last_initial.isalpha() or len(last_initial) > 1:
        return jsonify({"ok": False, "error": "Last initial should be a single letter."}), 400
    if story not in VALID_STORIES:
        return jsonify({"ok": False, "error": "Unrecognised story selection."}), 400

    display_name = f"{first_name.strip().title()} {last_initial.upper()}."
    entry = {"name": display_name, "story": story, "date": datetime.now(timezone.utc).strftime("%d %B %Y")}

    token = os.environ.get("SITE_REPO_TOKEN")
    if not token:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        if not is_clean(first_name) or not is_clean(story):
            flagged, sha = github_get(FLAGGED_FILE, token)
            if flagged is None:
                flagged = {"entries": []}
            flagged["entries"].append(entry)
            github_put(FLAGGED_FILE, token, flagged, sha, "Flagged Wall of Fame submission (auto-filter)")
            return jsonify({"ok": True})

        wall, sha = github_get(WALL_FILE, token)
        if wall is None:
            wall = {"entries": []}
        wall["entries"] = [e for e in wall["entries"] if not (e.get("name") == entry["name"] and e.get("story") == entry["story"])]
        wall["entries"].insert(0, entry)
        github_put(WALL_FILE, token, wall, sha, f"Wall of Fame: add/update {display_name}")
        return jsonify({"ok": True})
    except Exception:
        return jsonify({"ok": False, "error": "Could not save submission. Please try again shortly."}), 500


# ---------------------------------------------------------------------------
# 2. Gold/Silver Rate Update (cron-triggered, GET)
# ---------------------------------------------------------------------------

CONFIG_FILE = "site-config.json"
GRAMS_PER_TROY_OZ = 31.1034768


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "lawsticker-ai-cron/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def compute_rate(usd_per_oz, usd_to_inr, premium_pct):
    spot_inr_per_gram = (usd_per_oz / GRAMS_PER_TROY_OZ) * usd_to_inr
    return round(spot_inr_per_gram * (1 + premium_pct / 100))


@app.route('/api/update-gold-rate', methods=['GET'])
def update_gold_rate():
    token = os.environ.get("SITE_REPO_TOKEN")
    if not token:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500
    try:
        gold = fetch_json("https://api.gold-api.com/price/XAU")
        silver = fetch_json("https://api.gold-api.com/price/XAG")
        fx = fetch_json("https://open.er-api.com/v6/latest/USD")
        usd_to_inr = fx["rates"]["INR"]

        config, sha = github_get(CONFIG_FILE, token)
        rates = config.setdefault("rates", {})
        gold_premium = rates.get("gold_india_premium_pct", 15)
        silver_premium = rates.get("silver_india_premium_pct", 32)

        new_gold = compute_rate(gold["price"], usd_to_inr, gold_premium)
        new_silver = compute_rate(silver["price"], usd_to_inr, silver_premium)

        rates["gold_24k_per_gram_inr"] = new_gold
        rates["silver_999_per_gram_inr"] = new_silver
        rates["updated_at"] = datetime.now(timezone.utc).isoformat()
        rates["updated_by"] = "auto-cron-daily"

        github_put(CONFIG_FILE, token, config, sha, "Daily automated gold/silver rate update")
        return jsonify({"ok": True, "gold_24k_per_gram_inr": new_gold, "silver_999_per_gram_inr": new_silver})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# 3. Community Pulse counter
# ---------------------------------------------------------------------------

PULSE_FILE = "pulse-counts.json"
VALID_PULSE_TYPES = {"badge", "trick", "visit"}


@app.route('/api/pulse', methods=['GET'])
def pulse_get():
    token = os.environ.get("SITE_REPO_TOKEN")
    if not token:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500
    try:
        counts, _ = github_get(PULSE_FILE, token)
        if counts is None:
            counts = {"badges_total": 0, "tricks_total": 0, "visits_total": 0}
        return jsonify({"ok": True, "badges_total": counts.get("badges_total", 0),
                         "tricks_total": counts.get("tricks_total", 0), "visits_total": counts.get("visits_total", 0)})
    except Exception:
        return jsonify({"ok": False, "error": "Could not read counts."}), 500


@app.route('/api/pulse', methods=['POST'])
def pulse_post():
    token = os.environ.get("SITE_REPO_TOKEN")
    if not token:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500
    try:
        data = request.get_json(force=True, silent=True) or {}
    except Exception:
        return jsonify({"ok": False, "error": "Invalid request body."}), 400

    event_type = str(data.get("type", "")).strip()
    if event_type not in VALID_PULSE_TYPES:
        return jsonify({"ok": False, "error": "Unrecognised event type."}), 400

    try:
        counts, sha = github_get(PULSE_FILE, token)
        if counts is None:
            counts = {"badges_total": 0, "tricks_total": 0, "visits_total": 0}
        key = {"badge": "badges_total", "trick": "tricks_total", "visit": "visits_total"}[event_type]
        counts[key] = counts.get(key, 0) + 1
        github_put(PULSE_FILE, token, counts, sha, f"Pulse: +1 {event_type}")
        return jsonify({"ok": True, "badges_total": counts.get("badges_total", 0),
                         "tricks_total": counts.get("tricks_total", 0), "visits_total": counts.get("visits_total", 0)})
    except Exception:
        # A failed ping must never block the user's actual action — the
        # counter is a nice-to-have, not a critical path.
        return jsonify({"ok": False})


# ---------------------------------------------------------------------------
# 4. Site Activity Digest (cron-triggered, GET) — visit tracking + news fetch
# ---------------------------------------------------------------------------

VISIT_STATE_FILE = "visit-digest-state.json"
NEWS_FILE = "news-feed.json"
NEWSDATA_BASE = "https://newsdata.io/api/1/latest"
NEWS_QUERIES = {
    "legal": {"q": "court OR judgment OR verdict OR legislation OR tribunal", "country": "in", "language": "en"},
    "regional": {"q": "Hyderabad OR Telangana", "country": "in", "language": "en"},
    "national": {"country": "in", "language": "en"},
    "international": {"language": "en", "excludecountry": "in"},
}


def fetch_news(api_key, params):
    query = dict(params)
    query["apikey"] = api_key
    url = f"{NEWSDATA_BASE}?{urllib.parse.urlencode(query)}"
    with urllib.request.urlopen(urllib.request.Request(url), timeout=15) as resp:
        return json.loads(resp.read().decode())


def extract_articles(api_response, limit=8):
    return [{"title": r.get("title", ""), "link": r.get("link", ""), "source": r.get("source_id", ""),
             "pubDate": r.get("pubDate", ""), "description": (r.get("description") or "")[:200]}
            for r in api_response.get("results", [])[:limit]]


def run_visit_digest(site_token, bot_token, chat_id):
    try:
        pulse, _ = github_get(PULSE_FILE, site_token)
        current_visits = (pulse or {}).get("visits_total", 0)
        current_badges = (pulse or {}).get("badges_total", 0)
        current_tricks = (pulse or {}).get("tricks_total", 0)

        state, sha = github_get(VISIT_STATE_FILE, site_token)
        is_first_run = state is None
        last_visits = state.get("last_visits_total", 0) if state else 0
        new_visits = current_visits - last_visits

        telegram_sent = None
        if new_visits > 0 or is_first_run:
            if is_first_run:
                message = f"👀 Now tracking site visits.\n\nCurrent totals — Visits: {current_visits} · Badges: {current_badges} · Tricks spotted: {current_tricks}"
            else:
                now = datetime.now(timezone.utc).strftime("%d %b, %H:%M UTC")
                message = f"👀 <b>{new_visits} new visit{'s' if new_visits != 1 else ''}</b> since last check\n({now})\n\nSite totals — Visits: {current_visits} · Badges: {current_badges} · Tricks spotted: {current_tricks}"
            results = send_telegram_to_all(bot_token, chat_id, message)
            telegram_sent = all(v == "sent" for v in results.values())

        new_state = {"last_visits_total": current_visits, "last_checked_at": datetime.now(timezone.utc).isoformat()}
        github_put(VISIT_STATE_FILE, site_token, new_state, sha, "Visit digest check")
        return {"ok": True, "new_visits": new_visits, "telegram_sent": telegram_sent, "current_visits": current_visits}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_news_digest(site_token, newsdata_key):
    try:
        existing, sha = github_get(NEWS_FILE, site_token)
        existing = existing or {}
        previous_categories = existing.get("categories", {})
        feed, errors, stale = {}, {}, []
        for category, params in NEWS_QUERIES.items():
            try:
                raw = fetch_news(newsdata_key, params)
                if raw.get("status") == "success":
                    articles = extract_articles(raw)
                    if articles:
                        feed[category] = articles
                    else:
                        feed[category] = previous_categories.get(category, [])
                        stale.append(category)
                else:
                    errors[category] = raw.get("results", {}).get("message", "Unknown API error")
                    feed[category] = previous_categories.get(category, [])
                    stale.append(category)
            except Exception as e:
                errors[category] = str(e)
                feed[category] = previous_categories.get(category, [])
                stale.append(category)

        output = dict(existing)
        if len(stale) < len(NEWS_QUERIES):
            output["updated_at"] = datetime.now(timezone.utc).isoformat()
        output["categories"] = feed
        github_put(NEWS_FILE, site_token, output, sha, "News digest update")
        total = sum(len(v) for v in feed.values())
        return {"ok": True, "total_articles": total, "counts": {k: len(v) for k, v in feed.items()}, "stale_categories": stale or None, "errors": errors or None}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.route('/api/site-activity-digest', methods=['GET'])
def site_activity_digest():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    newsdata_key = os.environ.get("NEWSDATA_API_KEY")

    if not site_token:
        return jsonify({"ok": False, "error": "Server misconfiguration — missing SITE_REPO_TOKEN."}), 500

    visit_result = run_visit_digest(site_token, bot_token, chat_id) if (bot_token and chat_id) else {"ok": False, "error": "missing telegram env vars"}
    news_result = run_news_digest(site_token, newsdata_key) if newsdata_key else {"ok": False, "error": "missing NEWSDATA_API_KEY"}
    return jsonify({"ok": True, "visit_digest": visit_result, "news_digest": news_result})


# ---------------------------------------------------------------------------
# 5. Site Watchers (cron-triggered, GET) — PLC + LAWCET news change detection
# ---------------------------------------------------------------------------

MAX_TELEGRAM_LEN = 3800
PLC_STATE_FILE = "plc-watch-state.json"
PLC_URL = "https://plchyd.ac.in/"
LAWCET_STATE_FILE = "lawcet-news-watch-state.json"
LAWCET_URL = "https://law.careers360.com/articles/ts-lawcet-2026"
LAWCET_SECTION_MARKERS = ["TS LAWCET 2026 Latest Update", "Latest Update"]


def html_to_text(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;|&amp;|&quot;|&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_page_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerWatch/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return html_to_text(resp.read().decode("utf-8", errors="replace"))


def extract_latest_update_section(full_text):
    for marker in LAWCET_SECTION_MARKERS:
        idx = full_text.rfind(marker)
        if idx != -1:
            chunk = full_text[idx: idx + 2500]
            cutoff = re.search(r"(TS LAWCET 2026 Exam Date|TS LAWCET 2026 Eligibility)", chunk)
            if cutoff:
                chunk = chunk[:cutoff.start()]
            return chunk.strip()
    return None


def run_plc_watch(site_token, bot_token, chat_id):
    try:
        page_text = fetch_page_text(PLC_URL)
        current_hash = hashlib.sha256(page_text.encode()).hexdigest()
        state, sha = github_get(PLC_STATE_FILE, site_token)
        is_first_run = state is None
        changed = is_first_run or (current_hash != (state.get("last_hash") if state else None))

        telegram_sent = None
        if changed:
            prefix = ("🔍 Now watching plchyd.ac.in — baseline snapshot:\n\n" if is_first_run
                      else "🔔 plchyd.ac.in homepage has changed!\nhttps://plchyd.ac.in/\n\n")
            results = send_telegram_to_all(bot_token, chat_id, prefix + page_text[:MAX_TELEGRAM_LEN])
            telegram_sent = all(v == "sent" for v in results.values())

        new_state = {"last_hash": current_hash, "last_checked_at": datetime.now(timezone.utc).isoformat(),
                      "last_changed_at": datetime.now(timezone.utc).isoformat() if changed else (state.get("last_changed_at") if state else None)}
        github_put(PLC_STATE_FILE, site_token, new_state, sha, "PLC watch: " + ("change detected" if changed else "no change"))
        return {"ok": True, "changed": changed, "telegram_sent": telegram_sent}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def run_lawcet_news_watch(site_token, bot_token, chat_id):
    try:
        full_text = fetch_page_text(LAWCET_URL)
        section = extract_latest_update_section(full_text)
        if section is None:
            return {"ok": False, "error": "Could not locate the Latest Update section."}

        current_hash = hashlib.sha256(section.encode()).hexdigest()
        state, sha = github_get(LAWCET_STATE_FILE, site_token)
        is_first_run = state is None
        changed = is_first_run or (current_hash != (state.get("last_hash") if state else None))

        telegram_sent = None
        if changed:
            prefix = ("🔍 Now watching TS LAWCET news (via Careers360) — baseline:\n\n" if is_first_run
                      else "🔔 TS LAWCET update spotted!\nhttps://law.careers360.com/articles/ts-lawcet-2026\n\n")
            results = send_telegram_to_all(bot_token, chat_id, prefix + section[:MAX_TELEGRAM_LEN])
            telegram_sent = all(v == "sent" for v in results.values())

        new_state = {"last_hash": current_hash, "last_checked_at": datetime.now(timezone.utc).isoformat(),
                      "last_changed_at": datetime.now(timezone.utc).isoformat() if changed else (state.get("last_changed_at") if state else None)}
        github_put(LAWCET_STATE_FILE, site_token, new_state, sha, "LAWCET news watch: " + ("change detected" if changed else "no change"))
        return {"ok": True, "changed": changed, "telegram_sent": telegram_sent}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.route('/api/site-watchers', methods=['GET'])
def site_watchers():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not site_token or not bot_token or not chat_id:
        return jsonify({"ok": False, "error": "Server misconfiguration — missing env vars."}), 500

    plc_result = run_plc_watch(site_token, bot_token, chat_id)
    lawcet_result = run_lawcet_news_watch(site_token, bot_token, chat_id)
    return jsonify({"ok": True, "plc_watch": plc_result, "lawcet_news_watch": lawcet_result})


# ---------------------------------------------------------------------------
# Health check — visit this URL directly in your phone's browser to confirm
# the whole service deployed and is running, before testing individual routes.
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "lawsticker-backend-batch1", "routes": [
        "/api/wall-of-fame", "/api/update-gold-rate", "/api/pulse",
        "/api/site-activity-digest", "/api/site-watchers"
    ]})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
