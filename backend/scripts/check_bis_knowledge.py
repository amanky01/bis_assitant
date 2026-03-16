#!/usr/bin/env python3
"""Quick check: bis_knowledge collection has docs with embeddings and correct dims."""
import asyncio
import os
import sys
from pathlib import Path

# project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import motor.motor_asyncio

async def check():
    uri = os.getenv("MONGO_URI") or os.getenv("MONGODB_URI")
    db_name = os.getenv("MONGODB_DB_NAME", "bis_assistant")
    if not uri:
        print("MONGO_URI / MONGODB_URI not set")
        return

    client = motor.motor_asyncio.AsyncIOMotorClient(uri)
    db = client[db_name]

    # Check one doc (Hallmark or any)
    doc = await db["bis_knowledge"].find_one({"title": {"$regex": "Hallmark", "$options": "i"}})
    if not doc:
        doc = await db["bis_knowledge"].find_one({})

    if not doc:
        print("COLLECTION IS EMPTY")
        client.close()
        return

    emb = doc.get("embedding")
    if emb is None:
        print("NO EMBEDDING FIELD on document")
    else:
        print(f"Embedding dims: {len(emb)}")
        print(f"First 3 values: {emb[:3]}")

    count = await db["bis_knowledge"].count_documents({})
    count_with_emb = await db["bis_knowledge"].count_documents({"embedding": {"$exists": True}})
    print(f"Total docs: {count}")
    print(f"Docs with embedding field: {count_with_emb}")

    client.close()

if __name__ == "__main__":
    asyncio.run(check())
