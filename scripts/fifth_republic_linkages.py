"""第五共和国剧本：事件系统 ↔ 剧情树关联补充脚本。"""

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tavern.db"
SCRIPT_ID = "fifth_republic_dawn"


# ═══════════════════════════════════════════════
# 1. Story Tree 节点添加 activate_events
#    使节点可被一次性/周期事件 event-driven unlock
# ═══════════════════════════════════════════════
STORY_TREE_ACTIVATE_EVENTS = {
    # ── yushin_collapse ──
    "yc_busan_crisis": [
        "busan_uprising_started",              # 一次性事件 fire_events 触发
        "protest_ongoing",                     # 周期事件持续抗议
    ],
    "yc_power_struggle": [
        "event.event_1020_tape_viewing",       # 录像观看揭示权力斗争
        "assassination_imminent",              # 暗杀临近引发权力博弈
    ],
    "yc_tape_incident": [
        "event.event_1025_tape_delivery",      # 录音带送达即为此事件
        "intelligence_update",                 # 周期情报简报触发
    ],
    "yc_assassination_decision": [
        "event.event_1026_assassination_conspiracy",  # 暗杀阴谋启动
        "event.event_1026_dinner_invitation",         # 宴会邀请——暗杀即将发生
        "assassination_imminent",                     # 语义事件
    ],
    "yc_aftermath_chaos": [
        "event.event_1026_incident",           # 10.26事变直接触发善后
        "event.event_1026_first_shot",         # 第一枪即触发
        "assassination_occurred",              # 跨事件语义触发
    ],
    "yc_busan_escalation": [
        "event.event_1018_busan_martial_law",  # 釜山戒严引发升级
        "event.event_masan_protest_spread",    # 马山扩散引发升级
        "busan_uprising_started",              # 釜山起义事件
        "busan_crisis_started",                # 釜山危机启动
        "campus_unrest",                       # 校园动荡蔓延
    ],
    "yc_informant_reports": [
        "event.event_1026_chun_recommendation",  # 车智澈推荐全斗焕后情报活跃
        "regime_crisis",                         # 政权危机时线人活跃
        "intelligence_update",                   # 周期情报简报关联
        "identity_compromised",                  # 身份暴露后线人更紧张
    ],

    # ── new_military_rise ──
    "nm_investigation_power": [
        "event.event_chun_joint_investigation",  # 合同搜查授予调查权
        "event.event_1026_arrest_kim_jae_gyu",   # 金载圭被捕后保安司令部接权
        "regime_crisis",                         # 政权危机打开新军部机会窗口
        "kcia_collapsed",                        # 中情部瘫痪 → 保安司令部填补
        "investigation_authority_granted",       # 调查权获授
    ],
    "nm_evidence_gathering": [
        "event.event_chun_joint_investigation",  # 调查过程同时收集证据
        "investigation_authority_granted",       # 调查权获授
        "intelligence_update",                   # 情报简报带来证据
    ],
    "nm_hanahoe_conspiracy": [
        "event.event_hanahoe_secret_plot",       # 一心会密谋事件直接对应
        "hanahoe_activated",                     # 一心会激活
        "hanahoe_activity",                      # 周期：一心会聚会
    ],
    "nm_1212_prelude": [
        "event.event_1212_hannam_raid",          # 汉南洞突袭即为前奏
        "coup_imminent",                         # 政变临近信号
        "hanahoe_activated",                     # 一心会激活加速前奏
    ],
    "nm_1212_coup": [
        "event.event_1212_military_mutiny",      # 双十二兵变即为此节点
        "coup_executed",                         # 跨线：政变执行
        "military_crisis",                       # 军事危机触发
    ],
    "nm_loyalty_purge": [
        "event.event_chun_joint_investigation",  # 调查权限使清洗成为可能
        "coup_imminent",                         # 政变前加速清洗
        "hanahoe_activity",                      # 一心会活动触发清洗
    ],
    "nm_arms_buildup": [
        "coup_imminent",                         # 政变临近加速武器集结
        "military_crisis",                       # 军事危机触发
    ],

    # ── democratic_spring ──
    "ds_first_protests": [
        "event.event_1016_busan_protest",         # 釜山抗议启动民主运动
        "event.event_seoul_student_protest_oct",   # 首尔学潮启动
        "military_takeover",                       # 军事接管反激发抗议
        "busan_uprising_started",                  # 釜山起义事件
        "campus_unrest",                           # 校园动荡
    ],
    "ds_underground_network": [
        "event.event_masan_protest_spread",       # 抗议扩散激活地下网络
        "crackdown_occurred",                     # 镇压后地下组织活跃
        "underground_activity",                   # 周期：地下出版物分发
        "protest_ongoing",                        # 持续抗议激活地下
    ],
    "ds_public_demand": [
        "event.event_seoul_student_protest_oct",  # 学生运动推动公众诉求
        "general_strike",                         # 总罢工催生公众要求
        "democratic_confrontation",               # 民主对抗升级
        "campus_unrest",                          # 校园动荡
    ],
    "ds_confrontation": [
        "martial_law_declared",                   # 戒严令直接引发对抗
        "regime_collapsed",                       # 政权崩溃引发最终对抗
        "military_crisis",                        # 军事危机
    ],
    "ds_spring_fate": [
        "spring_outcome",                         # 语义闭环（自触发无害）
        "regime_collapsed",                       # 政权崩溃决定命运
        "coup_executed",                          # 政变执行影响春天走向
    ],
    "ds_martyrdom": [
        "event.event_1018_busan_martial_law",     # 釜山戒严镇压导致牺牲
        "crackdown_occurred",                     # 任何镇压都可能产生殉道者
    ],
    "ds_international_solidarity": [
        "event.event_1027_us_alert",              # 美军警戒触发国际关注
        "event.event_1027_us_statement",          # 美方声明触发国际声援
        "event.event_japan_media_comment",        # 日媒报道触发
        "us_intervention",                        # 美方介入
    ],
}


# ═══════════════════════════════════════════════
# 2. Story Tree 节点 effects 添加 fire_events
#    实现跨线触发
# ═══════════════════════════════════════════════
STORY_TREE_FIRE_EVENTS = {
    # 维新崩塌 → 新军部崛起 / 民主之春
    "yc_aftermath_chaos": ["regime_crisis", "kcia_collapsed"],
    "yc_busan_crisis": ["busan_crisis_started", "protest_ongoing"],
    "yc_assassination_decision": ["assassination_imminent"],
    "yc_busan_escalation": ["busan_uprising_started", "campus_unrest"],

    # 新军部崛起 → 民主之春 / 维新崩塌
    "nm_new_order": ["military_takeover"],
    "nm_1212_coup": ["coup_executed", "military_crisis"],
    "nm_hanahoe_conspiracy": ["hanahoe_activated"],
    "nm_investigation_power": ["investigation_authority_granted"],
    "nm_loyalty_purge": ["hanahoe_activity"],

    # 民主之春 → 跨线
    "ds_confrontation": ["democratic_confrontation", "crackdown_occurred"],
    "ds_spring_fate": ["spring_outcome"],
    "ds_martyrdom": ["crackdown_occurred"],
    "ds_underground_network": ["underground_activity"],
    "ds_first_protests": ["campus_unrest"],
    "ds_international_solidarity": ["us_intervention"],
}


# ═══════════════════════════════════════════════
# 3. 一次性事件添加 fire_events
#    向 story tree 发射语义事件
# ═══════════════════════════════════════════════
OT_FIRE_EVENTS = {
    "event_1026_incident": ["assassination_occurred", "regime_crisis"],
    "event_martial_law_declaration": ["martial_law_declared"],
    "event_1212_military_mutiny": ["coup_executed", "military_takeover"],
    "event_1016_busan_protest": ["busan_uprising_started"],
    "event_1026_arrest_kim_jae_gyu": ["kcia_collapsed", "regime_crisis"],
    "event_1026_assassination_conspiracy": ["assassination_imminent"],
    "event_1212_hannam_raid": ["coup_imminent"],
    "event_1027_us_alert": ["us_intervention"],
    "event_chun_joint_investigation": ["investigation_authority_granted"],
    "event_hanahoe_secret_plot": ["hanahoe_activated"],
}


# ═══════════════════════════════════════════════
# 4. 周期事件添加 fire_events
# ═══════════════════════════════════════════════
CY_FIRE_EVENTS = {
    "buma_protest_updates": ["protest_ongoing"],
    "hanahoe_secret_gathering": ["hanahoe_activity"],
    "kcia_morning_briefing": ["intelligence_update"],
    "seoul_campus_unrest": ["campus_unrest"],
    "underground_press_distribution": ["underground_activity"],
}


# ═══════════════════════════════════════════════
# 5. 动态事件添加 fire_events
#    (引擎已修改支持 de.fire_events → story tree effects)
# ═══════════════════════════════════════════════
DE_FIRE_EVENTS = {
    "de_crackdown": ["crackdown_occurred"],
    "de_coup_countdown": ["coup_imminent"],
    "de_general_strike": ["general_strike"],
    "de_regime_collapse_signal": ["regime_collapsed"],
    "de_campus_rally": ["campus_unrest"],
    "de_us_ultimatum": ["us_intervention"],
    "de_martial_law_extension": ["martial_law_declared"],
    "de_cover_blown_rumor": ["identity_compromised"],
    "de_military_standoff": ["military_crisis"],
    "de_point_of_no_return": ["coup_imminent", "regime_collapsed"],
}


def apply_story_tree_activate_events(script):
    st = script.get("story_tree", {})
    node_map = {}
    for tree in st.get("trees", []):
        for node in tree.get("nodes", []):
            node_map[node["id"]] = node

    updated = 0
    for nid, events in STORY_TREE_ACTIVATE_EVENTS.items():
        node = node_map.get(nid)
        if not node:
            continue
        existing = set(node.get("activate_events", []))
        new_events = [e for e in events if e not in existing]
        if new_events:
            node["activate_events"] = list(existing | set(new_events))
            updated += 1
    return updated


def apply_story_tree_fire_events(script):
    st = script.get("story_tree", {})
    node_map = {}
    for tree in st.get("trees", []):
        for node in tree.get("nodes", []):
            node_map[node["id"]] = node

    updated = 0
    for nid, events in STORY_TREE_FIRE_EVENTS.items():
        node = node_map.get(nid)
        if not node:
            continue
        effects = node.setdefault("effects", {})
        existing = set(effects.get("fire_events", []))
        new_events = [e for e in events if e not in existing]
        if new_events:
            effects["fire_events"] = list(existing | set(new_events))
            updated += 1
    return updated


def apply_ot_fire_events(script):
    events = script.get("one_time_events", [])
    eid_map = {e["id"]: e for e in events}
    updated = 0
    for eid, fire_events in OT_FIRE_EVENTS.items():
        evt = eid_map.get(eid)
        if not evt:
            continue
        existing = set(evt.get("fire_events", []))
        new_events = [e for e in fire_events if e not in existing]
        if new_events:
            evt["fire_events"] = list(existing | set(new_events))
            updated += 1
    return updated


def apply_cy_fire_events(script):
    events = script.get("cyclic_events", [])
    cid_map = {c["id"]: c for c in events}
    updated = 0
    for cid, fire_events in CY_FIRE_EVENTS.items():
        evt = cid_map.get(cid)
        if not evt:
            continue
        existing = set(evt.get("fire_events", []))
        new_events = [e for e in fire_events if e not in existing]
        if new_events:
            evt["fire_events"] = list(existing | set(new_events))
            updated += 1
    return updated


def apply_de_fire_events(script):
    events = script.get("dynamic_events", [])
    did_map = {d["id"]: d for d in events}
    updated = 0
    for did, fire_events in DE_FIRE_EVENTS.items():
        evt = did_map.get(did)
        if not evt:
            continue
        existing = set(evt.get("fire_events", []))
        new_events = [e for e in fire_events if e not in existing]
        if new_events:
            evt["fire_events"] = list(existing | set(new_events))
            updated += 1
    return updated


def validate_linkages(script):
    errors = []

    # Collect all fire_events emitted
    all_emitted = set()

    for evt in script.get("one_time_events", []):
        for fe in evt.get("fire_events", []):
            all_emitted.add(fe)
    for evt in script.get("cyclic_events", []):
        for fe in evt.get("fire_events", []):
            all_emitted.add(fe)
    for de in script.get("dynamic_events", []):
        for fe in de.get("fire_events", []):
            all_emitted.add(fe)
    # auto event.{eid} emissions
    for evt in script.get("one_time_events", []):
        all_emitted.add(f"event.{evt['id']}")
    for evt in script.get("cyclic_events", []):
        all_emitted.add(f"event.{evt['id']}")

    st = script.get("story_tree", {})
    for tree in st.get("trees", []):
        for node in tree.get("nodes", []):
            for fe in node.get("effects", {}).get("fire_events", []):
                all_emitted.add(fe)

    # Check all activate_events are actually emitted somewhere
    all_listened = set()
    for tree in st.get("trees", []):
        for node in tree.get("nodes", []):
            for ae in node.get("activate_events", []):
                all_listened.add(ae)
                if ae not in all_emitted:
                    errors.append(f"node '{node['id']}' listens for '{ae}' but nothing emits it")

    # Orphan fire_events (emitted but never listened)
    orphans = all_emitted - all_listened
    # Filter out event.{eid} auto-emissions (they fire regardless)
    orphans = {o for o in orphans if not o.startswith("event.")}

    return errors, orphans, len(all_listened), len(all_emitted)


def main():
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT content FROM scripts WHERE id=?", (SCRIPT_ID,)).fetchone()
    if not row:
        print(f"ERROR: script '{SCRIPT_ID}' not found")
        sys.exit(1)
    script = json.loads(row[0])

    print("=" * 60)
    print("事件系统 <-> 剧情树 关联补充")
    print("=" * 60)

    n1 = apply_story_tree_activate_events(script)
    print(f"[1] Story Tree activate_events: {n1} nodes updated")

    n2 = apply_story_tree_fire_events(script)
    print(f"[2] Story Tree fire_events (cross-tree): {n2} nodes updated")

    n3 = apply_ot_fire_events(script)
    print(f"[3] One-Time Events fire_events: {n3} events updated")

    n4 = apply_cy_fire_events(script)
    print(f"[4] Cyclic Events fire_events: {n4} events updated")

    n5 = apply_de_fire_events(script)
    print(f"[5] Dynamic Events fire_events: {n5} events updated")

    # Validate
    print("\n" + "-" * 60)
    errors, orphans, n_listened, n_emitted = validate_linkages(script)
    print(f"Total: {n_listened} events listened, {n_emitted} events emitted")

    if errors:
        print(f"\n[FAIL] {len(errors)} errors:")
        for e in errors:
            print(f"  ERROR: {e}")
        sys.exit(1)

    if orphans:
        print(f"\n[INFO] {len(orphans)} emitted events not listened by any node:")
        for o in sorted(orphans):
            print(f"  - {o}")

    if not errors:
        print("\n[OK] All linkages valid")

    # Write
    content = json.dumps(script, ensure_ascii=False, indent=None)
    conn.execute("UPDATE scripts SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (content, SCRIPT_ID))
    conn.commit()
    conn.close()

    # Print linkage map
    print("\n" + "=" * 60)
    print("Linkage Map:")
    st = script.get("story_tree", {})
    for tree in st.get("trees", []):
        print(f"\n  [{tree['id']}]")
        for node in tree.get("nodes", []):
            ae = node.get("activate_events", [])
            fe = node.get("effects", {}).get("fire_events", [])
            if ae or fe:
                tag = f"    {node['id']}"
                if ae:
                    tag += f" <-- {ae}"
                if fe:
                    tag += f" --> {fe}"
                print(tag)

    print("\n" + "=" * 60)
    print("Done.")


if __name__ == "__main__":
    main()
