#!/usr/bin/env python3

from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import subprocess
import json
import os
import uuid
import time
import shutil

app = Flask(__name__)
CORS(app)



BLENDER_PATH = shutil.which("blender") or "blender"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Point to Spring Boot resources directory relative to this script
BASE_MODELS_DIR = os.path.abspath(os.path.join(BASE_DIR, "..", "src", "main", "resources", "static", "models"))
GENERATED_DIR = os.path.join(BASE_DIR, "generated")
OUTPUT_DIR = GENERATED_DIR

# Default fallback model
CAR_MODEL_PATH = os.path.join(BASE_MODELS_DIR, "car.glb")

os.makedirs(BASE_MODELS_DIR, exist_ok=True)
os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)



@app.route("/health", methods=["GET"])
def health():
    try:
        result = subprocess.run(
            [BLENDER_PATH, "--version"],
            capture_output=True,
            text=True
        )
        blender_version = result.stdout.split("\n")[0]
    except Exception as e:
        blender_version = f"Error: {e}"

    return jsonify({
        "status": "ok",
        "blender_path": BLENDER_PATH,
        "blender_version": blender_version,
        "base_model_exists": os.path.exists(CAR_MODEL_PATH),
        "output_dir": OUTPUT_DIR
    })



@app.route("/generate", methods=["POST"])
def generate():
    try:
        config = request.json
        if not config:
            return jsonify({"error": "No config provided"}), 400

        if not os.path.exists(CAR_MODEL_PATH):
            return jsonify({
                "error": "Base model missing",
                "details": CAR_MODEL_PATH
            }), 500

        if "output_name" in config:
            output_filename = config["output_name"]
            if not output_filename.endswith(".glb"):
                output_filename += ".glb"
            model_id = output_filename.replace("car_", "").replace(".glb", "")
        else:
            model_id = str(uuid.uuid4())[:8]
            output_filename = f"car_{model_id}.glb"
        
        output_path = os.path.join(OUTPUT_DIR, output_filename)
        base_model_input = config.get("base_model", "car.glb")
        
        if "/" in base_model_input:
            base_model_name = base_model_input.split("/")[-1]
        else:
            base_model_name = base_model_input
            
        base_model_path = os.path.join(BASE_MODELS_DIR, base_model_name)
             
        if not os.path.exists(base_model_path):
             return jsonify({
                 "error": f"Base model file not found in resources: {base_model_name}",
                 "details": base_model_path
             }), 404

        blender_config = {
            "base_model": base_model_path,
            "output_path": output_path,
            "materials": config.get("materials", {})
        }

        config_path = os.path.join(GENERATED_DIR, f"config_{model_id}.json")
        with open(config_path, "w") as f:
            json.dump(blender_config, f)

        script_path = os.path.join(BASE_DIR, "generate.py")

        cmd = [
            BLENDER_PATH,
            "--background",
            "--python", script_path,
            "--", config_path
        ]

        print(f"INFO: Generating model {model_id}")
        start = time.time()

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )

        elapsed = time.time() - start

        if os.path.exists(config_path):
            os.remove(config_path)

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

        print("SUCCESS: Output file generated:", output_path)

        return jsonify({
            "success": True,
            "model_id": model_id,
            "filename": output_filename,
            "model_url": output_filename,
            "download_url": f"/models/{output_filename}",
            "generation_time": round(elapsed, 2)
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500



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
    app.run(host="0.0.0.0", port=5000, debug=False)
