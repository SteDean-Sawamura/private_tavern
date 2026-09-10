# -*- coding: utf-8 -*-
import sqlite3, json, sys, os

sys.stdout.reconfigure(encoding="utf-8")
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "tavern.db")
db = sqlite3.connect(db_path)
row = db.execute("SELECT content FROM scripts WHERE id = ?", ("qianfu",)).fetchone()
data = json.loads(row[0])

LQ = "“"  # left curly quote
RQ = "”"  # right curly quote
LSQ = "《"  # left book title mark
RSQ = "》"  # right book title mark

pc = data["player_character"]
pc["bio"] = (
    f"原国民党军统特务，代号{LQ}深海{RQ}。"
    "抗战期间奉命刺杀汪伪汉奸李海丰，"
    "行动成功后被共产党地下组织策反，"
    "从此以军统身份为掩护，"
    "秘密为组织传递情报。外表沉稳寡言，内心深藏信仰。"
)
pc["personality"] = (
    "沉稳冷静，心思缜密，善于察言观色。"
    "表面上是恪尽职守的国民党特务，"
    "实则对共产主义理想抱有坚定信念。"
    "在危险中保持克制，在利益面前不为所动。"
)
pc["long_term_goal"] = (
    "在保密局天津站长期潜伏，传递关键军事情报给组织，"
    "同时保护身边的同志不被暴露，为解放事业贡献力量。"
)
pc["initial_location"] = "loc_tianjin_station"
pc["portrait_desc"] = "三十出头的男子，国字脸，短发，着深色中山装，眉宇间透着精明与克制。"
pc["attributes"] = {
    "disguise": {"value": 75, "min": 0, "max": 100, "display_name": "伪装技巧",
                 "rule": "在军统内部维持身份掩护的能力。"},
    "composure": {"value": 70, "min": 0, "max": 100, "display_name": "心理素质",
                  "rule": "面对审讯、盘查和突发状况时的镇定程度。"},
    "intelligence_analysis": {"value": 65, "min": 0, "max": 100, "display_name": "情报分析",
                              "rule": "对情报价值的判断力和信息拼图的能力。"},
    "social_skill": {"value": 60, "min": 0, "max": 100, "display_name": "人际手腕",
                     "rule": "在办公室政治中周旋、拉拢和化解矛盾的能力。"},
    "fitness": {"value": 55, "min": 0, "max": 100, "display_name": "体能",
                "rule": "追击、搏斗和逃脱时的身体素质。"},
}

print("Player character updated")
