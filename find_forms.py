#!/usr/bin/env python3
"""Find Google Forms that upcoming workshops use to recruit reviewers.

Pipeline:
  1. Discover workshops from OpenReview: active venues, plus every
     <conference>/<this or next year>/Workshop/ prefix derived from them.
  2. Keep workshops that have not taken place yet, and look up each one's paper
     deadline (the Submission invitation's due date) for reference.
  3. Crawl each website: the homepage plus a few same-site pages whose link
     text or URL looks reviewer-related (call for reviewers, PC, committee, ...).
  4. Pull out forms.gle / docs.google.com/forms links, label each one by its link
     text (or the sentence before it), and keep reviewer forms that Google still
     shows as accepting responses.
  5. Diff against earlier runs, write site/index.html (the viewer), site/data.json
     and state/report.csv, and raise a macOS notification for new reviewer forms.
     With --publish, the page is also rewritten and published while the crawl runs.

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
ANY_YEAR_RE = re.compile(r"(?<!\d)(20\d\d)(?!\d)")
PAST_EDITION_RE = re.compile(r"previous|past[-_ /]|archive|older|history", re.I)
# Short years in URL slugs such as "social-sim26" or "cvpr25".
SLUG_YEAR_RE = re.compile(r"[a-z_-](2\d)(?=[/_-]|$)", re.I)


def is_past_edition(text, this_year, slugs=False):
    """A page or sentence about an earlier edition: it says previous/past/archive,
    or every year it mentions is before this_year. With slugs, also read two-digit
    years from URL slugs ("social-sim26")."""
    if PAST_EDITION_RE.search(text):
        return True
    years = {int(y) for y in ANY_YEAR_RE.findall(text)}
    if slugs:
        years |= {2000 + int(y) for y in SLUG_YEAR_RE.findall(text)}
    return bool(years) and max(years) < this_year


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


def is_upcoming(v, today):
    """The workshop has not taken place yet. Whether it still recruits reviewers is
    decided later by whether its form is still open, not by the paper deadline."""
    if v["start_date"]:
        return v["start_date"] >= today
    # No workshop date: a recent paper deadline or a recently created venue.
    if v["deadline"]:
        return v["deadline"] >= today - dt.timedelta(days=120)
    return v["created"] >= today - dt.timedelta(days=180)


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
    m = YEAR_RE.match(v.get("venue_id", ""))
    edition = int(m.group(2)) if m else dt.date.today().year
    found, err = {}, ""
    try:
        home, final = get(site)
    except Exception as e:  # noqa: BLE001 - any fetch failure is just reported
        return [], f"fetch failed: {type(e).__name__}"
    pages = [(final, home)]
    seen = {final.split("#")[0]}
    for href, text in HREF_RE.findall(home):
        url = urllib.parse.urljoin(final, html.unescape(href)).split("#")[0]
        label_text = url + " " + TAG_RE.sub(" ", text)
        if url in seen or not same_site(final, url) or not sub_re.search(label_text):
            continue
        # Old editions often keep their old (never closed) reviewer forms; compare
        # against the year of this workshop's own edition.
        path = urllib.parse.urlparse(url).path
        if is_past_edition(path, edition, slugs=True) or is_past_edition(TAG_RE.sub(" ", text), edition):
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
            # "...reviewers for our next workshop in 2022 (apply through this form)"
            if kind == "reviewer" and is_past_edition(ctx.rsplit("[", 1)[0][-250:], dt.date.today().year) \
                    and not PAST_EDITION_RE.search(urllib.parse.urlparse(final).path):
                kind = "other"
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


class Publisher:
    """Runs a publish command (e.g. ./deploy.sh) at most every `every` seconds while
    the crawl is running, without waiting for it, and skips a round if the previous
    one is still going."""

    def __init__(self, cmd, every):
        self.cmd, self.every, self.proc = cmd, every, None
        self.last = time.monotonic()

    def due(self):
        return bool(self.cmd) and time.monotonic() - self.last >= self.every \
            and (self.proc is None or self.proc.poll() is not None)

    def publish(self, wait=False):
        if not self.cmd:
            return
        if wait and self.proc is not None:
            self.proc.wait()
        self.last = time.monotonic()
        self.proc = subprocess.Popen(self.cmd, shell=True, cwd=HERE)
        if wait and self.proc.wait() != 0:
            print(f"publish command failed: {self.cmd}", file=sys.stderr)


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
    ap.add_argument("--publish", metavar="CMD",
                    help="shell command that publishes site/ (e.g. ./deploy.sh); run during the crawl and at the end")
    ap.add_argument("--update-every", type=int, default=60, metavar="SEC",
                    help="with --publish, refresh the published page this often while crawling (default 60)")
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
    venues = [v for v in fetch_venues(ids) if is_upcoming(v, today)]
    if args.limit:
        venues = venues[:args.limit]

    # Several OpenReview venues (main track, ARR commitment, shared task) often
    # share one website, so crawl each site once and merge rows per form.
    by_site = {}
    for v in venues:
        by_site.setdefault(v["website"], []).append(v)
    sites = [vs[0] for vs in by_site.values()]
    print(f"{len(ids)} workshop venues, {len(venues)} still ahead; "
          f"crawling {len(sites)} websites...", file=sys.stderr)
    seen = load_seen(args.previous)
    first_run = not seen
    far = dt.date(9999, 1, 1)
    by_form, done, failures = {}, 0, 0
    keep = (lambda f: True) if args.all else (lambda f: f["kind"] == "reviewer")

    def check(v):
        forms, err = crawl(v, cfg)
        # Check forms right away, so every partial page lists only open forms.
        for f in forms:
            if keep(f):
                f["open"] = form_is_open(f["form"])
        return v, forms, err

    def snapshot():
        rows = []
        for r in by_form.values():
            if keep(r) and r.get("open"):
                row = {k: x for k, x in r.items() if k not in ("created", "submission_id", "open")}
                row["first_seen"] = seen.get(r["form"], today.isoformat())
                row["new"] = not first_run and r["form"] not in seen
                rows.append(row)
        rows.sort(key=lambda r: (r["start_date"] or far, r["title"]))
        return rows

    def summary(partial):
        return {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                "today": today.isoformat(), "venues": len(venues), "sites": done,
                "sites_total": len(sites), "partial": partial, "unreachable": failures,
                "closed": sum(keep(r) and r.get("open") is False for r in by_form.values())}

    publisher = Publisher(args.publish, args.update_every)
    with cf.ThreadPoolExecutor(cfg["workers"]) as pool:
        for fut in cf.as_completed([pool.submit(check, v) for v in sites]):
            v, forms, err = fut.result()
            done += 1
            failures += err.startswith("fetch failed")
            group = by_site[v["website"]]
            for f in forms:
                r = by_form.setdefault(f["form"], {**v, **f})
                if f["kind"] == "reviewer" and r["kind"] != "reviewer":
                    r.update(kind="reviewer", page=f["page"], context=f["context"])
                if keep(r) and "open" not in r:
                    r["open"] = form_is_open(r["form"])
                r["venue_id"] = " ".join(sorted({*r["venue_id"].split(), *(g["venue_id"] for g in group)}))
                dates = [g["start_date"] for g in group if g["start_date"]]
                r["start_date"] = r["start_date"] or (min(dates) if dates else None)
                # The latest deadline among the venues sharing this site.
                dls = [g["deadline"] for g in group if g["deadline"]]
                if dls:
                    r["deadline"] = max(dls)
            if publisher.due():
                write_site(snapshot(), summary(partial=True))
                publisher.publish()
                print(f"  {done}/{len(sites)} sites, {len(snapshot())} open forms so far", file=sys.stderr)

    rows = snapshot()
    for r in rows:
        seen.setdefault(r["form"], r["first_seen"])
    if not args.limit:
        (STATE / "seen.json").write_text(json.dumps(seen, indent=1, sort_keys=True))
    with open(STATE / "report.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, ["new", "kind", "conference", "title", "start_date", "deadline",
                                "form", "website", "page", "first_seen", "venue_id", "context"])
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in w.fieldnames})

    final = summary(partial=False)
    page = write_site(rows, final)
    publisher.publish(wait=True)
    n_rev = sum(r["kind"] == "reviewer" for r in rows)
    n_new = sum(r["new"] and r["kind"] == "reviewer" for r in rows)
    print(f"{n_rev} open reviewer forms ({n_new} new, {final['closed']} closed ones dropped) from "
          f"{len(sites)} sites, {failures} unreachable -> {page}", file=sys.stderr)

    if n_new and not args.no_notify and sys.platform == "darwin":
        notify(n_new)
    if not args.no_open and not os.environ.get("CI"):
        webbrowser.open(page.as_uri())


if __name__ == "__main__":
    main()
