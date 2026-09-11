"""Secret type definitions for Colony's credential vault."""

from __future__ import annotations

from enum import Enum

VAULT_NAME = "Colony"

# All known secret keys — used by KeyringBackend.list() and migration
ALL_SECRET_KEYS: frozenset[str] = frozenset({
    # LLM providers
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    # Infrastructure
    "NEO4J_URI",
    "NEO4J_USERNAME",
    "NEO4J_PASSWORD",
    "COLONY_TOKEN_SECRET",
    # Messaging gateways
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "WHATSAPP_API_KEY",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "SIGNAL_API_KEY",
    # Email
    "EMAIL_IMAP_PASSWORD",
    "EMAIL_SMTP_PASSWORD",
    # Backup
    "BACKUP_PASSPHRASE",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    # Push notifications
    "APNS_AUTH_KEY",
    "APNS_KEY_ID",
    "APNS_TEAM_ID",
    "FCM_SERVICE_ACCOUNT_JSON",
    # Calendar / Health
    "CALDAV_PASSWORD",
    "OURA_PERSONAL_ACCESS_TOKEN",
    # 1Password Connect
    "OP_CONNECT_HOST",
    "OP_CONNECT_TOKEN",
})


class SecretType(Enum):
    LLM_API_KEY = "llm_api_key"
    NEO4J_CREDENTIAL = "neo4j_credential"
    GATEWAY_TOKEN = "gateway_token"
    MESH_PAIRING_KEY = "mesh_pairing_key"
    EMAIL_CREDENTIAL = "email_credential"
    BACKUP_PASSPHRASE = "backup_passphrase"
    COLONY_TOKEN = "colony_token"
    FEDERATION_CERT = "federation_cert"
    PUSH_CREDENTIAL = "push_credential"
    CALDAV_CREDENTIAL = "caldav_credential"
    HEALTH_CREDENTIAL = "health_credential"
    OTHER = "other"


# Map from env var name prefix to SecretType
SECRET_TYPE_MAP: dict[str, SecretType] = {
    "ANTHROPIC_": SecretType.LLM_API_KEY,
    "OPENAI_": SecretType.LLM_API_KEY,
    "OPENROUTER_": SecretType.LLM_API_KEY,
    "GEMINI_": SecretType.LLM_API_KEY,
    "GROQ_": SecretType.LLM_API_KEY,
    "TOGETHER_": SecretType.LLM_API_KEY,
    "NEO4J_": SecretType.NEO4J_CREDENTIAL,
    "COLONY_TOKEN": SecretType.COLONY_TOKEN,
    "TELEGRAM_": SecretType.GATEWAY_TOKEN,
    "DISCORD_": SecretType.GATEWAY_TOKEN,
    "SLACK_": SecretType.GATEWAY_TOKEN,
    "WHATSAPP_": SecretType.GATEWAY_TOKEN,
    "TWILIO_": SecretType.GATEWAY_TOKEN,
    "SIGNAL_": SecretType.GATEWAY_TOKEN,
    "EMAIL_": SecretType.EMAIL_CREDENTIAL,
    "BACKUP_": SecretType.BACKUP_PASSPHRASE,
    "S3_": SecretType.BACKUP_PASSPHRASE,
    "APNS_": SecretType.PUSH_CREDENTIAL,
    "FCM_": SecretType.PUSH_CREDENTIAL,
    "CALDAV_": SecretType.CALDAV_CREDENTIAL,
    "OURA_": SecretType.HEALTH_CREDENTIAL,
}


def infer_secret_type(key: str) -> SecretType:
    """Infer the SecretType for a given key name based on prefix matching."""
    for prefix, stype in SECRET_TYPE_MAP.items():
        if key.startswith(prefix):
            return stype
    return SecretType.OTHER
