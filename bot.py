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


def format_reset_time(guild_data):
    tz_str = guild_data.get("timezone_str", "UTC")
    hour = guild_data.get("reset_hour", 0)
    minute = guild_data.get("reset_minute", 0)
    suffix = "AM" if hour < 12 else "PM"
    display_hour = hour % 12 or 12
    return f"{display_hour}:{minute:02d} {suffix} {tz_str}"


async def update_presence():
    data = load_data()
    champion_nick = None
    for guild in bot.guilds:
        guild_data = get_guild_data(data, guild.id)
        uid = guild_data.get("current_champion_id")
        if uid:
            member = guild.get_member(int(uid))
            if member:
                champion_nick = member.display_name
                break
    if champion_nick:
        activity = discord.CustomActivity(name=f"👑 {champion_nick}")
    else:
        activity = discord.CustomActivity(name="Watching for top ducks 🦆")
    await bot.change_presence(activity=activity)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Failed to sync commands: {e}")
    await update_presence()
    daily_reset.start()


@bot.event
async def on_message(message):
    if message.author.bot:
        return
    if not message.guild:
        return

    data = load_data()
    guild_data = get_guild_data(data, message.guild.id)
    uid = str(message.author.id)
    guild_data["message_counts"][uid] = guild_data["message_counts"].get(uid, 0) + 1
    save_data(data)

    await bot.process_commands(message)


async def crown_champion(guild, guild_data):
    message_counts = guild_data.get("message_counts", {})
    if not message_counts:
        return None

    top_uid = max(message_counts, key=lambda uid: message_counts[uid])
    top_count = message_counts[top_uid]

    member = guild.get_member(int(top_uid))
    if not member:
        try:
            member = await guild.fetch_member(int(top_uid))
        except Exception:
            return None

    champion_role_id = guild_data.get("champion_role_id")
    champion_role = None

    if champion_role_id:
        champion_role = guild.get_role(int(champion_role_id))

    nick = member.display_name
    role_name = f"👑 {nick}"
    vc_name = f"👑: {nick}"

    if champion_role is None:
        try:
            champion_role = await guild.create_role(
                name=role_name,
                colour=discord.Colour.gold(),
                reason="Daily message champion role",
            )
            guild_data["champion_role_id"] = str(champion_role.id)
        except discord.Forbidden:
            print(f"[{guild.name}] Missing permission to create roles.")
            return None
    else:
        prev_champion_id = guild_data.get("current_champion_id")
        if prev_champion_id and str(prev_champion_id) != str(member.id):
            prev_member = guild.get_member(int(prev_champion_id))
            if prev_member and champion_role in prev_member.roles:
                try:
                    await prev_member.remove_roles(
                        champion_role, reason="Lost daily champion title"
                    )
                except discord.Forbidden:
                    pass
        try:
            await champion_role.edit(name=role_name, colour=discord.Colour.gold())
        except discord.Forbidden:
            pass

    if champion_role not in member.roles:
        try:
            await member.add_roles(champion_role, reason="Daily message champion")
        except discord.Forbidden:
            print(f"[{guild.name}] Missing permission to assign roles.")

    guild_data["current_champion_id"] = str(member.id)

    wins = guild_data.setdefault("all_time_wins", {})
    wins[str(member.id)] = wins.get(str(member.id), 0) + 1

    vc_id = guild_data.get("champion_vc_id")
    if vc_id:
        vc = guild.get_channel(int(vc_id))
        if vc and isinstance(vc, discord.VoiceChannel):
            try:
                await vc.edit(name=vc_name)
            except discord.Forbidden:
                print(f"[{guild.name}] Missing permission to rename VC.")

    announce_channel_id = guild_data.get("announce_channel_id")
    if announce_channel_id:
        channel = guild.get_channel(int(announce_channel_id))
        if channel:
            colour = member.colour if member.colour != discord.Colour.default() else discord.Colour.gold()
            embed = discord.Embed(
                description=f"-# All hail yesterday's top duck\n# 👑 {member.mention}",
                colour=colour,
            )
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                print(f"[{guild.name}] Missing permission to send in announce channel.")

    print(f"[{guild.name}] Champion: {nick} with {top_count} messages.")
    await update_presence()
    return member


@tasks.loop(minutes=1)
async def daily_reset():
    data = load_data()
    changed = False

    for guild in bot.guilds:
        guild_data = get_guild_data(data, guild.id)
        tz = get_guild_tz(guild_data)
        now_local = datetime.now(tz)
        today_local = now_local.strftime("%Y-%m-%d")

        reset_hour = guild_data.get("reset_hour", 0)
        reset_minute = guild_data.get("reset_minute", 0)
        last_reset = guild_data.get("last_reset_date")

        if (
            last_reset != today_local
            and now_local.hour == reset_hour
            and now_local.minute == reset_minute
        ):
            await crown_champion(guild, guild_data)
            guild_data["message_counts"] = {}
            guild_data["last_reset_date"] = today_local
            changed = True

    if changed:
        save_data(data)


@daily_reset.before_loop
async def before_daily_reset():
    await bot.wait_until_ready()


# ─── Slash Commands ────────────────────────────────────────────────────────────

@bot.tree.command(name="leaderboard", description="Show today's message leaderboard.")
async def slash_leaderboard(interaction: discord.Interaction):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    counts = guild_data.get("message_counts", {})

    if not counts:
        await interaction.response.send_message("No messages tracked yet today.", ephemeral=True)
        return

    sorted_counts = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = []
    for i, (uid, count) in enumerate(sorted_counts, 1):
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"Unknown ({uid})"
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        lines.append(f"{medal} **{name}** — {count} messages")

    embed = discord.Embed(
        title="📊 Today's Message Leaderboard",
        description="\n".join(lines),
        colour=discord.Colour.gold(),
    )
    embed.set_footer(text=f"Resets daily at {format_reset_time(guild_data)}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="champion", description="Show the current daily champion.")
async def slash_champion(interaction: discord.Interaction):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    uid = guild_data.get("current_champion_id")

    if not uid:
        await interaction.response.send_message("No champion has been crowned yet.", ephemeral=True)
        return

    member = interaction.guild.get_member(int(uid))
    name = member.display_name if member else f"Unknown ({uid})"
    wins = guild_data.get("all_time_wins", {}).get(uid, 0)

    embed = discord.Embed(
        title="👑 Current Champion",
        description=f"**{name}** is today's champion with **{wins}** all-time win(s).",
        colour=discord.Colour.gold(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Show a member's all-time champion win stats.")
@app_commands.describe(member="The member to look up (leave blank for yourself).")
async def slash_stats(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    wins = guild_data.get("all_time_wins", {}).get(str(target.id), 0)

    all_wins = guild_data.get("all_time_wins", {})
    sorted_wins = sorted(all_wins.items(), key=lambda x: x[1], reverse=True)
    rank = next((i + 1 for i, (uid, _) in enumerate(sorted_wins) if uid == str(target.id)), None)
    rank_str = f"#{rank}" if rank else "Unranked"

    embed = discord.Embed(
        title=f"📈 All-Time Stats — {target.display_name}",
        colour=discord.Colour.blurple(),
    )
    embed.add_field(name="Total Wins", value=str(wins), inline=True)
    embed.add_field(name="Server Rank", value=rank_str, inline=True)
    embed.set_thumbnail(url=target.display_avatar.url)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="alltime", description="Show the all-time champion leaderboard.")
async def slash_alltime(interaction: discord.Interaction):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    all_wins = guild_data.get("all_time_wins", {})

    if not all_wins:
        await interaction.response.send_message("No all-time stats yet.", ephemeral=True)
        return

    sorted_wins = sorted(all_wins.items(), key=lambda x: x[1], reverse=True)[:10]
    lines = []
    for i, (uid, wins) in enumerate(sorted_wins, 1):
        member = interaction.guild.get_member(int(uid))
        name = member.display_name if member else f"Unknown ({uid})"
        medal = ["🥇", "🥈", "🥉"][i - 1] if i <= 3 else f"{i}."
        lines.append(f"{medal} **{name}** — {wins} win(s)")

    embed = discord.Embed(
        title="🏆 All-Time Champion Leaderboard",
        description="\n".join(lines),
        colour=discord.Colour.gold(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="settimezone", description="Set the server timezone for the daily reset. (Admin)")
@app_commands.describe(timezone="IANA timezone name, e.g. America/New_York, Europe/London, US/Pacific, UTC")
@app_commands.default_permissions(administrator=True)
async def slash_settimezone(interaction: discord.Interaction, timezone: str):
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        await interaction.response.send_message(
            f"❌ `{timezone}` is not a valid timezone. Use an IANA name like `America/New_York`, `Europe/London`, `Asia/Tokyo`, or `UTC`.",
            ephemeral=True,
        )
        return

    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["timezone_str"] = timezone
    save_data(data)
    await interaction.response.send_message(
        f"✅ Timezone set to **{timezone}**. Daily reset will now use this timezone. Current reset time: **{format_reset_time(guild_data)}**.",
        ephemeral=True,
    )


@bot.tree.command(name="setresettime", description="Set the time of day for the daily reset. (Admin)")
@app_commands.describe(
    hour="Hour of the reset (0–23) in your configured timezone.",
    minute="Minute of the reset (0–59, default 0).",
)
@app_commands.default_permissions(administrator=True)
async def slash_setresettime(interaction: discord.Interaction, hour: int, minute: int = 0):
    if not (0 <= hour <= 23):
        await interaction.response.send_message("❌ Hour must be between 0 and 23.", ephemeral=True)
        return
    if not (0 <= minute <= 59):
        await interaction.response.send_message("❌ Minute must be between 0 and 59.", ephemeral=True)
        return

    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["reset_hour"] = hour
    guild_data["reset_minute"] = minute
    save_data(data)
    await interaction.response.send_message(
        f"✅ Daily reset time set to **{format_reset_time(guild_data)}**.",
        ephemeral=True,
    )


@bot.tree.command(name="setchampvc", description="Set the voice channel to rename for the daily champion. (Admin)")
@app_commands.describe(channel="The voice channel to rename each day.")
@app_commands.default_permissions(administrator=True)
async def slash_setchampvc(interaction: discord.Interaction, channel: discord.VoiceChannel):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["champion_vc_id"] = str(channel.id)
    save_data(data)
    await interaction.response.send_message(
        f"✅ Champion VC set to **{channel.name}**. It will be renamed daily to the top member's name.",
        ephemeral=True,
    )


@bot.tree.command(name="setchampionrole", description="Set an existing role as the champion role. (Admin)")
@app_commands.describe(role="The role to assign to the daily champion.")
@app_commands.default_permissions(administrator=True)
async def slash_setchampionrole(interaction: discord.Interaction, role: discord.Role):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["champion_role_id"] = str(role.id)
    save_data(data)
    await interaction.response.send_message(
        f"✅ Champion role set to **{role.name}**. This role will be assigned to the daily winner.",
        ephemeral=True,
    )


@bot.tree.command(name="setannouncechannel", description="Set the channel for daily champion announcements. (Admin)")
@app_commands.describe(channel="The text channel to post the daily champion announcement in.")
@app_commands.default_permissions(administrator=True)
async def slash_setannouncechannel(interaction: discord.Interaction, channel: discord.TextChannel):
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


# ===== Entry Point =====
async def main():
    await start_webserver() # Start Railway web server first
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())