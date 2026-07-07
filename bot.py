import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import aiosqlite
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

WARNING_DAYS = 5

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
    "heya", "ello", "hullo", "ahoy",
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

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ========== STREAK BADGES ==========

def get_badge(streak):
    if streak >= 30:
        return "👑 Legend"
    elif streak >= 14:
        return "💎 Master"
    elif streak >= 7:
        return "🦋 Social Butterfly"
    elif streak >= 3:
        return "😊 Friendly"
    else:
        return "🌱 Newbie"

# ========== DATABASE ==========

async def init_db():
    async with aiosqlite.connect("kanalkeeper.db") as db:
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                date TEXT,
                reason TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                warning_channel_id INTEGER,
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
    print(f"📅 Warning system: {WARNING_DAYS} days")
    print(f"🌐 Connected to {len(bot.guilds)} servers!")
    await init_db()
    daily_check.start()
    reminder_check.start()
    weekly_summary.start()

@bot.event
async def on_guild_join(guild):
    print(f"🎉 Joined new server: {guild.name} (ID: {guild.id})")
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
            value="`!commands` — See all commands\n`!status` — Check your stats\n`!leaderboard` — Top greeters",
            inline=False
        )
        embed.set_footer(text="KanalKeeper • Keeping channels active")
        await welcome_channel.send(embed=embed)

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    
    if not message.guild:
        return
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT enabled FROM guild_settings WHERE guild_id = ?", (message.guild.id,)
        )
        result = await cursor.fetchone()
        if result and result[0] == 0:
            return
    
    if is_greeting(message.content):
        await record_greeting(message.author, message.guild.id)
        print(f"👋 {message.author} greeted in #{message.channel.name} | Server: {message.guild.name}")
    
    await bot.process_commands(message)

# ========== DAILY CHECK (WARNINGS) ==========

@tasks.loop(hours=24)
async def daily_check():
    today = datetime.now().strftime("%Y-%m-%d")
    days_ago = (datetime.now() - timedelta(days=WARNING_DAYS)).strftime("%Y-%m-%d")
    
    print(f"🔍 Checking for inactive users across all servers...")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT DISTINCT guild_id FROM users")
        all_guilds = await cursor.fetchall()
        
        for guild_row in all_guilds:
            guild_id = guild_row["guild_id"]
            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            
            cursor = await db.execute(
                "SELECT enabled FROM guild_settings WHERE guild_id = ?", (guild_id,)
            )
            result = await cursor.fetchone()
            if result and result[0] == 0:
                continue
            
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
        return
    
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    
    try:
        user = await bot.fetch_user(user_id)
        embed = discord.Embed(
            title="⚠️ KanalKeeper Warning",
            description=f"You haven't said hi in **{WARNING_DAYS}** days!",
            color=discord.Color.orange()
        )
        embed.add_field(name="Warning #", value=str(warning_count))
        embed.add_field(name="How to Fix", value="Say hi in any channel! 👋", inline=False)
        await user.send(embed=embed)
    except:
        pass
    
    embed = discord.Embed(
        title="🎫 Warning Ticket",
        color=discord.Color.red() if warning_count >= 2 else discord.Color.orange(),
        timestamp=datetime.now()
    )
    embed.add_field(name="User", value=f"<@{user_id}> ({username})", inline=False)
    embed.add_field(name="Reason", value=f"{WARNING_DAYS} days no greetings", inline=False)
    embed.add_field(name="Warnings", value=str(warning_count), inline=True)
    embed.add_field(name="Last Greeting", value=last_greeting or "Never", inline=True)
    await channel.send(embed=embed)

# ========== FEATURE 1: DAILY REMINDER (Day 4) ==========

@tasks.loop(hours=24)
async def reminder_check():
    """Send friendly reminder on day 4 (1 day before warning)"""
    reminder_day = WARNING_DAYS - 1
    reminder_days_ago = (datetime.now() - timedelta(days=reminder_day)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    
    print(f"📢 Sending day {reminder_day} reminders...")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT DISTINCT guild_id FROM users
        """)
        all_guilds = await cursor.fetchall()
        
        for guild_row in all_guilds:
            guild_id = guild_row["guild_id"]
            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            
            cursor = await db.execute(
                "SELECT enabled FROM guild_settings WHERE guild_id = ?", (guild_id,)
            )
            result = await cursor.fetchone()
            if result and result[0] == 0:
                continue
            
            # Find users at day 4 (1 day before warning)
            cursor = await db.execute("""
                SELECT user_id, username, last_greeting, streak 
                FROM users 
                WHERE guild_id = ? AND last_greeting = ?
            """, (guild_id, reminder_days_ago))
            
            reminder_users = await cursor.fetchall()
            
            for user in reminder_users:
                try:
                    member = await bot.fetch_user(user["user_id"])
                    embed = discord.Embed(
                        title="⏰ Friendly Reminder",
                        description=f"Hey {member.mention}! You haven't greeted anyone in **{reminder_day}** days.",
                        color=discord.Color.yellow()
                    )
                    embed.add_field(
                        name="⚠️ Heads Up",
                        value=f"If you don't say hi today, you'll get a warning ticket tomorrow!",
                        inline=False
                    )
                    embed.add_field(
                        name="💡 How to Fix",
                        value="Just say `hello` or `hi` in any channel! 👋",
                        inline=False
                    )
                    embed.add_field(
                        name="🔥 Your Streak",
                        value=f"{user['streak']} days (will reset if you don't greet today)",
                        inline=True
                    )
                    embed.set_footer(text="KanalKeeper • Keeping our community active")
                    await member.send(embed=embed)
                    print(f"📢 Reminder sent to {member.name}")
                except Exception as e:
                    print(f"❌ Could not send reminder: {e}")

# ========== FEATURE 3: WEEKLY SUMMARY ==========

@tasks.loop(hours=168)  # Every 7 days
async def weekly_summary():
    """Post weekly activity summary every Sunday"""
    today = datetime.now().strftime("%Y-%m-%d")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT DISTINCT guild_id FROM guild_settings WHERE enabled = 1")
        enabled_guilds = await cursor.fetchall()
        
        for guild_row in enabled_guilds:
            guild_id = guild_row["guild_id"]
            guild = bot.get_guild(guild_id)
            if not guild:
                continue
            
            channel_id = await get_warning_channel(guild_id)
            if not channel_id:
                continue
            
            channel = bot.get_channel(channel_id)
            if not channel:
                continue
            
            # Get weekly stats
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            
            cursor = await db.execute("""
                SELECT COUNT(*) as total_greetings FROM users 
                WHERE guild_id = ? AND last_greeting >= ?
            """, (guild_id, week_ago))
            total_greetings = (await cursor.fetchone())["total_greetings"]
            
            cursor = await db.execute("""
                SELECT username, streak FROM users 
                WHERE guild_id = ? ORDER BY streak DESC LIMIT 1
            """, (guild_id,))
            top_user = await cursor.fetchone()
            
            cursor = await db.execute("""
                SELECT COUNT(*) as warning_count FROM warnings 
                WHERE guild_id = ? AND date >= ?
            """, (guild_id, week_ago))
            warnings = (await cursor.fetchone())["warning_count"]
            
            cursor = await db.execute("""
                SELECT COUNT(*) as active_users FROM users 
                WHERE guild_id = ? AND last_greeting >= ?
            """, (guild_id, week_ago))
            active_users = (await cursor.fetchone())["active_users"]
            
            # Create summary embed
            embed = discord.Embed(
                title="📊 Weekly KanalKeeper Report",
                description=f"**{guild.name}** activity summary",
                color=discord.Color.purple(),
                timestamp=datetime.now()
            )
            
            embed.add_field(
                name="👋 Total Greetings",
                value=str(total_greetings),
                inline=True
            )
            embed.add_field(
                name="👥 Active Members",
                value=str(active_users),
                inline=True
            )
            embed.add_field(
                name="⚠️ Warnings Issued",
                value=str(warnings),
                inline=True
            )
            
            if top_user:
                embed.add_field(
                    name="🏆 Top Greeter",
                    value=f"{top_user['username']} — {get_badge(top_user['streak'])}\n🔥 {top_user['streak']} day streak",
                    inline=False
                )
            
            embed.add_field(
                name="💡 Tip",
                value="Keep greeting daily to maintain your streak and earn badges!",
                inline=False
            )
            
            embed.set_footer(text="KanalKeeper • Weekly Report")
            await channel.send(embed=embed)
            print(f"📊 Weekly summary sent to {guild.name}")

# ========== HELP / COMMANDS LIST ==========

@bot.command()
async def commands(ctx):
    is_admin = ctx.author.guild_permissions.administrator if ctx.guild else False
    is_mod = ctx.author.guild_permissions.manage_messages if ctx.guild else False
    
    embed = discord.Embed(
        title="📋 KanalKeeper Commands",
        description="Here are all the commands you can use!",
        color=discord.Color.blue()
    )
    
    user_cmds = "`!status` — Check your greeting stats & badge\n`!leaderboard` — See top greeters\n`!commands` — Show this help"
    embed.add_field(name="👤 User Commands", value=user_cmds, inline=False)
    
    if is_mod or is_admin:
        mod_cmds = "`!warnlist` — See all active warnings\n`!forgive @user` — Clear a user's warnings"
        embed.add_field(name="🛡️ Mod Commands", value=mod_cmds, inline=False)
    
    if is_admin:
        admin_cmds = "`!setchannel #channel` — Set warning tickets channel\n`!toggle` — Enable/disable\n`!settings` — View settings"
        embed.add_field(name="⚙️ Admin Commands", value=admin_cmds, inline=False)
    
    embed.add_field(
        name="ℹ️ About",
        value=f"Warning after **{WARNING_DAYS}** days\n"
              f"Day **{WARNING_DAYS-1}** reminder DM\n"
              f"Weekly summary every Sunday\n"
              f"Badges: 🌱→😊→🦋→💎→👑",
        inline=False
    )
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.command()
async def help(ctx):
    await commands(ctx)

# ========== ADMIN COMMANDS ==========

from discord.ext.commands import check

def is_admin():
    async def predicate(ctx):
        if not ctx.guild:
            return False
        return ctx.author.guild_permissions.administrator
    return check(predicate)

def is_mod():
    async def predicate(ctx):
        if not ctx.guild:
            return False
        return ctx.author.guild_permissions.manage_messages
    return check(predicate)

@bot.command()
@is_admin()
async def setchannel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO guild_settings (guild_id, warning_channel_id)
            VALUES (?, ?)
        """, (ctx.guild.id, channel.id))
        await db.commit()
    
    await ctx.send(f"✅ Warning tickets will go to {channel.mention}")

@bot.command()
@is_admin()
async def toggle(ctx):
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
    await ctx.send(f"KanalKeeper {status}")

@bot.command()
@is_admin()
async def settings(ctx):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (ctx.guild.id,)
        )
        result = await cursor.fetchone()
    
    embed = discord.Embed(title="⚙️ Settings", color=discord.Color.blue())
    
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
    embed.add_field(name="Reminder Day", value=f"Day {WARNING_DAYS-1}", inline=True)
    embed.add_field(name="Weekly Summary", value="Every Sunday", inline=True)
    await ctx.send(embed=embed)

# ========== USER COMMANDS ==========

@bot.command()
async def status(ctx):
    if not ctx.guild:
        return await ctx.send("❌ This only works in servers!")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM users WHERE user_id = ? AND guild_id = ?", 
            (ctx.author.id, ctx.guild.id)
        )
        data = await cursor.fetchone()
    
    if not data:
        return await ctx.send("No data yet! Start greeting! 👋")
    
    badge = get_badge(data['streak'])
    
    embed = discord.Embed(
        title=f"📊 {ctx.author.display_name}'s Stats",
        color=discord.Color.blue()
    )
    embed.add_field(name="🏅 Badge", value=badge, inline=True)
    embed.add_field(name="🔥 Streak", value=f"{data['streak']} days", inline=True)
    embed.add_field(name="⚠️ Warnings", value=str(data['warnings']), inline=True)
    embed.add_field(name="📅 Last Greeting", value=data['last_greeting'] or "Never", inline=True)
    
    # Show next badge
    if data['streak'] < 3:
        next_badge = "😊 Friendly (3 days)"
    elif data['streak'] < 7:
        next_badge = "🦋 Social Butterfly (7 days)"
    elif data['streak'] < 14:
        next_badge = "💎 Master (14 days)"
    elif data['streak'] < 30:
        next_badge = "👑 Legend (30 days)"
    else:
        next_badge = "Max level reached!"
    
    embed.add_field(name="🎯 Next Badge", value=next_badge, inline=False)
    
    if data['warnings'] > 0:
        embed.add_field(name="Status", value="🔴 At Risk — Say hi today!", inline=False)
    
    await ctx.send(embed=embed)

@bot.command()
async def leaderboard(ctx):
    if not ctx.guild:
        return await ctx.send("❌ This only works in servers!")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT username, streak FROM users 
            WHERE guild_id = ? ORDER BY streak DESC LIMIT 10
        """, (ctx.guild.id,))
        top_users = await cursor.fetchall()
    
    embed = discord.Embed(
        title="🏆 Greeting Leaderboard",
        color=discord.Color.gold()
    )
    
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    
    for i, user in enumerate(top_users):
        badge = get_badge(user['streak'])
        embed.add_field(
            name=f"{medals[i]} {user['username']} {badge}",
            value=f"🔥 {user['streak']} day streak",
            inline=False
        )
    
    if not top_users:
        embed.description = "No data yet! Be first! 👋"
    
    await ctx.send(embed=embed)

@bot.command()
@is_mod()
async def warnlist(ctx):
    if not ctx.guild:
        return await ctx.send("❌ This only works in servers!")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT w.*, u.username FROM warnings w
            JOIN users u ON w.user_id = u.user_id AND w.guild_id = u.guild_id
            WHERE w.guild_id = ? ORDER BY w.date DESC LIMIT 20
        """, (ctx.guild.id,))
        warnings = await cursor.fetchall()
    
    if not warnings:
        return await ctx.send("✅ No warnings!")
    
    embed = discord.Embed(title="⚠️ Warnings", color=discord.Color.red())
    for w in warnings:
        embed.add_field(name=f"{w['username']} — {w['date']}", value=w['reason'], inline=False)
    
    await ctx.send(embed=embed)

@bot.command()
@is_mod()
async def forgive(ctx, member: discord.Member):
    if not ctx.guild:
        return await ctx.send("❌ This only works in servers!")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("DELETE FROM warnings WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
        await db.execute("UPDATE users SET warnings = 0 WHERE user_id = ? AND guild_id = ?", (member.id, ctx.guild.id))
        await db.commit()
    
    await ctx.send(f"✅ Cleared warnings for {member.mention}")

# ========== ERROR HANDLING ==========

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Usage: `!{ctx.command.name} {ctx.command.signature}`")
    else:
        print(f"Error: {error}")

@bot.event
async def on_disconnect():
    print("⚠️ Disconnected!")

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
            import time
            time.sleep(5)