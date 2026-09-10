"""审计修复脚本：为所有事件补充 name、统一字段格式、添加缺失的动态事件。"""

import json
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tavern.db"
SCRIPT_ID = "fifth_republic_dawn"


# ═══════════════════════════════════════════════
# 1. One-Time Events: 补充 name 字段
# ═══════════════════════════════════════════════
OT_NAMES = {
    "event_1026_incident": "10.26事变",
    "event_martial_law_declaration": "全国戒严令",
    "event_chun_joint_investigation": "合同搜查本部设立",
    "event_hanahoe_secret_plot": "一心会密谋",
    "event_choi_president_election": "崔圭夏当选总统",
    "event_1212_military_mutiny": "双十二军事叛乱",
    "防潮水利工程剪彩揭幕": "防潮水利工程揭幕",
    "event_1016_busan_protest": "釜山民众抗议",
    "event_1018_busan_martial_law": "釜山戒严令",
    "event_masan_protest_spread": "马山抗议扩散",
    "event_1020_tape_viewing": "示威录像观看",
    "event_1025_tape_delivery": "录音带递送事件",
    "event_1026_chun_recommendation": "车智澈推荐全斗焕",
    "event_1026_gun_preparation": "金载圭备枪",
    "event_1026_dinner_invitation": "宫井洞宴会邀请",
    "event_1026_assassination_conspiracy": "暗杀阴谋启动",
    "event_seoul_student_protest_oct": "首尔学生抗议",
    "event_1026_body_transfer": "总统遗体转移",
    "event_1026_army_hq_meeting": "陆军本部紧急会议",
    "event_1026_arrest_kim_jae_gyu": "金载圭被捕",
    "event_1027_choi_acting_president": "崔圭夏代理总统",
    "event_1027_us_alert": "驻韩美军警戒",
    "event_1027_us_statement": "美方声明",
    "event_japan_media_comment": "日本媒体报道",
    "event_1026_witnesses_escape": "目击者逃离",
    "event_1980_kim_jae_gyu_execution": "金载圭处决",
    "event_1026_park_arrival": "总统抵达宫井洞",
    "event_1026_first_shot": "第一枪",
    "event_1026_park_shot": "总统中弹",
    "event_1026_gun_jam": "手枪卡壳",
    "event_1026_cha_killed": "车智澈身亡",
    "event_1026_bodyguards_eliminated": "警卫人员殉职",
    "event_1212_hannam_raid": "汉南洞突袭",
    "event_1212_rok_army_standoff": "首都军与叛军对峙",
    "event_1212_jang_tae_wan_arrest": "张泰玩被捕",
}

# 为无效果的纯叙事事件补充 narrative_callback
OT_MISSING_EFFECTS = {
    "防潮水利工程剪彩揭幕": [
        {"type": "narrative_callback", "text": "防潮水利工程剪彩——这是一个和平日常的背景事件，暗示着平静表面下的暗流涌动。", "priority": "low"},
    ],
}

# 为部分缺少 fire_events 的关键事件补充
OT_MISSING_FIRE = {
    "event_1018_busan_martial_law": ["crackdown_occurred"],
    "event_masan_protest_spread": ["protest_ongoing"],
    "event_1020_tape_viewing": ["intelligence_update"],
    "event_seoul_student_protest_oct": ["campus_unrest"],
    "event_1026_bodyguards_eliminated": ["assassination_occurred"],
}


# ═══════════════════════════════════════════════
# 2. Cyclic Events: 补充 name、统一格式
# ═══════════════════════════════════════════════
CY_NAMES = {
    "nightly_curfew": "夜间宵禁",
    "kcia_morning_briefing": "中情部晨会简报",
    "buma_protest_updates": "釜马抗争动态",
    "hanahoe_secret_gathering": "一心会秘密聚会",
    "pss_loyalty_drill": "警护室忠诚训练",
    "seoul_campus_unrest": "首尔校园动荡",
    "underground_press_distribution": "地下传单散发",
    "chaebol_political_donation": "财阀政治献金",
}

# chaebol_political_donation 的格式修正 (interval→frequency_value+frequency_unit)
CY_FORMAT_FIX = {
    "chaebol_political_donation": {
        "frequency_value": 3,
        "frequency_unit": "day",
        "first_trigger": "1979-10-17T09:00",
    },
}


# ═══════════════════════════════════════════════
# 3. Dynamic Events — 50 个
# ═══════════════════════════════════════════════
DYNAMIC_EVENTS = [
    # ── 政治博弈 (10) ──
    {
        "id": "de_regime_confidence_drop",
        "name": "政权信心动摇",
        "description": "当政权稳定度持续走低时，内部开始出现动摇和倒戈迹象。",
        "condition": "script_variables.regime_stability <= 30",
        "cooldown": 8,
        "priority": 60,
        "effects": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "政权的支柱们开始动摇——有人悄悄联络新军部，有人暗中转移资产。", "priority": "high"},
        ],
    },
    {
        "id": "de_political_purge",
        "name": "政治清洗",
        "description": "当权者对政敌进行清洗，试图巩固权力。",
        "condition": "script_variables.regime_stability <= 50 AND script_variables.military_alert_level >= 3",
        "cooldown": 10,
        "priority": 50,
        "effects": [
            {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": -5},
            {"type": "activate_state", "id": "purge_wave"},
            {"type": "narrative_callback", "text": "又一批人名从名单上被划去——清洗的风暴正在扩大。", "priority": "high"},
        ],
    },
    {
        "id": "de_power_vacuum_crisis",
        "name": "权力真空危机",
        "description": "总统被暗杀后形成的权力真空引发各派系争夺。",
        "condition": "script_variables.regime_stability <= 20",
        "cooldown": 12,
        "priority": 70,
        "effects": [
            {"type": "activate_state", "id": "power_vacuum"},
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "权力的王座空悬——所有人都在觊觎，没有人愿意退让。", "priority": "critical"},
        ],
        "fire_events": ["regime_collapsed"],
    },
    {
        "id": "de_faction_negotiation",
        "name": "派系谈判",
        "description": "各政治势力尝试通过谈判分配权力。",
        "condition": "script_variables.regime_stability >= 30 AND script_variables.regime_stability <= 60",
        "cooldown": 6,
        "priority": 40,
        "effects": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "密室里的谈判仍在继续——各方都在试探对方的底线。", "priority": "medium"},
        ],
    },
    {
        "id": "de_opposition_rally",
        "name": "在野党集会",
        "description": "反对派组织大规模政治集会要求民主化。",
        "condition": "script_variables.protest_intensity >= 50 AND script_variables.media_freedom >= 15",
        "cooldown": 7,
        "priority": 45,
        "effects": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 8},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "在野党的集会吸引了数千民众——民主化的呼声越来越响亮。", "priority": "medium"},
        ],
    },
    {
        "id": "de_censorship_crackdown",
        "name": "媒体审查加强",
        "description": "当局加强对媒体的审查和管控。",
        "condition": "script_variables.media_freedom >= 30",
        "cooldown": 8,
        "priority": 35,
        "effects": [
            {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": -10},
            {"type": "activate_state", "id": "information_blackout"},
            {"type": "narrative_callback", "text": "又一家报社被查封——沉默正在蔓延。", "priority": "medium"},
        ],
    },
    {
        "id": "de_emergency_cabinet",
        "name": "紧急内阁会议",
        "description": "危机时刻召开紧急内阁会议讨论对策。",
        "condition": "script_variables.military_alert_level >= 3 AND script_variables.regime_stability <= 40",
        "cooldown": 10,
        "priority": 55,
        "effects": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 3},
            {"type": "narrative_callback", "text": "部长们连夜赶到青瓦台——会议桌上摆满了各种方案，但没有一个令人满意。", "priority": "high"},
        ],
    },
    {
        "id": "de_constitutional_debate",
        "name": "宪法修正讨论",
        "description": "关于政权合法性和宪政转型的讨论浮出水面。",
        "condition": "script_variables.regime_stability <= 45 AND script_variables.protest_intensity >= 40",
        "cooldown": 12,
        "priority": 30,
        "effects": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 5},
            {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "宪法修正的讨论在幕后展开——有人看到了和平转型的希望。", "priority": "medium"},
        ],
    },
    {
        "id": "de_loyalty_test",
        "name": "忠诚度测试",
        "description": "当权者对麾下进行忠诚度测试，清除异己。",
        "condition": "script_variables.troops_loyalty <= 40",
        "cooldown": 8,
        "priority": 50,
        "effects": [
            {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": 10},
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "新一轮忠诚度审查开始——每个人都必须表态站队。", "priority": "high"},
        ],
    },
    {
        "id": "de_intelligence_leak",
        "name": "情报泄露",
        "description": "敏感情报被泄露，引发连锁反应。",
        "condition": "script_variables.communication_security <= 40",
        "cooldown": 10,
        "priority": 60,
        "effects": [
            {"type": "state_change", "target": "script_variables.intelligence_leaks", "op": "add", "value": 1},
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "一份机密文件流出——有人在暗中出卖情报。", "priority": "high"},
        ],
        "fire_events": ["identity_compromised"],
    },

    # ── 军事动向 (10) ──
    {
        "id": "de_troop_movement",
        "name": "部队异常调动",
        "description": "检测到未经授权的部队调动迹象。",
        "condition": "script_variables.coup_readiness >= 40",
        "cooldown": 6,
        "priority": 65,
        "effects": [
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 5},
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "add", "value": 1},
            {"type": "narrative_callback", "text": "深夜的军营里传来引擎轰鸣——有人在暗中调动部队。", "priority": "high"},
        ],
        "fire_events": ["coup_imminent"],
    },
    {
        "id": "de_coup_countdown",
        "name": "政变倒计时",
        "description": "政变准备已接近完成，进入最终倒计时。",
        "condition": "script_variables.coup_readiness >= 80",
        "cooldown": 15,
        "priority": 80,
        "effects": [
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 10},
            {"type": "activate_state", "id": "military_lockdown"},
            {"type": "narrative_callback", "text": "一切准备就绪——政变的时针已经开始倒数。", "priority": "critical"},
        ],
        "fire_events": ["coup_imminent"],
    },
    {
        "id": "de_arms_cache_discovery",
        "name": "武器库发现",
        "description": "发现秘密储存的武器弹药。",
        "condition": "script_variables.weapons_secured >= 50",
        "cooldown": 12,
        "priority": 45,
        "effects": [
            {"type": "state_change", "target": "script_variables.weapons_secured", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "一处隐蔽的武器库被发现——弹药箱整齐码放，足以装备一个营。", "priority": "high"},
        ],
    },
    {
        "id": "de_military_standoff",
        "name": "军事对峙",
        "description": "不同派系的部队发生对峙。",
        "condition": "script_variables.troops_loyalty <= 35 AND script_variables.military_alert_level >= 3",
        "cooldown": 10,
        "priority": 70,
        "effects": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.civilian_casualties", "op": "add", "value": 2},
            {"type": "narrative_callback", "text": "坦克在首尔街头对峙——枪口指向彼此，城市笼罩在战争的阴影中。", "priority": "critical"},
        ],
        "fire_events": ["military_crisis"],
    },
    {
        "id": "de_garrison_defection",
        "name": "驻军倒戈",
        "description": "关键驻军宣布转投新军部一方。",
        "condition": "script_variables.hanahoe_infiltration >= 60 AND script_variables.troops_loyalty <= 40",
        "cooldown": 12,
        "priority": 60,
        "effects": [
            {"type": "state_change", "target": "script_variables.troops_loyalty", "op": "add", "value": -15},
            {"type": "state_change", "target": "script_variables.coup_readiness", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "又一支部队倒向新军部——忠于总统的力量正在快速瓦解。", "priority": "high"},
        ],
    },
    {
        "id": "de_communication_intercept",
        "name": "通讯截获",
        "description": "截获了重要的军事通讯情报。",
        "condition": "script_variables.communication_security <= 50",
        "cooldown": 7,
        "priority": 50,
        "effects": [
            {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 1},
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "一段加密电报被成功破译——内容令人不寒而栗。", "priority": "high"},
        ],
    },
    {
        "id": "de_martial_law_extension",
        "name": "戒严令延长",
        "description": "当局决定延长戒严状态。",
        "condition": "script_variables.military_alert_level >= 3 AND script_variables.protest_intensity >= 40",
        "cooldown": 10,
        "priority": 55,
        "effects": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": -5},
            {"type": "activate_state", "id": "curfew_enforced"},
            {"type": "narrative_callback", "text": "戒严令再次延长——街道上只有军靴的回声。", "priority": "high"},
        ],
        "fire_events": ["martial_law_declared"],
    },
    {
        "id": "de_border_alert",
        "name": "边境警报",
        "description": "南北边境出现异常活动。",
        "condition": "script_variables.military_alert_level >= 3",
        "cooldown": 15,
        "priority": 40,
        "effects": [
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "add", "value": 1},
            {"type": "activate_state", "id": "ceasefire_tension"},
            {"type": "narrative_callback", "text": "三八线以北传来异动——是真正的威胁还是转移注意力的把戏？", "priority": "high"},
        ],
    },
    {
        "id": "de_weapon_smuggling",
        "name": "武器走私",
        "description": "有人秘密转运武器弹药。",
        "condition": "script_variables.coup_readiness >= 30 AND script_variables.weapons_secured < 60",
        "cooldown": 8,
        "priority": 45,
        "effects": [
            {"type": "state_change", "target": "script_variables.weapons_secured", "op": "add", "value": 15},
            {"type": "narrative_callback", "text": "深夜的卡车悄然驶入军营后门——箱子里装的不是补给品。", "priority": "medium"},
        ],
    },
    {
        "id": "de_security_breach",
        "name": "安保漏洞",
        "description": "关键设施的安保出现漏洞。",
        "condition": "script_variables.communication_security <= 40 AND script_variables.cover_integrity <= 60",
        "cooldown": 9,
        "priority": 55,
        "effects": [
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "安保系统出现了缺口——有人的身份可能已经暴露。", "priority": "high"},
        ],
    },

    # ── 民众运动 (10) ──
    {
        "id": "de_campus_rally",
        "name": "校园集会",
        "description": "大学生组织大规模校园集会。",
        "condition": "script_variables.protest_intensity >= 50",
        "cooldown": 5,
        "priority": 50,
        "effects": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
            {"type": "state_change", "target": "script_variables.student_network_size", "op": "add", "value": 3},
            {"type": "narrative_callback", "text": "又一场校园集会爆发——年轻人的怒火正在汇聚成河。", "priority": "medium"},
        ],
        "fire_events": ["campus_unrest"],
        "chain_events": [{"event_id": "de_crackdown", "delay": 2}],
    },
    {
        "id": "de_crackdown",
        "name": "镇压行动",
        "description": "军警对抗议者发动镇压。",
        "condition": "script_variables.protest_intensity >= 60 AND script_variables.military_alert_level >= 2",
        "cooldown": 7,
        "priority": 65,
        "effects": [
            {"type": "state_change", "target": "script_variables.civilian_casualties", "op": "add", "value": 5},
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "催泪弹和警棍——街头变成了战场，血迹染红了人行道。", "priority": "critical"},
        ],
        "fire_events": ["crackdown_occurred"],
        "chain_events": [{"event_id": "de_international_outcry", "delay": 1}],
    },
    {
        "id": "de_general_strike",
        "name": "总罢工",
        "description": "工人阶级发动大规模总罢工。",
        "condition": "script_variables.protest_intensity >= 75 AND script_variables.civilian_casualties >= 10",
        "cooldown": 15,
        "priority": 70,
        "effects": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -15},
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "工厂停工、商铺关门——整座城市陷入瘫痪，民众用沉默表达愤怒。", "priority": "critical"},
        ],
        "fire_events": ["general_strike"],
    },
    {
        "id": "de_underground_pamphlet",
        "name": "地下传单潮",
        "description": "地下组织大量散发反体制传单。",
        "condition": "script_variables.student_network_size >= 20 AND script_variables.media_freedom <= 30",
        "cooldown": 5,
        "priority": 35,
        "effects": [
            {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": 3},
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 3},
            {"type": "narrative_callback", "text": "传单像雪花般飘落在校园和工厂——真相正在地下流通。", "priority": "medium"},
        ],
    },
    {
        "id": "de_memorial_protest",
        "name": "悼念示威",
        "description": "民众为牺牲者举行悼念活动。",
        "condition": "script_variables.civilian_casualties >= 5",
        "cooldown": 8,
        "priority": 45,
        "effects": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 8},
            {"type": "state_change", "target": "script_variables.student_network_size", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "烛光和白花——悼念变成了无声的抗议，悲伤化作了力量。", "priority": "high"},
        ],
    },
    {
        "id": "de_church_sanctuary",
        "name": "教会庇护",
        "description": "教会为被追捕的活动人士提供庇护。",
        "condition": "script_variables.civilian_casualties >= 8 AND script_variables.cover_integrity <= 70",
        "cooldown": 10,
        "priority": 35,
        "effects": [
            {"type": "state_change", "target": "script_variables.safe_house_count", "op": "add", "value": 1},
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "教堂的后门在深夜悄然打开——上帝的殿堂成为了最后的庇护所。", "priority": "medium"},
        ],
    },
    {
        "id": "de_labor_solidarity",
        "name": "工人声援",
        "description": "工人阶级声援学生运动。",
        "condition": "script_variables.protest_intensity >= 60 AND script_variables.student_network_size >= 30",
        "cooldown": 8,
        "priority": 50,
        "effects": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 10},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "工厂的汽笛长鸣——工人们走出车间，加入了学生的队伍。", "priority": "high"},
        ],
    },
    {
        "id": "de_media_resistance",
        "name": "媒体抗争",
        "description": "记者们以集体行动抵抗审查制度。",
        "condition": "script_variables.media_freedom <= 15 AND script_variables.protest_intensity >= 50",
        "cooldown": 12,
        "priority": 40,
        "effects": [
            {"type": "state_change", "target": "script_variables.media_freedom", "op": "add", "value": 8},
            {"type": "narrative_callback", "text": "一份空白的报纸——记者们用沉默表达了最响亮的抗议。", "priority": "medium"},
        ],
    },
    {
        "id": "de_student_leader_arrest",
        "name": "学生领袖被捕",
        "description": "运动核心人物被当局逮捕。",
        "condition": "script_variables.student_network_size >= 40",
        "cooldown": 10,
        "priority": 55,
        "effects": [
            {"type": "state_change", "target": "script_variables.student_network_size", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "学生领袖在深夜被带走——但镇压反而点燃了更大的怒火。", "priority": "high"},
        ],
    },
    {
        "id": "de_citizen_barricade",
        "name": "市民路障",
        "description": "市民自发筑起路障对抗军警。",
        "condition": "script_variables.protest_intensity >= 70 AND script_variables.civilian_casualties >= 8",
        "cooldown": 10,
        "priority": 60,
        "effects": [
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -8},
            {"type": "narrative_callback", "text": "公交车被推翻、轮胎燃烧——市民用血肉之躯筑起了最后的防线。", "priority": "critical"},
        ],
        "fire_events": ["democratic_confrontation"],
    },

    # ── 情报生存 (10) ──
    {
        "id": "de_cover_blown_rumor",
        "name": "身份暴露传闻",
        "description": "关于玩家真实身份的传闻开始流传。",
        "condition": "script_variables.cover_integrity <= 40",
        "cooldown": 8,
        "priority": 65,
        "effects": [
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "有人在背后窃窃私语——那些目光开始变得不对劲。", "priority": "critical"},
        ],
        "fire_events": ["identity_compromised"],
    },
    {
        "id": "de_safe_house_compromised",
        "name": "安全屋暴露",
        "description": "一处安全屋被敌方发现。",
        "condition": "script_variables.safe_house_count >= 2 AND script_variables.cover_integrity <= 50",
        "cooldown": 12,
        "priority": 55,
        "effects": [
            {"type": "state_change", "target": "script_variables.safe_house_count", "op": "add", "value": -1},
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "撤离！那个地方已经不安全了——门锁上的划痕说明有人来过。", "priority": "high"},
        ],
    },
    {
        "id": "de_double_agent_opportunity",
        "name": "双面谍机会",
        "description": "出现了打入敌方内部的机会。",
        "condition": "script_variables.cover_integrity >= 60 AND script_variables.betrayal_count == 0",
        "cooldown": 20,
        "priority": 40,
        "effects": [
            {"type": "activate_state", "id": "double_agent"},
            {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 2},
            {"type": "narrative_callback", "text": "一个危险的机会摆在面前——成为双面谍意味着走钢丝般的生活。", "priority": "high"},
        ],
    },
    {
        "id": "de_surveillance_tightened",
        "name": "监控加强",
        "description": "当局加强了对可疑人员的监控。",
        "condition": "script_variables.intelligence_leaks >= 2",
        "cooldown": 8,
        "priority": 50,
        "effects": [
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -8},
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -5},
            {"type": "narrative_callback", "text": "街角多了几个'读报纸'的人——监视的网正在收紧。", "priority": "high"},
        ],
    },
    {
        "id": "de_informant_contact",
        "name": "线人接头",
        "description": "线人带来了重要情报。",
        "condition": "script_variables.cover_integrity >= 40 AND script_variables.key_documents_found < 15",
        "cooldown": 5,
        "priority": 40,
        "effects": [
            {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 1},
            {"type": "narrative_callback", "text": "公园长椅上的一次短暂交谈——线人递过来的纸条或许能改变一切。", "priority": "medium"},
        ],
    },
    {
        "id": "de_pursuit_escape",
        "name": "追踪与脱逃",
        "description": "被敌方追踪，需要紧急脱身。",
        "condition": "script_variables.cover_integrity <= 30",
        "cooldown": 8,
        "priority": 70,
        "effects": [
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -5},
            {"type": "activate_state", "id": "player_hunted"},
            {"type": "narrative_callback", "text": "身后的脚步声越来越近——必须立刻消失在人群中。", "priority": "critical"},
        ],
    },
    {
        "id": "de_trust_network_expand",
        "name": "信任网络扩展",
        "description": "与新的可信赖人物建立联系。",
        "condition": "script_variables.cover_integrity >= 50 AND script_variables.betrayal_count <= 1",
        "cooldown": 10,
        "priority": 35,
        "effects": [
            {"type": "state_change", "target": "script_variables.safe_house_count", "op": "add", "value": 1},
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "又一个值得信赖的人加入了网络——暗号和接头地点已经约定好。", "priority": "medium"},
        ],
    },
    {
        "id": "de_document_discovery",
        "name": "机密文件发现",
        "description": "发现了关键的机密文件。",
        "condition": "script_variables.key_documents_found >= 3 AND script_variables.cover_integrity >= 30",
        "cooldown": 10,
        "priority": 55,
        "effects": [
            {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 2},
            {"type": "narrative_callback", "text": "档案室深处的一个信封——盖着最高机密印章的文件足以撼动整个政权。", "priority": "high"},
        ],
    },
    {
        "id": "de_betrayal_suspicion",
        "name": "背叛嫌疑",
        "description": "有人怀疑组织内部出了叛徒。",
        "condition": "script_variables.intelligence_leaks >= 3 AND script_variables.betrayal_count >= 1",
        "cooldown": 12,
        "priority": 60,
        "effects": [
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": -15},
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "猜疑像毒液一样蔓延——曾经信任的眼神变成了审视。", "priority": "critical"},
        ],
    },
    {
        "id": "de_emergency_extraction",
        "name": "紧急撤离",
        "description": "身份完全暴露，必须紧急撤离。",
        "condition": "script_variables.cover_integrity <= 15",
        "cooldown": 20,
        "priority": 80,
        "effects": [
            {"type": "activate_state", "id": "player_hunted"},
            {"type": "state_change", "target": "script_variables.safe_house_count", "op": "add", "value": -1},
            {"type": "narrative_callback", "text": "一切都结束了——烧掉文件，销毁证据，带上能带的东西，立刻消失。", "priority": "critical"},
        ],
    },

    # ── 跨线联动 (10) ──
    {
        "id": "de_international_outcry",
        "name": "国际舆论压力",
        "description": "国际社会对韩国局势表达强烈关切。",
        "condition": "script_variables.civilian_casualties >= 10 AND script_variables.diplomatic_crisis_level >= 1",
        "cooldown": 8,
        "priority": 55,
        "effects": [
            {"type": "state_change", "target": "script_variables.diplomatic_crisis_level", "op": "add", "value": 1},
            {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "西方媒体的头条上出现了首尔街头的照片——国际压力正在剧增。", "priority": "high"},
        ],
    },
    {
        "id": "de_us_ultimatum",
        "name": "美方最后通牒",
        "description": "美国向韩方发出严厉警告。",
        "condition": "script_variables.diplomatic_crisis_level >= 3",
        "cooldown": 20,
        "priority": 75,
        "effects": [
            {"type": "activate_state", "id": "foreign_intervention"},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "美国大使馆的车辆深夜驶入青瓦台——这不是友好访问。", "priority": "critical"},
        ],
        "fire_events": ["us_intervention"],
    },
    {
        "id": "de_regime_collapse_signal",
        "name": "政权崩塌信号",
        "description": "政权已到达崩塌临界点。",
        "condition": "script_variables.regime_stability <= 10 AND script_variables.coup_readiness >= 70",
        "cooldown": 20,
        "priority": 90,
        "effects": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "set", "value": 0},
            {"type": "activate_state", "id": "power_vacuum"},
            {"type": "narrative_callback", "text": "一个时代结束了——旧秩序在枪声和呐喊中轰然倒塌。", "priority": "critical"},
        ],
        "fire_events": ["regime_collapsed"],
    },
    {
        "id": "de_point_of_no_return",
        "name": "不归路",
        "description": "局势已经到了不可逆转的临界点。",
        "condition": "script_variables.coup_readiness >= 90 AND script_variables.regime_stability <= 15",
        "cooldown": 30,
        "priority": 95,
        "effects": [
            {"type": "narrative_callback", "text": "已经没有退路了——历史的车轮碾过了最后一个可以转向的路口。", "priority": "critical"},
        ],
        "fire_events": ["coup_imminent", "regime_collapsed"],
    },
    {
        "id": "de_diplomatic_channel",
        "name": "外交斡旋",
        "description": "国际势力通过外交渠道试图干预局势。",
        "condition": "script_variables.diplomatic_crisis_level >= 2 AND script_variables.us_confidence >= 30",
        "cooldown": 10,
        "priority": 45,
        "effects": [
            {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": 5},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": 3},
            {"type": "narrative_callback", "text": "外交官们穿梭于青瓦台和美国大使馆之间——还有和平解决的可能。", "priority": "medium"},
        ],
    },
    {
        "id": "de_economic_crisis",
        "name": "经济危机加深",
        "description": "政治动荡导致经济急剧恶化。",
        "condition": "script_variables.regime_stability <= 35 AND script_variables.protest_intensity >= 50",
        "cooldown": 10,
        "priority": 40,
        "effects": [
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -5},
            {"type": "state_change", "target": "script_variables.protest_intensity", "op": "add", "value": 5},
            {"type": "narrative_callback", "text": "股市暴跌、工厂倒闭——经济的崩溃正在加速政治的崩塌。", "priority": "high"},
        ],
    },
    {
        "id": "de_military_civilian_clash",
        "name": "军民冲突",
        "description": "军队与平民之间爆发直接冲突。",
        "condition": "script_variables.military_alert_level >= 3 AND script_variables.protest_intensity >= 65",
        "cooldown": 8,
        "priority": 65,
        "effects": [
            {"type": "state_change", "target": "script_variables.civilian_casualties", "op": "add", "value": 8},
            {"type": "state_change", "target": "script_variables.us_confidence", "op": "add", "value": -10},
            {"type": "state_change", "target": "script_variables.regime_stability", "op": "add", "value": -10},
            {"type": "narrative_callback", "text": "子弹射入人群——这不是镇压，这是屠杀。", "priority": "critical"},
        ],
        "fire_events": ["crackdown_occurred"],
    },
    {
        "id": "de_spy_network_activation",
        "name": "谍报网络激活",
        "description": "沉寂已久的情报网络全面激活。",
        "condition": "script_variables.military_alert_level >= 3 AND script_variables.key_documents_found >= 5",
        "cooldown": 15,
        "priority": 50,
        "effects": [
            {"type": "state_change", "target": "script_variables.communication_security", "op": "add", "value": 10},
            {"type": "state_change", "target": "script_variables.key_documents_found", "op": "add", "value": 3},
            {"type": "narrative_callback", "text": "所有的暗线同时开始传送信息——蛰伏已久的网络在最关键的时刻苏醒。", "priority": "high"},
        ],
    },
    {
        "id": "de_north_korea_provocation",
        "name": "北方挑衅",
        "description": "朝鲜利用韩国内乱进行军事挑衅。",
        "condition": "script_variables.military_alert_level >= 4 AND script_variables.regime_stability <= 25",
        "cooldown": 20,
        "priority": 50,
        "effects": [
            {"type": "activate_state", "id": "ceasefire_tension"},
            {"type": "state_change", "target": "script_variables.military_alert_level", "op": "set", "value": 4},
            {"type": "state_change", "target": "script_variables.diplomatic_crisis_level", "op": "add", "value": 1},
            {"type": "narrative_callback", "text": "三八线以北的炮声——北方在最不该的时候发出了威胁。", "priority": "critical"},
        ],
    },
    {
        "id": "de_safe_passage_established",
        "name": "安全通道建立",
        "description": "在混乱中建立了一条安全的撤退通道。",
        "condition": "script_variables.safe_house_count >= 2 AND script_variables.cover_integrity >= 30",
        "cooldown": 15,
        "priority": 35,
        "effects": [
            {"type": "activate_state", "id": "safe_passage"},
            {"type": "state_change", "target": "script_variables.cover_integrity", "op": "add", "value": 10},
            {"type": "narrative_callback", "text": "一条穿越混乱的安全路线已经确认——如果一切失败，至少还有退路。", "priority": "medium"},
        ],
    },
]


def fix_one_time_events(script):
    events = script.get("one_time_events", [])
    updated = 0
    for evt in events:
        eid = evt.get("id", "")
        # Add name
        if not evt.get("name") and eid in OT_NAMES:
            evt["name"] = OT_NAMES[eid]
            updated += 1
        # Add missing effects
        if eid in OT_MISSING_EFFECTS:
            existing = evt.get("effects", [])
            for eff in OT_MISSING_EFFECTS[eid]:
                if eff not in existing:
                    existing.append(eff)
            evt["effects"] = existing
            updated += 1
        # Add missing fire_events
        if eid in OT_MISSING_FIRE:
            existing = evt.get("fire_events", [])
            for fe in OT_MISSING_FIRE[eid]:
                if fe not in existing:
                    existing.append(fe)
            evt["fire_events"] = existing
            updated += 1
    # Fix OT[6] Chinese ID
    for evt in events:
        if evt.get("id") == "防潮水利工程剪彩揭幕":
            evt["id"] = "event_levee_ceremony"
            updated += 1
            break
    return updated


def fix_cyclic_events(script):
    events = script.get("cyclic_events", [])
    updated = 0
    for evt in events:
        eid = evt.get("id", "")
        # Add name
        if not evt.get("name") and eid in CY_NAMES:
            evt["name"] = CY_NAMES[eid]
            updated += 1
        elif eid in CY_NAMES and evt.get("name") != CY_NAMES[eid]:
            pass  # already has name
        # Fix format
        if eid in CY_FORMAT_FIX:
            fix = CY_FORMAT_FIX[eid]
            for k, v in fix.items():
                evt[k] = v
            # Remove old-style interval
            if "interval" in evt:
                del evt["interval"]
            updated += 1
    return updated


def add_dynamic_events(script):
    existing = script.get("dynamic_events", [])
    existing_ids = {de["id"] for de in existing}
    added = 0
    for de in DYNAMIC_EVENTS:
        if de["id"] not in existing_ids:
            existing.append(de)
            added += 1
    script["dynamic_events"] = existing
    return added


def validate(script):
    errors = []
    # Check OT names
    for evt in script.get("one_time_events", []):
        if not evt.get("name"):
            errors.append(f"OT '{evt['id']}' still missing name")
    # Check CY names
    for evt in script.get("cyclic_events", []):
        if not evt.get("name"):
            errors.append(f"CY '{evt['id']}' still missing name")
    # Check CY format
    for evt in script.get("cyclic_events", []):
        if not evt.get("frequency_value") or not evt.get("frequency_unit"):
            if not evt.get("interval"):
                errors.append(f"CY '{evt['id']}' missing frequency fields")
    # Check DE conditions
    for de in script.get("dynamic_events", []):
        cond = de.get("condition", "")
        if cond and not any(op in cond for op in [">=", "<=", ">", "<", "==", "!="]):
            errors.append(f"DE '{de['id']}' bad condition format: {cond}")
    # Check DE chain_events reference
    de_ids = {de["id"] for de in script.get("dynamic_events", [])}
    for de in script.get("dynamic_events", []):
        for chain in de.get("chain_events", []):
            if chain.get("event_id") not in de_ids:
                errors.append(f"DE '{de['id']}' chain references missing '{chain['event_id']}'")
    # Check activate_state references
    ps_ids = {ps["id"] for ps in script.get("persistent_states", [])}
    for src_type, src_list in [("OT", script.get("one_time_events", [])),
                                ("CY", script.get("cyclic_events", [])),
                                ("DE", script.get("dynamic_events", []))]:
        for evt in src_list:
            for eff in evt.get("effects", []):
                if eff.get("type") == "activate_state":
                    sid = eff.get("id", "")
                    if sid and sid not in ps_ids:
                        errors.append(f"{src_type} '{evt['id']}' activates unknown state '{sid}'")
    return errors


def main():
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT content FROM scripts WHERE id=?", (SCRIPT_ID,)).fetchone()
    if not row:
        print(f"ERROR: script '{SCRIPT_ID}' not found")
        sys.exit(1)
    script = json.loads(row[0])

    print("=" * 60)
    print("Event Audit & Fix")
    print("=" * 60)

    n1 = fix_one_time_events(script)
    print(f"[1] One-Time Events fixed: {n1} changes")

    n2 = fix_cyclic_events(script)
    print(f"[2] Cyclic Events fixed: {n2} changes")

    n3 = add_dynamic_events(script)
    print(f"[3] Dynamic Events added: {n3}")

    # Validate
    errors = validate(script)
    if errors:
        print(f"\n[FAIL] {len(errors)} errors:")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("\n[OK] All validations passed")

    # Stats
    print(f"\nFinal counts:")
    print(f"  One-Time Events: {len(script.get('one_time_events', []))}")
    print(f"  Cyclic Events: {len(script.get('cyclic_events', []))}")
    print(f"  Dynamic Events: {len(script.get('dynamic_events', []))}")
    print(f"  Variables: {len(script.get('variables', []))}")
    print(f"  Persistent States: {len(script.get('persistent_states', []))}")
    print(f"  Triggers: {len(script.get('triggers', []))}")

    # Write back
    content = json.dumps(script, ensure_ascii=False, indent=None)
    conn.execute("UPDATE scripts SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (content, SCRIPT_ID))
    conn.commit()
    conn.close()
    print("\n[DONE] Written to DB.")


if __name__ == "__main__":
    main()
