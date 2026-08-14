#!/usr/bin/env python3
"""Quickstart script to verify environment and show next steps."""

import subprocess
import sys


def main():
    print("\n=== Pre-Call Brief Quickstart ===\n")

    # Check Databricks CLI
    try:
        result = subprocess.run(
            ["databricks", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            print("✓ Databricks CLI configured")
        else:
            print("✗ Databricks CLI not configured")
            print("  Run: databricks configure --host <workspace-url>")
            sys.exit(1)
    except FileNotFoundError:
        print("✗ Databricks CLI not found")
        print("  Install: https://docs.databricks.com/dev-tools/cli/")
        sys.exit(1)

    print("\nNext steps:")
    print("\n1. Generate synthetic UC data:")
    print("   cd generate/")
    print("   python land_uc.py --profile=<profile> --warehouse-id=<id>")
    print("   python create_functions.py --profile=<profile> --warehouse-id=<id>")
    print("   # (the OKF bundle under .apx/okf/ is already shipped; regenerate with gen_okf.py)")
    print("\n2. Run tests (offline):")
    print("   uv run pytest")
    print("\n3. Run locally (serves /_apx/agent dev UI):")
    print("   uv run apx-agent agents run")
    print("\n4. Deploy to Databricks Apps:")
    print("   uv run apx-agent agents deploy --target apps --profile=<profile>")
    print("\n")


if __name__ == "__main__":
    main()
