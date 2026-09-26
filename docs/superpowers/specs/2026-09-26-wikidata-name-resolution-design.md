# Wikidata name resolution + cropper isolation

## Problem

OCR gives correct Bengali captions, but Gemini sometimes replaces a person's
name with one it remembers — e.g. the previous minister on a photo of the
current one. The manual corrections table (`translation_replacements.tsv`)
only covers names someone has added by hand.

## Goal

Before Gemini sees the caption, find people's names in the Bengali text and
replace them with their English Wikidata label, read from the Toolforge
Wikidata replica. Gemini then only translates the surrounding text.

Separately: move the photo/caption cropper into its own file, unchanged, and
guard it against accidental edits.

## Decisions

- **People only** (entities that are `instance of: human`, Q5). Organisations
  and places are out of scope.
- **Replace before translation.** Names not found on Wikidata are left for
  Gemini, as today. No review flag or hold.
- **Manual table wins.** Order: manual replacements → Wikidata → Gemini.
- **Fail open.** If the replica is unreachable or a query errors, log one
  warning and translate as today. Uploads never stop because of Wikidata.

## Name resolution — `src/name_resolver.py`

Public API: `resolve_names(text) -> (text, matches)`, where `matches` is a
list of `(bengali, english, qid)`. `main.py` logs each match:
`Wikidata: মুহাম্মদ ইউনূস → Muhammad Yunus (Q...)`.

### Matching

1. Tokenise the caption on whitespace and punctuation (`,` `।` `;` `:` etc.).
2. Build every window of 2–5 consecutive tokens. For each window also build a
   variant with a common case suffix stripped from the last token:
   `-এর`, `-ের`, `-র`, `-কে`, `-রা`, `-দের`. Single-token windows are never used.
3. One query per caption: find items whose **Bengali label or alias** exactly
   equals any candidate, keeping only items whose page links to Q5.
4. For each matched item fetch its **English label** and its sitelink count
   (`page_props.wb-sitelinks`). Items with no English label are dropped.
5. Same Bengali string → several humans: if the English labels agree, use it;
   otherwise use the item with the most sitelinks.
6. Replace spans longest-first, non-overlapping. When the match came from a
   suffix-stripped variant, the suffix is kept after the English name
   (`Muhammad Yunusের`) so Gemini still sees the grammatical case.

### Replica access

- Host `wikidatawiki.analytics.db.svc.wikimedia.cloud`, database
  `wikidatawiki_p`, via **PyMySQL** (new dependency).
- Credentials from `TOOL_REPLICA_USER` / `TOOL_REPLICA_PASSWORD`, which the
  Toolforge build service injects. `REPLICA_HOST` / `REPLICA_PORT` override for
  local testing through an SSH tunnel.
- Tables: `wbt_text`, `wbt_text_in_lang`, `wbt_term_in_lang`, `wbt_type`,
  `wbt_item_terms` for labels/aliases; `page`, `pagelinks`, `linktarget` for
  the Q5 filter; `page_props` for sitelink count.
- One connection per worker thread (thread-local), reconnect on drop.

## Pipeline change — `main.py` step 3

```
bengali_text = apply_translation_replacements(raw, table)
bengali_text, matches = resolve_names(bengali_text)   # new
translate_text(..., bengali_text, ...)
```

Nothing else in the pipeline changes.

## Cropper isolation — `src/cropper.py`

- `class Cropper` holds `find_white_separator`, `_background_fraction`,
  `find_separator_fallback`, `crop_side_whitespace`, `crop_image_sections`,
  copied **byte-for-byte** from `src/image_processor.py`.
- `class ImageProcessor(Cropper)` — all existing call sites, including
  `test_processor_cv.py`, keep working unchanged.
- `test_cropper_frozen.py` asserts the SHA-256 of `src/cropper.py`. Changing the
  cropper therefore requires deliberately updating the hash.
- `src/cropper.py` is where the standalone `D:\WikiTools\PID\cropper` version
  lands when it is reconciled.

## Testing

- `test_name_resolver.py`: stub the DB lookup; cover suffix stripping,
  longest-match-wins, ambiguous label resolved by sitelinks, single tokens
  ignored, DB failure returns text unchanged.
- One live query on Toolforge against a known current official.
- `test_cropper_frozen.py`, and `test_processor_cv.py` still passes.

## Out of scope

Organisations/places, review flags for unmatched names, a cached dictionary,
panel UI changes.
