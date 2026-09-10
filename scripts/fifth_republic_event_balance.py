"""审计修复脚本2：从内容和游戏性角度优化所有事件的字段合理性。"""

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
    ot_events = script.get("one_time_events", [])
    cy_events = script.get("cyclic_events", [])
    dy_events = script.get("dynamic_events", [])

    changes = 0

    # ═══════════════════════════════════════════════
    # 1. One-Time Events 修复
    # ═══════════════════════════════════════════════

    def find_ot(eid):
        return next((e for e in ot_events if e["id"] == eid), None)

    # 1a. event_1026_first_shot 时间从 19:40 改为 19:39 (先于 incident)
    e = find_ot("event_1026_first_shot")
    if e:
        e["trigger_time"] = "1979-10-26T19:39"
        # 增加 state_change：regime_stability 因暗杀即将发生而剧降
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "第一枪——金载圭拔枪射向车智澈，宴席上的杯盘碎裂，十八年独裁统治的倒计时开始。", "priority": "critical"},
        ]
        changes += 1
        print("  FIX event_1026_first_shot: 时间改为19:39, 增加regime_stability-10")

    # 1b. event_1026_park_shot — 增加 state_change
    e = find_ot("event_1026_park_shot")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -30},
            {"type": "narrative_callback", "text": "朴正熙中枪——子弹穿过总统的胸膛，十八年的维新体制在这一刻崩塌。", "priority": "critical"},
        ]
        changes += 1
        print("  FIX event_1026_park_shot: 增加regime_stability-30")

    # 1c. event_1026_gun_jam — 增加对掩护的影响 (混乱中暴露风险)
    e = find_ot("event_1026_gun_jam")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "枪支卡壳——命运在这几十秒间摇摆，金载圭疯狂地排除故障，历史悬于一线。", "priority": "critical"},
        ]
        changes += 1
        print("  FIX event_1026_gun_jam: 增加communication_security-5")

    # 1d. event_1026_bodyguards_eliminated — 移除重复的 fire_events
    e = find_ot("event_1026_bodyguards_eliminated")
    if e:
        e["fire_events"] = ["bodyguards_eliminated"]
        changes += 1
        print("  FIX event_1026_bodyguards_eliminated: fire_events去重(改为bodyguards_eliminated)")

    # 1e. event_1026_body_transfer — 增加 state_change
    e = find_ot("event_1026_body_transfer")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "add", "value": 1},
            {"type": "narrative_callback", "text": "总统遗体被转移——在混乱和恐惧中，尸体被匆忙运往陆军医院。", "priority": "high"},
        ]
        changes += 1
        print("  FIX event_1026_body_transfer: 增加military_alert_level+1")

    # 1f. event_1026_witnesses_escape — 增加 state_change
    e = find_ot("event_1026_witnesses_escape")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.intelligence_leaks", "op": "add", "value": 1},
            {"type": "narrative_callback", "text": "目击者逃离——几名女性在枪声中惊恐逃跑，她们将成为这个历史夜晚的关键证人。", "priority": "medium"},
        ]
        changes += 1
        print("  FIX event_1026_witnesses_escape: 增加intelligence_leaks+1")

    # 1g. event_1027_us_statement — 增加 narrative_callback
    e = find_ot("event_1027_us_statement")
    if e:
        e["effects"].append(
            {"type": "narrative_callback", "text": "美国国务卿的措辞看似温和，实则暗藏警告——华盛顿对韩国民主化进程有了新的期待。", "priority": "medium"}
        )
        changes += 1
        print("  FIX event_1027_us_statement: 增加narrative_callback")

    # 1h. event_levee_ceremony — 增加微弱的游戏性(体现日常的虚假平静)
    e = find_ot("event_levee_ceremony")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 3},
            {"type": "narrative_callback", "text": "防潮水利工程剪彩——这是一个和平日常的背景事件，暗示着平静表面下的暗流涌动。", "priority": "low"},
        ]
        changes += 1
        print("  FIX event_levee_ceremony: 增加regime_stability+3(虚假的繁荣)")

    # 1i. event_1026_gun_preparation — 增加 state_change
    e = find_ot("event_1026_gun_preparation")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -3},
            {"type": "narrative_callback", "text": "金载圭从保险柜取出手枪——冰冷的金属握在掌心，历史在这一刻开始倒计时。", "priority": "critical"},
        ]
        changes += 1
        print("  FIX event_1026_gun_preparation: 增加cover_integrity-3")

    # 1j. event_1026_dinner_invitation — 增加轻微 state_change
    e = find_ot("event_1026_dinner_invitation")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -3},
            {"type": "narrative_callback", "text": "宴会邀请发出——一场精心设计的局，所有人都被引向宫井洞那间注定沾满鲜血的房间。", "priority": "high"},
        ]
        changes += 1
        print("  FIX event_1026_dinner_invitation: 增加communication_security-3")

    # 1k. event_1026_assassination_conspiracy — 增加 state_change
    e = find_ot("event_1026_assassination_conspiracy")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -5},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "暗杀阴谋的最后准备——金载圭的几个亲信已经知道今晚将发生什么，恐惧和决心交织在他们脸上。", "priority": "critical"},
        ]
        changes += 1
        print("  FIX event_1026_assassination_conspiracy: 增加cover-5, regime-5")

    # 1l. event_1026_cha_killed — 增加 state_change
    e = find_ot("event_1026_cha_killed")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -15},
            {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "车智澈被击毙——权力核心的另一个支柱倒下，白色的瓷砖被血染成暗红。", "priority": "high"},
        ]
        changes += 1
        print("  FIX event_1026_cha_killed: 增加regime-15, troops_loyalty-10")

    # 1m. event_1026_park_arrival — 增加轻微效果
    e = find_ot("event_1026_park_arrival")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 2},
            {"type": "narrative_callback", "text": "朴正熙的车队驶入宫井洞——总统的最后一个黄昏，叙事应营造不祥的平静。", "priority": "high"},
        ]
        changes += 1
        print("  FIX event_1026_park_arrival: 增加regime_stability+2")

    # ═══════════════════════════════════════════════
    # 2. Cyclic Events 修复
    # ═══════════════════════════════════════════════

    def find_cy(eid):
        return next((e for e in cy_events if e["id"] == eid), None)

    # 2a. nightly_curfew — expires_at 应在全国戒严后继续，改为 12.12 后
    e = find_cy("nightly_curfew")
    if e:
        e["expires_at"] = "1980-05-17T00:00"
        e["condition"] = ""
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -2},
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -1},
        ]
        changes += 1
        print("  FIX nightly_curfew: expires_at延长至1980-05-17, 去掉condition, 增加comm_sec-1")

    # 2b. kcia_morning_briefing — 延长至中情部被全斗焕接管
    e = find_cy("kcia_morning_briefing")
    if e:
        e["expires_at"] = "1979-12-13T00:00"
        changes += 1
        print("  FIX kcia_morning_briefing: expires_at延长至12.13(双十二后KCIA失能)")

    # 2c. pss_loyalty_drill — 车智澈死后应停止，加 expires_at
    e = find_cy("pss_loyalty_drill")
    if e:
        e["expires_at"] = "1979-10-26T19:40"
        e["condition"] = ""
        changes += 1
        print("  FIX pss_loyalty_drill: 加expires_at 10.26 19:40 (车智澈遇害)")

    # 2d. hanahoe_secret_gathering — 加 expires_at 12.12 (之后公开行动)
    e = find_cy("hanahoe_secret_gathering")
    if e:
        e["expires_at"] = "1979-12-12T18:00"
        changes += 1
        print("  FIX hanahoe_secret_gathering: 加expires_at 12.12 (政变后不再需要秘密聚会)")

    # 2e. chaebol_political_donation — 增加 narrative_callback
    e = find_cy("chaebol_political_donation")
    if e:
        e["effects"].append(
            {"type": "narrative_callback", "text": "又一笔政治资金悄然到账——权钱交易的机器从未停止运转。", "priority": "low"}
        )
        changes += 1
        print("  FIX chaebol_political_donation: 增加narrative_callback")

    # ═══════════════════════════════════════════════
    # 3. Dynamic Events 修复
    # ═══════════════════════════════════════════════

    def find_dy(eid):
        return next((e for e in dy_events if e["id"] == eid), None)

    # 3a. de_arms_cache_discovery — "被发现"应减少武器(暴露)而非增加
    e = find_dy("de_arms_cache_discovery")
    if e:
        e["description"] = "秘密储藏的武器弹药被友方发现并编入作战序列。"
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.weapons_secured", "op": "add", "value": 10},
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "一处隐蔽的武器库被己方确认——弹药箱整齐码放，足以装备一个营。", "priority": "high"},
        ]
        e["condition"] = "script_variables.coup_readiness >= 40 AND script_variables.weapons_secured < 80"
        changes += 1
        print("  FIX de_arms_cache_discovery: 描述改为友方发现, 增加coup_readiness+5, 调整condition")

    # 3b. de_coup_countdown — coup_readiness 已>=80不需要再加, 改为加强实际效果
    e = find_dy("de_coup_countdown")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
            {"type": "activate_state", "id": "military_lockdown"},
            {"type": "narrative_callback", "text": "一切准备就绪——政变的时针已经开始倒数。", "priority": "critical"},
        ]
        changes += 1
        print("  FIX de_coup_countdown: 移除无意义的coup+10, 改为troops-10 regime-10")

    # 3c. de_point_of_no_return — 增加 state_change 使其有实际效果
    e = find_dy("de_point_of_no_return")
    if e:
        e["effects"] = [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "set", "value": 0},
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "set", "value": 100},
            {"type": "activate_state", "id": "power_vacuum"},
            {"type": "narrative_callback", "text": "已经没有退路了——历史的车轮碾过了最后一个可以转向的路口。", "priority": "critical"},
        ]
        changes += 1
        print("  FIX de_point_of_no_return: 增加regime=0, coup=100, activate power_vacuum")

    # 3d. de_informant_contact — cooldown太短容易刷key_documents, 改为8
    e = find_dy("de_informant_contact")
    if e:
        e["cooldown"] = 8
        e["condition"] = "script_variables.cover_integrity >= 50 AND script_variables.key_documents_found < 10"
        changes += 1
        print("  FIX de_informant_contact: cooldown 5→8, cover要求50+, documents上限10")

    # 3e. de_campus_rally — 与 cyclic seoul_campus_unrest 区分, 提高触发条件
    e = find_dy("de_campus_rally")
    if e:
        e["condition"] = "script_variables.protest_intensity >= 60 AND script_variables.student_network_size >= 25"
        e["cooldown"] = 7
        changes += 1
        print("  FIX de_campus_rally: 提高条件(protest>=60, network>=25), cooldown 5→7")

    # 3f. de_border_alert — 条件太宽松, 加 regime_stability 约束
    e = find_dy("de_border_alert")
    if e:
        e["condition"] = "script_variables.military_alert_level >= 4 AND script_variables.regime_stability <= 30"
        changes += 1
        print("  FIX de_border_alert: 条件收紧(alert>=4 + regime<=30)")

    # 3g. de_underground_pamphlet — cooldown太短会和cyclic重叠, 改为7
    e = find_dy("de_underground_pamphlet")
    if e:
        e["cooldown"] = 7
        changes += 1
        print("  FIX de_underground_pamphlet: cooldown 5→7 (避免与cyclic地下传单冲突)")

    # 3h. de_weapon_smuggling — weapons_secured+15太大, 改为8
    e = find_dy("de_weapon_smuggling")
    if e:
        e["effects"][0]["value"] = 8
        changes += 1
        print("  FIX de_weapon_smuggling: weapons_secured +15→+8 (平衡性)")

    # 3i. de_security_breach — 双重减值太严厉，comm-10+cover-5同时发生
    e = find_dy("de_security_breach")
    if e:
        e["effects"][0]["value"] = -7  # comm_security 从-10改为-7
        changes += 1
        print("  FIX de_security_breach: communication_security -10→-7 (稍微缓和)")

    # 3j. de_emergency_extraction — safe_house_count-1可能变负, 加floor check condition
    e = find_dy("de_emergency_extraction")
    if e:
        e["condition"] = "script_variables.cover_integrity <= 15 AND script_variables.safe_house_count >= 1"
        changes += 1
        print("  FIX de_emergency_extraction: 增加safe_house>=1条件(避免变负)")

    # 3k. de_double_agent_opportunity — betrayal_count==0 太严格, 大部分玩家无法触发
    e = find_dy("de_double_agent_opportunity")
    if e:
        e["condition"] = "script_variables.cover_integrity >= 60 AND script_variables.betrayal_count <= 1"
        changes += 1
        print("  FIX de_double_agent_opportunity: betrayal==0 改为 <=1 (放宽)")

    # 3l. de_north_korea_provocation — military_alert_level >= 4 在游戏早期就满足(戒严后), 加时间性条件
    e = find_dy("de_north_korea_provocation")
    if e:
        e["condition"] = "script_variables.military_alert_level >= 4 AND script_variables.regime_stability <= 20 AND script_variables.diplomatic_crisis_level >= 2"
        changes += 1
        print("  FIX de_north_korea_provocation: 增加diplomatic>=2条件(需国际紧张)")

    # 3m. de_regime_collapse_signal 与 de_point_of_no_return 功能重叠, 区分它们
    e = find_dy("de_regime_collapse_signal")
    if e:
        # 这个作为军事政变导致崩塌，point_of_no_return 作为综合崩塌
        e["condition"] = "script_variables.regime_stability <= 10 AND script_variables.coup_readiness >= 80 AND script_variables.troops_loyalty <= 20"
        changes += 1
        print("  FIX de_regime_collapse_signal: 增加troops_loyalty<=20条件(与point_of_no_return区分)")

    # 3n. de_general_strike — civilian_casualties>=10 条件可能过于苛刻
    e = find_dy("de_general_strike")
    if e:
        e["condition"] = "script_variables.protest_intensity >= 75 AND script_variables.civilian_casualties >= 5"
        changes += 1
        print("  FIX de_general_strike: casualties门槛 10→5 (更容易触发)")

    # 3o. de_military_civilian_clash — civilian_casualties+8太大, 改为5
    e = find_dy("de_military_civilian_clash")
    if e:
        e["effects"][0]["value"] = 5  # casualties +8→+5
        changes += 1
        print("  FIX de_military_civilian_clash: casualties +8→+5 (避免过快累积)")

    # ═══════════════════════════════════════════════
    # 保存
    # ═══════════════════════════════════════════════
    script["one_time_events"] = ot_events
    script["cyclic_events"] = cy_events
    script["dynamic_events"] = dy_events

    conn.execute("UPDATE scripts SET content=? WHERE id=?", (json.dumps(script, ensure_ascii=False), SCRIPT_ID))
    conn.commit()
    conn.close()
    print(f"\nDone. Applied {changes} fixes.")


if __name__ == "__main__":
    main()
