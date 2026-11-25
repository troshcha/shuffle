#!/usr/bin/env python3
"""
Nightly shuffle for a big YouTube Music playlist.

Reads the source playlist (duplicates = higher priority), shuffles it so that
copies of the same track are spread evenly instead of clumping together, and
rewrites a separate target playlist with the result. The target playlist keeps
the same ID/link between runs.

Usage:
    python shuffle.py            # real run
    python shuffle.py --dry-run  # only fetch + shuffle, print stats, change nothing
"""
import argparse
import json
import logging
import random
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from ytmusicapi import YTMusic

# ---------------- settings ----------------
SOURCE_ID = "PL-AI4-ojKbHSNovg6wmGjbXnk72ejNC2s"  # playlist "3"
TARGET_TITLE = "Daily Shuffle"
MIN_GAP = 30      # try to keep at least this many tracks between copies of one song
CHUNK = 100       # tracks per API request
PAUSE = 2.0       # seconds between requests
RETRIES = 3
# ------------------------------------------

BASE = Path(__file__).resolve().parent
AUTH = BASE / "browser.json"
STATE = BASE / "state.json"

log = logging.getLogger("shuffle")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def status_ok(resp):
    status = resp.get("status") if isinstance(resp, dict) else resp
    return status == "STATUS_SUCCEEDED"


def with_retries(fn, what):
    for attempt in range(1, RETRIES + 1):
        try:
            resp = fn()
            if status_ok(resp):
                return
            log.warning("%s: unexpected response (attempt %d): %s", what, attempt, str(resp)[:300])
        except Exception as e:  # network hiccups, rate limits, etc.
            log.warning("%s: error (attempt %d): %s", what, attempt, e)
        time.sleep(PAUSE * attempt * 3)
    raise RuntimeError(f"{what}: failed after {RETRIES} attempts")


# ---------------- shuffle logic ----------------
def spread_shuffle(ids, rng):
    """
    Each song with N copies gets N evenly spaced slots on [0, 1) with a random
    phase and a bit of jitter; sorting by slot gives a shuffle where repeated
    songs are spread across the whole playlist instead of clumping.
    """
    counts = Counter(ids)
    keyed = []
    for vid, n in counts.items():
        phase = rng.random()
        for i in range(n):
            pos = (i + phase + rng.uniform(-0.3, 0.3)) / n
            keyed.append((pos % 1.0, rng.random(), vid))
    keyed.sort()
    return [vid for _, _, vid in keyed]


def enforce_gap(order, gap):
    """Swap tracks forward so the same song doesn't repeat within `gap` positions."""
    order = list(order)
    n = len(order)
    for i in range(n):
        window = set(order[max(0, i - gap):i])
        if order[i] in window:
            for j in range(i + 1, n):
                if order[j] not in window:
                    order[i], order[j] = order[j], order[i]
                    break
    return order


def min_distance(order):
    last, best = {}, None
    for i, vid in enumerate(order):
        if vid in last:
            d = i - last[vid]
            best = d if best is None else min(best, d)
        last[vid] = i
    return best


# ---------------- YouTube Music ----------------
def fetch_source(yt):
    pl = yt.get_playlist(SOURCE_ID, limit=None)
    tracks = pl.get("tracks") or []
    ids = [t["videoId"] for t in tracks if t.get("videoId") and t.get("isAvailable", True)]
    skipped = len(tracks) - len(ids)
    log.info("source: %d tracks fetched (%d unavailable skipped), declared %s",
             len(tracks), skipped, pl.get("trackCount"))
    return ids


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2))


def get_target(yt, state):
    # get_playlist() crashes on empty playlists, so look the target up in the library instead
    library = yt.get_library_playlists(limit=None)
    ids = {p["playlistId"] for p in library}
    tid = state.get("target_id")
    if tid in ids:
        return tid
    if tid:
        log.warning("saved target playlist %s not found in library", tid)
    # reuse an existing playlist with the same title instead of making duplicates
    for p in library:
        if p.get("title") == TARGET_TITLE:
            tid = p["playlistId"]
            log.info("found existing target playlist %s by title", tid)
            break
    else:
        tid = yt.create_playlist(TARGET_TITLE, "Auto-shuffled nightly from playlist '3'",
                                 privacy_status="PRIVATE")
        if not isinstance(tid, str):
            raise RuntimeError(f"create_playlist failed: {tid}")
        log.info("created target playlist %s", tid)
        state["filled"] = 0
    state["target_id"] = tid
    save_state(state)
    return tid


def clear_playlist(yt, tid, state):
    try:
        pl = yt.get_playlist(tid, limit=None)
    except KeyError:
        # ytmusicapi can't parse empty playlists; only trust that if we know it's empty
        if state.get("filled", 0) == 0:
            log.info("target playlist is empty, nothing to clear")
            return
        raise
    items = [{"videoId": t["videoId"], "setVideoId": t["setVideoId"]}
             for t in (pl.get("tracks") or []) if t.get("setVideoId")]
    log.info("clearing %d tracks from target", len(items))
    for k, part in enumerate(chunks(items, CHUNK), 1):
        with_retries(lambda: yt.remove_playlist_items(tid, part), f"remove chunk {k}")
        time.sleep(PAUSE)


MAX_SKIPPED = 30  # more failures than this = something systemic (auth, limits), stop


def add_part(yt, tid, part, skipped):
    """Add a batch; if YT rejects it, split in halves to isolate and skip bad tracks."""
    err = None
    for attempt in range(2):
        try:
            resp = yt.add_playlist_items(tid, part, duplicates=True)
            if status_ok(resp):
                return
            err = str(resp)[:300]
        except Exception as e:
            err = str(e).splitlines()[0]
        time.sleep(PAUSE * (attempt + 1))
    if len(part) == 1:
        skipped.append(part[0])
        log.warning("skipping track https://music.youtube.com/watch?v=%s: %s", part[0], err)
        if len(skipped) > MAX_SKIPPED:
            raise RuntimeError(f"too many rejected tracks ({len(skipped)}), last error: {err}")
        return
    mid = len(part) // 2
    add_part(yt, tid, part[:mid], skipped)
    time.sleep(PAUSE)
    add_part(yt, tid, part[mid:], skipped)


def fill_playlist(yt, tid, order):
    log.info("adding %d tracks to target", len(order))
    skipped = []
    for k, part in enumerate(chunks(order, CHUNK), 1):
        add_part(yt, tid, part, skipped)
        time.sleep(PAUSE)
    if skipped:
        log.warning("%d track copies were rejected by YouTube: %s",
                    len(skipped), ", ".join(sorted(set(skipped))))
    return len(order) - len(skipped)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="don't modify anything")
    ap.add_argument("--seed", type=int, help="fixed random seed (for testing)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    yt = YTMusic(str(AUTH))
    ids = fetch_source(yt)
    if not ids:
        log.error("source playlist is empty or unreadable, aborting")
        return 1

    rng = random.Random(args.seed)
    order = enforce_gap(spread_shuffle(ids, rng), MIN_GAP)
    assert Counter(order) == Counter(ids)

    counts = Counter(ids)
    log.info("unique songs: %d, max copies of one song: %d, min distance between copies: %s",
             len(counts), max(counts.values()), min_distance(order))

    if args.dry_run:
        log.info("dry run, nothing changed")
        return 0

    state = load_state()
    tid = get_target(yt, state)
    clear_playlist(yt, tid, state)
    state["filled"] = 0
    save_state(state)
    state["filled"] = fill_playlist(yt, tid, order)
    save_state(state)
    try:
        yt.edit_playlist(tid, title=TARGET_TITLE, description=f"Auto-shuffled from '3' on {datetime.now():%Y-%m-%d %H:%M}")
    except Exception as e:
        log.warning("couldn't update description: %s", e)
    log.info("done: https://music.youtube.com/playlist?list=%s", tid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
