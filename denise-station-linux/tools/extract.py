#!/usr/bin/env python3
"""Extract _("...") strings from denise_station.py into locale/denise.pot and check .po files.

Usage: tools/extract.py          write locale/denise.pot and report .po problems
"""
import ast, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import denise_i18n as i18n


def extract(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            found.setdefault(node.args[0].value, node.lineno)
    return found


def esc(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def placeholders(s):
    return sorted(re.findall(r"\{[a-z_]+\}", s))


def main():
    msgs = {}
    for src in ("denise_station.py", "whdload_dialog.py", "setup_wizard.py"):
        for m, line in extract(ROOT / src).items():
            msgs.setdefault(m, (src, line))
    lines = ['msgid ""', 'msgstr ""', '"Content-Type: text/plain; charset=UTF-8\\n"', ""]
    for m in msgs:
        lines += [f"#: {msgs[m][0]}:{msgs[m][1]}", f'msgid "{esc(m)}"', 'msgstr ""', ""]
    (ROOT / "locale" / "denise.pot").write_text("\n".join(lines), encoding="utf-8")
    print(f"{len(msgs)} translatable strings -> locale/denise.pot")
    bad = 0
    for po in sorted((ROOT / "locale").glob("*.po")):
        cat = i18n.parse_po(po.read_text(encoding="utf-8"))
        missing = [m for m in msgs if m not in cat]
        stale = [m for m in cat if m not in msgs]
        wrong = [m for m in msgs if m in cat and placeholders(m) != placeholders(cat[m])]
        print(f"{po.name}: {len(cat)} entries, {len(missing)} missing, {len(stale)} unused, "
              f"{len(wrong)} placeholder mismatches")
        for label, items in (("missing", missing), ("unused", stale), ("placeholders", wrong)):
            for m in items:
                print(f"   {label}: {m!r}")
        bad += len(missing) + len(wrong)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
