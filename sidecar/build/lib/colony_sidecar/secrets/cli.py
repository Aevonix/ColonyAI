"""CLI command handlers for `colony secrets`."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse


# -------------------------------------------------------------------------
# Section grouping for pretty-print
# -------------------------------------------------------------------------

_SECTION_KEYS: dict[str, list[str]] = {
    "LLM Keys": [
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
        "GEMINI_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY",
    ],
    "Infrastructure": ["NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "COLONY_TOKEN_SECRET"],
    "Messaging Gateways": [
        "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN",
        "WHATSAPP_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "SIGNAL_API_KEY",
    ],
    "Email": ["EMAIL_IMAP_PASSWORD", "EMAIL_SMTP_PASSWORD"],
    "Backup": ["BACKUP_PASSPHRASE", "S3_ACCESS_KEY", "S3_SECRET_KEY"],
    "Push Notifications": ["APNS_AUTH_KEY", "APNS_KEY_ID", "APNS_TEAM_ID", "FCM_SERVICE_ACCOUNT_JSON"],
    "Calendar / Health": ["CALDAV_PASSWORD", "OURA_PERSONAL_ACCESS_TOKEN"],
    "1Password Connect": ["OP_CONNECT_HOST", "OP_CONNECT_TOKEN"],
}


def _get_manager():
    from colony_sidecar.secrets.manager import SecretsManager
    return SecretsManager()


def cmd_secrets_list(args: "argparse.Namespace") -> None:
    """colony secrets list — show configured secrets grouped by category."""
    sm = _get_manager()
    configured = set(sm.list())

    print(f"\nBackend: {sm.backend_name}\n")

    unconfigured: list[str] = []
    for section, keys in _SECTION_KEYS.items():
        section_configured = [k for k in keys if k in configured]
        section_missing = [k for k in keys if k not in configured]
        unconfigured.extend(section_missing)
        if section_configured:
            print(f"{section} ({len(section_configured)} configured):")
            for k in section_configured:
                print(f"  \u2713 {k}")
            print()

    if unconfigured:
        shown = unconfigured[:10]
        remaining = len(unconfigured) - len(shown)
        print("Not configured (run `colony secrets set KEY VALUE`):")
        for k in shown:
            print(f"  \u2717 {k}")
        if remaining > 0:
            print(f"  ... ({remaining} more)")
        print()


def cmd_secrets_get(args: "argparse.Namespace") -> None:
    """colony secrets get KEY — retrieve a secret."""
    sm = _get_manager()
    value = sm.get(args.key)
    if value is None:
        print(f"Secret '{args.key}' not found in {sm.backend_name} backend.", file=sys.stderr)
        sys.exit(1)
    print(value)


def cmd_secrets_set(args: "argparse.Namespace") -> None:
    """colony secrets set KEY VALUE — store a secret."""
    from colony_sidecar.secrets.types import infer_secret_type
    sm = _get_manager()
    secret_type = infer_secret_type(args.key)
    sm.set(args.key, args.value, secret_type=secret_type)
    print(f"\u2713 Set {args.key} in {sm.backend_name} backend.")


def cmd_secrets_delete(args: "argparse.Namespace") -> None:
    """colony secrets delete KEY — remove a secret."""
    sm = _get_manager()
    sm.delete(args.key)
    print(f"\u2713 Deleted {args.key} from {sm.backend_name} backend.")


def cmd_secrets_backend(args: "argparse.Namespace") -> None:
    """colony secrets backend — show current backend."""
    sm = _get_manager()
    print(f"Current secrets backend: {sm.backend_name}")
    if sm.backend_name == "1password":
        print("  Storage: 1Password vault")
    elif sm.backend_name == "keyring":
        print("  Storage: System keychain (macOS Keychain / Linux SecretService)")
    else:
        print("  Storage: ~/.colony/.env (plaintext, mode 0600)")
    print("\nChange backend: colony setup  (re-run onboarding wizard)")


def cmd_secrets_status(args: "argparse.Namespace") -> None:
    """colony secrets status — check backend availability."""
    from colony_sidecar.secrets.backends.env import EnvBackend
    from colony_sidecar.secrets.backends.keyring import KeyringBackend
    from colony_sidecar.secrets.backends.onepassword import OnePasswordBackend

    sm = _get_manager()
    print(f"\nActive backend: {sm.backend_name}")
    print()

    backends = [
        ("1password", OnePasswordBackend()),
        ("keyring", KeyringBackend()),
        ("env", EnvBackend()),
    ]
    for name, backend in backends:
        mark = "\u2713" if backend.is_available() else "\u2717"
        active = " (active)" if name == sm.backend_name else ""
        print(f"  {mark} {name}{active}")

    print()
    configured = sm.list()
    print(f"Configured secrets: {len(configured)}")


def cmd_secrets_migrate(args: "argparse.Namespace") -> None:
    """colony secrets migrate — move secrets between backends."""
    from colony_sidecar.secrets.backends.env import EnvBackend
    from colony_sidecar.secrets.backends.keyring import KeyringBackend
    from colony_sidecar.secrets.backends.onepassword import OnePasswordBackend
    from colony_sidecar.secrets.migration import SecretsMigrator

    _backend_map = {
        "env": EnvBackend,
        "keyring": KeyringBackend,
        "1password": OnePasswordBackend,
    }

    from_name = args.from_backend
    to_name = args.to_backend

    if from_name not in _backend_map:
        print(f"Unknown source backend: {from_name}", file=sys.stderr)
        sys.exit(1)
    if to_name not in _backend_map:
        print(f"Unknown target backend: {to_name}", file=sys.stderr)
        sys.exit(1)

    source = _backend_map[from_name]()
    target = _backend_map[to_name]()

    if not target.is_available():
        print(
            f"Target backend '{to_name}' is not available on this system.",
            file=sys.stderr,
        )
        sys.exit(1)

    migrator = SecretsMigrator(source, target)

    dry_run: bool = getattr(args, "dry_run", False)
    delete_source: bool = getattr(args, "delete_source", False)

    if dry_run:
        keys = migrator.preview()
        if not keys:
            print(f"No secrets found in '{from_name}' backend.")
            return
        print(f"Would migrate {len(keys)} secrets from '{from_name}' to '{to_name}':")
        print("  " + ", ".join(keys))
        print("\nRun without --dry-run to proceed.")
        return

    print(f"Migrating secrets from '{from_name}' to '{to_name}'...")
    result = migrator.migrate(dry_run=False, delete_from_source=delete_source)

    for key in result.migrated:
        print(f"  \u2713 {key}")
    for key, err in result.failed:
        print(f"  \u2717 {key}: {err}", file=sys.stderr)

    print(f"\n{result.summary()}")

    if delete_source and result.migrated:
        print(f"Removed {len(result.migrated)} secrets from '{from_name}' backend.")
