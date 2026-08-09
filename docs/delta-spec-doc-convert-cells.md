# Delta Spec — `doc_convert`: cell-addressed output format

**Scope:** authoring MCP schema + validator + `doc_convert` widget runtime + UI + the
`hslu-aire-doc-service` conversion endpoint that produces the payload.
**Status:** proposal. Backward compatible; no migration required.

---

## 0. Decisions

The three that determine whether this is memory-safe. All three are settled; the rest of the
document is detail that follows from them.

| | Decision | Settled | Consequence if reversed |
|---|---|---|---|
| **D1** | Merged ranges come from a **streaming pass** over the sheet XML; the cell walk stays `read_only=True`. | ✅ streaming | `read_only=False` peaks at ~120× file size → ~3.5 GB at the upload limit → OOM next to Torch (§12.2) |
| **D2** | App Runner `MaxConcurrency` is **2**, not the default 100. | ✅ **applied** 2026-08-09 | 100 concurrent requests × (30 MiB upload buffer + parse peak) in one 4 GB instance (§12.4) |
| **D3** | `.xls` is **out of scope** for `cells`; `.xlsx`/`.xlsm`/`.csv`/`.tsv` only. | ✅ dropped | `xlrd` has no streaming mode and cannot expose formulas (§3.1) |

The instance stays at **1 vCPU / 4 GB**. Raising it was considered and rejected: App Runner has
no 1 or 2 vCPU configuration above 6 GB, so 8 GB forces 4 vCPU and roughly doubles the always-on
floor to solve a problem D1 removes for free.

Still open, and none of them memory-relevant: §14.

---

## 1. Why

The current artifact converts any document to Markdown. For spreadsheets this is lossy in a
way that produces no error: merged cells collapse, multi-row headers become data rows, and
values silently land under the wrong column heading. A Markdown table also has no addresses,
so nothing can be written back into the source sheet.

A cell-addressed serialization fixes both. Each value carries its own row and column, so a
column shift is structurally impossible, and every value has a coordinate that survives the
round trip.

This is the format already in production use in the LV/Kalkulation pipeline. This spec makes
it available in training material so learners can compare the two representations directly.

> **Correction to the above claim.** `hslu-aire-doc-service` ships a serializer of this shape
> today (`app/convert.py:52`, `serialize_excel`), but it emits **0-based numeric** row and
> column indices, no sheet header, no merge annotation and no escaping. It is *a* row-wise
> format, not *this* one. Either the LV pipeline runs a different serializer, or the
> production claim is inaccurate. §9 lists the exact delta and treats it as work, not as an
> already-satisfied precondition.

---

## 2. Schema change

Add one optional field to the `doc_convert` artifactype:

```
outputFormat?: "markdown" | "cells" | "both"     // default: "markdown"
formulaMode?:  "silent" | "error" | "formula"    // default: "silent"
```

Constraints:

- Absent `outputFormat` behaves exactly as today. No existing artifact changes behaviour.
- `formulaMode` is ignored unless the resolved output includes `cells`.
- Both fields must appear in `describe_course_schema` under `artifactTypes.doc_convert.fields`,
  otherwise authoring clients will keep emitting artifacts without them.

`recordsInteraction` stays `docconvert`; `tracked` stays `true`. Completion fires on the first
successful conversion regardless of format — do not require the learner to run both.

---

## 3. Applicability by input type

| Input | `markdown` | `cells` |
|---|---|---|
| `.xlsx`, `.xlsm` | yes | yes |
| `.csv`, `.tsv` | yes | yes (single sheet, column letters synthesised A, B, C, …) |
| `.xls` (legacy BIFF) | yes | **not applicable** — decision D3 |
| `.pdf`, `.docx`, `.pptx`, images | yes | **not applicable** |

When `cells` or `both` is requested for a non-tabular input, return the Markdown output and a
single explanatory line in place of the cells pane:

```
Zellenformat nicht anwendbar — diese Datei hat keine Tabellenstruktur.
```

Do not fail the conversion and do not silently drop the request.

### 3.1 Routing notes

- `.csv`/`.tsv` currently route to the Docling/Markdown branch (`DOC_EXTS` in
  `app/convert.py:11`). `cells` for CSV requires a second routing rule: extension in
  `{csv, tsv}` **and** resolved format includes `cells` → tabular branch. Markdown for CSV
  stays on the Docling path so the `markdown` output is unchanged.
- CSV parsing for the cells branch: delimiter from the extension (`,` / `\t`), no sniffing,
  UTF-8 with BOM tolerated, quoting per RFC 4180. Sheet name is the filename without
  extension. Every physical line is a row, 1-based, including blank lines (which are then
  omitted per rule 3 but still consume their row number).
- `.xls` (legacy BIFF) is **excluded** from the cells path — decision D3. `openpyxl` cannot read
  it; `xlrd` can, but has no streaming mode and materialises the whole workbook, which
  reintroduces exactly the memory profile D1 removes. It also cannot expose formula text, so
  `formulaMode: "formula"` would be a permanent lie. `.xls` keeps its Markdown path unchanged
  and returns the §3 "nicht anwendbar" line for `cells`.
- Everything else: no attempt at table extraction from PDF/DOCX. "Not applicable" is a
  deliberate answer, not a missing feature — see §14.

---

## 4. Cell format grammar

One line per row that contains at least one non-empty cell.

```
R{row}| {col}:{value} || {col}:{value} || …
```

Rules, in order of precedence:

1. **Row numbers are the sheet's own**, 1-based. Rows that are entirely empty are omitted, and
   their numbers are simply missing from the output. The gap is the information — do not
   renumber.
2. **Column letters are the sheet's own** (`A`…`Z`, `AA`…). For CSV/TSV, synthesise them by
   position.
3. **Empty cells are omitted entirely.** This is the distinction between "no value" and a
   value that happens to be empty, and it is the main reason the format is unambiguous.
4. **Cell separator is ` || `** (space, two pipes, space).
5. **Values are stored values, not displayed values.** No thousands separators, no currency
   symbols added, no rounding. `196400`, not `196'400`.
   - Dates and datetimes: ISO 8601 (`2026-09-02`, `2026-09-02T14:30:00`).
   - Booleans: `TRUE` / `FALSE`.
   - Text: verbatim. Replace internal newlines with ` / `.
6. **Escaping:** a literal `|` in a value becomes `\|`. A literal `\` becomes `\\`. Nothing else
   is escaped. *(Amended by 6a/6b below.)*
7. **Merged cells:** emit the value once, at the anchor cell, annotated with the span:
   `A1[merged A1:D1]:Objektliste 2026`. The covered cells are not emitted separately. This is a
   deliberate improvement over Markdown, where the merge disappears.
8. **Multiple sheets:** precede each sheet's block with `# Sheet: {name}` on its own line, in
   workbook order. Single-sheet workbooks still get the header, for consistency.

### 4.1 Amendments — ambiguities the rules above leave open

**6a. Escape order.** Escape `\` first, then `|`. The reverse order double-escapes: `a|b` →
`a\|b` → `a\\|b`, which unescapes to `a\` + a stray separator.

**6b. Escape braces.** A literal `{` becomes `\{` and `}` becomes `\}`, in **all** formula
modes, not only in `formula`. Without this, a text cell whose value ends in `{=x}` is
indistinguishable from a formula annotation, and the grammar stops being parseable without
knowing which mode produced the line. The cost is two more characters in a rare case; the
benefit is that a parser needs no out-of-band mode flag.

**5a. Empty vs. blank.** A cell is omitted when its stored value is `None`, or a string that is
empty. A string of only whitespace is **kept**, verbatim — it is a stored value, and in
practice it is a finding (someone typed a space to "clear" a cell). Do not strip leading or
trailing whitespace from kept values.

**5b. Numbers.** Emit the shortest round-trip decimal representation of the stored double. No
exponent notation below 1e16; above that, Python's `repr` form is acceptable. Integers carry no
`.0`. Do not round — rule 5 forbids it, and `app/convert.py:27` currently rounds to 4 decimals
on the *stats* path, which must not leak into the cells path.

**5c. Times and durations.** A time-only cell is `HH:MM:SS`. A duration formatted as `[h]:mm`
has no ISO form and is emitted as its stored number of days (`0.5` for 12 h) — flag it in the
UI label, do not invent a unit.

**5e. Date vs. datetime.** Excel has no date-only type; a date cell is a serial number with a
date number format, and openpyxl returns `datetime` with a midnight time. Rule: **a datetime
whose time is exactly `00:00:00` is emitted date-only.** The cost is that a genuine midnight
timestamp loses its `T00:00:00`; the alternative — emitting `2026-09-02T00:00:00` for every
ordinary date — is worse and far more common.

**5d. Percentages.** Stored value, so `0.075`, never `7.5%`. This is the single most common
surprise for learners and should be called out in the exercise text.

**1a. Row/column ceiling.** Only the sheet's used range is walked. Columns beyond `ZZ` are
addressed with the normal base-26 scheme (`AAA`…); there is no cap.

**7a. Merge with an empty anchor.** A merged range whose anchor cell is empty emits nothing —
no address, no annotation. The merge is not information on its own.

**8a. Sheet names.** Emitted verbatim after `# Sheet: `, including `|` and newlines? No —
newlines in a sheet name are impossible in Excel; `|` is possible and is **not** escaped, since
the header line has no cell grammar to protect. Hidden sheets are included, in workbook order,
with no marker (a hidden sheet is not a different kind of data).

### 4.2 Formal grammar

```ebnf
document   = sheet , { sheet } ;
sheet      = "# Sheet: " , sheetname , NL , { row , NL } ;
row        = "R" , digits , "| " , cell , { " || " , cell } ;
cell       = colref , ":" , value ;
colref     = letters , [ "[merged " , ref , ":" , ref , "]" ] ;
ref        = letters , digits ;
value      = { escaped } , [ " {" , formula , "}" ] ;
escaped    = ? any char except \ | { } ? | "\\\\" | "\\|" | "\\{" | "\\}" ;
formula    = "=" , { escaped } ;
```

Parser contract: the separator is the first unescaped `:` **after the optional
`[merged …]` group** — not simply the first `:` in the cell. The annotation contains a `:` of
its own, so a naive first-colon split turns `A[merged A1:D1]:Objektliste 2026` into colref
`A[merged A1` and value `D1]:Objektliste 2026`. Match the colref instead:

```
^([A-Z]+)(?:\[merged [^\]]+\])?:(.*)$
```

Everything after that `:` is the value, unescaped per 6a/6b. This is why `:` itself is not
escaped and does not need to be — the colref is a bounded prefix, not a delimiter search.

*(Found by the round-trip test in §6.2 during implementation; the earlier "first unescaped
colon" contract was wrong.)*

### 4.3 Formula handling

| `formulaMode` | Emitted |
|---|---|
| `silent` | the computed value only |
| `error` | the computed value, or the error literal verbatim (`#VALUE!`, `#REF!`, `#DIV/0!`) |
| `formula` | the computed value followed by the formula in braces: `D7:369200 {=SUM(D4:D5)}` |

`error` must not substitute, blank, or coerce error literals. A broken formula in the source is
a finding, not noise.

**Additions:**

- `silent` means the error cell is **omitted like an empty cell**. That is what makes `error`
  a meaningful contrast. State it explicitly, because "the computed value only" also reads as
  "emit whatever is cached", which would leak `#REF!` into `silent`.
- Detect errors by cell type (`openpyxl` `cell.data_type == "e"`), not by string prefix.
  **Caveat, verified during implementation:** a cell whose *text* is `#REF!` also comes back as
  `data_type == "e"` — the type is inferred from the content on write, by openpyxl and by Excel
  itself. The earlier claim that "a text cell containing `#REF!` is data and must survive
  `silent`" is therefore not implementable and has been dropped. Anything that looks like an
  error literal is treated as one. In practice this is what a user means anyway.
- `formula` requires the workbook loaded **twice** — `data_only=True` for cached values,
  `data_only=False` for formula text. See §9.2.
- **No cached value.** A workbook last written by openpyxl or LibreOffice may carry formulas
  with no cached result. Then `formula` emits `D7:{=SUM(D4:D5)}` (empty value, formula kept),
  `error` and `silent` omit the cell, and the response carries
  `warnings: ["no cached formula results — Datei zuletzt nicht von Excel gespeichert"]`.
- Array/spill formulas: annotate only the anchor, same rule as merges. Shared formulas are
  expanded by openpyxl to their per-cell text; emit that.

---

## 5. UI

- `markdown` — unchanged from today.
- `cells` — single pane, monospace, no wrapping, horizontal scroll.
- `both` — two panes with a toggle (mobile: tabs; desktop: side by side). Markdown on the left.
- Copy button per pane.
- Show the resolved `formulaMode` as a small label on the cells pane, so learners can see which
  variant they are looking at without reading the artifact source.

### 5.1 States the panes must handle

| State | Cells pane shows |
|---|---|
| non-tabular input | the §3 sentence, no monospace frame, no copy button |
| tabular but zero non-empty cells | `Keine Zellen mit Inhalt gefunden.` |
| output truncated (§12.1) | the content plus a trailing `… [gekürzt: N von M Zeilen]` line and a persistent banner |
| conversion failed | the widget's existing error state; the Markdown pane is not shown either |

### 5.2 Detail

- Copy copies the **raw** serialization including the `# Sheet:` headers, not the rendered DOM.
- The sheet header lines are sticky within the scroll container for workbooks with >1 sheet.
- Horizontal scroll is on the pane, never on the page body.
- The `formulaMode` label shows the **resolved** mode, and when the request was downgraded
  (§12.3) it shows the downgrade: `formula → silent (keine gespeicherten Formelergebnisse)`.
- Accessibility: the pane is a `<pre>` inside a `role="region"` with an accessible name; it must
  be focusable so it can be scrolled by keyboard.
- Do not syntax-highlight the cells output. The whole point is that it is inspectable plain
  text; colouring it invites the belief that a renderer is interpreting it.

---

## 6. Acceptance criteria

> **The fixture must be a committed binary.** openpyxl never evaluates formulas, so a workbook
> it writes carries no cached results and `data_only=True` returns `None` for every formula
> cell — the expected values below would be unreachable if the fixture were generated in a
> test. `tests/make_fixtures.py` builds it and injects the cached `<v>` elements Excel would
> have written; the result is committed under `tests/fixtures/`.

Fixture `objektliste.xlsx`, single sheet named `Q3`:

```
      A                 B                  C                D
1   Objektliste 2026    ← merged A1:D1
2                       Fläche  ← merged B2:C2              Ertrag
3   Liegenschaft        Wohnen             Gewerbe          Miete/Jahr
4   Weidgasse 14        820                140              196400
5   Weidgasse 16        760                (empty)          172800
6   (entirely empty row)
7   Zwischensumme       =SUM(B4:B5)        =SUM(C4:C5)      =SUM(D4:D5)
```

*(Row 7 is corrected from the original draft, which showed a literal `140` in one column and a
single formula in another while asserting three computed values. The three subtotal formulas
above are what produce the expected output.)*

With `outputFormat: "cells"`, `formulaMode: "silent"`, the output must be exactly:

```
# Sheet: Q3
R1| A[merged A1:D1]:Objektliste 2026
R2| B[merged B2:C2]:Fläche || D:Ertrag
R3| A:Liegenschaft || B:Wohnen || C:Gewerbe || D:Miete/Jahr
R4| A:Weidgasse 14 || B:820 || C:140 || D:196400
R5| A:Weidgasse 16 || B:760 || D:172800
R7| A:Zwischensumme || B:1580 || C:140 || D:369200
```

Assert specifically:

- `R6` is absent and `R7` is not renumbered to `R6`.
- `C5` is absent from `R5` — not present as an empty value.
- `196400` carries no thousands separator, regardless of the cell's display format.
- The two merges are annotated at their anchors and nowhere else.

With `formulaMode: "formula"`, the last line is exactly:

```
R7| A:Zwischensumme || B:1580 {=SUM(B4:B5)} || C:140 {=SUM(C4:C5)} || D:369200 {=SUM(D4:D5)}
```

With the same fixture and `outputFormat: "markdown"`, no assertion is made about correctness —
the point of the exercise is that this output is plausible and wrong. Do not "fix" the Markdown
path to compensate.

### 6.1 Further fixtures

| Fixture | Exercises | Key assertion |
|---|---|---|
| `escaping.xlsx` | a cell `Fläche A|B`, a cell `C:\Temp\x`, a cell `{=nicht=formel}` | `A1:Fläche A\|B`, `B1:C:\\Temp\\x`, `C1:\{=nicht=formel\}` — and `B1` splits on its **first** `:`, yielding value `C:\\Temp\\x` |
| `fehler.xlsx` | `=1/0`, `=SVERWEIS(...)` → `#NV`, text cell `#REF!` | `silent`: only the text cell survives. `error`: all three, error literals verbatim. |
| `mehrblatt.xlsx` | 3 sheets, middle one empty, one hidden | three `# Sheet:` headers in workbook order; the empty sheet's header is present with no rows under it |
| `typen.xlsx` | date, datetime, time, bool, 7.5 % , `0.1+0.2` | `2026-09-02`, `2026-09-02T14:30:00`, `14:30:00`, `TRUE`, `0.075`, `0.30000000000000004` |
| `luecken.csv` | blank line 3, trailing empty columns, quoted `a,b` | `R3` absent, no trailing empty cells, `A4:a,b` unescaped (comma is not special) |
| `gross.xlsx` | 50 000 rows | truncation at the §12.1 limit, marker line present, no timeout |
| `bericht.pdf` | non-tabular | the §3 sentence, HTTP 200, Markdown pane populated |

### 6.2 Round-trip test

For every tabular fixture: serialize → parse with a reference parser → compare the
`{sheet, row, col} → value` map against a direct `openpyxl` read of the same workbook. Any
divergence is a bug in the serializer, the escaping, or the grammar — this is the test that
justifies the format's central claim.

---

## 7. Service contract (`hslu-aire-doc-service`)

`POST /convert` gains two optional multipart form fields alongside `file`:

| field | values | default |
|---|---|---|
| `outputFormat` | `markdown` \| `cells` \| `both` | `markdown` |
| `formulaMode` | `silent` \| `error` \| `formula` | `silent` |

Unknown values → `400` with `detail: "invalid outputFormat: <x>"`. Absent fields → today's
behaviour, byte for byte.

### 7.1 Response

The existing keys are untouched. `cells` is **added**:

```jsonc
{
  "kind": "excel",                    // unchanged
  "filename": "objektliste.xlsx",
  "excel": {
    "markdown":   "...",              // unchanged
    "serialized": [ ... ],            // unchanged, DEPRECATED — see §9.1
    "analysis":   [ ... ],            // unchanged
    "cells": {                        // NEW, present iff resolved format includes cells
      "text": "# Sheet: Q3\nR1| ...", // the complete serialization, all sheets
      "sheets": [ { "name": "Q3", "text": "R1| ...", "rows": 6, "cells": 19 } ],
      "formulaMode": "silent",        // resolved, post-downgrade
      "requestedFormulaMode": "formula",
      "truncated": false,
      "warnings": []
    }
  }
}
```

For a non-tabular input the response stays `kind: "markdown"` and gains:

```jsonc
{ "cells": { "applicable": false, "reason": "no_table_structure", "message": "Zellenformat nicht anwendbar — diese Datei hat keine Tabellenstruktur." } }
```

`applicable: false` and HTTP 200 — §3 forbids failing.

### 7.2 Why `text` *and* `sheets`

`text` is what the copy button copies and what an LLM prompt embeds. `sheets` is what the UI
paginates and what the interaction record counts. Deriving one from the other means re-parsing;
both are cheap to emit.

---

## 8. Validator rules (authoring MCP)

| Condition | Result |
|---|---|
| `outputFormat` not in enum | **error** — `outputFormat muss markdown, cells oder both sein` |
| `formulaMode` not in enum | **error** |
| `formulaMode` set, `outputFormat` absent or `markdown` | **warning** — ignored, drop it or set a format that includes cells |
| `outputFormat: "cells"`, artifact's sample/fixture file has a non-tabular extension | **warning** — the learner will only see the "nicht anwendbar" line |
| `outputFormat: "both"` and the section already renders two artifacts side by side | **info** — layout will be cramped on mobile |
| `recordsInteraction` ≠ `docconvert`, or `tracked` ≠ true | **error**, unchanged from today |

Warnings never block publication; `describe_course_schema` must document the enums and the
defaults, or authoring clients will keep omitting both fields (§2).

---

## 9. Delta against the current implementation

### 9.1 `serialize_excel` (`app/convert.py:52`)

| Aspect | Today | This spec |
|---|---|---|
| row number | `enumerate()` index, 0-based | `cell.row`, 1-based |
| empty-row gaps | not preserved (index is positional) | preserved by construction |
| column | `cell.column - 1`, numeric | `get_column_letter(cell.column)` |
| newline in cell | literal `\n` | ` / ` |
| escaping | none | `\\`, `\|`, `\{`, `\}` |
| merged cells | not handled (impossible under `read_only=True`) | anchor-annotated |
| sheet header | out-of-band JSON field | `# Sheet: {name}` line |
| formulas | `data_only=True`, cached value only | three modes |
| dates | `str(datetime)` → `2026-09-02 00:00:00` | `2026-09-02` |
| booleans | `str(True)` → `True` | `TRUE` |

**Do not change `serialize_excel` in place.** Its output shape is a published field of
`/convert` and something downstream may parse the 0-based form. Add `serialize_cells()`
alongside, mark `serialized` deprecated in the README, and remove it only after confirming no
consumer reads it.

### 9.2 openpyxl constraints this imposes

- `read_only=True` gives no merged ranges at all — `ReadOnlyWorksheet` has no `merged_cells`
  attribute, so the access raises `AttributeError` rather than returning an empty set. Rule 7
  therefore cannot be satisfied by the read-only worksheet API. It does **not** follow that
  `read_only=False` is required: see §12.2, the merges are read separately from the sheet XML
  and the cell walk stays read-only.
- `formula` mode needs two `load_workbook` calls on the same bytes (`data_only=True` and
  `False`). Load the values pass first and short-circuit the second when the sheet has no
  formula cells at all.
- `cell.is_date` distinguishes a date-formatted number from a plain number; a bare `time` is
  `datetime.time`; Excel's 1900 leap-year bug is openpyxl's problem, not ours.
- `.xls` via `xlrd`: no `data_type == "e"` equivalent for cached errors and no formula text.

### 9.3 Files touched

- `app/cells.py` — **new module** (rather than growing `convert.py`, which already does three
  jobs): `serialize_xlsx()`, `serialize_csv()`, `build_cells()`, `_merge_refs()`, `_fmt()`,
  `_escape()`. `convert.py` is untouched.
- `app/main.py` — two new `Form(...)` params, validation, response assembly.
- `requirements.txt` — no new dependency. `zipfile`/`xml.etree` are stdlib; `xlrd` is not needed
  because `.xls` is out of scope (D3).
- App Runner autoscaling configuration — `MaxConcurrency` (D2, §12.4). Infrastructure, not code.
- `README.md` — API section, deprecation note on `serialized`.
- `tests/test_cells.py`, `tests/make_fixtures.py`, `tests/fixtures/objektliste.xlsx` — 25 tests
  covering §6, escaping, value formatting, errors, multi-sheet, CSV, applicability and the
  §6.2 round trip. All passing.

---

## 10. Interaction record

`recordsInteraction: docconvert` payload gains, additively:

```jsonc
{
  "outputFormat":  "both",     // resolved
  "formulaMode":   "error",    // resolved, post-downgrade
  "applicable":    true,       // false for non-tabular + cells
  "sheetCount":    3,
  "cellCount":     412,        // non-empty cells emitted
  "truncated":     false,
  "paneViewed":    "cells"     // last pane the learner had open, "both" if toggled
}
```

Existing consumers ignore unknown keys; no schema version bump. `paneViewed` is the only field
that answers the question the exercise exists to ask — whether learners actually look at the
cell format or stay on the familiar Markdown.

---

## 11. Exercise framing (course-side, non-normative)

The comparison only teaches if the learner is pointed at the specific failure. Suggested
prompts for the section text around a `both` artifact:

1. Find `196400` in the Markdown output. Which column heading is above it? Which column is it
   actually in?
2. `R6` is missing. Why is that not a bug?
3. `C5` does not appear. What would a Markdown table show there, and what is the difference
   between "empty" and "not present"?
4. Switch `formulaMode` to `error`. Which cell appears that was not there before?

Do not ship a "corrected" Markdown fixture. §6 is explicit: the Markdown path stays wrong,
because the wrongness is the lesson.

---

## 12. Limits and failure modes

### 12.1 Truncation

| Limit | Value | On exceed |
|---|---|---|
| rows per sheet | 5 000 | stop, append `… [gekürzt: 5000 von M Zeilen]`, `truncated: true` |
| sheets per workbook | 50 | stop, append a `# … [gekürzt: 50 von N Blättern]` line |
| total `text` bytes | 4 MiB | stop at the row boundary that crosses it, same marker |
| upload bytes | `MAX_UPLOAD_BYTES`, 30 MiB today | existing `413` |

Truncation is always visible — in the text, in `truncated`, and in the UI banner (§5.1). A
silent cap would make the format's completeness claim false.

### 12.2 Resource sizing — read merges without loading the sheet

**Do not use `read_only=False` to obtain merged ranges.** Measured twice — Python allocations
on a 0.8 MiB workbook, and peak RSS in fresh processes on a 3.0 MiB one (50 001 rows × 12
columns, one merge):

| approach | merged ranges | alloc peak (0.8 MiB file) | peak RSS (3.0 MiB file) |
|---|---|---|---|
| `read_only=True` alone | `AttributeError` | — | — |
| `zipfile` + `iterparse` over the sheet part | correct | **1.9 MiB** | **54 MB** |
| `read_only=False` | correct | **96.8 MiB** | **311 MB** |

The RSS gap understates the difference: the 54 MB figure covers the *complete* serialization
(walk, escape, emit), while the 311 MB figure is `load_workbook` alone, before a single cell is
read. `read_only=False` runs at roughly 100× file size, so the 30 MiB upload limit lands near
3 GB — on a 1 vCPU / 4 GB instance that also holds Torch for the Docling path, that is an OOM,
not a slowdown. The streaming path also finished in 1.0 s against 3.0 s.

**Required approach:** walk cells with `read_only=True` (streaming, bounded), and read
`<mergeCells>` in a separate streaming pass over the same sheet part, clearing elements as they
close. Both passes are O(1) in memory. `<mergeCells>` follows `sheetData` in the schema, so the
pass traverses the row elements — clearing each `row`/`c` on close keeps the peak flat.

Raising the instance is the wrong fix and an expensive one: App Runner offers no 1 or 2 vCPU
configuration above 6 GB, so 8 GB forces 4 vCPU. Provisioned memory at $0.007/GB-h (Ireland;
Frankfurt is slightly higher) takes the always-on floor from ~$20 to ~$41 per month, and active
compute from 1 to 4 vCPU. If headroom is genuinely needed later, **2 vCPU / 6 GB** is the next
sensible step; 4 vCPU / 8 GB is not.

| vCPU | supported memory |
|---|---|
| 0.25 | 0.5, 1 GB |
| 0.5 | 1 GB |
| 1 | 2, 3, 4 GB ← today |
| 2 | 4, 6 GB |
| 4 | 8, 10, 12 GB |

### 12.3 Degradation, not failure

| Situation | Behaviour |
|---|---|
| `.xls` + any cells request | §3 "nicht anwendbar" line, HTTP 200, Markdown pane populated (D3) |
| no cached formula results | §4.3, formula kept, value empty |
| corrupt/password-protected workbook | `400`, `detail: "Datei kann nicht gelesen werden (beschädigt oder passwortgeschützt)"` — not a `500` |
| sheet with a broken dimension record | fall back to scanning all cells; do not trust `ws.max_row` |

### 12.4 Request concurrency (D2)

§12.2 bounds the memory of **one** conversion. Nothing so far bounds how many run at once, and
that is where the instance actually dies.

App Runner routes up to `MaxConcurrency` requests to a single container instance before it
starts another one. **The default is 100.** For a JSON API that is reasonable; for a service
where every request holds a document in memory it is not. Two costs stack per in-flight request:

1. **The upload buffer.** `app/main.py:28` does `data = await file.read()` — the entire upload
   is materialised in RAM before any dispatch, and it stays there for the life of the request.
   At the 30 MiB limit, 100 concurrent uploads are ~3 GB of buffers **before a single cell is
   parsed**. This is true today, independent of this spec.
2. **The parse peak.** Bounded by §12.2, but multiplied by the same factor.

Set `MaxConcurrency: 2` on the service's autoscaling configuration. App Runner then scales
*horizontally* — a third simultaneous request starts a second instance instead of sharing the
first one's 4 GB.

**Applied 2026-08-09.** The service was on AWS's `DefaultConfiguration` (100 / 1 / 25). It now
uses `doc-service-lowconc`:

| setting | was | now | why |
|---|---|---|---|
| `MaxConcurrency` | 100 | **2** | caps per-instance memory at 2 × (upload + parse) |
| `MinSize` | 1 | 1 | keeps the always-on instance; the ~$20/month floor is unchanged |
| `MaxSize` | 25 | **10** | 20 concurrent conversions — headroom for a lecture-sized burst, without the old 25-instance cost exposure |

```
arn:aws:apprunner:eu-central-1:302174038010:autoscalingconfiguration/doc-service-lowconc/1/e50e16e8fccc41e29a1d4dd93195074e
```

This is a change to the App Runner autoscaling configuration resource, not to the image. It
costs nothing at idle — provisioned memory is billed for `MinSize` instances, and scaled-out
instances only exist while they are serving. The doc service is **not** Terraform-managed
(`hslu-aire-server/terraform` covers only the portal, which consumes this service through a
`doc_service_url` variable), so there is no state to reconcile. App Runner autoscaling
configurations are immutable — changing these values means creating revision 2 and re-attaching.

Note that `/convert` is declared `async def` but performs blocking CPU work (openpyxl, Docling)
inline, so requests already serialise on the event loop within one process. That limits *parse*
concurrency by accident, but not upload buffers — queued requests have already read their bytes.
`MaxConcurrency` is the control that actually bounds both. Do not "fix" the blocking handler by
moving it to a threadpool without setting `MaxConcurrency` first; that would raise parallelism
and memory at the same time.

---

## 13. Out of scope

- Write into the source file. The addresses make it possible; nothing in this spec does it.
- Cell formatting, colours, and conditional formatting. Both formats drop them, and that is
  itself part of the lesson.
- Charts, images, and pivot tables embedded in the sheet.
- Table extraction from PDF/DOCX into cells. Docling can produce tables; mapping them to
  synthetic addresses would produce coordinates that do not exist in any source file, which
  defeats the round-trip property in §1.
- A parser. This spec defines a serialization; §6.2 needs a reference parser for testing, but
  shipping one as a product surface is separate work.
- Streaming/chunked responses for large workbooks. §12.1 caps instead.

---

## 14. Open decisions

1. **`serialized` removal.** Needs a consumer audit in `hslu-aire-server` before the field can
   go. Until then the response carries two row-wise formats, which is confusing.
2. ~~**`.xls` in the first cut.**~~ — **resolved, D3: out of scope.** `.xls` is treated as
   non-tabular for `cells`; its Markdown path is unchanged.
3. ~~**Row limit vs. instance size**~~ — **resolved.** Measured (§12.2): merges come from a
   streaming XML pass, the cell walk stays `read_only=True`, memory is bounded, and the
   instance stays at 1 vCPU / 4 GB. The row limit in §12.1 is now about output size and
   response time, not about surviving the parse.
4. **Amendment 6b (brace escaping)** changes rule 6 as originally written. It is the right
   call for parseability, but it makes output for brace-containing text differ from the LV
   pipeline's, if that pipeline ever emitted such cells. Confirm before freezing.

---

## Appendix A — reference serializer (sketch)

Not production code; it pins the rules that prose leaves ambiguous.

```python
from datetime import date, datetime, time
from openpyxl.utils import get_column_letter

def _escape(s: str) -> str:
    # order matters — backslash first (amendment 6a)
    for a, b in (("\\", "\\\\"), ("|", "\\|"), ("{", "\\{"), ("}", "\\}")):
        s = s.replace(a, b)
    return s

def _fmt(v) -> str:
    if v is True:  return "TRUE"
    if v is False: return "FALSE"
    if isinstance(v, datetime): return v.isoformat(sep="T", timespec="seconds")
    if isinstance(v, date):     return v.isoformat()
    if isinstance(v, time):     return v.isoformat(timespec="seconds")
    if isinstance(v, float) and v.is_integer(): return str(int(v))
    if isinstance(v, (int, float)): return repr(v)          # shortest round-trip, no rounding
    return _escape(str(v).replace("\r\n", "\n").replace("\n", " / "))

def merge_refs(raw: bytes, sheet_part: str) -> list[str]:
    """Merged ranges without loading the sheet — see §12.2. ~1.9 MB peak where
    load_workbook(read_only=False) peaks at ~97 MB on the same file."""
    import zipfile
    from xml.etree import ElementTree as ET
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    refs = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z, z.open(sheet_part) as f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag == NS + "mergeCell":
                refs.append(el.get("ref"))
            if el.tag in (NS + "row", NS + "c", NS + "mergeCell"):
                el.clear()          # keeps the pass over sheetData O(1) in memory
    return refs

def serialize_cells(ws, refs: list[str], formulas: dict | None, mode: str) -> str:
    # ws comes from load_workbook(read_only=True); refs from merge_refs()
    from openpyxl.worksheet.cell_range import CellRange
    ranges = [CellRange(r) for r in refs]
    anchors = {r.coord.split(":")[0]: r.coord for r in ranges}
    covered = {rc for r in ranges for rc in r.cells}   # (row, col) tuples, not coords
    lines = []
    for row in ws.iter_rows():
        parts = []
        for cell in row:
            coord = cell.coordinate
            if (cell.row, cell.column) in covered and coord not in anchors:
                continue                                     # rule 7
            if cell.data_type == "e":
                if mode == "silent":
                    continue                                 # §4.3 addition
                parts.append(f"{_ref(cell, anchors)}:{cell.value}")
                continue
            v = cell.value
            if v is None or v == "":
                continue                                     # rule 3 / amendment 5a
            s = f"{_ref(cell, anchors)}:{_fmt(v)}"
            if mode == "formula" and formulas and (f := formulas.get(coord)):
                s += " {" + _escape(f) + "}"
            parts.append(s)
        if parts:
            lines.append(f"R{row[0].row}| " + " || ".join(parts))  # rule 1: the sheet's own number
    return "\n".join(lines)

def _ref(cell, anchors) -> str:
    col = get_column_letter(cell.column)
    span = anchors.get(cell.coordinate)
    return f"{col}[merged {span}]" if span else col
```
