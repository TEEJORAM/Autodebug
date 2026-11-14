"""The renderdoc tool: Autodebug runs RenderDoc's qrenderdoc with the replay script, with a time limit, and keeps what it
found (docs/design.md, "RenderDoc"). A fake qrenderdoc plays RenderDoc here; RealRenderDoc uses a real one when
AUTODEBUG_TEST_QRENDERDOC and AUTODEBUG_TEST_RDC name it and a capture."""

import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from autodebug.connection import DebugError
from autodebug.tools import Debugger

FAKE = textwrap.dedent("""
    import json, os, sys, time
    assert sys.argv[1] == "--python" and sys.argv[2].endswith("replay.py"), sys.argv
    out, op, mode = os.environ["AUTODEBUG_RD_OUT"], os.environ["AUTODEBUG_RD_OP"], os.environ.get("FAKE_RD_MODE", "ok")
    def write(data):
        open(out, "w").write(json.dumps(data))
    if mode == "silent":
        sys.exit(3)
    if mode == "error":
        write({"progress": [], "done": False, "error": "Traceback...\\nRuntimeError: RenderDoc could not open the capture"})
        sys.exit(1)
    if op == "summary":
        write({"progress": ["0.1 s replay opened"], "done": True, "api": "Vulkan", "action_count": 2,
               "actions": [{"event": 11, "name": "vkCmdDraw()", "depth": 0, "draw": True, "dispatch": False}],
               "messages": [{"event": 11, "severity": "High", "category": "Validation",
                             "text": "token ghp_0123456789abcdefghijklmnopqrstuvwxyz used"}]})
    elif op == "pixel":
        pixel = {"x": int(os.environ["AUTODEBUG_RD_X"] or 250), "y": 250,
                 "history": [{"event": 11, "passed": True, "before": [0.2], "after": [0.5]}]}
        write({"progress": ["0.4 s shader debug started"], "done": False, "api": "Vulkan", "event": 11, "pixel": pixel})
        if mode == "hang":
            time.sleep(60)
        write({"progress": [], "done": True, "api": "Vulkan", "event": 11, "pixel": pixel})
    sys.exit(0)
""")


class FakeRenderDoc(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "fake_qrenderdoc.py").write_text(FAKE, encoding="utf-8")
        if os.name == "nt":
            program = root / "qrenderdoc.cmd"
            program.write_text(f'@"{sys.executable}" "{root / "fake_qrenderdoc.py"}" %*\n', encoding="utf-8")
        else:
            program = root / "qrenderdoc"
            program.write_text(f"#!{sys.executable}\n" + FAKE, encoding="utf-8")
            program.chmod(0o755)
        self.capture = root / "frame.rdc"
        self.capture.write_bytes(b"RDOC")
        self.env = {"AUTODEBUG_QRENDERDOC": str(program), "DISPLAY": os.environ.get("DISPLAY", ":0")}
        self.debugger = Debugger(find_windows=list)

    def run_tool(self, mode="ok", **kwargs):
        old = {k: os.environ.get(k) for k in [*self.env, "FAKE_RD_MODE"]}
        os.environ.update({**self.env, "FAKE_RD_MODE": mode})
        try:
            return self.debugger.renderdoc(str(self.capture), **kwargs)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_summary_lists_actions_and_redacts_messages(self):
        result = self.run_tool()
        self.assertEqual((result["api"], result["action_count"]), ("Vulkan", 2))
        self.assertEqual(result["actions"][0]["name"], "vkCmdDraw()")
        self.assertNotIn("ghp_0123456789", json.dumps(result["messages"]))
        self.assertEqual(self.debugger.timeline[-1].kind, "renderdoc")

    def test_a_replay_that_does_not_finish_keeps_what_it_found(self):
        result = self.run_tool("hang", op="pixel", event=11, x=7, debug_shader=True, timeout_seconds=5)
        self.assertTrue(result["timed_out"])
        self.assertEqual(result["pixel"]["x"], 7)
        self.assertIn("shader debug started", result["note"])

    def test_failures_say_why(self):
        with self.assertRaises(DebugError) as caught:
            self.run_tool("error")
        self.assertIn("could not open the capture", caught.exception.message)
        with self.assertRaises(DebugError) as caught:
            self.run_tool("silent")
        self.assertIn("open RenderDoc once", caught.exception.message)
        with self.assertRaises(DebugError) as caught:
            self.run_tool(op="pixel")
        self.assertIn("needs event", caught.exception.message)

    def test_missing_renderdoc_and_capture(self):
        self.env["AUTODEBUG_QRENDERDOC"] = str(self.capture.with_name("missing.exe"))
        with self.assertRaises(DebugError) as caught:
            self.run_tool()
        self.assertEqual(caught.exception.code, "NOT_SUPPORTED")
        with self.assertRaises(DebugError) as caught:
            self.debugger.renderdoc(str(self.capture.with_suffix(".png")))
        self.assertEqual(caught.exception.code, "NOT_FOUND")


@unittest.skipUnless(os.environ.get("AUTODEBUG_TEST_QRENDERDOC") and os.environ.get("AUTODEBUG_TEST_RDC"),
                     "needs a real RenderDoc and capture (AUTODEBUG_TEST_QRENDERDOC, AUTODEBUG_TEST_RDC)")
class RealRenderDoc(unittest.TestCase):
    def test_the_real_replay_script(self):
        os.environ["AUTODEBUG_QRENDERDOC"] = os.environ["AUTODEBUG_TEST_QRENDERDOC"]
        self.addCleanup(os.environ.pop, "AUTODEBUG_QRENDERDOC", None)
        debugger = Debugger(find_windows=list)
        capture = os.environ["AUTODEBUG_TEST_RDC"]
        summary = debugger.renderdoc(capture)
        draws = [a for a in summary["actions"] if a["draw"]]
        self.assertTrue(draws, summary)
        event = debugger.renderdoc(capture, op="event", event=draws[0]["event"])
        self.assertIn("pixel", event["pipeline"]["shaders"])
        vertex = debugger.renderdoc(capture, op="vertex", event=draws[0]["event"], vertex=0)
        self.assertGreater(vertex["debug"]["steps"], 0)


if __name__ == "__main__":
    unittest.main()
