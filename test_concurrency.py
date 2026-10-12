"""Render-capacity tests for the Blender generation service.

A Blender import of a 30-150 MB GLB costs a multiple of the file size in RAM. Nothing
limited concurrent renders, so a few parallel requests could OOM the VM. These tests
pin the gating behaviour and prove slots are always released.

Run with:  python3 test_concurrency.py     (no pytest required)
"""

import os
import sys
import tempfile
import threading
import time

TMP = tempfile.mkdtemp()
BASE = os.path.join(TMP, "base")
os.makedirs(BASE, exist_ok=True)
os.environ.update({
    "BLENDER_SERVICE_TOKEN": "t" * 64,
    "BASE_MODELS_DIR": BASE,
    "GENERATED_DIR": os.path.join(TMP, "generated"),
    "TEMP_DIR": os.path.join(TMP, "tmp"),
    "OCI_OBJECT_STORAGE_ENABLED": "false",
    "OCI_CLEANUP_LOCAL_FILES": "false",
    "MAX_CONCURRENT_RENDERS": "1",
    "RENDER_WAIT_SECONDS": "0",          # fail fast instead of queueing
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server

# A local base model so resolution needs no object storage.
with open(os.path.join(BASE, "car.glb"), "wb") as f:
    f.write(b"glTF-stub")

RENDER_SECONDS = 1.5
GOOD = {"X-Blender-Token": "t" * 64}
fails = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "   <- " + str(detail)))
    if not cond:
        fails.append(name)


class FakeCompleted:
    returncode = 0
    stdout = ""
    stderr = ""


def fake_blender(cmd, **kwargs):
    """Stand in for `blender --background`: take real time, produce a real file."""
    import json as _json
    config_path = cmd[-1]
    with open(config_path) as fh:
        cfg = _json.load(fh)
    time.sleep(RENDER_SECONDS)
    with open(cfg["output_path"], "wb") as fh:
        fh.write(b"glTF-rendered")
    return FakeCompleted()


server.subprocess.run = fake_blender


def post(name):
    client = server.app.test_client()
    return client.post("/generate", headers=GOOD,
                       json={"base_model": "car.glb", "output_name": name, "materials": {}})


print("\n[1] A second concurrent render is rejected, not stacked")
results = {}


def worker(key, name):
    results[key] = post(name)


first = threading.Thread(target=worker, args=("first", "car_slot_one"))
first.start()
time.sleep(0.4)                                    # let the first claim the slot
second = post("car_slot_two")                      # should be refused immediately
first.join()

check("first render succeeds", results["first"].status_code == 200, results["first"].status_code)
check("concurrent render gets 503", second.status_code == 503, second.status_code)
check("503 carries Retry-After", second.headers.get("Retry-After") is not None,
      dict(second.headers))
check("503 body explains the cap",
      second.get_json().get("max_concurrent_renders") == 1, second.get_json())

print("\n[2] Slots are released, not leaked")
check("active renders back to zero", server._active_renders == 0, server._active_renders)
third = post("car_slot_three")
check("a later render succeeds", third.status_code == 200, third.status_code)
check("active renders still zero", server._active_renders == 0, server._active_renders)

print("\n[3] A failing render still releases its slot")
def failing_blender(cmd, **kwargs):
    raise RuntimeError("blender crashed")

server.subprocess.run = failing_blender
crashed = post("car_slot_crash")
check("crash reported as 500", crashed.status_code == 500, crashed.status_code)
check("slot released after a crash", server._active_renders == 0, server._active_renders)

server.subprocess.run = fake_blender
after_crash = post("car_slot_after")
check("service still accepts work after a crash", after_crash.status_code == 200,
      after_crash.status_code)

print("\n[4] Capacity is visible for monitoring")
details = server.app.test_client().get("/health/details", headers=GOOD).get_json()
check("health/details reports the cap", details.get("max_concurrent_renders") == 1, details)
check("health/details reports active renders", "active_renders" in details, details)

print("\n" + ("ALL CHECKS PASSED" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
