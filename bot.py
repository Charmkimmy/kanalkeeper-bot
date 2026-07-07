import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import aiosqlite
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# ========== WARNING DAYS CONFIG ==========
WARNING_DAYS = 5

# ========== MORE GREETING WORDS ==========
GREETING_WORDS = [
    "hi", "hello", "hey", "howdy", "greetings", "welcome",
    "good morning", "good afternoon", "good evening", "good night",
    "morning", "evening", "night", "yo", "sup", "what's up",
    "hiya", "hey there", "hi there", "hello there", "hi guys",
    "hello guys", "hey guys", "hi everyone", "hello everyone",
    "hey everyone", "hi all", "hello all", "hey all",
    "how are you", "how r u", "how are u", "howdy there",
    "g'day", "salutations", "bonjour", "hola", "ciao",
    "namaste", "shalom", "salaam", "konichiwa", "annyeong",
    "kamusta", "kumusta", "musta", "magandang umaga",
    "magandang hapon", "magandang gabi", "mabuhay",
    "buenos dias", "buenas tardes", "buenas noches",
    "que tal", "como estas", "hola a todos",
    "wassup", "wasup", "whats up", "what up", "yo yo",
    "heya", "ello", "hullo", "ahoy", "greetings earthlings",
    "top of the morning", "rise and shine", "good day",
    "pleased to meet you", "nice to meet you", "sup guys",
    "yo everyone", "yo all", "hello peeps", "hi peeps",
    "hey peeps", "what's good", "wagwan", "how it going",
    "how's it going", "how you doing", "how you doin",
    "sup yall", "sup y'all", "hey yall", "hey y'all"
]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ========== DATABASE ==========

async def init_db():
    async with aiosqlite.connect("kanalkeeper.db") as db:
        # Users table (works across all servers)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                guild_id INTEGER,
                username TEXT,
                last_greeting TEXT,
                streak INTEGER DEFAULT 0,
                warnings INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        
        # Warnings table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                date TEXT,
                reason TEXT
            )
        """)
        
        # Server settings table (admin-configurable)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                warning_channel_id INTEGER,
                admin_role_id INTEGER,
                enabled INTEGER DEFAULT 1
            )
        """)
        await db.commit()

def is_greeting(text):
    text = text.lower()
    return any(word in text for word in GREETING_WORDS)

async def record_greeting(user, guild_id):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, guild_id, username, last_greeting, streak)
            VALUES (?, ?, ?, ?, 0)
        """, (user.id, guild_id, str(user), today))
        
        await db.execute("""
            UPDATE users SET last_greeting = ?, streak = streak + 1, username = ?
            WHERE user_id = ? AND guild_id = ?
        """, (today, str(user), user.id, guild_id))
        await db.commit()

async def get_warning_channel(guild_id):
    """Get the warning channel for a server, or None if not set"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT warning_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        result = await cursor.fetchone()
        return result["warning_channel_id"] if result else None

# ========== EVENTS ==========

@bot.event
async def on_ready():
    print(f"👋 KanalKeeper is online! Logged in as {bot.user}")
    print(f"📅 Warning system: {WARNING_DAYS} days without greetings")
    print(f"🔍 Tracking greetings in ALL channels across ALL servers")
    await init_db()
    daily_check.start()

@bot.event
async def on_guild_join(guild):
    """When bot joins a new server, welcome message"""
    print(f"🎉 Joined new server: {guild.name} (ID: {guild.id})")
    
    # Try to find a general/welcome channel
    welcome_channel = None
    for channel in guild.text_channels:
        if "general" in channel.name.lower() or "welcome" in channel.name.lower():
            welcome_channel = channel
            break
    
    if not welcome_channel:
        welcome_channel = guild.system_channel or guild.text_channels[0]
    
    if welcome_channel:
        embed = discord.Embed(
            title="👋 KanalKeeper has arrived!",
            description="I'll help keep your server active and welcoming!",
            color=discord.Color.green()
        )
        embed.add_field(
            name="How it works",
            value=f"Members who don't greet anyone for **{WARNING_DAYS} days** get a warning ticket.",
            inline=False
        )
        embed.add_field(
            name="Admin Setup",
            value="Use `!setchannel #channel` to set where warning tickets go.\nUse `!toggle` to enable/disable me.",
            inline=False
        )
        embed.add_field(
            name="Commands",
            value="`!status` - Check your stats\n`!leaderboard` - Top greeters",
            inline=False
        )
        embed.set_footer(text="KanalKeeper • Keeping channels active")
        await welcome_channel.send(embed=embed)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    # Ignore DMs
    if not message.guild:
        return
    
    # Check if bot is enabled for this server
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT enabled FROM guild_settings WHERE guild_id = ?", (message.guild.id,)
        )
        result = await cursor.fetchone()
        if result and result[0] == 0:
            return  # Bot disabled for this server
    
    # Track greetings in ANY channel
    if is_greeting(message.content):
        await record_greeting(message.author, message.guild.id)
        print(f"👋 {message.author} greeted in #{message.channel.name} | Server: {message.guild.name}")
    
    await bot.process_commands(message)

# ========== DAILY CHECK ==========

@tasks.loop(hours=24)
async def daily_check():
    today = datetime.now().strftime("%Y-%m-%d")
    days_ago = (datetime.now() - timedelta(days=WARNING_DAYS)).strftime("%Y-%m-%d")
    
    print(f"🔍 Checking for inactive users (>{WARNING_DAYS} days)...")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        
        # Get all enabled guilds
        cursor = await db.execute("SELECT guild_id FROM guild_settings WHERE enabled = 1")
        enabled_guilds = await cursor.fetchall()
        guild_ids = [g["guild_id"] for g in enabled_guilds]
        
        # Also check guilds not in settings (default enabled)
        cursor = await db.execute("SELECT DISTINCT guild_id FROM users")
        all_guilds = await cursor.fetchall()
        for g in all_guilds:
            if g["guild_id"] not in guild_ids:
                guild_ids.append(g["guild_id"])
        
        for guild_id in guild_ids:
            # Find inactive users in this guild
            cursor = await db.execute("""
                SELECT user_id, username, last_greeting, warnings 
                FROM users 
                WHERE guild_id = ? AND (last_greeting <= ? OR last_greeting IS NULL)
            """, (guild_id, days_ago))
            
            inactive = await cursor.fetchall()
            
            for user in inactive:
                user_id = user["user_id"]
                warnings = user["warnings"] + 1
                
                await db.execute("""
                    INSERT INTO warnings (user_id, guild_id, date, reason)
                    VALUES (?, ?, ?, ?)
                """, (user_id, guild_id, today, f'{WARNING_DAYS} days without greetings'))
                
                await db.execute("""
                    UPDATE users SET warnings = ? WHERE user_id = ? AND guild_id = ?
                """, (warnings, user_id, guild_id))
                await db.commit()
                
                await send_warning(guild_id, user_id, user["username"], warnings, user["last_greeting"])

async def send_warning(guild_id, user_id, username, warning_count, last_greeting):
    channel_id = await get_warning_channel(guild_id)
    if not channel_id:
        print(f"⚠️ No warning channel set for guild {guild_id}")
        return
    
    channel = bot.get_channel(channel_id)
    if not channel:
        print(f"❌ Warning channel {channel_id} not found")
        return
    
    # Try DM first
    try:
        user = await bot.fetch_user(user_id)
        embed = discord.Embed(
            title="⚠️ KanalKeeper Warning Ticket",
            description=f"Hey! KanalKeeper noticed you haven't said hi in **{WARNING_DAYS}** days!",
            color=discord.Color.orange()
        )
        embed.add_field(name="Warning #", value=str(warning_count))
        embed.add_field(name="Last Greeting", value=last_greeting or "Never")
        embed.add_field(name="How to Fix", value="Say hi in any channel! 👋", inline=False)
        embed.set_footer(text="KanalKeeper • Keeping our channel active")
        await user.send(embed=embed)
    except Exception as e:
        print(f"❌ Could not DM user: {e}")
    
    # Post in warning channel
    embed = discord.Embed(
        title="🎫 KanalKeeper Warning Ticket",
        color=discord.Color.red() if warning_count >= 2 else discord.Color.orange(),
        timestamp=datetime.now()
    )
    embed.add_field(name="User", value=f"<@{user_id}> ({username})", inline=False)
    embed.add_field(name="Reason", value=f"{WARNING_DAYS} consecutive days without greetings", inline=False)
    embed.add_field(name="Warnings", value=str(warning_count), inline=True)
    embed.add_field(name="Last Greeting", value=last_greeting or "Never", inline=True)
    
    if warning_count >= 3:
        embed.add_field(name="⚠️ Note", value="Multiple warnings received. Please stay active!", inline=False)
    
    embed.set_footer(text="KanalKeeper • Keeping our channel active")
    await channel.send(embed=embed)

# ========== ADMIN COMMANDS ==========

@bot.command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx, channel: discord.TextChannel):
    """Set the warning ticket channel for this server"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO guild_settings (guild_id, warning_channel_id)
            VALUES (?, ?)
        """, (ctx.guild.id, channel.id))
        await db.commit()
    
    embed = discord.Embed(
        title="✅ Channel Set",
        description=f"Warning tickets will now be posted in {channel.mention}",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def toggle(ctx):
    """Enable or disable KanalKeeper in this server"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT enabled FROM guild_settings WHERE guild_id = ?", (ctx.guild.id,)
        )
        result = await cursor.fetchone()
        
        current = result["enabled"] if result else 1
        new_status = 0 if current == 1 else 1
        
        await db.execute("""
            INSERT OR REPLACE INTO guild_settings (guild_id, enabled)
            VALUES (?, ?)
        """, (ctx.guild.id, new_status))
        await db.commit()
    
    status = "enabled ✅" if new_status == 1 else "disabled ❌"
    embed = discord.Embed(
        title=f"KanalKeeper {status}",
        color=discord.Color.green() if new_status == 1 else discord.Color.red()
    )
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def settings(ctx):
    """View current server settings"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (ctx.guild.id,)
        )
        result = await cursor.fetchone()
    
    embed = discord.Embed(
        title="⚙️ KanalKeeper Settings",
        color=discord.Color.blue()
    )
    
    if result:
        channel = bot.get_channel(result["warning_channel_id"])
        channel_mention = channel.mention if channel else "Not set"
        status = "Enabled ✅" if result["enabled"] == 1 else "Disabled ❌"
        
        embed.add_field(name="Warning Channel", value=channel_mention, inline=True)
        embed.add_field(name="Status", value=status, inline=True)
    else:
        embed.add_field(name="Warning Channel", value="Not set", inline=True)
        embed.add_field(name="Status", value="Enabled (default)", inline=True)
    
    embed.add_field(name="Warning Days", value=str(WARNING_DAYS), inline=True)
    await ctx.send(embed=embed)

# ========== USER COMMANDS ==========

@bot.command()
async def status(ctx):
    """Check your greeting stats"""
    if not ctx.guild:
        await ctx.send("This command only works in servers!")
        return
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ? AND guild_id = ?", 
            (ctx.author.id, ctx.guild.id)
        )
        data = await cursor.fetchone()
    
    if not data:
        embed = discord.Embed(
            title="📊 Your KanalKeeper Stats",
            description="No data yet! Start greeting people in any channel! 👋",
            color=discord.Color.blue()
        )
    else:
        embed = discord.Embed(
            title=f"📊 {ctx.author.display_name}'s KanalKeeper Stats",
            color=discord.Color.green() if data['warnings'] == 0 else discord.Color.orange()
        )
        embed.add_field(name="🔥 Streak", value=f"{data['streak']} days", inline=True)
        embed.add_field(name="👋 Total Greetings", value=str(data['streak']), inline=True)
        embed.add_field(name="⚠️ Warnings", value=str(data['warnings']), inline=True)
        embed.add_field(name="📅 Last Greeting", value=data['last_greeting'] or "Never", inline=True)
        
        if data['warnings'] > 0:
            embed.add_field(name="Status", value="🔴 At Risk — Say hi today!", inline=False)
        elif data['streak'] >= 7:
            embed.add_field(name="Status", value="🌟 Streak Master!", inline=False)
        else:
            embed.add_field(name="Status", value="🟢 All Good — Keep greeting!", inline=False)
    
    await ctx.send(embed=embed)

@bot.command()
async def leaderboard(ctx):
    """Top greeters in this server"""
    if not ctx.guild:
        await ctx.send("This command only works in servers!")
        return
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT username, streak, total_greetings 
            FROM users 
            WHERE guild_id = ?
            ORDER BY streak DESC 
            LIMIT 10
        """, (ctx.guild.id,))
        top_users = await cursor.fetchall()
    
    embed = discord.Embed(
        title="🏆 Greeting Leaderboard",
        description=f"Top greeters in {ctx.guild.name}!",
        color=discord.Color.gold()
    )
    
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    
    for i, user in enumerate(top_users):
        embed.add_field(
            name=f"{medals[i]} {user['username']}",
            value=f"🔥 {user['streak']} day streak",
            inline=False
        )
    
    if not top_users:
        embed.description = "No data yet! Be the first to greet! 👋"
    
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def warnlist(ctx):
    """[MOD] See all warnings in this server"""
    if not ctx.guild:
        await ctx.send("This command only works in servers!")
        return
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT w.*, u.username FROM warnings w
            JOIN users u ON w.user_id = u.user_id AND w.guild_id = u.guild_id
            WHERE w.guild_id = ?
            ORDER BY w.date DESC LIMIT 20
        """, (ctx.guild.id,))
        warnings = await cursor.fetchall()
    
    if not warnings:
        await ctx.send("✅ No active warnings in this server!")
        return
    
    embed = discord.Embed(title="⚠️ KanalKeeper Warning List", color=discord.Color.red())
    for w in warnings:
        embed.add_field(
            name=f"{w['username']} — {w['date']}",
            value=w['reason'],
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(manage_messages=True)
async def forgive(ctx, member: discord.Member):
    """[MOD] Clear a user's warnings in this server"""
    if not ctx.guild:
        await ctx.send("This command only works in servers!")
        return
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "DELETE FROM warnings WHERE user_id = ? AND guild_id = ?", 
            (member.id, ctx.guild.id)
        )
        await db.execute(
            "UPDATE users SET warnings = 0 WHERE user_id = ? AND guild_id = ?", 
            (member.id, ctx.guild.id)
        )
        await db.commit()
    
    embed = discord.Embed(
        title="✅ Forgiven",
        description=f"Cleared all warnings for {member.mention} in this server",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

# ========== ERROR HANDLING ==========

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        embed = discord.Embed(
            title="❌ Permission Denied",
            description="You need administrator/mod permissions to use this command.",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    elif isinstance(error, commands.MissingRequiredArgument):
        embed = discord.Embed(
            title="❌ Missing Argument",
            description=f"Usage: `!{ctx.command.name} {ctx.command.signature}`",
            color=discord.Color.red()
        )
        await ctx.send(embed=embed)
    else:
        print(f"Error: {error}")

@bot.event
async def on_disconnect():
    print("⚠️ Disconnected! Trying to reconnect...")

@bot.event
async def on_resumed():
    print("✅ Reconnected!")

# ========== RUN ==========

if __name__ == "__main__":
    while True:
        try:
            bot.run(TOKEN, reconnect=True)
        except Exception as e:
            print(f"❌ Bot crashed: {e}")
            print("🔄 Restarting in 5 seconds...")
            import time
            time.sleep(5)