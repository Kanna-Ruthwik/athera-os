#!/usr/bin/python3
"""Capability-checked, one-shot bubblewrap broker with an exported seccomp filter."""
import base64
import concurrent.futures
import ctypes
import ctypes.util
import errno
import hashlib
import hmac
import json
import logging
import os
import pwd
import resource
import secrets
import selectors
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import time
import threading

SOCKET_PATH = "/run/ai/exec.sock"
KEY_PATH = "/var/lib/aether/execd.key"
REPLAY_PATH = "/var/lib/execd/replay.sqlite3"
BWRAP = "/usr/bin/bwrap"
MAX_LINE = 16384
MAX_RUNTIME = 15
MAX_OUTPUT = 32768
MAX_CLIENTS = 12
nonce_database = None
nonce_lock = threading.Lock()
client_slots = threading.BoundedSemaphore(MAX_CLIENTS)

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("execd")


def event(name, **fields):
    logging.info(json.dumps({"service": "execd", "event": name, **fields}, separators=(",", ":")))


def ensure_key():
    if os.geteuid() != 0:
        raise RuntimeError("execution key provisioning must run as root")
    directory = os.path.dirname(KEY_PATH)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    directory_info = os.stat(directory, follow_symlinks=False)
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_uid != 0 or stat.S_IMODE(directory_info.st_mode) & 0o077:
        raise RuntimeError("execution key directory must be root-owned and private")
    try:
        fd = os.open(KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        info = os.lstat(KEY_PATH)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("existing execution key has unsafe ownership or permissions")
        return
    try:
        os.write(fd, secrets.token_bytes(32))
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def open_replay_database():
    global nonce_database
    replay_path = REPLAY_PATH
    nonce_database = sqlite3.connect(replay_path, timeout=5, check_same_thread=False)
    nonce_database.execute("PRAGMA journal_mode=WAL")
    nonce_database.execute("CREATE TABLE IF NOT EXISTS used_tokens (nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
    nonce_database.execute("CREATE INDEX IF NOT EXISTS used_tokens_expiry_idx ON used_tokens(expires_at)")
    nonce_database.commit()
    os.chmod(replay_path, 0o600)


def read_key():
    credential_directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not credential_directory or not os.path.isabs(credential_directory):
        raise RuntimeError("systemd execution-key credential is unavailable")
    credential_path = os.path.join(credential_directory, "execd.key")
    account = pwd.getpwnam("aether-exec")
    fd = os.open(credential_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != account.pw_uid or stat.S_IMODE(info.st_mode) != 0o400:
            raise RuntimeError("execution credential permissions are invalid")
        key = os.read(fd, 64)
    finally:
        os.close(fd)
    if len(key) != 32:
        raise RuntimeError("execution key must be exactly 32 bytes")
    return key


def within(path, root):
    try:
        return os.path.commonpath((path, root)) == root
    except ValueError:
        return False


def decode_capability(token, key, peer_uid):
    if not isinstance(token, str) or len(token) > 8192 or token.count(".") != 1:
        raise ValueError("invalid capability format")
    encoded, signature = token.split(".", 1)
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        supplied = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid capability encoding") from exc
    expected = hmac.new(key, encoded.encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("capability signature mismatch")
    claims = json.loads(payload.decode("utf-8"))
    if not isinstance(claims, dict) or claims.get("v") != 1:
        raise ValueError("unsupported capability payload")
    if isinstance(claims.get("uid"), bool) or not isinstance(claims.get("uid"), int) or claims.get("uid") != peer_uid:
        raise ValueError("capability is not scoped to this user")
    expiry = claims.get("exp")
    if isinstance(expiry, bool) or not isinstance(expiry, int) or expiry < int(time.time()):
        raise ValueError("capability has expired")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 128:
        raise ValueError("capability nonce is invalid")
    if not isinstance(claims.get("roots"), list) or not claims["roots"] or not isinstance(claims.get("argv_prefix"), list) or not claims["argv_prefix"]:
        raise ValueError("capability lacks a command or filesystem scope")
    if nonce_database is None:
        raise RuntimeError("persistent capability replay guard is unavailable")
    with nonce_lock:
        now = int(time.time())
        try:
            with nonce_database:
                nonce_database.execute("DELETE FROM used_tokens WHERE expires_at < ?", (now,))
                if nonce_database.execute("SELECT count(*) FROM used_tokens").fetchone()[0] >= 4096:
                    raise ValueError("capability replay cache is full; retry later")
                nonce_database.execute("INSERT INTO used_tokens(nonce,expires_at) VALUES(?,?)", (nonce, claims["exp"]))
        except sqlite3.IntegrityError as exc:
            raise ValueError("capability has already been used") from exc
    return claims


def build_seccomp_filter():
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise RuntimeError("libseccomp is required; refusing unsandboxed execution")
    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_syscall_resolve_name.restype = ctypes.c_int
    seccomp.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    seccomp.seccomp_rule_add.restype = ctypes.c_int
    seccomp.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    seccomp.seccomp_export_bpf.restype = ctypes.c_int
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    action_allow = 0x7FFF0000
    action_errno = 0x00050000 | errno.EPERM
    context = seccomp.seccomp_init(action_allow)
    if not context:
        raise RuntimeError("could not create seccomp policy")
    descriptor = os.memfd_create("aether-seccomp", flags=0)
    denied = ("socket", "socketpair", "connect", "bind", "listen", "accept", "accept4", "sendto", "recvfrom", "mount", "umount2", "pivot_root", "ptrace", "bpf", "perf_event_open", "open_by_handle_at", "keyctl", "add_key", "request_key", "kexec_load", "reboot", "init_module", "finit_module", "delete_module", "userfaultfd")
    try:
        for name in denied:
            number = seccomp.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number >= 0 and seccomp.seccomp_rule_add(context, action_errno, number, 0) != 0:
                raise RuntimeError("could not add seccomp rule for " + name)
        if seccomp.seccomp_export_bpf(context, descriptor) != 0:
            raise RuntimeError("could not export seccomp policy")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise
    finally:
        seccomp.seccomp_release(context)


def apply_limits():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_RUNTIME, MAX_RUNTIME + 1))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (4 * 1024 * 1024, 4 * 1024 * 1024))


def run_command(claims, request):
    argv = request.get("argv")
    cwd = request.get("cwd")
    if not isinstance(argv, list) or not 1 <= len(argv) <= 32 or any(not isinstance(arg, str) or not arg or len(arg) > 4096 or "\x00" in arg for arg in argv):
        raise ValueError("argv must contain 1..32 non-empty strings")
    if not isinstance(cwd, str) or len(cwd) > 4096:
        raise ValueError("cwd must be a path string")
    binary = os.path.realpath(argv[0])
    if not (binary.startswith("/usr/bin/") or binary.startswith("/bin/")) or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        raise ValueError("program must be an installed executable under /usr/bin or /bin")
    argv = [binary, *argv[1:]]
    if argv[:len(claims["argv_prefix"])] != claims["argv_prefix"]:
        raise ValueError("command is outside the approved argv prefix")
    canonical_roots = []
    for root in claims["roots"]:
        if not isinstance(root, str):
            raise ValueError("invalid filesystem scope")
        resolved = os.path.realpath(root)
        if not os.path.isdir(resolved) or not (within(resolved, "/home") and resolved != "/home" or within(resolved, "/tmp") and resolved != "/tmp"):
            raise ValueError("filesystem scope must be a directory below /home or /tmp")
        canonical_roots.append(resolved)
    canonical_cwd = os.path.realpath(cwd)
    if not os.path.isdir(canonical_cwd) or not any(within(canonical_cwd, root) for root in canonical_roots):
        raise ValueError("working directory is outside approved filesystem roots")

    seccomp_fd = build_seccomp_filter()
    command = [BWRAP, "--die-with-parent", "--new-session", "--unshare-all", "--ro-bind", "/usr", "/usr"]
    if os.path.isdir("/etc/alternatives"):
        command.extend(["--ro-bind", "/etc/alternatives", "/etc/alternatives"])
    command.extend(["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"])
    created = {"/tmp"}
    for root in canonical_roots:
        parts = root.strip("/").split("/")
        parent = ""
        for part in parts[:-1]:
            parent += "/" + part
            if parent not in created:
                command.extend(["--dir", parent])
                created.add(parent)
        command.extend(["--ro-bind", root, root])
    command.extend(["--chdir", canonical_cwd, "--seccomp", str(seccomp_fd), "--", *argv])
    environment = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, close_fds=True, pass_fds=(seccomp_fd,), start_new_session=True, preexec_fn=apply_limits)
    os.close(seccomp_fd)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    deadline = time.monotonic() + MAX_RUNTIME
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("sandbox exceeded 15 second wall-clock limit")
            for key, _ in selector.select(min(remaining, 1.0)):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffer = output[key.data]
                if len(buffer) < MAX_OUTPUT:
                    buffer.extend(chunk[:MAX_OUTPUT - len(buffer)])
        return {"exit_code": process.returncode, "stdout": output["stdout"].decode("utf-8", "replace"), "stderr": output["stderr"].decode("utf-8", "replace"), "truncated": any(len(value) >= MAX_OUTPUT for value in output.values())}
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        raise
    finally:
        selector.close()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()


def send(client, payload):
    client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"))


def client_handler(client, key):
    try:
        pid, uid, gid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        client.settimeout(4)
        line = client.makefile("rb").readline(MAX_LINE + 1)
        if not line.endswith(b"\n") or len(line) > MAX_LINE:
            raise ValueError("request must be a newline-terminated JSON line under 16 KiB")
        request = json.loads(line.decode("utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        if request.get("op") == "health":
            ready = os.path.isfile(BWRAP) and bool(ctypes.util.find_library("seccomp"))
            send(client, {"status": "ok", "service": "execd", "ready": ready, "sandbox": "bubblewrap+seccomp", "network": "disabled"})
            return
        if request.get("op") != "run":
            raise ValueError("unsupported operation")
        claims = decode_capability(request.get("capability"), key, uid)
        result = run_command(claims, request)
        event("sandbox_exit", peer_uid=uid, peer_gid=gid, peer_pid=pid, program=os.path.basename(request["argv"][0]), exit_code=result["exit_code"])
        send(client, {"status": "ok", **result})
    except (OSError, ValueError, TimeoutError, RuntimeError, KeyError, TypeError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        event("request_denied", reason=str(exc)[:180])
        try:
            send(client, {"status": "denied", "error": str(exc)[:240]})
        except OSError:
            pass
    finally:
        client.close()


def remove_stale_socket():
    try:
        info = os.lstat(SOCKET_PATH)
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("refusing to replace non-socket at " + SOCKET_PATH)
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass


def main():
    if len(os.sys.argv) == 2 and os.sys.argv[1] == "--ensure-key":
        if os.geteuid() != 0:
            raise SystemExit("key provisioning must run as root")
        ensure_key()
        return
    key = read_key()
    open_replay_database()
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o750, exist_ok=True)
    remove_stale_socket()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(24)
    event("started", socket=SOCKET_PATH, sandbox="bubblewrap+seccomp", network="disabled")
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_CLIENTS, thread_name_prefix="execd-client") as pool:
        while True:
            client, _ = server.accept()
            if not client_slots.acquire(blocking=False):
                client.close()
                event("client_limit_reached")
                continue
            future = pool.submit(client_handler, client, key)
            future.add_done_callback(lambda _future: client_slots.release())


if __name__ == "__main__":
    main()
