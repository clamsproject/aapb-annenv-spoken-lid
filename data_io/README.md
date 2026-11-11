# Data scripts

Scripts that prepare the clips for the annotation app and export the annotations. Each script prints its usage and options with `--help`.

| Script | Purpose |
|---|---|
| `preprocess.py` | Cuts one video into speech clips, in the clip directory of its language (see [Making clips](../README.md#making-clips)). |
| `sample_naive.py`, `sample_stratified.py`, `sample_convention.py` | Choose a sample of the clips of videos, and store it in the annotation database. |
| `export.py` | Writes the latest annotations of each annotator as a data drop, one CSV file per GUID. |

## Sampling

A sample is a set of clips of one video, chosen for annotation in place of all its clips. A sampling script takes the sample size `-n` and one or more videos, and stores one sample per video:

```bash
python data_io/sample_stratified.py -n 80 <GUID> [<GUID> ...]
```

A video is given by its stem or by its GUID, and must have a manifest in one of the clip directories of the app config. The scripts read the clip directories and the database from the app config (`--config`, by default `app/app_config.json` or `APP_CONFIG_PATH`).

The strategies:

* `naive` - `n` clips at random.
* `stratified` - the clips, in time order, are divided into strata with equal numbers of clips, and the same number of clips is chosen at random from each stratum.
* `convention` - all clips of the beginning and of the end of the video, and `n` clips of the rest at random. The duration of the video comes from its `<stem>.silencedetect.log`.

Each video gets its own random generator from `--seed`, so a sample does not depend on the other videos of the command. The same manifest, strategy, options and seed give the same sample.

A script stops before it stores anything when a video is unknown, matches more than one manifest, or already has a sample. A video has at most one sample; to sample it again, delete its rows first.

### `clip_samples` table

The sampling scripts add this table to the annotation database, with one row per sampled clip:

* `dataset_name` - the video stem, as in `annotation_events`
* `clip_id` - the sampled clip
* `strategy` - `naive`, `stratified` or `convention`
* `params` - the options of the sample, as JSON (for example `{"n": 80, "seed": 42, "strata": 5}`)
* `stratum` - the stratum or part of the video that the clip was chosen from, or empty
* `created_at` - when the sample was stored

The app shows only the sampled clips of a video with a sample, and all clips of a video without one. It stops with an error when a sample names a clip that is not in the manifest of its video, for example after the video was segmented again.

## Internal notes

* Batch `a` was packaged and pushed to the [aapb-annotations](https://github.com/clamsproject/aapb-annotations) repository as the data drop `261109-mellon-slid-a`. All 55 GUIDs of that batch are annotated in all their clips, which were cut by `preprocess.py`.
* The annotation database was reset after that data drop.
* We are experimenting with sampling strategies that can speed up annotation.
