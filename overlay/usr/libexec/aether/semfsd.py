#!/usr/bin/python3
"""Event-driven, owner-filtered text index for files readable by aether-semfs."""
import concurrent.futures
import ctypes
import errno
import hashlib
import json
import logging
import math
import os
import select
import socket
import sqlite3
import stat
import struct
import threading

SOCKET_PATH = "/run/ai/semfs.sock"
ROOT = os.environ.get("AETHER_INDEX_ROOT", "/home")
DATABASE = "/var/lib/semfs/index.sqlite3"
MODEL_NAME = "BAAI/bge-small-en-v1.5"
MODEL_CACHE = os.environ.get("AETHER_MODEL_CACHE", "/usr/share/aether/models")
MAX_LINE = 8192
MAX_FILE_BYTES = 262144
TEXT_SUFFIXES = {".txt", ".md", ".rst", ".csv", ".json", ".yaml", ".yml", ".toml", ".py", ".rs", ".js", ".ts", ".tsx", ".html", ".css", ".sh", ".conf", ".log"}
SKIP_DIRS = {".ssh", ".gnupg", ".aws", ".config", ".local", "node_modules", ".git"}
SKIP_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", "credentials", "secrets.json"}
IN_CREATE, IN_DELETE, IN_MODIFY, IN_ATTRIB, IN_CLOSE_WRITE, IN_MOVED_FROM, IN_MOVED_TO, IN_ISDIR = 0x100, 0x200, 0x2, 0x4, 0x8, 0x40, 0x80, 0x40000000
WATCH_MASK = IN_CREATE | IN_DELETE | IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("semfsd")
libc = ctypes.CDLL(None, use_errno=True)
libc.inotify_init1.argtypes = [ctypes.c_int]
libc.inotify_init1.restype = ctypes.c_int
libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
libc.inotify_add_watch.restype = ctypes.c_int
lock = threading.RLock()
watch_paths = {}
database = None
client_slots = threading.BoundedSemaphore(16)
embedding_model = None
embedding_lock = threading.RLock()


def event(name, **fields):
    logging.info(json.dumps({"service": "semfsd", "event": name, **fields}, separators=(",", ":")))


def within_root(path):
    try:
        return os.path.commonpath((ROOT, path)) == ROOT
    except ValueError:
        return False


def excluded(path):
    parts = os.path.relpath(path, ROOT).split(os.sep)
    return any(part in SKIP_DIRS or part.startswith(".") for part in parts) or os.path.basename(path).lower() in SKIP_NAMES


def open_database():
    global database
    os.makedirs(os.path.dirname(DATABASE), mode=0o750, exist_ok=True)
    database = sqlite3.connect(DATABASE, timeout=5, check_same_thread=False)
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA synchronous=NORMAL")
    database.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, inode INTEGER NOT NULL, owner_uid INTEGER NOT NULL, owner_gid INTEGER NOT NULL, mode INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL DEFAULT '', embedding BLOB, embedding_model TEXT)")
    columns = {row[1] for row in database.execute("PRAGMA table_info(files)")}
    migrations = {
        "content_sha256": "TEXT NOT NULL DEFAULT ''",
        "embedding": "BLOB",
        "embedding_model": "TEXT",
    }
    for column, definition in migrations.items():
        if column not in columns:
            database.execute("ALTER TABLE files ADD COLUMN " + column + " " + definition)
    database.execute("CREATE INDEX IF NOT EXISTS files_owner_idx ON files(owner_uid)")
    database.commit()
    os.chmod(DATABASE, 0o600)


def load_embedding_model():
    global embedding_model
    with embedding_lock:
        if embedding_model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise RuntimeError("FastEmbed is required for semantic search") from exc
            embedding_model = TextEmbedding(model_name=MODEL_NAME, cache_dir=MODEL_CACHE)
        return embedding_model


def embed_text(text):
    with embedding_lock:
        model = load_embedding_model()
        vector = next(model.embed([text]), None)
    if vector is None:
        raise RuntimeError("embedding model returned no vector")
    values = [float(value) for value in vector]
    if not values or any(not math.isfinite(value) for value in values):
        raise RuntimeError("embedding model returned an invalid vector")
    return values


def pack_embedding(values):
    return struct.pack("<" + "f" * len(values), *values)


def cosine_similarity(left, packed_right):
    if len(packed_right) == 0 or len(packed_right) % 4:
        return -1.0
    right = struct.unpack("<" + "f" * (len(packed_right) // 4), packed_right)
    if len(left) != len(right):
        return -1.0
    dot = math.fsum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(math.fsum(value * value for value in left))
    right_norm = math.sqrt(math.fsum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return -1.0
    return dot / (left_norm * right_norm)


def remove_path(path, tree=False):
    with lock:
        if tree:
            database.execute("DELETE FROM files WHERE path=? OR substr(path,1,?)=?", (path, len(path) + 1, path + os.sep))
        else:
            database.execute("DELETE FROM files WHERE path=?", (path,))
        database.commit()


def index_file(path):
    if not within_root(path) or excluded(path):
        return
    if os.path.splitext(path)[1].lower() not in TEXT_SUFFIXES:
        return
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            remove_path(path)
            return
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            opened = os.fstat(fd)
            if opened.st_ino != before.st_ino or opened.st_uid != before.st_uid or opened.st_size > MAX_FILE_BYTES:
                return
            data = os.read(fd, MAX_FILE_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_FILE_BYTES or b"\x00" in data:
            remove_path(path)
            return
        content = data.decode("utf-8", errors="replace").strip()
        if not content:
            remove_path(path)
            return
        content_sha256 = hashlib.sha256(data).hexdigest()
        with lock:
            cached = database.execute("SELECT embedding FROM files WHERE path=? AND content_sha256=? AND embedding_model=?", (path, content_sha256, MODEL_NAME)).fetchone()
        if cached and cached[0]:
            vector = cached[0]
        else:
            try:
                vector = pack_embedding(embed_text(content))
            except (RuntimeError, ValueError) as exc:
                event("embedding_error", path=path[:180], reason=str(exc)[:160])
                return
        with lock:
            database.execute("INSERT OR REPLACE INTO files(path,inode,owner_uid,owner_gid,mode,mtime_ns,content,content_sha256,embedding,embedding_model) VALUES(?,?,?,?,?,?,?,?,?,?)", (path, opened.st_ino, opened.st_uid, opened.st_gid, stat.S_IMODE(opened.st_mode), opened.st_mtime_ns, content, content_sha256, vector, MODEL_NAME))
            database.commit()
    except (OSError, UnicodeError) as exc:
        remove_path(path)
        if getattr(exc, "errno", None) not in (errno.ENOENT, errno.EACCES, errno.EPERM, errno.ELOOP):
            event("index_error", path=path[:180], reason=str(exc)[:160])


def add_watch(path, fd):
    if not within_root(path) or excluded(path):
        return
    wd = libc.inotify_add_watch(fd, os.fsencode(path), WATCH_MASK)
    if wd < 0:
        code = ctypes.get_errno()
        if code not in (errno.EACCES, errno.ENOENT, errno.ENOTDIR):
            event("watch_error", path=path[:160], errno=code)
        return
    watch_paths[wd] = path


def scan_tree(root, fd):
    if not within_root(root) or excluded(root):
        return
    try:
        mode = os.lstat(root).st_mode
    except OSError:
        return
    if stat.S_ISLNK(mode):
        return
    if stat.S_ISDIR(mode):
        add_watch(root, fd)
        try:
            with os.scandir(root) as entries:
                children = list(entries)
        except OSError:
            return
        for entry in children:
            if entry.name.startswith(".") or entry.name in SKIP_DIRS or entry.name in SKIP_NAMES:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    scan_tree(entry.path, fd)
                elif entry.is_file(follow_symlinks=False):
                    index_file(entry.path)
            except OSError:
                continue
    elif stat.S_ISREG(mode):
        index_file(root)


def handle_events(fd):
    header = struct.Struct("iIII")
    while True:
        readable, _, _ = select.select([fd], [], [])
        if not readable:
            continue
        try:
            raw = os.read(fd, 65536)
        except BlockingIOError:
            continue
        offset = 0
        while offset + header.size <= len(raw):
            wd, mask, _, name_length = header.unpack_from(raw, offset)
            offset += header.size
            if mask & 0x00004000:
                event("watch_overflow", action="rescan")
                scan_tree(ROOT, fd)
                continue
            name = raw[offset:offset + name_length].split(b"\0", 1)[0].decode("utf-8", "replace")
            offset += name_length
            parent = watch_paths.get(wd)
            if parent is None:
                continue
            path = os.path.join(parent, name) if name else parent
            is_dir = bool(mask & IN_ISDIR)
            if mask & (IN_DELETE | IN_MOVED_FROM):
                remove_path(path, tree=is_dir)
                for watched_id, watched_path in list(watch_paths.items()):
                    if watched_path == path or watched_path.startswith(path + os.sep):
                        watch_paths.pop(watched_id, None)
            elif mask & (IN_CREATE | IN_MOVED_TO):
                scan_tree(path, fd)
            elif mask & (IN_CLOSE_WRITE | IN_MODIFY | IN_ATTRIB):
                if is_dir:
                    add_watch(path, fd)
                else:
                    index_file(path)


def search(query, peer_uid, limit):
    if not query.strip():
        return []
    query_vector = embed_text(query)
    with lock:
        rows = database.execute("SELECT path,inode,owner_uid,mode,content,embedding FROM files WHERE owner_uid=? AND embedding_model=? AND embedding IS NOT NULL LIMIT 2000", (peer_uid, MODEL_NAME)).fetchall()
    results = []
    for path, inode, owner_uid, mode, content, vector in rows:
        try:
            current = os.stat(path, follow_symlinks=False)
            if current.st_ino != inode or current.st_uid != owner_uid or not stat.S_ISREG(current.st_mode) or not current.st_mode & stat.S_IRUSR:
                remove_path(path)
                continue
        except OSError:
            remove_path(path)
            continue
        score = cosine_similarity(query_vector, vector)
        if score < 0:
            continue
        results.append({"path": path, "snippet": content[:240], "owner_uid": owner_uid, "score": score})
    results.sort(key=lambda result: result["score"], reverse=True)
    return results[:limit]


def client_handler(client):
    try:
        _, uid, gid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        client.settimeout(4)
        line = client.makefile("rb").readline(MAX_LINE + 1)
        if not line.endswith(b"\n") or len(line) > MAX_LINE:
            response = {"status": "error", "error": "request line too large or incomplete"}
        else:
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            if request.get("op") == "health":
                with lock:
                    indexed = database.execute("SELECT count(*) FROM files").fetchone()[0]
                response = {"status": "ok", "service": "semfsd", "indexed": indexed}
            elif request.get("op") == "search":
                query = request.get("query")
                limit = request.get("limit", 5)
                if not isinstance(query, str) or len(query) > 256 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
                    raise ValueError("query must be <=256 characters and limit must be 1..10")
                response = {"status": "ok", "results": search(query, uid, limit)}
                event("search", peer_uid=uid, peer_gid=gid, query_length=len(query), result_count=len(response["results"]))
            else:
                response = {"status": "error", "error": "unsupported operation"}
        client.sendall((json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
        try:
            client.sendall((json.dumps({"status": "error", "error": str(exc)[:160]}) + "\n").encode("utf-8"))
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
    if not os.path.isdir(ROOT):
        os.makedirs(ROOT, mode=0o755, exist_ok=True)
    load_embedding_model()
    open_database()
    inotify_fd = libc.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if inotify_fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    add_watch(ROOT, inotify_fd)
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o750, exist_ok=True)
    remove_stale_socket()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(32)
    event("started", socket=SOCKET_PATH, root=ROOT, database=DATABASE, watches=len(watch_paths), embedding_model=MODEL_NAME)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="semfs-client") as pool:
        threading.Thread(target=handle_events, args=(inotify_fd,), name="semfs-inotify", daemon=True).start()
        threading.Thread(target=scan_tree, args=(ROOT, inotify_fd), name="semfs-initial-scan", daemon=True).start()
        while True:
            client, _ = server.accept()
            if not client_slots.acquire(blocking=False):
                client.close()
                event("client_limit_reached")
                continue
            future = pool.submit(client_handler, client)
            future.add_done_callback(lambda _future: client_slots.release())


if __name__ == "__main__":
    main()
