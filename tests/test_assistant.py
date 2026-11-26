#!/usr/bin/python3
"""Run with: python3 tests/test_assistant.py"""
import importlib.machinery
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    path = ROOT / relative
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


agent = load("aether_agent", "overlay/usr/libexec/aether/agent.py")


class FakeBackend:
    def __init__(self, answer="Hello there.", error=None):
        self.answer = answer
        self.error = error
        self.calls = []

    def ask(self, messages, max_tokens=256):
        self.calls.append(messages)
        if self.error:
            raise agent.AidError(self.error)
        return self.answer


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = os.path.realpath(self.temp.name)
        self.launched = []
        self.commands = []
        self.apps = os.path.join(self.home, "apps")
        os.makedirs(self.apps)
        self.write_desktop("firefox-esr", "Firefox ESR", "firefox-esr %u")
        self.write_desktop("htop", "Htop", "htop", extra="Terminal=true\n")
        self.write_desktop("hidden", "Hidden Tool", "hidden", extra="NoDisplay=true\n")
        self.backend = FakeBackend()
        self.agent = agent.Agent(
            home=self.home, backend=self.backend,
            runner=self.fake_run, launcher=self.launched.append, applications_dirs=[self.apps])

    def fake_run(self, argv, **kwargs):
        self.commands.append(argv)
        return mock.Mock(returncode=0, stderr="", stdout="")

    def write_desktop(self, filename, name, exec_line, extra=""):
        Path(self.apps, filename + ".desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=%s\nExec=%s\n%s" % (name, exec_line, extra))

    def path(self, *parts):
        return os.path.join(self.home, *parts)

    # ---- folders and files ----

    def test_create_folder_and_refuses_duplicates(self):
        reply = self.agent.handle("Please create a folder called Projects")
        self.assertTrue(reply.ok, reply.text)
        self.assertTrue(os.path.isdir(self.path("Projects")))
        self.assertFalse(self.agent.handle("make a new folder named Projects").ok)
        self.assertEqual(self.backend.calls, [], "explicit commands must not use the model")

    def test_create_file_never_overwrites_and_adds_extension(self):
        reply = self.agent.handle("create a note called todo with buy milk and eggs")
        self.assertTrue(reply.ok, reply.text)
        self.assertEqual(Path(self.path("todo.txt")).read_text(), "buy milk and eggs\n")
        again = self.agent.handle("create a note called todo with something else")
        self.assertFalse(again.ok)
        self.assertEqual(Path(self.path("todo.txt")).read_text(), "buy milk and eggs\n")

    def test_paths_outside_home_are_refused(self):
        for text in ("create a folder called ../escape", "create a folder called /tmp/aether-escape",
                     "list files in /etc", "read file /etc/passwd"):
            reply = self.agent.handle(text)
            self.assertFalse(reply.ok, text)
            self.assertIn("home folder", reply.text, text)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.home), "escape")))

    def test_symlink_escape_is_refused(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, outside)
        os.symlink(outside, self.path("link"))
        reply = self.agent.handle("create a folder called link/inside")
        self.assertFalse(reply.ok)
        self.assertEqual(os.listdir(outside), [])

    def test_secret_folders_are_blocked(self):
        os.makedirs(self.path(".ssh"))
        Path(self.path(".ssh", "id_rsa")).write_text("PRIVATE")
        reply = self.agent.handle("read file .ssh/id_rsa")
        self.assertFalse(reply.ok)
        self.assertNotIn("PRIVATE", reply.text)

    def test_list_and_read(self):
        os.makedirs(self.path("Docs"))
        Path(self.path("Docs", "a.txt")).write_text("hello world")
        Path(self.path("Docs", ".hidden")).write_text("x")
        listing = self.agent.handle("list files in Docs")
        self.assertIn("a.txt", listing.text)
        self.assertNotIn(".hidden", listing.text)
        self.assertIn("hello world", self.agent.handle("read file Docs/a.txt").text)
        Path(self.path("bin.dat")).write_bytes(b"\x00\x01\x02")
        self.assertFalse(self.agent.handle("read file bin.dat").ok)

    # ---- confirmation ----

    def test_trash_needs_approval_and_is_reversible(self):
        Path(self.path("old.txt")).write_text("data")
        reply = self.agent.handle("delete old.txt")
        self.assertIsNotNone(reply.pending)
        self.assertTrue(os.path.exists(self.path("old.txt")), "nothing may happen before approval")
        denied = self.agent.confirm(reply.pending, False)
        self.assertTrue(os.path.exists(self.path("old.txt")))
        self.assertIn("won't", denied.text)
        approved = self.agent.confirm(reply.pending, True)
        self.assertTrue(approved.ok, approved.text)
        self.assertFalse(os.path.exists(self.path("old.txt")))
        trashed = Path(self.path(".local/share/Trash/files/old.txt"))
        self.assertEqual(trashed.read_text(), "data")
        self.assertTrue(Path(self.path(".local/share/Trash/info/old.txt.trashinfo")).exists())

    def test_cannot_trash_home(self):
        reply = self.agent.handle("delete ~")
        approved = self.agent.confirm(reply.pending, True)
        self.assertFalse(approved.ok)
        self.assertTrue(os.path.isdir(self.home))

    def test_move_needs_approval_and_never_overwrites(self):
        Path(self.path("a.txt")).write_text("A")
        Path(self.path("b.txt")).write_text("B")
        os.makedirs(self.path("Documents"))
        reply = self.agent.handle("move a.txt to Documents")
        self.assertIsNotNone(reply.pending)
        self.assertTrue(os.path.exists(self.path("a.txt")))
        self.assertTrue(self.agent.confirm(reply.pending, True).ok)
        self.assertTrue(os.path.exists(self.path("Documents", "a.txt")))
        clash = self.agent.handle("move b.txt to Documents/a.txt")
        self.assertFalse(self.agent.confirm(clash.pending, True).ok)
        self.assertEqual(Path(self.path("Documents", "a.txt")).read_text(), "A")

    def test_power_needs_approval(self):
        reply = self.agent.handle("shut down")
        self.assertIsNotNone(reply.pending)
        self.assertEqual(self.commands, [])
        self.agent.confirm(reply.pending, True)
        self.assertEqual(self.commands, [["systemctl", "poweroff"]])

    # ---- model-proposed actions are untrusted ----

    def test_model_actions_need_approval_unless_read_only(self):
        self.backend.answer = 'ACTION {"tool": "create_folder", "args": {"path": "FromModel"}}'
        reply = self.agent.handle("I'd like somewhere to keep my ideas")
        self.assertIsNotNone(reply.pending)
        self.assertFalse(os.path.exists(self.path("FromModel")))
        self.assertTrue(self.agent.confirm(reply.pending, True).ok)
        self.assertTrue(os.path.isdir(self.path("FromModel")))

        os.makedirs(self.path("Visible"))
        self.backend.answer = 'ACTION {"tool": "list_folder", "args": {"path": "~"}}'
        reply = self.agent.handle("what do I have")
        self.assertIsNone(reply.pending)
        self.assertIn("Visible", reply.text)

    def test_model_cannot_use_unknown_tools_or_bad_json(self):
        for answer in ('ACTION {"tool": "rm_rf", "args": {}}', "ACTION {not json}", 'ACTION {"tool": "list_folder", "args": "x"}'):
            self.backend.answer = answer
            reply = self.agent.handle("hi there friend")
            self.assertIsNone(reply.pending)
        self.assertEqual(sorted(os.listdir(self.home)), ["apps"])

    def test_model_action_with_missing_args(self):
        self.backend.answer = 'ACTION {"tool": "create_folder", "args": {}}'
        reply = self.agent.handle("do something")
        self.assertFalse(reply.ok)
        self.assertIn("Missing", reply.text)

    def test_backend_unavailable_message(self):
        self.agent.backend = FakeBackend(error="unavailable")
        reply = self.agent.handle("tell me a story")
        self.assertFalse(reply.ok)
        self.assertIn("language model", reply.text)
        self.assertTrue(self.agent.handle("create a folder called Still").ok)

    def test_chat_uses_system_prompt_and_history(self):
        self.backend.answer = "Nice to meet you."
        self.agent.handle("Hello, I'm Sam")
        self.agent.handle("What's my name?")
        messages = self.backend.calls[-1]
        self.assertEqual(messages[0]["role"], "system")
        self.assertIn("Aether", messages[0]["content"])
        self.assertTrue(any("Sam" in m["content"] for m in messages))

    # ---- memory ----

    def test_memory_roundtrip_and_privacy(self):
        self.assertIn("#1", self.agent.handle("remember that my sister's birthday is 3 May").text)
        self.assertIn("3 May", self.agent.handle("what do you remember?").text)
        memory_file = self.path(".local/share/aether/memory.json")
        self.assertEqual(stat.S_IMODE(os.stat(memory_file).st_mode), 0o600)
        self.backend.answer = "ok"
        self.agent.handle("random question")
        self.assertIn("3 May", self.backend.calls[-1][0]["content"])
        self.assertTrue(self.agent.handle("forget 1").ok)
        self.assertIn("not remembering", self.agent.handle("what do you remember").text)

    def test_forget_all_needs_approval(self):
        self.agent.handle("remember that apples are red")
        reply = self.agent.handle("forget everything")
        self.assertIsNotNone(reply.pending)
        self.assertEqual(len(self.agent.memory.list()), 1)
        self.agent.confirm(reply.pending, True)
        self.assertEqual(self.agent.memory.list(), [])

    def test_memory_limits(self):
        for index in range(agent.MAX_MEMORY_ITEMS):
            self.agent.memory.add("item %d" % index)
        self.assertFalse(self.agent.handle("remember that one more").ok)

    # ---- apps and system ----

    def test_open_app_strips_field_codes_and_handles_terminal_apps(self):
        self.assertTrue(self.agent.handle("open firefox").ok)
        self.assertEqual(self.launched[-1], ["firefox-esr"])
        self.assertTrue(self.agent.handle("launch the htop app").ok)
        self.assertEqual(self.launched[-1], ["foot", "-e", "htop"])

    def test_unknown_or_hidden_apps_fall_through_to_model(self):
        self.backend.answer = "I'm not sure what you mean."
        self.agent.handle("open hidden tool")
        self.agent.handle("open the pod bay doors")
        self.assertEqual(self.launched, [])
        self.assertEqual(len(self.backend.calls), 2)

    def test_open_url_only_http(self):
        with mock.patch.object(agent.shutil, "which", return_value="/usr/bin/firefox-esr"):
            self.assertTrue(self.agent.handle("open https://example.org").ok)
            self.assertEqual(self.launched[-1], ["/usr/bin/firefox-esr", "https://example.org"])
            self.assertFalse(self.agent.request("open_url", {"url": "file:///etc/passwd"}).ok)
            self.assertFalse(self.agent.request("open_url", {"url": "javascript:alert(1)"}).ok)

    def test_volume_brightness_validation(self):
        with mock.patch.object(agent.shutil, "which", return_value="/usr/bin/wpctl"):
            self.assertTrue(self.agent.handle("volume 40").ok)
            self.assertEqual(self.commands[-1], ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "40%"])
            self.assertFalse(self.agent.handle("volume 400").ok)
            self.assertFalse(self.agent.handle("brightness 0").ok)
        with mock.patch.object(agent.shutil, "which", return_value=None):
            self.assertFalse(self.agent.handle("mute").ok)

    def test_system_info(self):
        reply = self.agent.handle("what time is it?")
        self.assertIn("It is", reply.text)

    def test_search_files_formats_results(self):
        results = {"status": "ok", "results": [{"path": self.path("notes", "idea.txt"), "snippet": "a trustworthy buddy OS", "score": 0.9}]}
        with mock.patch.object(agent, "unix_request", return_value=results):
            reply = self.agent.handle("search my files for trustworthy buddy")
        self.assertIn("~/notes/idea.txt", reply.text)
        with mock.patch.object(agent, "unix_request", side_effect=OSError("no socket")):
            self.assertFalse(self.agent.handle("search for anything").ok)

    # ---- audit ----

    def test_audit_log_records_actions_without_file_contents(self):
        self.agent.handle("create a note called secret with my password is hunter2")
        Path(self.path("x.txt")).write_text("x")
        denied = self.agent.handle("delete x.txt")
        self.agent.confirm(denied.pending, False)
        log = Path(self.path(".local/state/aether/actions.log")).read_text()
        self.assertNotIn("hunter2", log)
        entries = [json.loads(line) for line in log.splitlines()]
        self.assertEqual([e["status"] for e in entries], ["ok", "denied"])
        self.assertIn("create_file", self.agent.handle("what did you do").text)

    def test_help_lists_abilities(self):
        text = self.agent.handle("help").text
        self.assertIn("create a folder", text)
        self.assertIn("home folder", text)

    def test_empty_and_oversized_messages(self):
        self.assertFalse(self.agent.handle("   ").ok)
        self.assertFalse(self.agent.handle("x" * 3000).ok)


class BackendTests(unittest.TestCase):
    def test_backend_maps_statuses(self):
        backend = agent.Backend(socket_path="/nonexistent/socket")
        with self.assertRaises(agent.AidError) as caught:
            backend.ask([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.kind, "unavailable")
        with mock.patch.object(agent, "unix_request", return_value={"status": "ok", "answer": " hi \n"}):
            self.assertEqual(backend.ask([{"role": "user", "content": "hi"}]), "hi")
        with mock.patch.object(agent, "unix_request", return_value={"status": "timeout"}):
            with self.assertRaises(agent.AidError) as caught:
                backend.ask([{"role": "user", "content": "hi"}])
            self.assertEqual(caught.exception.kind, "timeout")


if __name__ == "__main__":
    unittest.main(verbosity=1)
