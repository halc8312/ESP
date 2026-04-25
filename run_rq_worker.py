"""
Backward-compatible wrapper for the dedicated worker entrypoint.
"""
from worker import main


if __name__ == "__main__":
    raise SystemExit(main())
