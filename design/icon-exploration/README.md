# Icon exploration

The selected design is **15, Conversation Weave**. Its production source is
[`weward.svg`](../../weward/static/icons/weward.svg). Keep the geometry and the
deep-green (`#16745f`) / mint (`#8bc7ae`) palette when adapting this mark.

This directory retains the twenty original candidates and the static review
gallery as internal design material. It is excluded from Docker build contexts.
Run `python3 design/icon-exploration/build.py` to rebuild the gallery, SVG contact
sheet, and downloadable ZIP. The ZIP and raster review screenshot are local
outputs, not production assets.

The application uses the selected SVG in both WebUI headers. Its adaptive SVG
favicon, ICO, Apple Touch Icon, and manifest icons are generated from that same
source with `python3 scripts/build-icons.py`. This development helper needs a
locally installed Chrome or Chromium; `--browser /path/to/browser` overrides
discovery. Python uses only the standard library, and the application and Docker
builds consume checked-in assets without a browser dependency.

The favicon uses a lighter palette on dark browser chrome. PNG shortcut icons
have an opaque white background; the maskable variant adds space for circular
and other system crops. The manifest supplies shortcut metadata, not offline
message storage or a service worker.
