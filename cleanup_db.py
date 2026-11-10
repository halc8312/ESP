"""Retired one-off cleanup helper; importing it never connects to a DB."""

import sys


def delete_product():
    print(
        "This hard-coded product deletion helper is retired. "
        "Use the authenticated product management UI to select the intended "
        "shop and product before deleting data.",
        file=sys.stderr,
    )
    raise SystemExit(2)


if __name__ == "__main__":
    delete_product()
