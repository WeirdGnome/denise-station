#!/usr/bin/env python3
"""Load Gemini's answers (answer_*.json) into the Denise Station library as descriptions.

  python3 tools/gemini_import.py [--dir ~/Denise-Gemini] [--apply] [--overwrite]

Without --apply nothing is written (dry run: only counts and examples). With --apply the library is backed up
(library.db.bak-gemini) first. Only games with an EMPTY description are filled (OAGD descriptions win);
--overwrite replaces existing ones too. Empty "opis" answers are skipped. Every version of a game gets the text.
Optional answer fields: "ocena" (1-100, becomes the 0-10 score under the cover and sets the stars) and
"kategorie" ({"Grafika": 90, ...}, 1-100 each, shown as the parts of the score). A score is only set when the
game has none yet (or with --overwrite).
"""
import argparse, json, os, shutil, sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import denise_core as core


def key_of(name):
    return "".join(ch for ch in name.lower() if ch.isalnum())


def read_objects(raw):
    """Collect every {...} object in the text, however broken the JSON around them is (missing comma or closing
    bracket, code block, remarks before/after). Objects that do not parse are skipped."""
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        i = raw.find("{", i)
        if i < 0:
            return out
        try:
            obj, end = dec.raw_decode(raw, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict) and "id" in obj:
            out.append(obj)
        i = end


def percent(v):
    """1-100 (also '87%', '87,5') -> 0-10 with one decimal, or 0.0 when unusable."""
    try:
        x = float(str(v).replace("%", "").replace(",", ".").strip())
    except ValueError:
        return 0.0
    return round(max(0.0, min(100.0, x)) / 10.0, 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(core.DB_PATH))
    ap.add_argument("--dir", default=str(Path.home() / "Denise-Gemini"))
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args()
    d = Path(a.dir)

    index = {g["id"]: g for g in json.loads((d / "index.json").read_text(encoding="utf-8"))}
    texts, bad = {}, []
    for f in sorted(d.glob("answer_*.json")):
        data = read_objects(f.read_text(encoding="utf-8"))
        if not data:
            bad.append("%s: no entries found" % f.name)
            continue
        for e in data:
            g = index.get(e.get("id"))
            txt = (e.get("opis") or "").strip()
            if g is None:
                bad.append("%s: unknown id %r" % (f.name, e.get("id")))
            elif txt:
                kat = e.get("kategorie") if isinstance(e.get("kategorie"), dict) else {}
                marks = {str(n): percent(x) for n, x in kat.items() if percent(x)}
                texts[key_of(g["tytul"])] = (txt, percent(e.get("ocena")), marks)
    print("answers read: %d descriptions (%d empty or unusable)" % (len(texts), len(bad)))
    for b in bad[:10]:
        print("  !", b)

    db = sqlite3.connect(a.db)
    db.row_factory = sqlite3.Row
    todo, kept = [], 0
    scored = 0
    for r in db.execute("SELECT id, path, notes, score FROM games"):
        name = core.search_title(os.path.basename(r["path"]))
        item = texts.get(key_of(name))
        if not item:
            continue
        txt, score, marks = item
        if (r["notes"] or "").strip() and not a.overwrite:
            kept += 1
            continue
        fields = {"notes": txt}
        if score and (a.overwrite or not r["score"]):
            fields["score"] = score
            fields["marks"] = json.dumps(marks, ensure_ascii=False) if marks else ""
            fields["rating"] = max(1, min(5, round(score / 2)))
            scored += 1
        todo.append((fields, r["id"], name))
    print("to fill: %d library entries (%d with a score); kept existing description in %d" % (len(todo), scored, kept))
    for _t, _i, n in todo[:3]:
        print("  e.g.", n)
    if not a.apply:
        print("Dry run. Add --apply to write.")
        return
    if str(a.db) != ":memory:":
        shutil.copy2(a.db, str(a.db) + ".bak-gemini")
    for fields, gid, _n in todo:
        db.execute("UPDATE games SET %s WHERE id=?" % ", ".join("%s=?" % k for k in fields),
                   list(fields.values()) + [gid])
    db.commit()
    print("Written. Backup: %s.bak-gemini" % a.db)


if __name__ == "__main__":
    main()
