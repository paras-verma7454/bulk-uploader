# Embedded XLSX Chart Rendering Design

## Overview
Some DOCX files contain charts as embedded Excel workbooks (`word/embeddings/*.xlsx`) without any preview image. The parser currently renders only inline images and therefore drops these charts. This design adds a LibreOffice-based export path to render embedded XLSX workbooks to PNG, then uploads the result to Cloudinary and embeds it in the generated HTML.

## Goals
- Render embedded XLSX charts as images in HTML output.
- Preserve upload order and existing HTML structure.
- Use LibreOffice headless export for visual fidelity.

## Non-Goals
- Parsing chart XML to render charts in Python.
- Rendering multiple worksheets per workbook (default to first worksheet).
- Changing how non-embedded images are handled.

## Approach Options
1. **LibreOffice export (recommended)**  
   Export embedded XLSX to PNG using `libreoffice --headless --convert-to png`. Matches Word/LibreOffice rendering and avoids chart XML parsing.
2. **Chart XML + matplotlib**  
   No external dependency but requires heavy XML parsing and chart rendering logic.
3. **Preview image extraction**  
   Not available in these DOCX files.

## Architecture
Add an embedded-workbook rendering path in `render_xml_content` that:
1. Detects relationships pointing to `word/embeddings/*.xlsx`.
2. Exports the workbook to PNG via LibreOffice (first worksheet only).
3. Trims whitespace and uploads the PNG to Cloudinary.
4. Emits `<img>` markup into the HTML fragment.

Existing image handling (drawing/EMF/WMF) remains unchanged.

## Data Flow
1. Parse paragraph XML and locate embedded workbook relationships.
2. Resolve the relationship part, write XLSX bytes to a temp file.
3. Run LibreOffice headless to export PNG.
4. Trim whitespace (reuse existing trim pipeline).
5. Upload PNG to Cloudinary and embed `<img>`.

## Error Handling
- If LibreOffice is missing or export fails, log the error and return an `[image]` placeholder.
- If no embedded workbook relationship is present, skip.
- Upload errors propagate as runtime errors (consistent with other embedded image handling).

## Dependencies
- **LibreOffice** installed in the container/runtime.
- No new Python dependencies required for export (reuse existing trimming and upload helpers).

## Testing
- Manual validation with a DOCX that contains embedded XLSX charts.
- Ensure non-chart images still render as before.
