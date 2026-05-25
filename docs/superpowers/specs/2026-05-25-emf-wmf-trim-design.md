# EMF/WMF Conversion Trim Design

## Overview
EMF/WMF images converted to PNG via Inkscape often contain large white margins. This design adds a deterministic trim step to crop away uniform white/near-white padding after conversion, while preserving the existing upload and embedding flow.

## Goals
- Remove excess white margins from EMF/WMF conversions.
- Keep conversion fidelity and existing HTML output behavior unchanged.
- Avoid silent failures; if trimming fails, continue with the untrimmed image and log the error.

## Non-Goals
- Changing how non-EMF/WMF images are handled.
- Replacing Cloudinary or HTML embedding behavior.
- Adding new user-facing API endpoints.

## Approaches Considered
1. **Inkscape conversion + trim (recommended)**  
   Convert with Inkscape, then trim using ImageMagick when available; otherwise use a Pillow-based trim. Best balance of reliability and dependency cost.
2. **LibreOffice conversion + trim**  
   Better compatibility for some EMF/WMF but heavier dependency and slower. Not ideal for container size or server startup.
3. **Inkscape-only export flags**  
   Minimal dependencies, but still often leaves margins due to WMF/EMF page bounds.

## Architecture
- **Conversion** remains in `render_metafile_with_inkscape`.
- **Trim step** runs immediately after conversion:
  - Prefer ImageMagick (`magick` or `convert`) with `-fuzz 1% -trim +repage`.
  - If ImageMagick is unavailable, use Pillow to crop away uniform white/near-white borders.
- **Upload & embed** unchanged: trimmed PNG is uploaded to Cloudinary and embedded as before.

## Data Flow
1. EMF/WMF blob extracted from DOCX.
2. Inkscape converts to PNG.
3. Trim step removes white margins.
4. Trimmed PNG uploaded to Cloudinary.
5. HTML `<img>` uses the Cloudinary URL.

## Error Handling
- If conversion fails, return placeholder as today.
- If trimming fails, log the error and continue with the untrimmed PNG.
- No silent drops or empty images.

## Dependencies & Configuration
- **Inkscape** remains required for EMF/WMF conversion.
- **ImageMagick** is optional; use if present.
- **Pillow** may be added as a lightweight fallback.
- No new environment variables required.

## Testing
- Unit test for the trim helper using a synthetic white‑padded PNG to confirm correct cropping.
- Confirm EMF/WMF conversion still succeeds and uploads after trimming.
