<p align="center"><img src="icon.svg" width="96" alt="Podroom"></p>

# Podroom

An AI podcast studio that runs on your computer. Create characters with their own voices, personalities and talking speed,
give them a topic, and they hold an unscripted conversation that is voiced live, on your own
machine, with [Phonon TTS](https://huggingface.co/gradium/phonon-40m-en). Also includes a simple text-to-speech page.

- **Podcast**: characters, templates, an AI director that decides who speaks next, interruptions,
  listener reactions, you at the table and in charge (type or talk: they stop, listen and do what you
  ask), minute-based episodes, an animated orb view with captions, WAV and transcript export. Every episode is saved to `episodes/` as it plays.
- **Casts**: everyone who has been on an episode, your own saved characters and the built-in templates in one place,
  plus named lineups you can load back in one click.
- **Speak**: type text and hear it, with model, voice, gender, accent, speed and expressiveness.
- **AI providers**: [OpenRouter](https://openrouter.ai) (any of its models with one key), DeepSeek, or OpenCode Go
  (OpenAI-compatible chat completions).

## Desktop app

Download Podroom for Linux, Windows or macOS from [Releases](https://github.com/AronMgH/podroom/releases). It is a
normal app window with everything bundled: no Python, browser or terminal needed. On first launch it uses
phonon-31m-en, which downloads by itself (65 MB). Your show, episodes and downloaded models are kept in the app's data
folder (Help → Open data folder), and your API keys in `~/.config/podroom/`.

- **Linux**: the `.deb` installs it with a menu entry. The `.AppImage` runs from anywhere: make it executable first.
  On Ubuntu 24.04 and later, AppImages of Electron apps can close straight away because of the system's sandbox rules;
  the `.deb` does not have that problem.
- **Windows**: run the installer. The builds are not code-signed yet, so SmartScreen may warn you: choose
  *More info → Run anyway*.
- **macOS**: open the `.dmg` (Apple Silicon `arm64` or Intel `x64`) and drag Podroom to Applications. It is not notarized
  by Apple yet, so the first time, open it, then allow it under *System Settings → Privacy & Security → Open Anyway*.

To talk to the show, download the speech model once under Settings → Speech to text. To use phonon-40m-en, accept its
terms on Hugging Face and put its files in a `phonon-40m-en` folder inside the data folder.

### Building it yourself

The app is an [Electron](https://www.electronjs.org) window around the same Python server, bundled with
[PyInstaller](https://pyinstaller.org). With the setup below done and Node.js 22:

```bash
uv pip install --python .venv/bin/python pyinstaller==6.22.3
.venv/bin/python desktop/build_server.py
cd desktop && npm ci && npx electron-builder --publish never
```

Installers land in `desktop/dist/`. `npm start` in `desktop/` runs the app straight from this checkout. Pushing a tag such
as `v0.1.0` builds all platforms on GitHub and drafts a release with the installers.

## Setup

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

The model is gated: accept the terms on Hugging Face, then put its files (`config.json`,
`tokenizer.json`, `model.q8.gguf`, `default-voice.safetensors`, …) in `phonon-40m-en/`.

### Speech to text (optional)

The mic button in the studio box lets you talk instead of type. Speech is
transcribed on this machine by [Moonshine](https://github.com/moonshine-ai/moonshine) base (English, MIT
license) run with [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) (Apache 2.0); nothing leaves the
computer. Download the model (about 290 MB) into `moonshine-base-en/`:

```bash
mkdir -p moonshine-base-en
for f in preprocess.onnx encode.int8.onnx uncached_decode.int8.onnx cached_decode.int8.onnx tokens.txt LICENSE; do
  curl -L -o moonshine-base-en/$f https://huggingface.co/csukuangfj/sherpa-onnx-moonshine-base-en-int8/resolve/main/$f
done
```

Click the mic and speak: whoever is talking stops and nobody else speaks while you do. Your words show
in the box as you talk, and about 2.5 seconds after you stop they go into the conversation (they are not
voiced); click the mic again to send at once. When a character asks you something or hands you the floor,
the show waits for you and the mic opens by itself as soon as their line ends. Clips are capped at 30 seconds.

## Run

```bash
./podroom.sh
```

This starts the server on http://127.0.0.1:7860 and opens it as an app window. Pick a provider and add its API key in
the app under Settings (or set `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY` or `OPENCODE_API_KEY`); keys are stored in `~/.config/podroom/` and never sent back to the browser.
`podroom.desktop` is a desktop launcher. Install it with the paths filled in for where you cloned this:

```bash
sed "s#@DIR@#$PWD#g" podroom.desktop > ~/.local/share/applications/podroom.desktop
```

With OpenRouter, choose "Other OpenRouter model…" in the model list to search every model it offers or paste any
model ID from [openrouter.ai/models](https://openrouter.ai/models). Thinking is turned off where the model allows it, since it only
adds delay to spoken lines.

Set `PODROOM_FAKE_LLM=1` to test with canned lines and no API key, and `PODROOM_PORT` to change the port.

## License

The code is released under the [MIT License](LICENSE).

## Model licenses

The models are downloaded separately and keep their own licenses. Check them before you use the output commercially:

- [phonon-40m-en](https://huggingface.co/gradium/phonon-40m-en), the default voice model: CC BY-NC 4.0 (non-commercial).
- [phonon-31m-en](https://huggingface.co/gradium/phonon-31m-en): Apache 2.0 and not gated. Pick it under Settings → Voice model
  if you need output you can use commercially.
- Moonshine base (speech to text): MIT. sherpa-onnx: Apache 2.0.
