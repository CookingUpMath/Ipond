import os
import json
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from aiohttp import web

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ===== Web server for Railway =====
async def health_handler(request):
    return web.Response(text="iPond Top Duck bot online")

async def start_webserver():
    app = web.Application()
    app.router.add_get("/", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get('PORT', 8080)) # Railway sets PORT
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Web server running on port {port}")

# ===== Discord bot =====
TOKEN = os.environ["DISCORD_TOKEN"]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_guild_data(data, guild_id):
    gid = str(guild_id)
    if gid not in data:
        data[gid] = {
            "message_counts": {},
            "current_champion_id": None,
            "champion_role_id": None,
            "champion_vc_id": None,
            "announce_channel_id": None,
            "last_reset_date": None,
            "all_time_wins": {},
            "timezone_str": "UTC",
            "reset_hour": 0,
            "reset_minute": 0,
        }
    else:
        for key, default in [
            ("all_time_wins", {}),
            ("announce_channel_id", None),
            ("timezone_str", "UTC"),
            ("reset_hour", 0),
            ("reset_minute", 0),
        ]:
            if key not in data[gid]:
                data[gid][key] = default
    return data[gid]

def get_guild_tz(guild_data):
    try:
        return ZoneInfo(guild_data.get("timezone_str", "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")

# ===== Paste all your commands here =====
# Everything from @bot.event to @bot.tree.command goes here
# I kept your /setchannel and /forcereset commands below as examples

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(e)

@bot.tree.command(name="setchannel", description="Set the channel for daily champion announcements. (Admin)")
@app_commands.default_permissions(administrator=True)
async def slash_setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["announce_channel_id"] = str(channel.id)
    save_data(data)
    await interaction.response.send_message(
        f"✅ Announcement channel set to {channel.mention}. The daily champion will be announced there each day.",
        ephemeral=True,
    )

@bot.tree.command(name="forcereset", description="Force the daily reset and crown a champion now. (Admin)")
@app_commands.default_permissions(administrator=True)
async def slash_forcereset(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champion = await crown_champion(interaction.guild, guild_data)
    tz = get_guild_tz(guild_data)
    guild_data["message_counts"] = {}
    guild_data["last_reset_date"] = datetime.now(tz).strftime("%Y-%m-%d")
    save_data(data)

    if champion:
        await interaction.followup.send(
            f"✅ Reset complete! **{champion.display_name}** has been crowned today's champion.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "✅ Reset complete! No messages were tracked yet so no champion was crowned.",
            ephemeral=True,
        )

# ===== Add your other functions like crown_champion(), tasks.loop, etc above this line =====

# ===== Entry Point =====
async def main():
    await start_webserver() # Start Railway web server first
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())