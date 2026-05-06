"""
services/image_service.py
"""
import os, time, re, requests
import dashscope
from dashscope import ImageSynthesis
from http import HTTPStatus

# API Key 硬写，确保所有接口都能用
_API_KEY = "sk-03c29524a21e446b85e3921ab529650b"
dashscope.api_key = _API_KEY

STYLE_PREFIX = {
    "日漫": "Japanese anime style, 2D animation, cel-shading, clean line art, vivid colors,",
    "国漫": "Chinese animation style, ink wash painting, bold outlines, rich colors,",
    "美漫": "American comic book style, strong shadows, bold lines, high contrast,",
    "写实": "realistic digital painting, detailed textures, photorealistic lighting,",
    "水墨": "traditional Chinese ink wash painting, monochrome, brush strokes,",
    "赛博朋克": "cyberpunk anime style, neon lights, dark atmosphere, futuristic,",
}

# ══════════════════════════════════════════════════════════════
# 角色立绘
# ══════════════════════════════════════════════════════════════

def generate_character_image(char_name, description, art_style="日漫"):
    prefix = STYLE_PREFIX.get(art_style, STYLE_PREFIX["日漫"])
    gw = _gender_word(description)
    prompt = (f"{prefix} {gw}, full body standing portrait, white background, "
              f"character design sheet, {description}, detailed costume, looking at viewer")
    neg = "multiple characters, background scene, 3D render, blur, watermark"
    return _t2i(prompt, neg, "768*1024")


def generate_character_views(description, art_style="日漫", front_url=None):
    """生成三视图，返回 {success, image_url, views:{front,side,face}}"""
    prefix = STYLE_PREFIX.get(art_style, STYLE_PREFIX["日漫"])
    gw = _gender_word(description)

    # 正面图
    p0 = (f"{prefix} {gw}, full body front view, character design sheet, "
          f"white background, {description}, standing pose, looking at viewer")
    r0 = _t2i(p0, "background scene, multiple people, blur", "768*1024")
    front = r0.get("url", "") if r0.get("success") else ""

    # 侧面图（参考正面）
    p1 = (f"{prefix} {gw}, full body side view profile, character design sheet, "
          f"white background, {description}, side pose")
    r1 = _t2i(p1, "background scene, front view", "768*1024",
              ref_url=front if front else None, ref_strength=0.55)
    side = r1.get("url", "") if r1.get("success") else ""

    # 特写图（参考正面）
    p2 = (f"{prefix} {gw}, face close-up portrait, white background, {description}, "
          f"detailed facial expression, upper body")
    r2 = _t2i(p2, "full body, background scene", "768*1024",
              ref_url=front if front else None, ref_strength=0.6)
    face = r2.get("url", "") if r2.get("success") else ""

    if not front:
        return {"success": False, "message": "正面图生成失败"}

    return {
        "success": True,
        "image_url": front,          # 主图 = 正面图
        "views": {
            "front": front,
            "side":  side,
            "face":  face,
        }
    }


# ══════════════════════════════════════════════════════════════
# 分镜图生成（纯文生图，场景规范注入prompt）
# ══════════════════════════════════════════════════════════════

def generate_storyboard_image(
    shot,
    char_refs=None,
    art_style="日漫",
    global_tone="",
    scene_spec="",
    scene_views=None,
):
    chars     = char_refs or []
    prefix    = STYLE_PREFIX.get(art_style, STYLE_PREFIX["日漫"])
    chars_in  = shot.get("characters_in_shot", [])
    scene_desc = shot.get("scene_description", "")
    action     = shot.get("action", "")

    sc = _clean_en(scene_spec or "")
    gt = _clean_en(global_tone or "")

    # 出场/不出场角色
    out_chars = [c for c in chars if c.get("name") in chars_in] if chars_in else []
    absent    = [c for c in chars if c.get("name") not in (chars_in or [])]
    absent_neg = ", ".join(
        _visual_kw(c.get("description", ""))
        for c in absent if _visual_kw(c.get("description", ""))
    )

    # 人数和性别词
    n = len(out_chars)
    count_w = {0: "no humans, empty scene,", 1: "1person,", 2: "2people,"}.get(n, f"{n}people,")
    gw = " ".join(_gender_word(c.get("description", "")) for c in out_chars)

    char_desc = "; ".join(
        f"{c.get('name', '')}: {c.get('description', '')}" for c in out_chars
    )

    # prompt 顺序：风格 > 人数性别 > 角色描述 > 场景规范 > 场景描述 > 动作 > 色调
    parts = [prefix, count_w]
    if gw:        parts.append(gw)
    if char_desc: parts.append(char_desc)
    if sc:        parts.append(sc[:200])
    if scene_desc: parts.append(scene_desc[:150])
    if action:    parts.append(action[:100])
    if gt:        parts.append(gt)
    parts.append("high quality, detailed, cinematic composition")
    prompt = ", ".join(p.strip().rstrip(",") for p in parts if p.strip())

    neg = "realistic photo, 3D render, watermark, text, extra limbs, deformed, blurry"
    if not out_chars:
        neg += ", humans, people, person, boy, girl, man, woman, face, body, figure"
    if absent_neg:
        neg += ", " + absent_neg

    return _t2i(prompt, neg, "1280*720")


# ══════════════════════════════════════════════════════════════
# 文生图核心
# ══════════════════════════════════════════════════════════════

def _t2i(prompt, negative="", size="1280*720", ref_url=None, ref_strength=0.5):
    dashscope.api_key = _API_KEY  # 每次调用前确保 key 已设置
    kw = dict(
        model="wanx2.1-t2i-turbo",
        prompt=prompt,
        negative_prompt=negative,
        n=1,
        size=size,
    )
    if ref_url:
        kw["ref_image_url"] = ref_url
        kw["ref_strength"]  = ref_strength

    try:
        rsp = ImageSynthesis.async_call(**kw)
        if rsp.status_code != HTTPStatus.OK:
            print(f"[t2i] 提交失败: {rsp.message}")
            return {"success": False, "message": rsp.message}

        for _ in range(80):
            time.sleep(4)
            result = ImageSynthesis.fetch(rsp)
            s = result.output.task_status
            if s == "SUCCEEDED":
                url = result.output.results[0].url
                print(f"[t2i] 成功: {url[:60]}")
                return {"success": True, "url": url, "image_url": url}
            if s == "FAILED":
                print(f"[t2i] 失败")
                return {"success": False, "message": "生成失败"}

        return {"success": False, "message": "超时"}
    except Exception as e:
        print(f"[t2i] 异常: {e}")
        return {"success": False, "message": str(e)}


# ══════════════════════════════════════════════════════════════
# 工具函数
# ══════════════════════════════════════════════════════════════

def _gender_word(desc):
    d = desc.lower()
    if re.search(r"\b(female|girl|woman|1girl)\b", d): return "1girl, female character"
    if re.search(r"\b(male|boy|man|1boy)\b", d):       return "1boy, male character"
    return "1person"

def _visual_kw(desc):
    kws = re.findall(
        r"\b([\w-]+ hair|[\w-]+ eyes|[\w-]+ dress|[\w-]+ uniform|"
        r"[\w-]+ skirt|[\w-]+ blouse|[\w-]+ coat|ponytail|braid|twintail)\b",
        desc.lower())
    return ", ".join(kws[:4])

def _clean_en(text):
    return re.sub(r'[\u4e00-\u9fff]+', '', text).strip().strip(",")