"""UTC start scheduling for the experimental compressed 16-FSK modem.

No network requests or system clock changes. The OS owns synchronization.
"""
import math
import shutil
import subprocess

GRID_SECONDS = .5


def near_grid(utc, window=.1):
    if not math.isfinite(utc) or not 0 <= window <= GRID_SECONDS/2:
        raise ValueError('invalid grid timing hint')
    phase = utc % GRID_SECONDS
    return min(phase, GRID_SECONDS-phase) <= window


def plan_start(utc, sample_rate=48000, output_latency=0., lead=.04):
    """Return target UTC and silence samples; lead allows initial pump setup.

    output_latency is measured device delay, not the sink's buffer capacity.
    Round upward so the scheduled waveform is never early by a whole sample.
    """
    if (not all(math.isfinite(x) for x in (utc, output_latency, lead)) or
            output_latency < 0 or lead < 0 or sample_rate <= 0):
        raise ValueError('invalid grid schedule')
    target = math.ceil((utc + output_latency + lead)/GRID_SECONDS)*GRID_SECONDS
    return target, max(0, math.ceil((target-utc-output_latency)*sample_rate))


def clock_status():
    """Best effort, bounded chrony status; unavailable does not mean unsynced."""
    if not shutil.which('chronyc'):
        return 'UTC clock quality unknown (chrony unavailable)'
    try:
        result = subprocess.run(['chronyc', '-c', 'tracking'], capture_output=True,
                                text=True, timeout=.3, check=True)
        fields = result.stdout.strip().split(',')
        # chronyc CSV: system correction, root delay, root dispersion, leap status.
        error = abs(float(fields[3])) + abs(float(fields[9]))/2 + float(fields[10])
        if fields[12].strip() != 'Normal':
            return 'UTC clock not synchronized; RX uses full acquisition fallback'
        return f'UTC estimated clock error ≤{error*1000:.1f} ms'
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 'UTC clock quality unknown (chrony status unavailable)'
