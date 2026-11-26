#!/usr/bin/python3
"""Aether assistant core: intent routing, safe tools, confirmation, memory, audit log.

No GUI imports here, so it can be used from the CLI (`aetherctl chat`), the GTK
window (`aether-assistant`) and the unit tests.

Safety model
  * Tools only touch the user's home folder (symlinks are resolved first) and
    never read obvious secret folders such as ~/.ssh.
  * Explicit requests ("create a folder ...") run directly if the tool is
    low-risk. Anything destructive (trash, move, power, forget-all) needs an
    explicit approval from the person, every time.
  * Actions proposed by the language model are untrusted: only read-only tools
    run automatically, everything else needs approval.
  * Every action is written to ~/.local/state/aether/actions.log.
  * Memory is a plain JSON file the person can read, edit or delete.
"""
import datetime
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import urllib.parse

AID_SOCKET = os.environ.get("AETHER_AID_SOCKET", "/run/ai/inference.sock")
SEMFS_SOCKET = os.environ.get("AETHER_SEMFS_SOCKET", "/run/ai/semfs.sock")
MAX_MEMORY_ITEMS = 100
MAX_MEMORY_CHARS = 300
MAX_FILE_BYTES = 200_000
MAX_NEW_FILE_CHARS = 20_000
HISTORY_LIMIT = 8
BLOCKED_PARTS = {".ssh", ".gnupg", ".aws", ".kube", ".password-store", ".mozilla", ".config/aether"}
SAFE_MESSAGES = {
    "unavailable": "My language model isn't running right now. It may still be starting (give it a minute), "
    "or no model is installed. I can still search your files, create folders, open apps and keep notes. Type 'help'.",
    "timeout": "The model took too long to answer. Try a shorter question.",
}


class ToolError(Exception):
    """A tool refused or failed; the message is safe to show to the person."""


class AidError(Exception):
    def __init__(self, kind, detail=""):
        super().__init__(detail or kind)
        self.kind = kind


class Reply:
    def __init__(self, text, pending=None, ok=True):
        self.text = text
        self.pending = pending
        self.ok = ok

    def __repr__(self):
        return "Reply(%r, pending=%r)" % (self.text, self.pending)


class Pending:
    """An action waiting for the person's approval."""

    def __init__(self, tool, args, summary):
        self.tool = tool
        self.args = args
        self.summary = summary

    def __repr__(self):
        return "Pending(%s, %r)" % (self.tool, self.args)


class Tool:
    def __init__(self, name, description, params, func, read_only=False, confirm=False, summary=None):
        self.name = name
        self.description = description
        self.params = params          # {"arg": "str" | "int"}
        self.func = func
        self.read_only = read_only    # safe even when the model asks for it
        self.confirm = confirm        # always needs approval, even for explicit requests
        self.summary = summary or (lambda args: name + " " + json.dumps(args, ensure_ascii=False))


# --------------------------------------------------------------------------- IO


def unix_request(path, payload, timeout):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(path)
        client.sendall((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        line = client.makefile("rb").readline(65537)
        if not line or len(line) > 65536:
            raise OSError("empty or oversized response")
        return json.loads(line.decode("utf-8"))
    finally:
        client.close()


class Backend:
    """Talks to the local inference broker (aid)."""

    def __init__(self, socket_path=None, timeout=100):
        self.socket_path = socket_path or AID_SOCKET
        self.timeout = timeout

    def ask(self, messages, max_tokens=256):
        payload = {"op": "generate", "priority": "interactive", "messages": messages, "max_tokens": max_tokens}
        try:
            response = unix_request(self.socket_path, payload, self.timeout)
        except (OSError, ValueError) as exc:
            raise AidError("unavailable", str(exc)) from exc
        status = response.get("status")
        if status == "ok":
            return str(response.get("answer", "")).strip()
        if status == "timeout":
            raise AidError("timeout")
        raise AidError("unavailable", str(response.get("error", status)))


# ------------------------------------------------------------------ persistence


class Memory:
    """User-visible memory: a small JSON list the person controls."""

    def __init__(self, path):
        self.path = path

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            return [item for item in data if isinstance(item, dict) and "id" in item and "text" in item]
        except (OSError, ValueError):
            return []

    def _save(self, items):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(items, handle, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def list(self):
        return self._load()

    def add(self, text):
        text = " ".join(text.split())[:MAX_MEMORY_CHARS]
        if not text:
            raise ToolError("What should I remember?")
        items = self._load()
        if len(items) >= MAX_MEMORY_ITEMS:
            raise ToolError("My memory is full (%d items). Forget something first." % MAX_MEMORY_ITEMS)
        next_id = max([item["id"] for item in items if isinstance(item["id"], int)] + [0]) + 1
        items.append({"id": next_id, "text": text, "ts": datetime.datetime.now().isoformat(timespec="seconds")})
        self._save(items)
        return next_id

    def forget(self, item_id):
        items = self._load()
        kept = [item for item in items if item["id"] != item_id]
        if len(kept) == len(items):
            raise ToolError("I don't have a memory number %s." % item_id)
        self._save(kept)

    def clear(self):
        self._save([])

    def prompt_block(self):
        items = self._load()
        if not items:
            return ""
        return "Things the user asked you to remember:\n" + "\n".join("- " + item["text"] for item in items[-20:])


class AuditLog:
    def __init__(self, path):
        self.path = path

    def record(self, tool, args, status, detail=""):
        entry = {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "tool": tool,
            "args": args,
            "status": status,
            "detail": str(detail)[:200],
        }
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass  # never fail an action just because logging failed

    def recent(self, count=10):
        try:
            with open(self.path, encoding="utf-8") as handle:
                lines = handle.readlines()[-count:]
        except OSError:
            return []
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries


# ------------------------------------------------------------------------ agent


class Agent:
    def __init__(self, home=None, backend=None, runner=None, launcher=None, applications_dirs=None):
        self.home = os.path.abspath(home or os.path.expanduser("~"))
        self.data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(self.home, ".local", "share")
        self.state_home = os.environ.get("XDG_STATE_HOME") or os.path.join(self.home, ".local", "state")
        if home:  # explicit home (tests): keep everything under it
            self.data_home = os.path.join(self.home, ".local", "share")
            self.state_home = os.path.join(self.home, ".local", "state")
        self.backend = backend or Backend()
        self.run = runner or subprocess.run
        self.launch = launcher or (lambda argv: subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
        self.applications_dirs = applications_dirs or [
            "/usr/share/applications", "/usr/local/share/applications", os.path.join(self.data_home, "applications")]
        self.memory = Memory(os.path.join(self.data_home, "aether", "memory.json"))
        self.audit = AuditLog(os.path.join(self.state_home, "aether", "actions.log"))
        self.history = []
        self.tools = {}
        self._register_tools()

    # ---- tool registry -----------------------------------------------------

    def _register_tools(self):
        add = self._add_tool
        add("search_files", "Search the user's files by meaning. args: query", {"query": "str"},
            self.t_search_files, read_only=True)
        add("list_folder", "List a folder inside the home directory. args: path", {"path": "str"},
            self.t_list_folder, read_only=True)
        add("read_file", "Show the start of a text file in the home directory. args: path", {"path": "str"},
            self.t_read_file, read_only=True)
        add("system_info", "Show time, date, memory, disk, battery and uptime. no args", {},
            self.t_system_info, read_only=True)
        add("recall", "List what the user asked you to remember. no args", {}, self.t_recall, read_only=True)
        add("show_activity", "Show the most recent actions taken. no args", {}, self.t_show_activity, read_only=True)
        add("create_folder", "Create a folder inside the home directory. args: path", {"path": "str"},
            self.t_create_folder)
        add("create_file", "Create a NEW text file (never overwrites). args: path, text", {"path": "str", "text": "str"},
            self.t_create_file)
        add("open_app", "Open an installed application by name. args: name", {"name": "str"}, self.t_open_app)
        add("open_url", "Open a web address in the browser. args: url", {"url": "str"}, self.t_open_url)
        add("set_volume", "Set the speaker volume 0-100. args: percent", {"percent": "int"}, self.t_set_volume)
        add("set_mute", "Mute or unmute. args: muted (1 or 0)", {"muted": "int"}, self.t_set_mute)
        add("set_brightness", "Set the screen brightness 1-100. args: percent", {"percent": "int"}, self.t_set_brightness)
        add("take_screenshot", "Save a screenshot to ~/Pictures. no args", {}, self.t_screenshot)
        add("remember", "Remember a fact for later. args: text", {"text": "str"}, self.t_remember)
        add("forget", "Forget one memory. args: id", {"id": "int"}, self.t_forget)
        add("move_path", "Move or rename a file or folder in the home directory. args: source, destination",
            {"source": "str", "destination": "str"}, self.t_move_path, confirm=True,
            summary=lambda a: "Move %s to %s" % (a["source"], a["destination"]))
        add("trash_path", "Move a file or folder to the Trash. args: path", {"path": "str"}, self.t_trash_path,
            confirm=True, summary=lambda a: "Move %s to the Trash" % a["path"])
        add("forget_all", "Erase everything remembered. no args", {}, self.t_forget_all, confirm=True,
            summary=lambda a: "Erase everything I remember about you")
        add("power", "Power off or reboot the computer. args: action (poweroff or reboot)", {"action": "str"},
            self.t_power, confirm=True, summary=lambda a: "%s the computer" % ("Reboot" if a["action"] == "reboot" else "Power off"))

    def _add_tool(self, name, description, params, func, **options):
        self.tools[name] = Tool(name, description, params, func, **options)

    # ---- path helpers ------------------------------------------------------

    def safe_path(self, raw, must_exist=False):
        raw = str(raw).strip().strip("\"'")
        if not raw:
            raise ToolError("Which file or folder do you mean?")
        if raw == "~" or raw.startswith("~/"):
            path = self.home + raw[1:]
        else:
            path = raw
        if not os.path.isabs(path):
            path = os.path.join(self.home, path)
        path = os.path.normpath(path)
        real_home = os.path.realpath(self.home)
        probe = path
        while not os.path.lexists(probe) and probe != os.path.dirname(probe):
            probe = os.path.dirname(probe)
        real = os.path.normpath(os.path.join(os.path.realpath(probe), os.path.relpath(path, probe)))
        if real != real_home and not real.startswith(real_home + os.sep):
            raise ToolError("I only work inside your home folder (%s)." % self.home)
        relative = os.path.relpath(real, real_home)
        parts = [] if relative == "." else relative.split(os.sep)
        for index, part in enumerate(parts):
            joined = os.sep.join(parts[: index + 1])
            if part in BLOCKED_PARTS or joined in BLOCKED_PARTS:
                raise ToolError("I keep out of %s because it can hold secrets." % part)
        if must_exist and not os.path.lexists(real):
            raise ToolError("I couldn't find %s." % self.pretty(raw))
        return real

    def pretty(self, path):
        path = str(path)
        return "~" + path[len(self.home):] if path.startswith(self.home) else path

    # ---- tools -------------------------------------------------------------

    def t_search_files(self, query):
        query = str(query).strip()
        if not query:
            raise ToolError("What should I search for?")
        try:
            response = unix_request(SEMFS_SOCKET, {"op": "search", "query": query, "limit": 5}, 15)
        except (OSError, ValueError):
            raise ToolError("The file search service isn't running (try `aetherctl status`).")
        results = response.get("results") or []
        if not results:
            return "I didn't find any files about that. New files can take a few seconds to be indexed."
        lines = []
        for number, item in enumerate(results, 1):
            snippet = " ".join(str(item.get("snippet", "")).split())[:90]
            lines.append("%d. %s - %s" % (number, self.pretty(item.get("path", "?")), snippet))
        return "Here's what I found:\n" + "\n".join(lines)

    def t_list_folder(self, path="~"):
        folder = self.safe_path(path or "~", must_exist=True)
        if not os.path.isdir(folder):
            raise ToolError("%s isn't a folder." % self.pretty(folder))
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError as exc:
            raise ToolError("I couldn't read that folder (%s)." % exc.strerror)
        names = [name for name in names if not name.startswith(".")]
        if not names:
            return "%s is empty." % self.pretty(folder)
        entries = [name + ("/" if os.path.isdir(os.path.join(folder, name)) else "") for name in names]
        shown = entries[:40]
        extra = "" if len(entries) <= 40 else "\n... and %d more" % (len(entries) - 40)
        return "%s:\n%s%s" % (self.pretty(folder), "\n".join(shown), extra)

    def t_read_file(self, path):
        target = self.safe_path(path, must_exist=True)
        if not os.path.isfile(target):
            raise ToolError("%s isn't a file." % self.pretty(target))
        if os.path.getsize(target) > MAX_FILE_BYTES:
            raise ToolError("That file is too large for me to show.")
        with open(target, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES)
        if b"\x00" in data:
            raise ToolError("That looks like a binary file, so I won't print it.")
        text = data.decode("utf-8", errors="replace").strip()
        if not text:
            return "%s is empty." % self.pretty(target)
        return "%s:\n%s%s" % (self.pretty(target), text[:1500], "\n..." if len(text) > 1500 else "")

    def t_system_info(self):
        now = datetime.datetime.now()
        lines = ["It is %s on %s." % (now.strftime("%H:%M"), now.strftime("%A %d %B %Y"))]
        try:
            with open("/proc/meminfo", encoding="ascii") as handle:
                meminfo = dict(line.split(":", 1) for line in handle if ":" in line)
            total = int(meminfo["MemTotal"].split()[0]) // 1024
            available = int(meminfo["MemAvailable"].split()[0]) // 1024
            lines.append("Memory: %d MB free of %d MB." % (available, total))
        except (OSError, KeyError, ValueError):
            pass
        try:
            usage = shutil.disk_usage(self.home)
            lines.append("Disk: %.1f GB free of %.1f GB." % (usage.free / 1e9, usage.total / 1e9))
        except OSError:
            pass
        try:
            with open("/proc/uptime", encoding="ascii") as handle:
                minutes = int(float(handle.read().split()[0]) // 60)
            lines.append("Up for %d h %d min." % (minutes // 60, minutes % 60))
        except (OSError, ValueError, IndexError):
            pass
        try:
            for name in sorted(os.listdir("/sys/class/power_supply")):
                if name.startswith("BAT"):
                    with open("/sys/class/power_supply/%s/capacity" % name, encoding="ascii") as handle:
                        lines.append("Battery: %s%%." % handle.read().strip())
                    break
        except OSError:
            pass
        return "\n".join(lines)

    def t_recall(self):
        items = self.memory.list()
        if not items:
            return "I'm not remembering anything yet. Say 'remember that ...' to teach me."
        return "I remember:\n" + "\n".join("%d. %s" % (item["id"], item["text"]) for item in items)

    def t_show_activity(self):
        entries = self.audit.recent(10)
        if not entries:
            return "I haven't done anything yet."
        lines = []
        for entry in entries:
            lines.append("%s  %s  [%s]" % (entry["ts"][11:], entry["tool"], entry["status"]))
        return "My recent actions (full log: ~/.local/state/aether/actions.log):\n" + "\n".join(lines)

    def t_create_folder(self, path):
        target = self.safe_path(path)
        if os.path.lexists(target):
            raise ToolError("%s already exists." % self.pretty(target))
        try:
            os.makedirs(target, mode=0o755)
        except OSError as exc:
            raise ToolError("I couldn't create it (%s)." % exc.strerror)
        return "Created the folder %s." % self.pretty(target)

    def t_create_file(self, path, text):
        target = self.safe_path(path)
        text = str(text)
        if len(text) > MAX_NEW_FILE_CHARS:
            raise ToolError("That's too much text for me to write in one go.")
        if os.path.lexists(target):
            raise ToolError("%s already exists, and I never overwrite files." % self.pretty(target))
        try:
            os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
            with open(target, "x", encoding="utf-8") as handle:
                handle.write(text + ("\n" if text and not text.endswith("\n") else ""))
        except OSError as exc:
            raise ToolError("I couldn't write it (%s)." % exc.strerror)
        return "Saved %s." % self.pretty(target)

    def desktop_entries(self):
        entries = []
        for directory in self.applications_dirs:
            try:
                names = sorted(os.listdir(directory))
            except OSError:
                continue
            for filename in names:
                if not filename.endswith(".desktop"):
                    continue
                fields = {}
                in_entry = False
                try:
                    with open(os.path.join(directory, filename), encoding="utf-8", errors="replace") as handle:
                        for line in handle:
                            line = line.strip()
                            if line.startswith("["):
                                in_entry = line == "[Desktop Entry]"
                            elif in_entry and "=" in line and not line.startswith("#"):
                                key, value = line.split("=", 1)
                                fields.setdefault(key, value)
                except OSError:
                    continue
                if fields.get("Type") != "Application" or fields.get("NoDisplay") == "true" or fields.get("Hidden") == "true":
                    continue
                if "Name" in fields and "Exec" in fields:
                    entries.append({"id": filename[:-8], **fields})
        return entries

    ALIASES = {"browser": "firefox", "web browser": "firefox", "internet": "firefox", "files": "thunar",
               "file manager": "thunar", "terminal": "foot", "console": "foot", "editor": "mousepad",
               "text editor": "mousepad", "installer": "install aetheros"}

    def find_app(self, name):
        wanted = self.ALIASES.get(name.strip().lower(), name.strip().lower())
        if not wanted:
            return None
        entries = self.desktop_entries()
        for matcher in (
            lambda e: e["Name"].lower() == wanted or e["id"].lower() == wanted,
            lambda e: e["Name"].lower().startswith(wanted) or e["id"].lower().startswith(wanted),
            lambda e: wanted in e["Name"].lower() or wanted in e["id"].lower(),
        ):
            for entry in entries:
                if matcher(entry):
                    return entry
        return None

    def t_open_app(self, name):
        entry = self.find_app(str(name))
        if not entry:
            raise ToolError("I couldn't find an app called '%s'." % name)
        command = re.sub(r"%[a-zA-Z]", "", entry["Exec"].replace("%%", "\0")).replace("\0", "%")
        try:
            argv = shlex.split(command)
        except ValueError:
            raise ToolError("I couldn't understand how to start %s." % entry["Name"])
        if entry.get("Terminal") == "true":
            argv = ["foot", "-e"] + argv
        if not argv:
            raise ToolError("I couldn't start %s." % entry["Name"])
        try:
            self.launch(argv)
        except OSError as exc:
            raise ToolError("I couldn't start %s (%s)." % (entry["Name"], exc.strerror))
        return "Opening %s." % entry["Name"]

    def t_open_url(self, url):
        url = str(url).strip()
        if url.startswith("www."):
            url = "https://" + url
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ToolError("I can only open http or https addresses.")
        browser = shutil.which("firefox-esr") or shutil.which("firefox") or shutil.which("xdg-open")
        if not browser:
            raise ToolError("No web browser is installed.")
        try:
            self.launch([browser, url])
        except OSError as exc:
            raise ToolError("I couldn't open the browser (%s)." % exc.strerror)
        return "Opening %s." % url

    def _command(self, argv, missing):
        if not shutil.which(argv[0]):
            raise ToolError(missing)
        result = self.run(argv, capture_output=True, text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise ToolError("That didn't work (%s)." % (result.stderr or "error").strip()[:100])

    def t_set_volume(self, percent):
        percent = int(percent)
        if not 0 <= percent <= 100:
            raise ToolError("Volume must be between 0 and 100.")
        self._command(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "%d%%" % percent], "Audio control isn't available here.")
        return "Volume set to %d%%." % percent

    def t_set_mute(self, muted):
        self._command(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if int(muted) else "0"], "Audio control isn't available here.")
        return "Muted." if int(muted) else "Unmuted."

    def t_set_brightness(self, percent):
        percent = int(percent)
        if not 1 <= percent <= 100:
            raise ToolError("Brightness must be between 1 and 100.")
        self._command(["brightnessctl", "set", "%d%%" % percent], "Brightness control isn't available on this machine.")
        return "Brightness set to %d%%." % percent

    def t_screenshot(self):
        folder = os.path.join(self.home, "Pictures")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, "screenshot-%s.png" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        self._command(["grim", target], "Screenshots need the Wayland desktop (grim).")
        return "Saved a screenshot to %s." % self.pretty(target)

    def t_remember(self, text):
        item_id = self.memory.add(text)
        return "Okay, I'll remember that (memory #%d)." % item_id

    def t_forget(self, id):  # noqa: A002 - tool argument name
        self.memory.forget(int(id))
        return "Forgotten."

    def t_forget_all(self):
        self.memory.clear()
        return "I've erased everything I remembered."

    def t_move_path(self, source, destination):
        src = self.safe_path(source, must_exist=True)
        dst = self.safe_path(destination)
        if src == os.path.realpath(self.home):
            raise ToolError("I won't move your home folder.")
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))
        if os.path.lexists(dst):
            raise ToolError("%s already exists, so I didn't move anything." % self.pretty(dst))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        try:
            shutil.move(src, dst)
        except (OSError, shutil.Error) as exc:
            raise ToolError("The move failed (%s)." % exc)
        return "Moved to %s." % self.pretty(dst)

    def t_trash_path(self, path):
        target = self.safe_path(path, must_exist=True)
        real_home = os.path.realpath(self.home)
        trash_root = os.path.join(self.data_home, "Trash")
        if target == real_home or target.startswith(os.path.realpath(trash_root)):
            raise ToolError("I won't trash that.")
        files_dir = os.path.join(trash_root, "files")
        info_dir = os.path.join(trash_root, "info")
        os.makedirs(files_dir, mode=0o700, exist_ok=True)
        os.makedirs(info_dir, mode=0o700, exist_ok=True)
        base = os.path.basename(target.rstrip(os.sep)) or "item"
        name, counter = base, 1
        while os.path.lexists(os.path.join(files_dir, name)):
            counter += 1
            name = "%s.%d" % (base, counter)
        with open(os.path.join(info_dir, name + ".trashinfo"), "w", encoding="utf-8") as handle:
            handle.write("[Trash Info]\nPath=%s\nDeletionDate=%s\n" % (
                urllib.parse.quote(target), datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
        try:
            shutil.move(target, os.path.join(files_dir, name))
        except (OSError, shutil.Error) as exc:
            raise ToolError("I couldn't move it to the Trash (%s)." % exc)
        return "Moved %s to the Trash. You can restore it from ~/.local/share/Trash/files." % self.pretty(target)

    def t_power(self, action):
        action = str(action).strip().lower()
        if action not in ("poweroff", "reboot"):
            raise ToolError("I can only power off or reboot.")
        self.run(["systemctl", action], check=False, timeout=15)
        return "Okay, %s now." % ("rebooting" if action == "reboot" else "powering off")

    # ---- running tools -------------------------------------------------------

    def validate_args(self, tool, args):
        if not isinstance(args, dict):
            raise ToolError("Invalid arguments.")
        clean = {}
        for name, kind in tool.params.items():
            if name not in args:
                raise ToolError("Missing '%s'." % name)
            value = args[name]
            if kind == "int":
                try:
                    clean[name] = int(value)
                except (TypeError, ValueError):
                    raise ToolError("'%s' must be a number." % name)
            else:
                if not isinstance(value, (str, int, float)):
                    raise ToolError("'%s' must be text." % name)
                clean[name] = str(value)
        return clean

    def execute(self, name, args):
        """Run a tool now (approval, if needed, was already given)."""
        tool = self.tools[name]
        clean = self.validate_args(tool, args)
        logged = dict(clean)
        if "text" in logged and name in ("create_file", "remember"):
            logged["text"] = "<%d chars>" % len(logged["text"])
        try:
            text = tool.func(**clean)
        except ToolError as exc:
            self.audit.record(name, logged, "refused", exc)
            return Reply(str(exc), ok=False)
        except (OSError, subprocess.SubprocessError) as exc:
            self.audit.record(name, logged, "error", exc)
            return Reply("Something went wrong: %s" % exc, ok=False)
        self.audit.record(name, logged, "ok")
        return Reply(text)

    def request(self, name, args, from_model=False):
        """Run now, or ask for approval when the tool or its source requires it."""
        tool = self.tools[name]
        try:
            clean = self.validate_args(tool, args)
        except ToolError as exc:
            return Reply(str(exc), ok=False)
        needs_approval = tool.confirm or (from_model and not tool.read_only)
        if needs_approval:
            summary = tool.summary(clean) if tool.confirm else "%s %s" % (name.replace("_", " "), json.dumps(clean, ensure_ascii=False))
            return Reply("I can do that. Please confirm: %s." % summary, pending=Pending(name, clean, summary))
        return self.execute(name, clean)

    def confirm(self, pending, approved):
        if not approved:
            self.audit.record(pending.tool, pending.args, "denied")
            return self._remember_turn(Reply("Okay, I won't do that."))
        return self._remember_turn(self.execute(pending.tool, pending.args))

    # ---- intent router (explicit commands) ----------------------------------------

    def route(self, text):
        t = text.strip()
        low = re.sub(r"^(?:please|hey aether|aether|can you|could you|would you)[,\s]+", "", t, flags=re.I).strip()
        low = re.sub(r"[?.!]+$", "", low).strip()
        lower = low.lower()
        match = None

        if lower in ("help", "what can you do", "what can you do for me", "commands"):
            return Reply(self.help_text())

        match = re.match(r"^(?:create|make|add|new)\s+(?:a\s+)?(?:new\s+)?(?:folder|directory|dir)\s+(?:called|named)?\s*(.+)$", low, re.I)
        if match:
            return self.request("create_folder", {"path": match.group(1)})
        match = re.match(r"^(?:create|make|write|save)\s+(?:a\s+)?(?:new\s+)?(?:note|file|text file)\s+(?:called|named)\s+(\S.*?)\s+(?:with|containing|that says|saying)\s+(.+)$", low, re.I | re.S)
        if match:
            name = match.group(1).strip()
            if "." not in os.path.basename(name):
                name += ".txt"
            return self.request("create_file", {"path": name, "text": match.group(2).strip()})
        match = re.match(r"^(?:search|find)\s+(?:my\s+)?(?:files?|notes?|documents?)\s+(?:about|on|for|named|containing)?\s*(.+)$", low, re.I) \
            or re.match(r"^search\s+(?:for\s+)?(.+)$", low, re.I)
        if match:
            return self.request("search_files", {"query": match.group(1)})
        match = re.match(r"^(?:list|show)\s+(?:me\s+)?(?:the\s+)?(?:files|folders|contents)(?:\s+(?:in|of)\s+(.+))?$", low, re.I) \
            or re.match(r"^ls(?:\s+(.+))?$", low, re.I)
        if match:
            return self.request("list_folder", {"path": match.group(1) or "~"})
        match = re.match(r"^(?:read|cat|show)\s+(?:me\s+)?(?:the\s+)?file\s+(.+)$", low, re.I)
        if match:
            return self.request("read_file", {"path": match.group(1)})
        match = re.match(r"^(?:go to|open|visit)\s+((?:https?://|www\.)\S+)$", low, re.I)
        if match:
            return self.request("open_url", {"url": match.group(1)})
        match = re.match(r"^(?:delete|trash)\s+(?:the\s+)?(?:file|folder|directory)?\s*(.+)$", low, re.I)
        if match:
            return self.request("trash_path", {"path": match.group(1)})
        match = re.match(r"^(?:move|rename)\s+(.+?)\s+(?:to|as|into)\s+(.+)$", low, re.I)
        if match:
            return self.request("move_path", {"source": match.group(1), "destination": match.group(2)})
        match = re.match(r"^(?:set\s+)?volume\s+(?:to\s+)?(\d{1,3})\s*%?$", low, re.I)
        if match:
            return self.request("set_volume", {"percent": int(match.group(1))})
        if lower in ("mute", "unmute"):
            return self.request("set_mute", {"muted": 1 if lower == "mute" else 0})
        match = re.match(r"^(?:set\s+)?brightness\s+(?:to\s+)?(\d{1,3})\s*%?$", low, re.I)
        if match:
            return self.request("set_brightness", {"percent": int(match.group(1))})
        if re.match(r"^(?:take\s+(?:a\s+)?)?screenshot$", lower):
            return self.request("take_screenshot", {})
        if re.match(r"^(?:what(?:'s| is)\s+(?:the\s+)?)?(?:time|date|time and date)(?:\s+now)?$", lower) \
                or lower in ("what time is it", "what day is it", "system info", "system status", "how is my computer", "how is my system", "battery"):
            return self.request("system_info", {})
        match = re.match(r"^remember(?:\s+that)?\s+(.+)$", low, re.I)
        if match:
            return self.request("remember", {"text": match.group(1)})
        if re.match(r"^(?:what do you remember.*|(?:show|list)\s+(?:my\s+)?memor(?:y|ies)|recall)$", lower):
            return self.request("recall", {})
        if re.match(r"^forget\s+(?:everything|all)$", lower):
            return self.request("forget_all", {})
        match = re.match(r"^forget\s+(?:memory\s+)?#?(\d+)$", lower)
        if match:
            return self.request("forget", {"id": int(match.group(1))})
        if re.match(r"^(?:what did you do|show activity|recent actions|activity)$", lower):
            return self.request("show_activity", {})
        if re.match(r"^(?:shut\s?down|power\s?off|turn off (?:the )?computer)$", lower):
            return self.request("power", {"action": "poweroff"})
        if re.match(r"^(?:reboot|restart(?: the computer)?)$", lower):
            return self.request("power", {"action": "reboot"})
        match = re.match(r"^(?:open|launch|start)\s+(?:the\s+)?(.+?)(?:\s+app(?:lication)?)?$", low, re.I)
        if match and self.find_app(match.group(1)):
            return self.request("open_app", {"name": match.group(1)})
        return None

    def help_text(self):
        return (
            "I'm Aether, running privately on this computer. Things you can ask me:\n"
            "- create a folder called Projects\n"
            "- create a note called todo with buy milk\n"
            "- search my files for travel plans\n"
            "- list files in Documents / read file notes.txt\n"
            "- open Firefox / open files / open terminal\n"
            "- volume 40 / mute / brightness 70 / take a screenshot\n"
            "- remember that my sister's birthday is 3 May / what do you remember\n"
            "- move a.txt to Documents / delete old.txt (I'll ask first)\n"
            "- what time is it / what did you do\n"
            "I only work inside your home folder, ask before anything destructive, and log what I do.\n"
            "For everything else I'll chat using the local model."
        )

    # ---- language model path --------------------------------------------------------

    def system_prompt(self):
        tool_lines = "\n".join("- %s: %s" % (tool.name, tool.description) for tool in self.tools.values())
        memory = self.memory.prompt_block()
        return (
            "You are Aether, a private assistant that runs on the user's own computer. "
            "Be honest, warm and brief (usually one to three sentences). Say so when you are unsure. "
            "Never claim to have done something you did not do.\n"
            "If the user asks you to do something on their computer that one of the tools below can do, "
            "reply with ONLY one line in exactly this form and nothing else:\n"
            'ACTION {"tool": "<name>", "args": {...}}\n'
            "Tools:\n" + tool_lines + ("\n\n" + memory if memory else "")
        )

    ACTION_RE = re.compile(r"^\s*ACTION\s*(\{.*\})\s*$", re.S)

    def parse_action(self, answer):
        match = self.ACTION_RE.match(answer.strip())
        if not match:
            return None
        try:
            data = json.loads(match.group(1))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("tool") not in self.tools or not isinstance(data.get("args", {}), dict):
            return None
        return data["tool"], data.get("args", {})

    def _remember_turn(self, reply):
        self.history.append({"role": "assistant", "content": reply.text})
        del self.history[:-HISTORY_LIMIT]
        return reply

    def ask_model(self, text):
        messages = [{"role": "system", "content": self.system_prompt()}] + self.history[-HISTORY_LIMIT:]
        try:
            answer = self.backend.ask(messages)
        except AidError as exc:
            return Reply(SAFE_MESSAGES.get(exc.kind, SAFE_MESSAGES["unavailable"]), ok=False)
        action = self.parse_action(answer)
        if action:
            return self.request(action[0], action[1], from_model=True)
        return Reply(answer or "I don't have an answer for that.")

    def handle(self, text):
        """Process one message from the person and return a Reply."""
        text = text.strip()
        if not text:
            return Reply("Say something and I'll help.", ok=False)
        if len(text) > 2000:
            return Reply("That message is too long for me. Try a shorter one.", ok=False)
        self.history.append({"role": "user", "content": text})
        del self.history[:-HISTORY_LIMIT]
        reply = self.route(text)
        if reply is None:
            reply = self.ask_model(text)
        return self._remember_turn(reply)
