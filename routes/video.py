"""
routes/video.py — 视频生成路由 (v11 补齐版)
═══════════════════════════════════════════════════════
★ v11 修复 ★
  1. 补上 /seedance/create_full_task —— 整片合成(短剧情≤4镜)
  2. 补上 /seedance/create_batch_tasks —— 分批生成(长剧情>4镜)
  3. 原有路由全部保留

原 v10 内容 (保留):
  • Seedance 参数通过 API 字段传递
  • /seedance/web_prompts 生成网页版提示词
  • 支持 ratio 参数 (16:9/9:16/1:1)
  • 支持 1080p 分辨率
"""
import os
import uuid
import requests
from flask import Blueprint, request, jsonify
from services.tts_service import generate_voice, get_voices
from services.video_service import compose_video

video_bp = Blueprint('video', __name__)


# ═══════════════════════════════════════════════════════
# TTS 配音
# ═══════════════════════════════════════════════════════
@video_bp.route('/tts', methods=['POST'])
def text_to_speech():
    data = request.get_json()
    text = data.get('text', '').strip()
    voice = data.get('voice', 'zh-CN-XiaoxiaoNeural')
    speed = float(data.get('speed', 1.0))
    if not text:
        return jsonify({'success': False, 'message': '文本不能为空'}), 400
    # ★ v12.1: 前端可能直接传中文 voice_style（如"温柔甜美"），需先解析
    if voice and not voice.startswith('zh-') and voice not in (
        'longxiaochun','longwan','longshu','longcheng','longhua','longxiang','longjing','longmiao'
    ):
        try:
            from services.tts_service import resolve_character_voice
            voice = resolve_character_voice(voice)
        except Exception:
            pass
    return jsonify(generate_voice(text=text, voice=voice, speed=speed))


@video_bp.route('/tts_batch', methods=['POST'])
def tts_batch():
    """★ v12: 批量 TTS，优先使用 shot['voice']（已由分镜路由从角色 voice_style 解析）
    若 shot 没有 voice 字段，则尝试从 characters.json 查找角色音色。
    """
    data = request.get_json()
    shots = data.get('shots', [])

    # 懒加载角色 DB（仅在有 shot 缺少 voice 字段时用到）
    _char_voice_cache = {}
    def _get_char_voice(char_name):
        if char_name in _char_voice_cache:
            return _char_voice_cache[char_name]
        try:
            from services.tts_service import resolve_character_voice
            import json as _json
            db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                   'uploads', 'characters.json')
            if os.path.exists(db_path):
                with open(db_path, encoding='utf-8') as f:
                    db = _json.load(f)
                for c in db.values():
                    if c.get('name') == char_name:
                        cdesc = (c.get('description', '') or '').lower()
                        # ★ v12.1: 优先检测女性词，避免 'man' ⊂ 'woman' 子串误判
                        is_female = any(w in cdesc for w in ['female', 'girl', 'woman', 'lady', '女'])
                        is_male   = any(w in cdesc for w in ['male', 'boy', ' man ', 'gentleman', '男'])
                        gender = 'male' if (is_male and not is_female) else 'female'
                        v = resolve_character_voice(c.get('voice_style', ''), gender)
                        _char_voice_cache[char_name] = v
                        return v
        except Exception as e:
            print(f'[tts_batch] 查找角色音色失败: {e}')
        _char_voice_cache[char_name] = 'zh-CN-XiaoxiaoNeural'
        return 'zh-CN-XiaoxiaoNeural'

    results = []
    for shot in shots:
        dialogue = shot.get('dialogue', '').strip()
        # 优先使用分镜路由已解析好的 voice 字段
        voice = shot.get('voice', '')
        if not voice:
            # 尝试从台词角色名查找
            chars_in = shot.get('characters_in_shot', [])
            if chars_in:
                voice = _get_char_voice(chars_in[0])
            else:
                voice = 'zh-CN-XiaoxiaoNeural'
        if dialogue:
            r = generate_voice(text=dialogue, voice=voice)
            results.append({'dialogue': dialogue, **r})
        else:
            results.append({'dialogue': '', 'success': True, 'audio_url': ''})
    return jsonify({'success': True, 'results': results})


# ═══════════════════════════════════════════════════════
# 视频合成
# ═══════════════════════════════════════════════════════
@video_bp.route('/compose', methods=['POST'])
def compose():
    data = request.get_json()
    project_id = data.get('project_id', str(uuid.uuid4())[:8])
    shots = data.get('shots', [])
    if not shots:
        return jsonify({'success': False, 'message': '没有分镜数据'}), 400
    result = compose_video(project_id=project_id, shots=shots)
    return jsonify(result)


@video_bp.route('/voices', methods=['GET'])
def list_voices():
    return jsonify({'success': True, 'voices': get_voices()})


# ═══════════════════════════════════════════════════════
# 分镜视频上传/截帧
# ═══════════════════════════════════════════════════════
@video_bp.route('/upload_shot_video', methods=['POST'])
def upload_shot_video():
    """接收用户从网页版(即梦等)下载的分镜视频"""
    try:
        f = request.files.get('video')
        if not f:
            return jsonify(success=False, message='没有视频文件')

        shot_index = request.form.get('shot_index', '0')
        save_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                'static', 'videos', 'shots')
        os.makedirs(save_dir, exist_ok=True)

        ext = os.path.splitext(f.filename)[1] or '.mp4'
        fname = f"shot_video_{shot_index}_{uuid.uuid4().hex[:8]}{ext}"
        fpath = os.path.join(save_dir, fname)
        f.save(fpath)

        video_url = f'/static/videos/shots/{fname}'
        return jsonify(success=True, video_url=video_url)
    except Exception as e:
        return jsonify(success=False, message=str(e))


@video_bp.route('/extract_last_frame', methods=['POST'])
def extract_last_frame_route():
    from services.video_service import extract_last_frame
    data = request.get_json(force=True)
    video_url = data.get('video_url', '')
    if not video_url:
        return jsonify(ok=False, error='没有视频路径')
    return jsonify(extract_last_frame(video_url))


# ═══════════════════════════════════════════════════════
# Seedance 视频生成
# ═══════════════════════════════════════════════════════
def _seedance():
    """懒加载 seedance 服务"""
    import sys
    import importlib
    services_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'services'
    )
    if services_dir not in sys.path:
        sys.path.insert(0, services_dir)
    return importlib.import_module('video_service_seedance')


@video_bp.route('/seedance/calc_durations', methods=['POST'])
def seedance_calc_durations():
    """计算每个分镜的智能时长, 供前端预览"""
    svc = _seedance()
    shots = request.get_json().get('shots', [])
    result = []
    total = 0
    for s in shots:
        if s:
            d = svc.calc_shot_duration(s)
            result.append({'shot_index': s.get('index', 0), 'duration': d})
            total += d
    return jsonify(success=True, durations=result, total_duration=total)


@video_bp.route('/seedance/web_prompts', methods=['POST'])
def seedance_web_prompts():
    """生成适合网页版测试的提示词"""
    svc = _seedance()
    data = request.get_json()
    shots = data.get('shots', [])
    return jsonify(svc.generate_web_prompts(shots))


@video_bp.route('/seedance/create_per_shot_tasks', methods=['POST'])
def seedance_create_per_shot_tasks():
    """为每个分镜单独创建任务, 立即返回所有 task_id"""
    svc = _seedance()
    data = request.get_json()
    shots = data.get('shots', [])
    resolution = data.get('resolution', '720p')
    ratio = data.get('ratio', '16:9')
    # ★ 支持前端传入固定时长（秒），None 表示智能计算
    duration_raw = data.get('duration', None)
    duration = int(duration_raw) if duration_raw else None
    return jsonify(svc.create_per_shot_tasks(shots, resolution=resolution, ratio=ratio, duration=duration))


@video_bp.route('/seedance/generate_single', methods=['POST'])
def seedance_generate_single():
    """同步生成单个分镜 (阻塞, 用于重试)"""
    svc = _seedance()
    data = request.get_json()
    shot = data.get('shot', {})
    resolution = data.get('resolution', '720p')
    duration = data.get('duration', None)
    ratio = data.get('ratio', '16:9')
    return jsonify(svc.generate_single_shot_video(
        shot, resolution=resolution, duration=duration, ratio=ratio
    ))


@video_bp.route('/seedance/poll/<task_id>', methods=['GET'])
def seedance_poll(task_id):
    """查询单个任务状态"""
    svc = _seedance()
    return jsonify(svc.poll_task(task_id))


@video_bp.route('/seedance/download', methods=['POST'])
def seedance_download():
    """下载远程视频到本地"""
    svc = _seedance()
    data = request.get_json()
    remote_url = data.get('video_url', '')
    name_hint = data.get('name_hint', 'shot')
    if not remote_url:
        return jsonify(success=False, message='无视频URL')
    try:
        # ★ v12: trust_env=False 避免被误注入的代理环境变量影响
        with requests.Session() as s:
            s.trust_env = False
            r = s.get(remote_url, timeout=120)
        local_url = svc._save_video(r.content, name_hint)
        return jsonify(success=True, video_url=local_url)
    except Exception as e:
        return jsonify(success=False, message=str(e))


# ═══════════════════════════════════════════════════════
# ★★ v11 新增: 整片合成 & 分批合成 (修复前端 404) ★★
# ═══════════════════════════════════════════════════════
@video_bp.route('/seedance/create_full_task', methods=['POST'])
def seedance_create_full_task():
    """★ v11 新增: 短剧情(≤4镜)一次性合成整片视频

    前端调用: POST /api/video/seedance/create_full_task
    Body: { shots: [...], duration: 5, resolution: '720p', ratio: '16:9' }
    Response: { success: true, task_id: '...' } 或 { success: false, message: '' }

    返回后前端会自己轮询 /seedance/poll/<task_id>
    """
    svc = _seedance()
    data = request.get_json()
    shots = data.get('shots', [])
    duration = data.get('duration', None)
    resolution = data.get('resolution', '720p')
    ratio = data.get('ratio', '16:9')

    if not shots:
        return jsonify(success=False, message='没有分镜数据'), 400

    return jsonify(svc.create_full_task(
        shots,
        duration=duration,
        resolution=resolution,
        ratio=ratio,
    ))


@video_bp.route('/seedance/create_batch_tasks', methods=['POST'])
def seedance_create_batch_tasks():
    """★ v11 新增: 长剧情(>4镜)分批合成

    前端每 4 镜分为一批, 并行提交, 并行轮询。
    Body: { shots: [...], duration: 5, resolution: '720p', ratio: '16:9',
            batch_size: 4 }
    Response: {
        success: true,
        total_batches: N,
        batches: [
            { batch: 1, shots: [1,2,3,4], success: true, task_id: '...' },
            ...
        ]
    }
    """
    svc = _seedance()
    data = request.get_json()
    shots = data.get('shots', [])
    duration = data.get('duration', None)
    resolution = data.get('resolution', '720p')
    ratio = data.get('ratio', '16:9')
    batch_size = int(data.get('batch_size', 4))

    if not shots:
        return jsonify(success=False, message='没有分镜数据'), 400

    return jsonify(svc.create_batch_tasks(
        shots,
        batch_size=batch_size,
        duration=duration,
        resolution=resolution,
        ratio=ratio,
    ))