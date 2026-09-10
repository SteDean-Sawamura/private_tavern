# -*- coding: utf-8 -*-
"""Update DnD script: add class_system setting, fix attribute keys, add presets."""
import sqlite3, json, sys, os

sys.stdout.reconfigure(encoding="utf-8")
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tavern.db")
db = sqlite3.connect(db_path)

row = db.execute("SELECT content FROM scripts WHERE id = ?", ("dnd_open_world_ashenvale",)).fetchone()
data = json.loads(row[0])

# 1. Fix attribute keys: Chinese -> English
ATTR_KEY_MAP = {
    "力量": "STR", "敏捷": "DEX", "体质": "CON",
    "智力": "INT", "感知": "WIS", "魅力": "CHA",
    "生命值": "HP", "魔力值": "MP", "金币": "gold", "声望": "reputation",
}

pc = data["player_character"]
old_attrs = pc.get("attributes", {})
new_attrs = {}
for old_key, attr_def in old_attrs.items():
    new_key = ATTR_KEY_MAP.get(old_key, old_key)
    if isinstance(attr_def, dict):
        attr_def["display_name"] = old_key  # keep Chinese as display_name
    new_attrs[new_key] = attr_def
pc["attributes"] = new_attrs

# 2. Also fix skill_check_map if values reference Chinese attribute names
old_scm = data.get("settings", {}).get("skill_check_map", {})
if old_scm:
    new_scm = {}
    for kw, attr in old_scm.items():
        new_scm[kw] = ATTR_KEY_MAP.get(attr, attr)
    data["settings"]["skill_check_map"] = new_scm

# 3. Add class_system setting
data["settings"]["class_system"] = "dnd5e"
data["settings"]["vector_memory_enabled"] = True
data["settings"]["ai_tools_enabled"] = True

# 3b. Adjust HP/MP attribute ranges for D&D 5e real formulas
#     D&D HP range: ~6 (Lv1 wizard) to ~200+ (Lv20 barbarian)
#     DMG Spell Points range: 0 to 133
hp_attr = pc.get("attributes", {}).get("HP")
if isinstance(hp_attr, dict):
    hp_attr["max"] = 300
    hp_attr["value"] = 10  # will be overridden by compute_hp
mp_attr = pc.get("attributes", {}).get("MP")
if isinstance(mp_attr, dict):
    mp_attr["max"] = 200
    mp_attr["value"] = 0   # will be overridden by compute_mp

# 4. Add player_presets for all 12 D&D classes
PRESETS = [
    {
        "id": "preset_fighter", "name": "铁壁·格伦", "class_id": "fighter", "level": 1,
        "bio": "退役佣兵，在无数战场上幸存，带着满身伤疤来到王冠城寻找新的生活。",
        "personality": "沉稳可靠，重视荣誉和战友情谊。",
        "portrait_desc": "壮硕的男子，短棕发，脸上有一道刀疤，穿着磨损的链甲。",
        "skill_proficiencies": ["athletics", "intimidation"],
        "attributes": {"STR": {"value": 65}, "CON": {"value": 60}},
        "initial_inventory": [{"item": "长剑", "quantity": 1}, {"item": "链甲", "quantity": 1}, {"item": "盾牌", "quantity": 1}],
    },
    {
        "id": "preset_wizard", "name": "星辉·艾琳", "class_id": "wizard", "level": 1,
        "bio": "奥术学院的年轻毕业生，渴望探索上古魔法的奥秘。",
        "personality": "好奇心旺盛，博学多才，有时过于沉迷研究而忽略周围危险。",
        "portrait_desc": "纤瘦的精灵女子，银白长发，戴着半月形眼镜，手持雕花法杖。",
        "skill_proficiencies": ["arcana", "investigation"],
        "attributes": {"INT": {"value": 70}, "WIS": {"value": 60}},
        "initial_inventory": [{"item": "法杖", "quantity": 1}, {"item": "法术书", "quantity": 1}, {"item": "施法材料包", "quantity": 1}],
    },
    {
        "id": "preset_rogue", "name": "影刺·莱恩", "class_id": "rogue", "level": 1,
        "bio": "暗影之手的前成员，因一次任务失败而脱离组织，如今独自在王冠城讨生活。",
        "personality": "机敏狡黠，不轻易信人，但对弱者怀有一份隐秘的同情。",
        "portrait_desc": "瘦削的年轻男子，黑发遮眼，穿着深色皮甲，腰间别着短剑。",
        "skill_proficiencies": ["stealth", "sleight_of_hand", "perception", "deception"],
        "attributes": {"DEX": {"value": 70}, "INT": {"value": 60}, "gold": {"value": 35}},
        "initial_inventory": [{"item": "短剑", "quantity": 2}, {"item": "皮甲", "quantity": 1}, {"item": "盗贼工具", "quantity": 1}],
    },
    {
        "id": "preset_cleric", "name": "圣光·赛琳娜", "class_id": "cleric", "level": 1,
        "bio": "圣光教会的见习祭司，被派往王冠城调查教会内部的分裂传闻。",
        "personality": "温和虔诚，正义感强，有时对信仰的坚持会与现实产生冲突。",
        "portrait_desc": "庄重的女性，金色短发，穿着白银圣袍，胸前挂着太阳神徽。",
        "skill_proficiencies": ["insight", "medicine"],
        "attributes": {"WIS": {"value": 70}, "CHA": {"value": 60}},
        "initial_inventory": [{"item": "锤矛", "quantity": 1}, {"item": "链甲", "quantity": 1}, {"item": "圣徽", "quantity": 1}],
    },
    {
        "id": "preset_ranger", "name": "孤鹰·索恩", "class_id": "ranger", "level": 1,
        "bio": "来自灰烬森林的猎人，追踪一只从北方山脉南下的异常魔兽来到了王冠城。",
        "personality": "寡言独行，与自然亲近，对城市生活感到不适但不得不适应。",
        "portrait_desc": "精瘦的男子，深绿斗篷，背负长弓，肩上栖着一只训练有素的猎鹰。",
        "skill_proficiencies": ["perception", "survival", "stealth"],
        "attributes": {"DEX": {"value": 65}, "WIS": {"value": 60}},
        "initial_inventory": [{"item": "长弓", "quantity": 1}, {"item": "弯刀", "quantity": 2}, {"item": "皮甲", "quantity": 1}],
    },
    {
        "id": "preset_bard", "name": "银舌·菲奥娜", "class_id": "bard", "level": 1,
        "bio": "四处游历的吟游诗人，用歌声和故事换取旅途的食宿，到处收集传说。",
        "personality": "热情开朗，善于社交，内心却藏着无人知晓的秘密。",
        "portrait_desc": "红发的年轻女子，穿着色彩斑斓的旅装，背着一把精致的诗琴。",
        "skill_proficiencies": ["performance", "persuasion", "deception"],
        "attributes": {"CHA": {"value": 70}, "DEX": {"value": 60}},
        "initial_inventory": [{"item": "诗琴", "quantity": 1}, {"item": "皮甲", "quantity": 1}, {"item": "匕首", "quantity": 1}],
    },
    {
        "id": "preset_paladin", "name": "誓盾·加雷斯", "class_id": "paladin", "level": 1,
        "bio": "誓守正义的骑士，从远方王国来此寻找失踪的同袍骑士。",
        "personality": "正直勇敢，信守誓言，有时过于固执地坚持原则。",
        "portrait_desc": "高大的男子，金色铠甲外罩白色斗篷，腰间悬挂长剑和圣徽。",
        "skill_proficiencies": ["athletics", "persuasion"],
        "attributes": {"STR": {"value": 65}, "CHA": {"value": 60}},
        "initial_inventory": [{"item": "长剑", "quantity": 1}, {"item": "盾牌", "quantity": 1}, {"item": "链甲", "quantity": 1}, {"item": "圣徽", "quantity": 1}],
    },
    {
        "id": "preset_barbarian", "name": "霜牙·乌尔加", "class_id": "barbarian", "level": 1,
        "bio": "北方冰原部落的流放战士，因违背族长意志被驱逐，南下寻找新的归宿。",
        "personality": "暴烈直率，鄙视诡计和欺骗，对强者心怀敬意。",
        "portrait_desc": "魁梧的兽人混血女性，编成辫的黑发，裸露的手臂上刺满部落图腾。",
        "skill_proficiencies": ["athletics", "survival"],
        "attributes": {"STR": {"value": 70}, "CON": {"value": 65}},
        "initial_inventory": [{"item": "巨斧", "quantity": 1}, {"item": "手斧", "quantity": 2}],
    },
    {
        "id": "preset_warlock", "name": "暗契·维克托", "class_id": "warlock", "level": 1,
        "bio": "与异界存在缔结了禁忌契约的神秘人，契约的代价至今未完全显现。",
        "personality": "阴郁寡言，对契约的秘密讳莫如深，偶尔展现出超乎常人的洞察力。",
        "portrait_desc": "苍白瘦削的男子，深色长袍，左眼隐约闪烁着不属于凡间的紫光。",
        "skill_proficiencies": ["arcana", "deception"],
        "attributes": {"CHA": {"value": 70}, "INT": {"value": 60}},
        "initial_inventory": [{"item": "轻弩", "quantity": 1}, {"item": "皮甲", "quantity": 1}, {"item": "奥术法器", "quantity": 1}],
    },
    {
        "id": "preset_druid", "name": "根脉·塔露", "class_id": "druid", "level": 1,
        "bio": "灰烬森林的守护者，感应到大地深处封印的异动，来到王冠城寻找答案。",
        "personality": "亲和平静，尊重一切生命，对破坏自然的行为深恶痛绝。",
        "portrait_desc": "精灵女性，褐色长发编入藤蔓，身披树叶编织的斗篷，手持橡木法杖。",
        "skill_proficiencies": ["nature", "perception"],
        "attributes": {"WIS": {"value": 70}, "CON": {"value": 60}},
        "initial_inventory": [{"item": "木盾", "quantity": 1}, {"item": "弯刀", "quantity": 1}, {"item": "德鲁伊法器", "quantity": 1}],
    },
    {
        "id": "preset_monk", "name": "铁拳·玄清", "class_id": "monk", "level": 1,
        "bio": "东方远行的武僧，修行内功和武术，以苦修磨砺身心。",
        "personality": "沉静内敛，追求武道极致，对物质欲望淡泊。",
        "portrait_desc": "光头的年轻男子，穿着简朴的僧袍，赤脚行走，双手布满茧。",
        "skill_proficiencies": ["acrobatics", "stealth"],
        "attributes": {"DEX": {"value": 65}, "WIS": {"value": 60}},
        "initial_inventory": [{"item": "短剑", "quantity": 1}, {"item": "飞镖", "quantity": 10}],
    },
    {
        "id": "preset_sorcerer", "name": "血焰·伊莎贝尔", "class_id": "sorcerer", "level": 1,
        "bio": "体内流淌着远古龙血的术士，魔力时常不受控制地涌出。",
        "personality": "自信骄傲，对自身血脉力量既自豪又恐惧，渴望掌控这份力量。",
        "portrait_desc": "红瞳的年轻女子，深红长发，皮肤上偶尔闪过鳞片般的光泽。",
        "skill_proficiencies": ["arcana", "persuasion"],
        "attributes": {"CHA": {"value": 70}, "CON": {"value": 60}},
        "initial_inventory": [{"item": "轻弩", "quantity": 1}, {"item": "奥术法器", "quantity": 1}],
    },
]

data["player_presets"] = PRESETS

# 5. Add Story Tree
STORY_TREE = {
    "trees": [
        {
            "id": "succession",
            "name": "王位继承",
            "description": "王冠城的权力更迭",
            "icon": "crown",
            "nodes": [
                {
                    "id": "st_succ_rumors",
                    "name": "风闻传言",
                    "description": "在城中听到关于王位空悬的传闻",
                    "type": "auto",
                    "requires": [],
                    "condition": "succession_tension >= 20",
                    "effects": {
                        "notify": "【剧情】你听到了关于王位继承纷争的传言……",
                        "activate_lore": ["lore_succession_crisis"],
                    },
                    "on_complete_unlock": ["st_succ_investigate"],
                },
                {
                    "id": "st_succ_investigate",
                    "name": "调查内幕",
                    "description": "深入了解继承争端的各方势力",
                    "type": "quest",
                    "requires": ["st_succ_rumors"],
                    "condition": "",
                    "effects": {
                        "set_var": [{"var_id": "succession_tension", "op": "inc", "value": 10}],
                        "notify": "【剧情】你对王位继承纷争有了更深的了解。",
                    },
                    "on_complete_unlock": ["st_succ_choose_side"],
                },
                {
                    "id": "st_succ_choose_side",
                    "name": "选择立场",
                    "description": "支持哪一方？",
                    "type": "choice",
                    "requires": ["st_succ_investigate"],
                    "condition": "succession_tension >= 40",
                    "choices": [
                        {
                            "id": "support_prince",
                            "label": "支持王子",
                            "description": "年轻的正统继承人，理想主义者",
                            "effects": {
                                "inject_prompt": "玩家选择支持年轻的王子。他的支持者多为理想主义贵族和平民。",
                                "activate_lore": ["lore_prince_faction"],
                            },
                            "unlock": ["st_succ_prince_rally"],
                        },
                        {
                            "id": "support_duke",
                            "label": "支持公爵",
                            "description": "经验老到的军事统帅",
                            "effects": {
                                "inject_prompt": "玩家选择支持公爵。他的支持者多为军方将领和保守贵族。",
                                "activate_lore": ["lore_duke_faction"],
                            },
                            "unlock": ["st_succ_duke_rally"],
                        },
                        {
                            "id": "stay_neutral",
                            "label": "保持中立",
                            "description": "两不相帮，寻找第三条路",
                            "effects": {
                                "inject_prompt": "玩家选择不站队，试图在两派之间周旋。",
                            },
                            "unlock": ["st_succ_mediator"],
                        },
                    ],
                },
                {
                    "id": "st_succ_prince_rally",
                    "name": "集结王子阵营",
                    "description": "帮助王子争取更多支持者",
                    "type": "quest",
                    "requires": ["st_succ_choose_side"],
                    "condition": "",
                    "effects": {
                        "set_var": [{"var_id": "succession_tension", "op": "inc", "value": 15}],
                        "notify": "【剧情】王子的势力日渐壮大。",
                    },
                    "on_complete_unlock": ["st_succ_crisis"],
                },
                {
                    "id": "st_succ_duke_rally",
                    "name": "巩固公爵势力",
                    "description": "帮助公爵控制更多军事要塞",
                    "type": "quest",
                    "requires": ["st_succ_choose_side"],
                    "condition": "",
                    "effects": {
                        "set_var": [{"var_id": "succession_tension", "op": "inc", "value": 15}],
                        "notify": "【剧情】公爵的军事力量进一步增强。",
                    },
                    "on_complete_unlock": ["st_succ_crisis"],
                },
                {
                    "id": "st_succ_mediator",
                    "name": "居中调停",
                    "description": "寻找和平解决王位纷争的方案",
                    "type": "quest",
                    "requires": ["st_succ_choose_side"],
                    "condition": "",
                    "effects": {
                        "set_var": [{"var_id": "succession_tension", "op": "dec", "value": 20}],
                        "notify": "【剧情】你的调停初见成效，紧张局势有所缓和。",
                    },
                    "on_complete_unlock": ["st_succ_crisis"],
                },
                {
                    "id": "st_succ_crisis",
                    "name": "继承危机",
                    "description": "局势走向最终摊牌",
                    "type": "auto",
                    "requires": [],
                    "condition": "succession_tension >= 70",
                    "effects": {
                        "notify": "【重大事件】王位继承危机达到顶点！",
                        "inject_prompt": "王位继承争端已经到了不可逆转的临界点，各方势力即将摊牌。",
                    },
                },
            ],
        },
        {
            "id": "seal_investigation",
            "name": "封印调查",
            "description": "远古龙封印的秘密",
            "icon": "eye",
            "nodes": [
                {
                    "id": "st_seal_tremor",
                    "name": "异常震动",
                    "description": "大地深处传来不明震动",
                    "type": "auto",
                    "requires": [],
                    "condition": "seal_integrity <= 80",
                    "effects": {
                        "notify": "【剧情】你感受到了来自地底的异常震动……",
                    },
                    "on_complete_unlock": ["st_seal_clues"],
                },
                {
                    "id": "st_seal_clues",
                    "name": "搜寻线索",
                    "description": "调查震动的来源和封印的状态",
                    "type": "quest",
                    "requires": ["st_seal_tremor"],
                    "condition": "",
                    "effects": {
                        "activate_lore": ["lore_dragon_war"],
                        "set_var": [{"var_id": "dragon_awakening", "op": "inc", "value": 10}],
                        "notify": "【剧情】你发现了远古龙封印的蛛丝马迹。",
                    },
                    "on_complete_unlock": ["st_seal_expedition"],
                },
                {
                    "id": "st_seal_expedition",
                    "name": "深入调查",
                    "description": "前往灰岩山脉探查封印遗迹",
                    "type": "quest",
                    "requires": ["st_seal_clues"],
                    "condition": "dragon_awakening >= 15",
                    "effects": {
                        "set_var": [{"var_id": "seal_integrity", "op": "dec", "value": 10}],
                        "unlock_locations": ["grey_mountain_ruins"],
                        "notify": "【剧情】你在灰岩山脉发现了封印遗迹的入口。",
                    },
                    "on_complete_unlock": ["st_seal_choice"],
                },
                {
                    "id": "st_seal_choice",
                    "name": "封印的抉择",
                    "description": "修复封印还是释放其力量？",
                    "type": "choice",
                    "requires": ["st_seal_expedition"],
                    "condition": "",
                    "choices": [
                        {
                            "id": "repair_seal",
                            "label": "修复封印",
                            "description": "维护千年来的安宁",
                            "effects": {
                                "set_var": [{"var_id": "seal_integrity", "op": "inc", "value": 30}],
                                "inject_prompt": "玩家决定修复远古封印，阻止龙的苏醒。",
                            },
                            "unlock": ["st_seal_repair"],
                        },
                        {
                            "id": "break_seal",
                            "label": "破坏封印",
                            "description": "释放远古之力，后果难料",
                            "effects": {
                                "set_var": [{"var_id": "seal_integrity", "op": "dec", "value": 30},
                                            {"var_id": "dragon_awakening", "op": "inc", "value": 30}],
                                "inject_prompt": "玩家选择破坏封印！远古龙力开始涌出。",
                            },
                            "unlock": ["st_seal_dragon"],
                        },
                    ],
                },
                {
                    "id": "st_seal_repair",
                    "name": "封印修复",
                    "description": "收集材料修复远古封印",
                    "type": "quest",
                    "requires": ["st_seal_choice"],
                    "condition": "",
                    "effects": {
                        "set_var": [{"var_id": "seal_integrity", "op": "set", "value": 95}],
                        "notify": "【重大成就】你成功修复了远古封印，大地恢复了安宁。",
                    },
                },
                {
                    "id": "st_seal_dragon",
                    "name": "龙之觉醒",
                    "description": "封印破碎，远古巨龙即将苏醒",
                    "type": "auto",
                    "requires": ["st_seal_choice"],
                    "condition": "dragon_awakening >= 50",
                    "effects": {
                        "notify": "【灾变】远古龙开始苏醒，大地震颤不止！",
                        "inject_prompt": "封印破碎，远古巨龙的力量开始渗透世界。灾变即将降临。",
                    },
                },
            ],
        },
        {
            "id": "guild_rise",
            "name": "公会崛起",
            "description": "冒险者公会的成长之路",
            "icon": "sword",
            "nodes": [
                {
                    "id": "st_guild_join",
                    "name": "加入公会",
                    "description": "成为冒险者公会的正式成员",
                    "type": "auto",
                    "requires": [],
                    "condition": "guild_reputation >= 5",
                    "effects": {
                        "notify": "【剧情】你正式成为了冒险者公会的成员。",
                    },
                    "on_complete_unlock": ["st_guild_first_quest"],
                },
                {
                    "id": "st_guild_first_quest",
                    "name": "首次委托",
                    "description": "接下并完成第一个公会任务",
                    "type": "quest",
                    "requires": ["st_guild_join"],
                    "condition": "",
                    "effects": {
                        "set_var": [{"var_id": "guild_reputation", "op": "inc", "value": 10}],
                        "notify": "【剧情】你完成了第一个公会委托，开始崭露头角。",
                    },
                    "on_complete_unlock": ["st_guild_silver"],
                },
                {
                    "id": "st_guild_silver",
                    "name": "白银冒险者",
                    "description": "声望提升至白银级",
                    "type": "auto",
                    "requires": ["st_guild_first_quest"],
                    "condition": "guild_reputation >= 30",
                    "effects": {
                        "notify": "【晋升】你被提升为白银级冒险者！",
                        "inject_prompt": "玩家已是白银级冒险者，可以接取更高难度的任务。",
                    },
                    "on_complete_unlock": ["st_guild_gold"],
                },
                {
                    "id": "st_guild_gold",
                    "name": "黄金冒险者",
                    "description": "成为公会精英",
                    "type": "auto",
                    "requires": ["st_guild_silver"],
                    "condition": "guild_reputation >= 60",
                    "effects": {
                        "notify": "【晋升】你被提升为黄金级冒险者！公会对你委以重任。",
                        "inject_prompt": "玩家是黄金级冒险者，公会核心成员之一，有资格参与公会决策。",
                    },
                    "on_complete_unlock": ["st_guild_master"],
                },
                {
                    "id": "st_guild_master",
                    "name": "公会传奇",
                    "description": "成为传说级冒险者",
                    "type": "auto",
                    "requires": ["st_guild_gold"],
                    "condition": "guild_reputation >= 90",
                    "effects": {
                        "notify": "【传奇成就】你已成为冒险者公会的传奇！你的名字将被铭记于公会大厅。",
                    },
                },
            ],
        },
        {
            "id": "cult_shadow",
            "name": "暗影势力",
            "description": "虚空教团的阴谋",
            "icon": "skull",
            "nodes": [
                {
                    "id": "st_cult_whispers",
                    "name": "黑暗低语",
                    "description": "城中出现异端崇拜的迹象",
                    "type": "auto",
                    "requires": [],
                    "condition": "cult_influence >= 15",
                    "effects": {
                        "notify": "【剧情】你注意到城中弥漫着一股不寻常的暗流……",
                    },
                    "on_complete_unlock": ["st_cult_investigate"],
                },
                {
                    "id": "st_cult_investigate",
                    "name": "追踪教团",
                    "description": "调查虚空教团的活动",
                    "type": "quest",
                    "requires": ["st_cult_whispers"],
                    "condition": "",
                    "effects": {
                        "set_var": [{"var_id": "cult_influence", "op": "inc", "value": 5}],
                        "notify": "【剧情】你发现虚空教团的活动比想象中更加猖獗。",
                    },
                    "on_complete_unlock": ["st_cult_confront"],
                },
                {
                    "id": "st_cult_confront",
                    "name": "正面对抗",
                    "description": "揭露并打击虚空教团的核心成员",
                    "type": "quest",
                    "requires": ["st_cult_investigate"],
                    "condition": "cult_influence >= 25",
                    "effects": {
                        "set_var": [{"var_id": "cult_influence", "op": "dec", "value": 20}],
                        "notify": "【剧情】你给予虚空教团沉重一击！",
                    },
                    "on_complete_unlock": ["st_cult_leader"],
                },
                {
                    "id": "st_cult_leader",
                    "name": "教团首脑",
                    "description": "找到并击败虚空教团的幕后主使",
                    "type": "quest",
                    "requires": ["st_cult_confront"],
                    "condition": "",
                    "effects": {
                        "set_var": [{"var_id": "cult_influence", "op": "set", "value": 0}],
                        "notify": "【重大胜利】虚空教团的阴谋被彻底粉碎！",
                        "inject_prompt": "虚空教团已被瓦解，城市恢复了安宁。但黑暗的种子是否真的被根除？",
                    },
                },
            ],
        },
    ],
}

data["story_tree"] = STORY_TREE

# Also add variables needed by story tree that may not exist yet
existing_var_ids = {v["id"] for v in data.get("variables", [])}
needed_vars = [
    {"id": "knows_succession", "name": "knows_succession", "type": "bool", "default": False},
    {"id": "faction", "name": "faction", "type": "string", "default": ""},
]
for nv in needed_vars:
    if nv["id"] not in existing_var_ids:
        data.setdefault("variables", []).append(nv)

# 6. Write back
new_content = json.dumps(data, ensure_ascii=False)
db.execute("UPDATE scripts SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
           (new_content, "dnd_open_world_ashenvale"))
db.commit()
print(f"Updated dnd_open_world_ashenvale: {len(new_content)} chars")
print(f"  class_system: {data['settings'].get('class_system')}")
print(f"  player_presets: {len(data.get('player_presets', []))} presets")
print(f"  PC attributes: {list(pc.get('attributes', {}).keys())}")
st = data.get("story_tree", {})
print(f"  story_tree: {len(st.get('trees', []))} trees, {sum(len(t.get('nodes', [])) for t in st.get('trees', []))} nodes")

# Also update JSON file
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "scripts", "dnd_open_world_ashenvale.json"),
          "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
print("  JSON file also updated")

db.close()
