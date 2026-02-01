"""
Discord Message Cleanup Bot

Automatically deletes old messages from the CLRI alerts channel.
Runs as a background task integrated into the worker.
"""

import asyncio
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

logger = logging.getLogger(__name__)


class DiscordCleanupBot:
    """
    Discord bot for automatic message cleanup.

    Features:
    - Deletes messages older than configured age (default 24 hours)
    - Runs cleanup check at configured interval (default hourly)
    - Preserves most recent N messages regardless of age
    - Handles Discord rate limits gracefully
    - Can be triggered manually for immediate cleanup
    """

    def __init__(
        self,
        bot_token: str,
        channel_id: int,
        max_age_hours: int = 24,
        check_interval_seconds: int = 3600,  # 1 hour
        keep_recent_count: int = 10,
    ):
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.max_age_hours = max_age_hours
        self.check_interval = check_interval_seconds
        self.keep_recent_count = keep_recent_count

        # Discord client with minimal intents
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        self._running = False
        self._ready = asyncio.Event()
        self._cleanup_task: Optional[asyncio.Task] = None

        # Register event handlers
        @self._client.event
        async def on_ready():
            logger.info(f"Discord cleanup bot connected as {self._client.user}")
            self._ready.set()

    async def start(self):
        """Start the cleanup bot"""
        self._running = True

        # Start Discord client in background
        asyncio.create_task(self._run_client())

        # Wait for client to be ready
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error("Discord cleanup bot failed to connect within 30 seconds")
            return

        # Start periodic cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info(
            f"Discord cleanup bot started: "
            f"channel={self.channel_id}, "
            f"max_age={self.max_age_hours}h, "
            f"interval={self.check_interval}s"
        )

    async def stop(self):
        """Stop the cleanup bot"""
        self._running = False

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        if self._client and not self._client.is_closed():
            await self._client.close()

        logger.info("Discord cleanup bot stopped")

    async def _run_client(self):
        """Run the Discord client"""
        try:
            await self._client.start(self.bot_token)
        except discord.LoginFailure:
            logger.error("Discord cleanup bot: Invalid bot token")
        except Exception as e:
            logger.error(f"Discord cleanup bot error: {e}")

    async def _cleanup_loop(self):
        """Periodic cleanup loop"""
        # Wait a bit before first cleanup to let worker stabilize
        await asyncio.sleep(60)

        while self._running:
            try:
                await self.cleanup_old_messages()
            except Exception as e:
                logger.error(f"Cleanup error: {e}")

            # Wait for next check interval
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def cleanup_old_messages(self) -> dict:
        """
        Delete messages older than max_age_hours, keeping most recent keep_recent_count.

        Returns dict with cleanup stats.
        """
        if not self._client.is_ready():
            logger.warning("Discord client not ready, skipping cleanup")
            return {"error": "not_ready"}

        channel = self._client.get_channel(self.channel_id)
        if not channel:
            # Try fetching the channel
            try:
                channel = await self._client.fetch_channel(self.channel_id)
            except discord.NotFound:
                logger.error(f"Channel {self.channel_id} not found")
                return {"error": "channel_not_found"}
            except discord.Forbidden:
                logger.error(f"No access to channel {self.channel_id}")
                return {"error": "forbidden"}

        if not isinstance(channel, discord.TextChannel):
            logger.error(f"Channel {self.channel_id} is not a text channel")
            return {"error": "not_text_channel"}

        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.max_age_hours)

        # Collect messages to delete
        messages_to_delete = []
        recent_messages = []

        try:
            async for message in channel.history(limit=1000):
                # Skip bot's own messages if they're recent
                recent_messages.append(message)

                if len(recent_messages) <= self.keep_recent_count:
                    continue

                if message.created_at < cutoff_time:
                    messages_to_delete.append(message)
        except discord.Forbidden:
            logger.error("Bot lacks permission to read message history")
            return {"error": "cannot_read_history"}

        if not messages_to_delete:
            logger.debug(f"No old messages to delete (checked {len(recent_messages)} messages)")
            return {"deleted": 0, "checked": len(recent_messages)}

        # Delete messages
        deleted_count = 0
        bulk_delete = []
        individual_delete = []

        # Discord bulk delete only works on messages < 14 days old
        bulk_cutoff = datetime.now(timezone.utc) - timedelta(days=14)

        for msg in messages_to_delete:
            if msg.created_at > bulk_cutoff:
                bulk_delete.append(msg)
            else:
                individual_delete.append(msg)

        # Bulk delete (faster, up to 100 at a time)
        if bulk_delete:
            for i in range(0, len(bulk_delete), 100):
                batch = bulk_delete[i:i+100]
                try:
                    await channel.delete_messages(batch)
                    deleted_count += len(batch)
                    logger.debug(f"Bulk deleted {len(batch)} messages")
                except discord.Forbidden:
                    logger.error("Bot lacks permission to delete messages")
                    break
                except discord.HTTPException as e:
                    logger.error(f"Bulk delete failed: {e}")
                    # Fall back to individual delete
                    individual_delete.extend(batch)

                # Small delay between batches
                await asyncio.sleep(1)

        # Individual delete for old messages (slower due to rate limits)
        for msg in individual_delete:
            try:
                await msg.delete()
                deleted_count += 1
                # Rate limit: ~5 messages per second
                await asyncio.sleep(0.25)
            except discord.Forbidden:
                logger.error("Bot lacks permission to delete messages")
                break
            except discord.NotFound:
                # Message already deleted
                pass
            except discord.HTTPException as e:
                logger.warning(f"Failed to delete message: {e}")

        logger.info(
            f"Cleanup complete: deleted {deleted_count}/{len(messages_to_delete)} messages "
            f"(kept {self.keep_recent_count} recent)"
        )

        return {
            "deleted": deleted_count,
            "attempted": len(messages_to_delete),
            "kept_recent": self.keep_recent_count,
            "checked": len(recent_messages),
        }

    async def purge_all(self, keep_count: int = 0) -> dict:
        """
        Delete all messages in the channel except the most recent keep_count.
        Use with caution - this is for clearing accumulated messages during development.

        Returns dict with cleanup stats.
        """
        if not self._client.is_ready():
            logger.warning("Discord client not ready, skipping purge")
            return {"error": "not_ready"}

        channel = self._client.get_channel(self.channel_id)
        if not channel:
            try:
                channel = await self._client.fetch_channel(self.channel_id)
            except Exception as e:
                logger.error(f"Cannot access channel: {e}")
                return {"error": str(e)}

        if not isinstance(channel, discord.TextChannel):
            return {"error": "not_text_channel"}

        # Collect all messages
        all_messages = []
        try:
            async for message in channel.history(limit=None):
                all_messages.append(message)
        except discord.Forbidden:
            return {"error": "cannot_read_history"}

        if len(all_messages) <= keep_count:
            return {"deleted": 0, "total": len(all_messages)}

        # Keep the most recent ones
        messages_to_delete = all_messages[keep_count:]

        deleted_count = 0
        bulk_cutoff = datetime.now(timezone.utc) - timedelta(days=14)

        # Separate into bulk-deletable and individual
        bulk_delete = [m for m in messages_to_delete if m.created_at > bulk_cutoff]
        individual_delete = [m for m in messages_to_delete if m.created_at <= bulk_cutoff]

        # Bulk delete
        for i in range(0, len(bulk_delete), 100):
            batch = bulk_delete[i:i+100]
            try:
                await channel.delete_messages(batch)
                deleted_count += len(batch)
                logger.info(f"Purge: bulk deleted {len(batch)} messages")
            except Exception as e:
                logger.error(f"Bulk delete error: {e}")
                individual_delete.extend(batch)
            await asyncio.sleep(1)

        # Individual delete
        for msg in individual_delete:
            try:
                await msg.delete()
                deleted_count += 1
                await asyncio.sleep(0.25)
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"Delete error: {e}")

        logger.info(f"Purge complete: deleted {deleted_count} messages, kept {keep_count}")

        return {
            "deleted": deleted_count,
            "kept": keep_count,
            "total": len(all_messages),
        }
