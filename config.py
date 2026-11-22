COLOR_MAIN = 0x5865F2
COLOR_SUCCESS = 0x57F287
COLOR_DANGER = 0xED4245
COLOR_WARN = 0xFEE75C

# Ticket types shown in the panel dropdown (max 25).
# Every type gets its OWN Discord category, created automatically by /setup
# (or re-created automatically if someone deletes it).
#   id                -> internal id (don't change after tickets exist)
#   label/emoji       -> what members see in the dropdown
#   prefix            -> channel name prefix, e.g. gmail-0001
#   discord_category  -> name of the Discord category the tickets go into
#   *_label / *_placeholder -> the form fields members fill in
CATEGORIES = [
    {
        "id": "gmail",
        "label": "Gmail Work",
        "emoji": "📧",
        "description": "Open a ticket for Gmail creation work",
        "prefix": "gmail",
        "discord_category": "📧 Gmail Work",
        "subject_label": "Work type / batch name",
        "subject_placeholder": "e.g. Gmail creation - batch 1",
        "description_label": "Details of your work",
        "description_placeholder": "How many accounts, what you need, any issue you're facing...",
    },
    {
        "id": "gcloud",
        "label": "Google Cloud Account",
        "emoji": "☁️",
        "description": "Open a ticket for Google Cloud account work",
        "prefix": "gcloud",
        "discord_category": "☁️ Google Cloud Work",
        "subject_label": "Work type / batch name",
        "subject_placeholder": "e.g. Google Cloud account creation",
        "description_label": "Details of your work",
        "description_placeholder": "How many accounts, what you need, any issue you're facing...",
    },
    {
        "id": "support",
        "label": "Support",
        "emoji": "🛠️",
        "description": "Get help from the team",
        "prefix": "support",
        "discord_category": "🛠️ Support Tickets",
        "subject_label": "Subject",
        "subject_placeholder": "What do you need help with?",
        "description_label": "Describe your issue in detail",
        "description_placeholder": "Tell us what's going on...",
    },
]

PRIORITIES = {
    "low": {"label": "Low", "emoji": "🟢"},
    "normal": {"label": "Normal", "emoji": "🟡"},
    "high": {"label": "High", "emoji": "🟠"},
    "urgent": {"label": "Urgent", "emoji": "🔴"},
}

DEFAULT_MAX_TICKETS = 3      # max open tickets per user
COOLDOWN_SECONDS = 30        # cooldown between opening tickets
AUTO_CLOSE_HOURS = 48        # auto-close after this many hours of inactivity
DELETE_DELAY_SECONDS = 5     # how long to wait before deleting a closed ticket channel
ALLOW_OWNER_CLOSE = True     # let the ticket owner close their own ticket
