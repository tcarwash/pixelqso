"""Regenerate the fixed weak-signal QC-LDPC artifact from the attributed NR table."""
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "licenses/weak-signal/5G_bg2.csv"
OUTPUT = ROOT / "weak_signal_ldpc_data.py"
Z, K, N, KB, I_LS, MB = 352, 3376, 14080, 10, 5, 33
EXPECTED_SHA256 = "05c6cf540be3769cbb7dc3dd7e68af93a14ebc98275ee54a1a54b0c6f5c3df55"


def load_nr_shifts(path=TABLE):
    base = [[-1] * 52 for _ in range(42)]
    row_index = -1
    with path.open(newline="", encoding="ascii") as stream:
        rows = csv.reader(stream, delimiter=";")
        next(rows)  # CSV column labels.
        next(rows)  # Lifting-set labels.
        for fields in rows:
            if not fields or not fields[1]:
                continue
            if fields[0]:
                row_index = int(fields[0])
            column_index = int(fields[1])
            shift = int(fields[2 + I_LS])
            base[row_index][column_index] = -1 if shift < 0 else shift % Z
    if row_index != 41:
        raise ValueError("BG2 table did not contain all 42 standard rows")
    # Rate matching at N=14080 retains 33 check rows and 43 block columns.
    return [line[:KB + MB] for line in base[:MB]]


def main():
    base = load_nr_shifts()
    canonical = b"".join(int(value).to_bytes(8, "little", signed=True)
                        for row in base for value in row)
    digest = hashlib.sha256(canonical).hexdigest()
    if digest != EXPECTED_SHA256:
        raise ValueError(f"BG2 construction mismatch: {digest}")
    artifact = {
        "format": "pixelqso-fixed-qc-ldpc-v1",
        "k": K, "n": N, "bg": 2, "z": Z, "kb": KB,
        "rows": MB, "columns": KB + MB,
        "lifting_set_index": I_LS,
        "base_sha256_le_i64": digest,
        "table_sha256": hashlib.sha256(TABLE.read_bytes()).hexdigest(),
        "base": base,
    }
    OUTPUT.write_text('"""Generated from the attributed NR BG2 table; see licenses/weak-signal/NOTICE.txt."""\nDATA = '
                      + repr(artifact) + "\n")
    print(digest)


if __name__ == "__main__":
    main()
