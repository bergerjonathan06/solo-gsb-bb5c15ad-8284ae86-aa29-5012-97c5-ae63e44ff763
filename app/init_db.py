"""CLI entry point: initialise the database and exit."""
from __future__ import annotations

from . import db
from .config import DB_PATH


def main() -> None:
    db.init_db(DB_PATH)
    print(f"Database initialised at {DB_PATH}")


if __name__ == "__main__":
    main()
