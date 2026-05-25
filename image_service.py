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
     - 豆包失败直接返回错误，不再降级 旧图像引擎
  5. 【多参考图权重】
     - 单参考图策略用于"角色特写 + 连续动作镜"
     - 多参考图策略用于"首镜 + 新场景 + 多角色"
"""
import os, re, io, uuid, base64, hashlib, time, json, requests
from PIL import Image

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── 旧图像引擎 配置（已过期，主流程不再调用）──
_旧图像引擎_TOKEN = os.getenv("旧图像引擎_TOKEN", "")
_旧图像引擎_PROXY = os.getenv("旧图像引擎_PROXY", "http://127.0.0.1:7892")

# ── 豆包 Seedream 图片生成配置 ──
# v18: 图片生成切换到 Seedream 5.0 Lite，并使用当前方舟 Key。
# 注意：Seedance 是视频模型；图片生成这里实际使用的是 Seedream。
_DOUBAO_IMG_API   = os.getenv("ARK_IMAGE_API", "https://ark.cn-beijing.volces.com/api/v3/images/generations")
_DEFAULT_ARK_IMAGE_KEY = "ark-527ef685-9d58-45b9-9cb5-765d58506bdf-c6f44"
_DOUBAO_IMG_KEY   = _DEFAULT_ARK_IMAGE_KEY  # v47 DEMO直连：不读环境变量，避免旧图片key覆盖
# 官方文档同时支持 doubao-seedream-5-0-260128 与 doubao-seedream-5-0-lite-260128；默认使用 Lite 别名，可用 ARK_IMAGE_MODEL 覆盖。
_DOUBAO_IMG_MODEL = (
    os.getenv("ARK_IMAGE_MODEL")
    or os.getenv("ARK_SEEDREAM_MODEL")
    or "doubao-seedream-5-0-lite-260128"
)

# ── 多参考图上限 ──
_MAX_REF_IMAGES = 4
_MAX_CHAR_REFS  = 2   # 最多取前 3 个角色立绘 (第 4+ 用文字描述)

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

def _is_chinese_prompt_text(text: str) -> bool:
    if not text:
        return False
    cn = len(re.findall(r'[一-鿿]', text))
    total = len(re.sub(r'\s+', '', text)) or 1
    return cn / total >= 0.25


def _is_creature_role(name: str = '', desc: str = '') -> bool:
    """判断是否是非人类/巨兽类角色。用于角色立绘阶段启用单主体约束。"""
    combo = f"{name or ''} {desc or ''}"
    creature_keys = [
        '龙', '巨龙', '古龙', '飞龙', '怪物', '怪兽', '巨兽', '魔物', '兽',
        '机器人', '机甲', '幽灵', '妖', '恶魔', '魔鬼', '外星', '异形',
        'dragon', 'monster', 'creature', 'beast', 'mecha', 'robot', 'demon', 'alien'
    ]
    return any(k in combo for k in creature_keys)


def _strip_relation_refs_for_character_desc(desc: str, char_name: str = '') -> str:
    """角色立绘阶段清理“和其他角色的比例/关系”描述。

    例如“体型约为勇者8倍”“高过勇者全身”会让生图模型在角色卡里画出
    勇者/比例小人。角色库描述只保留角色自身外貌；体型关系应放到分镜 prompt。
    """
    text = re.sub(r'\s+', ' ', (desc or '').strip(' ，,；;'))
    if not text:
        return text

    # 常见比例参照/关系句式，直接删除。
    patterns = [
        r'体型[^，。；;]*?(?:勇者|少年勇者|少年/勇者|人类|骑士|主角|敌人|对手)[^，。；;]*',
        r'头部[^，。；;]*?(?:勇者|少年勇者|少年/勇者|人类|骑士|主角)[^，。；;]*',
        r'高度[^，。；;]*?(?:勇者|少年勇者|少年/勇者|人类|骑士|主角)[^，。；;]*',
        r'高过[^，。；;]*?(?:勇者|少年勇者|少年/勇者|人类|骑士|主角)[^，。；;]*',
        r'(?:约为|大约是|至少是|超过)[^，。；;]*?(?:勇者|少年勇者|少年/勇者|人类|骑士|主角)[^，。；;]*',
        r'不能[^，。；;]*?(?:坐骑|骑乘|被骑|勇者|少年勇者|少年/勇者|人类|骑士)[^，。；;]*',
        r'不得[^，。；;]*?(?:坐骑|骑乘|被骑|勇者|少年勇者|少年/勇者|人类|骑士)[^，。；;]*',
    ]
    for pat in patterns:
        text = re.sub(pat, '', text)

    # 分句过滤：包含其他命名角色/人类参照的短句删掉；“非人类”保留。
    keep_parts = []
    ref_words = ['勇者', '少年勇者', '少年/勇者', '骑士', '主角', '敌人', '对手', '比例参照', '旁边小人']
    for part in re.split(r'[，,。；;\n]+', text):
        part = part.strip()
        if not part:
            continue
        if any(w in part for w in ref_words):
            continue
        if '人类' in part and '非人类' not in part:
            continue
        keep_parts.append(part)

    cleaned = '，'.join(keep_parts)
    cleaned = re.sub(r'[，,。；;]{2,}', '，', cleaned).strip(' ，,。；;')
    return cleaned or text.strip(' ，,。；;')


def _normalize_character_desc_for_doubao(name: str, desc: str) -> str:
    """豆包主链路角色描述：保留中文，并补充关键一致性锚点。

    v19 修复：非人类/巨兽角色的立绘描述会清理“和勇者/人类的比例关系”，
    避免角色卡里生成旁边小人或比例参照人物。
    """
    d = re.sub(r'\s+', ' ', (desc or '').strip(' ，,;；'))
    is_creature = _is_creature_role(name, d)
    if is_creature:
        d = _strip_relation_refs_for_character_desc(d, name)

    combo = f'{name}{d}'
    add = []
    if not is_creature:
        if any(k in combo for k in ['少年', '勇者', '骑士', '男主', '青年']) and not any(k in d for k in ['18岁', '十八', '青年', '不是儿童']):
            add.append('18岁左右青年男性，不是儿童，青年体型比例')
        if not any(k in d for k in ['发', '头发', '发型']):
            add.append('发型固定')
        if not any(k in d for k in ['穿', '服', '衣', '裙', '袍', '甲', '铠', '披风', '制服', '校服']):
            add.append('标志性服装款式和配色固定')
        if any(k in combo for k in ['剑', '圣剑', '持剑', '握剑']) and '剑' not in d:
            add.append('始终手持同一把剑')
    else:
        if any(k in combo for k in ['龙', '巨龙', '古龙']):
            for k, phrase in [
                ('鳞', '同一鳞片纹理固定'), ('翅', '巨大双翼'), ('翼', '同一翼膜颜色固定'),
                ('角', '同一弯曲角形固定'), ('尾', '长尾形状固定'), ('爪', '锋利巨爪')
            ]:
                if k not in d:
                    add.append(phrase)
            if '单独主体' not in d and '只允许出现' not in d:
                add.append('画面中只允许出现这一条巨龙，单独主体')
        else:
            if '单独主体' not in d:
                add.append('画面中只允许出现当前非人类角色，单独主体')
    if add:
        d = (d + '，' if d else '') + '，'.join(dict.fromkeys(add))
    return d[:360]


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
    # 匹配到任一关键词就不加 1girl/1boy 标签，让 旧图像引擎 按原样描述生成
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
            # 如果翻译结果太少，把原始中文描述也加到备注里（旧图像入口可识别部分英文词）
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


def _normalize_youth_terms_for_prompt(text: str, role_name: str = '') -> str:
    """防止“少年”被 Seedream 理解成儿童。"""
    t = text or ''
    role = role_name or ''
    should_fix = any(k in role for k in ['少年', '勇者', '男主', '青年']) or re.search(
        r'\b(young\s+boy|teenage\s+boy|teen\s+boy|little\s+boy|boy)\b', t, flags=re.I
    )
    if not should_fix:
        return t
    t = re.sub(r'\byoung\s+boy\b', 'young male hero around 18 years old', t, flags=re.I)
    t = re.sub(r'\bteenage\s+boy\b', 'young male hero around 18 years old', t, flags=re.I)
    t = re.sub(r'\bteen\s+boy\b', 'young male hero around 18 years old', t, flags=re.I)
    t = re.sub(r'\blittle\s+boy\b', 'young male hero around 18 years old', t, flags=re.I)
    t = re.sub(r'\bboy\b', 'young male hero', t, flags=re.I)
    t = re.sub(r'\bchild\b|\bkid\b', 'young adult', t, flags=re.I)
    if not re.search(r'\b(17|18|19|eighteen|late-teen|young adult)\b', t, flags=re.I):
        t = t.rstrip(' ，,.') + ', around 18 years old, late-teen young adult proportions'
    if not re.search(r'not a child|not childlike', t, flags=re.I):
        t = t.rstrip(' ，,.') + ', not a child, not childlike'
    return re.sub(r'\s+', ' ', t).strip()


def _extract_full_appearance(desc: str, name: str = "") -> str:
    desc = _normalize_character_desc_for_doubao(name, desc) if _is_chinese_prompt_text(desc) else _normalize_youth_terms_for_prompt(desc, name)
    if _is_chinese_prompt_text(desc):
        return desc[:180]
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
# 旧图像引擎 封装（已停用，主流程不再调用）
# ═══════════════════════════════════════════════════════
def _get_nai_client():
    """获取 旧图像引擎 客户端 — 不污染全局 os.environ
    ★ v12 修复:
      原版本会永久修改 os.environ 的 HTTPS_PROXY/HTTP_PROXY,
      一旦代理关闭, 所有后续 requests 调用都会连到死代理并挂掉。
      现在改为: 只设置 NOVELAI_API_KEY, 代理通过 httpx/requests 的
      参数级方式传入 (下面 _nai_generate 用 contextmanager 临时启用)。
    """
    os.environ["NOVELAI_API_KEY"] = _旧图像引擎_TOKEN
    from novelai import 旧图像引擎
    return 旧图像引擎()


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
    """旧图像引擎 入口已停用：项目当前统一使用豆包 Seedream。"""
    return {"success": False, "message": "旧图像入口已停用：当前项目统一使用 Seedream 中文主链路"}
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
        with _temporary_proxy_env(_旧图像引擎_PROXY):
            client = _get_nai_client()
            params = GenerateImageParams(**kw)
            try:
                cost = params.calculate_anlas(is_opus=True)
            except Exception:
                cost = 5 * len(char_ref_images or [])
            print(f"[旧旧图像引擎] 已停用，不应进入生成分支 {width}x{height}")
            images = client.image.generate(params)
        if images:
            buf = io.BytesIO()
            images[0].save(buf, format="PNG")
            url = _save_shot(buf.getvalue())
            print(f"[旧图像引擎] ✓ {url}")
            return {"success": True, "image_url": url, "url": url}
        return {"success": False, "message": "旧图像入口返回空"}
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "message": str(e)}


# ═══════════════════════════════════════════════════════
# 豆包 Seedream 5.0 Lite
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
    """豆包 Seedream 5.0 Lite"""
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
    """角色视图生成：豆包 Seedream 中文主链路，不再调用 旧图像引擎。

    豆包图片接口没有独立 negative_prompt 字段，这里把负面约束以中文
    “禁止项”追加到 prompt 末尾。
    """
    ref_images = ref_images or []
    base_neg = "禁止多人，禁止重复角色，禁止多视角拼图，禁止水印文字，禁止畸形手指，禁止改变服装配色"
    if neg:
        base_neg = base_neg + "，" + str(neg).strip(' ，,')
    prompt = prompt + "。禁止项：" + base_neg + "。"
    db_result = _doubao_generate(
        prompt=prompt,
        ref_image_urls=ref_images,
        size="2:3",
    )
    if db_result.get("success"):
        print(f"[角色] {view_name} 使用豆包成功")
        return db_result
    return {"success": False, "message": f"豆包失败: {db_result.get('message', '')}"}


def generate_character_views(char_name, description, art_style="日漫", front_url=None):
    """生成角色三视图：豆包中文主链路。

    v19：非人类/巨兽角色启用“单独主体立绘”模式，清理比例参照人物，
    避免远古巨龙角色卡旁边生成勇者/小人。
    """
    style_zh = STYLE_ZH_PREFIX.get(art_style) or f"{art_style}风格精致插画"
    desc_zh = _normalize_character_desc_for_doubao(char_name, description)
    is_creature = _is_creature_role(char_name, desc_zh)

    seed = int(hashlib.md5(f"doubao_char_{char_name}_{desc_zh[:120]}".encode()).hexdigest()[:8], 16) % (2**31)
    print(f"[角色] {char_name}: 豆包中文三视图 seed={seed} creature={is_creature}")

    if is_creature:
        subject_rule = (
            f"画面中只允许出现一个主体：{char_name}。"
            f"只画这一只{char_name}，不得出现任何人类、勇者、骑士、少年、比例参照小人、旁边小人、背景人物、其他角色、第二条龙。"
            "不要做角色设定表拼版，不要在旁边画缩略小人或比例尺。"
        )
        base = (
            f"{style_zh}，单体怪兽/巨兽角色立绘，{subject_rule}。"
            f"角色名称：{char_name}。外貌锁定：{desc_zh}。"
            "必须保持同一头部轮廓、同一角形、同一鳞片纹理、同一翼膜颜色、同一尾部形状和同一巨大体型轮廓。"
        )
        bg = "干净浅色纯背景，完整身体轮廓居中，体表纹理清晰，高质量，非三视图拼版"
        creature_neg = "人类，勇者，骑士，少年，女孩，男孩，比例参照小人，旁边小人，背景人物，其他角色，第二条龙，多条龙，缩略图，角色设计表小人，注释文字"

        front_prompt = f"{base}全身正面静态姿势，正面视角，双翼完整可见或自然展开，长尾完整可见，{bg}"
        fr = _generate_character_view_with_fallback(front_prompt, creature_neg, seed, "front")
        views = {}
        if fr.get("success"):
            views["front"] = fr.get("url") or fr.get("image_url", "")
        else:
            return {"success": False, "message": "正面立绘失败: " + fr.get("message", "")}

        ref = [front_url] if front_url else [views["front"]]
        view_prompts = [
            ("side", f"{base}全身侧面轮廓，侧身静态姿势，侧面视角，只出现这一只{char_name}，{bg}"),
            ("face", f"{base}头部和上颈部近景，红色发光眼和弯曲角形清晰，只出现巨龙头部，不出现任何人类或比例参照，{bg}"),
        ]
        for offset, (vn, vp) in enumerate(view_prompts, start=1):
            r = _generate_character_view_with_fallback(
                prompt=vp,
                neg=creature_neg,
                seed=seed + offset,
                view_name=vn,
                ref_images=ref,
            )
            views[vn] = (r.get("url") or r.get("image_url", "")) if r.get("success") else ""
        return {"success": True, "image_url": views["front"], "views": views}

    # 人类/普通角色：v20 改为“单视角”生成，避免 Seedream 把角色卡画成正面+侧面拼版。
    # 之前使用“单角色立绘/完整身体”仍可能被模型理解为角色设定表，导致一张图里出现
    # 正面、侧面、半身小图等多个视角。这里把每一次生成都明确为“只生成一个身体、一个视角”。
    subject_rule = (
        f"画面中只允许出现一个主体：{char_name}。"
        f"只画一个{char_name}，只画一个身体，只画一个视角；"
        "不得出现第二个人、侧面备用图、背面备用图、半身小图、小头像、比例参照人物或任何其他角色。"
    )
    base = (
        f"{style_zh}，单人单视角角色图，{subject_rule}。"
        f"角色名称：{char_name}。外貌锁定：{desc_zh}。"
        "必须保持同一张脸、同一发型、同一服装配色、同一道具/武器和同一体型比例。"
        "这不是角色设定表，不是三视图，不是双视图拼版，不是参考图排版。"
    )
    bg = "干净浅色纯背景，画面不分栏，不拼版，不加注释文字，角色居中，细节清晰，高质量"
    human_neg = (
        "其他角色，第二个人，双人，多人，重复人物，分身，克隆，旁边小人，背景人物，"
        "比例参照人物，正面和侧面同时出现，多个视角，同一角色多个身体，三视图，双视图拼版，"
        "角色设定表，角色设计表小人，小头像，半身小图，注释文字，分割画面"
    )

    front_prompt = (
        f"{base}当前只生成正面全身图：{char_name}一个人正面站姿，面向镜头，"
        f"全身完整可见，双脚到头顶完整入画；画面里不要出现侧面版本、背面版本或第二个身体。{bg}"
    )
    fr = _generate_character_view_with_fallback(front_prompt, human_neg, seed, "front")
    views = {}
    if fr.get("success"):
        views["front"] = fr.get("url") or fr.get("image_url", "")
    else:
        return {"success": False, "message": "正面立绘失败: " + fr.get("message", "")}

    # 侧面/特写分别单独生成。为避免 front 如果偶发拼版导致污染，默认只在用户显式传 front_url 时才引用。
    # 不再自动把刚生成的 front 作为参考图传入侧面/特写。
    ref = [front_url] if front_url else []
    view_prompts = [
        ("side", (
            f"{base}当前只生成侧面全身图：{char_name}一个人侧身站姿，纯侧面视角，"
            f"全身完整可见；画面里不要出现正面版本、背面版本或第二个身体。{bg}"
        )),
        ("face", (
            f"{base}当前只生成面部近景：{char_name}一个人的脸部和上半身近景，"
            f"五官清晰，表情自然；画面里不要出现全身小人、第二个身体或多视角拼版。{bg}"
        )),
    ]
    for offset, (vn, vp) in enumerate(view_prompts, start=1):
        r = _generate_character_view_with_fallback(
            prompt=vp,
            neg=human_neg,
            seed=seed + offset,
            view_name=vn,
            ref_images=ref,
        )
        views[vn] = (r.get("url") or r.get("image_url", "")) if r.get("success") else ""
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



def _is_dynamic_action_shot(shot: dict) -> bool:
    """判断当前镜头是否是动作镜。动作镜使用角色参考图时需要避免复制正面立绘姿势。"""
    text = (
        (shot.get('action_zh') or '') + ' ' +
        (shot.get('jimeng_ref_prompt') or '') + ' ' +
        (shot.get('video_prompt') or '')
    )
    dynamic_kws = [
        '高举', '举起', '抬起', '挥', '奔', '跑', '冲', '跃', '跳', '转身',
        '回头', '抬头', '低头', '站在龙背', '龙背', '骑', '飞', '盘旋',
        '逼近', '靠近', '怒吼', '蓄势', '发光', '飘动', '跪', '蹲'
    ]
    return any(k in text for k in dynamic_kws)




def _pick_character_identity_refs(char_obj, shot=None):
    """v18: 为单个命名角色挑选最有用的身份参考图。

    规则：front 永远优先；特写补 face；动作镜补 side。
    """
    if not char_obj:
        return []
    views = char_obj.get('views') or {}
    front = views.get('front') or char_obj.get('image_url') or ''
    side = views.get('side') or ''
    face = views.get('face') or ''
    refs = []
    if front:
        refs.append(front)

    shot = shot or {}
    shot_type = shot.get('shot_type', '') or ''
    is_dynamic = _is_dynamic_action_shot(shot)

    if any(k in shot_type for k in ['特写', '近景', '近特写']):
        aux = face or side
    elif is_dynamic:
        aux = side or face
    else:
        aux = face or side

    if aux and aux not in refs:
        refs.append(aux)
    return refs


def _is_creature_char(c: dict) -> bool:
    name = (c.get('name', '') or '')
    desc = (c.get('description', '') or '').lower()
    return any(k in name for k in ['龙', '巨龙', '古龙', '兽', '怪']) or any(k in desc for k in ['dragon', 'beast', 'monster', 'creature', 'serpent'])



def _clean_scene_text_for_prompt_v26(text: str) -> str:
    """生图前再次净化场景，避免场景描述把巨龙/人物塞回背景。"""
    s = str(text or '').strip()
    if not s:
        return ''
    # 常见英文场景转中文
    low = s.lower()
    if len(re.findall(r'[a-zA-Z]', s)) > len(re.findall(r'[\u4e00-\u9fff]', s)):
        parts = []
        if any(k in low for k in ['charred', 'scorched', 'burnt', 'burned']): parts.append('焦黑平原')
        if any(k in low for k in ['cracked', 'cracks', 'crisscrossed']): parts.append('焦土遍布纵横裂痕')
        if any(k in low for k in ['smoke', 'haze', 'billowing']): parts.append('低空烟尘翻涌')
        if any(k in low for k in ['afterglow', 'sunset', 'dusk']): parts.append('远天残阳余晖压低')
        if parts:
            s = '，'.join(parts)
    role_words = ['勇者','少年','青年','少女','主角','骑士','人物','角色','巨龙','远古巨龙','古龙','恶龙','魔龙','龙','怪物','巨兽','敌人','对手']
    action_words = ['站','坐','跪','跑','冲','扑','飞','盘踞','盘旋','张翼','咆哮','逼近','对峙','迎战','挥剑','举剑','攻击','倒地','倒下']
    clauses = [c.strip(' ，,。；;：:') for c in re.split(r'[，,。；;]+', s) if c.strip(' ，,。；;：:')]
    kept = []
    for c in clauses:
        if any(w in c for w in role_words) or any(w in c for w in action_words):
            continue
        kept.append(c)
    if not kept:
        return '焦黑平原，焦土遍布纵横裂痕，低空烟尘翻涌'
    return '，'.join(kept[:4])[:60]

def _reference_safety_directive(shot: dict, out_chars: list) -> str:
    """Seedream 5.0 Lite 参考图安全指令：避免把角色立绘当作站姿/构图复制。"""
    names = [c.get('name', '') for c in (out_chars or []) if c.get('name')]
    relation = _detect_spatial_relation_from_shot(shot)
    action = (shot.get('action_detail_zh') or shot.get('action_zh') or shot.get('action') or '').strip()
    shot_type = (shot.get('shot_type') or '').strip()
    parts = []

    if names:
        parts.append('命名角色数量锁定：' + '，'.join([f'{name}只出现1次' for name in names]))
        parts.append('每个命名角色只允许出现一次，禁止同一命名角色出现大头特写+全身小人两种重复形态，禁止画中画、分身、镜像复制、背景重复剪影')
        parts.append('允许符合题材和场景的非命名背景元素，如远处人群、同学、车辆、建筑、士兵剪影、怪物群影或城市行人；但它们必须弱化处理，不能抢占主体，不能与命名角色外貌相同，不能被画成第二个命名角色')
        if any(_is_creature_char(c) for c in (out_chars or [])):
            parts.append('非人类命名角色的体表材质、角、翼、尾部、眼睛发光颜色和体型比例必须在本镜保持与参考图一致，禁止把同一条龙画成不同品种、不同颜色或不同头冠结构')

    # 巨龙类强约束：禁止同时出现远景龙、近景龙头、第二条龙
    dragon_names = [n for n in names if any(k in n for k in ['龙', '巨龙', '古龙'])]
    if dragon_names:
        dn = dragon_names[0]
        parts.append(f'命名角色{dn}只能出现一次，不能同时出现远景整龙和近景龙头；背景可以有弱化的环境剪影，但不能画成第二个命名{dn}')
        parts.append(f'禁止同一画面同时出现站立/飞行的命名{dn}和倒地的命名{dn}；如果命名{dn}倒地，背景不得再出现可识别为同一命名{dn}的第二身体')
        parts.append(f'特别禁止把同一条{dn}拆成两颗龙头、两段龙身或左右对称的双龙夹击构图；若是人龙对峙镜，画面中关于{dn}的所有可见部位都必须属于同一条龙')

    if action:
        parts.append(f'本镜头唯一主要动作：{action}')
        parts.append('角色姿态必须服务于本镜头动作，禁止直接复制角色立绘中的正面站立姿势或持剑静态姿势')
    elif _is_dynamic_action_shot(shot):
        parts.append('参考图只用于身份、脸、服装颜色和武器样式，不用于复制姿势')

    if relation == 'mounted':
        # mounted: 不允许左右对峙，不允许第二个承载主体
        parts.append('空间关系锁定：承载/骑乘关系，人物在唯一巨兽背部或身体上方，二者不是左右对峙，禁止生成第二个承载主体')
        parts.append('承载关系镜头里巨兽必须是活体承载姿态，禁止同时画倒地尸体、断裂尸体或背景第二只巨兽')
    elif relation == 'confront':
        parts.append('空间关系锁定：双方对峙，主体分居画面两侧并侧身相对，视线交汇；除非分镜明确指定左右，否则不要硬性左右互换，禁止双方都正面看镜头')
    elif relation == 'same_side':
        parts.append('空间关系锁定：角色同向或并肩，禁止改成面对面对峙')
    elif relation == 'chase':
        parts.append('空间关系锁定：追击关系，前后景关系明确，禁止面对面对峙')

    if shot_type:
        parts.append(f'构图类型：{shot_type}；不要因为参考图是全身立绘就强行生成全身正面站姿')

    parts.append('保持所选画风和完整背景，禁止纯白背景、角色卡片背景、设定图排版')
    return '。'.join(parts)






# ═══════════════════════════════════════════════════════
# 中文 Seedream 提示词增强器：把自然语言动作改写成可视化镜头指令
# 目标：修复“从天而降双手持剑下劈”这类自然语言摘要导致的姿态阶段错误。
# 不绑定具体剧本；只根据动作类型/阶段补全可画信息。
# ═══════════════════════════════════════════════════════

_PROMPT_REFINE_HINTS = {
    'aerial': ['从天而降', '空中', '天空', '半空', '高空', '俯冲', '跳起', '跃起', '飞跃', '腾空', '飞扑'],
    'weapon': ['举剑', '举刀', '举枪', '举起', '高举', '抬起', '扬起', '持剑', '持刀', '握剑', '握刀', '双手持剑', '双手握剑'],
    'down_attack': ['下劈', '劈下', '重斩', '斩落', '挥砍', '挥剑', '挥刀', '斩击', '刺击'],
    'charge': ['蓄力', '准备', '起手', '爆发前', '终结'],
    'dash': ['冲刺', '猛冲', '扑向', '追击', '躲闪', '翻滚'],
    'evade': ['闪避', '躲避', '躲开', '闪开', '避开', '跳开', '后跳', '侧翻', '翻滚躲避', '跃开'],
    'defend': ['防守', '格挡', '戒备', '举剑防守', '半蹲防守'],
    'impact': ['拍击地面', '拍地', '砸地', '重击地面', '地面碎裂', '冲击波', '喷火', '吐息', '挥爪', '横扫', '扫击', '扑击'],
    'reaction': ['吃痛', '后仰', '被震开', '逼退', '受击', '后退'],
}

_INTERACTION_ATTACK_KEYS = ['拍击地面', '拍地', '砸地', '重击地面', '喷火', '吐息', '挥爪', '横扫', '扫击', '扑击', '压迫', '逼近']
_INTERACTION_RESPONSE_KEYS = ['闪避', '躲避', '躲开', '跳起', '跳开', '后跳', '翻滚', '侧翻', '防守', '格挡', '后仰', '吃痛', '逼退']


def _detect_interaction_mode(raw: str, out_chars=None) -> str:
    raw = str(raw or '')
    count = len(out_chars or [])
    has_attack = any(k in raw for k in _INTERACTION_ATTACK_KEYS)
    has_resp = any(k in raw for k in _INTERACTION_RESPONSE_KEYS)
    multi_subject_hint = count >= 2 or ('，' in raw or ',' in raw or '同时' in raw or '与' in raw)
    if multi_subject_hint and has_attack and has_resp:
        if any(k in raw for k in ['闪避', '躲避', '躲开', '跳起', '跳开', '后跳', '翻滚', '侧翻']):
            return 'attack_evade'
        if any(k in raw for k in ['防守', '格挡', '戒备']):
            return 'pressure_defend'
        if any(k in raw for k in ['吃痛', '后仰', '被震开', '逼退', '受击']):
            return 'hit_react'
        return 'interaction_general'
    return ''




def _extract_directional_relation_zh(raw: str) -> tuple[str, list[str]]:
    raw = str(raw or '').strip()
    notes = []
    avoid = []
    if any(k in raw for k in ['向左闪避', '向左躲避', '向左跳开', '向左后方']):
        notes.append('方向关系：反应者向画面左侧或左后上方闪避，攻击路径朝反应者原先站位压来')
    elif any(k in raw for k in ['向右闪避', '向右躲避', '向右跳开', '向右后方']):
        notes.append('方向关系：反应者向画面右侧或右后上方闪避，攻击路径朝反应者原先站位压来')
    elif any(k in raw for k in ['侧上方', '上方跃起', '跳起闪避', '跃起闪避']):
        notes.append('方向关系：反应者不是原地起跳，而是向侧上方斜向跃起闪避，保留清楚的水平位移')
    elif any(k in raw for k in ['后跳', '向后跳', '后撤']):
        notes.append('方向关系：反应者沿远离攻击路径的方向后跳或后撤，拉开与攻击者的距离')
    elif any(k in raw for k in ['翻滚', '侧翻']):
        notes.append('方向关系：反应者沿攻击路径侧面低位翻滚躲开，不要原地蹲下')
    else:
        notes.append('默认构图关系：攻击者位于画面左侧或左前方，朝右侧的反应者发动攻击；反应者位于画面右侧或右下方，沿侧向或后侧方向闪避或防守，二者之间保留清楚间距')

    if any(k in raw for k in ['喷火', '吐息']):
        notes.append('朝向关系：攻击者头部和口部明确朝向反应者，火焰从攻击者一侧连续喷向反应者原先位置，路径完整可见')
        avoid.extend(['火焰像背景特效没有明确起点', '火焰绕到反应者背后'])
    if any(k in raw for k in ['拍击地面', '拍地', '砸地', '重击地面']):
        notes.append('落点关系：攻击者前爪或攻击肢体的落点清楚，反应者位于落点外缘而不是正下方，冲击裂纹从落点向外扩散')
        avoid.extend(['攻击落点不清楚', '反应者站在落点正下方'])
    if any(k in raw for k in ['挥爪', '横扫', '扫击']):
        notes.append('扫掠关系：攻击者挥爪或横扫具有清楚的扫掠方向，反应者离开扫掠轨迹，不要与爪击方向重叠')
        avoid.extend(['爪击方向模糊', '反应者与爪击轨迹重叠'])
    if any(k in raw for k in ['逼近', '压迫', '防守', '格挡']):
        notes.append('对位关系：攻击者正面逼近反应者，反应者面向攻击者进行防守或戒备，二者视线和身体朝向要互相对准')
        avoid.extend(['二者都看向镜头', '二者朝向彼此不一致'])

    avoid.extend(['两个主体正面对称站桩', '主体重叠遮挡导致看不清动作', '攻击者和反应者都朝向镜头摆拍'])
    dedup=[]
    for a in avoid:
        if a and a not in dedup:
            dedup.append(a)
    return '。'.join(notes), dedup[:8]


def _build_interaction_relation_zh(raw: str, out_chars=None) -> tuple[str, list[str], str]:
    raw = str(raw or '').strip()
    mode = _detect_interaction_mode(raw, out_chars)
    if not mode:
        return '', [], ''

    names = [c.get('name', '') for c in (out_chars or []) if c.get('name')]
    if len(names) >= 2:
        duo = f"{names[0]}与{names[1]}必须同时出现在同一画面中，主体关系和空间距离清楚"
    else:
        duo = '双主体必须同时出现在同一画面中，攻击者与反应者关系清楚'

    directional_desc, directional_avoid = _extract_directional_relation_zh(raw)
    parts = [f"双主体关系：{duo}，攻击者体型或压迫感更强，反应者动作清楚，不要各做各的动作"]
    avoid = ['两个主体各站各的', '像摆拍合影', '看不出谁在攻击谁在反应']
    camera_default = '全景或中远景，低机位斜侧视角，两个主体、攻击路径和地面冲击范围都要看清'

    if mode == 'attack_evade':
        parts.append('交互动作：攻击者发动明确攻击，反应者不是进攻而是闪避或躲避；要表现“因为攻击袭来，所以反应者正在躲开”的因果关系')
        if any(k in raw for k in ['拍击地面', '拍地', '砸地', '重击地面']):
            parts.append('冲击表现：攻击者重击地面，地面碎裂，烟尘、碎石或冲击波向外爆开；反应者位于冲击中心附近，正向侧上方、侧后方或后方跃起或闪开，避开冲击中心')
            avoid.extend(['地面没有冲击痕迹', '反应者仍站在冲击中心', '攻击动作太轻'])
        if any(k in raw for k in ['喷火', '吐息']):
            parts.append('攻击路径：火焰或吐息有明确喷射方向；反应者向侧面或低位翻滚躲开，不要迎着火焰站立')
            avoid.extend(['火焰方向不清楚', '闪避方向不清楚'])
        if any(k in raw for k in ['挥爪', '横扫', '扫击', '扑击']):
            parts.append('空间关系：攻击动作有明确横向或前向扫掠方向；反应者通过后跳、侧闪或低身躲开攻击路径')
            avoid.extend(['攻击路径不清楚', '反应者仍站在扫击线上'])
        avoid.extend(['两个主体贴脸重叠', '画面看不出闪避方向'])
    elif mode == 'pressure_defend':
        parts.append('交互动作：攻击者逼近或压迫，反应者采取防守、格挡或戒备姿态；强调体型与压迫感对比')
        camera_default = '中景或中远景，低机位斜侧视角，主体之间距离关系清楚，保留明显前后景纵深'
        avoid.extend(['双方像普通站立合影', '没有压迫感'])
    elif mode == 'hit_react':
        parts.append('交互动作：一方攻击或冲击已产生效果，另一方出现吃痛、后仰、被震开或受击反应；要明确谁是受击者')
        camera_default = '中景或全景，斜侧视角，动作与受击反应都清楚'
        avoid.extend(['看不出受击者', '看不出命中结果'])
    else:
        parts.append('交互动作：两个主体在同一瞬间存在明确动作因果与空间关系，不要画成互不相关的独立 pose')

    if directional_desc:
        parts.append(f'朝向与空间：{directional_desc}')
    avoid.extend(directional_avoid)

    seen = []
    for item in avoid:
        if item and item not in seen:
            seen.append(item)
    return '。'.join(parts), seen[:12], camera_default

def _prompt_refine_tags(text: str) -> set:
    text = str(text or '')
    tags = set()
    for tag, keys in _PROMPT_REFINE_HINTS.items():
        if any(k in text for k in keys):
            tags.add(tag)
    return tags



def _should_use_prompt_refiner(shot: dict) -> bool:
    # Seedream 图片生成：只要有明确 image_prompt_zh/旧兼容首帧提示词，就走短链路，不再回到4000字身份锁。
    if str(shot.get('regen_mode') or '') in {'prompt_refine_action', 'prompt_passthrough', 'pose_action_first'}:
        return True
    if str(shot.get('image_prompt_zh') or shot.get('first_frame_prompt_zh') or shot.get('jimeng_ref_prompt') or '').strip():
        return True
    text = ' '.join(str(shot.get(k, '') or '') for k in [
        'user_override_prompt', 'action_zh', 'action_detail_zh', 'shot_type', 'camera_angle'
    ])
    tags = _prompt_refine_tags(text)
    return bool(
        shot.get('user_override_prompt')
        or ('aerial' in tags and (('weapon' in tags) or ('down_attack' in tags) or ('charge' in tags)))
        or ('weapon' in tags and 'down_attack' in tags)
    )

def _compact_style_zh(art_style: str) -> str:
    return STYLE_ZH_PREFIX.get(art_style) or (f"{art_style}风格精致插画" if art_style else STYLE_ZH_PREFIX['日漫'])


def _minimal_character_anchor_zh(out_chars) -> str:
    if not out_chars:
        return ''
    anchors = []
    for c in out_chars[:3]:
        name = c.get('name', '') or '角色'
        desc = _normalize_character_desc_for_doubao(name, c.get('description', '') or '')
        appearance = _extract_full_appearance(desc, name)
        outfits = _extract_outfit_keywords(desc)
        outfit_note = f"，服装/道具保持{'、'.join(outfits[:4])}" if outfits else ''
        anchors.append(f"{name}保持{appearance}{outfit_note}")
    return '；'.join(anchors)


def _minimal_scene_anchor_zh(shot, scene_spec_zh='') -> str:
    scene = _clean_scene_text_for_prompt_v26(shot.get('scene_description', '') or scene_spec_zh or shot.get('scene_anchor', ''))
    if not scene:
        return ''
    scene = re.sub(r'不得[^，。；]+[，。；]?', '', scene)
    scene = re.sub(r'禁止[^，。；]+[，。；]?', '', scene)
    return scene[:90]


def _refine_motion_visual_language(raw: str) -> tuple[str, list[str], set]:
    raw = str(raw or '').strip()
    tags = _prompt_refine_tags(raw)
    action_parts = []
    avoid = []
    if raw:
        action_parts.append(f"用户动作原意：{raw}")
    if 'aerial' in tags:
        action_parts.append('空间与运动：主体必须明确离开地面，处在半空或高处；画面要看出从高处向目标方向运动，身体重心和运动方向清楚；衣摆、披风、头发可受气流影响')
        avoid.extend(['站在地上', '普通站姿', '静止半身摆拍'])
    if 'weapon' in tags:
        action_parts.append('武器姿态：如果是举起或准备挥下武器，手臂和武器要形成清楚的起手姿态；武器位置、手部握持和朝向必须明确，不要让武器被特效遮挡')
        avoid.extend(['武器位置含糊', '武器被光效遮挡'])
    if 'down_attack' in tags:
        action_parts.append('动作阶段：表现攻击发动前一瞬间或蓄力起手帧，而不是已经完成攻击；若是下劈/重斩，武器应仍在头部上方或斜上方，尚未劈到底')
        avoid.extend(['武器低于头顶', '武器已经劈到底', '武器尖端插向地面'])
    if 'charge' in tags:
        action_parts.append('发力状态：肩膀、手臂和身体重心要有蓄力感，姿态紧绷，表现爆发前的力量积累')
        avoid.extend(['动作软弱', '松散站姿'])
    if 'dash' in tags:
        action_parts.append('速度表现：用身体倾斜、衣物拖拽、烟尘或运动线表现速度方向，避免正面站桩')
        avoid.extend(['正面站桩', '没有速度感'])
    if not action_parts and raw:
        action_parts.append(raw)
    seen = []
    for item in avoid:
        if item and item not in seen:
            seen.append(item)
    return '。'.join(action_parts), seen[:7], tags








def _seedream_compact_first_frame_prompt_v81(raw: str) -> str:
    """最终进入 Seedream 的短首帧 prompt：去掉元指令/身份锁/人物档案/视频阶段词。"""
    raw = str(raw or '').strip()
    raw = re.sub(r'\s+', '', raw)
    raw = re.sub(r'[；;]+', '。', raw)
    for pat in [
        r'视觉导演[:：]?', r'镜头目标[:：]?', r'动作表现[:：]?', r'本镜唯一目标[:：]?',
        r'本镜必须对应源剧本动作[:：]?', r'身份锁[:：]?', r'角色身份锁[:：]?',
        r'统一场景锚点[:：]?', r'命名角色数量锁定[:：]?', r'角色设定[:：]?', r'人物设定[:：]?',
        r'Seedream首帧图提示词[:：]?', r'Seedance视频运动提示词[:：]?',
    ]:
        raw = re.sub(pat, '', raw)
    # 删除视频阶段词
    raw = re.sub(r'前30%[:：]?[^。]*。?', '', raw)
    raw = re.sub(r'中40%[:：]?[^。]*。?', '', raw)
    raw = re.sub(r'后30%[:：]?[^。]*。?', '', raw)
    raw = re.sub(r'保持首帧[^。]*。?', '', raw)
    # 删除人物档案/身份块
    raw = re.sub(r'(少年勇者)\1+', r'\1', raw)
    raw = re.sub(r'(远古巨龙)\1+', r'\1', raw)
    for pat in [
        r'少年勇者[:：][^。]*。?', r'远古巨龙[:：][^。]*。?', r'18岁左右青年男性[^。]*。?',
        r'身穿蓝白配色圣骑士轻甲[^。]*。?', r'双手使用同一把[^。]*。?', r'黑曜石鳞片覆盖全身[^。]*。?',
    ]:
        raw = re.sub(pat, '', raw)
    for p in ['不是儿童','脸型清秀但神情坚毅','脸型清秀','神情坚毅','微乱刘海','完整统一','体型修长敏捷',
              '白银胸甲、肩甲、护腕、腰带、护腿、白色长靴、','白银胸甲、肩甲、护腕、腰带、护腿、白色长靴']:
        raw = raw.replace(p, '')
    raw = re.sub(r'。+', '。', raw).strip('，,。；;：: ')

    drop_keys = ['身份锁','命名角色数量锁定','禁止复制','禁止镜像','不得变成','参考图仅用于','画中画','源剧本','角色卡片','不是儿童','胸甲','护腕','护腿']
    keep_keys = ['远景','全景','大全景','中远景','中景','近景','特写','低机位','仰视','斜侧','画面','左','右','上方','下方','前景','朝向','背对镜头','俯视','勇者','巨龙','圣剑','龙爪','火焰','光柱','尸体','跃起','闪避','蓄力','重斩','裂开','碎石','烟尘','火星','冲击波','披风','焦黑','裂痕','残阳','夕阳','日漫','日系','插画','风格','压迫感']
    parts = []
    seen = set()
    for idx, part in enumerate(re.split(r'[。\n]+', raw)):
        part = part.strip('，,。；;：: ')
        if len(part) < 4:
            continue
        if any(k in part for k in drop_keys):
            continue
        score = sum(1 for k in keep_keys if k in part)
        if idx == 0:
            score += 4
        if any(k in part for k in ['低机位','仰视','画面','前景','上方','下方','左','右','朝向','背对镜头']):
            score += 3
        if score <= 0:
            continue
        key = re.sub(r'[，,。]', '', part)[:36]
        if key not in seen:
            parts.append(part)
            seen.add(key)
    if not parts:
        return raw[:280]
    out = ''
    for part in parts[:4]:
        cand = (out + '。' + part).strip('。') if out else part
        if len(cand) <= 280:
            out = cand
        elif not out:
            out = part[:280]
            break
    if '风格' not in out and '日漫' not in out and '日系' not in out and '插画' not in out:
        if len(out) < 250:
            out += '。日系热血奇幻动画风格'
    return out[:300]




def _hero_dragon_names_from_shot(shot):
    names = []
    for x in (shot.get('characters_in_shot') or []):
        if isinstance(x, str) and x.strip():
            names.append(x.strip())
        elif isinstance(x, dict) and str(x.get('name') or '').strip():
            names.append(str(x.get('name')).strip())
    human = next((n for n in names if '勇者' in n or '少年' in n or '骑士' in n), '少年勇者')
    dragon = next((n for n in names if '龙' in n), '远古巨龙')
    return human, dragon


def _locked_hero_dragon_prompt_v10(shot):
    """旧项目/旧前端字段回流时，根据固定战斗 profile 兜底恢复关键镜头提示词。"""
    role = str(shot.get('shot_role') or '').strip()
    idx = int(shot.get('index') or 0) if str(shot.get('index') or '').isdigit() else shot.get('index')
    human, dragon = _hero_dragon_names_from_shot(shot)
    scene = str(shot.get('scene_anchor') or shot.get('scene_description') or '黄昏火山焦土战场，黑色焦土龟裂，熔岩裂缝暗红发光，远处黑烟与火山灰翻涌').strip()
    if role == 'claw_slam_dodge' or idx == 2:
        return (f'日漫动态漫剧精致插画，中景仰视动作镜，强烈冲击感，动作方向清楚。画面核心是“只露出{dragon}的一只巨大龙爪真实拍地砸出深坑与碎石爆裂，{human}从落点旁高高跳起，在空中完成闪避”的瞬间。画面中不要出现完整{dragon}主体，不出现龙头、龙身、双翼和完整轮廓；只露出一只巨大的龙爪和部分前臂，从画面左上方或上方压入画面。巨大龙爪从左上方向画面中下部猛砸，爪尖和掌部已经真实拍中地面，落点必须砸出明显深坑，坑缘炸裂，放射状裂痕清楚可见，大量碎石、石块、火星和烟尘向四周爆开。{human}位于画面右侧偏上方的空中，双脚完全离地，从龙爪落点旁被冲击带起高高跳起。背景承接全景参考图：{scene}。不喷火、不让勇者被击中、不站桩对峙。')
    if role == 'aerial_charge_pre_slash' or idx == 4:
        return (f'【Seedream日漫动态漫精致插画】，单人英雄特写仰视高潮前摇镜，纵向冲刺感，强烈高燃张力，16:9电影画幅。画面只出现{human}一个命名角色主体。{human}按角色参考图保持外貌、服装和圣剑一致，位于高空画面中央偏上，镜头从下往上看，形成强烈仰视英雄特写。{human}身体腾空悬停，膝盖微屈，腰背发力，双手高举正常长度圣剑过头顶，剑尖指向天空，准备下一瞬向下挥砍。这是“蓄力准备挥砍”的定格，不是已经挥下。圣剑保持正常长度，不能变成巨大光剑、魔法巨剑或光柱。剑身周围只有金白色蓄力光、细小符文粒子和螺旋气流，光效围绕手臂和正常长度圣剑，不遮挡脸、身体、剑柄和剑尖。背景只保留暗色天空、火山灰、远处火山轮廓和焦土色调的模糊纵深，背景承接全景参考图：{scene}。画面中不出现{dragon}主体，不出现魔法阵，不出现魔法巨剑，不出现命中结果。')
    if role == 'magic_circle_giant_sword_finish' or idx == 5:
        return (f'【Seedream日漫动态漫精致插画】，最终决战终结技高潮镜，超广角全景仰视镜头，华丽高燃，史诗级震撼感，16:9电影画幅。最高优先级动作：一把巨大的金白色半透明魔法能量巨剑，剑尖明确朝下，从天空魔法阵中心垂直降下，精准贯穿{dragon}胸口中心，命中点爆发白金色核心强光。全局光影：暗色天空、厚重乌云、火山灰和黑烟翻涌，整体氛围压暗；{dragon}整体偏暗，以金色轮廓光勾出龙头、龙翼、躯干和尾部；命中点最亮，魔法巨剑第二亮，魔法阵第三亮。魔法巨剑不是写实金属剑，而是半透明神圣能量剑，内部有流动金色符文和粒子光流，剑柄、剑格、宽阔剑身和向下剑尖完整清楚。天空上方展开巨大圆形符文魔法阵，是横向展开并略微侧倾的悬浮圆盘，不是竖直圆环；魔法巨剑从阵心召唤产生。{dragon}按角色参考图占据画面中下部和右侧，头部、胸口命中点、前肢、躯干、长尾和展开龙翼尽量完整可见，在冲击下猛烈后仰、张口痛苦咆哮。{human}仅作为画面左上方或右上方高空较小身影，手持正常长度圣剑刚完成挥砍召唤动作；{human}与魔法巨剑完全分离，不接触、不共线，不站在剑上。命中区域向外爆发环形冲击波、碎石、烟尘、火星和金色魔法粒子，地面裂纹同步发光。背景承接全景参考图：{scene}。禁止普通光柱、细长激光、写实金属巨剑、魔法阵竖直立起、巨剑打偏、勇者与巨剑粘连、巨龙无伤站立、只画局部龙头或裁掉巨龙主体。')
    return ''

def _authoritative_image_prompt(shot):
    """选择本镜真正应送去 Seedream 的图片提示词。
    - 若 llm_service 已明确标记 image_prompt_locked / skip_action_append_to_image_prompt，
      优先使用 jimeng_ref_prompt，避免前端旧 image_prompt_zh 把新提示词覆盖回去。
    - 若现有 image_prompt_zh 含旧版拼接尾巴（如“避免：纯白背景…”），也回退到 jimeng_ref_prompt。
    """
    if not isinstance(shot, dict):
        return ''
    image_prompt = str(shot.get('image_prompt_zh') or '').strip()
    first_frame_prompt = str(shot.get('first_frame_prompt_zh') or '').strip()
    jimeng_prompt = str(shot.get('jimeng_ref_prompt') or '').strip()
    visual_prompt = str(shot.get('visual_action_prompt_zh') or '').strip()
    action_detail = str(shot.get('action_detail_zh') or '').strip()
    action = str(shot.get('action_zh') or shot.get('action') or '').strip()
    locked = bool(shot.get('image_prompt_locked')) or bool(shot.get('skip_action_append_to_image_prompt'))
    old_tail_markers = ['避免：纯白背景', '角色卡片排版', '主体朝向混乱', '攻击路径不清', 'around ...']
    looks_stale = any(m in image_prompt for m in old_tail_markers)
    role_prompt = _locked_hero_dragon_prompt_v10(shot)
    if locked and jimeng_prompt:
        return jimeng_prompt
    if role_prompt and (locked or looks_stale or not image_prompt):
        return role_prompt
    if jimeng_prompt and (looks_stale or not image_prompt):
        return jimeng_prompt
    return image_prompt or first_frame_prompt or jimeng_prompt or role_prompt or visual_prompt or action_detail or action

def _build_seedream_prompt_refined_zh(shot, out_chars, art_style='日漫', scene_spec_zh='', global_tone=''):
    """Seedream 5.0 Lite 首帧图短链路。
    只读取 image_prompt_zh/first_frame_prompt_zh/兼容 jimeng_ref_prompt，不读取 video_prompt_zh。
    """
    style_prefix = _compact_style_zh(art_style)
    locked_prompt = bool(shot.get('image_prompt_locked')) or bool(shot.get('skip_action_append_to_image_prompt'))
    raw = (
        shot.get('user_override_prompt')
        or _authoritative_image_prompt(shot)
        or ''
    ).strip()
    if not locked_prompt:
        raw = _seedream_compact_first_frame_prompt_v81(raw)
    else:
        raw = raw[:1800]

    if not raw:
        raw = (shot.get('action_zh') or shot.get('action') or '当前分镜关键动作').strip()

    has_style = any(k in raw[:80] for k in ['Seedream', '日系动漫', '国漫', '美漫', '动漫', '插画', '画风', '风格'])
    prompt = raw if has_style else f'{style_prefix}，{raw}'

    # 只加少量场景/质量兜底，不再塞身份锁/数量锁/上一镜强参考。
    scene_anchor = _minimal_scene_anchor_zh(shot, scene_spec_zh)
    if scene_anchor and scene_anchor not in prompt:
        prompt += f'。场景保持：{scene_anchor}'
    if global_tone and global_tone not in prompt:
        prompt += f'。{global_tone}'
    prompt += '。画面完整，动作清楚，电影感构图，高质量。避免：纯白背景、角色卡片排版、主体朝向混乱、攻击路径不清。'
    for c in out_chars:
        prompt = _normalize_youth_terms_for_prompt(prompt, c.get('name', ''))
    return prompt[:1800 if locked_prompt else 420]

def _warn_if_seedream_prompt_englishish(prompt: str, source='Seedream'):
    raw = prompt or ''
    cn = len(re.findall(r'[\u4e00-\u9fff]', raw))
    en_words = len(re.findall(r'[A-Za-z]+', raw))
    total = len(re.sub(r'\s+', '', raw)) or 1
    if en_words > 20 and cn / total < 0.25:
        print(f"[{source}] ⚠ prompt 中文占比偏低，可能混入旧英文/tag链路: cn_ratio={cn/total:.2f}, en_words={en_words}")
        print(f"[{source}] preview: {raw[:240]}")

def _collect_reference_images(shot, out_chars, prev_shot_image=None,
                                all_chars=None, panorama_views=None,
                                reference_mode='guide',
                                max_total=_MAX_REF_IMAGES,
                                is_first_shot=False):
    """v18 强参考锁版：优先保证角色和场景一致性。

    设计原则：
    - 只要本镜有命名角色，就必须带角色主参考图。
    - 单角色镜优先再补 1 张辅助参考图：特写补 face，动作镜补 side。
    - 多主体镜优先保证每个主要命名主体至少 1 张身份参考图。
    - 场景全景作为背景锚点，优先级高于上一镜图。
    - 上一镜图只在低动作连续镜里作补充，不替代角色主参考。
    """
    if reference_mode == 'off':
        return []

    refs = []
    shot_type = shot.get('shot_type', '') or ''
    scene_change = shot.get('scene_change', False)
    has_chars = bool(out_chars)
    is_dynamic = _is_dynamic_action_shot(shot)
    relation = _detect_spatial_relation_from_shot(shot)

    def _append_ref(path, why=''):
        if not path or path in refs or len(refs) >= max_total:
            return False
        refs.append(path)
        if why:
            print(f"[参考图] + {why}: {os.path.basename(path) if isinstance(path, str) else path}")
        return True

    # 1) 角色身份参考图：最高优先级
    if has_chars:
        char_candidates = list(out_chars or [])
        multi_subject = len(char_candidates) > 1

        if multi_subject:
            # 多主体镜：保留给场景锚点的名额，避免角色参考图把背景挤掉
            selected_chars = []
            humans = [c for c in char_candidates if not _is_creature_char(c)]
            creatures = [c for c in char_candidates if _is_creature_char(c)]
            if humans:
                selected_chars.extend(humans[:1])
            if creatures:
                selected_chars.extend(creatures[:1])
            for c in char_candidates:
                if c not in selected_chars:
                    selected_chars.append(c)

            char_budget = max_total
            should_reserve_panorama_slot = bool(
                panorama_views and (
                    is_first_shot
                    or scene_change
                    or any(k in shot_type for k in ['全景', '远景', '环境'])
                    or relation in ('confront', 'mounted', 'same_side', 'chase')
                )
            )
            if should_reserve_panorama_slot and char_budget > 1:
                char_budget -= 1

            selected_chars = selected_chars[:max(1, char_budget)]
            for c in selected_chars:
                if len(refs) >= char_budget:
                    break
                front = ((c.get('views') or {}).get('front') or c.get('image_url') or '')
                _append_ref(front, f"命名角色身份锚点 {c.get('name','?')}")
        else:
            c = char_candidates[0]
            for idx, ref in enumerate(_pick_character_identity_refs(c, shot)):
                if len(refs) >= max_total:
                    break
                why = f"主角主参考 {c.get('name','?')}" if idx == 0 else f"主角辅助参考 {c.get('name','?')}"
                _append_ref(ref, why)

    # 2) 场景全景参考图：优先级高于上一镜
    if panorama_views and len(refs) < max_total:
        pv_list = panorama_views if isinstance(panorama_views, list) else []
        is_wide = any(k in shot_type for k in ['全景', '远景', '环境'])
        allow_panorama = (
            (not has_chars)
            or is_first_shot
            or is_wide
            or scene_change
            or relation in ('confront', 'mounted', 'same_side', 'chase')
            or (has_chars and not scene_change and not is_dynamic)
        )
        if allow_panorama and pv_list:
            pv = pv_list[0]
            pv_url = pv.get('url', '') if isinstance(pv, dict) else pv
            _append_ref(pv_url, '场景全景锚点')

    # 3) 上一镜图：只给低动作连续镜加一点连贯性，不用于高动作镜
    if (prev_shot_image
            and len(refs) < max_total
            and not scene_change
            and not is_first_shot
            and has_chars
            and len(out_chars) == 1
            and not is_dynamic):
        _append_ref(prev_shot_image, '上一镜连续性辅助')

    return refs


def _extract_outfit_keywords(desc: str) -> list:
    """从角色描述里抽出服装/配饰/武器关键词。豆包主链路优先中文。"""
    if not desc:
        return []
    text = str(desc)
    found = []

    # 中文：颜色 + 服装/配饰
    colors = r'(?:白|黑|红|橙|黄|绿|青|蓝|紫|粉|灰|棕|金|银|藏青|米|卡其|墨|绛|赤|暗|亮|深|浅)色?'
    clothes = r'(?:衬衫|衬衣|T恤|卫衣|毛衣|外套|夹克|风衣|羽绒服|大衣|披风|斗篷|校服|水手服|制服|西装|西服|和服|汉服|旗袍|唐装|长袍|道袍|战袍|战甲|铠甲|盔甲|法袍|圣袍|长裙|连衣裙|短裙|迷你裙|百褶裙|裤子|牛仔裤|短裤|工装裤|靴子|靴|鞋|袜|领带|领结|围巾|帽子|头巾|头饰|发饰|眼镜|墨镜|手套|腰带|裙子|上衣|马甲|背心)'
    zh_pattern = rf'({colors}[的之]?\s*{clothes}|{clothes})'
    for f in re.findall(zh_pattern, text):
        f = f.strip()
        if f and f not in found:
            found.append(f)

    # 英文：常见颜色/材质 + 服装/配饰/武器，解决角色描述本来就是英文时锁不住衣服的问题。
    low = re.sub(r'[_\-]+', ' ', text.lower())
    color_words = (
        'white|black|red|orange|yellow|green|blue|purple|pink|gray|grey|brown|gold|golden|silver|navy|'
        'dark|light|pale|deep|bright|crimson|scarlet|ivory|beige|khaki|cyan|teal|emerald'
    )
    material_words = 'leather|metal|steel|silver|cloth|cotton|silk|linen|wool|denim|tactical|holy|knight|fantasy'
    item_words = (
        'shirt|blouse|jacket|coat|hoodie|sweater|robe|cloak|cape|armor|armour|chest plate|breastplate|'
        'shoulder guards|pauldrons|bracers|gauntlets|belt|leg guards|greaves|boots|shoes|socks|stockings|'
        'skirt|pleated skirt|dress|pants|trousers|jeans|shorts|uniform|school uniform|sailor uniform|suit|'
        'vest|tie|bow tie|scarf|hat|gloves|mask|hair ribbon|headband|sword|holy sword|blade|staff|wand|rifle|gun|bow|quiver'
    )
    # 多词短语优先直接检测。
    phrase_items = [
        'holy knight style light armor', 'blue and white holy knight style light armor',
        'school uniform', 'sailor uniform', 'pleated skirt', 'short cloak', 'chest plate',
        'shoulder guards', 'leg guards', 'holy sword', 'leather boots', 'white blouse',
        'navy pleated skirt', 'black jacket', 'tactical vest'
    ]
    for phrase in phrase_items:
        if phrase in low and phrase not in found:
            found.append(phrase)
    en_pattern = rf'\b(?:(?:{color_words}|{material_words})\s+){{0,4}}(?:{item_words})\b'
    for m in re.finditer(en_pattern, low, flags=re.I):
        item = re.sub(r'\s+', ' ', m.group(0)).strip()
        if item and item not in found:
            found.append(item)
        if len(found) >= 8:
            break

    # 去重，保留最关键的 6 个。
    seen = []
    for item in found:
        item = item.strip(' ,.;')
        if item and item not in seen:
            seen.append(item)
        if len(seen) >= 6:
            break
    return seen



def _detect_spatial_relation_from_shot(shot: dict) -> str:
    """v14.6: 识别人物空间关系，避免把“骑乘/承载”误写成左右对峙。"""
    rel = (shot.get('relationship_type') or '').strip().lower()
    if rel in {'mounted', 'same_side', 'confront', 'chase', 'neutral'}:
        return rel
    text = (
        (shot.get('action_zh') or '') +
        (shot.get('scene_description') or '') +
        (shot.get('jimeng_ref_prompt') or '') +
        (shot.get('video_prompt') or '')
    )
    if any(k in text for k in ['站在龙背', '站在巨龙背', '站在龙身上', '站在背上', '骑在龙背', '骑龙', '坐在龙背', '伏在龙颈', '站在肩上', '坐在肩上', '站在手掌', '站在机甲手', '托起']):
        return 'mounted'
    if any(k in text for k in ['并肩', '同行', '并列', '一起', '共同', '结伴', '相拥', '拥抱', '同向']):
        return 'same_side'
    if any(k in text for k in ['追赶', '追击', '追逐']):
        return 'chase'
    if any(k in text for k in ['对峙', '对视', '对战', '交战', '厮杀', '怒吼', '逼近', '冲向', '扑向', '举剑', '挥剑', '握剑', '交锋', '迎战', '对抗', '咆哮']):
        return 'confront'
    return 'neutral'


def _pick_mounted_roles(names: list, action_text: str):
    """粗略推断骑乘/承载关系中的 rider 和 carrier。"""
    if not names:
        return '', ''
    carrier_hints = ['龙', '巨龙', '古龙', '兽', '怪物', '机甲', '机器人', '战马', '马']
    carrier = next((n for n in names if any(h in n for h in carrier_hints)), '')
    if not carrier:
        # 看动作文本里是否有某个角色更像承载主体
        carrier = next((n for n in names if n and n in action_text and any(h in action_text for h in carrier_hints)), names[-1])
    rider = next((n for n in names if n != carrier), names[0])
    return rider, carrier


def _build_scene_prop_orientation_lock_v14(shot: dict) -> str:
    """v14: 把场景锚点、道具、朝向放到 prompt 最前/最后，防止跨镜跑偏。"""
    parts = []
    anchor = (shot.get('scene_anchor') or '').strip()
    if anchor:
        parts.append(f"统一场景锚点：{anchor}，本镜必须延续同一地形、光线、天气、主色调和环境材质")
    neg_scene = (shot.get('scene_negative') or '').strip()
    if neg_scene:
        parts.append(neg_scene)
    identity_locks = shot.get('identity_locks') or []
    if isinstance(identity_locks, str):
        identity_locks = [identity_locks]
    for lock in identity_locks:
        if lock:
            parts.append(str(lock))
    prop_locks = shot.get('prop_locks') or []
    if isinstance(prop_locks, str):
        prop_locks = [prop_locks]
    for p in prop_locks:
        if p:
            parts.append(str(p))
    orient = (shot.get('orientation_lock') or '').strip()
    if orient:
        parts.append(orient)
    action = (shot.get('action_zh') or shot.get('action') or '')
    if any(k in action for k in ['冲', '躲', '闪', '挥', '格挡', '追', '扑', '交锋', '战斗', '迎击']):
        parts.append('动态动作镜必须表现侧身运动和明确动作方向，禁止正面站桩、禁止人物看向镜头摆拍')
    if anchor:
        parts.append('全景/场景参考图只用于背景一致性，不用于复制人物姿势或改变角色设计')
    return '，'.join(parts)



def _battle_image_guardrail_v27(shot, out_chars, scene_zh=''):
    """v28: 战斗镜生图护栏。
    解决：双龙、远近两条龙、结果镜仍站立、背后仰视镜被画成平视并排对打。
    """
    if not out_chars:
        return ''
    text = ''.join(str(shot.get(k, '') or '') for k in [
        'action_detail_zh', 'action_zh', 'jimeng_ref_prompt', 'video_prompt',
        'relationship_type', 'shot_type', 'camera_angle'
    ])
    creature = ''
    human = ''
    for c in out_chars:
        name = c.get('name', '')
        if _is_creature_char(c) and not creature:
            creature = name
        elif not _is_creature_char(c) and not human:
            human = name
    # 非战斗/非巨大生物镜不加太长护栏，避免污染日常镜头。
    has_battle_word = any(k in text for k in ['战', '打', '交锋', '挥剑', '圣剑', '龙爪', '压迫', '对峙', '倒地', '震退', '反击', '龙背'])
    if not creature and not has_battle_word:
        return ''

    parts = []
    if creature:
        parts.append(f'命名角色{creature}只允许出现1条/1个，禁止第二颗头、第二段身体、远处第二条、背景第二条、镜像双龙、左右双龙夹击')
    if human:
        parts.append(f'命名角色{human}只允许出现1个，必须保持同一张脸、同一发型、同一套服装和同一把武器')
        if any(k in (human + text) for k in ['勇者', '少年', '圣剑', '圣骑士']):
            parts.append(f'{human}外观必须具体稳定：18岁左右青年男性、棕色短发、蓝色眼睛、蓝白圣骑士轻甲、白银胸甲肩甲护腕腰带护腿、深蓝短披风、白色长靴、同一把金色十字护手银白圣剑，禁止画成儿童、布衣路人或紧身衣')
    if human and creature:
        parts.append(f'{human}与{creature}必须是1人对1个巨大主体，不得画成人龙同等大小并排站桩')
        parts.append(f'尺度锁定：{creature}体型始终约为{human}的6到8倍，头部高度高过{human}全身，单只前爪接近{human}半身大小，禁止忽大忽小或缩成坐骑大小')
    is_ending_frame = bool(shot.get('ending_frame')) or any(k in text for k in ['收尾关键帧', '打完后的结果', '拉开距离', '暂时占上风'])
    if shot.get('single_action_frame') or '单动作关键帧' in text or '收尾关键帧' in text:
        parts.append('单帧锁定：本图只表现当前这一帧，禁止把闪避、格挡、命中、收尾多个阶段同时画进同一张图；不能画成连续漫画或多重残影')
        if not is_ending_frame:
            parts.append('动作间隔锁定：当前分镜和下一分镜相隔约4秒，本图不要提前画下一镜结果')
    if any(k in text for k in ['跃步闪避龙爪', '侧跃避开龙爪', '跃离爪影', '横闪', '龙爪压向焦土', '前爪从画面左侧砸向裂地']):
        parts.append('闪避镜锁定：只表现勇者横向跃步躲开龙爪落点，龙爪与勇者之间必须有明显空隙；禁止同时画格挡、反击或站稳收势')
    if any(k in text for k in ['横剑硬接冲击', '横剑承住冲击', '横剑顶住气浪', '硬接龙爪', '横持同一把圣剑']):
        parts.append('格挡镜锁定：只表现圣剑承受冲击，剑身火星、脚下短拖痕、披风受力；禁止画成低身斩击、圣剑拄地或轻松站桩')
    if any(k in text for k in ['砍中远古巨龙左前爪', '砍中', '接触点炸开', '前爪外侧关节', '前爪鳞甲']):
        parts.append('命中镜锁定：只表现剑刃砍中左前爪外侧关节的峰值帧，火星和碎鳞从接触点爆开；禁止同时画成战后收尾、巨龙倒地死亡或勇者垂剑胜利')
    if is_ending_frame or any(k in text for k in ['收回受伤', '拉开距离', '暂时占上风']):
        parts.append('收尾镜锁定：最后一镜只表现打完后的结果，巨龙收回受伤前爪并后退，勇者持剑警戒；不要再画剑正在命中，不要画成屠龙尸体')

    relation = (shot.get('relationship_type') or '').strip().lower()
    camera = (shot.get('camera_angle') or '').strip()
    is_back_low_angle = camera == '仰视' or any(k in text for k in ['背后', '背影', '肩后', '仰望', '天空中', '体型差'])
    is_ending = any(k in text for k in ['战斗结束', '胜负已分', '倒地', '倒下', '失去攻势', '收剑', '烟尘落下', '退却'])
    is_mounted = relation == 'mounted' or any(k in text for k in ['龙背', '背脊', '背上', '骑在', '承载'])

    if human and creature and is_back_low_angle and not is_mounted and not is_ending:
        parts.append(f'构图必须是低机位背后/肩后仰视：前景下方只有较小的{human}背影或背肩轮廓，远处或天空中只有巨大的{creature}占据画面上半部，突出压迫感和体型差')
    if is_mounted and creature:
        parts.append(f'本镜是承载/龙背关系：{creature}是唯一承载主体，人物在其背部/身体上方，禁止同时画倒地{creature}或对面第二条{creature}')
    if is_ending and creature:
        parts.append(f'本镜是结果/收束状态：{creature}必须失去攻势、倒地、退却或低伏，不得继续站立咆哮对峙；背景不得再出现飞行或站立的第二条{creature}')
    if any(k in text for k in ['占据上风', '击伤', '受伤前肢', '斩过前肢', '前肢鳞甲']):
        parts.append('本镜表达第一轮交锋后占据上风，不是最终击杀：巨龙可以吃痛、后仰、低伏或后退，但不能死亡、断头、尸体化或完全消失')
    if any(k in text for k in ['低身蓄斩起手', '拉剑蓄势', '剑尖贴近地面白光聚拢']):
        parts.append('蓄斩起手镜锁定：只画勇者低身拉剑、剑尖贴近地面聚光，巨龙前爪在侧前方暴露破绽但尚未被击中；禁止命中火花、禁止巨龙后退、禁止收剑结尾')
    if scene_zh:
        parts.append(f'背景只延续纯环境“{scene_zh[:32]}”，不要把背景烟云或山影画成第二个命名怪物')
    return '战斗画面硬性护栏：' + '；'.join(parts) if parts else ''

def _build_seedream_prompt(shot, out_chars, art_style="日漫",
                            scene_spec_zh="", global_tone="",
                            use_ref_images=True):
    """★ v12 修复: 构建 Seedream 5.0 Lite 的自然语言 prompt

    v12 新增修复:
      1. 参考图模式下强制写明【每个角色的衣着关键词】, 避免衣服错位/串色
      2. 强调【背景不得为纯色/留白】, 必须按 scene_description 生成
      3. 场景描述提到前半部分, 避免模型注意力衰减到角色细节

    v11 原修复:
      - 使用参考图时, prompt 重点描述变化(动作/表情/场景调整)
      - 不使用参考图时, 完整描述外貌
    """
    # 优先使用 Seedream 首帧图提示词；jimeng_ref_prompt 仅作旧兼容字段
    jimeng_prompt = _authoritative_image_prompt(shot).strip()
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

        # v14: 场景/道具/朝向锁，必须在安全指令之前就进入 prompt
        v14_lock = _build_scene_prop_orientation_lock_v14(shot)
        if v14_lock and v14_lock not in prompt:
            prompt = v14_lock + '。' + prompt

        scene_zh = _clean_scene_text_for_prompt_v26(shot.get('scene_description', '') or scene_spec_zh)
        battle_guardrail = _battle_image_guardrail_v27(shot, out_chars, scene_zh)
        if battle_guardrail:
            prompt = battle_guardrail + '。' + prompt

        # ★ v19: Seedream 5.0 Lite 防复制/防姿势锁死安全指令，必须放最前面
        safety_directive = _reference_safety_directive(shot, out_chars)
        if safety_directive:
            prompt = safety_directive + '。' + prompt

        # ★ v14.6 关键修复：主体复制（subject duplication）问题
        # Seedream 5.0 Lite 看到"居中立绘参考图"时，也可能把立绘的对称感复制到画面
        # 典型症状：1 个角色变 6-7 个横排；1 条龙变对称双胞胎
        # 解决方案：在 prompt 最前面明确数量 + 禁止复制
        if use_ref_images and out_chars:
            subject_count_parts = []
            for c in out_chars:
                cname = c.get('name', '')
                if cname:
                    subject_count_parts.append(f"命名角色{cname}只出现1次")
            if subject_count_parts:
                # 前置数量约束：放在 prompt 最前面让模型优先看到
                count_directive = (
                    "，".join(subject_count_parts)
                    + "，绝对禁止复制、镜像或多个相同命名角色并排出现"
                    + "，禁止同一命名角色同时出现大头特写和全身小人"
                    + "，参考图仅用于外观识别不得复制其对称构图"
                    + "，允许合理的非命名背景元素弱化存在，但不得抢占主体或变成第二个命名角色"
                )
                # 单角色的特殊强化：明确单个主体占据画面
                if len(out_chars) == 1:
                    cname = out_chars[0].get('name', '')
                    count_directive += (
                        f"。{cname}作为唯一主体自然地处于画面中，"
                        f"不得在画面中出现第二个、第三个或任何重复的命名角色{cname}"
                    )
                prompt = count_directive + "。" + prompt

        # ★ v11/v12: 使用参考图时追加强约束
        if use_ref_images:
            action_zh = shot.get('action_detail_zh') or shot.get('action_zh', '') or shot.get('action', '')
            shot_type = shot.get('shot_type', '')
            emphasis_parts = []
            if action_zh:
                emphasis_parts.append(f"请严格按照描述展现动作：{action_zh}")
                emphasis_parts.append('动作镜要明显改变姿态，不能把命名角色做成正面站桩；但不得复制命名角色数量')
                v14_extra = _build_scene_prop_orientation_lock_v14(shot)
                if v14_extra:
                    emphasis_parts.append(v14_extra)
                if any(k in action_zh for k in ['龙背', '骑', '背上']):
                    emphasis_parts.append('本镜是承载/龙背高光镜，巨龙保持单一承载主体，不要同时出现倒地巨龙或第二条巨龙')
                if any(k in action_zh for k in ['倒地', '倒下', '倒在']):
                    emphasis_parts.append('本镜是结尾倒地状态，只能出现一条倒地巨龙，背景里不要再出现飞行或站立的巨龙')
            if shot_type:
                emphasis_parts.append(f"使用{shot_type}构图")
            camera_angle = (shot.get('camera_angle') or '').strip()
            if camera_angle:
                angle_map = {
                    '仰视': '镜头角度锁定：低机位仰视，强调巨大主体的压迫感和高度差',
                    '俯视': '镜头角度锁定：高机位俯视，强调空间布局和位置关系',
                    '平视': '镜头角度锁定：平视，保持角色与环境关系自然稳定',
                    '侧面': '镜头角度锁定：侧面视角，强调动作方向和轮廓线'
                }
                emphasis_parts.append(angle_map.get(camera_angle, f'镜头角度锁定：{camera_angle}'))
            if shot.get('scene_change'):
                emphasis_parts.append("这是新场景，背景完全不同于之前")

            # ★ v12 新增: 逐个角色锁衣服, 防止多角色衣服错位
            if out_chars:
                outfit_locks = []
                for c in out_chars:
                    cname = c.get('name', '')
                    cdesc = _normalize_character_desc_for_doubao(cname, c.get('description', '') or '')
                    outfits = _extract_outfit_keywords(cdesc)
                    if outfits:
                        outfit_locks.append(
                            f"{cname}严格穿着{'/'.join(outfits)}"
                        )
                if outfit_locks:
                    emphasis_parts.append('服装/武器锁定：' + '，'.join(outfit_locks))
                identity_locks = shot.get('identity_locks') or []
                if isinstance(identity_locks, str):
                    identity_locks = [identity_locks]
                if identity_locks:
                    emphasis_parts.append('角色身份锁定：' + '；'.join(str(x)[:180] for x in identity_locks if x))
                emphasis_parts.append("必须同时参考本镜提供的全部角色/场景参考图；所有角色必须保持与参考图一致的脸型、发型、服装配色、体型和武器样式，禁止跨镜改变造型或重新设计角色")

            # ★ v14.6: 多角色空间关系约束，不再一律强制左右对峙
            if len(out_chars) >= 2:
                names = [c.get('name', '') for c in out_chars]
                action_text = (shot.get('action_zh', '') or '') + (shot.get('scene_description', '') or '') + (_authoritative_image_prompt(shot) or '')
                relation = _detect_spatial_relation_from_shot(shot)

                if relation == 'mounted':
                    rider, carrier = _pick_mounted_roles(names, action_text)
                    facing = (
                        f"★画面中只有1个{carrier}，{carrier}作为巨大承载主体横贯画面下方或占据画面主体，"
                        f"{rider}位于{carrier}背部/身体上方/承载位置，二者朝向同一方向，"
                        f"不是左右对峙关系；禁止在画面对面生成第二个{carrier}，禁止复制{carrier}★"
                    )
                elif relation == 'same_side':
                    facing = f"★{'、'.join(names)}并肩或同向行动，朝向同一方向，禁止写成左右对峙★"
                elif relation == 'chase':
                    facing = f"★保持追击空间关系：被追者在前方，追者在后方，二者沿同一方向运动，不要面对面对峙★"
                elif relation == 'confront':
                    a, b = names[0], names[1] if len(names) > 1 else ''
                    creature_name = ''
                    human_name = ''
                    for c in out_chars:
                        if _is_creature_char(c) and not creature_name:
                            creature_name = c.get('name', '')
                        elif not _is_creature_char(c) and not human_name:
                            human_name = c.get('name', '')
                    if creature_name and human_name:
                        facing = (
                            f"★{human_name}与{creature_name}是1人对1龙的对峙构图：画面中只允许出现1个{human_name}和1条{creature_name}。"
                            f"{creature_name}只能作为一个完整主体出现，可见部分可以是同一条龙的头颈+前肢/半身，但绝对禁止再生成第二颗龙头、第二段龙身、左右各一条龙夹击或镜像双龙。"
                            f"{human_name}与{creature_name}分居画面两侧并侧身相对，二者都不要正面朝向镜头；若动作未指定左右，不要强行固定左右，只需保持相对朝向清楚★"
                        )
                    else:
                        facing = (
                            f"★{a}和{b}分居画面两侧，二者侧身相对、视线交汇；"
                            f"不要让二者都正面朝向镜头；若动作未指定左右，不要强行固定左右，只要保持相对朝向清楚★"
                        )
                else:
                    facing = f"★{'、'.join(names)}保持画面中明确空间关系，不要复制命名角色，不要无依据地改成左右对峙；背景元素允许弱化存在但不得抢占主体★"
                emphasis_parts.append(facing)

            # v25: 巨大生物对峙镜的背后视角/仰望构图强化
            camera_angle = (shot.get('camera_angle') or '').strip()
            action_text_full = (shot.get('action_zh', '') or '') + (_authoritative_image_prompt(shot) or '')
            if len(out_chars) >= 2 and relation == 'confront':
                human_name = ''
                creature_name = ''
                for c in out_chars:
                    if _is_creature_char(c) and not creature_name:
                        creature_name = c.get('name', '')
                    elif not _is_creature_char(c) and not human_name:
                        human_name = c.get('name', '')
                if human_name and creature_name and (camera_angle == '仰视' or any(k in action_text_full for k in ['背后', '背影', '肩后', '仰望', '天空中'])):
                    emphasis_parts.append(
                        f'构图锁定：镜头位于{human_name}身后或肩后，前景下方只露出较小体量的{human_name}背影/背肩轮廓，远处或天空中的{creature_name}占据画面上半部，明确表现1人对1龙的巨大体型差；禁止把双方做成并排同大小对打'
                    )

            # ★ v14.5: 背景锁定（精简版，减少 prompt 长度）
            scene_zh = _clean_scene_text_for_prompt_v26(shot.get('scene_description', '') or scene_spec_zh)
            if scene_zh:
                emphasis_parts.append(f"背景：{scene_zh[:50]}，禁止白色/纯色背景；若提供了场景参考图，必须延续其地形、地表纹理、天空状态和主色调")
                if not shot.get('scene_change'):
                    emphasis_parts.append("若与上一镜属于同一场景，延续相同的地形、光线、天气和主色调，不要随意更换背景")
            else:
                emphasis_parts.append("禁止白色/纯色背景")

            battle_guardrail = _battle_image_guardrail_v27(shot, out_chars, scene_zh)
            if battle_guardrail:
                emphasis_parts.append(battle_guardrail)

            if emphasis_parts:
                prompt += "，" + "，".join(emphasis_parts)

        if '高质量' not in prompt and '质量' not in prompt:
            prompt += "，画面精致，色彩丰富，完整背景，无留白，高清高质量，不要角色卡片，不要三视图，不要同一命名主体重复出现，不要正面证件照式站桩，不要看向镜头摆拍"

        if len(out_chars) > 1:
            names = "、".join(c.get('name', '') for c in out_chars)
            if '左' not in prompt and '右' not in prompt:
                prompt += f"，画面中{names}外貌和服装明显不同，每个角色特征清晰可辨"

        for c in out_chars:
            prompt = _normalize_youth_terms_for_prompt(prompt, c.get('name', ''))
        return prompt

    # ── Fallback: 系统自动构建 ──
    style_prefix = STYLE_ZH_PREFIX.get(art_style) or (
        f"{art_style}风格精致插画" if art_style else STYLE_ZH_PREFIX["日漫"]
    )
    parts = [style_prefix]
    v14_lock = _build_scene_prop_orientation_lock_v14(shot)
    if v14_lock:
        parts.append(v14_lock)

    # ★ v12: 场景放在风格之后、角色之前, 保证注意力覆盖
    scene_zh = _clean_scene_text_for_prompt_v26(shot.get('scene_description', '') or scene_spec_zh)
    if scene_zh:
        parts.append(f"场景环境：{scene_zh}")
        if not shot.get('scene_change'):
            parts.append("若与上一镜同场景，延续相同地形、天气、光线和主色调")

    battle_guardrail = _battle_image_guardrail_v27(shot, out_chars, scene_zh)
    if battle_guardrail:
        parts.append(battle_guardrail)

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

    camera_angle = (shot.get('camera_angle') or '').strip()
    if camera_angle == '仰视':
        parts.append('低机位仰视镜头，强调高度差与压迫感')
    elif camera_angle == '俯视':
        parts.append('高机位俯视镜头，清晰展示空间布局')
    elif camera_angle == '侧面':
        parts.append('侧面视角，强调动作方向和人物轮廓')
    elif camera_angle == '平视':
        parts.append('平视镜头，关系稳定自然')

    if global_tone:
        parts.append(global_tone)

    if len(out_chars) > 1:
        names = "、".join(c.get('name', '') for c in out_chars)
        count_word = "两人" if len(out_chars) == 2 else f"{len(out_chars)}人"
        parts.append(f"本镜命名角色为{names}共{count_word}，每个命名角色只出现一次，外貌和服装明显不同")
        parts.append(f"这些命名角色各只有1个，绝对禁止复制或镜像；允许符合场景的非命名背景元素弱化存在，但不能抢占主体或复制命名角色")
        parts.append("所有角色在本镜中保持与既有设定一致的发型、服装配色、体型比例和武器样式")
    elif len(out_chars) == 1:
        cname = out_chars[0].get('name', '')
        parts.append(f"本镜命名角色只有1个{cname}，绝对禁止复制、镜像或多个相同命名角色并排出现；必须严格沿用参考图中的同一张脸、同一发型、同一服装和同一武器；允许合理背景元素弱化存在")
        parts.append(f"{cname}必须保持稳定发型、服装配色、体型和武器样式")

    action_desc = shot.get('action_detail_zh') or shot.get('action_zh', '') or shot.get('action', '')
    if action_desc:
        parts.append(f"本镜头唯一主要动作：{action_desc}，角色姿态必须与动作一致，不要复制正面站立立绘姿势")

    emotion = shot.get('emotion', '')
    if emotion:
        parts.append(f"人物情绪：{emotion}")

    positions = ["画面左侧", "画面右侧", "画面中间", "画面前方", "画面后方"]
    for i, c in enumerate(out_chars):
        cname = c.get('name', '')
        cdesc = _normalize_character_desc_for_doubao(cname, c.get('description', '') or "")
        appearance = _extract_full_appearance(cdesc, cname)
        # ★ v12: 强调服装锁定
        outfits = _extract_outfit_keywords(cdesc)
        outfit_note = f"，严格穿着{'/'.join(outfits)}" if outfits else ""
        pos = positions[i] if i < len(positions) else ""
        if len(out_chars) > 1:
            parts.append(f"{pos}是{cname}（{appearance}{outfit_note}）")
        else:
            parts.append(f"主角是{cname}（{appearance}{outfit_note}），必须保持同一套服装、同一发型和同一道具/武器")

    if len(out_chars) >= 2:
        human_name = ''
        creature_name = ''
        for c in out_chars:
            if _is_creature_char(c) and not creature_name:
                creature_name = c.get('name', '')
            elif not _is_creature_char(c) and not human_name:
                human_name = c.get('name', '')
        action_text_full = (shot.get('action_zh', '') or '') + (_authoritative_image_prompt(shot) or '')
        relation = _detect_spatial_relation_from_shot(shot)
        if human_name and creature_name and relation == 'confront' and (camera_angle == '仰视' or any(k in action_text_full for k in ['背后', '背影', '肩后', '仰望', '天空中'])):
            parts.append(f'镜头在{human_name}身后或肩后，前景下方只保留较小体量的{human_name}背影或背肩轮廓，远处或天空中的{creature_name}巨大地占据画面上半部，强调明显体型差和压迫感，禁止并排同大小对打')

    if not out_chars:
        parts.append("无人物，展示场景环境细节")

    # ★ v12: 背景强约束放末尾(作为强化)
    parts.append("背景环境必须完整详细，绝不使用白色或纯色留白")
    parts.append("画面精致，色彩丰富，完整背景，高清高质量，不要角色卡片，不要三视图，不要同一主体重复出现，不要正面证件照式站桩，不要看向镜头摆拍")

    prompt = "，".join(p for p in parts if p)
    for c in out_chars:
        prompt = _normalize_youth_terms_for_prompt(prompt, c.get('name', ''))
    return prompt


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
    if engine != "doubao":
        print(f"[分镜] ⚠ engine={engine} 已废弃，统一改用豆包 Seedream 中文主链路")
        engine = "doubao"

    if engine == "doubao":
        use_refiner = _should_use_prompt_refiner(shot)
        effective_reference_mode = reference_mode
        if use_refiner and effective_reference_mode in ('guide', 'strong', '', None):
            # 动作纠偏/用户重绘优先保证姿态，不让旧参考图复制姿势。
            effective_reference_mode = 'off'

        ref_images = _collect_reference_images(
            shot=shot,
            out_chars=out_chars,
            prev_shot_image=prev_shot_image,
            all_chars=all_c,
            panorama_views=panorama_views,
            reference_mode=effective_reference_mode,
            is_first_shot=is_first_shot,
        )

        if use_refiner:
            prompt = _build_seedream_prompt_refined_zh(
                shot, out_chars, art_style, scene_spec_zh, global_tone
            )
        else:
            prompt = _build_seedream_prompt(
                shot, out_chars, art_style, scene_spec_zh, global_tone,
                use_ref_images=bool(ref_images),
            )

        _warn_if_seedream_prompt_englishish(prompt)

        print(f"\n[分镜] ┌─ 豆包 Seedream 中文主链路 ──────────")
        print(f"[分镜] │ shot_type: {shot.get('shot_type','')}")
        print(f"[分镜] │ scene_change: {shot.get('scene_change', False)}")
        print(f"[分镜] │ prompt_refiner: {use_refiner}")
        print(f"[分镜] │ reference_mode: {effective_reference_mode}")
        print(f"[分镜] │ chars: {[c.get('name') for c in out_chars]}")
        print(f"[分镜] │ refs: {len(ref_images)} 张参考图")
        print(f"[分镜] │ prompt({len(prompt)}字): {prompt[:220]}...")
        print(f"[分镜] └──────────────────────────────\n")

        result = _doubao_generate(
            prompt=prompt,
            ref_image_urls=ref_images if ref_images else None,
            size=quality,
        )
        if result.get("success"):
            return result
        print(f"[分镜] Seedream 生成失败，不再切换旧图像入口: {result.get('message','')}")
        return result

    # 非豆包 engine 已废弃，统一走豆包。
    return generate_storyboard_image(
        shot=shot, char_refs=char_refs, art_style=art_style, style=style,
        global_tone=global_tone, scene_spec=scene_spec, scene_spec_zh=scene_spec_zh,
        scene_views=scene_views, panorama_views=panorama_views, all_chars=all_chars,
        project_id=project_id, prev_shot_image=prev_shot_image, engine="doubao",
        quality=quality, reference_mode=reference_mode, is_first_shot=is_first_shot,
    )

    # ══════════════════════════════════
    # 旧图像分镜 (旧代码保留但主流程不可达)
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

    # 旧图像入口的参考图使用策略保持原样
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

# v11 dragon size lock note: 远古巨龙必须是巨型压迫感主体，体型至少为少年勇者8倍以上，宽阔双翼展开占据画面大面积空间，粗壮四肢、巨大龙爪、长尾、弯曲黑角、红黑翼膜清晰，不是小龙、幼龙、普通飞龙或人形怪物；需要呈现庞大体积、厚重鳞片和强烈压迫感。
