import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta
import aiosqlite
import os
import asyncio

try:
    from google import genai as google_genai
    _gemini_available = True
except ImportError:
    _gemini_available = False

TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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
    "gomoni", "gomo ni", "gomoni ni", "gomo ni sa inyo", "gomoni ni sa inyo", "GOMOO",
    "Hallooo, g'aftie", "GOMOOO",
]

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Gemini client (for AI chatbot)
if _gemini_available and GEMINI_API_KEY:
    ai_model = google_genai.Client(api_key=GEMINI_API_KEY)
else:
    ai_model = None

# In-memory chat history per DM user (keeps context)
chat_histories: dict[int, list[dict]] = {}

# Deduplication: track recently processed message IDs to prevent double-handling
_processed_message_ids: set = set()

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
                muted_until TEXT,
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        # Migrations for older databases
        for col_def in [
            "ALTER TABLE users ADD COLUMN joined_at TEXT",
            "ALTER TABLE users ADD COLUMN muted_until TEXT",
            "ALTER TABLE users ADD COLUMN last_warned TEXT",
        ]:
            try:
                await db.execute(col_def)
            except Exception:
                pass

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
                enabled INTEGER DEFAULT 1,
                announce_channel_id INTEGER
            )
        """)
        try:
            await db.execute("ALTER TABLE guild_settings ADD COLUMN announce_channel_id INTEGER")
        except Exception:
            pass

        await db.execute("""
            CREATE TABLE IF NOT EXISTS custom_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                word TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS ignored_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS greeting_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS greeting_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                guild_id INTEGER,
                date TEXT
            )
        """)
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
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT channel_id FROM greeting_channels WHERE guild_id = ?", (guild_id,)
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}

async def channel_counts_for_greeting(channel_id, guild_id):
    if await is_channel_ignored(channel_id):
        return False
    desired = await get_greeting_channels(guild_id)
    if not desired:
        return True
    return channel_id in desired

def message_has_gif_attachment(message):
    """True if the message has an attached image/GIF file or a Tenor/Giphy embed."""
    for attachment in message.attachments:
        content_type = (attachment.content_type or "").lower()
        filename = (attachment.filename or "").lower()
        if content_type.startswith("image/") or filename.endswith((".gif", ".png", ".jpg", ".jpeg", ".webp")):
            return True
    # Also detect GIF embeds (Tenor/Giphy links auto-embed)
    for embed in message.embeds:
        if embed.type in ("gifv", "image"):
            return True
        if embed.url and any(g in (embed.url or "") for g in ["tenor.com", "giphy.com"]):
            return True
    return False

async def record_greeting(user, guild_id):
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT frozen_until, last_greeting FROM users WHERE user_id = ? AND guild_id = ?",
            (user.id, guild_id)
        )
        result = await cursor.fetchone()
        if result and result[0]:
            frozen_until = datetime.strptime(result[0], "%Y-%m-%d")
            if frozen_until > datetime.now():
                return False

        previous_last_greeting = result[1] if result else None
        already_greeted_today = bool(previous_last_greeting == today)

        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, guild_id, username, last_greeting, streak, joined_at)
            VALUES (?, ?, ?, NULL, 0, ?)
        """, (user.id, guild_id, str(user), today))

        if already_greeted_today:
            await db.execute("""
                UPDATE users SET username = ? WHERE user_id = ? AND guild_id = ?
            """, (str(user), user.id, guild_id))
            await db.commit()
            return False

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

async def get_announce_channel(guild_id):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT announce_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        result = await cursor.fetchone()
        if result and result["announce_channel_id"]:
            return result["announce_channel_id"]
        # Fall back to warning channel
        cursor = await db.execute(
            "SELECT warning_channel_id FROM guild_settings WHERE guild_id = ?", (guild_id,)
        )
        result = await cursor.fetchone()
        return result["warning_channel_id"] if result else None

async def sync_guild_members(guild):
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

# ========== AI CHATBOT (DMs) ==========


async def get_ai_response(user_id: int, user_message: str, username: str) -> str:
    if not ai_model:
        return "Sorry, I don't have an AI brain set up yet! But I'm still keeping an eye on your greetings 👀"

    if user_id not in chat_histories:
        chat_histories[user_id] = []

    history = chat_histories[user_id]
    if len(history) > 20:
        history = history[-20:]
        chat_histories[user_id] = history

    system_prompt = (
        f"You are KanalKeeper, a friendly and witty Discord bot for the KNL (Kanalkonek) server. "
        f"You track daily greetings and keep the community active. "
        f"You have a fun Filipino/PH community vibe — mix Tagalog and English naturally. "
        f"You are now chatting with {username} in a DM. "
        f"Keep responses concise (2-4 sentences). "
        f"If asked about commands, mention !status, !leaderboard, !commands."
    )

    contents = []
    for msg in history:
        role = "user" if msg["role"] == "user" else "model"
        contents.append({"role": role, "parts": [{"text": msg["content"]}]})
    full_message = f"{system_prompt}\n\n{user_message}" if not contents else user_message
    contents.append({"role": "user", "parts": [{"text": full_message}]})

    # Try a few model names in order (whichever your API key supports)
    model_candidates = ["gemini-2.5-flash", "gemini-1.5-flash", "gemini-2.0-flash"]
    last_error = None

    for model_name in model_candidates:
        try:
            response = await ai_model.aio.models.generate_content(
                model=model_name,
                contents=contents,
            )
            reply = (response.text or "").strip()
            if not reply:
                continue
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": reply})
            return reply
        except Exception as e:
            last_error = e
            print(f"❌ Gemini error on {model_name}: {type(e).__name__}: {e}")
            continue

    return f"Ay nako, my brain glitched! ({type(last_error).__name__ if last_error else 'no reply'}) 😅"


# ========== EVENTS ==========

@bot.event
async def on_ready():
    print(f"👋 KanalKeeper is online! Logged in as {bot.user}")
    print(f"📅 Warning system: {WARNING_DAYS} days")
    print(f"🌐 Connected to {len(bot.guilds)} servers!")
    await init_db()

    for guild in bot.guilds:
        try:
            await sync_guild_members(guild)
        except Exception as e:
            print(f"❌ Could not sync members for {guild.name}: {e}")

    daily_check.start()
    reminder_check.start()
    weekly_summary.start()
    auto_unmute_check.start()
    daily_announce_6pm.start()
    daily_announce_midnight.start()

@bot.event
async def on_member_join(member):
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
            description="I'll help keep your server active and welcoming!\n\nYou can also DM me — I have AI chat! 🤖",
            color=discord.Color.green()
        )
        embed.add_field(
            name="How it works",
            value=f"Members who don't greet anyone for **{WARNING_DAYS} days** get a warning ticket.\n"
                  f"Say **hi**, **hello**, **mabuhay** or send a **GIF** to count!",
            inline=False
        )
        embed.add_field(
            name="Admin Setup",
            value="Use `!setchannel #channel` to set where warning tickets go.\n"
                  "Use `!setannouncechannel #channel` to set where daily roll-call announcements go.\n"
                  "Use `!toggle` to enable/disable me.",
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

    # Ignore webhook-sourced messages (e.g. channel mirrors) to prevent double processing
    if message.webhook_id:
        return

    # Deduplicate — skip if this message ID was already processed (guards against reconnect replays)
    if message.id in _processed_message_ids:
        return
    _processed_message_ids.add(message.id)
    if len(_processed_message_ids) > 2000:
        _processed_message_ids.clear()

    # ── DM: AI Chatbot ──
    if not message.guild:
        # Ignore commands in DMs (most are guild-only)
        if message.content.startswith("!"):
            return
        async with message.channel.typing():
            reply = await get_ai_response(
                message.author.id,
                message.content,
                message.author.display_name,
            )
        await message.channel.send(reply)
        return

    # ── Guild messages ──
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT enabled FROM guild_settings WHERE guild_id = ?", (message.guild.id,)
        )
        result = await cursor.fetchone()
        if result and result[0] == 0:
            await bot.process_commands(message)
            return

    if await channel_counts_for_greeting(message.channel.id, message.guild.id):
        desired = await get_greeting_channels(message.guild.id)

        if desired and message.channel.id in desired:
            if not message.content.startswith(bot.command_prefix):
                success = await record_greeting(message.author, message.guild.id)
                if success:
                    print(f"👋 {message.author} greeted (chat) in #{message.channel.name} | {message.guild.name}")
        else:
            words = await get_all_greeting_words(message.guild.id)
            text = message.content.lower()
            is_word_greeting = any(word in text for word in words)
            is_gif_greeting = message_has_gif_attachment(message)

            if is_word_greeting or is_gif_greeting:
                success = await record_greeting(message.author, message.guild.id)
                if success:
                    kind = "gif/image" if is_gif_greeting and not is_word_greeting else "text"
                    print(f"👋 {message.author} greeted ({kind}) in #{message.channel.name} | {message.guild.name}")

    await bot.process_commands(message)

# ========== AUTO-UNMUTE TASK ==========

@tasks.loop(minutes=10)
async def auto_unmute_check():
    """Every 10 minutes: unmute anyone whose muted_until has passed."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("""
            SELECT user_id, guild_id, username FROM users
            WHERE muted_until IS NOT NULL AND muted_until <= ?
        """, (now_str,))
        expired = await cursor.fetchall()

        for row in expired:
            guild = bot.get_guild(row["guild_id"])
            if not guild:
                continue
            member = guild.get_member(row["user_id"])
            mute_role = discord.utils.get(guild.roles, name="Muted")
            if member and mute_role and mute_role in member.roles:
                try:
                    await member.remove_roles(mute_role, reason="KanalKeeper: auto-unmute after 1 day")
                    print(f"🔊 Auto-unmuted {row['username']} in {guild.name}")
                except Exception as e:
                    print(f"❌ Auto-unmute failed for {row['username']}: {e}")

            # Clear muted_until regardless
            await db.execute("""
                UPDATE users SET muted_until = NULL WHERE user_id = ? AND guild_id = ?
            """, (row["user_id"], row["guild_id"]))

        if expired:
            await db.commit()

# ========== DAILY ROLL-CALL ANNOUNCE ==========

async def post_daily_rollcall(label: str):
    """Post a list of members who have NOT greeted today, in all enabled guilds."""
    today = datetime.now().strftime("%Y-%m-%d")

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

            channel_id = await get_announce_channel(guild_id)
            if not channel_id:
                continue
            channel = bot.get_channel(channel_id)
            if not channel:
                continue

            # Members who haven't greeted today
            cursor = await db.execute("""
                SELECT user_id, username, last_greeting, streak FROM users
                WHERE guild_id = ? AND (last_greeting IS NULL OR last_greeting < ?)
                ORDER BY username
            """, (guild_id, today))
            not_greeted = await cursor.fetchall()

            # Filter to only members still in the server (skip left members)
            pending = []
            for row in not_greeted:
                member = guild.get_member(row["user_id"])
                if not member:
                    try:
                        member = await guild.fetch_member(row["user_id"])
                    except (discord.NotFound, discord.HTTPException):
                        member = None
                if member and not member.bot:
                    pending.append((member, row["streak"]))

            if not pending:
                embed = discord.Embed(
                    title=f"🎉 {label} Roll-Call — {today}",
                    description="✅ **Everyone has greeted today!** Amazing community! 🥳",
                    color=discord.Color.green(),
                    timestamp=datetime.now()
                )
                embed.set_footer(text="KanalKeeper • Daily Roll-Call")
                await channel.send(embed=embed)
                continue

            # Build list (chunk to avoid embed limit)
            mentions = "\n".join(
                f"• {m.mention} {'🔥 ' + str(streak) + ' day streak' if streak > 0 else '💩 No streak'}"
                for m, streak in pending
            )
            if len(mentions) > 3800:
                mentions = mentions[:3800] + "\n…and more"

            embed = discord.Embed(
                title=f"📋 {label} Roll-Call — {today}",
                description=(
                    f"These **{len(pending)}** member(s) haven't greeted yet today.\n"
                    f"Say **hi**, **hello**, **mabuhay** or send a GIF to check in! 👋\n\n"
                    + mentions
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now()
            )
            embed.set_footer(text="KanalKeeper • Daily Roll-Call")
            await channel.send(embed=embed)
            print(f"📢 {label} roll-call posted in {guild.name} ({len(pending)} pending)")

@tasks.loop(hours=24)
async def daily_announce_6pm():
    await post_daily_rollcall("6 PM")

@tasks.loop(hours=24)
async def daily_announce_midnight():
    await post_daily_rollcall("12 AM")

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
                  AND (frozen_until IS NULL OR frozen_until <= ?)
                  AND (
                        last_warned IS NULL
                        OR (last_greeting IS NOT NULL AND last_warned < last_greeting)
                        OR (last_greeting IS NULL AND last_warned < joined_at)
                      )
            """, (guild_id, days_ago, days_ago, today))

            inactive = await cursor.fetchall()

            for user in inactive:
                user_id = user["user_id"]
                member_obj = guild.get_member(user_id)
                if member_obj and member_obj.bot:
                    continue

                warnings = user["warnings"] + 1

                mute_role = None
                muted_until_str = None
                if warnings >= 3:
                    mute_role = discord.utils.get(guild.roles, name="Muted")
                    if not mute_role:
                        try:
                            mute_role = await guild.create_role(name="Muted", reason="KanalKeeper auto-mute")
                            for channel in guild.channels:
                                await channel.set_permissions(mute_role, send_messages=False)
                        except Exception:
                            pass
                    # Set muted_until = now + 1 day
                    muted_until_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

                await db.execute("""
                    INSERT INTO warnings (user_id, guild_id, date, reason)
                    VALUES (?, ?, ?, ?)
                """, (user_id, guild_id, today, f'{WARNING_DAYS} days without greetings'))

                await db.execute("""
                    UPDATE users SET warnings = ?, muted_until = ?, last_warned = ?
                    WHERE user_id = ? AND guild_id = ?
                """, (warnings, muted_until_str, today, user_id, guild_id))
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

    if mute_role and member:
        try:
            await member.add_roles(mute_role, reason="KanalKeeper: 3+ warnings")
            print(f"🔇 Muted {username} for 3+ warnings (auto-unmutes in 1 day)")
        except Exception as e:
            print(f"❌ Could not mute: {e}")

    try:
        user = await bot.fetch_user(user_id)
        embed = discord.Embed(
            title="⚠️ KanalKeeper Warning",
            description=f"You haven't said hi in **{WARNING_DAYS}** days!",
            color=discord.Color.orange()
        )
        embed.add_field(name="Warning #", value=str(warning_count))
        if warning_count >= 3:
            embed.add_field(name="🔇 MUTED", value="You've been muted for **24 hours**. You'll be automatically unmuted after 1 day!", inline=False)
        embed.add_field(name="How to Fix", value="Say **hi**, **hello**, **mabuhay** or send a GIF in any channel! 👋", inline=False)
        await user.send(embed=embed)
    except Exception:
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

    if warning_count >= 3:
        embed.add_field(name="🔇 ACTION TAKEN", value="User has been muted for 24 hours (auto-unmutes automatically)", inline=False)

    await channel.send(embed=embed)

# ========== REMINDER CHECK ==========

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
                    embed.add_field(name="⚠️ Heads Up", value="If you don't say hi today, you'll get a warning ticket tomorrow!", inline=False)
                    embed.add_field(name="💡 How to Fix", value="Just say `hello`, `hi`, `mabuhay` or send a GIF in any channel! 👋", inline=False)
                    embed.add_field(name="🔥 Your Streak", value=f"{user['streak']} days", inline=True)
                    embed.set_footer(text="KanalKeeper • Keeping our community active")
                    await member.send(embed=embed)
                    print(f"📢 Reminder sent to {member.name}")
                except Exception as e:
                    print(f"❌ Could not send reminder: {e}")

# ========== WEEKLY SUMMARY ==========

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

# ========== COMMANDS ==========

@bot.command()
async def commands(ctx):
    is_admin = ctx.author.guild_permissions.administrator if ctx.guild else False
    is_mod = ctx.author.guild_permissions.manage_messages if ctx.guild else False

    embed = discord.Embed(
        title="📋 KanalKeeper Commands",
        description="Here are all the commands you can use!",
        color=discord.Color.blue()
    )

    user_cmds = (
        "`!status` — Check your greeting stats & badge\n"
        "`!leaderboard` — See top greeters\n"
        "`!commands` — Show this help\n"
        "`!appeal` — Request warning forgiveness"
    )
    embed.add_field(name="👤 User Commands", value=user_cmds, inline=False)

    if is_mod or is_admin:
        mod_cmds = (
            "`!warnlist` — See all active warnings\n"
            "`!forgive @user` — Clear a user's warnings\n"
            "`!freeze @user [days]` — Pause tracking\n"
            "`!unfreeze @user` — Resume tracking\n"
            "`!ignore #channel` — Exclude channel from tracking\n"
            "`!unignore #channel` — Re-enable channel\n"
            "`!addgreetingchannel #channel` — Only count greetings here\n"
            "`!removegreetingchannel #channel` — Remove restriction\n"
            "`!unmute @user` — Manually unmute a user"
        )
        embed.add_field(name="🛡️ Mod Commands", value=mod_cmds, inline=False)

    if is_admin:
        admin_cmds = (
            "`!setchannel #channel` — Set warning tickets channel\n"
            "`!setannouncechannel #channel` — Set daily roll-call channel\n"
            "`!toggle` — Enable/disable\n"
            "`!settings` — View settings\n"
            "`!addword \"word\"` — Add custom greeting\n"
            "`!removeword \"word\"` — Remove custom greeting\n"
            "`!clearall` — Reset ALL warnings (new month)\n"
            "`!rollcall` — Post roll-call now\n"
            "`!checksync` — Diagnose Discord/DB member mismatches\n"
            "`!resync` — Re-sync all current members into the database"
        )
        embed.add_field(name="⚙️ Admin Commands", value=admin_cmds, inline=False)

    embed.add_field(
        name="ℹ️ About",
        value=(
            f"Warning after **{WARNING_DAYS}** days\n"
            f"Day **{WARNING_DAYS-1}** reminder DM\n"
            f"3 warnings = 24h mute (auto-unmuted!)\n"
            f"Greetings: words **or** GIFs count\n"
            f"'mabuhay' counts as a greeting ✅\n"
            f"Only **1** greeting per day counts toward streak\n"
            f"Roll-call announced at **6 PM** and **12 AM** daily\n"
            f"DM me to chat with AI! 🤖\n"
            f"Badges: 💩→🪳→🐌→🦟→🐊→🦨→🐀"
        ),
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
async def setannouncechannel(ctx, channel: discord.TextChannel):
    """Set the channel where daily 6pm and 12am roll-call announcements are posted."""
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT guild_id FROM guild_settings WHERE guild_id = ?", (ctx.guild.id,)
        )
        exists = await cursor.fetchone()
        if exists:
            await db.execute("""
                UPDATE guild_settings SET announce_channel_id = ? WHERE guild_id = ?
            """, (channel.id, ctx.guild.id))
        else:
            await db.execute("""
                INSERT INTO guild_settings (guild_id, announce_channel_id) VALUES (?, ?)
            """, (ctx.guild.id, channel.id))
        await db.commit()
    await ctx.send(f"✅ Daily roll-call announcements will go to {channel.mention} at **6 PM** and **12 AM**.")

@bot.command()
@admin_check()
async def rollcall(ctx):
    """Manually trigger a roll-call announcement right now."""
    await ctx.send("📋 Posting roll-call now...")
    await post_daily_rollcall("Manual")

@bot.command()
@admin_check()
async def checksync(ctx):
    """Diagnose mismatches between Discord's member list and the local DB/cache."""
    guild = ctx.guild
    discord_members = [m for m in guild.members if not m.bot]

    async with aiosqlite.connect("kanalkeeper.db") as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id FROM users WHERE guild_id = ?", (guild.id,)
        )
        db_rows = await cursor.fetchall()

    db_ids = {row["user_id"] for row in db_rows}
    discord_ids = {m.id for m in discord_members}

    in_discord_not_db = discord_ids - db_ids
    in_db_not_discord = db_ids - discord_ids

    unresolved = 0
    for uid in discord_ids:
        if guild.get_member(uid) is None:
            unresolved += 1

    embed = discord.Embed(title="🔍 Sync Check", color=discord.Color.blurple())
    embed.add_field(name="Discord members (non-bot)", value=str(len(discord_members)), inline=True)
    embed.add_field(name="DB rows for this guild", value=str(len(db_ids)), inline=True)
    embed.add_field(name="get_member() cache misses", value=str(unresolved), inline=True)
    embed.add_field(
        name="In Discord, missing from DB",
        value=", ".join(str(i) for i in list(in_discord_not_db)[:10]) or "None",
        inline=False
    )
    embed.add_field(
        name="In DB, not found in Discord",
        value=", ".join(str(i) for i in list(in_db_not_discord)[:10]) or "None",
        inline=False
    )
    if in_discord_not_db:
        embed.add_field(
            name="💡 Tip",
            value="Run `!resync` to add missing members to the database.",
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command()
@admin_check()
async def resync(ctx):
    """Re-sync all current guild members into the database (fixes missing rows)."""
    await sync_guild_members(ctx.guild)
    await ctx.send(f"✅ Re-synced {len([m for m in ctx.guild.members if not m.bot])} members for **{ctx.guild.name}**.")

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

        cursor = await db.execute(
            "SELECT word FROM custom_words WHERE guild_id = ?", (ctx.guild.id,)
        )
        custom_words = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT channel_id FROM ignored_channels WHERE guild_id = ?", (ctx.guild.id,)
        )
        ignored = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT channel_id FROM greeting_channels WHERE guild_id = ?", (ctx.guild.id,)
        )
        greet_channels = await cursor.fetchall()

    embed = discord.Embed(title="⚙️ Settings", color=discord.Color.blue())

    if result:
        channel = bot.get_channel(result["warning_channel_id"]) if result["warning_channel_id"] else None
        announce_channel = bot.get_channel(result["announce_channel_id"]) if result["announce_channel_id"] else None
        channel_mention = channel.mention if channel else "Not set"
        announce_mention = announce_channel.mention if announce_channel else "Same as warning channel"
        status = "Enabled ✅" if result["enabled"] == 1 else "Disabled ❌"
        embed.add_field(name="Warning Channel", value=channel_mention, inline=True)
        embed.add_field(name="Announce Channel", value=announce_mention, inline=True)
        embed.add_field(name="Status", value=status, inline=True)
    else:
        embed.add_field(name="Warning Channel", value="Not set", inline=True)
        embed.add_field(name="Announce Channel", value="Not set", inline=True)
        embed.add_field(name="Status", value="Enabled (default)", inline=True)

    embed.add_field(name="Warning Days", value=str(WARNING_DAYS), inline=True)
    embed.add_field(name="Reminder Day", value=f"Day {WARNING_DAYS-1}", inline=True)
    embed.add_field(name="Roll-Call Times", value="6 PM & 12 AM daily", inline=True)

    words_text = ", ".join([w["word"] for w in custom_words]) if custom_words else "None"
    embed.add_field(name="Custom Words", value=words_text, inline=False)

    ignored_text = ", ".join([f"<#{i['channel_id']}>" for i in ignored]) if ignored else "None"
    embed.add_field(name="Ignored Channels", value=ignored_text, inline=False)

    greet_text = ", ".join([f"<#{g['channel_id']}>" for g in greet_channels]) if greet_channels else "None (all channels count)"
    embed.add_field(name="Greeting Channels", value=greet_text, inline=False)

    await ctx.send(embed=embed)

@bot.command()
@admin_check()
async def addword(ctx, *, word: str):
    word = word.lower().strip()
    async with aiosqlite.connect("kanalkeeper.db") as db:
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
    word = word.lower().strip()
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "DELETE FROM custom_words WHERE guild_id = ? AND word = ?",
            (ctx.guild.id, word)
        )
        await db.commit()
    await ctx.send(f"✅ Removed `{word}` from custom greeting words.")

@bot.command()
@mod_check()
async def ignore(ctx, channel: discord.TextChannel):
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
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "DELETE FROM ignored_channels WHERE channel_id = ? AND guild_id = ?",
            (channel.id, ctx.guild.id)
        )
        await db.commit()
    await ctx.send(f"✅ {channel.mention} is now tracked again.")

@bot.command()
@mod_check()
async def addgreetingchannel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "INSERT OR IGNORE INTO greeting_channels (channel_id, guild_id) VALUES (?, ?)",
            (channel.id, ctx.guild.id)
        )
        await db.commit()
    await ctx.send(f"✅ {channel.mention} added as a greeting channel.")

@bot.command()
@mod_check()
async def removegreetingchannel(ctx, channel: discord.TextChannel):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute(
            "DELETE FROM greeting_channels WHERE channel_id = ? AND guild_id = ?",
            (channel.id, ctx.guild.id)
        )
        await db.commit()
    await ctx.send(f"✅ {channel.mention} removed from greeting channels.")

@bot.command()
async def appeal(ctx):
    if not ctx.guild:
        return await ctx.send("❌ This only works in servers!")

    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT warnings FROM users WHERE user_id = ? AND guild_id = ?",
            (ctx.author.id, ctx.guild.id)
        )
        result = await cursor.fetchone()
        if not result or result[0] == 0:
            return await ctx.send("✅ You have no warnings to appeal!")

        cursor = await db.execute(
            "SELECT status FROM appeals WHERE user_id = ? AND guild_id = ? AND status = 'pending'",
            (ctx.author.id, ctx.guild.id)
        )
        if await cursor.fetchone():
            return await ctx.send("⏳ You already have a pending appeal. Please wait for admin review.")

        await db.execute(
            "INSERT INTO appeals (user_id, guild_id, date, status) VALUES (?, ?, ?, 'pending')",
            (ctx.author.id, ctx.guild.id, datetime.now().strftime("%Y-%m-%d"))
        )
        await db.commit()

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

@bot.command()
@mod_check()
async def freeze(ctx, member: discord.Member, days: int = 7):
    if days < 1 or days > 30:
        return await ctx.send("❌ Freeze days must be between 1 and 30!")
    frozen_until = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            UPDATE users SET frozen_until = ? WHERE user_id = ? AND guild_id = ?
        """, (frozen_until, member.id, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ {member.mention} is frozen until **{frozen_until}**. No warnings during this time.")

@bot.command()
@mod_check()
async def unfreeze(ctx, member: discord.Member):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        await db.execute("""
            UPDATE users SET frozen_until = NULL WHERE user_id = ? AND guild_id = ?
        """, (member.id, ctx.guild.id))
        await db.commit()
    await ctx.send(f"✅ {member.mention} is no longer frozen. Tracking resumed!")

@bot.command()
@admin_check()
async def clearall(ctx):
    async with aiosqlite.connect("kanalkeeper.db") as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM warnings WHERE guild_id = ?", (ctx.guild.id,)
        )
        count = (await cursor.fetchone())[0]
        await db.execute("DELETE FROM warnings WHERE guild_id = ?", (ctx.guild.id,))
        await db.execute("UPDATE users SET warnings = 0 WHERE guild_id = ?", (ctx.guild.id,))
        await db.commit()
    await ctx.send(f"✅ Cleared **{count}** warnings! Everyone starts fresh! 🎉")

@bot.command()
@mod_check()
async def unmute(ctx, member: discord.Member):
    guild = ctx.guild
    mute_role = discord.utils.get(guild.roles, name="Muted")
    if mute_role and mute_role in member.roles:
        try:
            await member.remove_roles(mute_role, reason="Manual unmute by mod")
            async with aiosqlite.connect("kanalkeeper.db") as db:
                await db.execute("""
                    UPDATE users SET muted_until = NULL WHERE user_id = ? AND guild_id = ?
                """, (member.id, guild.id))
                await db.commit()
            await ctx.send(f"✅ {member.mention} has been unmuted!")
        except Exception as e:
            await ctx.send(f"❌ Could not unmute: {e}")
    else:
        await ctx.send("ℹ️ User is not muted.")

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

    frozen_until_val = data['frozen_until'] if 'frozen_until' in data.keys() else None
    if frozen_until_val:
        frozen_date = datetime.strptime(frozen_until_val, "%Y-%m-%d")
        if frozen_date > datetime.now():
            embed.add_field(name="❄️ Frozen Until", value=frozen_until_val, inline=True)

    muted_until_val = data['muted_until'] if 'muted_until' in data.keys() else None
    if muted_until_val:
        muted_date = datetime.strptime(muted_until_val, "%Y-%m-%d %H:%M:%S")
        if muted_date > datetime.now():
            embed.add_field(name="🔇 Muted Until", value=muted_until_val, inline=True)

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

    embed = discord.Embed(title="🏆 Greeting Leaderboard", color=discord.Color.gold())
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
    if isinstance(error, discord.ext.commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, discord.ext.commands.MissingRequiredArgument):
        await ctx.send(f"❌ Usage: `!{ctx.command.name} {ctx.command.signature}`")
    elif isinstance(error, discord.ext.commands.CheckFailure):
        await ctx.send("❌ You don't have permission to use this command.")
    elif isinstance(error, discord.ext.commands.BadArgument):
        await ctx.send(f"❌ Invalid argument. Usage: `!{ctx.command.name} {ctx.command.signature}`")
    else:
        print(f"Error: {error}")

@bot.event
async def on_disconnect():
    print("⚠️ Disconnected!")

@bot.event
async def on_resumed():
    print("✅ Reconnected!")

# ========== TASK SETUP: Fix time-based loops ==========

@daily_announce_6pm.before_loop
async def before_6pm():
    """Wait until the next 6 PM (UTC+8 = 10:00 UTC) to start the loop."""
    await bot.wait_until_ready()
    now = datetime.utcnow()
    # Target: 10:00 UTC = 6:00 PM Philippine Time (UTC+8)
    target = now.replace(hour=10, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    wait_seconds = (target - now).total_seconds()
    print(f"⏰ 6 PM roll-call will fire in {wait_seconds/3600:.1f} hours")
    await asyncio.sleep(wait_seconds)

@daily_announce_midnight.before_loop
async def before_midnight():
    """Wait until next 12 AM (UTC+8 = 16:00 UTC) to start the loop."""
    await bot.wait_until_ready()
    now = datetime.utcnow()
    # Target: 16:00 UTC = 12:00 AM Philippine Time (UTC+8)
    target = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    wait_seconds = (target - now).total_seconds()
    print(f"⏰ 12 AM roll-call will fire in {wait_seconds/3600:.1f} hours")
    await asyncio.sleep(wait_seconds)

@daily_check.before_loop
async def before_daily():
    await bot.wait_until_ready()

@reminder_check.before_loop
async def before_reminder():
    await bot.wait_until_ready()

@weekly_summary.before_loop
async def before_weekly():
    await bot.wait_until_ready()

@auto_unmute_check.before_loop
async def before_auto_unmute():
    await bot.wait_until_ready()

# ========== RUN ==========

if __name__ == "__main__":
    while True:
        try:
            bot.run(TOKEN, reconnect=True)
        except Exception as e:
            print(f"❌ Bot crashed: {e}")
            import time
            time.sleep(5)