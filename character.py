"""
character.py — 角色路由
修复：角色图片保存到本地，不依赖过期的 OSS 链接
"""
import os, json, uuid, time, requests
from flask import Blueprint, request, jsonify
from services.image_service import generate_character_views
from services.llm_service import extract_character_description

character_bp = Blueprint('character', __name__)
CHARACTER_DB = {}
DB_PATH      = os.path.join('uploads', 'characters.json')
CHAR_IMG_DIR = os.path.join('static', 'characters')
os.makedirs(CHAR_IMG_DIR, exist_ok=True)


def _save_db():
    os.makedirs('uploads', exist_ok=True)
    with open(DB_PATH, 'w', encoding='utf-8') as f:
        json.dump(CHARACTER_DB, f, ensure_ascii=False, indent=2)

def _load_db():
    if os.path.exists(DB_PATH):
        with open(DB_PATH, 'r', encoding='utf-8') as f:
            CHARACTER_DB.update(json.load(f))

_load_db()


def _localize_url(url: str, prefix: str = '') -> str:
    """
    把远程 URL 下载到本地 static/characters/，返回本地路径。
    如果已经是本地路径（/static/...）直接返回。
    """
    if not url:
        return url
    if url.startswith('/static/'):
        # 检查文件是否真实存在
        local = url.lstrip('/')
        if os.path.exists(local):
            return url
    if not url.startswith('http'):
        return url
    try:
        # ★ v12: trust_env=False 避免被误注入的代理环境变量影响
        with requests.Session() as s:
            s.trust_env = False
            r = s.get(url, timeout=15)
        if r.status_code == 200:
            fname = f"{prefix}_{uuid.uuid4().hex[:8]}.png"
            fpath = os.path.join(CHAR_IMG_DIR, fname)
            with open(fpath, 'wb') as f:
                f.write(r.content)
            return f'/static/characters/{fname}'
    except Exception as e:
        print(f'[character] 图片下载失败 ({url[:60]}...): {e}')
    return url  # 下载失败保留原 URL


def _localize_views(views: dict, char_name: str) -> dict:
    """把 views 里的所有 URL 都本地化"""
    result = {}
    for key, url in views.items():
        result[key] = _localize_url(url, prefix=f"{char_name}_{key}")
    return result


@character_bp.route('/list', methods=['GET'])
def list_characters():
    chars = sorted(CHARACTER_DB.values(), key=lambda x: x.get('created_at', 0), reverse=True)
    return jsonify({'success': True, 'characters': chars})


@character_bp.route('/create', methods=['POST'])
def create_character():
    data        = request.get_json()
    name        = data.get('name', '').strip()
    description = data.get('description', '').strip()
    personality = data.get('personality', '')
    voice_style = data.get('voice_style', 'longxiaochun')
    style       = data.get('style', '日漫')
    skip_image  = data.get('skip_image', False)  # ★ 跳过 NAI 生成，只保存角色数据

    if not name:
        return jsonify({'success': False, 'message': '角色名不能为空'}), 400
    if not description:
        return jsonify({'success': False, 'message': '外貌描述不能为空'}), 400

    char_id = str(uuid.uuid4())[:8]

    if skip_image:
        # ★ 只保存角色数据，不调用 NAI，不消耗 Anlas
        character = {
            'id': char_id, 'name': name, 'description': description,
            'personality': personality, 'voice_style': voice_style,
            'style': style, 'image_url': '', 'views': {},
            'created_at': int(time.time()),
        }
        CHARACTER_DB[char_id] = character
        _save_db()
        return jsonify({'success': True, 'character': character})

    result = generate_character_views(char_name=name, description=description, art_style=style)
    if not result['success']:
        return jsonify({'success': False, 'message': f'图像生成失败: {result.get("message","")}'}), 500

    image_url = _localize_url(result.get('image_url', ''), prefix=f"{name}_front")
    views_raw = result.get('views', {})
    views     = _localize_views(views_raw, name)
    if views.get('front'):
        image_url = views['front']

    character = {
        'id': char_id, 'name': name, 'description': description,
        'personality': personality, 'voice_style': voice_style,
        'style': style, 'image_url': image_url, 'views': views,
        'created_at': int(time.time()),
    }
    CHARACTER_DB[char_id] = character
    _save_db()
    return jsonify({'success': True, 'character': character})


@character_bp.route('/extract', methods=['POST'])
def extract_from_story():
    data = request.get_json()
    story_text = data.get('story_text', '').strip()
    if not story_text:
        return jsonify({'success': False, 'message': '剧本不能为空'}), 400
    result = extract_character_description(story_text)
    return jsonify(result)


@character_bp.route('/get/<char_id>', methods=['GET'])
def get_character(char_id):
    char = CHARACTER_DB.get(char_id)
    if char:
        return jsonify({'success': True, 'character': char})
    return jsonify({'success': False, 'message': '角色不存在'}), 404


@character_bp.route('/update/<char_id>', methods=['PUT'])
def update_character(char_id):
    if char_id not in CHARACTER_DB:
        return jsonify({'success': False, 'message': '角色不存在'}), 404
    data = request.get_json()
    for field in ['name', 'description', 'personality', 'voice_style']:
        if field in data:
            CHARACTER_DB[char_id][field] = data[field]
    _save_db()
    return jsonify({'success': True, 'character': CHARACTER_DB[char_id]})


@character_bp.route('/regen_image/<char_id>', methods=['POST'])
def regen_image(char_id):
    char = CHARACTER_DB.get(char_id)
    if not char:
        return jsonify({'success': False, 'message': '角色不存在'}), 404

    result = generate_character_views(char_name=char.get('name', char_id), description=char['description'], art_style=char.get('style', '日漫'))
    if not result['success']:
        return jsonify({'success': False, 'message': result.get('message', '')}), 500

    name      = char.get('name', char_id)
    image_url = _localize_url(result.get('image_url', ''), prefix=f"{name}_front")
    views     = _localize_views(result.get('views', {}), name)
    if views.get('front'):
        image_url = views['front']

    CHARACTER_DB[char_id]['image_url'] = image_url
    CHARACTER_DB[char_id]['views']     = views
    _save_db()
    return jsonify({'success': True, 'image_url': image_url, 'views': views})


@character_bp.route('/delete/<char_id>', methods=['DELETE'])
def delete_character(char_id):
    if char_id in CHARACTER_DB:
        del CHARACTER_DB[char_id]
        _save_db()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': '角色不存在'}), 404