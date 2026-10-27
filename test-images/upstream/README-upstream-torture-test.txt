Kenwood TK-x4x CHIRP upstream torture-test images
The set intended for CHIRP's tests/images/ when the driver is submitted.
The file names are the ones CHIRP expects.

Built on 2026-10-16 from the dense set (dense/) through the driver itself:
the channels of Systems 6, 13, 19, 24 and 29 were moved with the driver's
System setting into Systems 7, 12, 18, 23 and 28. That leaves those five
System numbers unused (holes in the pointer and count tables) while
keeping the dense set's 270 channels, so packing pressure and sparse
System numbers are tested together.

Each image contains 270 conventional channels and 38 empty logical memories.
Active Systems: 27 (1-5, 7-12, 14-18, 20-23, 25-28, 30-32).
Unused System numbers: 6, 13, 19, 24, 29. System 1 and System 32 are active,
and Home / Fixed-Revert stays on System 32 Group 45.
Packed System data uses 5508 of 5888 bytes, leaving 380 bytes free.

Every channel's frequencies, tones, name and feature flags are the same as
in the dense set; only System assignment and physical position changed.

Each file is a CHIRP-saved image: the 6144-byte codeplug followed by
CHIRP's metadata trailer, which names the model. The TK-940 and TK-941
codeplugs are byte-identical; the files differ only in that model tag.

CHIRP driver tests (tests/test_drivers.py) with this set: 105 passed, 0
failed (2026-10-16).

Kenwood_TK-840.img: 6301 bytes  SHA256 a0c4673e0761d9363c2c79204aacea16395074fe979fa9761c40905b1206e993
  codeplug (first 6144 bytes): SHA256 233be7e3b1e1640ab71802e693e3b2f02ab7e6b99320ec8ca38b02163895d92f
Kenwood_TK-940.img: 6301 bytes  SHA256 cf131b018f6537d4f008432474549ce2e3e710a4cc4b358c9d3ecdaec75b90af
  codeplug (first 6144 bytes): SHA256 9985841915162c3e4529a9b2e8466e79867e7248ba12a1411ae6fc8bdbe392f1
Kenwood_TK-941.img: 6301 bytes  SHA256 395aeced4c86d3d599b8062cdb7de7bdad947b11048ba6ae1dd660950b743c40
  codeplug (first 6144 bytes): SHA256 9985841915162c3e4529a9b2e8466e79867e7248ba12a1411ae6fc8bdbe392f1

TK-940/941 fixtures were updated for the corrected 851 MHz stock firmware base:
synthetic channel frequencies previously below 851 MHz were shifted +100 MHz,
and TX-inhibited receive-only channels were marked accordingly.

Copyright (c) 2026 Dave, KY4WI. Licensed under GPL-3.0-or-later.
