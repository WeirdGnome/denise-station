"""Denise Station - core logic (no GUI imports).

A clean-room Amiga game launcher backend: keeps a library of game files
(WHDLoad .lha archives, disk/CD images), stores user metadata in SQLite and
builds the command line that starts Amiberry (its AppImage).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sqlite3
import platform
import subprocess
import time
import urllib.request
from pathlib import Path

VERSION = "2.85"
APP_NAME = "denise-station"
OLD_APP_NAME = "igame-linux"   # name before 1.2; data is migrated on first start

XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
DATA_DIR = XDG_DATA / APP_NAME
CONFIG_DIR = XDG_CONFIG / APP_NAME
DB_PATH = DATA_DIR / "library.db"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
SCREENS_DIR = DATA_DIR / "screenshots"
LAUNCH_LOG = DATA_DIR / "last-launch.log"
SCRAPE_LOG = DATA_DIR / "last-scrape.log"
EXTRAS_DIR = DATA_DIR / "extras"            # <game id>/cover-back.jpg, manual.pdf ...
GAMES_DIR = Path.home() / "Games" / "Denise"   # the one fixed game folder (library, downloads, .amiberry)
PLACEHOLDER_COVER = Path(__file__).resolve().parent / "assets" / "placeholder_cover.png"


def placeholder_for(folder: str = "") -> Path:
    """The "NO COVER" picture for a game's category (Games, Demos, Magazines, Beta-Game, Beta-Demo);
    the generic one for everything else."""
    key = re.sub(r"[\s_]+", "-", (folder or "").strip().lower())
    p = PLACEHOLDER_COVER.parent / f"placeholder_{key}.png"
    return p if key in ("games", "demos", "magazines", "beta-game", "beta-demo") and p.exists() else PLACEHOLDER_COVER


def migrate_old_data() -> None:
    """Copy library, screenshots and settings from the pre-1.2 'igame-linux' folders, once."""
    import shutil
    for old, new in ((XDG_DATA / OLD_APP_NAME, DATA_DIR), (XDG_CONFIG / OLD_APP_NAME, CONFIG_DIR)):
        if old.is_dir() and not new.exists():
            try:
                shutil.copytree(old, new)
            except OSError:
                pass

DEFAULT_GENRES = [
    "Action", "Adventure", "Beat 'em up", "Board / Card", "Driving", "Educational",
    "Flight sim", "Platform", "Puzzle", "RPG", "Shoot 'em up", "Simulation",
    "Sports", "Strategy", "Other",
]

DEFAULT_SETTINGS = {
    "folders": [],
    "extensions": "lha adf adz ipf dms hdf iso cue chd",
    "recurse": True,
    # Leave empty to use the Amiberry AppImage Denise Station downloads. Set e.g. "amiberry" for a native install.
    "launch_command": "",
    # Start Amiberry in "full window" mode (the header bar switch); off leaves the choice to Amiberry's own config.
    "amiberry_fullwindow": False,
    "amiberry_theme_sync": True,          # write gui_theme (Default/Dark) into amiberry.conf before each launch
    "amiberry_shader": "",
    # Extra arguments passed to Amiberry for every game.
    "global_args": "",
    # Interface language: "auto" (follow the system) or a code such as "pl".
    "language": "auto",
    # Colour scheme: "auto" (follow the GNOME/desktop setting), "light" or "dark".
    "appearance": "auto",
    # Where Amiberry looks for WHDLoad .lha archives; empty = detect it.
    "lha_folder": "",
    # After a WHDLoad download: fetch info and cover/screenshot for the new games automatically.
    "scrape_after_download": True,
    # Also fetch the back cover and (when linked) the PDF manual.
    # Copyright reminder shown before WHDLoad downloads; the person can switch it off ("don't show again").
    "whdload_warning": True,
    # Address of the WHDLoad pack directory (https://...). Empty on purpose: Denise Station ships no download address,
    # you enter the source you use yourself.
    "whdload_source": "",
    # At start-up compare your archives with the last downloaded Retroplay list (offline) and point out newer versions.
    "check_updates": True,
    # Info scraper: Open Amiga Game Database (openretro.org), then Libretro thumbnails. No login needed.
    "scrape_screens": True,      # also download the box cover
    "scrape_overwrite": False,   # replace info you already have (including the title)
    # The first sub-folder below a game folder is the game's category (genre):
    # <game folder>/Platform/Turrican.lha -> "Platform". Games directly in the game folder keep their own genre.
    "folder_categories": True,
}


# ----------------------------------------------------------------- settings
def load_settings(path: Path = SETTINGS_PATH) -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        s.update(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        pass
    # settings of version 1.1 (ScreenScraper): keep the two still-relevant switches, drop the rest
    for old, new in (("ss_screens", "scrape_screens"), ("ss_overwrite", "scrape_overwrite")):
        if old in s:
            s[new] = s.pop(old)
    old = [f for f in (s.get("folders") or []) if os.path.expanduser(f) != str(GAMES_DIR)]
    if old:                                           # remembered, so library paths follow the files whenever they are moved
        s["legacy_folders"] = sorted(set(s.get("legacy_folders") or []) | set(old))
    s["folders"] = [str(GAMES_DIR)]                   # the game folder is fixed
    for stale in ("ss_devid", "ss_devpassword", "ss_user", "ss_password", "ss_systemid"):
        s.pop(stale, None)
    return s


def save_settings(s: dict, path: Path = SETTINGS_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: v for k, v in s.items() if not k.startswith("_")}, indent=2))
    try:
        path.chmod(0o600)  # settings may contain personal paths/commands
    except OSError:
        pass


# ------------------------------------------------------------------ helpers
def plain_title(filename: str) -> str:
    """File name without extension, '_' as a space (the display title of versions 1.4/1.5)."""
    stem = os.path.splitext(os.path.basename(filename))[0]
    return " ".join(stem.replace("_", " ").split()) or stem or filename


def guess_title(filename: str) -> str:
    """Name shown in the library: the file name without extension, '_' shown as a space, and
    words that are run together in CamelCase split apart. Version, chipset and number parts
    are left exactly as they are.

    'Turrican3_v1.6_Image_2633.lha'          -> 'Turrican3 v1.6 Image 2633'
    '20000LeaguesUnderTheSea_v1.1.lha'       -> '20000 Leagues Under The Sea v1.1'
    'Aladdin_v1.8_AGA_0420.lha'              -> 'Aladdin v1.8 AGA 0420'
    """
    s = plain_title(filename)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)           # LeaguesUnder -> Leagues Under
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)      # SWIVBlah -> SWIV Blah
    s = re.sub(r"(?<=[0-9])(?=[A-Z][a-z])", " ", s)      # 20000Leagues -> 20000 Leagues (not Turrican3)
    return " ".join(s.split()) or s


def search_title(filename: str) -> str:
    """Best guess of the real game name, used only for looking info up online.

    'Turrican3_v1.6_Image_2633.lha' -> 'Turrican 3';  'AlienBreed2_v1.2_0412.lha' -> 'Alien Breed 2'.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.split(r"_v\d|_\d{3,}|_Files\b|_Rev\d|_Image\b|_Disk\d", stem, maxsplit=1)[0]
    s = stem.replace("_", " ")
    s = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", s)       # camelCase / letter->digit
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)      # SWIVBlah -> SWIV Blah
    s = re.sub(r"(?<=[0-9])(?=[A-Z][a-z])", " ", s)      # 2Foo -> 2 Foo
    s = re.sub(r"\s+", " ", s).strip()
    return s or stem or filename


def norm_key(text: str) -> str:
    """Comparison form of a game name: lower case letters and digits only."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def lookup_title(title: str, path: str) -> str:
    """Title to search for: the cleaned-up file name while the library title is still the
    default (file name), otherwise whatever the user typed or a previous scrape stored."""
    return search_title(path) if title == guess_title(path) else title


def folder_category(path: str, roots, osp=os.path) -> str:
    """Category taken from the first sub-folder between a game folder and the file, or ''.

    <root>/Platform/Turrican.lha -> "Platform";  <root>/Action/Shmups/R-Type.lha -> "Action";
    <root>/Game.lha -> "".  With nested roots the innermost one counts.
    """
    path = osp.normpath(path)
    best = ""
    for root in roots:
        root = osp.normpath(osp.abspath(osp.expanduser(root)))
        if osp.normcase(path).startswith(osp.normcase(root).rstrip(osp.sep) + osp.sep) \
                and len(root) > len(best):
            best = root
    if not best:
        return ""
    parts = path[len(best):].strip(osp.sep).split(osp.sep)
    if len(parts) < 2:
        return ""
    return " ".join(parts[0].replace("_", " ").split())


def find_games(folders, extensions: str, recurse: bool = True) -> set[str]:
    exts = {"." + e.lower().lstrip(".") for e in extensions.split()}
    found: set[str] = set()
    for folder in folders:
        folder = os.path.normpath(os.path.expanduser(folder))
        if not os.path.isdir(folder):
            continue
        if recurse:
            for root, dirs, files in os.walk(folder, followlinks=True):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for f in files:
                    if os.path.splitext(f)[1].lower() in exts:
                        found.add(os.path.join(root, f))
        else:
            for f in os.listdir(folder):
                p = os.path.join(folder, f)
                if os.path.isfile(p) and os.path.splitext(f)[1].lower() in exts:
                    found.add(p)
    return found


def screenshot_for(game) -> Path | None:
    """Custom screenshot first, then an image next to the game file."""
    for ext in (".png", ".jpg", ".jpeg"):
        p = SCREENS_DIR / f"{game['id']}{ext}"
        if p.exists():
            return p
    base = Path(game["path"]).with_suffix("")
    for ext in (".png", ".jpg", ".jpeg", ".PNG", ".JPG"):
        p = base.with_suffix(ext)
        if p.exists():
            return p
    return None




# Demos, beta demos, disk magazines and your own Custom files are never looked up online.
NO_SCRAPE_FOLDERS = {"demos", "demo", "beta-demo", "beta-demos", "magazines", "magazine", "custom"}


def no_scrape_folder(folder: str) -> bool:
    return (folder or "").strip().lower() in NO_SCRAPE_FOLDERS


def no_scrape_game(folder: str, path: str = "", title: str = "") -> bool:
    return no_scrape_folder(folder)


def strip_demo_word(query: str, folder: str) -> str:
    """Games / Beta-Game(s): the word "demo" in the name is left out of the online search, so
    "ACSYS Demo" looks up "ACSYS" (the full game is listed without it)."""
    if (folder or "").strip().lower() in ("games", "game", "beta-game", "beta-games"):
        out = " ".join(re.sub(r"\bdemo\b", " ", query, flags=re.I).split())
        return out or query
    return query




def split_args(text: str) -> list[str]:
    return shlex.split(text or "")


def savestate_dirs(settings: dict) -> list[Path]:
    """Folders that may hold Amiberry savestates (.uss): the path from amiberry.conf (any key with 'savestate'
    in its name), then the usual default places."""
    home = Path.home()
    out: list[Path] = []
    try:
        for e in parse_conf(amiberry_conf_path(settings).read_text(encoding="utf-8")):
            if "k" in e and "savestate" in e["k"].lower() and e["v"]:
                out.append(Path(os.path.expanduser(e["v"])))
    except OSError:
        pass
    if appimage_portable(settings):
        pd = portable_dirs(settings)
        out += [b / n for b in (pd["data"] / "amiberry", pd["home"]) for n in ("savestates", "SaveStates", "Savestates")]
    out += [b / n for b in (XDG_DATA / "amiberry", home / "Amiberry", home / "amiberry")
            for n in ("SaveStates", "savestates", "Savestates")]
    seen, res = set(), []
    for d in out:
        if d not in seen and d.is_dir():
            seen.add(d)
            res.append(d)
    return res


_STATE_CACHE: dict = {}


def _state_picture(f: Path):
    """The picture Amiberry saved beside a savestate: <name>.png, <name>.uss.png, jpg too, any letter case, or
    another image whose name starts with the savestate's name."""
    exts = (".png", ".jpg", ".jpeg", ".bmp")
    stem = f.stem.lower()
    try:
        names = list(f.parent.iterdir())
    except OSError:
        return None
    exact = [q for q in names if q.suffix.lower() in exts and q.name.lower() in
             {stem + e for e in exts} | {f.name.lower() + e for e in exts}]
    if exact:
        return exact[0]
    loose = sorted(q for q in names if q.suffix.lower() in exts and q.stem.lower().startswith(stem))
    return loose[0] if loose else None


def find_savestates(settings: dict, game) -> list[dict]:
    """Savestates of a game, newest first: [{"path", "name", "mtime", "png"}]. A .uss belongs to the game when
    its name equals the archive's name or is that name plus a short slot suffix ("Game_1", "Game (2)")."""
    want = {norm_key(Path(game["path"]).stem)}
    if game["title"]:
        want.add(norm_key(game["title"]))
    want.discard("")
    found = []
    for d in savestate_dirs(settings):
        hit = _STATE_CACHE.get(d)
        if hit and time.monotonic() - hit[0] < 4:            # browsing the list must not rescan the folder every time
            files = hit[1]
        else:
            try:
                files = [f for f in d.rglob("*.uss") if len(f.relative_to(d).parts) <= 3]
            except OSError:
                continue
            _STATE_CACHE[d] = (time.monotonic(), files)
        for f in files:
            n = norm_key(f.stem)
            if any(n == w or (n.startswith(w) and len(n) - len(w) <= 3) for w in want):
                try:
                    st = f.stat()
                except OSError:
                    continue
                png = _state_picture(f)
                found.append({"path": str(f), "name": f.name, "mtime": st.st_mtime, "png": png})
    found.sort(key=lambda x: -x["mtime"])
    return found


def _state_files(path) -> list[Path]:
    """The savestate and the pictures that belong to it."""
    f = Path(path)
    pic = _state_picture(f)
    return [f] + ([pic] if pic else [])


def delete_savestate(path) -> None:
    _STATE_CACHE.clear()
    for q in _state_files(path):
        q.unlink()


def rename_savestate(path, new_name: str) -> Path:
    """Rename a savestate (and its picture). The new name keeps the .uss extension; existing files are never
    overwritten. Raises ValueError for a bad name and FileExistsError when the target exists."""
    new_name = new_name.strip()
    if new_name.lower().endswith(".uss"):
        new_name = new_name[:-4]
    if not new_name or "/" in new_name or "\\" in new_name or new_name.startswith("."):
        raise ValueError(new_name)
    _STATE_CACHE.clear()
    f = Path(path)
    target = f.with_name(new_name + ".uss")
    if target.exists():
        raise FileExistsError(str(target))
    pngs = {q: q.with_name(target.stem + q.name[len(f.stem):])      # same suffix style, new base name
            for q in _state_files(f) if q != f and q.name.lower().startswith(f.stem.lower())}
    f.rename(target)
    for old, new in pngs.items():
        if not new.exists():
            old.rename(new)
    return target


SHADERS = ("none", "tv", "pc", "lite", "1084")       # Amiberry's built-in shaders (amiberry.conf: shader=…)


# Default Amiga port devices (as in Amiberry's new.uae); changeable in Amiberry's own menu (F12).
# Skipped when the global or per-game arguments already set a joyport themselves.
DEFAULT_PORTS = (("joyport0", "mouse"), ("joyport1", "kbd9"))


def port_args(*arg_strings: str) -> list[str]:
    given = " ".join(a or "" for a in arg_strings)
    out = []
    for key, val in DEFAULT_PORTS:
        if key not in given:
            out += ["-s", f"{key}={val}"]
    return out


def base_command(settings: dict) -> list[str]:
    """Command that starts Amiberry itself (no game): the custom launcher, else our AppImage ([] when neither exists)."""
    launcher = (settings.get("launch_command") or "").strip()
    if launcher:
        return split_args(launcher)
    if appimage_path(settings):
        return appimage_base(settings)
    return []


def amiberry_missing(settings: dict) -> bool:
    """True when there is no way to start Amiberry yet (no custom launcher and no downloaded AppImage)."""
    return not base_command(settings)


def build_command(settings: dict, game, state: str = "") -> list[str]:
    base = base_command(settings)
    return (base
            + split_args(settings.get("global_args") or "")
            + split_args(game["args"] or "")
            + ["--autoload", game["path"]]
            # screen-mode overrides go AFTER --autoload so the WHDLoad
            # auto-config cannot overwrite them
            + (["-s", "gfx_fullscreen_amiga=fullwindow", "-s", "gfx_fullscreen_picasso=fullwindow"]
               if settings.get("amiberry_fullwindow") else [])
            + port_args(settings.get("global_args"), game["args"])
            + (["--statefile", state] if state else []))


def prelaunch(settings: dict, dark=None) -> None:
    """Amiberry settings Denise Station keeps in amiberry.conf before a game starts: F11 toggles full screen, Esc
    quits, the shader, and (when `dark` is known) the light/dark menu theme."""
    conf = amiberry_conf_path(settings)
    ensure_conf_key(conf, "default_fullscreen_toggle_key", "F11")
    ensure_conf_key(conf, "default_quit_key", "Escape")
    if settings.get("amiberry_shader"):
        ensure_conf_key(conf, "shader", settings["amiberry_shader"], force=True)
    if dark is not None and settings.get("amiberry_theme_sync", True):
        ensure_conf_key(conf, "gui_theme", "Dark.theme" if dark else "Default.theme", force=True)


def desktop_quote(arg: str) -> str:
    """One argument of an Exec= line (freedesktop rules: double quotes, and \\ \" ` $ escaped, % doubled)."""
    arg = arg.replace("%", "%%")
    if arg and not re.search(r'[\s"\'\\><~|&;$*?#()`]', arg):
        return arg
    return '"' + "".join("\\\\\\\\" if ch == "\\" else ("\\" + ch if ch in '"`$' else ch) for ch in arg) + '"'


def program_command() -> list[str]:
    """How to start Denise Station itself: the installed command, else this checkout."""
    found = shutil.which("denise-station")
    if found:
        return [found]
    import sys
    return [sys.executable, str(Path(__file__).resolve().parent / "denise_station.py")]


def applications_dir() -> Path:
    return XDG_DATA / "applications"


def desktop_title(game) -> str:
    return " ".join(str(game["title"] or Path(game["path"]).stem).split())


def desktop_file(game) -> Path:
    """Where the shortcut of this game lives (it may not exist)."""
    safe = re.sub(r'[\\/:*?"<>|]+', "_", desktop_title(game)).strip(". ") or "game"
    return applications_dir() / f"denise-{safe}.desktop"


def has_desktop(game) -> bool:
    return desktop_file(game).is_file()


def remove_desktop(game) -> bool:
    try:
        desktop_file(game).unlink()
        return True
    except OSError:
        return False


def export_desktop(game, dest_dir, icon: str = "") -> Path:
    """Write a .desktop launcher that starts this game through `denise-station --launch <file>`."""
    title = desktop_title(game)
    safe = re.sub(r'[\\/:*?"<>|]+', "_", title).strip(". ") or "game"
    exec_line = " ".join(desktop_quote(a) for a in program_command() + ["--launch", str(game["path"])])
    out = Path(dest_dir) / f"denise-{safe}.desktop"
    out.write_text("[Desktop Entry]\nType=Application\nVersion=1.0\nName=%s\nComment=%s\nExec=%s\nIcon=%s\n"
                   "Terminal=false\nCategories=Game;\nStartupNotify=false\n"
                   % (title, "Amiga - started with Denise Station", exec_line, icon or "denise-station"),
                   encoding="utf-8")
    out.chmod(0o755)
    return out


def launch_headless(path: str) -> int:
    """`denise-station --launch FILE`: start one game in Amiberry without opening the window (for shortcuts)."""
    import sys
    settings = load_settings()
    game = None
    try:
        game = Library(DB_PATH).get_by_path(path)
    except Exception:
        pass
    game = game or {"path": path, "args": "", "title": Path(path).stem}
    problem = ""
    if not os.path.exists(path):
        problem = "File not found: %s" % path
    elif amiberry_missing(settings):
        problem = "Amiberry is not installed. Start Denise Station and use Settings > Download / update Amiberry."
    elif whdboot_missing(settings):
        problem = "WHDLoad boot files are missing. Start Denise Station and use Settings > Update WHDLoad boot files."
    if problem:
        print(problem, file=sys.stderr)
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "Denise Station", problem], check=False)
        return 1
    prelaunch(settings)
    cmd = build_command(settings, game)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LAUNCH_LOG, "wb")
    log.write(("$ " + " ".join(shlex.quote(a) for a in cmd) + "\n\n").encode())
    log.flush()
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    os.execvp(cmd[0], cmd)










def lha_folder_candidates() -> list[Path]:
    """Likely places of Amiberry's archive folder, 'LHA' (its default spelling) before 'lha'."""
    home = Path.home()
    bases = [XDG_DATA / "amiberry", home / "Amiberry", home / "amiberry"]
    return [b / name for b in bases for name in ("LHA", "lha")]


def amiberry_lha_folder(settings: dict) -> Path:
    """The folder Amiberry reads WHDLoad archives from: the one set in Settings, else the first existing
    candidate, else the first one (it is created when the first archive is downloaded)."""
    custom = (settings.get("lha_folder") or "").strip()
    if custom:
        return Path(os.path.expanduser(custom))
    if appimage_portable(settings):
        h = portable_dirs(settings)["home"]
        return next((h / n for n in ("LHA", "lha") if (h / n).is_dir()), h / "lha")
    cands = lha_folder_candidates()
    return next((p for p in cands if p.is_dir()), cands[0])


# ---------------------------------------------------------- amiberry.conf
def amiberry_conf_path(settings: dict) -> Path:
    """Amiberry's own amiberry.conf: inside .amiberry for our portable AppImage, else ~/.config/amiberry
    (the first existing one of the usual places)."""
    home = Path.home()
    if appimage_portable(settings):
        return portable_dirs(settings)["config"] / "amiberry/amiberry.conf"
    cands = [home / ".config/amiberry/amiberry.conf", home / "amiberry/amiberry.conf"]
    return next((p for p in cands if p.is_file()), cands[0])


def parse_conf(text: str) -> list[dict]:
    """Lines of a key=value file: {"k", "v", "orig"} for settings, {"raw"} for comments and blank lines."""
    out = []
    for line in text.splitlines():
        st = line.strip()
        if st and not st.startswith(("#", ";")) and "=" in st:
            k, v = st.split("=", 1)
            out.append({"k": k.strip(), "v": v.strip(), "orig": line})
        else:
            out.append({"raw": line})
    return out


def serialize_conf(entries: list[dict]) -> str:
    lines = []
    for e in entries:
        if e.get("deleted"):
            continue
        if "raw" in e:
            lines.append(e["raw"])
        elif e.get("orig") and parse_conf(e["orig"])[0].get("k") == e["k"] and parse_conf(e["orig"])[0].get("v") == e["v"]:
            lines.append(e["orig"])                         # unchanged lines keep their exact spelling
        else:
            lines.append(f"{e['k']}={e['v']}")
    return "\n".join(lines).rstrip("\n") + "\n"


def save_conf(path: Path, text: str) -> None:
    """Write the file, keeping the previous one as <name>.bak."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, str(path) + ".bak")
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def ensure_conf_key(path: Path, key: str, value: str, force: bool = False) -> bool:
    """Add key=value to amiberry.conf only when the key is absent or empty (an existing value is the user's choice)."""
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        entries = parse_conf(text)
        cur = [e for e in entries if e.get("k") == key]
        if any(e["v"] == value for e in cur) or (not force and any(e["v"] for e in cur)):
            return False                                    # the user already chose a key
        if cur:
            cur[0]["v"] = value                             # key present but empty: fill it in
        else:
            entries.append({"k": key, "v": value})
        save_conf(path, serialize_conf(entries))
        return True
    except OSError:
        return False


# ------------------------------------------------- Amiberry AppImage
AMIBERRY_API = "https://api.github.com/repos/BlitterStudio/amiberry/releases/latest"
APPIMAGE_NAME = "Amiberry.AppImage"


def appimage_dir(settings: dict) -> Path:
    """Hidden folder next to the games: <first game folder>/.amiberry (a fixed one when no game folder is set)."""
    folders = settings.get("folders") or []
    base = Path(os.path.expanduser(folders[0])) if folders else XDG_DATA
    return base / ".amiberry"


def appimage_path(settings: dict) -> Path | None:
    p = appimage_dir(settings) / APPIMAGE_NAME
    return p if p.is_file() and os.access(p, os.X_OK) else None


def appimage_version(settings: dict) -> str:
    try:
        return (appimage_dir(settings) / "version.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def appimage_portable(settings: dict) -> bool:
    """True when our AppImage keeps all its files (ROMs, configs, savestates...) inside .amiberry."""
    return bool(appimage_path(settings)) and not (settings.get("launch_command") or "").strip() \
        and settings.get("amiberry_portable", True)


def portable_dirs(settings: dict) -> dict:
    """Where the portable Amiberry keeps things: home (roms, lha, floppies...), conf, config (amiberry.conf), data."""
    d = appimage_dir(settings)
    return {"home": d / "home", "conf": d / "home" / "conf", "config": d / "config", "data": d / "data"}


def portable_env(settings: dict) -> list[str]:
    """NAME=value pairs that make Amiberry use only the .amiberry folder (see the Amiberry directories wiki)."""
    if not appimage_portable(settings):
        return []
    pd = portable_dirs(settings)
    for p in pd.values():
        p.mkdir(parents=True, exist_ok=True)
    return [f"AMIBERRY_HOME_DIR={pd['home']}", f"AMIBERRY_CONFIG_DIR={pd['conf']}",
            f"XDG_CONFIG_HOME={pd['config']}", f"XDG_DATA_HOME={pd['data']}"]


def whdload_binary(settings: dict) -> Path:
    """Where Amiberry's WHDBooter expects the WHDLoad program (it is not shipped; Amiberry downloads it)."""
    return portable_dirs(settings)["data"] / "amiberry" / "WHDBoot" / "WHDLoad"


def whdboot_missing(settings: dict) -> bool:
    """True for our portable Amiberry when the WHDLoad program has not been downloaded yet."""
    return appimage_portable(settings) and not (settings.get("launch_command") or "").strip() \
        and not whdload_binary(settings).exists()


def download_whdboot(settings: dict, timeout: int = 300) -> tuple[bool, str]:
    """Run `Amiberry --download-whdboot` (fetches WHDLoad and the boot files). Returns (ok, output)."""
    try:
        r = subprocess.run(base_command(settings) + ["--download-whdboot"], capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    return whdload_binary(settings).exists(), out[-1500:]


def appimage_base(settings: dict) -> list[str]:
    """argv that starts the AppImage; without FUSE it unpacks itself on the fly."""
    p = str(appimage_path(settings))
    env = portable_env(settings)
    if not os.path.exists("/dev/fuse") or not (shutil.which("fusermount") or shutil.which("fusermount3")):
        env.append("APPIMAGE_EXTRACT_AND_RUN=1")
    return (["env"] + env + [p]) if env else [p]


def amiberry_data_dir(settings: dict) -> Path:
    """Folder to show for 'Open Amiberry data folder'."""
    if appimage_portable(settings):
        return portable_dirs(settings)["home"]
    return Path.home() / "Amiberry"


def pick_appimage_asset(release: dict, machine: str | None = None) -> tuple[str, str] | None:
    """(file name, download url) of the AppImage for this CPU in a GitHub release JSON."""
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        (machine or platform.machine()).lower())
    for a in release.get("assets", []):
        n = a.get("name", "")
        if n.endswith(".AppImage") and arch and arch in n:
            return n, a.get("browser_download_url", "")
    return None


def fetch_latest_release(timeout: int = 20) -> dict:
    req = urllib.request.Request(AMIBERRY_API, headers={"User-Agent": "denise-station",
                                                        "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def amiberry_update_info(settings: dict) -> tuple[str, bool]:
    """(latest tag, update available) - update is available when nothing is installed or the tag differs."""
    tag = fetch_latest_release().get("tag_name", "")
    return tag, bool(tag) and tag != appimage_version(settings)


def download_appimage(settings: dict, progress=None, cancel=None) -> str:
    """Download the newest AppImage into the hidden folder; returns the installed tag. Raises OSError/ValueError."""
    rel = fetch_latest_release()
    asset = pick_appimage_asset(rel)
    if not asset or not asset[1].startswith("https://"):
        raise ValueError("No AppImage for this CPU in the latest Amiberry release.")
    d = appimage_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (APPIMAGE_NAME + ".part")
    req = urllib.request.Request(asset[1], headers={"User-Agent": "denise-station"})
    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if cancel and cancel():
                f.close()
                tmp.unlink(missing_ok=True)
                raise OSError("cancelled")
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    os.chmod(tmp, 0o755)
    os.replace(tmp, d / APPIMAGE_NAME)
    (d / "version.txt").write_text(rel.get("tag_name", "") + "\n", encoding="utf-8")
    return rel.get("tag_name", "")


ROM_EXTS = (".rom", ".bin", ".a500", ".a600", ".a1000", ".a1200", ".a3000", ".a4000", ".cd32", ".cdtv", ".key")


def roms_dir(settings: dict) -> Path:
    """Where Amiberry looks for Kickstart ROMs."""
    base = portable_dirs(settings)["home"] if appimage_portable(settings) else Path.home() / "Amiberry"
    # Amiberry's own spelling is "ROMs"; Linux is case-sensitive, so reuse whichever one exists
    return next((base / n for n in ("ROMs", "roms", "Roms") if (base / n).is_dir()), base / "ROMs")


def import_roms(src: str | Path, dest: Path, max_depth: int = 3) -> tuple[int, int]:
    """Copy Kickstart ROM files (Amiga Forever's *.rom and rom.key included) from src, looking up to max_depth
    levels down, into dest. Existing files are kept. Returns (copied, already there)."""
    src, dest = Path(src), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied = same = 0
    base_depth = len(src.parts)
    for root, dirs, files in os.walk(src):
        if len(Path(root).parts) - base_depth >= max_depth:
            dirs[:] = []
        if Path(root).resolve() == dest.resolve():
            continue
        for n in files:
            if not n.lower().endswith(ROM_EXTS):
                continue
            f = Path(root) / n
            try:
                if not n.lower().endswith(".key") and not (65536 <= f.stat().st_size <= 4 * 1048576):
                    continue                                # not a Kickstart image
                t = dest / n
                if t.exists():
                    same += 1
                else:
                    shutil.copy2(f, t)
                    copied += 1
            except OSError:
                continue
    return copied, same


def migrate_library_paths(db, old_roots, new_root=None) -> int:
    """After the game folder moved: point library rows at the same file under the new folder (when it is there),
    so favourites, covers and play counts survive. Returns the number of rows moved."""
    new_root = str(new_root or GAMES_DIR)
    n = 0
    for old in old_roots:
        old = os.path.expanduser(old).rstrip("/")
        for row in db.execute("SELECT id, path FROM games WHERE path LIKE ?", (old.replace("%", "") + "/%",)).fetchall():
            new = new_root + row["path"][len(old):]
            if os.path.exists(new) and not db.execute("SELECT 1 FROM games WHERE path = ?", (new,)).fetchone():
                db.execute("UPDATE games SET path = ?, missing = 0 WHERE id = ?", (new, row["id"]))
                n += 1
    db.commit()
    return n


# ----------------------------------------------------------------- library
class Library:
    SCHEMA = """CREATE TABLE IF NOT EXISTS games(
        id INTEGER PRIMARY KEY,
        path TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        genre TEXT NOT NULL DEFAULT '',
        year TEXT NOT NULL DEFAULT '',
        developer TEXT NOT NULL DEFAULT '',
        publisher TEXT NOT NULL DEFAULT '',
        rating INTEGER NOT NULL DEFAULT 0,
        favorite INTEGER NOT NULL DEFAULT 0,
        hidden INTEGER NOT NULL DEFAULT 0,
        missing INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT '',
        args TEXT NOT NULL DEFAULT '',
        plays INTEGER NOT NULL DEFAULT 0,
        last_played REAL NOT NULL DEFAULT 0,
        added REAL NOT NULL DEFAULT 0,
        scraped REAL NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT '',
        folder TEXT NOT NULL DEFAULT '')"""
    # scraped: 0 = never, -1 = tried without a match, else timestamp.
    # source: which database the info came from ("oagd" or "libretro").
    # folder: category derived from the game's sub-folder at the last scan ('' = none).

    EDITABLE = {"title", "genre", "year", "developer", "publisher",
                "rating", "favorite", "hidden", "notes", "args", "scraped", "source", "score", "marks"}

    def __init__(self, path=DB_PATH):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._bundled_keys = None
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute(self.SCHEMA)
        self.db.execute("CREATE INDEX IF NOT EXISTS games_view ON games(missing, hidden, genre)")
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(games)")}
        if "scraped" not in cols:   # library created by version 1.0
            self.db.execute("ALTER TABLE games ADD COLUMN scraped REAL NOT NULL DEFAULT 0")
        if "source" not in cols:    # library created by version 1.0 / 1.1
            self.db.execute("ALTER TABLE games ADD COLUMN source TEXT NOT NULL DEFAULT ''")
        if "folder" not in cols:    # library created before 1.3
            self.db.execute("ALTER TABLE games ADD COLUMN folder TEXT NOT NULL DEFAULT ''")
        if "score" not in cols:     # magazine-style review score 0-10 and its parts (JSON), since 1.40
            self.db.execute("ALTER TABLE games ADD COLUMN score REAL NOT NULL DEFAULT 0")
        if "marks" not in cols:
            self.db.execute("ALTER TABLE games ADD COLUMN marks TEXT NOT NULL DEFAULT ''")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Before 1.4 titles were guessed ("Alien Breed 2"); now they are the file name. Switch the
            # titles that were never edited or scraped, and leave everything else alone.
            for r in self.db.execute("SELECT id, path, title FROM games").fetchall():
                if r["title"] == search_title(r["path"]) and r["title"] != guess_title(r["path"]):
                    self.db.execute("UPDATE games SET title=? WHERE id=?", (guess_title(r["path"]), r["id"]))
            self.db.execute("PRAGMA user_version=1")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 2:
            # 1.4/1.5 showed the raw file name ("20000LeaguesUnderTheSea v1.1"); 1.6 also splits
            # CamelCase. Titles still equal to the raw name are updated; edited ones are kept.
            for r in self.db.execute("SELECT id, path, title FROM games").fetchall():
                if r["title"] == plain_title(r["path"]) and r["title"] != guess_title(r["path"]):
                    self.db.execute("UPDATE games SET title=? WHERE id=?", (guess_title(r["path"]), r["id"]))
            self.db.execute("PRAGMA user_version=2")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 3:
            # 1.0-1.34 could store the page footer ("Page generated in 0.06 seconds ...") as the description.
            # Remove those and mark the games as not scraped, so "Scrape all missing info" fetches them again.
            self.db.execute("UPDATE games SET notes='', scraped=0 WHERE notes LIKE '%Page generated in%' "
                            "OR notes LIKE '%disclaim copyright%' OR notes LIKE '%All times are in UTC%'")
            self.db.execute("PRAGMA user_version=3")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 4:
            # From 1.45 descriptions are never taken from the online database: drop the ones it stored earlier.
            self.db.execute("UPDATE games SET notes='' WHERE scraped>0 AND source IN ('oagd','libretro')")
            self.db.execute("PRAGMA user_version=4")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 5:
            # From 1.52 descriptions come from OAGD again: remove the built-in ones and scrape those games anew.
            self.db.execute("CREATE TABLE IF NOT EXISTS bundled_done (path TEXT PRIMARY KEY)")
            done = [r[0] for r in self.db.execute("SELECT path FROM bundled_done")]
            for i in range(0, len(done), 500):
                ch = done[i:i + 500]
                self.db.execute("UPDATE games SET notes='', score=0, marks='', scraped=0 WHERE path IN (%s)"
                                % ",".join("?" * len(ch)), ch)
            self.db.execute("DROP TABLE IF EXISTS bundled_done")
            self.db.execute("PRAGMA user_version=5")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 6:
            # From 1.67 demos, beta demos and magazines are not looked up online: drop what an earlier version
            # fetched for them (info, rating, covers, extras). The genre/category is kept.
            for r in self.db.execute("SELECT id, folder FROM games WHERE scraped>0 AND source IN ('oagd','libretro')").fetchall():
                if not no_scrape_folder(r["folder"]):
                    continue
                self.db.execute("UPDATE games SET year='', developer='', publisher='', notes='', rating=0, score=0, "
                                "marks='', scraped=0, source='' WHERE id=?", (r["id"],))
                for ext in (".png", ".jpg", ".jpeg"):
                    try:
                        (SCREENS_DIR / f"{r['id']}{ext}").unlink()
                    except OSError:
                        pass
                shutil.rmtree(EXTRAS_DIR / str(r["id"]), ignore_errors=True)
            self.db.execute("PRAGMA user_version=6")
        if self.db.execute("PRAGMA user_version").fetchone()[0] < 7:
            # From 1.86 games cannot be hidden any more: bring back the ones hidden earlier.
            self.db.execute("UPDATE games SET hidden=0 WHERE hidden<>0")
            self.db.execute("PRAGMA user_version=7")
        self.db.commit()

    def unscraped_ids(self) -> list[int]:
        return [r[0] for r in self.db.execute(
            "SELECT id, folder, path, title FROM games WHERE missing=0 AND hidden=0 AND scraped=0 "
            "ORDER BY title COLLATE NOCASE") if not no_scrape_game(r[1], r[2], r[3])]

    def apply_scrape(self, gid: int, res, overwrite: bool = False, folder_genre: bool = False) -> None:
        """Store a scraper result. Existing user data is kept unless overwrite is set.
        With folder_genre, a genre that comes from the game's folder is never replaced."""
        g = self.get(gid)
        if g is None:
            return
        fields: dict = {}
        for key in ("genre", "year", "developer", "publisher"):
            if key == "genre" and folder_genre and g["folder"]:
                continue
            val = getattr(res, key, "")
            if val and (overwrite or not g[key]):
                fields[key] = val
        if getattr(res, "notes", "") and (overwrite or not g["notes"]):
            fields["notes"] = res.notes
        if getattr(res, "rating", 0) and (overwrite or not g["rating"]):
            fields["rating"] = res.rating
        if overwrite and getattr(res, "title", ""):
            fields["title"] = res.title
        fields["scraped"] = time.time()
        fields["source"] = getattr(res, "source", "") or ""
        self.update(gid, **fields)


    @staticmethod
    def _description_items(mapping: dict) -> dict:
        """{game name: text or {"text", "rating", "score", "marks"}} -> {norm_key: (text, rating, score, marks_json)}"""
        items = {}
        for k, v in mapping.items():
            if isinstance(v, str):
                v = {"text": v}
            if not isinstance(v, dict):
                continue
            text = str(v.get("text") or v.get("description") or "").strip()
            try:
                rating = max(0, min(5, int(v.get("rating") or 0)))
                score = max(0.0, min(10.0, float(v.get("score") or 0)))
            except (TypeError, ValueError):
                rating, score = 0, 0.0
            marks = {str(n): max(0.0, min(10.0, float(x))) for n, x in (v.get("marks") or {}).items()
                     if isinstance(x, (int, float))} if isinstance(v.get("marks"), dict) else {}
            if score and not rating:
                rating = max(1, min(5, round(score / 2)))       # the star rating follows the score
            if text or rating or score:
                key = norm_key(k) + ("*" if str(k).rstrip().endswith("*") else "")   # "Dizzy*" = every name starting so
                items[key] = (text, rating, score, json.dumps(marks, ensure_ascii=False) if marks else "")
        return items

    @staticmethod
    def _find_item(items: dict, path: str, title: str):
        """(matched key, item) for a game: its exact name first, then the longest "Name*" family."""
        names = (norm_key(search_title(path)), norm_key(title))
        for k in names:
            if k in items:
                return k, items[k]
        best = None
        for k in names:
            for fam in items:
                if fam.endswith("*") and k.startswith(fam[:-1]) and (best is None or len(fam) > len(best)):
                    best = fam
        return (best, items[best]) if best else (None, None)

    def _store_description(self, row, item, overwrite: bool, replace_notes: bool = False) -> int:
        text, rating, score, marks = item
        fields = {}
        if score and (overwrite or not row["score"]):
            fields["score"], fields["marks"] = score, marks
        if text and (overwrite or replace_notes or not row["notes"]):
            fields["notes"] = text
        if rating and (overwrite or not row["rating"]):
            fields["rating"] = rating
        if fields:
            self.update(row["id"], **fields)
        return 1 if fields else 0

    def import_descriptions(self, mapping: dict, overwrite: bool = False) -> tuple[int, int]:
        """Store descriptions and ratings in the matching games. mapping: {game name: text} or
        {game name: {"text": ..., "rating": 1-5, "score": 0-10, "marks": {...}}}. Names are matched on the
        name found in the file name, or on the title. Existing notes / ratings are kept unless overwrite is set.
        Returns (games changed, names from the file that matched no game)."""
        items = self._description_items(mapping)
        used, changed = set(), 0
        for r in self.db.execute("SELECT id, path, title, notes, rating, score FROM games").fetchall():
            k, item = self._find_item(items, r["path"], r["title"])
            if item:
                used.add(k)
                changed += self._store_description(r, item, overwrite)
        return changed, len(items) - len(used)


    def mark_unmatched(self, gid: int) -> None:
        self.update(gid, scraped=-1)

    def apply_scan(self, found: set[str], roots=(), use_folders: bool = False) -> tuple[int, int]:
        """Add new files, flag vanished ones as missing. Returns (added, missing).

        With use_folders, the genre of every found game that lies in a sub-folder of one of the
        roots is set to that folder's name (and follows the file when it is moved)."""
        now = time.time()
        existing = {r["path"] for r in self.db.execute("SELECT path FROM games")}
        new = sorted(found - existing)
        self.db.executemany("INSERT INTO games(path,title,added) VALUES(?,?,?)",
                            [(p, guess_title(p), now) for p in new])
        added = len(new)
        cats = [(folder_category(p, roots) if use_folders else "", p) for p in found]
        self.db.executemany("UPDATE games SET folder=?, genre=CASE WHEN ?<>'' THEN ? ELSE genre END, missing=0 "
                            "WHERE path=?", [(c, c, c, p) for c, p in cats])
        self.db.executemany("UPDATE games SET missing=? WHERE path=?",
                            [(0 if os.path.exists(p) else 1, p) for p in existing - found])
        self.db.commit()
        missing = self.db.execute("SELECT COUNT(*) FROM games WHERE missing=1").fetchone()[0]
        return added, missing

    def query(self, text: str = "", kind: str = "all", value=None):
        where = ["missing=0", "hidden=1" if kind == "hidden" else "hidden=0"]
        params: list = []
        if kind == "favorites":
            where.append("favorite=1")
        elif kind == "recent":
            where.append("last_played>0")
        elif kind == "genre" and value is not None:
            where.append("genre=?")
            params.append(value)
        if text:
            where.append("(title LIKE ? OR developer LIKE ? OR publisher LIKE ?)")
            params += [f"%{text}%"] * 3
        order = ("last_played DESC LIMIT 50" if kind == "recent"
                 else "favorite DESC, title COLLATE NOCASE" if kind in ("all", "genre")     # favourites first
                 else "title COLLATE NOCASE")
        return self.db.execute(
            f"SELECT * FROM games WHERE {' AND '.join(where)} ORDER BY {order}",
            params).fetchall()

    def get_by_path(self, path: str):
        return self.db.execute("SELECT * FROM games WHERE path=?", (str(path),)).fetchone()

    def get(self, gid: int):
        return self.db.execute("SELECT * FROM games WHERE id=?", (gid,)).fetchone()

    def update(self, gid: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in self.EDITABLE}
        if not fields:
            return
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE games SET {sets} WHERE id=?", [*fields.values(), gid])
        self.db.commit()

    def record_play(self, gid: int) -> None:
        self.db.execute("UPDATE games SET plays=plays+1, last_played=? WHERE id=?",
                        (time.time(), gid))
        self.db.commit()

    def clear_recent(self, gid: int | None = None) -> int:
        """Remove one game (or, without an id, every game) from "Recently played". The play counts stay."""
        if gid is None:
            cur = self.db.execute("UPDATE games SET last_played=0 WHERE last_played>0")
        else:
            cur = self.db.execute("UPDATE games SET last_played=0 WHERE id=? AND last_played>0", (gid,))
        self.db.commit()
        return cur.rowcount

    def genre_counts(self):
        return [(r[0], r[1]) for r in self.db.execute(
            "SELECT genre, COUNT(*) FROM games WHERE missing=0 AND hidden=0 "
            "GROUP BY genre ORDER BY genre='' , genre COLLATE NOCASE")]

    def known_genres(self) -> list[str]:
        used = [g for g, _ in self.genre_counts() if g]
        return sorted(set(DEFAULT_GENRES) | set(used), key=str.lower)
