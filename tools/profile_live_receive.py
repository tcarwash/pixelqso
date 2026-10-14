"""Profile the live receive worker against a mono, 16-bit PCM WAV capture.

The capture is replayed as growing snapshots using the live receive-all cadence.
CopyCaptureCache is retained across snapshots, matching the incremental header
scan and copy assembly behavior of the running receiver.
"""
import argparse
import cProfile
import io
import json
import pstats
from pathlib import Path
import statistics
import sys
import time
import wave

import numpy as np


def read_capture(path):
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError("expected a mono, 16-bit PCM WAV capture")
        rate = source.getframerate()
        raw = source.readframes(source.getnframes())
    if not rate:
        raise ValueError("capture has an invalid sample rate")
    return raw, rate


def snapshot_positions(total_samples, rate, first_seconds, interval_seconds):
    first = max(1, round(first_seconds * rate))
    interval = max(1, round(interval_seconds * rate))
    position = first
    while position <= total_samples:
        yield position
        position += interval
    if total_samples >= first and (total_samples - first) % interval:
        yield total_samples


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path, help="mono 16-bit PCM WAV")
    parser.add_argument("--mode", default="auto",
                        help="receive mode; use auto to profile receive-all (default: auto)")
    parser.add_argument("--selected-mode", default="experimental_16fsk_grid",
                        help="preferred modem when mode=auto")
    parser.add_argument("--include-experimental", action="store_true",
                        help="include experimental modes in receive-all candidate fallback")
    parser.add_argument("--first-snapshot-seconds", type=float, default=1.0,
                        help="first live-style decode time (default: 1 second)")
    parser.add_argument("--interval-seconds", type=float, default=5.0,
                        help="receive-all interval between snapshots (default: 5 seconds)")
    parser.add_argument("--window-seconds", type=float, default=360.0,
                        help="match the live receiver's rolling audio window (default: 360)")
    parser.add_argument("--output", type=Path,
                        default=Path("work/rx-latency/live-profile.json"),
                        help="JSON summary path; a sibling .prof file is also written")
    args = parser.parse_args()

    if args.first_snapshot_seconds <= 0 or args.interval_seconds <= 0 or args.window_seconds <= 0:
        parser.error("snapshot and window durations must be positive")
    app_root = Path(__file__).resolve().parents[1]
    if not (app_root / "app.py").is_file():
        parser.error(f"receiver app.py was not found under {app_root}")
    sys.path.insert(0, str(app_root))

    # Resolve imports from this checkout so the profile matches its source.
    import app as receiver_app
    import modem_envelope

    raw, rate = read_capture(args.capture)
    total_samples = len(raw) // 2
    audio_seconds = total_samples / rate
    from PySide6.QtCore import QCoreApplication
    qt_app = QCoreApplication.instance() or QCoreApplication([])
    del qt_app

    from modem_envelope import CopyCaptureCache
    copy_cache = CopyCaptureCache()
    timings = {"pcm_conversion_seconds": 0.0, "header_scan_seconds": 0.0,
               "header_scan_calls": 0}
    original_pcm_convert = receiver_app.pcm16_to_float32
    original_find_copies = modem_envelope.find_copies

    def timed_pcm_convert(samples):
        started = time.perf_counter()
        try:
            return original_pcm_convert(samples)
        finally:
            timings["pcm_conversion_seconds"] += time.perf_counter() - started

    def timed_find_copies(*call_args, **call_kwargs):
        started = time.perf_counter()
        try:
            return original_find_copies(*call_args, **call_kwargs)
        finally:
            timings["header_scan_seconds"] += time.perf_counter() - started
            timings["header_scan_calls"] += 1

    receiver_app.pcm16_to_float32 = timed_pcm_convert
    modem_envelope.find_copies = timed_find_copies

    snapshots = []
    profile = cProfile.Profile()
    total_started = time.perf_counter()
    def replay():
        for index, end_sample in enumerate(snapshot_positions(
                total_samples, rate, args.first_snapshot_seconds,
                args.interval_seconds), start=1):
            window_samples = round(args.window_seconds * rate)
            start_sample = max(0, end_sample - window_samples)
            byte_start, byte_end = start_sample * 2, end_sample * 2
            snapshot_started = time.perf_counter()
            samples = bytes(raw[byte_start:byte_end])
            snapshot_copy_seconds = time.perf_counter() - snapshot_started

            finished = []
            previews = []
            attempts = []
            worker = receiver_app.LiveDecodeWorker(
                samples, rate, "auto", (0, 1, index),
                mode=args.mode,
                preferred_mode=args.selected_mode,
                include_experimental=args.include_experimental,
                copy_cache=copy_cache,
                capture_start_sample=start_sample,
                receive_session_id=1)
            worker.signals.finished.connect(finished.append)
            worker.signals.preview.connect(lambda result: previews.append(result))
            worker.signals.progress.connect(lambda result: attempts.append(result[1]))
            worker.run()

            result = finished[-1] if finished else None
            report = result[1] if result else None
            error = result[3] if result else "worker emitted no finished result"
            worker_stats = result[4] if result and len(result) > 4 else {}
            snapshots.append({
                "snapshot_index": index,
                "capture_end_seconds": round(end_sample / rate, 6),
                "buffer_seconds": round((end_sample - start_sample) / rate, 6),
                "snapshot_copy_seconds": snapshot_copy_seconds,
                "decode_seconds": worker_stats.get("decode_seconds"),
                "audio_seconds": worker_stats.get("audio_seconds"),
                "decoder_attempts": attempts,
                "preview_updates": len(previews),
                "verified": bool(report and report.get("card", {}).get("exact")),
                "receive_activity": bool(report and report.get("receive_activity")),
                "result_mode": (report or {}).get("receive_profile"),
                "error": str(error) if error else None,
            })

    try:
        profile.runcall(replay)
    finally:
        receiver_app.pcm16_to_float32 = original_pcm_convert
        modem_envelope.find_copies = original_find_copies

    elapsed = time.perf_counter() - total_started
    profile_path = args.output.with_suffix(".prof")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    profile.dump_stats(str(profile_path))
    stats = pstats.Stats(profile).strip_dirs().sort_stats("cumtime")
    stats.stream = io.StringIO()
    stats.print_stats(60)

    decode_times = [row["decode_seconds"] for row in snapshots
                    if row["decode_seconds"] is not None]
    summary = {
        "capture": str(args.capture.resolve()),
        "app_root": str(app_root),
        "receiver_app_module": receiver_app.__file__,
        "sample_rate": rate,
        "audio_seconds": audio_seconds,
        "mode": args.mode,
        "selected_mode": args.selected_mode,
        "include_experimental": args.include_experimental,
        "first_snapshot_seconds": args.first_snapshot_seconds,
        "interval_seconds": args.interval_seconds,
        "window_seconds": args.window_seconds,
        "wall_seconds": elapsed,
        "snapshot_count": len(snapshots),
        "verified_snapshot_count": sum(row["verified"] for row in snapshots),
        "decode_seconds_median": statistics.median(decode_times) if decode_times else None,
        "decode_seconds_p95": (float(np.percentile(decode_times, 95))
                                if decode_times else None),
        "timings": timings,
        "snapshots": snapshots,
        "top_functions": stats.stream.getvalue(),
        "profile": str(profile_path),
    }
    args.output.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
