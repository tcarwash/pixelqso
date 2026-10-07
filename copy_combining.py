"""Bounded copy policy shared by the experimental waveform receivers.

Evidence is aligned by each modem, not by adding unsynchronized audio.
Candidates remain provisional until the modem's original integrity gate passes.
"""
from dataclasses import dataclass
from contextvars import ContextVar
import numpy as np

MAX_COPIES = 20
ADMITTED_IDENTITY = ContextVar("admitted_copy_identity", default=None)

def repeat_waveform(audio, copies):
    if int(copies) != copies or not 1 <= copies <= MAX_COPIES:
        raise ValueError(f'copy count must be between 1 and {MAX_COPIES}')
    return np.tile(audio, int(copies))

@dataclass
class SoftCopy:
    evidence: np.ndarray
    diagnostics: dict
    identity: tuple | None = None

    def __post_init__(self):
        if self.identity is None:
            self.identity = ADMITTED_IDENTITY.get()


def compatible(left, right):
    """Do not pool conflicting identities or unrelated evidence patterns.

    Unknown identities can only form provisional groups. Correlation is a
    rejection heuristic, not identity authentication; CRC remains mandatory.
    """
    if left.identity is not None and right.identity is not None and left.identity != right.identity:
        return False
    a, b = np.asarray(left.evidence, float), np.asarray(right.evidence, float)
    if a.shape != b.shape or not a.size:
        return False
    if left.identity is not None and left.identity == right.identity:
        return True
    if a.ndim > 1:
        a = a - a.mean(axis=-1, keepdims=True)
        b = b - b.mean(axis=-1, keepdims=True)
    a, b = a.ravel(), b.ravel()
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    return bool(denom > 0 and np.dot(a, b) / denom > max(.015, 3 / np.sqrt(a.size)))


def recovery_attempts(copies):
    """Singles first, then compatible cumulative groups; bounded CPU and memory."""
    copies = list(copies)[:MAX_COPIES]
    for copy in copies:
        yield copy.evidence, [copy]
    groups = []
    for copy in copies:
        group = next((g for g in groups if compatible(g[0], copy)), None)
        if group is None:
            groups.append([copy])
        else:
            group.append(copy)
            yield np.sum([c.evidence for c in group], axis=0), list(group)


def diagnostics_for(used, acquired):
    return {**used[0].diagnostics, 'combined_copies': len(used),
            'acquired_copies': acquired,
            'copy_diagnostics': [c.diagnostics for c in used]}


def hypothesis_attempts(candidates, max_extra=12):
    """Retry alternate receiver hypotheses without counting a copy twice.

    Each inner list describes ONE physical transmission. First retain the
    existing singles/cumulative policy. Extra retries choose at most one
    interpretation from each list, with a fixed CPU budget.
    """
    candidates = [list(values) for values in candidates if values][:MAX_COPIES]
    if not candidates:
        return
    primary = [values[0] for values in candidates]
    yield from recovery_attempts(primary)
    retries = 0
    # Retry each alternative receiver lane as a complete copy combination.
    for rank in range(1,max(map(len,candidates))):
        lane = [values[min(rank,len(values)-1)] for values in candidates]
        for evidence,used in recovery_attempts(lane):
            if len(used) != len(lane):
                continue
            yield evidence,used
            retries += 1
            if retries >= max_extra:
                return
    # A single ambiguous copy can need different timing from the others.
    for values in candidates:
        for alternative in values[1:]:
            selected = [alternative if frame is values[0] else frame for frame in primary]
            for evidence,used in recovery_attempts(selected):
                if len(used) != len(selected):
                    continue
                yield evidence,used
                retries += 1
                if retries >= max_extra:
                    return
