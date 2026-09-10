# 酒馆 — AI 互动文字游戏引擎

一个基于 AI 大语言模型的互动文字冒险游戏引擎。通过 JSON 格式的剧本定义世界观、角色、事件和规则，由 AI 扮演游戏主持人（GM），驱动叙事并与玩家实时互动。

---

## 给测试小白的快速上手指南

只需 3 步，不需要任何编程知识。

### 第 1 步：安装 Python

1. 打开 https://www.python.org/downloads/
2. 点击黄色大按钮 **Download Python 3.x.x** 下载
3. 运行安装程序，**一定要勾选底部的 `Add Python to PATH`**，然后点 Install Now
4. 等待安装完成，关闭安装程序

### 第 2 步：双击启动

打开酒馆文件夹，双击 **`start.bat`**

首次启动会自动：
- 创建运行环境（约 30 秒）
- 下载安装所需组件（约 1-3 分钟，取决于网速）
- 启动服务并自动打开浏览器

> 如果遇到 Windows 安全提示"Windows 已保护你的电脑"，点击 **更多信息** → **仍要运行**

### 第 3 步：配置 AI

浏览器打开后，你会看到游戏主页。在开始游戏前需要先配置 AI：

1. 点击页面上的 **设置**（齿轮图标）
2. 点击 **添加配置**
3. 选择一个预设（推荐选 **DeepSeek** 或 **OpenRouter**）
4. 填入你的 API Key（需要自行注册获取）
5. 点击 **测试连接**，显示成功后点 **激活**

配置好 AI 后就可以开始游戏了！你可以：
- 使用已有剧本直接开始游戏
- 在剧本编辑器中用 AI 辅助创建新剧本

### 常见问题

| 问题 | 解决办法 |
|------|----------|
| 双击 start.bat 闪退 | 右键 start.bat → 用管理员身份运行 |
| 提示"未检测到 Python" | 重新安装 Python，确保勾选了 Add to PATH；安装后重启电脑 |
| pip 安装依赖很慢 | 国内网络可能较慢，耐心等待；或使用科学上网 |
| 浏览器没有自动打开 | 手动打开浏览器，访问 http://127.0.0.1:8000 |
| 关闭游戏 | 直接关闭 start.bat 的黑色命令窗口即可 |
| 再次启动 | 再次双击 start.bat（第二次会很快，不需要重新安装） |

---

## 功能特性

### 核心游戏引擎
- **AI 驱动叙事** — AI 扮演 GM，根据剧本设定生成叙述、对话和选项
- **世界树分支历史** — 完整的树状游戏历史，支持回溯、分支探索和撤销
- **Swipe / 重新生成** — 对 AI 回复不满意可重新生成或左右切换备选版本
- **流式输出** — SSE 流式响应，实现打字机效果的实时输出
- **历史摘要压缩** — 自动分层压缩历史记录，节省 Token 消耗

### 剧本系统
- **JSON 剧本格式** — 结构化定义世界观、NPC、地点、事件、属性等
- **AI 辅助生成** — 通过分步向导让 AI 协助创建完整剧本
- **固定开局选项** — 支持预设开局选择，含确定性和条件性结果
- **剧本验证** — 自动检查剧本结构完整性和交叉引用一致性
- **剧本变量与宏** — 支持 `{{var::name}}` 格式的变量宏替换
- **正则脚本** — 对 AI 输出或玩家输入进行后处理的正则规则
- **触发器** — 基于游戏事件的生命周期钩子，自动执行动作

### 故事树 (Story Tree)
- **分支叙事线** — 类似国策树的多线程叙事结构，支持解锁、前置依赖
- **多节点类型** — auto / timed / choice / quest 四种节点类型
- **选择后果** — choice 节点提供带数值影响、inject_prompt、activate_lore 的分支选项
- **知识图谱联动** — 故事树节点可自动激活 Lorebook 条目

### 骰子系统
- **可配置骰子** — 支持 NdM+K 格式（如 1d100、2d6+3）
- **结果区间** — 骰子结果自动映射到预定义的区间标签和状态变化
- **D100 检定** — 支持大于/小于/等于多种比较方式的百分骰检定
- **持续效果 & 冷却** — 骰子结果可附带持续时间和冷却机制
- **玩家可切换** — 骰子系统可由玩家在游戏中开关

### 状态管理
- **点分路径导航** — 使用 `player.attributes.health` 格式访问嵌套状态
- **智能路径解析** — `player.mood` 自动解析到 `player.attributes.mood`
- **状态变更** — 支持 set / add / remove / append / toggle 等操作
- **过期机制** — 持续状态可设置过期时间自动移除

### NPC 系统
- **三维关系模型** — 信任（trust）/ 好感（affection）/ 畏惧（fear）三轴关系
- **独立对话** — 可与单个 NPC 进行独立对话交互
- **离屏模拟** — NPC 在玩家不在场时也会有自己的活动
- **动态注册** — AI 可在游戏中动态创建新 NPC
- **关系网络** — NPC 之间也存在相互关系，形成完整的社交网络
- **组织归属** — NPC 可隶属于组织，拥有职级 (hierarchy) 体系

### 组织系统
- **组织定义** — 定义组织的 hierarchy（等级结构）、standing（组织声望）
- **NPC-组织关系** — NPC 可在组织中担任特定职级
- **组织间关系** — 组织之间可设置关系（同盟、敌对等）

### 事件系统
- **统一事件引擎** — 整合故事树、事件调度器和状态管理的统一引擎
- **周期事件** — 按天/周/月/小时/分钟的频率循环触发，支持条件和过期
- **一次性事件** — 在指定时间点触发的剧情节点，支持条件门控
- **元事件总线** — 回合后自动评估和分发事件钩子
- **随机项** — 条件触发或始终生效的随机事件，与骰子系统联动

### 世界书 / Lorebook
- **关键词激活** — 当关键词出现在近期对话中时自动注入相关设定
- **主/副关键词** — 支持 AND 逻辑的双层关键词匹配
- **递归激活** — 已激活条目可触发关联条目（知识图谱边）
- **注入位置控制** — 可配置在系统提示的不同位置注入
- **自动生成** — 从 NPC 和关系数据自动生成 Lorebook 条目

### 职业与技能系统
- **职业注册表** — 支持 D&D 5e 和 CoC 职业/技能体系
- **技能解锁** — 游戏中可解锁新技能
- **商店系统** — 基于地点的道具买卖

### 数据银行 (Data Bank)
- **文档上传** — 上传参考文档用于游戏内容补充
- **向量检索** — 基于语义相似度的文档片段检索
- **网页抓取** — 支持 URL 内容抓取并存入数据银行

### 向量记忆 (Vector Memory)
- **长期记忆** — 基于 FastEmbed + ChromaDB 的向量记忆系统
- **可选依赖** — 未安装时自动降级为无操作模式
- **独立安装** — `install_vector_memory.bat` 一键安装

### 素材与搜索
- **网页搜索** — 集成 DuckDuckGo 搜索获取参考素材
- **网页抓取** — 支持 URL 内容抓取和提取
- **素材管理** — 保存、标签分类、检索搜索结果
- **素材注入** — 将素材内容注入到剧本的指定位置
- **AI 变换** — 对素材进行 AI 驱动的内容转换

### 多 AI 提供商
- **OpenAI** — 官方 API（GPT-4o 等）
- **Claude (Anthropic)** — Anthropic 官方 API
- **Ollama** — 本地模型（Llama 3 等）
- **OpenAI 兼容** — 支持任何 OpenAI 兼容接口
- **预设配置** — 内置 Xi-AI、DeepSeek、Moonshot、智谱 GLM、SiliconFlow、OpenRouter 等预设
- **多配置管理** — 可保存多个 AI 配置并随时切换
- **连接测试** — 一键测试 AI 服务连通性

## 项目结构

```
酒馆/
├── app.py                      # FastAPI 应用入口
├── config.py                   # 应用配置（路径、端口、认证）
├── requirements.txt            # Python 依赖
├── requirements-vector.txt     # 向量记忆可选依赖
├── start.bat                   # Windows 一键启动
├── install_vector_memory.bat   # 向量记忆安装脚本
├── ai/                         # AI 提供商
│   ├── base.py                 # 抽象基类、重试装饰器、think 标签处理
│   ├── openai_provider.py      # OpenAI / OpenAI 兼容提供商
│   ├── claude_provider.py      # Anthropic Claude 提供商
│   ├── ollama_provider.py      # Ollama 本地模型提供商
│   └── response_parser.py      # AI 响应解析（JSON 提取与容错修复）
├── api/                        # API 路由
│   ├── game_routes.py          # 游戏流程 API
│   ├── script_routes.py        # 剧本管理 API
│   ├── save_routes.py          # 存档管理 API
│   ├── config_routes.py        # AI 配置 API
│   ├── search_routes.py        # 搜索与素材 API
│   └── databank_routes.py      # 数据银行 API
├── db/                         # 数据库
│   ├── database.py             # SQLite 异步连接池
│   └── models.py               # Pydantic 数据模型
├── engine/                     # 游戏引擎核心
│   ├── game_session.py         # 游戏会话（核心协调器）
│   ├── prompt_builder.py       # AI 提示词构建
│   ├── script_loader.py        # 剧本加载与验证
│   ├── world_tree.py           # 世界树（分支历史）
│   ├── state_manager.py        # 状态管理器
│   ├── dice.py                 # 骰子系统
│   ├── event_scheduler.py      # 事件调度器（时间触发）
│   ├── event_engine.py         # 统一事件引擎
│   ├── meta_events.py          # 元事件总线（回合后钩子）
│   ├── story_tree.py           # 故事树（分支叙事控制）
│   ├── history_summarizer.py   # 历史摘要压缩
│   ├── lorebook.py             # 世界书 / Lorebook
│   ├── script_variables.py     # 剧本变量与宏替换
│   ├── triggers.py             # 触发器引擎
│   ├── regex_scripts.py        # 正则脚本后处理
│   ├── class_system.py         # 职业与技能注册表
│   ├── data_bank.py            # 数据银行（文档上传与检索）
│   └── vector_memory.py        # 向量长期记忆（可选）
├── search/                     # 搜索模块
│   ├── search_engine.py        # 搜索引擎（DuckDuckGo）
│   ├── web_scraper.py          # 网页抓取
│   ├── ai_knowledge.py         # AI 知识处理
│   └── material_manager.py     # 素材管理
├── storage/                    # 存储扩展（预留）
├── static/                     # 前端静态文件
│   ├── index.html
│   ├── css/style.css
│   └── js/
│       ├── app.js              # 主应用逻辑
│       ├── game_ui.js          # 游戏界面
│       ├── script_builder.js   # 剧本编辑器
│       ├── search_ui.js        # 搜索界面
│       ├── world_tree_view.js  # 世界树可视化
│       ├── story_tree_view.js  # 故事树可视化
│       └── story_tree_editor.js # 故事树编辑器
├── scripts/                    # 剧本优化工具脚本
├── experiments/                # 测试与基准实验
├── data/                       # 数据目录（运行时生成）
│   ├── tavern.db               # SQLite 数据库
│   ├── scripts/                # 剧本文件
│   └── saves/                  # 存档文件
└── tests/                      # 测试
    ├── test_dice.py
    ├── test_event_scheduler.py
    ├── test_response_parser.py
    └── test_state_manager.py
```

## 快速开始（开发者）

> 普通用户请直接双击 `start.bat`，以下为手动安装步骤。

### 环境要求

- Python 3.10+

### 安装

```bash
# 克隆项目
git clone <repo-url>
cd 酒馆

# 创建虚拟环境
python -m venv bar
source bar/bin/activate  # Linux/macOS
# 或 bar\Scripts\activate  # Windows

# 安装依赖
pip install -r requirements.txt

# （可选）安装向量记忆依赖
pip install -r requirements-vector.txt
```

### 运行

```bash
python app.py
```

服务默认启动在 `http://127.0.0.1:8000`，打开浏览器即可使用。

也可以直接双击 `start.bat`（Windows），会自动完成环境创建、依赖安装、启动服务并打开浏览器。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `TAVERN_HOST` | `127.0.0.1` | 服务监听地址 |
| `TAVERN_PORT` | `8000` | 服务监听端口 |
| `TAVERN_API_TOKEN` | _(空)_ | API 认证 Token，留空则不启用认证 |

### 配置 AI

启动后在设置页面配置 AI 提供商：

1. 选择一个预设（如 DeepSeek、OpenRouter）或手动配置
2. 填入 API Key
3. 点击"测试连接"验证
4. 激活该配置

## 剧本格式

剧本使用 JSON 格式，包含以下主要字段：

```json
{
  "script_id": "my_script",
  "script_name": "剧本名称",
  "version": "1.0",
  "start_time": "2025-09-01T07:00:00",
  "world_background": "世界观描述...",

  "settings": {
    "dice_check": { "default_enabled": true, "player_can_toggle": true },
    "fixed_opening": { "default_enabled": true, "player_can_toggle": true }
  },

  "opening": {
    "text": "开场白文本...",
    "choices": [
      {
        "id": "open_0",
        "text": "选项描述",
        "result": {
          "type": "deterministic",
          "description": "结果描述",
          "state_changes": [
            { "target": "player.mood", "op": "add", "value": 5 }
          ]
        }
      }
    ]
  },

  "player_character": {
    "id": "player_id",
    "bio": "角色简介",
    "initial_location": "初始位置",
    "long_term_goal": "长期目标",
    "attributes": {
      "health": { "value": 80, "min": 0, "max": 100, "rule": "规则说明" }
    },
    "relationships": {
      "npc_id": { "value": 60, "min": 0, "max": 100, "rule": "规则说明" }
    }
  },

  "player_presets": [
    {
      "id": "preset_id",
      "name": "预设名称",
      "description": "预设描述",
      "attributes": {},
      "initial_location": "location_id"
    }
  ],

  "npcs": [
    {
      "id": "npc_id",
      "name": "NPC名称",
      "bio": "NPC背景",
      "personality": "性格描述",
      "capabilities": "能力描述",
      "attitude_toward_player": 50,
      "organizations": [
        { "org_id": "org_id", "rank": "member" }
      ],
      "related_lore": ["lorebook_id"]
    }
  ],

  "organizations": [
    {
      "id": "org_id",
      "name": "组织名称",
      "description": "组织描述",
      "hierarchy": ["leader", "officer", "member"],
      "standing": { "value": 50 }
    }
  ],

  "locations": [
    {
      "id": "loc_id",
      "name": "地点名称",
      "description": "地点描述",
      "initially_visible": true,
      "connections": ["other_loc_id"]
    }
  ],

  "world_properties": [
    { "id": "prop_id", "name": "属性名", "value": "属性值", "rule": "规则" }
  ],

  "variables": [
    { "id": "var_id", "name": "变量名", "value": 0 }
  ],

  "triggers": [
    {
      "id": "trigger_id",
      "event": "on_turn_end",
      "condition": "player.health < 20",
      "actions": [
        { "type": "notify", "message": "你的生命值过低！" }
      ]
    }
  ],

  "random_items": [
    {
      "id": "item_id",
      "description": "描述",
      "trigger": "触发条件",
      "trigger_type": "conditional",
      "dice": { "count": 1, "faces": 100, "modifier": 0 },
      "ranges": [
        { "min": 1, "max": 50, "label": "结果标签", "state_changes": [] }
      ]
    }
  ],

  "persistent_states": [
    {
      "id": "state_id",
      "description": "持续状态描述",
      "initially_active": false,
      "expires_at": null
    }
  ],

  "cyclic_events": [
    {
      "id": "event_id",
      "description": "事件描述",
      "frequency_value": 1,
      "frequency_unit": "week",
      "first_trigger": "2025-09-04T15:30:00",
      "condition": "status.state_id == 'active'",
      "expires_at": null
    }
  ],

  "one_time_events": [
    {
      "id": "event_id",
      "description": "事件描述",
      "trigger_time": "2025-09-01T08:30:00",
      "condition": "status.state_id == 'active'"
    }
  ],

  "story_tree": {
    "trees": [
      {
        "id": "tree_id",
        "name": "故事线名称",
        "description": "描述",
        "icon": "skull",
        "nodes": [
          {
            "id": "node_id",
            "name": "节点名称",
            "description": "描述",
            "type": "choice",
            "requires": ["prerequisite_node_id"],
            "on_complete_unlock": ["next_node_id"],
            "effects": {
              "inject_prompt": "注入的叙事提示",
              "notify": "通知消息",
              "set_var": [
                { "target": "player.stat", "op": "add", "value": 5 }
              ],
              "activate_lore": ["lorebook_id"],
              "unlock_locations": ["location_id"]
            },
            "choices": [
              {
                "id": "choice_id",
                "label": "选项标签",
                "description": "选项描述",
                "effects": {},
                "unlock": ["next_node_id"]
              }
            ]
          }
        ]
      }
    ]
  },

  "npc_relationships": [
    {
      "from": "npc_id_1",
      "to": "npc_id_2",
      "trust": 60,
      "description": "关系描述"
    }
  ],

  "org_relationships": [
    {
      "from": "org_id_1",
      "to": "org_id_2",
      "type": "allied",
      "description": "关系描述"
    }
  ],

  "lorebook": [
    {
      "id": "lore_id",
      "keys": ["关键词1", "关键词2"],
      "secondary_keys": ["次级关键词"],
      "content": "知识条目内容...",
      "priority": 50,
      "scan_depth": 5,
      "constant": false
    }
  ],

  "regex_scripts": [
    {
      "id": "regex_id",
      "pattern": "正则表达式",
      "replacement": "替换文本",
      "target": "output"
    }
  ]
}
```

可参考 `data/scripts/` 目录下的示例剧本了解完整用法。

## API 参考

所有 API 路由前缀为 `/api`。若设置了 `TAVERN_API_TOKEN`，需在请求头中携带 `Authorization: Bearer <token>`。

### 游戏 `/api/game`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/classes/{system}` | 获取职业列表 |
| GET | `/classes/{system}/{class_id}` | 获取职业详情 |
| GET | `/skills/{system}` | 获取技能列表 |
| GET | `/opening-variants/{script_id}` | 获取开局变体 |
| POST | `/new` | 开始新游戏 |
| POST | `/{save_id}/action` | 提交玩家行动 |
| POST | `/{save_id}/action/stream` | 提交行动（流式响应） |
| GET | `/{save_id}/state` | 获取当前游戏状态 |
| GET | `/{save_id}/tree` | 获取世界树结构 |
| GET | `/{save_id}/tree/{node_id}` | 获取指定节点 |
| POST | `/{save_id}/branch/{node_id}` | 回溯到指定节点 |
| POST | `/{save_id}/toggle-dice` | 切换骰子系统开关 |
| GET | `/{save_id}/history` | 获取游戏历史 |
| GET | `/{save_id}/adventure-log` | 获取冒险日志 |
| GET | `/{save_id}/summary` | 获取历史摘要 |
| POST | `/{save_id}/summary/freeze` | 冻结历史摘要 |
| POST | `/{save_id}/use_item` | 使用道具 |
| POST | `/{save_id}/interact` | 交互操作 |
| POST | `/{save_id}/talk/{npc_id}` | 与 NPC 对话 |
| POST | `/{save_id}/talk/{npc_id}/stream` | 与 NPC 对话（流式响应） |
| GET | `/{save_id}/npc-schedules` | 获取 NPC 日程 |
| POST | `/{save_id}/regenerate` | 重新生成 AI 回复 |
| POST | `/{save_id}/swipe/{direction}` | 切换备选回复（left/right） |
| GET | `/{save_id}/swipes` | 获取备选回复列表 |
| POST | `/{save_id}/swipe/jump` | 跳转到指定备选回复 |
| POST | `/{save_id}/continue` | 继续生成 |
| POST | `/{save_id}/authors-note` | 设置作者注记 |
| POST | `/{save_id}/negative-prompt` | 设置负面提示 |
| POST | `/{save_id}/logit-bias` | 设置 logit 偏置 |
| POST | `/{save_id}/switch-persona` | 切换角色预设 |
| POST | `/{save_id}/npc-dialogue` | NPC 对话交互 |
| POST | `/{save_id}/undo` | 撤销上一回合 |
| GET | `/{save_id}/story-tree` | 获取故事树状态 |
| POST | `/{save_id}/story-tree/choice` | 提交故事树选择 |
| POST | `/{save_id}/deduce` | AI 推理 |
| GET | `/{save_id}/shop/{location_id}` | 获取商店信息 |
| POST | `/{save_id}/shop/buy` | 购买道具 |
| POST | `/{save_id}/shop/sell` | 出售道具 |
| POST | `/{save_id}/unlock-skill` | 解锁技能 |
| POST | `/{save_id}/newspaper` | 获取新闻报纸 |
| POST | `/{save_id}/newspaper/regenerate` | 重新生成报纸 |

### 数据银行 `/api/game`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/{save_id}/databank/upload` | 上传文档 |
| GET | `/{save_id}/databank` | 列出数据银行文件 |
| DELETE | `/{save_id}/databank/{file_id}` | 删除文件 |
| POST | `/{save_id}/databank/scrape` | 抓取网页存入数据银行 |

### 剧本 `/api/scripts`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 列出所有剧本 |
| GET | `/{script_id}` | 获取剧本内容 |
| POST | `/` | 创建剧本 |
| PUT | `/{script_id}` | 更新剧本 |
| DELETE | `/{script_id}` | 删除剧本 |
| GET | `/{script_id}/export` | 导出剧本文件 |
| POST | `/import` | 导入剧本文件 |
| POST | `/{script_id}/validate` | 验证剧本结构 |
| POST | `/ai-generate` | AI 辅助生成完整剧本 |
| POST | `/ai-generate/tab` | AI 辅助生成剧本单个标签页 |
| POST | `/ai-generate/fields` | AI 辅助生成剧本指定字段 |
| POST | `/ai-generate/fields-batch` | AI 批量生成剧本字段 |
| POST | `/ai-generate/optimize-relations` | AI 优化关系网络 |
| POST | `/ai-generate/optimize-events` | AI 优化事件系统 |

### 存档 `/api/saves`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 列出所有存档 |
| GET | `/{save_id}` | 获取存档详情 |
| DELETE | `/{save_id}` | 删除存档 |
| GET | `/{save_id}/export` | 导出存档文件 |
| POST | `/import` | 导入存档文件 |

### AI 配置 `/api/config`

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 获取当前活动配置 |
| PUT | `/` | 更新配置（兼容旧格式） |
| POST | `/test` | 测试当前活动配置 |
| GET | `/profiles` | 列出所有配置 |
| POST | `/profiles` | 创建配置 |
| PUT | `/profiles/{id}` | 更新配置 |
| DELETE | `/profiles/{id}` | 删除配置 |
| POST | `/profiles/{id}/activate` | 激活配置 |
| POST | `/profiles/{id}/test` | 测试指定配置 |
| GET | `/presets` | 获取提供商预设列表 |

### 搜索与素材 `/api/search`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/web` | 网页搜索 |
| POST | `/materials` | 保存素材 |
| GET | `/materials` | 列出素材 |
| GET | `/materials/tags` | 获取所有标签 |
| GET | `/materials/{id}` | 获取素材 |
| DELETE | `/materials/{id}` | 删除素材 |
| POST | `/materials/{id}/tags` | 添加标签 |
| DELETE | `/materials/{id}/tags/{tag}` | 删除标签 |
| POST | `/materials/search` | 搜索素材 |
| POST | `/materials/upload` | 上传素材文件 |
| POST | `/inject` | 注入素材到剧本 |
| POST | `/transform` | AI 变换素材内容 |

## 测试

```bash
pip install pytest
pytest tests/
```

## 技术栈

- **后端**: FastAPI + Uvicorn + aiosqlite (SQLite WAL 模式)
- **前端**: 原生 HTML/CSS/JavaScript
- **AI**: OpenAI SDK / Anthropic SDK / Ollama (httpx)
- **搜索**: DuckDuckGo + BeautifulSoup
- **向量检索**: FastEmbed + ChromaDB（可选）
