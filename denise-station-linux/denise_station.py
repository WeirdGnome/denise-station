#!/usr/bin/env python3
"""Denise Station - a clean-room Amiga game launcher using Amiberry (AppImage).

GTK3 front end. Logic lives in denise_core.py, scraping in denise_scraper.py,
translations in denise_i18n.py + locale/*.po.
"""
import collections
import concurrent.futures
import hashlib
import json
from pathlib import Path
import os
import random
import shlex
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gio, Gtk, Pango  # noqa: E402

import denise_core as core  # noqa: E402
import denise_i18n as i18n  # noqa: E402
import denise_scraper as scraper  # noqa: E402
import setup_wizard  # noqa: E402
import whdload_dialog  # noqa: E402
from denise_i18n import _  # noqa: E402


THUMB_W, THUMB_H = 120, 160
DETAIL_W = 290            # width of the panel on the right
GRID_CHUNK = 150          # tiles in the icon view at first; more are added while scrolling          # tile picture in the cover grid (3:4, like a box cover)


def make_thumb(path):
    """Cover-crop a picture to a THUMB_W x THUMB_H tile (None when it cannot be read)."""
    try:
        info = GdkPixbuf.Pixbuf.get_file_info(str(path))
        if not info or not info[0]:
            return None
        sw, sh = info[1], info[2]
        scale = max(THUMB_W / sw, THUMB_H / sh)
        nw, nh = max(THUMB_W, round(sw * scale)), max(THUMB_H, round(sh * scale))
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), nw, nh, False)
        return pb.new_subpixbuf((pb.get_width() - THUMB_W) // 2, (pb.get_height() - THUMB_H) // 2,
                                THUMB_W, THUMB_H).copy()
    except GLib.Error:
        return None


THUMB_DIR = core.DATA_DIR / "thumbs"


def is_blank_cover(path) -> bool:
    """True for an (almost) plain white picture: OAGD serves one for titles whose cover is not published."""
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 32, 32, False)
    except GLib.Error:
        return False
    n, rs, px = pb.get_n_channels(), pb.get_rowstride(), pb.get_pixels()
    white = total = 0
    for y in range(pb.get_height()):
        row = y * rs
        for x in range(pb.get_width()):
            o = row + x * n
            total += 1
            if px[o] >= 238 and px[o + 1] >= 238 and px[o + 2] >= 238:
                white += 1
    return total > 0 and white / total >= 0.98


def _thumb_file(path):
    try:
        st = os.stat(path)
        key = hashlib.sha1(f"{path}|{st.st_size}|{st.st_mtime_ns}|{THUMB_W}x{THUMB_H}".encode()).hexdigest()
    except OSError:
        return None
    return THUMB_DIR / (key + ".jpg")


def cached_thumb(path, only_store=False):
    """make_thumb() with a small on-disk cache (keyed by path, size and mtime), so big covers are decoded once.
    Safe to call from a worker thread. With only_store the tile is just written to the cache (returns None):
    used to warm the cache in the background without decoding the cached ones again."""
    f = _thumb_file(path)
    if f is None:
        return None
    if only_store and f.exists():
        return None
    try:
        return GdkPixbuf.Pixbuf.new_from_file(str(f))
    except GLib.Error:
        pass
    pb = make_thumb(path)
    if pb is not None:
        try:
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            pb.savev(str(f) + ".tmp", "jpeg", ["quality"], ["88"])
            os.replace(str(f) + ".tmp", f)
        except (GLib.Error, OSError):
            pass
    return None if only_store else pb


class Appearance:
    """Light/dark scheme: 'auto' follows the desktop's colour-scheme (GNOME 42+ / Mint), else light or dark."""
    SCHEMA = "org.gnome.desktop.interface"

    def __init__(self):
        self.mode = "auto"
        self.desktop = None
        src = Gio.SettingsSchemaSource.get_default()
        if src is not None and src.lookup(self.SCHEMA, True) is not None:
            self.desktop = Gio.Settings.new(self.SCHEMA)
            if "color-scheme" in self.desktop.list_keys():
                self.desktop.connect("changed::color-scheme", lambda *_a: self.apply())
            else:
                self.desktop = None

    def set_mode(self, mode):
        self.mode = mode if mode in ("auto", "light", "dark") else "auto"
        self.apply()

    def wants_dark(self):
        if self.mode != "auto":
            return self.mode == "dark"
        return bool(self.desktop) and self.desktop.get_string("color-scheme") == "prefer-dark"

    def apply(self):
        gs = Gtk.Settings.get_default()
        if gs is not None:
            # A theme that is dark by itself (e.g. Mint-Y-Dark) stays dark in 'auto' with this set to False.
            gs.set_property("gtk-application-prefer-dark-theme", self.wants_dark())


def stars(n):
    n = max(0, min(5, int(n or 0)))
    return "★" * n + "☆" * (5 - n)


def bold(text):
    return "<b>%s</b>" % GLib.markup_escape_text(text)


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Denise Station")
        core.migrate_old_data()
        self.settings = core.load_settings()
        i18n.set_language(self.settings["language"])
        self.appearance = Appearance()
        self.appearance.set_mode(self.settings.get("appearance", "auto"))
        self.set_default_size(1380, 780)
        icon_file = Path(__file__).resolve().parent / "assets" / "denise-station.png"
        try:
            if icon_file.exists():
                self.set_icon_from_file(str(icon_file))
            else:
                self.set_icon_name("applications-games")
        except GLib.Error:
            self.set_icon_name("applications-games")
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        core.SCREENS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            core.GAMES_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.lib = core.Library(core.DB_PATH)
        if self.settings.get("legacy_folders"):        # the game folder is now fixed: carry the library over
            core.migrate_library_paths(self.lib.db, self.settings["legacy_folders"])
            core.save_settings(self.settings)
        self.view = ("genre", "Games")
        self.current_id = None
        self._refreshing = False
        self._scraping = False
        self.cancel = threading.Event()

        self._fullscreen = False
        self.ensure_custom_folder()
        self._build_header()
        self._build_body()
        self.connect("key-press-event", self.on_key)
        self.refresh_sidebar()
        self.refresh_list()
        self.show_all()
        self.refresh_state_list(self.lib.get(self.current_id) if self.current_id else None)   # show_all revealed it
        self.b_stop.hide()
        self.set_view_mode(self.settings.get("view_mode", "grid"))
        GLib.idle_add(self.check_updates)
        if not self.settings.get("setup_done"):
            if setup_wizard.is_done(self.settings):      # an older, working install: nothing to set up
                self.settings["setup_done"] = True
                core.save_settings(self.settings)
            else:
                GLib.idle_add(self.run_wizard)
        GLib.idle_add(self.purge_blank_covers)
        GLib.timeout_add(4000, self.check_amiberry_update)

    # ------------------------------------------------------------------ UI
    def _build_header(self):
        hb = Gtk.HeaderBar(show_close_button=True, title="Denise Station")
        self.set_titlebar(hb)

        self.search = Gtk.SearchEntry(placeholder_text=_("Search title, developer, publisher…"))
        self.search.set_width_chars(34)
        self.search.connect("search-changed", lambda *a: self.refresh_list())
        self.search_btn = Gtk.ToggleButton()
        self.search_btn.set_image(Gtk.Image.new_from_icon_name("edit-find-symbolic", Gtk.IconSize.BUTTON))
        self.search_btn.set_tooltip_text(_("Search") + " (Ctrl+F)")
        self.search_btn.connect("toggled", lambda b: self.show_search(b.get_active()))
        hb.pack_start(self.search_btn)
        hb.pack_start(self.search)
        self.search.set_no_show_all(True)             # hidden until the magnifier or Ctrl+F
        self.search.hide()

        self.b_view = Gtk.ToggleButton()
        self.b_view.set_image(Gtk.Image.new_from_icon_name("view-grid-symbolic", Gtk.IconSize.BUTTON))
        self.b_view.set_tooltip_text(_("Cover grid / list"))
        self.b_view.connect("toggled", lambda b: self.set_view_mode("grid" if b.get_active() else "list"))
        hb.pack_start(self.b_view)

        self.dl_btn = dl_btn = Gtk.Button.new_from_icon_name("folder-download-symbolic", Gtk.IconSize.BUTTON)
        dl_btn.set_tooltip_text(_("Download games") + " – " + _("WHDLoad download…"))
        dl_btn.connect("clicked", lambda *a: whdload_dialog.open_dialog(self))

        self.scrape_btn = Gtk.Button.new_from_icon_name("dialog-information-symbolic", Gtk.IconSize.BUTTON)
        self.scrape_btn.set_tooltip_text(_("Scrape") + " – " + _("Scrape all missing info"))
        self.scrape_btn.connect("clicked", lambda *a: self.scrape_missing())

        menu_btn = Gtk.MenuButton()
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu = Gtk.Menu()
        logs = Gtk.MenuItem(label=_("Logs"))                  # one entry, two logs inside
        sub = Gtk.Menu()
        for label, cb in ((_("Open last launch log"), self.open_log),
                          (_("Open last scraping log"), self.open_scrape_log)):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda m, cb=cb: cb())
            sub.append(mi)
        logs.set_submenu(sub)
        # top to bottom: Denise settings, Amiberry settings, Logs, Keyboard shortcuts, About
        for label, cb in ((_("Settings…"), self.open_settings),
                          (_("Amiberry configuration…"), self.edit_amiberry_conf),
                          (_("Open Amiberry"), self.open_amiberry),
                          (_("Setup wizard…"), self.run_wizard),
                          (None, None),
                          (_("Keyboard shortcuts…"), self.show_shortcuts),
                          (_("About"), self.show_about)):
            if label is None:
                menu.append(logs)
                continue
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda m, cb=cb: cb())
            menu.append(mi)
        menu.show_all()
        menu_btn.set_popup(menu)
        self.fw_btn = Gtk.ToggleButton()
        self.fw_btn.set_image(Gtk.Image.new_from_icon_name("video-display-symbolic", Gtk.IconSize.BUTTON))
        self.fw_btn.set_active(bool(self.settings.get("amiberry_fullwindow")))
        self.fw_btn.set_tooltip_text(_("Amiberry: start in full window mode"))
        self.fw_btn.connect("toggled", self.on_fullwindow_toggled)
        self.fs_btn = Gtk.Button.new_from_icon_name("view-fullscreen-symbolic", Gtk.IconSize.BUTTON)
        self.fs_btn.set_tooltip_text(_("Full screen") + " (F11)")
        self.fs_btn.connect("clicked", lambda *a: self.toggle_fullscreen())
        self.connect("window-state-event", self.on_window_state)

        rnd = Gtk.Button.new_from_icon_name("media-playlist-shuffle-symbolic", Gtk.IconSize.BUTTON)
        rnd.set_tooltip_text(_("Pick a random game (Ctrl+R)"))
        rnd.connect("clicked", lambda *a: self.pick_random())

        scan = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        scan.set_tooltip_text(_("Rescan game folders (F5)"))
        scan.connect("clicked", lambda *a: self.start_scan())
        self.sh_btn = Gtk.Button()
        self.sh_btn.set_image(Gtk.Image.new_from_icon_name("applications-graphics-symbolic", Gtk.IconSize.BUTTON))
        self.sh_btn.connect("clicked", lambda *a: self.cycle_shader())
        self._shader_tip()

        # right side, left to right: Amiberry full window, shader, random | rescan, scrape, download | full screen, menu
        def sep():
            sp = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_top=8, margin_bottom=8)
            return sp
        for w in reversed((self.fw_btn, self.sh_btn, rnd, sep(), scan, self.scrape_btn, dl_btn, sep(),
                           self.fs_btn, menu_btn)):
            hb.pack_end(w)

    def _build_body(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(outer)

        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)     # games on the left, a compact fixed-width panel on the right
        outer.pack_start(inner, True, True, 0)

        # game list: id, title, genre, year, stars, plays, fav
        # + thumbnail (None until loaded), file path, text under the cover
        self.store = Gtk.ListStore(int, str, str, str, str, int, str, GdkPixbuf.Pixbuf, str, str, str)
        self.tree = Gtk.TreeView()
        self.tree.set_search_column(1)
        for i, (name, expand) in enumerate(((_("Title"), True), (_("Genre"), False),
                                             (_("Year"), False), (_("Rating"), False),
                                             (_("Plays"), False), ("♥", False))):
            col = Gtk.TreeViewColumn(name, Gtk.CellRendererText(), text=i + 1)
            col.set_resizable(True)
            col.set_expand(expand)
            col.set_sort_column_id(i + 1)
            self.tree.append_column(col)
        self.tree.get_selection().connect("changed", self.on_game_selected)
        self.tree.connect("row-activated", lambda *a: self.play_current())
        self.tree.connect("button-press-event", self.on_tree_click)
        sw2 = Gtk.ScrolledWindow()
        sw2.add(self.tree)

        # cover grid: shares the model with the list, so selection, search and sorting are the same
        self.grid = Gtk.IconView(margin=6)
        self.grid.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.grid.set_item_width(THUMB_W + 8)
        self.grid.set_spacing(1)
        self.grid.set_row_spacing(2)
        self.grid.set_column_spacing(2)
        self.grid.set_item_padding(3)
        self.grid.set_activate_on_single_click(False)
        pix = Gtk.CellRendererPixbuf(xalign=0.5, yalign=0.5)
        self.grid.pack_start(pix, False)
        self.grid.add_attribute(pix, "pixbuf", 7)
        txt = Gtk.CellRendererText(xalign=0.5, scale=0.85, wrap_width=THUMB_W, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                   alignment=Pango.Alignment.CENTER)      # long names wrap onto more lines, never cut off
        self.grid.pack_start(txt, False)
        self.grid.add_attribute(txt, "text", 9)
        self.grid.connect("selection-changed", self.on_grid_selected)
        self.grid.connect("item-activated", lambda *a: self.play_current())
        self.grid.connect("button-press-event", self.on_tree_click)
        self.sw_grid = Gtk.ScrolledWindow()
        self.sw_grid.add(self.grid)
        self.sw_grid.get_vadjustment().connect("value-changed", lambda *a: (self.more_tiles(), self.schedule_thumbs()))
        self.sw_grid.connect("size-allocate", lambda *a: self.schedule_thumbs())

        self.views = Gtk.Stack()
        self.views.add_named(self.sw_grid, "grid")
        self.views.add_named(sw2, "list")
        # category tabs above the games; favourites come first inside each tab
        self.tabs = Gtk.Notebook(scrollable=True, show_border=False)
        self.tabs.popup_enable()
        self._tab_vals = []
        self.tabs.connect("switch-page", self.on_tab_changed)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left.pack_start(self.tabs, False, False, 0)
        left.pack_start(self.views, True, True, 0)
        inner.pack_start(left, True, True, 0)
        self._tsig = None
        self._rows = []
        self._thumb_job = 0
        self._tcache = collections.OrderedDict()     # game id -> tile (memory)
        self._tpending = set()
        self._tmissing = set()
        self._tshown = set()                         # row indexes that currently hold a tile
        self._tpool = ThreadPoolExecutor(max_workers=min(6, max(3, os.cpu_count() or 4)))   # decoding releases the GIL
        self._warm_gen = 0
        self._placeholders = {}
        self._detail_folder = ""

        inner.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
        inner.pack_end(self._build_detail(), False, False, 0)
        self.inner = inner

        bar = Gtk.Box(spacing=6, margin=4)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        bar.pack_start(self.status, True, True, 0)
        self.b_stop = Gtk.Button(label=_("Stop"))
        self.b_stop.connect("clicked", lambda *a: self.cancel.set())
        bar.pack_end(self.b_stop, False, False, 0)
        outer.pack_end(bar, False, False, 0)

    def _build_detail(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        box.set_size_request(DETAIL_W, -1)
        self.shot = Gtk.Image()
        self.shot_box = Gtk.EventBox()               # click on the cover opens a large preview
        self.shot_box.add(self.shot)
        self.shot_box.connect("button-press-event", lambda *a: self.preview_cover())
        box.pack_start(self.shot_box, False, False, 0)

        nav = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.b_prev = Gtk.Button.new_with_label("◀")
        self.b_next = Gtk.Button.new_with_label("▶")
        self.l_gallery = Gtk.Label(label="")
        self.b_manual = Gtk.Button.new_with_label(_("Open manual"))
        self.b_prev.connect("clicked", lambda *a: self.gallery_step(-1))
        self.b_next.connect("clicked", lambda *a: self.gallery_step(1))
        self.b_manual.connect("clicked", lambda *a: self.open_manual())
        for w in (self.b_prev, self.l_gallery, self.b_next):
            nav.pack_start(w, False, False, 0)
        box.pack_start(nav, False, False, 0)
        self.b_manual.set_halign(Gtk.Align.CENTER)      # the manual button gets its own centred row
        box.pack_start(self.b_manual, False, False, 0)
        self.gallery, self.gallery_i, self.manual = [], 0, None

        self.d_title = Gtk.Label(xalign=0, wrap=True, selectable=True)
        box.pack_start(self.d_title, False, False, 0)
        # rating and description sit right under the picture and title
        self.d_stars = Gtk.Label(xalign=0, use_markup=True)
        box.pack_start(self.d_stars, False, False, 0)
        self.d_marks = Gtk.Label(xalign=0, wrap=True, max_width_chars=1)
        self.d_marks.get_style_context().add_class("dim-label")
        box.pack_start(self.d_marks, False, False, 0)
        self.d_notes = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True, max_width_chars=1,
                                 lines=7, ellipsize=Pango.EllipsizeMode.END)
        box.pack_start(self.d_notes, False, False, 0)
        self._notes_open = False
        self.b_more = Gtk.Button(label=_("More…"), relief=Gtk.ReliefStyle.NONE, halign=Gtk.Align.START, no_show_all=True)
        self.b_more.connect("clicked", self.on_notes_more)
        self.d_notes.connect("size-allocate", lambda *a: GLib.idle_add(self.update_more_button))
        box.pack_start(self.b_more, False, False, 0)

        grid = Gtk.Grid(column_spacing=12, row_spacing=2, halign=Gtk.Align.START)
        self.d_vals = {}
        for i, (key, label) in enumerate((("genre", _("Genre")), ("year", _("Year")),
                                          ("developer", _("Developer")),
                                          ("publisher", _("Publisher")),
                                          ("plays", _("Played")), ("last", _("Last played")),
                                          ("scraped", _("Info source")))):
            k = Gtk.Label(label=label, xalign=0, yalign=0, halign=Gtk.Align.START)
            k.get_style_context().add_class("dim-label")
            v = Gtk.Label(xalign=0, wrap=True, selectable=True, halign=Gtk.Align.START)
            grid.attach(k, 0, i, 1, 1)
            grid.attach(v, 1, i, 1, 1)
            self.d_vals[key] = v
        box.pack_start(grid, False, False, 0)

        # The file path gets the full width of the panel (below the table) and wraps instead of being cut.
        k = Gtk.Label(label=_("File"), xalign=0)
        k.get_style_context().add_class("dim-label")
        box.pack_start(k, False, False, 0)
        v = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.CHAR, selectable=True, max_width_chars=1)
        self.d_vals["file"] = v
        box.pack_start(v, False, False, 0)


        bar = Gtk.Box(spacing=8, margin=8)                         # always visible, never scrolled away
        # left: scrollable list of the game's savestates (date and time only); double-click starts from it
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.st_label = Gtk.Label(label=_("Savestates"), xalign=0)
        self.st_label.get_style_context().add_class("dim-label")
        self.st_store = Gtk.ListStore(str, str, str)             # date, file name, path
        self.st_tree = Gtk.TreeView(model=self.st_store, headers_visible=False)
        self.st_tree.append_column(Gtk.TreeViewColumn("", Gtk.CellRendererText(), text=0))
        self.st_tree.set_tooltip_text(_("Double-click: play from this savestate") + "\n" + _("Right click: Rename…, Delete"))
        self.st_tree.connect("button-press-event", self.on_state_click)
        self.st_tree.connect("row-activated", lambda tv, p, c: self.play_current(self.st_store[p][2]))
        self.st_sw = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.st_sw.set_min_content_height(150)
        self.st_sw.add(self.st_tree)
        left.pack_start(self.st_label, False, False, 0)
        left.pack_start(self.st_sw, True, True, 0)
        bar.pack_start(left, True, True, 0)

        # right: the buttons, one under another
        btns = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.b_play = Gtk.Button(label="▶  " + _("Play"))
        self.b_play.get_style_context().add_class("suggested-action")
        self.b_play.connect("clicked", lambda *a: self.play_current())
        self.b_fav = Gtk.Button(label="♥ " + _("Favorite"))
        self.b_fav.connect("clicked", lambda *a: self.toggle_flag("favorite"))
        self.b_edit = Gtk.Button(label=_("Edit…"))
        self.b_edit.connect("clicked", lambda *a: self.edit_current())
        self.b_scrape = Gtk.Button(label=_("Scrape info"))
        self.b_scrape.set_tooltip_text(_("Fetch genre, year and cover from the Open Amiga Game Database, then Libretro"))
        self.b_scrape.connect("clicked", lambda *a: self.scrape_current())
        self.b_shot = Gtk.Button(label=_("Set cover…"))
        self.b_shot.connect("clicked", lambda *a: self.set_screenshot())
        self.b_adv = Gtk.Button.new_from_icon_name("edit-find-symbolic", Gtk.IconSize.BUTTON)
        self.b_adv.set_tooltip_text(_("Advanced scrape…"))
        self.b_adv.connect("clicked", lambda *a: self.advanced_scrape())
        scrape_row = Gtk.Box(spacing=4)
        scrape_row.pack_start(self.b_scrape, True, True, 0)
        scrape_row.pack_start(self.b_adv, False, False, 0)
        for w in (self.b_play, self.b_fav, self.b_edit, scrape_row, self.b_shot):
            btns.pack_start(w, False, False, 0)
        for b in (self.b_play, self.b_fav, self.b_edit, self.b_scrape, self.b_shot):
            lab = b.get_child()
            if isinstance(lab, Gtk.Label):
                lab.set_ellipsize(Pango.EllipsizeMode.END)       # a long translation never widens the panel
        btns.set_size_request(128, -1)
        bar.pack_start(btns, False, False, 0)

        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(box)
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        panel.pack_start(sw, True, True, 0)
        panel.pack_start(bar, False, False, 0)
        return panel

    # ------------------------------------------------------------ refresh
    def say(self, text):
        self.status.set_text(text)
        return False

    CUSTOM_TAB = False        # the Custom tab is switched off for now; set True to bring it (and its folder) back

    def ensure_custom_folder(self):
        """<game folder>/Custom: put your own disk images (hdf, adf, ...) there; it is the Custom tab."""
        if not self.CUSTOM_TAB:
            return
        for root in self.settings.get("folders") or []:
            try:
                (Path(os.path.expanduser(root)) / "Custom").mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    TAB_ORDER = (("games", ""), ("beta-game", ""), ("beta-games", ""), ("demos", ""), ("demo", ""),
                 ("beta-demo", ""), ("beta-demos", ""), ("magazines", ""), ("custom", ""))

    def refresh_sidebar(self):
        """Rebuild the tab bar (there is no sidebar any more): Games, Beta-Game, Demos, Beta-Demo, Magazines, Custom,
        then any other genre; Recently played comes first."""
        self._refreshing = True
        c = self.lib.counts()
        counts = dict(self.lib.genre_counts())
        if self.CUSTOM_TAB and not any((g or "").strip().lower() == "custom" for g in counts):
            counts["Custom"] = 0                              # the Custom tab is always there, even when empty
        fixed = {k: (i, lab) for i, (k, lab) in enumerate(self.TAB_ORDER)}
        entries, used = [("recent", None, _("Recently played"))], set()          # first tab
        for key, (rank, lab) in sorted(fixed.items(), key=lambda kv: kv[1][0]):
            for g, n in counts.items():
                if g and g.strip().lower() == key and g not in used:
                    used.add(g)
                    entries.append(("genre", g, "%s (%d)" % (g, n)))       # the original folder name
        for g in sorted((g for g in counts if g and g not in used), key=str.lower):
            entries.append(("genre", g, "%s (%d)" % (g, counts[g])))
        if counts.get(""):
            entries.append(("genre", "", "%s (%d)" % (_("Unsorted"), counts[""])))
        self._tab_vals = [(k, v) for k, v, _l in entries]
        while self.tabs.get_n_pages():
            self.tabs.remove_page(0)
        for k, v, label in entries:
            self.tabs.append_page(Gtk.Box(), Gtk.Label(label=label))
        full = {(k, v) for k, v, lab in entries if k == "genre" and not lab.endswith("(0)")}
        if (not getattr(self, "_tab_chosen", False) and full and self.view not in full) \
                or self.view not in self._tab_vals or (self.view[0] == "genre" and self.view not in full and full):
            # an unknown or empty tab (e.g. Custom while the library was still empty): the first tab with games
            self.view = next(((k, v) for k, v, lab in entries if (k, v) in full), self._tab_vals[0])
        self.tabs.show_all()
        self.tabs.set_current_page(self._tab_vals.index(self.view))
        self._refreshing = False

    def on_tab_changed(self, nb, page, i):
        if self._refreshing or i >= len(self._tab_vals):
            return
        self.view = self._tab_vals[i]
        self._tab_chosen = True                               # from now on the tab you picked is kept
        self.refresh_list()

    def refresh_list(self):
        kind, value = self.view
        qkind = "favorites" if kind == "favorite" else kind
        rows = self.lib.query(self.search.get_text().strip(), qkind, value)
        keep = self.current_id
        self._refreshing = True
        self.tree.set_model(None)                        # no view re-lays itself out while we fill the store
        self.grid.set_model(None)
        self.store.clear()
        try:
            sig = core.SCREENS_DIR.stat().st_mtime_ns    # covers added or changed since the last fill?
        except OSError:
            sig = 0
        if sig != self._tsig:
            self._tcache.clear()
            self._tmissing.clear()
            self._tsig = sig
        self._tshown.clear()
        self._rows = rows
        idx = next((i for i, r in enumerate(rows) if r["id"] == keep), 0 if rows else None)
        # The list shows everything; the icon view lays out all its items at once, so it gets a window of
        # tiles that grows while you scroll down.
        limit = len(rows) if not self.grid_mode() else max(GRID_CHUNK, (idx or 0) + 200)
        self.append_rows(0, limit)
        self.attach_models()
        if idx is not None:
            self.select_row(Gtk.TreePath.new_from_indices([idx]))
        self._refreshing = False
        self.show_detail(rows[idx]["id"] if idx is not None else None)
        self.schedule_thumbs()
        self.warm_thumbs(rows)
        if not self._scraping:
            self.say(_("Games: {n}").format(n=len(rows)))

    def show_detail(self, gid):
        self.current_id = gid
        g = self.lib.get(gid) if gid else None
        for b in (self.b_play, self.b_fav, self.b_edit, self.b_shot, self.b_scrape, self.b_adv):
            b.set_sensitive(g is not None)
        if g is None:
            self.d_title.set_markup("<big>%s</big>" % GLib.markup_escape_text(_("No game selected")))
            for v in self.d_vals.values():
                v.set_text("")
            self.d_notes.set_text("")
            self.refresh_state_list(None)
            self.d_stars.set_text("")
            self.d_marks.set_text("")
            self.d_stars.hide()
            self.d_marks.hide()
            self.shot.clear()
            self.gallery, self.manual = [], None
            for w in (self.b_prev, self.b_next, self.b_manual):
                w.hide()
            self.l_gallery.set_text("")
            return
        self.d_title.set_markup("<span size='x-large' weight='bold'>%s</span>"
                                % GLib.markup_escape_text(g["title"]))
        v = self.d_vals
        v["genre"].set_text(g["genre"] or _("Unsorted"))
        v["year"].set_text(g["year"])
        v["developer"].set_text(g["developer"])
        v["publisher"].set_text(g["publisher"])
        v["plays"].set_text(f"{g['plays']}×")
        v["last"].set_text(time.strftime("%Y-%m-%d %H:%M", time.localtime(g["last_played"]))
                           if g["last_played"] else _("never"))
        if g["scraped"] > 0:
            src = {"oagd": "OAGD", "libretro": _("Libretro (cover only)")}.get(g["source"], "")
            when = time.strftime("%Y-%m-%d", time.localtime(g["scraped"]))
            v["scraped"].set_text(f"{src}, {when}" if src else when)
        elif g["scraped"] < 0:
            v["scraped"].set_text(_("no match found"))
        else:
            v["scraped"].set_text(_("not yet"))
        v["file"].set_text(g["path"])
        v["file"].set_tooltip_text(g["path"])
        self.set_notes_open(False)                              # every game starts collapsed
        self.d_notes.set_text(g["notes"])
        self.refresh_state_list(g)
        self.d_notes.set_tooltip_text(g["notes"] or None)       # the full text when it is cut after 7 lines
        self.show_score(g)
        self.b_fav.set_label("♥ " + (_("Unfavorite") if g["favorite"] else _("Favorite")))
        self._detail_folder = g["folder"]
        main = core.screenshot_for(g)
        extras, self.manual = core.extras_for(g["id"])
        self.gallery = [main] + extras            # first picture: cover/screenshot (None = placeholder)
        self.gallery_i = 0
        self.show_gallery()

    def refresh_state_list(self, g):
        """The compact savestate list in the right panel (hidden when the game has none)."""
        self.st_store.clear()
        states = core.find_savestates(self.settings, g) if g else []
        for st in states:
            self.st_store.append([time.strftime("%Y-%m-%d %H:%M", time.localtime(st["mtime"])), st["name"], st["path"]])
        self.st_label.set_visible(True)                           # the column stays so the buttons keep their place
        self.st_sw.set_visible(True)

    def show_score(self, g):
        """Magazine-style score (0-10) with its parts; the plain star rating when there is no score."""
        if g["score"]:
            sc = ("%.1f" % g["score"]).rstrip("0").rstrip(".")
            self.d_stars.set_markup("<span size='xx-large' weight='bold' foreground='#e0a800'>%s</span>"
                                    "<span size='large' foreground='#e0a800'>/10</span>" % sc)
            try:
                parts = json.loads(g["marks"]) if g["marks"] else {}
            except ValueError:
                parts = {}
            self.d_marks.set_text("   ".join("%s %s" % (n, ("%.1f" % x).rstrip("0").rstrip(".")) for n, x in parts.items()))
            self.d_marks.set_visible(bool(parts))
        else:
            self.d_stars.set_markup("<span size='large' foreground='#e0a800'>%s</span>" % stars(g["rating"])
                                    if g["rating"] else "")
            self.d_marks.set_text("")
            self.d_marks.set_visible(False)           # no empty gap between the stars and the text
        self.d_stars.set_visible(bool(g["score"] or g["rating"]))

    COVER_W, COVER_MAX_H = DETAIL_W - 16, 400             # the cover fills the panel width (panel 380 - margins)

    def _cover_path(self):
        pic = self.gallery[self.gallery_i] if self.gallery else None
        if pic:
            return str(pic)
        if core.PLACEHOLDER_COVER.exists():
            return str(core.placeholder_for(self._detail_folder))
        return None

    def show_gallery(self):
        path = self._cover_path()
        try:
            if path:
                pb = GdkPixbuf.Pixbuf.new_from_file(path)
                w, h = pb.get_width(), pb.get_height()
                k = min(self.COVER_W / w, self.COVER_MAX_H / h)
                if abs(k - 1) > 0.001:
                    pb = pb.scale_simple(max(1, int(w * k)), max(1, int(h * k)), GdkPixbuf.InterpType.BILINEAR)
                self.shot.set_from_pixbuf(pb)
                self.shot.set_halign(Gtk.Align.CENTER)
                self.shot_box.set_tooltip_text(_("Click to enlarge"))
            else:
                self.shot.set_from_icon_name("image-x-generic-symbolic", Gtk.IconSize.DIALOG)
        except GLib.Error:
            self.shot.set_from_icon_name("image-missing", Gtk.IconSize.DIALOG)
        n = len(self.gallery)
        self.b_prev.set_visible(n > 1)
        self.b_next.set_visible(n > 1)
        self.l_gallery.set_text("%d / %d" % (self.gallery_i + 1, n) if n > 1 else "")
        self.b_manual.set_visible(self.manual is not None)

    def preview_cover(self):
        path = self._cover_path()
        if not path or self.current_id is None:
            return
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
        except GLib.Error:
            return
        scr = self.get_screen()
        maxw, maxh = int(scr.get_width() * 0.85), int(scr.get_height() * 0.85)
        w, h = pb.get_width(), pb.get_height()
        k = min(maxw / w, maxh / h, 2.0)
        if abs(k - 1) > 0.001:
            pb = pb.scale_simple(max(1, int(w * k)), max(1, int(h * k)), GdkPixbuf.InterpType.BILINEAR)
        win = Gtk.Window(title=self.d_title.get_text() or "Denise Station", transient_for=self, modal=True,
                         window_position=Gtk.WindowPosition.CENTER_ON_PARENT, resizable=False)
        eb = Gtk.EventBox()
        eb.add(Gtk.Image.new_from_pixbuf(pb))
        eb.connect("button-press-event", lambda *a: win.destroy())
        win.connect("key-press-event", lambda w_, e: w_.destroy() if e.keyval == Gdk.KEY_Escape else False)
        win.add(eb)
        win.show_all()

    def gallery_step(self, d):
        if len(self.gallery) > 1:
            self.gallery_i = (self.gallery_i + d) % len(self.gallery)
            self.show_gallery()

    def open_manual(self):
        if self.manual and self.manual.exists():
            subprocess.Popen(["xdg-open", str(self.manual)])

    # ----------------------------------------------------------- handlers
    def append_rows(self, a, b):
        """Add rows to the store. Tiles that are in the disk cache are decoded first (in parallel) and go in with
        their row, so covers do not pop in one by one afterwards (every later change re-lays out the whole grid)."""
        rows = self._rows[a:b]
        self._preload_tiles(rows)
        for r in rows:
            tile = self._tcache.get(r["id"])
            if tile is None:
                tile = self.placeholder_thumb(r["folder"])
            else:
                self._tshown.add(len(self.store))
            self.store.append([r["id"], r["title"], r["genre"], r["year"],
                               stars(r["rating"]) if r["rating"] else "", r["plays"],
                               "♥" if r["favorite"] else "", tile, r["path"],
                               ("♥ " if r["favorite"] else "") + r["title"], r["folder"]])

    def _disk_tile(self, r):
        """Tile from the on-disk cache only (never generated here). Safe in a worker thread."""
        src = core.screenshot_for({"id": r["id"], "path": r["path"]})
        f = _thumb_file(src) if src else None
        if f is None:
            return None
        try:
            return GdkPixbuf.Pixbuf.new_from_file(str(f))
        except GLib.Error:
            return None

    def _preload_tiles(self, rows, wait=0.6):
        todo = [r for r in rows if r["id"] not in self._tcache and r["id"] not in self._tmissing]
        if not todo:
            return
        futs = {self._tpool.submit(self._disk_tile, r): r["id"] for r in todo}
        done, _pending = concurrent.futures.wait(futs, timeout=wait)
        for f in done:
            try:
                pb = f.result()
            except Exception:
                pb = None
            if pb is not None:
                self._tcache[futs[f]] = pb

    def more_tiles(self):
        """Grid only: add the next chunk of tiles when the end of the loaded part is near."""
        n = len(self.store)
        if not self.grid_mode() or n >= len(self._rows):
            return
        adj = self.sw_grid.get_vadjustment()
        if adj.get_value() + adj.get_page_size() > adj.get_upper() - 2 * adj.get_page_size():
            self.append_rows(n, n + GRID_CHUNK // 2)

    def grid_mode(self):
        return self.settings.get("view_mode", "grid") == "grid"

    def attach_models(self):
        """Only the view that is on screen gets the model, so a 4000-game list is laid out once, not twice."""
        g = self.grid_mode()
        self.tree.set_model(None if g else self.store)
        self.grid.set_model(self.store if g else None)

    def select_row(self, path, center=False):
        if self.grid_mode():
            self.grid.select_path(path)
            self.grid.scroll_to_path(path, center, 0.5 if center else 0, 0)
        else:
            self.tree.get_selection().select_path(path)
            self.tree.scroll_to_cell(path, None, center, 0.5 if center else 0, 0)

    def on_game_selected(self, sel):
        if self._refreshing:
            return
        model, it = sel.get_selected()
        self.show_detail(model[it][0] if it else None)

    def on_grid_selected(self, iv):
        if self._refreshing:
            return
        paths = iv.get_selected_items()
        if paths:
            self.show_detail(self.store[paths[0]][0])

    # ---- thumbnails: only the tiles near the visible area are loaded
    def schedule_thumbs(self):
        if not self._thumb_job:
            self._thumb_job = GLib.idle_add(self.load_thumbs)

    def placeholder_thumb(self, folder=""):
        """Tile picture for a game without a cover, one per category (shared pixbufs, so identity checks work)."""
        src = core.placeholder_for(folder)
        if src not in self._placeholders:
            self._placeholders[src] = make_thumb(src) if src.exists() else None
        return self._placeholders[src]

    def load_thumbs(self):
        """Give the visible tiles (plus a margin) their covers. Decoding happens in worker threads; tiles are
        applied a few per turn, because every change makes the icon view lay itself out again."""
        self._thumb_job = 0
        if self.views.get_visible_child_name() != "grid":
            return False
        rng = self.grid.get_visible_range()
        n = len(self.store)
        if not n:
            return False
        first, last = (rng[0].get_indices()[0], rng[1].get_indices()[0]) if rng else (0, min(n, 40) - 1)
        margin = 90                                          # load well ahead of the view, so scrolling finds tiles ready
        lo, hi = max(0, first - margin), min(n - 1, last + margin)
        budget, more = 24, False
        for i in sorted(range(lo, hi + 1), key=lambda k: abs(k - (first + last) // 2)):
            row = self.store[i]
            blank = self.placeholder_thumb(row[10])
            if row[7] is not blank:
                continue
            pb = self._tcache.get(row[0])
            if pb is not None:
                self._tcache.move_to_end(row[0])             # LRU: tiles in use are dropped last
                if budget:
                    row[7] = pb
                    self._tshown.add(i)
                    budget -= 1
                else:
                    more = True
            elif row[0] not in self._tpending and row[0] not in self._tmissing:
                self._tpending.add(row[0])
                self._tpool.submit(self._thumb_worker, row[0], row[8])
        if len(self._tshown) > 600:                          # free tiles far away from the view (the cache keeps them)
            for i in [k for k in self._tshown if k < lo - 3 * margin or k > hi + 3 * margin]:
                if i < n:
                    self.store[i][7] = self.placeholder_thumb(self.store[i][10])
                self._tshown.discard(i)
        while len(self._tcache) > 2000:
            self._tcache.popitem(last=False)
        if more:
            self._thumb_job = GLib.timeout_add(8, self.load_thumbs)
        return False

    def warm_thumbs(self, rows):
        """After the visible tiles are up, two background threads prepare the on-disk tiles of the whole library
        (this tab first, then everything else), so scrolling and switching tabs find them ready. Only missing
        tiles are generated; a newer list stops the old job."""
        if not self.grid_mode():
            return
        try:
            sig = (core.SCREENS_DIR.stat().st_mtime_ns, core.EXTRAS_DIR.stat().st_mtime_ns if core.EXTRAS_DIR.exists() else 0,
                   self.lib.db.execute("SELECT COUNT(*) FROM games WHERE missing=0").fetchone()[0])
        except OSError:
            sig = None
        if sig is not None and sig == getattr(self, "_warm_done", None):
            return                                           # the whole library was already prepared
        self._warm_gen += 1
        gen = self._warm_gen
        seen = {r["id"] for r in rows}
        items = [(r["id"], r["path"]) for r in rows]
        items += [(r[0], r[1]) for r in self.lib.db.execute(
            "SELECT id, path FROM games WHERE missing=0 AND hidden=0 ORDER BY title COLLATE NOCASE") if r[0] not in seen]

        def job(part):
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)   # this thread yields to the UI
            except (OSError, AttributeError):
                pass
            time.sleep(1.0)                                  # let the visible tiles go first
            for gid, path in part:
                if gen != self._warm_gen:
                    return
                src = core.screenshot_for({"id": gid, "path": path})
                if src:
                    cached_thumb(src, only_store=True)
                time.sleep(0.001)
            finished.append(1)
            if len(finished) == 2 and gen == self._warm_gen:
                self._warm_done = sig                        # both halves finished without being interrupted
        finished = []
        parts = [items[0::2], items[1::2]]
        for part in parts:
            threading.Thread(target=job, args=(part,), daemon=True).start()

    def _thumb_worker(self, gid, path):
        src = core.screenshot_for({"id": gid, "path": path})
        pb = cached_thumb(src) if src else None
        GLib.idle_add(self._thumb_ready, gid, pb)

    def _thumb_ready(self, gid, pb):
        self._tpending.discard(gid)
        if pb is None:
            self._tmissing.add(gid)                          # no picture: keep the placeholder, do not ask again
        else:
            self._tcache[gid] = pb
            self.schedule_thumbs()
        return False

    def set_view_mode(self, mode):
        mode = "grid" if mode == "grid" else "list"
        self.settings["view_mode"] = mode
        core.save_settings(self.settings)
        self.views.set_visible_child_name(mode)
        if getattr(self, "_rows", None):
            self.refresh_list()                       # fills the store the way the new view wants it
        if self.b_view.get_active() != (mode == "grid"):
            self.b_view.set_active(mode == "grid")
        if mode == "grid":
            self.schedule_thumbs()

    def show_search(self, show):
        """Show or hide the search field; hiding it clears the search."""
        if self.search_btn.get_active() != show:
            self.search_btn.set_active(show)          # re-enters here through the toggled handler
            return
        if show:
            self.search.show()
            self.search.grab_focus()
        else:
            self.search.set_text("")
            self.search.hide()
            (self.grid if self.views.get_visible_child_name() == "grid" else self.tree).grab_focus()

    def on_key(self, w, ev):
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        if ctrl and ev.keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.show_search(True)
            return True
        if ctrl and ev.keyval in (Gdk.KEY_r, Gdk.KEY_R):
            self.pick_random()
            return True
        if ev.keyval == Gdk.KEY_Escape and self.search.get_text():
            self.search.set_text("")                      # Esc clears the search (also when the focus is on the games)
            (self.grid if self.views.get_visible_child_name() == "grid" else self.tree).grab_focus()
            return True
        if ev.keyval == Gdk.KEY_Escape and self.search.get_visible():
            self.show_search(False)                       # a second Esc folds the empty field away
            return True
        if ev.keyval == Gdk.KEY_F11 or (ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
                                        and ev.state & Gtk.accelerator_get_default_mod_mask() == Gdk.ModifierType.MOD1_MASK):
            self.toggle_fullscreen()                      # F11 or Alt+Enter
            return True
        if ev.keyval == Gdk.KEY_F5:
            self.start_scan()
            return True
        return False

    def on_tree_click(self, tree, ev):
        if ev.button != 3:
            return False
        hit = tree.get_path_at_pos(int(ev.x), int(ev.y))
        if not hit:
            return False
        if isinstance(tree, Gtk.IconView):
            tree.select_path(hit)
        else:
            tree.get_selection().select_path(hit[0])
        g = self.lib.get(self.current_id)
        menu = Gtk.Menu()
        items = [(_("Play"), self.play_current),
                 (_("Remove from favorites") if g["favorite"] else _("Add to favorites"),
                  lambda: self.toggle_flag("favorite")),
                 (_("Edit…"), self.edit_current),
                 (_("Advanced scrape…"), self.advanced_scrape),
                 (_("Show file in file manager"), self.open_folder)]
        if g["last_played"]:
            items.append((_("Remove from recently played"), self.clear_recent_one))
        for label, cb in items:
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda m, cb=cb: cb())
            menu.append(mi)
        menu.show_all()
        menu.popup_at_pointer(ev)
        return True

    def clear_recent_one(self):
        if self.current_id and self.lib.clear_recent(self.current_id):
            self.refresh_list()
            self.say(_("Removed from recently played."))

    def clear_recent_all(self, parent=None):
        d = Gtk.MessageDialog(transient_for=parent or self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              text=_("Clear the recently played list?"))
        d.format_secondary_text(_("The games stay in the library, and their play counts are kept."))
        d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Clear"), Gtk.ResponseType.YES)
        yes = d.run() == Gtk.ResponseType.YES
        d.destroy()
        if yes:
            n = self.lib.clear_recent()
            self.refresh_list()
            self.say(_("Recently played cleared ({n} games).").format(n=n))

    def _shader_tip(self):
        cur = self.settings.get("amiberry_shader") or ""
        self.sh_btn.set_tooltip_text(_("Amiberry shader: {name} (click for the next one)").format(
            name=cur or _("as set in Amiberry")))

    def cycle_shader(self):
        order = core.SHADERS
        cur = self.settings.get("amiberry_shader") or ""
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
        self.settings["amiberry_shader"] = nxt
        core.save_settings(self.settings)
        self._shader_tip()
        self.say(_("Amiberry shader: {name}").format(name=nxt))

    def on_fullwindow_toggled(self, btn):
        self.settings["amiberry_fullwindow"] = btn.get_active()
        core.save_settings(self.settings)
        self.say(_("Amiberry will start in full window mode.") if btn.get_active()
                 else _("Amiberry will use its own window setting."))

    def set_notes_open(self, on):
        self._notes_open = on
        self.d_notes.set_lines(-1 if on else 7)
        self.d_notes.set_ellipsize(Pango.EllipsizeMode.NONE if on else Pango.EllipsizeMode.END)
        self.b_more.set_label(_("Less") if on else _("More…"))
        self.update_more_button()

    def on_notes_more(self, *_a):
        self.set_notes_open(not self._notes_open)

    def update_more_button(self):
        """Show 'More…' only when the description is cut off (or 'Less' while it is expanded)."""
        show = self._notes_open or (bool(self.d_notes.get_text()) and self.d_notes.get_layout().is_ellipsized())
        self.b_more.set_visible(show)
        return False

    def toggle_fullscreen(self):
        if self._fullscreen:
            self.unfullscreen()
        else:
            self.fullscreen()

    def on_window_state(self, win, ev):
        self._fullscreen = bool(ev.new_window_state & Gdk.WindowState.FULLSCREEN)
        self.fs_btn.set_image(Gtk.Image.new_from_icon_name(
            "view-restore-symbolic" if self._fullscreen else "view-fullscreen-symbolic", Gtk.IconSize.BUTTON))
        self.fs_btn.set_tooltip_text((_("Exit full screen") if self._fullscreen else _("Full screen")) + " (F11)")
        return False

    def pick_random(self):
        n = len(self._rows)
        if not n:
            return
        i = random.randrange(n)
        if i >= len(self.store):                           # not in the icon view yet
            self.append_rows(len(self.store), i + 100)
        path = Gtk.TreePath.new_from_indices([i])
        self.select_row(path, center=True)
        (self.grid if self.views.get_visible_child_name() == "grid" else self.tree).grab_focus()

    def toggle_flag(self, field):
        g = self.lib.get(self.current_id)
        if not g:
            return
        self.lib.update(g["id"], **{field: 0 if g[field] else 1})
        self.refresh_sidebar()
        self.refresh_list()

    def open_folder(self):
        g = self.lib.get(self.current_id)
        if g:
            subprocess.Popen(["xdg-open", os.path.dirname(g["path"])])

    # -------------------------------------------------------------- launch
    def play_current(self, state=""):
        g = self.lib.get(self.current_id) if self.current_id else None
        if not g:
            return
        if not os.path.exists(g["path"]):
            self.message(_("File not found"), g["path"], Gtk.MessageType.WARNING)
            return
        if core.amiberry_missing(self.settings):
            d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  text=_("Amiberry is not installed yet"))
            d.format_secondary_text(_("Denise Station can download Amiberry (AppImage) now."))
            d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Download"), Gtk.ResponseType.YES)
            yes = d.run() == Gtk.ResponseType.YES
            d.destroy()
            if yes:
                self.install_amiberry()
            return
        if core.whdboot_missing(self.settings) and not self.fetch_whdboot():
            return
        # Amiberry has no default key for toggling full screen / window: give it F11 (only if none is set)
        core.ensure_conf_key(core.amiberry_conf_path(self.settings), "default_fullscreen_toggle_key", "F11")
        core.ensure_conf_key(core.amiberry_conf_path(self.settings), "default_quit_key", "Escape")   # Esc quits the emulator
        if self.settings.get("amiberry_shader"):
            core.ensure_conf_key(core.amiberry_conf_path(self.settings), "shader",
                                 self.settings["amiberry_shader"], force=True)
        if self.settings.get("amiberry_theme_sync", True):          # Amiberry's menu follows light/dark of the desktop
            core.ensure_conf_key(core.amiberry_conf_path(self.settings), "gui_theme",
                                 "Dark.theme" if self.appearance.wants_dark() else "Default.theme", force=True)
        cmd = core.build_command(self.settings, g, state)
        core.DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = open(core.LAUNCH_LOG, "wb")
        log.write((" ".join(shlex.quote(c) for c in cmd) + "\n\n").encode())
        log.flush()
        try:
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True)
        except OSError as e:
            log.close()
            self.message(_("Could not start Amiberry"),
                         _("{error}\n\nCommand: {command}").format(error=e, command=" ".join(cmd)),
                         Gtk.MessageType.ERROR)
            return
        self.lib.record_play(g["id"])
        self.say(_("Started: {title}").format(title=g["title"]))
        title = g["title"]

        def waiter():
            rc = proc.wait()
            log.close()
            GLib.idle_add(self.on_exited, title, rc)

        threading.Thread(target=waiter, daemon=True).start()
        self.refresh_list()
        self.say(_("Started: {title}").format(title=title))

    def on_exited(self, title, rc):
        if rc == 0:
            self.say(_("{title} closed.").format(title=title))
        else:
            self.say(_("Amiberry exited with code {code} for {title} - see Menu → Open last launch log.")
                     .format(code=rc, title=title))
        return False

    def open_log(self):
        if core.LAUNCH_LOG.exists():
            subprocess.Popen(["xdg-open", str(core.LAUNCH_LOG)])
        else:
            self.message(_("No log yet"), _("Launch a game first."))

    def open_scrape_log(self):
        if core.SCRAPE_LOG.exists():
            subprocess.Popen(["xdg-open", str(core.SCRAPE_LOG)])
        else:
            self.message(_("No log yet"), _("Scrape a game first."))

    # ------------------------------------------------------------ editing
    def edit_current(self):
        g = self.lib.get(self.current_id) if self.current_id else None
        if not g:
            return
        dlg = Gtk.Dialog(title=_("Game properties"), transient_for=self, modal=True)
        dlg.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.OK)
        dlg.set_default_size(460, -1)
        area = dlg.get_content_area()
        area.set_border_width(12)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        area.add(grid)

        e_title = Gtk.Entry(text=g["title"])
        c_genre = Gtk.ComboBoxText.new_with_entry()
        for name in self.lib.known_genres():
            c_genre.append_text(name)
        c_genre.get_child().set_text(g["genre"])
        if g["folder"] and self.settings["folder_categories"]:
            c_genre.set_sensitive(False)
            c_genre.set_tooltip_text(_("The category comes from the folder. Move the file to another "
                                       "folder (and rescan) to change it."))
        e_year = Gtk.Entry(text=g["year"])
        e_dev = Gtk.Entry(text=g["developer"])
        e_pub = Gtk.Entry(text=g["publisher"])
        s_rate = Gtk.SpinButton.new_with_range(0, 5, 1)
        s_rate.set_value(g["rating"])
        e_args = Gtk.Entry(text=g["args"], placeholder_text=_("e.g. --model A1200"))
        tv = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD)
        tv.get_buffer().set_text(g["notes"])
        sw = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        sw.set_size_request(-1, 110)
        sw.add(tv)

        for i, (lab, w) in enumerate(((_("Title"), e_title), (_("Genre"), c_genre),
                                      (_("Year"), e_year), (_("Developer"), e_dev),
                                      (_("Publisher"), e_pub), (_("Rating (0–5)"), s_rate),
                                      (_("Extra Amiberry args"), e_args), (_("Notes"), sw))):
            w.set_hexpand(True)
            grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
            grid.attach(w, 1, i, 1, 1)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            buf = tv.get_buffer()
            self.lib.update(
                g["id"], title=e_title.get_text().strip() or g["title"],
                genre=c_genre.get_child().get_text().strip(), year=e_year.get_text().strip(),
                developer=e_dev.get_text().strip(), publisher=e_pub.get_text().strip(),
                rating=int(s_rate.get_value()), args=e_args.get_text().strip(),
                notes=buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
            self.refresh_sidebar()
            self.refresh_list()
        dlg.destroy()

    def set_screenshot(self):
        g = self.lib.get(self.current_id) if self.current_id else None
        if not g:
            return
        dlg = Gtk.FileChooserDialog(title=_("Choose a cover"), parent=self,
                                    action=Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Open"), Gtk.ResponseType.OK)
        flt = Gtk.FileFilter()
        flt.set_name(_("Images"))
        for pat in ("*.png", "*.jpg", "*.jpeg"):
            flt.add_pattern(pat)
        dlg.add_filter(flt)
        if dlg.run() == Gtk.ResponseType.OK:
            src = dlg.get_filename()
            ext = os.path.splitext(src)[1].lower()
            self.clear_custom_shots(g["id"])
            shutil.copyfile(src, core.SCREENS_DIR / f"{g['id']}{ext}")
            self.show_detail(g["id"])
        dlg.destroy()

    @staticmethod
    def clear_custom_shots(gid, keep=None):
        for ext in (".png", ".jpg", ".jpeg"):
            p = core.SCREENS_DIR / f"{gid}{ext}"
            if p != keep:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

    # ------------------------------------------------------------ scanning
    def run_wizard(self):
        """First-run wizard (also in the menu): Amiberry, WHDLoad files, ROMs, one Amiberry start to scan paths."""
        open_dl = setup_wizard.run(self)
        self.start_scan()
        if open_dl:
            whdload_dialog.open_dialog(self)
        return False

    def purge_blank_covers(self):
        """Covers saved earlier that are plain white (unpublished on OAGD) are removed, and their games are
        marked as not scraped so "Scrape" fetches them again (Libretro is tried when OAGD has no real cover)."""
        try:
            files = [f for f in core.SCREENS_DIR.iterdir() if f.suffix.lower() in (".jpg", ".jpeg", ".png")
                     and f.stem.isdigit() and f.stat().st_size < 6000]
        except OSError:
            return False
        gone = []
        for f in files:
            if is_blank_cover(f):
                try:
                    f.unlink()
                    gone.append(int(f.stem))
                except OSError:
                    pass
        if gone:
            self.lib.db.executemany("UPDATE games SET scraped=0 WHERE id=?", [(i,) for i in gone])
            self.lib.db.commit()
            self.refresh_list()
            self.say(_("Removed {n} blank covers; use Scrape to fetch them again.").format(n=len(gone)))
        return False

    def set_update_count(self, n):
        # the button is an icon now: the update count goes into its tooltip and a small badge on the icon
        self.dl_btn.set_tooltip_text(_("Download games ({n} updates)").format(n=n) if n
                                     else _("Download games") + " – " + _("WHDLoad download…"))
        if n:
            self.dl_btn.get_style_context().add_class("suggested-action")
        else:
            self.dl_btn.get_style_context().remove_class("suggested-action")
        return False

    def check_updates(self):
        """At start-up: compare your archives with the last downloaded Retroplay list (no network access)."""
        source = whdload_dialog.rp.validate_source(self.settings.get("whdload_source", ""))
        if not source or not self.settings.get("check_updates", True):
            return

        paths = [r["path"] for r in self.lib.db.execute("SELECT path FROM games WHERE missing = 0")]  # main thread

        def worker():
            try:
                entries = whdload_dialog.rp.Retroplay(source, cache_dir=core.DATA_DIR).cached_index()
                ups = whdload_dialog.rp.find_updates(paths, entries)
            except Exception:
                return
            if ups:
                GLib.idle_add(self.set_update_count, len(ups))
                GLib.idle_add(self.say, _("{count} of your WHDLoad archives have a newer version - open Download games.")
                              .format(count=len(ups)))

        threading.Thread(target=worker, daemon=True).start()

    def fetch_whdboot(self, parent=None, ask=True):
        """Download WHDLoad and the boot files with Amiberry's own --download-whdboot. True when WHDLoad is there."""
        if ask:
            d = Gtk.MessageDialog(transient_for=parent or self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                  text=_("WHDLoad is not installed yet"))
            d.format_secondary_text(_("WHDLoad games need the WHDLoad program (from whdload.de), which Amiberry "
                                      "downloads separately. Download it now?"))
            d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Download"), Gtk.ResponseType.YES)
            yes = d.run() == Gtk.ResponseType.YES
            d.destroy()
            if not yes:
                return False
        dlg = Gtk.Dialog(title=_("Download WHDLoad"), transient_for=parent or self, modal=True)
        box = dlg.get_content_area()
        box.set_border_width(16)
        box.set_spacing(10)
        box.pack_start(Gtk.Label(label=_("Downloading WHDLoad and the boot files…"), xalign=0), False, False, 0)
        bar = Gtk.ProgressBar()
        box.pack_start(bar, False, False, 0)
        res = {}

        def worker():
            res["r"] = core.download_whdboot(self.settings)
            GLib.idle_add(lambda: dlg.response(Gtk.ResponseType.OK) and False)

        pulse = GLib.timeout_add(150, lambda: bar.pulse() or True)
        threading.Thread(target=worker, daemon=True).start()
        dlg.show_all()
        dlg.run()
        GLib.source_remove(pulse)
        dlg.destroy()
        ok, out = res.get("r", (False, ""))
        if not ok:
            self.message(_("Download WHDLoad"), _("Could not download WHDLoad.\n\n{out}").format(out=out),
                         Gtk.MessageType.WARNING, parent=parent)
        elif not ask:
            self.message(_("Download WHDLoad"), _("WHDLoad and the boot files are installed."), parent=parent)
        return ok

    def open_amiberry(self):
        """Start Amiberry without a game: its own menu opens (scan ROMs, set paths, configure)."""
        if core.amiberry_missing(self.settings):
            self.message(_("Amiberry not found"), _("Use \"Download / update Amiberry\" to install it."),
                         Gtk.MessageType.WARNING)
            return
        cmd = core.base_command(self.settings)
        log = open(core.LAUNCH_LOG, "wb")
        log.write((" ".join(shlex.quote(c) for c in cmd) + "\n\n").encode())
        log.flush()
        try:
            subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            log.close()
            self.message(_("Could not start Amiberry"), str(e), Gtk.MessageType.ERROR)

    def import_roms(self, parent=None):
        fc = Gtk.FileChooserDialog(title=_("Choose the folder with the Kickstart ROMs (e.g. Amiga Forever → Shared → rom)"),
                                   transient_for=parent or self, action=Gtk.FileChooserAction.SELECT_FOLDER)
        fc.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Import"), Gtk.ResponseType.OK)
        if fc.run() != Gtk.ResponseType.OK:
            fc.destroy()
            return
        src = fc.get_filename()
        fc.destroy()
        dest = core.roms_dir(self.settings)
        copied, same = core.import_roms(src, dest)
        if copied or same:
            self.message(_("Import Kickstart ROMs"),
                         _("Copied {n} file(s) to {path} ({same} were already there).\n\nIf Amiberry does not "
                           "find them, open its menu (F12) and press \"Rescan ROMs\" on the Play page (First-run setup).")
                         .format(n=copied, path=dest, same=same), parent=parent)
        else:
            self.message(_("Import Kickstart ROMs"), _("No Kickstart ROM files found in {path}.").format(path=src),
                         Gtk.MessageType.WARNING, parent=parent)

    def check_amiberry_update(self):
        """At start-up: when Denise installed Amiberry itself, look (once) whether a newer release exists."""
        if not core.appimage_path(self.settings):
            return False

        def worker():
            try:
                tag, newer = core.amiberry_update_info(self.settings)
            except Exception:
                return                                      # offline etc.: stay quiet
            if newer:
                GLib.idle_add(self.say, _("Amiberry {tag} is available - Settings → Download / update Amiberry.")
                              .format(tag=tag))

        threading.Thread(target=worker, daemon=True).start()
        return False

    def install_amiberry(self, parent=None):
        """Download the newest Amiberry AppImage into <game folder>/.amiberry, with a progress bar."""
        if not self.settings.get("folders"):
            self.message(_("Download / update Amiberry"), _("Add a game folder first."), parent=parent)
            return
        dlg = Gtk.Dialog(title=_("Download / update Amiberry"), transient_for=parent or self, modal=True)
        dlg.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
        box = dlg.get_content_area()
        box.set_spacing(10)
        box.set_border_width(16)
        label = Gtk.Label(label=_("Looking for the latest release…"), xalign=0, wrap=True)
        bar = Gtk.ProgressBar()
        box.pack_start(label, False, False, 0)
        box.pack_start(bar, False, False, 0)
        state = {"cancel": False}

        def show_prog(done, total):
            bar.set_fraction(done / total if total else 0.0)
            label.set_text(_("Downloading… {mb} MB").format(mb=done // 1048576))
            return False

        def worker():
            try:
                tag = core.download_appimage(self.settings, lambda d, t: GLib.idle_add(show_prog, d, t),
                                             lambda: state["cancel"])
                state["res"] = (True, tag)
            except Exception as e:
                state["res"] = (False, str(e))
            GLib.idle_add(lambda: dlg.response(Gtk.ResponseType.OK) and False)

        threading.Thread(target=worker, daemon=True).start()
        dlg.show_all()
        r = dlg.run()
        if r != Gtk.ResponseType.OK:
            state["cancel"] = True
        dlg.destroy()
        res = state.get("res")
        if r == Gtk.ResponseType.OK and res and res[0]:
            self.say(_("Amiberry {tag} installed in {path}.").format(tag=res[1], path=core.appimage_dir(self.settings)))
            if core.whdboot_missing(self.settings):
                self.fetch_whdboot(parent, ask=False)
            self.message(_("Download / update Amiberry"),
                         _("Amiberry {tag} is ready. Games start with it from now on.\n\n"
                           "Amiberry now keeps all its files (ROMs, configurations, savestates) in the "
                           "hidden .amiberry folder. Copy your Kickstart ROMs to its \"ROMs\" folder "
                           "(Settings → Open Amiberry data folder).").format(tag=res[1]), parent=parent)
        elif r == Gtk.ResponseType.OK and res:
            self.message(_("Download / update Amiberry"), _("Could not download: {err}").format(err=res[1]),
                         Gtk.MessageType.WARNING, parent=parent)

    def scan_and_scrape(self):
        """Rescan; when new files appeared, fetch their info and pictures (if enabled in the settings)."""
        self._scrape_after_scan = bool(self.settings.get("scrape_after_download", True))
        self.start_scan()

    def start_scan(self):
        self.ensure_custom_folder()
        folders = list(self.settings["folders"])
        if not folders:
            self.say(_("No game folders configured - open Menu → Settings."))
            return
        self.say(_("Scanning…"))
        exts, rec = self.settings["extensions"], self.settings["recurse"]

        def worker():
            found = core.find_games(folders, exts, rec)
            GLib.idle_add(self.scan_done, found)

        threading.Thread(target=worker, daemon=True).start()

    def scan_done(self, found):
        added, missing = self.lib.apply_scan(found, self.settings["folders"], self.settings["folder_categories"])
        self.refresh_sidebar()
        self.refresh_list()
        self.say(_("Scan finished: {found} file(s) found, {added} new, {missing} missing.")
                 .format(found=len(found), added=added, missing=missing))
        if getattr(self, "_scrape_after_scan", False):
            self._scrape_after_scan = False
            ids = self.lib.unscraped_ids()
            if ids and not self._scraping:
                self.start_scrape(ids)
        return False

    # ----------------------------------------------------------- scraping
    def make_scraper(self):
        return scraper.Scraper(cache_dir=core.DATA_DIR)

    @staticmethod
    def scrape_error_text(e):
        if isinstance(e, scraper.ServerBusy):
            return _("The Open Amiga Game Database and Libretro could not be reached. "
                     "Check your internet connection and try again later.")
        return _("Scraping error: {detail}").format(detail=e)

    def edit_amiberry_conf(self):
        """Editor for amiberry.conf: a filterable table of key=value settings, and the raw text."""
        path = core.amiberry_conf_path(self.settings)
        try:
            text = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError as e:
            self.message(_("Amiberry configuration"), _("Could not read: {err}").format(err=e.strerror or e),
                         Gtk.MessageType.WARNING)
            return
        entries = core.parse_conf(text)
        dlg = Gtk.Dialog(title=_("Amiberry configuration"), transient_for=self, modal=True)
        dlg.set_default_size(720, 600)
        dlg.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.OK)
        box = dlg.get_content_area()
        box.set_spacing(6)
        lab = Gtk.Label(label=str(path), xalign=0, margin=10, selectable=True)
        lab.get_style_context().add_class("dim-label")
        box.pack_start(lab, False, False, 0)
        nb = Gtk.Notebook()
        box.pack_start(nb, True, True, 0)

        # page 1: table
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        flt = Gtk.SearchEntry(placeholder_text=_("Filter"))
        page.pack_start(flt, False, False, 0)
        store = Gtk.ListStore(str, str, int)                  # key, value, index in entries
        fmodel = store.filter_new()
        fmodel.set_visible_func(lambda m, it, d: not flt.get_text().strip()
                                or flt.get_text().strip().lower() in (m[it][0] + " " + m[it][1]).lower())
        tv = Gtk.TreeView(model=fmodel)
        tv.append_column(Gtk.TreeViewColumn(_("Key"), Gtk.CellRendererText(), text=0))
        rend = Gtk.CellRendererText(editable=True)
        col = Gtk.TreeViewColumn(_("Value"), rend, text=1)
        col.set_expand(True)
        tv.append_column(col)
        sw = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        sw.add(tv)
        page.pack_start(sw, True, True, 1)
        bar = Gtk.Box(spacing=6)
        b_add = Gtk.Button.new_with_label(_("Add"))
        b_del = Gtk.Button.new_with_label(_("Delete"))
        bar.pack_start(b_add, False, False, 0)
        bar.pack_start(b_del, False, False, 0)
        page.pack_start(bar, False, False, 0)
        nb.append_page(page, Gtk.Label(label=_("Table")))

        # page 2: raw text
        buf = Gtk.TextBuffer()
        tx = Gtk.TextView(buffer=buf, monospace=True, left_margin=6, right_margin=6)
        sw2 = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN, margin=8)
        sw2.add(tx)
        nb.append_page(sw2, Gtk.Label(label=_("Text")))

        def fill():
            store.clear()
            for i, e in enumerate(entries):
                if "k" in e and not e.get("deleted"):
                    store.append([e["k"], e["v"], i])

        def edited(r, path_str, new):
            row = fmodel[path_str]
            i = row[2]
            entries[i]["v"] = new.strip()
            store[fmodel.convert_path_to_child_path(Gtk.TreePath.new_from_string(path_str))][1] = new.strip()
        rend.connect("edited", edited)
        flt.connect("search-changed", lambda *a: fmodel.refilter())

        def add(*_a):
            d = Gtk.Dialog(title=_("Add"), transient_for=dlg, modal=True)
            d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Add"), Gtk.ResponseType.OK)
            g = Gtk.Grid(column_spacing=8, row_spacing=6, margin=12)
            ek, ev = Gtk.Entry(), Gtk.Entry()
            g.attach(Gtk.Label(label=_("Key"), xalign=0), 0, 0, 1, 1)
            g.attach(ek, 1, 0, 1, 1)
            g.attach(Gtk.Label(label=_("Value"), xalign=0), 0, 1, 1, 1)
            g.attach(ev, 1, 1, 1, 1)
            d.get_content_area().add(g)
            d.show_all()
            if d.run() == Gtk.ResponseType.OK and ek.get_text().strip():
                entries.append({"k": ek.get_text().strip(), "v": ev.get_text().strip()})
                fill()
            d.destroy()

        def delete(*_a):
            model, it = tv.get_selection().get_selected()
            if it:
                entries[model[it][2]]["deleted"] = True
                fill()
        b_add.connect("clicked", add)
        b_del.connect("clicked", delete)

        state = {"page": 0}

        def switched(nbk, pg, n):
            if n == 1 and state["page"] == 0:
                buf.set_text(core.serialize_conf(entries))
            elif n == 0 and state["page"] == 1:
                entries[:] = core.parse_conf(buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False))
                fill()
            state["page"] = n
        nb.connect("switch-page", switched)
        fill()
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            if state["page"] == 1:
                out = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)
                out = out.rstrip("\n") + "\n"
            else:
                out = core.serialize_conf(entries)
            try:
                core.save_conf(path, out)
                self.say(_("Saved. The previous file is kept next to it as .bak."))
            except OSError as e:
                self.message(_("Amiberry configuration"), _("Could not save: {err}").format(err=e.strerror or e),
                             Gtk.MessageType.WARNING)
        dlg.destroy()

    def on_state_click(self, tree, ev):
        """Right click on a savestate in the panel: menu with Play, Rename and Delete for that row."""
        if ev.button != 3:
            return False
        hit = tree.get_path_at_pos(int(ev.x), int(ev.y))
        if not hit:
            return False
        tree.get_selection().select_path(hit[0])
        path = self.st_store[hit[0]][2]
        menu = Gtk.Menu()
        for label, cb in ((_("Play from this savestate"), lambda: self.play_current(path)),
                          (_("Rename…"), lambda: self.rename_state(path)),
                          (_("Delete"), lambda: self.delete_state(path))):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda m, cb=cb: cb())
            menu.append(mi)
        menu.show_all()
        menu.popup_at_pointer(ev)
        return True

    def rename_state(self, path):
        d = Gtk.Dialog(title=_("Rename…"), transient_for=self, modal=True)
        d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Rename…"), Gtk.ResponseType.OK)
        e = Gtk.Entry(text=Path(path).stem, margin=12, activates_default=True, width_chars=40)
        d.get_content_area().add(e)
        d.set_default_response(Gtk.ResponseType.OK)
        d.show_all()
        name = e.get_text() if d.run() == Gtk.ResponseType.OK else ""
        d.destroy()
        if not name.strip():
            return
        try:
            core.rename_savestate(path, name)
        except (ValueError, FileExistsError, OSError) as err:
            self.message(_("Savestates"), _("Could not rename: {err}").format(
                err=_("that name is not allowed or already exists") if isinstance(err, (ValueError, FileExistsError))
                else (err.strerror or err)), Gtk.MessageType.WARNING)
        self.refresh_state_list(self.lib.get(self.current_id))

    def delete_state(self, path):
        m = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              text=_("Delete this savestate?"))
        m.format_secondary_text(Path(path).name)
        m.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Delete"), Gtk.ResponseType.YES)
        yes = m.run() == Gtk.ResponseType.YES
        m.destroy()
        if not yes:
            return
        try:
            core.delete_savestate(path)
        except OSError as err:
            self.message(_("Savestates"), _("Could not delete: {err}").format(err=err.strerror or err),
                         Gtk.MessageType.WARNING)
        self.refresh_state_list(self.lib.get(self.current_id))

    def advanced_scrape(self):
        """Edit the search text, look for candidates on OAGD and Libretro and pick the cover (and info) yourself."""
        g = self.lib.get(self.current_id) if self.current_id else None
        if g is None:
            return
        if self._scraping:
            self.say(_("A scrape is already running."))
            return
        gid = g["id"]
        query0 = core.strip_demo_word(core.lookup_title(g["title"], g["path"]), g["folder"])
        client = self.make_scraper()
        tmp = core.DATA_DIR / "pick"
        tmp.mkdir(parents=True, exist_ok=True)
        dlg = Gtk.Dialog(title=_("Advanced scrape"), transient_for=self, modal=True)
        dlg.set_default_size(760, 560)
        dlg.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Use selected"), Gtk.ResponseType.OK)
        ok_btn = dlg.get_widget_for_response(Gtk.ResponseType.OK)
        ok_btn.set_sensitive(False)
        box = dlg.get_content_area()
        box.set_spacing(8)
        row = Gtk.Box(spacing=6, margin=10)
        entry = Gtk.Entry(text=query0, hexpand=True)
        go = Gtk.Button.new_with_label(_("Search"))
        row.pack_start(entry, True, True, 0)
        row.pack_start(go, False, False, 0)
        box.pack_start(row, False, False, 0)
        info = Gtk.Label(xalign=0, margin_start=10, margin_end=10, wrap=True)
        info.get_style_context().add_class("dim-label")
        box.pack_start(info, False, False, 0)
        store = Gtk.ListStore(GdkPixbuf.Pixbuf, str, int)
        view = Gtk.IconView(model=store, margin=6)
        view.set_pixbuf_column(0)
        view.set_text_column(1)
        view.set_item_width(150)
        view.set_selection_mode(Gtk.SelectionMode.SINGLE)
        sw = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN, margin_start=10, margin_end=10, margin_bottom=10)
        sw.add(view)
        box.pack_start(sw, True, True, 1)
        state = {"cancel": threading.Event(), "n": 0, "found": []}   # found: candidates of the current search

        def add_row(cand, pb, k, idx):
            if state["n"] == k:
                store.append([pb, "%s\n(%s)" % (cand["name"], cand["source"]), idx])
            return False

        def finished(k):
            if state["n"] == k:
                info.set_text(_("{n} results.").format(n=len(store)) if len(store) else _("Nothing found."))
            return False

        def search(*_a):
            state["cancel"].set()
            state["cancel"] = cancel = threading.Event()
            state["n"] += 1
            k = state["n"]
            store.clear()
            found = state["found"] = []
            ok_btn.set_sensitive(False)
            info.set_text(_("Searching…"))
            text = entry.get_text().strip()

            def work():
                try:
                    for cand in client.candidates(text, cancel):
                        if cancel.is_set():
                            return
                        if not cand["cover_url"]:
                            continue
                        dest = client.download_image(cand["cover_url"], tmp / ("c%d_%d" % (k, len(found))))
                        if not dest or is_blank_cover(dest):
                            continue
                        pb = make_thumb(dest)
                        if pb is None:
                            continue
                        found.append(cand)
                        GLib.idle_add(add_row, cand, pb, k, len(found) - 1)
                except scraper.ScrapeError:
                    pass
                GLib.idle_add(finished, k)
            threading.Thread(target=work, daemon=True).start()

        go.connect("clicked", search)
        entry.connect("activate", search)
        view.connect("selection-changed", lambda v: ok_btn.set_sensitive(bool(v.get_selected_items())))
        view.connect("item-activated", lambda *a: dlg.response(Gtk.ResponseType.OK))
        dlg.show_all()
        search()
        chosen = None
        if dlg.run() == Gtk.ResponseType.OK:
            sel = view.get_selected_items()
            if sel:
                chosen = state["found"][store[sel[0]][2]]
        state["cancel"].set()
        dlg.destroy()
        if not chosen:
            return
        self.say(_("Downloading the chosen cover…"))

        def apply_choice():
            shot = client.download_image(chosen["cover_url"], core.SCREENS_DIR / str(gid))
            GLib.idle_add(self.advanced_done, gid, chosen["res"], shot)
        threading.Thread(target=apply_choice, daemon=True).start()

    def advanced_done(self, gid, res, shot):
        if shot:
            self.clear_custom_shots(gid, keep=shot)
        self.lib.apply_scrape(gid, res, False, self.settings["folder_categories"])
        self._tcache.pop(gid, None)
        self.refresh_list()
        self.say(_("Cover and info updated.") if shot else _("The cover could not be downloaded."))
        return False

    def scrape_current(self):
        if self.current_id:
            self.start_scrape([self.current_id])

    def json_dialog(self, title, save):
        dlg = Gtk.FileChooserDialog(title=title, parent=self,
                                    action=Gtk.FileChooserAction.SAVE if save else Gtk.FileChooserAction.OPEN)
        dlg.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save") if save else _("Open"), Gtk.ResponseType.OK)
        if save:
            dlg.set_current_name("denise-games.json")
            dlg.set_do_overwrite_confirmation(True)
        flt = Gtk.FileFilter()
        flt.set_name("JSON")
        flt.add_pattern("*.json")
        dlg.add_filter(flt)
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.OK else None
        dlg.destroy()
        return path

    def export_list(self):
        path = self.json_dialog(_("Save the game list"), True)
        if not path:
            return
        data = self.lib.export_game_list()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except OSError as e:
            self.say(_("Could not save: {err}").format(err=e.strerror or e))
            return
        self.say(_("Game list saved: {n} games").format(n=len(data)))

    def import_descriptions(self):
        path = self.json_dialog(_("Choose a file with descriptions"), False)
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):                       # [{"key": ..., "text": ...}]
                data = {e["key"]: e.get("text") or e.get("description") or "" for e in data}
            if not isinstance(data, dict):
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            self.say(_("This is not a valid descriptions file."))
            return
        m = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              text=_("Replace descriptions that games already have?"))
        m.format_secondary_text(_("Descriptions fetched from the database or typed by you are kept when you choose "
                                  "“Only empty”."))
        m.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Only empty"), Gtk.ResponseType.NO,
                      _("Replace all"), Gtk.ResponseType.YES)
        answer = m.run()
        m.destroy()
        if answer not in (Gtk.ResponseType.NO, Gtk.ResponseType.YES):
            return
        changed, unmatched = self.lib.import_descriptions(data, overwrite=answer == Gtk.ResponseType.YES)
        self.refresh_list()
        self.say(_("Descriptions added to {n} games; {m} entries matched no game.").format(n=changed, m=unmatched))

    def scrape_missing(self):
        ids = self.lib.unscraped_ids()
        if not ids:
            self.say(_("Every game has already been scraped."))
            return
        self.start_scrape(ids)

    def start_scrape(self, ids):
        if self._scraping:
            self.say(_("A scrape is already running."))
            return
        client = self.make_scraper()
        jobs = []
        skipped = 0
        for gid in ids:
            g = self.lib.get(gid)
            if g and core.no_scrape_game(g["folder"], g["path"], g["title"]):
                skipped += 1                                 # demos, beta demos and magazines: no online info
                continue
            if g:
                jobs.append({"id": gid, "path": g["path"], "title": g["title"], "query": core.strip_demo_word(core.lookup_title(g["title"], g["path"]), g["folder"]),
                             "has_shot": core.screenshot_for(g) is not None})
        if not jobs:
            if skipped:
                self.say(_("Demos, beta demos and magazines are not looked up online."))
            return
        overwrite, want_shot = self.settings["scrape_overwrite"], self.settings["scrape_screens"]
        self.cancel = threading.Event()
        cancel = self.cancel
        self._scraping = True
        self.b_stop.show()

        def worker():
            stats = {"ok": 0, "none": 0, "err": 0}
            fatal = None
            try:
                core.SCRAPE_LOG.write_text("Denise Station %s - scraping log\n" % core.VERSION)
            except OSError:
                pass
            for n, job in enumerate(jobs, 1):
                if cancel.is_set():
                    break
                GLib.idle_add(self.say, _("Scraping {n}/{total}: {title}")
                              .format(n=n, total=len(jobs), title=job["title"]))
                res, shot, outcome = None, None, "ok"
                try:
                    need_shot = want_shot and (overwrite or not job["has_shot"])
                    want_extras = bool(self.settings.get("scrape_extras", True))
                    res = client.lookup(job["path"], job["query"], want_screenshot=need_shot,
                                        prefer_cover=True, want_extras=want_extras)
                    if need_shot and res.cover_url:
                        shot = client.download_image(res.cover_url,
                                                     core.SCREENS_DIR / str(job["id"]))
                        if shot and is_blank_cover(shot):
                            # OAGD sends a plain white picture for unpublished covers: drop it, ask Libretro
                            try:
                                shot.unlink()
                            except OSError:
                                pass
                            shot = None
                            client._t("  OAGD cover is blank white -> trying Libretro")
                            try:
                                lib = client.libretro_lookup(job["query"])
                                if lib.cover_url:
                                    shot = client.download_image(lib.cover_url, core.SCREENS_DIR / str(job["id"]))
                                    if shot and is_blank_cover(shot):
                                        shot.unlink()
                                        shot = None
                            except (scraper.NotFound, scraper.ScrapeError, OSError):
                                shot = None
                        if shot:
                            self.clear_custom_shots(job["id"], keep=shot)
                    extras = 0
                    if want_extras and (res.extra_images or res.manual_url):
                        extras = self.download_extras(client, job["id"], res, overwrite, cancel)
                    if not (res.genre or res.year or res.developer or res.publisher
                            or shot or extras):
                        outcome = "none"          # e.g. a Libretro match whose image was not needed
                except scraper.NotFound:
                    outcome = "none"
                except scraper.ServerBusy as e:
                    fatal = e
                    try:
                        with open(core.SCRAPE_LOG, "a", encoding="utf-8") as lf:
                            lf.write("\n" + "\n".join(client.trace) + "\nResult: server unreachable (%s)\n" % e)
                    except OSError:
                        pass
                    break
                except scraper.ScrapeError:
                    outcome = "err"
                stats[outcome] += 1
                try:
                    with open(core.SCRAPE_LOG, "a", encoding="utf-8") as lf:
                        lf.write("\n" + "\n".join(client.trace) + "\nResult: %s\n" % outcome)
                except OSError:
                    pass
                GLib.idle_add(self.on_scraped, job["id"], res, outcome, overwrite)
            GLib.idle_add(self.scrape_done, stats, fatal, cancel.is_set())

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def download_extras(client, gid, res, overwrite, cancel):
        """Back cover and manual into extras/<id>/. Returns how many files came."""
        folder = core.EXTRAS_DIR / str(gid)
        got = 0
        for name, url in res.extra_images[:scraper.MAX_EXTRA_IMAGES]:
            if cancel.is_set():
                break
            if not overwrite and any((folder / (name + e)).exists() for e in (".png", ".jpg")):
                continue
            got += bool(client.download_image(url, folder / name))
        if res.manual_url and not cancel.is_set() and (overwrite or not (folder / "manual.pdf").exists()):
            got += bool(client.download_manual(res.manual_url, folder / "manual"))
        return got

    def on_scraped(self, gid, res, outcome, overwrite):
        if outcome == "ok":
            self.lib.apply_scrape(gid, res, overwrite, self.settings["folder_categories"])
        elif outcome == "none":
            self.lib.mark_unmatched(gid)
        if gid == self.current_id:
            self.show_detail(gid)
        return False

    def scrape_done(self, stats, fatal, stopped):
        self._scraping = False
        self.b_stop.hide()
        self.refresh_sidebar()
        self.refresh_list()
        fmt = _("Scraping stopped: {ok} updated, {none} not found, {err} failed.") if stopped \
            else _("Scraping finished: {ok} updated, {none} not found, {err} failed.")
        self.say(fmt.format(**stats))
        if fatal is not None:
            self.message(_("Scraping"), self.scrape_error_text(fatal), Gtk.MessageType.WARNING)
        return False

    # ------------------------------------------------------------ settings
    def open_settings(self):
        s = self.settings
        dlg = Gtk.Dialog(title=_("Settings"), transient_for=self, modal=True)
        dlg.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL,
                        _("Save and scan"), Gtk.ResponseType.OK)
        dlg.set_default_size(600, 560)
        area = dlg.get_content_area()
        area.set_border_width(12)
        nb = Gtk.Notebook()
        area.pack_start(nb, True, True, 0)

        def page(title):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
            nb.append_page(box, Gtk.Label(label=title))
            return box

        def new_grid(box, rows):
            grid = Gtk.Grid(column_spacing=10, row_spacing=8)
            for i, (lab, w) in enumerate(rows):
                w.set_hexpand(True)
                grid.attach(Gtk.Label(label=lab, xalign=1), 0, i, 1, 1)
                grid.attach(w, 1, i, 1, 1)
            box.pack_start(grid, False, False, 0)
            return grid

        # ---- Library tab
        lib_box = page(_("Library"))
        lib_box.pack_start(Gtk.Label(label=bold(_("Game folder")), use_markup=True, xalign=0), False, False, 0)
        lib_box.pack_start(Gtk.Label(label=str(core.GAMES_DIR), xalign=0, selectable=True), False, False, 0)

        e_ext = Gtk.Entry(text=s["extensions"])
        e_cmd = Gtk.Entry(text=s["launch_command"],
                          placeholder_text=_("empty = the downloaded Amiberry AppImage"))
        e_args = Gtk.Entry(text=s["global_args"],
                           placeholder_text=_("extra Amiberry arguments for every game"))
        c_shader = Gtk.ComboBoxText()
        c_shader.append("", _("(leave as in Amiberry)"))
        for name in core.SHADERS:
            c_shader.append(name, name)
        c_shader.set_active_id(s.get("amiberry_shader") or "")
        c_shader.set_tooltip_text(_("Written to amiberry.conf as shader=… when a game starts."))
        c_rec = Gtk.CheckButton(label=_("Scan sub-folders"), active=s["recurse"])
        c_cat = Gtk.CheckButton(label=_("Use the sub-folder name as the category (genre)"),
                                active=s["folder_categories"])
        c_cat.set_tooltip_text(_("Games in <game folder>/Platform/ get the category \"Platform\"; "
                                 "games directly in the game folder keep their own genre."))
        grid = new_grid(lib_box, ((_("File extensions"), e_ext), (_("Launch command"), e_cmd),
                                  (_("Global arguments"), e_args), (_("Amiberry shader"), c_shader)))
        grid.attach(c_rec, 1, 4, 1, 1)
        grid.attach(c_cat, 1, 5, 1, 1)

        tools = Gtk.Box(spacing=6)
        b_check = Gtk.Button(label=_("Check Amiberry"))
        b_data = Gtk.Button(label=_("Open Amiberry data folder"))

        def check(b):
            if core.appimage_path(self.settings):
                self.message(_("Amiberry found"), _("Amiberry {tag} (AppImage) in {path}").format(
                    tag=core.appimage_version(self.settings) or "?", path=core.appimage_dir(self.settings)), parent=dlg)
            elif (s["launch_command"] or "").strip():
                self.message(_("Amiberry found"), _("Custom launch command: {cmd}").format(cmd=s["launch_command"]),
                             parent=dlg)
            else:
                self.message(_("Amiberry not found"),
                             _("Use \"Download / update Amiberry\" to install it."), Gtk.MessageType.WARNING,
                             parent=dlg)

        def open_data(b):
            data = core.amiberry_data_dir(self.settings)
            if data.exists():
                subprocess.Popen(["xdg-open", str(data)])
            else:
                self.message(_("Not found"),
                             _("{path} does not exist yet.\nStart Amiberry once so it creates its "
                               "folders (Kickstart ROMs go there).").format(path=core.amiberry_data_dir(self.settings)),
                             parent=dlg)

        b_check.connect("clicked", check)
        b_data.connect("clicked", open_data)
        b_inst = Gtk.Button(label=_("Download / update Amiberry"))
        b_inst.set_tooltip_text(_("Installs Amiberry (AppImage) in a hidden .amiberry folder inside your first game "
                                  "folder."))
        b_inst.connect("clicked", lambda b: self.install_amiberry(dlg))
        tools.pack_start(b_inst, False, False, 0)
        b_whd = Gtk.Button(label=_("Update WHDLoad boot files"))
        b_whd.connect("clicked", lambda b: self.fetch_whdboot(dlg, ask=False))
        tools.pack_start(b_whd, False, False, 0)
        b_rom = Gtk.Button(label=_("Import Kickstart ROMs…"))
        b_rom.set_tooltip_text(_("Copies the ROM files (and rom.key) from a folder, e.g. Amiga Forever's Shared/rom, "
                                 "into Amiberry's roms folder."))
        b_rom.connect("clicked", lambda b: self.import_roms(dlg))
        tools.pack_start(b_rom, False, False, 0)
        tools.pack_start(b_check, False, False, 0)
        tools.pack_start(b_data, False, False, 0)
        b_recent = Gtk.Button(label=_("Clear recently played…"))
        b_recent.connect("clicked", lambda b: self.clear_recent_all(dlg))
        tools.pack_start(b_recent, False, False, 0)
        lib_box.pack_start(tools, False, False, 0)
        lib_box.pack_start(Gtk.Label(
            wrap=True, xalign=0, use_markup=True,
            label="<small>%s</small>" % GLib.markup_escape_text(
                _("On save, the sandboxed Amiberry is granted read-only access to each game folder. "
                  "Amiberry still needs Kickstart ROMs of your own."))), False, False, 0)

        # ---- Scraping tab
        sc_box = page(_("Scraping"))
        sc_box.pack_start(Gtk.Label(
            wrap=True, xalign=0, use_markup=True,
            label=GLib.markup_escape_text(
                _("Fetches year, developer, publisher, genre, rating and the box cover "
                  "from the Open Amiga Game Database (openretro.org). If a game is not found there, "
                  "the cover is taken from the Libretro thumbnails, matched by similar title. "
                  "No account or login is needed.")))
            , False, False, 0)
        c_shots = Gtk.CheckButton(label=_("Also download the box cover"), active=s["scrape_screens"])
        c_over = Gtk.CheckButton(label=_("Overwrite existing info (including the title)"),
                                 active=s["scrape_overwrite"])
        sc_box.pack_start(c_shots, False, False, 0)
        sc_box.pack_start(c_over, False, False, 0)
        c_auto = Gtk.CheckButton(label=_("After a WHDLoad download, fetch info and pictures for the new games"),
                                 active=s.get("scrape_after_download", True))
        sc_box.pack_start(c_auto, False, False, 0)
        c_extras = Gtk.CheckButton(label=_("Also download extra pictures and the manual (when available)"),
                                   active=s.get("scrape_extras", True))
        sc_box.pack_start(c_extras, False, False, 0)
        b_test = Gtk.Button(label=_("Test connection"))

        def test_conn(b):
            b.set_sensitive(False)

            def work():
                res = self.make_scraper().check()
                GLib.idle_add(done, res)

            def done(res):
                b.set_sensitive(True)
                lines = []
                for key, name in (("oagd", "Open Amiga Game Database"), ("libretro", "Libretro thumbnails")):
                    lines.append("%s: %s" % (name, _("OK") if not res.get(key)
                                             else _("not reachable ({detail})").format(detail=res[key])))
                bad = any(res.values())
                self.message(_("Connection test"), "\n".join(lines),
                             Gtk.MessageType.WARNING if bad else Gtk.MessageType.INFO, parent=dlg)
                return False

            threading.Thread(target=work, daemon=True).start()

        b_test.connect("clicked", test_conn)
        sc_box.pack_start(b_test, False, False, 0)

        # ---- General tab
        gen_box = page(_("General"))
        c_lang = Gtk.ComboBoxText()
        c_lang.append("auto", _("Automatic (system language)"))
        for code, name in i18n.available_languages().items():
            c_lang.append(code, name)
        c_lang.set_active_id(s["language"] if s["language"] in i18n.available_languages() else "auto")
        c_look = Gtk.ComboBoxText()
        c_look.append("auto", _("Automatic (follow the system)"))
        c_look.append("light", _("Light"))
        c_look.append("dark", _("Dark"))
        c_look.set_active_id(s.get("appearance", "auto") if s.get("appearance") in ("light", "dark") else "auto")
        c_look.connect("changed", lambda w: self.appearance.set_mode(w.get_active_id()))   # live preview
        e_lha = Gtk.Entry(text=s.get("lha_folder", ""), placeholder_text=str(core.amiberry_lha_folder({})))
        e_lha.set_hexpand(True)
        new_grid(gen_box, ((_("Language"), c_lang), (_("Appearance"), c_look),
                           (_("Amiberry LHA folder"), e_lha)))
        c_warn = Gtk.CheckButton(label=_("Show the copyright notice before WHDLoad downloads"),
                                 active=s.get("whdload_warning", True))
        gen_box.pack_start(c_warn, False, False, 0)
        e_src = Gtk.Entry(text=s.get("whdload_source", ""), placeholder_text="https://")
        e_src.set_hexpand(True)
        new_grid(gen_box, ((_("Retroplay repository"), e_src),))
        c_upd = Gtk.CheckButton(label=_("Check at start-up whether newer WHDLoad archives are available"),
                                active=s.get("check_updates", True))
        gen_box.pack_start(c_upd, False, False, 0)
        gen_box.pack_start(Gtk.Label(
            wrap=True, xalign=0, use_markup=True,
            label="<small>%s</small>" % GLib.markup_escape_text(
                _("The language change takes effect after restarting Denise Station."))), False, False, 0)

        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            old_lang = s["language"]
            s["extensions"] = e_ext.get_text().strip() or core.DEFAULT_SETTINGS["extensions"]
            s["launch_command"] = e_cmd.get_text().strip()
            s["global_args"] = e_args.get_text().strip()
            s["amiberry_shader"] = c_shader.get_active_id() or ""
            s["recurse"] = c_rec.get_active()
            s["folder_categories"] = c_cat.get_active()
            s["scrape_screens"] = c_shots.get_active()
            s["scrape_overwrite"] = c_over.get_active()
            s["scrape_after_download"] = c_auto.get_active()
            s["scrape_extras"] = c_extras.get_active()
            s["language"] = c_lang.get_active_id() or "auto"
            s["appearance"] = c_look.get_active_id() or "auto"
            s["lha_folder"] = e_lha.get_text().strip()
            s["whdload_warning"] = c_warn.get_active()
            s["whdload_source"] = whdload_dialog.rp.validate_source(e_src.get_text()) if e_src.get_text().strip() else ""
            s["check_updates"] = c_upd.get_active()
            core.save_settings(s)
            lang_changed = s["language"] != old_lang
            dlg.destroy()
            if lang_changed:
                self.message(_("Language"), _("Restart Denise Station to apply the new language."))
            self.start_scan()
        else:
            self.appearance.set_mode(self.settings.get("appearance", "auto"))   # undo the live preview
            dlg.destroy()

    # --------------------------------------------------------------- misc
    def message(self, title, text, kind=Gtk.MessageType.INFO, parent=None):
        d = Gtk.MessageDialog(transient_for=parent or self, modal=True, message_type=kind,
                              buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(text)
        d.run()
        d.destroy()

    def restore_titles(self):
        d = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                              buttons=Gtk.ButtonsType.YES_NO, text=_("Restore titles?"))
        d.format_secondary_text(_("All game titles will be replaced by their file names (without the "
                                  "extension, with _ shown as a space). Titles you edited or that were "
                                  "fetched from a database will be lost."))
        yes = d.run() == Gtk.ResponseType.YES
        d.destroy()
        if yes:
            n = self.lib.reset_titles()
            self.refresh_sidebar()
            self.refresh_list()
            self.say(_("Titles restored: {n}").format(n=n))

    def show_shortcuts(self):
        sections = ((_("Denise Station"), (("Ctrl+F", _("Search")),
                                           ("Esc", _("Clear the search, then hide the field")),
                                           ("Ctrl+R", _("Random game")),
                                           ("F5", _("Rescan the library")),
                                           ("F11 / Alt+Enter", _("Full screen (this program)")))),
                    (_("In Amiberry (while a game runs)"), (("F11", _("Toggle full screen / window")),
                                                            ("Esc", _("Quit the game and Amiberry")),
                                                            ("F12", _("Amiberry menu")))))
        d = Gtk.Dialog(title=_("Keyboard shortcuts"), transient_for=self, modal=True)
        d.add_button(_("Close"), Gtk.ResponseType.CLOSE)
        grid = Gtk.Grid(column_spacing=18, row_spacing=6, margin=16)
        r = 0
        for title, rows in sections:
            lb = Gtk.Label(xalign=0)
            lb.set_markup("<b>%s</b>" % GLib.markup_escape_text(title))
            grid.attach(lb, 0, r, 2, 1)
            r += 1
            for key, text in rows:
                k = Gtk.Label(label=key, xalign=0)
                k.set_markup("<tt>%s</tt>" % GLib.markup_escape_text(key))
                grid.attach(k, 0, r, 1, 1)
                grid.attach(Gtk.Label(label=text, xalign=0), 1, r, 1, 1)
                r += 1
            r += 1
        d.get_content_area().add(grid)
        d.show_all()
        d.run()
        d.destroy()

    def show_about(self):
        d = Gtk.AboutDialog(transient_for=self, modal=True)
        d.set_program_name("Denise Station")
        d.set_version(core.VERSION)
        d.set_comments(_("A clean-room Amiga game launcher that starts games in Amiberry (AppImage)."))
        d.set_logo_icon_name("denise-station" if Gtk.IconTheme.get_default().has_icon("denise-station") else "applications-games")
        d.set_copyright(_("© Elyon") + "\n" + _("Amiga and Commodore are trademarks of their respective owners. Denise Station is an independent project, not affiliated with them."))
        d.set_authors(["Elyon <mateusz.zimniak@protonmail.com>"])
        d.set_license_type(Gtk.License.GPL_2_0)
        d.add_button(_("Buy Amiga Forever"), 100)
        try:
            ab = d.get_action_area()
            ab.set_layout(Gtk.ButtonBoxStyle.CENTER)
            ab.set_halign(Gtk.Align.CENTER)
            ab.set_hexpand(True)
        except Exception:
            pass
        while True:
            r = d.run()
            if r != 100:
                break
            try:
                subprocess.Popen(["xdg-open", "https://www.amigaforever.com/"])
            except OSError:
                pass
        d.destroy()


class App(Gtk.Application):
    def __init__(self):
        GLib.set_prgname("local.denisestation.App")          # window class = application id = .desktop file name
        GLib.set_application_name("Denise Station")
        Gtk.Window.set_default_icon_name("denise-station")
        super().__init__(application_id="local.denisestation.App", flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        win = self.props.active_window or MainWindow(self)
        win.present()


if __name__ == "__main__":
    if "--version" in sys.argv:
        print("Denise Station", core.VERSION)
        print("program:", os.path.dirname(os.path.abspath(__file__)))
        print("library:", core.DB_PATH)
        sys.exit(0)
    sys.exit(App().run(sys.argv))
