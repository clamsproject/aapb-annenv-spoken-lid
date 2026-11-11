import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

APP_CONFIG_PATH = Path(os.getenv(
    "APP_CONFIG_PATH", Path(__file__).parent / "app_config.json"))

REQUIRED_COLS = {
    "clip_id",
    "guid",
    "start_sec",
    "end_sec",
    "duration_sec",
    "video_filename",
    "audio_filename",
}


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
            start_sec REAL,
            end_sec REAL,
            duration_sec REAL,
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
    if "dataset_name" not in existing_cols:
        cur.execute("ALTER TABLE annotation_events ADD COLUMN dataset_name TEXT DEFAULT ''")
    if "notes" not in existing_cols:
        cur.execute("ALTER TABLE annotation_events ADD COLUMN notes TEXT DEFAULT ''")
    if "mixed_order" not in existing_cols:
        cur.execute("ALTER TABLE annotation_events ADD COLUMN mixed_order TEXT DEFAULT ''")

    conn.commit()
    conn.close()


def load_manifest(clips_dir: Path) -> pd.DataFrame:
    manifest = clips_dir / "clips_manifest.csv"
    if not manifest.exists():
        st.error(f"Missing manifest: {manifest}")
        st.stop()

    df = pd.read_csv(manifest)
    missing = REQUIRED_COLS - set(df.columns)
    if missing:
        st.error(f"Manifest missing columns: {sorted(missing)}")
        st.stop()

    dataset_name = clips_dir.name
    df["clip_id"] = df["clip_id"].astype(str)
    df["guid"] = df["guid"].astype(str)
    df["dataset_name"] = dataset_name
    df["unique_clip_id"] = dataset_name + "::" + df["clip_id"]
    df["video_path"] = df["video_filename"].apply(lambda x: str(clips_dir / x))
    df["audio_path"] = df["audio_filename"].apply(lambda x: str(clips_dir / x))
    df["video_exists"] = df["video_path"].apply(os.path.exists)
    return df


def get_effective_clips_dirs(config: dict[str, Any], user_cfg: dict[str, Any]) -> list[Path]:
    clips_dirs = user_cfg.get("clips_dirs")
    if clips_dirs:
        return [Path(p) for p in clips_dirs]

    clips_dir = user_cfg.get("clips_dir")
    if isinstance(clips_dir, list):
        return [Path(p) for p in clips_dir]
    if clips_dir:
        return [Path(clips_dir)]

    default_dir = config.get("default", {}).get("clips_dir")
    return [Path(default_dir)] if default_dir else []


def load_user_manifests(config: dict[str, Any], user_cfg: dict[str, Any]) -> pd.DataFrame:
    frames = [load_manifest(clips_dir) for clips_dir in get_effective_clips_dirs(config, user_cfg)]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def apply_assignment_filter(df: pd.DataFrame, user_cfg: dict[str, Any]) -> pd.DataFrame:
    assigned_clip_ids = user_cfg.get("assigned_clip_ids")
    assigned_guids = user_cfg.get("assigned_guids")
    assigned_unique_clip_ids = user_cfg.get("assigned_unique_clip_ids")

    if assigned_clip_ids:
        allowed = {str(x) for x in assigned_clip_ids}
        df = df[df["clip_id"].isin(allowed)].copy()
    if assigned_guids:
        allowed = {str(x) for x in assigned_guids}
        df = df[df["guid"].astype(str).isin(allowed)].copy()
    if assigned_unique_clip_ids:
        allowed = {str(x) for x in assigned_unique_clip_ids}
        df = df[df["unique_clip_id"].isin(allowed)].copy()

    return df.reset_index(drop=True)


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
            username, clip_id, dataset_name, guid, start_sec, end_sec, duration_sec,
            video_filename, audio_filename, video_codec_used, label, notes, mixed_order,
            updated_at, view_count, total_time_sec, last_visit_started_at, is_edit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?)
        """,
        (
            username,
            str(manifest_row["clip_id"]),
            str(manifest_row["dataset_name"]),
            str(manifest_row["guid"]),
            float(manifest_row["start_sec"]),
            float(manifest_row["end_sec"]),
            float(manifest_row["duration_sec"]),
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


def get_effective_labels(config: dict[str, Any], user_cfg: dict[str, Any]) -> list[str]:
    return list(user_cfg.get("labels", config.get("default", {}).get("labels", [])))


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
        mixed_order=current_mixed_order if selected_label == "Mixed" else "",
        view_count=item.get("view_count", 1),
        total_time_sec=item.get("total_time_sec", 0.0),
        last_visit_started_at=item.get("last_visit_started_at"),
        is_edit=is_edit,
    )

    item["label"] = selected_label
    item["notes"] = current_notes
    item["mixed_order"] = current_mixed_order if selected_label == "Mixed" else ""
    item["saved"] = True
    st.session_state.ann[unique_clip_id] = item
    st.session_state.saved_labels[unique_clip_id] = selected_label
    st.session_state.saved_notes[unique_clip_id] = current_notes
    st.session_state.saved_mixed_orders[unique_clip_id] = current_mixed_order if selected_label == "Mixed" else ""
    st.session_state.flash_message = f"Saved: {selected_label}"


def save_current_clip_before_navigation(row: pd.Series) -> None:
    unique_clip_id = str(row["unique_clip_id"])
    label_key = f"label_{unique_clip_id}"
    notes_key = f"notes_{unique_clip_id}"
    mixed_key = f"mixed_order_{unique_clip_id}"

    current_label = st.session_state.get(label_key, "")
    current_notes = st.session_state.get(notes_key, "")
    current_mixed_order = st.session_state.get(mixed_key, "") if current_label == "Mixed" else ""

    if save_annotation_if_needed(row, current_label, current_notes, current_mixed_order):
        st.session_state.flash_message = "Saved"


def get_previous_note(df: pd.DataFrame, idx: int) -> str:
    if idx <= 0:
        return ""
    prev_unique = str(df.iloc[idx - 1]["unique_clip_id"])
    return st.session_state.saved_notes.get(prev_unique, "")


def dataset_name_to_aapb_guid(dataset_name: str) -> str:
    name = str(dataset_name).strip().strip("/")

    # If a full path somehow gets passed, keep only final folder name.
    name = Path(name).name

    # Remove known prefixes repeatedly.
    prefixes = ["smo_raw_", "esu_raw_", "spa_raw_", "clips_"]
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if name.startswith(prefix):
                name = name[len(prefix):]
                changed = True

    # Remove known suffixes repeatedly.
    suffixes = ["_trimmed_vadish", "_vadish", "_clip"]
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                changed = True

    return name


st.set_page_config(page_title="LID Annotation", layout="centered")
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
    st.title(config.get("app_title", "LID Annotation"))

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

labels = get_effective_labels(config, user_cfg)
df = apply_assignment_filter(load_user_manifests(config, user_cfg), user_cfg)

if df.empty:
    st.error("No clips are assigned to this user.")
    st.stop()

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

aapb_guid = dataset_name_to_aapb_guid(row["dataset_name"])
aapb_url = f"https://americanarchive.org/catalog/{aapb_guid}"

start_timer_for_clip(unique_clip_id)

current_annotations = st.session_state.ann
assigned_unique_ids = set(df["unique_clip_id"].astype(str))
complete_count = sum(
    1
    for uid in assigned_unique_ids
    if str(current_annotations.get(uid, {}).get("label", "")).strip()
)
total = len(df)

st.progress(complete_count / total if total else 0.0)
st.markdown(
    f"<div class='small-muted'>Progress {complete_count}/{total} &nbsp; | &nbsp; Clip {idx + 1}/{total}</div>",
    unsafe_allow_html=True,
)

if st.session_state.flash_message:
    st.caption(st.session_state.flash_message)
    st.session_state.flash_message = ""

st.markdown(
    f"<h4 style='text-align:center;'>Clip {idx + 1}</h4>",
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
    f"{row['dataset_name']} &nbsp; · &nbsp; {row['start_sec']:.2f}s – {row['end_sec']:.2f}s"
    f" &nbsp; · &nbsp; duration {row['duration_sec']:.2f}s"
    f"</div>",
    unsafe_allow_html=True,
)

st.markdown(
    f"""
    <div style='text-align:center; font-size:0.85rem; margin-bottom:0.4rem;'>
        AAPB source:
        <a href="{aapb_url}" target="_blank">{aapb_guid}</a>
    </div>
    """,
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

if selected_label == "Mixed":
    st.text_input(
        "Languages in order of appearance",
        key=mixed_key,
        placeholder="e.g., English → Samoan → English",
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
item["mixed_order"] = st.session_state.get(mixed_key, "") if selected_label == "Mixed" else ""
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
        "Jump to clip #",
        min_value=1,
        max_value=total,
        value=idx + 1,
        step=1,
    )
    if st.button("Jump"):
        save_current_clip_before_navigation(row)
        st.session_state.idx = clamp_idx(int(jump_to) - 1, total)
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
