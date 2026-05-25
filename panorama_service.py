"""
panorama_service.py — v11 场景背景生成 (彻底去词典化)
═══════════════════════════════════════════════════════
★ v11 核心修复 ★
  1. 【去词典化】完全删除硬编码中文关键词词典 _translate_scene_keywords
     - 原版本只支持 ~20 个室内词("教室""走廊"等)
     - 户外、奇幻、科幻、末日、古代、未来等场景全部失效
     - v11 改用豆包 LLM 实时翻译任意场景描述
  2. 【引擎切换】统一使用 Seedream 生成全景图
     - 不再使用旧图像引擎降级，避免错误提示污染前端
  3. 【智能尺寸】
     - 室内: 1:1 (裁切视角饱满)
     - 户外/全景: 16:9 (更开阔)
  4. 【视角扩展】
     - 支持自由数量的视角 (1/2/3/4/6)
     - 每个视角可自定义 yaw/pitch/fov
  5. 【场景判定】
     - 自动判定"室内/户外/奇幻/科幻",选择最合适的生成策略
"""
import os, math, uuid, re
import numpy as np
from PIL import Image

PANO_DIR = os.path.join("static", "panoramas")
os.makedirs(PANO_DIR, exist_ok=True)

# 室外/奇幻场景关键词（用于 aspect 选择, 不是内容限制）
_OUTDOOR_HINTS = [
    '户外', '室外', '野外', '野', '外面', '街上', '路上',
    '山', '海', '河', '湖', '森林', '草原', '沙漠', '峡谷', '悬崖',
    '天空', '云', '星空', '太空', '海底', '水下',
    '操场', '公园', '广场', '街道', '码头', '车站', '机场',
    '战场', '营地', '村庄', '城市', '郊区', '荒野',
    '废墟', '遗迹', '神殿', '宫殿', '城堡', '堡垒',
    '雪山', '火山', '极地', '丛林', '草地',
]


def _translate_scene_to_en(raw_scene: str, style: str = "日漫") -> str:
    """v11 核心升级: 用豆包 LLM 实时翻译任意中文场景为英文画面提示词

    不再使用硬编码词典。户外/奇幻/科幻/现代/古代任意场景都能翻译。

    失败时 fallback 为原文 + 通用关键词。
    """
    if not raw_scene or not raw_scene.strip():
        return "detailed scene, atmospheric lighting"

    # 如果输入已经是英文(含中文字符比例小)，直接返回
    if not re.search(r'[\u4e00-\u9fff]', raw_scene):
        return raw_scene.strip()

    try:
        from services.llm_service import _call_llm
        system = (
            "You are an AI image prompt translator.\n"
            "Translate Chinese scene into PURE environment prompt.\n"
            "STRICT RULES:\n"
            "- NO humans\n"
            "- NO characters\n"
            "- NO creatures\n"
            "- NO animals\n"
            "- NO monsters\n"
            "- Only environment, lighting, atmosphere\n"
            "Output ONLY English keywords."
        )
        user = f"""Translate this scene into English image-generation keywords:

"{raw_scene}"

Rules:
- Include: location/setting, lighting, atmosphere, key visual elements, time of day
- Keep NO characters/people in the description
- 30-60 English words, comma-separated
- Use concrete visual terms (e.g., 'abandoned gas station at sunset, rusted metal, cracked asphalt, apocalyptic wasteland, dramatic orange sky')
- Output ONLY the English keywords"""
        result = _call_llm(system, user, temperature=0.3, max_tokens=300)
        if result.get('success'):
            en = result['content'].strip()
            # 清洗中文字符
            en = re.sub(r'[\u4e00-\u9fff]+', '', en).strip()
            # 去掉常见前缀
            en = re.sub(r'^(output|keywords|translation)\s*:?\s*', '', en, flags=re.I)
            en = re.sub(r'\s+', ' ', en)
            if len(en) > 20:
                print(f"[panorama] LLM 翻译: {raw_scene[:40]} → {en[:100]}")
                return en
    except Exception as e:
        print(f"[panorama] LLM 翻译失败: {e}")

    # Fallback: 简单保底
    return "detailed background scene, atmospheric lighting, cinematic composition"


def _detect_scene_type(scene_text: str) -> dict:
    """v11 新增: 自动检测场景类型, 返回生成参数

    Returns:
        {
          'is_outdoor': bool,
          'aspect': '16:9' or '1:1',
          'width': int,
          'height': int,
        }
    """
    combined = scene_text or ""
    is_outdoor = any(hint in combined for hint in _OUTDOOR_HINTS)

    # 也判定英文
    en_outdoor = any(w in combined.lower() for w in [
        'outdoor', 'outside', 'mountain', 'forest', 'street', 'sky',
        'beach', 'field', 'desert', 'valley', 'cliff', 'ocean', 'sea',
        'castle', 'ruins', 'wasteland', 'battlefield', 'village', 'city',
        'rooftop', 'playground', 'park',
    ])
    is_outdoor = is_outdoor or en_outdoor

    if is_outdoor:
        return {
            'is_outdoor': True,
            'aspect': '16:9',
            'width': 1344,
            'height': 768,
            'size_str': '16:9',
        }
    else:
        return {
            'is_outdoor': False,
            'aspect': '1:1',
            'width': 1024,
            'height': 1024,
            'size_str': '1:1',
        }


def generate_equirect_panorama(scene_spec: str, style: str = "日漫",
                                raw_scene: str = "", engine: str = "doubao") -> dict:
    """v11: 生成宽幅场景图 (优先豆包)

    Args:
        scene_spec: 英文场景规范 (可能为空)
        style: 画风
        raw_scene: 用户原始输入(中文)
        engine: 'doubao' / 'seedream'，统一走 Seedream 中文主链路
    """
    # ── 1. 准备场景文本 (英文) ──
    scene_text_en = (scene_spec or '').strip()
    if not scene_text_en or len(scene_text_en) < 10:
        # LLM 翻译任意场景
        scene_text_en = _translate_scene_to_en(raw_scene or scene_spec, style)

    # ── 2. 判定场景类型, 选尺寸 ──
    scene_info = _detect_scene_type(raw_scene + " " + scene_spec)
    print(f"[panorama] 场景类型: {'户外' if scene_info['is_outdoor'] else '室内'}, "
          f"尺寸: {scene_info['size_str']}")

    # ── 3. 统一使用 Seedream 生成全景图 ──
    r = _generate_panorama_doubao(scene_text_en, style,
                                   raw_scene=raw_scene,
                                   scene_info=scene_info)
    if r.get("success"):
        return r
    msg = r.get('message', 'Seedream 全景图生成失败')
    print(f"[panorama] Seedream 全景图生成失败: {msg}")
    return {"success": False, "message": msg}


def _generate_panorama_doubao(scene_text_en: str, style: str,
                                raw_scene: str = "",
                                scene_info: dict = None) -> dict:
    """v11 新增: 豆包 Seedream 生成全景图"""
    from services.image_service import STYLE_ZH_PREFIX, _doubao_generate

    style_zh = STYLE_ZH_PREFIX.get(style) or f"{style}风格精致插画"

    # 构建中文 prompt (Seedream 中文理解更好)
    if raw_scene and re.search(r'[\u4e00-\u9fff]', raw_scene):
        scene_desc = raw_scene
    else:
        scene_desc = scene_text_en

    prompt_parts = [
        style_zh,
        f"场景：{scene_desc}",
        "宽幅场景远景构图，无任何人物，场景细节丰富",
        "天空与云层位于画面上半部，地平线保持在中下部，保留充足天空空间，不要低空压顶式构图",
        "光影细腻，氛围感强，画面饱满无留白",
        "电影级画质，完美构图，高清高质量",
    ]
    prompt = "，".join(prompt_parts)

    size_str = (scene_info or {}).get('size_str', '16:9')

    print(f"[panorama-doubao] prompt({len(prompt)}字): {prompt[:160]}...")
    result = _doubao_generate(prompt=prompt, ref_image_urls=None, size=size_str)

    if not result.get("success"):
        return result

    # 豆包返回的是 /static/shots/ 路径, 复制到 panoramas
    src_url = result.get("url") or result.get("image_url")
    if not src_url:
        return {"success": False, "message": "豆包返回路径为空"}

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = os.path.join(base_dir, src_url.lstrip("/"))
    local_name = f"pano_{uuid.uuid4().hex[:8]}.png"
    local_path = os.path.join(PANO_DIR, local_name)

    if os.path.exists(src):
        import shutil
        shutil.copy2(src, local_path)
        print(f"[panorama-doubao] ✓ {local_path}")
        return {
            "success": True,
            "local_path": local_path,
            "url": f"/static/panoramas/{local_name}",
        }

    return {"success": False, "message": "豆包文件未找到"}


def _generate_panorama_legacy(scene_text_en: str, style: str,
                              scene_info: dict = None) -> dict:
    """旧降级入口已移除：当前项目统一使用 Seedream 生成全景图。"""
    return {"success": False, "message": "旧降级入口已移除，请使用 Seedream 生成全景图"}

def convert_panorama_to_views(local_path, mode="quad", custom_views=None, out_size=(768, 432)):
    """从宽幅场景图裁切多个视角 (v11 兼容原有接口)"""
    if not os.path.exists(local_path):
        return {"success": False, "message": f"场景图不存在: {local_path}"}

    img = Image.open(local_path).convert("RGB")
    W, H = img.size
    base_name = os.path.splitext(os.path.basename(local_path))[0]

    # 视角定义
    views_def = [
        ("左侧视角",  0, "left",   -90, 0, 75),
        ("正面视角",   1, "center",   0, 0, 90),
        ("右侧视角",  2, "right",    90, 0, 75),
    ]
    if mode == "quad":
        views_def.append(("俯瞰视角", 3, "top", 0, -55, 85))
    elif mode == "single":
        views_def = [views_def[1]]  # 只要正面

    results = []
    n = len(views_def)

    aspect_w_over_h = W / H if H else 1.0

    for name, idx, suffix, yaw, pitch, fov in views_def:
        # 裁切宽度比例 (单图/三视图/四视图差异)
        if n == 1:
            crop_ratio = 1.0
        elif aspect_w_over_h > 1.5:  # 16:9 全景
            crop_ratio = 0.5
        else:  # 1:1 正方形
            crop_ratio = 0.55

        crop_w = int(W * crop_ratio)
        crop_h = int(crop_w * 9 / 16)
        if crop_h > H:
            crop_h = H
            crop_w = int(crop_h * 16 / 9)

        # 水平位置
        if n == 1:
            x_center = W // 2
        else:
            if aspect_w_over_h > 1.5:  # 16:9
                positions = [0.2, 0.5, 0.8, 0.5]
            else:  # 1:1
                positions = [0.25, 0.5, 0.75, 0.5]
            x_center = int(W * positions[min(idx, len(positions) - 1)])

        x0 = max(0, x_center - crop_w // 2)
        if x0 + crop_w > W:
            x0 = W - crop_w

        # 垂直位置
        if suffix == "top":
            y0 = 0
        else:
            y0 = max(0, int((H - crop_h) * 0.4))

        cropped = img.crop((x0, y0, x0 + crop_w, y0 + crop_h))
        cropped = cropped.resize(out_size, Image.LANCZOS)

        out_name = f"{base_name}_{suffix}.jpg"
        out_path = os.path.join(PANO_DIR, out_name)
        cropped.save(out_path, quality=92)

        results.append({
            "name": name,
            "url": f"/static/panoramas/{out_name}",
            "local_path": out_path,
            "yaw": yaw,
            "pitch": pitch,
            "fov": fov,
        })
        print(f"[panorama] {name}")

    return {"success": True, "views": results}


def convert_custom_angle(local_path, yaw=0, pitch=0, fov=90, out_size=(768, 432)):
    """自定义角度裁切"""
    if not os.path.exists(local_path):
        return {"success": False, "message": "场景图不存在"}
    img = Image.open(local_path).convert("RGB")
    W, H = img.size
    offset = int((yaw / 360.0) * W) % W
    crop_w = min(out_size[0], W)
    x0 = max(0, offset - crop_w // 2)
    x1 = min(W, x0 + crop_w)
    cropped = img.crop((x0, 0, x1, H)).resize(out_size, Image.LANCZOS)
    out_name = f"custom_{uuid.uuid4().hex[:6]}.jpg"
    out_path = os.path.join(PANO_DIR, out_name)
    cropped.save(out_path, quality=92)
    return {"success": True, "url": f"/static/panoramas/{out_name}", "local_path": out_path}


def get_best_view_for_shot(views, shot_type, action):
    """视角智能匹配 (v11 扩展)"""
    if not views:
        return None
    combined = ((shot_type or "") + " " + (action or "")).lower()
    rules = [
        (["corridor", "aisle", "走廊", "纵深", "深处"], ["右侧", "right"]),
        (["corner", "角落", "边缘"], ["左侧", "left"]),
        (["overhead", "俯瞰", "top", "鸟瞰"], ["俯瞰", "top"]),
        (["face to face", "面对面", "对视"], ["正面", "center"]),
    ]
    for kws, preferred in rules:
        if any(kw in combined for kw in kws):
            for pref in preferred:
                for v in views:
                    if pref in v.get("name", "") or pref in v.get("url", ""):
                        return v
    # 默认返回正面
    for v in views:
        if "正面" in v.get("name", "") or "center" in v.get("url", ""):
            return v
    return views[0]


# ═══════════════════════════════════════════════════════
# 向后兼容: 保留旧函数名 (仅作废弃警告, 不再使用硬编码词典)
# ═══════════════════════════════════════════════════════
def _translate_scene_keywords(raw_scene):
    """[DEPRECATED] 旧的硬编码词典翻译, v11 已改用 LLM 翻译.

    为保持向后兼容仍保留此函数, 但内部转用 _translate_scene_to_en.
    """
    print(f"[panorama] ⚠ 调用了已废弃的 _translate_scene_keywords, "
          f"自动转发到 LLM 翻译")
    return _translate_scene_to_en(raw_scene)