# Denise Station

**A cover-grid launcher for Amiga games on Linux.** It keeps a browsable library of your WHDLoad archives and disk
images and starts them in [Amiberry](https://github.com/BlitterStudio/amiberry), which it downloads, updates and
configures for you.

Denise Station is a clean-room program inspired by the idea of the iGame launcher. It is written from scratch in
Python and GTK 3; no code, graphics or data from the original AmigaOS program are used.

<!-- Add screenshots here: docs/screenshot-grid.png, docs/screenshot-detail.png -->

## Contents

- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [First run](#first-run)
- [Using Denise Station](#using-denise-station)
- [Categories from folders](#categories-from-folders)
- [Amiberry integration](#amiberry-integration)
- [WHDLoad download tool](#whdload-download-tool)
- [Scraping game info](#scraping-game-info)
- [Writing descriptions with an AI assistant](#writing-descriptions-with-an-ai-assistant)
- [Settings and data](#settings-and-data)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Translations](#translations)
- [Development](#development)
- [Legal](#legal)
- [License](#license)

## Features

- **Cover grid or list.** Covers load lazily, so libraries with thousands of games stay fast.
- **Library scanner** for WHDLoad `.lha` archives and disk or CD images (`adf adz ipf dms hdf iso cue chd`).
- **Readable titles** derived from file names (`Turrican3_v1.6_Image_2633.lha` becomes "Turrican3 v1.6 Image 2633"),
  editable at any time.
- **Metadata:** genre, year, developer, publisher, 0-5 star rating, magazine-style score with sub-scores, notes and
  per-game extra Amiberry arguments.
- **Categories from folders:** sub-folders of your game folder become genres in the sidebar.
- **Favourites, recently played,** play counter, last-played date, random game, search.
- **Savestates:** per-game list of Amiberry savestates (date and time), double-click to start from a state,
  right-click to rename or delete.
- **Info scraping** from the Open Amiga Game Database with Libretro thumbnails as a fallback: year, developer,
  publisher, genre, rating, description, box cover, back cover and manual (no account or API key needed).
- **WHDLoad download tool** with CRC32 verification, filters (system, extras, language), update detection and curated
  lists.
- **Self-managed Amiberry:** downloads the latest AppImage, keeps it in a hidden portable folder and checks for newer
  releases.
- **First-run wizard** that sets everything up: Amiberry, WHDLoad boot files, Kickstart ROMs, path scan, repository.
- **Header-bar controls** for Amiberry full window and shader cycling (`none`, `tv`, `pc`, `lite`, `1084`).
- **Light and dark theme** that follows the desktop. Amiberry's menu follows it too (see
  [Amiberry integration](#amiberry-integration)).
- **Languages:** English, Polski, Deutsch, Francais, Espanol (follows the system language or can be set manually).
- Games that disappear (for example an unmounted drive) are flagged as missing but keep their metadata.

## Requirements

- Linux with a GTK 3 desktop (developed and tested on Linux Mint XFCE)
- Python 3.8 or newer and PyGObject (GTK 3 bindings)
- glibc 2.35 or newer for the Amiberry AppImage (FUSE is optional; the AppImage unpacks itself if it is missing)
- Kickstart ROMs that you own (Denise Station does not include or download them)

Nothing else is needed: Denise Station uses only the Python standard library besides PyGObject.

## Installation

Install the GTK 3 bindings with your package manager. On Debian, Ubuntu and Linux Mint:

```sh
sudo apt install python3-gi gir1.2-gtk-3.0
```

Then install the program for the current user (no root needed):

```sh
./install.sh
```

`install.sh` copies the program to `~/.local/share/denise-station-app`, creates the `denise-station` command in
`~/.local/bin`, installs the icon and adds a menu entry. If the GTK bindings are missing it detects your
distribution and prints the matching install command.

Start **Denise Station** from the application menu or run `denise-station`.
`denise-station --version` prints the version and the folders in use.

You can also run it straight from a checkout without installing:

```sh
python3 denise_station.py
```

## First run

The wizard opens on the first start and can be repeated from the menu (**Setup wizard...**). It walks through:

1. **Amiberry:** downloads the latest AppImage and the WHDLoad boot files.
2. **Kickstart ROMs:** copies them from a folder you choose, for example Amiga Forever's `Shared/rom`
   (`rom.key` is copied as well). You can skip this and add ROMs later.
3. **Path scan (required):** Amiberry cannot be told about new ROMs from the command line, so it must scan once
   itself. Press **Open Amiberry**, then on the **Play** page, in the **First-run setup** section, press
   **Rescan ROMs**, check that it says "Kickstart ROMs detected." and close Amiberry. The wizard continues when
   Amiberry is closed. Skipping this step makes games start with a substitute ROM and close at once.
4. **Repository:** the https address of the Retroplay WHDLoad repository you want to download from (optional; it can
   be entered later). Denise Station ships without any address.

Your games live in **`~/Games/Denise`**. Put archives and disk images there (or download them with the built-in tool)
and press **F5** to scan.

## Using Denise Station

| Action | How |
|---|---|
| Start a game | double-click it or press **Play** |
| Search | **Ctrl+F** (the field folds away when empty; **Esc** clears it, then hides it) |
| Random game | **Ctrl+R** |
| Rescan folders | **F5** |
| Denise Station full screen | **F11** or **Alt+Enter** |
| Amiberry full window | header-bar button |
| Cycle Amiberry shader | header-bar button |
| Edit title, genre, notes, extra arguments | **Edit...** in the detail panel |
| Long description | **More...** under the text expands it, **Less** folds it |

While a game runs in Amiberry, Denise Station sets these keys (only when they are empty in `amiberry.conf`):
**F11** toggles full screen / window and **Esc** quits the emulator. **F12** opens Amiberry's own menu. The default
joystick ports are mouse in port 0 and keyboard in port 1. The full list is in the menu under
**Keyboard shortcuts...**.

## Categories from folders

Sub-folders of the game folder become categories (sidebar, Genres):

```
~/Games/Denise/
├── Platform/Turrican2_v1.1.lha      -> category "Platform"
├── Action/Shmups/RType_v1.0.lha     -> category "Action"  (the first sub-folder counts)
├── Role_Playing/DungeonMaster.adf   -> category "Role Playing"  (_ becomes a space)
└── Loose_v1.0.lha                   -> no folder category: keeps its own genre
```

- It is applied on every scan, so moving a file changes its category.
- The folder wins: such a genre cannot be edited by hand and scraping never replaces it.
- Collections sorted by initial letter (`A/`, `B/`, ...) would create categories "A", "B", ...; switch the feature off
  in Settings, Library ("Use the sub-folder name as the category") if your folders are not categories.
- Demos, magazines and the beta variants (folders `Demos`, `Magazines`, `Beta-Demo`, ...) are not looked up online.

## Amiberry integration

Denise Station does not bundle Amiberry. **Settings, Library, Download / update Amiberry** downloads the newest AppImage
from the Amiberry GitHub releases into the hidden folder `~/Games/Denise/.amiberry`. At start-up it checks for a newer
release and says so in the status bar. A game is started like this:

```
<game folder>/.amiberry/Amiberry.AppImage [global args] [game args] --autoload /path/to/Game.lha
```

**Portable mode.** Everything Amiberry needs stays in `.amiberry`: `home` (ROMs, LHA, Floppies, configurations, logs),
`config` (`amiberry.conf`) and `data` (savestates, screenshots, WHDLoad boot files). Denise Station starts the AppImage
with `AMIBERRY_HOME_DIR`, `AMIBERRY_CONFIG_DIR`, `XDG_CONFIG_HOME` and `XDG_DATA_HOME` pointing there. Set
`"amiberry_portable": false` in `settings.json` to let Amiberry use `~/Amiberry` instead.

**ROMs.** Settings, **Import Kickstart ROMs...** copies `*.rom` files and `rom.key` from a folder (for example Amiga
Forever's `Shared/rom`) into `.amiberry/home/ROMs`. **WHDLoad boot files** are fetched with **Update WHDLoad boot
files**.

**Defaults written to `amiberry.conf`** before each launch:
- `default_fullscreen_toggle_key=F11` and `default_quit_key=Escape` when they are empty
- `shader=<value>` when you picked one with the header-bar button
- `gui_theme=Dark.theme` or `Default.theme`, following the light or dark mode of Denise Station. This can be turned
  off with `"amiberry_theme_sync": false` in `settings.json`. The key name was inferred from Amiberry's source and not
  yet confirmed in its documentation, so if Amiberry's menu does not change, compare it with the entry Amiberry itself
  writes after you toggle "Dark theme" under F12, Misc.

**Using another Amiberry** (a native install, for example)? Set **Launch command** in Settings, for instance to
`amiberry`; the AppImage is then not used.

## WHDLoad download tool

**Menu, WHDLoad download...** (or the **Download games** button) downloads ready-made WHDLoad archives (`.lha`,
`.lzx`) from the Retroplay WHDLoad Packs. It is modelled on MrV2K's *WHDLoad Download Tool*.

**Denise Station contains no repository address.** You enter the https address of the repository you use, in the
first-run wizard or in Settings, General. Only that host is contacted. You are responsible for the repository you choose
and for having the right to use what you download.

- Only the five pack folders (Games, Demos, Games Beta & Unofficial, Demos Beta & Unofficial, Magazines) are read.
  Sizes and CRC32 come from the Retroplay DAT files and every download is verified; a damaged file is not kept.
- **Filters:** system (Amiga, AGA, CD32, CDTV, Arcadia, ...), extras (Files, Image, NTSC, ...) and language, plus pack,
  free text and "Only games I don't have". **Move files hidden by the filter to the trash...** moves matching files
  you already downloaded to the trash after you confirm.
- **Save list... / Load list...** keep a selection as a plain text file with one archive name per line.
- **Updates:** archives you have are compared with the server by name, variant and version. Outdated ones show
  "Update: v1.6 -> v1.7" and **Tick updates** selects them. The old file moves to a hidden `.replaced` folder. The
  number of updates shows in the title bar (switch off in Settings, General).
- A copyright notice must be confirmed before a download starts.
- Polite by design: one download at a time with a pause between files; the file list is cached for 3 days.

## Scraping game info

Select a game and press **Scrape info**, or use the menu, **Scrape all missing info**. Sources, in order:

1. **[Open Amiga Game Database](https://openretro.org)** (OAGD): year, developer, publisher, genre, rating,
   description and the box cover. OAGD has no public API, so its public pages are read; a redesign of the site could
   break this (the parser is `denise_scraper.py`).
2. **Libretro thumbnails** (`thumbnails.libretro.com`): box cover fallback, matched by a fuzzy title comparison.

Notes:
- Back cover and manual (PDF) are saved to `~/.local/share/denise-station/extras/<game id>/` when **Extras** is on
  (Settings, Scraping).
- Only empty fields are filled, unless "Overwrite existing info" is ticked. A title you edited is searched exactly as
  typed.
- Requests are paced (about 1.5 s apart). Both sources are free community services, so please keep it that way.
- Settings, Scraping, **Test connection** checks that both sources are reachable.

## Writing descriptions with an AI assistant

The repository contains two helper scripts for writing descriptions for the whole library outside the program, for
example with Gemini. Both only use the standard library.

```sh
python3 tools/gemini_export.py --size 10      # writes ~/Denise-Gemini/prompt.txt and batch_001.json, ...
# paste prompt.txt into the assistant, then each batch; save the answers as answer_001.json, ...
python3 tools/gemini_import.py                # dry run: counts only
python3 tools/gemini_import.py --apply        # writes (after backing up library.db)
```

- `gemini_export.py` merges versions of one game into one entry (id, title, year, developer, publisher, genre).
  Options: `--size`, `--out`, `--only-missing`, `--all-folders`.
- `gemini_import.py` reads `answer_*.json` leniently (missing commas or brackets, code blocks and remarks around the
  JSON are tolerated). Each answer entry has `id`, `tytul`, `opis` and optionally `ocena` (1-100) and `kategorie`
  (`{"Graphics": 90, ...}`), which become the 0-10 score and sub-scores shown under the cover.
- Descriptions already in the library (for example from OAGD) are kept unless you pass `--overwrite`.
- AI-written text can be wrong. Check a few entries, especially for obscure games, before importing a whole batch.

## Settings and data

| What | Where |
|---|---|
| Games, Amiberry, ROMs, savestates | `~/Games/Denise` (the game folder is fixed; Amiberry is in the hidden `.amiberry`) |
| Library (titles, metadata, favourites, play counts) | `~/.local/share/denise-station/library.db` |
| Covers, extras, logs | `~/.local/share/denise-station/` |
| Settings | `~/.config/denise-station/settings.json` |
| Program files (after `install.sh`) | `~/.local/share/denise-station-app` |

The library is stored in `~/.local/share`, not in `~/.config`. To start over completely, quit the program and delete
`~/.local/share/denise-station/` (this also removes favourites and play counts).

## Troubleshooting

- **A game starts and closes at once.** Amiberry has no ROM and fell back to a substitute. Import your Kickstart ROMs
  and run the path scan (Play, First-run setup, **Rescan ROMs**) once.
- **"WHDLoad is not installed".** Use Settings, **Update WHDLoad boot files**.
- **Any other launch problem.** Menu, Logs, last launch log shows the exact command and Amiberry's output.
- **Amiberry window frame is light in dark mode.** The frame is drawn by the window manager (or libdecor), not by
  Amiberry; choose a dark window theme in your desktop settings.
- **The wrong version starts.** Run `denise-station --version` and compare it with the archive you installed; run
  `./install.sh` again if they differ.

## Uninstall

```sh
rm -rf ~/.local/share/denise-station-app
rm -f  ~/.local/bin/denise-station ~/.local/share/applications/local.denisestation.App.desktop
rm -f  ~/.local/share/icons/hicolor/*/apps/denise-station.*
```

Your library and games are not touched. Delete `~/.local/share/denise-station`, `~/.config/denise-station` and
`~/Games/Denise` as well if you want to remove everything.

## Translations

Texts live in `locale/<code>.po` (plain gettext format, read at runtime, no compile step).

- **Add a language:** copy `locale/denise.pot` to `locale/<code>.po`, translate it and add the code and native name to
  `LANGUAGE_NAMES` in `denise_i18n.py`.
- After changing UI strings run `python3 tools/extract.py`. It refreshes `denise.pot` and reports missing translations
  and placeholder mismatches in every `.po`.
- The bundled translations are machine-assisted drafts; corrections from native speakers are welcome.

## Development

```
denise_station.py        GTK 3 interface (main window, dialogs, menus)
setup_wizard.py          first-run wizard
denise_core.py           library database, scanner, Amiberry AppImage handling, command builder (no GUI code)
denise_retroplay.py      Retroplay pack logic: listing, DAT files, filters, updates
whdload_dialog.py        WHDLoad download window
denise_scraper.py        OAGD and Libretro scraper (standard library only)
denise_i18n.py           gettext loader and language names
locale/                  translations (.po) and the template (.pot)
assets/                  icon, placeholder cover
tools/                   extract.py (string check), gemini_export.py, gemini_import.py
```

- The core is GUI-free and uses only the standard library, so it can be tested without a display.
- GUI smoke tests can run headless: `xvfb-run -a python3 denise_station.py`.
- Version number: `VERSION` in `denise_core.py`.
- Contributions are welcome: open an issue before larger changes, keep `python3 tools/extract.py` at 0 missing
  translations and keep the program free of third-party Python packages.

## Legal

- Denise Station is an independent project and is not affiliated with Amiga, Commodore, Cloanto, Amiberry or any
  repository it can download from. Amiga and Commodore are trademarks of their respective owners.
- Denise Station does not include games, ROMs or a repository address, and it does not download Kickstart ROMs.
  You need Kickstart ROMs you own, for example from [Amiga Forever](https://www.amigaforever.com/).
- Many Amiga games, demos and magazines are still under copyright. Download only what you are allowed to use.
- Scrapers request public pages at a polite rate; respect the terms of the services they use.

## License

GPL-2.0. Author: Elyon (mateusz.zimniak@protonmail.com).
