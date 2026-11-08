"""Game info scraper for Denise Station. Standard library only, no account or login needed.

Sources, in order:
  1. Open Amiga Game Database (OAGD, openretro.org): year, developer, publisher, genre, description,
     rating and the box cover. OAGD has no documented API, so its public game pages are read:
       - https://openretro.org/amiga/<slug>      (tried first, slug made from the title)
       - https://openretro.org/search?q=<title>   (fuzzy fallback)
  2. Libretro thumbnails (thumbnails.libretro.com, "Commodore - Amiga"): box covers only, found by
     fuzzy matching the title against the public file index. Used when OAGD has no match or is
     unreachable, and to fill in a cover when OAGD has none. Screenshots are not used at all.

The parsers are deliberately tolerant (they read labels such as "Year:" from the page text), because
OAGD's HTML is not a stable interface. Requests are paced and only go to the two hosts above.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path

OAGD_BASE = "https://openretro.org"
LIBRETRO_BASE = "https://thumbnails.libretro.com/Commodore%20-%20Amiga"
USER_AGENT = "DeniseStation/1.2 (Amiga game launcher; https://github.com/)"
ALLOWED_HOSTS = ("openretro.org", "thumbnails.libretro.com")
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_MANUAL_BYTES = 40 * 1024 * 1024
MAX_EXTRA_IMAGES = 8
MAX_PAGE_BYTES = 3 * 1024 * 1024
MAX_INDEX_BYTES = 40 * 1024 * 1024
INDEX_MAX_AGE = 14 * 86400
OAGD_MATCH_MIN = 0.80
LIBRETRO_MATCH_MIN = 0.84


class ScrapeError(Exception):
    """Base class; messages are safe to show."""


class NotFound(ScrapeError):
    pass


class ServerBusy(ScrapeError):      # unreachable / 429 / 5xx even after retrying
    pass


@dataclass
class ScrapeResult:
    title: str = ""
    genre: str = ""
    year: str = ""
    developer: str = ""
    publisher: str = ""
    rating: int = 0            # 0-5
    cover_url: str = ""        # front box cover
    source: str = ""           # "oagd" or "libretro"
    page_url: str = ""
    extra_images: list = field(default_factory=list)   # [(name, url)]: back cover
    manual_url: str = ""       # PDF manual, when the page links one
    notes: str = ""           # OAGD description
    full_name: str = ""        # Libretro only: complete game name, used to find the OAGD page


# ------------------------------------------------------------ name matching
_ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8",
          "ix": "9", "x": "10"}


def tokens(name: str) -> list[str]:
    """Normalise a game name for comparison: no accents, tags, punctuation, articles or case."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", s)            # (Europe) (AGA) [!] ...
    s = s.lower().replace("&", " and ").replace("_", " ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    out = []
    for i, t in enumerate(s.split()):
        if t in ("the", "a", "an", "and"):
            continue
        if t in _ROMAN and i > 0:                           # "Turrican II" == "Turrican 2"
            t = _ROMAN[t]
        out.append(t)
    return out


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    digits_a = {t for t in ta if t.isdigit()}
    digits_b = {t for t in tb if t.isdigit()}
    if digits_a != digits_b:                                # "Alien Breed" is not "Alien Breed 2"
        return min(0.6, SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    # A lone number of 3+ digits is as distinctive as a longer name: "1943" -> "1943: The Battle of Midway".
    pinned = len(short) == 1 and short[0].isdigit() and len(short[0]) >= 3
    if (len(short) >= 2 or pinned) and long_[:len(short)] == short and short != long_:
        # "Turrican 2" vs "Turrican II: The Final Fight": same game plus subtitle. Trusted more when
        # a number pins the sequel down; "Alien Breed" vs "Alien Breed - Tower Assault" stays doubtful
        # (accepted for OAGD, rejected for Libretro, see the thresholds).
        return 0.85 if (digits_a or pinned) else 0.82
    seq = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    sa, sb = set(ta), set(tb)
    jac = len(sa & sb) / len(sa | sb)
    return max(seq * 0.5 + jac * 0.5, seq if seq > 0.93 else 0)


def slugify(title: str) -> str:
    s = unicodedata.normalize("NFKD", title)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower().replace("'", "")).strip("-")


def tag_count(name: str) -> int:
    return len(re.findall(r"\([^)]*\)", name))


# ------------------------------------------------------------------- parsing
_BOILERPLATE = re.compile(r"page generated in|all times are in utc|disclaim copyright|by submitting|"
                          r"cookie|privacy policy|terms of (use|service)|all rights reserved|log ?in to|sign up", re.I)


def is_boilerplate(text: str) -> bool:
    """Page footers, legal notices and the like - never a game description."""
    return bool(_BOILERPLATE.search(text or ""))


class _Text(HTMLParser):
    """Collects visible text chunks, meta tags and image links of a page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []      # (href, text)
        self.hrefs: list[str] = []                  # every href/src attribute
        self.title = ""
        self.paragraphs: list[str] = []
        self._skip = 0
        self._in_title = False
        self._in_p = False
        self._p: list[str] = []
        self._a_href = None
        self._a_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "p":
            self._in_p, self._p = True, []
        elif tag == "a":
            self._a_href, self._a_text = a.get("href"), []
        for k in ("href", "src", "data-src", "data-original"):
            if a.get(k):
                self.hrefs.append(a[k])

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "p" and self._in_p:
            text = " ".join(" ".join(self._p).split())
            if text:
                self.paragraphs.append(text)
            self._in_p = False
        elif tag == "a" and self._a_href is not None:
            self.links.append((self._a_href, " ".join(" ".join(self._a_text).split())))
            self._a_href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._in_title:
            self.title += data
            return
        t = " ".join(data.split())
        if t:
            self.chunks.append(t)
            if self._in_p:
                self._p.append(t)
            if self._a_href is not None:
                self._a_text.append(t)


def _field(chunks: list[str], label: str) -> str:
    """Value of a 'Label:' field, whether it is in the same chunk or the following one."""
    pat = re.compile(r"^" + re.escape(label) + r"\s*:?\s*(.*)$", re.I)
    for i, c in enumerate(chunks):
        m = pat.match(c)
        if m and (c.lower().startswith(label.lower() + ":") or m.group(1) == "" or
                  c.lower().startswith(label.lower() + " :")):
            val = m.group(1).strip()
            if not val and i + 1 < len(chunks):
                val = chunks[i + 1].strip()
            if val and not val.endswith(":"):
                return val
    return ""


_GENRES = [  # (tag, genre): first match in this order wins, so specific tags come first
    ("shoot-em-up", "Shoot 'em up"), ("shmup", "Shoot 'em up"), ("shooter", "Shoot 'em up"),
    ("beat-em-up", "Beat 'em up"), ("fighting", "Beat 'em up"), ("flight", "Flight sim"),
    ("racing", "Driving"), ("driving", "Driving"), ("platform", "Platform"),
    ("platformer", "Platform"), ("puzzle", "Puzzle"), ("rpg", "RPG"), ("role-playing", "RPG"),
    ("adventure", "Adventure"), ("strategy", "Strategy"), ("sports", "Sports"),
    ("sport", "Sports"), ("board", "Board / Card"), ("card", "Board / Card"),
    ("educational", "Educational"), ("simulation", "Simulation"), ("action", "Action"),
]


def genre_from_tags(tag_text: str) -> str:
    tags = [t.strip().lower().replace(" ", "-") for t in re.split(r"[,;/]", tag_text) if t.strip()]
    for key, genre in _GENRES:
        if key in tags:
            return genre
    return tags[0].replace("-", " ").capitalize() if tags else ""


def parse_game_page(html: str, page_url: str = "") -> ScrapeResult:
    p = _Text()
    p.feed(html)
    r = ScrapeResult(source="oagd", page_url=page_url)
    m = re.match(r"\s*(.*?)\s*\(Amiga[^)]*\)\s*[-–|]", p.title)
    r.title = m.group(1) if m else ""

    ym = re.search(r"(19[89]\d|20[0-4]\d)", _field(p.chunks, "Year"))
    r.year = ym.group(1) if ym else ""
    r.developer = _field(p.chunks, "Developer")
    r.publisher = _field(p.chunks, "Publisher")
    r.genre = genre_from_tags(_field(p.chunks, "Tags"))

    score = re.search(r"\d+(?:[.,]\d+)?", _field(p.chunks, "Score"))
    if score:
        x = float(score.group(0).replace(",", "."))
        r.rating = max(0, min(5, round(x / 2 if x <= 10 else x / 20)))

    # Description: the longest real paragraph on the page (OAGD description), never boilerplate.
    cand = [t for t in p.paragraphs if len(t) >= 60 and not is_boilerplate(t)]
    if cand:
        r.notes = max(cand, key=len)[:2000]

    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(page_url)) if page_url else OAGD_BASE
    covers = []
    for href in p.hrefs:                                   # box covers carry t=lbcover; screenshots are ignored
        m = re.search(r"/image/([0-9a-f]{40})(\?[^\s\"'<>]*)?", href)
        if m and "lbcover" in (m.group(2) or "") and m.group(1) not in covers:
            covers.append(m.group(1))
    if covers:
        r.cover_url = f"{origin}/image/{covers[0]}?s=512&f=jpg"
    if len(covers) > 1:
        pass                                               # back covers are not used
    for href, text in p.links:                             # a linked PDF manual, if any
        if re.search(r"manual|instruction", href + " " + text, re.I) and urllib.parse.urlparse(href).path.lower().endswith(".pdf"):
            r.manual_url = urllib.parse.urljoin(page_url or OAGD_BASE + "/", href)
            break
    return r


def parse_search(html: str, wanted_title: str) -> list[tuple[float, str, str]]:
    """Amiga results of an OAGD search page as (score, title, absolute url), best first."""
    p = _Text()
    p.feed(html)
    out, seen = [], set()
    for href, text in p.links:
        path = urllib.parse.urlparse(urllib.parse.urljoin(OAGD_BASE + "/", href)).path
        m = re.fullmatch(r"/amiga/([a-z0-9][a-z0-9\-]*)", path)
        if not m or path in seen:
            continue
        seen.add(path)
        name = re.sub(r"\s*\(Amiga\)\s*$", "", text).strip() or m.group(1).replace("-", " ")
        out.append((max(similarity(wanted_title, name), similarity(wanted_title, m.group(1).replace("-", " "))),
                    name, OAGD_BASE + path))
    out.sort(key=lambda x: -x[0])
    return out


def parse_index(html: str) -> list[str]:
    """File names (still percent-encoded) of an Apache autoindex page."""
    names = []
    for href in re.findall(r"""href=["']([^"']+)["']""", html):
        if href.startswith(("?", "/", "http")) or href.endswith("/"):
            continue
        if href.lower().endswith((".png", ".jpg", ".jpeg")):
            names.append(href)
    return names


def best_libretro(title: str, names_encoded: list[str]) -> str | None:
    best, best_key = None, None
    for enc in names_encoded:
        name = urllib.parse.unquote(enc)
        stem = name.rsplit(".", 1)[0]
        score = similarity(title, stem)
        if score < LIBRETRO_MATCH_MIN:
            continue
        key = (score, -("demo" in stem.lower()), -tag_count(stem))   # prefer plain, non-demo names
        if best_key is None or key > best_key:
            best, best_key = enc, key
    return best


# -------------------------------------------------------------------- client
class _SameSiteRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname or ""
        if not newurl.startswith("https://") or host not in ALLOWED_HOSTS:
            raise urllib.error.HTTPError(newurl, 403, "redirect to a foreign host", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def title_variants(title: str) -> list[str]:
    """[title] plus, for compound names, the first part: 'A&B', 'A + B', 'A and B', 'A - B', 'A: B'."""
    out = [title]
    first = re.split(r"\s*(?:&|\+|\band\b|\s-\s|:)\s*", title, maxsplit=1, flags=re.I)[0].strip()
    if first and first != title and len(first) >= 5:
        out.append(first)
    return out


class Scraper:
    def __init__(self, cache_dir: Path | None = None, min_interval: float = 1.5,
                 oagd_base: str = OAGD_BASE, libretro_base: str = LIBRETRO_BASE):
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.min_interval = min_interval
        self.oagd_base = oagd_base.rstrip("/")
        self.libretro_base = libretro_base.rstrip("/")
        self._last: dict[str, float] = {}
        self._indexes: dict[str, list[str]] = {}
        self._opener = urllib.request.build_opener(_SameSiteRedirect)
        self.trace: list[str] = []          # what the last lookup tried, for the scraping log

    def _t(self, msg: str):
        self.trace.append(msg)

    # -- low level
    def _pace(self, host: str):
        gap = self.min_interval if "openretro" in host or host.startswith("127.") else 0.15
        wait = self._last.get(host, 0) + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def _get(self, url: str, limit: int, retries: int = 3) -> bytes:
        host = urllib.parse.urlparse(url).netloc
        for attempt in range(retries):
            self._pace(host)
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with self._opener.open(req, timeout=40) as resp:
                    data = resp.read(limit + 1)
            except urllib.error.HTTPError as e:
                self._t(f"  GET {url} -> HTTP {e.code}")
                if e.code == 404:
                    raise NotFound("not found") from None
                if e.code in (429, 423) or e.code >= 500:
                    if attempt + 1 < retries:
                        time.sleep(3 * (attempt + 1))
                        continue
                    raise ServerBusy(f"HTTP {e.code}") from None
                raise ScrapeError(f"HTTP {e.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                self._t(f"  GET {url} -> {type(e).__name__}: {e}")
                if attempt + 1 < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ServerBusy(type(e).__name__) from None
            if len(data) > limit:
                raise ScrapeError("response too large")
            self._t(f"  GET {url} -> OK, {len(data)} bytes")
            return data
        raise ServerBusy("no response")

    def _page(self, url: str) -> str:
        return self._get(url, MAX_PAGE_BYTES).decode("utf-8", "replace")

    # -- OAGD
    def oagd_lookup(self, title: str) -> ScrapeResult:
        self._t(f"OAGD: looking for {title!r}")
        candidates = [f"{self.oagd_base}/amiga/{slugify(title)}"] if slugify(title) else []
        res = None
        for url in candidates:
            try:
                res = parse_game_page(self._page(url), url)
                self._t(f"  page title {res.title!r}, year {res.year!r}, developer {res.developer!r}, "
                        f"cover {'yes' if res.cover_url else 'no'}")
                if res.title and similarity(title, res.title) < 0.6:
                    self._t("  -> rejected: page is a different game")
                    res = None                       # slug hit a different game
                break
            except NotFound:
                res = None
        if res is None or not (res.year or res.developer or res.publisher or res.genre or res.cover_url):
            res = None
            html = self._page(f"{self.oagd_base}/search?" + urllib.parse.urlencode({"q": title}))
            hits = parse_search(html, title)
            self._t("  search hits: " + (", ".join(f"{h[1]!r} {h[0]:.2f}" for h in hits[:5]) or "none"))
            if not hits or hits[0][0] < OAGD_MATCH_MIN:
                raise NotFound("no match in OAGD")
            url = hits[0][2]
            if url.startswith(OAGD_BASE):
                url = self.oagd_base + url[len(OAGD_BASE):]
            res = parse_game_page(self._page(url), url)
        if not (res.year or res.developer or res.publisher or res.genre or res.cover_url):
            raise NotFound("OAGD page has no usable data")
        return res

    # -- Libretro
    def _index(self, kind: str) -> list[str]:
        if kind in self._indexes:
            return self._indexes[kind]
        cache = self.cache_dir / f"libretro-{kind}.json" if self.cache_dir else None
        if cache and cache.exists() and time.time() - cache.stat().st_mtime < INDEX_MAX_AGE:
            try:
                names = json.loads(cache.read_text())
                if isinstance(names, list) and names:
                    self._indexes[kind] = names
                    return names
            except (OSError, ValueError):
                pass
        html = self._get(f"{self.libretro_base}/{kind}/", MAX_INDEX_BYTES).decode("utf-8", "replace")
        names = parse_index(html)
        if not names:
            raise ScrapeError("empty Libretro index")
        self._indexes[kind] = names
        if cache:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(names))
            except OSError:
                pass
        return names

    def libretro_lookup(self, title: str, want_cover: bool = True, want_extras: bool = False) -> ScrapeResult:
        """Box cover from the Libretro thumbnails (Named_Boxarts), found by similar title."""
        self._t(f"Libretro: looking for {title!r}")
        res = ScrapeResult(source="libretro")
        names = self._index("Named_Boxarts")
        top = sorted(((similarity(title, urllib.parse.unquote(n).rsplit(".", 1)[0]), urllib.parse.unquote(n))
                      for n in names), reverse=True)[:3]
        self._t(f"  Named_Boxarts: {len(names)} files; closest: " + (", ".join(f"{n!r} {sc:.2f}" for sc, n in top) or "-"))
        enc = best_libretro(title, names)
        if not enc:
            raise NotFound("no match in Libretro thumbnails")
        res.cover_url = f"{self.libretro_base}/Named_Boxarts/{enc}"
        res.full_name = self._full_name(enc)
        return res

    @staticmethod
    def _full_name(enc: str) -> str:
        """The full game name Libretro uses, without region tags: '1943 - The Battle of Midway'."""
        stem = urllib.parse.unquote(enc).rsplit(".", 1)[0]
        return re.sub(r"\s*\([^)]*\)", "", stem).replace("_", " ").strip()

    # -- manual search: several candidates to choose from
    def candidates(self, query: str, cancel=None, limit: int = 6):
        """Yield candidate games for a hand-edited search text, one at a time: OAGD pages first (each with its
        info and cover), then Libretro box covers. Each item: {"source", "name", "res", "cover_url"}."""
        try:
            html = self._page(f"{self.oagd_base}/search?" + urllib.parse.urlencode({"q": query}))
            hits = parse_search(html, query)[:limit]
        except ScrapeError:
            hits = []
        for _score, name, url in hits:
            if cancel is not None and cancel.is_set():
                return
            if url.startswith(OAGD_BASE):
                url = self.oagd_base + url[len(OAGD_BASE):]
            try:
                res = parse_game_page(self._page(url), url)
            except ScrapeError:
                continue
            yield {"source": "OAGD", "name": name or res.title, "res": res, "cover_url": res.cover_url}
        try:
            names = self._index("Named_Boxarts")
        except ScrapeError:
            return
        top = sorted(((similarity(query, urllib.parse.unquote(n).rsplit(".", 1)[0]), n) for n in names),
                     reverse=True)[:limit]
        for sc, enc in top:
            if sc < 0.45:
                break
            if cancel is not None and cancel.is_set():
                return
            res = ScrapeResult(source="libretro", cover_url=f"{self.libretro_base}/Named_Boxarts/{enc}")
            yield {"source": "Libretro", "name": urllib.parse.unquote(enc).rsplit(".", 1)[0], "res": res,
                   "cover_url": res.cover_url}

    # -- combined
    def lookup(self, path: str, title: str, want_screenshot: bool = True,
               prefer_cover: bool = False, want_extras: bool = False) -> ScrapeResult:
        """Try the title as given, then (only if that finds nothing) its main part: WHDLoad names
        like '4D Sports Driving&Master Tracks' bundle a game with a data disk the databases list
        separately. Raises NotFound / ServerBusy as _lookup_one does."""
        busy = None
        self.trace = [f"File: {path}", f"Search title: {title!r}"]
        for variant in title_variants(title):
            try:
                return self._lookup_one(path, variant, want_screenshot, prefer_cover, want_extras)
            except NotFound:
                continue
            except ServerBusy as e:
                busy = e
        if busy:
            raise busy
        raise NotFound("no match")

    def _lookup_one(self, path: str, title: str, want_screenshot: bool = True,
                    prefer_cover: bool = False, want_extras: bool = False) -> ScrapeResult:
        """OAGD first; Libretro (box covers) when OAGD fails or has no cover. Raises NotFound if neither
        has anything, ServerBusy if nothing could be reached."""
        res, oagd_down = None, False
        try:
            res = self.oagd_lookup(title)
        except NotFound:
            pass
        except (ServerBusy, ScrapeError):
            oagd_down = True
        lacks_image = res is not None and want_screenshot and not res.cover_url
        if res is None or lacks_image:
            try:
                lib = self.libretro_lookup(title)
                if res is None:
                    res = lib
                    # Libretro knows the full name ("1943 - The Battle of Midway"): its slug often is the
                    # OAGD page even when searching OAGD for the short name found nothing.
                    full = getattr(lib, "full_name", "")
                    if full and slugify(full) != slugify(title) and not oagd_down:
                        try:
                            res = self.oagd_lookup(full)
                            self._fill_images(res, lib)
                        except (NotFound, ServerBusy, ScrapeError):
                            res = lib
                else:
                    self._fill_images(res, lib)
            except NotFound:
                pass
            except (ServerBusy, ScrapeError):
                if res is None and oagd_down:
                    raise ServerBusy("OAGD and Libretro unreachable") from None
        if res is None:
            if oagd_down:
                raise ServerBusy("OAGD unreachable and no Libretro match")
            raise NotFound("no match")
        return res

    @staticmethod
    def _fill_images(res: ScrapeResult, lib: ScrapeResult):
        res.cover_url = res.cover_url or lib.cover_url
        have = {n for n, _u in res.extra_images}
        res.extra_images += [(n, u) for n, u in lib.extra_images if n not in have]

    def check(self) -> dict:
        """Reachability of both sources for the 'Test connection' button: {name: error or ''}."""
        out = {}
        try:
            self._page(f"{self.oagd_base}/amiga/turrican")
            out["oagd"] = ""
        except NotFound:
            out["oagd"] = ""        # reachable, just no such page
        except ScrapeError as e:
            out["oagd"] = str(e) or type(e).__name__
        try:
            out["libretro"] = "" if self._index("Named_Boxarts") else "empty"
        except ScrapeError as e:
            out["libretro"] = str(e) or type(e).__name__
        return out

    def download_manual(self, url: str, dest_base: Path) -> Path | None:
        """A PDF manual from the OAGD host. Returns the saved path, or None when it is not a PDF."""
        host = urllib.parse.urlparse(url).hostname or ""
        if not (host.startswith("127.") or (url.startswith("https://") and host in ALLOWED_HOSTS)):
            return None
        try:
            data = self._get(url, MAX_MANUAL_BYTES, retries=2)
        except ScrapeError:
            return None
        if not data.startswith(b"%PDF"):
            return None
        dest = Path(str(dest_base) + ".pdf")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest

    def download_image(self, url: str, dest_base: Path) -> Path | None:
        host = urllib.parse.urlparse(url).hostname or ""
        local = host.startswith("127.")
        if not local and (not url.startswith("https://") or host not in ALLOWED_HOSTS):
            return None
        tries = [url]
        if "openretro" in host and "?s=2x" in url:           # fall back to the plain thumbnail
            tries.append(url.replace("?s=2x", ""))
        data = None
        for u in tries:
            try:
                data = self._get(u, MAX_IMAGE_BYTES, retries=2)
                break
            except ScrapeError:
                continue
        if not data:
            return None
        if data.startswith(b"\x89PNG"):
            ext = ".png"
        elif data.startswith(b"\xff\xd8"):
            ext = ".jpg"
        else:
            return None                                      # e.g. an error page
        dest = Path(str(dest_base) + ext)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return dest
