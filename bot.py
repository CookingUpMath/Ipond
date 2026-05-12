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

        # NEW — Daily power usage (Option A)
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
# GUILD SETTINGS
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


# -----------------------------------------
# /forcesync
# -----------------------------------------

@tree.command(name="forcesync", description="Force sync slash commands.")
async def forcesync(interaction: discord.Interaction):
    await tree.sync()
    await interaction.response.send_message("Slash commands synced.")


# -----------------------------------------
# BOT READY
# -----------------------------------------

@bot.event
async def on_ready():
    await init_db()
    await tree.sync()

    if not daily_reset_loop.is_running():
        daily_reset_loop.start()

    print(f"Bot is online as {bot.user}")


# -----------------------------------------
# MESSAGE COUNTING
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


def clean_display_name(name: str) -> str:
    return name.replace(" 🤡", "").replace("🤡", "").strip()


# -----------------------------------------
# APPLY CHAMPION ROLE
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

    # Remove from old champion
    for member in role.members:
        if member != new_champion:
            try:
                await member.remove_roles(role, reason="New champion crowned")
            except:
                pass

    # Apply to new champion
    if new_champion and role not in new_champion.roles:
        try:
            await new_champion.add_roles(role, reason="Champion crowned")
        except:
            pass
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

        # Curse expiry
        if curse_until and curse_until < now_utc:
            cursed_user = None
            curse_until = None
            update_needed = True

        # Mime expiry
        if mime_until and mime_until < now_utc:
            # DM the user when mime ends naturally
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

        # Jester expiry
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

        # CURSED — 20% chance to annoy
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

        # MIMED — delete only messages containing text
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

        # JESTER — add 🤡 while active
        if jester_user == message.author.id and jester_until and jester_until >= now_utc:
            try:
                if "🤡" not in message.author.display_name:
                    await message.author.edit(nick=f"{message.author.display_name} 🤡")
            except:
                pass

    await bot.process_commands(message)


# -----------------------------------------
# DAILY RESET LOGIC
# -----------------------------------------

async def reset_daily_power_usage(guild_id: int):
    await ensure_db()
    async with pool.acquire() as conn:
        await conn.execute("""
            DELETE FROM daily_power_usage WHERE guild_id = $1
        """, guild_id)


async def perform_reset_for_guild(guild: discord.Guild, settings: dict, now: datetime):
    await ensure_db()

    top = await get_top_user(guild.id)

    if top:
        winner_id = top["user_id"]

        # Increment all-time wins
        await ensure_db()
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

            # Rename champion role
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

            # Rename champion VC
            if settings["champion_vc_id"]:
                vc = guild.get_channel(settings["champion_vc_id"])
                if isinstance(vc, discord.VoiceChannel):
                    try:
                        await vc.edit(name=f"👑: {display_name}")
                    except:
                        pass

            # Update bot presence
            try:
                await bot.change_presence(
                    activity=discord.Activity(
                        type=discord.ActivityType.playing,
                        name=f"👑 {display_name}"
                    )
                )
            except:
                pass

        # Announce winner
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

    # Reset daily counts
    await reset_daily_counts(guild.id)

    # Reset daily power usage
    await reset_daily_power_usage(guild.id)

    # Clear active effects
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

    # Remove 🤡 from all nicknames
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
# /reseteffects — ADMIN (BEGINNING)
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

        # DM mimed users
        for uid in mimed_users:
            user = guild.get_member(uid)
            if user:
                try:
                    await user.send("🙊 You have done well mime. You may now speak!")
                except:
                    pass

        # Remove 🤡 from jester users
        for uid in jester_users:
            user = guild.get_member(uid)
            if user:
                try:
                    new_name = clean_display_name(user.display_name)
                    if new_name != user.display_name:
                        await user.edit(nick=new_name)
                except:
                    pass

        # Clear effects
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

        # Reset daily power usage
        await reset_daily_power_usage(guild.id)

        # Announce
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

    # DM if mimed
    if effect["mimed_user"] == target.id:
        try:
            await target.send("🙊 You have done well mime. You may now speak!")
        except:
            pass

    # Remove 🤡 if jestered
    if effect["jester_user"] == target.id:
        try:
            new_name = clean_display_name(target.display_name)
            if new_name != target.display_name:
                await target.edit(nick=new_name)
        except:
            pass

    # Clear this user's effects
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

    # Announce
    if settings["announce_channel_id"]:
        channel = guild.get_channel(settings["announce_channel_id"])
        if channel:
            try:
                await channel.send(f"🔧 Crown effects have been reset for {target.mention} by an admin.")
            except:
                pass

    await interaction.response.send_message(f"✅ All effects cleared for {target.mention}.")


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
