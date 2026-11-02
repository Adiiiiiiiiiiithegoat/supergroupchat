"""One-time login setup: python -m backend.auth_setup  (run it in a real terminal; it asks for input)

Writes PASSWORD_HASH, TOTP_SECRET and SESSION_SECRET into .env. Re-running replaces them and logs out every session.
"""
import base64
import getpass
import os
import secrets
import time

import qrcode
from argon2 import PasswordHasher

from .auth import ROOT, totp

ENV = os.path.join(ROOT, ".env")


def write_env(values):
    lines = open(ENV, encoding="utf-8").read().splitlines() if os.path.exists(ENV) else []
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in values]
    # single quotes: python-dotenv leaves the $ in an argon2 hash alone
    lines += [f"{k}='{v}'" for k, v in values.items()]
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    while True:
        pw = getpass.getpass("New password: ")
        if len(pw) < 12:
            print("Use at least 12 characters.")
        elif pw != getpass.getpass("Again: "):
            print("They don't match.")
        else:
            break
    secret = base64.b32encode(secrets.token_bytes(20)).decode()
    qr = qrcode.QRCode(border=1)
    qr.add_data(f"otpauth://totp/supergroupchat:aditya?secret={secret}&issuer=supergroupchat")
    qr.print_ascii(invert=True)
    print(f"Scan with your authenticator app, or enter this key by hand: {secret}")
    while True:
        code = input("Code from the app: ").strip()
        c = int(time.time() // 30)
        if code in (totp(secret, n) for n in (c - 1, c, c + 1)):
            break
        print("That code doesn't match. Check the phone's clock and try the next one.")
    write_env({"PASSWORD_HASH": PasswordHasher().hash(pw), "TOTP_SECRET": secret,
               "SESSION_SECRET": secrets.token_hex(32)})
    print(f"Saved to {ENV}. Restart the server.")


if __name__ == "__main__":
    main()
