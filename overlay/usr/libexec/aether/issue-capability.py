#!/usr/bin/python3
"""Root-only CLI for minting short-lived, single-use execd capabilities."""
import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import stat
import sys
import time

KEY_PATH = "/var/lib/aether/execd.key"


def read_key():
    fd = os.open(KEY_PATH, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("execution key must be a root-owned 0600 regular file")
        key = os.read(fd, 64)
    finally:
        os.close(fd)
    if len(key) != 32:
        raise RuntimeError("execution key has invalid length")
    return key


def main():
    parser = argparse.ArgumentParser(description="Mint one short-lived capability for aether-execd")
    parser.add_argument("--uid", type=int, required=True, help="Linux UID allowed to use the token")
    parser.add_argument("--ttl", type=int, default=60, help="lifetime in seconds, maximum 300")
    parser.add_argument("--root", action="append", required=True, help="existing directory mounted read-only; repeatable")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="exact argv prefix after --")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("Run this command through sudo; only root may mint execution capabilities.")
    if not 1 <= args.ttl <= 300 or args.uid < 1:
        raise SystemExit("UID must be positive and TTL must be 1..300 seconds.")
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or len(command) > 32 or any(not item or len(item) > 4096 or "\x00" in item for item in command):
        raise SystemExit("Provide a command argv prefix after -- (no shell string).")
    binary = os.path.realpath(command[0])
    if not (binary.startswith("/usr/bin/") or binary.startswith("/bin/")) or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        raise SystemExit("Command must be an installed executable under /usr/bin or /bin.")
    command[0] = binary
    roots = []
    for root in args.root:
        resolved = os.path.realpath(root)
        allowed = resolved.startswith("/home/") or resolved.startswith("/tmp/")
        if not allowed or not os.path.isdir(resolved):
            raise SystemExit("Each --root must be an existing directory below /home or /tmp.")
        if any(os.path.commonpath((resolved, prior)) in (resolved, prior) for prior in roots):
            raise SystemExit("Overlapping filesystem roots are not allowed.")
        roots.append(resolved)
    claims = {"v": 1, "uid": args.uid, "exp": int(time.time()) + args.ttl, "nonce": secrets.token_urlsafe(18), "roots": sorted(roots), "argv_prefix": command}
    encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode("utf-8")).rstrip(b"=").decode("ascii")
    signature = base64.urlsafe_b64encode(hmac.new(read_key(), encoded.encode("ascii"), hashlib.sha256).digest()).rstrip(b"=").decode("ascii")
    sys.stdout.write(encoded + "." + signature + "\n")


if __name__ == "__main__":
    main()
