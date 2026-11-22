import json
import os

import config as cfg

FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
_data = {"guilds": {}, "tickets": {}}

if os.path.exists(FILE):
    try:
        with open(FILE, encoding="utf-8") as f:
            _data = json.load(f)
    except Exception as e:  # noqa: BLE001
        print("Could not load data.json:", e)


def save():
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(_data, f, indent=2, ensure_ascii=False)


def _guild_defaults():
    return {
        "categories": {},           # ticket type id -> auto-created Discord category ID
        "support_role": None,
        "log_channel": None,
        "counter": 0,
        "max_tickets": cfg.DEFAULT_MAX_TICKETS,
        "blacklist": [],
        "tags": {},                 # canned responses: {name: {"content": str, "by": int}}
        "auto_close_hours": None,   # None = use the default from config.py
        "welcome_message": None,    # None = default footer
        "autoreplies": {},          # {name: {"keywords": [...], "response": str, "mode": str, ...}}
        "board_channel": None,      # channel of the live status board
        "board_message_id": None,
    }


def get_guild(guild_id):
    key = str(guild_id)
    if key not in _data["guilds"]:
        _data["guilds"][key] = _guild_defaults()
        save()
    g = _data["guilds"][key]
    # Add any new keys to older data.json files
    for k, v in _guild_defaults().items():
        g.setdefault(k, v)
    return g


def all_guilds():
    return _data["guilds"]


def get_ticket(channel_id):
    return _data["tickets"].get(str(channel_id))


def add_ticket(t):
    _data["tickets"][str(t["channel_id"])] = t
    save()


def guild_tickets(guild_id):
    return [t for t in _data["tickets"].values() if t["guild_id"] == int(guild_id)]


def find_ticket(guild_id, number):
    return next((t for t in guild_tickets(guild_id) if t["number"] == number), None)


def open_tickets_of(guild_id, user_id):
    return [t for t in guild_tickets(guild_id) if t["user_id"] == user_id and t["status"] == "open"]


def all_open_tickets():
    return [t for t in _data["tickets"].values() if t["status"] == "open"]
