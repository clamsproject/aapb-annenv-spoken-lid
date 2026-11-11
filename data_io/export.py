"""
Writes a data drop from the annotation database: one CSV per GUID, with the
latest label of each annotator for each clip.

For a database with clip times in milliseconds (`start_ms`, `end_ms`). The
annotators are written by anonymous IDs, read from a JSON file that maps each
user name of the app to its ID; keep that file out of the repository.

    python3 data_io/export.py DBFILE ANNOTATORS_JSON OUTDIR [--batch BATCH]
        [--annotators ID ...]
"""
import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

COLUMNS = ['annotator', 'clip-set', 'clip', 'start_ms', 'end_ms', 'label',
           'mixed-order', 'notes']
# suffixes that video stems can carry after the GUID
GUID_SUFFIX = re.compile(r'(_trimmed|\.mp4|\.h264)$')


def bare_guid(guid):
    return GUID_SUFFIX.sub('', guid)


def latest_rows(con):
    """The latest row (highest id) of each annotator for each clip, with the
    same rule as the app: per user name, dataset and clip."""
    return con.execute("""
        select username, guid, clip_id, dataset_name, start_ms, end_ms,
               label, mixed_order, notes
        from annotation_events a
        where id = (select max(id) from annotation_events b
                    where b.username = a.username
                      and b.dataset_name = a.dataset_name
                      and b.clip_id = a.clip_id)""")


def export(dbfile, annotators_file, outdir, batch=None, keep=None):
    annotators = json.loads(Path(annotators_file).read_text())
    guids = None
    if batch:
        guids = {line.strip() for line in open(batch)
                 if line.strip() and not line.startswith('#')}
    con = sqlite3.connect(f'file:{dbfile}?mode=ro', uri=True)
    columns = {row[1] for row in con.execute(
        'pragma table_info(annotation_events)')}
    if 'start_ms' not in columns:
        sys.exit(f'{dbfile} has clip times in seconds, not in ms; it is a '
                 f'database of the old version of the app')
    by_guid = defaultdict(list)
    for (user, guid, clip_id, dataset, start_ms, end_ms, label, mixed,
         notes) in latest_rows(con):
        if user not in annotators:
            sys.exit(f'annotator without an ID in {annotators_file}')
        if keep and annotators[user] not in keep:
            continue
        if guids is not None and bare_guid(guid) not in guids:
            continue
        # clip_id is <stem>_<nnnn>_<start_ms>_<end_ms>
        clip = int(clip_id.rsplit('_', 3)[1])
        by_guid[bare_guid(guid)].append({
            'annotator': annotators[user], 'clip-set': dataset or '',
            'clip': clip,
            'start_ms': start_ms, 'end_ms': end_ms,
            'label': label or '', 'mixed-order': mixed or '',
            'notes': notes or ''})
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    for guid, rows in sorted(by_guid.items()):
        rows.sort(key=lambda r: (r['clip-set'], r['start_ms'], r['end_ms'],
                                 r['annotator']))
        with open(outdir / f'{guid}.csv', 'w', newline='') as f:
            writer = csv.DictWriter(f, COLUMNS, lineterminator='\n')
            writer.writeheader()
            writer.writerows(rows)
    print(f'wrote {sum(len(r) for r in by_guid.values())} rows to '
          f'{len(by_guid)} files in {outdir}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('dbfile')
    parser.add_argument('annotators_json')
    parser.add_argument('outdir')
    parser.add_argument('--batch', help='batch file; export only its GUIDs')
    parser.add_argument('--annotators', nargs='+',
                        help='annotator IDs to export (default: all)')
    args = parser.parse_args()
    export(args.dbfile, args.annotators_json, args.outdir, args.batch,
           args.annotators)
