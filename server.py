#!/usr/bin/env python3

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import subprocess
import json
import os
import uuid
import time
import shutil
import tempfile
import urllib.parse
import urllib.request
from contextlib import suppress

try:
    import oci
except ImportError:
    oci = None

app = Flask(__name__)
CORS(app)

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
HTTP_DOWNLOAD_TIMEOUT_SECONDS = int(os.environ.get("HTTP_DOWNLOAD_TIMEOUT_SECONDS", "60"))

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


def _extract_object_name_from_oci_url(url: str):
    if not _is_http_url(url):
        return None
    parsed = urllib.parse.urlparse(url)
    decoded_path = urllib.parse.unquote(parsed.path)
    marker = "/o/"
    if marker not in decoded_path:
        return None
    return decoded_path.split(marker, 1)[1]


def _download_from_url(source_url: str, output_path: str):
    with urllib.request.urlopen(source_url, timeout=HTTP_DOWNLOAD_TIMEOUT_SECONDS) as response, open(output_path, "wb") as local_file:
        shutil.copyfileobj(response, local_file)


def _download_from_oci_object(object_name: str, output_path: str):
    client = _get_oci_client()
    namespace_name = _get_oci_namespace(client)
    response = client.get_object(namespace_name, OCI_BUCKET_NAME, object_name)
    with open(output_path, "wb") as local_file:
        shutil.copyfileobj(response.data.raw, local_file)


def _resolve_base_model_to_local_file(base_model_input: str, model_id: str) -> str:
    model_candidate = (base_model_input or "").strip()
    if not model_candidate:
        model_candidate = CAR_MODEL_NAME

    temp_base_model_path = os.path.join(TEMP_DIR, f"base_{model_id}.glb")

    if _is_http_url(model_candidate):
        object_name = _extract_object_name_from_oci_url(model_candidate)
        if OCI_OBJECT_STORAGE_ENABLED and object_name:
            _download_from_oci_object(object_name, temp_base_model_path)
        else:
            _download_from_url(model_candidate, temp_base_model_path)
        return temp_base_model_path

    if os.path.isabs(model_candidate) and os.path.exists(model_candidate):
        return model_candidate

    local_filename = os.path.basename(model_candidate)
    local_path = os.path.join(BASE_MODELS_DIR, local_filename)
    if os.path.exists(local_path):
        return local_path

    if OCI_OBJECT_STORAGE_ENABLED:
        object_name = model_candidate
        if "/" not in object_name:
            object_name = f"{OCI_MODELS_PREFIX}{object_name}"
        _download_from_oci_object(object_name, temp_base_model_path)
        return temp_base_model_path

    raise FileNotFoundError(f"Base model not found: {model_candidate}")


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
    try:
        result = subprocess.run([BLENDER_PATH, "--version"], capture_output=True, text=True)
        blender_version = result.stdout.split("\n")[0]
    except Exception as e:
        blender_version = f"Error: {e}"

    namespace_value = OCI_NAMESPACE or "(auto-resolved)"
    oci_status = "disabled"
    if OCI_OBJECT_STORAGE_ENABLED:
        oci_status = "enabled" if oci is not None else "missing-sdk"

    return jsonify({
        "status": "ok",
        "blender_path": BLENDER_PATH,
        "blender_version": blender_version,
        "oci_enabled": OCI_OBJECT_STORAGE_ENABLED,
        "bucket": OCI_BUCKET_NAME,
        "namespace": namespace_value,
        "auth_mode": OCI_AUTH_MODE,
        "oci_object_storage": oci_status,
        "oci_region": OCI_REGION,
        "oci_namespace": namespace_value,
        "oci_bucket": OCI_BUCKET_NAME,
        "oci_models_prefix": OCI_MODELS_PREFIX,
        "oci_generated_prefix": OCI_GENERATED_PREFIX,
        "output_dir": OUTPUT_DIR,
        "temp_dir": TEMP_DIR
    })


@app.route("/generate", methods=["POST"])
def generate():
    temp_files = []
    output_path = None
    config_path = None

    try:
        config = request.json
        if not config:
            return jsonify({"error": "No config provided"}), 400

        if "output_name" in config:
            output_filename = config["output_name"]
            if not output_filename.endswith(".glb"):
                output_filename += ".glb"
            model_id = output_filename.replace("car_", "").replace(".glb", "")
        else:
            model_id = str(uuid.uuid4())[:8]
            output_filename = f"car_{model_id}.glb"

        output_path = os.path.join(OUTPUT_DIR, output_filename)
        base_model_input = config.get("base_model", CAR_MODEL_NAME)
        base_model_path = _resolve_base_model_to_local_file(base_model_input, model_id)
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
            print(result.stdout)
            print(result.stderr)
            return jsonify({
                "error": "Blender failed",
                "stdout": result.stdout,
                "stderr": result.stderr
            }), 500

        for _ in range(30):
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                break
            time.sleep(0.1)

        if not os.path.exists(output_path):
            return jsonify({
                "error": "Output file not created",
                "stdout": result.stdout
            }), 500

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
        return jsonify({"error": str(e)}), 500

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


@app.route("/models/<filename>", methods=["GET"])
def get_model(filename):
    filepath = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(filepath):
        return send_file(filepath, mimetype="model/gltf-binary")
    return jsonify({"error": "Model not found"}), 404


if __name__ == "__main__":
    print("INFO: Blender Service running on http://0.0.0.0:5000")
    print("INFO: Blender path:", BLENDER_PATH)
    print("INFO: Output directory:", OUTPUT_DIR)
    print("INFO: OCI object storage enabled:", OCI_OBJECT_STORAGE_ENABLED)
    app.run(host="0.0.0.0", port=5000, debug=False)
