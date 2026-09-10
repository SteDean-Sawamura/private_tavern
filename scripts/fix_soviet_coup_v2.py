"""八一九事变剧本第二轮优化"""
import sqlite3, json, sys

DB_PATH = "data/tavern.db"

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    db = sqlite3.connect(DB_PATH)
    row = db.execute('SELECT content FROM scripts WHERE id="soviet_august_coup_1991"').fetchone()
    data = json.loads(row[0])
    original = json.dumps(data, ensure_ascii=False)

    # ── 1. 开局选项 open_2 补全 success/failure 描述 ────────
    for c in data["opening"]["choices"]:
        if c["id"] == "open_2":
            c["result"]["success"] = {
                "description": "你避开走廊里的两个监控摄像头，在总档案室B区找到一份标注'特别技术行动/回声-91'的绿色封皮文件夹。里面是一份由克留奇科夫亲笔签署的旁路监听授权——对象赫然是克里姆林宫全部政府专线。你的手指冰凉，这不是常规反间谍行动，这是对自己人的监视。你迅速记下关键编号后将文件放回原处。",
                "state_changes": [
                    {"target": "player.洞察力", "op": "add", "value": 10},
                    {"target": "player.心理压力", "op": "add", "value": 8},
                ],
            }
            c["result"]["failure"] = {
                "description": "你在三楼走廊拐角处差点撞上一名内务部军官。他冷冷打量你的证件，询问你离开监听中心的理由。你结结巴巴说去洗手间，他似乎没有完全相信，但也没有深究——只是在本子上记下了你的名字和证件号。你匆匆返回工位，后背湿透。",
                "state_changes": [
                    {"target": "player.心理压力", "op": "add", "value": 12},
                ],
            }
    print("[1] 开局选项 open_2: 已补全 success/failure 描述和 state_changes")

    # ── 2. 修复引用不存在NPC的cyclic event条件 ─────────────
    # kremlin_access_anomaly_report 引用 pavel_korolev（是 player preset, 不是NPC）
    # marina_dead_drop_check 引用 marina_retko（是NPC，但条件语法可能有问题）
    ce_by_id = {e["id"]: e for e in data["cyclic_events"]}

    # kremlin_access_anomaly_report: 改为玩家在克里姆林宫时触发
    if "kremlin_access_anomaly_report" in ce_by_id:
        ce_by_id["kremlin_access_anomaly_report"]["condition"] = "player.location == 'kremlin'"

    # marina_dead_drop_check: 改为玩家已认识marina时触发
    if "marina_dead_drop_check" in ce_by_id:
        ce_by_id["marina_dead_drop_check"]["condition"] = "npcs.marina_retko.met == true"

    print("[2] cyclic events: 修复 2 个无效条件引用")

    # ── 3. 孤立地点补充 connections ──────────────────────────
    loc_by_id = {loc["id"]: loc for loc in data["locations"]}

    # ostankino_tv_center 无连接
    if "ostankino_tv_center" in loc_by_id:
        loc_by_id["ostankino_tv_center"]["connections"] = [
            "kremlin", "tverskaya_street",
        ]

    # 补充一些合理的双向连接
    CONNECTION_ADDITIONS = {
        "lubyanka_hq": ["arbat_street", "red_square", "tverskaya_street"],
        "moscow_military_district": ["kremlin"],
        "moscow_hippodrome": ["tverskaya_street"],
        "kurchatov_institute": ["moscow_military_district"],
        "red_square": ["cpsu_central_building"],
    }
    for lid, new_conns in CONNECTION_ADDITIONS.items():
        if lid in loc_by_id:
            existing = set(loc_by_id[lid].get("connections", []))
            for nc in new_conns:
                if nc not in existing:
                    loc_by_id[lid].setdefault("connections", []).append(nc)
            # 确保双向
            for nc in new_conns:
                if nc in loc_by_id:
                    nc_conns = set(loc_by_id[nc].get("connections", []))
                    if lid not in nc_conns:
                        loc_by_id[nc].setdefault("connections", []).append(lid)

    print("[3] locations: 补充连接，修复孤立地点")

    # ── 4. 关键地点补充 rooms ────────────────────────────────
    LOCATION_ROOMS = {
        "lubyanka_hq": [
            {"id": "monitoring_center", "name": "监听中心", "description": "地下二层无窗隔间，荧光灯常明，监听机柜排列成行，耳机线缠绕如蛛网"},
            {"id": "archive_room", "name": "总档案室", "description": "三楼B区，铁皮档案柜从地板排到天花板，空气中弥漫着旧纸和墨水的气味"},
            {"id": "kryuchkov_office", "name": "克留奇科夫办公室", "description": "五楼，厚重橡木门后的宽大办公室，窗帘常年拉上，保险柜嵌入墙体"},
            {"id": "basement_furnace", "name": "地下焚烧室", "description": "地下四层，斯大林时期行刑室改建，现为销毁文件的焚化炉房"},
            {"id": "comm_relay_room", "name": "通讯中继室", "description": "二楼无窗隔间，数控分线器安装于此，可同时监听克里姆林宫120条线路"},
        ],
        "kremlin": [
            {"id": "yanaev_office", "name": "副总统办公室", "description": "镀金吊灯下的沉重办公桌，亚纳耶夫的签名笔和酒瓶并排摆放"},
            {"id": "boldin_office", "name": "总统办公厅", "description": "博尔金的办公室，保险柜密码1937，戈尔巴乔夫的日程表在抽屉中"},
            {"id": "borovitsky_gate", "name": "博罗维茨基门", "description": "克里姆林宫主入口，石砌岗亭，警卫核查每一辆进入的车辆"},
            {"id": "comm_center", "name": "克里姆林宫通讯中心", "description": "地下层加密通讯枢纽，连接各部委的红色专线在此汇聚"},
        ],
        "foros_villa": [
            {"id": "presidential_study", "name": "总统书房", "description": "二楼面海的宽大书房，戈尔巴乔夫在此批阅文件，窗外可见地中海松林"},
            {"id": "comm_hub", "name": "地下通讯枢纽", "description": "高频无线电、海底光缆终端和闪电-M卫星电话的汇集点"},
            {"id": "helipad", "name": "直升机坪", "description": "别墅后山的混凝土停机坪，政变期间被克格勃封锁"},
            {"id": "guard_post_east", "name": "东侧警卫哨", "description": "面朝松林的固定哨位，可俯瞰进出别墅的唯一公路"},
        ],
        "white_house": [
            {"id": "yeltsin_office", "name": "叶利钦办公室", "description": "高层办公室，莫斯科河景一览无余，电话和传真机不停响铃"},
            {"id": "press_room", "name": "新闻发布厅", "description": "底层大厅，传真机和打字机此起彼伏，记者和助手穿梭往来"},
            {"id": "front_steps", "name": "白宫台阶", "description": "面向莫斯科河的宽阔台阶，叶利钦在此发表了著名的坦克演说"},
            {"id": "basement_shelter", "name": "地下掩体", "description": "防核掩体改作临时指挥所，影子内阁在此筹备最坏方案"},
        ],
        "arizona_safehouse": [
            {"id": "war_room", "name": "作战室", "description": "地下60米的混凝土堡垒，会议桌上摊着莫斯科市区地图和部队调动箭头"},
            {"id": "comm_room", "name": "加密通讯室", "description": "独立柴油发电机供电，多条加密线路通往国防部和各军区"},
        ],
        "sevastopol_comm_center": [
            {"id": "relay_terminal", "name": "中继终端室", "description": "海底光缆和高频无线电中继汇聚点，值班士官被信号旗小组替换"},
            {"id": "signal_control", "name": "信号控制台", "description": "安装有遥控衰减器的主控台，可远程调节福罗斯别墅的信号强度"},
        ],
        "defense_ministry": [
            {"id": "yazov_office", "name": "元帅办公室", "description": "亚佐夫的办公室，作战地图上标满师级调动箭头，灯火彻夜不熄"},
            {"id": "ops_center", "name": "作战指挥中心", "description": "地下层，大屏幕显示各军区部队态势，加密通讯终端密集排列"},
        ],
    }
    for lid, rooms in LOCATION_ROOMS.items():
        if lid in loc_by_id:
            loc_by_id[lid]["rooms"] = rooms
    print(f"[4] locations rooms: 已为 {len(LOCATION_ROOMS)} 个关键地点添加房间")

    # ── 5. Player presets 补全属性和物品 ─────────────────────
    PRESET_FIXES = {
        "tank_battalion_officer": {
            "attributes": {
                "洞察力": {"value": 55, "min": 0, "max": 100, "rule": "察觉细微异常、解读隐藏信息的能力"},
                "心理压力": {"value": 40, "min": 0, "max": 100, "rule": "过高将导致判断失误或崩溃行为"},
                "忠诚度": {"value": 60, "min": 0, "max": 100, "rule": "对体制和上级的认同程度"},
                "体能": {"value": 75, "min": 0, "max": 100, "rule": "体力与肉搏能力"},
            },
            "initial_inventory": [
                {"item": "军官手枪", "quantity": 1},
                {"item": "军用地图", "quantity": 1},
                {"item": "加密通讯器", "quantity": 1},
            ],
        },
        "foros_presidential_guard": {
            "attributes": {
                "洞察力": {"value": 65, "min": 0, "max": 100, "rule": "察觉细微异常、解读隐藏信息的能力"},
                "心理压力": {"value": 35, "min": 0, "max": 100, "rule": "过高将导致判断失误或崩溃行为"},
                "忠诚度": {"value": 90, "min": 0, "max": 100, "rule": "对总统的忠诚程度"},
                "体能": {"value": 80, "min": 0, "max": 100, "rule": "体力与肉搏能力"},
            },
            "initial_inventory": [
                {"item": "马卡洛夫手枪", "quantity": 1},
                {"item": "警卫证件", "quantity": 1},
                {"item": "对讲机", "quantity": 1},
            ],
        },
        "yeltsin_press_aide": {
            "attributes": {
                "洞察力": {"value": 70, "min": 0, "max": 100, "rule": "察觉细微异常、解读隐藏信息的能力"},
                "心理压力": {"value": 25, "min": 0, "max": 100, "rule": "过高将导致判断失误或崩溃行为"},
                "忠诚度": {"value": 80, "min": 0, "max": 100, "rule": "对改革事业的信念"},
                "技术能力": {"value": 60, "min": 0, "max": 100, "rule": "通讯和文书技能"},
                "体能": {"value": 35, "min": 0, "max": 100, "rule": "体力"},
            },
            "initial_inventory": [
                {"item": "新闻处证件", "quantity": 1},
                {"item": "便携打字机", "quantity": 1},
                {"item": "通讯录", "quantity": 1},
            ],
        },
        "arbat_bookseller": {
            "attributes": {
                "洞察力": {"value": 88, "min": 0, "max": 100, "rule": "老练的观察力和对危险的嗅觉"},
                "心理压力": {"value": 20, "min": 0, "max": 100, "rule": "过高将导致判断失误"},
                "忠诚度": {"value": 15, "min": 0, "max": 100, "rule": "对体制的认同程度，极低"},
                "体能": {"value": 35, "min": 0, "max": 100, "rule": "年迈体弱"},
            },
            "initial_inventory": [
                {"item": "书店钥匙", "quantity": 1},
                {"item": "短波收音机", "quantity": 1},
                {"item": "旧通讯录", "quantity": 1},
            ],
        },
        "kremlin_access_officer": {
            "attributes": {
                "洞察力": {"value": 72, "min": 0, "max": 100, "rule": "门禁警卫的职业观察力"},
                "心理压力": {"value": 45, "min": 0, "max": 100, "rule": "程序崩塌带来的焦虑"},
                "忠诚度": {"value": 55, "min": 0, "max": 100, "rule": "对规章制度的忠诚"},
                "体能": {"value": 70, "min": 0, "max": 100, "rule": "警卫体能"},
            },
            "initial_inventory": [
                {"item": "克格勃证件", "quantity": 1},
                {"item": "值班手册", "quantity": 1},
                {"item": "手枪", "quantity": 1},
            ],
        },
        "signals_trooper_sevastopol": {
            "initial_inventory": [
                {"item": "伪造检修令", "quantity": 1},
                {"item": "信号旗证件", "quantity": 1},
                {"item": "技术工具箱", "quantity": 1},
            ],
        },
        "vdv_company_commander": {
            "initial_inventory": [
                {"item": "军官手枪", "quantity": 1},
                {"item": "加密电台", "quantity": 1},
                {"item": "连队花名册", "quantity": 1},
            ],
        },
        "mvd_deputy_director": {
            "initial_inventory": [
                {"item": "内务部证件", "quantity": 1},
                {"item": "公务用车钥匙", "quantity": 1},
                {"item": "预防性拘留名单", "quantity": 1},
            ],
        },
        "yegor_sokolov_gkchp_advisor": {
            "initial_inventory": [
                {"item": "绝密行动时间表", "quantity": 1},
                {"item": "克格勃高级证件", "quantity": 1},
                {"item": "莫斯科军用地图", "quantity": 1},
            ],
        },
        "olga_konstantinova_yeltsin_aide": {
            "initial_inventory": [
                {"item": "联邦条约草案", "quantity": 1},
                {"item": "法律参考文件", "quantity": 1},
                {"item": "加密电话本", "quantity": 1},
            ],
        },
        "boldin_secretary": {
            "initial_inventory": [
                {"item": "克里姆林宫通行证", "quantity": 1},
                {"item": "速记本", "quantity": 1},
                {"item": "博尔金日程表副本", "quantity": 1},
            ],
        },
    }
    pp_by_id = {pp["id"]: pp for pp in data["player_presets"]}
    for pid, fixes in PRESET_FIXES.items():
        if pid in pp_by_id:
            if "attributes" in fixes:
                pp_by_id[pid]["attributes"] = fixes["attributes"]
            if "initial_inventory" in fixes:
                pp_by_id[pid]["initial_inventory"] = fixes["initial_inventory"]
    print(f"[5] player_presets: 已补全 {len(PRESET_FIXES)} 个预设的属性/物品")

    # ── 6. npc_relationships 补全缺失的 value 字段 ──────────
    # 检查并确保每条关系都有 from/to 和数值字段
    for rel in data.get("npc_relationships", []):
        if "trust" not in rel:
            rel["trust"] = 50
        if "affection" not in rel:
            rel["affection"] = 50
        if "fear" not in rel:
            rel["fear"] = 10
        if "initially_known" not in rel:
            rel["initially_known"] = True
        if "initially_met" not in rel:
            rel["initially_met"] = True
    print(f"[6] npc_relationships: 已检查 {len(data.get('npc_relationships', []))} 条")

    # ── 7. 补充 NPC schedule 夜间时段 ───────────────────────
    # 大多数NPC只有 07-12, 12-18 两个时段，缺少夜间
    NIGHT_SCHEDULES = {
        "dmitry_yazov": {"time_range": "18:00-07:00", "location": "defense_ministry",
                         "activity": "通宵坐镇作战指挥中心，通过加密线路协调各军区的'演习'部署，偶尔在沙发上假寐"},
        "boris_pugo": {"time_range": "18:00-07:00", "location": "lubyanka_hq",
                       "activity": "在内务部长办公室审阅逮捕名单和内卫部队调度方案，深夜饮浓茶提神"},
        "gennady_yanaev": {"time_range": "18:00-07:00", "location": "pavlov_dacha",
                           "activity": "出席帕夫洛夫别墅的秘密会议或酒宴，在酒精中寻找勇气，凌晨才回到克里姆林宫"},
        "igor_chebrikov": {"time_range": "18:00-07:00", "location": "lubyanka_hq",
                           "activity": "独自在监听中心值夜班，趁无人时用微型胶卷备份关键监听记录"},
        "marina_retko": {"time_range": "18:00-07:00", "location": "arbat_street",
                         "activity": "在尼娜书店的后间整理白天收集的情报，用《战争与和平》数字码编写密信"},
        "vladimir_kryuchkov": {"time_range": "18:00-07:00", "location": "arizona_safehouse",
                               "activity": "在安全屋主持核心会议，逐项确认'罐头'方案的执行细节"},
        "boris_yeltsin": {"time_range": "18:00-07:00", "location": "white_house",
                          "activity": "在办公室处理联邦事务，偶尔凝视窗外莫斯科河，对渐浓的不安辗转难眠"},
        "pavel_grachev": {"time_range": "18:00-07:00", "location": "moscow_military_district",
                          "activity": "在指挥中心地图前反复推演，对'集结令'的真实意图越发不安"},
        "valery_boldin": {"time_range": "18:00-07:00", "location": "kremlin",
                          "activity": "深夜独坐办公室，整理窃取的总统文件副本，通过加密电话向克留奇科夫汇报"},
        "shift_supervisor_petrov": {"time_range": "18:00-07:00", "location": "lubyanka_hq",
                                    "activity": "在值班室喝伏特加压惊，回忆白天那些不该听到的通话内容"},
        "yury_plekhanov": {"time_range": "18:00-07:00", "location": "foros_villa",
                           "activity": "率第九局先遣组抵达福罗斯外围，为通讯切断行动做最后踩点"},
        "viktor_karpukhin": {"time_range": "18:00-07:00", "location": "moscow_military_district",
                             "activity": "在阿尔法小组营房审阅士兵联名信，一夜未眠地权衡荣誉与命令"},
    }
    npc_by_id = {n["id"]: n for n in data["npcs"]}
    count_night = 0
    for nid, night in NIGHT_SCHEDULES.items():
        if nid in npc_by_id:
            schedules = npc_by_id[nid].get("schedule", [])
            # 检查是否已有夜间时段
            has_night = any("18:00" in s.get("time_range", "") for s in schedules)
            if not has_night:
                schedules.append(night)
                count_night += 1
    print(f"[7] NPC夜间schedule: 已添加 {count_night} 个")

    # ── 8. 补充缺失的 one_time_event condition ──────────────
    # foros_comms_total_cut 条件不完整（末尾缺值）
    ot_by_id = {e["id"]: e for e in data["one_time_events"]}
    if "foros_comms_total_cut" in ot_by_id:
        old_cond = ot_by_id["foros_comms_total_cut"].get("condition", "")
        if old_cond.endswith("coup_preparedness ") or old_cond.endswith("coup_preparedness"):
            ot_by_id["foros_comms_total_cut"]["condition"] = (
                "world.gorbachev_isolation >= 70 and world.coup_preparedness >= 95"
            )
            print("[8] foros_comms_total_cut: 修复截断的条件表达式")
        else:
            print(f"[8] foros_comms_total_cut: 条件正常 ({old_cond})")

    # ── 9. cyclic_events 补充 state_changes ──────────────────
    CYCLIC_SC = {
        "sevastopol_signal_attentuation_check": [
            {"target": "world.gorbachev_isolation", "op": "add", "value": 5},
        ],
        "arbat_rumor_mill": [
            {"target": "world.public_unease", "op": "add", "value": 3},
        ],
        "troop_convoy_movement": [
            {"target": "world.coup_preparedness", "op": "add", "value": 3},
        ],
        "yanaev_anxiety_episode": [
            {"target": "world.coup_preparedness", "op": "add", "value": -2},
        ],
        "daily_army_party_appeal": [
            {"target": "world.vdv_loyalty", "op": "add", "value": 3},
        ],
        "daily_opposition_defection_news": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 3},
            {"target": "world.public_unease", "op": "add", "value": 2},
        ],
    }
    ce_by_id2 = {e["id"]: e for e in data["cyclic_events"]}
    count_csc = 0
    for cid, sc in CYCLIC_SC.items():
        if cid in ce_by_id2 and not ce_by_id2[cid].get("state_changes"):
            ce_by_id2[cid]["state_changes"] = sc
            count_csc += 1
    print(f"[9] cyclic_events state_changes: 已补充 {count_csc} 个")

    # ── 保存 ─────────────────────────────────────────────────
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
