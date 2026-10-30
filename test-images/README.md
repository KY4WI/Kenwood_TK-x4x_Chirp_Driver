# Test images

These are synthetic codeplug images created specifically to exercise the TK-x4x CHIRP driver. They contain no callsigns or programming passwords.

Each file is a CHIRP-saved image: the 6144-byte codeplug followed by CHIRP's metadata trailer, which names the model. That tag is how CHIRP tells a TK-941 image from a TK-940 one, since the codeplugs themselves have no model marker. Every channel and System name uses only characters the radio accepts.

## feature/

Sparse-System feature torture tests. They contain 36 programmed channels and 272 empty logical memories, with active Systems 1, 2, 7, 16, and 32. They exercise frequency/raster edges, simplex and split operation, RX-only channels, CTCSS, normal/reverse DCS, cross tones, name edge cases, scan/lockout states, TX inhibit, commercial feature flags, global-setting boundaries, and sparse System layout.

## dense/

High-density development stress tests. They contain 270 programmed channels and 38 empty logical memories and deliberately put substantial pressure on the packed System-data area. Every programmed channel uses unique frequency/name/tone combinations, with other settings mixed independently.

The dense set activates all 32 Systems. It is useful for capacity and packing tests; the upstream set below is built from it.

## upstream/

The set intended for CHIRP's `tests/images/` when the driver is submitted, under the file names CHIRP expects. It keeps the dense set's 270 channels but leaves System numbers 6, 13, 19, 24 and 29 unused, so density and sparse System numbers are tested together. It was built from the dense set through the driver itself, moving those Systems' channels into their neighbours; every channel's frequencies, tones, name and flags are unchanged.

Within each set the TK-940 and TK-941 codeplugs are intentionally byte-identical; the files differ only in the model tag.

TK-940/941 fixtures use the **stock 851 MHz base** and 12.5 kHz channel raster (only the lower 15 bits carry frequency). Receive-only 94x records also enable the active-low TX Inhibit flag because stock firmware does not obey an empty stored TX word.

CI runs CHIRP's own driver test suite against all three sets (`.github/workflows/ci.yml`).

Copyright (c) 2026 Dave, KY4WI. Licensed under GPL-3.0-or-later.
