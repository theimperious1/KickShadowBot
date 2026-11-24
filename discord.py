from datetime import datetime, timezone
from config import CHANNEL_SLUG
import requests
import logging
from typing import Optional


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("discord")

def send_stream_notification(content: str):
    """Send a stream event to the Event Discord webhook."""
    notification = {
        "sender_username": "Kick Stream",
        "content": content,
    }
    send_to_discord(DISCORD_WEBHOOK_URL_EVENTS, notification, "")



def send_to_discord(
    webhook: str,
    message: dict,
    reason: str = "",
    category: Optional[str] = None,
):
    """Post a chat message, flagged message, or event to Discord."""
    username = message.get("sender_username", "unknown")
    content = message.get("content", "")
    msg_id = message.get("message_id", "")

    is_chat = webhook == DISCORD_WEBHOOK_URL_CHAT
    is_event = webhook == DISCORD_WEBHOOK_URL_EVENTS

    if is_chat or is_event:
        # Compact embed for ordinary chat messages or events
        embed = {
            "description": content[:4000] or "*empty*",
            "color": 0x5865F2,
            "author": {"name": username},
            "fields": [
                {
                    "name": "Category",
                    "value": category or "Unknown",
                    "inline": True,
                },
            ],
            "footer": {"text": f"Channel: {CHANNEL_SLUG}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    else:
        # Detailed embed for flagged messages
        badges = ", ".join(
            b.get("text", b.get("type", ""))
            for b in message.get("badges", [])
        ) or "none"

        embed = {
            "title": f"Flagged message from {username}",
            "description": content[:2000] or "*empty*",
            "color": 0xE74C3C,
            "fields": [
                {"name": "Reason", "value": reason or "—", "inline": False},
                {
                    "name": "Category",
                    "value": category or "Unknown",
                    "inline": True,
                },
                {"name": "Badges", "value": badges, "inline": True},
                {"name": "Message ID", "value": f"`{msg_id}`", "inline": True},
            ],
            "footer": {"text": f"Channel: {CHANNEL_SLUG}"},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    payload = {
        "username": "Asmongold247",
        "embeds": [embed],
    }

    try:
        r = requests.post(webhook, json=payload, timeout=10)
        if r.status_code >= 400:
            log.warning(f"Discord webhook error {r.status_code}: {r.text}")
        else:
            log.info(f"→ Discord: {username}: {content[:60]}...")
    except Exception as e:
        log.error(f"Failed to send Discord webhook: {e}")
