# Spoken Language Annotation Tool

A web tool for labeling the language spoken in short video clips. It has two parts:

* `data_io/preprocess.py` cuts a video into short speech clips, using the silences in its audio.
* `app/main.py` is a [Streamlit](https://streamlit.io) app. Annotators sign in, watch the clips of their languages one at a time, and label each clip with a language, "Mixed" or "Other". For a mixed clip they can give the order of the languages, and any clip can have a note. Every save is stored in an SQLite database.

The other scripts in `data_io/`, for sampling clips and exporting annotations, are described in [`data_io/README.md`](data_io/README.md).

## Requirements

* Python 3.10 or later, with the packages in `requirements.txt`
* `ffmpeg` and `ffprobe` on the `PATH`, for `preprocess.py`

For example, with conda:

```bash
conda create -n lid-annotation python=3.11
conda activate lid-annotation
pip install -r requirements.txt
```

## Configuration

The app needs a config file, and does not start without one. Copy [`app/app_config.example.json`](app/app_config.example.json) to `app/app_config.json` and edit it:

* `app_title` - the title on the sign-in page.
* `db_path` - the SQLite database file. The app makes it when it does not exist.
* `data_basepath` - the directory of the clips.
* `languages` - one entry per language, with the labels of its clips (`labels`) and the directory of its clips under `data_basepath` (`data_subpath`).
* `users` - one entry per user name, with a password hash (`password_sha256`) and the languages of the clips that the user annotates (`languages`).
* `source_url` (optional) - a link to the source of each video, with `{guid}` for the video stem without suffixes such as `.mp4`, or `{stem}` for the stem; for example `https://americanarchive.org/catalog/{guid}`.

Relative paths in `db_path` and `data_basepath` are relative to the directory of the config file. Git does not track `app/app_config.json` or the database.

A user sees the clips of each of the user's languages, video by video, and each clip has the labels of its own language. Of a video with a sample (see [`data_io/README.md`](data_io/README.md#sampling)), only the sampled clips are shown. Above each clip, the app shows the progress of its video: the number of clips, the number of sampled clips (for a sampled video), and the number of clips done. The label `Mixed` opens a field for the order of the languages.

To make a password hash:

```bash
python -c "import hashlib, getpass; print(hashlib.sha256(getpass.getpass().encode()).hexdigest())"
```

## Making clips

Run `preprocess.py` once for each video, with the clip directory of the video's language as the output directory: `<data_basepath>/<data_subpath>` from the config.

```bash
python data_io/preprocess.py path/to/video.mp4 -o <data_basepath>/<data_subpath>
```

For a video with the stem `<stem>` (its file name without the last extension), the clip directory then has:

```
<stem>_<nnnn>_<start_ms>_<end_ms>.mp4
<stem>_<nnnn>_<start_ms>_<end_ms>.wav
<stem>.manifest.csv
<stem>.silencedetect.log
```

* The app finds the clips through the manifests: a clip that is not in a manifest is not shown.
* The script refuses to run when the video is already segmented in that directory.
* Do not rename, move or segment again the clips of a video after annotation starts. The labels in the database refer to the clips by their names.

## Running the app

```bash
streamlit run app/main.py --server.port 8501  # streamlit default port is 8501
```

The app reads `app/app_config.json`. To use another config file, set `APP_CONFIG_PATH` to its path.

To keep the app running after the terminal closes or the machine restarts, run it in `tmux` or as a systemd service. Streamlit serves plain HTTP. To give annotators access over a network, put the app behind a reverse proxy with TLS.

## Annotation data

The database table `annotation_events` has one row for each save. A row stores the user, the clip, the label, the language order, the note, and the viewing time and count of the clip. The current label of a user for a clip is the row with the highest `id`.

## License

[Apache License 2.0](LICENSE)
