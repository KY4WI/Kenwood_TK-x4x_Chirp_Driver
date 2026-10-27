# Copyright (c) 2026 Dave, KY4WI
# SPDX-License-Identifier: GPL-3.0-or-later

"""Checks for the checked-in, synthetic CHIRP codeplug test fixtures.

These tests need no CHIRP installation and do not access a radio. CI also
runs CHIRP's own driver tests against these images (see ci.yml); neither
replaces bench validation.

Each fixture is a CHIRP-saved image: the 6144-byte codeplug followed by
CHIRP's metadata trailer (MAGIC, then base64-encoded JSON naming the model).
"""
import base64
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ROOT / "test-images"
MODELS = ("840", "940", "941")
SIZES = ("feature", "dense", "upstream")
CODEPLUG_SIZE = 0x1800
CHIRP_MAGIC = b"\x00\xffchirp\xeeimg\x00\x01"


def image_path(kind: str, model: str) -> Path:
    suffix = "_dense" if kind == "dense" else ""
    return IMAGES / kind / f"Kenwood_TK-{model}{suffix}.img"


def split_image(kind: str, model: str):
    """Return (codeplug bytes, metadata dict) for one fixture."""
    data = image_path(kind, model).read_bytes()
    index = data.index(CHIRP_MAGIC)
    meta = json.loads(base64.b64decode(data[index + len(CHIRP_MAGIC):]))
    return data[:index], meta


class TestSyntheticImages(unittest.TestCase):
    def test_codeplugs_are_6144_bytes(self):
        for kind in SIZES:
            for model in MODELS:
                with self.subTest(kind=kind, model=model):
                    image = image_path(kind, model)
                    self.assertTrue(image.is_file(),
                                    f"Missing fixture: {image}")
                    codeplug, _meta = split_image(kind, model)
                    self.assertEqual(len(codeplug), CODEPLUG_SIZE)

    def test_metadata_names_the_model(self):
        for kind in SIZES:
            for model in MODELS:
                with self.subTest(kind=kind, model=model):
                    _codeplug, meta = split_image(kind, model)
                    self.assertEqual(meta["vendor"], "Kenwood")
                    self.assertEqual(meta["model"], f"TK-{model}")
                    self.assertEqual(meta["rclass"], f"TK{model}Radio")

    def test_940_and_941_codeplugs_are_identical(self):
        for kind in SIZES:
            with self.subTest(kind=kind):
                self.assertEqual(split_image(kind, "940")[0],
                                 split_image(kind, "941")[0])

    def test_sets_differ(self):
        for model in MODELS:
            with self.subTest(model=model):
                codeplugs = {kind: split_image(kind, model)[0]
                             for kind in SIZES}
                self.assertEqual(len(set(codeplugs.values())), len(SIZES))

    def test_94x_channels_match_the_stock_851_mhz_raster(self):
        """All 94x synthetic channels must be usable by stock firmware.

        Conventional 94x firmware ignores frequency bit 15, and the stock
        TX path ignores TX words. TX-off records must enable TX Inhibit.
        """
        for kind in SIZES:
            codeplug, _meta = split_image(kind, "940")
            channels = 0
            for system in range(1, 33):
                pointer = int.from_bytes(
                    codeplug[2 * (system - 1):2 * system], "little")
                if pointer & 0x8000:
                    continue
                count = codeplug[0x40 + system]
                start = (0xC000 + (pointer & 0x3FFF)) - 0xE600 + 14
                self.assertGreaterEqual(start, 0)
                self.assertLessEqual(start + count * 19, CODEPLUG_SIZE)
                for index in range(count):
                    off = start + index * 19
                    channels += 1
                    rx = int.from_bytes(codeplug[off + 1:off + 3], "little")
                    tx = int.from_bytes(codeplug[off + 3:off + 5], "little")
                    with self.subTest(kind=kind, system=system, index=index):
                        self.assertNotEqual(rx & 0x7FFF, 0x7FFF)
                        for word in (rx, tx):
                            if (word & 0x7FFF) == 0x7FFF:
                                continue
                            hz = 851_000_000 + (word & 0x7FFF) * 12_500
                            self.assertGreaterEqual(hz, 851_000_000)
                            self.assertLessEqual(hz, 999_987_500)
                        if (tx & 0x7FFF) == 0x7FFF:
                            self.assertFalse(codeplug[off + 9] & 0x04,
                                             "TX-off requires TX Inhibit")
            self.assertEqual(channels, 36 if kind == "feature" else 270)

    def test_upstream_has_system_holes(self):
        # Pointer-table entries with bit 15 set are unused System numbers.
        for model in MODELS:
            with self.subTest(model=model):
                codeplug = split_image("upstream", model)[0]
                unused = [n + 1 for n in range(32)
                          if codeplug[2 * n + 1] & 0x80]
                self.assertEqual(unused, [6, 13, 19, 24, 29])


if __name__ == "__main__":
    unittest.main()
