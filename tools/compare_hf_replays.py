"""Compare two benchmark_hf result files by immutable capture digest."""
import argparse
import json
from pathlib import Path
from statistics import median


def keyed(path, paired_cases=False):
    data = json.loads(Path(path).read_text())
    rows = data['trials']
    keys = [((row['case_id'] if paired_cases else row['capture_sha256']),
             row['mode'], row['profile'], row['snr_db_2500'], row['seed'])
            for row in rows]
    if len(keys) != len(set(keys)):
        raise ValueError(f'duplicate captures in {path}')
    return dict(zip(keys, rows))


def compare(before_path, after_path, *, paired_cases=False):
    before, after = keyed(before_path, paired_cases), keyed(after_path, paired_cases)
    if before.keys() != after.keys():
        raise ValueError('capture sets differ; comparison requires identical input bytes')
    groups = {}
    for key in before:
        groups.setdefault((key[1], key[2], key[3]), []).append(key)
    summary = []
    for (mode, profile, snr), keys in sorted(groups.items(), key=lambda x: str(x[0])):
        old = [before[key] for key in keys]
        new = [after[key] for key in keys]
        summary.append(dict(mode=mode, profile=profile, snr_db_2500=snr,
            trials=len(keys), baseline_verified=sum(x['verified'] for x in old),
            candidate_verified=sum(x['verified'] for x in new),
            gained=sum(not x['verified'] and y['verified'] for x,y in zip(old,new)),
            lost=sum(x['verified'] and not y['verified'] for x,y in zip(old,new)),
            baseline_false_verified=sum(x['false_verified'] for x in old),
            candidate_false_verified=sum(x['false_verified'] for x in new),
            baseline_median_seconds=median(x['decode_seconds'] for x in old),
            candidate_median_seconds=median(x['decode_seconds'] for x in new)))
    return dict(captures=len(before), summary=summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline', type=Path)
    parser.add_argument('candidate', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--paired-cases', action='store_true',
                        help='Pair matching card/channel cases when waveforms differ')
    args = parser.parse_args()
    result = compare(args.baseline, args.candidate, paired_cases=args.paired_cases)
    rendered = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end='')


if __name__ == '__main__':
    main()
