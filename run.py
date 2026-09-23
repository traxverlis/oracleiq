#!/usr/bin/env python3
"""Compatibility entry point; use the same supervised launcher as the CLI."""
from oracleiq import main


if __name__ == "__main__":
    raise SystemExit(main(["all"]))
