"""
routes/story_route.py — 剧本生成路由
=====================================
用户选择方向 → 豆包生成剧本 + 角色 + 场景
"""
from flask import Blueprint, request, jsonify
from services.llm_service import generate_story

story_bp = Blueprint('story', __name__)


@story_bp.route('/generate', methods=['POST'])
def api_generate_story():
    """生成完整剧本 (含角色和场景)"""
    data = request.get_json(force=True)
    direction = data.get('direction', '')
    genre = data.get('genre', '校园')
    length = data.get('length', '中篇')
    custom = data.get('custom_requirements', '')

    if not direction and not custom:
        return jsonify(success=False, message='请输入创作方向或自定义要求')

    result = generate_story(direction, genre, length, custom)
    if result.get('success'):
        return jsonify(success=True, story=result['story'])
    return jsonify(success=False, message=result.get('message', '生成失败'))