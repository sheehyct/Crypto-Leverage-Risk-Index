"""
Discord Channel Purge Utility

One-time script to clear accumulated messages from the CLRI alerts channel.
Use this to clean up messages that accumulated during development.

Usage:
    python scripts/purge_discord.py [--keep N]

Options:
    --keep N    Keep the most recent N messages (default: 10)
"""

import asyncio
import argparse
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from core.discord_cleanup import DiscordCleanupBot


async def main():
    parser = argparse.ArgumentParser(description="Purge Discord channel messages")
    parser.add_argument(
        "--keep",
        type=int,
        default=10,
        help="Number of recent messages to keep (default: 10)",
    )
    args = parser.parse_args()

    # Load environment
    load_dotenv()

    bot_token = os.getenv("DISCORD_BOT_TOKEN")
    channel_id_str = os.getenv("DISCORD_CHANNEL_ID")

    if not bot_token:
        print("ERROR: DISCORD_BOT_TOKEN not set in environment")
        sys.exit(1)

    if not channel_id_str:
        print("ERROR: DISCORD_CHANNEL_ID not set in environment")
        sys.exit(1)

    channel_id = int(channel_id_str)

    print(f"Discord Channel Purge Utility")
    print(f"Channel ID: {channel_id}")
    print(f"Keeping: {args.keep} most recent messages")
    print()

    # Confirm
    confirm = input("Are you sure you want to delete messages? (yes/no): ")
    if confirm.lower() != "yes":
        print("Aborted.")
        sys.exit(0)

    print()
    print("Starting cleanup bot...")

    # Create cleanup bot
    bot = DiscordCleanupBot(
        bot_token=bot_token,
        channel_id=channel_id,
        max_age_hours=24,  # Not used for purge
        check_interval_seconds=3600,  # Not used for purge
        keep_recent_count=args.keep,
    )

    try:
        # Start the bot
        await bot.start()

        # Wait a moment for connection
        await asyncio.sleep(2)

        print("Running purge...")
        result = await bot.purge_all(keep_count=args.keep)

        print()
        print("Results:")
        print(f"  Total messages found: {result.get('total', 'N/A')}")
        print(f"  Messages deleted: {result.get('deleted', 'N/A')}")
        print(f"  Messages kept: {result.get('kept', args.keep)}")

        if result.get("error"):
            print(f"  Error: {result['error']}")

    finally:
        await bot.stop()

    print()
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
