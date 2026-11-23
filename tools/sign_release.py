"""Sign a release's SHA256SUMS so Baitd's updater installs only what the key holder published.

    python tools/sign_release.py keygen [KEY]     once: make the private key, print the public key
    python tools/sign_release.py sign SHA256SUMS [KEY]   every release: write SHA256SUMS.sig next to it

The private key never goes into the repository, GitHub or a synced folder. Keep a backup: without it the
next update cannot be signed, and installed copies would refuse it (they would need a manual install).
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_KEY = Path.home() / ".baitd-firma" / "baitd-release-key.pem"


def keygen(key_path: Path) -> str:
    """Write a new private key (refuses to overwrite one) and return the public key in hex."""
    if key_path.exists():
        raise SystemExit(f"Ya existe una clave en {key_path}: no se sobrescribe")
    key = Ed25519PrivateKey.generate()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    key_path.write_bytes(pem)
    try:
        key_path.chmod(0o600)  # POSIX; on Windows the user profile is already private
    except OSError:
        pass
    return public_hex(key)


def public_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()


def load(key_path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("La clave no es Ed25519")
    return key


def sign(sums_path: Path, key_path: Path) -> Path:
    """Write SHA256SUMS.sig (the base64 signature of the exact SHA256SUMS bytes) next to it."""
    sig = load(key_path).sign(sums_path.read_bytes())
    out = sums_path.with_name(sums_path.name + ".sig")
    out.write_text(base64.b64encode(sig).decode("ascii") + "\n", encoding="ascii")
    return out


def main(argv: list[str]) -> None:
    if len(argv) >= 1 and argv[0] == "keygen":
        key_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_KEY
        print(f"Clave privada: {key_path}")
        print(f"Clave pública (va en src/baitd/updater.py): {keygen(key_path)}")
    elif len(argv) >= 2 and argv[0] == "sign":
        key_path = Path(argv[2]) if len(argv) > 2 else DEFAULT_KEY
        print(f"Firma: {sign(Path(argv[1]), key_path)}")
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
