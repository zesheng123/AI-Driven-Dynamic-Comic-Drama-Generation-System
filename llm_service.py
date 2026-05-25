"""
llm_service.py — 豆包大模型服务 (v13 升级版)
═══════════════════════════════════════════════════════
v13 核心升级:
  ✦ 场景连续性铁律: 相邻分镜场景不跳变, 后处理自动注入前镜场景前缀
  ✦ 尺度锚定铁律: 多角色体型比例必须跨镜一致
  ✦ 动作节奏铁律: video_prompt 必须写"前X%/中Y%/后Z%"节奏分配
  ✦ 景别节奏铁律: 全片景别必须有变化曲线, 不允许连续3镜同景别
  ✦ duration_hint 范围扩展到 4-12 秒
原有特性:
  ✦ 角色朝向铁律 (v12)
  ✦ 外貌一致性铁律 (v10)
  ✦ 剧本生成/角色提取/场景规范/容错解析
"""
import json, re, os
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# v28: 语言模型默认切换到豆包 Seed 2.0 Lite。
# 优先读环境变量，方便部署时不把密钥写进仓库；没有环境变量时使用本地演示默认值。
_DEFAULT_ARK_API_KEY = "58d47ba2-b181-44a7-9377-1fb9c6a6575a"
_DOUBAO_API_KEY  = (
    os.getenv("ARK_API_KEY")
    or os.getenv("ARK_SEED2_LITE_KEY")
    or os.getenv("DOUBAO_API_KEY")
    or os.getenv("ARK_API_KEY_DIRECT")
    or _DEFAULT_ARK_API_KEY
)
_DOUBAO_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
# 如果你的方舟控制台给的是 ep-xxxx 接入点 ID，请把 ARK_LLM_MODEL 设置成该接入点。
_DEFAULT_SEED2_LITE_MODEL = "doubao-seed-2-0-lite-250428"
_FALLBACK_LLM_MODEL = os.getenv("ARK_LLM_FALLBACK_MODEL", "doubao-1-5-lite-32k-250115")
_DOUBAO_MODEL    = (
    os.getenv("ARK_LLM_MODEL")
    or os.getenv("ARK_SEED2_LITE_MODEL")
    or os.getenv("DOUBAO_LLM_MODEL")
    or _DEFAULT_SEED2_LITE_MODEL
)


# ═══════════════════════════════════════════════════════
# 底层 LLM 调用
# ═══════════════════════════════════════════════════════
def _call_llm(system, user, temperature=0.7, max_tokens=4000, json_mode=False, task_name=''):
    """统一 LLM 调用。
    v28:
    - 默认模型切换到 Seed 2.0 Lite；
    - 支持 json_mode，减少剧本/角色 JSON 包裹和截断问题；
    - 如果 Seed 2.0 Lite 模型名/接入点未开通，可自动尝试回退模型，避免整条链路中断。
    """
    if not _DOUBAO_API_KEY:
        return {"success": False, "message": "缺少方舟 API Key：请设置 ARK_API_KEY 或 ARK_SEED2_LITE_KEY"}
    if OpenAI is None:
        return {"success": False, "message": "当前环境缺少新版 openai 包，请执行 pip install -U openai"}

    client = OpenAI(api_key=_DOUBAO_API_KEY, base_url=_DOUBAO_BASE_URL)
    model_candidates = []
    for m in [_DOUBAO_MODEL, _FALLBACK_LLM_MODEL]:
        if m and m not in model_candidates:
            model_candidates.append(m)

    last_err = None
    for model_name in model_candidates:
        kwargs = dict(
            model=model_name,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**kwargs)
            content = resp.choices[0].message.content
            return {"success": True, "content": content, "model": model_name}
        except Exception as e:
            last_err = e
            msg = str(e)
            # 部分方舟兼容接口不支持 response_format：自动去掉 JSON mode 重试当前模型。
            if json_mode and ('response_format' in msg or 'unsupported' in msg.lower() or 'not support' in msg.lower()):
                try:
                    kwargs.pop("response_format", None)
                    resp = client.chat.completions.create(**kwargs)
                    content = resp.choices[0].message.content
                    return {"success": True, "content": content, "model": model_name}
                except Exception as e2:
                    last_err = e2
            print(f"[LLM] 模型 {model_name} 调用失败: {last_err}")
            continue
    return {"success": False, "message": str(last_err)}


def _parse_json(text):
    """鲁棒 JSON 解析 — 处理 Markdown 包裹/前后废话/半角全角混用/★截断挽救"""
    if not text:
        raise ValueError("空响应")
    text = text.strip()
    # 剥离 Markdown 代码块
    for tag in ['```json', '```JSON', '```']:
        text = text.replace(tag, '')
    text = text.strip()

    # ── 先走常规路径 ──
    for s, e in [('[', ']'), ('{', '}')]:
        i, j = text.find(s), text.rfind(e)
        if i != -1 and j != -1 and j > i:
            chunk = text[i:j + 1]
            try:
                return json.loads(chunk)
            except json.JSONDecodeError:
                fixed = re.sub(r',(\s*[}\]])', r'\1', chunk)
                try:
                    return json.loads(fixed)
                except Exception:
                    continue

    # ── 再试整串 ──
    try:
        return json.loads(text)
    except Exception:
        pass

    # ── ★ v12: 截断挽救 —— 当数组被截断时尝试保留已完整的对象 ──
    # 典型场景：LLM 输出到一半 token 用完，最后一个 {} 没闭合
    if text.startswith('['):
        # 扫描直到找到最后一个完整的 }，之后截断+补 ]
        depth = 0
        last_complete = -1
        in_str = False
        esc = False
        for idx, ch in enumerate(text):
            if esc:
                esc = False
                continue
            if ch == '\\':
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    last_complete = idx
        if last_complete > 0:
            salvage = text[:last_complete + 1] + ']'
            # 去掉可能的尾部逗号
            salvage = re.sub(r',(\s*\])', r'\1', salvage)
            try:
                result = json.loads(salvage)
                print(f"[json] ★ 截断挽救成功, 保留 {len(result)} 个完整对象")
                return result
            except Exception as e:
                print(f"[json] 截断挽救失败: {e}")

    raise ValueError("JSON 解析失败且无法挽救")


# ═══════════════════════════════════════════════════════
# 0. 剧本生成 ★ 强化画面感
# ═══════════════════════════════════════════════════════
def _extract_json_string_field(raw: str, field: str) -> str:
    """从不完整 JSON 文本中尽量提取某个字符串字段。仅作最终兜底。"""
    if not raw:
        return ""
    m = re.search(r'"' + re.escape(field) + r'"\s*:\s*"', raw)
    if not m:
        return ""
    i = m.end()
    out = []
    esc = False
    while i < len(raw):
        ch = raw[i]
        if esc:
            if ch == 'n':
                out.append('\n')
            elif ch == 't':
                out.append('\t')
            else:
                out.append(ch)
            esc = False
        elif ch == '\\':
            esc = True
        elif ch == '"':
            break
        else:
            out.append(ch)
        i += 1
    return ''.join(out).strip()


def _word_count_en(text: str) -> int:
    return len(re.findall(r"[A-Za-z]+(?:[-\'][A-Za-z]+)?", text or ""))


def _contains_enough_chinese(text: str) -> bool:
    raw = str(text or '').strip()
    if not raw:
        return False
    cn = len(re.findall(r'[一-鿿]', raw))
    total = len(re.sub(r'\s+', '', raw)) or 1
    return cn >= 8 and (cn / total) >= 0.25


def _fallback_scene_spec_zh(global_scene: str = '', story_text: str = '', scene_spec_en: str = '') -> str:
    candidates = [global_scene, story_text, scene_spec_en]
    merged = ' '.join(str(x or '') for x in candidates)
    anchor = _purify_scene_anchor_v26(merged, characters=None)
    if anchor:
        return anchor[:40]

    low = str(scene_spec_en or '').lower()
    parts = []
    if any(k in low for k in ['charred', 'scorched', 'burnt', 'burned']):
        parts.append('焦黑平原')
    elif any(k in low for k in ['plain', 'wasteland', 'field']):
        parts.append('荒芜平原')
    if any(k in low for k in ['crack', 'fissure']):
        parts.append('焦土裂痕清晰')
    if any(k in low for k in ['smoke', 'ash', 'dust', 'haze']):
        parts.append('低空烟尘与灰烬翻涌')
    if any(k in low for k in ['sunset', 'dusk', 'evening', 'afterglow']):
        parts.append('傍晚斜阳压低')
    if any(k in low for k in ['cloud', 'storm']):
        parts.append('厚重云层低压')
    if not parts:
        if any(k in str(story_text or '') for k in ['龙', '巨龙', '勇者', '圣剑', '裂缝', '余晖']):
            parts = ['焦黑平原', '焦土裂痕延伸', '余晖与烟尘并存']
        else:
            parts = ['同一主场景', '光线和天气保持连续', '环境细节清晰']
    return '，'.join(parts[:3])[:40]


def _infer_story_keywords(story_text: str) -> dict:
    s = str(story_text or "")
    return {
        "has_sword": any(k in s for k in ["剑", "圣剑", "长剑", "宝剑"]),
        "has_dragon": any(k in s for k in ["龙", "巨龙", "古龙"]),
        "has_cloak": any(k in s for k in ["披风", "斗篷", "长袍"]),
        "has_wind": any(k in s for k in ["风", "狂风", "风沙"]),
        "has_cracks": any(k in s for k in ["裂缝", "裂痕", "裂开"]),
        "has_sunset": any(k in s for k in ["夕阳", "余晖", "黄昏"]),
    }


def _is_youth_male_role(name: str, desc: str, story_text: str = '') -> bool:
    s = f"{name} {desc} {story_text}".lower()
    return (
        any(k in str(name) for k in ['少年', '勇者', '男主', '青年'])
        or any(k in s for k in ['young boy', 'teenage boy', 'teen boy', 'young male', 'hero', 'brave boy'])
    ) and not any(k in str(name) for k in ['少女', '女孩', '女主'])


def _normalize_youth_male_description(desc: str, name: str, story_text: str = '') -> str:
    """避免“少年”被翻译成儿童 boy，统一为 17-19 岁青年勇者。"""
    if not _is_youth_male_role(name, desc, story_text):
        return desc
    d = re.sub(r'\byoung\s+boy\b', 'young male hero around 18 years old', desc, flags=re.I)
    d = re.sub(r'\bteenage\s+boy\b', 'young male hero around 18 years old', d, flags=re.I)
    d = re.sub(r'\bteen\s+boy\b', 'young male hero around 18 years old', d, flags=re.I)
    d = re.sub(r'\blittle\s+boy\b', 'young male hero around 18 years old', d, flags=re.I)
    d = re.sub(r'\bboy\b', 'young male hero', d, flags=re.I)
    d = re.sub(r'\bchild\b|\bchildren\b|\bkid\b|\bkids\b', 'young adult', d, flags=re.I)
    if not re.search(r'\b(17|18|19|seventeen|eighteen|nineteen)\b', d, flags=re.I):
        d = d.rstrip(' ,.') + ', around 18 years old'
    if not re.search(r'\b(not a child|young adult|late teenage|adult)\b', d, flags=re.I):
        d = d.rstrip(' ,.') + ', late-teen young adult proportions, not a child'
    return re.sub(r'\s+', ' ', d).strip(' ,;')



def _is_chinese_character_desc(desc: str) -> bool:
    """判断角色描述是否以中文为主。豆包主链路直接使用中文描述，不再强制英文化。"""
    if not desc:
        return False
    cn = len(re.findall(r'[一-鿿]', desc))
    total = len(re.sub(r'\s+', '', desc)) or 1
    return cn / total >= 0.25


def _enrich_character_description_zh(name: str, desc: str, story_text: str = '', genre: str = '') -> str:
    """豆包中文主链路：补足中文角色外貌锁，避免跨镜换脸/换衣/换武器。
    v24 hotfix:
    - 角色物种判断只基于“角色名 + 角色自身描述”，不再把整段 story_text 混进去。
    - 修复：剧情里出现“巨龙/翅膀/角”等词时，把人类主角误判成非人类，导致勇者也长角长翅膀。
    """
    desc = re.sub(r'\s+', ' ', (desc or '').strip(' ，,；;'))
    story = str(story_text or '')
    entity_text = f'{name} {desc}'
    context_text = f'{name} {desc} {story}'

    creature_keys = ['龙', '巨龙', '古龙', '怪物', '巨兽', '魔物', '兽', '机器人', '机甲', '幽灵', '妖']
    dragon_keys = ['龙', '巨龙', '古龙']
    human_keys = ['少年', '勇者', '骑士', '男主', '青年', '少女', '女孩', '男孩', '学生', '老师', '主角']

    # 关键修复：只根据角色自身信息判断是不是非人类，不看整段故事上下文。
    is_creature = any(k in entity_text for k in creature_keys) and not any(k in name for k in human_keys)
    is_dragon_like = any(k in entity_text for k in dragon_keys) and not any(k in name for k in human_keys)

    if is_creature:
        add = []
        if not any(k in desc for k in ['鳞', '鳞片', '金属', '皮毛', '外壳', '装甲']):
            add.append('体表材质清晰')
        if not any(k in desc for k in ['眼', '瞳']):
            add.append('眼睛颜色固定')
        if is_dragon_like:
            if '翅' not in desc:
                add.append('巨大翅膀')
            if '角' not in desc:
                add.append('头部长角')
            if '尾' not in desc:
                add.append('长尾')
            if '爪' not in desc:
                add.append('锋利巨爪')
        if add:
            desc = (desc + '，' if desc else '') + '，'.join(add)
        if '每个镜头保持一致' not in desc:
            desc += '，每个镜头保持同一体型比例、同一体表纹理和同一轮廓'
        return desc[:220]

    add = []
    if any(k in context_text for k in ['少年', '勇者', '骑士', '男主', '青年']) and not any(k in desc for k in ['18岁', '十七', '十八', '十九', '青年', '不是儿童']):
        add.append('18岁左右青年男性，不是儿童，身形为青年比例')
    if not any(k in desc for k in ['发', '头发', '发型']):
        add.append('发型固定')
    if not any(k in desc for k in ['眼', '瞳']):
        add.append('眼神特征固定')
    if not any(k in desc for k in ['穿', '服', '衣', '裙', '袍', '甲', '铠', '披风', '制服', '校服']):
        if '奇幻' in genre or any(k in context_text for k in ['剑', '圣剑', '勇者', '骑士']):
            add.append('蓝白配色圣骑士轻甲，胸甲、肩甲、护腕、腰带、护腿、长靴和短披风完整统一')
        elif '校园' in genre or any(k in context_text for k in ['学校', '教室', '同学']):
            add.append('固定款式校服')
        else:
            add.append('标志性服装款式和配色固定')
    if any(k in context_text for k in ['圣剑', '长剑', '剑', '持剑', '握剑']) and not any(k in desc for k in ['剑', '武器']):
        add.append('始终手持同一把圣剑')
    if not any(k in desc for k in ['体型', '身形', '身材', '高挑', '娇小', '瘦', '壮']):
        add.append('体型比例固定')
    if add:
        desc = (desc + '，' if desc else '') + '，'.join(add)
    if '每个镜头保持一致' not in desc:
        desc += '，每个镜头保持同一张脸、同一发型、同一服装配色、同一道具和同一体型比例'
    return desc[:240]

def _enrich_character_description(name: str, desc: str, story_text: str = '', genre: str = '') -> str:
    desc = re.sub(r'\s+', ' ', (desc or '').strip())
    if _is_chinese_character_desc(desc):
        return _enrich_character_description_zh(name, desc, story_text=story_text, genre=genre)
    desc = _normalize_youth_male_description(desc, name, story_text)
    low = desc.lower()
    story_hints = _infer_story_keywords(story_text)
    is_creature = any(k in name for k in ['龙','兽','妖','怪']) or any(k in low for k in [
        'dragon','creature','monster','beast','demon','wolf','bird','serpent'
    ])

    if not is_creature:
        add = []
        if not any(k in low for k in ['eye','eyes']):
            add.append('clear determined eyes')
        if not any(k in low for k in ['hair','hairstyle']):
            add.append('neat short dark hair')
        if _is_youth_male_role(name, desc, story_text):
            # 避免奇幻勇者被画成紧身衣/连体战斗服
            desc = re.sub(r'(?:skin-tight|tight-fitting|tight fit|bodysuit|jumpsuit)', 'layered fantasy robe', desc, flags=re.I)
            desc = re.sub(r'tight\s+(?:combat\s+)?(?:suit|outfit|uniform|clothes|clothing|armor|leather\s+armor)', 'layered fantasy robe with light armor', desc, flags=re.I)
            low = desc.lower()
        if not any(k in low for k in ['robe','cloak','coat','jacket','armor','outfit','uniform','dress','tunic']):
            if '奇幻' in genre or story_hints['has_cloak'] or _is_youth_male_role(name, desc, story_text):
                add.append('blue and white holy knight-style light armor with a chest plate, shoulder guards, bracers, a leather belt, leg guards, calf-high boots, and a short cloak, holding a holy sword, not skin-tight, not a bodysuit')
            else:
                add.append('a clearly recognizable signature outfit')
        if story_hints['has_cloak'] and not any(k in low for k in ['cloak','cape','scarf']):
            add.append('a wind-blown cloak or scarf')
        if story_hints['has_sword'] and not any(k in low for k in ['sword','blade']):
            add.append('holding a glowing holy sword')
        if not any(k in low for k in ['build','slim','athletic','tall','short']):
            add.append('a slim youthful build')
        if not any(k in low for k in ['boots','belt','gloves']):
            add.append('boots and a simple belt')
        if add:
            desc = (desc.rstrip(' ,.') + ', ' if desc else '') + ', '.join(add)
    else:
        add = []
        if not any(k in low for k in ['scale','scales']):
            add.append('dark obsidian scales')
        if not any(k in low for k in ['eye','eyes']):
            add.append('glowing golden eyes')
        if not any(k in low for k in ['wing','wings']):
            add.append('huge leathery wings')
        if not any(k in low for k in ['horn','horns']):
            add.append('sharp curved horns')
        if not any(k in low for k in ['tail']):
            add.append('a long spiked tail')
        if not any(k in low for k in ['claw','claws']):
            add.append('heavy claws')
        if add:
            desc = (desc.rstrip(' ,.') + ', ' if desc else '') + ', '.join(add)

    desc = re.sub(r'\s+', ' ', desc).strip(' ,;')
    if _word_count_en(desc) < 26:
        if is_creature:
            extra = ' massive body silhouette, scarred wing edges, imposing ancient presence, consistent appearance across every shot'
        else:
            extra = ' recognizable facial features, stable costume colors, consistent hairstyle, the same weapon design in every shot'
        desc = (desc.rstrip(' ,.') + ',' + extra).strip(' ,;')
    return desc


def _enrich_scene_description(zh: str, en: str, story_text: str = '', genre: str = ''):
    zh = (zh or '').strip()
    en = (en or '').strip()
    hints = _infer_story_keywords(story_text)
    plain_hint = ('焦黑平原' in zh) or ('scorched plain' in en.lower()) or ('wasteland' in en.lower())
    if len(zh) < 12:
        extra_zh = []
        if '奇幻' in genre:
            extra_zh.append('空间开阔')
        if plain_hint:
            extra_zh.append('空气沉闷')
            extra_zh.append('灰烬散落')
        elif hints['has_wind']:
            extra_zh.append('空气流动感轻微')
        if hints['has_cracks']:
            extra_zh.append('地面裂痕清楚')
        if hints['has_sunset']:
            extra_zh.append('余晖映照')
        if extra_zh:
            zh = (zh + '，' if zh else '') + '，'.join(extra_zh)
    if len(en) < 8:
        extra_en = []
        if '奇幻' in genre:
            extra_en.append('wide fantasy environment')
        if plain_hint:
            extra_en.append('heavy still air')
            extra_en.append('scattered ash')
        elif hints['has_wind']:
            extra_en.append('subtle air movement')
        if hints['has_cracks']:
            extra_en.append('visible ground cracks')
        if hints['has_sunset']:
            extra_en.append('sunset rim light')
        if extra_en:
            en = (en + ', ' if en else '') + ', '.join(extra_en)
    return zh.strip('，, '), en.strip(' ,')


def _normalize_story_result(data, genre="", length="中篇"):
    """统一清理剧本生成结果，保证前端字段稳定。"""
    if not isinstance(data, dict):
        raise ValueError("剧本返回不是JSON对象")

    if not data.get('title'):
        data['title'] = '未命名动态漫剧'
    if not data.get('story_text'):
        raise ValueError("缺少story_text")

    data['story_text'] = re.sub(r'\n{3,}', '\n\n', str(data.get('story_text', '')).strip())
    data['genre'] = data.get('genre') or genre
    data['mood'] = data.get('mood') or ''

    # 兼容旧前端：保留字段，但不再要求LLM在剧本阶段输出复杂story_beats，避免JSON截断。
    if not isinstance(data.get('story_beats'), list):
        data['story_beats'] = []

    if not isinstance(data.get('characters'), list):
        data['characters'] = []
    if not isinstance(data.get('scenes'), list):
        data['scenes'] = []

    # 清理角色字段，防止缺字段导致前端报错。
    story_text_for_consistency = data.get('story_text', '')
    clean_chars = []
    for c in data.get('characters', []):
        if not isinstance(c, dict):
            continue
        name = str(c.get('name', '')).strip()
        desc = str(c.get('description', '')).strip()
        if not name or not desc:
            continue
        # 豆包中文主链路：保留中文外貌描述，不再强制英文化。
        desc = re.sub(r'\s+', ' ', desc).strip(' ，,;；')
        desc = _enrich_character_description(name, desc, story_text=story_text_for_consistency, genre=genre)
        clean_chars.append({
            'name': name,
            'description': desc,
            'personality': str(c.get('personality', '')).strip()[:20],
            'voice_style': str(c.get('voice_style', '')).strip()[:20],
        })
    data['characters'] = clean_chars

    # 场景描述保持纯环境，去除角色/生物/动作。
    char_names_for_purge = [c.get('name', '') for c in clean_chars if c.get('name')]
    clean_scenes = []
    for sc in data.get('scenes', []):
        if not isinstance(sc, dict):
            continue
        name = str(sc.get('name', '')).strip() or '主要场景'
        zh = str(sc.get('description_zh', '')).strip()
        en = str(sc.get('description_en', '')).strip()
        if zh:
            zh = _purge_characters_from_scene(zh, char_names_for_purge)
            zh = re.sub(r'\s+', '', zh).strip('，,。.;；')
        if en:
            # 英文场景也剥掉人物/生物词，保持纯环境。
            en = re.sub(
                r'\b(?:he|she|they|him|her|his|hers|them|their|boy|girl|man|woman|protagonist|character|hero|heroine|dragon|creature|monster|warrior|knight|person)\b[^.,;]*[.,;]?',
                '', en, flags=re.IGNORECASE)
            en = re.sub(r'\s+', ' ', en).strip(' ,;.')
        zh, en = _enrich_scene_description(zh, en, story_text=story_text_for_consistency, genre=genre)
        if not zh and not en:
            continue
        clean_scenes.append({
            'name': name[:12],
            'description_zh': zh or '主要场景，光线明确，环境细节清晰',
            'description_en': en or '',
            'time_of_day': str(sc.get('time_of_day', '')).strip() or 'daytime',
        })
    data['scenes'] = clean_scenes
    return data




def _split_story_paragraphs(story_text: str):
    """按段落/换行切分剧本文本，忽略空行。"""
    if not story_text:
        return []
    parts = [p.strip() for p in re.split(r'\n+', str(story_text).strip()) if p.strip()]
    return parts



# ═══════════════════════════════════════════════════════
# v14 数量/场景/道具一致性兜底
# ═══════════════════════════════════════════════════════
_CN_NUM_MAP = {
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10
}


def _normalize_length_mode_v14(length_mode: str) -> str:
    """兼容前端传来的“长篇 — 5~6个分镜”等非精确值。"""
    s = str(length_mode or '').strip()
    if not s:
        return ''
    if '长' in s or '5' in s or '6' in s:
        return '长篇'
    if '中' in s or '4' in s:
        return '中篇'
    if '短' in s or '3' in s:
        return '短篇'
    return s


def _cn_number_to_int_v14(s: str) -> int:
    s = str(s or '').strip()
    if s.isdigit():
        return int(s)
    if s in _CN_NUM_MAP:
        return _CN_NUM_MAP[s]
    if len(s) == 2 and s[0] == '十' and s[1] in _CN_NUM_MAP:
        return 10 + _CN_NUM_MAP[s[1]]
    if len(s) == 2 and s[1] == '十' and s[0] in _CN_NUM_MAP:
        return _CN_NUM_MAP[s[0]] * 10
    return 0


def _extract_numbered_story_segments_v14(story_text: str):
    """提取“第一镜/第1镜”这类已经导演化的文本。"""
    text = str(story_text or '').strip()
    if not text:
        return []
    pattern = r'(第\s*([一二三四五六七八九十0-9]+)\s*镜[：:，,\s]*)(.*?)(?=第\s*[一二三四五六七八九十0-9]+\s*镜[：:，,\s]*|$)'
    items = []
    for m in re.finditer(pattern, text, flags=re.S):
        idx = _cn_number_to_int_v14(m.group(2))
        body = re.sub(r'\s+', ' ', m.group(3)).strip(' ，,。')
        if body:
            items.append({'index': idx or len(items) + 1, 'text': body})
    if items:
        return items
    # 兼容每行一个镜头但没有空行的情况
    parts = [p.strip() for p in re.split(r'\n+', text) if p.strip()]
    if len(parts) >= 5:
        return [{'index': i + 1, 'text': re.sub(r'^第[一二三四五六七八九十0-9]+镜[：:，,\s]*', '', p)} for i, p in enumerate(parts)]
    return []


def _infer_scene_anchor_v14(scene_anchor: str = '', story_text: str = '', shots=None) -> str:
    """抽取全片主场景锚点，避免后续镜头从焦黑平原跳到草地/蓝天。"""
    candidates = []
    if scene_anchor:
        candidates.append(str(scene_anchor).strip())
    text = str(story_text or '')
    m = re.search(r'(焦黑|荒凉|废墟|雪原|森林|校园|教室|走廊|城市|街道|办公室|医院|古城|战场|末世)[^。；;\n，,]{0,28}(平原|荒原|废墟|雪原|森林|校园|教室|走廊|街道|办公室|医院|古城|战场|城市|山谷|海边)', text)
    if m:
        candidates.append(m.group(0))
    if shots:
        for s in shots:
            sd = (s.get('scene_description') or '').strip()
            if sd:
                candidates.append(sd)
                break
    anchor = next((c for c in candidates if c), '')
    anchor = re.sub(r'\s+', ' ', anchor).strip(' ，,。')
    return anchor[:90]


def _scene_negative_v14(anchor: str) -> str:
    """按主场景动态生成负面场景跳变提示，保持通用性。"""
    a = anchor or ''
    neg = []
    if any(k in a for k in ['焦黑', '荒原', '平原', '废土', '废墟', '战场']):
        neg.append('不得变成草地、森林、蓝天白云、明亮田野、城市街道或室内')
    if any(k in a for k in ['校园', '教室', '走廊']):
        neg.append('不得变成荒野、战场、宫殿或户外奇幻场景')
    if any(k in a for k in ['城市', '街道', '办公室']):
        neg.append('不得变成荒野、古代宫殿、森林或校园教室')
    if any(k in a for k in ['森林', '山谷', '雪原', '海边']):
        neg.append('不得突然变成城市室内、校园或无关场景')
    return '，'.join(neg)


def _extract_prop_locks_v14(characters, story_text: str = ''):
    """根据角色设定和剧情抽取贯穿道具/武器锁定。"""
    locks = {}
    story = str(story_text or '')
    for c in characters or []:
        name = c.get('name', '')
        desc = (c.get('description', '') or '') + ' ' + (c.get('personality', '') or '')
        low_desc = desc.lower()
        is_creature = any(k in (name + desc) for k in ['龙', '巨龙', '怪', '兽', 'monster', 'dragon', 'beast'])
        # 只有角色自身描述带武器，或非怪物/非巨兽角色在剧情中反复与武器绑定，才加武器锁。
        story_weapon_context = (not is_creature) and (
            (name and name in story and any(k in story for k in ['圣剑', '手持剑', '举起圣剑', '持剑', '握剑']))
            or any(k in name for k in ['勇者', '少年', '骑士', '剑士', '女侠', '武士', '主角'])
        )
        all_text = name + ' ' + desc
        props = []
        if any(k in all_text for k in ['圣剑', '长剑', '剑', 'sword', 'blade', 'katana']) or story_weapon_context:
            props.append('同一把正常长度银白圣剑/剑始终清晰可见，长度约为角色身高的三分之二到四分之三，角色不能空手，不能变成超长光剑、激光束或贯穿画面的巨大剑光，不能把武器换成其他物品')
        if (any(k in all_text for k in ['枪', '手枪', '步枪', 'rifle', 'pistol', 'gun'])
                or ((not is_creature) and any(k in story for k in ['手枪', '步枪', '持枪', '枪械']))):
            props.append('同一把枪械始终清晰可见，不能空手，不能随意更换武器类型')
        if any(k in all_text for k in ['法杖', '魔杖', 'staff', 'wand']):
            props.append('同一根法杖/魔杖始终清晰可见，不能空手')
        if props and name:
            locks[name] = props
    return locks


def _infer_characters_from_segment_v14(segment: str, characters):
    names = [c.get('name', '') for c in characters or [] if c.get('name')]
    found = [n for n in names if n and n in segment]
    # 如果是战斗/对峙段落，但没有显式写全角色，保守加入前两个命名角色
    if not found and names:
        found = names[:1]
    if any(k in segment for k in ['对', '战', '冲', '扑', '咆哮', '龙', '怪', '敌', '追', '交锋', '躲避']):
        for n in names[:2]:
            if n not in found:
                found.append(n)
    return found


def _make_shot_from_segment_v14(segment: str, characters, scene_anchor: str, style: str, index: int):
    names = _infer_characters_from_segment_v14(segment, characters)
    action = re.sub(r'^[第\s一二三四五六七八九十0-9镜：:,，]+', '', segment).strip(' 。')
    rel = 'neutral'
    if any(k in action for k in ['龙背', '背部', '骑']):
        rel = 'mounted'
    elif any(k in action for k in ['追', '冲向', '逼近']):
        rel = 'chase'
    elif len(names) >= 2 and any(k in action for k in ['战', '交锋', '对峙', '怒吼', '咆哮', '挥爪', '躲避', '迎击']):
        rel = 'confront'
    shot_type = '远景' if index == 1 else ('中景' if index in (2, 4, 5) else '近景')
    return {
        'shot_type': shot_type,
        'camera_angle': '平视',
        'scene_description': scene_anchor,
        'scene_anchor': scene_anchor,
        'action_zh': action[:45],
        'action': '',
        'pose_hint': 'side view dynamic action' if rel in ('confront', 'chase') else 'standing',
        'dialogue': '',
        'emotion': 'determined',
        'duration_hint': 6,
        'scene_change': False,
        'relationship_type': rel,
        'characters_in_shot': names,
        'is_key_shot': index in (1, 3, 5),
    }


def enforce_storyboard_count_and_scene_v14(shots, story_text, characters, scene_anchor='', style='', length_mode=''):
    """最终兜底：保证显式六镜不会在生图阶段退成四镜，并为每镜注入场景/道具/朝向锁。"""
    if not isinstance(shots, list):
        shots = []
    segments = _extract_numbered_story_segments_v14(story_text)
    norm_len = _normalize_length_mode_v14(length_mode)
    marker_count = len(segments)
    if marker_count >= 5:
        target = min(marker_count, 8)
    elif norm_len == '长篇':
        target = 6
    elif norm_len == '中篇':
        target = 5
    elif norm_len == '短篇':
        target = 4
    else:
        target = max(len(shots), marker_count, 4)
    target = max(1, min(target, 8))

    # v61: 勇者斩龙固定预设已经在 _v47_force_hero_dragon_advantage_storyboard()
    # 中写好了每镜独立 prompt。这里不能再把“统一场景锚点/道具锁/朝向锁”
    # 整段前置拼进去，否则前端分镜脚本编辑区会显示很长并被截断，且动作主句被冲淡。
    v60_roles = {
        'pressure_establish', 'claw_slam_dodge', 'fire_breath_dodge',
        'aerial_heavy_slash', 'light_column_finish', 'corpse_aftermath'
    }
    if shots and any((s.get('director_bible_v60') or s.get('shot_role') in v60_roles) for s in shots if isinstance(s, dict)):
        if len(shots) > target:
            shots = shots[:target]
        compact_scene = _purify_scene_anchor_v26(scene_anchor, characters) or scene_anchor or _v47_scene()
        for i, shot in enumerate(shots):
            if not isinstance(shot, dict):
                continue
            shot['index'] = i + 1
            shot.setdefault('duration_hint', 4)
            shot.setdefault('video_duration', 4)
            shot.setdefault('scene_change', False)
            if compact_scene and not shot.get('scene_anchor'):
                shot['scene_anchor'] = compact_scene
            if compact_scene and not shot.get('scene_description'):
                shot['scene_description'] = compact_scene
            # 保留原本短 prompt，不在这里追加大段锁。
            jp = str(shot.get('jimeng_ref_prompt') or '').strip()
            if jp:
                # 只做软上限，避免前端展示截断；不破坏句意。
                if len(jp) > 520:
                    jp = jp[:520].rstrip('，,。；; ') + '。'
                shot['jimeng_ref_prompt'] = jp
                shot['jimeng_prompt'] = jp
            vp = str(shot.get('video_prompt') or '').strip()
            if vp and len(vp) > 360:
                shot['video_prompt'] = vp[:360].rstrip('，,。；; ') + '。'
        return shots

    anchor = _infer_scene_anchor_v14(scene_anchor, story_text, shots)
    anchor = _purify_scene_anchor_v26(anchor, characters)
    neg_scene = _scene_negative_v14(anchor)
    prop_locks = _extract_prop_locks_v14(characters, story_text)

    # 如果 LLM 少给镜头，用显式“第X镜”文本补齐
    if marker_count and len(shots) < target:
        existing = list(shots)
        for i in range(len(existing), target):
            seg_text = segments[i]['text'] if i < len(segments) else segments[-1]['text']
            existing.append(_make_shot_from_segment_v14(seg_text, characters, anchor, style, i + 1))
        shots = existing
        print(f"[v14数量修正] 剧本有{marker_count}个显式镜头，实际只生成{len(existing)}个，已补齐到{target}个")

    if len(shots) > target:
        shots = shots[:target]
        print(f"[v14数量修正] 已按目标镜头数裁剪到{target}个")

    char_names = [c.get('name','') for c in characters or [] if c.get('name')]
    for i, shot in enumerate(shots):
        shot['index'] = i + 1
        shot.setdefault('scene_change', False)
        if anchor and not shot.get('scene_change'):
            shot['scene_anchor'] = anchor
            # 主场景直接覆盖为锚点，避免 LLM 把动作写进场景并导致生图跑偏
            shot['scene_description'] = anchor
            shot['scene_negative'] = neg_scene
        # 道具锁：只对本镜出现角色生效
        ci = shot.get('characters_in_shot') or []
        if isinstance(ci, str):
            ci = [ci]
        ci = [n for n in ci if n in char_names]
        shot['characters_in_shot'] = ci
        locks = []
        for n in ci:
            for p in prop_locks.get(n, []):
                locks.append(f'{n}：{p}')
        if locks:
            shot['prop_locks'] = locks
        # 朝向锁：动态镜不要正面站桩
        action_text = (shot.get('action_zh') or '') + (shot.get('video_prompt') or '') + (shot.get('jimeng_ref_prompt') or '')
        rel = (shot.get('relationship_type') or '').lower()
        if rel in ('confront', 'chase') or any(k in action_text for k in ['冲', '躲', '闪', '挥', '格挡', '追', '扑', '交锋', '战斗', '迎击']):
            shot['orientation_lock'] = '动态动作镜：命名角色采用侧身或四分之三侧身，视线和身体朝向动作目标；禁止正面朝镜头站桩、禁止角色卡/立绘式摆拍。'
        elif ci:
            shot['orientation_lock'] = '角色采用自然三分之四侧身或与动作一致的朝向，不要正面证件照式站立。'
        # 同步强化 prompt
        extra_parts = []
        if anchor:
            extra_parts.append(f'统一场景锚点：{anchor}')
        if neg_scene:
            extra_parts.append(neg_scene)
        if locks:
            extra_parts.extend(locks)
        if shot.get('orientation_lock'):
            extra_parts.append(shot['orientation_lock'])
        if extra_parts:
            prefix = '，'.join(extra_parts)
            for key in ['jimeng_ref_prompt', 'jimeng_prompt']:
                old = shot.get(key, '')
                if old and prefix not in old:
                    shot[key] = prefix + '，' + old
            if not shot.get('jimeng_ref_prompt'):
                shot['jimeng_ref_prompt'] = prefix + '，' + (shot.get('action_zh') or '')
                shot['jimeng_prompt'] = shot['jimeng_ref_prompt']
            old_v = shot.get('video_prompt', '')
            if old_v and anchor not in old_v:
                shot['video_prompt'] = f'保持首帧中的场景锚点不变：{anchor}。' + old_v
    return shots

def _story_text_needs_director_repair(story_text: str, length: str = '中篇') -> bool:
    """检测剧本是否仍像压缩小说而不是可制作镜头脚本。"""
    parts = _split_story_paragraphs(story_text)
    if not parts:
        return True

    # 长篇至少应有 5 个有效镜头，中篇至少 4 个；短篇至少 3 个。
    min_parts = {'短篇': 3, '中篇': 4, '长篇': 5}.get(length, 4)
    if len(parts) < min_parts:
        return True

    # 每个镜头太短，通常只能表达剧情节点，撑不住4秒。
    short_count = sum(1 for p in parts if len(re.sub(r'\s+', '', p)) < 34)
    if short_count >= max(2, len(parts)//2):
        return True

    # 同一句出现多个高风险连续动作，说明仍是压缩战斗/压缩剧情。
    risky_chains = [
        ['跃', '攀', '刺'], ['跃', '踩', '刺'], ['冲', '挥', '倒'],
        ['奔', '跳', '落'], ['追', '扑', '倒'], ['飞', '撞', '爆'],
        ['刺下', '倒地'], ['节节攀升', '刺下'], ['一剑', '轰然倒地'],
    ]
    for p in parts:
        for chain in risky_chains:
            if all(k in p for k in chain):
                return True

    # 高风险接触/站位，优先修复成更稳定画面。
    risky_phrases = ['站在龙角上', '龙头正上方', '踩着龙身节节攀升', '一剑刺下', '轰然倒地']
    if any(k in story_text for k in risky_phrases):
        return True

    return False


def _repair_story_text_for_ai_production(data: dict, genre: str = '', length: str = '中篇') -> dict:
    """当剧本仍偏压缩小说时，自动导演化重写 story_text。

    只让模型返回一个很小的 JSON: {"story_text": "..."}，避免再次长JSON截断。
    角色和场景沿用第一次生成结果，保证接口稳定。
    """
    if not isinstance(data, dict) or not data.get('story_text'):
        return data
    if not _story_text_needs_director_repair(data.get('story_text', ''), length=length):
        return data

    target = {
        '短篇': '3-4',
        '中篇': '4-5',
        '长篇': '5-6',
    }.get(length, '4-5')

    system = (
        "你是AI动态漫剧导演，负责把压缩剧情改写成可生图、可图生视频的镜头段落。"
        "只输出合法JSON对象，不要Markdown。"
    )
    prompt = f"""下面是一段已经生成的动态漫剧剧本，但它可能仍然像压缩小说，动作过快或镜头太薄。请只重写 story_text，不要改角色和场景。

【题材】{genre}
【目标篇幅】{length}，{target}个有效镜头
【原始story_text】
{data.get('story_text','')}

【自动导演规则】
1. 用户输入只是故事创意，你要自动改写成适合AI动态漫剧制作的镜头段落。
2. 每段对应一个镜头，每段45-85个中文字，能支撑至少4秒视频。
3. 每段只保留一个主要动作，并加入一个可持续的小变化：风、尘土、光线、表情、姿态、镜头推进。
4. 不要把“跃起、攀升、刺下、倒地”等连续动作压进同一段。
5. 高风险动作要自动降难：
   - “踩着龙身节节攀升”改成“借岩石跃上翼骨边缘 / 站在龙背稳住身体”。
   - “在龙头正上方刺下”改成“在龙背或龙颈上方举剑蓄势，金光照亮天空”。
   - “站在龙角上”改成“站在断裂龙角旁 / 倒下巨龙旁”。
6. 内容不足时宁可少一镜，不要拆空镜。
7. 段落之间只用\\n换行，不要编号。

【输出格式】
{{"story_text": "第一镜段落\\n第二镜段落\\n第三镜段落"}}
"""
    result = _call_llm(system, prompt, temperature=0.32, max_tokens=1800)
    if not result.get('success'):
        print(f"[剧本修复] 调用失败，保留原文: {result.get('message')}")
        return data
    try:
        fixed = _parse_json(result.get('content', ''))
        new_text = str(fixed.get('story_text', '')).strip() if isinstance(fixed, dict) else ''
        if new_text and len(new_text) > 40:
            data['story_text'] = re.sub(r'\n{3,}', '\n', new_text)
            data['director_repaired'] = True
            print('[剧本修复] ✓ 已自动导演化重写 story_text')
    except Exception as e:
        print(f"[剧本修复] 解析失败，保留原文: {e}")
    return data


def _auto_director_brief_v28(direction: str, genre: str = '', length: str = '', custom_requirements: str = '') -> str:
    """把用户的一句话创意解析成更稳定的导演提纲。
    该函数不依赖 LLM，避免用户提示太短时剧本发散。
    """
    raw = f"{direction or ''} {custom_requirements or ''} {genre or ''}"
    brief = []
    is_battle = any(k in raw for k in ['打', '战', '对决', '决斗', '击退', '击败', '怪物', '巨龙', '恶龙', 'Boss', 'BOSS', 'boss'])
    has_dragon = any(k in raw for k in ['龙', '巨龙', '恶龙', '古龙'])
    has_hero = any(k in raw for k in ['勇者', '少年', '骑士', '剑士', '圣剑'])
    if has_hero:
        brief.append('主角锚点：18岁左右青年勇者/骑士，不是儿童，蓝白圣骑士轻甲，短披风，手持同一把圣剑。')
    if has_dragon:
        brief.append('对手锚点：一条体型巨大的远古巨龙，黑色或暗色鳞片，红/金色发光眼睛，巨大翅膀、长尾、弯角、巨爪，始终只出现一条命名巨龙。')
    if is_battle and has_dragon:
        brief.append('战斗弧线：压迫建立→勇者起势→第一次交锋→被压制或震退后回稳→高光反击→结果收束；禁止一击秒杀。')
        brief.append('机位偏好：至少一镜使用勇者背后或肩后低机位仰视，前景是小体量勇者背影，远处/天空中是巨大巨龙，突出体型差。')
        brief.append('结尾要求：最后必须是战斗结束或胜负已分，巨龙失去攻势/倒地/退却，勇者收剑回稳；不要停在继续对峙或摆拍。')
    if any(k in raw for k in ['焦黑', '焦土', '平原', '荒原', '废土']):
        brief.append('场景锚点：焦黑平原、纵横裂痕、低空烟尘、灰烬、余晖或厚云；场景字段只写环境，不写远处巨龙盘踞。')
    elif has_dragon:
        brief.append('场景锚点：开阔战场或荒原，地面裂痕、灰烬、烟尘和压迫天空保持连续。')
    if '电影感' in raw or '镜头' in raw:
        brief.append('镜头风格：更电影感，景别有推进和变化，避免连续平视中景。')
    if not brief:
        brief.append('自动导演：从用户创意中提取主角、目标、阻碍、主场景、情绪转折和结尾收束画面；每段都要能直接变成分镜。')
    return '\n'.join(f'- {x}' for x in brief)



# ═══════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════
# v52 勇者 vs 巨龙：六镜 Profile 版（压迫→起势特写→闪避→格挡→命中→收尾）
# 目标：当前测试题材使用6镜，不写进通用规则；每张图只表现一个清晰关键帧。
#       主链路使用中文提示词，英文 scene_spec 只作兼容字段。
# ═══════════════════════════════════════════════════════
def _v47_is_hero_dragon_context(text: str = '', characters=None) -> bool:
    blob = str(text or '')
    for c in characters or []:
        blob += ' ' + str(c.get('name','')) + ' ' + str(c.get('description',''))
    return any(k in blob for k in ['勇者','少年','圣剑','骑士']) and any(k in blob for k in ['巨龙','远古巨龙','龙爪','恶龙','古龙'])


def _v47_roles(characters=None):
    """v53: 稳定解析“少年勇者 / 远古巨龙”。

    关键修复：不能仅因为巨龙描述里含有“体型约为勇者8倍”就把巨龙也识别成勇者。
    因此规则是：
    1. 优先精确角色名：少年勇者、远古巨龙；
    2. 先判定 dragon，再判定 hero；
    3. 只有“不是龙”的角色才能作为 hero；
    4. 不再按角色列表顺序推断。
    """
    human = ''
    dragon = ''

    dragon_terms = ['远古巨龙', '巨龙', '古龙', '恶龙', '魔龙', '龙爪', '黑曜石', '鳞片', '翼膜', 'dragon']
    hero_name_terms = ['少年勇者', '勇者', '圣骑士', '骑士']
    hero_desc_terms = ['圣剑', '轻甲', '披风', '青年男性', '蓝白', '白银胸甲']

    items = []
    for c in characters or []:
        if not isinstance(c, dict):
            continue
        n = str(c.get('name', '') or '').strip()
        d = str(c.get('description', '') or '').strip()
        text = n + ' ' + d
        items.append((n, d, text))

    # 1) 精确命名最高优先级
    for n, d, text in items:
        if n == '少年勇者':
            human = n
        if n == '远古巨龙':
            dragon = n

    # 2) 语义识别：龙角色不能再被识别成勇者
    for n, d, text in items:
        is_dragon = any(k in text for k in dragon_terms) or ('龙' in n and '勇者' not in n)
        is_hero_name = any(k in n for k in hero_name_terms)
        is_hero_desc = any(k in d for k in hero_desc_terms)

        if is_dragon and not dragon:
            dragon = n or '远古巨龙'
            continue

        if (is_hero_name or is_hero_desc) and (not is_dragon) and not human:
            human = n or '少年勇者'

    # 3) 兜底：使用规范名，避免把龙误当成勇者
    if not human:
        human = '少年勇者'
    if not dragon:
        dragon = '远古巨龙'

    if human == dragon:
        # 极端兜底：绝不允许双角色同名
        human = '少年勇者'
        dragon = '远古巨龙'

    return human, dragon


def _v47_hero_desc(name='少年勇者') -> str:
    # v48: 勇者不再只写“少年”，改成可被画面识别的角色设计。
    return (
        f'{name}：18岁左右青年男性，不是儿童，棕色短发，微乱刘海，蓝色眼睛，脸型清秀但神情坚毅；'
        '身穿蓝白配色圣骑士轻甲，白银胸甲、肩甲、护腕、腰带、护腿、白色长靴、深蓝短披风完整统一；'
        '双手使用同一把金色十字护手银白圣剑，剑身修长发白光，体型修长敏捷；'
        '所有镜头保持同一张脸、同一发型、同一套轻甲、同一把圣剑。'
    )


def _v47_dragon_desc(name='远古巨龙') -> str:
    # v53: 角色库描述只写巨龙自身，不写“勇者8倍”等人类参照，避免角色立绘生成旁边小人。
    return (
        f'{name}：黑曜石色巨型远古龙，非人类生物，单独主体，全身正面角色设定图；'
        '体型庞大威严，头颅高昂，红色发光眼，弯曲黑角，粗壮前肢与巨大锋利龙爪，背部尖刺，'
        '红黑翼膜巨大双翼，长尾带尖刺；'
        '每镜保持同一鳞片纹理、同一角形、同一翼膜颜色、同一尾部形状和同一巨大体型轮廓；'
        '画面中只允许出现这一条远古巨龙。'
    )


def _v47_scene() -> str:
    return '焦黑平原，焦土遍布纵横裂痕，低空烟尘翻涌，远天残阳压低'


def _v47_local_hero_dragon_story(direction='', genre='奇幻', length='中篇') -> dict:
    """LLM欠费/失败时可直接跑 demo 的本地导演化故事。
    v64: 改为用户最新确认的“压迫感终结技”六镜结构：
    压迫建立 → 龙爪拍地跳闪 → 勇者蓄力、巨龙怒吼 → 高空特写从天而降重斩 → 魔法圣剑贯穿巨龙 → 落地与巨龙尸体收束。
    """
    story = (
        '第一镜：焦黑平原上残阳压低，少年勇者背对镜头站在裂地前景，深蓝短披风被热浪掀起，右手握住金色十字护手圣剑。高空中的远古巨龙低头俯视，巨大的身躯遮住半片天空，压迫感逼人。\n'
        '第二镜：远古巨龙猛然从高处压下巨大前爪拍向地面，落点砸出焦黑深坑，飞石、火星和烟尘向四周炸开。少年勇者借冲击瞬间向上跃起，从坑边腾空跳闪，明确避开龙爪落点。\n'
        '第三镜：少年勇者在前景半蹲蓄力，双手握住圣剑收于身侧，脚下焦土被压出碎石与扬尘，准备下一瞬纵身挑起。中远景的远古巨龙昂首怒吼，张口示威，双翼与巨大身躯继续带来压迫感。\n'
        '第四镜：少年勇者已经跃至高空，从天而降，双手持圣剑举过头顶，身体前倾朝下方发出重剑下劈。镜头更贴近勇者，突出披风、发丝和下劈动势，远古巨龙作为下方受击目标可见。\n'
        '第五镜：少年勇者自高空裹挟魔法圣剑之力贯穿远古巨龙，发光的巨大圣剑轨迹自上而下刺穿巨龙主体，巨龙在冲击中仰身失去攻势，地面裂纹、烟尘和碎石被冲击波掀起。\n'
        '第六镜：战斗结束后，少年勇者落地站稳并收剑回稳，前景或侧前方保留勇者完整身影，中远景清楚可见远古巨龙倒伏的尸体，焦黑平原重新归于短暂平静。'
    )
    return {
        'title': '勇者斩龙·终结技版',
        'story_text': story,
        'story_beats': [],
        'characters': [
            {'name': '少年勇者', 'description': _v47_hero_desc('少年勇者'), 'personality': '坚毅果断', 'voice_style': '沉稳有力'},
            {'name': '远古巨龙', 'description': _v47_dragon_desc('远古巨龙'), 'personality': '暴戾凶猛', 'voice_style': '低沉咆哮'},
        ],
        'scenes': [
            {'name': '焦黑平原', 'description_zh': _v47_scene(), 'description_en': '', 'time_of_day': '黄昏'},
        ],
        'genre': genre or '奇幻',
        'mood': '压迫、热血、爽感终结、结果收束',
        'director_bible_v60': True,
    }

def _v47_force_hero_dragon_story(data: dict, direction='', genre='奇幻', length='中篇', custom_requirements='') -> dict:
    if not isinstance(data, dict):
        return data
    ctx = ' '.join([str(direction or ''), str(custom_requirements or ''), str(data.get('story_text',''))])
    if not _v47_is_hero_dragon_context(ctx, data.get('characters')):
        return data
    local = _v47_local_hero_dragon_story(direction, genre, length)
    data['title'] = data.get('title') or local['title']
    data['story_text'] = local['story_text']
    data['characters'] = local['characters']
    data['scenes'] = local['scenes']
    data['mood'] = local['mood']
    data['director_bible_v52'] = True
    return data


def _v47_clean_scene(scene_spec='') -> str:
    raw = str(scene_spec or '').strip()
    if not raw:
        return _v47_scene()
    try:
        raw = _purify_scene_anchor_v26(raw, characters=None) or raw
    except Exception:
        pass
    if any(k in raw for k in ['巨龙','勇者','少年','圣剑','战斗','对峙','攻击']):
        raw = _v47_scene()
    return raw[:42] or _v47_scene()


def _v48_action_phrase(text: str) -> str:
    """保证 action_zh 更像单动作标题，避免连续过程词。"""
    banned = ['已经','已','之后','随后','然后','再','顺势','接着','同时','后重新','后顺势','后']
    t = str(text or '')
    for b in banned:
        t = t.replace(b, '')
    t = t.replace('  ', ' ').strip('，,。 ')
    return t


def _v50_action_sentence(text: str) -> str:
    """v50: action_zh 既要单动作，又要让AI看懂两个主体关系。
    禁止“已/随后/然后/顺势”等过程词，但保留空间、受力和结果状态。
    """
    t = _v48_action_phrase(text)
    t = re.sub(r'[；;。]+$', '', t).strip(' ，,')
    return t[:88]


def _v50_dual_subject_rule(human: str, dragon: str) -> str:
    return (
        f'双主体动作语法：必须同时画清{dragon}的攻击方向、{human}的应对姿态、二者之间的距离和受力反馈；'
        f'画面只允许1个{human}和1条{dragon}，不允许第二个勇者、第二条龙、前景旁观者或多个时间残影；'
        '每张分镜只表现一个可截图瞬间，给相邻4秒视频留出动作空间。'
    )


def _v47_make_shot(i: int, characters=None, scene_spec='', style='日漫') -> dict:
    """v53: 勇者 vs 巨龙六镜低复杂度分镜。
    该函数只在 hero_dragon profile 命中时使用，不影响其他题材泛用性。

    v55 设计原则：
    - 不再要求图像模型画“剑刃精确接触龙爪”“龙爪压住剑身”等高失败率动作。
    - 不使用“巨龙阴影”“剑光光束”这类模型容易误解的抽象视觉。
    - 用“特写、环境冲击、单人回稳、局部剑爪交错、远距收束”替代完整身体复杂交互。
    - 多主体镜头只做清楚站位；如果需要战斗感，只做局部道具/龙爪交错，不同时画完整勇者和完整巨龙近身缠斗。
    """
    human, dragon = _v47_roles(characters)
    scene = _v47_clean_scene(scene_spec)
    hero = '18岁青年勇者，棕色短发、蓝眼、蓝白圣骑士轻甲、白银胸甲肩甲护腕护腿、白色长靴、深蓝短披风、金色十字护手银白圣剑，圣剑为正常长度，不能变成超长光剑或激光束'
    dragon_visual = '唯一远古巨龙，黑曜石鳞片、红色发光眼、弯曲黑角、红黑翼膜、粗壮前肢巨爪，巨大体型轮廓形成压迫感'
    style_txt = f'{style}动态漫剧精致插画' if style else '动态漫剧精致插画'
    both = [human, dragon]
    common_short = '只画当前这一瞬间，不画前后过程；禁止白底、文字、双龙、复制勇者、旁观者、时间残影；圣剑保持正常长度，禁止超长光剑、巨大光束、剑身贯穿画面。'
    no_contact = '本镜禁止画剑刃接触龙身、禁止画龙爪接触勇者身体、禁止画复杂缠斗。'

    if i == 1:
        action = f'{human}背对镜头站在画面下方前景，右手握住圣剑；{dragon}位于画面上方远处低头俯视，巨大身体压住天空。'
        return {
            'shot_type':'远景','camera_angle':'仰视','scene_description':scene,
            'action_zh':action,'action_detail_zh':action,
            'action':'hero faces giant dragon from behind','pose_hint':'standing','dialogue':'',
            'characters_in_shot':both,'emotion':'anxious','duration_hint':4,'video_duration':4,'scene_change':False,
            'relationship_type':'confront','is_key_shot':True,'director_bible_v55':True,
            'single_action_frame':True,'complexity_level':'safe','frame_gap_note':'第1镜是压迫建立，只负责体型差和场景压迫。',
            'jimeng_ref_prompt':f'{style_txt}，{scene}。远景低机位仰视，{human}只出现一次，背对镜头站在前景下方，右手握圣剑，短披风被风吹起，{hero}。{dragon}只出现一条，位于天空上半部远处低头俯视，{dragon_visual}。二者保持明显距离。{common_short}',
            'video_prompt':f'保持首帧中的场景、体型差和站位不变。前30%：低空烟尘掠过焦土，{human}披风轻动；中40%：{dragon}在远处缓慢张翼压近，巨大阴影覆盖地面；后30%：{human}握紧圣剑但不冲出，镜头轻微仰推。'
        }

    if i == 2:
        action = f'{human}双手握紧圣剑剑柄，剑身反射残阳微光；风沙掠过他的侧脸，眼神由紧张变得坚定。'
        return {
            'shot_type':'特写','camera_angle':'低角度近景','scene_description':scene,
            'action_zh':action,'action_detail_zh':action,
            'action':'hero grips normal sword and becomes determined','pose_hint':'standing','dialogue':'',
            'characters_in_shot':[human],'emotion':'determined','duration_hint':4,'video_duration':4,'scene_change':False,
            'relationship_type':'neutral','is_key_shot':True,'director_bible_v55':True,
            'single_action_frame':True,'complexity_level':'safe','frame_gap_note':'第2镜是单人起势特写，只出现勇者，不出现巨龙实体，也不使用巨龙阴影。',
            'jimeng_ref_prompt':f'{style_txt}，{scene}。低角度近景特写，只画{human}一个角色主体，双手握紧正常长度的金色十字护手银白圣剑，剑身只反射残阳微光，不出现巨龙倒影，不出现巨龙实体，风沙掠过侧脸，眼神坚定，{hero}。背景保持焦黑平原和残阳色调。{common_short}禁止出现第二个角色主体、禁止双视图、禁止角色设定表。',
            'video_prompt':f'保持首帧中的角色外貌、圣剑和背景色调不变。前30%：{human}手指慢慢收紧剑柄；中40%：圣剑剑身反射残阳微光但不变长；后30%：镜头轻推到坚定眼神，风沙从侧脸掠过。'
        }

    if i == 3:
        action = f'{dragon}的巨大前爪从画面左上方砸向焦土地面，爪尖击中裂缝边缘，碎石、火星和尘土弹起；画面中不出现{human}。'
        return {
            'shot_type':'中远景','camera_angle':'低角度侧面','scene_description':scene,
            'action_zh':action,'action_detail_zh':action,
            'action':'dragon claw slams into cracked ground, hero not visible','pose_hint':'claw impact','dialogue':'',
            'characters_in_shot':[dragon],'emotion':'tense','duration_hint':4,'video_duration':4,'scene_change':False,
            'relationship_type':'object_focus','is_key_shot':False,'director_bible_v55':True,
            'single_action_frame':True,'complexity_level':'safe','frame_gap_note':'第3镜只画龙爪落地和环境冲击，不画勇者闪避过程。',
            'jimeng_ref_prompt':f'{style_txt}，{scene}。中远景低角度侧面，只画{dragon}的一只巨大前爪从左上方砸到焦土地面，爪尖击中裂缝边缘，碎石、火星、尘土从地面弹起，{dragon_visual}。画面中不出现{human}，不出现任何人类角色。{common_short}{no_contact}',
            'video_prompt':f'保持首帧场景不变。前30%：{dragon}巨大前爪从左上方压向焦土地面；中40%：爪尖击中裂缝边缘，碎石和火星弹起；后30%：尘土向右侧扩散，画面不切到{human}。'
        }

    if i == 4:
        action = f'{human}在画面右侧单独落地回稳，单膝微屈，正常长度圣剑护在胸前；画面左侧保留上一镜龙爪砸出的裂缝、碎石和火星，身后尘土翻卷。'
        return {
            'shot_type':'中景','camera_angle':'侧面','scene_description':scene,
            'action_zh':action,'action_detail_zh':action,
            'action':'hero lands beside previous claw impact mark','pose_hint':'landing guard','dialogue':'',
            'characters_in_shot':[human],'emotion':'determined','duration_hint':4,'video_duration':4,'scene_change':False,
            'relationship_type':'neutral','is_key_shot':False,'director_bible_v55':True,
            'single_action_frame':True,'complexity_level':'safe','frame_gap_note':'第4镜承接上一镜：只画勇者回稳，但保留同一个龙爪落点裂缝、碎石和火星，形成战斗连续性。',
            'jimeng_ref_prompt':f'{style_txt}，{scene}。中景侧面，只画{human}一个角色主体，位于画面右侧单膝微屈落地回稳，双手持同一把正常长度圣剑护在胸前，披风和尘土向后翻卷，{hero}。画面左侧保留上一镜龙爪砸出的同一道裂缝、碎石和少量火星，作为连续战斗痕迹；不画巨龙，不画巨龙阴影，不画第二个角色。{common_short}禁止出现第二个勇者、禁止出现完整巨龙、禁止画成格挡接触、禁止新增怪物。',
            'video_prompt':f'保持首帧角色外貌和场景不变。前30%：{human}落地后膝盖微弯，左侧上一镜的裂缝里仍有火星；中40%：他把圣剑护到胸前，披风向后摆动；后30%：身体稳定下来，眼神看向画面左侧远处，保持战斗警戒。'
        }

    if i == 5:
        action = f'局部交锋特写：画面只出现{human}的双手、正常长度圣剑剑身、{dragon}的一只前爪局部；剑刃与龙爪尖在画面中央交错擦碰，少量火星爆开。'
        return {
            'shot_type':'特写','camera_angle':'平视侧面','scene_description':scene,
            'action_zh':action,'action_detail_zh':action,
            'action':'local clash close-up, sword edge crosses dragon claw tip','pose_hint':'local clash','dialogue':'',
            'characters_in_shot':both,'emotion':'determined','duration_hint':4,'video_duration':4,'scene_change':False,
            'relationship_type':'object_focus','is_key_shot':True,'director_bible_v55':True,
            'single_action_frame':True,'complexity_level':'medium_safe','frame_gap_note':'第5镜用局部交锋补回战斗感：只画圣剑与龙爪局部交错，不画完整身体近身缠斗。',
            'jimeng_ref_prompt':f'{style_txt}，{scene}。特写局部交锋镜，画面中央只表现{human}的双手握住同一把正常长度银白圣剑、{dragon}的一只前爪局部，剑刃与龙爪尖交错擦碰，接触点只有少量火星爆开，背景是焦黑裂地和低空烟尘。只画局部，不画完整{human}身体，不画完整{dragon}身体，不画第二个勇者，不画第二条龙，{hero}，{dragon_visual}。{common_short}禁止超长光剑、禁止激光束、禁止剑身贯穿画面、禁止龙爪抓住勇者身体、禁止画成屠龙终结。',
            'video_prompt':f'保持首帧局部构图不变。前30%：正常长度圣剑从右侧轻微压向龙爪尖；中40%：剑刃边缘与龙爪尖交错擦碰，少量火星爆开；后30%：火星向下散落，双方局部仍保持分离，不出现完整身体缠斗。'
        }

    action = f'{dragon}在远处收回前爪后退半步，{human}站在前景双手握剑警戒，双方隔着同一道裂痕、烟尘和火星重新拉开距离。'
    return {
        'shot_type':'全景','camera_angle':'平视','scene_description':scene,
        'action_zh':action,'action_detail_zh':action,
        'action':'dragon steps back while hero holds guard at distance','pose_hint':'standing guard','dialogue':'',
        'characters_in_shot':both,'emotion':'determined','duration_hint':4,'video_duration':4,'scene_change':False,
        'relationship_type':'neutral','is_key_shot':True,'director_bible_v55':True,
        'single_action_frame':True,'ending_frame':True,'complexity_level':'safe','frame_gap_note':'第6镜承接局部交锋后的收束：双方拉开距离，不继续对砍，不画死亡。',
        'jimeng_ref_prompt':f'{style_txt}，{scene}。全景平视收束镜，{dragon}只出现一条，位于远处后退半步并收起前爪，{dragon_visual}；{human}只出现一次，站在前景双手握剑警戒，剑尖斜指地面，{hero}。双方隔着同一道地面裂痕和烟尘保持明显距离，地面保留上一镜交锋后的少量火星和碎石。{common_short}禁止继续对砍、禁止剑和龙爪接触、禁止巨龙死亡。',
        'video_prompt':f'保持首帧场景和双方距离不变。前30%：{dragon}在远处收回前爪，火星从地面落下；中40%：{dragon}低头后退半步，和{human}拉开距离；后30%：{human}双手握剑警戒，镜头轻微拉远，表现局部交锋后短暂停住。'
    }

def _v47_force_hero_dragon_advantage_storyboard(shots, characters=None, scene_spec='', style='日漫', story_text=''):
    """勇者 vs 巨龙：用户确认版六镜高燃终结技分镜。

    固定结构：
    1) 压迫建立
    2) 龙爪拍地 + 勇者闪避
    3) 巨龙喷火压制 + 勇者跃起脱离火焰
    4) 勇者高空举剑重斩
    5) 圣光柱笼罩巨龙
    6) 勇者落地 + 巨龙尸体收束
    """
    if not _v47_is_hero_dragon_context(story_text, characters):
        return shots

    human, dragon = _v47_roles(characters or [])
    human = human or '少年勇者'
    dragon = dragon or '远古巨龙'

    scene = _v47_clean_scene(scene_spec) or '焦黑平原，焦土遍布纵横裂痕，低空烟尘翻涌，远天残阳压低'
    style_txt = f'{style}2D风格' if style else '日漫2D风格'
    hero_lock = (
        f'{human}：18岁左右青年男性，不是儿童，棕色短发，微乱刘海，蓝色眼睛，脸型清秀但神情坚毅；'
        '身穿蓝白配色圣骑士轻甲，白银胸甲、肩甲、护腕、腰带、护腿、白色长靴、深蓝短披风完整统一；'
        '双手使用同一把金色十字护手银白圣剑，剑身修长发白光，体型修长敏捷。'
    )
    dragon_lock = (
        f'{dragon}：黑曜石色巨型远古龙，红色发光眼，弯曲黑角，粗壮前肢与巨大锋利龙爪，背部尖刺，'
        '红黑翼膜巨大双翼，长尾带尖刺；每镜保持同一鳞片纹理、同一角形、同一翼膜颜色和同一巨大体型比例。'
    )
    scene_lock = f'场景保持：{scene}。'
    common_rule = (
        f'只允许1个{human}和1条{dragon}；勇者外观与圣剑一致；'
        f'巨龙体型、鳞片、角形、翼膜一致且不能缩小；画面完整，动作清楚。'
    )

    fixed = [
        {
            'index': 1,
            'shot_role': 'pressure_establish',
            'shot_type': '远景',
            'camera_angle': '仰视',
            'scene_description': scene,
            'scene_anchor': scene,
            'action_zh': f'{human}背对镜头站在裂地前景仰望高空中的{dragon}，披风被热浪掀起，握紧圣剑准备迎战',
            'action_detail_zh': f'{human}背对镜头站在前景裂地边缘，右手握住金色十字护手圣剑，剑身在画面中清晰可见；高空中的{dragon}低头俯视，巨大身躯压住天空，突出强烈体型差和压迫感',
            'action': 'hero faces giant dragon under pressure',
            'pose_hint': 'standing',
            'dialogue': '',
            'characters_in_shot': [human, dragon],
            'emotion': 'determined',
            'duration_hint': 4,
            'video_duration': 4,
            'scene_change': False,
            'relationship_type': 'confront',
            'is_key_shot': True,
            'single_action_frame': True,
            'complexity_level': 'safe',
            'frame_gap_note': '压迫建立镜，只建立体型差和对峙压迫，不发生攻击。',
            'jimeng_ref_prompt': (
                f'{style_txt}，远景仰视建立镜。{human}位于画面下方前景偏中，背对镜头站在裂地边缘，深蓝短披风被热浪掀起，右手握住金色十字护手圣剑，剑身在画面中清晰可见；'
                f'{dragon}位于画面上半部高空低头俯视，体型明显大于{human}，压迫感强。{scene_lock}{common_rule}'
            ),
            'video_prompt': (
                f'保持首帧中的站位、体型差和场景不变。前30%：低空烟尘与热浪掠过焦土，{human}披风和衣摆轻微摆动；'
                f'中40%：高空中的{dragon}缓慢压低头部并振翼逼近少许，巨大阴影覆盖地面；后30%：镜头轻微仰推，强化压迫感，不发生攻击。'
            ),
            'identity_locks': [f'{human}身份锁：{hero_lock}', f'{dragon}身份锁：{dragon_lock}'],
        },
        {
            'index': 2,
            'shot_role': 'claw_slam_dodge',
            'shot_type': '中景',
            'camera_angle': '仰视',
            'scene_description': scene,
            'scene_anchor': scene,
            'action_zh': f'{dragon}挟压迫感挥下巨大前爪砸出深坑，{human}借冲击向上跃起跳闪避开',
            'action_detail_zh': f'{dragon}位于画面左上方或上半部，巨大前爪裹挟压迫感砸向焦土地面，落点砸出焦黑深坑，飞石、火星与烟尘向四周炸开；{human}位于画面右侧前景，从坑边向上跃起跳闪，身体明确避开龙爪落点',
            'action': 'dragon claw smashes crater, hero leaps clear',
            'pose_hint': 'dodging',
            'dialogue': '',
            'characters_in_shot': [human, dragon],
            'emotion': 'determined',
            'duration_hint': 4,
            'video_duration': 4,
            'scene_change': False,
            'relationship_type': 'confront',
            'is_key_shot': True,
            'single_action_frame': True,
            'complexity_level': 'medium_safe',
            'frame_gap_note': '第二镜专门表现龙爪重击与勇者跳闪，必须有巨龙压迫感、深坑与飞石，不能退化成普通对峙或喷火。',
            'jimeng_ref_prompt': (
                f'{style_txt}，中景仰视动作镜。{dragon}位于画面左上方或上半部，巨大的前爪带着压迫感猛砸焦土地面，落点形成明显深坑，飞石、火星与烟尘向外炸开；'
                f'{human}位于画面右侧前景，从坑边向上跃起跳闪，身体腾空，明确避开龙爪落点。双方侧向相对，不要同时正面朝镜头。{scene_lock}{common_rule}必须看清龙爪攻击方向、深坑爆开的破坏力以及{human}的跃起闪避路径。'
            ),
            'video_prompt': (
                f'保持首帧中的双方身份和场景不变。前30%：{dragon}自高处压低身体并抬起巨大前爪蓄势下砸，阴影罩向{human}；'
                f'中40%：龙爪猛击焦土砸出深坑，飞石、火星和烟尘向外爆开，{human}借冲击瞬间向上跃起跳闪脱离落点；后30%：烟尘向后翻卷，{human}在空中完成避让，不要转成喷火镜。'
            ),
            'identity_locks': [f'{human}身份锁：{hero_lock}', f'{dragon}身份锁：{dragon_lock}'],
        },
        {
            'index': 3,
            'shot_role': 'hero_charge_dragon_roar',
            'shot_type': '中景',
            'camera_angle': '仰视',
            'scene_description': scene,
            'scene_anchor': scene,
            'action_zh': f'{human}在前景半蹲蓄力准备纵身挑起，远处的{dragon}昂首怒吼继续施压',
            'action_detail_zh': f'{human}位于画面右侧或前景，双手握住圣剑收于身侧或身后，半蹲压低重心蓄力，脚边有被压开的碎石与扬尘；中远景的{dragon}昂首怒吼，张口示威，双翼与巨大身躯继续制造压迫感',
            'action': 'hero charges up while dragon roars in distance',
            'pose_hint': 'charging',
            'dialogue': '',
            'characters_in_shot': [human, dragon],
            'emotion': 'tense',
            'duration_hint': 4,
            'video_duration': 4,
            'scene_change': False,
            'relationship_type': 'confront',
            'is_key_shot': True,
            'single_action_frame': True,
            'complexity_level': 'medium_safe',
            'frame_gap_note': '第三镜改为勇者蓄力与巨龙怒吼，不再表现喷火或拍地，重点是起势与压迫感。',
            'jimeng_ref_prompt': (
                f'{style_txt}，中景仰视起势镜。{human}位于画面右侧或前景，半蹲压低重心，双手握住金色十字护手圣剑收于身侧或身后，脚边碎石与烟尘被蓄力气势轻微带起，准备下一瞬纵身挑起；'
                f'{dragon}位于画面左侧中远景或上方，昂首怒吼并低头锁定{human}，巨大身躯和双翼继续制造压迫感。{scene_lock}{common_rule}重点表现{human}蓄力准备挑起与{dragon}远处怒吼，避免再次变成喷火镜或龙爪拍地镜。'
            ),
            'video_prompt': (
                f'保持首帧中的双方身份和背景不变。前30%：{human}落地后迅速压低重心，双手收剑到身侧进入蓄力姿态；'
                f'中40%：脚下碎石和烟尘被蓄力气势带起，远处{dragon}昂首怒吼并展开威压；后30%：{human}完成蓄力，身体前倾准备纵身挑起，停在下一击前摇，不要转成喷火或拍地。'
            ),
            'identity_locks': [f'{human}身份锁：{hero_lock}', f'{dragon}身份锁：{dragon_lock}'],
        },
        {
            'index': 4,
            'shot_role': 'aerial_heavy_slash',
            'shot_type': '特写',
            'camera_angle': '仰视',
            'scene_description': scene,
            'scene_anchor': scene,
            'action_zh': f'{human}自高空从天而降双手持圣剑过头顶，朝下方{dragon}发出重剑下劈',
            'action_detail_zh': f'{human}作为近距离主体位于画面上半部或中心，双手持圣剑高举过头顶并已经进入下劈动作，身体前倾向下俯冲，披风与发丝被气流强烈掀起；下方或背景中可见{dragon}作为受击目标参考，但不要抢占主体；本镜禁止出现贯穿结果',
            'action': 'hero descends from the sky with a heavy two-handed slash',
            'pose_hint': 'aerial heavy slash',
            'dialogue': '',
            'characters_in_shot': [human, dragon],
            'emotion': 'determined',
            'duration_hint': 4,
            'video_duration': 4,
            'scene_change': False,
            'relationship_type': 'confront',
            'is_key_shot': True,
            'single_action_frame': True,
            'complexity_level': 'medium',
            'frame_gap_note': '第四镜改为勇者特写的从天而降重剑下劈，重点看清人物，不要退化成远景或提前画出终结结果。',
            'jimeng_ref_prompt': (
                f'{style_txt}，特写仰视高潮镜。{human}作为画面主体位于中上部或前景，双手持金色十字护手圣剑举过头顶并从天而降，已经进入朝下方重剑下劈的动作，身体前倾俯冲，披风与发丝被气流强烈掀起；'
                f'{dragon}位于下方中远景或背景中作为明确受击目标参考，但不要抢占主体。{scene_lock}{common_rule}重点表现{human}特写、从天而降和双手重剑下劈的力量感，禁止提前画出贯穿结果。'
            ),
            'video_prompt': (
                f'保持首帧中的角色身份和空间关系不变。前30%：镜头跟随{human}自高空俯冲下落，披风和发丝被高速气流拉起；'
                f'中40%：{human}双手持圣剑过头顶，沿着明确下劈轨迹朝下方{dragon}猛然斩落；后30%：下劈动作继续推进并逼近命中前一瞬，停在即将击中的状态，不要提前画出贯穿结果。'
            ),
            'identity_locks': [f'{human}身份锁：{hero_lock}', f'{dragon}身份锁：{dragon_lock}'],
        },
        {
            'index': 5,
            'shot_role': 'magic_sword_impale_finish',
            'shot_type': '全景',
            'camera_angle': '仰视',
            'scene_description': scene,
            'scene_anchor': scene,
            'action_zh': f'{human}自高空双手重剑下劈，魔法圣剑自天而降贯穿{dragon}',
            'action_detail_zh': f'{human}自高空双手持圣剑下劈，发光的巨大魔法圣剑轨迹自上而下贯穿{dragon}主体，{dragon}在冲击中后仰失去攻势；地面裂纹发光，冲击波掀起烟尘与碎石',
            'action': 'magic holy sword plunges down and pierces the dragon',
            'pose_hint': 'impact',
            'dialogue': '',
            'characters_in_shot': [human, dragon],
            'emotion': 'shocked',
            'duration_hint': 4,
            'video_duration': 4,
            'scene_change': False,
            'relationship_type': 'neutral',
            'is_key_shot': True,
            'single_action_frame': True,
            'complexity_level': 'safe',
            'frame_gap_note': '第五镜改为魔法圣剑自天而降贯穿巨龙，必须比单纯光柱更有压迫感和命中感，不回退成对峙或近身摆拍。',
            'jimeng_ref_prompt': (
                f'{style_txt}，全景仰视终结镜，高潮爆发瞬间，强冲击力。唯一核心动作是一把巨大的金白色半透明魔法巨剑从天空魔法阵中心垂直向下坠落，精准贯穿唯一的{dragon}胸口中心；'
                f'魔法巨剑从魔法阵中心朝下方清晰坠落，剑尖明确朝下，可以近似垂直或略微倾斜，但必须保持完整清楚的下落轨迹，并精准命中{dragon}胸口中心；魔法阵位于天空上方，呈横向展开或轻微侧倾的圆盘透视，巨剑可以带有一定斜向透视，但不能偏离命中点。'
                f'{human}只作为较小高空身影出现在画面上方侧面，刚完成挥砍召唤动作，与魔法巨剑完全分离，不站在剑上，不和巨剑重合；{dragon}在受击瞬间猛然后仰，胸口命中处爆发强烈金白冲击光，双翼失衡展开，前爪抓地，身体受冲击产生明显后仰与扭转。'
                f'地面裂纹发出金白与熔岩红混合光，命中点下方爆发环形冲击波，烟尘、碎石、火星与鳞片碎片向外炸开。命中点、魔法巨剑、魔法阵与{dragon}受击姿态必须同时清楚，不能退化成普通光柱、细长激光或打偏。{scene_lock}{common_rule}禁止第二条龙，禁止重新对峙，禁止巨剑落在巨龙旁边、擦边落下或没有命中巨龙主体。'
            ),
            'video_prompt': (
                f'保持首帧中的场景和巨龙身份不变。前30%：天空上方迅速展开横向或轻微侧倾的巨大圆形魔法阵，{human}作为较小高空身影在侧上方完成召唤挥砍动作；'
                f'中40%：一把巨大的半透明金白魔法巨剑从魔法阵中心朝下方坠落，剑尖朝下，可以近似垂直或略微倾斜，但必须精准刺入并贯穿{dragon}胸口中心，地面裂纹发光，烟尘、碎石与冲击波向外爆开；后30%：{dragon}在贯穿冲击中后仰失去攻势，魔法光芒与烟尘逐渐收束，不要重新切回对峙镜，也不要让巨剑打偏。'
            ),
            'identity_locks': [f'{human}身份锁：{hero_lock}', f'{dragon}身份锁：{dragon_lock}'],
        },
        {
            'index': 6,
            'shot_role': 'corpse_aftermath',
            'shot_type': '远景',
            'camera_angle': '低机位',
            'scene_description': scene,
            'scene_anchor': scene,
            'action_zh': f'{human}落地站稳并收剑回稳，中远景清楚可见{dragon}倒伏尸体，战斗已经结束',
            'action_detail_zh': f'{human}位于前景一侧落地站稳并收剑回稳；中远景必须清楚看到{dragon}横向倒伏的尸体，头部、前肢和塌落双翼可见，眼睛不再发光，明确战斗已经结束，不再对峙',
            'action': 'hero lands, dragon corpse remains',
            'pose_hint': 'standing aftermath',
            'dialogue': '',
            'characters_in_shot': [human, dragon],
            'emotion': 'calm',
            'duration_hint': 4,
            'video_duration': 4,
            'scene_change': False,
            'relationship_type': 'neutral',
            'is_key_shot': True,
            'single_action_frame': True,
            'ending_frame': True,
            'complexity_level': 'safe',
            'frame_gap_note': '第六镜是战后收束，必须同时看到勇者与巨龙尸体，不能重新变回对峙。',
            'jimeng_ref_prompt': (
                f'{style_txt}，远景低机位收束镜。{human}位于前景一侧落地站稳并收剑回稳，姿态平稳，圣剑垂在身侧且清晰可见；中远景清楚可见{dragon}横向倒伏的尸体，头部、前肢和塌落双翼可见，眼睛不再发光，明确战斗已经结束。'
                f'画面必须同时看到{human}与{dragon}尸体，不能重新变回对峙。{scene_lock}{common_rule}避免继续对砍、主体缺失或纯白背景。'
            ),
            'video_prompt': (
                f'保持首帧中的战后空间关系不变。前30%：{human}落地后轻微屈膝缓冲并收住重心；'
                f'中40%：披风和尘土缓缓落下，{dragon}尸体在中远景横向倒伏不再动作，双翼塌落贴地；后30%：镜头轻微拉远形成收束定格，明确战斗结束，不要重新站起对峙。'
            ),
            'identity_locks': [f'{human}身份锁：{hero_lock}', f'{dragon}身份锁：{dragon_lock}'],
        },
    ]

    print('[v64战斗导演] 已按最新反馈重排6张分镜：强化压迫→拍地跳闪→蓄力怒吼→空中特写重斩→魔法圣剑贯穿→尸体收束')
    return fixed

def generate_story(direction, genre="校园", length="中篇", custom_requirements=""):
    """生成 AI 动态漫剧制作脚本 + 角色 + 场景。

    v17 自动工作流版：
      1. 用户输入只当作粗略创意，不要求用户会写专业提示词；
      2. 后端自动导演化：降难、拆分、补足4秒镜头内容；
      3. 剧本阶段仍保持轻量JSON，不输出复杂 story_beats；
      4. 如果生成结果仍偏压缩小说，会自动二次修复 story_text。
    """
    system = (
        "你是AI动态漫剧导演和制作脚本编剧。用户输入的是粗略故事创意，不是最终剧本；"
        "你必须自动把创意改写成适合AI生图和图生视频的制作脚本。\n"
        "必须遵守：\n"
        "1. story_text按镜头段落书写，每段对应一个可制作镜头。\n"
        "2. 每段45-85个中文字，只包含一个主要动作，并能支撑至少4秒视频。\n"
        "3. 自动规避高风险动作：超高速连续连招、复杂攀爬、精确刺杀、站在细小物体上、复杂缠斗。\n"
        "4. 如果用户输入包含高难动作，要改写为可拍的稳定战斗阶段，不要照抄，但也不要把整场战斗削成静止摆拍。\n"
        "5. 巨大生物、骑乘、站在龙背上等空间关系必须写清楚。\n"
        "6. 同一角色在所有镜头中必须保持稳定外貌、服装颜色、发型、武器样式，不要随意变化。\n"
        "7. 中文‘少年’在画面生成中应理解为17-19岁青年/青年勇者，不能画成明显儿童。\n"
        "8. 若多数镜头处于同一主场景，需反复保留同一组环境锚点，保证场景连续性。\n"
        "8. 场景描述只写环境，不把角色或动作写进场景。\n"
        "9. 输出必须是合法JSON对象，不要Markdown，不要解释。"
    )

    genre_hints = {
        "校园": "青春校园动态漫剧，使用教室、走廊、操场等清晰空间，动作以对话、递物、回头、停顿为主",
        "都市": "现代都市动态漫剧，强调街道、办公室、公寓等空间和人物关系，动作生活化且可拍",
        "奇幻": "东方奇幻或异世界动态漫剧，战斗要更激烈、更充分，优先写多轮交锋、压制与反制、爆发与回稳，不要一击秒杀；每镜聚焦一个清晰战斗阶段，保留强烈视觉冲突",
        "科幻": "近未来科技动态漫剧，动作围绕设备启动、光效变化、机械运动和空间变化展开",
        "悬疑": "推理悬疑动态漫剧，线索发现、靠近、回头、凝视、逃跑等动作清楚，强调光影节奏",
        "治愈": "温暖日常动态漫剧，节奏舒缓，适合稳定镜头、细小动作和情绪停留",
        "武侠": "中国武侠动态漫剧，动作可写试探交锋、闪避反制、压迫推进与关键出招，但每镜仍只聚焦一个战斗阶段，不要一句塞满整场打斗",
        "历史": "中国历史古代动态漫剧，强调服饰、礼仪、场景氛围和关键动作",
        "恐怖": "恐怖惊悚动态漫剧，以缓慢逼近、回头、颤抖、光影变化制造恐怖感",
        "末世": "末日后科幻动态漫剧，动作围绕生存、对峙、撤离、守护，避免大规模复杂群像",
        "言情": "甜蜜浪漫动态漫剧，动作以眼神、递物、靠近、停顿、回头为主",
        "热血": "热血竞技励志动态漫剧，强调连续对抗、攻防转换、爆发前的压迫与关键反击，不要一击结束全部战斗",
        "战争": "战争军事动态漫剧，动作以掩护、冲锋前后、爆炸光影和情绪停留为主",
        "体育": "运动竞技动态漫剧，动作拆成准备、启动、关键瞬间、结果反应",
    }
    length_profiles = {
        "短篇": {"shot_range": "3-4", "word_range": "240-360字", "desc": "3-4个有效镜头，适合20-30秒短视频"},
        "中篇": {"shot_range": "4-5", "word_range": "380-540字", "desc": "4-5个有效镜头，适合30-45秒视频"},
        "长篇": {"shot_range": "5-6", "word_range": "520-720字", "desc": "5-6个有效镜头，适合45-60秒视频；内容不足时用5镜，不要硬凑6镜"},
    }
    profile = length_profiles.get(length, length_profiles["中篇"])
    director_brief_v28 = _auto_director_brief_v28(direction, genre, length, custom_requirements)

    def _build_prompt(strict=False):
        extra = "" if not custom_requirements else f"\n【额外要求】{custom_requirements}"
        strict_line = "\n注意：JSON必须短，不要输出story_beats，不要输出多余字段。" if strict else ""
        return f"""请创作一个{genre}题材的 AI 动态漫剧制作脚本。{strict_line}

【用户原始创意】
{direction or '自由发挥一个具有画面感的故事'}

【系统自动解析出的导演提纲】
{director_brief_v28}

【重要定位】
用户原始创意可能是小说式梗概，可能包含难以生成的动作。你不能照抄，而要自动完成“AI导演预处理”：降难、拆分、补足镜头内容，使其适合后续Seedream生图和Seedance图生视频。

【题材风格】{genre_hints.get(genre, genre)}
【篇幅要求】{profile['desc']}，正文约{profile['word_range']}
【镜头数量】{profile['shot_range']}个有效镜头{extra}

【自动导演规则】
1. story_text必须按镜头段落写，段落之间用\\n换行；每段就是一个镜头。
2. 每段55-105个中文字，必须包含：稳定场景 + 明确角色位置 + 一个主要动作阶段 + 4秒内可表现的小变化或结果变化；禁止只写一句概括。
3. 不要把多个复杂动作写成一句，例如“跃起、攀升、刺下、倒地”不能塞在同一段；但允许一个镜头内部保留“起势→交锋”或“压制→反制”这种同一空间关系下的短动作链。
4. 高难动作要自动改写为稳定关键画面：如“跃上翼骨边缘”“站在龙背稳住身体”“举剑蓄势”“正面交锋后被震退”“巨龙俯冲压迫后少年反冲”。
5. 若用户写“站在龙角上”，优先改成“站在断裂龙角旁”或“站在倒下的巨龙旁”。
6. 若出现“骑龙/站在龙背/站在巨兽身上”，要写清：画面中只有一个承载主体，角色在其背部或身体上方，不是左右对峙。
7. 内容不足时宁可少一镜，不要拆空镜；但长篇应尽量达到5个以上有效镜头。
8. 长篇或战斗题材要保留2-3个明确战斗阶段：例如逼近对峙、第一次交锋、压制或反制、高光反击、结果收束。除非用户明确要求，禁止“一剑秒杀”式直接结束战斗。
9. 战斗题材可以更激烈，但每一镜只允许一个核心战斗阶段，不要把整场战斗压成一句；优先写“接近、交锋、震退、回稳、再反击”这种可拍过程。
10. 相邻镜头处于同一主场景时，要重复核心环境锚点，保持场景连续，不要每段都突然换一套光线和地点。
11. 角色在全篇中必须保持同一套核心外貌特征、服装配色和标志性道具。
12. 奇幻题材中的少年/勇者默认采用圣骑士风轻型或中型铠甲，包括胸甲、护肩、护臂、腰带、护腿、长靴和短披风，手持圣剑；不要写成紧身衣、贴身皮甲、连体战斗服，也不要只写普通布袍。
13. 若主场景是焦黑平原/荒原/废土，优先写焦土、裂痕、灰烬、烟云、余晖、压迫感，不要默认写狂风肆虐、飞沙走石，除非用户明确要求风暴或强风；场景字段绝不能写“远处巨龙盘踞/勇者站立/双方对峙”。
14. 如果角色名是“少年”或“勇者”，中文外貌必须写明“18岁左右青年男性、不是儿童、青年体型比例”。
15. 角色2-3个最佳；场景1-2个最佳，避免过多场景碎片化。
16. 每镜 characters_in_shot 只填写真正参与本镜主要动作的命名角色；路人、同学、人群、士兵、车辆、怪物群等如果只是背景环境，不要写入 characters_in_shot。

【输出JSON格式】
{{
  "title": "剧本标题，5-10字",
  "story_text": "第一镜段落\\n第二镜段落\\n第三镜段落",
  "characters": [
    {{
      "name": "角色中文名",
      "description": "中文外貌描述，80-160字。必须写清年龄/性别或物种、发型发色、眼睛、服装配色与款式、鞋靴、配饰、体型、标志性道具。若角色是少年/勇者，必须写明18岁左右青年男性、不是儿童；服装优先写蓝白配色圣骑士轻甲，包含胸甲、肩甲、护腕、腰带、护腿、长靴、短披风和圣剑；非人类要包含体表材质、眼睛、翅膀/角/尾巴等核心特征。",
      "personality": "性格，中文15字内",
      "voice_style": "说话风格，中文10字内"
    }}
  ],
  "scenes": [
    {{
      "name": "场景名",
      "description_zh": "纯环境描述，20-35字，只写地点、光线、天气、关键环境元素，不写角色/生物/动作；主场景要有稳定锚点。像焦黑平原这类场景优先写焦土、裂痕、灰烬、烟云，不要写远处巨龙盘踞，不要默认写狂风肆虐",
      "time_of_day": "具体时间"
    }}
  ],
  "genre": "{genre}",
  "mood": "整体氛围"
}}

只输出合法JSON对象。"""

    # 第一次：自动导演化生成，保持 JSON 轻量。
    result = _call_llm(system, _build_prompt(strict=False), temperature=0.42, max_tokens=6200, json_mode=True, task_name="story-generation")
    if not result.get('success'):
        if _v47_is_hero_dragon_context(' '.join([str(direction or ''), str(custom_requirements or ''), str(genre or '')])):
            print('[v47故事兜底] LLM失败，启用本地勇者巨龙一轮交锋占上风剧本')
            return {'success': True, 'story': _v47_local_hero_dragon_story(direction, genre, length), 'warning': result.get('message', '')}
        return result

    last_error = None
    for attempt, raw in enumerate([result.get('content', '')]):
        try:
            data = _parse_json(raw)
            data = _normalize_story_result(data, genre=genre, length=length)
            data = _repair_story_text_for_ai_production(data, genre=genre, length=length)
            data = _v47_force_hero_dragon_story(data, direction=direction, genre=genre, length=length, custom_requirements=custom_requirements)
            return {'success': True, 'story': data}
        except Exception as e:
            last_error = e
            print(f"[剧本] 第{attempt+1}次JSON解析失败: {e}")
            print(f"[剧本] 原始响应前500字: {raw[:500]}")

    # 第二次：极简重试，降低字段量和创造性。
    retry_result = _call_llm(system, _build_prompt(strict=True), temperature=0.22, max_tokens=4800, json_mode=True, task_name="story-generation-retry")
    if retry_result.get('success'):
        try:
            data = _parse_json(retry_result.get('content', ''))
            data = _normalize_story_result(data, genre=genre, length=length)
            data = _repair_story_text_for_ai_production(data, genre=genre, length=length)
            data = _v47_force_hero_dragon_story(data, direction=direction, genre=genre, length=length, custom_requirements=custom_requirements)
            print("[剧本] ✓ 极简重试解析成功")
            return {'success': True, 'story': data}
        except Exception as e2:
            last_error = e2
            print(f"[剧本] 极简重试仍解析失败: {e2}")
            print(f"[剧本] 重试响应前500字: {retry_result.get('content','')[:500]}")

    # 最终兜底：如果能从原始文本里提取 story_text，就返回可继续编辑的半结构化结果。
    raw = result.get('content', '')
    title = _extract_json_string_field(raw, 'title') or '未命名动态漫剧'
    story_text = _extract_json_string_field(raw, 'story_text')
    if story_text:
        fallback = {
            'title': title,
            'story_text': story_text,
            'story_beats': [],
            'characters': [],
            'scenes': [],
            'genre': genre,
            'mood': '',
        }
        fallback = _repair_story_text_for_ai_production(fallback, genre=genre, length=length)
        fallback = _v47_force_hero_dragon_story(fallback, direction=direction, genre=genre, length=length, custom_requirements=custom_requirements)
        print("[剧本] ⚠ 使用story_text兜底返回，角色/场景可能需要手动检查")
        return {'success': True, 'story': fallback, 'warning': '剧本结构不完整，已保留story_text兜底'}

    return {'success': False, 'message': f'剧本JSON解析失败（已重试）: {last_error}'}


# ═══════════════════════════════════════════════════════
# 1. 角色提取
# ═══════════════════════════════════════════════════════
def extract_character_description(story_text):
    """从剧情文本中提取角色信息（豆包中文主链路：角色描述保持中文）。"""
    system = "你是剧本分析师，擅长从文本中提取角色视觉信息。只输出JSON数组。"
    prompt = f"""从以下剧情中提取所有出场角色的完整信息。

【剧情】{story_text}

【要求】
1. description 必须是中文详细外貌描述，80-160字。根据角色类型灵活调整：

   【人类角色】必须包含：性别与年龄感、发型发色、眼睛、上装、下装、鞋靴、配饰、体型、标志性道具。
   示例："17岁少女，黑色长直发齐刘海，棕色眼睛，穿白色水手领校服上衣、藏青百褶裙、白色过膝袜和黑色圆头皮鞋，红色发带，身形纤细。"

   【非人类角色（龙/巨龙/机器人/精灵/动物/怪物/幽灵等）】按照角色本身的特征描述，禁止强加人类字段。
   巨龙示例："远古巨龙，体型巨大，通体覆盖银灰金属质感鳞片，金色发光眼睛，巨大皮膜翅膀，蛇形长尾带尖刺，弯曲双角，锋利巨爪。"
   机器人示例："人形战斗机器人，银白金属外甲，胸口和肩部有蓝色发光纹路，关节机械结构清晰，黑色面罩式发光眼屏，身高约两米半。"

2. 如果剧情没明说服装/外观细节，也要根据场景和角色类型合理补全（校园人→校服；都市人→固定休闲装；奇幻人→古风或铠甲；巨龙→鳞片、翅膀、角、尾巴、爪）。
3. 少年/勇者必须写成18岁左右青年男性、不是儿童，并锁定服装和武器。
4. personality 中文15字内，voice_style 中文10字内（非人类角色可写"低沉怒吼""机械合成"等）。

【返回格式】
[
  {{
    "name": "角色中文名",
    "description": "完整中文外貌描述",
    "personality": "性格(中文)",
    "voice_style": "说话风格(中文)"
  }}
]"""
    result = _call_llm(system, prompt, temperature=0.3, max_tokens=2500)
    if not result['success']:
        return result
    try:
        chars = _parse_json(result['content'])
        if not isinstance(chars, list):
            return {'success': False, 'message': '返回格式错误：不是数组'}
        for c in chars:
            d = c.get('description', '')
            d = re.sub(r'\s+', ' ', d).strip(' ，,;；')
            c['description'] = _enrich_character_description_zh(c.get('name', ''), d, story_text=story_text, genre='')
        return {'success': True, 'characters': chars}
    except Exception as e:
        return {'success': False, 'message': f'角色解析失败: {e}'}


# ═══════════════════════════════════════════════════════
# 2. 场景规范 ★ 中文主链路
# ═══════════════════════════════════════════════════════
def _purge_characters_from_scene(text, char_names=None):
    """★ v14: 净化场景描述, 剥离误混入的角色名/人物动作句
    - 去角色名
    - 去以人物代词/名字开头的分句(含集合代词 二者/两者/双方 等)
    - 去明显动作动词的短语
    - 去"勾勒出XX轮廓"类隐式指人短语
    """
    if not text:
        return text
    s = text
    # 1. 去掉所有角色名(整串替换)
    for n in (char_names or []):
        if n and len(n) >= 1:
            s = s.replace(n, '')

    # ★ v14 扩展的人物词表
    # 个体代词
    _person_words = (
        # 基础代词
        r'他|她|它|他们|她们|它们|'
        # 集合代词 ★ 关键新增
        r'二人|两人|二者|两者|双方|彼此|众人|一行人|一行|群人|一群|数人|数个|一队|几人|三人|四人|几个人|'
        # 身份/年龄称谓
        r'主角|主人公|主角们|男主|女主|少年|少女|男子|女子|男孩|女孩|青年|壮年|老人|老者|老汉|老妇|孩童|小孩|儿童|'
        # 奇幻/职业 ★ 关键新增
        r'巨龙|巨兽|恶龙|魔物|怪物|妖兽|神兽|灵兽|魔兽|野兽|猛兽|骑士|战士|剑士|勇士|法师|魔法师|术士|圣骑士|'
        r'公主|王子|皇帝|国王|将军|士兵|武士|忍者|刺客|盗贼|精灵|矮人|兽人|魔王|魔神|'
        # ★ v14.3 新增: 现代职业/身份
        r'学生|学生们|同学|同学们|老师|教师|校长|班长|教授|博士|硕士|学者|研究员|研究生|'
        r'医生|护士|警察|军人|工人|司机|厨师|服务员|店员|店主|老板|总裁|CEO|经理|'
        r'艺术家|画家|作家|诗人|音乐家|歌手|演员|明星|记者|主持人|'
        r'侦探|间谍|特工|杀手|保镖|'
        # 复数限定词
        r'这人|那人|此人|某人|路人|行人|旅人|来者|来人'
    )

    # 2. 去掉以人物代词【开头】、到下一个标点为止的子句(前有标点分隔)
    s = re.sub(
        rf'[,，。、;；]\s*(?:{_person_words})[^,，。、;；]*',
        '',
        s,
    )
    # 3. 去掉【整段开头】的人物代词分句
    s = re.sub(
        rf'^(?:{_person_words})[^,，。、;；]*[,，。、;；]?',
        '',
        s,
    )
    # 4. 去掉典型对话/动作动词短句
    s = re.sub(
        r'[,，。、;；]\s*(?:说|喊|叫|问|答|走|跑|跳|坐|站|看|想|笑|哭|望|倒|举|拿|握|冲|扑|挥|砍|刺|劈|飞|盘旋|跃|怒吼|咆哮|低吼|嘶鸣|倒地|举剑|拔剑|紧握|瞪|凝视|冲向|扑向)[^,，。、;；]*',
        '',
        s,
    )

    # ★ v14 新增:
    # 5. 去掉"勾勒出 XX 轮廓"(XX 一般是人物/生物),  以及"二者""双方"类短语
    s = re.sub(
        r'[,，。、;；]?\s*[^,，。、;；]*(?:勾勒|映出|映衬|照出|显出|凸显|衬托|轮廓|剪影)[^,，。、;；]*',
        '',
        s,
    )
    # 6. 去掉包含"人物/角色"字样的短句
    s = re.sub(
        r'[,，。、;；]?\s*[^,，。、;；]*(?:人物|角色|身影|身姿|身形)[^,，。、;；]*',
        '',
        s,
    )
    # 7. 去掉包含"XX 站|坐|立|卧|躺|伏 在"这种明显人物位置句
    s = re.sub(
        r'[,，。、;；]?\s*[^,，。、;；]*(?:站|坐|立|卧|躺|伏|跪|蹲)(?:在|于)[^,，。、;；]*',
        '',
        s,
    )

    # ★ v14.3 修: 剥除"方位 + 是/站着/立着 + XXX"的短语
    # 典型: "对面是盘旋而来的远古巨龙" "前方是勇者" "面前站着一人"
    # ★ v14.4 修: 从谓语词里移除"有" (因"远处有山"合法)
    # ★ v14.4.1 修: 后面紧跟物体/地形词时不剥 (如"对面是湖泊""左边是书柜")
    _nonliving_words = (
        # 自然地形
        r'湖|湖泊|海|海洋|河|河流|江|溪|池|池塘|瀑布|山|山脉|山峰|山峦|山丘|山顶|山脚|岩|岩壁|悬崖|峭壁|'
        r'森林|树林|竹林|草原|平原|荒野|沙漠|戈壁|冰原|雪原|绿洲|田野|稻田|'
        # 家具
        r'书柜|书架|衣柜|衣橱|橱柜|柜子|床头柜|梳妆台|五斗柜|'
        r'沙发|椅子|板凳|凳子|躺椅|摇椅|'
        r'桌子|木桌|石桌|圆桌|长桌|餐桌|书桌|茶几|案几|供桌|'
        r'床|大床|单人床|双人床|婴儿床|'
        r'电视|冰箱|洗衣机|空调|微波炉|烤箱|灯|吊灯|台灯|壁灯|'
        # 建筑部件
        r'窗户|窗|门|大门|楼梯|台阶|走廊|过道|墙|墙壁|屋顶|天花板|地板|地砖|瓦|砖|柱|梁|'
        # 场所/空间
        r'街道|小路|大道|马路|走廊|广场|庭院|院子|花园|菜园|果园|阳台|露台|栅栏|围墙|'
        # 建筑物
        r'建筑|楼房|高楼|大厦|别墅|房屋|木屋|茅屋|小屋|'
        r'城墙|古城|小城|城池|城堡|要塞|'
        r'塔|高塔|古塔|宝塔|灯塔|烽火台|'
        r'亭|亭子|凉亭|阁|楼|馆|寺|庙|教堂|宫|殿|堂|祠|'
        # 通用兜底: "X桌|X椅|X柜|X床|X灯|X架|X台"
        r'[\u4e00-\u9fff]?(?:桌|椅|柜|床|灯|架|台|案)'
    )
    s = re.sub(
        rf'[,，。、;；]?\s*(?:对面|前方|后方|左边|右边|左侧|右侧|面前|身前|身后)(?:\s*(?:是|站着|立着)|[^,，。、;；]{{0,3}}(?:是|站着|立着))\s*(?!(?:一片|一座|一条|一幅|一块|一棵|一张)?(?:{_nonliving_words}))[^,，。、;；]*',
        '',
        s,
    )
    # "远处" 单独处理: 仅当后面跟量词/人物代词时剥除, 跟普通名词时保留
    # 例如 "远处一只狐狸" 剥; "远处有山" "远处的山林" 保留
    s = re.sub(
        r'[,，。、;；]?\s*远处\s*(?:一只|一头|一个|一名|一位|一条|几只|几头|几个|数只|数头|两只|两头|两人|两个|是|站着|立着)[^,，。、;；]*',
        '',
        s,
    )

    # ★ v14.3 新增: 剥除主谓结构——名词 + 常见人物动作动词
    # 触发: "学生们收拾书包" "将军举剑" "士兵冲锋" "博士操作"
    # 只作用在已有主语词后接动词的情况
    _verb_pattern = (
        r'收拾|整理|放|拿|握|举|操作|指挥|命令|训练|练习|挥|砍|刺|劈|'
        r'冲|扑|攻|守|跑|走|跳|飞|站|坐|卧|躺|'
        r'说|喊|叫|笑|哭|骂|笑着|哭着|喊着|'
        r'望|盯|瞪|看|瞥|凝视|注视|'
        r'思考|想|沉思'
    )
    s = re.sub(
        rf'[,，。、;；]?\s*[^,，。、;；]{{1,8}}?(?:{_verb_pattern})[^,，。、;；]*',
        lambda m: '' if any(
            v in m.group(0) for v in (
                '收拾','举','冲','扑','挥','砍','刺','劈',
                '说','喊','叫','凝视','指挥','操作','训练'
            )
        ) else m.group(0),
        s,
    )

    # ★ v14.4: 剥除"(方位词)? 量词 + XXX"
    # 典型漏网: "一头巨兽咆哮" "几个身影出现" "远处，一只正展翅盘旋而来"
    # 方位词+量词的组合时, 把方位词也一并剥掉(包括前置的孤立"远处。")
    s = re.sub(
        r'[,，。、;；]?\s*(?:远处|近处|前方|后方|空中|半空|天空|不远处|不远|上空)?[,，、]?\s*'
        r'(?:一只|一头|一个|一名|一位|一条|几只|几头|几个|几名|数只|数头|数个|数名|两只|两头|两名)'
        r'[^,，。、;；]*',
        '',
        s,
    )

    # ★ v14.4 新增: 剥除飞行/盘旋等明显生物动作短语
    # 因为它们强烈暗示生物主语: "展翅盘旋而来" "腾空而起" "从天而降"
    s = re.sub(
        r'[,，。、;；]?\s*[^,，。、;；]*(?:展翅|盘旋|翱翔|振翅|拍翅|腾空|从天而降|凌空|翻腾|盘绕|蜿蜒而来)[^,，。、;；]*',
        '',
        s,
    )
    # ★ v14.1: 去掉"XX 洒在/落在/照在 身上/肩上/脸上/头上/面孔/脸颊"
    # 触发典型: "余晖洒在身上" "月光落在脸上" "光芒映在头顶"
    _body_parts = (
        r'身上|身体|肩上|肩膀|脸上|脸颊|脸庞|面孔|面容|头上|头顶|发丝|发间|'
        r'眼中|眼里|眼眸|眼眶|手上|手中|手心|指尖|胸前|背上|背后|额头'
    )
    s = re.sub(
        rf'[,，。、;；]?\s*[^,，。、;；]*(?:{_body_parts})[^,，。、;；]*',
        '',
        s,
    )

    # ★ v14.1 新增: 剥离角色名后留下的残句, 如 "洒在和上" "映照着与之间"
    # 当角色名被 replace 掉后, 可能剩下介词+方位的孤立短语
    # 只处理真正孤立的残句: "介词+和/与+标点", 不处理"XX和XX"这种正常词
    s = re.sub(r'[,，。、;；]?\s*(?:洒在|落在|照在|映在|投在|打在|映照着|映出)\s*(?:和|与|以及|跟)?\s*(?:之上|之间|身上|面前)?\s*(?=[,，。、;；]|$)', '', s)
    # 孤立的 "和|与" 残留 (前后都是标点或句首/句尾)
    s = re.sub(r'(?<=[,，。、;；])\s*[和与]\s*(?=[,，。、;；]|$)', '', s)
    s = re.sub(r'^\s*[和与]\s*(?=[,，。、;；])', '', s)

    # ★ v14.4: 清理方位词被剥得只剩孤零零的情况
    # 典型: "...尘土飞扬。远处。" → "...尘土飞扬。"
    s = re.sub(
        r'[,，。、;；]\s*(?:远处|近处|前方|后方|空中|半空|天空|不远处|不远|上空|左边|右边|左侧|右侧)\s*(?=[,，。、;；]|$)',
        '',
        s,
    )

    # 8. 清理多余标点和空白
    s = re.sub(r'[,，、;；]{2,}', '，', s)
    s = re.sub(r'^[,，、;；\s]+', '', s)
    s = re.sub(r'[,，、;；\s]+$', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()


def generate_scene_spec(story_text, global_scene='', style='日漫', characters=None):
    """生成场景的中英双语规范 — v13 加固版
    ★ 核心修复:
      1. 用户没填 global_scene 时不再直接把剧情前 400 字当场景, 而是
         让 LLM 从剧情里"推断"场景类型, 并严禁写入任何人物/动作/剧情
      2. 返回前做净化, 剥离仍可能漏网的人物短句
      3. scene_spec_zh 给 Seedream 4.5 用
         scene_spec_en 仅兼容旧字段，豆包主链路优先使用中文
    """
    if not global_scene and not story_text:
        return {'success': True, 'scene_spec': '', 'scene_spec_zh': ''}

    style_hints = {
        '日漫': 'Japanese anime 2D style',
        '国漫': 'Chinese manhua style',
        '美漫': 'American comic book style',
        '写实': 'semi-realistic anime style',
        '水墨': 'Chinese ink wash style',
        '赛博朋克': 'cyberpunk anime style',
        '古风': 'Chinese ancient fantasy style',
        '吉卜力': 'Studio Ghibli style',
        '新海诚': 'Makoto Shinkai style',
    }

    # ★ v13: 分两路取场景参考
    # - 如果用户填了 global_scene, 优先用它
    # - 否则让 LLM 从剧情里推断(只取场景, 不复述剧情)
    user_scene = (global_scene or '').strip()
    story_snippet = (story_text or '').strip()[:300]

    char_name_list = ', '.join(
        c.get('name', '') for c in (characters or []) if isinstance(c, dict) and c.get('name')
    )

    system = (
        "你是动漫场景设计师, 专门设计【纯环境】场景规范。"
        "只输出 JSON, 不输出任何其他内容。"
        "严格禁止在场景描述里写任何人物、角色、动作、对话、情绪、剧情。"
    )

    if user_scene:
        # 用户指定了场景, 直接规范化
        prompt = f"""把以下场景描述规范化为【纯环境】视觉规范(中英双语)。

【用户提供的场景】{user_scene}
【画风】{style} ({style_hints.get(style, 'anime')})

【严格禁止 — 违反即无效】
❌ 不得写入任何人物(他/她/主角/少年/少女/男子/女子/名字等)
❌ 不得写入任何集合代词(二人/两人/二者/双方/彼此/众人等)
❌ 不得写入任何奇幻生物角色(巨龙/魔物/精灵/骑士/勇士/法师等当成剧中角色的)
❌ 不得写入任何动作(走/跑/坐/看/说/握/举/盘旋/咆哮等)
❌ 不得写入任何对话、情绪、剧情事件
❌ 不得出现"人""person""character""boy""girl"等词
❌ 不得使用"勾勒出XX轮廓""映出XX身影""照出XX身形"这类隐式指人表达

【必须包含】
✅ 空间布局(室内/室外/地形)
✅ 建筑/家具/陈设(如有)
✅ 光线方向与色温
✅ 色彩基调
✅ 天气、时间段
✅ 氛围关键词

【字数 — 简洁优先】
- scene_spec_zh: 25-40 中文字 (够用即可，不要文学化修辞)
- scene_spec_en: 15-25 English words, NO humans mentioned

【输出JSON, 不要任何额外文字】
{{"scene_spec_zh":"...","scene_spec_en":"..."}}"""
    else:
        # 没有场景, 从剧情推断
        prompt = f"""从以下剧情中【仅提取场景环境】, 输出视觉规范(中英双语)。

【剧情片段 — 仅用于推断场景类型, 绝对不要复述剧情内容】
{story_snippet}

【画风】{style} ({style_hints.get(style, 'anime')})
【剧中角色名】{char_name_list or '(无)'}

【严格禁止 — 违反即无效】
❌ 不得写入任何上述角色名
❌ 不得写入任何人物(他/她/主角/少年/少女/男子/女子等)
❌ 不得写入任何集合代词(二人/两人/二者/双方/彼此/众人等)
❌ 不得写入任何奇幻生物角色(巨龙/魔物/精灵/骑士/勇士/法师等当成剧中角色的)
❌ 不得写入任何动作(走/跑/坐/看/说/握/举/盘旋/咆哮等)
❌ 不得写入量词指代生物("一只""一头""一个""几个"等)
❌ 不得写入任何对话、情绪、剧情事件
❌ 不得出现"人""person""character""boy""girl"等词
❌ 不得使用"勾勒出XX轮廓""映出XX身影""照出XX身形"这类隐式指人表达
❌ 不得复述剧情中的任何事件

【必须包含】
✅ 空间布局(室内/室外/地形)
✅ 关键环境特征(建筑/家具/地貌/植被)
✅ 光线方向 + 天气 + 时间

【字数 — 简洁优先】
- scene_spec_zh: 25-40 中文字 (够用即可，不要文学化修辞)
- scene_spec_en: 15-25 English words, NO humans mentioned

【输出JSON, 不要任何额外文字】
{{"scene_spec_zh":"...","scene_spec_en":"..."}}"""

    result = _call_llm(system, prompt, temperature=0.3, max_tokens=1500)
    if not result['success']:
        return result
    try:
        data = _parse_json(result['content'])
        zh = data.get('scene_spec_zh', '').strip()
        en = data.get('scene_spec_en', '').strip()

        if not _contains_enough_chinese(zh):
            zh = _fallback_scene_spec_zh(global_scene=user_scene, story_text=story_snippet, scene_spec_en=en or zh)

        # ★ v13 净化: 即使 LLM 偷偷写了人物也剥掉
        char_names_for_purge = [
            c.get('name', '') for c in (characters or []) if isinstance(c, dict)
        ]
        zh = _purge_characters_from_scene(zh, char_names_for_purge)

        # 清洗 en 中的中文
        en = re.sub(r'[\u4e00-\u9fff]+', ' ', en)
        en = re.sub(r'\s+', ' ', en).strip()
        # 英文也剥掉典型人物词 (保守做法)
        en = re.sub(
            r'\b(?:he|she|they|him|her|his|hers|them|their|the\s+(?:boy|girl|man|woman|protagonist|character|person|hero|heroine))\b[^.,;]*',
            '',
            en,
            flags=re.IGNORECASE,
        )
        en = re.sub(r'\s+', ' ', en).strip(' ,;.')

        return {
            'success': True,
            'scene_spec': en,      # 兼容字段(英文)
            'scene_spec_zh': zh or _fallback_scene_spec_zh(global_scene=user_scene, story_text=story_snippet, scene_spec_en=en),   # 中文版(给豆包用)
            'scene_spec_en': en,   # 显式英文版
        }
    except Exception:
        # fallback: 全清中文的旧行为 + 净化
        spec = re.sub(r'[\u4e00-\u9fff]+', ' ', result['content']).strip()
        spec = re.sub(r'\s+', ' ', spec)
        return {
            'success': True,
            'scene_spec': spec,
            'scene_spec_zh': _fallback_scene_spec_zh(global_scene=user_scene, story_text=story_snippet, scene_spec_en=spec),
            'scene_spec_en': spec,
        }


# ═══════════════════════════════════════════════════════
# 3. 分镜脚本 ★★ 核心升级
# ═══════════════════════════════════════════════════════
def _get_style_examples(style: str) -> dict:
    """v12 新增: 按画风/题材返回合适的分镜示例集

    Returns dict with keys: 'single_char', 'two_char'
    """
    # ── 古风 / 武侠 ──
    if style in ("古风", "武侠", "历史", "水墨"):
        single = (
            "「中国古风精致插画，黄昏山间竹林小径，余晖透过竹叶洒下细碎金光，地面散落枯叶。"
            "画面主角是少侠云逸：二十岁男性，墨黑中长发束发髻配白色发带，"
            "深邃的黑色眼睛，穿着白色广袖武者道袍、腰束青色长绸、黑布靴，"
            "背负长剑，身形挺拔。他正单手按剑柄缓慢抽出半截剑身，侧目警惕地"
            "凝视竹林深处，眉宇间透出凌厉与专注。中景侧面四十五度仰视构图，"
            "竹叶在风中轻颤，光影随之微动」"
        )
        two = (
            "「中国古风精致插画，客栈大厅夜晚，红烛摇曳光影昏黄，桌上散落酒坛。"
            "画面左侧是云逸（白道袍、束发、背剑），侧身朝向右方，右手持酒碗向右侧举起。"
            "画面右侧是女侠苏鸢（藏青色窄袖劲装、黑发盘髻插碧玉簪、腰悬短刃），"
            "侧身朝向左方，两人目光相接于画面中央，各自举碗相碰，"
            "神情复杂——既有惺惺相惜又有无言对峙。"
            "近景正面对称构图，烛光在两人脸上投下温暖而摇曳的光影」"
        )
    # ── 奇幻（含非人类角色：龙/精灵/野兽/机器人示例）──
    elif style in ("奇幻",):
        single = (
            "「奇幻动漫风格精致插画，焦黑的平原战场黄昏，远山燃烧冒出黑烟，"
            "天空被火光染成血橙色，地面布满碎石和烧焦的战旗。画面主角是远古巨龙："
            "体长三十米，金属银色鳞片覆盖全身，金色发光的圆瞳眼，巨大皮革状翅膀"
            "半展，长蛇形尾部带尖刺，弯曲锋利的爪子深陷土地，头冠长有螺旋状尖角，"
            "口中露出利齿。它正俯首低吼，双翼微微张开准备起飞，金色眼睛直视前方。"
            "远景仰视全身构图，龙身上的鳞片在夕光下闪烁冷金属光泽」"
        )
        two = (
            "「奇幻动漫风格精致插画，古代神殿遗迹白天，断裂石柱间透进斜射阳光，"
            "地面长满青苔和野草。画面左侧是少年勇者艾伦：十八岁男性，金色短发、"
            "蓝色眼睛，穿着银色胸甲、白色内衬、深蓝色披风、棕色皮靴，右手举起一把"
            "发光的长剑。画面右侧是精灵弓手莉娅：长银发尖耳朵、翠绿色眼睛，穿着"
            "森绿色紧身皮甲、米色长裤、深棕色长靴，背负长弓和箭袋，左手搭箭。"
            "两人侧身朝向对方前方，准备共同迎战。近景平视构图，阳光在两人发丝"
            "和铠甲上形成金色高光」"
        )

    # ── 赛博朋克 / 科幻 ──
    elif style in ("赛博朋克", "科幻", "3D"):
        single = (
            "「赛博朋克动漫风格精致插画，近未来城市霓虹雨夜，潮湿的沥青地面倒映"
            "红蓝霓虹广告牌，背景高楼密布全息投影。画面主角是黑客零：二十二岁女性，"
            "银白短发遮半脸、单侧蓝色发丝，左眼是深蓝色虹膜右眼是橙色机械义眼，"
            "穿着黑色紧身皮夹克内衬荧光绿连帽卫衣、破损黑色工装裤、厚底机车靴，"
            "右手腕有红色电路纹身。她正蹲在一台故障的终端前，左手将指尖连接线插入"
            "主机接口，眼神专注而冷静。近景俯视三十度构图，雨水顺脸颊流下」"
        )
        two = (
            "「赛博朋克动漫风格，废弃工厂内激光警报室，红色闪烁灯光交替打亮黑暗空间。"
            "画面左侧是黑客零（银白短发、黑色皮夹克、机械义眼），背贴金属墙面向右侧"
            "探头。画面右侧是佣兵阿修（褐色寸头、脸部有纵向刀疤、穿军绿色战术背心），"
            "手持电磁枪指向左侧通道，两人背靠背呈警戒姿态，视线分别瞄准不同方向。"
            "中景平视广角构图，红色警报光在两人身上投下断续的危险色泽」"
        )
    # ── 恐怖 / 悬疑 ──
    elif style in ("恐怖", "悬疑"):
        single = (
            "「恐怖漫画风格高对比度插画，废弃学校走廊深夜，破碎的荧光灯管发出嗡嗡声"
            "忽明忽暗，地板腐烂翘起，墙壁有黑色污迹延伸至天花板。画面主角是田野："
            "十七岁少年，棕色乱发、苍白面孔、瞳孔因恐惧而散大，穿着白色校服衬衫"
            "（领口撕裂）和黑色校裤。他正双手撑墙缓缓后退，眼神惊恐地盯着画面"
            "外的某物，嘴唇微张发不出声音。特写近景仰视构图，背景阴影中有模糊的"
            "黑色轮廓」"
        )
        two = (
            "「恐怖漫画风格高对比度插画，阁楼昏暗，仅一根蜡烛光源。"
            "画面左侧是田野（白校服、棕色乱发，用颤抖的手举起蜡烛），"
            "右侧是同学美冬（黑色长发遮半脸、黑色连衣裙）双手指向地板裂缝，"
            "裂缝中渗出暗红色液体。两人都是侧面朝向裂缝的半身构图，蜡烛光"
            "在他们面孔上投下强烈的明暗对比，远处有隐约的脚步声提示」"
        )
    # ── 热血 / 体育 / 战争 ──
    elif style in ("热血", "体育", "战争"):
        single = (
            "「热血动漫风格精致插画，决赛赛场上午强烈白炽灯光，地板上反光，"
            "看台人声鼎沸虚化成背景色点。画面主角是队长林峰：十八岁男性，"
            "黑色短直发额前湿透、深邃的黑色眼睛，穿着红白两色球衣（背号10号）、"
            "黑色运动短裤、白色运动袜和红色战靴。他正以最大步伐向前冲刺，"
            "右腿大幅前迈、身体前倾，眼神锁定前方目标，汗珠在灯光下飞溅。"
            "低角度仰视全身构图，动感模糊强调速度」"
        )
        two = (
            "「热血动漫风格精致插画，赛后更衣室昏黄灯光，金属储物柜排列背景。"
            "画面左侧是林峰（红白球衣、黑发汗湿，低头喘气手撑膝盖，侧身朝向右方），"
            "右侧是副队陈壮（同款球衣01号、寸头、宽肩，从右侧走上前侧身朝向左方）。"
            "陈壮右手伸出拍了一下林峰左肩，两人侧面朝向对方的中景构图，"
            "动作简单但情感浓缩在这一拍之中，陈壮嘴角微扬不说话，林峰缓缓抬起头」"
        )
    # ── 言情 / 治愈 ──
    elif style in ("言情", "治愈"):
        single = (
            "「清新日系动漫风格精致插画，初春河边下午，樱花树投下柔和粉色光影，"
            "河水倒映天空，微风使花瓣飘落在少女发间。画面主角是林夏：十七岁女性，"
            "栗色中长发扎低马尾有碎刘海，明亮的浅棕色眼睛，穿着白色宽松毛衣、"
            "淡粉色百褶裙、米色厚底靴，手捧一杯热饮。她站在河边栏杆旁望向远处，"
            "嘴角有若有若无的笑意，目光柔软而温柔。中景半侧面构图，花瓣在"
            "她身周轻轻旋落」"
        )
        two = (
            "「清新日系动漫风格，公园长椅黄昏，暖橙色夕光从树隙斜射，"
            "地面铺满金黄落叶。画面左侧是林夏（栗色马尾、白毛衣、淡粉裙），"
            "右侧是男生顾年（黑色短发露额头、棕色眼睛、深蓝色卫衣、米色休闲裤），"
            "两人并排坐在长椅上，中间距离约一拳。顾年将一个热乎乎的糖炒栗子纸袋"
            "递向林夏那侧，林夏侧头看向纸袋再看向他，眼睛弯起来。近景平视构图，"
            "夕光把两人侧脸都镀上了金色」"
        )
    # ── 默认：日漫 / 校园 / 热血战斗 ──
    else:
        single = (
            "「热血日漫风格精致插画，焦黑平原黄昏，天幕被赤橙余晖与压低的乌云切开，"
            "地面遍布裂痕、碎石与翻卷灰烬。画面主角是少年勇者：十八岁男性，黑色短发，"
            "坚定的深色眼睛，穿着蓝白配色轻甲、胸甲肩甲护腕、长靴与短披风，双手握住发光圣剑。"
            "他侧身压低重心，披风被热风掀起，正准备迎击前方强敌。中景低角度构图，完整背景清晰可见，"
            "突出热血战斗前的蓄势感，禁止白底站桩和看向镜头摆拍」"
        )
        two = (
            "「热血日漫风格精致插画，焦黑战场黄昏，低空烟尘翻卷，斜射余晖穿过厚重云层。"
            "镜头位于少年勇者身后或肩后，前景下方是体型较小的勇者背影：黑色短发、蓝白轻甲、短披风、手握发光圣剑；"
            "远处天空中只有1条巨大的远古巨龙张翼盘旋，暗色鳞片、弯角、巨爪与长尾清晰，体型至少是勇者的八码以上，"
            "占据画面上半部形成强烈压迫感。远景仰视构图，完整背景，禁止第二条巨龙，禁止平视并排对打，禁止角色复制」"
        )
    return {"single": single, "two": two}


def generate_storyboard_script(story_text, characters, style='日漫',
                                global_scene='', scene_spec='',
                                scene_spec_zh='', global_tone='', **kw):
    """生成分镜脚本 — 为 Seedream 4.5 + Seedance 1.5 Pro + Seed 2.0 Lite 定制
    ★ 新增特性:
      1. jimeng_ref_prompt: 300字自然语言画面描述(自带角色外貌/场景/动作)
      2. video_prompt: 250字动态序列描述(先→然后→接着)
      3. 镜间一致性约束(同一角色外貌必须一致, 场景必须连续)
      4. 支持5-6镜, 目标总时长44-60秒
    """
    if not characters:
        characters = []

    tl = len((story_text or '').strip())
    paragraphs_for_count = _split_story_paragraphs(story_text)
    para_count = len(paragraphs_for_count)
    # v17: 自动导演化剧本通常已经按段落组织，优先尊重段落数量。
    # 不再为“凑6镜”强行拆分；但如果已有5-6个有效段落，就允许生成对应镜数。
    length_mode = _normalize_length_mode_v14(kw.get('length_mode') or kw.get('length') or '')
    numbered_segments_v14 = _extract_numbered_story_segments_v14(story_text)
    numbered_count_v14 = len(numbered_segments_v14)
    length_ranges = {
        '短篇': (3, 4, '3-4'),
        '中篇': (4, 5, '4-5'),
        '长篇': (5, 6, '5-6'),
    }
    # v14: 前端可能传“长篇 — 5~6个分镜”，不能按短文本退回4镜；
    # 若剧本文本已经含“第一镜...第六镜”，必须尊重显式镜头数量。
    if numbered_count_v14 >= 5:
        mn = numbered_count_v14
        mx = min(numbered_count_v14, 8)
        ts = str(numbered_count_v14)
    elif length_mode in length_ranges:
        mn, desired_max, range_label = length_ranges[length_mode]
        mx = desired_max
        ts = range_label
    else:
        mn = 3
        if tl < 120:   mx, ts = 3, "3"
        elif tl < 280: mx, ts = 4, "4"
        elif tl < 520: mx, ts = 5, "4-5"
        else:          mx, ts = 6, "5-6"

    # 角色档案 — 给 LLM 看完整外貌，确保跨镜一致性
    cd_lines = []
    for c in characters:
        name = c.get('name', '未知')
        desc = c.get('description', '')[:200]
        personality = c.get('personality', '')
        line = f"  ★ {name}: {desc}"
        if personality:
            line += f" (性格: {personality})"
        cd_lines.append(line)
    cd = '\n'.join(cd_lines) if cd_lines else '  （无预设角色）'

    # 场景参考 — 豆包/Seedream/Seedance 主链路优先中文；英文 scene_spec 只作兼容字段
    def _valid_scene_text_v52(s):
        return bool(re.sub(r'[，,。.;；:\s]+', '', str(s or '')))

    scene_ref = scene_spec_zh if _valid_scene_text_v52(scene_spec_zh) else (scene_spec if _valid_scene_text_v52(scene_spec) else (global_scene or ''))

    # 可选题材 Profile：当前用于“勇者 vs 巨龙”测试，不影响其他题材
    prompt_profile = kw.get('prompt_profile') or {}
    if prompt_profile and prompt_profile.get('target_shots'):
        try:
            profile_target = int(prompt_profile.get('target_shots'))
            mn = mx = profile_target
            ts = str(profile_target)
        except Exception:
            pass

    profile_text = ''
    if prompt_profile:
        profile_text = f'''\n【当前测试题材 Profile】\nProfile名称：{prompt_profile.get('name','')}\n建议镜头数：{prompt_profile.get('target_shots','')}\n主场景锚点：{prompt_profile.get('scene_anchor','')}\n镜头职责：{' → '.join(prompt_profile.get('shot_roles', []))}\n体型规则：{prompt_profile.get('scale_rule','')}\n动作规则：{prompt_profile.get('action_rule','')}\n构图规则：{prompt_profile.get('composition_rule','')}\n禁止项：{'；'.join(prompt_profile.get('forbidden', []))}\n注意：Profile只服务当前测试题材，不改变通用JSON字段结构；中文大模型主链路使用中文提示词。\n'''

    # ★ v12: 按画风切换示例，避免所有题材都给校园日漫示例
    _style_examples = _get_style_examples(style)
    _ex_single = _style_examples["single"]
    _ex_two    = _style_examples["two"]

    system = f"""你是动漫分镜导演，把剧情拆成分镜。只输出JSON数组，不要任何其他内容。

【画风】{style}

【输出规则】
1. 同一角色在所有分镜中的外貌描述必须一致（发色/服装颜色款式完全不变）
2. jimeng_ref_prompt 用纯中文，90-140 字精炼描述（画风+场景+角色+动作+构图），不要重复
3. ★★★【人物关系铁律】多角色互动时必须先判断关系类型，不要一律写成左右对峙：
   - confront 对抗/对峙："A在画面左侧侧身朝右，B在画面右侧侧身朝左，两者视线交汇对视"
   - same_side 并肩/同向："A、B并肩而立，同时朝向画面右方/朝向某共同方向"
   - chase 追击："被追者在前方朝右奔跑，追者在后方朝右追赶"
   - mounted 承载/骑乘/站在巨物上："画面中只有一个承载主体，A站在/骑在B背部或身上，二者不是左右对峙关系"
   ❌ 禁止把"站在龙背上/骑在龙身上/站在机甲手掌中"写成面对面对峙
   ❌ 禁止写"两人站在战场"这种不明朝向的模糊描述
4. ★★★ 战斗题材先考虑“镜头职责”，不是先概括剧情：
   - 镜1=压迫建立镜：必须体现体型差、空间关系、压迫感
   - 镜2=起势镜：必须有拔剑/蓄力/闪避准备/重心变化等明显动作
   - 镜3=第一次正面交锋：必须有碰撞反馈，如火花、气流、碎石、震退
   - 镜4=压制或失衡后回稳：保证不是一镜打赢
   - 镜5=高光反击或龙背高光：必须是明确高潮镜
   - 镜6=结果/收束镜：必须表现胜负已分、烟尘落下、收剑回稳，禁止继续停在对峙摆拍
5. 每个分镜必须能支撑至少4秒视频：要有一个稳定首帧 + 一个主要动作阶段 + 一个小幅动态变化或结果变化；不要把一个很薄的动作拆成多个镜头
6. 长篇必须生成5-6个有效镜头；如果剧情只有4段，需要从复合动作段落中拆出一个稳定过渡或高光镜头，不能直接输出4镜
7. 如果剧情已经按段落呈现，优先一段对应一镜；但当篇幅要求为长篇且段落少于5段时，允许把“逼近/第一次交锋/压制或反制/龙背高光/结尾”拆成独立镜头
8. 绝对禁止同一镜同时写“站在龙背/骑在龙背”和“巨龙倒下/坠落/尸体/倒地”。龙背高光镜是承载关系，结尾倒地镜必须单独成镜
9. Seedream 首帧提示词负责画面设计：风格、场景、主体、空间关系、构图、禁止项要清楚
10. Seedance 视频提示词负责运动导演：必须写“前30% / 中40% / 后30%”节奏，保持首帧关系不变
11. 相邻分镜的场景保持连续，除非 scene_change=true
12. 战斗题材默认不要一击秒杀：至少出现一次正面交锋和一次反制或回稳过程；首尾帧方案下，每张分镜图只写一个单动作姿态，禁止在 action_zh 使用“已/之后/随后/然后/再/顺势/同时”等连续过程词；多角色时第一镜写明体型比例（如"巨龙体型是少年8倍"），后续镜保持一致
13.1 如果是【单名勇者/人类 vs 巨大巨龙/巨兽】的战斗，优先采用镜头职责链：①压迫建立远景 → ②勇者握剑/眼神起势特写 → ③龙爪拍落与闪避 → ④横剑格挡 → ⑤圣剑反击命中龙爪 → ⑥巨龙后退与收束；若通用长度要求不是6镜，可压缩但不要删除起势特写和收束镜
13.2 上述巨大生物战中，至少有1镜必须明确表现体型差：勇者较小，巨龙巨大，占据天空或画面上半部；不要每一镜都做成平视并排对打
13.3 如果剧情已进入收束、胜负已分或出现"战斗结束"语义，最后一镜必须是结果镜/收束镜，不要让最后一镜仍停留在巨龙站立对峙或纯摆拍状态
14. 景别要有变化，不允许连续3镜用同一景别
15. ★ scene_description 必须只写【纯环境】(空间/光线/色调/天气/氛围)，严禁出现人物、角色名、动作、对话、情绪、"二者""双方""勾勒XX轮廓"等隐式指人表达
16. ★ 服装颜色和款式锁定: 每个角色的衣着必须在每一镜的 jimeng_ref_prompt 里都明确写出(如"黑色学生校服""红色连衣裙"), 不得省略或变更
17. ★ 背景必须有明确环境描述, 禁止写"白色背景""纯色背景""简约背景""空白背景"等

★ 每个字段字数严格控制，否则输出被截断："""

    prompt = f"""将以下剧情转换成 {ts} 个分镜。

【剧情】
{story_text}

【角色档案（所有分镜必须严格使用下列外貌描述）】
{cd}

【场景参考】{scene_ref or '根据剧情设计'}
{profile_text}
【字段说明】
- shot_type: 特写/近景/中景/远景/全景/环境（只能选一个）
- camera_angle: 平视/俯视/仰视/侧面
- scene_description: ★纯环境描述(空间布局+光影+色调+氛围)，≤40中文字，严禁出现人物/角色名/生物/动作/对话，例如不能写“远处巨龙盘踞”
- action_zh: 高质量中文动作句，35-70字，必须写清【前置姿态/重心变化 + 主动作 + 环境反馈或结果】，禁止只写“举剑/开始战斗/准备迎战”这类短标签；战斗镜要写火花、尘土、裂地、被震退、回稳、翼风压迫等可视变化
- action: 英文动作，≤15词
- pose_hint: standing/sitting/walking/running/kneeling/flying 等英文单词
- dialogue: 角色台词，可空
- characters_in_shot: 数组，只填上面角色档案里有的名字
- emotion: 英文单词 (calm/happy/sad/surprised/determined/tearful/anxious)
- duration_hint: 整数秒，范围4-10秒；内容不足不要拆镜，宁可减少镜数
- scene_change: true/false
- relationship_type: neutral/confront/same_side/chase/mounted，单人或环境镜用neutral
- jimeng_ref_prompt: ★纯中文 90-140 字★ Seedream首帧提示词，包含【画风{style}】+【场景】+【主体】+【空间关系】+【构图】+【禁止项】；战斗镜必须明确体型差、前后景关系或受力反馈，例如：
  {_ex_single[:150]}...
- video_prompt: ★纯中文 120-220 字★ Seedance视频提示词，必须包含“保持首帧关系不变”以及“前30%/中40%/后30%”三个阶段；前30%只写起势/逼近/镜头建立，中40%只写唯一主要交锋或高潮动作，后30%只写回稳/反制结果/烟尘收束；必须包含镜头运动方式和物理反馈，只描述运动和镜头，不重新设计角色

【返回格式 — 严格按照这个结构，字数不要超】
[
  {{
    "shot_type": "中景",
    "camera_angle": "平视",
    "scene_description": "场景环境光影（≤40字）",
    "action_zh": "核心动作（≤35字，能支撑4秒画面）",
    "action": "english action (≤15 words)",
    "pose_hint": "standing",
    "dialogue": "",
    "characters_in_shot": ["角色名"],
    "emotion": "calm",
    "duration_hint": 6,
    "scene_change": false,
    "relationship_type": "neutral",
    "jimeng_ref_prompt": "★纯中文90-140字Seedream首帧描述，包含画风、场景、主体、空间关系、构图、禁止项★",
    "video_prompt": "保持首帧中的角色身份、服装、站位、背景和空间关系不变。前30%：环境和角色小幅运动；中40%：主要动作缓慢展开；后30%：镜头自然收束。动作幅度适中，画面稳定，禁止新增角色和文字。"
  }}
]

【最终检查】
- 每镜 scene_description 必须是纯环境，不能包含勇者/巨龙/人物/生物/动作
- 每镜 action_zh 必须是完整动作句，不能少于12个中文字，不能只写状态标签
- 每镜 jimeng_ref_prompt 字数 90-160 字（不要超）
- 每镜 video_prompt 必须含“前30%/中40%/后30%”
- 长篇必须返回5-6镜，不得只返回4镜
- 若为战斗题材，默认不要“一击结束”，至少有一次交锋过程和一次反制或回稳过程
- 每镜都要有足够4秒视频生成的内容，不要硬拆空镜
- 同一镜禁止同时出现“龙背高光”和“巨龙倒地”两个状态
- 同一角色在各镜外貌描述完全一致
- characters_in_shot 只锁定命名角色；背景人群/车辆/士兵/怪物群可作为环境元素出现，但不得复制命名角色或抢占主体
- 相邻镜景别有变化
- JSON 格式正确，所有字符串必须用双引号闭合"""

    result = _call_llm(system, prompt, temperature=0.42, max_tokens=11000, task_name="storyboard-script")
    if not result['success']:
        if _v47_is_hero_dragon_context(story_text, characters):
            shots = _v47_force_hero_dragon_advantage_storyboard([], characters, scene_spec_zh or scene_spec or global_scene, style=style, story_text=story_text)
            total_dur = sum(x.get('duration_hint', 7) for x in shots)
            print('[v47分镜兜底] LLM失败，启用本地6镜战斗分镜')
            return {'success': True, 'shots': shots, 'events': [], 'scene_spec': scene_spec, 'total_duration': total_dur, 'warning': result.get('message','')}
        return result
    try:
        shots = _parse_json(result['content'])
        if not isinstance(shots, list) or len(shots) == 0:
            raise ValueError('返回不是有效数组')
        shots = _post_process(shots, characters, scene_spec_zh or scene_spec, style=style, story_text=story_text)
        if length_mode in length_ranges or numbered_count_v14 >= 5:
            shots = _ensure_min_shots(shots, characters, mn, mx, scene_spec_zh or scene_spec, style)
        shots = _repair_battle_arc_v25(
            shots, characters, scene_spec_zh or scene_spec or global_scene,
            style=style, story_text=story_text, length_mode=length_mode
        )
        shots = enforce_storyboard_count_and_scene_v14(
            shots, story_text, characters, scene_spec_zh or scene_spec or global_scene,
            style=style, length_mode=length_mode
        )
        shots = _upgrade_storyboard_quality_v26(
            shots, characters, scene_spec_zh or scene_spec or global_scene,
            style=style, story_text=story_text
        )
        shots = _v47_force_hero_dragon_advantage_storyboard(
            shots, characters, scene_spec_zh or scene_spec or global_scene,
            style=style, story_text=story_text
        )
        if _v47_is_hero_dragon_context(story_text, characters):
            mx = max(mx, 6)
        if len(shots) > mx:
            shots = shots[:mx]
        # 统计日志
        total_dur = sum(s.get('duration_hint', 7) for s in shots)
        print(f"\n[分镜] ═══ 共 {len(shots)} 镜, 预估总时长 {total_dur}s ═══")
        for i, s in enumerate(shots):
            print(f"  镜{i + 1} [{s.get('shot_type', '')}] "
                  f"{s.get('duration_hint', 7)}s - {s.get('action_zh', '')}")
        return {
            'success': True,
            'shots': shots,
            'events': [],
            'scene_spec': scene_spec,
            'total_duration': total_dur,
        }
    except Exception as e:
        print(f"[分镜] 首次解析失败: {e}")
        print(f"[分镜] 原始响应前500字: {result['content'][:500]}")

        # ★ 降级重试：用极简 prompt 再试一次
        print("[分镜] 🔄 降级重试 (极简模式)...")
        retry_system = "你是分镜脚本作者。只输出纯JSON数组。每个字段严格控制字数，不要超。"
        retry_prompt = f"""把以下剧情拆成 {ts} 个分镜，以 JSON 数组返回。若要求为5-6镜，绝对不能只返回4镜：

剧情：{story_text[:500]}

角色：{', '.join(c.get('name','') for c in characters)}

画风：{style}

每个分镜包含字段（字数严格）：
- shot_type: 特写/近景/中景/远景/全景
- scene_description: ≤30字场景
- action_zh: ≤30字动作，必须能支撑4秒画面
- pose_hint: standing/sitting/walking/running/kneeling
- characters_in_shot: 角色名数组
- emotion: calm/happy/sad/surprised/determined
- duration_hint: 整数秒，范围4-10秒
- relationship_type: neutral/confront/same_side/chase/mounted
- dialogue: 可空
- jimeng_ref_prompt: ★纯中文70-110字★ 画风+场景+角色特征+动作+人物关系+构图

返回格式：
[{{"shot_type":"中景","scene_description":"...","action_zh":"...","pose_hint":"standing","characters_in_shot":["..."],"emotion":"calm","duration_hint":5,"relationship_type":"neutral","dialogue":"","jimeng_ref_prompt":"..."}}]

严格要求：
1. 必须是合法 JSON，双引号闭合
2. jimeng_ref_prompt 不超 90 字
3. 长篇/5-6镜要求下不得只输出4镜
4. 战斗题材不要一击结束，至少写出一次交锋和一次反制/回稳
4. 同一镜禁止同时写龙背承载和巨龙倒地
5. 只输出 JSON，不要任何说明文字"""

        retry_result = _call_llm(retry_system, retry_prompt, temperature=0.24, max_tokens=8000, task_name="storyboard-retry")
        if not retry_result['success']:
            if _v47_is_hero_dragon_context(story_text, characters):
                shots = _v47_force_hero_dragon_advantage_storyboard([], characters, scene_spec_zh or scene_spec or global_scene, style=style, story_text=story_text)
                total_dur = sum(x.get('duration_hint', 7) for x in shots)
                print('[v47分镜兜底] 重试也失败，启用本地6镜战斗分镜')
                return {'success': True, 'shots': shots, 'events': [], 'scene_spec': scene_spec, 'total_duration': total_dur, 'warning': retry_result.get('message','')}
            return {'success': False, 'message': f'分镜解析失败: {e}; 重试也失败'}
        try:
            shots = _parse_json(retry_result['content'])
            if not isinstance(shots, list) or len(shots) == 0:
                raise ValueError('重试返回不是有效数组')
            shots = _post_process(shots, characters, scene_spec_zh or scene_spec, style=style, story_text=story_text)
            if length_mode in length_ranges or numbered_count_v14 >= 5:
                shots = _ensure_min_shots(shots, characters, mn, mx, scene_spec_zh or scene_spec, style)
            shots = enforce_storyboard_count_and_scene_v14(
                shots, story_text, characters, scene_spec_zh or scene_spec or global_scene,
                style=style, length_mode=length_mode
            )
            shots = _v47_force_hero_dragon_advantage_storyboard(
                shots, characters, scene_spec_zh or scene_spec or global_scene,
                style=style, story_text=story_text
            )
            if _v47_is_hero_dragon_context(story_text, characters):
                mx = max(mx, 6)
            if len(shots) > mx:
                shots = shots[:mx]
            total_dur = sum(s.get('duration_hint', 7) for s in shots)
            print(f"[分镜] ✓ 降级重试成功, 共 {len(shots)} 镜")
            return {
                'success': True,
                'shots': shots,
                'events': [],
                'scene_spec': scene_spec,
                'total_duration': total_dur,
            }
        except Exception as e2:
            print(f"[分镜] 重试也失败: {e2}")
            print(f"[分镜] 重试响应前500字: {retry_result['content'][:500]}")
            if _v47_is_hero_dragon_context(story_text, characters):
                shots = _v47_force_hero_dragon_advantage_storyboard([], characters, scene_spec_zh or scene_spec or global_scene, style=style, story_text=story_text)
                total_dur = sum(x.get('duration_hint', 7) for x in shots)
                print('[v47分镜兜底] 解析失败后启用本地6镜战斗分镜')
                return {'success': True, 'shots': shots, 'events': [], 'scene_spec': scene_spec, 'total_duration': total_dur, 'warning': str(e2)}
            return {'success': False, 'message': f'分镜解析失败（已重试）: {e2}'}



def _is_dragon_name(name: str) -> bool:
    return any(k in (name or '') for k in ['龙', '巨龙', '古龙', '远古巨龙'])


def _find_role_names(characters):
    human = ''
    creature = ''
    for c in characters or []:
        n = c.get('name', '')
        d = (c.get('description', '') or '').lower()
        if not creature and (_is_dragon_name(n) or 'dragon' in d or 'beast' in d or 'monster' in d):
            creature = n
        elif not human:
            human = n
    return human, creature


def _make_insert_shot(kind, characters, scene_spec='', style=''):
    human, creature = _find_role_names(characters)
    human = human or '主角'
    creature = creature or '对手'
    base_scene = (scene_spec or '同一主场景，风沙与光影延续')[:40]
    style_txt = f'{style}风格' if style else '动态漫剧风格'

    if kind == 'pressure':
        return {
            'shot_type': '远景', 'camera_angle': '仰视',
            'scene_description': base_scene,
            'action_zh': f'{human}背对镜头仰望空中的{creature}，压迫感扑面而来',
            'action': 'hero faces giant dragon from behind',
            'pose_hint': 'standing', 'dialogue': '',
            'characters_in_shot': [x for x in [human, creature] if x],
            'emotion': 'anxious', 'duration_hint': 6,
            'scene_change': False, 'relationship_type': 'confront',
            'jimeng_ref_prompt': f'{style_txt}，{base_scene}，镜头在{human}身后或肩后，前景下方是体型较小的{human}背影，远处或天空中只有1个巨大的{creature}张翼盘踞，占据画面上半部，突出强烈体型差和压迫感，禁止第二条{creature}，禁止平视并排站桩。',
            'video_prompt': f'保持首帧角色数量和空间关系不变。前30%：风沙掠过地面，{human}披风轻微摆动；中40%：天空中的{creature}振翼盘旋并缓慢逼近；后30%：{human}握紧武器，保持仰望迎战姿态。禁止复制命名角色，禁止切成平视对打。'
        }

    if kind == 'clash':
        return {
            'shot_type': '中景', 'camera_angle': '平视',
            'scene_description': base_scene,
            'action_zh': f'{human}正面迎击，被{creature}震退半步',
            'action': 'hero clashes and gets pushed back',
            'pose_hint': 'fighting', 'dialogue': '',
            'characters_in_shot': [x for x in [human, creature] if x],
            'emotion': 'determined', 'duration_hint': 6,
            'scene_change': False, 'relationship_type': 'confront',
            'jimeng_ref_prompt': f'{style_txt}，{base_scene}，命名角色只有1个{human}和1个{creature}，{human}挥动武器正面迎击，{creature}探爪或俯冲压迫，碰撞瞬间火花与气流爆开，禁止复制命名角色。',
            'video_prompt': f'保持首帧关系不变。前30%：{human}前冲举剑迎上；中40%：与{creature}正面碰撞，火花和尘土爆开；后30%：{human}被震退半步后重新稳住。禁止复制命名角色。'
        }

    if kind == 'counter':
        return {
            'shot_type': '中景', 'camera_angle': '侧面',
            'scene_description': base_scene,
            'action_zh': f'{human}侧身闪避后反冲逼近{creature}',
            'action': 'hero dodges then counters',
            'pose_hint': 'running', 'dialogue': '',
            'characters_in_shot': [x for x in [human, creature] if x],
            'emotion': 'determined', 'duration_hint': 6,
            'scene_change': False, 'relationship_type': 'chase',
            'jimeng_ref_prompt': f'{style_txt}，{base_scene}，命名角色只有1个{human}和1个{creature}，{human}侧身避开攻击后沿同一方向反冲逼近{creature}，动作连贯有速度感，禁止面对镜头站桩，禁止复制命名角色。',
            'video_prompt': f'保持首帧关系不变。前30%：{creature}攻击擦身而过；中40%：{human}侧身闪避并迅速回稳；后30%：{human}顺势反冲逼近{creature}。禁止复制命名角色。'
        }

    if kind == 'powerup':
        return {
            'shot_type': '中景', 'camera_angle': '平视',
            'scene_description': base_scene,
            'action_zh': f'{human}举起武器，光芒逐渐增强',
            'action': 'hero raises glowing weapon',
            'pose_hint': 'raising weapon', 'dialogue': '',
            'characters_in_shot': [human],
            'emotion': 'determined', 'duration_hint': 6,
            'scene_change': False, 'relationship_type': 'neutral',
            'jimeng_ref_prompt': f'{style_txt}，{base_scene}，命名角色只有1个{human}，{human}侧身举起发光武器，衣物随风摆动，完整背景，禁止出现第二个{human}，禁止画中画和重复特写。',
            'video_prompt': f'保持首帧身份和服装不变。前30%：镜头缓慢推近；中40%：武器光芒逐渐增强；后30%：风吹动衣物。禁止复制命名角色。'
        }

    return {
        'shot_type': '全景', 'camera_angle': '俯视',
        'scene_description': base_scene,
        'action_zh': f'{human}站在{creature}背上举剑蓄势',
        'action': 'hero stands on dragon back',
        'pose_hint': 'standing on back', 'dialogue': '',
        'characters_in_shot': [x for x in [human, creature] if x],
        'emotion': 'determined', 'duration_hint': 6,
        'scene_change': False, 'relationship_type': 'mounted',
        'jimeng_ref_prompt': f'{style_txt}，{base_scene}，命名角色只有1个{human}和1个{creature}，{creature}作为唯一巨大承载主体横贯画面下方，{human}站在其背部举剑蓄势，禁止第二个命名{creature}，禁止倒地尸体和背景龙影。',
        'video_prompt': f'保持承载关系不变，画面中只有一个命名{creature}。前30%：风沙流动与龙躯起伏；中40%：{human}稳住脚步并缓慢举剑蓄势；后30%：光芒照亮环境。禁止变成对峙，禁止复制命名角色。'
    }




def _scene_from_english_hint_v26(text: str) -> str:
    """把常见英文场景锚点转成中文，避免前端展示英文场景词。"""
    raw = str(text or '').strip()
    low = raw.lower()
    cn_count = len(re.findall(r'[\u4e00-\u9fff]', raw))
    en_count = len(re.findall(r'[a-zA-Z]', raw))
    if en_count <= cn_count:
        return raw
    parts = []
    if any(k in low for k in ['charred', 'scorched', 'burnt', 'burned']):
        parts.append('焦黑平原')
    elif any(k in low for k in ['plain', 'wasteland', 'wilderness', 'field']):
        parts.append('荒芜平原')
    if any(k in low for k in ['cracked', 'cracks', 'crisscrossed', 'fissure']):
        parts.append('焦土遍布纵横裂痕')
    if any(k in low for k in ['smoke', 'smoky', 'haze', 'billowing']):
        parts.append('低空烟尘翻涌')
    if any(k in low for k in ['afterglow', 'sunset', 'dusk', 'orange', 'red sky']):
        parts.append('远天残阳余晖压低')
    if any(k in low for k in ['cloud', 'clouds']):
        parts.append('厚重云层堆叠')
    if not parts:
        return raw
    return '，'.join(parts[:4])


def _purify_scene_anchor_v26(scene_text: str, characters=None) -> str:
    """把场景锚点净化成纯环境，禁止混入角色/生物/动作。"""
    text = _scene_from_english_hint_v26(str(scene_text or '').strip())
    text = re.sub(r'\s+', ' ', text).strip(' ，,。；;：:')
    if not text:
        return ''

    char_names = [c.get('name', '') for c in (characters or []) if c.get('name')]
    role_words = [
        '勇者','少年','青年','少女','女孩','男孩','主角','骑士','剑士','人影','人物','角色',
        '巨龙','远古巨龙','古龙','恶龙','魔龙','龙','怪物','巨兽','魔物','敌人','对手'
    ] + char_names
    action_words = [
        '站','坐','跪','蹲','跑','冲','扑','飞','盘踞','盘旋','张翼','展翅','咆哮','低吼','逼近','对峙','迎战','战斗','挥剑','举剑','蓄光','攻击','倒地','倒下','收剑'
    ]
    clauses = [c.strip(' ，,。；;：:') for c in re.split(r'[，,。；;。；]+', text) if c.strip(' ，,。；;：:')]
    kept = []
    for c in clauses:
        has_role = any(w and w in c for w in role_words)
        has_action = any(w in c for w in action_words)
        # 保留“龙卷风/山脉”等非角色词的空间，但普通“龙/巨龙”必须删
        if has_role or has_action:
            continue
        kept.append(c)
    if not kept:
        # 常见奇幻战场兜底，绝不带角色。
        low = text.lower()
        if any(k in text for k in ['焦', '裂', '烟', '平原']) or any(k in low for k in ['charred', 'scorched', 'plain', 'crack']):
            kept = ['焦黑平原', '焦土遍布纵横裂痕', '低空烟尘翻涌']
        else:
            kept = ['同一主场景', '光线和天气保持连续', '环境细节清晰']
    # 去重并控制长度
    out = []
    for item in kept:
        item = item.strip(' ，,。；;：:')
        if item and item not in out:
            out.append(item)
    return '，'.join(out[:4])[:60]


def _is_creature_name_v26(name: str) -> bool:
    return any(k in str(name or '') for k in ['龙', '巨龙', '古龙', '恶龙', '魔龙', '兽', '怪'])


def _enhance_battle_action_v26(shot: dict, characters=None, index: int = 0, total: int = 0) -> dict:
    """把战斗动作从短标签升级为可出图/可出视频的动作句。"""
    if not isinstance(shot, dict):
        return shot
    human, creature = _find_role_names(characters or [])
    human = human or '勇者'
    creature = creature or '巨龙'
    ci = shot.get('characters_in_shot') or []
    if isinstance(ci, str): ci = [ci]
    txt = ' '.join(str(shot.get(k, '') or '') for k in ['action_zh','jimeng_ref_prompt','video_prompt'])
    rel = str(shot.get('relationship_type') or '').lower()
    old_action = str(shot.get('action_zh') or '').strip()

    # 非勇者打龙题材不强行改，只修太短动作。
    has_dragon_context = (creature and (creature in txt or any(_is_creature_name_v26(x) for x in ci))) or any(k in txt for k in ['巨龙','龙爪','龙翼','龙背'])
    has_human_context = (human and (human in txt or human in ci)) or any(k in txt for k in ['勇者','少年','圣剑','剑光'])
    if not (has_dragon_context or has_human_context):
        if len(old_action) < 6:
            shot['action_zh'] = old_action or '角色完成当前主要动作，姿态稳定清晰'
        return shot

    action = old_action
    is_last = total and index == total - 1
    if is_last or any(k in txt for k in ['战斗结束', '收束', '倒地', '倒下', '失去攻势', '尘埃落定']):
        action = f'{creature}倒在焦土上失去攻势，{human}收剑站稳，烟尘缓慢落下'
        shot['relationship_type'] = 'neutral'
        shot['shot_type'] = shot.get('shot_type') or '中景'
    elif any(k in txt for k in ['背后', '背影', '肩后', '仰望', '天空中']) or (index == 0 and rel == 'confront'):
        action = f'{human}背对镜头仰望天空中的{creature}，披风被热风掀起，手指握紧剑柄'
        shot['camera_angle'] = '仰视'
        shot['shot_type'] = '远景'
        shot['relationship_type'] = 'confront'
    elif any(k in txt for k in ['蓄光', '光芒', '发光', '举起', '举剑']) and (len(ci) <= 1 or rel == 'neutral'):
        action = f'{human}双手举起圣剑蓄光，脚下焦土裂缝被剑芒照亮'
        shot['relationship_type'] = 'neutral'
    elif rel == 'mounted' or any(k in txt for k in ['龙背', '背上', '站在龙身']):
        action = f'{human}稳住脚步站在{creature}背脊上，圣剑光芒沿鳞片向前铺开'
        shot['relationship_type'] = 'mounted'
    elif rel == 'chase' or any(k in txt for k in ['闪避', '反冲', '侧身', '追']):
        action = f'{human}侧身避开龙爪后反冲，剑光沿裂地划出明亮弧线'
        shot['relationship_type'] = 'chase'
    elif rel == 'confront' or any(k in txt for k in ['迎击', '交锋', '挥剑', '龙爪', '压迫', '逼近']):
        action = f'{human}压低重心突进挥剑，{creature}探爪下压，火星在两者间爆开'
        shot['relationship_type'] = 'confront'
    elif len(action) < 10:
        action = f'{human}调整站姿握紧圣剑，目光锁定前方威胁，准备进入下一次交锋'

    shot['action_zh'] = action[:72]
    shot['action_detail_zh'] = action
    # 同步强化图像提示词，避免 UI 里动作改了但生图仍用旧短词。
    jp = str(shot.get('jimeng_ref_prompt') or '')
    if action and action not in jp:
        shot['jimeng_ref_prompt'] = (jp + f'，本镜核心动作：{action}')[:680] if jp else f'本镜核心动作：{action}'
    vp = str(shot.get('video_prompt') or '')
    if action and '前30%' in vp and action[:12] not in vp:
        shot['video_prompt'] = (vp + f'，全程围绕核心动作：{action}').strip('，,')[:760]
    elif action and not vp:
        shot['video_prompt'] = f'保持首帧关系不变。前30%：镜头稳定建立动作姿态；中40%：{action}；后30%：烟尘和衣摆自然收束，禁止新增角色和文字。'
    return shot


def _upgrade_storyboard_quality_v26(shots, characters=None, scene_anchor='', style='', story_text=''):
    """最终质量修复：纯环境场景 + 高质量动作。"""
    if not isinstance(shots, list):
        return shots
    anchor = _purify_scene_anchor_v26(scene_anchor or '', characters)
    for i, shot in enumerate(shots):
        current_scene = shot.get('scene_description') or anchor
        clean_scene = _purify_scene_anchor_v26(current_scene or anchor, characters)
        shot['scene_description'] = clean_scene or anchor or '同一主场景，环境细节清晰'
        if anchor:
            shot['scene_anchor'] = anchor
        shot = _enhance_battle_action_v26(shot, characters, i, len(shots))
        shots[i] = shot
    return shots

def _looks_like_human_vs_dragon_battle_v25(story_text: str, characters) -> bool:
    text = str(story_text or '')
    human, creature = _find_role_names(characters)
    if not (human and creature):
        return False
    battle_keys = ['战斗', '交锋', '迎战', '对峙', '咆哮', '挥剑', '圣剑', '勇者', '巨龙', '恶龙']
    return any(k in text for k in battle_keys)


def _make_scale_establishing_shot_v25(characters, scene_spec='', style=''):
    human, creature = _find_role_names(characters)
    human = human or '主角'
    creature = creature or '巨龙'
    base_scene = (scene_spec or '同一主场景，风沙、烟尘与天光延续')[:40]
    style_txt = f'{style}风格' if style else '动态漫剧风格'
    return {
        'shot_type': '远景',
        'camera_angle': '仰视',
        'scene_description': base_scene,
        'action_zh': f'{human}背对镜头仰望天空中的{creature}，披风被热风掀起，手指握紧剑柄',
        'action': 'hero looks up at giant dragon',
        'pose_hint': 'standing',
        'dialogue': '',
        'characters_in_shot': [x for x in [human, creature] if x],
        'emotion': 'anxious',
        'duration_hint': 6,
        'scene_change': False,
        'relationship_type': 'confront',
        'jimeng_ref_prompt': f'{style_txt}，{base_scene}，镜头位于{human}身后或肩后，前景下方是体型较小的{human}背影，远处或天空中只有1个巨大的{creature}张翼盘踞，占据画面上半部，突出强烈体型差和压迫感，禁止第二条{creature}，禁止平视并排站桩。',
        'video_prompt': f'保持首帧关系不变。前30%：风沙掠过地面，{human}披风和衣摆轻微摆动；中40%：天空中的{creature}振翼盘旋或缓慢逼近，维持巨大体型压迫；后30%：{human}仰头握紧武器准备迎战。禁止新增第二条{creature}，禁止镜头切成平视对打。'
    }


def _make_aftermath_shot_v25(characters, scene_spec='', style=''):
    human, creature = _find_role_names(characters)
    human = human or '主角'
    creature = creature or '巨龙'
    base_scene = (scene_spec or '同一主场景，烟尘渐散，战场光线收束')[:40]
    style_txt = f'{style}风格' if style else '动态漫剧风格'
    return {
        'shot_type': '中景',
        'camera_angle': '平视',
        'scene_description': base_scene,
        'action_zh': f'{creature}倒在焦土上失去攻势，{human}收剑站稳，烟尘缓慢落下',
        'action': 'dragon falls and battle ends',
        'pose_hint': 'standing',
        'dialogue': '',
        'characters_in_shot': [x for x in [human, creature] if x],
        'emotion': 'determined',
        'duration_hint': 6,
        'scene_change': False,
        'relationship_type': 'neutral',
        'jimeng_ref_prompt': f'{style_txt}，{base_scene}，战斗结束后的收束画面，只有1个{human}和1条{creature}。{creature}倒在地面失去攻势，{human}站在近景或中景位置收剑回稳，尘土与余烬渐渐落下，画面强调结果与收束，禁止背景再出现站立或飞行的第二条{creature}。',
        'video_prompt': f'保持首帧关系不变。前30%：尘土和火星缓慢下落；中40%：倒地的{creature}不再发起攻击，{human}缓慢收剑并调整呼吸；后30%：画面自然收束，突出战斗结束后的余韵。禁止背景再出现站立或飞行的第二条{creature}。'
    }


def _is_scale_pressure_shot_v25(shot: dict) -> bool:
    txt = ' '.join(str(shot.get(k, '') or '') for k in ['action_zh', 'jimeng_ref_prompt', 'video_prompt'])
    st = str(shot.get('shot_type', '') or '')
    cam = str(shot.get('camera_angle', '') or '')
    return (('背后' in txt or '背影' in txt or '肩后' in txt or '仰望' in txt) and ('巨龙' in txt or '龙' in txt) and (cam == '仰视' or any(k in st for k in ['远景', '全景'])))


def _is_ending_shot_v25(shot: dict) -> bool:
    txt = ' '.join(str(shot.get(k, '') or '') for k in ['action_zh', 'jimeng_ref_prompt', 'video_prompt'])
    ending_keys = ['战斗结束', '结束', '倒地', '倒下', '坠落', '失去攻势', '收剑', '尘埃落定', '胜负已分']
    return any(k in txt for k in ending_keys)


def _repair_battle_arc_v25(shots, characters, scene_spec='', style='', story_text='', length_mode=''):
    """v25: 修复巨大生物战分镜弧线。
    - 第一镜优先改成“勇者背后看天空巨龙”的压迫建立镜。
    - 最后一镜如果不是结果镜，则改成明确的战斗结束/收束镜。
    """
    if not isinstance(shots, list) or len(shots) < 4:
        return shots
    if not _looks_like_human_vs_dragon_battle_v25(story_text, characters):
        return shots

    fixed = list(shots)
    if not _is_scale_pressure_shot_v25(fixed[0]):
        new_first = _make_scale_establishing_shot_v25(characters, scene_spec, style)
        for k in ['source_story_index', 'source_story_segment']:
            if fixed[0].get(k) is not None:
                new_first[k] = fixed[0].get(k)
        fixed[0] = new_first
        print('[分镜弧线修复] 已将首镜强化为背后视角的体型压迫建立镜')

    last = fixed[-1]
    last_rel = str(last.get('relationship_type', '') or '')
    last_txt = ' '.join(str(last.get(k, '') or '') for k in ['action_zh', 'jimeng_ref_prompt', 'video_prompt'])
    bad_final = (not _is_ending_shot_v25(last)) and (last_rel in ['confront', 'mounted', 'chase'] or any(k in last_txt for k in ['逼近', '迎战', '对峙', '站在龙背', '举剑蓄势']))
    if bad_final:
        new_last = _make_aftermath_shot_v25(characters, scene_spec, style)
        for k in ['source_story_index', 'source_story_segment']:
            if last.get(k) is not None:
                new_last[k] = last.get(k)
        fixed[-1] = new_last
        print('[分镜弧线修复] 已将最后一镜修正为结果/收束镜，避免以站立对峙结束')
    return fixed


def _ensure_min_shots(shots, characters, min_count, max_count, scene_spec='', style=''):
    """长篇兜底：LLM 只给4镜时自动补足。通用版只补命名角色参与的有效镜头，不写死少年打龙。"""
    if not isinstance(shots, list):
        return shots
    if len(shots) >= min_count:
        return shots[:max_count]

    need = min_count - len(shots)
    inserts = []
    has_creature = any(
        _is_dragon_name(c.get('name', '')) or
        'dragon' in (c.get('description', '') or '').lower() or
        'monster' in (c.get('description', '') or '').lower() or
        'beast' in (c.get('description', '') or '').lower()
        for c in (characters or [])
    )
    # 有巨兽/怪物时优先补交锋和反制；普通题材则补压迫/蓄势/行动变化。
    plan = ['pressure', 'clash', 'counter', 'powerup', 'mounted'] if has_creature else ['pressure', 'powerup', 'counter']
    for k in plan:
        if len(inserts) >= need:
            break
        inserts.append(_make_insert_shot(k, characters, scene_spec, style))

    if len(shots) >= 2:
        shots = shots[:-1] + inserts + shots[-1:]
    else:
        shots = shots + inserts
    print(f"[分镜后处理] 镜头数不足，已自动补足到 {len(shots)} 镜")
    return shots[:max_count]


def _fix_dragon_state_conflict(shot, characters):
    """修复同一镜同时出现“龙背高光”和“巨龙倒下”的冲突，减少一画面两条龙。"""
    txt = ' '.join(str(shot.get(k, '') or '') for k in ['action_zh','jimeng_ref_prompt','video_prompt'])
    mounted = any(k in txt for k in ['龙背', '骑在', '站在龙身', '背上']) or shot.get('relationship_type') == 'mounted'
    fallen = any(k in txt for k in ['倒下', '倒地', '坠落', '尸体', '战斗结束'])
    if mounted and fallen:
        human, creature = _find_role_names(characters)
        human = human or '主角'; creature = creature or '巨龙'
        shot['relationship_type'] = 'mounted'
        shot['action_zh'] = f'{human}站在{creature}背上举剑蓄势'
        repls = ['轰然倒地','倒地','倒下','坠落','尸体','战斗结束后','龙尸']
        for field in ['jimeng_ref_prompt','video_prompt']:
            s = str(shot.get(field,'') or '')
            for r in repls:
                s = s.replace(r, '保持承载姿态')
            s += f'，画面中只有1个{creature}，禁止同时出现站立/飞行{creature}和倒地{creature}，禁止第二条{creature}'
            shot[field] = s
        print('[分镜后处理] 已修复龙背/倒地状态冲突，避免同镜出现两条龙')
    return shot


def _extract_dialogue_from_segment_v21(segment: str) -> str:
    """从原始剧本段落里抽取真实台词，避免分镜阶段凭空编台词。"""
    seg = str(segment or '')
    patterns = [r'「([^」]{1,60})」', r'“([^”]{1,60})”', r'"([^"]{1,60})"', r'：\s*([^。！？!?,，]{1,40})']
    for pat in patterns:
        m = re.search(pat, seg)
        if m:
            text = re.sub(r'\s+', '', m.group(1)).strip(' ，,。！？!')
            if text:
                return text[:60]
    return ''



def _compress_segment_action_v21(segment: str, char_names=None, relation: str = '') -> str:
    """把对应剧本段落压缩成单镜核心动作，尽量保留主体 + 动作 + 结果。
    v23 修复：
    1) 不再只取一个过短短句，避免出现“绛”这类残缺动作；
    2) 允许保留 1~2 个动作子句，让动作描述更完整；
    3) 对单人蓄势镜、对峙镜、龙背镜给出更稳的动作兜底。
    """
    text = str(segment or '').strip()
    text = re.sub(r'^第\s*[一二三四五六七八九十0-9]+\s*镜[：:，,\s]*', '', text)
    text = re.sub(r'「[^」]*」|“[^”]*”|"[^"]*"', '', text)
    text = re.sub(r'\s+', '', text).strip('，,。；;：:')
    if not text:
        return ''

    action_verbs = [
        '走','跑','冲','扑','跃','跳','转','回头','抬头','低头','望','看','凝视','盯',
        '举','握','拔','挥','砍','刺','劈','格挡','闪避','迎击','交锋','对峙','逼近','追',
        '递','接','抱','拉','推','坐','站','跪','蹲','倒','起身','停住','靠近','离开',
        '亮起','发光','蔓延','飘动','落下','震退','稳住','蓄势','反冲','压迫','咆哮','探爪',
        '后退','回稳','俯冲','扑来','横扫','翻身','抬爪','振翅','盘旋'
    ]
    scene_words = ['平原','荒原','地面','裂痕','天空','云层','浓烟','火光','废墟','风沙','焦土','远处','空气','氛围']
    clauses = [c.strip('，,。；;：:') for c in re.split(r'[，,。；;！!？?]', text) if c.strip('，,。；;：:')]
    if not clauses:
        return text[:42]

    action_clauses = []
    for c in clauses:
        has_action = any(v in c for v in action_verbs)
        mostly_scene = any(w in c for w in scene_words) and not has_action
        if has_action and not mostly_scene:
            action_clauses.append(c)
    chosen_parts = action_clauses[:2]
    if not chosen_parts:
        chosen_parts = [clauses[1] if len(clauses) > 1 else clauses[0]]

    cleaned = []
    for c in chosen_parts:
        c2 = re.sub(r'^(?:在|于|从|朝|向)?[^，,。；;]{0,12}(?:中|里|上|下|前|后|旁|边)，?', '', c)
        c2 = c2.strip('，,。；;：:') or c
        if len(c2) >= 3:
            cleaned.append(c2)
    chosen = '，'.join(cleaned[:2]).strip('，,。；;：:')

    if len(chosen) < 4:
        rel = relation or _infer_relation_from_segment_v21(segment)
        has_human = any(k in text for k in ['少年','勇者','主角','骑士','少女','女孩','男孩','青年','她','他'])
        has_dragon = any(k in text for k in ['龙','巨龙','古龙','恶龙','魔龙','怪物','巨兽'])
        if rel == 'mounted':
            chosen = '站稳承载主体背部并举起武器蓄势'
        elif rel == 'confront' and has_human and has_dragon:
            chosen = '一方压低身形逼近，另一方握紧武器迎击'
        elif any(k in text for k in ['握剑','举剑','蓄势','站定','稳住']) and has_human:
            chosen = '双手握紧武器稳住身形，准备出手'
        elif has_dragon and any(k in text for k in ['逼近','咆哮','探爪','压迫']):
            chosen = '压低身形缓慢逼近，带来强烈压迫'
        else:
            chosen = clauses[0][:24]

    return chosen[:56]



def _infer_relation_from_segment_v21(segment: str) -> str:
    text = str(segment or '')
    if any(k in text for k in ['龙背', '背部', '骑在', '骑乘', '站在巨兽', '站在机甲手', '托起']):
        return 'mounted'
    if any(k in text for k in ['并肩', '同行', '一起', '共同', '同向', '背靠背']):
        return 'same_side'
    if any(k in text for k in ['追赶', '追击', '追逐', '逃向', '追上']):
        return 'chase'
    # v23：只有出现明确双向交锋/敌对语义时才判为 confront，
    # 避免“勇者举剑蓄势”这种单人镜被误判成对峙镜。
    direct_confront = ['对峙', '对抗', '迎战', '交锋', '厮杀', '被震退', '格挡', '扑向', '探爪', '压迫']
    if any(k in text for k in direct_confront):
        return 'confront'
    attack_words = ['攻击', '挥剑', '挥砍', '刺出', '斩向', '逼近', '咆哮']
    opponent_words = ['龙', '巨龙', '古龙', '怪物', '巨兽', '魔物', '敌人', '对手', '对方']
    if any(k in text for k in attack_words) and any(k in text for k in opponent_words):
        return 'confront'
    return 'neutral'



def _infer_chars_from_segment_v21(segment: str, characters):
    text = str(segment or '')
    names = [c.get('name', '') for c in (characters or []) if c.get('name')]
    found = [n for n in names if n and n in text]

    # “少年/勇者/巨龙”等通用称谓经常没写角色正式名，按描述补全。
    for c in characters or []:
        n = c.get('name', '')
        desc = (c.get('description', '') or '').lower()
        if not n or n in found:
            continue
        is_creature = any(k in n for k in ['龙','兽','怪']) or any(k in desc for k in ['dragon','beast','monster','creature'])
        if is_creature and any(k in text for k in ['龙','巨龙','巨兽','怪物','魔物','兽']):
            found.append(n)
        elif (not is_creature) and any(k in text for k in ['少年','勇者','主角','骑士','少女','女孩','男孩','青年','她','他']):
            # 只补第一个人类命名角色，避免把未出场角色拉进来。
            if not any((fn != n) and not any(x in fn for x in ['龙','兽','怪']) for fn in found):
                found.append(n)

    # v23：仅在段落明确写出“双方交锋/对手存在”时，才把另一方补进来。
    if len(found) == 1:
        text_flags = {
            'has_explicit_opponent': any(k in text for k in ['龙','巨龙','古龙','怪物','巨兽','魔物','敌人','对手','对方']),
            'has_mutual_combat': any(k in text for k in ['对峙','迎战','交锋','战斗','被震退','格挡','扑向','探爪','咆哮','压迫']),
            'is_prep_shot': any(k in text for k in ['握剑','举剑','蓄势','站定','稳住','凝视','抬头','低头','看向','独自'])
        }
        if (text_flags['has_explicit_opponent'] and text_flags['has_mutual_combat'] and not text_flags['is_prep_shot']):
            for c in characters or []:
                n = c.get('name', '')
                if n and n not in found:
                    found.append(n)
                    break
    return found


def _append_character_identity_locks_v21(shot: dict, characters):
    """给每镜注入角色身份锁：姓名、核心外貌、服装/道具、体型比例。"""
    ci = shot.get('characters_in_shot') or []
    if isinstance(ci, str):
        ci = [ci]
    char_map = {c.get('name', ''): c for c in (characters or [])}
    locks = []
    for name in ci:
        c = char_map.get(name)
        if not c:
            continue
        desc = re.sub(r'\s+', ' ', c.get('description', '') or '').strip()
        if not desc:
            continue
        # 英文描述也直接保留，Seedream/NAI 都能利用；长度控制避免 prompt 过长。
        locks.append(f"{name}身份锁：{desc[:180]}")
    if locks:
        shot['identity_locks'] = locks
        jp = str(shot.get('jimeng_ref_prompt') or '')
        lock_text = '；'.join(locks)
        if lock_text and lock_text not in jp:
            shot['jimeng_ref_prompt'] = (jp + '，' + lock_text)[:520] if jp else lock_text[:520]
        vp = str(shot.get('video_prompt') or '')
        if '保持角色身份锁' not in vp:
            shot['video_prompt'] = (vp + '，保持角色身份锁、服装配色、发型、体型和道具完全不变').strip('，,')
    return shot


def _align_shots_to_story_segments_v21(shots, story_text, characters, scene_spec='', style=''):
    """把每个分镜重新绑定到对应剧本段落，解决“分镜和剧本不对应”。"""
    if not isinstance(shots, list) or not story_text:
        return shots
    numbered = _extract_numbered_story_segments_v14(story_text)
    if numbered:
        segments = [x.get('text', '') for x in numbered]
    else:
        segments = _split_story_paragraphs(story_text)
    segments = [re.sub(r'\s+', ' ', str(x)).strip() for x in segments if str(x).strip()]
    if len(segments) < 2:
        return shots

    for i, shot in enumerate(shots):
        if i >= len(segments):
            break
        seg = segments[i]
        shot['source_story_index'] = i + 1
        shot['source_story_segment'] = seg[:180]

        action = _compress_segment_action_v21(seg, [c.get('name', '') for c in characters], relation=_infer_relation_from_segment_v21(seg))
        if action:
            old_action = str(shot.get('action_zh') or '')
            # 保留旧值但以源段落动作作为主动作，避免 LLM 自由发挥错位。
            if old_action and old_action != action:
                shot['llm_action_zh'] = old_action[:60]
            shot['action_zh'] = action
            if not shot.get('action'):
                shot['action'] = ''

        chars = _infer_chars_from_segment_v21(seg, characters)
        if chars:
            shot['characters_in_shot'] = chars

        dia = _extract_dialogue_from_segment_v21(seg)
        if dia:
            shot['dialogue'] = dia
        else:
            # 没有真实台词时，清掉 LLM 可能虚构的短台词。
            old_dia = str(shot.get('dialogue') or '').strip()
            if old_dia and old_dia not in story_text:
                shot['dialogue'] = ''

        rel = _infer_relation_from_segment_v21(seg)
        if rel != 'neutral':
            shot['relationship_type'] = rel

        # 将源段落动作写回提示词，防止图片生成时偏离剧本。
        jp = str(shot.get('jimeng_ref_prompt') or '')
        if action and action not in jp:
            shot['jimeng_ref_prompt'] = (jp + f'，本镜必须对应源剧本动作：{action}')[:520] if jp else f'{style}风格精致插画，本镜必须对应源剧本动作：{action}'
        vp = str(shot.get('video_prompt') or '')
        if action and action not in vp:
            shot['video_prompt'] = (vp + f'，保持首帧关系不变，本镜只表现源剧本动作：{action}').strip('，,')

        shot = _append_character_identity_locks_v21(shot, characters)
        shots[i] = shot
    return shots

def _post_process(shots, characters, scene_spec='', style='', story_text=''):
    """后处理：清洗字段、校验角色名、兜底缺失字段"""
    char_names = {c['name'] for c in characters}
    char_desc_map = {c['name']: c.get('description', '') for c in characters}

    for i, shot in enumerate(shots):
        # ── 英文 action 清洗 ──
        a = shot.get('action', '')
        a = re.sub(r'\([^)]*\)', '', a)
        a = re.sub(r'[\u4e00-\u9fff]+', '', a)
        shot['action'] = re.sub(r'\s*,\s*,\s*', ', ', a).strip(', ')

        # ── v23: 动作描述兜底修复，避免出现过短/乱码式 action_zh（如单字）──
        az = str(shot.get('action_zh') or '').strip()
        if len(az) < 4:
            rebuilt = _compress_segment_action_v21(str(shot.get('source_story_segment') or shot.get('dialogue') or az), relation=str(shot.get('relationship_type') or ''))
            if rebuilt and len(rebuilt) >= 4:
                shot['action_zh'] = rebuilt

        # ── emotion 清洗 ──
        shot['emotion'] = re.sub(r'[\u4e00-\u9fff]+', '',
                                  shot.get('emotion', '')).strip(', ').lower()

        # ── pose_hint 格式化 ──
        ph = shot.get('pose_hint', '')
        if isinstance(ph, list):
            shot['pose_hint'] = ', '.join(ph)

        # ── characters_in_shot 校验 ──
        ci = shot.get('characters_in_shot', [])
        if isinstance(ci, str):
            ci = [ci]
        shot['characters_in_shot'] = [c for c in ci if c in char_names]

        # ── 默认 pose ──
        if not shot.get('pose_hint'):
            al = shot.get('action', '').lower()
            if 'sit' in al:
                shot['pose_hint'] = 'sitting'
            elif 'walk' in al:
                shot['pose_hint'] = 'walking'
            elif 'run' in al:
                shot['pose_hint'] = 'running'
            elif 'kneel' in al:
                shot['pose_hint'] = 'kneeling'
            else:
                shot['pose_hint'] = 'standing'

        # ── v14.6: 关系类型兜底。避免“站在龙背上/骑乘”被后续逻辑误判为左右对峙 ──
        rel = (shot.get('relationship_type') or '').strip()
        relation_text = (
            (shot.get('action_zh') or '') +
            (shot.get('jimeng_ref_prompt') or '') +
            (shot.get('video_prompt') or '')
        )
        if not rel:
            if any(k in relation_text for k in ['站在龙背', '站在巨龙背', '站在龙身上', '骑在龙背', '骑龙', '坐在龙背', '伏在龙颈', '站在肩上', '站在手掌', '托起']):
                rel = 'mounted'
            elif any(k in relation_text for k in ['并肩', '同行', '一起', '共同', '同向']):
                rel = 'same_side'
            elif any(k in relation_text for k in ['追赶', '追击', '追逐']):
                rel = 'chase'
            elif any(k in relation_text for k in ['对峙', '对视', '对抗', '迎战', '交锋', '挥剑', '举剑']):
                rel = 'confront'
            else:
                rel = 'neutral'
        shot['relationship_type'] = rel

        # ── v20: 修复“龙背高光 + 巨龙倒地”同镜冲突，避免生成两条龙 ──
        shot = _fix_dragon_state_conflict(shot, characters)

        # ── duration_hint 范围 4~12 (Seedance 1.5 Pro 支持) ──
        shot['duration_hint'] = max(4, min(int(shot.get('duration_hint', 7) or 7), 12))

        # ── jimeng_ref_prompt 兜底 ──
        if not shot.get('jimeng_ref_prompt') or len(shot.get('jimeng_ref_prompt', '')) < 30:
            cs = shot.get('characters_in_shot', [])
            parts = [f'{style}风格精致插画' if style else '精致动态漫剧插画']
            if shot.get('scene_description'):
                parts.append(shot['scene_description'])
            elif scene_spec:
                parts.append(scene_spec[:80])
            # 插入角色外貌
            for cname in cs:
                desc = char_desc_map.get(cname, '')
                if desc:
                    parts.append(f"{cname}（{desc[:100]}）")
                else:
                    parts.append(cname)
            if shot.get('action_zh'):
                parts.append(shot['action_zh'])
            parts.append('画面精致，色彩丰富，完整背景，高质量')
            shot['jimeng_ref_prompt'] = '，'.join(parts)[:400]

        # ── video_prompt 兜底 ──
        if not shot.get('video_prompt') or len(shot.get('video_prompt', '')) < 30:
            action_zh = shot.get('action_zh', '')
            scene = shot.get('scene_description', '')
            emotion_zh = {
                'sad': '神情忧伤',
                'happy': '嘴角上扬微笑',
                'calm': '神情平静',
                'surprised': '眼睛睁大表情惊讶',
                'determined': '眼神坚定',
                'nostalgic': '若有所思',
                'tearful': '眼眶泛红',
                'gentle': '神情温柔',
                'curious': '眼神好奇',
                'anxious': '神情焦虑',
                'pensive': '凝视沉思',
                'reluctant': '神情犹豫',
            }.get(shot.get('emotion', ''), '')
            seq_parts = []
            if scene:
                seq_parts.append(scene)
            if action_zh:
                seq_parts.append(f"主体先{action_zh}")
            if emotion_zh:
                seq_parts.append(f"然后{emotion_zh}")
            seq_parts.append('环境光影自然流动，衣物和头发有自然飘动')
            seq_parts.append(f'{style}风格，画面流畅细腻' if style else '保持首帧画风，画面流畅细腻')
            shot['video_prompt'] = '，'.join(seq_parts)

        # ── 过渡提示 ──
        if not shot.get('jimeng_trans_prompt'):
            shot['jimeng_trans_prompt'] = '无' if i >= len(shots) - 1 else "画面自然过渡到下一场景"

        # 兼容字段
        shot['jimeng_prompt'] = shot['jimeng_ref_prompt']

    # ── ★ v13: 场景连续性后处理 ──
    # 如果相邻两镜没有 scene_change=true，但 scene_description 完全不同，
    # 则把后一镜的 scene_description 前缀与前一镜对齐
    for i in range(1, len(shots)):
        prev = shots[i - 1]
        curr = shots[i]
        if curr.get('scene_change'):
            continue  # 明确场景切换，允许不同
        prev_scene = (prev.get('scene_description') or '').strip()
        curr_scene = (curr.get('scene_description') or '').strip()
        if not prev_scene or not curr_scene:
            continue
        # 提取前一镜的核心场景词(前20字)
        prev_core = prev_scene[:20]
        # 如果后一镜的 scene_description 完全不包含前一镜的核心词汇
        overlap = sum(1 for ch in prev_core if ch in curr_scene and ch not in '，。、的和在')
        if overlap < 3 and len(prev_core) > 5:
            # 将前一镜的场景描述作为前缀注入
            fixed = f"{prev_scene}，{curr_scene}"
            shots[i]['scene_description'] = fixed[:60]  # 限制长度
            print(f"[分镜后处理] 镜{i+1} 场景不连续, 已注入前一镜场景: {fixed[:40]}...")

    # ── ★ v12 新增: 净化每镜 scene_description, 剥掉 LLM 偷偷混入的人物/动作 ──
    char_names_list = [c.get('name', '') for c in characters if c.get('name')]
    for i, shot in enumerate(shots):
        sd_before = shot.get('scene_description', '') or ''
        if not sd_before:
            continue
        sd_after = _purge_characters_from_scene(sd_before, char_names_list)
        # 如果净化后被削掉超过一半, 说明原场景描述大部分都是剧情, 用兜底
        if sd_after and len(sd_after) < len(sd_before) * 0.5:
            print(f"[分镜后处理] 镜{i+1} scene_description 含大量剧情已净化: "
                  f"{sd_before[:30]}... → {sd_after[:30]}...")
        if sd_after != sd_before:
            shot['scene_description'] = sd_after
            print(f"[分镜后处理] 镜{i+1} scene_description 已净化")
        # 如果完全没了, 用 scene_spec 兜底
        if not shot.get('scene_description'):
            shot['scene_description'] = (scene_spec or '')[:40]

    # ── v21: 最终把每镜绑定回原始剧本段落，并补充角色身份锁 ──
    if story_text:
        shots = _align_shots_to_story_segments_v21(shots, story_text, characters, scene_spec=scene_spec, style=style)
    else:
        for i, shot in enumerate(shots):
            shots[i] = _append_character_identity_locks_v21(shot, characters)

    return shots


# ═══════════════════════════════════════════════════════
# 4. 旧版翻译工具（豆包主链路不再调用）
# ═══════════════════════════════════════════════════════
def translate_to_nai_prompt(chinese_text, context="动作描述"):
    """把中文动作描述翻译成英文 Danbooru 风格标签"""
    system = "你是动漫AI绘图标签翻译专家。只输出英文标签，用逗号分隔。"
    prompt = f"""把以下中文{context}翻译成15-20词的英文AI绘图提示词(Danbooru风格)。

【中文】{chinese_text}

输出要求：
- 只输出英文，不要任何其他内容
- 使用简洁的标签式，逗号分隔
- 优先使用动漫常用词: sitting, standing, side view, three-quarter view, dynamic pose, smile, close-up"""
    r = _call_llm(system, prompt, temperature=0.3, max_tokens=300)
    if r['success']:
        en = re.sub(r'[\u4e00-\u9fff]+', '', r['content']).strip('" \n')
        en = re.sub(r'\s+', ' ', en)
        return {'success': True, 'content': en}
    return r


# ═══════════════════════════════════════════════════════
# 5. 剧情续写
# ═══════════════════════════════════════════════════════
def continue_story(story_text, characters, direction='自然发展'):
    """续写剧情 — 保持角色一致性"""
    char_list = ', '.join([c.get('name', '') for c in characters]) if characters else '原角色'
    system = "你是漫画编剧，擅长根据已有剧情自然延伸，保持角色性格一致。"
    prompt = f"""续写 250-400 字，保持画面感，不写心理活动。

【已有剧情】
{story_text}

【延伸方向】{direction}
【原有角色(请沿用)】{char_list}

【写作要求】
- 直接输出续写内容，不要"接下来..."这类过渡语
- 多用具体动作/对话/场景细节
- 避免心理描写、内心独白
- 风格与原文一致"""
    r = _call_llm(system, prompt, temperature=0.8, max_tokens=1500)
    return {'success': True, 'continued_story': r['content']} if r['success'] else r
