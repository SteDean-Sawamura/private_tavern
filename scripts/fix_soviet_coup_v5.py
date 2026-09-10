"""八一九事变剧本第五轮优化：随机项 + 变量 + world_properties 完善"""
import sqlite3, json, sys

DB_PATH = "data/tavern.db"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    db = sqlite3.connect(DB_PATH)
    row = db.execute('SELECT content FROM scripts WHERE id="soviet_august_coup_1991"').fetchone()
    data = json.loads(row[0])
    original = json.dumps(data, ensure_ascii=False)

    # ═══════════════════════════════════════════════════════════════════
    # 1. world_properties 补全 min/max/rule
    # ═══════════════════════════════════════════════════════════════════
    WP_BOUNDS = {
        "coup_preparedness": {"min": 0, "max": 100, "rule": "政变准备程度。100=完全就绪，0=彻底瓦解"},
        "gorbachev_isolation": {"min": 0, "max": 100, "rule": "戈尔巴乔夫被孤立程度。100=完全与外界切断联系"},
        "yeltsin_awareness": {"min": 0, "max": 100, "rule": "叶利钦阵营的警觉与动员程度"},
        "public_unease": {"min": 0, "max": 300, "rule": "莫斯科市民不安程度。超过100=大规模恐慌/抗议"},
        "vdv_loyalty": {"min": -100, "max": 100, "rule": "军队对政变的忠诚度。正值=服从命令，负值=倒戈倾向"},
    }
    wp_by_id = {w["id"]: w for w in data["world_properties"]}
    count_wp = 0
    for wid, bounds in WP_BOUNDS.items():
        if wid in wp_by_id:
            for k, v in bounds.items():
                wp_by_id[wid][k] = v
            count_wp += 1
    print(f"[1] world_properties: 补全 {count_wp} 个 min/max/rule")

    # ═══════════════════════════════════════════════════════════════════
    # 2. variables — 跟踪关键决策和隐藏状态
    # ═══════════════════════════════════════════════════════════════════
    variables = data.get("variables", [])
    existing_var_ids = {v.get("id", "") for v in variables}

    NEW_VARS = [
        {
            "id": "intel_gathered",
            "type": "number",
            "default": 0,
            "min": 0,
            "max": 100,
            "label": "情报积累",
            "description": "玩家收集到的政变相关情报总量",
        },
        {
            "id": "cover_integrity",
            "type": "number",
            "default": 100,
            "min": 0,
            "max": 100,
            "label": "身份掩护",
            "description": "玩家伪装身份的完整度。低于30时可能被识破",
        },
        {
            "id": "marina_trust",
            "type": "number",
            "default": 0,
            "min": 0,
            "max": 100,
            "label": "玛丽娜信任度",
            "description": "地下网络联络人玛丽娜对玩家的信任程度",
        },
        {
            "id": "kryuchkov_favor",
            "type": "number",
            "default": 30,
            "min": 0,
            "max": 100,
            "label": "克留奇科夫赏识",
            "description": "克格勃主席克留奇科夫对玩家的赏识程度",
        },
        {
            "id": "evidence_collected",
            "type": "number",
            "default": 0,
            "min": 0,
            "max": 10,
            "label": "实物证据",
            "description": "玩家持有的可证明政变阴谋的实物证据数量（磁带、文件、照片等）",
        },
        {
            "id": "faction_alignment",
            "type": "number",
            "default": 0,
            "min": -100,
            "max": 100,
            "label": "阵营倾向",
            "description": "正值=倾向政变派，负值=倾向改革派。影响NPC对话和事件走向",
        },
        {
            "id": "nights_without_sleep",
            "type": "number",
            "default": 0,
            "min": 0,
            "max": 5,
            "label": "连续失眠夜数",
            "description": "连续未能正常休息的天数。超过3天时洞察力和判断力下降",
        },
        {
            "id": "chebrikov_warned",
            "type": "bool",
            "default": False,
            "label": "切布里科夫已示警",
            "description": "切布里科夫中校是否已向玩家暗示了异常",
        },
        {
            "id": "tape_backup_exists",
            "type": "bool",
            "default": False,
            "label": "备份磁带存在",
            "description": "玩家是否偷偷备份了关键监听录音",
        },
        {
            "id": "dead_drops_active",
            "type": "number",
            "default": 0,
            "min": 0,
            "max": 5,
            "label": "活跃死信箱",
            "description": "玩家建立的尚未暴露的情报交接点数量",
        },
    ]
    count_var = 0
    for v in NEW_VARS:
        if v["id"] not in existing_var_ids:
            variables.append(v)
            count_var += 1
    data["variables"] = variables
    print(f"[2] variables: 新增 {count_var} 个变量")

    # ═══════════════════════════════════════════════════════════════════
    # 3. 新增 random_items
    # ═══════════════════════════════════════════════════════════════════
    existing_ri_ids = {ri["id"] for ri in data.get("random_items", [])}

    NEW_RANDOM_ITEMS = [
        # ── 监听相关 ──
        {
            "id": "intercepted_phone_call",
            "description": "在漫长的监听值班中，耳机里突然传来一段不同寻常的通话——语气、内容或通话方式都偏离了正常模式。掷骰决定这段通话的情报价值。",
            "trigger": "玩家在卢比扬卡执行监听任务时",
            "trigger_type": "conditional",
            "condition": "player.location == 'lubyanka_hq'",
            "duration_turns": 0,
            "cooldown_turns": 2,
            "dice": {"count": 1, "faces": 100, "modifier": 0},
            "ranges": [
                {
                    "min": 1, "max": 30,
                    "label": "日常杂音",
                    "description": "又一段无关紧要的行政通话——后勤供应、车辆调度、食堂菜单确认。你在日志上写下'无异常'，指尖因连续敲击键盘而微微发麻。",
                    "state_changes": [],
                },
                {
                    "min": 31, "max": 65,
                    "label": "含糊暗语",
                    "description": "某位副部长级官员在通话中使用了不属于标准密码本的暗语——'花园里的苹果快熟了'。你无法确定含义，但这种偏离规程的说法本身就值得记录。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                    ],
                },
                {
                    "min": 66, "max": 85,
                    "label": "关键情报",
                    "description": "你截获了一段国防部与莫斯科军区之间的通话，明确提到了'8月19日零时'和'甲类战备'。这不是演习——没有哪个演习会在凌晨启动甲类战备。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 10},
                        {"target": "player.心理压力", "op": "add", "value": 5},
                    ],
                },
                {
                    "min": 86, "max": 100,
                    "label": "绝密通话",
                    "description": "克留奇科夫亲自拨出的一通电话意外接入了你的监听频道——他正在向某人确认'名单上第一页的人选是否全部到位'。你的心跳几乎停止，手指悬在录音键上方颤抖。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 15},
                        {"target": "player.心理压力", "op": "add", "value": 12},
                    ],
                },
            ],
        },
        # ── 街头遭遇 ──
        {
            "id": "street_checkpoint",
            "description": "莫斯科街头出现了临时检查站，内务部士兵在检查行人证件。掷骰决定这次盘查的结果。",
            "trigger": "玩家在莫斯科街区移动时",
            "trigger_type": "conditional",
            "condition": "player.location in ['tverskaya_street', 'arbat_street', 'new_arbat_street', 'red_square'] and world.coup_preparedness >= 80",
            "duration_turns": 0,
            "cooldown_turns": 3,
            "dice": {"count": 1, "faces": 100, "modifier": 0},
            "ranges": [
                {
                    "min": 1, "max": 40,
                    "label": "顺利通过",
                    "description": "士兵扫了一眼你的证件，挥手放行。克格勃的证件在这种时候是最好的通行证——没有人想和卢比扬卡扯上关系。",
                    "state_changes": [],
                },
                {
                    "min": 41, "max": 75,
                    "label": "仔细盘问",
                    "description": "一个过于认真的年轻中尉将你拦下详细询问出行目的。你冷静地编造了一个合理的理由，但他在本子上记下了你的名字和证件号。",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 5},
                    ],
                },
                {
                    "min": 76, "max": 100,
                    "label": "意外发现",
                    "description": "排队等待检查时，你注意到检查站使用的对讲机频率与'回声-91'行动的加密通讯频段只差0.5兆赫——有人在用军用设备监控平民街区。你不动声色地记住了频率参数。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 8},
                        {"target": "player.心理压力", "op": "add", "value": 3},
                    ],
                },
            ],
        },
        # ── 克里姆林宫内部 ──
        {
            "id": "kremlin_corridor_encounter",
            "description": "在克里姆林宫走廊中，你与一位高级官员不期而遇。掷骰决定这次相遇的影响。",
            "trigger": "玩家在克里姆林宫区域时",
            "trigger_type": "conditional",
            "condition": "player.location == 'kremlin'",
            "duration_turns": 0,
            "cooldown_turns": 3,
            "dice": {"count": 1, "faces": 100, "modifier": 0},
            "ranges": [
                {
                    "min": 1, "max": 35,
                    "label": "博尔金的阴影",
                    "description": "博尔金从一间办公室快步走出，怀里抱着一叠标注'绝密'的文件夹。他看到你时明显一愣，随即恢复镇定，用审视的目光打量你的证件。'你不该出现在这层楼。'",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 8},
                    ],
                },
                {
                    "min": 36, "max": 65,
                    "label": "焦虑的秘书",
                    "description": "一位总统办公厅的女秘书在楼梯拐角处差点撞到你。她的眼眶泛红，手里攥着一封拆开的信件。'对不起……他们要把我调走，说是'人事优化'……'她的声音越来越低，随即快步离去。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 3},
                    ],
                },
                {
                    "min": 66, "max": 100,
                    "label": "通讯中心异常",
                    "description": "路过克里姆林宫通讯中心时，你注意到往常只有两人值守的加密终端室此刻挤进了五名信号旗部队的技术军官。他们正在安装某种你从未见过的设备——小型化远程操控装置。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 10},
                        {"target": "player.心理压力", "op": "add", "value": 5},
                    ],
                },
            ],
        },
        # ── 夜间事件 ──
        {
            "id": "sleepless_night_event",
            "description": "又一个无法入眠的深夜。窗外莫斯科的灯火在薄雾中模糊，你的思绪在恐惧、良知和生存本能之间翻腾。掷骰决定这个夜晚带给你什么。",
            "trigger": "夜间（18:00后），玩家心理压力较高时",
            "trigger_type": "conditional",
            "condition": "player.心理压力 >= 40",
            "duration_turns": 0,
            "cooldown_turns": 2,
            "dice": {"count": 1, "faces": 100, "modifier": 0},
            "ranges": [
                {
                    "min": 1, "max": 30,
                    "label": "噩梦缠绕",
                    "description": "你在汗湿的床单上惊醒，梦境中父亲的脸与克留奇科夫的脸不断重叠。窗外远处传来军用卡车的引擎声——不知是梦境的残余还是现实的回响。你再也无法入睡。",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 8},
                        {"target": "player.洞察力", "op": "add", "value": -3},
                    ],
                },
                {
                    "min": 31, "max": 60,
                    "label": "收音机微光",
                    "description": "你在黑暗中拧开一台老式短波收音机，在嘶嘶的杂音中搜寻到自由电台的微弱信号。一个低沉的声音正在分析莫斯科近日的异常军事调动——外面的世界已经开始察觉。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                        {"target": "player.心理压力", "op": "add", "value": 3},
                    ],
                },
                {
                    "min": 61, "max": 85,
                    "label": "深夜来访",
                    "description": "凌晨两点，有人轻叩你的宿舍门。是切布里科夫，他带来一瓶格鲁吉亚白兰地和一个问题：'科洛廖夫，如果有一天你必须在祖国和上级之间做选择，你会怎么做？'",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 8},
                        {"target": "player.心理压力", "op": "add", "value": -5},
                    ],
                },
                {
                    "min": 86, "max": 100,
                    "label": "意外的清醒",
                    "description": "反常地，今夜你的头脑异常清醒。各种碎片信息——不寻常的调令、含糊的暗语、反常的人事调动——在脑海中如拼图般渐渐聚合。你第一次看清了事态的全貌，恐惧反而让位于一种冰冷的确定感。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 12},
                        {"target": "player.心理压力", "op": "add", "value": -8},
                    ],
                },
            ],
        },
        # ── 白宫周边 ──
        {
            "id": "white_house_barricade_event",
            "description": "白宫前的路障区域人声鼎沸，守卫者和围观群众混杂在一起。掷骰决定你在这里目睹什么。",
            "trigger": "玩家在白宫区域时（政变已发动）",
            "trigger_type": "conditional",
            "condition": "player.location == 'white_house' and world.coup_preparedness >= 90",
            "duration_turns": 0,
            "cooldown_turns": 2,
            "dice": {"count": 1, "faces": 100, "modifier": 0},
            "ranges": [
                {
                    "min": 1, "max": 30,
                    "label": "坦克兵的眼泪",
                    "description": "一名年轻的坦克驾驶员从舱盖探出头，接过一个老太太递来的面包。他的嘴唇颤抖着，用袖子擦了一下眼睛。你听到他对通话器说：'长官，我们的炮管里不应该装对着自己人的炮弹。'",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 10},
                        {"target": "player.忠诚度", "op": "add", "value": -8},
                    ],
                },
                {
                    "min": 31, "max": 60,
                    "label": "地下印刷品",
                    "description": "有人塞给你一张油墨未干的传单——叶利钦签署的总统令全文，呼吁所有公民和平抵抗非法政变。传单底部印着一行小字：'复印并传递——这是你的公民义务。'",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                        {"target": "world.yeltsin_awareness", "op": "add", "value": 2},
                    ],
                },
                {
                    "min": 61, "max": 85,
                    "label": "外国记者",
                    "description": "一个背着摄像机的CNN记者拦住你，用蹩脚的俄语问：'你是军人吗？你支持谁？'镜头对准了你。你知道这段画面可能会在几小时后出现在全世界的电视屏幕上。",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 8},
                    ],
                },
                {
                    "min": 86, "max": 100,
                    "label": "罗斯特罗波维奇的大提琴",
                    "description": "从路障后方传来大提琴的旋律——是巴赫的无伴奏组曲。你挤过人群看到一个白发苍苍的老人坐在折叠椅上拉琴，面前竖着一块硬纸板写着：'俄罗斯，我在这里。'有人低声告诉你那是罗斯特罗波维奇。",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": -5},
                        {"target": "player.忠诚度", "op": "add", "value": -5},
                        {"target": "world.public_unease", "op": "add", "value": 3},
                    ],
                },
            ],
        },
        # ── 军事基地 ──
        {
            "id": "military_base_tension",
            "description": "军事基地内弥漫着不安的气氛，各种迹象暗示着即将到来的行动。掷骰决定你观察到什么。",
            "trigger": "玩家在军事区域时",
            "trigger_type": "conditional",
            "condition": "player.location in ['moscow_military_district', 'defense_ministry'] and world.coup_preparedness >= 70",
            "duration_turns": 0,
            "cooldown_turns": 3,
            "dice": {"count": 1, "faces": 100, "modifier": 0},
            "ranges": [
                {
                    "min": 1, "max": 35,
                    "label": "弹药装载",
                    "description": "后勤分队正在将实弹——不是演习用的空包弹——装入坦克弹药舱。军士长的脸色铁青，他从军二十年从未在莫斯科执行过实弹装填命令。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                        {"target": "player.心理压力", "op": "add", "value": 8},
                    ],
                },
                {
                    "min": 36, "max": 65,
                    "label": "撕裂的命令",
                    "description": "垃圾桶里有一张被撕碎的电报纸。你假装系鞋带时捡起碎片拼凑——是一份被某位军官拒绝执行后销毁的逮捕令，名单上有三位莫斯科市级官员的名字。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 10},
                        {"target": "player.心理压力", "op": "add", "value": 5},
                    ],
                },
                {
                    "min": 66, "max": 100,
                    "label": "军官密谈",
                    "description": "在军官食堂角落，三个中校围着一张地图低声争论。你路过时捕捉到一句：'格拉乔夫说了，他的伞兵不会动。我们呢？'他们注意到你后立即散开。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 8},
                        {"target": "world.vdv_loyalty", "op": "add", "value": -3},
                    ],
                },
            ],
        },
        # ── 阿尔巴特街书店 ──
        {
            "id": "arbat_bookshop_event",
            "description": "阿尔巴特街的旧书店是信息的隐秘交汇点。掷骰决定今天书店里藏着什么。",
            "trigger": "玩家在阿尔巴特街时",
            "trigger_type": "conditional",
            "condition": "player.location == 'arbat_street'",
            "duration_turns": 0,
            "cooldown_turns": 3,
            "dice": {"count": 1, "faces": 100, "modifier": 0},
            "ranges": [
                {
                    "min": 1, "max": 35,
                    "label": "书页间的纸条",
                    "description": "翻阅一本陀思妥耶夫斯基的旧版《群魔》时，一张对折的纸条从书页间滑出。上面用打字机打着一串数字——可能是某个死信箱的密码，也可能只是前一位读者的购物清单。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 3},
                    ],
                },
                {
                    "min": 36, "max": 65,
                    "label": "旧书商的暗示",
                    "description": "老书商擦着柜台，头也不抬地说：'今天的《真理报》很有意思，特别是第三版左下角的那篇社论。你不妨看看字里行间省略了什么。'他用手指轻敲了三下柜台——这是某种信号。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 8},
                    ],
                },
                {
                    "min": 66, "max": 100,
                    "label": "地下出版物",
                    "description": "玛丽娜从后间走出，递给你一份用蜡纸印刷的薄薄册子——汇编了过去一周被监听到的关键信息摘要，经过了谨慎的脱敏处理。'新货，刚到。'她压低声音，'看完了烧掉。'",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 12},
                        {"target": "player.心理压力", "op": "add", "value": 5},
                    ],
                },
            ],
        },
        # ── 福罗斯别墅 ──
        {
            "id": "foros_villa_atmosphere",
            "description": "被封锁的福罗斯别墅笼罩在不安的静谧中。掷骰决定这片孤岛上发生了什么。",
            "trigger": "玩家在福罗斯别墅时",
            "trigger_type": "conditional",
            "condition": "player.location == 'foros_villa' and world.gorbachev_isolation >= 30",
            "duration_turns": 0,
            "cooldown_turns": 2,
            "dice": {"count": 1, "faces": 100, "modifier": 0},
            "ranges": [
                {
                    "min": 1, "max": 30,
                    "label": "沉默的松林",
                    "description": "别墅周围的松林在海风中沙沙作响，但往常在树间跑动的警卫犬今天被全部拴在了岗亭旁。空气中弥漫着一种不祥的安静——像暴风雨前最后的平静。",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 5},
                    ],
                },
                {
                    "min": 31, "max": 60,
                    "label": "总统的散步",
                    "description": "戈尔巴乔夫在花园中独自散步，双手背在身后。你远远地看到他停在一棵老松树前站了很久，然后弯腰捡起一块石头看了看，又轻轻放下。他的孤独像一面无形的墙。",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 8},
                        {"target": "player.忠诚度", "op": "add", "value": 5},
                    ],
                },
                {
                    "min": 61, "max": 100,
                    "label": "信号残余",
                    "description": "在地下通讯枢纽巡逻时，你注意到一台被认为已经关闭的老式高频电台的指示灯仍在微弱闪烁——也许通讯切断并没有做到百分之百。也许还有一线希望。",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 10},
                        {"target": "player.心理压力", "op": "add", "value": -3},
                    ],
                },
            ],
        },
    ]

    count_ri = 0
    for ri in NEW_RANDOM_ITEMS:
        if ri["id"] not in existing_ri_ids:
            data["random_items"].append(ri)
            count_ri += 1
    print(f"[3] random_items: 新增 {count_ri} 个（总计 {len(data['random_items'])}）")

    # ═══════════════════════════════════════════════════════════════════
    # 4. 现有 random_items 的 state_changes 中 player 属性缺 prefix
    # ═══════════════════════════════════════════════════════════════════
    # 检查 pavlov_dacha_decision 的 "心理压力" target（没有 player. 前缀）
    count_fix = 0
    PLAYER_ATTRS = {"洞察力", "心理压力", "忠诚度", "技术能力", "体能"}
    for ri in data["random_items"]:
        for r in ri.get("ranges", []):
            for sc in r.get("state_changes", []):
                t = sc.get("target", "")
                if t in PLAYER_ATTRS:
                    sc["target"] = f"player.{t}"
                    count_fix += 1
    if count_fix:
        print(f"[4] random_items: 修复 {count_fix} 个缺少 player. 前缀的 target")
    else:
        print("[4] random_items: target 前缀检查通过")

    # ═══════════════════════════════════════════════════════════════════
    # 5. persistent_states 补充变化触发条件
    # ═══════════════════════════════════════════════════════════════════
    ps_by_id = {ps["id"]: ps for ps in data.get("persistent_states", [])}

    PS_ENHANCEMENTS = {
        "communication_monitoring": {
            "activation_condition": "",
            "deactivation_condition": "world.coup_preparedness <= 0",
            "prompt_when_active": "克格勃正在对克里姆林宫全部政府专线进行旁路监听（'回声-91'行动）。在卢比扬卡监听中心可接触到通话内容",
            "prompt_when_inactive": "监听行动已终止，'回声-91'的设备被封存",
        },
        "troop_movement_stealth": {
            "activation_condition": "",
            "deactivation_condition": "world.coup_preparedness >= 95",
            "prompt_when_active": "部队以'演习'名义秘密向莫斯科集结，但坦克引擎声和军用卡车已引起部分市民注意",
            "prompt_when_inactive": "军队已公开进入莫斯科，秘密调动阶段结束",
        },
        "foros_guard_standby": {
            "activation_condition": "",
            "deactivation_condition": "world.gorbachev_isolation >= 100",
            "prompt_when_active": "福罗斯别墅的通讯切断方案已就绪但尚未执行。总统卫队仍在正常执勤",
            "prompt_when_inactive": "福罗斯别墅已被完全封锁，总统卫队被解除武装或替换",
        },
    }
    count_ps = 0
    for pid, enhancements in PS_ENHANCEMENTS.items():
        if pid in ps_by_id:
            ps_by_id[pid].update(enhancements)
            count_ps += 1
    print(f"[5] persistent_states: 增强 {count_ps} 个状态描述")

    # ═══════════════════════════════════════════════════════════════════
    # 6. skill_check_map 补充缺失映射
    # ═══════════════════════════════════════════════════════════════════
    scm = data.get("settings", {}).get("skill_check_map", {})
    NEW_SKILLS = {
        "监听": "洞察力",
        "窃听": "洞察力",
        "破译密码": "洞察力",
        "辨声": "洞察力",
        "伪装": "洞察力",
        "潜行": "体能",
        "格斗": "体能",
        "射击": "体能",
        "驾驶": "体能",
        "说服": "洞察力",
        "谈判": "洞察力",
        "欺骗": "洞察力",
        "威吓": "体能",
        "急救": "体能",
        "拆卸": "技术能力",
        "焊接": "技术能力",
        "编码": "技术能力",
        "电子对抗": "技术能力",
        "频率分析": "技术能力",
        "抗审讯": "心理压力",
        "压力测试": "心理压力",
    }
    count_skill = 0
    for skill, attr in NEW_SKILLS.items():
        if skill not in scm:
            scm[skill] = attr
            count_skill += 1
    print(f"[6] skill_check_map: 新增 {count_skill} 个技能映射")

    # ═══════════════════════════════════════════════════════════════════
    # 保存
    # ═══════════════════════════════════════════════════════════════════
    new_content = json.dumps(data, ensure_ascii=False)
    if new_content != original:
        db.execute(
            'UPDATE scripts SET content=?, updated_at=CURRENT_TIMESTAMP WHERE id="soviet_august_coup_1991"',
            (new_content,),
        )
        db.commit()
        size_kb = len(new_content.encode("utf-8")) / 1024
        print(f"\n✓ 已保存到数据库 ({size_kb:.1f} KB)")
    else:
        print("\n⚠ 无变更")
    db.close()


if __name__ == "__main__":
    main()
