import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import aiosqlite
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

WARNING_DAYS = 5


DEFAULT_GREETING_WORDS = [
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
    "sup yall", "sup y'all", "hey yall", "hey y'all", "gomo", 
    "gomoni", "gomo ni", "gomoni ni", "gomo ni sa inyo", "gomoni ni sa inyo","GOMOO",
    "Hallooo, g'aftie","GOMOOO",
]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ========== STREAK BADGES ==========

def get_badge(streak):
    if streak >= 50:
        return "🐀 Daga ng Kanal"
    elif streak >= 30:
        return "🦨 Amoy Imburnal"
    elif streak >= 20:
        return "🐊 Buwaya ng GC"
    elif streak >= 10:
        return "🦟 Lamok sa Tenga"
    elif streak >= 5:
        return "🐌 Sipsip sa Kanal"
    elif streak >= 3:
        return "🪳 Ipis na Buhay"
    else:
        return "💩 Taeng Hindi Na-flush"

# ========== DATABASE ==========

async def init_db():
    async with aiosqlite.connect("kanalkeeper.db") as db:
        # Users table
        # last_greeting: date of the user's last REAL counted greeting, or NULL
        #   if they've never greeted (used for consecutive-streak math).
        # joined_at: date the user was first tracked (join or sync). Used
        #   only for the warning grace period, so it never gets confused
        #   with an actual greeting date.
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER,
                guild_id INTEGER,
                username TEXT,
                last_greeting TEXT,
                streak INTEGER DEFAULT 0,
                warnings INTEGER DEFAULT 0,
                frozen_until TEXT,
                joined_at TEXT,
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        # Migration for pre-existing databases created before joined_at existed
        try:
            await db.execute("ALTER TABLE users ADD COLUMN joined_at TEXT")
        except Exception:
            pass  # column already exists
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
        # Guild settings table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                warning_channel_id INTEGER,
                enabled INTEGER DEFAULT 1
            )
        """)
        # Custom greeting words per guild
        await db.execute("""
            CREATE TABLE IF NOT EXISTS custom_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                word TEXT
            )
        """)
        # Ignored channels
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ignored_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER
            )
        """)
        # Desired/greeting channels (if a guild has any rows here, only these
        # channels are checked for greetings; otherwise all channels count,
        # minus ignored_channels)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS greeting_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER
            )
        """)
        # Log of individual counted greeting events (one row per user per day
        # they greeted), used for accurate weekly "Total Greetings" stats
        # separate from "Active Members" (distinct user count).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS greeting_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                date TEXT
            )
        """)
        # Appeals
        await db.execute("""
            CREATE TABLE IF NOT EXISTS appeals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                date TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        await db.commit()

async def get_all_greeting_words(guild_id):
    """Get default + custom greeting words for a guild"""
    words = list(DEFAULT_GREETING_WORDS)
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT word FROM custom_words WHERE guild_id = ?", (guild_id,)
        )
        custom = await cursor.fetchall()
        for row in custom:
            if row["word"] not in words:
                words.append(row["word"])
    
    return words

async def is_channel_ignored(channel_id):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT 1 FROM ignored_channels WHERE channel_id = ?", (channel_id,)
        )
        return await cursor.fetchone() is not None

async def get_greeting_channels(guild_id):
    """Return the set of channel IDs configured as 'desired' greeting channels
    for this guild. Empty set means no restriction (all channels count)."""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT channel_id FROM greeting_channels WHERE guild_id = ?", (guild_id,)
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}

async def channel_counts_for_greeting(channel_id, guild_id):
    """A channel counts for greeting detection if:
    - it isn't in ignored_channels, AND
    - either no greeting_channels are configured for the guild (detect everywhere),
      or this channel IS one of the configured greeting_channels."""
    if await is_channel_ignored(channel_id):
        return False
    desired = await get_greeting_channels(guild_id)
    if not desired:
        return True
    return channel_id in desired

def message_has_gif_attachment(message):
    """True if the message has an attached image/GIF file."""
    for attachment in message.attachments:
        content_type = (attachment.content_type or "").lower()
        filename = (attachment.filename or "").lower()
        if content_type.startswith("image/") or filename.endswith((".gif", ".png", ".jpg", ".jpeg", ".webp")):
            return True
    return False

def is_greeting(text, guild_id):
    text = text.lower()
    # This will be async in the actual call, but for simplicity we'll handle it in on_message
    return any(word in text for word in DEFAULT_GREETING_WORDS)

async def record_greeting(user, guild_id):
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    async with aiosqlite.connect("kanalkeeper.db") as db:
        # Check if user is frozen, and whether they already greeted today
        cursor = await db.execute(
            "SELECT frozen_until, last_greeting FROM users WHERE user_id = ? AND guild_id = ?",
            (user.id, guild_id)
        )
        result = await cursor.fetchone()
        if result and result[0]:
            frozen_until = datetime.strptime(result[0], "%Y-%m-%d")
            if frozen_until > datetime.now():
                return False  # User is frozen, don't record

        previous_last_greeting = result[1] if result else None
        already_greeted_today = bool(previous_last_greeting == today)

        # New rows use joined_at (not last_greeting) for the warning grace
        # period, so an actual first greeting today still counts below.
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, guild_id, username, last_greeting, streak, joined_at)
            VALUES (?, ?, ?, NULL, 0, ?)
        """, (user.id, guild_id, str(user), today))

        if already_greeted_today:
            # Only 1 greeting counts per day toward streak/leaderboard.
            # Still keep username fresh, but don't touch streak or last_greeting.
            await db.execute("""
                UPDATE users SET username = ? WHERE user_id = ? AND guild_id = ?
            """, (str(user), user.id, guild_id))
            await db.commit()
            return False

        # Streak requires consecutive days: only continue it if the user's
        # last real greeting was exactly yesterday. Any gap (or a first-ever
        # greeting) resets the streak to 1.
        if previous_last_greeting == yesterday:
            new_streak_sql = "streak + 1"
        else:
            new_streak_sql = "1"

        await db.execute(f"""
            UPDATE users SET last_greeting = ?, streak = {new_streak_sql}, username = ?
            WHERE user_id = ? AND guild_id = ?
        """, (today, str(user), user.id, guild_id))

        await db.execute("""
            INSERT INTO greeting_log (user_id, guild_id, date) VALUES (?, ?, ?)
        """, (user.id, guild_id, today))

        await db.commit()
        return True

async def get_warning_channel(guild_id):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT warning_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        result = await cursor.fetchone()
        return result["warning_channel_id"] if result else None

async def sync_guild_members(guild):
    """Add every non-bot member of a guild to the users table if not already tracked.
    Existing rows are left untouched (INSERT OR IGNORE), so this is safe to re-run.
    New rows get joined_at = today for the warning grace period, while
    last_greeting stays NULL until they actually greet — keeping the streak
    math clean and letting a same-day first greeting still count."""
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect("kanalkeeper.db") as db:
        for member in guild.members:
            if member.bot:
                continue
            await db.execute("""
                INSERT OR IGNORE INTO users (user_id, guild_id, username, last_greeting, streak, joined_at)
                VALUES (?, ?, ?, NULL, 0, ?)
            """, (member.id, guild.id, str(member), today))
        await db.commit()
    print(f"🔄 Synced members for {guild.name}")

# ========== EVENTS ==========

@bot.event
async def on_ready():
    print(f"👋 KanalKeeper is online! Logged in as {bot.user}")
    print(f"📅 Warning system: {WARNING_DAYS} days")
    print(f"🌐 Connected to {len(bot.guilds)} servers!")
    await init_db()

    # Make sure every existing human member is tracked, not just people
    # who have already said a greeting at least once. Bots are skipped.
    for guild in bot.guilds:
        try:
            await sync_guild_members(guild)
        except Exception as e:
            print(f"❌ Could not sync members for {guild.name}: {e}")

    daily_check.start()
    reminder_check.start()
    weekly_summary.start()

@bot.event
async def on_member_join(member):
    """Start tracking a member the moment they join — unless they're a bot.
    They get joined_at = today for the grace period; last_greeting stays
    NULL so a genuine greeting on their join day still counts as day 1."""
    if member.bot:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, guild_id, username, last_greeting, streak, joined_at)
            VALUES (?, ?, ?, NULL, 0, ?)
        """, (member.id, member.guild.id, str(member), today))
        await db.commit()
    print(f"➕ Now tracking {member} in {member.guild.name}")


@bot.event
async def on_guild_join(guild):
    print(f"🎉 Joined new server: {guild.name} (ID: {guild.id})")

    # Track all existing non-bot members of the server we just joined
    try:
        await sync_guild_members(guild)
    except Exception as e:
        print(f"❌ Could not sync members for {guild.name}: {e}")

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

    # Only channels ignored, or (if configured) not one of the desired
    # greeting channels, are excluded from detection.
    if await channel_counts_for_greeting(message.channel.id, message.guild.id):
        desired = await get_greeting_channels(message.guild.id)

        if desired and message.channel.id in desired:
            # This is a configured "desired" greeting channel — ANY chat
            # message counts as a greeting (words/GIFs no longer required
            # here), except bot commands like !status which aren't chat.
            if not message.content.startswith(bot.command_prefix):
                success = await record_greeting(message.author, message.guild.id)
                if success:
                    print(f"👋 {message.author} greeted (chat) in #{message.channel.name} | Server: {message.guild.name}")
        else:
            # No desired channels configured (or this channel isn't restricted
            # to a list) — fall back to requiring a greeting word or GIF/image.
            words = await get_all_greeting_words(message.guild.id)
            text = message.content.lower()

            is_word_greeting = any(word in text for word in words)
            is_gif_greeting = message_has_gif_attachment(message)

            if is_word_greeting or is_gif_greeting:
                success = await record_greeting(message.author, message.guild.id)
                if success:
                    kind = "gif/image" if is_gif_greeting and not is_word_greeting else "text"
                    print(f"👋 {message.author} greeted ({kind}) in #{message.channel.name} | Server: {message.guild.name}")

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
                WHERE guild_id = ?
                  AND (
                        (last_greeting IS NOT NULL AND last_greeting <= ?)
                        OR (last_greeting IS NULL AND (joined_at IS NULL OR joined_at <= ?))
                      )
            """, (guild_id, days_ago, days_ago))
            
            inactive = await cursor.fetchall()
            
            for user in inactive:
                user_id = user["user_id"]

                # Double-check this isn't a bot account (defense in depth —
                # bots should never even be in this table, but skip just in case)
                member_obj = guild.get_member(user_id)
                if member_obj and member_obj.bot:
                    continue

                warnings = user["warnings"] + 1
                
                # Mute on 3 warnings
                mute_role = None
                if warnings >= 3:
                    mute_role = discord.utils.get(guild.roles, name="Muted")
                    if not mute_role:
                        try:
                            mute_role = await guild.create_role(name="Muted", reason="KanalKeeper auto-mute")
                            for channel in guild.channels:
                                await channel.set_permissions(mute_role, send_messages=False)
                        except:
                            pass
                
                await db.execute("""
                    INSERT INTO warnings (user_id, guild_id, date, reason)
                    VALUES (?, ?, ?, ?)
                """, (user_id, guild_id, today, f'{WARNING_DAYS} days without greetings'))
                
                await db.execute("""
                    UPDATE users SET warnings = ? WHERE user_id = ? AND guild_id = ?
                """, (warnings, user_id, guild_id))
                await db.commit()
                
                await send_warning(guild_id, user_id, user["username"], warnings, user["last_greeting"], mute_role)

async def send_warning(guild_id, user_id, username, warning_count, last_greeting, mute_role=None):
    channel_id = await get_warning_channel(guild_id)
    if not channel_id:
        return
    
    channel = bot.get_channel(channel_id)
    if not channel:
        return
    
    guild = bot.get_guild(guild_id)
    member = guild.get_member(user_id) if guild else None
    
    # Apply mute if 3+ warnings
    if mute_role and member:
        try:
            await member.add_roles(mute_role, reason="KanalKeeper: 3+ warnings")
            print(f"🔇 Muted {username} for 3+ warnings")
        except Exception as e:
            print(f"❌ Could not mute: {e}")
    
    # Try DM
    try:
        user = await bot.fetch_user(user_id)
        embed = discord.Embed(
            title="⚠️ KanalKeeper Warning",
            description=f"You haven't said hi in **{WARNING_DAYS}** days!",
            color=discord.Color.orange()
        )
        embed.add_field(name="Warning #", value=str(warning_count))
        if warning_count >= 3:
            embed.add_field(name="🔇 MUTED", value="You've been muted for 24 hours due to multiple warnings.", inline=False)
        embed.add_field(name="How to Fix", value="Say hi in any channel! 👋", inline=False)
        await user.send(embed=embed)
    except:
        pass
    
    # Post in warning channel
    embed = discord.Embed(
        title="🎫 Warning Ticket",
        color=discord.Color.red() if warning_count >= 2 else discord.Color.orange(),
        timestamp=datetime.now()
    )
    embed.add_field(name="User", value=f"<@{user_id}> ({username})", inline=False)
    embed.add_field(name="Reason", value=f"{WARNING_DAYS} days no greetings", inline=False)
    embed.add_field(name="Warnings", value=str(warning_count), inline=True)
    embed.add_field(name="Last Greeting", value=last_greeting or "Never", inline=True)
    
    if warning_count >= 3:
        embed.add_field(name="🔇 ACTION TAKEN", value="User has been muted for 24 hours", inline=False)
    
    await channel.send(embed=embed)


@tasks.loop(hours=24)
async def reminder_check():
    reminder_day = WARNING_DAYS - 1
    reminder_days_ago = (datetime.now() - timedelta(days=reminder_day)).strftime("%Y-%m-%d")
    
    print(f"📢 Sending day {reminder_day} reminders...")
    
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
                SELECT user_id, username, last_greeting, streak 
                FROM users 
                WHERE guild_id = ? AND last_greeting = ? AND (frozen_until IS NULL OR frozen_until <= ?)
            """, (guild_id, reminder_days_ago, datetime.now().strftime("%Y-%m-%d")))
            
            reminder_users = await cursor.fetchall()
            
            for user in reminder_users:
                try:
                    member = await bot.fetch_user(user["user_id"])
                    embed = discord.Embed(
                        title="⏰ Friendly Reminder",
                        description=f"Hey {member.mention}! You haven't greeted anyone in **{reminder_day}** days.",
                        color=discord.Color.yellow()
                    )
                    embed.add_field(name="⚠️ Heads Up", value=f"If you don't say hi today, you'll get a warning ticket tomorrow!", inline=False)
                    embed.add_field(name="💡 How to Fix", value="Just say `hello` or `hi` in any channel! 👋", inline=False)
                    embed.add_field(name="🔥 Your Streak", value=f"{user['streak']} days", inline=True)
                    embed.set_footer(text="KanalKeeper • Keeping our community active")
                    await member.send(embed=embed)
                    print(f"📢 Reminder sent to {member.name}")
                except Exception as e:
                    print(f"❌ Could not send reminder: {e}")

# ========== FEATURE 3: WEEKLY SUMMARY ==========

@tasks.loop(hours=168)
async def weekly_summary():
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
            
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            
            # Total Greetings: actual count of counted greeting events this
            # week (one per user per day they greeted), from greeting_log.
            cursor = await db.execute("""
                SELECT COUNT(*) as total_greetings FROM greeting_log
                WHERE guild_id = ? AND date >= ?
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
            
            # Active Members: distinct users who greeted at all this week —
            # a different, smaller-or-equal number than Total Greetings.
            cursor = await db.execute("""
                SELECT COUNT(DISTINCT user_id) as active_users FROM greeting_log
                WHERE guild_id = ? AND date >= ?
            """, (guild_id, week_ago))
            active_users = (await cursor.fetchone())["active_users"]
            
            embed = discord.Embed(
                title="📊 Weekly KanalKeeper Report",
                description=f"**{guild.name}** activity summary",
                color=discord.Color.purple(),
                timestamp=datetime.now()
            )
            
            embed.add_field(name="👋 Total Greetings", value=str(total_greetings), inline=True)
            embed.add_field(name="👥 Active Members", value=str(active_users), inline=True)
            embed.add_field(name="⚠️ Warnings Issued", value=str(warnings), inline=True)
            
            if top_user:
                badge = get_badge(top_user['streak'])
                embed.add_field(
                    name="🏆 Top Greeter",
                    value=f"{top_user['username']} — {badge}\n🔥 {top_user['streak']} day streak",
                    inline=False
                )
            
            embed.add_field(name="💡 Tip", value="Keep greeting daily to maintain your streak and earn badges!", inline=False)
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
    
    user_cmds = "`!status` — Check your greeting stats & badge\n`!leaderboard` — See top greeters\n`!commands` — Show this help\n`!appeal` — Request warning forgiveness"
    embed.add_field(name="👤 User Commands", value=user_cmds, inline=False)
    
    if is_mod or is_admin:
        mod_cmds = "`!warnlist` — See all active warnings\n`!forgive @user` — Clear a user's warnings\n`!freeze @user [days]` — Pause tracking\n`!ignore #channel` — Exclude channel from tracking\n`!addgreetingchannel #channel` — Only count greetings here\n`!removegreetingchannel #channel` — Remove restriction"
        embed.add_field(name="🛡️ Mod Commands", value=mod_cmds, inline=False)
    
    if is_admin:
        admin_cmds = "`!setchannel #channel` — Set warning tickets channel\n`!toggle` — Enable/disable\n`!settings` — View settings\n`!addword \"word\"` — Add custom greeting\n`!removeword \"word\"` — Remove custom greeting\n`!clearall` — Reset ALL warnings (new month)\n`!unmute @user` — Unmute a user"
        embed.add_field(name="⚙️ Admin Commands", value=admin_cmds, inline=False)
    
    embed.add_field(
        name="ℹ️ About",
        value=f"Warning after **{WARNING_DAYS}** days\n"
              f"Day **{WARNING_DAYS-1}** reminder DM\n"
              f"3 warnings = 24h mute\n"
              f"In a desired greeting channel: **any** chat message counts\n"
              f"Elsewhere: a greeting word or GIF/image attachment is needed\n"
              f"Only **1** greeting per day counts toward your streak\n"
              f"If greeting channels are set, only those channels count\n"
              f"Weekly summary every Sunday\n"
              f"Badges: 💩→🪳→🐌→🦟→🐊→🦨→🐀",
        inline=False
    )
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    await ctx.send(embed=embed)

@bot.command()
async def help(ctx):
    await commands(ctx)

# ========== ADMIN COMMANDS ==========

from discord.ext.commands import check

def admin_check():
    async def predicate(ctx):
        if not ctx.guild:
            return False
        return ctx.author.guild_permissions.administrator
    return check(predicate)

def mod_check():
    async def predicate(ctx):
        if not ctx.guild:
            return False
        return ctx.author.guild_permissions.manage_messages
    return check(predicate)

@bot.command()
@admin_check()
async def setchannel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            INSERT OR REPLACE INTO guild_settings (guild_id, warning_channel_id)
            VALUES (?, ?)
        """, (ctx.guild.id, channel.id))
        await db.commit()
    
    await ctx.send(f"✅ Warning tickets will go to {channel.mention}")

@bot.command()
@admin_check()
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
@admin_check()
async def settings(ctx):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM guild_settings WHERE guild_id = ?", (ctx.guild.id,)
        )
        result = await cursor.fetchone()
        
        # Get custom words
        cursor = await db.execute(
            "SELECT word FROM custom_words WHERE guild_id = ?", (ctx.guild.id,)
        )
        custom_words = await cursor.fetchall()
        
        # Get ignored channels
        cursor = await db.execute(
            "SELECT channel_id FROM ignored_channels WHERE guild_id = ?", (ctx.guild.id,)
        )
        ignored = await cursor.fetchall()

        # Get desired greeting channels
        cursor = await db.execute(
            "SELECT channel_id FROM greeting_channels WHERE guild_id = ?", (ctx.guild.id,)
        )
        greet_channels = await cursor.fetchall()
    
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
    
    # Custom words
    words_text = ", ".join([w["word"] for w in custom_words]) if custom_words else "None"
    embed.add_field(name="Custom Words", value=words_text, inline=False)
    
    # Ignored channels
    ignored_text = ", ".join([f"<#{i['channel_id']}>" for i in ignored]) if ignored else "None"
    embed.add_field(name="Ignored Channels", value=ignored_text, inline=False)

    # Desired greeting channels
    greet_text = ", ".join([f"<#{g['channel_id']}>" for g in greet_channels]) if greet_channels else "None (all channels count)"
    embed.add_field(name="Greeting Channels", value=greet_text, inline=False)
    
    await ctx.send(embed=embed)

# ========== NEW FEATURE: CUSTOM WORDS ==========

@bot.command()
@admin_check()
async def addword(ctx, *, word: str):
    """Add a custom greeting word for this server"""
    word = word.lower().strip()
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        # Check if already exists
        cursor = await db.execute(
            "SELECT 1 FROM custom_words WHERE guild_id = ? AND word = ?",
            (ctx.guild.id, word)
        )
        if await cursor.fetchone():
            return await ctx.send(f"❌ `{word}` is already a greeting word!")
        
        await db.execute(
            "INSERT INTO custom_words (guild_id, word) VALUES (?, ?)",
            (ctx.guild.id, word)
        )
        await db.commit()
    
    await ctx.send(f"✅ Added `{word}` as a custom greeting word!")

@bot.command()
@admin_check()
async def removeword(ctx, *, word: str):
    """Remove a custom greeting word"""
    word = word.lower().strip()
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "DELETE FROM custom_words WHERE guild_id = ? AND word = ?",
            (ctx.guild.id, word)
        )
        await db.commit()
    
    await ctx.send(f"✅ Removed `{word}` from custom greeting words.")

# ========== NEW FEATURE: IGNORE CHANNELS ==========

@bot.command()
@mod_check()
async def ignore(ctx, channel: discord.TextChannel):
    """Ignore a channel from greeting tracking"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO ignored_channels (channel_id, guild_id) VALUES (?, ?)",
            (channel.id, ctx.guild.id)
        )
        await db.commit()
    
    await ctx.send(f"✅ {channel.mention} is now ignored from greeting tracking.")

@bot.command()
@mod_check()
async def unignore(ctx, channel: discord.TextChannel):
    """Remove a channel from ignore list"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "DELETE FROM ignored_channels WHERE channel_id = ? AND guild_id = ?",
            (channel.id, ctx.guild.id)
        )
        await db.commit()
    
    await ctx.send(f"✅ {channel.mention} is now tracked again.")

# ========== NEW FEATURE: DESIRED GREETING CHANNELS ==========

@bot.command()
@mod_check()
async def addgreetingchannel(ctx, channel: discord.TextChannel):
    """Restrict greeting detection to this channel (multiple can be added).
    If no greeting channels are set, all channels count by default."""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO greeting_channels (channel_id, guild_id) VALUES (?, ?)",
            (channel.id, ctx.guild.id)
        )
        await db.commit()

    await ctx.send(f"✅ {channel.mention} added as a greeting channel. Only greetings in configured channels will count now.")

@bot.command()
@mod_check()
async def removegreetingchannel(ctx, channel: discord.TextChannel):
    """Remove a channel from the greeting-channel restriction list."""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "DELETE FROM greeting_channels WHERE channel_id = ? AND guild_id = ?",
            (channel.id, ctx.guild.id)
        )
        await db.commit()

    await ctx.send(f"✅ {channel.mention} removed from greeting channels.")

# ========== NEW FEATURE: WARNING APPEAL ==========

@bot.command()
async def appeal(ctx):
    """Request forgiveness for your warnings"""
    if not ctx.guild:
        return await ctx.send("❌ This only works in servers!")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        # Check if user has warnings
        cursor = await db.execute(
            "SELECT warnings FROM users WHERE user_id = ? AND guild_id = ?",
            (ctx.author.id, ctx.guild.id)
        )
        result = await cursor.fetchone()
        
        if not result or result[0] == 0:
            return await ctx.send("✅ You have no warnings to appeal!")
        
        # Check if already has pending appeal
        cursor = await db.execute(
            "SELECT status FROM appeals WHERE user_id = ? AND guild_id = ? AND status = 'pending'",
            (ctx.author.id, ctx.guild.id)
        )
        if await cursor.fetchone():
            return await ctx.send("⏳ You already have a pending appeal. Please wait for admin review.")
        
        # Create appeal
        await db.execute(
            "INSERT INTO appeals (user_id, guild_id, date, status) VALUES (?, ?, ?, 'pending')",
            (ctx.author.id, ctx.guild.id, datetime.now().strftime("%Y-%m-%d"))
        )
        await db.commit()
    
    # Notify admin channel
    channel_id = await get_warning_channel(ctx.guild.id)
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel:
            embed = discord.Embed(
                title="📋 New Appeal Request",
                description=f"{ctx.author.mention} is requesting forgiveness for their warnings.",
                color=discord.Color.yellow()
            )
            embed.add_field(name="User", value=f"{ctx.author} ({ctx.author.id})", inline=True)
            embed.add_field(name="Action", value="Use `!forgive @user` to approve", inline=False)
            await channel.send(embed=embed)
    
    await ctx.send("📋 Your appeal has been submitted! An admin will review it soon.")

# ========== NEW FEATURE: FREEZE USER ==========

@bot.command()
@mod_check()
async def freeze(ctx, member: discord.Member, days: int = 7):
    """Pause greeting tracking for a user (vacation/break)"""
    if days < 1 or days > 30:
        return await ctx.send("❌ Freeze days must be between 1 and 30!")
    
    frozen_until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            UPDATE users SET frozen_until = ? WHERE user_id = ? AND guild_id = ?
        """, (frozen_until, member.id, ctx.guild.id))
        await db.commit()
    
    await ctx.send(f"✅ {member.mention} is frozen until **{frozen_until}**. No warnings will be issued during this time.")

@bot.command()
@mod_check()
async def unfreeze(ctx, member: discord.Member):
    """Remove freeze from a user"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            UPDATE users SET frozen_until = NULL WHERE user_id = ? AND guild_id = ?
        """, (member.id, ctx.guild.id))
        await db.commit()
    
    await ctx.send(f"✅ {member.mention} is no longer frozen. Tracking resumed!")

# ========== NEW FEATURE: CLEAR ALL WARNINGS ==========

@bot.command()
@admin_check()
async def clearall(ctx):
    """Clear ALL warnings in this server (use for new month)"""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        # Count warnings
        cursor = await db.execute(
            "SELECT COUNT(*) FROM warnings WHERE guild_id = ?", (ctx.guild.id,)
        )
        count = (await cursor.fetchone())[0]
        
        # Clear all
        await db.execute("DELETE FROM warnings WHERE guild_id = ?", (ctx.guild.id,))
        await db.execute("UPDATE users SET warnings = 0 WHERE guild_id = ?", (ctx.guild.id,))
        await db.commit()
    
    await ctx.send(f"✅ Cleared **{count}** warnings! Everyone starts fresh! 🎉")

# ========== NEW FEATURE: UNMUTE USER ==========

@bot.command()
@mod_check()
async def unmute(ctx, member: discord.Member):
    """Manually unmute a user"""
    guild = ctx.guild
    mute_role = discord.utils.get(guild.roles, name="Muted")
    
    if mute_role and mute_role in member.roles:
        try:
            await member.remove_roles(mute_role, reason="Manual unmute by mod")
            await ctx.send(f"✅ {member.mention} has been unmuted!")
        except Exception as e:
            await ctx.send(f"❌ Could not unmute: {e}")
    else:
        await ctx.send("ℹ️ User is not muted.")

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
    
    # Check if frozen
    frozen_until_val = data['frozen_until'] if 'frozen_until' in data.keys() else None
    if frozen_until_val:
        frozen_date = datetime.strptime(frozen_until_val, "%Y-%m-%d")
        if frozen_date > datetime.now():
            embed.add_field(name="❄️ Frozen Until", value=frozen_until_val, inline=True)
    
    # Next badge
    if data['streak'] < 3:
        next_badge = "🪳 Ipis na Buhay (3 days)"
    elif data['streak'] < 5:
        next_badge = "🐌 Sipsip sa Kanal (5 days)"
    elif data['streak'] < 10:
        next_badge = "🦟 Lamok sa Tenga (10 days)"
    elif data['streak'] < 20:
        next_badge = "🐊 Buwaya ng GC (20 days)"
    elif data['streak'] < 30:
        next_badge = "🦨 Amoy Imburnal (30 days)"
    else:
        next_badge = "🐀 Daga ng Kanal (50 days)"
    
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
@mod_check()
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
@mod_check()
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
    # FIX: Use discord.ext.commands.MissingPermissions directly
    if isinstance(error, discord.ext.commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, discord.ext.commands.MissingRequiredArgument):
        await ctx.send(f"❌ Usage: `!{ctx.command.name} {ctx.command.signature}`")
    elif isinstance(error, discord.ext.commands.CheckFailure):
        await ctx.send("❌ You don't have permission to use this command.")
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