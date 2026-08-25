"""Security regression tests for the Blender generation service.

Covers the three findings that made this service remotely exploitable:
  1. /generate accepted anonymous requests
  2. base_model was fetched with urllib from ANY http(s) URL (SSRF -> OCI metadata)
  3. output_name was concatenated into a path (traversal -> arbitrary write as root)

Run with:  python3 test_security.py     (no pytest required)
"""

import os
import sys
import tempfile
import uuid

# Fixture values only - deliberately NOT the real tenancy namespace or bucket, so
# this file carries no infrastructure identifiers.
REGION = "us-example-1"
NAMESPACE = "examplenamespace"
BUCKET = "example-models"
HOST = f"objectstorage.{REGION}.oraclecloud.com"
BASE = f"https://{HOST}/n/{NAMESPACE}/b/{BUCKET}/o"

TMP = tempfile.mkdtemp()
os.environ.update({
    "BLENDER_SERVICE_TOKEN": "t" * 64,
    "BASE_MODELS_DIR": os.path.join(TMP, "base"),
    "GENERATED_DIR": os.path.join(TMP, "generated"),
    "TEMP_DIR": os.path.join(TMP, "tmp"),
    "OCI_REGION": REGION,
    "OCI_NAMESPACE": NAMESPACE,
    "OCI_BUCKET_NAME": BUCKET,
})
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server


GOOD = {"X-Blender-Token": "t" * 64}
c = server.app.test_client()
fails = []

def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "   <- " + str(detail)))
    if not cond:
        fails.append(name)

print("\n[1] Authentication (Critical #1)")
check("no token -> 401", c.post("/generate", json={}).status_code == 401)
check("wrong token -> 401", c.post("/generate", json={}, headers={"X-Blender-Token": "x"*64}).status_code == 401)
check("/models requires token", c.get("/models/car_abc.glb").status_code == 401)
check("/health/details requires token", c.get("/health/details").status_code == 401)
check("/health open for liveness", c.get("/health").status_code == 200)
check("/health leaks no config", "bucket" not in c.get("/health").get_json())
check("valid token passes auth (400 not 401)", c.post("/generate", json={}, headers=GOOD).status_code == 400)
check("no CORS wildcard header", "Access-Control-Allow-Origin" not in c.get("/health").headers)

print("\n[2] SSRF (Critical #2)")
for label, url in [
    ("IMDS metadata endpoint", "http://169.254.169.254/opc/v2/instance/"),
    ("internal http host", "http://10.0.0.5/model.glb"),
    ("attacker domain", "https://evil.example.com/model.glb"),
    ("right path, wrong host", f"https://evil.example.com/n/{NAMESPACE}/b/{BUCKET}/o/models%2Fa.glb"),
    ("right host, wrong bucket", f"https://{HOST}/n/{NAMESPACE}/b/other-bucket/o/models%2Fa.glb"),
    ("right host, wrong namespace", f"https://{HOST}/n/othernamespace/b/{BUCKET}/o/models%2Fa.glb"),
    ("http (not https) own host", f"http://{HOST}/n/{NAMESPACE}/b/{BUCKET}/o/models%2Fa.glb"),
]:
    try:
        server._resolve_base_model_to_local_file(url, "job1")
        check(label + " rejected", False, "ACCEPTED")
    except (ValueError, FileNotFoundError) as e:
        check(label + " rejected", True)
    except Exception as e:
        check(label + " rejected", False, type(e).__name__ + ": " + str(e))

check("raw-URL fetch helper removed", not hasattr(server, "_download_from_url"))
check("urllib.request not imported", "urllib.request" not in open(server.__file__).read())

ok_url = f"{BASE}/models%2Fhyundai-aura.glb"
check("legitimate bucket URL -> correct object key",
      server._extract_object_name_from_oci_url(ok_url) == "models/hyundai-aura.glb",
      server._extract_object_name_from_oci_url(ok_url))

print("\n[3] Object-key validation (bucket escape)")
for label, key in [
    ("parent traversal", "models/../../secret.glb"),
    ("absolute path", "/etc/passwd.glb"),
    ("outside allowed prefix", "private/keys.glb"),
    ("non-glb object", "models/config.json"),
]:
    try:
        server._validate_object_name(key)
        check(label + " rejected", False, "ACCEPTED")
    except ValueError:
        check(label + " rejected", True)
check("legitimate key accepted", server._validate_object_name("models/bmw-i4.glb") == "models/bmw-i4.glb")

print("\n[4] Path traversal in output_name (Critical #3)")
for label, name in [
    ("cron.d write", "../../../../etc/cron.d/x"),
    ("ssh key write", "../../root/.ssh/authorized_keys"),
    ("absolute path", "/tmp/pwned"),
    ("nested subdir", "a/b/c"),
    ("null byte", "car_1\x00.glb"),
    ("over-long name", "a" * 200),
    ("empty", ""),
]:
    try:
        server._resolve_output_path(name)
        check(label + " rejected", False, "ACCEPTED")
    except ValueError:
        check(label + " rejected", True)

mid, fname, path = server._resolve_output_path("car_9f2a4c1b")
check("legitimate name accepted", fname == "car_9f2a4c1b.glb" and mid == "car_9f2a4c1b", (mid, fname))
check("output stays inside OUTPUT_DIR", os.path.realpath(path).startswith(os.path.realpath(server.OUTPUT_DIR) + os.sep))
check("'.glb' suffix not doubled", server._resolve_output_path("car_x.glb")[1] == "car_x.glb")

print("\n[5] End-to-end payload from the audit report")
r = c.post("/generate", headers=GOOD, json={
    "base_model": "http://169.254.169.254/opc/v2/instance/",
    "output_name": "../../../../root/.ssh/authorized_keys",
    "materials": {},
})
check("full attack payload -> 400, no traversal, no fetch", r.status_code == 400, r.status_code)
check("no file written outside output dir", not os.path.exists("/root/.ssh/authorized_keys.glb"))

print("\n[6] Production input shapes must keep working")
name = "car_" + str(uuid.uuid4())          # CustomizationService sends "car_" + UUID
check("customization UUID name accepted", server._resolve_output_path(name)[1] == name + ".glb")

# car.getModelUrl(), built by ModelAssetProperties from APP_MODELS_STATIC_BASE_URL
url = f"{BASE}/models%2Fmahindra-thar.glb"
check("static model URL resolves to object key",
      server._extract_object_name_from_oci_url(url) == "models/mahindra-thar.glb")
check("versioned URL (?v=) resolves",
      server._extract_object_name_from_oci_url(url + "?v=3") == "models/mahindra-thar.glb")
check("generated-prefix URL resolves",
      server._validate_object_name(server._extract_object_name_from_oci_url(
          f"{BASE}/generated%2Fcar_abc.glb")) == "generated/car_abc.glb")

print("\n" + ("ALL CHECKS PASSED" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
