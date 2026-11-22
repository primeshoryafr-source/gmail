import asyncio
import io
import os
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import config as cfg
import db
from transcript import build_transcript

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True  # required for transcripts (also enable it in the Developer Portal)


class TicketBot(commands.Bot):
    async def setup_hook(self):
        # Persistent views: buttons/dropdowns keep working after a bot restart
        self.add_view(PanelView())
        self.add_view(TicketControlView())
        self.add_dynamic_items(RatingButton)

        guild_id = os.getenv("GUILD_ID")
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            print("✅ Slash commands synced (guild)")
        else:
            await self.tree.sync()
            print("✅ Slash commands synced (global)")

        auto_close.start()


bot = TicketBot(command_prefix="!", intents=intents)
cooldowns: dict[str, float] = {}


# ------------------------------------------------------------------ #
#  Helpers                                                            #
# ------------------------------------------------------------------ #
def pad(n: int) -> str:
    return f"{n:04d}"


def quick(color: int, text: str) -> discord.Embed:
    return discord.Embed(color=color, description=text)


async def reply(interaction: discord.Interaction, text: str, color: int = cfg.COLOR_DANGER):
    if interaction.response.is_done():
        await interaction.followup.send(embed=quick(color, text), ephemeral=True)
    else:
        await interaction.response.send_message(embed=quick(color, text), ephemeral=True)


def cat_of(cat_id: str) -> dict:
    return next((c for c in cfg.CATEGORIES if c["id"] == cat_id), {"emoji": "🎫", "label": "Ticket"})


def is_staff(member: discord.Member, g: dict) -> bool:
    if member.guild_permissions.manage_channels:
        return True
    return bool(g["support_role"]) and any(r.id == g["support_role"] for r in member.roles)


async def ensure_category(guild: discord.Guild, cat_id: str, g: dict, role: Optional[discord.Role] = None):
    """Returns the Discord category for a ticket type. Creates it automatically if it doesn't exist
    (or was deleted). Reuses an existing category with the same name instead of making a duplicate."""
    cat = cat_of(cat_id)
    cats = g.setdefault("categories", {})

    existing = guild.get_channel(cats[cat_id]) if cats.get(cat_id) else None
    if isinstance(existing, discord.CategoryChannel):
        return existing

    name = cat.get("discord_category") or cat["label"]
    found = discord.utils.get(guild.categories, name=name)
    if found:
        cats[cat_id] = found.id
        db.save()
        return found

    role = role or (guild.get_role(g["support_role"]) if g["support_role"] else None)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            embed_links=True, attach_files=True, manage_channels=True, manage_messages=True,
        ),
    }
    if role:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

    category = await guild.create_category(name=name, overwrites=overwrites, reason="Ticket category (auto-created)")
    cats[cat_id] = category.id
    db.save()
    return category


def ticket_embed(t: dict) -> discord.Embed:
    cat = cat_of(t["category"])
    pr = cfg.PRIORITIES[t["priority"]]
    e = discord.Embed(
        color=cfg.COLOR_MAIN,
        title=f"🎫 Ticket #{pad(t['number'])}",
        description=f"**Subject:** {t['subject']}\n\n{t['description']}",
        timestamp=datetime.fromtimestamp(t["created_at"], tz=timezone.utc),
    )
    e.add_field(name="👤 User", value=f"<@{t['user_id']}>")
    e.add_field(name="📂 Category", value=f"{cat['emoji']} {cat['label']}")
    e.add_field(name="⚡ Priority", value=f"{pr['emoji']} {pr['label']}")
    e.add_field(name="🙋 Claimed By", value=f"<@{t['claimed_by']}>" if t["claimed_by"] else "Nobody yet")
    e.set_footer(text=t.get("footer") or "Our team will reply shortly.")
    return e


async def refresh_welcome(channel: discord.TextChannel, t: dict):
    try:
        msg = await channel.fetch_message(t["welcome_id"])
        await msg.edit(embed=ticket_embed(t), view=TicketControlView(claimed=bool(t["claimed_by"])))
    except discord.HTTPException:
        pass  # welcome message was deleted - ignore


async def safe_rename(channel: discord.TextChannel, name: str):
    # Discord limits channel renames to 2 per 10 minutes. We run it in the
    # background so the bot never hangs while waiting on the rate limit.
    try:
        await channel.edit(name=name)
    except discord.HTTPException:
        pass


async def ticket_ctx(interaction: discord.Interaction):
    """Returns (ticket, guild config, is_staff, is_owner) for the current channel, or None."""
    t = db.get_ticket(interaction.channel_id)
    if not t or t["status"] != "open":
        await reply(interaction, "❌ This command/button only works inside an open ticket channel.")
        return None
    g = db.get_guild(interaction.guild_id)
    return t, g, is_staff(interaction.user, g), t["user_id"] == interaction.user.id


# ------------------------------------------------------------------ #
#  Ticket create / claim / close                                      #
# ------------------------------------------------------------------ #
async def create_ticket(interaction: discord.Interaction, category_id: str, subject: str, description: str):
    guild = interaction.guild
    user = interaction.user
    g = db.get_guild(guild.id)

    async def fail(text, color=cfg.COLOR_DANGER):
        await interaction.followup.send(embed=quick(color, text), ephemeral=True)

    role = guild.get_role(g["support_role"]) if g["support_role"] else None
    if not role:
        return await fail("❌ The ticket system hasn't been set up yet. Ask an admin to run `/setup`.")
    if user.id in g["blacklist"]:
        return await fail("🚫 You have been blocked from opening tickets.")

    key = f"{guild.id}:{user.id}"
    left = cooldowns.get(key, 0) + cfg.COOLDOWN_SECONDS - time.time()
    if left > 0:
        return await fail(f"⏳ Please wait {int(left) + 1} more second(s) before opening another ticket.", cfg.COLOR_WARN)

    open_ones = db.open_tickets_of(guild.id, user.id)
    if len(open_ones) >= g["max_tickets"]:
        links = ", ".join(f"<#{t['channel_id']}>" for t in open_ones)
        return await fail(
            f"⚠️ You already have {len(open_ones)} open ticket(s) (limit: {g['max_tickets']}).\n{links}",
            cfg.COLOR_WARN,
        )

    try:
        category = await ensure_category(guild, category_id, g, role)
    except discord.Forbidden:
        return await fail("❌ I can't create the ticket category. Please give me the **Manage Channels** permission.")

    g["counter"] += 1
    number = g["counter"]
    db.save()

    base = dict(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        user: discord.PermissionOverwrite(**base),
        role: discord.PermissionOverwrite(**base, manage_messages=True),
        guild.me: discord.PermissionOverwrite(**base, manage_channels=True, manage_messages=True),
    }

    channel = await guild.create_text_channel(
        name=f"{cfg.PRIORITIES['normal']['emoji']}-{cat_of(category_id).get('prefix', 'ticket')}-{pad(number)}",
        category=category,
        topic=f"Ticket #{pad(number)} | Owner: {user} ({user.id}) | {cat_of(category_id)['label']}",
        overwrites=overwrites,
    )

    t = {
        "channel_id": channel.id,
        "guild_id": guild.id,
        "number": number,
        "user_id": user.id,
        "category": category_id,
        "subject": subject,
        "description": description,
        "priority": "normal",
        "claimed_by": None,
        "status": "open",
        "users": [],
        "created_at": time.time(),
        "last_activity": time.time(),
        "rating": None,
        "notes": [],
        "locked": False,
        "footer": g.get("welcome_message"),
    }

    welcome = await channel.send(
        content=f"<@{user.id}> | <@&{role.id}>",
        embed=ticket_embed(t),
        view=TicketControlView(),
    )
    try:
        await welcome.pin()
    except discord.HTTPException:
        pass
    t["welcome_id"] = welcome.id
    db.add_ticket(t)
    cooldowns[key] = time.time()
    schedule_board(guild.id)

    await fail(f"✅ Your ticket has been created: {channel.mention}", cfg.COLOR_SUCCESS)

    log = guild.get_channel(g["log_channel"]) if g["log_channel"] else None
    if log:
        e = discord.Embed(color=cfg.COLOR_SUCCESS, title="📂 Ticket Opened", timestamp=datetime.now(timezone.utc))
        e.add_field(name="Ticket", value=f"{channel.mention} (#{pad(number)})")
        e.add_field(name="User", value=f"{user.mention} ({user})")
        e.add_field(name="Category", value=cat_of(category_id)["label"])
        e.add_field(name="Subject", value=subject, inline=False)
        try:
            await log.send(embed=e)
        except discord.HTTPException:
            pass


async def dm_user(user_id: int, **kwargs) -> bool:
    """Sends a DM. Returns False if the user has DMs disabled."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
        await user.send(**kwargs)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


async def log_event(guild: discord.Guild, color: int, title: str, text: str):
    g = db.get_guild(guild.id)
    log = guild.get_channel(g["log_channel"]) if g["log_channel"] else None
    if not log:
        return
    try:
        await log.send(embed=discord.Embed(color=color, title=title, description=text, timestamp=datetime.now(timezone.utc)))
    except discord.HTTPException:
        pass


async def claim_ticket(channel: discord.TextChannel, t: dict, member: discord.Member):
    t["claimed_by"] = member.id
    t["claimed_at"] = time.time()
    db.save()
    await refresh_welcome(channel, t)
    await channel.send(
        content=f"<@{t['user_id']}>",
        embed=quick(cfg.COLOR_SUCCESS, f"🙋 {member.mention} has claimed this ticket and will be helping you from here."),
    )
    await dm_user(
        t["user_id"],
        embed=quick(
            cfg.COLOR_SUCCESS,
            f"🙋 Your ticket {channel.mention} in **{channel.guild.name}** was claimed by **{member.display_name}**.",
        ),
    )
    await log_event(
        channel.guild, cfg.COLOR_SUCCESS, "🙋 Ticket Claimed",
        f"{channel.mention} (#{pad(t['number'])}) was claimed by {member.mention}.",
    )
    schedule_board(channel.guild.id)


async def close_ticket(channel: discord.TextChannel, closer, reason: Optional[str] = None):
    reason = reason or "No reason provided"
    t = db.get_ticket(channel.id)
    if not t or t["status"] != "open":
        return

    t["status"] = "closed"
    t["closed_by"] = closer.id
    t["closed_at"] = time.time()
    t["close_reason"] = reason
    db.save()
    schedule_board(channel.guild.id)

    g = db.get_guild(channel.guild.id)
    data, filename, count = await build_transcript(channel, t)

    def make_file():
        return discord.File(io.BytesIO(data), filename=filename)

    info = discord.Embed(color=cfg.COLOR_DANGER, title=f"🔒 Ticket #{pad(t['number'])} Closed", timestamp=datetime.now(timezone.utc))
    info.add_field(name="Owner", value=f"<@{t['user_id']}>")
    info.add_field(name="Closed By", value=f"<@{closer.id}>")
    info.add_field(name="Claimed By", value=f"<@{t['claimed_by']}>" if t["claimed_by"] else "Nobody")
    info.add_field(name="Category", value=cat_of(t["category"])["label"])
    info.add_field(name="Messages", value=str(count))
    info.add_field(name="Reason", value=reason, inline=False)

    log = channel.guild.get_channel(g["log_channel"]) if g["log_channel"] else None
    if log:
        try:
            await log.send(embed=info, file=make_file())
        except discord.HTTPException:
            traceback.print_exc()

    # DM the owner: transcript + rating buttons
    try:
        owner = channel.guild.get_member(t["user_id"]) or await bot.fetch_user(t["user_id"])
        view = discord.ui.View(timeout=None)
        for n in range(1, 6):
            view.add_item(RatingButton(t["guild_id"], t["number"], n))
        await owner.send(
            content=f"Your ticket **#{pad(t['number'])}** ({channel.guild.name}) has been closed.\nPlease rate our support 👇",
            embed=info,
            file=make_file(),
            view=view,
        )
    except (discord.Forbidden, discord.HTTPException):
        pass  # DMs are closed

    try:
        await channel.send(embed=quick(cfg.COLOR_DANGER, f"🔒 Ticket closed. This channel will be deleted in {cfg.DELETE_DELAY_SECONDS} seconds..."))
    except discord.HTTPException:
        pass
    await asyncio.sleep(cfg.DELETE_DELAY_SECONDS)
    try:
        await channel.delete(reason="Ticket closed")
    except discord.HTTPException:
        pass


# ------------------------------------------------------------------ #
#  UI: panel, modal, ticket buttons, rating                           #
# ------------------------------------------------------------------ #
class TicketModal(discord.ui.Modal):
    def __init__(self, category_id: str):
        super().__init__(title=cat_of(category_id)["label"][:45])
        self.category_id = category_id
        c = cat_of(category_id)
        self.subject = discord.ui.TextInput(
            label=c.get("subject_label", "Subject")[:45],
            placeholder=c.get("subject_placeholder"),
            max_length=100,
            required=True,
        )
        self.description = discord.ui.TextInput(
            label=c.get("description_label", "Describe your issue in detail")[:45],
            placeholder=c.get("description_placeholder"),
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=True,
        )
        self.add_item(self.subject)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await create_ticket(interaction, self.category_id, self.subject.value, self.description.value)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = "❌ Couldn't create the ticket. Please check the bot's permissions."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=c["label"], value=c["id"], description=c["description"], emoji=c["emoji"])
            for c in cfg.CATEGORIES
        ]
        super().__init__(custom_id="ticket_category", placeholder="📩 Choose a ticket category...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TicketModal(self.values[0]))
        # Reset the dropdown so the same option can be picked again
        try:
            await interaction.message.edit(view=PanelView())
        except discord.HTTPException:
            pass


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(CategorySelect())


class CloseConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.button(label="Yes, close it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=quick(cfg.COLOR_WARN, "⏳ Closing the ticket..."), view=None)
        await close_ticket(interaction.channel, interaction.user)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=quick(cfg.COLOR_SUCCESS, "✅ Cancelled."), view=None)


class TicketControlView(discord.ui.View):
    def __init__(self, claimed: bool = False):
        super().__init__(timeout=None)
        if claimed:
            self.claim_button.disabled = True
            self.claim_button.label = "Claimed"

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await ticket_ctx(interaction)
        if not ctx:
            return
        t, g, staff, owner = ctx
        if not staff and not (cfg.ALLOW_OWNER_CLOSE and owner):
            return await reply(interaction, "❌ You don't have permission to close this ticket.")
        await interaction.response.send_message(
            embed=quick(cfg.COLOR_WARN, "❓ Are you sure you want to close this ticket?"),
            view=CloseConfirmView(),
            ephemeral=True,
        )

    @discord.ui.button(label="Claim", emoji="🙋", style=discord.ButtonStyle.success, custom_id="ticket_claim")
    async def claim_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await ticket_ctx(interaction)
        if not ctx:
            return
        t, g, staff, owner = ctx
        if not staff:
            return await reply(interaction, "❌ Only support staff can claim tickets.")
        if t["claimed_by"]:
            return await reply(interaction, f"❌ This ticket is already claimed by <@{t['claimed_by']}>.")
        await interaction.response.defer()
        await claim_ticket(interaction.channel, t, interaction.user)

    @discord.ui.button(label="Transcript", emoji="📄", style=discord.ButtonStyle.secondary, custom_id="ticket_transcript")
    async def transcript_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        ctx = await ticket_ctx(interaction)
        if not ctx:
            return
        t, g, staff, owner = ctx
        if not staff:
            return await reply(interaction, "❌ Only support staff can generate transcripts.")
        await interaction.response.defer(ephemeral=True)
        data, filename, count = await build_transcript(interaction.channel, t)
        await interaction.followup.send(
            content=f"📄 Transcript of {count} messages:",
            file=discord.File(io.BytesIO(data), filename=filename),
            ephemeral=True,
        )


class RatingButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"rate:(?P<guild>\d+):(?P<number>\d+):(?P<stars>[1-5])",
):
    def __init__(self, guild_id: int, number: int, stars: int):
        super().__init__(
            discord.ui.Button(
                label=str(stars),
                emoji="⭐",
                style=discord.ButtonStyle.secondary,
                custom_id=f"rate:{guild_id}:{number}:{stars}",
            )
        )
        self.guild_id = guild_id
        self.number = number
        self.stars = stars

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match, /):
        return cls(int(match["guild"]), int(match["number"]), int(match["stars"]))

    async def callback(self, interaction: discord.Interaction):
        t = db.find_ticket(self.guild_id, self.number)
        if not t:
            return await interaction.response.edit_message(view=None)
        if t.get("rating"):
            return await interaction.response.send_message("You have already rated this ticket.", ephemeral=True)

        t["rating"] = self.stars
        db.save()
        await interaction.response.edit_message(content=f"Thanks for your feedback! {'⭐' * self.stars}", view=None)

        g = db.get_guild(self.guild_id)
        guild = bot.get_guild(self.guild_id)
        log = guild.get_channel(g["log_channel"]) if guild and g["log_channel"] else None
        if log:
            e = discord.Embed(
                color=cfg.COLOR_WARN,
                title=f"⭐ Rating — Ticket #{pad(t['number'])}",
                description=(
                    f"{'⭐' * self.stars} ({self.stars}/5)\n"
                    f"User: <@{t['user_id']}>\n"
                    f"Handled by: {'<@' + str(t['claimed_by']) + '>' if t['claimed_by'] else 'Nobody'}"
                ),
                timestamp=datetime.now(timezone.utc),
            )
            try:
                await log.send(embed=e)
            except discord.HTTPException:
                pass


# ------------------------------------------------------------------ #
#  /setup                                                             #
# ------------------------------------------------------------------ #
@bot.tree.command(name="setup", description="Set up the ticket system and send the ticket panel")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    panel_channel="Channel where the ticket panel will be posted",
    support_role="Support staff role (can see and answer all tickets)",
    log_channel="Channel for logs and transcripts (empty = I create a private #ticket-logs)",
    max_tickets="Max open tickets per user",
)
async def setup_cmd(
    interaction: discord.Interaction,
    panel_channel: discord.TextChannel,
    support_role: discord.Role,
    log_channel: Optional[discord.TextChannel] = None,
    max_tickets: Optional[app_commands.Range[int, 1, 10]] = None,
):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    g = db.get_guild(guild.id)
    g["support_role"] = support_role.id
    if max_tickets:
        g["max_tickets"] = max_tickets

    try:
        # One Discord category per ticket type, created automatically
        lines = []
        for c in cfg.CATEGORIES:
            category = await ensure_category(guild, c["id"], g, support_role)
            lines.append(f"{c['emoji']} **{c['label']}** → {category.mention if hasattr(category, 'mention') else category.name}")

        # Log channel: use the given one, keep the old one, or create a private one
        if log_channel is None:
            old = guild.get_channel(g["log_channel"]) if g["log_channel"] else None
            if isinstance(old, discord.TextChannel):
                log_channel = old
            else:
                log_channel = await guild.create_text_channel(
                    "ticket-logs",
                    overwrites={
                        guild.default_role: discord.PermissionOverwrite(view_channel=False),
                        support_role: discord.PermissionOverwrite(view_channel=True, read_message_history=True),
                        guild.me: discord.PermissionOverwrite(
                            view_channel=True, send_messages=True, embed_links=True, attach_files=True
                        ),
                    },
                    reason="Ticket log channel (auto-created)",
                )
        g["log_channel"] = log_channel.id
        db.save()

        await panel_channel.send(embed=build_panel_embed(guild.name), view=PanelView())
    except discord.Forbidden:
        return await reply(
            interaction,
            "❌ I'm missing permissions. Give me **Manage Channels**, **Manage Roles**, **View Channels** and "
            f"**Send Messages** (also in {panel_channel.mention}), then run `/setup` again.",
        )

    await reply(
        interaction,
        "✅ Setup complete!\n\n"
        + "\n".join(lines)
        + f"\n\n📜 Logs → {log_channel.mention}\n📩 Panel → {panel_channel.mention}",
        cfg.COLOR_SUCCESS,
    )


# ------------------------------------------------------------------ #
#  /ticket group                                                      #
# ------------------------------------------------------------------ #
ticket_group = app_commands.Group(name="ticket", description="Ticket management commands", guild_only=True)


async def staff_ticket(interaction: discord.Interaction):
    """Common check for staff-only ticket commands. Returns (ticket, guild config) or None."""
    ctx = await ticket_ctx(interaction)
    if not ctx:
        return None
    t, g, staff, _ = ctx
    if not staff:
        await reply(interaction, "❌ This command is for support staff only.")
        return None
    return t, g


@ticket_group.command(name="add", description="Add a user to this ticket")
@app_commands.describe(user="User to add")
async def ticket_add(interaction: discord.Interaction, user: discord.Member):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    await interaction.channel.set_permissions(
        user, view_channel=True, send_messages=True, read_message_history=True, attach_files=True
    )
    if user.id not in t["users"]:
        t["users"].append(user.id)
    db.save()
    await interaction.response.send_message(embed=quick(cfg.COLOR_SUCCESS, f"➕ {user.mention} was added to the ticket."))


@ticket_group.command(name="remove", description="Remove a user from this ticket")
@app_commands.describe(user="User to remove")
async def ticket_remove(interaction: discord.Interaction, user: discord.Member):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    if user.id == t["user_id"]:
        return await reply(interaction, "❌ You can't remove the ticket owner.")
    await interaction.channel.set_permissions(user, overwrite=None)
    t["users"] = [u for u in t["users"] if u != user.id]
    db.save()
    await interaction.response.send_message(embed=quick(cfg.COLOR_WARN, f"➖ {user.mention} was removed from the ticket."))


@ticket_group.command(name="rename", description="Rename this ticket channel")
@app_commands.describe(name="New name")
async def ticket_rename(interaction: discord.Interaction, name: app_commands.Range[str, 1, 80]):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    clean = re.sub(r"[^a-z0-9\-_ ]", "", name.lower()).strip().replace(" ", "-") or f"ticket-{pad(t['number'])}"
    await interaction.response.send_message(embed=quick(cfg.COLOR_SUCCESS, f"✏️ Renaming the ticket to: **{clean}**"))
    asyncio.create_task(safe_rename(interaction.channel, f"{cfg.PRIORITIES[t['priority']]['emoji']}-{clean}"))


@ticket_group.command(name="priority", description="Set the ticket priority")
@app_commands.describe(level="Priority level")
@app_commands.choices(
    level=[app_commands.Choice(name=f"{p['emoji']} {p['label']}", value=k) for k, p in cfg.PRIORITIES.items()]
)
async def ticket_priority(interaction: discord.Interaction, level: app_commands.Choice[str]):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    t["priority"] = level.value
    db.save()
    schedule_board(interaction.guild_id)
    p = cfg.PRIORITIES[level.value]
    await interaction.response.send_message(embed=quick(cfg.COLOR_SUCCESS, f"⚡ Priority set to {p['emoji']} **{p['label']}**"))
    await refresh_welcome(interaction.channel, t)
    base = re.sub(r"^[^\w]+", "", interaction.channel.name) or f"ticket-{pad(t['number'])}"
    asyncio.create_task(safe_rename(interaction.channel, f"{p['emoji']}-{base}"))


@ticket_group.command(name="claim", description="Claim this ticket")
async def ticket_claim(interaction: discord.Interaction):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    if t["claimed_by"]:
        return await reply(interaction, f"❌ This ticket is already claimed by <@{t['claimed_by']}>.")
    await reply(interaction, "✅ Claimed!", cfg.COLOR_SUCCESS)
    await claim_ticket(interaction.channel, t, interaction.user)


@ticket_group.command(name="unclaim", description="Remove your claim from this ticket")
async def ticket_unclaim(interaction: discord.Interaction):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    if not t["claimed_by"]:
        return await reply(interaction, "❌ This ticket isn't claimed.")
    if t["claimed_by"] != interaction.user.id and not interaction.user.guild_permissions.administrator:
        return await reply(interaction, "❌ Only the staff member who claimed it (or an admin) can unclaim this ticket.")
    t["claimed_by"] = None
    db.save()
    schedule_board(interaction.guild_id)
    await log_event(
        interaction.guild, cfg.COLOR_WARN, "🔓 Ticket Unclaimed",
        f"{interaction.channel.mention} (#{pad(t['number'])}) was unclaimed by {interaction.user.mention}.",
    )
    await refresh_welcome(interaction.channel, t)
    await interaction.response.send_message(embed=quick(cfg.COLOR_WARN, "🔓 Ticket unclaimed."))


@ticket_group.command(name="close", description="Close this ticket")
@app_commands.describe(reason="Reason for closing")
async def ticket_close(interaction: discord.Interaction, reason: Optional[app_commands.Range[str, 1, 200]] = None):
    ctx = await ticket_ctx(interaction)
    if not ctx:
        return
    t, g, staff, owner = ctx
    if not staff and not (cfg.ALLOW_OWNER_CLOSE and owner):
        return await reply(interaction, "❌ You don't have permission to close this ticket.")
    await interaction.response.send_message(embed=quick(cfg.COLOR_WARN, "⏳ Closing the ticket and generating the transcript..."))
    await close_ticket(interaction.channel, interaction.user, reason)


@ticket_group.command(name="transcript", description="Generate a transcript of this ticket")
async def ticket_transcript(interaction: discord.Interaction):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    await interaction.response.defer(ephemeral=True)
    data, filename, count = await build_transcript(interaction.channel, t)
    await interaction.followup.send(
        content=f"📄 Transcript of {count} messages:",
        file=discord.File(io.BytesIO(data), filename=filename),
        ephemeral=True,
    )


# ------------------------------------------------------------------ #
#  Extra helpers                                                      #
# ------------------------------------------------------------------ #
def ts(t: float, style: str = "R") -> str:
    """Discord timestamp: <t:...:R> => '2 hours ago'"""
    return f"<t:{int(t)}:{style}>"


def build_panel_embed(guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        color=cfg.COLOR_MAIN,
        title="🎫 Support Tickets",
        description=(
            "Need help? Pick a category from the dropdown below and tell us about your issue.\n\n"
            + "\n".join(f"{c['emoji']} **{c['label']}** — {c['description']}" for c in cfg.CATEGORIES)
        ),
    )
    embed.set_footer(text=guild_name)
    return embed


async def require_staff(interaction: discord.Interaction):
    """Server-level staff check (works outside ticket channels too). Returns the guild config or None."""
    g = db.get_guild(interaction.guild_id)
    if not is_staff(interaction.user, g):
        await reply(interaction, "❌ This command is for support staff only.")
        return None
    return g


# ------------------------------------------------------------------ #
#  Extra /ticket commands                                             #
# ------------------------------------------------------------------ #
@ticket_group.command(name="info", description="Show full details about this ticket")
async def ticket_info(interaction: discord.Interaction):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    cat = cat_of(t["category"])
    pr = cfg.PRIORITIES[t["priority"]]
    e = discord.Embed(color=cfg.COLOR_MAIN, title=f"ℹ️ Ticket #{pad(t['number'])}")
    e.add_field(name="👤 Owner", value=f"<@{t['user_id']}>")
    e.add_field(name="📂 Category", value=f"{cat['emoji']} {cat['label']}")
    e.add_field(name="⚡ Priority", value=f"{pr['emoji']} {pr['label']}")
    e.add_field(name="🙋 Claimed By", value=f"<@{t['claimed_by']}>" if t["claimed_by"] else "Nobody yet")
    e.add_field(name="🕒 Opened", value=ts(t["created_at"]))
    e.add_field(name="💬 Last Activity", value=ts(t["last_activity"]))
    e.add_field(name="🔒 Locked", value="Yes" if t.get("locked") else "No")
    e.add_field(name="📝 Notes", value=str(len(t.get("notes", []))))
    e.add_field(name="👥 Added Users", value=", ".join(f"<@{u}>" for u in t["users"]) or "None", inline=False)
    e.add_field(name="Subject", value=t["subject"], inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@ticket_group.command(name="note", description="Save a private staff note (not visible to the user)")
@app_commands.describe(text="Note text")
async def ticket_note(interaction: discord.Interaction, text: app_commands.Range[str, 1, 500]):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    t.setdefault("notes", []).append({"by": interaction.user.id, "text": text, "at": time.time()})
    db.save()
    await reply(interaction, f"📝 Note saved (#{len(t['notes'])}). View all notes with `/ticket notes`.", cfg.COLOR_SUCCESS)


@ticket_group.command(name="notes", description="View all staff notes for this ticket")
async def ticket_notes(interaction: discord.Interaction):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    notes = t.get("notes", [])
    if not notes:
        return await reply(interaction, "There are no notes on this ticket yet.", cfg.COLOR_MAIN)
    text = "\n\n".join(f"**#{i}** {ts(n['at'])} — <@{n['by']}>\n{n['text']}" for i, n in enumerate(notes, 1))
    await interaction.response.send_message(
        embed=discord.Embed(color=cfg.COLOR_MAIN, title="📝 Staff Notes", description=text[:4000]),
        ephemeral=True,
    )


@ticket_group.command(name="move", description="Change the ticket category")
@app_commands.describe(category="New category")
@app_commands.choices(
    category=[app_commands.Choice(name=f"{c['emoji']} {c['label']}", value=c["id"]) for c in cfg.CATEGORIES]
)
async def ticket_move(interaction: discord.Interaction, category: app_commands.Choice[str]):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, g = ctx
    await interaction.response.defer()
    try:
        target = await ensure_category(interaction.guild, category.value, g)
        await interaction.channel.edit(category=target, sync_permissions=False)
    except discord.HTTPException:
        return await interaction.followup.send(
            embed=quick(cfg.COLOR_DANGER, "❌ I couldn't move this channel. Check my **Manage Channels** permission."),
            ephemeral=True,
        )
    t["category"] = category.value
    db.save()
    schedule_board(interaction.guild_id)
    await interaction.followup.send(embed=quick(cfg.COLOR_SUCCESS, f"📂 Ticket moved to **{category.name}**."))
    await refresh_welcome(interaction.channel, t)


@ticket_group.command(name="transfer", description="Transfer this ticket to another staff member")
@app_commands.describe(staff="New staff member")
async def ticket_transfer(interaction: discord.Interaction, staff: discord.Member):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, g = ctx
    if staff.bot or not is_staff(staff, g):
        return await reply(interaction, "❌ That member isn't support staff.")
    t["claimed_by"] = staff.id
    t["claimed_at"] = time.time()
    db.save()
    schedule_board(interaction.guild_id)
    await log_event(
        interaction.guild, cfg.COLOR_MAIN, "🔁 Ticket Transferred",
        f"{interaction.channel.mention} (#{pad(t['number'])}): {interaction.user.mention} → {staff.mention}",
    )
    await interaction.response.send_message(
        embed=quick(cfg.COLOR_SUCCESS, f"🔁 {interaction.user.mention} transferred this ticket to {staff.mention}.")
    )
    await refresh_welcome(interaction.channel, t)


async def set_ticket_lock(interaction: discord.Interaction, locked: bool):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    await interaction.response.defer()
    for uid in [t["user_id"], *t["users"]]:
        member = interaction.guild.get_member(uid)
        if not member:
            continue
        ow = interaction.channel.overwrites_for(member)
        ow.send_messages = False if locked else True
        await interaction.channel.set_permissions(member, overwrite=ow)
    t["locked"] = locked
    db.save()
    text = "🔒 Ticket locked. The user can no longer send messages." if locked else "🔓 Ticket unlocked."
    await interaction.followup.send(embed=quick(cfg.COLOR_WARN if locked else cfg.COLOR_SUCCESS, text))


@ticket_group.command(name="lock", description="Stop the user from sending messages in this ticket")
async def ticket_lock(interaction: discord.Interaction):
    await set_ticket_lock(interaction, True)


@ticket_group.command(name="unlock", description="Unlock this ticket")
async def ticket_unlock(interaction: discord.Interaction):
    await set_ticket_lock(interaction, False)


@ticket_group.command(name="slowmode", description="Set slowmode in this ticket")
@app_commands.describe(seconds="Seconds (0 = off, max 21600)")
async def ticket_slowmode(interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    await interaction.channel.edit(slowmode_delay=seconds)
    text = f"🐢 Slowmode set to **{seconds}s**." if seconds else "⚡ Slowmode turned off."
    await interaction.response.send_message(embed=quick(cfg.COLOR_SUCCESS, text))


@ticket_group.command(name="ping", description="Send the ticket owner a reminder")
async def ticket_ping(interaction: discord.Interaction):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    await interaction.response.send_message(
        content=f"<@{t['user_id']}>",
        embed=quick(cfg.COLOR_WARN, f"🔔 {interaction.user.mention} is waiting for your reply!"),
    )
    await dm_user(
        t["user_id"],
        content=f"🔔 Staff in **{interaction.guild.name}** is waiting for your reply in {interaction.channel.mention}.",
    )


class CloseRequestView(discord.ui.View):
    def __init__(self, owner_id: int, reason: Optional[str]):
        super().__init__(timeout=3600)
        self.owner_id = owner_id
        self.reason = reason

    def _allowed(self, interaction: discord.Interaction) -> bool:
        g = db.get_guild(interaction.guild_id)
        return interaction.user.id == self.owner_id or is_staff(interaction.user, g)

    @discord.ui.button(label="Yes, close it", emoji="✅", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._allowed(interaction):
            return await reply(interaction, "❌ This button is only for the ticket owner or staff.")
        await interaction.response.edit_message(content=None, embed=quick(cfg.COLOR_WARN, "⏳ Closing the ticket..."), view=None)
        await close_ticket(interaction.channel, interaction.user, self.reason or "Close request accepted")

    @discord.ui.button(label="No, keep it open", emoji="❌", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._allowed(interaction):
            return await reply(interaction, "❌ This button is only for the ticket owner or staff.")
        await interaction.response.edit_message(
            content=None, embed=quick(cfg.COLOR_SUCCESS, "✅ Okay, the ticket will stay open."), view=None
        )


@ticket_group.command(name="closerequest", description="Ask the user whether the ticket can be closed")
@app_commands.describe(reason="Reason (optional)")
async def ticket_closerequest(interaction: discord.Interaction, reason: Optional[app_commands.Range[str, 1, 200]] = None):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    text = f"{interaction.user.mention} believes your issue has been resolved. Can we close this ticket?"
    if reason:
        text += f"\n**Reason:** {reason}"
    await interaction.response.send_message(
        content=f"<@{t['user_id']}>",
        embed=quick(cfg.COLOR_WARN, text),
        view=CloseRequestView(t["user_id"], reason),
    )


@ticket_group.command(name="list", description="List all open tickets")
async def ticket_list(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    open_t = sorted((t for t in db.guild_tickets(interaction.guild_id) if t["status"] == "open"), key=lambda t: t["number"])
    if not open_t:
        return await reply(interaction, "✅ There are no open tickets right now.", cfg.COLOR_SUCCESS)
    lines = [
        f"{cfg.PRIORITIES[t['priority']]['emoji']} <#{t['channel_id']}> — <@{t['user_id']}> • "
        f"{cat_of(t['category'])['label']} • {('<@' + str(t['claimed_by']) + '>') if t['claimed_by'] else 'unclaimed'}"
        for t in open_t[:25]
    ]
    if len(open_t) > 25:
        lines.append(f"… and {len(open_t) - 25} more tickets")
    await interaction.response.send_message(
        embed=discord.Embed(color=cfg.COLOR_MAIN, title=f"📋 Open Tickets ({len(open_t)})", description="\n".join(lines)),
        ephemeral=True,
    )


@ticket_group.command(name="history", description="View a user's previous tickets")
@app_commands.describe(user="User")
async def ticket_history(interaction: discord.Interaction, user: discord.User):
    if not await require_staff(interaction):
        return
    mine = sorted((t for t in db.guild_tickets(interaction.guild_id) if t["user_id"] == user.id), key=lambda t: -t["number"])
    if not mine:
        return await reply(interaction, f"{user.mention} hasn't opened any tickets yet.", cfg.COLOR_MAIN)
    lines = [
        f"**#{pad(t['number'])}** • {cat_of(t['category'])['label']} • "
        f"{'🟢 Open' if t['status'] == 'open' else '🔴 Closed'} • {ts(t['created_at'])}"
        + (f" • {'⭐' * t['rating']}" if t.get("rating") else "")
        for t in mine[:10]
    ]
    e = discord.Embed(color=cfg.COLOR_MAIN, title=f"🗂️ Ticket history for {user} ({len(mine)} total)", description="\n".join(lines))
    await interaction.response.send_message(embed=e, ephemeral=True)


bot.tree.add_command(ticket_group)


# ------------------------------------------------------------------ #
#  /tag — canned responses (staff)                                    #
# ------------------------------------------------------------------ #
tag_group = app_commands.Group(name="tag", description="Ready-made replies (tags)", guild_only=True)


def norm_tag(name: str) -> str:
    return re.sub(r"[^a-z0-9\-_]", "", name.strip().lower().replace(" ", "-"))


async def tag_autocomplete(interaction: discord.Interaction, current: str):
    g = db.get_guild(interaction.guild_id)
    return [app_commands.Choice(name=n, value=n) for n in g["tags"] if current.lower() in n][:25]


@tag_group.command(name="create", description="Create a new tag")
@app_commands.describe(name="Tag name", content="Tag text")
async def tag_create(interaction: discord.Interaction, name: app_commands.Range[str, 1, 32], content: app_commands.Range[str, 1, 1500]):
    g = await require_staff(interaction)
    if not g:
        return
    key = norm_tag(name)
    if not key:
        return await reply(interaction, "❌ The name needs at least one letter or number.")
    if key in g["tags"]:
        return await reply(interaction, f"❌ A tag named `{key}` already exists. Use `/tag edit` instead.")
    g["tags"][key] = {"content": content, "by": interaction.user.id}
    db.save()
    await reply(interaction, f"✅ Tag `{key}` created. Use it with `/tag use {key}`.", cfg.COLOR_SUCCESS)


@tag_group.command(name="edit", description="Edit a tag's text")
@app_commands.describe(name="Tag name", content="New text")
@app_commands.autocomplete(name=tag_autocomplete)
async def tag_edit(interaction: discord.Interaction, name: str, content: app_commands.Range[str, 1, 1500]):
    g = await require_staff(interaction)
    if not g:
        return
    key = norm_tag(name)
    if key not in g["tags"]:
        return await reply(interaction, f"❌ There's no tag named `{key}`.")
    g["tags"][key]["content"] = content
    db.save()
    await reply(interaction, f"✏️ Tag `{key}` updated.", cfg.COLOR_SUCCESS)


@tag_group.command(name="use", description="Send a tag in this channel")
@app_commands.describe(name="Tag name")
@app_commands.autocomplete(name=tag_autocomplete)
async def tag_use(interaction: discord.Interaction, name: str):
    g = await require_staff(interaction)
    if not g:
        return
    key = norm_tag(name)
    tag = g["tags"].get(key)
    if not tag:
        return await reply(interaction, f"❌ There's no tag named `{key}`. Check `/tag list`.")
    e = discord.Embed(color=cfg.COLOR_MAIN, description=tag["content"])
    e.set_footer(text=f"Tag: {key} • {interaction.user}")
    await interaction.response.send_message(embed=e)


@tag_group.command(name="delete", description="Delete a tag")
@app_commands.describe(name="Tag name")
@app_commands.autocomplete(name=tag_autocomplete)
async def tag_delete(interaction: discord.Interaction, name: str):
    g = await require_staff(interaction)
    if not g:
        return
    key = norm_tag(name)
    if g["tags"].pop(key, None) is None:
        return await reply(interaction, f"❌ There's no tag named `{key}`.")
    db.save()
    await reply(interaction, f"🗑️ Tag `{key}` deleted.", cfg.COLOR_SUCCESS)


@tag_group.command(name="list", description="List all tags")
async def tag_list(interaction: discord.Interaction):
    g = await require_staff(interaction)
    if not g:
        return
    if not g["tags"]:
        return await reply(interaction, "There are no tags yet. Create one with `/tag create`.", cfg.COLOR_MAIN)
    await reply(interaction, "🏷️ " + ", ".join(f"`{n}`" for n in sorted(g["tags"])), cfg.COLOR_MAIN)


bot.tree.add_command(tag_group)


# ------------------------------------------------------------------ #
#  /config — change settings (admin)                                  #
# ------------------------------------------------------------------ #
config_group = app_commands.Group(
    name="config",
    description="Ticket system settings",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)


@config_group.command(name="view", description="View the current settings")
async def config_view(interaction: discord.Interaction):
    g = db.get_guild(interaction.guild_id)
    e = discord.Embed(color=cfg.COLOR_MAIN, title="⚙️ Ticket Settings")
    cats = g.get("categories", {})
    e.add_field(
        name="📁 Ticket Categories",
        value="\n".join(
            f"{c['emoji']} {c['label']} → " + (f"<#{cats[c['id']]}>" if cats.get(c["id"]) else "not created yet")
            for c in cfg.CATEGORIES
        ),
        inline=False,
    )
    e.add_field(name="🛡️ Support Role", value=f"<@&{g['support_role']}>" if g["support_role"] else "Not set")
    e.add_field(name="📜 Log Channel", value=f"<#{g['log_channel']}>" if g["log_channel"] else "Not set")
    e.add_field(name="🎟️ Max Tickets/User", value=str(g["max_tickets"]))
    e.add_field(name="⏰ Auto-close", value=f"{g['auto_close_hours'] or cfg.AUTO_CLOSE_HOURS} hours")
    e.add_field(name="🚫 Blacklisted", value=str(len(g["blacklist"])))
    e.add_field(name="🏷️ Tags", value=str(len(g["tags"])))
    e.add_field(name="🔢 Total Tickets", value=str(g["counter"]))
    e.add_field(name="👋 Welcome Footer", value=g["welcome_message"] or "Default", inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@config_group.command(name="set", description="Change settings (fill in only what you want to change)")
@app_commands.describe(
    support_role="Support staff role",
    log_channel="Channel for logs and transcripts",
    max_tickets="Max open tickets per user",
    auto_close_hours="Auto-close tickets after this many hours of inactivity",
    welcome_message="Ticket embed footer text (type 'default' to reset)",
)
async def config_set(
    interaction: discord.Interaction,
    support_role: Optional[discord.Role] = None,
    log_channel: Optional[discord.TextChannel] = None,
    max_tickets: Optional[app_commands.Range[int, 1, 10]] = None,
    auto_close_hours: Optional[app_commands.Range[int, 1, 720]] = None,
    welcome_message: Optional[app_commands.Range[str, 1, 200]] = None,
):
    g = db.get_guild(interaction.guild_id)
    changed = []
    if support_role:
        g["support_role"] = support_role.id
        changed.append(f"🛡️ Support role → {support_role.mention}")
    if log_channel:
        g["log_channel"] = log_channel.id
        changed.append(f"📜 Log channel → {log_channel.mention}")
    if max_tickets:
        g["max_tickets"] = max_tickets
        changed.append(f"🎟️ Max tickets → {max_tickets}")
    if auto_close_hours:
        g["auto_close_hours"] = auto_close_hours
        changed.append(f"⏰ Auto-close → {auto_close_hours} hours")
    if welcome_message:
        g["welcome_message"] = None if welcome_message.lower() == "default" else welcome_message
        changed.append(f"👋 Welcome footer → {g['welcome_message'] or 'Default'}")
    if not changed:
        return await reply(interaction, "❌ Fill in at least one option.", cfg.COLOR_WARN)
    db.save()
    await reply(interaction, "✅ Settings updated:\n" + "\n".join(changed), cfg.COLOR_SUCCESS)


bot.tree.add_command(config_group)


# ------------------------------------------------------------------ #
#  /sendpanel, /staffstats, /ping, /help                              #
# ------------------------------------------------------------------ #
@bot.tree.command(name="sendpanel", description="Send the ticket panel again (after /setup)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel to post the panel in (empty = current channel)")
async def sendpanel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    g = db.get_guild(interaction.guild_id)
    if not (g["support_role"] and g["log_channel"]):
        return await reply(interaction, "❌ Run `/setup` first.")
    target = channel or interaction.channel
    await target.send(embed=build_panel_embed(interaction.guild.name), view=PanelView())
    await reply(interaction, f"✅ Panel sent to {target.mention}.", cfg.COLOR_SUCCESS)


@bot.tree.command(name="staffstats", description="View ticket stats for a staff member")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
@app_commands.describe(member="Staff member (empty = yourself)")
async def staffstats(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    if not await require_staff(interaction):
        return
    member = member or interaction.user
    mine = [t for t in db.guild_tickets(interaction.guild_id) if t["claimed_by"] == member.id]
    closed = [t for t in mine if t["status"] == "closed"]
    ratings = [t["rating"] for t in mine if t.get("rating")]
    avg = f"{sum(ratings) / len(ratings):.2f}" if ratings else "N/A"
    e = discord.Embed(color=cfg.COLOR_MAIN, title=f"📈 Stats for {member.display_name}")
    e.set_thumbnail(url=member.display_avatar.url)
    e.add_field(name="Claimed", value=str(len(mine)))
    e.add_field(name="Closed", value=str(len(closed)))
    e.add_field(name="Open", value=str(len(mine) - len(closed)))
    e.add_field(name="⭐ Avg Rating", value=f"{avg} ({len(ratings)} ratings)")
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction: discord.Interaction):
    await reply(interaction, f"🏓 Pong! **{round(bot.latency * 1000)}ms**", cfg.COLOR_SUCCESS)


@bot.tree.command(name="help", description="List all commands")
async def help_cmd(interaction: discord.Interaction):
    e = discord.Embed(color=cfg.COLOR_MAIN, title="📖 Ticket Bot — Commands")
    e.add_field(
        name="👤 Users",
        value="Open a ticket from the panel dropdown\n`/ticket close` — close your own ticket\n`/ping` — bot latency",
        inline=False,
    )
    e.add_field(
        name="🛡️ Staff — inside a ticket",
        value=(
            "`/ticket add` `remove` `rename` `priority` `claim` `unclaim` `transfer`\n"
            "`/ticket info` `note` `notes` `move` `lock` `unlock` `slowmode`\n"
            "`/ticket ping` `update` `closerequest` `close` `transcript`"
        ),
        inline=False,
    )
    e.add_field(
        name="🛡️ Staff — anywhere",
        value=(
            "`/ticket list` `/ticket history` `/tag create|edit|use|delete|list` `/staffstats` `/stats`\n"
            "`/autoreply add|remove|toggle|list` — keyword auto-replies"
        ),
        inline=False,
    )
    e.add_field(
        name="👑 Admin",
        value="`/setup` `/sendpanel` `/config view|set` `/board set|refresh|remove` `/blacklist add|remove|list`",
        inline=False,
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


# ------------------------------------------------------------------ #
#  /blacklist group                                                   #
# ------------------------------------------------------------------ #
blacklist_group = app_commands.Group(
    name="blacklist",
    description="Block or unblock users from opening tickets",
    guild_only=True,
    default_permissions=discord.Permissions(manage_guild=True),
)


@blacklist_group.command(name="add", description="Blacklist a user")
async def blacklist_add(interaction: discord.Interaction, user: discord.User):
    g = db.get_guild(interaction.guild_id)
    if user.id not in g["blacklist"]:
        g["blacklist"].append(user.id)
    db.save()
    await reply(interaction, f"🚫 {user.mention} has been blacklisted.", cfg.COLOR_SUCCESS)


@blacklist_group.command(name="remove", description="Remove a user from the blacklist")
async def blacklist_remove(interaction: discord.Interaction, user: discord.User):
    g = db.get_guild(interaction.guild_id)
    g["blacklist"] = [u for u in g["blacklist"] if u != user.id]
    db.save()
    await reply(interaction, f"✅ {user.mention} has been removed from the blacklist.", cfg.COLOR_SUCCESS)


@blacklist_group.command(name="list", description="View blacklisted users")
async def blacklist_list(interaction: discord.Interaction):
    g = db.get_guild(interaction.guild_id)
    text = "\n".join(f"<@{u}> ({u})" for u in g["blacklist"]) or "No blacklisted users."
    await reply(interaction, text, cfg.COLOR_MAIN)


bot.tree.add_command(blacklist_group)


# ------------------------------------------------------------------ #
#  /stats                                                             #
# ------------------------------------------------------------------ #
@bot.tree.command(name="stats", description="View ticket statistics")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def stats(interaction: discord.Interaction):
    all_t = db.guild_tickets(interaction.guild_id)
    open_n = sum(1 for t in all_t if t["status"] == "open")
    rated = [t["rating"] for t in all_t if t.get("rating")]
    avg = f"{sum(rated) / len(rated):.2f}" if rated else "N/A"

    per_staff: dict[int, int] = {}
    for t in all_t:
        if t["claimed_by"]:
            per_staff[t["claimed_by"]] = per_staff.get(t["claimed_by"], 0) + 1
    top = "\n".join(
        f"{i}. <@{uid}> — {n}" for i, (uid, n) in enumerate(sorted(per_staff.items(), key=lambda x: -x[1])[:5], 1)
    ) or "No data yet"

    e = discord.Embed(color=cfg.COLOR_MAIN, title="📊 Ticket Stats")
    e.add_field(name="Total", value=str(len(all_t)))
    e.add_field(name="Open", value=str(open_n))
    e.add_field(name="Closed", value=str(len(all_t) - open_n))
    e.add_field(name="⭐ Avg Rating", value=f"{avg} ({len(rated)} ratings)")
    e.add_field(name="🏆 Top Staff (claims)", value=top, inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


AUTOREPLY_COOLDOWN = 120   # the same rule can only trigger once per channel in this many seconds
MAX_AUTOREPLIES = 25
PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


# ------------------------------------------------------------------ #
#  Keyword auto-replies                                               #
# ------------------------------------------------------------------ #
autoreply_cd: dict = {}


def render_reply(text: str, t: dict) -> str:
    staff = f"<@{t['claimed_by']}>" if t["claimed_by"] else "our staff"
    return text.replace("{user}", f"<@{t['user_id']}>").replace("{ticket}", pad(t["number"])).replace("{staff}", staff)


async def handle_autoreply(message: discord.Message, t: dict):
    g = db.get_guild(message.guild.id)
    rules = g.get("autoreplies") or {}
    if not rules:
        return
    if isinstance(message.author, discord.Member) and is_staff(message.author, g):
        return  # never auto-reply to staff messages
    text = message.content.lower().strip()
    if not text:
        return

    for name, r in rules.items():
        if not r.get("enabled", True):
            continue
        if r.get("only_unclaimed", True) and t["claimed_by"]:
            continue
        hit = False
        for kw in r["keywords"]:
            if r.get("mode") == "exact":
                hit = text == kw
            else:
                hit = re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", text) is not None
            if hit:
                break
        if not hit:
            continue

        key = (message.channel.id, name)
        if time.time() - autoreply_cd.get(key, 0) < AUTOREPLY_COOLDOWN:
            return
        autoreply_cd[key] = time.time()
        e = quick(cfg.COLOR_MAIN, render_reply(r["response"], t))
        e.set_footer(text="🤖 Auto-reply")
        try:
            await message.reply(embed=e, mention_author=False)
        except discord.HTTPException:
            pass
        return  # only one rule per message


autoreply_group = app_commands.Group(name="autoreply", description="Keyword auto-replies (inside ticket channels)", guild_only=True)


async def autoreply_autocomplete(interaction: discord.Interaction, current: str):
    g = db.get_guild(interaction.guild_id)
    return [app_commands.Choice(name=n, value=n) for n in g["autoreplies"] if current.lower() in n][:25]


@autoreply_group.command(name="add", description="Create a new auto-reply rule")
@app_commands.describe(
    name="Rule name",
    keywords="Comma-separated keywords (e.g. price, cost, pricing)",
    response="Reply text. Placeholders: {user}, {ticket}, {staff}",
    mode="How to match",
    only_unclaimed="Only reply while the ticket is unclaimed (default: yes)",
)
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Contains — keyword anywhere in the message", value="contains"),
        app_commands.Choice(name="Exact — the whole message must match", value="exact"),
    ]
)
async def autoreply_add(
    interaction: discord.Interaction,
    name: app_commands.Range[str, 1, 32],
    keywords: app_commands.Range[str, 1, 200],
    response: app_commands.Range[str, 1, 1500],
    mode: Optional[app_commands.Choice[str]] = None,
    only_unclaimed: bool = True,
):
    g = await require_staff(interaction)
    if not g:
        return
    key = norm_tag(name)
    kws = [k.strip().lower() for k in keywords.split(",") if k.strip()]
    if not key or not kws:
        return await reply(interaction, "❌ A name and at least one keyword are required.")
    if key in g["autoreplies"]:
        return await reply(interaction, f"❌ A rule named `{key}` already exists. Remove it first with `/autoreply remove`.")
    if len(g["autoreplies"]) >= MAX_AUTOREPLIES:
        return await reply(interaction, f"❌ You can have at most {MAX_AUTOREPLIES} rules.")
    g["autoreplies"][key] = {
        "keywords": kws,
        "response": response,
        "mode": mode.value if mode else "contains",
        "only_unclaimed": only_unclaimed,
        "enabled": True,
        "by": interaction.user.id,
    }
    db.save()
    await reply(
        interaction,
        f"✅ Rule `{key}` created.\nKeywords: {', '.join(f'`{k}`' for k in kws)}\n"
        f"The bot will now auto-reply when a user says one of these in a ticket.",
        cfg.COLOR_SUCCESS,
    )


@autoreply_group.command(name="remove", description="Delete an auto-reply rule")
@app_commands.describe(name="Rule name")
@app_commands.autocomplete(name=autoreply_autocomplete)
async def autoreply_remove(interaction: discord.Interaction, name: str):
    g = await require_staff(interaction)
    if not g:
        return
    if g["autoreplies"].pop(norm_tag(name), None) is None:
        return await reply(interaction, "❌ There's no rule with that name.")
    db.save()
    await reply(interaction, f"🗑️ Rule `{norm_tag(name)}` deleted.", cfg.COLOR_SUCCESS)


@autoreply_group.command(name="toggle", description="Turn a rule on or off")
@app_commands.describe(name="Rule name")
@app_commands.autocomplete(name=autoreply_autocomplete)
async def autoreply_toggle(interaction: discord.Interaction, name: str):
    g = await require_staff(interaction)
    if not g:
        return
    r = g["autoreplies"].get(norm_tag(name))
    if not r:
        return await reply(interaction, "❌ There's no rule with that name.")
    r["enabled"] = not r.get("enabled", True)
    db.save()
    await reply(interaction, f"Rule `{norm_tag(name)}` is now **{'ON ✅' if r['enabled'] else 'OFF ⏸️'}**.", cfg.COLOR_SUCCESS)


@autoreply_group.command(name="list", description="View all auto-reply rules")
async def autoreply_list(interaction: discord.Interaction):
    g = await require_staff(interaction)
    if not g:
        return
    if not g["autoreplies"]:
        return await reply(interaction, "There are no rules yet. Create one with `/autoreply add`.", cfg.COLOR_MAIN)
    lines = [
        f"{'✅' if r.get('enabled', True) else '⏸️'} **{n}** — {', '.join(f'`{k}`' for k in r['keywords'])} "
        f"• {r.get('mode', 'contains')}{' • unclaimed only' if r.get('only_unclaimed', True) else ''}"
        for n, r in g["autoreplies"].items()
    ]
    await interaction.response.send_message(
        embed=discord.Embed(color=cfg.COLOR_MAIN, title="🤖 Auto-reply Rules", description="\n".join(lines)[:4000]),
        ephemeral=True,
    )


bot.tree.add_command(autoreply_group)


# ------------------------------------------------------------------ #
#  /ticket update — send the user a status update (channel + DM)      #
# ------------------------------------------------------------------ #
@ticket_group.command(name="update", description="Send the user a status update (channel + DM)")
@app_commands.describe(message="Update message")
async def ticket_update(interaction: discord.Interaction, message: app_commands.Range[str, 1, 500]):
    ctx = await staff_ticket(interaction)
    if not ctx:
        return
    t, _ = ctx
    e = discord.Embed(color=cfg.COLOR_MAIN, title="📣 Ticket Update", description=message, timestamp=datetime.now(timezone.utc))
    e.set_footer(text=f"By {interaction.user.display_name}")
    await interaction.response.send_message(content=f"<@{t['user_id']}>", embed=e)
    await dm_user(
        t["user_id"],
        content=f"📣 **{interaction.guild.name}** — update on your ticket {interaction.channel.mention}:",
        embed=e,
    )


# ------------------------------------------------------------------ #
#  Live ticket board — auto-updating status message                   #
# ------------------------------------------------------------------ #
board_tasks: dict = {}
board_dirty: set = set()


def build_board_embed(guild_id: int) -> discord.Embed:
    open_t = sorted(
        (t for t in db.guild_tickets(guild_id) if t["status"] == "open"),
        key=lambda t: (PRIORITY_ORDER.get(t["priority"], 2), t["number"]),
    )
    claimed = sum(1 for t in open_t if t["claimed_by"])
    e = discord.Embed(color=cfg.COLOR_MAIN, title="📊 Live Ticket Board", timestamp=datetime.now(timezone.utc))
    if not open_t:
        e.description = "✅ There are no open tickets right now."
    else:
        lines = [
            f"{cfg.PRIORITIES[t['priority']]['emoji']} <#{t['channel_id']}> • {cat_of(t['category'])['emoji']} • "
            f"{('🙋 <@' + str(t['claimed_by']) + '>') if t['claimed_by'] else '⏳ unclaimed'} • {ts(t['created_at'])}"
            for t in open_t[:20]
        ]
        if len(open_t) > 20:
            lines.append(f"… and {len(open_t) - 20} more tickets")
        e.description = "\n".join(lines)
    e.add_field(name="Open", value=str(len(open_t)))
    e.add_field(name="Claimed", value=str(claimed))
    e.add_field(name="Unclaimed", value=str(len(open_t) - claimed))
    e.set_footer(text="This board updates automatically")
    return e


async def update_board(guild_id: int):
    g = db.get_guild(guild_id)
    guild = bot.get_guild(guild_id)
    channel = guild.get_channel(g["board_channel"]) if guild and g.get("board_channel") else None
    if not channel:
        return
    embed = build_board_embed(guild_id)
    msg = None
    if g.get("board_message_id"):
        try:
            msg = await channel.fetch_message(g["board_message_id"])
        except discord.NotFound:
            msg = None
    if msg:
        await msg.edit(embed=embed)
    else:
        msg = await channel.send(embed=embed)
        g["board_message_id"] = msg.id
        db.save()


def schedule_board(guild_id: int):
    """Batches board updates over 3 seconds to avoid hitting rate limits."""
    if not db.get_guild(guild_id).get("board_channel"):
        return
    board_dirty.add(guild_id)
    task = board_tasks.get(guild_id)
    if task and not task.done():
        return
    board_tasks[guild_id] = asyncio.create_task(_run_board(guild_id))


async def _run_board(guild_id: int):
    while guild_id in board_dirty:
        await asyncio.sleep(3)
        board_dirty.discard(guild_id)
        try:
            await update_board(guild_id)
        except Exception:  # noqa: BLE001
            traceback.print_exc()


board_group = app_commands.Group(
    name="board",
    description="Live ticket status board",
    guild_only=True,
    default_permissions=discord.Permissions(administrator=True),
)


@board_group.command(name="set", description="Create the live board in a channel")
@app_commands.describe(channel="Channel to show the board in")
async def board_set(interaction: discord.Interaction, channel: discord.TextChannel):
    g = db.get_guild(interaction.guild_id)
    g["board_channel"] = channel.id
    g["board_message_id"] = None
    db.save()
    await interaction.response.defer(ephemeral=True)
    try:
        await update_board(interaction.guild_id)
    except discord.Forbidden:
        g["board_channel"] = None
        db.save()
        return await interaction.followup.send(
            embed=quick(cfg.COLOR_DANGER, f"❌ I don't have permission to send messages in {channel.mention}."),
            ephemeral=True,
        )
    await interaction.followup.send(
        embed=quick(cfg.COLOR_SUCCESS, f"✅ Live board created in {channel.mention}. It will now update automatically whenever tickets are created, claimed or closed."),
        ephemeral=True,
    )


@board_group.command(name="refresh", description="Refresh the board now")
async def board_refresh(interaction: discord.Interaction):
    g = db.get_guild(interaction.guild_id)
    if not g.get("board_channel"):
        return await reply(interaction, "❌ Run `/board set` first.")
    await interaction.response.defer(ephemeral=True)
    await update_board(interaction.guild_id)
    await interaction.followup.send(embed=quick(cfg.COLOR_SUCCESS, "✅ Board refreshed."), ephemeral=True)


@board_group.command(name="remove", description="Turn off the live board")
async def board_remove(interaction: discord.Interaction):
    g = db.get_guild(interaction.guild_id)
    channel = interaction.guild.get_channel(g["board_channel"]) if g.get("board_channel") else None
    if channel and g.get("board_message_id"):
        try:
            msg = await channel.fetch_message(g["board_message_id"])
            await msg.delete()
        except discord.HTTPException:
            pass
    g["board_channel"] = None
    g["board_message_id"] = None
    db.save()
    await reply(interaction, "🗑️ Live board removed.", cfg.COLOR_SUCCESS)


bot.tree.add_command(board_group)


# ------------------------------------------------------------------ #
#  Events + background tasks                                          #
# ------------------------------------------------------------------ #
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    traceback.print_exception(type(error), error, error.__traceback__)
    msg = "❌ Something went wrong. Please check the bot's permissions."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    # Refresh live boards after a restart
    for gid, g in db.all_guilds().items():
        if g.get("board_channel"):
            schedule_board(int(gid))


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    t = db.get_ticket(message.channel.id)
    if t and t["status"] == "open":
        t["last_activity"] = time.time()
        db.save()
        await handle_autoreply(message, t)


@bot.event
async def on_guild_channel_delete(channel):
    t = db.get_ticket(channel.id)
    if t and t["status"] == "open":
        t["status"] = "closed"
        t["close_reason"] = "Channel was deleted manually"
        db.save()
        schedule_board(channel.guild.id)


@tasks.loop(minutes=30)
async def auto_close():
    for t in db.all_open_tickets():
        hours = db.get_guild(t["guild_id"]).get("auto_close_hours") or cfg.AUTO_CLOSE_HOURS
        if time.time() - t["last_activity"] < hours * 3600:
            continue
        channel = bot.get_channel(t["channel_id"])
        if channel is None:
            t["status"] = "closed"
            db.save()
            continue
        await close_ticket(channel, bot.user, f"Auto-closed after {hours} hours of inactivity")


@auto_close.before_loop
async def before_auto_close():
    await bot.wait_until_ready()


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("❌ DISCORD_TOKEN is missing. Add your bot token to the .env file.")
    bot.run(token)
