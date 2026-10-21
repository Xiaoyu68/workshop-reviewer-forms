#!/usr/bin/env python3
"""Find Google Forms that upcoming workshops use to recruit reviewers.

Pipeline:
  1. Discover workshops from OpenReview: active venues, plus every
     <conference>/<this or next year>/Workshop/ prefix derived from them.
  2. Look up each workshop's paper deadline (the Submission invitation's due
     date) and drop the ones whose deadline has passed.
  3. Crawl each website: the homepage plus a few same-site pages whose link
     text or URL looks reviewer-related (call for reviewers, PC, committee, ...).
  4. Pull out forms.gle / docs.google.com/forms links and label each one by the
     text around it ("reviewer" if it mentions reviewing / program committee).
  5. Diff against earlier runs, write site/index.html (the viewer), site/data.json
     and state/report.csv, and raise a macOS notification for new reviewer forms.

Standard library only. Usage: python3 find_forms.py [--all] [--limit N] [--no-open]
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
SITE = HERE / "site"
API = "https://api2.openreview.net"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) workshop-reviewer-forms/1.0"

FORM_RE = re.compile(
    r"https?://(?:forms\.gle/[\w-]+|docs\.google\.com/forms/(?:u/\d+/)?d/(e/)?([\w-]+))")
SCRIPT_RE = re.compile(r"<(script|style)\b.*?</\1>", re.I | re.S)
# Link text that says nothing about what the form is for.
GENERIC_ANCHOR = {"here", "this", "form", "link", "click", "sign", "up", "fill", "out", "in", "the",
                  "google", "please", "apply", "now", "our", "a", "↗", "→"}
HREF_RE = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
DEADLINE_RE = re.compile(r"Submission Deadline:\s*([A-Z][a-z]{2} \d{1,2} \d{4})")
YEAR_RE = re.compile(r"^(.+?)/(20\d\d)/")


def load_config():
    return json.loads((HERE / "config.json").read_text())


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace"), r.geturl()


def api(path, tries=6):
    # OpenReview rate-limits bursts with 429; back off and retry.
    for attempt in range(tries):
        try:
            return json.loads(get(API + path, timeout=60)[0])
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == tries - 1:
                raise
            time.sleep(float(e.headers.get("Retry-After") or 2 ** attempt))


def from_ms(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date()


# ---------- 1. discovery ----------

def prefix_ids(prefix):
    ids, offset = set(), 0
    while True:
        q = urllib.parse.urlencode({"prefix": prefix, "limit": 1000, "offset": offset})
        groups = api(f"/groups?{q}")["groups"]
        # Only the workshop venue itself, not its Reviewers/Authors/... subgroups.
        ids.update(g["id"] for g in groups if g["id"].count("/") == prefix.rstrip("/").count("/") + 1)
        if len(groups) < 1000:
            return ids
        offset += 1000


def discover_ids(cfg, today):
    members = api("/groups?id=active_venues")["groups"][0]["members"]
    ids = {m for m in members if "/Workshop/" in m}
    # Workshops of a conference are not always listed as active (e.g. before
    # their CFP opens), so scan <root>/<year>/Workshop/ for every conference
    # root that has workshops, for this year and next.
    roots = {m.group(1) for m in map(YEAR_RE.match, ids) if m}
    prefixes = {f"{r}/{y}/Workshop/" for r in roots for y in (today.year, today.year + 1)}
    prefixes.update(cfg.get("extra_prefixes", []))
    with cf.ThreadPoolExecutor(4) as pool:
        for found in pool.map(prefix_ids, sorted(prefixes)):
            ids |= found
    return sorted(ids)


def parse_date(s):
    # OpenReview stores start_date either as "Jun 03 2026" or as epoch milliseconds.
    if isinstance(s, (int, float)):
        return from_ms(s)
    try:
        return dt.datetime.strptime(str(s).strip(), "%b %d %Y").date()
    except ValueError:
        return None


def conference_of(venue_id):
    # "thecvf.com/CVPR/2026/Workshop/X" -> "CVPR 2026", "NeurIPS.cc/2026/..." -> "NeurIPS 2026"
    m = YEAR_RE.match(venue_id)
    if not m:
        return venue_id.split("/")[0]
    name = m.group(1).split("/")[-1]
    name = re.sub(r"\.(cc|org|com|net|ws|io|fr|de)$", "", name)
    return f"{name} {m.group(2)}"


def submission_deadlines(inv_ids):
    out = {}
    for i in range(0, len(inv_ids), 50):
        q = urllib.parse.urlencode({"ids": ",".join(inv_ids[i:i + 50]), "expired": "true"})
        for inv in api(f"/invitations?{q}").get("invitations", []):
            if inv.get("duedate"):
                out[inv["id"]] = from_ms(inv["duedate"])
    return out


def fetch_venues(ids):
    venues = []
    for i in range(0, len(ids), 50):
        q = urllib.parse.urlencode({"ids": ",".join(ids[i:i + 50])})
        for g in api(f"/groups?{q}")["groups"]:
            c = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in g.get("content", {}).items()}
            m = DEADLINE_RE.search(str(c.get("date") or ""))
            venues.append({
                "venue_id": g["id"],
                "conference": conference_of(g["id"]),
                "title": c.get("title") or g["id"].rsplit("/", 1)[-1],
                "website": (c.get("website") or "").strip(),
                "start_date": parse_date(c.get("start_date") or ""),
                "created": from_ms(g.get("cdate", 0)),
                "deadline": parse_date(m.group(1)) if m else None,
                "submission_id": c.get("submission_id") or g["id"] + "/-/Submission",
            })
    # The Submission invitation's due date is the real paper deadline; the
    # free-text "date" field above is only a fallback.
    due = submission_deadlines(sorted({v["submission_id"] for v in venues}))
    for v in venues:
        v["deadline"] = due.get(v["submission_id"], v["deadline"])
    return venues


def status_of(v, today, grace):
    """'open' (deadline not passed), 'unknown' (no deadline but upcoming) or None (drop)."""
    if v["deadline"]:
        return "open" if v["deadline"] >= today - dt.timedelta(days=grace) else None
    if v["start_date"]:
        return "unknown" if v["start_date"] >= today else None
    return "unknown" if v["created"] >= today - dt.timedelta(days=180) else None


# ---------- 2. crawl ----------

def normalize(page):
    # Google Sites wraps outbound links as google.com/url?q=<encoded>; decode so
    # the form regex sees them.
    return urllib.parse.unquote(html.unescape(page))


def text_of(fragment):
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", fragment)).strip()


def canonical_form(m):
    # docs.google.com/forms/d/e/<id> without /viewform is a 404; always link the viewform page.
    if m.group(0).startswith(("http://forms.gle", "https://forms.gle")):
        return m.group(0).replace("http://", "https://")
    return f"https://docs.google.com/forms/d/{m.group(1) or ''}{m.group(2)}/viewform"


def label(anchor, before, cfg):
    """'reviewer' or 'other', judged by the link's own text first, then the sentence before it."""
    words = re.findall(r"[\w↗→]+", anchor.lower())
    if words and not set(words) <= GENERIC_ANCHOR:
        return "reviewer" if re.search(cfg["reviewer_anchor"], anchor, re.I) else "other"
    return "reviewer" if re.search(cfg["reviewer_keywords"], before[-cfg["context_chars"]:], re.I) else "other"


def form_links(page, cfg):
    """Yield (form_url, kind, context) for every Google Form linked or mentioned on a page."""
    page = SCRIPT_RE.sub(" ", normalize(page))
    covered = []
    for a in HREF_RE.finditer(page):
        m = FORM_RE.search(a.group(1))
        if not m:
            continue
        covered.append(a.span())
        anchor = text_of(a.group(2))[:120]
        before = text_of(page[max(0, a.start() - 3000):a.start()])
        yield canonical_form(m), label(anchor, before, cfg), f"{before[-200:]} [{anchor}]"
    for m in FORM_RE.finditer(page):
        if any(s <= m.start() < e for s, e in covered):
            continue
        before = text_of(page[max(0, m.start() - 3000):m.start()])
        yield canonical_form(m), label("", before, cfg), f"{before[-200:]} [{m.group(0)}]"


def form_is_open(url):
    """False when Google says the form is closed or it no longer exists."""
    try:
        body, final = get(url)
    except Exception:  # noqa: BLE001 - 404 / gone
        return False
    return "closedform" not in final and "no longer accepting responses" not in body.lower()


def same_site(base, url):
    b, u = urllib.parse.urlparse(base), urllib.parse.urlparse(url)
    if b.netloc != u.netloc:
        return False
    # Many workshops share a host (sites.google.com/view/<name>, *.github.io/<repo>).
    root = "/".join(b.path.split("/")[:3]) if b.netloc in ("sites.google.com",) else ""
    return u.path.startswith(root)


def crawl(v, cfg):
    site = v["website"]
    if not site.startswith("http"):
        site = "https://" + site if site else ""
    if not site:
        return [], "no website"
    sub_re = re.compile(cfg["subpage_keywords"], re.I)
    found, err = {}, ""
    try:
        home, final = get(site)
    except Exception as e:  # noqa: BLE001 - any fetch failure is just reported
        return [], f"fetch failed: {type(e).__name__}"
    pages = [(final, home)]
    seen = {final.split("#")[0]}
    for href, text in HREF_RE.findall(home):
        url = urllib.parse.urljoin(final, html.unescape(href)).split("#")[0]
        if url in seen or not same_site(final, url) or not sub_re.search(url + " " + TAG_RE.sub(" ", text)):
            continue
        seen.add(url)
        if len(seen) > cfg["max_subpages"]:
            break
        try:
            body, got = get(url)
            pages.append((got, body))
        except Exception:  # noqa: BLE001
            err = "some subpages failed"
    for url, raw in pages:
        for form, kind, ctx in form_links(raw, cfg):
            prev = found.get(form)
            if not prev or (prev["kind"] == "other" and kind == "reviewer"):
                found[form] = {"form": form, "kind": kind, "page": url, "context": ctx}
    return list(found.values()), err


# ---------- 3. report ----------

def load_seen(previous):
    """form -> first-seen date, from state/seen.json and/or a previous data.json (path or URL)."""
    seen_path = HERE / "state" / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    if previous:
        try:
            raw = get(previous)[0] if previous.startswith("http") else Path(previous).read_text()
            for r in json.loads(raw)["forms"]:
                seen.setdefault(r["form"], r["first_seen"])
        except Exception as e:  # noqa: BLE001 - a missing previous run just means everything is new
            print(f"no previous data from {previous}: {type(e).__name__}", file=sys.stderr)
    # Older runs stored docs.google.com links without /viewform.
    canon = {}
    for form, first in seen.items():
        m = FORM_RE.match(form)
        key = canonical_form(m) if m else form
        canon[key] = min(first, canon.get(key, first))
    return canon


def notify(n):
    msg = f"{n} new workshop reviewer form(s) - see site/index.html"
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Workshop reviewer forms"'],
                   check=False, capture_output=True)


def write_site(rows, summary):
    SITE.mkdir(parents=True, exist_ok=True)
    data = {**summary, "forms": rows}
    payload = json.dumps(data, ensure_ascii=False, default=str)
    (SITE / "data.json").write_text(payload)
    page = (HERE / "page_template.html").read_text()
    # Embed the data so the page also works when opened straight from disk.
    embedded = payload.replace("</", "<\\/")
    (SITE / "index.html").write_text(page.replace("/*__DATA__*/null", embedded))
    return SITE / "index.html"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also show forms not labelled reviewer")
    ap.add_argument("--limit", type=int, default=0, help="crawl only the first N workshops (testing)")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--no-open", action="store_true", help="don't open the page in a browser")
    ap.add_argument("--previous", help="earlier data.json (path or URL) to carry first-seen dates over")
    args = ap.parse_args()
    cfg = load_config()
    today = dt.date.today()
    global STATE, SITE
    if args.limit:
        # A partial test run must not overwrite the real report or the seen list.
        STATE = SITE = HERE / "state" / "test"
    STATE.mkdir(parents=True, exist_ok=True)

    print("discovering workshops on OpenReview...", file=sys.stderr)
    ids = discover_ids(cfg, today)
    venues = []
    for v in fetch_venues(ids):
        v["status"] = status_of(v, today, cfg.get("deadline_grace_days", 0))
        if v["status"]:
            venues.append(v)
    if args.limit:
        venues = venues[:args.limit]

    # Several OpenReview venues (main track, ARR commitment, shared task) often
    # share one website, so crawl each site once and merge rows per form.
    by_site = {}
    for v in venues:
        by_site.setdefault(v["website"], []).append(v)
    sites = [vs[0] for vs in by_site.values()]
    print(f"{len(ids)} workshop venues, {len(venues)} with the deadline not passed; "
          f"crawling {len(sites)} websites...", file=sys.stderr)
    by_form, failures = {}, 0
    with cf.ThreadPoolExecutor(cfg["workers"]) as pool:
        for v, (forms, err) in zip(sites, pool.map(lambda v: crawl(v, cfg), sites)):
            failures += err.startswith("fetch failed")
            group = by_site[v["website"]]
            for f in forms:
                r = by_form.setdefault(f["form"], {**v, **f})
                if f["kind"] == "reviewer":
                    r["kind"] = "reviewer"
                r["venue_id"] = " ".join(sorted({*r["venue_id"].split(), *(g["venue_id"] for g in group)}))
                dates = [g["start_date"] for g in group if g["start_date"]]
                r["start_date"] = r["start_date"] or (min(dates) if dates else None)
                # The latest open deadline among the venues sharing this site.
                dls = [g["deadline"] for g in group if g["deadline"]]
                if dls:
                    r["deadline"], r["status"] = max(dls), "open"
    rows = [r for r in by_form.values() if args.all or r["kind"] == "reviewer"]
    # Drop forms Google reports as closed or deleted.
    with cf.ThreadPoolExecutor(cfg["workers"]) as pool:
        is_open = list(pool.map(lambda r: form_is_open(r["form"]), rows))
    closed = sum(not o for o in is_open)
    rows = [r for r, o in zip(rows, is_open) if o]

    seen = load_seen(args.previous)
    first_run = not seen
    for r in rows:
        r["first_seen"] = seen.get(r["form"], today.isoformat())
        r["new"] = not first_run and r["form"] not in seen
        seen.setdefault(r["form"], r["first_seen"])
        r.pop("created", None)
        r.pop("submission_id", None)
    if not args.limit:
        (STATE / "seen.json").write_text(json.dumps(seen, indent=1, sort_keys=True))

    far = dt.date(9999, 1, 1)
    rows.sort(key=lambda r: (r["deadline"] or far, r["start_date"] or far, r["title"]))
    with open(STATE / "report.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, ["new", "kind", "status", "conference", "title", "deadline", "start_date",
                                "form", "website", "page", "first_seen", "venue_id", "context"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in w.fieldnames})

    summary = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
               "today": today.isoformat(), "venues": len(venues), "sites": len(sites),
               "unreachable": failures, "closed": closed}
    page = write_site(rows, summary)
    n_rev = sum(r["kind"] == "reviewer" for r in rows)
    n_new = sum(r["new"] and r["kind"] == "reviewer" for r in rows)
    print(f"{n_rev} open reviewer forms ({n_new} new, {closed} closed ones dropped) from {len(sites)} sites, "
          f"{failures} unreachable -> {page}", file=sys.stderr)

    if n_new and not args.no_notify and sys.platform == "darwin":
        notify(n_new)
    if not args.no_open and not os.environ.get("CI"):
        webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
