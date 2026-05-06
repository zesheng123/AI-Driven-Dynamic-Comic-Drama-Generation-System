"""
routes/storyboard_route.py — ★ 备份/草稿文件，app_.py 未导入此文件 ★
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
⚠ 警告: 此文件与 routes/storyboard.py 定义了同名蓝图 (storyboard_bp)。
  两者不可同时在 app_.py 中导入，否则会引发 Flask 蓝图注册冲突。

  此文件的路由装饰器带有完整前缀 /api/storyboard/generate_stream，
  而 storyboard.py 使用相对路径 /generate_stream（注册时由 url_prefix 补全）。
  如果误将此文件导入，实际路由会变成 /api/storyboard/api/storyboard/...。

  当前生效文件: routes/storyboard.py
  本文件状态:   草稿/备份，请勿注册到 app_.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os
import re
import json
import uuid
import time
from flask import Blueprint, request, jsonify, Response

from services.llm_service import generate_storyboard_script, generate_scene_spec
from services.image_service import generate_storyboard_image
from services.panorama_service import get_best_view_for_shot

storyboard_bp = Blueprint("storyboard", __name__)


# ─────────────────────────────────────────
# SSE 流式生成（主接口）
# ─────────────────────────────────────────
@storyboard_bp.route("/api/storyboard/generate_stream", methods=["POST"])
def generate_stream():
    data           = request.get_json(force=True)
    plot           = data.get("story_text", "") or data.get("plot", "")
    style          = data.get("style", "日漫")
    global_scene   = data.get("global_scene", "")
    char_names     = data.get("characters", [])
    panorama_url   = data.get("panorama_url", "")
    panorama_local = data.get("panorama_local", "")
    panorama_views = data.get("panorama_views", [])
    scene_spec_in  = data.get("scene_spec", "")

    chars_data = _load_chars(char_names)

    def stream():
        # ── Step 0: 场景规范 ──────────────────────────
        if scene_spec_in:
            scene_spec = scene_spec_in
        else:
            yield _sse("status", {"msg": "🧠 豆包生成场景规范..."})
            try:
                result = generate_scene_spec(plot, global_scene)
                # generate_scene_spec 返回 dict 或 str，都处理
                if isinstance(result, dict):
                    scene_spec = result.get("scene_spec", "") or result.get("content", "")
                else:
                    scene_spec = result or ""
            except Exception as e:
                scene_spec = global_scene or ""
                print(f"[storyboard] scene_spec 失败: {e}")
        scene_spec_clean = re.sub(r'[\u4e00-\u9fff]+', ' ', scene_spec or "").strip()
        scene_spec_clean = re.sub(r'\s+', ' ', scene_spec_clean)

        # ── Step 1: 豆包生成脚本 ──────────────────────
        yield _sse("status", {"msg": "📜 豆包分析剧情，生成分镜脚本..."})
        try:
            script = generate_storyboard_script(
                story_text=plot,
                characters=chars_data,
                global_scene=global_scene,
                scene_spec=scene_spec_clean,
                style=style,
            )
        except Exception as e:
            yield _sse("error", {"msg": f"脚本生成异常: {e}"})
            return

        if not script or not script.get("shots"):
            yield _sse("error", {"msg": "脚本生成失败，请检查剧情文本"})
            return

        shots = script["shots"]
        total = len(shots)

        yield _sse("script_done", {
            "step": "script_done",
            "total": total,
            "shots_preview": shots,
            "panorama_url": panorama_url,
            "scene_spec": scene_spec_clean,
        })

        # ── Step 2: 串行逐镜生图（每镜完成立即推送）──
        results = [None] * total

        for i, shot in enumerate(shots):
            yield _sse("status", {"msg": f"🎨 生成第 {i+1}/{total} 镜..."})

            try:
                chars_in  = shot.get("characters_in_shot", [])
                best_view = get_best_view_for_shot(
                    panorama_views,
                    shot.get("shot_type", ""),
                    shot.get("action", ""),
                )
                char_refs = [c for c in chars_data if c.get("name") in chars_in]

                img_result = generate_storyboard_image(
                    shot=shot,
                    style=style,
                    char_refs=char_refs,
                    all_chars=chars_data,
                    scene_spec=scene_spec_clean,
                    panorama_views=panorama_views,
                )

                shot["image_url"]      = img_result.get("image_url", "")
                shot["_used_view_name"] = best_view.get("name", "全景") if best_view else "文生图"

            except Exception as e:
                print(f"[storyboard] 第{i+1}镜异常: {e}")
                shot["image_url"]      = ""
                shot["_used_view_name"] = "失败"

            shot["index"] = i + 1
            results[i]   = shot

            yield _sse("shot_done", {
                "shot":  shot,
                "index": i + 1,
                "done":  i + 1,
                "total": total,
            })

        yield _sse("complete", {
            "msg":       "分镜生成完成",
            "shots":     results,
            "project_id": str(uuid.uuid4())[:8],
        })

    return Response(
        stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────
# 单镜重新生成
# ─────────────────────────────────────────
@storyboard_bp.route("/api/storyboard/regen_shot", methods=["POST"])
def regen_shot():
    data           = request.get_json(force=True)
    shot           = data.get("shot", {})
    style          = data.get("style", "日漫")
    char_names     = data.get("characters", [])
    scene_spec     = data.get("scene_spec", "")
    panorama_views = data.get("panorama_views", [])

    chars_data = _load_chars(char_names)
    chars_in   = shot.get("characters_in_shot", [])
    char_refs  = [c for c in chars_data if c.get("name") in chars_in]

    result = generate_storyboard_image(
        shot=shot,
        style=style,
        char_refs=char_refs,
        all_chars=chars_data,
        scene_spec=scene_spec,
        panorama_views=panorama_views,
    )
    return jsonify(result)


# ─────────────────────────────────────────
# gen_panorama 兼容旧接口
# ─────────────────────────────────────────
@storyboard_bp.route("/api/storyboard/gen_panorama", methods=["POST"])
def gen_panorama():
    from services.panorama_service import generate_equirect_panorama, convert_panorama_to_views
    data         = request.get_json(force=True)
    plot         = data.get("plot", "")
    global_scene = data.get("global_scene", "")
    style        = data.get("style", "日漫")
    mode         = data.get("mode", "quad")

    try:
        spec_raw = generate_scene_spec(plot, global_scene)
        spec = spec_raw if isinstance(spec_raw, str) else spec_raw.get("scene_spec", global_scene)
    except Exception:
        spec = global_scene

    pano = generate_equirect_panorama(spec, style)
    if not pano["success"]:
        return jsonify(pano)

    views_result = convert_panorama_to_views(pano["local_path"], mode=mode)
    return jsonify({
        "success":        True,
        "panorama_url":   pano["url"],
        "panorama_local": pano["local_path"],
        "scene_spec":     spec,
        "views":          views_result.get("views", []),
    })


# ─────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────
def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _load_chars(char_names) -> list:
    """从 uploads/characters.json 读取角色数据"""
    db_path = os.path.join("uploads", "characters.json")
    if not os.path.exists(db_path):
        return []
    try:
        with open(db_path, encoding="utf-8") as f:
            db = json.load(f)
        chars = list(db.values())
        if char_names:
            names = [
                c.get("name") if isinstance(c, dict) else c
                for c in char_names
            ]
            chars = [c for c in chars if c.get("name") in names]
        return chars
    except Exception as e:
        print(f"[chars] 读取失败: {e}")
        return []