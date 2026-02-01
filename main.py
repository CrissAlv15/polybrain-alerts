import os
import asyncio
import discord

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

if not TOKEN or not CHANNEL_ID:
    raise SystemExit("Missing DISCORD_BOT_TOKEN or DISCORD_CHANNEL_ID")

CHANNEL_ID = int(CHANNEL_ID)

intents = discord.Intents.default()
intents.guilds = True  # important for channel fetch in some setups

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    try:
        print(f"Logged in as: {client.user} (id={client.user.id})", flush=True)
        print(f"Attempting fetch_channel({CHANNEL_ID})...", flush=True)

        channel = await client.fetch_channel(CHANNEL_ID)  # force API fetch
        print(f"Fetched channel: {channel} (type={type(channel)})", flush=True)

        await channel.send("✅ PolyBrain is live on Railway. Alerts are working.")
        print("Sent message successfully ✅", flush=True)

    except Exception as e:
        print("❌ FAILED to send message:", repr(e), flush=True)
        raise

    while True:
        await asyncio.sleep(3600)

client.run(TOKEN)
