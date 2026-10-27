#!/usr/bin/env python3
"""Create local-only credentials without putting their values in terminal output."""

import os
import secrets
from pathlib import Path


def main():
    path = Path(__file__).resolve().parents[1] / ".env"
    values = {
        "POSTGRES_PASSWORD": secrets.token_hex(24),
        "EXECUTOR_TOKEN": secrets.token_hex(32),
        "GITHUB_WEBHOOK_SECRET": secrets.token_hex(32),
        "OPERATOR_TOKEN": secrets.token_hex(32),
        "DEMO_MODE": "scripted",
        "GITHUB_SANDBOX_REPO": "temporal-sa/box-agent-sdlc-storefront",
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit(".env already exists; retained existing credentials.")
    with os.fdopen(fd, "w") as output:
        output.write("# Local credentials; never commit or share this file.\n")
        for key, value in values.items():
            output.write(f"{key}={value}\n")
    print("Created .env with mode 0600. Values were not printed.")


if __name__ == "__main__":
    main()
