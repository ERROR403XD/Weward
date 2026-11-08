"""Render the selected SVG into checked-in browser and shortcut icons."""

import argparse
import base64
import html
import json
import os
import shutil
import struct
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "weward" / "static"
TARGETS = [
    {"name": "favicon-16", "size": 16},
    {"name": "favicon-32", "size": 32},
    {"name": "favicon-48", "size": 48},
    {"name": "apple-touch-icon.png", "size": 180, "background": "#ffffff"},
    {"name": "icons/icon-192.png", "size": 192, "background": "#ffffff"},
    {"name": "icons/icon-512.png", "size": 512, "background": "#ffffff"},
    {"name": "icons/icon-maskable-512.png", "size": 512, "background": "#ffffff", "inset": 76},
]


class OutputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reading = False
        self.output = ""

    def handle_starttag(self, tag, attrs):
        if tag == "pre" and dict(attrs).get("id") == "output":
            self.reading = True

    def handle_data(self, data):
        if self.reading:
            self.output += data

    def handle_endtag(self, tag):
        if tag == "pre":
            self.reading = False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", help="Path to a locally installed Chrome or Chromium executable")
    args = parser.parse_args()
    browser = args.browser or next(
        (found for name in ("google-chrome", "chromium", "chromium-browser")
         if (found := shutil.which(name))), None,
    )
    if not browser:
        parser.error("Install Chrome/Chromium or supply --browser; no downloads are performed.")

    source = (STATIC / "icons" / "weward.svg").read_bytes()
    image_url = "data:image/svg+xml;base64," + base64.b64encode(source).decode("ascii")
    page = """<!doctype html><meta charset="utf-8"><pre id="output"></pre><script>
const output = document.getElementById("output");
const image = new Image();
image.onload = () => {
  const result = {};
  for (const target of TARGETS) {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = target.size;
    const context = canvas.getContext("2d");
    if (target.background) {
      context.fillStyle = target.background;
      context.fillRect(0, 0, target.size, target.size);
    }
    const inset = target.inset || 0;
    context.drawImage(image, inset, inset, target.size - 2 * inset, target.size - 2 * inset);
    result[target.name] = canvas.toDataURL("image/png").split(",")[1];
  }
  output.textContent = JSON.stringify(result);
};
image.onerror = () => { output.textContent = JSON.stringify({error: "SVG rendering failed"}); };
image.src = IMAGE_URL;
</script>""".replace("TARGETS", json.dumps(TARGETS)).replace("IMAGE_URL", json.dumps(image_url))
    with tempfile.TemporaryDirectory(prefix="weward-icons-") as temporary:
        work = Path(temporary)
        page_path = work / "render.html"
        page_path.write_text(page, encoding="utf-8")
        command = [
            browser, "--headless=new", "--disable-gpu", "--disable-background-networking",
            "--no-first-run", "--no-default-browser-check", "--disable-extensions",
            f"--user-data-dir={work / 'profile'}", "--virtual-time-budget=2000", "--dump-dom",
        ]
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            command.append("--no-sandbox")
        command.append(page_path.as_uri())
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    output = OutputParser()
    output.feed(result.stdout)
    images = json.loads(output.output)
    if set(images) != {target["name"] for target in TARGETS}:
        raise RuntimeError("Browser did not render the complete icon set: " + html.escape(str(list(images))))
    rendered = {name: base64.b64decode(data, validate=True) for name, data in images.items()}
    for target in TARGETS:
        data = rendered[target["name"]]
        if data[:8] != b"\x89PNG\r\n\x1a\n" or struct.unpack(">II", data[16:24]) != (target["size"],) * 2:
            raise RuntimeError("Unexpected PNG output: " + target["name"])

    namespace = "http://www.w3.org/2000/svg"
    ElementTree.register_namespace("", namespace)
    favicon = ElementTree.fromstring(source)
    style = ElementTree.Element(f"{{{namespace}}}style")
    style.text = "@media(prefers-color-scheme:dark){svg{color:#c6eddb}.accent{fill:#79c6a8}}"
    favicon.insert(1, style)
    (STATIC / "favicon.svg").write_bytes(ElementTree.tostring(favicon) + b"\n")
    for target in TARGETS:
        if target["name"].endswith(".png"):
            (STATIC / target["name"]).write_bytes(rendered[target["name"]])

    # Each ICO directory entry points to a complete PNG image.
    entries, chunks = [], []
    offset = 6 + 3 * 16
    for size in (16, 32, 48):
        data = rendered[f"favicon-{size}"]
        entries.append(struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(data), offset))
        chunks.append(data)
        offset += len(data)
    (STATIC / "favicon.ico").write_bytes(struct.pack("<HHH", 0, 1, 3) + b"".join(entries + chunks))
    print("Generated SVG favicon, three-size ICO, Apple Touch Icon, and three manifest icons.")


if __name__ == "__main__":
    main()
