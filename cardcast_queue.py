"""Manual, persistent artwork playlist. No modem or transmission behavior here."""

from __future__ import annotations


class CardcastQueue:
    """Ordered saved-card paths. Repeats are allowed for intentional playlists."""

    MAX_CARDS = 40

    def __init__(self, paths=()):
        if isinstance(paths, str):
            paths = [paths] if paths else []
        self.paths = [path for path in paths if isinstance(path, str) and path][
            :self.MAX_CARDS
        ]

    def add(self, path: str) -> bool:
        if not isinstance(path, str) or not path or len(self.paths) >= self.MAX_CARDS:
            return False
        self.paths.append(path)
        return True

    def move(self, index: int, delta: int) -> int:
        destination = index + delta
        if not (0 <= index < len(self.paths) and 0 <= destination < len(self.paths)):
            return index
        self.paths.insert(destination, self.paths.pop(index))
        return destination

    def remove(self, index: int) -> str | None:
        if 0 <= index < len(self.paths):
            return self.paths.pop(index)
        return None

    def prune(self, allowed_paths: set[str]) -> bool:
        remaining = [path for path in self.paths if path in allowed_paths]
        changed = remaining != self.paths
        self.paths = remaining
        return changed
