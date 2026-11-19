"""
Mason Investment Bot - Uploaded File Reader
============================================
Reads files Nico drops in UploadedFiles/ (via the web UI's upload button) so
Mason can answer questions about them directly in chat. v1 scope: plain text
documents only — CSV, JSON, Excel (.xlsx/.xlsm), Word (.docx), and .txt/.md/.log.
No PDFs, no images, no binary formats.

Design notes:
- Filenames are fuzzy-matched (case-insensitive, extension optional) because
  Nico types names from memory, not copy-paste.
- Every reader caps its output (MAX_ROWS / MAX_CHARS) so one big file can't
  blow the model's context window.
- Excel/Word readers import their libraries lazily and fail with an exact
  `pip install X` instruction rather than crashing the tool call, since those
  two deps aren't guaranteed to be on Nico's machine.
"""

import csv
import json
from datetime import datetime
from pathlib import Path

BASE        = Path(__file__).parent
UPLOADS_DIR = BASE / "UploadedFiles"

TEXT_EXTS  = {".txt", ".md", ".log"}
CSV_EXTS   = {".csv", ".tsv"}
EXCEL_EXTS = {".xlsx", ".xlsm", ".xls"}
WORD_EXTS  = {".docx"}
JSON_EXTS  = {".json"}

MAX_ROWS  = 200      # cap table rows per file/sheet
MAX_CHARS = 20000    # cap raw text length


def list_uploaded_files() -> list:
    """List every file currently in UploadedFiles/, newest first."""
    if not UPLOADS_DIR.exists():
        return []
    out = []
    for p in sorted(UPLOADS_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
        if p.is_file():
            out.append({
                "name":     p.name,
                "size_kb":  round(p.stat().st_size / 1024, 1),
                "modified": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
            })
    return out


def _resolve_file(name_hint):
    """Fuzzy-match a user-given filename against what's actually in UploadedFiles/.
    Tries exact name, exact stem (no extension), then substring; returns None
    on no match or leaves ambiguity to the caller by picking the newest file."""
    if not UPLOADS_DIR.exists():
        return None
    candidates = [p for p in UPLOADS_DIR.iterdir() if p.is_file()]
    hint = name_hint.strip().strip('"').strip("'")
    hint_lower = hint.lower()

    for p in candidates:
        if p.name.lower() == hint_lower:
            return p
    for p in candidates:
        if p.stem.lower() == hint_lower:
            return p
    matches = [p for p in candidates if hint_lower in p.name.lower() or p.stem.lower() in hint_lower]
    if matches:
        return max(matches, key=lambda f: f.stat().st_mtime)
    return None


def _read_csv(path: Path) -> str:
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(f, dialect))

    if not rows:
        return "(empty file)"

    header, raw_body = rows[0], rows[1:]

    # Brokerage exports (Schwab etc.) tack a disclaimer paragraph and a
    # "Date downloaded" line onto the bottom of the CSV, outside the real column
    # structure. Those trailer lines are quoted as a single field (or blank), so
    # their CELL COUNT collapses to ~1 regardless of how sparse a legit data row's
    # VALUES are (e.g. a cash position with mostly blank columns still has the
    # full column count). Stop at the first row whose cell count doesn't roughly
    # match the header rather than counting non-empty values.
    min_cells = max(1, len(header) // 2)
    body = []
    for r in raw_body:
        if len(r) < min_cells:
            break
        body.append(r)

    truncated = len(body) > MAX_ROWS
    shown = body[:MAX_ROWS]

    lines = [f"Columns: {', '.join(header)}", f"Rows: {len(body)}", ""]
    lines.append(" | ".join(header))
    lines.append(" | ".join(["---"] * len(header)))
    for r in shown:
        lines.append(" | ".join(r))
    if truncated:
        lines.append(f"\n... truncated, showing first {MAX_ROWS} of {len(body)} rows.")
    return "\n".join(lines)


def _read_json(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    text = json.dumps(data, indent=2)
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n... truncated ({len(text)} chars total)."
    return text


def _read_text(path: Path) -> str:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n... truncated ({len(text)} chars total)."
    return text


def _read_excel(path: Path) -> str:
    try:
        import openpyxl
    except ImportError:
        return ("Can't read Excel files yet — openpyxl isn't installed.\n"
                "Run: pip install openpyxl")

    if path.suffix.lower() == ".xls":
        return ("'.xls' (old binary Excel format) isn't supported. "
                "Re-save it as .xlsx in Excel and re-upload.")

    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        out.append(f"=== Sheet: {sheet_name} ===")
        rows_written = 0
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            cells = [str(c) if c is not None else "" for c in row]
            if i == 0:
                out.append(" | ".join(cells))
                out.append(" | ".join(["---"] * len(cells)))
                continue
            if rows_written >= MAX_ROWS:
                out.append(f"... truncated at {MAX_ROWS} rows for this sheet.")
                break
            out.append(" | ".join(cells))
            rows_written += 1
        out.append("")
    wb.close()
    return "\n".join(out)


def _read_word(path: Path) -> str:
    try:
        import docx
    except ImportError:
        return ("Can't read Word files yet — python-docx isn't installed.\n"
                "Run: pip install python-docx")

    doc = docx.Document(str(path))
    out = []
    for para in doc.paragraphs:
        if para.text.strip():
            out.append(para.text)
    for i, table in enumerate(doc.tables):
        out.append(f"\n=== Table {i + 1} ===")
        for row in table.rows:
            out.append(" | ".join(cell.text.strip() for cell in row.cells))

    text = "\n".join(out)
    if len(text) > MAX_CHARS:
        return text[:MAX_CHARS] + f"\n... truncated ({len(text)} chars total)."
    return text or "(no readable text found in document)"


def read_uploaded_file(name_hint: str = "") -> str:
    """Main entry point. Empty/missing hint -> list what's available.
    Otherwise fuzzy-match the hint to a file in UploadedFiles/ and return its
    contents as formatted text, dispatched by extension."""
    if not name_hint or not name_hint.strip():
        files = list_uploaded_files()
        if not files:
            return "No files found in UploadedFiles/."
        lines = ["Files in UploadedFiles/:"]
        for f in files:
            lines.append(f"  {f['name']}  ({f['size_kb']} KB, modified {f['modified']})")
        return "\n".join(lines)

    path = _resolve_file(name_hint)
    if not path:
        available = [f["name"] for f in list_uploaded_files()]
        return (f"Couldn't find a file matching '{name_hint}' in UploadedFiles/. "
                f"Available files: {', '.join(available) if available else '(none)'}")

    ext = path.suffix.lower()
    try:
        if ext in CSV_EXTS:
            body = _read_csv(path)
        elif ext in JSON_EXTS:
            body = _read_json(path)
        elif ext in EXCEL_EXTS:
            body = _read_excel(path)
        elif ext in WORD_EXTS:
            body = _read_word(path)
        elif ext in TEXT_EXTS:
            body = _read_text(path)
        else:
            return (f"'{path.name}' is a {ext or 'unknown'} file — Mason can currently only "
                     f"read CSV, JSON, Excel (.xlsx/.xlsm), Word (.docx), and plain text files.")
    except Exception as e:
        return f"Error reading '{path.name}': {e}"

    return f"=== {path.name} ===\n\n{body}"
