import os
import discord

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

if not TOKEN or not CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")

CHANNEL_ID_INT = int(CHANNEL_ID)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

_channel_cache = None

async def get_channel():
    global _channel_cache
    if _channel_cache is not None:
        return _channel_cache

    ch = client.get_channel(CHANNEL_ID_INT)
    if ch is None:
        # Fallback fetch
        ch = await client.fetch_channel(CHANNEL_ID_INT)

    _channel_cache = ch
    return ch

async def send(text: str):
    ch = await get_channel()
    await ch.send(text)
