#!/usr/bin/env python3
"""
Kick Chat -> Discord Flag Prototype (Pusher/WebSocket method)

Usage:
  1. Set CHANNEL_SLUG and DISCORD_WEBHOOK_URL_FLAGGED below
  2. Run: python kick_chat_monitor.py
"""

import asyncio
import json
import logging
from collections import deque
import re
import requests
import websockets
from ai import ask_ollama
from config import SYSTEM_PROMPT, CHANNEL_SLUG, PUSHER_APP_KEY, DISCORD_WEBHOOK_URL_CHAT, DISCORD_WEBHOOK_URL_EVENTS, DISCORD_WEBHOOK_URL_FLAGGED
from discord import send_to_discord

# Optional: hardcode chatroom ID if you already know it (skips the lookup)
# CHATROOM_ID = 123456
CHATROOM_ID = None

PUSHER_CLUSTER = "us2"
PUSHER_URL = (
    f"wss://ws-{PUSHER_CLUSTER}.pusher.com/app/{PUSHER_APP_KEY}"
    "?protocol=7&client=js&version=8.4.0-rc2&flash=false"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kick-monitor")

RECENT_MESSAGES = deque(maxlen=8)

# Fast path for the most common spam right now
SPAM_PATTERNS = [
    r"kivasmm\.?com",
    r"make your (live )?stream (stand out|fire|legendary)",
    r"bring fire energy to your live stream",
]

def is_obvious_spam(content: str) -> bool:
    content_lower = content.lower()
    return any(re.search(p, content_lower) for p in SPAM_PATTERNS)

def clean_message(content: str) -> str:
    """Remove Kick emote tags and clean up whitespace."""
    if not content:
        return ""

    # Remove Kick emote tags, including consecutive tags.
    content = re.sub(
        r"\[emote:\s*\d+\s*:[^\]]*\]",
        "",
        content,
        flags=re.IGNORECASE,
    )

    # Normalize whitespace.
    content = re.sub(r"\s+", " ", content).strip()

    return content


def get_chatroom_id(slug: str) -> int:
    """Resolve channel slug → chatroom ID using Kick's public v2 endpoint."""
    url = f"https://kick.com/api/v2/channels/{slug}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    r = requests.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    data = r.json()
    chatroom_id = data["chatroom"]["id"]
    log.info(f"Resolved {slug} → chatroom_id={chatroom_id}")
    return chatroom_id


# ---------------------------------------------------------------------------
# AI REVIEW
# ---------------------------------------------------------------------------
def should_flag(message: dict) -> tuple[bool, str]:
    raw_content = message.get("content", "").strip()
    content = clean_message(raw_content)
    username = message.get("sender_username", "unknown")

    # Skip completely empty messages (just emotes)
    if not content:
        return False, ""

    if is_obvious_spam(content):
        return True, "Obvious spam pattern (kivasmm / stream growth promo)"

    if username == "theimperious1" and content == "test":
        return True, "Developer testing"

    # Add current message to history
    RECENT_MESSAGES.append({
        "user": username,
        "msg": content
    })

    # Build recent context
    recent_context = ""
    if len(RECENT_MESSAGES) > 1:
        recent_context = "Recent messages in chat:\n"
        for m in list(RECENT_MESSAGES)[:-1]:  # exclude the current one
            recent_context += f"- {m['user']}: {m['msg']}\n"
        recent_context += "\n"

    prompt = f"""{recent_context}Current message to evaluate:
User: {username}
Message: {content}
"""

    try:
        reply = ask_ollama(prompt, SYSTEM_PROMPT)
    except Exception as e:
        log.error(f"Ollama error: {e}")
        return False, ""  # fail open so the bot doesn't die

    # Parse the response
    reply = reply.strip()
    flagged = False
    reason = ""

    # Look for FLAG: yes/no
    flag_match = re.search(r"FLAG:\s*(yes|no)", reply, re.IGNORECASE)
    reason_match = re.search(r"REASON:\s*(.*)", reply, re.IGNORECASE | re.DOTALL)

    if flag_match:
        flagged = flag_match.group(1).lower() == "yes"

    if reason_match:
        reason = reason_match.group(1).strip()
        # Clean up any leftover formatting
        reason = reason.split("\n")[0].strip()

    if flagged and not reason:
        reason = "AI flagged (no reason given)"

    return flagged, reason
# ---------------------------------------------------------------------------


def decode_event_data(data):
    """Decode Pusher's potentially double-encoded JSON payload."""
    if isinstance(data, str):
        return json.loads(data)
    return data or {}



async def listen(chatroom_id: int, channel_id: int = 0):
    channel_name = f"chatrooms.{chatroom_id}.v2"

    # Keep track of stream state and metadata between events.
    is_live = None
    last_title = None
    last_category = None

    log.info(f"Connecting to {PUSHER_URL}")
    log.info(f"Subscribing to {channel_name}")

    while True:
        try:
            async with websockets.connect(
                PUSHER_URL,
                ping_interval=20,
                ping_timeout=20
            ) as ws:

                # Wait for connection_established
                raw = await ws.recv()
                data = json.loads(raw)

                if data.get("event") != "pusher:connection_established":
                    log.warning(f"Unexpected first message: {data}")
                    continue

                # Subscribe to chat
                channels = [channel_name]

                if channel_id:
                    channels.append(f"channel_{channel_id}")

                for channel in channels:
                    subscribe = {
                        "event": "pusher:subscribe",
                        "data": {
                            "auth": "",
                            "channel": channel,
                        },
                    }
                    await ws.send(json.dumps(subscribe))

                log.info(f"Subscribed to: {', '.join(channels)}")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        event = msg.get("event", "")

                        # Keepalive
                        if event == "pusher:ping":
                            await ws.send(json.dumps({
                                "event": "pusher:pong",
                                "data": {},
                            }))
                            continue

                        inner = decode_event_data(msg.get("data"))

                        # -----------------------------------------
                        # CHAT MESSAGE
                        # -----------------------------------------
                        if "ChatMessageEvent" in event:
                            sender = inner.get("sender", {})
                            identity = sender.get("identity") or {}
                            badges = identity.get("badges", [])

                            normalized = {
                                "message_id": inner.get("id") or inner.get("message_id"),
                                "content": inner.get("content", ""),
                                "sender_username": sender.get("username", "unknown"),
                                "sender_id": sender.get("id"),
                                "badges": badges,
                                "created_at": inner.get("created_at"),
                                "raw": inner,
                            }

                            log.info(
                                f"{normalized['sender_username']}: "
                                f"{normalized['content'][:100]}"
                            )

                            flagged, reason = should_flag(normalized)

                            if flagged:
                                log.info(
                                    f"FLAGGED | {normalized['sender_username']}: "
                                    f"{normalized['content'][:80]}"
                                )
                                send_to_discord(
                                    DISCORD_WEBHOOK_URL_FLAGGED,
                                    normalized,
                                    reason,
                                    category=last_category,
                                )
                            else:
                                send_to_discord(
                                    DISCORD_WEBHOOK_URL_CHAT,
                                    normalized,
                                    reason,
                                    category=last_category,
                                )

                        # -----------------------------------------
                        # STREAM ONLINE
                        # -----------------------------------------
                        elif event in (
                            "App\\Events\\StreamerIsLive",
                            "livestream.status.updated",
                        ):
                            if inner.get("is_live", True) and is_live is not True:
                                is_live = True
                                title = inner.get("title", "Unknown title")

                                log.info(f"STREAM ONLINE | {title}")
                                send_to_discord(
                                    DISCORD_WEBHOOK_URL_EVENTS,
                                    {
                                        "sender_username": "Stream Status",
                                        "content": (
                                            f"🟢 **Stream is now ONLINE**\n"
                                            f"**Title:** {title}"
                                        ),
                                    },
                                    category=last_category,
                                )

                        # -----------------------------------------
                        # STREAM OFFLINE
                        # -----------------------------------------
                        elif event in (
                            "App\\Events\\StopStreamBroadcast",
                            "livestream.status.updated",
                        ):
                            if inner.get("is_live") is False and is_live is not False:
                                is_live = False

                                log.info("STREAM OFFLINE")
                                send_to_discord(
                                    DISCORD_WEBHOOK_URL_EVENTS,
                                    {
                                        "sender_username": "Stream Status",
                                        "content": "🔴 **Stream is now OFFLINE**",
                                    },
                                    category=last_category,
                                )

                        # -----------------------------------------
                        # TITLE / CATEGORY UPDATE
                        # -----------------------------------------
                        elif event == "livestream.metadata.updated":
                            metadata = inner.get("metadata", {})

                            title = metadata.get("title", last_title)

                            category_data = (
                                metadata.get("category")
                                or metadata.get("Category")
                                or {}
                            )

                            if isinstance(category_data, dict):
                                category = category_data.get("name")
                            else:
                                category = category_data

                            changes = []

                            if (
                                title is not None
                                and last_title is not None
                                and title != last_title
                            ):
                                changes.append(
                                    f"**Title:** {last_title} → {title}"
                                )

                            if (
                                category is not None
                                and last_category is not None
                                and category != last_category
                            ):
                                changes.append(
                                    f"**Category:** {last_category} → {category}"
                                )

                            if title is not None:
                                last_title = title

                            if category is not None:
                                last_category = category

                            if changes:
                                content = "📝 **Stream information updated**\n"
                                content += "\n".join(changes)

                                log.info(content)
                                send_to_discord(
                                    DISCORD_WEBHOOK_URL_EVENTS,
                                    {
                                        "sender_username": "Stream Update",
                                        "content": content,
                                    },
                                    category=last_category,
                                )

                        # -----------------------------------------
                        # DEBUG UNKNOWN EVENTS
                        # -----------------------------------------
                        elif event not in (
                            "pusher:connection_established",
                            "pusher:subscription_succeeded",
                        ):
                            log.debug(
                                f"Other Pusher event: {event} | {inner}"
                            )

                    except json.JSONDecodeError:
                        continue

                    except Exception as e:
                        log.exception(f"Error processing message: {e}")

        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            log.warning(f"Connection lost ({e}). Reconnecting in 5s...")
            await asyncio.sleep(5)

        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            await asyncio.sleep(5)



async def main():
    chatroom_id = CHATROOM_ID
    if chatroom_id is None:
        try:
            chatroom_id = get_chatroom_id(CHANNEL_SLUG)
        except Exception as e:
            log.error(f"Could not resolve chatroom ID for '{CHANNEL_SLUG}': {e}")
            log.error("You can hardcode CHATROOM_ID at the top of the file instead.")
            return

    await listen(chatroom_id)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user.")