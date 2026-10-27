#!/usr/bin/env python3
"""Bootstrap narrow runtime credentials from a private management environment.

Never prints secret values, writes a secret file, or puts values on a command
line. Requires the AWS CLI, psycopg, a private route to RDS, and its CA bundle.
Run with `uv run python deploy/bootstrap-secrets.py --help` after `uv sync`.
"""

from __future__ import annotations

import argparse
import getpass
import json
import re
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlencode, urlsplit


class BootstrapError(Exception):
    pass


def aws(args, operation, *, payload=None, absent_ok=False):
    command = ["aws", "--region", args.region, "--no-cli-pager"]
    if args.profile:
        command.extend(["--profile", args.profile])
    command.extend(operation)
    command.extend(["--output", "json"])
    if payload is not None:
        command.extend(["--cli-input-json", "file:///dev/stdin"])
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        if absent_ok and "ResourceNotFoundException" in result.stderr:
            return None
        # Some operations touch passwords. Do not echo arguments, response data,
        # SDK tracebacks, or raw provider diagnostics to this terminal.
        raise BootstrapError(
            f"AWS {operation[0]} {operation[1]} failed; verify identity, permissions, and resource metadata."
        )
    return json.loads(result.stdout or "{}")


def read_secret(args, name, *, absent_ok=False):
    response = aws(args, ["secretsmanager", "get-secret-value", "--secret-id", name], absent_ok=absent_ok)
    if response is None:
        return {}
    try:
        value = json.loads(response["SecretString"])
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (KeyError, ValueError):
        raise BootstrapError(f"Secret {name} must be a JSON object.") from None


def write_secret(args, name, value):
    aws(
        args,
        ["secretsmanager", "put-secret-value"],
        payload={"SecretId": name, "SecretString": json.dumps(value)},
    )


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", help="AWS CLI profile; otherwise use its normal credential chain")
    parser.add_argument("--region", default="us-west-1")
    parser.add_argument("--project", default="box-agent-sdlc")
    parser.add_argument("--database-id", help="Defaults to tmprl-dem-cld/<project> in RDS identifier form")
    parser.add_argument(
        "--ca-bundle",
        type=Path,
        required=True,
        help="Local RDS CA bundle used for bootstrap TLS verification",
    )
    parser.add_argument(
        "--rotate-database-password",
        action="store_true",
        help="Explicitly rotate the app DB credential during a maintenance window",
    )
    parser.add_argument(
        "--set-github-token",
        action="store_true",
        help="Prompt privately for a narrowly scoped sandbox GitHub token",
    )
    parser.add_argument(
        "--github-command-actors",
        help="Comma-separated GitHub logins allowed to approve or steer runs; required on first setup",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,24}", args.project):
        raise BootstrapError("Use a lowercase project name of 2–25 characters.")
    expected_database = f"tmprl-dem-cld-{args.project}"
    args.database_id = args.database_id or expected_database
    if args.database_id != expected_database:
        raise BootstrapError(
            "Database identifier must match this project's independent infrastructure stack."
        )
    if not args.ca_bundle.is_file():
        raise BootstrapError("The local RDS CA bundle does not exist.")
    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        raise BootstrapError("Install the application's dependencies first: uv sync.") from None

    instances = aws(args, ["rds", "describe-db-instances", "--db-instance-identifier", args.database_id])[
        "DBInstances"
    ]
    instance = instances[0]
    if instance.get("PubliclyAccessible"):
        raise BootstrapError("Refusing to bootstrap a publicly accessible database.")
    host = instance["Endpoint"]["Address"]
    port = instance["Endpoint"]["Port"]
    database = instance["DBName"]
    master = read_secret(args, instance["MasterUserSecret"]["SecretArn"])
    runtime_name = f"tmprl-dem-cld/{args.project}/runtime"
    runtime = read_secret(args, runtime_name, absent_ok=True)
    actors = (
        args.github_command_actors
        if args.github_command_actors is not None
        else runtime.get("GITHUB_COMMAND_ACTORS", "")
    )
    if not actors or not all(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", actor) for actor in actors.split(",")
    ):
        raise BootstrapError(
            "Set --github-command-actors to a comma-separated allowlist of authorized GitHub logins."
        )
    existing_url = runtime.get("DATABASE_URL", "")
    parsed = urlsplit(existing_url)
    if existing_url and (
        parsed.hostname != host
        or unquote(parsed.path.lstrip("/")) != database
        or parsed.username != "box_app"
    ):
        raise BootstrapError(
            "Existing runtime DATABASE_URL targets another database/user; review it through the approved secret workflow."
        )
    password = unquote(parsed.password or "")
    rotating = args.rotate_database_password or not password
    if rotating:
        password = secrets.token_urlsafe(36)

    try:
        with psycopg.connect(
            host=host,
            port=port,
            dbname=database,
            user=master["username"],
            password=master["password"],
            sslmode="verify-full",
            sslrootcert=str(args.ca_bundle.resolve()),
            connect_timeout=10,
            autocommit=True,
        ) as connection:
            role_exists = connection.execute("SELECT 1 FROM pg_roles WHERE rolname = 'box_app'").fetchone()
            if role_exists and not existing_url and not args.rotate_database_password:
                raise BootstrapError(
                    "box_app exists but its runtime credential is missing. Recover the secret or explicitly rotate with --rotate-database-password."
                )
            if not role_exists:
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE box_app LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Literal(password))
                )
            elif rotating:
                connection.execute(sql.SQL("ALTER ROLE box_app PASSWORD {}").format(sql.Literal(password)))
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
            )
            connection.execute(
                sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO box_app").format(sql.Identifier(database))
            )
            connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
            connection.execute("GRANT USAGE, CREATE ON SCHEMA public TO box_app")
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError(
            "Database bootstrap failed; verify private routing, approved management security group, CA, and RDS status. Secret values were suppressed."
        ) from None

    query = urlencode({"sslmode": "verify-full", "sslrootcert": "/app/certs/rds-global-bundle.pem"})
    runtime["DATABASE_URL"] = (
        f"postgresql://box_app:{quote(password, safe='')}@{host}:{port}/{quote(database, safe='')}?{query}"
    )
    runtime.setdefault("GITHUB_WEBHOOK_SECRET", secrets.token_urlsafe(32))
    runtime.setdefault("EXECUTOR_TOKEN", secrets.token_urlsafe(32))
    runtime["GITHUB_COMMAND_ACTORS"] = actors
    write_secret(args, runtime_name, runtime)
    if args.set_github_token:
        token = getpass.getpass("Fine-grained GitHub sandbox token (hidden): ").strip()
        if not token:
            raise BootstrapError(
                "GitHub token was empty; runtime bootstrap succeeded, token update was skipped."
            )
        write_secret(args, f"tmprl-dem-cld/{args.project}/github", {"GITHUB_TOKEN": token})
    print("Runtime secret updated. Passwords and tokens were not printed or written locally.")
    print("Keep the RDS-managed master credential restricted to bootstrap operators.")


if __name__ == "__main__":
    try:
        main()
    except (BootstrapError, KeyError, ValueError, OSError) as error:
        if isinstance(error, BootstrapError):
            message = str(error)
        else:
            message = "Bootstrap failed; inspect nonsecret prerequisites and run again. No secret values were printed."
        print(message, file=sys.stderr)
        raise SystemExit(1)
