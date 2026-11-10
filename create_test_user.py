"""Retired development helper; importing this module never connects to a DB."""

import sys


def create_admin():
    print(
        "This fixed-password helper is retired. Select an isolated development "
        "database and use: flask --app app:create_cli_app create-user. "
        "Existing accounts are never reset by this script.",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    create_admin()
