import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import aiosqlite
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID"))
WARNING_CHANNEL_ID = int(os.getenv("WARNING_CHANNEL_ID"))

# ========== MORE GREETING WORDS ==========
GREETING_WORDS = [
    # English
    "hi", "hello", "hey", "howdy", "greetings", "welcome",
    "good morning", "good afternoon", "good evening", "good night",
    "morning", "evening", "night", "yo", "sup", "what's up",
    "hiya", "hey there", "hi there", "hello there", "hi guys",
    "hello guys", "hey guys", "hi everyone", "hello everyone",
    "hey everyone", "hi all", "hello all", "hey all",
    "how are you", "how r u", "how are u", "howdy there",
    "g'day", "salutations", "bonjour", "hola", "ciao",
    "namaste", "shalom", "salaam", "konichiwa", "annyeong",
    
    # Filipino/Tagalog
    "kamusta", "kumusta", "musta", "magandang umaga",
    "magandang hapon", "magandang gabi", "mabuhay",
    
    # Spanish
    "buenos dias", "buenas tardes", "buenas noches",
    "que tal", "como estas", "hola a todos",
    
    # Other common
    "wassup", "wasup", "whats up", "what up", "yo yo",
    "heya", "ello", "hullo", "ahoy", "greetings earthlings",
    "top of the morning", "rise and shine", "good day",
    "pleased to meet you", "nice to meet you", "sup guys",
    "yo everyone", "yo all", "hello peeps", "hi peeps",
    "hey peeps", "what's good", "wagwan", "how it going",
    "how's it going", "how you doing", "how you doin",
    "sup yall", "sup y'all", "hey yall", "hey y'all"
]

# ========== WARNING DAYS CONFIG ==========
WARNING_DAYS = 3  # Change this to any number you want

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ========== DATABASE ==========

async def init_db():
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                last_greeting TEXT,
                streak INTEGER DEFAULT 0,
                warnings INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date TEXT,
                reason TEXT
            )
        """)
        await db.commit()

def is_greeting(text):
    text = text.lower()
    return any(word in text for word in GREETING_WORDS)

async def record_greeting(user):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, last_greeting, streak)
            VALUES (?, ?, ?, 0)
        """, (user.id, str(user), today))
        
        await db.execute("""
            UPDATE users SET last_greeting = ?, streak = streak + 1, username = ?
            WHERE user_id = ?
        """, (today, str(user), user.id))
        await db.commit()

# ========== EVENTS ==========

@bot.event
async def on_ready():
    print(f"👋 KanalKeeper is online! Logged in as {bot.user}")
    print(f"📅 Warning system: {WARNING_DAYS} days without greetings")
    print(f"🔍 Tracking greetings in ALL channels")
    await init_db()
    daily_check.start()
    
    guild = bot.get_guild(GUILD_ID)
    if guild:
        try:
            await guild.me.edit(nick="KanalKeeper")
        except:
            pass

@bot.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        return
    
    # Track greetings in ANY channel (DMs too!)
    if is_greeting(message.content):
        await record_greeting(message.author)
        print(f"👋 {message.author} greeted in #{message.channel.name if hasattr(message.channel, 'name') else 'DM'}!")
    
    await bot.process_commands(message)

# ========== DAILY CHECK ==========

@tasks.loop(hours=24)
async def daily_check():
    days_ago = (datetime.now() - timedelta(days=WARNING_DAYS)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    
    print(f"🔍 Checking for inactive users (>{WARNING_DAYS} days)...")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        
        cursor = await db.execute("""
            SELECT user_id, username, last_greeting, warnings 
            FROM users 
            WHERE last_greeting <= ? OR last_greeting IS NULL
        """, (days_ago,))
        
        inactive = await cursor.fetchall()
        print(f"⚠️ Found {len(inactive)} inactive users")
        
        for user in inactive:
            user_id = user["user_id"]
            warnings = user["warnings"] + 1
            
            await db.execute("""
                INSERT INTO warnings (user_id, date, reason)
                VALUES (?, ?, ?)
            """, (user_id, today, f'{WARNING_DAYS} days without greetings'))
            
            await db.execute("""
                UPDATE users SET warnings = ? WHERE user_id = ?
            """, (warnings, user_id))
            await db.commit()
            
            await send_warning(user_id, user["username"], warnings, user["last_greeting"])

async def send_warning(user_id, username, warning_count, last_greeting):
    channel = bot.get_channel(WARNING_CHANNEL_ID)
    if not channel:
        print(f"❌ Warning channel not found!")
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

# ========== COMMANDS ==========

@bot.command()
async def status(ctx):
    """Check your greeting stats"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (ctx.author.id,))
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
@commands.has_permissions(manage_messages=True)
async def warnlist(ctx):
    """[MOD] See all warnings"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT w.*, u.username FROM warnings w
            JOIN users u ON w.user_id = u.user_id
            ORDER BY w.date DESC LIMIT 20
        """)
        warnings = await cursor.fetchall()
    
    if not warnings:
        await ctx.send("✅ No active warnings!")
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
    """[MOD] Clear a user's warnings"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("DELETE FROM warnings WHERE user_id = ?", (member.id,))
        await db.execute("UPDATE users SET warnings = 0 WHERE user_id = ?", (member.id,))
        await db.commit()
    
    embed = discord.Embed(
        title="✅ Forgiven",
        description=f"Cleared all warnings for {member.mention}",
        color=discord.Color.green()
    )
    await ctx.send(embed=embed)

# ========== 24/7 KEEP ALIVE ==========

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