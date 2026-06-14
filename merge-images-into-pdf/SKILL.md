---
name: merge-images-into-pdf
description: Convert photos of document pages into PDFs. Use when Codex needs to rotate page images upright, combine image pages into a PDF, deskew or perspective-correct photographed pages so they look more like scans, create both a rotation-only PDF and a scanner-style PDF, or process JPG/PNG/TIFF/HEIC images of certificates, IDs, forms, receipts, letters, book pages, or similar documents.
---

# Merge Images Into PDF

## Workflow

Use the bundled CLI for page-photo cleanup:

```bash
python3 /Users/han/.codex/skills/merge-images-into-pdf/scripts/create_document_pdfs.py \
  --output-dir /path/to/output \
  --basename document \
  --rotation 90 \
  /path/to/page-1.jpg /path/to/page-2.jpg
```

The script writes:

- `<basename>_rotated.pdf`: rotated pages combined without perspective correction
- `<basename>_scanned.pdf`: rotated pages with document rectangle detection, perspective correction, and mild scan-style enhancement
- `rotated-images/`: intermediate upright page JPGs
- `scanned-images/`: intermediate flattened page JPGs

## Procedure

1. List the input images. The script naturally sorts by filename; pass `--no-sort` when the caller's explicit path order must be preserved.
2. Inspect a sample image before running the script and choose the clockwise rotation needed to make text upright: `0`, `90`, `180`, or `270`.
3. Run `scripts/create_document_pdfs.py` with `--rotation <degrees>`, `--output-dir`, and `--basename`.
4. Inspect `scanned-images/` or the scanner PDF. If automatic perspective correction bends or crops a page poorly, rerun with `--corners` for that page.
5. Verify the final PDFs have the expected page count and readable orientation.

## Manual Corners

Use `--corners` only for pages where automatic rectangle detection is wrong. Coordinates are in pixels on the rotated upright image, with `y` measured from the top edge:

```bash
python3 /Users/han/.codex/skills/merge-images-into-pdf/scripts/create_document_pdfs.py \
  --output-dir /path/to/output \
  --basename document \
  --rotation 90 \
  --corners '3:90,45,1135,50,60,1575,1115,1570' \
  /path/to/page-1.jpg /path/to/page-2.jpg /path/to/page-3.jpg
```

Corner order is:

```text
page:topLeftX,topLeftY,topRightX,topRightY,bottomLeftX,bottomLeftY,bottomRightX,bottomRightY
```

Pass multiple `--corners` flags for multiple pages. Keep the manual correction conservative: preserve readability over making every page edge perfectly rectangular.

## Notes

- The CLI requires macOS with `/usr/bin/swift` because it uses Apple Vision and Core Image.
- Leave original images unchanged; write outputs to a new directory.
- If pages have mixed orientations, pass `--rotations 90,90,270,0` instead of `--rotation`.
- Scanner-style correction handles perspective skew; it cannot fully flatten curled, folded, or wavy paper.
