# Copyright (c) 2026 Dave, KY4WI
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only sparse firmware fingerprint and patch checks."""
import ast
import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "tk840.py"
FUNCS = {
    "_select_firmware_patch_profile", "_matches_dealer_pattern",
    "_classify_tot_code", "_classify_plus25_code", "_classify_tx_code",
    "_probe_firmware_code", "_firmware_flags", "_firmware_revision_status_text",
    "_firmware_tx_status_text", "_firmware_tot_status_text",
    "_firmware_patch_advice",
}
ASSIGNS = {
    "FIRMWARE_FINGERPRINT_BLOCK", "FIRMWARE_PATCH_PROFILES",
    "_DEALER_TX_STOCK", "_DEALER_TX_PATCHED",
    "_PLUS25_STOCK", "_PLUS25_SHORT", "_PLUS25_FULL", "_PLUS25_SITES",
    "BLOCK_SIZE", "FIRMWARE_TAG_PREFIX_MASK", "FIRMWARE_TAG_PREFIX",
    "FIRMWARE_FEATURE_TX_FIX", "FIRMWARE_FEATURE_TOT_OFF",
}

class RadioError(Exception):
    pass

def namespace():
    tree=ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes=[]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCS:
            nodes.append(node)
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id in ASSIGNS for t in node.targets
        ):
            nodes.append(node)
    ns={"hashlib":hashlib,"LOG":logging.getLogger(__name__),
        "errors":SimpleNamespace(RadioError=RadioError),
        "_radio_label":lambda radio:"synthetic radio",
        "bytes":bytes}
    exec(compile(ast.Module(body=nodes,type_ignores=[]),str(SOURCE),"exec"),ns)
    return ns

class SyntheticRadio:
    def __init__(self,family):
        self._radio_family=family
        self.blocks={}
        self.reads=[]
        self.fail=set()

    def read_block(self,address):
        self.reads.append(address)
        if address in self.fail:
            raise RadioError("synthetic failed firmware read")
        return self.blocks.get(address,bytes([0xFF])*128)

    def place(self,address,data):
        for i,value in enumerate(data):
            offset=address+i
            base=offset & ~127
            buf=bytearray(self.blocks.get(base,bytes([0xFF])*128))
            buf[offset-base]=value
            self.blocks[base]=bytes(buf)

def seed(radio,rev,profile,tx="stock",tot="stock"):
    legacy_done=False
    for index,(addr,stock,patched) in enumerate(profile.get("tot_regions",())):
        if tot=="test10x":
            data=bytearray(patched)
            if index<2:
                data[37 if index==0 else 11]=0x01
        else:
            data=patched if tot=="ff-off" else stock
        radio.place(addr,data)
        if (tot=="legacy" and not legacy_done and len(stock)>21 and
                stock[20:22]==b"\x81\x01"):
            radio.place(addr+20,b"\x00\x00")
            legacy_done=True
    if "tx_addr" in profile:
        enhanced=(tx=="patched")
        radio.place(profile["tx_addr"],
                    profile["fix_tx"] if enhanced else profile["stock_tx"])
        template=NS["_DEALER_TX_PATCHED"] if enhanced else NS["_DEALER_TX_STOCK"]
        radio.place(profile["dealer_addr"],template[:1]+b"\x34\x12"+template[3:])
        plus=NS["_PLUS25_SHORT"] if tx=="plus25" else NS["_PLUS25_STOCK"]
        for addr in NS["_PLUS25_SITES"][rev]:
            radio.place(addr,plus)

NS=namespace()

class TestFirmwarePatchProbe(unittest.TestCase):
    def run_probe(self,rev,tx="stock",tot="stock"):
        profile=NS["FIRMWARE_PATCH_PROFILES"][rev]
        radio=SyntheticRadio("940" if profile["family"]=="94x" else "840")
        seed(radio,rev,profile,tx,tot)
        NS["_read_block"]=lambda radio,addr,**kw:radio.read_block(addr)
        NS["_select_firmware_patch_profile"]=lambda block,family:rev
        return radio,NS["_probe_firmware_code"](radio)

    def test_optional_patch_advice_is_verified_and_model_specific(self):
        advice = NS["_firmware_patch_advice"]
        self.assertEqual(
            advice("940", {"revision": "AB00", "tx": "stock",
                           "tot": "stock"}),
            "Optional Enhanced TX and Enhanced TOT patches available")
        self.assertEqual(
            advice("941", {"revision": "2CCA", "tx": "stock",
                           "tot": "unsupported"}),
            "Optional Enhanced TX Patch available")
        self.assertEqual(
            advice("840", {"revision": "8355", "tx": "not-applicable",
                           "tot": "stock"}),
            "Optional Enhanced TOT Patch available")
        self.assertIsNone(
            advice("840", {"revision": "8355", "tx": "not-applicable",
                           "tot": "ff-off"}))
        self.assertIsNone(
            advice("941", {"revision": "AB00", "tx": "patched",
                           "tot": "ff-off"}))
        self.assertIsNone(
            advice("941", {"revision": None, "tx": "stock", "tot": "stock"}))
        self.assertIsNone(advice("940", None))
        self.assertEqual(
            advice("840", {"revision": "9306", "tot": "legacy"}),
            "Enhanced TOT is an optional alternative to Legacy TOT Bypass")
        self.assertEqual(
            advice("840", {"revision": "8355", "tot": "test10x"}),
            "Restore normal-speed firmware after diagnostic testing")

    def test_sparse_known_enhanced_tot(self):
        for rev in ("AB00","8355","9306"):
            with self.subTest(rev=rev):
                radio,result=self.run_probe(rev,tot="ff-off")
                self.assertEqual(result["tot"],"ff-off")
                self.assertEqual(
                    NS["_firmware_tot_status_text"](0,result),
                    "Enhanced TOT Patch")
                self.assertLessEqual(len(set(radio.reads)),12)
                self.assertTrue(all(0x8000 <= a < 0xE580 for a in radio.reads))

    def test_exact_10x_test_firmware_is_detected_and_warned(self):
        for rev in ("AB00", "8355", "9306"):
            with self.subTest(rev=rev):
                radio,result=self.run_probe(rev,tot="test10x")
                self.assertEqual(result["tot"],"test10x")
                self.assertEqual(
                    NS["_firmware_tot_status_text"](None,result),
                    "WARNING: 10x TOT TEST MODE")
                self.assertLessEqual(len(set(radio.reads)),12)

    def test_partial_10x_changes_are_never_treated_as_test_mode(self):
        for rev in ("AB00", "8355", "9306"):
            with self.subTest(rev=rev):
                radio,result=self.run_probe(rev,tot="test10x")
                first=NS["FIRMWARE_PATCH_PROFILES"][rev]["tot_regions"][0]
                radio.place(first[0]+37,b"\x0A")
                self.assertEqual(NS["_probe_firmware_code"](radio)["tot"],
                                 "modified")
                radio.place(first[0]+37,b"\x01")
                # An unrelated edit at a known TOT site disqualifies 10x.
                radio.place(first[0],b"\xFF")
                self.assertEqual(NS["_probe_firmware_code"](radio)["tot"],
                                 "modified")

    def test_ab00_requires_unmodified_third_enhanced_tot_region(self):
        radio,result=self.run_probe("AB00",tot="test10x")
        self.assertEqual(result["tot"],"test10x")
        third=NS["FIRMWARE_PATCH_PROFILES"]["AB00"]["tot_regions"][2]
        radio.place(third[0],third[1])
        self.assertEqual(NS["_probe_firmware_code"](radio)["tot"],
                         "modified")

    def test_known_legacy_tot_bypasses(self):
        for rev in ("AB00","8355","9306"):
            with self.subTest(rev=rev):
                radio,result=self.run_probe(rev,tot="legacy")
                self.assertEqual(result["tot"],"legacy")
                self.assertEqual(
                    NS["_firmware_tot_status_text"](None,result),
                    "Legacy TOT Bypass")

    def test_enhanced_tx_without_metadata(self):
        for rev in ("AB00","B5EC","2CCA","8E9D"):
            radio,result=self.run_probe(rev,tx="patched")
            self.assertEqual(result["tx"],"patched")
            self.assertEqual(
                NS["_firmware_tx_status_text"](0,"94x",result),
                "Enhanced TX Patch")

    def test_plus25_patch_is_named(self):
        for rev in ("AB00","B5EC","2CCA","8E9D"):
            radio,result=self.run_probe(rev,tx="plus25")
            self.assertEqual(result["tx"],"plus25")
            self.assertEqual(
                NS["_firmware_tx_status_text"](0,"94x",result),
                "+25 MHz Offset Patch")

    def test_both_940_and_941_models_share_firmware_fingerprints(self):
        profile=NS["FIRMWARE_PATCH_PROFILES"]["AB00"]
        NS["_select_firmware_patch_profile"]=lambda block,family: (
            "AB00" if family=="94x" else None)
        NS["_read_block"]=lambda radio,addr,**kw:radio.read_block(addr)
        for model in ("940","941"):
            radio=SyntheticRadio(model)
            seed(radio,"AB00",profile,"patched","ff-off")
            p=NS["_probe_firmware_code"](radio)
            self.assertEqual(p["revision"],"AB00")
            self.assertEqual(p["tx"],"patched")
            self.assertEqual(p["tot"],"ff-off")

    def test_partial_and_mixed_modifications_are_not_verified(self):
        radio,result=self.run_probe("AB00",tx="patched",tot="ff-off")
        radio.place(0x94E6,b"\x00")
        radio.place(NS["FIRMWARE_PATCH_PROFILES"]["AB00"]["dealer_addr"],b"\xFF")
        p=NS["_probe_firmware_code"](radio)
        self.assertEqual(p["tot"],"modified")
        self.assertEqual(p["tx"],"partial")

    def test_unavailable_blocks_cause_unverified_without_abort(self):
        radio,result=self.run_probe("8355",tot="ff-off")
        radio.fail.add(0x9480)
        self.assertEqual(NS["_probe_firmware_code"](radio)["tot"],"unverified")

    def test_unknown_fingerprint_skips_patch_reads(self):
        radio=SyntheticRadio("840")
        NS["_read_block"]=lambda radio,addr,**kw:radio.read_block(addr)
        NS["_select_firmware_patch_profile"]=namespace()[
            "_select_firmware_patch_profile"]
        result=NS["_probe_firmware_code"](radio)
        self.assertIsNone(result["revision"])
        self.assertEqual(radio.reads,[0xDF00])

    def test_known_profile_fingerprints_are_unique(self):
        profiles=NS["FIRMWARE_PATCH_PROFILES"]
        fingerprints=[p["fingerprint"] for p in profiles.values()]
        self.assertEqual(len(fingerprints),len(set(fingerprints)))
        self.assertEqual(len(profiles),6)
        for name,profile in profiles.items():
            self.assertEqual(len(profile["fingerprint"]),64)
            for addr,stock,patched in profile.get("tot_regions",()):
                self.assertEqual(len(stock),len(patched))
                self.assertGreaterEqual(addr,0x8000)

    def test_9306_reports_as_tk840(self):
        probe={"revision":"9306","tx":"not-applicable","tot":"stock"}
        self.assertEqual(
            NS["_firmware_revision_status_text"](probe),
            "TK-840 9306 (fingerprint verified)")

if __name__=="__main__":
    unittest.main()
