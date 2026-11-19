"""
Vetti Pechu: a Claude-powered friend for your Discord group chat.
("Vetti pechu" is Tamil for idle chatter.)

It replies whenever someone @mentions it (or replies to one of its
messages). It reads the last few messages in the channel so it can follow
the conversation.
"""

import logging
import os

import anthropic
import discord
from dotenv import load_dotenv

load_dotenv()  # reads the .env file next to this script

# ---- Settings (change these in .env, not here) -------------------------
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "20"))
BOT_NAME = os.getenv("BOT_NAME", "Vetti Pechu")

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


async def build_transcript(channel) -> str:
    history = [m async for m in channel.history(limit=HISTORY_LIMIT)]
    history.reverse()  # oldest first
    lines = []
    for m in history:
        name = BOT_NAME if m.author == bot.user else m.author.display_name
        lines.append(f"{name}: {clean(m)}")
    return "\n".join(lines)


async def ask_claude(transcript: str) -> str:
    response = await claude.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        output_config={"effort": "low"},  # chat doesn't need deep thinking; keeps it cheap and fast
        messages=[{"role": "user", "content": transcript}],
    )
    if response.stop_reason == "refusal":
        return "I'd rather not weigh in on that one."
    # The reply can include thinking blocks; keep only the text.
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text or "🤔"


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
    ref = msg.reference.resolved if msg.reference else None
    return isinstance(ref, discord.Message) and ref.author == bot.user


@bot.event
async def on_ready():
    log.info("Logged in as %s. Mention me in a channel to chat.", bot.user)


@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot or not is_for_me(msg):
        return

    try:
        async with msg.channel.typing():
            transcript = await build_transcript(msg.channel)
            reply = await ask_claude(transcript)
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

    chunks = split_for_discord(reply)
    await msg.reply(chunks[0], mention_author=False)
    for extra in chunks[1:]:
        await msg.channel.send(extra)


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN, log_handler=None)
