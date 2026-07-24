#!/usr/bin/env python3
# ======================================================================
#  CSV  ->  AUDIO + VIDEO  DOWNLOADER
# ----------------------------------------------------------------------
#  Give it a CSV of songs (e.g. a Spotify playlist export) and it will,
#  for every track:
#     1. Open a real Chrome / Edge window (the "popup").
#     2. Type the search query into YouTube one character at a time with
#        human-like, randomized delays so the server does not treat the
#        traffic as a robot.
#     3. Pick the best matching result (title similarity + duration).
#     4. Download the audio (MP3) and/or the video (MP4) with yt-dlp.
#     5. Tag the MP3 using the rich metadata already in the CSV.
#
#  If the browser cannot start or cannot find a link, it automatically
#  falls back to a plain yt-dlp search so the download still happens.
#
#  Usage:
#     python download_from_csv.py --csv "The_Ultimate_Hit_Mix.csv" --mode both
#     python download_from_csv.py --csv playlist.csv --mode audio --limit 10
#     python download_from_csv.py --csv playlist.csv --no-browser        # headless fallback
#
#  Requirements (install once):
#     pip install selenium yt-dlp mutagen
#     ffmpeg on PATH (winget install Gyan.FFmpeg)   <- needed for MP3 / MP4 muxing
# ======================================================================

from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
import os
import random
import re
import shutil
import sys
import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from tqdm import tqdm

# The Windows console defaults to cp1252, which chokes on emoji / accents that appear
# in track names and playlist filenames. Force UTF-8 so printing never crashes.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ------------------- CONFIG (edit here or override on the command line) -------------------
SCRIPT_DIR   = Path(__file__).resolve().parent
OUTPUT_DIR   = Path("D:/Downloads")          # audio/ and video/ are created inside
DEFAULT_MODE = "both"                            # "audio" | "video" | "both"

# Ledger of every track ever downloaded, with timestamps. Lives at the root of the
# output folder, so a track grabbed under one CSV's subfolder is skipped under any other.
HISTORY_FILENAME = "download_history.json"

# Human-typing behaviour (seconds). Kept deliberately un-robotic.
TYPE_DELAY_MIN   = 0.06     # fastest gap between two keystrokes
TYPE_DELAY_MAX   = 0.22     # slowest normal gap between two keystrokes
THINK_PAUSE_PROB = 0.08     # chance, per character, of a longer "thinking" pause
THINK_PAUSE_MIN  = 0.40
THINK_PAUSE_MAX  = 1.10

# Polite, randomized wait BETWEEN tracks so the search pattern is not machine-regular.
TRACK_PAUSE_MIN  = 2.5
TRACK_PAUSE_MAX  = 6.0

# How many tracks to download at once. 1 = classic sequential (nicest logs, one browser).
# >1 overlaps the slow network downloads; in browser mode each worker opens its OWN
# browser, so keep this small (2-4) or you defeat the anti-bot pacing and hog RAM.
DEFAULT_WORKERS  = 1

# How close (seconds) a YouTube result's length must be to the CSV duration to score well.
DURATION_TOLERANCE = 30

# Audio quality: "0" = best VBR for MP3.
AUDIO_QUALITY = "0"

# Cover art. YouTube thumbnails are 16:9, so phone music apps letterbox them ("edges").
# When True, we fetch the real SQUARE album cover (iTunes -> Deezer, same art Apple Music
# uses) from the CSV metadata and embed that instead. Falls back to the YouTube thumbnail
# only if no square cover is found.
PREFER_SQUARE_ART = True
ART_MAX_EDGE      = "3000"   # px. Apple ships up to 3000; 1200 looks soft filling a Retina screen

# Do the rate-limited iTunes/Deezer art + genre lookups WHILE downloading? Off by default:
# it would serialise every parallel download on the iTunes rate limit. Downloads stay fast
# (CSV genre + YouTube thumbnail); run --reart and --fix-genres afterwards to enrich.
ENRICH_DURING_DOWNLOAD = False
ART_MIN_SCORE     = 0.55     # how well the found cover's artist+title must match the track
ART_SQUARE_TOL    = 0.06     # |w/h - 1| below this counts as "already square" (skip in --reart)
REART_RETRY_DAYS  = 30       # a track that had no cover last time is retried only after this many days

# iTunes answers HTTP 429 past roughly 20 requests/minute. Going faster does not make the
# run quicker - it just makes iTunes refuse, and every refused track silently falls back
# to a worse genre source. Pace it properly and retry when throttled.
ITUNES_MIN_INTERVAL = 3.2    # seconds between iTunes calls, across ALL worker threads
ITUNES_MAX_RETRIES  = 3
ITUNES_BACKOFF      = 6.0    # extra seconds to wait after a 429, multiplied per attempt
ITUNES_FAIL_LIMIT   = 5      # consecutive refusals before we stop asking iTunes at all

# Prefer Chrome; fall back to Edge automatically if Chrome is not installed.
BROWSER_PREFERENCE = ["chrome", "edge"]

# Age-restricted / sign-in videos (The Weeknd's darker visuals, explicit tracks, etc.)
# need a logged-in session. Drop a Netscape "cookies.txt" next to this script and it is
# picked up automatically - no flag needed. Override with --cookies, disable with --no-cookies.
#
# Cookies are NOT sent on the first attempt: an authenticated request makes YouTube demand
# a JS "n-signature" challenge for every video, which slows down (and can break) the ~95%
# of tracks that need no login. So we download clean first, and only retry WITH cookies
# when the failure is specifically an age/sign-in wall.
COOKIE_FILE = None
DEFAULT_COOKIE_FILE = SCRIPT_DIR / "cookies.txt"

# Error text that means "this needs a logged-in account" - the only case worth retrying.
AUTH_ERROR_MARKERS = (
    "sign in to confirm your age",
    "confirm your age",
    "age-restricted",
    "age restricted",
    "inappropriate for some users",
    "sign in to confirm you're not a bot",
    "login required",
    "private video",
    "members-only",
)


# ======================================================================
#  Data model
# ======================================================================
@dataclass
class Track:
    title: str
    artist: str                      # primary artist (for searching / filenames)
    all_artists: str = ""            # full "A;B;C" credit (for tags)
    album: str = ""
    date: str = ""                   # 4-digit year
    genre: str = ""
    duration_s: Optional[int] = None # expected length in seconds, if known
    track_no: str = ""
    playlist_genre: str = ""         # dominant genre of the CSV this row came from
    record_label: str = ""           # record label   -> TPUB  (NB: `label` is the display name)
    tempo: str = ""                  # BPM            -> TBPM
    key: str = ""                    # e.g. "Am"      -> TKEY
    explicit: bool = False           # -> TXXX:ITUNESADVISORY (the "E" badge on iPhone)

    @property
    def query(self) -> str:
        # Used only for searching. YouTube treats punctuation literally, so a title like
        # "F*ck Love" or "P!nk" returns ZERO results unless we strip the symbols first.
        raw = f"{self.artist} {self.title}"
        cleaned = re.sub(r"[^\w\s]", " ", raw, flags=re.UNICODE)
        return re.sub(r"\s+", " ", cleaned).strip()

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}".strip(" -")


@dataclass
class Stats:
    total: int = 0
    audio_ok: int = 0
    video_ok: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list = field(default_factory=list)


# ======================================================================
#  Helpers
# ======================================================================
def log(msg: str) -> None:
    # tqdm.write keeps the line from colliding with an active progress bar.
    tqdm.write(f"[{time.strftime('%H:%M:%S')}] {msg}")


# Windows refuses any path over 260 characters unless LongPathsEnabled is turned on, and
# yt-dlp appends temp suffixes (".f251.webm.part") while downloading. Some playlist names
# are enormous, so budget the path: cap the folder, then fit the filename in what's left.
WINDOWS_PATH_LIMIT = 250     # leave margin under 260 for yt-dlp's temp suffixes
COLLECTION_MAX     = 60      # chars allowed for the <csv name> folder
YTDLP_SUFFIX_ROOM  = 24      # ".f251.webm.part" and friends


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = re.sub(r'[\\/:"*?<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:max_len] if len(name) > max_len else name


def safe_collection(name: str) -> str:
    """Folder name for a CSV. Long playlist titles are truncated, with a short hash so two
    playlists that share a long prefix can't collapse into the same folder."""
    clean = sanitize_filename(name, max_len=10_000)
    if len(clean) <= COLLECTION_MAX:
        return clean
    digest = hashlib.sha1(clean.encode("utf-8")).hexdigest()[:6]
    return f"{clean[:COLLECTION_MAX - 7].rstrip(' _-.')}_{digest}"


def fit_filename(directory: Path, stem: str, ext: str) -> str:
    """Trim the file's stem so directory + name + yt-dlp's temp suffix stays under the limit.
    When trimming is needed, append a short hash of the full stem so two different long
    titles in the same folder can't collapse onto the same filename."""
    budget = WINDOWS_PATH_LIMIT - len(str(directory)) - 1 - len(ext) - YTDLP_SUFFIX_ROOM
    budget = max(budget, 24)
    if len(stem) <= budget:
        return stem
    digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:6]
    return f"{stem[:budget - 7].rstrip(' _-.')}_{digest}"


def normalize(text: str) -> str:
    """Lower-case, drop noise words and punctuation for fuzzy comparison."""
    text = text.lower()
    text = re.sub(
        r"\b(official|video|audio|lyrics?|lyric|visualizer|hd|hq|mv|"
        r"remaster(ed)?|explicit|clean|feat|ft|prod)\b",
        " ", text)
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# A featured-artist credit is just a different way of naming the same song, e.g.
#   "King Promise - Terminator"  ==  "King Promise - Terminator (feat. Young Jonn)"
# so it is stripped before a track is keyed. Note we only remove a bare trailing
# "feat/ft/featuring" - never a bare "with", or titles like "Come Along with Me" would break.
_FEAT_PATTERNS = (
    re.compile(r"\((?:feat|ft|featuring|with)\b[^)]*\)", re.I),      # (feat. X) / (with X)
    re.compile(r"\[(?:feat|ft|featuring|with)\b[^\]]*\]", re.I),     # [feat. X]
    re.compile(r"\s[-–—]\s(?:feat|ft|featuring)\b.*$", re.I),   # - feat X
    re.compile(r"\s(?:feat|ft|featuring)\.?\s.*$", re.I),            # trailing feat X
)


def strip_features(text: str) -> str:
    for pattern in _FEAT_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def song_key(artist: str, title: str) -> str:
    """Folder- and credit-independent identity for a song."""
    return normalize(strip_features(f"{artist} {title}"))


def song_key_from_stem(stem: str) -> str:
    """Same identity, derived from an 'Artist - Title' filename."""
    return normalize(strip_features(stem))


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def parse_hms(text: str) -> Optional[int]:
    """'3:45' -> 225 ; '1:02:03' -> 3723 ; anything unparseable -> None."""
    text = text.strip()
    if not re.fullmatch(r"(\d+:)?\d{1,2}:\d{2}", text):
        return None
    parts = [int(p) for p in text.split(":")]
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def human_pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def now_stamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ======================================================================
#  Download history (JSON ledger + on-disk scan)
# ======================================================================
class DownloadHistory:
    """Remembers every completed download so nothing is fetched twice.

    Two independent layers, either of which is enough to trigger a skip:
      1. the JSON ledger  - survives renaming or reshuffling subfolders
      2. a scan of the output tree - catches files that were downloaded
         before the ledger existed, in whatever subfolder they now sit
    """

    def __init__(self, path: Path, out_dir: Path):
        self.path = path
        self.out_dir = out_dir
        self.data: dict = {"version": 1, "updated_at": None, "tracks": {}}
        self._lock = threading.RLock()          # guards data/disk/inflight for parallel runs
        self._inflight: set[tuple] = set()       # (key, kind) currently being downloaded
        self._load()
        self._disk = self._scan_disk()

    @staticmethod
    def key(track: Track) -> str:
        """Folder- and credit-independent identity for a track."""
        return song_key(track.artist, track.title)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded.get("tracks"), dict):
                self.data = loaded
        except Exception as exc:
            log(f"Ledger unreadable ({exc}); starting a fresh one.")

    def _scan_disk(self) -> dict:
        """Map normalized name -> {'audio': Path, 'video': Path} for files already present."""
        found: dict = {}
        for kind, ext in (("audio", ".mp3"), ("video", ".mp4")):
            root = self.out_dir / kind
            if not root.exists():
                continue
            for f in root.rglob(f"*{ext}"):
                if f.stat().st_size > 0:
                    found.setdefault(song_key_from_stem(f.stem), {})[kind] = f
        return found

    def has(self, track: Track, kind: str) -> Optional[Path]:
        """Return the existing file for this track+kind (any subfolder), else None."""
        with self._lock:
            k = self.key(track)
            entry = self.data["tracks"].get(k, {}).get(kind)
            if entry:
                p = Path(entry["path"])
                if p.exists() and p.stat().st_size > 0:
                    return p
            disk = self._disk.get(k, {}).get(kind)
            if disk and disk.exists() and disk.stat().st_size > 0:
                return disk
            return None

    def claim(self, track: Track, want_audio: bool, want_video: bool) -> tuple[bool, bool]:
        """Atomically decide what still needs downloading AND reserve it, so two
        parallel workers never grab the same song. Returns (need_audio, need_video);
        reserved kinds must be release()d when done."""
        with self._lock:
            k = self.key(track)
            need_a = (want_audio and self.has(track, "audio") is None
                      and (k, "audio") not in self._inflight)
            need_v = (want_video and self.has(track, "video") is None
                      and (k, "video") not in self._inflight)
            if need_a:
                self._inflight.add((k, "audio"))
            if need_v:
                self._inflight.add((k, "video"))
            return need_a, need_v

    def release(self, track: Track, kind: str) -> None:
        with self._lock:
            self._inflight.discard((self.key(track), kind))

    def record(self, track: Track, kind: str, path: Path,
               collection: str, source: str) -> None:
        with self._lock:
            k = self.key(track)
            entry = self.data["tracks"].setdefault(k, {})
            entry["label"] = track.label
            entry[kind] = {
                "path": str(path.resolve()),
                "collection": collection,
                "source": source,
                "size_bytes": path.stat().st_size if path.exists() else 0,
                "downloaded_at": now_stamp(),
            }
            self._disk.setdefault(k, {})[kind] = path
            self.save()

    def reindex(self) -> int:
        """Rebuild the ledger from what is actually on disk. Entries are re-keyed with the
        current song_key, and files that no longer exist are dropped. Whatever we already
        knew about a surviving file (its source URL, when it was fetched) is carried over."""
        with self._lock:
            # remember what we knew, keyed by the file path it referred to
            old_by_path = {}
            for entry in self.data.get("tracks", {}).values():
                for kind in ("audio", "video"):
                    info = entry.get(kind)
                    if isinstance(info, dict) and info.get("path"):
                        old_by_path[os.path.normcase(info["path"])] = info

            tracks: dict = {}
            count = 0
            for key, kinds in self._disk.items():
                entry = tracks.setdefault(key, {})
                for kind, path in kinds.items():
                    stat = path.stat()
                    resolved = str(path.resolve())
                    prior = old_by_path.get(os.path.normcase(resolved), {})
                    entry.setdefault("label", path.stem)
                    entry[kind] = {
                        "path": resolved,
                        "collection": path.parent.name,
                        "source": prior.get("source", "pre-existing file (reindexed)"),
                        "size_bytes": stat.st_size,
                        "downloaded_at": prior.get("downloaded_at") or datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc).astimezone().isoformat(timespec="seconds"),
                    }
                    count += 1
            self.data["tracks"] = tracks
            self.save()
            return count

    def save(self) -> None:
        with self._lock:
            self.data["updated_at"] = now_stamp()
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                               encoding="utf-8")
                tmp.replace(self.path)   # atomic: a crash never leaves a half-written ledger
            except Exception as exc:
                log(f"Could not write ledger: {exc}")


# ======================================================================
#  yt-dlp progress -> tqdm bar
# ======================================================================
class YtdlpProgressBar:
    """Turns yt-dlp's progress callbacks into a tqdm byte bar (one per stream)."""

    def __init__(self, label: str):
        self.label = label
        self.bar: Optional[tqdm] = None
        self.current: Optional[str] = None

    def __call__(self, d: dict) -> None:
        status = d.get("status")
        if status == "downloading":
            filename = d.get("filename")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if self.bar is None or filename != self.current:
                self.close()
                self.current = filename
                self.bar = tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024,
                                desc=f"   {self.label[:40]}", leave=False,
                                dynamic_ncols=True)
            if total and self.bar.total != total:
                self.bar.total = total
            done = d.get("downloaded_bytes") or 0
            self.bar.update(max(0, done - self.bar.n))
        elif status == "finished":
            self.close()

    def close(self) -> None:
        if self.bar is not None:
            self.bar.close()
            self.bar = None


# ======================================================================
#  CSV loading (flexible column detection)
# ======================================================================
_COLUMN_ALIASES = {
    "title":    ["track name", "title", "song", "song name", "name", "track"],
    "artist":   ["artist name(s)", "artist name", "artists", "artist"],
    "album":    ["album name", "album"],
    "date":     ["release date", "date", "year"],
    "genre":    ["genres", "genre"],
    "duration": ["duration (ms)", "duration ms", "duration"],
    "track_no": ["track number", "track no", "#"],
    # extra columns the Spotify export already gives us, previously unused
    "label":    ["record label", "label", "publisher"],
    "tempo":    ["tempo", "bpm"],
    "key":      ["key"],
    "mode":     ["mode"],
    "explicit": ["explicit"],
}

# Spotify stores the key as a pitch class (0=C .. 11=B) and mode as 1=major / 0=minor.
_PITCH_CLASSES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def musical_key(key: str, mode: str) -> str:
    """'9' + '0' -> 'Am'  (the TKEY frame the iOS Music app reads)."""
    try:
        k = int(str(key).strip())
    except (TypeError, ValueError):
        return ""
    if not 0 <= k <= 11:
        return ""
    name = _PITCH_CLASSES[k]
    return name if str(mode).strip() == "1" else f"{name}m"


def _safe_bpm(value) -> str:
    try:
        bpm = round(float(str(value).strip()))
    except (TypeError, ValueError):
        return ""
    return str(bpm) if 20 <= bpm <= 300 else ""


def sort_name(name: str) -> str:
    """'The Weeknd' -> 'Weeknd, The', so it files under W rather than T."""
    if not name:
        return ""
    m = re.match(r"^(The|A|An)\s+(.*)$", name.strip(), re.I)
    return f"{m.group(2)}, {m.group(1)}" if m else name.strip()


def _match_column(fieldnames: list[str], aliases: list[str]) -> Optional[str]:
    lowered = {fn.lower().strip(): fn for fn in fieldnames}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    # loose contains-match as a last resort
    for alias in aliases:
        for low, original in lowered.items():
            if alias in low:
                return original
    return None


def load_tracks(csv_path: Path) -> list[Track]:
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"{csv_path.name} has no header row.")

        cols = {key: _match_column(reader.fieldnames, aliases)
                for key, aliases in _COLUMN_ALIASES.items()}
        if not cols["title"]:
            raise ValueError(
                f"Could not find a title column in {csv_path.name}. "
                f"Columns present: {reader.fieldnames}")

        tracks: list[Track] = []
        for row in reader:
            title = (row.get(cols["title"]) or "").strip()
            if not title:
                continue
            all_artists = (row.get(cols["artist"]) or "").strip() if cols["artist"] else ""
            primary = re.split(r"[;,/]| feat| ft ", all_artists, maxsplit=1)[0].strip()

            duration_s = None
            if cols["duration"]:
                raw = (row.get(cols["duration"]) or "").strip()
                if raw.isdigit():
                    val = int(raw)
                    duration_s = val // 1000 if val > 10000 else val  # ms vs s heuristic

            date = (row.get(cols["date"]) or "").strip()[:4] if cols["date"] else ""
            genre = ""
            if cols["genre"]:
                genre = (row.get(cols["genre"]) or "").strip().split(",")[0].strip()

            tracks.append(Track(
                title=title,
                artist=primary or "Unknown Artist",
                all_artists=all_artists or primary,
                album=(row.get(cols["album"]) or "").strip() if cols["album"] else "",
                date=date,
                genre=genre,
                duration_s=duration_s,
                track_no=(row.get(cols["track_no"]) or "").strip() if cols["track_no"] else "",
                record_label=(row.get(cols["label"]) or "").strip() if cols["label"] else "",
                tempo=_safe_bpm(row.get(cols["tempo"])) if cols["tempo"] else "",
                key=(musical_key(row.get(cols["key"], ""), row.get(cols["mode"], ""))
                     if cols["key"] else ""),
                explicit=((row.get(cols["explicit"]) or "").strip().lower() == "true"
                          if cols["explicit"] else False),
            ))

        # A playlist has a character: most of "Bongo_Flava" really is bongo flava. Use the
        # CSV's dominant genre as the last-resort fallback for its own rows that have none,
        # which is how local tracks no catalogue knows still get a sensible genre.
        counts: dict = {}
        for t in tracks:
            g = canonical_genre(t.genre)
            if g:
                counts[g] = counts.get(g, 0) + 1
        dominant = max(counts, key=counts.get) if counts else ""
        for t in tracks:
            t.playlist_genre = dominant
        return tracks


# ======================================================================
#  Browser (Selenium) with human-like typing
# ======================================================================
class YouTubeSearcher:
    """Opens a real browser window and searches YouTube like a person would."""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self.driver = None
        self.kind = None  # "chrome" | "edge"

    def start(self) -> bool:
        from selenium import webdriver

        for kind in BROWSER_PREFERENCE:
            try:
                if kind == "chrome":
                    from selenium.webdriver.chrome.options import Options
                    from selenium.webdriver.chrome.service import Service as ChromeService
                    opts = self._common_options(Options())
                    self.driver = webdriver.Chrome(options=opts, service=ChromeService())
                else:
                    from selenium.webdriver.edge.options import Options
                    from selenium.webdriver.edge.service import Service as EdgeService
                    opts = self._common_options(Options())
                    self.driver = webdriver.Edge(options=opts, service=EdgeService())

                # Hide the obvious webdriver flag.
                try:
                    self.driver.execute_cdp_cmd(
                        "Page.addScriptToEvaluateOnNewDocument",
                        {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
                    )
                except Exception:
                    pass

                self.kind = kind
                log(f"Browser started: {kind}")
                return True
            except Exception as exc:
                log(f"Could not start {kind}: {exc}")
                self.driver = None
        return False

    def _common_options(self, opts):
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--start-maximized")
        opts.add_argument("--lang=en-US")
        opts.add_argument(
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")
        try:
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)
        except Exception:
            pass
        return opts

    def _dismiss_consent(self):
        """Click a YouTube/Google cookie-consent button if one appears."""
        from selenium.webdriver.common.by import By
        for xpath in (
            "//button[.//span[contains(text(),'Reject all')]]",
            "//button[.//span[contains(text(),'Accept all')]]",
            "//button[contains(@aria-label,'Reject')]",
            "//button[contains(@aria-label,'Accept')]",
        ):
            try:
                btns = self.driver.find_elements(By.XPATH, xpath)
                if btns:
                    btns[0].click()
                    human_pause(0.8, 1.5)
                    return
            except Exception:
                continue

    def _type_like_human(self, element, text: str) -> None:
        for ch in text:
            element.send_keys(ch)
            if random.random() < THINK_PAUSE_PROB:
                human_pause(THINK_PAUSE_MIN, THINK_PAUSE_MAX)
            else:
                human_pause(TYPE_DELAY_MIN, TYPE_DELAY_MAX)

    def search(self, track: Track, max_results: int = 8) -> Optional[str]:
        """Return the best-matching watch URL, or None."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        driver = self.driver
        try:
            driver.get("https://www.youtube.com/")
            self._dismiss_consent()

            box = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((By.NAME, "search_query")))
            box.click()
            human_pause(0.3, 0.8)
            box.clear()
            self._type_like_human(box, track.query)
            human_pause(0.3, 0.9)         # small pause before committing, like a person
            box.send_keys(Keys.ENTER)

            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "ytd-video-renderer")))
            human_pause(1.0, 2.0)
            return self._pick_best(track, max_results)
        except Exception as exc:
            log(f"  Browser search failed: {exc}")
            return None

    def _pick_best(self, track: Track, max_results: int) -> Optional[str]:
        from selenium.webdriver.common.by import By

        renderers = self.driver.find_elements(By.CSS_SELECTOR, "ytd-video-renderer")[:max_results]
        best_url, best_score = None, -1.0

        for r in renderers:
            try:
                link = r.find_element(By.CSS_SELECTOR, "a#video-title")
                href = link.get_attribute("href") or ""
                title = (link.get_attribute("title") or link.text or "").strip()
                if "watch?v=" not in href:
                    continue

                dur = None
                try:
                    badge = r.find_element(
                        By.CSS_SELECTOR,
                        "ytd-thumbnail-overlay-time-status-renderer #text, "
                        "span.ytd-thumbnail-overlay-time-status-renderer")
                    dur = parse_hms(badge.text)
                except Exception:
                    pass

                score = 0.65 * similarity(track.title, title)
                score += 0.20 * similarity(track.artist, title)
                if track.duration_s and dur:
                    diff = abs(dur - track.duration_s)
                    score += 0.15 * max(0.0, 1.0 - diff / DURATION_TOLERANCE)
                    if dur > track.duration_s * 2 + 60:   # avoid hour-long mixes
                        score -= 0.5

                if score > best_score:
                    best_score, best_url = score, href.split("&")[0]
            except Exception:
                continue

        if best_url:
            log(f"  Matched (score {best_score:.2f}): {best_url}")
        return best_url

    def stop(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None


# ======================================================================
#  Downloading (yt-dlp) + tagging (mutagen)
# ======================================================================
def find_ffmpeg() -> Optional[str]:
    exe = shutil.which("ffmpeg")
    if exe:
        return str(Path(exe).parent)
    # common winget install location
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if base.exists():
        hits = list(base.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
        if hits:
            return str(hits[0].parent)
    return None


FFMPEG_DIR = find_ffmpeg()


def find_deno() -> Optional[str]:
    """Locate the Deno JS runtime that yt-dlp uses to solve YouTube's JS challenge
    (required for age-restricted videos). Checks PATH, then the winget install spot."""
    exe = shutil.which("deno")
    if exe:
        return str(Path(exe).parent)
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for cand in (
        local / "Microsoft/WinGet/Links/deno.exe",
        Path(os.environ.get("USERPROFILE", "")) / ".deno/bin/deno.exe",
    ):
        if cand.exists():
            return str(cand.parent)
    pkgs = local / "Microsoft/WinGet/Packages"
    if pkgs.exists():
        hits = list(pkgs.glob("DenoLand.Deno*/**/deno.exe")) or list(pkgs.glob("*/deno.exe"))
        if hits:
            return str(hits[0].parent)
    return None


DENO_DIR = find_deno()
# Make sure yt-dlp can find Deno even if this shell's PATH predates the install.
if DENO_DIR and DENO_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = DENO_DIR + os.pathsep + os.environ.get("PATH", "")


def _clear_stale_parts(dest: Path) -> None:
    """Delete leftover .part / .ytdl fragments for this track. Resuming a stale fragment
    from an earlier interrupted run is what triggers 'HTTP Error 416: range not
    satisfiable', so we start each download clean."""
    parent = dest.parent
    if not parent.exists():
        return
    stem = dest.stem
    try:
        for f in parent.glob(f"{stem}.*"):
            if f.suffix in (".part", ".ytdl") or ".part-Frag" in f.name or f.name.endswith(".part"):
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


class _ErrCollector:
    """Captures yt-dlp's error text instead of letting it spray raw ERROR: lines at the
    console. We inspect the text to decide whether a cookie retry is worth doing."""

    def __init__(self):
        self.errors: list[str] = []

    def debug(self, msg):   pass
    def info(self, msg):    pass
    def warning(self, msg): pass

    def error(self, msg):
        self.errors.append(str(msg))

    @property
    def text(self) -> str:
        return " | ".join(self.errors).lower()


def _base_ydl_opts(outtmpl: str, use_cookies: bool) -> dict:
    opts = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,      # tqdm draws the bar instead
        "ignoreerrors": True,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "continuedl": False,     # don't resume old .part files -> avoids HTTP 416 range errors
        "restrictfilenames": False,
        "windowsfilenames": True,
        # Let yt-dlp fetch its EJS challenge-solver script so Deno can solve YouTube's
        # JS "n-signature" when a video requires it. Only used when a challenge is hit.
        "remote_components": ["ejs:github"],
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    if use_cookies and COOKIE_FILE:
        opts["cookiefile"] = str(COOKIE_FILE)
    return opts


def _ydl_opts_for(kind: str, outtmpl: str, use_cookies: bool, bar) -> dict:
    opts = _base_ydl_opts(outtmpl, use_cookies)
    if kind == "audio":
        opts.update({
            "format": "bestaudio/best",
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": AUDIO_QUALITY},
                {"key": "FFmpegMetadata"},
                {"key": "EmbedThumbnail"},
            ],
        })
    else:
        opts.update({
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "postprocessors": [{"key": "FFmpegMetadata"}],
        })
    if bar:
        opts["progress_hooks"] = [bar]
    return opts


def _attempt(source: str, dest: Path, kind: str, use_cookies: bool,
             label: str, show_bar: bool) -> tuple:
    """One download attempt. Returns (ok, error_text)."""
    import yt_dlp
    _clear_stale_parts(dest)          # a previous failed try can leave a dead fragment
    collector = _ErrCollector()
    bar = YtdlpProgressBar(label or dest.stem) if show_bar else None
    opts = _ydl_opts_for(kind, str(dest.with_suffix(".%(ext)s")), use_cookies, bar)
    opts["logger"] = collector
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download([source])
        return (code == 0 and dest.exists()), collector.text
    except Exception as exc:
        return False, f"{collector.text} | {exc}".lower()
    finally:
        if bar:
            bar.close()


def _needs_login(error_text: str) -> bool:
    return any(marker in error_text for marker in AUTH_ERROR_MARKERS)


def _download(source: str, dest: Path, kind: str, label: str, show_bar: bool) -> bool:
    """Clean pass first. Retry with cookies ONLY when the video actually demands a
    login (age-restricted etc.) - cookies cannot fix a 416 or a network error."""
    name = label or dest.stem
    ok, err = _attempt(source, dest, kind, False, label, show_bar)
    if ok:
        return True

    if _needs_login(err):
        if COOKIE_FILE:
            log(f"  age-restricted -> retrying {kind} with cookies: {name}")
            ok, err = _attempt(source, dest, kind, True, label, show_bar)
            if ok:
                log(f"  unlocked with cookies: {name}")
                return True
            log(f"  {kind} still blocked after cookies (cookies may be stale): {name}")
        else:
            log(f"  {kind} needs a login (age-restricted), no cookies.txt: {name}")
        return False

    if not err:
        # yt-dlp exited cleanly but produced no file: nothing matched the query.
        log(f"  {kind} failed: {name} -> nothing downloaded (no match for this query)")
        return False
    log(f"  {kind} failed: {name} -> {err.split(' | ')[0][:150]}")
    return False


def download_audio(source: str, dest_mp3: Path, label: str = "", show_bar: bool = True) -> bool:
    return _download(source, dest_mp3, "audio", label, show_bar)


def download_video(source: str, dest_mp4: Path, label: str = "", show_bar: bool = True) -> bool:
    return _download(source, dest_mp4, "video", label, show_bar)


# ======================================================================
#  Square cover art (iTunes -> Deezer), the same square covers Apple Music uses
# ======================================================================
ART_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MusicArt/1.0"
_cover_cache: dict = {}
_cover_lock = threading.Lock()
_itunes_cache: dict = {}
_itunes_cache_lock = threading.Lock()
_itunes_lock = threading.Lock()
_itunes_last = [0.0]
_itunes_fails = [0]       # consecutive refusals
_itunes_down = [False]    # tripped breaker: stop asking iTunes for the rest of this run

# Genres that carry no information. YouTube tags every music video as "Music", and
# ffmpeg's metadata step copies that straight into the file - it is worse than nothing.
JUNK_GENRES = {"", "music", "other", "unknown", "none", "misc", "general"}

# Double-barrelled genres get collapsed to a single one. Most can just be cut at the "/",
# but a few would turn into nonsense that way ("Singer/Songwriter" -> "Singer"), so those
# are spelled out. Keys are lower-case; values are what actually gets written.
GENRE_ALIASES = {
    "r&b/soul": "R&B",
    "hip-hop/rap": "Hip-Hop",
    "hip hop/rap": "Hip-Hop",
    "dance/electronic": "Dance",
    "singer/songwriter": "Singer-Songwriter",
    "pop/rock": "Pop",
    "rock/pop": "Rock",
    "folk/acoustic": "Folk",
    "soundtrack/score": "Soundtrack",
    "christian & gospel": "Christian",
    "christian/gospel": "Christian",
    "reggae/dancehall": "Reggae",
    "latin/world": "Latin",
    "jazz/blues": "Jazz",
}


# Casing is normalised too, otherwise the CSVs' "rap" and Deezer's "Rap" show up as two
# separate genres in a music app. These keep their conventional spelling.
GENRE_CASING = {
    "r&b": "R&B", "edm": "EDM", "idm": "IDM", "dj": "DJ", "uk drill": "UK Drill",
    "uk garage": "UK Garage", "k-pop": "K-Pop", "j-pop": "J-Pop", "p-pop": "P-Pop",
    "r&b/soul": "R&B", "hip-hop": "Hip-Hop", "hip hop": "Hip-Hop", "lo-fi": "Lo-Fi",
    "ccm": "CCM", "emo": "Emo", "rnb": "R&B",
    # merge spellings that would otherwise become two near-identical genre playlists
    "bongo-flava": "Bongo Flava", "bongoflava": "Bongo Flava",
    "afro-beat": "Afrobeats", "afrobeat": "Afrobeats", "afro beat": "Afrobeats",
    "afro-pop": "Afro-Pop", "afropop": "Afro-Pop", "afro pop": "Afro-Pop",
    "afro-fusion": "Afro-Fusion", "afrofusion": "Afro-Fusion",
    "afro-soul": "Afro Soul", "r and b": "R&B", "rhythm and blues": "R&B",
    "roots reggae": "Roots Reggae", "folk-rock": "Folk-Rock", "folk rock": "Folk-Rock",
}


def _title_case(text: str) -> str:
    """Title-case a genre, respecting hyphens ('hip-hop' -> 'Hip-Hop')."""
    parts = []
    for word in text.split():
        parts.append("-".join(w[:1].upper() + w[1:] if w else w for w in word.split("-")))
    return " ".join(parts)


def canonical_genre(genre: Optional[str]) -> Optional[str]:
    """Exactly one genre, consistently spelled.
    'R&B/Soul' -> 'R&B',  'afrobeats,afrobeat' -> 'Afrobeats',  'rap' -> 'Rap'."""
    if not genre:
        return None
    g = re.sub(r"\s+", " ", str(genre)).strip()
    if not g or g.lower() in JUNK_GENRES:
        return None
    alias = GENRE_ALIASES.get(g.lower())
    if alias:
        return alias
    # keep only the first genre listed ("Christmas: Classic" -> "Christmas")
    g = re.split(r"\s*[/,;|:]\s*", g)[0].strip()
    if not g or g.lower() in JUNK_GENRES:
        return None
    return GENRE_CASING.get(g.lower()) or _title_case(g)


def _itunes_best(artist: str, title: str, album: str = "") -> Optional[dict]:
    """The best-matching iTunes song for this track, cached. One call serves BOTH the
    square cover art and the genre, so asking for the genre costs no extra request.

    iTunes rate-limits at roughly 20 requests/minute and answers HTTP 429 past that. We
    pace ourselves accordingly and back off when told to. Crucially, a 429 or a network
    error is NOT cached: caching it would permanently mark a findable track as "no match"
    and silently push it onto a weaker genre source.
    """
    if _itunes_down[0]:                       # circuit breaker: Apple is refusing us
        return None

    key = song_key(artist, title)
    with _itunes_cache_lock:
        if key in _itunes_cache:
            return _itunes_cache[key]

    result, cacheable = None, False
    for attempt in range(ITUNES_MAX_RETRIES):
        with _itunes_lock:                    # global pacing across all worker threads
            gap = time.time() - _itunes_last[0]
            if gap < ITUNES_MIN_INTERVAL:
                time.sleep(ITUNES_MIN_INTERVAL - gap)
            _itunes_last[0] = time.time()
        try:
            term = requests.utils.quote(f"{artist} {title}")
            r = requests.get(f"https://itunes.apple.com/search?term={term}&entity=song&limit=6",
                             headers={"User-Agent": ART_UA}, timeout=15)
            if r.status_code == 429:          # rate limited - wait longer and try again
                time.sleep(ITUNES_BACKOFF * (attempt + 1))
                continue
            if r.status_code != 200:
                # 403 = Apple has blocked this IP for a while. Don't sit through a 3s
                # pacing wait before every doomed call; trip the breaker and use Deezer.
                with _itunes_cache_lock:
                    _itunes_fails[0] += 1
                    if _itunes_fails[0] >= ITUNES_FAIL_LIMIT and not _itunes_down[0]:
                        _itunes_down[0] = True
                        log(f"iTunes is refusing requests (HTTP {r.status_code}) - skipping it "
                            "for this run and using Deezer / MusicBrainz instead.")
                break
            best, best_score = None, 0.0
            for it in r.json().get("results", []):
                score = (0.6 * similarity(title, it.get("trackName", ""))
                         + 0.4 * similarity(artist, it.get("artistName", "")))
                if album:
                    score += 0.1 * similarity(album, it.get("collectionName", ""))
                if score > best_score:
                    best, best_score = it, score
            if best and best_score >= ART_MIN_SCORE:
                result = best
            cacheable = True                  # a real answer (hit or genuine miss)
            with _itunes_cache_lock:
                _itunes_fails[0] = 0          # healthy again
            break
        except Exception:
            break

    if cacheable:                             # never cache a rate-limit / network failure
        with _itunes_cache_lock:
            _itunes_cache[key] = result
    return result


def _itunes_cover_url(artist: str, title: str, album: str) -> Optional[str]:
    best = _itunes_best(artist, title, album)
    if best and best.get("artworkUrl100"):
        # upgrade the tiny 100x100 default to a big square cover
        return best["artworkUrl100"].replace("100x100bb", f"{ART_MAX_EDGE}x{ART_MAX_EDGE}bb")
    return None


def fetch_genre(artist: str, title: str, album: str = "") -> Optional[str]:
    """iTunes' per-track genre (e.g. 'R&B/Soul' -> 'R&B')."""
    best = _itunes_best(artist, title, album)
    return canonical_genre((best or {}).get("primaryGenreName"))


def lookup_genre(artist: str, title: str, album: str = "",
                 fallback: Optional[str] = None) -> Optional[str]:
    """Every genre source in order of trustworthiness, stopping at the first hit:
        iTunes  ->  Deezer  ->  MusicBrainz  ->  the playlist's own dominant genre.
    The last one is what guarantees local tracks (Ugandan, Bongo) that no Western
    catalogue carries still end up with a sensible genre rather than nothing."""
    for candidate in (
        lambda: (_itunes_best(artist, title, album) or {}).get("primaryGenreName"),
        lambda: _deezer_genre(artist, title),
        lambda: _musicbrainz_genre(artist, title),
        lambda: fallback,
    ):
        try:
            genre = canonical_genre(candidate())
        except Exception:
            genre = None
        if genre:
            return genre
    return None


def resolve_genre(track: Track) -> Optional[str]:
    """CSV genre first (most trustworthy), then every online source, then the playlist."""
    own = canonical_genre(track.genre)
    if own:
        return own
    return lookup_genre(track.artist, track.title, track.album,
                        fallback=track.playlist_genre)


_deezer_cache: dict = {}
_deezer_lock = threading.Lock()
_album_genre_cache: dict = {}
_mb_lock = threading.Lock()
_mb_last = [0.0]


def _deezer_best(artist: str, title: str) -> Optional[dict]:
    """Best-matching Deezer track, cached. Serves both cover art and genre."""
    key = song_key(artist, title)
    with _deezer_lock:
        if key in _deezer_cache:
            return _deezer_cache[key]
    result = None
    try:
        q = requests.utils.quote(f'artist:"{artist}" track:"{title}"')
        r = requests.get(f"https://api.deezer.com/search?q={q}&limit=6",
                         headers={"User-Agent": ART_UA}, timeout=15)
        if r.status_code == 200:
            best, best_score = None, 0.0
            for tr in r.json().get("data", []):
                score = (0.6 * similarity(title, tr.get("title", ""))
                         + 0.4 * similarity(artist, tr.get("artist", {}).get("name", "")))
                if score > best_score:
                    best, best_score = tr, score
            if best and best_score >= ART_MIN_SCORE:
                result = best
    except Exception:
        result = None
    with _deezer_lock:
        _deezer_cache[key] = result
    return result


def _deezer_cover_url(artist: str, title: str, album: str) -> Optional[str]:
    best = _deezer_best(artist, title)
    alb = (best or {}).get("album", {})
    return alb.get("cover_xl") or alb.get("cover_big") or None   # square, 1000x1000 / 500x500


def _deezer_genre(artist: str, title: str) -> Optional[str]:
    """Deezer keeps the genre on the ALBUM, so we look the album up once and cache it."""
    best = _deezer_best(artist, title)
    album_id = (best or {}).get("album", {}).get("id")
    if not album_id:
        return None
    with _deezer_lock:
        if album_id in _album_genre_cache:
            return _album_genre_cache[album_id]
    genre = None
    try:
        r = requests.get(f"https://api.deezer.com/album/{album_id}",
                         headers={"User-Agent": ART_UA}, timeout=15)
        if r.status_code == 200:
            entries = (r.json().get("genres") or {}).get("data") or []
            if entries:
                genre = entries[0].get("name")
    except Exception:
        genre = None
    with _deezer_lock:
        _album_genre_cache[album_id] = genre
    return genre


# --- album track listings (used by --fill-albums to add more songs from an album) ---
_album_tracks_cache: dict = {}
_album_tracks_lock = threading.Lock()


def album_tracks(artist: str, title: str, album: str) -> list:
    """Return the other songs on the album a track belongs to, as
    [(track_no, song_title, track_artist, duration_s, genre, explicit)]. iTunes first
    (real album grouping), Deezer as fallback. Cached per album."""
    seed = _itunes_best(artist, title, album)
    cid = (seed or {}).get("collectionId")
    key = f"it:{cid}" if cid else None

    if key:
        with _album_tracks_lock:
            if key in _album_tracks_cache:
                return _album_tracks_cache[key]

    out: list = []
    if cid and not _itunes_down[0]:
        try:
            with _itunes_lock:                       # obey the same iTunes pacing/limit
                gap = time.time() - _itunes_last[0]
                if gap < ITUNES_MIN_INTERVAL:
                    time.sleep(ITUNES_MIN_INTERVAL - gap)
                _itunes_last[0] = time.time()
            r = requests.get(f"https://itunes.apple.com/lookup?id={cid}&entity=song&limit=200",
                             headers={"User-Agent": ART_UA}, timeout=15)
            if r.status_code == 200:
                for s in r.json().get("results", []):
                    if s.get("wrapperType") != "track" or s.get("kind") != "song":
                        continue
                    out.append((
                        str(s.get("trackNumber") or ""),
                        s.get("trackName") or "",
                        s.get("artistName") or artist,
                        round((s.get("trackTimeMillis") or 0) / 1000) or None,
                        canonical_genre(s.get("primaryGenreName")),
                        s.get("trackExplicitness") == "explicit",
                    ))
        except Exception:
            out = []

    if not out:                                      # Deezer fallback
        db = _deezer_best(artist, title)
        aid = (db or {}).get("album", {}).get("id")
        if aid:
            try:
                r = requests.get(f"https://api.deezer.com/album/{aid}/tracks?limit=100",
                                 headers={"User-Agent": ART_UA}, timeout=15)
                if r.status_code == 200:
                    for s in r.json().get("data", []):
                        out.append((
                            str(s.get("track_position") or ""),
                            s.get("title") or "",
                            (s.get("artist") or {}).get("name") or artist,
                            s.get("duration") or None,
                            None,
                            bool(s.get("explicit_lyrics")),
                        ))
            except Exception:
                out = []

    if key:
        with _album_tracks_lock:
            _album_tracks_cache[key] = out
    return out


def _musicbrainz_genre(artist: str, title: str) -> Optional[str]:
    """Community tags from MusicBrainz. Good for older/reggae catalogue that Apple and
    Deezer miss. MB allows one request per second, so this is throttled hard."""
    with _mb_lock:
        gap = time.time() - _mb_last[0]
        if gap < 1.1:
            time.sleep(1.1 - gap)
        _mb_last[0] = time.time()
    try:
        q = requests.utils.quote(f'recording:"{title}" AND artist:"{artist}"')
        r = requests.get(
            f"https://musicbrainz.org/ws/2/recording/?query={q}&fmt=json&limit=3",
            headers={"User-Agent": "CsvMusicDownloader/1.0 (personal use)"}, timeout=20)
        if r.status_code != 200:
            return None
        for rec in r.json().get("recordings", []):
            tags = rec.get("tags") or []
            if tags:
                best = max(tags, key=lambda t: t.get("count", 0))
                if best.get("count", 0) > 0:
                    return best.get("name")
    except Exception:
        pass
    return None


def _cover_mime(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return "image/jpeg"


def fetch_square_cover(artist: str, title: str, album: str = "") -> Optional[tuple]:
    """Return (image_bytes, mime) for a square album cover, or None. Cached per track."""
    key = normalize(f"{artist} {title}")
    with _cover_lock:
        if key in _cover_cache:
            return _cover_cache[key]
    result = None
    url = _itunes_cover_url(artist, title, album) or _deezer_cover_url(artist, title, album)
    if url:
        try:
            r = requests.get(url, headers={"User-Agent": ART_UA}, timeout=20)
            if r.status_code == 200 and len(r.content) > 3000 and \
               r.headers.get("content-type", "").startswith("image"):
                result = (r.content, _cover_mime(r.content))
        except Exception:
            result = None
    with _cover_lock:
        _cover_cache[key] = result
    return result


def embed_cover_into_file(path: Path, data: bytes, mime: str) -> bool:
    """Replace the embedded picture of an existing MP3 (keeps all other tags)."""
    from mutagen.id3 import ID3, ID3NoHeaderError, APIC
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("APIC")
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        save_id3(tags, path)
        return True
    except Exception as exc:
        log(f"  art embed failed for {path.name}: {exc}")
        return False


def embedded_art_ratio(path: Path) -> Optional[float]:
    """width/height of the currently embedded picture, or None if there is none."""
    from mutagen.id3 import ID3
    import io
    try:
        tags = ID3(path)
    except Exception:
        return None
    apics = [t for t in tags.values() if getattr(t, "FrameID", "") == "APIC"]
    if not apics:
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(apics[0].data)) as im:
            w, h = im.size
        return (w / h) if h else None
    except Exception:
        return None


# --reart records what it did on each file in a TXXX:REART frame ("ok@<time>" or
# "nomatch@<time>"). That lets a later run skip a track it already failed to find art for,
# instead of re-querying iTunes/Deezer for it on every single run.
def reart_state(path: Path) -> tuple:
    """Read the file ONCE and return (is_square, marker) where marker is
    (status, datetime|None) or None. Used to decide whether --reart can skip this file."""
    from mutagen.id3 import ID3
    import io
    try:
        tags = ID3(path)
    except Exception:
        return (False, None)

    is_square = False
    apics = [t for t in tags.values() if getattr(t, "FrameID", "") == "APIC"]
    if apics:
        try:
            from PIL import Image
            with Image.open(io.BytesIO(apics[0].data)) as im:
                w, h = im.size
            is_square = bool(h) and abs(w / h - 1.0) <= ART_SQUARE_TOL
        except Exception:
            is_square = False

    marker = None
    frame = tags.get("TXXX:REART")
    if frame and frame.text and "@" in frame.text[0]:
        status, _, ts = frame.text[0].partition("@")
        when = None
        try:
            when = datetime.fromisoformat(ts)
        except Exception:
            when = None
        marker = (status, when)
    return (is_square, marker)


def stamp_reart(path: Path, status: str) -> None:
    """Record the outcome of a --reart attempt ('ok' or 'nomatch') on the file."""
    from mutagen.id3 import ID3, ID3NoHeaderError, TXXX
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()
        tags.delall("TXXX:REART")
        tags.add(TXXX(encoding=3, desc="REART", text=f"{status}@{now_stamp()}"))
        save_id3(tags, path)
    except Exception:
        pass


def _reart_age_days(when) -> float:
    if when is None:
        return float("inf")
    now = datetime.now(timezone.utc).astimezone()
    if when.tzinfo is None:                       # be safe if a naive stamp slipped in
        when = when.replace(tzinfo=now.tzinfo)
    return (now - when).total_seconds() / 86400.0


def _identity_from_tags(path: Path) -> tuple:
    """Best (artist, title, album) for a file, from its tags then its filename."""
    from mutagen.id3 import ID3
    artist = title = album = ""
    try:
        t = ID3(path)
        title = (t.get("TIT2").text[0] if t.get("TIT2") else "") or ""
        artist = ((t.get("TPE2").text[0] if t.get("TPE2") else "")
                  or (t.get("TPE1").text[0] if t.get("TPE1") else "") or "")
        album = (t.get("TALB").text[0] if t.get("TALB") else "") or ""
    except Exception:
        pass
    if not title or not artist:
        stem = path.stem
        if " - " in stem:
            a, ti = stem.split(" - ", 1)
            artist = artist or a.strip()
            title = title or ti.strip()
        else:
            title = title or stem
    return artist, title, album


# ffmpeg copies YouTube's video blurb into every file. None of it belongs in a music
# library, and some players surface it as the track comment.
_JUNK_TXXX = {"description", "synopsis", "purl", "comment", "compatible_brands",
              "major_brand", "minor_version"}


def strip_youtube_junk(tags) -> None:
    tags.delall("COMM")                       # the YouTube URL, as an ID3v1 comment
    tags.delall("WXXX")                       # "official audio" style URLs
    for frame in [k for k in tags.keys() if k.startswith("TXXX:")]:
        if frame.split(":", 1)[1].strip().lower() in _JUNK_TXXX:
            tags.delall(frame)


def write_full_tags(tags, track: Track, genre: Optional[str]) -> None:
    """Everything the iOS Music app actually reads."""
    from mutagen.id3 import (APIC, TIT2, TPE1, TPE2, TALB, TDRC, TCON, TRCK, TPOS,
                             TPUB, TBPM, TKEY, TCMP, TSOP, TSOA, TSOT, TXXX)

    strip_youtube_junk(tags)

    tags.setall("TIT2", [TIT2(encoding=3, text=track.title)])
    tags.setall("TPE1", [TPE1(encoding=3, text=track.all_artists.replace(";", ", "))])
    tags.setall("TPE2", [TPE2(encoding=3, text=track.artist)])     # album artist: groups albums
    if track.album:
        tags.setall("TALB", [TALB(encoding=3, text=track.album)])
    if track.date:
        tags.setall("TDRC", [TDRC(encoding=3, text=track.date)])
    if genre:
        tags.setall("TCON", [TCON(encoding=3, text=genre)])
    else:
        tags.delall("TCON")

    tags.setall("TRCK", [TRCK(encoding=3, text=track.track_no or "1")])
    tags.setall("TPOS", [TPOS(encoding=3, text="1/1")])            # disc 1 of 1
    tags.setall("TCMP", [TCMP(encoding=3, text="0")])              # not a compilation

    # Sort tags: "The Weeknd" should file under W, not T.
    tags.setall("TSOP", [TSOP(encoding=3, text=sort_name(track.artist))])
    tags.setall("TSOT", [TSOT(encoding=3, text=sort_name(track.title))])
    if track.album:
        tags.setall("TSOA", [TSOA(encoding=3, text=sort_name(track.album))])

    if track.record_label:
        tags.setall("TPUB", [TPUB(encoding=3, text=track.record_label)])
    if track.tempo:
        tags.setall("TBPM", [TBPM(encoding=3, text=track.tempo)])
    if track.key:
        tags.setall("TKEY", [TKEY(encoding=3, text=track.key)])

    # Apple reads this to show the "E" / "Clean" badge. 1 = explicit, 2 = clean.
    tags.delall("TXXX:ITUNESADVISORY")
    tags.add(TXXX(encoding=3, desc="ITUNESADVISORY",
                  text="1" if track.explicit else "2"))


def tag_mp3(path: Path, track: Track) -> None:
    """Tag a freshly-downloaded file.

    By default this stays OFFLINE: genre comes from the CSV / playlist and the cover is
    whatever yt-dlp embedded (the YouTube thumbnail). The online square-art and genre
    lookups are rate-limited (iTunes ~20/min) and, if done here, would serialise every
    parallel download on that limit. So enrichment is deferred to the --reart and
    --fix-genres passes. Set ENRICH_DURING_DOWNLOAD = True to do it inline instead.
    """
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, APIC
    except Exception:
        return
    try:
        try:
            tags = ID3(path)
        except ID3NoHeaderError:
            tags = ID3()

        if ENRICH_DURING_DOWNLOAD:
            genre = resolve_genre(track)                      # CSV -> iTunes -> Deezer -> ...
        else:
            genre = canonical_genre(track.genre) or canonical_genre(track.playlist_genre)
        write_full_tags(tags, track, genre)

        if ENRICH_DURING_DOWNLOAD and PREFER_SQUARE_ART:
            cover = fetch_square_cover(track.artist, track.title, track.album)
            if cover:
                data, mime = cover
                tags.delall("APIC")   # drop the 16:9 thumbnail yt-dlp embedded
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        save_id3(tags, path)
    except Exception as exc:
        log(f"  tagging skipped: {exc}")


def _csv_genre_map() -> dict:
    """song_key -> genre, from every CSV next to this script (the trusted source)."""
    mapping: dict = {}
    for csv_path in sorted(SCRIPT_DIR.glob("*.csv")):
        try:
            for t in load_tracks(csv_path):
                if t.genre and t.genre.strip().lower() not in JUNK_GENRES:
                    mapping.setdefault(song_key(t.artist, t.title), t.genre)
        except Exception:
            continue
    return mapping


def _csv_track_map() -> dict:
    """song_key -> the full Track row, from every CSV next to this script."""
    mapping: dict = {}
    for csv_path in sorted(SCRIPT_DIR.glob("*.csv")):
        try:
            for t in load_tracks(csv_path):
                mapping.setdefault(song_key(t.artist, t.title), t)
        except Exception:
            continue
    return mapping


def retag_library(out_dir: Path, workers: int) -> None:
    """Bring every existing MP3 up to the full tag set: album artist, sort tags, label,
    BPM, key, explicit badge, disc/track numbers - and strip the YouTube blurb ffmpeg
    left behind. Cover art and genre are left as they are (use --reart / --fix-genres)."""
    from mutagen.id3 import ID3, ID3NoHeaderError

    audio_root = out_dir / "audio"
    files = sorted(audio_root.rglob("*.mp3"))
    if not files:
        log(f"No MP3 files found under {audio_root}")
        return

    csv_tracks = _csv_track_map()
    log(f"Retagging {len(files)} files against {len(csv_tracks)} CSV rows, "
        f"{workers} at a time...")

    stats = collections.Counter()
    lock = threading.Lock()
    bar = tqdm(total=len(files), desc="Retag", unit="file", dynamic_ncols=True, colour="blue")

    def work(path: Path) -> None:
        try:
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()

            artist, title, album = _identity_from_tags(path)
            track = csv_tracks.get(song_key(artist, title))
            if track is None:
                # not in any CSV (renamed, or hand-added): still worth de-littering
                strip_youtube_junk(tags)
                save_id3(tags, path)
                with lock:
                    stats["cleaned_only"] += 1
                return

            # keep whatever genre the file already has; --fix-genres owns that
            frame = tags.get("TCON")
            genre = canonical_genre(frame.text[0] if (frame and frame.text) else None) \
                or canonical_genre(track.genre)
            write_full_tags(tags, track, genre)
            save_id3(tags, path)
            with lock:
                stats["retagged"] += 1
                if track.record_label:
                    stats["with_label"] += 1
                if track.tempo:
                    stats["with_bpm"] += 1
                if track.key:
                    stats["with_key"] += 1
                if track.explicit:
                    stats["explicit"] += 1
        except Exception as exc:
            with lock:
                stats["failed"] += 1
            log(f"  retag error {path.name}: {exc}")
        finally:
            bar.update(1)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, files))
    finally:
        bar.close()

    print("\n" + "=" * 60)
    print(f"  MP3 files:              {len(files)}")
    print(f"  Fully retagged:         {stats['retagged']}")
    print(f"    ...with record label: {stats['with_label']}")
    print(f"    ...with BPM:          {stats['with_bpm']}")
    print(f"    ...with musical key:  {stats['with_key']}")
    print(f"    ...marked explicit:   {stats['explicit']}")
    print(f"  Not in any CSV (cleaned only): {stats['cleaned_only']}")
    print(f"  Failed:                 {stats['failed']}")
    print("=" * 60)


def _folder_genres(files: list) -> dict:
    """collection folder -> its dominant genre, learned from the files that already have
    one. This is the safety net that lets every remaining file get *something*."""
    from mutagen.id3 import ID3
    counts: dict = {}
    for path in files:
        try:
            frame = ID3(path).get("TCON")
        except Exception:
            continue
        g = canonical_genre(frame.text[0] if (frame and frame.text) else None)
        if g:
            counts.setdefault(path.parent.name, {})
            counts[path.parent.name][g] = counts[path.parent.name].get(g, 0) + 1
    return {folder: max(c, key=c.get) for folder, c in counts.items() if c}


def fix_genres(out_dir: Path, workers: int) -> None:
    """Give every MP3 exactly one real genre.

    Order: the CSV -> iTunes -> Deezer -> MusicBrainz -> the playlist's dominant genre.
    Also rewrites double-barrelled values already in the library ('R&B/Soul' -> 'R&B').
    Files that are already a single, valid genre cost no network call, so re-running is cheap.
    """
    from mutagen.id3 import ID3, ID3NoHeaderError, TCON

    audio_root = out_dir / "audio"
    files = sorted(audio_root.rglob("*.mp3"))
    if not files:
        log(f"No MP3 files found under {audio_root}")
        return

    csv_genres = {k: canonical_genre(v) for k, v in _csv_genre_map().items()}
    folder_genres = _folder_genres(files)
    log(f"Genres: {len(files)} files | {len(csv_genres)} from CSVs | "
        f"{len(folder_genres)} playlists have a dominant genre | {workers} at a time")

    stats = collections.Counter()
    lock = threading.Lock()
    bar = tqdm(total=len(files), desc="Genres", unit="file", dynamic_ncols=True, colour="green")

    def work(path: Path) -> None:
        try:
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            frame = tags.get("TCON")
            current = (frame.text[0].strip() if (frame and frame.text and frame.text[0]) else "")
            canon = canonical_genre(current)

            # already a single clean genre -> nothing to do, no network call
            if canon and canon == current:
                with lock:
                    stats["already_ok"] += 1
                return

            # had a genre, but double-barrelled ("R&B/Soul") -> just collapse it
            if canon:
                tags.setall("TCON", [TCON(encoding=3, text=canon)])
                save_id3(tags, path)
                with lock:
                    stats["normalized"] += 1
                log(f"{current} -> {canon}  ({path.name})")
                return

            # no usable genre -> work through every source
            artist, title, album = _identity_from_tags(path)
            genre = csv_genres.get(song_key(artist, title))
            source = "csv"
            if not genre:
                genre = lookup_genre(artist, title, album,
                                     fallback=folder_genres.get(path.parent.name))
                source = "lookup"

            if genre:
                tags.setall("TCON", [TCON(encoding=3, text=genre)])
                save_id3(tags, path)
                with lock:
                    stats["from_csv" if source == "csv" else "from_lookup"] += 1
                log(f"{genre:<20} <- {path.name}")
            else:
                if current:
                    tags.delall("TCON")
                    save_id3(tags, path)
                with lock:
                    stats["still_unknown"] += 1
        except Exception as exc:
            with lock:
                stats["failed"] += 1
            log(f"  genre error {path.name}: {exc}")
        finally:
            bar.update(1)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, files))
    finally:
        bar.close()

    print("\n" + "=" * 60)
    print(f"  MP3 files:            {len(files)}")
    print(f"  Already single genre: {stats['already_ok']}")
    print(f"  Collapsed 'A/B' -> A: {stats['normalized']}")
    print(f"  Filled from CSV:      {stats['from_csv']}")
    print(f"  Filled from lookup:   {stats['from_lookup']}")
    print(f"  Still unknown:        {stats['still_unknown']}")
    print(f"  Failed:               {stats['failed']}")
    print("=" * 60)


def _media_length(path: Path) -> Optional[int]:
    """Length in whole seconds, or None."""
    try:
        if path.suffix.lower() == ".mp3":
            from mutagen.mp3 import MP3
            return round(MP3(path).info.length)
        from mutagen.mp4 import MP4
        return round(MP4(path).info.length)
    except Exception:
        return None


def _expected_durations() -> dict:
    """song_key -> expected seconds, taken from every CSV next to this script."""
    expected: dict = {}
    for csv_path in sorted(SCRIPT_DIR.glob("*.csv")):
        try:
            for t in load_tracks(csv_path):
                if t.duration_s:
                    expected.setdefault(song_key(t.artist, t.title), t.duration_s)
        except Exception:
            continue
    return expected


def _mmss(seconds: Optional[int]) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "??"


def dedupe_library(out_dir: Path, apply: bool = False) -> None:
    """Find songs stored more than once under different filenames.

    Reports by default; only deletes when apply=True. The copy kept is the one whose
    length is closest to the CSV's duration, for video as well as audio - a wrong upload
    is often the BIGGEST file (a 7:19 'Calm Down' is not the 3:59 song), so file size is
    only ever used to break a tie, or when we have no duration to compare against.
    """
    expected = _expected_durations()
    removed_files = 0
    removed_bytes = 0
    to_delete: list = []

    for kind, ext in (("audio", ".mp3"), ("video", ".mp4")):
        root = out_dir / kind
        if not root.exists():
            continue
        groups: dict = {}
        for f in root.rglob(f"*{ext}"):
            groups.setdefault(song_key_from_stem(f.stem), []).append(f)
        dupes = {k: v for k, v in sorted(groups.items()) if len(v) > 1}

        print("\n" + "=" * 72)
        print(f"  {kind.upper()}: {len(dupes)} duplicate group(s)")
        print("=" * 72)
        if not dupes:
            print("  none")
            continue

        for key, paths in dupes.items():
            want = expected.get(key)
            scored = [(p, _media_length(p), p.stat().st_size) for p in paths]
            if want and all(s[1] for s in scored):
                # closest to the real track length wins; size only breaks a tie
                keeper = min(scored, key=lambda s: (abs(s[1] - want), -s[2]))[0]
            else:
                keeper = max(scored, key=lambda s: s[2])[0]

            header = f"  [{key}]"
            if want:
                header += f"   expected {_mmss(want)}"
            print("\n" + header)
            for p, length, size in scored:
                keep = p is keeper
                print(f"    {'KEEP  ' if keep else 'REMOVE'} {_mmss(length):>6} "
                      f"{size // 1024:>7} KB  {p.parent.name}/{p.name}")
                if not keep:
                    to_delete.append(p)
                    removed_files += 1
                    removed_bytes += size

    print("\n" + "=" * 72)
    print(f"  Redundant copies: {removed_files} files, {removed_bytes / 1024 / 1024:.0f} MB")
    if not apply:
        print("  DRY RUN - nothing deleted. Re-run with --dedupe --apply to remove them.")
        print("=" * 72)
        return

    deleted = 0
    for p in to_delete:
        try:
            p.unlink()
            deleted += 1
        except OSError as exc:
            log(f"could not delete {p}: {exc}")
    print(f"  Deleted {deleted} file(s).")
    print("=" * 72)

    history = DownloadHistory(out_dir / HISTORY_FILENAME, out_dir)
    total = history.reindex()
    log(f"Ledger rebuilt from disk: {total} file(s), "
        f"{len(history.data['tracks'])} unique songs.")


def reart_library(out_dir: Path, workers: int, force: bool = False) -> None:
    """Swap the embedded art of existing MP3s for a square cover, in place, without
    touching the audio. Skips files that are already square, and (thanks to the REART
    marker) files it already failed to find art for within the last REART_RETRY_DAYS -
    so re-running does not re-query iTunes/Deezer for the same hopeless tracks every time.
    --reart-force ignores both and re-pulls everything (e.g. to upgrade old art to 3000px)."""
    audio_root = out_dir / "audio"
    files = sorted(audio_root.rglob("*.mp3"))
    if not files:
        log(f"No MP3 files found under {audio_root}")
        return
    log(f"Re-art: {len(files)} MP3 files, {workers} at a time "
        f"({'forcing all' if force else f'skipping square + recent no-match (<{REART_RETRY_DAYS}d)'})...")
    stats = {"replaced": 0, "already_square": 0, "skipped_nomatch": 0, "no_match": 0, "failed": 0}
    lock = threading.Lock()
    bar = tqdm(total=len(files), desc="Re-art", unit="file", dynamic_ncols=True, colour="magenta")

    def work(path: Path) -> None:
        try:
            is_square, marker = reart_state(path)
            if not force:
                if is_square:                                  # already has good square art
                    with lock:
                        stats["already_square"] += 1
                    return
                # tried recently and found nothing -> don't re-query it again yet
                if (marker and marker[0] == "nomatch"
                        and _reart_age_days(marker[1]) < REART_RETRY_DAYS):
                    with lock:
                        stats["skipped_nomatch"] += 1
                    return

            artist, title, album = _identity_from_tags(path)
            cover = fetch_square_cover(artist, title, album)
            if not cover:
                if not is_square:            # remember the miss (don't relabel a file that has art)
                    stamp_reart(path, "nomatch")
                with lock:
                    stats["no_match"] += 1
                return
            if embed_cover_into_file(path, *cover):
                stamp_reart(path, "ok")
                with lock:
                    stats["replaced"] += 1
                log(f"re-arted: {path.name}")
            else:
                with lock:
                    stats["failed"] += 1
        except Exception as exc:
            with lock:
                stats["failed"] += 1
            log(f"  reart error {path.name}: {exc}")
        finally:
            bar.update(1)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, files))
    finally:
        bar.close()

    print("\n" + "=" * 60)
    print(f"  MP3 files:             {len(files)}")
    print(f"  Art replaced:          {stats['replaced']}")
    print(f"  Already square:        {stats['already_square']}")
    print(f"  Skipped (no cover <{REART_RETRY_DAYS}d): {stats['skipped_nomatch']}")
    print(f"  No cover found now:    {stats['no_match']}")
    print(f"  Failed:                {stats['failed']}")
    print("=" * 60)


# Words kept as-is when tidying a playlist name, and words left lower-case mid-title.
_NAME_KEEP = {
    "r&b": "R&B", "edm": "EDM", "dj": "DJ", "djs": "DJs", "tv": "TV", "ost": "OST",
    "nsfw": "NSFW", "uk": "UK", "us": "US", "usa": "USA", "nyc": "NYC", "la": "LA",
    "kpop": "KPOP", "jpop": "JPOP", "k-pop": "K-Pop", "j-pop": "J-Pop", "hd": "HD",
    "ii": "II", "iii": "III", "bpm": "BPM", "mc": "MC", "rnb": "R&B",
    "ccm": "CCM", "chh": "CHH", "hiphop": "Hip-Hop", "nf": "NF", "abba": "ABBA",
}
_NAME_SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "nor",
               "of", "on", "or", "the", "to", "vs", "with"}


def _cap_word(word: str) -> str:
    """Capitalise the first LETTER (not the first character), so '(slow' -> '(Slow'."""
    out, done = [], False
    for ch in word:
        if not done and ch.isalpha():
            out.append(ch.upper())
            done = True
        else:
            out.append(ch.lower() if done else ch)
    return "".join(out)


def _smart_title(text: str) -> str:
    words = [w for w in text.split(" ") if w != ""]
    out = []
    for i, w in enumerate(words):
        # split off surrounding punctuation/emoji so "(ALL" and "Time)" behave
        m = re.match(r"^(\W*)(.*?)(\W*)$", w, re.DOTALL)
        pre, core, suf = m.group(1), m.group(2), m.group(3)
        if not core:
            out.append(w)                            # pure symbol/emoji token
            continue
        low = core.lower()
        mid = 0 < i < len(words) - 1 and not pre     # not first/last, not after a bracket
        if low in _NAME_KEEP:                        # R&B, EDM, CCM ...
            core = _NAME_KEEP[low]
        elif any(ch.isdigit() for ch in core):       # 2026, 100, 70s, 2010-2026
            pass
        elif mid and low in _NAME_SMALL:
            core = low                               # "Best of Lucky Dube", not "Best OF ..."
        elif core.isupper() and len(core) <= 2:      # NF, JT - initialisms
            pass
        else:                                        # capitalise each hyphen part: Hip-Hop
            core = "-".join(_cap_word(p) for p in core.split("-"))
        out.append(pre + core + suf)
    return " ".join(out)


def pretty_playlist_name(stem: str, allow_truncate: bool = True) -> str:
    """'UGANDAN_TOP_100_HITS_2026_'          -> 'Ugandan Top 100 Hits 2026'
       'Best_R&B_Bedroom_Playlist_🔥_🔥_🔥'    -> 'Best R&B Bedroom Playlist 🔥🔥🔥'
       'SEX_PLAYLIST_SENSUAL_🔞_(Hot_Bed...)' -> 'Sex Playlist Sensual 🔞'  (long tail dropped)"""
    name = stem.replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip(" -–—,.&")
    name = _smart_title(name)
    # re-join runs of symbols/emoji that the underscores had split apart ("🔥 🔥" -> "🔥🔥")
    name = re.sub(r"(?<=[^\w\s])\s+(?=[^\w\s])", "", name)
    # very long Spotify names carry a descriptive tail - "Playlist (Hot Songs, Dark...)"
    # or "Playlist - Best Music Mix ..." - so cut at whichever marker comes first.
    if allow_truncate and len(name) > 60:
        cuts = [name.find(sep) for sep in (" (", " - ", " – ", " | ") if name.find(sep) > 0]
        if cuts:
            head = name[:min(cuts)].strip()
            if len(head) >= 10:
                name = head
    name = re.sub(r"\s+", " ", name).strip(" -–—,.")
    return sanitize_filename(name, max_len=80) or "Playlist"


def assign_playlist_names(csv_paths: list) -> dict:
    """csv path -> final display name. Collisions are resolved identically everywhere, so
    the .m3u8 filenames and the [Grouping] labels always agree."""
    names, used = {}, set()
    for p in csv_paths:
        name = pretty_playlist_name(p.stem)
        if name.lower() in used:
            # two CSVs tidied down to the same name - keep the fuller one instead of "(2)"
            name = pretty_playlist_name(p.stem, allow_truncate=False)
        base, n = name, 2
        while name.lower() in used:
            name = f"{base} ({n})"
            n += 1
        used.add(name.lower())
        names[p] = name
    return names


def group_tag_library(out_dir: Path, workers: int) -> None:
    """Stamp each MP3's playlist membership into the Grouping field (ID3 TIT1) as
    '[Playlist A][Playlist B]'. iTunes smart-playlist rules can match on Grouping, so a
    rule like  Grouping contains [Chill Hits]  keeps updating itself as new files arrive.
    The brackets stop one playlist name matching another that merely starts the same way."""
    from mutagen.id3 import ID3, ID3NoHeaderError, TIT1

    csv_paths = sorted(SCRIPT_DIR.glob("*.csv"))
    if not csv_paths:
        log(f"No CSV files found in {SCRIPT_DIR}")
        return
    names = assign_playlist_names(csv_paths)

    membership: dict = collections.defaultdict(set)
    for p in csv_paths:
        try:
            for t in load_tracks(p):
                membership[song_key(t.artist, t.title)].add(names[p])
        except Exception as exc:
            log(f"  {p.name}: unreadable ({exc}) - skipped")

    files = sorted((out_dir / "audio").rglob("*.mp3"))
    if not files:
        log(f"No MP3 files found under {out_dir / 'audio'}")
        return
    log(f"Grouping tags: {len(files)} files against {len(membership)} known songs, "
        f"{workers} at a time...")

    stats = collections.Counter()
    lock = threading.Lock()
    bar = tqdm(total=len(files), desc="Grouping", unit="file", dynamic_ncols=True, colour="yellow")

    def work(path: Path) -> None:
        try:
            artist, title, _ = _identity_from_tags(path)
            want = membership.get(song_key(artist, title))
            if not want:
                with lock:
                    stats["not_in_csv"] += 1
                return
            value = "".join(f"[{n}]" for n in sorted(want))
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            frame = tags.get("TIT1")
            current = frame.text[0] if (frame and frame.text and frame.text[0]) else ""
            if current == value:
                with lock:
                    stats["unchanged"] += 1
                return
            tags.setall("TIT1", [TIT1(encoding=3, text=value)])
            save_id3(tags, path)
            with lock:
                stats["updated"] += 1
                if len(want) > 1:
                    stats["multi_playlist"] += 1
        except Exception as exc:
            with lock:
                stats["failed"] += 1
            log(f"  grouping error {path.name}: {exc}")
        finally:
            bar.update(1)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, files))
    finally:
        bar.close()

    print("\n" + "=" * 60)
    print(f"  MP3 files:            {len(files)}")
    print(f"  Grouping written:     {stats['updated']}")
    print(f"    ...in 2+ playlists: {stats['multi_playlist']}")
    print(f"  Already correct:      {stats['unchanged']}")
    print(f"  Not in any CSV:       {stats['not_in_csv']}")
    print(f"  Failed:               {stats['failed']}")
    print("=" * 60)
    print("  In iTunes: File > New > Smart Playlist >  Grouping  contains  [Playlist Name]")


ALBUM_EXTRAS_COLLECTION = "Album Extras"
ALBUM_RECHECK_DAYS = 30           # skip an album already processed within this many days


def _is_real_album(album: str, title: str) -> bool:
    """True only for a genuine multi-track album name (not a single / EP)."""
    al = (album or "").strip().lower()
    if not al or al == "single":
        return False
    if al.endswith("- single") or al.endswith("- ep") or "(single)" in al:
        return False
    return al != (title or "").strip().lower()


class _AlbumState:
    """Remembers which albums --fill-albums has already worked through, so a chunked
    (--max-albums) run advances to new albums each time instead of re-checking the same
    ones. Albums are re-examined after ALBUM_RECHECK_DAYS (catches deluxe editions and
    retries earlier failures)."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {}

    def age_days(self, key: str) -> float:
        entry = self.data.get(key)
        if not entry:
            return float("inf")
        try:
            return _reart_age_days(datetime.fromisoformat(entry["at"]))
        except Exception:
            return float("inf")

    def mark(self, key: str, album: str, added: int) -> None:
        self.data[key] = {"album": album, "added": added, "at": now_stamp()}

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass


def fill_albums(out_dir: Path, extra: int, full: bool, workers: int, use_browser: bool,
                headless: bool, dry_run: bool, max_albums: int = 0) -> None:
    """For every album you own a song from, pull more tracks from that album. `full` grabs
    the WHOLE album; otherwise `extra` more tracks. New songs land in an 'Album Extras'
    folder, tagged with the real album so iTunes groups them with the song you already had."""
    history = DownloadHistory(out_dir / HISTORY_FILENAME, out_dir)
    state = _AlbumState(out_dir / "album_fill_state.json")

    # Group CSV tracks by album (recording which are downloaded), and note every CSV
    # song_key so we don't pull a playlist song into Album Extras - the normal run owns it.
    groups: dict = {}
    csv_keys: set = set()
    for csv_path in sorted(SCRIPT_DIR.glob("*.csv")):
        try:
            tracks = load_tracks(csv_path)
        except Exception:
            continue
        for t in tracks:
            csv_keys.add(song_key(t.artist, t.title))
            if not _is_real_album(t.album, t.title):
                continue
            g = groups.setdefault(normalize(f"{t.artist} {t.album}"),
                                  {"key": normalize(f"{t.artist} {t.album}"),
                                   "artist": t.artist, "album": t.album, "have": []})
            if history.has(t, "audio"):
                g["have"].append(t)

    target = 1 + max(1, extra)
    owned = [g for g in groups.values() if g["have"]]
    # not looked at recently; and (full mode, or still short of the target count)
    todo = [g for g in owned
            if state.age_days(g["key"]) >= ALBUM_RECHECK_DAYS and (full or len(g["have"]) < target)]
    todo.sort(key=lambda g: state.age_days(g["key"]), reverse=True)   # never-seen first
    if max_albums:
        todo = todo[:max_albums]
    log(f"Albums to complete: {len(todo)} of {len(owned)} owned "
        f"({'whole album' if full else f'up to {target} tracks each'})")

    extras_dir = out_dir / "audio" / sanitize_filename(ALBUM_EXTRAS_COLLECTION)
    queue: list = []
    seen: set = set()
    for g in todo:
        seed = g["have"][0]
        need = 10_000 if full else target - len(g["have"])
        picked = 0
        for tno, stitle, sartist, dur, genre, expl in album_tracks(seed.artist, seed.title, seed.album):
            if picked >= need:
                break
            if not stitle:
                continue
            primary = re.split(r"[;,/]| feat | ft ", sartist, maxsplit=1)[0].strip()
            sk = song_key(primary, stitle)
            if sk in seen or sk in csv_keys:          # already queued, or a playlist song
                continue
            cand = Track(title=stitle, artist=primary, all_artists=sartist, album=seed.album,
                         genre=genre or "", duration_s=dur, track_no=tno, explicit=bool(expl),
                         playlist_genre=seed.playlist_genre)
            if history.has(cand, "audio"):            # already own it somewhere
                continue
            queue.append(cand)
            seen.add(sk)
            picked += 1
        if not dry_run:
            state.mark(g["key"], g["album"], picked)   # examined; won't re-check for 30 days

    log(f"Album tracks to fetch: {len(queue)}")
    if dry_run:
        for c in queue[:60]:
            log(f"  would fetch: {c.label}   [{c.album}]")
        if len(queue) > 60:
            log(f"  ... and {len(queue) - 60} more")
        return
    if not queue:
        state.save()
        return

    extras_dir.mkdir(parents=True, exist_ok=True)
    items = [WorkItem(c, ALBUM_EXTRAS_COLLECTION, extras_dir, extras_dir) for c in queue]
    totals = {ALBUM_EXTRAS_COLLECTION: len(items)}
    try:
        grand, _ = run_parallel(items, totals, "audio", history, workers,
                                use_browser, headless, dry_run=False)
    finally:
        state.save()
    print("\n" + "=" * 60)
    print(f"  Album-fill: {grand.audio_ok} added, {grand.skipped} skipped, "
          f"{grand.failed} failed  (into '{ALBUM_EXTRAS_COLLECTION}')")
    print("=" * 60)


GENRE_PLAYLIST_PREFIX = "Album Extras - "   # keeps them together, apart from your curated lists
GENRE_PLAYLIST_MIN    = 5                   # don't clutter iTunes with 1-track genre lists


# A finished download must look like real music. Anything failing these checks is a bad
# grab (dead stream, wrong result, aborted transfer) and is rejected rather than kept.
MIN_BITRATE_KBPS = 128
MIN_DURATION_S   = 45
# Titles that are legitimately short - album skits/interludes are not broken downloads.
_SHORT_OK = re.compile(r"\b(skit|intro|outro|interlude|prelude|reprise|prologue|epilogue)\b", re.I)


def audio_problem(path: Path, expected_s: Optional[int] = None,
                  require_art: bool = False) -> Optional[str]:
    """Short reason this file is not a usable music track, or None if it is fine."""
    try:
        if not path.exists():
            return "missing"
        if path.stat().st_size < 50_000:
            return "tiny file"
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3
        audio = MP3(path, ID3=ID3)
        duration = audio.info.length or 0
        bitrate = (audio.info.bitrate or 0) // 1000
        tags = audio.tags
    except Exception as exc:
        return f"unreadable ({str(exc)[:34]})"

    if duration <= 0:
        return "zero length"
    if expected_s and expected_s > 60 and duration < expected_s * 0.5:
        return f"truncated ({int(duration)}s of ~{expected_s}s)"

    floor = MIN_DURATION_S
    if expected_s:                       # a track that is genuinely short is allowed to be
        floor = min(MIN_DURATION_S, max(10, int(expected_s * 0.6)))
    if duration < floor and not _SHORT_OK.search(path.stem):
        return f"too short ({int(duration)}s)"

    if bitrate and bitrate < MIN_BITRATE_KBPS:
        return f"low bitrate ({bitrate}k)"
    if require_art:
        has_art = any(getattr(f, "FrameID", "") == "APIC" for f in (tags.values() if tags else []))
        if not has_art:
            return "no cover art"
    return None


def find_broken(out_dir: Path, apply: bool, workers: int) -> None:
    """Report (and with apply=True, delete) files that are not usable music: corrupt,
    zero-length, truncated, too short, low bitrate, or with no cover art. Deleted tracks
    drop out of the ledger too, so the next normal run simply downloads them again."""
    audio_root = out_dir / "audio"
    files = sorted(audio_root.rglob("*.mp3"))
    if not files:
        log(f"No MP3 files found under {audio_root}")
        return

    expected = _expected_durations()          # song_key -> seconds, from the CSVs
    log(f"Checking {len(files)} file(s) with {workers} workers...")

    found: list = []
    protected: list = []
    lock = threading.Lock()
    bar = tqdm(total=len(files), desc="Checking", unit="file", dynamic_ncols=True, colour="red")

    def work(path: Path) -> None:
        try:
            want = expected.get(song_key_from_stem(path.stem))
            reason = audio_problem(path, want, require_art=True)
            if reason:
                with lock:
                    found.append((path, reason))
            elif _SHORT_OK.search(path.stem):
                with lock:
                    protected.append(path)
        except Exception as exc:
            with lock:
                found.append((path, f"check failed ({str(exc)[:30]})"))
        finally:
            bar.update(1)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, files))
    finally:
        bar.close()

    if not found:
        print("\nNothing broken - every file passed.")
        return

    buckets: dict = collections.defaultdict(list)
    for path, reason in found:
        buckets[reason.split(" (")[0]].append((path, reason))

    print("\n" + "=" * 66)
    print(f"  Problem files: {len(found)} of {len(files)}")
    print("=" * 66)
    for kind in sorted(buckets):
        rows = buckets[kind]
        print(f"\n  {kind.upper()}  ({len(rows)})")
        for path, reason in rows[:8]:
            print(f"    {reason:28} {path.parent.name}/{path.name[:44]}")
        if len(rows) > 8:
            print(f"    ... and {len(rows) - 8} more")

    freed = sum(p.stat().st_size for p, _ in found if p.exists()) / 1024 / 1024
    print("\n" + "=" * 66)
    print(f"  Total: {len(found)} file(s), {freed:.0f} MB")
    if protected:
        print(f"  Kept {len(protected)} genuinely short track(s) (skit/intro/interlude).")
    if not apply:
        print("  DRY RUN - nothing deleted. Re-run with --find-broken --apply to remove them.")
        print("=" * 66)
        return

    deleted = 0
    for path, _ in found:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            log(f"could not delete {path.name}: {exc}")
    print(f"  Deleted {deleted} file(s).")
    print("=" * 66)

    history = DownloadHistory(out_dir / HISTORY_FILENAME, out_dir)
    total = history.reindex()
    log(f"Ledger rebuilt: {total} file(s), {len(history.data['tracks'])} songs. "
        "Deleted tracks will be re-downloaded on the next run.")


def save_id3(tags, path: Path) -> None:
    """Save ID3 tags as v2.3, NOT mutagen's default v2.4.

    iTunes / Apple Music on Windows only properly supports ID3 v2.2 and v2.3. Given a
    v2.4 tag it frequently fails to read the artist frame and shows the track as
    'By Unknown' even though TPE1 is present. update_to_v23() also rewrites v2.4-only
    frames (e.g. TDRC -> TYER) so nothing is silently dropped.
    """
    # update_to_v23() drops the v2.4-only sort frames, so carry them over by hand -
    # iTunes reads TSOP/TSOA/TSOT out of a v2.3 tag happily, and they are what makes
    # "The Weeknd" file under W instead of T.
    keep = {}
    for key in ("TSOP", "TSOA", "TSOT", "TSO2"):
        frame = tags.get(key)
        if frame is not None:
            keep[key] = frame
    try:
        tags.update_to_v23()
    except Exception:
        pass
    for frame in keep.values():
        try:
            tags.add(frame)
        except Exception:
            pass
    tags.save(path, v2_version=3)


def _extras_meta(path: Path) -> tuple:
    """(genre, artist, title, seconds) in a single file read."""
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3
        audio = MP3(path, ID3=ID3)
        tags = audio.tags

        def txt(key):
            frame = tags.get(key) if tags else None
            return frame.text[0] if (frame and frame.text and frame.text[0]) else ""

        return (canonical_genre(txt("TCON")),
                txt("TPE1") or txt("TPE2"),
                txt("TIT2") or path.stem,
                round(audio.info.length) if audio.info else -1)
    except Exception:
        return (None, "", path.stem, -1)


def build_genre_playlists(out_dir: Path, pl_dir: Path, workers: int = 8) -> tuple:
    """One .m3u8 per genre for the Album Extras. Those tracks belong to no curated
    playlist (they came from completing albums), so grouping them by genre gives them a
    home. Genres with fewer than GENRE_PLAYLIST_MIN tracks are left to iTunes' own genre
    browser rather than becoming near-empty playlists."""
    extras_dir = out_dir / "audio" / sanitize_filename(ALBUM_EXTRAS_COLLECTION)
    if not extras_dir.exists():
        return (0, 0)
    files = sorted(extras_dir.rglob("*.mp3"))
    if not files:
        return (0, 0)

    log(f"Genre playlists: reading tags from {len(files)} Album Extras file(s)...")
    groups: dict = collections.defaultdict(list)
    lock = threading.Lock()

    def work(path: Path) -> None:
        genre, artist, title, seconds = _extras_meta(path)
        if not genre:
            return
        with lock:
            groups[genre].append((path, artist, title, seconds))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, files))

    written = referenced = 0
    for genre, entries in sorted(groups.items()):
        if len(entries) < GENRE_PLAYLIST_MIN:
            continue
        entries.sort(key=lambda e: (e[1].lower(), e[2].lower()))    # artist, then title
        lines = ["#EXTM3U"]
        for path, artist, title, seconds in entries:
            lines.append(f"#EXTINF:{seconds},{artist} - {title}".rstrip(" -"))
            lines.append(str(path))
        name = sanitize_filename(f"{GENRE_PLAYLIST_PREFIX}{genre}", max_len=80)
        with open(pl_dir / f"{name}.m3u8", "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("\n".join(lines) + "\n")
        written += 1
        referenced += len(entries)
        log(f"  {name}.m3u8 -> {len(entries)} track(s)")

    skipped = sum(len(v) for k, v in groups.items() if len(v) < GENRE_PLAYLIST_MIN)
    if skipped:
        log(f"  ({skipped} track(s) in genres under {GENRE_PLAYLIST_MIN} - browse those by genre in iTunes)")
    return (written, referenced)


def build_playlists(out_dir: Path) -> None:
    """Write one .m3u8 per CSV, ready to import into iTunes / Apple Music.

    Built from the CSV, not from folder contents. Deduplication means a song shared by
    several playlists is physically stored in only ONE of their folders, so a folder
    listing would produce badly incomplete playlists - instead every CSV row is resolved
    to wherever its file actually lives, preserving the playlist's own order.
    """
    history = DownloadHistory(out_dir / HISTORY_FILENAME, out_dir)
    pl_dir = out_dir / "playlists"
    pl_dir.mkdir(parents=True, exist_ok=True)

    csv_paths = sorted(SCRIPT_DIR.glob("*.csv"))
    if not csv_paths:
        log(f"No CSV files found in {SCRIPT_DIR}")
        return

    # These files are fully regenerated every run; clear the old ones so renamed
    # playlists don't leave a stale duplicate behind.
    stale = list(pl_dir.glob("*.m3u8")) + list(pl_dir.glob("*.m3u"))
    for old in stale:
        try:
            old.unlink()
        except OSError:
            pass
    if stale:
        log(f"Cleared {len(stale)} previously generated playlist file(s)")

    log(f"Building {len(csv_paths)} playlist(s) into {pl_dir}")
    names = assign_playlist_names(csv_paths)
    total_tracks = total_missing = 0

    for csv_path in csv_paths:
        try:
            tracks = load_tracks(csv_path)
        except Exception as exc:
            log(f"  {csv_path.name}: unreadable ({exc}) - skipped")
            continue

        lines = ["#EXTM3U"]
        found = missing = 0
        for t in tracks:
            path = history.has(t, "audio")
            if not path:
                missing += 1
                continue
            seconds = t.duration_s if t.duration_s else (_media_length(path) or -1)
            artist = (t.all_artists or t.artist).replace(";", ", ")
            lines.append(f"#EXTINF:{seconds},{artist} - {t.title}")
            lines.append(str(path))          # absolute path - most reliable for iTunes
            found += 1

        # the filename becomes the playlist name inside iTunes (matches the Grouping tag)
        candidate = names[csv_path]
        target = pl_dir / f"{candidate}.m3u8"
        # UTF-8 (no BOM) + CRLF is what iTunes on Windows reads most reliably
        with open(target, "w", encoding="utf-8", newline="\r\n") as fh:
            fh.write("\n".join(lines) + "\n")

        total_tracks += found
        total_missing += missing
        flag = f"  ({missing} not downloaded yet)" if missing else ""
        log(f"  {candidate}.m3u8 -> {found} track(s){flag}")

    genre_lists, genre_tracks = build_genre_playlists(out_dir, pl_dir)

    print("\n" + "=" * 60)
    print(f"  Playlists written : {len(names)}  ->  {pl_dir}")
    print(f"  Tracks referenced : {total_tracks}")
    print(f"  Not yet downloaded: {total_missing}")
    print(f"  Genre playlists   : {genre_lists}  ({genre_tracks} Album Extras tracks)")
    print("=" * 60)
    print("  Import into iTunes / Apple Music:")
    print("    File > Library > Import Playlist...  then pick a .m3u8")
    print("    (repeat per playlist; the file name becomes the playlist name)")


def _score_candidate(track: Track, title: str, duration: Optional[int]) -> float:
    """How well a YouTube result matches the CSV row."""
    score = 0.65 * similarity(track.title, title) + 0.20 * similarity(track.artist, title)
    if track.duration_s and duration:
        diff = abs(duration - track.duration_s)
        score += 0.15 * max(0.0, 1.0 - diff / DURATION_TOLERANCE)
        if duration > track.duration_s * 2 + 60:      # an hour-long mix is not the song
            score -= 0.5
    return score


def ytdlp_search(track: Track, limit: int = 5) -> Optional[str]:
    """Search YouTube through yt-dlp (no browser) and return the best watch URL.

    NOTE: do NOT append words like 'audio' to the query. YouTube treats every word as a
    required term, so 'XAttraction Moonshine audio' returns zero results while
    'XAttraction Moonshine' finds the song. Instead we ask for several results and pick
    the one whose title and length best match the CSV row.
    """
    import yt_dlp
    opts = {
        "quiet": True, "no_warnings": True, "ignoreerrors": True, "noplaylist": True,
        "skip_download": True, "extract_flat": "in_playlist", "logger": _ErrCollector(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch{limit}:{track.query}", download=False)
    except Exception as exc:
        log(f"  search error for {track.label}: {exc}")
        return None

    entries = [e for e in ((info or {}).get("entries") or []) if e]
    if not entries:
        return None

    best, best_score = None, -1.0
    for e in entries:
        score = _score_candidate(track, e.get("title") or "", e.get("duration"))
        if score > best_score:
            best, best_score = e, score
    if best and best.get("id"):
        return f"https://www.youtube.com/watch?v={best['id']}"
    return None


# ======================================================================
#  Orchestration
# ======================================================================
def resolve_source(track: Track, searcher: Optional[YouTubeSearcher]) -> Optional[str]:
    """Best watch URL for a track, or None if YouTube simply has no match."""
    if searcher and searcher.driver:
        url = searcher.search(track)
        if url:
            return url
        log("  Falling back to yt-dlp search.")
    return ytdlp_search(track)


def process_one_csv(csv_path: Path, mode: str, out_dir: Path,
                    history: DownloadHistory, searcher: Optional[YouTubeSearcher],
                    dry_run: bool, limit: int = 0) -> Stats:
    """Download one CSV's tracks using a SHARED ledger + browser (so multiple CSVs
    can run back-to-back without re-opening the browser or re-loading the ledger)."""
    tracks = load_tracks(csv_path)
    if limit > 0:
        tracks = tracks[:limit]
    collection = safe_collection(csv_path.stem)
    stats = Stats(total=len(tracks))
    if not tracks:
        log(f"{csv_path.name}: no tracks found - skipping this file.")
        return stats

    # Keep each CSV's files apart: downloads/audio/<csv name>/ and downloads/video/<csv name>/
    audio_dir = out_dir / "audio" / collection
    video_dir = out_dir / "video" / collection
    if mode in ("audio", "both"):
        audio_dir.mkdir(parents=True, exist_ok=True)
    if mode in ("video", "both"):
        video_dir.mkdir(parents=True, exist_ok=True)

    want_audio = mode in ("audio", "both")
    want_video = mode in ("video", "both")

    log(f"=== {csv_path.name}: {len(tracks)} tracks -> '{collection}' ===")
    bar = tqdm(total=stats.total, desc=collection[:22], unit="track",
               dynamic_ncols=True, colour="cyan")
    try:
        for i, track in enumerate(tracks, 1):
            bar.set_postfix_str(track.label[:38])
            safe = sanitize_filename(track.label)
            mp3 = audio_dir / f"{fit_filename(audio_dir, safe, '.mp3')}.mp3"
            mp4 = video_dir / f"{fit_filename(video_dir, safe, '.mp4')}.mp4"

            # Skip if we already have it, wherever it lives.
            have_audio = history.has(track, "audio") if want_audio else None
            have_video = history.has(track, "video") if want_video else None
            need_audio = want_audio and have_audio is None
            need_video = want_video and have_video is None

            if not need_audio and not need_video:
                where = (have_audio or have_video).parent.name
                log(f"[{i}/{stats.total}] {track.label} -> already have it in '{where}', skipping.")
                stats.skipped += 1
                bar.update(1)
                continue

            log(f"[{i}/{stats.total}] {track.label}")
            source = resolve_source(track, searcher)
            if not source:
                log(f"  no YouTube match for: {track.label}")
                stats.failed += 1
                stats.failures.append(f"{track.label} (no YouTube match)")
                bar.update(1)
                continue
            log(f"  Source: {source}")

            if dry_run:
                bar.update(1)
                continue

            if need_audio:
                problem = None
                if download_audio(source, mp3, track.label):
                    # a "successful" download can still be a dud (dead stream, wrong
                    # result, aborted transfer) - throw it away so it retries next run
                    problem = audio_problem(mp3, track.duration_s)
                    if problem:
                        try:
                            mp3.unlink()
                        except OSError:
                            pass
                    else:
                        tag_mp3(mp3, track)
                        history.record(track, "audio", mp3, collection, source)
                        stats.audio_ok += 1
                        log("  Audio OK")
                else:
                    problem = "download failed"
                if problem:
                    stats.failed += 1
                    stats.failures.append(f"{track.label} (audio: {problem})")
                    log(f"  rejected: {problem}")
            if need_video:
                if download_video(source, mp4, track.label):
                    history.record(track, "video", mp4, collection, source)
                    stats.video_ok += 1
                    log("  Video OK")
                else:
                    stats.failed += 1
                    stats.failures.append(f"{track.label} (video)")

            bar.update(1)
            if i < stats.total:
                human_pause(TRACK_PAUSE_MIN, TRACK_PAUSE_MAX)
    finally:
        bar.close()
    return stats


def print_summary(per_csv: list[tuple[str, Stats]], grand: Stats) -> None:
    print("\n" + "=" * 60)
    if len(per_csv) > 1:
        print("  Per playlist:")
        for name, s in per_csv:
            print(f"    {name}: audio {s.audio_ok}, video {s.video_ok}, "
                  f"skipped {s.skipped}, failed {s.failed}  (of {s.total})")
        print("  " + "-" * 40)
        print(f"  CSVs:     {len(per_csv)}")
    print(f"  Tracks:   {grand.total}")
    print(f"  Audio OK: {grand.audio_ok}")
    print(f"  Video OK: {grand.video_ok}")
    print(f"  Skipped:  {grand.skipped}")
    print(f"  Failed:   {grand.failed}")
    if grand.failures:
        print("  Failures:")
        for f in grand.failures:
            print(f"    - {f}")
    print("=" * 60)


@dataclass
class WorkItem:
    track: Track
    collection: str
    audio_dir: Path
    video_dir: Path


def build_work_items(csv_paths: list[Path], mode: str, out_dir: Path,
                     limit: int) -> tuple[list[WorkItem], dict[str, int]]:
    """Flatten every selected CSV into one flat list of per-track jobs and create
    each collection's output folders. Returns (items, track-count-per-collection)."""
    items: list[WorkItem] = []
    totals: dict[str, int] = {}
    want_audio = mode in ("audio", "both")
    want_video = mode in ("video", "both")
    for csv_path in csv_paths:
        try:
            tracks = load_tracks(csv_path)
        except Exception as exc:
            log(f"{csv_path.name}: could not read ({exc}) - skipping.")
            continue
        if limit > 0:
            tracks = tracks[:limit]
        collection = safe_collection(csv_path.stem)
        totals[collection] = totals.get(collection, 0) + len(tracks)
        audio_dir = out_dir / "audio" / collection
        video_dir = out_dir / "video" / collection
        if want_audio:
            audio_dir.mkdir(parents=True, exist_ok=True)
        if want_video:
            video_dir.mkdir(parents=True, exist_ok=True)
        for t in tracks:
            items.append(WorkItem(t, collection, audio_dir, video_dir))
    return items, totals


def run_parallel(items: list[WorkItem], totals: dict[str, int], mode: str,
                 history: DownloadHistory, workers: int,
                 use_browser: bool, headless: bool,
                 dry_run: bool) -> tuple[Stats, dict[str, Stats]]:
    """Download many tracks at once with a bounded thread pool. The ledger is shared
    and lock-protected; an atomic claim() ensures the same song is fetched only once
    even when it appears in several playlists."""
    want_audio = mode in ("audio", "both")
    want_video = mode in ("video", "both")

    # Selenium drivers are not thread-safe, so each worker thread owns its own browser.
    tl = threading.local()
    created: list[YouTubeSearcher] = []
    created_lock = threading.Lock()

    def get_searcher() -> Optional[YouTubeSearcher]:
        if not use_browser:
            return None
        s = getattr(tl, "searcher", "unset")
        if s == "unset":
            drv = YouTubeSearcher(headless=headless)
            s = drv if drv.start() else None
            tl.searcher = s
            if s:
                with created_lock:
                    created.append(s)
        return s

    stats_by = {c: Stats(total=n) for c, n in totals.items()}
    grand = Stats(total=len(items))
    stats_lock = threading.Lock()
    bar = tqdm(total=len(items), desc=f"All tracks (x{workers})", unit="track",
               dynamic_ncols=True, colour="cyan")

    def work(item: WorkItem) -> None:
        track = item.track
        st = stats_by[item.collection]
        # Atomic: what's still missing AND not already being fetched by a sibling worker.
        need_a, need_v = history.claim(track, want_audio, want_video)
        if not need_a and not need_v:
            with stats_lock:
                st.skipped += 1
                grand.skipped += 1
            bar.update(1)
            return
        try:
            source = resolve_source(track, get_searcher())
            if not source:
                with stats_lock:
                    st.failed += 1
                    grand.failed += 1
                    grand.failures.append(f"{track.label} (no YouTube match)")
                log(f"no YouTube match: {track.label}")
                return
            if dry_run:
                return
            safe = sanitize_filename(track.label)
            if need_a:
                mp3 = item.audio_dir / f"{fit_filename(item.audio_dir, safe, '.mp3')}.mp3"
                problem = None
                if download_audio(source, mp3, track.label, show_bar=False):
                    # reject duds (dead stream, wrong result, aborted transfer) so they
                    # are not recorded as done - the next run will try them again
                    problem = audio_problem(mp3, track.duration_s)
                    if problem:
                        try:
                            mp3.unlink()
                        except OSError:
                            pass
                    else:
                        tag_mp3(mp3, track)
                        history.record(track, "audio", mp3, item.collection, source)
                        with stats_lock:
                            st.audio_ok += 1
                            grand.audio_ok += 1
                        log(f"audio OK: {track.label}")
                else:
                    problem = "download failed"
                if problem:
                    with stats_lock:
                        st.failed += 1
                        grand.failed += 1
                        grand.failures.append(f"{track.label} (audio: {problem})")
                    log(f"rejected [{problem}]: {track.label}")
            if need_v:
                mp4 = item.video_dir / f"{fit_filename(item.video_dir, safe, '.mp4')}.mp4"
                if download_video(source, mp4, track.label, show_bar=False):
                    history.record(track, "video", mp4, item.collection, source)
                    with stats_lock:
                        st.video_ok += 1
                        grand.video_ok += 1
                    log(f"video OK: {track.label}")
                else:
                    with stats_lock:
                        st.failed += 1
                        grand.failed += 1
                        grand.failures.append(f"{track.label} (video)")
        finally:
            if need_a:
                history.release(track, "audio")
            if need_v:
                history.release(track, "video")
            bar.update(1)
            if not dry_run:
                human_pause(TRACK_PAUSE_MIN, TRACK_PAUSE_MAX)

    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, items))
    finally:
        bar.close()
        for s in created:
            s.stop()
    return grand, stats_by


def run(csv_paths: list[Path], mode: str, out_dir: Path, use_browser: bool,
        headless: bool, dry_run: bool, limit: int, workers: int) -> None:
    """Process one or many CSVs. workers==1 keeps the classic sequential flow;
    workers>1 downloads that many tracks at once through a shared ledger."""
    history = DownloadHistory(out_dir / HISTORY_FILENAME, out_dir)
    log(f"Ledger: {history.path} ({len(history.data['tracks'])} tracks known)")
    log(f"Mode: {mode} | Browser: {'yes' if use_browser else 'no'} | "
        f"Workers: {workers} | "
        f"ffmpeg: {'found' if FFMPEG_DIR or shutil.which('ffmpeg') else 'MISSING'} | "
        f"JS runtime (deno): {'found' if DENO_DIR else 'MISSING'}")
    if not (FFMPEG_DIR or shutil.which("ffmpeg")):
        log("WARNING: ffmpeg not found - MP3 extraction and MP4 merging will fail. "
            "Install with:  winget install Gyan.FFmpeg")

    if workers > 1:
        if use_browser:
            log(f"Parallel mode opens one browser per worker (~{workers} windows). "
                "Use --no-browser for a lighter run.")
        items, totals = build_work_items(csv_paths, mode, out_dir, limit)
        if not items:
            log("Nothing to download.")
            return
        log(f"Downloading {len(items)} tracks from {len(totals)} playlist(s), "
            f"{workers} at a time...")
        grand, stats_by = run_parallel(items, totals, mode, history, workers,
                                       use_browser, headless, dry_run)
        print_summary(sorted(stats_by.items()), grand)
        return

    # ---- sequential (workers == 1): one browser, nicest per-file progress bars ----
    searcher = None
    if use_browser:
        searcher = YouTubeSearcher(headless=headless)
        if not searcher.start():
            log("No browser available - using yt-dlp search for every track.")
            searcher = None

    grand = Stats()
    per_csv: list[tuple[str, Stats]] = []
    try:
        for csv_path in csv_paths:
            s = process_one_csv(csv_path, mode, out_dir, history, searcher, dry_run, limit)
            per_csv.append((csv_path.name, s))
            grand.total += s.total
            grand.audio_ok += s.audio_ok
            grand.video_ok += s.video_ok
            grand.skipped += s.skipped
            grand.failed += s.failed
            grand.failures.extend(s.failures)
    finally:
        if searcher:
            searcher.stop()
    print_summary(per_csv, grand)


def resolve_csvs(arg: Optional[str], run_all: bool) -> list[Path]:
    """Return the list of CSV files to process."""
    if run_all:
        found = sorted(SCRIPT_DIR.glob("*.csv"))
        if not found:
            sys.exit(f"--all: no CSV files found in {SCRIPT_DIR}")
        return found
    if arg:
        p = Path(arg)
        if not p.is_absolute():
            p = SCRIPT_DIR / arg
        if p.is_dir():                       # a folder path -> every CSV inside it
            found = sorted(p.glob("*.csv"))
            if not found:
                sys.exit(f"No CSV files found in {p}")
            return found
        if not p.exists():
            sys.exit(f"CSV not found: {p}")
        return [p]
    found = sorted(SCRIPT_DIR.glob("*.csv"))
    if len(found) == 1:
        return found
    if not found:
        sys.exit(f"No CSV given and none found in {SCRIPT_DIR}")
    listing = "\n  ".join(f'--csv "{f.name}"' for f in found)
    sys.exit("Multiple CSVs found - run every one with  --all , or pick one:\n  " + listing)


def main() -> None:
    ap = argparse.ArgumentParser(description="Download audio/video for songs listed in a CSV.")
    ap.add_argument("--csv", help="Path to a CSV file (or a folder of CSVs). "
                                  "Omit and use --all to process every CSV in the script folder.")
    ap.add_argument("--all", action="store_true",
                    help="Process EVERY .csv in the script folder, back-to-back.")
    ap.add_argument("--mode", choices=["audio", "video", "both"], default=DEFAULT_MODE)
    ap.add_argument("--out", default=str(OUTPUT_DIR), help="Output folder.")
    ap.add_argument("--limit", type=int, default=0, help="Only process the first N tracks (0 = all).")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="Download this many tracks at once (default 1 = sequential). "
                         "In browser mode each worker opens its own window, so keep it small (2-4).")
    ap.add_argument("--no-browser", action="store_true",
                    help="Skip Selenium; let yt-dlp find the videos itself.")
    ap.add_argument("--headless", action="store_true",
                    help="Run the browser without a visible window.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve source links but do not download.")
    ap.add_argument("--reindex", action="store_true",
                    help="Rebuild the JSON ledger from files already on disk, then exit.")
    ap.add_argument("--reart", action="store_true",
                    help="Swap the embedded art of existing MP3s for a square album cover "
                         "(in place, no re-download), then exit. Skips already-square files.")
    ap.add_argument("--reart-force", action="store_true",
                    help="Like --reart but re-fetch art for every file, even square ones.")
    ap.add_argument("--no-square-art", action="store_true",
                    help="Keep the raw YouTube thumbnail instead of fetching a square cover.")
    ap.add_argument("--cookies", metavar="FILE", default=None,
                    help="Netscape cookies.txt used to unlock age-restricted videos. "
                         f"Defaults to {DEFAULT_COOKIE_FILE.name} next to this script if present.")
    ap.add_argument("--no-cookies", action="store_true",
                    help="Ignore cookies.txt entirely (age-restricted videos will be skipped).")
    ap.add_argument("--fill-albums", nargs="?", const="all", default=None, metavar="N|all",
                    help="For every album you own a song from, download more tracks from that "
                         "album into an 'Album Extras' folder, then exit. Bare flag (or 'all') "
                         "pulls the WHOLE album; a number pulls that many extra tracks.")
    ap.add_argument("--max-albums", type=int, default=0,
                    help="With --fill-albums: only process this many albums per run (chunking).")
    ap.add_argument("--group-tags", action="store_true",
                    help="Stamp each track's playlist membership into the Grouping field "
                         "so iTunes smart playlists can auto-update, then exit.")
    ap.add_argument("--playlists", action="store_true",
                    help="Write one .m3u8 per CSV (into <out>/playlists) for importing "
                         "into iTunes / Apple Music, then exit.")
    ap.add_argument("--retag", action="store_true",
                    help="Rewrite the full tag set on existing MP3s (album artist, sort tags, "
                         "label, BPM, key, explicit badge) and strip YouTube leftovers, then exit.")
    ap.add_argument("--fix-genres", action="store_true",
                    help="Repair the genre tag on existing MP3s (replace YouTube's 'Music' "
                         "with the CSV genre, else iTunes), then exit.")
    ap.add_argument("--find-broken", action="store_true",
                    help="Report unusable files (corrupt, zero-length, truncated, too short, "
                         "low bitrate, no cover art). Add --apply to delete them, then exit.")
    ap.add_argument("--dedupe", action="store_true",
                    help="Report songs stored more than once under different filenames, then exit. "
                         "Nothing is deleted unless you add --apply.")
    ap.add_argument("--apply", action="store_true",
                    help="With --dedupe: actually delete the redundant copies.")
    args = ap.parse_args()

    if args.no_square_art:
        global PREFER_SQUARE_ART
        PREFER_SQUARE_ART = False

    global COOKIE_FILE
    if not args.no_cookies:
        chosen = Path(args.cookies) if args.cookies else DEFAULT_COOKIE_FILE
        if args.cookies and not chosen.exists():
            sys.exit(f"--cookies file not found: {chosen}")
        if chosen.exists():
            COOKIE_FILE = chosen
            log(f"Cookies: {chosen} (used only to retry age-restricted videos)")
            try:
                body = chosen.read_text(encoding="utf-8", errors="replace")
                if "youtube.com" not in body:
                    log("  WARNING: this cookies.txt has no youtube.com entries - "
                        "re-export it while logged in to YouTube.")
            except OSError:
                pass
        else:
            log(f"Cookies: none ({DEFAULT_COOKIE_FILE.name} not found) - "
                "age-restricted videos will be skipped.")

    workers = max(1, args.workers)

    if args.reindex:
        out = Path(args.out)
        history = DownloadHistory(out / HISTORY_FILENAME, out)
        added = history.reindex()
        log(f"Reindexed {added} file(s); ledger now knows "
            f"{len(history.data['tracks'])} tracks -> {history.path}")
        return

    if args.fill_albums is not None:
        val = str(args.fill_albums).strip().lower()
        full = val in ("all", "full", "0", "")
        try:
            extra = 1 if full else max(1, int(val))
        except ValueError:
            sys.exit(f"--fill-albums: expected a number or 'all', got {args.fill_albums!r}")
        fill_albums(Path(args.out), extra=extra, full=full, workers=workers,
                    use_browser=not args.no_browser, headless=args.headless,
                    dry_run=args.dry_run, max_albums=args.max_albums)
        return

    if args.group_tags:
        group_tag_library(Path(args.out), workers=workers)
        return

    if args.playlists:
        build_playlists(Path(args.out))
        return

    if args.retag:
        retag_library(Path(args.out), workers=workers)
        return

    if args.fix_genres:
        fix_genres(Path(args.out), workers=workers)
        return

    if args.find_broken:
        find_broken(Path(args.out), apply=args.apply, workers=workers)
        return

    if args.dedupe:
        dedupe_library(Path(args.out), apply=args.apply)
        return

    if args.reart or args.reart_force:
        reart_library(Path(args.out), workers=workers, force=args.reart_force)
        return
    csv_paths = resolve_csvs(args.csv, args.all)
    log(f"Saving under: {Path(args.out)}\\<audio|video>\\<csv name>")
    log(f"Selected {len(csv_paths)} CSV file(s): " + ", ".join(p.name for p in csv_paths))

    run(csv_paths, args.mode, Path(args.out),
        use_browser=not args.no_browser, headless=args.headless,
        dry_run=args.dry_run, limit=args.limit, workers=workers)


if __name__ == "__main__":
    main()
