#!/usr/bin/env python3

from flask import Flask, request, jsonify, send_file
import functools
import hmac
import re
import subprocess
import threading
import json
import os
import uuid
import time
import shutil
import tempfile
import urllib.parse
from contextlib import suppress

try:
    import oci
except ImportError:
    oci = None

app = Flask(__name__)

BLENDER_PATH = shutil.which("blender") or "blender"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_MODELS_DIR = os.environ.get(
    "BASE_MODELS_DIR",
    os.path.abspath(os.path.join(BASE_DIR, "src", "main", "resources", "static", "models"))
)
GENERATED_DIR = os.environ.get("GENERATED_DIR", os.path.join(BASE_DIR, "generated"))
OUTPUT_DIR = GENERATED_DIR
TEMP_DIR = os.environ.get("TEMP_DIR", os.path.join(tempfile.gettempdir(), "arcs-blender"))
GENERATE_TIMEOUT_SECONDS = int(os.environ.get("GENERATE_TIMEOUT_SECONDS", "120"))

# Shared secret required on every non-health request. The service used to trust the
# network completely, which is only safe if the network is actually private - it was
# not. Fail closed at startup rather than silently accepting anonymous render jobs.
BLENDER_SERVICE_TOKEN = os.environ.get("BLENDER_SERVICE_TOKEN", "").strip()
MIN_TOKEN_LENGTH = 32

# One Blender import of a 30-150 MB GLB costs a multiple of the file size in RAM, and
# nothing here used to limit how many ran at once - a handful of concurrent requests
# was enough for the kernel to start OOM-killing processes. Cap the renders and make
# the overflow explicit instead of letting the VM fall over.
MAX_CONCURRENT_RENDERS = max(1, int(os.environ.get("MAX_CONCURRENT_RENDERS", "1")))
RENDER_WAIT_SECONDS = max(0, int(os.environ.get("RENDER_WAIT_SECONDS", "30")))
SERVER_THREADS = max(2, int(os.environ.get("SERVER_THREADS", "8")))

_render_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RENDERS)
_render_slots_held = threading.Lock()
_active_renders = 0


def _begin_render():
    """Claim a render slot, or return False when the service is already saturated."""
    global _active_renders
    acquired = _render_slots.acquire(timeout=RENDER_WAIT_SECONDS) if RENDER_WAIT_SECONDS \
        else _render_slots.acquire(blocking=False)
    if not acquired:
        return False
    with _render_slots_held:
        _active_renders += 1
    return True


def _end_render():
    global _active_renders
    with _render_slots_held:
        _active_renders -= 1
    _render_slots.release()

CAR_MODEL_NAME = os.environ.get("CAR_MODEL_NAME", "car.glb")
CAR_MODEL_PATH = os.path.join(BASE_MODELS_DIR, CAR_MODEL_NAME)

OCI_OBJECT_STORAGE_ENABLED = os.environ.get("OCI_OBJECT_STORAGE_ENABLED", "true").lower() in {"1", "true", "yes"}
OCI_REGION = os.environ.get("OCI_REGION", "ap-hyderabad-1")
OCI_AUTH_MODE = os.environ.get("OCI_AUTH_MODE", "instance_principal").strip().lower()
OCI_NAMESPACE = os.environ.get("OCI_NAMESPACE", "").strip()
OCI_BUCKET_NAME = os.environ.get("OCI_BUCKET_NAME", "ar-models").strip()
OCI_MODELS_PREFIX = os.environ.get("OCI_MODELS_PREFIX", "models/").strip()
OCI_GENERATED_PREFIX = os.environ.get("OCI_GENERATED_PREFIX", "generated/").strip()
OCI_PUBLIC_BASE_URL = os.environ.get("OCI_PUBLIC_BASE_URL", "").strip()
OCI_CLEANUP_LOCAL_FILES = os.environ.get("OCI_CLEANUP_LOCAL_FILES", "true").lower() in {"1", "true", "yes"}

_oci_client = None
_oci_namespace_cache = None

if len(BLENDER_SERVICE_TOKEN) < MIN_TOKEN_LENGTH:
    raise SystemExit(
        "BLENDER_SERVICE_TOKEN is missing or shorter than {} characters.\n"
        "This service performs unauthenticated-by-default work (spawning Blender, reading\n"
        "object storage) and must not run without a shared secret.\n"
        "Generate one with:  openssl rand -hex 32\n"
        "then set it identically here and on the Spring backend.".format(MIN_TOKEN_LENGTH)
    )

# Object names and output names are attacker-influenced. Keep them to a strict charset
# so nothing can escape a directory or a bucket prefix.
SAFE_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SAFE_OBJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/._-]{0,255}$")

os.makedirs(BASE_MODELS_DIR, exist_ok=True)
os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)


def _normalize_prefix(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if not value.endswith("/"):
        return value + "/"
    return value


OCI_MODELS_PREFIX = _normalize_prefix(OCI_MODELS_PREFIX)
OCI_GENERATED_PREFIX = _normalize_prefix(OCI_GENERATED_PREFIX)


def _is_http_url(value: str) -> bool:
    if not value:
        return False
    lowered = value.lower()
    return lowered.startswith("http://") or lowered.startswith("https://")


def _get_oci_client():
    global _oci_client
    if _oci_client is not None:
        return _oci_client
    if oci is None:
        raise RuntimeError("OCI SDK is not installed in blender-service container.")

    if OCI_AUTH_MODE == "config":
        profile = os.environ.get("OCI_CONFIG_PROFILE", "DEFAULT")
        config_file = os.environ.get("OCI_CONFIG_FILE", os.path.expanduser("~/.oci/config"))
        config = oci.config.from_file(file_location=config_file, profile_name=profile)
        _oci_client = oci.object_storage.ObjectStorageClient(config)
    else:
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        _oci_client = oci.object_storage.ObjectStorageClient(config={"region": OCI_REGION}, signer=signer)

    return _oci_client


def _get_oci_namespace(client):
    global _oci_namespace_cache
    if OCI_NAMESPACE:
        return OCI_NAMESPACE
    if _oci_namespace_cache:
        return _oci_namespace_cache
    _oci_namespace_cache = client.get_namespace().data
    return _oci_namespace_cache


def _get_public_base_url(namespace_name: str) -> str:
    if OCI_PUBLIC_BASE_URL:
        return OCI_PUBLIC_BASE_URL.rstrip("/")
    return f"https://objectstorage.{OCI_REGION}.oraclecloud.com/n/{namespace_name}/b/{OCI_BUCKET_NAME}/o"


def _build_oci_object_url(object_name: str, namespace_name: str) -> str:
    encoded_object_name = urllib.parse.quote(object_name, safe="")
    return f"{_get_public_base_url(namespace_name)}/{encoded_object_name}"


def _allowed_object_hosts() -> set:
    hosts = {"objectstorage.{}.oraclecloud.com".format(OCI_REGION)}
    if OCI_PUBLIC_BASE_URL:
        custom = urllib.parse.urlparse(OCI_PUBLIC_BASE_URL).netloc
        if custom:
            hosts.add(custom)
    return hosts


def _validate_object_name(object_name: str) -> str:
    """Reject anything that is not a plain .glb key under a configured prefix.

    This is the SSRF/traversal chokepoint: every path that ends in an object read
    goes through here, so a caller cannot walk out of the bucket prefix or point us
    at an arbitrary key.
    """
    candidate = (object_name or "").strip().lstrip("/")

    if not candidate or not SAFE_OBJECT_NAME.match(candidate):
        raise ValueError("Base model reference contains illegal characters.")

    if ".." in candidate.split("/") or "\\" in candidate:
        raise ValueError("Base model reference must not traverse directories.")

    if not candidate.lower().endswith(".glb"):
        raise ValueError("Base model must be a .glb object.")

    allowed_prefixes = tuple(p for p in (OCI_MODELS_PREFIX, OCI_GENERATED_PREFIX) if p)
    if allowed_prefixes and not candidate.startswith(allowed_prefixes):
        raise ValueError("Base model must live under an allowed bucket prefix.")

    return candidate


def _extract_object_name_from_oci_url(url: str):
    """Map a trusted object-storage URL back to its object key.

    Returns None when the URL is not one of ours - callers must then reject the
    request rather than falling back to fetching it directly.
    """
    if not _is_http_url(url):
        return None

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https":
        return None
    if parsed.netloc not in _allowed_object_hosts():
        return None

    decoded_path = urllib.parse.unquote(parsed.path)
    marker = "/o/"
    if marker not in decoded_path:
        return None

    prefix, object_name = decoded_path.split(marker, 1)

    # When the canonical objectstorage host is used the path carries /n/<ns>/b/<bucket>;
    # make sure the request is not pointed at somebody else's bucket.
    if "/b/" in prefix:
        bucket_segment = prefix.split("/b/", 1)[1].split("/", 1)[0]
        if bucket_segment != OCI_BUCKET_NAME:
            return None
    if "/n/" in prefix and OCI_NAMESPACE:
        namespace_segment = prefix.split("/n/", 1)[1].split("/", 1)[0]
        if namespace_segment != OCI_NAMESPACE:
            return None

    return object_name


def _download_from_oci_object(object_name: str, output_path: str):
    client = _get_oci_client()
    namespace_name = _get_oci_namespace(client)
    response = client.get_object(namespace_name, OCI_BUCKET_NAME, object_name)
    with open(output_path, "wb") as local_file:
        shutil.copyfileobj(response.data.raw, local_file)


def _resolve_base_model_to_local_file(base_model_input: str, model_id: str) -> str:
    """Resolve a caller-supplied base model reference to a local file.

    Only two forms are accepted, and both end in an authenticated OCI SDK read of a
    validated object key:

      1. a bare object key or filename (``hyundai-aura.glb`` / ``models/hyundai-aura.glb``)
      2. an https URL on our own object-storage host for our own bucket

    Arbitrary URLs are NOT fetched. The previous implementation passed any http(s)
    string to urllib, which let a caller reach internal addresses - including the
    instance metadata endpoint that issues this VM's OCI credentials.
    """
    model_candidate = (base_model_input or "").strip()
    if not model_candidate:
        model_candidate = CAR_MODEL_NAME

    if _is_http_url(model_candidate):
        object_name = _extract_object_name_from_oci_url(model_candidate)
        if not object_name:
            raise ValueError(
                "Base model URL is not an allowed object-storage location."
            )
        object_name = _validate_object_name(object_name)
        temp_base_model_path = os.path.join(TEMP_DIR, "base_{}.glb".format(model_id))
        _download_from_oci_object(object_name, temp_base_model_path)
        return temp_base_model_path

    # Non-URL input: treat as a bucket key, but serve from the local mirror when present.
    local_filename = os.path.basename(model_candidate)
    if not SAFE_OBJECT_NAME.match(local_filename) or not local_filename.lower().endswith(".glb"):
        raise ValueError("Base model reference is not a valid .glb name.")

    local_path = os.path.realpath(os.path.join(BASE_MODELS_DIR, local_filename))
    if local_path.startswith(os.path.realpath(BASE_MODELS_DIR) + os.sep) and os.path.exists(local_path):
        return local_path

    if not OCI_OBJECT_STORAGE_ENABLED:
        raise FileNotFoundError("Base model not found: {}".format(local_filename))

    object_name = model_candidate.lstrip("/")
    if "/" not in object_name:
        object_name = "{}{}".format(OCI_MODELS_PREFIX, object_name)
    object_name = _validate_object_name(object_name)

    temp_base_model_path = os.path.join(TEMP_DIR, "base_{}.glb".format(model_id))
    _download_from_oci_object(object_name, temp_base_model_path)
    return temp_base_model_path


def _resolve_output_path(output_name: str):
    """Turn a caller-supplied output name into a safe filename inside OUTPUT_DIR.

    ``output_name`` used to be concatenated straight into a path, so a value like
    ``../../etc/cron.d/x`` wrote outside the output directory - as root.
    """
    raw = (output_name or "").strip()
    if raw.lower().endswith(".glb"):
        raw = raw[:-4]

    # Validate the value as supplied. Deliberately NOT basename() first: silently
    # rewriting "../../etc/cron.d/x" into "x" would hide the attack and let two
    # different requested names collide onto one output file.
    if not SAFE_OUTPUT_NAME.match(raw) or raw != os.path.basename(raw):
        raise ValueError(
            "output_name must be 1-64 characters of letters, digits, underscore or hyphen."
        )

    output_filename = raw + ".glb"
    output_path = os.path.realpath(os.path.join(OUTPUT_DIR, output_filename))

    # Belt and braces: the regex already forbids separators, but assert containment.
    if not output_path.startswith(os.path.realpath(OUTPUT_DIR) + os.sep):
        raise ValueError("Resolved output path escapes the output directory.")

    return raw, output_filename, output_path


def require_service_token(view):
    """Reject any request that does not present the shared secret."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        presented = request.headers.get("X-Blender-Token", "")
        if not hmac.compare_digest(presented, BLENDER_SERVICE_TOKEN):
            return jsonify({"error": "Unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapper


def _upload_generated_model(local_output_path: str, output_filename: str):
    if not OCI_OBJECT_STORAGE_ENABLED:
        return output_filename, f"/models/{output_filename}"

    client = _get_oci_client()
    namespace_name = _get_oci_namespace(client)
    object_name = f"{OCI_GENERATED_PREFIX}{output_filename}"
    with open(local_output_path, "rb") as model_stream:
        client.put_object(
            namespace_name=namespace_name,
            bucket_name=OCI_BUCKET_NAME,
            object_name=object_name,
            put_object_body=model_stream,
            content_type="model/gltf-binary",
        )
    object_url = _build_oci_object_url(object_name, namespace_name)
    return object_url, object_url


@app.route("/health", methods=["GET"])
def health():
    """Unauthenticated liveness probe.

    Deliberately minimal: it neither shells out to Blender (which would let anyone
    spawn processes) nor echoes bucket, namespace or path configuration.
    """
    return jsonify({"status": "ok", "service": "blender-generation"})


@app.route("/health/details", methods=["GET"])
@require_service_token
def health_details():
    try:
        result = subprocess.run(
            [BLENDER_PATH, "--version"], capture_output=True, text=True, timeout=15
        )
        blender_version = result.stdout.split("\n")[0]
    except Exception as e:
        blender_version = f"Error: {e}"

    oci_status = "disabled"
    if OCI_OBJECT_STORAGE_ENABLED:
        oci_status = "enabled" if oci is not None else "missing-sdk"

    return jsonify({
        "status": "ok",
        "blender_path": BLENDER_PATH,
        "blender_version": blender_version,
        "oci_object_storage": oci_status,
        "oci_region": OCI_REGION,
        "oci_bucket": OCI_BUCKET_NAME,
        "oci_models_prefix": OCI_MODELS_PREFIX,
        "oci_generated_prefix": OCI_GENERATED_PREFIX,
        "output_dir": OUTPUT_DIR,
        "temp_dir": TEMP_DIR,
        "max_concurrent_renders": MAX_CONCURRENT_RENDERS,
        "active_renders": _active_renders,
        "render_wait_seconds": RENDER_WAIT_SECONDS
    })


@app.route("/generate", methods=["POST"])
@require_service_token
def generate():
    temp_files = []
    output_path = None
    config_path = None

    config = request.json
    if not config:
        return jsonify({"error": "No config provided"}), 400

    if not _begin_render():
        print(f"WARN: render capacity reached ({MAX_CONCURRENT_RENDERS} in flight); rejecting request")
        response = jsonify({
            "error": "Generation capacity reached. Please retry shortly.",
            "max_concurrent_renders": MAX_CONCURRENT_RENDERS,
        })
        response.headers["Retry-After"] = str(max(5, RENDER_WAIT_SECONDS))
        return response, 503

    try:

        requested_name = config.get("output_name") or f"car_{str(uuid.uuid4())[:8]}"
        try:
            model_id, output_filename, output_path = _resolve_output_path(requested_name)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        base_model_input = config.get("base_model", CAR_MODEL_NAME)
        try:
            base_model_path = _resolve_base_model_to_local_file(base_model_input, model_id)
        except (ValueError, FileNotFoundError) as e:
            return jsonify({"error": str(e)}), 400
        if base_model_path.startswith(TEMP_DIR):
            temp_files.append(base_model_path)

        blender_config = {
            "base_model": base_model_path,
            "output_path": output_path,
            "materials": config.get("materials", {})
        }

        config_path = os.path.join(TEMP_DIR, f"config_{model_id}.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(blender_config, f)

        script_path = os.path.join(BASE_DIR, "generate.py")
        cmd = [BLENDER_PATH, "--background", "--python", script_path, "--", config_path]

        print(f"INFO: Generating model {model_id}")
        start = time.time()

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=GENERATE_TIMEOUT_SECONDS
        )

        elapsed = time.time() - start

        if result.returncode != 0:
            print(f"ERROR: Blender exited {result.returncode} for {model_id}")
            print(result.stdout)
            print(result.stderr)
            return jsonify({"error": "Blender failed", "model_id": model_id}), 500

        for _ in range(30):
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                break
            time.sleep(0.1)

        if not os.path.exists(output_path):
            print(f"ERROR: no output produced for {model_id}\n{result.stdout}")
            return jsonify({"error": "Output file not created", "model_id": model_id}), 500

        if result.stdout:
            print(f"INFO: Blender stdout for {model_id}:\n{result.stdout}")
        if result.stderr:
            print(f"WARN: Blender stderr for {model_id}:\n{result.stderr}")

        print("SUCCESS: Output file generated:", output_path)

        model_url, download_url = _upload_generated_model(output_path, output_filename)

        return jsonify({
            "success": True,
            "model_id": model_id,
            "filename": output_filename,
            "model_url": model_url,
            "download_url": download_url,
            "generation_time": round(elapsed, 2)
        })

    except Exception as e:
        print(f"ERROR: generation failed: {e}")
        return jsonify({"error": "Generation failed"}), 500

    finally:
        if config_path:
            with suppress(FileNotFoundError):
                os.remove(config_path)

        for temp_file in temp_files:
            with suppress(FileNotFoundError):
                os.remove(temp_file)

        if OCI_CLEANUP_LOCAL_FILES and output_path:
            with suppress(FileNotFoundError):
                os.remove(output_path)

        _end_render()


@app.route("/models/<filename>", methods=["GET"])
@require_service_token
def get_model(filename):
    try:
        _, _, filepath = _resolve_output_path(filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    if not os.path.exists(filepath):
        return jsonify({"error": "Model not found"}), 404

    return send_file(filepath, mimetype="model/gltf-binary")


if __name__ == "__main__":
    print("INFO: Blender Service running on http://0.0.0.0:5000")
    print("INFO: Blender path:", BLENDER_PATH)
    print("INFO: Output directory:", OUTPUT_DIR)
    print("INFO: OCI object storage enabled:", OCI_OBJECT_STORAGE_ENABLED)
    print(f"INFO: Max concurrent renders: {MAX_CONCURRENT_RENDERS}")

    # Flask's development server is not meant to face real traffic. waitress is a
    # pure-python WSGI server, so it needs no build tooling in the slim image.
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000, threads=SERVER_THREADS)
    except ImportError:
        print("WARN: waitress is not installed; falling back to the Flask dev server.")
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
