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


GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
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

ENRICH_SCHEMA = {
    "type": "object",
    "properties": {
        "scam_type_label": {"type": "string", "description": "Short, well-known name for this scam pattern type, e.g. 'Pyramid Scheme / MLM Fraud'"},
        "modus_operandi": {"type": "string", "description": "2-3 sentences on how this type of scam generically operates, based on well-known patterns"},
        "red_flags": {"type": "array", "items": {"type": "string"}, "description": "3-5 short, practical warning signs to watch for"},
        "relevant_laws": {"type": "array", "items": {"type": "string"}, "description": "Names of well-established Indian Acts/laws relevant to this scam type — ONLY Act names, never case citations"},
        "prevalence_note": {"type": "string", "description": "One honest, qualitative sentence on how common/known this pattern is — no invented statistics"},
        "supportive_note": {"type": "string", "description": "A brief, warm, reassuring message for both the person who experienced this and future readers"},
    },
    "required": ["scam_type_label", "modus_operandi", "red_flags", "relevant_laws", "prevalence_note", "supportive_note"],
}


def call_gemini_enrichment(api_key, category, anonymized_story, lang):
    lang_names = {"en": "English", "te": "Telugu", "hi": "Hindi"}
    prompt = f"""You are enriching an approved, anonymized scam report for LawSticker AI's public "Scam Stories & Remedies" education page — a real person will read this to learn and feel supported.

Category: {category}
Story: {anonymized_story}

Produce, in {lang_names.get(lang, "English")}:
- scam_type_label: the well-known name for this pattern (e.g. "Pyramid Scheme / MLM Fraud", "Phishing / OTP Scam")
- modus_operandi: how scams of this general type typically work — genuinely informative, not vague
- red_flags: 3-5 concrete, practical warning signs
- relevant_laws: ONLY the names of well-established Indian Acts/laws relevant to this scam type (e.g. "Consumer Protection Act 2019", "Prize Chits and Money Circulation Schemes (Banning) Act 1978", "Information Technology Act 2000"). Do NOT cite specific court cases, judgments, or rulings — those cannot be verified here and must never be invented.
- prevalence_note: one honest, qualitative sentence on how commonly this pattern is reported — do not invent statistics or percentages
- supportive_note: warm, genuine reassurance — for the person who went through this, and for anyone reading this to learn

Stay factual and general. If you're not confident about a specific law applying, leave it out rather than guess.
Use simple, everyday language a common person can easily understand — avoid formal or academic wording throughout."""

    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 700,
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
    with urllib.request.urlopen(req, timeout=15) as resp:
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
        src_fields = target.get("structured_fields", {})
        public_entry = {
            "id": target["id"],
            "ref_number": target.get("ref_number", ""),
            "category": target["category"],
            "title": target["title"],
            "anonymized_story": target["anonymized_story"],
            "signals": {
                "contact_method": src_fields.get("contact_method", ""),
                "ask_action": src_fields.get("ask_action", ""),
                "cost_items": src_fields.get("cost_items", []),
                "money_range": src_fields.get("money_range", ""),
            },
            "lang": target.get("lang", "en"),
            "status": "approved",
            "submitted_at": target.get("submitted_at", ""),
            "approved_at": datetime.now(timezone.utc).isoformat(),
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


def render_social_card(hook, subtext, label, icon="\U0001F4A1", ai_background_bytes=None):
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

    badge_text = "DID YOU KNOW?"
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
        caption = (
            f"📲 <b>Today's shareable card</b>\n\n"
            f"Forward this to your WhatsApp Status, Instagram, or wherever — "
            f"ready as-is."
        )
        results = {}
        for cid in [c.strip() for c in chat_id.split(",") if c.strip()]:
            try:
                send_telegram_photo(bot_token, cid, image_bytes, caption,
                                     button_text="🔗 Read Full Story", button_url=page_url)
                results[cid] = "sent"
            except Exception as e:
                results[cid] = f"failed: {e}"

        return jsonify({"ok": True, "card": card_data, "telegram_results": results})

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# the whole service deployed and is running, before testing individual routes.
# ---------------------------------------------------------------------------

@app.route('/', methods=['GET'])
def health():
    return jsonify({"ok": True, "service": "lawsticker-backend-full", "routes": [
        "/api/wall-of-fame", "/api/update-gold-rate", "/api/pulse",
        "/api/site-activity-digest", "/api/site-watchers",
        "/api/ask-ai", "/api/scam-ed", "/api/scam-moderate",
        "/api/daily-digest", "/api/news-digest-i18n", "/api/daily-quiz", "/api/telegram-webhook", "/api/daily-social-card"
    ]})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
