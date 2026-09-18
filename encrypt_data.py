import os
from pathlib import Path
from cryptography.fernet import Fernet

key = os.environ["DATA_KEY"].encode()
plain = Path("data.json").read_bytes()
Path("data.enc").write_bytes(Fernet(key).encrypt(plain))
print("data.enc cree.")
