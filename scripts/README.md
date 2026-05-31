Quick job-scraper using ScrapeGraphAI

Prereqs

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt
playwright install
```

Set your LLM API key (example for OpenAI):

```bash
export OPENAI_APIKEY="sk-..."
```

Run the scraper with a file containing careers URLs (one per line):

```bash
python scripts/job_scraper.py --sources-file scripts/companies_example.txt --out results.json --out-csv results.csv
```

Notes
- If you prefer a different model, pass `--model`.
- Some sites block scraping; consider using proxies or authenticated Playwright sessions.
