Kenwood TK-x4x CHIRP torture-test images
Generated for CHIRP driver testing; no callsigns or passwords included.

Each image contains 36 conventional channels and 272 empty logical memories.
Active sparse Systems: 1, 2, 7, 16, 32.
Group Numbers exercise: 1, 2, 7, 42, 99, 128, 200, 250.
Exercises: band/raster edges, simplex, positive/negative and unusual offsets,
RX-only, CTCSS, DCS normal/reverse, cross-tone combinations, blank/8-char/
punctuation names, System lockout/scan-list states, TX inhibit, and each
commercial per-channel flag. Global settings also use several boundary values.

Each file is a CHIRP-saved image: the 6144-byte codeplug followed by
CHIRP's metadata trailer, which names the model. The TK-940 and TK-941
codeplugs are byte-identical; the files differ only in that model tag.

Four names that used '>' (not in the radio's character set) were changed
to '-' on 2026-10-16: TN-D0017, D445-018, TN-D0029, D445-030.

Kenwood_TK-840.img: 6301 bytes  SHA256 8c9c1b8445fc267ca8285e1a58bcada4c23e22bc0646ff86293559a5d3652d4d
  codeplug (first 6144 bytes): SHA256 b615bedd4e44edf196a2d6b18b1e51a8562113fe0dcabafe48364b35fb5e36fa
Kenwood_TK-940.img: 6301 bytes  SHA256 be19211a2ae7779816b3fa4cabfa5e7a0a0565347ba9486dfafa84c3b2c5539d
  codeplug (first 6144 bytes): SHA256 5b241001224e3d24fd94931fa5286a9351fa954b22293ace0932906632977726
Kenwood_TK-941.img: 6301 bytes  SHA256 61db4d4e4ced781388a436bc7e30631073795b456c69299ac5bf23ddbdfa66df
  codeplug (first 6144 bytes): SHA256 5b241001224e3d24fd94931fa5286a9351fa954b22293ace0932906632977726

TK-940/941 fixtures were updated for the corrected 851 MHz stock firmware base:
synthetic channel frequencies previously below 851 MHz were shifted +100 MHz,
and TX-inhibited receive-only channels were marked accordingly.

Copyright (c) 2026 Dave, KY4WI. Licensed under GPL-3.0-or-later.
