"""GPU frames with RenderDoc (docs/design.md, "RenderDoc").

Autodebug never loads RenderDoc's Python module itself: it only loads in the Python it was built for (RenderDoc's
documentation), so the replay runs inside RenderDoc as `qrenderdoc --python renderdoc_scripts/replay.py`, in RenderDoc's
own embedded Python, in a separate process with a time limit. That script replays a capture (.rdc); it runs none of the
captured program. Results come back as JSON in a file, written after every step.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from autodebug.connection import DebugError
from autodebug.redact import clean_text

SCRIPT = Path(__file__).resolve().parent / "renderdoc_scripts" / "replay.py"
OPERATIONS = ("summary", "event", "pixel", "vertex")


def find_qrenderdoc() -> str | None:
    """RenderDoc's UI program, which runs the replay script: AUTODEBUG_QRENDERDOC, then PATH, then the installer's
    folder on Windows."""
    configured = os.environ.get("AUTODEBUG_QRENDERDOC")
    if configured:
        return configured if Path(configured).is_file() else None
    found = shutil.which("qrenderdoc")
    if found:
        return found
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
            candidate = Path(base or "C:/Program Files") / "RenderDoc" / "qrenderdoc.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def _inputs(capture: Path, op: str, event, x, y, vertex, debug_shader: bool, name_filter, limit: int, out: Path) -> dict:
    values = {"CAPTURE": str(capture), "OP": op, "OUT": str(out), "DEBUG": "1" if debug_shader else "",
              "FILTER": name_filter or "", "LIMIT": str(limit)}
    for key, value in (("EVENT", event), ("X", x), ("Y", y), ("VERTEX", vertex)):
        values[key] = "" if value is None else str(int(value))
    return {f"AUTODEBUG_RD_{k}": v for k, v in values.items()}


def analyse(capture: str, op: str = "summary", event: int | None = None, x: int | None = None, y: int | None = None,
            vertex: int | None = None, debug_shader: bool = False, name_filter: str | None = None, limit: int = 100,
            timeout_seconds: float = 120) -> dict:
    """Replay `capture` in RenderDoc and return what `op` reads. Raises DebugError with the reason and the fix."""
    if op not in OPERATIONS:
        raise DebugError("BAD_REQUEST", f"op is one of {', '.join(OPERATIONS)}")
    if op != "summary" and event is None:
        raise DebugError("BAD_REQUEST", f"op {op} needs event: an event id from op summary")
    path = Path(capture)
    if not path.is_absolute() or path.suffix.lower() != ".rdc" or not path.is_file():
        raise DebugError("NOT_FOUND", f"{capture} is not an existing RenderDoc capture (an absolute path to a .rdc file)")
    program = find_qrenderdoc()
    if program is None:
        raise DebugError("NOT_SUPPORTED", "RenderDoc is not installed (no qrenderdoc on PATH or in Program Files). "
                                          "Install it from renderdoc.org, or set AUTODEBUG_QRENDERDOC to qrenderdoc's path.")
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        raise DebugError("NOT_SUPPORTED", "RenderDoc's qrenderdoc needs an X display (DISPLAY is not set), even though "
                                          "its window never opens. Run Autodebug in a desktop session, or start Xvfb "
                                          "and set DISPLAY.")
    with tempfile.TemporaryDirectory(prefix="autodebug-rd-") as folder:
        out = Path(folder) / "result.json"
        env = {**os.environ, **_inputs(path, op, event, x, y, vertex, debug_shader, name_filter,
                                       max(1, min(int(limit), 1000)), out)}
        began = time.monotonic()
        process = subprocess.Popen([program, "--python", str(SCRIPT)], env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            process.wait(timeout=max(5.0, min(float(timeout_seconds), 600.0)))
            timed_out = False
        except subprocess.TimeoutExpired:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(10)
            timed_out = True
        seconds = round(time.monotonic() - began, 1)
        try:
            found = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            found = None
    if found is None:
        hint = ("If RenderDoc has never been opened on this computer, it first asks a question in a window that "
                "cannot show here: ask the developer to open RenderDoc once and answer it.")
        if timed_out:
            raise DebugError("TIMEOUT", f"RenderDoc did not start the replay within {seconds} s. {hint}")
        raise DebugError("IDE_ERROR", f"RenderDoc ended without a result (exit code {process.returncode}). {hint}")
    return _shaped(found, op, timed_out, seconds)


def _shaped(found: dict, op: str, timed_out: bool, seconds: float) -> dict:
    result: dict = {"op": op, "seconds": seconds}
    for key in ("api", "event", "pipeline", "actions", "action_count", "pixel", "vertex", "debug"):
        if key in found:
            result[key] = found[key]
    if "messages" in found:
        result["messages"] = [{**m, "text": clean_text(m.get("text", ""), 500)} for m in found["messages"]]
    if found.get("error"):
        last = found["error"].strip().splitlines()[-1]
        raise DebugError("IDE_ERROR", f"RenderDoc failed: {clean_text(last, 500)}")
    if timed_out:
        result["timed_out"] = True
        result["note"] = ("RenderDoc did not finish in time; this is what it found before that (last step: "
                          f"{(found.get('progress') or ['none'])[-1]}). Shader debugging of a pixel can take long, or "
                          "not return on some drivers: try op vertex, another pixel, or a longer timeout_seconds.")
    elif not found.get("done"):
        result["note"] = "RenderDoc stopped before the end; this is what it found."
    return result
