# Quarterly rate feed (all banks, all slabs)

Flow: **GitHub Action (every quarter) -> rate_agent.py -> pull request -> you review -> merge -> app downloads it.**

- Banks: HDFC Bank, ICICI Bank, State Bank of India, Union Bank of India.
- The feed holds every slab of each bank (7 days to 10 years) including special tenures such as Union Bank's 400, 444 and 555 days,
  with the general rate and the senior-citizen rate (`null` = "No slab": no separate senior rate).
- Free first: `scrape_banks.py` reads each bank's rate table straight from its page (no AI, no key, standard library only).
  If a page cannot be read, or the numbers fail the checks (gaps between slabs, rate outside 1-15%, senior below general),
  `rate_agent.py` asks Claude with web search for that bank only (about $0.10 per bank). With `--no-ai` it never calls Claude.
- A bank that cannot be fetched either way keeps its last known rates, marked `"stale": true`, and is listed in `review.md`.

## Test on your PC
```
cd rate-feed
python scrape_banks.py "Union Bank of India"     # prints slabs, no key needed
python scrape_banks.py --all                      # tries all four banks
python rate_agent.py --prev rates.json --no-ai   # builds rates.json + review.md using only the free scraper
```
If a bank prints `FAILED`, save its page (Chrome: Ctrl+S, "Webpage, HTML only") and send it to me; I will adapt the scraper.
Until then the AI fallback covers it.

## Production (automatic)
1. Put the project in a public GitHub repo.
2. Repo > Settings > Secrets and variables > Actions: add `ANTHROPIC_API_KEY` (only used if a scraper fails).
3. `.github/workflows/quarterly-rates.yml` runs on the 5th of Jan/Apr/Jul/Oct (or press "Run workflow") and opens a pull request
   with the new `rates.json` and `review.md` (old -> new rates, large moves flagged).
4. Check the pull request against the bank pages, merge it.
5. Set `FEED_URL` in `RateFeedStore.kt` once to `https://raw.githubusercontent.com/<you>/<repo>/main/rate-feed/rates.json`.
   The app downloads a newer feed in its daily check and sends the rate-change alerts.

To add a bank: add its name and rate-page URL to `BANKS` in `scrape_banks.py` and to `DEFAULT_BANKS` in `rate_agent.py`.
