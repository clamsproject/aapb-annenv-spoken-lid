"""
Cuts one video into short speech clips for the annotation app.

Finds silences with ffmpeg `silencedetect`, packs the speech between them
into clips, and writes to the output directory, for the video stem `<stem>`
(the video file name without its last extension):

* `<stem>_<nnnn>_<start_ms>_<end_ms>.mp4` and `.wav` for each clip
* `<stem>.manifest.csv`, the clip list that the app reads
* `<stem>.silencedetect.log`, the raw ffmpeg output

Requires `ffmpeg` and `ffprobe` on the PATH. Refuses to run when the video
is already segmented in the output directory.
"""
import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# --- ffmpeg silencedetect knobs ---
NOISE_DB = "-35dB"
MIN_SIL_DUR = 0.40
DROP_INITIAL = 0.0

# --- clip construction knobs ---
# Clip length. The first clip sets (one Yup'ik and one Samoan video, both
# partly labeled) used 35-80 s clips, about 60 s. Later sets use these
# shorter clips.
TARGET_LEN = 15.0
MIN_LEN = 8.0
MAX_LEN = 20.0
MERGE_GAP = 0.50
PAD_BEFORE = 0.20
PAD_AFTER = 0.20

# Manifest columns. The app checks for the same columns.
MANIFEST_FIELDS = [
    "clip_id", "guid", "source_tag", "source_video",
    "start_ms", "end_ms", "duration_ms",
    "video_filename", "audio_filename",
]

pat_start = re.compile(r"silence_start:\s*([0-9.]+)")
pat_end = re.compile(r"silence_end:\s*([0-9.]+)")


# =========================
# HELPERS
# =========================
def clamp(x, lo, hi):
    return max(lo, min(x, hi))


def ffprobe_duration(path):
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def run_silencedetect(video_path, log_path):
    cmd = [
        "ffmpeg", "-nostdin", "-y",
        "-i", str(video_path),
        "-af", f"silencedetect=noise={NOISE_DB}:d={MIN_SIL_DUR}",
        "-f", "null", "-"
    ]
    with open(log_path, "w", encoding="utf-8") as f:
        subprocess.run(cmd, stdout=f, stderr=f, check=True)


def parse_silences(log_path):
    sil_starts = []
    sil_ends = []

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = pat_start.search(line)
            if m:
                sil_starts.append(float(m.group(1)))
                continue
            m = pat_end.search(line)
            if m:
                sil_ends.append(float(m.group(1)))

    pairs = []
    j = 0
    for s0 in sil_starts:
        while j < len(sil_ends) and sil_ends[j] <= s0:
            j += 1
        if j < len(sil_ends):
            s1 = sil_ends[j]
            dur = s1 - s0
            if dur >= MIN_SIL_DUR:
                pairs.append((s0, s1, dur))
            j += 1
    return pairs


def silences_to_speech_intervals(silences, duration, drop_initial=0.0):
    speech = []
    cur = max(0.0, drop_initial)

    for s0, s1, _ in silences:
        if s1 <= cur:
            continue
        if s0 > cur:
            speech.append((cur, s0))
        cur = max(cur, s1)

    if cur < duration:
        speech.append((cur, duration))

    speech = [(a, b) for a, b in speech if (b - a) >= 0.5]
    return speech


def merge_small_gaps(intervals, max_gap=MERGE_GAP):
    if not intervals:
        return []

    merged = [intervals[0]]
    for a, b in intervals[1:]:
        prev_a, prev_b = merged[-1]
        if a - prev_b <= max_gap:
            merged[-1] = (prev_a, b)
        else:
            merged.append((a, b))
    return merged


def pack_long_interval(start, end, silence_midpoints):
    out = []
    cur = start

    while (end - cur) > MAX_LEN:
        lo = cur + MIN_LEN
        hi = cur + MAX_LEN
        preferred = cur + TARGET_LEN

        candidates = [c for c in silence_midpoints if lo <= c <= hi]

        if candidates:
            split = min(candidates, key=lambda c: abs(c - preferred))
        else:
            split = preferred

        out.append((cur, split))
        cur = split

    if end > cur:
        out.append((cur, end))

    return out


def pack_speech_into_clips(speech_intervals, silence_midpoints, duration):
    if not speech_intervals:
        return []

    coarse = []
    cur_start, cur_end = speech_intervals[0]

    for a, b in speech_intervals[1:]:
        proposed_end = b
        proposed_len = proposed_end - cur_start

        if proposed_len <= MAX_LEN:
            cur_end = proposed_end
        else:
            coarse.append((cur_start, cur_end))
            cur_start, cur_end = a, b

    coarse.append((cur_start, cur_end))

    refined = []
    for a, b in coarse:
        if (b - a) <= MAX_LEN:
            refined.append((a, b))
        else:
            refined.extend(pack_long_interval(a, b, silence_midpoints))

    merged = []
    for a, b in refined:
        if not merged:
            merged.append((a, b))
            continue

        if (b - a) < MIN_LEN:
            prev_a, prev_b = merged[-1]
            if (b - prev_a) <= MAX_LEN:
                merged[-1] = (prev_a, b)
            else:
                merged.append((a, b))
        else:
            merged.append((a, b))

    padded = []
    for a, b in merged:
        pa = clamp(a - PAD_BEFORE, 0.0, duration)
        pb = clamp(b + PAD_AFTER, 0.0, duration)
        if (pb - pa) >= 1.0:
            padded.append((pa, pb))

    return padded


def to_ms(segments):
    """Converts `(start, end)` pairs in seconds to integer milliseconds."""
    return [(round(a * 1000), round(b * 1000)) for a, b in segments]


def existing_clip_files(outdir, stem):
    """Clip files of `stem` in `outdir`, left by an earlier run."""
    pattern = re.compile(re.escape(stem) + r"_\d{4}_\d+_\d+\.(mp4|wav)")
    return sorted(p for p in outdir.iterdir() if pattern.fullmatch(p.name))


def export_segments(video_path, stem, segments_ms, outdir, source_tag):
    rows = []

    for idx, (start_ms, end_ms) in enumerate(segments_ms, start=1):
        clip_id = f"{stem}_{idx:04d}_{start_ms}_{end_ms}"
        out_mp4 = outdir / f"{clip_id}.mp4"
        out_wav = outdir / f"{clip_id}.wav"
        ss = f"{start_ms / 1000:.3f}"
        to = f"{end_ms / 1000:.3f}"

        # One ffmpeg run writes both files; the options before each output
        # file apply to that file only.
        cmd = [
            "ffmpeg", "-nostdin", "-y",
            "-ss", ss,
            "-to", to,
            "-i", str(video_path),
            "-c:v", "copy",
            "-c:a", "aac",
            "-movflags", "+faststart",
            str(out_mp4),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            str(out_wav)
        ]
        subprocess.run(cmd, check=True)

        rows.append({
            "clip_id": clip_id,
            "guid": stem,
            "source_tag": source_tag,
            "source_video": video_path.name,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": end_ms - start_ms,
            "video_filename": out_mp4.name,
            "audio_filename": out_wav.name,
        })

    # Written last, and atomically: an existing manifest means the video
    # is completely segmented.
    manifest = outdir / f"{stem}.manifest.csv"
    tmp = manifest.with_name(manifest.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, manifest)

    print(f"Made {len(rows)} clips in {outdir}")
    print(f"Wrote {manifest}")


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("video", type=Path, help="source video file")
    p.add_argument(
        "-o", "--outdir", type=Path, required=True,
        help="output directory for the clips of the video's target "
             "language, created if missing. The outdir should match what "
             "the app config says: <data_basepath>/<data_subpath> of the "
             "language.")
    return p.parse_args()


def main():
    args = parse_args()
    video_path = args.video
    outdir = args.outdir

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"{tool} is not on the PATH")
    if not video_path.is_file():
        sys.exit(f"Video not found: {video_path}")

    stem = video_path.stem
    outdir.mkdir(parents=True, exist_ok=True)
    manifest = outdir / f"{stem}.manifest.csv"
    if manifest.exists():
        sys.exit(f"{stem} is already segmented: {manifest} exists")
    leftovers = existing_clip_files(outdir, stem)
    if leftovers:
        sys.exit(f"{len(leftovers)} clip files of {stem} are in {outdir} "
                 f"without a manifest, for example {leftovers[0].name}; "
                 f"delete them and run again")

    source_tag = video_path.resolve().parent.name
    log_path = outdir / f"{stem}.silencedetect.log"
    print(f"[START] {video_path}")

    duration = ffprobe_duration(video_path)
    run_silencedetect(video_path, log_path)
    silences = parse_silences(log_path)

    silence_midpoints = sorted((s0 + s1) / 2.0 for s0, s1, _ in silences)
    speech = silences_to_speech_intervals(silences, duration, DROP_INITIAL)
    speech = merge_small_gaps(speech, MERGE_GAP)

    segments = pack_speech_into_clips(speech, silence_midpoints, duration)
    if not segments:
        sys.exit(f"No clips for {video_path}: no speech found")
    export_segments(video_path, stem, to_ms(segments), outdir, source_tag)

    print(f"[DONE] {stem}")


if __name__ == "__main__":
    main()
