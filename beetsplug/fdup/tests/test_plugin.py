"""Tests for the FDupPlugin duplicate matching logic using fake items."""
import uuid
from typing import Any, Iterable, List, Tuple

from beetsplug.fdup.fdup import FDupPlugin


class FakeItem:
    """A very small stand-in for beets.library.Item used by the plugin.

    It only implements attribute storage and an `update` method that the
    plugin relies on when writing fingerprints back to items.
    """

    def __init__(self, artist: str, title: str, path: str = ""):
        self.id = str(uuid.uuid4())
        self.artist = artist
        self.title = title
        self.path = path or f"{artist} - {title}"

    def update(self, values: dict[str, Any]):
        for k, v in values.items():
            setattr(self, k, v)

    def __repr__(self):
        return f"FakeItem(artist={self.artist!r}, title={self.title!r})"


def collect_pairs(gen: Iterable[Tuple[FakeItem, List[FakeItem]]]) -> set[tuple[str, str, str, str]]:
    pairs = set()
    for i1, matches in gen:
        for i2 in matches:
            pairs.add((i1.artist, i1.title, i2.artist, i2.title))
    return pairs


class TestFDupPlugin:

    # def wip_test(self):
    #
    #     plugin = FDupPlugin()
    #     plugin._fetch_minimal_items()

    def test_detects_duplicates_within_artist(self):
        # Arrange: two clearly duplicate titles under the same artist, plus a distinct one
        items: List[FakeItem] = [
            FakeItem("The Example", "Hello World"),
            FakeItem("The Example", "Hello  World"),  # extra space only
            FakeItem("The Example", "Different Song"),
        ]

        plugin = FDupPlugin()

        # Act
        plugin._calc_fingerprints(items)  # type: ignore[arg-type]
        ids = list(map(lambda x: x.id, items))
        pairs = collect_pairs(plugin._find_matches(ids, items))  # type: ignore[arg-type]
        print(pairs)

        # Assert: the two similar "Hello World" tracks should be flagged
        assert ("The Example", "Hello World", "The Example", "Hello  World") in pairs
        assert ("The Example", "Hello  World", "The Example", "Hello World") in pairs

        # Assert: there should be no other matches
        assert len(pairs) == 2

    def test_detects_duplicates_across_artist(self):
        # Arrange: two clearly duplicate titles under the same artist, plus a distinct one
        items: List[FakeItem] = [
            FakeItem("The Example", "Hello World"),
            FakeItem("The  Example", "Hello  World"),  # extra spaces only
            FakeItem("Different Artist", "Hello World"),
            FakeItem("Different Artist", "Different Track"),
        ]

        plugin = FDupPlugin()

        # Act
        plugin._calc_fingerprints(items)  # type: ignore[arg-type]
        ids = list(map(lambda x: x.id, items))
        pairs = collect_pairs(plugin._find_matches(ids, items))  # type: ignore[arg-type]
        print(pairs)

        # Assert: the two similar "Hello World" tracks should be flagged
        assert ("The Example", "Hello World", "The  Example", "Hello  World") in pairs
        assert ("The  Example", "Hello  World", "The Example", "Hello World") in pairs

        # Assert: there should be no other matches
        assert len(pairs) == 2
