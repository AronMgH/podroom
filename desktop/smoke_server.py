"""Start the bundled server from dist/podroom-server and check that it serves the app, then stop it.

Catches what PyInstaller can miss (a page, a module, a native library) before an installer is built.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
exe = os.path.join(ROOT, "dist", "podroom-server", "podroom-server" + (".exe" if os.name == "nt" else ""))

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
data = tempfile.mkdtemp(prefix="podroom-smoke-")
proc = subprocess.Popen([exe], stdin=subprocess.PIPE, env=dict(os.environ, PODROOM_PORT=str(port), PODROOM_DATA=data,
                                                              PODROOM_EXIT_WITH_STDIN="1", PODROOM_FAKE_LLM="1"))
base = f"http://127.0.0.1:{port}"
try:
    for _ in range(300):
        try:
            urllib.request.urlopen(base + "/api/stt", timeout=2)
            break
        except OSError:
            if proc.poll() is not None:
                sys.exit(f"server exited with code {proc.returncode}")
            time.sleep(0.2)
    for page in ("/", "/casts", "/speak", "/theme.css"):
        body = urllib.request.urlopen(base + page, timeout=10).read()
        assert len(body) > 1000, page
    models = json.load(urllib.request.urlopen(base + "/api/models", timeout=10))
    assert any(m["name"] == "phonon-31m-en" for m in models), models
    state = json.load(urllib.request.urlopen(base + "/api/podcast/state", timeout=10))
    assert state["status"]["providers"], state
    stt = json.load(urllib.request.urlopen(base + "/api/stt", timeout=10))
    assert stt["ready"] is False and stt["running"] is False, stt
    # Speech to text must load from the bundle too: with no model downloaded yet, sherpa-onnx imports and the
    # server answers 503 "not installed". A missing library would answer 500 instead.
    clip = urllib.request.Request(base + "/api/stt", data=bytes(16000 * 4), method="POST",
                                  headers={"Content-Type": "application/octet-stream"})
    try:
        urllib.request.urlopen(clip, timeout=30)
        sys.exit("speech to text answered without a model")
    except urllib.error.HTTPError as e:
        assert e.code == 503, (e.code, e.read())
    print("bundled server OK:", base)
finally:
    proc.stdin.close()  # the server exits when its stdin closes, as it does under the desktop app
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        sys.exit("server did not exit when stdin closed")
