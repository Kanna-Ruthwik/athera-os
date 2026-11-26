#!/usr/bin/python3
"""Aether assistant window (GTK 4 + libadwaita).

Opened with Super+Space. Pressing the hotkey again just focuses the running
window; Escape closes it (and frees its memory). All logic lives in agent.py.

Testing helpers:  --send "text"  submits a message at start-up,
                  --quit-after N closes the window after N seconds.
"""
import argparse
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

CSS = """
.aether-root { background-color: #0f1117; color: #e6e9f2; }
.aether-root headerbar { background-color: #0f1117; box-shadow: none; border-bottom: 1px solid rgba(138,180,255,0.14); }
.aether-root headerbar .title { color: #e6e9f2; font-weight: 600; }
.aether-root headerbar .subtitle { color: #8b93a7; font-size: 11px; }
.bubble { padding: 10px 14px; border-radius: 16px; font-size: 14px; }
.bubble.user { background-color: #2b4a8f; color: #ffffff; }
.bubble.assistant { background-color: #1a1e2b; color: #e6e9f2; }
.bubble.dim { color: #8b93a7; }
.bubble.error { background-color: #3a1f28; color: #ffd6dd; }
.chip { background: #171a23; color: #8ab4ff; border-radius: 14px; padding: 6px 12px; border: 1px solid rgba(138,180,255,0.25); }
.chip:hover { background: #1d2230; }
.confirm { background-color: #1d2230; border-radius: 14px; padding: 10px 14px; border: 1px solid rgba(138,180,255,0.3); }
.prompt { background-color: #171a23; color: #e6e9f2; border-radius: 22px; padding: 8px 16px; min-height: 26px; border: 1px solid rgba(138,180,255,0.18); }
.prompt:focus-within { border-color: #8ab4ff; }
.send { background: #8ab4ff; color: #0f1117; border-radius: 20px; padding: 6px 18px; font-weight: 600; }
.speak { background: #1a1e2b; color: #e6e9f2; border-radius: 20px; padding: 6px 14px; }
.allow { background: #8ab4ff; color: #0f1117; border-radius: 12px; padding: 4px 14px; font-weight: 600; }
.cancel { background: #2a2f3d; color: #e6e9f2; border-radius: 12px; padding: 4px 14px; }
"""

SUGGESTIONS = ["What can you do?", "Create a folder called Projects", "What time is it?", "Open the terminal"]


def load_agent():
    for name in ("agent.py", "agent.py.txt"):
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            loader = importlib.machinery.SourceFileLoader("aether_agent", path)
            spec = importlib.util.spec_from_loader("aether_agent", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
    raise RuntimeError("agent module not found next to " + __file__)


class AssistantWindow(Adw.ApplicationWindow):
    def __init__(self, app, agent_module):
        super().__init__(application=app, title="Aether")
        self.agent_module = agent_module
        self.agent = agent_module.Agent()
        self.busy = False
        self.pending = None
        self.set_default_size(760, 660)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("aether-root")
        self.set_content(root)

        self.title_widget = Adw.WindowTitle(title="Aether", subtitle="Private assistant, running on this computer")
        header = Adw.HeaderBar()
        header.set_title_widget(self.title_widget)
        root.append(header)

        self.messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.messages.set_margin_top(16)
        self.messages.set_margin_bottom(8)
        self.messages.set_margin_start(18)
        self.messages.set_margin_end(18)
        self.scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.scroll.set_child(self.messages)
        root.append(self.scroll)

        self.confirm_label = Gtk.Label(wrap=True, xalign=0, hexpand=True)
        allow = Gtk.Button(label="Allow")
        allow.add_css_class("allow")
        allow.connect("clicked", lambda _b: self.answer_pending(True))
        cancel = Gtk.Button(label="Cancel")
        cancel.add_css_class("cancel")
        cancel.connect("clicked", lambda _b: self.answer_pending(False))
        confirm_box = Gtk.Box(spacing=10)
        confirm_box.add_css_class("confirm")
        confirm_box.set_margin_start(18)
        confirm_box.set_margin_end(18)
        confirm_box.append(self.confirm_label)
        confirm_box.append(cancel)
        confirm_box.append(allow)
        self.confirm_revealer = Gtk.Revealer(child=confirm_box, transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        root.append(self.confirm_revealer)

        self.entry = Gtk.Entry(hexpand=True, placeholder_text="Ask me anything, or tell me what to do")
        self.entry.add_css_class("prompt")
        self.entry.connect("activate", lambda _e: self.submit())
        speak = Gtk.Button(label="Speak")
        speak.add_css_class("speak")
        speak.connect("clicked", lambda _b: self.listen())
        send = Gtk.Button(label="Send")
        send.add_css_class("send")
        send.connect("clicked", lambda _b: self.submit())
        row = Gtk.Box(spacing=8)
        row.set_margin_top(10)
        row.set_margin_bottom(16)
        row.set_margin_start(18)
        row.set_margin_end(18)
        row.append(self.entry)
        row.append(speak)
        row.append(send)
        root.append(row)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        self.add_bubble("Hi, I'm Aether. I run privately on this computer. Ask me something, or try one of these:", "assistant")
        chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4, row_spacing=6, column_spacing=6)
        chips.set_hexpand(True)
        chips.set_homogeneous(False)
        for text in SUGGESTIONS:
            chip = Gtk.Button(label=text)
            chip.add_css_class("chip")
            chip.connect("clicked", lambda _b, value=text: self.send(value))
            chips.append(chip)
        self.messages.append(chips)

        threading.Thread(target=self.check_backend, daemon=True).start()

    # ---- helpers ----

    def on_key(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def add_bubble(self, text, role):
        label = Gtk.Label(label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, selectable=True)
        label.set_max_width_chars(60)
        for css in ("bubble",) + tuple(role.split()):
            label.add_css_class(css)
        label.set_halign(Gtk.Align.END if role.startswith("user") else Gtk.Align.START)
        self.messages.append(label)
        GLib.idle_add(self.scroll_to_end)
        return label

    def scroll_to_end(self):
        adjustment = self.scroll.get_vadjustment()
        adjustment.set_value(adjustment.get_upper() - adjustment.get_page_size())
        return False

    def check_backend(self):
        try:
            health = self.agent_module.unix_request(self.agent_module.AID_SOCKET, {"op": "health"}, 2)
            ready = bool(health.get("backend_reachable"))
            text = "Local model ready" if ready else "Local model not running"
        except (OSError, ValueError):
            text = "Services not running"
        GLib.idle_add(self.title_widget.set_subtitle, text)

    # ---- sending ----

    def submit(self):
        self.send(self.entry.get_text())

    def send(self, text):
        text = text.strip()
        if not text or self.busy:
            return
        self.entry.set_text("")
        self.add_bubble(text, "user")
        self.busy = True
        self.thinking = self.add_bubble("Thinking...", "assistant dim")
        threading.Thread(target=self.worker, args=(self.agent.handle, text), daemon=True).start()

    def worker(self, function, *args):
        try:
            reply = function(*args)
        except Exception as exc:  # keep the window alive whatever a tool does
            reply = self.agent_module.Reply("Something went wrong: %s" % exc, ok=False)
        GLib.idle_add(self.show_reply, reply)

    def show_reply(self, reply):
        self.thinking.set_text(reply.text)
        self.thinking.remove_css_class("dim")
        self.thinking.add_css_class("error" if not reply.ok else "assistant")
        self.busy = False
        if reply.pending:
            self.pending = reply.pending
            self.confirm_label.set_text(reply.pending.summary + "?")
            self.confirm_revealer.set_reveal_child(True)
        else:
            self.entry.grab_focus()
        GLib.idle_add(self.scroll_to_end)
        return False

    def answer_pending(self, approved):
        pending, self.pending = self.pending, None
        self.confirm_revealer.set_reveal_child(False)
        if not pending or self.busy:
            return
        self.busy = True
        self.thinking = self.add_bubble("Working...", "assistant dim")
        threading.Thread(target=self.worker, args=(lambda p=pending, a=approved: self.agent.confirm(p, a),), daemon=True).start()

    def listen(self):
        if self.busy:
            return
        self.busy = True
        bubble = self.add_bubble("Listening for 5 seconds...", "assistant dim")
        threading.Thread(target=self.listen_worker, args=(bubble,), daemon=True).start()

    def listen_worker(self, bubble):
        text = ""
        try:
            result = subprocess.run(["aetherctl", "listen", "--seconds", "5"], capture_output=True, text=True, timeout=30)
            text = result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError):
            text = ""
        GLib.idle_add(self.heard, bubble, text)

    def heard(self, bubble, text):
        self.messages.remove(bubble)
        self.busy = False
        if text:
            self.send(text)
        else:
            self.add_bubble("I didn't catch anything. Check that a microphone is connected and enabled.", "assistant error")
        return False


class AetherApp(Adw.Application):
    def __init__(self, options):
        super().__init__(application_id="os.aether.Assistant")
        self.options = options
        self.window = None

    def do_activate(self):
        if self.window is None:
            Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
            provider = Gtk.CssProvider()
            if hasattr(provider, "load_from_string"):
                provider.load_from_string(CSS)
            else:
                provider.load_from_data(CSS.encode("utf-8"))
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.window = AssistantWindow(self, load_agent())
            if self.options.send:
                GLib.timeout_add(800, lambda: self.window.send(self.options.send) or False)
            if self.options.quit_after:
                GLib.timeout_add_seconds(self.options.quit_after, self.quit)
        self.window.present()
        self.window.entry.grab_focus()


def main():
    parser = argparse.ArgumentParser(prog="aether-assistant")
    parser.add_argument("--send", help="submit this message at start-up (testing)")
    parser.add_argument("--quit-after", type=int, default=0, help="close after N seconds (testing)")
    options = parser.parse_args()
    return AetherApp(options).run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
