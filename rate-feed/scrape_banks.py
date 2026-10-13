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
    # verified on the live pages
    "Union Bank of India": "https://www.unionbankofindia.bank.in/en/details/rate-of-interest",
    "State Bank of India": "https://sbi.bank.in/web/interest-rates/deposit-rates/retail-domestic-term-deposits",
    "HDFC Bank": "https://v.hdfc.bank.in/amp/personal/save/deposits/fixed-deposit/fixed-deposit-interest-rate.html",
    "ICICI Bank": "https://www.icici.bank.in/personal-banking/deposits/fixed-deposit/fd-interest-rates",
    # not tested yet: run `python scrape_banks.py --all --insecure` and send me the output
    "Canara Bank": "https://www.canarabank.bank.in/term-deposits-rate-of-interest-p.a.",
    "Indian Bank": "https://indianbank.bank.in/en/deposit-rates",
    "Axis Bank": "https://www.axis.bank.in/deposits/fixed-deposits/fd-interest-rates",
    "Kotak Mahindra Bank": "https://www.kotak.bank.in/en/rates/interest-rates.html",
    "Punjab National Bank": "https://www.pnb.bank.in/Interest-Rates-Deposit.html",
    "IDFC FIRST Bank": "https://www.idfcfirst.bank.in/interest-rate",
    # added from the URLs you sent; IDBI and Central Bank are plain HTML tables, the other three need `--debug` checks
    "IDBI Bank": "https://www.idbi.bank.in/interest-rates.aspx",
    "Central Bank of India": "https://www.centralbank.bank.in/en/domestic-term-deposit-interest-rate-per-annum",
    "Bank of India": "https://bankofindia.bank.in/interest-rate/rupee-term-deposit-rate",
    "Yes Bank": "https://www.yes.bank.in/personal-banking/yes-individual/deposits/fixed-deposit",
    "IndusInd Bank": "https://www.indusind.bank.in/in/en/personal/fixed-deposit-interest-rate.html",
    "Bank of Baroda": "https://bankofbaroda.bank.in/interest-rate-and-service-charges/deposits-interest-rates/fixed-deposits-callable-and-non-callable-upto-ten-crores",
}
# Axis publishes its rates as a PDF; the page links to it (the file name carries the date, so the link is looked up each time).
PDF_BANKS = {"IDFC FIRST Bank": r"""["']([^"']*Interest-Rates-on-Retail-Deposits[^"']*\.pdf[^"']*)["']""",
             "Axis Bank": r"""["']([^"']*domestic-fixed-deposit-interest-rates-less-than-3-crores[^"']*\.pdf[^"']*)["']"""}
# Kotak builds its table with JavaScript; a headless browser (pip install playwright; playwright install chromium) renders the page.
RENDER_BANKS = {"Kotak Mahindra Bank": "Fixed Deposit", "Yes Bank": None, "IndusInd Bank": None, "Bank of India": None}
RENDER_FIRST = {"Yes Bank", "IndusInd Bank", "Bank of India"}      # their tables only appear after scrolling, so a plain download is incomplete
# Bank of India shows general rates on the first tab and senior rates on the "SENIOR CITIZEN" tab (same rows)
SENIOR_TAB = {"Bank of India": "SENIOR CITIZEN"}
DEFAULT_SENIOR_ADD = 0.50      # only used when a page has no senior column and does not state its own add-on
MIN_DAYS, MAX_DAYS = 7, 3650


class Tables(HTMLParser):
    """Collects tables as grids of text; colspan / rowspan cells are repeated so every row has the same columns.
    Tables nested inside another table's cell are collected too, and the outer table keeps its own rows."""
    def __init__(self):
        super().__init__(); self.tables = []; self._stack = []
        self._t = None; self._r = None; self._c = None; self._span = (1, 1); self._carry = {}
    def handle_starttag(self, tag, a):
        a = dict(a)
        if tag == "table":
            self._stack.append((self._t, self._r, self._c, self._span, self._carry))
            self._t = []; self._r = None; self._c = None; self._carry = {}
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
        if tag in ("td", "th") and self._c is not None and self._r is not None:
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
            self.tables.append(self._t)
            self._t, self._r, self._c, self._span, self._carry = self._stack.pop() if self._stack else (None, None, None, (1, 1), {})


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
    'More than 3 to 10 yrs', '2 years 1 day to < 2 years 11 months', '90 days to <= 6 months',
    '2 Years & above to less than 3 Years', 'Above 5 years', 'IND Grow (555 Days)'."""
    t = label.lower().replace("–", "-").replace("—", "-").replace("≤", "<=").replace("&lt;", "<")
    t = re.sub(r"[*#$]+", " ", t)
    t = re.sub(r"\band\s+(?:up\s*to|less than)\b", lambda m: "to" + (" less than" if "less" in m.group(0) else ""), t)   # "400 days and upto 2 years", "271 days & above and less than 1 year"
    t = re.sub(r"\bupto\b|\bup\s+to\b", "to", t)
    t = re.sub(r"\band above\b", "& above", t)
    t = re.sub(r"\bbut\b", "to", t)                      # "3 years & above but less than 4 years"
    if "<" in t and not re.search(r"\bto\b", t) and not re.match(r"\s*<", t):
        t = re.sub(r"<(?!=)", " to <", t, count=1)          # "4 months < 6 months"
    # (35 months) after a full duration is a repeat -> drop; (555 days) after a name is the tenor -> keep
    t = re.sub(r"\(([^)]*)\)", lambda m: " " if re.search(r"\d\s*(?:days?|months?|years?|yrs?|yr|mo)\b", t[:m.start()]) else " " + m.group(1) + " ", t)
    t = re.sub(r"(?<!to)\s*<=", " to <=", t)          # "90 days <= 6 months" -> "90 days to <= 6 months"
    parts = re.split(r"\bto\b|-(?=\s*(?:less than|under)?\s*[<=>]*\s*\d)", t, maxsplit=1)
    unit_of = lambda x: next((u for _, u in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", x) if u in UNIT), None)
    if len(parts) == 1:
        d = duration(parts[0])
        if d is None: return None
        if re.search(r"\b(?:above|more than|over)\b", parts[0]) and "&" not in parts[0]: return d + 1, MAX_DAYS
        return d, d
    lo_t, hi_t = parts
    hi_unit = unit_of(hi_t); lo_unit = unit_of(lo_t) or hi_unit
    lo = duration(lo_t, lo_unit); hi = duration(hi_t, hi_unit)
    if lo is None or hi is None: return None
    if re.search(r"\b(?:above|more than|over)\b|>", lo_t) and "&" not in lo_t:
        lo += 1                                         # "Above 1 year" / "More than 3" start the day after; "2 Years & above" starts on the day
    if re.match(r"\s*(<(?!=)|less than|under)", hi_t.strip()): hi -= 1
    return lo, hi


def num(cell):
    m = re.fullmatch(r"\s*(\d{1,2}(?:\.\d{1,3})?)\s*%?\s*[*#$]*\s*(?:highest)?\s*", (cell or "").lower())
    return float(m.group(1)) if m else None


def find_columns(tbl):
    """(general_col, senior_col|None) from the first header rows (colspan/rowspan cells are already expanded)."""
    ncols = max(len(r) for r in tbl)
    sen = None
    cands = {"strict": None, "loose": None}
    for row in tbl[:6]:
        if len(set(row)) <= 2 and len(row) >= 3: continue            # a full-width title row
        for i, h in enumerate(row):
            h = h.lower().replace("sr.", "senior")
            if "existing" in h or "old" in h.split() or "yield" in h or "annualised" in h: continue   # old columns / yield columns
            if "senior" in h and "super" not in h:
                if sen is None: sen = i
            elif ("non" in h and "callable" in h) or "super" in h or "senior" in h: continue
            elif cands["strict"] is None and any(k in h for k in ("general", "public", "regular")): cands["strict"] = i
            elif cands["loose"] is None and any(k in h for k in ("callable", "rate", "<")): cands["loose"] = i
    gen = cands["strict"] if cands["strict"] is not None else cands["loose"]
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
    for m in re.finditer(r"(?:effective from|effective date|effective|w\.e\.f\.?|applicable from)[^0-9A-Za-z]{0,12}(.{0,40})", text, re.I):
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


def resolve_overlaps(slabs):
    """Special tenors (444 days, 555 days...) sit inside wider slabs: on every day the narrowest slab wins."""
    if not slabs: return []
    lo, hi = min(x[0] for x in slabs), max(x[1] for x in slabs)
    out = []
    for d in range(lo, hi + 1):
        c = [x for x in slabs if x[0] <= d <= x[1]]
        if not c: continue
        x = min(c, key=lambda y: (y[1] - y[0], y[0]))
        if out and out[-1][2:] == x[2:] and out[-1][1] == d - 1: out[-1][1] = d
        else: out.append([d, d, x[2], x[3]])
    return out


def senior_addon(text):
    """The extra % a senior citizen gets, from a sentence like 'additional 0.50% ... senior citizens'; (value, found)."""
    for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z])", text):
        low = sent.lower()
        if "senior" in low and ("additional" in low or "over and above" in low or "extra" in low):
            m = re.search(r"(\d\.\d+)\s*%", sent)
            if m: return float(m.group(1)), True
    return DEFAULT_SENIOR_ADD, False


def parse_tables(html, url=""):
    """Every table on the page that looks like a rate table, in page order (list of result dicts)."""
    p = Tables(); p.feed(html); found = []
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    add, add_found = senior_addon(text)
    for tbl in p.tables:
        head = " ".join(" ".join(r) for r in tbl[:6]).lower()
        if re.search(r"\d+\s*crore\s*-\s*less than|above\s*\d{2,}\s*cr|not allowed", head): continue     # bulk-deposit tables
        tcol = tenor_column(tbl)
        rows = [(slab_days(r[tcol]), r) for r in tbl if len(r) > tcol and slab_days(r[tcol])]
        if len(rows) < 8: continue
        gcol, scol = find_columns(tbl)
        if gcol == tcol: gcol = None
        if gcol is None:     # first numeric column
            gcol = next((i for i in range(len(tbl[-1])) if i != tcol and all(num(r[i]) is not None for _, r in rows if len(r) > i and r[i])), 1)
        slabs = []
        for (lo, hi), r in rows:
            g = num(r[gcol]) if len(r) > gcol else None
            if g is None: continue
            s = (num(r[scol]) if len(r) > scol else None) if scol is not None else round(g + add, 2)
            if scol is not None and s is None: continue      # e.g. a special product with "Not listed" for seniors
            slabs.append([max(lo, MIN_DAYS), min(hi, MAX_DAYS), g, s])
        merged = resolve_overlaps(slabs)
        if len(merged) < 8: continue
        how = f"senior column {scol}" if scol is not None else (f"senior = general + {add}" + ("" if add_found else " (default, not found on page)"))
        # effective date: from the table's own heading rows, else just before the table, else anywhere on the page
        own = " ".join(" ".join(r) for r in tbl[:8])
        pos = text.find(tbl[0][0]) if tbl[0] and tbl[0][0] else -1
        eff = latest_date(own) or (latest_date(text[max(0, pos - 500):pos]) if pos > 0 else None) or latest_date(text)
        found.append({"slabs": merged, "source": url, "effective": eff, "notes": f"tenor column {tcol}, general column {gcol}; {how}"})
    return found


def parse(html, url=""):
    found = parse_tables(html, url)
    if not found: raise ValueError("deposit-rate table not found on page")
    return found[0]


def parse_text(text, url=""):
    """Rate list as plain text (PDF): '<tenor> <general>% <senior>%' repeated, tenor like '88 days to 3 months 24 days'."""
    t = " ".join(text.replace("–", "-").replace("—", "-").split())
    u = r"(?:days?|months?|years?)"
    dur = rf"\d+(?:\s*{u})?(?:\s+\d+\s*{u})*"
    pat = re.compile(rf"((?<![\d.]){dur}(?:\s*(?:to|-|<)\s*(?:under|less than|<)?\s*{dur})?)\s*(\d{{1,2}}\.\d{{1,2}})\s*%?\s+(\d{{1,2}}\.\d{{1,2}})\s*%?", re.I)
    slabs = []
    for m in pat.finditer(t):
        d = slab_days(m.group(1).strip())
        if d: slabs.append([max(d[0], MIN_DAYS), min(d[1], MAX_DAYS), float(m.group(2)), float(m.group(3))])
    merged = resolve_overlaps(slabs)
    if len(merged) < 8: raise ValueError("deposit-rate list not found in the text")
    return {"slabs": merged, "source": url, "effective": latest_date(t), "notes": "read from a PDF (general, senior)"}


def fetch_bytes(url):
    import ssl
    ctx = ssl._create_unverified_context() if INSECURE else None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"})
    return urllib.request.urlopen(req, timeout=60, context=ctx).read()


def pdf_text(data):
    try:
        import io, pypdf
        return "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(data)).pages)
    except ImportError:
        pass
    import shutil, subprocess, tempfile
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f: f.write(data)
        return subprocess.run(["pdftotext", "-layout", f.name, "-"], capture_output=True, text=True).stdout
    raise RuntimeError("reading a PDF needs:  pip install pypdf")


LAST_TEXT = ""


def render(url, click=None):
    """Page HTML after JavaScript has run and the page was scrolled to the bottom (lazy-loaded tables).
    Needs: pip install playwright ; playwright install chromium"""
    global LAST_TEXT
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("this page needs a browser engine:  pip install playwright  then  playwright install chromium")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    with sync_playwright() as pw:
        b = pw.chromium.launch(args=["--disable-http2"])          # some bank sites break on HTTP/2
        pg = b.new_context(ignore_https_errors=INSECURE, user_agent=ua, viewport={"width": 1400, "height": 900}).new_page()
        pg.goto(url, wait_until="domcontentloaded", timeout=90000)   # "networkidle" never happens on pages with trackers
        try: pg.wait_for_load_state("networkidle", timeout=20000)
        except Exception: pass
        for _ in range(14):                                          # scroll down so lazy-loaded tables appear
            pg.evaluate("window.scrollBy(0, 900)"); pg.wait_for_timeout(500)
        pg.wait_for_timeout(2000)
        if click:
            try: pg.get_by_text(click, exact=True).first.click(timeout=5000); pg.wait_for_timeout(2000)
            except Exception: pass
        html = pg.content()
        try: LAST_TEXT = pg.inner_text("body")
        except Exception: LAST_TEXT = ""
        b.close()
    return html


INSECURE = False
DEBUG_TEXT = False


def fetch(url):
    """Download a page. On some Windows PCs Python lacks a bank's intermediate certificate; run with --insecure there
    (read-only public rate data; the review step is the safety net). GitHub's servers do not need it."""
    import ssl
    ctx = ssl._create_unverified_context() if INSECURE else None
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-IN,en;q=0.9"})
    try:
        return urllib.request.urlopen(req, timeout=30, context=ctx).read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e):
            raise RuntimeError("certificate could not be verified on this PC - run again with --insecure to test") from e
        raise


def pdf_for(bank, page_html, page_url):
    from urllib.parse import urljoin
    m = re.search(PDF_BANKS[bank], page_html, re.I)
    if not m: raise ValueError("rate PDF link not found on the page")
    return urljoin(page_url, m.group(1).replace("&amp;", "&").replace("\\/", "/"))


# Banks whose server sends an incomplete certificate chain (fails even on GitHub's servers). Only public rate pages are read,
# nothing is sent, and the review pull request is the safety net.
INSECURE_OK = {"IDBI Bank"}


def scrape(bank, html=None):
    global INSECURE
    old = INSECURE
    if bank in INSECURE_OK: INSECURE = True
    try: return _scrape(bank, html)
    finally: INSECURE = old


def _scrape(bank, html=None):
    url = BANKS[bank]
    if bank in PDF_BANKS and html is None:
        page = fetch(url)
        try: link = pdf_for(bank, page, url)
        except ValueError:
            link = pdf_for(bank, render(url), url)           # the link may be added by JavaScript
        text = pdf_text(fetch_bytes(link))
        if DEBUG_TEXT: open("debug_" + re.sub(r"\W+", "_", bank) + "_pdf.txt", "w", encoding="utf-8").write(text)
        r = parse_text(text, link); r["source"] = link
        return r
    if html is not None: return parse(html, url)
    if bank in SENIOR_TAB: return scrape_two_tabs(bank, url)
    if bank in RENDER_FIRST: return parse(render(url, RENDER_BANKS[bank]), url)
    page = fetch(url)
    try: return parse(page, url)
    except ValueError:
        if bank not in RENDER_BANKS: raise
    page = render(url)
    try: return parse(page, url)
    except ValueError:
        return parse(render(url, RENDER_BANKS[bank]), url)


def scrape_two_tabs(bank, url):
    """General rates from the first table, senior rates from the first table on the senior tab (same tenor rows)."""
    html = render(url)
    tabs = parse_tables(html, url)
    if len(tabs) < 2: tabs = parse_tables(render(url, SENIOR_TAB[bank]), url)    # senior table only appears after clicking its tab
    if len(tabs) < 2: raise ValueError("senior-citizen table not found")
    gen, sen = tabs[0], tabs[1]
    at = lambda tab, d: next((x[2] for x in tab["slabs"] if x[0] <= d <= x[1]), None)
    lo, hi = max(gen["slabs"][0][0], sen["slabs"][0][0]), min(gen["slabs"][-1][1], sen["slabs"][-1][1])
    cuts = sorted({x[0] for t in (gen, sen) for x in t["slabs"] if lo <= x[0] <= hi})
    slabs = []
    for i, c in enumerate(cuts):
        end = (cuts[i + 1] - 1) if i + 1 < len(cuts) else hi
        g, sr = at(gen, c), at(sen, c)
        if g is None or sr is None: raise ValueError("general and senior tables do not cover the same tenors")
        if slabs and slabs[-1][2:] == [g, sr]: slabs[-1][1] = end
        else: slabs.append([c, end, g, sr])
    return {"slabs": slabs, "source": url, "effective": gen["effective"], "notes": "general from the first tab, senior from the senior tab"}


def dump_tables(html, path):
    """Debug aid: write every table with several numeric rows, one row per line, so a wrong parse can be diagnosed."""
    p = Tables(); p.feed(html); out = []
    for i, t in enumerate(p.tables):
        if sum(1 for r in t if any(re.search(r"\d\.\d", c) for c in r)) < 5: continue
        out.append(f"=== table {i}: {len(t)} rows")
        out += [" | ".join(r) for r in t]
    open(path, "w", encoding="utf-8").write("\n".join(out) or "no tables with rates found in the page (it may be loaded by JavaScript)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("bank", nargs="?"); ap.add_argument("--html"); ap.add_argument("--all", action="store_true"); ap.add_argument("--insecure", action="store_true", help="skip HTTPS certificate check (testing only)")
    ap.add_argument("--debug", action="store_true", help="write debug_<bank>.txt (all tables seen) next to this script, to diagnose a wrong or failed parse")
    a = ap.parse_args()
    INSECURE = a.insecure; DEBUG_TEXT = a.debug
    names = list(BANKS) if a.all else [a.bank or "Union Bank of India"]
    for n in names:
        print(f"--- {n} ...", file=sys.stderr, flush=True)
        try:
            html = open(a.html, encoding="utf-8").read() if a.html else None
            if a.debug and n not in PDF_BANKS:
                tag = re.sub(r"\W+", "_", n)
                if html: dump_tables(html, f"debug_{tag}.txt")
                else:
                    try: dump_tables(fetch(BANKS[n]), f"debug_{tag}.txt")
                    except Exception as e: print(f"  (plain download for debug failed: {e})", file=sys.stderr)
                    if n in RENDER_BANKS:
                        try:
                            dump_tables(render(BANKS[n], RENDER_BANKS[n]), f"debug_{tag}_browser.txt")
                            open(f"debug_{tag}_text.txt", "w", encoding="utf-8").write(LAST_TEXT)
                        except Exception as e: print(f"  (browser view for debug failed: {e})", file=sys.stderr)
            r = scrape(n, html)
            print(n, json.dumps(r, indent=0))
        except Exception as e:
            print(f"{n}: FAILED - {e}", file=sys.stderr)
