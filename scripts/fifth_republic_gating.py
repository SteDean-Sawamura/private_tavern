"""门控关系优化：确保时间锚定节点使用双门控（requires + event），
补充事件间的 fire_events/activate_events 连接。"""

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
    ot_events = script.get("one_time_events", [])
    dy_events = script.get("dynamic_events", [])
    changes = 0

    # ═══════════════════════════════════════════════════════════════
    # 1. 时间锚定节点：移除 on_complete_unlock 强制事件门控
    #    设计原则：历史上有确定发生时间的事件（暗杀10/26、政变12/12）
    #    不应仅靠树内流转就提前触发，必须等待事件总线信号
    # ═══════════════════════════════════════════════════════════════

    # 1a. yc_assassination_decision 必须等 assassination_imminent 事件
    #     移除所有父节点对它的 on_complete_unlock
    for target_parent in ["yc_power_struggle", "yc_tape_incident", "yc_busan_escalation"]:
        node = _find_node(trees, target_parent)
        if node and "yc_assassination_decision" in node.get("on_complete_unlock", []):
            node["on_complete_unlock"].remove("yc_assassination_decision")
            changes += 1
            print(f"  GATE {target_parent}: 移除 on_complete_unlock → yc_assassination_decision")

    # 1b. nm_1212_coup 必须等 coup_imminent / event_1212_military_mutiny 事件
    node = _find_node(trees, "nm_1212_prelude")
    if node and "nm_1212_coup" in node.get("on_complete_unlock", []):
        node["on_complete_unlock"].remove("nm_1212_coup")
        changes += 1
        print("  GATE nm_1212_prelude: 移除 on_complete_unlock → nm_1212_coup")

    # ═══════════════════════════════════════════════════════════════
    # 2. 为重要一次性事件添加 fire_events（连接事件总线到故事树）
    #    原则：关键历史事件应向系统广播信号
    # ═══════════════════════════════════════════════════════════════

    ot_fire_additions = {
        # 暗杀序列：第一枪和击中总统都应广播 assassination_occurred
        "event_1026_first_shot": ["assassination_occurred"],
        "event_1026_park_shot": ["assassination_occurred"],
        # 12.12序列：对峙和逮捕应广播军事信号
        "event_1212_rok_army_standoff": ["military_crisis"],
        "event_1212_jang_tae_wan_arrest": ["coup_executed"],
        # 录音带交付：情报更新信号
        "event_1025_tape_delivery": ["intelligence_update"],
        # 国际反应：触发外交干预信号
        "event_1027_us_statement": ["us_intervention"],
        "event_japan_media_comment": ["us_intervention"],
    }

    for eid, new_fires in ot_fire_additions.items():
        evt = next((e for e in ot_events if e["id"] == eid), None)
        if evt:
            existing = evt.get("fire_events", [])
            added = []
            for f in new_fires:
                if f not in existing:
                    existing.append(f)
                    added.append(f)
            if added:
                evt["fire_events"] = existing
                changes += 1
                print(f"  FIRE {eid}: +{added}")

    # ═══════════════════════════════════════════════════════════════
    # 3. 为故事树节点补充 activate_events 备份路径
    #    原则：关键节点应有多种事件驱动解锁方式，避免死锁
    # ═══════════════════════════════════════════════════════════════

    node_activate_additions = {
        # nm_new_order：政变成功后的新秩序，需要军事接管信号
        "nm_new_order": ["military_takeover", "coup_executed"],
        # ds_confrontation：总罢工也应触发对峙
        "ds_confrontation": ["general_strike"],
        # nm_hanahoe_conspiracy：动态事件 de_garrison_defection 也涉及哈那会
        "nm_hanahoe_conspiracy": ["event.event_hanahoe_secret_plot"],
        # yc_aftermath_chaos：确保暗杀发生信号覆盖
        "yc_aftermath_chaos": ["assassination_occurred"],
    }

    for nid, new_activates in node_activate_additions.items():
        node = _find_node(trees, nid)
        if node:
            existing = node.get("activate_events", [])
            added = []
            for a in new_activates:
                if a not in existing:
                    existing.append(a)
                    added.append(a)
            if added:
                node["activate_events"] = existing
                changes += 1
                print(f"  ACTIVATE {nid}: +{added}")

    # ═══════════════════════════════════════════════════════════════
    # 4. 为动态事件添加 fire_events（连接到故事树事件总线）
    #    原则：达到关键阈值的动态事件应向故事树广播信号
    # ═══════════════════════════════════════════════════════════════

    dy_fire_additions = {
        # 反对派集会 → 抗议持续信号
        "de_opposition_rally": ["protest_ongoing"],
        # 劳工团结 → 总罢工信号
        "de_labor_solidarity": ["general_strike"],
        # 媒体抵抗 → 地下活动信号
        "de_media_resistance": ["underground_activity"],
        # 经济危机 → 政权危机信号
        "de_economic_crisis": ["regime_crisis"],
        # 市民路障 → 应同时触发 crackdown 可能性
        "de_citizen_barricade": ["crackdown_occurred"],
        # 军事对峙 → 军事危机已在其中 ✓, 但加 martial_law_declared
        "de_military_standoff": ["martial_law_declared"],
    }

    for eid, new_fires in dy_fire_additions.items():
        evt = next((e for e in dy_events if e["id"] == eid), None)
        if evt:
            existing = evt.get("fire_events", [])
            added = []
            for f in new_fires:
                if f not in existing:
                    existing.append(f)
                    added.append(f)
            if added:
                evt["fire_events"] = existing
                changes += 1
                print(f"  DY_FIRE {eid}: +{added}")

    # ═══════════════════════════════════════════════════════════════
    # 5. 验证并修复流转节点的 on_complete_unlock 完整性
    #    原则：纯流转节点（非时间锚定）应通过 on_complete_unlock 驱动下游
    # ═══════════════════════════════════════════════════════════════

    # nm_evidence_gathering 完成后除了现有目标，也应解锁 nm_arms_buildup
    # （已有 ✓：on_complete_unlock: ['nm_hanahoe_conspiracy', 'nm_arms_buildup']）

    # ds_underground_network → ds_public_demand (已有 ✓)
    # ds_first_protests → ds_underground_network, ds_martyrdom, ds_international_solidarity (已有 ✓)

    # yc_busan_crisis → 验证移除 assassination_decision 后仍有正确的 unlock
    node = _find_node(trees, "yc_busan_crisis")
    if node:
        ocu = node.get("on_complete_unlock", [])
        expected = {"yc_power_struggle", "yc_tape_incident", "yc_busan_escalation", "yc_informant_reports"}
        missing = expected - set(ocu)
        if missing:
            ocu.extend(missing)
            node["on_complete_unlock"] = ocu
            changes += 1
            print(f"  FLOW yc_busan_crisis: 补充 on_complete_unlock +{list(missing)}")

    # ═══════════════════════════════════════════════════════════════
    # 6. 新增：为缺少事件连接的 cyclic events 添加 fire_events
    # ═══════════════════════════════════════════════════════════════
    cy_events = script.get("cyclic_events", [])

    # nightly_curfew 应该 fire 一个信号表示戒严持续中
    evt = next((e for e in cy_events if e["id"] == "nightly_curfew"), None)
    if evt:
        existing = evt.get("fire_events", [])
        if "martial_law_declared" not in existing:
            existing.append("martial_law_declared")
            evt["fire_events"] = existing
            changes += 1
            print("  CY_FIRE nightly_curfew: +['martial_law_declared']")

    # pss_loyalty_drill → 维持 troops_loyalty 相关信号
    # 不需要 fire_events，它已经是纯循环效果

    # ═══════════════════════════════════════════════════════════════
    # 7. 补充：为 choice 节点确保 activate_events 中有足够的事件驱动路径
    #    尤其是被移除 on_complete_unlock 后依赖事件的节点
    # ═══════════════════════════════════════════════════════════════

    # yc_assassination_decision 确认 activate_events 覆盖足够
    node = _find_node(trees, "yc_assassination_decision")
    if node:
        ae = node.get("activate_events", [])
        needed = ["assassination_imminent", "assassination_occurred"]
        added = []
        for a in needed:
            if a not in ae:
                ae.append(a)
                added.append(a)
        if added:
            node["activate_events"] = ae
            changes += 1
            print(f"  ACTIVATE yc_assassination_decision: +{added}")

    # nm_1212_coup 确认 activate_events 覆盖足够
    node = _find_node(trees, "nm_1212_coup")
    if node:
        ae = node.get("activate_events", [])
        needed = ["coup_imminent", "coup_executed", "event.event_1212_military_mutiny",
                  "event.event_1212_hannam_raid"]
        added = []
        for a in needed:
            if a not in ae:
                ae.append(a)
                added.append(a)
        if added:
            node["activate_events"] = ae
            changes += 1
            print(f"  ACTIVATE nm_1212_coup: +{added}")

    # ═══════════════════════════════════════════════════════════════
    # 保存
    # ═══════════════════════════════════════════════════════════════
    script["story_tree"]["trees"] = trees
    script["one_time_events"] = ot_events
    script["dynamic_events"] = dy_events
    script["cyclic_events"] = cy_events

    conn.execute("UPDATE scripts SET content=? WHERE id=?",
                 (json.dumps(script, ensure_ascii=False), SCRIPT_ID))
    conn.commit()
    conn.close()
    print(f"\nDone. Applied {changes} changes.")


def _find_node(trees, node_id):
    for tree in trees:
        for node in tree.get("nodes", []):
            if node["id"] == node_id:
                return node
    return None


if __name__ == "__main__":
    main()
