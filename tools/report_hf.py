"""Render the HF benchmark JSON as a Markdown table."""
import argparse
import json
from pathlib import Path

def render(data):
    args = data['arguments']
    rows = data['trials']
    profiles = args['profiles']
    lines = ['# Experimental modem HF benchmark', '',
             f"SNR in 2500 Hz: {args['snr_db']}; {args['trials']} cards per channel; "
             f"{args['repeats']} transmission(s), TX RMS 0.1, CFO {args['cfo_hz']} Hz, "
             f"drift {args.get('drift_hz_per_second',0)} Hz/s, clock {args['clock_ppm']} ppm.", '',
             f"Card pattern: {args.get('card_pattern','random')}.", '',
             f"Common framing: {'native payload only' if args.get('native_payload',False) else 'backend default'}.", '',
             'Cells count receiver-verified, exact identity and pixel recovery. Raw mode exact matches are shown separately.', '',
             '| Mode | TX seconds | SNR dB | ' + ' | '.join(profiles) + ' | Mean RX wall seconds |',
             '|---|---:|---:|' + '---:|' * len(profiles) + '---:|']
    for mode in args['modes']:
        for snr in args['snr_db']:
            selected = [r for r in rows if r['mode'] == mode and r['snr_db_2500'] == snr]
            cells = []
            for profile in profiles:
                group = [r for r in selected if r['profile'] == profile]
                count = sum(r['verified'] for r in group)
                cell = f'{count}/{len(group)}'
                if mode == 'fast_avatar':
                    cell += f" (exact {sum(r['exact_match'] for r in group)}/{len(group)})"
                cells.append(cell)
            airtime = selected[0]['airtime_seconds']
            cpu = sum(r['decode_seconds'] for r in selected) / len(selected)
            lines.append(f'| `{mode}` | {airtime:.2f} | {snr:g} | ' + ' | '.join(cells) + f' | {cpu:.2f} |')
    controls = [r for r in rows if r['profile'] == 'noise_only']
    errors = [r for r in rows if r.get('failure_kind') == 'backend_error']
    lines += ['', f"Noise-only controls: {len(controls)}; false verified cards across all attempts: "
              f"{sum(r['false_verified'] for r in rows)}; backend errors: {len(errors)}.", '',
              'Illustrative synthetic Gaussian-scatter channels; small samples, no RF validation. '
              'Equal RMS and copy count do not mean equal airtime or transmit energy. '
              'CPU timing is wall time and depends on machine load.', '']
    return '\n'.join(lines)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('results', type=Path)
    p.add_argument('--output', type=Path)
    a = p.parse_args()
    result = render(json.loads(a.results.read_text()))
    if a.output:
        a.output.write_text(result)
    else:
        print(result)

if __name__ == '__main__':
    main()
