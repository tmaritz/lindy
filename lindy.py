#!/usr/bin/env python3
"""lindy: re-rank trending GitHub repos by how likely they are to last.

Pulls repos from a few "what's hot" sources (Hacker News, GitHub Trending),
then scores each one on age, how steadily it has been committed to, and how
many people work on it. A young repo with a huge star count that has already
gone quiet gets marked down.

Writes LINDY.md and a standalone index.html. Standard library only. Set
GITHUB_TOKEN or run `gh auth login` for the higher API rate limit.
"""
import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

UA = "lindy/0.1 (github.com/DietrichGebert style trending filter)"


def gh_token():
    if os.environ.get("GITHUB_TOKEN"):
        return os.environ["GITHUB_TOKEN"]
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except Exception:
        return None


TOKEN = gh_token()


def log(msg):
    print(msg, file=sys.stderr)


def warn(msg):
    print(f"warn: {msg}", file=sys.stderr)


def fetch(url, accept=None, auth=False):
    headers = {"User-Agent": UA}
    if accept:
        headers["Accept"] = accept
    if auth and TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.status, r.read().decode("utf-8", "replace"), dict(r.headers)


def gh_api(path, accept="application/vnd.github+json"):
    """GitHub REST call with retry for 202 (stats warming up) and rate limits."""
    url = "https://api.github.com" + path
    for attempt in range(4):
        try:
            status, body, hdrs = fetch(url, accept, auth=True)
            if status == 202:  # stats endpoint still computing
                time.sleep(2 * (attempt + 1))
                continue
            return (json.loads(body) if body.strip() else None), hdrs
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, {}
            if e.code in (403, 429) and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            raise
    return None, {}


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #

REPO_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*?)(?:[/#?].*)?$"
)
NON_REPO_OWNERS = {
    "marketplace", "topics", "sponsors", "about", "features", "collections",
    "trending", "orgs", "apps", "settings", "pricing", "customer-stories",
    "readme", "explore", "notifications", "new", "login", "join",
}


def normalise(slug):
    parts = slug.strip().strip("/").split("/")
    if len(parts) < 2:
        return None
    owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
    if owner.lower() in NON_REPO_OWNERS:
        return None
    if not re.match(r"^[\w.-]+$", repo) or repo in {".", ".."}:
        return None
    return f"{owner}/{repo}"


def source_hn(min_points=120):
    """High-scoring Hacker News stories whose link points at a GitHub repo."""
    found = {}
    url = (
        "https://hn.algolia.com/api/v1/search_by_date?tags=story"
        f"&numericFilters=points%3E{min_points}"
        "&query=github.com&restrictSearchableAttributes=url&hitsPerPage=200"
    )
    try:
        _, body, _ = fetch(url)
        hits = json.loads(body).get("hits", [])
    except Exception as e:
        warn(f"hn source failed: {e}")
        return found
    for hit in hits:
        m = REPO_URL_RE.search(hit.get("url") or "")
        if not m:
            continue
        slug = normalise(m.group(1))
        if slug:
            found.setdefault(slug, set()).add("hn")
    return found


def source_gh_trending(ranges=("daily", "weekly")):
    """Scrape github.com/trending. Breaks if GitHub changes the markup."""
    found = {}
    row_re = re.compile(
        r'<h2[^>]*class="[^"]*lh-condensed[^"]*"[^>]*>\s*<a[^>]+href="/([^"]+)"'
    )
    for rng in ranges:
        try:
            _, body, _ = fetch(
                f"https://github.com/trending?since={rng}", accept="text/html"
            )
        except Exception as e:
            warn(f"gh-trending {rng} failed: {e}")
            continue
        hits = row_re.findall(body)
        if not hits:
            warn(f"gh-trending {rng}: no rows matched (page layout changed?)")
        for raw in hits:
            slug = normalise(raw)
            if slug:
                found.setdefault(slug, set()).add(f"gh-trending-{rng}")
    return found


def source_trendshift():
    """Pull repo slugs out of trendshift.io's embedded page data."""
    found = {}
    try:
        _, body, _ = fetch("https://trendshift.io/", accept="text/html")
    except Exception as e:
        warn(f"trendshift source failed: {e}")
        return found
    slugs = re.findall(r'"full_name"\s*:\s*"([\w.-]+/[\w.-]+)"', body)
    if not slugs:
        warn("trendshift: no repo slugs found in page (layout changed?)")
    for raw in slugs:
        slug = normalise(raw)
        if slug:
            found.setdefault(slug, set()).add("trendshift")
    return found


def gather():
    merged = {}
    for src in (source_hn, source_gh_trending, source_trendshift):
        for slug, tags in src().items():
            merged.setdefault(slug, set()).update(tags)
    return merged


# --------------------------------------------------------------------------- #
# enrichment + scoring
# --------------------------------------------------------------------------- #

def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def active_months(slug):
    """(months with >=1 commit in last year, total commits in last year)."""
    data, _ = gh_api(f"/repos/{slug}/stats/commit_activity")
    if not isinstance(data, list) or not data:
        return None, None
    weeks = data[-52:]
    buckets = [0] * 12
    for i, w in enumerate(weeks):
        buckets[min(11, i * 12 // len(weeks))] += w.get("total", 0)
    return sum(1 for b in buckets if b > 0), sum(buckets)


def contributor_count(slug):
    try:
        _, body, hdrs = fetch(
            f"https://api.github.com/repos/{slug}/contributors?per_page=1&anon=1",
            accept="application/vnd.github+json",
            auth=True,
        )
    except Exception:
        return None
    m = re.search(r'[?&]page=(\d+)>;\s*rel="last"', hdrs.get("Link", ""))
    if m:
        return int(m.group(1))
    try:
        return len(json.loads(body))
    except Exception:
        return None


def score(r):
    """0-100 score plus a short plain-text explanation."""
    reasons, s = [], 0.0
    age_m = r["age_days"] / 30.4

    s += min(age_m / 24, 1.0) * 25           # age, capped at two years
    if age_m >= 24:
        reasons.append("2y+ old")
    elif age_m < 3:
        reasons.append("only weeks old")

    if r["active_months"] is not None:       # months with commits, last year
        s += r["active_months"] / 12 * 30
        if r["active_months"] >= 10:
            reasons.append("commits almost every month")
        elif r["active_months"] <= 3:
            reasons.append("sporadic commits")
    else:
        s += 10

    if r["contributors"] is not None:        # how many people work on it
        s += min(r["contributors"] / 10, 1.0) * 20
        if r["contributors"] >= 10:
            reasons.append(f"{r['contributors']}+ contributors")
        elif r["contributors"] <= 2:
            reasons.append("1-2 contributors")
    else:
        s += 8

    if r["has_release"]:
        s += 8
        reasons.append("tagged releases")

    vel = r["stars"] / max(r["age_days"], 1)
    if age_m < 3 and r["stars"] > 3000:      # young and already huge: probably a spike
        s -= min((r["stars"] / 1000) * (3 - age_m), 45)
        reasons.append(f"star spike (~{vel:.0f}/day)")
    if r["quiet_days"] > 21:                 # not touched in a while
        s -= min((r["quiet_days"] - 21) / 3, 20)
        reasons.append(f"no push in {r['quiet_days']}d")

    return round(max(0.0, min(100.0, s)), 1), "; ".join(reasons)


def enrich(slug, sources):
    repo, _ = gh_api(f"/repos/{slug}")
    if not repo:
        return None
    if repo.get("archived") or repo.get("disabled") or repo.get("fork"):
        return None
    now = datetime.now(timezone.utc)
    am, yr_commits = active_months(slug)
    rec = {
        "slug": slug,
        "sources": sorted(sources),
        "stars": repo["stargazers_count"],
        "forks": repo["forks_count"],
        "open_issues": repo["open_issues_count"],
        "age_days": max((now - parse_dt(repo["created_at"])).days, 1),
        "quiet_days": (now - parse_dt(repo["pushed_at"])).days,
        "active_months": am,
        "year_commits": yr_commits,
        "contributors": contributor_count(slug),
        "has_release": bool(gh_api(f"/repos/{slug}/releases?per_page=1")[0]),
        "license": (repo.get("license") or {}).get("spdx_id"),
        "language": repo.get("language"),
        "desc": (repo.get("description") or "").strip(),
    }
    rec["score"], rec["why"] = score(rec)
    return rec


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def human_age(days):
    if days >= 365:
        return f"{days // 365}y"
    if days >= 30:
        return f"{days // 30}mo"
    return f"{days}d"


def src_label(s):
    return s.replace("gh-trending-", "gh:")


def write_markdown(recs, path, stamp):
    lines = [
        "# lindy",
        "",
        "Trending GitHub repos, re-sorted by how likely they are to still be "
        "maintained in a few years. See the README for how the score works.",
        "",
        f"_Updated {stamp:%Y-%m-%d %H:%M UTC}. {len(recs)} repos._",
        "",
        "| # | Repo | Score | Stars | Age | Act. mo | Contrib | Sources | Notes |",
        "|--:|------|------:|------:|----:|:------:|--------:|---------|-------|",
    ]
    for i, r in enumerate(recs, 1):
        lines.append(
            f"| {i} | [{r['slug']}](https://github.com/{r['slug']}) "
            f"| **{r['score']}** | {r['stars']:,} | {human_age(r['age_days'])} "
            f"| {r['active_months'] if r['active_months'] is not None else '—'} "
            f"| {r['contributors'] if r['contributors'] is not None else '—'} "
            f"| {', '.join(src_label(s) for s in r['sources'])} | {r['why']} |"
        )
    lines += ["", "## Descriptions", ""]
    for r in recs:
        lines.append(
            f"- **[{r['slug']}](https://github.com/{r['slug']})** "
            f"({r['language'] or 'n/a'}): {r['desc'] or '_no description_'}"
        )
    open(path, "w").write("\n".join(lines) + "\n")


HTML_HEAD = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>lindy</title>
<style>
  :root { color-scheme: light dark; --fg:#1a1a1a; --bg:#fff; --mut:#666;
          --line:#e3e3e3; --acc:#0969da; --row:#fafafa; }
  @media (prefers-color-scheme: dark) {
    :root { --fg:#e6e6e6; --bg:#0d1117; --mut:#8b949e; --line:#30363d;
            --acc:#58a6ff; --row:#161b22; }
  }
  * { box-sizing: border-box; }
  body { margin:0 auto; max-width:1100px; padding:2.5rem 1.25rem 4rem;
         font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         color:var(--fg); background:var(--bg); }
  h1 { font-size:1.9rem; margin:0 0 .3rem; letter-spacing:-.02em; }
  p.sub { color:var(--mut); margin:.2rem 0 1.6rem; }
  .wrap { overflow-x:auto; border:1px solid var(--line); border-radius:8px; }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th, td { padding:.55rem .7rem; text-align:left; border-bottom:1px solid var(--line);
           white-space:nowrap; }
  th { position:sticky; top:0; background:var(--bg); font-weight:600;
       border-bottom:2px solid var(--line); }
  tr:nth-child(even) td { background:var(--row); }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  td.notes { white-space:normal; color:var(--mut); min-width:16rem; }
  a { color:var(--acc); text-decoration:none; }
  a:hover { text-decoration:underline; }
  .score { font-weight:700; }
  .pill { display:inline-block; padding:.05rem .4rem; margin:0 .15rem .15rem 0;
          font-size:11px; border:1px solid var(--line); border-radius:999px;
          color:var(--mut); }
  footer { margin-top:2rem; color:var(--mut); font-size:13px; }
  code { background:var(--row); padding:.1rem .3rem; border-radius:4px; }
</style>
"""


def write_html(recs, path, stamp):
    rows = []
    for i, r in enumerate(recs, 1):
        pills = "".join(
            f'<span class="pill">{html.escape(src_label(s))}</span>'
            for s in r["sources"]
        )
        rows.append(
            "<tr>"
            f'<td class="num">{i}</td>'
            f'<td><a href="https://github.com/{html.escape(r["slug"])}">'
            f'{html.escape(r["slug"])}</a>'
            f'<div class="pill-row">{pills}</div></td>'
            f'<td class="num score">{r["score"]}</td>'
            f'<td class="num">{r["stars"]:,}</td>'
            f'<td class="num">{human_age(r["age_days"])}</td>'
            f'<td class="num">{r["active_months"] if r["active_months"] is not None else "&mdash;"}</td>'
            f'<td class="num">{r["contributors"] if r["contributors"] is not None else "&mdash;"}</td>'
            f'<td>{html.escape(r["language"] or "")}</td>'
            f'<td class="notes">{html.escape(r["desc"] or "")}'
            f'{" &mdash; <em>" + html.escape(r["why"]) + "</em>" if r["why"] else ""}</td>'
            "</tr>"
        )
    doc = (
        HTML_HEAD
        + "<h1>lindy</h1>\n"
        + '<p class="sub">Trending GitHub repos, re-sorted by how likely they '
        "are to still be maintained in a few years. Sources: Hacker News and "
        "GitHub Trending. "
        '<a href="https://github.com/tmaritz/lindy">How the score works.</a><br>'
        f"Updated {stamp:%Y-%m-%d %H:%M UTC}, {len(recs)} repos.</p>\n"
        '<div class="wrap"><table>\n<thead><tr>'
        '<th class="num">#</th><th>Repo</th><th class="num">Score</th>'
        '<th class="num">Stars</th><th class="num">Age</th>'
        '<th class="num">Act. mo</th><th class="num">Contrib</th>'
        "<th>Lang</th><th>Notes</th>"
        "</tr></thead>\n<tbody>\n"
        + "\n".join(rows)
        + "\n</tbody></table></div>\n"
        '<footer>Named after the Lindy effect. '
        '<a href="https://github.com/tmaritz/lindy">Source.</a></footer>\n'
    )
    open(path, "w").write(doc)


# --------------------------------------------------------------------------- #

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=60,
                    help="max repos to enrich, prioritised by source count (default 60)")
    ap.add_argument("--min-score", type=float, default=0.0,
                    help="drop repos scoring below this (default 0)")
    ap.add_argument("--md", default="LINDY.md", help="markdown output path")
    ap.add_argument("--html", default="index.html", help="html output path")
    ap.add_argument("--json", default=None, help="optional json dump path")
    args = ap.parse_args(argv)

    if not TOKEN:
        warn("no GitHub token - REST limited to 60 req/hr "
             "(set GITHUB_TOKEN or run `gh auth login`)")

    merged = gather()
    log(f"{len(merged)} unique repos from sources")
    ordered = sorted(merged.items(), key=lambda kv: len(kv[1]), reverse=True)
    ordered = ordered[: args.limit]

    recs = []
    for slug, sources in ordered:
        try:
            rec = enrich(slug, sources)
        except urllib.error.HTTPError as e:
            warn(f"{slug}: {e}")
            continue
        if rec and rec["score"] >= args.min_score:
            recs.append(rec)
            log(f"  {rec['score']:5.1f}  {slug}")
        time.sleep(0.15)

    recs.sort(key=lambda r: r["score"], reverse=True)
    stamp = datetime.now(timezone.utc)
    write_markdown(recs, args.md, stamp)
    write_html(recs, args.html, stamp)
    if args.json:
        json.dump(recs, open(args.json, "w"), indent=2)
    log(f"wrote {args.md} and {args.html} ({len(recs)} repos)")


if __name__ == "__main__":
    main()
