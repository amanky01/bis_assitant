"""Backfill anonymous message expiry. Dry run unless --apply is supplied."""
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.db.mongo import MongoDB


async def migrate(apply: bool) -> None:
    settings = get_settings()
    await MongoDB.connect()
    try:
        messages = MongoDB.col(settings.col_messages)
        sessions = MongoDB.col(settings.col_sessions)
        counts = {"backfill": 0, "orphan": 0, "account": 0}
        criteria = {"user_id": None, "expires_at": None}
        cursor = await messages.aggregate([
            {"$match": criteria},
            {"$group": {"_id": "$session_id", "count": {"$sum": 1}}},
        ])
        now = datetime.now(timezone.utc)
        async for group in cursor:
            session_id = group["_id"]
            parent = await sessions.find_one(
                {"session_id": session_id}, {"user_id": 1, "expires_at": 1}
            )
            if parent and parent.get("user_id") is not None:
                counts["account"] += group["count"]
                continue

            expiry = parent.get("expires_at") if parent else None
            if isinstance(expiry, datetime):
                counts["backfill"] += group["count"]
            else:
                # Mark legacy orphans as expired; MongoDB TTL handles deletion.
                expiry = now
                counts["orphan"] += group["count"]
            if apply:
                await messages.update_many(
                    {**criteria, "session_id": session_id}, {"$set": {"expires_at": expiry}}
                )
        print(f"{'Applied' if apply else 'Dry run'}: {counts}")
    finally:
        await MongoDB.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(migrate(parser.parse_args().apply))
