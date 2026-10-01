"""One-off helper: convert the official UK SIC 2007 condensed list CSV into
a Python dict module for offline explain_sic_code(). Run once; output is
committed so users never need the CSV.

Usage:  python3 make_sic_data.py <sic.csv> <output.py>
"""
import csv
import py_compile
import sys

src_csv, out_path = sys.argv[1], sys.argv[2]

rows = []
with open(src_csv, newline="", encoding="utf-8") as fh:
    reader = csv.reader(fh)
    next(reader)  # header: "SIC Code,Description"
    for row in reader:
        if len(row) >= 2 and row[0].strip():
            rows.append((row[0].strip().zfill(5), row[1].strip()))

assert len(rows) > 700, f"expected ~731 codes, got {len(rows)}"
for must in ("43220", "62012", "01230"):
    assert any(code == must for code, _ in rows), f"{must} missing"

lines = [
    '"""Official UK SIC 2007 condensed code list (offline copy).',
    "",
    "Source: the Companies House condensed list of SIC codes, downloaded",
    "from the UK Government publishing service (SIC07_CH_condensed_list_en.csv)",
    "and embedded here so the explain_sic_code tool needs no network and",
    "no API key.",
    "",
    "Contains public sector information licensed under the Open Government",
    'Licence v3.0 (http://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).',
    "The project's MIT licence covers its code; this data remains under OGL v3.",
    '"""',
    "",
    "# code -> official plain-English description",
    "SIC_DESCRIPTIONS = {",
]
for code, desc in rows:
    escaped = desc.replace("\\", "\\\\").replace('"', '\\"')
    lines.append(f'    "{code}": "{escaped}",')
lines.append("}")
lines += [
    "",
    "",
    'def describe_sic_code(code: str):',
    '    """Return the official description for a SIC code, or None.',
    "",
    "    Accepts codes with or without leading zeros ('4322' is treated as",
    "    '04322'). Plain English: turn the 5-digit 'nature of business'",
    "    code on a company record into words.",
    '    """',
    '    cleaned = "".join(ch for ch in str(code or "") if ch.isdigit())',
    '    if not cleaned:',
    "        return None",
    "    if len(cleaned) < 5:",
    "        cleaned = cleaned.zfill(5)",
    "    return SIC_DESCRIPTIONS.get(cleaned)",
    "",
]

with open(out_path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(lines))

py_compile.compile(out_path, doraise=True)
print(f"written {out_path}: {len(rows)} codes, compiles OK")