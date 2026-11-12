"""
Shared code of the `sample_*.py` scripts.

A sample is a set of clips of one video, chosen for annotation in place of
all its clips. The scripts find the clips of a video through the app config,
choose a sample, and store it in the annotation database, one row per
sampled clip in the `clip_samples` table.
"""
import argparse
import csv
import json
import os
import random
import re
import sqlite3
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(os.getenv(
    "APP_CONFIG_PATH",
    Path(__file__).resolve().parent.parent / "app" / "app_config.json"))
DEFAULT_SEED = 42

GUID_PATTERN = re.compile(r"cpb-aacip-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*")
DURATION_PATTERN = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")

CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS clip_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dataset_name TEXT NOT NULL,
        clip_id TEXT NOT NULL,
        strategy TEXT NOT NULL,
        params TEXT NOT NULL,
        stratum TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (dataset_name, clip_id)
    )
"""


class Video:
    """The clips of one segmented video, as its manifest lists them."""

    def __init__(self, language, manifest):
        self.language = language
        self.manifest = manifest
        self.stem = manifest.name[:-len(".manifest.csv")]
        with open(manifest, newline="", encoding="utf-8") as f:
            self.clips = [
                {"clip_id": r["clip_id"],
                 "start_ms": int(r["start_ms"]),
                 "end_ms": int(r["end_ms"])}
                for r in csv.DictReader(f)]

    def duration_ms(self):
        """
        :returns: the duration of the source video, from the ffmpeg header
            in `<stem>.silencedetect.log`
        """
        log = self.manifest.with_name(f"{self.stem}.silencedetect.log")
        with open(log, encoding="utf-8", errors="ignore") as f:
            m = DURATION_PATTERN.search(f.read())
        if not m:
            sys.exit(f"No duration in {log}")
        h, mnt, s = m.groups()
        return round((int(h) * 3600 + int(mnt) * 60 + float(s)) * 1000)


def midpoint_ms(clip):
    return (clip["start_ms"] + clip["end_ms"]) / 2


def load_config(path):
    if not path.is_file():
        sys.exit(f"App config not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def language_dirs(config, config_path):
    """
    :returns: the clip directory of each language, resolved as the app
        resolves it
    """
    base = config_path.parent / config["data_basepath"]
    return {lang: base / c["data_subpath"]
            for lang, c in config["languages"].items()}


def find_video(guid, dirs):
    """
    :returns: the video whose stem is `guid`, or else whose stem starts
        with the GUID `guid`
    :raises SystemExit: when no video or more than one video matches
    """
    manifests = [(lang, m) for lang, d in sorted(dirs.items())
                 for m in sorted(d.glob("*.manifest.csv"))]
    stem = lambda m: m.name[:-len(".manifest.csv")]
    found = [(lang, m) for lang, m in manifests if stem(m) == guid]
    if not found:
        found = [(lang, m) for lang, m in manifests
                 if GUID_PATTERN.match(stem(m))
                 and GUID_PATTERN.match(stem(m)).group(0) == guid]
    if len(found) != 1:
        names = ", ".join(f"{lang}/{stem(m)}" for lang, m in found)
        sys.exit(f"{guid}: {len(found)} matching videos {names}".rstrip())
    return Video(*found[0])


def open_db(path):
    """
    Opens the annotation database, and makes the `clip_samples` table when
    it does not exist.

    :raises SystemExit: for a database with clip times in seconds
    """
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(annotation_events)")}
    if cols and "start_ms" not in cols:
        conn.close()
        sys.exit(f"{path} has clip times in seconds, not in milliseconds")
    conn.execute(CREATE_TABLE)
    return conn


def parse_args(description, add_options):
    p = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-n", type=int, required=True,
                   help="number of clips to sample (see the description)")
    p.add_argument("guids", nargs="+", metavar="GUID",
                   help="video stem, or the GUID of a video")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED,
                   help=f"random seed, one generator per video "
                        f"(default: {DEFAULT_SEED})")
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG,
                   help=f"app config, for the clip directories and the "
                        f"database (default: {DEFAULT_CONFIG})")
    if add_options:
        add_options(p)
    args = p.parse_args()
    if args.n < 1:
        p.error("-n must be at least 1")
    return args


def main(description, strategy, choose, add_options=None, params=None):
    """
    Samples each video of the command line and stores the samples.

    :param choose: `choose(video, args, rng)` returns the sampled clips of
        a video as `(clip, stratum)` pairs
    :param params: `params(args)` returns the strategy options to record
        besides `n` and the seed
    """
    args = parse_args(description, add_options)
    config_path = args.config.resolve()
    config = load_config(config_path)
    dirs = language_dirs(config, config_path)
    videos = [find_video(guid, dirs) for guid in args.guids]
    stems = [v.stem for v in videos]
    if len(set(stems)) != len(stems):
        sys.exit("The same video is given twice")

    conn = open_db(config_path.parent / config["db_path"])
    sampled = [s for s in stems if conn.execute(
        "SELECT 1 FROM clip_samples WHERE dataset_name = ? LIMIT 1",
        (s,)).fetchone()]
    if sampled:
        conn.close()
        sys.exit(f"Already sampled: {', '.join(sampled)}")

    recorded = {"n": args.n, "seed": args.seed}
    if params:
        recorded.update(params(args))
    with conn:
        for video in videos:
            picked = choose(video, args, random.Random(args.seed))
            picked.sort(key=lambda p: p[0]["start_ms"])
            conn.executemany(
                "INSERT INTO clip_samples (dataset_name, clip_id, strategy, "
                "params, stratum) VALUES (?, ?, ?, ?, ?)",
                [(video.stem, clip["clip_id"], strategy,
                  json.dumps(recorded, sort_keys=True), stratum)
                 for clip, stratum in picked])
            print(f"{video.language}/{video.stem}: {len(picked)} of "
                  f"{len(video.clips)} clips ({strategy})")
    conn.close()
