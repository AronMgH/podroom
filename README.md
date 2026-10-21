# Podcast Studio

A local AI podcast studio. Create characters with their own voices, personalities and talking speed,
give them a topic, and they hold an unscripted conversation that is voiced live with
[Phonon TTS](https://huggingface.co/gradium/phonon-40m-en). Also includes a simple text-to-speech page.

- **Podcast**: characters, templates, an AI director that decides who speaks next, interruptions,
  listener reactions, a producer who can steer or speak on air, minute-based episodes, an animated orb
  view with captions, WAV and transcript export. Every episode is saved to `episodes/` as it plays.
- **Speak**: type text and hear it, with model, voice, gender, accent, speed and expressiveness.
- **AI providers**: DeepSeek or OpenCode Go (OpenAI-compatible chat completions).

## Setup

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

The model is gated: accept the terms on Hugging Face, then put its files (`config.json`,
`tokenizer.json`, `model.q8.gguf`, `default-voice.safetensors`, …) in `phonon-40m-en/`.

## Run

```bash
./phonon-tts.sh
```

This starts the server on http://127.0.0.1:7860 and opens it as an app window. Add your API key in
the app under Settings; keys are stored in `~/.config/phonon-tts/` and never sent back to the browser.
`phonon-tts.desktop` is a desktop launcher (edit the paths in it to match where you cloned this).

Set `PHONON_FAKE_LLM=1` to test with canned lines and no API key, and `PHONON_PORT` to change the port.
