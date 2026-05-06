"""
tts_service.py — 语音合成服务（v7 清洁版）
=============================================
方案: 使用 edge-tts (微软免费 TTS) 作为默认方案
      无需 API key, 支持多种中文音色, 质量够用

如需更高质量, 可接入火山引擎豆包语音合成 (需另外申请 appid/token)
"""
import os, uuid, asyncio

# 可用音色列表 (edge-tts 中文音色)
VOICES = [
    {'id': 'zh-CN-XiaoxiaoNeural',   'name': '晓晓',  'gender': 'female', 'desc': '温柔甜美'},
    {'id': 'zh-CN-XiaoyiNeural',     'name': '晓依',  'gender': 'female', 'desc': '活泼可爱'},
    {'id': 'zh-CN-YunjianNeural',    'name': '云健',  'gender': 'male',   'desc': '浑厚磁性'},
    {'id': 'zh-CN-YunxiNeural',      'name': '云希',  'gender': 'male',   'desc': '年轻阳光'},
    {'id': 'zh-CN-YunxiaNeural',     'name': '云夏',  'gender': 'male',   'desc': '少年清亮'},
    {'id': 'zh-CN-XiaochenNeural',   'name': '晓辰',  'gender': 'female', 'desc': '知性优雅'},
    {'id': 'zh-CN-XiaohanNeural',    'name': '晓涵',  'gender': 'female', 'desc': '冷静干练'},
    {'id': 'zh-CN-XiaomoNeural',     'name': '晓墨',  'gender': 'female', 'desc': '清新自然'},
]

# 旧音色 → 新音色映射 (兼容已保存的角色数据)
_VOICE_COMPAT = {
    'longxiaochun': 'zh-CN-XiaoxiaoNeural',
    'longwan':      'zh-CN-XiaochenNeural',
    'longshu':      'zh-CN-XiaoyiNeural',
    'longcheng':    'zh-CN-YunjianNeural',
    'longhua':      'zh-CN-YunxiNeural',
    'longxiang':    'zh-CN-YunxiaNeural',
    'longjing':     'zh-CN-XiaohanNeural',
    'longmiao':     'zh-CN-XiaomoNeural',
}

# ★ v12 新增: 角色 voice_style 中文描述 → edge-tts 音色
# 按关键词匹配，先长后短（避免子串冲突）
_STYLE_KEYWORD_MAP = [
    # 女声
    ('温柔甜美', 'zh-CN-XiaoxiaoNeural'),
    ('温柔',     'zh-CN-XiaoxiaoNeural'),
    ('甜美',     'zh-CN-XiaoxiaoNeural'),
    ('柔和',     'zh-CN-XiaoxiaoNeural'),
    ('娇软',     'zh-CN-XiaoxiaoNeural'),
    ('活泼开朗', 'zh-CN-XiaoyiNeural'),
    ('活泼',     'zh-CN-XiaoyiNeural'),
    ('开朗',     'zh-CN-XiaoyiNeural'),
    ('可爱',     'zh-CN-XiaoyiNeural'),
    ('俏皮',     'zh-CN-XiaoyiNeural'),
    ('知性优雅', 'zh-CN-XiaochenNeural'),
    ('知性',     'zh-CN-XiaochenNeural'),
    ('优雅',     'zh-CN-XiaochenNeural'),
    ('成熟女',   'zh-CN-XiaochenNeural'),
    ('冷静干练', 'zh-CN-XiaohanNeural'),
    ('冷静',     'zh-CN-XiaohanNeural'),
    ('干练',     'zh-CN-XiaohanNeural'),
    ('冷淡',     'zh-CN-XiaohanNeural'),
    ('冷酷',     'zh-CN-XiaohanNeural'),
    ('清新',     'zh-CN-XiaomoNeural'),
    ('自然',     'zh-CN-XiaomoNeural'),
    ('随和',     'zh-CN-XiaomoNeural'),
    # 男声
    ('浑厚磁性', 'zh-CN-YunjianNeural'),
    ('浑厚',     'zh-CN-YunjianNeural'),
    ('磁性',     'zh-CN-YunjianNeural'),
    ('低沉',     'zh-CN-YunjianNeural'),
    ('沉稳',     'zh-CN-YunjianNeural'),
    ('成熟稳重', 'zh-CN-YunjianNeural'),
    ('稳重',     'zh-CN-YunjianNeural'),
    ('成熟',     'zh-CN-YunjianNeural'),
    ('年轻阳光', 'zh-CN-YunxiNeural'),
    ('阳光',     'zh-CN-YunxiNeural'),
    ('活力',     'zh-CN-YunxiNeural'),
    ('开朗男',   'zh-CN-YunxiNeural'),
    ('少年清亮', 'zh-CN-YunxiaNeural'),
    ('清亮',     'zh-CN-YunxiaNeural'),
    ('少年',     'zh-CN-YunxiaNeural'),
    ('稚嫩',     'zh-CN-YunxiaNeural'),
]


def _resolve_voice(voice_id):
    """兼容旧的 CosyVoice 音色 ID"""
    if voice_id in _VOICE_COMPAT:
        return _VOICE_COMPAT[voice_id]
    # 如果已经是 edge-tts 格式, 直接返回
    if voice_id.startswith('zh-'):
        return voice_id
    # 默认
    return 'zh-CN-XiaoxiaoNeural'


def resolve_character_voice(voice_style: str, gender: str = '') -> str:
    """★ v12 新增: 根据角色 voice_style 中文描述 + 性别推断合适的 edge-tts 音色

    优先级:
      1. 已知音色 ID (兼容旧格式)
      2. edge-tts 格式直接返回
      3. 关键词匹配 _STYLE_KEYWORD_MAP
      4. 按性别降级: 男→云希, 女→晓晓

    Args:
        voice_style: 角色的 voice_style 字段 (如"温柔甜美""沉稳低沉")
        gender: 'male'/'female' 或空字符串
    """
    if not voice_style:
        return ('zh-CN-YunxiNeural' if gender == 'male'
                else 'zh-CN-XiaoxiaoNeural')

    # 已知 ID 直接解析
    resolved = _resolve_voice(voice_style)
    if resolved != 'zh-CN-XiaoxiaoNeural' or voice_style.startswith('zh-'):
        return resolved

    # 关键词匹配 (按列表顺序, 已按长度/优先级排好)
    for kw, voice_id in _STYLE_KEYWORD_MAP:
        if kw in voice_style:
            return voice_id

    # 性别兜底
    if gender == 'male':
        return 'zh-CN-YunxiNeural'
    return 'zh-CN-XiaoxiaoNeural'




async def _generate_edge_tts(text, voice, filepath):
    """使用 edge-tts 生成语音"""
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(filepath)


def generate_voice(text: str, voice: str = 'zh-CN-XiaoxiaoNeural', speed: float = 1.0) -> dict:
    """生成语音, 返回 {'success': True, 'audio_url': '/static/audio/xxx.mp3'}"""
    audio_dir = os.path.join('static', 'audio')
    os.makedirs(audio_dir, exist_ok=True)

    filename = f"{uuid.uuid4().hex[:10]}.mp3"
    filepath = os.path.join(audio_dir, filename)

    resolved_voice = _resolve_voice(voice)

    try:
        # edge-tts 是异步的, 用 asyncio 包一层
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_generate_edge_tts(text, resolved_voice, filepath))
        loop.close()

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            return {
                'success': True,
                'audio_url': f'/static/audio/{filename}',
                'filename': filename,
                'voice': resolved_voice,
                'text_length': len(text),
            }
        return {'success': False, 'message': '语音文件生成为空'}

    except ImportError:
        return {'success': False,
                'message': 'edge-tts 未安装, 请运行: pip install edge-tts'}
    except Exception as e:
        return {'success': False, 'message': f'TTS 生成失败: {str(e)}'}


def get_voices():
    return VOICES