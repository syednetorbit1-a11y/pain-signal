"""Full pipeline: scrape Reddit -> analyze with Claude -> market_research_report.md

Usage:
    python main.py                         # past year, 25 posts per keyword per sub, 5 comments each
    python main.py --time month --limit 50 --comments 10
    python main.py --skip-scrape data.json # re-run only the analysis on an existing scrape
"""
import argparse
import os
import sys
from datetime import datetime

import anthropic
from dotenv import load_dotenv

from scraper import REPORT_FILE, analyze, make_reddit, save_posts, scrape


def check_env():
    load_dotenv()
    missing = [k for k in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "ANTHROPIC_API_KEY") if not os.getenv(k)]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}  (copy .env.template to .env and fill it in)")


def main():
    ap = argparse.ArgumentParser(description="Reddit cloud pain-point market research")
    ap.add_argument("--time", default="year", choices=["hour", "day", "week", "month", "year", "all"])
    ap.add_argument("--limit", type=int, default=25, help="posts per keyword per subreddit")
    ap.add_argument("--comments", type=int, default=5, help="top comments per post")
    ap.add_argument("--skip-scrape", metavar="JSON", help="analyze an existing scrape instead of scraping again")
    args = ap.parse_args()

    check_env()

    if args.skip_scrape:
        data_file = args.skip_scrape
    else:
        print("== Step 1/2: scraping Reddit ==")
        data_file = f"reddit_pains_{datetime.now():%Y%m%d_%H%M}.json"
        posts = scrape(make_reddit(), args.time, args.limit, args.comments)
        if not posts:
            sys.exit("No posts found. Try --time all or a higher --limit.")
        save_posts(posts, data_file)

    print("\n== Step 2/2: analyzing with Claude ==")
    try:
        analyze(data_file, REPORT_FILE)
    except anthropic.AuthenticationError:
        sys.exit("Invalid ANTHROPIC_API_KEY in .env")
    except anthropic.RateLimitError:
        sys.exit(f"Rate limited by Anthropic. Scrape is saved; rerun: python main.py --skip-scrape {data_file}")
    except anthropic.APIStatusError as e:
        sys.exit(f"Anthropic API error {e.status_code}: {e.message}\nRerun: python main.py --skip-scrape {data_file}")
    except anthropic.APIConnectionError:
        sys.exit(f"Network error reaching Anthropic. Rerun: python main.py --skip-scrape {data_file}")

    print(f"\nDone. Open {os.path.abspath(REPORT_FILE)}")


if __name__ == "__main__":
    main()
