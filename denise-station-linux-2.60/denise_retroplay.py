"""Denise Station - Retroplay WHDLoad pack downloader (no GUI imports, standard library only).

Works like MrV2K's "WHDLoad Download Tool": it reads the file lists (DAT files) and the directory listing of the
Retroplay WHDLoad Packs on the server whose address you enter yourself, lets you pick archives (games, betas, demos, magazines), and downloads
only what you choose, verifying each file against the CRC32 from the DAT.

Layout of the server is discovered from its directory listings (Apache style), not hard-coded, so folder renames
there do not break it. Only the host of the address you entered is contacted, one request at a time with a short pause.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path

# Denise Station contains no download address: the person using it enters the source address themselves
# (Settings / first start). Only that one host is ever contacted, over https.
USER_AGENT = "DeniseStation/1.0 (Retroplay pack downloader)"
ARCHIVE_EXT = (".lha", ".lzx")
MAX_LISTING_BYTES = 4 * 1024 * 1024
MAX_DAT_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 ** 3        # archives are streamed to disk, so big CD games are fine
INDEX_MAX_AGE = 3 * 24 * 3600
MAX_DEPTH = 3
PAUSE = 0.4              # between two archive downloads
LIST_PAUSE = 0.05        # between two directory-listing requests (small pages)
LIST_WORKERS = 4         # directory listings are fetched a few at a time

# Server layout (from the original WHDLoad Download Tool's defaults): <base>/<pack folder>/..., with pack folders named
# Commodore_Amiga_-_WHDLoad_-_Games, ..._Demos, ..._Games_-_Beta_&_Unofficial, ..._Demos_-_Beta_&_Unofficial,
# ..._Magazines. They are discovered, not hard-coded.

# Only these five folders are read and downloaded from; everything else on the server is ignored.
PACK_FOLDERS = (
    "Commodore_Amiga_-_WHDLoad_-_Demos",
    "Commodore_Amiga_-_WHDLoad_-_Demos_-_Beta_&_Unofficial",
    "Commodore_Amiga_-_WHDLoad_-_Games",
    "Commodore_Amiga_-_WHDLoad_-_Games_-_Beta_&_Unofficial",
    "Commodore_Amiga_-_WHDLoad_-_Magazines",
)

# Words after the version in a Retroplay name that are not hardware/language tags.
_NOT_TAGS = {"image", "files", "file", "disk", "disks", "rev", "demo", "beta"}
LANGS = {"de", "fr", "es", "it", "pl", "sv", "fi", "dk", "no", "nl", "cz", "hu", "gr", "pt", "ru", "tr", "jp",
         "en", "uk", "us"}


class DownloadError(Exception):
    """Something went wrong; the message is meant for the user."""


@dataclass
class Entry:
    name: str          # readable name from the file name, e.g. "Alien Breed 2"
    file: str          # e.g. "AlienBreed2_v1.2_0412.lha"
    url: str
    pack: str          # e.g. "Games"
    size: int = 0
    crc: str = ""      # lower-case 8 hex digits from the DAT, '' when unknown
    tags: tuple = ()   # e.g. ("AGA",) or ("De",)

    def to_json(self):
        d = asdict(self)
        d["tags"] = list(self.tags)
        return d

    @staticmethod
    def from_json(d):
        d = dict(d)
        d["tags"] = tuple(d.get("tags", ()))
        return Entry(**d)


# ------------------------------------------------------------------ names
def safe_file_name(name: str) -> str:
    name = os.path.basename(urllib.parse.unquote(name))
    ok = re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+&()'\- ]*", name) and name.lower().endswith(ARCHIVE_EXT)
    return name if ok else ""


def name_and_tags(file: str) -> tuple[str, tuple]:
    """'AlienBreed2_v1.2_AGA_0412.lha' -> ('Alien Breed 2', ('AGA',))."""
    stem = os.path.splitext(file)[0]
    parts = stem.split("_")
    cut = next((i for i, p in enumerate(parts) if re.fullmatch(r"v\d[\w.\-]*", p, flags=re.I)), None)
    base_parts = parts if cut is None else parts[:cut]
    rest = [] if cut is None else parts[cut + 1:]
    tags = tuple(p for p in rest if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,5}", p) and p.lower() not in _NOT_TAGS
                 and not re.fullmatch(r"(?:disk|rev)\d*", p, flags=re.I))
    name = " ".join(base_parts)
    name = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", name)
    name = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    name = re.sub(r"(?<=[0-9])(?=[A-Z][a-z])", " ", name)
    return re.sub(r"\s+", " ", name).strip() or stem, tags


# Filter groups, as in the original WHDLoad Download Tool: an archive is hidden when any of its properties is unticked.
FILTER_SYSTEM = ("Amiga", "Arcadia", "CD32", "CDTV", "CDROM")
FILTER_EXTRAS = ("AGA", "ECS/OCS", "NTSC", "PAL", "Chip", "Fast", "Files", "Image")
FILTER_LANGS = ("Czech", "Danish", "Dutch", "English", "Finnish", "French", "German", "Greek", "Italian", "Multi",
                "Polish", "Spanish", "Swedish")
_LANG_CODES = {"de": "German", "fr": "French", "es": "Spanish", "it": "Italian", "pl": "Polish", "sv": "Swedish",
               "fi": "Finnish", "dk": "Danish", "da": "Danish", "nl": "Dutch", "cz": "Czech", "gr": "Greek",
               "en": "English", "uk": "English", "us": "English"}
_SYSTEM_WORDS = {"arcadia": "Arcadia", "cd32": "CD32", "cdtv": "CDTV", "cdrom": "CDROM", "cd": "CDROM"}


def attributes(file: str) -> frozenset:
    """Properties of a Retroplay archive read from its file name, e.g. 'Foo_v1.0_AGA_De.lha' ->
    {Amiga, AGA, PAL, German}. Missing parts take their defaults: Amiga, ECS/OCS, PAL, English."""
    parts = os.path.splitext(os.path.basename(file))[0].split("_")
    cut = next((i for i, p in enumerate(parts) if re.fullmatch(r"v\d[\w.\-]*", p, flags=re.I)), None)
    rest = [] if cut is None else [p.lower() for p in parts[cut + 1:]]
    out = set()
    system = {_SYSTEM_WORDS[p] for p in rest if p in _SYSTEM_WORDS}
    out |= system or {"Amiga"}
    out.add("AGA" if "aga" in rest else "ECS/OCS")
    out.add("NTSC" if "ntsc" in rest else "PAL")
    out |= {p.capitalize() for p in rest if p in ("chip", "fast", "files", "image")}      # exact words only
    langs = {_LANG_CODES[p] for p in rest if p in _LANG_CODES}
    if any(re.fullmatch(r"multi\d*", p) for p in rest):
        langs.add("Multi")
    out |= langs or {"English"}
    return frozenset(out)


def is_language(tag: str) -> bool:
    return tag.lower() in LANGS


def local_key(file_name: str) -> str:
    """'Turrican3_v1.6_Image_2633.lha' and 'Turrican3_v1.7_2744.lha' share the key 'turrican3'."""
    stem = os.path.splitext(os.path.basename(file_name))[0]
    parts = stem.split("_")
    cut = next((i for i, p in enumerate(parts) if re.fullmatch(r"v\d[\w.\-]*", p, flags=re.I)), len(parts))
    return re.sub(r"[^a-z0-9]+", "", "".join(parts[:cut]).lower())


def version_of(file: str) -> tuple:
    """(1, 6) for 'Turrican3_v1.6_Image_2633.lha'; () when the name carries no version."""
    for part in os.path.splitext(os.path.basename(file))[0].split("_"):
        m = re.fullmatch(r"v(\d+(?:\.\d+)*)\w*", part, flags=re.I)
        if m:
            return tuple(int(x) for x in m.group(1).split("."))
    return ()


def update_key(file: str) -> str:
    """Same game, same variant: name plus tags (AGA, CD32, De, ...), without version and build numbers."""
    _name, tags = name_and_tags(os.path.basename(file))
    return local_key(file) + "|" + "+".join(sorted(t.lower() for t in tags))


def find_updates(local_files, entries) -> dict:
    """{remote file name: (local path, old version, new version)} for archives you have in an older version."""
    best: dict[str, tuple] = {}
    for path in local_files:
        if not str(path).lower().endswith(ARCHIVE_EXT):
            continue
        v = version_of(path)
        k = update_key(path)
        if v and (k not in best or v > best[k][1]):
            best[k] = (str(path), v)
    out = {}
    for e in entries:
        have = best.get(update_key(e.file))
        nv = version_of(e.file)
        if have and nv and nv > have[1] and os.path.basename(have[0]).lower() != e.file.lower():
            out[e.file] = (have[0], have[1], nv)
    return out


def fmt_version(v: tuple) -> str:
    return "v" + ".".join(str(x) for x in v)


def replace_old(old_path, new_path) -> Path | None:
    """Move an outdated archive into a hidden '.replaced' folder next to it (the library scan skips it)."""
    old = Path(old_path)
    if not old.is_file() or old.resolve() == Path(new_path).resolve():
        return None
    dest = old.parent / ".replaced"
    try:
        dest.mkdir(exist_ok=True)
        target = dest / old.name
        os.replace(old, target)
        return target
    except OSError:
        return None


def pack_label(pack: str) -> str:
    """'Commodore_Amiga_-_WHDLoad_-_Games_-_Beta_&_Unofficial' -> 'Games - Beta & Unofficial'."""
    p = re.sub(r"^Commodore[_ ]Amiga[_ ]-[_ ]WHDLoad[_ ]-[_ ]", "", pack)
    return re.sub(r"\s+", " ", p.replace("_-_", " - ").replace("_", " ")).strip() or pack


def local_pack_folder(pack: str) -> str:
    """Folder name used when sorting by pack (same names as the original tool: Games, Demos, Beta-Game, ...)."""
    low = pack_label(pack).lower()
    beta = "beta" in low or "unofficial" in low or "unreleased" in low
    if "magazine" in low:
        return "Magazines"
    if "demo" in low:
        return "Beta-Demo" if beta else "Demos"
    if "game" in low:
        return "Beta-Game" if beta else "Games"
    return re.sub(r"[^A-Za-z0-9._ \-]+", "_", pack_label(pack)).strip(" .") or "Other"


def folder_for(entry: Entry, setup: str) -> str:
    """Sub-folder below the destination. setup: 'flat', 'letter' (A, B, ... '0-9'), 'pack' (Games, Demos, ...)
    or 'pack_letter'."""
    parts = []
    if setup in ("pack", "pack_letter"):
        parts.append(local_pack_folder(entry.pack))
    if setup in ("letter", "pack_letter"):
        c = entry.name[:1].upper()
        parts.append(c if c.isalpha() else "0-9")
    return "/".join(parts)


# ------------------------------------------------------------------ parsing
class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def parse_listing(html: str, page_url: str, extra_hosts=()) -> tuple[list[str], list[str]]:
    """(sub-directory URLs, file URLs) of an Apache-style directory listing, same host only."""
    p = _Links()
    p.feed(html)
    here = urllib.parse.urlparse(page_url)
    hosts = {here.hostname} | set(extra_hosts)               # the listing's own host only
    dirs, files = [], []
    for href in p.links:
        if href.startswith(("?", "#", "mailto:")) or href in ("../", "./", "/"):
            continue
        url = urllib.parse.urljoin(page_url, href)
        u = urllib.parse.urlparse(url)
        if u.hostname not in hosts or u.query:
            continue
        if not u.path.startswith(here.path):          # parent directories and sibling sites
            continue
        if u.path == here.path:
            continue
        (dirs if u.path.endswith("/") else files).append(url)
    return dirs, files


def parse_dat(xml_bytes: bytes) -> dict[str, tuple[int, str]]:
    """{file name lower-case: (size, crc)} from a Logiqx/clrmamepro style XML DAT (<rom name size crc>)."""
    out: dict[str, tuple[int, str]] = {}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    for rom in root.iter("rom"):
        name = os.path.basename((rom.get("name") or "").replace("\\", "/"))
        if not name.lower().endswith(ARCHIVE_EXT):
            continue
        try:
            size = int(rom.get("size") or 0)
        except ValueError:
            size = 0
        crc = (rom.get("crc") or "").strip().lower()
        out[name.lower()] = (size, crc if re.fullmatch(r"[0-9a-f]{8}", crc) else "")
    return out


def pack_name(url: str, base: str) -> str:
    rel = urllib.parse.unquote(urllib.parse.urlparse(url).path[len(urllib.parse.urlparse(base).path):])
    return rel.strip("/").split("/")[0] if rel.strip("/") else ""


# ------------------------------------------------------------------ network
class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = set(hosts)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).hostname not in self.hosts:
            raise urllib.error.HTTPError(newurl, 403, "redirect to a foreign host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_source(url: str) -> str:
    """The cleaned-up source address (https, no credentials or query, trailing slash) or '' when it is not usable."""
    url = (url or "").strip()
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        return ""
    return url if url.endswith("/") else url + "/"


class Retroplay:
    def __init__(self, base: str, cache_dir: Path | None = None, pause: float = PAUSE):
        self.base = base if base.endswith("/") else base + "/"
        self.test = self.base.startswith("http://127.0.0.1")
        self.host = urllib.parse.urlparse(self.base).hostname or ""
        tag = hashlib.sha1(self.base.encode()).hexdigest()[:8]            # one cached list per source
        self.cache = Path(cache_dir) / f"retroplay-index-{tag}.json" if cache_dir else None
        self.pause = pause
        self._next = 0.0
        self._lock = threading.Lock()
        self.list_pause = LIST_PAUSE if pause else 0.0
        self._opener = urllib.request.build_opener(_SameHostRedirect({self.host, "127.0.0.1"} if self.test else {self.host}))
        self.trace: list[str] = []

    def _allowed(self, url: str) -> bool:
        u = urllib.parse.urlparse(url)
        if self.test and u.hostname == "127.0.0.1":
            return True
        return u.scheme == "https" and u.hostname == self.host

    def _get(self, url: str, limit: int, cancel=None, progress=None, pause=None) -> bytes:
        if not self._allowed(url):
            raise DownloadError("Refusing to contact " + (urllib.parse.urlparse(url).hostname or url))
        gap = self.pause if pause is None else pause
        with self._lock:                                   # start requests at least `gap` seconds apart
            start = max(time.monotonic(), self._next)
            self._next = start + gap
        if start > time.monotonic():
            time.sleep(start - time.monotonic())
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self._opener.open(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                if total > limit:
                    raise DownloadError("The file is larger than allowed.")
                chunks, got = [], 0
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadError("Cancelled.")
                    block = resp.read(64 * 1024)
                    if not block:
                        break
                    chunks.append(block)
                    got += len(block)
                    if got > limit:
                        raise DownloadError("The file is larger than allowed.")
                    if progress:
                        progress(got, total)
        except urllib.error.HTTPError as e:
            self.trace.append(f"GET {url} -> HTTP {e.code}")
            raise DownloadError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            self.trace.append(f"GET {url} -> {type(e).__name__}: {e}")
            raise DownloadError(type(e).__name__) from None
        self.trace.append(f"GET {url} -> OK, {got} bytes")
        return b"".join(chunks)

    # -- directory crawl
    def _list_one(self, url: str, cancel) -> tuple[list[str], list[str]]:
        html = self._get(url, MAX_LISTING_BYTES, cancel, pause=self.list_pause).decode("utf-8", "replace")
        return parse_listing(html, url, ("127.0.0.1",) if self.test else ())

    def _crawl(self, cancel, status) -> tuple[list[str], list[str]]:
        """(archive URLs, DAT zip URLs) in the five pack folders: folders are read level by level, a few at a time."""
        dirs, _files = self._list_one(self.base, cancel)           # the root: only the pack folders count
        level = [d for d in dirs if pack_name(d, self.base) in PACK_FOLDERS]
        archives: list[str] = []
        dats: list[str] = []
        done = 0
        with ThreadPoolExecutor(max_workers=LIST_WORKERS) as pool:
            for depth in range(MAX_DEPTH):
                if not level:
                    break
                nxt: list[str] = []
                futures = [(u, pool.submit(self._list_one, u, cancel)) for u in level]
                for u, fut in futures:
                    if cancel is not None and cancel.is_set():
                        for _u, f in futures:
                            f.cancel()
                        raise DownloadError("Cancelled.")
                    try:
                        sub, files = fut.result()
                    except DownloadError as e:
                        if str(e) == "Cancelled.":
                            raise
                        continue                  # one unreadable folder should not stop the rest
                    done += 1
                    archives += [f for f in files if f.lower().split("?")[0].endswith(ARCHIVE_EXT)]
                    dats += [f for f in files if f.lower().endswith(".zip")]
                    nxt += sub
                    if status:
                        status("%s (%d folders, %d archives)" % (
                            urllib.parse.unquote(urllib.parse.urlparse(u).path[len(urllib.parse.urlparse(self.base).path):]),
                            done, len(archives)))
                level = nxt
        return archives, dats

    def cached_index(self) -> list[Entry]:
        """The list from the last load, however old; [] when there is none (no network access)."""
        try:
            return [e for e in (Entry.from_json(d) for d in json.loads(self.cache.read_text())) if e.pack in PACK_FOLDERS]
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return []

    def index(self, refresh: bool = False, cancel=None, status=None) -> list[Entry]:
        """All archives on the server, with size/CRC from the DAT files where available."""
        if self.cache and self.cache.exists() and not refresh \
                and time.time() - self.cache.stat().st_mtime < INDEX_MAX_AGE:
            try:
                return [e for e in (Entry.from_json(d) for d in json.loads(self.cache.read_text()))
                        if e.pack in PACK_FOLDERS]
            except (OSError, ValueError, TypeError, KeyError):
                pass
        self.trace = []
        archives, dats = self._crawl(cancel, status)
        meta: dict[str, tuple[int, str]] = {}
        for z in dats:
            if status:
                status("DAT: " + urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(z).path)))
            try:
                zf = zipfile.ZipFile(io.BytesIO(self._get(z, MAX_DAT_BYTES, cancel)))
                for info in zf.infolist():
                    if info.filename.lower().endswith((".dat", ".xml")) and info.file_size < 64 * 1024 * 1024:
                        meta.update(parse_dat(zf.read(info)))
            except (zipfile.BadZipFile, DownloadError) as e:
                if str(e) == "Cancelled.":
                    raise
                continue
        entries, seen = [], set()
        for url in archives:
            file = safe_file_name(urllib.parse.urlparse(url).path)
            if not file or url in seen:
                continue
            seen.add(url)
            name, tags = name_and_tags(file)
            size, crc = meta.get(file.lower(), (0, ""))
            entries.append(Entry(name=name, file=file, url=url, pack=pack_name(url, self.base) or "Games",
                                 size=size, crc=crc, tags=tags))
        if not entries:
            raise DownloadError("No archives were found in the Retroplay pack folders (the layout may have changed).")
        entries.sort(key=lambda e: (e.pack.lower(), e.name.lower(), e.file.lower()))
        if self.cache:
            try:
                self.cache.parent.mkdir(parents=True, exist_ok=True)
                self.cache.write_text(json.dumps([e.to_json() for e in entries]))
            except OSError:
                pass
        return entries

    # -- one archive
    def download(self, entry: Entry, dest_dir, cancel=None, progress=None) -> Path:
        """Download one archive into dest_dir, verify size and CRC32 when known, return the path."""
        file = safe_file_name(entry.file)
        if not file or not self._allowed(entry.url) or pack_name(entry.url, self.base) not in PACK_FOLDERS:
            raise DownloadError("Unsafe entry: " + entry.file)
        dest_dir = Path(dest_dir)
        target = dest_dir / file
        tmp = dest_dir / (file + ".part")
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            size, crc, head = self._stream(entry.url, tmp, cancel, progress)
            if file.lower().endswith(".lha") and not (len(head) > 7 and head[2:5] == b"-lh" and head[6:7] == b"-"):
                raise DownloadError("Not an LHA archive (the server sent something else).")
            if entry.size and size != entry.size:
                raise DownloadError(f"Wrong size ({size} bytes, expected {entry.size}).")
            if entry.crc and f"{crc & 0xffffffff:08x}" != entry.crc.lower():
                raise DownloadError("CRC32 mismatch - the file is damaged.")
            os.replace(tmp, target)
        except OSError as e:
            raise DownloadError("Could not save %s: %s" % (file, e.strerror or e)) from None
        finally:
            try:
                tmp.unlink()                  # nothing is left behind if anything failed
            except OSError:
                pass
        return target

    def _stream(self, url, tmp, cancel=None, progress=None):
        """Download url into the file tmp. Returns (size, crc32, first bytes)."""
        if not self._allowed(url):
            raise DownloadError("Refusing to contact " + (urllib.parse.urlparse(url).hostname or url))
        with self._lock:
            start = max(time.monotonic(), self._next)
            self._next = start + self.pause
        if start > time.monotonic():
            time.sleep(start - time.monotonic())
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        got, crc, head = 0, 0, b""
        try:
            with self._opener.open(req, timeout=60) as resp, open(tmp, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                if total > MAX_ARCHIVE_BYTES:
                    raise DownloadError("The file is larger than allowed.")
                while True:
                    if cancel is not None and cancel.is_set():
                        raise DownloadError("Cancelled.")
                    block = resp.read(256 * 1024)
                    if not block:
                        break
                    if not head:
                        head = block[:16]
                    out.write(block)
                    crc = zlib.crc32(block, crc)
                    got += len(block)
                    if got > MAX_ARCHIVE_BYTES:
                        raise DownloadError("The file is larger than allowed.")
                    if progress:
                        progress(got, total)
        except urllib.error.HTTPError as e:
            self.trace.append(f"GET {url} -> HTTP {e.code}")
            raise DownloadError(f"HTTP {e.code}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            self.trace.append(f"GET {url} -> {type(e).__name__}: {e}")
            raise DownloadError(type(e).__name__) from None
        self.trace.append(f"GET {url} -> OK, {got} bytes")
        return got, crc, head
