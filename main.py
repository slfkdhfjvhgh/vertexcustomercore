import os
import re
import random
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
OPERATIONS_ROLE_ID = int(os.getenv("OPERATIONS_ROLE_ID", "0"))
TIMEZONE_NAME = os.getenv("TIMEZONE", "Asia/Kolkata")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")
if not GUILD_ID:
    raise RuntimeError("GUILD_ID is missing.")

try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except Exception:
    LOCAL_TZ = timezone.utc

BRAND_COLOR = 0x1B2A6B

MODMAIL_STAFF_ROLE_ID = 1523239968561172511
TICKET_CATEGORY_ID = 1523240280168599552
CLOSED_TICKET_LOG_CHANNEL_ID = 1523240300909301790
DEPARTURE_CHANNEL_ID = 1523011676365000885

LOGO = "<:logo:1523025299879493873>"
FLAG = "<:flag:1523024705999864051>"
ANNOUNCE = "<:announce:1523019706674708490>"
SCHEDULE = "<:schedule:1523018152668303512>"
NETWORK = "<:network:1523018073114677558>"
HELPDESK = "<:helpdesk:1523013663441686621>"
MAIL = "<:mail:1523013626976276610>"
ROBLOX = "<:roblox:1523013155494826045>"
INFORMATION = "<:information:1523012977308209182>"
VR_CROSS = "<:VR_cross:1523179629605687458>"
VR_TICK = "<:VR_tick:1523179608269258854>"
POINTER = "<:Pointer:1523241611171987506>"
FLIGHT = "<:flight:1523246237954871367>"

GREEK_NAMES = [
    "Alexandros", "Andreas", "Dimitrios", "Eleni", "Katerina",
    "Konstantinos", "Leonidas", "Nikos", "Sofia", "Stavros",
    "Theodoros", "Yannis",
]

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(
    command_prefix=commands.when_mentioned_or(".", ";"),
    intents=intents,
    help_command=None,
    case_insensitive=True,
)

db = sqlite3.connect("vertex_customer_core.db")
db.row_factory = sqlite3.Row
cursor = db.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    channel_id INTEGER,
    category TEXT NOT NULL,
    opened_at INTEGER NOT NULL,
    opened_by_id INTEGER NOT NULL,
    closed_at INTEGER,
    closed_by_id INTEGER,
    status TEXT NOT NULL DEFAULT 'open'
)
""")

cursor.execute("""
CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket_per_user
ON tickets(user_id)
WHERE status = 'open'
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS banned_users (
    user_id INTEGER PRIMARY KEY,
    banned_by_id INTEGER NOT NULL,
    banned_at INTEGER NOT NULL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS flights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER UNIQUE NOT NULL,
    event_url TEXT NOT NULL,
    flight_number TEXT NOT NULL,
    start_timestamp INTEGER NOT NULL,
    game_link TEXT NOT NULL,
    route TEXT NOT NULL,
    aircraft TEXT NOT NULL,
    scheduled_by_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS bot_config (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")
db.commit()


def utc_now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def make_embed(title: str, description: str = "") -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=BRAND_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="Vertex Air Customer Core")
    return embed


def clean_channel_name(name: str) -> str:
    name = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())
    return re.sub(r"-+", "-", name).strip("-")[:70] or "passenger"


def has_role(member: discord.Member, role_id: int) -> bool:
    return any(role.id == role_id for role in member.roles)


def is_modmail_staff(member: discord.Member) -> bool:
    return has_role(member, MODMAIL_STAFF_ROLE_ID)


def is_operations_staff(member: discord.Member) -> bool:
    if OPERATIONS_ROLE_ID and has_role(member, OPERATIONS_ROLE_ID):
        return True
    return member.guild_permissions.manage_events or member.guild_permissions.manage_guild


def staff_rank(member: discord.Member) -> str:
    roles = [r for r in member.roles if r != member.guild.default_role and not r.managed]
    return roles[-1].name if roles else "Helpline Agent"


def get_open_ticket_for_user(user_id: int):
    cursor.execute("SELECT * FROM tickets WHERE user_id = ? AND status = 'open'", (user_id,))
    return cursor.fetchone()


def get_open_ticket_for_channel(channel_id: int):
    cursor.execute("SELECT * FROM tickets WHERE channel_id = ? AND status = 'open'", (channel_id,))
    return cursor.fetchone()


def is_banned(user_id: int) -> bool:
    cursor.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,))
    return cursor.fetchone() is not None


async def add_reaction_safe(message: discord.Message, emoji_text: str):
    try:
        await message.add_reaction(discord.PartialEmoji.from_str(emoji_text))
    except Exception:
        pass


async def send_passenger_message_to_ticket(message: discord.Message, ticket_row):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return False
    channel = guild.get_channel(ticket_row["channel_id"])
    if not channel:
        return False

    description = message.content.strip() or "*No text was included.*"
    if message.attachments:
        description += "\n\n" + "\n".join(
            f"[Attachment {i}]({a.url})"
            for i, a in enumerate(message.attachments, start=1)
        )

    embed = make_embed(f"{MAIL} Passenger Message", description)
    embed.set_author(
        name=f"{message.author} • {message.author.id}",
        icon_url=message.author.display_avatar.url,
    )
    await channel.send(embed=embed)
    return True


pending_first_messages: dict[int, discord.Message] = {}


async def create_ticket(user, category_name: str, opened_by_id: int):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        raise RuntimeError("Configured guild not found.")

    if is_banned(user.id):
        raise RuntimeError("This user is banned.")

    existing = get_open_ticket_for_user(user.id)
    if existing:
        channel = guild.get_channel(existing["channel_id"])
        if channel:
            return channel

    category = guild.get_channel(TICKET_CATEGORY_ID)
    staff_role = guild.get_role(MODMAIL_STAFF_ROLE_ID)

    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("Ticket category not found.")
    if not staff_role:
        raise RuntimeError("Modmail staff role not found.")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        staff_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        ),
    }

    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            manage_channels=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
        )

    channel = await guild.create_text_channel(
        name=f"ticket-{clean_channel_name(user.name)}-{str(user.id)[-4:]}",
        category=category,
        overwrites=overwrites,
        topic=f"Vertex Air Modmail | User ID: {user.id} | Category: {category_name}",
        reason=f"Modmail ticket opened for {user}",
    )

    opened_at = utc_now_ts()
    cursor.execute(
        "INSERT INTO tickets (user_id, channel_id, category, opened_at, opened_by_id, status) VALUES (?, ?, ?, ?, ?, 'open')",
        (user.id, channel.id, category_name, opened_at, opened_by_id),
    )
    db.commit()

    embed = make_embed(
        f"{HELPDESK} New Helpline Ticket",
        f"{POINTER} A new passenger has connected.\n\n"
        f"**Passenger:** {user.mention} (`{user.id}`)\n"
        f"**Category:** {category_name}\n"
        f"**Opened:** <t:{opened_at}:F>\n"
        f"**Opened By:** <@{opened_by_id}>",
    )
    embed.set_thumbnail(url=user.display_avatar.url)

    await channel.send(
        content=staff_role.mention,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True),
    )
    return channel


class TicketCategorySelect(discord.ui.Select):
    def __init__(self, user_id: int):
        self.user_id = user_id
        options = [
            discord.SelectOption(label="Human Resources", value="Human Resources", emoji="👥"),
            discord.SelectOption(label="Public Relations", value="Public Relations", emoji="📣"),
            discord.SelectOption(label="General", value="General", emoji="📩"),
        ]
        super().__init__(
            placeholder="Choose a helpline department",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=make_embed(f"{VR_CROSS} Not Available", "This request belongs to another passenger."),
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        category = self.values[0]
        first_message = pending_first_messages.pop(self.user_id, None)

        if is_banned(self.user_id):
            await interaction.edit_original_response(
                embed=make_embed(
                    f"{VR_CROSS} Connection Refused",
                    f"{POINTER} You are currently unable to open a ticket.",
                ),
                view=None,
            )
            return

        if get_open_ticket_for_user(self.user_id):
            await interaction.edit_original_response(
                embed=make_embed(
                    f"{NETWORK} Already Connected",
                    f"{POINTER} You already have an active ticket.",
                ),
                view=None,
            )
            return

        try:
            await create_ticket(interaction.user, category, interaction.user.id)
        except Exception as exc:
            await interaction.edit_original_response(
                embed=make_embed(f"{VR_CROSS} Connection Failed", f"`{exc}`"),
                view=None,
            )
            return

        await interaction.edit_original_response(
            embed=make_embed(
                f"{NETWORK} Connected",
                f"{POINTER} You are now connected to our system. One of our helpline agents will be assisting you momentarily.",
            ),
            view=None,
        )

        if first_message:
            ticket = get_open_ticket_for_user(self.user_id)
            if ticket:
                await send_passenger_message_to_ticket(first_message, ticket)
                await add_reaction_safe(first_message, VR_TICK)


class TicketCategoryView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.add_item(TicketCategorySelect(user_id))


class TicketConfirmView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(
        label="Confirm",
        style=discord.ButtonStyle.success,
        emoji=discord.PartialEmoji.from_str(VR_TICK),
    )
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=make_embed(f"{VR_CROSS} Not Available", "This request belongs to another passenger."),
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=make_embed(
                f"{HELPDESK} Choose a Department",
                f"{POINTER} Please select the department that best matches your enquiry.",
            ),
            view=TicketCategoryView(self.user_id),
        )

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        emoji=discord.PartialEmoji.from_str(VR_CROSS),
    )
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                embed=make_embed(f"{VR_CROSS} Not Available", "This request belongs to another passenger."),
                ephemeral=True,
            )
            return
        pending_first_messages.pop(self.user_id, None)
        await interaction.response.edit_message(
            embed=make_embed(
                f"{VR_CROSS} Connection Cancelled",
                f"{POINTER} Your ticket was not created.",
            ),
            view=None,
        )


async def require_modmail_staff(ctx: commands.Context) -> bool:
    if not isinstance(ctx.author, discord.Member) or not is_modmail_staff(ctx.author):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Permission Denied",
                "Only authorized Vertex Air helpline staff may use this command.",
            )
        )
        return False
    return True


async def require_ticket_channel(ctx: commands.Context):
    ticket = get_open_ticket_for_channel(ctx.channel.id)
    if not ticket:
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Ticket Required",
                "This command can only be used inside an active ticket channel.",
            )
        )
        return None
    return ticket


@bot.command(name="reply")
async def reply_command(ctx: commands.Context, *, message: str):
    if not await require_modmail_staff(ctx):
        return
    ticket = await require_ticket_channel(ctx)
    if not ticket:
        return

    try:
        user = await bot.fetch_user(ticket["user_id"])
    except Exception:
        await ctx.send(embed=make_embed(f"{VR_CROSS} Passenger Not Found", "The passenger could not be found."))
        return

    embed = make_embed("", message)
    embed.set_author(name=ctx.author.display_name, icon_url=ctx.author.display_avatar.url)
    embed.set_footer(
        text=f"{staff_rank(ctx.author)} • {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M')}"
    )

    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Delivery Failed",
                "The passenger has disabled direct messages or blocked the bot.",
            )
        )
        return

    await add_reaction_safe(ctx.message, VR_TICK)


@bot.command(name="areply")
async def anonymous_reply_command(ctx: commands.Context, *, message: str):
    if not await require_modmail_staff(ctx):
        return
    ticket = await require_ticket_channel(ctx)
    if not ticket:
        return

    try:
        user = await bot.fetch_user(ticket["user_id"])
    except Exception:
        await ctx.send(embed=make_embed(f"{VR_CROSS} Passenger Not Found", "The passenger could not be found."))
        return

    alias = random.choice(GREEK_NAMES)
    embed = make_embed("", message)
    embed.set_author(name=alias, icon_url=bot.user.display_avatar.url)
    embed.set_footer(
        text=f"Vertex Air Helpline • {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M')}"
    )

    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Delivery Failed",
                "The passenger has disabled direct messages or blocked the bot.",
            )
        )
        return

    await add_reaction_safe(ctx.message, VR_TICK)


@bot.command(name="openfor")
async def open_for_command(ctx: commands.Context, user_id: int):
    if not await require_modmail_staff(ctx):
        return

    if is_banned(user_id):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} User Banned",
                "A ticket cannot be opened because this user is banned.",
            )
        )
        return

    try:
        user = await bot.fetch_user(user_id)
    except Exception:
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Invalid User",
                "A Discord user could not be found with that ID.",
            )
        )
        return

    existing = get_open_ticket_for_user(user_id)
    if existing:
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Ticket Already Open",
                f"This user already has an active ticket: <#{existing['channel_id']}>",
            )
        )
        return

    channel = await create_ticket(user, "General", ctx.author.id)

    try:
        await user.send(
            embed=make_embed(
                f"{NETWORK} Connected",
                f"{POINTER} A Vertex Air helpline agent has opened a ticket for you. You may reply directly to this DM.",
            )
        )
    except Exception:
        pass

    await ctx.send(
        embed=make_embed(
            f"{VR_TICK} Ticket Opened",
            f"The ticket has been created successfully: {channel.mention}",
        )
    )


@bot.command(name="ban")
async def ban_command(ctx: commands.Context, user_id: int):
    if not await require_modmail_staff(ctx):
        return

    cursor.execute(
        """
        INSERT INTO banned_users (user_id, banned_by_id, banned_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            banned_by_id = excluded.banned_by_id,
            banned_at = excluded.banned_at
        """,
        (user_id, ctx.author.id, utc_now_ts()),
    )
    db.commit()

    ticket = get_open_ticket_for_user(user_id)
    if ticket:
        channel = ctx.guild.get_channel(ticket["channel_id"])
        cursor.execute(
            "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by_id = ? WHERE ticket_id = ?",
            (utc_now_ts(), ctx.author.id, ticket["ticket_id"]),
        )
        db.commit()
        if channel:
            try:
                await channel.delete(reason=f"User banned by {ctx.author}")
            except Exception:
                pass

    await ctx.send(
        embed=make_embed(
            f"{VR_TICK} User Banned",
            f"User ID `{user_id}` has been banned from using modmail.",
        )
    )


@bot.command(name="unban")
async def unban_command(ctx: commands.Context, user_id: int):
    if not await require_modmail_staff(ctx):
        return

    cursor.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    db.commit()

    await ctx.send(
        embed=make_embed(
            f"{VR_TICK} User Unbanned",
            f"User ID `{user_id}` may now use the Vertex Air helpline again.",
        )
    )


async def close_ticket(ticket_row, closed_by: discord.Member, source_channel: discord.TextChannel):
    closed_at = utc_now_ts()

    cursor.execute(
        "UPDATE tickets SET status = 'closed', closed_at = ?, closed_by_id = ? WHERE ticket_id = ?",
        (closed_at, closed_by.id, ticket_row["ticket_id"]),
    )
    db.commit()

    try:
        user = await bot.fetch_user(ticket_row["user_id"])
        await user.send(
            embed=make_embed(
                f"{VR_CROSS} Ticket Closed",
                f"{POINTER} Your Vertex Air helpline ticket has been closed by a staff member.",
            )
        )
    except Exception:
        pass

    log_channel = source_channel.guild.get_channel(CLOSED_TICKET_LOG_CHANNEL_ID)
    if log_channel:
        duration = str(
            timedelta(seconds=max(0, closed_at - ticket_row["opened_at"]))
        ).split(".")[0]

        embed = make_embed(
            f"{ticket_row['ticket_id']} ({str(ticket_row['user_id'])[-2:]})",
            "**Ticket closed by a staff member**\n\n"
            f"🟢 **Opened by:** <@{ticket_row['user_id']}> (`{ticket_row['user_id']}`) "
            f"at <t:{ticket_row['opened_at']}:F>\n\n"
            f"🔴 **Closed by:** {closed_by.mention} (`{closed_by.id}`) "
            f"at <t:{closed_at}:F>\n\n"
            f"📁 **Panel:** {ticket_row['category']}\n"
            f"⏱️ **Duration:** {duration}",
        )
        await log_channel.send(embed=embed)

    await source_channel.send(
        embed=make_embed(
            f"{VR_TICK} Ticket Closed",
            "This ticket will be deleted in five seconds.",
        )
    )
    await asyncio.sleep(5)
    await source_channel.delete(reason=f"Ticket closed by {closed_by}")


@bot.command(name="close")
async def close_command(ctx: commands.Context):
    if not await require_modmail_staff(ctx):
        return
    ticket = await require_ticket_channel(ctx)
    if not ticket:
        return
    await close_ticket(ticket, ctx.author, ctx.channel)


async def ask_dm_question(ctx: commands.Context, title: str, prompt: str, timeout: int = 300):
    try:
        dm = ctx.author.dm_channel or await ctx.author.create_dm()
        await dm.send(embed=make_embed(title, prompt))
    except discord.Forbidden:
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Direct Messages Required",
                "Please enable direct messages to complete the scheduling form.",
            )
        )
        return None

    def check(message: discord.Message):
        return message.author.id == ctx.author.id and isinstance(message.channel, discord.DMChannel)

    try:
        response = await bot.wait_for("message", check=check, timeout=timeout)
        return response.content.strip()
    except asyncio.TimeoutError:
        await dm.send(
            embed=make_embed(
                f"{VR_CROSS} Form Expired",
                "The scheduling form expired.",
            )
        )
        return None


def parse_unix_timestamp(value: str):
    match = re.search(r"(\d{10})", value.strip())
    return int(match.group(1)) if match else None


async def update_departure_schedule_message():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    channel = guild.get_channel(DEPARTURE_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return

    cursor.execute("SELECT * FROM flights WHERE status = 'scheduled' ORDER BY start_timestamp ASC")
    flights = cursor.fetchall()

    today = datetime.now(LOCAL_TZ).date()
    today_flights = []
    future_flights = []

    for row in flights:
        flight_date = datetime.fromtimestamp(
            row["start_timestamp"],
            tz=timezone.utc,
        ).astimezone(LOCAL_TZ).date()

        if flight_date == today:
            today_flights.append(row)
        elif flight_date > today:
            future_flights.append(row)

    def lines(rows):
        if not rows:
            return f"{FLIGHT} No flights currently listed."
        return "\n".join(
            f"{FLIGHT} [{row['flight_number']}]({row['event_url']})"
            for row in rows
        )

    now_ts = utc_now_ts()
    embed = make_embed(
        f"{SCHEDULE} Flight Schedule",
        f"-# `LAST UPDATED:` <t:{now_ts}:R>\n\n"
        f"> We are excited to share that **{len(today_flights)}** flight(s) are scheduled for today. "
        "For your convenience, all details may be found in the event cards below.\n\n"
        f"**{INFORMATION} Scheduled Today**\n"
        f"{lines(today_flights)}\n\n"
        f"**{INFORMATION} Scheduled Flights**\n"
        f"{lines(future_flights)}",
    )

    cursor.execute("SELECT value FROM bot_config WHERE key = 'schedule_message_id'")
    row = cursor.fetchone()
    message_id = int(row["value"]) if row and row["value"] else None

    if message_id:
        try:
            message = await channel.fetch_message(message_id)
            await message.edit(embed=embed)
            return
        except Exception:
            pass

    message = await channel.send(embed=embed)
    cursor.execute(
        """
        INSERT INTO bot_config (key, value)
        VALUES ('schedule_message_id', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(message.id),),
    )
    db.commit()


@bot.command(name="schedule")
async def schedule_command(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not is_operations_staff(ctx.author):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Permission Denied",
                "You do not have permission to schedule Vertex Air flights.",
            )
        )
        return

    await ctx.send(
        embed=make_embed(
            f"{MAIL} Scheduling Form Sent",
            "Please check your direct messages and complete the flight scheduling form.",
        )
    )

    flight_number = await ask_dm_question(
        ctx,
        f"{SCHEDULE} Flight Number",
        "Please enter the flight number.",
    )
    if not flight_number:
        return

    timestamp_input = await ask_dm_question(
        ctx,
        f"{SCHEDULE} Flight Timestamp",
        "Please enter the Unix timestamp for the flight.",
    )
    if not timestamp_input:
        return

    start_timestamp = parse_unix_timestamp(timestamp_input)
    if not start_timestamp:
        await ctx.author.send(
            embed=make_embed(
                f"{VR_CROSS} Invalid Timestamp",
                "Please provide a valid ten-digit Unix timestamp.",
            )
        )
        return

    if start_timestamp <= utc_now_ts():
        await ctx.author.send(
            embed=make_embed(
                f"{VR_CROSS} Invalid Timestamp",
                "The scheduled flight time must be in the future.",
            )
        )
        return

    game_link = await ask_dm_question(
        ctx,
        f"{ROBLOX} Game Link",
        "Please enter the Roblox game or private-server link.",
    )
    if not game_link:
        return
    if not game_link.startswith(("http://", "https://")):
        game_link = "https://" + game_link

    route = await ask_dm_question(
        ctx,
        f"{INFORMATION} Flight Route",
        "Please enter the route, for example `LHR → ATH`.",
    )
    if not route:
        return

    aircraft = await ask_dm_question(
        ctx,
        f"{FLIGHT} Aircraft",
        "Please enter the aircraft for this flight.",
    )
    if not aircraft:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await ctx.author.send(
            embed=make_embed(
                f"{VR_CROSS} Scheduling Failed",
                "The configured Vertex Air server could not be found.",
            )
        )
        return

    start_time = datetime.fromtimestamp(start_timestamp, tz=timezone.utc)
    end_time = start_time + timedelta(hours=2)

    event_description = (
        f"**{LOGO} Flight Scheduled**\n\n"
        f"{POINTER} Greetings! A new flight has been scheduled "
        f"**{SCHEDULE} {flight_number}** and the route is "
        f"**{INFORMATION} {route}** onboard our **{aircraft}** "
        f"from **{ROBLOX} [Game Link]({game_link})**.\n\n"
        "If interested, please click **Interested**."
    )

    try:
        event = await guild.create_scheduled_event(
            name=f"{flight_number} | {route}",
            description=event_description,
            start_time=start_time,
            end_time=end_time,
            entity_type=discord.EntityType.external,
            privacy_level=discord.PrivacyLevel.guild_only,
            location=game_link[:100],
            reason=f"Flight scheduled by {ctx.author}",
        )
    except Exception as exc:
        await ctx.author.send(
            embed=make_embed(
                f"{VR_CROSS} Event Creation Failed",
                f"The event could not be created.\n\n`{exc}`",
            )
        )
        return

    event_url = f"https://discord.com/events/{guild.id}/{event.id}"

    cursor.execute(
        """
        INSERT INTO flights (
            event_id, event_url, flight_number, start_timestamp,
            game_link, route, aircraft, scheduled_by_id, created_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled')
        """,
        (
            event.id,
            event_url,
            flight_number,
            start_timestamp,
            game_link,
            route,
            aircraft,
            ctx.author.id,
            utc_now_ts(),
        ),
    )
    db.commit()

    await update_departure_schedule_message()

    await ctx.author.send(
        embed=make_embed(
            f"{VR_TICK} Flight Scheduled",
            f"**Flight:** {flight_number}\n"
            f"**Route:** {route}\n"
            f"**Aircraft:** {aircraft}\n"
            f"**Departure:** <t:{start_timestamp}:F>\n"
            f"**Event:** [Open Event Card]({event_url})",
        )
    )


@bot.command(name="unlock")
async def unlock_command(ctx: commands.Context, *, flight_number: str):
    if not isinstance(ctx.author, discord.Member) or not is_operations_staff(ctx.author):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Permission Denied",
                "You do not have permission to unlock Vertex Air flight servers.",
            )
        )
        return

    cursor.execute(
        """
        SELECT * FROM flights
        WHERE LOWER(flight_number) = LOWER(?)
          AND status = 'scheduled'
        ORDER BY id DESC
        LIMIT 1
        """,
        (flight_number.strip(),),
    )
    flight_row = cursor.fetchone()

    if not flight_row:
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Flight Not Found",
                f"No scheduled flight was found with flight number `{flight_number}`.",
            )
        )
        return

    departure_channel = ctx.guild.get_channel(DEPARTURE_CHANNEL_ID)
    if not isinstance(departure_channel, discord.TextChannel):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Channel Not Found",
                "The configured departure channel could not be found.",
            )
        )
        return

    announcement_embed = make_embed(
        f"{ROBLOX} Server Unlocked",
        (
            f"{POINTER} Greetings! It is with pleasure that I announce that the server "
            f"has been unlocked for passengers to join for flight "
            f"**{FLIGHT} {flight_row['flight_number']}**. "
            f"Please join through **[this link]({flight_row['game_link']})**."
        ),
    )

    ghost_ping = await departure_channel.send(
        content="@everyone",
        allowed_mentions=discord.AllowedMentions(everyone=True),
    )
    try:
        await ghost_ping.delete()
    except Exception:
        pass

    await departure_channel.send(embed=announcement_embed)

    await ctx.send(
        embed=make_embed(
            f"{VR_TICK} Server Unlock Announced",
            f"The server unlock announcement for **{flight_row['flight_number']}** was posted successfully.",
        )
    )


@bot.command(name="lock")
async def lock_command(ctx: commands.Context, *, flight_number: str):
    if not isinstance(ctx.author, discord.Member) or not is_operations_staff(ctx.author):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Permission Denied",
                "You do not have permission to lock Vertex Air flight servers.",
            )
        )
        return

    cursor.execute(
        """
        SELECT * FROM flights
        WHERE LOWER(flight_number) = LOWER(?)
          AND status = 'scheduled'
        ORDER BY id DESC
        LIMIT 1
        """,
        (flight_number.strip(),),
    )
    flight_row = cursor.fetchone()

    if not flight_row:
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Flight Not Found",
                f"No scheduled flight was found with flight number `{flight_number}`.",
            )
        )
        return

    departure_channel = ctx.guild.get_channel(DEPARTURE_CHANNEL_ID)
    if not isinstance(departure_channel, discord.TextChannel):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Channel Not Found",
                "The configured departure channel could not be found.",
            )
        )
        return

    announcement_embed = make_embed(
        f"{INFORMATION} Server Locked",
        (
            f"{POINTER} It is with great pleasure I announce that boarding has begun "
            f"for flight **{FLIGHT} {flight_row['flight_number']}**, and therefore, "
            "the server has been locked for smooth operations."
        ),
    )

    await departure_channel.send(
        content="@here",
        embed=announcement_embed,
        allowed_mentions=discord.AllowedMentions(everyone=True),
    )

    await ctx.send(
        embed=make_embed(
            f"{VR_TICK} Server Lock Announced",
            f"The server lock announcement for **{flight_row['flight_number']}** was posted successfully.",
        )
    )

@bot.command(name="list-events")
async def list_events_command(ctx: commands.Context):
    if not isinstance(ctx.author, discord.Member) or not is_operations_staff(ctx.author):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Permission Denied",
                "You do not have permission to list Vertex Air flight events.",
            )
        )
        return

    departure_channel = ctx.guild.get_channel(DEPARTURE_CHANNEL_ID)

    if not isinstance(departure_channel, discord.TextChannel):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Channel Not Found",
                "The configured departure channel could not be found.",
            )
        )
        return

    # Remove the saved message ID so the bot sends a fresh schedule message.
    cursor.execute(
        "DELETE FROM bot_config WHERE key = 'schedule_message_id'"
    )
    db.commit()

    await update_departure_schedule_message()

    await ctx.send(
        embed=make_embed(
            f"{VR_TICK} Events Listed",
            f"The flight schedule has been posted again in {departure_channel.mention}.",
        )
    )


@refresh_schedule.before_loop
async def before_refresh_schedule():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} | Vertex Air Customer Core online")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if message.guild is not None:
        await bot.process_commands(message)
        return

    if is_banned(message.author.id):
        await add_reaction_safe(message, VR_CROSS)
        try:
            await message.author.send(
                embed=make_embed(
                    f"{VR_CROSS} Connection Refused",
                    f"{POINTER} You are currently unable to open a Vertex Air helpline ticket.",
                )
            )
        except Exception:
            pass
        return

    open_ticket = get_open_ticket_for_user(message.author.id)
    if open_ticket:
        sent = await send_passenger_message_to_ticket(message, open_ticket)
        await add_reaction_safe(message, VR_TICK if sent else VR_CROSS)
        return

    pending_first_messages[message.author.id] = message

    await message.author.send(
        embed=make_embed(
            f"{MAIL} Connecting",
            f"{FLAG} Kalosórisma, thank you for contacting Vertex Air helpline.\n\n"
            f"{POINTER} We appreciate your interest in consulting with us today, "
            "but are you sure you want to create a ticket? Please use the buttons below.",
        ),
        view=TicketConfirmView(message.author.id),
    )


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Missing Information",
                f"Required information is missing: `{error.param.name}`.",
            )
        )
        return

    if isinstance(error, commands.BadArgument):
        await ctx.send(
            embed=make_embed(
                f"{VR_CROSS} Invalid Information",
                "One of the provided values was invalid.",
            )
        )
        return

    await ctx.send(
        embed=make_embed(
            f"{VR_CROSS} Command Error",
            f"`{type(error).__name__}: {error}`",
        )
    )
    raise error


bot.run(TOKEN)
