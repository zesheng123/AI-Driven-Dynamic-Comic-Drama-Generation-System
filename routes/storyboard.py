"""
routes/storyboard.py — v11 分镜生成 (修复参考图锁死)
═══════════════════════════════════════════════════════
★ v11 升级 ★
  1. 支持 reference_mode 参数传递 ('strong'/'guide'/'off')
     - 前端可控制参考图强度
     - 默认 'guide' (修复参考图锁死问题)
  2. 首镜标识 is_first_shot 传递给 image_service
  3. _extract_tone 用关键词抽取扩展
  4. scene_change 字段识别新场景,不再强塞全景图
  5. 允许前端在 regen 时强制 reference_mode='off'
     (用户想看"完全重新生成"时用)
"""
import os, json, uuid, time, re
from flask import Blueprint, request, jsonify, Response, stream_with_context
from services.llm_service import (
    generate_storyboard_script, generate_scene_spec, continue_story,
)
from services.image_service import generate_storyboard_image

storyboard_bp = Blueprint('storyboard', __name__)
STORYBOARD_DB = {}

# ★ v12: 项目数据落盘路径
_PROJECTS_DB_PATH = os.path.join('uploads', 'projects.json')


def _save_projects_db():
    """持久化项目数据到 JSON 文件，最多保留最近 50 个项目"""
    try:
        os.makedirs('uploads', exist_ok=True)
        # ★ v12.1: 限制条目数量，按 created_at 保留最新 50 个
        if len(STORYBOARD_DB) > 50:
            sorted_keys = sorted(
                STORYBOARD_DB, key=lambda k: STORYBOARD_DB[k].get('created_at', 0)
            )
            for old_key in sorted_keys[:-50]:
                del STORYBOARD_DB[old_key]
        with open(_PROJECTS_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(STORYBOARD_DB, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f'[projects] DB保存失败: {e}')


def _load_projects_db():
    """启动时从 JSON 恢复项目数据"""
    if not os.path.exists(_PROJECTS_DB_PATH):
        return
    try:
        with open(_PROJECTS_DB_PATH, 'r', encoding='utf-8') as f:
            STORYBOARD_DB.update(json.load(f))
        print(f'[projects] 已加载 {len(STORYBOARD_DB)} 个项目')
    except Exception as e:
        print(f'[projects] DB读取失败: {e}')


_load_projects_db()


# ═══════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════
def _sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# v11 色调关键词库 (扩展 3x)
_TONE_KEYWORDS = [
    # 光线
    '温暖金色光', '夕阳余晖', '金色夕照', '晨光', '清晨微光', '月光', '星光',
    '柔和光线', '硬光', '漫反射', '顺光', '逆光', '侧光', '顶光',
    '霓虹灯光', '烛光', '火光', '篝火光', '荧光', '电灯光',
    # 色调
    '昏暗', '冷色调', '暖色调', '高饱和', '低饱和', '灰暗',
    '莫兰迪色调', '复古色调', '怀旧色调', '高对比度',
    # 天气/时间
    '阴雨天', '雨雾', '雾气', '朦胧', '潮湿', '阴沉', '多云',
    '烈日', '晴朗', '阳光明媚', '夕阳西下', '黄昏', '黎明',
    '深夜', '午夜', '子夜',
    # 氛围
    '梦幻', '诡异', '紧张', '悬疑', '浪漫', '温馨', '孤寂', '荒凉',
    '神秘', '压抑', '悲伤',
]


def _extract_tone(shots):
    """v11 修复+扩展: 从所有镜头的场景描述中抓取最显著的色调/氛围词"""
    if not shots:
        return ''
    # 合并所有镜头的场景描述
    combined = ' '.join(s.get('scene_description', '') for s in shots)
    if not combined:
        return ''
    # 按长度降序匹配(优先匹配长词,避免子串干扰)
    for kw in sorted(_TONE_KEYWORDS, key=len, reverse=True):
        if kw in combined:
            return kw
    return ''


def _get_char_refs(shot, characters):
    names = shot.get('characters_in_shot', [])
    return [c for c in characters if c.get('name', '') in names] if names else []


def _clean_spec(scene_spec, keep_chinese=False):
    """保留中文版本或清理为纯英文"""
    if isinstance(scene_spec, dict):
        scene_spec = scene_spec.get('scene_spec', '')
    if not scene_spec:
        return ''
    if keep_chinese:
        return scene_spec.strip()
    s = re.sub(r'[\u4e00-\u9fff]+', ' ', scene_spec).strip()
    return re.sub(r'\s+', ' ', s)


def _load_chars(char_names) -> list:
    db_path = os.path.join('uploads', 'characters.json')
    if not os.path.exists(db_path):
        return []
    try:
        with open(db_path, encoding='utf-8') as f:
            db = json.load(f)
        chars = list(db.values())
        if char_names:
            names = [c.get('name') if isinstance(c, dict) else c for c in char_names]
            chars = [c for c in chars if c.get('name') in names]
        return chars
    except Exception as e:
        print(f'[chars] 读取失败: {e}')
        return []


def _generate_video_prompt_fallback(shot, is_env=False):
    """兜底版本的视频提示词"""
    scene = shot.get('scene_description', '') or ''
    action_zh = shot.get('action_zh', '') or ''
    shot_type = shot.get('shot_type', '') or ''
    emotion = shot.get('emotion', '') or ''

    emotion_map = {
        'sad': '神情忧伤低落', 'happy': '嘴角上扬愉悦',
        'surprised': '眼睛睁大惊讶', 'angry': '眉头紧皱愤怒',
        'calm': '神情平静淡然', 'determined': '眼神坚定沉着',
        'nostalgic': '若有所思迷离', 'tearful': '眼眶泛红含泪',
        'gentle': '神情温柔和煦', 'curious': '眼神好奇歪头',
        'shocked': '震惊失色呆滞', 'puzzled': '蹙眉困惑思索',
        'anxious': '神情焦虑不安', 'pensive': '沉思凝视远方',
        'reluctant': '神情犹豫踌躇',
    }
    emotion_zh = emotion_map.get(emotion, '')

    camera_map = {
        '特写': '镜头缓慢推近聚焦面部',
        '近景': '镜头轻微浮动手持感',
        '中景': '镜头平稳横移',
        '远景': '镜头缓缓拉远',
        '全景': '镜头缓慢旋转环视',
        '环境': '固定镜头光影流动',
    }
    camera_zh = next((v for k, v in camera_map.items() if k in shot_type),
                    '镜头保持稳定')

    if is_env:
        parts = [p for p in [scene, '无人物纯场景', '光影随时间缓慢流动',
                              camera_zh, '日系动漫风格，画面流畅'] if p]
        return '，'.join(parts)

    if action_zh:
        seq = f"先{action_zh}"
        if emotion_zh:
            seq += f"，然后{emotion_zh}"
        seq += f"，{camera_zh}"
    else:
        seq = camera_zh

    parts = [p for p in [scene, seq, '衣物发丝自然飘动',
                          '日系动漫风格，画面流畅高质量'] if p]
    return '，'.join(parts)


def _generate_multiframe_prompt(shots):
    """为即梦智能多帧模式生成整体提示词"""
    actions = []
    for s in shots:
        action_zh = s.get('action_zh', '')
        if action_zh:
            actions.append(action_zh)
        elif s.get('action'):
            actions.append(s['action'])

    emotions = []
    emotion_map = {
        'sad': '忧伤', 'happy': '愉快', 'surprised': '惊讶',
        'angry': '愤怒', 'calm': '平静', 'determined': '坚定',
        'nostalgic': '怀念', 'tearful': '含泪', 'gentle': '温柔',
    }
    for s in shots:
        e = s.get('emotion', '')
        if e in emotion_map and emotion_map[e] not in emotions:
            emotions.append(emotion_map[e])

    parts = ['动漫风格', '背景保持固定不变', '镜头保持稳定']
    if actions:
        key_actions = actions[:4]
        parts.append('动作变化：' + '→'.join(key_actions))
    parts.append('人物动作缓慢流畅')
    parts.append('头发轻微飘动')
    if emotions:
        parts.append('情绪从' + '到'.join(emotions[:3]))
    parts.append('光影缓慢变化')
    parts.append('高质量动画')
    parts.append('画面柔和流畅')

    return '，'.join(parts)


def _build_fallback_scene_specs(global_scene: str):
    """LLM 失败时的场景规范兜底。"""
    scene_spec_zh = (global_scene or '荒凉平原').strip()
    scene_spec_en = _clean_spec(scene_spec_zh, keep_chinese=False)
    if not scene_spec_en:
        scene_spec_en = 'scorched wasteland, cracked ground, dramatic sky, strong wind'
    return scene_spec_en, scene_spec_zh


def _build_fallback_shots(story_text: str, global_scene: str, chars_data: list):
    """LLM 失败时的分镜脚本兜底，保证后续生图/视频可继续。"""
    scene = (global_scene or '荒凉平原').strip()
    char_names = [c.get('name', '') for c in (chars_data or []) if c.get('name')]
    first_char = char_names[0] if char_names else ''
    second_char = char_names[1] if len(char_names) > 1 else ''

    shots = [
        {
            'shot_type': '远景',
            'scene_description': scene,
            'action': '狂风席卷地面，尘土与碎石被卷起',
            'action_zh': '狂风席卷地面，尘土与碎石被卷起',
            'dialogue': '',
            'emotion': 'calm',
            'characters_in_shot': [],
            'duration_hint': 5,
            'scene_change': False,
            'is_key_shot': True,
        },
        {
            'shot_type': '中景',
            'scene_description': scene,
            'action': '地面裂缝蔓延，能量或光芒从裂缝中翻涌而出',
            'action_zh': '地面裂缝蔓延，能量或光芒从裂缝中翻涌而出',
            'dialogue': '',
            'emotion': 'determined',
            'characters_in_shot': [first_char] if first_char else [],
            'duration_hint': 6,
            'scene_change': False,
            'is_key_shot': False,
        },
        {
            'shot_type': '特写' if first_char else '近景',
            'scene_description': scene,
            'action': '镜头聚焦关键主体，气流与光影持续翻动，氛围紧张',
            'action_zh': '镜头聚焦关键主体，气流与光影持续翻动，氛围紧张',
            'dialogue': '',
            'emotion': 'determined',
            'characters_in_shot': ([first_char, second_char] if second_char else ([first_char] if first_char else [])),
            'duration_hint': 6,
            'scene_change': False,
            'is_key_shot': True,
        }
    ]
    return {
        'success': True,
        'shots': shots,
        'total_duration': sum(s.get('duration_hint', 5) for s in shots),
        '_fallback': True,
    }


# ═══════════════════════════════════════════════════════
# 路由 1: 只生成分镜脚本
# ═══════════════════════════════════════════════════════
@storyboard_bp.route('/generate_script', methods=['POST'])
def generate_script_only():
    data = request.get_json()
    story_text = data.get('story_text', data.get('story', '')).strip()
    characters = data.get('characters', [])
    style = data.get('style', '日漫')
    global_scene = data.get('global_scene', '')
    scene_spec_in = data.get('scene_spec', '')

    if not story_text:
        return jsonify(ok=False, message='请输入剧情')

    if characters and isinstance(characters[0], dict) and 'description' in characters[0]:
        chars_data = characters
    else:
        chars_data = _load_chars(characters)

    project_id = str(uuid.uuid4())[:8]

    scene_spec_en = ''
    scene_spec_zh = ''
    if scene_spec_in:
        scene_spec_en = _clean_spec(scene_spec_in, keep_chinese=False)
        scene_spec_zh = _clean_spec(scene_spec_in, keep_chinese=True)
    else:
        try:
            r = generate_scene_spec(story_text, global_scene, style, characters=chars_data)
            if r.get('success'):
                scene_spec_en = r.get('scene_spec_en', '') or r.get('scene_spec', '')
                scene_spec_zh = r.get('scene_spec_zh', '') or global_scene
            else:
                print(f'[script] scene_spec 失败: {r.get("message","")}')
                scene_spec_en, scene_spec_zh = _build_fallback_scene_specs(global_scene)
        except Exception as e:
            print(f'[script] scene_spec 异常: {e}')
            scene_spec_en, scene_spec_zh = _build_fallback_scene_specs(global_scene)

    try:
        script_result = generate_storyboard_script(
            story_text=story_text, characters=chars_data, style=style,
            global_scene=global_scene,
            scene_spec=scene_spec_en,
            scene_spec_zh=scene_spec_zh,
        )

        if not script_result or not script_result.get('success'):
            print(f"[script] LLM脚本失败，启用兜底: {script_result.get('message', '') if script_result else 'empty'}")
            script_result = _build_fallback_shots(story_text, global_scene, chars_data)

        shots = script_result['shots']
        multiframe_prompt = _generate_multiframe_prompt(shots)

        # ★ v12.1: generate_script_only 也要附加 voice 字段（与流式接口保持一致）
        try:
            from services.tts_service import resolve_character_voice
            _voice_map = {}
            for c in chars_data:
                cname = c.get('name', '')
                cdesc = (c.get('description', '') or '').lower()
                is_female = any(w in cdesc for w in ['female', 'girl', 'woman', 'lady', '女'])
                is_male   = any(w in cdesc for w in ['male', 'boy', ' man ', 'gentleman', '男'])
                gender = 'male' if (is_male and not is_female) else 'female'
                _voice_map[cname] = resolve_character_voice(c.get('voice_style', ''), gender)
        except Exception:
            _voice_map = {}

        for i, shot in enumerate(shots):
            shot['index'] = i + 1
            # 附加音色
            chars_in = shot.get('characters_in_shot', [])
            if chars_in and _voice_map:
                shot['voice'] = _voice_map.get(chars_in[0], 'zh-CN-XiaoxiaoNeural')
            if not shot.get('jimeng_prompt'):
                is_env = len(chars_in) == 0
                shot['jimeng_prompt'] = _generate_video_prompt_fallback(shot, is_env)

        STORYBOARD_DB[project_id] = {
            'id': project_id, 'story_text': story_text, 'style': style,
            'global_scene': global_scene,
            'scene_spec': scene_spec_en,
            'scene_spec_zh': scene_spec_zh,
            'shots': shots, 'characters': chars_data,
            'created_at': int(time.time()),
        }
        _save_projects_db()  # ★ v12: 持久化

        return jsonify(ok=True, shots=shots, project_id=project_id,
                       scene_spec=scene_spec_en,
                       scene_spec_zh=scene_spec_zh,
                       multiframe_prompt=multiframe_prompt,
                       total_duration=script_result.get('total_duration', 0))

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify(ok=False, message=str(e))


# ═══════════════════════════════════════════════════════
# 路由 2: 流式分镜生成 (主接口, v11 核心修改)
# ═══════════════════════════════════════════════════════
@storyboard_bp.route('/generate_stream', methods=['POST'])
def generate_stream():
    data = request.get_json()
    story_text = data.get('story_text', '').strip()
    characters = data.get('characters', [])
    style = data.get('style', '日漫')
    global_scene = data.get('global_scene', '')
    panorama_url = data.get('panorama_url', '')
    panorama_views = data.get('panorama_views', [])
    scene_spec_in = data.get('scene_spec', '')
    engine = data.get('engine', 'doubao')
    quality = data.get('quality', '16:9')
    # v11 新增: 参考图强度 ('strong'/'guide'/'off')
    reference_mode = data.get('reference_mode', 'guide')

    if not story_text:
        return jsonify({'success': False, 'message': '剧情文本不能为空'}), 400

    if characters and isinstance(characters[0], dict) and 'description' in characters[0]:
        chars_data = characters
    else:
        chars_data = _load_chars(characters)

    def event_stream():
        # ── Step 0: 场景规范 ──
        scene_spec_en = ''
        scene_spec_zh = ''
        if scene_spec_in:
            scene_spec_en = _clean_spec(scene_spec_in, keep_chinese=False)
            scene_spec_zh = _clean_spec(scene_spec_in, keep_chinese=True)
        else:
            yield _sse('progress', {'step': 'scene_spec',
                                     'msg': '豆包生成场景视觉规范…'})
            try:
                r = generate_scene_spec(story_text, global_scene, style, characters=chars_data)
                if r.get('success'):
                    scene_spec_en = r.get('scene_spec_en', '') or r.get('scene_spec', '')
                    scene_spec_zh = r.get('scene_spec_zh', '') or global_scene
                else:
                    scene_spec_en, scene_spec_zh = _build_fallback_scene_specs(global_scene)
                    print(f'[storyboard] scene_spec 失败: {r.get("message","")}')
            except Exception as e:
                scene_spec_en, scene_spec_zh = _build_fallback_scene_specs(global_scene)
                print(f'[storyboard] scene_spec 异常: {e}')

        # ── Step 1: 生成脚本 ──
        yield _sse('progress', {'step': 'script',
                                 'msg': '豆包解析剧情，生成分镜脚本…'})
        try:
            script_result = generate_storyboard_script(
                story_text=story_text, characters=chars_data, style=style,
                global_scene=global_scene,
                scene_spec=scene_spec_en,
                scene_spec_zh=scene_spec_zh,
            )
        except Exception as e:
            print(f'[storyboard] 脚本生成异常，启用兜底: {e}')
            script_result = None

        if not script_result or not script_result.get('success'):
            print(f"[storyboard] 脚本生成失败，启用兜底: {script_result.get('message', '') if script_result else 'empty'}")
            script_result = _build_fallback_shots(story_text, global_scene, chars_data)

        shots = script_result['shots']
        project_id = str(uuid.uuid4())[:8]
        total = len(shots)
        global_tone = _extract_tone(shots)

        # ★ v12: 构建角色名→音色 映射表 (供 TTS 使用)
        try:
            from services.tts_service import resolve_character_voice
            _char_voice_map = {}
            for c in chars_data:
                cname = c.get('name', '')
                vs = c.get('voice_style', '')
                # ★ v12.1 修复: 用优先级顺序避免 'man' ⊂ 'woman' 子串误判
                cdesc = (c.get('description', '') or '').lower()
                is_female = any(w in cdesc for w in ['female', 'girl', 'woman', 'lady', '女'])
                is_male   = any(w in cdesc for w in ['male', 'boy', ' man ', 'gentleman', '男'])
                gender = 'male' if (is_male and not is_female) else 'female'
                _char_voice_map[cname] = resolve_character_voice(vs, gender)
            print(f'[tts] 角色音色映射: {_char_voice_map}')
        except Exception as e:
            _char_voice_map = {}
            print(f'[tts] 音色映射失败: {e}')

        # 预处理: 角色描述英文化
        try:
            from services.image_service import _ensure_char_desc_english
            for c in chars_data:
                en = _ensure_char_desc_english(c)
                print(f"[chars] {c.get('name')} → {en[:100]}")
        except Exception as e:
            print(f"[chars] 描述转换失败: {e}")

        # 通知前端展示骨架
        yield _sse('script_done', {
            'total': total, 'global_tone': global_tone,
            'scene_spec': scene_spec_en,
            'scene_spec_zh': scene_spec_zh,
            'panorama_url': panorama_url,
            'total_duration': script_result.get('total_duration', 0),
            'shots_preview': [
                {'index': i + 1, 'shot_type': s.get('shot_type', ''),
                 'dialogue': s.get('dialogue', ''),
                 'duration_hint': s.get('duration_hint', 7)}
                for i, s in enumerate(shots)
            ]
        })

        # ── Step 2: 串行逐镜生图 (v11 修改) ──
        results = []
        prev_image = None
        _current_panorama_views = list(panorama_views or [])  # ★ v12: 可变全景图列表

        for i, shot in enumerate(shots):
            yield _sse('progress', {
                'step': 'image', 'current': i + 1, 'total': total,
                'msg': f'生成第 {i + 1}/{total} 张分镜…',
                'shot_type': shot.get('shot_type', ''),
            })

            # ★ v12: 为分镜附加台词角色的音色
            chars_in_shot = shot.get('characters_in_shot', [])
            if chars_in_shot and _char_voice_map:
                # 取第一个出场角色的音色作为该镜台词的配音
                shot_voice = _char_voice_map.get(chars_in_shot[0], 'zh-CN-XiaoxiaoNeural')
                shot['voice'] = shot_voice

            # ★ v12: 场景切换时重新生成全景图
            if shot.get('scene_change') and i > 0:
                scene_desc = shot.get('scene_description', '') or global_scene
                if scene_desc:
                    yield _sse('progress', {
                        'step': 'panorama_regen', 'current': i + 1, 'total': total,
                        'msg': f'第 {i + 1} 镜检测到场景切换，重新生成全景图…',
                    })
                    try:
                        from services.panorama_service import (
                            generate_equirect_panorama, convert_panorama_to_views
                        )
                        new_spec_r = generate_scene_spec(scene_desc, scene_desc, style)
                        new_spec_en = (new_spec_r.get('scene_spec_en') or
                                       new_spec_r.get('scene_spec', scene_desc)
                                       if new_spec_r.get('success') else scene_desc)
                        new_pano = generate_equirect_panorama(
                            new_spec_en, style, raw_scene=scene_desc
                        )
                        if new_pano.get('success'):
                            new_views_r = convert_panorama_to_views(
                                new_pano['local_path'], mode='quad'
                            )
                            if new_views_r.get('success'):
                                _current_panorama_views = new_views_r['views']
                                yield _sse('panorama_updated', {
                                    'shot_index': i + 1,
                                    'panorama_url': new_pano['url'],
                                    'panorama_local': new_pano['local_path'],
                                    'views': _current_panorama_views,
                                })
                                print(f'[storyboard] ★ 第{i+1}镜场景切换 → 新全景图已生成')
                            else:
                                print(f'[storyboard] 新全景视角生成失败')
                        else:
                            print(f'[storyboard] 新全景图生成失败: {new_pano.get("message","")}')
                    except Exception as e:
                        print(f'[storyboard] 场景切换全景重生成异常: {e}')

            try:
                print(f"\n[debug] shot {i + 1} chars={shot.get('characters_in_shot')} "
                      f"scene_change={shot.get('scene_change', False)}")
                char_refs = _get_char_refs(shot, chars_data)

                img_result = generate_storyboard_image(
                    shot=shot,
                    char_refs=char_refs,
                    art_style=style,
                    scene_spec=scene_spec_en,
                    scene_spec_zh=scene_spec_zh,
                    global_tone=global_tone,
                    scene_views=_current_panorama_views,
                    panorama_views=_current_panorama_views,
                    all_chars=chars_data,
                    project_id=project_id,
                    prev_shot_image=prev_image,
                    engine=engine,
                    quality=quality,
                    # v11 新增
                    reference_mode=reference_mode,
                    is_first_shot=(i == 0),
                )
                if img_result.get('success'):
                    shot['image_url'] = img_result.get('image_url', '')
                    shot['image_error'] = ''
                    prev_image = shot['image_url']
                else:
                    shot['image_url'] = ''
                    shot['image_error'] = img_result.get('message', '')
            except Exception as e:
                print(f'[storyboard] 第{i + 1}镜异常: {e}')
                shot['image_url'] = ''
                shot['image_error'] = str(e)

            shot['index'] = i + 1
            if not shot.get('video_prompt') or len(shot.get('video_prompt', '')) < 20:
                is_env = len(shot.get('characters_in_shot', [])) == 0
                shot['video_prompt'] = _generate_video_prompt_fallback(shot, is_env)

            results.append(shot)
            yield _sse('shot_done', {'index': i + 1, 'shot': shot})

        multiframe_prompt = _generate_multiframe_prompt(results)

        STORYBOARD_DB[project_id] = {
            'id': project_id, 'story_text': story_text, 'style': style,
            'global_scene': global_scene,
            'scene_spec': scene_spec_en,
            'scene_spec_zh': scene_spec_zh,
            'panorama_url': panorama_url,
            'panorama_views': _current_panorama_views,  # ★ v12: 含场景切换后更新的视图
            'global_tone': global_tone,
            'shots': results, 'characters': chars_data,
            'reference_mode': reference_mode,
            'created_at': int(time.time()),
        }
        _save_projects_db()  # ★ v12: 持久化
        yield _sse('complete', {
            'project_id': project_id, 'shots': results,
            'multiframe_prompt': multiframe_prompt,
            'total_duration': script_result.get('total_duration', 0),
        })

    return Response(
        stream_with_context(event_stream()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


# ═══════════════════════════════════════════════════════
# 单镜重绘 (v11: 支持 reference_mode 传递)
# ═══════════════════════════════════════════════════════
@storyboard_bp.route('/regen_shot', methods=['POST'])
def regen_single_shot():
    data = request.get_json()
    project_id = data.get('project_id')
    shot_index = data.get('shot_index', 0)
    custom_prompt = data.get('custom_prompt', '')
    custom_action = data.get('custom_action', '')
    front_shot = data.get('shot', None)
    style = data.get('style', '日漫')
    scene_spec = data.get('scene_spec', '')
    scene_spec_zh = data.get('scene_spec_zh', '')
    panorama_views = data.get('panorama_views', [])
    # v11: 重绘时默认使用 'off' (完全依赖文本, 避免重绘不变)
    # 用户可以显式指定 'guide' 或 'strong' 来保持一致性
    reference_mode = data.get('reference_mode', 'off')

    project = STORYBOARD_DB.get(project_id)
    if front_shot:
        shot = dict(front_shot)
        if project and 0 <= shot_index < len(project.get('shots', [])):
            mem_shot = project['shots'][shot_index]
            for k in ['image_url']:
                if k not in shot or not shot[k]:
                    shot[k] = mem_shot.get(k, '')
    elif project and 0 <= shot_index < len(project.get('shots', [])):
        shot = dict(project['shots'][shot_index])
    else:
        return jsonify({'success': False, 'message': '找不到分镜数据'}), 404

    if project:
        if not style:
            style = project.get('style', '日漫')
        if not scene_spec:
            scene_spec = project.get('scene_spec', '')
        if not scene_spec_zh:
            scene_spec_zh = project.get('scene_spec_zh', '')
        if not panorama_views:
            panorama_views = project.get('panorama_views', [])
    global_tone = (project or {}).get('global_tone', '')

    front_chars = data.get('characters', [])
    if front_chars and isinstance(front_chars[0], dict) and 'description' in front_chars[0]:
        chars_data = front_chars
    elif project:
        chars_data = project.get('characters', [])
    else:
        chars_data = _load_chars(shot.get('characters_in_shot', []))
    char_refs = _get_char_refs(shot, chars_data)

    if char_refs and not any((cr.get('views') or {}).get('front') or cr.get('image_url')
                              for cr in char_refs):
        fresh = _load_chars([cr.get('name') for cr in char_refs])
        if fresh:
            char_refs = fresh

    # 用户修正
    action_changed = False
    if custom_action:
        shot['action_zh'] = custom_action
        if re.search(r'[\u4e00-\u9fff]', custom_action):
            try:
                from services.llm_service import translate_to_nai_prompt
                r = translate_to_nai_prompt(custom_action, "动作描述")
                if r.get('success'):
                    shot['action'] = re.sub(r'[\u4e00-\u9fff]+', '',
                                            r['content']).strip('" \n')
                else:
                    shot['action'] = custom_action
            except Exception:
                shot['action'] = custom_action
        else:
            shot['action'] = custom_action
        action_changed = True
        print(f"[regen] action_zh: {custom_action[:60]}")

    if custom_prompt:
        shot['scene_description'] = custom_prompt
        action_changed = True
        print(f"[regen] scene_description: {custom_prompt[:60]}")

    # v11: 用户改了描述, 清空旧 prompt + 强制新随机种子
    if action_changed:
        shot.pop('jimeng_ref_prompt', None)
        shot.pop('jimeng_trans_prompt', None)
        # ★ 重绘时把该 shot 标记为场景切换, 避免参考上一镜
        if custom_prompt:
            shot['scene_change'] = True
        print(f"[regen] ✓ 已清空旧 prompt, 将重新构建 (reference_mode={reference_mode})")

    no_characters = data.get('no_characters', False)
    if no_characters:
        shot['characters_in_shot'] = []
        char_refs = []

    import random
    regen_project_id = f"{project_id or 'demo'}_regen_{random.randint(1000, 9999)}"
    prev_shot_ref = data.get('prev_shot_ref', '')
    engine = data.get('engine', 'doubao')
    quality = data.get('quality', '16:9')

    print(f"\n[regen] ── 修正重绘 SHOT {shot.get('index','?')} ─────")
    print(f"[regen] action_zh: {shot.get('action_zh','')[:60]}")
    print(f"[regen] engine={engine}, quality={quality}, "
          f"reference_mode={reference_mode}")

    img_result = generate_storyboard_image(
        shot=shot, char_refs=char_refs, art_style=style,
        scene_spec=scene_spec, scene_spec_zh=scene_spec_zh,
        global_tone=global_tone,
        scene_views=panorama_views, panorama_views=panorama_views,
        all_chars=chars_data, project_id=regen_project_id,
        prev_shot_image=prev_shot_ref if prev_shot_ref else None,
        engine=engine, quality=quality,
        reference_mode=reference_mode,   # v11
        is_first_shot=(shot_index == 0),  # v11
    )
    if img_result.get('success'):
        shot['image_url'] = img_result['image_url']
        if project and 0 <= shot_index < len(project.get('shots', [])):
            project['shots'][shot_index] = shot
            _save_projects_db()  # ★ v12
        return jsonify({'success': True, 'image_url': img_result['image_url']})
    return jsonify({'success': False,
                    'message': img_result.get('message', '生成失败')}), 500


# ═══════════════════════════════════════════════════════
# 其他路由 (保留)
# ═══════════════════════════════════════════════════════
@storyboard_bp.route('/delete_shot', methods=['POST'])
def delete_shot():
    data = request.get_json()
    project_id = data.get('project_id')
    shot_index = data.get('shot_index', 0)

    project = STORYBOARD_DB.get(project_id)
    if not project:
        return jsonify(success=True)

    shots = project.get('shots', [])
    if 0 <= shot_index < len(shots):
        shots.pop(shot_index)
        for i, s in enumerate(shots):
            s['index'] = i + 1
        project['shots'] = shots
        _save_projects_db()  # ★ v12

    return jsonify(success=True, total=len(shots))


@storyboard_bp.route('/upload_shot_image', methods=['POST'])
def upload_shot_image():
    try:
        f = request.files.get('image')
        if not f:
            return jsonify(success=False, message='没有图片文件')

        shot_index = request.form.get('shot_index', '0')
        save_dir = os.path.join('static', 'shots')
        os.makedirs(save_dir, exist_ok=True)

        ext = os.path.splitext(f.filename)[1] or '.png'
        fname = f"upload_{shot_index}_{uuid.uuid4().hex[:8]}{ext}"
        fpath = os.path.join(save_dir, fname)
        f.save(fpath)

        image_url = f'/static/shots/{fname}'
        return jsonify(success=True, image_url=image_url)
    except Exception as e:
        return jsonify(success=False, message=str(e))


@storyboard_bp.route('/continue', methods=['POST'])
def continue_storyboard():
    data = request.get_json()
    story_text = data.get('story_text', '').strip()
    if not story_text:
        return jsonify({'success': False, 'message': '剧情文本不能为空'}), 400
    result = continue_story(story_text, data.get('characters', []),
                             data.get('direction', '自然发展'))
    return jsonify(result)


@storyboard_bp.route('/gen_panorama', methods=['POST'])
def gen_panorama():
    from services.panorama_service import generate_equirect_panorama, convert_panorama_to_views
    data = request.get_json()
    global_scene = data.get('global_scene', '').strip()
    style = data.get('style', '日漫')
    mode = data.get('mode', 'quad')
    engine = data.get('engine', 'doubao')  # v11: 允许前端选引擎

    if not global_scene:
        return jsonify({'success': False, 'message': '请填写全局场景'}), 400

    try:
        r = generate_scene_spec(global_scene, global_scene, style)
        scene_spec = (r.get('scene_spec_en') or r.get('scene_spec', global_scene)) \
                        if r.get('success') else global_scene
    except Exception:
        scene_spec = global_scene

    pano = generate_equirect_panorama(scene_spec, style,
                                        raw_scene=global_scene,
                                        engine=engine)
    if not pano['success']:
        return jsonify({'success': False,
                        'message': pano.get('message', '全景图生成失败')}), 500

    views_result = convert_panorama_to_views(pano['local_path'], mode=mode)
    return jsonify({
        'success': True,
        'panorama_url': pano['url'],
        'panorama_local': pano['local_path'],
        'scene_views': views_result.get('views', []),
        'scene_spec': scene_spec,
    })


@storyboard_bp.route('/project/<project_id>', methods=['GET'])
def get_project(project_id):
    p = STORYBOARD_DB.get(project_id)
    if p:
        return jsonify({'success': True, 'project': p})
    return jsonify({'success': False, 'message': '项目不存在'}), 404


@storyboard_bp.route('/projects', methods=['GET'])
def list_projects():
    projects = [{k: v for k, v in p.items() if k != 'shots'}
                for p in STORYBOARD_DB.values()]
    projects.sort(key=lambda x: x.get('created_at', 0), reverse=True)
    return jsonify({'success': True, 'projects': projects})