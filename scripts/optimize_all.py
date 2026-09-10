# -*- coding: utf-8 -*-
"""Optimize all 3 scripts in database."""
import sqlite3, json, sys, os

sys.stdout.reconfigure(encoding="utf-8")
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tavern.db")
db = sqlite3.connect(db_path)

LQ = "“"  # left curly double quote
RQ = "”"  # right curly double quote

# ===============================================================
# 1. QIANFU
# ===============================================================
row = db.execute("SELECT content FROM scripts WHERE id = ?", ("qianfu",)).fetchone()
data = json.loads(row[0])

pc = data["player_character"]
pc["bio"] = f"原国民党军统特务，代号{LQ}深海{RQ}。抗战期间奉命刺杀汪伪汉奸李海丰，行动成功后被共产党地下组织策反，从此以军统身份为掩护，秘密为组织传递情报。外表沉稳寡言，内心深藏信仰。"
pc["personality"] = "沉稳冷静，心思缜密，善于察言观色。表面上是恪尽职守的国民党特务，实则对共产主义理想抱有坚定信念。在危险中保持克制，在利益面前不为所动。"
pc["long_term_goal"] = "在保密局天津站长期潜伏，传递关键军事情报给组织，同时保护身边的同志不被暴露，为解放事业贡献力量。"
pc["initial_location"] = "loc_tianjin_station"
pc["portrait_desc"] = "三十出头的男子，国字脸，短发，着深色中山装，眉宇间透着精明与克制。"
pc["attributes"] = {
    "disguise": {"value": 75, "min": 0, "max": 100, "display_name": "伪装技巧", "rule": "在军统内部维持身份掩护的能力。"},
    "composure": {"value": 70, "min": 0, "max": 100, "display_name": "心理素质", "rule": "面对审讯、盘查和突发状况时的镇定程度。"},
    "intelligence_analysis": {"value": 65, "min": 0, "max": 100, "display_name": "情报分析", "rule": "对情报价值的判断力和信息拼图的能力。"},
    "social_skill": {"value": 60, "min": 0, "max": 100, "display_name": "人际手腕", "rule": "在办公室政治中周旋、拉拢和化解矛盾的能力。"},
    "fitness": {"value": 55, "min": 0, "max": 100, "display_name": "体能", "rule": "追击、搏斗和逃脱时的身体素质。"},
}

data["opening"] = {
    "text": f"1945年9月，抗战胜利的消息传遍大江南北。你——余则成，刚完成刺杀汪伪特务头子李海丰的任务，被军统局长戴笠亲自嘉奖。然而在那次行动中，共产党地下工作者左蓝的牺牲深深震撼了你，你接受了组织的策反，成为了代号{LQ}深海{RQ}的潜伏者。\n\n如今，你被调往保密局天津站报到。站在天津站办公楼前，你深吸一口气，推开了那扇厚重的木门。走廊里弥漫着香烟和油墨的气味，几双目光不经意地扫过你——这里的每一个人，都可能是你的同事，也可能是你最危险的敌人。",
    "choices": [
        {"id": "open_0", "text": "先去站长办公室报到，给吴敬中留下良好的第一印象。",
         "result": {"type": "deterministic", "description": "你整理好仪容，敲开了站长办公室的门。吴敬中放下手中的茶杯，上下打量着你。",
                    "state_changes": [{"target": "player.social_skill", "op": "add", "value": 3}]}},
        {"id": "open_1", "text": "先在走廊里观察一圈，摸清站内各科室的布局和人员。",
         "result": {"type": "conditional", "description": "你不动声色地在走廊里踱步，将各科室的门牌和人员进出情况默默记在心里。",
                    "state_changes": [{"target": "player.intelligence_analysis", "op": "add", "value": 5}],
                    "conditions": [{"check": "dice < 40", "description": "你的探查引起了档案股长盛乡的注意，他投来一个审视的目光。", "state_changes": []}]}},
        {"id": "open_2", "text": "去机要室附近转转，试探能否接触到核心情报区域。",
         "result": {"type": "conditional", "description": "你大致了解了机要室的位置和门禁情况。",
                    "state_changes": [{"target": "player.intelligence_analysis", "op": "add", "value": 3}],
                    "conditions": [{"check": "dice > 70", "description": "你意外发现机要室的通风口正对着走廊拐角的一个储物间。",
                                    "state_changes": [{"target": "player.disguise", "op": "add", "value": 2}]}]}},
    ],
}

# NPC enhancements
npc_config = {
    "npc_mei_jie": {"talkativeness": 70, "default_location": "lime_townhouse", "schedule": [
        {"time_range": "06:00-10:00", "location": "lime_townhouse", "activity": "在家梳洗、安排家务"},
        {"time_range": "10:00-16:00", "location": "south_market", "activity": "与其他太太们喝茶、打牌、交换情报"},
        {"time_range": "16:00-06:00", "location": "lime_townhouse", "activity": "在家等候吴敬中，操持晚饭"}]},
    "npc_mu_wanqiu": {"talkativeness": 35, "default_location": "south_market", "schedule": [
        {"time_range": "06:00-12:00", "location": "south_market", "activity": "在家读书、练琴"},
        {"time_range": "12:00-18:00", "location": "south_market", "activity": "偶尔外出散步或参加文学沙龙"},
        {"time_range": "18:00-06:00", "location": "south_market", "activity": "在家独处，弹琴写日记"}]},
    "npc_wang_zhanjin": {"talkativeness": 60, "default_location": "south_market", "schedule": [
        {"time_range": "06:00-10:00", "location": "south_market", "activity": "在街头捡拾废品"},
        {"time_range": "10:00-16:00", "location": "haimen_dock", "activity": "在码头附近捡废铁、讨零工"},
        {"time_range": "16:00-06:00", "location": "south_market", "activity": "在茶馆外蹭热闹，寻找旧相识"}]},
    "npc_dai_li": {"talkativeness": 25, "default_location": "loc_tianjin_station", "schedule": [
        {"time_range": "06:00-22:00", "location": "loc_tianjin_station", "activity": "在南京或重庆遥控指挥，偶尔巡视天津站"},
        {"time_range": "22:00-06:00", "location": "", "activity": "不在天津"}]},
    "npc_sheng_xiang": {"talkativeness": 30, "default_location": "loc_tianjin_station", "schedule": [
        {"time_range": "07:00-12:00", "location": "loc_tianjin_station", "activity": "整理档案、处理文件归档"},
        {"time_range": "12:00-14:00", "location": "huang_restaurant", "activity": "外出吃饭"},
        {"time_range": "14:00-18:00", "location": "loc_tianjin_station", "activity": "继续档案工作，偷偷销毁与自己过去有关的记录"}]},
    "npc_hong_mishu": {"talkativeness": 55, "default_location": "loc_tianjin_station", "schedule": [
        {"time_range": "07:30-12:00", "location": "loc_tianjin_station", "activity": "在站长办公室整理公文、跑腿"},
        {"time_range": "12:00-14:00", "location": "south_market", "activity": "外出办私事或赴约"},
        {"time_range": "14:00-18:00", "location": "loc_tianjin_station", "activity": "继续处理日常事务"}]},
    "npc_ma_tai_tai": {"talkativeness": 65, "default_location": "lime_townhouse", "schedule": [
        {"time_range": "06:00-10:00", "location": "lime_townhouse", "activity": "在家照顾家事"},
        {"time_range": "10:00-16:00", "location": "south_market", "activity": "与梅姐等太太喝茶、打听消息"},
        {"time_range": "16:00-06:00", "location": "lime_townhouse", "activity": "在家等马奎，独自烦闷"}]},
    "npc_long_er": {"talkativeness": 75, "default_location": "haimen_dock", "schedule": [
        {"time_range": "06:00-10:00", "location": "haimen_dock", "activity": "在码头区巡视地盘，收取保护费"},
        {"time_range": "10:00-18:00", "location": "south_market", "activity": "在茶馆赌场消磨时间，结交三教九流"},
        {"time_range": "18:00-06:00", "location": "haimen_dock", "activity": "监督走私货物的装卸"}]},
}
for npc in data["npcs"]:
    cfg = npc_config.get(npc["id"])
    if cfg:
        npc["talkativeness"] = cfg["talkativeness"]
        npc["default_location"] = cfg["default_location"]
        npc["schedule"] = cfg["schedule"]

# Random items
data["random_items"] = [
    {"id": "suspicion_fluctuation", "description": "天津站内部的猜忌氛围波动，影响余则成被怀疑的风险。",
     "trigger": "每次余则成接触机密信息或与可疑人物接触时。", "trigger_type": "conditional",
     "dice": {"count": 1, "faces": 10, "modifier": -5}, "ranges": [
         {"min": -4, "max": -2, "label": "风平浪静", "description": "站内忙于日常事务，无人注意你的行踪。"},
         {"min": -1, "max": 1, "label": "暗流涌动", "description": "有人在闲聊中提到了内鬼的传言，气氛微妙。"},
         {"min": 2, "max": 3, "label": "草木皆兵", "description": "站内突然加强了安保检查，每个人都显得紧张。"},
         {"min": 4, "max": 5, "label": "风声鹤唢", "description": "有人举报站内有共产党潜伏，全站进入排查状态。",
          "state_changes": [{"target": "player.composure", "op": "add", "value": -5}]}]},
    {"id": "street_encounter", "description": "在天津街头活动时可能遇到的人或事。",
     "trigger": "当玩家在非站内地点活动时。", "trigger_type": "conditional",
     "dice": {"count": 1, "faces": 20, "modifier": 0}, "ranges": [
         {"min": 1, "max": 10, "label": "平安无事", "state_changes": []},
         {"min": 11, "max": 13, "label": "偶遇线人", "description": "一个面生的人递过来一张纸条就匆匆走了。",
          "state_changes": [{"target": "player.intelligence_analysis", "op": "add", "value": 3}]},
         {"min": 14, "max": 16, "label": "遭遇盘查", "description": "军警在街头设卡检查证件。", "state_changes": []},
         {"min": 17, "max": 18, "label": "发现跟踪", "description": "你注意到身后有人在跟踪你。",
          "state_changes": [{"target": "player.composure", "op": "add", "value": -3}]},
         {"min": 19, "max": 20, "label": "意外收获", "description": "无意中听到了有价值的情报碎片。",
          "state_changes": [{"target": "player.intelligence_analysis", "op": "add", "value": 5}]}]},
    {"id": "office_politics", "description": "站内办公室政治的日常变化。",
     "trigger": "永远生效。", "trigger_type": "always",
     "dice": {"count": 1, "faces": 100, "modifier": 0}, "ranges": [
         {"min": 1, "max": 40, "label": "平静", "description": "站内一切如常。"},
         {"min": 41, "max": 65, "label": "暗中较劲", "description": "马奎和陆桥山又在争夺功劳。"},
         {"min": 66, "max": 85, "label": "站长施压", "description": "吴敬中对近期工作不满意，训斥了下属。"},
         {"min": 86, "max": 95, "label": "人事变动", "description": "有传言说上级要调整天津站的人事安排。"},
         {"min": 96, "max": 100, "label": "重大任务", "description": "南京方面下达了紧急任务，全站进入紧张状态。"}]},
]

# Cyclic events
data["cyclic_events"] = [
    {"id": "station_morning_meeting", "description": "天津站每日晨会，站长吴敬中主持，通报近期任务和人事动态。",
     "frequency_value": 1, "frequency_unit": "day", "first_trigger": "1945-09-01T08:30:00", "expires_at": None},
    {"id": "intel_drop_window", "description": "组织安排的定期情报交接窗口，通常在黄记小馆或码头。",
     "frequency_value": 5, "frequency_unit": "day", "first_trigger": "1945-09-03T20:00:00", "expires_at": None},
    {"id": "madam_tea_party", "description": "梅姐组织的太太聚会，翠平需要出席应酬。",
     "frequency_value": 1, "frequency_unit": "week", "first_trigger": "1945-09-05T14:00:00", "expires_at": None},
    {"id": "security_patrol", "description": "天津站定期内部安全巡查，检查文件保密和人员动向。",
     "frequency_value": 2, "frequency_unit": "week", "first_trigger": "1945-09-02T10:00:00", "expires_at": None},
]

# One-time events
data["one_time_events"] = [
    {"id": "first_contact", "description": "余则成首次与天津地下组织接头人在黄记小馆秘密会面，确认联络暗号和情报传递方式。", "trigger_time": "1945-09-03T20:00:00"},
    {"id": "makui_suspicion", "description": "马奎开始怀疑站内有共产党卧底，暗中调查同事们的背景和行踪。", "trigger_time": "1945-09-15T09:00:00"},
    {"id": "luqiaoshan_scheme", "description": "陆桥山密谋扳倒马奎、争夺行动队长一职，试图拉拢余则成加入联盟。", "trigger_time": "1945-09-20T14:00:00"},
    {"id": "emei_peak_investigation", "description": f"{LQ}峨眉峰{RQ}代号身份排查行动启动，站长吴敬中下令彻查站内所有人员。", "trigger_time": "1945-10-10T08:00:00"},
    {"id": "cuiping_cover_blown", "description": "翠平的游击队出身被王占金认出，余则成必须立刻处理这个致命的安全漏洞。", "trigger_time": "1945-10-25T16:00:00"},
    {"id": "dai_li_inspection", "description": "戴笠亲临天津视察，全站高度紧张，余则成既要表现忠诚又要保护秘密。", "trigger_time": "1945-11-01T09:00:00"},
]

# NPC relationships
data["npc_relationships"] = [
    {"from_id": "npc_mei_jie", "to_id": "npc_ma_tai_tai", "type": "social", "description": "太太圈朋友，梅姐以站长夫人身份居高，马太太跟随攀附"},
    {"from_id": "npc_hong_mishu", "to_id": "npc_ma_tai_tai", "type": "secret", "description": "秘密情人关系，二人长期私通"},
    {"from_id": "npc_dai_li", "to_id": "npc_sheng_xiang", "type": "subordinate", "description": "上下级关系，戴笠并不知盛乡的真实身份"},
    {"from_id": "npc_mu_wanqiu", "to_id": "npc_long_er", "type": "negative", "description": "穆晚秋厌恶龙二这种黑道人物"},
    {"from_id": "npc_wang_zhanjin", "to_id": "npc_long_er", "type": "fear", "description": "王占金惧怕龙二的势力，偶尔帮龙二跑腿换取庇护"},
    {"from_id": "npc_mei_jie", "to_id": "npc_mu_wanqiu", "type": "patronizing", "description": "梅姐对穆晚秋有长辈式的关照，但带有功利目的"},
    {"from_id": "npc_sheng_xiang", "to_id": "npc_hong_mishu", "type": "cautious", "description": "盛乡对洪秘书保持距离，怕后者多嘴暴露自己的秘密"},
    {"from_id": "npc_dai_li", "to_id": "npc_mei_jie", "type": "political", "description": "戴笠对吴敬中夫妇的敛财行为有所耳闻但暂时容忍"},
]

# Settings
data["settings"]["vector_memory_enabled"] = True
data["settings"]["ai_tools_enabled"] = True
data["settings"]["skill_check_map"] = {
    "伪装": "disguise", "隐瞒": "disguise", "掩饰": "disguise",
    "镇定": "composure", "冷静": "composure", "忍耐": "composure",
    "分析": "intelligence_analysis", "推理": "intelligence_analysis", "破译": "intelligence_analysis",
    "说服": "social_skill", "拉拢": "social_skill", "交涉": "social_skill", "周旋": "social_skill",
    "搏斗": "fitness", "追击": "fitness", "逃跑": "fitness", "攀爬": "fitness",
}

new_content = json.dumps(data, ensure_ascii=False)
db.execute("UPDATE scripts SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_content, "qianfu"))
db.commit()
print(f"[1/3] qianfu updated, {len(new_content)} chars")


# ===============================================================
# 2. FIFTH REPUBLIC
# ===============================================================
row = db.execute("SELECT content FROM scripts WHERE id = ?", ("fifth_republic_dawn",)).fetchone()
data = json.loads(row[0])

# NPC talkativeness
npc_talk = {
    # Guess reasonable values based on personality
}
# Read all NPC IDs and set talkativeness based on personality keywords
for npc in data["npcs"]:
    p = npc.get("personality", "")
    if any(k in p for k in ["健谈", "话多", "热情", "活泼", "开朗"]):
        npc["talkativeness"] = 75
    elif any(k in p for k in ["寡言", "沉默", "内敛", "冷漠", "阴沉"]):
        npc["talkativeness"] = 25
    elif any(k in p for k in ["谨慎", "保守", "低调"]):
        npc["talkativeness"] = 35
    elif any(k in p for k in ["圆滑", "世故", "善于交际"]):
        npc["talkativeness"] = 65
    else:
        npc["talkativeness"] = 50
    # Ensure default_location exists
    if not npc.get("default_location"):
        # Try to get from first schedule entry
        sched = npc.get("schedule", [])
        if sched and sched[0].get("location"):
            npc["default_location"] = sched[0]["location"]

# Settings
data["settings"]["vector_memory_enabled"] = True
data["settings"]["ai_tools_enabled"] = True

# Skill check map based on PC attributes (Chinese keys)
data["settings"]["skill_check_map"] = {
    "判断": "政治嗅觉", "洞察": "政治嗅觉", "分析局势": "政治嗅觉", "预判": "政治嗅觉",
    "拉拢": "人脉网络", "联络": "人脉网络", "交涉": "人脉网络", "说服": "人脉网络", "游说": "人脉网络",
    "筹款": "资金储备", "贿赂": "资金储备", "收买": "资金储备", "投资": "资金储备",
    "行动": "行动力", "突袭": "行动力", "执行": "行动力", "冲锋": "行动力", "逃跑": "行动力",
    "演讲": "公众声望", "宣传": "公众声望", "号召": "公众声望", "鼓动": "公众声望",
}

new_content = json.dumps(data, ensure_ascii=False)
db.execute("UPDATE scripts SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_content, "fifth_republic_dawn"))
db.commit()
print(f"[2/3] fifth_republic_dawn updated, {len(new_content)} chars")


# ===============================================================
# 3. SOVIET AUGUST COUP
# ===============================================================
row = db.execute("SELECT content FROM scripts WHERE id = ?", ("soviet_august_coup_1991",)).fetchone()
data = json.loads(row[0])

# NPC talkativeness
for npc in data["npcs"]:
    p = npc.get("personality", "")
    if any(k in p for k in ["健谈", "话多", "热情", "活泼", "开朗", "雄辩"]):
        npc["talkativeness"] = 75
    elif any(k in p for k in ["寡言", "沉默", "内敛", "冷漠", "阴沉", "冗酷"]):
        npc["talkativeness"] = 25
    elif any(k in p for k in ["谨慎", "保守", "低调", "武人"]):
        npc["talkativeness"] = 35
    elif any(k in p for k in ["圆滑", "世故", "善于交际", "外交"]):
        npc["talkativeness"] = 65
    else:
        npc["talkativeness"] = 50

# Settings
data["settings"]["vector_memory_enabled"] = True
data["settings"]["ai_tools_enabled"] = True

# Skill check map (Chinese keys)
data["settings"]["skill_check_map"] = {
    "观察": "洞察力", "推理": "洞察力", "分析": "洞察力", "调查": "洞察力", "识破": "洞察力",
    "镇定": "心理压力", "忍耐": "心理压力", "抗压": "心理压力", "冷静": "心理压力",
    "效忠": "忠诚度", "服从": "忠诚度", "坚守": "忠诚度", "信仰": "忠诚度",
    "操作": "技术能力", "破译": "技术能力", "通信": "技术能力", "修理": "技术能力",
    "搏斗": "体能", "追击": "体能", "逃跑": "体能", "攀爬": "体能",
}

new_content = json.dumps(data, ensure_ascii=False)
db.execute("UPDATE scripts SET content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (new_content, "soviet_august_coup_1991"))
db.commit()
print(f"[3/3] soviet_august_coup_1991 updated, {len(new_content)} chars")

db.close()
print("All 3 scripts optimized successfully!")
