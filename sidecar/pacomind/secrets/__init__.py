"""PacoMind secrets management — pluggable credential vault."""

from pacomind.secrets.manager import SecretsManager
from pacomind.secrets.types import SecretType, ALL_SECRET_KEYS, VAULT_NAME

__all__ = ["SecretsManager", "SecretType", "ALL_SECRET_KEYS", "VAULT_NAME"]
