#!/usr/bin/env python3
"""Prepare the clean-default PSKA benchmark corpus.

The script copies the synthetic GBrain calibration markdown fixtures and adds
two PSKA-specific notes plus one small XLSX workbook for spreadsheet coverage.
It only recreates the benchmark subdirectory under the target notes root.
"""

from __future__ import annotations

import argparse
import html
import shutil
import zipfile
from pathlib import Path


BENCHMARK_DIR = "benchmark-2026-06-25"

MANUAL_NOTES = {
    "fastreact-digest-claim-a.md": """---
title: FastReAct digest execution A
type: note
---

# FastReAct digest execution A

PSKA depends on FastReAct for digest jobs and candidate write-back.

This note exists to test repeated semantic claim handling with a stable dedupe key.
""",
    "fastreact-digest-claim-b.md": """---
title: FastReAct digest execution B
type: note
---

# FastReAct digest execution B

FastReAct is the execution layer PSKA uses when digest jobs need agentic candidate write-back.

This is intentionally a paraphrase of the other FastReAct note.
""",
}

WORKBOOK_SHEETS = [
    (
        "Pipeline",
        [
            ["Company", "Lead", "Status", "ARR", "Next Step"],
            ["Acme Example", "Alice Example", "active", 1200000, "Prepare partner meeting brief"],
            ["Widget Co", "Charlie Example", "watch", 450000, "Review COO transition risk"],
            ["Fund A", "Bob Example", "fundraising", 0, "Follow up on seed allocation"],
        ],
    ),
    (
        "Actions",
        [
            ["Owner", "Action", "Due", "Source"],
            ["Alice Example", "Send Acme Example cohort metrics", "2026-07-01", "fundraise meeting"],
            ["Charlie Example", "Draft 90-day COO trial plan", "2026-07-03", "hiring note"],
            ["PSKA", "Compare Excel extraction against markdown notes", "2026-06-25", "benchmark"],
        ],
    ),
]


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def sheet_xml(rows: list[list[object]]) -> str:
    row_xml = []
    for row_index, row in enumerate(rows, start=1):
        cell_xml = []
        for col_index, value in enumerate(row, start=1):
            ref = f"{column_name(col_index)}{row_index}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cell_xml.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                escaped = html.escape(str(value), quote=False)
                cell_xml.append(f'<c r="{ref}" t="inlineStr"><is><t>{escaped}</t></is></c>')
        row_xml.append(f'<row r="{row_index}">{"".join(cell_xml)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(row_xml)}</sheetData>'
        "</worksheet>"
    )


def writestr(zf: zipfile.ZipFile, name: str, content: str) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 6, 25, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    zf.writestr(info, content.encode("utf-8"))


def write_xlsx(path: Path) -> None:
    sheets_xml = "\n".join(
        f'<sheet name="{html.escape(name, quote=True)}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, (name, _) in enumerate(WORKBOOK_SHEETS, start=1)
    )
    rels_xml = "\n".join(
        f'<Relationship Id="rId{idx}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        f'Target="worksheets/sheet{idx}.xml"/>'
        for idx, _ in enumerate(WORKBOOK_SHEETS, start=1)
    )
    overrides_xml = "\n".join(
        f'<Override PartName="/xl/worksheets/sheet{idx}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for idx, _ in enumerate(WORKBOOK_SHEETS, start=1)
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        writestr(
            zf,
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f"{overrides_xml}"
            "</Types>",
        )
        writestr(
            zf,
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        writestr(
            zf,
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheets_xml}</sheets>"
            "</workbook>",
        )
        writestr(
            zf,
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{rels_xml}"
            "</Relationships>",
        )
        for idx, (_, rows) in enumerate(WORKBOOK_SHEETS, start=1):
            writestr(zf, f"xl/worksheets/sheet{idx}.xml", sheet_xml(rows))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gbrain-fixtures-root",
        type=Path,
        default=Path("/tmp/codex-compare-gbrain/test/fixtures/calibration"),
    )
    parser.add_argument(
        "--notes-root",
        type=Path,
        default=Path("/Users/xudawei/PSKA_workspaces/default/notes"),
    )
    args = parser.parse_args()

    source_root = args.gbrain_fixtures_root.expanduser().resolve()
    notes_root = args.notes_root.expanduser().resolve()
    target = notes_root / BENCHMARK_DIR
    if not source_root.exists():
        raise SystemExit(f"GBrain fixtures root does not exist: {source_root}")

    if target.exists():
        shutil.rmtree(target)
    (target / "gbrain-calibration").mkdir(parents=True)

    copied = 0
    for fixture_dir in ("extract-takes-corpus", "holdout"):
        source_dir = source_root / fixture_dir
        destination_dir = target / "gbrain-calibration" / fixture_dir
        destination_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_dir.glob("*.md")):
            shutil.copy2(source, destination_dir / source.name)
            copied += 1

    manual_dir = target / "manual"
    manual_dir.mkdir(parents=True)
    for filename, content in MANUAL_NOTES.items():
        (manual_dir / filename).write_text(content, encoding="utf-8")

    write_xlsx(target / "spreadsheets" / "portfolio-pipeline.xlsx")

    print(f"prepared: {target}")
    print(f"gbrain_markdown_copied: {copied}")
    print(f"manual_markdown_added: {len(MANUAL_NOTES)}")
    print("xlsx_added: 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
