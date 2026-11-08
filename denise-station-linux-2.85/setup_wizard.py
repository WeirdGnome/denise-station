"""Denise Station - first-run wizard (GTK3): download Amiberry and WHDLoad, import the Kickstart ROMs, open
Amiberry once so it can scan its paths."""
import subprocess
import threading

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

import denise_core as core  # noqa: E402
import denise_retroplay as rp  # noqa: E402
from denise_i18n import _  # noqa: E402


def page_box():
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=24)
    return box


def label(text, bold=False):
    lb = Gtk.Label(xalign=0, wrap=True, selectable=False)
    if bold:
        lb.set_markup("<b>%s</b>" % GLib.markup_escape_text(text))
    else:
        lb.set_text(text)
    lb.set_max_width_chars(70)
    return lb


def is_done(settings) -> bool:
    """Everything the wizard sets up is in place already (Amiberry, WHDLoad and at least one Kickstart ROM)."""
    if (settings.get("launch_command") or "").strip():
        return True                                       # a custom launcher: the user manages Amiberry themselves
    rd = core.roms_dir(settings)
    has_roms = rd.is_dir() and any(p.suffix.lower() in core.ROM_EXTS for p in rd.iterdir())
    return bool(core.appimage_path(settings)) and not core.whdboot_missing(settings) and has_roms


def run(win):
    """Show the wizard (modal) over the main window; returns when it is closed."""
    s = win.settings
    state = {"installed": False, "busy": False, "scanned": False, "open_dl": True, "result": None}
    asst = Gtk.Assistant(transient_for=win, modal=True)
    asst.set_title(_("Welcome to Denise Station"))
    asst.set_default_size(640, 420)

    # ---- 1. welcome
    p1 = page_box()
    p1.pack_start(label(_("Welcome to Denise Station"), True), False, False, 0)
    p1.pack_start(label(_("This short setup downloads Amiberry (the emulator) and the files it needs, copies your "
                          "Kickstart ROMs, and opens Amiberry once so that it can find them.")), False, False, 0)
    p1.pack_start(label(_("Your games live in {folder}. Amiberry and its files are kept in the hidden .amiberry "
                          "folder inside it.").format(folder=core.GAMES_DIR)), False, False, 0)
    asst.append_page(p1)
    asst.set_page_type(p1, Gtk.AssistantPageType.INTRO)
    asst.set_page_title(p1, _("Welcome"))
    asst.set_page_complete(p1, True)

    # ---- 2. download
    p2 = page_box()
    l2 = label(_("Press Download to fetch Amiberry and the WHDLoad boot files."))
    bar = Gtk.ProgressBar()
    b_dl = Gtk.Button(label=_("Download"), halign=Gtk.Align.START)
    p2.pack_start(l2, False, False, 0)
    p2.pack_start(bar, False, False, 0)
    p2.pack_start(b_dl, False, False, 0)
    asst.append_page(p2)
    asst.set_page_type(p2, Gtk.AssistantPageType.CONTENT)
    asst.set_page_title(p2, _("Amiberry"))

    def show_prog(done, total):
        bar.set_fraction(done / total if total else 0.0)
        l2.set_text(_("Downloading Amiberry… {mb} MB").format(mb=done // 1048576))
        return False

    def finish_dl(ok, msg):
        state["busy"] = False
        pulse_id = state.pop("pulse", None)
        if pulse_id:
            GLib.source_remove(pulse_id)
        if ok:
            bar.set_fraction(1.0)
            l2.set_text(_("Amiberry {tag} and the WHDLoad boot files are installed.").format(tag=msg))
            b_dl.set_visible(False)
            state["installed"] = True
            asst.set_page_complete(p2, True)
        else:
            bar.set_fraction(0.0)
            l2.set_text(_("Could not download: {err}").format(err=msg))
            b_dl.set_label(_("Try again"))
            b_dl.set_sensitive(True)
        return False

    def worker():
        try:
            tag = core.appimage_version(s) if core.appimage_path(s) else ""
            if not tag:
                tag = core.download_appimage(s, lambda d, t: GLib.idle_add(show_prog, d, t))
            if core.whdboot_missing(s):
                GLib.idle_add(l2.set_text, _("Downloading WHDLoad and the boot files…"))
                state["pulse"] = GLib.timeout_add(150, lambda: bar.pulse() or True)
                ok, out = core.download_whdboot(s)
                if not ok:
                    GLib.idle_add(finish_dl, False, out or _("WHDLoad could not be downloaded."))
                    return
            GLib.idle_add(finish_dl, True, tag)
        except Exception as e:                           # network down, disk full...
            GLib.idle_add(finish_dl, False, str(e))

    def start_dl(_b):
        if state["busy"]:
            return
        state["busy"] = True
        b_dl.set_sensitive(False)
        threading.Thread(target=worker, daemon=True).start()

    b_dl.connect("clicked", start_dl)

    # ---- 3. ROMs
    p3 = page_box()
    p3.pack_start(label(_("Kickstart ROMs"), True), False, False, 0)
    p3.pack_start(label(_("Amiberry needs Kickstart ROMs you own. Choose the folder that holds them, for example "
                          "Amiga Forever's Shared/rom folder (rom.key is copied too). You can skip this and "
                          "import them later in Settings.")), False, False, 0)
    l3 = label("")
    b_rom = Gtk.Button(label=_("Choose folder…"), halign=Gtk.Align.START)
    p3.pack_start(b_rom, False, False, 0)
    p3.pack_start(l3, False, False, 0)
    asst.append_page(p3)
    asst.set_page_type(p3, Gtk.AssistantPageType.CONTENT)
    asst.set_page_title(p3, _("ROMs"))
    asst.set_page_complete(p3, True)

    def roms_present():
        rd = core.roms_dir(s)
        return rd.is_dir() and any(p.suffix.lower() in core.ROM_EXTS for p in rd.iterdir())

    def choose_roms(_b):
        fc = Gtk.FileChooserDialog(title=_("Choose the folder with the Kickstart ROMs (e.g. Amiga Forever → Shared → rom)"),
                                   transient_for=asst, action=Gtk.FileChooserAction.SELECT_FOLDER)
        fc.add_buttons(_("Cancel"), Gtk.ResponseType.CANCEL, _("Import"), Gtk.ResponseType.OK)
        if fc.run() == Gtk.ResponseType.OK:
            src = fc.get_filename()
            fc.destroy()
            copied, same = core.import_roms(src, core.roms_dir(s))
            if copied or same:
                l3.set_text(_("Copied {n} file(s) to {path} ({same} were already there).")
                            .format(n=copied, path=core.roms_dir(s), same=same))
            else:
                l3.set_text(_("No Kickstart ROM files found in {path}.").format(path=src))
        else:
            fc.destroy()

    b_rom.connect("clicked", choose_roms)

    # ---- 4. open Amiberry once
    p4 = page_box()
    p4.pack_start(label(_("Let Amiberry find its files"), True), False, False, 0)
    warn = Gtk.InfoBar(message_type=Gtk.MessageType.WARNING)
    wl = Gtk.Label(xalign=0, wrap=True, max_width_chars=70)
    wl.set_markup("<b>%s</b>\n%s" % (
        GLib.markup_escape_text(_("This step is required - do not skip it!")),
        GLib.markup_escape_text(_("Without it Amiberry does not know where your ROMs are: games then start with a "
                                  "substitute ROM and close right away."))))
    warn.get_content_area().add(wl)
    p4.pack_start(warn, False, False, 0)
    steps = label(_("1. Press \"Open Amiberry\" below.\n"
                    "2. On the \"Play\" page, in the \"First-run setup\" section, press \"Rescan ROMs\".\n"
                    "3. Check that it says \"Kickstart ROMs detected.\"\n"
                    "4. Press \"Done\", then close Amiberry (Quit).\n\n"
                    "The \"Next\" button unlocks when Amiberry is closed."))
    p4.pack_start(steps, False, False, 0)
    l4 = label("")
    b_open = Gtk.Button(label=_("Open Amiberry"), halign=Gtk.Align.START)
    p4.pack_start(b_open, False, False, 0)
    p4.pack_start(l4, False, False, 0)
    asst.append_page(p4)
    asst.set_page_type(p4, Gtk.AssistantPageType.CONTENT)
    asst.set_page_title(p4, _("Scan"))

    def amiberry_closed(rc):
        state["scanned"] = True
        b_open.set_sensitive(True)
        b_open.set_label(_("Open Amiberry again"))
        l4.set_text(_("Amiberry was closed. You can continue."))
        asst.set_page_complete(p4, True)
        return False

    def open_amiberry(_b):
        if core.amiberry_missing(s):
            l4.set_text(_("Amiberry is not installed yet - go back and download it first."))
            return
        cmd = core.base_command(s)
        try:
            log = open(core.LAUNCH_LOG, "wb")
            proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            l4.set_text(str(e))
            return
        b_open.set_sensitive(False)
        l4.set_text(_("Amiberry is running…"))

        def waiter():
            rc = proc.wait()
            log.close()
            GLib.idle_add(amiberry_closed, rc)

        threading.Thread(target=waiter, daemon=True).start()

    b_open.connect("clicked", open_amiberry)

    # ---- 5. repository (the Retroplay address the games are downloaded from)
    p45 = page_box()
    p45.pack_start(label(_("Game repository"), True), False, False, 0)
    p45.pack_start(label(_("Denise Station does not come with a Retroplay repository address. Enter the address "
                           "(https://…) of the Retroplay repository you want to use. You are responsible for the "
                           "repository you choose and for having the right to use the files it offers.")),
                   False, False, 0)
    e_repo = Gtk.Entry(text=s.get("whdload_source", ""), placeholder_text="https://")
    l_repo = label(_("You can leave this empty and enter the address later, when you first open the download window."))
    p45.pack_start(e_repo, False, False, 0)
    p45.pack_start(l_repo, False, False, 0)
    asst.append_page(p45)
    asst.set_page_type(p45, Gtk.AssistantPageType.CONTENT)
    asst.set_page_title(p45, _("Repository"))
    asst.set_page_complete(p45, True)

    def repo_changed(_e):
        text = e_repo.get_text().strip()
        ok = not text or bool(rp.validate_source(text))
        asst.set_page_complete(p45, ok)
        l_repo.set_text(_("Please enter a full https:// address without a user name or password.") if not ok else
                        _("You can leave this empty and enter the address later, when you first open the download window."))

    e_repo.connect("changed", repo_changed)

    # ---- 6. summary
    p5 = page_box()
    p5.pack_start(label(_("All set"), True), False, False, 0)
    p5.pack_start(label(_("Denise Station is ready. Download games with the download button in the title bar, or put "
                          "your own games into {folder}.").format(folder=core.GAMES_DIR)), False, False, 0)
    chk = Gtk.CheckButton(label=_("Open the games download window now"), active=True)
    p5.pack_start(chk, False, False, 0)
    asst.append_page(p5)
    asst.set_page_type(p5, Gtk.AssistantPageType.CONFIRM)
    asst.set_page_title(p5, _("Done"))
    asst.set_page_complete(p5, True)

    def on_prepare(a, page):
        if page is p2 and not state["installed"] and not state["busy"] and not core.amiberry_missing(s) \
                and not core.whdboot_missing(s):
            finish_dl(True, core.appimage_version(s) or "")
        if page is p3:
            l3.set_text(_("ROMs found in {path}.").format(path=core.roms_dir(s)) if roms_present() else "")
        if page is p4 and not state["scanned"]:
            asst.set_page_complete(p4, False)

    def on_apply(a):
        src = rp.validate_source(e_repo.get_text().strip()) if e_repo.get_text().strip() else ""
        if src:
            s["whdload_source"] = src
        s["setup_done"] = True
        state["result"] = "applied"
        state["open_dl"] = chk.get_active()
        core.save_settings(s)

    def on_close(a, *_a):
        a.destroy()
        loop.quit()

    asst.connect("prepare", on_prepare)
    asst.connect("apply", on_apply)
    asst.connect("close", on_close)
    def on_cancel(a):
        s["setup_done"] = True                           # skipped: do not ask again (Settings menu can re-run it)
        core.save_settings(s)
        a.destroy()
        loop.quit()

    asst.connect("cancel", on_cancel)
    loop = GLib.MainLoop()
    asst.show_all()
    loop.run()
    return state["result"] == "applied" and state["open_dl"]
