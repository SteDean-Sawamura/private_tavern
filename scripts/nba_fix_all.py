"""NBA 2K14 剧本全面修复脚本 — 单次读写，避免覆盖。"""

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tavern.db"
SCRIPT_ID = "nba2k14_2015"


# ──────────────────────────────────────────────
# 1. World Properties 默认值修复
# ──────────────────────────────────────────────
def fix_world_properties(script):
    wp_list = script.get("world_properties", [])
    fixes = {
        "team_wins": {"value": 0, "type": "number", "min": 0, "max": 82},
        "team_losses": {"value": 0, "type": "number", "min": 0, "max": 82},
        "playoff_seed": {"value": 0, "type": "number", "min": 0, "max": 16},
        "team_chemistry": {"value": 50, "type": "number", "min": 0, "max": 100},
        "media_attention": {"value": 30, "type": "number", "min": 0, "max": 100},
    }
    for wp in wp_list:
        wid = wp.get("id")
        if wid in fixes:
            for k, v in fixes[wid].items():
                wp[k] = v


# ──────────────────────────────────────────────
# 2. Variables 补充 team_wins/team_losses
# ──────────────────────────────────────────────
def fix_variables(script):
    variables = script.get("variables", [])
    existing_ids = {v["id"] for v in variables}
    new_vars = [
        {"id": "team_wins", "name": "球队胜场", "type": "number", "default": 0, "min": 0, "max": 82},
        {"id": "team_losses", "name": "球队负场", "type": "number", "default": 0, "min": 0, "max": 82},
    ]
    for nv in new_vars:
        if nv["id"] not in existing_ids:
            variables.append(nv)


# ──────────────────────────────────────────────
# 3. Attribute Thresholds 修复
# ──────────────────────────────────────────────
def fix_attribute_thresholds(script):
    attrs = script.get("player_character", {}).get("attributes", {})

    threshold_map = {
        "体力": {
            "max": 100,
            "thresholds": [
                {"value": 30, "direction": "below", "description": "身体已到极限，随时可能受伤！", "activate_state": "injury_high_risk"},
                {"value": 50, "direction": "below", "description": "体力不足，比赛表现开始受影响"},
                {"value": 70, "direction": "above", "description": "身体状态恢复良好"},
            ],
        },
        "抗压能力": {
            "max": 99,
            "thresholds": [
                {"value": 80, "direction": "above", "description": "大心脏球员！关键时刻值得信赖", "activate_state": "clutch_player"},
            ],
        },
        "球场声望": {
            "max": 100,
            "thresholds": [
                {"value": 70, "direction": "above", "description": "联盟新星崛起！代言和全明星机会来了", "activate_state": "league_star"},
                {"value": 40, "direction": "below", "description": "关注度下降，需要用表现重新证明自己"},
            ],
        },
    }

    for attr_name, fix in threshold_map.items():
        if attr_name in attrs and isinstance(attrs[attr_name], dict):
            attrs[attr_name]["thresholds"] = fix["thresholds"]
            attrs[attr_name]["max"] = fix["max"]

    for attr_name, attr in attrs.items():
        if isinstance(attr, dict) and attr_name not in threshold_map:
            if attr_name not in ("体力", "球场声望"):
                attr["max"] = 99


# ──────────────────────────────────────────────
# 4. Dynamic Events 恢复（14 个）
# ──────────────────────────────────────────────
def restore_dynamic_events(script):
    script["dynamic_events"] = [
        {
            "id": "de_chemistry_boost",
            "name": "化学反应提升",
            "description": "球队配合越来越默契，团队化学反应提升",
            "weight": 10,
            "cooldown": 8,
            "conditions": [
                {"path": "world_properties.team_chemistry", "op": ">=", "value": 60},
                {"path": "script_variables.win_streak", "op": ">=", "value": 3},
            ],
            "effects": [
                {"type": "state_change", "target": "world_properties.team_chemistry", "op": "add", "value": 5},
                {"type": "narrative_callback", "text": "连胜让球队上下信心爆棚，更衣室气氛前所未有地融洽。队友们开始在场上形成心灵感应般的默契。", "priority": "medium"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_chemistry_drop",
            "name": "化学反应下降",
            "description": "连败导致更衣室出现裂痕",
            "weight": 10,
            "cooldown": 8,
            "conditions": [
                {"path": "world_properties.team_chemistry", "op": "<=", "value": 40},
                {"path": "world_properties.team_losses", "op": ">=", "value": 15},
            ],
            "effects": [
                {"type": "state_change", "target": "world_properties.team_chemistry", "op": "add", "value": -5},
                {"type": "narrative_callback", "text": "更衣室气氛变得紧张，队友之间开始出现不满和指责。教练需要想办法稳住军心。", "priority": "high"},
                {"type": "activate_state", "id": "locker_room_crisis"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_media_frenzy",
            "name": "媒体狂潮",
            "description": "高声望引发媒体追捧",
            "weight": 8,
            "cooldown": 10,
            "conditions": [
                {"path": "player.球场声望", "op": ">=", "value": 75},
                {"path": "world_properties.media_attention", "op": ">=", "value": 50},
            ],
            "effects": [
                {"type": "state_change", "target": "world_properties.media_attention", "op": "add", "value": 10},
                {"type": "narrative_callback", "text": "你的表现引爆了媒体，社交媒体上关于你的讨论占据了热搜榜。ESPN和TNT都在争相报道你的故事。", "priority": "medium"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_media_scrutiny",
            "name": "媒体审视",
            "description": "低迷表现引发质疑",
            "weight": 8,
            "cooldown": 10,
            "conditions": [
                {"path": "player.球场声望", "op": "<=", "value": 35},
                {"path": "world_properties.media_attention", "op": ">=", "value": 40},
            ],
            "effects": [
                {"type": "state_change", "target": "world_properties.media_attention", "op": "add", "value": -5},
                {"type": "narrative_callback", "text": "媒体开始质疑你的能力和态度。赛后发布会上记者的问题越来越尖锐，社交媒体上充斥着负面评论。", "priority": "medium"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_trade_rumor",
            "name": "交易传闻",
            "description": "交易截止日前的传闻",
            "weight": 6,
            "cooldown": 15,
            "conditions": [
                {"path": "script_variables.season_phase", "op": "==", "value": "regular"},
                {"path": "script_variables.games_played", "op": ">=", "value": 30},
            ],
            "effects": [
                {"type": "narrative_callback", "text": "据可靠消息，球队管理层正在评估阵容调整方案。你的名字出现在了几支球队的交易意向名单上。经纪人建议你保持冷静，专注比赛。", "priority": "high"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_endorsement_offer",
            "name": "代言机会",
            "description": "品牌代言邀约",
            "weight": 7,
            "cooldown": 20,
            "conditions": [
                {"path": "player.球场声望", "op": ">=", "value": 60},
                {"path": "world_properties.media_attention", "op": ">=", "value": 40},
            ],
            "effects": [
                {"type": "narrative_callback", "text": "经纪人兴奋地告诉你，一家知名运动品牌希望和你签订代言合同。这是你职业生涯的重要里程碑。", "priority": "medium"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_injury_scare",
            "name": "伤病隐患",
            "description": "低体力时的伤病风险",
            "weight": 12,
            "cooldown": 6,
            "conditions": [
                {"path": "player.体力", "op": "<=", "value": 35},
                {"path": "script_variables.is_injured", "op": "==", "value": "false"},
            ],
            "effects": [
                {"type": "state_change", "target": "player.体力", "op": "add", "value": -5},
                {"type": "narrative_callback", "text": "训练中你感到身体某处隐隐作痛。队医建议你减少训练强度，否则可能会发展成正式伤病。", "priority": "high"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_allstar_buzz",
            "name": "全明星讨论",
            "description": "全明星投票相关事件",
            "weight": 7,
            "cooldown": 15,
            "conditions": [
                {"path": "player.球场声望", "op": ">=", "value": 65},
                {"path": "script_variables.games_played", "op": ">=", "value": 20},
                {"path": "script_variables.is_allstar", "op": "==", "value": "false"},
            ],
            "effects": [
                {"type": "narrative_callback", "text": "全明星投票正在进行，你的名字频繁出现在各大投票榜单上。球迷们在社交媒体上发起了为你拉票的活动。", "priority": "medium"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_hot_streak",
            "name": "火热手感",
            "description": "连续高分表现",
            "weight": 8,
            "cooldown": 12,
            "conditions": [
                {"path": "player.球场声望", "op": ">=", "value": 55},
                {"path": "script_variables.win_streak", "op": ">=", "value": 2},
            ],
            "effects": [
                {"type": "activate_state", "id": "hot_streak"},
                {"type": "narrative_callback", "text": "你最近的表现势不可挡！投篮命中率创下赛季新高，对手们开始在赛前会议上重点部署对你的防守策略。", "priority": "medium"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_playoff_pressure",
            "name": "季后赛压力",
            "description": "季后赛争夺白热化",
            "weight": 10,
            "cooldown": 8,
            "conditions": [
                {"path": "script_variables.season_phase", "op": "==", "value": "regular"},
                {"path": "script_variables.games_played", "op": ">=", "value": 60},
            ],
            "effects": [
                {"type": "narrative_callback", "text": "赛季进入冲刺阶段，季后赛席位争夺白热化。每场比赛都可能决定你们的命运，全队上下绷紧了神经。", "priority": "high"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_rookie_wall",
            "name": "新秀墙",
            "description": "新秀赛季中期的体能和心理瓶颈",
            "weight": 8,
            "cooldown": 20,
            "conditions": [
                {"path": "script_variables.games_played", "op": ">=", "value": 35},
                {"path": "script_variables.games_played", "op": "<=", "value": 55},
                {"path": "player.体力", "op": "<=", "value": 55},
            ],
            "effects": [
                {"type": "state_change", "target": "player.体力", "op": "add", "value": -3},
                {"type": "state_change", "target": "player.抗压能力", "op": "add", "value": -2},
                {"type": "narrative_callback", "text": "漫长赛季的消耗开始显现。这就是所谓的'新秀墙'——身体和精神的双重疲惫让你在场上的表现出现波动。", "priority": "high"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_locker_room_bond",
            "name": "更衣室友谊",
            "description": "队友间建立深厚友谊",
            "weight": 6,
            "cooldown": 15,
            "conditions": [
                {"path": "world_properties.team_chemistry", "op": ">=", "value": 70},
            ],
            "effects": [
                {"type": "narrative_callback", "text": "训练结束后，几个队友邀请你一起聚餐。在球场之外的相处让你们的关系更加紧密，这种信任会转化为场上的默契。", "priority": "low"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_contract_tension",
            "name": "合同压力",
            "description": "合同年的额外压力",
            "weight": 9,
            "cooldown": 12,
            "conditions": [
                {"path": "script_variables.contract_year", "op": "==", "value": "true"},
                {"path": "player.抗压能力", "op": "<=", "value": 50},
            ],
            "effects": [
                {"type": "state_change", "target": "player.抗压能力", "op": "add", "value": -3},
                {"type": "narrative_callback", "text": "合同年的压力如影随形。每次失误都让你忍不住想：这会影响我的下一份合同吗？经纪人说别想太多，但你控制不住。", "priority": "high"},
            ],
            "chain_events": [],
        },
        {
            "id": "de_fan_interaction",
            "name": "球迷互动",
            "description": "球迷的热情支持或失望",
            "weight": 5,
            "cooldown": 10,
            "conditions": [
                {"path": "player.球场声望", "op": ">=", "value": 45},
            ],
            "effects": [
                {"type": "narrative_callback", "text": "比赛结束后，一群年轻球迷围在通道口等你签名。其中一个孩子穿着你的球衣，眼里满是崇拜。这就是你打球的意义。", "priority": "low"},
            ],
            "chain_events": [],
        },
    ]


# ──────────────────────────────────────────────
# 5. NPC 完善（更新 agent_mike + 新增 3 个）
# ──────────────────────────────────────────────
def fix_npcs(script):
    npcs = script.get("npcs", [])
    npc_by_id = {n["id"]: n for n in npcs}

    if "agent_mike" in npc_by_id:
        npc_by_id["agent_mike"]["organizations"] = [{"org_id": "player_agent_firm", "rank": 3}]
        npc_by_id["agent_mike"]["goals"] = [
            {"description": "为客户争取最大合同和最佳条款", "priority": "high"},
            {"description": "拿下至少一个大品牌代言合作", "priority": "medium"},
        ]

    new_npcs = [
        {
            "id": "girlfriend",
            "name": "女友艾琳",
            "bio": "大学时期认识的女朋友，在你被选秀之前就在一起了。她有自己的事业追求，不只是'球星女友'这个标签。",
            "personality": "温柔但有主见，支持你的梦想但也坚持自己的底线。不喜欢被媒体聚光灯打扰，但愿意为你适应公众生活。",
            "capabilities": "情感支持，帮助你保持生活平衡，偶尔会从旁观者角度给出犀利的观察",
            "title": "女友",
            "organizations": [],
            "related_lore": [],
            "attitude_toward_player": 80,
            "talkativeness": 70,
            "known": True,
            "met": True,
            "portrait_desc": "一位气质温婉的年轻女性，扎着马尾，穿着简约时尚",
            "default_location": "nightlife_district",
            "schedule": [
                {"time_range": "08:00-17:00", "location": "nightlife_district", "activity": "工作或自己的事情"},
                {"time_range": "17:00-21:00", "location": "nightlife_district", "activity": "等你或一起吃饭"},
                {"time_range": "21:00-23:00", "location": "nightlife_district", "activity": "在家休息"},
            ],
            "goals": [
                {"description": "维持和你的感情稳定，不被名利冲散", "priority": "high"},
                {"description": "在你成名后保持自我，发展自己的事业", "priority": "medium"},
            ],
        },
        {
            "id": "personal_trainer",
            "name": "私人训练师老李",
            "bio": "退役运动员转型的体能训练师，在圈子里口碑极好。服务过多位NBA球员，懂得如何在漫长赛季中管理球员体能。",
            "personality": "严格但关心人，说话直接不拐弯。训练时一丝不苟，私下里是个暖心的大哥。相信科学训练，反对透支身体。",
            "capabilities": "体能评估，伤病康复指导，赛季体能规划，营养建议",
            "title": "私人训练师",
            "organizations": [],
            "related_lore": [],
            "attitude_toward_player": 65,
            "talkativeness": 60,
            "known": True,
            "met": True,
            "portrait_desc": "一位体格健壮的中年男性，穿着运动装，手里经常拿着写字板",
            "default_location": "rehab_center",
            "schedule": [
                {"time_range": "06:00-12:00", "location": "practice_facility", "activity": "指导训练"},
                {"time_range": "12:00-18:00", "location": "rehab_center", "activity": "康复治疗和体能评估"},
                {"time_range": "18:00-21:00", "location": "rehab_center", "activity": "制定训练计划"},
            ],
            "goals": [
                {"description": "确保球员整个赛季健康无大伤", "priority": "high"},
                {"description": "通过科学训练提升球员体能上限", "priority": "medium"},
            ],
        },
        {
            "id": "father",
            "name": "父亲",
            "bio": "一个普通的工薪阶层父亲，年轻时也打过业余篮球。儿子进入NBA是他最大的骄傲，但他更希望儿子做一个好人。",
            "personality": "沉稳内敛，不善言辞但每句话都分量十足。从不在外人面前炫耀儿子，但在家乡的球场上会悄悄关注每场比赛直播。",
            "capabilities": "精神支持，人生智慧，来自家乡的温暖力量",
            "title": "父亲",
            "organizations": [],
            "related_lore": [],
            "attitude_toward_player": 90,
            "talkativeness": 40,
            "known": True,
            "met": True,
            "portrait_desc": "一位头发花白的中年男性，穿着朴素，眼神慈祥而坚定",
            "default_location": "",
            "schedule": [],
            "goals": [
                {"description": "看到儿子在NBA取得成功", "priority": "high"},
                {"description": "希望一家人能更多地团聚", "priority": "medium"},
            ],
        },
    ]

    for npc in new_npcs:
        if npc["id"] not in npc_by_id:
            npcs.append(npc)


# ──────────────────────────────────────────────
# 6. NPC Relationships
# ──────────────────────────────────────────────
def fix_npc_relationships(script):
    script["npc_relationships"] = [
        {"from": "agent_mike", "to": "girlfriend", "trust": 45, "affection": 40, "fear": 0,
         "description": "迈克尊重艾琳但偶尔因为安排冲突产生摩擦", "initially_known": True, "initially_met": True},
        {"from": "girlfriend", "to": "agent_mike", "trust": 35, "affection": 35, "fear": 0,
         "description": "艾琳觉得迈克有时把商业利益看得太重", "initially_known": True, "initially_met": True},
        {"from": "agent_mike", "to": "personal_trainer", "trust": 50, "affection": 50, "fear": 0,
         "description": "职业合作关系，都为球员利益服务", "initially_known": True, "initially_met": True},
        {"from": "father", "to": "agent_mike", "trust": 40, "affection": 45, "fear": 0,
         "description": "父亲对经纪人行业有些不信任但认可迈克的能力", "initially_known": True, "initially_met": True},
        {"from": "father", "to": "girlfriend", "trust": 65, "affection": 70, "fear": 0,
         "description": "父亲对艾琳很满意，觉得她是个好姑娘", "initially_known": True, "initially_met": True},
        {"from": "girlfriend", "to": "father", "trust": 60, "affection": 65, "fear": 0,
         "description": "艾琳尊重老人，关系融洽", "initially_known": True, "initially_met": True},
    ]


# ──────────────────────────────────────────────
# 7. Org Relationships
# ──────────────────────────────────────────────
def fix_org_relationships(script):
    script["org_relationships"] = [
        {"a": "media_corps", "b": "nba_league", "type": "合作", "description": "媒体报道联盟赛事，共生关系", "intensity": 70},
        {"a": "player_agent_firm", "b": "nba_league", "type": "博弈", "description": "经纪公司代表球员与联盟谈判", "intensity": 55},
        {"a": "media_corps", "b": "player_agent_firm", "type": "利用", "description": "媒体从经纪人处获取独家消息", "intensity": 45},
    ]


# ──────────────────────────────────────────────
# 8. Story Tree Node Status
# ──────────────────────────────────────────────
def fix_story_tree_status(script):
    st = script.get("story_tree", {})
    if not isinstance(st, dict):
        return
    for tree in st.get("trees", []):
        for node in tree.get("nodes", []):
            requires = node.get("requires", [])
            node["status"] = "available" if not requires else "locked"


# ──────────────────────────────────────────────
# 9. Preset NPC Overrides + Opening Choice State Changes
# ──────────────────────────────────────────────
def fix_presets(script):
    presets = script.get("player_presets", [])
    preset_by_id = {p["id"]: p for p in presets}

    npc_overrides_map = {
        "rookie_pg": [
            {"id": "agent_mike", "personality": "年轻有冲劲的新锐经纪人，把你当成自己的第一个大客户全力以赴。虽然经验不足但满腔热血，会为了你跟任何人据理力争。"},
            {"id": "girlfriend", "name": "女友小雨", "personality": "大学时期的女朋友，还在读研究生。异地恋让你们的关系充满考验，但她一直默默支持你的篮球梦。"},
            {"id": "personal_trainer", "personality": "和你一样年轻的体能训练师，推崇最新的运动科学理论。喜欢用数据说话，经常拿出iPad给你看各种训练指标。"},
            {"id": "father", "personality": "为儿子进入NBA而骄傲得睡不着觉的父亲。嘴上说着'别骄傲，好好打'，其实早就把你的球衣裱了起来挂在客厅。"},
        ],
        "veteran_sg": [
            {"id": "agent_mike", "personality": "合作多年的老搭档，关系早已超越了纯粹的商业合作。他了解你的每一个底线，也知道什么时候该逼你一把。"},
            {"id": "girlfriend", "name": "妻子雅琴", "personality": "相恋多年的妻子，经历过你职业生涯的起起落落。她是这个家的支柱，用自己的方式守护着一切。"},
            {"id": "personal_trainer", "personality": "跟了你五年的私人训练师，深知你身体的每一个旧伤和弱点。训练方案越来越保守，因为他比谁都清楚你的身体状况。"},
            {"id": "father", "personality": "年纪越来越大的父亲，开始用一种更柔和的方式关心你。不再像年轻时那样严格要求，而是更多地希望你健康快乐。"},
        ],
        "athletic_forward": [
            {"id": "agent_mike", "personality": "嗅觉敏锐的经纪人，看准了你的运动天赋是最大的卖点。总是在寻找能展示你运动能力的商业合作机会。"},
            {"id": "girlfriend", "name": "女友娜娜", "personality": "一个活泼开朗的女孩，和你一样热爱运动。你们经常一起去户外运动，她是你最好的减压方式。"},
            {"id": "personal_trainer", "personality": "前田径运动员出身的训练师，特别注重爆发力和敏捷性训练。认为你的运动天赋还有很大的开发空间。"},
            {"id": "father", "personality": "当年的业余篮球好手，你的运动天赋很大程度上遗传自他。偶尔会用当年的经验指点你，虽然时代不同了但道理相通。"},
        ],
        "defensive_center": [
            {"id": "agent_mike", "personality": "务实型经纪人，深知防守型球员在市场上的定位。总是提醒你'数据不会说谎'，帮你争取那些看重防守的球队的关注。"},
            {"id": "girlfriend", "name": "女友小文", "personality": "一个安静内敛的女孩，和你性格互补。她不太懂篮球，但每次看你打球时那专注的眼神让你觉得被理解。"},
            {"id": "personal_trainer", "personality": "专注于力量训练的体能教练，认为对抗能力是内线球员的生命线。训练强度大但效果显著，在联盟内线球员圈子里很有名。"},
            {"id": "father", "personality": "高大魁梧的父亲，你的身体条件有一半归功于他。他总是说'防守赢得冠军'，这句话你从小听到大。"},
        ],
        "sixth_man": [
            {"id": "agent_mike", "personality": "灵活变通的经纪人，擅长把'最佳第六人'这个角色包装出独特价值。总是在帮你寻找能给你更多上场时间的机会。"},
            {"id": "girlfriend", "name": "女友小丽", "personality": "一个通情达理的女孩，理解你作为替补的心态。她总说'你是球队不可或缺的一部分'，在你失落时给你最温暖的鼓励。"},
            {"id": "personal_trainer", "personality": "注重全面性训练的教练，认为你的优势就是什么都能来一点。训练计划涵盖体能的各个方面，让你能适应任何位置。"},
            {"id": "father", "personality": "务实的父亲，不在乎你是首发还是替补，只关心你在场上是否全力以赴。'做好手里的每一件事'是他的人生哲学。"},
        ],
        "international_player": [
            {"id": "agent_mike", "personality": "有国际球员运营经验的经纪人，帮你处理签证、翻译等各种杂事。他深知国际球员在NBA面临的额外挑战，总是多走一步帮你铺路。"},
            {"id": "girlfriend", "name": "女友露西", "personality": "在美国认识的女朋友，帮助你适应美国的生活和文化。她耐心地教你英语俚语，带你融入当地社区。"},
            {"id": "personal_trainer", "personality": "曾在欧洲执教过的训练师，了解不同篮球体系的训练差异。帮助你从国际篮球的节奏适应到更快更强的NBA节奏。"},
            {"id": "father", "personality": "在大洋彼岸牵挂你的父亲，只能通过视频电话看到你。时差让你们的通话总在深夜，但他从不抱怨。"},
        ],
    }

    opening_choices_map = {
        "rookie_pg": [
            [
                {"target": "player.篮球智商", "op": "add", "value": 2},
                {"target": "player.relationships.agent_mike", "op": "add", "value": 5},
            ],
            [
                {"target": "player.抗压能力", "op": "add", "value": 2},
            ],
            [
                {"target": "player.球场声望", "op": "add", "value": 3},
                {"target": "player.抗压能力", "op": "add", "value": -1},
            ],
        ],
        "veteran_sg": [
            [
                {"target": "player.篮球智商", "op": "add", "value": 2},
                {"target": "player.抗压能力", "op": "add", "value": 1},
            ],
            [
                {"target": "player.球场声望", "op": "add", "value": 3},
                {"target": "world_properties.media_attention", "op": "add", "value": 5},
            ],
            [
                {"target": "player.体力", "op": "add", "value": 3},
                {"target": "player.抗压能力", "op": "add", "value": 2},
            ],
        ],
        "athletic_forward": [
            [
                {"target": "player.运动能力", "op": "add", "value": 2},
                {"target": "player.球场声望", "op": "add", "value": 2},
            ],
            [
                {"target": "player.篮球智商", "op": "add", "value": 3},
                {"target": "player.运动能力", "op": "add", "value": -1},
            ],
            [
                {"target": "player.球场声望", "op": "add", "value": 3},
                {"target": "player.抗压能力", "op": "add", "value": 1},
            ],
        ],
        "defensive_center": [
            [
                {"target": "player.内线防守", "op": "add", "value": 2},
                {"target": "player.篮板能力", "op": "add", "value": 1},
            ],
            [
                {"target": "player.篮球智商", "op": "add", "value": 2},
                {"target": "player.抗压能力", "op": "add", "value": 1},
            ],
            [
                {"target": "player.球场声望", "op": "add", "value": 2},
                {"target": "world_properties.team_chemistry", "op": "add", "value": 3},
            ],
        ],
        "sixth_man": [
            [
                {"target": "player.抗压能力", "op": "add", "value": 3},
                {"target": "player.篮球智商", "op": "add", "value": 1},
            ],
            [
                {"target": "player.球场声望", "op": "add", "value": 3},
                {"target": "player.体力", "op": "add", "value": -2},
            ],
            [
                {"target": "player.篮球智商", "op": "add", "value": 2},
                {"target": "world_properties.team_chemistry", "op": "add", "value": 3},
            ],
        ],
        "international_player": [
            [
                {"target": "player.抗压能力", "op": "add", "value": 3},
                {"target": "player.篮球智商", "op": "add", "value": 1},
            ],
            [
                {"target": "player.球场声望", "op": "add", "value": 2},
                {"target": "world_properties.media_attention", "op": "add", "value": 3},
            ],
            [
                {"target": "player.篮球智商", "op": "add", "value": 2},
                {"target": "player.组织传球", "op": "add", "value": 1},
            ],
        ],
    }

    for pid, overrides in npc_overrides_map.items():
        if pid in preset_by_id:
            preset_by_id[pid]["npc_overrides"] = overrides

    for pid, choice_changes in opening_choices_map.items():
        preset = preset_by_id.get(pid)
        if not preset:
            continue
        choices = preset.get("opening_choices", [])
        for i, changes in enumerate(choice_changes):
            if i < len(choices):
                choices[i]["state_changes"] = changes


# ──────────────────────────────────────────────
# 10. Player Character Relationships
# ──────────────────────────────────────────────
def fix_pc_relationships(script):
    pc = script.get("player_character", {})
    rels = pc.setdefault("relationships", {})
    defaults = {
        "agent_mike": {"value": 70, "min": 0, "max": 100, "rule": "与经纪人迈克的关系"},
        "girlfriend": {"value": 75, "min": 0, "max": 100, "rule": "与女友的亲密关系"},
        "personal_trainer": {"value": 55, "min": 0, "max": 100, "rule": "与私人训练师的合作关系"},
        "father": {"value": 85, "min": 0, "max": 100, "rule": "与父亲的家庭纽带"},
    }
    for rid, rdef in defaults.items():
        if rid not in rels:
            rels[rid] = rdef


# ──────────────────────────────────────────────
# 11. World Background 扩展
# ──────────────────────────────────────────────
def fix_world_background(script):
    script["world_background"] = (
        "2014-2015赛季的NBA正处于历史变革期。勇士队的三分革命正在改写篮球规则，"
        "传统内线为王的时代渐行渐远。勒布朗·詹姆斯重返克利夫兰追寻冠军梦，"
        "科比·布莱恩特在伤病中苦苦挣扎，联盟的权力格局正在经历新老交替。\n\n"
        "这个赛季有82场常规赛的漫长征途，从十月的训练营到次年六月的总决赛，"
        "球员们需要经历伤病考验、交易窗口的焦虑、全明星周末的荣耀和季后赛的残酷淘汰。"
        "每天的节奏围绕着训练、比赛、恢复和媒体应对展开——对于年轻球员来说，"
        "适应这种高强度的职业生活本身就是一场考验。\n\n"
        "场外的世界同样精彩和复杂：经纪人为你筹划商业版图，媒体放大你的每一个举动，"
        "社交媒体让球迷的声音前所未有地响亮。你需要在球场表现、商业价值、"
        "个人生活和公众形象之间找到平衡。你的每一个选择——场上的每一次传球、"
        "场下的每一个决定——都在书写属于你的NBA故事。"
    )


# ──────────────────────────────────────────────
# 12. 新增 Lorebook: 动态角色扮演指南
# ──────────────────────────────────────────────
def add_dynamic_roles_lorebook(script):
    lorebook = script.get("lorebook", [])
    existing_ids = {lb["id"] for lb in lorebook}

    if "lore_nba_dynamic_roles" not in existing_ids:
        lorebook.append({
            "id": "lore_nba_dynamic_roles",
            "keys": ["教练", "队友", "总经理", "记者", "训练", "更衣室"],
            "secondary_keys": ["coach", "teammate", "GM", "reporter", "locker_room"],
            "position": "after_world",
            "enabled": True,
            "constant": False,
            "priority": 85,
            "scan_depth": 3,
            "comment": "动态角色扮演指南",
            "related_entries": [],
            "probability": 100,
            "sticky": 0,
            "cooldown": 0,
            "group": "",
            "group_weight": 100,
            "depth": 4,
            "role": "system",
            "content": (
                "【动态角色扮演指南】\n"
                "以下角色不是固定NPC，AI应根据球队lorebook数据和剧情上下文动态扮演：\n\n"
                "1. 主教练：参考球队lorebook中的教练姓名和执教风格。教练决定战术、上场时间、首发阵容。"
                "性格应与其执教风格一致——进攻型教练更开放大胆，防守型教练更保守严厉。\n\n"
                "2. 队友：参考球队lorebook中的首发和替补球员名单。"
                "同位置竞争者应有适度的竞争张力。老将队友应有导师气质。"
                "AI应维持队友性格的一致性——一旦在前文中建立了某个队友的形象，后续应保持。\n\n"
                "3. 总经理/管理层：影响交易决策和合同谈判。在交易截止日前后尤为活跃。"
                "对球员的态度取决于球队战绩和球员表现。\n\n"
                "4. 赛后记者：赛后必有简短的记者提问环节。"
                "问题应围绕本场比赛的关键时刻、个人表现数据和球队近况。\n\n"
                "5. 对手球员：比赛中的对位球员应参考对方球队lorebook中的数据。"
                "对位球星应有名有姓有特点，不要用'对方球员'这种泛称。"
            ),
        })


# ──────────────────────────────────────────────
# 13. 确认 Organizations 声望值
# ──────────────────────────────────────────────
def fix_organizations(script):
    orgs = script.get("organizations", [])
    rep_map = {"nba_league": 50, "player_agent_firm": 70, "media_corps": 50}
    for org in orgs:
        oid = org.get("id")
        if oid in rep_map:
            org["initial_reputation"] = rep_map[oid]


# ──────────────────────────────────────────────
# VALIDATION
# ──────────────────────────────────────────────
def validate(script):
    errors = []
    npc_ids = {n["id"] for n in script.get("npcs", [])}
    loc_ids = {l["id"] for l in script.get("locations", [])}
    org_ids = {o["id"] for o in script.get("organizations", [])}
    ps_ids = {p["id"] for p in script.get("persistent_states", [])}
    var_ids = {v["id"] for v in script.get("variables", [])}
    lb_ids = set()

    # Lorebook uniqueness
    for lb in script.get("lorebook", []):
        lid = lb.get("id", "")
        if lid in lb_ids:
            errors.append(f"重复 lorebook ID: {lid}")
        lb_ids.add(lid)

    # NPC refs
    for npc in script.get("npcs", []):
        nid = npc.get("id")
        dloc = npc.get("default_location", "")
        if dloc and dloc not in loc_ids:
            errors.append(f"NPC '{nid}' default_location '{dloc}' 不存在")
        for om in npc.get("organizations", []):
            oid = om.get("org_id", "")
            if oid and oid not in org_ids:
                errors.append(f"NPC '{nid}' 引用不存在的组织 '{oid}'")

    # NPC relationships
    for rel in script.get("npc_relationships", []):
        for k in ("from", "to"):
            rid = rel.get(k, "")
            if rid and rid not in npc_ids:
                errors.append(f"npc_relationship.{k} 引用不存在的 NPC '{rid}'")

    # Org relationships
    for orel in script.get("org_relationships", []):
        for k in ("a", "b"):
            ref = orel.get(k, "")
            if ref and ref not in org_ids:
                errors.append(f"org_relationship.{k} 引用不存在的组织 '{ref}'")

    # Dynamic event paths (basic check)
    for de in script.get("dynamic_events", []):
        for cond in de.get("conditions", []):
            path = cond.get("path", "")
            if "球队" in path or "媒体" in path:
                errors.append(f"dynamic_event '{de['id']}' 条件路径使用了中文: {path}")

    # Threshold activate_state refs
    for attr_name, attr in script.get("player_character", {}).get("attributes", {}).items():
        if isinstance(attr, dict):
            for th in attr.get("thresholds", []):
                sid = th.get("activate_state", "")
                if sid and sid not in ps_ids:
                    errors.append(f"属性 '{attr_name}' threshold activate_state '{sid}' 不在 persistent_states 中")

    # Trigger variable refs
    for t in script.get("triggers", []):
        params = t.get("params", {})
        text = params.get("text", "")
        import re
        for match in re.finditer(r"\{\{var::(\w+)\}\}", text):
            vid = match.group(1)
            if vid not in var_ids:
                errors.append(f"trigger '{t.get('id')}' 引用不存在的变量 '{{{{var::{vid}}}}}'")

    # Story tree status
    for tree in script.get("story_tree", {}).get("trees", []):
        for node in tree.get("nodes", []):
            if "status" not in node:
                errors.append(f"story tree node '{node.get('id')}' 缺少 status 字段")

    return errors


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def main():
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT content FROM scripts WHERE id = ?", (SCRIPT_ID,)).fetchone()
    if not row:
        print(f"未找到剧本 {SCRIPT_ID}")
        sys.exit(1)

    script = json.loads(row[0])
    print(f"读取剧本: {script.get('script_name', SCRIPT_ID)}")

    # Apply all fixes
    print("修复 world_properties 默认值...")
    fix_world_properties(script)

    print("补充 variables (team_wins/team_losses)...")
    fix_variables(script)

    print("修复 attribute thresholds...")
    fix_attribute_thresholds(script)

    print("恢复 14 个 dynamic_events...")
    restore_dynamic_events(script)

    print("完善 NPC (4 个)...")
    fix_npcs(script)

    print("添加 NPC relationships (6 条)...")
    fix_npc_relationships(script)

    print("添加 org relationships (3 条)...")
    fix_org_relationships(script)

    print("修复 story tree node status...")
    fix_story_tree_status(script)

    print("修复 presets (NPC overrides + opening choice state_changes)...")
    fix_presets(script)

    print("添加 player_character relationships...")
    fix_pc_relationships(script)

    print("扩展 world_background...")
    fix_world_background(script)

    print("添加动态角色扮演 lorebook...")
    add_dynamic_roles_lorebook(script)

    print("确认组织声望值...")
    fix_organizations(script)

    # Validate
    print("\n运行验证...")
    errors = validate(script)
    if errors:
        print(f"\n发现 {len(errors)} 个问题:")
        for e in errors:
            print(f"  ✗ {e}")
        print("\n仍将写入数据库，但请检查上述问题。")
    else:
        print("验证通过！")

    # Write back
    content_str = json.dumps(script, ensure_ascii=False, indent=2)
    conn.execute(
        "UPDATE scripts SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (content_str, SCRIPT_ID),
    )
    conn.commit()
    conn.close()

    # Summary
    print(f"\n═══ 修复完成统计 ═══")
    print(f"  variables:          {len(script.get('variables', []))}")
    print(f"  world_properties:   {len(script.get('world_properties', []))}")
    print(f"  npcs:               {len(script.get('npcs', []))}")
    print(f"  npc_relationships:  {len(script.get('npc_relationships', []))}")
    print(f"  org_relationships:  {len(script.get('org_relationships', []))}")
    print(f"  organizations:      {len(script.get('organizations', []))}")
    print(f"  dynamic_events:     {len(script.get('dynamic_events', []))}")
    print(f"  lorebook:           {len(script.get('lorebook', []))}")
    print(f"  player_presets:     {len(script.get('player_presets', []))}")
    print(f"  locations:          {len(script.get('locations', []))}")
    print(f"  persistent_states:  {len(script.get('persistent_states', []))}")
    print(f"  triggers:           {len(script.get('triggers', []))}")
    pc = script.get("player_character", {})
    attrs = pc.get("attributes", {})
    th_count = sum(len(a.get("thresholds", [])) for a in attrs.values() if isinstance(a, dict))
    print(f"  attribute thresholds: {th_count}")
    print(f"  pc relationships:   {len(pc.get('relationships', {}))}")
    st_nodes = sum(len(t.get("nodes", [])) for t in script.get("story_tree", {}).get("trees", []))
    with_status = sum(
        1 for t in script.get("story_tree", {}).get("trees", [])
        for n in t.get("nodes", []) if "status" in n
    )
    print(f"  story tree nodes:   {st_nodes} (with status: {with_status})")
    print(f"  world_background:   {len(script.get('world_background', ''))} chars")


if __name__ == "__main__":
    main()
