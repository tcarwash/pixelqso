"""Playlist order, persistence inputs, and failure semantics are modem-independent."""
from cardcast_queue import CardcastQueue


def test_playlist_preserves_order_and_explicit_duplicates():
    queue = CardcastQueue(["a", "b"])
    assert queue.add("a")
    assert queue.paths == ["a", "b", "a"]
    assert queue.move(0, 1) == 1
    assert queue.paths == ["b", "a", "a"]
    assert queue.move(2, 1) == 2
    assert queue.paths == ["b", "a", "a"]
    assert queue.remove(1) == "a"
    assert queue.paths == ["b", "a"]


def test_playlist_prunes_missing_cards_without_advancing_or_sending():
    queue = CardcastQueue(["a", "gone", "a", "b"])
    assert queue.prune({"a", "b"})
    assert queue.paths == ["a", "a", "b"]
    assert not queue.prune({"a", "b"})
    assert queue.remove(-1) is None
    assert queue.remove(33) is None


def test_playlist_limits_bad_persisted_values_and_size():
    assert CardcastQueue("a").paths == ["a"]
    assert CardcastQueue(["a", "", None, 4, "b"]).paths == ["a", "b"]
    queue = CardcastQueue(["a"] * CardcastQueue.MAX_CARDS)
    assert not queue.add("b")
    assert queue.paths[-1] == "a"
