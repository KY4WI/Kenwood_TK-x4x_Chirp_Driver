# Copyright (c) 2026 Dave, KY4WI
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for current firmware feature bits and TOT-Off handling.

The feature byte is informational for TOT. The driver always exposes the
Enhanced TOT Patch Off value and leaves it to the user to select it only when
the radio has suitable firmware.
"""
import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "tk840.py"
FUNCTIONS = {
    "_firmware_flags", "_firmware_feature_status", "_firmware_tot_status_text",
    "_tot_setting_value", "_tot_choice_to_raw", "_tot_test10x_detected",
    "_list_value_with_unknown",
}
CONSTANTS = {
    "FIRMWARE_TAG_PREFIX_MASK", "FIRMWARE_TAG_PREFIX",
    "FIRMWARE_FEATURE_TX_FIX", "FIRMWARE_FEATURE_TOT_OFF",
    "TOT_CHOICES", "TOT_TEST10X_CHOICES", "TOT_OFF_LABEL",
}


class FakeList:
    def __init__(self, choices, current_index=0):
        self.choices = list(choices)
        self.current_index = current_index


def load_code():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in CONSTANTS
                for target in node.targets):
            nodes.append(node)
    ns = {
        "RadioSettingValueList": FakeList,
        "errors": SimpleNamespace(InvalidValueError=ValueError),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"),
         ns)
    return ns


class TestFirmwareTotFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = load_code()

    def test_current_feature_tags(self):
        flags = self.ns["_firmware_flags"]
        self.assertEqual(flags(0x80), 0x00)
        self.assertEqual(flags(0x81), 0x01)
        self.assertEqual(flags(0x82), 0x02)
        self.assertEqual(flags(0x83), 0x03)
        for tag in (0x00, 0x40, 0x43, 0x48, 0xC3, 0xFF,
                    None, "0x83", True):
            with self.subTest(tag=tag):
                self.assertEqual(flags(tag), 0)

    def test_patch_metadata_is_informational(self):
        metadata = self.ns["_firmware_feature_status"]
        status = self.ns["_firmware_tot_status_text"]
        self.assertEqual(metadata(0x81), "Enhanced TOT Patch")
        self.assertEqual(metadata(0x82), "Enhanced TX Patch")
        self.assertEqual(
            metadata(0x83), "Enhanced TX Patch, Enhanced TOT Patch")
        self.assertEqual(metadata(0x80), "None")
        self.assertEqual(status(0x81), "Not verified")
        self.assertEqual(status(0x80), "Not verified")
        self.assertEqual(status(None), "Not verified")
        self.assertEqual(status(0x80, {"tot": "test10x"}),
                         "WARNING: 10x TOT TEST MODE")

    def test_off_is_always_shown(self):
        ns = self.ns
        for raw in (0, 1, 39, 0xFE, 0xFF):
            with self.subTest(raw=raw):
                setting = ns["_tot_setting_value"](raw)
                self.assertIn("Off (Enhanced TOT Patch only)", setting.choices)
        setting = ns["_tot_setting_value"](0xFF)
        self.assertEqual(setting.choices[setting.current_index],
                         "Off (Enhanced TOT Patch only)")

    def test_regular_tot_values_still_work(self):
        ns = self.ns
        for raw, label in ((0, "0:15"), (1, "0:30"), (39, "10:00")):
            with self.subTest(raw=raw):
                setting = ns["_tot_setting_value"](raw)
                self.assertEqual(setting.choices[setting.current_index],
                                 label)
                self.assertEqual(ns["_tot_choice_to_raw"](label), raw)
                self.assertTrue(all("10x" not in choice
                                    for choice in setting.choices))

    def test_verified_10x_uses_actual_durations_without_reencoding(self):
        ns = self.ns
        self.assertFalse(ns["_tot_test10x_detected"](None))
        self.assertFalse(ns["_tot_test10x_detected"]({"tot": "modified"}))
        self.assertTrue(ns["_tot_test10x_detected"]({"tot": "test10x"}))
        for raw, label in ((0, "0:15 (10x: 1.5 sec)"),
                           (1, "0:30 (10x: 3 sec)"),
                           (2, "0:45 (10x: 4.5 sec)"),
                           (11, "3:00 (10x: 18 sec)"),
                           (39, "10:00 (10x: 60 sec)")):
            with self.subTest(raw=raw):
                selected=ns["_tot_setting_value"](raw,accelerated=True)
                self.assertEqual(selected.choices[selected.current_index],
                                 label)
                self.assertEqual(ns["_tot_choice_to_raw"](
                    label,accelerated=True),raw)
                # The same raw byte has an ordinary display when unverified.
                normal=ns["_tot_setting_value"](raw)
                self.assertEqual(
                    ns["_tot_choice_to_raw"](
                        normal.choices[normal.current_index]),raw)

    def test_all_40_test_labels_round_trip_without_changing_normal_options(self):
        ns = self.ns
        for raw in range(40):
            with self.subTest(raw=raw):
                normal=ns["_tot_setting_value"](raw)
                annotated=ns["_tot_setting_value"](raw,accelerated=True)
                normal_label=normal.choices[normal.current_index]
                test_label=annotated.choices[annotated.current_index]
                self.assertTrue(test_label.startswith(normal_label+" (10x: "))
                self.assertTrue(test_label.endswith(" sec)"))
                self.assertNotIn("10x",normal_label)
                self.assertEqual(ns["_tot_choice_to_raw"](normal_label),raw)
                self.assertEqual(ns["_tot_choice_to_raw"](
                    test_label,accelerated=True),raw)
                self.assertEqual(len(annotated.choices),41)
                self.assertEqual(len(normal.choices),41)

    def test_accelerated_off_and_unknown_bytes_are_preserved(self):
        ns=self.ns
        selected=ns["_tot_setting_value"](0xFF,accelerated=True)
        self.assertEqual(selected.choices[selected.current_index],
                         "Off (Enhanced TOT Patch only)")
        self.assertEqual(ns["_tot_choice_to_raw"](
            "Off (Enhanced TOT Patch only)",accelerated=True),0xFF)
        unknown=ns["_tot_setting_value"](0xFE,accelerated=True)
        self.assertEqual(unknown.choices[-1],"Unknown (0xFE)")
        self.assertIsNone(ns["_tot_choice_to_raw"](
            "Unknown (0xFE)",accelerated=True))
        for wrong in ("0:15", "garbage", "1.5 seconds",
                      "0.5 seconds", "60.5 seconds"):
            with self.subTest(wrong=wrong):
                with self.assertRaises(ValueError):
                    ns["_tot_choice_to_raw"](wrong,accelerated=True)
        with self.assertRaises(ValueError):
            ns["_tot_choice_to_raw"]("1.5 seconds")

    def test_off_always_writes_ff(self):
        ns = self.ns
        for label in ("Off (Enhanced TOT Patch only)", "OFF"):
            self.assertEqual(ns["_tot_choice_to_raw"](label), 0xFF)

    def test_unknown_values_preserved_and_bad_timing_rejected(self):
        ns = self.ns
        setting = ns["_tot_setting_value"](0xFE)
        self.assertEqual(setting.choices[-1], "Unknown (0xFE)")
        self.assertIsNone(ns["_tot_choice_to_raw"]("Unknown (0xFE)"))
        for label in ("10:15", "0:20", "5:70", "garbage"):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    ns["_tot_choice_to_raw"](label)


if __name__ == "__main__":
    unittest.main()
