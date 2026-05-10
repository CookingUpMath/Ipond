# -*- coding: utf-8 -*-
import discord
from discord import app_commands
from discord.ext import tasks
import asyncio
import json
import os
from datetime import datetime, timedelta
import pytz
from aiohttp import web

# ===== Config =====
TOKEN = os.getenv("DISCORD_TOKEN")
DATA_FILE = "data.json"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

# Global state
mimed_user = None
mime_until = None
cursed_user = None
curse_until = None
jester_user = None
jester_until = None

# ===== Data Handling =====
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_guild_data(data, guild_id):
    gid = str(guild_id)
    if gid not in data:
        data[gid] = {
            "message_counts": {},
            "overall_wins": {},
            "crown_uses": {},
            "crown_uses_count": {},
            "cursed_victims": {},
            "mimed_victims": {},
            "jester_victims": {},
            "daily_counts": {},
            "timezone": "UTC",
            "announce_channel_id": None,
            "champion_role_id": None
        }
    return data[gid]

def get_today_key(tz_str):
    tz = pytz.timezone(tz_str)
    return datetime.now(tz).strftime("%Y-%m-%d")

# ===== Bot Events =====
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    await tree.sync()
    daily_reset.start()
    if not hasattr(bot, 'web_started'):
        bot.web_started = True
        await start_webserver()

@bot.event
async def on_message(message):
    global mimed_user, mime_until
    
    if message.author.bot or not message.guild:
        return
    
    # Handle mimed users
    if mimed_user and message.author.id == mimed_user:
        if datetime.now() < mime_until:
            is_allowed = False
            
            if message.stickers:
                is_allowed = True
            elif message.attachments:
                for att in message.attachments:
                    if any(att.filename.lower().endswith(ext) for ext in ['.gif', '.png', '.jpg', '.jpeg', '.webp']):
                        is_allowed = True
                        break
            elif any(x in message.content for x in ['tenor.com', 'giphy.com', 'cdn.discordapp.com']):
                is_allowed = True
            else:
                import re
                content_no_emoji = re.sub(r'<a?:\w+:\d+>', '', message.content)
                content_no_emoji = re.sub(r'[\U0001F000-\U0001FFFF]', '', content_no_emoji)
                content_no_emoji = content_no_emoji.strip()
                if not content_no_emoji and message.content.strip():
                    is_allowed = True
            
            if not is_allowed:
                try:
                    await message.delete()
                    await message.channel.send(f"{message.author.mention} 🙂 Mimed users can only send emojis, stickers, or GIFs!", delete_after=5)
                except:
                    pass
                return
        else:
            mimed_user = None
            mime_until = None
    
    # Track messages
    data = load_data()
    guild_data = get_guild_data(data, message.guild.id)
    uid = str(message.author.id)
    today = get_today_key(guild_data.get("timezone", "UTC"))
    
    daily = guild_data.setdefault("daily_counts", {}).setdefault(today, {})
    daily[uid] = daily.get(uid, 0) + 1
    
    save_data(data)
    await bot.process_commands(message)

# ===== Daily Reset =====
@tasks.loop(time=datetime.strptime("00:00", "%H:%M").time())
async def daily_reset():
    global mimed_user, mime_until, cursed_user, curse_until, jester_user, jester_until
    
    for guild in bot.guilds:
        data = load_data()
        guild_data = get_guild_data(data, guild.id)
        
        tz_str = guild_data.get("timezone", "UTC")
        yesterday = (datetime.now(pytz.timezone(tz_str)) - timedelta(days=1)).strftime("%Y-%m-%d")
        daily_counts = guild_data.get("daily_counts", {}).get(yesterday, {})
        
        if daily_counts:
            winner_id = max(daily_counts, key=daily_counts.get)
            winner = guild.get_member(int(winner_id))
            
            if winner:
                # Update overall wins
                wins = guild_data.setdefault("overall_wins", {})
                wins[winner_id] = wins.get(winner_id, 0) + 1
                
                # Update champion role
                champ_role_id = guild_data.get("champion_role_id")
                if champ_role_id:
                    role = guild.get_role(int(champ_role_id))
                    if role:
                        for member in guild.members:
                            if role in member.roles and member.id != winner.id:
                                await member.remove_roles(role)
                        if role not in winner.roles:
                            await winner.add_roles(role)
                
                # Update status
                await bot.change_presence(activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=f"👑 {winner.display_name}"
                ))
                
                # Announce
                ch_id = guild_data.get("announce_channel_id")
                if ch_id:
                    ch = guild.get_channel(int(ch_id))
                    if ch:
                        embed = discord.Embed(color=winner.color)
                        embed.description = f"-# All hail the top chatter\n# 👑 {winner.mention}"
                        embed.set_thumbnail(url=winner.display_avatar.url)
                        await ch.send(embed=embed)
        
        # Reset daily states
        mimed_user = None
        mime_until = None
        cursed_user = None
        curse_until = None
        jester_user = None
        jester_until = None
        
        save_data(data)

# ===== Commands =====
@tree.command(name="stats", description="Show your Top Duck wins and crown stats")
async def stats(interaction: discord.Interaction, user: discord.Member = None):
    target = user or interaction.user
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    
    uid = str(target.id)
    wins = guild_data.get("overall_wins", {}).get(uid, 0)
    crown_uses = guild_data.get("crown_uses_count", {}).get(uid, 0)
    cursed_count = guild_data.get("cursed_victims", {}).get(uid, 0)
    mimed_count = guild_data.get("mimed_victims", {}).get(uid, 0)
    jester_count = guild_data.get("jester_victims", {}).get(uid, 0)
    
    today = get_today_key(guild_data.get("timezone", "UTC"))
    messages_today = guild_data.get("daily_counts", {}).get(today, {}).get(uid, 0)
    
    embed = discord.Embed(
        title=f"📊 Stats for {target.display_name}",
        color=0x3498DB
    )
    embed.add_field(name="🏆 Top Duck Wins", value=f"`{wins}`", inline=True)
    embed.add_field(name="💬 Messages Today", value=f"`{messages_today}`", inline=True)
    embed.add_field(name="🙊", value="🙊", inline=True)
    
    crown_stats = f"👑: `{crown_uses}`  🤡: `{jester_count}`\n🔮: `{cursed_count}`  🙂: `{mimed_count}`"
    embed.add_field(name="Crown Stats", value=crown_stats, inline=False)
    
    await interaction.response.send_message(embed=embed)

@tree.command(name="curse", description="Curse a user - remove champion role for the day")
async def curse(interaction: discord.Interaction, user: discord.Member):
    global cursed_user, curse_until
    
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champ_role_id = guild_data.get("champion_role_id")
    
    if not champ_role_id or not any(role.id == int(champ_role_id) for role in interaction.user.roles):
        await interaction.response.send_message("❌ Only the current champion can use crown powers.", ephemeral=True)
        return
    
    if user.bot:
        await interaction.response.send_message("❌ You can't curse bots.", ephemeral=True)
        return
    
    crown_uses = guild_data.setdefault("crown_uses", {}).setdefault(str(interaction.user.id), {})
    if crown_uses.get("curse") == datetime.now().date().isoformat():
        await interaction.response.send_message("❌ You've already used /curse today.", ephemeral=True)
        return
    
    if cursed_user and datetime.now() < curse_until:
        await interaction.response.send_message("❌ Someone is already cursed today.", ephemeral=True)
        return
    
    try:
        role = interaction.guild.get_role(int(champ_role_id))
        if role in user.roles:
            await user.remove_roles(role)
    except:
        pass
    
    cursed_user = user.id
    curse_until = datetime.now().replace(hour=23, minute=59, second=59)
    crown_uses["curse"] = datetime.now().date().isoformat()
    
    guild_data.setdefault("cursed_victims", {})[str(user.id)] = guild_data.setdefault("cursed_victims", {}).get(str(user.id), 0) + 1
    guild_data.setdefault("crown_uses_count", {})[str(interaction.user.id)] = guild_data.setdefault("crown_uses_count", {}).get(str(interaction.user.id), 0) + 1
    save_data(data)
    
    await interaction.response.send_message(f"🔮 {user.mention} has been cursed and stripped of the crown!")

@tree.command(name="mime", description="Mime a user - they can only send emoji/sticker/GIF for 10 mins")
async def mime(interaction: discord.Interaction, user: discord.Member):
    global mimed_user, mime_until
    
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champ_role_id = guild_data.get("champion_role_id")
    
    if not champ_role_id or not any(role.id == int(champ_role_id) for role in interaction.user.roles):
        await interaction.response.send_message("❌ Only the current champion can use crown powers.", ephemeral=True)
        return
    
    if user.bot:
        await interaction.response.send_message("❌ You can't mime bots.", ephemeral=True)
        return
    
    crown_uses = guild_data.setdefault("crown_uses", {}).setdefault(str(interaction.user.id), {})
    if crown_uses.get("mime") == datetime.now().date().isoformat():
        await interaction.response.send_message("❌ You've already used /mime today.", ephemeral=True)
        return
    
    if mimed_user and datetime.now() < mime_until:
        await interaction.response.send_message("❌ Someone is already mimed.", ephemeral=True)
        return
    
    mimed_user = user.id
    mime_until = datetime.now() + timedelta(minutes=10)
    crown_uses["mime"] = datetime.now().date().isoformat()
    
    guild_data.setdefault("mimed_victims", {})[str(user.id)] = guild_data.setdefault("mimed_victims", {}).get(str(user.id), 0) + 1
    guild_data.setdefault("crown_uses_count", {})[str(interaction.user.id)] = guild_data.setdefault("crown_uses_count", {}).get(str(interaction.user.id), 0) + 1
    save_data(data)
    
    await interaction.response.send_message(f"🙂 {user.mention} has been mimed for 10 minutes! They can only send emojis, stickers, or GIFs.")

@tree.command(name="jester", description="Jester a user - change their nickname to clown emoji")
async def jester(interaction: discord.Interaction, user: discord.Member):
    global jester_user, jester_until
    
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    champ_role_id = guild_data.get("champion_role_id")
    
    if not champ_role_id or not any(role.id == int(champ_role_id) for role in interaction.user.roles):
        await interaction.response.send_message("❌ Only the current champion can use crown powers.", ephemeral=True)
        return
    
    if user.bot:
        await interaction.response.send_message("❌ You can't jester bots.", ephemeral=True)
        return
    
    crown_uses = guild_data.setdefault("crown_uses", {}).setdefault(str(interaction.user.id), {})
    if crown_uses.get("jester") == datetime.now().date().isoformat():
        await interaction.response.send_message("❌ You've already used /jester today.", ephemeral=True)
        return
    
    if jester_user and datetime.now() < jester_until:
        await interaction.response.send_message("❌ Someone is already jestered today.", ephemeral=True)
        return
    
    try:
        await user.edit(nick=f"🤡 {user.display_name}"[:32])
    except:
        await interaction.response.send_message("❌ I can't change that user's nickname.", ephemeral=True)
        return
    
    jester_user = user.id
    jester_until = datetime.now().replace(hour=23, minute=59, second=59)
    crown_uses["jester"] = datetime.now().date().isoformat()
    
    guild_data.setdefault("jester_victims", {})[str(user.id)] = guild_data.setdefault("jester_victims", {}).get(str(user.id), 0) + 1
    guild_data.setdefault("crown_uses_count", {})[str(interaction.user.id)] = guild_data.setdefault("crown_uses_count", {}).get(str(interaction.user.id), 0) + 1
    save_data(data)
    
    await interaction.response.send_message(f"🤡 {user.mention} has been jestered until midnight!")

@tree.command(name="setchannel", description="Set announcement channel")
@app_commands.default_permissions(administrator=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["announce_channel_id"] = str(channel.id)
    save_data(data)
    await interaction.response.send_message(f"✅ Announcements will go to {channel.mention}", ephemeral=True)

@tree.command(name="setchamprole", description="Set the champion role")
@app_commands.default_permissions(administrator=True)
async def setchamprole(interaction: discord.Interaction, role: discord.Role):
    data = load_data()
    guild_data = get_guild_data(data, interaction.guild.id)
    guild_data["champion_role_id"] = str(role.id)
    save_data(data)
    await interaction.response.send_message(f"✅ Champion role set to {role.mention}", ephemeral=True)

# ===== Webserver for Railway =====
async def handle_ping(request):
    return web.Response(text="Bot is alive!")

async def start_webserver():
    app = web.Application()
    app.router.add_get('/', handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.getenv('PORT', 8080)))
    await site.start()

# ===== Entry Point =====
async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
