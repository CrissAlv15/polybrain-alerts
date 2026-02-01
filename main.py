import os
import asyncio
import discord

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

if not TOKEN or not CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")

CHANNEL_ID = int(CHANNEL_ID)

intents = discord.Intents.default()
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    channel = client.get_channel(CHANNEL_ID) or await client.fetch_channel(CHANNEL_ID)
    await channel.send("✅ PolyBrain is live on Railway. Alerts are working.")
    while True:
        await asyncio.sleep(3600)

client.run(TOKEN)
