#!/usr/bin/env python3
"""Prepare files for Gemini: one JSON batch per N games plus a ready prompt.

  python3 tools/gemini_export.py [--size 40] [--out ~/Denise-Gemini] [--only-missing] [--all-folders]

Reads the Denise Station library read-only. Versions of one game (Turrican3_v1.5, _v1.6, AGA...) are merged
into one entry, so Gemini describes each game once. Paste prompt.txt first, then batch_001.json, batch_002.json ...
Save Gemini's answers as answer_001.json ... in the same folder.
"""
import argparse, json, os, sqlite3, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import denise_core as core

PROMPT = """Jesteś redaktorem opisów gier Amiga. Dla każdej gry z listy napisz opis po polsku,
2–3 zdania, neutralny, bez spoilerów: gatunek, o co chodzi w grze, co ją wyróżnia.
Zasady:
- Opisuj TYLKO gry, które znasz na pewno. Jeśli nie masz pewności co do gry, wpisz "opis": "" - nie zgaduj
  i nie wymyślaj.
- Zwróć uwagę na wersję i rok: różne części serii (np. Monkey Island 1 i 2) to osobne gry.
- Nie kopiuj tekstów z recenzji ani z opakowań, pisz własnymi słowami.
- Pole "id" przepisz bez zmian.
- Odpowiedz wyłącznie poprawnym JSON-em, bez komentarza i bez bloku markdown:
  [{"id": 12, "tytul": "...", "opis": "..."}]

Kolejne wiadomości będą zawierać listy gier (pola: id, tytul, rok, producent, wydawca, gatunek).
Odpowiadaj na każdą listę osobno, w tym samym formacie.
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(core.DB_PATH), help="library.db (default: the Denise Station library)")
    ap.add_argument("--out", default=str(Path.home() / "Denise-Gemini"))
    ap.add_argument("--size", type=int, default=40, help="games per batch (default 40)")
    ap.add_argument("--only-missing", action="store_true", help="skip games that already have a description")
    ap.add_argument("--all-folders", action="store_true",
                    help="also demos, magazines and beta demos (by default only games)")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        sys.exit("Library not found: %s" % a.db)
    db = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    db.row_factory = sqlite3.Row
    rows = db.execute("SELECT * FROM games WHERE missing=0 ORDER BY title COLLATE NOCASE").fetchall()

    games = {}
    for r in rows:
        if not a.all_folders and core.no_scrape_folder(r["folder"] or ""):
            continue
        name = core.search_title(os.path.basename(r["path"])) or r["title"]
        key = "".join(ch for ch in name.lower() if ch.isalnum())
        if not key:
            continue
        g = games.setdefault(key, {"tytul": name, "rok": "", "producent": "", "wydawca": "", "gatunek": "",
                                   "ma_opis": False})
        for fld, col in (("rok", "year"), ("producent", "developer"), ("wydawca", "publisher"), ("gatunek", "genre")):
            if not g[fld] and r[col]:
                g[fld] = r[col]
        if (r["notes"] or "").strip():
            g["ma_opis"] = True

    items = sorted(games.values(), key=lambda g: g["tytul"].lower())
    if a.only_missing:
        items = [g for g in items if not g["ma_opis"]]
    for i, g in enumerate(items, 1):
        g["id"] = i
        g.pop("ma_opis", None)
        for k in [k for k in ("rok", "producent", "wydawca", "gatunek") if not g[k]]:
            del g[k]                                   # leave out empty fields: shorter prompt, no confusion

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.txt").write_text(PROMPT, encoding="utf-8")
    (out / "index.json").write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    n = 0
    for i in range(0, len(items), a.size):
        n += 1
        chunk = [{"id": g["id"], **{k: v for k, v in g.items() if k != "id"}} for g in items[i:i + a.size]]
        (out / ("batch_%03d.json" % n)).write_text(json.dumps(chunk, ensure_ascii=False, indent=1), encoding="utf-8")
    print("%d games (%d library entries) -> %d batches of up to %d in %s" % (len(items), len(rows), n, a.size, out))
    print("1. Paste prompt.txt into Gemini.  2. Paste batch_001.json, then batch_002.json ...")
    print("3. Save each answer as answer_001.json, answer_002.json ... in the same folder.")


if __name__ == "__main__":
    main()
