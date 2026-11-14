"""Runs inside RenderDoc (`qrenderdoc --python replay.py`), on RenderDoc's own embedded Python (3.8 in RenderDoc
1.47), never in Autodebug's process: the `renderdoc` module only loads in the Python it was built for. It replays a
capture (it runs none of the captured program) and writes JSON to AUTODEBUG_RD_OUT after every step, so a step that
never returns still leaves what was found before it. Inputs come from the environment (qrenderdoc passes no argv):

AUTODEBUG_RD_CAPTURE  the .rdc file             AUTODEBUG_RD_OP     summary | event | pixel | vertex
AUTODEBUG_RD_EVENT    event id (event, pixel, vertex)               AUTODEBUG_RD_X, _Y   pixel (default: centre)
AUTODEBUG_RD_VERTEX   vertex index (vertex)     AUTODEBUG_RD_DEBUG  1: debug the shader (pixel)
AUTODEBUG_RD_FILTER   text the action names must contain (summary)  AUTODEBUG_RD_LIMIT  most actions listed

Exits with sys.exit, which makes qrenderdoc quit without opening its window (qrenderdoc.cpp: SystemExit sets
pythonExited).
"""

import json
import os
import sys
import time
import traceback

import renderdoc as rd

OUT = os.environ["AUTODEBUG_RD_OUT"]
STARTED = time.time()
result = {"progress": [], "done": False}


def save():
    with open(OUT + ".tmp", "w") as handle:
        json.dump(result, handle)
    os.replace(OUT + ".tmp", OUT)


def mark(text):
    result["progress"].append(f"{time.time() - STARTED:.1f} s {text}")
    save()


def number(name, default=None):
    value = os.environ.get(name, "")
    return int(value) if value.strip() else default


def actions(controller, limit, wanted):
    names = controller.GetStructuredFile()
    found = []
    count = 0

    def walk(action, depth):
        nonlocal count
        count += 1
        name = action.GetName(names)
        if len(found) < limit and (not wanted or wanted.lower() in name.lower()):
            found.append({"event": action.eventId, "name": name[:200], "depth": depth,
                          "draw": bool(action.flags & rd.ActionFlags.Drawcall),
                          "dispatch": bool(action.flags & rd.ActionFlags.Dispatch)})
        for child in action.children:
            walk(child, depth + 1)

    for root in controller.GetRootActions():
        walk(root, 0)
    return found, count


def messages(controller):
    return [{"event": m.eventId, "severity": str(m.severity).split(".")[-1], "category": str(m.category).split(".")[-1],
             "text": m.description[:500]} for m in controller.GetDebugMessages()][:50]


def pipeline(controller):
    state = controller.GetPipelineState()
    shaders = {}
    for stage in (rd.ShaderStage.Vertex, rd.ShaderStage.Hull, rd.ShaderStage.Domain, rd.ShaderStage.Geometry,
                  rd.ShaderStage.Pixel, rd.ShaderStage.Compute):
        reflection = state.GetShaderReflection(stage)
        if reflection is not None:
            shaders[str(stage).split(".")[-1].lower()] = {
                "entry": reflection.entryPoint,
                "resource": str(state.GetShader(stage)),
                "inputs": [s.varName for s in reflection.inputSignature][:20],
                "outputs": [s.varName for s in reflection.outputSignature][:20],
            }
    viewport = state.GetViewport(0)
    return {"shaders": shaders, "targets": [str(t.resource) for t in state.GetOutputTargets() if t.resource != rd.ResourceId.Null()],
            "depth_target": str(state.GetDepthTarget().resource),
            "viewport": [viewport.x, viewport.y, viewport.width, viewport.height]}, viewport


def changes(state):
    out = []
    for change in state.changes:
        value = change.after
        if not value.name:
            continue
        floats = [round(v, 6) for v in value.value.f32v[:max(1, value.columns or 1) * max(1, value.rows or 1)][:16]]
        out.append({"name": value.name, "value": floats})
    return out


def debug(controller, trace, limit=2000):
    """Step a shader debug trace to its end (at most `limit` steps), saving progress after each batch."""
    if trace is None or trace.debugger is None:
        return {"available": False}
    steps = []
    while len(steps) < limit:
        batch = controller.ContinueDebug(trace.debugger)
        if len(batch) == 0:
            break
        for state in batch:
            steps.append({"step": state.stepIndex, "changes": changes(state)})
        result["debug"] = {"available": True, "steps": len(steps), "last": steps[-12:]}
        mark(f"shader debug: {len(steps)} steps")
    controller.FreeTrace(trace)
    return {"available": True, "steps": len(steps), "first": steps[:12], "last": steps[-12:],
            "complete": len(steps) < limit}


def main():
    capture = rd.OpenCaptureFile()
    status = capture.OpenFile(os.environ["AUTODEBUG_RD_CAPTURE"], "", None)
    if status != rd.ResultCode.Succeeded:
        raise RuntimeError(f"RenderDoc could not open the capture: {status}")
    if not capture.LocalReplaySupport():
        raise RuntimeError("this capture cannot be replayed on this computer (another GPU vendor or API)")
    status, controller = capture.OpenCapture(rd.ReplayOptions(), None)
    if status != rd.ResultCode.Succeeded:
        raise RuntimeError(f"RenderDoc could not replay the capture: {status}")
    mark("replay opened")
    op = os.environ.get("AUTODEBUG_RD_OP", "summary")
    properties = controller.GetAPIProperties()
    result["api"] = str(properties.pipelineType).split(".")[-1]
    if op == "summary":
        found, count = actions(controller, number("AUTODEBUG_RD_LIMIT", 200), os.environ.get("AUTODEBUG_RD_FILTER", ""))
        result.update({"actions": found, "action_count": count, "messages": messages(controller)})
    else:
        event = number("AUTODEBUG_RD_EVENT")
        controller.SetFrameEvent(event, True)
        state, viewport = pipeline(controller)
        result["event"] = event
        result["pipeline"] = state
        mark("pipeline read")
        if op == "pixel":
            x = number("AUTODEBUG_RD_X", int(viewport.x + viewport.width // 2))
            y = number("AUTODEBUG_RD_Y", int(viewport.y + viewport.height // 2))
            target = controller.GetPipelineState().GetOutputTargets()[0].resource
            history = controller.PixelHistory(target, x, y, rd.Subresource(), rd.CompType.Typeless)
            result["pixel"] = {"x": x, "y": y, "history": [
                {"event": h.eventId, "passed": h.Passed(), "before": [round(v, 6) for v in h.preMod.col.floatValue],
                 "after": [round(v, 6) for v in h.postMod.col.floatValue]} for h in history][:50]}
            mark("pixel history read")
            if os.environ.get("AUTODEBUG_RD_DEBUG") == "1":
                inputs = rd.DebugPixelInputs()
                inputs.sample = 0
                inputs.primitive = rd.ReplayController.NoPreference
                result["debug"] = {"available": True, "steps": 0}
                mark("shader debug started")
                result["debug"] = debug(controller, controller.DebugPixel(x, y, inputs))
        elif op == "vertex":
            vertex = number("AUTODEBUG_RD_VERTEX", 0)
            result["vertex"] = vertex
            mark("shader debug started")
            result["debug"] = debug(controller, controller.DebugVertex(vertex, 0, vertex, 0))
    controller.Shutdown()
    capture.Shutdown()


try:
    main()
    result["done"] = True
    save()
    sys.exit(0)
except SystemExit:
    raise
except Exception:
    result["error"] = traceback.format_exc()[-2000:]
    save()
    sys.exit(1)
