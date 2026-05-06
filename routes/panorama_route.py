# routes/panorama.py  ★ 已修复：彻底去人物污染版

import os
import json
from flask import Blueprint, request, jsonify, Response
from services.llm_service import generate_scene_spec

panorama_bp = Blueprint("panorama", __name__)


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _svc():
    from services.panorama_service import (
        generate_equirect_panorama,
        convert_panorama_to_views,
        convert_custom_angle,
    )
    return generate_equirect_panorama, convert_panorama_to_views, convert_custom_angle


@panorama_bp.route("/api/panorama/generate", methods=["POST"])
def api_generate_panorama():
    data         = request.get_json(force=True)
    plot         = data.get("plot", "")
    global_scene = data.get("global_scene", "")
    style        = data.get("style", "日漫")
    mode         = data.get("mode", "quad")

    def stream():
        generate_equirect_panorama, convert_panorama_to_views, _ = _svc()

        # ❗❗❗ 核心修复：完全不使用 plot（防止龙/人物污染）
        yield _sse("status", {"msg": "🧠 生成纯场景..."})

        try:
            result = generate_scene_spec(global_scene, global_scene, style)
            scene_spec = result.get("scene_spec", "") if isinstance(result, dict) else (result or "")
        except Exception as e:
            print(f"[panorama] scene_spec失败: {e}")
            scene_spec = global_scene or "empty wasteland, cracked ground, dramatic sky"

        yield _sse("scene_spec", {"scene_spec": scene_spec})

        # 生成全景图
        yield _sse("status", {"msg": "🎨 生成纯场景图..."})
        pano_result = generate_equirect_panorama(scene_spec, style, raw_scene=global_scene)

        if not pano_result["success"]:
            yield _sse("error", {"msg": pano_result.get("message", "全景图生成失败")})
            return

        pano_url  = pano_result["url"]
        pano_path = pano_result["local_path"]

        yield _sse("panorama_done", {
            "url": pano_url,
            "local_path": pano_path,
            "scene_spec": scene_spec,
        })

        # 多视角
        count = 12 if mode == "twelve" else 4
        yield _sse("status", {"msg": f"✂️ 转换 {count} 个视角..."})

        views_result = convert_panorama_to_views(pano_path, mode=mode)

        if not views_result["success"]:
            yield _sse("error", {"msg": views_result.get("message", "视角转换失败")})
            return

        yield _sse("views_done", {
            "views": views_result["views"],
            "panorama_url": pano_url,
            "panorama_local": pano_path,
            "scene_spec": scene_spec,
        })

        yield _sse("complete", {"msg": "完成"})

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )