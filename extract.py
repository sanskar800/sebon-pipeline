"""SEBON prospectus shareholder-table extractor.

Finds a company on sebon.gov.np/prospectus and pulls the founder/director
background table (संस्थापक/सञ्चालकको पृष्ठभूमी) into a JSON schema, incl. each
person's other companies. lipi/npttf2utf decodes the legacy Preeti font so we
keyword-find the table page locally, then send only those pages to Gemini.

  python extract.py "Mount Everest"
  python extract.py https://sebon.gov.np/uploads/.../x.pdf

Output: output/<slug>.json. Run in the worker/queue container (GEMINI_API_KEY + net).
"""
import base64
import html
import io
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber
from pypdf import PdfReader, PdfWriter
from lipi.converter.npttf2utf_wrapper import wrapper_nppttf2utf

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
CACHE = OUT / "cache"
LISTING = "https://sebon.gov.np/prospectus"
PRICE_IN, PRICE_OUT = 0.30 / 1e6, 2.50 / 1e6


def gemini_key():
    for p in (ROOT / ".env", ROOT.parent / "main_conglomerates" / ".env"):
        if p.exists():
            env = dict(re.findall(r"^(\w+)\s*=\s*(.+)$", p.read_text(), re.M))
            if env.get("GEMINI_API_KEY"):
                return env["GEMINI_API_KEY"], env.get("MODEL_NAME", "gemini-2.5-flash")
    raise SystemExit("GEMINI_API_KEY not found (.env or ../main_conglomerates/.env)")


def _get(url, timeout=120):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def list_prospectuses(pages=2):
    """[{title, url, date}] from the listing (first `pages` listing pages)."""
    out, seen = [], set()
    for pg in range(1, pages + 1):
        h = _get(f"{LISTING}?page={pg}", 60).decode("utf-8", "ignore")
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", h, re.S):
            m = re.search(r'href="(https://sebon[^"]*\.pdf)"', row)
            if not m or m.group(1) in seen:
                continue
            txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", row))).strip()
            date = (re.search(r"\d{4}-\d{2}-\d{2}", txt) or [None])[0] if re.search(r"\d{4}-\d{2}-\d{2}", txt) else None
            title = re.split(r"\s*\d{4}-\d{2}-\d{2}", txt)[0].strip(" -")
            seen.add(m.group(1))
            out.append({"title": title, "url": m.group(1), "date": date})
    return out


def resolve(query):
    if query.startswith("http"):
        return {"title": None, "url": query, "date": None}
    for p in list_prospectuses(pages=9):
        if query.lower() in (p["title"] or "").lower():
            return p
    raise SystemExit(f"No prospectus matching {query!r}")


def base_company(title):
    """Drop the issue-type suffix so 'X Ltd.-General Public', 'X Ltd.-Foreign
    Employment', 'X Ltd. (Right Issue)' (same prospectus content) collapse to one."""
    t = re.sub(r"\s*\([^)]*\)\s*$", "", title or "")          # trailing (Right Issue)
    m = re.search(r"^(.*?(?:Ltd\.?|Limited|Company))(?=\s*[-–(]|\s*$)", t, re.I)
    return (m.group(1) if m else re.split(r"\s*[-–]\s*", t)[0]).strip()


def unique_prospectuses(pages=9):
    """Listing deduped to one row per company (issue-type variants merged)."""
    seen, out = set(), []
    for r in list_prospectuses(pages):
        b = base_company(r["title"]).lower()
        if b and b not in seen:
            seen.add(b)
            out.append({**r, "company": base_company(r["title"])})
    return out


# Section markers + 'नेपाली' (one per shareholder row) anchor the table page.
FOUNDER_KW = ("संस्थापक", "सञ्चालक", "शेयरधनी", "पृष्ठभूमि", "शेयरधनीको")


def find_table_pages(pdf_bytes):
    """0-based pages of the founder section (all 3 tables span a contiguous block,
    not one page), or None if the PDF has no Preeti text (scanned/CID -> whole PDF)."""
    pages = pdfplumber.open(io.BytesIO(pdf_bytes)).pages
    nat, sec, text_pages = [], [], 0
    for pg in pages:
        raw = pg.extract_text() or ""
        if len(raw) > 80:
            text_pages += 1
        uni = wrapper_nppttf2utf(raw)
        # one nationality cell per row — नेपाली for people, लागू नहुने for corporate promoters
        nat.append(uni.count("नेपाली") + uni.count("लागू नहुने"))
        sec.append(sum(uni.count(k) for k in FOUNDER_KW))
    if text_pages < len(pages) * 0.4:         # mostly scanned/CID -> Gemini reads whole PDF
        return None
    n = len(nat)
    rows = [i for i in range(n) if nat[i] >= 2]   # pages that actually contain table rows
    if not rows:                                  # table is a scanned image -> whole PDF
        return None
    # anchor on the row page whose section keywords are densest = the founder section.
    # restricting to row pages stops the keyword-only TOC (nat=0) from winning.
    peak = max(rows, key=lambda i: nat[i] + sec[i] * 3)

    def hot(i):                               # a founder-section page: rows or a title
        return 0 <= i < n and (nat[i] >= 2 or sec[i])
    lo = hi = peak
    while True:                               # extend to the farthest founder page within
        nlo = min((j for j in range(max(0, lo - 6), lo) if hot(j)), default=lo)  # ~6-page
        nhi = max((j for j in range(hi + 1, min(n, hi + 7)) if hot(j)), default=hi)  # reach
        if (nlo, nhi) == (lo, hi):            # — bridges the gap to the affiliations table
            break
        lo, hi = nlo, nhi
    if hi - lo + 1 > 14:                       # runaway (funds/debentures) -> bound it
        lo, hi = max(0, peak - 4), min(n - 1, peak + 9)
    return list(range(lo, hi + 1))


def slice_pdf(pdf_bytes, indices):
    r = PdfReader(io.BytesIO(pdf_bytes))
    w = PdfWriter()
    for i in indices:
        w.add_page(r.pages[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


SCHEMA_PROMPT = """This is a SEBON IPO prospectus (Nepali; legacy Devanagari font — read it visually, transcribe to Unicode). The promoter/director section has up to FOUR separate tables. Extract all that are present into this JSON object (and nothing else):

{
  "shareholders": [                  // table "आधारभूत शेयरधनीहरूको विवरण"
    {"sn": int, "name": str, "address": str|null, "age": int|null, "nationality": str,
     "education": str|null, "shares": int|null, "share_percent": number|null,
     "experience": str|null, "experience_sectors": [str], "proposed_business": str|null}
  ],
  "directors": [                     // table "संचालकहरूको विवरण"
    {"sn": int, "name": str, "address": str|null, "position": str, "nationality": str,
     "shares": int|null, "education": str|null, "experience": str|null,
     "experience_sectors": [str], "proposed_business": str|null}
  ],
  "director_affiliations": [         // table "संचालकहरू अन्य कम्पनी/संस्थासँग आवद्ध ... संलग्नताको विवरण"
    {"director_name": str, "director_address": str|null,
     "affiliations": [{"company": str, "address": str|null, "role": str|null,
                       "from": str|null, "to": str|null}]}
  ],
  "promoter_companies": [            // table "संस्थापक कुनै कम्पनी/संस्था भएमा ... संक्षिप्त विवरण (Profile) र उक्त कम्पनी/संस्थाका संचालकहरुको नाम" — only when a promoter/shareholder is itself a company
    {"sn": int, "company": str, "address": str|null, "profile": str|null,
     "directors": [{"name": str, "address": str|null}]}
  ]
}

Rules: convert Devanagari digits to Arabic. Parse every row into its own object (each affiliation, each promoter-company director). Use null / [] for absent columns or "लागू नहुने". Output only rows that actually appear. Any table absent -> []."""


def gemini_extract(pdf_bytes, key, model):
    payload = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "application/pdf",
                             "data": base64.b64encode(pdf_bytes).decode()}},
            {"text": SCHEMA_PROMPT}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json",
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model}:generateContent?key={key}")
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    resp = json.load(urllib.request.urlopen(req, timeout=240))
    um = resp.get("usageMetadata", {})
    data = json.loads(resp["candidates"][0]["content"]["parts"][0]["text"])
    cost = round(um.get("promptTokenCount", 0) * PRICE_IN
                 + um.get("candidatesTokenCount", 0) * PRICE_OUT, 4)
    return data, {"in": um.get("promptTokenCount", 0),
                  "out": um.get("candidatesTokenCount", 0), "cost_usd": cost}


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "prospectus").lower()).strip("-")[:50]


def process(p, key, model):
    company = base_company(p["title"]) if p.get("title") else None
    print(f"Prospectus: {company or p['url']}")
    slug = slugify(company or p["url"].rsplit("/", 1)[-1])

    pdf_cache = CACHE / f"{slug}.pdf"
    pdf = pdf_cache.read_bytes() if pdf_cache.exists() else _get(p["url"])
    pdf_cache.write_bytes(pdf)
    total_pages = len(PdfReader(io.BytesIO(pdf)).pages)

    # lipi finds the table page locally (free) so we send Gemini ~2 pages, not 57.
    table_pages = find_table_pages(pdf)
    if table_pages:
        send_pdf = slice_pdf(pdf, table_pages)
        print(f"  PDF {len(pdf)//1024} KB, {total_pages} pages -> table on page(s) "
              f"{[i + 1 for i in table_pages]} (lipi); sending those to Gemini")
    else:
        send_pdf = pdf
        print(f"  PDF {len(pdf)//1024} KB, {total_pages} pages -> no Preeti text, "
              f"sending whole PDF to Gemini")

    keys = ("shareholders", "directors", "director_affiliations", "promoter_companies")
    # Gemini can transiently return [] even at temp 0 — retry before giving up.
    tables, usage = {k: [] for k in keys}, {"in": 0, "out": 0, "cost_usd": 0.0}
    for attempt in range(3):
        data, u = gemini_extract(send_pdf, key, model)
        usage = {k: usage[k] + u[k] for k in usage}     # sum cost across retries
        if isinstance(data, dict) and any(data.get(k) for k in keys):
            tables = {k: data.get(k) or [] for k in keys}
            break
        print(f"  attempt {attempt + 1}: empty, retrying...")

    counts = {k: len(v) for k, v in tables.items()}
    result = {
        "company": company,
        "source_title": p.get("title"),
        "prospectus_url": p["url"],
        "issue_date": p.get("date"),
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total_pages": total_pages,
        "table_pages": [i + 1 for i in table_pages] if table_pages else "whole_pdf",
        "counts": counts,
        "llm_cost": usage,
        **tables,
    }
    OUT.mkdir(exist_ok=True)
    (OUT / f"{slug}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"  shareholders {counts['shareholders']}, directors {counts['directors']}, "
          f"affiliations {counts['director_affiliations']}, "
          f"promoter-cos {counts['promoter_companies']} | ${usage['cost_usd']}")
    print(f"Saved output/{slug}.json")
    return result


def main():
    key, model = gemini_key()
    CACHE.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]

    if args and args[0] == "--batch":      # process N companies not yet extracted
        want = int(args[1]) if len(args) > 1 else 10
        done = 0
        for p in unique_prospectuses(pages=9):
            if (OUT / f"{slugify(p['company'])}.json").exists():
                print(f"skip (done): {p['company']}")
                continue
            process(p, key, model)
            done += 1
            if done >= want:
                break
        print(f"\nbatch: {done} new companies extracted")
    else:
        process(resolve(args[0] if args else "Mount Everest"), key, model)


if __name__ == "__main__":
    main()
