"""Denise Station - the 'WHDLoad download' window (GTK3). The logic lives in denise_retroplay.py."""
import os
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

import denise_core as core  # noqa: E402
import denise_retroplay as rp  # noqa: E402
from denise_i18n import _  # noqa: E402


def human_size(n):
    if not n:
        return ""
    return "%.0f KB" % (n / 1024) if n < 1024 * 1024 else "%.1f MB" % (n / 1024 / 1024)


def confirm_rights(win, parent):
    """Copyright reminder before a download. Returns True to go on; 'don't show again' is saved in the settings."""
    if not win.settings.get("whdload_warning", True):
        return True
    m = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.WARNING,
                          text=_("Copyright notice"))
    m.format_secondary_text(
        _("Many Amiga games and magazines are still protected by copyright. Download only files you have the right to "
          "use: games you own, works released as freeware or with the rights holder's permission, or files that are "
          "legal to use where you live. You are responsible for having that right."))
    m.add_button(_("Cancel"), Gtk.ResponseType.CANCEL)
    m.add_button(_("I have the right to use these files"), Gtk.ResponseType.OK)
    m.set_default_response(Gtk.ResponseType.CANCEL)
    chk = Gtk.CheckButton(label=_("Do not show this notice again"))
    m.get_message_area().pack_start(chk, False, False, 6)
    chk.show()
    ok = m.run() == Gtk.ResponseType.OK
    if ok and chk.get_active():
        win.settings["whdload_warning"] = False
        core.save_settings(win.settings)
    m.destroy()
    return ok


def ask_source(win, parent):
    """Ask for the address of the pack directory. Returns the validated address or '' when cancelled."""
    m = Gtk.Dialog(title=_("Retroplay repository"), transient_for=parent, modal=True)
    m.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.OK)
    box = m.get_content_area()
    box.set_spacing(8)
    box.set_border_width(12)
    box.pack_start(Gtk.Label(
        wrap=True, xalign=0, max_width_chars=60,
        label=_("Denise Station does not come with a Retroplay repository address. Enter the address (https://…) of the "
                "Retroplay repository you want to use. You are responsible for the repository you choose and for having the "
                "right to use the files it offers.")), False, False, 0)
    entry = Gtk.Entry(text=win.settings.get("whdload_source", ""), placeholder_text="https://")
    entry.set_activates_default(True)
    box.pack_start(entry, False, False, 0)
    err = Gtk.Label(xalign=0)
    box.pack_start(err, False, False, 0)
    m.set_default_response(Gtk.ResponseType.OK)
    m.show_all()
    result = ""
    while m.run() == Gtk.ResponseType.OK:
        result = rp.validate_source(entry.get_text())
        if result:
            win.settings["whdload_source"] = result
            core.save_settings(win.settings)
            break
        err.set_text(_("Please enter a full https:// address without a user name or password."))
    m.destroy()
    return result


def open_dialog(win):
    """Modal window: list the Retroplay WHDLoad archives, tick some, download them into a game folder."""
    source = rp.validate_source(win.settings.get("whdload_source", "")) or ask_source(win, win)
    if not source:
        return
    client = rp.Retroplay(source, cache_dir=core.DATA_DIR)
    local_paths = [r["path"] for r in win.lib.db.execute("SELECT path FROM games WHERE missing = 0")]
    have = {rp.local_key(p) for p in local_paths}
    # also the archives that lie in the game folders but are not (yet) in the library, e.g. before a rescan
    for root in {str(f) for f in win.settings.get("folders", []) if f}:
        for dirpath, dirnames, files in os.walk(os.path.expanduser(root)):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            have.update(rp.local_key(f) for f in files if f.lower().endswith((".lha", ".lzx", ".zip")))
    updates = {}                                  # remote file name -> (local path, old version, new version)
    entries = []
    state = {"busy": False, "cancel": threading.Event(), "downloaded": 0, "loading": False, "dest": ""}

    dlg = Gtk.Dialog(title=_("WHDLoad download"), transient_for=win, modal=True)
    dlg.set_default_size(980, 700)
    dlg.add_button(_("Close"), Gtk.ResponseType.CLOSE)
    box = dlg.get_content_area()
    box.set_spacing(8)
    for side in ("start", "end", "top", "bottom"):
        getattr(box, "set_margin_" + side)(12)

    box.pack_start(Gtk.Label(
        wrap=True, xalign=0,
        label=_("Downloads ready-made WHDLoad archives from the Retroplay packs on Turran's server, one at a time, "
                "and checks each file against its CRC32. Only download what you are allowed to use.")),
        False, False, 0)

    # ---- filters
    row1 = Gtk.Box(spacing=8)
    search = Gtk.SearchEntry(placeholder_text=_("Search"))
    c_pack = Gtk.ComboBoxText()
    off = set()                                   # filter properties that are unticked
    attrs = {}                                    # entry index -> properties
    b_filter = Gtk.MenuButton(label=_("Filters"))
    boxes = {}                                    # property -> check button
    chk_missing = Gtk.CheckButton(label=_("Only games I don't have"))
    b_refresh = Gtk.Button(label=_("Refresh list"))
    row1.pack_start(search, True, True, 0)
    for w in (c_pack, b_filter, chk_missing, b_refresh):
        row1.pack_start(w, False, False, 0)
    box.pack_start(row1, False, False, 0)

    # filter popover: System / Extras / Language check boxes like the original WHDLoad Download Tool
    bulk = [False]

    def on_filter_toggled(cb, key):
        (off.discard if cb.get_active() else off.add)(key)
        b_filter.set_label(_("Filters") + (" (%d)" % len(off) if off else ""))
        if not bulk[0]:
            apply_filters()

    def apply_filters():
        """Refilter, and untick what the filters now hide, so that hidden archives are never downloaded."""
        for row in store:
            if row[0] and not visible(store, row.iter, None):
                row[0] = False
        filt.refilter()
        update_count()

    def trash_hidden(_b):
        """Move the archives in your library that the unticked filters describe to the trash (asks first)."""
        if not off:
            return
        rows = win.lib.db.execute("SELECT path FROM games WHERE missing = 0").fetchall()
        hits = sorted(r["path"] for r in rows
                      if os.path.isfile(r["path"]) and not rp.attributes(os.path.basename(r["path"])).isdisjoint(off))
        pop.popdown()
        if not hits:
            say(_("None of your files match the unticked filters."))
            return
        d = Gtk.MessageDialog(transient_for=dlg, modal=True, message_type=Gtk.MessageType.WARNING,
                              text=_("Move {n} file(s) to the trash?").format(n=len(hits)))
        shown = "\n".join(os.path.basename(h) for h in hits[:15])
        more = "\n…" + _("and {n} more").format(n=len(hits) - 15) if len(hits) > 15 else ""
        d.format_secondary_text(_("These files in your library match the filters you unticked. They go to the "
                                  "trash, so you can restore them from there.") + "\n\n" + shown + more)
        d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Move to trash"), Gtk.ResponseType.YES)
        d.set_default_response(Gtk.ResponseType.CANCEL)
        yes = d.run() == Gtk.ResponseType.YES
        d.destroy()
        if not yes:
            return
        done, failed = 0, []
        for h in hits:
            try:
                Gio.File.new_for_path(h).trash(None)
                done += 1
            except GLib.Error as e:
                failed.append("%s: %s" % (os.path.basename(h), e.message))
        say(_("{n} file(s) moved to the trash.").format(n=done))
        if failed:
            win.message(_("Move to trash"), _("Could not move:") + "\n" + "\n".join(failed[:10]),
                        Gtk.MessageType.WARNING, parent=dlg)
        win.start_scan()                              # the library forgets the removed files
        have.clear()
        have.update(rp.local_key(p) for p in (r["path"] for r in win.lib.db.execute(
            "SELECT path FROM games WHERE missing = 0")) if os.path.exists(p))
        for row in store:
            if entries and rp.local_key(entries[row[6]].file) not in have and row[5] == _("In your library"):
                row[5] = ""

    def set_all(on):
        bulk[0] = True                                # one refilter for the whole batch, not one per box
        for cb in boxes.values():
            cb.set_active(on)
        bulk[0] = False
        apply_filters()

    pop = Gtk.Popover()
    pgrid = Gtk.Grid(column_spacing=22, row_spacing=4, margin=12)
    for col, (title, keys) in enumerate(((_("System"), rp.FILTER_SYSTEM), (_("Extras"), rp.FILTER_EXTRAS),
                                         (_("Language"), rp.FILTER_LANGS))):
        head = Gtk.Label(xalign=0)
        head.set_markup("<b>%s</b>" % GLib.markup_escape_text(title))
        pgrid.attach(head, col, 0, 1, 1)
        for row, key in enumerate(keys, 1):
            cb = Gtk.CheckButton(label=_(key) if key in rp.FILTER_LANGS else key, active=True)
            cb.connect("toggled", on_filter_toggled, key)
            boxes[key] = cb
            pgrid.attach(cb, col, row, 1, 1)
    bar_f = Gtk.Box(spacing=6, halign=Gtk.Align.END)
    b_clear_f = Gtk.Button(label=_("Clear filter"))
    b_reset_f = Gtk.Button(label=_("Reset filter"))
    b_clear_f.set_tooltip_text(_("Untick everything"))
    b_reset_f.set_tooltip_text(_("Tick everything: show all archives"))
    b_clear_f.connect("clicked", lambda b: set_all(False))
    b_reset_f.connect("clicked", lambda b: set_all(True))
    bar_f.pack_start(b_clear_f, False, False, 0)
    bar_f.pack_start(b_reset_f, False, False, 0)
    pgrid.attach(bar_f, 0, 15, 3, 1)
    b_trash = Gtk.Button(label=_("Move files hidden by the filter to the trash…"))
    b_trash.get_style_context().add_class("destructive-action")
    b_trash.connect("clicked", trash_hidden)
    pgrid.attach(b_trash, 0, 16, 3, 1)
    pop.add(pgrid)
    pgrid.show_all()
    b_filter.set_popover(pop)

    # ---- list
    store = Gtk.ListStore(bool, str, str, str, str, str, int)    # ticked, name, tags, size, pack, status, index
    filt = store.filter_new()

    def visible(model, it, _data):
        row = model[it]
        e = entries[row[6]]
        pack = c_pack.get_active_id()
        if pack and pack != "*" and e.pack != pack:
            return False
        if off and not attrs.setdefault(row[6], rp.attributes(e.file)).isdisjoint(off):
            return False
        if chk_missing.get_active() and rp.local_key(e.file) in have:
            return False
        q = search.get_text().lower().split()
        return all(w in (e.name + " " + e.file).lower() for w in q)

    filt.set_visible_func(visible)
    view = Gtk.TreeView(model=filt)
    toggle = Gtk.CellRendererToggle()

    def on_toggle(_cell, path):
        child = filt.convert_path_to_child_path(Gtk.TreePath(path))
        store[child][0] = not store[child][0]
        update_count()

    toggle.connect("toggled", on_toggle)
    view.append_column(Gtk.TreeViewColumn("", toggle, active=0))
    for i, title in ((1, _("Name")), (2, _("Tags")), (3, _("Size")), (4, _("Pack")), (5, _("Status"))):
        col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=i)
        col.set_resizable(True)
        if i == 1:
            col.set_expand(True)
        view.append_column(col)
    scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    scroll.add(view)
    box.pack_start(scroll, True, True, 0)

    # ---- selection helpers and curated lists
    row2 = Gtk.Box(spacing=8)
    b_upd = Gtk.Button(label=_("Tick updates"))
    b_all = Gtk.Button(label=_("Tick all shown"))
    b_none = Gtk.Button(label=_("Untick all"))
    b_save = Gtk.Button(label=_("Save list…"))
    b_load = Gtk.Button(label=_("Load list…"))
    for w in (b_upd, b_all, b_none, b_save, b_load):
        row2.pack_start(w, False, False, 0)
    box.pack_start(row2, False, False, 0)

    # ---- destination
    grid = Gtk.Grid(column_spacing=8, row_spacing=6)
    c_folder = Gtk.ComboBoxText()                      # the game folder is fixed
    c_folder.append_text(str(core.GAMES_DIR))
    c_folder.set_active(0)
    c_folder.set_sensitive(False)
    e_sub = Gtk.Entry(placeholder_text=_("optional, e.g. Platform"))
    e_sub.set_hexpand(True)
    c_setup = Gtk.ComboBoxText()
    c_setup.append("pack", _("Sorted by pack (Games, Demos…)"))
    c_setup.append("pack_letter", _("Sorted by pack, then A, B, C…"))
    c_setup.append("flat", _("All files in one folder"))
    c_setup.append("letter", _("Sorted into A, B, C… folders"))
    c_setup.set_active_id(win.settings.get("whdload_setup") if win.settings.get("whdload_setup") in
                          ("pack", "pack_letter", "flat", "letter") else "pack")      # default: by pack
    for r, (label, w) in enumerate(((_("Save to folder"), c_folder), (_("Category sub-folder"), e_sub),
                                    (_("Folder layout"), c_setup))):
        grid.attach(Gtk.Label(label=label, xalign=0), 0, r, 1, 1)
        grid.attach(w, 1, r, 1, 1)
    box.pack_start(grid, False, False, 0)

    bottom = Gtk.Box(spacing=8)
    lbl = Gtk.Label(xalign=0, wrap=True, wrap_mode=2, max_width_chars=1)     # own full-width row, wraps in a narrow window
    bar = Gtk.ProgressBar()
    bar.set_size_request(180, -1)
    b_stop = Gtk.Button(label=_("Stop"))
    b_stop.set_sensitive(False)
    b_go = Gtk.Button(label=_("Download selected"))
    b_go.get_style_context().add_class("suggested-action")
    bar.set_hexpand(True)
    for w in (bar, b_stop, b_go):
        bottom.pack_start(w, w is bar, w is bar, 0)
    box.pack_start(lbl, False, False, 0)
    box.pack_start(bottom, False, False, 0)

    def say(text):
        lbl.set_text(text)
        return False

    def update_count():
        n = sum(1 for r in store if r[0])
        b_go.set_label(_("Download selected") + (" (%d)" % n if n else ""))
        b_go.set_sensitive(bool(n) and not state["busy"])

    def rebuild_filters():
        c_pack.remove_all()
        c_pack.append("*", _("All packs"))
        for p in sorted({e.pack for e in entries}, key=lambda x: rp.pack_label(x).lower()):
            c_pack.append(p, rp.pack_label(p))
        c_pack.set_active_id("*")
        attrs.clear()

    def fill(items):
        state["loading"] = False
        b_stop.set_sensitive(False)
        entries[:] = items
        store.clear()
        rebuild_filters()
        updates.clear()
        updates.update(rp.find_updates(local_paths, items))
        win.set_update_count(len(updates))
        for i, e in enumerate(items):
            if e.file in updates:
                u = updates[e.file]
                status = _("Update: {old} → {new}").format(old=rp.fmt_version(u[1]), new=rp.fmt_version(u[2]))
            else:
                status = _("In your library") if rp.local_key(e.file) in have else ""
            store.append([False, e.name, " ".join(e.tags), human_size(e.size), rp.pack_label(e.pack), status, i])
        filt.refilter()
        say(_("{count} archives available.").format(count=len(items)))
        b_refresh.set_sensitive(True)
        update_count()
        return False

    def fail_list(msg):
        state["loading"] = False
        b_stop.set_sensitive(False)
        b_refresh.set_sensitive(True)
        say(msg if msg == _("Stopped.") else _("Could not load the list: {error}").format(error=msg))
        return False

    def load(refresh=False):
        if state["loading"]:
            return
        state["loading"] = True
        state["cancel"] = cancel = threading.Event()
        b_refresh.set_sensitive(False)
        b_stop.set_sensitive(True)
        say(_("Loading the list…"))

        def worker():
            try:
                items = client.index(refresh, cancel,
                                     lambda s: GLib.idle_add(say, _("Reading the server: {path}").format(path=s)))
            except rp.DownloadError as e:
                GLib.idle_add(fail_list, _("Stopped.") if cancel.is_set() else str(e))
                return
            GLib.idle_add(fill, items)

        threading.Thread(target=worker, daemon=True).start()

    def go(_b):
        picked = [entries[r[6]] for r in store if r[0]]
        if not picked or state["busy"]:
            return
        if not confirm_rights(win, dlg):
            return
        sub = e_sub.get_text().strip().strip("/\\")
        if any(p == ".." for p in sub.replace("\\", "/").split("/")):
            say(_("The sub-folder name is not valid."))
            return
        state["dest"] = c_folder.get_active_text()
        root = Path(c_folder.get_active_text()) / sub if sub else Path(c_folder.get_active_text())
        setup = c_setup.get_active_id()
        win.settings["whdload_setup"] = setup
        core.save_settings(win.settings)
        state.update(busy=True, downloaded=0)
        state["cancel"] = cancel = threading.Event()
        b_go.set_sensitive(False)
        b_stop.set_sensitive(True)

        def worker():
            ok, failed, skipped = 0, [], 0
            for n, e in enumerate(picked, 1):
                if cancel.is_set():
                    break
                target = root / rp.folder_for(e, setup)   # '' -> root itself
                if (target / e.file).exists() and (not e.size or (target / e.file).stat().st_size == e.size):
                    skipped += 1
                    GLib.idle_add(mark, e.file, _("Already there"))
                    continue
                GLib.idle_add(say, _("Downloading {n}/{total}: {name}").format(n=n, total=len(picked), name=e.name))
                GLib.idle_add(bar.set_fraction, 0.0)
                try:
                    client.download(e, target, cancel,
                                    lambda got, total: GLib.idle_add(bar.set_fraction, (got / total) if total else 0.0))
                    ok += 1
                    if e.file in updates:                 # keep the outdated file out of the library
                        rp.replace_old(updates[e.file][0], target / e.file)
                    GLib.idle_add(mark, e.file, _("Downloaded"))
                except (rp.DownloadError, OSError) as err:
                    if cancel.is_set():
                        break
                    failed.append((e.name, str(err)))
                    GLib.idle_add(mark, e.file, _("Failed"))
            GLib.idle_add(finished, ok, failed, skipped, cancel.is_set())

        threading.Thread(target=worker, daemon=True).start()

    def mark(file, status):
        for r in store:
            if entries[r[6]].file == file:
                r[5] = status
                if status in (_("Downloaded"), _("Already there")):
                    r[0] = False
        return False

    def finished(ok, failed, skipped, stopped):
        state["busy"] = False
        state["downloaded"] += ok
        b_stop.set_sensitive(False)
        bar.set_fraction(0.0)
        msg = _("{ok} downloaded, {skipped} already there, {failed} failed.").format(
            ok=ok, skipped=skipped, failed=len(failed))
        if stopped:
            msg = _("Stopped.") + " " + msg
        if failed:
            msg += " " + failed[0][0] + ": " + failed[0][1]
        say(msg)
        update_count()
        return False

    def tick_shown(_b):
        for r in filt:
            store[filt.convert_iter_to_child_iter(r.iter)][0] = True
        update_count()

    def tick_updates(_b):
        n = 0
        for r in store:
            hit = entries[r[6]].file in updates
            if hit:
                r[0] = True
                n += 1
        update_count()
        say(_("{count} updates ticked.").format(count=n) if n else _("All your WHDLoad archives are up to date."))

    def untick(_b):
        for r in store:
            r[0] = False
        update_count()

    def save_list(_b):
        files = [entries[r[6]].file for r in store if r[0]]
        if not files:
            say(_("Tick some archives first."))
            return
        d = Gtk.FileChooserDialog(title=_("Save list…"), transient_for=dlg, action=Gtk.FileChooserAction.SAVE)
        d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Save"), Gtk.ResponseType.OK)
        d.set_current_name("whdload-list.txt")
        d.set_do_overwrite_confirmation(True)
        if d.run() == Gtk.ResponseType.OK:
            try:
                Path(d.get_filename()).write_text(
                    "# Denise Station - list of WHDLoad archives (one file name per line)\n" + "\n".join(files) + "\n",
                    encoding="utf-8")
                say(_("List saved ({count} archives).").format(count=len(files)))
            except OSError as err:
                say(str(err))
        d.destroy()

    def load_list(_b):
        d = Gtk.FileChooserDialog(title=_("Load list…"), transient_for=dlg, action=Gtk.FileChooserAction.OPEN)
        d.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Open"), Gtk.ResponseType.OK)
        if d.run() == Gtk.ResponseType.OK:
            try:
                wanted = {ln.strip().lower() for ln in Path(d.get_filename()).read_text(encoding="utf-8").splitlines()
                          if ln.strip() and not ln.startswith("#")}
            except (OSError, UnicodeDecodeError) as err:
                wanted = set()
                say(str(err))
            n = 0
            for r in store:
                hit = entries[r[6]].file.lower() in wanted
                r[0] = hit
                n += hit
            update_count()
            say(_("{count} archives from the list ticked.").format(count=n))
        d.destroy()

    b_go.connect("clicked", go)
    b_stop.connect("clicked", lambda _b: state["cancel"].set())
    b_refresh.connect("clicked", lambda _b: load(True))
    b_upd.connect("clicked", tick_updates)
    b_all.connect("clicked", tick_shown)
    b_none.connect("clicked", untick)
    b_save.connect("clicked", save_list)
    b_load.connect("clicked", load_list)
    c_pack.connect("changed", lambda *_a: apply_filters())
    chk_missing.connect("toggled", lambda *_a: apply_filters())
    search.connect("search-changed", lambda *_a: filt.refilter())

    dlg.show_all()
    load()
    dlg.run()
    state["cancel"].set()
    dlg.destroy()
    if state["downloaded"]:
        if state["dest"] and state["dest"] not in win.settings["folders"]:
            win.settings["folders"].append(state["dest"])   # so that the new files show up in the library
            core.save_settings(win.settings)
        win.scan_and_scrape()
