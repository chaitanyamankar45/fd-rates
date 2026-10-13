#!/usr/bin/env python3
"""
Smart FD Tracker - bank rate feed builder
-----------------------------------------
For each bank: 1) read the rate table from the bank's page with the free scraper (scrape_banks.py);
2) if that fails or the numbers do not pass the checks, ask Claude (web search) as a fallback.
Writes rates.json (the app's feed) and review.md (what changed, what needs a human look).

  python rate_agent.py --prev rates.json                # all four banks
  python rate_agent.py --banks "HDFC Bank" --no-ai      # scraper only, no API key needed
  pip install anthropic   (only needed for the AI fallback; set ANTHROPIC_API_KEY)

Feed format: {"asOf","sample","banks":{"Bank":{"slabs":[[fromDays,toDays,general,senior|null],...],"source","effective"}}}
senior = null means the bank has no separate senior rate for that slab ("No slab").
Nothing is published automatically: a person reads review.md, then merges the pull request.
"""
import argparse, datetime as dt, json, re, sys
import scrape_banks

MODEL = "claude-sonnet-5-5"
DEFAULT_BANKS = ["HDFC Bank", "ICICI Bank", "State Bank of India", "Union Bank of India"]
MAX_JUMP = 0.75   # percentage points vs the previous feed that needs extra attention

PROMPT = """Find the bank's CURRENT domestic fixed deposit interest rates (deposits below Rs 3 crore, regular / \
non-callable-or-callable standard table) from the bank's OFFICIAL website only. Use the web_search tool.
Start with this page if reachable: {url}

Return EVERY slab of the table from 7 days up to 10 years, including special-tenure rows (e.g. 400 days, 444 days, 555 days), \
with no gaps and no overlaps. Convert each slab to days: 1 year = 365 days, 1 month = 30.4375 days rounded (6 months = 183, \
18 months = 548, 3 years = 1095, 5 years = 1825, 10 years = 3650). "2 years 1 day to 3 years" = from 731 to 1095.
For each slab give the general public rate and the senior citizen (60+) rate in % p.a. If the bank shows "No slab" / no separate \
senior rate for a slab, use null for the senior rate. Never guess: if a number cannot be verified on the official page, omit the whole bank.
Reply with ONLY one JSON object, no prose, no code fences:
{{"bank":"{bank}","source":"<official page url>","effective":"YYYY-MM-DD or null","slabs":[[from_days,to_days,general,senior_or_null],...]}}"""


def ask(client, bank):
    msg = client.messages.create(
        model=MODEL, max_tokens=3000,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
        messages=[{"role": "user", "content": PROMPT.format(bank=bank, url=scrape_banks.BANKS.get(bank, "the bank's official site"))}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON in reply")
    return json.loads(m.group(0))


def check(d):
    """Return (clean_entry or None, list_of_problems)."""
    probs = []
    slabs = d.get("slabs")
    if not isinstance(slabs, list) or len(slabs) < 8:
        return None, ["fewer than 8 slabs"]
    slabs = sorted([list(s) for s in slabs], key=lambda s: s[0])
    for s in slabs:
        if len(s) != 4 or not all(isinstance(x, (int, float)) for x in s[:3]) or s[0] > s[1]:
            return None, [f"bad slab {s}"]
        if not (1 < s[2] < 15) or (s[3] is not None and not (1 < s[3] < 15)):
            probs.append(f"rate outside 1-15% in slab {s[:2]}")
        if s[3] is not None and s[3] < s[2]:
            probs.append(f"senior rate lower than general in slab {s[:2]}")
    for a, b in zip(slabs, slabs[1:]):
        if b[0] != a[1] + 1:
            probs.append(f"gap or overlap between day {a[1]} and day {b[0]}")
    if slabs[0][0] > 30 or slabs[-1][1] < 1825:
        probs.append("slabs do not cover 30 days to 5 years")
    # the range the app cares about (6 months .. 5 years) must have no gaps
    if not str(d.get("source", "")).startswith("http"):
        probs.append("missing source url")
    if probs:
        return None, probs
    return {"slabs": slabs, "source": d["source"], "effective": d.get("effective")}, []


def slab_at(entry, days):
    for s in entry.get("slabs", []):
        if s[0] <= days <= s[1]:
            return s
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--banks", help="comma separated; default = all four")
    ap.add_argument("--prev", help="previous rates.json to diff against and to keep last-known rates from")
    ap.add_argument("--out", default="rates.json")
    ap.add_argument("--no-ai", action="store_true", help="scraper only, never call the AI agent")
    a = ap.parse_args()
    banks = [b.strip() for b in a.banks.split(",")] if a.banks else DEFAULT_BANKS
    prev = json.load(open(a.prev))["banks"] if a.prev else {}
    client = None

    feed = {"asOf": dt.date.today().isoformat(), "sample": False, "banks": {}}
    review = [f"# Rate review {feed['asOf']}\n", "Check each bank against its official page before merging.\n"]
    for bank in banks:
        print("fetching", bank, file=sys.stderr)
        entry, probs, how = None, [], ""
        if bank in scrape_banks.BANKS:
            try:
                d = scrape_banks.scrape(bank)
                entry, probs = check(d)
                how = "free scraper (" + d["notes"] + ")"
            except Exception as e:
                probs = [f"scraper: {e}"]
        if entry is None and not a.no_ai:
            try:
                if client is None:
                    import anthropic
                    client = anthropic.Anthropic()
                entry, p2 = check(ask(client, bank))
                probs += p2
                how = "AI agent fallback"
            except Exception as e:
                probs.append(f"agent error: {e}")
        if entry is None:
            review.append(f"- **{bank}: SKIPPED** - " + "; ".join(probs))
            if bank in prev:  # keep the last known good rates and flag them as stale
                feed["banks"][bank] = {**prev[bank], "stale": True}
            continue
        feed["banks"][bank] = entry
        review.append(f"- **{bank}** - {how}; effective {entry.get('effective') or 'not found'}; {len(entry['slabs'])} slabs ({entry['source']})")
        if probs:
            review.append("  - earlier attempt failed: " + "; ".join(probs))
        for days, name in [(183, "6 months"), (365, "1 year"), (730, "2 years"), (1095, "3 years"), (1825, "5 years")]:
            new, old = slab_at(entry, days), slab_at(prev.get(bank, {}), days)
            if new and old and (new[2] != old[2] or new[3] != old[3]):
                big = abs(new[2] - old[2]) >= MAX_JUMP
                review.append(f"  - {name}: general {old[2]}% -> {new[2]}%, senior {old[3]}% -> {new[3]}%" + ("  WARNING large move, verify" if big else ""))

    json.dump(feed, open(a.out, "w"), indent=1)
    open("review.md", "w").write("\n".join(review) + "\n")
    print(f"wrote {a.out} and review.md - review before publishing", file=sys.stderr)


if __name__ == "__main__":
    main()
