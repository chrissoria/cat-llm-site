#!/usr/bin/env python3
"""Collect third-party mentions of CatLLM from public APIs.

Nothing this script finds is published. It writes candidates to
assets/mentions-pending.json for review; only entries hand-moved into
assets/mentions.json are rendered on the site. See scripts/README.md.

Standard library only, so the workflow needs no pip install.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")

APPROVED = os.path.join(ASSETS, "mentions.json")
PENDING = os.path.join(ASSETS, "mentions-pending.json")
REJECTED = os.path.join(ASSETS, "mentions-rejected.json")

CONTACT = "chrissoria@berkeley.edu"
UA = f"catllm-site-mentions/1.0 (+https://catllm.com; {CONTACT})"

# Works whose citations we track. Add preprint DOIs here as they publish.
TRACKED_DOIS = [
    "10.21105/joss.09678",   # JOSS software paper
    "10.5281/zenodo.15532316",  # Zenodo concept DOI (all versions)
]

# "CatLLM" and "cat-llm" are both this project, so name searches cover both
# spellings plus the sibling packages.
PROJECT_NAMES = [
    "CatLLM", "cat-llm", "cat-stack", "cat-pol", "cat-vader",
    "cat-ademic", "cat-web", "cat-survey", "cat-cog",
]

# Code search is the opposite trade: precision over recall. Unrelated
# projects share the name (TaoZhen1110/CAT-LLM, duowuyms/OpenCATP-LLM,
# ARP02000/CatLLM), so here we match only import and install lines, which
# no same-name project would produce.
CODE_QUERIES = [
    '"import catllm"',
    '"from catllm import"',
    '"pip install cat-llm"',
    '"library(cat.llm)"',
    '"install.packages(\\"cat.llm\\")"',
    '"from catsurvey import"',
    '"import catpol"',
    '"import catvader"',
]

# Our own repos and the journal's paper archive are not third-party usage.
OWN_ACCOUNTS = {"chrissoria", "openjournals"}


def fetch(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def warn(msg):
    print(f"  ! {msg}", file=sys.stderr)


def load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        warn(f"could not read {os.path.basename(path)}: {e}")
        return default


def norm(url):
    """Normalize a URL so the same page found twice dedupes to one entry."""
    u = (url or "").strip().lower()
    for prefix in ("https://", "http://", "www."):
        if u.startswith(prefix):
            u = u[len(prefix):]
    return u.rstrip("/")


# ── Sources ──────────────────────────────────────────────────────────
# Each returns a list of dicts and swallows its own errors, so one dead
# API cannot take down the whole run.

def from_openalex():
    out = []
    for doi in TRACKED_DOIS:
        try:
            work = fetch(f"https://api.openalex.org/works/doi:{doi}?mailto={CONTACT}")
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            warn(f"openalex lookup {doi}: {e}")
            continue
        wid = (work.get("id") or "").rsplit("/", 1)[-1]
        if not wid:
            continue
        try:
            cites = fetch(
                f"https://api.openalex.org/works?filter=cites:{wid}"
                f"&per-page=50&mailto={CONTACT}"
            )
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            warn(f"openalex cites {wid}: {e}")
            continue
        for w in cites.get("results", []):
            out.append({
                "title": w.get("title") or "(untitled)",
                "url": w.get("doi") or w.get("id"),
                "source": "openalex",
                "kind": "citation",
                "date": str(w.get("publication_year") or ""),
                "author": ((w.get("authorships") or [{}])[0]
                           .get("author", {}).get("display_name", "")),
            })
    return out


def from_semantic_scholar():
    out = []
    for doi in TRACKED_DOIS:
        url = (f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}"
               f"/citations?fields=title,year,externalIds,authors&limit=50")
        try:
            data = fetch(url)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            warn(f"semanticscholar {doi}: {e}")
            continue
        for item in data.get("data", []):
            p = item.get("citingPaper", {})
            ext = p.get("externalIds") or {}
            link = (f"https://doi.org/{ext['DOI']}" if ext.get("DOI")
                    else f"https://www.semanticscholar.org/paper/{p.get('paperId','')}")
            authors = p.get("authors") or []
            out.append({
                "title": p.get("title") or "(untitled)",
                "url": link,
                "source": "semanticscholar",
                "kind": "citation",
                "date": str(p.get("year") or ""),
                "author": authors[0].get("name", "") if authors else "",
            })
    return out


def from_github(token):
    """Code search needs a PAT; the ephemeral GITHUB_TOKEN is not enough.

    Two traps here, both hit during development:

    1. The REST code-search index tokenizes and prefix-matches rather than
       matching phrases, so '"import catpol"' returned 20 files whose real
       content was 'import CatPolicy' or 'import CatPolBadge'. We ask for
       text-match fragments and keep a hit only if the literal phrase appears
       in one AND is not glued to more word characters.
    2. Code search allows 10 requests/minute, and firing all queries back to
       back trips a 403. Hence the sleep between calls.
    """
    if not token:
        warn("no MENTIONS_TOKEN set, skipping GitHub code search")
        return []
    out = []
    filtered = 0
    headers = {
        "Authorization": f"Bearer {token}",
        # text-match gives us the surrounding fragment to verify against.
        "Accept": "application/vnd.github.text-match+json",
    }
    for i, q in enumerate(CODE_QUERIES):
        if i:
            time.sleep(7)  # stay under 10 req/min
        literal = q.strip('"').replace('\\"', '"').lower()
        url = ("https://api.github.com/search/code?q="
               + urllib.parse.quote(q + " -user:chrissoria") + "&per_page=20")
        try:
            data = fetch(url, headers=headers)
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
            warn(f"github {q}: {e}")
            continue
        for item in data.get("items", []):
            repo = item.get("repository", {})
            owner = repo.get("owner", {}).get("login", "")
            if owner in OWN_ACCOUNTS:
                continue
            fragments = " ".join(
                m.get("fragment", "") for m in item.get("text_matches", [])
            ).lower()
            # Bare substring is not enough: "import catpol" is a prefix of
            # "import catpolicy". Require no trailing word character.
            pattern = (r"(?<![A-Za-z0-9_])" + re.escape(literal)
                       + r"(?![A-Za-z0-9_])")
            if not re.search(pattern, fragments):
                filtered += 1
                continue
            out.append({
                "title": f"{repo.get('full_name','')} · {item.get('path','')}",
                "url": item.get("html_url", ""),
                "source": "github",
                "kind": "code",
                "date": "",
                "author": owner,
                "matched": q,
            })
    if filtered:
        print(f"    (dropped {filtered} tokenizer false positive(s))")
    return out


def from_hackernews():
    out = []
    query = " OR ".join(f'"{n}"' for n in PROJECT_NAMES)
    url = ("https://hn.algolia.com/api/v1/search?query="
           + urllib.parse.quote(query) + "&hitsPerPage=30")
    try:
        data = fetch(url)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as e:
        warn(f"hackernews: {e}")
        return out
    for h in data.get("hits", []):
        out.append({
            "title": h.get("title") or h.get("story_title") or "(untitled)",
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
            "source": "hackernews",
            "kind": "discussion",
            "date": (h.get("created_at") or "")[:10],
            "author": h.get("author", ""),
        })
    return out


def from_bluesky():
    """Public search returned 403 from some networks; treated as optional."""
    out = []
    for name in ("cat-llm", "CatLLM"):
        url = ("https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts?q="
               + urllib.parse.quote(name) + "&limit=25")
        try:
            data = fetch(url)
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, TimeoutError) as e:
            warn(f"bluesky {name} (optional): {e}")
            continue
        for p in data.get("posts", []):
            handle = p.get("author", {}).get("handle", "")
            rkey = (p.get("uri") or "").rsplit("/", 1)[-1]
            text = (p.get("record", {}).get("text") or "").replace("\n", " ")
            out.append({
                "title": text[:160],
                "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
                "source": "bluesky",
                "kind": "post",
                "date": (p.get("indexedAt") or "")[:10],
                "author": handle,
            })
    return out


def main():
    approved = load(APPROVED, {"mentions": []})
    pending = load(PENDING, {"mentions": []})
    rejected = load(REJECTED, {"urls": []})

    # Anything already approved, already queued, or previously dismissed is
    # not new. Without the rejected list, known false positives such as
    # HETDEX/elixer matching "cat_stack" would reappear every night.
    seen = {norm(m.get("url")) for m in approved.get("mentions", [])}
    seen |= {norm(m.get("url")) for m in pending.get("mentions", [])}
    seen |= {norm(u) for u in rejected.get("urls", [])}

    sources = [
        ("openalex", from_openalex),
        ("semanticscholar", from_semantic_scholar),
        ("github", lambda: from_github(os.environ.get("MENTIONS_TOKEN"))),
        ("hackernews", from_hackernews),
        ("bluesky", from_bluesky),
    ]

    found = []
    for name, fn in sources:
        try:
            hits = fn()
        except Exception as e:  # a source bug must not kill the run
            warn(f"{name} failed: {e}")
            hits = []
        print(f"  {name}: {len(hits)} hit(s)")
        found.extend(hits)

    today = str(date.today())
    new = []
    for m in found:
        key = norm(m.get("url"))
        if not key or key in seen:
            continue
        seen.add(key)
        m["found"] = today
        new.append(m)

    if new:
        pending.setdefault("mentions", []).extend(new)
        pending["updated"] = today
        with open(PENDING, "w") as f:
            json.dump(pending, f, indent=2, ensure_ascii=False)
            f.write("\n")

    print(f"\n{len(new)} new candidate(s); {len(pending.get('mentions', []))} pending total")

    # Hand the count and a summary to the workflow so it can open an issue.
    summary = "\n".join(
        f"- [{m['source']}] {m['title'][:110]}\n  {m['url']}" for m in new
    )
    step_out = os.environ.get("GITHUB_OUTPUT")
    if step_out:
        with open(step_out, "a") as f:
            f.write(f"new_count={len(new)}\n")
            f.write("summary<<MENTIONS_EOF\n" + summary + "\nMENTIONS_EOF\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
