"""
Integration secret encryption.

Fernet symmetric encryption for is_secret_ref values in integration_configs.
Key is loaded once from config at startup. Never called for non-secret fields.

This is the ONLY crypto boundary in the codebase. No other file performs
encryption or decryption. If you need a decrypted value, call get_setting_value().
"""

from cryptography.fernet import Fernet, InvalidToken
from app.config import get_settings


def _get_fernet() -> Fernet:
    key = get_settings().settings_encryption_key
    if not key:
        raise RuntimeError(
            "SETTINGS_ENCRYPTION_KEY is not configured. "
            "Cannot encrypt or decrypt integration secrets."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a secret value for storage. Returns base64 ciphertext string."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt a stored secret value. Raises ValueError on tampered/invalid data."""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Failed to decrypt integration secret — key mismatch or data corrupted") from e
