# Copyright (c) 2026 Dave, KY4WI
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for preserving Talk Around during commercial cleanup.

Exercise the driver's actual masks and methods, extracted without CHIRP.
"""
import ast
import struct
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "tk840.py"
CONSTANTS = {
    "CH_FLAGS_A", "CH_FLAGS_B", "CH_RX_FREQ", "CH_TX_FREQ",
    "CONV_RECORD_SIZE", "FLAGA_OPTIONAL_SIGNALING", "FLAGA_TALK_AROUND",
    "FLAGA_TX_INHIBIT", "FLAGA_CALL", "FLAGA_BUSY_LOCKOUT", "FLAGA_HORN",
    "FLAGA_UNUSED", "FLAGA_COMMERCIAL", "FLAGA_EDITABLE",
    "FLAGB_DATA", "FLAGB_UNUSED", "FLAGB_COMMERCIAL", "FLAGB_EDITABLE",
}
FUNCTIONS = {"_u16le", "_put_u16le", "_protected_record_signature"}
METHODS = {"_apply_commercial_policy", "_record_for_new_memory"}


def load_driver_parts():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in CONSTANTS
                for target in node.targets):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "TK840Radio":
            nodes.extend(
                child for child in node.body
                if isinstance(child, ast.FunctionDef) and
                child.name in METHODS)
    ns = {
        "struct": struct,
        "errors": SimpleNamespace(RadioError=ValueError),
        "_extra_value": None,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]),
                 str(SOURCE), "exec"), ns)
    return ns


class FakeMap:
    def __init__(self, data):
        self.data = bytearray(data)

    def get_packed(self):
        return bytes(self.data)

    def set(self, offset, data):
        self.data[offset:offset + len(data)] = data

    def get(self, offset, size):
        return bytes(self.data[offset:offset + size])


class TestTalkAroundPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_driver_parts()

    def test_cleanup_mask_excludes_ta_but_not_other_commercial_bits(self):
        ns = self.ns
        self.assertFalse(ns["FLAGA_COMMERCIAL"] & ns["FLAGA_TALK_AROUND"])
        self.assertTrue(ns["FLAGA_EDITABLE"] & ns["FLAGA_TALK_AROUND"])
        for bit in ("FLAGA_OPTIONAL_SIGNALING", "FLAGA_CALL",
                    "FLAGA_BUSY_LOCKOUT", "FLAGA_HORN"):
            with self.subTest(bit=bit):
                self.assertTrue(ns["FLAGA_COMMERCIAL"] & ns[bit])
        self.assertFalse(ns["FLAGA_COMMERCIAL"] & ns["FLAGA_TX_INHIBIT"])

    def test_default_cleanup_keeps_ta_state_and_tx_inhibit(self):
        ns = self.ns
        size = ns["CONV_RECORD_SIZE"]
        image = bytearray(b"\xFF" * (size * 2))
        # Channel one has enabled TA and TX Inhibit (both active low).
        image[ns["CH_FLAGS_A"]] = 0x00
        image[ns["CH_FLAGS_B"]] = 0x00
        # Channel two has disabled TA and TX permitted.
        image[size + ns["CH_FLAGS_A"]] = 0xFF
        image[size + ns["CH_FLAGS_B"]] = 0xFF
        cache = [
            {"flags_a": 0x00, "flags_b": 0x00},
            {"flags_a": 0xFF, "flags_b": 0xFF},
        ]
        radio = SimpleNamespace(_mmap=FakeMap(image), _channels=cache)
        ns["_parse_conventional_channels"] = (
            lambda _data: ([{"offset": 0}, {"offset": size}], []))
        ns["_apply_commercial_policy"](radio)

        flags = [radio._mmap.data[pos + ns["CH_FLAGS_A"]]
                 for pos in (0, size)]
        self.assertFalse(flags[0] & ns["FLAGA_TALK_AROUND"])
        self.assertTrue(flags[1] & ns["FLAGA_TALK_AROUND"])
        self.assertFalse(flags[0] & ns["FLAGA_TX_INHIBIT"])
        self.assertTrue(flags[1] & ns["FLAGA_TX_INHIBIT"])
        self.assertEqual(flags[0] & ns["FLAGA_COMMERCIAL"],
                         ns["FLAGA_COMMERCIAL"])
        self.assertEqual(radio._mmap.data[ns["CH_FLAGS_B"]] &
                         ns["FLAGB_COMMERCIAL"], ns["FLAGB_COMMERCIAL"])
        for index in (0, 1):
            self.assertEqual(cache[index]["flags_a"], flags[index])
            self.assertEqual(cache[index]["flags_b"],
                             radio._mmap.data[index * size +
                                              ns["CH_FLAGS_B"]])

    def test_new_channel_defaults_to_ta_off_even_if_template_has_ta(self):
        ns = self.ns
        size = ns["CONV_RECORD_SIZE"]
        template = bytearray(b"\xFF" * size)
        template[ns["CH_FLAGS_A"]] &= ~ns["FLAGA_TALK_AROUND"]
        radio = SimpleNamespace(
            _channels=[{"offset": 0}],
            _mmap=FakeMap(template),
            _frequency_unused_mask=0x8000,
            _extra_value=lambda _mem, _key, default: default,
        )
        memory = SimpleNamespace(number=2)
        record = ns["_record_for_new_memory"](radio, memory)
        self.assertTrue(record[ns["CH_FLAGS_A"]] &
                        ns["FLAGA_TALK_AROUND"])
        self.assertTrue(record[ns["CH_FLAGS_A"]] &
                        ns["FLAGA_COMMERCIAL"])

    def test_copied_channel_preserves_raw_ta_flag(self):
        ns = self.ns
        size = ns["CONV_RECORD_SIZE"]
        template = bytearray(b"\xFF" * size)
        radio = SimpleNamespace(
            _channels=[],
            _mmap=FakeMap(template),
            _frequency_unused_mask=0x8000,
            _extra_value=lambda _mem, key, default: {
                "tk840_flags": "FD/FF",
            }.get(key, default),
        )
        record = ns["_record_for_new_memory"](
            radio, SimpleNamespace(number=1))
        self.assertFalse(record[ns["CH_FLAGS_A"]] &
                         ns["FLAGA_TALK_AROUND"])

    def test_repack_signature_allows_ta_edits(self):
        ns = self.ns
        size = ns["CONV_RECORD_SIZE"]
        record = bytearray(b"\xFF" * size)
        channel = {"offset": 0}
        before = ns["_protected_record_signature"](record, channel)
        record[ns["CH_FLAGS_A"]] &= ~ns["FLAGA_TALK_AROUND"]
        after = ns["_protected_record_signature"](record, channel)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
