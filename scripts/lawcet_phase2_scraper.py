"""
TG LAWCET 2026 Phase 2 (Allot26) Allotment List Scraper
=========================================================

WHERE TO RUN THIS:
This script needs to reach lawcetadm.tgche.ac.in directly. It has been
confirmed (2026-08-05) that the Claude Code Remote sandbox this repo was
edited from CANNOT reach that host - the session's egress proxy returns an
explicit 403 policy denial for it. Run this on:
  - Your own laptop/PC (just needs Python + `pip install requests beautifulsoup4`)
  - Or as a one-off script on the Cloud Run backend, if you'd rather trigger
    it as a URL like the other Eklavya/scam-story setup steps this session
  - Google Colab also works fine for this

WHAT IT DOES:
The Allotment List page (lawcetadm.tgche.ac.in/Allot26/Info/Allotmentlist)
is a classic ASP.NET WebForms page: a dropdown listing every college+course
combination, and choosing one triggers a server "postback" (not a simple
URL change) that returns that college's table.

ASP.NET postbacks require re-submitting the hidden state fields every
request (__VIEWSTATE, __VIEWSTATEGENERATOR, __EVENTVALIDATION, and
sometimes others) alongside the actual selection - the server checks these
to make sure the request came from its own form. This script:
  1. Loads the page once, keeping a `requests.Session()` for cookies
  2. Auto-detects the dropdown control (rather than trusting a hardcoded
     field name) and parses its real <option value="..."> IDs (NOT the
     visible text - ASP.NET dropdowns use internal numeric/coded values)
  3. Parses ALL hidden <input> fields from the form (not just the three
     usual suspects) so any extra anti-forgery/state fields ride along
  4. Loops through every option, POSTs the selection + hidden state,
     re-reads the fresh state from each response (it changes per request),
     and parses the resulting allotment table
  5. Appends every row to one big CSV

IMPORTANT - NOT LIVE-VERIFIED:
Nobody with live network access to this site has run DIAGNOSTIC mode yet.
The auto-detection logic below (dropdown-by-option-count, table-by-heuristic)
is written defensively so it should adapt to the real page without needing
hand-edited field names, but it has only been exercised against synthetic
HTML, not the real response. Run DIAGNOSTIC=True first and sanity-check the
printed output before trusting a full run. If DIAGNOSTIC output looks wrong
(e.g. no <select> at all), see the note in the diagnostic block below about
non-standard controls (e.g. DevExpress ASPxComboBox), which some Indian
govt sites use instead of plain WebForms <select> dropdowns.
"""

import csv
import re
import time
import sys
from bs4 import BeautifulSoup

try:
    import requests
except ImportError:
    sys.exit("Run: pip install requests beautifulsoup4")

BASE_URL = "https://lawcetadm.tgche.ac.in/Allot26/Info/Allotmentlist"
OUTPUT_CSV = "lawcet_phase2_allotments.csv"
DELAY_BETWEEN_REQUESTS = 1.5  # be a polite scraper, don't hammer a govt server

# Used only as a preferred match if present; auto-detection below no longer
# depends on this being correct (see find_dropdown()).
DROPDOWN_FIELD_NAME_HINT = "ctl00$ContentPlaceHolder1$ddlCollege"
DIAGNOSTIC = True  # set to False once diagnostic output looks sane

# A plain WebForms <select> with fewer options than this is almost
# certainly not the college/course dropdown (nav menus, page-size
# selectors, language pickers, etc. tend to be tiny).
MIN_PLAUSIBLE_OPTIONS = 10


def get_hidden_fields(soup):
    """
    Pull every hidden ASP.NET postback field from the form, not just the
    three usual ones - some pages add extra anti-forgery/state fields that
    still need to be echoed back on every POST.
    """
    fields = {}
    for tag in soup.find_all("input", {"type": "hidden"}):
        name = tag.get("name")
        if name:
            fields[name] = tag.get("value", "")
    return fields


def find_dropdown(soup):
    """
    Locate the college/course <select>, auto-detecting it rather than
    trusting a hardcoded control ID (which we have not been able to
    confirm against the live page). Preference order:
      1. A <select> whose name matches DROPDOWN_FIELD_NAME_HINT
      2. The <select> with the most <option> entries, as long as it clears
         MIN_PLAUSIBLE_OPTIONS (the real dropdown has ~90 entries; small
         selects are almost always something else, like page size or
         language).
    Returns the tag, or None if nothing plausible was found.
    """
    selects = soup.find_all("select")
    if not selects:
        return None

    for sel in selects:
        if sel.get("name") == DROPDOWN_FIELD_NAME_HINT:
            return sel

    best = max(selects, key=lambda s: len(s.find_all("option")))
    if len(best.find_all("option")) >= MIN_PLAUSIBLE_OPTIONS:
        return best
    return None


def get_dropdown_options(soup):
    """
    Find the college/course dropdown and return (field_name, [(value, label), ...]).
    `value` is the real ASP.NET option value (what actually gets POSTed);
    `label` is the human-readable text you saw in the browser.
    """
    select = find_dropdown(soup)
    if not select:
        raise RuntimeError(
            "Could not find a plausible college dropdown on the page. "
            "If DIAGNOSTIC output shows no <select> at all, the control is "
            "likely rendered as a non-standard widget (e.g. DevExpress "
            "ASPxComboBox) that needs different scraping logic - inspect "
            "the page in browser devtools."
        )
    field_name = select.get("name")
    options = []
    for opt in select.find_all("option"):
        val = opt.get("value", "").strip()
        label = opt.get_text(strip=True)
        if val and label and label.lower() not in ("-select-", "select", ""):
            options.append((val, label))
    return field_name, options


def _dedupe_headers(raw_headers):
    """
    ASP.NET GridViews sometimes render blank or repeated header cells
    (e.g. an unlabeled actions column). Give blanks/duplicates unique
    names so they don't silently collide in the output dict.
    """
    seen = {}
    headers = []
    for i, h in enumerate(raw_headers):
        name = h if h else f"col_{i+1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        headers.append(name)
    return headers


def find_result_table(soup):
    """
    Pick the table most likely to be the allotment GridView, rather than
    assuming it's the first <table> on the page (layout/nav tables often
    come first in ASP.NET markup). Preference order:
      1. A table whose id/class hints at being a grid (contains "grd",
         "grid", or "gv", case-insensitive - common ASP.NET/DevExpress
         naming conventions)
      2. The table with the most rows
    """
    tables = soup.find_all("table")
    if not tables:
        return None

    grid_pattern = re.compile(r"grd|grid|gv", re.IGNORECASE)
    for t in tables:
        ident = " ".join(filter(None, [t.get("id", ""), " ".join(t.get("class", []))]))
        if grid_pattern.search(ident):
            return t

    return max(tables, key=lambda t: len(t.find_all("tr")))


def parse_allotment_table(soup):
    """
    Parse the results table returned by a postback into a list of dict
    rows. ASP.NET GridViews render as a plain <table> with a header row -
    this grabs headers from the first row and zips them with each
    subsequent row's cells.
    """
    table = find_result_table(soup)
    if not table:
        return []
    rows = table.find_all("tr")
    if not rows:
        return []
    raw_headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
    headers = _dedupe_headers(raw_headers)
    data = []
    for tr in rows[1:]:
        cells = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not cells or len(cells) != len(headers):
            continue
        data.append(dict(zip(headers, cells)))
    return data


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": BASE_URL,
    })

    print("Loading initial page...")
    resp = session.get(BASE_URL, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")

    if DIAGNOSTIC:
        print("\n=== DIAGNOSTIC OUTPUT ===")

        selects = soup.find_all("select")
        print(f"Found {len(selects)} <select> element(s) on the page:")
        for sel in selects:
            opts = sel.find_all("option")
            print(f"   name={sel.get('name')!r} id={sel.get('id')!r} options={len(opts)}")
        if not selects:
            print("   NONE - the dropdown may be a non-standard widget (see")
            print("   module docstring note on DevExpress-style controls).")

        detected = find_dropdown(soup)
        print("\nAuto-detected dropdown:", detected.get("name") if detected is not None else "NONE FOUND")
        if detected is not None:
            print("First 5 options (value, label):")
            for opt in detected.find_all("option")[:5]:
                print("   ", opt.get("value"), "|", opt.get_text(strip=True)[:60])

        hidden = get_hidden_fields(soup)
        print(f"\nHidden form fields found ({len(hidden)}):", ", ".join(sorted(hidden.keys())) or "NONE")

        tables = soup.find_all("table")
        print(f"\n<table> elements on initial page load: {len(tables)}")
        for i, t in enumerate(tables[:5]):
            print(f"   table[{i}] id={t.get('id')!r} rows={len(t.find_all('tr'))}")

        print("=== END DIAGNOSTIC ===\n")
        print("If the auto-detected dropdown or hidden fields look wrong, or")
        print("if NO <select> was found, paste this output back before running")
        print("a full scrape - the parsing logic likely needs adjustment for")
        print("this site's actual markup.\n")
        return

    field_name, options = get_dropdown_options(soup)
    print(f"Using dropdown field: {field_name}")
    print(f"Found {len(options)} college/course combinations to fetch.")

    all_rows = []
    for i, (value, label) in enumerate(options, 1):
        print(f"[{i}/{len(options)}] {label[:70]}")

        post_data = {
            **get_hidden_fields(soup),
            field_name: value,
            "__EVENTTARGET": field_name,
            "__EVENTARGUMENT": "",
        }

        try:
            resp = session.post(BASE_URL, data=post_data, timeout=20)
        except requests.RequestException as e:
            print(f"   -> request failed: {e}, skipping")
            continue

        soup = BeautifulSoup(resp.text, "html.parser")  # refresh for next viewstate
        rows = parse_allotment_table(soup)
        for row in rows:
            row["_college_course_label"] = label
        all_rows.extend(rows)
        print(f"   -> {len(rows)} rows")

        time.sleep(DELAY_BETWEEN_REQUESTS)

    if not all_rows:
        print("No data collected - check the diagnostic output and field names.")
        return

    fieldnames = sorted({k for row in all_rows for k in row.keys()})
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nDone. {len(all_rows)} total rows written to {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
