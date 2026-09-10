"""八一九事变剧本综合优化脚本"""
import sqlite3, json, sys, copy

DB_PATH = "data/tavern.db"

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    db = sqlite3.connect(DB_PATH)
    row = db.execute('SELECT content FROM scripts WHERE id="soviet_august_coup_1991"').fetchone()
    data = json.loads(row[0])
    original = json.dumps(data, ensure_ascii=False)

    # ── 1. NPC initial_location ──────────────────────────────
    NPC_LOCATIONS = {
        "dmitry_yazov": "defense_ministry",
        "boris_pugo": "lubyanka_hq",
        "gennady_yanaev": "kremlin",
        "igor_chebrikov": "lubyanka_hq",
        "marina_retko": "arbat_street",
        "mikhail_gorbachev": "foros_villa",
        "vasily_starodubtsev": "kremlin",
        "vladimir_kryuchkov": "lubyanka_hq",
        "boris_yeltsin": "white_house",
        "pavel_grachev": "moscow_military_district",
        "shift_supervisor_petrov": "lubyanka_hq",
        "valery_boldin": "kremlin",
        "yury_plekhanov": "lubyanka_hq",
        "viktor_karpukhin": "moscow_military_district",
        "anatoly_sobchak": "leningrad_city_hall",
        "gavriil_popov_mayor": "kremlin",
        "nursultan_nazarbayev": "novo_ogaryovo",
        "leonid_kravchuk_ukraine": "",  # 乌克兰，不在莫斯科
        "oleg_lobov": "white_house",
        "george_hw_bush": "us_embassy_moscow",
        "alexander_tizyakov": "lubyanka_hq",
    }
    for npc in data["npcs"]:
        nid = npc["id"]
        if nid in NPC_LOCATIONS:
            npc["location"] = NPC_LOCATIONS[nid]
            npc["initial_location"] = NPC_LOCATIONS[nid]
    print(f"[1] NPC initial_location: 已设置 {sum(1 for v in NPC_LOCATIONS.values() if v)} 个")

    # ── 2. 组织成员 ─────────────────────────────────────────
    ORG_MEMBERS = {
        "org_gkchp": [
            {"npc_id": "vladimir_kryuchkov", "role": "核心策划者"},
            {"npc_id": "dmitry_yazov", "role": "军事支柱"},
            {"npc_id": "boris_pugo", "role": "内务执行者"},
            {"npc_id": "gennady_yanaev", "role": "名义领导人"},
            {"npc_id": "vasily_starodubtsev", "role": "农业代表"},
            {"npc_id": "alexander_tizyakov", "role": "工业代表"},
            {"npc_id": "valery_boldin", "role": "内应"},
        ],
        "org_kgb": [
            {"npc_id": "vladimir_kryuchkov", "role": "主席"},
            {"npc_id": "igor_chebrikov", "role": "第12局监听主管"},
            {"npc_id": "shift_supervisor_petrov", "role": "监听中心值班长"},
            {"npc_id": "yury_plekhanov", "role": "第九局局长"},
            {"npc_id": "viktor_karpukhin", "role": "阿尔法小组指挥官"},
        ],
        "org_defense_ministry": [
            {"npc_id": "dmitry_yazov", "role": "国防部长"},
            {"npc_id": "pavel_grachev", "role": "空降兵司令"},
        ],
        "org_gorbachev_team": [
            {"npc_id": "mikhail_gorbachev", "role": "总统"},
        ],
        "org_yeltsin_camp": [
            {"npc_id": "boris_yeltsin", "role": "俄罗斯联邦总统"},
            {"npc_id": "oleg_lobov", "role": "第一副总理"},
            {"npc_id": "gavriil_popov_mayor", "role": "莫斯科市长"},
        ],
        "org_mvd": [
            {"npc_id": "boris_pugo", "role": "内务部长"},
        ],
        "org_vdv": [
            {"npc_id": "pavel_grachev", "role": "空降兵司令"},
        ],
        "org_presidential_administration": [
            {"npc_id": "valery_boldin", "role": "办公厅主任"},
        ],
        "org_kgb_ninth_directorate": [
            {"npc_id": "yury_plekhanov", "role": "局长"},
        ],
        "org_kgb_alfa": [
            {"npc_id": "viktor_karpukhin", "role": "指挥官"},
        ],
        "org_leningrad_city_government": [
            {"npc_id": "anatoly_sobchak", "role": "市长"},
        ],
        "org_moscow_city_government": [
            {"npc_id": "gavriil_popov_mayor", "role": "市长"},
        ],
        "org_kazakhstan_government": [
            {"npc_id": "nursultan_nazarbayev", "role": "总统"},
        ],
        "org_ukraine_government": [
            {"npc_id": "leonid_kravchuk_ukraine", "role": "最高苏维埃主席"},
        ],
        "org_russian_shadow_cabinet": [
            {"npc_id": "oleg_lobov", "role": "副总理"},
        ],
        "org_us_government": [
            {"npc_id": "george_hw_bush", "role": "总统"},
        ],
        "org_soviet_industrial_union": [
            {"npc_id": "alexander_tizyakov", "role": "会长"},
        ],
        "org_soviet_farmers_union": [
            {"npc_id": "vasily_starodubtsev", "role": "主席"},
        ],
    }
    org_by_id = {o["id"]: o for o in data["organizations"]}
    count_org = 0
    for oid, members in ORG_MEMBERS.items():
        if oid in org_by_id:
            org_by_id[oid]["members"] = members
            count_org += 1
    print(f"[2] 组织成员: 已填充 {count_org} 个组织")

    # ── 3. Lorebook 关键词 ───────────────────────────────────
    LOREBOOK_KEYWORDS = {
        "lore_lubyanka_legend": ["卢比扬卡", "契卡", "地下室", "行刑室", "焚毁档案"],
        "lore_foros_isolation": ["福罗斯", "别墅", "通讯切断", "软禁", "孤岛"],
        "lore_safehouse_arizona": ["亚利桑那", "安全屋", "秘密会议", "备用指挥部"],
        "lore_gkchp_decision": ["罐头", "政变决策", "紧急状态委员会", "封锁红场"],
        "lore_kgb_surveillance": ["回声-91", "监听", "分线器", "第12局", "技术局"],
        "lore_foros_signal_cut": ["信号旗", "衰减器", "海底光缆", "闪电-M", "通讯中断"],
        "lore_chebrikov_dilemma": ["切布里科夫", "胶卷", "备份", "父亲", "平反"],
        "lore_starodubtsev_feud": ["斯塔罗杜布采夫", "隔离小组", "逮捕名单", "农民联盟"],
        "lore_marina_contact": ["雷特科", "死信箱", "胶卷", "线人", "记者"],
        "kg_npc_secret_chebrikov_microfilm": ["软盘", "通风井", "回声-91", "未删节记录"],
        "kg_npc_secret_marina_dead_drop": ["路灯杆", "钥匙", "普希金博物馆", "储物柜", "铅笔"],
        "lore_mvd_internal_split": ["内务部", "格罗莫夫", "裂痕", "倒戈", "武器库"],
        "lore_vdv_grachev_dilemma": ["格拉乔夫", "空降兵", "集结", "倒戈", "开火"],
        "lore_sevastopol_comm_takeover": ["塞瓦斯托波尔", "通讯中心", "衰减器", "信号旗"],
        "kg_npc_vladimir_kryuchkov": ["克留奇科夫", "顺序-90", "三百一十七人", "逮捕名单"],
        "kg_npc_boris_yeltsin": ["叶利钦", "匿名电话", "军队在移动", "警卫加强"],
        "kg_secret_yeltsin_arrest_plan": ["阿尔法", "逮捕叶利钦", "突袭", "联名信"],
        "kg_npc_gromov_dilemma": ["格罗莫夫", "实弹配发", "行刑队", "消极怠工"],
        "lore_army_appeal_context": ["呼吁书", "非常措施", "连队点名", "军队党组织"],
        "lore_yakovlev_warning_inside": ["雅科夫列夫", "退党声明", "政变", "消息报"],
        "lore_ostankino_takeover": ["奥斯坦金诺", "电视中心", "天鹅湖", "播音员"],
        "kg_npc_valery_boldin": ["博尔金", "背叛", "保险柜", "1937", "复仇"],
        "kg_npc_yury_plekhanov": ["普列汉诺夫", "巴格拉季昂-2", "第九局", "福罗斯隔绝"],
    }
    lb_by_id = {lb["id"]: lb for lb in data["lorebook"]}
    count_lb = 0
    for lid, kws in LOREBOOK_KEYWORDS.items():
        if lid in lb_by_id:
            lb_by_id[lid]["keywords"] = kws
            count_lb += 1
    print(f"[3] Lorebook 关键词: 已设置 {count_lb} 条")

    # ── 4. NPC voice_hint ────────────────────────────────────
    NPC_VOICE_HINTS = {
        "dmitry_yazov": "军人式短句，命令口吻，偶尔用战场比喻，称下属为'同志'",
        "boris_pugo": "冷酷低沉，从不废话，语气带威胁性，常用'必须''立刻'",
        "gennady_yanaev": "犹豫吞吐，常用'可能''也许''我想'，紧张时口吃，酒后话多",
        "igor_chebrikov": "专业克制，用代号和术语，极少流露情感，语速均匀如节拍器",
        "marina_retko": "急促热情，连珠炮式追问，带新闻记者的职业性急切",
        "mikhail_gorbachev": "知识分子长句，引经据典，带南俄口音，喜用'同志们，我们必须理解……'",
        "vasily_starodubtsev": "粗俗直白，短句断句，常骂骂咧咧，夹杂农村俚语",
        "vladimir_kryuchkov": "低沉平缓，措辞如手术刀精确，从不提高音量，用沉默施压",
        "boris_yeltsin": "洪亮有力，短句，善煽动，喜用反问句和排比，偶尔拍桌子",
        "pavel_grachev": "沉稳简练，军事术语，提到阿富汗时沉默，用'兄弟们'称呼士兵",
        "shift_supervisor_petrov": "刻板照章，不多说一个字，紧张时语速加快，常说'按规定'",
        "valery_boldin": "恭顺温和的表面，措辞滴水不漏，从不暴露真实想法",
        "yury_plekhanov": "机械冷硬，用'命令就是命令'类套话，无个人情感色彩",
        "viktor_karpukhin": "极少说话，说话时字字千钧，沉默比言语更多",
        "anatoly_sobchak": "激昂演说腔，修辞华丽，逻辑层层推进，声音穿透力强",
        "gavriil_popov_mayor": "犀利幽默，常用反讽，语速快，冷笑话化解紧张",
        "nursultan_nazarbayev": "温和外交辞令，从不把话说死，每句都留有余地",
        "leonid_kravchuk_ukraine": "法律用语包裹真实意图，话中有话，语速缓慢慎重",
        "oleg_lobov": "极简，只说'是''明白''执行'，偶尔长叹一声",
        "george_hw_bush": "外交辞令温和但暗藏锋芒，常用'我们深表关切'式委婉表达",
        "alexander_tizyakov": "工厂式直白，只讲数字和效率，厌恶空谈，常敲桌面强调",
    }
    npc_by_id = {n["id"]: n for n in data["npcs"]}
    for nid, vh in NPC_VOICE_HINTS.items():
        if nid in npc_by_id:
            npc_by_id[nid]["voice_hint"] = vh
    print(f"[4] NPC voice_hint: 已设置 {len(NPC_VOICE_HINTS)} 个")

    # ── 5. NPC secrets ───────────────────────────────────────
    NPC_SECRETS = {
        "igor_chebrikov": [
            "在卢比扬卡地下室通风井砖缝中藏有一张软盘，存储着'回声-91'全部未删节监听记录",
            "妻子乡间别墅的旧饼干盒里藏有微型胶卷备份，记录了克里姆林宫高层全部通话",
            "父亲1937年被清洗的档案仍封存于卢比扬卡，这是他执行任务时唯一的心理裂隙",
        ],
        "vladimir_kryuchkov": [
            "保险柜中锁着代号'顺序-90'的名单，列出政变成功后必须逮捕的317人",
            "为逮捕叶利钦准备了A/B双重方案，但未向亚纳耶夫透露完整内容",
            "深信戈尔巴乔夫正有意瓦解苏联，这种生存恐惧驱使他走向极端",
        ],
        "marina_retko": [
            "通过尼娜的旧关系与美国广播公司特约人员保持单线联系",
            "使用基于《战争与和平》书页的数字码传递部队车号情报",
            "阿尔巴特大街某路灯杆底部有死信箱钥匙，对应普希金博物馆储物柜",
        ],
        "valery_boldin": [
            "保险柜密码是1937，父亲在清洗中被捕的年份",
            "1988年被戈尔巴乔夫公开否决后暗中记录总统的一切弱点和疏失",
            "加入政变的真实动机是个人复仇而非意识形态",
        ],
        "yury_plekhanov": [
            "主动请缨执行福罗斯隔绝任务，将其命名为'巴格拉季昂-2'",
            "每晚对着亡妻照片独饮伏特加，内心远非表面那般坚不可摧",
        ],
        "pavel_grachev": [
            "已私下通过中间人向叶利钦安全主管传递了模糊警告",
            "对副手坦言：一旦对白宫开火，空降兵将永世背负恶名",
        ],
        "viktor_karpukhin": [
            "收到士兵联名信：'我们不是为向本国总统开枪而受训的'",
            "已决定拒绝执行逮捕叶利钦的命令，但克留奇科夫对此毫不知情",
        ],
        "boris_yeltsin": [
            "8月15日晚接到匿名电话警告'军队在移动'，但未与任何人分享",
            "1988年与格拉乔夫在图拉空降师彻夜长谈，建立了个人交情",
        ],
        "shift_supervisor_petrov": [
            "注意到近日深夜有不明高层频繁进出监听中心，但不敢询问",
            "用伏特加掩饰对异常命令的焦虑，开始怀疑自己正在参与某件大事",
        ],
    }
    for nid, secrets in NPC_SECRETS.items():
        if nid in npc_by_id:
            npc_by_id[nid]["secrets"] = secrets
    print(f"[5] NPC secrets: 已设置 {len(NPC_SECRETS)} 个NPC")

    # ── 6. random_items 补充 state_changes ───────────────────
    ri_by_id = {ri["id"]: ri for ri in data["random_items"]}
    RANDOM_ITEM_FIXES = {
        "yakovlev_warning_yeltsin_alert": {
            0: [{"target": "world.yeltsin_awareness", "op": "add", "value": 2}],
            1: [{"target": "world.yeltsin_awareness", "op": "add", "value": 5},
                {"target": "world.public_unease", "op": "add", "value": 3}],
            2: [{"target": "world.yeltsin_awareness", "op": "add", "value": 10},
                {"target": "world.public_unease", "op": "add", "value": 5},
                {"target": "world.coup_preparedness", "op": "add", "value": 3}],
        },
        "army_party_appeal_troop_morale": {
            0: [{"target": "world.vdv_loyalty", "op": "add", "value": 10},
                {"target": "world.coup_preparedness", "op": "add", "value": 3}],
            1: [{"target": "world.vdv_loyalty", "op": "add", "value": -5},
                {"target": "world.public_unease", "op": "add", "value": 3}],
            2: [{"target": "world.vdv_loyalty", "op": "add", "value": -15},
                {"target": "world.yeltsin_awareness", "op": "add", "value": 5}],
        },
        "pavlov_dacha_decision": {
            0: [{"target": "world.coup_preparedness", "op": "add", "value": 5}],
            1: [{"target": "world.coup_preparedness", "op": "add", "value": 3},
                {"target": "player.心理压力", "op": "add", "value": 3}],
            2: [{"target": "world.coup_preparedness", "op": "add", "value": 2},
                {"target": "world.yeltsin_awareness", "op": "add", "value": 3}],
        },
        "foros_guard_loyalty_decide": {
            0: [{"target": "world.gorbachev_isolation", "op": "add", "value": 10}],
            1: [{"target": "world.gorbachev_isolation", "op": "add", "value": 5}],
            2: [{"target": "world.gorbachev_isolation", "op": "add", "value": -5},
                {"target": "world.yeltsin_awareness", "op": "add", "value": 3}],
        },
        "alpha_team_conscience": {
            0: [{"target": "world.vdv_loyalty", "op": "add", "value": 10}],
            1: [{"target": "world.vdv_loyalty", "op": "add", "value": -20},
                {"target": "world.yeltsin_awareness", "op": "add", "value": 5}],
            2: [{"target": "world.vdv_loyalty", "op": "add", "value": -15},
                {"target": "world.yeltsin_awareness", "op": "add", "value": 10}],
        },
    }
    count_ri = 0
    for rid, range_fixes in RANDOM_ITEM_FIXES.items():
        if rid in ri_by_id:
            ranges = ri_by_id[rid].get("ranges", [])
            for idx, sc in range_fixes.items():
                if idx < len(ranges) and not ranges[idx].get("state_changes"):
                    ranges[idx]["state_changes"] = sc
            count_ri += 1
    print(f"[6] random_items state_changes: 已补充 {count_ri} 个")

    # ── 7. one_time_events 补充 state_changes ────────────────
    ot_by_id = {e["id"]: e for e in data["one_time_events"]}
    ONE_TIME_SC = {
        "foros_emissary_departure": [
            {"target": "world.gorbachev_isolation", "op": "add", "value": 15},
            {"target": "world.coup_preparedness", "op": "add", "value": 5},
        ],
        "foros_comms_total_cut": [
            {"target": "world.gorbachev_isolation", "op": "set", "value": 100},
        ],
        "tanks_roll_into_moscow": [
            {"target": "world.public_unease", "op": "add", "value": 30},
            {"target": "world.yeltsin_awareness", "op": "add", "value": 20},
        ],
        "yeltsin_white_house_speech": [
            {"target": "world.yeltsin_awareness", "op": "set", "value": 100},
            {"target": "world.public_unease", "op": "add", "value": 20},
            {"target": "world.vdv_loyalty", "op": "add", "value": -15},
        ],
        "vdv_grachev_refusal": [
            {"target": "world.vdv_loyalty", "op": "set", "value": 15},
        ],
        "gromov_countermove": [
            {"target": "world.vdv_loyalty", "op": "add", "value": -10},
        ],
        "army_party_urgent_appeal_0816": [
            {"target": "world.coup_preparedness", "op": "add", "value": 5},
            {"target": "world.vdv_loyalty", "op": "add", "value": 5},
        ],
        "yakovlev_izvestia_warning_0816": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 8},
            {"target": "world.public_unease", "op": "add", "value": 5},
        ],
        "novo_ogaryovo_secret_agreement": [
            {"target": "world.coup_preparedness", "op": "add", "value": 10},
        ],
        "moskovskie_novosti_expose": [
            {"target": "world.coup_preparedness", "op": "add", "value": 8},
            {"target": "world.public_unease", "op": "add", "value": 3},
        ],
        "pavlov_dacha_party_interrupted": [
            {"target": "world.coup_preparedness", "op": "add", "value": 5},
        ],
        "emergency_committee_formation": [
            {"target": "world.coup_preparedness", "op": "set", "value": 100},
        ],
        "foros_ultimatum_confrontation": [
            {"target": "world.gorbachev_isolation", "op": "add", "value": 20},
        ],
        "tv_radio_gkchp_announcement": [
            {"target": "world.public_unease", "op": "add", "value": 25},
            {"target": "world.yeltsin_awareness", "op": "add", "value": 15},
        ],
        "gkchp_appeal_to_people": [
            {"target": "world.public_unease", "op": "add", "value": 10},
        ],
        "yeltsin_unimpeded_to_white_house": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 10},
        ],
        "yeltsin_decree_59": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 10},
        ],
        "alpha_refusal_to_arrest": [
            {"target": "world.vdv_loyalty", "op": "add", "value": -25},
        ],
        "yanaev_press_conference": [
            {"target": "world.public_unease", "op": "add", "value": 15},
            {"target": "world.yeltsin_awareness", "op": "add", "value": 5},
        ],
        "pavlov_hypertension_collapse": [
            {"target": "world.coup_preparedness", "op": "add", "value": -5},
        ],
        "bush_condemnation_statement": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 5},
        ],
        "kurchatov_internet_breakthrough": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 10},
            {"target": "world.public_unease", "op": "add", "value": 5},
        ],
        "yeltsin_letter_to_lukyanov": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 5},
        ],
        "nazarbayev_constitutional_stance": [
            {"target": "world.vdv_loyalty", "op": "add", "value": -5},
        ],
        "sobchak_tv_denunciation": [
            {"target": "world.yeltsin_awareness", "op": "add", "value": 8},
            {"target": "world.vdv_loyalty", "op": "add", "value": -5},
        ],
        "moscow_curfew_declared": [
            {"target": "world.public_unease", "op": "add", "value": 15},
        ],
        "whitehouse_assault_attempt": [
            {"target": "world.public_unease", "op": "add", "value": 25},
            {"target": "world.vdv_loyalty", "op": "add", "value": -20},
        ],
        "tank_battalion_defection": [
            {"target": "world.vdv_loyalty", "op": "add", "value": -30},
            {"target": "world.yeltsin_awareness", "op": "add", "value": 10},
        ],
        "defense_ministry_withdrawal_order": [
            {"target": "world.coup_preparedness", "op": "add", "value": -30},
        ],
        "gorbachev_return_and_arrests": [
            {"target": "world.gorbachev_isolation", "op": "set", "value": 0},
            {"target": "world.coup_preparedness", "op": "set", "value": 0},
        ],
        "pugo_suicide": [
            {"target": "world.public_unease", "op": "add", "value": 10},
        ],
        "cpsu_suspension_and_gorbachev_resignation": [
            {"target": "world.coup_preparedness", "op": "set", "value": 0},
        ],
    }
    count_ot = 0
    for eid, sc in ONE_TIME_SC.items():
        if eid in ot_by_id:
            if not ot_by_id[eid].get("state_changes"):
                ot_by_id[eid]["state_changes"] = sc
                count_ot += 1
    print(f"[7] one_time_events state_changes: 已补充 {count_ot} 个")

    # ── 8. cyclic_events 补充 start_time ─────────────────────
    CYCLIC_START_TIMES = {
        "morning_monitoring_shift": "1991-08-16T06:00",
        "daily_encrypted_briefing": "1991-08-16T08:00",
        "sevastopol_signal_attentuation_check": "1991-08-16T06:00",
        "arbat_rumor_mill": "1991-08-16T09:00",
        "troop_convoy_movement": "1991-08-16T22:00",
        "kremlin_access_anomaly_report": "1991-08-16T08:00",
        "marina_dead_drop_check": "1991-08-16T10:00",
        "yanaev_anxiety_episode": "1991-08-17T20:00",
        "daily_army_party_appeal": "1991-08-16T10:00",
        "daily_opposition_defection_news": "1991-08-16T11:00",
    }
    ce_by_id = {e["id"]: e for e in data["cyclic_events"]}
    for cid, st in CYCLIC_START_TIMES.items():
        if cid in ce_by_id:
            ce_by_id[cid]["start_time"] = st
    print(f"[8] cyclic_events start_time: 已设置 {len(CYCLIC_START_TIMES)} 个")

    # ── 9. persistent_states 结构补全 ────────────────────────
    PS_FIXES = {
        "communication_monitoring": {
            "type": "boolean", "default": True,
            "rule": "克格勃已启动克里姆林宫通讯旁路监听，玩家在卢比扬卡可接触到监听内容",
        },
        "troop_movement_stealth": {
            "type": "boolean", "default": True,
            "rule": "部队以演习名义向莫斯科集结，玩家可能通过监听或情报获知端倪",
        },
        "special_period_protocol": {
            "type": "boolean", "default": True,
            "rule": "莫斯科军区启用加密通讯规程，非必要通信被限制",
        },
        "foros_guard_standby": {
            "type": "boolean", "default": True,
            "rule": "福罗斯别墅通讯切断计划就绪，警卫即将被动孤立",
        },
        "sevastopol_comm_takeover": {
            "type": "boolean", "default": True,
            "rule": "信号旗小组已控制塞瓦斯托波尔通讯中心，正渐进切断福罗斯通讯",
        },
    }
    ps_by_id = {ps["id"]: ps for ps in data["persistent_states"]}
    for pid, fixes in PS_FIXES.items():
        if pid in ps_by_id:
            ps_by_id[pid].update(fixes)
    print(f"[9] persistent_states 结构: 已补全 {len(PS_FIXES)} 个")

    # ── 10. 开局选项 state_changes 补充 ──────────────────────
    choices = data.get("opening", {}).get("choices", [])
    CHOICE_SC = {
        "open_0": [
            {"target": "player.忠诚度", "op": "add", "value": 5},
        ],
        "open_1": [
            {"target": "player.洞察力", "op": "add", "value": 5},
            {"target": "player.忠诚度", "op": "add", "value": -5},
        ],
        "open_2": [
            {"target": "player.洞察力", "op": "add", "value": 3},
            {"target": "player.心理压力", "op": "add", "value": 5},
        ],
    }
    for c in choices:
        cid = c.get("id")
        if cid in CHOICE_SC and "result" in c:
            if not c["result"].get("state_changes"):
                c["result"]["state_changes"] = CHOICE_SC[cid]
    print(f"[10] 开局选项 state_changes: 已补充")

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
