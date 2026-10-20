"""Scrape cloud/infra pain points from Reddit into a JSON file, then analyze with Claude.

Usage:
    python scraper.py                      # scrape: past year, 25 posts per keyword per sub
    python scraper.py --time month --limit 50 --comments 10 --out data.json
    python scraper.py --analyze reddit_pains_20261020_1200.json   # -> market_research_report.md
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import anthropic
import praw
from dotenv import load_dotenv
from prawcore.exceptions import PrawcoreException

SUBREDDITS = ["devops", "sysadmin", "aws", "azure", "cloud"]
KEYWORDS = [
    "bill surprise",
    "multi-cloud",
    "orphaned",
    "cost spike",
    "manual task",
]


def make_reddit():
    load_dotenv()
    missing = [k for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing in .env: {', '.join(missing)}")
    return praw.Reddit(
        client_id=os.getenv("REDDIT_CLIENT_ID"),
        client_secret=os.getenv("REDDIT_CLIENT_SECRET"),
        user_agent=os.getenv("REDDIT_USER_AGENT", "pain-scraper/0.1"),
        read_only=True,
    )


def top_comments(submission, n):
    submission.comment_sort = "top"
    submission.comments.replace_more(limit=0)  # skip "load more" stubs
    out = []
    for c in sorted(submission.comments, key=lambda c: c.score, reverse=True)[:n]:
        if c.body in ("[deleted]", "[removed]"):
            continue
        out.append({
            "author": str(c.author) if c.author else None,
            "score": c.score,
            "body": c.body,
            "created_utc": datetime.fromtimestamp(c.created_utc, timezone.utc).isoformat(),
        })
    return out


def _print_progress(event):
    if event["type"] == "search_start":
        print(f"r/{event['subreddit']}  \"{event['keyword']}\" ...", end=" ", flush=True)
    elif event["type"] == "search_done":
        print(event["found"] if not event.get("error") else f"error: {event['error']}")
    elif event["type"] == "chunk_start":
        print(f"Analyzing chunk {event['index']}/{event['total']} ({event['posts']} posts) ...", flush=True)
    elif event["type"] == "report_start":
        print(f"Writing report ({event['quotes']} verified quotes, {event['dropped']} unverifiable dropped) ...")


def scrape(reddit, time_filter, limit, n_comments, subreddits=SUBREDDITS, keywords=KEYWORDS,
           on_progress=_print_progress):
    """on_progress(event) is called around each search; it may raise to cancel the run."""
    posts = {}  # id -> post, dedupes across keywords
    for sub in subreddits:
        for kw in keywords:
            query = f'"{kw}"'  # exact phrase
            on_progress({"type": "search_start", "subreddit": sub, "keyword": kw})
            found = 0
            try:
                for s in reddit.subreddit(sub).search(query, sort="relevance",
                                                      time_filter=time_filter, limit=limit):
                    if s.id in posts:
                        posts[s.id]["matched_keywords"].append(kw)
                        continue
                    posts[s.id] = {
                        "id": s.id,
                        "subreddit": sub,
                        "matched_keywords": [kw],
                        "title": s.title,
                        "body": s.selftext,
                        "author": str(s.author) if s.author else None,
                        "score": s.score,
                        "num_comments": s.num_comments,
                        "url": f"https://www.reddit.com{s.permalink}",
                        "created_utc": datetime.fromtimestamp(s.created_utc, timezone.utc).isoformat(),
                        "top_comments": top_comments(s, n_comments),
                    }
                    found += 1
            except PrawcoreException as e:
                on_progress({"type": "search_done", "subreddit": sub, "keyword": kw, "found": found,
                             "error": str(e)})
                time.sleep(5)
                continue
            on_progress({"type": "search_done", "subreddit": sub, "keyword": kw, "found": found,
                         "total": len(posts)})
    return list(posts.values())


# ---------------------------------------------------------------- AI analysis

# claude-3-5-sonnet was retired by Anthropic on 2025-10-28; this is the current Sonnet.
MODEL = "claude-sonnet-5-5"
CHUNK_CHARS = 120_000  # ~30K tokens of posts per extraction call
REPORT_FILE = "market_research_report.md"

EXTRACT_SYSTEM = """You are a market researcher analyzing Reddit threads from cloud and \
infrastructure engineers. Extract evidence only from the posts given; \
never invent facts, quotes or URLs. Quotes must be copied character-for-character from a post \
or comment, 1-3 sentences, chosen to show real frustration. Skip posts that are off-topic \
(job hunting, homework, vendor ads)."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "pain_points": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "theme": {"type": "string", "description": "short label, e.g. 'unexpected NAT gateway charges'"},
                "description": {"type": "string"},
                "providers": {"type": "array", "items": {"type": "string"}},
                "post_urls": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["theme", "description", "providers", "post_urls"],
            "additionalProperties": False,
        }},
        "cross_cloud_friction": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "providers": {"type": "array", "items": {"type": "string"}},
                "issue": {"type": "string"},
                "post_url": {"type": "string"},
            },
            "required": ["providers", "issue", "post_url"],
            "additionalProperties": False,
        }},
        "quotes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "quote": {"type": "string"},
                "post_url": {"type": "string"},
                "subreddit": {"type": "string"},
                "theme": {"type": "string"},
            },
            "required": ["quote", "post_url", "subreddit", "theme"],
            "additionalProperties": False,
        }},
        "feature_ideas": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "idea": {"type": "string"},
                "pain_addressed": {"type": "string"},
            },
            "required": ["idea", "pain_addressed"],
            "additionalProperties": False,
        }},
    },
    "required": ["pain_points", "cross_cloud_friction", "quotes", "feature_ideas"],
    "additionalProperties": False,
}

REPORT_PROMPT = """Below are findings extracted from {n_posts} Reddit posts by cloud engineers \
(scraped {scraped_at}; subreddits: {subs}; search keywords: {kws}). Every quote has already been \
checked against the source text.

<findings>
{findings}
</findings>

Write a Market Validation Report in clean GitHub Markdown with exactly these sections:

# Market Validation Report: Cloud Infrastructure Pain Points
A 3-4 sentence executive summary and a one-line note on the data source and sample size.

## 1. Top 3 Recurring Technical Pain Points
Rank by how often a theme appears across posts (merge near-duplicate themes). For each: what it \
is, why it hurts (time, money, risk), which providers it shows up on, and how many posts mention it.

## 2. Cross-Cloud Friction (AWS vs Azure vs GCP)
A table comparing friction points per provider, then the specific pain of running more than one \
cloud (billing consolidation, IAM, tagging, tooling, networking). Say plainly where evidence is thin.

## 3. Product / Agent Feature Opportunities
5-8 concrete features, each tied to a pain point above. Every feature must be read-only or \
human-in-the-loop: it may read billing/usage/config data, detect, alert, explain and recommend, \
or generate a change (PR, script, ticket) for a human to review and apply. No feature may get \
write access to apply changes to production infrastructure on its own. For each, give: the \
feature, the pain it solves, the minimum read-only permissions it needs, and why an engineer \
would pay for it.

## 4. Voice of the Engineer
8-12 of the strongest quotes, verbatim, each as a blockquote followed by \
"— r/<subreddit>, [thread](<url>)". Use only quotes from the findings.

## 5. Caveats
Sampling bias (keyword search, Reddit audience), sample size, and what to validate next \
(e.g. customer interviews).

Output only the Markdown report."""


def _norm(s):
    return re.sub(r"\s+", " ", s).strip().lower()


def _format_post(p):
    lines = [f"### POST {p['url']} (r/{p['subreddit']}, score {p['score']})",
             f"TITLE: {p['title']}"]
    if p["body"]:
        lines.append(f"BODY: {p['body']}")
    for c in p["top_comments"]:
        lines.append(f"COMMENT (score {c['score']}): {c['body']}")
    return "\n".join(lines)


def _chunks(posts):
    """Group posts into chunks under CHUNK_CHARS. Posts are never truncated."""
    chunk, size = [], 0
    for p in posts:
        text = _format_post(p)
        if chunk and size + len(text) > CHUNK_CHARS:
            yield chunk
            chunk, size = [], 0
        chunk.append(text)
        size += len(text)
    if chunk:
        yield chunk


PRICE_PER_MTOK = {"input": 2.00, "output": 10.00}  # claude-sonnet-5-5, USD


def _ask_claude(client, system, user, effort, schema=None, usage=None):
    output_config = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=64000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config=output_config,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # a safety decline gets re-run on Anthropic's recommended fallback model
    ) as stream:
        msg = stream.get_final_message()
    if usage is not None:
        usage["input_tokens"] = usage.get("input_tokens", 0) + msg.usage.input_tokens
        usage["output_tokens"] = usage.get("output_tokens", 0) + msg.usage.output_tokens
        usage["cost_usd"] = round(usage["input_tokens"] / 1e6 * PRICE_PER_MTOK["input"]
                                  + usage["output_tokens"] / 1e6 * PRICE_PER_MTOK["output"], 4)
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined: {msg.stop_details}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("Response hit max_tokens; lower CHUNK_CHARS and retry")
    return next(b.text for b in msg.content if b.type == "text")


def analyze(json_path, out_path=REPORT_FILE, on_progress=_print_progress, usage=None):
    """Read scraped Reddit JSON, extract findings chunk by chunk, then write the Markdown report."""
    load_dotenv()
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    posts = data["posts"]
    if not posts:
        sys.exit("No posts in file.")

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    chunks = list(_chunks(posts))
    findings = {"pain_points": [], "cross_cloud_friction": [], "quotes": [], "feature_ideas": []}
    dropped = 0

    for i, chunk in enumerate(chunks, 1):
        on_progress({"type": "chunk_start", "index": i, "total": len(chunks), "posts": len(chunk)})
        text = "\n\n".join(chunk)
        result = json.loads(_ask_claude(
            client, EXTRACT_SYSTEM,
            f"Extract pain points, cross-cloud friction, verbatim quotes and feature ideas from these posts:\n\n{text}",
            effort="medium", schema=EXTRACT_SCHEMA, usage=usage,
        ))
        # Keep only quotes that really appear in the source text
        source = _norm(text)
        verified = [q for q in result["quotes"] if _norm(q["quote"]) in source]
        dropped += len(result["quotes"]) - len(verified)
        result["quotes"] = verified
        for k in findings:
            findings[k].extend(result[k])

    on_progress({"type": "report_start", "quotes": len(findings["quotes"]), "dropped": dropped})
    report = _ask_claude(
        client,
        "You are a senior market analyst writing for a founder deciding what cloud tooling product to build.",
        REPORT_PROMPT.format(
            n_posts=len(posts),
            scraped_at=data.get("scraped_at", "unknown"),
            subs=", ".join(data.get("subreddits", SUBREDDITS)),
            kws=", ".join(data.get("keywords", KEYWORDS)),
            findings=json.dumps(findings, ensure_ascii=False, indent=1),
        ),
        effort="high", usage=usage,
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report.strip() + "\n")
    print(f"Saved -> {out_path}")
    return {"quotes": len(findings["quotes"]), "dropped": dropped, "chunks": len(chunks)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--time", default="year", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--limit", type=int, default=25, help="posts per keyword per subreddit")
    ap.add_argument("--comments", type=int, default=5, help="top comments per post")
    ap.add_argument("--out", default=f"reddit_pains_{datetime.now():%Y%m%d_%H%M}.json")
    ap.add_argument("--analyze", metavar="JSON", help="skip scraping; analyze this scraped file with Claude")
    args = ap.parse_args()

    if args.analyze:
        try:
            analyze(args.analyze)
        except anthropic.AuthenticationError:
            sys.exit("Invalid ANTHROPIC_API_KEY in .env")
        except anthropic.RateLimitError:
            sys.exit("Rate limited by Anthropic; wait a minute and rerun.")
        except anthropic.APIStatusError as e:
            sys.exit(f"Anthropic API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            sys.exit("Network error reaching Anthropic.")
        return

    reddit = make_reddit()
    posts = scrape(reddit, args.time, args.limit, args.comments)
    save_posts(posts, args.out)


def save_posts(posts, path, subreddits=SUBREDDITS, keywords=KEYWORDS):
    posts.sort(key=lambda p: p["score"], reverse=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "subreddits": subreddits,
            "keywords": keywords,
            "count": len(posts),
            "posts": posts,
        }, f, ensure_ascii=False, indent=2)
    print(f"\nSaved {len(posts)} unique posts -> {path}")


if __name__ == "__main__":
    main()
