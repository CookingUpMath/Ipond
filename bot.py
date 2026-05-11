import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncpg
import os
import random
from datetime import datetime, timedelta
import pytz

# -----------------------------------------
# BOT + INTENTS
# -----------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Slash command tree
tree = bot.tree

# -----------------------------------------
# DATABASE CONNECTION
# -----------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:xdbadtLrYdLWvRUjgnyyjsIGBnjWeyRf@postgres.railway.internal:5432/railway"
)

pool: asyncpg.Pool | None = None

async def init_db():
    global pool
    if pool is not None:
        return

    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:

        # Guild settings
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id BIGINT PRIMARY KEY,
            announce_channel_id BIGINT,
            champion_vc_id BIGINT,
            timezone_str TEXT DEFAULT 'EST',
            reset_hour INT DEFAULT 0,
            reset_minute INT DEFAULT 0,
            current_champion_id BIGINT,
            last_reset_date DATE
        );
        """)

        # Daily message counts
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS message_counts (
            guild_id BIGINT,
            user_id BIGINT,
            count BIGINT DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        # All-time wins
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS all_time_wins (
            guild_id BIGINT,
            user_id BIGINT,
            wins BIGINT DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        # Crown uses
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS crown_uses (
            guild_id BIGINT,
            user_id BIGINT,
            curse_used BIGINT DEFAULT 0,
            mime_used BIGINT DEFAULT 0,
            jester_used BIGINT DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        # Victim stats
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS victim_stats (
            guild_id BIGINT,
            user_id BIGINT,
            cursed BIGINT DEFAULT 0,
            mimed BIGINT DEFAULT 0,
            jestered BIGINT DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        # Active effects
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS active_effects (
            guild_id BIGINT PRIMARY KEY,
            cursed_user BIGINT,
            curse_until TIMESTAMP,
            mimed_user BIGINT,
            mime_until TIMESTAMP,
            jester_user BIGINT,
            jester_until TIMESTAMP
        );
        """)

        # ⭐ Champion role table (NEW)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS crown_settings (
            guild_id BIGINT PRIMARY KEY,
            role_id BIGINT
        );
        """)

    print("Database initialized and tables ensured.")


# -----------------------------------------
# GUILD SETTINGS FETCH
# -----------------------------------------

async def get_guild_settings(guild_id: int):
    global pool
    if pool is None:
        await init_db()

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM guild_settings WHERE guild_id = $1", guild_id)

        if row is None:
            await conn.execute("""
                INSERT INTO guild_settings (guild_id)
                VALUES ($1)
            """, guild_id)

            return {
                "guild_id": guild_id,
                "announce_channel_id": None,
                "champion_vc_id": None,
                "timezone_str": "EST",
                "reset_hour": 0,
                "reset_minute": 0,
                "current_champion_id": None,
                "last_reset_date": None
            }

        return dict(row)


def get_tz(settings: dict):
    try:
        return pytz.timezone(settings["timezone_str"])
    except:
        return pytz.timezone("EST")


# -----------------------------------------
# /forcesync — GLOBAL SYNC
# -----------------------------------------

@tree.command(name="forcesync", description="Force sync slash commands.")
async def forcesync(interaction: discord.Interaction):
    await tree.sync()
    await interaction.response.send_message("Slash commands synced.")


# -----------------------------------------
# BOT READY + SLASH SYNC
# -----------------------------------------

@bot.event
async def on_ready():
    await init_db()

    # Global slash command sync
    await tree.sync()

    # Start daily reset loop if not running
    if not daily_reset_loop.is_running():
        daily_reset_loop.start()

    print(f"Bot is online as {bot.user}")


# -----------------------------------------
# PART 2 — MESSAGE COUNTING + EFFECTS + RESET
# -----------------------------------------

async def increment_message_count(guild_id: int, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO message_counts (guild_id, user_id, count)
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET count = message_counts.count + 1
        """, guild_id, user_id)


async def reset_daily_counts(guild_id: int):
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM message_counts WHERE guild_id = $1", guild_id)


async def get_top_user(guild_id: int):
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT user_id, count
            FROM message_counts
            WHERE guild_id = $1
            ORDER BY count DESC
            LIMIT 1
        """, guild_id)


# -----------------------------------------
# MESSAGE EVENT — COUNT + EFFECTS
# -----------------------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    guild = message.guild
    if guild is None:
        return

    settings = await get_guild_settings(guild.id)

    # Count message
    await increment_message_count(guild.id, message.author.id)

    # Fetch active effects
    async with pool.acquire() as conn:
        effect = await conn.fetchrow("""
            SELECT * FROM active_effects WHERE guild_id = $1
        """, guild.id)

    now_utc = datetime.utcnow()

    if effect:
        cursed_user = effect["cursed_user"]
        curse_until = effect["curse_until"]
        mimed_user = effect["mimed_user"]
        mime_until = effect["mime_until"]
        jester_user = effect["jester_user"]
        jester_until = effect["jester_until"]

        update_needed = False

        if curse_until and curse_until < now_utc:
            cursed_user = None
            curse_until = None
            update_needed = True

        if mime_until and mime_until < now_utc:
            mimed_user = None
            mime_until = None
            update_needed = True

        if jester_until and jester_until < now_utc:
            jester_user = None
            jester_until = None
            update_needed = True

        if update_needed:
            async with pool.acquire() as conn:
                await conn.execute("""
                    UPDATE active_effects
                    SET cursed_user = $2,
                        curse_until = $3,
                        mimed_user = $4,
                        mime_until = $5,
                        jester_user = $6,
                        jester_until = $7
                    WHERE guild_id = $1
                """, guild.id,
                   cursed_user, curse_until,
                   mimed_user, mime_until,
                   jester_user, jester_until)

        # CURSED — 30% chance to annoy
        if cursed_user == message.author.id and curse_until and curse_until >= now_utc:
            if random.random() < 0.30:
                await message.add_reaction("🦆")
                await message.channel.send("quack")

        # MIMED — delete message
        if mimed_user == message.author.id and mime_until and mime_until >= now_utc:
            try:
                await message.delete()
            except:
                pass
            return

        # JESTER — add 🤡 to nickname
        if jester_user == message.author.id and jester_until and jester_until >= now_utc:
            try:
                if "🤡" not in message.author.display_name:
                    await message.author.edit(nick=f"{message.author.display_name} 🤡")
            except:
                pass

    await bot.process_commands(message)

# -----------------------------------------
# Champion Role Helper
# -----------------------------------------

async def apply_champion_role(guild: discord.Guild, new_champion: discord.Member):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role_id FROM crown_settings WHERE guild_id = $1",
            guild.id
        )

    if not row or not row["role_id"]:
        return  # No champion role set

    role = guild.get_role(row["role_id"])
    if not role:
        return

    # Remove role from previous champion(s)
    for member in role.members:
        if member != new_champion:
            try:
                await member.remove_roles(role, reason="New champion crowned")
            except:
                pass

    # Add role to new champion
    if new_champion and role not in new_champion.roles:
        try:
            await new_champion.add_roles(role, reason="Champion crowned")
        except:
            pass
# -----------------------------------------
# DAILY RESET HELPER
# -----------------------------------------

async def perform_reset_for_guild(guild: discord.Guild, settings: dict, now: datetime):
    # Safety: DB must exist
    if pool is None:
        return

    top = await get_top_user(guild.id)

    if top:
        winner_id = top["user_id"]

        # Increment all-time wins
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO all_time_wins (guild_id, user_id, wins)
                VALUES ($1, $2, 1)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET wins = all_time_wins.wins + 1
            """, guild.id, winner_id)

        # Update champion in guild_settings
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE guild_settings
                SET current_champion_id = $1,
                    last_reset_date = $2
                WHERE guild_id = $3
            """, winner_id, now.date(), guild.id)

        # Update nickname with crown if champion VC is set
        if settings["champion_vc_id"]:
            member = guild.get_member(winner_id)
            if member:
                try:
                    if "👑" not in member.display_name:
                        await member.edit(nick=f"{member.display_name} 👑")
                except:
                    pass

        # ⭐ Apply champion role if set
        winner_member = guild.get_member(winner_id)
        if winner_member:
            await apply_champion_role(guild, winner_member)

            # Update bot status to show the new champion
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.playing,
                    name=f"👑 {winner_member.display_name}"
                )
            )

        # Announce winner
        if settings["announce_channel_id"]:
            channel = guild.get_channel(settings["announce_channel_id"])
            if channel:
                reset_hour = settings["reset_hour"]
                reset_minute = settings["reset_minute"]
                embed = discord.Embed(
                    title="",
                    color=discord.Color.gold()
                )
                embed.description = (
                    "# 🏆 Daily Champion\n"
                    f"-# Reset Time: {reset_hour:02d}:{reset_minute:02d} {settings['timezone_str']}\n\n"
                    f"👑 **<@{winner_id}>** is today's champion with **{top['count']} messages!**"
                )
                await channel.send(embed=embed)

    # Reset daily counts
    await reset_daily_counts(guild.id)


# -----------------------------------------
# DAILY RESET LOOP
# -----------------------------------------

@tasks.loop(minutes=1)
async def daily_reset_loop():
    if pool is None:
        return

    for guild in bot.guilds:
        settings = await get_guild_settings(guild.id)
        tz = get_tz(settings)

        now = datetime.now(tz)
        reset_hour = settings["reset_hour"]
        reset_minute = settings["reset_minute"]

        # Time to reset?
        if now.hour == reset_hour and now.minute == reset_minute:
            await perform_reset_for_guild(guild, settings, now)


# -----------------------------------------
# Leaderboard
# -----------------------------------------

@tree.command(name="leaderboard", description="View the daily and all-time leaderboards.")
async def leaderboard(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    # -----------------------------
    # Fetch DAILY leaderboard
    # -----------------------------
    async with pool.acquire() as conn:
        daily_rows = await conn.fetch("""
            SELECT user_id, count
            FROM message_counts
            WHERE guild_id = $1
            ORDER BY count DESC
            LIMIT 10
        """, guild_id)

    daily_lines = []
    for i, row in enumerate(daily_rows, start=1):
        user = interaction.guild.get_member(row["user_id"])
        name = user.display_name if user else f"User {row['user_id']}"
        count = row["count"]

        if i == 1:
            daily_lines.append(f"** 🥇 {name} - {count} **")
        elif i == 2:
            daily_lines.append(f"** 🥈 {name} - {count} **")
        elif i == 3:
            daily_lines.append(f"** 🥉 {name} - {count} **")
        else:
            daily_lines.append(f"▪️ {name} - {count}")

    if not daily_lines:
        daily_lines.append("▪️ No messages today.")

    # -----------------------------
    # Fetch ALL-TIME leaderboard
    # -----------------------------
    async with pool.acquire() as conn:
        all_rows = await conn.fetch("""
            SELECT user_id, wins
            FROM all_time_wins
            WHERE guild_id = $1
            ORDER BY wins DESC
            LIMIT 10
        """, guild_id)

    all_lines = []
    for i, row in enumerate(all_rows, start=1):
        user = interaction.guild.get_member(row["user_id"])
        name = user.display_name if user else f"User {row['user_id']}"
        wins = row["wins"] if row else 0

        if i == 1:
            all_lines.append(f"** 🥇 {name} - {wins} **")
        elif i == 2:
            all_lines.append(f"** 🥈 {name} - {wins} **")
        elif i == 3:
            all_lines.append(f"** 🥉 {name} - {wins} **")
        else:
            all_lines.append(f"▪️ {name} - {wins}")

    if not all_lines:
        all_lines.append("▪️ No champions yet.")

    # -----------------------------
    # Build embed
    # -----------------------------
    embed = discord.Embed(color=discord.Color.gold())
    embed.description = (
        "# 🗓️ Daily Leaderboard\n"
        + "\n".join(daily_lines)
        + "\n\n# 🏆 Overall Leaderboard\n"
        + "\n".join(all_lines)
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# PART 3 — CROWN POWERS (SLASH COMMANDS)
# -----------------------------------------

async def set_active_effect(guild_id: int, effect: str, user_id: int, until: datetime):
    column_user = {
        "curse": "cursed_user",
        "mime": "mimed_user",
        "jester": "jester_user"
    }.get(effect)

    column_until = {
        "curse": "curse_until",
        "mime": "mime_until",
        "jester": "jester_until"
    }.get(effect)

    if not column_user or not column_until:
        return

    async with pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO active_effects (guild_id, {column_user}, {column_until})
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id)
            DO UPDATE SET
                {column_user} = EXCLUDED.{column_user},
                {column_until} = EXCLUDED.{column_until}
        """, guild_id, user_id, until)


async def increment_crown_use(guild_id: int, user_id: int, effect: str):
    column = {
        "curse": "curse_used",
        "mime": "mime_used",
        "jester": "jester_used"
    }.get(effect)

    if not column:
        return

    async with pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO crown_uses (guild_id, user_id, {column})
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET {column} = crown_uses.{column} + 1
        """, guild_id, user_id)


async def increment_victim_stat(guild_id: int, user_id: int, effect: str):
    column = {
        "curse": "cursed",
        "mime": "mimed",
        "jester": "jestered"
    }.get(effect)

    if not column:
        return

    async with pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO victim_stats (guild_id, user_id, {column})
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET {column} = victim_stats.{column} + 1
        """, guild_id, user_id)


# -----------------------------------------
# /curse — UNTIL DAILY RESET
# -----------------------------------------

@tree.command(name="curse", description="Curse a user until the daily reset.")
async def curse(interaction: discord.Interaction, member: discord.Member):

    guild_id = interaction.guild_id
    user_id = interaction.user.id

    # Track usage + victim stats
    await increment_crown_use(guild_id, user_id, "curse")
    await increment_victim_stat(guild_id, member.id, "curse")

    # Curse lasts until reset
    settings = await get_guild_settings(guild_id)
    tz = get_tz(settings)
    now = datetime.now(tz)
    reset_time = now.replace(
        hour=settings["reset_hour"],
        minute=settings["reset_minute"],
        second=0,
        microsecond=0
    )
    if reset_time <= now:
        reset_time += timedelta(days=1)

    until_utc = reset_time.astimezone(pytz.utc).replace(tzinfo=None)
    await set_active_effect(guild_id, "curse", member.id, until_utc)

    embed = discord.Embed(color=discord.Color.red())
    embed.description = (
        "# 🦆 Curse Applied\n"
        "-# Duration: Until daily reset\n"
        "-# Effect: 30% chance to annoy with quacks\n\n"
        f"**{member.mention}** has been **cursed**."
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /mime — 30 MINUTES
# -----------------------------------------

@tree.command(name="mime", description="Silence a user for 30 minutes.")
async def mime(interaction: discord.Interaction, member: discord.Member):

    guild_id = interaction.guild_id
    user_id = interaction.user.id

    await increment_crown_use(guild_id, user_id, "mime")
    await increment_victim_stat(guild_id, member.id, "mime")

    until = datetime.utcnow() + timedelta(minutes=30)
    await set_active_effect(guild_id, "mime", member.id, until)

    embed = discord.Embed(color=discord.Color.dark_gray())
    embed.description = (
        "# 🤐 Mime Applied\n"
        "-# Duration: 30 minutes\n"
        "-# Effect: Deletes all messages they send\n\n"
        f"**{member.mention}** has been **mimed**."
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /jester — UNTIL DAILY RESET
# -----------------------------------------

@tree.command(name="jester", description="Turn a user into a jester until the daily reset.")
async def jester(interaction: discord.Interaction, member: discord.Member):

    guild_id = interaction.guild_id
    user_id = interaction.user.id

    await increment_crown_use(guild_id, user_id, "jester")
    await increment_victim_stat(guild_id, member.id, "jester")

    # Jester lasts until reset
    settings = await get_guild_settings(guild_id)
    tz = get_tz(settings)
    now = datetime.now(tz)
    reset_time = now.replace(
        hour=settings["reset_hour"],
        minute=settings["reset_minute"],
        second=0,
        microsecond=0
    )
    if reset_time <= now:
        reset_time += timedelta(days=1)

    until_utc = reset_time.astimezone(pytz.utc).replace(tzinfo=None)
    await set_active_effect(guild_id, "jester", member.id, until_utc)

    embed = discord.Embed(color=discord.Color.purple())
    embed.description = (
        "# 🤡 Jester Applied\n"
        "-# Duration: Until daily reset\n"
        "-# Effect: Adds 🤡 to their nickname\n\n"
        f"**{member.mention}** has been **jestered**."
    )

    await interaction.response.send_message(embed=embed)
# -----------------------------------------
# PART 4 — ADMIN SETTINGS (SLASH COMMANDS)
# -----------------------------------------

def admin_only():
    async def predicate(interaction: discord.Interaction):
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)

# -----------------------------------------
# /setrole — Set the champion role
# -----------------------------------------

@tree.command(name="setrole", description="Set the champion role for this server.")
@admin_only()
@app_commands.describe(role="The role that will be assigned to the daily champion.")
async def setrole(interaction: discord.Interaction, role: discord.Role):

    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO crown_settings (guild_id, role_id)
            VALUES ($1, $2)
            ON CONFLICT (guild_id)
            DO UPDATE SET role_id = EXCLUDED.role_id
        """, interaction.guild_id, role.id)

    embed = discord.Embed(color=discord.Color.gold())
    embed.description = (
        "# 👑 Champion Role Updated\n"
        f"-# New Role: {role.mention}"
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /setannounce
# -----------------------------------------

@tree.command(name="setannounce", description="Set the channel where champion announcements are posted.")
@admin_only()
async def setannounce(interaction: discord.Interaction, channel: discord.TextChannel):

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET announce_channel_id = $1
            WHERE guild_id = $2
        """, channel.id, interaction.guild_id)

    embed = discord.Embed(color=discord.Color.blurple())
    embed.description = (
        "# 📢 Announce Channel Updated\n"
        f"-# New Channel: {channel.mention}"
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /setchampionvc
# -----------------------------------------

@tree.command(name="setchampionvc", description="Set the VC used for champion nickname styling.")
@admin_only()
async def setchampionvc(interaction: discord.Interaction, channel: discord.VoiceChannel):

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET champion_vc_id = $1
            WHERE guild_id = $2
        """, channel.id, interaction.guild_id)

    embed = discord.Embed(color=discord.Color.gold())
    embed.description = (
        "# 🎧 Champion VC Updated\n"
        f"-# New VC: **{channel.name}**\n"
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /settimezone
# -----------------------------------------

@tree.command(name="settimezone", description="Set the server's timezone.")
@admin_only()
async def settimezone(interaction: discord.Interaction, timezone: str):

    try:
        pytz.timezone(timezone)
    except:
        await interaction.response.send_message("❌ Invalid timezone. Example: `EST`, `UTC`, `America/New_York`")
        return

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET timezone_str = $1
            WHERE guild_id = $2
        """, timezone, interaction.guild_id)

    embed = discord.Embed(color=discord.Color.green())
    embed.description = (
        "# ⏰ Timezone Updated\n"
        f"-# New Timezone: **{timezone}**"
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /setreset
# -----------------------------------------

@tree.command(name="setreset", description="Set the daily reset time (24h format).")
@admin_only()
async def setreset(interaction: discord.Interaction, hour: int, minute: int):

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await interaction.response.send_message("❌ Invalid time. Use 24‑hour format.")
        return

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET reset_hour = $1,
                reset_minute = $2
            WHERE guild_id = $3
        """, hour, minute, interaction.guild_id)

    embed = discord.Embed(color=discord.Color.orange())
    embed.description = (
        "# ⏳ Reset Time Updated\n"
        f"-# New Time: **{hour:02d}:{minute:02d}**"
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /forcereset — Force a manual reset
# -----------------------------------------

@tree.command(name="forcereset", description="Force the daily reset now.")
@admin_only()
async def forcereset(interaction: discord.Interaction):

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command can only be used in a server.")
        return

    settings = await get_guild_settings(guild.id)
    tz = get_tz(settings)
    now = datetime.now(tz)

    await perform_reset_for_guild(guild, settings, now)

    embed = discord.Embed(color=discord.Color.red())
    embed.description = (
        "# 🔁 Manual Reset Triggered\n"
        "-# The daily reset has been forced for this server."
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /settings
# -----------------------------------------

@tree.command(name="settings", description="View all server settings.")
async def settings_cmd(interaction: discord.Interaction):

    settings = await get_guild_settings(interaction.guild_id)

    # Champion role
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT role_id FROM crown_settings
            WHERE guild_id = $1
        """, interaction.guild_id)

    role_id = row["role_id"] if row else None

    announce = f"<#{settings['announce_channel_id']}>" if settings["announce_channel_id"] else "Not set"
    champion_vc = f"<#{settings['champion_vc_id']}>" if settings["champion_vc_id"] else "Not set"
    champion_role = f"<@&{role_id}>" if role_id else "Not set"
    current_champion = f"<@{settings['current_champion_id']}>" if settings["current_champion_id"] else "No champion yet"

    embed = discord.Embed(color=discord.Color.blurple())
    embed.description = (
        "# ⚙️ Server Settings\n"
        f"-# Announce Channel: {announce}\n"
        f"-# Champion VC: {champion_vc}\n"
        f"-# Champion Role: {champion_role}\n"
        f"-# Timezone: **{settings['timezone_str']}**\n"
        f"-# Reset Time: **{settings['reset_hour']:02d}:{settings['reset_minute']:02d}**\n"
        f"-# Current Champion: {current_champion}"
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /stats
# -----------------------------------------

@tree.command(name="stats", description="View your stats or another member's stats.")
async def stats(interaction: discord.Interaction, member: discord.Member | None = None):

    guild_id = interaction.guild_id
    target = member or interaction.user

    # Daily message count
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT count FROM message_counts
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, target.id)
    daily = row["count"] if row else 0

    # All-time wins
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT wins FROM all_time_wins
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, target.id)
    wins = row["wins"] if row else 0

    # Crown uses (how many times THEY used powers)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT curse_used, mime_used, jester_used
            FROM crown_uses
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, target.id)
    curse_used = row["curse_used"] if row else 0
    mime_used = row["mime_used"] if row else 0
    jester_used = row["jester_used"] if row else 0
    total_powers = curse_used + mime_used + jester_used

    # Victim stats (how many times THEY were targeted)
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT cursed, mimed, jestered
            FROM victim_stats
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, target.id)
    cursed = row["cursed"] if row else 0
    mimed = row["mimed"] if row else 0
    jestered = row["jestered"] if row else 0

    embed = discord.Embed(color=discord.Color.blurple())
    embed.description = (
        f"# 📊 {target.display_name}'s Stats\n"
        f"🗓️ Today: **{daily}**\n"
        f"👑 Crowned: **{wins}**\n"
        f"⚡️ Powers Used: **{total_powers}**\n"
        f"-# 🔮 Curses Cast: **{curse_used}**\n"
        f"-# 🙊 Mimes Cast: **{mime_used}**\n"
        f"-# 🤡 Jesters Cast: **{jester_used}**\n\n"
        f"# 🎯 As a Victim\n"
        f"-# 🔮 Cursed: **{cursed}**\n"
        f"-# 🙊 Mimed: **{mimed}**\n"
        f"-# 🤡 Jestered: **{jestered}**"
    )

    embed.set_thumbnail(url=target.display_avatar.url)

    await interaction.response.send_message(embed=embed)



# -----------------------------------------
# GRACEFUL SHUTDOWN
# -----------------------------------------

async def close_db():
    global pool
    if pool:
        await pool.close()
        pool = None


@bot.event
async def on_disconnect():
    await close_db()


# -----------------------------------------
# RUN THE BOT
# -----------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("ERROR: DISCORD_TOKEN environment variable not set.")
else:
    bot.run(TOKEN)
