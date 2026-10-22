"""Podroom: a local AI podcast studio and text-to-speech web app, voiced with Phonon TTS.

Serves the UI on http://127.0.0.1:7860 and streams speech as raw float32 PCM
while it is generated, so the browser can start playing within half a second.

Models: every folder in the data folder (next to this file, unless PODROOM_DATA says otherwise) that holds
a Phonon checkpoint (config.json plus model.*.gguf or model.safetensors), plus Phonon 31m from Hugging Face,
which is not gated and downloads on first use.
"""
import glob
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy
import ptts

import podcast

# The pages ship with the code (inside the bundle when packaged); everything you make goes to the data folder.
HERE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA = podcast.DATA_DIR
HOST, PORT = "127.0.0.1", int(os.environ.get("PODROOM_PORT") or os.environ.get("PHONON_PORT") or "7860")
MAX_CHARS = 20000
HF_CACHE = os.path.join(os.environ.get("HF_HOME", os.path.expanduser("~/.cache/huggingface")), "hub")

# Weight files a checkpoint folder may hold, and the quant that loads each.
WEIGHTS = [
    ("model.q8.gguf", "q8_0", "8-bit"),
    ("model.q4k.gguf", "q4k", "4-bit"),
    ("model.safetensors", "f32", "full precision"),
]
REMOTE_MODELS = [
    {
        "repo": "gradium/phonon-31m-en",
        "name": "phonon-31m-en",
        "quant": "q8_0",
        "languages": ["en"],
        "size": "65 MB",
        "license": "Apache 2.0",
    },
]

# Delivery steps. Speed drives the checkpoint's duration_delta conditioner, measured on
# phonon-40m-en at about 0.8x to 1.2x the normal length; it saturates beyond these values.
# Normal is a touch slower than the model's own pace, which reads as rushed in conversation.
SPEED_DELTA = {-2: 1.5, -1: 0.6, 0: 0.25, 1: 0.0, 2: -0.2}
# Expressiveness is the sampling temperature of each line: higher sounds more animated.
EXPR_TEMP = {0: 0.15, 1: 0.3, 2: 0.45, 3: 0.6}

lock = threading.Lock()  # the runtime wants one call at a time
_loaded = {}  # (model id, lang) -> ptts.TTS
_voices = None

# Speech to text for the producer's mic: Moonshine (English, MIT) run by sherpa-onnx, fully offline.
# The browser sends 16 kHz mono float32 PCM; clips are push-to-talk, so 30 s is plenty.
STT_DIR = os.path.join(DATA, "moonshine-base-en")
STT_URL = "https://huggingface.co/csukuangfj/sherpa-onnx-moonshine-base-en-int8/resolve/main/"
STT_FILES = ["preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx", "cached_decode.int8.onnx", "tokens.txt", "LICENSE"]
stt_download = {"running": False, "done": 0, "total": 0, "error": ""}
STT_RATE = 16000
STT_MAX_SECONDS = 30
stt_lock = threading.Lock()  # separate from the TTS lock, so the mic never waits for a line being spoken
_stt = None


def list_models():
    models = []
    for d in sorted(os.listdir(DATA)):
        path = os.path.join(DATA, d)
        config = os.path.join(path, "config.json")
        if not os.path.isfile(config):
            continue
        try:
            languages = json.load(open(config)).get("languages") or ["en", "fr", "de", "es", "pt"]
        except Exception:
            continue
        files = [w for w in WEIGHTS if os.path.isfile(os.path.join(path, w[0]))]
        # Full precision is only worth listing when it is the only weights file.
        if len(files) > 1:
            files = [w for w in files if w[1] != "f32"]
        for _, quant, label in files:
            models.append({
                "id": f"{d}@{quant}",
                "name": d,
                "variant": label,
                "source": path,
                "quant": quant,
                "languages": languages,
                "local": True,
                "note": "",
            })
    local_names = {m["name"] for m in models}
    for r in REMOTE_MODELS:
        if r["name"] in local_names:
            continue
        cached = os.path.isdir(os.path.join(HF_CACHE, "models--" + r["repo"].replace("/", "--"), "snapshots"))
        models.append({
            "id": f"hf:{r['repo']}@{r['quant']}",
            "name": r["name"],
            "variant": "8-bit",
            "source": f"hf://{r['repo']}",
            "quant": r["quant"],
            "languages": r["languages"],
            "local": cached,
            "note": "" if cached else f"downloads {r['size']} on first use",
        })
    return models


def set_speed(tts, speed):
    """Apply a speed step to the next line. Conditions stick to the model, so every line sets them."""
    delta = SPEED_DELTA.get(speed, SPEED_DELTA[0])
    try:
        tts.set_conditions({"duration_delta": delta} if delta else {})
    except Exception:
        pass  # a checkpoint without that conditioner speaks at its own pace


def get_tts(model_id, lang):
    model = next((m for m in list_models() if m["id"] == model_id), None)
    if model is None:
        raise ValueError(f"unknown model {model_id}")
    if lang not in model["languages"]:
        lang = model["languages"][0]
    key = (model_id, lang)
    if key not in _loaded:
        _loaded[key] = ptts.TTS(model["source"], lang=lang, quant=model["quant"])
    return _loaded[key]


def stt_files():
    """The Moonshine files in STT_DIR, preferring the int8 builds, or None when the model is not there."""
    def pick(stem):
        found = sorted(glob.glob(os.path.join(STT_DIR, stem + "*.onnx")), key=lambda p: "int8" not in p)
        return found[0] if found else None
    files = {
        "preprocessor": pick("preprocess"),
        "encoder": pick("encode"),
        "uncached_decoder": pick("uncached_decode"),
        "cached_decoder": pick("cached_decode"),
        "tokens": os.path.join(STT_DIR, "tokens.txt"),
    }
    if not all(files.values()) or not os.path.isfile(files["tokens"]):
        return None
    return files


def download_stt():
    """Fetch the Moonshine model from Hugging Face into STT_DIR, keeping progress in stt_download for the page."""
    part = STT_DIR + ".download"
    try:
        os.makedirs(part, exist_ok=True)
        sizes = {}
        for name in STT_FILES:  # sizes first, so the progress bar is honest from the start
            req = urllib.request.Request(STT_URL + name, method="HEAD", headers={"User-Agent": podcast.USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as res:
                sizes[name] = int(res.headers.get("Content-Length") or 0)
        stt_download["total"] = sum(sizes.values())
        for name in STT_FILES:
            dest = os.path.join(part, name)
            if os.path.isfile(dest) and os.path.getsize(dest) == sizes[name]:
                stt_download["done"] += sizes[name]  # left over from an earlier try
                continue
            req = urllib.request.Request(STT_URL + name, headers={"User-Agent": podcast.USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as res, open(dest + ".tmp", "wb") as f:
                while chunk := res.read(1 << 20):
                    f.write(chunk)
                    stt_download["done"] += len(chunk)
            os.replace(dest + ".tmp", dest)
        if os.path.isdir(STT_DIR):
            os.rename(STT_DIR, STT_DIR + ".old-" + str(int(time.time())))
        os.replace(part, STT_DIR)
    except Exception as e:
        stt_download["error"] = f"Download failed: {e}"
    finally:
        stt_download["running"] = False


def get_stt():
    global _stt
    if _stt is None:
        import sherpa_onnx  # only needed once someone uses the mic

        files = stt_files()
        if files is None:
            raise FileNotFoundError("The speech-to-text model is not installed. Download it under Settings.")
        _stt = sherpa_onnx.OfflineRecognizer.from_moonshine(**files, num_threads=4)
    return _stt


def transcribe(pcm):
    with stt_lock:
        recognizer = get_stt()
        stream = recognizer.create_stream()
        stream.accept_waveform(STT_RATE, pcm)
        recognizer.decode_stream(stream)
        return stream.result.text.strip()


def catalog():
    """Every voice of the catalog, with gender, age and accent where it lists them."""
    global _voices
    if _voices is not None:
        return _voices
    files = glob.glob(os.path.join(HF_CACHE, "models--gradium--phonon-voices", "snapshots", "*", "voices.jsonl"))
    if not files:
        try:
            ptts.catalog_voices()  # downloads the catalog, without any voice
        except Exception:
            return []  # offline: the model's own voice still works; try again next time
        files = glob.glob(os.path.join(HF_CACHE, "models--gradium--phonon-voices", "snapshots", "*", "voices.jsonl"))
    if files:
        rows = [json.loads(line) for line in open(max(files, key=os.path.getmtime)) if line.strip()]
    else:
        rows = ptts.catalog_voices()  # cache elsewhere: names and descriptions only
    _voices = [
        {
            "name": r["name"],
            "language": r.get("language"),
            "gender": r.get("gender"),
            "age": r.get("age"),
            "locale": r.get("locale"),
            "description": r.get("description", ""),
        }
        for r in rows
    ]
    return _voices


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    def send_body(self, code, body, content_type):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")  # the app window must always load the current page
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, code, obj):
        self.send_body(code, json.dumps(obj).encode(), "application/json")

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def read_request(self):
        data = self.read_json()
        text = str(data.get("text", "")).strip()[:MAX_CHARS]
        kwargs = {}
        if data.get("voice"):
            kwargs["voice"] = data["voice"]
        if data.get("expr") is not None and int(data["expr"]) in EXPR_TEMP:
            kwargs["temperature"] = EXPR_TEMP[int(data["expr"])]
        speed = int(data.get("speed") or 0)
        return text, data.get("model", ""), data.get("lang", "en"), kwargs, speed

    def do_GET(self):
        if self.path in ("/", "/podcast"):
            with open(os.path.join(HERE, "podcast.html"), "rb") as f:
                self.send_body(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/casts":
            with open(os.path.join(HERE, "casts.html"), "rb") as f:
                self.send_body(200, f.read(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/podcast/models?provider="):
            pid = self.path.split("=", 1)[1]
            if pid not in podcast.PROVIDERS:
                return self.send_json(400, {"error": "unknown provider"})
            self.send_json(200, podcast.list_models(pid))
        elif self.path == "/api/podcast/people":
            self.send_json(200, podcast.past_characters())
        elif self.path == "/speak":
            with open(os.path.join(HERE, "speak.html"), "rb") as f:
                self.send_body(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/voices":
            self.send_json(200, catalog())
        elif self.path == "/api/models":
            self.send_json(200, list_models())
        elif self.path == "/theme.css":
            with open(os.path.join(HERE, "theme.css"), "rb") as f:
                self.send_body(200, f.read(), "text/css; charset=utf-8")
        elif self.path == "/api/podcast/state":
            self.send_json(200, {"state": podcast.load_state(), "status": podcast.status()})
        elif self.path == "/api/stt":
            self.send_json(200, {"ready": stt_files() is not None, **stt_download})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/stream":
            self.stream()
        elif self.path == "/api/wav":
            self.wav()
        elif self.path == "/api/load":
            self.load()
        elif self.path == "/api/warm":
            self.warm()
        elif self.path == "/api/podcast/state":
            podcast.save_state(self.read_json())
            self.send_json(200, {"ok": True})
        elif self.path == "/api/podcast/key":
            data = self.read_json()
            provider = data.get("provider")
            if provider not in podcast.PROVIDERS:
                return self.send_json(400, {"error": "unknown provider"})
            key = str(data.get("key", ""))
            if key.strip():
                podcast.save_key(provider, key)
            else:
                podcast.delete_key(provider)
            self.send_json(200, podcast.status())  # never send the key back
        elif self.path == "/api/podcast/episode":
            try:
                self.send_json(200, {"saved": podcast.save_episode(self.read_json())})
            except (OSError, ValueError) as e:
                self.send_json(400, {"error": str(e)})
        elif self.path == "/api/podcast/invite":
            try:
                self.send_json(200, podcast.invite(self.read_json()))
            except PermissionError as e:
                self.send_json(401, {"error": str(e)})
            except podcast.RateLimited as e:
                self.send_json(429, {"error": str(e)})
            except Exception as e:
                print(f"invite: {e}", file=sys.stderr, flush=True)
                self.send_json(502, {"error": str(e)})
        elif self.path == "/api/podcast/next":
            try:
                self.send_json(200, podcast.direct(self.read_json()))
            except podcast.RateLimited as e:
                self.send_json(429, {"error": str(e)})
            except Exception as e:
                print(f"director: {e}", file=sys.stderr, flush=True)
                self.send_json(502, {"error": str(e)})
        elif self.path == "/api/podcast/turn":
            self.turn()
        elif self.path == "/api/podcast/sample":
            self.turn(sample=True)
        elif self.path == "/api/stt":
            self.stt()
        elif self.path == "/api/stt/download":
            if not stt_download["running"] and stt_files() is None:
                stt_download.update(running=True, done=0, total=0, error="")
                threading.Thread(target=download_stt, daemon=True).start()
            self.send_json(200, {"ready": stt_files() is not None, **stt_download})
        else:
            self.send_json(404, {"error": "not found"})

    def load(self):
        data = self.read_json()
        t0 = time.perf_counter()
        try:
            with lock:
                tts = get_tts(data.get("model", ""), data.get("lang", "en"))
            self.send_json(200, {"voices": list(tts.voices), "seconds": round(time.perf_counter() - t0, 2)})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def warm(self):
        """Speak a throwaway word in each voice, so a new voice downloads before the show, not during it."""
        data = self.read_json()
        try:
            with lock:
                tts = get_tts(data.get("model", ""), data.get("lang", "en"))
                for voice in data.get("voices", []):
                    with tts.stream("Hi.", **({"voice": voice} if voice else {})) as audio:
                        for _ in audio:
                            pass
            self.send_json(200, {"ok": True})
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def turn(self, sample=False):
        # No TTS lock here: the network call runs while the previous line is still being spoken.
        try:
            data = self.read_json()
            if sample:
                data["phase"], data["transcript"] = "sample", []
            result = podcast.generate_turn(data)
            if sample:
                try:
                    podcast.save_sample(data.get("speaker", "?"), result.get("text", ""), data.get("show", ""), data.get("topic", ""))
                except OSError as e:
                    print(f"could not save sample: {e}", file=sys.stderr)
            self.send_json(200, result)
        except PermissionError as e:
            self.send_json(401, {"error": str(e)})
        except podcast.RateLimited as e:
            self.send_json(429, {"error": str(e), "retryAfter": e.retry_after})
        except Exception as e:
            self.send_json(502, {"error": str(e)})

    def stt(self):
        """Transcribe a push-to-talk clip: raw little-endian float32 mono PCM at 16 kHz."""
        length = int(self.headers.get("Content-Length", 0))
        if length > STT_MAX_SECONDS * STT_RATE * 4:
            return self.send_json(413, {"error": f"Keep it under {STT_MAX_SECONDS} seconds."})
        body = self.rfile.read(length)
        if len(body) < (STT_RATE // 4) * 4:  # under a quarter of a second of float32: nothing was said
            return self.send_json(200, {"text": ""})
        pcm = numpy.frombuffer(body[: len(body) // 4 * 4], dtype="<f4")
        t0 = time.perf_counter()
        try:
            text = transcribe(pcm)
        except FileNotFoundError as e:
            return self.send_json(503, {"error": str(e)})
        except Exception as e:
            print(f"stt: {e}", file=sys.stderr, flush=True)
            return self.send_json(500, {"error": str(e)})
        self.send_json(200, {"text": text, "seconds": round(time.perf_counter() - t0, 2)})

    def stream(self):
        text, model, lang, kwargs, speed = self.read_request()
        if not text:
            return self.send_json(400, {"error": "no text"})
        with lock:
            try:
                tts = get_tts(model, lang)
                set_speed(tts, speed)
                with tts.stream(text, **kwargs) as audio:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("X-Sample-Rate", str(audio.sample_rate))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    for pcm in audio:
                        self.wfile.write(pcm.astype("<f4").tobytes())
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the browser pressed Stop
            except Exception as e:
                try:
                    self.send_json(500, {"error": str(e)})
                except Exception:
                    pass

    def wav(self):
        text, model, lang, kwargs, speed = self.read_request()
        if not text:
            return self.send_json(400, {"error": "no text"})
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            with lock:
                tts = get_tts(model, lang)
                set_speed(tts, speed)
                tts.save(path, text, **kwargs)
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Disposition", 'attachment; filename="podroom-%d.wav"' % int(time.time()))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_json(500, {"error": str(e)})
        finally:
            os.unlink(path)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    if os.environ.get("PODROOM_EXIT_WITH_STDIN") == "1" and sys.stdin:
        # Started by the desktop app: its end of the pipe closes when it quits or crashes, and so do we.
        def watch():
            sys.stdin.read()
            os._exit(0)
        threading.Thread(target=watch, daemon=True).start()
    print(f"Podroom on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
