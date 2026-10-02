"""Aggregate the diverse-card campaign and plot measured recovery points."""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path


def interval(successes, count):
    z = 1.96
    p = successes/count
    denominator = 1+z*z/count
    center = (p+z*z/(2*count))/denominator
    radius = z*math.sqrt(p*(1-p)/count+z*z/(4*count*count))/denominator
    return center-radius, center+radius


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    data = json.loads((args.directory/'results.json').read_text())
    groups = defaultdict(list)
    for row in data['trials']:
        groups[row['backend'],row['profile'],row['snr_db']].append(row)
    aggregated = []
    for (backend,profile,snr), rows in sorted(groups.items()):
        count = len(rows)
        good = sum(row['verified_recovery'] for row in rows)
        low, high = interval(good,count)
        aggregated.append(dict(backend=backend,profile=profile,snr_db=snr,trials=count,
                               verified_recoveries=good,recovery_rate=good/count,
                               ci95=[low,high],false_verified=sum(row['false_verified'] for row in rows)))
    (args.directory/'aggregate.json').write_text(json.dumps(aggregated,indent=2)+'\n')
    lines = ['# Diverse-card weak-signal comparison', '',
             '32×32 eight-color cards; one independent burst per attempt. Five fixed cards with independent noise realizations at each level.',
             'SNR references 2500 Hz; signal and noise share the 100–3100 Hz receive FIR.', '',
             '| Backend | SNR dB | Checked recovery | 95% Wilson interval |',
             '| --- | ---: | ---: | ---: |']
    for row in aggregated:
        low, high = row['ci95']
        label = 'Weak signal' if row['backend']=='experimental_qpsk_5s' else 'Production Resilient'
        lines.append(f"| {label} | {row['snr_db']:g} | {row['verified_recoveries']}/{row['trials']} | {low:.1%}–{high:.1%} |")
    errors = defaultdict(int)
    for row in data['trials']:
        if row['error']:
            errors[row['error']] += 1
    lines += ['', f"Total attempts: {len(data['trials'])}. False verified cards: {sum(r['false_verified'] for r in data['trials'])}.", '',
              '## Interpretation', '',
              'The transition is bracketed by measured points; no interpolated threshold is claimed.',
              'The confidence intervals describe the fixed five-card test set and independent synthetic noise realizations.',
              'They do not measure the variation of real HF paths or establish performance on all artwork.', '',
              'New audio is 4.622 s versus 3.090 s for one production burst. At equal average transmit power,',
              'the new attempt uses 49.6% more energy (1.75 dB). The sensitivity difference includes that energy',
              'and the waveform, acquisition and coding changes; it is not an isolated coding-gain measurement.', '',
              'CPU timings were collected while other benchmark processes were running. They are not live latency claims.', '',
              '## Failure counts', '']
    lines += [f'- {error}: {count}' for error,count in sorted(errors.items())]
    lines += ['', '[Detailed report](report.html) · [Recovery curves](recovery-curves.svg) · [Aggregate JSON](aggregate.json)', '',
              'Reproduction: run `tools/weak_signal_campaign.py` into a new directory, then this script with system Python',
              '(Matplotlib required). Source snapshots and individual invocations are preserved in the campaign directory.']
    (args.directory/'RESULTS.md').write_text('\n'.join(lines)+'\n')
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(9,5))
    for backend, label, color in [('experimental_qpsk_5s','Experimental QPSK / LDPC','#007c91'),
                                   ('fast_avatar_fec','Production Resilient','#b65d21')]:
        rows = [r for r in aggregated if r['backend']==backend and r['profile']=='awgn']
        if not rows:
            continue
        ax.errorbar([r['snr_db'] for r in rows], [r['recovery_rate']*100 for r in rows],
                    yerr=[[max(0,100*(r['recovery_rate']-r['ci95'][0])) for r in rows],
                          [max(0,100*(r['ci95'][1]-r['recovery_rate'])) for r in rows]],
                    fmt='o', capsize=4, color=color, label=label)
    ax.axhline(90, linestyle='--', color='gray', linewidth=1)
    ax.axvline(0, color='gray', linewidth=1)
    ax.set(xlabel='SNR in 2500 Hz (dB)', ylabel='Verified full-card recovery (%)',
           title='32×32, one burst, five cards — measured points with 95% intervals', ylim=(-3,104))
    ax.grid(alpha=.2)
    ax.legend(loc='center', bbox_to_anchor=(.52,.65))
    fig.tight_layout()
    fig.savefig(args.directory/'recovery-curves.svg')
    fig.savefig(args.directory/'recovery-curves.png',dpi=160)


if __name__ == '__main__':
    main()
