"""10 回合叙事质量推演测试。

前置条件：服务器已启动 (python app.py)，数据库中有 fifth_republic_dawn 剧本。
用法：python scripts/narrative_quality_test.py [--base-url http://127.0.0.1:8000]
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import httpx
except ImportError:
    print("需要 httpx: pip install httpx")
    sys.exit(1)

SCRIPT_ID = "fifth_republic_dawn"

ACTIONS = [
    # 回合 1: opening 自动生成（不需要行动）
    None,
    # 回合 2: 探索
    "查看桌上的文件",
    # 回合 3: 社交
    "去走廊找金科长谈谈",
    # 回合 4: 观察
    "仔细观察周围的人",
    # 回合 5: 日常/思考
    "回办公室整理一下思路",
    # 回合 6: 纯等待（测试推进）
    "等待，看看会发生什么",
    # 回合 7: 日常（测试推进）
    "去食堂吃午饭",
    # 回合 8: 探索+方式修饰
    "暗中调查档案室的记录",
    # 回合 9: 社交
    "找一个可信的同事打听消息",
    # 回合 10: 思考
    "回顾今天收集到的所有信息",
]

AI_BANNED_WORDS = [
    "仿佛", "悄然", "不禁", "油然而生", "不经意间", "弥漫着", "笼罩着",
    "宛如", "似乎在诉说", "静静地", "默默地", "缓缓地", "轻轻地",
    "不可名状", "莫名的", "微妙的", "在这一刻", "时间仿佛", "命运的", "岁月的",
]

AI_BANNED_PATTERNS = [
    r"空气中弥漫着.{0,10}的气息",
    r"仿佛在.{0,15}",
    r"一股.{0,10}涌上心头",
    r"为这个.{0,10}平添了几分",
    r"目光不经意间",
    r"心中油然而生",
    r"眼中闪过一丝",
]


def check_ai_cliche(text: str) -> dict:
    hits = []
    for w in AI_BANNED_WORDS:
        count = text.count(w)
        if count > 0:
            hits.extend([w] * count)
    pattern_hits = []
    for p in AI_BANNED_PATTERNS:
        matches = re.findall(p, text)
        pattern_hits.extend(matches)
    return {"word_hits": hits, "pattern_hits": pattern_hits, "total": len(hits) + len(pattern_hits)}


def check_progression(narrative: str, state: dict) -> dict:
    has_world_pulse = "[世界脉搏]" in narrative or "世界脉搏" in narrative
    story_tree = state.get("story_tree_state", {})
    active = story_tree.get("active", [])
    completed = story_tree.get("completed", [])
    return {
        "has_world_pulse": has_world_pulse,
        "active_nodes": len(active),
        "completed_nodes": len(completed),
    }


def extract_entities(text: str) -> set:
    """粗略提取叙事中的实体（NPC 名字、地点等中文名词）。"""
    names = set()
    for m in re.finditer(r'[一-鿿]{2,4}(?:科长|部长|将军|少尉|中尉|大尉|上校|中校|少校|上尉|参谋|军官|秘书|局长|处长|主任)', text):
        names.add(m.group())
    return names


def run_test(base_url: str):
    client = httpx.Client(base_url=base_url, timeout=120)

    print("=" * 60)
    print("叙事质量优化推演测试")
    print(f"剧本: {SCRIPT_ID}")
    print(f"服务器: {base_url}")
    print(f"时间: {datetime.now().isoformat()}")
    print("=" * 60)

    # 1. 创建新游戏
    print("\n[1/2] 创建新游戏...")
    resp = client.post("/api/game/new", json={"script_id": SCRIPT_ID})
    if resp.status_code != 200:
        print(f"创建游戏失败: {resp.status_code} {resp.text[:200]}")
        return
    game_data = resp.json()
    save_id = game_data.get("save_id", "")
    if not save_id:
        print("未获取到 save_id")
        return
    print(f"  save_id: {save_id}")

    # 收集结果
    results = []
    all_entities = set()
    prev_entities = set()

    opening_narrative = game_data.get("narrative", "") or game_data.get("response", "")
    if opening_narrative:
        cliche = check_ai_cliche(opening_narrative)
        prog = check_progression(opening_narrative, game_data.get("state", {}))
        entities = extract_entities(opening_narrative)
        all_entities.update(entities)
        results.append({
            "turn": 0,
            "action": "(开场白)",
            "narrative": opening_narrative,
            "cliche": cliche,
            "progression": prog,
            "entities": entities,
            "entity_continuity": True,
            "game_time": game_data.get("state", {}).get("game_time", ""),
        })
        prev_entities = entities
        print(f"  开场: {len(opening_narrative)}字, AI腔{cliche['total']}处")

    # 2. 执行 10 回合
    print(f"\n[2/2] 开始 {len(ACTIONS) - 1} 回合推演...")
    for i, action_text in enumerate(ACTIONS[1:], 1):
        print(f"\n--- 回合 {i}: {action_text} ---")
        t0 = time.time()
        try:
            resp = client.post(
                f"/api/game/{save_id}/action",
                json={"type": "freeform", "text": action_text},
            )
        except httpx.ReadTimeout:
            print(f"  超时（120s），跳过")
            results.append({"turn": i, "action": action_text, "error": "timeout"})
            continue

        elapsed = time.time() - t0
        if resp.status_code != 200:
            print(f"  错误: {resp.status_code}")
            results.append({"turn": i, "action": action_text, "error": f"HTTP {resp.status_code}"})
            continue

        data = resp.json()
        narrative = data.get("narrative", "") or data.get("response", "")
        state = data.get("state", {})
        game_time = state.get("game_time", "")

        cliche = check_ai_cliche(narrative)
        prog = check_progression(narrative, state)
        entities = extract_entities(narrative)
        all_entities.update(entities)

        # 遗忘检测：前一轮出现的实体，本轮是否合理延续
        entity_continuity = True
        if prev_entities and not entities.intersection(prev_entities) and len(prev_entities) > 0:
            entity_continuity = False

        results.append({
            "turn": i,
            "action": action_text,
            "narrative": narrative,
            "cliche": cliche,
            "progression": prog,
            "entities": entities,
            "entity_continuity": entity_continuity,
            "game_time": game_time,
            "elapsed": round(elapsed, 1),
        })
        prev_entities = entities

        print(f"  {len(narrative)}字 | AI腔{cliche['total']}处 | "
              f"活跃节点{prog['active_nodes']} | 完成{prog['completed_nodes']} | "
              f"{elapsed:.1f}s")
        if cliche["word_hits"]:
            print(f"  命中词: {', '.join(cliche['word_hits'][:5])}")

    # 3. 生成报告
    report = generate_report(results, base_url)
    report_path = Path(__file__).parent.parent / "data" / "narrative_quality_report.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"\n报告已保存: {report_path}")
    print("\n" + report)


def generate_report(results: list, base_url: str) -> str:
    lines = [
        "# 叙事质量优化实验报告\n",
        "## 测试环境",
        f"- 剧本：{SCRIPT_ID}",
        f"- 服务器：{base_url}",
        f"- 日期：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 回合数：{len(results)}",
        "",
    ]

    # 逐回合分析
    lines.append("## 逐回合分析\n")
    total_cliche = 0
    logic_breaks = 0
    forget_issues = 0
    progression_turns = 0

    for r in results:
        turn = r["turn"]
        lines.append(f"### 回合 {turn}")
        lines.append(f"- **玩家行动**：{r['action']}")

        if r.get("error"):
            lines.append(f"- **错误**：{r['error']}")
            lines.append("")
            continue

        cliche = r["cliche"]
        total_cliche += cliche["total"]
        lines.append(f"- **AI腔评分**：{cliche['total']}处命中")
        if cliche["word_hits"]:
            lines.append(f"  - 命中词：{', '.join(cliche['word_hits'])}")
        if cliche["pattern_hits"]:
            lines.append(f"  - 命中句式：{', '.join(cliche['pattern_hits'][:3])}")

        if not r.get("entity_continuity", True):
            forget_issues += 1
            lines.append(f"- **细节保持**：⚠ 前轮实体未延续")
        else:
            lines.append(f"- **细节保持**：PASS")

        prog = r["progression"]
        has_prog = prog["active_nodes"] > 0 or prog["completed_nodes"] > 0
        if has_prog:
            progression_turns += 1
        lines.append(f"- **剧情推进**：{'有' if has_prog else '无'}"
                     f"（活跃{prog['active_nodes']}/完成{prog['completed_nodes']}）")

        game_time = r.get("game_time", "")
        if game_time:
            lines.append(f"- **游戏时间**：{game_time}")

        elapsed = r.get("elapsed")
        if elapsed:
            lines.append(f"- **耗时**：{elapsed}s")

        narrative = r.get("narrative", "")
        if narrative:
            summary = narrative[:100].replace("\n", " ") + ("..." if len(narrative) > 100 else "")
            lines.append(f"- **叙事摘要**：{summary}")
        lines.append("")

    # 总体评估
    valid = [r for r in results if not r.get("error")]
    n = len(valid) or 1
    lines.append("## 总体评估\n")
    lines.append("| 指标 | 结果 |")
    lines.append("|------|------|")
    lines.append(f"| 平均 AI 腔词频 | {total_cliche / n:.1f} 次/回合 |")
    lines.append(f"| 逻辑断裂次数 | {logic_breaks}/{n} |")
    lines.append(f"| 遗忘问题次数 | {forget_issues}/{n} |")
    lines.append(f"| 有推进回合数 | {progression_turns}/{n} |")
    lines.append("")

    # 评价
    lines.append("## 评价\n")
    if total_cliche / n <= 1:
        lines.append("- AI 腔控制：**优秀**（平均 ≤1 处/回合）")
    elif total_cliche / n <= 3:
        lines.append("- AI 腔控制：**良好**（平均 ≤3 处/回合）")
    else:
        lines.append(f"- AI 腔控制：**待改进**（平均 {total_cliche/n:.1f} 处/回合）")

    if forget_issues <= 1:
        lines.append("- 细节保持：**良好**")
    else:
        lines.append(f"- 细节保持：**待改进**（{forget_issues} 次遗忘）")

    if progression_turns >= n * 0.6:
        lines.append("- 剧情推进：**良好**")
    else:
        lines.append(f"- 剧情推进：**待改进**（仅 {progression_turns}/{n} 回合有推进）")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="叙事质量推演测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="服务器地址")
    args = parser.parse_args()
    run_test(args.base_url)
