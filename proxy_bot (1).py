"""
Webhook proxy bot.

Deletes each user message and re-posts it through a channel webhook using the
author's display name and avatar. Webhook messages aren't attached to a real
account, so nobody can click through to a profile or DM the sender.

Setup:
    pip install -U "discord.py>=2.3"
    export DISCORD_TOKEN="your-token"
    python proxy_bot.py

In the Developer Portal, under Bot -> Privileged Gateway Intents, turn on
MESSAGE CONTENT INTENT. The bot's role needs Manage Messages, Manage Webhooks,
and Read Message History in every channel it proxies.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Dict, Set

import discord

# ---------------------------------------------------------------- config ----

# Only proxy in these channels. Leave empty to proxy everywhere the bot can see.
PROXY_CHANNEL_IDS: Set[int] = set()

# Never proxy in these channels.
IGNORED_CHANNEL_IDS: Set[int] = set()

# Users with any of these roles are left alone (useful for mods).
EXEMPT_ROLE_IDS: Set[int] = set()

WEBHOOK_NAME = "proxy-relay"

# Discord rejects webhook usernames containing these.
BANNED_NAME_WORDS = ("discord", "clyde")

# --------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

client = discord.Client(intents=intents)

_webhook_cache: Dict[int, discord.Webhook] = {}
_webhook_locks: Dict[int, asyncio.Lock] = {}


def sanitize_username(name: str) -> str:
    """Make a display name safe for the webhook username field."""
    cleaned = name
    for word in BANNED_NAME_WORDS:
        cleaned = re.sub(word, "\u2022" * len(word), cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()[:80]
    return cleaned or "Unknown"


async def get_webhook(channel: discord.TextChannel) -> discord.Webhook:
    """Fetch or create this channel's relay webhook, cached per channel."""
    if channel.id in _webhook_cache:
        return _webhook_cache[channel.id]

    lock = _webhook_locks.setdefault(channel.id, asyncio.Lock())
    async with lock:
        if channel.id in _webhook_cache:
            return _webhook_cache[channel.id]

        for hook in await channel.webhooks():
            if hook.name == WEBHOOK_NAME and hook.token is not None:
                _webhook_cache[channel.id] = hook
                return hook

        hook = await channel.create_webhook(
            name=WEBHOOK_NAME, reason="Message proxying"
        )
        _webhook_cache[channel.id] = hook
        return hook


def should_proxy(message: discord.Message) -> bool:
    if message.guild is None:
        return False
    if message.author.bot or message.webhook_id is not None:
        return False
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return False

    channel = message.channel
    parent_id = getattr(channel, "parent_id", None) or channel.id
    if parent_id in IGNORED_CHANNEL_IDS or channel.id in IGNORED_CHANNEL_IDS:
        return False
    if PROXY_CHANNEL_IDS and parent_id not in PROXY_CHANNEL_IDS:
        return False

    if EXEMPT_ROLE_IDS and isinstance(message.author, discord.Member):
        if any(role.id in EXEMPT_ROLE_IDS for role in message.author.roles):
            return False

    return True


def build_reply_header(message: discord.Message) -> str:
    """Webhooks can't use native replies, so fake one with a quote line."""
    ref = message.reference
    if ref is None:
        return ""

    resolved = ref.resolved
    if isinstance(resolved, discord.Message):
        author = resolved.author.display_name
        snippet = resolved.content.replace("\n", " ").strip()
        if len(snippet) > 80:
            snippet = snippet[:77] + "..."
        if not snippet:
            snippet = "*(attachment)*"
        return f"-# \u21b3 replying to **{author}**: {snippet}\n"

    return "-# \u21b3 replying to an earlier message\n"


@client.event
async def on_ready() -> None:
    print(f"Connected as {client.user} ({client.user.id})")


@client.event
async def on_message(message: discord.Message) -> None:
    if not should_proxy(message):
        return

    channel = message.channel
    is_thread = isinstance(channel, discord.Thread)
    target = channel.parent if is_thread else channel

    if not isinstance(target, discord.TextChannel):
        return

    try:
        webhook = await get_webhook(target)
    except discord.Forbidden:
        print(f"Missing Manage Webhooks in #{target.name}")
        return

    # Re-upload attachments before deleting; the CDN links die with the message.
    files = []  # type: list
    for attachment in message.attachments:
        try:
            files.append(await attachment.to_file(spoiler=attachment.is_spoiler()))
        except (discord.HTTPException, discord.NotFound):
            pass

    content = build_reply_header(message) + message.content

    if not content.strip() and not files:
        return  # nothing worth relaying (sticker-only, poll, etc.)

    if len(content) > 2000:
        content = content[:1997] + "..."

    member = message.author
    avatar = member.display_avatar.replace(static_format="png").url

    # Let role/user pings through, but never let a proxied message hit @everyone.
    mentions = discord.AllowedMentions(everyone=False, users=True, roles=False)

    kwargs = {
        "content": content,
        "username": sanitize_username(member.display_name),
        "avatar_url": avatar,
        "files": files,
        "allowed_mentions": mentions,
        "wait": True,
    }
    if is_thread:
        kwargs["thread"] = channel

    try:
        await webhook.send(**kwargs)
    except discord.NotFound:
        # Webhook was deleted out from under us; rebuild and retry once.
        _webhook_cache.pop(target.id, None)
        try:
            webhook = await get_webhook(target)
            await webhook.send(**kwargs)
        except discord.HTTPException as exc:
            print(f"Relay failed in #{target.name}: {exc}")
            return
    except discord.HTTPException as exc:
        print(f"Relay failed in #{target.name}: {exc}")
        return

    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound):
        pass


@client.event
async def on_webhooks_update(channel: discord.abc.GuildChannel) -> None:
    _webhook_cache.pop(channel.id, None)


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("Set the DISCORD_TOKEN environment variable.")
    client.run(token)


if __name__ == "__main__":
    main()
