"""潜伏剧本第二轮丰富：补充 lorebook + 新增剧情树。"""
import json
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tavern.db")

# ─── 新增 lorebook 条目 ───────────────────────────────────────────────────────

NEW_LOREBOOK = [
    {
        "id": "左蓝之死",
        "keys": ["左蓝", "牺牲", "暗杀", "中毒"],
        "secondary_keys": ["余则成", "感情", "暴露"],
        "content": "左蓝是中共地下党员，也是余则成的初恋与革命引路人。她在天津执行任务期间身份暴露，被军统特务追杀。余则成试图营救但未能成功，左蓝最终牺牲。她的死对余则成产生了极其深远的影响——既坚定了他的革命信念，也让他内心深处埋下了永远无法愈合的伤痕。此后余则成在面对危险时，常以左蓝为精神支柱。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 60,
        "scan_depth": 3, "comment": "左蓝牺牲经过与影响", "related_entries": ["左蓝", "余则成"]
    },
    {
        "id": "马奎覆灭",
        "keys": ["马奎", "处决", "叛徒", "暴露"],
        "secondary_keys": ["峨眉峰", "调查", "李涯"],
        "content": "马奎是天津站行动队长，能力出众但为人粗暴。他在追查'峨眉峰'身份时最为积极，因为他本人就曾是中共叛徒——他害怕自己的旧底被翻出。马奎通过分析电报格式和接头暗号等线索一度接近真相，但余则成巧妙设计将嫌疑转移。最终马奎因其他问题（贪腐或被构陷）落马，被清除出天津站。他的覆灭是站内权力格局剧变的转折点。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 55,
        "scan_depth": 3, "comment": "马奎覆灭经过", "related_entries": ["马奎", "余则成"]
    },
    {
        "id": "翠平险情",
        "keys": ["翠平", "暴露", "农村", "习惯", "穿帮"],
        "secondary_keys": ["太太", "社交", "身份"],
        "content": "翠平原是冀中军区的游击队员，大字不识几个，性格泼辣直爽。她被组织安排假扮余则成的太太，以掩护其地下工作。然而翠平的农村习惯屡屡在上层社交场合穿帮：随地吐痰、大声说话、不会用刀叉、对太太们的话题一窍不通。每次险情都需余则成或秋掌柜紧急善后。翠平也在不断学习适应，但骨子里的朴实与刚烈始终是她最大的破绽和最真实的魅力。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 60,
        "scan_depth": 3, "comment": "翠平身份危机细节", "related_entries": ["翠平", "余则成"]
    },
    {
        "id": "电讯科运作",
        "keys": ["电讯科", "电报", "密码", "破译", "监听"],
        "secondary_keys": ["余则成", "发报", "频率"],
        "content": "天津站电讯科负责无线电通讯的收发与监控。余则成兼管电讯科事务，这给了他接触机密电报的便利，但也意味着他必须格外小心——每份经手的电报都有记录。电讯科日常工作包括：监听可疑频段、破译截获的共党电文、为行动队提供通讯支持。站内电报使用分级密码本，余则成的职务让他能接触乙级及以上密码。发报需登记时间、频率、字数。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 50,
        "scan_depth": 2, "comment": "电讯科日常运作与余则成的便利", "related_entries": ["余则成", "天津站日常"]
    },
    {
        "id": "天津站日常",
        "keys": ["天津站", "日常", "早会", "点名", "值班"],
        "secondary_keys": ["吴敬中", "站务", "例会"],
        "content": "天津站每日早晨8时举行例会，由站长吴敬中主持。各科室汇报前日工作进展、当日计划。李涯的情报科通常汇报共党动向分析，马奎/陆桥山的行动队报告抓捕行动，余则成的电讯科通报电报往来概要。例会后各科室各自运作。站内实行严格的签到签退制度，外出需登记。站内互相监视气氛浓厚，任何异常行为都可能被记录在案。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 45,
        "scan_depth": 2, "comment": "天津站日常运作", "related_entries": ["吴敬中", "余则成", "李涯"]
    },
    {
        "id": "行动队任务",
        "keys": ["行动队", "抓捕", "跟踪", "搜查", "刑讯"],
        "secondary_keys": ["马奎", "陆桥山", "暗杀"],
        "content": "天津站行动队是执行外勤任务的武装力量，负责：跟踪可疑目标、突袭共党据点、抓捕嫌疑人、执行暗杀命令、押送犯人。行动队员多为受过特训的老手，配备手枪和短刀。行动前需由站长或副站长签发行动令。大规模行动（如围捕）需协调宪兵和警察配合。行动队长（马奎/陆桥山）拥有一定的现场决断权，但重要目标必须活口带回审讯。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 45,
        "scan_depth": 2, "comment": "行动队职能与流程", "related_entries": ["马奎", "陆桥山"]
    },
    {
        "id": "吴站长用人术",
        "keys": ["吴敬中", "权术", "平衡", "制衡"],
        "secondary_keys": ["站长", "手腕", "袖手旁观"],
        "content": "吴敬中深谙官场权术，其管理天津站的核心策略是'制衡'：让李涯与陆桥山/马奎互相竞争牵制，让各科室互相监督。他自己则高坐钓鱼台，坐收渔利。吴敬中从不亲自动手做脏活，总让下属互相揭发、互相消耗。他对下属的忠诚度判断极准，但贪财好色的弱点也让他容易被利用。余则成正是利用吴敬中的贪婪本性，通过送礼和利益输送来维护信任关系。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 55,
        "scan_depth": 3, "comment": "吴敬中的管理权术", "related_entries": ["吴敬中", "李涯", "陆桥山"]
    },
    {
        "id": "李涯执念",
        "keys": ["李涯", "执念", "忠诚", "信仰"],
        "secondary_keys": ["反共", "查内鬼", "不眠"],
        "content": "李涯是天津站最危险的人物——不是因为他狡诈，而是因为他真诚。他是军统中少有的真正信仰'党国'理想的人，不贪财不好色，工作起来不要命。他追查共党卧底近乎偏执，常年睡眠不足，办公桌上堆满分析材料。李涯的可怕在于他的纯粹：没有弱点可以利用，没有欲望可以收买，只有那股'誓死反共'的执念驱动着他不断逼近真相。余则成视他为最大威胁。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 60,
        "scan_depth": 3, "comment": "李涯性格深度", "related_entries": ["李涯", "余则成"]
    },
    {
        "id": "谢若林生意经",
        "keys": ["谢若林", "情报", "买卖", "生意"],
        "secondary_keys": ["中间人", "价格", "消息"],
        "content": "谢若林是天津站的'情报掮客'，他将情报视为纯粹的商品——不问来源不问去向，只看价钱。他的名言是'两根金条放在一起，分不出哪根是高尚的哪根是龌龊的'。谢若林与各方势力都有交易：军统、共党、日伪残余、商界，谁出价高就卖给谁。他是余则成获取情报的重要渠道之一，但也极其危险——因为他同样可能把余则成的秘密卖给出价更高的人。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 50,
        "scan_depth": 2, "comment": "谢若林的情报交易", "related_entries": ["谢若林", "余则成"]
    },
    {
        "id": "佛龛详细",
        "keys": ["佛龛", "情报传递", "粉笔记号", "暗格"],
        "secondary_keys": ["余则成", "秋掌柜", "死信箱"],
        "content": "余则成与秋掌柜之间的情报传递系统以翠平家中的佛龛为核心：佛龛底座内设有暗格，可藏匿微缩胶卷或纸条。日常流程为：余则成将情报用密写药水写在普通纸张上（或拍成缩微胶卷），放入暗格；翠平在买菜时通过花盆暗号通知秋掌柜；秋掌柜择机以访客身份上门取走。紧急情况下另有备用方案：公园长椅下的报纸、理发店特定座位的杂志夹层等。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 65,
        "scan_depth": 3, "comment": "佛龛情报系统详细运作", "related_entries": ["佛龛行动", "余则成", "翠平", "秋掌柜"]
    },
    {
        "id": "军统内部派系",
        "keys": ["军统", "派系", "中统", "嫡系"],
        "secondary_keys": ["保密局", "CC系", "复兴社"],
        "content": "军统（保密局）内部并非铁板一块，存在多重派系：戴笠嫡系（浙江帮）、黄埔系、地方势力、降日人员等。天津站的吴敬中属浙江派余脉，李涯出身青浦特训班（中坚骨干），陆桥山是北方地方势力代表。各派系争夺资源和升迁机会，互相倾轧。同时军统与中统（CC系）的明争暗斗从未停歇——两个特务系统争抢功劳、互挖墙脚。这种内耗客观上为余则成提供了操作空间。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 50,
        "scan_depth": 2, "comment": "军统内部派系结构", "related_entries": ["保密局改组", "吴敬中", "李涯"]
    },
    {
        "id": "晚宴交际",
        "keys": ["晚宴", "交际", "舞会", "宴请"],
        "secondary_keys": ["太太", "应酬", "消息"],
        "content": "天津站高层经常举办或参加各种晚宴和舞会——这既是社交需要，也是情报工作的延伸。太太们在晚宴上的闲聊往往泄露丈夫的工作动向。余则成和翠平必须频繁出席这类场合，翠平的不适应成为巨大隐患。晚宴上各方势力互相试探：谁与谁走得近、谁最近频繁外出、谁突然阔绰了——都是有心人的观察目标。站长太太的茶话会更是消息集散地。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 45,
        "scan_depth": 2, "comment": "晚宴社交场景", "related_entries": ["太太外交", "翠平"]
    },
    {
        "id": "接头暗语",
        "keys": ["暗语", "接头", "切口", "暗号"],
        "secondary_keys": ["联络", "确认身份"],
        "content": "地下工作中的接头暗语是确认身份的关键手段。常见形式包括：约定问答对（如'今天天气不错'——'是的，适合去紫竹林走走'）、特定物品展示（左手持报纸、胸口别白色花）、时间地点组合（双数日上午10点鱼市第三个摊位）。暗语定期更换以防泄露。紧急接头有简化暗号，如特定方式敲门。暗语被破译是地下工作最致命的威胁之一。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 50,
        "scan_depth": 2, "comment": "接头暗语规则", "related_entries": ["单线联系", "花盆暗号"]
    },
    {
        "id": "穆连成商路",
        "keys": ["穆连成", "走私", "商路", "货物"],
        "secondary_keys": ["海门", "物资", "利润"],
        "content": "穆连成经营着天津最大的地下走私网络，从海门码头到法租界的路线是他的命脉。他的商路不仅运输紧俏物资（盘尼西林、布匹、五金），也暗中为各方传递人员和文件。穆连成奉行'黑白通吃'原则：给军统交保护费、给共党行方便、给日伪残余销赃。他的价值在于中立性——所有人都需要他的渠道，所以所有人都默许他的存在。但一旦平衡被打破，他就是最脆弱的环节。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 45,
        "scan_depth": 2, "comment": "穆连成的走私商路", "related_entries": ["穆连成", "海门走私网"]
    },
    {
        "id": "廖三民忠义",
        "keys": ["廖三民", "忠义", "底层", "干脏活"],
        "secondary_keys": ["行动队", "兄弟", "出生入死"],
        "content": "廖三民是行动队的资深队员，枪法精准、身手利落，曾多次执行暗杀和抓捕任务。他不懂政治权谋，只认'谁对我好我就跟谁'的江湖义气。马奎对他有知遇之恩，所以他死心塌地追随；马奎倒台后他陷入迷茫。廖三民代表了军统底层特务的悲哀——出生入死干脏活，却永远是大人物的棋子。如果余则成能争取到他的信任，将获得一个危险但忠诚的盟友。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 45,
        "scan_depth": 2, "comment": "廖三民性格与潜力", "related_entries": ["廖三民", "马奎"]
    },
    {
        "id": "天津租界",
        "keys": ["租界", "法租界", "英租界", "意租界"],
        "secondary_keys": ["天津", "势力范围", "管辖"],
        "content": "1946-1949年的天津虽然租界已名义上收回，但各前租界区仍保有明显的外国色彩和复杂的势力格局。法租界区域的咖啡馆和舞厅是上层社交场所；前英租界区域的银行和洋行仍是金融中心。不同区域之间的管辖权模糊地带是地下活动的温床——军统、中统、共党、帮派、外国势力都在这些灰色地带活动。追踪者进入前租界区域往往会'跟丢'目标。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 45,
        "scan_depth": 2, "comment": "天津租界地理与势力", "related_entries": ["天津地理"]
    },
    {
        "id": "国共停战破裂",
        "keys": ["停战", "破裂", "和谈", "全面内战"],
        "secondary_keys": ["马歇尔", "军调处", "撕毁"],
        "content": "1946年初国共在美国调停下达成停战协议，军事调处执行部（军调处）负责监督。但双方互不信任，停战期间小规模冲突不断。1946年6月蒋介石撕毁停战协议，全面内战爆发。这一背景意味着：军调处这个曾经的'中立'机构变成了废纸，原本在军调处活动的各方情报人员需要迅速转移或隐蔽。余则成的'军调处时期'身份变成了一把双刃剑。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 50,
        "scan_depth": 2, "comment": "停战破裂背景", "related_entries": ["军调处", "双十协定"]
    },
    {
        "id": "共党策反术",
        "keys": ["策反", "争取", "瓦解", "起义"],
        "secondary_keys": ["组织", "动摇", "投诚"],
        "content": "中共在解放战争中大量运用策反战术：针对国民党军政人员的不满情绪，通过地下关系逐步拉拢。策反对象通常具备以下特征：对腐败不满、有旧同学/亲友在共方、仕途受阻心生怨恨、或是已被抓住把柄的人。策反过程极为耐心——先建立信任，再逐步透露身份，最后提出合作。一旦成功，被策反者比原有地下党更难被怀疑。天津站内就有多人已暗中动摇。",
        "position": "after_char", "enabled": True, "constant": False, "priority": 55,
        "scan_depth": 2, "comment": "中共策反术", "related_entries": ["余则成", "单线联系"]
    },
]

# ─── 新增剧情树：翠平身份危机线 ─────────────────────────────────────────────

NEW_TREE_CUIPING = {
    "id": "cuiping_crisis",
    "name": "粗瓷碗与高脚杯 — 翠平线",
    "description": "翠平的农村背景与上层太太伪装之间的矛盾不断激化，每一次社交都是一场惊心动魄的走钢丝。",
    "icon": "🏺",
    "nodes": [
        {
            "id": "cc_first_banquet",
            "name": "第一次晚宴",
            "description": "翠平首次出席天津站内部聚会，必须装出大家闺秀的样子。",
            "type": "auto",
            "condition": "",
            "requires": [],
            "on_complete_unlock": ["cc_etiquette_training", "cc_neighbor_suspicion"],
            "effects": {
                "inject_prompt": "翠平在晚宴上的表现引起了部分太太的好奇——这位余太太似乎不太一样。",
                "notify": "【新支线】翠平的伪装面临考验"
            },
            "position": {"x": 0, "y": 0}
        },
        {
            "id": "cc_etiquette_training",
            "name": "礼仪特训",
            "description": "余则成抽空教翠平基本社交礼仪，但时间紧迫效果有限。",
            "type": "timed",
            "duration_turns": 3,
            "condition": "",
            "requires": ["cc_first_banquet"],
            "on_complete_unlock": ["cc_tea_party_test"],
            "effects": {
                "inject_prompt": "翠平学会了基本的用餐礼仪和社交应答，虽然生硬但勉强过关。",
                "set_var": [{"key": "cuiping_etiquette", "value": 1}]
            },
            "position": {"x": 1, "y": -1}
        },
        {
            "id": "cc_neighbor_suspicion",
            "name": "邻居起疑",
            "description": "楼下邻居注意到翠平白天的行为习惯异于常人——大声说话、晾衣方式奇怪。",
            "type": "auto",
            "condition": "",
            "requires": ["cc_first_banquet"],
            "on_complete_unlock": ["cc_tea_party_test"],
            "effects": {
                "inject_prompt": "邻居太太向站长太太随口提起'余太太好像是乡下来的'，话虽轻描淡写，却像一根刺扎在余则成心里。"
            },
            "position": {"x": 1, "y": 1}
        },
        {
            "id": "cc_tea_party_test",
            "name": "太太茶话会",
            "description": "站长太太举办茶话会，翠平必须出席。这是一次关键考验。",
            "type": "choice",
            "condition": "",
            "requires": ["cc_etiquette_training", "cc_neighbor_suspicion"],
            "on_complete_unlock": [],
            "choices": [
                {
                    "id": "cc_coach_cuiping",
                    "label": "事先详细叮嘱翠平",
                    "description": "花时间反复演练可能的对话场景，减少穿帮风险但耗费精力。",
                    "effects": {
                        "inject_prompt": "翠平在茶话会上表现得沉默寡言但没出大错——太太们觉得她'内向'。",
                        "set_var": [{"key": "cuiping_cover_strength", "value": 70}]
                    },
                    "unlock": ["cc_cuiping_adapts"]
                },
                {
                    "id": "cc_fake_illness",
                    "label": "让翠平称病不去",
                    "description": "回避风险，但引起更多好奇和猜测。",
                    "effects": {
                        "inject_prompt": "翠平缺席引发了太太们的议论——'余太太是不是瞧不起我们？'这种猜测比真相更麻烦。",
                        "set_var": [{"key": "cuiping_cover_strength", "value": 40}]
                    },
                    "unlock": ["cc_escalation"]
                },
                {
                    "id": "cc_let_cuiping_be",
                    "label": "让翠平自由发挥",
                    "description": "信任翠平的应变能力，但风险极大。",
                    "effects": {
                        "inject_prompt": "翠平在茶话会上闹了几个笑话，但她用爽朗的性格化解了尴尬——有些太太反而觉得她'真性情'。结果是不确定的。",
                        "set_var": [{"key": "cuiping_cover_strength", "value": 55}]
                    },
                    "unlock": ["cc_cuiping_adapts", "cc_escalation"]
                }
            ],
            "position": {"x": 2, "y": 0}
        },
        {
            "id": "cc_cuiping_adapts",
            "name": "翠平的蜕变",
            "description": "翠平逐渐找到了自己的社交方式——不完美，但有效。",
            "type": "auto",
            "condition": "cuiping_cover_strength >= 55",
            "requires": ["cc_tea_party_test"],
            "on_complete_unlock": ["cc_final_test"],
            "effects": {
                "inject_prompt": "翠平开始学会用沉默和微笑应对不懂的话题，偶尔冒出的'土话'被她巧妙伪装成'老家方言'。她正在成长。",
                "notify": "翠平的伪装技能有所提升"
            },
            "position": {"x": 3, "y": -1}
        },
        {
            "id": "cc_escalation",
            "name": "疑云加重",
            "description": "有心人开始认真调查余太太的背景，局势愈发紧张。",
            "type": "auto",
            "condition": "cuiping_cover_strength < 55",
            "requires": ["cc_tea_party_test"],
            "on_complete_unlock": ["cc_final_test"],
            "effects": {
                "inject_prompt": "李涯在一次偶然中注意到翠平说了句'同志们'——虽然她立刻改口，但李涯的眼神明显变了。",
                "notify": "【危险】翠平的伪装出现严重裂痕"
            },
            "position": {"x": 3, "y": 1}
        },
        {
            "id": "cc_final_test",
            "name": "最终抉择",
            "description": "翠平的真实身份即将暴露，余则成必须做出艰难决定。",
            "type": "choice",
            "condition": "",
            "requires": ["cc_cuiping_adapts", "cc_escalation"],
            "on_complete_unlock": [],
            "choices": [
                {
                    "id": "cc_send_away",
                    "label": "送翠平离开天津",
                    "description": "以'回老家养病'为由将翠平转移，安全但失去重要掩护。",
                    "effects": {
                        "inject_prompt": "翠平含泪离开——她知道这可能是永别。余则成独自一人面对更加危险的处境，但至少翠平安全了。",
                        "set_var": [{"key": "cuiping_status", "value": "evacuated"}],
                        "fire_events": ["cuiping_leaves"]
                    },
                    "unlock": []
                },
                {
                    "id": "cc_double_down",
                    "label": "加固伪装继续坚持",
                    "description": "制造更精密的身份背景故事，冒险但维持现有格局。",
                    "effects": {
                        "inject_prompt": "余则成精心伪造了一套'翠平老家'的完整故事和证据，暂时稳住了局面。但谎言越来越复杂，维护成本越来越高。",
                        "set_var": [{"key": "cuiping_status", "value": "reinforced"}]
                    },
                    "unlock": []
                },
                {
                    "id": "cc_counterattack",
                    "label": "反击质疑者",
                    "description": "主动出击，用计谋让追查者自顾不暇。",
                    "effects": {
                        "inject_prompt": "余则成反手将'调查余太太'的人牵连进一桩贪腐案——让他们忙于自保，无暇顾及翠平的事。这步棋很险，但目前奏效了。",
                        "set_var": [{"key": "cuiping_status", "value": "counterattack"}],
                        "fire_events": ["internal_conflict_escalates"]
                    },
                    "unlock": []
                }
            ],
            "position": {"x": 4, "y": 0}
        }
    ]
}

# ─── 扩展现有剧情树：情报暗战增加后续节点 ──────────────────────────────────────

INTEL_WAR_NEW_NODES = [
    {
        "id": "iw_network_expansion",
        "name": "扩展情报网",
        "description": "利用已有的情报渠道建立更广泛的信息网络——但网络越大越容易暴露。",
        "type": "choice",
        "condition": "",
        "requires": ["iw_final_delivery"],
        "on_complete_unlock": [],
        "choices": [
            {
                "id": "iw_expand_cautious",
                "label": "稳健扩展",
                "description": "只发展经过长期考验的可靠线人，速度慢但安全。",
                "effects": {
                    "inject_prompt": "情报网缓慢但稳健地扩展，每一个新节点都经过反复验证。质量优先于速度。",
                    "set_var": [{"key": "intel_network_size", "value": "moderate"}]
                },
                "unlock": ["iw_endgame_prep"]
            },
            {
                "id": "iw_expand_aggressive",
                "label": "快速扩张",
                "description": "争取尽可能多的情报来源，为即将到来的决战做准备。",
                "effects": {
                    "inject_prompt": "情报网快速扩展——但仓促发展的线人中是否混入了敌方的耳目？这个疑虑如影随形。",
                    "set_var": [{"key": "intel_network_size", "value": "large"}]
                },
                "unlock": ["iw_endgame_prep"]
            }
        ],
        "position": {"x": 6, "y": 0}
    },
    {
        "id": "iw_endgame_prep",
        "name": "终局准备",
        "description": "平津战役前夜，所有情报力量都在为最后的决战做准备。",
        "type": "quest",
        "condition": "",
        "requires": ["iw_network_expansion"],
        "on_complete_unlock": [],
        "effects": {
            "inject_prompt": "解放军兵临城下，天津站陷入混乱。余则成必须在这最后的时刻完成组织交付的终极任务——这将决定天津解放的代价。",
            "notify": "【终局】平津战役序幕拉开",
            "fire_events": ["endgame_begins"],
            "activate_state": ["final_phase"]
        },
        "position": {"x": 7, "y": 0}
    }
]

# ─── 执行 ─────────────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT content FROM scripts WHERE id='qianfu'").fetchone()
    if not row:
        print("ERROR: Script 'qianfu' not found")
        return
    script = json.loads(row[0])

    # 1. 添加 lorebook
    existing_ids = {e["id"] for e in script.get("lorebook", [])}
    added_lb = 0
    for entry in NEW_LOREBOOK:
        if entry["id"] not in existing_ids:
            script.setdefault("lorebook", []).append(entry)
            added_lb += 1
    print(f"Lorebook: added {added_lb} entries (total: {len(script['lorebook'])})")

    # 2. 添加翠平剧情树
    st = script.setdefault("story_tree", {"trees": []})
    existing_tree_ids = {t["id"] for t in st["trees"]}
    if NEW_TREE_CUIPING["id"] not in existing_tree_ids:
        st["trees"].append(NEW_TREE_CUIPING)
        print(f"Story tree: added '{NEW_TREE_CUIPING['name']}'")
    else:
        print(f"Story tree '{NEW_TREE_CUIPING['id']}' already exists, skipping")

    # 3. 扩展情报暗战树
    intel_tree = None
    for t in st["trees"]:
        if t["id"] == "intelligence_war":
            intel_tree = t
            break
    if intel_tree:
        existing_node_ids = {n["id"] for n in intel_tree.get("nodes", [])}
        added_nodes = 0
        for node in INTEL_WAR_NEW_NODES:
            if node["id"] not in existing_node_ids:
                intel_tree["nodes"].append(node)
                added_nodes += 1
        print(f"Intelligence_war tree: added {added_nodes} nodes (total: {len(intel_tree['nodes'])})")
    else:
        print("WARNING: intelligence_war tree not found")

    # 4. 验证
    print("\n--- Validation ---")
    all_node_ids = set()
    for t in st["trees"]:
        for n in t.get("nodes", []):
            all_node_ids.add(n["id"])

    errors = []
    for t in st["trees"]:
        for n in t.get("nodes", []):
            for req in n.get("requires", []):
                if req not in all_node_ids:
                    errors.append(f"Node '{n['id']}' requires unknown '{req}'")
            for unlock in n.get("on_complete_unlock", []):
                if unlock not in all_node_ids:
                    errors.append(f"Node '{n['id']}' unlocks unknown '{unlock}'")
            for c in n.get("choices", []):
                for u in c.get("unlock", []):
                    if u not in all_node_ids:
                        errors.append(f"Choice '{c['id']}' unlocks unknown '{u}'")

    if errors:
        for e in errors:
            print(f"  [!] {e}")
    else:
        print("  [OK] All story tree references valid")

    # Check lorebook
    lb_ids = [e["id"] for e in script["lorebook"]]
    if len(lb_ids) != len(set(lb_ids)):
        print("  [!] Duplicate lorebook IDs!")
    else:
        print(f"  [OK] Lorebook: {len(lb_ids)} entries, no duplicates")

    # 5. 写入数据库
    content_str = json.dumps(script, ensure_ascii=False, indent=2)
    conn.execute("UPDATE scripts SET content=?, updated_at=datetime('now') WHERE id='qianfu'",
                 (content_str,))
    conn.commit()
    conn.close()
    print(f"\n[DONE] Database updated. Total script size: {len(content_str)} chars")

    # Save working copy
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qianfu_enriched.json")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content_str)
    print(f"Working copy saved: {out_path}")


if __name__ == "__main__":
    main()
