# Kenwood TK-x4x CHIRP Driver

CHIRP driver for the **Kenwood TK-840, TK-940, and TK-941**.

Copyright (c) 2026 Dave, KY4WI.

**Current version: v1.0.0-beta.1**

This driver provides conventional channel programming, radio configuration
download/upload, programming-password authentication, System and global
settings, commercial channel options, and dealer/panel alignment-frequency
editing. Configuration read/write and supported firmware-feature detection
have been tested on real radios.

## Supported radios

- Kenwood TK-840
- Kenwood TK-940
- Kenwood TK-941

The TK-840 has different frequency encoding from the TK-940/TK-941 family.
The TK-940 and TK-941 share a codeplug and firmware format, but retain
separate names in CHIRP and have different RF hardware. CHIRP-saved images
retain the selected model in their metadata.

**TK-840N:** Not supported by this CHIRP driver. Its programming behavior and
model-specific differences have not been verified; do not assume TK-840
channel programming or firmware modifications apply to it.

## Programming cable

A DIY KPG-4 programming interface can be built using a USB-to-TTL serial
adapter. A wiring diagram is provided below.

<img src="DIY_KPG-4_Programming_Interface.png" alt="DIY KPG-4 programming interface wiring diagram" width="500">

**3.3 V adapters:** A USB-to-TTL adapter using 3.3 V logic should also work
if its RX input is 5 V tolerant. I've successfully used a 3.3 V adapter
with four different radios. Check the adapter's RX voltage tolerance
before connecting it.

## How to install this driver

Until this driver is included in an official CHIRP release, it can be installed manually.

### Install it like a built-in driver

The simplest permanent method is to copy `tk840.py` into CHIRP's existing driver directory, alongside the other radio driver files. The directory is normally named:

`chirp/drivers/`

For example, if you can already see files such as `tk760.py`, `tk8180.py`, and other radio drivers, you are in the right place.

1. Close CHIRP.
2. Download `tk840.py` from this repository.
3. Copy it into CHIRP's `chirp/drivers/` directory.
4. Start CHIRP again.

CHIRP will then load it like one of its built-in drivers. This adds three Kenwood radio choices to the normal radio-selection menus:

- **Kenwood TK-840**
- **Kenwood TK-940**
- **Kenwood TK-941**

This method survives normal CHIRP restarts, but a CHIRP upgrade may replace the program directory, so you may need to copy the driver back afterward.

### Temporary loading with Developer Mode

If you do not want to modify CHIRP's installed files, CHIRP can also load the driver temporarily:

1. Start CHIRP.
2. Open **Help -> Enable Developer Mode** and enable it.
3. Restart CHIRP.
4. Open **File -> Load Module**.
5. Select the downloaded `tk840.py` file.

The same three Kenwood models will then appear in CHIRP's radio menus. A manually loaded module is active only for the current CHIRP session, so it must be loaded again after restarting CHIRP.

Developer Mode allows CHIRP to load external Python driver code, so only load driver files from sources you trust.

When this driver is eventually included with CHIRP, neither manual installation method will be necessary.

## Programming notes

### TK-940/TK-941 frequencies

CHIRP supports conventional frequencies from **851.000 to 999.9875 MHz**
on these models, using their 12.5 kHz frequency-word format. Frequencies
below 851 MHz cannot be represented correctly by this format. CHIRP
shows the **stored RX and TX frequencies**, which may differ from the
actual transmit frequency on radios running stock or legacy-patched firmware.

### Receive-only channels and TX Inhibit

**Duplex: off** indicates that a channel has no programmed TX frequency.
However, an absent TX frequency does not by itself guarantee that the radio
cannot transmit, particularly with stock TK-940/TK-941 firmware.

**TX Inhibit** is a separate channel option under **Extra**. Changing Duplex
while editing does not silently change TX Inhibit. Before an upload, CHIRP
forces TX Inhibit on for conventional channels without a TX frequency;
channels with valid TX frequencies keep their existing TX Inhibit setting.
If you later change a receive-only channel back to simplex or split,
you may need to clear TX Inhibit manually.

### Talk Around (TA)

On TK-840 conventional channels, the TA flag controls the **TA display
indicator**, not the transmit frequency. On TK-940/TK-941 conventional channels
with the Enhanced TX Patch, it also no longer selects the transmit frequency,
so the indicator can be used as a visual marker.

**TK-940/TK-941 TX delay:** Enabling TA also adds a delay before transmission.
This was intended to provide additional VCO settling time for the larger
frequency changes in the radios' original configurations. It may help in some
cases, although whether the extra delay is beneficial for typical amateur-band
frequency splits is uncertain. This delay is separate from the TA indicator
and from the Enhanced TX Patch's transmit-frequency handling.

**LTR channels retain functional Talk Around behavior**, so do not use TA as
an arbitrary marker there. Unpatched TK-940/TK-941 conventional channels
retain their stock transmit-frequency behavior.

### Data option

The per-channel **Data** option enables or disables data transmission for
that group, as documented in Kenwood's TK-840/(N) service manual (section
3-25). Data PTT uses the accessory connector, and QT/DQT is configured
separately. CHIRP preserves the option under commercial channel settings.

## Firmware Patch Compatibility

CHIRP works with stock firmware, recognized legacy patches, and the
**Enhanced Firmware Patches**. The driver edits radio configuration,
**not firmware**. It can identify supported patches with a small read-only
firmware check and report their status under **Settings -> Firmware Features**.

### Legacy patches

- **+25 MHz Offset Patch (TK-940/TK-941):** Replaces the stock fixed TX
  offset with +25 MHz. It still ignores the programmed TX-frequency field.
- **Legacy TOT Bypass:** Bypasses the transmit time-out timer instead of
  offering a selectable Off setting.

### Enhanced patches

- **Enhanced TX Patch (TK-940/TK-941):** Stock firmware ignores the channel's
  programmed TX frequency and calculates transmit frequency from RX using a
  fixed offset (-45 MHz on TK-940, -39 MHz on TK-941). The Enhanced TX Patch
  **honors the TX frequency entered in CHIRP**. Program RX and TX normally:
  no more offset programming games or workaround channel entries. The TK-840
  already honors its TX field. Conventional TX on patched TK-940/TK-941
  must be **851 MHz or higher**; this patch does not cover the TK-940's
  original 806-825 MHz transmit band. LTR operation is unaffected.
- **Enhanced TOT Patch (TK-840/TK-940/TK-941):** Adds a selectable **Off**
  setting to the Dispatch transmit time-out timer while retaining its normal
  timed choices. Telephone TOT is unaffected. Unlike the legacy bypass,
  the timer can still be enabled with a normal duration.

**After installing Enhanced TX:** Review every transmit-capable channel's
TX frequency before transmitting. Stock firmware ignored that field, so
existing values may be unsuitable. For simplex, program TX equal to RX.
For receive-only channels, use **Duplex: off** and retain TX Inhibit.

**TOT Off requires Enhanced TOT firmware.** CHIRP displays
`Off (Enhanced TOT Patch only)` even with stock or unverified firmware.
On stock firmware, that stored value does **not** disable the timer.
Select it only after confirming the Enhanced TOT Patch.

### Firmware-feature reporting

The patch check does not download or write a full firmware image. If a
revision or modification cannot be verified, CHIRP reports **Not verified**
or **Modified or unknown** rather than guessing. This does not mean
normal channel programming on a supported radio is untested.

Firmware-feature results saved with a CHIRP `.img` file reflect the last
radio download. If the firmware changes, download from the radio again
to refresh the reported status.

### 10x diagnostic timer mode

CHIRP can recognize the Toolkit's **10x TOT TEST MODE** and warn when it
is installed. In this mode the timer expires ten times faster, so the
setting `3:00 (10x: 18 sec)` means 18 seconds during testing but three
minutes after restoring normal firmware. The programmed timeout value
itself does not change. Normal or unverified firmware shows standard
timeout labels without 10x annotations.

**10x firmware is for testing only.** Restore normal-speed firmware
before regular operation, then download from the radio again to refresh
the displayed timer status.

For firmware revisions, backup and recovery, patch application or removal,
and other firmware details, see the
[Kenwood TK-x4x Toolkit](https://github.com/KY4WI/Kenwood_TK-x4x_Toolkit).

## Development and testing

`tk840.py` is the driver source file. This repository also contains synthetic images and automated regression tests used to validate the driver.

The `test-images/` directory contains three sets of synthetic CHIRP test images:

- `feature/` - sparse-System feature-coverage images with 36 programmed channels and substantial empty space. These emphasize unusual settings, holes in the System table, boundary values, tones, DCS, cross tones, scan/lockout states, TX inhibit, and commercial flags.
- `dense/` - high-density stress images with 270 programmed channels and 38 empty logical memories. These emphasize packed-layout pressure, large/uneven Systems, unique channel combinations, tones, flags, names, and boundary values.
- `upstream/` - the set for CHIRP submission: the dense set's 270 channels with System numbers 6, 13, 19, 24 and 29 left unused, so density and sparse System numbers are tested together. File names match what CHIRP's `tests/images/` expects.

The test images are CHIRP-saved `.img` files: the codeplug plus CHIRP's metadata, which names the model. The TK-940 and TK-941 codeplugs are intentionally byte-identical because the driver treats their format as interchangeable; raw codeplugs contain no reliable model discriminator, so a metadata-free 94x image falls back to TK-940, while a CHIRP-saved image reopens as TK-940 or TK-941 exactly.

## Automated checks

Every push and pull request runs:

- Python syntax validation and driver regression tests, including codeplug size, model metadata, frequency encoding, firmware-feature handling, TX-inhibit policy, and synthetic-image integrity:

  ```sh
  python3 -m py_compile tk840.py
  python3 -m unittest discover -s tests -v
  ```

- CHIRP's own driver test suite (`tests/test_drivers.py` from kk7ds/chirp), with the driver in `chirp/drivers/` and each of the three image sets in turn as `tests/images/Kenwood_TK-840.img`, `TK-940.img` and `TK-941.img`. This covers memory round-trips, band edges, names, tones, settings, image detection, and clone error handling against a fake serial port. It runs against both a pinned known-good CHIRP commit and CHIRP's current master.

Automated tests complement the completed hardware validation of the supported radio workflows and help catch regressions.

## Safety / write behavior

Driver-only CHIRP preferences are stored in CHIRP's normal `.img` metadata
trailer. They are not stored in the radio's flash-ROM configuration area and
are stripped from the image before any radio upload.

The upload path is intentionally conservative:

- Each configuration block is read and compared before any write to that block.
- Unchanged blocks are skipped.
- Changed blocks are written and read back immediately before moving on.
- Verification failures are retried with protocol recovery where safe.
- If a low-level partial serial write occurs during a flash-ROM configuration payload, the driver avoids sending potentially destructive recovery bytes and requires the radio to be power-cycled.

## License

This driver is licensed under **GPL-3.0-or-later**. It is based in part on CHIRP's Kenwood TK-760 driver and related CHIRP Kenwood driver code. Original copyright and GPL terms are retained in the source.

See `LICENSE` for the GNU General Public License version 3.
