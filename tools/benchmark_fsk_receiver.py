"""Run the HF benchmark with the current receiver or an isolated source snapshot.

Pass --receiver-source before the ordinary benchmark_hf arguments to replay the
three FSK/header modules saved before a receiver change. TX and channel settings
remain the same, making the resulting JSON files directly comparable.
"""
import argparse
import importlib.util
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__,add_help=False)
    parser.add_argument('--receiver-source',type=Path)
    args,remaining = parser.parse_known_args()
    if args.receiver_source is not None:
        for name in ('experimental_burst_modem','compressed_fsk_modem','modem_envelope'):
            path = args.receiver_source/f'{name}.py'
            if not path.is_file():
                parser.error(f'missing receiver source: {path}')
            spec = importlib.util.spec_from_file_location(name,path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
    sys.argv = [sys.argv[0],*remaining]
    from tools.benchmark_hf import main as benchmark
    return benchmark()


if __name__ == '__main__':
    raise SystemExit(main())
