from __future__ import annotations

from getpass import getpass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"


def _replace_or_append(lines: list[str], name: str, value: str) -> list[str]:
    prefix = f"{name}="
    replacement = f"{prefix}{value}"
    found = False
    updated: list[str] = []
    for line in lines:
        if line.lstrip().startswith(prefix):
            if not found:
                updated.append(replacement)
                found = True
            continue
        updated.append(line)
    if not found:
        updated.append(replacement)
    return updated


def main() -> None:
    print(f"Updating Groww credentials in: {ENV_PATH}")
    access_token = getpass("GROWW_ACCESS_TOKEN (blank allowed): ").strip()
    api_key = getpass("GROWW_API_KEY: ").strip()
    api_secret = getpass("GROWW_API_SECRET: ").strip()

    if not access_token and not (api_key and api_secret):
        raise SystemExit(
            "Configuration not saved: enter GROWW_ACCESS_TOKEN, or both GROWW_API_KEY and GROWW_API_SECRET."
        )

    lines = ENV_PATH.read_text(encoding="utf-8-sig").splitlines() if ENV_PATH.exists() else []
    for name, value in (
        ("MARKET_DATA_PROVIDER", "groww"),
        ("GROWW_ACCESS_TOKEN", access_token),
        ("GROWW_API_KEY", api_key),
        ("GROWW_API_SECRET", api_secret),
    ):
        lines = _replace_or_append(lines, name, value)

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("Saved successfully. Credential values were not displayed.")


if __name__ == "__main__":
    main()
