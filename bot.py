import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncpg
import os
import random
from datetime import datetime, timedelta
import pytz
import emoji

# -----------------------------------------
# BOT + INTENTS
# -----------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
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

        # Champion role settings
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS crown_settings (
            guild_id BIGINT PRIMARY KEY,
            role_id BIGINT
        );
        """)

        # Daily power usage
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_power_usage (
            guild_id BIGINT,
            user_id BIGINT,
            curse_used_today BOOLEAN DEFAULT FALSE,
            mime_used_today BOOLEAN DEFAULT FALSE,
            jester_used_today BOOLEAN DEFAULT FALSE,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

    print("Database initialized and tables ensured.")


async def ensure_db():
    global pool
    if pool is None:
        await init_db()


# -----------------------------------------
# GUILD SETTINGS + HELPERS
# -----------------------------------------

async def get_guild_settings(guild_id: int):
    await ensure_db()
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


def clean_display_name(name: str) -> str:
    return name.replace(" 🤡", "").replace("🤡", "").strip()


# -----------------------------------------
# MESSAGE + STATS HELPERS
# -----------------------------------------

async def increment_message_count(guild_id: int, user_id: int):
    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO message_counts (guild_id, user_id, count)
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET count = message_counts.count + 1
        """, guild_id, user_id)


async def reset_daily_counts(guild_id: int):
    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM message_counts WHERE guild_id = $1", guild_id)


async def get_top_user(guild_id: int):
    await ensure_db()
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT user_id, count
            FROM message_counts
            WHERE guild_id = $1
            ORDER BY count DESC
            LIMIT 1
        """, guild_id)


async def increment_crown_use(guild_id: int, user_id: int, power: str):
    col = {
        "curse": "curse_used",
        "mime": "mime_used",
        "jester": "jester_used"
    }.get(power)
    if not col:
        return

    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO crown_uses (guild_id, user_id, {col})
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET {col} = crown_uses.{col} + 1
        """, guild_id, user_id)


async def increment_victim_stat(guild_id: int, user_id: int, power: str):
    col = {
        "curse": "cursed",
        "mime": "mimed",
        "jester": "jestered"
    }.get(power)
    if not col:
        return

    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO victim_stats (guild_id, user_id, {col})
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET {col} = victim_stats.{col} + 1
        """, guild_id, user_id)


async def set_active_effect(guild_id: int, effect_type: str, user_id: int, until_utc: datetime):
    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT * FROM active_effects WHERE guild_id = $1
        """, guild_id)

        cursed_user = row["cursed_user"] if row else None
        curse_until = row["curse_until"] if row else None
        mimed_user = row["mimed_user"] if row else None
        mime_until = row["mime_until"] if row else None
        jester_user = row["jester_user"] if row else None
        jester_until = row["jester_until"] if row else None

        if effect_type == "curse":
            cursed_user = user_id
            curse_until = until_utc
        elif effect_type == "mime":
            mimed_user = user_id
            mime_until = until_utc
        elif effect_type == "jester":
            jester_user = user_id
            jester_until = until_utc

        await conn.execute("""
            INSERT INTO active_effects (guild_id, cursed_user, curse_until, mimed_user, mime_until, jester_user, jester_until)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (guild_id)
            DO UPDATE SET
                cursed_user = EXCLUDED.cursed_user,
                curse_until = EXCLUDED.curse_until,
                mimed_user = EXCLUDED.mimed_user,
                mime_until = EXCLUDED.mime_until,
                jester_user = EXCLUDED.jester_user,
                jester_until = EXCLUDED.jester_until
        """, guild_id, cursed_user, curse_until, mimed_user, mime_until, jester_user, jester_until)


async def reset_daily_power_usage(guild_id: int):
    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM daily_power_usage WHERE guild_id = $1
        """, guild_id)


# -----------------------------------------
# CHAMPION ROLE
# -----------------------------------------

async def apply_champion_role(guild: discord.Guild, new_champion: discord.Member):
    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role_id FROM crown_settings WHERE guild_id = $1",
            guild.id
        )

    if not row or not row["role_id"]:
        return

    role = guild.get_role(row["role_id"])
    if not role:
        return

    for member in role.members:
        if member != new_champion:
            try:
                await member.remove_roles(role, reason="New champion crowned")
            except:
                pass

    if new_champion and role not in new_champion.roles:
        try:
            await new_champion.add_roles(role, reason="Champion crowned")
        except:
            pass


# -----------------------------------------
# DAILY RESET LOGIC + LOOP
# -----------------------------------------

async def perform_reset_for_guild(guild: discord.Guild, settings: dict, now: datetime):
    await ensure_db()

    top = await get_top_user(guild.id)

    if top:
        winner_id = top["user_id"]

        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO all_time_wins (guild_id, user_id, wins)
                VALUES ($1, $2, 1)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET wins = all_time_wins.wins + 1
            """, guild.id, winner_id)

            await conn.execute("""
                UPDATE guild_settings
                SET current_champion_id = $1,
                    last_reset_date = $2
                WHERE guild_id = $3
            """, winner_id, now.date(), guild.id)

        winner_member = guild.get_member(winner_id)

        if winner_member:
            await apply_champion_role(guild, winner_member)

            display_name = clean_display_name(winner_member.display_name)

            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT role_id FROM crown_settings WHERE guild_id = $1",
                    guild.id
                )

            if row and row["role_id"]:
                role = guild.get_role(row["role_id"])
                if role:
                    try:
                        await role.edit(name=f"👑 {display_name}")
                    except:
                        pass

            if settings["champion_vc_id"]:
                vc = guild.get_channel(settings["champion_vc_id"])
                if isinstance(vc, discord.VoiceChannel):
                    try:
                        await vc.edit(name=f"👑: {display_name}")
                    except:
                        pass

            try:
                await bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.playing,
                        name=f"👑 {display_name}"
                    )
                )
            except:
                pass

        if settings["announce_channel_id"]:
            channel = guild.get_channel(settings["announce_channel_id"])
            if channel:
                reset_hour = settings["reset_hour"]
                reset_minute = settings["reset_minute"]
                embed = discord.Embed(color=discord.Color.gold())
                embed.description = (
                    "# 🏆 Daily Champion\n"
                    f"-# Reset Time: {reset_hour:02d}:{reset_minute:02d} {settings['timezone_str']}\n\n"
                    f"👑 **<@{winner_id}>** is today's champion with **{top['count']} messages!**"
                )
                try:
                    await channel.send(embed=embed)
                except:
                    pass

    await reset_daily_counts(guild.id)
    await reset_daily_power_usage(guild.id)

    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE active_effects
            SET cursed_user = NULL,
                curse_until = NULL,
                mimed_user = NULL,
                mime_until = NULL,
                jester_user = NULL,
                jester_until = NULL
            WHERE guild_id = $1
        """, guild.id)

    for member in guild.members:
        if "🤡" in member.display_name:
            try:
                new_name = clean_display_name(member.display_name)
                if new_name != member.display_name:
                    await member.edit(nick=new_name)
            except:
                pass


@tasks.loop(minutes=1)
async def daily_reset_loop():
    await ensure_db()

    for guild in bot.guilds:
        settings = await get_guild_settings(guild.id)
        tz = get_tz(settings)

        now = datetime.now(tz)
        reset_hour = settings["reset_hour"]
        reset_minute = settings["reset_minute"]

        if now.hour == reset_hour and now.minute == reset_minute:
            await perform_reset_for_guild(guild, settings, now)


# -----------------------------------------
# /forcesync + on_ready
# -----------------------------------------

@tree.command(name="forcesync", description="Force sync slash commands.")
async def forcesync(interaction: discord.Interaction):
    await tree.sync()
    await interaction.response.send_message("Slash commands synced.")


@bot.event
async def on_ready():
    await init_db()
    await tree.sync()

    if not daily_reset_loop.is_running():
        daily_reset_loop.start()

    # Start inactive kick system safely (AFTER DB is ready)
    asyncio.create_task(inactive_member_kick_task())

    print(f"Bot is online as {bot.user}")


# ---------------------------------------------------------
# MERGED INACTIVITY KICK SYSTEM
# (NEW MEMBERS + OLD MEMBERS)
# ---------------------------------------------------------

import asyncio
from datetime import datetime, timezone
from discord.ext import commands

# Track new members only
new_member_joins = {}
new_member_activity = {}
dm_warning_sent = {}

# Default values
DEFAULT_NEW_MINUTES = 24 * 60
DEFAULT_OLD_MINUTES = 14 * 24 * 60  # 14 days
DEFAULT_ENABLED = False


# ---------------------------------------------------------
# DATABASE HELPERS (POOL VERSION)
# ---------------------------------------------------------

async def ensure_kicker_columns(guild_id):
    global pool

    # Add missing columns if needed
    await pool.execute("""
        ALTER TABLE guild_settings
        ADD COLUMN IF NOT EXISTS inactive_minutes_new INT DEFAULT 1440,
        ADD COLUMN IF NOT EXISTS inactive_minutes_old INT DEFAULT 20160,
        ADD COLUMN IF NOT EXISTS inactive_bypass_new BIGINT,
        ADD COLUMN IF NOT EXISTS inactive_bypass_old BIGINT,
        ADD COLUMN IF NOT EXISTS inactive_enabled_new BOOLEAN DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS inactive_enabled_old BOOLEAN DEFAULT FALSE;
    """)

    # Ensure row exists
    await pool.execute("""
        INSERT INTO guild_settings (guild_id)
        VALUES ($1)
        ON CONFLICT (guild_id) DO NOTHING;
    """, guild_id)


async def get_kicker_settings(guild_id):
    global pool
    await ensure_kicker_columns(guild_id)

    row = await pool.fetchrow("""
        SELECT inactive_minutes_new, inactive_minutes_old,
               inactive_bypass_new, inactive_bypass_old,
               inactive_enabled_new, inactive_enabled_old
        FROM guild_settings
        WHERE guild_id = $1
    """, guild_id)

    return {
        "new_minutes": row["inactive_minutes_new"],
        "old_minutes": row["inactive_minutes_old"],
        "bypass_new": row["inactive_bypass_new"],
        "bypass_old": row["inactive_bypass_old"],
        "enabled_new": row["inactive_enabled_new"],
        "enabled_old": row["inactive_enabled_old"],
    }


async def update_kicker_setting(guild_id, column, value):
    global pool
    await pool.execute(
        f"UPDATE guild_settings SET {column} = $1 WHERE guild_id = $2",
        value, guild_id
    )


# ---------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------

@bot.tree.command(name="setnewkicktimer", description="Set inactivity timer for NEW members.")
@app_commands.describe(hours="Hours", minutes="Minutes")
@commands.has_permissions(administrator=True)
async def set_new_kick_timer(interaction, hours: int, minutes: int):
    total = hours * 60 + minutes
    await update_kicker_setting(interaction.guild.id, "inactive_minutes_new", total)
    await interaction.response.send_message(f"New-member kick timer set to {hours}h {minutes}m.")


@bot.tree.command(name="setoldkicktimer", description="Set inactivity timer for OLD members.")
@app_commands.describe(days="Days")
@commands.has_permissions(administrator=True)
async def set_old_kick_timer(interaction, days: int):
    total = days * 24 * 60
    await update_kicker_setting(interaction.guild.id, "inactive_minutes_old", total)
    await interaction.response.send_message(f"Old-member kick timer set to {days} days.")


@bot.tree.command(name="setnewbypass", description="Set bypass role for NEW members.")
@commands.has_permissions(administrator=True)
async def set_new_bypass(interaction, role: discord.Role):
    await update_kicker_setting(interaction.guild.id, "inactive_bypass_new", role.id)
    await interaction.response.send_message(f"New-member bypass role set to {role.name}.")


@bot.tree.command(name="setoldbypass", description="Set bypass role for OLD members.")
@commands.has_permissions(administrator=True)
async def set_old_bypass(interaction, role: discord.Role):
    await update_kicker_setting(interaction.guild.id, "inactive_bypass_old", role.id)
    await interaction.response.send_message(f"Old-member bypass role set to {role.name}.")


@bot.tree.command(name="togglekicker", description="Enable or disable new/old inactivity kick systems.")
@app_commands.describe(system="new or old", state="on or off")
@commands.has_permissions(administrator=True)
async def toggle_kicker(interaction, system: str, state: str):
    system = system.lower()
    state = state.lower()

    if system not in ["new", "old"]:
        return await interaction.response.send_message("System must be 'new' or 'old'.", ephemeral=True)

    if state not in ["on", "off"]:
        return await interaction.response.send_message("State must be 'on' or 'off'.", ephemeral=True)

    column = f"inactive_enabled_{system}"
    await update_kicker_setting(interaction.guild.id, column, state == "on")

    await interaction.response.send_message(
        f"{system.capitalize()} member kicker is now **{state.upper()}**."
    )


# ---------------------------------------------------------
# TRACK NEW MEMBER ACTIVITY
# ---------------------------------------------------------

@bot.event
async def on_member_join(member):
    now = datetime.now(timezone.utc)
    new_member_joins[member.id] = now
    new_member_activity[member.id] = False
    dm_warning_sent[member.id] = False


@bot.event
async def on_message(message):
    if not message.author.bot:
        if message.author.id in new_member_activity:
            new_member_activity[message.author.id] = True
    await bot.process_commands(message)


# ---------------------------------------------------------
# MERGED BACKGROUND LOOP
# ---------------------------------------------------------

async def merged_inactivity_loop():
    await bot.wait_until_ready()

    while True:
        guild = bot.guilds[0]
        now = datetime.now(timezone.utc)

        settings = await get_kicker_settings(guild.id)

        # -------------------------
        # NEW MEMBER CHECK
        # -------------------------
        if settings["enabled_new"]:
            for member_id, join_time in list(new_member_joins.items()):
                member = guild.get_member(member_id)
                if not member:
                    continue

                # Bypass
                if settings["bypass_new"] and discord.utils.get(member.roles, id=settings["bypass_new"]):
                    continue

                # Active → skip
                if new_member_activity.get(member_id):
                    continue

                minutes_since_join = (now - join_time).total_seconds() / 60

                # DM warning
                if minutes_since_join >= settings["new_minutes"] - 20 and not dm_warning_sent.get(member_id):
                    try:
                        await member.send(
                            f"You must send at least one message within {settings['new_minutes']} minutes to stay in {guild.name}."
                        )
                    except:
                        pass
                    dm_warning_sent[member_id] = True

                # Kick
                if minutes_since_join >= settings["new_minutes"]:
                    try:
                        await member.kick(reason="Inactive new member")
                    except:
                        pass

                    new_member_joins.pop(member_id, None)
                    new_member_activity.pop(member_id, None)
                    dm_warning_sent.pop(member_id, None)

        # -------------------------
        # OLD MEMBER CHECK
        # -------------------------
        if settings["enabled_old"]:
            for member in guild.members:
                if member.bot:
                    continue

                # Bypass
                if settings["bypass_old"] and discord.utils.get(member.roles, id=settings["bypass_old"]):
                    continue

                # Last message lookup
                last_msg = await pool.fetchrow("""
                    SELECT last_message
                    FROM message_counts
                    WHERE user_id = $1 AND guild_id = $2
                """, member.id, guild.id)

                if not last_msg:
                    continue

                last_time = last_msg["last_message"]
                minutes_since_msg = (now - last_time).total_seconds() / 60

                if minutes_since_msg >= settings["old_minutes"]:
                    try:
                        await member.kick(reason="Inactive old member")
                    except:
                        pass

        await asyncio.sleep(600)



# -----------------------------------------
# ON MESSAGE — COUNT + EFFECTS
# -----------------------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    guild = message.guild
    if guild is None:
        return

    settings = await get_guild_settings(guild.id)

    await increment_message_count(guild.id, message.author.id)

    await ensure_db()
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
            if mimed_user:
                member = guild.get_member(mimed_user)
                if member:
                    try:
                        await member.send("🙊 You have done well mime. You may now speak!")
                    except:
                        pass
            mimed_user = None
            mime_until = None
            update_needed = True

        if jester_until and jester_until < now_utc:
            if jester_user:
                member = guild.get_member(jester_user)
                if member:
                    try:
                        new_name = clean_display_name(member.display_name)
                        if new_name != member.display_name:
                            await member.edit(nick=new_name)
                    except:
                        pass
            jester_user = None
            jester_until = None
            update_needed = True

        if update_needed:
            await ensure_db()
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

        if cursed_user == message.author.id and curse_until and curse_until >= now_utc:
            if random.random() < 0.20:
                try:
                    await message.add_reaction("🦆")
                except:
                    pass
                try:
                    await message.channel.send("quack")
                except:
                    pass

        if mimed_user == message.author.id and mime_until and mime_until >= now_utc:
            content = message.content

            if content and content.strip():
                stripped = content.strip()

                def is_emoji_char(c: str) -> bool:
                    return c in emoji.EMOJI_DATA

                if not all(is_emoji_char(c) or c.isspace() for c in stripped):
                    try:
                        await message.delete()
                    except:
                        pass
                    return

            await bot.process_commands(message)
            return

        if jester_user == message.author.id and jester_until and jester_until >= now_utc:
            try:
                if "🤡" not in message.author.display_name:
                    await message.author.edit(nick=f"{message.author.display_name} 🤡")
            except:
                pass

    await bot.process_commands(message)


# -----------------------------------------
# LEADERBOARD
# -----------------------------------------

@tree.command(name="leaderboard", description="View the daily and all-time leaderboards.")
async def leaderboard(interaction: discord.Interaction):
    guild_id = interaction.guild_id

    await ensure_db()
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

    await ensure_db()
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

    embed = discord.Embed(color=discord.Color.gold())
    embed.description = (
        "# 🗓️ Daily Leaderboard\n"
        + "\n".join(daily_lines)
        + "\n\n# 🏆 Overall Leaderboard\n"
        + "\n".join(all_lines)
    )

    await interaction.response.send_message(embed=embed)
    
# -----------------------------------------
# /stats — VIEW USER STATS
# -----------------------------------------

@tree.command(name="stats", description="View your stats or another member's stats.")
async def stats(interaction: discord.Interaction, member: discord.Member | None = None):

    guild = interaction.guild
    user = member or interaction.user
    guild_id = guild.id
    user_id = user.id

    await ensure_db()

    # Daily messages
    async with pool.acquire() as conn:
        daily = await conn.fetchrow("""
            SELECT count FROM message_counts
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, user_id)
    daily_count = daily["count"] if daily else 0

    # All-time wins
    async with pool.acquire() as conn:
        wins = await conn.fetchrow("""
            SELECT wins FROM all_time_wins
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, user_id)
    win_count = wins["wins"] if wins else 0

    # Casted stats
    async with pool.acquire() as conn:
        uses = await conn.fetchrow("""
            SELECT curse_used, mime_used, jester_used
            FROM crown_uses
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, user_id)

    curse_casted = uses["curse_used"] if uses else 0
    mime_casted = uses["mime_used"] if uses else 0
    jester_casted = uses["jester_used"] if uses else 0

    # Received stats
    async with pool.acquire() as conn:
        victim = await conn.fetchrow("""
            SELECT cursed, mimed, jestered
            FROM victim_stats
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, user_id)

    cursed_on = victim["cursed"] if victim else 0
    mimed_on = victim["mimed"] if victim else 0
    jestered_on = victim["jestered"] if victim else 0

    total_casted = curse_casted + mime_casted + jester_casted

    # -----------------------------------------
    # COLOR — use user's role color
    # -----------------------------------------

    user_color = user.color if user.color.value != 0 else discord.Color.blurple()

    # -----------------------------------------
    # BUILD EMBED
    # -----------------------------------------

    embed = discord.Embed(color=user_color)
    embed.set_thumbnail(url=user.display_avatar.url)

    embed.description = (
        f"# 🗯️ {user.display_name}'s Stats\n"
        f"🗓️ Messages Today: **{daily_count}**\n"
        f"👑 Crowned: **{win_count}**\n"
        f"⚡ Powers Used: **{total_casted}**\n"
        f"## ⚡️ Royal Powers\n"
        f"-# 🙊 Mime: **{mime_casted} | {mimed_on}**\n"
        f"-# 🤡 Jester: **{jester_casted} | {jestered_on}**\n"
        f"-# 🔮 Curse: **{curse_casted} | {cursed_on}**"
    )

    embed.set_footer(text="⚠️ Stats are Sent | Received")

    await interaction.response.send_message(embed=embed)



# -----------------------------------------
# DAILY POWER USAGE HELPERS
# -----------------------------------------

async def check_power_used(guild_id: int, user_id: int, power: str) -> bool:
    column = {
        "curse": "curse_used_today",
        "mime": "mime_used_today",
        "jester": "jester_used_today"
    }.get(power)

    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(f"""
            SELECT {column} FROM daily_power_usage
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, user_id)

    return row and row[column]


async def mark_power_used(guild_id: int, user_id: int, power: str):
    column = {
        "curse": "curse_used_today",
        "mime": "mime_used_today",
        "jester": "jester_used_today"
    }.get(power)

    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO daily_power_usage (guild_id, user_id, {column})
            VALUES ($1, $2, TRUE)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET {column} = TRUE
        """, guild_id, user_id)


# -----------------------------------------
# CROWN POWER PERMISSION CHECK
# -----------------------------------------

async def ensure_champion_role_holder(interaction: discord.Interaction) -> bool:
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command can only be used in a server.")
        return False

    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role_id FROM crown_settings WHERE guild_id = $1",
            guild.id
        )

    if not row or not row["role_id"]:
        await interaction.response.send_message(
            "❌ No champion role is set. Use `/setrole` first."
        )
        return False

    champion_role = guild.get_role(row["role_id"])
    if not champion_role:
        await interaction.response.send_message(
            "❌ The champion role no longer exists. Set it again with `/setrole`."
        )
        return False

    if champion_role not in interaction.user.roles:
        await interaction.response.send_message(
            "❌ Only the current champion can use crown powers."
        )
        return False

    return True


# -----------------------------------------
# /curse — ONCE PER RESET
# -----------------------------------------

@tree.command(name="curse", description="Curse a user until the daily reset.")
async def curse(interaction: discord.Interaction, member: discord.Member):

    if not await ensure_champion_role_holder(interaction):
        return

    guild_id = interaction.guild_id
    user_id = interaction.user.id

    if await check_power_used(guild_id, user_id, "curse"):
        await interaction.response.send_message("❌ You have already used **Curse** today.")
        return

    await mark_power_used(guild_id, user_id, "curse")
    await increment_crown_use(guild_id, user_id, "curse")
    await increment_victim_stat(guild_id, member.id, "curse")

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
        "-# Effect: 20% chance to annoy with quacks\n\n"
        f"**{member.mention}** has been **cursed**."
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /mime — ONCE PER RESET
# -----------------------------------------

@tree.command(name="mime", description="Silence a user for 30 minutes (emoji-only messages allowed).")
async def mime(interaction: discord.Interaction, member: discord.Member):

    if not await ensure_champion_role_holder(interaction):
        return

    guild_id = interaction.guild_id
    user_id = interaction.user.id

    if await check_power_used(guild_id, user_id, "mime"):
        await interaction.response.send_message("❌ You have already used **Mime** today.")
        return

    await mark_power_used(guild_id, user_id, "mime")
    await increment_crown_use(guild_id, user_id, "mime")
    await increment_victim_stat(guild_id, member.id, "mime")

    until = datetime.utcnow() + timedelta(minutes=30)
    await set_active_effect(guild_id, "mime", member.id, until)

    embed = discord.Embed(color=discord.Color.dark_gray())
    embed.description = (
        "# 🤐 Mime Applied\n"
        "-# Duration: 30 minutes\n"
        "-# Effect: Deletes any message containing text (emoji/stickers/images/GIFs allowed)\n\n"
        f"**{member.mention}** has been **mimed**."
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /jester — ONCE PER RESET
# -----------------------------------------

@tree.command(name="jester", description="Turn a user into a jester until 5 minutes before the daily reset.")
async def jester(interaction: discord.Interaction, member: discord.Member):

    if not await ensure_champion_role_holder(interaction):
        return

    guild_id = interaction.guild_id
    user_id = interaction.user.id

    if await check_power_used(guild_id, user_id, "jester"):
        await interaction.response.send_message("❌ You have already used **Jester** today.")
        return

    await mark_power_used(guild_id, user_id, "jester")
    await increment_crown_use(guild_id, user_id, "jester")
    await increment_victim_stat(guild_id, member.id, "jester")

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

    jester_end_local = reset_time - timedelta(minutes=5)
    until_utc = jester_end_local.astimezone(pytz.utc).replace(tzinfo=None)

    await set_active_effect(guild_id, "jester", member.id, until_utc)

    embed = discord.Embed(color=discord.Color.purple())
    embed.description = (
        "# 🤡 Jester Applied\n"
        "-# Duration: Until 5 minutes before daily reset\n"
        "-# Effect: Adds 🤡 to their nickname while active\n\n"
        f"**{member.mention}** has been **jestered**."
    )

    await interaction.response.send_message(embed=embed)


# -----------------------------------------
# /reseteffects — ADMIN
# -----------------------------------------

def admin_only():
    async def predicate(interaction: discord.Interaction):
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)


@tree.command(name="reseteffects", description="Reset all crown effects or a specific user's effects.")
@admin_only()
async def reseteffects(interaction: discord.Interaction, member: discord.Member | None = None):

    guild = interaction.guild
    settings = await get_guild_settings(guild.id)

    await ensure_db()
    async with pool.acquire() as conn:
        effect = await conn.fetchrow("""
            SELECT * FROM active_effects WHERE guild_id = $1
        """, guild.id)

    # RESET ALL EFFECTS
    if member is None:
        mimed_users = []
        jester_users = []

        if effect:
            if effect["mimed_user"]:
                mimed_users.append(effect["mimed_user"])
            if effect["jester_user"]:
                jester_users.append(effect["jester_user"])

        for uid in mimed_users:
            user = guild.get_member(uid)
            if user:
                try:
                    await user.send("🙊 You have done well mime. You may now speak!")
                except:
                    pass

        for uid in jester_users:
            user = guild.get_member(uid)
            if user:
                try:
                    new_name = clean_display_name(user.display_name)
                    if new_name != user.display_name:
                        await user.edit(nick=new_name)
                except:
                    pass

        await ensure_db()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE active_effects
                SET cursed_user = NULL,
                    curse_until = NULL,
                    mimed_user = NULL,
                    mime_until = NULL,
                    jester_user = NULL,
                    jester_until = NULL
                WHERE guild_id = $1
            """, guild.id)

        await reset_daily_power_usage(guild.id)

        if settings["announce_channel_id"]:
            channel = guild.get_channel(settings["announce_channel_id"])
            if channel:
                try:
                    await channel.send("🔧 All crown effects have been reset by an admin.")
                except:
                    pass

        await interaction.response.send_message("✅ All effects have been reset.")
        return

    # RESET ONE USER'S EFFECTS
    target = member
    if not effect or (
        effect["cursed_user"] != target.id and
        effect["mimed_user"] != target.id and
        effect["jester_user"] != target.id
    ):
        await interaction.response.send_message("❌ That member has no active effects.")
        return

    if effect["mimed_user"] == target.id:
        try:
            await target.send("🙊 You have done well mime. You may now speak!")
        except:
            pass

    if effect["jester_user"] == target.id:
        try:
            new_name = clean_display_name(target.display_name)
            if new_name != target.display_name:
                await target.edit(nick=new_name)
        except:
            pass

    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE active_effects
            SET cursed_user = CASE WHEN cursed_user = $2 THEN NULL ELSE cursed_user END,
                curse_until = CASE WHEN cursed_user = $2 THEN NULL ELSE curse_until END,
                mimed_user = CASE WHEN mimed_user = $2 THEN NULL ELSE mimed_user END,
                mime_until = CASE WHEN mimed_user = $2 THEN NULL ELSE mime_until END,
                jester_user = CASE WHEN jester_user = $2 THEN NULL ELSE jester_user END,
                jester_until = CASE WHEN jester_user = $2 THEN NULL ELSE jester_until END
            WHERE guild_id = $1
        """, guild.id, target.id)

    if settings["announce_channel_id"]:
        channel = guild.get_channel(settings["announce_channel_id"])
        if channel:
            try:
                await channel.send(f"🔧 Crown effects have been reset for {target.mention} by an admin.")
            except:
                pass

    await interaction.response.send_message(f"✅ All effects cleared for {target.mention}.")
# -----------------------------------------
# ADMIN COMMANDS
# -----------------------------------------

@tree.command(name="setrole", description="Set the champion role for this server.")
@admin_only()
@app_commands.describe(role="The role that will be assigned to the daily champion.")
async def setrole(interaction: discord.Interaction, role: discord.Role):

    await ensure_db()
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


@tree.command(name="setannounce", description="Set the channel where champion announcements are posted.")
@admin_only()
async def setannounce(interaction: discord.Interaction, channel: discord.TextChannel):

    await ensure_db()
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


@tree.command(name="setchampionvc", description="Set the VC used for champion nickname styling.")
@admin_only()
async def setchampionvc(interaction: discord.Interaction, channel: discord.VoiceChannel):

    await ensure_db()
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


@tree.command(name="settimezone", description="Set the server's timezone.")
@admin_only()
async def settimezone(interaction: discord.Interaction, timezone: str):

    try:
        pytz.timezone(timezone)
    except:
        await interaction.response.send_message("❌ Invalid timezone. Example: `EST`, `UTC`, `America/New_York`")
        return

    await ensure_db()
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


@tree.command(name="setreset", description="Set the daily reset time (24h format).")
@admin_only()
async def setreset(interaction: discord.Interaction, hour: int, minute: int):

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await interaction.response.send_message("❌ Invalid time. Use 24‑hour format.")
        return

    await ensure_db()
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


@tree.command(name="setchampion", description="Manually set the current champion and award them a win.")
@admin_only()
async def setchampion(interaction: discord.Interaction, member: discord.Member):

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message("❌ This command can only be used in a server.")
        return

    settings = await get_guild_settings(guild.id)
    tz = get_tz(settings)
    now = datetime.now(tz)

    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO all_time_wins (guild_id, user_id, wins)
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET wins = all_time_wins.wins + 1
        """, guild.id, member.id)

        await conn.execute("""
            UPDATE guild_settings
            SET current_champion_id = $1,
                last_reset_date = $2
            WHERE guild_id = $3
        """, member.id, now.date(), guild.id)

    await apply_champion_role(guild, member)

    display_name = clean_display_name(member.display_name)

    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role_id FROM crown_settings WHERE guild_id = $1",
            guild.id
        )

    if row and row["role_id"]:
        role = guild.get_role(row["role_id"])
        if role:
            try:
                await role.edit(name=f"👑 {display_name}")
            except:
                pass

    if settings["champion_vc_id"]:
        vc = guild.get_channel(settings["champion_vc_id"])
        if isinstance(vc, discord.VoiceChannel):
            try:
                await vc.edit(name=f"👑: {display_name}")
            except:
                pass

    try:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name=f"👑 {display_name}"
            )
        )
    except:
        pass

    embed = discord.Embed(color=discord.Color.gold())
    embed.description = (
        "# 👑 Champion Updated\n"
        f"-# New Champion: {member.mention}"
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="settings", description="View the server's crown system settings.")
async def settings_cmd(interaction: discord.Interaction):

    settings = await get_guild_settings(interaction.guild_id)

    announce = (
        f"<#{settings['announce_channel_id']}>" if settings["announce_channel_id"] else "Not set"
    )
    vc = (
        f"<#{settings['champion_vc_id']}>" if settings["champion_vc_id"] else "Not set"
    )
    champion = (
        f"<@{settings['current_champion_id']}>" if settings["current_champion_id"] else "None"
    )

    embed = discord.Embed(color=discord.Color.blue())
    embed.description = (
        "# ⚙️ Server Settings\n"
        f"-# Announce Channel: {announce}\n"
        f"-# Champion VC: {vc}\n"
        f"-# Timezone: **{settings['timezone_str']}**\n"
        f"-# Reset Time: **{settings['reset_hour']:02d}:{settings['reset_minute']:02d}**\n"
        f"-# Current Champion: {champion}"
    )

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
