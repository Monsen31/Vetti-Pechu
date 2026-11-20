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
from pathlib import Path

import anthropic
import discord
from dotenv import load_dotenv

load_dotenv()  # reads the .env file next to this script

# ---- Settings (change these in .env, not here) -------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
BOT_NAME = os.getenv("BOT_NAME", "Vetti Pechu")

# Web browsing: lets the bot search the web and read pages for fresh info.
# Each web search costs $0.01 (on top of tokens), so the caps keep it cheap.
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

DISCORD_MAX_CHARS = 2000

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vetti-pechu")

# ANTHROPIC_API_KEY is picked up from the environment automatically.
claude = anthropic.AsyncAnthropic()

intents = discord.Intents.default()
intents.message_content = True  # must also be switched on in the Developer Portal
bot = discord.Client(intents=intents)


def clean(msg: discord.Message) -> str:
    """Turn a Discord message into readable text (mentions become @Name)."""
    text = msg.clean_content.strip()
    if msg.attachments:
        text += " [shared an attachment]"
    return text or "[empty message]"


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


memory = Memory(MEMORY_FILE) if MEMORY else None


def chat_key(channel) -> str:
    guild = getattr(channel, "guild", None)
    return f"server-{guild.id}" if guild else f"dm-{channel.id}"


async def build_transcript(channel) -> str:
    history = [m async for m in channel.history(limit=HISTORY_LIMIT)]
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


def source_links(content) -> list[str]:
    """Collect the web pages Claude cited, so friends can check them."""
    urls = []
    for block in content:
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url and url not in urls:
                urls.append(url)
    return urls[:MAX_SOURCES]


def add_assistant(messages: list, content) -> None:
    """Append Claude's turn, merging with the last one if it was also Claude's."""
    if messages[-1]["role"] == "assistant":
        messages[-1]["content"] = list(messages[-1]["content"]) + list(content)
    else:
        messages.append({"role": "assistant", "content": list(content)})


def run_memory_tool(key: str, block) -> dict:
    args = block.input if isinstance(block.input, dict) else {}
    person, fact = str(args.get("person", "")), str(args.get("fact", ""))
    if not memory or not person or not fact:
        result, error = "Missing person or fact.", True
    elif block.name == "remember":
        result, error = memory.remember(key, person, fact), False
    elif block.name == "forget":
        result, error = memory.forget(key, person, fact), False
    else:
        result, error = f"Unknown tool {block.name}", True
    log.info("Memory %s: %s", block.name, result)
    return {"type": "tool_result", "tool_use_id": block.id, "content": result, "is_error": error}


async def ask_claude(transcript: str, key: str) -> str:
    prompt = transcript
    if memory:
        known = memory.summary(key)
        if known:
            prompt = f"<memory>\n{known}\n</memory>\n\nRecent chat:\n{transcript}"
    messages = [{"role": "user", "content": prompt}]
    tools = (WEB_TOOLS if WEB_BROWSING else []) + (MEMORY_TOOLS if memory else [])
    request = dict(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
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
            results = [run_memory_tool(key, b) for b in response.content if b.type == "tool_use"]
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
        # <...> stops Discord from showing a big preview for each link.
        text += "\n-# Sources: " + " · ".join(f"<{u}>" for u in sources)
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


@bot.event
async def on_ready():
    log.info("Logged in as %s. Web browsing is %s. Memory is %s.", bot.user,
             "on" if WEB_BROWSING else "off", f"on ({MEMORY_FILE})" if memory else "off")


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not is_for_me(msg):
        return

    try:
        async with msg.channel.typing():
            transcript = await build_transcript(msg.channel)
            reply = await ask_claude(transcript, chat_key(msg.channel))
    except anthropic.RateLimitError:
        log.warning("Rate limited by the Claude API")
        reply = "I'm getting a lot of messages right now. Try me again in a minute."
    except anthropic.AuthenticationError:
        log.error("Claude API key is missing or wrong. Check ANTHROPIC_API_KEY in .env")
        reply = "My API key isn't working. Ask whoever runs me to check it."
    except anthropic.APIStatusError as e:
        log.error("Claude API error %s: %s", e.status_code, e.message)
        reply = "Something went wrong on my end (maybe out of API credit?). Try again later."
    except anthropic.APIConnectionError:
        log.exception("Couldn't reach the Claude API")
        reply = "I can't reach my brain right now. Try again in a moment."
    except Exception:
        log.exception("Unexpected error while answering")
        reply = "Oops, something broke on my side. Try again?"

    chunks = split_for_discord(reply)
    await msg.reply(chunks[0], mention_author=False)
    for extra in chunks[1:]:
        await msg.channel.send(extra)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
