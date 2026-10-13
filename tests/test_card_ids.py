import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from card_ids import allocate_card_id


class CardIdTests(unittest.TestCase):
    def test_multiple_copies_avoid_original_and_other_library_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, card_id in (("cards/original.json", 49374),
                                  ("sent-cards/other.json", 49375)):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"card_id": card_id}))
            with patch("card_ids.secrets.randbelow", return_value=49374):
                first = allocate_card_id(root)
                (root / "cards/copy.json").write_text(json.dumps({"card_id": first}))
                second = allocate_card_id(root)
            self.assertEqual(len({49374, 49375, first, second}), 4)

    def test_legacy_default_id_is_reserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cards/legacy.json"
            path.parent.mkdir()
            path.write_text('{}')
            with patch("card_ids.secrets.randbelow", return_value=0xC0DE):
                self.assertNotEqual(allocate_card_id(folder), 0xC0DE)

    def test_unsaved_ids_and_wraparound(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("card_ids.secrets.randbelow", return_value=65535):
                self.assertEqual(allocate_card_id(folder, [65535, 0]), 1)

    def test_exhaustion_does_not_reuse_an_id(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "All card IDs"):
                allocate_card_id(folder, range(65536))
