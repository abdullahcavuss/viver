# Third-party notices

VIVER is licensed under the MIT License (see `LICENSE`). It includes or depends on the following third-party software.

## Bundled

### pylabview — `third_party/pylabview/`

- Source: https://github.com/mefistotelis/pylabview
- License: MIT — full text in `third_party/pylabview/LICENSE`
- Copyright (C) 2013 Jessica Creighton; Copyright (C) 2019-2020 Mefistotelis

### Tom Thumb font — `third_party/pylabview/assets/tom-thumb.*`

- Author: Robey Pointer
- License: MIT (may also be used under CC0 or CC-BY 3.0) — see `third_party/pylabview/assets/tom-thumb.txt`

## Installed separately (via `pip install -r requirements.txt`, not redistributed here)

| Package | License |
|---|---|
| Flask (and its dependencies Werkzeug, Jinja2, MarkupSafe, itsdangerous, click, blinker) | BSD-3-Clause |
| Pillow | MIT-CMU (HPND) |
| waitress | ZPL 2.1 |

## Not included

VIVER does not contain or redistribute any National Instruments software or files. Primitive names are read at
run time from the function palettes of the LabVIEW installation on the user's own computer and stored only in the
user's local `data/` folder.
