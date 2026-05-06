import os
from flask import Flask, send_from_directory
from flask_cors import CORS
from config import Config
from routes.auth import auth_bp
from routes.character import character_bp
from routes.storyboard import storyboard_bp
from routes.video import video_bp
from routes.panorama_route import panorama_bp

app = Flask(__name__, static_folder='static', template_folder='templates')
app.config.from_object(Config)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB 上传限制 (即梦视频10-30MB)
CORS(app, supports_credentials=True)

# 注册蓝图
app.register_blueprint(auth_bp,       url_prefix='/api/auth')
app.register_blueprint(character_bp,  url_prefix='/api/character')
app.register_blueprint(storyboard_bp, url_prefix='/api/storyboard')
app.register_blueprint(video_bp,      url_prefix='/api/video')
app.register_blueprint(panorama_bp)

# 尝试注册剧本生成蓝图 (新功能)
try:
    from routes.story_route import story_bp
    app.register_blueprint(story_bp, url_prefix='/api/story')
except ImportError:
    print("[app] story_route 未找到, 跳过")

# 创建目录
for folder in [
    Config.UPLOAD_FOLDER, Config.CHARACTER_FOLDER,
    Config.STORYBOARD_FOLDER, Config.AUDIO_FOLDER,
    Config.VIDEO_FOLDER,
    os.path.join('static', 'panoramas'),
    os.path.join('static', 'shots'),
]:
    os.makedirs(folder, exist_ok=True)

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

if __name__ == '__main__':
    app.run(debug=True, port=5000)