from flask import Blueprint, request, jsonify, session

auth_bp = Blueprint('auth', __name__)

# 简单演示用账号（毕设不需要数据库，用内存即可）
DEMO_USERS = {
    'admin': 'admin123',
    'demo': 'demo123',
}


@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    username = data.get('username', '')
    password = data.get('password', '')

    if username in DEMO_USERS and DEMO_USERS[username] == password:
        session['user'] = username
        return jsonify({'success': True, 'username': username})

    return jsonify({'success': False, 'message': '用户名或密码错误'}), 401


@auth_bp.route('/guest', methods=['POST'])
def guest_login():
    session['user'] = 'guest'
    return jsonify({'success': True, 'username': 'guest'})


@auth_bp.route('/logout', methods=['POST'])
def logout():
    session.pop('user', None)
    return jsonify({'success': True})


@auth_bp.route('/status', methods=['GET'])
def status():
    user = session.get('user')
    if user:
        return jsonify({'logged_in': True, 'username': user})
    return jsonify({'logged_in': False})