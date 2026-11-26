#!/usr/bin/python3
"""Small CLI for the three local AetherOS Unix-socket APIs."""
import argparse
import getpass
import importlib.machinery
import importlib.util
import json
import os
import socket
import subprocess
import sys

SOCKETS = {"aid": "/run/ai/inference.sock", "semfsd": "/run/ai/semfs.sock", "execd": "/run/ai/exec.sock"}
SPEECH_PYTHON = "/opt/aether/venv/bin/python3"
SPEECH_SCRIPT = "/usr/libexec/aether/speech.py.txt"


def request(path, payload, timeout=100):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
        client.sendall((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        response = client.makefile("rb").readline(65537)
        if not response or len(response) > 65536:
            raise RuntimeError("daemon returned an empty or oversized response")
        return json.loads(response.decode("utf-8"))
    finally:
        client.close()


def show(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))
    if value.get("status") not in ("ok",):
        raise SystemExit(1)


def speech_command(*arguments):
    try:
        result = subprocess.run([SPEECH_PYTHON, SPEECH_SCRIPT, *arguments], check=False, text=True, capture_output=True)
    except OSError as exc:
        raise SystemExit("offline speech runtime unavailable: " + str(exc)) from exc
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="")
    if result.returncode:
        raise SystemExit(result.returncode)
    return result.stdout.strip()


def load_agent():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("agent.py", "agent.py.txt"):
        path = os.path.join(here, name)
        if os.path.exists(path):
            loader = importlib.machinery.SourceFileLoader("aether_agent", path)
            spec = importlib.util.spec_from_loader("aether_agent", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
    raise SystemExit("assistant module not found")


def chat():
    module = load_agent()
    agent = module.Agent()
    print("Aether chat. Type 'help' for what I can do, 'exit' to leave.")
    while True:
        try:
            line = input("you> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line.strip().lower() in ("exit", "quit", ":q"):
            return
        reply = agent.handle(line)
        print("aether> " + reply.text)
        if reply.pending:
            try:
                answer = input("Allow this? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
            print("aether> " + agent.confirm(reply.pending, answer in ("y", "yes")).text)


def doctor():
    print("AetherOS doctor")
    for unit in ("aid", "semfsd", "execd", "aether-llm"):
        try:
            state = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            state = "unknown"
        print("  %-12s %s" % (unit, state))
    try:
        health = request(SOCKETS["aid"], {"op": "health"}, timeout=2)
        print("  model backend reachable: %s" % health.get("backend_reachable"))
    except (OSError, ValueError, RuntimeError) as exc:
        print("  model backend: aid unavailable (%s)" % exc)
    for label, path in (("model file", "/usr/share/aether/models/llm/model.gguf"), ("llama-server", "/opt/aether/llama/llama-server")):
        print("  %-12s %s" % (label, "present" if os.path.exists(path) else "MISSING"))
    report = "/usr/share/aether/build-report.txt"
    try:
        with open(report, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        print("  (no build report found)")
        return
    problems = [line for line in lines if line.startswith(("MISSING", "WARNING", "UNVERIFIED", "SKIPPED"))]
    ok_count = sum(1 for line in lines if line.startswith("OK"))
    print("Build report: %d OK, %d problem(s)  (full text: %s)" % (ok_count, len(problems), report))
    for line in problems:
        print("  " + line)


def main():
    parser = argparse.ArgumentParser(prog="aetherctl")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("status", help="show daemon status")
    search = sub.add_parser("search", help="search your permission-filtered local index")
    search.add_argument("query", nargs="+")
    ask = sub.add_parser("ask", help="send a prompt to the local inference backend")
    ask.add_argument("prompt", nargs="+")
    run = sub.add_parser("run", help="run a one-shot capability-authorized sandbox command")
    run.add_argument("--cwd", required=True)
    run.add_argument("command", nargs=argparse.REMAINDER)
    listen = sub.add_parser("listen", help="record a short clip and transcribe it offline")
    listen.add_argument("--seconds", type=int, default=5)
    say = sub.add_parser("say", help="speak text offline")
    say.add_argument("text", nargs="+")
    sub.add_parser("chat", help="chat with the Aether assistant in the terminal")
    sub.add_parser("assistant", help="open the Aether assistant window")
    sub.add_parser("doctor", help="check services, the model and the build report")
    voice = sub.add_parser("voice", help="ask the local model using speech")
    voice.add_argument("--seconds", type=int, default=5)
    args = parser.parse_args()
    if args.action == "status":
        for service, path in SOCKETS.items():
            try:
                print(json.dumps({"service": service, **request(path, {"op": "health"}, timeout=2)}, ensure_ascii=False))
            except (OSError, ValueError, RuntimeError) as exc:
                print(json.dumps({"service": service, "status": "unavailable", "error": str(exc)}))
        return
    if args.action == "chat":
        chat()
        return
    if args.action == "doctor":
        doctor()
        return
    if args.action == "assistant":
        os.execvp("aether-assistant", ["aether-assistant"])
    if args.action == "search":
        show(request(SOCKETS["semfsd"], {"op": "search", "query": " ".join(args.query), "limit": 5}, timeout=10))
        return
    if args.action == "ask":
        show(request(SOCKETS["aid"], {"op": "generate", "priority": "interactive", "messages": [{"role": "user", "content": " ".join(args.prompt)}]}))
        return
    if args.action == "listen":
        print(speech_command("listen", "--seconds", str(args.seconds)))
        return
    if args.action == "say":
        speech_command("say", " ".join(args.text))
        return
    if args.action == "voice":
        prompt = speech_command("listen", "--seconds", str(args.seconds))
        if not prompt:
            raise SystemExit("No speech was recognized")
        response = request(SOCKETS["aid"], {"op": "generate", "priority": "interactive", "messages": [{"role": "user", "content": prompt}]})
        show(response)
        speech_command("say", response["answer"])
        return
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        raise SystemExit("Usage: aetherctl run --cwd DIR -- PROGRAM [ARG ...]")
    token = getpass.getpass("Paste the short-lived capability (input hidden): ")
    show(request(SOCKETS["execd"], {"op": "run", "capability": token, "cwd": args.cwd, "argv": command}, timeout=20))


if __name__ == "__main__":
    main()
