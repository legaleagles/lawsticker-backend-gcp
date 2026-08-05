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
import io
import textwrap
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)
CORS(app, origins=["https://lawsticker-ai.com"])

REPO = "legaleagles/LabourLaw2"
GITHUB_API = "https://api.github.com"

# ---------------------------------------------------------------------------
# Shared helpers — identical to what every function on Vercel already used,
# just kept once here instead of duplicated across files.
# ---------------------------------------------------------------------------

def github_get(path, token, timeout=15):
    req = urllib.request.Request(
        f"{GITHUB_API}/repos/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            content = base64.b64decode(data["content"]).decode()
            return json.loads(content), data["sha"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, None
        raise


def github_put(path, token, content_obj, sha, message, timeout=15):
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def send_telegram_with_buttons(bot_token, chat_id, text, report_id):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Approve", "callback_data": f"approve:{report_id}"},
                {"text": "❌ Reject", "callback_data": f"reject:{report_id}"},
            ]]
        },
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def edit_telegram_message(bot_token, chat_id, message_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
    payload = json.dumps({
        "chat_id": chat_id, "message_id": message_id, "text": text,
        "parse_mode": "HTML", "reply_markup": {"inline_keyboard": []},
    }).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def answer_callback_query(bot_token, callback_query_id, text=""):
    url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
    payload = json.dumps({"callback_query_id": callback_query_id, "text": text}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def send_telegram_photo(bot_token, chat_id, image_bytes, caption="", button_text=None, button_url=None):
    # Telegram's sendPhoto needs multipart/form-data, not JSON like every
    # other call here — building the multipart body by hand to avoid
    # pulling in a new dependency just for this one upload.
    boundary = "----LawStickerCardBoundary"
    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

    parts = []
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"parse_mode\"\r\n\r\nHTML\r\n".encode())
    if button_text and button_url:
        markup = json.dumps({"inline_keyboard": [[{"text": button_text, "url": button_url}]]})
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"reply_markup\"\r\n\r\n{markup}\r\n".encode())
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"card.png\"\r\nContent-Type: image/png\r\n\r\n".encode())
    parts.append(image_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode())


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
# 6. Ask Durga Bro (+ Scam Verify topic) — Gemini-powered legal Q&A
# ---------------------------------------------------------------------------

KB_FILE = "knowledge-base.json"
GEMINI_MODEL = "gemini-flash-lite-latest"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
MAX_QUESTION_LEN = 500

ASKAI_STOPWORDS = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for",
             "of", "and", "or", "my", "me", "i", "do", "does", "can", "what", "how", "if",
             "it", "this", "that", "be", "have", "has", "will", "should", "would", "am"}

TOPIC_PAGE_MAP = {
    "consumer": ["rights-consumer"], "property": ["rights-property"], "family": ["rights-family"],
    "health": ["rights-health"], "digital": ["rights-digital"], "farmer": ["rights-farmer"],
    "personal": ["rights-personal"], "student": ["rights-student"],
    "tax": ["rights-tax"],
    "cinema": ["rights-cinema"],
    "lawcet": ["lawcet"],
    "llbsubjects": ["subjects"],
    "calculators": ["limitation-calc", "court-fee-calc", "chit-fund-calc", "electricity-calc",
                     "gold-loan-calc", "gold-calculator", "eligibility-calculator"],
}

TOPIC_LABELS = {
    "consumer": "Consumer Rights", "property": "Property Rights", "family": "Family Rights",
    "health": "Health Rights", "digital": "Digital Rights", "farmer": "Farmer Rights",
    "personal": "Personal Rights", "student": "Student Rights", "tax": "Income Tax Basics", "cinema": "Cinema Rights",
    "lawcet": "LAWCET Counselling", "calculators": "Site Calculators", "llbsubjects": "LLB Subjects",
    "scam_verify": "Check a Scam",
}

PUBLIC_SCAM_FILE = "scam-reports.json"
SCAM_VERIFY_RISK_LEVELS = ["High Concern", "Some Concern", "Low Concern", "Not Enough Information"]
SCAM_VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string", "enum": SCAM_VERIFY_RISK_LEVELS},
        "scam_type_label": {"type": "string", "description": "If this resembles a known scam pattern type (even loosely), name it (e.g. 'Pyramid Scheme / MLM Fraud', 'Phishing / OTP Scam'). Empty string if genuinely nothing recognizable."},
        "red_flags_found": {"type": "array", "items": {"type": "string"}, "description": "Specific warning signs identified in THIS situation, empty array if none"},
        "how_this_typically_works": {"type": "string", "description": "2-3 sentences of genuine educational context on how this type of scam generally operates — even if this specific case isn't a confirmed match, this helps the user recognize the pattern"},
        "matches_known_pattern": {"type": "boolean", "description": "True only if this genuinely resembles a pattern in the provided reported cases"},
        "matched_category": {"type": "string", "description": "Which category it resembles, empty string if matches_known_pattern is false"},
        "relevant_laws": {"type": "array", "items": {"type": "string"}, "description": "Names of well-established Indian Acts/laws relevant to this scam type, if genuinely applicable — ONLY Act names, never case citations. Empty array if not confident."},
        "verification_steps": {"type": "array", "items": {"type": "string"}, "description": "3-5 concrete, specific things the user can independently verify — not generic advice, actual actions (e.g. 'call the company's official number listed on their verified website, not the number given to you')"},
        "who_to_contact": {"type": "array", "items": {"type": "string"}, "description": "Specific, relevant helplines or authorities for THIS situation — only include ones that are genuinely relevant, not a boilerplate list every time"},
        "reasoning": {"type": "string", "description": "2-3 sentences explaining the overall assessment, plain and simple language"},
    },
    "required": ["risk_level", "scam_type_label", "red_flags_found", "how_this_typically_works", "matches_known_pattern", "matched_category", "relevant_laws", "verification_steps", "who_to_contact", "reasoning"],
}


def summarize_known_scam_patterns(entries, max_entries=50):
    # Feeding richer context per entry (red flags, relevant laws already
    # generated at approval time) gives Gemini genuinely better grounding
    # to cross-reference against, not just a bare category label. The
    # corpus is still small enough that feeding up to 50 entries (rather
    # than the previous 20) costs very little extra and covers the whole
    # database in practice.
    lines = []
    for e in entries[-max_entries:]:
        enrichment = e.get("enrichment", {})
        signals = e.get("signals", {})
        line = f"- Category: {e.get('category', 'Other')}"
        if enrichment.get("scam_type_label"):
            line += f" | Type: {enrichment['scam_type_label']}"
        if signals.get("contact_method"):
            line += f" | Contact: {signals['contact_method']}"
        if signals.get("ask_action"):
            line += f" | Asked for: {signals['ask_action']}"
        if enrichment.get("red_flags"):
            line += f" | Known red flags: {'; '.join(enrichment['red_flags'][:3])}"
        lines.append(line)
    return "\n".join(lines) if lines else "No reported cases in the database yet."


def build_scam_verify_prompt(situation, known_patterns_summary, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    return f"""You are Durga Bro, doing a genuinely thorough check on whether a described situation shows signs of being a scam — this should be a real, substantive analysis, not a quick surface-level check.

REPORTED PATTERNS FROM OUR COMMUNITY DATABASE (real, anonymized cases, with known red flags where available):
{known_patterns_summary}

USER'S SITUATION:
{situation}

Analyze thoroughly and produce, in {lang_names.get(lang, "English")}:
- risk_level: your genuine, calibrated assessment. Do NOT default to "High Concern" just to be safe — only use it when the situation clearly shows real warning signs. Use "Not Enough Information" honestly when the description is too vague to judge.
- scam_type_label: if this resembles a recognizable scam pattern (even a well-known general type, not just from the database above), name it plainly. Leave empty if genuinely nothing recognizable.
- red_flags_found: concrete warning signs actually present in what they described — do not invent flags that aren't there.
- how_this_typically_works: genuine educational context on how this type of scam generally operates — this should teach the user something real about the pattern, whether or not this specific case is confirmed.
- matches_known_pattern / matched_category: true only if this situation genuinely resembles one of the database patterns above — do not force a match.
- relevant_laws: name well-established Indian Acts relevant to this situation (e.g. Consumer Protection Act 2019, IT Act 2000, Prize Chits and Money Circulation Schemes Banning Act 1978) — ONLY if you're genuinely confident, never invent or guess. Leave empty rather than force one.
- verification_steps: give SPECIFIC, actionable things to check — not "be careful," but concrete verification actions tailored to this situation.
- who_to_contact: only list helplines/authorities that are genuinely relevant to this specific situation — don't pad with a generic list every time. Cybercrime Helpline 1930 and NALSA legal aid 15100 are real national resources, use them where genuinely applicable.
- reasoning: explain the overall assessment plainly.

Be honest and calibrated — false alarms erode trust just as much as missed warnings. If this looks like a completely normal, legitimate interaction, say so plainly rather than manufacturing concern. Give this a real, thoughtful analysis — you have room to be genuinely thorough here, not just a one-line reaction."""


def call_gemini_structured(api_key, prompt, schema, max_tokens=600):
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read().decode())
    raw_text = result["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw_text)


def call_gemini_grounded(api_key, prompt, max_tokens=1500):
    # Phase 1 of the scam-story pipeline: a real Google-Search-grounded call.
    # Structured output (responseSchema) cannot be combined with the
    # google_search tool on this model, so this returns free text plus
    # the REAL source URLs from groundingMetadata — never a URL the model
    # might have typed itself, which could be invented.
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as resp:
        result = json.loads(resp.read().decode())

    candidate = (result.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    raw_text = "".join(p.get("text", "") for p in parts)

    source_urls = []
    grounding = candidate.get("groundingMetadata", {})
    for chunk in grounding.get("groundingChunks", []):
        uri = chunk.get("web", {}).get("uri")
        title = chunk.get("web", {}).get("title", "")
        if uri:
            source_urls.append({"uri": uri, "title": title})

    return raw_text, source_urls


def build_grounded_search_prompt(category, topic_label):
    return f"""Search for a REAL, recent (last 12 months if possible) news article, government advisory, or consumer-forum report documenting an actual case of this scam pattern in India:

TOPIC: "{topic_label}"
CATEGORY: {category}

Find one concrete, real, publicly reported case or well-documented pattern. Report back in plain text:
1. What happened — real specifics from the source (names of companies/apps/platforms involved ARE fine if the source names them; this is public-interest reporting on a published source, not a new accusation).
2. How the scam mechanism worked, step by step.
3. What the outcome/impact was, per the source.
4. The exact source you found this in (publication name, and mention there IS a URL — the citation will be attached separately).

If you cannot find a real, verifiable case for this specific topic, say clearly "NO VERIFIABLE SOURCE FOUND" and nothing else."""


def extract_scam_story_from_grounded_text(api_key, grounded_text, category, topic_label):
    prompt = f"""Below is real, source-grounded research about a scam pattern in India. Turn it into structured public-education content for LawSticker AI's Scam Stories page.

RESEARCH:
{grounded_text}

TOPIC: {topic_label}
CATEGORY: {category}

RULES:
- Use the real specifics from the research above — names of companies, apps, platforms, or public figures that the research itself names ARE fine to keep, since this repackages an already-published source, not a new claim. Do NOT invent any name, number, or detail not present in the research.
- Do NOT invent source URLs or article titles yourself — a real source citation will be attached separately from search grounding.
- Write at the level of a first-time smartphone user. No legal jargon.

Generate ALL fields in ALL THREE languages in a single response:

title_en / title_te / title_hi — vivid, specific headline (max 15 words)
story_en / story_te / story_hi — 3-4 paragraphs: the hook, the escalation, the discovery/impact, grounded in the research above
remedies_en / remedies_te / remedies_hi — structured object with three arrays of plain numbered steps (no markdown):
  before: 3-5 prevention steps
  during: 2-4 steps if mid-scam right now
  after: 3-5 recovery steps — MUST include 1930 Cybercrime Helpline, cybercrime.gov.in, bank fraud dispute, FIR, and the specific IT Act 2000 / BNS 2023 section that applies
source_note — one sentence describing what kind of source this came from (e.g. "Reported by [outlet type] in [timeframe]")
category — must be one of: {SCAMED_CATEGORIES}

Write Telugu and Hindi as genuine translations — proper sentences in the correct script, never transliteration."""

    return call_gemini_structured(api_key, prompt, AI_DAILY_SCAM_ED_SCHEMA, max_tokens=4000)


GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"

# ============================================================
# LLB 5TH SEMESTER STUDY PLAN — "Eklavya"
# One-time topic-index generation per subject (45 sequential days,
# derived from the actual Osmania University syllabus units below),
# then a daily cron that generates ONLY that day's full lecture —
# never pre-generated in bulk, so nothing leaks ahead of schedule.
# ============================================================

LLB5_START_DATE = "2026-08-05"  # day 1 of the 45-day plan, IST calendar dates
LLB5_DAYS = 45

LLB5_SUBJECTS = {
    "cpc": {
        "name": "Civil Procedure Code & Law of Limitation",
        "short": "CPC & Limitation",
        "semester": 5,
        "units": {
            "Unit I": "Codification of Civil Procedure and Introduction to CPC — Principal features of the Civil Procedure Code — Suits — Parties to Suit — Framing of Suit — Institution of Suits — Bars of Suit — Doctrines of Sub Judice and Res Judicata — Place of Suing — Transfer of suits — Territorial Jurisdiction — 'Cause of Action' and Jurisdictional Bars — Summons — Service of Foreign summons.",
            "Unit II": "Pleadings — Contents of pleadings — Forms of Pleading — Striking out/Amendment of Pleadings — Plaint — Essentials of Plaint — Return of Plaint — Rejection of Plaint — Production and marking of Documents — Written Statement — Counterclaim — Set off — Application of Sec.89 — Framing of issues.",
            "Unit III": "Appearance and Examination of parties & Adjournments — Ex-parte Procedure — Summoning and Attendance of Witnesses — Examination — Admissions — Production, Impounding, Return of Documents — Hearing — Affidavit — Judgment and Decree — Concepts of Judgment, Decree, and Interim Orders and Stay — Injunctions — Appointment of Receivers and Commissions — Costs — Execution — Concept of Execution — General Principles of Execution — Power of Executing Courts — Procedure for Execution — Modes of Execution — Arrest and detention — Attachment and Sale.",
            "Unit IV": "Suits in Particular Cases — Suits by or against Government — Suits relating to public matters — Suits by or against minors, persons with unsound mind — Suits by indigent persons — Interpleader suits — Incidental and supplementary proceedings — Appeals, Reference, Review and Revision — Appeals from Original Decrees — Appeals from Appellate Decrees — Appeals from Orders — General Provisions Relating to Appeals.",
            "Unit V": "Law of Limitation — Concept of Limitation — Object of limitation — General Principles of Limitation — Extension — Condonation of delay — Sufficient Cause — Computation of limitation — Acknowledgment and Part-payment — Legal Disability — Provisions of the Limitation Act, 1963 (Excluding Schedule)."
        }
    },
    "bnss": {
        "name": "Bharatiya Nagarik Suraksha Sanhita, Juvenile Justice & Probation of Offenders",
        "short": "BNSS & Juvenile Justice",
        "semester": 5,
        "units": {
            "Unit I": "BNSS — Object and Importance — Comparison with Cr.P.C, 1973 — Definitions — Difference between Cognizable and Non-Cognizable Offences — Bailable and Non-Bailable Offences — Investigation, Inquiry and Trial — Classification of Criminal Courts, Jurisdiction and Powers — Directorate of Prosecution — role of Prosecutors under BNSS — Role of Defence Lawyer — Role of Prisons and Correctional Methods — Indian Constitution and BNSS.",
            "Unit II": "Maintenance of Wife, Children and Parents (Sec.144-147) — Security for Keeping Peace and Good Behaviour (Sec.125-143) — Cognizance by police — Role of Police under BNSS — Preventive Action of Police — Unlawful Assembly — Public Nuisance — Urgent Cases of Nuisance (Sec.148-172) — Information to Police — FIR (Sec.173-196) — Arrest of Persons (Sec.35-62) — Arrest with/without Warrant — Rights of Arrested Person — Proclamation and Attachment of Property (Sec.63-93) — Process to Compel Production of Things (Sec.94-110).",
            "Unit III": "Trial, Charge, Inquiries and Bail — Complaints to Magistrates — Process to Compel Appearance — Cognizance of Offences by Magistrate — General Principles of Fair Trial (Sec.197-222) — Trial (Sec.223-233) — Charge, Joinder of Charges (Sec.234-247) — Trial Before Court of Session (Sec.248-260) — Trial of Warrant Case by Magistrate (Sec.261-273) — Trial of Summons Cases (Sec.274-282) — Summary Trials (Sec.283-288) — Plea Bargaining (Sec.289-300) — Provisions as to Bails and Bonds (Sec.478-496) — General Provisions as to Inquiries & Trial (Sec.337-378).",
            "Unit IV": "Administration of Criminal Justice — Offences affecting Administration of Justice (Sec.379-391) — The Judgement (Sec.392-406) — Submission of Death Sentence for Confirmation (Sec.407-412) — Appeal, Revision, Reference (Sec.413-435) — Execution, Suspension, Remission and Commutation of Sentence (Sec.453-477) — Inherent Powers of the Court — Transfer of Criminal Cases.",
            "Unit V": "The Juvenile Justice (Care and Protection of Children) Act, 2015 — Preliminary and General Provisions — Salient Features — Procedure — Treatment and Rehabilitation of Juveniles — Protection of Juvenile offenders — Legislative and Judicial Role — Probation of Offenders Act — Probation and Parole — Authority Granting Parole — Supervision — Conditional Release — Suspension of Sentence — Salient Features of the Act."
        }
    },
    "banking": {
        "name": "Law of Banking and Negotiable Instruments",
        "short": "Banking & NI Act",
        "semester": 5,
        "units": {
            "Unit I": "History of the Banking Regulation Act — Salient features — Banking Business and its importance in modern times — Different kinds of Banking — impact of Information Technology on Banking.",
            "Unit II": "Relationship between Banker and Customer — Debtor and Creditor Relationship — Fiduciary Relationship — Trustee and Beneficiary — Principal and Agent — Bailor and Bailee — Guarantor.",
            "Unit III": "Cheques — Crossed Cheques — Account Payee — Banker's Drafts — Dividend Warrants — Negotiable instruments and deemed negotiable instruments — Salient features of The Negotiable Instruments Act — The Negotiable Instruments (Amendment) Act 2018.",
            "Unit IV": "The Paying Banker — Statutory protection to Bankers — Collecting Banker — Statutory protection — Rights and obligations of paying and collecting bankers.",
            "Unit V": "Banker's lien and set off — Advances — Pledge — Land — Stocks — Shares — Life Policies — Document of title to Goods — Bank Guarantees — Letters of Credit — Recovery of Bank loans and position under the SARFAESI Act, 2002 — Jurisdiction and powers of Debt Recovery Tribunal."
        }
    },
    "adr": {
        "name": "Alternate Dispute Resolution",
        "short": "ADR",
        "semester": 5,
        "units": {
            "Unit I": "Alternate Dispute Resolution — Characteristics — Advantages and Disadvantages — Unilateral, Bilateral, Triadic (Third Party) Intervention — Techniques and processes — Negotiation, Conciliation, Mediation, Arbitration — Distinction between Arbitration, Conciliation and Negotiation — ADR under different laws in India.",
            "Unit II": "The Arbitration and Conciliation Act, 1996 — Historical Background and Objectives — Amendment Acts 2015 & 2019 — Definitions of Arbitration, Arbitrator, Arbitration Agreement — Appointment/Termination of Arbitrator — Proceedings in Arbitral Tribunal — Arbitral Award — Setting aside of Award — Finality and Enforcement — Conciliation — Appointment/Powers of Conciliators — Arbitration Council of India (ACI) — International Commercial Arbitration — UNCITRAL Model Law 1985 — Geneva Convention 1927 — New York Convention 1958 — Recognition and Enforcement of Foreign Award — Singapore Convention on Mediation 2019 — Online Dispute Resolution.",
            "Unit III": "Other Alternative Dispute Resolution Systems — Tribunals — Lokpal and Lokayukta — Lok Adalats — Family Courts — Commercial Courts — Section 89 and Order X, Rules 1A, 1B, 1C of CPC — ADR and Mediation Rules — Pre-litigation Mediation in India."
        }
    },
    "ethics": {
        "name": "Professional Ethics and Professional Accounting System",
        "short": "Professional Ethics",
        "semester": 5,
        "units": {
            "Unit I": "Development of Legal Profession in India — The Advocates Act, 1961 — Right to Practice — Constitutional guarantee under Article 19(1)(g) — Enrolment and Practice — Latest BCI Rules — All India Bar Examination (AIBE) — Advocates and Solicitors' firm — Elements of Advocacy.",
            "Unit II": "Seven lamps of advocacy — Advocate's duties towards public, clients, court, and other advocates and legal aid — Bar Council of India's Code of Ethics.",
            "Unit III": "Disciplinary proceedings — Professional misconduct — Disqualifications — Functions of Bar Council of India/State Bar Councils — Disciplinary Committees — Powers and functions — Disqualification and removal from rolls.",
            "Unit IV": "Professional Accounting — Accountancy for Lawyers — Nature and functions of accounting — Important branches of accounting — Accounting and Law — Bar Bench Relations."
        }
    },

    # ---------------- SEMESTER I ----------------
    "s1-contract1": {
        "name": "Law of Contract – I", "short": "Contract-I", "semester": 1,
        "units": {
            "Unit I": "Definition and essentials of a valid Contract — Definition and essentials of a valid Offer — Definition and essentials of valid Acceptance — Communication of Offer and Acceptance — Revocation of Offer and Acceptance through various modes including electronic medium — Consideration — salient features — Exception to consideration — Doctrine of Privity of Contract — Exceptions to the privity of contract — Standard form of Contract.",
            "Unit II": "Capacity of the parties — Effect of Minor's Agreement — Contracts with insane persons and persons disqualified by law — Concepts of Free Consent — Coercion — Undue influence — Misrepresentation — Fraud — Mistake — Lawful Object — Immoral agreements and various heads of public policy — illegal agreements — Uncertain agreements — Wagering agreements — Contingent contracts — Void and Voidable contracts.",
            "Unit III": "Discharge of Contracts — By performance — Appropriation of payments — Performance by joint promisors — Discharge by Novation — Remission — Accord and Satisfaction — Discharge by impossibility of performance (Doctrine of Frustration) — Discharge by Breach — Anticipatory Breach — Actual breach.",
            "Unit IV": "Quasi Contract — Necessaries supplied to a person who is incapable of entering into a contract — Payment by an interested person — Liability to pay for non-gratuitous acts — Rights of finder of lost goods — Things delivered by mistake or coercion — Quantum merit — Remedies for breach of contract — Kinds of damages — liquidated and unliquidated damages and penalty — Duty to mitigate.",
            "Unit V": "Specific Relief Act including 2018 Amendment — Recovering possession of property — Specific performance of the contract — Rectification of instruments — Rescission of contracts — Cancellation of instruments — Declaratory Decrees — Preventive Relief Injunctions — Temporary and Perpetual injunctions — Mandatory & Prohibitory injunctions — Injunctions to perform negative agreement — Limited liability partnership (LLP) — Special provision for contracts relating to infrastructure projects — Arbitration clause — Impact of COVID-19 on 'specific performance of contracts'."
        }
    },
    "s1-familylaw1": {
        "name": "Family Law – I (Hindu Law)", "short": "Family Law-I", "semester": 1,
        "units": {
            "Unit I": "Sources of Hindu Law — Scope and application of Hindu Law — Schools of Hindu Law — Mitakshara and Dayabhaga Schools — Concept of Joint Family, Coparcenary, Joint Family Property and Coparcenary Property — Institution of Karta — Powers and Functions of Karta — Pious Obligation — Partition — Debts and alienation of property.",
            "Unit II": "Marriage — Definition — Importance of institution of marriage under Hindu Law — Conditions of Hindu Marriage — Ceremonies and Registration — Monogamy — Polygamy — Recent Trends in the institution of marriage.",
            "Unit III": "Matrimonial Remedies under the Hindu Marriage Act, 1955 — Restitution of Conjugal Rights — Nullity of marriage — Judicial separation — Divorce — Maintenance pendente lite — importance of conciliation — Role of Family Courts in Resolution of matrimonial disputes.",
            "Unit IV": "Concept of Adoption — Historical perspectives of adoption in India — In country and inter-country adoptions — Law of Maintenance — Law of Guardianship — The Hindu Adoption and Maintenance Act, 1956 — The Hindu Minority and Guardianship Act 1956.",
            "Unit V": "Succession — Intestate succession — Succession to property of Hindu Male and Female — Dwelling House — Hindu Succession Act, 1956 as amended by Hindu Succession (Andhra Pradesh Amendment) Act, 1986 & the Hindu Succession (Amendment) Act, 2005 — Notional Partition — Classes of heirs — Enlargement of limited estate of women into their absolute estate — Daughter's right to inherit ancestral property and impact of recent changes in law."
        }
    },
    "s1-constlaw1": {
        "name": "Constitutional Law – I", "short": "Constitutional Law-I", "semester": 1,
        "units": {
            "Unit I": "Constitution — Meaning and Significance — Evolution of Modern Constitutions — Classification of Constitutions — Indian Constitution — Historical Perspectives — Government of India Act, 1919 — Government of India Act — Framing of Indian Constitution — Role of Drafting Committee of the Constituent Assembly.",
            "Unit II": "Nature and Salient Features of Indian Constitution — Preamble to Indian Constitution — Union and its Territories — Citizenship — General Principles relating to Fundamental Rights (Art.13) — Definition of State — Doctrine of Judicial Review.",
            "Unit III": "Right to Equality (Art.14-18) — Freedoms and Restrictions under Art.19 — Protection against Ex-post facto law — Guarantee against Double Jeopardy — Privilege against Self-incrimination — Right to Life and Personal Liberty — Right to Education — Protection against Arrest and Preventive Detention.",
            "Unit IV": "Rights against Exploitation — Right to Freedom of Religion — Cultural and Educational Rights — Right to Constitutional Remedies — Limitations on Fundamental Rights (Art.31-A, 31-B, 31-C, 335, 358 & 359).",
            "Unit V": "Directive Principles of State Policy — Significance — Nature — Classification — Application and Judicial Interpretation — Relationship between Fundamental Rights and Directive Principles — Fundamental Duties: Significance, Enforceability and Judicial Interpretation."
        }
    },
    "s1-torts1": {
        "name": "Law of Torts including Motor Vehicle Accidents and Consumer Protection Laws", "short": "Torts & Consumer Law", "semester": 1,
        "units": {
            "Unit I": "Nature of Law of Torts — Definition of Tort — Elements of Tort — Development of Law of Torts in England and India — Wrongful Act and Legal Damage — Damnum Sine Injuria and Injuria Sine Damno — Tort distinguished from Crime and Breach of Contract — General Principles of Liability in Torts — Fault — Wrongful intent — Malice — Negligence — Liability without fault — Statutory liability — Parties to proceedings.",
            "Unit II": "General Defences to an action in Torts — Vicarious Liability — Liability of the State for Torts — Defence of Sovereign Immunity — Joint Liability — Liability of Joint Tortfeasors — Rule of Strict Liability (Rylands v Fletcher) — Rule of Absolute Liability (M.C. Mehta v. Union of India) — Occupiers liability — Extinction of liability — Waiver and Acquiescence — Release — Accord and Satisfaction — Death.",
            "Unit III": "Specific Torts — Torts affecting the person — Assault — Battery — False Imprisonment — Malicious Prosecution — Nervous Shock — Torts affecting Immovable Property — Trespass to land — Nuisance — Public Nuisance and Private Nuisance — Torts relating to movable property — Liability arising out of accidents (Relevant provisions of the Motor Vehicles Act).",
            "Unit IV": "Defamation — Negligence — Torts against Business Relations — Injurious falsehood — Negligent Misstatement — Passing off — Conspiracy — Torts affecting family relations — Remedies — Judicial and Extra-judicial Remedies — Damages — Kinds of Damages — Assessment of Damages — Remoteness of damage — Injunctions — Death in relation to tort — Actio personalis moritur cum persona.",
            "Unit V": "Consumer Laws: Common Law and the Consumer — Duty to take care and liability for negligence — Consumerism — Salient features of the Consumer Protection Act, 1986 — Consumer Protection Act, 2019 — Definition of Consumer — Rights of Consumers — Defects in goods and deficiency in services — Restrictive and Unfair Trade Practices — Redressal Machinery under the Consumer Protection Act — Consumer Protection Councils — Central Consumer Protection Authority (CCPA) — Liability of Service Providers, Manufacturers and Traders — Product Liability — Consumer Disputes Redressal Commissions: Jurisdiction and Powers — Procedure for filing a consumer dispute — E-filing — Continuous cause of action — Civil & Criminal liability — ADR & consumer — Penalties for misleading advertisement — Mediation under the Act."
        }
    },
    "s1-envlaw1": {
        "name": "Environmental Law", "short": "Environmental Law", "semester": 1,
        "units": {
            "Unit I": "The meaning and definition of environment — Ecology — Ecosystems — Biosphere — Biomes — Ozone depletion — Global Warming — Climatic changes — Need for the preservation, conservation and protection of environment — Ancient Indian approach to environment — Environmental degradation and pollution — Kinds, causes and effects of pollution.",
            "Unit II": "Common Law remedies against pollution — trespass, negligence, and theories of Strict Liability & Absolute Liability — Relevant provisions of I.P.C. and Cr.P.C. and C.P.C. for the abatement of public nuisance in pollution cases — Remedies under Specific Relief Act — Reliefs against smoke and noise — Noise Pollution.",
            "Unit III": "The law relating to preservation, conservation and protection of forests, wild life and endangered species, marine life, coastal ecosystems and lakes etc — Prevention of cruelty towards animals — Law relating to prevention and control of water pollution — Air Pollution — Environment (Protection) Act, 1986 — Biological Diversity Act, 2002 — Hazardous Wastes (Management, Handling and Transboundary) Regulations — Environment pollution control mechanism — National Environmental Tribunal and National Environmental Appellate Authority — National Green Tribunal — powers and jurisdiction.",
            "Unit IV": "Art. 48A and Art. 51A(g) of the Constitution of India — Right to wholesome environment — Right to development — Restriction on freedom of trade, profession, occupation for the protection of environment — Immunity of Environment legislation from judicial scrutiny (Art.31C) — Legislative powers of the Centre and State Government — Writ jurisdiction — Role of Indian Judiciary in the evolution of environmental jurisprudence — Role of green belt development.",
            "Unit V": "International Environmental Regime — Transactional Pollution — State Liability — Customary International Law — Liability of Multinational corporations/Companies — Stockholm Declaration on Human Environment 1972 — Role of UNEP — Ramsar Convention 1971 — Bonn Convention (Migratory Birds) 1992 — Nairobi Convention 1982 (CFCC) — Biodiversity Convention (Earth Summit) 1992 — Kyoto Protocol 1997 — Johannesburg Convention 2002 — UN Framework Convention on Climate Change (UNFCCC) — UN Climate Change Conference (COP21) & Paris Agreement 2016."
        }
    },

    # ---------------- SEMESTER II ----------------
    "s2-contract2": {
        "name": "Law of Contract – II", "short": "Contract-II", "semester": 2,
        "units": {
            "Unit I": "Indemnity and Guarantee — Contract of Indemnity, definition — Rights of Indemnity holder — Liability of the indemnified — Contract of Guarantee — Definition — Essential characteristics — Distinction between Indemnity and Guarantee — Kinds of Guarantee — Rights and liabilities of Surety — Discharge of surety — Contract of Bailment — Definition — Essential requisites — Kinds of bailment — Rights and duties of bailor and bailee — Termination of bailment — Pledge — Definition — Rights and duties of Pawnor and Pawnee — Pledge by non-owner.",
            "Unit II": "Contract of Agency — Definition of Agent — Creation of Agency — Rights and duties of Agent — Delegation of authority — Personal liability of agent — Relations of principal and agent with third parties — Termination of Agency.",
            "Unit III": "Contract of Sale of Goods — Formation of contract — Subject matter of sale — Conditions and Warranties — Express and implied conditions and warranties — Pricing — Caveat Emptor — Hire Purchase Agreements.",
            "Unit IV": "Property — Possession and Rules relating to passing of property — Sale by non-owner — Nemo dat quod non habet — Delivery of goods — Rights and duties of seller and buyer before and after sale — Rights of unpaid seller — Remedies for breach.",
            "Unit V": "Contract of Partnership — Definition and nature of partnership — Formation of partnership — Test of partnership — Partnership and other associations — Registration of firm — Effect of non-registration — Relations of partners — Rights and duties of partners — Property of firm — Relation of partners to third parties — Implied authority of partners — Kinds of partners — Minor as partner — Reconstitution of firm — Dissolution of firm — Limited Liability Partnership (LLP)."
        }
    },
    "s2-familylaw2": {
        "name": "Family Law – II (Muslim Law and Other Personal Laws)", "short": "Family Law-II", "semester": 2,
        "units": {
            "Unit I": "Origin and development of Muslim Law — Sources of Muslim Law — Schools of Muslim Law — Difference between the Sunni and Shia Schools — Sub-schools of Sunni Law — Operation and application of Muslim Law — Conversion to Islam — Effects of conversion — Law of Marriage — Essential requirements of valid Marriage — Kinds of Marriages — distinction between void, irregular and valid marriage — Dower (Mahr) — origin, nature, importance and classification of dower — The Muslim Women (Protection of Rights on Marriage) Act, 2019.",
            "Unit II": "Divorce — Classification of divorce — different modes of Talaq — Legal consequences of divorce — Validity of Triple Talaq: Judicial Interpretation and Legislative Response — Dissolution of Muslim Marriages Act, 1939 and its Amendment — Maintenance: Principles of maintenance & persons entitled — The Muslim Women (Protection of Rights on Divorce) Act, 1986 — Effect of conversion on maintenance and difference between Shia and Sunni Law.",
            "Unit III": "Parentage — Maternity and Paternity — Legitimacy and acknowledgment — Guardianship — Meaning — Kinds of guardianship — Removal of guardian — Difference between Shia and Sunni Law — Gift — Definition — Requisites of valid gift — Gift formalities — Revocation of gift — Kinds of gift — Wills: Meaning, Requisites of valid Will, Revocation of Will — Distinction between Will and Gift — Difference between Shia and Sunni Law.",
            "Unit IV": "Waqf — Definition — Essentials of Waqf — Kinds of Waqf — Creation of Waqf — Revocation of Waqf — Salient features of the Waqf Act, 1995 — Recent Changes in Waqf Laws and impact — Mutawalli — Powers and duties of Mutawalli — Removal of Mutawalli and Management of Waqf property — Succession — Administration — Waqf Tribunals and Jurisdiction.",
            "Unit V": "Special Marriage Act, 1954 — Salient features of Indian Divorce Act — Domicile — Maintenance to dependents/Spouses — Intestate succession of Christians under the Indian Succession Act, 1925."
        }
    },
    "s2-constlaw2": {
        "name": "Constitutional Law – II", "short": "Constitutional Law-II", "semester": 2,
        "units": {
            "Unit I": "Legislature under Indian Constitution — Union and State Legislatures — Composition, Powers, Functions and Privileges — Anti-Defection Law — Executive under Indian Constitution — President and Union Council of Ministers — Governor and State Council of Ministers — Powers and position of President and Governor.",
            "Unit II": "Judiciary under Constitution — Supreme Court — Appointment of Judges, Powers and Jurisdiction — High Courts — Appointment and Transfer of Judges — Powers and Jurisdiction — Subordinate Judiciary — Independence of judiciary — Judicial Accountability.",
            "Unit III": "Centre State Relations — Cooperative and Competitive Federalism — Legislative, Administrative and Financial Relations — Cooperation and Coordination between the Centre and States — Judicial Interpretation of Centre-State Relations — Local Self Government under 73rd and 74th Amendments, 1992.",
            "Unit IV": "Liability of State in Torts and Contracts — Freedom of Interstate Trade, Commerce and Intercourse — Services under the State — All India Services — Public Service Commissions — Election Commissions.",
            "Unit V": "Emergency — Need of Emergency Powers — Different kinds of Emergency — National, State and Financial emergency — Impact of Emergency on Federalism and Fundamental Rights — Amendment of Indian Constitution and Basic Structure Theory."
        }
    },
    "s2-crimes1": {
        "name": "Law of Crimes", "short": "Law of Crimes", "semester": 2,
        "units": {
            "Unit I": "Concept of crime — Meaning of Crime — Distinction between Crime and Tort — Stages of Crime — Intention, Preparation, Attempt and Commission of Crime — Elements of Crime — Actus Reus and Mens Rea — Codification of Law of Crimes in India — IPC 1860 — Application of the Bharatiya Nyaya Sanhita, 2023 (Sec.1-3) — Territorial and Extra-Territorial Application (Sec.1) — Definition (Sec.2) — Punishments (Sec.4-13).",
            "Unit II": "General Explanations — General Exceptions under BNS 2023 — Abetment — Criminal Conspiracy — Attempt — Offences against Women and Child (Sec.63-87) — Sexual Offences — Assault and Criminal Force against Women — Offences relating to Marriage — Kidnapping and Abduction — Causing Miscarriage — Offences against Child (Sec.88-99).",
            "Unit III": "Offences affecting Human Life (Sec.100-113) — Culpable Homicide and Murder — Causing Death by Negligence — Organised Crime — Petty Organised Crime — Terrorist Act — Offences affecting Human Body (Sec.114-144) — Hurt and Grievous Hurt — Wrongful restraint and Wrongful confinement — Criminal Force and Assault — Kidnapping and Abduction.",
            "Unit IV": "Offences against the State — Offences Relating to Army, Navy and Air Force — Offences relating to Election — Offences Relating to Coin, Currency-Notes, Bank-Notes, and Government Stamps — Offences against Public Tranquillity — Offences by or relating to Public Servants — Contempt of Lawful Authorities of Public Servants — False Evidence and Offences against Public Justice — Offences affecting the Public Health, Safety, Convenience, Decency and Morals.",
            "Unit V": "Offences relating to Religion — Offences against Property — Theft — Extortion — Robbery & Dacoity — Cheating — Mischief — Criminal Trespass — Criminal Misappropriation of Property and Criminal Breach of Trust — Receiving Stolen Property — Offences relating to Documents and Property Marks — Criminal Intimidation, Insult, Annoyance, Defamation. (Note: Comparative study of IPC 1860 and BNS 2023 wherever necessary.)"
        }
    },
    "s2-evidence1": {
        "name": "Law of Evidence", "short": "Law of Evidence", "semester": 2,
        "units": {
            "Unit I": "Bharatiya Sakshya Adhiniyam, 2023 — Salient Features — Meaning and Kinds of Evidence — Interpretation Clause — Documents, May Presume, Shall Presume and Conclusive Proof — Fact, Fact in Issue and Relevant Facts, Proved, Disproved — Distinction Between Relevancy and Admissibility — Doctrine of Res Gestae — Motive, Preparation and Conduct — Conspiracy — When Facts Not Otherwise Relevant Become Relevant — Right and Custom — Facts Showing the State of Mind.",
            "Unit II": "Admissions & Confessions — General Principles — Differences between 'Admission' and 'Confession' — Confessions obtained by inducement, threat or promise — Confessions made to police officer — Statement made in custody leading to discovery of incriminating material — Admissibility of Confessions by one accused against co-accused — Dying Declarations and their evidentiary value — Other Statements by persons who cannot be called as Witnesses — Admissibility of evidence in previous judicial proceedings.",
            "Unit III": "Relevancy of Judgments — Opinion of witnesses — Expert's opinion — Opinion on Relationship, proof of marriage — Facts which need not be proved — Oral and Documentary Evidence — Primary, electronic or digital record — admissibility of electronic records and Secondary evidence — Modes of proof of execution of documents — Presumptions as to documents — Exclusion of Oral by Documentary Evidence — Relevance of social media in the law of evidence.",
            "Unit IV": "Rules relating to Burden of Proof — Presumption as to Dowry Death — Estoppels — Kinds of estoppels — Res Judicata, Waiver and Presumption.",
            "Unit V": "Competency to testify — Privileged communications — Testimony of Accomplice — Examination in Chief, Cross examination and Re-examination — Leading questions — Lawful questions in cross examination — Compulsion to answer questions — Hostile witness — Impeaching the credit of witness — Refreshing memory — Questions of corroboration — Improper admission and rejection of evidence. (Comparative study of Indian Evidence Act 1872 and BSA 2023 wherever necessary.)"
        }
    },

    # ---------------- SEMESTER III ----------------
    "s3-jurisprudence": {
        "name": "Jurisprudence", "short": "Jurisprudence", "semester": 3,
        "units": {
            "Unit I": "Meaning and Definition of Jurisprudence — General and Particular Jurisprudence — Elements of Ancient Indian Jurisprudence — Schools of Jurisprudence — Analytical, Historical, Philosophical and Sociological Schools — Theories of Law — Meaning and Definition of Law — Nature and Function of Law — Purpose of Law — Classification of Law — Equity, Law and Justice — Theory of Sovereignty.",
            "Unit II": "Sources of Law — Legal and Historical Sources — Legislation — Classification of legislation — Supreme and Subordinate Legislation — Direct and Indirect Legislation — Principles of Statutory Interpretation — Precedent — Kinds of Precedent — Stare Decisis — Original and Declaratory Precedents — Authoritative and Persuasive Precedents — Custom — Kinds of Custom — Requisites of a valid custom — Relative merits and demerits of Legislation, Precedent and Custom — Codification: Concept, Advantages and disadvantages.",
            "Unit III": "Persons — Nature of personality — Legal Status of Lower Animals, Dead Persons and Unborn persons — Legal Persons — Corporations — Purpose of Incorporation — Nature of Corporate Personality — Rights and Duties — Definition of Right — Classification of Rights and Duties — Absolute and Relative Rights and Duties — Rights and Cognate concepts like Liberty, Power, Immunity, Privilege.",
            "Unit IV": "Obligation — Nature of Obligation — Obligation arising out of Contract, Quasi Contract, trust and breach of obligation — Liability — Nature and kinds of liability — Acts — Mens Rea — Intention and Motive — Relevance of Motive — Negligence — Strict Liability — Accident — Vicarious Liability — measure of Civil and Criminal Liability.",
            "Unit V": "Ownership — Definition and kinds of Ownership — Possession — Elements of Possession — Relation between Ownership and Possession — Possessory Remedies — Property — Meaning — Kinds of Property — Modes of Acquisition of Property — Legal Sanctions — Meaning and Classification of Sanctions — Civil and Criminal Justice — Concept of Justice — Theories regarding purpose of Criminal Justice — Deterrent, Preventive, Reformative and Retributive theories."
        }
    },
    "s3-property1": {
        "name": "Law of Property", "short": "Property Law", "semester": 3,
        "units": {
            "Unit I": "Meaning and concept of property — Kinds of property — Transfer of property — Transferable and non-transferable property — Who can transfer — Operation of transfer — Mode of transfer — Conditional transfer — Void and unlawful conditions — Condition precedent and condition subsequent — Vested and contingent interest — Transfer to unborn persons.",
            "Unit II": "Doctrine of Election — Covenants — Transfer by ostensible owner — Doctrine of Feeding the Grant by Estoppel — Doctrine of Lis Pendens — Fraudulent Transfers — Doctrine of Part-performance.",
            "Unit III": "Sale: Essential features, Mode of Sale, Rights and liabilities of parties — Mortgage: Kinds of Mortgages — Rights and liabilities of mortgagor and mortgagee — Marshalling and Contribution — Charges.",
            "Unit IV": "Lease — Essential features — Kinds of leases — Rights and liabilities of lessor and lessee — Termination of lease — forfeiture — Exchange — Gifts — Different types of gifts — Registration of Gifts — Transfer of Actionable Claims.",
            "Unit V": "Easements: Definition, Distinction between Lease and License — Dominant and Servient Tenements — Acquisition of property through testamentary succession — Will — Codicil — Capacity to execute Will — Nature of bequests — Executors of Will — Rights and Obligations of Legatees."
        }
    },
    "s3-adminlaw": {
        "name": "Administrative Law", "short": "Administrative Law", "semester": 3,
        "units": {
            "Unit I": "Nature and scope of Administrative Law — Meaning, Definition and Evolution — Reasons for the growth of Administrative Law — Relationship between Administrative Law and Constitutional Law.",
            "Unit II": "Basic concepts of Administrative Law — Rule of Law — Interpretation of Dicey's Principle of Rule of Law — Modern trends — Theory of Separation of Powers — Position in India, UK and USA.",
            "Unit III": "Classification of Administrative functions: Legislative, Quasi-judicial, Administrative and Ministerial functions — Delegated Legislation: Meaning, Reasons for growth, Classification — Judicial, Legislative and Procedural Control of Delegated legislation.",
            "Unit IV": "Judicial Control of Administrative Action — Grounds of Judicial Control — Principles of Natural Justice — Administrative discretion and its control — Wednesbury Principle (Doctrine of Proportionality) — Doctrine of Legitimate Expectation.",
            "Unit V": "Remedies available against the State — Writs — Lokpal and Lokayukta — Right to Information — Liability of the State in Torts and Contracts — Rule of Promissory Estoppel — Administrative Tribunals — Commissions of Inquiry — Public Corporations."
        }
    },
    "s3-companylaw": {
        "name": "Company Law", "short": "Company Law", "semester": 3,
        "units": {
            "Unit I": "Corporate Personality — General Principles of Company Law — Nature and Definition of Company — Private Company and Public Company — One Person Company — Characteristics of a Company — Different kinds of Company — Registration & Incorporation — Lifting the Corporate Veil — Company distinguished from Partnership, HUF and LLP.",
            "Unit II": "Promoters — Memorandum of Association — Doctrine of Ultra Vires — Articles of Association — Doctrine of Indoor Management — Prospectus — Civil and Criminal liability, Compounding of offences under Sec.441, decriminalization — Liability for misstatement in prospectus — Statement in lieu of Prospectus — Pre-incorporation Contracts — Membership in a Company — Borrowing Powers — Debentures & Charges — insider trading of company shares.",
            "Unit III": "Shares & Stock — Kinds of shares — Statutory restrictions on allotment of shares — Intermediaries — Call on shares — Transfer and Transmission of shares — Reduction on transfer of shares — Rectification of register on transfer — Certification and issue of certificate of transfer — Limitation of time for issue of certificates.",
            "Unit IV": "Directors — women director — Independent director — Different kinds of Directors — Appointment, position, qualifications and disqualifications — Powers, Rights and Duties of Directors — Meetings and proceedings — kinds of meetings — Statutory meeting — Annual General Meeting — Extraordinary meeting — Power of the Tribunal to order meeting — Chairman for meetings — Proxy — Resolutions — Minutes — Shareholders Activism — Corporate Social Responsibility.",
            "Unit V": "Accounts and Audit — Inspection and Investigation — Compromises, Reconstruction and Amalgamation — Majority rule and Rights of minority shareholders — Prevention of oppression and mismanagement — class action — Revival and rehabilitation of sick industrial companies — Mergers, Amalgamation and Takeover — Winding up of companies — Modes and consequences of winding up — Insolvency and Bankruptcy Code, 2016 in relation to winding up — Authorities: NCLAT, NCLT, ROC, SFIO — Corporate governance and pandemic-related relaxations."
        }
    },
    "s3-labourlaw1": {
        "name": "Labour Law – I (Trade Union Laws and Industrial Dispute Act)", "short": "Labour Law-I", "semester": 3,
        "units": {
            "Unit I": "Concept of Labour through the ages — Trade Unions: History of Trade Union Movement — Trade Unions according to Industrial Relations Code 2020 — Definitions — Registration — Rights and Liabilities of Registered Trade Union — Immunities — Amalgamation and Dissolution of unions — Reorganization of Trade Unions.",
            "Unit II": "Prevention and Settlement of Industrial Disputes in India — Role of State in Industrial Relations under new Industrial Relations Code 2020 — Definition of industry, Industrial Dispute — Individual Dispute — Workmen — special provisions relating to Lay-Off, Retrenchment, Closure, Award, Strike, Lockout under Chapter X.",
            "Unit III": "Authorities under the ID Code — Works Committee — Conciliation — Limitation to raise dispute — Court of Inquiry — Tribunals — Powers and Functions of Authorities — Voluntary Arbitration — Alteration of conditions of service — Management rights of action during pendency of proceedings — Recovery of money due from employer — Unfair labour practices — miscellaneous provisions.",
            "Unit IV": "Standing Orders — concept and Nature — Certification process — operation and binding effect — modification and Temporary application of Model Standing Orders — Interpretation and enforcement of Standing Orders and provisions in the Industrial Relations Code 2020.",
            "Unit V": "Disciplinary proceedings in Industries — Termination of employment and notice thereof — Suspension or dismissal for misconduct, acts or omissions which constitute misconduct — Means of redress for workers against unfair treatment or wrongful executions."
        }
    },

    # ---------------- SEMESTER IV ----------------
    "s4-labourlaw2": {
        "name": "Labour Law – II", "short": "Labour Law-II", "semester": 4,
        "units": {
            "Unit I": "Wages — Concepts — Minimum, Fair, Living Wages — Wage and Industrial Policies — Whitley Commission Recommendations — Provisions of Code on Wages 2019 — Timely payment of wages — Authorized deductions — Claims — Minimum Wages under the Code — Definitions — Types of wages — Procedure for fixing and revising Minimum Wages — Remedy.",
            "Unit II": "Bonus — concept — Right to claim Bonus — Full Bench formula — Payment of Bonus under the Code on Wages 2019 — Computation of gross profit, available/allocable surplus — Eligibility, Disqualification of Bonus — set on/set off — Minimum and Maximum Bonus — Recovery of Bonus.",
            "Unit III": "Employees Security and Welfare — Social Security — Concept — Social Insurance — Social Assistance Schemes — Law relating to workmen's compensation — Employee's Compensation Act 1923 — Employer's liability — Nexus between injury and employment — Employees State Insurance Act 1948 — Application, Benefits, Adjudication of disputes — ESI Corporation.",
            "Unit IV": "Employees Provident Fund and Miscellaneous Provisions Act 1952 — Contributions, Schemes, Benefits — Maternity Benefit (Amendment) Act, 2018 — Definitions, Application, Benefits — Payment of Gratuity Act 1972 — Definitions, application, payment of gratuity eligibility, forfeiture, Nomination, Controlling authorities.",
            "Unit V": "The Factories Act 1948 — Chapters dealing with Health, Safety and Welfare of Labour — Child Labour — Rights of child and the Indian Constitution — Child Labour (Prohibition and Regulation) Act 1986 — Equal Remuneration Act, 1976."
        }
    },
    "s4-pil": {
        "name": "Public International Law", "short": "Public International Law", "semester": 4,
        "units": {
            "Unit I": "Definition, Nature, Scope and Importance of International Law — Relation of International Law to Municipal Law — Sources of International Law — Codification.",
            "Unit II": "State Recognition — State Succession — Responsibility of States for International delinquencies — State Territory — Modes of acquiring State Territory.",
            "Unit III": "Position of Individual in International Law — Nationality — Extradition — Asylum — Privileges and Immunities of Diplomatic Envoys — Treaties — Formation of Treaties — Modes of Consent, Reservation and termination.",
            "Unit IV": "The Legal Regime of the Seas — Evolution of the Law of the Sea — Freedoms of the High Seas — Common Heritage of Mankind — UN Convention on the Law of the Seas — Legal Regime of Airspace — Paris, Havana, Warsaw and Chicago Conventions — Five Freedoms of Air — Legal Regime of Outer Space — Outer Space Treaty, Rescue Agreement, Liability Convention, Registration Convention, Moon Treaty — India's space missions.",
            "Unit V": "International Organizations — League of Nations and United Nations — International Court of Justice — International Criminal Court — Specialized agencies of the UN — WHO, UNESCO, ILO, IMF and WTO."
        }
    },
    "s4-interpretation": {
        "name": "Interpretation of Statutes", "short": "Interpretation of Statutes", "semester": 4,
        "units": {
            "Unit I": "Meaning and Definition of Statutes — Classification of Statutes — Meaning and Definition of Interpretation — General Principles of Interpretation — Rules of Construction under the General Clauses Act, 1897.",
            "Unit II": "Grammatical Rule of Interpretation — Golden Rule of Interpretation — Rule of Interpretation to avoid mischief — 60th and 183rd Reports of Law Commission of India on the General Clauses Act.",
            "Unit III": "Interpretation of Penal Statutes and Statutes of Taxation — Beneficial Construction — Construction to avoid conflict with other provisions — Doctrine of Harmonious Construction.",
            "Unit IV": "External Aids to Interpretation — Statement of objects, Legislative debates, identification of legislative purpose — Internal Aids — Preamble, title, interpretation clause, marginal notes, explanations — Presumptions.",
            "Unit V": "Effect of Repeal — Effect of amendments to statutes — Conflict between parent legislation and subordinate legislation — Methods of interpreting substantive and procedural laws."
        }
    },
    "s4-landlaws": {
        "name": "Land Laws", "short": "Land Laws", "semester": 4,
        "units": {
            "Unit I": "Classification of lands — Ownership of Land — Absolute and limited ownership (tenancy, lease etc.) — Doctrine of Eminent Domain — Doctrine of Escheat — Doctrine of Bona Vacantia — Maintenance of land records, Pattas and Title Deeds — Telangana Rights in Land and Pattadar Pass Books Act 2020 — Land Titling (Torrens System): Title Guarantee, Conclusive Title, Title Insurance.",
            "Unit II": "Law Reforms before and after independence — Zamindari Settlement — Ryotwari Settlement — Mahalwari System — Intermediaries — Constitutional Provisions — Abolition of Zamindaries, Jagirs and Inams — Tenancy Laws — Conferment of ownership on tenants/ryots.",
            "Unit III": "Laws relating to acquisition of property — Right to Fair Compensation and Transparency in Land Acquisition, Rehabilitation and Resettlement Act, 2013 — Procedure for Land Acquisition: notification, Social Impact Assessment, consent of land owners, award enquiry, payment of compensation, reference to civil courts.",
            "Unit IV": "Laws relating to Ceiling on Land Holdings — Telangana Land Reforms (Ceiling on Agricultural Holdings) Act, 1973 — Effect of inclusion in the IX Schedule — Interpretation of Directive Principles in relation to land (Articles 38, 39) — Land survey and sub-division — Land Rights under the Scheduled Tribes and Other Traditional Forest Dwellers (Recognition of Forest Rights) Act, 2006.",
            "Unit V": "Laws relating to alienation — Scheduled Areas Land Transfer Regulation — Telangana Assigned Lands (Prohibition of Transfers) Act 1977 — Resumption of Lands to the Transferor/Government — Role of Special Tribunals and Courts in Resolution of land disputes."
        }
    },
    "s4-ipr": {
        "name": "Intellectual Property Law", "short": "IP Law", "semester": 4,
        "units": {
            "Unit I": "Intellectual Property — Meaning, Nature and Classification — Significance and need of protection — Main forms: Patents, Trademarks, Industrial designs, Geographical Indications, Copyright and Neighbouring Rights — New forms: Plant Varieties Protection and Biotechnology, GRTK, Layout Designs, Computer Programmes, Artificial Intelligence and Intellectual Property.",
            "Unit II": "Evolution of International Protection of IPRs — Paris Convention 1883 — WCT 1996 — Berne Convention 1886 — Madrid Agreement 1891 and Protocol 1989 — Patent Co-operation Treaty 1970 — WIPO Conventions — TRIPS Agreement 1994 and its impact.",
            "Unit III": "Copyright: Meaning, Nature, historical evolution — Copyright Act, 1957 — Salient Features — Idea-Expression Dichotomy — Subject matter of Copyright Protection — Neighboring rights — Ownership, Rights of Authors and owners — Assignment — Collective management — Infringement and Criteria — Exceptions — Doctrine of Fair Use — Remedies for infringement.",
            "Unit IV": "Trademarks and rationale of protection — Trade Marks Act, 1999 — Definition, kinds — Trademarks and Internet Domain Names — Registration, Rights of trademark owners — Passing off — Infringement, Remedies — Industrial designs — Designs Act, 2000 — Definition, characteristics, Registration, rights of design holders — Copyright in design — Remedies for infringement.",
            "Unit V": "Patents — Concept — Historical overview of Patent Law in India — Patents Act, 1970 — Patentable Inventions — Kinds of Patents — Procedure for obtaining patent in India and abroad — PCT procedure — Rights and obligations of a patentee — Limitations: compulsory licensing, government acquisition, secrecy directions — Infringement and remedies."
        }
    },

    # ---------------- SEMESTER VI ----------------
    "s6-taxlaw": {
        "name": "Law of Taxation", "short": "Taxation Law", "semester": 6,
        "units": {
            "Unit I": "Constitutional basis of power of taxation — Article 265 — Basic concept of Income Tax — Outlines of Income Tax Law — Definition of Income and Agricultural Income — Residential Status — Previous Year — Assessment Year — Computation of Income.",
            "Unit II": "Heads of Income and Computation — Income from Salary, Income from House Property, Profits and Gains of Business or Profession, Capital Gains, Income from other sources — Taxation Law (Amendment) Act 2019.",
            "Unit III": "Law and Procedure — PAN — Filing of Returns — Payment of Advance Tax — Deduction of Tax at Source (TDS) — Double Tax Relief — Assessment, Penalties, Prosecution, Appeals and Grievances — Authorities.",
            "Unit IV": "GST Act 2017 — Introduction, Background, Basic Concepts — Kinds of GST: CGST, SGST, IGST — Administration officers — Levy and collection of tax — Scope of supply — Tax liability on composite/mixed supplies — Input tax credit — Eligibility and conditions.",
            "Unit V": "GST Act 2017: Registration — persons liable/not liable — procedure — Returns — furnishing of outward/inward supplies — Payment of tax, interest, penalty — TDS/TCS — Demand and Recovery — Advance Ruling — Appeals and revision — Appellate Tribunal — Offences and penalties."
        }
    },
    "s6-itlaw": {
        "name": "Information Technology Law", "short": "IT Law", "semester": 6,
        "units": {
            "Unit I": "Concept of Information Technology and Cyber Space — Interface of Technology and Law — Jurisdiction in Cyber Space vs traditional sense — Internet Jurisdiction — Indian Context — Enforcement agencies — International position — Cases in Cyber Jurisdiction.",
            "Unit II": "Information Technology Act, 2000 — Aims and Objects — Jurisdiction — Electronic Governance — Legal Recognition of Electronic Records and Evidence — Digital Signature Certificates — Duties of Subscribers — Role of Certifying Authorities — Cyber Regulations Appellate Tribunal — Internet Service Providers and Liability — Powers of Police under the Act.",
            "Unit III": "E-Commerce — UNCITRAL Model Law — Legal aspects of E-Commerce — Digital Signatures — E-taxation, E-banking, online publishing and online credit card payment — Employment Contracts — Sales, Reseller and Distributor Agreements, Non-Disclosure Agreements — Shrink Wrap Contract, Source Code, Escrow Agreements.",
            "Unit IV": "Cyber Law and IPRs — Copyright in Information Technology — Software Copyrights vs Patents debate — Authorship and Assignment — Copyright in Internet, Multimedia — Software Piracy — Patents — European/US/Indian positions on Computer related Patents — Trademarks in Internet — Domain name registration, Domain Name Disputes & WIPO — Databases in IT.",
            "Unit V": "Cyber Crimes — Meaning, Different Kinds — Cyber crimes under BNS, BNSS and BSA 2023 — Cyber crimes under the IT Act 2000 — Hacking, Child Pornography, Cyber Stalking, Denial of Service Attack, Virus Dissemination, Software Piracy, IRC Crime, Credit Card Fraud, Net Extortion, Phishing — Cyber Terrorism — Violation of Privacy — Data Protection and Privacy."
        }
    },
    "s6-optional-women": {
        "name": "Optional: Law Relating to Women", "short": "Optional — Women", "semester": 6,
        "units": {
            "Unit I": "Historical background and status of women in ancient India — Constitutional Provisions and gender justice — Provisions relating to women in Fundamental Rights, Directive Principles and Fundamental Duties — Equal right of women to worship.",
            "Unit II": "Laws relating to marriage, divorce, succession and maintenance under personal laws with emphasis on women — Special Marriage Act — Maintenance under Cr.P.C 1973/BNSS 2023 — NRI Marriages — Live-in relationships — Uniform Civil Code and gender justice — Personal Laws (Amendment) Act 2019.",
            "Unit III": "Special provisions relating to women under BSA 2023 — Offences against women under BNS 2023 — outraging modesty — Acid Attacks, sexual harassment, rape, bigamy, mock/fraudulent marriages, adultery decriminalization, causing miscarriage, insulting women — Impact of New Criminal Laws 2023.",
            "Unit IV": "Socio-Legal position of women — Dowry Prohibition Act 1961 — Medical Termination of Pregnancy Act — misuse of Pre-natal Diagnostic Techniques and Sex selection — Immoral Trafficking law — Domestic Violence law — Sexual Harassment at workplace — Honour Killings.",
            "Unit V": "Position of women under Maternity Benefit Act and other Labour laws — Position under International instruments — CEDAW, International Covenant on Civil and Political Rights, International Covenant on Social Cultural and Economic Rights."
        }
    },
    "s6-optional-humanrights": {
        "name": "Optional: Human Rights Law", "short": "Optional — Human Rights", "semester": 6,
        "units": {
            "Unit I": "Meaning and definition of Human Rights — Evolution of Human Rights — Human Rights and Domestic Jurisdiction — Classification of Human Rights — Third World Perspectives.",
            "Unit II": "Adoption of Human Rights by the UN Charter — UN Commission on Human Rights — Universal Declaration of Human Rights — International Covenants on Human Rights (Civil/Political; Economic/Social/Cultural).",
            "Unit III": "Regional Conventions on Human Rights — European Convention — American Convention — African Charter (Banjul).",
            "Unit IV": "International Conventions — Genocide Convention, Convention against Torture, CEDAW, Child Rights Convention, Convention on Statelessness, Convention against Slavery, Convention on Refugees — International Conference on Human Rights 1968 — World Conference on Human Rights 1993.",
            "Unit V": "Human Rights Protection in India — Human Rights Commissions — Protection of Human Rights Act — National Human Rights Commission (NHRC) — State Human Rights Commissions — Human Rights Courts in Districts."
        }
    },
    "s6-optional-investments": {
        "name": "Optional: Law of Investments and Securities", "short": "Optional — Investments", "semester": 6,
        "units": {
            "Unit I": "Administration of Company Law in relation to issue of prospectus and shares — membership and share capital — Kinds of shares — public issue of shares — procedure for issue — allotment of shares — transfer and transmission of shares.",
            "Unit II": "Debentures — Kinds of Debentures and Charges — Dividend — Inter-Corporate Loans and Investments.",
            "Unit III": "Basic features of the Security Contracts (Regulation) Act, 1956 — Recognition of Stock Exchanges — Regulation of Contracts and option in securities — Listing of securities — Guidelines for listing of shares/debentures.",
            "Unit IV": "Basic features of the Security and Exchange Board of India Act, 1992 — Establishment of SEBI — Functions and Powers of SEBI — Powers of the Central Government under the Act — Guidelines for disclosure — Investor Protection — SEBI Appellate Tribunal — Appeals.",
            "Unit V": "Non-Banking Financial Institutions — Classification and Law Relating to NBFCs — Protection of Depositors Act — Foreign Exchange Management Act."
        }
    },
    "s6-drafting": {
        "name": "Drafting, Pleadings and Conveyancing", "short": "Drafting & Pleadings", "semester": 6,
        "units": {
            "Unit I": "Drafting: Drafting and documentation in civil, criminal and constitutional cases — General Principles of Drafting and relevant Substantive Rules — Distinction between pleadings and conveyancing.",
            "Unit II": "Pleadings: Essentials and drafting — (i) Civil: Plaint, Written Statement, Memo, Interlocutory Application, Original Petition, Affidavit, Execution Petition, Memorandum of Appeal and Revision. (ii) Petition under Article 226 and 32 — Drafting of Writ Petition and PIL Petition. (iii) Criminal: Complaint, Criminal Miscellaneous Petition, Bail Application, Memorandum of Appeal and Revision.",
            "Unit III": "Conveyancing: Essentials and drafting of Sale Deed, Mortgage Deed, Lease Deed, Gift Deed, Promissory Note, Power of Attorney, Will and Trust Deed."
        }
    }
}

LLB5_TOPIC_INDEX_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "topics": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "day": {"type": "INTEGER"},
                    "unit": {"type": "STRING"},
                    "topic": {"type": "STRING"},
                },
                "required": ["day", "unit", "topic"]
            }
        }
    },
    "required": ["topics"]
}

LLB5_LECTURE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "lecture_title": {"type": "STRING"},
        "memory_hook": {"type": "STRING"},
        "concept_explanation": {"type": "STRING"},
        "key_provisions": {"type": "ARRAY", "items": {"type": "STRING"}},
        "case_laws": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "case_name": {"type": "STRING"},
                    "citation": {"type": "STRING"},
                    "facts": {"type": "STRING"},
                    "holding": {"type": "STRING"},
                    "why_it_matters": {"type": "STRING"},
                },
                "required": ["case_name", "facts", "holding", "why_it_matters"]
            }
        },
        "real_world_example": {"type": "STRING"},
        "illustration": {"type": "STRING"},
        "quick_answer": {"type": "STRING"},
        "essay_answer": {"type": "STRING"},
        "exam_angle": {"type": "STRING"},
        "quick_recap": {"type": "ARRAY", "items": {"type": "STRING"}},
        "importance_stars": {"type": "INTEGER"},
        "importance_reason": {"type": "STRING"},
    },
    "required": [
        "lecture_title", "memory_hook", "concept_explanation", "key_provisions", "case_laws",
        "real_world_example", "illustration", "quick_answer", "essay_answer", "exam_angle",
        "quick_recap", "importance_stars", "importance_reason"
    ],
}


def llb5_topics_file(subject):
    return f"llb5-{subject}-topics.json"


def llb5_lectures_file(subject):
    return f"llb5-{subject}-lectures.json"


def build_llb5_topic_index_prompt(subject_name, units):
    units_text = "\n\n".join(f"{u}:\n{content}" for u, content in units.items())
    return f"""You are structuring the official Osmania University LL.B. syllabus for the paper "{subject_name}" into a {LLB5_DAYS}-day daily study plan.

SYLLABUS (exact official units — do not add topics not implied by this text):
{units_text}

Break this syllabus into EXACTLY {LLB5_DAYS} sequential daily topics, in teaching order (Unit I concepts first, through to the final unit). Weight days per unit roughly by how much content that unit actually contains — dense units get more days, short units get fewer — but use all {LLB5_DAYS} days across all units combined.

Each day should be one coherent, teachable topic (not a whole unit, not a single case name) — the size a good lecture covers in one sitting. Cover the syllabus faithfully and completely; do not invent sub-topics the syllabus doesn't contain.

Return a "topics" array of exactly {LLB5_DAYS} objects, each with:
day — integer 1 to {LLB5_DAYS}
unit — which unit this belongs to (e.g. "Unit I")
topic — short topic title (under 12 words), specific enough that a student knows exactly what will be taught that day"""


def build_llb5_lecture_prompt(subject_name, unit, topic):
    return f"""You are an outstanding, memorable Indian law faculty member writing today's self-study lecture for an LL.B. student following the Osmania University syllabus, for the paper "{subject_name}".

TODAY'S TOPIC: "{topic}" (from {unit})

This student may never attend a physical class for this topic — this lecture is their ONLY exposure to it. Write something that actually teaches AND sticks in memory, not a dry summary. Cover it at every level a student might need: quick revision, short-answer exam response, and full essay-length answer.

Generate ALL fields in a single response:

lecture_title — clear, specific title for today's lecture
memory_hook — ONE vivid sentence, analogy, or mnemonic that makes this topic memorable and impossible to confuse with a similar concept — this is the single thing the student should still remember a month from now
concept_explanation — 3-5 paragraphs, plain but precise legal language, building the concept from first principles through to its practical application. Assume no prior knowledge of today's specific topic, but assume general first-year law familiarity.
key_provisions — array of the specific Sections/Articles/provisions relevant to this topic, each as "Section X — one-line description of what it says"
case_laws — array of 2-5 landmark or illustrative cases genuinely relevant to this exact topic. For EACH case give: case_name, citation (only if you're genuinely confident of it, else empty string — never invent a citation), facts (2-3 sentences on what actually happened between the parties), holding (what the court actually decided, precisely), why_it_matters (1-2 sentences on why this case is significant for this topic specifically, e.g. what test/principle it established that's still applied today)
real_world_example — a genuine real-world/contemporary scenario (news-style, not a textbook hypothetical) showing why this topic matters outside an exam hall — something that makes the student go "oh, that's what this is actually for"
illustration — ONE concrete worked hypothetical fact pattern (the kind a professor poses in class) applying the concept step by step, distinct from the real-world example above
quick_answer — a tight, exam-ready SHORT ANSWER version (120-180 words) — what a student should write for a 5-mark short-answer question, hitting only the essential points
essay_answer — a FULL long-form ESSAY ANSWER (500-700 words) structured with a clear introduction, developed body covering all sub-issues with supporting case law woven in, and a conclusion — the depth expected for a 10-15 mark long-answer question. Write it the way a topper's answer sheet would read.
exam_angle — 2-3 sentences on how this topic is typically tested (which question type it favours, common angles examiners take, what distinguishes a good answer from an average one)
quick_recap — array of 4-6 short bullet points for the night before the exam
importance_stars — your own honest rating 1-5 of how likely and how heavily this specific topic is tested relative to the rest of the syllabus (5 = near-certain to appear and central to the paper, 1 = rarely tested standalone)
importance_reason — ONE sentence explaining your star rating

Be thorough and genuinely engaging — vary sentence rhythm, use concrete detail, don't pad with filler. This is the student's only preparation for this topic. Write in clear English."""


@app.route('/api/llb5-build-topics', methods=['GET'])
def llb5_build_topics():
    # One-time (per subject) structuring of the syllabus into a 45-day
    # sequence. Re-running for a subject that already has an index is a
    # no-op unless ?force=1 is passed.
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    subject = request.args.get("subject")
    force = request.args.get("force") == "1"
    if subject not in LLB5_SUBJECTS:
        return jsonify({"ok": False, "error": f"Unknown subject. Valid: {list(LLB5_SUBJECTS.keys())}"}), 400

    result = _llb5_build_one_subject_topics(subject, site_token, gemini_key, force)
    return jsonify(result), (200 if result.get("ok") else 502)


def _llb5_build_one_subject_topics(subject, site_token, gemini_key, force=False):
    try:
        fname = llb5_topics_file(subject)
        try:
            existing, sha = github_get(fname, site_token, timeout=8)
        except Exception:
            existing, sha = None, None

        if existing and existing.get("topics") and not force:
            return {"ok": True, "subject": subject, "skipped": True, "reason": "Topic index already exists."}

        info = LLB5_SUBJECTS[subject]
        prompt = build_llb5_topic_index_prompt(info["name"], info["units"])
        try:
            # 45 structured objects in one response needs real headroom —
            # too low a limit here truncates the JSON mid-array and the
            # whole call fails, which is why nothing was showing up.
            parsed = call_gemini_structured(gemini_key, prompt, LLB5_TOPIC_INDEX_SCHEMA, max_tokens=8000)
        except urllib.error.HTTPError as he:
            body = he.read().decode()
            return {"ok": False, "subject": subject, "error": f"Gemini error {he.code}: {body[:300]}"}
        except Exception as ge:
            return {"ok": False, "subject": subject, "error": f"Generation/parse error: {str(ge)[:300]}"}

        topics = parsed.get("topics", []) if parsed else []
        if len(topics) < 10:
            return {"ok": False, "subject": subject, "error": f"Generation produced only {len(topics)} topics, not saving."}

        topics.sort(key=lambda t: t.get("day", 0))
        data = {
            "subject": subject,
            "subject_name": info["name"],
            "start_date": LLB5_START_DATE,
            "total_days": len(topics),
            "topics": topics,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        github_put(fname, site_token, data, sha, f"Build LLB5 topic index: {subject}", timeout=20)
        return {"ok": True, "subject": subject, "topic_count": len(topics)}
    except Exception as e:
        return {"ok": False, "subject": subject, "error": str(e)}


@app.route('/api/llb5-build-all-topics', methods=['GET'])
def llb5_build_all_topics():
    # ONE click builds the topic index for every subject across every
    # semester that doesn't have one yet. Existing subjects with an index
    # already are skipped automatically (no ?force=1 needed) — safe to
    # re-run any time new subjects are added to LLB5_SUBJECTS.
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    results = []
    for subject in LLB5_SUBJECTS.keys():
        results.append(_llb5_build_one_subject_topics(subject, site_token, gemini_key, force=False))

    return jsonify({
        "ok": all(r.get("ok") for r in results),
        "built": [r["subject"] for r in results if r.get("ok") and not r.get("skipped")],
        "already_had": [r["subject"] for r in results if r.get("ok") and r.get("skipped")],
        "failed": [r for r in results if not r.get("ok")],
    })


def _llb5_generate_one_subject_lecture(subject, site_token, gemini_key):
    # Shared logic for a single subject's "today" lecture — used by both
    # the per-subject endpoint (handy for manual testing/backfill) and the
    # single combined endpoint that refreshes all 5 subjects in one call.
    # Never overwrites an existing day's lecture — each day's content is
    # permanent once generated; only a day that hasn't been written yet
    # gets filled in.
    topics_data, _ = github_get(llb5_topics_file(subject), site_token, timeout=8)
    if not topics_data or not topics_data.get("topics"):
        return {"ok": False, "subject": subject, "error": "No topic index yet — run /api/llb5-build-topics first."}

    start = datetime.fromisoformat(topics_data.get("start_date", LLB5_START_DATE)).date()
    today = datetime.now(timezone.utc).date()
    day_num = (today - start).days + 1

    if day_num < 1:
        return {"ok": True, "subject": subject, "skipped": True, "reason": f"Plan hasn't started yet (starts {start})."}
    if day_num > len(topics_data["topics"]):
        return {"ok": True, "subject": subject, "skipped": True, "reason": "Plan already completed."}

    entry_meta = next((t for t in topics_data["topics"] if t.get("day") == day_num), None)
    if not entry_meta:
        return {"ok": False, "subject": subject, "error": f"No topic defined for day {day_num}."}

    lfname = llb5_lectures_file(subject)
    try:
        lectures, lsha = github_get(lfname, site_token, timeout=8)
        if lectures is None:
            lectures = {"lectures": {}}
    except Exception:
        lectures, lsha = {"lectures": {}}, None

    if str(day_num) in lectures.get("lectures", {}):
        return {"ok": True, "subject": subject, "skipped": True, "reason": f"Day {day_num} already generated."}

    prompt = build_llb5_lecture_prompt(LLB5_SUBJECTS[subject]["name"], entry_meta["unit"], entry_meta["topic"])
    try:
        parsed = call_gemini_structured(gemini_key, prompt, LLB5_LECTURE_SCHEMA, max_tokens=12000)
    except Exception as ge:
        return {"ok": False, "subject": subject, "error": f"Generation error: {str(ge)[:300]}"}

    if not parsed or not parsed.get("concept_explanation"):
        return {"ok": False, "subject": subject, "error": "Generation returned no content."}

    lecture_entry = {
        "day": day_num,
        "date": today.isoformat(),
        "unit": entry_meta["unit"],
        "topic": entry_meta["topic"],
        "lecture_title": parsed["lecture_title"],
        "memory_hook": parsed.get("memory_hook", ""),
        "concept_explanation": parsed["concept_explanation"],
        "key_provisions": parsed.get("key_provisions", []),
        "case_laws": parsed.get("case_laws", []),
        "real_world_example": parsed.get("real_world_example", ""),
        "illustration": parsed.get("illustration", ""),
        "quick_answer": parsed.get("quick_answer", ""),
        "essay_answer": parsed.get("essay_answer", ""),
        "exam_angle": parsed.get("exam_angle", ""),
        "quick_recap": parsed.get("quick_recap", []),
        "importance_stars": parsed.get("importance_stars", 3),
        "importance_reason": parsed.get("importance_reason", ""),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lectures.setdefault("lectures", {})[str(day_num)] = lecture_entry
    try:
        github_put(lfname, site_token, lectures, lsha, f"LLB5 {subject} day {day_num}: {entry_meta['topic'][:50]}", timeout=20)
    except Exception as we:
        return {"ok": False, "subject": subject, "error": f"Save error: {str(we)[:300]}"}

    return {"ok": True, "subject": subject, "day": day_num, "topic": entry_meta["topic"]}


@app.route('/api/llb5-daily-lecture', methods=['GET'])
def llb5_daily_lecture():
    # Single-subject version — useful for manual testing/backfilling one
    # subject. For the actual daily cron, use /api/llb5-daily-lecture-all
    # instead, which refreshes all 5 subjects in one call/one cron job.
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    subject = request.args.get("subject")
    if subject not in LLB5_SUBJECTS:
        return jsonify({"ok": False, "error": f"Unknown subject. Valid: {list(LLB5_SUBJECTS.keys())}"}), 400

    try:
        result = _llb5_generate_one_subject_lecture(subject, site_token, gemini_key)
        return jsonify(result), (200 if result.get("ok") else 502)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/llb5-daily-lecture-all', methods=['GET'])
def llb5_daily_lecture_all():
    # THE cron target — one job, once a day, refreshes all 5 subjects.
    # Each subject is independent: a failure or skip on one never blocks
    # the others, and a day that's already generated is never touched
    # again (so nothing gets "washed out" — today's content, once
    # written, stays exactly as-is; only a not-yet-written day fills in).
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    results = []
    for subject in LLB5_SUBJECTS.keys():
        try:
            results.append(_llb5_generate_one_subject_lecture(subject, site_token, gemini_key))
        except Exception as e:
            results.append({"ok": False, "subject": subject, "error": str(e)})

    return jsonify({
        "ok": all(r.get("ok") for r in results),
        "results": results,
    })


GEMINI_IMAGE_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent"


def call_gemini_image(api_key, art_prompt):
    # Free-tier image model, used ONLY for background art — explicitly
    # told never to render any text or letters, so the AI's well-known
    # weakness (garbled/inaccurate text-in-image) never touches the actual
    # facts on the card. The exact hook/subtext text is always drawn
    # separately and exactly, in render_social_card. Returns None on any
    # failure so the caller can safely fall back to the plain gradient —
    # this is a visual nice-to-have, never something that should be able
    # to break the card entirely.
    full_prompt = (
        art_prompt +
        " IMPORTANT: the image must contain absolutely no text, no letters, "
        "no words, no numbers, no typography of any kind — pure abstract "
        "visual/background art only. Vertical portrait orientation, dark "
        "moody navy and gold color palette, professional and elegant, "
        "suitable as a background behind text that will be added separately."
    )
    payload = json.dumps({
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_IMAGE_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read().decode())
        for part in result["candidates"][0]["content"]["parts"]:
            if "inlineData" in part:
                return base64.b64decode(part["inlineData"]["data"])
        return None
    except Exception:
        return None


def filter_relevant_entries_askai(question, entries, topic=None, prev_question=None, max_entries=10):
    topic_pages = set(TOPIC_PAGE_MAP.get(topic, [])) if topic else set()

    combined_text = question + (" " + prev_question if prev_question else "")
    q_words = {w for w in combined_text.lower().split() if w not in ASKAI_STOPWORDS and len(w) > 2}
    if not q_words:
        if topic_pages:
            topic_entries = [e for e in entries if e["source_page"] in topic_pages]
            return topic_entries[:max_entries] if topic_entries else entries[:max_entries]
        return entries[:max_entries]

    scored = []
    for e in entries:
        text = " ".join([
            e["title"].get("en", ""), e["tag"].get("en", ""), e["body"].get("en", ""),
        ]).lower()
        score = sum(1 for w in q_words if w in text)
        if e["source_page"] in topic_pages:
            score += 5
        scored.append((score, e))

    scored.sort(key=lambda x: x[0], reverse=True)
    relevant = [e for score, e in scored if score > 0][:max_entries]
    return relevant if relevant else entries[:max_entries]


def build_askai_prompt(question, entries, lang, prev_question=None, prev_answer=None, topic_label=None):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    context_blocks = []
    for e in entries:
        title = e["title"].get(lang) or e["title"].get("en", "")
        body = e["body"].get(lang) or e["body"].get("en", "")
        context_blocks.append(f"[Source: {e['source_page']}]\nTitle: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    topic_block = ""
    if topic_label:
        topic_block = f"""
ACTIVE TOPIC: The user selected "{topic_label}" as what they want to discuss. If the new question is clearly and obviously about something else entirely (not a related follow-up, not this topic phrased differently), do NOT answer it — instead reply only with a brief, friendly note that this chat is currently focused on {topic_label}, and ask whether they'd like to switch topics or ask something related to {topic_label} instead. If the question is genuinely related to {topic_label} (even loosely), answer normally.
"""

    conversation_block = ""
    if prev_question and prev_answer:
        conversation_block = f"""
PREVIOUS EXCHANGE (for context — the new question may be a follow-up to this):
Previous question: {prev_question[:300]}
Previous answer: {prev_answer[:600]}
"""

    prompt = f"""You are "Durga Bro" — the AI legal-rights assistant on LawSticker AI, an Indian legal-rights education website. You are named after the site's founder, who is known by that name among his own LLB friends and community, but you are an AI agent, not that person. If the user ever directly asks whether you are a real person, whether you are the actual Durga, or who/what you are, you must clearly and honestly say you are an AI agent modeled to help the way he would, not the real person. Never claim or imply you are human or the actual founder.
{topic_block}{conversation_block}
Answer using the APPROVED CONTENT below wherever it's relevant. For anything the approved content doesn't cover, you may still help using your own general knowledge of Indian law — the line that matters is NOT the topic, it's the type of claim within your answer:

- SPECIFIC CLAIMS (exact numbers, deadlines, fees, compensation amounts, section numbers, filing procedures, forms) must ONLY ever come from the APPROVED CONTENT below. Never invent or infer a specific figure or deadline that isn't stated there.
- GENERAL GUIDANCE (what kind of remedy exists, which body to approach, broad concepts, what a law is generally about) can come from your own knowledge of Indian law when the approved content doesn't cover the topic — this is genuinely useful even without site-verified specifics.

If the new question is a vague follow-up (like "explain more", "tell me more about this"), interpret it in light of the PREVIOUS EXCHANGE above, not as a brand new standalone topic. If there's no previous exchange and the question is too vague to answer on its own, say so honestly rather than guessing at an unrelated topic.

Decide per answer, honestly, which case you're in:
- If your answer relies only on the APPROVED CONTENT (even if you also add general context around it), end with: [Source: page-name] (the exact page name from the content used).
- If any part of your answer draws on your own general knowledge because the approved content didn't cover it, end with exactly: [General Knowledge] instead — and say plainly in the answer that this part isn't from the site's verified content.
- If you're not confident either way, say so honestly and suggest a professional or legal aid clinic. Do not guess at specific figures under any circumstances.

FORMATTING: Structure the answer for readability, not one dense paragraph. Use **bold** for key terms (like the name of a law), and short bullet points (using "-") for lists of options, steps, or remedies where that fits better than prose. Keep it skimmable on a phone screen.

IMPORTANT: Output ONLY the final answer itself. Do not show your classification, reasoning, or any meta-commentary about which case applies — the person should just see a clean, direct answer.

RULES THAT APPLY EITHER WAY:
- Answer in {lang_names.get(lang, "English")}.
- Keep the answer concise and practical — a few sentences or a short list, not an essay.
- Never state a specific number, deadline, or amount that isn't in the approved content, even inside an otherwise general-knowledge answer.

APPROVED CONTENT:
{context}

USER QUESTION: {question}"""
    return prompt


def build_bill_prompt(entries, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    context_blocks = []
    for e in entries:
        if e["source_page"] != "rights-consumer":
            continue
        title = e["title"].get(lang) or e["title"].get("en", "")
        body = e["body"].get(lang) or e["body"].get("en", "")
        context_blocks.append(f"Title: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    prompt = f"""You are looking at an uploaded restaurant/shop bill (photo or document) for a visitor to LawSticker AI, an Indian consumer-rights education website.

Using ONLY the approved consumer-rights content below, check the bill for common issues and explain what you find in plain, practical language:
- Is there a "service charge" line item? If so, note that service charge is optional in India (per CCPA Guidelines 2022) and the customer can ask for it to be removed.
- Do the individual item prices and totals add up correctly? Point out any arithmetic mismatch you can actually see in the image.
- Is there anything charged that looks unusual or unclearly labeled?

STRICT RULES:
- Only state legal facts that appear explicitly in the approved content below. Never invent legal information not stated here.
- Only comment on what you can actually see in the image — do not guess at numbers you cannot read clearly.
- Answer in {lang_names.get(lang, "English")}.
- Keep it concise and practical.
- End with: [Source: rights-consumer]

APPROVED CONTENT:
{context}"""
    return prompt


def call_gemini_askai(api_key, prompt, image_base64=None, image_mime_type=None):
    parts = [{"text": prompt}]
    if image_base64:
        parts.append({"inline_data": {"mime_type": image_mime_type or "image/jpeg", "data": image_base64}})
    payload = json.dumps({
        "contents": [{"parts": parts}],
        "generationConfig": {"maxOutputTokens": 550},
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode())
    try:
        return result["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return None


@app.route('/api/ask-ai', methods=['POST'])
def ask_ai():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        body = request.get_json(force=True, silent=True) or {}
        question = (body.get("question") or "").strip()[:MAX_QUESTION_LEN]
        lang = body.get("lang", "en")
        if lang not in ("en", "te", "hi"):
            lang = "en"
        image_base64 = body.get("image_base64")
        image_mime_type = body.get("image_mime_type")
        topic = body.get("topic")
        prev_question = (body.get("previous_question") or "").strip()[:300] or None
        prev_answer = (body.get("previous_answer") or "").strip()[:600] or None

        if not question and not image_base64:
            return jsonify({"ok": False, "error": "No question or image provided."}), 400

        if topic == "scam_verify" and question:
            try:
                public_data, _ = github_get(PUBLIC_SCAM_FILE, site_token, timeout=5)
                scam_entries = (public_data or {}).get("entries", [])
            except Exception:
                scam_entries = []
            known_patterns_summary = summarize_known_scam_patterns(scam_entries)
            verify_prompt = build_scam_verify_prompt(question, known_patterns_summary, lang)
            try:
                verify_result = call_gemini_structured(gemini_key, verify_prompt, SCAM_VERIFY_SCHEMA, max_tokens=1200)
            except urllib.error.HTTPError as e:
                error_body = e.read().decode()
                if e.code == 429:
                    return jsonify({"ok": False, "error": f"BUSY_RIGHT_NOW: {error_body[:200]}"})
                return jsonify({"ok": False, "error": f"AI service error: {error_body[:300]}"})
            except Exception:
                verify_result = None

            if not verify_result:
                return jsonify({"ok": False, "error": "AI service returned an unexpected response."})

            return jsonify({"ok": True, "result_type": "scam_verify", "result": verify_result})

        kb, _ = github_get(KB_FILE, site_token, timeout=5)
        entries = (kb or {}).get("entries", [])

        if image_base64:
            prompt = build_bill_prompt(entries, lang)
        else:
            relevant_entries = filter_relevant_entries_askai(question, entries, topic, prev_question)
            prompt = build_askai_prompt(question, relevant_entries, lang, prev_question, prev_answer, TOPIC_LABELS.get(topic))

        try:
            answer = call_gemini_askai(gemini_key, prompt, image_base64, image_mime_type)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            if e.code == 429:
                return jsonify({"ok": False, "error": f"BUSY_RIGHT_NOW: {error_body[:200]}"})
            return jsonify({"ok": False, "error": f"AI service error: {error_body[:300]}"})

        if answer is None:
            return jsonify({"ok": False, "error": "AI service returned an unexpected response."})

        return jsonify({"ok": True, "result_type": "text", "answer": answer})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# 7. Scam-Ed — structured scam report submission
# ---------------------------------------------------------------------------

PENDING_FILE = "scam-reports-pending.json"
MAX_STORY_LEN = 2000

SCAMED_CATEGORIES = ["Phone Scam", "Online Shopping", "Investment", "Job Offer",
              "Loan/Financial", "Digital/Cyber", "Pyramid Scheme/MLM", "Other"]

SCAMED_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": SCAMED_CATEGORIES},
        "title": {"type": "string", "description": "5-8 word anonymized title describing the pattern, not the person"},
        "anonymized_story": {"type": "string", "description": "Story rewritten with all names, businesses, phone numbers, and locations removed"},
        "remedy_advice": {"type": "string", "description": "Practical legal remedy advice for the user"},
    },
    "required": ["category", "title", "anonymized_story", "remedy_advice"],
}


def compose_narrative(fields):
    parts = []
    if fields.get("contact_method"):
        parts.append(f"Contacted via: {fields['contact_method']}.")
    if fields.get("offer_claim"):
        parts.append(f"They were offering/claiming: {fields['offer_claim']}.")
    if fields.get("ask_action"):
        parts.append(f"They asked the user to: {fields['ask_action']}.")
    if fields.get("suspicion_trigger"):
        parts.append(f"What first raised suspicion: {fields['suspicion_trigger']}.")
    cost_items = fields.get("cost_items") or []
    if cost_items:
        cost_line = f"What it cost the user: {', '.join(cost_items)}."
        if "Money" in cost_items and fields.get("money_range"):
            cost_line += f" Approximate range: {fields['money_range']}."
        parts.append(cost_line)
    if fields.get("extra_details"):
        parts.append(f"Additional details: {fields['extra_details']}.")
    return " ".join(parts)


def build_scamed_prompt(story, entries, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    context_blocks = []
    for e in entries:
        title = e["title"].get(lang) or e["title"].get("en", "")
        body = e["body"].get(lang) or e["body"].get("en", "")
        context_blocks.append(f"[Source: {e['source_page']}]\nTitle: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    prompt = f"""You are processing a scam-experience submission for LawSticker AI's "Scam-Ed" feature — a community scam-awareness archive. The submission below comes from a structured form (not free text), so treat each piece as a factual answer to a specific question.

Analyze the user's submission below and produce:
- category: pick the single best-fitting category
- title: a short anonymized title describing the SCAM PATTERN, not the person or business
- anonymized_story: a clear, specific 2-4 sentence narrative synthesizing the submission below — describe the actual technique used (how contact happened, what was offered, what was asked for, what gave it away) with ALL names, business names, phone numbers, email addresses, and specific locations removed. This should read as a genuinely informative account of the scam pattern, not a vague summary.
- remedy_advice: practical advice for the user, answered in {lang_names.get(lang, "English")}

For remedy_advice specifically:
- Prefer the APPROVED CONTENT below wherever relevant. Specific claims (exact numbers, deadlines, fees, section numbers) must ONLY come from the APPROVED CONTENT.
- If the approved content doesn't cover it, general guidance from your own knowledge of Indian law is fine, but say plainly this part isn't from the site's verified content.
- Keep it concise and practical.
- Use simple, everyday language a common person can easily understand — avoid formal or overly technical wording.
- If a genuinely relevant national helpline exists (fraud/cybercrime: 1930, NALSA legal aid: 15100), mention it.
- If the submission doesn't actually describe a scam (sounds like a personal dispute, refund disagreement, etc.), say so honestly rather than forcing a categorization.
- Some victims lose things other than money — time, trust, emotional wellbeing, safety. If the "cost" mentions any of these, acknowledge it genuinely rather than only addressing financial loss.

APPROVED CONTENT:
{context}

USER'S SUBMISSION:
{story}"""
    return prompt


@app.route('/api/scam-ed', methods=['POST'])
def scam_ed():
    # Public reader submission retired — Scam Stories is now fully
    # AI-sourced (grounded + published by /api/daily-scam-ed). Route kept,
    # neutered, rather than removed, so nothing 404s if anything old
    # still points here.
    return jsonify({
        "ok": False,
        "error": "Reader scam reporting has been retired. Scam Stories is now sourced automatically by AI each day — see lawsticker-ai.com/scam-stories.html"
    }), 410

    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        body = request.get_json(force=True, silent=True) or {}
        lang = body.get("lang", "en")
        if lang not in ("en", "te", "hi"):
            lang = "en"

        fields = {
            "contact_method": (body.get("contact_method") or "").strip()[:100],
            "offer_claim": (body.get("offer_claim") or "").strip()[:200],
            "ask_action": (body.get("ask_action") or "").strip()[:100],
            "suspicion_trigger": (body.get("suspicion_trigger") or "").strip()[:300],
            "cost_items": [str(c).strip()[:60] for c in (body.get("cost_items") or [])][:10],
            "money_range": (body.get("money_range") or "").strip()[:50],
            "extra_details": (body.get("extra_details") or "").strip()[:800],
        }
        story = compose_narrative(fields)[:MAX_STORY_LEN]

        if not story:
            return jsonify({"ok": False, "error": "No details provided."}), 400

        kb, _ = github_get(KB_FILE, site_token, timeout=5)
        entries = (kb or {}).get("entries", [])
        relevant_entries = filter_relevant_entries_scamed(story, entries)
        prompt = build_scamed_prompt(story, relevant_entries, lang)

        try:
            parsed = call_gemini_structured(gemini_key, prompt, SCAMED_RESPONSE_SCHEMA)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            if e.code == 429:
                return jsonify({"ok": False, "error": "BUSY_RIGHT_NOW"})
            return jsonify({"ok": False, "error": f"AI service error: {error_body[:200]}"})

        if not parsed or not parsed.get("remedy_advice"):
            return jsonify({"ok": False, "error": "AI service returned an unexpected response."})

        internal_id = f"scam-{int(datetime.now(timezone.utc).timestamp())}"
        ref_number = "SE-" + internal_id.split("-")[1][-6:]
        try:
            pending, sha = github_get(PENDING_FILE, site_token, timeout=6)
            if pending is None:
                pending = {"entries": []}
        except Exception:
            pending, sha = {"entries": []}, None
        pending.setdefault("entries", []).append({
            "id": internal_id,
            "ref_number": ref_number,
            "original_story": story,
            "structured_fields": fields,
            "category": parsed["category"],
            "title": parsed["title"],
            "anonymized_story": parsed["anonymized_story"],
            "lang": lang,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "origin": "user_submitted",
            "origin_date": datetime.now(timezone.utc).date().isoformat(),
            "status": "pending",
        })
        pending["entries"] = pending["entries"][-500:]
        github_put(PENDING_FILE, site_token, pending, sha, "New scam submission pending review", timeout=8)

        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if bot_token and chat_id:
            try:
                alert_text = (
                    f"🛡️ <b>New Scam-Ed Report</b> ({ref_number})\n"
                    f"Category: {parsed['category']}\n\n"
                    f"<b>Raw submission (private, not yet public):</b>\n{story[:1500]}\n\n"
                    f"<b>AI-generated title:</b> {parsed['title']}\n"
                    f"<b>AI-generated anonymized version:</b>\n{parsed['anonymized_story'][:800]}\n\n"
                    f"Tap below to approve or reject directly. For takedown/restore, use the moderator page."
                )
                # Send to each configured chat with buttons attached — the
                # callback later tells us exactly which chat and message
                # to edit, and which Telegram user tapped it.
                for cid in [c.strip() for c in chat_id.split(",") if c.strip()]:
                    try:
                        send_telegram_with_buttons(bot_token, cid, alert_text, internal_id)
                    except Exception:
                        pass
            except Exception:
                pass

        under_review_note = {
            "en": f"\n\n📋 Your report is saved under reference {ref_number}. It's under review and will appear on the Scam Stories page once approved — no need to resubmit.",
            "te": f"\n\n📋 మీ నివేదిక {ref_number} రిఫరెన్స్‌తో సేవ్ చేయబడింది. ఇది సమీక్షలో ఉంది, ఆమోదించిన తర్వాత Scam Stories పేజీలో కనిపిస్తుంది — మళ్లీ సమర్పించాల్సిన అవసరం లేదు.",
            "hi": f"\n\n📋 आपकी रिपोर्ट संदर्भ {ref_number} के तहत सहेजी गई है। यह समीक्षा में है और स्वीकृत होने पर Scam Stories पेज पर दिखाई देगी — दोबारा सबमिट करने की आवश्यकता नहीं है।",
        }
        answer_with_ref = parsed["remedy_advice"] + under_review_note.get(lang, under_review_note["en"])

        return jsonify({"ok": True, "answer": answer_with_ref, "ref_number": ref_number})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def filter_relevant_entries_scamed(text, entries, max_entries=8):
    q_words = {w for w in text.lower().split() if w not in ASKAI_STOPWORDS and len(w) > 2}
    if not q_words:
        return entries[:max_entries]
    scored = []
    for e in entries:
        body = " ".join([e["title"].get("en", ""), e["tag"].get("en", ""), e["body"].get("en", "")]).lower()
        score = sum(1 for w in q_words if w in body)
        scored.append((score, e))
    scored.sort(key=lambda x: x[0], reverse=True)
    relevant = [e for score, e in scored if score > 0][:max_entries]
    return relevant if relevant else entries[:max_entries]


# ---------------------------------------------------------------------------
# 8. Scam Moderate — approve/reject/takedown/restore, password-gated
# ---------------------------------------------------------------------------

PUBLIC_FILE = "scam-reports.json"
ARCHIVE_FILE = "scam-reports-archived.json"

_YOUTUBE_ID_RE = re.compile(
    r'(?:youtube\.com/(?:watch\?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{11})'
)


def extract_youtube_id(url):
    # Accepts full watch URLs, youtu.be short links, and Shorts links.
    # Returns just the 11-char video ID, or None if it doesn't look like YouTube.
    if not url:
        return None
    m = _YOUTUBE_ID_RE.search(url.strip())
    return m.group(1) if m else None

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "scam_type_label":    {"type": "string", "description": "Short, well-known name for this scam pattern type, e.g. 'Pyramid Scheme / MLM Fraud'"},
        "modus_operandi_en":  {"type": "string", "description": "2-3 sentences in English on how this scam type generically operates"},
        "modus_operandi_te":  {"type": "string", "description": "Same content in Telugu — proper sentences in Telugu script, not transliteration"},
        "modus_operandi_hi":  {"type": "string", "description": "Same content in Hindi — proper sentences in Devanagari script, not transliteration"},
        "red_flags_en":       {"type": "array", "items": {"type": "string"}, "description": "3-5 short practical warning signs in English"},
        "red_flags_te":       {"type": "array", "items": {"type": "string"}, "description": "Same warning signs in Telugu script"},
        "red_flags_hi":       {"type": "array", "items": {"type": "string"}, "description": "Same warning signs in Hindi/Devanagari script"},
        "relevant_laws":      {"type": "array", "items": {"type": "string"}, "description": "Names of well-established Indian Acts/laws — ONLY Act names, never case citations"},
        "prevalence_note":    {"type": "string", "description": "One honest, qualitative sentence on how common this pattern is — no invented statistics"},
        "supportive_note":    {"type": "string", "description": "A brief, warm, reassuring message for both the person who experienced this and future readers"},
    },
    "required": [
        "scam_type_label",
        "modus_operandi_en", "modus_operandi_te", "modus_operandi_hi",
        "red_flags_en", "red_flags_te", "red_flags_hi",
        "relevant_laws", "prevalence_note", "supportive_note",
    ],
}


def call_gemini_enrichment(api_key, category, anonymized_story, lang):
    prompt = f"""You are enriching an approved, anonymized scam report for LawSticker AI's public "Scam Stories & Remedies" education page — a real person will read this to learn and feel supported.

Category: {category}
Story: {anonymized_story}

Produce ALL fields in ALL THREE languages in a single response:

- scam_type_label: the well-known name for this pattern in English (e.g. "Pyramid Scheme / MLM Fraud")
- modus_operandi_en / modus_operandi_te / modus_operandi_hi: 2-3 sentences on how this scam type typically operates — genuine translations in proper Telugu script and Devanagari, never transliteration
- red_flags_en / red_flags_te / red_flags_hi: 3-5 concrete, practical warning signs — genuine translations in proper script for each language
- relevant_laws: ONLY the names of well-established Indian Acts/laws (e.g. "Consumer Protection Act 2019", "IT Act 2000"). Law names stay in English. Do NOT cite court cases or rulings.
- prevalence_note: one honest, qualitative sentence on how commonly this pattern is reported — no invented statistics
- supportive_note: warm, genuine reassurance for the person who went through this and future readers

Stay factual and general. Use simple, everyday language. If you're not confident a law applies, omit it rather than guess."""

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 1200,
            "responseMimeType": "application/json",
            "responseSchema": ENRICH_SCHEMA,
        },
    }).encode()
    req = urllib.request.Request(
        f"{GEMINI_URL}?key={api_key}",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        result = json.loads(resp.read().decode())
    try:
        raw_text = result["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(raw_text)
    except (KeyError, IndexError, json.JSONDecodeError):
        return None


def check_moderator_password(provided):
    real_password = os.environ.get("SCAM_MODERATOR_PASSWORD")
    if not real_password:
        return False
    return provided == real_password


def process_scam_decision(report_id, action, site_token):
    # Shared by the web moderator page and the Telegram inline-button
    # webhook — one true implementation of approve/reject, so both entry
    # points can never drift apart or duplicate the double-click-race fix.
    if action not in ("approve", "reject"):
        return {"ok": False, "error": "Unknown action.", "_status": 400}

    pending, pending_sha = github_get(PENDING_FILE, site_token)
    target = None
    for e in (pending or {}).get("entries", []):
        if e.get("id") == report_id:
            target = e
            break
    if not target:
        return {"ok": False, "error": "Report not found.", "_status": 404}

    if target.get("status") != "pending":
        return {"ok": False, "error": f"Already {target.get('status')} — refresh to see current state.", "_status": 409}

    if action == "approve":
        gemini_key = os.environ.get("GEMINI_API_KEY")
        enrichment = None
        if gemini_key:
            try:
                enrichment = call_gemini_enrichment(gemini_key, target["category"], target["anonymized_story"], target.get("lang", "en"))
            except Exception:
                enrichment = None

        try:
            public_data, public_sha = github_get(PUBLIC_FILE, site_token)
            if public_data is None:
                public_data = {"entries": []}
        except Exception:
            public_data, public_sha = {"entries": []}, None
        # Copy every field from pending except raw user data and internal status.
        # This preserves all trilingual fields (title_en/te/hi, story_en/te/hi,
        # remedies.en/te/hi, source_note, topic_label, etc.) whatever the
        # generator wrote — no hardcoded field list that silently drops new fields.
        _private = {"original_story", "structured_fields", "status"}
        public_entry = {k: v for k, v in target.items() if k not in _private}
        public_entry["status"] = "approved"
        public_entry["approved_at"] = datetime.now(timezone.utc).isoformat()

        # Signals block for user-submitted entries (structured_fields → signals)
        src_fields = target.get("structured_fields", {})
        if src_fields:
            public_entry["signals"] = {
                "contact_method": src_fields.get("contact_method", ""),
                "ask_action": src_fields.get("ask_action", ""),
                "cost_items": src_fields.get("cost_items", []),
                "money_range": src_fields.get("money_range", ""),
            }

        if enrichment:
            public_entry["enrichment"] = enrichment
        public_data.setdefault("entries", []).append(public_entry)
        github_put(PUBLIC_FILE, site_token, public_data, public_sha, "Approve scam report")
        target["status"] = "approved"

    else:
        target["status"] = "rejected"

    github_put(PENDING_FILE, site_token, pending, pending_sha, f"Scam report {action}")
    return {"ok": True}


@app.route('/api/scam-moderate', methods=['POST'])
def scam_moderate():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    if not site_token:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        body = request.get_json(force=True, silent=True) or {}
        password = body.get("password", "")
        action = body.get("action")

        if not check_moderator_password(password):
            return jsonify({"ok": False, "error": "Incorrect or unset moderator password."}), 401

        if action == "list":
            try:
                pending, _ = github_get(PENDING_FILE, site_token)
                if pending is None:
                    pending = {"entries": []}
            except Exception:
                pending = {"entries": []}
            items = [e for e in pending.get("entries", []) if e.get("status") == "pending"]
            return jsonify({"ok": True, "items": items})

        if action == "list_published":
            try:
                public_data, _ = github_get(PUBLIC_FILE, site_token)
                if public_data is None:
                    public_data = {"entries": []}
            except Exception:
                public_data = {"entries": []}
            return jsonify({"ok": True, "items": public_data.get("entries", [])})

        if action == "list_archived":
            try:
                archive, _ = github_get(ARCHIVE_FILE, site_token)
                if archive is None:
                    archive = {"entries": []}
            except Exception:
                archive = {"entries": []}
            return jsonify({"ok": True, "items": archive.get("entries", [])})

        report_id = body.get("id")
        if not report_id:
            return jsonify({"ok": False, "error": "No report id provided."}), 400

        if action == "takedown":
            public_data, public_sha = github_get(PUBLIC_FILE, site_token)
            entry = None
            remaining = []
            for e in (public_data or {}).get("entries", []):
                if e.get("id") == report_id:
                    entry = e
                else:
                    remaining.append(e)
            if not entry:
                return jsonify({"ok": False, "error": "Published story not found."}), 404
            entry["taken_down_at"] = datetime.now(timezone.utc).isoformat()
            public_data["entries"] = remaining
            github_put(PUBLIC_FILE, site_token, public_data, public_sha, "Take down scam story")

            try:
                archive, archive_sha = github_get(ARCHIVE_FILE, site_token)
                if archive is None:
                    archive = {"entries": []}
            except Exception:
                archive, archive_sha = {"entries": []}, None
            archive.setdefault("entries", []).append(entry)
            github_put(ARCHIVE_FILE, site_token, archive, archive_sha, "Archive taken-down scam story")
            return jsonify({"ok": True})

        if action == "restore":
            archive, archive_sha = github_get(ARCHIVE_FILE, site_token)
            entry = None
            remaining = []
            for e in (archive or {}).get("entries", []):
                if e.get("id") == report_id:
                    entry = e
                else:
                    remaining.append(e)
            if not entry:
                return jsonify({"ok": False, "error": "Archived story not found."}), 404
            entry.pop("taken_down_at", None)
            archive["entries"] = remaining
            github_put(ARCHIVE_FILE, site_token, archive, archive_sha, "Restore scam story from archive")

            try:
                public_data, public_sha = github_get(PUBLIC_FILE, site_token)
                if public_data is None:
                    public_data = {"entries": []}
            except Exception:
                public_data, public_sha = {"entries": []}, None
            public_data.setdefault("entries", []).append(entry)
            github_put(PUBLIC_FILE, site_token, public_data, public_sha, "Restore scam story to public")
            return jsonify({"ok": True})

        if action == "set_video_url":
            raw_url = (body.get("video_url") or "").strip()
            video_id = extract_youtube_id(raw_url) if raw_url else None
            if raw_url and not video_id:
                return jsonify({"ok": False, "error": "That doesn't look like a valid YouTube link."}), 400

            public_data, public_sha = github_get(PUBLIC_FILE, site_token)
            entry = None
            for e in (public_data or {}).get("entries", []):
                if e.get("id") == report_id:
                    entry = e
                    break
            if not entry:
                return jsonify({"ok": False, "error": "Published story not found."}), 404

            if video_id:
                entry["video_url"] = raw_url
                entry["video_id"] = video_id
            else:
                # Empty submission clears an existing video link
                entry.pop("video_url", None)
                entry.pop("video_id", None)
            github_put(PUBLIC_FILE, site_token, public_data, public_sha, "Set scam story video URL")
            return jsonify({"ok": True})

        result = process_scam_decision(report_id, action, site_token)
        return jsonify(result), (200 if result.get("ok") else result.get("_status", 400))

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/telegram-webhook', methods=['POST'])
def telegram_webhook():
    # Telegram calls this automatically whenever someone taps an inline
    # button on a message this bot sent. Restricted to the configured
    # channel(s) only — anyone else tapping (which shouldn't be possible
    # in a closed channel, but checked anyway) is silently ignored.
    site_token = os.environ.get("SITE_REPO_TOKEN")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    allowed_chat_ids = {c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()}

    try:
        update = request.get_json(force=True, silent=True) or {}
        cq = update.get("callback_query")
        if not cq:
            return jsonify({"ok": True})  # not a button tap — nothing to do, still 200 so Telegram doesn't retry

        chat_id = str(cq.get("message", {}).get("chat", {}).get("id", ""))
        message_id = cq.get("message", {}).get("message_id")
        callback_id = cq.get("id")
        data = cq.get("data", "")

        if chat_id not in allowed_chat_ids:
            if bot_token and callback_id:
                answer_callback_query(bot_token, callback_id, "Not authorized.")
            return jsonify({"ok": True})

        if ":" not in data:
            return jsonify({"ok": True})
        action, report_id = data.split(":", 1)

        result = process_scam_decision(report_id, action, site_token)

        if bot_token and callback_id:
            feedback = "✅ Approved" if (result.get("ok") and action == "approve") else \
                       "❌ Rejected" if (result.get("ok") and action == "reject") else \
                       result.get("error", "Failed")
            try:
                answer_callback_query(bot_token, callback_id, feedback)
            except Exception:
                pass
            if result.get("ok") and message_id:
                try:
                    original_text = cq.get("message", {}).get("text", "")
                    tapper = cq.get("from", {}).get("first_name", "someone")
                    edit_telegram_message(bot_token, chat_id, message_id,
                                           original_text + f"\n\n— {feedback} by {tapper}")
                except Exception:
                    pass

        return jsonify({"ok": True})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



# ---------------------------------------------------------------------------
# 9. Daily Digest — petrol/diesel/gold/silver + top headlines to Telegram
# ---------------------------------------------------------------------------

DAILYDIGEST_CONFIG_FILE = "site-config.json"
DAILYDIGEST_STATE_FILE = "daily-digest-state.json"
PETROL_URL = "https://www.goodreturns.in/petrol-price-in-hyderabad.html"
DIESEL_URL = "https://www.goodreturns.in/diesel-price-in-hyderabad.html"


def fetch_text_digest(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerDigest/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&#8377;", "₹").replace("&#x20b9;", "₹").replace("&rupee;", "₹")
    text = re.sub(r"&nbsp;|&amp;|&quot;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_fuel_price(text, fuel_word):
    patterns = [
        rf"{fuel_word} price in Hyderabad (?:is at|stands at) (?:₹|Rs\.?)\s*([\d.]+)",
        rf"{fuel_word} price.{{0,30}}?Hyderabad.{{0,30}}?(?:₹|Rs\.?)\s*([\d.]+)",
        rf"(?:₹|Rs\.?)\s*([\d.]+)\s*per litre",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def arrow_digest(current, previous):
    if previous is None:
        return ""
    diff = round(current - previous, 2)
    if diff > 0:
        return f" 🔺 +₹{diff}"
    elif diff < 0:
        return f" 🔻 -₹{abs(diff)}"
    return " ➖ no change"


def escape_html_digest(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_digest_message(petrol, diesel, gold, silver, prev, news_categories):
    today = datetime.now(timezone.utc).strftime("%d %B %Y")
    lines = []
    lines.append(f"📊 <b>Today's Rates — {today}</b>")
    lines.append("")
    lines.append(f"⛽ <b>Petrol</b> (Hyderabad): ₹{petrol}/L{arrow_digest(petrol, prev.get('petrol'))}")
    lines.append(f"🛢️ <b>Diesel</b> (Hyderabad): ₹{diesel}/L{arrow_digest(diesel, prev.get('diesel'))}")
    lines.append("")
    lines.append(f"🥇 <b>Gold</b> (24K): ₹{gold}/gram{arrow_digest(gold, prev.get('gold'))}")
    lines.append(f"🥈 <b>Silver</b> (999): ₹{silver}/gram{arrow_digest(silver, prev.get('silver'))}")

    news_labels = [
        ("legal", "⚖️ Legal"),
        ("regional", "📍 Regional"),
        ("national", "🇮🇳 National"),
        ("international", "🌍 World"),
    ]
    headline_lines = []
    for key, label in news_labels:
        articles = (news_categories or {}).get(key) or []
        if articles:
            top = articles[0]
            title = escape_html_digest(top.get("title", ""))
            link = top.get("link", "")
            headline_lines.append(f'{label}: <a href="{link}">{title}</a>')

    if headline_lines:
        lines.append("")
        lines.append("📰 <b>Today's Headlines</b>")
        lines.extend(headline_lines)

    lines.append("")
    lines.append("🔗 More news: lawsticker-ai.com/news.html")
    lines.append("🔗 More tools: lawsticker-ai.com/calculators.html")
    return "\n".join(lines)


@app.route('/api/daily-digest', methods=['GET'])
def daily_digest():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not site_token or not bot_token or not chat_id:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        petrol_text = fetch_text_digest(PETROL_URL)
        diesel_text = fetch_text_digest(DIESEL_URL)
        petrol = extract_fuel_price(petrol_text, "petrol")
        diesel = extract_fuel_price(diesel_text, "diesel")

        if petrol is None or diesel is None:
            return jsonify({"ok": False, "error": "Could not extract fuel prices — source page format may have changed.", "petrol": petrol, "diesel": diesel})

        config, _ = github_get(DAILYDIGEST_CONFIG_FILE, site_token)
        rates = (config or {}).get("rates", {})
        gold = rates.get("gold_24k_per_gram_inr")
        silver = rates.get("silver_999_per_gram_inr")

        if gold is None or silver is None:
            return jsonify({"ok": False, "error": "Gold/silver rates not found in site-config.json."})

        state, sha = github_get(DAILYDIGEST_STATE_FILE, site_token)
        prev = state or {}

        try:
            news_data, _ = github_get(NEWS_FILE, site_token)
            news_categories = (news_data or {}).get("categories", {})
        except Exception:
            news_categories = {}

        message = build_digest_message(petrol, diesel, gold, silver, prev, news_categories)
        results = send_telegram_to_all(bot_token, chat_id, message)
        telegram_sent = all(v == "sent" for v in results.values())

        new_state = {
            "petrol": petrol, "diesel": diesel, "gold": gold, "silver": silver,
            "posted_at": datetime.now(timezone.utc).isoformat(),
        }
        github_put(DAILYDIGEST_STATE_FILE, site_token, new_state, sha, "Daily digest posted")

        return jsonify({"ok": True, "telegram_sent": telegram_sent, "telegram_results": results, "values": new_state})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# 10. Multilingual News Digest — Telugu + Hindi, Regional + National
# ---------------------------------------------------------------------------

I18N_LANGUAGES = {
    "te": {
        "regional": {"q": "Hyderabad OR Telangana", "country": "in", "language": "te"},
        "national": {"country": "in", "language": "te"},
    },
    "hi": {
        "regional": {"q": "Hyderabad OR Telangana", "country": "in", "language": "hi"},
        "national": {"country": "in", "language": "hi"},
    },
}


def fetch_news_i18n(api_key, params):
    q = dict(params)
    q["apikey"] = api_key
    url = NEWSDATA_BASE + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; LawStickerNews/1.0)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def extract_articles_i18n(api_response, limit=6):
    articles = []
    for item in (api_response.get("results") or [])[:limit]:
        articles.append({
            "title": item.get("title", ""),
            "link": item.get("link", ""),
            "source": item.get("source_id", "unknown"),
            "pubDate": item.get("pubDate", ""),
            "image_url": item.get("image_url"),
            "description": (item.get("description") or "")[:180],
            "category": item.get("category") or [],
        })
    return articles


@app.route('/api/news-digest-i18n', methods=['GET'])
def news_digest_i18n():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    newsdata_key = os.environ.get("NEWSDATA_API_KEY")

    if not site_token or not newsdata_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        results = {}
        errors = {}
        for lang, queries in I18N_LANGUAGES.items():
            results[lang] = {}
            for category, params in queries.items():
                key = f"{lang}_{category}"
                try:
                    raw = fetch_news_i18n(newsdata_key, params)
                    if raw.get("status") == "success":
                        results[lang][category] = extract_articles_i18n(raw)
                    else:
                        errors[key] = raw.get("results", {}).get("message", "Unknown API error")
                        results[lang][category] = []
                except urllib.error.HTTPError as e:
                    try:
                        errors[key] = e.read().decode()
                    except Exception:
                        errors[key] = str(e)
                    results[lang][category] = []
                except Exception as e:
                    errors[key] = str(e)
                    results[lang][category] = []

        existing, sha = github_get(NEWS_FILE, site_token)
        output = dict(existing) if existing else {}
        output["updated_at_i18n"] = datetime.now(timezone.utc).isoformat()
        output["categories_te"] = results.get("te", {})
        output["categories_hi"] = results.get("hi", {})

        github_put(NEWS_FILE, site_token, output, sha, "Multilingual news update (te/hi)")

        total = sum(len(v) for lang_data in results.values() for v in lang_data.values())
        return jsonify({
            "ok": True,
            "total_articles": total,
            "counts": {f"{lang}_{cat}": len(arts) for lang, cats in results.items() for cat, arts in cats.items()},
            "errors": errors or None,
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



# ---------------------------------------------------------------------------
# 11. Daily Quiz Generator — cron-triggered ONCE PER DAY only.
# This never runs on a page visit — the homepage just reads the cached
# daily-quiz.json file, exactly like it already reads news-feed.json.
# One Gemini call per day, total, regardless of how many people visit.
# ---------------------------------------------------------------------------

QUIZ_FILE = "daily-quiz.json"
QUIZ_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "A genuinely interesting question testing real understanding, not trivia"},
        "options": {"type": "array", "items": {"type": "string"}, "description": "Exactly 4 answer options"},
        "correct_index": {"type": "integer", "description": "Index (0-3) of the correct option"},
        "explanation": {"type": "string", "description": "2-3 sentences explaining the correct answer, grounded only in the provided content"},
        "source_page": {"type": "string", "description": "Which source_page this question was grounded in"},
    },
    "required": ["question", "options", "correct_index", "explanation", "source_page"],
}


def build_quiz_prompt(entries):
    import random
    sample = random.sample(entries, min(6, len(entries)))
    context_blocks = []
    for e in sample:
        title = e["title"].get("en", "")
        body = e["body"].get("en", "")
        context_blocks.append(f"[Source: {e['source_page']}]\nTitle: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    return f"""You are writing today's quiz question for LawSticker AI's homepage — something that makes someone stop scrolling and think "wait, really?"

Pick ONE of the topics below and write a genuinely interesting multiple-choice question testing real understanding of it — not a trivial date/number lookup, something that reveals a fact people commonly get wrong or don't know.

AVAILABLE CONTENT:
{context}

Requirements:
- question: engaging, specific, makes someone want to know the answer
- options: exactly 4 plausible options, only one correct
- correct_index: 0-3
- explanation: why the correct answer is right, using ONLY the content above — never invent a fact not stated there
- source_page: which [Source: ...] tag the question came from

Stay strictly grounded in the provided content — if you're not confident the content clearly supports a specific answer, pick a different, simpler angle rather than guess."""


@app.route('/api/daily-quiz', methods=['GET'])
def daily_quiz():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        kb, _ = github_get(KB_FILE, site_token, timeout=5)
        entries = (kb or {}).get("entries", [])
        if not entries:
            return jsonify({"ok": False, "error": "Knowledge base is empty."}), 500

        prompt = build_quiz_prompt(entries)
        quiz = call_gemini_structured(gemini_key, prompt, QUIZ_SCHEMA, max_tokens=500)
        if not quiz or len(quiz.get("options", [])) != 4:
            return jsonify({"ok": False, "error": "AI returned an unexpected quiz format."}), 500

        try:
            existing, sha = github_get(QUIZ_FILE, site_token, timeout=5)
        except Exception:
            existing, sha = None, None

        output = {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "quiz": quiz,
        }
        github_put(QUIZ_FILE, site_token, output, sha, "Daily quiz generated", timeout=8)
        return jsonify({"ok": True, "quiz": quiz})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# 12. Daily Social Card — cron-triggered ONCE PER DAY only, same discipline
# as the quiz. Gemini writes a genuinely curiosity-driving hook grounded in
# a real, verified fact (never a false claim — "clickbait framing, honest
# content" is the deliberate line). PIL renders it onto a branded vertical
# card. Delivered to Telegram as a photo, ready to forward to WhatsApp
# Status, Instagram, wherever — no direct posting, human stays in the loop.
# ---------------------------------------------------------------------------

SOCIAL_CARD_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string", "description": "A short, genuinely curiosity-driving headline (under 60 characters) — makes someone stop scrolling. Must be a real, true claim, never exaggerated or misleading."},
        "subtext": {"type": "string", "description": "1-2 sentences of real explanation, grounded only in the provided content"},
        "icon": {"type": "string", "description": "ONE single emoji that genuinely fits the topic (e.g. 🎬 for cinema, 💰 for tax, 🛡️ for scams) — exactly one emoji character, nothing else"},
        "label": {"type": "string", "description": "A short 1-3 word category label in capitals, e.g. 'CINEMA RIGHTS' or 'CONSUMER LAW'"},
        "art_theme": {"type": "string", "description": "A short visual theme description for abstract background art matching the topic's mood (e.g. 'golden scales of justice silhouette, dramatic lighting' or 'cinema film reel and spotlight, moody atmosphere') — describe mood and abstract imagery only, never mention any text or words appearing in the image"},
        "source_page": {"type": "string", "description": "Which [Source: ...] tag this was grounded in"},
    },
    "required": ["hook", "subtext", "icon", "label", "art_theme", "source_page"],
}


def build_social_card_prompt(entries):
    import random
    sample = random.sample(entries, min(6, len(entries)))
    context_blocks = []
    for e in sample:
        title = e["title"].get("en", "")
        body = e["body"].get("en", "")
        context_blocks.append(f"[Source: {e['source_page']}]\nTitle: {title}\nContent: {body}")
    context = "\n\n".join(context_blocks)

    return f"""You are writing today's social media card for LawSticker AI — something genuinely scroll-stopping that makes someone go "wait, really?" and want to know more.

AVAILABLE CONTENT:
{context}

Pick ONE genuinely surprising angle and write:
- hook: under 60 characters, punchy, creates real curiosity — but every word must be something the content below actually supports. Curiosity in FRAMING is the goal; never curiosity through exaggeration or a misleading implication.
- subtext: 1-2 sentences of real explanation, grounded only in the content above
- icon: exactly one emoji that genuinely fits the topic
- label: a short 1-3 word category label in capitals (e.g. "CINEMA RIGHTS", "TAX BASICS")
- art_theme: a short visual mood/imagery description for abstract background art matching this topic (e.g. "golden scales of justice silhouette, dramatic lighting" or "cinema film reel and spotlight beams, moody atmosphere") — imagery and mood only, never mention text or words
- source_page: which [Source: ...] tag this came from

The test: if someone fact-checked this hook against the actual content, it should hold up completely. Surprising and true, not surprising because it's stretched."""


def wrap_lines(draw, text, font, max_width_px):
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textlength(test, font=font) <= max_width_px:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_centered(draw, text, font, cx, y, fill):
    w = draw.textlength(text, font=font)
    draw.text((cx - w / 2, y), text, font=font, fill=fill)


def render_emoji_layer(font_dir, icon, target_size, opacity=1.0):
    # NotoColorEmoji is a fixed-size bitmap font (only renders correctly
    # at its native ~109px) — render small, then resize the IMAGE to the
    # size we actually want, never ask the font itself for a bigger size.
    f = ImageFont.truetype(os.path.join(font_dir, "NotoColorEmoji.ttf"), 109)
    small = Image.new('RGBA', (130, 130), (0, 0, 0, 0))
    d = ImageDraw.Draw(small)
    d.text((65, 65), icon, font=f, anchor="mm", embedded_color=True)
    big = small.resize((target_size, target_size), Image.LANCZOS)
    if opacity < 1.0:
        alpha = big.split()[3].point(lambda p: int(p * opacity))
        big.putalpha(alpha)
    return big


def render_social_card(hook, subtext, label, icon="\U0001F4A1", ai_background_bytes=None, badge_text="DID YOU KNOW?"):
    # Matches rights-shorts.html's actual design system precisely (same
    # badge, watermark, footer band) — the background is now either real
    # Gemini-generated art (hybrid approach) or the original flat gradient
    # as a safe fallback if AI generation wasn't available that day. Either
    # way, every word of text is drawn separately and exactly by PIL —
    # never regenerated by the AI — so the facts on the card are always
    # guaranteed accurate regardless of which background was used.
    W, H = 1080, 1920
    img = Image.new('RGBA', (W, H), (13, 17, 23, 255))
    draw = ImageDraw.Draw(img)

    used_ai_background = False
    if ai_background_bytes:
        try:
            bg = Image.open(io.BytesIO(ai_background_bytes)).convert('RGBA')
            # Cover-fit crop to exactly W x H, same idea as CSS background-size:cover
            bg_ratio = bg.width / bg.height
            target_ratio = W / H
            if bg_ratio > target_ratio:
                new_h = H
                new_w = int(H * bg_ratio)
            else:
                new_w = W
                new_h = int(W / bg_ratio)
            bg = bg.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - W) // 2
            top = (new_h - H) // 2
            bg = bg.crop((left, top, left + W, top + H))
            img.alpha_composite(bg)
            # Dark overlay so white text stays legible over busier AI art —
            # same principle as a movie poster's gradient over a photo.
            overlay = Image.new('RGBA', (W, H), (13, 17, 23, 0))
            odraw = ImageDraw.Draw(overlay)
            for y in range(H):
                t = y / H
                a = int(90 + 90 * abs(t - 0.5) * 2)  # darker at top/bottom, lighter in the middle
                odraw.line([(0, y), (W, y)], fill=(13, 17, 23, a))
            img.alpha_composite(overlay)
            draw = ImageDraw.Draw(img)
            used_ai_background = True
        except Exception:
            used_ai_background = False

    if not used_ai_background:
        stops = [(0x0D, 0x11, 0x17), (0x13, 0x1B, 0x29), (0x0D, 0x11, 0x17)]
        for y in range(H):
            t = y / H
            if t <= 0.5:
                t2 = t / 0.5
                c0, c1 = stops[0], stops[1]
            else:
                t2 = (t - 0.5) / 0.5
                c0, c1 = stops[1], stops[2]
            r = int(c0[0] + (c1[0] - c0[0]) * t2)
            g = int(c0[1] + (c1[1] - c0[1]) * t2)
            b = int(c0[2] + (c1[2] - c0[2]) * t2)
            draw.line([(0, y), (W, y)], fill=(r, g, b))

    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    bold_58 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Bold.ttf"), 58)
    bold_36 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Bold.ttf"), 36)
    bold_32 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Bold.ttf"), 32)
    bold_50 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Bold.ttf"), 50)
    reg_38 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Regular.ttf"), 38)
    reg_32 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Regular.ttf"), 32)
    ACCENT = (0xC9, 0xA2, 0x27)

    draw.rectangle([(0, 0), (W, 16)], fill=ACCENT)

    wm = render_emoji_layer(font_dir, icon, 700, opacity=0.06)
    img.alpha_composite(wm, (int(W / 2 - 350), 1150))
    draw = ImageDraw.Draw(img)

    badge_text = badge_text
    bw = draw.textlength(badge_text, font=bold_36) + 76
    draw.rounded_rectangle([(W / 2 - bw / 2, 300), (W / 2 + bw / 2, 378)], radius=39, fill=ACCENT)
    draw_centered(draw, badge_text, bold_36, W / 2, 320, (13, 17, 23))

    icon_layer = render_emoji_layer(font_dir, icon, 150)
    img.alpha_composite(icon_layer, (int(W / 2 - 75), 500))
    draw = ImageDraw.Draw(img)

    lines = wrap_lines(draw, hook, bold_58, W - 160)
    y = 700
    for line in lines:
        draw_centered(draw, line, bold_58, W / 2, y, (255, 255, 255))
        y += 80
    fact_end = y

    draw.line([(140, fact_end + 40), (W - 140, fact_end + 40)], fill=(201, 162, 39, 150), width=3)

    draw_centered(draw, label, bold_32, W / 2, fact_end + 85, ACCENT)
    detail_lines = wrap_lines(draw, subtext, reg_38, W - 200)
    yy = fact_end + 145
    for line in detail_lines:
        draw_centered(draw, line, reg_38, W / 2, yy, (255, 255, 255, 204))
        yy += 52

    draw.rectangle([(0, H - 300), (W, H)], fill=(28, 25, 15, 255))
    draw_centered(draw, "LawSticker AI", bold_50, W / 2, H - 215, (245, 215, 110))
    draw_centered(draw, "Your rights. Your language. Free.", reg_32, W / 2, H - 155, (255, 255, 255, 180))
    draw_centered(draw, "lawsticker-ai.com", bold_36, W / 2, H - 95, (245, 215, 110))

    icon_footer = render_emoji_layer(font_dir, "\u2696\uFE0F", 56)
    img.alpha_composite(icon_footer, (int(W / 2 - 260), H - 245))

    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG')
    return buf.getvalue()


@app.route('/api/daily-social-card', methods=['GET'])
def daily_social_card():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not site_token or not gemini_key or not bot_token or not chat_id:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        kb, _ = github_get(KB_FILE, site_token, timeout=5)
        entries = (kb or {}).get("entries", [])
        if not entries:
            return jsonify({"ok": False, "error": "Knowledge base is empty."}), 500

        prompt = build_social_card_prompt(entries)
        card_data = call_gemini_structured(gemini_key, prompt, SOCIAL_CARD_SCHEMA, max_tokens=300)
        if not card_data or not card_data.get("hook"):
            return jsonify({"ok": False, "error": "AI returned an unexpected format."}), 500

        # Hybrid approach: AI generates the background art (mood/imagery
        # only, explicitly forbidden from rendering any text), while every
        # word on the card is still drawn exactly by PIL — never
        # regenerated visually — so accuracy is never at risk. If image
        # generation fails for any reason, render_social_card falls back
        # to the plain gradient automatically.
        ai_background = None
        art_theme = card_data.get("art_theme")
        if art_theme:
            ai_background = call_gemini_image(gemini_key, art_theme)

        image_bytes = render_social_card(
            card_data["hook"], card_data["subtext"],
            card_data.get("label", "DURGA BRO"), card_data.get("icon", "💡"),
            ai_background_bytes=ai_background,
        )

        source_page = card_data.get('source_page', '')
        page_url = f"https://lawsticker-ai.com/{source_page}.html" if source_page else "https://lawsticker-ai.com"
        # No caption — the photo goes out exactly as it'll be forwarded to
        # WhatsApp Status / Instagram / wherever, so nothing extra rides
        # along with it. The Read Full Story button stays on this same
        # message for reading inside Telegram.
        results = {}
        for cid in [c.strip() for c in chat_id.split(",") if c.strip()]:
            try:
                send_telegram_photo(bot_token, cid, image_bytes,
                                     button_text="🔗 Read Full Story", button_url=page_url)
                results[cid] = "sent"
            except Exception as e:
                results[cid] = f"failed: {e}"

        return jsonify({"ok": True, "card": card_data, "telegram_results": results})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# 13. Supreme Court Today — daily Gemini Search digest in EN/TE/HI.
#     Two-step: (1) Gemini with Google Search grounding to find today's
#     most people-relevant SC judgment; (2) Gemini structured to format
#     it trilingual. PIL card in English (LiberationSans is Latin-only);
#     all three languages live in sc-digest.json for the sc-today.html page.
# ---------------------------------------------------------------------------

SC_DIGEST_FILE = "sc-digest.json"
SC_SEARCH_MODEL = "gemini-2.0-flash"
SC_SEARCH_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{SC_SEARCH_MODEL}:generateContent"
)

SC_DIGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "object",
            "properties": {
                "en": {"type": "string"},
                "te": {"type": "string"},
                "hi": {"type": "string"},
            },
            "required": ["en", "te", "hi"],
        },
        "means": {
            "type": "object",
            "properties": {
                "en": {"type": "string"},
                "te": {"type": "string"},
                "hi": {"type": "string"},
            },
            "required": ["en", "te", "hi"],
        },
        "action": {
            "type": "object",
            "properties": {
                "en": {"type": "string"},
                "te": {"type": "string"},
                "hi": {"type": "string"},
            },
            "required": ["en", "te", "hi"],
        },
        "case_ref":   {"type": "string"},
        "category":   {"type": "string"},
        "icon":       {"type": "string"},
        "source_info":{"type": "string"},
        "source_url": {"type": "string"},
        "is_recent":  {"type": "boolean"},
    },
    "required": ["headline", "means", "action", "case_ref", "category", "icon"],
}


def call_gemini_search(api_key, prompt):
    # Gemini 2.0 REST API uses camelCase "googleSearch" for the built-in
    # Google Search grounding tool. Fallback to no-tool call if this fails
    # so a bad quota or key issue never takes down the whole endpoint.
    for tool_key in ("googleSearch", "google_search"):
        try:
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "tools": [{tool_key: {}}],
            }).encode()
            req = urllib.request.Request(
                f"{SC_SEARCH_URL}?key={api_key}",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
            parts = result["candidates"][0]["content"]["parts"]
            text = " ".join(p.get("text", "") for p in parts if "text" in p).strip()
            if text:
                return text
        except Exception:
            continue
    return None


def render_sc_card(data, ai_background_bytes=None):
    W, H = 1080, 1920
    img = Image.new('RGBA', (W, H), (13, 17, 23, 255))
    draw = ImageDraw.Draw(img)
    ACCENT = (0xC9, 0xA2, 0x27)

    used_ai = False
    if ai_background_bytes:
        try:
            bg = Image.open(io.BytesIO(ai_background_bytes)).convert('RGBA')
            br = bg.width / bg.height
            tr = W / H
            if br > tr:
                nw, nh = int(H * br), H
            else:
                nw, nh = W, int(W / br)
            bg = bg.resize((nw, nh), Image.LANCZOS)
            bg = bg.crop(((nw - W) // 2, (nh - H) // 2, (nw - W) // 2 + W, (nh - H) // 2 + H))
            img.alpha_composite(bg)
            ov = Image.new('RGBA', (W, H), (13, 17, 23, 0))
            od = ImageDraw.Draw(ov)
            for y in range(H):
                a = int(100 + 80 * abs(y / H - 0.5) * 2)
                od.line([(0, y), (W, y)], fill=(13, 17, 23, a))
            img.alpha_composite(ov)
            draw = ImageDraw.Draw(img)
            used_ai = True
        except Exception:
            pass

    if not used_ai:
        for y in range(H):
            t = y / H
            t2 = t / 0.5 if t <= 0.5 else (t - 0.5) / 0.5
            c0 = (0x0D, 0x11, 0x17) if t <= 0.5 else (0x12, 0x1A, 0x2E)
            c1 = (0x12, 0x1A, 0x2E) if t <= 0.5 else (0x0D, 0x11, 0x17)
            draw.line([(0, y), (W, y)], fill=tuple(int(c0[i] + (c1[i] - c0[i]) * t2) for i in range(3)))

    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    bold_58 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Bold.ttf"), 58)
    bold_50 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Bold.ttf"), 50)
    bold_36 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Bold.ttf"), 36)
    bold_32 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Bold.ttf"), 32)
    reg_38 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Regular.ttf"), 38)
    reg_28 = ImageFont.truetype(os.path.join(font_dir, "LiberationSans-Regular.ttf"), 28)

    icon = data.get("icon", "⚖️")

    draw.rectangle([(0, 0), (W, 16)], fill=ACCENT)

    wm = render_emoji_layer(font_dir, icon, 700, opacity=0.06)
    img.alpha_composite(wm, (int(W / 2 - 350), 1050))
    draw = ImageDraw.Draw(img)

    badge_text = "SC TODAY"
    bw = draw.textlength(badge_text, font=bold_36) + 76
    draw.rounded_rectangle([(W/2 - bw/2, 260), (W/2 + bw/2, 338)], radius=39, fill=ACCENT)
    draw_centered(draw, badge_text, bold_36, W/2, 280, (13, 17, 23))

    icon_layer = render_emoji_layer(font_dir, icon, 130)
    img.alpha_composite(icon_layer, (int(W/2 - 65), 370))
    draw = ImageDraw.Draw(img)

    headline = data.get("headline", {}).get("en", "")
    lines = wrap_lines(draw, headline, bold_58, W - 140)
    y = 540
    for line in lines:
        draw_centered(draw, line, bold_58, W/2, y, (255, 255, 255))
        y += 76
    headline_end = y

    category = data.get("category", "SC RULING")
    draw_centered(draw, category, bold_32, W/2, headline_end + 20, ACCENT)

    div1 = headline_end + 80
    draw.line([(120, div1), (W - 120, div1)], fill=(201, 162, 39, 150), width=2)

    draw_centered(draw, "WHAT IT MEANS", bold_36, W/2, div1 + 28, ACCENT)
    means_lines = wrap_lines(draw, data.get("means", {}).get("en", ""), reg_38, W - 180)
    yy = div1 + 86
    for line in means_lines:
        draw_centered(draw, line, reg_38, W/2, yy, (255, 255, 255, 220))
        yy += 54
    means_end = yy

    div2 = means_end + 28
    draw.line([(120, div2), (W - 120, div2)], fill=(201, 162, 39, 120), width=2)

    draw_centered(draw, "WHAT TO DO", bold_36, W/2, div2 + 28, ACCENT)
    action_lines = wrap_lines(draw, data.get("action", {}).get("en", ""), reg_38, W - 180)
    yyy = div2 + 86
    for line in action_lines:
        draw_centered(draw, line, reg_38, W/2, yyy, (255, 255, 255, 200))
        yyy += 54
    action_end = yyy

    case_ref = data.get("case_ref", "")
    if case_ref:
        draw_centered(draw, case_ref, reg_28, W/2, action_end + 36, (201, 162, 39, 180))

    draw.rectangle([(0, H - 300), (W, H)], fill=(28, 25, 15, 255))
    draw_centered(draw, "LawSticker AI", bold_50, W/2, H - 215, (245, 215, 110))
    draw_centered(draw, "Supreme Court. Plain Language. Free.", reg_38, W/2, H - 155, (255, 255, 255, 180))
    draw_centered(draw, "lawsticker-ai.com/sc-today.html", bold_36, W/2, H - 95, (245, 215, 110))
    icon_footer = render_emoji_layer(font_dir, "⚖️", 56)
    img.alpha_composite(icon_footer, (int(W/2 - 260), H - 245))

    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG')
    return buf.getvalue()


@app.route('/api/daily-sc-digest', methods=['GET'])
def daily_sc_digest():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key  = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Step 1: live Google Search grounding — returns None if unavailable
        search_context = call_gemini_search(gemini_key, (
            f"Today is {today}. Search for the most recent Supreme Court of India "
            "judgment or order from today or this week that directly affects ordinary "
            "citizens — tenants, workers, consumers, patients, students, or families. "
            "Report: (1) exact case name or number, (2) what the court ruled, "
            "(3) which category of people it affects, (4) practical impact on citizens. "
            "Be factual and specific. If nothing from today, use the most impactful "
            "judgment from the past 7 days."
        ))

        # Build the structuring prompt — grounded if search worked, knowledge-based fallback if not
        if search_context:
            source_section = f"RAW SC NEWS (from live Google Search):\n{search_context}"
        else:
            source_section = (
                "Use your most recent training knowledge of Supreme Court of India judgments. "
                "Pick the most recent and impactful ruling that affects ordinary people — "
                "tenants, workers, consumers, patients, students, or families. Be specific "
                "with the case name and ruling, and mark is_recent as false."
            )

        structure_prompt = (
            "Create a Supreme Court of India digest for LawSticker AI — a legal rights "
            "education platform for Telugu, Hindi, and English speakers.\n\n"
            f"{source_section}\n\nToday: {today}\n\n"
            "Format for ordinary citizens with no legal background:\n"
            "- headline.en: what happened, max 80 chars, plain English, no Latin, no section numbers\n"
            "- headline.te: accurate Telugu translation\n"
            "- headline.hi: accurate Hindi translation\n"
            "- means.en: what this ruling means for an ordinary person, 1 sentence\n"
            "- means.te / means.hi: accurate translations\n"
            "- action.en: concrete step someone affected should take, 1 sentence\n"
            "- action.te / action.hi: accurate translations\n"
            "- case_ref: full case name or citation\n"
            "- category: CONSUMER | LABOUR | TENANT | HEALTH | EDUCATION | "
            "ENVIRONMENT | CRIMINAL | PROPERTY | FAMILY | OTHER\n"
            "- icon: single emoji matching the category\n"
            "- source_info: news outlet name, or 'Gemini knowledge base' if no live search\n"
            "- source_url: direct URL to the judgment or news article if you found one via live search — empty string if unknown (never invent a URL)\n"
            "- is_recent: true only if judgment is from this week"
        )
        digest = call_gemini_structured(gemini_key, structure_prompt, SC_DIGEST_SCHEMA, max_tokens=900)
        if not digest or not digest.get("headline"):
            return jsonify({"ok": False, "error": "AI returned unexpected format."}), 500

        entry = {"date": today, **digest}
        if search_context:
            entry["source_text"] = search_context[:3000]
        archive, sha = github_get(SC_DIGEST_FILE, site_token)
        if archive is None:
            archive = {"entries": []}
        entries = [e for e in archive.get("entries", []) if e.get("date") != today]
        entries.insert(0, entry)
        archive = {"last_updated": today, "entries": entries[:30]}
        github_put(SC_DIGEST_FILE, site_token, archive, sha, f"SC digest {today}")

        return jsonify({"ok": True, "digest": digest})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/sc-digest-data', methods=['GET'])
def sc_digest_data():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    if not site_token:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500
    try:
        archive, _ = github_get(SC_DIGEST_FILE, site_token)
        if not archive:
            return jsonify({"ok": True, "entries": [], "last_updated": None})
        return jsonify({
            "ok": True,
            "entries": archive.get("entries", []),
            "last_updated": archive.get("last_updated"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


AI_SCAM_TOPICS = [
    ("Phone Scam",         "Fake KYC expiry call from bank impersonator"),
    ("Phone Scam",         "Fake customs/courier parcel seized scam"),
    ("Phone Scam",         "Fake electricity disconnection threat"),
    ("Phone Scam",         "Fake police arrest warrant / digital arrest"),
    ("Phone Scam",         "OTP phishing via fake telecom executive"),
    ("Digital/Cyber",      "SIM swap and OTP hijack to drain bank accounts"),
    ("Digital/Cyber",      "Fake loan app data extortion and harassment"),
    ("Digital/Cyber",      "Aadhaar-enabled payment fraud via AePS"),
    ("Digital/Cyber",      "WhatsApp account takeover and impersonation of contacts"),
    ("Digital/Cyber",      "Screen-sharing scam posing as tech support"),
    ("Investment",         "Pig butchering long-con investment fraud via social media"),
    ("Investment",         "Fake IPO / SME share allotment advance fee"),
    ("Investment",         "Ponzi scheme disguised as gold or commodity trading"),
    ("Investment",         "Fake mutual fund advisor churning client accounts"),
    ("Investment",         "Unlicensed forex trading platform with false returns"),
    ("Job Offer",          "Part-time task fraud on fake rating platforms"),
    ("Job Offer",          "Advance fee demanded for government job offer"),
    ("Job Offer",          "Work-from-home data entry advance payment trap"),
    ("Job Offer",          "Fake placement agency charging registration fees"),
    ("Online Shopping",    "Non-delivery after UPI payment to fake seller"),
    ("Online Shopping",    "Counterfeit goods sold as branded on social media"),
    ("Online Shopping",    "OLX / second-hand marketplace QR code refund scam"),
    ("Loan/Financial",     "Instant loan app with hidden processing fee trap"),
    ("Loan/Financial",     "Fake DSA charging upfront insurance to disburse loan"),
    ("Loan/Financial",     "Credit card reward point redemption phishing"),
    ("Pyramid Scheme/MLM", "Health product MLM with mandatory downline recruitment"),
    ("Pyramid Scheme/MLM", "Cryptocurrency MLM with token staking rewards"),
    ("Other",              "Fake charity / PM relief fund collection after disaster"),
    ("Other",              "Matrimonial profile fraud leading to money transfer"),
    ("Other",              "Fake rental property advance payment scam"),
]

# Trilingual structured schema for daily AI entries
AI_DAILY_SCAM_ED_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category":   {"type": "STRING"},
        "title_en":   {"type": "STRING"},
        "title_te":   {"type": "STRING"},
        "title_hi":   {"type": "STRING"},
        "story_en":   {"type": "STRING"},
        "story_te":   {"type": "STRING"},
        "story_hi":   {"type": "STRING"},
        "remedies_en": {
            "type": "OBJECT",
            "properties": {
                "before": {"type": "ARRAY", "items": {"type": "STRING"}},
                "during": {"type": "ARRAY", "items": {"type": "STRING"}},
                "after":  {"type": "ARRAY", "items": {"type": "STRING"}},
            }
        },
        "remedies_te": {
            "type": "OBJECT",
            "properties": {
                "before": {"type": "ARRAY", "items": {"type": "STRING"}},
                "during": {"type": "ARRAY", "items": {"type": "STRING"}},
                "after":  {"type": "ARRAY", "items": {"type": "STRING"}},
            }
        },
        "remedies_hi": {
            "type": "OBJECT",
            "properties": {
                "before": {"type": "ARRAY", "items": {"type": "STRING"}},
                "during": {"type": "ARRAY", "items": {"type": "STRING"}},
                "after":  {"type": "ARRAY", "items": {"type": "STRING"}},
            }
        },
        "source_note": {"type": "STRING"},
    },
    "required": [
        "category",
        "title_en", "title_te", "title_hi",
        "story_en", "story_te", "story_hi",
        "remedies_en", "remedies_te", "remedies_hi",
        "source_note",
    ]
}

# Lightweight schema for duplicate-topic judgment
AI_DUP_CHECK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_duplicate":   {"type": "BOOLEAN"},
        "matching_title": {"type": "STRING"},
    },
    "required": ["is_duplicate", "matching_title"]
}


N_PER_DAY = 3  # AI stories generated per cron run

TRICKS_FILE = "marketing-tricks.json"

MARKETING_TRICK_TOPICS = [
    "Mall/cinema kiosk charging above the MRP printed on a sealed bottled water",
    "Dual MRP - same product printed with a higher price for malls/cinemas/airports than regular shops",
    "Restaurant adding a 'service charge' as if it were mandatory GST",
    "Fake discount tags - inflated 'original price' crossed out to make a normal price look like a bargain",
    "E-commerce dark pattern - a pre-ticked add-on or insurance silently added at checkout",
    "Countdown timer or 'only 2 left' urgency messages that reset or are fake",
    "Weighing scale or quantity shortchanging at local vendors and petrol pumps",
    "Negative-option billing - free trial silently converting into a paid auto-renewing subscription",
    "'No-cost EMI' that hides a processing fee or inflated product price to cover the interest",
    "Mall/multiplex parking charging beyond the legally mandated free grace period",
    "Buy-1-get-1 offers where the MRP is quietly inflated to cover the 'free' item",
    "Extended warranty upsell using scare tactics about a product breaking down",
    "Local shopkeeper insisting a product is 'non-returnable/non-refundable' when the packaging says otherwise",
    "Cashback that lands in a locked wallet balance instead of the bank account",
    "Festival/limited-edition packaging used to justify a price hike on an everyday product",
    "Coaching centre or gym locking students into a long-term contract with no cooling-off refund",
    "Hidden convenience fee added only at the final payment step of ticket/food delivery apps",
    "Loose branded item (e.g. sweets, snacks) sold without any MRP/price disclosure at the counter",
    "Currency exchange or forex counters at airports quoting a rate far off the actual market rate",
    "Real estate broker or builder demanding cash 'token amount' with no receipt",
]

MARKETING_TRICK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "category":          {"type": "STRING"},
        "where_it_happens":  {"type": "STRING"},
        "trick_title_en":    {"type": "STRING"},
        "trick_title_te":    {"type": "STRING"},
        "trick_title_hi":    {"type": "STRING"},
        "trick_explain_en":  {"type": "STRING"},
        "trick_explain_te":  {"type": "STRING"},
        "trick_explain_hi":  {"type": "STRING"},
        "watch_for_en":      {"type": "ARRAY", "items": {"type": "STRING"}},
        "watch_for_te":      {"type": "ARRAY", "items": {"type": "STRING"}},
        "watch_for_hi":      {"type": "ARRAY", "items": {"type": "STRING"}},
        "smart_response_en": {"type": "STRING"},
        "smart_response_te": {"type": "STRING"},
        "smart_response_hi": {"type": "STRING"},
        "share_line_en":     {"type": "STRING"},
        "legal_note_en":     {"type": "STRING"},
    },
    "required": [
        "category", "where_it_happens",
        "trick_title_en", "trick_title_te", "trick_title_hi",
        "trick_explain_en", "trick_explain_te", "trick_explain_hi",
        "watch_for_en", "watch_for_te", "watch_for_hi",
        "smart_response_en", "smart_response_te", "smart_response_hi",
        "share_line_en", "legal_note_en",
    ],
}


def build_marketing_trick_prompt(topic_label):
    return f"""Write today's "Smart Shopper" card for LawSticker AI — a daily feature that exposes one everyday commercial trick businesses in India use on customers, and gives a short, confident, ready-to-say response.

TOPIC: "{topic_label}"

This is NOT a scam or fraud story — it's a common, usually-legal-adjacent-but-unfair commercial practice ordinary shoppers face (overcharging, dark patterns, pressure tactics, hidden fees). Do not name any specific real company, brand, or business — describe the pattern generically ("a mall kiosk", "an online store", "a local vendor") since this is general consumer education, not a report about anyone in particular.

Generate ALL fields in a single response:

category — short label, e.g. "Overcharging", "Dark Pattern", "Hidden Fee", "Fake Urgency", "Bundling Trick"
where_it_happens — short phrase, e.g. "Malls, cinemas & food courts" or "Online checkout pages" — where a shopper is most likely to run into this
trick_title_en/te/hi — punchy, specific headline naming the trick (max 12 words) — should make someone stop scrolling
trick_explain_en/te/hi — 2 short paragraphs: what the business does and why it works on people psychologically or practically. Plain language, no jargon.
watch_for_en/te/hi — array of exactly 3 short (under 12 words each) concrete warning signs a shopper can spot in the moment, e.g. "Price is only visible after you've already committed to buying"
smart_response_en/te/hi — a SHORT, confident, word-for-word script (1-3 sentences) the person can literally say or do in the moment to push back. Must be realistic and non-confrontational, something an ordinary person would actually say.
share_line_en — ONE punchy sentence (under 20 words) written for a WhatsApp status or Instagram caption — should make people want to forward it. No hashtags.
legal_note_en — ONE sentence citing the relevant Indian law/rule ONLY if one genuinely, specifically applies (e.g. Legal Metrology (Packaged Commodities) Rules 2011 for MRP, Consumer Protection Act 2019 for unfair trade practice, RBI guidelines for EMI/loan practices). If no specific law applies, say "This is a business practice, not necessarily illegal — but you can still push back."

Write Telugu and Hindi as genuine translations in proper script, never transliteration."""


def daily_marketing_trick_already_done(entries, today_str):
    return any(e.get("origin_date") == today_str for e in entries)


@app.route('/api/daily-marketing-trick', methods=['GET'])
def daily_marketing_trick():
    # Same fully-automatic philosophy as scam stories, but lighter:
    # one card a day, no grounding needed since these are generic,
    # well-known commercial patterns (not claims about anyone specific),
    # so no defamation exposure and no search-grounding cost.
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        today_str = datetime.now(timezone.utc).date().isoformat()

        try:
            data, sha = github_get(TRICKS_FILE, site_token, timeout=8)
            if data is None:
                data = {"entries": []}
        except Exception:
            data, sha = {"entries": []}, None

        entries = data.get("entries", [])
        if daily_marketing_trick_already_done(entries, today_str):
            return jsonify({"ok": True, "skipped": True, "reason": "Already published today's trick."})

        used_topics = {e.get("topic_label") for e in entries[-len(MARKETING_TRICK_TOPICS):]}
        candidates = [t for t in MARKETING_TRICK_TOPICS if t not in used_topics] or MARKETING_TRICK_TOPICS
        # Deterministic-ish daily rotation rather than random, so runs are reproducible if retried same day
        topic_label = candidates[int(datetime.now(timezone.utc).timestamp() // 86400) % len(candidates)]

        prompt = build_marketing_trick_prompt(topic_label)
        try:
            parsed = call_gemini_structured(gemini_key, prompt, MARKETING_TRICK_SCHEMA, max_tokens=2500)
        except urllib.error.HTTPError as he:
            body = he.read().decode()
            return jsonify({"ok": False, "error": f"Gemini error {he.code}: {body[:200]}"}), 502

        if not parsed or not parsed.get("trick_title_en"):
            return jsonify({"ok": False, "error": "Generation failed."}), 502

        entry = {
            "id":            f"trick-{int(datetime.now(timezone.utc).timestamp())}",
            "topic_label":   topic_label,
            "category":      parsed["category"],
            "where":         parsed.get("where_it_happens", ""),
            "title_en":      parsed["trick_title_en"],
            "title_te":      parsed["trick_title_te"],
            "title_hi":      parsed["trick_title_hi"],
            "explain_en":    parsed["trick_explain_en"],
            "explain_te":    parsed["trick_explain_te"],
            "explain_hi":    parsed["trick_explain_hi"],
            "watch_for_en":  parsed.get("watch_for_en", []),
            "watch_for_te":  parsed.get("watch_for_te", []),
            "watch_for_hi":  parsed.get("watch_for_hi", []),
            "response_en":   parsed["smart_response_en"],
            "response_te":   parsed["smart_response_te"],
            "response_hi":   parsed["smart_response_hi"],
            "share_line":    parsed["share_line_en"],
            "legal_note":    parsed["legal_note_en"],
            "origin":        "ai_generated",
            "origin_date":   today_str,
            "created_at":    datetime.now(timezone.utc).isoformat(),
        }
        entries.append(entry)
        data["entries"] = entries[-300:]
        github_put(TRICKS_FILE, site_token, data, sha,
                   f"Daily marketing trick {today_str}: {topic_label[:60]}", timeout=15)

        return jsonify({"ok": True, "entry": {"title": entry["title_en"], "category": entry["category"]}})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/api/trick-card-image', methods=['GET'])
def trick_card_image():
    # Deterministic PIL render, no AI image generation — reuses the exact
    # same non-AI branded template as the daily social card's fallback path.
    # Optional ?id=trick-xxxx to render an older entry; defaults to latest.
    site_token = os.environ.get("SITE_REPO_TOKEN")
    if not site_token:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500
    try:
        data, _ = github_get(TRICKS_FILE, site_token, timeout=8)
        entries = (data or {}).get("entries", [])
        if not entries:
            return jsonify({"ok": False, "error": "No tricks published yet."}), 404

        wanted_id = request.args.get("id")
        entry = None
        if wanted_id:
            entry = next((e for e in entries if e.get("id") == wanted_id), None)
        if not entry:
            entry = entries[-1]

        img_bytes = render_social_card(
            hook=entry.get("title_en", ""),
            subtext=entry.get("response_en", ""),
            label=entry.get("category", "Smart Shopper"),
            icon="\U0001F6CD\uFE0F",  # shopping bags
            badge_text="SMART SHOPPER",
        )
        resp = app.response_class(img_bytes, mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def pick_ai_scam_topic(today_str, pending_entries, skip_topics=None):
    skip_topics = skip_topics or set()
    recent_labels = set()
    ai_entries = [e for e in pending_entries if e.get("origin") == "ai_generated"]
    for e in ai_entries[-30:]:
        key = (e.get("title_en") or e.get("title") or "")[:40]
        recent_labels.add(key)
        if e.get("topic_label"):
            recent_labels.add(e["topic_label"][:40])

    seed = int(hashlib.md5(today_str.encode()).hexdigest(), 16)
    for offset in range(len(AI_SCAM_TOPICS)):
        idx = (seed + offset) % len(AI_SCAM_TOPICS)
        category, label = AI_SCAM_TOPICS[idx]
        if label[:40] not in recent_labels and label not in skip_topics:
            return category, label
    return AI_SCAM_TOPICS[seed % len(AI_SCAM_TOPICS)]


def prefilter_duplicate_candidates(topic_label, category, all_entries):
    stopwords = {
        "a", "an", "the", "and", "or", "of", "in", "on", "at", "to",
        "for", "via", "with", "from", "by", "fake", "fraud", "scam",
        "using", "how", "your",
    }
    topic_words = {
        w.lower() for w in re.split(r"\W+", topic_label)
        if len(w) > 2 and w.lower() not in stopwords
    }
    candidates = []
    for e in all_entries:
        if e.get("category") != category:
            continue
        title = e.get("title_en") or e.get("title") or ""
        title_words = {
            w.lower() for w in re.split(r"\W+", title)
            if len(w) > 2 and w.lower() not in stopwords
        }
        if len(topic_words & title_words) >= 2:
            candidates.append(e)
    return candidates


def check_duplicate_with_gemini(gemini_key, topic_label, candidate_titles):
    titles_list = "\n".join(f"- {t}" for t in candidate_titles)
    prompt = (
        f'Proposed new topic: "{topic_label}"\n\n'
        f"Already in the database:\n{titles_list}\n\n"
        "Are these describing the SAME underlying scam mechanism? "
        "Duplicates share the same method — not just the same broad category. "
        "If duplicate, set is_duplicate true and matching_title to the exact matching title. "
        "If genuinely different, set is_duplicate false and matching_title empty."
    )
    try:
        return call_gemini_structured(
            gemini_key, prompt, AI_DUP_CHECK_SCHEMA, max_tokens=80
        )
    except Exception:
        return {"is_duplicate": False, "matching_title": ""}


def increment_reported_count(report_id, pending, pending_sha,
                              public_data, public_sha, site_token):
    for e in pending.get("entries", []):
        if e.get("id") == report_id:
            e["reported_count"] = e.get("reported_count", 1) + 1
            github_put(PENDING_FILE, site_token, pending, pending_sha,
                       f"Bump reported_count {report_id}", timeout=8)
            return
    for e in public_data.get("entries", []):
        if e.get("id") == report_id:
            e["reported_count"] = e.get("reported_count", 1) + 1
            github_put(PUBLIC_FILE, site_token, public_data, public_sha,
                       f"Bump reported_count {report_id}", timeout=8)
            return


def build_ai_scam_ed_prompt_v2(category, topic_label):
    return f"""You are writing scam awareness content for LawSticker AI, an Indian legal-aid platform. This entry is published in English, Telugu, and Hindi simultaneously.

TOPIC: "{topic_label}"
CATEGORY: {category}

RULES:
- Real names of government bodies, regulators (RBI, TRAI, SEBI, MHA, NCPCR), and well-documented public cases ARE allowed — this is public-domain awareness content, not a personal claim.
- Do NOT name or imply claims against any private individual or unlisted business.
- For source references: do NOT invent URLs or article titles. Use generic descriptions only: e.g. "widely reported in Indian media in July 2025", "flagged in RBI circular 2024", "documented in TRAI advisory 2023".
- Write at the level of a first-time smartphone user. No legal jargon.
- Be generous with depth — one entry per day, readers depend on it.

Generate ALL fields in ALL THREE languages in a single response:

title_en / title_te / title_hi
  A vivid, specific headline (max 15 words) describing what victims actually experience.

story_en / story_te / story_hi
  3–4 paragraphs:
  Para 1 — The hook: how does the scammer first make contact?
  Para 2 — The escalation: what happens once the victim engages?
  Para 3-4 — The discovery: what victims realise too late, and the real-world impact.

remedies_en / remedies_te / remedies_hi
  A structured object with three arrays of plain numbered steps (no markdown, no asterisks):
  before: 3–5 steps to prevent falling for this scam
  during: 2–4 steps if you are mid-scam right now
  after:  3–5 recovery steps — MUST include 1930 Cybercrime Helpline, cybercrime.gov.in, bank fraud dispute, FIR, and the specific IT Act 2000 / BNS 2023 section that applies

source_note
  One sentence describing where this pattern is documented (generic, no invented links).

category
  Must be one of: {SCAMED_CATEGORIES}

Write Telugu and Hindi as genuine translations — proper sentences in the correct script, never transliteration."""


@app.route('/api/daily-scam-ed', methods=['GET'])
def daily_scam_ed():
    # Fully automatic pipeline: no pending queue, no Telegram approval.
    # Each story must come from a real, Google-Search-grounded source with
    # a real URL (taken from groundingMetadata, never model-typed text)
    # before it's allowed to publish. No source found = that slot is
    # skipped for today, nothing fabricated goes live.
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    bot_token  = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id    = os.environ.get("TELEGRAM_CHAT_ID")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        today_str = datetime.now(timezone.utc).date().isoformat()

        try:
            public_data, public_sha = github_get(PUBLIC_FILE, site_token, timeout=8)
            if public_data is None:
                public_data = {"entries": []}
        except Exception:
            public_data, public_sha = {"entries": []}, None

        public_entries = public_data.get("entries", [])
        today_ai = [
            e for e in public_entries
            if e.get("origin") == "ai_generated" and e.get("origin_date") == today_str
        ]
        slots_remaining = N_PER_DAY - len(today_ai)
        if slots_remaining <= 0:
            return jsonify({"ok": True, "skipped": True,
                            "reason": f"Already published {N_PER_DAY} entries for today."})

        generated = []   # list of (entry_dict, ref_number)
        used_topics = set()
        ts_base = int(datetime.now(timezone.utc).timestamp())

        # A few spare tries: some topics won't have a findable real source,
        # some will turn out duplicate — both just move to the next topic.
        for attempt in range(slots_remaining + 6):
            if len(generated) >= slots_remaining:
                break

            category, topic_label = pick_ai_scam_topic(today_str, public_entries, skip_topics=used_topics)
            used_topics.add(topic_label)

            # Duplicate prefilter against everything already published
            candidates = prefilter_duplicate_candidates(topic_label, category, public_entries)
            if candidates:
                candidate_titles = [e.get("title_en") or e.get("title", "") for e in candidates]
                dup = check_duplicate_with_gemini(gemini_key, topic_label, candidate_titles)
                if dup.get("is_duplicate"):
                    continue

            # Phase 1 — grounded search for a real, sourced case
            search_prompt = build_grounded_search_prompt(category, topic_label)
            try:
                grounded_text, source_urls = call_gemini_grounded(gemini_key, search_prompt)
            except Exception:
                continue

            if not grounded_text or "NO VERIFIABLE SOURCE FOUND" in grounded_text or not source_urls:
                continue  # hard gate: no real source URL, no publish

            best_source = source_urls[0]

            # Phase 2 — structured trilingual extraction from the grounded text
            try:
                parsed = extract_scam_story_from_grounded_text(
                    gemini_key, grounded_text, category, topic_label
                )
            except urllib.error.HTTPError as he:
                body = he.read().decode()
                return jsonify({"ok": False, "error": f"Gemini error {he.code}: {body[:200]}"}), 502

            if not parsed or not parsed.get("story_en"):
                continue

            seq = len(generated) + 1
            internal_id = f"scam-ai-{ts_base + seq}"
            ref_number  = f"AI-{today_str.replace('-', '')[2:]}-{seq}"

            entry = {
                "id":               internal_id,
                "ref_number":       ref_number,
                "topic_label":      topic_label,
                "category":         parsed["category"],
                "title_en":         parsed["title_en"],
                "title_te":         parsed["title_te"],
                "title_hi":         parsed["title_hi"],
                "title":            parsed["title_en"],
                "story_en":         parsed["story_en"],
                "story_te":         parsed["story_te"],
                "story_hi":         parsed["story_hi"],
                "anonymized_story": parsed["story_en"],
                "remedies": {
                    "en": parsed["remedies_en"],
                    "te": parsed["remedies_te"],
                    "hi": parsed["remedies_hi"],
                },
                "source_note":      parsed.get("source_note", ""),
                "source_url":       best_source["uri"],
                "source_title":     best_source.get("title", ""),
                "lang":             "en",
                "submitted_at":     datetime.now(timezone.utc).isoformat(),
                "approved_at":      datetime.now(timezone.utc).isoformat(),
                "origin":           "ai_generated",
                "origin_date":      today_str,
                "reported_count":   1,
                "status":           "approved",
            }
            public_entries.append(entry)
            generated.append((entry, ref_number))

        if not generated:
            return jsonify({"ok": True, "skipped": True,
                            "reason": "No topic today had a verifiable real source."})

        public_data["entries"] = public_entries[-1000:]
        github_put(PUBLIC_FILE, site_token, public_data, public_sha,
                   f"AI daily scam stories {today_str} ({len(generated)} published, sourced)", timeout=20)

        # Simple info ping — no approve/reject buttons, nothing to action
        if bot_token and chat_id:
            titles = "\n".join(f"• {e['title_en']} ({e['source_url']})" for e, _ in generated)
            msg = f"🛡️ <b>{len(generated)} AI scam stories published today</b>\n\n{titles}"
            for cid in [c.strip() for c in chat_id.split(",") if c.strip()]:
                try:
                    send_telegram(bot_token, cid, msg)
                except Exception:
                    pass

        return jsonify({
            "ok": True,
            "generated": len(generated),
            "entries": [
                {"ref_number": ref_number, "category": e["category"], "title": e["title_en"], "source_url": e["source_url"]}
                for e, ref_number in generated
            ],
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


TRANSLATE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title_te": {"type": "STRING"},
        "title_hi": {"type": "STRING"},
        "story_te": {"type": "STRING"},
        "story_hi": {"type": "STRING"},
    },
    "required": ["title_te", "title_hi", "story_te", "story_hi"],
}


@app.route('/api/backfill-translations', methods=['GET'])
def backfill_translations():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not site_token or not gemini_key:
        return jsonify({"ok": False, "error": "Server misconfiguration."}), 500

    try:
        public_data, public_sha = github_get(PUBLIC_FILE, site_token, timeout=10)
        if not public_data:
            return jsonify({"ok": False, "error": "Could not read public file."}), 500

        entries = public_data.get("entries", [])
        translated = 0
        skipped = 0
        errors = []

        for entry in entries:
            if entry.get("title_te") and entry.get("title_hi"):
                skipped += 1
                continue

            title_en = entry.get("title") or ""
            story_en = entry.get("anonymized_story") or ""
            if not title_en or not story_en:
                skipped += 1
                continue

            prompt = (
                f'Translate this scam story from English into Telugu and Hindi.\n\n'
                f'Title (EN): {title_en}\n\n'
                f'Story (EN): {story_en}\n\n'
                f'Output title_te, title_hi, story_te, story_hi.\n'
                f'Use proper Telugu and Hindi script — never transliteration.\n'
                f'Preserve the full length and meaning of the story in both languages.'
            )
            try:
                parsed = call_gemini_structured(
                    gemini_key, prompt, TRANSLATE_SCHEMA, max_tokens=2000
                )
                if parsed and parsed.get("title_te"):
                    entry["title_en"]  = title_en
                    entry["title_te"]  = parsed["title_te"]
                    entry["title_hi"]  = parsed["title_hi"]
                    entry["story_en"]  = story_en
                    entry["story_te"]  = parsed["story_te"]
                    entry["story_hi"]  = parsed["story_hi"]
                    translated += 1
                else:
                    errors.append(entry.get("id", "?") + ": empty Gemini response")
            except Exception as ex:
                errors.append(entry.get("id", "?") + ": " + str(ex))

        if translated:
            public_data["entries"] = entries
            github_put(PUBLIC_FILE, site_token, public_data, public_sha,
                       f"Backfill translations for {translated} entries", timeout=12)

        return jsonify({
            "ok": True,
            "translated": translated,
            "skipped": skipped,
            "errors": errors,
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------------
# YouTube Stats — cron-triggered ONCE PER DAY only. Fetches real subscriber
# and video counts from YouTube's public Data API (read-only, no OAuth
# needed for public channel stats) and caches to a file, same pattern as
# news/quiz/social card. Homepage reads the cache, never calls YouTube
# directly on a page visit.
# ---------------------------------------------------------------------------

YT_STATS_FILE = "youtube-stats.json"
YT_CHANNEL_HANDLE = "lawstickerai"


@app.route('/api/youtube-stats', methods=['GET'])
def youtube_stats():
    site_token = os.environ.get("SITE_REPO_TOKEN")
    yt_key = os.environ.get("YOUTUBE_API_KEY")
    if not site_token or not yt_key:
        return jsonify({"ok": False, "error": "Server misconfiguration — missing YOUTUBE_API_KEY."}), 500

    try:
        url = (
            "https://www.googleapis.com/youtube/v3/channels"
            f"?part=statistics&forHandle={YT_CHANNEL_HANDLE}&key={yt_key}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "lawsticker-ai-cron/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        items = data.get("items", [])
        if not items:
            return jsonify({"ok": False, "error": "Channel not found for handle.", "raw": data}), 500

        stats = items[0]["statistics"]
        output = {
            "subscriber_count": int(stats.get("subscriberCount", 0)),
            "video_count": int(stats.get("videoCount", 0)),
            "view_count": int(stats.get("viewCount", 0)),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            _, sha = github_get(YT_STATS_FILE, site_token, timeout=5)
        except Exception:
            sha = None
        github_put(YT_STATS_FILE, site_token, output, sha, "Daily YouTube stats refresh", timeout=8)

        return jsonify({"ok": True, "stats": output})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



# ---------------------------------------------------------------------------
# Health check — visit this URL directly in your phone's browser to confirm
# the whole service deployed and is running, before testing individual routes.
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "lawsticker-backend-full", "routes": [
        "/api/wall-of-fame", "/api/update-gold-rate", "/api/pulse",
        "/api/site-activity-digest", "/api/site-watchers",
        "/api/ask-ai", "/api/scam-ed", "/api/scam-moderate",
        "/api/daily-digest", "/api/news-digest-i18n", "/api/daily-quiz",
        "/api/telegram-webhook", "/api/daily-social-card",
        "/api/daily-sc-digest", "/api/sc-digest-data",
        "/api/daily-scam-ed", "/api/youtube-stats",
    ]})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
