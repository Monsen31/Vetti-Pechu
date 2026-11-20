"""
Vetti Pechu: a Claude-powered friend for your Discord group chat.
("Vetti pechu" is Tamil for idle chatter.)

It replies whenever someone @mentions it (or replies to one of its
messages). It reads the last few messages in the channel so it can follow
the conversation.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse
from pathlib import Path

import anthropic
import discord
import httpx
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()  # reads the .env file next to this script

# ---- Settings (change these in .env, not here) -------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
# Which AI answers: "claude" (default) or "groq" (free tier).
PROVIDER = os.getenv("PROVIDER", "claude").strip().lower()
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
# Used if the main Groq model fails or hits its limit.
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
# Groq's built-in browser search only works on its gpt-oss models.
GROQ_SEARCH_MODEL = os.getenv("GROQ_SEARCH_MODEL", "openai/gpt-oss-120b")
# gpt-oss can't see images, so pictures go to Groq's vision model instead.
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
# Image understanding: look at pictures people share with the bot.
IMAGES = os.getenv("IMAGES", "on").lower() in ("on", "true", "yes", "1")
MAX_IMAGES = 3
MAX_IMAGE_MB = 5  # bigger files are skipped (the bot never downloads them itself)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
BOT_NAME = os.getenv("BOT_NAME", "Vetti Pechu")

# Web browsing: lets the bot search the web and read pages for fresh info.
# With Claude each web search costs $0.01 (on top of tokens), so the caps keep it cheap.
WEB_BROWSING = os.getenv("WEB_BROWSING", "on").lower() in ("on", "true", "yes", "1")
WEB_MAX_SEARCHES = int(os.getenv("WEB_MAX_SEARCHES", "3"))  # per reply
WEB_MAX_PAGE_READS = int(os.getenv("WEB_MAX_PAGE_READS", "2"))  # per reply

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    f"You are {BOT_NAME}, one of the friends in a casual Discord group chat. "
    "Your name is Tamil for idle chatter, and that's your vibe: easy-going, "
    "playful banter, happy to riff on whatever the group is talking about, "
    "but genuinely helpful when someone asks a real question. "
    "You'll see the recent conversation as lines of 'Name: message'. "
    "Reply to the latest message that mentions you, like a friend would: "
    "short, natural and warm, usually one to three sentences. "
    "Don't start your reply with your own name. Use Discord markdown sparingly.",
)

if WEB_BROWSING:
    SYSTEM_PROMPT += (
        " You can search the web and read web pages. Use them when someone asks "
        "about news, scores, prices, weather, recent events, or anything you're "
        "not sure is still true, or when someone shares a link. Don't search for "
        "casual chat. Give the answer in your own words; don't paste long quotes."
    )

WEB_TOOLS = [
    # Haiku uses these versions of the server-side web tools.
    {"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_MAX_SEARCHES},
    {
        "type": "web_fetch_20250910",
        "name": "web_fetch",
        "max_uses": WEB_MAX_PAGE_READS,
        "max_content_tokens": 8000,  # caps how much of a page it reads
    },
]
MAX_SOURCES = 3

# Also reply when someone pings @everyone or @here.
REPLY_TO_EVERYONE = os.getenv("REPLY_TO_EVERYONE", "on").lower() in ("on", "true", "yes", "1")

# Long-term memory: names and facts saved to a small JSON file next to bot.py.
MEMORY = os.getenv("MEMORY", "on").lower() in ("on", "true", "yes", "1")
MEMORY_FILE = Path(os.getenv("MEMORY_FILE", Path(__file__).with_name("memory.json")))
MAX_FACTS_PER_PERSON = 15
MAX_FACT_CHARS = 200

if MEMORY:
    SYSTEM_PROMPT += (
        " You have a long-term memory. Before the chat you'll see who is in the group "
        "and what you remember about them. When someone shares something worth "
        "remembering about a person (nicknames, likes, birthdays, jobs, plans, running "
        "jokes), save it with the remember tool, and use forget when asked to or when "
        "a fact is out of date. Don't save small talk. Don't announce that you saved "
        "something unless it fits naturally."
    )

MEMORY_TOOLS = [
    {
        "name": "remember",
        "description": "Save a lasting fact about a person in this group chat.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "The person's display name as shown in the chat"},
                "fact": {"type": "string", "description": "One short fact, e.g. 'supports Arsenal'"},
            },
            "required": ["person", "fact"],
            "additionalProperties": False,
        },
    },
    {
        "name": "forget",
        "description": "Remove a saved fact about a person.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "person": {"type": "string", "description": "The person's display name"},
                "fact": {"type": "string", "description": "Words from the fact to remove"},
            },
            "required": ["person", "fact"],
            "additionalProperties": False,
        },
    },
]

GROQ_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current information (news, scores, prices, "
                       "weather, recent events, or anything that may have changed).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What to look up"}},
            "required": ["query"],
        },
    },
}

SYSTEM_PROMPT += (
    " If you share a link, write it as a Discord masked link with a few words, "
    "like [BBC match report](https://...), never as a bare URL."
)

# /persona presets: (label shown in Discord, style instructions). These are
# playful impressions for a friends' chat, never claims to be the real person.
PERSONAS = {
    "chef": ("Angry chef (Ramsay-style)",
             "a fiery celebrity TV chef doing a Gordon Ramsay-style impression: dramatic "
             "outrage, kitchen insults that stay PG ('it's RAW!', 'you donkey'), brutal "
             "honesty, then a sudden warm compliment"),
    "president": ("Big-talk president (Trump-style)",
                  "a playful Donald Trump-style impression: superlatives ('tremendous', "
                  "'the best, believe me'), boasting, nicknames for people in the chat, "
                  "rambling tangents and the odd ALL-CAPS word. Keep it light banter, "
                  "no real-world politics"),
    "hero": ("Kollywood mass hero",
             "a Tamil cinema mass hero: punch dialogues, slow-motion-entry swagger, "
             "dramatic pauses, and a sprinkle of Tanglish"),
    "commentator": ("Football commentator",
                    "an over-excited football commentator narrating every message like a "
                    "last-minute winner"),
    "butler": ("Posh butler", "an impeccably polite, very posh British butler with dry wit"),
    "pirate": ("Pirate", "a salty pirate captain: arr, me hearties, sea metaphors"),
    "shakespeare": ("Shakespeare", "a Shakespearean actor speaking in flowery Elizabethan English"),
}
MAX_CUSTOM_PERSONA_CHARS = 300

DISCORD_MAX_CHARS = 2000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vetti-pechu")
logging.getLogger("httpx").setLevel(logging.WARNING)  # quieter logs

# ANTHROPIC_API_KEY is picked up from the environment automatically.
if PROVIDER not in ("claude", "groq"):
    raise SystemExit(f"Unknown PROVIDER={PROVIDER!r}; use claude or groq")
if PROVIDER == "groq" and not GROQ_API_KEY:
    raise SystemExit("PROVIDER=groq but GROQ_API_KEY is missing from .env")
claude = anthropic.AsyncAnthropic() if PROVIDER == "claude" else None
groq = httpx.AsyncClient(timeout=60) if PROVIDER == "groq" else None

intents = discord.Intents.default()
intents.message_content = True  # must also be switched on in the Developer Portal
bot = discord.Client(intents=intents)


def is_image(a: discord.Attachment) -> bool:
    return (a.content_type or "").startswith("image/")


def clean(msg: discord.Message) -> str:
    """Turn a Discord message into readable text (mentions become @Name)."""
    text = msg.clean_content.strip()
    for a in msg.attachments:
        text += " [shared an image]" if is_image(a) else " [shared a file]"
    return text or "[empty message]"


def image_urls(*messages) -> list[str]:
    """Links to pictures in these messages (the AI fetches them, not this server)."""
    if not IMAGES:
        return []
    urls = []
    for m in messages:
        for a in getattr(m, "attachments", None) or []:
            if is_image(a) and a.size <= MAX_IMAGE_MB * 1024 * 1024:
                urls.append(a.url)
    return urls[:MAX_IMAGES]


class Memory:
    """A tiny JSON store: per chat, who's who and facts about each person."""

    def __init__(self, path: Path):
        self.path = path
        self.data = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except (OSError, ValueError):
                log.exception("Couldn't read %s; starting with empty memory", path)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False))
        os.replace(tmp, self.path)  # never leaves a half-written file

    def chat(self, key: str) -> dict:
        return self.data.setdefault(key, {"people": {}, "facts": {}})

    def see(self, key: str, member) -> bool:
        """Record a person's current name. Returns True if anything changed."""
        people = self.chat(key)["people"]
        entry = {"name": member.display_name, "username": member.name}
        if people.get(str(member.id)) == entry:
            return False
        people[str(member.id)] = entry
        return True

    def remember(self, key: str, person: str, fact: str) -> str:
        facts = self.chat(key)["facts"].setdefault(person.strip(), [])
        fact = fact.strip()[:MAX_FACT_CHARS]
        if fact and fact not in facts:
            facts.append(fact)
            del facts[:-MAX_FACTS_PER_PERSON]  # keep the newest
            self.save()
        return f"Saved: {person}: {fact}"

    def forget(self, key: str, person: str, fact: str) -> str:
        facts = self.chat(key)["facts"].get(person.strip(), [])
        keep = [f for f in facts if fact.lower() not in f.lower()]
        removed = len(facts) - len(keep)
        if removed:
            self.chat(key)["facts"][person.strip()] = keep
            self.save()
        return f"Removed {removed} fact(s) about {person}."

    def summary(self, key: str) -> str:
        chat = self.chat(key)
        lines = []
        if chat["people"]:
            names = ", ".join(f"{p['name']} (@{p['username']})" for p in chat["people"].values())
            lines.append(f"People in this chat: {names}")
        facts = [(who, f) for who, fs in chat["facts"].items() for f in fs]
        if facts:
            lines.append("What you remember:")
            lines += [f"- {who}: {f}" for who, f in facts]
        return "\n".join(lines)


store = Memory(MEMORY_FILE)  # also holds personas and /reset points
memory = store if MEMORY else None


def system_for(key: str) -> str:
    persona = store.chat(key).get("persona")
    if not persona:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT + (
        f" For fun, the group has asked you to talk like {persona['style']}. Commit to the "
        f"bit, but you're still {BOT_NAME}: it's an impression, so never claim to really be "
        "anyone, and keep replies short and friendly."
    )


def chat_key(channel) -> str:
    guild = getattr(channel, "guild", None)
    return f"server-{guild.id}" if guild else f"dm-{channel.id}"


async def build_transcript(channel) -> str:
    reset = store.chat(chat_key(channel)).get("reset_at", {}).get(str(channel.id))
    after = datetime.fromisoformat(reset) if reset else None
    history = [m async for m in channel.history(limit=HISTORY_LIMIT, after=after,
                                                 oldest_first=False)]
    history.reverse()  # oldest first
    lines = []
    if memory:
        key = chat_key(channel)
        changed = [memory.see(key, m.author) for m in history if not m.author.bot]
        if any(changed):
            memory.save()
    for m in history:
        name = BOT_NAME if m.author == bot.user else m.author.display_name
        lines.append(f"{name}: {clean(m)}")
    return "\n".join(lines)


def short_label(url: str, title: str | None = None, max_words: int = 6) -> str:
    """A few readable words for a link: the page title if known, else from the URL."""
    words = []
    if title:
        title = re.split(r"\s+[|\-–—]\s+", title.strip())[0]  # drop " - Site Name"
        words = title.split()
    if not words:
        parsed = urlparse(url)
        site = parsed.netloc.lower().removeprefix("www.")
        slug = [p for p in unquote(parsed.path).split("/") if p and not p.isdigit()]
        slug_words = re.split(r"[-_+.]+", slug[-1]) if slug else []
        slug_words = [w for w in slug_words if w and not w.isdigit() and w.lower() not in ("html", "htm", "php")]
        words = [site + (":" if slug_words else "")] + slug_words
    label = " ".join(words[:max_words]) + ("…" if len(words) > max_words else "")
    return re.sub(r"[\[\]()<>`*_~|]", "", label).strip() or "link"


def masked(url: str, title: str | None = None) -> str:
    # <...> stops Discord from showing a big preview for each link.
    return f"[{short_label(url, title)}](<{url}>)"


def format_sources(sources: list[tuple[str, str | None]]) -> str:
    return "\n-# Sources: " + " · ".join(masked(u, t) for u, t in sources[:MAX_SOURCES])


MD_LINK = re.compile(r"\[([^\]]+)\]\(<?(https?://[^\s)>]+)>?\)")
BARE_URL = re.compile(r"<?(https?://[^\s<>]+[^\s<>.,;:!?)\]'\"])>?")


def shorten_links(text: str) -> str:
    """Turn bare URLs into a few hyperlinked words and stop link previews."""
    keep = []

    def stash(m):
        label = re.sub(r"https?://\S+", "", m.group(1)).strip() or short_label(m.group(2))
        keep.append(f"[{label}](<{m.group(2)}>)")
        return f"\x00{len(keep) - 1}\x00"

    text = MD_LINK.sub(stash, text)  # existing masked links: keep their words
    text = BARE_URL.sub(lambda m: masked(m.group(1)), text)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def source_links(content) -> list[tuple[str, str | None]]:
    """Collect the web pages Claude cited, so friends can check them."""
    found = {}
    for block in content:
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in found:
                found[url] = getattr(c, "title", None)
    return list(found.items())[:MAX_SOURCES]


def add_assistant(messages: list, content) -> None:
    """Append Claude's turn, merging with the last one if it was also Claude's."""
    if messages[-1]["role"] == "assistant":
        messages[-1]["content"] = list(messages[-1]["content"]) + list(content)
    else:
        messages.append({"role": "assistant", "content": list(content)})


def run_memory_tool(key: str, name: str, args) -> tuple[str, bool]:
    """Run remember/forget. Returns (result text, is_error)."""
    args = args if isinstance(args, dict) else {}
    person, fact = str(args.get("person", "")), str(args.get("fact", ""))
    if not memory or not person or not fact:
        result, error = "Missing person or fact.", True
    elif name == "remember":
        result, error = memory.remember(key, person, fact), False
    elif name == "forget":
        result, error = memory.forget(key, person, fact), False
    else:
        result, error = f"Unknown tool {name}", True
    log.info("Memory %s: %s", name, result)
    return result, error


def build_prompt(transcript: str, key: str) -> str:
    if memory:
        known = memory.summary(key)
        if known:
            return f"<memory>\n{known}\n</memory>\n\nRecent chat:\n{transcript}"
    return transcript


async def ask(transcript: str, key: str, images: list[str] = ()) -> str:
    prompt = build_prompt(transcript, key)
    if images:
        prompt += f"\n\n(The {len(images)} image(s) attached are from the latest messages.)"
    if PROVIDER == "groq":
        return await ask_groq(prompt, key, list(images))
    return await ask_claude(prompt, key, list(images))


THINK = re.compile(r"<think>.*?</think>|\s*【[^】]*】", re.S)  # reasoning text, citation marks


class GroqFailed(Exception):
    pass


async def groq_call(body: dict, models: list[str]) -> dict:
    """Call Groq, trying each model in turn. Returns the response message."""
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    last = None
    for model in dict.fromkeys(m for m in models if m):  # unique, in order
        payload = {**body, "model": model}
        if model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = "low"  # chat doesn't need deep thinking
        try:
            r = await groq.post(GROQ_URL, json=payload, headers=headers)
        except httpx.HTTPError as e:
            log.warning("Groq %s unreachable: %s", model, e)
            last = "unreachable"
            continue
        if r.status_code == 401:
            raise GroqFailed("key")
        if r.status_code >= 400:
            log.warning("Groq %s error %s: %s", model, r.status_code, r.text[:300])
            last = "limit" if r.status_code == 429 else "error"
            continue
        data = r.json()
        usage = data.get("usage", {})
        log.info("Reply (groq %s): in=%s out=%s", model,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"))
        return data["choices"][0]["message"]
    raise GroqFailed(last or "error")


def find_links(obj, found=None) -> dict:
    """Pull web addresses (and titles) out of Groq's tool output, whatever its shape."""
    found = {} if found is None else found
    if isinstance(obj, dict):
        url = obj.get("url")
        if isinstance(url, str) and url.startswith("http") and url not in found:
            title = obj.get("title")
            found[url] = title if isinstance(title, str) else None
        for k, v in obj.items():
            if k != "url":
                find_links(v, found)
    elif isinstance(obj, list):
        for v in obj:
            find_links(v, found)
    return found


async def groq_search(query: str, sources: list) -> str:
    """Run one web search with Groq's built-in browser search."""
    body = {
        "messages": [{"role": "user", "content":
                      f"Search the web and give a short factual answer with key details: {query}"}],
        "tools": [{"type": "browser_search"}],
        "max_tokens": 1024,
    }
    try:
        msg = await groq_call(body, [GROQ_SEARCH_MODEL, "openai/gpt-oss-20b"])
    except GroqFailed as e:
        log.warning("Groq web search failed (%s)", e)
        return "Web search isn't available right now. Answer from what you know and say you couldn't check."
    for url, title in find_links(msg.get("executed_tools")).items():
        if url not in [u for u, _ in sources]:
            sources.append((url, title))
    answer = re.sub(r"\s*【[^】]*】", "", msg.get("content") or "").strip()  # drop citation markers
    log.info("Groq web search: %r", query)
    return answer or "No results found."


async def ask_groq(prompt: str, key: str, images: list[str]) -> str:
    """Groq's free API (OpenAI-style), with memory tools, web search and images."""
    def user_turn(with_images: bool):
        if not with_images:
            note = "\n(Someone shared an image you couldn't open this time.)" if images else ""
            return {"role": "user", "content": prompt + note}
        return {"role": "user", "content": [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": u}} for u in images]}

    messages = [{"role": "system", "content": system_for(key)}, user_turn(bool(images))]
    models = [GROQ_VISION_MODEL] if images else [GROQ_MODEL, GROQ_FALLBACK_MODEL]
    body = {"max_tokens": 1024}
    tools = [GROQ_SEARCH_TOOL] if WEB_BROWSING else []
    if memory:
        tools += [
            {"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in MEMORY_TOOLS
        ]
    if tools:
        body["tools"] = tools
    sources: list[tuple[str, str | None]] = []
    searches = 0

    for _ in range(5):
        try:
            msg = await groq_call({**body, "messages": messages}, models)
        except GroqFailed as e:
            if images and models == [GROQ_VISION_MODEL] and str(e) != "key":
                # The vision model failed: answer without the picture instead.
                log.warning("Groq vision model failed (%s); answering without images", e)
                messages[1] = user_turn(False)
                models = [GROQ_MODEL, GROQ_FALLBACK_MODEL]
                continue
            if str(e) == "key":
                log.error("Groq API key is wrong. Check GROQ_API_KEY in .env")
                return "My Groq key isn't working. Ask whoever runs me to check it."
            if str(e) == "limit":
                return "I've hit my free limit for now. Give me a minute and try again."
            return "Something went wrong on my end. Try again later."
        calls = msg.get("tool_calls") or []
        if not calls:
            text = THINK.sub("", msg.get("content") or "").strip()
            if not text:
                return "🤔"
            if sources:
                text += format_sources(sources)
            return text
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            if name == "web_search":
                searches += 1
                if searches > WEB_MAX_SEARCHES:
                    result = "Search limit reached for this reply. Answer with what you have."
                else:
                    result = await groq_search(str(args.get("query", "")), sources)
            else:
                result, _ = run_memory_tool(key, name, args)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return "👍"


async def ask_claude(prompt: str, key: str, images: list[str]) -> str:
    content = [{"type": "image", "source": {"type": "url", "url": u}} for u in images]
    messages = [{"role": "user", "content": content + [{"type": "text", "text": prompt}]}]
    tools = (WEB_TOOLS if WEB_BROWSING else []) + (MEMORY_TOOLS if memory else [])
    request = dict(
        model=MODEL,
        max_tokens=2048,
        system=system_for(key),
        output_config={"effort": "low"},  # chat doesn't need deep thinking; keeps it cheap and fast
    )
    if tools:
        request["tools"] = tools

    texts, content = [], []
    for _ in range(6):  # a few rounds at most: web lookups and memory saves
        response = await claude.messages.create(messages=messages, **request)
        content += response.content
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if text:
            texts.append(text)
        if response.stop_reason == "pause_turn":
            # A long web lookup paused partway; send it back so it can carry on.
            add_assistant(messages, response.content)
        elif response.stop_reason == "tool_use":
            add_assistant(messages, response.content)
            results = []
            for b in response.content:
                if b.type == "tool_use":
                    result, error = run_memory_tool(key, b.name, b.input)
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": result, "is_error": error})
            messages.append({"role": "user", "content": results})
        else:
            break

    usage = response.usage
    searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", 0) or 0
    log.info("Reply: stop=%s in=%s out=%s searches=%s",
             response.stop_reason, usage.input_tokens, usage.output_tokens, searches)

    if response.stop_reason == "refusal":
        return "I'd rather not weigh in on that one."
    # Keep only the text (the reply can also hold thinking, web results and tool calls).
    text = "\n".join(texts).strip()
    if not text:
        return "👍" if any(b.type == "tool_use" for b in content) else "🤔"
    sources = source_links(content)
    if sources:
        text += format_sources(sources)
    return text


def split_for_discord(text: str) -> list[str]:
    chunks = []
    while len(text) > DISCORD_MAX_CHARS:
        cut = text.rfind("\n", 0, DISCORD_MAX_CHARS)
        if cut <= 0:
            cut = DISCORD_MAX_CHARS
        chunks.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


def is_for_me(msg: discord.Message) -> bool:
    if bot.user in msg.mentions:
        return True
    # Typing "@Vetti Pechu" sometimes picks the bot's role instead of the bot.
    me = msg.guild.me if msg.guild else None
    if me and any(role in msg.role_mentions for role in me.roles if role.managed):
        return True
    # mention_everyone is only set when the sender is allowed to ping everyone,
    # so also check the text itself.
    if REPLY_TO_EVERYONE and (
        msg.mention_everyone or "@everyone" in msg.content or "@here" in msg.content
    ):
        return True
    ref = msg.reference.resolved if msg.reference else None
    return isinstance(ref, discord.Message) and ref.author == bot.user


async def answer(transcript: str, key: str, images: list[str] = ()) -> str:
    """Get a reply, turning any failure into a friendly message."""
    try:
        return await ask(transcript, key, images)
    except anthropic.BadRequestError as e:
        if images:
            log.warning("Claude couldn't use the image(s): %s; retrying without", e.message)
            return await answer(transcript + "\n(Someone shared an image you couldn't open.)", key)
        log.error("Claude API error %s: %s", e.status_code, e.message)
        return "Something went wrong on my end. Try again later."
    except anthropic.RateLimitError:
        log.warning("Rate limited by the Claude API")
        return "I'm getting a lot of messages right now. Try me again in a minute."
    except anthropic.AuthenticationError:
        log.error("Claude API key is missing or wrong. Check ANTHROPIC_API_KEY in .env")
        return "My API key isn't working. Ask whoever runs me to check it."
    except anthropic.APIStatusError as e:
        log.error("Claude API error %s: %s", e.status_code, e.message)
        return "Something went wrong on my end (maybe out of API credit?). Try again later."
    except anthropic.APIConnectionError:
        log.exception("Couldn't reach the Claude API")
        return "I can't reach my brain right now. Try again in a moment."
    except Exception:
        log.exception("Unexpected error while answering")
        return "Oops, something broke on my side. Try again?"


# ---- Slash commands -----------------------------------------------------
tree = app_commands.CommandTree(bot)
synced_guilds: set[int] = set()


async def sync_commands(guild: discord.Guild):
    """Register the slash commands in a server (shows up straight away)."""
    if guild.id in synced_guilds:
        return
    try:
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
        synced_guilds.add(guild.id)
        log.info("Slash commands ready in %s", guild.name)
    except discord.Forbidden:
        log.error("Can't add slash commands in %s. Re-invite the bot with the "
                  "applications.commands scope (see README).", guild.name)


async def send_chunks(interaction: discord.Interaction, text: str, ephemeral: bool = False):
    for chunk in split_for_discord(shorten_links(text)):
        await interaction.followup.send(chunk, ephemeral=ephemeral)


persona = app_commands.Group(name="persona", description=f"Change how {BOT_NAME} talks")


@persona.command(name="pick", description="Pick a fun speaking style")
@app_commands.describe(style="The style to use")
@app_commands.choices(style=[app_commands.Choice(name=label, value=k)
                             for k, (label, _) in PERSONAS.items()])
async def persona_pick(interaction: discord.Interaction, style: app_commands.Choice[str]):
    label, desc = PERSONAS[style.value]
    store.chat(chat_key(interaction.channel))["persona"] = {"name": label, "style": desc}
    store.save()
    await interaction.response.send_message(f"🎭 {BOT_NAME} is now: **{label}**. "
                                            "Use `/persona default` to switch back.")


@persona.command(name="custom", description="Describe your own speaking style")
@app_commands.describe(description="e.g. a grumpy cat who secretly loves everyone")
async def persona_custom(interaction: discord.Interaction, description: str):
    description = description.strip()[:MAX_CUSTOM_PERSONA_CHARS]
    store.chat(chat_key(interaction.channel))["persona"] = {"name": description, "style": description}
    store.save()
    await interaction.response.send_message(f"🎭 {BOT_NAME} will now talk like: *{description}*. "
                                            "Use `/persona default` to switch back.")


@persona.command(name="default", description=f"Go back to normal {BOT_NAME}")
async def persona_default(interaction: discord.Interaction):
    store.chat(chat_key(interaction.channel)).pop("persona", None)
    store.save()
    await interaction.response.send_message(f"🙂 Back to plain old {BOT_NAME}.")


@persona.command(name="show", description="See which style is on")
async def persona_show(interaction: discord.Interaction):
    p = store.chat(chat_key(interaction.channel)).get("persona")
    await interaction.response.send_message(
        f"🎭 Current style: **{p['name']}**" if p else f"🙂 Just plain {BOT_NAME} right now.",
        ephemeral=True)


tree.add_command(persona)


@tree.command(name="ask", description=f"Ask {BOT_NAME} something")
@app_commands.describe(question="Your question", image="Optional picture to look at")
async def ask_command(interaction: discord.Interaction, question: str,
                      image: discord.Attachment | None = None):
    await interaction.response.defer(thinking=True)
    channel = interaction.channel
    transcript = await build_transcript(channel) if channel else ""
    transcript += f"\n{interaction.user.display_name}: @{BOT_NAME} {question}"
    if image and is_image(image):
        transcript += " [shared an image]"
    images = image_urls(NamedAttachments([image])) if image else []
    reply = await answer(transcript.strip(), chat_key(channel), images)
    await send_chunks(interaction, f"> {question[:300]}\n{reply}")


class NamedAttachments:
    """Lets image_urls() read a slash-command attachment like a message's."""

    def __init__(self, attachments):
        self.attachments = attachments


@tree.command(name="reset", description=f"Make {BOT_NAME} forget things")
@app_commands.describe(what="What to forget")
@app_commands.choices(what=[
    app_commands.Choice(name="The conversation so far (in this channel)", value="chat"),
    app_commands.Choice(name="Saved names and facts", value="memory"),
    app_commands.Choice(name="Everything, including the persona", value="everything"),
])
async def reset_command(interaction: discord.Interaction, what: app_commands.Choice[str]):
    chat = store.chat(chat_key(interaction.channel))
    if what.value in ("chat", "everything"):
        now = datetime.now(timezone.utc).isoformat()
        chat.setdefault("reset_at", {})[str(interaction.channel_id)] = now
    if what.value in ("memory", "everything"):
        chat["people"], chat["facts"] = {}, {}
    if what.value == "everything":
        chat.pop("persona", None)
    store.save()
    done = {"chat": "Fresh start: I'll ignore everything said here before now.",
            "memory": "Done: I've forgotten the names and facts I'd saved.",
            "everything": "Clean slate: conversation, saved facts and persona all reset."}
    await interaction.response.send_message(f"🧹 {done[what.value]}")


@bot.event
async def on_ready():
    log.info("Logged in as %s. AI: %s. Web browsing is %s. Memory is %s. Images are %s.", bot.user,
             f"Groq ({GROQ_MODEL}, backup {GROQ_FALLBACK_MODEL}, images {GROQ_VISION_MODEL})"
             if PROVIDER == "groq" else f"Claude ({MODEL})",
             "on" if WEB_BROWSING else "off", f"on ({MEMORY_FILE})" if memory else "off",
             "on" if IMAGES else "off")
    for guild in bot.guilds:
        await sync_commands(guild)


@bot.event
async def on_guild_join(guild: discord.Guild):
    await sync_commands(guild)


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not is_for_me(msg):
        return

    ref = msg.reference.resolved if msg.reference else None
    images = image_urls(msg, ref if isinstance(ref, discord.Message) else None)
    async with msg.channel.typing():
        try:
            transcript = await build_transcript(msg.channel)
        except Exception:
            log.exception("Couldn't read the channel history")
            transcript = f"{msg.author.display_name}: {clean(msg)}"
        reply = await answer(transcript, chat_key(msg.channel), images)

    chunks = split_for_discord(shorten_links(reply))
    await msg.reply(chunks[0], mention_author=False)
    for extra in chunks[1:]:
        await msg.channel.send(extra)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
