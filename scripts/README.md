# Mention collection

`fetch_mentions.py` looks for third-party mentions of CatLLM in public APIs and
queues them for review. It runs nightly via `.github/workflows/mentions.yml`.

**Nothing it finds is published automatically.** Scraped results appear on
catllm.com under Chris's name, so an irrelevant hit, a spam page, or something
hostile would go live unreviewed. The script only ever writes to a pending file.

## The three files

| File | Role |
|---|---|
| `assets/mentions.json` | Reviewed and approved. This is what the site renders. |
| `assets/mentions-pending.json` | Unreviewed candidates. Never rendered. |
| `assets/mentions-rejected.json` | Dismissed URLs, so false positives stop coming back. |

## Reviewing

When the script finds something new it opens a GitHub issue listing the
candidates. For each one:

- **Real mention.** Move the entry from `mentions-pending.json` into the
  `mentions` array in `mentions.json`. Add a `note` field if it needs context.
- **False positive.** Add its URL to the `urls` array in
  `mentions-rejected.json` and delete it from pending.

Both files are plain JSON; edit by hand.

## Sources

| Source | Key needed | Notes |
|---|---|---|
| OpenAlex | No | Citations of the tracked DOIs. The main long-term signal. |
| Semantic Scholar | No | Second citation index. Returns 404 for the Zenodo DOI, which is expected and logged. |
| GitHub code search | Yes, `MENTIONS_TOKEN` | Fine-grained PAT with public-repo read. The built-in `GITHUB_TOKEN` cannot use code search. |
| Hacker News (Algolia) | No | Name search across both spellings. |
| Bluesky | No | Optional. The public endpoint 403s from some networks; failure is logged and skipped. |

LinkedIn, X, and Google Scholar are deliberately absent. None offers a
content-search API that permits this, and scraping them breaks their terms.
Mentions on those platforms have to arrive by someone telling us, which is what
the "Used CatLLM in your research?" section on the homepage is for.

## Two traps worth remembering

**GitHub code search prefix-matches.** Querying `"import catpol"` returns files
containing `import CatPolicy`. The script asks for `text-match` fragments and
re-checks each hit against the literal string with word boundaries. On the first
real run this dropped 20 of 20 hits, all false. Do not remove that check.

**Code search allows 10 requests per minute.** The script sleeps 7 seconds
between queries. Firing them back to back returns a 403.

## Running locally

```bash
MENTIONS_TOKEN=$(gh auth token) python3 scripts/fetch_mentions.py
```

Standard library only, so there is nothing to install.
