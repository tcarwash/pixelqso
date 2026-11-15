"""Train a small offline FSK soft demapper and export NumPy weights.

Training uses random 16-FSK symbol streams through the benchmark channel. The
runtime receiver only needs NumPy; PyTorch is required for this trainer only.
"""
import argparse
from pathlib import Path
import sys

import numpy as np
import torch
from torch import nn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import experimental_burst_modem as burst
from tools.hf_channel import PROFILES, simulate

KEY = "experimental_16fsk_ldpc"
RADIUS = 3


def features(metrics):
    log_energy = np.log(np.maximum(metrics, 1e-12))
    log_energy -= log_energy.max(axis=1, keepdims=True)
    windows = np.stack([log_energy[i-RADIUS:i+RADIUS+1]
                        for i in range(RADIUS, len(metrics)-RADIUS)])
    return windows.reshape(len(windows), -1).astype(np.float32)


def make_split(count, seed, *, heldout=False):
    rng = np.random.default_rng(seed)
    fmt = burst.FORMATS[KEY]
    xs, ys = [], []
    profiles = ("awgn", "hf_mild", "hf_moderate", "hf_severe")
    for i in range(count):
        symbols = rng.integers(0, fmt.tones, size=192)
        waveform = .75 * burst._complex_wave(symbols, fmt).real
        snr = rng.uniform(-7, 9)
        profile = profiles[int(rng.integers(0, len(profiles)))]
        if heldout:
            # Disjoint seeds and SNR continuum; every profile remains represented.
            profile = profiles[i % len(profiles)]
            snr = -6.5 + (i % 32) * .5
        audio, _ = simulate(waveform, burst.FS, profile=profile, snr_db=snr,
                            seed=int(rng.integers(0, 2**31-1)),
                            cfo_hz=float(rng.uniform(-8, 8)),
                            clock_ppm=float(rng.uniform(-120, 120)))
        source = burst._resample(audio, burst.FS, burst.FS)
        analytic = burst.hilbert(source)
        offset = float(rng.uniform(-.4, .4)) * (burst.FS / fmt.baud)
        # The channel simulator adds 300 ms of leading guard noise.
        metrics = burst._tone_metrics(analytic, burst.FS * .3 + offset, fmt,
                                      burst.FS / fmt.baud,
                                      np.arange(len(symbols)))
        xs.append(features(metrics))
        ys.append(symbols[RADIUS:-RADIUS].astype(np.int64))
    return np.concatenate(xs), np.concatenate(ys)


class Demapper(nn.Module):
    def __init__(self):
        super().__init__()
        self.net = nn.Sequential(nn.Linear((2*RADIUS+1)*16, 64), nn.ReLU(),
                                 nn.Linear(64, 16))

    def forward(self, x):
        return self.net(x)


def accuracy(model, x, y, device):
    model.eval()
    with torch.no_grad():
        pred = model(torch.from_numpy(x).to(device)).argmax(1).cpu().numpy()
    return float(np.mean(pred == y))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--train-bursts", type=int, default=1000)
    p.add_argument("--test-bursts", type=int, default=200)
    p.add_argument("--seed", type=int, default=20261111)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--epochs", type=int, default=12)
    p.add_argument("--batch-size", type=int, default=2048)
    a = p.parse_args()
    torch.manual_seed(a.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    train_x, train_y = make_split(a.train_bursts, a.seed)
    test_x, test_y = make_split(a.test_bursts, a.seed + 1_000_003, heldout=True)
    model = Demapper().to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=8e-4, weight_decay=1e-4)
    for epoch in range(a.epochs):
        model.train()
        order = np.random.default_rng(a.seed + epoch).permutation(len(train_y))
        for start in range(0, len(order), a.batch_size):
            indices = order[start:start+a.batch_size]
            x = torch.from_numpy(train_x[indices]).to(device)
            y = torch.from_numpy(train_y[indices]).to(device)
            optimizer.zero_grad(set_to_none=True)
            loss = nn.functional.cross_entropy(model(x), y)
            loss.backward()
            optimizer.step()
        print(f"epoch {epoch+1}/{a.epochs}: train_symbol_accuracy="
              f"{accuracy(model, train_x, train_y, device):.4f} "
              f"heldout_symbol_accuracy={accuracy(model, test_x, test_y, device):.4f}",
              flush=True)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    state = model.cpu().state_dict()
    np.savez(a.output, w1=state["net.0.weight"].numpy(), b1=state["net.0.bias"].numpy(),
             w2=state["net.2.weight"].numpy(), b2=state["net.2.bias"].numpy(),
             radius=np.asarray(RADIUS), schema=np.asarray(1))
    print(f"saved={a.output} train_symbols={len(train_y)} heldout_symbols={len(test_y)}")


if __name__ == "__main__":
    main()
