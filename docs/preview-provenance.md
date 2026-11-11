# Preview provenance

## What the image shows

`preview.png` is a fresh, reproducible **isolated renderer preview**, captured
on 2026-11-11 UTC for this project. `docs/screensaver.png` is an identical
compatibility copy. Both replace the earlier undocumented preview.

The image contains actual `ttfx colorshift` output decoded by this repository's
`TerminalDecoder`/`frame_runs` and painted by its unchanged production
`TerminalFrame::paint` implementation. The harness renders directly to a Qt
`QImage`; it does not recreate the letters or their colors. The header and
disclosure labels are the only additions, and are drawn by the capture harness.
They are not part of the lock interface.

**This is not a screenshot of a secure lock session.** It does not demonstrate
session locking, PAM/password authentication, unlocking, multi-monitor behavior,
display blanking, or delivered animation FPS. The finite seeded colorshift effect
is selected for a repeatable still; the live bridge normally selects random
effects. Its offline 120-FPS setting controls effect generation, not a measured
or promised runtime frame rate.

## Inputs and rights

- **Omarchy wordmark:** `docs/preview-logo.txt` is an unmodified copy of
  [Omarchy v4.5.0 `logo.txt`](https://github.com/omacom/omarchy/blob/v4.5.0/logo.txt),
  obtained from the installed Omarchy 4.5.0-3 package. Its SHA-256 is
  `f98c966b1670d71ffc26620a6ff885a73ab355b370bff6082cea3215ee580a29`.
  The upstream [MIT license](https://github.com/omacom/omarchy/blob/v4.5.0/LICENSE)
  names David Heinemeier Hansson; the complete notice is retained in
  [NOTICE](../NOTICE). The branding is upstream material, not a claim that this
  project created or exclusively owns the Omarchy wordmark.
- **Effect:** installed `ttfx` 0.5.0-3, upstream
  [v0.5.0](https://github.com/omacom/ttfx/tree/v0.5.0).
  Its [MIT license](https://github.com/omacom/ttfx/blob/v0.5.0/LICENSE) credits
  37signals / omacom-io and ChrisBuilds. Its
  [NOTICE](https://github.com/omacom/ttfx/blob/v0.5.0/NOTICE) explains that the
  effects originate in TerminalTextEffects. Those credits and the complete
  permission notice are also included in this project's [NOTICE](../NOTICE).
  The effect executable and its source are dependencies, not bundled assets.
- **Font:** JetBrainsMono Nerd Font Regular from installed package
  `ttf-jetbrains-mono-nerd-basic` 3.5.1-2, derived from
  [JetBrains Mono](https://github.com/JetBrains/JetBrainsMono) through
  [Nerd Fonts](https://github.com/ryanoasis/nerd-fonts). The package's
  `OFL.txt` identifies copyright 2020 The JetBrains Mono Project Authors and
  the SIL Open Font License 1.1. Its terms explicitly exclude documents made
  with the font from the requirement to be licensed under the OFL. No font
  file is bundled in this repository. The complete packaged notice is retained
  in [preview-font-license.txt](preview-font-license.txt). The font's embedded
  copyright and license names match that notice. The [Nerd Fonts v3.5.1
  per-font README](https://github.com/ryanoasis/nerd-fonts/blob/v3.5.1/patched-fonts/JetBrainsMono/README.md)
  identifies the base font as JetBrains Mono 2.304 and lists its additional icon
  sets separately. This preview uses ASCII and the basic block characters
  U+2580, U+2584 and U+2588, not any Nerd Fonts icon-set glyphs.
- **Project code and labels:** the capture harness, production renderer,
  decoder, and literal disclosure labels are project material under the MIT
  license in [LICENSE](../LICENSE), with upstream rights preserved.
- **Other capture dependencies:** Qt 6.12.0 (packages `qt6-base` 6.12.0-3 and
  `qt6-declarative` 6.12.0-2), Python 3.14.9, pyte 0.8.2 (LGPLv3), and
  wcwidth 0.9.4 (MIT). Qt's relevant headers offer LGPLv3/GPL/commercial
  alternatives. These packages are used locally and are not bundled.

The credits describe the actual inputs and their licenses. They do not assert
that running a program automatically applies its source-code license to its
output. This is an unofficial demonstration, not an Omarchy endorsement.

## Reproduce without accessing the desktop

Use a local Arch/Omarchy environment with `qmake6`, `make`, a C++ compiler,
`ttfx`, the font above, and a Python interpreter already providing `pyte`.
The script does not download dependencies, install the plugin, or run any
Omarchy configuration commands.

From the repository root, choose unused output filenames:

```sh
python3 -B scripts/capture-preview \
  --output /tmp/lock-screensaver-preview.png \
  --record /tmp/lock-screensaver-preview.json
```

If using an existing Python virtual environment, invoke that environment's
Python instead. To deliberately replace the repository assets:

```sh
python3 -B scripts/capture-preview \
  --output preview.png \
  --mirror docs/screensaver.png \
  --record docs/preview-capture.json \
  --replace
```

The script:

1. Builds a small harness against the existing `native/renderer.cpp` and
   `native/renderer.h` in a temporary directory.
2. Uses a temporary HOME, XDG directories and Fontconfig containing only the
   selected packaged font. Qt uses `QT_QPA_PLATFORM=offscreen`. Display,
   compositor and D-Bus environment variables are not inherited.
3. Computes the same 1280 × 720 geometry and font-size/grid formula as
   `qml/SecureSaver.qml.in`: 12-pixel font, 182 columns and 49 rows with the
   recorded font. It runs one finite seeded colorshift cycle in an isolated
   PTY, matching the production bridge's terminal behavior.
4. Decodes the resulting terminal state with the production decoder and calls
   the production painter once, then adds the disclosure labels.
5. Writes PNG output and a JSON record containing the exact effect arguments,
   package versions and SHA-256 hashes of the image, script, renderer, decoder,
   logo, font, executable, raw ANSI output and decoded runs. No local username
   or private filesystem path is written to that record.

[preview-capture.json](preview-capture.json) is the record for the committed
images. Matching dependencies and inputs reproduce the image bytes; different
Qt, font or effect versions may change them. The timestamp in a regenerated
record will naturally differ.

## Review and limits

The generated 1280 × 720 image was visually inspected for legibility, framing,
disclosure labels and private data. It contains only the upstream logo/effect,
the packaged font rendering and literal public labels. No desktop capture,
custom user branding, account details, password input or live configuration
was used. A second isolated run reproduced the identical PNG SHA-256.

No live shell, lock client, compositor, PAM session or display was started,
stopped, changed, reloaded or captured by this procedure.
