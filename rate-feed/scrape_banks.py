#!/usr/bin/env python3
"""
Free (no AI, no API key) rate fetch from the banks' own rate pages. Standard library only.
Reads the HTML rate table of a bank and returns slabs in the app's feed format:
  {"slabs": [[fromDays, toDays, general, senior|None], ...], "source": url, "effective": "YYYY-MM-DD"|None, "notes": "..."}

  python scrape_banks.py "Union Bank of India"             # fetch live, print JSON
  python scrape_banks.py "Union Bank of India" --html p.html  # parse a saved copy of the page (testing)
  python scrape_banks.py --all                              # all banks

If a page changes shape (or loads its table with JavaScript) the scraper raises an error; rate_agent.py then falls back
to the AI agent for that bank, so a layout change never publishes wrong numbers.
"""
import argparse, datetime as dt, json, re, sys, urllib.error, urllib.request
from html.parser import HTMLParser

BANKS = {
    "Union Bank of India": "https://www.unionbankofindia.bank.in/en/details/rate-of-interest",
    "State Bank of India": "https://sbi.bank.in/web/interest-rates/deposit-rates/retail-domestic-term-deposits",
    "HDFC Bank": "https://v.hdfc.bank.in/amp/personal/save/deposits/fixed-deposit/fixed-deposit-interest-rate.html",
    "ICICI Bank": "https://www.icici.bank.in/personal-banking/deposits/fixed-deposit/fd-interest-rates",
}
DEFAULT_SENIOR_ADD = 0.50      # only used when a page has no senior column and does not state its own add-on
MIN_DAYS, MAX_DAYS = 7, 3650


class Tables(HTMLParser):
    """Collects tables as grids of text; colspan / rowspan cells are repeated so every row has the same columns."""
    def __init__(self):
        super().__init__(); self.tables = []; self._t = None; self._r = None; self._c = None; self._span = (1, 1); self._carry = {}
    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "table": self._t = []; self._carry = {}
        elif tag == "tr" and self._t is not None: self._r = []
        elif tag in ("td", "th") and self._r is not None:
            self._c = []
            num_ = lambda k: int(re.sub(r"\D", "", a.get(k) or "1") or 1)
            self._span = (max(1, num_("colspan")), max(1, num_("rowspan")))
    def handle_data(self, d):
        if self._c is not None: self._c.append(d)
    def _fill_carry(self):
        while len(self._r) in self._carry and self._carry[len(self._r)][1] > 0:
            txt, left = self._carry[len(self._r)]
            self._carry[len(self._r)] = (txt, left - 1); self._r.append(txt)
    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._c is not None:
            self._fill_carry()
            txt = " ".join("".join(self._c).split()); cs, rs = self._span
            for _ in range(cs):
                if rs > 1: self._carry[len(self._r)] = (txt, rs - 1)
                self._r.append(txt)
            self._c = None
        elif tag == "tr" and self._r is not None:
            self._fill_carry()
            if self._r: self._t.append(self._r)
            self._r = None
        elif tag == "table" and self._t is not None:
            self.tables.append(self._t); self._t = None


UNIT = {"d": 1, "day": 1, "days": 1, "m": 30.4375, "mo": 30.4375, "month": 30.4375, "months": 30.4375, "y": 365, "yr": 365, "yrs": 365, "year": 365, "years": 365}


def duration(text, default_unit=None):
    """'2 years 11 months 1 day' -> days; a bare number takes default_unit. None if no number."""
    pairs = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)?", text.lower())
    total, found = 0.0, False
    for n, u in pairs:
        u = u if u in UNIT else None
        u = u or default_unit
        if u is None: continue
        total += float(n) * UNIT[u]; found = True
    return round(total) if found else None


def slab_days(label):
    """Tenor text -> (from_days, to_days) or None. Handles '7-14 days', '1 yr to 399 days', '400 days',
    'More than 3 to 10 yrs', '2 years 1 day to < 2 years 11 months', '90 days to <= 6 months'."""
    t = label.lower().replace("–", "-").replace("—", "-").replace("≤", "<=").replace("&lt;", "<")
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"(?<!to)\s*<=", " to <=", t)          # "90 days <= 6 months" -> "90 days to <= 6 months"
    parts = re.split(r"\bto\b|-(?=\s*[<=>]*\s*\d)", t, maxsplit=1)
    unit_of = lambda s: next((u for _, u in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", s) if u in UNIT), None)
    if len(parts) == 1:
        d = duration(parts[0]); return None if d is None else (d, d)
    lo_t, hi_t = parts
    hi_unit = unit_of(hi_t); lo_unit = unit_of(lo_t) or hi_unit
    lo = duration(lo_t, lo_unit); hi = duration(hi_t, hi_unit)
    if lo is None or hi is None: return None
    if re.search(r"more than|above|over|>", lo_t) and not re.search(r"\d+\s*day", lo_t.split("more than")[-1] if "more than" in lo_t else ""):
        lo += 1
    if re.match(r"\s*(<(?!=)|less than)", hi_t.strip()): hi -= 1
    return lo, hi


def num(cell):
    m = re.fullmatch(r"\s*(\d{1,2}(?:\.\d{1,3})?)\s*%?\s*\*?\s*(?:highest)?\s*", (cell or "").lower())
    return float(m.group(1)) if m else None


def find_columns(tbl):
    """(general_col, senior_col|None) from the first header rows (rows with fewer cells than the body are right-aligned)."""
    ncols = max(len(r) for r in tbl)
    gen = sen = None
    for row in tbl[:3]:
        off = ncols - len(row) if len(row) < ncols else 0
        for i, h in enumerate(row):
            h = h.lower(); i += off
            if "existing" in h or "old" in h.split(): continue          # SBI shows old and revised columns
            if "senior" in h and "super" not in h:
                if sen is None: sen = i
            elif ("non" in h and "callable" in h) or "super" in h: continue
            elif gen is None and any(k in h for k in ("callable", "general", "regular", "public", "rate", "<")) and "senior" not in h: gen = i
    return gen, sen


def tenor_column(tbl):
    """Column that holds the tenor text (first column normally; the middle one on ICICI)."""
    ncols = max(len(r) for r in tbl)
    best = max(range(ncols), key=lambda c: sum(1 for r in tbl if len(r) > c and slab_days(r[c])))
    return best


MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def latest_date(text):
    """Latest 'effective / w.e.f. / applicable from' date on the page, as YYYY-MM-DD (or None)."""
    found = []
    for m in re.finditer(r"(?:effective from|effective date|w\.e\.f\.?|applicable from)[^0-9A-Za-z]{0,12}([^.]{0,40})", text, re.I):
        t = m.group(1)
        for d, mo, y in re.findall(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", t):
            try: found.append(dt.date(int(y), int(mo), int(d)))
            except ValueError: pass
        for d, mo, y in re.findall(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", t):
            if mo.lower() in MONTHS: found.append(dt.date(int(y), MONTHS.index(mo.lower()) + 1, int(d)))
        for mo, d, y in re.findall(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})\s*(?:st|nd|rd|th)?,?\s+(\d{4})", t):
            if mo.lower() in MONTHS: found.append(dt.date(int(y), MONTHS.index(mo.lower()) + 1, int(d)))
    found = [d for d in found if d <= dt.date.today()]
    return max(found).isoformat() if found else None


def parse(html, url=""):
    p = Tables(); p.feed(html)
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    m = re.search(r"senior citizen deposits?\s+is\s+(\d\.\d+)\s*%", text, re.I) or re.search(r"(\d\.\d+)\s*%\s*over and above", text, re.I)
    add = float(m.group(1)) if m else DEFAULT_SENIOR_ADD
    for tbl in p.tables:
        tcol = tenor_column(tbl)
        rows = [(slab_days(r[tcol]), r) for r in tbl if len(r) > tcol and slab_days(r[tcol])]
        if len(rows) < 8: continue
        gcol, scol = find_columns(tbl)
        if gcol is None:     # first numeric column
            gcol = next((i for i in range(len(tbl[-1])) if i != tcol and all(num(r[i]) is not None for _, r in rows if len(r) > i and r[i])), 1)
        slabs = []
        for (lo, hi), r in rows:
            g = num(r[gcol]) if len(r) > gcol else None
            if g is None: continue
            s = (num(r[scol]) if len(r) > scol else None) if scol is not None else round(g + add, 2)
            slabs.append([max(lo, MIN_DAYS), min(hi, MAX_DAYS), g, s])
        slabs.sort()
        merged = []
        for s in slabs:                        # join neighbouring rows that have identical rates
            if merged and merged[-1][2:] == s[2:] and s[0] <= merged[-1][1] + 1: merged[-1][1] = max(merged[-1][1], s[1])
            else: merged.append(s)
        if len(merged) < 8: continue
        how = f"senior column {scol}" if scol is not None else (f"senior = general + {add}" + ("" if m else " (default, not found on page)"))
        return {"slabs": merged, "source": url, "effective": latest_date(text), "notes": f"tenor column {tcol}, general column {gcol}; {how}"}
    raise ValueError("deposit-rate table not found on page")


INSECURE = False


def fetch(url):
    """Download a page. On some Windows PCs Python lacks a bank's intermediate certificate; run with --insecure there
    (read-only public rate data; the review step is the safety net). GitHub's servers do not need it."""
    import ssl
    ctx = ssl._create_unverified_context() if INSECURE else None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; FDTrackerRates/1.0)"})
    try:
        return urllib.request.urlopen(req, timeout=30, context=ctx).read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            raise RuntimeError("certificate could not be verified on this PC - run again with --insecure to test") from e
        raise


def scrape(bank, html=None):
    url = BANKS[bank]
    return parse(html if html is not None else fetch(url), url)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("bank", nargs="?"); ap.add_argument("--html"); ap.add_argument("--all", action="store_true"); ap.add_argument("--insecure", action="store_true", help="skip HTTPS certificate check (testing only)")
    a = ap.parse_args()
    INSECURE = a.insecure
    names = list(BANKS) if a.all else [a.bank or "Union Bank of India"]
    for n in names:
        try:
            r = scrape(n, open(a.html, encoding="utf-8").read() if a.html else None)
            print(n, json.dumps(r, indent=0))
        except Exception as e:
            print(f"{n}: FAILED - {e}", file=sys.stderr)
