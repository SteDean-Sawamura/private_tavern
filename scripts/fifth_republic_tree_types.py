"""优化故事树节点：将合适的 choice 节点改为 auto_resolve，
为 quest 节点添加自动完成条件，调整 timed 节点的合理性。"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tavern.db"
SCRIPT_ID = "fifth_republic_dawn"


def main():
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT content FROM scripts WHERE id=?", (SCRIPT_ID,)).fetchone()
    if not row:
        print("ERROR: Script not found.")
        return

    script = json.loads(row[0])
    trees = script["story_tree"]["trees"]
    changes = 0

    # ═══════════════════════════════════════════════
    # 1. nm_hanahoe_conspiracy → auto_resolve
    #    渗透: hanahoe_infiltration高 + cover好
    #    警告正统派: troops_loyalty高
    #    中立: 兜底
    # ═══════════════════════════════════════════════
    node = _find_node(trees, "nm_hanahoe_conspiracy")
    if node:
        node["auto_resolve"] = True
        for c in node.get("choices", []):
            if c["id"] == "infiltrate_hanahoe":
                c["condition"] = "script_variables.hanahoe_infiltration >= 40 AND script_variables.cover_integrity >= 50"
            elif c["id"] == "warn_orthodox":
                c["condition"] = "script_variables.troops_loyalty >= 50 AND script_variables.hanahoe_infiltration < 40"
            # stay_neutral_nm: no condition (fallback)
        changes += 1
        print("  FIX nm_hanahoe_conspiracy → auto_resolve")
        print("    infiltrate: hanahoe>=40 + cover>=50")
        print("    warn_orthodox: troops>=50 + hanahoe<40")
        print("    stay_neutral: fallback")

    # ═══════════════════════════════════════════════
    # 2. nm_1212_coup → auto_resolve
    #    站队新军部: coup_readiness高 + hanahoe_infiltration高
    #    抵抗政变: troops_loyalty高 + regime_stability存在
    #    逃跑: 兜底（cover低或两边都不站）
    # ═══════════════════════════════════════════════
    node = _find_node(trees, "nm_1212_coup")
    if node:
        node["auto_resolve"] = True
        for c in node.get("choices", []):
            if c["id"] == "side_with_coup":
                c["condition"] = "script_variables.hanahoe_infiltration >= 50 AND script_variables.coup_readiness >= 60"
            elif c["id"] == "resist_coup":
                c["condition"] = "script_variables.troops_loyalty >= 45 AND script_variables.regime_stability >= 15"
            # flee_1212: no condition (fallback)
        changes += 1
        print("  FIX nm_1212_coup → auto_resolve")
        print("    side_with_coup: hanahoe>=50 + coup>=60")
        print("    resist_coup: troops>=45 + regime>=15")
        print("    flee: fallback")

    # ═══════════════════════════════════════════════
    # 3. ds_public_demand → auto_resolve
    #    升级抗议: protest_intensity高 + student_network大
    #    借助国际压力: diplomatic_crisis高 + us_confidence存在
    #    寻求谈判: 兜底（温和路线）
    # ═══════════════════════════════════════════════
    node = _find_node(trees, "ds_public_demand")
    if node:
        node["auto_resolve"] = True
        for c in node.get("choices", []):
            if c["id"] == "escalate_protests":
                c["condition"] = "script_variables.protest_intensity >= 60 AND script_variables.student_network_size >= 25"
            elif c["id"] == "international_pressure":
                c["condition"] = "script_variables.diplomatic_crisis_level >= 2 AND script_variables.us_confidence >= 30"
            # negotiate_path: no condition (fallback)
        changes += 1
        print("  FIX ds_public_demand → auto_resolve")
        print("    escalate: protest>=60 + network>=25")
        print("    international: diplomatic>=2 + us>=30")
        print("    negotiate: fallback")

    # ═══════════════════════════════════════════════
    # 4. quest 节点添加自动完成条件
    #    yc_aftermath_chaos: 暗杀后混乱期，几回合后自动完成
    #    ds_underground_network: 地下网络建设阶段
    # ═══════════════════════════════════════════════

    # yc_aftermath_chaos → 改为 timed (暗杀后的过渡期自然经过2回合)
    node = _find_node(trees, "yc_aftermath_chaos")
    if node:
        node["type"] = "timed"
        node["duration_turns"] = 2
        changes += 1
        print("  FIX yc_aftermath_chaos: quest → timed (dur=2, 暗杀后过渡自动完成)")

    # ds_underground_network → 改为 timed + condition
    # 需要一定的学生网络规模才能完成
    node = _find_node(trees, "ds_underground_network")
    if node:
        node["type"] = "timed"
        node["duration_turns"] = 3
        node["condition"] = "script_variables.student_network_size >= 15"
        changes += 1
        print("  FIX ds_underground_network: quest → timed (dur=3, cond: network>=15)")

    # ═══════════════════════════════════════════════
    # 5. timed 节点合理性调整
    # ═══════════════════════════════════════════════

    # yc_power_struggle: dur=3 合理(暗杀前权力斗争经过3回合)
    # yc_busan_escalation: dur=2 + cond protest>=70 合理(加速路径)
    # nm_evidence_gathering: dur=4 偏长，改为3(搜查本部效率高)
    node = _find_node(trees, "nm_evidence_gathering")
    if node:
        node["duration_turns"] = 3
        changes += 1
        print("  FIX nm_evidence_gathering: dur 4→3 (搜查本部效率高)")

    # nm_arms_buildup: dur=3 + cond weapons>=30 合理
    # nm_1212_prelude: dur=2 合理(紧凑的政变前夜)
    # ds_confrontation: dur=5 偏长，首尔之春的对峙改为4回合
    node = _find_node(trees, "ds_confrontation")
    if node:
        node["duration_turns"] = 4
        changes += 1
        print("  FIX ds_confrontation: dur 5→4 (首尔之春对峙加速)")

    # ds_international_solidarity: dur=3 + cond diplomatic>=2 合理

    # ═══════════════════════════════════════════════
    # 6. periodic 节点检查
    # ═══════════════════════════════════════════════
    # yc_informant_reports: periodic, repeatable, cooldown=3, weight=15 — 合理
    # nm_loyalty_purge: periodic, repeatable, cooldown=5, weight=12 — 合理
    # 无需修改

    # ═══════════════════════════════════════════════
    # 7. 额外类型优化：为没有 activate_events 的关键节点补充
    # ═══════════════════════════════════════════════

    # nm_1212_coup 应该通过 coup_executed 事件也能激活（不仅依赖前置）
    node = _find_node(trees, "nm_1212_coup")
    if node:
        if not node.get("activate_events"):
            node["activate_events"] = []
        if "coup_imminent" not in node["activate_events"]:
            node["activate_events"].append("coup_imminent")
            changes += 1
            print("  FIX nm_1212_coup: 添加 activate_events=['coup_imminent']")

    # ds_martyrdom 应该能被 crackdown_occurred 激活
    node = _find_node(trees, "ds_martyrdom")
    if node:
        if not node.get("activate_events"):
            node["activate_events"] = []
        if "crackdown_occurred" not in node["activate_events"]:
            node["activate_events"].append("crackdown_occurred")
            changes += 1
            print("  FIX ds_martyrdom: 添加 activate_events 含 crackdown_occurred")

    # ═══════════════════════════════════════════════
    # 保存
    # ═══════════════════════════════════════════════
    script["story_tree"]["trees"] = trees
    conn.execute("UPDATE scripts SET content=? WHERE id=?", (json.dumps(script, ensure_ascii=False), SCRIPT_ID))
    conn.commit()
    conn.close()
    print(f"\nDone. Applied {changes} fixes.")


def _find_node(trees, node_id):
    for tree in trees:
        for node in tree.get("nodes", []):
            if node["id"] == node_id:
                return node
    return None


if __name__ == "__main__":
    main()
