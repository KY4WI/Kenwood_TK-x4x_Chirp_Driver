# Copyright (c) 2026 Dave, KY4WI
# SPDX-License-Identifier: GPL-3.0-or-later

"""TX-off editing and upload safety tests without a CHIRP installation.

Compile the actual driver helpers and channel-editor method from their AST.
The harness replaces only CHIRP's setting, tone and name interfaces.
"""
import ast
import logging
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "tk840.py"
HELPERS = {
    "_u16le", "_put_u16le", "_encode_frequency",
    "_encode_frequency_941", "_enforce_receive_only_tx_inhibit",
}
CONSTANTS = {
    "CH_RX_FREQ", "CH_TX_FREQ", "CH_RX_TONE", "CH_TX_TONE",
    "CH_FLAGS_A", "CH_FLAGS_B", "CH_NAME", "FLAGA_TX_INHIBIT",
    "FLAGA_OPTIONAL_SIGNALING", "FLAGA_TALK_AROUND", "FLAGA_CALL",
    "FLAGA_BUSY_LOCKOUT", "FLAGA_HORN", "FLAGB_DATA",
    "FREQ_BASE_HZ", "FREQ_BASE_94X_HZ", "FREQ_STEP_HZ",
    "FREQ_UNUSED_MASK",
}


def load_code():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in HELPERS:
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in CONSTANTS
                for t in node.targets):
            selected.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "TK840Radio":
            selected.extend(
                child for child in node.body
                if isinstance(child, ast.FunctionDef) and
                child.name == "_encode_memory_into_record")
    ns = {
        "struct": struct,
        "LOG": logging.getLogger(__name__),
        "errors": SimpleNamespace(InvalidValueError=ValueError),
        "chirp_common": SimpleNamespace(
            split_tone_encode=lambda memory: (None, None)),
        "_encode_tone": lambda tone: 0xFFFF,
        "_encode_name": lambda name: str(name).upper().encode("ascii")[:8]
        .ljust(8, b"\x00"),
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]),
                 str(SOURCE), "exec"), ns)
    return ns


class ExtraOption:
    def __init__(self, name, value):
        self.name = name
        self.value = value

    def get_name(self):
        return self.name


class TestTxInhibitPolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_code()

    def memory(self, duplex, freq=450_000_000, extra=None):
        return SimpleNamespace(
            freq=freq, offset=0, duplex=duplex, name="TEST", extra=extra)

    def record(self, tx_word, flags=0xFF):
        rec = bytearray(b"\xFF" * 19)
        self.ns["_put_u16le"](rec, self.ns["CH_TX_FREQ"], tx_word)
        rec[self.ns["CH_FLAGS_A"]] = flags
        return rec

    def edit(self, tx_word, flags, duplex, model="840", extra=None):
        ns = self.ns
        radio = SimpleNamespace(
            _display_model="TK-" + model, _system_edit_error=None,
            _encode_frequency_word=ns["_encode_frequency_941"] if
            model != "840" else ns["_encode_frequency"],
        )
        freq = 900_000_000 if model != "840" else 450_000_000
        record = self.record(tx_word, flags)
        updated, system = ns["_encode_memory_into_record"](
            radio, self.memory(duplex, freq, extra), record, 1)
        self.assertEqual(system, 1)
        return updated

    def test_off_stores_7fff_but_does_not_change_inhibit_while_editing(self):
        for model, freq in (("840", 450_000_000), ("940", 900_000_000)):
            with self.subTest(model=model):
                original = self.ns["_encode_frequency_941"](
                    freq, 0x8000) if model != "840" else self.ns[
                        "_encode_frequency"](freq, 0x8000)
                edited = self.edit(original, 0xFF, "off", model)
                self.assertEqual(
                    self.ns["_u16le"](edited, self.ns["CH_TX_FREQ"]),
                    0x7FFF)
                self.assertEqual(
                    edited[self.ns["CH_FLAGS_A"]] &
                    self.ns["FLAGA_TX_INHIBIT"],
                    self.ns["FLAGA_TX_INHIBIT"])

    def test_changing_to_simplex_preserves_manual_inhibit(self):
        for model in ("840", "940"):
            with self.subTest(model=model):
                edited = self.edit(0x7FFF, 0xFB, "", model)
                self.assertFalse(
                    edited[self.ns["CH_FLAGS_A"]] &
                    self.ns["FLAGA_TX_INHIBIT"])
                self.assertNotEqual(
                    self.ns["_u16le"](edited, self.ns["CH_TX_FREQ"]) &
                    0x7FFF, 0x7FFF)

    def test_explicit_extra_setting_controls_inhibit_in_editor(self):
        yes = [ExtraOption("tk840_tx_inhibit", True)]
        no = [ExtraOption("tk840_tx_inhibit", False)]
        for duplex in ("off", ""):
            inhibited = self.edit(0xFFFF, 0xFF, duplex, extra=yes)
            self.assertFalse(
                inhibited[self.ns["CH_FLAGS_A"]] &
                self.ns["FLAGA_TX_INHIBIT"])
            permitted = self.edit(0xFFFF, 0xFB, duplex, extra=no)
            self.assertTrue(
                permitted[self.ns["CH_FLAGS_A"]] &
                self.ns["FLAGA_TX_INHIBIT"])

    def test_upload_forces_inhibit_only_for_absent_tx(self):
        ns = self.ns
        for word in (0x7FFF, 0xFFFF):
            with self.subTest(word=word):
                image = self.record(word, 0xFF)
                count = ns["_enforce_receive_only_tx_inhibit"](
                    image, [{"offset": 0}])
                self.assertEqual(count, 1)
                self.assertEqual(
                    ns["_u16le"](image, ns["CH_TX_FREQ"]), 0x7FFF)
                self.assertFalse(image[ns["CH_FLAGS_A"]] &
                                 ns["FLAGA_TX_INHIBIT"])
                # Safe upload is idempotent.
                self.assertEqual(
                    ns["_enforce_receive_only_tx_inhibit"](
                        image, [{"offset": 0}]), 0)

    def test_upload_never_clears_inhibit_on_valid_tx(self):
        ns = self.ns
        for flags in (0xFB, 0xFF):
            with self.subTest(flags=flags):
                image = self.record(0x8F50, flags)
                before = bytes(image)
                self.assertEqual(
                    ns["_enforce_receive_only_tx_inhibit"](
                        image, [{"offset": 0}]), 0)
                self.assertEqual(bytes(image), before)

    def test_upload_does_not_touch_non_channel_data(self):
        ns = self.ns
        image = bytearray(b"\xAE" * 80)
        a = 9
        b = 45
        ns["_put_u16le"](image, a + ns["CH_TX_FREQ"], 0xFFFF)
        ns["_put_u16le"](image, b + ns["CH_TX_FREQ"], 0x8F50)
        image[a + ns["CH_FLAGS_A"]] = 0xFF
        image[b + ns["CH_FLAGS_A"]] = 0xFB
        self.assertEqual(ns["_enforce_receive_only_tx_inhibit"](
            image, [{"offset": a}, {"offset": b}]), 1)
        self.assertEqual(image[b + ns["CH_FLAGS_A"]], 0xFB)
        self.assertEqual(ns["_u16le"](
            image, b + ns["CH_TX_FREQ"]), 0x8F50)
        self.assertEqual(image[0], 0xAE)


if __name__ == "__main__":
    unittest.main()
