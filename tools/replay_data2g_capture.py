#!/usr/bin/env python3
"""Replay opt-in Data2G frame sidecars through both assembly paths."""
from __future__ import annotations

import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cardmodem as modem
from card_transfer import CardAssemblyService, CardTransferReceiver


def _load_events(path: Path):
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid event JSON on line {line_number}: {exc}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"Event on line {line_number} must be a JSON object")
            yield event


def _frame_path(folder: Path, event: dict) -> Path:
    capture = event.get("binary_capture")
    if not isinstance(capture, dict) or capture.get("storage") != "local_only":
        raise ValueError("host_rx_frame event has no local binary sidecar")
    name = capture.get("file")
    if not isinstance(name, str) or Path(name).name != name:
        raise ValueError("frame sidecar name must be a local file name")
    return folder / name


def _feed_service(service: CardAssemblyService, frame: bytes):
    _assembly, metadata = service.feed_transport_frame("PIXELQSO", frame)
    metadata = dict(metadata)
    pixels = metadata.pop("pixels")
    transport_result = {**metadata, "pixels": pixels}
    palette_id = int(metadata["palette_id"])
    report = {
        "sample_rate": 0,
        "receive_profile": "Data2G host/KISS replay",
        "valid_packets": [],
        "valid_packet_count": 1,
        "errors": [],
        "card": {**metadata,
                 "palette": [list(color) for color in modem.AVATAR_PALETTES[palette_id]],
                 "avatar_burst": True,
                 "received_copies": 1},
        "pixels": pixels,
    }
    return transport_result, service.merge_preview(report)[0]


def replay(events_path: Path) -> dict:
    events_path = events_path.resolve()
    folder = events_path.parent
    events = _load_events(events_path)
    direct_receiver = CardTransferReceiver()
    service = CardAssemblyService()
    pending_references = {}
    result = {"frames": 0, "accepted": 0, "rejected": 0,
              "reference_comparisons": 0, "reference_mismatches": [],
              "path_mismatches": [], "sidecar_errors": [],
              "verified_cards": [], "unmatched_recorded_cards": 0,
              "unmatched_references": 0,
              "capture_drops": 0, "capture_write_errors": 0,
              "closed_cleanly": False, "client_leave_seen": False,
              "capture_summary_error": None, "event_identity_mismatch": False}
    verified_identities = set()
    capture_session_id = capture_station_id = None

    for event in events:
        if capture_session_id is None:
            capture_session_id = event.get("session_id")
            capture_station_id = event.get("station_id")
        elif (event.get("session_id") != capture_session_id or
              event.get("station_id") != capture_station_id):
            result["event_identity_mismatch"] = True
        if event.get("kind") == "host_rx_frame":
            # A successful frame and its host_rx_card annotation are emitted
            # consecutively from the UI thread. If a recorder drop separated
            # them, record the missing evidence and discard it rather than
            # retaining an unbounded queue for a long capture.
            result["unmatched_references"] += sum(
                len(items) for items in pending_references.values())
            pending_references.clear()
            result["frames"] += 1
            details = event.get("details") or {}
            try:
                sidecar = _frame_path(folder, event)
                frame = sidecar.read_bytes()
                capture = event["binary_capture"]
                digest = hashlib.sha256(frame).hexdigest()
                if (len(frame) != int(capture.get("bytes", -1)) or
                        digest != capture.get("sha256") or
                        digest != details.get("sha256") or
                        len(frame) != int(details.get("bytes", -1))):
                    raise ValueError("frame sidecar size or SHA-256 does not match its event")
            except (OSError, TypeError, ValueError) as exc:
                result["sidecar_errors"].append({"event_id": event.get("event_id"),
                                                 "error": str(exc)})
                continue

            direct = service_transport = None
            direct_error = service_error = None
            try:
                _assembly, direct = direct_receiver.feed("PIXELQSO", frame)
            except Exception as exc:
                direct_error = exc
            try:
                service_transport, preview = _feed_service(service, frame)
            except Exception as exc:
                service_error = exc
            if direct_error or service_error:
                if (direct_error is None or service_error is None or
                        (type(direct_error), str(direct_error)) !=
                        (type(service_error), str(service_error))):
                    result["path_mismatches"].append({
                        "event_id": event.get("event_id"),
                        "direct_error": (f"{type(direct_error).__name__}: {direct_error}"
                                         if direct_error else "accepted"),
                        "service_error": (f"{type(service_error).__name__}: {service_error}"
                                          if service_error else "accepted"),
                    })
                else:
                    result["rejected"] += 1
                continue

            # Compare the transport-only receiver against the transport result
            # before the shared preview and verification layer is applied.
            if direct != service_transport:
                result["path_mismatches"].append({
                    "event_id": event.get("event_id"),
                    "detail": "direct and service transport metadata differ",
                })
            service_card = preview.get("card") or {}
            assembly_event = preview.get("assembly_event") or {}
            if bool(direct.get("exact")) != bool(assembly_event.get("verified_complete")):
                result["path_mismatches"].append({
                    "event_id": event.get("event_id"),
                    "detail": "transport completion and shared verification differ",
                })
            result["accepted"] += 1
            pending_references.setdefault(details["sha256"], deque()).append({
                "event_id": event.get("event_id"),
                "reference": {
                    "callsign": service_card.get("callsign"),
                    "grid": service_card.get("grid"),
                    "card_id": service_card.get("card_id"),
                    "width": service_card.get("width"),
                    "height": service_card.get("height"),
                    "palette_id": service_card.get("palette_id"),
                    "pixel_sha256": hashlib.sha256(bytes(preview.get("pixels") or [])).hexdigest(),
                    "exact": bool(service_card.get("exact")),
                },
            })
            if assembly_event.get("verified_complete"):
                verified = {
                    "callsign": service_card.get("callsign"),
                    "grid": service_card.get("grid"),
                    "card_id": service_card.get("card_id"),
                    "image_id": service_card.get("image_id"),
                }
                identity = tuple(verified.values())
                if identity not in verified_identities:
                    verified_identities.add(identity)
                    result["verified_cards"].append(verified)

        elif event.get("kind") == "host_rx_card":
            details = event.get("details") or {}
            digest = details.get("sha256")
            references = pending_references.get(digest)
            if references:
                replayed = references.popleft()
                if not references:
                    pending_references.pop(digest, None)
                result["reference_comparisons"] += 1
                if replayed["reference"] != details.get("reference"):
                    result["reference_mismatches"].append({
                        "event_id": replayed["event_id"], "sha256": digest,
                        "recorded": details.get("reference"),
                        "replayed": replayed["reference"],
                    })
            else:
                result["unmatched_recorded_cards"] += 1
        elif event.get("kind") == "client_leave":
            result["client_leave_seen"] = True

    summary_path = folder / "capture-summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if (not isinstance(summary, dict) or summary.get("schema") != 1 or
                summary.get("session_id") != capture_session_id or
                summary.get("station_id") != capture_station_id):
            raise ValueError("invalid capture summary")
        result["capture_drops"] = int(summary.get("dropped_jobs", -1))
        result["capture_write_errors"] = int(summary.get("write_errors", -1))
        if result["capture_drops"] < 0 or result["capture_write_errors"] < 0:
            raise ValueError("invalid recorder drop or write-error count")
        result["closed_cleanly"] = bool(
            summary.get("drained") and summary.get("clean") and
            result["capture_drops"] == 0 and result["capture_write_errors"] == 0)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        result["capture_summary_error"] = str(exc)

    result["unmatched_references"] += sum(map(len, pending_references.values()))
    result["inconclusive"] = (result["frames"] == 0 or
                              result["reference_comparisons"] == 0 or
                              result["unmatched_references"] != 0 or
                              result["unmatched_recorded_cards"] != 0 or
                              result["event_identity_mismatch"] or
                              not result["client_leave_seen"] or
                              not result["closed_cleanly"] or
                              result["capture_summary_error"] is not None or
                              result["capture_drops"] != 0 or
                              result["capture_write_errors"] != 0)
    result["ok"] = (not result["inconclusive"] and not any(
        result[key] for key in ("sidecar_errors", "path_mismatches", "reference_mismatches")))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("events", type=Path,
                        help="local events.jsonl produced by opt-in session recording")
    args = parser.parse_args()
    try:
        result = replay(args.events)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
