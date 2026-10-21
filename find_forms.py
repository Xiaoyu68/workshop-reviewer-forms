#!/usr/bin/env python3
"""Find Google Forms that upcoming workshops use to recruit reviewers.

Pipeline:
  1. Discover workshops from OpenReview (active venues + configured prefixes).
     Each venue group carries its website, start date and submission deadline.
  2. Keep workshops that have not happened yet.
  3. Crawl each website: the homepage plus a few same-site pages whose link
     text or URL looks reviewer-related (call for reviewers, PC, committee, ...).
  4. Pull out forms.gle / docs.google.com/forms links and label each one by the
     text around it ("reviewer" if it mentions reviewing / program committee).
  5. Diff against state/seen.json, write state/report.md + report.csv, and
     raise a macOS notification when new reviewer forms appear.

Standard library only. Usage: python3 find_forms.py [--all] [--limit N]
"""
import argparse
import concurrent.futures as cf
import csv
import datetime as dt
import html
import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = HERE / "state"
API = "https://api2.openreview.net"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) workshop-reviewer-forms/1.0"

FORM_RE = re.compile(
    r"https?://(?:forms\.gle/[\w-]+|docs\.google\.com/forms/(?:u/\d+/)?d/(?:e/)?[\w-]+)")
HREF_RE = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
TAG_RE = re.compile(r"<[^>]+>")
DEADLINE_RE = re.compile(r"Submission Deadline:\s*([A-Z][a-z]{2} \d{1,2} \d{4})")


def load_config():
    return json.loads((HERE / "config.json").read_text())


def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(3_000_000).decode(r.headers.get_content_charset() or "utf-8", "replace"), r.geturl()


def api(path):
    return json.loads(get(API + path, timeout=60)[0])


# ---------- 1. discovery ----------

def discover_ids(cfg):
    ids = set()
    members = api("/groups?id=active_venues")["groups"][0]["members"]
    ids.update(m for m in members if "/Workshop/" in m)
    for prefix in cfg["openreview_prefixes"]:
        offset = 0
        while True:
            q = urllib.parse.urlencode({"prefix": prefix, "limit": 1000, "offset": offset})
            groups = api(f"/groups?{q}")["groups"]
            # Only the workshop venue itself, not its Reviewers/Authors/... subgroups.
            ids.update(g["id"] for g in groups if g["id"].count("/") == prefix.rstrip("/").count("/") + 1)
            if len(groups) < 1000:
                break
            offset += 1000
    return sorted(ids)


def parse_date(s):
    # OpenReview stores start_date either as "Jun 03 2026" or as epoch milliseconds.
    if isinstance(s, (int, float)):
        return dt.datetime.fromtimestamp(s / 1000, dt.timezone.utc).date()
    try:
        return dt.datetime.strptime(str(s).strip(), "%b %d %Y").date()
    except ValueError:
        return None


def fetch_venues(ids):
    venues = []
    for i in range(0, len(ids), 50):
        q = urllib.parse.urlencode({"ids": ",".join(ids[i:i + 50])})
        for g in api(f"/groups?{q}")["groups"]:
            c = {k: (v.get("value") if isinstance(v, dict) else v) for k, v in g.get("content", {}).items()}
            m = DEADLINE_RE.search(str(c.get("date") or ""))
            venues.append({
                "venue_id": g["id"],
                "title": c.get("title") or g["id"].rsplit("/", 1)[-1],
                "website": (c.get("website") or "").strip(),
                "start_date": parse_date(c.get("start_date") or ""),
                "created": dt.datetime.fromtimestamp(g.get("cdate", 0) / 1000, dt.timezone.utc).date(),
                "deadline": parse_date(m.group(1)) if m else None,
            })
    return venues


def is_upcoming(v, today):
    # Prefer the workshop date; without one, a paper deadline in the last ~4 months
    # or a venue created in the last ~7 months still means it may be recruiting.
    if v["start_date"]:
        return v["start_date"] >= today
    if v["deadline"]:
        return v["deadline"] >= today - dt.timedelta(days=120)
    return v["created"] >= today - dt.timedelta(days=210)


# ---------- 2. crawl ----------

def normalize(page):
    # Google Sites wraps outbound links as google.com/url?q=<encoded>; decode so
    # the form regex sees them.
    return urllib.parse.unquote(html.unescape(page))


def context_of(page, start, end, width):
    snippet = page[max(0, start - width):end + width]
    return re.sub(r"\s+", " ", TAG_RE.sub(" ", snippet)).strip()


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
    rev_re = re.compile(cfg["reviewer_keywords"], re.I)
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
        page = normalize(raw)
        for m in FORM_RE.finditer(page):
            form = m.group(0)
            ctx = context_of(page, m.start(), m.end(), cfg["context_chars"])
            kind = "reviewer" if rev_re.search(ctx) else "other"
            prev = found.get(form)
            if not prev or (prev["kind"] == "other" and kind == "reviewer"):
                found[form] = {"form": form, "kind": kind, "page": url, "context": ctx[:300]}
    return list(found.values()), err


# ---------- 3. report ----------

def notify(n):
    msg = f"{n} new workshop reviewer form(s) - see state/report.md"
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Workshop reviewer forms"'],
                   check=False, capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="also list forms not labelled reviewer")
    ap.add_argument("--limit", type=int, default=0, help="crawl only the first N workshops (testing)")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    today = dt.date.today()
    STATE.mkdir(exist_ok=True)

    ids = discover_ids(cfg)
    venues = [v for v in fetch_venues(ids) if is_upcoming(v, today)]
    if args.limit:
        venues = venues[:args.limit]
    print(f"{len(ids)} workshop venues on OpenReview, {len(venues)} upcoming; crawling...", file=sys.stderr)

    # Several OpenReview venues (main track, ARR commitment, shared task) often
    # share one website, so crawl each site once and merge rows per form.
    by_site = {}
    for v in venues:
        by_site.setdefault(v["website"], []).append(v)
    sites = [vs[0] for vs in by_site.values()]
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
                dls = [g["deadline"] for g in group if g["deadline"]]
                r["deadline"] = r["deadline"] or (max(dls) if dls else None)
    rows = list(by_form.values())

    seen_path = STATE / "seen.json"
    seen = json.loads(seen_path.read_text()) if seen_path.exists() else {}
    first_run = not seen
    for r in rows:
        r["first_seen"] = seen.get(r["form"], today.isoformat())
        r["new"] = r["form"] not in seen
        seen.setdefault(r["form"], today.isoformat())
    seen_path.write_text(json.dumps(seen, indent=1, sort_keys=True))

    shown = [r for r in rows if args.all or r["kind"] == "reviewer"]
    far = dt.date(9999, 1, 1)
    shown.sort(key=lambda r: (not r["new"] or first_run, r["start_date"] or far, r["title"]))

    with open(STATE / "report.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, ["new", "kind", "title", "venue_id", "deadline", "start_date",
                                "form", "website", "page", "first_seen", "context"])
        w.writeheader()
        for r in sorted(rows, key=lambda r: (r["kind"] != "reviewer", r["start_date"] or far)):
            w.writerow({k: r[k] for k in w.fieldnames})

    lines = [f"# Workshop reviewer forms — {today}", "",
             f"{len(venues)} upcoming workshop venues ({len(sites)} sites) crawled ({failures} sites unreachable); "
             f"{sum(r['kind'] == 'reviewer' for r in rows)} reviewer forms, "
             f"{sum(r['new'] and r['kind'] == 'reviewer' for r in rows)} new.", "",
             "| new | workshop | workshop date | paper deadline | form |", "|---|---|---|---|---|"]
    for r in shown:
        title = r["title"].replace("|", "/")
        lines.append(f"| {'🆕' if r['new'] and not first_run else ''} | [{title}]({r['website']}) "
                     f"| {r['start_date'] or '?'} | {r['deadline'] or '?'} | {r['form']} |")
    (STATE / "report.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))

    new_rev = sum(r["new"] and r["kind"] == "reviewer" for r in rows)
    if new_rev and not first_run and not args.no_notify:
        notify(new_rev)


if __name__ == "__main__":
    main()
