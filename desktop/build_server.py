"""Bundle server.py and everything it needs into dist/podroom-server, for the desktop app to ship.

Run it with the Python that has requirements.txt and PyInstaller installed:
    python desktop/build_server.py
"""
import os

import PyInstaller.__main__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAGES = ["podcast.html", "casts.html", "settings.html", "shell.js", "theme.css", "icon.svg"]

args = [
    os.path.join(ROOT, "server.py"),
    "--name", "podroom-server",
    "--onedir", "--console", "--noconfirm", "--clean",
    "--distpath", os.path.join(ROOT, "dist"),
    "--workpath", os.path.join(ROOT, "build", "pyinstaller"),
    "--specpath", os.path.join(ROOT, "build"),
    "--paths", ROOT,
    "--hidden-import", "podcast",
    "--collect-all", "ptts",         # the speech engine is one native library
    "--collect-all", "sherpa_onnx",  # speech to text, with its onnxruntime libraries
]
for page in PAGES:
    args += ["--add-data", os.path.join(ROOT, page) + os.pathsep + "."]

PyInstaller.__main__.run(args)
