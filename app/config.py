from __future__ import annotations

import os
from pathlib import Path

# Database location. Inside Docker this points at the /data volume;
# locally it defaults to ./data/resolver.db next to the project root.
_PROJECT_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "resolver.db"
DB_PATH = Path(os.getenv("RESOLVER_DB", str(_PROJECT_DEFAULT_DB)))

# HTTP service configuration.
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))
API_PREFIX = "/api/v1"
