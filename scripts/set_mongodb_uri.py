"""Write MONGODB_URI to .env without the password ever appearing in a chat, shell history or the screen.

Usage:  .venv/bin/python scripts/set_mongodb_uri.py
Prompts for the Atlas host, database user and password (hidden), URL-encodes the credentials (so `@`, `:`, `/`
in a password work), writes the line to .env (git-ignored) and tests the connection.
"""

from __future__ import annotations

import getpass
from pathlib import Path
from urllib.parse import quote_plus

ENV = Path(__file__).resolve().parents[1] / ".env"


def main() -> int:
    host = input("Atlas host [robohack.iztie1d.mongodb.net]: ").strip() or "robohack.iztie1d.mongodb.net"
    user = input("Database user [vnmoorthy]: ").strip() or "vnmoorthy"
    password = getpass.getpass("Database user password (hidden): ")
    uri = f"mongodb+srv://{quote_plus(user)}:{quote_plus(password)}@{host}/?retryWrites=true&w=majority&appName=amperes"
    lines = [ln for ln in (ENV.read_text().splitlines() if ENV.exists() else []) if not ln.startswith("MONGODB_URI=")]
    lines += [f"MONGODB_URI={uri}"]
    ENV.write_text("\n".join(lines).rstrip("\n") + "\n")
    print(f"MONGODB_URI written to {ENV} (password not shown). Testing connection...")
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError

    try:
        with MongoClient(uri, serverSelectionTimeoutMS=10_000) as c:
            c.admin.command("ping")
        print("OK: authenticated and reachable.")
        return 0
    except PyMongoError as exc:
        msg = str(exc).split(", full error")[0]
        print(f"FAILED: {msg}\nCheck the user/password in Atlas > Database Access, and that your IP is allowed in Network Access.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
