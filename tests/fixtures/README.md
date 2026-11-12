---
title: Use explicit rendering fixtures
audience: maintainer
---
# Use explicit rendering fixtures

Supply the exact fixture directory to an installed-package test. It contains
fictional text and an unmodified, licensed Inter Bold font; it contains no customer
media or production design. [fonts/fonts.lock.json](fonts/fonts.lock.json) records
the upstream release, observed archive/member hashes, metadata, and fictional text.
Keep [fonts/OFL.txt](fonts/OFL.txt) with redistributed font copies. The font remains
OFL-1.1, separate from VideoGenie's application license.

## Keep fixture distribution explicit

The source checkout and source distribution include this directory. The runtime
wheel includes application code and dependency metadata, but does not include test
fixtures or the font. Installed render proofs must explicitly supply the font,
license, and lock, then register/import the verified font into their disposable
data root. A missing fixture must fail; don't install a system font, search a
checkout implicitly, download replacement bytes, or select a default font.

Inter's release label is v4.1; this exact face reports internal version
`4.001;git-9221beed3`. Preserve both facts. The upstream release URL and tag are
mutable, so verify the recorded hashes before reuse.

## Use the pinned image and character-map APIs

Python 3.12 installations resolve `pillow==13.0.0` for RGBA composition, FreeType
text measurement, and rasterization, plus `fonttools==4.68.0` for
`TTFont.getBestCmap()`. No NumPy or fontTools optional extras are required.
FFmpeg and ffprobe remain explicit external executables; package installation
does not install them.

Read bounded, verified font bytes once and pass independent `BytesIO` streams to
both libraries. Passing a font filename to Pillow can trigger a system-font search
if that path is missing. Verify every required codepoint against the exact font's
Unicode cmap before rasterizing; missing entries must fail rather than produce
replacement glyphs.

Select the layout engine explicitly. The dependency smoke uses
`ImageFont.Layout.BASIC` for the fictional accented Latin fixture. The inspected
macOS CPython 3.12 wheel reports RAQM unavailable; requesting RAQM can warn and
silently fall back. A renderer requiring RAQM must check
`PIL.features.check_feature("raqm")` and reject absence. BASIC and cmap coverage
do not prove support for arbitrary scripts, shaping, bidirectional text, or
combining sequences. The renderer must reject unsupported shaping requirements.

These pins and fixture checks do not implement a lower-third renderer or prove
MOV encoding, alpha, layout, overflow, or cross-platform pixel parity. Keep those
checks in the worker and installed-render proof.

## Review sources when changing a pin

- [Pillow 13.0.0 release and security fixes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html)
- [Pillow font loading and layout engines](https://pillow.readthedocs.io/en/stable/reference/ImageFont.html)
- [fontTools 4.68.0 release](https://github.com/fonttools/fonttools/releases/tag/4.68.0)
- [fontTools character-map API](https://fonttools.readthedocs.io/en/stable/ttLib/ttFont.html)

Review current upstream advisories and package hashes when updating. Empty
vulnerability metadata is an observation, not a guarantee of safety. Keep input
size, execution, and format limits in the worker; dependency pins alone do not
supply process isolation.
