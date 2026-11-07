"""Load the OSHA CSV into MongoDB Atlas.

    python scripts/load_data.py                     # uses MONGODB_URI from .env
    python scripts/load_data.py --csv other.csv --drop

Re-running is safe: documents are upserted by _id.
"""

import argparse
import sys
from pathlib import Path

from pymongo import MongoClient, ReplaceOne

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
from app.ingest import INDEXES, read_csv, rows_to_documents  # noqa: E402

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "OSHA HSE DATA_ALL ABSTRACTS 15-17_FINAL.csv"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--drop", action="store_true", help="drop the collection first")
    args = parser.parse_args()

    client = MongoClient(config.MONGODB_URI, serverSelectionTimeoutMS=10000)
    coll = client[config.MONGODB_DB][config.MONGODB_COLLECTION]
    if args.drop:
        coll.drop()

    docs = list(rows_to_documents(read_csv(args.csv)))
    for start in range(0, len(docs), 500):
        batch = docs[start : start + 500]
        coll.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in batch], ordered=False)
    for keys in INDEXES:
        coll.create_index(keys)

    print(f"Loaded {len(docs)} documents into {config.MONGODB_DB}.{config.MONGODB_COLLECTION}")
    print(f"Collection now holds {coll.count_documents({})} documents")


if __name__ == "__main__":
    main()
