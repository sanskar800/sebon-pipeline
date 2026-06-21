# sebon_prospectus

Extract the **founder/promoter/director background table** (आधारभूत शेयरधनी /
संस्थापक/सञ्चालकको पृष्ठभूमी) from SEBON IPO prospectuses into a clean JSON
schema — including each promoter's **other company involvements** (the
अन्य कार्य अनुभव column), which feeds the ownership/investment graph.

## How it works

1. **Find** — scrape `sebon.gov.np/prospectus` (plain HTML, paginated) → match a
   company → download its PDF.
2. **Locate the table page (local, free)** — the PDFs use a legacy **Preeti**
   font, so extracted text is ASCII (`dfpG6 Pe/]i6` = "Mount Everest"). `lipi`'s
   `npttf2utf` decodes it to Unicode, then a keyword search (`संस्थापक/नेपाली`)
   finds the table page(s). The table sits at very different pages across
   prospectuses (15-18, 17-21, 29-33), so a fixed range won't do.
3. **Extract** — send only those ~2-5 pages to **Gemini** for the JSON schema.
   Gemini reads visually (correct numbers + free-text columns); lipi conversion
   garbles digits so it's used only for finding, not the final data.

Sending ~3 pages instead of the whole 57-69 page PDF cuts cost to **~0.6 paisa
($0.006)** per prospectus, same accuracy. PDFs with no Preeti text
(scanned/CID-font) fall back to sending the whole PDF.

## Run

```bash
docker compose exec -T queue bash -c "cd /app/sebon_prospectus && python extract.py 'Mount Everest'"
# or a direct PDF url:  python extract.py https://sebon.gov.np/uploads/.../x.pdf
```

Output: `output/<slug>.json`. PDFs cached under `output/cache/` (gitignored).
Needs `GEMINI_API_KEY` (from `.env` or `../main_conglomerates/.env`) and runs in
the worker/queue container.

## Dependencies

`pdfplumber`, `pypdf`, and **`lipi`** (private — `ankamala/lipi`, for `npttf2utf`):

```bash
pip install pdfplumber pypdf "git+https://<TOKEN>@github.com/ankamala/lipi.git"
```

Only `npttf2utf` is used (pure-Python) — lipi's camelot/OCR (and their
`ghostscript`/`tesseract` system deps) aren't needed for this flow.

## JSON schema

The founder section has **three separate tables**; we extract each into its own
array (the table position varies, hence `table_pages`):

```jsonc
{
  "company": "...", "prospectus_url": "...", "issue_date": "...",
  "total_pages": 57, "table_pages": [15, 16, 17, 18],
  "counts": { "shareholders": 8, "directors": 4, "director_affiliations": 4 },
  "llm_cost": { "in": ..., "out": ..., "cost_usd": ... },

  "shareholders": [            // table 1: आधारभूत शेयरधनीहरूको विवरण
    { "sn": 1, "name": "श्री अर्जुन प्रसाद पौडेल", "address": "...", "age": 51,
      "nationality": "नेपाली", "education": "...", "shares": 2628645,
      "share_percent": 43.67, "experience": "...", "experience_sectors": ["कृषि"],
      "proposed_business": "व्यापार" }
  ],
  "directors": [               // table 2: संचालकहरूको विवरण
    { "sn": 1, "name": "श्री अर्जुन प्रसाद पौडेल", "address": "...",
      "position": "अध्यक्ष", "nationality": "नेपाली", "shares": 2628645,
      "education": "...", "experience": "...", "experience_sectors": [],
      "proposed_business": "व्यापार" }
  ],
  "director_affiliations": [   // table 3: संचालकहरू अन्य कम्पनीसँग संलग्नताको विवरण
    { "director_name": "श्री अर्जुन प्रसाद पौडेल", "director_address": "...",
      "affiliations": [
        { "company": "रिचेत जलविद्युत कम्पनी लिमिटेड", "address": null,
          "role": "अध्यक्ष", "from": null, "to": "हाल" }
      ] }
  ]
}
```

`table_pages` is `"whole_pdf"` when the Preeti finder couldn't read the PDF.
`director_affiliations` are the cross-company links — match them to the registry
(DOI/FNCCI/CNI/NEPSE) with the conglomerate linker to grow the ownership graph.
