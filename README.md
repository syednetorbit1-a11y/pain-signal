# Pain Signal

A personal, non-commercial research tool that finds out what cloud and infrastructure engineers struggle with, in their own words.

It searches public Reddit posts in a few technical communities (r/devops, r/sysadmin, r/aws, r/azure, r/cloud) for phrases like "cost spike", "multi-cloud" and "orphaned", then summarizes the recurring problems into a private Markdown report.

## How it uses the Reddit API

- **Read-only.** Uses [PRAW](https://praw.readthedocs.io/) with `read_only=True`. It never posts, comments, votes, messages or performs account actions.
- **Small volume.** 25 searches per run (5 subreddits × 5 phrases), up to 25 posts each plus their top comments: roughly 600 requests per run, run weekly at most, within Reddit's rate limits. PRAW handles rate limiting automatically.
- **Descriptive user agent**, e.g. `pain-signal/1.0 by u/Ok-Mistake-7160`.
- **Local storage only.** Results are saved on the user's own computer, never shared, sold or published, and can be deleted from the UI at any time.
- **No model training.** Post text is summarized by an AI model for a private report; it is not used to train any AI or machine-learning model.

## Files

| File | Purpose |
|---|---|
| `scraper.py` | Reddit search (PRAW) and the summarization step |
| `main.py` | Runs the whole pipeline from the command line |
| `app.py`, `templates/`, `static/` | Local web UI at http://127.0.0.1:8820 |

## Running it

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.template .env      # add your own API credentials
python app.py                # or: python main.py
```

Credentials live in `.env`, which is excluded from this repository.
