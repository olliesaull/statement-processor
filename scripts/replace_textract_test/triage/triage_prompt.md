# Statement Triage Prompt

Paste this into a new Claude Code session.

---

I need to select a diverse test dataset of ~15-20 supplier statement PDFs from a pool of ~54 suppliers. Thumbnails of page 1 of up to 3 PDFs per supplier are in:

```
scripts/replace_textract_test/triage/thumbnails/
```

Each filename encodes the supplier name and page count, e.g. `003a_PeninsulaBevrages_14pages.png`. There are 141 thumbnails total.

## Step 1: Classify

Spawn Sonnet 4.6 subagents (`model: "sonnet"`) in parallel to process the thumbnails in batches of ~10. Each subagent should:

1. Read each thumbnail PNG using the Read tool (it handles images)
2. Classify each along these axes:
   - **Quality**: clean digital / poor scan / faded / skewed
   - **Handwriting**: none / some annotations / mostly handwritten
   - **Layout complexity**: simple single table / multi-section / complex nested tables / non-standard layout
   - **Visual density**: sparse / moderate / dense
   - **Date format** (if visible): DD/MM/YYYY, MM/DD/YYYY, DD.MM.YYYY, etc.
   - **Number format** (if visible): 1,234.56 vs 1.234,56 vs spaces, etc.
   - **Language** (if detectable): English, German, French, etc.
   - **Page count**: from the filename
3. Return the classifications as structured text

## Step 2: Select

Once all subagents complete, combine their classifications and recommend ~15-20 PDFs that maximise diversity. Requirements:

1. **At least 1-2 PDFs with 10+ pages** (for chunk boundary testing)
2. **At least 1-2 with handwriting or annotations**
3. **At least 1-2 with poor scan quality**
4. **At least 1-2 with non-English content** (if any exist)
5. **Mix of simple and complex layouts**
6. **Mix of date and number formats**

For each selected PDF, give a one-line reason why it was picked (e.g. "14 pages, German, comma decimals, dense layout").

## Step 3: Copy selected thumbnails

Copy the selected thumbnails into a new directory for easy review:

```
scripts/replace_textract_test/triage/selected/
```

Write a `selection.md` file inside the `selected/` directory containing:
- The numbered list of selected PDFs with one-line reasons
- The relevant entries from `scripts/replace_textract_test/triage/thumbnails/mapping.txt` so I can find the original PDF paths

Also print the contents of `selection.md` to the console.
