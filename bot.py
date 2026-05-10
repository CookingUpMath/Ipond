import discord
from discord.ext import commands, tasks
import asyncpg
import os
import random
from datetime import datetime, timedelta
import pytz

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# -----------------------------
# DATABASE CONNECTION
# -----------------------------
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:pLfujvTHBmCInONYmotvXXhBYqqsfljB@postgres.railway.internal:5432/railway"
)

pool: asyncpg.Pool | None = None

async def init_db():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL)

    async with pool.acquire() as conn:
        # Guild settings
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id BIGINT PRIMARY KEY,
            announce_channel_id BIGINT,
            champion_role_id BIGINT,
            champion_vc_id BIGINT,
            timezone_str TEXT DEFAULT 'EST',
            reset_hour INT DEFAULT 0,
            reset_minute INT DEFAULT 0,
            current_champion_id BIGINT,
            last_reset_date DATE
        );
        """)

        # Message counts (daily)
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

        # Crown uses (curse/mime/jester)
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

        # Active effects (curse/mime/jester)
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

    print("Database initialized and tables ensured.")


# -----------------------------
# DB HELPER FUNCTIONS
# -----------------------------

async def get_guild_settings(guild_id: int):
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
                "champion_role_id": None,
                "champion_vc_id": None,
                "timezone_str": "EST",
                "reset_hour": 0,
                "reset_minute": 0,
                "current_champion_id": None,
                "last_reset_date": None
            }

        return dict(row)


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


async def increment_all_time_win(guild_id: int, user_id: int):
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO all_time_wins (guild_id, user_id, wins)
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET wins = all_time_wins.wins + 1
        """, guild_id, user_id)


async def set_active_effect(guild_id: int, effect: str, user_id: int, until: datetime):
    async with pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO active_effects (guild_id, {effect}_user, {effect}_until)
            VALUES ($1, $2, $3)
            ON CONFLICT (guild_id)
            DO UPDATE SET {effect}_user = EXCLUDED.{effect}_user,
                          {effect}_until = EXCLUDED.{effect}_until
        """, guild_id, user_id, until)


async def clear_effect(guild_id: int, effect: str):
    async with pool.acquire() as conn:
        await conn.execute(f"""
            UPDATE active_effects
            SET {effect}_user = NULL,
                {effect}_until = NULL
            WHERE guild_id = $1
        """, guild_id)


@bot.event
async def on_ready():
    await init_db()
    print(f"Logged in as {bot.user}")

# -----------------------------------------
# PART 2 — MESSAGE COUNTING + DAILY RESET
# -----------------------------------------

def get_tz(settings: dict):
    try:
        return pytz.timezone(settings["timezone_str"])
    except:
        return pytz.timezone("EST")


async def get_daily_counts(guild_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, count FROM message_counts
            WHERE guild_id = $1
        """, guild_id)
        return {str(r["user_id"]): r["count"] for r in rows}


async def get_top_user(guild_id: int):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT user_id, count FROM message_counts
            WHERE guild_id = $1
            ORDER BY count DESC
            LIMIT 1
        """, guild_id)
        return row


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    guild = message.guild
    if guild is None:
        return

    settings = await get_guild_settings(guild.id)

    # Increment message count
    await increment_message_count(guild.id, message.author.id)

    # -----------------------------------------
    # CURSE / MIME / JESTER EFFECTS
    # -----------------------------------------
    async with pool.acquire() as conn:
        effect = await conn.fetchrow("""
            SELECT * FROM active_effects WHERE guild_id = $1
        """, guild.id)

    if effect:
        # CURSED USER — 30% chance to annoy
        if effect["cursed_user"] == message.author.id:
            if random.random() < 0.30:  # 30% chance
                await message.add_reaction("🦆")
                await message.channel.send("quack")

        # MIMED USER — delete message
        if effect["mimed_user"] == message.author.id:
            try:
                await message.delete()
            except:
                pass
            return

        # JESTER USER — add 🤡 to nickname
        if effect["jester_user"] == message.author.id:
            try:
                if "🤡" not in message.author.display_name:
                    await message.author.edit(nick=f"{message.author.display_name} 🤡")
            except:
                pass

    await bot.process_commands(message)


# -----------------------------------------
# DAILY RESET LOOP
# -----------------------------------------

@tasks.loop(minutes=1)
async def daily_reset_loop():
    for guild in bot.guilds:
        settings = await get_guild_settings(guild.id)
        tz = get_tz(settings)

        now = datetime.now(tz)
        reset_hour = settings["reset_hour"]
        reset_minute = settings["reset_minute"]

        # Check if it's time to reset
        if now.hour == reset_hour and now.minute == reset_minute:
            top = await get_top_user(guild.id)

            if top:
                winner_id = top["user_id"]
                await increment_all_time_win(guild.id, winner_id)

                # Announce winner
                if settings["announce_channel_id"]:
                    channel = guild.get_channel(settings["announce_channel_id"])
                    if channel:
                        member = guild.get_member(winner_id)
                        if member:
                            await channel.send(
                                f"🎉 **{member.display_name}** is today's champion with **{top['count']} messages!**"
                            )

                # Move champion to VC if set
                if settings["champion_vc_id"]:
                    vc = guild.get_channel(settings["champion_vc_id"])
                    if vc:
                        member = guild.get_member(winner_id)
                        if member and member.voice:
                            try:
                                await member.move_to(vc)
                            except:
                                pass

                # Update champion in DB
                async with pool.acquire() as conn:
                    await conn.execute("""
                        UPDATE guild_settings
                        SET current_champion_id = $1,
                            last_reset_date = $2
                        WHERE guild_id = $3
                    """, winner_id, now.date(), guild.id)

            # Reset daily counts
            await reset_daily_counts(guild.id)


@bot.event
async def on_ready():
    await init_db()
    daily_reset_loop.start()
    print(f"Logged in as {bot.user}")

# -----------------------------------------
# PART 3 — CROWN POWERS + LEADERBOARDS
# -----------------------------------------

async def increment_crown_use(guild_id: int, user_id: int, effect: str):
    column = {
        "curse": "curse_used",
        "mime": "mime_used",
        "jester": "jester_used"
    }.get(effect)

    if column is None:
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

    if column is None:
        return

    async with pool.acquire() as conn:
        await conn.execute(f"""
            INSERT INTO victim_stats (guild_id, user_id, {column})
            VALUES ($1, $2, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET {column} = victim_stats.{column} + 1
        """, guild_id, user_id)


async def get_crown_uses(guild_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, curse_used, mime_used, jester_used
            FROM crown_uses
            WHERE guild_id = $1
        """, guild_id)

    return {
        str(r["user_id"]): {
            "curse": r["curse_used"],
            "mime": r["mime_used"],
            "jester": r["jester_used"]
        }
        for r in rows
    }


async def get_victim_stats(guild_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, cursed, mimed, jestered
            FROM victim_stats
            WHERE guild_id = $1
        """, guild_id)

    return {
        str(r["user_id"]): {
            "cursed": r["cursed"],
            "mimed": r["mimed"],
            "jestered": r["jestered"]
        }
        for r in rows
    }


# -----------------------------------------
# LEADERBOARD COMMAND
# -----------------------------------------

@bot.command(name="leaderboard")
async def leaderboard(ctx):
    guild_id = ctx.guild.id

    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, wins
            FROM all_time_wins
            WHERE guild_id = $1
            ORDER BY wins DESC
            LIMIT 10
        """, guild_id)

    if not rows:
        await ctx.send("No wins recorded yet.")
        return

    embed = discord.Embed(
        title="🏆 All-Time Champion Leaderboard",
        color=discord.Color.gold()
    )

    for i, row in enumerate(rows, start=1):
        member = ctx.guild.get_member(row["user_id"])
        name = member.display_name if member else f"User {row['user_id']}"
        embed.add_field(
            name=f"#{i} — {name}",
            value=f"**{row['wins']} wins**",
            inline=False
        )

    await ctx.send(embed=embed)


# -----------------------------------------
# CROWN POWER COMMANDS
# -----------------------------------------

@bot.command(name="curse")
async def curse(ctx, member: discord.Member):
    guild_id = ctx.guild.id
    user_id = ctx.author.id

    # Record crown use
    await increment_crown_use(guild_id, user_id, "curse")
    await increment_victim_stat(guild_id, member.id, "curse")

    # Apply effect for 24 hours
    until = datetime.utcnow() + timedelta(hours=24)
    await set_active_effect(guild_id, "curse", member.id, until)

    await ctx.send(f"🦆 {member.mention} has been **cursed** for 24 hours!")


@bot.command(name="mime")
async def mime(ctx, member: discord.Member):
    guild_id = ctx.guild.id
    user_id = ctx.author.id

    await increment_crown_use(guild_id, user_id, "mime")
    await increment_victim_stat(guild_id, member.id, "mime")

    until = datetime.utcnow() + timedelta(hours=24)
    await set_active_effect(guild_id, "mime", member.id, until)

    await ctx.send(f"🤐 {member.mention} has been **mimed** for 24 hours!")


@bot.command(name="jester")
async def jester(ctx, member: discord.Member):
    guild_id = ctx.guild.id
    user_id = ctx.author.id

    await increment_crown_use(guild_id, user_id, "jester")
    await increment_victim_stat(guild_id, member.id, "jester")

    until = datetime.utcnow() + timedelta(hours=24)
    await set_active_effect(guild_id, "jester", member.id, until)

    await ctx.send(f"🤡 {member.mention} has been **jestered** for 24 hours!")

# -----------------------------------------
# PART 4 — ADMIN COMMANDS + SETTINGS
# -----------------------------------------

def admin_only():
    async def predicate(ctx):
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)


# -----------------------------------------
# SET ANNOUNCE CHANNEL
# -----------------------------------------

@bot.command(name="setannounce")
@admin_only()
async def setannounce(ctx, channel: discord.TextChannel):
    guild_id = ctx.guild.id

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET announce_channel_id = $1
            WHERE guild_id = $2
        """, channel.id, guild_id)

    await ctx.send(f"📢 Announce channel set to {channel.mention}")


# -----------------------------------------
# SET CHAMPION VC
# -----------------------------------------

@bot.command(name="setchampionvc")
@admin_only()
async def setchampionvc(ctx, channel: discord.VoiceChannel):
    guild_id = ctx.guild.id

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET champion_vc_id = $1
            WHERE guild_id = $2
        """, channel.id, guild_id)

    await ctx.send(f"🎧 Champion VC set to **{channel.name}**")


# -----------------------------------------
# SET TIMEZONE
# -----------------------------------------

@bot.command(name="settimezone")
@admin_only()
async def settimezone(ctx, tz: str):
    guild_id = ctx.guild.id

    try:
        pytz.timezone(tz)
    except:
        await ctx.send("❌ Invalid timezone. Example: `EST`, `America/New_York`, `UTC`")
        return

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET timezone_str = $1
            WHERE guild_id = $2
        """, tz, guild_id)

    await ctx.send(f"⏰ Timezone updated to **{tz}**")


# -----------------------------------------
# SET RESET TIME
# -----------------------------------------

@bot.command(name="setreset")
@admin_only()
async def setreset(ctx, hour: int, minute: int = 0):
    guild_id = ctx.guild.id

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        await ctx.send("❌ Invalid time. Use 24‑hour format: `!setreset 23 30`")
        return

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE guild_settings
            SET reset_hour = $1,
                reset_minute = $2
            WHERE guild_id = $3
        """, hour, minute, guild_id)

    await ctx.send(f"⏳ Daily reset time set to **{hour:02d}:{minute:02d}**")


# -----------------------------------------
# VIEW CURRENT SETTINGS
# -----------------------------------------

@bot.command(name="settings")
async def settings_cmd(ctx):
    guild_id = ctx.guild.id
    settings = await get_guild_settings(guild_id)

    embed = discord.Embed(
        title="⚙️ Server Settings",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Announce Channel",
        value=f"<#{settings['announce_channel_id']}>" if settings["announce_channel_id"] else "Not set",
        inline=False
    )

    embed.add_field(
        name="Champion VC",
        value=f"<#{settings['champion_vc_id']}>" if settings["champion_vc_id"] else "Not set",
        inline=False
    )

    embed.add_field(
        name="Timezone",
        value=settings["timezone_str"],
        inline=False
    )

    embed.add_field(
        name="Daily Reset Time",
        value=f"{settings['reset_hour']:02d}:{settings['reset_minute']:02d}",
        inline=False
    )

    embed.add_field(
        name="Current Champion",
        value=f"<@{settings['current_champion_id']}>" if settings["current_champion_id"] else "None",
        inline=False
    )

    await ctx.send(embed=embed)


# -----------------------------------------
# CLEAR EFFECTS (ADMIN)
# -----------------------------------------

@bot.command(name="cleareffects")
@admin_only()
async def cleareffects(ctx):
    guild_id = ctx.guild.id

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
        """, guild_id)

    await ctx.send("✨ All curse/mime/jester effects cleared.")

# -----------------------------------------
# PART 5 — FINAL STARTUP + TOKEN
# -----------------------------------------

@bot.event
async def on_ready():
    # Ensure DB is ready
    await init_db()

    # Start daily reset loop
    if not daily_reset_loop.is_running():
        daily_reset_loop.start()

    print(f"Bot is online as {bot.user}")


# Graceful shutdown (optional but clean)
async def close_db():
    global pool
    if pool:
        await pool.close()


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
