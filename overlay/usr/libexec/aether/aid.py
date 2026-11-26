#!/usr/bin/python3
"""Bounded local inference broker. It only connects to loopback llama.cpp."""
import concurrent.futures
import http.client
import json
import logging
import os
import queue
import socket
import stat
import struct
import threading
import time

SOCKET_PATH = "/run/ai/inference.sock"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8080
MAX_LINE = 65536
MAX_QUEUE = 16
MAX_WORKERS = 20
MAX_ANSWER = 32768
MAX_PROMPT = 12000

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("aid")
work_queue = queue.PriorityQueue(MAX_QUEUE)
sequence = 0
sequence_lock = threading.Lock()
workers = threading.BoundedSemaphore(MAX_WORKERS)
client_slots = threading.BoundedSemaphore(MAX_WORKERS)


def event(name, **fields):
    logging.info(json.dumps({"service": "aid", "event": name, **fields}, separators=(",", ":")))


def peer_credentials(client):
    raw = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


def send_json(client, status, payload):
    client.sendall((json.dumps({"status": status, **payload}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))


def validate_request(request):
    if not isinstance(request, dict):
        raise ValueError("request must be an object")
    operation = request.get("op", "generate")
    if operation == "health":
        return operation, None, None
    if operation != "generate":
        raise ValueError("unsupported operation")
    priority = request.get("priority", "interactive")
    if priority not in ("interactive", "background"):
        raise ValueError("priority must be interactive or background")
    messages = request.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 24:
        raise ValueError("messages must contain between 1 and 24 entries")
    clean_messages = []
    total = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant"):
            raise ValueError("each message needs a supported role and text content")
        content = message.get("content")
        if not isinstance(content, str) or len(content) > MAX_PROMPT:
            raise ValueError("message content must be text under 12000 characters")
        total += len(content)
        clean_messages.append({"role": message["role"], "content": content})
    if total > MAX_LINE // 2:
        raise ValueError("combined prompt is too large")
    max_tokens = request.get("max_tokens", 512)
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 2048:
        raise ValueError("max_tokens must be an integer from 1 to 2048")
    return operation, (clean_messages, max_tokens), priority


def backend_generate(messages, max_tokens):
    payload = json.dumps({"model": "local", "messages": messages, "max_tokens": max_tokens, "stream": False}, separators=(",", ":"))
    connection = http.client.HTTPConnection(BACKEND_HOST, BACKEND_PORT, timeout=90)
    try:
        connection.request("POST", "/v1/chat/completions", body=payload, headers={"Content-Type": "application/json", "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(MAX_ANSWER * 2 + 1)
        if response.status != 200:
            raise RuntimeError("local model backend returned HTTP " + str(response.status))
        if len(raw) > MAX_ANSWER * 2:
            raise RuntimeError("local model response exceeded limit")
        data = json.loads(raw.decode("utf-8"))
        answer = data["choices"][0]["message"]["content"]
        if not isinstance(answer, str):
            raise RuntimeError("local model returned an invalid response")
        return answer[:MAX_ANSWER]
    finally:
        connection.close()


def run_job(job):
    messages, max_tokens = job[3]
    try:
        job[4]["result"] = backend_generate(messages, max_tokens)
    except (OSError, TimeoutError, ValueError, KeyError, IndexError, RuntimeError, http.client.HTTPException) as exc:
        job[4]["error"] = str(exc)[:240]
    finally:
        job[5].set()


def inference_worker():
    while True:
        item = work_queue.get()
        if item is None:
            return
        run_job(item)


def handle(client):
    client.settimeout(5)
    acquired = False
    try:
        pid, uid, gid = peer_credentials(client)
        stream = client.makefile("rb")
        line = stream.readline(MAX_LINE + 1)
        if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
            send_json(client, "error", {"error": "request must be one newline-terminated JSON line under 64 KiB"})
            return
        request = json.loads(line.decode("utf-8"))
        operation, body, priority = validate_request(request)
        if operation == "health":
            try:
                with socket.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=0.3):
                    backend_reachable = True
            except OSError:
                backend_reachable = False
            send_json(client, "ok", {"service": "aid", "backend": "loopback llama.cpp", "backend_reachable": backend_reachable, "priority_queue": work_queue.qsize()})
            return
        if not workers.acquire(blocking=False):
            send_json(client, "busy", {"error": "request concurrency limit reached"})
            return
        acquired = True
        completion = threading.Event()
        result = {}
        global sequence
        with sequence_lock:
            sequence += 1
            order = sequence
        priority_number = 0 if priority == "interactive" else 1
        job = (priority_number, order, time.monotonic(), body, result, completion)
        try:
            work_queue.put_nowait(job)
        except queue.Full:
            send_json(client, "busy", {"error": "inference queue is full"})
            return
        event("request_queued", peer_uid=uid, peer_gid=gid, peer_pid=pid, priority=priority, queue_depth=work_queue.qsize())
        client.settimeout(95)
        if not completion.wait(90):
            send_json(client, "timeout", {"error": "local inference exceeded 90 seconds"})
        elif "error" in result:
            event("backend_unavailable", peer_uid=uid, reason=result["error"])
            send_json(client, "unavailable", {"error": "local inference backend unavailable", "detail": result["error"]})
        else:
            send_json(client, "ok", {"answer": result["result"], "source": "local"})
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        try:
            send_json(client, "error", {"error": str(exc)[:200]})
        except OSError:
            pass
    except (OSError, TimeoutError) as exc:
        event("client_error", reason=str(exc)[:160])
    finally:
        if acquired:
            workers.release()
        try:
            client.close()
        except OSError:
            pass


def remove_stale_socket():
    try:
        info = os.lstat(SOCKET_PATH)
        if not stat.S_ISSOCK(info.st_mode):
            raise RuntimeError("refusing to replace a non-socket at " + SOCKET_PATH)
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass


def main():
    os.makedirs(os.path.dirname(SOCKET_PATH), mode=0o750, exist_ok=True)
    remove_stale_socket()
    threading.Thread(target=inference_worker, name="aid-priority-worker", daemon=True).start()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o660)
    server.listen(32)
    event("started", socket=SOCKET_PATH, backend_host=BACKEND_HOST, backend_port=BACKEND_PORT)
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="aid-client") as pool:
        while True:
            client, _ = server.accept()
            if not client_slots.acquire(blocking=False):
                client.close()
                event("client_limit_reached")
                continue
            future = pool.submit(handle, client)
            future.add_done_callback(lambda _future: client_slots.release())


if __name__ == "__main__":
    main()
