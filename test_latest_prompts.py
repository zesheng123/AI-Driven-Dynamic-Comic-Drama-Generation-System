# test_latest_prompts.py
# 用法：
# 1) 把 llm_service_latest_story.py 放在本脚本同目录，或修改 LLM_FILE 路径
# 2) python test_latest_prompts.py
#
# 这个脚本不调用外部 API，只测试最新版“勇者 vs 巨龙”兜底分镜提示词。

import importlib.util
from pathlib import Path
import json

LLM_FILE = Path(__file__).with_name("llm_service.py")

spec = importlib.util.spec_from_file_location("llm_service_latest_story", LLM_FILE)
llm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(llm)

characters = [
    {
        "name": "少年勇者",
        "description": "18岁青年男性，棕色短发，蓝色眼睛，身穿蓝白圣骑士轻甲，深蓝短披风，手持金色十字护手银白圣剑。",
        "personality": "坚毅、勇敢、冷静"
    },
    {
        "name": "远古巨龙",
        "description": "黑曜石色巨型远古龙，红色发光眼，弯曲黑角，红黑翼膜巨大双翼，粗壮前肢与锋利龙爪。",
        "personality": "暴怒、压迫感强"
    }
]

story_text = """
少年勇者在黄昏火山焦土战场上面对远古巨龙。
巨龙压迫而来，随后重重拍地，勇者跳跃躲避。
巨龙怒吼，勇者摆出蓄力姿势。
勇者跳到高空蓄力准备挥砍。
勇者挥砍，天空出现巨大魔法阵，召唤魔法巨剑贯穿巨龙。
勇者落地，焦土上留下巨龙尸体。
"""

scene = "黄昏火山焦土战场，黑色焦土龟裂，熔岩裂缝暗红发光，远处黑烟与火山灰翻涌，橙红夕阳压低"

shots = llm._v47_force_hero_dragon_advantage_storyboard(
    shots=[],
    characters=characters,
    scene_spec=scene,
    style="日漫",
    story_text=story_text
)

print(f"生成镜头数: {len(shots)}")
print("=" * 100)

for s in shots:
    print(f"\n第{s['index']}镜 | {s.get('shot_role')} | {s.get('shot_type')} | {s.get('camera_angle')}")
    print("- action_zh:")
    print(s.get("action_zh", ""))
    print("- jimeng_ref_prompt:")
    print(s.get("jimeng_ref_prompt", ""))
    print("- video_prompt:")
    print(s.get("video_prompt", ""))
    print("-" * 100)

# 可选：保存 JSON 方便你贴到前端或调试
out = Path(__file__).with_name("latest_storyboard_prompts_preview.json")
out.write_text(json.dumps(shots, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n已保存: {out}")
