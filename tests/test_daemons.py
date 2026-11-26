#!/usr/bin/python3
"""Run with: python3 tests/test_daemons.py"""
import base64
import hashlib
import hmac
import ctypes.util
import importlib.machinery
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import types
import unittest
from unittest import mock
import sys
import wave

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]

def load(name, relative):
    path = ROOT / relative
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module

aid = load("aether_aid", "overlay/usr/libexec/aether/aid.py")
semfs = load("aether_semfsd", "overlay/usr/libexec/aether/semfsd.py")
execd = load("aether_execd", "overlay/usr/libexec/aether/execd.py")
speech = load("aether_speech", "overlay/usr/libexec/aether/speech.py")


def new_semfs_database():
    database = sqlite3.connect(":memory:", check_same_thread=False)
    database.execute("CREATE TABLE files (path TEXT PRIMARY KEY, inode INTEGER NOT NULL, owner_uid INTEGER NOT NULL, owner_gid INTEGER NOT NULL, mode INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, content TEXT NOT NULL, content_sha256 TEXT NOT NULL DEFAULT '', embedding BLOB, embedding_model TEXT)")
    return database


class DaemonContractTests(unittest.TestCase):
    def test_sources_compile(self):
        sources = list((ROOT / "overlay/usr/libexec/aether").glob("*.py"))
        self.assertGreaterEqual(len(sources), 4)
        for source in sources:
            compile(source.read_text(encoding="utf-8"), str(source), "exec")

    def test_inference_request_validation(self):
        op, payload, priority = aid.validate_request({"op": "generate", "priority": "background", "messages": [{"role": "user", "content": "hello"}], "max_tokens": 16})
        self.assertEqual(op, "generate")
        self.assertEqual(priority, "background")
        self.assertEqual(payload[0][0]["content"], "hello")
        with self.assertRaises(ValueError):
            aid.validate_request({"messages": [{"role": "user", "content": "x" * 12001}]})
        with self.assertRaises(ValueError):
            aid.validate_request({"messages": [{"role": "tool", "content": "run this"}]})
        with self.assertRaises(ValueError):
            aid.validate_request({"messages": [{"role": "user", "content": "x"}], "max_tokens": True})

    def test_inference_calls_only_the_local_compatible_backend(self):
        class Backend(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                body = json.dumps({"choices": [{"message": {"content": "local response"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        old_port = aid.BACKEND_PORT
        aid.BACKEND_PORT = server.server_port
        thread.start()
        try:
            self.assertEqual(aid.BACKEND_HOST, "127.0.0.1")
            self.assertEqual(aid.backend_generate([{"role": "user", "content": "hello"}], 12), "local response")
        finally:
            aid.BACKEND_PORT = old_port
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_index_is_owner_scoped_and_rechecks_file_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "notes.md"
            path.write_text("Project Aether privacy notes", encoding="utf-8")
            os.chmod(path, 0o600)
            old_root, old_database, old_embed_text = semfs.ROOT, semfs.database, semfs.embed_text
            semfs.ROOT = root
            semfs.database = new_semfs_database()
            semfs.embed_text = lambda _text: [1.0, 0.0]
            semfs.index_file(str(path))
            self.assertEqual(len(semfs.search("privacy notes", os.getuid(), 3)), 1)
            self.assertEqual(semfs.search("privacy notes", os.getuid() + 5000, 3), [])
            os.chmod(path, 0o000)
            self.assertEqual(semfs.search("privacy notes", os.getuid(), 3), [])
            semfs.database.close()
            semfs.ROOT, semfs.database, semfs.embed_text = old_root, old_database, old_embed_text

    def test_semantic_search_ranks_similar_content_and_limits_results_to_owner(self):
        with tempfile.TemporaryDirectory() as root:
            similar = Path(root) / "weather.md"
            unrelated = Path(root) / "sports.md"
            similar.write_text("Weather patterns and climate change", encoding="utf-8")
            unrelated.write_text("Football scores and match results", encoding="utf-8")
            os.chmod(similar, 0o600)
            os.chmod(unrelated, 0o600)
            old_root, old_database, old_embed_text = semfs.ROOT, semfs.database, semfs.embed_text
            semfs.ROOT = root
            semfs.database = new_semfs_database()
            vectors = {
                "global warming": [1.0, 0.0],
                "Weather patterns and climate change": [0.98, 0.02],
                "Football scores and match results": [0.0, 1.0],
            }
            semfs.embed_text = lambda text: vectors[text]
            try:
                semfs.index_file(str(similar))
                semfs.index_file(str(unrelated))
                semfs.database.execute(
                    "INSERT INTO files(path,inode,owner_uid,owner_gid,mode,mtime_ns,content,embedding,embedding_model) VALUES(?,?,?,?,?,?,?,?,?)",
                    (str(Path(root) / "other-user.md"), 1, os.getuid() + 5000, 1, 0o600, 1, "private", struct.pack("<ff", 1.0, 0.0), semfs.MODEL_NAME),
                )
                results = semfs.search("global warming", os.getuid(), 2)
                self.assertEqual([result["path"] for result in results], [str(similar), str(unrelated)])
                self.assertGreater(results[0]["score"], results[1]["score"])
                self.assertTrue(all(result["owner_uid"] == os.getuid() for result in results))
            finally:
                semfs.database.close()
                semfs.ROOT, semfs.database, semfs.embed_text = old_root, old_database, old_embed_text

    def test_embedding_model_loads_once_and_returns_numeric_vectors(self):
        class FakeTextEmbedding:
            def __init__(self, model_name, cache_dir):
                self.model_name = model_name
                self.cache_dir = cache_dir
            def embed(self, texts):
                return iter([[0.25, 0.75] for _ in texts])

        fake_fastembed = types.ModuleType("fastembed")
        fake_fastembed.TextEmbedding = FakeTextEmbedding
        old_model = semfs.embedding_model
        semfs.embedding_model = None
        try:
            with mock.patch.dict(sys.modules, {"fastembed": fake_fastembed}):
                self.assertEqual(semfs.embed_text("some text"), [0.25, 0.75])
                self.assertEqual(semfs.embedding_model.model_name, semfs.MODEL_NAME)
        finally:
            semfs.embedding_model = old_model

    def test_existing_text_index_schema_migrates_for_embeddings(self):
        with tempfile.TemporaryDirectory() as state:
            path = Path(state) / "index.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE files (path TEXT PRIMARY KEY, inode INTEGER NOT NULL, owner_uid INTEGER NOT NULL, owner_gid INTEGER NOT NULL, mode INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, content TEXT NOT NULL)")
            connection.close()
            old_path, old_database = semfs.DATABASE, semfs.database
            semfs.DATABASE = str(path)
            try:
                semfs.open_database()
                columns = {row[1] for row in semfs.database.execute("PRAGMA table_info(files)")}
                self.assertTrue({"content_sha256", "embedding", "embedding_model"}.issubset(columns))
            finally:
                semfs.database.close()
                semfs.DATABASE, semfs.database = old_path, old_database

    def test_speech_rejects_unsupported_audio_and_recording_limits(self):
        with self.assertRaises(ValueError):
            speech.record(0)
        with self.assertRaises(ValueError):
            speech.transcribe("missing.wav")
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "stereo.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\x00" * 8)
            with self.assertRaisesRegex(ValueError, "mono"):
                speech.transcribe(path)
        with self.assertRaises(ValueError):
            speech.speak("")

    def test_speech_transcribes_valid_wav_without_network_dependencies(self):
        class FakeRecognizer:
            def __init__(self, _model, sample_rate):
                self.sample_rate = sample_rate
            def AcceptWaveform(self, _chunk):
                return True
            def FinalResult(self):
                return json.dumps({"text": "hello from offline speech"})

        class FakeModel:
            def __init__(self, path):
                self.path = path

        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "speech.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(16000)
                audio.writeframes(b"\x00" * 800)
            fake_vosk = types.ModuleType("vosk")
            fake_vosk.KaldiRecognizer = FakeRecognizer
            fake_vosk.Model = FakeModel
            with mock.patch.dict(sys.modules, {"vosk": fake_vosk}):
                self.assertEqual(speech.transcribe(path), "hello from offline speech")

    def test_speech_synthesis_uses_local_engine_and_audio_output(self):
        with mock.patch.object(speech.subprocess, "run") as run:
            run.side_effect = [
                type("ProcessResult", (), {"stdout": b"local wav data"})(),
                type("ProcessResult", (), {"stdout": b""})(),
            ]
            speech.speak("hello")
            self.assertEqual(run.call_args_list[0].args[0], ["espeak-ng", "--stdout", "hello"])
            self.assertEqual(run.call_args_list[1].args[0], ["aplay", "-q"])
            self.assertEqual(run.call_args_list[1].kwargs["input"], b"local wav data")

    def test_execution_capability_signature_expiry_scope_and_replay(self):
        key = b"k" * 32
        nonce = "0123456789abcdef0123456789"
        claims = {"v": 1, "uid": 500, "exp": int(time.time()) + 60, "nonce": nonce, "roots": ["/tmp"], "argv_prefix": ["/usr/bin/printf"]}
        encoded = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=").decode()
        signature = base64.urlsafe_b64encode(hmac.new(key, encoded.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
        token = encoded + "." + signature
        with tempfile.TemporaryDirectory() as state:
            replay_path = str(Path(state) / "replay.sqlite3")
            execd.nonce_database = sqlite3.connect(replay_path, check_same_thread=False)
            execd.nonce_database.execute("CREATE TABLE used_tokens (nonce TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
            execd.nonce_database.commit()
            accepted = execd.decode_capability(token, key, 500)
            self.assertEqual(accepted["argv_prefix"], ["/usr/bin/printf"])
            execd.nonce_database.close()
            execd.nonce_database = sqlite3.connect(replay_path, check_same_thread=False)
            with self.assertRaises(ValueError):
                execd.decode_capability(token, key, 500)
            with self.assertRaises(ValueError):
                execd.decode_capability(token, key, 501)
            claims["exp"] = int(time.time()) - 1
            encoded_expired = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode()).rstrip(b"=").decode()
            signature_expired = base64.urlsafe_b64encode(hmac.new(key, encoded_expired.encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
            with self.assertRaises(ValueError):
                execd.decode_capability(encoded_expired + "." + signature_expired, key, 500)
            execd.nonce_database.close()
            execd.nonce_database = None

    def test_seccomp_filter_is_generated_or_fails_closed(self):
        if not ctypes.util.find_library("seccomp"):
            with self.assertRaises(RuntimeError):
                execd.build_seccomp_filter()
            return
        descriptor = execd.build_seccomp_filter()
        try:
            self.assertGreater(os.fstat(descriptor).st_size, 0)
        finally:
            os.close(descriptor)

    def test_execution_command_cannot_escape_scoped_directory(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            claims = {"roots": [root], "argv_prefix": ["/usr/bin/printf"]}
            with self.assertRaises(ValueError):
                execd.run_command(claims, {"argv": ["/usr/bin/printf", "no"], "cwd": outside})
            with self.assertRaises(ValueError):
                execd.run_command(claims, {"argv": ["/usr/bin/sh", "-c", "id"], "cwd": root})

if __name__ == "__main__":
    unittest.main(verbosity=2)
