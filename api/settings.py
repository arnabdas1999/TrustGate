"""Settings read from the environment at call time, so tests can change them."""

from __future__ import annotations

import os


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def mock_mode() -> bool:
    return env("MOCK", "1") == "1"


def approver_password() -> str:
    return env("APPROVER_PASSWORD")


def approver_name() -> str:
    return env("APPROVER_NAME", "approver")


def signing_secret() -> str:
    return env("APPROVAL_SIGNING_SECRET")


def gate_url() -> str:
    return env("GATE_URL", "http://localhost:8100").rstrip("/")


def web_origins() -> list[str]:
    return [o.strip() for o in env("WEB_ORIGIN", "http://localhost:3000").split(",") if o.strip()]


SESSION_HOURS = 8
APPROVAL_MINUTES = 15
