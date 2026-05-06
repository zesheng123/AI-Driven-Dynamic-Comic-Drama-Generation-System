"""
image_service.py — v11 参考图智能化 (修复锁死问题)
═══════════════════════════════════════════════════════
★ v11 核心修复 ★
  1. 【参考图锁死修复】
     - 参考图改为"可选、按需、可降权"策略
     - 全景图仅在 scene_change=False 且当前镜是室内静态场景时使用
     - 角色立绘采用"身份锚点"模式：prompt 前置角色特征描述，立绘仅作辅助
     - 新增 reference_mode 参数：'strong'=锁死 / 'guide'=引导 / 'off'=纯文本
  2. 【风格扩展】
     - STYLE_PREFIX 从 6 种扩展到 16 种
     - STYLE_ZH_PREFIX 同步
     - 允许用户传入 custom_style (自由文本)
  3. 【中文描述增强】
     - _extract_full_appearance 字段词典扩充 3x
     - _gender_tag 新增中文判定
  4. 【降级策略优化】
     - 豆包失败先尝试"去参考图重试"
     - 再失败才降级 NAI
  5. 【多参考图权重】
     - 单参考图策略用于"角色特写 + 连续动作镜"
     - 多参考图策略用于"首镜 + 新场景 + 多角色"
"""
import os, re, io, uuid, base64, hashlib, time, json, requests
from PIL import Image

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── NAI 配置 ──
_NAI_TOKEN = "pst-rPMBT3dptv1AMPBHnJtfXfEpE4RTtZdRgJR9K9erav5TvB1lnYPT9C7uvGw5nc41"
_NAI_PROXY = "http://127.0.0.1:7892"

# ── 豆包 Seedream 配置 ──
_DOUBAO_IMG_API   = "https://ark.cn-beijing.volces.com/api/v3/images/generations"
_DOUBAO_IMG_KEY   = "d61385eb-04a1-4901-bd91-5f2ab153dd11"
_DOUBAO_IMG_MODEL = "doubao-seedream-4-5-251128"

# ── 多参考图上限 ──
_MAX_REF_IMAGES = 5
_MAX_CHAR_REFS  = 3   # 最多取前 3 个角色立绘 (第 4+ 用文字描述)

# ═══════════════════════════════════════════════════════
# 风格系统 (v11 大幅扩展)
# ═══════════════════════════════════════════════════════
STYLE_PREFIX = {
    "日漫":      "anime coloring, anime screenshot, cel shading, clean lineart, vibrant colors",
    "国漫":      "chinese manhua style, detailed lineart, traditional colors",
    "美漫":      "american comic book style, bold outlines, flat color",
    "写实":      "semi-realistic anime, detailed illustration, soft shading",
    "水墨":      "chinese ink wash painting style, monochrome, brush strokes",
    "赛博朋克":  "cyberpunk anime, neon lights, dark futuristic",
    # v11 新增
    "厚涂":      "digital oil painting, thick brush strokes, painterly style, rich color layering",
    "厚涂油画":  "oil painting style, heavy impasto, painterly rendering",
    "水彩":      "watercolor painting style, soft washes, translucent colors, paper texture",
    "像素风":    "pixel art style, 16-bit, limited palette, sharp pixels",
    "古风":      "chinese ancient style illustration, traditional costume, elegant composition",
    "吉卜力":    "studio ghibli style, soft watercolor tones, whimsical, hand-painted feel",
    "新海诚":    "makoto shinkai style, photorealistic backgrounds, vivid sky, atmospheric lighting",
    "漫画分镜":  "manga black and white, screentone, inked lineart, clear panel",
    "3D":        "3d rendered, cinematic lighting, detailed textures, octane render style",
    "低幼童话":  "children book illustration, soft pastel colors, whimsical style, gentle shapes",
    "恐怖漫画":  "horror manga style, high contrast, dark shadows, unsettling atmosphere",
    "写实油画":  "realistic oil painting, classical art, chiaroscuro, detailed brushwork",
}

STYLE_ZH_PREFIX = {
    "日漫":      "日系动漫风格精致插画，二次元赛璐璐上色，清晰流畅线稿，色彩鲜艳饱和",
    "国漫":      "中国动漫风格精致插画，国风配色，细腻线条",
    "美漫":      "美式漫画风格插画，粗犷线条，鲜明对比色",
    "写实":      "半写实动漫风格精致插画，细腻光影渲染",
    "水墨":      "中式水墨风格插画，淡雅色调，笔墨韵味",
    "赛博朋克":  "赛博朋克动漫风格，霓虹灯光，暗色科技背景",
    # v11 新增
    "厚涂":      "厚涂风格数字绘画，油画质感笔触，色彩层次丰富，光影扎实",
    "厚涂油画":  "厚涂油画风格，笔触厚重富有质感，色彩饱满",
    "水彩":      "水彩画风格，柔和渲染，透明感色彩，纸张质感",
    "像素风":    "像素艺术风格，16位复古游戏感，限定调色板，锐利像素",
    "古风":      "中国古风插画，传统服饰与意境，优雅构图",
    "吉卜力":    "吉卜力工作室风格，柔和水彩色调，童话感手绘质感",
    "新海诚":    "新海诚风格，写实质感背景，鲜艳天空，大气光照氛围",
    "漫画分镜":  "黑白漫画分镜风格，网点纸色调，清晰墨线",
    "3D":        "3D渲染风格，电影级光照，精致材质",
    "低幼童话":  "儿童绘本插画风格，柔和粉彩色，温暖童话感",
    "恐怖漫画":  "恐怖漫画风格，高对比度，深邃阴影，不安氛围",
    "写实油画":  "写实油画风格，古典艺术感，明暗对照，精细笔触",
}

_QUALITY = "masterpiece, best quality, very aesthetic"
_BASE_NEG = (
    "lowres, worst quality, bad quality, very displeasing, "
    "bad anatomy, bad hands, extra fingers, extra limbs, deformed, "
    "watermark, signature, text, blurry, nsfw, nude, nipples, "
    "inconsistent clothing, wrong outfit, costume change, "
    "multiple views, comic panel, split screen, "
    "duplicate, duplicated character, twins, clone, cloned figure, "
    "mirrored characters, symmetrical duplicates, multiple copies of same character, "
    "row of identical figures, character repeated in image, crowd of same person"
)

# ── 中文→英文标签词典 (v11 扩展 3x) ──
_ZH_TO_TAG = {
    # 性别
    "女": "1girl", "女生": "1girl", "少女": "1girl", "女孩": "1girl", "小姐": "1girl",
    "男": "1boy", "男生": "1boy", "少年": "1boy", "男孩": "1boy", "先生": "1boy",
    # 发型 发色
    "长发": "long hair", "短发": "short hair", "马尾": "ponytail", "双马尾": "twin tails",
    "盘发": "hair bun", "发髻": "hair bun", "编发": "braided hair", "卷发": "curly hair",
    "直发": "straight hair", "中长发": "medium hair",
    "黑发": "black hair", "棕发": "brown hair", "金发": "blonde hair", "银发": "silver hair",
    "白发": "white hair", "红发": "red hair", "粉发": "pink hair", "蓝发": "blue hair",
    "紫发": "purple hair", "青发": "green hair", "橙发": "orange hair",
    "刘海": "bangs", "齐刘海": "blunt bangs", "斜刘海": "side bangs", "无刘海": "no bangs",
    # 眼睛
    "黑眼": "black eyes", "棕眼": "brown eyes", "蓝眼": "blue eyes", "绿眼": "green eyes",
    "金眼": "gold eyes", "紫眼": "purple eyes", "红眼": "red eyes", "琥珀": "amber eyes",
    # 服装 - 校园
    "校服": "school uniform", "水手服": "sailor school uniform", "JK": "japanese high school uniform",
    "白衬衫": "white shirt", "百褶裙": "pleated skirt", "领带": "necktie", "领结": "bow tie",
    "制服": "uniform",
    # 服装 - 古风
    "汉服": "hanfu", "和服": "kimono", "旗袍": "qipao", "唐装": "tangzhuang",
    "长袍": "robe", "披风": "cloak", "铠甲": "armor", "铁甲": "plate armor",
    "战袍": "battle robe", "道袍": "taoist robe",
    # 服装 - 现代
    "西装": "suit", "西服": "business suit", "外套": "coat", "风衣": "trench coat",
    "羽绒服": "down jacket", "毛衣": "sweater", "卫衣": "hoodie", "T恤": "t-shirt",
    "连衣裙": "dress", "迷你裙": "mini skirt", "长裙": "long skirt",
    "牛仔裤": "jeans", "短裤": "shorts", "短裙": "short skirt",
    # 服装 - 奇幻/特殊
    "法袍": "wizard robe", "斗篷": "cape", "战甲": "battle armor", "圣袍": "holy robe",
    "实验服": "lab coat", "白大褂": "white coat", "护士服": "nurse outfit",
    "女仆装": "maid outfit", "警服": "police uniform", "军装": "military uniform",
    "宇航服": "spacesuit", "机甲": "mecha suit",
    # 配饰
    "过膝袜": "thigh highs", "长筒袜": "stockings", "短袜": "ankle socks",
    "帽子": "hat", "围巾": "scarf", "眼镜": "glasses", "墨镜": "sunglasses",
    "耳环": "earrings", "项链": "necklace", "戒指": "ring", "手套": "gloves",
    "面具": "mask", "头纱": "veil",
    # 体型/特征
    "纤细": "slender", "高挑": "tall", "娇小": "petite", "健硕": "muscular",
    "胖": "chubby", "瘦": "slim", "肌肉": "muscular build",
    "高中生": "high school student", "大学生": "college student",
    # 年龄
    "小孩": "child", "儿童": "child", "少年": "teenager", "青年": "young adult",
    "中年": "middle aged", "老年": "elderly", "老人": "elderly",
    # 特殊种族/属性
    "精灵": "elf", "兽耳": "animal ears", "猫耳": "cat ears", "狗耳": "dog ears",
    "狐耳": "fox ears", "恶魔": "demon", "天使": "angel", "僵尸": "zombie",
    "机器人": "robot", "机械": "mechanical",
}


# ═══════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════
def _is_mostly_chinese(t):
    if not t:
        return False
    cn = len(re.findall(r'[\u4e00-\u9fff]', t))
    total = len(re.sub(r'\s', '', t))
    return total > 0 and cn / total > 0.3


def _zh_desc_to_tags(desc):
    if not desc:
        return ""
    tags = []
    for zh in sorted(_ZH_TO_TAG.keys(), key=len, reverse=True):
        if zh in desc:
            tag = _ZH_TO_TAG[zh]
            if tag not in tags:
                tags.append(tag)
    return ", ".join(tags)


def _clean_en(t):
    return re.sub(r"[\u4e00-\u9fff]+", " ", t).strip() if t else ""


def _trim(t, mx):
    t = _clean_en(t)
    return t[:mx].rsplit(",", 1)[0].strip(", ") if len(t) > mx else t


def _gender_tag(d):
    """v12: 同时支持中英文判定。★ 非人类角色返回空字符串，不强行加 gender tag"""
    if not d:
        return ""  # 没描述就不加标签

    # ★ 非人类关键词检测（中英文）
    # 匹配到任一关键词就不加 1girl/1boy 标签，让 NAI 按原样描述生成
    non_human_zh = [
        "龙", "巨龙", "飞龙", "幽灵", "鬼", "怪兽", "怪物", "机器人", "机甲",
        "外星", "异形", "精灵", "妖精", "兽", "野兽", "狼", "虎", "熊", "狮",
        "鸟", "猫", "狗", "马", "鹿", "蛇", "鱼", "史莱姆", "哥布林", "兽人",
        "恶魔", "魔鬼", "吸血鬼", "骷髅", "僵尸", "丧尸", "木乃伊",
        "神像", "雕像", "AI", "机械体", "载具", "飞船", "机甲", "战车",
    ]
    non_human_en = [
        "dragon", "ghost", "monster", "creature", "robot", "mecha", "mech",
        "alien", "zombie", "skeleton", "slime", "goblin", "orc", "demon",
        "devil", "vampire", "beast", "wolf", "tiger", "bear", "lion",
        "cat", "dog", "horse", "dragon", "serpent", "statue", "spirit",
        "automaton", "android", "cyborg", "ghoul", "wraith",
    ]
    if any(kw in d for kw in non_human_zh):
        return ""
    dl = d.lower()
    if any(kw in dl for kw in non_human_en):
        return ""

    # 人类角色按性别判定
    m_en = sum(1 for w in ["boy", "man", "male", " he ", "his ", "gentleman"] if w in dl)
    f_en = sum(1 for w in ["girl", "woman", "female", "she ", "her ", "lady"] if w in dl)
    m_zh = sum(1 for w in ["男生", "男孩", "少年", "男性", "先生", "男人", "小伙"] if w in d)
    f_zh = sum(1 for w in ["女生", "女孩", "少女", "女性", "小姐", "女人", "姑娘"] if w in d)
    m_score = m_en + m_zh
    f_score = f_en + f_zh

    # ★ 都是 0 分就返回空，不强行猜
    if m_score == 0 and f_score == 0:
        return ""
    return "1boy" if m_score > f_score else "1girl"


def _ensure_char_desc_english(c):
    cached = c.get("_desc_en")
    if cached:
        return cached
    raw = c.get("description", "") or ""
    if not raw:
        return ""
    if _is_mostly_chinese(raw):
        en = _zh_desc_to_tags(raw)
        g = _gender_tag(raw)
        # ★ v12: 只有人类角色才用默认"黑发+校服"补全模板
        # 非人类角色（g为空）直接用词典翻译结果，不强塞人类特征
        if g and en.count(",") < 3:
            p = [g, "black hair"]
            if "刘海" in raw:
                p.append("blunt bangs")
            if any(w in raw for w in ["校服", "衬衫", "制服"]):
                p.append("school uniform")
            if en:
                p.append(en)
            en = ", ".join(p)
        elif not g:
            # 非人类：词典翻译 + 原文英文部分保留
            en_raw_part = re.sub(r"[\u4e00-\u9fff]+", " ", raw).strip()
            en = ", ".join(filter(None, [en, en_raw_part])) if en_raw_part else en
            # 如果翻译结果太少，把原始中文描述也加到备注里（NAI 可识别部分英文词）
            if not en or len(en) < 10:
                en = raw  # 至少保留原始描述
        c["_desc_en"] = en
        return en
    c["_desc_en"] = raw
    return raw


def _extract_identity_anchor(desc):
    """v11 增强: 抓住更多特征词"""
    if not desc:
        return ""
    dl = desc.lower()
    a = []
    # 发色
    hc = re.search(r'\b(black|brown|blonde|silver|white|red|pink|blue|purple|green|orange|grey|gray)\s*hair\b', dl)
    if hc:
        a.append(hc.group(0))
    # 发型
    for ht in ['long hair', 'short hair', 'ponytail', 'twin tails', 'bun', 'braided']:
        if ht in dl:
            a.append(ht)
            break
    if 'bangs' in dl:
        a.append("bangs")
    # 眼睛
    eyes = re.search(r'\b(black|brown|blue|green|red|gold|amber|purple|silver)\s*eyes\b', dl)
    if eyes:
        a.append(eyes.group(0))
    # 主要服装
    for cl in ['school uniform', 'sailor', 'kimono', 'hanfu', 'suit',
               'robe', 'armor', 'cloak', 'dress', 'qipao', 'lab coat']:
        if cl in dl:
            a.append(cl)
            break
    return ", ".join(a[:5])


def _extract_full_appearance(desc: str, name: str = "") -> str:
    """从英文描述中提取中文外貌要点 (给豆包提示用) - v12 扩展
    ★ v12: 非人类角色（龙/机器人/幽灵等）不按人类外貌模板提取，直接用原描述
    """
    if not desc:
        return name or "角色"
    dl = desc.lower()

    # ★ v12: 非人类角色检测 - 直接返回原描述（已翻译成英文）
    non_human_en = ["dragon", "ghost", "monster", "creature", "robot", "mecha", "mech",
                    "alien", "zombie", "skeleton", "slime", "goblin", "orc", "demon",
                    "devil", "vampire", "beast", "wolf", "tiger", "bear", "lion",
                    "cat", "dog", "horse", "serpent", "statue", "spirit",
                    "automaton", "android", "cyborg", "ghoul", "wraith"]
    if any(kw in dl for kw in non_human_en):
        # 直接用 name + 简化描述，不走人类外貌模板
        return f"{name}（{desc[:100]}）" if name else desc[:120]

    parts = []

    # 性别 + 年龄
    if any(w in dl for w in ["female", "girl", "woman", "lady"]):
        if any(w in dl for w in ["young", "teen", "17", "18", "19", "16", "15"]):
            parts.append("年轻女性")
        elif any(w in dl for w in ["child", "little"]):
            parts.append("女童")
        elif any(w in dl for w in ["elderly", "old"]):
            parts.append("老年女性")
        else:
            parts.append("女性")
    elif any(w in dl for w in ["male", "boy", "man", "gentleman"]):
        if any(w in dl for w in ["young", "teen", "17", "18", "19", "16", "15"]):
            parts.append("年轻男性")
        elif any(w in dl for w in ["child", "little"]):
            parts.append("男童")
        elif any(w in dl for w in ["elderly", "old"]):
            parts.append("老年男性")
        else:
            parts.append("男性")

    # 发色
    hair_color_map = {
        "black hair": "黑发", "brown hair": "棕发", "blonde hair": "金发",
        "silver hair": "银发", "white hair": "白发", "red hair": "红发",
        "pink hair": "粉发", "blue hair": "蓝发", "purple hair": "紫发",
        "green hair": "绿发", "orange hair": "橙发",
        "dark hair": "深色头发", "grey hair": "灰发", "gray hair": "灰发",
    }
    for en, zh in hair_color_map.items():
        if en in dl:
            parts.append(zh)
            break

    # 发型长度
    if "long hair" in dl or "long straight hair" in dl:
        parts.append("长发")
    elif "short hair" in dl:
        parts.append("短发")
    elif "medium hair" in dl or "shoulder-length" in dl:
        parts.append("中长发")

    # 发型样式
    if "ponytail" in dl:
        parts.append("马尾")
    elif "twin tail" in dl or "twintails" in dl:
        parts.append("双马尾")
    elif "bun" in dl:
        parts.append("发髻")
    elif "braided" in dl:
        parts.append("编发")
    elif "curly" in dl:
        parts.append("卷发")

    # 刘海
    if "blunt bangs" in dl or "straight bangs" in dl:
        parts.append("齐刘海")
    elif "bangs" in dl:
        parts.append("有刘海")

    # 眼睛颜色
    eye_map = {
        "brown eyes": "棕色眼睛", "black eyes": "黑色眼睛", "blue eyes": "蓝色眼睛",
        "green eyes": "绿色眼睛", "gold eyes": "金色眼睛", "amber eyes": "琥珀色眼睛",
        "purple eyes": "紫色眼睛", "red eyes": "红色眼睛", "grey eyes": "灰色眼睛",
        "silver eyes": "银色眼睛",
    }
    for en, zh in eye_map.items():
        if en in dl:
            parts.append(zh)
            break

    # 服装颜色映射
    color_map = [
        ("white", "白色"), ("black", "黑色"), ("navy", "藏青色"), ("blue", "蓝色"),
        ("red", "红色"), ("green", "绿色"), ("gray", "灰色"), ("grey", "灰色"),
        ("brown", "棕色"), ("orange", "橙色"), ("purple", "紫色"),
        ("yellow", "黄色"), ("pink", "粉色"), ("beige", "米色"), ("gold", "金色"),
        ("silver", "银色"), ("dark", "深色"), ("light", "浅色"),
    ]

    # 上衣 (大扩展)
    top_map = {
        "shirt": "衬衫", "blouse": "上衣", "top": "上衣", "jacket": "夹克",
        "coat": "外套", "uniform": "制服", "hanfu": "汉服", "kimono": "和服",
        "robe": "长袍", "tunic": "长衫", "sweater": "毛衣", "hoodie": "卫衣",
        "t-shirt": "T恤", "blazer": "西装外套", "armor": "铠甲", "cloak": "斗篷",
        "qipao": "旗袍", "suit": "西装", "lab coat": "白大褂",
        "military uniform": "军装", "trench coat": "风衣",
    }
    for color_en, color_zh in color_map:
        for top_en, top_zh in top_map.items():
            if f"{color_en} {top_en}" in dl or f"{color_en}-{top_en}" in dl:
                parts.append(f"{color_zh}{top_zh}")
                break
        else:
            continue
        break
    else:
        # 没找到"颜色+款式"组合，单独找款式
        for top_en, top_zh in top_map.items():
            if top_en in dl:
                parts.append(top_zh)
                break

    # 下装
    bottom_map = {
        "skirt": "裙子", "pleated skirt": "百褶裙", "mini-skirt": "迷你裙",
        "pants": "裤子", "trousers": "长裤", "shorts": "短裤",
        "jeans": "牛仔裤", "slacks": "西裤", "leggings": "紧身裤",
        "dress": "连衣裙", "long skirt": "长裙",
    }
    for color_en, color_zh in color_map:
        for bot_en, bot_zh in bottom_map.items():
            if f"{color_en} {bot_en}" in dl:
                parts.append(f"{color_zh}{bot_zh}")
                break
        else:
            continue
        break

    # 特殊服装/配饰 (扩展)
    special = {
        "school uniform": "校服", "sailor uniform": "水手服",
        "thigh high": "过膝袜", "knee sock": "过膝袜", "knee-high socks": "过膝袜",
        "necktie": "领带", "neckerchief": "领巾",
        "glasses": "戴眼镜", "ribbon": "丝带",
        "loafers": "乐福鞋", "mary jane": "玛丽珍鞋",
        "boots": "靴子", "high heels": "高跟鞋", "sneakers": "运动鞋",
        "hat": "帽子", "scarf": "围巾", "gloves": "手套", "mask": "面具",
        "earrings": "耳环", "necklace": "项链",
    }
    existing = "".join(parts)
    for en, zh in special.items():
        if en in dl and zh not in existing:
            parts.append(zh)

    # 体型
    if "slim" in dl or "slender" in dl:
        parts.append("纤细身材")
    elif "tall" in dl:
        parts.append("高挑")
    elif "muscular" in dl:
        parts.append("健硕")
    elif "petite" in dl:
        parts.append("娇小")

    if not parts:
        return desc[:150]

    return "，".join(parts)


def _save_shot(img_bytes):
    d = os.path.join(BASE_DIR, "static", "shots")
    os.makedirs(d, exist_ok=True)
    fn = f"shot_{uuid.uuid4().hex[:10]}.png"
    with open(os.path.join(d, fn), "wb") as f:
        f.write(img_bytes)
    return f"/static/shots/{fn}"


def _project_seed(pid):
    return int(hashlib.md5(str(pid or "x").encode()).hexdigest()[:8], 16) % (2**31)


def _char_combo_seed(pid, names):
    combo = "+".join(sorted(names)) if names else "_"
    return int(hashlib.md5(f"{pid or 'x'}|{combo}".encode()).hexdigest()[:8], 16) % (2**31)


def _auto_project_id(s, a, g):
    return hashlib.md5(f"{s}|{a}|{g}".encode()).hexdigest()[:12]


# ═══════════════════════════════════════════════════════
# NAI 封装
# ═══════════════════════════════════════════════════════
def _get_nai_client():
    """获取 NAI 客户端 — 不污染全局 os.environ
    ★ v12 修复:
      原版本会永久修改 os.environ 的 HTTPS_PROXY/HTTP_PROXY,
      一旦代理关闭, 所有后续 requests 调用都会连到死代理并挂掉。
      现在改为: 只设置 NOVELAI_API_KEY, 代理通过 httpx/requests 的
      参数级方式传入 (下面 _nai_generate 用 contextmanager 临时启用)。
    """
    os.environ["NOVELAI_API_KEY"] = _NAI_TOKEN
    from novelai import NovelAI
    return NovelAI()


from contextlib import contextmanager

@contextmanager
def _temporary_proxy_env(proxy_url):
    """临时把 HTTP(S)_PROXY 注入 os.environ, with 块退出后自动恢复。
    这样既能让 novelai 库(内部走 httpx, 会读 env)用上代理,
    又不会污染后续的 requests 调用。
    """
    if not proxy_url:
        yield
        return
    keys = ["HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"]
    backup = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ[k] = proxy_url
        yield
    finally:
        for k, v in backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _nai_generate(prompt, negative_prompt="", width=1216, height=832,
                  seed=0, char_ref_images=None, steps=28, scale=6.0,
                  sampler="k_euler", nai_characters=None):
    """NAI v4.5 生成 — 支持 Character Reference + Multi-Character"""
    from novelai.types import GenerateImageParams, CharacterReference, Character
    kw = dict(
        prompt=prompt, model="nai-diffusion-4-5-full",
        size=(width, height), negative_prompt=negative_prompt or _BASE_NEG,
        quality=True, steps=steps, scale=scale, sampler=sampler,
        seed=seed if seed else 0,
    )
    if char_ref_images:
        refs = []
        for p in char_ref_images:
            ap = os.path.join(BASE_DIR, p.lstrip("/")) if isinstance(p, str) and p.startswith("/") else p
            if isinstance(ap, str) and os.path.exists(ap):
                refs.append(CharacterReference(image=ap, type="character", fidelity=0.6, strength=0.6))
        if refs:
            kw["character_references"] = refs
    if nai_characters:
        kw["characters"] = [Character(prompt=nc["prompt"], position=nc.get("position", "C3"),
                                       enabled=True) for nc in nai_characters]
    try:
        # ★ v12: 临时启用代理 (仅在 with 块内有效, 退出后自动恢复)
        with _temporary_proxy_env(_NAI_PROXY):
            client = _get_nai_client()
            params = GenerateImageParams(**kw)
            try:
                cost = params.calculate_anlas(is_opus=True)
            except Exception:
                cost = 5 * len(char_ref_images or [])
            print(f"[NAI] 生成中 cost≈{cost} Anlas {width}x{height}")
            images = client.image.generate(params)
        if images:
            buf = io.BytesIO()
            images[0].save(buf, format="PNG")
            url = _save_shot(buf.getvalue())
            print(f"[NAI] ✓ {url}")
            return {"success": True, "image_url": url, "url": url}
        return {"success": False, "message": "NAI 返回空"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


# ═══════════════════════════════════════════════════════
# 豆包 Seedream 4.5
# ═══════════════════════════════════════════════════════
def _img_to_base64_uri(url_or_path, max_size=1024):
    if not url_or_path:
        return None
    local = (os.path.join(BASE_DIR, url_or_path.lstrip("/"))
             if url_or_path.startswith("/") else url_or_path)
    if not isinstance(local, str) or not os.path.exists(local):
        return None
    try:
        img = Image.open(local).convert("RGB")
        if max(img.size) > max_size:
            img.thumbnail((max_size, max_size), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode()
        return f"data:image/png;base64,{b64}"
    except Exception as e:
        print(f"[豆包] 图片转base64失败: {e}")
        return None


def _direct_request(method, url, **kwargs):
    """直连请求：忽略系统/环境代理，避免本地代理关闭时 requests 仍走代理。"""
    session = requests.Session()
    session.trust_env = False
    try:
        return session.request(method=method, url=url, **kwargs)
    finally:
        session.close()


def _doubao_generate(prompt, ref_image_urls=None, size="16:9"):
    """豆包 Seedream 4.5"""
    _SIZE_MAP = {
        "16:9": "2560x1440",
        "9:16": "1440x2560",
        "4:3":  "2048x1536",
        "3:2":  "2400x1600",
        "2:3":  "1600x2400",
        "1:1":  "1920x1920",
        "2K":   "2048x2048",
        "4K":   "4096x4096",
    }
    actual_size = _SIZE_MAP.get(size, size)

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {_DOUBAO_IMG_KEY}",
    }
    payload = {
        "model": _DOUBAO_IMG_MODEL,
        "prompt": prompt,
        "response_format": "url",
        "size": actual_size,
        "watermark": False,
        "sequential_image_generation": "disabled",
    }

    if ref_image_urls:
        image_list = []
        for url in ref_image_urls[:_MAX_REF_IMAGES]:
            if not url:
                continue
            if url.startswith("/") or (not url.startswith("http")):
                b64uri = _img_to_base64_uri(url)
                if b64uri:
                    image_list.append(b64uri)
                    print(f"[豆包] ✓ 参考图(本地): {os.path.basename(url)}")
            elif url.startswith("http"):
                image_list.append(url)
                print(f"[豆包] ✓ 参考图(URL): {url[:60]}...")
        if image_list:
            payload["image"] = image_list[0] if len(image_list) == 1 else image_list
            print(f"[豆包] ★ 总参考图: {len(image_list)} 张")

    print(f"[豆包] 提交 prompt={len(prompt)}字 refs={len((ref_image_urls or []))} size={size}→{actual_size}")
    try:
        resp = _direct_request("POST", _DOUBAO_IMG_API, headers=headers, json=payload, timeout=120)
        data = resp.json()
        print(f"[豆包] HTTP {resp.status_code}")

        if resp.status_code == 200 and data.get("data"):
            img_url = data["data"][0].get("url", "")
            if img_url:
                img_resp = _direct_request("GET", img_url, timeout=30)
                if img_resp.status_code == 200:
                    local_url = _save_shot(img_resp.content)
                    print(f"[豆包] ✓ {local_url}")
                    return {"success": True, "image_url": local_url, "url": local_url}
            b64_data = data["data"][0].get("b64_json", "")
            if b64_data:
                local_url = _save_shot(base64.b64decode(b64_data))
                print(f"[豆包] ✓ base64 {local_url}")
                return {"success": True, "image_url": local_url, "url": local_url}

        err_msg = ""
        if "error" in data:
            err_msg = str(data["error"])
        elif "message" in data:
            err_msg = data["message"]
        else:
            err_msg = str(data)[:200]
        print(f"[豆包] 失败 ({resp.status_code}): {err_msg[:200]}")

        # ★ v11: 分级重试 — 先减少参考图，再完全去掉
        if ref_image_urls and "image" in payload:
            # 重试 1: 只保留第一张参考图
            if isinstance(payload["image"], list) and len(payload["image"]) > 1:
                print(f"[豆包] 重试1: 只保留首张参考图...")
                payload_r1 = dict(payload)
                payload_r1["image"] = payload["image"][0]
                resp_r1 = _direct_request("POST", _DOUBAO_IMG_API, headers=headers, json=payload_r1, timeout=120)
                data_r1 = resp_r1.json()
                if resp_r1.status_code == 200 and data_r1.get("data"):
                    img_url = data_r1["data"][0].get("url", "")
                    if img_url:
                        img_resp = _direct_request("GET", img_url, timeout=30)
                        if img_resp.status_code == 200:
                            local_url = _save_shot(img_resp.content)
                            print(f"[豆包] ✓ 重试1成功 {local_url}")
                            return {"success": True, "image_url": local_url, "url": local_url}

            # 重试 2: 去掉所有参考图
            print(f"[豆包] 重试2: 去掉参考图...")
            payload_r2 = {k: v for k, v in payload.items() if k != "image"}
            resp_r2 = _direct_request("POST", _DOUBAO_IMG_API, headers=headers, json=payload_r2, timeout=120)
            data_r2 = resp_r2.json()
            if resp_r2.status_code == 200 and data_r2.get("data"):
                img_url = data_r2["data"][0].get("url", "")
                if img_url:
                    img_resp = _direct_request("GET", img_url, timeout=30)
                    if img_resp.status_code == 200:
                        local_url = _save_shot(img_resp.content)
                        print(f"[豆包] ✓ 重试2成功(无参考图) {local_url}")
                        return {"success": True, "image_url": local_url, "url": local_url}

        return {"success": False, "message": f"豆包: {err_msg[:80]}"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


# ═══════════════════════════════════════════════════════
# 角色三视图
# ═══════════════════════════════════════════════════════
def _generate_character_view_with_fallback(prompt, neg, seed, view_name, ref_images=None):
    """角色视图生成：优先 NAI，失败自动降级豆包。"""
    ref_images = ref_images or []

    nai_result = _nai_generate(
        prompt=prompt,
        negative_prompt=neg,
        width=832,
        height=1216,
        seed=seed,
        char_ref_images=ref_images,
        scale=6.0,
    )
    if nai_result.get("success"):
        print(f"[角色] {view_name} 使用 NAI 成功")
        return nai_result

    nai_msg = nai_result.get("message", "")
    print(f"[角色] {view_name} NAI 失败，降级豆包: {nai_msg}")

    db_result = _doubao_generate(
        prompt=prompt,
        ref_image_urls=ref_images,
        size="2:3",
    )
    if db_result.get("success"):
        print(f"[角色] {view_name} 使用豆包成功")
        return db_result

    db_msg = db_result.get("message", "")
    return {
        "success": False,
        "message": f"NAI失败: {nai_msg}；豆包失败: {db_msg}",
    }


def generate_character_views(char_name, description, art_style="日漫", front_url=None):
    prefix = STYLE_PREFIX.get(art_style, STYLE_PREFIX["日漫"])
    raw = f"{char_name} {description}".lower()
    gender_tag = _gender_tag(char_name + " " + description)  # ★ 可能返回空字符串（非人类）
    gender = gender_tag
    is_human = bool(gender)  # ★ 判断是否人类角色

    if _is_mostly_chinese(description):
        desc_en = _zh_desc_to_tags(description)
        # ★ v12: 只有人类角色才用默认"黑发+校服"补全
        if is_human and desc_en.count(",") < 3:
            p = [gender, "black hair"]
            if "刘海" in description:
                p.append("blunt bangs")
            if any(w in description for w in ["校服", "衬衫", "制服"]):
                p.append("school uniform")
            if desc_en:
                p.append(desc_en)
            desc_en = ", ".join(p)
        elif not is_human:
            # 非人类角色：只用词典翻译 + 保留原描述中的英文部分
            en_raw_part = re.sub(r"[一-鿿]+", " ", description).strip()
            desc_en = ", ".join(filter(None, [desc_en, en_raw_part])) if en_raw_part else desc_en
            if not desc_en or len(desc_en) < 10:
                desc_en = description  # 保底用原文
        if gender and gender not in desc_en:
            desc_en = f"{gender}, {desc_en}"
    else:
        desc_en = description
        if gender and gender not in desc_en.lower():
            desc_en = f"{gender}, {desc_en}"
    desc_en = re.sub(r"[一-鿿]+", " ", desc_en).strip()

    neg = _BASE_NEG + ", multiple characters, complex background"
    if is_human:
        neg += (", 1boy, male" if gender == "1girl" else ", 1girl, female")

    identity = _extract_identity_anchor(desc_en)

    seed = int(hashlib.md5(f"char_{char_name}_{desc_en[:50]}_{int(time.time())}".encode()).hexdigest()[:8], 16) % (2**31)
    print(f"[角色] {char_name}: gender={gender or 'non-human'}, seed={seed}")

    gender_part = f"{gender}, " if gender else ""
    identity_part = f"{identity}, " if identity else ""
    fp = f"{prefix}, {gender_part}{identity_part}{desc_en}, full body, front view, standing, simple white background, solo focus, {_QUALITY}"
    fr = _generate_character_view_with_fallback(fp, neg, seed, "front")
    views = {}
    if fr.get("success"):
        views["front"] = fr["url"]
    else:
        return {"success": False, "message": "正面立绘失败: " + fr.get("message", "")}

    ref = [front_url] if front_url else [views["front"]]
    for vn, vp in [
        ("side", f"{prefix}, {gender_part}{identity_part}{desc_en}, full body, profile, from side, standing, simple white background, {_QUALITY}"),
        ("face", f"{prefix}, {gender_part}{identity_part}{desc_en}, portrait, close-up, face focus, upper body, simple white background, detailed face, {_QUALITY}"),
    ]:
        r = _generate_character_view_with_fallback(
            prompt=vp,
            neg=neg,
            seed=seed + (1 if vn == "side" else 2),
            view_name=vn,
            ref_images=ref,
        )
        views[vn] = r["url"] if r.get("success") else ""
    return {"success": True, "image_url": views["front"], "views": views}


def generate_character_image(cn, desc, style="日漫"):
    r = generate_character_views(cn, desc, style)
    return {"success": True, "image_url": r.get("image_url", "")} if r.get("success") else r


# ═══════════════════════════════════════════════════════
# 姿势映射
# ═══════════════════════════════════════════════════════
_POSE_MAP = {
    "standing": "standing", "walking": "walking", "sitting": "sitting down",
    "kneeling": "kneeling", "looking_down": "looking down", "looking_up": "looking up",
    "turning_back": "looking over shoulder", "holding_object": "holding object",
    "covering_face": "covering face with hands", "two_people_facing": "facing each other",
    "pointing": "pointing", "running": "running", "jumping": "jumping",
    "lying_down": "lying down", "crouching": "crouching", "hugging": "hugging",
}


def get_pose_sd_tags(ph):
    if not ph:
        return ""
    if isinstance(ph, list):
        ph = ", ".join(ph)
    return ", ".join(dict.fromkeys(
        _POSE_MAP.get(h.strip(), h.strip().replace("_", " "))
        for h in re.split(r"[,\s]+", ph.lower()) if h.strip()
    ))


def _strip_character_refs(text, names):
    if not text or not names:
        return text
    for n in names:
        text = text.replace(n, "")
    return re.sub(r',\s*,+', ',', re.sub(r'\s+', ' ', text)).strip(', ')


# ═══════════════════════════════════════════════════════
# ★★★ v11 核心修复：智能参考图收集 ★★★
# ═══════════════════════════════════════════════════════
def _should_use_panorama(shot, reference_mode='guide'):
    """v11 新增: 智能判断是否应该使用全景图作为参考

    全景图仅适用于:
      - 场景未切换 (scene_change=False 或未设置)
      - 室内/固定环境
      - reference_mode != 'off'

    不适用于:
      - 明确 scene_change=True
      - 场景描述中含有"切换""新场景""路上""回到"等词
      - 户外大幅运动场景
    """
    if reference_mode == 'off':
        return False
    if shot.get('scene_change'):
        return False
    scene = (shot.get('scene_description', '') or '') + ' ' + (shot.get('action_zh', '') or '')
    # 场景切换关键词
    change_kws = ['切换', '回到', '来到', '走进', '走出', '飞向', '穿越',
                  '远处', '另一处', '新场景', '场景转']
    if any(kw in scene for kw in change_kws):
        return False
    return True


def _collect_reference_images(shot, out_chars, prev_shot_image=None,
                                all_chars=None, panorama_views=None,
                                reference_mode='guide',
                                max_total=_MAX_REF_IMAGES,
                                is_first_shot=False):
    """v11 大幅重构: 智能参考图策略

    Args:
        shot: 当前分镜数据
        out_chars: 当前镜出场角色
        prev_shot_image: 上一镜图
        panorama_views: 全景图视图
        reference_mode:
          'strong' - 强引用(给角色特写/近景,锁定外貌)
          'guide'  - 软引导(默认,只用必要的参考图)
          'off'    - 关闭(完全靠prompt文本)
        is_first_shot: 是否首镜(首镜不参考上一镜)

    Returns:
        list: 参考图URL列表
    """
    if reference_mode == 'off':
        return []

    refs = []
    shot_type = shot.get('shot_type', '') or ''
    scene_change = shot.get('scene_change', False)

    # ─────────────────────────────────────
    # 1. 角色立绘 (★ v14.5 重构: 一致性优先)
    # ─────────────────────────────────────
    # ★ v14.5 核心变更:
    #   之前 guide 模式只在特写/近景/中景传立绘, 远景/全景跳过
    #   → 导致远景镜头角色造型完全不一致(衣服变色/体型变化)
    #
    #   现在改为: 只要有角色出场就传立绘(无论景别)
    #   - 'strong' - 全部参考
    #   - 'guide'  - 有角色就传立绘(★ 远景也传, 因为角色造型/配色需要锚定)
    #   - 'off'    - 不传
    #   - "纯环境"镜头(characters_in_shot 为空) → 不传立绘(这才是真正不需要的)
    need_char_ref = False
    if reference_mode == 'strong':
        need_char_ref = True
    elif reference_mode == 'guide':
        # ★ v14.5: 只要有角色就传立绘, 不再按景别过滤
        # 远景虽然面部看不清, 但整体造型/配色/体型需要立绘锚定
        if out_chars:
            need_char_ref = True

    if need_char_ref:
        # 最多取前 _MAX_CHAR_REFS 个角色的立绘（剩下的靠文字）
        char_count = 0
        for c in (out_chars or []):
            if char_count >= _MAX_CHAR_REFS:
                break
            views = c.get('views') or {}
            front = views.get('front', '') or c.get('image_url', '')
            if front and front not in refs:
                refs.append(front)
                char_count += 1
                print(f"[参考图] + 角色立绘: {c.get('name','?')}")
            if len(refs) >= max_total:
                return refs

    # ─────────────────────────────────────
    # 2. 上一镜图 (场景连续性) — v14.5 再调整
    # ─────────────────────────────────────
    # ★ v14.5: 恢复远景的上一镜图参考
    #   之前砍太狠(远景+大景别都跳过), 导致连续镜头角色造型不一致
    #   现在只在以下情况跳过:
    #   - scene_change=True
    #   - 首镜
    #   - 特写(让构图自由)
    #   - 无角色的纯环境镜(全景图就够)
    if (prev_shot_image
            and not scene_change
            and not is_first_shot
            and '特写' not in shot_type
            and out_chars                    # 有角色时才参考
            ):
        if prev_shot_image not in refs:
            refs.append(prev_shot_image)
            print(f"[参考图] + 上一镜图: {os.path.basename(prev_shot_image)}")
            if len(refs) >= max_total:
                return refs

    # ─────────────────────────────────────
    # 3. 全景图 (★条件性, 不强塞)
    # ─────────────────────────────────────
    # 全景图仅在：
    #   - 场景未切换
    #   - 当前是全景/远景/环境类镜头
    #   - reference_mode != 'off'
    if _should_use_panorama(shot, reference_mode) and panorama_views:
        is_wide = any(k in shot_type for k in ['全景', '远景', '环境'])
        if is_wide or is_first_shot:
            pv_list = panorama_views if isinstance(panorama_views, list) else []
            if pv_list:
                pv = pv_list[0]
                pv_url = pv.get('url', '') if isinstance(pv, dict) else pv
                if pv_url and pv_url not in refs:
                    refs.append(pv_url)
                    print(f"[参考图] + 全景视图 (场景建立)")

    return refs


def _extract_outfit_keywords(desc: str) -> list:
    """★ v12 新增: 从角色描述里抽出服装/配饰关键词, 用于 prompt 锁定
    返回最多 3 个关键词 (如 ['黑色学生校服', '红色领结', '蓝色百褶裙'])
    """
    if not desc:
        return []
    # 先找带颜色+服装的组合
    # 常见颜色词
    colors = r'(?:白|黑|红|橙|黄|绿|青|蓝|紫|粉|灰|棕|金|银|藏青|米|卡其|墨|绛|赤|暗|亮|深|浅)色?'
    # 常见服装/配饰
    clothes = r'(?:衬衫|衬衣|T恤|卫衣|毛衣|外套|夹克|风衣|羽绒服|大衣|披风|斗篷|校服|水手服|制服|西装|西服|和服|汉服|旗袍|唐装|长袍|道袍|战袍|战甲|铠甲|盔甲|法袍|圣袍|斗篷|长裙|连衣裙|短裙|迷你裙|百褶裙|旗袍|裤子|牛仔裤|短裤|工装裤|靴子|靴|鞋|袜|领带|领结|围巾|帽子|头巾|头饰|发饰|眼镜|墨镜|手套|腰带|裙子|上衣|马甲|背心|披肩)'
    pattern = rf'({colors}[的之]?\s*{clothes}|{clothes})'
    found = re.findall(pattern, desc)
    # 去重、保留前 3
    seen = []
    for f in found:
        f2 = f.strip()
        if f2 and f2 not in seen:
            seen.append(f2)
        if len(seen) >= 3:
            break
    return seen


def _build_seedream_prompt(shot, out_chars, art_style="日漫",
                            scene_spec_zh="", global_tone="",
                            use_ref_images=True):
    """★ v12 修复: 构建 Seedream 4.5 的自然语言 prompt

    v12 新增修复:
      1. 参考图模式下强制写明【每个角色的衣着关键词】, 避免衣服错位/串色
      2. 强调【背景不得为纯色/留白】, 必须按 scene_description 生成
      3. 场景描述提到前半部分, 避免模型注意力衰减到角色细节

    v11 原修复:
      - 使用参考图时, prompt 重点描述变化(动作/表情/场景调整)
      - 不使用参考图时, 完整描述外貌
    """
    # 优先使用 LLM 的 jimeng_ref_prompt
    jimeng_prompt = shot.get('jimeng_ref_prompt', '').strip()
    if jimeng_prompt and len(jimeng_prompt) >= 60:
        style_prefix = STYLE_ZH_PREFIX.get(art_style) or (
            f"{art_style}风格精致插画" if art_style else STYLE_ZH_PREFIX["日漫"]
        )

        has_style = any(k in jimeng_prompt[:40] for k in
                        ['日系动漫', '国漫', '美漫', '动漫', '插画', '画风', '风格'])
        if has_style:
            prompt = jimeng_prompt
        else:
            prompt = f"{style_prefix}，{jimeng_prompt}"

        # ★ v14.6 关键修复：主体复制（subject duplication）问题
        # Seedream 4.5 看到"居中立绘参考图"时，会把立绘的对称感复制到画面
        # 典型症状：1 个角色变 6-7 个横排；1 条龙变对称双胞胎
        # 解决方案：在 prompt 最前面明确数量 + 禁止复制
        if use_ref_images and out_chars:
            subject_count_parts = []
            for c in out_chars:
                cname = c.get('name', '')
                if cname:
                    subject_count_parts.append(f"画面中只有1个{cname}")
            if subject_count_parts:
                # 前置数量约束：放在 prompt 最前面让模型优先看到
                count_directive = (
                    "，".join(subject_count_parts)
                    + "，绝对禁止复制、镜像或多个相同角色并排出现"
                    + "，参考图仅用于外观识别不得复制其对称构图"
                )
                # 单角色的特殊强化：明确单个主体占据画面
                if len(out_chars) == 1:
                    cname = out_chars[0].get('name', '')
                    count_directive += (
                        f"。{cname}作为唯一主体自然地处于画面中，"
                        f"不得在画面中出现第二个、第三个或任何重复的{cname}"
                    )
                prompt = count_directive + "。" + prompt

        # ★ v11/v12: 使用参考图时追加强约束
        if use_ref_images:
            action_zh = shot.get('action_zh', '') or shot.get('action', '')
            shot_type = shot.get('shot_type', '')
            emphasis_parts = []
            if action_zh:
                emphasis_parts.append(f"请严格按照描述展现动作：{action_zh}")
            if shot_type:
                emphasis_parts.append(f"使用{shot_type}构图")
            if shot.get('scene_change'):
                emphasis_parts.append("这是新场景，背景完全不同于之前")

            # ★ v12 新增: 逐个角色锁衣服, 防止多角色衣服错位
            if out_chars:
                outfit_locks = []
                for c in out_chars:
                    cname = c.get('name', '')
                    cdesc = c.get('description', '') or ''
                    outfits = _extract_outfit_keywords(cdesc)
                    if outfits:
                        outfit_locks.append(
                            f"{cname}严格穿着{'/'.join(outfits)}"
                        )
                if outfit_locks:
                    emphasis_parts.append('，'.join(outfit_locks))

            # ★ v14 新增: 多角色朝向硬约束
            # 问题: Seedream 4.5 在多角色场景里经常让角色"各自朝前",
            # 不会自动理解"少年对着龙"这种互动关系。这里强制注入朝向。
            if len(out_chars) >= 2:
                # 默认左右分站, 左角色侧身朝右, 右角色侧身朝左
                names = [c.get('name', '') for c in out_chars]
                action_text = shot.get('action_zh', '') + shot.get('scene_description', '')
                # 检测 prompt/剧情中是否有对抗性词
                is_confront = any(kw in action_text for kw in
                    ['对峙', '对视', '对战', '交战', '厮杀', '怒吼', '逼近', '冲向', '扑向',
                     '举剑', '挥剑', '握剑', '交锋', '迎战', '对抗', '盘旋', '咆哮'])
                # 检测是否是并肩/同向
                is_same_side = any(kw in action_text for kw in
                    ['并肩', '同行', '并列', '一起', '共同', '结伴', '相拥', '拥抱'])

                if is_same_side:
                    facing = f"{'、'.join(names)}并肩朝向同一方向"
                elif is_confront or len(out_chars) == 2:
                    a, b = names[0], names[1] if len(names) > 1 else ''
                    facing = (
                        f"★{a}在画面左侧侧身朝右面对{b}，"
                        f"{b}在画面右侧侧身朝左面对{a}，"
                        f"视线交汇，禁止各自朝向镜头★"
                    )
                else:
                    facing = (
                        f"{'、'.join(names)}彼此面对，视线交汇，禁止背对或各自朝前"
                    )
                emphasis_parts.append(facing)

            # ★ v14.5: 背景锁定（精简版，减少 prompt 长度）
            scene_zh = shot.get('scene_description', '') or scene_spec_zh
            if scene_zh:
                emphasis_parts.append(f"背景：{scene_zh[:50]}，禁止白色/纯色背景")
            else:
                emphasis_parts.append("禁止白色/纯色背景")

            if emphasis_parts:
                prompt += "，" + "，".join(emphasis_parts)

        if '高质量' not in prompt and '质量' not in prompt:
            prompt += "，画面精致，色彩丰富，完整背景，无留白，高清高质量"

        if len(out_chars) > 1:
            names = "、".join(c.get('name', '') for c in out_chars)
            if '左' not in prompt and '右' not in prompt:
                prompt += f"，画面中{names}外貌和服装明显不同，每个角色特征清晰可辨"

        return prompt

    # ── Fallback: 系统自动构建 ──
    style_prefix = STYLE_ZH_PREFIX.get(art_style) or (
        f"{art_style}风格精致插画" if art_style else STYLE_ZH_PREFIX["日漫"]
    )
    parts = [style_prefix]

    # ★ v12: 场景放在风格之后、角色之前, 保证注意力覆盖
    scene_zh = shot.get('scene_description', '') or scene_spec_zh
    if scene_zh:
        parts.append(f"场景环境：{scene_zh}")

    shot_type = shot.get('shot_type', '')
    comp_map = {
        "特写": "脸部特写构图，面部占画面主体",
        "近景": "上半身近景构图，面部清晰",
        "中景": "中景构图，腰部以上可见",
        "远景": "远景全身构图，环境细节丰富",
        "全景": "宽幅全景构图，完整展示场景",
        "环境": "场景环境全景，无人物，背景细节极丰富",
    }
    for k, v in comp_map.items():
        if k in shot_type:
            parts.append(v)
            break
    else:
        parts.append("中景构图")

    if global_tone:
        parts.append(global_tone)

    if len(out_chars) > 1:
        names = "、".join(c.get('name', '') for c in out_chars)
        count_word = "两人" if len(out_chars) == 2 else f"{len(out_chars)}人"
        parts.append(f"画面中有{names}共{count_word}，外貌和服装明显不同")
        parts.append(f"画面中这{count_word}各只有1个，绝对禁止复制或镜像")
    elif len(out_chars) == 1:
        cname = out_chars[0].get('name', '')
        parts.append(f"画面中只有1个{cname}，绝对禁止复制、镜像或多个相同角色并排出现")

    action_desc = shot.get('action_zh', '') or shot.get('action', '')
    if action_desc:
        parts.append(f"画面动作：{action_desc}")

    emotion = shot.get('emotion', '')
    if emotion:
        parts.append(f"人物情绪：{emotion}")

    positions = ["画面左侧", "画面右侧", "画面中间", "画面前方", "画面后方"]
    for i, c in enumerate(out_chars):
        cname = c.get('name', '')
        cdesc = c.get('description', '') or ""
        appearance = _extract_full_appearance(cdesc, cname)
        # ★ v12: 强调服装锁定
        outfits = _extract_outfit_keywords(cdesc)
        outfit_note = f"，严格穿着{'/'.join(outfits)}" if outfits else ""
        pos = positions[i] if i < len(positions) else ""
        if len(out_chars) > 1:
            parts.append(f"{pos}是{cname}（{appearance}{outfit_note}）")
        else:
            parts.append(f"主角是{cname}（{appearance}{outfit_note}）")

    if not out_chars:
        parts.append("无人物，展示场景环境细节")

    # ★ v12: 背景强约束放末尾(作为强化)
    parts.append("背景环境必须完整详细，绝不使用白色或纯色留白")
    parts.append("画面精致，色彩丰富，完整背景，高清高质量")

    return "，".join(p for p in parts if p)


def generate_storyboard_image(
    shot, char_refs=None, art_style="日漫", style=None,
    global_tone="", scene_spec="", scene_spec_zh="",
    scene_views=None, panorama_views=None, all_chars=None,
    project_id=None, prev_shot_image=None,
    engine="doubao", quality="16:9",
    reference_mode='guide',  # v11 新增: 'strong'/'guide'/'off'
    is_first_shot=False,      # v11 新增: 是否首镜
):
    """生成单张分镜图 (v11)
    engine: "doubao" / "nai"
    reference_mode: 参考图使用策略
        'strong' - 强引用, 角色立绘全部参考 (适合高一致性需求)
        'guide'  - 软引导 (默认), 智能选择参考图
        'off'    - 关闭, 完全靠 prompt (适合大幅场景变化)
    """
    if style:
        art_style = style
    chars = char_refs or []
    all_c = all_chars or chars

    chars_in = shot.get("characters_in_shot") or []
    if isinstance(chars_in, str):
        chars_in = [chars_in]

    out_chars = [c for c in chars if c.get("name") in chars_in]
    has_chars = len(out_chars) > 0

    # ══════════════════════════════════
    # ★ 豆包 Seedream 分镜 (默认)
    # ══════════════════════════════════
    if engine == "doubao":
        # v11: 智能收集参考图
        ref_images = _collect_reference_images(
            shot=shot,
            out_chars=out_chars,
            prev_shot_image=prev_shot_image,
            all_chars=all_c,
            panorama_views=panorama_views,
            reference_mode=reference_mode,
            is_first_shot=is_first_shot,
        )

        # v11: prompt 构建根据是否有参考图调整
        prompt = _build_seedream_prompt(
            shot, out_chars, art_style, scene_spec_zh, global_tone,
            use_ref_images=bool(ref_images),
        )

        print(f"\n[分镜] ┌─ 豆包 Seedream 4.5 (v11) ──────────")
        print(f"[分镜] │ shot_type: {shot.get('shot_type','')}")
        print(f"[分镜] │ scene_change: {shot.get('scene_change', False)}")
        print(f"[分镜] │ reference_mode: {reference_mode}")
        print(f"[分镜] │ chars: {[c.get('name') for c in out_chars]}")
        print(f"[分镜] │ refs: {len(ref_images)} 张参考图")
        print(f"[分镜] │ prompt({len(prompt)}字): {prompt[:180]}...")
        print(f"[分镜] └──────────────────────────────\n")

        result = _doubao_generate(
            prompt=prompt,
            ref_image_urls=ref_images if ref_images else None,
            size=quality,
        )
        if result.get("success"):
            return result
        print(f"[分镜] 豆包失败, 降级到 NAI: {result.get('message','')}")

    # ══════════════════════════════════
    # NAI 分镜 (降级或用户选择)
    # ══════════════════════════════════
    prefix = STYLE_PREFIX.get(art_style) or "anime illustration"
    parts = [prefix]
    if scene_spec:
        parts.append(_trim(scene_spec, 150))
    elif shot.get('scene_description'):
        parts.append(_clean_en(shot['scene_description']))

    action = shot.get('action', '')
    if action:
        parts.append(_clean_en(action))

    emotion = shot.get('emotion', '')
    if emotion:
        parts.append(f"{emotion} expression")

    if not has_chars:
        parts.append("no humans, scenery only")

    parts.append(_QUALITY)
    prompt = ", ".join(p for p in parts if p and p.strip())

    neg = _BASE_NEG + (", person, human, face" if not has_chars else "")

    if not project_id:
        project_id = _auto_project_id(scene_spec or "", art_style, global_tone or "")
    seed = (_char_combo_seed(project_id, [c.get("name", "") for c in out_chars])
            if has_chars else _project_seed(project_id))

    # NAI 的参考图使用策略保持原样
    nai_refs = _collect_reference_images(
        shot=shot,
        out_chars=out_chars,
        prev_shot_image=prev_shot_image,
        all_chars=all_c,
        reference_mode=reference_mode,
        is_first_shot=is_first_shot,
    )

    nai_chars = []
    for c in out_chars:
        de = _ensure_char_desc_english(c)
        ident = _extract_identity_anchor(de)
        g = _gender_tag(de)
        nai_chars.append({"prompt": f"{g}, {ident}, {de}", "position": "C3"})

    return _nai_generate(
        prompt=prompt, negative_prompt=neg, width=1216, height=832,
        seed=seed, char_ref_images=nai_refs or None,
        nai_characters=nai_chars if nai_chars else None,
        scale=6.0, sampler="k_euler",
    )


# ═══════════════════════════════════════════════════════
# Ken Burns (保留原有功能)
# ═══════════════════════════════════════════════════════
def apply_ken_burns(img_path, duration=3.0, fps=24, out_path=None):
    import numpy as np
    from moviepy import VideoClip
    img = Image.open(img_path).convert("RGB")
    W, H = img.size
    arr = np.array(img)

    def make_frame(t):
        p = t / duration
        s = 1.0 + 0.08 * p
        nw, nh = int(W / s), int(H / s)
        x0, y0 = (W - nw) // 2, (H - nh) // 2
        return np.array(Image.fromarray(arr[y0:y0 + nh, x0:x0 + nw]).resize((W, H), Image.LANCZOS))

    clip = VideoClip(make_frame, duration=duration)
    if not out_path:
        out_path = os.path.join(BASE_DIR, "static", "videos", f"kb_{uuid.uuid4().hex[:8]}.mp4")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    clip.write_videofile(out_path, fps=fps, codec="libx264", audio=False, logger=None)
    return out_path