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
