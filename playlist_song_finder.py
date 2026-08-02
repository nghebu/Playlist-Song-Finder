#!/usr/bin/env python3
"""
Playlist Song Finder
Paste the URL of one long YouTube video that is a compilation / mix / DJ set of
many songs, and this tool builds a tracklist: song title + artist + a YouTube
link for each track.

How it figures out the songs (in order, cheapest first):
  1. Metadata  -- yt-dlp --dump-single-json (title, duration, description, chapters)
  2. Text tracklist:
       - use the video's chapters if it has them, else
       - parse timestamp lines out of the description, else
       - scan the top comments for a posted tracklist.
  3. Audio recognition (Shazam-like) fallback -- only when no text tracklist is
     found, or the user ticks "Force audio recognition". Downloads the audio,
     cuts short clips with ffmpeg, and identifies each with shazamio.
  4. YouTube link lookup for every entry (yt-dlp ytsearch1).

Output: shown in the log, and saved as <video-title>.txt and <video-title>.csv
in the output folder (default: a "tracklists" folder next to this script).

The core pipeline functions live above the GUI and take no Tkinter objects, so
they can be imported and unit-tested headlessly (see the parser tests).

Requires: Python 3.10+ (shazamio), yt-dlp, ffmpeg. Run setup.bat once.
"""

import os
import re
import sys
import csv
import json
import time
import queue
import shutil
import asyncio
import tempfile
import threading
import subprocess
from datetime import datetime

# ---------------------------------------------------------------------------
# Config / defaults
# ---------------------------------------------------------------------------
APP_TITLE = "Playlist Song Finder"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUTDIR = os.path.join(SCRIPT_DIR, "tracklists")
DEFAULT_SAMPLE_INTERVAL = 90   # seconds, used when no timestamps are known
CLIP_SECONDS = 12              # length of each audio clip sent to shazam


# ---------------------------------------------------------------------------
# Executable discovery (mirrors YT-M Downloader)
# ---------------------------------------------------------------------------
def find_executable(name):
    """Return a usable path/command for an executable, or None."""
    p = shutil.which(name)
    if p:
        return p
    if name == "yt-dlp":
        try:
            subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, check=True)
            return [sys.executable, "-m", "yt_dlp"]
        except Exception:
            return None
    return None


def as_list(cmd):
    return cmd if isinstance(cmd, list) else [cmd]


def _run_json(cmd):
    """Run a command expected to print a single JSON document; return the dict.
    Raises on non-zero exit or unparseable output."""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 and not proc.stdout.strip():
        raise RuntimeError((proc.stderr or "yt-dlp failed").strip()[:500])
    return json.loads(proc.stdout)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def fmt_time(seconds):
    """Seconds -> 'hh:mm:ss' (or 'mm:ss' when under an hour)."""
    if seconds is None:
        return ""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def parse_hms(hh, mm, ss):
    """Turn matched (hours-or-None, minutes, seconds) strings into total seconds."""
    h = int(hh) if hh else 0
    return h * 3600 + int(mm) * 60 + int(ss)


# ---------------------------------------------------------------------------
# Tracklist entry
# ---------------------------------------------------------------------------
class Track:
    """One recognised song. `start` is seconds from the video start (or None)."""
    __slots__ = ("start", "title", "artist", "source", "url")

    def __init__(self, start, title, artist="", source="", url=""):
        self.start = start
        self.title = (title or "").strip()
        self.artist = (artist or "").strip()
        self.source = source
        self.url = url

    def __repr__(self):
        return f"Track({self.start!r}, {self.title!r}, {self.artist!r}, {self.source!r})"

    def __eq__(self, other):
        return (isinstance(other, Track) and self.start == other.start
                and self.title == other.title and self.artist == other.artist
                and self.source == other.source and self.url == other.url)


# ---------------------------------------------------------------------------
# Text parsing -- the testable heart of the tool
# ---------------------------------------------------------------------------
# A timestamp: optional hours, then mm:ss (mm may be 1-2 digits).
_TS = r"(?:(\d{1,2}):)?(\d{1,2}):(\d{2})"
# A timestamp that may be wrapped in ()/[] and have surrounding junk.
_TS_LINE = re.compile(r"[\(\[]?\s*" + _TS + r"\s*[\)\]]?")

# Leading list numbering like "01.", "1)", "1 -", "#3".
_LEADING_NUM = re.compile(r"^\s*[#]?\d{1,3}[\.\)\-:]?\s+")
# Leftover bracket tags like [Official Video], (HD), (Lyrics).
_BRACKET_TAG = re.compile(r"[\(\[][^\)\]]*[\)\]]")
# URLs (YouTube renders setlist timestamps as [0:00](https://...&t=110s) links).
_URL = re.compile(r"https?://\S+")


def _clean_label(seg):
    """Turn a raw text segment following a timestamp into a clean song label."""
    seg = _URL.sub(" ", seg)              # drop markdown-link URLs
    seg = clean_song_text(seg)            # strip numbering + balanced (tags)
    seg = re.sub(r"[()\[\]]", " ", seg)   # stray brackets left by markdown links
    seg = re.sub(r"\s{2,}", " ", seg)
    return seg.strip(" -–—•·|\t")


def _split_artist_title(text):
    """Best-effort split of 'Artist - Title' into (artist, title).
    If there's no separator, everything is the title and artist is ''. """
    text = text.strip().strip("-–—•·|").strip()
    # Prefer the common " - " / en-dash / em-dash separators.
    for sep in (" - ", " – ", " — ", " -- "):
        if sep in text:
            left, right = text.split(sep, 1)
            return left.strip(), right.strip()
    # A bare hyphen with no spaces around it is risky (song titles use them),
    # so only fall back to it when nothing better was found.
    m = re.match(r"^(.+?)\s*[-–—]\s*(.+)$", text)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", text


def clean_song_text(text):
    """Strip list numbering and trailing bracket tags, collapse whitespace."""
    text = _LEADING_NUM.sub("", text)
    text = _BRACKET_TAG.sub("", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -–—•·|\t").strip()


def parse_timestamp_lines(text):
    """Parse a block of text (description or a comment) into a list of Track.

    Scans the whole text for timestamps (not line-by-line), so it handles both
    one-per-line setlists AND single-line/inline blobs like
    "0:00 Intro 1:50 Trôi 5:43 ..." as well as YouTube's markdown-link form
    "[0:00](https://...&t=110s) Intro [1:50](...) Trôi". The label for each
    timestamp is the text up to the next timestamp (trimmed to the first line
    when that line already carries the title, so following junk lines are
    excluded). Returns [] if fewer than 2 timestamps are found (too weak).
    """
    text = text or ""
    matches = list(_TS_LINE.finditer(text))
    if len(matches) < 2:
        return []

    tracks = []
    for i, m in enumerate(matches):
        start = parse_hms(m.group(1), m.group(2), m.group(3))
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg = text[m.end():seg_end]
        # Prefer the title on the same line as the timestamp; fall back to the
        # whole segment when the timestamp sits alone on its line.
        first_line = _clean_label(seg.split("\n", 1)[0])
        label = first_line if first_line else _clean_label(seg)
        if not label:
            continue
        artist, title = _split_artist_title(label)
        tracks.append(Track(start, title, artist, source=""))
    if len(tracks) < 2:
        return []
    return tracks


def count_timestamp_lines(text):
    """How many lines in `text` carry a timestamp -- used to score comments."""
    n = 0
    for raw in (text or "").splitlines():
        if _TS_LINE.search(raw):
            n += 1
    return n


def tracks_from_chapters(chapters):
    """Convert yt-dlp chapter dicts into Track objects."""
    out = []
    for ch in chapters or []:
        title = ch.get("title") or ""
        start = ch.get("start_time")
        artist, ttl = _split_artist_title(clean_song_text(title))
        out.append(Track(start, ttl, artist, source="chapters"))
    return out


def best_comment_tracklist(comments):
    """From a list of yt-dlp comment dicts, pick the one whose text has the most
    timestamp lines (a posted tracklist) and parse it. Needs >=3 timestamps.
    Returns a list of Track (source='comment') or []."""
    best_text, best_n = None, 0
    for c in comments or []:
        txt = c.get("text") or ""
        n = count_timestamp_lines(txt)
        if n > best_n:
            best_text, best_n = txt, n
    if best_n < 3:
        return []
    tracks = parse_timestamp_lines(best_text)
    for t in tracks:
        t.source = "comment"
    return tracks


def dedupe_consecutive(tracks):
    """Collapse runs of identical (title, artist) into one entry keeping the
    earliest timestamp. Case-insensitive on the key."""
    out = []
    for t in tracks:
        key = (t.title.lower(), t.artist.lower())
        if out and (out[-1].title.lower(), out[-1].artist.lower()) == key:
            # keep earliest start
            if t.start is not None and (out[-1].start is None or t.start < out[-1].start):
                out[-1].start = t.start
            continue
        out.append(t)
    return out


# ---------------------------------------------------------------------------
# Filename sanitising
# ---------------------------------------------------------------------------
_BAD_FN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name, fallback="tracklist"):
    """Make a string safe to use as a Windows filename."""
    name = _BAD_FN.sub("_", name or "")
    name = name.strip().strip(".")           # no trailing dot/space on Windows
    name = re.sub(r"\s{2,}", " ", name)
    name = name[:150].strip()
    return name or fallback


# ---------------------------------------------------------------------------
# yt-dlp driven steps (network) -- kept out of the parser tests
# ---------------------------------------------------------------------------
def fetch_metadata(ytdlp, url):
    """Return the yt-dlp single-json dict for a video (no download)."""
    cmd = as_list(ytdlp) + [
        "--dump-single-json", "--skip-download", "--no-warnings", url,
    ]
    return _run_json(cmd)


def fetch_comments(ytdlp, url, max_comments=100):
    """Return the list of comment dicts for a video (top-sorted)."""
    cmd = as_list(ytdlp) + [
        "--skip-download", "--write-comments",
        "--extractor-args",
        f"youtube:max_comments={max_comments},20,0,0;comment_sort=top",
        "--dump-single-json", "--no-warnings", url,
    ]
    data = _run_json(cmd)
    return data.get("comments") or []


def text_tracklist(meta, ytdlp=None, url=None, fetch_comments_enabled=True,
                   on_log=lambda m: None):
    """Try chapters -> description -> comments. Return (tracks, source) or ([], '')."""
    # 1) chapters
    chapters = meta.get("chapters")
    if chapters:
        tracks = tracks_from_chapters(chapters)
        if tracks:
            on_log(f"Using {len(tracks)} chapters from the video metadata.")
            return tracks, "chapters"

    # 2) description
    desc = meta.get("description") or ""
    tracks = parse_timestamp_lines(desc)
    if tracks:
        for t in tracks:
            t.source = "description"
        on_log(f"Parsed {len(tracks)} timestamped lines from the description.")
        return tracks, "description"

    # 3) comments
    if fetch_comments_enabled and ytdlp and url:
        on_log("No tracklist in description; scanning top comments...")
        try:
            comments = fetch_comments(ytdlp, url)
            tracks = best_comment_tracklist(comments)
            if tracks:
                on_log(f"Found a tracklist in a comment ({len(tracks)} songs).")
                return tracks, "comment"
        except Exception as e:
            on_log(f"Comment scan failed: {e}")

    return [], ""


def youtube_link_for(ytdlp, query):
    """ytsearch1 for `query`; return a youtu.be URL or '' on failure."""
    cmd = as_list(ytdlp) + [
        f"ytsearch1:{query}", "--flat-playlist", "--dump-single-json",
        "--no-warnings",
    ]
    try:
        data = _run_json(cmd)
        entries = data.get("entries") or []
        if not entries:
            return ""
        e = entries[0]
        vid = e.get("id")
        if vid:
            return f"https://youtu.be/{vid}"
        return e.get("url") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Audio recognition fallback (shazamio + ffmpeg)
# ---------------------------------------------------------------------------
def sample_times(duration, tracks, interval):
    """Decide which second-offsets to sample.
    If we have (rough) track start times, sample each track's midpoint; else
    sample every `interval` seconds across the whole video."""
    times = []
    starts = [t.start for t in tracks if t.start is not None]
    if len(starts) >= 2 and duration:
        starts = sorted(set(starts))
        for i, s in enumerate(starts):
            nxt = starts[i + 1] if i + 1 < len(starts) else duration
            mid = s + max(3, (nxt - s) / 2)
            if mid < duration:
                times.append(int(mid))
    elif duration:
        t = interval // 2 if interval else 30
        while t < duration:
            times.append(int(t))
            t += max(15, interval)
    return times


def download_audio(ytdlp, url, dest_dir, on_log=lambda m: None):
    """Download bestaudio as m4a into dest_dir. Return the audio file path."""
    outtmpl = os.path.join(dest_dir, "source.%(ext)s")
    cmd = as_list(ytdlp) + [
        "-f", "bestaudio", "-x", "--audio-format", "m4a",
        "-o", outtmpl, "--no-warnings", url,
    ]
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", env=env,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for f in os.listdir(dest_dir):
        if f.startswith("source."):
            return os.path.join(dest_dir, f)
    raise RuntimeError("Audio download failed (no file produced).")


def cut_clip(ffmpeg, audio_path, start, out_wav, seconds=CLIP_SECONDS):
    """Cut a mono 44.1k wav clip starting at `start` seconds."""
    cmd = as_list(ffmpeg) + [
        "-y", "-ss", str(start), "-t", str(seconds), "-i", audio_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1", out_wav,
    ]
    subprocess.run(cmd, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return out_wav


async def _shazam_recognize(shazam, path):
    """Await one recognition; return (title, artist) or (None, None)."""
    result = await shazam.recognize(path)
    track = (result or {}).get("track") or {}
    title = track.get("title")
    artist = track.get("subtitle")
    return title, artist


def recognize_clips(audio_path, times, ffmpeg, tmp_dir,
                    on_log=lambda m: None, should_stop=lambda: False):
    """Cut and Shazam each sample time. Returns a list of Track (source='shazam').
    Runs one asyncio event loop for the whole batch, sleeping politely between
    calls and retrying a miss once."""
    from shazamio import Shazam

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shazam = Shazam()
    tracks = []
    try:
        for i, t in enumerate(times, 1):
            if should_stop():
                on_log("Stopped.")
                break
            wav = os.path.join(tmp_dir, f"clip_{i:04d}.wav")
            try:
                cut_clip(ffmpeg, audio_path, t, wav)
            except Exception as e:
                on_log(f"[{fmt_time(t)}] clip cut failed: {e}")
                continue
            title = artist = None
            for attempt in (1, 2):
                try:
                    title, artist = loop.run_until_complete(
                        _shazam_recognize(shazam, wav))
                except Exception as e:
                    on_log(f"[{fmt_time(t)}] recognize error: {e}")
                    title = artist = None
                if title:
                    break
                if attempt == 1:
                    time.sleep(2.0)   # brief backoff before the single retry
            try:
                os.remove(wav)
            except OSError:
                pass
            if title:
                on_log(f"[{fmt_time(t)}] {artist or '?'} - {title}")
                tracks.append(Track(t, title, artist or "", source="shazam"))
            else:
                on_log(f"[{fmt_time(t)}] (no match)")
            time.sleep(1.5)           # be polite to the Shazam endpoint
    finally:
        loop.close()
    return dedupe_consecutive(tracks)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
def write_outputs(tracks, video_title, out_dir):
    """Write <title>.txt and <title>.csv. Returns (txt_path, csv_path)."""
    os.makedirs(out_dir, exist_ok=True)
    stem = sanitize_filename(video_title, "tracklist")
    txt_path = os.path.join(out_dir, stem + ".txt")
    csv_path = os.path.join(out_dir, stem + ".csv")

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"Tracklist for: {video_title}\n")
        f.write(f"Generated {datetime.now():%Y-%m-%d %H:%M}\n\n")
        for t in tracks:
            ts = f"[{fmt_time(t.start)}] " if t.start is not None else ""
            who = f"{t.artist} - {t.title}" if t.artist else t.title
            link = f"  {t.url}" if t.url else ""
            f.write(f"{ts}{who}{link}\n")

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "title", "artist", "source", "youtube_url"])
        for t in tracks:
            ts = fmt_time(t.start) if t.start is not None else ""
            w.writerow([ts, t.title, t.artist, t.source, t.url])

    return txt_path, csv_path


# ---------------------------------------------------------------------------
# Full pipeline (headless-friendly; takes callbacks, no Tkinter)
# ---------------------------------------------------------------------------
def find_songs(url, out_dir, ytdlp, ffmpeg=None, *,
               force_audio=False, fetch_comments_enabled=True,
               sample_interval=DEFAULT_SAMPLE_INTERVAL,
               on_log=lambda m: None, should_stop=lambda: False):
    """Run the whole pipeline. Returns (tracks, video_title, txt_path, csv_path).
    Raises on fatal errors (e.g. metadata unreachable)."""
    on_log("Fetching video metadata...")
    meta = fetch_metadata(ytdlp, url)
    if should_stop():
        return [], "", "", ""
    video_title = meta.get("title") or "tracklist"
    duration = meta.get("duration")
    on_log(f"Video: {video_title}"
           + (f"  ({fmt_time(duration)})" if duration else ""))

    tracks, source = ([], "")
    if not force_audio:
        tracks, source = text_tracklist(
            meta, ytdlp=ytdlp, url=url,
            fetch_comments_enabled=fetch_comments_enabled, on_log=on_log)

    if should_stop():
        return [], video_title, "", ""

    if not tracks or force_audio:
        if force_audio:
            on_log("Force audio recognition is on -- identifying by sound.")
        else:
            on_log("No text tracklist found -- falling back to audio recognition.")
        if not ffmpeg:
            on_log("[!] ffmpeg not found; cannot do audio recognition. "
                   "Run setup.bat.")
            if not tracks:
                return [], video_title, "", ""
        else:
            with tempfile.TemporaryDirectory(prefix="psf_") as tmp:
                on_log("Downloading audio (bestaudio)...")
                audio = download_audio(ytdlp, url, tmp, on_log=on_log)
                if should_stop():
                    return [], video_title, "", ""
                times = sample_times(duration, tracks, sample_interval)
                on_log(f"Sampling {len(times)} points, {CLIP_SECONDS}s each...")
                tracks = recognize_clips(audio, times, ffmpeg, tmp,
                                         on_log=on_log, should_stop=should_stop)
                source = "shazam"

    if should_stop():
        return tracks, video_title, "", ""

    if not tracks:
        on_log("No songs could be identified.")
        return [], video_title, "", ""

    # YouTube link lookup for each entry.
    on_log(f"Looking up YouTube links for {len(tracks)} songs...")
    for i, t in enumerate(tracks, 1):
        if should_stop():
            break
        if not t.title:
            continue
        query = f"{t.artist} {t.title}".strip()
        t.url = youtube_link_for(ytdlp, query)

    txt_path, csv_path = write_outputs(tracks, video_title, out_dir)
    on_log(f"\nSaved:\n  {txt_path}\n  {csv_path}")
    return tracks, video_title, txt_path, csv_path


# ---------------------------------------------------------------------------
# GUI (Tkinter) -- mirrors YT-M Downloader layout
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_TITLE)
        root.geometry("780x600")
        root.minsize(700, 520)

        self.log_q = queue.Queue()
        self.worker = None
        self._stop = False

        self._build_ui()
        self._poll_log()
        self._check_deps()

    # --- UI ---------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        frm = ttk.Frame(self.root)
        frm.pack(fill="x", **pad)

        ttk.Label(frm, text="Video URL:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.url_var, width=70).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=4)

        ttk.Label(frm, text="Save to:").grid(row=1, column=0, sticky="w")
        self.out_var = tk.StringVar(value=DEFAULT_OUTDIR)
        ttk.Entry(frm, textvariable=self.out_var, width=58).grid(
            row=1, column=1, columnspan=2, sticky="we", padx=4)
        ttk.Button(frm, text="Browse...", command=self._browse).grid(
            row=1, column=3, sticky="e")

        frm.columnconfigure(1, weight=1)

        # options
        opt = ttk.LabelFrame(self.root, text="Options", padding=8)
        opt.pack(fill="x", padx=8, pady=4)

        ttk.Label(opt, text="Audio sample interval (sec):").grid(
            row=0, column=0, sticky="w")
        self.interval_var = tk.IntVar(value=DEFAULT_SAMPLE_INTERVAL)
        ttk.Spinbox(opt, from_=30, to=600, increment=15, width=8,
                    textvariable=self.interval_var).grid(
            row=0, column=1, sticky="w", padx=8)

        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Force audio recognition",
                        variable=self.force_var).grid(
            row=0, column=2, sticky="w", padx=12)

        self.comments_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Fetch comments",
                        variable=self.comments_var).grid(
            row=0, column=3, sticky="w", padx=12)

        # buttons
        btns = ttk.Frame(self.root)
        btns.pack(fill="x", **pad)
        self.find_btn = ttk.Button(btns, text="Find Songs", command=self.find)
        self.find_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Open folder", command=self._open_folder).pack(
            side="right", padx=4)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=12)

        self.progress = ttk.Progressbar(self.root, mode="indeterminate")
        self.progress.pack(fill="x", padx=8, pady=4)

        # log box
        logfrm = ttk.Frame(self.root)
        logfrm.pack(fill="both", expand=True, padx=8, pady=6)
        self.log = tk.Text(logfrm, wrap="word", height=18, bg="#111", fg="#ddd",
                           insertbackground="#ddd", font=("Consolas", 9))
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logfrm, command=self.log.yview)
        sb.pack(side="right", fill="y")
        self.log.config(yscrollcommand=sb.set)

    def _browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or SCRIPT_DIR)
        if d:
            self.out_var.set(d)

    def _open_folder(self):
        d = self.out_var.get() or DEFAULT_OUTDIR
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)  # Windows only
        except AttributeError:
            try:
                subprocess.Popen(["xdg-open", d])
            except Exception:
                pass

    # --- logging ----------------------------------------------------------
    def logln(self, text=""):
        self.log_q.put(str(text) + "\n")

    def _poll_log(self):
        try:
            while True:
                line = self.log_q.get_nowait()
                self.log.insert("end", line)
                self.log.see("end")
        except queue.Empty:
            pass
        self.root.after(120, self._poll_log)

    def set_status(self, s):
        self.status_var.set(s)

    # --- dependency check -------------------------------------------------
    def _check_deps(self):
        self.ytdlp = find_executable("yt-dlp")
        self.ffmpeg = find_executable("ffmpeg")
        if self.ytdlp:
            self.logln("[ok] yt-dlp found.")
        else:
            self.logln("[!] yt-dlp not found. Run setup.bat (or: pip install yt-dlp).")
        if self.ffmpeg:
            self.logln("[ok] ffmpeg found.")
        else:
            self.logln("[!] ffmpeg not found. Needed for audio recognition. "
                       "Run setup.bat.")
        try:
            import shazamio  # noqa: F401
            self.logln("[ok] shazamio found.")
        except Exception as e:
            self.logln(f"[!] shazamio failed to load ({type(e).__name__}: {e}).")
            if "audioop" in str(e) or "pyaudioop" in str(e):
                self.logln("    Python 3.13+ removed the audioop module that "
                           "shazamio needs. Fix it with:")
                self.logln("      python -m pip install audioop-lts")
                self.logln("    (setup.bat now does this too), then reopen the app.")
            else:
                self.logln("    Audio recognition will be unavailable. "
                           "Run setup.bat (needs Python 3.10+).")
        self.logln("")

    # --- run / stop -------------------------------------------------------
    def _busy(self, busy):
        self.find_btn.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def stop(self):
        self._stop = True
        self.set_status("Stopping...")
        self.logln("[stopping after current step]")

    def find(self):
        if not self.ytdlp:
            messagebox.showerror(APP_TITLE, "yt-dlp not found. Run setup.bat first.")
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showerror(APP_TITLE, "Paste a video URL first.")
            return
        out_dir = self.out_var.get().strip() or DEFAULT_OUTDIR

        self._stop = False
        self._busy(True)
        self.set_status("Working...")
        self.logln(f"=== Find Songs @ {datetime.now():%H:%M:%S} ===")
        self.logln(f"URL: {url}\n")

        force = self.force_var.get()
        comments = self.comments_var.get()
        try:
            interval = int(self.interval_var.get())
        except (ValueError, tk.TclError):
            interval = DEFAULT_SAMPLE_INTERVAL

        def target():
            try:
                tracks, title, txt, csvp = find_songs(
                    url, out_dir, self.ytdlp, self.ffmpeg,
                    force_audio=force, fetch_comments_enabled=comments,
                    sample_interval=interval,
                    on_log=lambda m: self.logln(m),
                    should_stop=lambda: self._stop,
                )
                if self._stop:
                    self.root.after(0, lambda: self.set_status("Stopped."))
                elif tracks:
                    self.root.after(0, lambda: self.set_status(
                        f"Done -- {len(tracks)} songs found."))
                else:
                    self.root.after(0, lambda: self.set_status(
                        "Finished -- no songs identified."))
            except Exception as e:
                msg = str(e)
                self.logln(f"[error] {msg}")
                self.root.after(0, lambda: self.set_status("Failed -- see log."))
            finally:
                self.root.after(0, lambda: self._busy(False))

        self.worker = threading.Thread(target=target, daemon=True)
        self.worker.start()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("clam")
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    # GUI imports are deferred so the core functions above import cleanly in a
    # headless environment (tests, CI) that has no Tkinter display.
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    main()
