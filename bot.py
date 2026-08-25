import os
import random
import asyncio
import logging
import re
import io
import textwrap
from datetime import datetime, timedelta, timezone

import asyncpg
import discord
from discord import app_commands
from discord.ext import commands, tasks
import pytz
import emoji
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

# -----------------------------------------
# LOGGING
# -----------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("crown-bot")

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

# Require the env var — never hardcode credentials in source.
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL environment variable is required. "
        "Do not hardcode credentials in the source code."
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
            sticky_channel_id BIGINT,
            timezone_str TEXT DEFAULT 'EST',
            reset_hour INT DEFAULT 0,
            reset_minute INT DEFAULT 0,
            current_champion_id BIGINT,
            last_reset_date DATE,
            speak_minutes INT DEFAULT 20160,
            speak_enabled BOOLEAN DEFAULT FALSE,
            kicker_bypass_role BIGINT
        );
        """)

        # Ensure sticky_channel_id exists on older databases
        await conn.execute("""
        ALTER TABLE guild_settings
        ADD COLUMN IF NOT EXISTS sticky_channel_id BIGINT;
        """)

        # Daily message counts (now with last_message)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS message_counts (
            guild_id BIGINT,
            user_id BIGINT,
            count BIGINT DEFAULT 0,
            last_message TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

        # Ensure last_message exists even if table pre-existed
        await conn.execute("""
        ALTER TABLE message_counts
        ADD COLUMN IF NOT EXISTS last_message TIMESTAMP;
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

        # D_ZLove total tracker
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS d_zlove_total (
            id INT PRIMARY KEY,
            total BIGINT DEFAULT 0
        );
        """)

        # Ensure a single row exists
        await conn.execute("""
        INSERT INTO d_zlove_total (id, total)
        VALUES (1, 0)
        ON CONFLICT (id) DO NOTHING;
        """)

        # Sticky note balances (earned by winning the crown)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS sticky_balances (
            guild_id BIGINT,
            user_id BIGINT,
            balance INT DEFAULT 0,
            PRIMARY KEY (guild_id, user_id)
        );
        """)

    log.info("Database initialized and tables ensured.")


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
                "sticky_channel_id": None,
                "timezone_str": "EST",
                "reset_hour": 0,
                "reset_minute": 0,
                "current_champion_id": None,
                "last_reset_date": None,
                "speak_minutes": 20160,
                "speak_enabled": False,
                "kicker_bypass_role": None
            }

        return dict(row)


def get_tz(settings: dict):
    try:
        return pytz.timezone(settings["timezone_str"])
    except Exception:
        return pytz.timezone("EST")


def clean_display_name(name: str) -> str:
    return name.replace(" 🤡", "").replace("🤡", "").strip()


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(timezone.utc)


def to_naive_utc(dt: datetime) -> datetime:
    """Convert aware datetime to naive UTC for storage (asyncpg TIMESTAMP)."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


# -----------------------------------------
# MESSAGE + STATS HELPERS
# -----------------------------------------

async def increment_message_count(guild_id: int, user_id: int):
    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO message_counts (guild_id, user_id, count, last_message)
            VALUES ($1, $2, 1, NOW())
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET
                count = message_counts.count + 1,
                last_message = NOW()
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


# -----------------------------------------
# STICKY BALANCE HELPERS
# -----------------------------------------

STICKY_MAX_BALANCE = 100
STICKY_REWARD_ON_CROWN = 2


async def get_sticky_balance(guild_id: int, user_id: int) -> int:
    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT balance FROM sticky_balances
            WHERE guild_id = $1 AND user_id = $2
        """, guild_id, user_id)
    return row["balance"] if row else 0


async def add_stickies(guild_id: int, user_id: int, amount: int) -> int:
    """Add stickies, capped at STICKY_MAX_BALANCE. Returns new balance."""
    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO sticky_balances (guild_id, user_id, balance)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET balance = LEAST(sticky_balances.balance + $3, $4)
            RETURNING balance
        """, guild_id, user_id, amount, STICKY_MAX_BALANCE)
    return row["balance"]


async def spend_sticky(guild_id: int, user_id: int) -> bool:
    """Spend 1 sticky if available. Returns True on success."""
    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE sticky_balances
            SET balance = balance - 1
            WHERE guild_id = $1 AND user_id = $2 AND balance > 0
            RETURNING balance
        """, guild_id, user_id)
    return row is not None


def format_sticky_count(n: int) -> str:
    """Display 💯 when at max, otherwise the number."""
    if n >= STICKY_MAX_BALANCE:
        return "💯"
    return str(n)


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

        naive_until = to_naive_utc(until_utc)

        if effect_type == "curse":
            cursed_user = user_id
            curse_until = naive_until
        elif effect_type == "mime":
            mimed_user = user_id
            mime_until = naive_until
        elif effect_type == "jester":
            jester_user = user_id
            jester_until = naive_until

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
            except Exception as e:
                log.warning("Failed to remove champion role from %s: %s", member.id, e)

    if new_champion and role not in new_champion.roles:
        try:
            await new_champion.add_roles(role, reason="Champion crowned")
        except Exception as e:
            log.warning("Failed to add champion role to %s: %s", new_champion.id, e)


# -----------------------------------------
# DAILY RESET LOGIC + LOOP
# -----------------------------------------

async def perform_reset_for_guild(guild: discord.Guild, settings: dict, now: datetime):
    await ensure_db()

    # Mark today as handled unconditionally — otherwise a day with zero
    # messages would leave last_reset_date unchanged, and the catch-up
    # check in daily_reset_loop would re-trigger this function every
    # single minute for the rest of that day.
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET last_reset_date = $1
            WHERE guild_id = $2
        """, now.date(), guild.id)

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
                SET current_champion_id = $1
                WHERE guild_id = $2
            """, winner_id, guild.id)

        # Award sticky notes for winning the crown
        await add_stickies(guild.id, winner_id, STICKY_REWARD_ON_CROWN)

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
                    except Exception as e:
                        log.warning("Failed to rename champion role: %s", e)

            if settings["champion_vc_id"]:
                vc = guild.get_channel(settings["champion_vc_id"])
                if isinstance(vc, discord.VoiceChannel):
                    try:
                        await vc.edit(name=f"👑: {display_name}")
                    except Exception as e:
                        log.warning("Failed to rename champion VC: %s", e)

            try:
                await bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.playing,
                        name=f"👑 {display_name}"
                    )
                )
            except Exception as e:
                log.warning("Failed to update presence: %s", e)

        if settings["announce_channel_id"]:
            channel = guild.get_channel(settings["announce_channel_id"])
            if channel:
                reset_hour = settings["reset_hour"]
                reset_minute = settings["reset_minute"]
                embed = discord.Embed(color=discord.Color.gold())
                embed.description = (
                    "# 🏆 Daily Champion\n"
                    f"-# Reset Time: {reset_hour:02d}:{reset_minute:02d} {settings['timezone_str']}\n\n"
                    f"👑 **<@{winner_id}>** is today's champion with **{top['count']} messages!**\n"
                    f"-# 📝 +{STICKY_REWARD_ON_CROWN} stickies awarded"
                )
                try:
                    await channel.send(embed=embed)
                except Exception as e:
                    log.warning("Failed to send champion announcement: %s", e)

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
            except Exception as e:
                log.warning("Failed to clean jester nick for %s: %s", member.id, e)


@tasks.loop(minutes=1)
async def daily_reset_loop():
    await ensure_db()

    for guild in bot.guilds:
        try:
            settings = await get_guild_settings(guild.id)
            tz = get_tz(settings)

            now = datetime.now(tz)
            reset_hour = settings["reset_hour"]
            reset_minute = settings["reset_minute"]
            today = now.date()

            already_reset_today = settings["last_reset_date"] == today
            past_reset_time = (now.hour, now.minute) >= (reset_hour, reset_minute)

            if past_reset_time and not already_reset_today:
                await perform_reset_for_guild(guild, settings, now)
        except Exception as e:
            # Never let one guild's failure take down the loop for every other guild.
            log.error("[daily_reset_loop] error processing guild %s: %s", guild.id, e)
            continue


@daily_reset_loop.error
async def daily_reset_loop_error(error):
    log.error("[daily_reset_loop] loop crashed, restarting: %s", error)
    if not daily_reset_loop.is_running():
        daily_reset_loop.start()


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

    # Sticky balance
    sticky_count = await get_sticky_balance(guild_id, user_id)
    sticky_display = format_sticky_count(sticky_count)

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

    user_color = user.color if user.color.value != 0 else discord.Color.blurple()

    embed = discord.Embed(color=user_color)
    embed.set_thumbnail(url=user.display_avatar.url)

    embed.description = (
        f"# 🗯️ {user.display_name}'s Stats\n"
        f"📝 Stickies: **{sticky_display}**\n"
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
            "❌ No champion role is set. Use `/settings` to set one first."
        )
        return False

    champion_role = guild.get_role(row["role_id"])
    if not champion_role:
        await interaction.response.send_message(
            "❌ The champion role no longer exists. Set it again with `/settings`."
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

    until_utc = reset_time.astimezone(timezone.utc)
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

    until = utcnow() + timedelta(minutes=30)
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
    until_utc = jester_end_local.astimezone(timezone.utc)

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
                except Exception:
                    pass

        for uid in jester_users:
            user = guild.get_member(uid)
            if user:
                try:
                    new_name = clean_display_name(user.display_name)
                    if new_name != user.display_name:
                        await user.edit(nick=new_name)
                except Exception as e:
                    log.warning("Failed to clean jester nick on reset: %s", e)

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
                except Exception as e:
                    log.warning("Failed to send effects-reset announcement: %s", e)

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
        except Exception:
            pass

    if effect["jester_user"] == target.id:
        try:
            new_name = clean_display_name(target.display_name)
            if new_name != target.display_name:
                await target.edit(nick=new_name)
        except Exception as e:
            log.warning("Failed to clean jester nick for single reset: %s", e)

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
            except Exception as e:
                log.warning("Failed to send single-user effects-reset announcement: %s", e)

    await interaction.response.send_message(f"✅ All effects cleared for {target.mention}.")


# -----------------------------------------
# ADMIN COMMANDS
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

    # Award sticky notes for being set as champion
    await add_stickies(guild.id, member.id, STICKY_REWARD_ON_CROWN)

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
            except Exception as e:
                log.warning("Failed to rename champion role in setchampion: %s", e)

    if settings["champion_vc_id"]:
        vc = guild.get_channel(settings["champion_vc_id"])
        if isinstance(vc, discord.VoiceChannel):
            try:
                await vc.edit(name=f"👑: {display_name}")
            except Exception as e:
                log.warning("Failed to rename champion VC in setchampion: %s", e)

    try:
        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.playing,
                name=f"👑 {display_name}"
            )
        )
    except Exception as e:
        log.warning("Failed to update presence in setchampion: %s", e)

    embed = discord.Embed(color=discord.Color.gold())
    embed.description = (
        "# 👑 Champion Updated\n"
        f"-# New Champion: {member.mention}"
    )

    await interaction.response.send_message(embed=embed)


async def get_champion_role_id(guild_id: int):
    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT role_id FROM crown_settings WHERE guild_id = $1", guild_id
        )
    return row["role_id"] if row else None


async def build_settings_embed(guild_id: int) -> discord.Embed:
    settings = await get_guild_settings(guild_id)
    role_id = await get_champion_role_id(guild_id)

    role = f"<@&{role_id}>" if role_id else "Not set"
    announce = f"<#{settings['announce_channel_id']}>" if settings["announce_channel_id"] else "Not set"
    vc = f"<#{settings['champion_vc_id']}>" if settings["champion_vc_id"] else "Not set"
    sticky = f"<#{settings['sticky_channel_id']}>" if settings.get("sticky_channel_id") else "Not set"
    champion = f"<@{settings['current_champion_id']}>" if settings["current_champion_id"] else "None"

    embed = discord.Embed(color=discord.Color.blue())
    embed.description = (
        "# ⚙️ Server Settings\n"
        f"-# Champion Role: {role}\n"
        f"-# Announce Channel: {announce}\n"
        f"-# Champion VC: {vc}\n"
        f"-# Sticky Notes Channel: {sticky}\n"
        f"-# Timezone: **{settings['timezone_str']}**\n"
        f"-# Reset Time: **{settings['reset_hour']:02d}:{settings['reset_minute']:02d}**\n"
        f"-# Current Champion: {champion}\n\n"
        f"## 🛡️ Kicker System\n"
        f"-# 24h New-Account Barrier: **Always Enabled**"
    )
    embed.set_footer(text="Pick an option below to update a setting.")
    return embed


class TimezoneModal(discord.ui.Modal, title="Edit Timezone"):
    timezone_input = discord.ui.TextInput(
        label="Timezone", placeholder="EST, UTC, America/New_York", max_length=50
    )

    def __init__(self, guild_id: int, parent_view: "SettingsView"):
        super().__init__()
        self.guild_id = guild_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        tz_str = str(self.timezone_input).strip()
        try:
            pytz.timezone(tz_str)
        except Exception:
            return await interaction.response.send_message(
                "❌ Invalid timezone. Example: `EST`, `UTC`, `America/New_York`", ephemeral=True
            )

        await ensure_db()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_settings SET timezone_str = $1 WHERE guild_id = $2",
                tz_str, self.guild_id
            )

        embed = await build_settings_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class ResetTimeModal(discord.ui.Modal, title="Edit Daily Reset Time"):
    hour_input = discord.ui.TextInput(label="Hour (0-23)", max_length=2)
    minute_input = discord.ui.TextInput(label="Minute (0-59)", max_length=2)

    def __init__(self, guild_id: int, parent_view: "SettingsView"):
        super().__init__()
        self.guild_id = guild_id
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            hour = int(str(self.hour_input))
            minute = int(str(self.minute_input))
        except ValueError:
            return await interaction.response.send_message(
                "❌ Hour and minute must be numbers.", ephemeral=True
            )

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return await interaction.response.send_message(
                "❌ Invalid time. Use 24-hour format.", ephemeral=True
            )

        await ensure_db()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE guild_settings
                SET reset_hour = $1, reset_minute = $2
                WHERE guild_id = $3
            """, hour, minute, self.guild_id)

        embed = await build_settings_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self.parent_view)


class SettingsView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = guild_id

        self.role_select = discord.ui.RoleSelect(
            placeholder="👑 Set Champion Role", min_values=1, max_values=1
        )
        self.role_select.callback = self.on_role_select
        self.add_item(self.role_select)

        self.announce_select = discord.ui.ChannelSelect(
            placeholder="📢 Set Announce Channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
        )
        self.announce_select.callback = self.on_announce_select
        self.add_item(self.announce_select)

        self.vc_select = discord.ui.ChannelSelect(
            placeholder="🎧 Set Champion VC",
            channel_types=[discord.ChannelType.voice],
            min_values=1, max_values=1,
        )
        self.vc_select.callback = self.on_vc_select
        self.add_item(self.vc_select)

        self.sticky_select = discord.ui.ChannelSelect(
            placeholder="📝 Set Sticky Notes Channel",
            channel_types=[discord.ChannelType.text],
            min_values=1, max_values=1,
        )
        self.sticky_select.callback = self.on_sticky_select
        self.add_item(self.sticky_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ You do not have permission to change these settings.", ephemeral=True
            )
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction):
        embed = await build_settings_embed(self.guild_id)
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_role_select(self, interaction: discord.Interaction):
        role = self.role_select.values[0]
        await ensure_db()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO crown_settings (guild_id, role_id)
                VALUES ($1, $2)
                ON CONFLICT (guild_id) DO UPDATE SET role_id = EXCLUDED.role_id
            """, self.guild_id, role.id)
        await self._refresh(interaction)

    async def on_announce_select(self, interaction: discord.Interaction):
        channel = self.announce_select.values[0]
        await ensure_db()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_settings SET announce_channel_id = $1 WHERE guild_id = $2",
                channel.id, self.guild_id
            )
        await self._refresh(interaction)

    async def on_vc_select(self, interaction: discord.Interaction):
        channel = self.vc_select.values[0]
        await ensure_db()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_settings SET champion_vc_id = $1 WHERE guild_id = $2",
                channel.id, self.guild_id
            )
        await self._refresh(interaction)

    async def on_sticky_select(self, interaction: discord.Interaction):
        channel = self.sticky_select.values[0]
        await ensure_db()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE guild_settings SET sticky_channel_id = $1 WHERE guild_id = $2",
                channel.id, self.guild_id
            )
        await self._refresh(interaction)

    @discord.ui.button(label="Edit Timezone", style=discord.ButtonStyle.secondary, emoji="⏰", row=4)
    async def edit_timezone_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimezoneModal(self.guild_id, self))

    @discord.ui.button(label="Edit Reset Time", style=discord.ButtonStyle.secondary, emoji="⏳", row=4)
    async def edit_reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResetTimeModal(self.guild_id, self))


@tree.command(name="settings", description="View and edit the server's crown and kicker settings.")
@admin_only()
async def settings_cmd(interaction: discord.Interaction):
    embed = await build_settings_embed(interaction.guild_id)
    view = SettingsView(interaction.guild_id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ---------------------------------------------------------
# 24H ACCOUNT-AGE KICKER
# ---------------------------------------------------------

ACCOUNT_AGE_LIMIT_MINUTES = 24 * 60  # 24 hours

# Optional: override the rejoin invite via env var (recommended)
KICKER_REJOIN_INVITE = os.getenv("KICKER_REJOIN_INVITE", "https://discord.gg/BqYVrX8rPK")


# ---------------------------------------------------------
# MEMBER JOIN HANDLER
# ---------------------------------------------------------

@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return

    # -----------------------------------------------------
    # STRICT 24H AUTO-KICK (DM → Kick instantly)
    # -----------------------------------------------------
    account_age_minutes = (discord.utils.utcnow() - member.created_at).total_seconds() / 60

    if account_age_minutes < ACCOUNT_AGE_LIMIT_MINUTES:
        # DM first (failure does NOT block kick)
        try:
            await member.send(
                f"Hey! Your Discord account is too new to join **{member.guild.name}**.\n"
                f"Please wait until your account is at least **24 hours old**, then rejoin using this link:\n"
                f"{KICKER_REJOIN_INVITE}"
            )
        except Exception:
            pass

        # Kick instantly
        try:
            await member.kick(reason="Account under 24 hours old (auto-kick)")
        except Exception as e:
            log.warning("Failed to kick new account %s: %s", member.id, e)
        return


# -----------------------------------------
# ON MESSAGE — COUNT + EFFECTS
# -----------------------------------------

# Regex for Discord custom emojis (static + animated)
CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def is_emoji_only(text: str) -> bool:
    """
    Return True if the message contains only Unicode emoji, custom Discord
    emojis, and whitespace. Everything else (letters, numbers, punctuation
    outside of emoji syntax) is considered text and should be deleted under mime.
    """
    if not text or not text.strip():
        return True

    # Remove custom emoji syntax first
    cleaned = CUSTOM_EMOJI_RE.sub("", text)

    for c in cleaned:
        if c.isspace():
            continue
        if c in emoji.EMOJI_DATA:
            continue
        # Anything left is considered real text
        return False
    return True


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    guild = message.guild
    if guild is None:
        return

    await ensure_db()

    await increment_message_count(guild.id, message.author.id)

    async with pool.acquire() as conn:
        effect = await conn.fetchrow("""
            SELECT * FROM active_effects WHERE guild_id = $1
        """, guild.id)

    now_utc = utcnow().replace(tzinfo=None)  # naive for comparison with stored TIMESTAMP

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
                    except Exception:
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
                    except Exception as e:
                        log.warning("Failed to clean expired jester nick: %s", e)
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

        # Curse effect
        if cursed_user == message.author.id and curse_until and curse_until >= now_utc:
            if random.random() < 0.20:
                try:
                    await message.add_reaction("🦆")
                except Exception:
                    pass
                try:
                    await message.channel.send("quack")
                except Exception:
                    pass

        # Mime effect
        if mimed_user == message.author.id and mime_until and mime_until >= now_utc:
            content = message.content or ""

            if content.strip() and not is_emoji_only(content):
                try:
                    await message.delete()
                except Exception as e:
                    log.warning("Failed to delete mimed message: %s", e)
                return  # do not process commands on deleted messages

        # Jester effect
        if jester_user == message.author.id and jester_until and jester_until >= now_utc:
            try:
                if "🤡" not in message.author.display_name:
                    await message.author.edit(nick=f"{message.author.display_name} 🤡")
            except Exception as e:
                log.warning("Failed to apply jester nick: %s", e)

    await bot.process_commands(message)


# ------------------------------------------------------------
# ❤️ D_ZLOVE REACTION TRACKER
# ------------------------------------------------------------

# Prefer environment variables so the bot is not tied to one server.
TARGET_CHANNEL_ID = int(os.getenv("LOVE_CHANNEL_ID", "1500998760875167744"))
TARGET_EMOJI_NAME = os.getenv("LOVE_EMOJI_NAME", "D_ZLove")
TARGET_EMOJI_ID = int(os.getenv("LOVE_EMOJI_ID", "1295255068483784786"))


async def get_love_total():
    await ensure_db()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT total FROM d_zlove_total WHERE id = 1;")
        return row["total"] if row else 0


async def set_love_total(value: int):
    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE d_zlove_total
            SET total = $1
            WHERE id = 1;
        """, value)


@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return

    emoji_obj = reaction.emoji
    if (
        getattr(emoji_obj, "name", None) != TARGET_EMOJI_NAME
        and getattr(emoji_obj, "id", None) != TARGET_EMOJI_ID
    ):
        return

    total = await get_love_total()
    await set_love_total(total + 1)


@bot.event
async def on_reaction_remove(reaction, user):
    if user.bot:
        return

    emoji_obj = reaction.emoji
    if (
        getattr(emoji_obj, "name", None) != TARGET_EMOJI_NAME
        and getattr(emoji_obj, "id", None) != TARGET_EMOJI_ID
    ):
        return

    total = await get_love_total()
    await set_love_total(max(0, total - 1))


async def update_love_channel():
    global pool
    await bot.wait_until_ready()

    while pool is None:
        log.info("Waiting for database pool for love tracker...")
        await asyncio.sleep(1)

    channel = bot.get_channel(TARGET_CHANNEL_ID)

    while True:
        total = await get_love_total()
        if channel:
            try:
                await channel.edit(name=f"❤️: {total}")
            except Exception as e:
                log.warning("Failed to update love channel name: %s", e)
        await asyncio.sleep(300)  # 5 minutes


# -----------------------------------------
# /forcesync + on_ready
# -----------------------------------------

@tree.command(name="forcesync", description="Force sync slash commands.")
async def forcesync(interaction: discord.Interaction):
    await tree.sync()
    await interaction.response.send_message("Slash commands synced.")


async def restore_champion_presence():
    """Re-apply the current champion's status on startup — otherwise a
    restart/redeploy leaves the bot blank until the next daily reset,
    which could be up to 24 hours away.
    """
    await ensure_db()
    for guild in bot.guilds:
        settings = await get_guild_settings(guild.id)
        champion_id = settings["current_champion_id"]
        if not champion_id:
            continue

        member = guild.get_member(champion_id)
        if not member:
            continue

        display_name = clean_display_name(member.display_name)
        try:
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.playing,
                    name=f"👑 {display_name}"
                )
            )
        except Exception as e:
            log.warning("Failed to restore champion presence: %s", e)
        return  # presence is bot-wide, not per-guild — first champion found wins


@bot.event
async def on_ready():
    await init_db()
    await tree.sync()

    if not daily_reset_loop.is_running():
        daily_reset_loop.start()

    asyncio.create_task(update_love_channel())
    await restore_champion_presence()

    log.info("Bot is online as %s", bot.user)


# -----------------------------------------
# STICKY NOTES
# -----------------------------------------

STICKY_MAX_CHARS = 120
STICKY_MIN_CHARS = 4

# Very basic blocklist — expand as needed
STICKY_BLOCKED = {
    "discord.gg", "http://", "https://", "@everyone", "@here",
    "nigger", "faggot", "retard",  # hard blocked slurs
}


# Handwriting fonts bundled with the bot (works on Railway / any host)
# Paths are relative to this file's directory.
_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
STICKY_FONTS = [
    (os.path.join(_FONTS_DIR, "PatrickHand-Regular.ttf"), 28),
    (os.path.join(_FONTS_DIR, "Caveat-Variable.ttf"), 32),
    (os.path.join(_FONTS_DIR, "Handlee-Regular.ttf"), 28),
    (os.path.join(_FONTS_DIR, "Kalam-Regular.ttf"), 26),
    (os.path.join(_FONTS_DIR, "GochiHand-Regular.ttf"), 28),
    (os.path.join(_FONTS_DIR, "ShadowsIntoLight-Regular.ttf"), 30),
    (os.path.join(_FONTS_DIR, "GloriaHallelujah-Regular.ttf"), 26),
    (os.path.join(_FONTS_DIR, "HomemadeApple-Regular.ttf"), 24),
    (os.path.join(_FONTS_DIR, "AmaticSC-Regular.ttf"), 34),
    (os.path.join(_FONTS_DIR, "PermanentMarker-Regular.ttf"), 24),
]
STICKY_FONT_FALLBACK = os.path.join(_FONTS_DIR, "PatrickHand-Regular.ttf")



# Discord custom emoji: <:name:id> or <a:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def _sticky_is_clean(text: str) -> tuple[bool, str]:
    """Basic moderation check. Returns (ok, reason)."""
    stripped = text.strip()
    if not stripped:
        return False, "Note cannot be empty."
    if len(stripped) < STICKY_MIN_CHARS:
        return False, f"Note is too short (min {STICKY_MIN_CHARS} characters)."
    if len(text) > STICKY_MAX_CHARS:
        return False, f"Note is too long (max {STICKY_MAX_CHARS} characters)."
    if _CUSTOM_EMOJI_RE.search(stripped):
        return False, "Custom emojis can’t be used on stickies — try normal text instead."
    lowered = stripped.lower()
    for bad in STICKY_BLOCKED:
        if bad in lowered:
            return False, "That note contains something that isn’t allowed."
    return True, ""


# Soft pastel fallbacks when user has no role color
STICKY_COLORS = [
    (255, 249, 196),
    (255, 224, 230),
    (220, 237, 255),
    (232, 245, 233),
    (255, 236, 210),
    (237, 231, 246),
    (255, 243, 224),
]

# Cute animal + fruit emojis for the corner
STICKY_EMOJIS = [
    "🦆", "🐸", "🦊", "🐰", "🐻", "🐼", "🐨", "🐯", "🦁", "🐮",
    "🐷", "🐵", "🐔", "🐧", "🐦", "🐤", "🦉", "🦇", "🐺", "🐗",
    "🐴", "🦄", "🐝", "🐛", "🦋", "🐌", "🐞", "🐜", "🐢", "🐍",
    "🦎", "🐙", "🦑", "🦐", "🦞", "🦀", "🐡", "🐠", "🐟", "🐬",
    "🐳", "🐋", "🦈", "🐊", "🐅", "🐆", "🦓", "🦍", "🐘", "🦛",
    "🦏", "🐪", "🦒", "🦘", "🦬", "🐄", "🐎", "🐖", "🐏", "🐑",
    "🦙", "🐐", "🦌", "🐕", "🐩", "🐈", "🦃", "🦚", "🦜", "🦢",
    "🦩", "🐇", "🦝", "🦨", "🦡", "🦫", "🦦", "🦥", "🐁", "🐀",
    "🐿", "🦔",
    "🍎", "🍐", "🍊", "🍋", "🍌", "🍉", "🍇", "🍓", "🫐", "🍈",
    "🍒", "🍑", "🥭", "🍍", "🥥", "🥝", "🍅", "🍆", "🥑", "🥦",
    "🥒", "🌶", "🌽", "🥕", "🫒", "🧄", "🧅", "🥔", "🥐", "🥯",
    "🍞", "🧀", "🥚", "🍳", "🥞", "🧇", "🥓", "🍗", "🌭", "🍔",
    "🍟", "🍕", "🥪", "🌮", "🌯", "🥗", "🍝", "🍜", "🍲", "🍛",
    "🍣", "🍱", "🥟", "🍤", "🍙", "🍚", "🍧", "🍨", "🍦", "🥧",
    "🧁", "🍰", "🎂", "🍮", "🍭", "🍬", "🍫", "🍿", "🍩", "🍪",
    "🌰", "🥜", "🍯", "☕", "🍵", "🧃", "🥤", "🧋", "🍺", "🍻",
    "🥂", "🍷", "🧊",
]


def _fade_color(rgb: tuple[int, int, int], amount: float = 0.68) -> tuple[int, int, int]:
    """Mix a color toward soft cream so handwriting stays readable."""
    target = (255, 252, 245)
    return tuple(
        int(c * (1 - amount) + t * amount)
        for c, t in zip(rgb, target)
    )


def _render_emoji_sticker(emoji_char: str, size: int = 36) -> "Image.Image":
    """Render a single emoji as a small transparent sticker."""
    emoji_font_candidates = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/NotoColorEmoji.ttf",
        os.path.join(_FONTS_DIR, "NotoColorEmoji.ttf"),
    ]
    for fpath in emoji_font_candidates:
        if not os.path.isfile(fpath):
            continue
        try:
            font = ImageFont.truetype(fpath, 109)
            canvas = Image.new("RGBA", (160, 160), (0, 0, 0, 0))
            draw = ImageDraw.Draw(canvas)
            draw.text((10, 10), emoji_char, font=font, embedded_color=True)
            bbox = canvas.getbbox()
            if not bbox:
                continue
            cropped = canvas.crop(bbox)
            return cropped.resize((size, size), Image.Resampling.LANCZOS)
        except Exception:
            continue

    # Fallback: soft pastel circle (no broken grey / text)
    fallback = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(fallback)
    d.ellipse([1, 1, size - 2, size - 2], fill=(255, 255, 255, 160))
    d.ellipse([4, 4, size - 5, size - 5], fill=(255, 200, 200, 200))
    return fallback


def create_sticky_note(
    text: str,
    author_name: str,
    user_color: tuple[int, int, int] | None = None,
) -> io.BytesIO:
    """Generate a square pastel sticky-note (transparent PNG)."""
    width = height = 420

    if user_color and user_color != (0, 0, 0):
        bg_color = _fade_color(user_color, amount=0.68)
    else:
        bg_color = random.choice(STICKY_COLORS)

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))

    # Soft drop shadow
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rounded_rectangle(
        [14, 16, width - 6, height - 6],
        radius=10,
        fill=(0, 0, 0, 50),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))
    img = Image.alpha_composite(img, shadow)

    # Main sticky body
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        [8, 8, width - 14, height - 14],
        radius=8,
        fill=bg_color + (255,),
    )

    # Subtle top tape
    tape_color = tuple(max(0, c - 30) for c in bg_color) + (210,)
    draw.rectangle(
        [width // 2 - 42, 6, width // 2 + 42, 18],
        fill=tape_color,
    )

    # Random animal / fruit emoji in top-right
    emoji_char = random.choice(STICKY_EMOJIS)
    sticker = _render_emoji_sticker(emoji_char, size=38)
    pad = 26
    sx = width - 14 - pad - sticker.width
    sy = 10 + pad
    img.paste(sticker, (sx, sy), sticker)

    # Random handwriting font (bundled)
    font_path, font_size = random.choice(STICKY_FONTS)
    try:
        font = ImageFont.truetype(font_path, font_size)
        name_font = ImageFont.truetype(font_path, max(18, font_size - 6))
    except Exception:
        try:
            font = ImageFont.truetype(STICKY_FONT_FALLBACK, 28)
            name_font = ImageFont.truetype(STICKY_FONT_FALLBACK, 20)
        except Exception:
            font = ImageFont.load_default()
            name_font = font

    wrapped = textwrap.fill(text.strip(), width=20)
    signature = f"— {author_name}"

    # Text layer (slight independent tilt)
    text_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    td = ImageDraw.Draw(text_layer)

    # Measure text block for vertical centering in the upper-middle area
    body_bbox = td.multiline_textbbox((0, 0), wrapped, font=font, spacing=10)
    body_w = body_bbox[2] - body_bbox[0]
    body_h = body_bbox[3] - body_bbox[1]

    # Leave room for emoji on the right; center text block left-of-center
    max_text_right = sx - 16
    text_x = max(28, (max_text_right - body_w) // 2)
    # Vertically: start in the upper half so it doesn't look stuck at the top
    text_y = max(50, min(90, (height - body_h) // 3))

    td.multiline_text(
        (text_x, text_y),
        wrapped,
        font=font,
        fill=(40, 40, 40, 255),
        spacing=10,
    )

    # Signature centered under the body — reads as part of the quote
    body_bbox2 = td.multiline_textbbox((text_x, text_y), wrapped, font=font, spacing=10)
    sig_bbox = td.textbbox((0, 0), signature, font=name_font)
    sig_w = sig_bbox[2] - sig_bbox[0]
    # A little more space below the note so it feels attached, not floating
    sig_y = body_bbox2[3] + 28
    if sig_y + 28 > height - 24:
        sig_y = height - 52
    # Center under the text block (not the whole card)
    body_center_x = (body_bbox2[0] + body_bbox2[2]) // 2
    sig_x = body_center_x - sig_w // 2
    # Keep a little margin from the edges
    sig_x = max(24, min(sig_x, width - 24 - sig_w))
    td.text(
        (sig_x, sig_y),
        signature,
        font=name_font,
        fill=(85, 85, 85, 255),
    )

    text_angle = random.uniform(-1.6, 1.6)
    text_layer = text_layer.rotate(
        text_angle, resample=Image.BICUBIC, expand=False, center=(width // 2, height // 2)
    )
    img = Image.alpha_composite(img, text_layer)

    # Whole-note tilt
    angle = random.uniform(-3.2, 3.2)
    img = img.rotate(angle, resample=Image.BICUBIC, expand=True)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    return buffer


@tree.command(name="sticky", description="Leave a little sticky note for the pond. Costs 1 sticky.")
@app_commands.describe(note="What do you want to write? (max 120 characters)")
async def sticky(interaction: discord.Interaction, note: str):
    """Post a handwritten sticky note image. Requires 1 sticky balance."""
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Sticky notes can only be used in a server.", ephemeral=True
        )
        return

    guild_id = interaction.guild.id
    user_id = interaction.user.id

    # Require a configured sticky notes channel
    settings = await get_guild_settings(guild_id)
    sticky_channel_id = settings.get("sticky_channel_id")
    if not sticky_channel_id:
        await interaction.response.send_message(
            "❌ No sticky notes channel is set yet.\n"
            "-# An admin needs to set one in `/settings`.",
            ephemeral=True,
        )
        return

    sticky_channel = interaction.guild.get_channel(sticky_channel_id)
    if sticky_channel is None or not isinstance(sticky_channel, discord.TextChannel):
        await interaction.response.send_message(
            "❌ The sticky notes channel no longer exists.\n"
            "-# An admin needs to set a new one in `/settings`.",
            ephemeral=True,
        )
        return

    # Must have at least 1 sticky
    balance = await get_sticky_balance(guild_id, user_id)
    if balance < 1:
        await interaction.response.send_message(
            "❌ You don’t have any stickies left.\n"
            "-# Win the daily crown to earn **+2** stickies.",
            ephemeral=True,
        )
        return

    # Moderation / length check
    ok, reason = _sticky_is_clean(note)
    if not ok:
        await interaction.response.send_message(f"❌ {reason}", ephemeral=True)
        return

    # Spend one sticky first (prevents free notes on image failure)
    spent = await spend_sticky(guild_id, user_id)
    if not spent:
        await interaction.response.send_message(
            "❌ You don’t have any stickies left.",
            ephemeral=True,
        )
        return

    # Generate image
    try:
        display_name = interaction.user.display_name
        if len(display_name) > 24:
            display_name = display_name[:22] + "…"

        user_color = None
        if isinstance(interaction.user, discord.Member):
            c = interaction.user.color
            if c and c.value != 0:
                user_color = c.to_rgb()

        image_buffer = create_sticky_note(note, display_name, user_color=user_color)
    except Exception as e:
        log.error("Failed to generate sticky note: %s", e)
        await add_stickies(guild_id, user_id, 1)  # refund
        await interaction.response.send_message(
            "❌ Something went wrong making your sticky note. Your sticky was refunded.",
            ephemeral=True,
        )
        return

    remaining = await get_sticky_balance(guild_id, user_id)
    remaining_display = format_sticky_count(remaining)

    file = discord.File(image_buffer, filename="sticky.png")

    # Post the note in the dedicated channel
    try:
        await sticky_channel.send(file=file)
    except Exception as e:
        log.error("Failed to post sticky note to channel %s: %s", sticky_channel_id, e)
        await add_stickies(guild_id, user_id, 1)  # refund
        await interaction.response.send_message(
            "❌ Couldn’t post to the sticky notes channel. Your sticky was refunded.",
            ephemeral=True,
        )
        return

    # Quiet confirmation to the user
    await interaction.response.send_message(
        f"✅ Sticky posted in {sticky_channel.mention}\n"
        f"-# 📝 Stickies left: **{remaining_display}**",
        ephemeral=True,
    )


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
    log.error("ERROR: DISCORD_TOKEN environment variable not set.")
else:
    bot.run(TOKEN)
