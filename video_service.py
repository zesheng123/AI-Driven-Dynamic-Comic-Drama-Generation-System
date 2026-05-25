"""
video_service.py — 分镜视频合成（v5 即梦工作台版）
==================================================
新增:
1. extract_last_frame: 从视频截取最后一帧（用于首尾帧模式）
2. compose_video: 支持用户上传视频 + Ken Burns 混合拼接
3. 所有输出统一 1280x720, H.264 编码
"""

import os, uuid, traceback, subprocess, shutil
from pathlib import Path

# ─── 静态资源目录 ───
_BASE_DIR   = Path(__file__).resolve().parent.parent
_STATIC_DIR = _BASE_DIR / "static"
_SHOTS_DIR  = _STATIC_DIR / "shots"
_VIDEO_DIR  = _STATIC_DIR / "videos"
_FRAMES_DIR = _STATIC_DIR / "frames"   # 截取的尾帧保存在这里
_TMP_DIR    = _STATIC_DIR / "tmp_clips"
for d in [_VIDEO_DIR, _FRAMES_DIR, _TMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)


import cv2
import numpy as np


# ═══════════════════════════════════════════════════════
# ★ ffmpeg 重编码 — 解决 Windows 上 OpenCV 无法输出
#   浏览器可播放的 H.264 问题
# ═══════════════════════════════════════════════════════
def _get_ffmpeg() -> str:
    """查找可用的 ffmpeg 可执行文件路径"""
    # 优先使用 imageio-ffmpeg 自带的静态版本（跨平台）
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    # 回退到系统 PATH
    for candidate in ['ffmpeg', 'ffmpeg.exe']:
        if shutil.which(candidate):
            return candidate
    return ''


_FFMPEG_EXE = _get_ffmpeg()


def _reencode_h264(src: str, dst: str) -> bool:
    """将任意编码的视频重编码为 H.264/AAC，输出浏览器可直接播放的 MP4。

    使用 -movflags +faststart 使视频元数据写到文件头，支持网页即时播放。
    Returns True 表示成功，False 则保留原文件。
    """
    if not _FFMPEG_EXE:
        print("[video] ⚠ 未找到 ffmpeg，跳过重编码（浏览器可能无法播放）")
        return False
    if not os.path.exists(src) or os.path.getsize(src) == 0:
        return False
    try:
        cmd = [
            _FFMPEG_EXE,
            '-i', src,
            '-vcodec', 'libx264',
            '-preset', 'fast',
            '-crf', '23',
            '-pix_fmt', 'yuv420p',   # 确保浏览器兼容
            '-movflags', '+faststart',
            '-an',                    # Ken Burns 无音轨，避免错误
            '-y', dst,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode == 0 and os.path.getsize(dst) > 0:
            print(f"[video] ✓ ffmpeg 重编码成功: {os.path.basename(dst)} "
                  f"({os.path.getsize(dst)//1024}KB)")
            return True
        else:
            stderr = result.stderr.decode(errors='replace')[-300:]
            print(f"[video] ✗ ffmpeg 失败: {stderr}")
            return False
    except Exception as e:
        print(f"[video] ✗ ffmpeg 异常: {e}")
        return False




# ═══════════════════════════════════════════════════════
# ★ 新增: 从视频截取最后一帧
# ═══════════════════════════════════════════════════════

def extract_last_frame(video_path: str) -> dict:
    """
    从视频文件中截取最后一帧,保存为 PNG。
    用于首尾帧模式:上一段视频的最后一帧 → 下一段视频的首帧。

    返回: {"ok": True, "frame_url": "/static/frames/xxx.png"}
    """
    base = str(_BASE_DIR)

    # 解析路径
    if video_path.startswith("/static/"):
        abs_path = os.path.join(base, video_path.lstrip("/"))
    elif video_path.startswith("static/"):
        abs_path = os.path.join(base, video_path)
    else:
        abs_path = video_path

    if not os.path.exists(abs_path):
        return {"ok": False, "error": f"视频文件不存在: {abs_path}"}

    try:
        cap = cv2.VideoCapture(abs_path)
        if not cap.isOpened():
            return {"ok": False, "error": "无法打开视频文件"}

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total <= 0:
            return {"ok": False, "error": "视频帧数为0"}

        # 跳到最后一帧
        cap.set(cv2.CAP_PROP_POS_FRAMES, total - 1)
        ret, frame = cap.read()
        cap.release()

        if not ret or frame is None:
            return {"ok": False, "error": "读取最后一帧失败"}

        # 统一尺寸 1280x720
        h, w = frame.shape[:2]
        if w != 1280 or h != 720:
            frame = cv2.resize(frame, (1280, 720), interpolation=cv2.INTER_LANCZOS4)

        # 保存
        fname = f"lastframe_{uuid.uuid4().hex[:8]}.png"
        save_path = str(_FRAMES_DIR / fname)
        cv2.imwrite(save_path, frame)

        frame_url = f"/static/frames/{fname}"
        print(f"[截帧] ✓ 从 {os.path.basename(abs_path)} 截取最后一帧 → {fname}")
        return {"ok": True, "frame_url": frame_url}

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ═══════════════════════════════════════════════════════
# Ken Burns：纯 cv2 实现，支持 10 种运镜方向
# ═══════════════════════════════════════════════════════

def _ken_burns_clip(image_path, duration, shot_index, out_dir, direction='zoom_in', fps=30):
    """生成带方向控制的 Ken Burns 运镜视频片段"""
    output_path = os.path.join(out_dir, f"kb_clip_{shot_index}.mp4")
    total_frames = int(duration * fps)

    img = cv2.imread(image_path)
    if img is None:
        raise FileNotFoundError(f"无法读取图片: {image_path}")

    h, w = img.shape[:2]
    out_w, out_h = 1280, 720

    max_zoom = 0.12
    margin = int(min(w, h) * max_zoom)

    directions = {
        'zoom_in':        ((0, 0, w, h), (margin, margin, w-2*margin, h-2*margin)),
        'zoom_out':       ((margin, margin, w-2*margin, h-2*margin), (0, 0, w, h)),
        'pan_left':       ((2*margin, 0, w-2*margin, h), (0, 0, w-2*margin, h)),
        'pan_right':      ((0, 0, w-2*margin, h), (2*margin, 0, w-2*margin, h)),
        'pan_up':         ((0, 2*margin, w, h-2*margin), (0, 0, w, h-2*margin)),
        'pan_down':       ((0, 0, w, h-2*margin), (0, 2*margin, w, h-2*margin)),
        'pan_left_up':    ((2*margin, 2*margin, w-2*margin, h-2*margin), (0, 0, w-2*margin, h-2*margin)),
        'pan_right_up':   ((0, 2*margin, w-2*margin, h-2*margin), (2*margin, 0, w-2*margin, h-2*margin)),
        'pan_left_down':  ((2*margin, 0, w-2*margin, h-2*margin), (0, 2*margin, w-2*margin, h-2*margin)),
        'pan_right_down': ((0, 0, w-2*margin, h-2*margin), (2*margin, 2*margin, w-2*margin, h-2*margin)),
    }
    start, end = directions.get(direction, directions['zoom_in'])

    # 尝试 H.264 编码
    fourcc_list = [
        cv2.VideoWriter_fourcc(*'avc1'),
        cv2.VideoWriter_fourcc(*'H264'),
        cv2.VideoWriter_fourcc(*'mp4v'),
    ]
    writer = None
    for fourcc in fourcc_list:
        w_test = cv2.VideoWriter(output_path, fourcc, fps, (out_w, out_h))
        if w_test.isOpened():
            writer = w_test
            break
        w_test.release()
    if writer is None:
        writer = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (out_w, out_h))

    for frame_i in range(total_frames):
        t = frame_i / max(total_frames - 1, 1)
        t = t * t * (3 - 2 * t)  # ease-in-out

        x = max(0, min(int(start[0] + (end[0]-start[0])*t), w-10))
        y = max(0, min(int(start[1] + (end[1]-start[1])*t), h-10))
        cw = max(10, min(int(start[2] + (end[2]-start[2])*t), w-x))
        ch = max(10, min(int(start[3] + (end[3]-start[3])*t), h-y))

        crop = img[y:y+ch, x:x+cw]
        frame = cv2.resize(crop, (out_w, out_h), interpolation=cv2.INTER_LANCZOS4)
        writer.write(frame)

    writer.release()
    print(f"  [KB] Shot {shot_index+1}: ✓ ({direction})")

    # ★ 重编码为浏览器可播放的 H.264
    h264_path = output_path.replace('.mp4', '_h264.mp4')
    if _reencode_h264(output_path, h264_path):
        os.replace(h264_path, output_path)

    return output_path


# ═══════════════════════════════════════════════════════
# 视频拼接：纯 cv2，统一 1280x720
# ═══════════════════════════════════════════════════════

def _concat_clips(clip_paths: list, output_path: str) -> bool:
    """用 cv2 拼接多个视频片段"""
    try:
        out_w, out_h = 1280, 720
        fps = 30

        fourcc_list = [
            cv2.VideoWriter_fourcc(*'avc1'),
            cv2.VideoWriter_fourcc(*'H264'),
            cv2.VideoWriter_fourcc(*'mp4v'),
        ]
        writer = None
        for fourcc in fourcc_list:
            w_test = cv2.VideoWriter(output_path, fourcc, fps, (out_w, out_h))
            if w_test.isOpened():
                writer = w_test
                break
            w_test.release()
        if writer is None:
            writer = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (out_w, out_h))
        if not writer.isOpened():
            return False

        for clip_path in clip_paths:
            if not os.path.exists(clip_path):
                continue
            cap = cv2.VideoCapture(clip_path)
            if not cap.isOpened():
                continue
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                fh, fw = frame.shape[:2]
                if fw != out_w or fh != out_h:
                    frame = cv2.resize(frame, (out_w, out_h))
                writer.write(frame)
            cap.release()

        writer.release()
        ok = os.path.exists(output_path) and os.path.getsize(output_path) > 0
        if ok:
            print(f"[拼接] ✓ {output_path} ({os.path.getsize(output_path)//1024}KB)")
            # ★ 重编码为浏览器可播放的 H.264
            h264_path = output_path.replace('.mp4', '_h264.mp4')
            if _reencode_h264(output_path, h264_path):
                os.replace(h264_path, output_path)
                print(f"[拼接] ✓ H.264 重编码完成 ({os.path.getsize(output_path)//1024}KB)")
        return ok
    except Exception as e:
        print(f"[拼接] 异常: {e}")
        traceback.print_exc()
        return False


# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════

def compose_video(shots, project_dir="", project_id=None, **kwargs):
    """
    合成最终视频。
    shots 中每个 shot 可带:
      - image_url:     分镜图路径
      - video_url:     用户从即梦上传的视频（优先使用）
      - kb_direction:  Ken Burns 运镜方向
      - duration:      时长(秒)
    """
    n = len(shots)
    if n == 0:
        return {"ok": False, "success": False, "error": "没有分镜数据"}

    print(f"\n[合成] ═══ 开始 ({n} 镜) ═══")

    base = str(_BASE_DIR)
    tmp_dir = str(_TMP_DIR)

    clip_files = []
    uploaded_count = 0
    kb_count = 0

    for i, shot in enumerate(shots):
        print(f"  ── Shot {i+1}/{n} ──")

        # ★ 优先使用用户上传的即梦视频
        user_video = shot.get("video_url", "")
        if user_video:
            if user_video.startswith("/static/"):
                abs_path = os.path.join(base, user_video.lstrip("/"))
            elif user_video.startswith("static/"):
                abs_path = os.path.join(base, user_video)
            else:
                abs_path = os.path.join(base, "static", "videos", "shots",
                                        os.path.basename(user_video))
            if os.path.exists(abs_path):
                clip_files.append(abs_path)
                uploaded_count += 1
                print(f"    ★ 即梦视频: {os.path.basename(abs_path)}")
                continue
            else:
                print(f"    ⚠ 视频不存在: {abs_path}")

        # Ken Burns 运镜
        img_url = shot.get("image_url", "")
        if not img_url:
            print(f"    ⚠ 无图片，跳过")
            continue

        if img_url.startswith("/static/"):
            img_path = os.path.join(base, img_url.lstrip("/"))
        elif img_url.startswith("static/"):
            img_path = os.path.join(base, img_url)
        else:
            img_path = os.path.join(base, "static", "shots", os.path.basename(img_url))

        if not os.path.exists(img_path):
            print(f"    ⚠ 图片不存在: {img_path}")
            continue

        dur = min(float(shot.get("duration") or shot.get("duration_hint") or 3), 5)
        direction = shot.get("kb_direction", "zoom_in")

        try:
            kb_file = _ken_burns_clip(img_path, duration=dur, shot_index=i,
                                       out_dir=tmp_dir, direction=direction)
            if kb_file and os.path.exists(kb_file):
                clip_files.append(kb_file)
                kb_count += 1
            else:
                print(f"    [KB] ✗ 生成失败")
        except Exception as e:
            print(f"    [KB] ✗ {e}")

    if not clip_files:
        return {"ok": False, "success": False, "error": "没有生成任何视频片段"}

    out_name = f"comic_{uuid.uuid4().hex[:8]}.mp4"
    out_path = os.path.join(str(_VIDEO_DIR), out_name)

    success = _concat_clips(clip_files, out_path)
    if not success:
        return {"ok": False, "success": False, "error": "视频拼接失败"}

    video_url = f"/static/videos/{out_name}"
    print(f"[合成] ✓ 完成 (即梦:{uploaded_count} KB:{kb_count})")

    return {
        "ok": True, "success": True,
        "video_url": video_url, "url": video_url, "video_path": video_url,
        "i2v_count": uploaded_count, "kb_count": kb_count,
    }