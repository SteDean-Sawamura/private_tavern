"""第五共和国剧本：统一事件系统全面优化脚本。"""

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tavern.db"
SCRIPT_ID = "fifth_republic_dawn"


# ═══════════════════════════════════════════════
# 1. Variables — 20 个
# ═══════════════════════════════════════════════
def add_variables(script):
    variables = script.setdefault("variables", [])
    existing = {v["id"] for v in variables}
    new_vars = [
        # 政治线
        {"id": "regime_stability", "name": "政权稳定度", "type": "number", "default": 70, "min": 0, "max": 100},
        {"id": "coup_readiness", "name": "政变准备度", "type": "number", "default": 0, "min": 0, "max": 100},
        {"id": "us_confidence", "name": "美方信任度", "type": "number", "default": 50, "min": 0, "max": 100},
        {"id": "diplomatic_crisis_level", "name": "外交危机等级", "type": "number", "default": 0, "min": 0, "max": 3},
        {"id": "political_faction", "name": "政治立场", "type": "text", "default": "neutral"},
        {"id": "intelligence_leaks", "name": "情报泄露次数", "type": "number", "default": 0, "min": 0, "max": 10},
        # 军事线
        {"id": "military_alert_level", "name": "军事戒备等级", "type": "number", "default": 1, "min": 1, "max": 4},
        {"id": "troops_loyalty", "name": "部队忠诚度", "type": "number", "default": 50, "min": 0, "max": 100},
        {"id": "weapons_secured", "name": "武器储备", "type": "number", "default": 0, "min": 0, "max": 100},
        {"id": "communication_security", "name": "通讯安全度", "type": "number", "default": 70, "min": 0, "max": 100},
        {"id": "hanahoe_infiltration", "name": "一心会渗透度", "type": "number", "default": 30, "min": 0, "max": 100},
        # 民众线
        {"id": "protest_intensity", "name": "抗议强度", "type": "number", "default": 60, "min": 0, "max": 100},
        {"id": "media_freedom", "name": "媒体自由度", "type": "number", "default": 20, "min": 0, "max": 100},
        {"id": "student_network_size", "name": "学生网络规模", "type": "number", "default": 10, "min": 0, "max": 100},
        {"id": "civilian_casualties", "name": "平民伤亡", "type": "number", "default": 0, "min": 0, "max": 100},
        # 个人线
        {"id": "cover_integrity", "name": "身份掩护度", "type": "number", "default": 100, "min": 0, "max": 100},
        {"id": "betrayal_count", "name": "背叛次数", "type": "number", "default": 0, "min": 0, "max": 10},
        {"id": "key_documents_found", "name": "关键文件数", "type": "number", "default": 0, "min": 0, "max": 20},
        {"id": "safe_house_count", "name": "安全屋数量", "type": "number", "default": 0, "min": 0, "max": 5},
        {"id": "days_elapsed", "name": "经过天数", "type": "number", "default": 0, "min": 0, "max": 365},
    ]
    added = 0
    for v in new_vars:
        if v["id"] not in existing:
            variables.append(v)
            existing.add(v["id"])
            added += 1
    return added


# ═══════════════════════════════════════════════
# 2. Persistent States — 新增 12 个
# ═══════════════════════════════════════════════
def add_persistent_states(script):
    ps_list = script.setdefault("persistent_states", [])
    existing = {p["id"] for p in ps_list}
    new_states = [
        {"id": "player_hunted", "name": "通缉状态", "initially_active": False,
         "description": "玩家被当局通缉，行动受限",
         "prompt_inject": "⚠ 你正被当局通缉。公共场所有你的照片，军警在搜捕你。行动必须极度谨慎。"},
        {"id": "double_agent", "name": "双面间谍", "initially_active": False,
         "description": "玩家同时为两方工作",
         "prompt_inject": "你正以双面间谍身份活动，必须在两个阵营之间维持信任的平衡，任何一方的怀疑都可能致命。"},
        {"id": "military_lockdown", "name": "军事封锁", "initially_active": False,
         "description": "城市实施军事管制",
         "prompt_inject": "⚠ 军事封锁生效中。街道上有装甲车和持枪士兵巡逻，检查站遍布主要路口，通行需要军方许可证。"},
        {"id": "information_blackout", "name": "信息管制", "initially_active": False,
         "description": "新闻和通讯受严格审查",
         "prompt_inject": "信息管制令下达——报社被军方接管，电话线路被监听，外国记者被限制活动范围。"},
        {"id": "power_vacuum", "name": "权力真空", "initially_active": False,
         "description": "最高权力出现真空",
         "prompt_inject": "权力真空状态——没有明确的最高决策者，各派系都在争夺主导权，政令出多门，局势充满不确定性。"},
        {"id": "foreign_intervention", "name": "外部介入", "initially_active": False,
         "description": "美国等外部力量直接介入",
         "prompt_inject": "外部势力已直接介入局势——美国大使馆发出强硬声明，驻韩美军提升戒备等级，国际压力骤增。"},
        {"id": "underground_active", "name": "地下网络活跃", "initially_active": False,
         "description": "地下民主运动网络活跃",
         "prompt_inject": "地下网络正在活跃运作——秘密印刷品在教会和大学之间流通，联络人网络覆盖了主要城市。"},
        {"id": "purge_wave", "name": "清洗浪潮", "initially_active": False,
         "description": "政治清洗正在进行",
         "prompt_inject": "⚠ 清洗浪潮席卷军政两界——大批官员被免职或逮捕，人人自危，告密成风。"},
        {"id": "assassination_aftermath", "name": "刺杀余波", "initially_active": False,
         "description": "总统遇刺后的混乱期",
         "prompt_inject": "刺杀余波未平——整个政权处于震荡之中，军队在集结，情报机构陷入瘫痪，每个人都在揣测接下来谁会掌权。"},
        {"id": "ceasefire_tension", "name": "半岛紧张", "initially_active": False,
         "description": "朝鲜半岛军事紧张升级",
         "prompt_inject": "半岛紧张局势升级——北方在三八线附近增兵，驻韩美军进入警戒状态，任何内部动荡都可能被外敌利用。"},
        {"id": "curfew_enforced", "name": "宵禁执行中", "initially_active": False,
         "description": "全城宵禁",
         "prompt_inject": "宵禁令生效——夜间十时至凌晨四时禁止一切平民活动，违者可被就地拘押。巡逻队在街道上来回穿梭。"},
        {"id": "safe_passage", "name": "安全通道", "initially_active": False,
         "description": "玩家获得了安全通行权限",
         "prompt_inject": "你持有一份安全通行证——可以在大部分检查站自由通过，但这份文件的有效性随时可能被撤销。"},
    ]
    added = 0
    for s in new_states:
        if s["id"] not in existing:
            ps_list.append(s)
            existing.add(s["id"])
            added += 1
    return added


# ═══════════════════════════════════════════════
# 3. Triggers — 6 个
# ═══════════════════════════════════════════════
def add_triggers(script):
    triggers = script.setdefault("triggers", [])
    existing = {t["id"] for t in triggers if isinstance(t, dict) and t.get("id")}
    new_triggers = [
        {
            "id": "t_situation_inject",
            "name": "局势概览注入",
            "event": "before_generation",
            "condition": "",
            "action": "inject_prompt",
            "params": {
                "text": (
                    "[局势仪表盘] "
                    "政权稳定度={{var::regime_stability}}/100 | "
                    "军事戒备={{var::military_alert_level}}/4 | "
                    "抗议强度={{var::protest_intensity}}/100 | "
                    "新军部渗透={{var::hanahoe_infiltration}}/100 | "
                    "身份掩护={{var::cover_integrity}}/100 | "
                    "政变准备={{var::coup_readiness}}/100"
                )
            },
        },
        {
            "id": "t_high_danger",
            "name": "身份濒临暴露",
            "event": "before_generation",
            "condition": "script_variables.cover_integrity < 30",
            "action": "inject_prompt",
            "params": {
                "text": (
                    "⚠ 身份掩护度极低（{{var::cover_integrity}}）——"
                    "角色应感到被监视、被追踪的恐惧。NPC对话中应暗示怀疑和不信任。"
                    "描写中加入紧张的环境细节：跟踪的脚步声、可疑的车辆、电话中的杂音。"
                )
            },
        },
        {
            "id": "t_coup_imminent",
            "name": "政变倒计时",
            "event": "before_generation",
            "condition": "script_variables.coup_readiness >= 70",
            "action": "inject_prompt",
            "params": {
                "text": (
                    "⚠ 政变准备度极高（{{var::coup_readiness}}）——"
                    "空气中弥漫着山雨欲来的紧张感。军官们交换意味深长的眼神，"
                    "电话通讯变得频繁而简短，部队在深夜悄然调动。"
                    "叙事应传达出倒计时般的紧迫感。"
                )
            },
        },
        {
            "id": "t_diplomatic_alert",
            "name": "外交关注",
            "event": "before_generation",
            "condition": "script_variables.diplomatic_crisis_level >= 2",
            "action": "inject_prompt",
            "params": {
                "text": (
                    "国际社会高度关注韩国局势——"
                    "美国大使馆车辆频繁出入青瓦台，外国记者聚集在主要政府建筑外，"
                    "NHK和CNN的实况转播让全世界都在注视着这座城市。"
                )
            },
        },
        {
            "id": "t_protest_peak",
            "name": "抗议白热化",
            "event": "before_generation",
            "condition": "script_variables.protest_intensity >= 80",
            "action": "inject_prompt",
            "params": {
                "text": (
                    "⚠ 抗议运动已达白热化——"
                    "主要街道上到处是示威人群和催泪瓦斯的烟雾，"
                    "大学校园被占领，工厂开始罢工，商店纷纷关门。"
                    "环境描写应体现社会动荡的激烈程度。"
                )
            },
        },
        {
            "id": "t_trust_broken",
            "name": "信任崩塌",
            "event": "before_generation",
            "condition": "script_variables.betrayal_count >= 2",
            "action": "inject_prompt",
            "params": {
                "text": (
                    "你已多次背叛盟友——"
                    "曾经的同伴对你心存戒备，消息来源开始枯竭，"
                    "NPC在对话中应表现出疏远和不信任。"
                    "没有人愿意再把后背交给你。"
                )
            },
        },
    ]
    added = 0
    for t in new_triggers:
        if t["id"] not in existing:
            triggers.append(t)
            existing.add(t["id"])
            added += 1
    return added


# ═══════════════════════════════════════════════
# 4. Story Tree: 修复 set_var + 扩展 6 节点
# ═══════════════════════════════════════════════
def fix_and_expand_story_tree(script):
    st = script.get("story_tree", {})
    trees = st.get("trees", [])
    tree_map = {t["id"]: t for t in trees}

    # 4a. 修复 set_var 格式：target→var_id（仅简单变量名）, add→inc
    # 注意：带 "." 的 dotted path (如 world.社维压力) 保持 target 格式不变，
    # 因为引擎修复已支持 target 作为 var_id 的 fallback
    # 这里只需将 op:"add" 映射到引擎支持的 op
    # state_manager.apply_changes 支持 "add" 直接作为 op，所以无需修改
    fixed = 0

    # 4b. 扩展 story tree — 每条线新增 2 个节点
    new_nodes = {
        "yushin_collapse": [
            {
                "id": "yc_informant_reports",
                "name": "情报线人报告",
                "description": "你在情报系统中的线人定期传来关于高层动向的最新消息——谁在密会、谁在调兵、谁在准备跑路。",
                "type": "periodic",
                "requires": ["yc_busan_crisis"],
                "on_complete_unlock": [],
                "cooldown": 3,
                "weight": 15,
                "repeatable": True,
                "effects": {
                    "set_var": [{"var_id": "key_documents_found", "op": "inc", "value": 1}],
                    "inject_prompt": "线人传来新情报——关于高层人物近期活动的可靠消息。叙事中应体现情报收集的紧张感。",
                },
            },
            {
                "id": "yc_busan_escalation",
                "name": "釜山事态升级",
                "description": "釜山的抗议活动从街头蔓延到工厂区，工人阶级加入示威队伍，局势开始失控。",
                "type": "timed",
                "requires": ["yc_busan_crisis"],
                "on_complete_unlock": ["yc_assassination_decision"],
                "condition": "script_variables.protest_intensity >= 70",
                "duration_turns": 2,
                "effects": {
                    "set_var": [
                        {"var_id": "protest_intensity", "op": "inc", "value": 10},
                        {"var_id": "regime_stability", "op": "dec", "value": 10},
                    ],
                    "inject_prompt": "釜山事态急剧升级——工人罢工与学生示威合流，军队被迫出动但控制力不断削弱。",
                    "notify": "釜山局势进一步恶化，抗议规模扩大",
                },
            },
        ],
        "new_military_rise": [
            {
                "id": "nm_loyalty_purge",
                "name": "新军部清洗异己",
                "description": "全斗焕的一心会开始系统性地将不服从的军官调离关键岗位，替换为自己人。",
                "type": "periodic",
                "requires": ["nm_investigation_power"],
                "on_complete_unlock": [],
                "cooldown": 5,
                "weight": 12,
                "repeatable": True,
                "effects": {
                    "set_var": [
                        {"var_id": "hanahoe_infiltration", "op": "inc", "value": 5},
                        {"var_id": "troops_loyalty", "op": "dec", "value": 3},
                    ],
                    "inject_prompt": "又有军官被调离岗位——新军部的势力范围在悄然扩大。",
                    "activate_state": ["purge_wave"],
                },
            },
            {
                "id": "nm_arms_buildup",
                "name": "武器集结",
                "description": "新军部开始秘密调集武器装备，为可能的军事行动做准备。",
                "type": "timed",
                "requires": ["nm_evidence_gathering"],
                "on_complete_unlock": ["nm_1212_prelude"],
                "condition": "script_variables.weapons_secured >= 30",
                "duration_turns": 3,
                "effects": {
                    "set_var": [
                        {"var_id": "weapons_secured", "op": "inc", "value": 15},
                        {"var_id": "coup_readiness", "op": "inc", "value": 10},
                    ],
                    "inject_prompt": "武器和弹药正在秘密转移——新军部的战争机器已经启动。",
                    "notify": "情报显示大量武器正被秘密调集",
                },
            },
        ],
        "democratic_spring": [
            {
                "id": "ds_martyrdom",
                "name": "殉道事件",
                "description": "镇压中的平民伤亡激起了更广泛的同情和愤怒，牺牲者成为民主运动的象征。",
                "type": "auto",
                "requires": ["ds_first_protests"],
                "on_complete_unlock": ["ds_public_demand"],
                "condition": "script_variables.civilian_casualties >= 10",
                "effects": {
                    "set_var": [
                        {"var_id": "protest_intensity", "op": "inc", "value": 15},
                        {"var_id": "us_confidence", "op": "dec", "value": 10},
                    ],
                    "inject_prompt": "一位年轻示威者的死亡成为运动的转折点——鲜花和照片铺满了事发地点，哀悼的人群比抗议的人群更多。",
                    "notify": "殉道者的牺牲引发更大规模的民众抗议",
                    "narrative_callback": {"text": "将殉道事件的情感冲击融入叙事——这不仅是政治事件，更是人性的悲剧。", "priority": "high"},
                },
            },
            {
                "id": "ds_international_solidarity",
                "name": "国际声援",
                "description": "海外韩人社区和国际人权组织开始为韩国民主运动发声。",
                "type": "timed",
                "requires": ["ds_first_protests"],
                "on_complete_unlock": ["ds_public_demand"],
                "condition": "script_variables.diplomatic_crisis_level >= 2",
                "duration_turns": 3,
                "effects": {
                    "set_var": [
                        {"var_id": "diplomatic_crisis_level", "op": "inc", "value": 1},
                        {"var_id": "us_confidence", "op": "dec", "value": 5},
                    ],
                    "inject_prompt": "国际声援浪潮涌来——海外韩人社区在华盛顿、东京举行声援集会，国际特赦组织发表声明谴责。",
                    "notify": "国际社会开始为韩国民主运动发声",
                },
            },
        ],
    }
    added = 0
    for tree_id, nodes in new_nodes.items():
        tree = tree_map.get(tree_id)
        if not tree:
            continue
        existing_ids = {n["id"] for n in tree.get("nodes", [])}
        for node in nodes:
            if node["id"] not in existing_ids:
                tree["nodes"].append(node)
                added += 1
    return fixed, added


# ═══════════════════════════════════════════════
# 5. One-Time Events 增强
# ═══════════════════════════════════════════════
def enhance_one_time_events(script):
    events = script.get("one_time_events", [])
    eid_map = {e["id"]: e for e in events}

    enhancements = {
        # --- 政治/军事关键事件 ---
        "event_1026_incident": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "set", "value": 10},
            {"type": "activate_state", "id": "assassination_aftermath"},
            {"type": "activate_state", "id": "power_vacuum"},
            {"type": "narrative_callback", "text": "10.26事件——总统遇刺的震荡波及全国，叙事应体现极度的混乱和恐惧。", "priority": "critical"},
        ],
        "event_martial_law_declaration": [
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "set", "value": 4},
            {"type": "activate_state", "id": "military_lockdown"},
            {"type": "activate_state", "id": "curfew_enforced"},
            {"type": "state_change", "target": "script_variables.media_freedom", "op": "set", "value": 5},
            {"type": "activate_state", "id": "information_blackout"},
        ],
        "event_chun_joint_investigation": [
            {"type": "state_change", "target": "script_variables.hanahoe_infiltration", "op": "add", "value": 15},
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "全斗焕借合同搜查之名扩大权力——叙事中应暗示他的真实意图远不止调查。", "priority": "high"},
        ],
        "event_hanahoe_secret_plot": [
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 20},
            {"type": "state_change", "target": "script_variables.hanahoe_infiltration", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "一心会密谋——描写秘密会议的紧张氛围和参与者的不同态度。", "priority": "high"},
        ],
        "event_choi_president_election": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "崔圭夏当选代总统——一个没有实权的过渡人物，叙事应体现他的无力感。", "priority": "medium"},
        ],
        "event_1212_military_mutiny": [
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "set", "value": 100},
            {"type": "state_change", "target": "world_properties.new_military_influence", "op": "set", "value": 85},
            {"type": "activate_state", "id": "military_lockdown"},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "set", "value": 5},
            {"type": "narrative_callback", "text": "双十二兵变——枪声在汉城的冬夜里回响，一个时代在坦克履带下终结。", "priority": "critical"},
        ],
        # --- 釜马抗争事件 ---
        "event_1016_busan_protest": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 15},
            {"type": "state_change", "target": "world_properties.social_unrest_pressure", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "釜山抗议爆发——民众的愤怒如同火山喷发，叙事应体现从个人到集体的愤怒转化。", "priority": "high"},
        ],
        "event_1018_busan_martial_law": [
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "add", "value": 1},
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": -5},
            {"type": "state_change", "target": "script_variables.civilian_casualties", "op": "add", "value": 3},
        ],
        "event_masan_protest_spread": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 10},
            {"type": "state_change", "target": "script_variables.student_network_size", "op": "add", "value": 5},
        ],
        "event_seoul_student_protest_oct": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 8},
            {"type": "state_change", "target": "script_variables.student_network_size", "op": "add", "value": 5},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
        ],
        # --- 10.26 录像/情报事件 ---
        "event_1020_tape_viewing": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "车智澈和朴正熙观看釜山示威录像——总统的愤怒和情报部长的冷汗，两人之间的裂痕在加深。", "priority": "high"},
        ],
        "event_1025_tape_delivery": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 1},
            {"type": "narrative_callback", "text": "录音带送达——这盘录音成为压垮金载圭最后一根稻草，暗杀的决心在这一刻定格。", "priority": "critical"},
        ],
        "event_1026_chun_recommendation": [
            {"type": "state_change", "target": "script_variables.hanahoe_infiltration", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "车智澈向朴正熙推荐全斗焕——一个看似平常的人事建议，却为未来的政变埋下伏笔。", "priority": "medium"},
        ],
        "event_1026_gun_preparation": [
            {"type": "narrative_callback", "text": "金载圭从保险柜取出手枪——冰冷的金属握在掌心，历史在这一刻开始倒计时。", "priority": "critical"},
        ],
        "event_1026_dinner_invitation": [
            {"type": "narrative_callback", "text": "宴会邀请发出——一场精心设计的局，所有人都被引向宫井洞那间注定沾满鲜血的房间。", "priority": "high"},
        ],
        "event_1026_assassination_conspiracy": [
            {"type": "narrative_callback", "text": "暗杀阴谋的最后准备——金载圭的几个亲信已经知道今晚将发生什么，恐惧和决心交织在他们脸上。", "priority": "critical"},
        ],
        # --- 10.26 枪击现场事件 ---
        "event_1026_park_arrival": [
            {"type": "narrative_callback", "text": "朴正熙的车队驶入宫井洞——总统的最后一个黄昏，叙事应营造不祥的平静。", "priority": "high"},
        ],
        "event_1026_first_shot": [
            {"type": "narrative_callback", "text": "第一枪——金载圭拔枪射向车智澈，宴席上的杯盘碎裂，十八年独裁统治的倒计时开始。", "priority": "critical"},
        ],
        "event_1026_park_shot": [
            {"type": "narrative_callback", "text": "朴正熙中枪——子弹穿过总统的胸膛，十八年的维新体制在这一刻崩塌。", "priority": "critical"},
        ],
        "event_1026_gun_jam": [
            {"type": "narrative_callback", "text": "枪支卡壳——命运在这几十秒间摇摆，金载圭疯狂地排除故障，历史悬于一线。", "priority": "critical"},
        ],
        "event_1026_cha_killed": [
            {"type": "narrative_callback", "text": "车智澈被击毙——权力核心的另一个支柱倒下，白色的瓷砖被血染成暗红。", "priority": "high"},
        ],
        "event_1026_bodyguards_eliminated": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -20},
            {"type": "narrative_callback", "text": "警卫人员被清除——枪声平息后，宫井洞笼罩在硝烟和死寂之中。", "priority": "high"},
        ],
        "event_1026_witnesses_escape": [
            {"type": "narrative_callback", "text": "目击者逃离——几名女性在枪声中惊恐逃跑，她们将成为这个历史夜晚的关键证人。", "priority": "medium"},
        ],
        # --- 10.26 善后事件 ---
        "event_1026_body_transfer": [
            {"type": "narrative_callback", "text": "总统遗体被转移——在混乱和恐惧中，尸体被匆忙运往陆军医院。", "priority": "high"},
        ],
        "event_1026_army_hq_meeting": [
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "add", "value": 1},
            {"type": "narrative_callback", "text": "金载圭前往陆军本部——他试图将暗杀包装为正义之举，但军方将领的反应冰冷而充满敌意。", "priority": "high"},
        ],
        "event_1026_arrest_kim_jae_gyu": [
            {"type": "activate_state", "id": "assassination_aftermath"},
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "金载圭被逮捕——中央情报部长沦为阶下囚，情报系统瞬间陷入瘫痪。", "priority": "high"},
        ],
        "event_1027_choi_acting_president": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "崔圭夏就任代总统——一个温和的过渡人物，但真正的权力已经开始向别处转移。", "priority": "medium"},
        ],
        # --- 外交/国际反应 ---
        "event_1027_us_alert": [
            {"type": "state_change", "target": "script_variables.diplomatic_crisis_level", "op": "add", "value": 1},
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "add", "value": 1},
            {"type": "activate_state", "id": "ceasefire_tension"},
            {"type": "narrative_callback", "text": "美军进入戒备状态——小鹰号航母战斗群驶向朝鲜海峡，半岛局势骤然紧张。", "priority": "high"},
        ],
        "event_1027_us_statement": [
            {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.diplomatic_crisis_level", "op": "add", "value": 1},
        ],
        "event_japan_media_comment": [
            {"type": "state_change", "target": "script_variables.diplomatic_crisis_level", "op": "add", "value": 1},
            {"type": "narrative_callback", "text": "日本媒体大篇幅报道——国际舆论聚焦韩国局势，外交压力持续增大。", "priority": "medium"},
        ],
        # --- 12.12 兵变 ---
        "event_1212_hannam_raid": [
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "set", "value": 4},
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 20},
            {"type": "narrative_callback", "text": "突袭汉南洞——搜查队在夜色中包围郑升和的官邸，枪声打破了冬夜的寂静。", "priority": "critical"},
        ],
        "event_1212_rok_army_standoff": [
            {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": -20},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -15},
            {"type": "narrative_callback", "text": "陆军对峙——首都警备司令部与新军部的坦克在汉江桥头对峙，千钧一发。", "priority": "critical"},
        ],
        "event_1212_jang_tae_wan_arrest": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": -15},
            {"type": "narrative_callback", "text": "张泰玩被捕——首都警备司令的防线被突破，正统军方的最后堡垒沦陷。", "priority": "high"},
        ],
        # --- 其他 ---
        "event_1980_kim_jae_gyu_execution": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "金载圭被处决——绞刑架下，他的最后遗言是「为了民主化」。历史的审判远未结束。", "priority": "high"},
        ],
    }

    enhanced = 0
    for eid, effects in enhancements.items():
        evt = eid_map.get(eid)
        if evt is not None:
            evt["effects"] = effects
            enhanced += 1

    # 处理中文 ID 事件（安之阁宴会启程仪式开幕）
    for evt in events:
        if evt["id"] not in enhancements and "effects" not in evt:
            evt["effects"] = []
    return enhanced


# ═══════════════════════════════════════════════
# 6. Cyclic Events 增强 + 新增 1 个
# ═══════════════════════════════════════════════
def enhance_cyclic_events(script):
    cy_list = script.setdefault("cyclic_events", [])
    cy_map = {c["id"]: c for c in cy_list}

    enhancements = {
        "nightly_curfew": [
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -2},
        ],
        "kcia_morning_briefing": [
            {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 1},
        ],
        "buma_protest_updates": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 3},
            {"type": "state_change", "target": "world_properties.social_unrest_pressure", "op": "add", "value": 2},
        ],
        "hanahoe_secret_gathering": [
            {"type": "state_change", "target": "script_variables.hanahoe_infiltration", "op": "add", "value": 3},
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 2},
        ],
        "pss_loyalty_drill": [
            {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": 3},
        ],
        "seoul_campus_unrest": [
            {"type": "state_change", "target": "script_variables.student_network_size", "op": "add", "value": 2},
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 2},
        ],
        "underground_press_distribution": [
            {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": 1},
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 1},
        ],
    }

    enhanced = 0
    for cid, effects in enhancements.items():
        evt = cy_map.get(cid)
        if evt is not None:
            evt["effects"] = effects
            enhanced += 1

    # 新增: 财阀政治献金
    existing_ids = {c["id"] for c in cy_list}
    if "chaebol_political_donation" not in existing_ids:
        cy_list.append({
            "id": "chaebol_political_donation",
            "name": "财阀政治献金",
            "description": "大财阀集团定期向当权者输送政治资金，维持政商关系网络的运转。",
            "interval": "3d",
            "condition": "",
            "expires_at": None,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 2},
            ],
        })
        enhanced += 1

    return enhanced


# ═══════════════════════════════════════════════
# 7. Dynamic Events — 50 个
# ═══════════════════════════════════════════════
def add_dynamic_events(script):
    de_list = script.setdefault("dynamic_events", [])
    existing = {d["id"] for d in de_list if isinstance(d, dict) and d.get("id")}

    new_events = [
        # ── 政治博弈线 (10) ──
        {
            "id": "de_cabinet_reshuffle", "name": "内阁改组",
            "description": "政权稳定度下降引发内阁改组，各派系争夺关键职位。",
            "condition": "script_variables.regime_stability <= 50",
            "cooldown": 8, "weight": 12,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 5},
                {"type": "narrative_callback", "text": "内阁改组——新面孔被推上前台，但真正的权力博弈在幕后进行。", "priority": "medium"},
            ],
        },
        {
            "id": "de_opposition_rally", "name": "在野党集会",
            "description": "在野政治人物公开集会，要求政治改革。",
            "condition": "script_variables.regime_stability <= 60",
            "cooldown": 6, "weight": 14,
            "effects": [
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -3},
                {"type": "narrative_callback", "text": "在野党领袖在集会上慷慨陈词——要求结束军事独裁，恢复民主宪政。", "priority": "medium"},
            ],
        },
        {
            "id": "de_power_broker_deal", "name": "权力掮客交易",
            "description": "政治掮客在各派系之间穿针引线，谋求利益交换。",
            "condition": "script_variables.regime_stability <= 70",
            "cooldown": 7, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.intelligence_leaks", "op": "add", "value": 1},
                {"type": "narrative_callback", "text": "权力掮客在密室中运作——信息就是货币，忠诚可以买卖。", "priority": "low"},
            ],
        },
        {
            "id": "de_constitutional_debate", "name": "宪法修改争论",
            "description": "朝野围绕宪法修改展开激烈争论，维新宪法的存废成为焦点。",
            "condition": "script_variables.regime_stability <= 40",
            "cooldown": 10, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 3},
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
            ],
        },
        {
            "id": "de_faction_split", "name": "派系分裂",
            "description": "执政势力内部出现严重分裂，不同派系公开对立。",
            "condition": "script_variables.regime_stability <= 30",
            "cooldown": 12, "weight": 6,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
                {"type": "activate_state", "id": "power_vacuum"},
            ],
            "chain_events": [{"event_id": "de_succession_crisis", "delay_turns": 2}],
        },
        {
            "id": "de_emergency_cabinet", "name": "紧急国务会议",
            "description": "军事戒备升级触发紧急国务会议。",
            "condition": "script_variables.military_alert_level >= 3",
            "cooldown": 6, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -3},
                {"type": "narrative_callback", "text": "紧急国务会议在深夜召开——部长们面色凝重，军方代表的声音越来越大。", "priority": "high"},
            ],
        },
        {
            "id": "de_political_prisoner_release", "name": "政治犯释放",
            "description": "迫于压力释放部分政治犯，引发连锁反应。",
            "condition": "script_variables.protest_intensity >= 70",
            "cooldown": 10, "weight": 7,
            "effects": [
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": -5},
                {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -3},
            ],
        },
        {
            "id": "de_censorship_order", "name": "新闻审查令",
            "description": "当局颁布更严格的新闻审查令。",
            "condition": "script_variables.media_freedom >= 40",
            "cooldown": 8, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": -15},
                {"type": "activate_state", "id": "information_blackout"},
            ],
            "chain_events": [{"event_id": "de_underground_radio", "delay_turns": 2}],
        },
        {
            "id": "de_backroom_deal", "name": "密室交易",
            "description": "各方势力在幕后进行秘密谈判。",
            "condition": "script_variables.regime_stability >= 30 AND script_variables.regime_stability <= 70",
            "cooldown": 7, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -3},
                {"type": "narrative_callback", "text": "密室中的交易——没有人知道完整的棋局，每个人都只是棋子。", "priority": "medium"},
            ],
        },
        {
            "id": "de_succession_crisis", "name": "继承危机",
            "description": "最高权力的继承问题引发严重危机。",
            "condition": "script_variables.regime_stability <= 20",
            "cooldown": 15, "weight": 5,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
                {"type": "activate_state", "id": "power_vacuum"},
                {"type": "narrative_callback", "text": "继承危机——王座空悬，野心家们磨刀霍霍。", "priority": "critical"},
            ],
            "chain_events": [{"event_id": "de_regime_collapse_signal", "delay_turns": 2}],
        },

        # ── 军事动向线 (10) ──
        {
            "id": "de_troop_movement", "name": "部队调动",
            "description": "异常的部队调动引起注意。",
            "condition": "script_variables.military_alert_level >= 2",
            "cooldown": 5, "weight": 14,
            "effects": [
                {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 3},
                {"type": "narrative_callback", "text": "深夜的军用卡车队伍穿过汉城街头——没有人知道他们要去哪里。", "priority": "medium"},
            ],
            "chain_events": [{"event_id": "de_garrison_alert", "delay_turns": 1}],
        },
        {
            "id": "de_loyalty_test", "name": "忠诚度测试",
            "description": "新军部对关键岗位军官进行忠诚度试探。",
            "condition": "script_variables.hanahoe_infiltration >= 40",
            "cooldown": 7, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": -5},
                {"type": "state_change", "target": "script_variables.hanahoe_infiltration", "op": "add", "value": 3},
            ],
        },
        {
            "id": "de_weapons_cache", "name": "武器库发现",
            "description": "发现秘密武器储备。",
            "condition": "script_variables.weapons_secured >= 20",
            "cooldown": 10, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.weapons_secured", "op": "add", "value": 10},
                {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 1},
            ],
        },
        {
            "id": "de_desertion_incident", "name": "逃兵事件",
            "description": "士兵开小差事件增多，军纪松弛的信号。",
            "condition": "script_variables.troops_loyalty <= 30",
            "cooldown": 8, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": -5},
                {"type": "state_change", "target": "script_variables.military_alert_level", "op": "add", "value": 1},
            ],
            "chain_events": [{"event_id": "de_military_standoff", "delay_turns": 2}],
        },
        {
            "id": "de_military_exercise", "name": "军事演习",
            "description": "名为演习实为部署的军事行动。",
            "condition": "script_variables.military_alert_level >= 2",
            "cooldown": 8, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.weapons_secured", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 5},
            ],
        },
        {
            "id": "de_hanahoe_recruitment", "name": "一心会招募",
            "description": "一心会吸收新成员，势力范围扩大。",
            "condition": "script_variables.hanahoe_infiltration >= 30",
            "cooldown": 6, "weight": 12,
            "effects": [
                {"type": "state_change", "target": "script_variables.hanahoe_infiltration", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 3},
            ],
            "chain_events": [{"event_id": "de_coup_countdown", "delay_turns": 5}],
        },
        {
            "id": "de_communication_intercept", "name": "通讯截获",
            "description": "截获可疑通讯内容。",
            "condition": "script_variables.communication_security <= 50",
            "cooldown": 6, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 1},
                {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -5},
            ],
        },
        {
            "id": "de_garrison_alert", "name": "驻军警戒",
            "description": "驻军进入高度警戒状态。",
            "condition": "script_variables.military_alert_level >= 3",
            "cooldown": 5, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -5},
                {"type": "state_change", "target": "script_variables.military_alert_level", "op": "add", "value": 1},
                {"type": "narrative_callback", "text": "驻军全面警戒——检查站增加，盘查更加严格，空气中弥漫着火药味。", "priority": "high"},
            ],
        },
        {
            "id": "de_arms_smuggling", "name": "武器走私",
            "description": "地下武器走私网络活跃。",
            "condition": "script_variables.weapons_secured >= 40",
            "cooldown": 10, "weight": 6,
            "effects": [
                {"type": "state_change", "target": "script_variables.weapons_secured", "op": "add", "value": 8},
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -5},
            ],
        },
        {
            "id": "de_military_tribunal", "name": "军事法庭",
            "description": "异见军官被送上军事法庭。",
            "condition": "script_variables.troops_loyalty <= 40",
            "cooldown": 10, "weight": 7,
            "effects": [
                {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -3},
                {"type": "narrative_callback", "text": "军事法庭开庭——被告席上坐着的是昨天的战友，审判席上坐着的是明天的敌人。", "priority": "medium"},
            ],
        },

        # ── 民众运动线 (10) ──
        {
            "id": "de_campus_rally", "name": "校园集会",
            "description": "大学校园内爆发大规模集会。",
            "condition": "script_variables.protest_intensity >= 50",
            "cooldown": 5, "weight": 14,
            "effects": [
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.student_network_size", "op": "add", "value": 3},
            ],
            "chain_events": [{"event_id": "de_crackdown", "delay_turns": 2}],
        },
        {
            "id": "de_labor_strike", "name": "工人罢工",
            "description": "工厂工人发起罢工行动。",
            "condition": "script_variables.protest_intensity >= 60",
            "cooldown": 8, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 8},
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
            ],
            "chain_events": [{"event_id": "de_general_strike", "delay_turns": 3}],
        },
        {
            "id": "de_crackdown", "name": "镇压行动",
            "description": "当局对示威者进行暴力镇压。",
            "condition": "script_variables.protest_intensity >= 50 AND script_variables.military_alert_level >= 2",
            "cooldown": 5, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.civilian_casualties", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": -10},
                {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -5},
                {"type": "narrative_callback", "text": "镇压——催泪瓦斯和警棍落下，鲜血染红了大学门前的石阶。", "priority": "high"},
            ],
            "chain_events": [{"event_id": "de_international_outcry", "delay_turns": 1}],
        },
        {
            "id": "de_international_outcry", "name": "国际抗议",
            "description": "国际社会对镇压行动表示强烈谴责。",
            "condition": "script_variables.civilian_casualties >= 5",
            "cooldown": 6, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.diplomatic_crisis_level", "op": "add", "value": 1},
                {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -10},
            ],
        },
        {
            "id": "de_underground_leaflet", "name": "地下传单",
            "description": "秘密印刷的传单在城市中流传。",
            "condition": "script_variables.student_network_size >= 20",
            "cooldown": 4, "weight": 14,
            "effects": [
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 3},
                {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": 2},
                {"type": "activate_state", "id": "underground_active"},
            ],
        },
        {
            "id": "de_media_expose", "name": "媒体揭露",
            "description": "媒体揭露政府的秘密行动。",
            "condition": "script_variables.media_freedom >= 30",
            "cooldown": 8, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
            ],
            "chain_events": [{"event_id": "de_foreign_media_frenzy", "delay_turns": 1}],
        },
        {
            "id": "de_candlelight_vigil", "name": "烛光祈祷会",
            "description": "民众为伤亡者举行烛光祈祷会。",
            "condition": "script_variables.civilian_casualties >= 5",
            "cooldown": 6, "weight": 12,
            "effects": [
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -3},
                {"type": "narrative_callback", "text": "烛光祈祷会——成千上万支蜡烛在夜色中摇曳，沉默比呐喊更有力量。", "priority": "high"},
            ],
        },
        {
            "id": "de_student_arrest", "name": "学生逮捕",
            "description": "大批学生运动领袖被逮捕。",
            "condition": "script_variables.student_network_size >= 30",
            "cooldown": 8, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.student_network_size", "op": "add", "value": -10},
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": -5},
                {"type": "state_change", "target": "script_variables.civilian_casualties", "op": "add", "value": 2},
            ],
            "chain_events": [{"event_id": "de_candlelight_vigil", "delay_turns": 1}],
        },
        {
            "id": "de_church_sanctuary", "name": "教会庇护",
            "description": "教会为被追捕的民主人士提供庇护。",
            "condition": "script_variables.protest_intensity >= 40",
            "cooldown": 8, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.safe_house_count", "op": "add", "value": 1},
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": 5},
                {"type": "narrative_callback", "text": "教会敞开大门——十字架下的庇护所，成为黑暗时代中最后的光亮。", "priority": "medium"},
            ],
        },
        {
            "id": "de_mothers_march", "name": "母亲游行",
            "description": "牺牲者的母亲们手持遗像走上街头。",
            "condition": "script_variables.civilian_casualties >= 10",
            "cooldown": 10, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 10},
                {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -5},
                {"type": "narrative_callback", "text": "母亲们的游行——黑色的衣裙，白色的遗像，无声的控诉比任何口号都更令人动容。", "priority": "critical"},
            ],
        },

        # ── 情报生存线 (10) ──
        {
            "id": "de_cover_blown_rumor", "name": "身份暴露传闻",
            "description": "关于你真实身份的传闻开始流传。",
            "condition": "script_variables.cover_integrity <= 50",
            "cooldown": 6, "weight": 12,
            "effects": [
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -10},
                {"type": "narrative_callback", "text": "有人在打听你的底细——传闻像野火一样蔓延，你能感到周围的目光在变化。", "priority": "high"},
            ],
            "chain_events": [{"event_id": "de_safe_house_raid", "delay_turns": 2}],
        },
        {
            "id": "de_safe_house_raid", "name": "安全屋搜查",
            "description": "当局搜查了你的一个安全屋。",
            "condition": "script_variables.safe_house_count >= 1 AND script_variables.cover_integrity <= 40",
            "cooldown": 8, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.safe_house_count", "op": "add", "value": -1},
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -10},
                {"type": "narrative_callback", "text": "安全屋被搜查——你到达时只看到被翻过的抽屉和踹开的门，他们比你早到了半小时。", "priority": "high"},
            ],
        },
        {
            "id": "de_document_leak", "name": "文件泄露",
            "description": "你手中的关键文件信息被泄露。",
            "condition": "script_variables.key_documents_found >= 5",
            "cooldown": 10, "weight": 6,
            "effects": [
                {"type": "state_change", "target": "script_variables.intelligence_leaks", "op": "add", "value": 1},
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -8},
            ],
            "chain_events": [{"event_id": "de_interrogation_threat", "delay_turns": 3}],
        },
        {
            "id": "de_double_agent_approach", "name": "双面间谍接触",
            "description": "有人试图策反你为另一方工作。",
            "condition": "script_variables.intelligence_leaks >= 2",
            "cooldown": 12, "weight": 6,
            "effects": [
                {"type": "narrative_callback", "text": "一个不该出现在这里的人找到了你——他的提议危险而诱人：为两边工作，换取安全保障。", "priority": "high"},
            ],
        },
        {
            "id": "de_surveillance_detected", "name": "发现监控",
            "description": "你发现自己正被监视。",
            "condition": "script_variables.cover_integrity <= 60",
            "cooldown": 5, "weight": 12,
            "effects": [
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -5},
                {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -5},
                {"type": "narrative_callback", "text": "街角那辆黑色轿车已经停了三天了——你开始注意到跟踪你的人。", "priority": "medium"},
            ],
        },
        {
            "id": "de_informant_betrayal", "name": "线人背叛",
            "description": "你的一个线人被策反或背叛了你。",
            "condition": "script_variables.betrayal_count >= 1",
            "cooldown": 10, "weight": 6,
            "effects": [
                {"type": "state_change", "target": "script_variables.betrayal_count", "op": "add", "value": 1},
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -15},
                {"type": "narrative_callback", "text": "线人的背叛——你最信任的消息来源把你卖了，现在你的一切行踪都暴露在敌人面前。", "priority": "critical"},
            ],
            "chain_events": [{"event_id": "de_escape_route", "delay_turns": 2}],
        },
        {
            "id": "de_coded_message", "name": "密码消息",
            "description": "收到一条加密的紧急消息。",
            "condition": "script_variables.communication_security >= 50",
            "cooldown": 5, "weight": 12,
            "effects": [
                {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 1},
                {"type": "narrative_callback", "text": "一条加密消息通过安全渠道送达——三行数字背后隐藏着可能改变局势的情报。", "priority": "medium"},
            ],
        },
        {
            "id": "de_dead_drop", "name": "死信投递",
            "description": "在约定地点发现秘密投递的情报。",
            "condition": "script_variables.key_documents_found >= 3",
            "cooldown": 6, "weight": 10,
            "effects": [
                {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 2},
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -3},
            ],
        },
        {
            "id": "de_interrogation_threat", "name": "审讯威胁",
            "description": "你面临被拘捕和审讯的威胁。",
            "condition": "script_variables.cover_integrity <= 30",
            "cooldown": 8, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -10},
                {"type": "activate_state", "id": "player_hunted"},
                {"type": "narrative_callback", "text": "审讯室的灯光已经为你准备好了——逮捕令上写着你的名字。", "priority": "critical"},
            ],
        },
        {
            "id": "de_escape_route", "name": "逃跑路线",
            "description": "在绝境中发现一条逃跑路线。",
            "condition": "script_variables.cover_integrity <= 20",
            "cooldown": 10, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": 10},
                {"type": "activate_state", "id": "safe_passage"},
                {"type": "narrative_callback", "text": "一条秘密通道——教会地下室通往城外的水渠，这可能是你最后的退路。", "priority": "high"},
            ],
        },

        # ── 跨线联动线 (10) ──
        {
            "id": "de_us_ultimatum", "name": "美方最后通牒",
            "description": "美国政府发出强硬警告。",
            "condition": "script_variables.diplomatic_crisis_level >= 3",
            "cooldown": 15, "weight": 5,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
                {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -15},
                {"type": "activate_state", "id": "foreign_intervention"},
                {"type": "narrative_callback", "text": "美国大使递交了措辞严厉的照会——如果不停止镇压，一切后果自负。", "priority": "critical"},
            ],
        },
        {
            "id": "de_martial_law_extension", "name": "戒严扩大",
            "description": "戒严范围从部分地区扩大到全国。",
            "condition": "script_variables.military_alert_level >= 3 AND script_variables.protest_intensity >= 60",
            "cooldown": 12, "weight": 7,
            "effects": [
                {"type": "state_change", "target": "script_variables.military_alert_level", "op": "set", "value": 4},
                {"type": "activate_state", "id": "military_lockdown"},
                {"type": "activate_state", "id": "curfew_enforced"},
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": -15},
                {"type": "state_change", "target": "script_variables.media_freedom", "op": "set", "value": 5},
            ],
            "chain_events": [{"event_id": "de_peace_negotiation", "delay_turns": 3}],
        },
        {
            "id": "de_general_strike", "name": "总罢工",
            "description": "全国性总罢工爆发。",
            "condition": "script_variables.protest_intensity >= 80 AND script_variables.student_network_size >= 40",
            "cooldown": 15, "weight": 4,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -20},
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "set", "value": 100},
                {"type": "narrative_callback", "text": "总罢工——工厂停产，商店关门，公交停运，整座城市陷入瘫痪。", "priority": "critical"},
            ],
        },
        {
            "id": "de_coup_countdown", "name": "政变倒计时",
            "description": "政变准备进入最后阶段。",
            "condition": "script_variables.coup_readiness >= 80",
            "cooldown": 10, "weight": 6,
            "effects": [
                {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 10},
                {"type": "state_change", "target": "script_variables.military_alert_level", "op": "set", "value": 4},
                {"type": "narrative_callback", "text": "倒计时已经开始——军靴声在深夜的走廊里回响，命令在加密频道中传递。", "priority": "critical"},
            ],
        },
        {
            "id": "de_regime_collapse_signal", "name": "政权崩溃信号",
            "description": "政权崩溃的前兆开始显现。",
            "condition": "script_variables.regime_stability <= 10",
            "cooldown": 15, "weight": 4,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "set", "value": 0},
                {"type": "activate_state", "id": "power_vacuum"},
                {"type": "narrative_callback", "text": "政权崩溃——高官们开始销毁文件、转移资产，大楼里弥漫着纸灰的味道。", "priority": "critical"},
            ],
        },
        {
            "id": "de_foreign_media_frenzy", "name": "外媒疯狂",
            "description": "外国媒体大规模涌入报道韩国局势。",
            "condition": "script_variables.diplomatic_crisis_level >= 2 AND script_variables.civilian_casualties >= 5",
            "cooldown": 8, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.diplomatic_crisis_level", "op": "add", "value": 1},
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
                {"type": "narrative_callback", "text": "外国记者蜂拥而至——他们的镜头比任何武器都更让当局恐惧。", "priority": "high"},
            ],
        },
        {
            "id": "de_underground_radio", "name": "地下广播",
            "description": "秘密广播电台开始播报被审查的消息。",
            "condition": "script_variables.media_freedom <= 10 AND script_variables.student_network_size >= 30",
            "cooldown": 8, "weight": 8,
            "effects": [
                {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
                {"type": "activate_state", "id": "underground_active"},
                {"type": "narrative_callback", "text": "地下广播——「这里是自由汉城之声」，微弱但清晰的信号穿透了审查的铁幕。", "priority": "high"},
            ],
        },
        {
            "id": "de_military_standoff", "name": "军事对峙",
            "description": "不同派系的军队之间爆发对峙。",
            "condition": "script_variables.troops_loyalty <= 30 AND script_variables.coup_readiness >= 50",
            "cooldown": 12, "weight": 5,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -15},
                {"type": "state_change", "target": "script_variables.military_alert_level", "op": "set", "value": 4},
                {"type": "narrative_callback", "text": "军事对峙——两支部队在桥头遥遥相望，坦克炮管指向彼此，整座城市屏住呼吸。", "priority": "critical"},
            ],
        },
        {
            "id": "de_peace_negotiation", "name": "和平谈判",
            "description": "在国际压力下，各方被迫坐到谈判桌前。",
            "condition": "script_variables.diplomatic_crisis_level >= 2 AND script_variables.regime_stability <= 40",
            "cooldown": 12, "weight": 6,
            "effects": [
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 5},
                {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": -5},
                {"type": "narrative_callback", "text": "谈判桌上——各方代表面色如铁，每一个让步都像割肉，但战争的代价更大。", "priority": "high"},
            ],
        },
        {
            "id": "de_point_of_no_return", "name": "不归点",
            "description": "局势发展到了无法逆转的临界点。",
            "condition": "script_variables.coup_readiness >= 90 AND script_variables.regime_stability <= 20",
            "cooldown": 20, "weight": 3,
            "effects": [
                {"type": "state_change", "target": "script_variables.coup_readiness", "op": "set", "value": 100},
                {"type": "state_change", "target": "script_variables.regime_stability", "op": "set", "value": 0},
                {"type": "activate_state", "id": "military_lockdown"},
                {"type": "narrative_callback", "text": "不归点——历史的齿轮已经咬合，没有任何力量能阻止接下来发生的事情。", "priority": "critical"},
            ],
        },
    ]

    added = 0
    for de in new_events:
        if de["id"] not in existing:
            de_list.append(de)
            existing.add(de["id"])
            added += 1
    return added


# ═══════════════════════════════════════════════
# 8. Attribute Thresholds — 5属性 × 2条
# ═══════════════════════════════════════════════
def add_attribute_thresholds(script):
    pc = script.get("player_character", {})
    attrs = pc.get("attributes", {})

    thresholds_map = {
        "政治嗅觉": [
            {"value": 80, "direction": "above", "description": "你对政治局势的判断几乎从不失误，各方势力都在试图拉拢你。"},
            {"value": 20, "direction": "below", "description": "你对政治博弈的理解严重不足，频繁做出错误判断，正在被边缘化。"},
        ],
        "人脉网络": [
            {"value": 80, "direction": "above", "description": "你已建立起高效的人脉网络，关键信息总能第一时间传到你手中。"},
            {"value": 20, "direction": "below", "description": "人脉资源枯竭，你对局势的了解严重滞后，行动几乎是盲目的。"},
        ],
        "资金储备": [
            {"value": 75, "direction": "above", "description": "充裕的资金让你可以自由运作——行贿、购买情报、资助行动，无需顾虑成本。"},
            {"value": 20, "direction": "below", "description": "资金几近枯竭，连基本的行动开销都难以维持，你不得不向不该求助的人借钱。"},
        ],
        "行动力": [
            {"value": 80, "direction": "above", "description": "你的执行力令人敬畏——行动果断、反应迅速，每一步都精准到位。"},
            {"value": 25, "direction": "below", "description": "精疲力竭——连续的高压行动已经把你逼到了极限，反应迟钝，判断力下降。"},
        ],
        "公众声望": [
            {"value": 80, "direction": "above", "description": "你已成为公众人物——你的一言一行都被关注，这既是力量也是危险。"},
            {"value": 25, "direction": "below", "description": "你在公众视野中几乎不存在，没有人会听你的声音，也没有人会为你发声。"},
        ],
    }

    added = 0
    for attr_name, thresholds in thresholds_map.items():
        if attr_name in attrs:
            existing_th = attrs[attr_name].get("thresholds", [])
            existing_vals = {(t.get("value"), t.get("direction")) for t in existing_th}
            for t in thresholds:
                if (t["value"], t["direction"]) not in existing_vals:
                    existing_th.append(t)
                    added += 1
            attrs[attr_name]["thresholds"] = existing_th
    return added


# ═══════════════════════════════════════════════
# 9. Opening Choices State Changes
# ═══════════════════════════════════════════════
def add_opening_choice_state_changes(script):
    presets = script.get("player_presets", [])
    preset_map = {p["id"]: p for p in presets}

    sc_defs = {
        "kcia_operative": {
            "open_0": [{"target": "script_variables.key_documents_found", "op": "add", "value": 2},
                       {"target": "player.人脉网络", "op": "add", "value": 5}],
            "open_1": [{"target": "script_variables.hanahoe_infiltration", "op": "add", "value": 5},
                       {"target": "script_variables.cover_integrity", "op": "add", "value": -10}],
            "open_2": [{"target": "script_variables.safe_house_count", "op": "add", "value": 1},
                       {"target": "player.资金储备", "op": "add", "value": 5}],
        },
        "hanahoe_officer": {
            "open_0": [{"target": "script_variables.coup_readiness", "op": "add", "value": 5},
                       {"target": "player.行动力", "op": "add", "value": 5}],
            "open_1": [{"target": "script_variables.hanahoe_infiltration", "op": "add", "value": 5},
                       {"target": "player.政治嗅觉", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.weapons_secured", "op": "add", "value": 10},
                       {"target": "player.行动力", "op": "add", "value": 5}],
        },
        "dissident_journalist": {
            "open_0": [{"target": "script_variables.media_freedom", "op": "add", "value": 5},
                       {"target": "player.公众声望", "op": "add", "value": 5}],
            "open_1": [{"target": "script_variables.student_network_size", "op": "add", "value": 5},
                       {"target": "player.人脉网络", "op": "add", "value": 3}],
            "open_2": [{"target": "script_variables.cover_integrity", "op": "add", "value": -5},
                       {"target": "player.公众声望", "op": "add", "value": 8}],
            "open_3": [{"target": "script_variables.key_documents_found", "op": "add", "value": 1},
                       {"target": "player.人脉网络", "op": "add", "value": 5}],
        },
        "power_broker": {
            "open_0": [{"target": "script_variables.regime_stability", "op": "add", "value": 3},
                       {"target": "player.资金储备", "op": "add", "value": 10}],
            "open_1": [{"target": "script_variables.intelligence_leaks", "op": "add", "value": 1},
                       {"target": "player.政治嗅觉", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.us_confidence", "op": "add", "value": 5},
                       {"target": "player.公众声望", "op": "add", "value": 5}],
            "open_3": [{"target": "player.资金储备", "op": "add", "value": 5},
                       {"target": "script_variables.cover_integrity", "op": "add", "value": -5}],
        },
        "pss_bodyguard": {
            "open_0": [{"target": "script_variables.troops_loyalty", "op": "add", "value": 5},
                       {"target": "player.行动力", "op": "add", "value": 5}],
            "open_1": [{"target": "script_variables.key_documents_found", "op": "add", "value": 1},
                       {"target": "player.人脉网络", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.cover_integrity", "op": "add", "value": 5},
                       {"target": "player.行动力", "op": "add", "value": 3}],
            "open_3": [{"target": "script_variables.military_alert_level", "op": "add", "value": 1},
                       {"target": "player.政治嗅觉", "op": "add", "value": 3}],
        },
        "orthodox_colonel": {
            "open_0": [{"target": "script_variables.troops_loyalty", "op": "add", "value": 10},
                       {"target": "player.行动力", "op": "add", "value": 5}],
            "open_1": [{"target": "script_variables.regime_stability", "op": "add", "value": 5},
                       {"target": "player.政治嗅觉", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.hanahoe_infiltration", "op": "add", "value": -5},
                       {"target": "player.人脉网络", "op": "add", "value": 5}],
            "open_3": [{"target": "script_variables.communication_security", "op": "add", "value": 5},
                       {"target": "player.行动力", "op": "add", "value": 3}],
        },
        "student_activist": {
            "open_0": [{"target": "script_variables.student_network_size", "op": "add", "value": 10},
                       {"target": "player.公众声望", "op": "add", "value": 5}],
            "open_1": [{"target": "script_variables.protest_intensity", "op": "add", "value": 5},
                       {"target": "player.行动力", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.media_freedom", "op": "add", "value": 3},
                       {"target": "player.公众声望", "op": "add", "value": 5}],
            "open_3": [{"target": "script_variables.cover_integrity", "op": "add", "value": -10},
                       {"target": "player.公众声望", "op": "add", "value": 8}],
        },
        "us_embassy_liaison": {
            "open_0": [{"target": "script_variables.us_confidence", "op": "add", "value": 10},
                       {"target": "player.政治嗅觉", "op": "add", "value": 5}],
            "open_1": [{"target": "script_variables.diplomatic_crisis_level", "op": "add", "value": 1},
                       {"target": "player.人脉网络", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.key_documents_found", "op": "add", "value": 2},
                       {"target": "player.人脉网络", "op": "add", "value": 3}],
            "open_3": [{"target": "script_variables.regime_stability", "op": "add", "value": -3},
                       {"target": "player.公众声望", "op": "add", "value": 5}],
        },
        "chaebol_fixer": {
            "open_0": [{"target": "player.资金储备", "op": "add", "value": 15},
                       {"target": "script_variables.regime_stability", "op": "add", "value": 3}],
            "open_1": [{"target": "script_variables.intelligence_leaks", "op": "add", "value": 1},
                       {"target": "player.资金储备", "op": "add", "value": 10}],
            "open_2": [{"target": "script_variables.cover_integrity", "op": "add", "value": -5},
                       {"target": "player.政治嗅觉", "op": "add", "value": 5}],
            "open_3": [{"target": "player.资金储备", "op": "add", "value": 8},
                       {"target": "player.公众声望", "op": "add", "value": 3}],
        },
        "nk_sleeper_agent": {
            "open_0": [{"target": "script_variables.cover_integrity", "op": "add", "value": -15},
                       {"target": "player.人脉网络", "op": "add", "value": 10}],
            "open_1": [{"target": "script_variables.communication_security", "op": "add", "value": -10},
                       {"target": "player.行动力", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.safe_house_count", "op": "add", "value": 1},
                       {"target": "player.人脉网络", "op": "add", "value": 5}],
            "open_3": [{"target": "script_variables.key_documents_found", "op": "add", "value": 3},
                       {"target": "script_variables.cover_integrity", "op": "add", "value": -10}],
        },
        "blue_house_secretary": {
            "open_0": [{"target": "script_variables.regime_stability", "op": "add", "value": 5},
                       {"target": "player.政治嗅觉", "op": "add", "value": 8}],
            "open_1": [{"target": "script_variables.key_documents_found", "op": "add", "value": 2},
                       {"target": "player.人脉网络", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.us_confidence", "op": "add", "value": 5},
                       {"target": "player.公众声望", "op": "add", "value": 5}],
        },
        "opposition_aide": {
            "open_0": [{"target": "script_variables.protest_intensity", "op": "add", "value": 5},
                       {"target": "player.公众声望", "op": "add", "value": 5}],
            "open_1": [{"target": "script_variables.student_network_size", "op": "add", "value": 5},
                       {"target": "player.政治嗅觉", "op": "add", "value": 5}],
            "open_2": [{"target": "script_variables.media_freedom", "op": "add", "value": 3},
                       {"target": "player.人脉网络", "op": "add", "value": 5}],
            "open_3": [{"target": "script_variables.cover_integrity", "op": "add", "value": -5},
                       {"target": "player.行动力", "op": "add", "value": 5}],
        },
    }

    updated = 0
    for preset_id, questions in sc_defs.items():
        preset = preset_map.get(preset_id)
        if not preset:
            continue
        oc_list = preset.get("opening_choices", [])
        for q in oc_list:
            qid = q.get("id", "")
            sc = questions.get(qid)
            if sc is not None:
                result = q.get("result", {})
                result["state_changes"] = sc
                q["result"] = result
                updated += 1
    return updated


# ═══════════════════════════════════════════════
# 10. Validation
# ═══════════════════════════════════════════════
def validate(script):
    errors = []
    warnings = []

    # Collect all IDs
    var_ids = {v["id"] for v in script.get("variables", [])}
    ps_ids = {p["id"] for p in script.get("persistent_states", [])}
    de_ids = {d["id"] for d in script.get("dynamic_events", []) if isinstance(d, dict)}
    ot_ids = {e["id"] for e in script.get("one_time_events", []) if isinstance(e, dict)}
    cy_ids = {c["id"] for c in script.get("cyclic_events", []) if isinstance(c, dict)}

    # Check dynamic event chain references
    for de in script.get("dynamic_events", []):
        for chain in de.get("chain_events", []):
            cid = chain.get("event_id", "")
            if cid and cid not in de_ids:
                errors.append(f"chain_event '{cid}' (from {de['id']}) not in dynamic_events")

    # Check activate_state references in dynamic events
    for de in script.get("dynamic_events", []):
        for eff in de.get("effects", []):
            if eff.get("type") == "activate_state":
                sid = eff.get("id", "")
                if sid and sid not in ps_ids:
                    errors.append(f"activate_state '{sid}' (from de:{de['id']}) not in persistent_states")

    # Check activate_state references in one-time events
    for evt in script.get("one_time_events", []):
        for eff in evt.get("effects", []):
            if isinstance(eff, dict) and eff.get("type") == "activate_state":
                sid = eff.get("id", "")
                if sid and sid not in ps_ids:
                    errors.append(f"activate_state '{sid}' (from ot:{evt['id']}) not in persistent_states")

    # Check trigger {{var::}} references
    for t in script.get("triggers", []):
        text = t.get("params", {}).get("text", "")
        import re
        for m in re.finditer(r'\{\{var::(\w+)\}\}', text):
            vid = m.group(1)
            if vid not in var_ids:
                errors.append(f"trigger '{t['id']}' references unknown variable '{{{{var::{vid}}}}}'")

    # Check story tree set_var references
    st = script.get("story_tree", {})
    for tree in st.get("trees", []):
        for node in tree.get("nodes", []):
            for sv in node.get("effects", {}).get("set_var", []):
                vid = sv.get("var_id", "")
                if vid and "." not in vid and vid not in var_ids:
                    warnings.append(f"story tree node '{node['id']}' set_var references unknown var '{vid}'")

    return errors, warnings


# ═══════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════
def main():
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT content FROM scripts WHERE id=?", (SCRIPT_ID,)).fetchone()
    if not row:
        print(f"ERROR: script '{SCRIPT_ID}' not found in DB")
        sys.exit(1)
    script = json.loads(row[0])

    print("=" * 60)
    print("第五共和国剧本：统一事件系统全面优化")
    print("=" * 60)

    # Execute all enhancements
    n_vars = add_variables(script)
    print(f"[1] Variables: 新增 {n_vars} 个（总计 {len(script.get('variables', []))}）")

    n_ps = add_persistent_states(script)
    print(f"[2] Persistent States: 新增 {n_ps} 个（总计 {len(script.get('persistent_states', []))}）")

    n_trig = add_triggers(script)
    print(f"[3] Triggers: 新增 {n_trig} 个（总计 {len(script.get('triggers', []))}）")

    fixed, expanded = fix_and_expand_story_tree(script)
    print(f"[4] Story Tree: 修复 {fixed} 个 set_var，新增 {expanded} 个节点")

    n_ot = enhance_one_time_events(script)
    print(f"[5] One-Time Events: 增强 {n_ot} 个（总计 {len(script.get('one_time_events', []))}）")

    n_cy = enhance_cyclic_events(script)
    print(f"[6] Cyclic Events: 增强/新增 {n_cy} 个（总计 {len(script.get('cyclic_events', []))}）")

    n_de = add_dynamic_events(script)
    print(f"[7] Dynamic Events: 新增 {n_de} 个（总计 {len(script.get('dynamic_events', []))}）")

    n_th = add_attribute_thresholds(script)
    print(f"[8] Attribute Thresholds: 新增 {n_th} 条")

    n_oc = add_opening_choice_state_changes(script)
    print(f"[9] Opening Choices: 更新 {n_oc} 个选择的 state_changes")

    # Validate
    print("\n" + "=" * 60)
    print("验证中...")
    errors, warnings = validate(script)
    if errors:
        print(f"\n[FAIL] {len(errors)} 个错误:")
        for e in errors:
            print(f"  ERROR: {e}")
    if warnings:
        print(f"\n[WARN] {len(warnings)} 个警告:")
        for w in warnings:
            print(f"  WARN: {w}")
    if not errors and not warnings:
        print("[OK] 全部验证通过")

    # Write back
    if errors:
        print("\n存在错误，不写入数据库。")
        sys.exit(1)

    content = json.dumps(script, ensure_ascii=False, indent=None)
    conn.execute("UPDATE scripts SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (content, SCRIPT_ID))
    conn.commit()
    conn.close()

    # Summary
    print("\n" + "=" * 60)
    print("写入完成。最终统计:")
    print(f"  Variables:         {len(script.get('variables', []))}")
    print(f"  Persistent States: {len(script.get('persistent_states', []))}")
    print(f"  Triggers:          {len(script.get('triggers', []))}")
    st = script.get("story_tree", {})
    total_nodes = sum(len(t.get("nodes", [])) for t in st.get("trees", []))
    print(f"  Story Tree Nodes:  {total_nodes} ({len(st.get('trees', []))} trees)")
    print(f"  One-Time Events:   {len(script.get('one_time_events', []))}")
    print(f"  Cyclic Events:     {len(script.get('cyclic_events', []))}")
    print(f"  Dynamic Events:    {len(script.get('dynamic_events', []))}")
    print("=" * 60)


if __name__ == "__main__":
    main()
