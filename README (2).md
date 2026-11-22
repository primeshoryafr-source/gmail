# 🎫 Advanced Discord Ticket Bot (Python / discord.py 2.x)

## Features
- Dropdown **ticket panel** (multiple categories) + **modal form** (subject + description)
- Private ticket channels, automatic access for your support role
- **Claim / Unclaim / Transfer**, **Priority** (🟢🟡🟠🔴), **Rename**, **Add / Remove user**, **Lock / Unlock**, **Slowmode**
- **Close** with confirmation + reason, plus **close requests** (the user accepts or declines with buttons)
- **HTML transcripts** (sent to the log channel and DM'd to the user)
- ⭐ **Rating system** (1–5 stars via DM, reported to the log channel)
- **Keyword auto-replies**, **tags** (canned responses), private **staff notes**
- **Live ticket board** that updates itself
- **Max tickets per user**, **cooldown**, **blacklist**, **auto-close** for inactive tickets
- `/stats`, `/staffstats`, `/ticket history`, `/ticket list`
- Buttons and dropdowns keep working after a bot restart (persistent views)
- JSON storage (`data.json`) — no database to install

## Setup
1. Install **Python 3.10+**.
2. Go to https://discord.com/developers/applications → create an application → **Bot** tab:
   - Copy the bot token
   - Turn **Message Content Intent** ON (required for transcripts and auto-replies)
3. Invite the bot (OAuth2 → URL Generator): scopes `bot` + `applications.commands`
   Permissions: Manage Channels, View Channels, Send Messages, Manage Messages, Embed Links, Attach Files, Read Message History
4. Open the **`.env`** file and paste your token:
   ```
   DISCORD_TOKEN=your_real_token_here
   GUILD_ID=your_server_id   # optional, makes slash commands appear instantly
   ```
5. In a terminal:
   ```bash
   pip install -r requirements.txt
   python bot.py
   ```
6. In your server, run:
   `/setup panel_channel:#tickets support_role:@Staff`
   The bot **creates the categories by itself** (and a private `#ticket-logs` channel if you don't pick one):
   - 📧 **Gmail Work** → tickets named `gmail-0001`, `gmail-0002`, ...
   - ☁️ **Google Cloud Work** → tickets named `gcloud-0001`, ...
   - 🛠️ **Support Tickets** → tickets named `support-0001`, ...

   If someone deletes one of these categories, the bot re-creates it the next time a ticket is opened.

## Ticket types
The panel dropdown has three options: **Gmail Work**, **Google Cloud Account** and **Support**. Each has its own
form wording and its own Discord category. To rename them or add more types, edit `CATEGORIES` in `config.py`
(`discord_category` = the category name, `prefix` = channel name prefix). `/ticket move` also moves the channel into
the matching category.

> `example.env` is just a template. The bot reads the file named **`.env`**.

## Commands

### Ticket commands (`/ticket ...`)
| Command | What it does | Who can use it |
|---|---|---|
| `add <user>` / `remove <user>` | Add or remove a user | Staff |
| `rename <name>` | Rename the channel | Staff |
| `priority <level>` | Low / Normal / High / Urgent | Staff |
| `claim` / `unclaim` | Claim or release the ticket | Staff |
| `transfer <staff>` | Hand the ticket to another staff member | Staff |
| `info` | Full ticket details | Staff |
| `note <text>` / `notes` | Private staff notes (hidden from the user) | Staff |
| `move <category>` | Change the ticket category | Staff |
| `lock` / `unlock` | Stop / allow the user from sending messages | Staff |
| `slowmode <seconds>` | Set channel slowmode | Staff |
| `ping` | Remind the owner (channel + DM) | Staff |
| `update <message>` | Send the user a status update (channel + DM) | Staff |
| `closerequest [reason]` | Ask the user if the ticket can be closed (buttons) | Staff |
| `close [reason]` | Close the ticket | Staff + owner |
| `transcript` | Generate an HTML transcript | Staff |
| `list` | List all open tickets | Staff |
| `history <user>` | A user's previous tickets | Staff |

### Other commands
| Command | What it does | Who can use it |
|---|---|---|
| `/setup` | Configure the system and post the panel | Admin |
| `/sendpanel [channel]` | Post the panel again | Admin |
| `/config view` / `set` | Change support role, log channel, max tickets, auto-close hours, welcome footer | Admin |
| `/board set` / `refresh` / `remove` | Live ticket board | Admin |
| `/blacklist add` / `remove` / `list` | Block users from opening tickets | Manage Server |
| `/tag create` / `edit` / `use` / `delete` / `list` | Canned responses (with autocomplete) | Staff |
| `/autoreply add` / `remove` / `toggle` / `list` | Keyword auto-replies | Staff |
| `/stats` | Server-wide ticket stats | Manage Channels |
| `/staffstats [member]` | Claimed / closed tickets + average rating | Staff |
| `/ping` / `/help` | Latency / command list | Everyone |

## 🤖 Auto-reply keywords
```
/autoreply add name:price keywords:price, cost, pricing response:Hi {user}! See our pricing here: https://example.com/pricing
```
- When a **non-staff** user says a keyword in a ticket, the bot replies (staff messages are ignored).
- `mode`: **contains** (whole-word match anywhere in the message) or **exact** (the whole message must match).
- `only_unclaimed` (default: yes): auto-replies stop as soon as a staff member claims the ticket.
- Placeholders: `{user}` = ticket owner, `{ticket}` = ticket number, `{staff}` = the staff member who claimed it.
- Anti-spam: a rule can only trigger once per channel every 120 seconds.

## 🙋 Claim & updates
- **Claiming** updates the ticket embed, pings the user in the channel, DMs them, and writes an entry in the log channel.
- **Unclaim / transfer** are logged too.
- `/ticket update` lets staff send progress updates to the user (channel + DM).
- **Live board** (`/board set #channel`): one message listing open tickets by priority, with claimed/unclaimed status. It refreshes automatically when tickets are created, claimed, unclaimed, transferred, re-prioritised, moved or closed, and also after a bot restart.

## Customize
Edit `config.py` to change categories, priorities, max tickets, cooldown and auto-close time.

## Tips
- The bot's role must be **above the support role** in your server's role list.
- Discord limits channel renames to 2 per 10 minutes, so renaming or changing priority repeatedly may delay the new channel name.
- Never share your token or upload `.env` to GitHub (the included `.gitignore` already excludes it). If your token leaks, reset it in the Developer Portal right away.
