# Copyright (c) 2026 Dave, KY4WI
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression checks for the stock TK-940/941 frequency-word encoding.

Uses AST extraction of the actual driver helpers, so these tests run without
installing CHIRP. CHIRP's full driver suite is exercised separately by CI.
"""
import ast
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "tk840.py"
FUNCTIONS = {
    "_decode_frequency", "_encode_frequency",
    "_decode_frequency_941", "_encode_frequency_941",
}
CONSTANTS = {
    "FREQ_BASE_HZ", "FREQ_BASE_94X_HZ", "FREQ_STEP_HZ", "FREQ_UNUSED_MASK",
}


def load_frequency_helpers():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in CONSTANTS
                for target in node.targets):
            selected.append(node)
    namespace = {
        "errors": SimpleNamespace(InvalidValueError=ValueError),
        "LOG": logging.getLogger(__name__),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]),
                 str(SOURCE), "exec"), namespace)
    return (namespace["_decode_frequency_941"],
            namespace["_encode_frequency_941"],
            namespace["_decode_frequency"],
            namespace["_encode_frequency"])


class Test940941FrequencyWords(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        decode, encode, decode840, encode840 = load_frequency_helpers()
        cls.decode = staticmethod(decode)
        cls.encode = staticmethod(encode)
        cls.decode840 = staticmethod(decode840)
        cls.encode840 = staticmethod(encode840)

    def test_high_bit_ignored_by_stock_firmware(self):
        self.assertEqual(self.decode(0x0F50), 900_000_000)
        self.assertEqual(self.decode(0x8F50), 900_000_000)
        self.assertEqual(self.decode(0x0000), 851_000_000)
        self.assertEqual(self.decode(0x8000), 851_000_000)

    def test_disabled_frequency_both_high_bit_values(self):
        self.assertIsNone(self.decode(0x7FFF))
        self.assertIsNone(self.decode(0xFFFF))

    def test_900_mhz_encoding_preserves_high_bit(self):
        self.assertEqual(self.encode(900_000_000, 0x0000), 0x0F50)
        self.assertEqual(self.encode(900_000_000, 0x8000), 0x8F50)

    def test_disabled_encoding_is_canonical_7fff(self):
        self.assertEqual(self.encode(None, 0x0000), 0x7FFF)
        self.assertEqual(self.encode(None, 0x8000), 0x7FFF)
        self.assertEqual(self.encode(None, 0xFFFF), 0x7FFF)
        self.assertEqual(self.encode840(None, 0x0000), 0x7FFF)
        self.assertEqual(self.encode840(None, 0x8000), 0x7FFF)
        self.assertIsNone(self.decode840(0x7FFF))
        self.assertIsNone(self.decode840(0xFFFF))

    def test_rejects_below_851_mhz(self):
        with self.assertRaises(ValueError):
            self.encode(840_000_000, 0x8000)
        with self.assertRaises(ValueError):
            self.encode(850_987_500, 0x8000)

    def test_max_code_reserved_for_disabled(self):
        self.assertEqual(self.decode(0x7FFE), 1_260_575_000)
        self.assertEqual(self.encode(1_260_575_000, 0), 0x7FFE)
        with self.assertRaises(ValueError):
            self.encode(1_260_587_500, 0)

    def test_rounding_stays_on_12_5_khz_raster(self):
        self.assertEqual(self.encode(900_005_000, 0x8000), 0x8F50)
        self.assertEqual(self.encode(900_007_500, 0x8000), 0x8F51)

    def test_roundtrip_all_valid_codes(self):
        for code in range(0x7FFF):
            with self.subTest(code=code) if code % 4096 == 0 else _quiet():
                freq = self.decode(code)
                self.assertEqual(self.encode(freq, code), code)
                self.assertEqual(self.decode(code | 0x8000), freq)


class _quiet:
    """No-op context manager to avoid thousands of subtest objects."""

    def __enter__(self):
        return None

    def __exit__(self, exc_type, exc, tb):
        return False


if __name__ == "__main__":
    unittest.main()
