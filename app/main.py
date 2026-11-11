import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

APP_CONFIG_PATH = Path(os.getenv(
    "APP_CONFIG_PATH", Path(__file__).parent / "app_config.json"))

# Manifest columns. preprocess.py writes the same columns.
REQUIRED_COLS = {
    "clip_id",
    "guid",
    "start_ms",
    "end_ms",
    "duration_ms",
    "video_filename",
    "audio_filename",
}

# The label that opens the field for the order of the languages
MIXED = "Mixed"

# Suffixes that video stems can carry after the GUID; export.py removes the
# same suffixes.
GUID_SUFFIX = re.compile(r"(_trimmed|\.mp4|\.h264)$")


def is_mixed(label: str) -> bool:
    return label.strip().casefold() == MIXED.casefold()


def ensure_parent_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_app_config() -> dict[str, Any]:
    if not APP_CONFIG_PATH.is_file():
        st.error(f"App config not found: {APP_CONFIG_PATH}. Copy "
                 "app_config.example.json to it, or set APP_CONFIG_PATH "
                 "to the config file.")
        st.stop()
    with APP_CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_db_path(config: dict[str, Any]) -> Path:
    """
    :returns: `db_path` of the config; a relative path is relative to
        the directory of the config file
    """
    db_path = config.get("db_path")
    if not db_path:
        st.error(f"No db_path in {APP_CONFIG_PATH}.")
        st.stop()
    return APP_CONFIG_PATH.parent / db_path


def get_language_dirs(config: dict[str, Any]) -> dict[str, Path]:
    """
    :returns: the clip directory of each language in the config,
        `<data_basepath>/<data_subpath>`; a relative `data_basepath` is
        relative to the directory of the config file
    """
    basepath = config.get("data_basepath")
    if not basepath:
        st.error(f"No data_basepath in {APP_CONFIG_PATH}.")
        st.stop()
    dirs = {}
    for language, lang_cfg in config.get("languages", {}).items():
        subpath = lang_cfg.get("data_subpath")
        if not subpath or Path(subpath).is_absolute():
            st.error(f"Language {language} in {APP_CONFIG_PATH} needs a "
                     f"relative data_subpath.")
            st.stop()
        dirs[language] = APP_CONFIG_PATH.parent / basepath / subpath
    return dirs


def get_labels(config: dict[str, Any], language: str) -> list[str]:
    return list(config["languages"][language].get("labels", []))


def get_user_config(config: dict[str, Any], username: str) -> dict[str, Any] | None:
    return config.get("users", {}).get(username)


def password_matches(user_cfg: dict[str, Any], supplied_password: str) -> bool:
    stored_plain = user_cfg.get("password")
    stored_sha256 = user_cfg.get("password_sha256")
    if stored_sha256:
        return sha256_text(supplied_password) == stored_sha256
    if stored_plain is not None:
        return supplied_password == stored_plain
    return False


def init_db() -> None:
    ensure_parent_dir(DATABASE_PATH)
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS annotation_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            clip_id TEXT NOT NULL,
            dataset_name TEXT DEFAULT '',
            guid TEXT,
            start_ms INTEGER,
            end_ms INTEGER,
            duration_ms INTEGER,
            video_filename TEXT,
            audio_filename TEXT,
            video_codec_used TEXT,
            label TEXT,
            notes TEXT DEFAULT '',
            mixed_order TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            view_count INTEGER DEFAULT 0,
            total_time_sec REAL DEFAULT 0,
            last_visit_started_at REAL,
            is_edit INTEGER DEFAULT 0
        )
        """
    )

    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(annotation_events)").fetchall()}
    conn.commit()
    conn.close()
    if "start_ms" not in existing_cols:
        st.error(f"{DATABASE_PATH} has clip times in seconds, not in "
                 "milliseconds. Migrate it before use.")
        st.stop()


def load_manifest(manifest: Path, language: str) -> pd.DataFrame:
    """
    Reads the manifest of one video. Its clips are in the directory of the
    manifest, and `dataset_name` is the video stem (the `guid` column).
    """
    df = pd.read_csv(manifest)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        st.error(f"Manifest {manifest} missing columns: {sorted(missing)}")
        st.stop()

    clips_dir = manifest.parent
    df["clip_id"] = df["clip_id"].astype(str)
    df["guid"] = df["guid"].astype(str)
    df["language"] = language
    df["dataset_name"] = df["guid"]
    df["unique_clip_id"] = df["dataset_name"] + "::" + df["clip_id"]
    df["video_path"] = df["video_filename"].apply(lambda x: str(clips_dir / x))
    df["audio_path"] = df["audio_filename"].apply(lambda x: str(clips_dir / x))
    df["video_exists"] = df["video_path"].apply(os.path.exists)
    return df


def load_user_manifests(config: dict[str, Any], user_cfg: dict[str, Any]) -> pd.DataFrame:
    """
    :returns: the clips of every `*.manifest.csv` in the clip directory of
        each language of the user, in the order of the languages, the
        manifest file names and the manifest rows
    """
    language_dirs = get_language_dirs(config)
    frames = []
    for language in user_cfg.get("languages", []):
        if language not in language_dirs:
            st.error(f"Unknown language {language} for this user in "
                     f"{APP_CONFIG_PATH}.")
            st.stop()
        clips_dir = language_dirs[language]
        if not clips_dir.is_dir():
            st.error(f"Clip directory of {language} not found: {clips_dir}")
            st.stop()
        frames.extend(load_manifest(manifest, language)
                      for manifest in sorted(clips_dir.glob("*.manifest.csv")))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def load_samples() -> dict[str, set[str]]:
    """
    :returns: the sampled clip IDs of each sampled video, from the
        `clip_samples` table that the sampling scripts of data_io write;
        empty when the table does not exist
    """
    conn = sqlite3.connect(DATABASE_PATH)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'clip_samples'").fetchone()
        if not exists:
            return {}
        samples = {}
        for stem, clip_id in conn.execute(
                "SELECT dataset_name, clip_id FROM clip_samples"):
            samples.setdefault(stem, set()).add(clip_id)
        return samples
    finally:
        conn.close()


def apply_samples(df: pd.DataFrame) -> pd.DataFrame:
    """
    Keeps only the sampled clips of each sampled video, and all clips of
    the other videos. Adds `video_clips`, the number of clips of the video,
    and `sampled`, whether the video has a sample.
    """
    df = df.copy()
    df["video_clips"] = df.groupby("dataset_name")["clip_id"].transform("size")
    samples = {stem: ids for stem, ids in load_samples().items()
               if stem in set(df["dataset_name"])}
    for stem, clip_ids in samples.items():
        missing = clip_ids - set(df.loc[df["dataset_name"] == stem, "clip_id"])
        if missing:
            st.error(f"The sample of {stem} has {len(missing)} clips that "
                     f"are not in its manifest, e.g. {sorted(missing)[0]}.")
            st.stop()
    keep = [stem not in samples or clip_id in samples[stem]
            for stem, clip_id in zip(df["dataset_name"], df["clip_id"])]
    df = df[keep].reset_index(drop=True)
    df["sampled"] = df["dataset_name"].isin(samples)
    return df


def source_link(config: dict[str, Any], stem: str) -> str:
    """
    :returns: an HTML link to the source of a video, from the `source_url`
        template of the config (`{guid}` is the stem without suffixes such
        as `.mp4`), or an empty string without a template
    """
    template = config.get("source_url")
    if not template:
        return ""
    guid = GUID_SUFFIX.sub("", stem)
    url = template.format(guid=guid, stem=stem)
    return f'<a href="{url}" target="_blank">{guid}</a>'


def load_latest_annotations(username: str) -> dict[str, dict[str, Any]]:
    conn = sqlite3.connect(DATABASE_PATH)
    query = """
        SELECT
            e.clip_id,
            e.dataset_name,
            e.label,
            e.notes,
            e.mixed_order,
            e.view_count,
            e.total_time_sec,
            e.last_visit_started_at,
            e.id
        FROM annotation_events e
        INNER JOIN (
            SELECT dataset_name, clip_id, MAX(id) AS max_id
            FROM annotation_events
            WHERE username = ?
            GROUP BY dataset_name, clip_id
        ) latest
        ON e.id = latest.max_id
        WHERE e.username = ?
    """
    saved = pd.read_sql_query(query, conn, params=(username, username))
    conn.close()

    ann = {}
    for _, r in saved.iterrows():
        unique_clip_id = f"{r['dataset_name']}::{r['clip_id']}"
        ann[unique_clip_id] = {
            "label": r["label"] if pd.notna(r["label"]) else "",
            "notes": r["notes"] if pd.notna(r["notes"]) else "",
            "mixed_order": r["mixed_order"] if "mixed_order" in r and pd.notna(r["mixed_order"]) else "",
            "view_count": int(r["view_count"]) if pd.notna(r["view_count"]) else 0,
            "total_time_sec": float(r["total_time_sec"]) if pd.notna(r["total_time_sec"]) else 0.0,
            "last_visit_started_at": float(r["last_visit_started_at"]) if pd.notna(r["last_visit_started_at"]) else None,
            "saved": True,
            "event_id": int(r["id"]),
        }
    return ann


def insert_annotation_event(
    username: str,
    manifest_row: pd.Series,
    label: str,
    *,
    notes: str = "",
    mixed_order: str = "",
    view_count: int = 0,
    total_time_sec: float = 0.0,
    last_visit_started_at: float | None = None,
    is_edit: bool = False,
) -> None:
    conn = sqlite3.connect(DATABASE_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO annotation_events (
            username, clip_id, dataset_name, guid, start_ms, end_ms, duration_ms,
            video_filename, audio_filename, video_codec_used, label, notes, mixed_order,
            updated_at, view_count, total_time_sec, last_visit_started_at, is_edit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
        """,
        (
            username,
            str(manifest_row["clip_id"]),
            str(manifest_row["dataset_name"]),
            str(manifest_row["guid"]),
            int(manifest_row["start_ms"]),
            int(manifest_row["end_ms"]),
            int(manifest_row["duration_ms"]),
            manifest_row["video_filename"],
            manifest_row["audio_filename"],
            manifest_row.get("video_codec_used", ""),
            label,
            notes,
            mixed_order,
            int(view_count),
            float(total_time_sec),
            last_visit_started_at,
            1 if is_edit else 0,
        ),
    )
    conn.commit()
    conn.close()


def clamp_idx(i: int, total: int) -> int:
    return max(0, min(i, total - 1)) if total else 0


def start_timer_for_clip(unique_clip_id: str) -> None:
    now = time.time()
    previous_clip_id = st.session_state.get("current_clip_id")
    previous_enter_time = st.session_state.get("clip_enter_time")

    if previous_clip_id is not None and previous_enter_time is not None and previous_clip_id != unique_clip_id:
        elapsed = max(0.0, now - previous_enter_time)
        prev_item = st.session_state.ann.get(previous_clip_id, {})
        prev_item["total_time_sec"] = float(prev_item.get("total_time_sec", 0.0)) + elapsed
        st.session_state.ann[previous_clip_id] = prev_item

    if previous_clip_id != unique_clip_id:
        item = st.session_state.ann.get(unique_clip_id, {"label": "", "notes": "", "mixed_order": "", "saved": False})
        item["view_count"] = int(item.get("view_count", 0)) + 1
        item["last_visit_started_at"] = now
        st.session_state.ann[unique_clip_id] = item
        st.session_state.current_clip_id = unique_clip_id
        st.session_state.clip_enter_time = now


def save_annotation_if_needed(row: pd.Series, label: str, notes: str, mixed_order: str = "") -> bool:
    unique_clip_id = str(row["unique_clip_id"])
    prior_saved_label = st.session_state.saved_labels.get(unique_clip_id, "")
    prior_saved_notes = st.session_state.saved_notes.get(unique_clip_id, "")
    prior_saved_mixed_order = st.session_state.saved_mixed_orders.get(unique_clip_id, "")

    if not label and not notes.strip() and not mixed_order.strip():
        return False

    if label == prior_saved_label and notes == prior_saved_notes and mixed_order == prior_saved_mixed_order:
        return False

    item = st.session_state.ann.get(unique_clip_id, {})
    is_edit = bool(prior_saved_label or prior_saved_notes or prior_saved_mixed_order)

    insert_annotation_event(
        st.session_state.username,
        row,
        label,
        notes=notes,
        mixed_order=mixed_order,
        view_count=item.get("view_count", 1),
        total_time_sec=item.get("total_time_sec", 0.0),
        last_visit_started_at=item.get("last_visit_started_at"),
        is_edit=is_edit,
    )

    item["label"] = label
    item["notes"] = notes
    item["mixed_order"] = mixed_order
    item["saved"] = True
    st.session_state.ann[unique_clip_id] = item
    st.session_state.saved_labels[unique_clip_id] = label
    st.session_state.saved_notes[unique_clip_id] = notes
    st.session_state.saved_mixed_orders[unique_clip_id] = mixed_order
    return True


def maybe_auto_save_label_only(row: pd.Series, selected_label: str) -> None:
    unique_clip_id = str(row["unique_clip_id"])
    if not selected_label:
        return

    prior_saved_label = st.session_state.saved_labels.get(unique_clip_id, "")
    if selected_label == prior_saved_label:
        return

    notes_key = f"notes_{unique_clip_id}"
    mixed_key = f"mixed_order_{unique_clip_id}"
    current_notes = st.session_state.get(notes_key, st.session_state.saved_notes.get(unique_clip_id, ""))
    current_mixed_order = st.session_state.get(mixed_key, st.session_state.saved_mixed_orders.get(unique_clip_id, ""))

    item = st.session_state.ann.get(unique_clip_id, {})
    is_edit = bool(
        prior_saved_label
        or st.session_state.saved_notes.get(unique_clip_id, "")
        or st.session_state.saved_mixed_orders.get(unique_clip_id, "")
    )

    insert_annotation_event(
        st.session_state.username,
        row,
        selected_label,
        notes=current_notes,
        mixed_order=current_mixed_order if is_mixed(selected_label) else "",
        view_count=item.get("view_count", 1),
        total_time_sec=item.get("total_time_sec", 0.0),
        last_visit_started_at=item.get("last_visit_started_at"),
        is_edit=is_edit,
    )

    item["label"] = selected_label
    item["notes"] = current_notes
    item["mixed_order"] = current_mixed_order if is_mixed(selected_label) else ""
    item["saved"] = True
    st.session_state.ann[unique_clip_id] = item
    st.session_state.saved_labels[unique_clip_id] = selected_label
    st.session_state.saved_notes[unique_clip_id] = current_notes
    st.session_state.saved_mixed_orders[unique_clip_id] = current_mixed_order if is_mixed(selected_label) else ""
    st.session_state.flash_message = f"Saved: {selected_label}"


def save_current_clip_before_navigation(row: pd.Series) -> None:
    unique_clip_id = str(row["unique_clip_id"])
    label_key = f"label_{unique_clip_id}"
    notes_key = f"notes_{unique_clip_id}"
    mixed_key = f"mixed_order_{unique_clip_id}"

    current_label = st.session_state.get(label_key, "")
    current_notes = st.session_state.get(notes_key, "")
    current_mixed_order = st.session_state.get(mixed_key, "") if is_mixed(current_label) else ""

    if save_annotation_if_needed(row, current_label, current_notes, current_mixed_order):
        st.session_state.flash_message = "Saved"


def get_previous_note(df: pd.DataFrame, idx: int) -> str:
    if idx <= 0:
        return ""
    prev_unique = str(df.iloc[idx - 1]["unique_clip_id"])
    return st.session_state.saved_notes.get(prev_unique, "")


st.set_page_config(page_title="Spoken Language Annotation", layout="centered")
st.markdown(
    """
    <style>
      header[data-testid="stHeader"] {display: none;}
      #MainMenu {visibility: hidden;}
      footer {visibility: hidden;}
      .stAppToolbar {display: none;}

      .block-container {
        padding-top: 0.20rem;
        padding-bottom: 1rem;
        max-width: 900px;
      }

      h4 {
        margin-top: 0rem !important;
        margin-bottom: 0.15rem !important;
      }

      .clip-meta {
        color: rgba(49, 51, 63, 0.75);
        font-size: 0.82rem;
        margin-top: 0.05rem;
        margin-bottom: 0.15rem;
        text-align: center;
      }

      .small-muted {
        color: rgba(49, 51, 63, 0.75);
        font-size: 0.85rem;
        text-align: center;
        margin-bottom: 0.1rem;
      }

      div[role="radiogroup"] {
        justify-content: center;
        gap: 0.5rem;
      }

      video {
        max-height: 330px !important;
      }

      div[data-testid="stVideo"] {
        margin-bottom: 0rem !important;
      }

      div[data-testid="stRadio"] {
        margin-top: 0rem !important;
        margin-bottom: 0.25rem !important;
      }

      div[data-testid="stTextArea"] {
        margin-top: 0rem !important;
      }
    </style>
    """,
    unsafe_allow_html=True,
)

config = load_app_config()
DATABASE_PATH = get_db_path(config)
init_db()

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title(config.get("app_title", "Spoken Language Annotation"))

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Sign in", use_container_width=True)

    if submitted:
        clean_username = username.strip()
        user_cfg = get_user_config(config, clean_username)
        if not user_cfg:
            st.error("Unknown username.")
        elif not password_matches(user_cfg, password):
            st.error("Incorrect password.")
        else:
            st.session_state.authenticated = True
            st.session_state.username = clean_username
            st.session_state.display_name = user_cfg.get("display_name", clean_username)
            st.session_state.idx = 0
            st.session_state.ann = load_latest_annotations(clean_username)
            st.session_state.saved_labels = {
                clip_id: item.get("label", "") for clip_id, item in st.session_state.ann.items()
            }
            st.session_state.saved_notes = {
                clip_id: item.get("notes", "") for clip_id, item in st.session_state.ann.items()
            }
            st.session_state.saved_mixed_orders = {
                clip_id: item.get("mixed_order", "") for clip_id, item in st.session_state.ann.items()
            }
            st.session_state.flash_message = ""
            st.rerun()

    st.stop()

username = st.session_state.username
user_cfg = get_user_config(config, username)
if not user_cfg:
    st.error("Your user configuration is missing.")
    st.stop()

df = load_user_manifests(config, user_cfg)

if df.empty:
    st.error("No clips are assigned to this user.")
    st.stop()
df = apply_samples(df)

if "ann" not in st.session_state:
    st.session_state.ann = load_latest_annotations(username)
if "saved_labels" not in st.session_state:
    st.session_state.saved_labels = {
        clip_id: item.get("label", "") for clip_id, item in st.session_state.ann.items()
    }
if "saved_notes" not in st.session_state:
    st.session_state.saved_notes = {
        clip_id: item.get("notes", "") for clip_id, item in st.session_state.ann.items()
    }
if "saved_mixed_orders" not in st.session_state:
    st.session_state.saved_mixed_orders = {
        clip_id: item.get("mixed_order", "") for clip_id, item in st.session_state.ann.items()
    }
if "idx" not in st.session_state:
    st.session_state.idx = 0
if "flash_message" not in st.session_state:
    st.session_state.flash_message = ""
if "autoplay_enabled" not in st.session_state:
    st.session_state.autoplay_enabled = True

st.session_state.idx = clamp_idx(st.session_state.idx, len(df))
idx = st.session_state.idx

row = df.iloc[idx]
unique_clip_id = str(row["unique_clip_id"])
labels = get_labels(config, row["language"])

start_timer_for_clip(unique_clip_id)

current_annotations = st.session_state.ann
total = len(df)

# Progress of the current video only; the totals over all videos are not
# shown.
video_idx = list(df.index[df["dataset_name"] == row["dataset_name"]])
video_pos = video_idx.index(idx) + 1
# A label chosen on this run is in the widget state before it is saved.
done_count = sum(
    1
    for uid in df.loc[video_idx, "unique_clip_id"].astype(str)
    if str(st.session_state.get(
        f"label_{uid}",
        current_annotations.get(uid, {}).get("label", ""))).strip()
)
counts = [f"{int(row['video_clips'])} clips"]
if row["sampled"]:
    counts.append(f"{len(video_idx)} sampled")
counts.append(f"{done_count} done")
link = source_link(config, row["dataset_name"])

st.progress(done_count / len(video_idx))
st.markdown(
    f"<div class='small-muted'>{row['dataset_name']} &nbsp; · &nbsp; "
    + " &nbsp; · &nbsp; ".join(counts)
    + (f" &nbsp; · &nbsp; {link}" if link else "")
    + "</div>",
    unsafe_allow_html=True,
)

if st.session_state.flash_message:
    st.caption(st.session_state.flash_message)
    st.session_state.flash_message = ""

st.markdown(
    f"<h4 style='text-align:center;'>Clip {video_pos}</h4>",
    unsafe_allow_html=True,
)

left, center, right = st.columns([0.25, 4, 0.25])
with center:
    if row["video_exists"]:
        st.video(
            row["video_path"],
            autoplay=st.session_state.autoplay_enabled,
        )
    else:
        st.error(f"Missing video file: {row['video_filename']}")

st.markdown(
    f"<div class='clip-meta'>"
    f"{row['language']} &nbsp; · &nbsp; "
    f"{row['start_ms'] / 1000:.2f}s – {row['end_ms'] / 1000:.2f}s"
    f" &nbsp; · &nbsp; duration {row['duration_ms'] / 1000:.2f}s"
    f"</div>",
    unsafe_allow_html=True,
)

existing = current_annotations.get(unique_clip_id, {"label": "", "notes": "", "mixed_order": "", "saved": False})
options = [""] + labels
radio_key = f"label_{unique_clip_id}"
notes_key = f"notes_{unique_clip_id}"
mixed_key = f"mixed_order_{unique_clip_id}"

if radio_key not in st.session_state:
    st.session_state[radio_key] = existing["label"] if existing.get("label") in options else ""
if notes_key not in st.session_state:
    st.session_state[notes_key] = existing.get("notes", "")
if mixed_key not in st.session_state:
    st.session_state[mixed_key] = existing.get("mixed_order", "")

selected_label = st.radio(
    "Label",
    options=options,
    key=radio_key,
    horizontal=True,
    label_visibility="collapsed",
    format_func=lambda x: "Select..." if x == "" else x,
)

if is_mixed(selected_label):
    st.text_input(
        "Languages in order of appearance",
        key=mixed_key,
        placeholder="e.g., English → Spanish → English",
        label_visibility="collapsed",
    )

notes = st.text_area(
    "Notes",
    key=notes_key,
    height=55,
    placeholder="Optional short note...",
    label_visibility="collapsed",
)

item = st.session_state.ann.get(unique_clip_id, {})
item["label"] = selected_label
item["notes"] = notes
item["mixed_order"] = st.session_state.get(mixed_key, "") if is_mixed(selected_label) else ""
st.session_state.ann[unique_clip_id] = item

maybe_auto_save_label_only(row, selected_label)

nav_col1, nav_col2 = st.columns([1, 1])
with nav_col1:
    if st.button("⬅ Prev", use_container_width=True, disabled=(idx == 0)):
        save_current_clip_before_navigation(row)
        st.session_state.idx = clamp_idx(idx - 1, total)
        st.rerun()

with nav_col2:
    if st.button("Next ➡", use_container_width=True, disabled=(idx == total - 1)):
        save_current_clip_before_navigation(row)
        st.session_state.idx = clamp_idx(idx + 1, total)
        st.rerun()

with st.expander("More options"):
    st.checkbox("Autoplay", key="autoplay_enabled")

    jump_to = st.number_input(
        "Jump to clip # of this video",
        min_value=1,
        max_value=len(video_idx),
        value=video_pos,
        step=1,
    )
    if st.button("Jump"):
        save_current_clip_before_navigation(row)
        st.session_state.idx = int(video_idx[int(jump_to) - 1])
        st.rerun()

    unfinished_indices = [
        i for i, r in df.iterrows()
        if not str(st.session_state.ann.get(str(r["unique_clip_id"]), {}).get("label", "")).strip()
    ]
    if st.button("Go to first unfinished"):
        save_current_clip_before_navigation(row)
        if unfinished_indices:
            st.session_state.idx = clamp_idx(int(unfinished_indices[0]), total)
            st.rerun()
        else:
            st.success("All clips are annotated.")

    prev_note = get_previous_note(df, idx)
    if prev_note:
        st.markdown("Previous note:")
        st.code(prev_note)
        if st.button("Paste previous note"):
            st.session_state[notes_key] = prev_note
            st.rerun()

    st.caption("Playback speed is not included because Streamlit's normal video player does not expose a reliable speed control.")
