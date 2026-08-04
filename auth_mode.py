"""Detect how Claude Code is being billed: subscription, API key, or gateway.

Why this isn't just an env-var lookup
-------------------------------------
The obvious check is `ANTHROPIC_API_KEY` vs `CLAUDE_CODE_OAUTH_TOKEN`. That
correctly catches API-key users, but it reports "unknown" for most subscription
users — when you run `claude login`, the OAuth token is stored in the OS
keychain (macOS) or `~/.claude/.credentials.json`, *not* in an environment
variable. `CLAUDE_CODE_OAUTH_TOKEN` is the opt-in headless/CI path.

The authoritative signal for a logged-in subscription is `~/.claude.json` →
`oauthAccount`, which carries `billingType`, `organizationType`, and the
subscription start date. This module prefers that and falls back to env vars.

Resolution order mirrors Claude Code's own credential precedence — an API key
wins over a subscription login, which is exactly the silent-overbilling trap
worth warning about.

No secret is ever read, logged, or returned. Only presence is recorded.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Any

HOME = Path.home()
CLAUDE_JSON = HOME / ".claude.json"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"
CREDENTIALS_JSON = HOME / ".claude" / ".credentials.json"
KEYCHAIN_SERVICE = "Claude Code-credentials"

CREDENTIAL_VARS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
ROUTING_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
)

SHELL_PROFILES = (
    ".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile",
    ".profile", ".config/fish/config.fish",
)

# `export ANTHROPIC_API_KEY=…`, `set -x ANTHROPIC_API_KEY …`, or a bare assignment.
_PROFILE_ASSIGN = re.compile(
    r"^\s*(?:export\s+|set\s+-[gx]+\s+)?(ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN)\b\s*[= ]",
)

# organizationType / billingType -> human label
PLAN_LABELS = {
    "claude_pro": "Claude Pro",
    "claude_max": "Claude Max",
    "claude_team": "Claude Team",
    "claude_enterprise": "Claude Enterprise",
}

_cache: dict[str, Any] = {"at": 0.0, "value": None}
_TTL_SECONDS = 30


# --------------------------------------------------------------------------- #
# individual probes
# --------------------------------------------------------------------------- #

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _keychain_has_credentials() -> bool:
    """macOS only. Checks for the item's *existence* — the secret is not read
    (no `-g` flag), so this never triggers a keychain access prompt."""
    if platform.system() != "Darwin":
        return False
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _scan_shell_profiles() -> list[dict[str, Any]]:
    """Find credential assignments in shell startup files.

    This is the point of the whole feature: a stale `export ANTHROPIC_API_KEY`
    from an old project silently overrides a Pro subscription in every new
    shell, and nothing in Claude Code's UI tells you. Only the file and line
    number are recorded — never the value.
    """
    found = []
    for rel in SHELL_PROFILES:
        path = HOME / rel
        try:
            if not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                match = _PROFILE_ASSIGN.match(line)
                if match:
                    found.append({"var": match.group(1), "file": f"~/{rel}", "line": lineno})
        except OSError:
            continue
    return found


def _settings_env() -> dict[str, Any]:
    """Claude Code applies `env` from settings.json to its own process, so a key
    set there counts even when the surrounding shell is clean."""
    settings = _read_json(CLAUDE_SETTINGS)
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    return {
        "vars": sorted(k for k in env if k in CREDENTIAL_VARS or k in ROUTING_VARS),
        "api_key_helper": bool(settings.get("apiKeyHelper")),
    }


def _oauth_account() -> dict[str, Any] | None:
    account = _read_json(CLAUDE_JSON).get("oauthAccount")
    if not isinstance(account, dict):
        return None
    org_type = account.get("organizationType")
    return {
        "plan": PLAN_LABELS.get(org_type, (org_type or "subscription").replace("_", " ").title()),
        "organization_type": org_type,
        "billing_type": account.get("billingType"),
        "organization_name": account.get("organizationName"),
        "subscription_started": account.get("subscriptionCreatedAt"),
        "trial_ends": account.get("claudeCodeTrialEndsAt"),
        "extra_usage_enabled": bool(account.get("hasExtraUsageEnabled")),
        "rate_limit_tier": account.get("organizationRateLimitTier"),
    }


# --------------------------------------------------------------------------- #
# detection
# --------------------------------------------------------------------------- #

def detect(refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    if not refresh and _cache["value"] is not None and now - _cache["at"] < _TTL_SECONDS:
        return _cache["value"]

    env_set = {name: bool(os.environ.get(name)) for name in CREDENTIAL_VARS + ROUTING_VARS}
    profiles = _scan_shell_profiles()
    settings = _settings_env()
    account = _oauth_account()
    stored_login = _keychain_has_credentials() or CREDENTIALS_JSON.is_file()

    settings_has_key = "ANTHROPIC_API_KEY" in settings["vars"]
    settings_has_token = "ANTHROPIC_AUTH_TOKEN" in settings["vars"]

    # Same precedence Claude Code itself applies.
    if env_set["ANTHROPIC_API_KEY"] or settings_has_key:
        mode, label = "api_key", "API key"
        billing = "Pay-per-token"
        detail = "Billed per token to the developer console account that owns the key."
    elif env_set["ANTHROPIC_AUTH_TOKEN"] or settings_has_token:
        mode, label = "gateway", "Gateway token"
        billing = "Via gateway"
        detail = "A bearer token is set, so requests go through a proxy or gateway that owns billing."
    elif env_set["CLAUDE_CODE_USE_BEDROCK"] or env_set["CLAUDE_CODE_USE_VERTEX"]:
        provider = "Amazon Bedrock" if env_set["CLAUDE_CODE_USE_BEDROCK"] else "Google Vertex AI"
        mode, label = "cloud_provider", provider
        billing = "Cloud provider"
        detail = f"Requests are served through {provider} and billed by that provider."
    elif env_set["CLAUDE_CODE_OAUTH_TOKEN"] or stored_login or account:
        mode, label = "subscription", (account or {}).get("plan") or "Subscription"
        billing = "Subscription"
        detail = "Usage is covered by the subscription — the costs below are API-equivalent value, not a bill."
    else:
        mode, label = "unknown", "Unknown"
        billing = "Unknown"
        detail = "No credential source found. Claude Code may be authenticated in a different environment."

    warnings = _warnings(mode, env_set, profiles, settings, account, stored_login)

    result = {
        "mode": mode,
        "label": label,
        "billing": billing,
        "detail": detail,
        "is_subscription": mode == "subscription",
        "costs_are_billed": mode in ("api_key", "cloud_provider"),
        "account": account,
        "signals": {
            "env": sorted(k for k, v in env_set.items() if v),
            "settings_env": settings["vars"],
            "api_key_helper": settings["api_key_helper"],
            "stored_login": stored_login,
            "shell_profiles": profiles,
        },
        "warnings": warnings,
        "checked_at": now,
    }
    _cache.update(at=now, value=result)
    return result


def _warnings(
    mode: str,
    env_set: dict[str, bool],
    profiles: list[dict],
    settings: dict,
    account: dict | None,
    stored_login: bool,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []

    if mode == "api_key" and (account or stored_login):
        out.append({
            "level": "error",
            "title": "An API key is overriding your subscription",
            "body": "You are signed in to "
                    + ((account or {}).get("plan") or "a Claude subscription")
                    + ", but ANTHROPIC_API_KEY takes precedence — this usage is being "
                      "billed per token instead of covered by the plan. Unset the key to "
                      "use the subscription.",
        })

    # The latent version of the same trap: the dashboard's own environment is
    # clean, but every new shell will export a key.
    if mode != "api_key" and profiles:
        where = ", ".join(f"{p['file']}:{p['line']}" for p in profiles[:4])
        out.append({
            "level": "warn",
            "title": "A credential is exported from a shell profile",
            "body": f"Found an assignment in {where}. This dashboard's environment "
                    "doesn't have it, but shells started from that profile will — and "
                    "Claude Code would then bill per token rather than using the plan.",
        })

    if settings["api_key_helper"]:
        out.append({
            "level": "warn",
            "title": "apiKeyHelper is configured",
            "body": "~/.claude/settings.json defines apiKeyHelper, which supplies an API "
                    "key at runtime and can override subscription auth.",
        })

    if env_set.get("ANTHROPIC_BASE_URL"):
        out.append({
            "level": "warn",
            "title": "ANTHROPIC_BASE_URL is set",
            "body": "Requests are pointed at a non-default endpoint, so billing depends "
                    "on whatever serves that URL.",
        })

    if mode == "unknown":
        out.append({
            "level": "info",
            "title": "Billing mode could not be determined",
            "body": "No API key, gateway token, or stored login was found. If Claude Code "
                    "works fine, it is probably authenticated in an environment this "
                    "dashboard cannot see.",
        })

    return out


def cost_caption(info: dict[str, Any]) -> str:
    """Short caption for the cost card — honest about what the number means."""
    if info["is_subscription"]:
        return "covered by plan"
    if info["mode"] == "api_key":
        return "billed per token"
    if info["mode"] == "cloud_provider":
        return f"billed via {info['label']}"
    return "at API rates"


if __name__ == "__main__":
    print(json.dumps(detect(refresh=True), indent=2, default=str))
