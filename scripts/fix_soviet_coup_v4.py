"""八一九事变剧本第四轮优化：剧情树 + 事件选择 + 动态事件 + 填充空白时间线"""
import sqlite3, json, sys

DB_PATH = "data/tavern.db"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    db = sqlite3.connect(DB_PATH)
    row = db.execute('SELECT content FROM scripts WHERE id="soviet_august_coup_1991"').fetchone()
    data = json.loads(row[0])
    original = json.dumps(data, ensure_ascii=False)

    # ═══════════════════════════════════════════════════════════════════
    # 1. STORY TREE — 三条主线剧情树
    # ═══════════════════════════════════════════════════════════════════
    story_tree = data.get("story_tree", {})
    story_tree.setdefault("trees", [])

    existing_tree_ids = {t["id"] for t in story_tree["trees"]}

    # ── 剧情树 A: 真相之路（信息收集主线）──────────────────────
    if "truth_path" not in existing_tree_ids:
        story_tree["trees"].append({
            "id": "truth_path",
            "name": "真相之路",
            "description": "通过监听、窃密和人脉，逐步拼凑政变阴谋的全貌",
            "icon": "eye",
            "nodes": [
                {
                    "id": "tp_echo91_discovery",
                    "name": "发现'回声-91'",
                    "description": "在日常监听工作中发现克留奇科夫授权的秘密监听行动，对象竟是克里姆林宫全部政府专线",
                    "type": "auto",
                    "condition": "",
                    "position": {"x": 0, "y": 0},
                    "effects": {
                        "inject_prompt": "玩家已意识到克格勃内部存在针对最高领导层的秘密监听行动'回声-91'，这不是常规反间谍，而是政变准备的一部分",
                        "activate_lore": ["lore_echo91"],
                    },
                    "on_complete_unlock": ["tp_choose_side_early"],
                    "related_npcs": ["igor_chebrikov", "vladimir_kryuchkov"],
                    "related_orgs": ["org_kgb_12th_directorate"],
                },
                {
                    "id": "tp_choose_side_early",
                    "name": "初次抉择",
                    "description": "掌握了'回声-91'的秘密后，你必须决定如何处置这份危险的知识",
                    "type": "choice",
                    "position": {"x": 0, "y": 1},
                    "choices": [
                        {
                            "id": "report_to_kryuchkov",
                            "label": "向上汇报",
                            "description": "向克留奇科夫表忠心，报告你发现了'回声-91'并请求参与更深层的行动",
                            "effects": {
                                "set_var": [{"var_id": "player.忠诚度", "op": "add", "value": 20}],
                                "inject_prompt": "玩家选择效忠克留奇科夫，成为政变阴谋的知情参与者。克留奇科夫对你的忠诚表示赞赏，但也意味着你已无退路",
                            },
                            "unlock": ["tp_insider_briefing", "tp_canned_goods_access"],
                        },
                        {
                            "id": "contact_marina",
                            "label": "秘密联络线人",
                            "description": "通过阿尔巴特街的旧书店联系地下信息网络，将发现告知可信赖的人",
                            "effects": {
                                "set_var": [
                                    {"var_id": "player.忠诚度", "op": "add", "value": -15},
                                    {"var_id": "player.洞察力", "op": "add", "value": 10},
                                ],
                                "inject_prompt": "玩家选择秘密将'回声-91'的情报传递给地下信息网络，开始走向反抗之路",
                                "reveal_npcs": ["marina_retko"],
                            },
                            "unlock": ["tp_underground_network", "tp_dead_drop_chain"],
                        },
                        {
                            "id": "stay_silent",
                            "label": "保持沉默",
                            "description": "装作什么都不知道，继续执行日常监听任务，暗中观察局势发展",
                            "effects": {
                                "set_var": [{"var_id": "player.心理压力", "op": "add", "value": 15}],
                                "inject_prompt": "玩家选择沉默观望，内心在恐惧与良知之间煎熬。秘密像一块烧红的铁压在胸口",
                            },
                            "unlock": ["tp_passive_intel", "tp_conscience_crisis"],
                        },
                    ],
                    "related_npcs": ["vladimir_kryuchkov", "marina_retko"],
                },
                # ── 效忠路线分支 ──
                {
                    "id": "tp_insider_briefing",
                    "name": "内部吹风会",
                    "description": "克留奇科夫在亚利桑那安全屋召开核心吹风会，你被允许旁听'罐头'方案的部分内容",
                    "type": "timed",
                    "duration_turns": 2,
                    "position": {"x": -1, "y": 2},
                    "effects": {
                        "inject_prompt": "玩家在安全屋旁听到'罐头'方案的关键细节：8月18日派特使团赴福罗斯、切断通讯、19日凌晨坦克入城。政变时间表已在你脑中",
                        "set_var": [{"var_id": "player.洞察力", "op": "add", "value": 15}],
                    },
                    "on_complete_unlock": ["tp_final_choice"],
                    "related_npcs": ["vladimir_kryuchkov"],
                    "related_orgs": ["org_gkchp"],
                },
                {
                    "id": "tp_canned_goods_access",
                    "name": "接触'罐头'文件",
                    "description": "利用新获得的信任权限，尝试接触完整版'罐头'方案文件",
                    "type": "quest",
                    "completion_keywords": ["罐头", "方案", "文件", "档案"],
                    "position": {"x": -2, "y": 2},
                    "effects": {
                        "set_var": [{"var_id": "player.洞察力", "op": "add", "value": 20}],
                        "inject_prompt": "玩家获取了完整的'罐头'方案——包括紧急状态委员会名单、军队调动计划、逮捕名单和媒体管控方案",
                    },
                    "on_complete_unlock": ["tp_final_choice"],
                    "related_orgs": ["org_gkchp"],
                },
                # ── 反抗路线分支 ──
                {
                    "id": "tp_underground_network",
                    "name": "地下情报网",
                    "description": "通过玛丽娜的书店建立秘密情报传递渠道，将监听到的关键信息传递给叶利钦阵营",
                    "type": "timed",
                    "duration_turns": 3,
                    "position": {"x": 1, "y": 2},
                    "effects": {
                        "set_var": [
                            {"var_id": "world.yeltsin_awareness", "op": "add", "value": 10},
                            {"var_id": "player.心理压力", "op": "add", "value": 10},
                        ],
                        "inject_prompt": "玩家成功建立了从克格勃内部到叶利钦阵营的秘密情报管道，每天通过书店的死信箱传递关键监听摘要",
                    },
                    "on_complete_unlock": ["tp_final_choice"],
                    "related_npcs": ["marina_retko", "boris_yeltsin"],
                },
                {
                    "id": "tp_dead_drop_chain",
                    "name": "死信箱任务链",
                    "description": "在莫斯科各处设置安全的情报交接点，每个地点都有独特的暗号系统",
                    "type": "quest",
                    "completion_keywords": ["死信箱", "暗号", "交接", "传递"],
                    "position": {"x": 2, "y": 2},
                    "effects": {
                        "inject_prompt": "玩家在阿尔巴特街书店、红场地铁站、特维尔大街报亭建立了三个死信箱。每个使用不同的《战争与和平》页码作为暗号",
                        "set_var": [{"var_id": "player.洞察力", "op": "add", "value": 10}],
                    },
                    "on_complete_unlock": ["tp_final_choice"],
                    "related_npcs": ["marina_retko"],
                },
                # ── 沉默路线分支 ──
                {
                    "id": "tp_passive_intel",
                    "name": "被动情报积累",
                    "description": "在日常工作中不动声色地记忆每一条关键信息，等待使用它们的时机",
                    "type": "timed",
                    "duration_turns": 4,
                    "position": {"x": 0, "y": 2},
                    "effects": {
                        "set_var": [
                            {"var_id": "player.洞察力", "op": "add", "value": 25},
                            {"var_id": "player.心理压力", "op": "add", "value": 20},
                        ],
                        "inject_prompt": "长时间的沉默观察让你对政变计划的了解比大多数参与者还要深入，但秘密的重压也在吞噬你的精神",
                    },
                    "on_complete_unlock": ["tp_conscience_crisis"],
                },
                {
                    "id": "tp_conscience_crisis",
                    "name": "良知危机",
                    "description": "当政变进入倒计时，沉默不再是一种选项——你必须做出行动",
                    "type": "choice",
                    "condition": "world.coup_preparedness >= 90",
                    "position": {"x": 0, "y": 3},
                    "choices": [
                        {
                            "id": "break_silence_resist",
                            "label": "打破沉默——反抗",
                            "description": "将积累的所有情报一次性传递给叶利钦阵营",
                            "effects": {
                                "set_var": [
                                    {"var_id": "world.yeltsin_awareness", "op": "add", "value": 15},
                                    {"var_id": "player.忠诚度", "op": "add", "value": -30},
                                ],
                                "inject_prompt": "你在最后时刻将所有积累的情报倾倒给叶利钦的人——时间表、名单、部署图，一切。良心终于得到了片刻安宁，但危险才刚刚开始",
                            },
                            "unlock": ["tp_final_choice"],
                        },
                        {
                            "id": "break_silence_join",
                            "label": "打破沉默——加入",
                            "description": "主动向政变派表明立场，用你的情报优势换取保护",
                            "effects": {
                                "set_var": [{"var_id": "player.忠诚度", "op": "add", "value": 25}],
                                "inject_prompt": "你选择投身政变一方，用你对形势的深刻了解作为投名状。但历史的走向，远比你想象的更不可控",
                            },
                            "unlock": ["tp_final_choice"],
                        },
                    ],
                },
                # ── 汇合：最终抉择 ──
                {
                    "id": "tp_final_choice",
                    "name": "帝国的黄昏",
                    "description": "政变已经发动，坦克碾过莫斯科街头。你掌握的真相将决定你和许多人的命运",
                    "type": "choice",
                    "condition": "world.coup_preparedness >= 95",
                    "position": {"x": 0, "y": 4},
                    "choices": [
                        {
                            "id": "final_support_coup",
                            "label": "支持紧急状态委员会",
                            "description": "用你的情报和技术能力帮助政变派巩固权力",
                            "effects": {
                                "set_var": [
                                    {"var_id": "player.忠诚度", "op": "add", "value": 30},
                                    {"var_id": "world.coup_preparedness", "op": "add", "value": 5},
                                ],
                                "inject_prompt": "你全力支持政变。但随着叶利钦登上坦克、军队开始倒戈，你意识到自己押错了历史的赌注",
                            },
                        },
                        {
                            "id": "final_resist_coup",
                            "label": "协助抵抗政变",
                            "description": "冒着生命危险将关键情报传递给白宫守卫者",
                            "effects": {
                                "set_var": [
                                    {"var_id": "world.yeltsin_awareness", "op": "add", "value": 10},
                                    {"var_id": "player.心理压力", "op": "add", "value": 15},
                                ],
                                "inject_prompt": "你冒死将军队调动情报传递给白宫守卫者。在枪声和呐喊中，你看到了民主的脆弱与坚韧",
                            },
                        },
                        {
                            "id": "final_escape",
                            "label": "带着秘密消失",
                            "description": "利用掌握的情报作为护身符，在混乱中脱身",
                            "effects": {
                                "set_var": [{"var_id": "player.心理压力", "op": "add", "value": 25}],
                                "inject_prompt": "你选择在乱局中消失，带走的秘密足以让无数人夜不能寐。但逃离的人，往往被记忆追赶一辈子",
                            },
                        },
                    ],
                    "related_orgs": ["org_gkchp"],
                },
            ],
        })
        print("[1a] story_tree: 添加'真相之路'（12 节点，3 条分支路线）")

    # ── 剧情树 B: 军队抉择线（适合军官/VDV预设）──────────────
    if "military_dilemma" not in existing_tree_ids:
        story_tree["trees"].append({
            "id": "military_dilemma",
            "name": "军人的荣誉",
            "description": "当誓言与良知冲突，军人必须选择真正的祖国",
            "icon": "shield",
            "nodes": [
                {
                    "id": "md_unusual_orders",
                    "name": "异常调令",
                    "description": "部队接到不经正常指挥链下达的集结命令，目的地指向莫斯科市中心",
                    "type": "auto",
                    "condition": "world.coup_preparedness >= 70",
                    "position": {"x": 0, "y": 0},
                    "effects": {
                        "inject_prompt": "部队收到异常调令——不经正常指挥链，直接命令向莫斯科市中心集结。老兵们交换着不安的眼神",
                    },
                    "on_complete_unlock": ["md_verify_orders"],
                    "related_orgs": ["org_taman_division", "org_kantemirovskaya_division"],
                },
                {
                    "id": "md_verify_orders",
                    "name": "核实命令",
                    "description": "尝试通过正规渠道核实调令的合法性",
                    "type": "choice",
                    "position": {"x": 0, "y": 1},
                    "choices": [
                        {
                            "id": "obey_blindly",
                            "label": "无条件服从",
                            "description": "军人以服从命令为天职，不问原因",
                            "effects": {
                                "set_var": [{"var_id": "player.忠诚度", "op": "add", "value": 10}],
                                "inject_prompt": "你选择不问原因服从命令。坦克发动机轰鸣着驶向莫斯科，你在炮塔里听到收音机里反复播放《天鹅湖》",
                            },
                            "unlock": ["md_face_civilians"],
                        },
                        {
                            "id": "seek_confirmation",
                            "label": "向上级求证",
                            "description": "通过加密电台联系师部，确认命令是否经过总参谋部授权",
                            "effects": {
                                "set_var": [{"var_id": "player.洞察力", "op": "add", "value": 10}],
                                "inject_prompt": "师部的回复含混不清——他们也不确定。这条指挥链的某个环节被人为绕过了",
                            },
                            "unlock": ["md_face_civilians", "md_contact_grachev"],
                        },
                        {
                            "id": "delay_movement",
                            "label": "以技术故障拖延",
                            "description": "谎报车辆故障，争取时间了解真相",
                            "effects": {
                                "set_var": [
                                    {"var_id": "player.忠诚度", "op": "add", "value": -10},
                                    {"var_id": "player.洞察力", "op": "add", "value": 5},
                                ],
                                "inject_prompt": "你谎报三辆坦克出现液压故障，赢得了几小时的缓冲。但政治军官已经开始注意你",
                            },
                            "unlock": ["md_contact_grachev"],
                        },
                    ],
                    "related_npcs": ["dmitry_yazov"],
                },
                {
                    "id": "md_contact_grachev",
                    "name": "格拉乔夫的秘密通话",
                    "description": "通过私人渠道联系空降兵司令格拉乔夫，了解军队内部的真实态度",
                    "type": "timed",
                    "duration_turns": 2,
                    "position": {"x": 1, "y": 2},
                    "effects": {
                        "inject_prompt": "格拉乔夫在加密通话中暗示：'注意叶利钦在白宫的动向，不要做让自己后悔的事。'他的部队不会开枪",
                        "set_var": [{"var_id": "world.vdv_loyalty", "op": "add", "value": -5}],
                    },
                    "on_complete_unlock": ["md_barricade_decision"],
                    "related_npcs": ["pavel_grachev"],
                },
                {
                    "id": "md_face_civilians",
                    "name": "面对平民",
                    "description": "坦克部队抵达莫斯科街头，大批市民围堵在装甲车前，有人送来面包和鲜花",
                    "type": "auto",
                    "condition": "world.coup_preparedness >= 95",
                    "position": {"x": -1, "y": 2},
                    "effects": {
                        "inject_prompt": "年轻的莫斯科姑娘把一束野花塞进坦克履带。一个老太太举着圣像跪在车前。你的炮手悄声说：'长官，我射不了。'",
                        "set_var": [{"var_id": "player.心理压力", "op": "add", "value": 15}],
                    },
                    "on_complete_unlock": ["md_barricade_decision"],
                },
                {
                    "id": "md_barricade_decision",
                    "name": "路障前的抉择",
                    "description": "白宫前的路障越建越高，你的部队被命令清除障碍物并控制桥梁",
                    "type": "choice",
                    "condition": "world.coup_preparedness >= 95",
                    "position": {"x": 0, "y": 3},
                    "choices": [
                        {
                            "id": "clear_barricade",
                            "label": "执行清障命令",
                            "description": "军令如山，开动坦克推倒路障",
                            "effects": {
                                "set_var": [
                                    {"var_id": "world.public_unease", "op": "add", "value": 20},
                                    {"var_id": "player.心理压力", "op": "add", "value": 25},
                                ],
                                "inject_prompt": "坦克碾过路障的声音像骨头断裂。人群尖叫着后退，有人高喊叛徒。你的战友低下头不忍直视",
                            },
                        },
                        {
                            "id": "turn_turrets",
                            "label": "调转炮口",
                            "description": "命令全连调转炮口，面向克里姆林宫方向——这是无声的宣言",
                            "effects": {
                                "set_var": [
                                    {"var_id": "world.vdv_loyalty", "op": "add", "value": -15},
                                    {"var_id": "world.yeltsin_awareness", "op": "add", "value": 5},
                                    {"var_id": "player.忠诚度", "op": "add", "value": -30},
                                ],
                                "inject_prompt": "十二辆坦克同时调转炮口，面向克里姆林宫。人群爆发出欢呼。你知道，这一刻你选择了历史的另一边",
                                "fire_events": ["tank_battalion_defection"],
                            },
                        },
                        {
                            "id": "hold_position",
                            "label": "原地待命",
                            "description": "拒绝执行任何方向的命令，就地驻扎等待局势明朗",
                            "effects": {
                                "set_var": [{"var_id": "player.心理压力", "op": "add", "value": 10}],
                                "inject_prompt": "你命令全连就地待命，既不前进也不撤退。坦克变成了钢铁岛屿，在人群的海洋中静默不动",
                            },
                        },
                    ],
                    "related_npcs": ["pavel_grachev", "viktor_karpukhin"],
                },
            ],
        })
        print("[1b] story_tree: 添加'军人的荣誉'（5 节点）")

    # ── 剧情树 C: 福罗斯围城线（适合总统卫队预设）──────────────
    if "foros_siege" not in existing_tree_ids:
        story_tree["trees"].append({
            "id": "foros_siege",
            "name": "福罗斯围城",
            "description": "在与世隔绝的别墅中保护总统，是忠诚的最极端考验",
            "icon": "castle",
            "nodes": [
                {
                    "id": "fs_comms_dying",
                    "name": "信号消亡",
                    "description": "别墅的通讯设备逐一失灵，最后连普通电话线都陷入死寂",
                    "type": "auto",
                    "condition": "world.gorbachev_isolation >= 45",
                    "position": {"x": 0, "y": 0},
                    "effects": {
                        "inject_prompt": "先是卫星电话，然后是高频电台，接着是海底光缆终端——福罗斯别墅的通讯线路在一小时内逐一死亡。总统面色铁青",
                    },
                    "on_complete_unlock": ["fs_plekhanov_arrives"],
                    "related_npcs": ["yury_plekhanov"],
                },
                {
                    "id": "fs_plekhanov_arrives",
                    "name": "不速之客",
                    "description": "克格勃第九局局长普列哈诺夫率武装人员抵达别墅外围",
                    "type": "choice",
                    "position": {"x": 0, "y": 1},
                    "choices": [
                        {
                            "id": "fortify_villa",
                            "label": "加固防线",
                            "description": "封锁别墅所有出入口，进入战时警戒状态",
                            "effects": {
                                "set_var": [{"var_id": "player.体能", "op": "add", "value": -10}],
                                "inject_prompt": "你下令封锁别墅的十七个出入口，在二楼走廊架设交叉火力位。但你心里清楚——面对整个克格勃第九局，这只是象征性的抵抗",
                            },
                            "unlock": ["fs_ultimatum"],
                        },
                        {
                            "id": "negotiate_entry",
                            "label": "谈判拖延",
                            "description": "以核实身份和权限为由拖延时间",
                            "effects": {
                                "set_var": [{"var_id": "player.洞察力", "op": "add", "value": 10}],
                                "inject_prompt": "你在门口与普列哈诺夫的副官周旋了四十分钟，以'核实授权文件'为由争取时间。在此期间，总统设法用备用手段记录了一份视频声明",
                            },
                            "unlock": ["fs_ultimatum", "fs_secret_recording"],
                        },
                        {
                            "id": "let_them_in",
                            "label": "允许进入",
                            "description": "按规程核实证件后放行——他们是克格勃的人，理论上有权进入",
                            "effects": {
                                "set_var": [
                                    {"var_id": "player.忠诚度", "op": "add", "value": -10},
                                    {"var_id": "player.心理压力", "op": "add", "value": 15},
                                ],
                                "inject_prompt": "你检查了授权文件后放行。博尔金一行人径直走向总统书房，脚步声在大理石走廊中回荡如丧钟",
                            },
                            "unlock": ["fs_ultimatum"],
                        },
                    ],
                    "related_npcs": ["yury_plekhanov", "valery_boldin"],
                },
                {
                    "id": "fs_secret_recording",
                    "name": "秘密录像",
                    "description": "协助总统用家用摄像机录制一份否认自愿放权的声明",
                    "type": "quest",
                    "completion_keywords": ["录像", "声明", "摄像", "记录"],
                    "position": {"x": 1, "y": 2},
                    "effects": {
                        "inject_prompt": "你帮助戈尔巴乔夫用一台家用索尼摄像机录制了声明：'我是苏联总统，我没有放弃权力，所谓紧急状态委员会是违宪的。'这段录像被藏在别墅花园的石墙缝隙中",
                        "set_var": [{"var_id": "player.洞察力", "op": "add", "value": 10}],
                    },
                    "related_npcs": ["mikhail_gorbachev"],
                },
                {
                    "id": "fs_ultimatum",
                    "name": "最后通牒",
                    "description": "特使团向戈尔巴乔夫提出最后通牒：签署紧急状态令或被废黜",
                    "type": "choice",
                    "position": {"x": 0, "y": 3},
                    "choices": [
                        {
                            "id": "protect_president",
                            "label": "挡在总统面前",
                            "description": "以武力威胁阻止特使接近总统",
                            "effects": {
                                "set_var": [
                                    {"var_id": "player.体能", "op": "add", "value": -15},
                                    {"var_id": "player.忠诚度", "op": "add", "value": 15},
                                ],
                                "inject_prompt": "你拔出手枪挡在总统书房门前。博尔金愣住了——他没想到一个卫士敢对克格勃举枪。紧张的对峙后，特使们退让了，但他们的通讯兵已经发出了封锁信号",
                            },
                        },
                        {
                            "id": "witness_silently",
                            "label": "沉默见证",
                            "description": "站在一旁，作为历史的沉默见证者",
                            "effects": {
                                "set_var": [{"var_id": "player.心理压力", "op": "add", "value": 20}],
                                "inject_prompt": "你看着戈尔巴乔夫拒绝签字，特使们恼羞成怒地离去。总统对你说：'记住今天的一切。'他的声音比你想象的更平静",
                            },
                        },
                    ],
                    "related_npcs": ["valery_boldin", "mikhail_gorbachev"],
                },
            ],
        })
        print("[1c] story_tree: 添加'福罗斯围城'（5 节点）")

    data["story_tree"] = story_tree

    # ═══════════════════════════════════════════════════════════════════
    # 2. ONE_TIME_EVENTS 补充玩家选择
    # ═══════════════════════════════════════════════════════════════════
    ot_by_id = {e["id"]: e for e in data["one_time_events"]}

    # 给关键 one_time_events 添加 player_choices 字段
    # 这些选择会被 prompt_builder 注入到叙事 prompt 中
    EVENT_CHOICES = {
        "emergency_committee_formation": {
            "player_choices": [
                {
                    "id": "ec_leak_info",
                    "text": "设法将紧急委员会成立的消息泄露给外部",
                    "condition": "player.洞察力 >= 60",
                    "state_changes": [
                        {"target": "world.yeltsin_awareness", "op": "add", "value": 5},
                        {"target": "player.心理压力", "op": "add", "value": 10},
                    ],
                },
                {
                    "id": "ec_monitor_closely",
                    "text": "加强对委员会成员通讯的监听，记录一切",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                    ],
                },
                {
                    "id": "ec_pledge_loyalty",
                    "text": "主动向上级表态支持紧急委员会",
                    "state_changes": [
                        {"target": "player.忠诚度", "op": "add", "value": 15},
                    ],
                },
            ],
        },
        "tanks_roll_into_moscow": {
            "player_choices": [
                {
                    "id": "tr_warn_civilians",
                    "text": "冒险向沿途居民示意远离主要道路",
                    "state_changes": [
                        {"target": "player.忠诚度", "op": "add", "value": -5},
                        {"target": "world.public_unease", "op": "add", "value": 5},
                    ],
                },
                {
                    "id": "tr_document",
                    "text": "用随身相机秘密拍摄军队调动的证据",
                    "condition": "player.洞察力 >= 50",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 8},
                        {"target": "player.洞察力", "op": "add", "value": 5},
                    ],
                },
                {
                    "id": "tr_follow_orders",
                    "text": "严格执行调动命令，不做任何多余举动",
                    "state_changes": [
                        {"target": "player.忠诚度", "op": "add", "value": 5},
                    ],
                },
            ],
        },
        "yeltsin_white_house_speech": {
            "player_choices": [
                {
                    "id": "yw_join_crowd",
                    "text": "混入白宫前的人群，亲耳聆听叶利钦的演说",
                    "state_changes": [
                        {"target": "player.忠诚度", "op": "add", "value": -10},
                        {"target": "player.洞察力", "op": "add", "value": 10},
                    ],
                },
                {
                    "id": "yw_report_speech",
                    "text": "将演说内容逐字记录并向上级汇报",
                    "state_changes": [
                        {"target": "player.忠诚度", "op": "add", "value": 5},
                        {"target": "player.洞察力", "op": "add", "value": 5},
                    ],
                },
                {
                    "id": "yw_spread_word",
                    "text": "通过私人渠道将演说内容传播给更多人",
                    "condition": "player.忠诚度 < 50",
                    "state_changes": [
                        {"target": "world.yeltsin_awareness", "op": "add", "value": 5},
                        {"target": "world.public_unease", "op": "add", "value": 5},
                    ],
                },
            ],
        },
        "yanaev_press_conference": {
            "player_choices": [
                {
                    "id": "yp_notice_hands",
                    "text": "注意到亚纳耶夫颤抖的双手——他连自己都说服不了",
                    "condition": "player.洞察力 >= 60",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                    ],
                },
                {
                    "id": "yp_report_weakness",
                    "text": "将发布会的混乱细节报告给体制外的联系人",
                    "condition": "player.忠诚度 < 40",
                    "state_changes": [
                        {"target": "world.yeltsin_awareness", "op": "add", "value": 3},
                    ],
                },
            ],
        },
        "whitehouse_assault_attempt": {
            "player_choices": [
                {
                    "id": "wa_defend",
                    "text": "加入白宫的防御者行列",
                    "condition": "player.忠诚度 < 40",
                    "state_changes": [
                        {"target": "player.体能", "op": "add", "value": -20},
                        {"target": "player.心理压力", "op": "add", "value": 20},
                        {"target": "player.忠诚度", "op": "add", "value": -20},
                    ],
                },
                {
                    "id": "wa_sabotage",
                    "text": "从内部破坏进攻方的通讯线路",
                    "condition": "player.洞察力 >= 70",
                    "state_changes": [
                        {"target": "world.coup_preparedness", "op": "add", "value": -5},
                        {"target": "player.心理压力", "op": "add", "value": 15},
                    ],
                },
                {
                    "id": "wa_evacuate",
                    "text": "帮助平民撤离危险区域",
                    "state_changes": [
                        {"target": "player.体能", "op": "add", "value": -10},
                    ],
                },
                {
                    "id": "wa_stand_watch",
                    "text": "在远处观察，记录事件的每一个细节",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                        {"target": "player.心理压力", "op": "add", "value": 10},
                    ],
                },
            ],
        },
    }
    count_ch = 0
    for eid, patch in EVENT_CHOICES.items():
        if eid in ot_by_id:
            ot_by_id[eid]["player_choices"] = patch["player_choices"]
            count_ch += 1
    print(f"[2] one_time_events: 为 {count_ch} 个事件添加玩家选择")

    # ═══════════════════════════════════════════════════════════════════
    # 3. 补充空白时间段的 ONE_TIME_EVENTS（8/16-8/17 几乎空白）
    # ═══════════════════════════════════════════════════════════════════
    NEW_EVENTS = [
        # ── 8月16日 ──
        {
            "id": "chebrikov_encrypted_warning",
            "description": "切布里科夫中校在午间换班时递给你一张折叠的纸条，上面用铅笔写着一行小字：'注意第5频道，今晚22:00后的通话。'他的表情异常严肃。这位上司从不做无谓的事——你意识到他可能知道些什么。",
            "trigger_time": "1991-08-16T12:30",
            "condition": "",
            "state_changes": [
                {"target": "player.洞察力", "op": "add", "value": 5},
            ],
            "player_choices": [
                {
                    "id": "cw_follow_hint",
                    "text": "按照提示，在22:00后密切监听第5频道",
                    "state_changes": [{"target": "player.洞察力", "op": "add", "value": 10}],
                },
                {
                    "id": "cw_ask_chebrikov",
                    "text": "私下找切布里科夫询问详情",
                    "state_changes": [{"target": "player.心理压力", "op": "add", "value": 5}],
                },
                {
                    "id": "cw_ignore",
                    "text": "忽略纸条，按常规执勤",
                    "state_changes": [],
                },
            ],
        },
        {
            "id": "lubyanka_canteen_rumors",
            "description": "食堂里几个老资格的克格勃军官在角落低声交谈。你端着餐盘经过时，听到了碎片般的词句：'八月二十号之前……''联盟条约不能签……''亚佐夫已经点头了……'他们看到你后立即闭嘴。",
            "trigger_time": "1991-08-16T13:00",
            "condition": "",
            "state_changes": [
                {"target": "player.心理压力", "op": "add", "value": 5},
                {"target": "player.洞察力", "op": "add", "value": 3},
            ],
        },
        {
            "id": "mysterious_motorcade_0816",
            "description": "傍晚时分，你在卢比扬卡大楼的窗口看到一队没有车牌的黑色伏尔加轿车驶出地下车库，由两辆军用吉普护送。车队方向是通往伏努科沃机场的公路——有人正在连夜转移什么。",
            "trigger_time": "1991-08-16T18:30",
            "condition": "",
            "state_changes": [
                {"target": "player.洞察力", "op": "add", "value": 3},
                {"target": "world.coup_preparedness", "op": "add", "value": 2},
            ],
        },
        {
            "id": "channel5_interception_0816",
            "description": "深夜，第5频道传来一段不寻常的加密通话。虽然你无法完全破解，但通过频率特征判断，通话双方分别位于卢比扬卡和国防部——克留奇科夫正在与亚佐夫直接通话。通话中反复出现'罐头'和'雷霆'两个代号。",
            "trigger_time": "1991-08-16T22:30",
            "condition": "",
            "state_changes": [
                {"target": "player.洞察力", "op": "add", "value": 8},
                {"target": "player.心理压力", "op": "add", "value": 5},
                {"target": "world.coup_preparedness", "op": "add", "value": 3},
            ],
            "player_choices": [
                {
                    "id": "c5_record",
                    "text": "偷偷启动备份磁带录音",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                        {"target": "player.心理压力", "op": "add", "value": 8},
                    ],
                },
                {
                    "id": "c5_decode",
                    "text": "尝试用已知密码本交叉比对部分内容",
                    "condition": "player.洞察力 >= 65",
                    "state_changes": [{"target": "player.洞察力", "op": "add", "value": 12}],
                },
                {
                    "id": "c5_log_normal",
                    "text": "将通话记录在正式日志中标注为'技术测试'",
                    "state_changes": [{"target": "player.忠诚度", "op": "add", "value": 5}],
                },
            ],
        },
        # ── 8月17日 ──
        {
            "id": "saturday_abnormal_activity",
            "description": "周六本该是安静的日子，但卢比扬卡大楼异常繁忙。高级军官和政府官员的专车不断出入，地下车库传来沉闷的发动机声。档案室的碎纸机从早上开始就没停过——有人在大规模销毁文件。",
            "trigger_time": "1991-08-17T09:00",
            "condition": "",
            "state_changes": [
                {"target": "player.心理压力", "op": "add", "value": 8},
                {"target": "world.coup_preparedness", "op": "add", "value": 3},
            ],
            "player_choices": [
                {
                    "id": "sa_salvage",
                    "text": "从碎纸机旁抢救几份未完全销毁的文件碎片",
                    "condition": "player.洞察力 >= 55",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 8},
                        {"target": "player.心理压力", "op": "add", "value": 10},
                    ],
                },
                {
                    "id": "sa_observe",
                    "text": "记录进出大楼的车辆和人员",
                    "state_changes": [{"target": "player.洞察力", "op": "add", "value": 5}],
                },
                {
                    "id": "sa_stay_invisible",
                    "text": "躲在监听室里不出去，尽量不引人注意",
                    "state_changes": [{"target": "player.忠诚度", "op": "add", "value": 3}],
                },
            ],
        },
        {
            "id": "boldin_kremlin_meeting_0817",
            "description": "监听记录显示博尔金在克里姆林宫召集了一次紧急的小范围会议，与会者包括国防部长亚佐夫和内务部长普戈。会议内容不详，但结束后，总参谋部连续发出了三份标注'绝密/闪电'的电报。",
            "trigger_time": "1991-08-17T14:00",
            "condition": "",
            "state_changes": [
                {"target": "world.coup_preparedness", "op": "add", "value": 5},
                {"target": "player.洞察力", "op": "add", "value": 5},
            ],
        },
        {
            "id": "sevastopol_signal_test_0817",
            "description": "塞瓦斯托波尔通讯中继站报告进行'例行信号衰减测试'，但测试持续时间异常——长达45分钟。在此期间，福罗斯别墅的外线通讯质量明显下降。这是政变通讯切断行动的预演。",
            "trigger_time": "1991-08-17T16:00",
            "condition": "world.coup_preparedness >= 75",
            "state_changes": [
                {"target": "world.gorbachev_isolation", "op": "add", "value": 5},
            ],
        },
        {
            "id": "kryuchkov_arizona_0817",
            "description": "克留奇科夫经由加密通讯从'亚利桑那'安全屋发出最终指令：所有参与者确认就位，'罐头'方案进入不可逆执行阶段。明天一早，特使团将飞往克里米亚。历史的齿轮已经咬合，再也无法回转。",
            "trigger_time": "1991-08-17T23:00",
            "condition": "world.coup_preparedness >= 80",
            "state_changes": [
                {"target": "world.coup_preparedness", "op": "add", "value": 5},
                {"target": "player.心理压力", "op": "add", "value": 10},
            ],
            "player_choices": [
                {
                    "id": "ka_last_chance",
                    "text": "这是最后的窗口——尝试将消息传递出去",
                    "condition": "player.忠诚度 < 50",
                    "state_changes": [
                        {"target": "world.yeltsin_awareness", "op": "add", "value": 5},
                        {"target": "player.心理压力", "op": "add", "value": 15},
                    ],
                },
                {
                    "id": "ka_prepare",
                    "text": "开始为自己准备后路——复制一份关键文件作为保险",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                        {"target": "player.心理压力", "op": "add", "value": 8},
                    ],
                },
                {
                    "id": "ka_sleep",
                    "text": "告诉自己这不是你能改变的事，试图入睡",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 12},
                    ],
                },
            ],
        },
        # ── 8月18日 补充 ──
        {
            "id": "foros_flight_departure",
            "description": "清晨五点，伏努科沃军用机场。一架图-134专机在晨雾中启动引擎——博尔金、巴克拉诺夫、谢宁和两名克格勃将军登机。飞行计划显示目的地是辛菲罗波尔——那是距离福罗斯别墅最近的军用机场。倒计时开始了。",
            "trigger_time": "1991-08-18T05:00",
            "condition": "world.coup_preparedness >= 85",
            "state_changes": [
                {"target": "world.coup_preparedness", "op": "add", "value": 3},
            ],
        },
        # ── 8月19日 补充 ──
        {
            "id": "three_killed_at_tunnel",
            "description": "深夜，白宫附近的花园环路隧道传来枪声和碾压声。三名年轻人——科马尔、乌索夫和克里奇夫斯基——在试图阻挡装甲车时遇难。他们是这场政变中唯一的死难者。消息迅速在守卫者中传开，哀恸与愤怒像野火一样蔓延。",
            "trigger_time": "1991-08-21T00:30",
            "condition": "",
            "state_changes": [
                {"target": "world.public_unease", "op": "add", "value": 15},
                {"target": "world.vdv_loyalty", "op": "add", "value": -10},
            ],
            "player_choices": [
                {
                    "id": "tk_mourn",
                    "text": "赶到现场，在血迹旁默默脱帽致哀",
                    "state_changes": [
                        {"target": "player.心理压力", "op": "add", "value": 15},
                        {"target": "player.忠诚度", "op": "add", "value": -10},
                    ],
                },
                {
                    "id": "tk_report",
                    "text": "将死亡事件作为关键情报上报",
                    "state_changes": [
                        {"target": "player.洞察力", "op": "add", "value": 5},
                    ],
                },
            ],
        },
        # ── 8月21日 补充 ──
        {
            "id": "yazov_breaks",
            "description": "国防部长亚佐夫在办公室独坐了整整一个小时后，拿起红色专线电话命令撤回所有进入莫斯科的部队。据他的副官回忆，元帅放下电话后说了一句：'我不会让军队向人民开枪。'这位参加过斯大林格勒战役的老兵，在最后时刻选择了良知。",
            "trigger_time": "1991-08-21T14:00",
            "condition": "",
            "state_changes": [
                {"target": "world.coup_preparedness", "op": "add", "value": -20},
                {"target": "world.vdv_loyalty", "op": "add", "value": -15},
            ],
        },
        {
            "id": "kryuchkov_last_call",
            "description": "克留奇科夫从安全屋拨出最后一个电话——打给亚纳耶夫，要求'坚持到底'。但亚纳耶夫已经喝得语无伦次，电话那头传来的除了含混的呓语什么都没有。克格勃主席缓缓挂上电话，对身边的人说：'完了。'",
            "trigger_time": "1991-08-21T15:00",
            "condition": "",
            "state_changes": [
                {"target": "world.coup_preparedness", "op": "add", "value": -15},
            ],
        },
    ]

    existing_ot_ids = {e["id"] for e in data["one_time_events"]}
    count_new = 0
    for evt in NEW_EVENTS:
        if evt["id"] not in existing_ot_ids:
            data["one_time_events"].append(evt)
            count_new += 1
    print(f"[3] one_time_events: 新增 {count_new} 个事件（填充空白时间线）")

    # ═══════════════════════════════════════════════════════════════════
    # 4. DYNAMIC_EVENTS — 条件触发的动态事件
    # ═══════════════════════════════════════════════════════════════════
    dynamic_events = data.get("dynamic_events", [])
    existing_de_ids = {e.get("id", "") for e in dynamic_events}

    NEW_DYNAMIC = [
        {
            "id": "de_stress_breakdown",
            "name": "精神崩溃边缘",
            "description": "连续的高压和道德困境使你的精神状态濒临崩溃，手开始不由自主地颤抖",
            "conditions": [
                {"path": "player.心理压力", "op": ">=", "value": 80},
            ],
            "cooldown": 8,
            "weight": 15,
            "effects": [
                {"type": "narrative_callback", "text": "玩家精神压力过大，出现幻听、手抖、失眠等症状。可能在关键时刻做出非理性行为", "priority": "high"},
                {"type": "state_change", "target": "player.洞察力", "op": "add", "value": -10},
            ],
        },
        {
            "id": "de_loyalty_questioned",
            "name": "忠诚审查",
            "description": "你的异常行为引起了政治军官的注意，一次非正式的'谈话'正在等着你",
            "conditions": [
                {"path": "player.忠诚度", "op": "<", "value": 30},
            ],
            "cooldown": 10,
            "weight": 20,
            "effects": [
                {"type": "narrative_callback", "text": "政治军官找玩家'谈话'，质疑其近期行为。玩家需要在对话中自证忠诚，否则可能被限制行动自由", "priority": "high"},
                {"type": "state_change", "target": "player.心理压力", "op": "add", "value": 15},
            ],
        },
        {
            "id": "de_informant_approach",
            "name": "线人主动接触",
            "description": "一个自称认识玛丽娜的陌生人在阿尔巴特街拦住你，低声说出了一个只有内部人才知道的代号",
            "conditions": [
                {"path": "player.忠诚度", "op": "<", "value": 50},
                {"path": "player.洞察力", "op": ">=", "value": 60},
            ],
            "cooldown": 15,
            "weight": 10,
            "effects": [
                {"type": "narrative_callback", "text": "一位地下网络的联络人主动接触玩家，提供了一条关键情报交换请求", "priority": "medium"},
            ],
        },
        {
            "id": "de_power_outage_lubyanka",
            "name": "卢比扬卡停电",
            "description": "大楼的备用发电机突然启动——主电网不知何故中断了供电。在昏暗的应急灯下，每个人的表情都显得格外阴森",
            "conditions": [
                {"path": "world.coup_preparedness", "op": ">=", "value": 85},
            ],
            "cooldown": 20,
            "weight": 8,
            "effects": [
                {"type": "narrative_callback", "text": "卢比扬卡大楼短暂停电，在混乱中可能出现行动窗口——档案室门禁短暂失效，监控摄像头离线", "priority": "medium"},
            ],
        },
        {
            "id": "de_old_colleague_warning",
            "name": "老同事的暗示",
            "description": "一位即将退休的老同事在走廊里拍拍你的肩膀，低声说：'年轻人，这几天小心。留条后路。'",
            "conditions": [
                {"path": "world.coup_preparedness", "op": ">=", "value": 75},
                {"path": "world.coup_preparedness", "op": "<", "value": 95},
            ],
            "cooldown": 30,
            "weight": 12,
            "effects": [
                {"type": "narrative_callback", "text": "一位经验丰富的老克格勃向玩家暗示局势将有剧变，建议做好准备。这种人不会无缘无故说这种话", "priority": "medium"},
                {"type": "state_change", "target": "player.洞察力", "op": "add", "value": 3},
            ],
        },
        {
            "id": "de_high_awareness_contact",
            "name": "叶利钦阵营来电",
            "description": "一通来自白宫的加密电话意外接入了你的监听频道——有人在试图联络克格勃内部的同情者",
            "conditions": [
                {"path": "world.yeltsin_awareness", "op": ">=", "value": 50},
                {"path": "player.忠诚度", "op": "<", "value": 45},
            ],
            "cooldown": 15,
            "weight": 10,
            "effects": [
                {"type": "narrative_callback", "text": "叶利钦阵营的人通过加密频道联络玩家，提出合作请求——用内部情报换取政变后的安全保障", "priority": "high"},
            ],
        },
    ]
    count_de = 0
    for de in NEW_DYNAMIC:
        if de["id"] not in existing_de_ids:
            dynamic_events.append(de)
            count_de += 1
    data["dynamic_events"] = dynamic_events
    print(f"[4] dynamic_events: 新增 {count_de} 个条件触发动态事件")

    # ═══════════════════════════════════════════════════════════════════
    # 5. QUEST_TEMPLATES — 可重复触发的任务模板
    # ═══════════════════════════════════════════════════════════════════
    quest_templates = data.get("quest_templates", [])
    existing_qt_ids = {q.get("id", "") for q in quest_templates}

    NEW_QUESTS = [
        {
            "id": "qt_intercept_cipher",
            "name": "截获加密通讯",
            "description": "监听到一段使用未知密码的通讯，尝试破解其内容",
            "condition": "player.location == 'lubyanka_hq' and player.洞察力 >= 50",
            "cooldown": 5,
            "completion_keywords": ["破解", "密码", "解密", "内容"],
            "rewards": {
                "state_changes": [
                    {"target": "player.洞察力", "op": "add", "value": 8},
                ],
                "narrative_callback": "玩家成功破解了一段加密通讯，获得了有价值的情报碎片",
            },
        },
        {
            "id": "qt_dead_drop_delivery",
            "name": "死信箱投递",
            "description": "将收集到的情报通过死信箱传递给地下网络",
            "condition": "player.忠诚度 < 50 and player.洞察力 >= 40",
            "cooldown": 4,
            "completion_keywords": ["投递", "死信箱", "传递", "信息"],
            "rewards": {
                "state_changes": [
                    {"target": "world.yeltsin_awareness", "op": "add", "value": 3},
                    {"target": "player.心理压力", "op": "add", "value": 5},
                ],
                "narrative_callback": "玩家通过死信箱成功传递了一份情报",
            },
        },
        {
            "id": "qt_calm_soldiers",
            "name": "安抚士兵",
            "description": "部队中弥漫着不安情绪，需要稳定军心",
            "condition": "world.vdv_loyalty <= 40 and world.coup_preparedness >= 90",
            "cooldown": 6,
            "completion_keywords": ["安抚", "稳定", "军心", "士兵"],
            "rewards": {
                "state_changes": [
                    {"target": "world.vdv_loyalty", "op": "add", "value": 5},
                ],
            },
        },
    ]
    count_qt = 0
    for qt in NEW_QUESTS:
        if qt["id"] not in existing_qt_ids:
            quest_templates.append(qt)
            count_qt += 1
    data["quest_templates"] = quest_templates
    print(f"[5] quest_templates: 新增 {count_qt} 个任务模板")

    # ═══════════════════════════════════════════════════════════════════
    # 6. 补充 lorebook 条目（配合剧情树引用）
    # ═══════════════════════════════════════════════════════════════════
    lore_by_id = {l["id"]: l for l in data.get("lorebook", [])}
    NEW_LORE = [
        {
            "id": "lore_echo91",
            "title": "回声-91行动",
            "content": "代号'回声-91'，由克格勃第12局局长克留奇科夫亲自授权的秘密监听行动。对象为克里姆林宫全部政府专线，包括总统与各共和国领导人的通话。表面上是反间谍行动，实际是政变准备工作的核心情报收集环节——通过监听政府高层通讯，掌握潜在反对者的动向和新联盟条约的谈判细节。",
            "keywords": ["回声-91", "回声", "echo", "监听行动", "旁路监听"],
            "active": False,
        },
        {
            "id": "lore_canned_goods",
            "title": "'罐头'方案",
            "content": "代号'罐头'（Консервы），由克留奇科夫在亚利桑那安全屋主持制定的政变总体方案。主要内容包括：1）派特使团赴福罗斯向戈尔巴乔夫摊牌；2）切断福罗斯别墅全部对外通讯；3）成立国家紧急状态委员会接管权力；4）调动塔曼师和坎捷米罗夫卡师控制莫斯科；5）逮捕叶利钦等反对派领袖；6）接管媒体和通讯。方案以精确的时间表执行，但未能预见军队的大规模抗命。",
            "keywords": ["罐头", "方案", "罐头方案", "консервы", "政变计划"],
            "active": False,
        },
        {
            "id": "lore_thunder_protocol",
            "title": "'雷霆'通讯协议",
            "content": "政变期间使用的军事级加密通讯协议，连接克留奇科夫、亚佐夫、普戈三人的专用加密线路。使用一次性密码本和跳频技术，理论上无法被常规监听手段截获——但'回声-91'行动的监听站恰好覆盖了其中一个中继节点。",
            "keywords": ["雷霆", "雷霆协议", "加密通讯", "跳频"],
            "active": False,
        },
    ]
    count_lore = 0
    for lore in NEW_LORE:
        if lore["id"] not in lore_by_id:
            data["lorebook"].append(lore)
            count_lore += 1
    print(f"[6] lorebook: 新增 {count_lore} 个条目（配合剧情树）")

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
