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


# Section markers + 'नेपाली' (one per shareholder row) anchor the table page.
FOUNDER_KW = ("संस्थापक", "सञ्चालक", "शेयरधनी", "पृष्ठभूमि", "शेयरधनीको")


def find_table_pages(pdf_bytes):
    """0-based page indices of the founder table, or None if the PDF has no Preeti
    text (scanned/CID -> caller sends the whole PDF)."""
    pages = pdfplumber.open(io.BytesIO(pdf_bytes)).pages
    nat, sec, had_text = [], [], False
    for pg in pages:
        raw = pg.extract_text() or ""
        if raw:
            had_text = True
        uni = wrapper_nppttf2utf(raw)
        nat.append(uni.count("नेपाली"))
        sec.append(sum(uni.count(k) for k in FOUNDER_KW))
    if not had_text or not any(nat):
        return None
    best = max(range(len(nat)), key=lambda i: nat[i] + sec[i] * 3)
    out = {best}
    for d in (-2, -1, 1, 2):                 # table can span consecutive pages
        j = best + d
        if 0 <= j < len(nat) and (nat[j] >= 2 or sec[j]):
            out.add(j)
    return sorted(out)


def slice_pdf(pdf_bytes, indices):
    r = PdfReader(io.BytesIO(pdf_bytes))
    w = PdfWriter()
    for i in indices:
        w.add_page(r.pages[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


SCHEMA_PROMPT = """This is a SEBON IPO prospectus (Nepali; legacy Devanagari font — read it visually, transcribe to Unicode). The promoter/director section has THREE separate tables. Extract all three into this JSON object (and nothing else):

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
  ]
}

Rules: convert Devanagari digits to Arabic. Parse every affiliation row into its own object. Use null / [] for absent columns or "लागू नहुने". Output only rows that actually appear. Any table absent -> []."""


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


def main():
    query = sys.argv[1] if len(sys.argv) > 1 else "Mount Everest"
    key, model = gemini_key()
    CACHE.mkdir(parents=True, exist_ok=True)

    p = resolve(query)
    print(f"Prospectus: {p['title'] or p['url']}")
    slug = slugify(p["title"] or p["url"].rsplit("/", 1)[-1])

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

    keys = ("shareholders", "directors", "director_affiliations")
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
        "company": p["title"],
        "prospectus_url": p["url"],
        "issue_date": p["date"],
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
          f"affiliations {counts['director_affiliations']} | ${usage['cost_usd']}")
    print(f"Saved output/{slug}.json")


if __name__ == "__main__":
    main()
