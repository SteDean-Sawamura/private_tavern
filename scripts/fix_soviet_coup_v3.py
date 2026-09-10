"""八一九事变剧本第三轮优化：事件链可达性修复"""
import sqlite3, json, sys

DB_PATH = "data/tavern.db"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    db = sqlite3.connect(DB_PATH)
    row = db.execute('SELECT content FROM scripts WHERE id="soviet_august_coup_1991"').fetchone()
    data = json.loads(row[0])
    original = json.dumps(data, ensure_ascii=False)

    ot_by_id = {e["id"]: e for e in data["one_time_events"]}
    wp_by_id = {w["id"]: w for w in data["world_properties"]}

    # ── 1. world_properties: 字符串值转整数 ────────────────────
    count_wp = 0
    for w in data["world_properties"]:
        if isinstance(w["value"], str):
            try:
                w["value"] = int(w["value"])
                count_wp += 1
            except ValueError:
                try:
                    w["value"] = float(w["value"])
                    count_wp += 1
                except ValueError:
                    pass
    print(f"[1] world_properties: 修复 {count_wp} 个字符串值为整数")

    # ── 2. novo_ogaryovo_secret_agreement: 开局前事件 ──────────
    # trigger_time 1991-08-15 在游戏开始 (08-16 06:00) 之前
    # 方案：移到 08-16T07:00 改为"回忆/消息传来"形式
    if "novo_ogaryovo_secret_agreement" in ot_by_id:
        evt = ot_by_id["novo_ogaryovo_secret_agreement"]
        evt["trigger_time"] = "1991-08-16T07:00"
        old_desc = evt.get("description", "")
        if "新联盟条约" in old_desc and "回忆" not in old_desc:
            evt["description"] = (
                "你从同事口中听到昨天的消息：戈尔巴乔夫与各共和国领导人原定于8月20日"
                "在新奥加廖沃签署《新联盟条约》，将大幅削弱中央权力。"
                "这意味着强硬派们所剩的时间已经不多了。"
            )
        print("[2] novo_ogaryovo_secret_agreement: 移至 08-16T07:00（回忆形式）")
    else:
        print("[2] novo_ogaryovo_secret_agreement: 未找到")

    # ── 3. foros_comms_total_cut: gorbachev_isolation 不足 ─────
    # 需要 >= 70，但模拟到 08-18T16:00 时仅 50
    # 方案：降低条件阈值到 >= 45，并在 foros_emissary_departure 增加 +10 效果
    if "foros_comms_total_cut" in ot_by_id:
        evt = ot_by_id["foros_comms_total_cut"]
        old_cond = evt.get("condition", "")
        evt["condition"] = old_cond.replace(
            "gorbachev_isolation >= 70", "gorbachev_isolation >= 45"
        )
        print(f"[3a] foros_comms_total_cut: 条件阈值 70→45")

    if "foros_emissary_departure" in ot_by_id:
        evt = ot_by_id["foros_emissary_departure"]
        sc = evt.get("state_changes", [])
        has_iso = any("gorbachev_isolation" in s.get("target", "") for s in sc)
        if not has_iso:
            sc.append({"target": "world.gorbachev_isolation", "op": "add", "value": 10})
            evt["state_changes"] = sc
            print("[3b] foros_emissary_departure: 增加 gorbachev_isolation +10")

    # ── 4. tanks_roll_into_moscow: coup_preparedness 差 2 点 ──
    # 需要 >= 100，模拟到 08-19T03:00 时为 98
    # 方案：emergency_committee_formation (08-18T22:00) 增加 coup_preparedness +5
    if "emergency_committee_formation" in ot_by_id:
        evt = ot_by_id["emergency_committee_formation"]
        sc = evt.get("state_changes", [])
        has_cp = any("coup_preparedness" in s.get("target", "") for s in sc)
        if not has_cp:
            sc.append({"target": "world.coup_preparedness", "op": "add", "value": 5})
            evt["state_changes"] = sc
            print("[4] emergency_committee_formation: 增加 coup_preparedness +5")

    # ── 5. vdv_grachev_refusal: vdv_loyalty 方向错误 ──────────
    # 需要 <= 30 但 vdv_loyalty 从 50 一直涨
    # 方案A: 在 tanks_roll_into_moscow 增加 vdv_loyalty -15（士兵看到民众后动摇）
    # 方案B: yeltsin_decree_59 增加 vdv_loyalty -10
    # 方案C: 同时加入 alpha_refusal_to_arrest vdv_loyalty -10
    # 综合方案：tanks + yeltsin_decree 共 -25，加上调整事件顺序
    # 但 vdv_grachev_refusal 在 09:00，tanks 在 03:00，中间需足够下降
    # vdv_loyalty 到 tanks 时约 58，tanks -20 → 38，还需额外 -9
    # gkchp_appeal_to_people (07:00) 增加 vdv_loyalty -10（士兵得知真相后质疑）
    if "tanks_roll_into_moscow" in ot_by_id:
        evt = ot_by_id["tanks_roll_into_moscow"]
        sc = evt.get("state_changes", [])
        has_vdv = any("vdv_loyalty" in s.get("target", "") for s in sc)
        if not has_vdv:
            sc.append({"target": "world.vdv_loyalty", "op": "add", "value": -20})
            evt["state_changes"] = sc
            print("[5a] tanks_roll_into_moscow: 增加 vdv_loyalty -20（民众对峙动摇）")

    if "gkchp_appeal_to_people" in ot_by_id:
        evt = ot_by_id["gkchp_appeal_to_people"]
        sc = evt.get("state_changes", [])
        has_vdv = any("vdv_loyalty" in s.get("target", "") for s in sc)
        if not has_vdv:
            sc.append({"target": "world.vdv_loyalty", "op": "add", "value": -10})
            evt["state_changes"] = sc
            print("[5b] gkchp_appeal_to_people: 增加 vdv_loyalty -10（谎言激怒士兵）")

    # ── 6. yeltsin_white_house_speech: yeltsin_awareness 差 9 ─
    # 需要 >= 70，模拟到 08-19T12:00 时为 61
    # 方案：yeltsin_decree_59 (10:00) 增加 yeltsin_awareness +10
    if "yeltsin_decree_59" in ot_by_id:
        evt = ot_by_id["yeltsin_decree_59"]
        sc = evt.get("state_changes", [])
        has_ya = any("yeltsin_awareness" in s.get("target", "") for s in sc)
        if not has_ya:
            sc.append({"target": "world.yeltsin_awareness", "op": "add", "value": 10})
            evt["state_changes"] = sc
            print("[6] yeltsin_decree_59: 增加 yeltsin_awareness +10")

    # ── 7. 补充关键事件的缺失 state_changes ───────────────────
    MISSING_SC = {
        "pavlov_dacha_party_interrupted": [
            {"target": "world.coup_preparedness", "op": "add", "value": 5},
        ],
        "foros_ultimatum_confrontation": [
            {"target": "world.gorbachev_isolation", "op": "add", "value": 20},
        ],
        "tv_radio_gkchp_announcement": [
            {"target": "world.public_unease", "op": "add", "value": 25},
            {"target": "world.yeltsin_awareness", "op": "add", "value": 15},
        ],
        "alpha_refusal_to_arrest": [
            {"target": "world.vdv_loyalty", "op": "add", "value": -15},
            {"target": "world.coup_preparedness", "op": "add", "value": -10},
        ],
        "kurchatov_internet_breakthrough": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 5},
            {"target": "world.public_unease", "op": "add", "value": 5},
        ],
        "yanaev_press_conference": [
            {"target": "world.coup_preparedness", "op": "add", "value": -15},
            {"target": "world.public_unease", "op": "add", "value": 10},
        ],
        "moscow_curfew_declared": [
            {"target": "world.public_unease", "op": "add", "value": 15},
        ],
        "whitehouse_assault_attempt": [
            {"target": "world.public_unease", "op": "add", "value": 20},
            {"target": "world.vdv_loyalty", "op": "add", "value": -20},
        ],
        "tank_battalion_defection": [
            {"target": "world.vdv_loyalty", "op": "add", "value": -30},
            {"target": "world.coup_preparedness", "op": "add", "value": -25},
        ],
        "defense_ministry_withdrawal_order": [
            {"target": "world.coup_preparedness", "op": "set", "value": 0},
        ],
        "gorbachev_return_and_arrests": [
            {"target": "world.gorbachev_isolation", "op": "set", "value": 0},
        ],
    }
    count_sc = 0
    for eid, sc_list in MISSING_SC.items():
        if eid in ot_by_id:
            evt = ot_by_id[eid]
            existing = evt.get("state_changes", [])
            existing_targets = {s.get("target", "") for s in existing}
            added = []
            for sc in sc_list:
                if sc["target"] not in existing_targets:
                    existing.append(sc)
                    added.append(sc["target"].split(".")[-1])
            if added:
                evt["state_changes"] = existing
                count_sc += 1
    print(f"[7] 补充 state_changes: {count_sc} 个事件")

    # ── 保存 ─────────────────────────────────────────────────────
    new_content = json.dumps(data, ensure_ascii=False)
    if new_content != original:
        db.execute(
            'UPDATE scripts SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id="soviet_august_coup_1991"',
            (new_content,),
        )
        db.commit()
        print("\n✓ 已保存到数据库")
    else:
        print("\n⚠ 无变更")
    db.close()


if __name__ == "__main__":
    main()
