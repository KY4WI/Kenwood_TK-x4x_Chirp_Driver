Kenwood TK-x4x CHIRP dense torture-test images
The upstream/ set (with System-number holes) is built from these.
Generated to complement the sparse/feature torture-test images.

Each image contains 270 conventional channels and 38 empty logical memories.
All 32 Systems are active. System sizes are deliberately uneven:
  80, 50, 30, 20, 15, 10, 8, 6, 5, 4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1

Packed System data uses 5578 of 5888 bytes, leaving 310 bytes free.
Every populated channel has a unique RX frequency, unique 8-character name, and unique
TX/RX tone-pair combination. Other settings are mixed independently across channels.

Exercises include: all 32 Systems, very uneven System sizes, Group Numbers spread through
1..250, System lockout/scan-list combinations, 270 distinct 12.5-kHz raster frequencies,
simplex, RX-only, +/- one-step and several ordinary/odd splits, CTCSS, normal/reverse DCS,
cross-tone combinations, all combinations of the seven known per-channel feature flags,
punctuation-heavy unique names, global-setting edge values, and all ten alignment slots.

Each file is a CHIRP-saved image: the 6144-byte codeplug followed by
CHIRP's metadata trailer, which names the model. The TK-940 and TK-941
codeplugs are byte-identical; the files differ only in that model tag.

Validation with tk840_RC02.py:
  Kenwood_TK-840_dense.img: family=840, channels=270, systems=32, tone-fields=540
  Kenwood_TK-940_dense.img: family=94x, channels=270, systems=32, tone-fields=540
  Kenwood_TK-941_dense.img: family=94x, channels=270, systems=32, tone-fields=540

Kenwood_TK-840_dense.img: 6301 bytes  SHA256 64cad27d7aead2240f369b68c23a55aa13092ef510bcd92c50cb5d8c81b7347c
  codeplug (first 6144 bytes): SHA256 2092bcf935019844c4788e2a62a2dce5fa633075c5457681be0f31f451d5e4f8
Kenwood_TK-940_dense.img: 6301 bytes  SHA256 2d09d80d892197905a97d2ba2768231d9a61a28f041f3e7cbb3f37382a9613d4
  codeplug (first 6144 bytes): SHA256 8edab505211609826cd8c2440e8e70ffd4299d8f52669ca96aad7572c96d6394
Kenwood_TK-941_dense.img: 6301 bytes  SHA256 010e59cf66145d78f7b78e3411a9112aeb6233817dae99639e5e241c126b908e
  codeplug (first 6144 bytes): SHA256 8edab505211609826cd8c2440e8e70ffd4299d8f52669ca96aad7572c96d6394

TK-940/941 fixtures were updated for the corrected 851 MHz stock firmware base:
synthetic channel frequencies previously below 851 MHz were shifted +100 MHz,
and TX-inhibited receive-only channels were marked accordingly.

Copyright (c) 2026 Dave, KY4WI. Licensed under GPL-3.0-or-later.
