# Copyright 2026 Dave KY4WI <chirp-kenwood-tk-x4x@ky4wi.us>
# Copyright 2016 Pavel Milanes CO7WT, <co7wt@frcuba.co.cu> <pavelmc@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Based in part on CHIRP's Kenwood TK-760 driver and related Kenwood driver
# code. The original copyright notice and GPL terms are retained here.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# Release: v1.0.0-beta.1
#
# CHIRP driver for the Kenwood TK-840, TK-940 and TK-941.
#
# Conventional operation, radio-configuration read/write, password
# authentication, System editing, ham-oriented cleanup policies and the
# dealer/panel alignment table are supported. Live downloads on all three
# models also perform a small read-only firmware probe to report recognized
# firmware revisions and historical or enhanced patches. Firmware updating
# and patch installation are handled by the separate TK-x4x Toolkit:
# https://github.com/KY4WI/Kenwood_TK-x4x_Toolkit
#
# Derived from the general structure of CHIRP's tk760.py driver and from
# reverse engineering Kenwood KPG-25D v3.x. Configuration download/upload,
# conventional record handling, and feature detection have been
# hardware-tested.
# Uploads are confined to E600-FDFF. Each block is read and compared; changed
# blocks are written and immediately read back before moving to the next block,
# with up to two retries if verification fails. Optional normalization keeps
# unused configuration fields in factory form.

import hashlib
import logging
import struct
import time

from chirp import chirp_common, directory, errors, memmap, util
from chirp.settings import (
    RadioSetting,
    RadioSettingGroup,
    RadioSettingValueBoolean,
    RadioSettingValueInteger,
    RadioSettingValueList,
    RadioSettingValueString,
    RadioSettings,
)

LOG = logging.getLogger(__name__)

ACK = b"\x06"
NAK = b"\x15"
PROGRAM = b"PROGRAM"

INITIAL_BAUD = 1200
CLONE_BAUD = 9600
TIMEOUT = 1.5

RADIO_BASE = 0xE600
MEM_SIZE = 0x1800
BLOCK_SIZE = 0x80
RADIO_END = RADIO_BASE + MEM_SIZE

# PROGRAM-mode password field. KPG-25D sends command 04h followed by these
# ten bytes; the resident mask-ROM monitor compares them directly against
# E670-E679. An unprotected radio stores FF in all ten positions.
PASSWORD_BLOCK_ADDR = RADIO_BASE       # E600-E67F contains the whole field
PASSWORD_OFFSET = 0x0070               # E670 relative to the codeplug
PASSWORD_LENGTH = 10
PASSWORD_ERASED = b"\xFF" * PASSWORD_LENGTH
PASSWORD_CHARSET = "".join(chr(value) for value in range(0x20, 0x7F))

# KPG-25D's packed conventional-system representation.
SYSTEM_COUNT = 32
SYSTEM_PTR_BASE = 0x0000       # 32 little-endian 16-bit entries
ACTIVE_SYSTEM_COUNT = 0x0040
GROUP_COUNT_BASE = 0x0041      # 32 bytes
SYSTEM_DATA_PTR_BASE = 0x2600  # value subtracted from pointer's low 14 bits
SYSTEM_DATA_START = 0x0100
SYSTEM_SCAN_INCLUDE_PTR_MASK = 0x4000
SYSTEM_SCAN_INCLUDE_PTR_BYTE_MASK = 0x40
MAX_EDITABLE_SYSTEMS = SYSTEM_COUNT

CONV_HEADER_SIZE = 0x0E
CONV_RECORD_SIZE = 0x13        # 19 bytes
# Kenwood documents 32 Systems, up to 250 conventional channels in one
# System, and 308 conventional channels total. System blocks are variable
# length and share the packed 0x0100-0x17FF data pool, so the physical image
# size can impose a lower total when many Systems are active.
MAX_GROUPS_PER_EDITABLE_SYSTEM = 250
MAX_TOTAL_CONVENTIONAL_CHANNELS = 308
MAX_LOGICAL_MEMORIES = MAX_TOTAL_CONVENTIONAL_CHANNELS

# Conventional System controls. These are separate settings:
#   pointer bit 14: System Lockout state, set = System enabled/unlocked
#   header byte 0 bit 7: Scan List membership, active-low (0 = Yes, 1 = No)
# CHIRP's standard Skip column maps to System Lockout because that is the
# closest native meaning. Scan List is exposed separately in System Settings.
SYSTEM_FLAGS = 0x00
SYSTEM_SCAN_LIST_EXCLUDE_MASK = 0x80
SYSTEM_NAME = 0x06
SYSTEM_NAME_LENGTH = 8
# Canonical KPG-created conventional header observed on the TK-840. Bits and
# bytes that are LTR-only remain in their normal inactive/high state. Scan List
# and System Enabled are applied separately when the System becomes active.
CONV_HEADER_DEFAULT = (
    bytes((0xFE, 0xFF, 0xFE, 0xFF, 0xFF, 0xFF)) + (b"\xFF" * 8))

# Channel record offsets
CH_GROUP = 0x00
CH_RX_FREQ = 0x01              # little-endian u16
CH_TX_FREQ = 0x03              # little-endian u16
CH_TX_TONE = 0x05              # little-endian u16; QT/DQT encode
CH_RX_TONE = 0x07              # little-endian u16; QT/DQT decode
CH_FLAGS_A = 0x09
CH_FLAGS_B = 0x0A
CH_NAME = 0x0B                 # 8 bytes

# The two bytes at record+0x09 and +0x0A contain active-low feature flags.
# Firmware tracing of the TK-840 application shows Flags-A bits 6/7 are unused
# by this model, and Flags-B bits 1..7 are likewise unused.  The radio copies
# Flags-B bit 0 (Data) into runtime Flags-A bit 6, overwriting the stored A6,
# and never consumes runtime bit 7.  Canonicalize all proven-unused stored bits
# high (1) when uploading a TK-840 codeplug.

# Frequency coding proven for the normal 12.5-kHz grid:
#   MHz = 300.000 + code * 0.0125
# TK-840 firmware explicitly clears bit 15 after fetching either conventional
# RX or TX frequency word, and no conventional-channel path consumes it as a
# separate option. KPG-25D likewise masks it out for numeric frequency work and
# preserves it separately. Existing values are therefore preserved verbatim by
# default, while newly created channels and explicit unused-bit normalization
# use the KPG-created canonical value of 1.
FREQ_BASE_HZ = 300_000_000
FREQ_BASE_94X_HZ = 851_000_000
FREQ_STEP_HZ = 12_500
FREQ_UNUSED_MASK = 0x8000

# KPG-25D's group-name editor invokes its generic text editor in input mode 3.
# The mode-3 validator accepts alphanumerics plus exactly these punctuation
# characters: space, # $ %, ( ) * + , - / = @ \ _ |.  Notably, '.' is
# rejected by KPG-25D itself. KPG accepts lower-case input, but the TK-840
# front-panel display does not render lower-case. The driver therefore accepts
# lower-case from CHIRP and stores it upper-case.
STORED_NAME_CHARSET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    " #$%()*+,-/=@\\_|"
)
# The radio/KPG character set has no period, so period input is stored as a
# comma, which the display shows almost the same way. Channel names are offered
# to CHIRP upper-case only; filter_name() below maps '.' to ',' and CHIRP then
# upper-cases, so the editor and imports show exactly what is stored. System
# names in Settings get the same mapping from _encode_name().
CHANNEL_NAME_CHARSET = STORED_NAME_CHARSET.replace(
    "abcdefghijklmnopqrstuvwxyz", "")
NAME_CHARSET = STORED_NAME_CHARSET + "."

# KPG-25D conventional-group option mapping.  Record byte +0x09 carries
# Optional Signalling, Talk Around, TX Inhibit, Call, BCL and Horn; record
# byte +0x0A bit 0 carries Data.  All are active-low: zero enables the option.
#
# Only TX Inhibit is treated as a normal ham-useful option. Kenwood's Data
# bit enables/disables data transmission for the selected group (TK-840/(N)
# service manual, section 3-25). It remains with the optional commercial
# controls because ordinary ham-channel editing does not need it.
FLAGA_OPTIONAL_SIGNALING = 0x01
FLAGA_TALK_AROUND = 0x02
FLAGA_TX_INHIBIT = 0x04
FLAGA_CALL = 0x08
FLAGA_BUSY_LOCKOUT = 0x10
FLAGA_HORN = 0x20

# Proven unused on the ordinary TK-840 application firmware. They are preserved
# verbatim by default and normalized high only when explicitly requested.
FLAGA_UNUSED = 0xC0          # bits 6..7

FLAGB_DATA = 0x01
FLAGB_UNUSED = 0xFE          # bits 1..7

# Talk Around is operationally significant, so automatic commercial-feature
# cleanup must never clear it. Keep it independently editable.
FLAGA_COMMERCIAL = (FLAGA_OPTIONAL_SIGNALING | FLAGA_CALL |
                    FLAGA_BUSY_LOCKOUT | FLAGA_HORN)
FLAGB_COMMERCIAL = FLAGB_DATA
FLAGA_EDITABLE = (FLAGA_TALK_AROUND | FLAGA_TX_INHIBIT |
                  FLAGA_COMMERCIAL | FLAGA_UNUSED)
FLAGB_EDITABLE = FLAGB_COMMERCIAL | FLAGB_UNUSED

# KPG-25D global-options word at image offset 0x006A.  Its three consecutive
# off-hook options are bits 8, 9 and 10: Off Hook Scan, Off Hook Horn Alert,
# and Off Hook Decode respectively.  KPG's own display routine maps bit=0 to
# "Enable" and bit=1 to "Disable".  They therefore share image byte
# 0x006B and are all active-low.
GLOBAL_OFFHOOK_SCAN = 0x006B
GLOBAL_OFFHOOK_SCAN_MASK = 0x01
GLOBAL_OFFHOOK_HORN = 0x006B
GLOBAL_OFFHOOK_HORN_MASK = 0x02
GLOBAL_OFFHOOK_DECODE = 0x006B
GLOBAL_OFFHOOK_DECODE_MASK = 0x04

# KPG-25D Scan Switch is encoded in the low byte of the same global word.
# bit 0 = 1: SCAN key disabled
# bit 0 = 0, bit 6 = 0: Scan List
# bit 0 = 0, bit 6 = 1: Fix System Scan
GLOBAL_SCAN_SWITCH = 0x006A
GLOBAL_SCAN_DISABLE_MASK = 0x01
GLOBAL_SCAN_FIXED_MASK = 0x40
SCAN_SWITCH_CHOICES = ["Disabled", "Scan List", "Fixed System Scan"]

# KPG-25D stores the AUX assignment in bits 8..10 of its word at image offset
# 0x0066.  In the cloned byte stream this is byte 0x0067, bits 0..2.  Its
# display routine decodes values 0..7 in this order.  The labels below use
# normal ham terminology where practical while retaining Kenwood's term in
# parentheses when useful.
GLOBAL_AUX_SWITCH = 0x0067
GLOBAL_AUX_SWITCH_MASK = 0x07
AUX_SWITCH_CHOICES = [
    "Scan Add/Delete",
    "Home Channel (Fixed Revert)",
    "Name/System-Group Display",
    "Auto-Tel",
    "Horn Alert",
    "Manual Relay",
    "Optional Signaling Reset",
    "Not Assigned",
]

# Firmware/KPG-proven unused upper bits in the AUX-assignment byte. Every
# application read masks E667 with 07h before use. Preserve by default and
# canonicalize high only when explicit unused-bit normalization is requested.
GLOBAL_AUX_UNUSED_MASK = 0xF8

# Ham-useful global timing and fixed-revert fields. KPG-25D stores Dispatch
# TOT as a 0..39 index representing 15..600 seconds in 15-second increments.
# Scan drop-out and post-TX dwell are direct seconds, 0..254.
GLOBAL_TOT_TEL = 0x0061
GLOBAL_TOT_DISPATCH = 0x0062
GLOBAL_SCAN_DROPOUT = 0x0063
GLOBAL_SCAN_DWELL = 0x0064
GLOBAL_HOME_SYSTEM = 0x0068
GLOBAL_HOME_GROUP = 0x0069

# Mask-ROM power-on key/mode enables at codeplug offset 0x006C (CPU E66C).
# Bench-tested mappings on the TK-840:
#   bit 0 / event 0x8C = GRP UP   -> Dealer Mode
#   bit 1 / event 0x8D = GRP DOWN -> Panel Tuning / Alignment Mode
#   bit 2 / event 0x84 = SYSTEM UP -> Firmware Programming / PROG
#   bit 3 / event 0x9E = AUX       -> Clone Mode
# Bits are active-high: set = permitted.  The common mask ROM uses the same
# dispatcher on the TK-940/941 family. The TK-840, TK-940 and TK-941
# family behavior is hardware-tested.
GLOBAL_POWERON_OPTIONS = 0x006C
POWERON_EVENT_8C_MASK = 0x01
POWERON_EVENT_8D_MASK = 0x02
POWERON_SYSTEM_UP_MASK = 0x04
POWERON_EVENT_9E_MASK = 0x08

# Driver-only preferences belong in CHIRP's standard .img metadata trailer,
# not in the radio's 6144-byte configuration image. CloneModeRadio strips this
# metadata before constructing _mmap, so these values can never be uploaded.
DRIVER_PREFS_METADATA_KEY = "tkx4x_driver_preferences"
DRIVER_PREFS_METADATA_VERSION = 1

# Toolkit firmware feature byte at E5DF, outside the CHIRP codeplug.
# Metadata format: prefix 0x80, bit 0 Enhanced TOT Patch,
# bit 1 Enhanced TX Patch.
FIRMWARE_FEATURE_BLOCK = 0xE580
FIRMWARE_FEATURE_ADDR = 0xE5DF
FIRMWARE_TAG_PREFIX_MASK = 0xC0
FIRMWARE_TAG_PREFIX = 0x80
FIRMWARE_FEATURE_TOT_OFF = 0x01
FIRMWARE_FEATURE_TX_FIX = 0x02

# Read-only firmware fingerprint references, mirrored from TK-x4x Toolkit.
# A revision is accepted only after matching the SHA-256 of firmware block
# DF00-DF7F. This block is unchanged by all recognized patches.
# Byte-exact patch regions are checked at fixed addresses, never searched or
# transplanted to other firmware. Unknown/mixed data is NOT counted as patched.
FIRMWARE_FINGERPRINT_BLOCK = 0xDF00
FIRMWARE_PATCH_PROFILES = {
    "8355": {
        "family": "840",
        "fingerprint": (
            "9e5c43f511294d9269f1cb20d179d7b5"
            "dba0df12c77f742d63a95b69b358ccc3"),
        "legacy_tot": True,
        "tot_regions": [
            (0x94E9,
             bytes.fromhex(
                 "08a17903727f0c09f062e6af28830eb903140a09f061e6af"
                 "288302b90bc1b800d862f401052a240a1a40a07d3a420a56"),
             bytes.fromhex(
                 "08a17903727f2009f062e6b803afff4208117daf288301d0"
                 "c1bbfa050b31c81a40a07d3a420a5609f061e6b80b4014e0")),
            (0xB0DE,
             bytes.fromhex(
                 "08d5d50156274280133a420a07e9401c402f0000810156b0"
                 "7d2839c656"),
             bytes.fromhex(
                 "08d5d50156274280fb3a420a717df51c402e01001a4080ec"
                 "b07d2c39c6")),
        ],
    },
    "9306": {
        "family": "840",
        "fingerprint": (
            "09b613b6ae83ba86a754e7226482d433"
            "2121d9c1e9b64dc05b9c818d582a8db7"),
        "legacy_tot": True,
        "tot_regions": [
            (0x94EF,
             bytes.fromhex(
                 "08a17903727f0c09f062e6af28830eb903140a09f061e6af"
                 "288302b90bc1b800d862f401052a240a1a40a07d3a420a56"),
             bytes.fromhex(
                 "08a17903727f2009f062e6b803afff4208117daf288301d0"
                 "c1bbfa050b31c81a40a07d3a420a5609f061e6b80b4014e0")),
            (0xB15F,
             bytes.fromhex(
                 "08d5d50156274280133a420a07e9401c402f0000810156b0"
                 "7d28f2c656"),
             bytes.fromhex(
                 "08d5d50156274280fb3a420a717df51c402e01001a4080ec"
                 "b07d2cf2c6")),
        ],
    },
    "2CCA": {
        "family": "94x",
        "fingerprint": (
            "ca97cbe94e06e993ce06671e8d5e8fb0"
            "ed5e2356b167c25aa874821205dd7196"),
        "tx_addr": 0x9A13,
        "stock_tx": bytes.fromhex("28429a719e0a2df0091a203a22011418"),
        "fix_tx": bytes.fromhex("283bd983294747475924015d039f14c7"),
        "dealer_addr": 0x9839,
    },
    "8E9D": {
        "family": "94x",
        "fingerprint": (
            "d8f4812b6359aeb456739059fa799514"
            "8616763e96cdaa175588d699f7b4386d"),
        "tx_addr": 0x9A41,
        "stock_tx": bytes.fromhex("28709a719e0a2df0091a203a22011418"),
        "fix_tx": bytes.fromhex("2811da83294747475924015d039f14c7"),
        "dealer_addr": 0x9867,
    },
    "AB00": {
        "family": "94x",
        "fingerprint": (
            "4d53b1384e082aedd5587cf1e8e4c5c8"
            "7a88ff65ff67e784df27125d1b929f61"),
        "tx_addr": 0x9C61,
        "stock_tx": bytes.fromhex("28909c719e0a2df0091a203a22011418"),
        "fix_tx": bytes.fromhex("286fdd83294747475924015d039f14c7"),
        "dealer_addr": 0x9A8E,
        "legacy_tot": True,
        "tot_regions": [
            (0x94E6,
             bytes.fromhex(
                 "08a17903727f0c09f062e6af28830eb903140a09f061e6af"
                 "288302b90bc1b800d862f401052a240a1a40a07d3a420a56"),
             bytes.fromhex(
                 "08a17903727f2009f062e6b803afff4208117daf288301d0"
                 "c1bbfa050b31c81a40a07d3a420a5609f061e6b80b4014e0")),
            (0xB19A,
             bytes.fromhex(
                 "08d5d50156274280133a420a07e9401c402f0000810156b0"
                 "7d28dcc256"),
             bytes.fromhex(
                 "08d5d50156274280fb3a420a717df51c402e01001a4080ec"
                 "b07d2cdcc2")),
            (0xD6F2,
             bytes.fromhex(
                 "07e9a01ca02f00008002a5c408a7780c07e9401c402f0000"
                 "8002b07d07e9ae1cae2f00008002b1db"),
             bytes.fromhex(
                 "1ca02e01001aa08002a5c408a7780e717d0b1c402e01001a"
                 "408002b07d1cae2e01001aae8002b1db")),
        ],
    },
    "B5EC": {
        "family": "94x",
        "fingerprint": (
            "122298db0e22e18db531b62b4e436b00"
            "e7eb88514596cd4755c7027d532f5f3e"),
        "tx_addr": 0x9C6E,
        "stock_tx": bytes.fromhex("289d9c719e0a2df0091a203a22011418"),
        "fix_tx": bytes.fromhex("289ade83294747475924015d039f14c7"),
        "dealer_addr": 0x9A94,
    },
}

_PLUS25_STOCK = bytes.fromhex("2dc0fd1a208205")
_PLUS25_SHORT = bytes.fromhex("2d20021a208205")
_PLUS25_FULL = bytes.fromhex("2d20021a200000")
_PLUS25_SITES = {
    "AB00": (0x9AB0, 0x9C4B, 0x9C7A),
    "B5EC": (0x9AB6, 0x9C58, 0x9C87),
    "2CCA": (0x985B, 0x99FD, 0x9A2C),
    "8E9D": (0x9889, 0x9A2B, 0x9A5A),
}

# Fixed/dealer test routine. Bytes 1 and 2 contain a stock CALL destination;
# they are ignored only within an otherwise byte-exact, known-revision match.
_DEALER_TX_STOCK = bytes.fromhex(
    "28 00 00 24 c8 58 24 01 5c 03 9f 08 a4 78 0a")
_DEALER_TX_PATCHED = bytes.fromhex(
    "28 00 00 24 c8 46 46 58 24 01 5c 03 9f 14 13")

# Dealer/panel-tuning alignment-frequency table.  The table occupies ten
# four-byte slots at E680-E6A7 (codeplug offsets 0x0080-0x00A7).  On the
# TK-840 only seven slots are populated by Kenwood; the remaining three are FF.
# Each populated TK-840 record is RX word followed by TX word.
TEST_TABLE_OFFSET = 0x0080
TEST_TABLE_LENGTH = 0x28
TEST_TABLE_SLOTS = 10

# Ham-oriented defaults are convenience values only. All ten physical table
# slots remain editable, so users may replace these with any frequency the
# radio's table encoding can represent. Tuple order is TX, RX.
HAM_TEST_840 = (
    (420_000_000, 420_000_000),
    (423_000_000, 423_000_000),
    (426_000_000, 426_000_000),
    (430_000_000, 430_000_000),
    (433_000_000, 433_000_000),
    (436_000_000, 436_000_000),
    (440_000_000, 440_000_000),
    (443_000_000, 443_000_000),
    (446_000_000, 446_000_000),
    (450_000_000, 450_000_000),
)

# TK-941 defaults span the 33-cm amateur band. RX and TX are intentionally
# equal at each alignment point; the dealer-mode firmware fix makes TX honor
# the second word instead of deriving a fixed split from the first.
HAM_TEST_941 = (
    (902_000_000, 902_000_000),
    (905_000_000, 905_000_000),
    (908_000_000, 908_000_000),
    (911_000_000, 911_000_000),
    (914_000_000, 914_000_000),
    (917_000_000, 917_000_000),
    (920_000_000, 920_000_000),
    (923_000_000, 923_000_000),
    (926_000_000, 926_000_000),
    (928_000_000, 928_000_000),
)

TOT_CHOICES = ["%d:%02d" % divmod(seconds, 60)
               for seconds in range(15, 601, 15)]
# Show the programmed (normal-speed) duration first, with the accelerated
# actual duration beside it. Both use exactly the same codeplug byte.
TOT_TEST10X_CHOICES = [
    "%s (10x: %g sec)" % (normal, seconds / 10.0)
    for normal, seconds in zip(TOT_CHOICES, range(15, 601, 15))
]
TOT_OFF_LABEL = "Off (Enhanced TOT Patch only)"
DELAY_CHOICES = ["%d:%02d" % divmod(seconds, 60)
                 for seconds in range(0, 255)]
HOME_SYSTEM_CHOICES = [str(number) for number in range(1, SYSTEM_COUNT + 1)]
HOME_GROUP_CHOICES = [str(number) for number in range(1, 251)]


def _encode_test_word_840(freq_hz):
    """Encode a TK-840 dealer/tuning-table frequency word."""
    delta = int(freq_hz) - 300_000_000
    if delta < 0 or delta % 12_500:
        raise errors.InvalidValueError(
            "TK-840 test frequency must be on a 12.5 kHz step")
    steps = delta // 12_500
    if not 0 <= steps <= 0x7FFE:
        raise errors.InvalidValueError("TK-840 test frequency is out of range")
    return steps | 0x8000


def _build_test_table_840(pairs):
    data = bytearray(b"\xFF" * TEST_TABLE_LENGTH)
    for index, (tx_hz, rx_hz) in enumerate(pairs):
        if index >= TEST_TABLE_SLOTS:
            break
        off = index * 4
        # Kenwood stores RX first, TX second in this table.
        data[off:off + 2] = struct.pack("<H", _encode_test_word_840(rx_hz))
        data[off + 2:off + 4] = struct.pack("<H", _encode_test_word_840(tx_hz))
    return bytes(data)


def _encode_test_word_941(freq_hz, base_hz):
    """Encode a TK-941 alignment-table word for its RX or TX path."""
    delta = int(freq_hz) - int(base_hz)
    if delta < 0 or delta % 12_500:
        raise errors.InvalidValueError(
            "TK-941 test frequency must be on a 12.5 kHz step")
    steps = delta // 12_500
    if not 0 <= steps <= 0x7FFE:
        raise errors.InvalidValueError("TK-941 test frequency is out of range")
    return steps


def _build_test_table_941(pairs):
    data = bytearray(b"\xFF" * TEST_TABLE_LENGTH)
    for index, (tx_hz, rx_hz) in enumerate(pairs):
        if index >= TEST_TABLE_SLOTS:
            break
        off = index * 4
        # The service table has model-specific frequency bases: RX 851 MHz
        # and TX 812 MHz. Separate words allow patched dealer-mode firmware to
        # use independent receive and transmit alignment frequencies.
        rx_word = _encode_test_word_941(rx_hz, 851_000_000)
        tx_word = _encode_test_word_941(tx_hz, 812_000_000)
        data[off:off + 2] = struct.pack("<H", rx_word)
        data[off + 2:off + 4] = struct.pack("<H", tx_word)
    return bytes(data)


def _decode_test_word_840(word):
    if word == 0xFFFF or (word & 0x7FFF) == 0x7FFF:
        return None
    return 300_000_000 + (word & 0x7FFF) * 12_500


def _decode_test_word_941(word, base_hz):
    if word == 0xFFFF or (word & 0x7FFF) == 0x7FFF:
        return None
    return int(base_hz) + (word & 0x7FFF) * 12_500


def _format_test_frequency(freq_hz):
    if freq_hz is None:
        return ""
    return "%.6f" % (int(freq_hz) / 1_000_000.0)


def _parse_test_frequency(value):
    text = str(value).strip()
    if not text:
        return None
    try:
        hz = int(round(float(text) * 1_000_000.0))
    except (TypeError, ValueError):
        raise errors.InvalidValueError(
            "Test/alignment frequency must be entered in MHz")
    return hz


def _radio_label(radio):
    return getattr(radio, "_display_model", "Kenwood radio")


def _ident_model(ident):
    """Return the model name represented by a seven-byte PROGRAM identity."""
    if len(ident) != 7 or ident[6:7] not in (b"N", b"Y"):
        return None
    if ident[0:6] in (b"M940B1", b"M941B1"):
        return "TK-940" if ident[1:4] == b"940" else "TK-941"
    if (ident[0:1] == b"M" and ident[1:2] == b"8" and
            ident[2:3] in (b"4", b"9") and ident[3:5] == b"0B" and
            ident[5:6] in (b"1", b"2", b"3")):
        return "TK-840"
    return None


def _ident_matches_radio(radio, ident):
    """Match PROGRAM identity while treating TK-940/TK-941 as one family."""
    found = _ident_model(ident)
    family = getattr(radio, "_radio_family", "840")
    if family in ("940", "941"):
        return found in ("TK-940", "TK-941")
    return found == "TK-840"


def _read_exact(radio, amount):
    """Read exactly *amount* bytes, tolerating partial serial reads."""
    data = bytearray()
    deadline = time.monotonic() + TIMEOUT
    while len(data) < amount:
        try:
            chunk = radio.pipe.read(amount - len(data))
        except Exception as exc:
            raise errors.RadioError(
                "Error reading from %s: %s" % (_radio_label(radio), exc))
        if chunk:
            data.extend(chunk)
            deadline = time.monotonic() + TIMEOUT
            continue
        if time.monotonic() >= deadline:
            break
    if len(data) != amount:
        raise errors.RadioError(
            "Short read from %s: wanted %d bytes, got %d (%s)" %
            (_radio_label(radio), amount, len(data),
             util.hexprint(bytes(data))))
    return bytes(data)


def _send(radio, data):
    LOG.debug("%s -> %s", _radio_label(radio), util.hexprint(data))
    try:
        written = radio.pipe.write(data)
        if written is not None and written != len(data):
            raise IOError("short serial write: wanted %d bytes, wrote %d" %
                          (len(data), written))
    except Exception as exc:
        raise errors.RadioError(
            "Error writing to %s: %s" % (_radio_label(radio), exc))


def _recv(radio, amount):
    data = _read_exact(radio, amount)
    LOG.debug("%s <- %s", _radio_label(radio), util.hexprint(data))
    return data


def _set_serial(radio, baud):
    try:
        radio.pipe.baudrate = baud
        radio.pipe.bytesize = 8
        radio.pipe.parity = "N"
        radio.pipe.stopbits = 2
        radio.pipe.timeout = TIMEOUT
        # The genuine KPG-4 is an RS-232 interface and some versions derive
        # operating bias/power from modem-control lines. Assert both, but do
        # not enable hardware flow control.
        try:
            radio.pipe.xonxoff = False
            radio.pipe.rtscts = False
            radio.pipe.dsrdtr = False
            radio.pipe.dtr = True
            radio.pipe.rts = True
        except Exception:
            pass
    except Exception as exc:
        raise errors.RadioError(
            "Unable to configure the serial port for %s: %s" %
            (_radio_label(radio), exc))


def _flush_input(radio):
    try:
        radio.pipe.reset_input_buffer()
    except Exception:
        try:
            old_timeout = radio.pipe.timeout
            radio.pipe.timeout = 0.02
            while radio.pipe.read(256):
                pass
            radio.pipe.timeout = old_timeout
        except Exception:
            pass


def _set_transaction_state(radio, state):
    """Record the current clone-protocol transaction for recovery/cleanup."""
    radio._tk840_transaction = state


def _exit_program_mode(radio, quiet=False):
    """Best-effort exit from PROGRAM mode without hiding the original error.

    If a read/identity transaction was interrupted, first try the same harmless
    ACK-plus-ident probe used to resume an existing PROGRAM session. A serial
    write that failed before the complete frame was accepted is deliberately
    not followed by protocol bytes because the radio may still be collecting
    flash-ROM configuration payload data.
    """
    state = getattr(radio, "_tk840_transaction", None)
    if state == "write-uncertain":
        message = (
            "%s configuration write was interrupted before the complete frame "
            "was accepted; automatic PROGRAM-mode exit is unsafe. Power-cycle "
            "the radio before retrying." % _radio_label(radio))
        if quiet:
            LOG.error(message)
            return False
        raise errors.RadioError(message)

    old_timeout = getattr(radio.pipe, "timeout", TIMEOUT)
    try:
        _set_serial(radio, CLONE_BAUD)

        if state not in (None, "idle"):
            ident = _probe_existing_program_session(radio)
            if ident is not None:
                _send(radio, ACK)
                _set_transaction_state(radio, "idle")
                LOG.info(
                    "Resynchronized %s before leaving PROGRAM mode",
                    _radio_label(radio))

        _send(radio, b"E")
        radio.pipe.timeout = 0.20
        reply = radio.pipe.read(1)

        if not reply:
            LOG.debug("%s <- <no reply to E; accepted>", _radio_label(radio))
        else:
            LOG.debug("%s <- %s", _radio_label(radio), util.hexprint(reply))
            if reply != ACK:
                raise errors.RadioError(
                    "Unexpected %s exit reply (got %s)" %
                    (_radio_label(radio), util.hexprint(reply)))

        _set_transaction_state(radio, None)
        time.sleep(0.05)
        return True
    except Exception as exc:
        if quiet:
            LOG.warning(
                "Best-effort PROGRAM-mode exit for %s failed: %s",
                _radio_label(radio), exc)
            return False
        if isinstance(exc, errors.RadioError):
            raise
        raise errors.RadioError(
            "Unable to leave %s PROGRAM mode: %s" %
            (_radio_label(radio), exc))
    finally:
        try:
            radio.pipe.timeout = old_timeout
        except Exception:
            pass


def _probe_existing_program_session(radio):
    """Return an identity if the radio is already in PROGRAM mode at 9600."""
    previous_state = getattr(radio, "_tk840_transaction", None)
    try:
        _set_serial(radio, CLONE_BAUD)
        _flush_input(radio)
        # If a previous attempt stopped while the radio was waiting for the
        # host's final ACK, this completes that transaction. At an idle PROGRAM
        # prompt the extra ACK is harmless; then request identity.
        _send(radio, ACK)
        time.sleep(0.05)
        # Completing an interrupted read can itself produce a final ACK. Drop
        # that acknowledgement before requesting identity so it cannot become
        # the first byte of the seven-byte identity response.
        _flush_input(radio)
        _set_transaction_state(radio, "identity")
        _send(radio, b"\x02")
        ident = _recv(radio, 7)
        if _ident_model(ident):
            LOG.info(
                "%s was already in PROGRAM mode (ident=%s)",
                _radio_label(radio), ident.decode("ascii", "replace"))
            return ident
    except errors.RadioError:
        pass
    _set_transaction_state(radio, previous_state)
    return None


def _authenticate_known_password(radio, password):
    """Re-authenticate a resumed password-protected PROGRAM session."""
    _set_transaction_state(radio, "auth")
    _send(radio, b"\x04" + bytes(password))
    ack = _recv(radio, 1)
    if ack != ACK:
        raise errors.RadioError(
            "Stored programming password was rejected while resuming %s "
            "(got %s)" % (_radio_label(radio), util.hexprint(ack)))
    _set_transaction_state(radio, "idle")


def _accept_program_identity(radio, ident, operation):
    """ACK, validate and authenticate a PROGRAM identity."""
    found_model = _ident_model(ident)

    # KPG-25D expects an ACK after the seven-byte identity. ACK before deciding
    # whether the selected CHIRP model matches, otherwise the radio remains
    # waiting in the identity transaction and may ignore the exit command.
    _send(radio, ACK)
    _set_transaction_state(radio, "idle")

    if found_model is None:
        raise errors.RadioError(
            "%s returned an unrecognized PROGRAM identity %r (%s)" %
            (_radio_label(radio), ident, util.hexprint(ident)))

    if not _ident_matches_radio(radio, ident):
        selected = _radio_label(radio)
        raise errors.RadioError(
            "Selected %s, but the connected radio identifies as %s (%s)." %
            (selected, found_model, ident.decode("ascii", "replace")))

    if ident[-1:] == b"Y":
        password = getattr(radio, "_tk840_password", None)
        if password is not None:
            _authenticate_known_password(radio, password)
        else:
            LOG.warning(
                "%s reports password protection; retrieving E670-E679 before "
                "normal command-04 authentication", _radio_label(radio))
            radio._tk840_password = _recover_and_authenticate_password(
                radio, operation)
    return found_model


def _open_radio(radio, operation="download"):
    """Enter/resume PROGRAM mode, authenticate if needed, and identify."""
    ident = None
    entry_error = None
    radio._tk840_password = None
    _set_transaction_state(radio, None)

    try:
        _set_serial(radio, INITIAL_BAUD)
        _flush_input(radio)
        _send(radio, PROGRAM)
        ack = _recv(radio, 1)
        if ack != ACK:
            raise errors.RadioError(
                "%s did not ACK PROGRAM at 1200 baud (got %s)" %
                (_radio_label(radio), util.hexprint(ack)))

        # KPG-25D changes to 9600 8N2 immediately after PROGRAM/ACK.
        _set_transaction_state(radio, "idle")
        time.sleep(0.05)
        _set_serial(radio, CLONE_BAUD)
        time.sleep(0.05)
        _set_transaction_state(radio, "identity")
        _send(radio, b"\x02")
        ident = _recv(radio, 7)
    except errors.RadioError as exc:
        entry_error = exc
        ident = _probe_existing_program_session(radio)

    if ident is None:
        raise errors.RadioError(
            "No response from %s. Check that the radio is powered on, the "
            "programming cable is connected, and the correct serial port is "
            "selected. (%s)" % (_radio_label(radio), entry_error))

    try:
        _accept_program_identity(radio, ident, operation)
    except Exception:
        _exit_program_mode(radio, quiet=True)
        raise

    LOG.info(
        "Connected to %s, ident=%s",
        _radio_label(radio), ident.decode("ascii", "replace"))
    return ident


def _resync_program_session(radio, operation):
    """Recover an interrupted block transaction to an idle session."""
    if getattr(radio, "_tk840_transaction", None) == "write-uncertain":
        raise errors.RadioError(
            "%s write frame was interrupted; automatic protocol recovery is "
            "unsafe until the radio is power-cycled" % _radio_label(radio))

    ident = _probe_existing_program_session(radio)
    if ident is None:
        LOG.warning(
            "Could not resume %s at 9600 baud; attempting a fresh PROGRAM "
            "entry",
            _radio_label(radio))
        return _open_radio(radio, operation)

    _accept_program_identity(radio, ident, operation)
    radio._tk840_ident = ident
    LOG.info("Resynchronized %s PROGRAM session", _radio_label(radio))
    return ident


def _read_block_once(radio, addr):
    """Read one block without retrying; callers may resynchronize and retry."""
    request = struct.pack(">cHB", b"R", addr, BLOCK_SIZE)
    _set_transaction_state(radio, "read")
    _send(radio, request)

    # The radio normally sends an ACK before the W/address/length response
    # header. Be tolerant of a variant that starts directly with 'W'.
    first = _recv(radio, 1)
    if first == ACK:
        header = _recv(radio, 4)
    elif first == b"W":
        header = first + _recv(radio, 3)
    else:
        raise errors.RadioError(
            "Bad %s response at %04X: expected ACK or W, got %s" %
            (_radio_label(radio), addr, util.hexprint(first)))

    cmd, raddr, length = struct.unpack(">cHB", header)
    if cmd != b"W" or raddr != addr or length != BLOCK_SIZE:
        raise errors.RadioError(
            "Bad %s read header at %04X: cmd=%r addr=%04X len=%02X" %
            (_radio_label(radio), addr, cmd, raddr, length))

    data = _recv(radio, BLOCK_SIZE)
    _send(radio, ACK)
    ack = _recv(radio, 1)
    if ack != ACK:
        raise errors.RadioError(
            "%s did not ACK read block %04X (got %s)" %
            (_radio_label(radio), addr, util.hexprint(ack)))
    _set_transaction_state(radio, "idle")
    return data


def _read_block(radio, addr, operation="download", attempts=3):
    """Read a block, resynchronizing the PROGRAM session before each retry."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return _read_block_once(radio, addr)
        except errors.RadioError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            LOG.warning(
                "%s block %04X read failed on attempt %d/%d: %s",
                _radio_label(radio), addr, attempt, attempts, exc)
            _resync_program_session(radio, operation)
    raise last_error


def _firmware_flags(tag):
    """Decode the Toolkit firmware feature bits."""
    if (isinstance(tag, bool) or not isinstance(tag, int) or
            not 0 <= tag <= 0xFF):
        return 0
    if tag & FIRMWARE_TAG_PREFIX_MASK != FIRMWARE_TAG_PREFIX:
        return 0
    return tag & ~FIRMWARE_TAG_PREFIX_MASK


def _probe_firmware_features(radio):
    """Read only the firmware metadata block, never inspect executable code.

    None means the probe failed; zero means a successful read with no valid
    Toolkit metadata tag.
    """
    try:
        block = _read_block(
            radio, FIRMWARE_FEATURE_BLOCK,
            operation="firmware feature probe", attempts=2)
    except errors.RadioError as exc:
        LOG.warning("%s firmware feature probe failed: %s",
                    _radio_label(radio), exc)
        return None
    raw = block[FIRMWARE_FEATURE_ADDR - FIRMWARE_FEATURE_BLOCK]
    return (raw if raw & FIRMWARE_TAG_PREFIX_MASK == FIRMWARE_TAG_PREFIX
            else 0)


def _select_firmware_patch_profile(fingerprint_block, family):
    """Identify a known firmware revision from its DF00 fingerprint block."""
    fingerprint = hashlib.sha256(fingerprint_block).hexdigest()
    for revision, profile in FIRMWARE_PATCH_PROFILES.items():
        if (profile["family"] == family and
                profile["fingerprint"] == fingerprint):
            return revision
    return None


def _matches_dealer_pattern(actual, pattern):
    """Exact routine match except for the relocated two-byte CALL target."""
    return (actual is not None and len(actual) == len(pattern) and
            actual[0:1] == pattern[0:1] and actual[3:] == pattern[3:])


def _classify_tot_code(profile, read_site, revision):
    """Recognize complete stock, Enhanced TOT, legacy, or 10x TEST images."""
    regions = profile.get("tot_regions")
    if not regions:
        return "unsupported"
    values = []
    for index, (addr, stock, patched) in enumerate(regions):
        actual = read_site(addr, len(stock))
        if actual is None:
            return "unverified"
        speed_index = (37 if index == 0 else 11)
        if actual == stock:
            values.append("stock")
        elif actual == patched:
            values.append("patched")
        elif (index < 2 and len(patched) > speed_index and
              patched[speed_index] == 0x0A and
              actual[speed_index] == 0x01 and
              actual[:speed_index] == patched[:speed_index] and
              actual[speed_index + 1:] == patched[speed_index + 1:]):
            # Same exact-byte test as Toolkit: only two accelerated timer
            # bytes differ, and AB00's third TOT region remains enhanced.
            values.append("test10x")
        elif (profile.get("legacy_tot") and len(stock) > 21 and
              stock[20:22] == b"\x81\x01" and
              actual[:20] == stock[:20] and
              actual[20:22] == b"\x00\x00" and
              actual[22:] == stock[22:]):
            values.append("legacy")
        else:
            values.append("modified")
    if all(value == "stock" for value in values):
        return "stock"
    if all(value == "patched" for value in values):
        return "ff-off"
    if (len(values) >= 2 and values[:2] == ["test10x", "test10x"] and
            all(value == "patched" for value in values[2:])):
        return "test10x"
    if values.count("legacy") == 1 and all(
            value in ("stock", "legacy") for value in values):
        return "legacy"
    return "modified"


def _classify_plus25_code(read_site, revision):
    sites = _PLUS25_SITES.get(revision)
    if not sites:
        return "unsupported"
    values = []
    for addr in sites:
        actual = read_site(addr, len(_PLUS25_STOCK))
        if actual is None:
            return "unverified"
        if actual == _PLUS25_STOCK:
            values.append("stock")
        elif actual in (_PLUS25_SHORT, _PLUS25_FULL):
            values.append("patched")
        else:
            values.append("modified")
    if all(value == "stock" for value in values):
        return "stock"
    if all(value == "patched" for value in values):
        return "patched"
    return "modified"


def _classify_tx_code(profile, read_site, revision):
    if profile["family"] == "840":
        return "not-applicable"
    tx = read_site(profile["tx_addr"], 16)
    dealer = read_site(profile["dealer_addr"], len(_DEALER_TX_STOCK))
    plus25 = _classify_plus25_code(read_site, revision)
    if tx is None or dealer is None or plus25 == "unverified":
        return "unverified"
    tx_fixed = tx == profile["fix_tx"]
    tx_stock = tx == profile["stock_tx"]
    dealer_fixed = _matches_dealer_pattern(dealer, _DEALER_TX_PATCHED)
    dealer_stock = _matches_dealer_pattern(dealer, _DEALER_TX_STOCK)
    if tx_fixed and dealer_fixed and plus25 == "stock":
        return "patched"
    if tx_stock and dealer_stock and plus25 == "patched":
        return "plus25"
    if tx_stock and dealer_stock and plus25 == "stock":
        return "stock"
    return "partial"


def _probe_firmware_code(radio):
    """Read only firmware-identification and known patch-site blocks.

    The probe is strictly read-only and optional. A failed block read results
    in 'unverified' without aborting CHIRP's configuration download.
    """
    family = ("94x" if radio._radio_family in ("940", "941")
              else radio._radio_family)
    try:
        fingerprint_block = _read_block(
            radio, FIRMWARE_FINGERPRINT_BLOCK,
            operation="firmware fingerprint probe", attempts=2)
    except errors.RadioError as exc:
        LOG.warning("%s firmware identification read failed: %s",
                    _radio_label(radio), exc)
        return {"revision": None, "tx": "unverified", "tot": "unverified"}
    revision = _select_firmware_patch_profile(fingerprint_block, family)
    if revision is None:
        return {"revision": None, "tx": "unknown", "tot": "unknown"}
    profile = FIRMWARE_PATCH_PROFILES[revision]
    cache = {FIRMWARE_FINGERPRINT_BLOCK: fingerprint_block}

    def read_site(addr, length):
        base = addr & ~(BLOCK_SIZE - 1)
        final = (addr + length - 1) & ~(BLOCK_SIZE - 1)
        chunks = []
        for address in range(base, final + 1, BLOCK_SIZE):
            if address not in cache:
                try:
                    cache[address] = _read_block(
                        radio, address, operation="firmware patch probe",
                        attempts=2)
                except errors.RadioError as exc:
                    LOG.warning("%s firmware patch block %04X unavailable: %s",
                                _radio_label(radio), address, exc)
                    cache[address] = None
            if cache[address] is None:
                return None
            chunks.append(cache[address])
        return b"".join(chunks)[addr - base:addr - base + length]
    tot = _classify_tot_code(profile, read_site, revision)
    if tot == "test10x":
        LOG.warning("%s has 10x TOT TEST firmware installed. Restore "
                    "normal-speed firmware before regular operation.",
                    _radio_label(radio))
    return {"revision": revision,
            "tx": _classify_tx_code(profile, read_site, revision),
            "tot": tot}


def _firmware_feature_status(tag):
    if tag is None:
        return "Not read"
    if tag & FIRMWARE_TAG_PREFIX_MASK != FIRMWARE_TAG_PREFIX:
        return "None"
    flags = _firmware_flags(tag)
    names = []
    if flags & FIRMWARE_FEATURE_TX_FIX:
        names.append("Enhanced TX Patch")
    if flags & FIRMWARE_FEATURE_TOT_OFF:
        names.append("Enhanced TOT Patch")
    unknown = flags & ~(FIRMWARE_FEATURE_TX_FIX | FIRMWARE_FEATURE_TOT_OFF)
    if unknown:
        names.append("Unknown flags")
    return ", ".join(names) if names else "None"


def _firmware_revision_status_text(probe):
    if not isinstance(probe, dict):
        return "Not verified"
    rev = probe.get("revision")
    profile = FIRMWARE_PATCH_PROFILES.get(rev)
    if profile:
        model = "840" if profile["family"] == "840" else "940/941"
        return "TK-%s %s (fingerprint verified)" % (model, rev)
    return "Unknown firmware revision (fingerprint not recognized)"


def _firmware_tx_status_text(tag, family, probe=None):
    if family == "840":
        return "Stock: programmed TX field used"
    code = probe.get("tx") if isinstance(probe, dict) else None
    if code == "patched":
        return "Enhanced TX Patch"
    if code == "plus25":
        return "+25 MHz Offset Patch"
    if code in ("partial", "legacy"):
        return "Modified or partial"
    if code == "stock":
        return "Stock fixed offset"
    if code in ("unverified", "unknown"):
        return "Not verified"
    if tag is None:
        return "Not verified"
    return "Not verified"


def _firmware_tot_status_text(tag, probe=None):
    code = probe.get("tot") if isinstance(probe, dict) else None
    if code == "ff-off":
        return "Enhanced TOT Patch"
    if code == "test10x":
        return "WARNING: 10x TOT TEST MODE"
    if code == "legacy":
        return "Legacy TOT Bypass"
    if code == "stock":
        return "Stock"
    if code == "modified":
        return "Modified or unknown"
    if code in ("unsupported", "unknown", "unverified"):
        return "Not verified"
    if tag is None:
        return "Not verified"
    return "Not verified"


def _firmware_patch_advice(family, probe):
    """Mention optional patches only when relevant bytes are verified."""
    if (not isinstance(probe, dict) or
            probe.get("revision") not in FIRMWARE_PATCH_PROFILES):
        return None

    tx = probe.get("tx")
    tot = probe.get("tot")
    if tot == "test10x":
        return "Restore normal-speed firmware after diagnostic testing"

    tx_available = family in ("940", "941") and tx in ("stock", "plus25")
    tot_available = tot == "stock"
    if tx_available and tot_available:
        return "Optional Enhanced TX and Enhanced TOT patches available"
    if tx_available:
        return "Optional Enhanced TX Patch available"
    if tot_available:
        return "Optional Enhanced TOT Patch available"
    if tot == "legacy":
        return "Enhanced TOT is an optional alternative to Legacy TOT Bypass"
    return None


def _tot_test10x_detected(probe):
    """Use accelerated labels only when actual firmware bytes are verified."""
    return isinstance(probe, dict) and probe.get("tot") == "test10x"


def _tot_setting_value(raw, accelerated=False):
    """Show normal values, optionally annotated with 10x actual duration."""
    choices = (TOT_TEST10X_CHOICES if accelerated else TOT_CHOICES) + [
        TOT_OFF_LABEL]
    if raw == 0xFF:
        return RadioSettingValueList(
            choices, current_index=len(choices) - 1)
    return _list_value_with_unknown(choices, raw)


def _tot_choice_to_raw(value, accelerated=False):
    """Map the visible duration to the same radio codeplug byte."""
    value = str(value)
    if value.startswith("Unknown"):
        return None
    if value in ("OFF", TOT_OFF_LABEL):
        return 0xFF
    if accelerated:
        if value in TOT_TEST10X_CHOICES:
            return TOT_TEST10X_CHOICES.index(value)
        raise errors.InvalidValueError(
            "10x TEST timer must use a listed duration or Off")
    try:
        minutes, secs = [int(part) for part in value.split(":", 1)]
    except (TypeError, ValueError):
        raise errors.InvalidValueError(
            "TX Time-out Timer must use M:SS format or OFF")
    seconds = minutes * 60 + secs
    if seconds < 15 or seconds > 600 or seconds % 15 or not 0 <= secs < 60:
        raise errors.InvalidValueError(
            "TX Time-out Timer must be 0:15..10:00 in 15-second increments")
    return (seconds // 15) - 1


def _write_block(radio, addr, payload):
    """Write one 0x80-byte block using one complete KPG-25D frame."""
    if len(payload) != BLOCK_SIZE:
        raise errors.RadioError(
            "Internal configuration write block size error")

    frame = (b"W" + struct.pack(">H", addr) +
             bytes((BLOCK_SIZE,)) + bytes(payload))
    _set_transaction_state(radio, "write-frame")
    try:
        _send(radio, frame)
    except errors.RadioError:
        # Do not send recovery bytes after a possibly partial flash-ROM
        # payload.
        _set_transaction_state(radio, "write-uncertain")
        raise

    _set_transaction_state(radio, "write-ack")
    ack = _recv(radio, 1)
    if ack != ACK:
        raise errors.RadioError(
            "%s did not ACK write block %04X (got %s)" %
            (_radio_label(radio), addr, util.hexprint(ack)))
    _set_transaction_state(radio, "idle")


def _password_from_first_block(block):
    """Extract the literal ten-byte password from an E600-E67F block."""
    if len(block) != BLOCK_SIZE:
        raise errors.RadioError("Internal password block size error")
    start = PASSWORD_OFFSET - (PASSWORD_BLOCK_ADDR - RADIO_BASE)
    end = start + PASSWORD_LENGTH
    if not (0 <= start < end <= BLOCK_SIZE):
        raise errors.RadioError("Internal password-field geometry error")
    return bytes(block[start:end])


def _recover_and_authenticate_password(radio, operation):
    """Recover the stored password, then authenticate with command 04h."""
    status = chirp_common.Status()
    status.cur = 1
    status.max = 3
    status.msg = ("%s is password protected; retrieving password..." %
                  _radio_label(radio))
    radio.status_fn(status)

    try:
        first_block = _read_block_once(radio, PASSWORD_BLOCK_ADDR)
    except errors.RadioError as exc:
        raise errors.RadioError(
            "Radio reports password protection, but the driver could not read "
            "E600-E67F before authentication: %s" % exc)

    password = _password_from_first_block(first_block)
    if password == PASSWORD_ERASED:
        raise errors.RadioError(
            "Radio reports password protection, but E670-E679 are all FF. "
            "The protection state does not match the decoded mask-ROM format.")

    status.cur = 2
    status.msg = "Password retrieved; logging in normally..."
    radio.status_fn(status)

    _set_transaction_state(radio, "auth")
    _send(radio, b"\x04" + password)
    ack = _recv(radio, 1)
    if ack != ACK:
        raise errors.RadioError(
            "Recovered programming password was rejected by command 04h (got "
            "%s)" %
            util.hexprint(ack))
    _set_transaction_state(radio, "idle")

    status.cur = 3
    status.msg = (
        "Password accepted; reading radio..." if operation == "download"
        else "Password accepted; preparing upload...")
    radio.status_fn(status)
    LOG.info(
        "Programming password recovered from E670-E679 and accepted by "
        "command 04h")
    return password


def _read_image_in_session(radio, status_message):
    """Read the complete 0x1800-byte image while already in PROGRAM mode."""
    status = chirp_common.Status()
    status.cur = 0
    status.max = MEM_SIZE // BLOCK_SIZE
    status.msg = status_message
    radio.status_fn(status)

    image = bytearray()
    total_blocks = MEM_SIZE // BLOCK_SIZE
    password_prefix = ("Reading w/password block"
                       if getattr(radio, "_tk840_password", None) is not None
                       else "Reading block")
    for block, addr in enumerate(range(RADIO_BASE, RADIO_END, BLOCK_SIZE)):
        status.msg = "%s %d/%d..." % (password_prefix, block + 1, total_blocks)
        radio.status_fn(status)
        try:
            image.extend(_read_block(radio, addr))
        except errors.RadioError as exc:
            raise errors.RadioError(
                "%s read failed at block %d/%d, address %04X: %s" %
                (_radio_label(radio), block + 1,
                 MEM_SIZE // BLOCK_SIZE, addr, exc))
        status.cur = block + 1
        radio.status_fn(status)

    if len(image) != MEM_SIZE:
        raise errors.RadioError(
            "%s clone image has unexpected size %d" %
            (_radio_label(radio), len(image)))
    return bytes(image)


def _upload_blocks_in_session(radio, target):
    """Read, compare, write, and verify one configuration block at a time."""
    status = chirp_common.Status()
    total_blocks = MEM_SIZE // BLOCK_SIZE
    status.cur = 0
    status.max = total_blocks
    status.msg = "Uploading %s radio configuration..." % _radio_label(radio)
    radio.status_fn(status)

    password_tag = (" w/password"
                    if getattr(radio, "_tk840_password", None) is not None
                    else "")
    changed_blocks = 0
    retries = 0

    for block, addr in enumerate(range(RADIO_BASE, RADIO_END, BLOCK_SIZE)):
        block_num = block + 1
        pos = block * BLOCK_SIZE
        wanted = bytes(target[pos:pos + BLOCK_SIZE])

        status.msg = "Checking%s block %d/%d..." % (
            password_tag, block_num, total_blocks)
        radio.status_fn(status)
        try:
            current = _read_block(radio, addr, operation="upload")
        except errors.RadioError as exc:
            raise errors.RadioError(
                "%s comparison read failed at block %d/%d, address %04X: %s" %
                (_radio_label(radio), block_num, total_blocks, addr, exc))

        if current == wanted:
            status.cur = block_num
            status.msg = "Block %d/%d unchanged" % (block_num, total_blocks)
            radio.status_fn(status)
            continue

        changed_blocks += 1
        verified = False
        for attempt in range(1, 4):
            status.msg = (
                "Writing%s block %d/%d, attempt %d/3..." %
                (password_tag, block_num, total_blocks, attempt))
            radio.status_fn(status)

            try:
                _write_block(radio, addr, wanted)
            except errors.RadioError as exc:
                if (getattr(radio, "_tk840_transaction", None) ==
                        "write-uncertain"):
                    raise errors.RadioError(
                        "%s write failed at block %d/%d, address %04X. The "
                        "serial write may have stopped inside the flash-ROM "
                        "payload; power-cycle the radio before retrying. "
                        "(%s)" %
                        (_radio_label(radio), block_num, total_blocks,
                         addr, exc))

                LOG.warning(
                    "%s block %04X write/ACK failed on attempt %d/3: %s",
                    _radio_label(radio), addr, attempt, exc)

                # A missing ACK does not prove the flash-ROM write failed.
                # Resume the protocol, read this block, and only rewrite if it
                # still differs.
                try:
                    _resync_program_session(radio, "upload")
                    observed = _read_block(
                        radio, addr, operation="upload", attempts=3)
                except errors.RadioError as recovery_exc:
                    raise errors.RadioError(
                        "%s could not recover after the write error at block "
                        "%d/%d, address %04X: %s" %
                        (_radio_label(radio), block_num, total_blocks, addr,
                         recovery_exc))

                if observed == wanted:
                    LOG.warning(
                        "%s block %04X already matches after lost/invalid "
                        "ACK; not rewriting flash ROM",
                        _radio_label(radio), addr)
                    verified = True
                    break

                if attempt < 3:
                    retries += 1
                    continue
                raise errors.RadioError(
                    "%s write failed at block %d/%d, address %04X after 3 "
                    "attempts: %s" %
                    (_radio_label(radio), block_num, total_blocks, addr, exc))

            status.msg = (
                "Verifying%s block %d/%d, attempt %d/3..." %
                (password_tag, block_num, total_blocks, attempt))
            radio.status_fn(status)
            try:
                verify = _read_block(
                    radio, addr, operation="upload", attempts=3)
            except errors.RadioError as exc:
                # Do not blindly rewrite a block that may already be correct if
                # communications are too unreliable to verify it.
                raise errors.RadioError(
                    "%s verification read failed at block %d/%d, "
                    "address %04X: %s" %
                    (_radio_label(radio), block_num, total_blocks, addr, exc))

            if verify == wanted:
                if attempt > 1:
                    LOG.warning(
                        "%s block %04X succeeded on attempt %d/3",
                        _radio_label(radio), addr, attempt)
                verified = True
                break

            if attempt < 3:
                retries += 1
                LOG.warning(
                    "%s block %04X verification mismatch on attempt %d/3",
                    _radio_label(radio), addr, attempt)
                continue

            mismatches = [
                i for i, (actual, expected) in
                enumerate(zip(verify, wanted)) if actual != expected]
            shown = ", ".join(
                "+%02X(%02X!=%02X)" %
                (i, verify[i], wanted[i]) for i in mismatches[:12])
            more = " ..." if len(mismatches) > 12 else ""
            LOG.error(
                "%s block %04X failed verification after 3 attempts: %s%s",
                _radio_label(radio), addr, shown, more)
            raise errors.RadioError(
                "%s block %d/%d failed verification after 3 attempts" %
                (_radio_label(radio), block_num, total_blocks))

        if not verified:
            raise errors.RadioError(
                "%s block %d/%d was not verified" %
                (_radio_label(radio), block_num, total_blocks))

        status.cur = block_num
        status.msg = "Verified block %d/%d (%04X)" % (
            block_num, total_blocks, addr)
        radio.status_fn(status)

    if changed_blocks:
        status.msg = (
            "Upload complete: %d block(s) changed, %d retry/retries" %
            (changed_blocks, retries))
    else:
        status.msg = (
            "Upload complete: radio already matches edited configuration")
    radio.status_fn(status)
    return changed_blocks, retries


def do_upload(radio):
    """Read, compare, write, and verify configuration blocks in sequence."""
    model = _radio_label(radio)
    target = bytes(radio._mmap.get_packed())
    if len(target) != MEM_SIZE:
        raise errors.RadioError("%s image has unexpected size %d" %
                                (model, len(target)))

    cleanup_needed = True
    try:
        try:
            radio._tk840_ident = _open_radio(radio, "upload")
        except errors.RadioError as exc:
            raise errors.RadioError(
                "Unable to enter %s programming mode for upload: %s" %
                (model, exc))

        changed, retries = _upload_blocks_in_session(radio, target)
        if changed:
            LOG.info(
                "%s upload verified: %d changed block(s) written, %d "
                "retry/retries",
                model, changed, retries)
        else:
            LOG.info(
                "%s upload: radio already matches the edited configuration",
                model)

        _exit_program_mode(radio)
        cleanup_needed = False
    finally:
        if (cleanup_needed and
                getattr(radio, "_tk840_transaction", None) is not None):
            _exit_program_mode(radio, quiet=True)


def do_download(radio):
    cleanup_needed = True
    try:
        try:
            radio._tk840_ident = _open_radio(radio, "download")
        except errors.RadioError as exc:
            raise errors.RadioError(
                "Unable to enter %s programming mode for download: %s" %
                (_radio_label(radio), exc))

        image = _read_image_in_session(
            radio, "Reading %s radio configuration..." % _radio_label(radio))

        radio._firmware_tag = _probe_firmware_features(radio)
        radio._firmware_probe = _probe_firmware_code(radio)
        LOG.info("%s firmware metadata byte: %s; metadata: %s; code probe: %s",
                 _radio_label(radio),
                 ("unreadable" if radio._firmware_tag is None else
                  "0x%02X" % radio._firmware_tag),
                 _firmware_feature_status(radio._firmware_tag),
                 radio._firmware_probe)

        _exit_program_mode(radio)
        cleanup_needed = False

        LOG.info("Downloaded complete %s image (%d bytes)",
                 _radio_label(radio), len(image))
        return memmap.MemoryMapBytes(image)
    finally:
        if (cleanup_needed and
                getattr(radio, "_tk840_transaction", None) is not None):
            _exit_program_mode(radio, quiet=True)


def _u16le(data, offset):
    return data[offset] | (data[offset + 1] << 8)


def _clean_name(raw):
    # KPG commonly uses NUL/FF for unused characters.  Preserve ordinary
    # printable ASCII and turn those two fill values into spaces.
    chars = []
    for value in raw:
        if value in (0x00, 0xFF):
            chars.append(" ")
        elif 0x20 <= value <= 0x7E:
            chars.append(chr(value))
        else:
            chars.append("?")
    return "".join(chars).rstrip()


def _decode_frequency(word):
    code = word & 0x7FFF
    if code == 0x7FFF:
        return None
    return FREQ_BASE_HZ + code * FREQ_STEP_HZ


def _encode_frequency(freq_hz, old_word):
    """Encode on the TK-840's fixed 12.5-kHz PLL raster.

    User-entered frequencies that fall between PLL points are rounded to the
    nearest representable frequency. Bit 15 is unused by the TK-840 firmware
    for conventional RX/TX frequency operation and is preserved by default
    for valid frequencies. An absent frequency is written as 7FFF.
    """
    if freq_hz is None:
        # Canonical disabled-frequency word, irrespective of old bit 15.
        return 0x7FFF

    delta = int(freq_hz) - FREQ_BASE_HZ
    if delta < 0:
        raise errors.InvalidValueError(
            "TK-840 frequency %0.6f MHz is below the encodable range" %
            (freq_hz / 1_000_000.0))

    code = (delta + FREQ_STEP_HZ // 2) // FREQ_STEP_HZ
    if code < 0 or code >= 0x7FFF:
        raise errors.InvalidValueError(
            "TK-840 frequency %0.6f MHz is outside the encodable range" %
            (freq_hz / 1_000_000.0))
    rounded = FREQ_BASE_HZ + code * FREQ_STEP_HZ
    if rounded != int(freq_hz):
        LOG.info(
            "Rounding TK-840 frequency %.6f MHz to %.6f MHz on the 12.5-kHz "
            "raster",
            freq_hz / 1_000_000.0, rounded / 1_000_000.0)
    return (old_word & FREQ_UNUSED_MASK) | int(code)


def _decode_frequency_941(word):
    """Decode a stock TK-940/TK-941 conventional frequency word.

    The firmware ignores bit 15. The lower 15 bits count 12.5 kHz steps
    above 851 MHz, and 0x7FFF (with either high-bit value) means absent.
    """
    code = word & 0x7FFF
    if code == 0x7FFF:
        return None
    return FREQ_BASE_94X_HZ + code * FREQ_STEP_HZ


def _encode_frequency_941(freq_hz, old_word):
    """Encode 94x 851 MHz raster, keeping bit 15 for valid frequencies.

    For an absent frequency, use the canonical 7FFF word.
    """
    if freq_hz is None:
        # Canonical disabled-frequency word, irrespective of old bit 15.
        return 0x7FFF

    delta = int(freq_hz) - FREQ_BASE_94X_HZ
    if delta < 0:
        raise errors.InvalidValueError(
            "TK-940/TK-941 frequency %0.6f MHz is below the 851 MHz "
            "conventional-channel base" % (freq_hz / 1_000_000.0))

    code = (delta + FREQ_STEP_HZ // 2) // FREQ_STEP_HZ
    if code < 0 or code >= 0x7FFF:
        raise errors.InvalidValueError(
            "TK-940/TK-941 frequency %0.6f MHz is outside the encodable "
            "range" % (freq_hz / 1_000_000.0))
    rounded = FREQ_BASE_94X_HZ + code * FREQ_STEP_HZ
    if rounded != int(freq_hz):
        LOG.info(
            "Rounding TK-940/TK-941 frequency %.6f MHz to %.6f MHz on "
            "the 12.5-kHz raster", freq_hz / 1_000_000.0,
            rounded / 1_000_000.0)
    return (old_word & FREQ_UNUSED_MASK) | int(code)


def _reverse3(value):
    """Reverse the low three bits used for one octal DCS digit."""
    value = int(value) & 0x07
    return ((value & 0x01) << 2) | (value & 0x02) | ((value & 0x04) >> 2)


def _decode_tone(word):
    """Return CHIRP split-tone tuple: (mode, value, polarity)."""
    if word in (0xFFFF, 0x7FFF) or (word & 0x7FFF) == 0x7FFF:
        return ("", None, None)

    tag = word & 0xF000
    if tag == 0xC000:
        tone = (word & 0x0FFF) / 10.0
        return ("Tone", tone, None)

    if tag == 0xE000:
        payload = word & 0x0FFF
        if payload & 0x001:
            polarity = "N"
        else:
            polarity = "R"
            payload ^= 0x0FFF

        d1 = _reverse3((payload >> 3) & 0x07)
        d2 = _reverse3((payload >> 6) & 0x07)
        d3 = _reverse3((payload >> 9) & 0x07)
        code = d1 * 100 + d2 * 10 + d3
        return ("DTCS", code, polarity)

    LOG.warning("Unknown Kenwood tone word %04X", word)
    return ("", None, None)


def _encode_tone(tone):
    """Encode CHIRP split-tone tuple (mode, value, polarity)."""
    mode, value, polarity = tone
    if not mode:
        return 0xFFFF

    if mode == "Tone":
        raw = int(round(float(value) * 10.0))
        if raw < 0 or raw > 0x0FFF:
            raise errors.InvalidValueError(
                "Invalid Kenwood CTCSS tone %r" % value)
        return 0xC000 | raw

    if mode == "DTCS":
        code = int(value)
        d1 = code // 100
        d2 = (code // 10) % 10
        d3 = code % 10
        if any(d < 0 or d > 7 for d in (d1, d2, d3)):
            raise errors.InvalidValueError(
                "Invalid Kenwood DCS code %03d" % code)

        payload = 0x001
        payload |= _reverse3(d1) << 3
        payload |= _reverse3(d2) << 6
        payload |= _reverse3(d3) << 9
        if polarity == "R":
            payload ^= 0x0FFF
        elif polarity not in ("N", None):
            raise errors.InvalidValueError(
                "Unsupported Kenwood DCS polarity %r" % polarity)
        return 0xE000 | payload

    raise errors.InvalidValueError(
        "Unsupported Kenwood tone mode %r" % mode)


def _put_u16le(buf, offset, value):
    buf[offset:offset + 2] = struct.pack("<H", value & 0xFFFF)


def _encode_name(name):
    # KPG-25D accepts lower-case at entry, but the TK-840 display does not
    # render it. Accept lower-case in CHIRP for convenience and normalize the
    # stored eight-byte name to upper-case.
    text = str(name)[:8].replace(".", ",")
    bad = sorted(set(ch for ch in text if ch not in STORED_NAME_CHARSET))
    if bad:
        raise errors.InvalidValueError(
            "Name contains unsupported display character(s): %s" %
            " ".join(repr(ch) for ch in bad))
    text = text.upper()
    raw = text.encode("ascii", "strict")
    return raw + (b"\x00" * (8 - len(raw)))


def _decode_program_password(raw):
    """Return the editable text portion of a ten-byte FF-padded password."""
    raw = bytes(raw)
    if raw == PASSWORD_ERASED:
        return ""
    raw = raw.split(b"\xFF", 1)[0]
    return "".join(chr(value) if 0x20 <= value <= 0x7E else "?"
                   for value in raw)


def _encode_program_password(value):
    """Encode a 1..10 character password using KPG-25D's FF padding."""
    text = str(value)
    if not text:
        raise errors.InvalidValueError(
            "Enter a password before enabling programming-password protection")
    if len(text) > PASSWORD_LENGTH:
        raise errors.InvalidValueError(
            "Programming password is limited to 10 characters")
    bad = sorted(set(ch for ch in text if ch not in PASSWORD_CHARSET))
    if bad:
        raise errors.InvalidValueError(
            "Programming password contains unsupported character(s): %s" %
            " ".join(repr(ch) for ch in bad))
    raw = text.encode("ascii", "strict")
    return raw + (b"\xFF" * (PASSWORD_LENGTH - len(raw)))


def _ro_string(name, label, value, maximum=48):
    setting_value = RadioSettingValueString(0, maximum, str(value))
    setting_value.set_mutable(False)
    return RadioSetting(name, label, setting_value)


def _ro_bool(name, label, value):
    setting_value = RadioSettingValueBoolean(bool(value))
    setting_value.set_mutable(False)
    return RadioSetting(name, label, setting_value)


def _with_doc(setting, text):
    """Attach CHIRP hover/help documentation and return the setting."""
    setting.set_doc(text)
    return setting


def _help_string(name, label, text):
    """Create a read-only help row with the full explanation as its tooltip."""
    setting = _ro_string(name, label, text, max(80, len(text)))
    setting.set_doc(text)
    return setting


def _list_value_with_unknown(choices, raw, base=0):
    """Build a list value without silently changing an unexpected raw byte."""
    local = list(choices)
    index = int(raw) - int(base)
    if 0 <= index < len(local):
        return RadioSettingValueList(local, current_index=index)
    local.append("Unknown (0x%02X)" % (int(raw) & 0xFF))
    return RadioSettingValueList(local, current_index=len(local) - 1)


def _system_offset(ptr_word):
    # KPG-25D masks the two high flag bits and converts its internal system
    # pointer to an offset in the cloned image by subtracting 0x2600.
    return (ptr_word & 0x3FFF) - SYSTEM_DATA_PTR_BASE


def _system_block_size(group_count):
    return CONV_HEADER_SIZE + int(group_count) * CONV_RECORD_SIZE


def _system_pointer_for_offset(offset, scan_included=True):
    low = SYSTEM_DATA_PTR_BASE + offset
    if low < 0 or low > 0x3FFF:
        raise errors.InvalidValueError(
            "System-data offset 0x%04X cannot be encoded" % offset)
    if scan_included:
        return SYSTEM_SCAN_INCLUDE_PTR_MASK | low
    return low


def _set_system_lockout_included(image, system_number, included):
    """Set conventional System Lockout state in pointer bit 14.

    ``included`` means the System is enabled/unlocked. The setting is shared
    by every channel in the System and maps to CHIRP's standard Skip column.
    """
    output = bytearray(image)
    ptr_offset = SYSTEM_PTR_BASE + (int(system_number) - 1) * 2
    pointer = _u16le(output, ptr_offset)
    if pointer & 0x8000:
        raise errors.InvalidValueError(
            "System %d is not programmed" % system_number)
    if included:
        pointer |= SYSTEM_SCAN_INCLUDE_PTR_MASK
    else:
        pointer &= ~SYSTEM_SCAN_INCLUDE_PTR_MASK
    _put_u16le(output, ptr_offset, pointer)
    return bytes(output)


def _set_system_scan_list(image, system_number, included):
    """Set conventional Scan List membership in header byte 0 bit 7.

    The encoding is active-low: clear = Yes/in Scan List, set = No.
    """
    output = bytearray(image)
    ptr_offset = SYSTEM_PTR_BASE + (int(system_number) - 1) * 2
    pointer = _u16le(output, ptr_offset)
    if pointer & 0x8000:
        raise errors.InvalidValueError(
            "System %d is not programmed" % system_number)
    system_offset = _system_offset(pointer)
    if system_offset < 0 or system_offset + CONV_HEADER_SIZE > MEM_SIZE:
        raise errors.InvalidValueError(
            "System %d has an invalid header pointer" % system_number)
    pos = system_offset + SYSTEM_FLAGS
    if included:
        output[pos] &= ~SYSTEM_SCAN_LIST_EXCLUDE_MASK & 0xFF
    else:
        output[pos] |= SYSTEM_SCAN_LIST_EXCLUDE_MASK
    return bytes(output)


def _set_system_name(image, system_number, name):
    """Set the shared eight-character conventional System name."""
    output = bytearray(image)
    ptr_offset = SYSTEM_PTR_BASE + (int(system_number) - 1) * 2
    pointer = _u16le(output, ptr_offset)
    if pointer & 0x8000:
        raise errors.InvalidValueError(
            "System %d is not programmed" % system_number)
    system_offset = _system_offset(pointer)
    if system_offset < 0 or system_offset + CONV_HEADER_SIZE > MEM_SIZE:
        raise errors.InvalidValueError(
            "System %d has an invalid header pointer" % system_number)
    name_offset = system_offset + SYSTEM_NAME
    output[name_offset:name_offset + SYSTEM_NAME_LENGTH] = _encode_name(name)
    return bytes(output)


def _validate_dynamic_layout(image, description):
    """Validate the TK-840 variable-length conventional System layout.

    The 32-entry pointer/count tables may contain holes. Active conventional
    System blocks may start anywhere in the packed System-data pool, provided
    they do not overlap and each block fits completely inside the codeplug.
    Structural editing is intentionally disabled for mixed LTR/conventional
    images because the LTR block format has not been mapped yet.
    """
    image = bytes(image)
    if len(image) != MEM_SIZE:
        raise errors.RadioError("%s has the wrong image size" % description)

    channels, systems = _parse_conventional_channels(image)
    active = []
    ranges = []
    active_count = 0

    for system in systems:
        if system["unused"]:
            continue
        active_count += 1
        count = int(system["count"])
        # The count byte can contain 251..255 in malformed images. The radio's
        # usable per-System limit is 250, but keep such images structurally
        # editable so CHIRP can spill excess records into the next System.
        if count < 1 or count > 0xFF:
            raise errors.RadioError(
                "%s System %d has invalid channel/group count %d" %
                (description, system["number"], count))
        offset = int(system["offset"])
        end = offset + _system_block_size(count)
        if offset < SYSTEM_DATA_START or end > MEM_SIZE:
            raise errors.RadioError(
                "%s System %d block 0x%04X-0x%04X is outside the packed "
                "System-data area" %
                (description, system["number"], offset, end - 1))

        header = image[offset:offset + CONV_HEADER_SIZE]
        if len(header) != CONV_HEADER_SIZE:
            raise errors.RadioError(
                "%s System %d header is truncated" %
                (description, system["number"]))
        if header[0] & 0x01:
            raise errors.RadioError(
                "%s contains active LTR System %d; variable structural "
                "editing is only enabled for conventional-only images" %
                (description, system["number"]))

        system = dict(system)
        system.update({
            "ltr": False,
            "name": _clean_name(header[0x06:0x0E]),
        })
        active.append(system)
        ranges.append((offset, end, system["number"]))

    if image[ACTIVE_SYSTEM_COUNT] != active_count:
        raise errors.RadioError(
            "%s active-system count is %d, but %d pointer-table entries are "
            "active" %
            (description, image[ACTIVE_SYSTEM_COUNT], active_count))

    ranges.sort()
    for left_range, right_range in zip(ranges, ranges[1:]):
        left_start, left_end, left_num = left_range
        right_start, right_end, right_num = right_range
        if right_start < left_end:
            raise errors.RadioError(
                "%s Systems %d and %d overlap in packed System data" %
                (description, left_num, right_num))

    if len(channels) > MAX_TOTAL_CONVENTIONAL_CHANNELS:
        raise errors.RadioError(
            "%s has %d conventional channels; documented maximum is %d" %
            (description, len(channels), MAX_TOTAL_CONVENTIONAL_CHANNELS))

    return channels, systems, active


def _protected_record_signature(image, channel):
    """Return the per-record bits a system repack is never allowed to alter."""
    record = image[channel["offset"]:channel["offset"] + CONV_RECORD_SIZE]
    if len(record) != CONV_RECORD_SIZE:
        raise errors.RadioError("Conventional channel record is truncated")
    return bytes((record[CH_FLAGS_A] & (~FLAGA_EDITABLE & 0xFF),
                  record[CH_FLAGS_B] & (~FLAGB_EDITABLE & 0xFF)))


def _protected_system_header(image, system):
    """Return a system header with the proven scan-inclusion bit masked out."""
    header = bytearray(image[
        system["offset"]:system["offset"] + CONV_HEADER_SIZE])
    if len(header) != CONV_HEADER_SIZE:
        raise errors.RadioError("Conventional System header is truncated")
    header[SYSTEM_FLAGS] &= ~SYSTEM_SCAN_LIST_EXCLUDE_MASK
    return bytes(header)


def _validate_system_repack(current, target):
    """Sanity-check a structurally repacked conventional image."""
    _validate_dynamic_layout(current, "Current Kenwood configuration")
    tgt_channels, _tgt_systems, _tgt_active = _validate_dynamic_layout(
        target, "Repacked Kenwood configuration")
    if not tgt_channels:
        raise errors.RadioError(
            "Structural editing cannot remove every conventional channel")


def _repack_conventional_systems(
        image, channels, target_systems, header_sources=None,
        record_overrides=None):
    """Pack conventional Systems sequentially and return new record offsets.

    System numbers are independent of storage order. Holes in the 1..32 System
    number space are valid; unprogrammed entries remain FFFF/FF in the pointer
    and count tables. Existing System headers follow their System number. A
    newly created System inherits the complete proven header and pointer scan
    bit from the source System of its first channel unless ``header_sources``
    explicitly selects another source.
    """
    image = bytes(image)
    if len(channels) != len(target_systems):
        raise errors.InvalidValueError(
            "Internal system-assignment count mismatch")
    if not channels:
        raise errors.InvalidValueError(
            "At least one conventional channel is required")
    if len(channels) > MAX_TOTAL_CONVENTIONAL_CHANNELS:
        raise errors.InvalidValueError(
            "This radio configuration supports at most %d conventional "
            "channels total" %
            MAX_TOTAL_CONVENTIONAL_CHANNELS)

    _, _, active = _validate_dynamic_layout(
        image, "Current Kenwood configuration")
    templates = {
        system["number"]: {
            "header": bytes(image[
                system["offset"]:system["offset"] + CONV_HEADER_SIZE]),
            "pointer_scan_included": bool(
                system["ptr"] & SYSTEM_SCAN_INCLUDE_PTR_MASK),
        }
        for system in active
    }
    if not templates:
        raise errors.InvalidValueError(
            "No conventional System header is available as a template")

    header_sources = dict(header_sources or {})
    record_overrides = dict(record_overrides or {})
    records = []
    for logical_index, channel in enumerate(channels):
        if logical_index in record_overrides:
            record = bytearray(record_overrides[logical_index])
        else:
            record = bytearray(image[
                channel["offset"]:channel["offset"] + CONV_RECORD_SIZE])
        if len(record) != CONV_RECORD_SIZE:
            raise errors.InvalidValueError(
                "A conventional channel record is truncated")
        records.append(record)

    target_systems = [int(number) for number in target_systems]
    if any(number < 1 or number > SYSTEM_COUNT for number in target_systems):
        raise errors.InvalidValueError(
            "System number must be between 1 and %d" % SYSTEM_COUNT)
    used_numbers = sorted(set(target_systems))
    if len(used_numbers) > SYSTEM_COUNT:
        raise errors.InvalidValueError(
            "This radio configuration supports at most %d "
            "Systems" % SYSTEM_COUNT)

    rows_by_system = {number: [] for number in used_numbers}
    for logical_index, number in enumerate(target_systems):
        rows_by_system[number].append(logical_index)
    for number, rows in rows_by_system.items():
        if len(rows) > MAX_GROUPS_PER_EDITABLE_SYSTEM:
            raise errors.InvalidValueError(
                "System %d would contain %d channels; documented maximum is "
                "%d" %
                (number, len(rows), MAX_GROUPS_PER_EDITABLE_SYSTEM))

    required = sum(
        _system_block_size(len(rows_by_system[number]))
        for number in used_numbers)
    available = MEM_SIZE - SYSTEM_DATA_START
    if required > available:
        max_for_layout = ((available - len(used_numbers) * CONV_HEADER_SIZE) //
                          CONV_RECORD_SIZE)
        raise errors.InvalidValueError(
            "This %d-System layout needs %d bytes of packed System data, but "
            "only "
            "%d bytes are available. With %d active Systems this image can "
            "hold "
            "at most %d conventional channels." %
            (len(used_numbers), required, available, len(used_numbers),
             max(0, min(MAX_TOTAL_CONVENTIONAL_CHANNELS, max_for_layout))))

    output = bytearray(image)
    output[SYSTEM_PTR_BASE:SYSTEM_PTR_BASE + SYSTEM_COUNT * 2] = (
        b"\xFF" * (SYSTEM_COUNT * 2))
    output[GROUP_COUNT_BASE:GROUP_COUNT_BASE + SYSTEM_COUNT] = (
        b"\xFF" * SYSTEM_COUNT)
    output[ACTIVE_SYSTEM_COUNT] = len(used_numbers)

    # Do not erase the remainder of the data pool. Old/unreferenced bytes are
    # harmless and preserving them avoids gratuitous changes to an image. Only
    # pointer-referenced blocks below are rewritten.
    cursor = SYSTEM_DATA_START
    new_offsets = [None] * len(channels)
    for number in used_numbers:
        source_number = header_sources.get(number)
        if source_number is None and number in templates:
            source_number = number
        if source_number in templates:
            template = templates[source_number]
        else:
            # A genuinely new conventional System gets a clean KPG-style
            # conventional header instead of inheriting opaque/LTR-only state
            # from whichever System happened to supply its first channel.
            template = {
                "header": CONV_HEADER_DEFAULT,
                "pointer_scan_included": True,
            }

        pointer = _system_pointer_for_offset(
            cursor, template["pointer_scan_included"])
        _put_u16le(output, SYSTEM_PTR_BASE + (number - 1) * 2, pointer)
        output[GROUP_COUNT_BASE + number - 1] = len(rows_by_system[number])
        output[cursor:cursor + CONV_HEADER_SIZE] = template["header"]

        base = cursor + CONV_HEADER_SIZE
        for packed_index, logical_index in enumerate(rows_by_system[number]):
            record = bytearray(records[logical_index])
            # Group Number belongs to the record; physical record order
            # controls channel stepping, so repacking must not renumber
            # untouched records.
            record_offset = base + packed_index * CONV_RECORD_SIZE
            output[record_offset:record_offset + CONV_RECORD_SIZE] = record
            new_offsets[logical_index] = record_offset

        cursor += _system_block_size(len(rows_by_system[number]))

    _validate_dynamic_layout(bytes(output), "Repacked Kenwood configuration")
    return bytes(output), new_offsets


def _compact_system_assignments(image, target_systems):
    """Validate and preserve sparse System numbers exactly.

    Real TK-840 System numbers may be sparse. Existing destination Systems
    retain their own headers; new destinations inherit from the moved
    channel's source System inside the repacker.
    """
    target_systems = [int(number) for number in target_systems]
    if not target_systems:
        raise errors.InvalidValueError(
            "At least one conventional System is required")
    if any(number < 1 or number > SYSTEM_COUNT for number in target_systems):
        raise errors.InvalidValueError(
            "System number must be between 1 and %d" % SYSTEM_COUNT)

    _, _, active = _validate_dynamic_layout(
        image, "Current Kenwood configuration before System reassignment")
    existing_numbers = {system["number"] for system in active}
    used_numbers = set(target_systems)
    header_sources = {
        number: number for number in used_numbers if number in existing_numbers
    }
    identity = {number: number for number in used_numbers}
    return target_systems, header_sources, identity


def _parse_conventional_channels(image, log=False):
    """Return flattened conventional channels plus diagnostic system data.

    Silent by default: image detection (match_model) parses files and must
    not log. Pass log=True when loading an image into the driver.
    """
    channels = []
    systems = []

    if len(image) != MEM_SIZE:
        return channels, systems

    reported_active = image[ACTIVE_SYSTEM_COUNT]
    if log:
        LOG.info("Configuration reports %d active systems", reported_active)
    for sys_index in range(SYSTEM_COUNT):
        ptr_word = _u16le(image, SYSTEM_PTR_BASE + sys_index * 2)
        group_count = image[GROUP_COUNT_BASE + sys_index]
        unused = bool(ptr_word & 0x8000)
        pointer_scan_included = bool(
            ptr_word & SYSTEM_SCAN_INCLUDE_PTR_MASK)
        offset = _system_offset(ptr_word)

        diag = {
            "number": sys_index + 1,
            "ptr": ptr_word,
            "count": group_count,
            "unused": unused,
            "fixed_scan_included": pointer_scan_included,
            "offset": offset,
        }
        systems.append(diag)

        if log:
            LOG.debug(
                "SYS %02d ptr=%04X off=%04X count=%d unused=%s "
                "fixed_scan_included=%s",
                sys_index + 1, ptr_word, offset, group_count, unused,
                pointer_scan_included)

        if unused or group_count == 0:
            continue

        # Be defensive against corrupt or implausible System layout data.
        required = CONV_HEADER_SIZE + group_count * CONV_RECORD_SIZE
        if offset < 0 or offset + required > MEM_SIZE:
            if log:
                LOG.warning(
                    "Ignoring implausible system %d: ptr=%04X -> off=%04X, "
                    "count=%d requires %d bytes",
                    sys_index + 1, ptr_word, offset, group_count, required)
            continue

        header = image[offset:offset + CONV_HEADER_SIZE]
        is_ltr = bool(header[0] & 0x01)
        scan_list_included = not bool(
            header[SYSTEM_FLAGS] & SYSTEM_SCAN_LIST_EXCLUDE_MASK)
        fixed_scan_included = pointer_scan_included
        system_name = _clean_name(
            header[SYSTEM_NAME:SYSTEM_NAME + SYSTEM_NAME_LENGTH])
        diag.update({
            "ltr": is_ltr,
            "name": system_name,
            "system_lockout_included": fixed_scan_included,
            "scan_list_included": scan_list_included,
            "fixed_scan_included": fixed_scan_included,
        })

        if is_ltr:
            if log:
                LOG.info("Skipping LTR system %d (%r)",
                         sys_index + 1, system_name)
            continue

        for packed_index in range(group_count):
            rec_offset = (offset + CONV_HEADER_SIZE +
                          packed_index * CONV_RECORD_SIZE)
            rec = image[rec_offset:rec_offset + CONV_RECORD_SIZE]
            if len(rec) != CONV_RECORD_SIZE:
                break

            group_number = rec[CH_GROUP]
            rx_word = _u16le(rec, CH_RX_FREQ)
            tx_word = _u16le(rec, CH_TX_FREQ)
            rx_tone_word = _u16le(rec, CH_RX_TONE)
            tx_tone_word = _u16le(rec, CH_TX_TONE)
            flags_a = rec[CH_FLAGS_A]
            flags_b = rec[CH_FLAGS_B]
            name = _clean_name(rec[CH_NAME:CH_NAME + 8])

            channels.append({
                "system": sys_index + 1,
                "system_name": system_name,
                "system_offset": offset,
                "system_lockout_included": fixed_scan_included,
                "scan_list_included": scan_list_included,
                "packed_index": packed_index,
                "group": group_number,
                "offset": rec_offset,
                "rx_word": rx_word,
                "tx_word": tx_word,
                "rx_tone_word": rx_tone_word,
                "tx_tone_word": tx_tone_word,
                "flags_a": flags_a,
                "flags_b": flags_b,
                "name": name,
            })

    return channels, systems


def _enforce_receive_only_tx_inhibit(image, channels):
    """Make absent conventional TX words safe for radio upload.

    Only absent TX words are modified. An existing TX Inhibit on a
    programmed channel is *never* released just because its TX frequency
    is valid. Both 7FFF and FFFF are recognized, but write 7FFF.
    `image` must be a mutable bytearray.
    """
    changed = 0
    for channel in channels:
        off = channel["offset"]
        word = _u16le(image, off + CH_TX_FREQ)
        if (word & 0x7FFF) != 0x7FFF:
            continue
        flags = image[off + CH_FLAGS_A]
        if word == 0x7FFF and not (flags & FLAGA_TX_INHIBIT):
            continue
        _put_u16le(image, off + CH_TX_FREQ, 0x7FFF)
        image[off + CH_FLAGS_A] = flags & ~FLAGA_TX_INHIBIT
        changed += 1
    return changed


def _looks_like_supported_image(data):
    if len(data) != MEM_SIZE:
        return False
    channels, systems = _parse_conventional_channels(data)
    # An image may be LTR-only, so also accept at least one plausible active
    # system pointer even when no conventional channels were produced.
    plausible_system = any(
        (not s["unused"] and s["count"] and
         0 <= s["offset"] < MEM_SIZE)
        for s in systems
    )
    return bool(channels) or plausible_system


def _saved_image_family_hint(data):
    """Infer 840 versus shared 94x format from plausible channel frequencies.

    This is intentionally a hint, not a hard model marker. TK-940 and TK-941
    codeplugs are treated as interchangeable. Ambiguous images remain eligible
    for either family rather than being rejected.
    """
    if not _looks_like_supported_image(data):
        return None
    channels, _systems = _parse_conventional_channels(data)
    score_840 = 0
    score_94x = 0
    for channel in channels:
        word = channel["rx_word"]
        f840 = _decode_frequency(word)
        f94x = _decode_frequency_941(word)
        if f840 is not None and 403_000_000 <= f840 <= 512_000_000:
            score_840 += 1
        if f94x is not None and 851_000_000 <= f94x <= 999_987_500:
            score_94x += 1
    if score_840 and not score_94x:
        return "840"
    if score_94x and not score_840:
        return "94x"
    if score_840 >= max(3, score_94x * 2):
        return "840"
    if score_94x >= max(3, score_840 * 2):
        return "94x"
    return None


def _clone_prompts(model, firmware_probe):
    """Download/upload prompts shared by the three models."""
    prompts = chirp_common.RadioPrompts()
    prompts.pre_download = (
        "1. Turn the %s off.\n"
        "2. Connect the KPG-4 interface.\n"
        "3. Turn the radio on normally.\n"
        "4. Click OK to begin.\n\n"
        "If the radio has a programming password, CHIRP reads it from the "
        "radio and logs in with it. The password is not changed." % model)
    if firmware_probe:
        prompts.pre_download += (
            "\n\nFirmware compatibility: This driver works with stock "
            "Kenwood firmware and recognized modifications, including "
            "the Enhanced TX and Enhanced TOT patches. After downloading, "
            "see Settings -> Firmware Features for your radio's patch "
            "status and information about optional enhancements.")
    prompts.pre_upload = (
        "Verified %s configuration upload.\n"
        "Only E600-FDFF is written. Each block is read and compared first; "
        "unchanged blocks are skipped, while changed blocks are written and "
        "immediately verified, with up to three attempts." % model)
    return prompts


@directory.register


class TK840Radio(chirp_common.CloneModeRadio):
    """Kenwood TK-840 driver with verified configuration writes."""

    VENDOR = "Kenwood"
    MODEL = "TK-840"
    BAUD_RATE = INITIAL_BAUD
    _radio_family = "840"
    _display_model = "TK-840"
    _has_ham_test_defaults = True
    # The radio's actual inclusive upper limit is 522.000000 MHz. Current
    # CHIRP treats valid_bands upper endpoints as exclusive.
    _valid_bands = [(370_000_000, 522_000_001)]
    _frequency_unused_mask = FREQ_UNUSED_MASK
    _normalize_unused_supported = True

    # Conservative default used before a radio image has been parsed.
    _upper = MAX_LOGICAL_MEMORIES
    _tk840_ident = None
    _tk840_password = None
    _tk840_transaction = None
    _tk840_clone_active = False
    _show_commercial_options = False
    _normalize_unused = True
    _disable_commercial_features = True
    _normalize_group_numbers = False
    _pending_system_settings = None
    _firmware_tag = None
    _firmware_probe = None

    @classmethod
    def get_prompts(cls):
        return _clone_prompts(cls._display_model, True)

    def sync_in(self):
        if getattr(self, "_tk840_clone_active", False):
            raise errors.RadioError(
                "%s clone operation is already in "
                "progress" % _radio_label(self))
        self._tk840_clone_active = True
        try:
            self._mmap = do_download(self)
            live_tag = self._firmware_tag
            live_probe = self._firmware_probe
            self.process_mmap()
            # process_mmap loads saved-image metadata, but a live probe always
            # takes precedence. Persist the result for offline .img editing.
            self._firmware_tag = live_tag
            self._firmware_probe = live_probe
            self._save_driver_preferences()
        finally:
            self._tk840_clone_active = False

    def sync_out(self):
        if getattr(self, "_tk840_clone_active", False):
            raise errors.RadioError(
                "%s clone operation is already in "
                "progress" % _radio_label(self))
        self._tk840_clone_active = True
        try:
            if not hasattr(self, "_channels"):
                self.process_mmap()
            self._apply_upload_policies()
            do_upload(self)
        finally:
            self._tk840_clone_active = False

    def _load_driver_preferences(self):
        """Load CHIRP-only policy from the standard .img metadata trailer."""
        prefs = self.metadata.get(DRIVER_PREFS_METADATA_KEY, {})
        if (not isinstance(prefs, dict) or
                prefs.get("version") != DRIVER_PREFS_METADATA_VERSION):
            prefs = {}

        self._normalize_unused = bool(
            prefs.get("normalize_unused", True))
        self._disable_commercial_features = bool(
            prefs.get("disable_commercial_features", True))
        self._show_commercial_options = bool(
            prefs.get("show_commercial_options", False))
        self._normalize_group_numbers = bool(
            prefs.get("normalize_group_numbers", False))
        tag = prefs.get("firmware_tag")
        self._firmware_tag = (
            tag if isinstance(tag, int) and not isinstance(tag, bool) and
            0 <= tag <= 0xFF else None)
        saved_probe = prefs.get("firmware_probe")
        if (isinstance(saved_probe, dict) and
                saved_probe.get("revision") in
                tuple(FIRMWARE_PATCH_PROFILES) + (None,) and
                saved_probe.get("tx") in
                ("patched", "plus25", "stock", "legacy", "partial",
                 "unverified", "unknown", "not-applicable") and
                saved_probe.get("tot") in
                ("ff-off", "test10x", "stock", "legacy", "modified",
                 "unsupported", "unverified", "unknown")):
            self._firmware_probe = dict(saved_probe)
        else:
            self._firmware_probe = None

    def _save_driver_preferences(self):
        """Save CHIRP-only policy in .img metadata, never in radio memory."""
        self.metadata = {
            DRIVER_PREFS_METADATA_KEY: {
                "version": DRIVER_PREFS_METADATA_VERSION,
                "normalize_unused": self._normalize_unused,
                "disable_commercial_features":
                    self._disable_commercial_features,
                "show_commercial_options": self._show_commercial_options,
                "normalize_group_numbers": self._normalize_group_numbers,
                "firmware_tag": self._firmware_tag,
                "firmware_probe": self._firmware_probe,
            }
        }

    def _apply_commercial_policy(self):
        """Disable other commercial bits, preserving TA and TX Inhibit."""
        image = bytearray(self._mmap.get_packed())
        channels, _systems = _parse_conventional_channels(image)
        for channel in channels:
            off = channel["offset"]
            image[off + CH_FLAGS_A] |= FLAGA_COMMERCIAL
            image[off + CH_FLAGS_B] |= FLAGB_COMMERCIAL
        self._mmap.set(0, bytes(image))
        if hasattr(self, "_channels"):
            for channel in self._channels:
                if channel is not None:
                    channel["flags_a"] |= FLAGA_COMMERCIAL
                    channel["flags_b"] |= FLAGB_COMMERCIAL

    def _ham_test_table(self):
        """Return the full-band amateur alignment table for a TK-840."""
        return _build_test_table_840(HAM_TEST_840)

    def _apply_ham_test_table(self):
        table = self._ham_test_table()
        if len(table) != TEST_TABLE_LENGTH:
            raise errors.RadioError("Internal amateur test-table size error")
        current = bytes(self._mmap.get(TEST_TABLE_OFFSET, TEST_TABLE_LENGTH))
        if current == table:
            LOG.debug("Amateur test/alignment table already correct for %s",
                      self._display_model)
            return
        self._mmap.set(TEST_TABLE_OFFSET, table)
        LOG.info("Corrected amateur test/alignment table for %s",
                 self._display_model)

    def _apply_upload_policies(self):
        """Apply CHIRP policy before constructing the upload image."""
        self._repair_group_layout()
        if self._normalize_group_numbers:
            self._normalize_groups_by_physical_order()
        if self._normalize_unused:
            self._normalize_unused_bits()
        if self._disable_commercial_features:
            self._apply_commercial_policy()
        self._enforce_receive_only_tx_on_upload()

    def _enforce_receive_only_tx_on_upload(self):
        """Force TX Inhibit for missing TX words just before writing."""
        image = bytearray(self._mmap.get_packed())
        channels, _systems = _parse_conventional_channels(image)
        changed = _enforce_receive_only_tx_inhibit(image, channels)
        if not changed:
            return
        self._mmap.set(0, bytes(image))
        # Keep CHIRP's logical row ordering unchanged. Update its cached
        # record values in place rather than rebuilding that mapping.
        for channel in getattr(self, "_channels", []):
            if channel is None:
                continue
            if (channel["tx_word"] & 0x7FFF) == 0x7FFF:
                channel["tx_word"] = 0x7FFF
                channel["flags_a"] &= ~FLAGA_TX_INHIBIT
        LOG.info("%s upload safety: inhibited %d receive-only channel(s)",
                 self._display_model, changed)

    def _normalize_groups_by_physical_order(self):
        """Renumber Group IDs 1..N in physical order within each System.

        Group Number is an internal channel ID, not the channel stepping order.
        When this explicit ham-oriented policy is enabled, make those IDs match
        physical order while preserving the target of the known
        Home/Fixed-Revert System/Group reference.
        """
        image = bytearray(self._mmap.get_packed())
        channels, _systems = _parse_conventional_channels(image)
        if not channels:
            return

        home_system = image[GLOBAL_HOME_SYSTEM]
        home_group = image[GLOBAL_HOME_GROUP]
        home_matches = [
            channel for channel in channels
            if (int(channel["system"]) == home_system and
                int(channel["group"]) == home_group)
        ]
        home_offset = (home_matches[0]["offset"]
                       if len(home_matches) == 1 else None)

        next_group = {}
        changed = False
        new_home_group = None
        for channel in channels:
            system = int(channel["system"])
            group = next_group.get(system, 1)
            if group > MAX_GROUPS_PER_EDITABLE_SYSTEM:
                raise errors.InvalidValueError(
                    "System %d exceeds the %d-channel limit" %
                    (system, MAX_GROUPS_PER_EDITABLE_SYSTEM))
            next_group[system] = group + 1
            if int(channel["group"]) != group:
                image[channel["offset"] + CH_GROUP] = group
                changed = True
            if home_offset == channel["offset"]:
                new_home_group = group

        if new_home_group is not None and home_group != new_home_group:
            image[GLOBAL_HOME_GROUP] = new_home_group
            changed = True
            LOG.info(
                "%s kept Home / Fixed-Revert on the same physical channel "
                "while normalizing Group Numbers: System %d Group %d -> %d",
                self._display_model, home_system, home_group, new_home_group)

        if changed:
            self._mmap.set(0, bytes(image))
            self._refresh_channel_cache(bytes(image))
            LOG.info("Normalized %s Group Numbers by physical channel order",
                     self._display_model)

    def _normalize_unused_bits(self):
        """Normalize unused bits, except the canonical absent-TX word 7FFF."""
        image = bytearray(self._mmap.get_packed())
        image[GLOBAL_AUX_SWITCH] |= GLOBAL_AUX_UNUSED_MASK
        channels, _systems = _parse_conventional_channels(image)
        for channel in channels:
            off = channel["offset"]
            image[off + CH_FLAGS_A] |= FLAGA_UNUSED
            image[off + CH_FLAGS_B] |= FLAGB_UNUSED
            _put_u16le(
                image, off + CH_RX_FREQ,
                _u16le(image, off + CH_RX_FREQ) | self._frequency_unused_mask)
            # Missing TX words use the canonical 7FFF marker; unlike a
            # programmed frequency, their high bit must stay clear.
            tx_word = _u16le(image, off + CH_TX_FREQ)
            if (tx_word & 0x7FFF) != 0x7FFF:
                _put_u16le(
                    image, off + CH_TX_FREQ,
                    tx_word | self._frequency_unused_mask)
        self._mmap.set(0, bytes(image))
        if hasattr(self, "_channels"):
            for channel in self._channels:
                if channel is not None:
                    channel["flags_a"] |= FLAGA_UNUSED
                    channel["flags_b"] |= FLAGB_UNUSED
                    channel["rx_word"] |= self._frequency_unused_mask
                    if (channel["tx_word"] & 0x7FFF) != 0x7FFF:
                        channel["tx_word"] |= self._frequency_unused_mask

    def _ensure_pending_system_settings(self):
        """Return session-only settings for all 32 possible Systems.

        Unused Systems have no on-radio header, so their settings can only live
        in the current CHIRP driver instance until a channel activates them.
        """
        if self._pending_system_settings is None:
            self._pending_system_settings = {}
        for number in range(1, SYSTEM_COUNT + 1):
            self._pending_system_settings.setdefault(number, {
                "name": "",
                "scan_list_included": True,
                "system_lockout_included": True,
            })
        return self._pending_system_settings

    def _sync_pending_system_settings_from_image(self):
        """Refresh pending values for Systems that actually exist on-radio."""
        pending = self._ensure_pending_system_settings()
        for system in self._systems:
            if (system.get("unused") or not system.get("count") or
                    system.get("ltr") is not False):
                continue
            pending[system["number"]] = {
                "name": str(system.get("name", "")).upper(),
                "scan_list_included": bool(system.get("scan_list_included")),
                "system_lockout_included": bool(
                    system.get("system_lockout_included")),
            }

    def _system_by_number(self, number):
        for system in self._systems:
            if system.get("number") == int(number):
                return system
        return None

    def _active_conventional_system(self, number):
        system = self._system_by_number(number)
        return bool(system and not system.get("unused") and system.get("count")
                    and system.get("ltr") is False)

    def _apply_pending_system_settings(self, image):
        """Apply pending settings only to Systems present in *image*."""
        output = bytes(image)
        pending = self._ensure_pending_system_settings()
        _channels, systems = _parse_conventional_channels(output)
        for system in systems:
            if (system.get("unused") or not system.get("count") or
                    system.get("ltr") is not False):
                continue
            number = system["number"]
            values = pending[number]
            output = _set_system_name(output, number, values["name"])
            output = _set_system_scan_list(
                output, number, values["scan_list_included"])
            output = _set_system_lockout_included(
                output, number, values["system_lockout_included"])
        return output

    def process_mmap(self):
        image = self._mmap.get_packed()
        self._load_driver_preferences()
        # A newly loaded image starts a new pending-settings session.
        # Values for active Systems are immediately populated from the actual
        # codeplug.
        self._pending_system_settings = {}
        self._refresh_channel_cache(image)
        # Normalize only objectively invalid Group assignments in the working
        # image. The radio is not changed until the user uploads.
        self._repair_group_layout()

    def _refresh_channel_cache(self, image=None, logical_offsets=None):
        """Refresh decoded records, optionally retaining row order."""
        if image is None:
            image = self._mmap.get_packed()
        parsed_channels, self._systems = _parse_conventional_channels(
            image, log=logical_offsets is None)
        self._sync_pending_system_settings_from_image()
        if logical_offsets is None:
            self._channels = list(parsed_channels)
            self._channels.extend(
                [None] * (MAX_LOGICAL_MEMORIES - len(self._channels)))
        else:
            by_offset = {channel["offset"]: channel
                         for channel in parsed_channels}
            try:
                self._channels = [None if offset is None else by_offset[offset]
                                  for offset in logical_offsets]
            except KeyError as exc:
                raise errors.InvalidValueError(
                    "Internal channel-offset mismatch after edit: %s" %
                    exc)
            if len(self._channels) < MAX_LOGICAL_MEMORIES:
                self._channels.extend(
                    [None] * (MAX_LOGICAL_MEMORIES - len(self._channels)))
            elif len(self._channels) > MAX_LOGICAL_MEMORIES:
                self._channels = self._channels[:MAX_LOGICAL_MEMORIES]

        self._upper = MAX_LOGICAL_MEMORIES
        active_count = sum(channel is not None for channel in self._channels)
        LOG.info(
            "Parsed %d conventional %s records",
            active_count, self._display_model)
        try:
            _validate_dynamic_layout(
                image, "Loaded %s image" % self._display_model)
            self._system_edit_error = None
        except errors.RadioError as exc:
            self._system_edit_error = str(exc)
            LOG.info("System assignment remains read-only: %s", exc)
        if not active_count:
            LOG.warning(
                "No conventional channel records were parsed. Save the raw "
                "image and debug log; the clone transport may still be "
                "correct.")

    def filter_name(self, name):
        """Map period to comma (the radio has no period), then CHIRP's filter.

        CHIRP calls this for names typed in the memory editor and for names
        imported from other radios and files.
        """
        return super().filter_name(str(name).replace(".", ","))

    def _system_choices(self):
        return [str(number) for number in range(1, SYSTEM_COUNT + 1)]

    def _decode_frequency_word(self, word):
        return _decode_frequency(word)

    def _encode_frequency_word(self, freq_hz, old_word):
        return _encode_frequency(freq_hz, old_word)

    def get_features(self):
        rf = chirp_common.RadioFeatures()
        rf.has_settings = True
        rf.has_bank = False
        # The ordinary TK-840 uses the proven 12.5-kHz PLL raster.  Keep the
        # Tune Step column hidden because there is no per-channel user setting.
        rf.has_tuning_step = False
        rf.valid_tuning_steps = [12.5]
        rf.has_name = True
        rf.has_offset = True
        rf.has_mode = False          # bandwidth encoding not mapped yet
        rf.valid_modes = ["FM"]
        rf.has_dtcs = True
        rf.has_rx_dtcs = True
        rf.has_dtcs_polarity = True
        rf.has_ctone = True
        rf.has_cross = True
        rf.can_odd_split = True

        rf.valid_tmodes = ["", "Tone", "TSQL", "DTCS", "Cross"]
        rf.valid_cross_modes = [
            "Tone->Tone", "DTCS->", "->DTCS", "Tone->DTCS",
            "DTCS->Tone", "->Tone", "DTCS->DTCS",
        ]
        rf.valid_duplexes = ["", "-", "+", "split", "off"]
        # TK-840 System Lockout is shared by every channel in a system.
        rf.valid_skips = ["", "S"]
        rf.valid_dtcs_codes = list(chirp_common.DTCS_CODES)
        rf.valid_characters = CHANNEL_NAME_CHARSET
        rf.valid_name_length = 8
        # Intentionally broader than the documented RF splits so experimenters
        # can test where a particular radio's synthesizer and RF hardware work.
        rf.valid_bands = list(self._valid_bands)
        rf.memory_bounds = (1, MAX_LOGICAL_MEMORIES)
        return rf

    def get_settings(self):
        """Expose only global settings whose codeplug encoding is proven."""
        basic = RadioSettingGroup(
            "basic", "%s Global Settings" % self._display_model)

        aux_raw = self._mmap.get(GLOBAL_AUX_SWITCH, 1)[0]
        aux_index = aux_raw & GLOBAL_AUX_SWITCH_MASK
        basic.append(_with_doc(RadioSetting(
            "tk840_aux_switch", "AUX Button",
            RadioSettingValueList(
                AUX_SWITCH_CHOICES, current_index=aux_index)),
            "Assigns the front-panel AUX key. Scan Add/Delete is useful for "
            "temporarily adding or deleting the selected System from scan. "
            "The triangle indicator shows the deleted/locked-out state."))

        scan_raw = self._mmap.get(GLOBAL_SCAN_SWITCH, 1)[0]
        if scan_raw & GLOBAL_SCAN_DISABLE_MASK:
            scan_value = "Disabled"
        elif scan_raw & GLOBAL_SCAN_FIXED_MASK:
            scan_value = "Fixed System Scan"
        else:
            scan_value = "Scan List"
        basic.append(_with_doc(RadioSetting(
            "tk840_scan_switch", "Scan Switch",
            RadioSettingValueList(
                SCAN_SWITCH_CHOICES,
                current_index=SCAN_SWITCH_CHOICES.index(scan_value))),
            "Selects the radio scan mode. System Lockout/CHIRP Skip applies "
            "to a whole System, not to an individual channel. The selected "
            "revert System is still scanned even when it is locked out."))

        raw = self._mmap.get(GLOBAL_OFFHOOK_DECODE, 1)[0]
        basic.append(_with_doc(RadioSetting(
            "tk840_off_hook_decode", "Off Hook Decode",
            RadioSettingValueBoolean(
                not bool(raw & GLOBAL_OFFHOOK_DECODE_MASK))),
            "Controls whether receive decoding remains active while the "
            "microphone is off hook. Most amateur installations will want "
            "decode enabled."))
        basic.append(_with_doc(RadioSetting(
            "tk840_off_hook_scan", "Off Hook Scan",
            RadioSettingValueBoolean(
                not bool(raw & GLOBAL_OFFHOOK_SCAN_MASK))),
            "Controls whether scanning continues while the microphone is off "
            "hook. Amateur users will normally want this enabled so taking "
            "the microphone off hook does not silently stop scanning."))


        dispatch_raw = self._mmap.get(GLOBAL_TOT_DISPATCH, 1)[0]
        test10x = _tot_test10x_detected(self._firmware_probe)
        if test10x:
            basic.append(_with_doc(
                _ro_string(
                    "tk840_tot_test_warning", "WARNING: 10x TEST FIRMWARE",
                    "Not for normal use - restore normal-speed firmware!", 90),
                "The Toolkit's accelerated 10x Dispatch TOT diagnostic "
                "firmware was detected by an exact-byte firmware probe. "
                "Timeouts are one tenth of normal. Restore normal-speed "
                "firmware before regular operation. For an offline CHIRP "
                "image, this information comes from the radio's last "
                "download; re-download after reflashing to refresh it."))
        basic.append(_with_doc(RadioSetting(
            "tk840_dispatch_tot",
            ("TX Time-out Timer (10x TEST)" if test10x
             else "TX Time-out Timer (M:SS)"),
            _tot_setting_value(dispatch_raw, accelerated=test10x)),
            ("WARNING: 10x TEST firmware detected. Each choice shows "
             "the programmed normal-speed timeout first, followed by "
             "the actual accelerated duration in parentheses. After "
             "restoring normal-speed firmware, the programmed time "
             "remains unchanged. Restore normal-speed firmware before "
             "regular use. "
             if test10x else
             "Maximum continuous transmit time, 0:15 through 10:00. ") +
            "Off (Enhanced TOT Patch only) stores raw 0xFF. On stock "
            "firmware 0xFF is NOT Off; select it only when Enhanced TOT "
            "is verified."))

        dropout_raw = self._mmap.get(GLOBAL_SCAN_DROPOUT, 1)[0]
        basic.append(_with_doc(RadioSetting(
            "tk840_scan_dropout", "Scan Resume Delay (M:SS)",
            _list_value_with_unknown(DELAY_CHOICES, dropout_raw)),
            "Delay after a received signal disappears before scanning "
            "resumes, shown as minutes:seconds."))

        dwell_raw = self._mmap.get(GLOBAL_SCAN_DWELL, 1)[0]
        basic.append(_with_doc(RadioSetting(
            "tk840_scan_dwell", "Post-TX Scan Dwell (M:SS)",
            _list_value_with_unknown(DELAY_CHOICES, dwell_raw)),
            "Delay after transmitting before scanning resumes, shown as "
            "minutes:seconds."))

        home_system_raw = self._mmap.get(GLOBAL_HOME_SYSTEM, 1)[0]
        basic.append(_with_doc(RadioSetting(
            "tk840_home_system", "Home / Fixed-Revert System",
            _list_value_with_unknown(
                HOME_SYSTEM_CHOICES, home_system_raw, base=1)),
            "System selected by the AUX Home Channel / Fixed Revert "
            "function."))

        home_group_raw = self._mmap.get(GLOBAL_HOME_GROUP, 1)[0]
        basic.append(_with_doc(RadioSetting(
            "tk840_home_group", "Home / Fixed-Revert Channel",
            _list_value_with_unknown(
                HOME_GROUP_CHOICES, home_group_raw, base=1)),
            "Channel/group selected by the AUX Home Channel / Fixed Revert "
            "function."))

        power_on = RadioSettingGroup("power_on", "Power-On Key Options")
        power_raw = self._mmap.get(GLOBAL_POWERON_OPTIONS, 1)[0]
        power_on.append(_with_doc(RadioSetting(
            "tk840_poweron_event_8c", "Group Up: Dealer Mode",
            RadioSettingValueBoolean(bool(power_raw & POWERON_EVENT_8C_MASK))),
            "Allows Dealer Mode to be entered by holding GRP UP during "
            "power-on."))
        power_on.append(_with_doc(RadioSetting(
            "tk840_poweron_event_8d", "Group Down: Panel Tuning / Alignment",
            RadioSettingValueBoolean(bool(power_raw & POWERON_EVENT_8D_MASK))),
            "Allows Panel Tuning / Alignment Mode to be entered by holding "
            "GRP DOWN during power-on."))
        power_on.append(_with_doc(RadioSetting(
            "tk840_poweron_system_up",
            "System Up: Firmware Programming / PROG",
            RadioSettingValueBoolean(
                bool(power_raw & POWERON_SYSTEM_UP_MASK))),
            "Allows firmware programming mode to be entered with SYSTEM UP "
            "during power-on."))
        power_on.append(_with_doc(RadioSetting(
            "tk840_poweron_event_9e", "AUX: Clone Mode",
            RadioSettingValueBoolean(bool(power_raw & POWERON_EVENT_9E_MASK))),
            "Allows radio-to-radio Clone Mode to be entered with AUX during "
            "power-on."))

        password = RadioSettingGroup("password", "Programming Password")
        password_raw = bytes(self._mmap.get(PASSWORD_OFFSET, PASSWORD_LENGTH))
        password_enabled = password_raw != PASSWORD_ERASED
        password.append(_with_doc(RadioSetting(
            "tk840_password_enabled", "Password protection enabled",
            RadioSettingValueBoolean(password_enabled)),
            "Enables or disables the radio's programming password. When "
            "enabled, enter a password of up to ten printable characters "
            "below."))
        password_value = RadioSettingValueString(
            0, PASSWORD_LENGTH, _decode_program_password(password_raw), False)
        password_value.set_charset(PASSWORD_CHARSET)
        password.append(_with_doc(RadioSetting(
            "tk840_programming_password", "Programming Password",
            password_value),
            "Case-sensitive programming password. The driver can read the "
            "stored password from a protected radio and use it automatically "
            "when cloning."))

        systems = RadioSettingGroup("systems", "Conventional System Settings")
        pending = self._ensure_pending_system_settings()
        by_number = {system["number"]: system for system in self._systems}
        for number in range(1, SYSTEM_COUNT + 1):
            system = by_number.get(number, {})
            active = bool(
                not system.get("unused", True) and system.get("count"))
            ltr = bool(active and system.get("ltr") is True)
            values = pending[number]
            tree_name = str(values.get("name", "")).strip()
            tree_label = ("System %d - %s" % (number, tree_name)
                          if tree_name else "System %d" % number)
            group = RadioSettingGroup(
                "system_%02d" % number, tree_label)
            unused_note = (
                " This System is currently unused. These settings are kept "
                "only in this CHIRP session and are not stored in the radio "
                "until a channel is assigned to this System. If it remains "
                "unused, they will be lost when the image is reloaded or the "
                "editor is closed."
            ) if not active else ""

            if ltr:
                group.append(_with_doc(
                    _ro_string("tk840_system_name_%02d" % number,
                               "System Name", system.get("name", ""),
                               SYSTEM_NAME_LENGTH),
                    "This active System is LTR; conventional System editing "
                    "is not enabled for it."))
                systems.append(group)
                continue

            name_value = RadioSettingValueString(
                0, SYSTEM_NAME_LENGTH, values["name"], False)
            name_value.set_charset(NAME_CHARSET)
            name_value.set_validate_callback(lambda value: str(value).upper())
            name_setting = RadioSetting(
                "tk840_system_name_%02d" % number, "System Name",
                name_value)
            group.append(_with_doc(
                name_setting,
                "Eight-character System name. Lower-case input is stored as "
                "upper-case; period input is stored as comma." + unused_note))

            scan_list_setting = RadioSetting(
                "tk840_system_scan_list_%02d" % number, "Scan List",
                RadioSettingValueBoolean(values["scan_list_included"]))
            group.append(_with_doc(
                scan_list_setting,
                "Includes this System in Scan List mode. This is separate "
                "from System Lockout." + unused_note))

            lockout_setting = RadioSetting(
                "tk840_system_lockout_%02d" % number, "System Enabled",
                RadioSettingValueBoolean(values["system_lockout_included"]))
            group.append(_with_doc(
                lockout_setting,
                "Controls System Lockout. Enabled means the System is "
                "available to Fixed System Scan; CHIRP's Skip column mirrors "
                "this "
                "setting."
                + unused_note))
            systems.append(group)

        alignment = RadioSettingGroup(
            "alignment", "Dealer / Panel Alignment Test Frequencies")
        # CHIRP does not live-refresh setting widgets after set_settings().
        # Reset the one-shot latch whenever a fresh Settings view is built.
        self._ham_test_defaults_latched = False
        if self._has_ham_test_defaults:
            alignment.append(_with_doc(RadioSetting(
                "tk840_install_ham_test_frequencies",
                "Load amateur test frequencies",
                RadioSettingValueBoolean(False)),
                "Loads amateur test/alignment frequencies into all ten slots. "
                "Due to a quirk in how CHIRP refreshes settings, the table "
                "may still look unchanged after applying this option. Write "
                "the radio configuration and read it back to refresh the "
                "displayed values."))
        test_raw = bytes(self._mmap.get(TEST_TABLE_OFFSET, TEST_TABLE_LENGTH))
        for index in range(TEST_TABLE_SLOTS):
            off = index * 4
            rx_word = _u16le(test_raw, off)
            tx_word = _u16le(test_raw, off + 2)
            if self._radio_family in ("940", "941"):
                rx_hz = _decode_test_word_941(rx_word, 851_000_000)
                tx_hz = _decode_test_word_941(tx_word, 812_000_000)
            else:
                rx_hz = _decode_test_word_840(rx_word)
                tx_hz = _decode_test_word_840(tx_word)
            for side, freq_hz in (("rx", rx_hz), ("tx", tx_hz)):
                value = RadioSettingValueString(
                    0, 12, _format_test_frequency(freq_hz), False)
                value.set_charset("0123456789.")
                alignment.append(_with_doc(RadioSetting(
                    "tk840_test_%s_%02d" % (side, index + 1),
                    "Test %d %s (MHz)" % (index + 1, side.upper()), value),
                    "%s frequency for alignment/test slot %d. Leave blank to "
                    "make the slot unused. Frequencies use 12.5 kHz steps." %
                    (side.upper(), index + 1)))

        firmware = RadioSettingGroup(
            "firmware_features", "Firmware Features")
        firmware.append(_with_doc(
            _ro_string(
                "tk840_firmware_revision", "Firmware revision",
                _firmware_revision_status_text(self._firmware_probe), 100),
            "Revision identified by the SHA-256 firmware fingerprint in "
            "block DF00-DF7F, not by its 16-bit checksum."))
        firmware.append(_with_doc(
            _ro_string(
                "tk840_firmware_feature_tag", "Patch metadata",
                _firmware_feature_status(self._firmware_tag), 100),
            "Read-only patch metadata at E5DF. It is informational; actual "
            "known patch code is checked independently even when metadata "
            "is absent or disagrees."))
        firmware.append(_with_doc(
            _ro_string(
                "tk840_firmware_tx_status", "TX behavior",
                _firmware_tx_status_text(
                    self._firmware_tag, self._radio_family,
                    self._firmware_probe), 100),
            "Read-only inspection of the Enhanced TX and historical +25 MHz "
            "patch locations on recognized TK-940/941 firmware revisions."))
        firmware.append(_with_doc(
            _ro_string(
                "tk840_firmware_tot_status", "TOT behavior",
                _firmware_tot_status_text(
                    self._firmware_tag, self._firmware_probe), 100),
            "Read-only byte-exact inspection of Enhanced TOT, 10x TEST MODE "
            "and Legacy TOT Bypass on TK-840 8355/9306 and TK-940/941 "
            "AB00. 10x mode is diagnostic firmware, not for normal use. "
            "Restore normal-speed firmware using the Toolkit. Off stores "
            "0xFF and only works with Enhanced TOT."))

        if self._radio_family in ("940", "941"):
            firmware.append(_with_doc(
                _ro_string(
                    "tk840_firmware_tx_explanation", "Enhanced TX benefit",
                    "Uses channel TX frequency instead of a fixed offset",
                    100),
                "On the TK-940/TK-941, stock firmware ignores the channel's "
                "programmed TX frequency. Enhanced TX honors that field, "
                "eliminating fixed-offset programming workarounds."))
        firmware.append(_with_doc(
            _ro_string(
                "tk840_firmware_tot_explanation", "Enhanced TOT benefit",
                "Adds a timer Off choice while retaining normal durations",
                100),
            "Enhanced TOT adds an Off option for the Dispatch transmit "
            "time-out timer. Off does not function as intended on stock "
            "firmware."))
        advice = _firmware_patch_advice(
            self._radio_family, self._firmware_probe)
        if advice:
            firmware.append(_with_doc(
                _ro_string(
                    "tk840_firmware_patch_advice", "Optional enhancements",
                    advice, 100),
                "Firmware modifications are optional. Stock firmware "
                "remains supported. See the Toolkit documentation "
                "for compatible revisions and patching instructions."))
        firmware.append(_with_doc(
            _ro_string(
                "tk840_firmware_toolkit", "Toolkit information",
                "https://github.com/KY4WI/Kenwood_TK-x4x_Toolkit", 100),
            "Documentation for optional Enhanced firmware patches, "
            "supported revisions, backup, and firmware modification. "
            "This is an informational URL, not a CHIRP firmware update."))

        driver = RadioSettingGroup("driver", "Driver Options")
        if self._normalize_unused_supported:
            driver.append(_with_doc(RadioSetting(
                "tk840_normalize_unused", "Normalize unused fields",
                RadioSettingValueBoolean(self._normalize_unused)),
                "Keeps unused configuration fields in the radio's normal "
                "factory state. Recommended unless you are deliberately "
                "studying the raw image."))
        driver.append(_with_doc(RadioSetting(
            "tk840_normalize_groups", "Normalize Group Numbers",
            RadioSettingValueBoolean(self._normalize_group_numbers)),
            "Renumbers each System's Group Numbers sequentially in the "
            "radio's physical channel order. Group Number is normally hidden "
            "because it does not control channel stepping. Known Home / "
            "Fixed-Revert references are updated so they continue to select "
            "the same physical channel. Leave disabled to preserve valid "
            "existing Group Numbers."))
        driver.append(_with_doc(RadioSetting(
            "tk840_disable_commercial", "Disable commercial channel features",
            RadioSettingValueBoolean(self._disable_commercial_features)),
            "Disables other legacy commercial channel features that are "
            "normally not needed for amateur use. Talk Around and "
            "TX Inhibit are preserved."))
        driver.append(_with_doc(RadioSetting(
            "tk840_show_commercial", "Show commercial channel options",
            RadioSettingValueBoolean(self._show_commercial_options)),
            "Shows the radio's legacy commercial channel options in each "
            "channel's Extra fields so they can be inspected or edited. "
            "After changing this setting, use the Display menu's Show extra "
            "fields option. If it is already on, turn it off and back on; "
            "if it is off, turn it on."))

        groups = [basic, power_on, password, systems, alignment]
        groups.append(firmware)
        groups.append(driver)
        return RadioSettings(*groups)

    def set_settings(self, settings):
        """Patch proven radio settings and CHIRP-only driver policy."""
        leaves = []

        def collect(items):
            for item in items:
                if isinstance(item, RadioSetting):
                    leaves.append(item)
                else:
                    collect(item)

        collect(settings)

        # Apply the enable checkbox and password text as one logical setting,
        # independent of the order CHIRP traverses the two controls.
        password_enable = next(
            (item for item in leaves
             if item.get_name() == "tk840_password_enabled"), None)
        password_text = next(
            (item for item in leaves
             if item.get_name() == "tk840_programming_password"), None)
        if (password_enable is not None and password_text is not None and
                (password_enable.changed() or password_text.changed())):
            if bool(password_enable.value):
                encoded = _encode_program_password(str(password_text.value))
            else:
                encoded = PASSWORD_ERASED
            self._mmap.set(PASSWORD_OFFSET, encoded)

        load_amateur_defaults = next(
            (item for item in leaves
             if item.get_name() == "tk840_install_ham_test_frequencies"), None)
        defaults_checked = bool(
            load_amateur_defaults is not None and
            bool(load_amateur_defaults.value))
        if not defaults_checked:
            self._ham_test_defaults_latched = False
        use_amateur_defaults = bool(
            defaults_checked and
            not getattr(self, "_ham_test_defaults_latched", False))
        if use_amateur_defaults:
            self._ham_test_defaults_latched = True
            self._apply_ham_test_table()
            # Keep the still-open Settings view synchronized with the bytes we
            # just installed. This makes Apply immediately show exactly what
            # will be written rather than leaving stale pre-default values.
            test_raw = bytes(
                self._mmap.get(TEST_TABLE_OFFSET, TEST_TABLE_LENGTH))
            by_name = {item.get_name(): item for item in leaves}
            for index in range(TEST_TABLE_SLOTS):
                off = index * 4
                rx_word = _u16le(test_raw, off)
                tx_word = _u16le(test_raw, off + 2)
                if self._radio_family in ("940", "941"):
                    rx_hz = _decode_test_word_941(rx_word, 851_000_000)
                    tx_hz = _decode_test_word_941(tx_word, 812_000_000)
                else:
                    rx_hz = _decode_test_word_840(rx_word)
                    tx_hz = _decode_test_word_840(tx_word)
                for side, freq_hz in (("rx", rx_hz), ("tx", tx_hz)):
                    item = by_name.get("tk840_test_%s_%02d" %
                                       (side, index + 1))
                    if item is not None:
                        item.value.set_value(_format_test_frequency(freq_hz))
            if load_amateur_defaults is not None:
                load_amateur_defaults.value.set_value(False)

        for element in leaves:
            name = element.get_name()
            if name in ("tk840_password_enabled",
                        "tk840_programming_password"):
                continue
            if not element.changed():
                continue

            if name == "tk840_install_ham_test_frequencies":
                # One-shot action handled before per-field changes.
                continue

            if (name.startswith("tk840_test_rx_") or
                    name.startswith("tk840_test_tx_")):
                if use_amateur_defaults:
                    # The requested defaults intentionally win over stale
                    # values still displayed in the Settings dialog.
                    continue
                side = "rx" if name.startswith("tk840_test_rx_") else "tx"
                index = int(name.rsplit("_", 1)[1]) - 1
                if index < 0 or index >= TEST_TABLE_SLOTS:
                    raise errors.InvalidValueError(
                        "Invalid alignment-table slot")
                freq_hz = _parse_test_frequency(str(element.value))
                if freq_hz is None:
                    word = 0xFFFF
                elif self._radio_family in ("940", "941"):
                    base = 851_000_000 if side == "rx" else 812_000_000
                    word = _encode_test_word_941(freq_hz, base)
                else:
                    word = _encode_test_word_840(freq_hz)
                off = (TEST_TABLE_OFFSET + index * 4 +
                       (0 if side == "rx" else 2))
                self._mmap.set(off, struct.pack("<H", word))
                continue

            if name == "tk840_show_commercial":
                self._show_commercial_options = bool(element.value)
                self._save_driver_preferences()
                continue

            if name == "tk840_normalize_groups":
                self._normalize_group_numbers = bool(element.value)
                self._save_driver_preferences()
                continue

            if name == "tk840_normalize_unused":
                self._normalize_unused = bool(element.value)
                self._save_driver_preferences()
                continue

            if name == "tk840_disable_commercial":
                self._disable_commercial_features = bool(element.value)
                self._save_driver_preferences()
                continue

            if name == "tk840_aux_switch":
                value = str(element.value)
                if value not in AUX_SWITCH_CHOICES:
                    raise errors.InvalidValueError(
                        "Unsupported %s AUX Button value %r" %
                        (self._display_model, value))
                raw = self._mmap.get(GLOBAL_AUX_SWITCH, 1)[0]
                raw &= ~GLOBAL_AUX_SWITCH_MASK
                raw |= AUX_SWITCH_CHOICES.index(value)
                self._mmap.set(GLOBAL_AUX_SWITCH, bytes((raw,)))
                continue

            if name == "tk840_scan_switch":
                raw = self._mmap.get(GLOBAL_SCAN_SWITCH, 1)[0]
                value = str(element.value)
                if value == "Disabled":
                    raw |= GLOBAL_SCAN_DISABLE_MASK
                elif value == "Scan List":
                    raw &= ~GLOBAL_SCAN_DISABLE_MASK
                    raw &= ~GLOBAL_SCAN_FIXED_MASK
                elif value == "Fixed System Scan":
                    raw &= ~GLOBAL_SCAN_DISABLE_MASK
                    raw |= GLOBAL_SCAN_FIXED_MASK
                else:
                    raise errors.InvalidValueError(
                        "Unsupported %s Scan Switch value %r" %
                        (self._display_model, value))
                self._mmap.set(GLOBAL_SCAN_SWITCH, bytes((raw,)))
                continue

            power_masks = {
                "tk840_poweron_event_8c": POWERON_EVENT_8C_MASK,
                "tk840_poweron_event_8d": POWERON_EVENT_8D_MASK,
                "tk840_poweron_system_up": POWERON_SYSTEM_UP_MASK,
                "tk840_poweron_event_9e": POWERON_EVENT_9E_MASK,
            }
            if name in power_masks:
                raw = self._mmap.get(GLOBAL_POWERON_OPTIONS, 1)[0]
                mask = power_masks[name]
                if bool(element.value):
                    raw |= mask
                else:
                    raw &= ~mask
                self._mmap.set(GLOBAL_POWERON_OPTIONS, bytes((raw,)))
                continue

            if name == "tk840_dispatch_tot":
                raw = _tot_choice_to_raw(
                    str(element.value),
                    accelerated=_tot_test10x_detected(self._firmware_probe))
                if raw is not None:
                    self._mmap.set(GLOBAL_TOT_DISPATCH, bytes((raw,)))
                continue

            if name in ("tk840_scan_dropout", "tk840_scan_dwell"):
                value = str(element.value)
                if value.startswith("Unknown"):
                    continue
                try:
                    minutes, secs = [int(part) for part in value.split(":", 1)]
                except (ValueError, TypeError):
                    raise errors.InvalidValueError(
                        "%s scan delay must use M:SS format" %
                        self._display_model)
                if secs < 0 or secs >= 60:
                    raise errors.InvalidValueError(
                        "%s scan delay must use M:SS format" %
                        self._display_model)
                seconds = minutes * 60 + secs
                if seconds < 0 or seconds > 254:
                    raise errors.InvalidValueError(
                        "%s scan delay must be between 0:00 and 4:14" %
                        self._display_model)
                offset = (GLOBAL_SCAN_DROPOUT if name == "tk840_scan_dropout"
                          else GLOBAL_SCAN_DWELL)
                self._mmap.set(offset, bytes((seconds,)))
                continue

            if name in ("tk840_home_system", "tk840_home_group"):
                value = str(element.value)
                if value.startswith("Unknown"):
                    continue
                number = int(value)
                if name == "tk840_home_system":
                    if number < 1 or number > SYSTEM_COUNT:
                        raise errors.InvalidValueError(
                            "%s Home System must be 1..%d" %
                            (self._display_model, SYSTEM_COUNT))
                    offset = GLOBAL_HOME_SYSTEM
                else:
                    if number < 1 or number > 250:
                        raise errors.InvalidValueError(
                            "%s Home Channel must be "
                            "1..250" % self._display_model)
                    offset = GLOBAL_HOME_GROUP
                self._mmap.set(offset, bytes((number,)))
                continue

            if name.startswith("tk840_system_name_"):
                number = int(name.rsplit("_", 1)[1])
                # Validate against the radio's character set and force
                # upper-case even for pending settings on Systems that do not
                # yet exist.
                normalized = _clean_name(_encode_name(str(element.value)))
                pending = self._ensure_pending_system_settings()
                pending[number]["name"] = normalized
                if self._active_conventional_system(number):
                    output = _set_system_name(
                        self._mmap.get_packed(), number, normalized)
                    self._mmap.set(0, output)
                    for channel in self._channels:
                        if channel is not None and channel["system"] == number:
                            channel["system_name"] = normalized
                    system = self._system_by_number(number)
                    if system is not None:
                        system["name"] = normalized
                continue

            if name.startswith("tk840_system_scan_list_"):
                number = int(name.rsplit("_", 1)[1])
                enabled = bool(element.value)
                pending = self._ensure_pending_system_settings()
                pending[number]["scan_list_included"] = enabled
                if self._active_conventional_system(number):
                    output = _set_system_scan_list(
                        self._mmap.get_packed(), number, enabled)
                    self._mmap.set(0, output)
                    for channel in self._channels:
                        if channel is not None and channel["system"] == number:
                            channel["scan_list_included"] = enabled
                    system = self._system_by_number(number)
                    if system is not None:
                        system["scan_list_included"] = enabled
                continue

            if name.startswith("tk840_system_lockout_"):
                number = int(name.rsplit("_", 1)[1])
                enabled = bool(element.value)
                pending = self._ensure_pending_system_settings()
                pending[number]["system_lockout_included"] = enabled
                if self._active_conventional_system(number):
                    output = _set_system_lockout_included(
                        self._mmap.get_packed(), number, enabled)
                    self._mmap.set(0, output)
                    for channel in self._channels:
                        if channel is not None and channel["system"] == number:
                            channel["system_lockout_included"] = enabled
                    system = self._system_by_number(number)
                    if system is not None:
                        system["system_lockout_included"] = enabled
                        system["fixed_scan_included"] = enabled
                continue

            if name == "tk840_off_hook_decode":
                mask = GLOBAL_OFFHOOK_DECODE_MASK
            elif name == "tk840_off_hook_scan":
                mask = GLOBAL_OFFHOOK_SCAN_MASK
            else:
                continue

            raw = self._mmap.get(GLOBAL_OFFHOOK_DECODE, 1)[0]
            enabled = bool(element.value)
            if enabled:
                raw &= ~mask
            else:
                raw |= mask
            self._mmap.set(GLOBAL_OFFHOOK_DECODE, bytes((raw,)))

    def get_memory(self, number):
        mem = chirp_common.Memory()
        mem.number = number
        mem.tuning_step = 12.5

        if number < 1 or number > len(self._channels):
            mem.empty = True
            return mem

        ch = self._channels[number - 1]
        if ch is None:
            mem.empty = True
            return mem
        rx_hz = self._decode_frequency_word(ch["rx_word"])
        tx_hz = self._decode_frequency_word(ch["tx_word"])

        if rx_hz is None:
            mem.empty = True
            return mem

        mem.empty = False
        mem.freq = rx_hz
        # The display has no lower case and KPG-25D accepts it, so a stored
        # name may contain lower case: show it as the radio does, so CHIRP's
        # upper-case-only name check does not block editing the row.
        mem.name = ch["name"].upper()
        mem.skip = "" if ch["system_lockout_included"] else "S"

        if tx_hz is None:
            mem.duplex = "off"
            mem.offset = 0
        else:
            diff = tx_hz - rx_hz
            if diff < 0:
                mem.duplex = "-"
                mem.offset = abs(diff)
            elif diff > 0:
                mem.duplex = "+"
                mem.offset = diff
            else:
                mem.duplex = ""
                mem.offset = 0

        tx_tone = _decode_tone(ch["tx_tone_word"])
        rx_tone = _decode_tone(ch["rx_tone_word"])
        try:
            chirp_common.split_tone_decode(mem, tx_tone, rx_tone)
        except Exception:
            LOG.exception(
                "Unable to map tones for SYS %d GRP %d: TX=%04X RX=%04X",
                ch["system"], ch["group"],
                ch["tx_tone_word"], ch["rx_tone_word"])

        # CHIRP does not provide driver-defined main-grid columns, so TK-840
        # channel-specific fields live in Extra. Keep the ordinary ham-useful
        # fields first and reserve the Commercial: prefix for legacy functions.
        extra = RadioSettingGroup(
            "extra", "%s Channel Settings" % self._display_model)

        if self._system_edit_error is None:
            system_choices = self._system_choices()
            system_value = str(ch["system"])
            extra.append(_with_doc(RadioSetting(
                "tk840_system", "System",
                RadioSettingValueList(
                    system_choices,
                    current_index=system_choices.index(system_value))),
                "Selects conventional System 1 through 32 for this channel. "
                "This is a fundamental channel property. CHIRP has no "
                "driver-defined main-grid column for it, so it appears here. "
                "If the channel Name is blank, the radio displays its "
                "System/Group identification instead."))
        else:
            extra.append(_ro_string(
                "tk840_system", "System", ch["system"], 8))

        extra.append(_with_doc(RadioSetting(
            "tk840_tx_inhibit", "TX Inhibit",
            RadioSettingValueBoolean(
                not bool(ch["flags_a"] & FLAGA_TX_INHIBIT))),
            "Independent of Duplex. Changing RX/TX frequencies does not "
            "automatically clear TX Inhibit. Before upload, channels with an "
            "absent TX frequency (7FFF or FFFF) are forced to TX Inhibit."))

        if self._show_commercial_options:
            group_value = int(ch["group"])
            # process_mmap repairs malformed/duplicate assignments in CHIRP's
            # working image. Keep a defensive wrap here for transient records.
            if not 1 <= group_value <= MAX_GROUPS_PER_EDITABLE_SYSTEM:
                group_value = ((group_value - 1) %
                               MAX_GROUPS_PER_EDITABLE_SYSTEM) + 1
            extra.append(_with_doc(RadioSetting(
                "tk840_group_number", "Commercial: Group Number",
                RadioSettingValueInteger(
                    1, MAX_GROUPS_PER_EDITABLE_SYSTEM, group_value)),
                "Stored Group Number for this channel. Values must be unique "
                "within a System. If the requested number is already in use, "
                "the driver automatically selects the next free number."))
            extra.append(_with_doc(RadioSetting(
                "tk840_talkaround", "Commercial: Talk Around",
                RadioSettingValueBoolean(
                    not bool(ch["flags_a"] & FLAGA_TALK_AROUND))),
                "On LTR channels, this is the functional Talk Around "
                "setting. On TK-840 conventional channels, it controls "
                "the TA display icon. On TK-940/TK-941 conventional "
                "channels with Enhanced TX, it does not select TX frequency "
                "but still adds TX key-up delay, so it may be used as a "
                "visual marker or for additional VCO settling time. "
                "Automatic commercial-feature cleanup preserves this flag."))
            extra.append(_with_doc(RadioSetting(
                "tk840_data", "Commercial: Data",
                RadioSettingValueBoolean(
                    not bool(ch["flags_b"] & FLAGB_DATA))),
                "Enables this group's Kenwood data-transmission option for "
                "accessory Data PTT operation. Normally leave disabled "
                "unless using external data equipment."))
            extra.append(_with_doc(RadioSetting(
                "tk840_optional_signalling", "Commercial: Optional Signalling",
                RadioSettingValueBoolean(
                    not bool(ch["flags_a"] & FLAGA_OPTIONAL_SIGNALING))),
                "Legacy commercial optional-signalling function. Normally "
                "leave disabled for amateur use unless deliberately "
                "experimenting with the radio's signalling functions."))
            extra.append(_with_doc(RadioSetting(
                "tk840_call", "Commercial: Call",
                RadioSettingValueBoolean(
                    not bool(ch["flags_a"] & FLAGA_CALL))),
                "Legacy commercial Call function. Normally leave disabled for "
                "amateur use unless intentionally using the feature."))
            extra.append(_with_doc(RadioSetting(
                "tk840_busy_lockout", "Commercial: BCL",
                RadioSettingValueBoolean(
                    not bool(ch["flags_a"] & FLAGA_BUSY_LOCKOUT))),
                "Busy Channel Lockout. With a receive tone programmed, "
                "inhibits transmit when a carrier is present but its tone "
                "does not match the programmed receive tone. Intended for "
                "shared commercial channels and usually not useful for normal "
                "amateur operation."))
            extra.append(_with_doc(RadioSetting(
                "tk840_horn", "Commercial: Horn",
                RadioSettingValueBoolean(
                    not bool(ch["flags_a"] & FLAGA_HORN))),
                "Legacy commercial horn-alert function. Normally leave "
                "disabled for amateur use."))

        extra.append(_ro_string(
            "tk840_system_name", "System name", ch["system_name"] or "", 16))
        extra.append(_ro_bool(
            "tk840_scan_list_state", "System in Scan List",
            ch["scan_list_included"]))
        extra.append(_ro_string(
            "tk840_offset", "Image record offset",
            "0x%04X" % ch["offset"], 12))
        extra.append(_ro_string(
            "tk840_freq_words", "Raw RX/TX words",
            "%04X / %04X" % (ch["rx_word"], ch["tx_word"]), 20))
        extra.append(_ro_string(
            "tk840_pll_raster", "PLL raster", "12.5 kHz", 12))
        extra.append(_ro_string(
            "tk840_tone_words", "Raw RX/TX tone words",
            "%04X / %04X" % (ch["rx_tone_word"], ch["tx_tone_word"]), 20))
        extra.append(_ro_string(
            "tk840_flags", "Raw flags A/B",
            "%02X / %02X" % (ch["flags_a"], ch["flags_b"]), 16))
        extra.append(_ro_string(
            "tk840_unused_flags", "Firmware-unused bits",
            "A6-A7 / B1-B7", 20))
        mem.extra = extra

        return mem

    def _extra_value(self, memory, name, default=None):
        if not memory.extra:
            return default
        for setting in memory.extra:
            if setting.get_name() == name:
                return setting.value
        return default

    def _resolve_group_assignment(self, system, requested, exclude_row=None):
        """Return (System, Group Number), spilling to the next System if full.

        Group Numbers 1..250 must be unique within a System. Existing valid
        numbers are preserved where possible. Invalid/duplicate requests
        advance to the next free value and wrap at 250. A full System advances
        to the
        next System, with System 32 wrapping to System 1.
        """
        system = int(system)
        try:
            requested = int(requested)
        except (TypeError, ValueError):
            requested = 1
        # Preserve the natural wrap semantics for malformed 251..255 values.
        requested = ((requested - 1) % MAX_GROUPS_PER_EDITABLE_SYSTEM) + 1

        for system_step in range(SYSTEM_COUNT):
            candidate_system = ((system - 1 + system_step) % SYSTEM_COUNT) + 1
            members = [
                (index, channel)
                for index, channel in enumerate(self._channels)
                if (channel is not None and index != exclude_row and
                    channel["system"] == candidate_system)
            ]
            if len(members) >= MAX_GROUPS_PER_EDITABLE_SYSTEM:
                continue
            used = {
                int(channel["group"]) for _index, channel in members
                if 1 <= int(channel["group"]) <= MAX_GROUPS_PER_EDITABLE_SYSTEM
            }
            for step in range(MAX_GROUPS_PER_EDITABLE_SYSTEM):
                candidate_group = ((requested - 1 + step) %
                                   MAX_GROUPS_PER_EDITABLE_SYSTEM) + 1
                if candidate_group not in used:
                    if (candidate_system != system or
                            candidate_group != requested):
                        LOG.info(
                            "%s requested System %d Group %d; using System %d "
                            "Group %d",
                            self._display_model, system, requested,
                            candidate_system, candidate_group)
                    return candidate_system, candidate_group

        # With at most 308 physical channel records this should be unreachable,
        # but keep a defensive error for corrupt/internal state.
        raise errors.InvalidValueError(
            "No usable System/Group assignment is available")

    def _repair_group_layout(self):
        """Repair invalid/duplicate Group Numbers and spill Systems over 250.

        The first valid occurrence of a Group Number is preserved. Later
        duplicates and values outside 1..250 take the next free number. Records
        beyond 250 in one System roll into the next System, wrapping 32 -> 1.
        Physical record order is preserved.
        """
        active_rows = [
            (index, channel) for index, channel in enumerate(self._channels)
            if channel is not None
        ]
        if not active_rows:
            return False

        used = {number: set() for number in range(1, SYSTEM_COUNT + 1)}
        counts = {number: 0 for number in range(1, SYSTEM_COUNT + 1)}
        assignments = []
        overrides = {}
        changed = False
        current = self._mmap.get_packed()

        for active_index, (row_index, channel) in enumerate(active_rows):
            original_system = int(channel["system"])
            original_group = int(channel["group"])
            target_system = original_system

            for system_step in range(SYSTEM_COUNT):
                candidate_system = (
                    (original_system - 1 + system_step) % SYSTEM_COUNT) + 1
                if counts[candidate_system] < MAX_GROUPS_PER_EDITABLE_SYSTEM:
                    target_system = candidate_system
                    break
            else:
                raise errors.InvalidValueError(
                    "No usable System is available for channel repair")

            requested = ((original_group - 1) %
                         MAX_GROUPS_PER_EDITABLE_SYSTEM) + 1
            target_group = None
            for step in range(MAX_GROUPS_PER_EDITABLE_SYSTEM):
                candidate_group = ((requested - 1 + step) %
                                   MAX_GROUPS_PER_EDITABLE_SYSTEM) + 1
                if candidate_group not in used[target_system]:
                    target_group = candidate_group
                    break
            if target_group is None:
                # A count below 250 guarantees a free number, so this indicates
                # inconsistent internal state rather than a radio limitation.
                raise errors.InvalidValueError(
                    "System %d has no free Group Number" % target_system)

            used[target_system].add(target_group)
            counts[target_system] += 1
            assignments.append(target_system)

            if (target_system != original_system or
                    target_group != original_group):
                rec = bytearray(current[
                    channel["offset"]:channel["offset"] + CONV_RECORD_SIZE])
                rec[CH_GROUP] = target_group
                overrides[active_index] = bytes(rec)
                changed = True
                LOG.warning(
                    "%s repaired row %d: System %d Group %d -> System %d "
                    "Group %d",
                    self._display_model, row_index + 1, original_system,
                    original_group, target_system, target_group)

        if not changed:
            return False

        channels = [channel for _row, channel in active_rows]
        output, offsets = _repack_conventional_systems(
            current, channels, assignments, record_overrides=overrides)
        output = self._apply_pending_system_settings(output)
        logical_offsets = [None] * MAX_LOGICAL_MEMORIES
        for (row_index, _channel), offset in zip(active_rows, offsets):
            logical_offsets[row_index] = offset
        self._mmap.set(0, output)
        self._refresh_channel_cache(output, logical_offsets)
        return True

    def _record_for_new_memory(self, memory):
        """Build a new record while preserving copied hidden/raw state."""
        template = None
        # For CHIRP Insert Rows Above, the first insertion copies the previous
        # occupied row downward, so use it as the source for hidden/raw state
        # when CHIRP is shifting an existing record.
        for index in range(memory.number - 2, -1, -1):
            if self._channels[index] is not None:
                ch = self._channels[index]
                template = bytearray(self._mmap.get(
                    ch["offset"], CONV_RECORD_SIZE))
                break
        if template is None:
            template = bytearray(b"\xFF" * CONV_RECORD_SIZE)

        # A copied populated Memory carries these read-only diagnostics even if
        # commercial fields are hidden. Preserve exact source flags and
        # frequency high bits when CHIRP shifts an existing row.
        raw_flags = str(self._extra_value(memory, "tk840_flags", ""))
        try:
            left, right = [x.strip() for x in raw_flags.split("/", 1)]
            template[CH_FLAGS_A] = int(left, 16) & 0xFF
            template[CH_FLAGS_B] = int(right, 16) & 0xFF
        except (ValueError, TypeError):
            # A genuinely new blank channel defaults to all optional/commercial
            # functions disabled and unused bits high.
            template[CH_FLAGS_A] |= (
                FLAGA_COMMERCIAL | FLAGA_TALK_AROUND |
                FLAGA_TX_INHIBIT | FLAGA_UNUSED)
            template[CH_FLAGS_B] |= (FLAGB_DATA | FLAGB_UNUSED)

        raw_freqs = str(self._extra_value(memory, "tk840_freq_words", ""))
        try:
            left, right = [x.strip() for x in raw_freqs.split("/", 1)]
            _put_u16le(template, CH_RX_FREQ, int(left, 16) & 0xFFFF)
            _put_u16le(template, CH_TX_FREQ, int(right, 16) & 0xFFFF)
        except (ValueError, TypeError):
            # A genuinely new channel gets the canonical KPG/TK-840 value for
            # the firmware-unused frequency high bits. Existing/copied rows
            # retain their exact raw words through the diagnostic path above.
            _put_u16le(
                template, CH_RX_FREQ,
                _u16le(template, CH_RX_FREQ) | self._frequency_unused_mask)
            _put_u16le(
                template, CH_TX_FREQ,
                _u16le(template, CH_TX_FREQ) | self._frequency_unused_mask)
        return template

    def _encode_memory_into_record(self, memory, rec, current_system):
        rec = bytearray(rec)
        rx_hz = int(memory.freq)
        if memory.duplex == "off":
            tx_hz = None
        elif memory.duplex == "split":
            tx_hz = int(memory.offset)
        elif memory.duplex == "+":
            tx_hz = rx_hz + int(memory.offset)
        elif memory.duplex == "-":
            tx_hz = rx_hz - int(memory.offset)
        elif memory.duplex == "":
            tx_hz = rx_hz
        else:
            raise errors.InvalidValueError(
                "Unsupported %s duplex mode %r" %
                (self._display_model, memory.duplex))

        _put_u16le(rec, CH_RX_FREQ,
                   self._encode_frequency_word(
                       rx_hz, _u16le(rec, CH_RX_FREQ)))
        _put_u16le(rec, CH_TX_FREQ,
                   self._encode_frequency_word(
                       tx_hz, _u16le(rec, CH_TX_FREQ)))
        tx_tone, rx_tone = chirp_common.split_tone_encode(memory)
        _put_u16le(rec, CH_RX_TONE, _encode_tone(rx_tone))
        _put_u16le(rec, CH_TX_TONE, _encode_tone(tx_tone))
        rec[CH_NAME:CH_NAME + 8] = _encode_name(memory.name)

        target_system = current_system
        masks_a = {
            "tk840_optional_signalling": FLAGA_OPTIONAL_SIGNALING,
            "tk840_talkaround": FLAGA_TALK_AROUND,
            "tk840_tx_inhibit": FLAGA_TX_INHIBIT,
            "tk840_call": FLAGA_CALL,
            "tk840_busy_lockout": FLAGA_BUSY_LOCKOUT,
            "tk840_horn": FLAGA_HORN,
        }
        masks_b = {"tk840_data": FLAGB_DATA}
        if memory.extra:
            for setting in memory.extra:
                name = setting.get_name()
                if name == "tk840_system" and self._system_edit_error is None:
                    value = str(setting.value)
                    if value not in self._system_choices():
                        raise errors.InvalidValueError(
                            "Unsupported %s System value %r" %
                            (self._display_model, value))
                    target_system = int(value)
                    continue
                if name == "tk840_group_number":
                    rec[CH_GROUP] = int(setting.value) & 0xFF
                    continue
                mask = masks_a.get(name)
                if mask is not None:
                    if bool(setting.value):
                        rec[CH_FLAGS_A] &= ~mask
                    else:
                        rec[CH_FLAGS_A] |= mask
                    continue
                mask = masks_b.get(name)
                if mask is not None:
                    if bool(setting.value):
                        rec[CH_FLAGS_B] &= ~mask
                    else:
                        rec[CH_FLAGS_B] |= mask
        # The on-radio TX Inhibit flag is independent of CHIRP Duplex.
        # Do not silently set or clear it when editing a frequency. The
        # upload policy pass enforces an inhibit only if TX is unprogrammed.
        return rec, target_system

    def _add_memory(self, memory):
        """Create a conventional channel in an empty logical CHIRP row."""
        if self._system_edit_error is not None:
            raise errors.InvalidValueError(
                "%s channel insertion requires the editable System layout: "
                "%s" %
                (self._display_model, self._system_edit_error))
        if memory.skip not in ("", "S"):
            raise errors.InvalidValueError(
                "%s Skip must be blank or S" % self._display_model)

        default_system = 1
        raw_system = self._extra_value(memory, "tk840_system", None)
        if raw_system is not None:
            try:
                default_system = int(str(raw_system))
            except ValueError:
                default_system = 1

        rec = self._record_for_new_memory(memory)
        rec, target_system = self._encode_memory_into_record(
            memory, rec, default_system)
        raw_group = self._extra_value(memory, "tk840_group_number", None)
        if raw_group is None:
            # New/copy-pasted rows may not carry the commercial-only field.
            # Prefer the next number after the highest valid one already used.
            existing_groups = [
                int(ch["group"]) for ch in self._channels
                if (ch is not None and ch["system"] == target_system and
                    1 <= int(ch["group"]) <= MAX_GROUPS_PER_EDITABLE_SYSTEM)
            ]
            requested_group = ((max(existing_groups) %
                                MAX_GROUPS_PER_EDITABLE_SYSTEM) + 1
                               if existing_groups else 1)
        else:
            requested_group = int(raw_group)
        target_system, resolved_group = self._resolve_group_assignment(
            target_system, requested_group)
        rec[CH_GROUP] = resolved_group

        rows = []
        channels = []
        assignments = []
        overrides = {}
        new_active_index = None
        # The synthetic row still carries a plausible source System number for
        # bookkeeping. A genuinely new destination gets a clean conventional
        # header in the repacker and then receives any pending System settings.
        active_systems = {s["number"] for s in self._systems
                          if not s["unused"] and s["count"] and
                          s.get("ltr") is False}
        target_was_active = target_system in active_systems
        fallback_system = min(active_systems) if active_systems else 1
        source_system = (target_system if target_was_active
                         else fallback_system)

        for row_index, channel in enumerate(self._channels):
            if row_index == memory.number - 1:
                new_active_index = len(channels)
                rows.append(row_index)
                channels.append({"offset": 0, "system": source_system})
                assignments.append(target_system)
                overrides[new_active_index] = bytes(rec)
            if channel is not None:
                rows.append(row_index)
                channels.append(channel)
                assignments.append(channel["system"])

        if new_active_index is None:
            raise errors.InvalidMemoryLocation(
                "%s insertion row is outside the supported range" %
                self._display_model)

        current = self._mmap.get_packed()
        assignments, header_sources, _renumber = (
            _compact_system_assignments(current, assignments))
        final_system = assignments[new_active_index]
        output, offsets = _repack_conventional_systems(
            current, channels, assignments,
            header_sources=header_sources, record_overrides=overrides)
        # Activate any preconfigured settings for a newly used System. Existing
        # destination Systems retain their settings; CHIRP Skip may still edit
        # their System Lockout state in the normal way.
        output = self._apply_pending_system_settings(output)
        if target_was_active:
            output = _set_system_lockout_included(
                output, final_system, memory.skip != "S")

        logical_offsets = [None] * MAX_LOGICAL_MEMORIES
        for row_index, offset in zip(rows, offsets):
            logical_offsets[row_index] = offset
        self._mmap.set(0, output)
        self._refresh_channel_cache(output, logical_offsets)

    def _delete_memory(self, number):
        """Delete one logical row and safely repack all retained records."""
        row_index = number - 1
        if self._channels[row_index] is None:
            return
        if self._system_edit_error is not None:
            raise errors.InvalidValueError(
                "%s channel deletion requires the conventional System layout: "
                "%s" %
                (self._display_model, self._system_edit_error))

        active_rows = [
            (index, channel)
            for index, channel in enumerate(self._channels)
            if channel is not None
        ]
        if len(active_rows) <= 1:
            raise errors.InvalidValueError(
                "The last %s channel cannot be deleted" % self._display_model)

        retained_rows = [
            (index, channel) for index, channel in active_rows
            if index != row_index
        ]
        retained_channels = [channel for _, channel in retained_rows]

        # Real TK-840 System numbers may be sparse. If deletion empties a
        # System, leave that System number unprogrammed rather than renumbering
        # later Systems.
        target_systems = [
            channel["system"] for channel in retained_channels]

        current = self._mmap.get_packed()
        output, retained_offsets = _repack_conventional_systems(
            current, retained_channels, target_systems)
        output = self._apply_pending_system_settings(output)
        _validate_system_repack(current, output)

        logical_offsets = [None] * MAX_LOGICAL_MEMORIES
        for (index, _channel), offset in zip(
                retained_rows, retained_offsets):
            logical_offsets[index] = offset

        self._mmap.set(0, output)
        self._refresh_channel_cache(output, logical_offsets)

    def set_memory(self, memory):
        """Patch fields in an existing conventional record.

        Preserve existing RX/TX bit 15 by default on all models. Newly
        created channels use the canonical high value for unused bits.
        Duplex off writes 7FFF in the TX word. TX Inhibit is an
        independent Extra setting and is preserved during memory edits;
        upload enforces TX Inhibit for missing TX frequencies.
        """
        if memory.number < 1 or memory.number > MAX_LOGICAL_MEMORIES:
            raise errors.InvalidMemoryLocation(
                "%s memory must be between 1 and %d" %
                (self._display_model, MAX_LOGICAL_MEMORIES))
        if memory.empty:
            self._delete_memory(memory.number)
            return

        ch = self._channels[memory.number - 1]
        if ch is None:
            self._add_memory(memory)
            return
        if memory.skip not in ("", "S"):
            raise errors.InvalidValueError(
                "%s Skip must be blank or S" % self._display_model)

        off = ch["offset"]
        rec = bytearray(self._mmap.get(off, CONV_RECORD_SIZE))
        rec, target_system = self._encode_memory_into_record(
            memory, rec, ch["system"])
        target_system, resolved_group = self._resolve_group_assignment(
            target_system, rec[CH_GROUP], exclude_row=memory.number - 1)
        rec[CH_GROUP] = resolved_group

        # Work transactionally so a rejected system move leaves the image
        # completely untouched. Group Number and protected flag bits travel
        # with their record during a repack.
        working = bytearray(self._mmap.get_packed())
        working[off:off + CONV_RECORD_SIZE] = rec
        logical_offsets = [
            None if channel is None else channel["offset"]
            for channel in self._channels
        ]
        active_rows = [
            (index, channel)
            for index, channel in enumerate(self._channels)
            if channel is not None
        ]
        active_channels = [channel for _, channel in active_rows]
        active_index = next(
            index
            for index, (logical_index, _channel) in enumerate(active_rows)
            if logical_index == memory.number - 1)

        final_target_system = target_system
        if target_system != ch["system"]:
            assignments = [channel["system"] for channel in active_channels]
            assignments[active_index] = target_system

            # System numbers are sparse-capable; preserve the requested
            # destination exactly even if moving the last channel leaves a
            # hole.
            assignments, header_sources, renumber = (
                _compact_system_assignments(bytes(working), assignments))
            final_target_system = assignments[active_index]
            output, active_offsets = _repack_conventional_systems(
                bytes(working), active_channels, assignments,
                header_sources=header_sources)
            output = self._apply_pending_system_settings(output)
            for (logical_index, _channel), new_offset in zip(
                    active_rows, active_offsets):
                logical_offsets[logical_index] = new_offset
        else:
            output = bytes(working)

        # CHIRP presents Skip per row, but System Lockout is shared by the
        # whole System. When a row is merely edited in place, honor Skip. When
        # moving it to another System, preserve that destination System's own
        # setting (including a session-only value configured before activation)
        # rather than copying the source System's Skip state into it.
        if final_target_system == ch["system"]:
            output = _set_system_lockout_included(
                output, final_target_system, memory.skip != "S")

        self._mmap.set(0, output)
        self._refresh_channel_cache(output, logical_offsets)

    def get_raw_memory(self, number):
        if number < 1 or number > len(self._channels):
            return "<empty/out of range>"
        ch = self._channels[number - 1]
        if ch is None:
            return "<empty/deleted>"
        raw = self._mmap.get(ch["offset"], CONV_RECORD_SIZE)
        return (
            "SYS=%d GROUP=%d OFFSET=0x%04X NAME=%r RAW=%s" %
            (ch["system"], ch["group"], ch["offset"], ch["name"],
             util.hexprint(raw)))

    @classmethod
    def match_model(cls, filedata, filename):
        # Metadata-free raw images are accepted when their family can be
        # identified. Normal CHIRP .img files use CloneModeRadio metadata for
        # exact model selection.
        try:
            if not _looks_like_supported_image(filedata):
                return False
            return _saved_image_family_hint(filedata) == "840"
        except Exception:
            return False


@directory.register


class TK940Radio(TK840Radio):
    """Kenwood TK-940 using the shared TK-940/TK-941 configuration format."""

    MODEL = "TK-940"
    _radio_family = "940"
    _display_model = "TK-940"
    # The radio's actual inclusive upper limit is 999.987500 MHz. Current
    # CHIRP treats valid_bands upper endpoints as exclusive.
    _valid_bands = [(851_000_000, 999_987_501)]
    _frequency_unused_mask = FREQ_UNUSED_MASK
    _normalize_unused_supported = True
    _normalize_unused = True
    _disable_commercial_features = True
    _has_ham_test_defaults = True

    @classmethod
    def match_model(cls, filedata, filename):
        # TK-940 and TK-941 raw codeplugs have no model discriminator. Use
        # TK-940 as the canonical fallback for metadata-free 94x images.
        try:
            if not _looks_like_supported_image(filedata):
                return False
            return _saved_image_family_hint(filedata) == "94x"
        except Exception:
            return False

    @classmethod
    def get_prompts(cls):
        return _clone_prompts(cls._display_model, True)

    def _decode_frequency_word(self, word):
        return _decode_frequency_941(word)

    def _encode_frequency_word(self, freq_hz, old_word):
        return _encode_frequency_941(freq_hz, old_word)

    def _ham_test_table(self):
        return _build_test_table_941(HAM_TEST_941)

    def get_features(self):
        rf = super().get_features()
        rf.valid_bands = list(self._valid_bands)
        return rf


@directory.register


class TK941Radio(TK840Radio):
    """Kenwood TK-941 using the shared TK-940/TK-941 configuration format."""

    MODEL = "TK-941"
    _radio_family = "941"
    _display_model = "TK-941"
    # The radio's actual inclusive upper limit is 999.987500 MHz. Current
    # CHIRP treats valid_bands upper endpoints as exclusive.
    _valid_bands = [(851_000_000, 999_987_501)]
    _frequency_unused_mask = FREQ_UNUSED_MASK
    _normalize_unused_supported = True
    _normalize_unused = True
    _disable_commercial_features = True
    _has_ham_test_defaults = True

    @classmethod
    def match_model(cls, filedata, filename):
        # A raw 94x image cannot distinguish TK-941 from TK-940. CHIRP-native
        # .img metadata selects TK-941 exactly, so do not claim untagged files.
        return False

    @classmethod
    def get_prompts(cls):
        return _clone_prompts(cls._display_model, True)

    def _decode_frequency_word(self, word):
        return _decode_frequency_941(word)

    def _encode_frequency_word(self, freq_hz, old_word):
        return _encode_frequency_941(freq_hz, old_word)

    def _ham_test_table(self):
        return _build_test_table_941(HAM_TEST_941)

    def get_features(self):
        rf = super().get_features()
        rf.valid_bands = list(self._valid_bands)
        return rf
