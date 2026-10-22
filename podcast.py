"""Podcast mode: characters that talk to each other through an LLM provider.

Each turn is one call to the chat API with the speaker's own persona as the
system prompt, so every character is a separate agent that only sees the
transcript. The browser decides who speaks next and plays the audio.
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# Where your show, episodes and downloaded models live. A source checkout keeps them next to the code; the
# desktop app passes its own per-user folder, since the installed app itself is read-only.
DATA_DIR = os.environ.get("PODROOM_DATA") or HERE
os.makedirs(DATA_DIR, exist_ok=True)
STATE_FILE = os.path.join(DATA_DIR, "podcast.json")
EPISODE_DIR = os.path.join(DATA_DIR, "episodes")
KEY_DIR = os.path.expanduser("~/.config/podroom")
OLD_KEY_DIR = os.path.expanduser("~/.config/phonon-tts")  # where keys lived before the rename; still read
FAKE = (os.environ.get("PODROOM_FAKE_LLM") or os.environ.get("PHONON_FAKE_LLM")) == "1"  # canned lines, for testing without a key
USER_AGENT = "podroom/1.0"

# OpenAI-style chat completion providers. "thinking" lists model prefixes that accept
# {"thinking": {"type": "disabled"}}; other models get a larger token budget instead,
# so any hidden reasoning cannot eat the whole reply.
PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "url": "https://api.deepseek.com/chat/completions",
        "env": "DEEPSEEK_API_KEY",
        "keyHint": "sk-…",
        "models": [("deepseek-flash", "DeepSeek V4.1 Flash (fast)"), ("deepseek-v4-pro", "DeepSeek V4 Pro")],
        "thinking": ("deepseek",),
    },
    "opencode-go": {
        "label": "OpenCode Go",
        "url": "https://opencode.ai/inference/go/openai/v1/chat/completions",
        "env": "OPENCODE_API_KEY",
        "keyHint": "OpenCode Console API key",
        "models": [
            ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash (fast)"),
            ("glm-5.3-flash", "GLM-5.3 Flash"),
            ("mimo-v2.6-flash", "MiMo V2.6 Flash"),
            ("longcat-2.0", "LongCat 2.0"),
            ("hy3", "Hy3"),
            ("kimi-k2.7-code", "Kimi K2.7 Code"),
            ("deepseek-v4-pro", "DeepSeek V4 Pro"),
            ("glm-5.3", "GLM-5.3"),
            ("kimi-k3", "Kimi K3"),
            ("minimax-m3.1-flash-free", "MiniMax M3.1 Flash (free, limited time)"),
            ("longcat-2.5-preview-free", "LongCat 2.5 Preview (free, limited time)"),
        ],
        "thinking": ("deepseek", "glm"),
        # Go asks clients to name themselves and send a stable session ID per conversation.
        "session_header": "x-opencode-session",
    },
    # OpenRouter reaches hundreds of models with one key. The list is a starting point: any model ID from
    # openrouter.ai/models works. Reasoning is switched off per model from its catalog entry (see _reasoning).
    "openrouter": {
        "label": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "env": "OPENROUTER_API_KEY",
        "keyHint": "sk-or-…",
        "models": [
            ("deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash (fast, cheap)"),
            ("anthropic/claude-haiku-5.5", "Claude Haiku 5.5 (fast)"),
            ("qwen/qwen3.8-flash", "Qwen3.8 Flash (fast)"),
            ("anthropic/claude-sonnet-5", "Claude Sonnet 5"),
            ("moonshotai/kimi-k3", "Kimi K3"),
            ("mistralai/mistral-large-4-0", "Mistral Large 4"),
            ("google/gemini-3.8-flash", "Gemini 3.8 Flash (always thinks a little)"),
        ],
        "anyModel": True,
        "catalog": "https://openrouter.ai/api/v1/models",
        "thinking": (),
        # App attribution: optional, shows the app on openrouter.ai rankings.
        "headers": {"HTTP-Referer": "https://github.com/AronMgH/podroom", "X-OpenRouter-Title": "Podroom"},
    },
}
PROVIDERS = {pid: PROVIDERS[pid] for pid in ("openrouter", "deepseek", "opencode-go")}  # OpenRouter first: one key, every model
MODEL_ID = re.compile(r"^[~A-Za-z0-9._\-/:]{1,120}$")
# (what the prompt asks for, max tokens, most sentences kept). Models drift long when only
# asked nicely, so replies are also trimmed to the sentence limit after they come back.
LENGTHS = {
    "short": ("one or two short sentences, under 30 words", 90, 2),
    "medium": ("two or three sentences, under 60 words", 160, 3),
    "long": ("four or five sentences, under 110 words", 300, 5),
}

# Long turns: a character can be handed the floor for a stretch (a story, a proper explanation), spoken
# as several back-to-back parts. `longest` on a character is how long the AI director may hold them there;
# you (the real person running the show) can ask for more, up to FLOOR_MAX seconds.
FLOOR_MAX = 360
LONGEST_CHOICES = (0, 60, 120, 180)

ATTITUDES = {
    "balanced": ("Balanced", ""),
    "friendly": ("Friendly", "Warm and encouraging. Gives others credit and looks for common ground."),
    "curious": ("Curious", "Endlessly curious. Asks follow-up questions and wants to understand how things really work."),
    "skeptical": ("Sceptical", "Doubts big claims, asks for evidence and politely pokes holes in arguments."),
    "enthusiastic": ("Enthusiastic", "Excited and optimistic. Loves new ideas and talks with real energy."),
    "sarcastic": ("Sarcastic", "Dry and sarcastic. Teases the others, but still makes real points."),
    "provocative": ("Provocative", "Plays devil's advocate on purpose, takes the unpopular side and stirs up debate."),
    "calm": ("Calm & wise", "Calm and thoughtful. Slows the conversation down and explains with analogies."),
    "grumpy": ("Grumpy", "Grumpy and impatient, complains about everything, and is secretly very funny."),
    "nerdy": ("Nerdy", "Loves details, facts and numbers, and goes off on fascinating tangents."),
    "funny": ("Comedian", "A comedian. Most lines have a joke, a witty aside or playful exaggeration."),
    "dramatic": ("Dramatic", "Theatrical and over the top. Everything is the most incredible thing ever."),
}

# Delivery: speed -2 (very slow) to 2 (very fast); expr 0 (calm) to 3 (dramatic); length "" uses the episode setting.
def _char(id, name, role, voice, attitude, persona, style, speed=0, expr=1, length="", longest=0):
    return {"id": id, "name": name, "role": role, "voice": voice, "attitude": attitude, "persona": persona,
            "style": style, "speed": speed, "expr": expr, "length": length, "longest": longest}


CHARACTER_TEMPLATES = [
    _char("t-host", "Maya", "host", "Harper", "curious",
          "A tech podcast host who has interviewed hundreds of founders. Keeps the conversation moving and sums up disagreements fairly.",
          "Warm and quick, with the odd light joke."),
    _char("t-engineer", "Theo", "guest", "Miles", "skeptical",
          "A software engineer with fifteen years of shipping products. Has seen every hype cycle and likes concrete examples.",
          "Dry and measured.", speed=-1),
    _char("t-founder", "Jake", "guest", "Russell", "enthusiastic",
          "A startup founder who just raised a big round. Thinks everything will change in the next two years.",
          "Fast, punchy, lots of big claims.", speed=1, expr=2, longest=60),
    _char("t-professor", "Dr. Eleanor Hart", "guest", "Everly", "calm",
          "A university professor and researcher. Explains complex ideas simply and cites studies.",
          "Patient and clear, likes a good analogy.", speed=-1, expr=0, length="medium", longest=120),
    _char("t-comedian", "Benny", "guest", "Micah", "funny",
          "A stand-up comedian who turns every topic into a bit, but is surprisingly sharp underneath.",
          "Rapid-fire, playful, loves a callback.", speed=1, expr=3, longest=60),
    _char("t-journalist", "Priya", "host", "Maeve", "provocative",
          "An investigative journalist who asks the uncomfortable questions and follows the money.",
          "Crisp and direct, never lets a dodge slide."),
    _char("t-veteran", "Walt", "guest", "Edwin", "grumpy",
          "A retired engineer who thinks things were better built in the old days, and has the stories to prove it.",
          "Slow, gravelly complaints with a punchline.", speed=-2, expr=1, longest=120),
    _char("t-creator", "Zoey", "guest", "Zoey", "enthusiastic",
          "A Gen Z content creator with two million followers. Knows what is trending and why.",
          "Upbeat, casual, says 'literally' a lot.", speed=1, expr=2),
    _char("t-wizard", "Master Aldric", "host", "Master Aldric", "calm",
          "An ancient wizard who has seen empires rise and fall. Speaks in riddles but means well.",
          "Grand and unhurried.", speed=-2, expr=2, length="medium", longest=120),
    _char("t-pirate", "Captain Silas", "guest", "Captain Silas Blackwater", "dramatic",
          "A pirate captain from the West Country with a tall tale for every occasion.",
          "Booming and theatrical, rolls his Rs.", expr=3, longest=120),
]
_T = {t["id"]: t for t in CHARACTER_TEMPLATES}

EPISODE_TEMPLATES = [
    {"id": "e-debate", "label": "Tech debate", "show": "Signal vs Noise",
     "topic": "Will AI agents replace apps, or just become another app? Cover cost, privacy and what users actually want.",
     "cast": ["t-host", "t-engineer", "t-founder"], "settings": {"minutes": 10, "length": "short", "turnMode": "smart"}},
    {"id": "e-interview", "label": "Expert interview", "show": "Deep Dive",
     "topic": "How sleep shapes memory, and what listeners can change tonight.",
     "cast": ["t-host", "t-professor"], "settings": {"minutes": 12, "length": "medium", "turnMode": "smart"}},
    {"id": "e-comedy", "label": "Comedy roundtable", "show": "Hot Takes, Cold Coffee",
     "topic": "Things that were genuinely better in the nineties.",
     "cast": ["t-comedian", "t-veteran", "t-creator"], "settings": {"minutes": 8, "length": "short", "turnMode": "smart"}},
    {"id": "e-news", "label": "News breakdown", "show": "The Briefing",
     "topic": "This week's biggest tech story: replace this with the story and a few facts you want covered.",
     "cast": ["t-journalist", "t-engineer"], "settings": {"minutes": 8, "length": "medium", "turnMode": "smart"}},
    {"id": "e-fantasy", "label": "Fantasy tavern tales", "show": "Tales from the Drowned Lantern",
     "topic": "The greatest treasure ever lost at sea, and who is to blame.",
     "cast": ["t-wizard", "t-pirate"], "settings": {"minutes": 8, "length": "short", "turnMode": "smart"}},
]

DEFAULT_STATE = {
    "show": "The Open Mic",
    "topic": "Will AI agents replace apps, or just become another app?",
    "characters": [dict(_T["t-host"], id="c1"), dict(_T["t-engineer"], id="c2")],
    "myTemplates": [],
    "casts": [],        # saved lineups from the Casts page: {"id", "name", "characters": [...]}
    "hiddenPast": [],   # names hidden from "From past episodes" on the Casts page
    # You: the real person running the show, who joins in from the studio box (typed or spoken, never voiced).
    "you": {"name": ""},
    "settings": {
        "maxTurns": 12,
        "length": "short",
        "turnMode": "smart",
        "provider": "openrouter",
        "llmModel": "deepseek/deepseek-v4.1-flash",
        "temperature": 1.0,
        "gap": 0.5,
        "interrupts": 1,  # 0 off, 1 some, 2 lots
        "reactions": 1,   # listeners say "mm-hm" or "yeah" during other lines
        "rpm": 10,        # most AI calls per minute; providers throttle bursts
        "minutes": 10,    # episode length; the host wraps up when the audio gets there
        "ttsModel": "",
    },
}


# ---- state and key ---------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
        merged = json.loads(json.dumps(DEFAULT_STATE))
        merged.update({k: v for k, v in state.items() if k != "settings"})
        merged["settings"].update(state.get("settings", {}))
        return merged
    except (OSError, ValueError):
        return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ---- saved episodes ----------------------------------------------------------------
# The app sends the whole episode record after every line; each episode keeps a .json (everything)
# and a .txt (easy to read) under episodes/, named by start time and show.

def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def episode_text(rec):
    s = rec.get("settings", {})
    out = [rec.get("show") or "Episode", f"Topic: {rec.get('topic', '')}"]
    out.append(f"Started {rec.get('startedAt', '?')}" + (f" · Ended {rec['endedAt']}: {rec.get('ended', '')}" if rec.get("endedAt") else " · still running"))
    out.append(f"Length {s.get('minutes', '?')} min · {s.get('provider', '?')} / {s.get('llmModel', '?')} · turn-taking {s.get('turnMode', '?')}")
    out.append("Cast: " + "; ".join(f"{c.get('name')} ({c.get('role')}, {c.get('attitude')}, voice {c.get('voice') or 'default'})" for c in rec.get("cast", [])))
    if rec.get("tokens"):
        out.append(f"Tokens: {rec['tokens']}")
    out.append("")
    for e in rec.get("lines", []):
        kind = e.get("type")
        if kind == "note":  # older episodes: a private note to the characters
            out.append(f"[Note: {e.get('text', '')}]\n")
            continue
        if kind == "event":  # you joined or left the table
            out.append(f"[{e.get('name', 'You')} {e.get('text', '')}]\n")
            continue
        who = e.get("name", "?") + (" (you)" if kind in ("you", "onair") else "")
        text = e.get("cut") or e.get("text", "")
        tags = []
        if e.get("cutIn"):
            tags.append(f"cuts in on {e['cutIn']}")
        if e.get("cut"):
            tags.append("cut off here")
        if e.get("phase") in ("open", "reopen", "close"):
            tags.append(e["phase"])
        if e.get("toYou"):
            tags.append("hands over to you")
        if e.get("floor"):
            tags.append({"start": "takes the floor", "continue": "still holding the floor", "last": "ends their time at the mic"}[e["floor"]])
        if e.get("byYou") or e.get("byProducer"):
            tags.append("cut off when you spoke")
        line = f"{who}: {text}" + (f"   [{', '.join(tags)}]" if tags else "")
        if e.get("dropped"):
            line = f"(not played) {line}"
        out.append(line + "\n")
    return "\n".join(out)


def save_episode(rec):
    path = _episode_file(rec.get("id"))
    os.makedirs(EPISODE_DIR, exist_ok=True)
    base, name = path[:-5], os.path.basename(path)[:-5]
    _write(base + ".json", json.dumps(rec, indent=2, ensure_ascii=False))
    _write(base + ".txt", episode_text(rec))
    return name


def _episode_file(eid):
    """The JSON file of an episode id. Only letters, digits, - and _ survive, so an id can never reach outside EPISODE_DIR."""
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(eid or ""))[:80].strip("-")
    if not name:
        raise ValueError("episode needs an id")
    return os.path.join(EPISODE_DIR, name + ".json")


SPOKEN = ("line", "you", "onair")


def list_episodes():
    """Every saved episode that has at least one line, most recently active first."""
    out = []
    for f in os.listdir(EPISODE_DIR) if os.path.isdir(EPISODE_DIR) else []:
        if not f.endswith(".json"):
            continue
        try:
            with open(os.path.join(EPISODE_DIR, f)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        lines = [e for e in rec.get("lines") or [] if isinstance(e, dict) and e.get("type") in SPOKEN and not e.get("dropped")]
        if not lines:
            continue
        out.append({"id": f[:-5], "show": rec.get("show") or "Untitled show", "topic": rec.get("topic") or "",
                    "startedAt": rec.get("startedAt", ""), "updatedAt": lines[-1].get("at") or rec.get("startedAt", ""),
                    "lines": len(lines), "cast": [c.get("name", "") for c in rec.get("cast") or [] if isinstance(c, dict)],
                    "ended": bool(rec.get("endedAt"))})
    out.sort(key=lambda e: e["updatedAt"], reverse=True)
    return out


def load_episode(eid):
    with open(_episode_file(eid)) as f:
        return json.load(f)


def past_characters():
    """Everyone who has been on an episode, newest appearance first, one entry per name."""
    people = {}
    for path in sorted((f for f in os.listdir(EPISODE_DIR) if f.endswith(".json")), reverse=True) if os.path.isdir(EPISODE_DIR) else []:
        try:
            with open(os.path.join(EPISODE_DIR, path)) as f:
                rec = json.load(f)
        except (OSError, ValueError):
            continue
        for c in rec.get("cast") or []:
            name = str(c.get("name") or "").strip()
            if not name or not isinstance(c, dict):
                continue
            seen = people.get(name.lower())
            if seen:
                seen["episodes"] += 1
                continue
            people[name.lower()] = dict(c, name=name, episodes=1, lastShow=rec.get("show", ""),
                                        lastTopic=rec.get("topic", ""), lastAt=rec.get("startedAt", ""))
    return list(people.values())


def save_sample(speaker, text, show="", topic=""):
    os.makedirs(EPISODE_DIR, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(EPISODE_DIR, "samples.txt"), "a") as f:
        f.write(f"[{stamp}] {show} · {topic}\n{speaker}: {text}\n\n")


def key_file(provider):
    return os.path.join(KEY_DIR, f"{provider}.key")


def api_key(provider):
    for path in (key_file(provider), os.path.join(OLD_KEY_DIR, f"{provider}.key")):
        try:
            with open(path) as f:
                key = f.read().strip()
                if key:
                    return key
        except OSError:
            pass
    return os.environ.get(PROVIDERS[provider]["env"], "").strip()


def save_key(provider, key):
    path = key_file(provider)
    os.makedirs(KEY_DIR, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key.strip())
    os.chmod(path, 0o600)


def delete_key(provider):
    for path in (key_file(provider), os.path.join(OLD_KEY_DIR, f"{provider}.key")):
        try:
            os.unlink(path)
        except OSError:
            pass


_catalogs = {}  # provider id -> (fetched at, {model id: entry})


def catalog(pid):
    """The provider's public model list, fetched at most once an hour. Empty when it cannot be reached."""
    url = PROVIDERS[pid].get("catalog")
    if not url:
        return {}
    at, models = _catalogs.get(pid, (0, {}))
    if time.time() - at < 3600:
        return models
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as res:
            rows = json.load(res).get("data", [])
        models = {m["id"]: m for m in rows if isinstance(m, dict) and m.get("id")}
        _catalogs[pid] = (time.time(), models)
    except (OSError, ValueError) as e:
        print(f"model list from {url} failed: {e}", file=sys.stderr, flush=True)
        _catalogs[pid] = (time.time() - 3300, models)  # try again in five minutes
    return models


def list_models(pid):
    """Text chat models for the model picker: id, name, and price per million tokens in and out."""
    out = []
    for m in catalog(pid).values():
        arch = m.get("architecture") or {}
        if "text" not in (arch.get("output_modalities") or ["text"]) or "embedding" in (arch.get("modality") or ""):
            continue
        price = m.get("pricing") or {}
        try:
            cost = [round(float(price.get(k) or 0) * 1e6, 3) for k in ("prompt", "completion")]
        except (TypeError, ValueError):
            cost = None
        out.append({"id": m["id"], "name": m.get("name") or m["id"], "price": cost,
                    "thinks": bool((m.get("reasoning") or {}).get("mandatory"))})
    return sorted(out, key=lambda m: m["name"].lower())


def _reasoning(pid, model):
    """OpenRouter: the reasoning setting that keeps replies quick. Returns (setting or None, extra token budget)."""
    info = (catalog(pid).get(model) or {}).get("reasoning")
    if info is None:  # unknown model, or the catalog is unreachable: let it think if it must, but hide it
        return {"exclude": True}, 2000
    efforts = info.get("supported_efforts") or []
    if not info.get("mandatory"):
        return ({"effort": "none"} if "none" in efforts else {"enabled": False}), 0
    lowest = next((e for e in ("minimal", "low", "medium") if e in efforts), None)
    return dict({"effort": lowest} if lowest else {}, exclude=True), 2000


def status():
    return {
        "fake": FAKE,
        "providers": [
            {"id": pid, "label": p["label"], "keyHint": p["keyHint"], "keyFile": key_file(pid).replace(os.path.expanduser("~"), "~"),
             "keyConfigured": FAKE or bool(api_key(pid)), "models": [{"id": m, "label": l} for m, l in p["models"]],
             "anyModel": bool(p.get("anyModel"))}
            for pid, p in PROVIDERS.items()
        ],
        "attitudes": [{"id": k, "label": v[0], "prompt": v[1]} for k, v in ATTITUDES.items()],
        "characterTemplates": CHARACTER_TEMPLATES,
        "episodeTemplates": EPISODE_TEMPLATES,
    }


# ---- prompts -----------------------------------------------------------------------

class RateLimited(RuntimeError):
    """The provider said "too many requests". retry_after is in seconds when it says how long to wait."""
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def system_prompt(me, cast, show, topic, length, phase, cut_in=None, user=None, cues=(), floor=None, pick_next=False, user_here=True):
    spec = LENGTHS[length] if isinstance(length, str) else length
    others = [c for c in cast if c["name"] != me["name"]]
    host = me.get("role") == "host"
    lines = [
        f'You are {me["name"]}, the {me.get("role", "guest")} of an audio podcast called "{show}".',
        f'Who you are: {me.get("persona", "").strip() or "a thoughtful guest"}',
    ]
    attitude = ATTITUDES.get(me.get("attitude") or "balanced", ATTITUDES["balanced"])[1]
    if attitude:
        lines.append(f"Your attitude: {attitude}")
    if me.get("style", "").strip():
        lines.append(f'How you speak: {me["style"].strip()}')
    # Nobody gets the others' full character sheets: they learn who people are from what they say.
    # The host has done a little prep, so gets one line on each guest to introduce them.
    if others:
        lines.append("Also on this episode (you only know what they tell you on air):")
        for c in others:
            intro = ""
            if host and c.get("persona", "").strip():
                intro = ": " + re.split(r"(?<=[.!?])\s", c["persona"].strip())[0]
            lines.append(f'- {c["name"]}, the {c.get("role", "guest")}{intro}')
    if user and user_here:
        lines.append(f"- {user}: a real person at the table with you, and the one running this show. They join in whenever "
                     "they like. What they ask for happens, immediately.")
    elif user:
        lines.append(f"- {user}: runs this show, but is not at the table right now. Do not talk to them or hand over to them: "
                     "the show carries on without them.")
    lines += [
        f"Today's topic: {topic.strip()}",
        "Nobody has planned where this conversation goes. Follow it wherever it leads. Change your mind only when someone gives you "
        "a real reason, not just to be agreeable.",
        "People listen to this for fun. Be specific, surprising and alive: real details, fresh examples, humour where it fits. "
        "If the topic is a request (cheer someone up, explain something, help them decide), the show exists to deliver it: "
        "give them the thing itself, such as the story, the joke, the tip or the answer, rather than debating whether it can be done.",
        "",
        "Rules:",
        f'- Reply with only what {me["name"]} says next, out loud: {spec[0]}.',
        "- This is spoken audio read by a text-to-speech voice. Plain spoken English only: no markdown, lists, emoji, "
        "stage directions, sound effects or written laughter such as ha or haha.",
        "- Stay fully in character. Your attitude and way of speaking should be obvious in every line: your word choice, "
        "your opinions, your rhythm.",
        "- Respond to what was just said before anything else: answer it, agree and add to it, or push back on it. "
        "Stay on the thread everyone is on. Bring in your own background only when it says something about that exact "
        "point, and tie it back to the topic. Never change the subject or start a new side story on your own. "
        "Never retell a story, example or image that has already been used on this episode, by you or anyone: bring a new one.",
        "- Sound like a real person on a relaxed podcast: contractions and natural reactions such as \"Oh, totally\", "
        "\"No, no, no\" or \"Wait, really?\". Vary your shape: sometimes a blunt one-liner, sometimes a quick story.",
        "- Never write lines for anyone else and never start with your own name. Do not repeat points already made "
        "and do not summarise the conversation.",
        *([f"- {user} is in charge and their word is final. If they ask for something, do it at once and exactly, even if it "
           "takes the conversation somewhere new. Never treat their request as a distraction. Talk to them directly, by name, "
           "like anyone else at the table.",
           f"- When your line hands the conversation to {user} (you ask them something, or ask them to go on or finish "
           "their thought), end it with the tag [HANDOVER]. Nobody else speaks until they answer."] if user and user_here else []),
    ]
    if floor:
        lines.append("- You are holding the floor: you are talking, not interviewing. Do not greet anyone, do not ask the others "
                     "a question, and speak as one continuous stretch of talk.")
    elif host:
        lines.append("- You are the host, so you steer. React briefly in your own voice, then either ask one sharp follow-up "
                     "or bring in a guest who has been quiet, by name. When a point has gone back and forth twice, close it "
                     "in a few words and move to a fresh, concrete angle on the topic, linked to what was just said so the turn feels natural. Keep it short: the guests are the stars. "
                     + (f"What {user} asks for always comes before your steering." if user else ""))
    else:
        lines.append("- You are a guest, not an interviewer. You answer, argue, tell stories and give opinions. "
                     "Do not end your line with a question, except a pointed challenge to someone you disagree with. "
                     "You can name another guest when you agree or disagree with them.")
    if phase == "open":
        guests = ", ".join(c["name"] for c in others) or "the listeners"
        lines.append(f"- The episode is starting now. In a few quick sentences, welcome the listeners, introduce the topic and {guests} by name, then hand over with a question.")
    elif phase == "reopen":
        lines.append("- The episode is back after a break. In two or three sentences, welcome the listeners back, recap in a few words "
                     "where the conversation had got to, then carry it forward with a question to a guest by name.")
    elif phase == "sample":
        lines.append("- This is an audition before the show. Introduce yourself in character and give your opening take on the topic.")
    elif phase == "close":
        lines.append("- This is the last line of the episode. Wrap up in two or three sentences, thank everyone by name and sign off.")
    if floor:
        mins = max(1, round(floor.get("secs", 60) / 60))
        mode = floor.get("mode")
        by_boss = floor.get("by") in ("you", "producer")
        if mode == "start" and by_boss:
            lines.append(f"- {user or 'The person running the show'} has put you on the stage for about {mins} minute{'s' if mins != 1 else ''}. This is the first part of "
                         "your turn at the mic: give your real, detailed opinion on the topic and on what has been said so far. Build one clear "
                         "argument with a concrete example or story, in your own voice. Do not wrap up yet.")
        elif mode == "start":
            lines.append(f"- You have the floor for a longer stretch (up to about {mins} minute{'s' if mins != 1 else ''} in all) because this "
                         "deserves a proper answer or story. This is the first part: develop one idea with a concrete example. Do not wrap up yet.")
        elif mode == "continue":
            lines.append("- You still have the floor. Carry straight on from your last words: the next step of the argument or story. "
                         "New content only, nothing repeated, no wrap-up yet.")
        elif mode == "last":
            lines.append("- This is the end of your time at the mic. Finish your point with one strong closing beat and hand the "
                         "conversation back.")
    for cue in cues:
        lines.append(f"- {cue}")
    if pick_next and others:
        host_name = next((c["name"] for c in others if c.get("role") == "host"), None)
        lines.append("- After your line, on a new line, add the tag [NEXT: name] with who should naturally speak after you: "
                     "the person you asked or spoke to, whoever your line most concerns"
                     + (f", or {host_name} to move things on" if host_name else "")
                     + f". One of: {', '.join(c['name'] for c in others)}. The tag is not read out.")
    if cut_in:
        lines.append(f"- You are cutting in: {cut_in} was interrupted mid-sentence (their line ends with a dash). "
                     "Jump in with a quick interjection such as \"Wait, wait\", \"Hold on\" or \"Sorry, but\", then make your point fast.")
    return "\n".join(lines)


EVENTS = {
    "join": "{user} has just joined the table. Welcome them by name in a sentence, catch them up in a few words on where the "
            "conversation is, and invite them in.",
    "leave": "{user} has just stepped away from the table. Mention it in passing in a few words, the way a real host would, and carry "
             "on the conversation without them.",
    "noAnswer": "{user} has not answered your question, so do not wait: say you will go with the plan, and start on it.",
    "answered": "{user} has answered your question, and the plan now follows their answer. Thank them in a few words and start on it.",
}


def turn_cues(me, cast, transcript, user, intent=None, agenda=None, phase="normal", user_here=True, event=None):
    """Short situational hints: answer a direct question, and (for the host) bring in whoever has gone quiet."""
    cues = []
    if event in EVENTS and user and me.get("role") == "host":
        cues.append(EVENTS[event].format(user=user))
    if not user_here:
        user = None  # away: nothing they said earlier is a request to act on now
    if transcript:
        last = transcript[-1]
        if last["name"] != me["name"] and re.search(rf"\b{re.escape(me['name'])}\b", last["text"]) and "?" in last["text"]:
            cues.append(f"{last['name']} just asked you directly. Answer that first, in your own way.")
        elif user and last["name"] == user and phase == "close":
            cues.append(f"{user} just spoke. Answer them in a few words, then sign off: this is still the last line of the episode.")
        elif user and last["name"] == user and me.get("role") == "host" and _overdue(agenda):
            pass  # the run-of-show cue says how to answer them and keep to time
        elif user and last["name"] == user:
            cues.append(f"{user}, who runs this show, just spoke. Do what they asked, fully and right now, and talk to them "
                        "directly. If they asked someone else, give way with one short line.")
    if intent:
        cues.append(f"What you want to do now: {intent}")
    cues += agenda_cues(me, agenda, phase, user, bool(user and transcript and transcript[-1]["name"] == user))
    if stalled(transcript, cast, user):
        a, b = sorted({t["name"] for t in transcript[-4:]})
        if me.get("role") == "host":
            nxt = "move to a point from the run of show that has not been covered yet" if agenda else "open a fresh, concrete angle on the topic"
            cues.append(f"{a} and {b} have been going back and forth on the same point. Close that thread in one sentence "
                        f"and {nxt}, or bring in someone else.")
        else:
            cues.append("This back-and-forth has run its course. Do not make another round of the same point: add something new.")
    if me.get("role") == "host" and len(transcript) >= 4:
        recent = {t["name"] for t in transcript[-4:]}
        quiet = [c["name"] for c in cast if c["name"] != me["name"] and c["name"] not in recent]
        if quiet:
            cues.append(f"{quiet[0]} has not spoken for a while. Consider bringing them in.")
    return cues


def stalled(transcript, cast, user=None):
    """True when two characters have taken the last four lines between them while others wait, which is where a thread
    goes in circles. A two-person show always looks like that, so it never counts as stalled."""
    last = transcript[-4:]
    names = {t["name"] for t in last}
    return len(cast) >= 3 and len(last) == 4 and user not in names and len(names) == 2


def user_prompt(me, transcript):
    if not transcript:
        return f'The recording has started. Say {me["name"]}\'s first line.'
    convo = "\n".join(f'{t["name"]}: {t["text"]}' for t in transcript[-40:])
    last = transcript[-1]
    if last["name"] == me["name"]:  # holding the floor: carry on from their own last words
        return f"Conversation so far:\n{convo}\n\nSay {me['name']}'s next part, carrying straight on from where they left off."
    return (f"Conversation so far:\n{convo}\n\nThe last thing said was {last['name']}: \"{last['text']}\"\n"
            f"Say {me['name']}'s next line, responding to that.")


EMOJI = re.compile("[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF️]")


def clean(text, me, cast):
    names = [re.escape(c["name"]) for c in cast if c.get("name")]
    if names:
        # Cut where the model starts writing someone else's line.
        m = re.search(r"\n\s*(?:%s)\s*:" % "|".join(names), text)
        if m:
            text = text[: m.start()]
        text = re.sub(r"^\s*(?:\*\*)?(?:%s)(?:\*\*)?\s*:\s*" % "|".join(names), "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)  # **bold** keeps its words
    text = re.sub(r"\*[^*]{0,80}\*", " ", text)  # *laughs*, *leans in*
    # Written laughter sounds wrong in a synthetic voice, so drop it: "Ha, fair enough" becomes "Fair enough".
    text = re.sub(r"\b(?:ha(?:ha)*|ah(?:ha)+|heh|hehe|lol|lmao)\b[\s,.!-]*", "", text, flags=re.I)
    text = re.sub(r"(?m)^\s*(?:[-•]|\d+[.)])\s+", "", text)  # list markers
    text = re.sub(r"\([^)]{0,80}\)|\[[^\]]{0,80}\]", " ", text)  # (pauses) [music]
    text = re.sub(r"[*_#`>]+", "", text)
    text = EMOJI.sub("", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([.,!?;:])", r"\1", text).strip().strip('"').strip()
    text = re.sub(r"^[,.;:!\s]+", "", text)
    text = text[:1].upper() + text[1:]
    return text


def limit_sentences(text, n):
    """Keep at most n sentences. A closing question survives, since it hands the turn to someone."""
    # Split only where end punctuation is followed by a space and a new sentence, so "3.5" and "Dr. Hart" stay whole.
    parts = re.split(r"(?<![A-Z][a-z]\.)(?<!\b[A-Z]\.)(?<!\b(?:Mr|Ms|Dr|St|vs)\.)(?<!\bMrs\.)(?<!\bProf\.)(?<!\betc\.)(?<=[.!?])[\"')\]]*\s+(?=[\"'(]?[A-Z0-9])", text)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= n:
        return text
    if parts[-1].endswith("?") and n >= 2:
        parts = parts[: n - 1] + [parts[-1]]
    else:
        parts = parts[:n]
    return " ".join(parts)


# ---- the turn ----------------------------------------------------------------------

def chat(req, messages, max_tokens, temperature, stop=None, timeout=90):
    """One chat completion with the provider and model in req. Returns (response, model, provider label)."""
    pid = req.get("provider") if req.get("provider") in PROVIDERS else "deepseek"
    prov = PROVIDERS[pid]
    name = prov["label"]
    key = api_key(pid)
    if not key:
        raise PermissionError(f"Add your {name} API key in Settings first.")
    models = [m for m, _ in prov["models"]]
    wanted = str(req.get("llmModel") or "").strip()
    model = wanted if wanted in models or (prov.get("anyModel") and MODEL_ID.match(wanted)) else models[0]
    body = {"model": model, "messages": messages, "temperature": max(0.0, min(2.0, float(temperature))), "stream": False}
    if "catalog" in prov:
        reasoning, extra = _reasoning(pid, model)
        if reasoning:
            body["reasoning"] = reasoning
    elif model.startswith(prov["thinking"]):
        body["thinking"] = {"type": "disabled"}  # thinking is on by default and only adds delay here
        extra = 0
    else:
        extra = 2000
    body["max_tokens"] = max_tokens + extra
    if stop:
        body["stop"] = stop
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": USER_AGENT, **prov.get("headers", {})}
    if prov.get("session_header") and req.get("session"):
        headers[prov["session_header"]] = str(req["session"])[:100]
    request = urllib.request.Request(prov["url"], data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as res:
            return json.load(res), model, name
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.load(e).get("error", {}).get("message", "")
        except Exception:
            pass
        print(f"{name} HTTP {e.code}: {detail or e.reason}", file=sys.stderr, flush=True)
        if e.code == 429:
            try:
                retry_after = float(e.headers.get("Retry-After") or 0) or None
            except ValueError:
                retry_after = None
            raise RateLimited(f"{name} says too many requests ({detail or 'rate limit'})", retry_after)
        reasons = {401: f"{name} rejected the API key.", 402: f"Your {name} balance or plan limit is used up."}
        raise RuntimeError(reasons.get(e.code, f"{name} error {e.code}: {detail or e.reason}"))
    except urllib.error.URLError as e:
        raise RuntimeError(f"Could not reach {name}: {e.reason}")


def generate_turn(req):
    cast = [c for c in req.get("characters", []) if c.get("name", "").strip()]
    me = next((c for c in cast if c["name"] == req.get("speaker")), None)
    if me is None:
        raise ValueError("unknown speaker")
    # A character's own talkativeness wins over the episode setting.
    length = me.get("length") if me.get("length") in LENGTHS else req.get("length") if req.get("length") in LENGTHS else "short"
    phase = req.get("phase", "normal")
    transcript = req.get("transcript", [])
    user = (req.get("user") or "").strip() or None
    user_here = req.get("userPresent") is not False
    event = req.get("event") if req.get("event") in EVENTS else None
    floor = req.get("floor") if isinstance(req.get("floor"), dict) and req["floor"].get("mode") in ("start", "continue", "last") else None
    spec = LENGTHS[length]
    # Smart turn-taking: the speaker also names who goes next, so a line needs one AI call, not two.
    pick_next = bool(req.get("pickNext")) and phase in ("open", "reopen", "normal") and (not floor or floor["mode"] == "last")
    if floor:  # a long turn is written in parts of 30 to 170 words, with a token and sentence budget to match
        words = max(30, min(170, int(floor.get("words") or 120)))
        spec = (f"about {words} words of natural, continuous speech", int(words * 1.8) + 60, max(3, round(words / 11)))

    if FAKE:
        if floor:
            return {"name": me["name"], "text": fake_floor(me, words)}
        line, replan = take_replan(fake_line(me, cast, transcript, phase))
        line, nxt = take_next(line, me, cast, user)
        return {"name": me["name"], "text": f"Wait, hold on. {line}" if req.get("cutIn") else line, "next": nxt if pick_next else None,
                "replan": replan if me.get("role") == "host" and phase == "normal" else None}

    others = [c["name"] for c in cast if c["name"] != me["name"]]
    messages = [
        {"role": "system", "content": system_prompt(me, cast, req.get("show", "the show"), req.get("topic", ""), spec, phase,
                                                    req.get("cutIn"), user,
                                                    turn_cues(me, cast, transcript, user, req.get("intent"), _agenda(req), phase, user_here, event),
                                                    floor, pick_next, user_here)},
        {"role": "user", "content": user_prompt(me, transcript)},
    ]
    stop = [f"\n{n}:" for n in others + ([user] if user else [])][:4]  # four is the most every provider accepts
    data, model, name = chat(req, messages, spec[1] + (15 if pick_next else 0), req.get("temperature", 1.0), stop)

    choice = data["choices"][0]
    raw = re.sub(r"(?s)<think>.*?(</think>|$)", "", choice["message"].get("content") or "")  # inline reasoning
    raw, replan = take_replan(raw)
    raw, nxt = take_next(raw, me, cast, user)
    # The opening and the sign-off get one extra sentence for the welcome or the thank-yous.
    everyone = cast + ([{"name": user}] if user else [])
    text = limit_sentences(clean(raw, me, everyone), spec[2] + (1 if phase in ("open", "reopen", "close") else 0))
    # Handing over to you: the line ends by asking you by name, or the model tags it (clean() strips the tag). The tag
    # only counts when the line talks to you (names you, or answers you), since models also tag questions meant for another guest.
    talks_to_you = bool(user) and (re.search(rf"\b{re.escape(user)}\b", text, re.I) is not None
                                   or bool(transcript) and transcript[-1]["name"] == user)
    to_you = bool(user) and user_here and phase != "close" and (asks_user(text, user)
                                                  or bool(talks_to_you and re.search(r"\[\s*handover\s*\]", raw, re.I)))
    if not text:
        if choice.get("finish_reason") == "length":
            raise RuntimeError(f"{model} spent its whole reply budget thinking. Pick a faster model.")
        raise RuntimeError(f"{name} returned an empty line.")
    usage = data.get("usage", {})
    return {"name": me["name"], "text": text, "toYou": to_you, "next": nxt if pick_next and not to_you else None,
            "replan": replan if me.get("role") == "host" and phase == "normal" else None,
            "usage": {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0)}}


# "[NEXT: Benny]", "(Next: Benny)" or "Next: Benny" on a line of its own, at the very end of the reply.
NEXT_TAG = re.compile(r"(?:[\[(]|(?:^|\n)[ \t]*)\s*next(?:\s+speaker)?\s*[:=]\s*([^\]\)\n]{1,60}?)\s*[\])]?\s*$", re.I)
HANDOVER_END = re.compile(r"\s*\[\s*handover\s*\]\s*$", re.I)


def take_next(raw, me, cast, user=None):
    """Split the speaker's [NEXT: name] tag off the end of their reply. Returns (reply without the tag, the name or None).
    The name only counts when it is someone else at the table, never the speaker and never you."""
    body = raw.rstrip()
    h = HANDOVER_END.search(body)  # the handover tag may come before or after it; keep it for the check after clean()
    if h:
        body = body[: h.start()]
    m = NEXT_TAG.search(body)
    if not m:
        return raw, None
    raw = body[: m.start()].rstrip() + (" [HANDOVER]" if h else "")
    wanted = m.group(1).strip().strip("\"'*.").lower()
    names = [c["name"] for c in cast if c["name"] != me["name"] and c["name"] != user]
    pick = next((n for n in names if n.lower() == wanted), None) \
        or next((n for n in names if wanted and (wanted in n.lower() or n.lower() in wanted)), None)
    return raw, pick


def asks_user(text, user):
    """True when the line ends by asking you something by name, as in "Aron, what do you think?"."""
    last = re.split(r"(?<=[.!?])\s+", text.strip())[-1]
    return last.endswith("?") and re.search(rf"\b{re.escape(user)}\b", last, re.I) is not None


# ---- the director --------------------------------------------------------------------
# Reads the whole conversation and decides who would naturally speak next, what they want to do,
# and whether they would jump in before the last speaker finishes. Only the director sees everyone's
# full character sheet; the speaker still only knows what was said on air.

def direct(req):
    cast = [c for c in req.get("characters", []) if c.get("name", "").strip()]
    transcript = req.get("transcript", [])
    user = (req.get("user") or "").strip() or None
    last = transcript[-1]["name"] if transcript else None
    candidates = [c for c in cast if c["name"] != last]
    if not candidates:
        raise ValueError("nobody to pick")
    here = req.get("userPresent") is not False
    by_boss = bool(user) and here and last == user  # your word is in play
    if FAKE:
        pick = random.choice(candidates)
        floor = pick.get("longest", 0) if random.random() < 0.3 else 0
        return {"next": pick["name"], "intent": "Respond directly to the last point.", "cutIn": random.random() < 0.2, "floor": floor}

    counts = {c["name"]: sum(1 for t in transcript if t["name"] == c["name"]) for c in cast}
    sheet = "\n".join(
        f'- {c["name"]} ({c.get("role", "guest")}, {c.get("attitude") or "balanced"}): {c.get("persona", "").strip()} '
        f'Has spoken {counts[c["name"]]} times. '
        + (f'Can hold the floor for a long stretch, up to {int(c["longest"])} seconds.' if c.get("longest") else "Short turns only.")
        for c in cast)
    system = "\n".join([
        f'You direct a live, unscripted audio podcast called "{req.get("show", "the show")}". Today\'s topic: {req.get("topic", "").strip()}',
        "The people at the table:",
        sheet,
        *([f"- {user}: runs the show but is away from the table right now. Never pick them."] if user and not here else []),
        *([f"- {user}: a real person at the table who runs the show and joins in whenever they like. Never pick them.",
           "",
           f"{user}'s word is final. If their latest message asks for something, such as who should speak, who should take "
           "the stage or continue for some minutes, or to move on from a subject, do exactly that: pick that person and make "
           "the intent carry it out. Never treat it as a distraction. If they spoke without naming anyone, pick whoever "
           "should answer them."] if user and here else []),
        "Read the conversation and decide who would naturally speak next, the way a real conversation flows: "
        "whoever was just asked something, whoever the last line most provokes or concerns given who they are, "
        "or whoever has a direct answer to the point on the table. The host steps in to move things on or to bring "
        "in someone quiet, not after every line. Keep it balanced over time, so nobody dominates.",
        "The intent must pick up a specific point that was already said and say how this person will respond to it "
        "(agree and add, push back, answer, ask for the evidence). Never a new unrelated angle, except when a thread is "
        "spent: if two people have traded the same point back and forth, pick the host or someone else and make the intent "
        "close it and open a fresh, concrete angle on the topic.",
        "The show should be lively and give listeners what the topic promises. If the topic is a request, such as cheering "
        "someone up, the intents deliver it (stories, jokes, ideas they can use) instead of debating it.",
        "The intent is about the substance of today's topic. Never tell someone to talk about their job, company, "
        "industry or past unless the topic itself is about that: their background shows in their attitude and how "
        "they think, not in what they talk about.",
        "cutIn is true only when this person would genuinely be unable to wait, for example a strong disagreement "
        "or being misquoted. It is rare: most turns are false.",
        "floor is 0 for a normal reply. Set it to a number of seconds only when (a) the person running the show asked someone to speak at length "
        "(use the time they asked for, at most %d), or (b) the next speaker is marked as able to hold the floor and the point on the "
        "table needs a proper explanation or a story rather than a quick reply (choose between 60 and their maximum). Case (b) is rare: "
        "never twice in a row." % FLOOR_MAX,
        f'Reply with JSON only: {{"next": "<one of: {", ".join(c["name"] for c in candidates)}>", "intent": "<one short sentence>", "cutIn": false, "floor": 0}}',
    ])
    agenda = _agenda(req)
    if agenda and agenda["title"]:
        system += (f'\nThe show is in part {agenda["part"]} of {agenda["parts"]} of its run of show, "{agenda["title"]}", which should cover: '
                   + "; ".join(agenda["points"]) + ". Intents stay inside this part unless the person running the show asked for something else.")
    convo = "\n".join(f'{t["name"]}: {t["text"]}' for t in transcript[-16:]) or "(nothing yet)"
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": f"Conversation so far:\n{convo}\n\nWho speaks next? JSON only."}]
    data, _, name = chat(req, messages, 140, 0.4, timeout=20)
    raw = re.sub(r"(?s)<think>.*?(</think>|$)", "", data["choices"][0]["message"].get("content") or "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError(f"{name} director gave no JSON")
    out = json.loads(m.group(0))
    wanted = str(out.get("next", "")).strip().lower()
    pick = next((c for c in candidates if c["name"].lower() == wanted), None) \
        or next((c for c in candidates if wanted and (wanted in c["name"].lower() or c["name"].lower() in wanted)), None)
    if not pick:
        raise RuntimeError(f"director picked someone not at the table: {out.get('next')!r}")
    # You can hand anyone the stage up to FLOOR_MAX; otherwise the person's own limit is the cap.
    try:
        floor = int(float(out.get("floor") or 0))
    except (TypeError, ValueError):
        floor = 0
    cap = FLOOR_MAX if by_boss else int(pick.get("longest") or 0)
    floor = min(floor, cap)
    if floor < 30:
        floor = 0
    usage = data.get("usage", {})
    return {"next": pick["name"], "intent": str(out.get("intent") or "").strip()[:200], "cutIn": out.get("cutIn") is True and not floor,
            "floor": floor, "usage": {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0)}}


# ---- the run of show ---------------------------------------------------------------------
# One call at the start of an episode splits the topic into parts, each with the points it should cover
# and its share of the time. The host then steers by it: the page tells them which part they are in, what
# it should cover and how long is left, and when a part's time is up the host moves the show on.

def _clean_segments(raw_segments):
    segments = []
    for seg in (raw_segments or [])[:6]:
        if not isinstance(seg, dict) or not str(seg.get("title") or "").strip():
            continue
        try:
            mins = max(0.3, float(seg.get("minutes") or 0))
        except (TypeError, ValueError):
            mins = 0.5
        points = [str(x).strip()[:200] for x in seg.get("points") or [] if str(x).strip()][:4]
        segments.append({"title": str(seg["title"]).strip()[:80], "points": points, "minutes": mins})
    return segments


def plan_episode(req):
    """The run of show. A first plan for the whole episode, or (with "current") a new plan from the part the show is
    in, when the host says the conversation has changed course or the time should change."""
    cast = [c for c in req.get("characters", []) if c.get("name", "").strip()]
    minutes = _clamp_int(req.get("minutes"), 1, 120, 10)
    topic = str(req.get("topic") or "").strip()[:1500]
    if not topic:
        raise ValueError("no topic to plan")
    user = (req.get("user") or "").strip() or None
    if req.get("userPresent") is False:
        user = None  # away: not at the table, and nobody to ask
    current = _clean_segments(req.get("current")) if isinstance(req.get("current"), list) else None
    part = _clamp_int(req.get("part"), 0, 5, 0)
    try:
        elapsed = max(0.0, float(req.get("elapsed") or 0))
    except (TypeError, ValueError):
        elapsed = 0.0
    left = max(0.5, minutes - elapsed - 0.4)  # what is left, less the sign-off
    talk = max(0.5, minutes - 0.7)  # a first plan: the episode less the opening and the sign-off
    parts = 1 if minutes <= 3 else 2 if minutes <= 6 else 3 if minutes <= 12 else 4 if minutes <= 25 else 5
    if FAKE:
        if current:
            rest = max(1, len(current) - part)
            return {"segments": [dict(x, minutes=round(left / rest, 2)) for x in current[part:]], "minutes": minutes}
        return {"segments": [{"title": f"Part {i + 1}", "points": [f"Point {i + 1}a", f"Point {i + 1}b"], "minutes": round(talk / parts, 2)}
                             for i in range(parts)], "minutes": minutes}
    people = "; ".join(f'{c["name"]} ({c.get("role", "guest")}): {_first_sentence(c.get("persona", ""))}' for c in cast)
    head = [f'You plan the run of show for an unscripted audio podcast called "{req.get("show") or "the show"}".',
            f"Topic: {topic}", f"At the table: {people}" + (f"; and {user}, a real person who runs the show." if user else "")]
    fmt = 'Reply with JSON only: {"minutes": <episode length in minutes>, "segments": [{"title": "...", "points": ["...", "..."], "minutes": 2.5}]}'
    if current:
        done = "; ".join(f'"{x["title"]}"' for x in current[:part]) or "nothing yet"
        convo = "\n".join(f'{t.get("name")}: {t.get("text")}' for t in (req.get("transcript") or [])[-12:] if isinstance(t, dict))
        system = "\n".join(head + [
            f"The episode is {minutes} minutes long and {elapsed:.1f} minutes have been used. Parts already done: {done}.",
            "The plan from here was: " + "; ".join(f'"{x["title"]}" ({x["minutes"]:g} min: {", ".join(x["points"])})' for x in current[part:]),
            f'The host has asked to change it: {str(req.get("note") or "the conversation has moved").strip()[:300]}',
            f"Rewrite the plan from the part the show is in now. The first part you give is the one happening now, and its minutes are "
            f"the time it still has from now. Follow where the conversation has gone and what was suggested. "
            + (f"If {user} asked for a different episode length, use it and say so in \"minutes\"; " if user else "")
            + f"otherwise the parts share the {left:.1f} minutes that are left and \"minutes\" stays {minutes}.",
            "Each part has a short title, two to four concrete points or questions to cover, and its minutes.", fmt])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": f"Recent conversation:\n{convo}\n\nThe new plan. JSON only."}]
    else:
        if parts == 1:
            shape = (f"That is very short: pick ONE specific, concrete angle on the topic that can land in {talk:.1f} minutes, and plan it as a "
                     "single part with two or three points that build to a clear takeaway. No tour of subtopics.")
        else:
            shape = (f"Split those minutes into {parts} parts that together give listeners what this topic promises, in an order that builds. "
                     + ("With this much time, go deep: each part earns its place with real stories, evidence or practical detail." if minutes > 12 else
                        "Keep each part tight: one idea, well made."))
        system = "\n".join(head + [
            f"The episode lasts {minutes} minutes. After a short opening, the talk has {talk:.1f} minutes before the sign-off.",
            shape,
            "If the topic is broad, narrow it: choose the most interesting specific angle that fits the time rather than skimming everything. "
            "If the topic is a request (explain something, cheer someone up, help someone decide), the parts deliver it rather than debate it.",
            "Each part has a short title, two to four concrete points or questions to cover, and its share of the minutes.", fmt])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": "Plan the episode. JSON only."}]
    data, _, name = chat(req, messages, 800, 0.5, timeout=40)
    raw = re.sub(r"(?s)<think>.*?(</think>|$)", "", data["choices"][0]["message"].get("content") or "")
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError(f"{name} gave no plan")
    out = json.loads(m.group(0))
    segments = _clean_segments(out.get("segments"))
    if not segments:
        raise RuntimeError(f"{name} gave an empty plan")
    new_minutes = _clamp_int(out.get("minutes"), 1, 120, minutes) if current and user else minutes
    budget = (new_minutes - elapsed - 0.4) if current else talk
    total = sum(x["minutes"] for x in segments)
    for x in segments:  # the parts fill exactly the time there is
        x["minutes"] = round(max(0.3, x["minutes"] * max(0.5, budget) / total), 2)
    usage = data.get("usage", {})
    return {"segments": segments, "minutes": new_minutes,
            "usage": {"in": usage.get("prompt_tokens", 0), "out": usage.get("completion_tokens", 0)}}


def _agenda(req):
    """Where the show is: the clock always, and the part of the plan when there is one. Checked field by field."""
    a = req.get("agenda")
    if not isinstance(a, dict):
        return None
    num = lambda k: max(0.0, float(a.get(k) or 0)) if isinstance(a.get(k), (int, float)) else 0.0
    title = str(a.get("title") or "").strip()[:80]
    return {"title": title, "points": [str(x).strip()[:200] for x in (a.get("points") or [])[:4] if str(x).strip()],
            "part": int(num("part")) or 1, "parts": int(num("parts")) or 1, "next": str(a.get("next") or "").strip()[:80],
            "outline": [str(x).strip()[:80] for x in (a.get("outline") or [])[:6] if str(x).strip()],
            "minsPart": round(num("minsPart"), 1), "minsShow": round(num("minsShow"), 1), "minsTotal": round(num("minsTotal"), 1),
            "wrap": a.get("wrap") is True, "checkIn": a.get("checkIn") is True}


def agenda_cues(me, agenda, phase, user=None, user_spoke=False):
    """The host runs the clock and the plan; guests just know which part the show is in."""
    if not agenda:
        return []
    host = me.get("role") == "host"
    if phase == "open":
        if host and len(agenda["outline"]) == 1:
            return [f'Today\'s angle: {agenda["title"] or agenda["outline"][0]}. This is a short episode: in your welcome, say in one sentence what '
                    "it will nail, then go straight into it."]
        if host and agenda["outline"]:
            return [f"Today's run of show ({agenda['minsTotal']:g} minutes): " + "; ".join(agenda["outline"]) + ". In your welcome, tell the "
                    "listeners in one short sentence what the episode will cover, then start on the first part."]
        return []
    if phase == "close":
        return []
    if not host:
        return [f'Run of show: part {agenda["part"]} of {agenda["parts"]}, "{agenda["title"]}". Keep your point inside it, and do not dwell on '
                "side details: say what matters and move the conversation forward."] if agenda["title"] else []
    cues = []
    if agenda["title"]:
        points = "; ".join(agenda["points"])
        cues.append(f'Run of show: part {agenda["part"]} of {agenda["parts"]}, "{agenda["title"]}"' + (f". It should cover: {points}." if points else "."))
        cues.append(f'About {agenda["minsPart"]:g} minutes left in this part and {agenda["minsShow"]:g} in the episode. You keep the show on this plan '
                    "and on time: if the talk drifts away from these points, or gets stuck on a side detail, close that in one line and bring it "
                    "back to what matters, in a friendly way.")
        cues.append("If someone suggests a better direction, a part deserves more or less time, or " + (f"{user} " if user else "the person running the show ")
                    + "asks for a different length, go with it and end your line with [REPLAN: what should change]. The plan and timing will be redone.")
    else:
        cues.append(f'About {agenda["minsShow"]:g} minutes left in the episode. Keep an eye on the time.')
    if agenda.get("checkIn") and not user_spoke:
        cues.append("You have let the guests run for a while. Check where the thread has got to: if it is circling a minor point, close it "
                    "in one line and steer back to this part's points; if it is going well, push it one step deeper with a sharp question.")
    if user_spoke and _overdue(agenda):
        target = f'the next part: "{agenda["next"]}"' if agenda["wrap"] and agenda["next"] else "a close"
        cues.append(f"{user} just spoke, and the time for this is up. If they asked for something (to stay on this, more time, a "
                    "different direction), do it and end your line with [REPLAN: what they want]. If it was a passing remark, answer "
                    f"it warmly in a sentence, then move the show on to {target}.")
    elif user_spoke:
        cues.append(f"{user} just spoke: do what they asked first. The plan can wait until that is done.")
    elif agenda["wrap"] and agenda["next"]:
        cues.append(f'This part\'s time is up. Close it in one sentence and move the show to the next part: "{agenda["next"]}". '
                    "Bring in a guest on it by name.")
    elif _winding_down(agenda):
        cues.append("The episode is nearly over: start bringing the conversation to a close, with no new topics.")
    elif agenda["wrap"]:
        cues.append("This part's time is up. Close it in one sentence and steer towards the last word on the topic.")
    return cues


def _winding_down(agenda):
    """The last stretch of the episode: 15% of it, between half a minute and a minute and a half."""
    return agenda["minsShow"] <= max(0.5, min(1.5, 0.15 * (agenda["minsTotal"] or 10)))


def _overdue(agenda):
    """The current part's time is up, or the episode is nearly over."""
    return bool(agenda) and (agenda["wrap"] or (_winding_down(agenda) and bool(agenda["title"])))


REPLAN_TAG = re.compile(r"[\[(]\s*replan\s*:\s*([^\]\)\n]{1,300})[\])]", re.I)


def take_replan(raw):
    """Split the host's [REPLAN: what should change] tag out of the reply, wherever it is."""
    m = REPLAN_TAG.search(raw)
    if not m:
        return raw, None
    return (raw[: m.start()] + raw[m.end():]).strip(), m.group(1).strip()


# ---- casting ---------------------------------------------------------------------------
# "Invite with AI": the casting director looks through the template library first and writes whole new
# characters only for what no template fits. The reply is checked field by field before it reaches the app.

def _clamp_int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


def _first_sentence(text):
    return re.split(r"(?<=[.!?])\s", (text or "").strip())[0]


def _snap_longest(v):
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        return 0
    return min(LONGEST_CHOICES, key=lambda c: abs(c - v))


def invite(req):
    cast = [c for c in req.get("characters", []) if c.get("name", "").strip()]
    user = (req.get("user") or "").strip()
    replace = bool(req.get("replace"))
    keep = [] if replace else cast
    # One slot per guest, each optionally describing who is wanted ("a stand-up comedian"); empty means the AI's choice.
    slots = [str(x).strip()[:200] for x in (req.get("slots") or [])][:6]
    count = len(slots) if slots else max(1, min(4, int(req.get("count") or 2)))
    hint = str(req.get("hint") or "").strip()[:400]
    topic = str(req.get("topic") or "").strip()
    voices = [v for v in req.get("voices", []) if v.get("name")]
    mine = [t for t in req.get("myTemplates", []) if t.get("name")]
    library = CHARACTER_TEMPLATES + [dict(t, id=t.get("id") or "my-" + t["name"]) for t in mine]
    in_cast = {c["name"].lower() for c in keep}
    library = [t for t in library if t["name"].lower() not in in_cast]  # never invite someone who is already at the table
    need_host = not any(c.get("role") == "host" for c in keep)
    taken_names = {c["name"].lower() for c in keep} | ({user.lower()} if user else set())
    taken_voices = {c.get("voice") for c in keep if c.get("voice")}

    if FAKE:
        raw = {"characters": [
            {"name": n, "role": "guest", "gender": g, "voice": "", "attitude": a,
             "persona": f"{n} is {('a ' + slots[i]) if i < len(slots) and slots[i] else 'someone'} who lives this topic and has a firm view on it.",
             "style": "Direct and warm.", "speed": 0, "expr": 1, "longest": 120}
            for i, (n, g, a) in enumerate([("Dr. Lena Okafor", "female", "calm"), ("Marcus Reid", "male", "skeptical"),
                                           ("Aiko Tanaka", "female", "curious"), ("Sam Ortega", "male", "provocative"),
                                           ("Nina Hale", "female", "funny"), ("Omar Diaz", "male", "friendly")][:count])]}
    else:
        def line(v):
            tag = " [novelty character voice]" if v.get("locale") == "characters" else ""
            return f'- {v["name"]} ({v.get("gender") or "n/a"}, {v.get("age") or "n/a"}){tag}'
        lib = "\n".join(f'- {t["id"]}: {t["name"]}, {t.get("role", "guest")}, {t.get("attitude", "balanced")}. '
                        f'{_first_sentence(t.get("persona"))}' for t in library)
        now = "\n".join(f'- {c["name"]} ({c.get("role", "guest")}, {c.get("attitude", "balanced")}): {(c.get("persona") or "").strip()[:200]}'
                        for c in keep) or "(nobody yet)"
        system = "\n".join([
            f'You are the casting director of an unscripted audio podcast called "{req.get("show", "the show")}".',
            f"Topic: {topic}",
            "Already in the cast:", now,
            *([f"Invite exactly {count} {'person' if count == 1 else 'people'}, one for each numbered slot below, and return them in that same order. "
               "A slot names the kind of person the show's creator wants: honour it exactly (if it says comedian, that character is a comedian; "
               "if it says politician, a politician) and make that person specific and interesting rather than a stereotype. "
               'A slot marked "your choice" is yours: pick whoever this topic needs most.',
               *[f'{i + 1}. {sl or "your choice"}' for i, sl in enumerate(slots)]]
              if slots else [f"Invite {count} {'person' if count == 1 else 'people'} so the conversation will be genuinely good on THIS topic."]),
            *([f"The show's creator also says: {hint}"] if hint else []),
            *(["There is no host yet, so the first one must have role \"host\"."] if need_host else
              ['Everyone you invite has role "guest".']),
            "",
            "First look through this library of ready-made characters. Use one only if it truly fits the topic, by returning "
            '{"template": "<its id>"}:',
            lib,
            "First work out what this episode is for: a request (cheer someone up, help them decide, teach them something), a fun "
            "conversation, a debate or an explainer. Cast for that, and make the cast's attitudes, energy, speed and style fit it. "
            "An upbeat or comedy topic needs funny, warm, quick people (speed 0 to 2), not solemn or melancholy ones.",
            "Wherever none fits, write a whole new character: a vivid, specific person with a real stake in this topic, not a job title. "
            "Bring different angles and real chemistry. Add someone who disagrees only when the topic is genuinely contested. Do not repeat "
            "a profession, attitude or voice already in the cast. New characters have these fields:",
            '{"name": "<first name, with a title only if natural>", "role": "host|guest", "gender": "female|male", "voice": "<exactly one name from the voice list>", '
            f'"attitude": "<one of: {", ".join(ATTITUDES)}>", "persona": "<3 to 4 sentences: who they are, what they believe about this topic, and two or three different specific experiences or stories they can draw on>", '
            '"style": "<a short phrase on how they talk>", "speed": <-2 to 2>, "expr": <0 to 3>, '
            '"longest": <0, 60, 120 or 180: how long they may talk at a stretch; give 120 to people who explain or tell stories, 0 to quick talkers>}',
            "Choose each voice to suit the person (gender, age, energy). Never reuse a voice. Voices marked novelty are for fantasy or role-play topics only. Voices:",
            *[line(v) for v in voices if v["name"] not in taken_voices],
            "",
            'Reply with JSON only: {"characters": [ ... ]}',
        ])
        messages = [{"role": "system", "content": system}, {"role": "user", "content": "Cast the episode. JSON only."}]
        data, _, name = chat(req, messages, 420 * count + 200, 0.9, timeout=60)
        text = re.sub(r"(?s)<think>.*?(</think>|$)", "", data["choices"][0]["message"].get("content") or "")
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise RuntimeError(f"{name} did not return any characters")
        raw = json.loads(m.group(0))
        raw["usage"] = data.get("usage", {})

    by_voice = {v["name"]: v for v in voices}
    used = set(taken_voices)
    made = []
    for item in (raw.get("characters") or [])[:count]:
        if not isinstance(item, dict):
            continue
        tpl = next((t for t in library if item.get("template") and t["id"] == item["template"]), None)
        if tpl:
            c = dict(tpl)
            if c.get("voice") in used:  # a template's own voice may already be taken: swap in a free one of the same gender
                gender = (by_voice.get(c["voice"]) or {}).get("gender")
                free = [v for v in voices if v["name"] not in used and v.get("locale") != "characters"]
                c["voice"] = next((v["name"] for v in free if v.get("gender") == gender), free[0]["name"] if free else c["voice"])
        else:
            c = {"name": str(item.get("name", "")).strip()[:40], "role": "guest", "voice": str(item.get("voice", "")).strip(),
                 "attitude": item.get("attitude") if item.get("attitude") in ATTITUDES else "balanced",
                 "persona": str(item.get("persona", "")).strip()[:1200], "style": str(item.get("style", "")).strip()[:200],
                 "speed": _clamp_int(item.get("speed"), -2, 2, 0), "expr": _clamp_int(item.get("expr"), 0, 3, 1),
                 "length": "", "longest": _snap_longest(item.get("longest"))}
            # A voice must exist and be free; otherwise take the first free regular voice of the right gender.
            if c["voice"] not in by_voice or c["voice"] in used:
                gender = str(item.get("gender", "")).lower()
                free = [v for v in voices if v["name"] not in used and v.get("locale") != "characters"]
                c["voice"] = next((v["name"] for v in free if v.get("gender") == gender), free[0]["name"] if free else "")
        if not c["name"] or not (c.get("persona") or "").strip():
            continue
        base, n = c["name"], 2
        while c["name"].lower() in taken_names:
            c["name"] = f"{base} {n}"; n += 1
        taken_names.add(c["name"].lower())
        used.add(c.get("voice"))
        c["role"] = "guest"
        c["id"] = "c" + str(int(time.time() * 1000) + len(made))
        made.append(c)
    if not made:
        raise RuntimeError("The casting director did not come up with anyone usable. Try again, or describe who you want.")
    if need_host:
        made[0]["role"] = "host"
    return {"characters": made, "usage": {"in": raw.get("usage", {}).get("prompt_tokens", 0), "out": raw.get("usage", {}).get("completion_tokens", 0)}}


def fake_floor(me, words):
    base = ("Let me take the long way round, because the short answer hides the interesting part. When I first ran into this, "
            "nobody could say why it worked, only that it did. The more we looked, the more it came down to habits, not tools. "
            "People repeat what is easy, so the easy path decides the outcome, whatever the design says. ")
    out = []
    while len(" ".join(out).split()) < words:
        out.append(base)
    return " ".join(" ".join(out).split()[:words]).rstrip(",;") + "."


def fake_line(me, cast, transcript, phase):
    others = [c["name"] for c in cast if c["name"] != me["name"]]
    if phase == "sample":
        return f"Hi, I'm {me['name']}. You'll hear plenty from me on this one."
    if phase == "open":
        return f"Welcome to the show, everyone. I'm {me['name']}, and today I'm here with {', '.join(others)}. {random.choice(others)}, what's your first take?"
    if phase == "reopen":
        return f"Welcome back, everyone. We were in the middle of it. {random.choice(others)}, pick it up from there?"
    if phase == "close":
        return f"That's all we have time for. Thank you, {', '.join(others)}. See you next week."
    line = random.choice([
        "That's a fair point, but I see it a little differently.",
        "I agree with part of that, and here's an example from my own work.",
        "Let me push back on that for a second.",
        "Honestly, I think the real question is about cost, and nobody in this industry wants to talk about that part.",
    ])
    if me.get("role") == "host" and random.random() < 0.2:
        line += " [REPLAN: give this part a bit more time]"
    if others and random.random() < 0.5:
        line += f" What do you think, {random.choice(others)}?"
    elif others and random.random() < 0.8:
        line += f"\n[NEXT: {random.choice(others)}]"
    return line
