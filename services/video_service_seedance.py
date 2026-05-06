"""
video_service_seedance.py — 豆包 Seedance 逐镜视频生成 (v13)
═══════════════════════════════════════════════════════════════════════
★ v13 新增 / 修复 (2026-04-16 第二次迭代) ★
  1. ★★ 新增角色朝向提示 (_infer_orientation_hint)
     —— Seedance 不读图的空间关系, 多人物场景默认让所有人正对镜头
     —— 从 jimeng_ref_prompt 提取"画面左/右侧是X"自动生成"侧身相对"描述
     —— LLM 已写 video_prompt 时智能判断是否需要补充
  2. 修复 calc_shot_duration 的时长压制 bug
     —— 旧逻辑 min(dur, duration_hint=7, 10) 会把对话 9s 压回 7s
     —— 新逻辑: duration_hint 只上调不下压
  3. 时长范围 4→12 秒 (原 4-10, 1.5 Pro 原生支持到 12s)
  4. 对话镜头按字数动态分配时长 (3.5字/秒)
  5. 台词不再塞进 video_prompt (Seedance 不生成音频, TTS 是独立链路
     —— 原写法「开口说道「xxx」」可能被模型当成需要显示的字幕)

★ v12 关键修复 ★
  1. API 参数改为官方 --param 格式 (content[0].text 末尾拼接)
  2. 超时 300s → 600s
  3. 错误解析增强 (401/404/429/欠费)
  4. 统一 video_url 提取, 增加 health_check()

API 参考:
  - 创建任务: POST https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks
  - 参数格式: content[0].text 末尾追加 "--rs 720p --dur 5 --rt 16:9 --cf false"
"""
import os
import sys
import time
import uuid
import base64
import requests

# ═══════════════════════════════════════════════════════
# 配置
# ═══════════════════════════════════════════════════════
_SEEDANCE_KEY   = "58d47ba2-b181-44a7-9377-1fb9c6a6575a"
_SEEDANCE_MODEL = "ep-20260415220515-zzzqn"                  # 1.5 Pro 推理接入点
_SEEDANCE_API   = "https://ark.cn-beijing.volces.com/api/v3/contents/generations/tasks"
_POLL_INTERVAL  = 6
_MAX_WAIT       = 600   # v12: 从 300s 提到 600s, 1080p 视频可能需要 3-5 分钟
_DEFAULT_RS     = "720p"    # 720p/1080p  注: 1.5 Pro 支持 1080p
_DEFAULT_RATIO  = "16:9"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_SEEDANCE_KEY}",
    }


def _direct_session():
    """忽略系统代理，避免本机未开启代理时 requests 仍然走代理导致 WinError 10061。"""
    s = requests.Session()
    s.trust_env = False
    return s


def _direct_post(url, **kwargs):
    with _direct_session() as s:
        return s.post(url, **kwargs)


def _direct_get(url, **kwargs):
    with _direct_session() as s:
        return s.get(url, **kwargs)



def _save_video(content: bytes, name_hint: str = "shot") -> str:
    save_dir = os.path.join(BASE_DIR, "static", "videos", "shots")
    os.makedirs(save_dir, exist_ok=True)
    fname = f"sdance_{name_hint}_{uuid.uuid4().hex[:8]}.mp4"
    fpath = os.path.join(save_dir, fname)
    with open(fpath, "wb") as f:
        f.write(content)
    print(f"[Seedance] ✓ {fname}")
    return f"/static/videos/shots/{fname}"


def _img_to_data_uri(url_or_path: str):
    if not url_or_path:
        return None
    local = (os.path.join(BASE_DIR, url_or_path.lstrip("/"))
             if url_or_path.startswith("/") else url_or_path)
    if not os.path.exists(local):
        print(f"[Seedance] ⚠ 图片不存在: {local}")
        return None
    ext = os.path.splitext(local)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    with open(local, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return f"data:{mime};base64,{b64}"


# ═══════════════════════════════════════════════════════
# 智能时长计算 (范围 4~10 秒)
# ═══════════════════════════════════════════════════════
def calc_shot_duration(shot: dict) -> int:
    """根据分镜内容智能计算视频时长
    ★ v13: 范围 4~12 秒 (Seedance 1.5 Pro 原生支持)
    ★ v13 修复: duration_hint 不再作为硬上限压制智能计算结果
    """
    shot_type    = (shot.get("shot_type") or "").strip()
    action_zh    = (shot.get("action_zh") or shot.get("action") or "").strip()
    video_prompt = (shot.get("video_prompt") or "").strip()
    dialogue     = (shot.get("dialogue") or "").strip()
    is_key       = shot.get("is_key_shot") or shot.get("key_shot") or False
    duration_hint = shot.get("duration_hint")  # ★ 不再给默认 7
    chars_in     = shot.get("characters_in_shot") or []

    # 基础时长
    dur = 7

    # 无人物纯场景 → 4-5秒
    if not chars_in:
        dur = 5
    # ★ v13: 对话时长按字数动态算 (中文 ~3.5字/秒, 留 1 秒缓冲)
    elif dialogue:
        dia_len = len(dialogue)
        if dia_len > 30:
            dur = 12   # 长对话顶到上限
        elif dia_len > 20:
            dur = 10
        elif dia_len > 10:
            dur = 9
        else:
            dur = 7
    # 关键镜头
    elif is_key:
        dur = 9
    # 特写 → 情绪停留
    elif "特写" in shot_type:
        dur = 6
    # 远景/全景
    elif any(k in shot_type for k in ["远景", "全景", "环境"]):
        dur = 5

    # 动作复杂度 (video_prompt 越长越复杂)
    prompt_len = len(video_prompt) if video_prompt else len(action_zh)
    if prompt_len > 150:
        dur = max(dur, 10)
    elif prompt_len > 100:
        dur = max(dur, 9)
    elif prompt_len > 70:
        dur = max(dur, 8)
    elif prompt_len > 40:
        dur = max(dur, 7)

    # ★ v13: duration_hint 只作为"参考值上调", 不再硬压制
    # 旧逻辑 min(dur, duration_hint, 10) 会让 hint=7 把所有 9s/10s 都压回 7s
    if duration_hint:
        try:
            hint = int(duration_hint)
            # 如果 LLM hint 比智能算出的还大, 尊重 LLM
            if hint > dur:
                dur = min(hint, 12)
        except (ValueError, TypeError):
            pass

    # 范围限制 4~12
    dur = max(4, min(dur, 12))

    print(f"[Seedance] SHOT {shot.get('index','?')} 时长={dur}s "
          f"(type={shot_type}, chars={len(chars_in)}, dialogue={len(dialogue)}字, "
          f"prompt_len={prompt_len}, hint={duration_hint})")
    return dur


# ═══════════════════════════════════════════════════════
# ★ v13 新增: 角色朝向提示 (Seedance 不会读图的空间关系,
#   如果 video_prompt 不提朝向,模型默认让所有人物正对镜头)
# ═══════════════════════════════════════════════════════
_INTERACTION_KEYWORDS = [
    "对话", "说道", "对视", "看向", "看着", "望向", "望着", "盯着",
    "交谈", "争吵", "争执", "对峙", "对抗", "攻击", "打斗", "战斗",
    "握手", "拥抱", "推开", "抓住", "递给", "接过", "拉住", "靠近",
    "并肩", "挡在", "护着", "怒视", "凝视", "追赶", "追击", "迎战",
    "回头", "转身", "走向", "跑向",
]


def _infer_orientation_hint(shot: dict, is_env: bool) -> str:
    """推断并返回朝向提示词, 空串表示不需要提示。
    Seedance 不会自动读首帧图里的空间关系 → 需要在 prompt 里显式描述
    人物朝向, 否则模型容易让所有人物突然转向正对镜头。

    规则:
      1. 无人物/纯环境 → 无朝向
      2. 单人特写/近景 → 无朝向(允许正对镜头)
      3. 单人中远景/带方向性动作 → 提示保持首帧朝向
      4. 多人物(2+) → 强制侧身相对、保持空间关系
    """
    if is_env:
        return ""

    chars = shot.get("characters_in_shot") or []
    if len(chars) == 0:
        return ""

    shot_type = (shot.get("shot_type") or "").strip()
    action_zh = (shot.get("action_zh") or shot.get("action") or "").strip()
    ref_prompt = (shot.get("jimeng_ref_prompt") or "").strip()
    video_prompt = (shot.get("video_prompt") or "").strip()

    # ── 单人物 ──
    if len(chars) == 1:
        if any(k in shot_type for k in ["特写", "近景"]):
            return ""
        has_directional = any(
            k in action_zh for k in ["转身", "回头", "走向", "跑向", "看向", "望向", "追"]
        )
        if has_directional:
            return "人物保持与首帧图片一致的身体朝向和行进方向，不要正对镜头"
        return "人物保持与首帧图片一致的身体朝向，不要突然转向正对镜头"

    # ── 多人物 (2+) ──
    # 从 jimeng_ref_prompt 提取左右位置 (LLM 生成分镜图 prompt 时已遵循"左右铁律")
    # ★ 必须用 characters_in_shot 白名单过滤, 否则正则可能匹配到动词("挥砍"等)
    left_char, right_char = None, None
    if ref_prompt and chars:
        # 为每个角色名在 ref_prompt 里找最近的"左/右"方位词
        import re as _re
        for char_name in chars:
            if not char_name or char_name in (left_char, right_char):
                continue
            # 在角色名出现位置往前 30 字内搜索方位词
            for match in _re.finditer(_re.escape(char_name), ref_prompt):
                start = match.start()
                context_before = ref_prompt[max(0, start - 30):start]
                if _re.search(r"(?:画面)?左(?:侧|方|边)", context_before) and not left_char:
                    left_char = char_name
                    break
                elif _re.search(r"(?:画面)?右(?:侧|方|边)", context_before) and not right_char:
                    right_char = char_name
                    break
            if left_char and right_char:
                break

    check_text = action_zh + video_prompt
    is_combat = any(k in check_text for k in ["打斗", "战斗", "攻击", "对峙", "挥剑", "出招", "厮杀", "交锋"])
    is_interactive = any(kw in check_text for kw in _INTERACTION_KEYWORDS)
    has_dialogue = bool((shot.get("dialogue") or "").strip())

    if left_char and right_char:
        if is_combat:
            return (
                f"画面左侧的{left_char}保持侧身面向右方，画面右侧的{right_char}保持侧身面向左方，"
                f"两人形成明确的对峙关系，绝不要正对镜头，保持与首帧图片完全一致的站位和朝向"
            )
        elif is_interactive or has_dialogue:
            return (
                f"画面左侧的{left_char}侧身朝右看向{right_char}，画面右侧的{right_char}侧身朝左看向{left_char}，"
                f"两人目光相接，保持与首帧图片一致的空间朝向关系，不要正对镜头"
            )
        else:
            return (
                f"两人保持与首帧图片一致的站位——{left_char}在左、{right_char}在右，"
                f"身体朝向保持不变，不要突然转向正对镜头"
            )

    # 没提取到明确左右,但多人,仍需强调
    if is_combat:
        return "所有人物保持侧身对峙姿态，呈明确的空间对抗关系，绝不要正对镜头"
    if is_interactive or has_dialogue:
        return "人物之间保持侧身相对、目光相接的交互姿态，保持与首帧图片一致的朝向，不要正对镜头"
    return "所有人物保持与首帧图片一致的身体朝向和站位，不要默认转向镜头"


# ═══════════════════════════════════════════════════════
# ★ 视频 prompt 构建 (纯净, 不混入API参数)
# ═══════════════════════════════════════════════════════
def build_video_prompt(shot: dict, is_env: bool = False,
                        next_shot: dict = None) -> str:
    """为 Seedance 1.5 Pro 构建视频提示词 (v11 增强版)

    v11 改动:
      1. 目标长度从 180-250 字提升到 250-400 字，给 AI 更丰富的动作信息
      2. 新增 next_shot 参数：首尾帧模式时追加过渡描述（来自 jimeng_trans_prompt）
      3. 动态加入角色外貌锚点（避免 AI 自由发挥服装）
      4. 增强环境动态描述（光影/粒子/天气）
    """
    # ── 优先使用 LLM 生成的 video_prompt ──
    video_prompt = (shot.get("video_prompt") or "").strip()
    if video_prompt and len(video_prompt) >= 40:
        extras = []

        # 风格锚点
        if not any(k in video_prompt for k in ['动漫', '风格', '画风', '插画']):
            extras.append("日系动漫风格")

        # 环境动态
        if not any(k in video_prompt for k in ['飘动', '动态', '流动', '摇曳', '晃动']):
            extras.append("衣物和头发随动作自然飘动")

        # 画质
        if not any(k in video_prompt for k in ['流畅', '细腻', '高质', '高清']):
            extras.append("画面流畅细腻高清")

        # ★ v13: 朝向提示(仅当 video_prompt 本身未提到朝向时才补充)
        if not any(k in video_prompt for k in [
            '朝向', '面向', '侧身', '侧面', '背对', '侧脸', '转向',
            '左侧', '右侧', '目光相接', '对视'
        ]):
            orient = _infer_orientation_hint(shot, is_env=is_env)
            if orient:
                extras.append(orient)

        # v11: 首尾帧过渡提示
        if next_shot:
            trans = (shot.get("jimeng_trans_prompt") or "").strip()
            if trans and trans != "无" and len(trans) > 5:
                extras.append(f"镜头末尾自然过渡：{trans}")
            elif next_shot.get("scene_change"):
                extras.append("镜头末尾画面渐出，场景自然切换")

        base = video_prompt + ("，" + "，".join(extras) if extras else "")

        # v11: 如果还不足 150 字，追加场景环境动态补充
        if len(base) < 150:
            scene = (shot.get("scene_description") or "").strip()
            if scene and scene not in base:
                base = f"{scene}，{base}"

        return base

    # ── Fallback: 系统拼接（v11 大幅增强） ──
    scene = shot.get("scene_description") or ""
    action_zh = shot.get("action_zh") or shot.get("action") or "镜头缓缓推进"
    dialogue = (shot.get("dialogue") or "").strip()

    emotion_map = {
        'sad':       '表情逐渐忧伤，眼神低垂，嘴角微微下垂',
        'happy':     '嘴角缓缓上扬，眼睛微微弯起，露出愉快笑容',
        'surprised': '眼睛猛地睁大，嘴微张，神情惊讶',
        'angry':     '眉头紧蹙，嘴唇绷紧，神情逐渐愤怒',
        'calm':      '神情始终平静淡然，眼神沉稳',
        'determined':'眼神逐渐坚定，嘴唇抿紧，姿态挺立',
        'nostalgic': '眼神迷离，若有所思，视线飘向远方',
        'tearful':   '眼眶慢慢泛红，眼角积聚泪水',
        'gentle':    '眼神温柔，嘴角带着浅浅微笑',
        'curious':   '微微歪头，眼神闪烁好奇光芒',
        'shocked':   '瞳孔骤然放大，全身微微僵住，震惊失色',
        'puzzled':   '蹙眉思索，嘴唇轻动，一脸困惑',
        'anxious':   '神情焦虑，双手不安地轻握，坐立难安',
        'pensive':   '沉思若定，视线凝聚在某处不动',
        'reluctant': '神情犹豫，脚步迟疑，欲言又止',
    }
    emotion_zh = emotion_map.get(shot.get("emotion", ""), "")

    shot_type = shot.get("shot_type") or ""
    camera_map = {
        "特写": "镜头缓慢而稳定地推近，最终聚焦在面部情绪，有轻微呼吸感",
        "近景": "镜头轻微浮动，模拟手持拍摄感，人物面部和上半身清晰",
        "中景": "镜头平稳缓慢横移，均匀展示人物姿态与周围环境",
        "远景": "镜头从中景缓缓拉远，最终展现宏大完整的环境全貌",
        "全景": "镜头平滑弧形旋转，沉浸式展示整个场景空间",
        "环境": "固定镜头，光影随时间自然流动变化，无人物干扰",
    }
    camera_zh = next((v for k, v in camera_map.items() if k in shot_type),
                     "镜头保持稳定，有轻微呼吸感")

    if is_env or not shot.get("characters_in_shot"):
        # 纯环境镜头：重点描述光影/天气/氛围变化
        env_parts = []
        if scene:
            env_parts.append(scene)
        env_parts.append("无人物纯场景")
        env_parts.append("光影随时间自然缓慢流动，明暗层次丰富")
        # 尝试从场景描述推断环境动态
        scene_low = scene.lower()
        if any(w in scene_low for w in ['风', '树', '叶', '草']):
            env_parts.append("树叶随风轻轻摇曳")
        if any(w in scene_low for w in ['水', '河', '海', '湖', '雨']):
            env_parts.append("水面微微荡漾，波光粼粼")
        if any(w in scene_low for w in ['云', '天', '空', '星']):
            env_parts.append("云层缓慢漂移，天色渐变")
        env_parts.append(camera_zh)
        env_parts.append("日系动漫风格，画面流畅细腻高清")
        return "，".join(p for p in env_parts if p)

    # 有人物：完整动作序列（先→然后→接着→最后）
    seq_parts = []

    if scene:
        seq_parts.append(scene)

    # ★ v13: 朝向提示放在最前面(人物做动作前先定位好朝向)
    orient = _infer_orientation_hint(shot, is_env=False)
    if orient:
        seq_parts.append(orient)

    # 主要动作序列
    if action_zh:
        seq_parts.append(f"人物先{action_zh}")
    if emotion_zh:
        seq_parts.append(f"然后{emotion_zh}")
    if dialogue:
        # v13: 改为"轻启双唇说话"的视觉描述, 避免 Seedance 把台词当字幕
        seq_parts.append("嘴唇自然张合，口型与说话节奏吻合")

    # 环境动态（让画面不死板）
    scene_low = scene.lower()
    env_dynamics = []
    if any(w in scene_low for w in ['风', '窗', '帘', '树']):
        env_dynamics.append("窗帘或树叶随风轻轻摇曳")
    if any(w in scene_low for w in ['夕阳', '阳光', '光斑', '光线', '日光']):
        env_dynamics.append("光斑随云层移动缓慢变化")
    if any(w in scene_low for w in ['雨', '水', '河', '海']):
        env_dynamics.append("水面轻微波动")
    if env_dynamics:
        seq_parts.append("同时" + "，".join(env_dynamics))

    # 衣物/发丝动态
    seq_parts.append("衣物和发丝随动作自然飘动")

    # 镜头运动
    seq_parts.append(camera_zh)

    # v11: 首尾帧过渡提示
    if next_shot:
        trans = (shot.get("jimeng_trans_prompt") or "").strip()
        if trans and trans != "无" and len(trans) > 5:
            seq_parts.append(f"末尾{trans}")

    seq_parts.append("日系动漫风格，画面流畅细腻高清")

    return "，".join(p for p in seq_parts if p)


def build_web_friendly_prompt(shot: dict, duration: int = None) -> str:
    """生成适合即梦网页版手动测试的纯文本提示词"""
    dur = duration if duration else calc_shot_duration(shot)
    is_env = not shot.get("characters_in_shot")
    base = build_video_prompt(shot, is_env=is_env)
    ratio_zh = {'16:9': '横屏16:9', '9:16': '竖屏9:16', '1:1': '正方形1:1'}.get(
        _DEFAULT_RATIO, '横屏16:9')
    return f"{base}，{ratio_zh}，时长{dur}秒，高清1080P"


# ═══════════════════════════════════════════════════════
# API 调用底层 (v12 修复版)
# ═══════════════════════════════════════════════════════
# ★ 关键修复 ★
#   火山引擎 Seedance 官方 API 的控制参数 **必须** 通过 content.text 末尾
#   的 `--param value` 形式传递，不存在独立的 `parameters` 字段。
#   （官方文档示例: "...--rs 720p --dur 5 --rt 16:9 --cf false"）
#
#   原 v11 代码先尝试不存在的 `parameters` 字段 → 必然返回 400 → 再降级
#   到 v2，导致每次任务都要发 2 次 HTTP 请求，日志刷错。v12 直接只用
#   官方正确格式，一次请求即可。
# ═══════════════════════════════════════════════════════
def _create_task(prompt: str, image_uri: str = None,
                 last_frame_uri: str = None,
                 duration: int = None, resolution: str = None,
                 ratio: str = None) -> dict:
    """创建 Seedance 任务 (官方 --param 风格)
    支持:
      * 纯文生视频 (image_uri=None)
      * 图生视频 (image_uri=first_frame)
      * 首尾帧生视频 (image_uri + last_frame_uri)
    参数通过 content[0].text 末尾追加 "--rs 720p --dur 5 --rt 16:9 --cf false"
    """
    # ── 1. 拼接控制参数到 prompt 末尾 ──
    suffix_parts = []
    if ratio:
        suffix_parts.append(f"--rt {ratio}")          # --rt 16:9
    if duration:
        suffix_parts.append(f"--dur {int(duration)}") # --dur 5
    if resolution:
        suffix_parts.append(f"--rs {resolution}")     # --rs 720p
    # --cf false: 不固定镜头，允许运镜
    suffix_parts.append("--cf false")
    full_text = prompt
    if suffix_parts:
        full_text = prompt.rstrip() + " " + " ".join(suffix_parts)

    # ── 2. 构建 content 数组 ──
    content = [{"type": "text", "text": full_text}]
    if image_uri:
        content.append({
            "type": "image_url",
            "image_url": {"url": image_uri},
            "role": "first_frame",
        })
    if last_frame_uri:
        content.append({
            "type": "image_url",
            "image_url": {"url": last_frame_uri},
            "role": "last_frame",
        })

    payload = {
        "model": _SEEDANCE_MODEL,
        "content": content,
    }

    # ── 3. 发送请求 (timeout 调高到 90s, base64 图片可能较大) ──
    try:
        resp = _direct_post(_SEEDANCE_API, headers=_headers(),
                             json=payload, timeout=90)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:500]}

        # 日志完整打印错误信息, 方便排查
        print(f"[Seedance] 创建 HTTP {resp.status_code}: {str(data)[:400]}")

        if resp.status_code == 200 and data.get("id"):
            return {"success": True, "task_id": data["id"]}

        # 解析错误消息
        err_obj = data.get("error") or {}
        err_msg = err_obj.get("message") or err_obj.get("code") or str(data)
        err_msg = str(err_msg)[:400]

        # 给常见错误加友好提示
        low = err_msg.lower()
        if resp.status_code == 401 or "unauthorized" in low or "api key" in low:
            err_msg = f"[401 API Key 无效或过期] {err_msg}"
        elif resp.status_code == 404 or "endpoint" in low or "not found" in low:
            err_msg = f"[404 推理接入点未找到/未启用, 请在火山方舟控制台检查] {err_msg}"
        elif "balance" in low or "quota" in low or "insufficient" in low or "欠费" in err_msg:
            err_msg = f"[账户余额/额度不足, 请充值] {err_msg}"
        elif resp.status_code == 429 or "rate" in low or "limit" in low:
            err_msg = f"[429 QPS 超限/并发过多, 请稍后重试] {err_msg}"

        return {"success": False, "message": err_msg}

    except requests.exceptions.Timeout:
        return {"success": False, "message": "创建任务请求超时 (90s), 可能是图片过大"}
    except Exception as e:
        print(f"[Seedance] 创建异常: {e}")
        return {"success": False, "message": f"创建异常: {str(e)}"}


def _extract_video_url(data: dict) -> str:
    """从查询响应中提取视频 URL, 兼容多种字段格式"""
    # 格式 1: content.video_url.url
    content = data.get("content")
    if isinstance(content, dict):
        vu = content.get("video_url")
        if isinstance(vu, dict):
            url = vu.get("url", "")
            if url:
                return url
        elif isinstance(vu, str):
            return vu
        # 偶有直接 content.url
        if content.get("url"):
            return content["url"]
    # 格式 2: content 是 list
    if isinstance(content, list):
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "video_url":
                vu = c.get("video_url")
                if isinstance(vu, dict) and vu.get("url"):
                    return vu["url"]
                if isinstance(vu, str):
                    return vu
            if c.get("url"):
                return c["url"]
    # 格式 3: 顶层 video_url
    if isinstance(data.get("video_url"), str):
        return data["video_url"]
    if isinstance(data.get("video_url"), dict):
        return data["video_url"].get("url", "")
    return ""


def _wait_for_task(task_id: str, label: str = "") -> dict:
    """轮询等待任务完成"""
    waited = 0
    while waited < _MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
        try:
            resp = _direct_get(f"{_SEEDANCE_API}/{task_id}",
                                headers=_headers(), timeout=15)
            try:
                data = resp.json()
            except Exception:
                print(f"[Seedance] 轮询 HTTP {resp.status_code}, 响应非 JSON")
                continue
            status = data.get("status", "unknown")
            print(f"[Seedance] {label} {status} ({waited}s)")

            if status == "succeeded":
                url = _extract_video_url(data)
                if url:
                    return {"success": True, "video_url": url}
                print(f"[Seedance] 成功但未找到 URL, 完整响应: {str(data)[:400]}")
                return {"success": False, "message": "成功但找不到视频URL"}

            elif status == "failed":
                err_obj = data.get("error") or {}
                err = err_obj.get("message") or err_obj.get("code") or "任务失败"
                return {"success": False, "message": str(err)[:300]}

            elif status in ("cancelled", "canceled"):
                return {"success": False, "message": "任务已被取消"}
            # queued / running / unknown → 继续轮询
        except requests.exceptions.Timeout:
            print(f"[Seedance] 轮询超时, 继续等待")
        except Exception as e:
            print(f"[Seedance] 轮询异常: {e}")
    return {"success": False, "message": f"轮询超时({_MAX_WAIT}s), 任务可能仍在进行, 请到火山方舟控制台查看"}


# ═══════════════════════════════════════════════════════
# 主入口 — 单镜视频生成
# ═══════════════════════════════════════════════════════
def generate_single_shot_video(shot: dict,
                                 resolution: str = _DEFAULT_RS,
                                 duration: int = None,
                                 ratio: str = _DEFAULT_RATIO,
                                 last_frame_url: str = None) -> dict:
    """生成单个分镜的视频
    Args:
        shot: 分镜数据字典
        resolution: '720p' 或 '1080p'
        duration: None 时自动计算 (4-10秒)
        ratio: '16:9' / '9:16' / '1:1'
        last_frame_url: 尾帧图片路径 (可选，有则启用首尾帧模式)
    """
    dur = duration if duration is not None else calc_shot_duration(shot)
    is_env = not shot.get("characters_in_shot")

    full_prompt = build_video_prompt(shot, is_env=is_env)

    print(f"\n[Seedance] ── SHOT {shot.get('index','?')} ──────")
    print(f"[Seedance] 时长={dur}s  分辨率={resolution}  比例={ratio}")
    print(f"[Seedance] 首尾帧={'是' if last_frame_url else '否'}")
    print(f"[Seedance] prompt({len(full_prompt)}字): {full_prompt[:200]}...")

    # 首帧图
    img_url = shot.get("image_url", "")
    image_uri = None
    if img_url:
        if img_url.startswith("/"):
            image_uri = _img_to_data_uri(img_url)
            if image_uri:
                print(f"[Seedance] 首帧: {os.path.basename(img_url)}")
        elif img_url.startswith("http"):
            image_uri = img_url

    # 尾帧图 (可选)
    last_frame_uri = None
    if last_frame_url:
        if last_frame_url.startswith("/"):
            last_frame_uri = _img_to_data_uri(last_frame_url)
            if last_frame_uri:
                print(f"[Seedance] 尾帧: {os.path.basename(last_frame_url)}")
        elif last_frame_url.startswith("http"):
            last_frame_uri = last_frame_url

    cr = _create_task(
        prompt=full_prompt,
        image_uri=image_uri,
        last_frame_uri=last_frame_uri,
        duration=dur,
        resolution=resolution,
        ratio=ratio,
    )
    if not cr["success"]:
        return cr

    wr = _wait_for_task(cr["task_id"], f"SHOT{shot.get('index','?')}")
    if not wr["success"]:
        return wr

    try:
        r = _direct_get(wr["video_url"], timeout=120)
        local = _save_video(r.content, f"s{shot.get('index',0):02d}_{dur}s")
        return {
            "success": True,
            "video_url": local,
            "task_id": cr["task_id"],
            "duration": dur,
        }
    except Exception:
        return {
            "success": True,
            "video_url": wr["video_url"],
            "task_id": cr["task_id"],
            "duration": dur,
        }


# ═══════════════════════════════════════════════════════
# ★★ 首尾帧相邻对批量任务 (v11 核心) ★★
# ═══════════════════════════════════════════════════════
def create_per_shot_tasks(shots: list,
                           resolution: str = _DEFAULT_RS,
                           ratio: str = _DEFAULT_RATIO,
                           duration: int = None) -> dict:
    """★ v11: 首尾帧相邻对模式 —— 每个任务用相邻两张分镜图做首尾帧
    Args:
        duration: 若不为 None，强制每镜使用该时长（秒），覆盖智能计算

    策略:
      - N 张分镜图 → N 个任务
      - Task[i] (i < N-1): first_frame=shots[i].image, last_frame=shots[i+1].image
                            prompt = shots[i].video_prompt + 过渡提示
      - Task[N-1] (最后一镜): first_frame=shots[N-1].image, 无 last_frame
      - 效果: 前一段视频的尾帧 == 后一段视频的首帧 → 拼接时视觉连贯

    Returns:
        {
          success: true,
          tasks: [
            {
              shot_index: 1,
              next_shot_index: 2,    # 尾帧对应的 shot（末镜无此字段）
              has_last_frame: true,
              duration: 7,
              task_id: '...',
              success: true,
            },
            ...
          ],
          total_shots: N,
          estimated_total_duration: T,
        }
    """
    valid = [s for s in shots if s and s.get("image_url")]
    if not valid:
        return {"success": False, "message": "没有带图片的分镜"}

    tasks = []
    total_dur = 0

    for i, shot in enumerate(valid):
        # ★ 用户手动指定时长时直接使用，否则智能计算
        dur = int(duration) if duration else calc_shot_duration(shot)
        # v13: 限制范围 4~12 秒 (Seedance 1.5 Pro 支持)
        dur = max(4, min(dur, 12))
        total_dur += dur
        is_env = not shot.get("characters_in_shot")
        is_last = (i == len(valid) - 1)

        # 相邻的下一镜（用于尾帧 + 过渡提示）
        next_shot = valid[i + 1] if not is_last else None

        # 构建 prompt（传入 next_shot 以生成过渡提示）
        full_prompt = build_video_prompt(shot, is_env=is_env, next_shot=next_shot)

        # 首帧
        img_url = shot.get("image_url", "")
        image_uri = None
        if img_url.startswith("/"):
            image_uri = _img_to_data_uri(img_url)
        elif img_url.startswith("http"):
            image_uri = img_url

        # 尾帧（下一镜的分镜图）
        last_frame_uri = None
        if next_shot:
            next_img = next_shot.get("image_url", "")
            if next_img.startswith("/"):
                last_frame_uri = _img_to_data_uri(next_img)
            elif next_img.startswith("http"):
                last_frame_uri = next_img

        print(f"\n[Seedance] ── SHOT {shot.get('index','?')} "
              f"{'→ SHOT ' + str(next_shot.get('index','?')) if next_shot else '(末镜)'} ──")
        print(f"[Seedance] 时长={dur}s  首尾帧={'是' if last_frame_uri else '否'}")
        print(f"[Seedance] prompt({len(full_prompt)}字): {full_prompt[:150]}...")

        cr = _create_task(
            prompt=full_prompt,
            image_uri=image_uri,
            last_frame_uri=last_frame_uri,
            duration=dur,
            resolution=resolution,
            ratio=ratio,
        )

        task_entry = {
            "shot_index": shot.get("index", 0),
            "duration": dur,
            "has_last_frame": bool(last_frame_uri),
            "web_prompt": build_web_friendly_prompt(shot, duration=dur),
        }
        if not is_last:
            task_entry["next_shot_index"] = next_shot.get("index", 0)

        if cr["success"]:
            task_entry.update({"success": True, "task_id": cr["task_id"]})
        else:
            task_entry.update({"success": False, "message": cr.get("message", "")})
            print(f"[Seedance] ✗ SHOT {shot.get('index','?')} 任务创建失败: "
                  f"{cr.get('message','')}")
        tasks.append(task_entry)

    success_count = sum(1 for t in tasks if t.get("success"))
    print(f"\n[Seedance] ★ 首尾帧批量: {success_count}/{len(tasks)} 任务成功, "
          f"预估总时长 {total_dur}s")

    return {
        "success": success_count > 0,
        "tasks": tasks,
        "total_shots": len(valid),
        "estimated_total_duration": total_dur,
    }


def poll_task(task_id: str) -> dict:
    """查询单个任务状态 (供前端/路由调用)"""
    try:
        resp = _direct_get(f"{_SEEDANCE_API}/{task_id}",
                            headers=_headers(), timeout=15)
        try:
            data = resp.json()
        except Exception:
            return {"success": False, "status": "error",
                    "message": f"HTTP {resp.status_code}: 非 JSON 响应"}

        status = data.get("status", "unknown")
        video_url = ""
        err_msg = ""

        if status == "succeeded":
            video_url = _extract_video_url(data)
        elif status == "failed":
            err_obj = data.get("error") or {}
            err_msg = err_obj.get("message") or err_obj.get("code") or "任务失败"
            err_msg = str(err_msg)[:300]

        return {
            "success": True,
            "status": status,
            "video_url": video_url,
            "message": err_msg,
        }
    except Exception as e:
        return {"success": False, "status": "error", "message": str(e)}


# ═══════════════════════════════════════════════════════
# ★ 新增: 批量生成网页版提示词 (给用户手动测试用)
# ═══════════════════════════════════════════════════════
def generate_web_prompts(shots: list) -> dict:
    """为所有分镜生成即梦网页版的提示词
    返回一个字符串数组, 用户可以一条条复制到即梦网页测试
    """
    valid = [s for s in shots if s]
    if not valid:
        return {"success": False, "message": "没有分镜数据"}

    prompts = []
    total_dur = 0
    for shot in valid:
        dur = calc_shot_duration(shot)
        total_dur += dur
        prompts.append({
            "shot_index": shot.get("index", 0),
            "duration": dur,
            "prompt": build_web_friendly_prompt(shot, duration=dur),
            "image_url": shot.get("image_url", ""),  # 首帧图路径
            "shot_type": shot.get("shot_type", ""),
        })

    return {
        "success": True,
        "prompts": prompts,
        "total_shots": len(valid),
        "estimated_total_duration": total_dur,
    }

# ═══════════════════════════════════════════════════════
# create_full_task / create_batch_tasks
# (路由层调用接口，内部复用首尾帧逻辑)
# ═══════════════════════════════════════════════════════
def create_full_task(shots: list,
                      duration: int = None,
                      resolution: str = _DEFAULT_RS,
                      ratio: str = _DEFAULT_RATIO) -> dict:
    """一次提交所有分镜的首尾帧任务（供 /create_full_task 路由调用）

    内部直接调用 create_per_shot_tasks，保持首尾帧一致性逻辑。
    duration 参数对全局时长无精确控制（每镜自动计算），保留作为兼容参数。
    """
    return create_per_shot_tasks(shots, resolution=resolution, ratio=ratio)


def create_batch_tasks(shots: list,
                        batch_size: int = 4,
                        duration: int = None,
                        resolution: str = _DEFAULT_RS,
                        ratio: str = _DEFAULT_RATIO) -> dict:
    """分批提交首尾帧任务（供 /create_batch_tasks 路由调用）

    将 shots 按 batch_size 切分，每批独立调用 create_per_shot_tasks。
    注意：批次边界处的两张分镜不会相互形成首尾帧对，批次间需要手动转场。

    Returns:
        {
          success: true,
          total_batches: N,
          batches: [
            { batch: 1, shots: [1,2,3,4], tasks: [...], success: true },
            ...
          ],
          total_shots: M,
        }
    """
    valid = [s for s in shots if s and s.get("image_url")]
    if not valid:
        return {"success": False, "message": "没有带图片的分镜"}

    batch_size = max(2, batch_size)  # 至少 2 镜才能形成首尾帧对
    batches_shots = []
    for i in range(0, len(valid), batch_size):
        chunk = valid[i:i + batch_size]
        batches_shots.append(chunk)

    print(f"\n[Seedance] ★ 分批首尾帧: {len(valid)} 镜 → {len(batches_shots)} 批 "
          f"(每批 {batch_size} 镜)")

    results = []
    for bi, batch in enumerate(batches_shots, 1):
        shot_indices = [s.get("index", 0) for s in batch]
        print(f"\n[Seedance] 批次 {bi}/{len(batches_shots)} (SHOT {shot_indices})")

        cr = create_per_shot_tasks(batch, resolution=resolution, ratio=ratio)
        results.append({
            "batch": bi,
            "shots": shot_indices,
            "success": cr.get("success", False),
            "tasks": cr.get("tasks", []),
            "message": cr.get("message", "") if not cr.get("success") else "",
        })

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "success": success_count > 0,
        "total_batches": len(batches_shots),
        "success_batches": success_count,
        "batches": results,
        "total_shots": len(valid),
    }

# ═══════════════════════════════════════════════════════
# v12 新增: 健康检查 (用于排查 endpoint/API Key 问题)
# ═══════════════════════════════════════════════════════
def health_check() -> dict:
    """发送一个最小的测试请求, 验证 endpoint 和 API Key 是否可用
    返回:
      { ok: bool, endpoint: str, message: str, task_id?: str }

    用法: 在 Python shell 里
      >>> from services.video_service_seedance import health_check
      >>> print(health_check())
    """
    test_prompt = "一只可爱的橘色小猫在阳光明媚的草地上慢慢走动，画面温馨治愈 --rs 480p --dur 5 --rt 16:9"
    payload = {
        "model": _SEEDANCE_MODEL,
        "content": [{"type": "text", "text": test_prompt}],
    }
    try:
        resp = _direct_post(_SEEDANCE_API, headers=_headers(),
                             json=payload, timeout=30)
        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text[:500]}

        if resp.status_code == 200 and data.get("id"):
            return {
                "ok": True,
                "endpoint": _SEEDANCE_MODEL,
                "task_id": data["id"],
                "message": f"✓ 健康检查通过, 测试任务已创建: {data['id']}",
            }

        err = (data.get("error") or {})
        msg = err.get("message") or err.get("code") or str(data)[:400]
        return {
            "ok": False,
            "endpoint": _SEEDANCE_MODEL,
            "message": f"✗ HTTP {resp.status_code}: {msg}",
        }
    except Exception as e:
        return {
            "ok": False,
            "endpoint": _SEEDANCE_MODEL,
            "message": f"✗ 请求异常: {e}",
        }


if __name__ == "__main__":
    # 直接运行此文件可以快速验证 API 是否通
    import json
    print("=" * 60)
    print("Seedance API 健康检查")
    print("=" * 60)
    result = health_check()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("=" * 60)
    if result["ok"]:
        print(f"\n✓ API 正常, 任务 ID: {result['task_id']}")
        print(f"  → 等待 30-60 秒后用此 task_id 调用 poll_task() 查看结果")
    else:
        print(f"\n✗ API 异常, 请根据上述错误消息排查")
        print(f"  常见原因:")
        print(f"    1. _SEEDANCE_MODEL ({_SEEDANCE_MODEL}) 在火山方舟控制台")
        print(f"       '推理接入点' 页面确实创建且状态为'运行中'")
        print(f"    2. _SEEDANCE_KEY 有效且未过期")
        print(f"    3. 账户余额 > 0 (Seedance 按 token 计费)")