from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


MAGIC = b"SLABENC1"
SALT_SIZE = 16
ITERATIONS = 390000


def passphrase_from_env(env_name: str = "STRATEGY_BUNDLE_PASSPHRASE") -> str:
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeError(
            f"Missing {env_name}. Set it in your private environment before "
            "encrypting or decrypting bundles."
        )
    return value


def encrypt_file(
    *,
    input_path: Path,
    output_path: Path,
    passphrase: str,
) -> Path:
    salt = os.urandom(SALT_SIZE)
    key = derive_key(passphrase, salt)
    token = Fernet(key).encrypt(input_path.read_bytes())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(MAGIC + salt + token)
    return output_path


def decrypt_file(
    *,
    input_path: Path,
    output_path: Path,
    passphrase: str,
) -> Path:
    payload = input_path.read_bytes()
    if not payload.startswith(MAGIC):
        raise ValueError("Input is not a strategy lab encrypted bundle.")
    salt_start = len(MAGIC)
    salt_end = salt_start + SALT_SIZE
    salt = payload[salt_start:salt_end]
    token = payload[salt_end:]
    key = derive_key(passphrase, salt)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(Fernet(key).decrypt(token))
    return output_path


def derive_key(passphrase: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))

