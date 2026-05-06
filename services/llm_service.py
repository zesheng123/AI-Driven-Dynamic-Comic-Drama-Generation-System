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
import json, re
from openai import OpenAI

_DOUBAO_API_KEY  = "cc285fe6-6cff-4507-bcd9-d43a5a0cbb03"
_DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
_DOUBAO_MODEL    = "doubao-1-5-lite-32k-250115"


# ═══════════════════════════════════════════════════════
# 底层 LLM 调用
# ═══════════════════════════════════════════════════════
def _call_llm(system, user, temperature=0.7, max_tokens=4000):
    try:
        client = OpenAI(api_key=_DOUBAO_API_KEY, base_url=_DOUBAO_BASE_URL)
        resp = client.chat.completions.create(
            model=_DOUBAO_MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=temperature, max_tokens=max_tokens,
        )
        return {"success": True, "content": resp.choices[0].message.content}
    except Exception as e:
        print(f"[LLM] 豆包调用失败: {e}")
        return {"success": False, "message": str(e)}


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
def generate_story(direction, genre="校园", length="中篇", custom_requirements=""):
    """根据方向生成完整剧本 + 角色 + 场景
    ★ 核心要求: 剧情必须具有强烈画面感, 不写心理活动/纯背景描述
    """
    system = (
        "你是专业的漫画剧本编剧，擅长创作具有强烈画面感的短篇故事。"
        "你写的剧本必须满足：\n"
        "1. 每一段都有明确的『视觉画面』——看得见的场景、动作、表情\n"
        "2. 不写心理活动、不写纯背景介绍、不写人物的内心独白\n"
        "3. 通过动作、对话、表情、环境细节来传递情感\n"
        "4. 场景切换要具体，角色位置要清晰\n"
        "只输出JSON，不输出任何其他内容。"
    )

    genre_hints = {
        "校园": "青春校园故事，教室/走廊/天台/操场/图书馆等具体场景，学生的具体动作和对话",
        "都市": "现代都市故事，咖啡厅/地铁/公寓/街道等场景，现代人的具体互动",
        "奇幻": "东方奇幻或异世界，古风场景/法术施展/神秘生物等具象元素",
        "科幻": "近未来科技，实验室/飞船/虚拟空间等场景，具体的科技物件",
        "悬疑": "推理悬疑故事，具体的线索/发现/追踪动作，层次分明的视觉节奏",
        "治愈": "温暖日常故事，具体的温馨场景和人与人之间的小动作",
        # ★ v12 新增
        "武侠": "中国武侠故事，江湖/武馆/山林/客栈/演武场等场景，剑法对决、轻功腾跃、暗器飞出等具体武侠动作，有侠义之气",
        "历史": "中国历史古代故事，宫廷/战场/古镇/市集等场景，朝代服饰礼节，刀兵交锋或政治阴谋的具体画面",
        "恐怖": "恐怖惊悚故事，废墟/黑暗地下室/阴雨夜晚等场景，强烈视觉冲击，具体恐怖物件和角色反应（颤抖/奔跑/尖叫）",
        "末世": "末日后科幻，荒废城市/避难所/核爆废土等场景，幸存者的具体生存动作，紧张对峙和情感羁绊",
        "言情": "甜蜜浪漫故事，咖啡厅/公园/约会场景，细腻情感互动、微表情变化、道具传递（花束/礼物/书信）",
        "热血": "热血竞技励志，竞技场/训练场/决赛赛场等场景，紧张的比赛动作和克服困难的具体身体细节",
        "战争": "战争军事题材，战壕/战场/指挥部/医疗帐篷等场景，具体战斗动作和战友情谊的画面瞬间",
        "体育": "运动竞技题材，体育馆/球场/赛道/泳池等场景，具体运动动作的爆发瞬间和情感节奏",
    }

    length_map = {
        "短篇": "200-300字，3个分镜讲完一个完整情节（起-承-合）",
        "中篇": "350-500字，4-5个分镜，完整的起承转合",
        "长篇": "500-700字，6个分镜，情节有转折和高潮",
    }

    prompt = f"""请创作一个{genre}题材的漫画短剧本。

【创作方向】{direction or '自由发挥一个具有画面感的故事'}
【题材风格】{genre_hints.get(genre, genre)}
【篇幅要求】{length_map.get(length, length_map['中篇'])}
{f'【额外要求】{custom_requirements}' if custom_requirements else ''}

═══ 写作规范（★必须严格遵守）═══
✓ 每段至少包含1个具体动作 + 1个具体场景细节 + 1个可视化的表情/物件
✓ 用"具象描写"代替"心理描写"：不写"她很难过"，写"她低下头，一滴眼泪落在作业本上"
✓ 用"动作对话"代替"叙述介绍"：不写"两人认识很久了"，写"她一边说一边熟练地把他的书包挂在椅背上"
✓ 场景要有具体空间位置：窗边、门口、桌前、楼梯上
✓ 光线/时间要明确：清晨的阳光、午后斜光、傍晚余晖、深夜昏暗
✗ 禁止出现"她想起了..."、"他回忆起..."、"内心深处..."等心理描写
✗ 禁止长段的背景介绍或世界观说明

═══ 输出格式 ═══
{{
  "title": "剧本标题(简洁有力，5-10字)",
  "story_text": "完整剧情文本。按上述规范写作，必须有具体场景和动作。{length_map.get(length, '350-500字')}",
  "characters": [
    {{
      "name": "角色中文名",
      "description": "英文外貌描述（60-120词）。★ 根据角色类型灵活调整：【人类】用标准格式 'gender, age, hair, eye color, clothing, body type'。【非人类（龙/机器人/精灵/动物/怪物等）】按角色本身特征描述，绝对禁止塞入 'young female/male'、'hair'、'eye color' 等人类字段。示例：人类 'young female, 17yo, long black hair, brown eyes, white school blouse, navy pleated skirt, slim build'；巨龙 'ancient massive dragon, metallic silver scales, golden glowing eyes, leathery wings, spiked tail'；机器人 'humanoid combat robot, chrome armor, blue LED accents, articulated joints, glowing visor'。",
      "personality": "性格(中文,15字内)",
      "voice_style": "说话风格(中文,10字内，非人类可写'低沉怒吼''机械合成'等)"
    }}
  ],
  "scenes": [
    {{
      "name": "场景名(中文, 4-8字)",
      "description_zh": "★精简纯环境(20-35字)★ 只写【地点 + 关键环境特征 + 光线/天气】, 不要文学化修辞, 不要角色/动作/剧情。合格示例: '焦黑的平原，狂风呼啸，地面裂痕遍布，天色昏沉'(22字)。不合格示例: '焦黑的平原上，一只远古巨龙展翅盘旋而来'(含人物/生物活动)。",
      "description_en": "★Pure environment, 15-25 words★ Location + key features + light/weather only. NO humans/creatures/actions.",
      "time_of_day": "具体时间(如: late afternoon sunset / deep night / early morning)"
    }}
  ],
  "genre": "{genre}",
  "mood": "整体氛围(如: 温暖怀旧/悬疑紧张/青春甜蜜/孤寂治愈)"
}}

重要提示：
- story_text 是最关键的，它会被转换成分镜。务必画面感十足。
- description 必须详细到足以生成一致性高的角色立绘（衣服颜色、款式、配饰）
- 角色2-3个最佳（画面叙事最清晰）
- 场景1-3个即可（过多会让故事碎片化）"""

    result = _call_llm(system, prompt, temperature=0.85, max_tokens=5000)
    if not result['success']:
        return result
    try:
        data = _parse_json(result['content'])
        # 安全检查
        if not data.get('story_text'):
            return {'success': False, 'message': '剧本生成不完整(缺少story_text)'}
        if not data.get('characters'):
            data['characters'] = []
        if not data.get('scenes'):
            data['scenes'] = []

        # ★ v14.3: 对 scenes[].description_zh/en 做净化, 剥掉 LLM 偷偷塞的人物描述
        char_names_for_purge = [
            c.get('name', '') for c in data.get('characters', [])
            if isinstance(c, dict) and c.get('name')
        ]
        for sc in data.get('scenes', []):
            if not isinstance(sc, dict):
                continue
            zh_before = sc.get('description_zh', '') or ''
            if zh_before:
                zh_after = _purge_characters_from_scene(zh_before, char_names_for_purge)
                if zh_after != zh_before:
                    print(f"[剧本] 场景 '{sc.get('name','')}' description_zh 已净化")
                    sc['description_zh'] = zh_after

            # 英文也简单净化: 剥人称代词和 the boy/girl/young man 等
            en_before = sc.get('description_en', '') or ''
            if en_before:
                en_after = re.sub(
                    r'\b(?:he|she|they|him|her|his|hers|them|their|'
                    r'the\s+(?:boy|girl|man|woman|protagonist|character|person|hero|heroine|youth|young\s+(?:man|woman|boy|girl)|ancient\s+\w+|massive\s+\w+|dragon|warrior|knight|princess|prince))\b'
                    r'[^.,;]*[.,;]?',
                    '',
                    en_before,
                    flags=re.IGNORECASE,
                )
                # 剥角色名
                for cn in [c.get('name','') for c in data.get('characters', []) if isinstance(c, dict)]:
                    if cn:
                        en_after = re.sub(re.escape(cn), '', en_after, flags=re.IGNORECASE)
                # 清标点空白
                en_after = re.sub(r'\s+', ' ', en_after).strip(' ,;.')
                if en_after != en_before:
                    sc['description_en'] = en_after

        return {'success': True, 'story': data}
    except Exception as e:
        print(f"[剧本] JSON解析失败: {e}")
        print(f"[剧本] 原始响应前500字: {result['content'][:500]}")
        return {'success': False, 'message': f'剧本JSON解析失败: {e}'}


# ═══════════════════════════════════════════════════════
# 1. 角色提取
# ═══════════════════════════════════════════════════════
def extract_character_description(story_text):
    """从剧情文本中提取角色信息（★ v12: 支持非人类角色：龙、机器人、精灵、动物等）"""
    system = "你是剧本分析师，擅长从文本中提取角色视觉信息。只输出JSON数组。"
    prompt = f"""从以下剧情中提取所有出场角色的完整信息。

【剧情】{story_text}

【要求】
1. description 必须是英文详细外貌描述（60-120 英文词）。★ 重要：根据角色类型（人类/非人类）灵活调整格式：

   【人类角色】用标准格式：
   "gender + age + hair(color/length/style/bangs) + eye color + upper clothing + lower clothing + footwear + accessories + body type"
   示例: "young female, 17 years old, long straight black hair with blunt bangs, clear brown eyes, white school blouse with navy sailor collar, navy pleated skirt, white knee socks, black Mary Jane shoes, slim build, red hair ribbon"

   【非人类角色（龙/巨龙/机器人/精灵/动物/怪物/幽灵等）】按照角色本身的特征描述，**绝对禁止**塞入 "young female/male""17 years old""hair" 等人类字段：
   - 巨龙示例: "ancient massive dragon, metallic silver scales covering entire body, golden glowing eyes, enormous leathery wings, long serpentine tail with spiked tip, sharp curved claws, razor-sharp teeth, horned head crest, wingspan 30 meters"
   - 机器人示例: "humanoid combat robot, polished chrome exterior armor, blue LED accents on chest and shoulders, articulated mechanical joints, servo motors visible at knees, glowing visor eye slit, titanium plating, 2.5 meters tall"
   - 野兽示例: "large grey wolf, thick fur coat with white underbelly, piercing amber eyes, long snout, pointed ears, muscular build, bushy tail"
   - 精灵示例: "elven archer, long platinum blonde hair, pointed ears, silver eyes, forest green tunic with leather bracers, dark brown cloak, quiver on back, slim athletic build"

2. 自动识别角色类型 —— 剧情中提到"龙""怪兽""机器人""精灵""动物"等非人类词汇时，必须用非人类格式描述，绝不强加人类特征。

3. 即使剧情没明说服装/外观细节，也要根据场景和角色类型合理补全（校园人→校服；都市人→休闲装；奇幻人→古装；巨龙→按龙的特征；机器人→按机械特征）。

4. personality 中文15字内，voice_style 中文10字内（非人类角色的 voice_style 可以是"低沉怒吼""机械合成""兽类嘶吼"等）。

【返回格式】
[
  {{
    "name": "角色中文名",
    "description": "完整英文外貌描述",
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
            d = re.sub(r'[\u4e00-\u9fff]+', '', d)
            c['description'] = re.sub(r'\s+', ' ', d).strip(', ')
        return {'success': True, 'characters': chars}
    except Exception as e:
        return {'success': False, 'message': f'角色解析失败: {e}'}


# ═══════════════════════════════════════════════════════
# 2. 场景规范 ★ 中英双输出
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
         scene_spec_en 给 NAI 用
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
            'scene_spec_zh': zh,   # 中文版(给豆包用)
            'scene_spec_en': en,   # 显式英文版
        }
    except Exception:
        # fallback: 全清中文的旧行为 + 净化
        spec = re.sub(r'[\u4e00-\u9fff]+', ' ', result['content']).strip()
        spec = re.sub(r'\s+', ' ', spec)
        return {'success': True, 'scene_spec': spec, 'scene_spec_zh': '', 'scene_spec_en': spec}


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
    # ── 默认：日漫 / 校园 ──
    else:
        single = (
            "「日系动漫风格精致插画，放学后的教室内部，夕阳从西侧窗户斜射进来在"
            "木质地板上投下长长的金色光斑，空气中漂浮着细小尘埃，黑板已擦干净，"
            "整齐排列的课桌椅。画面主角是林晓：十七岁女高中生，齐腰的黑色长直发"
            "配齐刘海，清澈的棕色眼睛，穿着白色水手服校服上衣配红色领巾、藏青色"
            "百褶裙、白色过膝袜和黑色皮鞋，身材纤细。她正站在窗边，右手轻轻抚过"
            "课桌表面，低垂着眼睛看着桌上摊开的一封信，表情若有所思。中景侧面构图，"
            "画面笼罩在温暖的金色夕照中」"
        )
        two = (
            "「日系动漫风格精致插画，樱花纷飞的校门口春日午后，柔和阳光透过樱花树，"
            "地上散落粉色花瓣，背景有模糊的校舍。画面左侧是陈默：十八岁少年，"
            "黑色短碎发露出额头，深邃的黑眼睛，穿着深蓝色立领校服外套、白色衬衫、"
            "灰色西裤、黑色皮鞋，挺拔身材，正右手伸出递出一本淡蓝色封皮的笔记本。"
            "画面右侧是苏晴：十七岁少女，棕色中长发扎低马尾有碎刘海，明亮的浅棕色"
            "眼睛，穿着白色水手服、深蓝百褶裙、白色过膝袜、黑色皮鞋，纤细身材，"
            "正双手伸出准备接过笔记本，微微低头脸颊泛红。近景正面构图，粉色花瓣"
            "在两人之间飘舞」"
        )
    return {"single": single, "two": two}


def generate_storyboard_script(story_text, characters, style='日漫',
                                global_scene='', scene_spec='',
                                scene_spec_zh='', global_tone='', **kw):
    """生成分镜脚本 — 为 Seedream 4.5 + Seedance 1.5 Pro 定制
    ★ 新增特性:
      1. jimeng_ref_prompt: 300字自然语言画面描述(自带角色外貌/场景/动作)
      2. video_prompt: 250字动态序列描述(先→然后→接着)
      3. 镜间一致性约束(同一角色外貌必须一致, 场景必须连续)
      4. 支持5-6镜, 目标总时长44-60秒
    """
    if not characters:
        characters = []

    tl = len((story_text or '').strip())
    # ★ 分镜数量: 根据剧情长度动态调整
    if tl < 120:   mx, ts = 3, "3"
    elif tl < 280: mx, ts = 4, "4"
    elif tl < 500: mx, ts = 5, "5"
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

    # 场景参考 — 优先中文
    scene_ref = scene_spec_zh or scene_spec or global_scene or ''

    # ★ v12: 按画风切换示例，避免所有题材都给校园日漫示例
    _style_examples = _get_style_examples(style)
    _ex_single = _style_examples["single"]
    _ex_two    = _style_examples["two"]

    system = f"""你是动漫分镜导演，把剧情拆成分镜。只输出JSON数组，不要任何其他内容。

【画风】{style}

【输出规则】
1. 同一角色在所有分镜中的外貌描述必须一致（发色/服装颜色款式完全不变）
2. jimeng_ref_prompt 用纯中文，80-120 字精炼描述（画风+场景+角色+动作+构图），不要重复
3. ★★★【朝向铁律】多角色互动时必须写明双方朝向：
   - 对抗/对峙："A在画面左侧侧身朝右，B在画面右侧侧身朝左，两者视线交汇对视"
   - 并肩："A、B并肩而立，同时朝向画面右方/朝向某共同方向"
   - 追击："被追者在前方朝右奔跑，追者在后方朝右追赶"
   ❌ 禁止写"两人站在战场"这种不明朝向的模糊描述
   ❌ 禁止让某角色背对另一角色（除非剧情明确要求）
4. 相邻分镜的场景保持连续，除非 scene_change=true
5. 多角色时第一镜写明体型比例（如"巨龙体型是少年8倍"），后续镜保持一致
6. 景别要有变化，不允许连续3镜用同一景别
7. ★ scene_description 必须只写【纯环境】(空间/光线/色调/天气/氛围)，严禁出现人物、角色名、动作、对话、情绪、"二者""双方""勾勒XX轮廓"等隐式指人表达
8. ★ 服装颜色和款式锁定: 每个角色的衣着必须在每一镜的 jimeng_ref_prompt 里都明确写出(如"黑色学生校服""红色连衣裙"), 不得省略或变更
9. ★ 背景必须有明确环境描述, 禁止写"白色背景""纯色背景""简约背景""空白背景"等

★ 每个字段字数严格控制，否则输出被截断："""

    prompt = f"""将以下剧情转换成 {ts} 个分镜。

【剧情】
{story_text}

【角色档案（所有分镜必须严格使用下列外貌描述）】
{cd}

【场景参考】{scene_ref or '根据剧情设计'}

【字段说明】
- shot_type: 特写/近景/中景/远景/全景/环境（只能选一个）
- camera_angle: 平视/俯视/仰视/侧面
- scene_description: ★纯环境描述(空间布局+光影+色调+氛围)，≤40中文字，严禁出现人物/角色名/动作/对话
- action_zh: 核心动作，≤25中文字
- action: 英文动作，≤15词
- pose_hint: standing/sitting/walking/running/kneeling/flying 等英文单词
- dialogue: 角色台词，可空
- characters_in_shot: 数组，只填上面角色档案里有的名字
- emotion: 英文单词 (calm/happy/sad/surprised/determined/tearful/anxious)
- duration_hint: 整数秒，简单情绪5秒、中等动作7秒、复杂序列10秒
- scene_change: true/false
- jimeng_ref_prompt: ★纯中文 80-120 字★ 画风+场景光影+角色完整外貌(含服装颜色款式)+动作+构图角度，例如：
  {_ex_single[:150]}...

【返回格式 — 严格按照这个结构，字数不要超】
[
  {{
    "shot_type": "中景",
    "camera_angle": "平视",
    "scene_description": "场景环境光影（≤40字）",
    "action_zh": "核心动作（≤25字）",
    "action": "english action (≤15 words)",
    "pose_hint": "standing",
    "dialogue": "",
    "characters_in_shot": ["角色名"],
    "emotion": "calm",
    "duration_hint": 6,
    "scene_change": false,
    "jimeng_ref_prompt": "★纯中文80-120字画面描述，包含画风、场景光影、角色完整外貌、动作、构图★"
  }}
]

【最终检查】
- 每镜 jimeng_ref_prompt 字数 80-120 字（不要超）
- 同一角色在各镜外貌描述完全一致
- 相邻镜景别有变化
- JSON 格式正确，所有字符串必须用双引号闭合"""

    result = _call_llm(system, prompt, temperature=0.55, max_tokens=8000)
    if not result['success']:
        return result
    try:
        shots = _parse_json(result['content'])
        if not isinstance(shots, list) or len(shots) == 0:
            raise ValueError('返回不是有效数组')
        shots = _post_process(shots, characters, scene_spec_zh or scene_spec)
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
        retry_prompt = f"""把以下剧情拆成 {ts} 个分镜，以 JSON 数组返回：

剧情：{story_text[:500]}

角色：{', '.join(c.get('name','') for c in characters)}

画风：{style}

每个分镜包含字段（字数严格）：
- shot_type: 特写/近景/中景/远景/全景
- scene_description: ≤30字场景
- action_zh: ≤20字动作
- pose_hint: standing/sitting/walking/running/kneeling
- characters_in_shot: 角色名数组
- emotion: calm/happy/sad/surprised/determined
- duration_hint: 整数秒
- dialogue: 可空
- jimeng_ref_prompt: ★纯中文60-90字★ 画风+场景+角色特征+动作+构图

返回格式：
[{{"shot_type":"中景","scene_description":"...","action_zh":"...","pose_hint":"standing","characters_in_shot":["..."],"emotion":"calm","duration_hint":5,"dialogue":"","jimeng_ref_prompt":"..."}}]

严格要求：
1. 必须是合法 JSON，双引号闭合
2. jimeng_ref_prompt 不超 90 字
3. 只输出 JSON，不要任何说明文字"""

        retry_result = _call_llm(retry_system, retry_prompt, temperature=0.3, max_tokens=5000)
        if not retry_result['success']:
            return {'success': False, 'message': f'分镜解析失败: {e}; 重试也失败'}
        try:
            shots = _parse_json(retry_result['content'])
            if not isinstance(shots, list) or len(shots) == 0:
                raise ValueError('重试返回不是有效数组')
            shots = _post_process(shots, characters, scene_spec_zh or scene_spec)
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
            return {'success': False, 'message': f'分镜解析失败（已重试）: {e2}'}


def _post_process(shots, characters, scene_spec=''):
    """后处理：清洗字段、校验角色名、兜底缺失字段"""
    char_names = {c['name'] for c in characters}
    char_desc_map = {c['name']: c.get('description', '') for c in characters}

    for i, shot in enumerate(shots):
        # ── 英文 action 清洗 ──
        a = shot.get('action', '')
        a = re.sub(r'\([^)]*\)', '', a)
        a = re.sub(r'[\u4e00-\u9fff]+', '', a)
        shot['action'] = re.sub(r'\s*,\s*,\s*', ', ', a).strip(', ')

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

        # ── duration_hint 范围 4~12 (Seedance 1.5 Pro 支持) ──
        shot['duration_hint'] = max(4, min(int(shot.get('duration_hint', 7) or 7), 12))

        # ── jimeng_ref_prompt 兜底 ──
        if not shot.get('jimeng_ref_prompt') or len(shot.get('jimeng_ref_prompt', '')) < 30:
            cs = shot.get('characters_in_shot', [])
            parts = ['日系动漫风格精致插画']
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
            seq_parts.append('日系动漫风格，画面流畅细腻')
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

    return shots


# ═══════════════════════════════════════════════════════
# 4. 翻译 (为 NAI 服务)
# ═══════════════════════════════════════════════════════
def translate_to_nai_prompt(chinese_text, context="动作描述"):
    """把中文动作描述翻译成英文 Danbooru 风格标签"""
    system = "你是动漫AI绘图标签翻译专家。只输出英文标签，用逗号分隔。"
    prompt = f"""把以下中文{context}翻译成15-20词的英文AI绘图提示词(Danbooru风格)。

【中文】{chinese_text}

输出要求：
- 只输出英文，不要任何其他内容
- 使用简洁的标签式，逗号分隔
- 优先使用动漫常用词: sitting, standing, looking at viewer, smile, close-up, side view"""
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