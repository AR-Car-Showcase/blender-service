#!/usr/bin/env python3

import bpy
import json
import sys
import os


def hex_to_rgb(hex_color):
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i:i+2], 16) / 255.0 for i in (0, 2, 4))

def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    for data in [bpy.data.meshes, bpy.data.materials]:
        for block in data:
            if block.users == 0:
                data.remove(block)

def apply_material_colors(material_map):
    count = 0
    for mat in bpy.data.materials:
        if mat.name in material_map:
            color_hex = material_map[mat.name]
            rgb = hex_to_rgb(color_hex)
            
            if mat.use_nodes:
                for node in mat.node_tree.nodes:
                    if node.type == "BSDF_PRINCIPLED":
                        node.inputs["Base Color"].default_value = (*rgb, 1.0)
                        node.inputs["Metallic"].default_value = 0.5
                        node.inputs["Roughness"].default_value = 0.3
                        count += 1
                        break
            else:
                mat.diffuse_color = (*rgb, 1.0)
                count += 1
    
    print(f"INFO: Modified {count} materials")

# --------------------------------------------------

def main():
    argv = sys.argv
    if "--" not in argv:
        print("ERROR: No configuration file provided")
        sys.exit(1)

    config_path = argv[argv.index("--") + 1]

    with open(config_path) as f:
        config = json.load(f)

    base_model = config["base_model"]
    output_path = config["output_path"]
    materials = config.get("materials", {})

    clear_scene()

    print("[INFO] Importing model:", base_model)
    bpy.ops.import_scene.gltf(filepath=base_model)

    apply_material_colors(materials)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    bpy.ops.export_scene.gltf(
        filepath=output_path,
        export_format="GLB",
        export_apply=True,
        export_materials="EXPORT"
    )

# --------------------------------------------------

if __name__ == "__main__":
    main()
