"""Game session: core game loop orchestrator."""

import asyncio
import copy
import json
import logging
import random
import re
import time as _time
import uuid
from datetime import datetime, timedelta

from engine.dice import DiceRoller
from engine.script_loader import ScriptLoader
from engine.state_manager import StateManager
from engine.world_tree import WorldTree
from engine.event_scheduler import EventScheduler, parse_time
from engine.event_engine import EventEngine, EventResult, GameEvent
from engine.meta_events import MetaEventBus, MetaEvent, MetaEventTrigger
from engine.lorebook import Lorebook
from engine.prompt_builder import PromptBuilder, _RE_GAME_STATE_BLOCK, _RE_GAME_STATE_OPEN
from engine.history_summarizer import HistorySummarizer
from ai.response_parser import ResponseParser
from ai.base import stream_split_think, strip_think_tags, _THINK_EXTRACT_RE
from engine.vector_memory import VectorMemory, _VECTOR_AVAILABLE
from engine.script_variables import ScriptVariables
from engine.triggers import TriggerEngine
from engine.regex_scripts import RegexScriptEngine
from engine.data_bank import DataBank
from engine.class_system import ClassRegistry
from engine.story_tree import StoryTreeEngine

logger = logging.getLogger(__name__)


# ===== 游戏调优常量 =====
# 离屏 NPC 模拟频率（每 N 回合触发一次）
OFFSCREEN_SIM_INTERVAL = 3
# 玩家风格分析频率（每 N 回合）
PLAY_STYLE_INTERVAL = 5
# NPC 对话场景的聊天历史保留轮数
NPC_CHAT_HISTORY_DEPTH = 6
# NPC 对话历史最大存储轮数（超出后 FIFO 淘汰最旧记录）
NPC_CHAT_HISTORY_MAX = 20
# NPC 对话每轮推进的游戏时间（分钟）
NPC_CHAT_TIME_MINUTES = 5
# O-3: 预编译 CJK 检测正则，避免每回合重新编译
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')
_TOOL_CALL_RE = re.compile(r'\[TOOL_CALL:\s*(\w+)\(([^)]*)\)\]')
_SPEAKER_RE = re.compile(
    r'([\u4e00-\u9fff\u3400-\u4dbf]{2,4}(?:\u5148\u751f|\u5973\u58eb|\u5c0f\u59d0|\u8001\u5e08|\u533b\u751f|\u6559\u6388|\u961f\u957f|\u90e8\u957f|\u5c40\u957f|\u79d8\u4e66|\u7ba1\u7406\u5458|\u4e3b\u4efb|\u8bfe\u957f|\u5904\u957f|\u7ec4\u957f|\u5e08\u5085|\u5927\u4eba|\u9601\u4e0b)?)'
    r'(?:\u8bf4\u9053|\u8bf4|\u558a\u9053|\u4f4e\u58f0\u9053|\u95ee\u9053|\u7b54\u9053|\u53f9\u9053|\u7b11\u9053|\u51b7\u7b11\u9053|\u6012\u9053|\u56de\u7b54|\u5f00\u53e3|\u8865\u5145\u9053|\u89e3\u91ca\u9053|\u63d0\u9192\u9053|\u561f\u56d4\u9053|\u4f4e\u58f0\u8bf4|\u8f7b\u58f0\u8bf4)'
    r'|(?:^|\n)\s*([\u4e00-\u9fff\u3400-\u4dbf]{2,6})(?:\uff1a|:)\s*[\u300c"\'"]'
)
# \u81ea\u52a8\u6ce8\u518c speaker \u65f6\u6392\u9664\u7684\u8bef\u5339\u914d\u6a21\u5f0f\uff08\u526f\u8bcd/\u52a8\u4f5c\u77ed\u8bed/\u4ee3\u8bcd/\u6cdb\u79f0\u7b49\uff09
_SPEAKER_REJECT_CHARS = set("\u7684\u5730\u5f97\u4e86\u7740\u8fc7\u6765\u53bb\u5728\u4ece\u5230\u628a\u88ab\u7ed9\u8ba9\u53eb\u662f\u6709\u6ca1\u4e0d\u4e5f\u5c31\u90fd\u8fd8\u53c8\u624d\u80fd\u4f1a\u8981\u60f3")
_SPEAKER_REJECT_WORDS = {
    "\u6b64\u523b", "\u6ca1\u4eba", "\u542b\u6df7", "\u538b\u4f4e", "\u62ff\u8d77", "\u653e\u4e0b", "\u62ac\u5934", "\u4f4e\u5934",
    "\u8f6c\u8eab", "\u8d77\u8eab", "\u4f38\u624b", "\u70b9\u5934", "\u6447\u5934", "\u6325\u624b", "\u7ec8\u4e8e", "\u7a81\u7136",
    "\u7136\u540e", "\u968f\u540e", "\u6700\u7ec8", "\u5ffd\u7136", "\u7acb\u523b", "\u9a6c\u4e0a", "\u8fde\u5fd9", "\u8d76\u7d27",
    "\u53ea\u89c1", "\u90a3\u4eba", "\u6b64\u4eba", "\u67d0\u4eba", "\u5bf9\u65b9", "\u8eab\u8fb9", "\u65c1\u8fb9", "\u95e8\u5916",
    "\u6709\u4eba", "\u65e0\u4eba", "\u4f17\u4eba", "\u4e00\u4eba", "\u4ed6\u4eba", "\u8def\u4eba", "\u51e0\u4eba", "\u4e24\u4eba",
    "\u8bdd\u7b52", "\u55d3\u5b50", "\u624b\u6307", "\u7709\u5934", "\u80a9\u8180", "\u8111\u888b", "\u8eab\u5b50", "\u8170\u8eab",
}

_ATTR_SYNONYMS = {
    "力量": ("武力", "力", "武", "攻击", "战斗", "体力", "strength", "str"),
    "敏捷": ("身法", "速度", "灵巧", "反应", "闪避", "dexterity", "dex", "agility"),
    "智力": ("智谋", "智慧", "才学", "学识", "知识", "情报", "intelligence", "int", "wisdom", "wis"),
    "魅力": ("口才", "话术", "社交", "外交", "领导", "统率", "政治", "charisma", "cha"),
    "体力": ("耐力", "体质", "生命", "constitution", "con", "stamina"),
}

GAME_TOOLS = {
    "roll_dice": {"desc": "\u63b7\u9ab0\u5b50\u68c0\u5b9a\u3002\u53c2\u6570: skill(\u6280\u80fd\u540d), dc(\u96be\u5ea6,\u53ef\u9009)", "example": "roll_dice(\u5bdf\u89c9, 12)"},
    "check_inventory": {"desc": "\u68c0\u67e5\u73a9\u5bb6\u662f\u5426\u6301\u6709\u67d0\u7269\u54c1\u3002\u53c2\u6570: item_name", "example": "check_inventory(\u706b\u628a)"},
    "get_npc_attitude": {"desc": "\u67e5\u8be2NPC\u5bf9\u73a9\u5bb6\u7684\u597d\u611f\u5ea6\u3002\u53c2\u6570: npc_id", "example": "get_npc_attitude(merchant_lin)"},
    "get_time": {"desc": "\u83b7\u53d6\u5f53\u524d\u6e38\u620f\u65f6\u95f4", "example": "get_time()"},
}

GAME_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "roll_dice",
        "description": "\u63b7\u9ab0\u5b50\u8fdb\u884c\u6280\u80fd\u68c0\u5b9a\uff0c\u8fd4\u56ded20\u7ed3\u679c\u548c\u6210\u529f/\u5931\u8d25\u5224\u5b9a",
        "parameters": {"type": "object", "properties": {
            "skill": {"type": "string", "description": "\u6280\u80fd\u540d\u79f0\uff0c\u5982\u5bdf\u89c9\u3001\u4ea4\u6d89\u3001\u6f5c\u884c"},
            "dc": {"type": "integer", "description": "\u96be\u5ea6\u7b49\u7ea7(Difficulty Class)\uff0c\u9ed8\u8ba410", "default": 10},
        }, "required": ["skill"]},
    }},
    {"type": "function", "function": {
        "name": "check_inventory",
        "description": "\u68c0\u67e5\u73a9\u5bb6\u80cc\u5305\u4e2d\u662f\u5426\u6301\u6709\u6307\u5b9a\u7269\u54c1",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "\u7269\u54c1\u540d\u79f0"},
        }, "required": ["item_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_npc_attitude",
        "description": "\u67e5\u8be2NPC\u5f53\u524d\u5bf9\u73a9\u5bb6\u7684\u597d\u611f\u5ea6/\u6001\u5ea6",
        "parameters": {"type": "object", "properties": {
            "npc_id": {"type": "string", "description": "NPC\u7684ID\u6807\u8bc6"},
        }, "required": ["npc_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "\u83b7\u53d6\u5f53\u524d\u7684\u6e38\u620f\u5185\u65f6\u95f4",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "modify_stat",
        "description": "\u4fee\u6539\u73a9\u5bb6\u7684\u5c5e\u6027\u503c\uff08\u5982\u751f\u547d\u503c\u3001\u91d1\u5e01\u7b49\uff09",
        "parameters": {"type": "object", "properties": {
            "stat": {"type": "string", "description": "\u5c5e\u6027\u540d\u79f0\uff0c\u5982hp\u3001gold\u3001mana"},
            "delta": {"type": "integer", "description": "\u53d8\u5316\u91cf\uff0c\u6b63\u6570\u589e\u52a0\uff0c\u8d1f\u6570\u51cf\u5c11"},
        }, "required": ["stat", "delta"]},
    }},
    {"type": "function", "function": {
        "name": "query_lore",
        "description": "\u67e5\u8be2\u77e5\u8bc6\u5e93\u4e2d\u4e0e\u5173\u952e\u8bcd\u76f8\u5173\u7684\u6761\u76ee\uff08\u4e16\u754c\u89c2/\u4eba\u7269/\u5730\u70b9\u4fe1\u606f\uff09",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "\u67e5\u8be2\u5173\u952e\u8bcd"},
        }, "required": ["keyword"]},
    }},
    {"type": "function", "function": {
        "name": "change_location",
        "description": "\u5c06\u73a9\u5bb6\u79fb\u52a8\u5230\u6307\u5b9a\u5730\u70b9",
        "parameters": {"type": "object", "properties": {
            "location_id": {"type": "string", "description": "\u76ee\u6807\u5730\u70b9ID"},
        }, "required": ["location_id"]},
    }},
    {"type": "function", "function": {
        "name": "set_variable",
        "description": "\u8bbe\u7f6e\u6216\u4fee\u6539\u811a\u672c\u53d8\u91cf\u7684\u503c",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "\u53d8\u91cf\u540d\u79f0"},
            "value": {"type": "string", "description": "\u53d8\u91cf\u503c"},
        }, "required": ["name", "value"]},
    }},
    # --- P1 \u4e0a\u4e0b\u6587 Pull \u5de5\u5177 ---
    {"type": "function", "function": {
        "name": "recall_history",
        "description": "\u641c\u7d22\u5386\u53f2\u5bf9\u8bdd\u8bb0\u5f55\uff0c\u8fd4\u56de\u76f8\u5173\u56de\u5408\u7684\u6458\u8981",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "\u641c\u7d22\u5173\u952e\u8bcd"},
            "max_results": {"type": "integer", "description": "\u6700\u591a\u8fd4\u56de\u6761\u6570", "default": 5},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "query_lorebook",
        "description": "\u6309\u5173\u952e\u8bcd\u67e5\u8be2\u4e16\u754c\u4e66\u77e5\u8bc6\u6761\u76ee",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "\u641c\u7d22\u8bcd"},
        }, "required": ["keyword"]},
    }},
    {"type": "function", "function": {
        "name": "query_npc_history",
        "description": "\u67e5\u8be2\u4e0e\u7279\u5b9aNPC\u7684\u4e92\u52a8\u5386\u53f2",
        "parameters": {"type": "object", "properties": {
            "npc_name": {"type": "string", "description": "NPC\u540d\u79f0"},
            "max_turns": {"type": "integer", "description": "\u6700\u8fd1\u51e0\u8f6e", "default": 5},
        }, "required": ["npc_name"]},
    }},
]

# P2: 叙事生成工具 schema（Stage 2+3 合并后的辅助工具调用）
NARRATIVE_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "set_atmosphere",
        "description": "设置场景氛围效果",
        "parameters": {"type": "object", "properties": {
            "weather": {"type": "string", "description": "天气"},
            "lighting": {"type": "string", "description": "光照"},
            "sounds": {"type": "string", "description": "环境音"},
            "mood": {"type": "string", "description": "整体氛围基调"},
        }},
    }},
    {"type": "function", "function": {
        "name": "set_scene_image",
        "description": "触发场景图片生成",
        "parameters": {"type": "object", "properties": {
            "prompt": {"type": "string", "description": "图片描述（英文）"},
            "style": {"type": "string", "enum": ["realistic", "anime", "pixel"], "description": "风格"},
        }, "required": ["prompt"]},
    }},
]

# Stage 4: 状态推演工具 schema（单次工具调用替代 5 路并行 LLM）
STATE_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "update_resources",
        "description": "更新角色属性、物品、持续状态、胜负判定",
        "parameters": {"type": "object", "properties": {
            "state_changes": {
                "type": "array",
                "description": '属性变更。每项: {"target":"player.属性名","op":"add|subtract|set","value":数值,"reason":"原因(add/subtract时必填)"}。也可修改NPC属性: target="npcs.{npc_id}.字段名" op="set"',
                "items": {"type": "object", "properties": {
                    "target": {"type": "string"}, "op": {"type": "string", "enum": ["add", "subtract", "set"]},
                    "value": {}, "reason": {"type": "string"},
                }, "required": ["target", "op", "value"]},
            },
            "inventory_changes": {
                "type": "array",
                "description": '物品变更。每项: {"item":"物品名","action":"add|remove","quantity":1,"description":"可选"}。add限制：只能添加叙事中明确描写获取的物品',
                "items": {"type": "object", "properties": {
                    "item": {"type": "string"}, "action": {"type": "string", "enum": ["add", "remove"]},
                    "quantity": {"type": "integer", "default": 1}, "description": {"type": "string"},
                }, "required": ["item", "action"]},
            },
            "activate_states": {
                "type": "array",
                "description": '激活持续状态。每项: {"id":"状态ID","name":"中文显示名称（必填）","description":"一句话描述"}',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"}, "description": {"type": "string"},
                }, "required": ["id", "name"]},
            },
            "deactivate_states": {
                "type": "array", "description": "要移除的状态ID列表",
                "items": {"type": "string"},
            },
            "game_over": {
                "type": "object", "description": '游戏结束时填写: {"reason":"","ending_type":""}',
                "properties": {"reason": {"type": "string"}, "ending_type": {"type": "string"}},
            },
        }},
    }},
    {"type": "function", "function": {
        "name": "update_spatial",
        "description": "更新位置移动、地点发现、NPC位置、房间、场景描写",
        "parameters": {"type": "object", "properties": {
            "location_change": {
                "type": "string",
                "description": "玩家回合结束时所在位置ID（必须从已知地点列表复制，未移动则省略）",
            },
            "reveal_locations": {
                "type": "array", "description": '新发现地点: [{"id":"位置ID","name":"显示名称"}]',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "name": {"type": "string"},
                }, "required": ["id", "name"]},
            },
            "npc_location_changes": {
                "type": "array",
                "description": '本回合在场NPC位置变动: [{"npc_id":"","new_location":"位置ID","reason":"原因"}]',
                "items": {"type": "object", "properties": {
                    "npc_id": {"type": "string"}, "new_location": {"type": "string"}, "reason": {"type": "string"},
                }, "required": ["npc_id", "new_location"]},
            },
            "room_changes": {
                "type": "array",
                "description": '同一建筑内房间级移动: [{"id":"npc_id或player","new_room":"房间名","reason":"原因"}]',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "new_room": {"type": "string"}, "reason": {"type": "string"},
                }, "required": ["id", "new_room"]},
            },
            "scene_details": {
                "type": "object",
                "description": '场景描写: {"atmosphere":"","sensory":"","key_objects":[],"physical":{"lighting":"","floor":"","spatial_note":""}}',
                "properties": {
                    "atmosphere": {"type": "string"}, "sensory": {"type": "string"},
                    "key_objects": {"type": "array", "items": {"type": "string"}},
                    "physical": {"type": "object", "properties": {
                        "lighting": {"type": "string"}, "floor": {"type": "string"}, "spatial_note": {"type": "string"},
                    }},
                },
            },
        }},
    }},
    {"type": "function", "function": {
        "name": "update_time",
        "description": "更新游戏时间（end_time）",
        "parameters": {"type": "object", "properties": {
            "end_time": {
                "type": "string",
                "description": "叙事文本最后一句描写对应的时刻，ISO 8601格式（如1979-10-26T10:00:00）。必填。严格对齐叙事末尾时间点",
            },
        }, "required": ["end_time"]},
    }},
    {"type": "function", "function": {
        "name": "update_world",
        "description": "更新宏观世界级变量（戒严等级、势力影响等），不写物件状态",
        "parameters": {"type": "object", "properties": {
            "world_property_changes": {
                "type": "array",
                "description": '世界属性变更: [{"id":"属性ID","value":"新值"}]。只写宏观世界级变量，不写物件状态（台灯/门窗等属于scene_details）',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string"}, "value": {"type": "string"},
                }, "required": ["id", "value"]},
            },
        }},
    }},
    {"type": "function", "function": {
        "name": "update_extended",
        "description": "更新离场NPC动态、新NPC注册、阵营声望、道德维度、同伴变动",
        "parameters": {"type": "object", "properties": {
            "offscreen_npc_updates": {
                "type": "array",
                "description": '离场NPC动态（最多2-3个）: [{"name":"NPC中文全名","action":"简述行动","location":"当前位置"}]',
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"}, "action": {"type": "string"}, "location": {"type": "string"},
                }, "required": ["name", "action"]},
            },
            "new_npcs": {
                "type": "array",
                "description": '新NPC注册（叙事中有名有姓、有台词或具体互动的非预设角色必须注册）',
                "items": {"type": "object", "properties": {
                    "id": {"type": "string", "description": "英文蛇形ID"},
                    "name": {"type": "string", "description": "完整全名"},
                    "title": {"type": "string"}, "bio": {"type": "string"},
                    "personality": {"type": "string"}, "location": {"type": "string"},
                    "trust": {"type": "integer"}, "affection": {"type": "integer"}, "fear": {"type": "integer"},
                }, "required": ["id", "name", "location"]},
            },
            "faction_reputation_changes": {
                "type": "array",
                "description": '阵营声望变化: [{"faction_id":"","change":±数值,"reason":""}]（±1到±15）',
                "items": {"type": "object", "properties": {
                    "faction_id": {"type": "string"}, "change": {"type": "integer"}, "reason": {"type": "string"},
                }, "required": ["faction_id", "change"]},
            },
            "moral_alignment_changes": {
                "type": "array",
                "description": '道德维度影响: [{"axis":"mercy_vs_cruelty|honesty_vs_deception|order_vs_chaos","change":±数值,"reason":""}]（±1到±15）',
                "items": {"type": "object", "properties": {
                    "axis": {"type": "string"}, "change": {"type": "integer"}, "reason": {"type": "string"},
                }, "required": ["axis", "change"]},
            },
            "recruit_companions": {
                "type": "array", "description": "加入队伍的NPC ID列表",
                "items": {"type": "string"},
            },
            "dismiss_companions": {
                "type": "array", "description": "离队的NPC ID列表",
                "items": {"type": "string"},
            },
            "invalidate_lore": {
                "type": "array", "description": "要废止的知识词条ID列表",
                "items": {"type": "string"},
            },
        }},
    }},
]


def _extract_reasoning(raw: str) -> str:
    """Extract content inside <think>...</think> tags. Returns empty string if none."""
    if not raw:
        return ""
    m = _THINK_EXTRACT_RE.search(raw)
    return m.group(1).strip() if m else ""


class GameSession:
    def __init__(self, script: dict, ai_provider, save_id: str | None = None,
                 stage_models: dict | None = None):
        self.script = script
        self.ai_provider = ai_provider
        self.stage_models = stage_models or {}
        self.save_id = save_id or str(uuid.uuid4())
        self.dice = DiceRoller()
        self.state_manager = StateManager(script)
        self.event_scheduler = EventScheduler(script)
        self.prompt_builder = PromptBuilder(script)
        self.prompt_builder.condition_eval = self._evaluate_condition
        self.response_parser = ResponseParser()
        self.world_tree = WorldTree(script_id=script.get("script_id", ""))
        _settings = script.get("settings", {})
        self.history_summarizer = HistorySummarizer(
            threshold=_settings.get("summary_interval", 10),
            keep_recent=_settings.get("summary_keep_recent", 5),
            word_threshold=_settings.get("summary_word_threshold", 3000),
        )
        _vm_enabled = _settings.get("vector_memory_enabled", False)
        self.vector_memory = VectorMemory(self.save_id) if (_vm_enabled and _VECTOR_AVAILABLE) else None
        self.data_bank = DataBank(self.save_id) if _VECTOR_AVAILABLE else None
        self.script_variables = ScriptVariables(script.get("variables", []))
        self.trigger_engine = TriggerEngine(script.get("triggers", []), self.script_variables)
        self.regex_engine = RegexScriptEngine(script.get("regex_scripts", []))
        _class_system = _settings.get("class_system")
        self.class_registry = ClassRegistry(_class_system) if _class_system else None
        _story_tree_def = script.get("story_tree", {})
        self._merge_legacy_events_into_tree(_story_tree_def, script)
        self.story_tree_engine = StoryTreeEngine(_story_tree_def) if _story_tree_def.get("trees") else None
        self.event_engine = EventEngine(script)
        self.current_state: dict = {}
        self.turn_number = 0
        self.authors_note = ""  # Player's behind-the-scenes directive
        self.authors_note_position = "end"  # "end" or "at_depth"
        self.authors_note_depth = 4
        self.negative_prompt = ""  # CFG-style negative prompt (injected as prohibition rules)
        self.logit_bias: list[dict] = []  # [{"text": "暴力", "bias": -5}, ...]
        self._background_tasks: set[asyncio.Task] = set()
        self._state_lock = asyncio.Lock()  # 保护 current_state 的后台任务锁
        self._4b_semaphore = asyncio.Semaphore(_settings.get("max_parallel_4b", 5))
        self._summary_fail_count = 0  # 摘要连续失败计数
        self._last_activity_time: float = 0.0  # 上次活跃时间戳（用于 play_time 计算）
        # P1: 预构建 ID → 名称/对象的字典查找，避免每次行动 O(n) 扫列表
        self._location_by_id: dict = {
            loc["id"]: loc for loc in script.get("locations", [])
            if isinstance(loc, dict) and loc.get("id")
        }
        self._event_desc_by_id: dict = {}
        self._event_def_by_id: dict = {}
        for ev in script.get("cyclic_events", []):
            if isinstance(ev, dict) and ev.get("id"):
                self._event_desc_by_id[ev["id"]] = ev.get("description", ev["id"])
                self._event_def_by_id[ev["id"]] = ev
        for ev in script.get("one_time_events", []):
            if isinstance(ev, dict) and ev.get("id"):
                self._event_desc_by_id[ev["id"]] = ev.get("description", ev["id"])
                self._event_def_by_id[ev["id"]] = ev
        self._npc_by_id: dict = {
            n["id"]: n for n in script.get("npcs", [])
            if isinstance(n, dict) and n.get("id")
        }
        self._npc_name_to_id: dict[str, str] = {
            n.get("name", ""): n["id"] for n in script.get("npcs", [])
            if isinstance(n, dict) and n.get("id") and n.get("name")
        }
        self._random_item_by_id: dict = {
            ri["id"]: ri for ri in script.get("random_items", [])
            if isinstance(ri, dict) and ri.get("id")
        }
        self._ps_by_id: dict = {
            ps["id"]: ps for ps in script.get("persistent_states", [])
            if isinstance(ps, dict) and ps.get("id")
        }
        self._org_members: dict[str, list[str]] = {}
        for _npc_def in script.get("npcs", []):
            for _om in _npc_def.get("organizations", []):
                _oid = _om.get("org_id", "")
                if _oid:
                    self._org_members.setdefault(_oid, []).append(_npc_def["id"])
        self.meta_event_bus = MetaEventBus()
        self._register_meta_events()

    def _stage_kwargs(self, stage: str) -> dict:
        model = self.stage_models.get(stage)
        return {"model": model} if model else {}

    def _register_meta_events(self):
        bus = self.meta_event_bus
        # --- Migrated existing background tasks ---
        bus.register(MetaEvent(
            id="summarize_history",
            handler="_handle_summarize_history",
            trigger=MetaEventTrigger(requires_ai=False),
        ))
        bus.register(MetaEvent(
            id="analyze_play_style",
            handler="_handle_analyze_play_style",
            trigger=MetaEventTrigger(
                cooldown_turns=PLAY_STYLE_INTERVAL,
                min_turn=PLAY_STYLE_INTERVAL,
            ),
        ))
        bus.register(MetaEvent(
            id="offscreen_simulation",
            handler="_handle_offscreen_simulation",
            trigger=MetaEventTrigger(turn_interval=OFFSCREEN_SIM_INTERVAL),
        ))
        bus.register(MetaEvent(
            id="ai_polish_chapters",
            handler="_handle_ai_polish_chapters",
            trigger=MetaEventTrigger(),
        ))
        bus.register(MetaEvent(
            id="story_director",
            handler="_handle_story_director",
            trigger=MetaEventTrigger(turn_interval=5, min_turn=5),
            priority=80,
        ))
        bus.register(MetaEvent(
            id="story_director_flag",
            handler="_handle_story_director",
            trigger=MetaEventTrigger(
                state_flag="_pending_story_director", consume_flag=True,
            ),
        ))
        # --- New system-level meta-events ---
        bus.register(MetaEvent(
            id="lorebook_evolution",
            handler="_handle_lorebook_evolution",
            trigger=MetaEventTrigger(cooldown_turns=3, min_turn=5),
        ))
        bus.register(MetaEvent(
            id="lorebook_evolution_urgent",
            handler="_handle_lorebook_evolution",
            trigger=MetaEventTrigger(
                state_flag="_pending_lore_evolution", consume_flag=True,
            ),
        ))
        bus.register(MetaEvent(
            id="narrative_consistency",
            handler="_handle_narrative_consistency",
            trigger=MetaEventTrigger(
                cooldown_turns=2, min_turn=3, requires_vector_memory=True,
            ),
        ))
        bus.register(MetaEvent(
            id="player_behavior_profiling",
            handler="_handle_player_behavior_profiling",
            trigger=MetaEventTrigger(turn_interval=4, min_turn=4),
        ))
        bus.register(MetaEvent(
            id="story_feedback",
            handler="_handle_story_feedback",
            trigger=MetaEventTrigger(requires_ai=False),
            priority=200,
        ))
        bus.register(MetaEvent(
            id="event_stage",
            handler="_handle_event_stage",
            trigger=MetaEventTrigger(requires_ai=True),
            priority=250,
        ))
        # --- Gameplay innovation modules ---
        bus.register(MetaEvent(
            id="promise_extraction",
            handler="_handle_promise_extraction",
            trigger=MetaEventTrigger(turn_interval=1, requires_ai=True),
            priority=80,
        ))
        bus.register(MetaEvent(
            id="plan_progress_check",
            handler="_handle_plan_progress",
            trigger=MetaEventTrigger(turn_interval=1, requires_ai=True),
            priority=70,
        ))
        bus.register(MetaEvent(
            id="faction_warfare_tick",
            handler="_handle_faction_warfare",
            trigger=MetaEventTrigger(turn_interval=3, min_turn=5),
            priority=60,
        ))
        bus.register(MetaEvent(
            id="retroactive_revelation",
            handler="_handle_retroactive_revelation",
            trigger=MetaEventTrigger(
                state_flag="_pending_flashback", consume_flag=True,
                requires_ai=True,
            ),
            priority=90,
        ))
        bus.register(MetaEvent(
            id="npc_goal_conflict",
            handler="_handle_npc_goal_conflict",
            trigger=MetaEventTrigger(
                state_flag="_npc_conflict_pending", consume_flag=True,
                requires_ai=True,
            ),
            priority=85,
        ))

    def _dispatch_meta_events(self, ctx: dict, parsed: dict,
                              player_action: dict, raw_response: str = ""):
        if ctx.get("route", {}).get("expand_story"):
            self.current_state["_pending_story_director"] = True
        events_to_fire = self.meta_event_bus.evaluate(
            turn_number=self.turn_number,
            state=self.current_state,
            condition_eval=self._evaluate_condition,
            ai_available=self.ai_provider is not None,
            vector_available=self.vector_memory is not None,
        )
        if not events_to_fire:
            return
        meta_ctx = {
            "turn_number": self.turn_number,
            "narrative": parsed.get("narrative", raw_response),
            "raw_response": raw_response,
            "parsed": parsed,
            "player_action": player_action,
            "old_time": ctx.get("old_time", ""),
            "new_time": self.current_state.get("game_time", ""),
            "state_changes": ctx.get("all_state_changes", []),
            "triggered_events": ctx.get("triggered_events", []),
            "activated_lore": ctx.get("activated_lore", []),
        }
        for evt in events_to_fire:
            handler = getattr(self, evt.handler, None)
            if handler is None:
                continue
            if evt.trigger.state_flag and evt.trigger.consume_flag:
                self.current_state.pop(evt.trigger.state_flag, None)
            self.meta_event_bus.mark_fired(
                self.current_state, evt.id, self.turn_number,
            )
            self._schedule_background_task(handler(meta_ctx))

    def _build_logit_bias_hint(self) -> str:
        if not self.logit_bias:
            return ""
        encourage = [e["text"] for e in self.logit_bias if e.get("bias", 0) > 0]
        suppress = [e["text"] for e in self.logit_bias if e.get("bias", 0) < 0]
        parts = []
        if encourage:
            parts.append("鼓励使用的元素/词汇: " + "、".join(encourage))
        if suppress:
            parts.append("尽量避免的元素/词汇: " + "、".join(suppress))
        return "## 词汇偏置指引\n" + "\n".join(parts) if parts else ""

    async def _stage1_with_native_tools(self, plot_msgs: list[dict], plot_sys: str,
                                         tools_schema: list[dict], **kwargs) -> tuple[str, list[dict]]:
        """Execute Stage 1 with native API tool calling loop.

        Returns (plot_decision_text, tool_results).
        """
        tool_results = []
        messages = list(plot_msgs)
        max_rounds = 5

        for _ in range(max_rounds):
            resp = await self.ai_provider.generate_with_tools(
                messages, system=plot_sys, tools=tools_schema, **kwargs
            )
            tc_list = resp.get("tool_calls")
            if not tc_list:
                content = resp.get("content", "")
                if tool_results:
                    tool_context = "\n".join(f"[{r['tool']}结果: {r['result']}]" for r in tool_results)
                    content = content + "\n" + tool_context
                return content, tool_results

            assistant_msg = {"role": "assistant", "content": resp.get("content") or None}
            reasoning = resp.get("reasoning_content")
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            # Build tool_calls for the assistant message (OpenAI format)
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in tc_list
            ]
            messages.append(assistant_msg)

            for tc in tc_list:
                result = self._run_tool_native(tc["name"], tc["arguments"])
                tool_results.append({"tool": tc["name"], "args": tc["arguments"], "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # Max rounds reached — get final content
        resp = await self.ai_provider.generate_with_tools(
            messages, system=plot_sys, **kwargs
        )
        content = resp.get("content", "")
        if tool_results:
            tool_context = "\n".join(f"[{r['tool']}结果: {r['result']}]" for r in tool_results)
            content = content + "\n" + tool_context
        return content, tool_results

    def _mark_activity(self) -> int:
        """标记活跃并返回自上次活跃以来的秒数（上限600秒，避免计入挂机时间）。"""
        now = _time.time()
        if self._last_activity_time <= 0:
            self._last_activity_time = now
            return 0
        elapsed = int(now - self._last_activity_time)
        self._last_activity_time = now
        return min(elapsed, 600)

    def _schedule_background_task(self, coro):
        """Schedule a background task with proper exception logging."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)

        def _on_done(t: asyncio.Task):
            self._background_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logging.getLogger(__name__).error(
                    "后台任务异常: %s", exc, exc_info=exc
                )

        task.add_done_callback(_on_done)

    async def _drain_background_tasks(self, timeout: float = 10.0):
        """P0-3: 等待所有后台任务完成，确保 state 不会被并发修改。
        在每个用户入口（process_action 等）开头调用。"""
        if not self._background_tasks:
            return
        tasks = list(self._background_tasks)
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        self._background_tasks -= done
        if pending:
            logger.warning("_drain_background_tasks: %d 个后台任务超时(%ss)，强制取消", len(pending), timeout)
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            self._background_tasks -= pending

    async def _enrich_with_rag(
        self, action_text: str, recent: list[dict],
        activated_lore: list, history_context: str,
    ) -> tuple[str, set]:
        """Enrich history_context with vector memory retrieval + keyword lorebook fallback.

        Returns (enriched_history_context, lore_ids_from_rag).
        """
        lore_ids_from_rag: set = set()
        if self.vector_memory:
            exclude = [recent[-1].get("turn_number", 0)] if recent else []
            vq = action_text + " " + (recent[-1].get("ai_response", "")[:100] if recent else "")
            if self.story_tree_engine:
                sts = self.current_state.get("story_tree_state", {})
                for _nid in list(sts.get("active", []))[:3]:
                    _node = self.story_tree_engine._nodes.get(_nid)
                    if _node:
                        vq += " " + (_node.get("description", "") or _node.get("name", ""))[:60]
            # 追加当前地点名和在场 NPC 名以偏向相关内容
            _loc_id = self.current_state.get("player", {}).get("location", "")
            if _loc_id:
                _loc_name = self.current_state.get("display_names", {}).get(_loc_id, _loc_id)
                vq += " " + _loc_name
            _npc_states = self.current_state.get("npcs", {})
            for _nid_k, _ns in list(_npc_states.items())[:3]:
                if isinstance(_ns, dict) and _ns.get("current_location") == _loc_id:
                    vq += " " + _ns.get("name", _nid_k)
            retrieved = await asyncio.get_running_loop().run_in_executor(
                None, self.vector_memory.query, vq, 5, exclude
            )
            if retrieved:
                history_lines = []
                lore_lines = []
                for r in retrieved:
                    if r.get("doc_type") == "lorebook":
                        lore_lines.append(f"[{r.get('comment', '')}] {r['text']}")
                        lore_ids_from_rag.add(r.get("entry_id", ""))
                    else:
                        history_lines.append(f"第{r['turn']}回合({r['game_time']}): {r['text']}")
                        # 第二层：从历史回合 metadata 提取关联 lore IDs
                        assoc_lore = r.get("active_lore_ids", "")
                        if assoc_lore:
                            for lid in assoc_lore.split(","):
                                lid = lid.strip()
                                if lid:
                                    lore_ids_from_rag.add(lid)
                extra = ""
                if history_lines:
                    extra += "## 相关历史记忆（语义检索）\n" + "\n".join(history_lines)
                if lore_lines:
                    extra += ("\n\n" if extra else "") + "## 相关世界知识（语义检索）\n" + "\n".join(lore_lines)
                if extra:
                    history_context = (history_context + "\n\n" + extra).strip()

        # 第一层：RAG 召回的 lorebook 条目回注 activated_lore，参与正常注入流程
        if lore_ids_from_rag:
            entry_by_id = {e.id: e for e in self.prompt_builder.lorebook.entries}
            for lid in lore_ids_from_rag:
                entry = entry_by_id.get(lid)
                if entry and not any(e.id == lid for e in activated_lore):
                    activated_lore.append(entry)

        keyword_only_lore = [e for e in activated_lore if e.id not in lore_ids_from_rag]
        if keyword_only_lore:
            fallback_lines = ["## 关键词匹配的补充知识"]
            for e in keyword_only_lore[:5]:
                fallback_lines.append(f"[{e.comment or e.id}] {e.content}")
            history_context = (history_context + "\n\n" + "\n".join(fallback_lines)).strip()

        # Data Bank retrieval
        if self.data_bank:
            vq = action_text + " " + (recent[-1].get("ai_response", "")[:100] if recent else "")
            try:
                loop = asyncio.get_running_loop()
                bank_results = await loop.run_in_executor(None, self.data_bank.query, vq, 3)
                if bank_results:
                    self._last_bank_results = bank_results
                    bank_lines = ["## 知识库参考"]
                    for br in bank_results:
                        src = f"[{br['filename']}]" if br.get("filename") else ""
                        bank_lines.append(f"{src} {br['text'][:300]}")
                    history_context = (history_context + "\n\n" + "\n".join(bank_lines)).strip()
            except Exception:
                pass

        return history_context, lore_ids_from_rag

    async def _stage_rag_query(self, query_text: str, top_k: int = 3,
                                doc_type: str | None = None,
                                exclude_turns: list | None = None,
                                boost_lore_ids: set | None = None) -> list[dict]:
        """Lightweight per-stage RAG query with optional story-aware re-ranking."""
        if not self.vector_memory or not query_text:
            return []
        loop = asyncio.get_running_loop()
        fetch_k = top_k * 2 if boost_lore_ids else top_k
        results = await loop.run_in_executor(
            None, self.vector_memory.query, query_text, fetch_k, exclude_turns, doc_type
        )
        if boost_lore_ids and results:
            for r in results:
                if r.get("doc_type") == "milestone":
                    r["score"] = r.get("score", 0.5) * 1.3
                lore_csv = r.get("active_lore_ids", "")
                if lore_csv and boost_lore_ids & set(lore_csv.split(",")):
                    r["score"] = r.get("score", 0.5) * 1.2
            results.sort(key=lambda x: x.get("score", 0), reverse=True)
            results = results[:top_k]
        return results

    def _boost_lore_by_route(self, activated_lore: list, route: dict):
        """根据 route 的 scene_type/scope 临时调整 activated_lore 排序。"""
        scene_type = route.get("scene_type", "")
        scope = route.get("scope", "moderate")
        type_keywords = {
            "combat": ["战斗", "武器", "军事", "combat"],
            "social": ["关系", "社交", "对话", "social"],
            "exploration": ["地点", "探索", "地理", "exploration"],
            "trade": ["交易", "商业", "经济", "trade"],
        }
        boost_kws = type_keywords.get(scene_type, [])
        if not boost_kws:
            return
        boosted = []
        rest = []
        for entry in activated_lore:
            comment_lower = (entry.comment or "").lower()
            if any(kw in comment_lower for kw in boost_kws):
                boosted.append(entry)
            else:
                rest.append(entry)
        if scope == "major" and hasattr(self, "prompt_builder") and self.prompt_builder.lorebook:
            existing_ids = {e.id for e in activated_lore}
            for entry in self.prompt_builder.lorebook.entries:
                if entry.id not in existing_ids and entry.priority >= 90:
                    rest.append(entry)
        activated_lore.clear()
        activated_lore.extend(boosted + rest)

    @staticmethod
    def _parse_route(raw: str) -> dict:
        raw = strip_think_tags(raw or "").strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```\s*$', '', raw)
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(raw[start:end + 1])
                except json.JSONDecodeError:
                    pass
        return {"scene_type": "unknown", "systems": [], "scope": "moderate", "focus_npcs": [], "skill_check": {"needed": False}}

    async def _run_route_stage(self, action_text: str, ctx: dict) -> dict:
        player = self.current_state.get("player", {})
        location_id = player.get("location", "")
        location_name = self.prompt_builder._resolve_location_name(location_id)
        loc_def = self.prompt_builder._location_by_id.get(location_id, {})
        loc_desc = loc_def.get("description", "")
        present_npc_ids = ctx.get("present_npc_ids", [])
        npc_states = self.current_state.get("npcs", {})
        npc_entries = []
        for nid in present_npc_ids[:8]:
            npc_data = npc_states.get(nid, {})
            npc_name = npc_data.get("name", nid) if isinstance(npc_data, dict) else nid
            npc_entries.append(f"{nid}({npc_name})")
        npc_list_str = ", ".join(npc_entries)
        attrs = player.get("attributes", {})
        attr_names = list(attrs.keys())
        attr_list_str = "/".join(attr_names) if attr_names else "力量/敏捷/智力/魅力"
        route_system = "你是场景分析器。根据玩家行动判断涉及的游戏系统和是否需要技能检定。只返回紧凑JSON，不要解释。"
        loc_line = location_name
        if loc_desc:
            loc_line += f"（{loc_desc[:40]}）"
        route_user = (
            f"位置: {loc_line}\n"
            f"当前游戏时间: {ctx.get('old_time', '未知')}\n"
            f"在场NPC: {npc_list_str or '无'}\n"
            f"角色属性: {attr_list_str}\n"
            f"玩家行动: {action_text}\n\n"
            '返回: {"scene_type":"social|combat|exploration|trade|travel|rest",'
            '"systems":["从npc/location/inventory/faction/moral/companion/deadline/clue/quest/combat/new_npc中选择相关的"],'
            '"scope":"minor|moderate|major",'
            '"focus_npcs":["填写npc_id（英文ID），不要填中文名"],'
            '"skill_check":{"needed":true/false,"attr":"从角色属性中选择最相关的","difficulty":"easy|medium|hard|extreme"},'
            '"expand_story":true/false,'
            '"generate_image":true/false}'
            "\n\nskill_check规则：\n"
            "- 日常对话、简单移动、等待、休息等不需要检定(needed:false)\n"
            "- 有风险或挑战性的行动需要检定(needed:true)，如潜行、说服、战斗、调查、偷窃等\n"
            "- difficulty根据行动难度和环境判断\n\n"
            "expand_story规则：\n"
            "- 当剧情树大部分节点已完成、或剧情出现重大转折(scope=major)、或玩家进入全新区域时 → true\n"
            "- 日常行动、推进中的剧情尚未完结时 → false\n\n"
            "generate_image规则：\n"
            "- 场景发生明显视觉变化时生成图像(true)：进入新地点、战斗场面、重大事件、环境剧变、初次见面\n"
            "- 纯对话、等待、思考、小幅移动、重复场景等无明显视觉变化时不生成(false)"
        )
        # Feature #3: Plan awareness in route
        plan = self.current_state.get("pending_plan")
        if plan and plan.get("status") == "active" and plan.get("steps_remaining"):
            route_user += (
                f"\n\n注意: 玩家有未完成计划「{plan['goal'][:30]}」，"
                f"当前步骤: {plan['steps_remaining'][0][:40]}"
            )
        try:
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": route_user}],
                system=route_system, max_tokens=500,
                **self._stage_kwargs("knowledge_graph"),
            )
            route = self._parse_route(raw)
        except Exception as e:
            logger.warning("Route Stage 失败: %s", e)
            route = {"scene_type": "unknown", "systems": [], "scope": "moderate", "focus_npcs": []}
        # Feature #1: Scene hijack overrides route
        hijack = self.current_state.get("_scene_hijack")
        if hijack:
            route["scene_type"] = "npc_hijack"
            route["scope"] = "major"
            route["skill_check"] = {"needed": False}
            hijack_npc = hijack.get("npc_id", "")
            if hijack_npc and hijack_npc not in route.get("focus_npcs", []):
                route.setdefault("focus_npcs", []).insert(0, hijack_npc)
        # Feature #3: Plan declaration detection
        _PLAN_KEYWORDS = ("计划", "打算", "准备", "预谋", "我的计划是", "策划", "筹划")
        if any(kw in action_text for kw in _PLAN_KEYWORDS):
            if not (plan and plan.get("status") == "active"):
                route["has_plan_declaration"] = True
        # Store route for MetaEvent handlers
        self.current_state["_last_route"] = route
        self.current_state["_last_present_npcs"] = present_npc_ids
        logger.debug("Route: type=%s scope=%s systems=%s focus=%s",
                      route.get("scene_type"), route.get("scope"),
                      route.get("systems", []), route.get("focus_npcs", []))
        return route

    async def _async_vector_store(self, node_id: str, text: str, metadata: dict):
        """Store a turn's text in the vector DB (runs in thread pool).

        If vector_summarize is enabled and text is long, summarize first for better embeddings.
        """
        embed_text = ""
        _settings = self.script.get("settings", {})
        if _settings.get("vector_summarize") and len(text) > 500 and self.ai_provider:
            try:
                summary = await self.ai_provider.generate(
                    [{"role": "user", "content": f"用50字概括以下剧情的核心事件和情感变化:\n{text[:1000]}"}],
                    max_tokens=100,
                )
                if summary and len(summary.strip()) > 10:
                    embed_text = summary.strip()
            except Exception:
                pass
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.vector_memory.add, node_id, text, metadata, embed_text)

    async def _async_lorebook_vector_sync(self, batch: list[tuple]):
        if self.vector_memory:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, self.vector_memory.add_lorebook_batch, batch,
            )

    # Keys stripped from swipe snapshots to save memory (reconstructable from main state)
    _SWIPE_STRIP_KEYS = frozenset({
        "adventure_log", "npc_offscreen_log",
        "play_style_summary", "play_style_last_turn",
        "history_summary", "key_events", "last_summarized_turn",
        "npc_chat_history",
    })

    @staticmethod
    def _slim_snapshot(state: dict) -> dict:
        """Create a shallow copy of state with large reconstructable keys removed."""
        return {k: v for k, v in state.items() if k not in GameSession._SWIPE_STRIP_KEYS}

    @staticmethod
    def _sanitize_gather_results(
        results: tuple, labels: list[tuple[str, bool]],
    ) -> tuple[list[str], list[str]]:
        """Clean gather results: handle BaseException + strip_think_tags.

        labels: list of (display_label, emit_warning) per result.
        Returns (cleaned_strings, warnings).
        """
        cleaned: list[str] = []
        warnings: list[str] = []
        for raw, (label, warn_on_fail) in zip(results, labels):
            if isinstance(raw, BaseException):
                logger.warning("%s失败: %s", label, raw)
                cleaned.append("")
                if warn_on_fail:
                    warnings.append(f"{label}失败，本回合相关数据未更新。")
            else:
                cleaned.append(strip_think_tags(raw) if raw else "")
        return cleaned, warnings

    def _fire_lifecycle_event(self, event_name: str) -> tuple[list[str], list[str]]:
        """Fire story_tree + event_engine for a lifecycle event.
        Returns (inject_prompts, notifications).
        """
        inject_prompts: list[str] = []
        notifications: list[str] = []
        if self.story_tree_engine:
            st_res = self.story_tree_engine.evaluate(
                self.current_state, self.turn_number,
                condition_eval=self._evaluate_condition,
                active_events=[event_name],
            )
            self._apply_story_tree_result(st_res)
            inject_prompts.extend(st_res.inject_prompts)
            notifications.extend(st_res.notifications)
        if self.event_engine:
            ee_res = self.event_engine.fire_event(
                event_name, self.current_state,
                condition_eval=self._evaluate_condition,
            )
            self._apply_event_result(ee_res)
            inject_prompts.extend(ee_res.inject_prompts)
            notifications.extend(ee_res.notifications)
        return inject_prompts, notifications

    def _apply_event_def_effects(self, evt_def: dict, event_id: str) -> list:
        """Apply effects list from an event definition. Returns state change log."""
        state_log: list = []
        for eff in evt_def.get("effects", []):
            etype = eff.get("type", "")
            if etype == "state_change":
                changes = [{"target": eff["target"], "op": eff.get("op", "set"), "value": eff.get("value")}]
                self.current_state, log = self.state_manager.apply_changes(
                    self.current_state, changes, inplace=True
                )
                state_log.extend(log)
            elif etype == "activate_state":
                aps = self.current_state.setdefault("active_persistent_states", [])
                sid = eff.get("id", "")
                if sid and sid not in aps:
                    aps.append(sid)
            elif etype == "deactivate_state":
                aps = self.current_state.get("active_persistent_states", [])
                sid = eff.get("id", "")
                if sid in aps:
                    aps.remove(sid)
            elif etype == "narrative_callback":
                self._record_narrative_callback(
                    eff.get("text", ""), tags=[f"event:{event_id}"],
                    priority=eff.get("priority", "medium"),
                )
            elif etype == "activate_lore":
                self.prompt_builder.lorebook.update_entry_enabled(
                    eff.get("id", ""), True,
                )
            elif etype == "deactivate_lore":
                self.prompt_builder.lorebook.update_entry_enabled(
                    eff.get("id", ""), False,
                )
            elif etype == "add_lore":
                entry_data = eff.get("entry", {})
                if entry_data.get("id") and entry_data.get("content"):
                    entry_data.setdefault("enabled", True)
                    entry_data.setdefault("position", "after_world")
                    entry_data.setdefault("priority", 80)
                    entry_data.setdefault("scan_depth", 3)
                    entry_data["entry_type"] = "event_generated"
                    self.prompt_builder.lorebook.add_entries([entry_data])
            elif etype == "update_lore":
                lid = eff.get("id", "")
                if lid and eff.get("content"):
                    self.prompt_builder.lorebook.update_entry(
                        lid, content=eff["content"], keys=eff.get("keys"),
                    )
            elif etype == "remove_lore":
                rid = eff.get("id", "")
                if rid:
                    self.prompt_builder.lorebook.remove_entry(rid)
            elif etype == "add_random_item":
                item_data = eff.get("item", {})
                if item_data.get("id"):
                    self._apply_random_item_updates({"add": [item_data]})
            elif etype == "update_random_item":
                upd_data = eff.get("update", {})
                if upd_data.get("id"):
                    self._apply_random_item_updates({"update": [upd_data]})
            elif etype == "remove_random_item":
                rid = eff.get("id", "")
                if rid:
                    self._apply_random_item_updates({"remove": [rid]})
            elif etype == "add_variable":
                var_data = eff.get("variable", {})
                if var_data.get("id"):
                    self._apply_variable_updates({"add": [var_data]})
            elif etype == "add_trigger":
                trg_data = eff.get("trigger", {})
                if trg_data.get("id"):
                    self._apply_trigger_updates({"add": [trg_data]})
            elif etype == "update_trigger":
                upd_data = eff.get("update", {})
                if upd_data.get("id"):
                    self._apply_trigger_updates({"update": [upd_data]})
        return state_log

    async def initialize(self) -> dict:
        """Initialize a new game, returning the opening data."""
        self.current_state = ScriptLoader.create_initial_state(self.script)
        self.script_variables.init_state(self.current_state)
        self._init_shop_inventories()
        self.turn_number = 0

        # Build opening base text and choices (synchronous, from script data)
        settings = self.script.get("settings", {})
        fixed_opening = settings.get("fixed_opening", {}).get("default_enabled", False)
        opening = self.script.get("opening", {})

        preset_opening = self.script.get("_preset_opening_text")
        preset_choices_raw = self.script.get("_preset_opening_choices")
        opening_variants = self.script.get("opening_variants", [])
        selected_variant_id = self.script.get("_selected_opening_variant", "")
        if preset_opening:
            narrative = preset_opening
            if preset_choices_raw:
                choices = self._build_opening_choices({"choices": preset_choices_raw})
            else:
                choices = self._build_opening_choices(opening)
        elif opening_variants:
            if selected_variant_id:
                variant = next((v for v in opening_variants if v.get("id") == selected_variant_id), None)
            else:
                variant = None
            if not variant:
                variant = random.choice(opening_variants)
            narrative = variant.get("text", self.script.get("world_background", "游戏开始了。"))
            self.current_state["_opening_variant_id"] = variant.get("id", "")
            v_choices = variant.get("choices")
            choices = self._build_opening_choices({"choices": v_choices}) if v_choices else self._build_opening_choices(opening)
        elif fixed_opening and opening.get("text"):
            narrative = opening["text"]
            choices = self._build_opening_choices(opening)
        else:
            narrative = self.script.get("world_background", "游戏开始了。")
            choices = self._build_opening_choices(opening)

        if not choices:
            choices = self._generate_opening_choices()

        # Roll initial weather if applicable (before AI branch — ctx needs dice_dicts)
        dice_results = self._roll_always_active_dice()
        self._apply_weather(dice_results)

        # 8 阶段 pipeline 开局，失败时回退到旧 3 阶段轻量润色
        _narrative_reasoning = ""
        if self.ai_provider:
            try:
                present_npc_ids, nearby_npc_ids = self._compute_present_npcs(self.current_state)

                world_bg = self.script.get("world_background", "")
                opening_history = f"## 世界背景\n{world_bg}\n\n## 开局骨架（在此基础上大幅扩写润色）\n{narrative}"
                if choices:
                    choices_lines = "\n".join(
                        f"- {c.get('id','')}: {c.get('text','')}" for c in choices
                    )
                    opening_history += f"\n\n## 脚本预设选项（保留核心意图方向，可润色措辞）\n{choices_lines}"

                self._ensure_identity_lore()
                activated_lore, _ = self.prompt_builder.scan_lorebook(
                    narrative, [],
                    timed_state=self.current_state.get("lorebook_timed_state"),
                    turn_number=self.turn_number,
                )

                opening_ctx = {
                    "check_result": {},
                    "dice_dicts": [self._dice_result_to_dict(d) for d in dice_results],
                    "triggered_events": [],
                    "triggered_consequences": [],
                    "achieved_milestones": [],
                    "present_npc_ids": present_npc_ids,
                    "nearby_npc_ids": nearby_npc_ids,
                    "activated_lore": activated_lore,
                    "stage_directives": {},
                    "event_sections": PromptBuilder._format_events_for_prompt(
                        self.event_engine.get_events_for_prompt(self.current_state)
                    ),
                    "old_time": self.current_state.get("game_time", ""),
                    "new_time": self.current_state.get("game_time", ""),
                    "estimated_minutes": 0,
                    "recent_nodes": [],
                    "prev_plot_decision": "",
                    "base_history_context": opening_history,
                    "history_context": opening_history,
                    "context_memory": "",
                    "recent_reasoning": [],
                    "action_text": "游戏开始",
                    "all_state_changes": [],
                }
                opening_route = {
                    "scope": "major",
                    "scene_type": "opening",
                    "systems": None,
                    "focus_npcs": present_npc_ids[:4],
                }
                open_action = {"type": "system", "text": "游戏开始"}

                _narrative_reasoning = ""
                async for item in self._execute_pipeline(
                    opening_ctx, opening_route, open_action,
                ):
                    if item["type"] == "pipeline_result":
                        parsed = item["parsed"]
                        pipeline_narrative = parsed.get("narrative", "")
                        if pipeline_narrative:
                            narrative = pipeline_narrative
                        if parsed.get("choices"):
                            choices = parsed["choices"]
                        _narrative_reasoning = item.get("narrative_reasoning", "")
                        break
                old_attitudes = {
                    nid: nd.get("attitude_toward_player", 50)
                    for nid, nd in self.current_state.get("npcs", {}).items()
                    if isinstance(nd, dict)
                }
                old_faction_reps = {
                    fid: fd.get("value", 50)
                    for fid, fd in self.current_state.get("faction_reputation", {}).items()
                    if isinstance(fd, dict)
                }
                self.current_state, _ = self._apply_common_parsed_changes(
                    self.current_state, parsed, inplace=True,
                    present_npc_ids=present_npc_ids,
                )
                self._check_attitude_thresholds(
                    old_attitudes, parsed.get("npc_attitude_changes", [])
                )
                self._sync_world_changes_to_lorebook(old_attitudes)
                self._check_faction_reputation_events(old_faction_reps)

                # 时间推进
                _old_time = opening_ctx["old_time"]
                ai_end_time = parsed.get("end_time")
                if ai_end_time and _old_time:
                    try:
                        _parsed_end = datetime.fromisoformat(ai_end_time.replace("Z", "+00:00"))
                        _old_dt = datetime.fromisoformat(_old_time.replace("Z", "+00:00"))
                        if _parsed_end > _old_dt:
                            _delta_m = (_parsed_end - _old_dt).total_seconds() / 60
                            if _delta_m <= 2880:
                                self.current_state["game_time"] = ai_end_time
                    except (ValueError, TypeError):
                        pass
                self._compute_time_atmosphere()

                await self._safe_infer_initial_state(narrative)

            except Exception as e:
                logging.getLogger(__name__).warning(
                    "开局 pipeline 失败，回退到轻量润色: %s", e
                )
                personalized_narrative = await self._safe_personalize_narrative(narrative)
                if personalized_narrative:
                    narrative = personalized_narrative
                _infer_ok = await self._safe_infer_initial_state(narrative)
                personalized_choices = await self._safe_personalize_choices(
                    narrative, choices
                )
                if personalized_choices:
                    choices = personalized_choices
                if not _infer_ok:
                    narrative += "\n\n[初始状态推演未成功，背包和持续状态使用了默认配置]"

        # 扫描开局叙事，标记出现过的NPC为 known+met
        self._auto_mark_npcs_known(narrative)

        self.current_state["opening_context"] = narrative[:600] if narrative else ""

        # Index lorebook entries into vector memory for unified semantic retrieval
        if self.vector_memory:
            lore_batch = []
            for entry in self.prompt_builder.lorebook.entries:
                if entry.content and len(entry.content) >= 20:
                    lore_batch.append((entry.id, entry.content, {
                        "entry_type": entry.entry_type,
                        "comment": entry.comment,
                    }))
            if lore_batch:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.vector_memory.add_lorebook_batch, lore_batch)

        node_id = self.world_tree.add_node(
            parent_id=None,
            game_time=self.current_state.get("game_time", ""),
            turn_number=0,
            player_action=None,
            ai_response=narrative,
            choices_presented=choices,
            dice_rolls=[self._dice_result_to_dict(d) for d in dice_results],
            state_snapshot=self.current_state,
        )
        if _narrative_reasoning:
            node = self.world_tree.get_node(node_id)
            if node:
                node["thinking"] = _narrative_reasoning

        # Fire on_start triggers
        if self.story_tree_engine:
            start_result = self.story_tree_engine.evaluate(
                self.current_state, self.turn_number,
                condition_eval=self._evaluate_condition,
                active_events=["on_start"],
            )
            self._apply_story_tree_result(start_result)
        ee_start = self.event_engine.fire_event("on_start", self.current_state, self._evaluate_condition)
        self._apply_event_result(ee_start)

        result = {
            "save_id": self.save_id,
            "node_id": node_id,
            "narrative": narrative,
            "choices": choices,
            "state": self.current_state,
            "dice_rolls": [self._dice_result_to_dict(d) for d in dice_results],
        }
        if _narrative_reasoning:
            result["thinking"] = _narrative_reasoning

        # Opening scene image generation
        if getattr(self, "_image_provider", None) and narrative:
            try:
                from ai.image_prompt_builder import build_image_prompt
                loc = self.current_state.get("player", {}).get("location", "")
                loc_data = self._location_by_id.get(loc, {})
                loc_name = loc_data.get("name", loc) if loc_data else loc
                loc_desc = loc_data.get("description", "") if loc_data else ""
                tod = self.current_state.get("time_of_day", "day")
                weather = self.current_state.get("current_weather", "")

                characters = []
                pc = self.script.get("player_character", {})
                pc_app = pc.get("appearance") or pc.get("bio") or ""
                if pc_app:
                    characters.append(f"Player: {pc_app[:200]}")
                npc_states = self.current_state.get("npcs", {})
                for nid, ns in list(npc_states.items())[:5]:
                    if isinstance(ns, dict) and ns.get("current_location") == loc:
                        npc_def = self._npc_by_id.get(nid, {})
                        desc = npc_def.get("appearance") or npc_def.get("bio") or ""
                        name = ns.get("name") or npc_def.get("name", nid)
                        if desc:
                            characters.append(f"{name}: {desc[:150]}")

                image_style = ""
                try:
                    from api.config_routes import get_image_style
                    style_data = await get_image_style()
                    image_style = style_data.get("custom") or style_data.get("preset") or ""
                except Exception:
                    pass

                img_prompt = await build_image_prompt(
                    narrative, loc_name, "atmospheric", tod, self.ai_provider,
                    weather=weather, characters=characters,
                    location_desc=loc_desc, image_style=image_style,
                )
                scene_img = await self._image_provider.generate_image(img_prompt)
                scene_img["_prompt"] = img_prompt
                result["scene_image"] = scene_img
            except Exception as e:
                logger.warning("Opening scene image generation failed: %s", e)
                result["scene_image"] = None
        else:
            result["scene_image"] = None

        return result

    async def _safe_infer_initial_state(self, narrative: str) -> bool:
        try:
            return await self._infer_initial_state(narrative)
        except Exception as e:
            logging.getLogger(__name__).warning("初始状态推演失败，使用默认值: %s", e)
            return False

    async def _safe_personalize_narrative(self, base_text: str) -> str | None:
        try:
            result = await self._personalize_narrative(base_text)
            min_len = min(80, int(len(base_text) * 0.4))
            if result and len(result) < min_len:
                logging.getLogger(__name__).warning("叙事润色结果过短(%d字，阈值%d)，使用原文", len(result), min_len)
                return None
            return result
        except Exception as e:
            logging.getLogger(__name__).warning("叙事润色失败: %s", e)
            return None

    async def _safe_personalize_choices(self, narrative: str, base_choices: list) -> list | None:
        try:
            return await self._personalize_choices(narrative, base_choices)
        except Exception as e:
            logging.getLogger(__name__).warning("选项润色失败: %s", e)
            return None

    def _ensure_identity_lore(self):
        """从 state 中提取叙事性字段，生成/确认 lorebook 词条（幂等）。"""
        if not self.prompt_builder.lorebook:
            return
        if self.current_state.get("_identity_lore_initialized"):
            return
        existing_ids = {e.id for e in self.prompt_builder.lorebook.entries}
        entries_to_add = []

        # --- 主角档案 ---
        PC_ID = "_pc_identity"
        if PC_ID not in existing_ids:
            pc = self.current_state.get("player", {})
            parts = []
            if pc.get("name"):
                parts.append(f"姓名: {pc['name']}")
            if pc.get("title"):
                parts.append(f"职位: {pc['title']}（NPC应以此称呼主角）")
            if pc.get("bio"):
                parts.append(f"身份: {pc['bio']}")
            if pc.get("personality"):
                parts.append(f"性格: {pc['personality']}")
            if pc.get("long_term_goal"):
                parts.append(f"当前目标: {pc['long_term_goal']}")
            if pc.get("portrait_desc"):
                parts.append(f"外貌: {pc['portrait_desc']}")
            if parts:
                entries_to_add.append({
                    "id": PC_ID, "keys": [pc.get("name", "主角"), "主角"],
                    "content": "【主角档案】\n" + "\n".join(parts),
                    "comment": "主角身份", "entry_type": "pc_identity",
                    "constant": True, "priority": 200, "position": "after_world",
                })

        # --- NPC 档案 ---
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            lore_id = f"_npc_profile_{npc_id}"
            if lore_id in existing_ids:
                continue
            npc_st = self.current_state.get("npcs", {}).get(npc_id, {})
            if not isinstance(npc_st, dict):
                npc_st = {}
            name = npc_st.get("name") or npc.get("name", npc_id)
            bio = npc_st.get("bio") or npc.get("bio", "")
            personality = npc_st.get("personality") or npc.get("personality", "")
            caps = npc_st.get("capabilities") or npc.get("capabilities", "")
            title = npc_st.get("title") or npc.get("title", "")
            if not bio and not personality:
                continue
            parts = [f"【{name}】"]
            if title:
                parts.append(f"职位: {title}")
            if bio:
                parts.append(f"背景: {bio}")
            if personality:
                parts.append(f"性格: {personality}")
            if caps:
                parts.append(f"能力: {caps}")
            entries_to_add.append({
                "id": lore_id, "keys": [name],
                "content": "\n".join(parts),
                "comment": f"NPC:{name}", "entry_type": "npc_profile",
                "priority": 120, "position": "after_world", "scan_depth": 3,
            })

        # --- NPC 关系 ---
        npc_rels = self.current_state.get("npc_relationships_known", {})
        dn = self.current_state.get("display_names", {})
        for key, rel in npc_rels.items():
            lore_id = f"_npc_rel_{key}"
            if lore_id in existing_ids:
                continue
            if not isinstance(rel, dict):
                continue
            if "from" in rel:
                fn = dn.get(rel["from"], rel["from"])
                tn = dn.get(rel["to"], rel["to"])
                desc = rel.get("description", "") if isinstance(rel, dict) else ""
                content = f"{fn}→{tn}: {desc}" if desc else f"{fn}→{tn}"
                keys = [fn, tn]
            else:
                an = dn.get(rel.get("a", ""), rel.get("a", ""))
                bn = dn.get(rel.get("b", ""), rel.get("b", ""))
                desc = rel.get("description", "") if isinstance(rel, dict) else ""
                content = f"{an}↔{bn}: {rel.get('type', '中立')}"
                if desc:
                    content += f"\n{desc}"
                keys = [an, bn]
            entries_to_add.append({
                "id": lore_id, "keys": keys,
                "content": content,
                "comment": "NPC关系", "entry_type": "npc_relationship",
                "priority": 80, "position": "after_world", "scan_depth": 3,
            })

        # --- 地点描述 ---
        for loc_id, loc_desc in self.current_state.get("location_descriptions", {}).items():
            lore_id = f"_loc_desc_{loc_id}"
            if lore_id in existing_ids or not loc_desc:
                continue
            loc_name = dn.get(loc_id, loc_id)
            entries_to_add.append({
                "id": lore_id, "keys": [loc_name, loc_id],
                "content": f"【{loc_name}】\n{loc_desc}",
                "comment": f"地点:{loc_name}", "entry_type": "location_desc",
                "priority": 90, "position": "after_world", "scan_depth": 2,
            })

        # --- 注册并持久化 ---
        if entries_to_add:
            self.prompt_builder.lorebook.add_entries(entries_to_add)
            dynamic = self.current_state.setdefault("dynamic_lorebook", [])
            dynamic.extend(entries_to_add)
            if self.vector_memory:
                batch = [
                    (e["id"], e["content"], {"entry_type": e.get("entry_type", "")})
                    for e in entries_to_add if len(e.get("content", "")) >= 20
                ]
                if batch:
                    self._schedule_background_task(
                        self._async_lorebook_vector_sync(batch)
                    )
        self.current_state["_identity_lore_initialized"] = True

    def _compute_present_npcs(self, state: dict) -> tuple[list[str], list[str]]:
        """计算给定 state 下的在场/附近 NPC 列表（room 感知）。"""
        self._ensure_rooms_initialized()
        player_loc = state.get("player", {}).get("location", "")
        player_room = state.get("player", {}).get("current_room", "")
        present: list[str] = []
        nearby: list[str] = []
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            npc_location = self._get_npc_location(npc_id, state)
            if not (npc_location and self._locations_match(player_loc, npc_location)):
                continue
            if player_room:
                npc_st = state.get("npcs", {}).get(npc_id, {})
                npc_room = npc_st.get("current_room", "") if isinstance(npc_st, dict) else ""
                if npc_room and npc_room != player_room:
                    nearby.append(npc_id)
                    continue
            present.append(npc_id)
        for npc_id, info in state.get("npcs", {}).items():
            if npc_id not in self._npc_by_id and npc_id not in present and npc_id not in nearby:
                if not isinstance(info, dict):
                    continue
                cur_loc = info.get("current_location", info.get("default_location", ""))
                if cur_loc and self._locations_match(player_loc, cur_loc):
                    if player_room:
                        npc_room = info.get("current_room", "")
                        if npc_room and npc_room != player_room:
                            nearby.append(npc_id)
                            continue
                    present.append(npc_id)
        for cid in state.get("companions", []):
            if cid not in present:
                present.append(cid)
        return present, nearby

    def _prepare_turn(self, player_action: dict, *, legacy_prompt: bool = False) -> dict:
        """Prepare all pre-AI data for a turn (dice, events, state changes, prompts).

        Returns a context dict used by process_action / process_action_stream.
        The returned dict includes 'rollback_state' — the state snapshot before
        any modifications, to be used by the caller for rollback protection.

        legacy_prompt: if True, builds the old single-shot narrative system_prompt
        and messages (used by regenerate stream fallback). New 8-stage pipeline
        skips this to save CPU.
        """
        self._mark_activity()
        self._ensure_identity_lore()
        old_time = self.current_state.get("game_time", "")

        # Flow#2: 验证选项 — 检查存在性和锁定状态
        if player_action.get("type") == "choice":
            choice_id = player_action.get("choice_id", "")
            if not choice_id.startswith("open_"):
                active_node = self.world_tree.get_node(self.world_tree.active_node_id)
                if active_node:
                    presented = active_node.get("choices_presented", [])
                    matched = next((c for c in presented if c.get("id") == choice_id), None)
                    if presented and not matched:
                        raise ValueError(f"选项不存在于当前展示列表: {choice_id}")
                    if matched and matched.get("locked"):
                        raise ValueError(f"该选项已锁定: {matched.get('text', choice_id)}（{matched.get('lock_reason', '条件不满足')}）")

        # B1: turn_number 在校验通过后才推进，校验失败时不消耗回合数
        self.turn_number += 1

        opening_result = self._check_opening_choice(player_action)
        opening_description = opening_result.get("description", "") if opening_result else ""

        # 时间预估：仅用于事件检查窗口，最终时间由 Stage 4b AI 决定
        action_type = player_action.get("type", "freeform")
        action_text = player_action.get("text", "")
        if action_type == "choice":
            estimated_minutes = self._get_choice_time_hint(player_action) or 15
        else:
            estimated_minutes = 30
            _at = action_text
            # 尝试从文本中提取明确的时间量（如"等待1小时"、"等30分钟"）
            _explicit_time = re.search(r'(\d+)\s*(小时|时|hour|h)', _at)
            _explicit_min = re.search(r'(\d+)\s*(分钟|分|minute|min|m)', _at)
            if _explicit_time:
                estimated_minutes = int(_explicit_time.group(1)) * 60
                if _explicit_min:
                    estimated_minutes += int(_explicit_min.group(1))
            elif _explicit_min:
                estimated_minutes = int(_explicit_min.group(1))
            elif any(w in _at for w in ("睡", "休息", "过夜", "扎营", "入睡")):
                estimated_minutes = 480
            elif any(w in _at for w in ("等待", "等", "候", "守")):
                estimated_minutes = 60
            elif any(w in _at for w in ("前往", "出发", "赶路", "旅行", "骑马", "驾车", "乘")):
                estimated_minutes = 180
            elif any(w in _at for w in ("观察", "查看", "翻阅", "检查", "环顾", "阅读", "端详")):
                estimated_minutes = 15
            elif any(w in _at for w in ("说", "问", "回答", "告诉", "聊", "谈", "交谈", "搭话")):
                estimated_minutes = 10

        estimated_advance = timedelta(minutes=estimated_minutes)
        new_time = self._advance_game_time(old_time, estimated_advance)
        # P-1: 时间解析失败时 new_time == old_time，警告并继续
        if new_time == old_time and old_time:
            logging.getLogger(__name__).warning(
                "游戏时间未推进（可能格式错误），old_time=%r", old_time
            )

        triggered_events = self.event_scheduler.check_events(
            self.current_state, old_time, new_time,
            condition_eval=self._evaluate_condition,
        )
        # 先捕获 rollback_state（event_engine 会就地修改 state）
        try:
            rollback_state = json.loads(json.dumps(self.current_state, ensure_ascii=False))
        except (TypeError, ValueError):
            rollback_state = copy.deepcopy(self.current_state)
        # Unified event engine tick (parallel with legacy systems during migration)
        event_engine_result = self.event_engine.tick(
            self.current_state, self.turn_number,
            game_time=new_time, old_time=old_time,
            condition_eval=self._evaluate_condition,
            player_action=player_action.get("text", ""),
            state_manager=self.state_manager,
        )
        self._apply_event_result(event_engine_result)

        self.current_state, expired = self.state_manager.check_expirations(
            self.current_state, new_time, inplace=True
        )

        # Roll dice
        dice_results = self._roll_always_active_dice()
        self._apply_weather(dice_results)
        dice_results.extend(self._roll_event_linked_dice(triggered_events))
        dice_results.extend(self._roll_conditional_dice())

        # Apply opening choice + dice state changes
        all_state_changes = []
        if opening_result:
            opening_changes = opening_result.get("state_changes", [])
            if opening_changes:
                self.current_state, log = self.state_manager.apply_changes(
                    self.current_state, opening_changes, inplace=True
                )
                all_state_changes.extend(log)
            # Persist opening choice result in state for later turns
            if opening_description:
                prev = self.current_state.get("opening_context", "")
                self.current_state["opening_context"] = (
                    prev + "\n\n[玩家开局选择] " + player_action.get("text", "") +
                    " → " + opening_description
                ).strip()[:800]

        dice_enabled = self.current_state.get("dice_check_enabled", True)
        for dr in dice_results:
            if dr.range_state_changes:
                if not dice_enabled and getattr(dr, 'random_item_id', '') != 'weather':
                    continue
                self.current_state, log = self.state_manager.apply_changes(
                    self.current_state, dr.range_state_changes, inplace=True
                )
                all_state_changes.extend(log)

        # Consequences from event engine (already processed in tick above)
        action_text_raw = player_action.get("text", "")
        triggered_consequences = event_engine_result.triggered_consequences
        check_result = None
        achieved_milestones, milestone_reward_changes = self._check_milestones()
        all_state_changes.extend(milestone_reward_changes)
        milestone_progress = self._check_milestone_progress()

        # Fire milestone events to story tree and event engine before evaluation
        if achieved_milestones:
            for ms in achieved_milestones:
                ms_id = ms.get("id", "")
                if ms_id:
                    self._fire_event_dual(f"milestone.{ms_id}")

        # Fire scheduled events to story tree (event_engine already processed in tick)
        if self.story_tree_engine and triggered_events:
            for evt in triggered_events:
                eid = evt.get("event_id", "")
                if eid:
                    self.story_tree_engine.fire_event(
                        f"event.{eid}", self.current_state,
                        condition_eval=self._evaluate_condition,
                    )
                evt_def = self._event_def_by_id.get(eid)
                if isinstance(evt_def, dict):
                    for fe in evt_def.get("fire_events", []):
                        self.story_tree_engine.fire_event(
                            fe, self.current_state,
                            condition_eval=self._evaluate_condition,
                        )

        # Event def effects deferred to _apply_parsed_response (after AI finalizes time)

        # Choice → quest immediate feedback: match choice text against active quest completion_keywords
        if player_action.get("text"):
            self._check_quest_keywords(player_action["text"])

        # Story tree evaluation
        story_tree_result = None
        if self.story_tree_engine:
            story_tree_result = self.story_tree_engine.evaluate(
                self.current_state, self.turn_number,
                condition_eval=self._evaluate_condition,
            )
            self._apply_story_tree_result(story_tree_result)

        self._run_turn_computations(story_tree_result, check_result, achieved_milestones)

        # Build AI prompts
        if not dice_enabled:
            dice_results = [dr for dr in dice_results if getattr(dr, 'random_item_id', '') == 'weather']
        dice_dicts = [self._dice_result_to_dict(d) for d in dice_results]
        recent = self.world_tree.get_recent_history(6)
        recent_messages = [n.get("ai_response", "") for n in recent]
        action_text = player_action.get("text", "")
        action_text = self.regex_engine.apply(action_text, "user_input")
        activated_lore, new_lore_ts = self.prompt_builder.scan_lorebook(
            action_text, recent_messages,
            timed_state=self.current_state.get("lorebook_timed_state"),
            turn_number=self.turn_number,
        )
        self.current_state["lorebook_timed_state"] = new_lore_ts

        # 地点/时间感知激活（不依赖关键词匹配）
        _loc_id = self.current_state.get("player", {}).get("location", "")
        _gt = self.current_state.get("game_time", "")
        context_lore = self.prompt_builder.lorebook.activate_by_context(
            location_id=_loc_id, game_time=_gt,
            already_activated={e.id for e in activated_lore},
        )
        activated_lore.extend(context_lore)

        # 触发事件关联 lorebook 条目激活
        if triggered_events and self.prompt_builder.lorebook:
            _already = {e.id for e in activated_lore}
            _evt_texts = [ev.get("description", "") for ev in triggered_events if ev.get("description")]
            if _evt_texts:
                _evt_lore, _ = self.prompt_builder.lorebook.scan(
                    " ".join(_evt_texts), [], timed_state=None,
                )
                for _el in _evt_lore:
                    if _el.id not in _already:
                        _el.priority = min(_el.priority + 20, 200)
                        activated_lore.append(_el)
                        _already.add(_el.id)

        # 剧情树关联 lorebook 条目优先级提升
        _story_lore_ids: set = set()
        if self.story_tree_engine and activated_lore:
            _story_keywords = set()
            sts = self.current_state.get("story_tree_state", {})
            for _sn_id in list(sts.get("active", [])):
                _sn = self.story_tree_engine._nodes.get(_sn_id)
                if _sn:
                    for _rid in _sn.get("related_npcs", []):
                        _story_keywords.add(_rid)
                        _rname = self._get_npc_or_org_name(_rid)
                        if _rname:
                            _story_keywords.add(_rname.lower())
                    for _oid in _sn.get("related_orgs", []):
                        _story_keywords.add(_oid)
                        _oname = self._get_npc_or_org_name(_oid)
                        if _oname:
                            _story_keywords.add(_oname.lower())
            if _story_keywords:
                _story_lore_ids = set()
                for _le in activated_lore:
                    if any(k.lower() in _story_keywords for k in _le.keys if k):
                        _le.priority = min(_le.priority + 30, 200)
                        _story_lore_ids.add(_le.id)
                activated_lore.sort(key=lambda e: e.priority, reverse=True)

        # 始终构建历史上下文和在场NPC（8阶段 + legacy 都需要）
        history_summary = self.current_state.get("history_summary", "")
        history_context = self.prompt_builder.build_history_context(recent, history_summary)
        context_memory = self.prompt_builder.build_context_memory(recent, self.current_state)

        # 宏展开: 对历史上下文和上下文记忆中的变量引用做替换
        history_context = self.script_variables.expand_macros(history_context, self.current_state)
        context_memory = self.script_variables.expand_macros(context_memory, self.current_state)

        # Reasoning 回注: 收集前几轮的剧情决策思路（可配置 lookback）
        _reasoning_lookback = self.script.get("settings", {}).get("reasoning_lookback", 2)
        recent_reasoning = []
        prev_plot_decision = ""
        for node in recent[-_reasoning_lookback:]:
            r = node.get("plot_reasoning", "")
            if r:
                recent_reasoning.append({
                    "turn": node.get("turn_number", 0),
                    "reasoning": r[:300],
                })
        if recent:
            prev_plot_decision = recent[-1].get("plot_decision", "")

        # 确保 NPC 房间信息已初始化
        self._ensure_rooms_initialized()

        # 计算在场NPC（room 感知：同房间=在场，同建筑不同房间=nearby）
        present_npc_ids, nearby_npc_ids = self._compute_present_npcs(self.current_state)

        # 叙事专用 system prompt（仅 legacy 模式构建）
        system_prompt = None
        messages = None
        _macro_exp = lambda t: self.script_variables.expand_macros(t, self.current_state)
        _event_data = self.event_engine.get_events_for_prompt(self.current_state)
        _event_sections = PromptBuilder._format_events_for_prompt(_event_data)
        if legacy_prompt:
            system_prompt = self.prompt_builder.build_narrative_system_prompt(
                self.current_state,
                activated_lore=activated_lore,
                authors_note=self.authors_note,
                turn_number=self.turn_number,
                authors_note_position=self.authors_note_position,
                macro_expander=_macro_exp,
                negative_prompt=getattr(self, "negative_prompt", ""),
                event_sections=_event_sections,
                pc_discovered_lore=self.current_state.get("pc_discovered_lore", []),
            )
            if history_context:
                system_prompt += "\n\n---\n\n" + _macro_exp(history_context)

            # D2: 注入近期上下文要点，帮助AI保持NPC/道具/地点一致性
            if context_memory:
                system_prompt += "\n\n" + context_memory

        user_message = self.prompt_builder.build_user_message(
            player_action, new_time, triggered_events, dice_dicts,
            check_result=check_result,
            triggered_consequences=triggered_consequences,
            achieved_milestones=achieved_milestones,
            opening_description=opening_description,
            state=self.current_state,
            event_sections=_event_sections,
        )

        if legacy_prompt:
            history_messages = self.prompt_builder.build_history_messages(
                recent, self.current_state.get("history_summary", ""),
                chapter_summaries=self.current_state.get("adventure_log", {}).get("chapter_summaries"),
            )
            messages = history_messages + [{"role": "user", "content": user_message}]
            # Author's Note at_depth injection
            if self.authors_note and self.authors_note_position == "at_depth":
                an_msg = {"role": "system", "content": f"[创作指令] {self.authors_note}"}
                idx = max(0, len(messages) - self.authors_note_depth)
                messages.insert(idx, an_msg)

        # Fire before_generation triggers (via unified story tree + event engine)
        bg_inject, bg_notifications = self._fire_lifecycle_event("before_generation")
        if bg_inject:
            extra = "\n".join(bg_inject)
            if system_prompt:
                system_prompt += "\n\n" + extra
            history_context = (history_context + "\n\n## 触发器注入\n" + extra).strip()

        # Inject story tree prompts into system prompt
        all_notifications = list(bg_notifications)
        if story_tree_result:
            if story_tree_result.inject_prompts:
                st_extra = "\n".join(story_tree_result.inject_prompts)
                if system_prompt:
                    system_prompt += "\n\n" + st_extra
                history_context = (history_context + "\n\n## 剧情推进\n" + st_extra).strip()
            all_notifications.extend(story_tree_result.notifications)

        # Inject event engine prompts into context
        if event_engine_result.inject_prompts:
            ee_extra = "\n".join(event_engine_result.inject_prompts)
            if system_prompt:
                system_prompt += "\n\n" + ee_extra
            history_context = (history_context + "\n\n## 事件系统注入\n" + ee_extra).strip()
        all_notifications.extend(event_engine_result.notifications)

        # 收集活跃剧情线的阶段指令
        stage_directives: dict[str, list[str]] = {}
        # From event engine active events
        _es = self.current_state.get("events", {})
        for eid, evs in _es.items():
            if evs.get("status") != "active":
                continue
            ev = self.event_engine.events.get(eid)
            if ev and hasattr(ev, "metadata") and ev.metadata.get("stage_directives"):
                for stage, directive in ev.metadata["stage_directives"].items():
                    stage_directives.setdefault(stage, []).append(directive)
        # Also from legacy story tree
        if self.story_tree_engine:
            sts = self.current_state.get("story_tree_state", {})
            for nid in sts.get("active", []):
                node = self.story_tree_engine._nodes.get(nid)
                if node and node.get("stage_directives"):
                    for stage, directive in node["stage_directives"].items():
                        stage_directives.setdefault(stage, []).append(directive)

        return {
            "old_time": old_time,
            "new_time": new_time,
            "estimated_minutes": estimated_minutes,
            "triggered_events": triggered_events,
            "initial_event_count": len(triggered_events),
            "expired": expired,
            "all_state_changes": all_state_changes,
            "triggered_consequences": triggered_consequences,
            "check_result": check_result,
            "achieved_milestones": achieved_milestones,
            "milestone_progress": milestone_progress,
            "dice_dicts": dice_dicts,
            "activated_lore": activated_lore,
            "system_prompt": system_prompt,
            "user_message": user_message,
            "messages": messages,
            "rollback_state": rollback_state,
            "present_npc_ids": present_npc_ids,
            "nearby_npc_ids": nearby_npc_ids,
            "history_context": history_context,
            "base_history_context": history_context,
            "context_memory": context_memory,
            "recent_nodes": recent,
            "recent_reasoning": recent_reasoning,
            "prev_plot_decision": prev_plot_decision,
            "trigger_notifications": all_notifications,
            "story_tree_updates": story_tree_result,
            "stage_directives": stage_directives,
            "story_lore_ids": _story_lore_ids,
            "event_sections": _event_sections,
            "action_text": action_text,
        }

    async def _apply_parsed_response(
        self, parsed: dict, raw_response: str, player_action: dict, ctx: dict,
    ) -> dict:
        """Apply parsed AI response to game state and finalize the turn.

        Returns the full result dict.
        """
        # FLOW-3: Replace generic fallback choices with context-aware ones
        _GENERIC_TEXTS = {"继续探索", "与周围的人交谈", "做其他事情", "环顾四周", "离开这里"}
        choices = parsed.get("choices", [])
        if choices:
            generic_indices = [i for i, c in enumerate(choices) if c.get("text") in _GENERIC_TEXTS]
            if generic_indices:
                if len(generic_indices) == len(choices):
                    # 全部泛化：整体替换
                    parsed["choices"] = self._generate_context_choices()
                else:
                    # 部分泛化：逐条替换
                    context_choices = self._generate_context_choices()
                    for idx in generic_indices:
                        # 从 context_choices 中取一个未出现在当前列表中的替代
                        replacement = None
                        for cc in context_choices:
                            if not any(c.get("text") == cc["text"] for c in choices):
                                replacement = cc
                                break
                        if replacement:
                            replacement["id"] = choices[idx].get("id", f"c{idx+1}")
                            choices[idx] = replacement

        new_time = ctx["new_time"]
        triggered_events = ctx["triggered_events"]
        all_state_changes = ctx["all_state_changes"]

        # Post-processing rules
        rules = self.script.get("post_processing_rules", [])
        if rules:
            parsed["narrative"] = self._apply_post_processing(
                parsed.get("narrative", raw_response), rules
            )

        # Regex scripts on AI output
        if parsed.get("narrative"):
            parsed["narrative"] = self.regex_engine.apply(parsed["narrative"], "ai_output")

        # O-1: 复用共享方法处理通用 state 变更（state_changes / npc_attitude /
        # persistent_states / world_properties / inventory / consequences /
        # offscreen NPC / dynamic NPCs / npc_relationships / reveal_locations /
        # location_change + travel_time）
        old_attitudes = {
            nid: nd.get("attitude_toward_player", 50)
            for nid, nd in self.current_state.get("npcs", {}).items()
            if isinstance(nd, dict)
        }
        old_faction_reps = {
            fid: fd.get("value", 50)
            for fid, fd in self.current_state.get("faction_reputation", {}).items()
            if isinstance(fd, dict)
        }
        self.current_state, common_changes = self._apply_common_parsed_changes(
            self.current_state, parsed, inplace=True,
            present_npc_ids=ctx.get("present_npc_ids"),
        )
        all_state_changes.extend(common_changes)
        npc_attitude_notifications = self._check_attitude_thresholds(
            old_attitudes, parsed.get("npc_attitude_changes", [])
        )
        self._sync_world_changes_to_lorebook(old_attitudes)
        self._check_faction_reputation_events(old_faction_reps)

        # 记录场景中有态度变化的 NPC 到 interaction_log
        npc_att_changes = parsed.get("npc_attitude_changes", [])
        if npc_att_changes:
            action_text = player_action.get("text", "")
            interaction_log = self.current_state.setdefault("npc_interaction_log", {})
            for ac in npc_att_changes:
                npc_id = ac.get("npc_id", "")
                if not npc_id:
                    continue
                npc_ilog = interaction_log.setdefault(npc_id, [])
                npc_ilog.append({
                    "turn": self.turn_number,
                    "context": action_text[:60],
                    "attitude_delta": ac.get("change", 0),
                })
                if len(npc_ilog) > 15:
                    interaction_log[npc_id] = npc_ilog[-10:]

        # 自动标记已出场NPC为 known=true，避免AI反复"首次出场"
        narrative_text = parsed.get("narrative", raw_response)
        self._auto_mark_npcs_known(narrative_text)

        # Time advance from AI (Stage 4b is the sole decision-maker)
        # end_time: AI 直接输出叙事结束时的绝对时间戳
        ai_end_time = parsed.get("end_time")
        # 向后兼容：旧格式 time_advance (ISO 8601 duration)
        ai_time_advance = parsed.get("time_advance") if not ai_end_time else None
        pre_ai_time = new_time
        if ai_end_time:
            try:
                _parsed_end = datetime.fromisoformat(ai_end_time.replace("Z", "+00:00"))
                _old_dt = datetime.fromisoformat(ctx["old_time"].replace("Z", "+00:00"))
                if _parsed_end <= _old_dt:
                    logger.warning("AI end_time=%s <= old_time=%s，使用预估时间", ai_end_time, ctx["old_time"])
                else:
                    _delta_minutes = (_parsed_end - _old_dt).total_seconds() / 60
                    _est = ctx.get("estimated_minutes", 30)
                    _max_reasonable = max(_est * 10, 120)
                    if _delta_minutes > 2880:
                        logger.warning("AI end_time=%s 超过48小时，钳位", ai_end_time)
                        new_time = self._advance_game_time(ctx["old_time"], timedelta(hours=48))
                    elif _delta_minutes > _max_reasonable:
                        _clamped = max(_est * 3, 60)
                        logger.warning(
                            "AI end_time=%s 推进%.0f分钟，远超预估%d分钟，钳位到%d分钟",
                            ai_end_time, _delta_minutes, _est, _clamped,
                        )
                        new_time = self._advance_game_time(ctx["old_time"], timedelta(minutes=_clamped))
                    else:
                        new_time = ai_end_time
            except (ValueError, TypeError) as e:
                logger.warning("AI end_time=%r 格式错误: %s", ai_end_time, e)
        elif ai_time_advance:
            ai_new_time = self._apply_iso_duration(ctx["old_time"], ai_time_advance)
            ai_minutes = self._parse_duration_minutes(ai_time_advance)
            if ai_minutes is not None:
                if ai_minutes < 5:
                    ai_new_time = self._advance_game_time(ctx["old_time"], timedelta(minutes=5))
                elif ai_minutes > 2880:
                    ai_new_time = self._advance_game_time(ctx["old_time"], timedelta(hours=48))
            new_time = ai_new_time

        # Location change travel time
        if parsed.get("location_change"):
            travel_time = self._get_location_travel_time(parsed["location_change"])
            if travel_time:
                new_time = self._apply_iso_duration(new_time, travel_time)

        # Re-check events if AI extended time beyond initial estimate
        if new_time != pre_ai_time:
            extra_events = self.event_scheduler.check_events(
                self.current_state, pre_ai_time, new_time,
                condition_eval=self._evaluate_condition,
            )
            existing_keys = {(e["event_id"], e["fire_time"]) for e in triggered_events}
            for e in extra_events:
                if (e["event_id"], e["fire_time"]) not in existing_keys:
                    triggered_events.append(e)
            # Fire extra events to story tree (same as _prepare_turn does for initial events)
            if self.story_tree_engine and extra_events:
                for evt in extra_events:
                    eid = evt.get("event_id", "")
                    if eid:
                        self.story_tree_engine.fire_event(
                            f"event.{eid}", self.current_state,
                            condition_eval=self._evaluate_condition,
                        )
                    evt_def = self._event_def_by_id.get(eid)
                    if isinstance(evt_def, dict):
                        for fe in evt_def.get("fire_events", []):
                            self.story_tree_engine.fire_event(
                                fe, self.current_state,
                                condition_eval=self._evaluate_condition,
                            )

        # Filter out events whose fire_time exceeds final time (estimated was too large)
        final_time = new_time
        if final_time and ctx["old_time"]:
            valid_events = []
            for evt in triggered_events:
                ft = evt.get("fire_time", "")
                if not ft or ft <= final_time:
                    valid_events.append(evt)
                else:
                    logger.info("事件 %s fire_time=%s 超出最终时间 %s，跳过",
                                evt.get("event_id"), ft, final_time)
            triggered_events[:] = valid_events

        # Apply effects from event definitions (deferred from _prepare_turn)
        for evt in triggered_events:
            eid = evt.get("event_id", "")
            evt_def = self._event_def_by_id.get(eid)
            if isinstance(evt_def, dict):
                log = self._apply_event_def_effects(evt_def, eid)
                all_state_changes.extend(log)

        # B8: 确保 reveal_locations 中的动态地点有 display_name
        for loc_entry in parsed.get("reveal_locations", []):
            if isinstance(loc_entry, dict):
                loc_id = loc_entry.get("id", "")
                loc_name = loc_entry.get("name", loc_id)
            else:
                loc_id = loc_entry
                loc_name = self._location_by_id.get(loc_id, {}).get("name", loc_id)
            if not loc_id:
                continue
            dn = self.current_state.setdefault("display_names", {})
            if loc_id not in dn:
                dn[loc_id] = loc_name
        self._apply_weather_effects()
        threshold_events = self._check_attribute_thresholds(all_state_changes)

        # Finalize time and trackers
        self.current_state["game_time"] = new_time
        self._compute_time_atmosphere()
        self.current_state = self.event_scheduler.update_trackers(
            self.current_state, triggered_events, new_time, inplace=True
        )

        # Adventure log
        self._update_adventure_log(
            player_action, parsed, triggered_events,
            ctx["triggered_consequences"], ctx["achieved_milestones"],
            threshold_events, all_state_changes=all_state_changes,
        )
        self._mark_used_callbacks(parsed.get("narrative", ""))
        self._mark_used_interactables(player_action)
        self._update_pacing_state(ctx)
        self._track_world_pulse(ctx)
        self._record_information(parsed, ctx)
        self._propagate_information()
        self._consolidate_information_memory()
        self._check_memory_echoes(ctx)
        self._trace_choice_ripples(ctx)
        self._compute_compose_feedback(
            parsed.get("narrative", raw_response), ctx,
        )

        game_over = parsed.get("game_over")

        # Build game statistics when game ends
        game_statistics = None
        if game_over:
            game_statistics = {
                "total_turns": self.turn_number,
                "play_time_seconds": self.current_state.get("play_time_seconds", 0),
                "locations_visited": len(self.current_state.get("visible_locations", [])),
                "npcs_met": sum(
                    1 for n in self.current_state.get("npcs", {}).values()
                    if isinstance(n, dict) and n.get("met", n.get("known", False))
                ),
                "milestones_achieved": len(self.current_state.get("achieved_milestones", [])),
                "items_collected": len(self.current_state.get("inventory", [])),
                "ending_type": game_over.get("ending_type", "") if isinstance(game_over, dict) else "",
            }

        # Refresh NPC current_location so the frontend always has up-to-date positions
        for npc_id, npc_data in self.current_state.get("npcs", {}).items():
            if not isinstance(npc_data, dict):
                continue
            resolved_loc = self._get_npc_location(npc_id)
            if resolved_loc:
                npc_data["current_location"] = resolved_loc

        # World tree node
        node_id = self.world_tree.add_node(
            parent_id=self.world_tree.active_node_id
            if self.turn_number > 1
            else self.world_tree.root_node_id,
            game_time=new_time,
            turn_number=self.turn_number,
            player_action=player_action,
            ai_response=parsed.get("narrative", raw_response),
            choices_presented=parsed.get("choices", []),
            dice_rolls=ctx["dice_dicts"],
            state_changes=all_state_changes,
            triggered_events=[e["event_id"] for e in triggered_events],
            state_snapshot=self.current_state,
        )
        # Bug-1: 将 regenerate 需要的上下文写入节点，避免重新生成时丢失
        node = self.world_tree.get_node(node_id)
        if node:
            node["check_result"] = ctx["check_result"]
            node["triggered_consequences"] = ctx["triggered_consequences"]
            node["achieved_milestones"] = ctx["achieved_milestones"]
            if ctx.get("plot_reasoning"):
                node["plot_reasoning"] = ctx["plot_reasoning"]
            if ctx.get("plot_decision"):
                node["plot_decision"] = ctx["plot_decision"]
            node["_pipeline_ctx"] = {
                "check_result": ctx["check_result"],
                "dice_dicts": ctx["dice_dicts"],
                "triggered_events": ctx["triggered_events"],
                "triggered_consequences": ctx["triggered_consequences"],
                "achieved_milestones": ctx["achieved_milestones"],
                "present_npc_ids": ctx["present_npc_ids"],
                "nearby_npc_ids": ctx["nearby_npc_ids"],
                "activated_lore": ctx["activated_lore"],
                "stage_directives": ctx["stage_directives"],
                "event_sections": ctx["event_sections"],
                "old_time": ctx["old_time"],
                "estimated_minutes": ctx["estimated_minutes"],
                "recent_nodes": ctx["recent_nodes"],
                "prev_plot_decision": ctx["prev_plot_decision"],
                "base_history_context": ctx["base_history_context"],
                "context_memory": ctx.get("context_memory", ""),
                "recent_reasoning": ctx.get("recent_reasoning", []),
                "story_lore_ids": ctx.get("story_lore_ids"),
                "history_context": ctx.get("history_context", ""),
                "lore_ids_from_rag": ctx.get("lore_ids_from_rag"),
                "databank_hits": ctx.get("databank_hits", []),
            }
            if ctx.get("route"):
                node["_pipeline_route"] = ctx["route"]

        # Urgent lorebook evolution on significant changes
        if parsed.get("location_change") or parsed.get("new_npcs"):
            self.current_state["_pending_lore_evolution"] = True

        # Feature #1: Consume scene hijack and extend cooldown
        hijack = self.current_state.pop("_scene_hijack", None)
        if hijack:
            cooldowns = self.current_state.setdefault("npc_intervention_cooldowns", {})
            cooldowns[hijack.get("npc_id", "")] = self.turn_number + 10

        # Feature #3: Initialize plan from plot decision if declared
        route = ctx.get("route", {})
        if route.get("has_plan_declaration") and self.current_state.get("pending_plan", {}).get("status") != "active":
            plan_steps = self._extract_plan_steps(ctx.get("plot_decision", ""))
            if plan_steps:
                self.current_state["pending_plan"] = {
                    "id": f"plan_{self.turn_number}",
                    "declared_turn": self.turn_number,
                    "goal": player_action.get("text", "")[:80],
                    "steps_remaining": plan_steps[:5],
                    "steps_completed": [],
                    "status": "active",
                    "risk_level": self._extract_plan_risk(ctx.get("plot_decision", "")),
                }

        # Feature #5: Check for flashback-triggering lore reveals
        seen_flashbacks = self.current_state.get("_seen_flashback_lore", [])
        for lore_entry in ctx.get("activated_lore", []):
            lore_id = lore_entry.id if hasattr(lore_entry, "id") else ""
            if not lore_id or lore_id in seen_flashbacks:
                continue
            has_flashback = (
                (hasattr(lore_entry, "discoverable") and lore_entry.discoverable.get("flashback"))
                or (hasattr(lore_entry, "visibility") and lore_entry.visibility == "hidden")
            )
            if has_flashback:
                content = lore_entry.content if hasattr(lore_entry, "content") else ""
                self.current_state["_pending_flashback"] = {
                    "lore_id": lore_id,
                    "content": content[:500],
                }
                self.current_state.setdefault("_seen_flashback_lore", []).append(lore_id)
                # Cap seen list
                if len(self.current_state["_seen_flashback_lore"]) > 50:
                    self.current_state["_seen_flashback_lore"] = self.current_state["_seen_flashback_lore"][-50:]
                break

        # Unified post-turn dispatch via MetaEventBus
        self._dispatch_meta_events(ctx, parsed, player_action, raw_response)

        # Vector memory: store this turn's content for future semantic retrieval
        if self.vector_memory:
            narrative = parsed.get("narrative", raw_response)
            vm_text = f"{player_action.get('text', '')} → {narrative}"
            lore_ids_str = ",".join(
                e.id for e in ctx.get("activated_lore", [])[:20]
            ) if ctx.get("activated_lore") else ""
            vm_meta = {
                "turn_number": self.turn_number,
                "game_time": self.current_state.get("game_time", ""),
                "location": self.current_state.get("player", {}).get("location", ""),
                "active_lore_ids": lore_ids_str,
            }
            self._schedule_background_task(self._async_vector_store(node_id, vm_text, vm_meta))

        # 提取 NPC 表情标签和主动发言（Stage 4a 已返回）
        npc_expressions = []
        npc_interjections = parsed.get("npc_interjections", [])
        sd = self.current_state.get("scene_details")
        if sd and isinstance(sd, dict):
            npc_expressions = sd.get("npc_expressions", [])

        self._enrich_choice_previews(parsed.get("choices", []))

        result = {
            "node_id": node_id,
            "narrative": parsed.get("narrative", raw_response),
            "choices": parsed.get("choices", []),
            "state": self.current_state,
            "dice_rolls": ctx["dice_dicts"],
            "state_changes": all_state_changes,
            "triggered_events": triggered_events,
            "expired_states": ctx["expired"],
            "check_result": ctx["check_result"],
            "triggered_consequences": ctx["triggered_consequences"],
            "achieved_milestones": ctx["achieved_milestones"],
            "milestone_progress": ctx.get("milestone_progress", []),
            "threshold_events": threshold_events,
            "npc_attitude_notifications": npc_attitude_notifications,
            "game_over": game_over,
            "game_statistics": game_statistics,
            "npc_expressions": npc_expressions,
            "npc_interjections": npc_interjections,
            "activated_lore": [
                {"id": e.id, "comment": e.comment, "content": e.content}
                for e in (ctx["activated_lore"] or []) if e.comment or e.content
            ],
            "databank_hits": [
                {"text": h["text"][:200], "filename": h.get("filename", "")}
                for h in ctx.get("databank_hits", [])
            ],
        }
        if parsed.get("_state_parse_failed"):
            result["warnings"] = ["状态推演解析失败，本回合属性/物品/NPC态度未更新。"]

        # Fire after_ai triggers (via unified story tree + event engine)
        _, aa_notifications = self._fire_lifecycle_event("after_ai")
        if aa_notifications:
            result.setdefault("trigger_notifications", []).extend(aa_notifications)

        # Add before_generation notifications from ctx
        if ctx.get("trigger_notifications"):
            result.setdefault("trigger_notifications", []).extend(ctx["trigger_notifications"])

        # Story tree updates for frontend
        st_result = ctx.get("story_tree_updates")
        if st_result:
            result["story_tree_updates"] = {
                "newly_completed": [{"id": n["id"], "name": n.get("name", n["id"])} for n in st_result.newly_completed],
                "newly_active": [{"id": n["id"], "name": n.get("name", n["id"]), "type": n.get("type")} for n in st_result.newly_active],
            }

        # Generate news from completed story nodes + triggered one-time events
        # Only use events from the initial time window (_prepare_turn), not from
        # AI time-jump re-checks, to prevent future events leaking into current news.
        try:
            news_sources = []
            if st_result and st_result.newly_completed:
                for n in st_result.newly_completed:
                    news_sources.append({"type": "story_node", "name": n.get("name", n.get("id", "")), "description": n.get("description", "")})
            ot_ids = {e["id"] for e in self.script.get("one_time_events", []) if isinstance(e, dict) and e.get("id")}
            initial_count = ctx.get("initial_event_count", len(ctx.get("triggered_events") or []))
            initial_events = (ctx.get("triggered_events") or [])[:initial_count]
            for evt in initial_events:
                eid = evt.get("event_id", "")
                if eid in ot_ids:
                    evt_def = self._event_def_by_id.get(eid)
                    desc = evt.get("description", "")
                    if not desc and isinstance(evt_def, dict):
                        desc = evt_def.get("description", "")
                    news_sources.append({"type": "one_time_event", "name": (evt_def or {}).get("name", eid), "description": desc})
            if news_sources and self.ai_provider:
                news = await self._generate_event_news(news_sources)
                if news:
                    feed = self.current_state.setdefault("news_feed", [])
                    feed.append(news)
                    if len(feed) > 50:
                        self.current_state["news_feed"] = feed[-50:]
                    result["news"] = [news]
        except Exception:
            pass

        # Emotion classification (non-blocking: if it fails, no emotion label)
        narrative_text = result.get("narrative", "")
        if narrative_text and self.ai_provider:
            try:
                emotion = await self._classify_emotion(narrative_text)
                if emotion:
                    result["emotion"] = emotion
            except Exception:
                pass

        return result

    async def _classify_emotion(self, narrative: str) -> str:
        """Classify narrative emotion using a quick AI call."""
        prompt = (
            "从以下标签中选择最匹配的情绪：joy, sadness, anger, fear, surprise, "
            "love, tension, calm, excitement, melancholy。只输出一个英文词。\n\n"
            + narrative[-300:]
        )
        result = await self.ai_provider.generate(
            [{"role": "user", "content": prompt}], max_tokens=10
        )
        if not result:
            return ""
        parts = result.strip().lower().split()
        if not parts:
            return ""
        word = parts[0]
        valid = {"joy", "sadness", "anger", "fear", "surprise", "love", "tension", "calm", "excitement", "melancholy"}
        return word if word in valid else ""

    async def _generate_event_news(self, sources: list[dict]) -> dict | None:
        """Generate a news article from triggered events using AI."""
        if not self.ai_provider or not sources:
            return None
        try:
            bg = self.script.get("background", "")[:300]
            game_time = self.current_state.get("game_time", "")
            event_desc = "\n".join(f"- [{s['type']}] {s['name']}: {s.get('description', '')}" for s in sources)
            prompt = (
                f"根据以下游戏事件，写一条符合游戏世界观的新闻快讯。\n"
                f"世界背景: {bg}\n当前时间: {game_time}\n\n事件:\n{event_desc}\n\n"
                f"请返回JSON: {{\"title\": \"新闻标题(10字内)\", \"content\": \"新闻正文(50-150字)\"}}"
            )
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是游戏世界中的新闻记者，用符合世界观的风格写新闻。只返回紧凑JSON。",
                max_tokens=1024,
            )
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            data = json.loads(m.group()) if m else {"title": "快讯", "content": raw[:200]}
            return {
                "id": f"news_{self.turn_number}",
                "turn": self.turn_number,
                "time": game_time,
                "title": data.get("title", "快讯"),
                "content": data.get("content", ""),
                "source_events": [s["name"] for s in sources],
            }
        except Exception:
            return None

    async def generate_newspaper(self, force: bool = False) -> dict:
        """Generate a newspaper summary using AI, with per-turn caching."""
        turn_key = str(self.turn_number)
        papers = self.current_state.get("newspapers", {})
        if not force and turn_key in papers:
            cached = dict(papers[turn_key])
            cached["cached"] = True
            return cached

        game_time = self.current_state.get("game_time", "")
        if not self.ai_provider:
            return {"title": "报纸", "date": game_time, "headline": "无AI服务", "sections": [{"title": "提示", "content": "需要配置AI服务才能生成报纸。"}]}
        bg = self.script.get("background", "")[:300]
        log = self.current_state.get("adventure_log", [])
        news_feed = self.current_state.get("news_feed", [])

        log_text = ""
        recent = log[-20:] if isinstance(log, list) else []
        for entry in recent:
            if isinstance(entry, dict):
                events = entry.get("events", [])
                for ev in events:
                    if isinstance(ev, dict):
                        log_text += f"- [回合{entry.get('turn', '?')}] {ev.get('text', '')}\n"

        news_text = ""
        for n in (news_feed[-10:] if isinstance(news_feed, list) else []):
            if isinstance(n, dict):
                news_text += f"- {n.get('title', '')}: {n.get('content', '')}\n"

        material = (log_text + "\n" + news_text).strip()
        if not material:
            result = {"title": "报纸", "date": game_time, "headline": "风平浪静", "sections": [{"title": "本期无重大新闻", "content": "一切平安。"}]}
            result["cached"] = False
            self.current_state.setdefault("newspapers", {})[turn_key] = result
            return result

        prompt = (
            f"你是游戏世界中的报社总编。根据以下素材，编写一份报纸。\n"
            f"世界背景: {bg}\n当前时间: {game_time}\n\n素材:\n{material[:2000]}\n\n"
            f"请返回JSON: {{\"title\": \"报纸名(2-4字)\", \"date\": \"刊载日期\", "
            f"\"headline\": \"头条标题\", \"sections\": [{{\"title\": \"栏目标题\", \"content\": \"栏目内容\"}}]}}\n"
            f"生成3-5个栏目，内容要符合世界观，有趣生动。只返回JSON。"
        )
        try:
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是游戏世界报社总编，用符合世界观的风格编写报纸。只返回JSON。",
                max_tokens=1200,
            )
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                result = json.loads(m.group())
            else:
                result = {"title": "报纸", "date": game_time, "content": raw[:500]}
        except Exception as e:
            return {"title": "报纸", "date": game_time, "headline": "生成失败", "sections": [{"title": "错误", "content": str(e)}]}
        result["cached"] = False
        self.current_state.setdefault("newspapers", {})[turn_key] = result
        return result

    async def switch_pov(self, preset_id: str | None = None, custom_character: dict | None = None) -> dict | None:
        """Switch player POV: shelve current PC as NPC, init new PC in same world timeline."""
        state = self.current_state
        pov_history = state.setdefault("pov_history", [])

        # Limit switches
        if len(pov_history) >= 5:
            return None

        # Validate target
        if preset_id:
            shelved = state.get("shelved_pc_data", {})
            presets = self.script.get("player_presets", [])
            preset = next((p for p in presets if p.get("id") == preset_id), None)
            is_shelved = preset_id in shelved
            if not preset and not is_shelved:
                return None
            # Cannot switch to a preset that's already an active NPC (not shelved)
            if not is_shelved and preset_id in state.get("npcs", {}):
                return None
        elif not custom_character:
            return None

        # 1) Shelve current PC
        self._shelve_current_pc()

        # 2) Init new PC
        self._init_new_pc(preset_id, custom_character)

        # 3) Mark POV switch in world tree
        node_id = self.world_tree.add_node(
            parent_id=self.world_tree.active_node_id,
            game_time=state.get("game_time", ""),
            turn_number=self.turn_number,
            player_action={"type": "pov_switch", "text": f"视角切换至 {state['player']['name']}"},
            ai_response="",
            state_snapshot=state,
        )

        # 4) Generate POV switch narrative
        narrative = await self._generate_pov_narrative()

        # Update the node with the narrative
        node = self.world_tree.get_node(node_id)
        if node:
            node["ai_response"] = narrative

        # 5) Generate choices for new PC
        choices = self._generate_context_choices()

        return {
            "narrative": narrative,
            "player": state["player"],
            "choices": choices,
            "state": state,
        }

    def _shelve_current_pc(self):
        """Convert current PC into an NPC, preserving all evolved state."""
        state = self.current_state
        player = state.get("player", {})
        pc_id = player.get("id", "player")

        # Store PC-bound data
        shelved = state.setdefault("shelved_pc_data", {})
        shelved[pc_id] = {
            "player": dict(player),
            "inventory": list(state.get("inventory", [])),
            "discovered_lore": list(state.get("pc_discovered_lore", [])),
            "visible_locations": list(state.get("visible_locations", [])),
            "chat_history": dict(state.get("npc_chat_history", {})),
            "turn_shelved": self.turn_number,
        }

        # Add old PC as NPC
        pc_orgs = player.get("organizations", [])
        state.setdefault("npcs", {})[pc_id] = {
            "attitude_toward_player": 50,
            "name": player.get("name", pc_id),
            "known": True,
            "met": False,
            "default_location": player.get("location", ""),
            "bio": player.get("bio", ""),
            "personality": player.get("personality", ""),
            "title": player.get("title", ""),
            "current_room": player.get("current_room", ""),
            "capabilities": "",
            "organizations": pc_orgs,
            "superior": "",
            "_shelved_attributes": player.get("attributes", {}),
        }

        # Register shelved PC in script NPC list and KG lorebook
        script_npcs = self.script.setdefault("npcs", [])
        existing_entry = next((n for n in script_npcs if n.get("id") == pc_id), None)
        if not existing_entry:
            existing_entry = {
                "id": pc_id,
                "name": player.get("name", pc_id),
                "bio": player.get("bio", ""),
                "personality": player.get("personality", ""),
                "title": player.get("title", ""),
                "organizations": pc_orgs,
                "initial_location": player.get("location", ""),
            }
            script_npcs.append(existing_entry)
        self._npc_by_id[pc_id] = existing_entry
        self.prompt_builder._npc_name_map[pc_id] = player.get("name", pc_id)
        if self.prompt_builder._has_kg:
            self.prompt_builder.refresh_kg_entries([pc_id], [])

        # Transfer old PC's discovered lore → NPC knows it
        if self.prompt_builder and self.prompt_builder.lorebook:
            for lore_id in state.get("pc_discovered_lore", []):
                entry = next((e for e in self.prompt_builder.lorebook.entries if e.id == lore_id), None)
                if entry and entry.visibility == "hidden" and pc_id not in entry.known_by_npcs:
                    entry.known_by_npcs.append(pc_id)

        # Record POV history
        state.setdefault("pov_history", []).append({
            "pc_id": pc_id,
            "turn": self.turn_number,
            "game_time": state.get("game_time", ""),
        })

        # Add display name for the shelved PC
        state.setdefault("display_names", {})[pc_id] = player.get("name", pc_id)

    def _init_new_pc(self, preset_id: str | None, custom_character: dict | None):
        """Initialize a new PC from preset or custom data, keeping world state intact."""
        state = self.current_state

        if preset_id:
            # Check if this is a returning PC (previously shelved)
            shelved = state.get("shelved_pc_data", {}).get(preset_id)
            if shelved:
                # Restore shelved PC
                state["player"] = shelved["player"]
                state["inventory"] = shelved["inventory"]
                state["pc_discovered_lore"] = shelved["discovered_lore"]
                state["visible_locations"] = shelved["visible_locations"]
                state["npc_chat_history"] = shelved["chat_history"]
                # Remove from NPC pool
                state.get("npcs", {}).pop(preset_id, None)
                # Remove from shelved data
                state["shelved_pc_data"].pop(preset_id, None)
            else:
                # Fresh PC from preset
                presets = self.script.get("player_presets", [])
                preset = next((p for p in presets if p.get("id") == preset_id), None)
                if not preset:
                    return
                attrs = {}
                for attr_name, attr_def in preset.get("attributes", {}).items():
                    if isinstance(attr_def, dict):
                        attrs[attr_name] = attr_def.get("value", 50)
                    else:
                        attrs[attr_name] = attr_def

                state["player"] = {
                    "id": preset_id,
                    "name": preset.get("name", preset_id),
                    "title": preset.get("title", ""),
                    "bio": preset.get("bio", ""),
                    "personality": preset.get("personality", ""),
                    "portrait_desc": preset.get("portrait_desc", ""),
                    "location": preset.get("initial_location", ""),
                    "current_room": preset.get("default_room", ""),
                    "long_term_goal": preset.get("long_term_goal", ""),
                    "attributes": attrs,
                    "relationships": {},
                }
                state["inventory"] = []
                if preset.get("initial_inventory"):
                    for item in preset["initial_inventory"]:
                        if isinstance(item, dict):
                            state["inventory"].append({"item": item.get("item", item.get("name", "")), "quantity": item.get("quantity", 1)})
                        elif isinstance(item, str):
                            state["inventory"].append({"item": item, "quantity": 1})

                state["pc_discovered_lore"] = list(preset.get("known_lore", []))

                # Visible locations: script initially_visible + new PC's location
                vis = [loc["id"] for loc in self.script.get("locations", []) if loc.get("initially_visible", True)]
                new_loc = preset.get("initial_location", "")
                if new_loc and new_loc not in vis:
                    vis.append(new_loc)
                state["visible_locations"] = vis

        elif custom_character:
            pc_id = custom_character.get("id", f"custom_{len(state.get('pov_history', []))}")
            attrs = {}
            for k, v in custom_character.get("attributes", {}).items():
                attrs[k] = v.get("value", 50) if isinstance(v, dict) else v

            pc_name = custom_character.get("name", "自定义角色")
            new_loc = custom_character.get("initial_location", "")
            org = custom_character.get("organization", "")
            title = custom_character.get("title", "")

            state["player"] = {
                "id": pc_id,
                "name": pc_name,
                "bio": custom_character.get("bio", ""),
                "personality": custom_character.get("personality", ""),
                "portrait_desc": custom_character.get("portrait_desc", ""),
                "title": title,
                "location": new_loc,
                "current_room": "",
                "long_term_goal": custom_character.get("long_term_goal", ""),
                "attributes": attrs,
                "relationships": {},
            }
            if org:
                state["player"]["organizations"] = [{"org_id": org}]
            state["inventory"] = []
            state["pc_discovered_lore"] = []

            # Register new location via existing reveal system + lorebook KG
            vis = [loc["id"] for loc in self.script.get("locations", []) if loc.get("initially_visible", True)]
            if new_loc:
                if new_loc not in vis:
                    vis.append(new_loc)
                all_loc_ids = {loc["id"] for loc in self.script.get("locations", [])}
                if new_loc not in all_loc_ids:
                    state.setdefault("display_names", {})[new_loc] = new_loc
                    state.setdefault("location_descriptions", {})[new_loc] = ""
                    if self.prompt_builder and self.prompt_builder.lorebook:
                        self.prompt_builder.add_location_kg_entry(new_loc, new_loc, "")
                        self.prompt_builder._loc_name_map[new_loc] = new_loc
            state["visible_locations"] = vis

            # Register new organization via KG refresh
            if org:
                existing_orgs = {o.get("id", "") for o in self.script.get("organizations", [])}
                if org not in existing_orgs:
                    new_org_def = {"id": org, "name": org, "description": ""}
                    self.script.setdefault("organizations", []).append(new_org_def)
                    state.setdefault("display_names", {})[org] = org
                if self.prompt_builder and self.prompt_builder._has_kg:
                    self.prompt_builder.refresh_kg_entries([], [org])

            # Register the new PC as a known NPC profile in lorebook (for AI context)
            if self.prompt_builder and self.prompt_builder.lorebook:
                entry_id = f"_kg_npc_{pc_id}"
                if not any(e.id == entry_id for e in self.prompt_builder.lorebook.entries):
                    bio_parts = [pc_name]
                    if title:
                        bio_parts.append(f"职位: {title}")
                    if org:
                        dn = state.get("display_names", {})
                        bio_parts.append(f"所属: {dn.get(org, org)}")
                    if custom_character.get("bio"):
                        bio_parts.append(custom_character["bio"])
                    content = "；".join(bio_parts)
                    keys = [pc_name]
                    if title:
                        keys.append(title)
                    self.prompt_builder.lorebook.add_entries([{
                        "id": entry_id,
                        "keys": keys,
                        "content": content,
                        "entry_type": "character",
                        "priority": 80,
                        "position": "after_world",
                        "constant": False,
                        "enabled": True,
                        "scan_depth": 3,
                        "comment": f"角色: {pc_name}",
                    }])

        # Reset PC-specific transient state
        state["npc_chat_history"] = {}
        state["play_style_summary"] = ""
        pacing = state.get("pacing_state")
        if pacing:
            pacing["tension"] = 40
            pacing["trend"] = "stable"
            pacing["consecutive_high"] = 0
            pacing["consecutive_low"] = 0

    async def _generate_pov_narrative(self) -> str:
        """Generate a brief narrative introducing the new PC's current situation."""
        state = self.current_state
        player = state["player"]
        game_time = state.get("game_time", "")
        location_id = player.get("location", "")
        loc_name = state.get("display_names", {}).get(location_id, location_id)

        system = (
            "你是文字游戏的叙事整合师。用200字以内，以第二人称写一段简短的视角切换叙事。\n"
            "要求：交代新角色此刻的处境（位置、正在做什么、周围氛围），不要写前情回顾。\n"
            "直接输出叙事文本，不要JSON/标记。"
        )
        content = (
            f"角色: {player.get('name', '')}\n"
            f"身份: {player.get('bio', '')}\n"
            f"性格: {player.get('personality', '')}\n"
            f"当前时间: {self.prompt_builder.format_game_time(game_time) if game_time else '未知'}\n"
            f"当前位置: {loc_name}\n"
            f"天气: {state.get('current_weather', '未知')}"
        )

        try:
            response = await self.ai_provider.generate(
                [{"role": "user", "content": content}],
                system=system,
                max_tokens=400,
                **self._stage_kwargs("narrative"),
            )
            return response.strip() if isinstance(response, str) else f"视角切换至{player.get('name', '新角色')}。"
        except Exception:
            return f"视角切换至{player.get('name', '新角色')}。"

    def _apply_common_parsed_changes(
        self, state: dict, parsed: dict, *, inplace: bool = False,
        present_npc_ids: list[str] | None = None,
    ) -> tuple[dict, list[dict]]:
        """P3: 共享方法 — 把 parsed AI 响应中的通用 state 变更应用到给定 state。

        被 _apply_parsed_response（主回合，inplace=True）和 regenerate（swipe 副本，
        inplace=False）共用，避免重复逻辑漂移。

        包含：state_changes / npc_attitude_changes / activate|deactivate_states /
        world_property_changes / location_change / reveal_locations / inventory_changes /
        offscreen_npc_updates / new_npcs / npc_relationship_updates。
        注: 事件CRUD(consequences/deadlines/clues/threads)已移至异步事件阶段(event_stage)处理。
        不含 time_advance（两边时间基准不同）和仅主回合需要的副作用
        （旅行时间 / 事件再检查 / 天气 / 阈值 / adventure log）。
        """
        all_changes: list[dict] = []
        dirty_npc_ids: set[str] = set()

        if parsed.get("state_changes"):
            # Handle variable ops before passing to state_manager
            var_ops = [sc for sc in parsed["state_changes"] if sc.get("type") == "variable"]
            regular_changes = [sc for sc in parsed["state_changes"] if sc.get("type") != "variable"]
            for vop in var_ops:
                self.script_variables.apply_op(
                    state, vop.get("id", ""), vop.get("op", "set"), vop.get("value")
                )
            if regular_changes:
                validated = []
                MAX_DELTA = 30
                for sc in regular_changes:
                    op = sc.get("op", "add")
                    val = sc.get("value", 0)
                    if op in ("add", "subtract") and not sc.get("reason"):
                        logger.warning("丢弃无reason属性变更: %s", sc)
                        continue
                    if isinstance(val, (int, float)):
                        if op == "set":
                            old = self.state_manager._get_value(state, sc.get("target", ""))
                            if isinstance(old, (int, float)):
                                delta = val - old
                                if abs(delta) > MAX_DELTA:
                                    clamped = old + (MAX_DELTA if delta > 0 else -MAX_DELTA)
                                    logger.warning(
                                        "属性变化幅度过大 %s: %s→%s, 限制为→%s",
                                        sc.get("target"), old, val, clamped,
                                    )
                                    sc = {**sc, "value": clamped}
                        elif op in ("add", "subtract") and abs(val) > MAX_DELTA:
                            clamped = MAX_DELTA if val > 0 else -MAX_DELTA
                            logger.warning(
                                "属性变化幅度过大 %s: op=%s val=%s, 限制为%s",
                                sc.get("target"), op, val, clamped,
                            )
                            sc = {**sc, "value": clamped}
                    validated.append(sc)
                regular_changes = validated
            if regular_changes:
                state, log = self.state_manager.apply_changes(
                    state, regular_changes, inplace=inplace
                )
                all_changes.extend(log)
                self._apply_org_changes_to_script(regular_changes)
                for sc in regular_changes:
                    t = sc.get("target", "")
                    if t.startswith("npcs."):
                        parts = t.split(".", 2)
                        if len(parts) >= 2:
                            dirty_npc_ids.add(parts[1])

        npc_att = parsed.get("npc_attitude_changes", [])
        if npc_att:
            att_as_state = self._npc_attitude_to_state_changes(npc_att, state=state)
            MAX_ATTITUDE_DELTA = 20
            for sc in att_as_state:
                val = sc.get("value", 0)
                if sc.get("op") in ("add", "subtract") and isinstance(val, (int, float)):
                    if abs(val) > MAX_ATTITUDE_DELTA:
                        sc["value"] = MAX_ATTITUDE_DELTA if val > 0 else -MAX_ATTITUDE_DELTA
            state, log = self.state_manager.apply_changes(
                state, att_as_state, inplace=inplace
            )
            all_changes.extend(log)
            for sc in att_as_state:
                t = sc.get("target", "")
                if "relationships" in t or t.startswith("npcs."):
                    parts = t.split(".")
                    if "relationships" in t and len(parts) >= 3:
                        dirty_npc_ids.add(parts[2])
                    elif t.startswith("npcs.") and len(parts) >= 2:
                        dirty_npc_ids.add(parts[1])
            # Sync significant attitude changes to lorebook
            self._sync_attitude_to_lorebook(npc_att)
            # NPC 揭露：态度达到阈值时自动发现 hidden 词条
            if self.prompt_builder.lorebook:
                _disc = state.get("pc_discovered_lore", [])
                _present = set(present_npc_ids) if present_npc_ids else set()
                for entry in self.prompt_builder.lorebook.entries:
                    if (entry.visibility == "hidden"
                            and entry.discoverable.get("method") == "npc_reveal"
                            and entry.id not in _disc):
                        src_npc = entry.discoverable.get("npc_source", "")
                        threshold = entry.discoverable.get("attitude_threshold", 60)
                        if src_npc and src_npc in _present:
                            npc_st = state.get("npcs", {}).get(src_npc, {})
                            att = npc_st.get("attitude_toward_player", 50)
                            if att >= threshold:
                                _disc.append(entry.id)
                                state["pc_discovered_lore"] = _disc
                                logger.info("Lore discovered: %s (source=npc_reveal:%s)", entry.id, src_npc)

        _REJECT_STATE_PATTERNS = ("weather", "天气", "时段", "time_period", "dawn", "dusk",
                                    "morning", "afternoon", "night", "noon", "黎明", "黄昏",
                                    "上午", "午后", "夜晚", "深夜", "日出", "日落")
        for entry in parsed.get("activate_states", []):
            if isinstance(entry, dict):
                sid = entry.get("id", "")
                s_name = entry.get("name", "")
                s_desc = entry.get("description", "")
                # name 等于 ID 或纯英文时视为缺失，从描述生成中文名
                if not s_name or s_name == sid or (s_name.isascii() and "_" in s_name):
                    if s_desc:
                        s_name = s_desc[:10].rstrip("，。、,.")
                    else:
                        s_name = sid.replace("_", " ")
            else:
                sid = entry
                ps_def = self._ps_by_id.get(sid, {})
                s_name = ps_def.get("name") or ps_def.get("display_name") or ""
                s_desc = ps_def.get("description") or ""
            if not sid:
                continue
            # Reject weather/time-period states (managed by engine, not AI)
            _sid_lower = sid.lower()
            _name_lower = (s_name or "").lower()
            if any(p in _sid_lower or p in _name_lower for p in _REJECT_STATE_PATTERNS):
                continue
            active = state.setdefault("active_persistent_states", [])
            if sid not in active:
                active.append(sid)
            # 为新状态注册 display_name 和 description
            dn = state.setdefault("display_names", {})
            existing_name = dn.get(sid, "")
            # 覆盖条件：之前没有、或之前存的是英文ID
            if s_name and (not existing_name or (existing_name.isascii() and "_" in existing_name)):
                dn[sid] = s_name
            psd = state.setdefault("persistent_state_descriptions", {})
            if s_desc and sid not in psd:
                psd[sid] = s_desc
        for sid in parsed.get("deactivate_states", []):
            active = state.get("active_persistent_states", [])
            if sid in active:
                active.remove(sid)

        # Invalidate speculative lorebook entries (butterfly effect)
        for lore_id in parsed.get("invalidate_lore", []):
            self.prompt_builder.lorebook.remove_entry(lore_id)
            if self.vector_memory:
                self.vector_memory.remove_lorebook(lore_id)
            dl = state.get("dynamic_lorebook", [])
            state["dynamic_lorebook"] = [e for e in dl if e.get("id") != lore_id]

        # Filter world_property_changes: reject object-level details (belong in scene_details)
        _REJECT_WP_PATTERNS = ("lamp", "light", "door", "window", "clock", "alarm",
                               "desk", "chair", "phone", "radio", "tv", "灯", "门",
                               "窗", "桌", "椅", "电话", "闹钟", "台灯", "scattered",
                               "curtain", "drawer", "paper", "document", "书桌", "抽屉")
        for wp in parsed.get("world_property_changes", []):
            wp_id = wp.get("id")
            if not wp_id:
                continue
            _wp_lower = wp_id.lower()
            if any(p in _wp_lower for p in _REJECT_WP_PATTERNS):
                continue
            state.setdefault("world_properties", {})[wp_id] = wp.get("value")
            wp_name = wp.get("name")
            if wp_name:
                state.setdefault("display_names", {})[wp_id] = wp_name
        if parsed.get("world_property_changes"):
            self._update_dynamic_connections(state)

        # reveal_locations 先于 location_change 处理，确保同回合揭示+移动可行
        for loc_entry in parsed.get("reveal_locations", []):
            if isinstance(loc_entry, dict):
                loc_id = loc_entry.get("id", "")
                loc_name = loc_entry.get("name", loc_id)
                loc_desc = loc_entry.get("description", "")
            else:
                loc_id = loc_entry
                loc_name = self._location_by_id.get(loc_id, {}).get("name", loc_id)
                loc_desc = ""
            if not loc_id:
                continue
            if not self._is_duplicate_location(state, loc_id):
                state = self.state_manager.reveal_location(state, loc_id, inplace=inplace)
                state.setdefault("display_names", {})[loc_id] = loc_name
                if loc_id not in self._location_by_id and loc_desc:
                    self.prompt_builder.add_location_kg_entry(loc_id, loc_name, loc_desc)
                    self.prompt_builder._loc_name_map[loc_id] = loc_name
                    if self.vector_memory:
                        kg_entry = next((e for e in self.prompt_builder.lorebook.entries if e.id == f"_kg_loc_{loc_id}"), None)
                        if kg_entry and kg_entry.content:
                            self.vector_memory.add_lorebook(kg_entry.id, kg_entry.content, {
                                "entry_type": kg_entry.entry_type, "comment": kg_entry.comment,
                            })

        if parsed.get("location_change"):
            new_loc_id = parsed["location_change"]
            if isinstance(new_loc_id, dict):
                new_loc_id = new_loc_id.get("id", new_loc_id.get("location_id", ""))
            _valid_locs = self._location_by_id
            _visible = state.get("visible_locations", [])
            if new_loc_id and new_loc_id not in _valid_locs and new_loc_id not in _visible:
                logger.warning("AI location_change=%r 不在合法地点列表中，跳过", new_loc_id)
            elif new_loc_id:
                new_loc_def = _valid_locs.get(new_loc_id, {})
                access_cond = new_loc_def.get("access_condition", "")
                if access_cond and not self._evaluate_condition(access_cond):
                    state["location_access_blocked"] = {
                        "location_id": new_loc_id,
                        "reason": new_loc_def.get("access_blocked_reason", "声望不足，无法进入此区域"),
                    }
                else:
                    state.setdefault("player", {})["location"] = new_loc_id
                    state.pop("location_access_blocked", None)

        for inv in parsed.get("inventory_changes", []):
            item_name = inv.get("item", "")
            if not item_name:
                continue
            inv_action = inv.get("action", "add")
            quantity = inv.get("quantity", 1)
            description = inv.get("description", "")
            inventory = state.setdefault("inventory", [])
            if inv_action == "add":
                found = False
                for entry in inventory:
                    if entry.get("item") == item_name:
                        entry["quantity"] = entry.get("quantity", 1) + quantity
                        if description:
                            entry["description"] = description
                        found = True
                        break
                if not found:
                    new_entry = {"item": item_name, "quantity": quantity}
                    if description:
                        new_entry["description"] = description
                    inventory.append(new_entry)
            elif inv_action == "remove":
                found_entry = next((e for e in inventory if e.get("item") == item_name), None)
                if not found_entry:
                    logger.warning("丢弃移除不存在物品: %s", item_name)
                    continue
                found_entry["quantity"] = found_entry.get("quantity", 1) - quantity
                if found_entry["quantity"] <= 0:
                    inventory.remove(found_entry)

        # Offscreen NPC updates
        _offscreen_seen_ids: set = set()
        for update in parsed.get("offscreen_npc_updates", []):
            # 优先用 name 匹配，兼容旧格式 npc_id
            raw_name = update.get("name", "")
            raw_id = update.get("npc_id", "")
            npc_id = ""
            if raw_name:
                npc_id = self._npc_name_to_id.get(raw_name, "")
                if not npc_id:
                    # 尝试从动态 NPC state 中按名字查找
                    for _sid, _sn in state.get("npcs", {}).items():
                        if isinstance(_sn, dict) and _sn.get("name") == raw_name:
                            npc_id = _sid
                            break
            if not npc_id and raw_id:
                if raw_id in self._npc_by_id or raw_id in state.get("npcs", {}):
                    npc_id = raw_id
            if not npc_id:
                continue
            if npc_id in _offscreen_seen_ids:
                continue
            _offscreen_seen_ids.add(npc_id)
            log = state.setdefault("npc_offscreen_log", {})
            npc_log = log.setdefault(npc_id, [])
            npc_log.append({
                "turn": self.turn_number,
                "time": state.get("game_time", ""),
                "action": update.get("action", ""),
                "location": update.get("location", ""),
                "mood": update.get("mood", ""),
            })
            if len(npc_log) > 20:
                log[npc_id] = npc_log[-15:]
            new_loc = update.get("location", "")
            if new_loc:
                npc_st = state.get("npcs", {}).get(npc_id)
                if isinstance(npc_st, dict):
                    npc_st["current_location"] = new_loc
            self._sync_offscreen_to_lorebook(npc_id, log.get(npc_id, []))

        # NPC location changes (in-scene departures/arrivals)
        for change in parsed.get("npc_location_changes", []):
            npc_id = change.get("npc_id", "")
            new_loc = change.get("new_location", "")
            if not npc_id or not new_loc:
                continue
            if new_loc not in self._location_by_id and new_loc not in state.get("visible_locations", []):
                logger.warning("AI npc_location_changes: new_location=%r 不在合法地点列表中，跳过", new_loc)
                continue
            npc_st = state.get("npcs", {}).get(npc_id)
            if isinstance(npc_st, dict):
                npc_st["current_location"] = new_loc

        # Room-level changes (within same building)
        for change in parsed.get("room_changes", []):
            target_id = change.get("id", "")
            new_room = change.get("new_room", "")
            if not target_id or not new_room:
                continue
            if target_id == "player":
                state.setdefault("player", {})["current_room"] = new_room
            else:
                npc_st = state.get("npcs", {}).get(target_id)
                if isinstance(npc_st, dict):
                    npc_st["current_room"] = new_room

        # Dynamic new NPCs
        script_npc_ids = {n["id"] for n in self.script.get("npcs", []) if n.get("id")}
        script_npc_names = {n.get("name", "") for n in self.script.get("npcs", []) if n.get("name")}
        current_pc_id = state.get("player", {}).get("id", "player")
        current_pc_name = state.get("player", {}).get("name", "")
        for new_npc in parsed.get("new_npcs", []):
            npc_id = new_npc.get("id", "")
            if not npc_id:
                continue
            # Skip if this is the current PC
            if npc_id == current_pc_id:
                continue
            npcs_dict = state.setdefault("npcs", {})
            if npc_id in npcs_dict:
                continue
            if npc_id in script_npc_ids:
                continue
            # 按 name 去重：AI 可能给同一 NPC 生成不同 id
            new_name = new_npc.get("name", npc_id)
            existing_names = {
                nd.get("name", "") for nd in npcs_dict.values() if isinstance(nd, dict)
            }
            if new_name in existing_names or new_name in script_npc_names:
                continue
            # Skip if name matches current PC
            if new_name == current_pc_name:
                continue
            npcs_dict[npc_id] = {
                "name": new_npc.get("name", npc_id),
                "attitude_toward_player": new_npc.get("attitude_toward_player", 50),
                "known": new_npc.get("known", True),
                "met": new_npc.get("met", new_npc.get("known", True)),
                "default_location": new_npc.get("location", ""),
                "current_location": new_npc.get("location", ""),
                "bio": new_npc.get("bio", ""),
                "personality": new_npc.get("personality", ""),
                "capabilities": new_npc.get("capabilities", ""),
                "title": new_npc.get("title", ""),
                "organizations": new_npc.get("organizations") or self._migrate_npc_org_fields(new_npc),
                "superior": new_npc.get("superior", ""),
            }
            state.setdefault("display_names", {})[npc_id] = new_npc.get("name", npc_id)
            rels = state.get("player", {}).setdefault("relationships", {})
            if npc_id not in rels:
                att = new_npc.get("attitude_toward_player", 50)
                # Use AI-provided 3D values if present, else fall back to personality heuristic
                ai_trust = new_npc.get("trust")
                ai_affection = new_npc.get("affection")
                ai_fear = new_npc.get("fear")
                if ai_trust is not None or ai_affection is not None or ai_fear is not None:
                    trust = max(0, min(100, int(ai_trust or att)))
                    affection = max(0, min(100, int(ai_affection or att)))
                    fear = max(0, min(100, int(ai_fear or 0)))
                else:
                    personality = (new_npc.get("personality", "") or "").lower()
                    trust = att
                    affection = att
                    fear = 0
                    if any(w in personality for w in ("冷", "警惕", "多疑", "严厉", "冷酷")):
                        trust = max(0, att - 15)
                        affection = max(0, att - 10)
                    elif any(w in personality for w in ("热情", "友善", "善良", "温和", "开朗")):
                        affection = min(100, att + 10)
                    if any(w in personality for w in ("威严", "强势", "暴力", "危险", "凶")):
                        fear = min(40, max(10, 60 - att))
                rels[npc_id] = {"trust": trust, "affection": affection, "fear": fear}
            # 同步 script 和 prompt_builder 缓存
            script_npcs = self.script.setdefault("npcs", [])
            if not any(n.get("id") == npc_id for n in script_npcs):
                script_npcs.append(new_npc)
            self._npc_by_id[npc_id] = new_npc
            self.prompt_builder._npc_name_map[npc_id] = new_npc.get("name", npc_id)
            dirty_npc_ids.add(npc_id)

        # Generate KG lorebook entries for newly created NPCs
        new_npc_ids = [n.get("id", "") for n in parsed.get("new_npcs", []) if n.get("id")]
        new_npc_ids = [nid for nid in new_npc_ids if nid in state.get("npcs", {})]
        if new_npc_ids and self.prompt_builder._has_kg:
            self.prompt_builder.refresh_kg_entries(new_npc_ids, [])
            if self.vector_memory:
                self._sync_kg_entries_to_vector(new_npc_ids, [])

        # NPC met changes (认识→熟识)
        for mc in parsed.get("npc_met_changes", []):
            npc_id = mc.get("npc_id", "")
            if not npc_id:
                continue
            npc_st = state.get("npcs", {}).get(npc_id)
            if isinstance(npc_st, dict):
                if mc.get("met") is not None:
                    npc_st["met"] = bool(mc["met"])
                if mc.get("known") is not None:
                    npc_st["known"] = bool(mc["known"])

        # NPC-NPC relationship updates
        npc_rel_updates = parsed.get("npc_relationship_updates", [])
        if npc_rel_updates:
            self._apply_npc_relationship_updates(npc_rel_updates, state=state)

        scene_details = parsed.get("scene_details")
        if scene_details and isinstance(scene_details, dict):
            state["scene_details"] = scene_details
            loc_id = state.get("player", {}).get("location", "")
            if loc_id and self.prompt_builder.lorebook:
                self.prompt_builder.update_location_scene(loc_id, scene_details)

        for frc in parsed.get("faction_reputation_changes", []):
            fid = frc.get("faction_id", "")
            if not fid:
                continue
            change = frc.get("change", 0)
            rep = state.setdefault("faction_reputation", {})
            entry = rep.setdefault(fid, {"value": 50})
            entry["value"] = max(0, min(100, entry.get("value", 50) + change))
            entry["title"] = self._reputation_title(entry["value"])
            if frc.get("reason"):
                entry["last_reason"] = frc["reason"]

        # Moral alignment changes
        for mac in parsed.get("moral_alignment_changes", []):
            axis = mac.get("axis", "")
            change = mac.get("change", 0)
            if not axis or not change:
                continue
            ma = state.setdefault("moral_alignment", {
                "mercy_vs_cruelty": 0,
                "honesty_vs_deception": 0,
                "order_vs_chaos": 0,
            })
            if axis in ma:
                ma[axis] = max(-100, min(100, ma[axis] + change))

        self._sync_npc_fields_to_script(state, dirty_npc_ids=dirty_npc_ids)

        # Companion changes from AI
        for recruit in parsed.get("recruit_companions", []):
            cid = recruit if isinstance(recruit, str) else recruit.get("npc_id", "")
            if not cid:
                continue
            companions = state.setdefault("companions", [])
            if cid in companions:
                continue
            npc_st = state.get("npcs", {}).get(cid)
            if not isinstance(npc_st, dict):
                continue
            companions.append(cid)
            loyalty = state.setdefault("companion_loyalty", {})
            loyalty.setdefault(cid, {"value": 50})
            npc_name = npc_st.get("name", cid)
            state.setdefault("_companion_events", []).append({
                "npc_id": cid, "name": npc_name, "event": "join",
            })
        for dismiss in parsed.get("dismiss_companions", []):
            cid = dismiss if isinstance(dismiss, str) else dismiss.get("npc_id", "")
            companions = state.get("companions", [])
            if cid in companions:
                companions.remove(cid)
                npc_st = state.get("npcs", {}).get(cid, {})
                npc_name = npc_st.get("name", cid) if isinstance(npc_st, dict) else cid
                state.setdefault("_companion_events", []).append({
                    "npc_id": cid, "name": npc_name, "event": "leave",
                })

        # Sync attitude for all dirty NPCs (state_changes or attitude_changes touched relationships)
        if dirty_npc_ids:
            rels = state.get("player", {}).get("relationships", {})
            npcs_s = state.get("npcs", {})
            for npc_id in dirty_npc_ids:
                npc_data = npcs_s.get(npc_id)
                if not isinstance(npc_data, dict) or npc_id not in rels:
                    continue
                val = rels[npc_id]
                if isinstance(val, dict) and any(k in val for k in ("trust", "affection", "fear")):
                    attitude = self._calc_attitude_from_3d(
                        val.get("trust", 50), val.get("affection", 50), val.get("fear", 0),
                    )
                elif isinstance(val, (int, float)):
                    attitude = int(val)
                else:
                    continue
                npc_data["attitude_toward_player"] = max(0, min(100, attitude))

        return state, all_changes

    # ================================================================
    # NPC 声音一致性校验（Stage 3 后置检查）
    # ================================================================

    _MODAL_PARTICLES = "呢啊嘛呀吧哦哎唉嗯嘿喂哈"

    def _extract_npc_dialogues(
        self, narrative: str, present_npcs: list[str]
    ) -> dict[str, list[str]]:
        """从叙事中提取归属到各NPC的对话文本。"""
        npc_states = self.current_state.get("npcs", {})
        dn = self.current_state.get("display_names", {})
        name_to_id = {}
        for npc_id in present_npcs:
            ns = npc_states.get(npc_id, {})
            name = ns.get("name", dn.get(npc_id, npc_id))
            name_to_id[name] = npc_id

        result: dict[str, list[str]] = {npc_id: [] for npc_id in present_npcs}

        # 匹配模式: NPC名 + 说/道/笑道 等 + "对话"  或  "对话" 前一行提到NPC名
        for name, npc_id in name_to_id.items():
            pattern = re.compile(
                rf'{re.escape(name)}[^""\n]{{0,20}}["“]([^"”]+)["”]'
            )
            for m in pattern.finditer(narrative):
                result[npc_id].append(m.group(1))

        return result

    def _check_npc_voice_consistency(
        self, narrative: str, present_npcs: list[str]
    ) -> bool:
        """返回True=通过，False=NPC对话风格雷同需修正。"""
        if len(present_npcs) < 2:
            return True

        npc_dialogues = self._extract_npc_dialogues(narrative, present_npcs)
        # 只分析有对话的NPC
        active = {k: v for k, v in npc_dialogues.items() if v}
        if len(active) < 2:
            return True

        features = {}
        for npc_id, texts in active.items():
            combined = "".join(texts)
            if not combined:
                continue
            clen = len(combined)
            features[npc_id] = {
                "avg_len": sum(len(t) for t in texts) / len(texts),
                "excl": combined.count("！") / clen,
                "ques": combined.count("？") / clen,
                "ellip": combined.count("…") / clen,
                "modal": sum(combined.count(p) for p in self._MODAL_PARTICLES) / clen,
            }

        npcs = list(features.keys())
        for i in range(len(npcs)):
            for j in range(i + 1, len(npcs)):
                fi, fj = features[npcs[i]], features[npcs[j]]
                if (
                    abs(fi["avg_len"] - fj["avg_len"]) < 3
                    and abs(fi["excl"] - fj["excl"]) + abs(fi["ques"] - fj["ques"]) < 0.02
                    and abs(fi["modal"] - fj["modal"]) < 0.02
                ):
                    return False
        return True

    async def _maybe_fix_npc_voices(self, narrative: str, state: dict, present_npc_ids: list[str] | None = None) -> str:
        """如果NPC对话风格雷同，用角色行为器重写对话部分。"""
        if not present_npc_ids:
            return narrative
        # Extract dialogue for lorebook regardless of consistency check
        self._accumulate_npc_dialogue_style(narrative, present_npc_ids)
        if self._check_npc_voice_consistency(narrative, present_npc_ids):
            return narrative

        logger.info("NPC声音校验未通过，触发对话修正")
        # 用角色行为器的prompt生成修正后的对话
        voice_table = self.prompt_builder._build_npc_voice_table(state, present_npc_ids or [])
        system = (
            "你是NPC对话修正师。下方叙事中的NPC对话风格过于雷同，请根据声纹速查表重写对话部分。\n\n"
            "规则：\n"
            "- 只修改引号内的对话内容和说话动作描写\n"
            "- 不要修改环境描写和剧情事实\n"
            "- 每个NPC的说话方式必须明显不同\n"
            "- 保持对话的语义不变，只改风格\n"
            "- 输出完整的修正后叙事"
        )
        content = f"原始叙事:\n{narrative}\n\n{voice_table}"
        messages = [{"role": "user", "content": content}]

        try:
            raw = await self.ai_provider.generate(messages, system=system, max_tokens=8192, **self._stage_kwargs("narrative"))
            fixed = strip_think_tags(raw).strip()
            if len(fixed) > len(narrative) * 0.5:
                return fixed
        except Exception as e:
            logger.warning("NPC声音修正调用失败: %s", e)

        return narrative

    async def _review_narrative(
        self, narrative: str, action_text: str, ctx: dict,
        *, state: dict | None = None,
    ) -> dict | None:
        """Stage 3.5: AI 叙事质量评审。返回 {pass, violations} 或 None。"""
        if not self.ai_provider or not narrative:
            return None
        _st = state if state is not None else self.current_state
        npc_states = _st.get("npcs", {})
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        present_npcs = []
        for nid in (ctx.get("present_npc_ids") or [])[:6]:
            ns = npc_states.get(nid, {})
            nd = npc_defs.get(nid, {})
            if not isinstance(ns, dict):
                continue
            present_npcs.append({
                "id": nid,
                "name": ns.get("name", nid),
                "title": ns.get("title") or nd.get("title") or nd.get("role") or nd.get("occupation", ""),
            })
        msgs, sys_prompt = self.prompt_builder.build_narrative_review_prompt(
            narrative, action_text, present_npcs,
            pc_name=_st.get("player", {}).get("name", ""),
        )
        try:
            raw = await self.ai_provider.generate(
                msgs, system=sys_prompt, max_tokens=500,
                **self._stage_kwargs("state"),
            )
            raw = strip_think_tags(raw)
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                result = json.loads(m.group())
                if isinstance(result, dict) and "pass" in result:
                    return result
        except Exception as e:
            logger.warning("叙事质量评审失败: %s", e)
        return None

    @staticmethod
    def _trim_plot_sections(plot_decision: str, known_objects: set[str] | None = None) -> str:
        """Parse Stage 1 output into sections and enforce per-section item limits.

        Handles repeated section headers (AI sometimes outputs [关键事件] multiple
        times instead of a list) by merging them, then deduplicates and caps.
        """
        if not plot_decision:
            return plot_decision

        # Section header regex: [SectionName]
        section_re = re.compile(r'^\[(.+?)\]\s*', re.MULTILINE)
        sections: dict[str, list[str]] = {}
        order: list[str] = []
        last_key = None
        last_start = 0

        for m in section_re.finditer(plot_decision):
            if last_key is not None:
                chunk = plot_decision[last_start:m.start()].strip()
                if chunk:
                    sections.setdefault(last_key, []).append(chunk)
                if last_key not in order:
                    order.append(last_key)
            last_key = m.group(1)
            last_start = m.end()
        if last_key is not None:
            chunk = plot_decision[last_start:].strip()
            if chunk:
                sections.setdefault(last_key, []).append(chunk)
            if last_key not in order:
                order.append(last_key)

        if not sections:
            return plot_decision

        # Per-section item limits
        limits = {"关键事件": 3, "世界脉搏": 2, "NPC决策": 3, "场景约束": 3}
        item_split_re = re.compile(r'[;；\n]+')

        assembled: dict[str, str] = {}
        for key in order:
            chunks = sections.get(key, [])
            raw_text = "\n".join(chunks)
            items = [s.strip() for s in item_split_re.split(raw_text) if s.strip()]
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for item in items:
                if item not in seen:
                    seen.add(item)
                    unique.append(item)
            limit = limits.get(key)
            if limit and len(unique) > limit:
                unique = unique[:limit]
            assembled[key] = "；".join(unique) if len(unique) > 1 else (unique[0] if unique else "")

        # Validate [场景约束]: filter out items mentioning unknown objects
        if known_objects and "场景约束" in assembled:
            scene_text = assembled["场景约束"]
            obj_match = re.search(r'物件\s*[=＝]\s*([^;；\n]+)', scene_text)
            if obj_match:
                raw_objects = [o.strip() for o in obj_match.group(1).split(",") if o.strip()]
                # Keep only objects that match known sources (substring match)
                valid = [o for o in raw_objects if any(k in o or o in k for k in known_objects)]
                if len(valid) < len(raw_objects):
                    if valid:
                        new_obj_str = ", ".join(valid)
                    else:
                        new_obj_str = "无"
                    assembled["场景约束"] = scene_text[:obj_match.start(1)] + new_obj_str + scene_text[obj_match.end(1):]

        # Reassemble
        parts = []
        for key in order:
            if key in assembled and assembled[key]:
                parts.append(f"[{key}] {assembled[key]}")
        return "\n".join(parts)

    async def _execute_pipeline(
        self, ctx: dict, route: dict, player_action: dict, *,
        streaming: bool = False,
        state_baseline: dict | None = None,
    ):
        """Shared Stage 1→5 pipeline. Async generator yielding intermediate chunks
        and a final pipeline_result.

        state_baseline: if provided (regenerate), use this instead of self.current_state
                        for prompt building. None means use self.current_state.
        """
        _state = state_baseline if state_baseline is not None else self.current_state
        _warnings: list[str] = []
        action_text = ctx.get("action_text") or player_action.get("text", "")

        # --- Shared pre-Stage-1 setup ---
        _recent_for_tail = ctx.get("recent_nodes", [])
        _recent_narratives = []
        _prev_tail = ""
        if _recent_for_tail:
            _rn = _recent_for_tail[-1]
            _rn_action = _rn.get("player_action")
            _rn_action_text = ""
            if _rn_action:
                _rn_action_text = _rn_action.get("text", "") if isinstance(_rn_action, dict) else str(_rn_action)
            _rn_narrative = _rn.get("ai_response", "")
            if _rn_narrative:
                _recent_narratives.append({
                    "turn": _rn.get("turn_number", "?"),
                    "action": _rn_action_text,
                    "narrative": _rn_narrative,
                })
                _prev_tail = _rn_narrative[-500:] if len(_rn_narrative) > 500 else _rn_narrative

        _prev_plot = ctx.get("prev_plot_decision", "")

        _tools = GAME_TOOLS if self.script.get("settings", {}).get("ai_tools_enabled") else None
        _use_native_tools = _tools and hasattr(self.ai_provider, 'generate_with_tools')
        _focus_npcs = route.get("focus_npcs") or ctx.get("present_npc_ids")
        _sd_plot = ctx.get("stage_directives", {}).get("plot")
        _plot_hctx = "\n".join(["## 当前剧情线指令"] + _sd_plot) if _sd_plot else ""
        _pacing = _state.get("pacing_state", {})
        _pacing_tension = _pacing.get("tension", 50)
        _pacing_rec = _pacing.get("recommendation", "")
        if _pacing_tension < 40:
            _pacing_hint = (
                f"## 叙事节奏\n紧张度:{_pacing_tension}/100\n"
                "低紧张期：[关键事件]以日常因果为主。[世界脉搏]最多一条暗示性观察（看到/听到），"
                "不引发即时冲突，不揭示结论。整体氛围应是'日常中偶有不对劲'而非'步步惊心'。"
            )
        elif _pacing_tension < 70:
            _pacing_hint = (
                f"## 叙事节奏\n紧张度:{_pacing_tension}/100\n"
                "中紧张期：可以有一个信息推进或异常发现，但必须与已有线索/NPC关联。"
                "不要同时堆叠多个新悬疑元素。"
            )
        else:
            _pacing_hint = (
                f"## 叙事节奏\n紧张度:{_pacing_tension}/100\n"
                "高紧张期：可以有明确冲突、多条信息同时涌来、或NPC态度急转。"
            )
        if _pacing_rec:
            _pacing_hint += f"\n{_pacing_rec}"
        _plot_hctx = (_plot_hctx + "\n\n" + _pacing_hint).strip() if _plot_hctx else _pacing_hint
        _cfb = _state.get("_compose_feedback", "")
        if _cfb:
            _plot_hctx = (_plot_hctx + f"\n\n## 上轮叙事问题（本轮骨架需规避）\n{_cfb}").strip()
        _dissolves = _state.get("_pulse_dissolves")
        if _dissolves:
            _dissolve_text = "；".join(d if isinstance(d, str) else d.get("text", "") for d in _dissolves[:3])
            _plot_hctx = (_plot_hctx + f"\n\n## 可消解的旧暗示（非强制，有自然机会时收束）\n"
                          f"以下前几轮的世界脉搏暗示未转化为正式事件，如果本轮场景有合理契机，"
                          f"可以在[世界脉搏]中一笔带过给出日常解释（如'原来只是例行巡逻'）。"
                          f"如无契机则忽略，不要强行插入：\n"
                          f"{_dissolve_text}").strip()
        _lore_summary = self._build_lore_summary_for_plot(ctx.get("activated_lore", []))
        if _lore_summary:
            _plot_hctx = (_plot_hctx + f"\n\n{_lore_summary}").strip() if _plot_hctx else _lore_summary
        _profile_hint = self._build_player_profile_hint(_state)
        if _profile_hint:
            _plot_hctx = (_plot_hctx + f"\n\n{_profile_hint}").strip() if _plot_hctx else _profile_hint

        if route.get("scene_type") == "opening":
            _opening_base = ctx.get("base_history_context", "")
            if _opening_base:
                _plot_hctx = (_opening_base + "\n\n" + _plot_hctx).strip() if _plot_hctx else _opening_base

        # Feature #1: NPC scene hijack directive
        if route.get("scene_type") == "npc_hijack":
            _hijack = _state.get("_scene_hijack") or {}
            _hijack_hint = (
                f"## 场景劫持\n本回合由NPC主导场景。{_hijack.get('npc_name', '')}因「{_hijack.get('reason', '')}」"
                f"打断玩家行动。玩家的行动被中断，叙事焦点转移到NPC的主动行为上。\n"
                f"建议场景: {_hijack.get('suggested_action', '')}"
            )
            _plot_hctx = (_hijack_hint + "\n\n" + _plot_hctx).strip()

        # Feature #3: Plan decomposition directive
        if route.get("has_plan_declaration"):
            _plot_hctx = (
                _plot_hctx + "\n\n## 计划分解\n"
                "玩家声明了一个多步骤计划。在[行动结果]之后额外输出:\n"
                "plan_steps: [\"步骤1\", \"步骤2\", ...]\n"
                "plan_risk: \"low|medium|high\"\n"
                "将计划分解为3-5个具体可执行步骤，评估整体风险。"
            ).strip()

        # Feature #4: Active war context
        faction_wars = _state.get("faction_wars", [])
        player_loc = _state.get("player", {}).get("location", "")
        for war in faction_wars:
            if war.get("status") in ("skirmish", "open_war"):
                territories = war.get("territories", {})
                if player_loc in territories:
                    controller = territories[player_loc]
                    _war_hint = (
                        f"## 战争氛围\n玩家所在地被{self._get_org_name(controller)}控制，"
                        f"{'全面战争' if war['status'] == 'open_war' else '武装冲突'}进行中。"
                        f"天平偏向: {'进攻方' if war.get('balance', 0) > 0 else '防守方'}({abs(war.get('balance', 0))}%)"
                    )
                    _plot_hctx = (_plot_hctx + "\n\n" + _war_hint).strip()
                    break

        # --- Stage 1: 剧情决策 ---
        plot_msgs, plot_sys = self.prompt_builder.build_plot_decision_prompt(
            action_text,
            _state,
            check_result=ctx.get("check_result"),
            dice_results=ctx.get("dice_dicts"),
            triggered_events=ctx.get("triggered_events"),
            triggered_consequences=ctx.get("triggered_consequences"),
            achieved_milestones=ctx.get("achieved_milestones"),
            present_npc_ids=_focus_npcs,
            history_context=_plot_hctx,
            recent_reasoning=None,
            game_tools=None if _use_native_tools else _tools,
            prev_narrative_tail=_prev_tail,
            prev_plot_decision=_prev_plot,
            recent_narratives=_recent_narratives or None,
            event_sections=ctx.get("event_sections"),
        )

        if _use_native_tools:
            plot_decision, tool_results = await self._stage1_with_native_tools(
                plot_msgs, plot_sys, GAME_TOOLS_SCHEMA,
                max_tokens=8192, **self._stage_kwargs("narrative")
            )
            plot_reasoning = ""
        else:
            raw_plot = await self.ai_provider.generate(
                plot_msgs, system=plot_sys, max_tokens=8192, **self._stage_kwargs("narrative")
            )
            plot_reasoning = _extract_reasoning(raw_plot)
            plot_decision = strip_think_tags(raw_plot)
            plot_decision, tool_results = self._execute_tool_calls(plot_decision)
            if tool_results:
                tool_context = "\n".join(f"[{r['tool']}结果: {r['result']}]" for r in tool_results)
                plot_decision = plot_decision + "\n" + tool_context

        if plot_decision and plot_decision.lstrip().startswith(("```json", "```\n[", "[{")):
            _marker = "[行动结果]"
            _pos = plot_decision.find(_marker)
            if _pos > 0:
                logger.info("Stage 1 输出包含前缀 JSON，已截取骨架部分")
                plot_decision = plot_decision[_pos:]

        ctx["plot_reasoning"] = plot_reasoning
        # Build known objects set for scene constraint validation
        _known_objects = set()
        for it in _state.get("inventory", []):
            _item_name = it.get("item", "")
            if _item_name:
                _known_objects.add(_item_name)
        _player_loc = _state.get("player", {}).get("location", "")
        if _player_loc:
            _loc_def = self.prompt_builder._location_by_id.get(_player_loc, {})
            _loc_desc = _loc_def.get("description", "")
            if _loc_desc:
                _known_objects.add(_loc_desc)
        plot_decision = self._trim_plot_sections(plot_decision, _known_objects or None)
        ctx["plot_decision"] = plot_decision
        logger.info("=== Stage 1 骨架 ===\n%s", plot_decision)

        # Start compose context build in parallel with Stage 2
        _scope = route.get("scope", "moderate")
        _compose_ctx_task = asyncio.create_task(
            self._build_compose_context(plot_decision, ctx, scope=_scope, state=_state)
        )

        # Shared pre-computation for Stage 2/3
        recent_openings = []
        for node in ctx.get("recent_nodes", [])[-3:]:
            resp = node.get("ai_response", "")
            if resp:
                recent_openings.append(resp[:20])

        _prev_ending_type = ""
        if _prev_tail:
            _last_100 = _prev_tail[-100:]
            if '"' in _last_100 or '“' in _last_100 or '”' in _last_100:
                _prev_ending_type = "对话未完"
            elif any(w in _last_100 for w in ("走", "转身", "站起", "推开", "拿起", "迈")):
                _prev_ending_type = "动作收束"
            elif any(w in _last_100 for w in ("也许", "或许", "不知道", "？", "……")):
                _prev_ending_type = "悬念留白"
            else:
                _prev_ending_type = "画面定格"

        _nearby_hint = self.prompt_builder.build_nearby_npc_hint(ctx.get("nearby_npc_ids", []), _state)
        _use_merged_narrative = _use_native_tools  # P2: 合并 Stage 2+3 当工具调用可用时

        if _use_merged_narrative:
            # --- Stage 2+3 合并：单次叙事生成（工具调用模式）---
            compose_history = await _compose_ctx_task

            narrative_msgs, narrative_sys = self.prompt_builder.build_narrative_prompt(
                ctx, plot_decision, route, _state,
                recent_openings=recent_openings,
                history_context=compose_history,
                prev_narrative_tail=_prev_tail,
                prev_ending_type=_prev_ending_type,
                authors_note=self.authors_note,
                action_text=action_text,
                negative_prompt=self.negative_prompt,
                logit_bias_hint=self._build_logit_bias_hint(),
                estimated_minutes=ctx.get("estimated_minutes", 30),
                event_sections=ctx.get("event_sections"),
            )
            # Inject lorebook and nearby NPC hints
            _pc_disc = _state.get("pc_discovered_lore", [])
            _vis_lore = Lorebook.filter_by_visibility(ctx["activated_lore"], "pc", _pc_disc)
            narrative_msgs = self.prompt_builder.inject_depth_lore(narrative_msgs, _vis_lore)
            if _nearby_hint and narrative_msgs:
                narrative_msgs[-1]["content"] += _nearby_hint
            # Inject stage directives
            _sd_env = ctx.get("stage_directives", {}).get("env")
            _sd_char = ctx.get("stage_directives", {}).get("char")
            _extra_directives = []
            if _sd_env:
                _extra_directives.append("## 环境剧情线指令\n" + "\n".join(_sd_env))
            if _sd_char:
                _extra_directives.append("## 角色剧情线指令\n" + "\n".join(_sd_char))
            if _extra_directives and narrative_msgs:
                narrative_msgs[-1]["content"] += "\n\n" + "\n\n".join(_extra_directives)

            env_text = ""
            char_text = ""

            # Variables needed by post-narrative code (Stage 4b-temporal, review, state settlement)
            _use_state_tools = True  # merged path always uses state tools
            _active_sys = route.get("systems") or None
            _check_res = ctx.get("check_result")
            _old_time = ctx.get("old_time", "")
            compose_msgs = narrative_msgs  # for review retry reuse
            compose_sys = narrative_sys

            async def _4b_gen(msgs, sys_prompt):
                async with self._4b_semaphore:
                    return await self.ai_provider.generate(msgs, system=sys_prompt, max_tokens=4096, **self._stage_kwargs("state"))

            # 合并叙事生成（工具调用模式不流式，等完整响应）
            _narrative_reasoning = ""
            _4b_tasks = []
            try:
                resp = await self.ai_provider.generate_with_tools(
                    narrative_msgs, system=narrative_sys,
                    tools=NARRATIVE_TOOLS_SCHEMA,
                    max_tokens=8192, **self._stage_kwargs("narrative")
                )
                raw_narrative = resp.get("content", "")
                _narrative_reasoning = _extract_reasoning(raw_narrative)
                narrative = strip_think_tags(raw_narrative)

                # 处理工具调用结果
                for tc in (resp.get("tool_calls") or []):
                    tc_name = tc.get("name", "")
                    tc_args = tc.get("arguments", {})
                    if tc_name == "set_atmosphere":
                        ctx["atmosphere"] = tc_args
                        logger.info("set_atmosphere: %s", tc_args)
                    elif tc_name == "set_scene_image":
                        ctx["scene_image_prompt"] = tc_args
                        logger.info("set_scene_image: %s", tc_args)
            except BaseException:
                for t in _4b_tasks:
                    t.cancel()
                raise

        else:
            # --- fallback: 原 Stage 2 + Stage 3 分离逻辑 ---
            # --- Stage 2: 环境渲染 ‖ 角色行为（自适应）---
            _scene_type = route.get("scene_type", "")
            _skip_env = _scene_type in ("social", "rest") or route.get("scope") == "minor"
            if route.get("scope") == "minor":
                env_text = ""
                char_text = ""
            else:
                if _skip_env:
                    env_text = ""
                else:
                    env_msgs, env_sys = self.prompt_builder.build_env_render_prompt(
                        plot_decision, _state
                    )
                    _sd_env = ctx.get("stage_directives", {}).get("env")
                    if _sd_env and env_msgs:
                        env_msgs[-1]["content"] += "\n\n## 环境剧情线指令\n" + "\n".join(_sd_env)
                    try:
                        raw_env = await self.ai_provider.generate(env_msgs, system=env_sys, max_tokens=8192, **self._stage_kwargs("narrative"))
                    except Exception as _env_err:
                        logger.warning("环境渲染失败: %s", _env_err)
                        raw_env = ""
                    env_text = strip_think_tags(raw_env) if raw_env else ""
                char_msgs, char_sys = self.prompt_builder.build_character_action_prompt(
                    plot_decision, _state,
                    present_npc_ids=ctx.get("present_npc_ids"),
                    dice_results=ctx.get("dice_dicts"),
                    check_result=ctx.get("check_result"),
                    triggered_events=ctx.get("triggered_events"),
                    triggered_consequences=ctx.get("triggered_consequences"),
                )
                _sd_char = ctx.get("stage_directives", {}).get("char")
                if _sd_char and char_msgs:
                    char_msgs[-1]["content"] += "\n\n## 角色剧情线指令\n" + "\n".join(_sd_char)
                if env_text and char_msgs:
                    _env_brief = env_text[:150]
                    char_msgs[-1]["content"] += f"\n\n== 已确定的环境描写（角色行为须与之一致）==\n{_env_brief}"
                try:
                    raw_char = await self.ai_provider.generate(char_msgs, system=char_sys, max_tokens=8192, **self._stage_kwargs("narrative"))
                except Exception as _char_err:
                    logger.warning("角色行为失败: %s", _char_err)
                    raw_char = ""
                char_text = strip_think_tags(raw_char) if raw_char else ""

            # --- Stage 3: 叙事润色整合 ---
            compose_history = await _compose_ctx_task

            compose_msgs, compose_sys = self.prompt_builder.build_narrative_compose_prompt(
                plot_decision, env_text, char_text, _state, recent_openings,
                missing_env=not env_text, missing_char=not char_text,
                history_context=compose_history,
                prev_narrative_tail=_prev_tail,
                scene_type=route.get("scene_type", ""),
                prev_ending_type=_prev_ending_type,
                authors_note=self.authors_note,
                action_text=action_text,
                negative_prompt=self.negative_prompt,
                logit_bias_hint=self._build_logit_bias_hint(),
                scope=route.get("scope", "moderate"),
                estimated_minutes=ctx.get("estimated_minutes", 30),
                event_sections=ctx.get("event_sections"),
            )
            _pc_disc = _state.get("pc_discovered_lore", [])
            _vis_lore = Lorebook.filter_by_visibility(ctx["activated_lore"], "pc", _pc_disc)
            compose_msgs = self.prompt_builder.inject_depth_lore(compose_msgs, _vis_lore)
            if _nearby_hint and compose_msgs:
                compose_msgs[-1]["content"] += _nearby_hint

            # --- Stage 3 ‖ 4b parallel launch ---
            _active_sys = route.get("systems") or None
            _check_res = ctx.get("check_result")
            _old_time = ctx.get("old_time", "")
            _use_state_tools = _use_native_tools and hasattr(self.ai_provider, 'generate_with_tools')

            # 4b parallel prompts/tasks only needed in fallback (non-tool) path
            _4b_tasks = []
            if not _use_state_tools:
                res_msgs, res_sys = self.prompt_builder.build_world_state_resource_prompt(
                    "", action_text, _state,
                    check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
                )
                spa_msgs, spa_sys = self.prompt_builder.build_world_state_spatial_prompt(
                    "", action_text, _state,
                    check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
                )
                wld_msgs, wld_sys = self.prompt_builder.build_world_state_world_prompt(
                    "", action_text, _state,
                    check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
                )
                ext_msgs, ext_sys = self.prompt_builder.build_world_state_ext_prompt(
                    "", action_text, _state,
                    check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
                    event_sections=ctx.get("event_sections"),
                )

                # stage directives → resource
                _sd_world = ctx.get("stage_directives", {}).get("world")
                if _sd_world and wld_msgs:
                    wld_msgs[-1]["content"] += "\n\n## 当前剧情线指令\n" + "\n".join(_sd_world)
                # lorebook context → resource
                _core_lore = self._build_lore_context_for_core(ctx.get("activated_lore", []))
                if _core_lore and res_msgs:
                    res_msgs[-1]["content"] += _core_lore
                # nearby NPC hint → spatial (reuse cached _nearby_hint from Stage 3)
                if _nearby_hint and spa_msgs:
                    spa_msgs[-1]["content"] += _nearby_hint
                # story context → ext
                _story_ctx = self.prompt_builder._build_story_context_section(_state)
                if _story_ctx and ext_msgs:
                    ext_msgs[-1]["content"] += f"\n\n{_story_ctx}"

                # Launch 4b tasks (concurrency limited by _4b_semaphore)
                async def _4b_gen(msgs, sys_prompt):
                    async with self._4b_semaphore:
                        return await self.ai_provider.generate(msgs, system=sys_prompt, max_tokens=4096, **self._stage_kwargs("state"))

                _4b_res_task = asyncio.create_task(_4b_gen(res_msgs, res_sys))
                _4b_spa_task = asyncio.create_task(_4b_gen(spa_msgs, spa_sys))
                _4b_wld_task = asyncio.create_task(_4b_gen(wld_msgs, wld_sys))
                _4b_ext_task = asyncio.create_task(_4b_gen(ext_msgs, ext_sys))
                _4b_tasks = [_4b_res_task, _4b_spa_task, _4b_wld_task, _4b_ext_task]

            # Stage 3: narrative generation (streaming or non-streaming)
            _narrative_reasoning = ""
            try:
                if streaming:
                    full_narrative = ""
                    _think_parts = []
                    raw_stream = self.ai_provider.generate_stream(
                        compose_msgs, system=compose_sys, raw=True, **self._stage_kwargs("narrative")
                    )
                    async for msg_type, chunk in stream_split_think(raw_stream):
                        if msg_type == "think":
                            _think_parts.append(chunk)
                            yield {"type": "thinking", "content": chunk}
                        else:
                            full_narrative += chunk
                            yield {"type": "text", "content": chunk}
                    narrative = strip_think_tags(full_narrative)
                    if _think_parts:
                        _narrative_reasoning = "".join(_think_parts)
                else:
                    raw_narrative_result = await self.ai_provider.generate(
                        compose_msgs, system=compose_sys, raw=True, **self._stage_kwargs("narrative")
                    )
                    _narrative_reasoning = _extract_reasoning(raw_narrative_result)
                    narrative = strip_think_tags(raw_narrative_result)
            except BaseException:
                for t in _4b_tasks:
                    t.cancel()
                raise

            # 素材复用率监控
            if char_text and narrative:
                _n = 6
                _src_ngrams = set(char_text[i:i+_n] for i in range(max(0, len(char_text) - _n + 1)))
                _out_ngrams = [narrative[i:i+_n] for i in range(max(0, len(narrative) - _n + 1))]
                if _out_ngrams and _src_ngrams:
                    _reuse = sum(1 for ng in _out_ngrams if ng in _src_ngrams) / len(_out_ngrams)
                    if _reuse > 0.6:
                        logger.warning("Stage 3 素材复用率 %.1f%%（高于60%%阈值）", _reuse * 100)

        # NPC 声音校验
        pre_fix_narrative = narrative
        narrative = await self._maybe_fix_npc_voices(narrative, _state, ctx.get("present_npc_ids"))
        if streaming and narrative != pre_fix_narrative:
            yield {"type": "narrative_revised", "content": narrative}

        # Stage 4b-temporal: 延迟到叙事完成后（仅 fallback 路径）
        if not _use_state_tools:
            tmp_msgs, tmp_sys = self.prompt_builder.build_world_state_temporal_prompt(
                narrative, action_text, _state,
                check_result=_check_res, active_systems=_active_sys, plot_decision=plot_decision,
            )
            if tmp_msgs:
                tmp_msgs[-1]["content"] += (
                    f"\n\n## end_time 决策指引\n"
                    f"当前游戏时间: {_old_time}\n"
                    "你是end_time的唯一决策者。根据叙事最后场景的时间输出绝对时间戳：\n"
                    "- 对话/观察/翻阅文件: 当前时间 +10~30分钟\n"
                    "- 常规互动/短途移动: 当前时间 +30分钟~2小时\n"
                    "- 长途旅行/大型战斗: 当前时间 +2~8小时\n"
                    "- 睡觉/过夜: 若叙事写到入睡那一刻则给入睡时间（如23:30），若叙事写到醒来才给次日早晨\n"
                    f"格式示例: {_old_time[:10] or '1970-01-01'}T10:00:00"
                )
            _4b_tmp_task = asyncio.create_task(_4b_gen(tmp_msgs, tmp_sys))
            _4b_tasks.append(_4b_tmp_task)

        # ★ Stage 3.5 review ‖ NPC RAG 并行 ★
        async def _do_review():
            return await self._review_narrative(narrative, action_text, ctx, state=_state)

        async def _do_npc_rag():
            if not (self.vector_memory and ctx.get("present_npc_ids")):
                return ""
            _npc_st = _state.get("npcs", {})
            npc_names = [_npc_st.get(nid, {}).get("name", nid)
                         for nid in ctx["present_npc_ids"][:3]
                         if isinstance(_npc_st.get(nid), dict)]
            if not npc_names:
                return ""
            npc_hits = await self._stage_rag_query(
                " ".join(npc_names), top_k=3, doc_type="turn",
                exclude_turns=[self.turn_number],
            )
            if npc_hits:
                return "\n".join(f"第{r['turn']}回合: {r['text'][:200]}" for r in npc_hits)
            return ""

        review, npc_rag_context = await asyncio.gather(
            _do_review(), _do_npc_rag(),
        )

        if review and not review.get("pass"):
            violations = review.get("violations", [])
            logger.info("叙事质量校验未通过: %s", violations)
            violation_text = "\n".join(
                f"- {v.get('type', '?')}: {v.get('detail', '')}" for v in violations if isinstance(v, dict)
            )
            if violation_text:
                retry_msgs = [dict(m) for m in compose_msgs]
                retry_msgs[-1]["content"] += (
                    f"\n\n## 上次生成被审核拒绝，请修正以下问题后重写：\n{violation_text}"
                )
                try:
                    retry_raw = await self.ai_provider.generate(
                        retry_msgs, system=compose_sys, **self._stage_kwargs("narrative")
                    )
                    narrative = strip_think_tags(retry_raw)
                    if streaming:
                        yield {"type": "narrative_revised", "content": narrative}
                except Exception as e:
                    logger.warning("叙事重试失败: %s", e)

        # 汇合 Stage 4b 结果
        if _use_state_tools:
            # 工具调用路径：单次调用替代 5 路并行
            try:
                parsed = await self._execute_state_settlement(
                    ctx, narrative, plot_decision, action_text, _state,
                    old_time=_old_time,
                )
            except Exception as e:
                logger.warning("状态推演工具调用失败，回退到空结果: %s", e)
                parsed = self.response_parser._empty_result()
                parsed["narrative"] = narrative.strip()
                _warnings.append("状态推演工具调用失败，本回合属性/物品变化可能未正确记录")
        else:
            # fallback: 原 5 路并行 + parse_split_v3
            _4b_raw = await asyncio.gather(
                _4b_res_task, _4b_spa_task, _4b_tmp_task, _4b_wld_task, _4b_ext_task,
                return_exceptions=True,
            )
            [raw_resource, raw_spatial, raw_temporal, raw_world, raw_world_ext], _4b_warns = self._sanitize_gather_results(
                _4b_raw, [("资源状态推演", True), ("空间状态推演", False),
                           ("时间状态推演", False), ("世界属性推演", False), ("扩展状态推演", False)],
            )
            _warnings.extend(_4b_warns)

            # partial parse（不含NPC）→ 提取 world_change_hints
            parsed = self.response_parser.parse_split_v3(narrative, "", raw_resource, raw_spatial, raw_temporal, raw_world, raw_world_ext)

            if parsed.get("_state_parse_failed"):
                _warnings.append("状态推演部分失败，本回合属性/物品变化可能未正确记录")

        new_scene = parsed.get("scene_details")
        if new_scene and isinstance(new_scene, dict):
            _state["scene_details"] = new_scene

        _wch_parts = []
        for _rl in parsed.get("reveal_locations", []):
            _rln = _state.get("display_names", {}).get(_rl, _rl) if isinstance(_rl, str) else str(_rl)
            _wch_parts.append(f"- 新发现地点: {_rln}")
        for _frc in parsed.get("faction_reputation_changes", []):
            if abs(_frc.get("change", 0)) >= 15:
                _wch_parts.append(f"- 阵营声望剧变: {_frc.get('faction_id', '?')} {_frc.get('change', 0):+d}")
        if parsed.get("location_change"):
            _lcn = _state.get("display_names", {}).get(parsed["location_change"], parsed["location_change"])
            _wch_parts.append(f"- 位置已变更至: {_lcn}")
        for _as in parsed.get("activate_states", []):
            _wch_parts.append(f"- 状态生效: {_as}")
        _world_change_hints = ("\n\n## 本回合世界变化（选项可反映这些变化）\n" + "\n".join(_wch_parts)) if _wch_parts else ""

        _story_hints = ""
        if self.story_tree_engine:
            _st_sts = _state.get("story_tree_state", {})
            _sh_parts = []
            for _sh_nid in _st_sts.get("active", []):
                _sh_node = self.story_tree_engine._nodes.get(_sh_nid)
                if _sh_node and _sh_node.get("type") in ("quest", "choice"):
                    _sh_parts.append(f"- [活跃] {_sh_node.get('name', _sh_nid)}: {_sh_node.get('description', '')[:60]}")
            for _sh_un in self.story_tree_engine.get_upcoming_nodes(_state, limit=2):
                _sh_parts.append(f"- [即将] {_sh_un.get('name', '')}: {_sh_un.get('description', '')[:60]}")
            if _sh_parts:
                _story_hints = "\n\n## 活跃剧情线（至少1个选项应与此相关）\n" + "\n".join(_sh_parts)

        # --- Stage 4a ‖ Stage 5 并行 ---
        npc_lore_ids = set()
        _present_npc_ids = ctx.get("present_npc_ids", [])
        for nid in _present_npc_ids:
            npc_def = self._npc_by_id.get(nid, {})
            npc_lore_ids.update(npc_def.get("related_lore", []))
        _npc_base_lore = [
            e for e in ctx.get("activated_lore", [])
            if e.constant or e.id in npc_lore_ids
        ] if npc_lore_ids else ctx.get("activated_lore", [])
        npc_filtered_lore = Lorebook.filter_by_visibility(
            _npc_base_lore, "npc", npc_ids=_present_npc_ids,
        ) if _npc_base_lore else None

        _sd_npc = ctx.get("stage_directives", {}).get("npc")
        if _sd_npc:
            _npc_dir = "## 当前剧情线NPC指令\n" + "\n".join(_sd_npc)
            npc_rag_context = (npc_rag_context + "\n" + _npc_dir).strip() if npc_rag_context else _npc_dir

        npc_msgs, npc_sys = self.prompt_builder.build_npc_reaction_prompt(
            narrative, action_text, _state,
            check_result=ctx.get("check_result"),
            present_npc_ids=ctx.get("present_npc_ids"),
            npc_history=npc_rag_context,
            npc_lore=npc_filtered_lore,
        )
        _state["_nearby_npc_ids"] = ctx.get("nearby_npc_ids", [])
        choices_msgs, choices_sys = self.prompt_builder.build_choices_prompt(
            narrative, action_text, _state,
            turn_number=self.turn_number, activated_lore=ctx["activated_lore"],
            story_hints=_story_hints, world_change_hints=_world_change_hints,
            event_sections=ctx.get("event_sections"),
            pc_discovered_lore=_state.get("pc_discovered_lore", []))
        _state.pop("_nearby_npc_ids", None)

        _4a5_raw = await asyncio.gather(
            self.ai_provider.generate(npc_msgs, system=npc_sys, max_tokens=4096, **self._stage_kwargs("state")),
            self.ai_provider.generate(choices_msgs, system=choices_sys, max_tokens=8192, **self._stage_kwargs("choices")),
            return_exceptions=True,
        )
        [raw_npc, raw_choices], _4a5_warns = self._sanitize_gather_results(
            _4a5_raw, [("NPC关系推演", True), ("选项生成", False)],
        )
        _warnings.extend(_4a5_warns)

        # merge NPC results into parsed
        npc_parsed = self.response_parser.parse_npc_reaction(raw_npc)
        for k, v in npc_parsed.items():
            if k == "scene_details" and parsed.get("scene_details"):
                sd = parsed["scene_details"]
                if isinstance(v, dict):
                    if v.get("npc_expressions"):
                        sd.setdefault("npc_expressions", v["npc_expressions"])
                    if v.get("pending_tension"):
                        sd.setdefault("pending_tension", v["pending_tension"])
            else:
                parsed[k] = v

        parsed["choices"] = self.response_parser.parse_choices(raw_choices)

        if not parsed.get("choices"):
            parsed["choices"] = self._generate_context_choices()

        yield {
            "type": "pipeline_result",
            "narrative": narrative,
            "parsed": parsed,
            "warnings": _warnings,
            "plot_decision": plot_decision,
            "plot_reasoning": plot_reasoning,
            "narrative_reasoning": _narrative_reasoning,
            "compose_msgs": compose_msgs,
            "compose_sys": compose_sys,
        }

    async def _enrich_and_route(self, player_action: dict, ctx: dict) -> dict:
        """RAG 增强 + Route 分类 + 技能检定。返回 route。"""
        action_text = player_action.get("text", "")
        ctx["history_context"], ctx["lore_ids_from_rag"] = await self._enrich_with_rag(
            action_text, ctx["recent_nodes"],
            ctx.get("activated_lore", []), ctx.get("history_context", ""),
        )
        ctx["databank_hits"] = getattr(self, '_last_bank_results', [])
        self._last_bank_results = []
        route = await self._run_route_stage(action_text, ctx)
        ctx["route"] = route
        self._boost_lore_by_route(ctx.get("activated_lore", []), route)
        ctx["check_result"] = self._resolve_skill_check_from_route(route, action_text)
        return route

    async def process_action(self, player_action: dict) -> dict:
        """Process a player action and return the result.

        8-stage pipeline: plot_decision → env‖char → compose → npc‖world → choices.
        Falls back to legacy 3-step if new pipeline encounters critical errors.
        """
        if not self.ai_provider:
            raise RuntimeError("AI provider not configured")
        await self._drain_background_tasks()  # P0-3
        rollback_turn = self.turn_number
        rollback_state = None
        try:
            ctx = self._prepare_turn(player_action)
            rollback_state = ctx["rollback_state"]

            route = await self._enrich_and_route(player_action, ctx)

            # --- Stage 1→5 via shared pipeline ---
            _warnings = []
            narrative = ""
            parsed = {}
            _narrative_reasoning = ""
            async for item in self._execute_pipeline(ctx, route, player_action):
                if item["type"] == "pipeline_result":
                    narrative = item["narrative"]
                    parsed = item["parsed"]
                    _warnings = item["warnings"]
                    _narrative_reasoning = item.get("narrative_reasoning", "")
                    break

            result = await self._apply_parsed_response(parsed, narrative, player_action, ctx)
            if _narrative_reasoning:
                result["thinking"] = _narrative_reasoning
                node = self.world_tree.get_node(self.world_tree.active_node_id)
                if node:
                    node["thinking"] = _narrative_reasoning
            if _warnings:
                result.setdefault("warnings", []).extend(_warnings)
            return result
        except Exception:
            if rollback_state is not None:
                self.current_state = rollback_state
            self.turn_number = rollback_turn
            raise

    async def process_action_stream(self, player_action: dict):
        """Process action with streaming AI response. Yields chunks.

        8-stage pipeline: plot → env‖char → compose(streamed) → npc‖world → choices.
        注意：流式模式下的回滚由 API 层 (saving_event_stream) 统一处理，
        session 层不做 rollback，避免双重回滚导致状态混乱。
        调用方需自行在 finally 中处理 state/turn_number/world_tree 的恢复。
        """
        if not self.ai_provider:
            raise RuntimeError("AI provider not configured")
        await self._drain_background_tasks()  # P0-3
        ctx = self._prepare_turn(player_action)

        route = await self._enrich_and_route(player_action, ctx)

        # --- Stage 1→5 via shared pipeline (streaming) ---
        _warnings = []
        narrative = ""
        parsed = {}
        _narrative_reasoning = ""
        async for item in self._execute_pipeline(ctx, route, player_action, streaming=True):
            if item["type"] == "pipeline_result":
                narrative = item["narrative"]
                parsed = item["parsed"]
                _warnings = item["warnings"]
                _narrative_reasoning = item.get("narrative_reasoning", "")
            else:
                yield item  # thinking/text/narrative_revised

        result = await self._apply_parsed_response(
            parsed, narrative, player_action, ctx
        )
        if _narrative_reasoning:
            result["thinking"] = _narrative_reasoning
            node = self.world_tree.get_node(self.world_tree.active_node_id)
            if node:
                node["thinking"] = _narrative_reasoning
        if _warnings:
            result.setdefault("warnings", []).extend(_warnings)

        # Scene image generation — only when route decides visual change is significant
        should_gen_image = ctx.get("route", {}).get("generate_image", False)
        if should_gen_image and getattr(self, "_image_provider", None) and narrative:
            try:
                from ai.image_prompt_builder import build_image_prompt
                loc = self.current_state.get("player", {}).get("location", "")
                loc_data = self._location_by_id.get(loc, {})
                loc_name = loc_data.get("name", loc) if loc_data else loc
                loc_desc = loc_data.get("description", "") if loc_data else ""
                mood = ctx.get("mood", "tense")
                tod = self.current_state.get("time_of_day", "day")
                weather = self.current_state.get("current_weather", "")

                characters = []
                pc = self.script.get("player_character", {})
                pc_app = pc.get("appearance") or pc.get("bio") or ""
                if pc_app:
                    characters.append(f"Player: {pc_app[:200]}")
                npc_states = self.current_state.get("npcs", {})
                for nid, ns in list(npc_states.items())[:5]:
                    if isinstance(ns, dict) and ns.get("current_location") == loc:
                        npc_def = self._npc_by_id.get(nid, {})
                        desc = npc_def.get("appearance") or npc_def.get("bio") or ""
                        name = ns.get("name") or npc_def.get("name", nid)
                        if desc:
                            characters.append(f"{name}: {desc[:150]}")

                image_style = ""
                try:
                    from api.config_routes import get_image_style
                    style_data = await get_image_style()
                    image_style = style_data.get("custom") or style_data.get("preset") or ""
                except Exception:
                    pass

                img_prompt = await build_image_prompt(
                    narrative, loc_name, mood, tod, self.ai_provider,
                    weather=weather, characters=characters,
                    location_desc=loc_desc, image_style=image_style,
                )
                scene_img = await self._image_provider.generate_image(img_prompt)
                scene_img["_prompt"] = img_prompt
                result["scene_image"] = scene_img
            except Exception as e:
                logger.warning("Scene image generation failed: %s", e)
                result["scene_image"] = None
        else:
            result["scene_image"] = None

        yield {"type": "final", **result}

    async def branch_to_node(self, node_id: str) -> dict | None:
        """Switch to a different branch by loading a past node's state."""
        await self._drain_background_tasks()
        node = self.world_tree.get_node(node_id)
        if not node:
            return None
        self.world_tree.set_active_node(node_id)

        # Load snapshot from DB if it was evicted from memory
        snapshot = node.get("state_snapshot")
        if not snapshot:
            snapshot = await self._load_snapshot_from_db(node_id)
            if snapshot:
                node["state_snapshot"] = snapshot

        # B2: 如果快照为 None 且 DB 也加载不到，返回错误而非使用空 dict
        if not snapshot:
            return {"error": "状态快照已丢失且无法从数据库恢复，无法切换分支"}

        self.current_state = copy.deepcopy(snapshot)
        self.turn_number = node.get("turn_number", 0)
        return {
            "node_id": node_id,
            "narrative": node.get("ai_response", ""),
            "choices": node.get("choices_presented", []),
            "state": self.current_state,
        }

    async def _load_snapshot_from_db(self, node_id: str) -> dict | None:
        """Load a state_snapshot from the database for an evicted node."""
        try:
            from db.database import get_db
            async with get_db() as db:
                cursor = await db.execute(
                    "SELECT state_snapshot FROM tree_nodes WHERE id = ?", (node_id,)
                )
                row = await cursor.fetchone()
                if row and row["state_snapshot"]:
                    snapshot = json.loads(row["state_snapshot"])
                    # 提取 _node_context 到 node dict（如果有），不留在 state 中
                    nc = snapshot.pop("_node_context", None)
                    node = self.world_tree.get_node(node_id)
                    if nc and isinstance(nc, dict) and node:
                        node.setdefault("check_result", nc.get("check_result"))
                        node.setdefault("triggered_consequences", nc.get("triggered_consequences"))
                        node.setdefault("achieved_milestones", nc.get("achieved_milestones"))
                    return snapshot
        except Exception:
            pass
        return None

    def set_authors_note(self, note: str, position: str = "end", depth: int = 4):
        """Set the player's behind-the-scenes directive."""
        self.authors_note = note
        self.authors_note_position = position
        self.authors_note_depth = max(1, depth)

    async def npc_dialogue_round(self, topic: str = "") -> list[dict]:
        """Generate batch NPC speeches via single AI call with rich character prompts."""
        present_ids = self._get_present_npc_ids()
        if not present_ids:
            return []

        speaking_npcs = []
        for npc_id in present_ids:
            npc = self._npc_by_id.get(npc_id)
            if not npc:
                npc = self.current_state.get("npcs", {}).get(npc_id)
                if not npc or not isinstance(npc, dict):
                    continue
            talkativeness = npc.get("talkativeness", 50) / 100
            if random.random() > talkativeness:
                continue
            speaking_npcs.append((npc_id, npc))

        if not speaking_npcs:
            return []

        # --- Build merged system prompt with full character settings ---
        npc_profiles = []
        for npc_id, npc in speaking_npcs:
            profile = self.prompt_builder.build_npc_talk_prompt(self.current_state, npc_id)
            if profile:
                npc_profiles.append(f"=== 角色: {npc.get('name', npc_id)} (ID: {npc_id}) ===\n{profile}")

        if not npc_profiles:
            return []

        system_prompt = (
            "你需要同时扮演以下多个角色，为每人各写一段群聊对话。\n"
            "每个角色都有独立的性格、说话风格和态度，你必须严格区分。\n\n"
            + "\n\n".join(npc_profiles)
            + "\n\n## 输出要求\n"
            "- 返回纯JSON列表，格式: [{\"npc_id\":\"角色ID\",\"speech\":\"对话内容\"},...]"
            "\n- 每人50-150字，保持各自性格和说话风格"
            "\n- NPC之间可以互相回应、接话、争论"
            "\n- 不要包含```或其他标记，直接输出JSON"
        )

        # --- Build enhanced user message ---
        loc_id = self.current_state.get("player", {}).get("location", "")
        loc_name = self._location_by_id.get(loc_id, {}).get("name", loc_id) if loc_id else "未知"
        game_time = self.current_state.get("game_time", "未知")
        weather = self.current_state.get("current_weather", "")
        atmo = self.current_state.get("time_atmosphere", {})
        period = atmo.get("period_label", "")

        ctx_parts = [f"地点: {loc_name}", f"时间: {game_time}"]
        if weather:
            ctx_parts.append(f"天气: {weather}")
        if period:
            ctx_parts.append(f"时段: {period}")
        if topic:
            ctx_parts.append(f"话题: {topic}")

        # Dialogue history per NPC (last 1 round each)
        hist_lines = []
        for npc_id, npc in speaking_npcs:
            chat_hist = self.current_state.get("npc_chat_history", {}).get(npc_id, [])
            if chat_hist:
                last = [h for h in chat_hist if not h.get("_summary")][-1:]
                for h in last:
                    npc_msg = h.get("npc", "")
                    if npc_msg:
                        hist_lines.append(f"{npc.get('name', npc_id)}: {npc_msg[:60]}")
        if hist_lines:
            ctx_parts.append("近期对话:\n" + "\n".join(hist_lines))

        # Recent narrative snippet
        narr = self.current_state.get("last_narrative", "")
        if narr:
            ctx_parts.append(f"最近叙事: {narr[:100]}")

        # Information network: unique info held by present NPCs
        network = self.current_state.get("information_network", [])
        npc_id_set = {nid for nid, _ in speaking_npcs}
        info_lines = []
        for info in network[-10:]:
            holders = set(info.get("known_by", [])) & npc_id_set
            if holders:
                names = ", ".join(self._npc_by_id.get(h, {}).get("name", h) for h in holders)
                info_lines.append(f"[{names}知道] {info['fact'][:50]}")
        if info_lines:
            ctx_parts.append("NPC掌握的情报（可自然融入对话）:\n" + "\n".join(info_lines))

        user_msg = "\n".join(ctx_parts) + "\n\n请以上述角色各生成一段对话。"

        raw = await self.ai_provider.generate(
            [{"role": "user", "content": user_msg}],
            system=system_prompt,
            max_tokens=2000,
            **self._stage_kwargs("state"),
        )
        raw = strip_think_tags(raw or "").strip()

        parsed = GameSession._parse_simple_json_list(raw)
        if not parsed:
            return []

        results = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            npc_id = item.get("npc_id", "")
            speech = item.get("speech", "")
            if npc_id and speech and npc_id in npc_id_set:
                npc_name = self._npc_by_id.get(npc_id, {}).get("name", npc_id)
                results.append({"npc_id": npc_id, "name": npc_name, "speech": speech})

        if results:
            self.current_state["last_npc_speeches"] = results

        return results

    def _get_present_npc_ids(self) -> list[str]:
        """Get IDs of NPCs currently present at player's location."""
        loc = self.current_state.get("player", {}).get("location", "")
        if not loc:
            return []
        present = []
        npcs_state = self.current_state.get("npcs", {})
        for npc_id, npc_data in npcs_state.items():
            if not isinstance(npc_data, dict):
                continue
            npc_loc = npc_data.get("current_location", npc_data.get("default_location", ""))
            if npc_loc and self._locations_match(loc, npc_loc):
                present.append(npc_id)
        return present

    async def undo_last_turn(self) -> dict | None:
        """Flow#7: 撤销最近一次行动，回到父节点状态。"""
        await self._drain_background_tasks()
        active_node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not active_node:
            return None
        parent_id = active_node.get("parent_id")
        if not parent_id:
            return {"error": "已经是开局节点，无法撤销"}

        parent = self.world_tree.get_node(parent_id)
        if not parent:
            return {"error": "父节点不存在"}

        # 加载父节点状态快照
        snapshot = parent.get("state_snapshot")
        if not snapshot:
            snapshot = await self._load_snapshot_from_db(parent_id)
            if snapshot:
                parent["state_snapshot"] = snapshot
        if not snapshot:
            return {"error": "父节点状态快照已丢失，无法撤销"}

        # B4: 先移除当前节点（叶节点），再切到父节点；处理移除失败
        removed_id = active_node["id"]
        if not self.world_tree.remove_node(removed_id):
            return {"error": "无法移除当前节点（可能不是叶节点）"}
        self.world_tree.set_active_node(parent_id)

        self.current_state = copy.deepcopy(snapshot)
        self.turn_number = parent.get("turn_number", 0)
        return {
            "node_id": parent_id,
            "removed_node_id": removed_id,
            "narrative": parent.get("ai_response", ""),
            "choices": parent.get("choices_presented", []),
            "state": self.current_state,
        }

    async def talk_to_npc(self, npc_id: str, message: str) -> dict:
        """Have a dedicated conversation with an NPC.

        Flow#3: 维护每个NPC的对话历史，提供上下文连续性。
        与主线整合：
        - 推进 5 分钟游戏时间（避免对话发生在时间真空中）
        - 维护对话计数 npc_dialogue_counts
        - 关系阈值跨越时检查并触发主线事件
        """
        await self._drain_background_tasks()  # P0-3
        # Validate NPC exists
        npc_state = self.current_state.get("npcs", {}).get(npc_id)
        if not npc_state:
            return {"error": f"NPC not found: {npc_id}"}

        npc_name = npc_state.get("name", npc_id) if isinstance(npc_state, dict) else npc_id

        # Check if NPC is at the same location as the player
        player_loc = self.current_state.get("player", {}).get("location", "")
        npc_location = self._get_npc_location(npc_id)
        if player_loc and npc_location:
            if not self._locations_match(player_loc, npc_location):
                # FLOW-4: Use AI as fallback to check if locations semantically match
                ai_match = await self._ai_check_same_location(player_loc, npc_location)
                if not ai_match:
                    return {"error": f"{npc_name}不在你当前的位置（{npc_location}）"}

        # 记录对话前关系快照（用于阈值跨越检测）
        rels_before = copy.deepcopy(
            self.current_state.get("player", {}).get("relationships", {}).get(npc_id, {})
        )

        # Build NPC-specific prompt
        system_prompt = self.prompt_builder.build_npc_talk_prompt(
            self.current_state, npc_id
        )
        if not system_prompt:
            return {"error": f"NPC not in script: {npc_id}"}

        # Flow#3: 获取对话历史，构建多轮消息
        npc_chat_history = self.current_state.setdefault("npc_chat_history", {})
        history = npc_chat_history.get(npc_id, [])

        # 构建包含历史的消息列表
        messages = []
        # 如果有压缩摘要，先注入为上下文
        if history and isinstance(history[0], dict) and history[0].get("_summary"):
            summary_text = history[0]["_summary"]
            messages.append({"role": "user", "content": f"[之前对话摘要: {summary_text}]"})
            messages.append({"role": "assistant", "content": "好的，我了解之前的对话内容。"})
        for h in history[-NPC_CHAT_HISTORY_DEPTH:]:
            if h.get("_summary"):
                continue
            messages.append({"role": "user", "content": h["player"]})
            messages.append({"role": "assistant", "content": h["npc"]})
        messages.append({"role": "user", "content": message})

        raw_response = await self.ai_provider.generate(
            messages, system=system_prompt, **self._stage_kwargs("narrative")
        )
        raw_response = strip_think_tags(raw_response)

        # Parse attitude changes from response
        npc_att_changes = self._parse_npc_talk_response(raw_response)

        # Strip the JSON block from the response for display
        clean_response = re.sub(
            r'```npc_talk\s*\{.*?\}\s*```', '', raw_response, flags=re.DOTALL
        ).strip()

        result = self._finalize_npc_talk(
            npc_id, npc_name, npc_state, message, clean_response,
            npc_att_changes, npc_chat_history, history, rels_before,
        )
        return result

    async def talk_to_npc_stream(self, npc_id: str, message: str):
        """Streaming version of talk_to_npc. Yields dicts with type='text'/'thinking'/'final'."""
        await self._drain_background_tasks()
        npc_state = self.current_state.get("npcs", {}).get(npc_id)
        if not npc_state:
            yield {"type": "final", "error": f"NPC not found: {npc_id}"}
            return

        npc_name = npc_state.get("name", npc_id) if isinstance(npc_state, dict) else npc_id

        player_loc = self.current_state.get("player", {}).get("location", "")
        npc_location = self._get_npc_location(npc_id)
        if player_loc and npc_location:
            if not self._locations_match(player_loc, npc_location):
                ai_match = await self._ai_check_same_location(player_loc, npc_location)
                if not ai_match:
                    yield {"type": "final", "error": f"{npc_name}不在你当前的位置（{npc_location}）"}
                    return

        rels_before = copy.deepcopy(
            self.current_state.get("player", {}).get("relationships", {}).get(npc_id, {})
        )

        system_prompt = self.prompt_builder.build_npc_talk_prompt(
            self.current_state, npc_id
        )
        if not system_prompt:
            yield {"type": "final", "error": f"NPC not in script: {npc_id}"}
            return

        npc_chat_history = self.current_state.setdefault("npc_chat_history", {})
        history = npc_chat_history.get(npc_id, [])

        messages = []
        if history and isinstance(history[0], dict) and history[0].get("_summary"):
            summary_text = history[0]["_summary"]
            messages.append({"role": "user", "content": f"[之前对话摘要: {summary_text}]"})
            messages.append({"role": "assistant", "content": "好的，我了解之前的对话内容。"})
        for h in history[-NPC_CHAT_HISTORY_DEPTH:]:
            if h.get("_summary"):
                continue
            messages.append({"role": "user", "content": h["player"]})
            messages.append({"role": "assistant", "content": h["npc"]})
        messages.append({"role": "user", "content": message})

        # Stream response, collecting full text (text only, not thinking)
        full_text = []
        async for kind, text in stream_split_think(
            self.ai_provider.generate_stream(messages, system=system_prompt, raw=True, **self._stage_kwargs("narrative"))
        ):
            if kind != "think":
                full_text.append(text)
            yield {"type": "thinking" if kind == "think" else "text", "content": text}

        raw_response = "".join(full_text)

        # Parse attitude changes and apply (same as non-streaming)
        npc_att_changes = self._parse_npc_talk_response(raw_response)

        clean_response = re.sub(
            r'```npc_talk\s*\{.*?\}\s*```', '', raw_response, flags=re.DOTALL
        ).strip()

        if clean_response != raw_response.strip():
            yield {"type": "narrative_revised", "content": clean_response}

        result = self._finalize_npc_talk(
            npc_id, npc_name, npc_state, message, clean_response,
            npc_att_changes, npc_chat_history, history, rels_before,
        )
        yield {"type": "final", **result}

    @staticmethod
    def _detect_relationship_crossings(before: dict, after: dict) -> list[dict]:
        """检测三维关系（trust/affection/fear）跨越关键阈值（30/60/80）。

        返回示例：[{"dim": "trust", "from": 28, "to": 32, "threshold": 30, "direction": "up"}]
        前端可用此触发UI提示或剧情节点。
        """
        if not isinstance(before, dict) or not isinstance(after, dict):
            return []
        thresholds = [30, 60, 80]
        out = []
        for dim in ("trust", "affection", "fear"):
            b = before.get(dim)
            a = after.get(dim)
            if not isinstance(b, (int, float)) or not isinstance(a, (int, float)):
                continue
            if a == b:
                continue
            for th in thresholds:
                if b < th <= a:
                    out.append({"dim": dim, "from": b, "to": a, "threshold": th, "direction": "up"})
                elif b >= th > a:
                    out.append({"dim": dim, "from": b, "to": a, "threshold": th, "direction": "down"})
        return out

    def _parse_npc_talk_response(self, response: str) -> list:
        """Extract npc_attitude_changes from NPC talk response."""
        match = re.search(r'```npc_talk\s*(\{.*?\})\s*```', response, re.DOTALL)
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
            return data.get("npc_attitude_changes", [])
        except (json.JSONDecodeError, KeyError):
            return []

    def _finalize_npc_talk(
        self, npc_id: str, npc_name: str, npc_state: dict,
        message: str, clean_response: str, npc_att_changes: list,
        npc_chat_history: dict, history: list, rels_before: dict,
    ) -> dict:
        """Shared post-processing for talk_to_npc / talk_to_npc_stream."""
        all_state_changes = []

        clean_response = self.regex_engine.apply(clean_response, "ai_output")

        # 对话技能检定：检测说服/威胁/欺骗/询问机密等意图
        talk_check = self._maybe_talk_skill_check(message)
        if talk_check and npc_att_changes:
            bonus = 2 if talk_check["outcome"] in ("success", "critical_success") else -2
            for ac in npc_att_changes:
                if isinstance(ac.get("change"), (int, float)):
                    ac["change"] = ac["change"] + bonus

        if npc_att_changes:
            att_as_state = self._npc_attitude_to_state_changes(npc_att_changes)
            MAX_ATTITUDE_DELTA = 20
            for sc in att_as_state:
                val = sc.get("value", 0)
                if sc.get("op") in ("add", "subtract") and isinstance(val, (int, float)):
                    if abs(val) > MAX_ATTITUDE_DELTA:
                        sc["value"] = MAX_ATTITUDE_DELTA if val > 0 else -MAX_ATTITUDE_DELTA
            self.current_state, att_log = self.state_manager.apply_changes(
                self.current_state, att_as_state, inplace=True
            )
            all_state_changes.extend(att_log)
            self._sync_npc_attitudes()

        history.append({"player": message, "npc": clean_response})
        if len(history) > NPC_CHAT_HISTORY_MAX:
            # 压缩最早的对话为摘要，保留近期完整对话
            overflow = history[:-NPC_CHAT_HISTORY_DEPTH]
            kept = history[-NPC_CHAT_HISTORY_DEPTH:]
            summary_parts = []
            for h in overflow[-5:]:
                player_brief = h.get("player", "")[:30]
                npc_brief = h.get("npc", "")[:50]
                summary_parts.append(f"玩家:{player_brief}→NPC:{npc_brief}")
            existing_summary = history[0].get("_summary", "") if history and history[0].get("_summary") else ""
            new_summary = existing_summary + "; ".join(summary_parts)
            if len(new_summary) > 500:
                new_summary = new_summary[-500:]
            history = [{"_summary": new_summary}] + kept
        npc_chat_history[npc_id] = history

        dlg_counts = self.current_state.setdefault("npc_dialogue_counts", {})
        dlg_counts[npc_id] = dlg_counts.get(npc_id, 0) + 1
        enc_counts = self.current_state.setdefault("npc_encounter_counts", {})
        enc_counts[npc_id] = enc_counts.get(npc_id, 0) + 1

        interaction_log = self.current_state.setdefault("npc_interaction_log", {})
        npc_ilog = interaction_log.setdefault(npc_id, [])
        npc_ilog.append({
            "turn": self.turn_number,
            "player": message[:80],
            "npc": clean_response[:80],
            "attitude_delta": sum(ac.get("change", 0) for ac in npc_att_changes) if npc_att_changes else 0,
        })
        if len(npc_ilog) > 15:
            interaction_log[npc_id] = npc_ilog[-10:]

        if isinstance(npc_state, dict):
            if not npc_state.get("known"):
                npc_state["known"] = True
            if not npc_state.get("met"):
                npc_state["met"] = True

        # 扫描对话内容触发 lorebook 词条
        _, new_lore_ts = self.prompt_builder.scan_lorebook(
            message, [clean_response],
            timed_state=self.current_state.get("lorebook_timed_state"),
            turn_number=self.turn_number,
        )
        self.current_state["lorebook_timed_state"] = new_lore_ts

        old_time = self.current_state.get("game_time", "")
        triggered_events = []
        if old_time:
            chat_minutes = min(NPC_CHAT_TIME_MINUTES + len(clean_response) // 100 * 2, 30)
            new_time = self._advance_game_time(old_time, timedelta(minutes=chat_minutes))
            try:
                triggered_events = self.event_scheduler.check_events(
                    self.current_state, old_time, new_time,
                    condition_eval=self._evaluate_condition,
                ) or []
            except Exception as e:
                logging.getLogger(__name__).warning("NPC对话事件检查失败: %s", e)
                triggered_events = []
            if self.event_engine:
                try:
                    ee_npc = self.event_engine.tick(
                        self.current_state, self.turn_number,
                        game_time=new_time, old_time=old_time,
                        condition_eval=self._evaluate_condition,
                        player_action=message,
                    )
                    self._apply_event_result(ee_npc)
                except Exception as e:
                    logging.getLogger(__name__).warning("NPC对话事件引擎tick失败: %s", e)
            self.current_state["game_time"] = new_time
            if triggered_events:
                self.current_state = self.event_scheduler.update_trackers(
                    self.current_state, triggered_events, new_time, inplace=True
                )
                for evt in triggered_events:
                    eid = evt.get("event_id", "") if isinstance(evt, dict) else ""
                    evt_def = self._event_def_by_id.get(eid)
                    if isinstance(evt_def, dict):
                        log = self._apply_event_def_effects(evt_def, eid)
                        all_state_changes.extend(log)
                event_lines = []
                for ev in triggered_events:
                    eid = ev.get("event_id", "") if isinstance(ev, dict) else str(ev)
                    desc = self._get_event_description(eid) if eid else ""
                    if desc:
                        event_lines.append(f"・{desc}")
                if event_lines:
                    clean_response = (
                        clean_response.rstrip()
                        + "\n\n[此期间发生]\n"
                        + "\n".join(event_lines)
                    )

        rels_after = self.current_state.get("player", {}).get("relationships", {}).get(npc_id, {})
        crossed = self._detect_relationship_crossings(rels_before, rels_after)

        # 优化6: NPC对话计入世界树节点，支持撤销和分支回溯
        node_id = self.world_tree.add_node(
            parent_id=self.world_tree.active_node_id,
            game_time=self.current_state.get("game_time", ""),
            turn_number=self.turn_number,
            player_action={"type": "npc_talk", "npc_id": npc_id, "text": message},
            ai_response=clean_response,
            choices_presented=[],
            dice_rolls=[],
            state_changes=all_state_changes,
            triggered_events=[
                e.get("event_id", e) if isinstance(e, dict) else e
                for e in triggered_events
            ],
            state_snapshot=self.current_state,
        )
        node = self.world_tree.get_node(node_id)
        if node and talk_check:
            node["talk_check"] = talk_check

        return {
            "node_id": node_id,
            "npc_id": npc_id,
            "npc_name": npc_name,
            "response": clean_response,
            "state_changes": all_state_changes,
            "triggered_events": [
                e.get("event_id", e) if isinstance(e, dict) else e
                for e in triggered_events
            ],
            "relationship_crossings": crossed,
            "dialogue_count": dlg_counts[npc_id],
            "talk_check": talk_check,
            "quick_replies": self._generate_npc_quick_replies(npc_id, npc_name, history),
            "state": self.current_state,
        }

    def _generate_npc_quick_replies(self, npc_id: str, npc_name: str, history: list) -> list[str]:
        """根据NPC态度和对话上下文生成快速回复建议。"""
        att = (self.current_state.get("npcs", {}).get(npc_id) or {}).get("attitude_toward_player", 50)
        turn_count = len(history)

        if att >= 70:
            replies = [f"向{npc_name}请求帮助", f"打听最近的消息", "继续聊天"]
        elif att >= 40:
            replies = [f"向{npc_name}打听消息", "友好地闲聊几句", "告辞离开"]
        else:
            replies = [f"尝试向{npc_name}解释", "保持沉默", "告辞离开"]

        # 根据最近NPC回复内容生成针对性选项
        if history:
            last_resp = ""
            for msg in reversed(history):
                if msg.get("npc"):
                    last_resp = msg["npc"]
                    break
            if last_resp and len(last_resp) > 10:
                contextual = self._extract_contextual_reply(last_resp, npc_name)
                if contextual:
                    replies[0] = contextual

        if turn_count >= 5:
            replies[-1] = "告辞离开"
        return replies

    @staticmethod
    def _extract_contextual_reply(npc_response: str, npc_name: str) -> str:
        """从NPC回复中提取可追问的话题。"""
        # 检测NPC提到的名词/话题（引号内容、书名号内容）
        quoted = re.findall(r'[「""]([^」""]{2,10})[」""]', npc_response)
        if quoted:
            return f"追问关于「{quoted[0]}」的事"
        book_quoted = re.findall(r'《([^》]{2,10})》', npc_response)
        if book_quoted:
            return f"追问关于《{book_quoted[0]}》的事"
        # 检测疑问句——NPC问了问题，玩家可以回应
        if "？" in npc_response[-80:] or "?" in npc_response[-80:]:
            return f"回应{npc_name}的问题"
        # 检测方位/地点提及
        loc_match = re.search(r'(?:去|到|在|前往)([^\s，。,]{2,8})', npc_response[-120:])
        if loc_match:
            return f"询问{loc_match.group(1)}的情况"
        return ""

    async def regenerate(self, stage: str = "all", hint: str = "") -> dict:
        """Regenerate the AI response for the current node (Swipe system).

        stage: "all" = full regenerate (narrative+choices+state)
               "choices" = keep narrative, regenerate choices only
               "state" = keep narrative+choices, regenerate state only
        hint: optional narrative direction hint injected into the prompt
        """
        await self._drain_background_tasks()  # P0-3
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return {"error": "No active node"}

        action = node.get("player_action") or {"type": "freeform", "text": ""}
        game_time = node.get("game_time", "")

        # B16: 排除当前节点（正在被重新生成），只取之前的历史
        all_recent = self.world_tree.get_recent_history(6)
        recent = [n for n in all_recent if n["id"] != self.world_tree.active_node_id][-5:]
        recent_messages = [n.get("ai_response", "") for n in recent]
        action_text = action.get("text", "") if isinstance(action, dict) else str(action)
        activated_lore, _ = self.prompt_builder.scan_lorebook(
            action_text, recent_messages,
            timed_state=self.current_state.get("lorebook_timed_state"),
            turn_number=self.turn_number,
        )

        dice_dicts = node.get("dice_rolls", [])
        triggered_events = node.get("triggered_events", [])
        event_dicts = [
            {"event_id": e, "description": self._get_event_description(e)}
            for e in triggered_events
        ]

        check_result = node.get("check_result")
        triggered_consequences = node.get("triggered_consequences")
        achieved_milestones = node.get("achieved_milestones")

        current_narrative = node.get("ai_response", "")
        current_choices = node.get("choices_presented", [])

        # 计算在场NPC和历史上下文（regenerate也需要）
        # 使用父节点快照的位置作为基准（regenerate 的状态基线是父节点）
        parent_id = node.get("parent_id")
        parent_snapshot = None
        if parent_id:
            parent_node = self.world_tree.get_node(parent_id)
            if parent_node:
                parent_snapshot = parent_node.get("state_snapshot")
                if not parent_snapshot:
                    parent_snapshot = await self._load_snapshot_from_db(parent_id)
                    if parent_snapshot:
                        parent_node["state_snapshot"] = parent_snapshot
        # regen_baseline: the state BEFORE this turn (what AI should see as "current")
        regen_baseline = parent_snapshot if parent_snapshot else self.current_state
        regen_present_npc_ids, _ = self._compute_present_npcs(regen_baseline)
        game_time = regen_baseline.get("game_time", "")

        # 构建历史上下文（regenerate也需要）
        history_summary = regen_baseline.get("history_summary", "")
        regen_history_context = self.prompt_builder.build_history_context(recent, history_summary)
        regen_context_memory = self.prompt_builder.build_context_memory(recent, regen_baseline)
        if regen_context_memory:
            regen_history_context = (regen_history_context + "\n\n" + regen_context_memory).strip() if regen_history_context else regen_context_memory

        regen_history_context, _ = await self._enrich_with_rag(
            action_text, recent, activated_lore, regen_history_context,
        )

        if stage == "all":
            # Build pipeline ctx: prefer persisted ctx from node, fallback to manual reconstruction
            saved_ctx = node.get("_pipeline_ctx")
            saved_route = node.get("_pipeline_route")

            if saved_ctx and saved_route:
                regen_ctx = dict(saved_ctx)
                regen_route = dict(saved_route)
                # Override fields that regenerate must refresh
                regen_ctx["recent_nodes"] = recent
                regen_ctx["history_context"] = regen_history_context
                regen_ctx["activated_lore"] = activated_lore
                regen_ctx["event_sections"] = PromptBuilder._format_events_for_prompt(
                    self.event_engine.get_events_for_prompt(regen_baseline)
                )
            else:
                # Fallback for historical nodes without _pipeline_ctx
                _regen_sd: dict[str, list[str]] = {}
                _regen_es = regen_baseline.get("events", {})
                for _eid, _evs in _regen_es.items():
                    if _evs.get("status") != "active":
                        continue
                    _ev = self.event_engine.events.get(_eid)
                    if _ev and hasattr(_ev, "metadata") and _ev.metadata.get("stage_directives"):
                        for _stg, _dir in _ev.metadata["stage_directives"].items():
                            _regen_sd.setdefault(_stg, []).append(_dir)
                if self.story_tree_engine:
                    _sts = regen_baseline.get("story_tree_state", {})
                    for _nid in _sts.get("active", []):
                        _sn = self.story_tree_engine._nodes.get(_nid)
                        if _sn and _sn.get("stage_directives"):
                            for _stg, _dir in _sn["stage_directives"].items():
                                _regen_sd.setdefault(_stg, []).append(_dir)

                regen_ctx = {
                    "check_result": check_result,
                    "dice_dicts": dice_dicts,
                    "triggered_events": event_dicts,
                    "triggered_consequences": triggered_consequences,
                    "achieved_milestones": achieved_milestones,
                    "present_npc_ids": regen_present_npc_ids,
                    "nearby_npc_ids": [],
                    "activated_lore": activated_lore,
                    "stage_directives": _regen_sd,
                    "event_sections": PromptBuilder._format_events_for_prompt(
                        self.event_engine.get_events_for_prompt(regen_baseline)
                    ),
                    "old_time": regen_baseline.get("game_time", ""),
                    "estimated_minutes": 30,
                    "recent_nodes": recent,
                    "prev_plot_decision": recent[-1].get("plot_decision", "") if recent else "",
                    "base_history_context": regen_history_context,
                    "history_context": regen_history_context,
                    "context_memory": regen_context_memory,
                }
                regen_route = {"scope": "moderate", "scene_type": "", "systems": None}

            # hint injection
            regen_action = dict(action)
            if hint:
                regen_action["text"] = action_text + f"\n[重生成倾向] 请让本次叙事偏向以下方向：{hint}"

            regen_plot_reasoning = ""
            async for item in self._execute_pipeline(
                regen_ctx, regen_route, regen_action,
                state_baseline=regen_baseline,
            ):
                if item["type"] == "pipeline_result":
                    raw_narrative = item["narrative"]
                    parsed = item["parsed"]
                    regen_plot_reasoning = item.get("plot_reasoning", "")
                    plot_decision = item.get("plot_decision", "")
                    break

        elif stage == "choices":
            choices_msgs, choices_sys = self.prompt_builder.build_choices_prompt(
                current_narrative, action_text, regen_baseline,
                turn_number=self.turn_number, activated_lore=activated_lore,
                event_sections=PromptBuilder._format_events_for_prompt(self.event_engine.get_events_for_prompt(regen_baseline)))
            raw_choices = await self.ai_provider.generate(
                choices_msgs, system=choices_sys, max_tokens=8192, **self._stage_kwargs("choices"))
            raw_choices = strip_think_tags(raw_choices)

            parsed = {"narrative": current_narrative, "choices": self.response_parser.parse_choices(raw_choices)}

        elif stage == "state":
            npc_msgs, npc_sys = self.prompt_builder.build_npc_reaction_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
                present_npc_ids=regen_present_npc_ids,
            )
            res_msgs, res_sys = self.prompt_builder.build_world_state_resource_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
            )
            spa_msgs, spa_sys = self.prompt_builder.build_world_state_spatial_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
            )
            tmp_msgs, tmp_sys = self.prompt_builder.build_world_state_temporal_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
            )
            wld_msgs, wld_sys = self.prompt_builder.build_world_state_world_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
            )
            ext_msgs, ext_sys = self.prompt_builder.build_world_state_ext_prompt(
                current_narrative, action_text, regen_baseline,
                check_result=check_result,
                event_sections=PromptBuilder._format_events_for_prompt(self.event_engine.get_events_for_prompt(regen_baseline)),
            )
            _regen_raw = await asyncio.gather(
                self.ai_provider.generate(npc_msgs, system=npc_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(res_msgs, system=res_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(spa_msgs, system=spa_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(tmp_msgs, system=tmp_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(wld_msgs, system=wld_sys, max_tokens=4096, **self._stage_kwargs("state")),
                self.ai_provider.generate(ext_msgs, system=ext_sys, max_tokens=4096, **self._stage_kwargs("state")),
                return_exceptions=True,
            )
            [raw_npc, raw_resource, raw_spatial, raw_temporal, raw_world, raw_world_ext], _ = self._sanitize_gather_results(
                _regen_raw, [("NPC关系推演", False), ("资源状态推演", False),
                             ("空间状态推演", False), ("时间状态推演", False),
                             ("世界属性推演", False), ("扩展状态推演", False)],
            )

            parsed = self.response_parser.parse_split_v3(current_narrative, raw_npc, raw_resource, raw_spatial, raw_temporal, raw_world, raw_world_ext)
            parsed["choices"] = current_choices

        else:
            return {"error": f"Unknown stage: {stage}"}

        rules = self.script.get("post_processing_rules", [])
        if rules and stage == "all":
            parsed["narrative"] = self._apply_post_processing(
                parsed.get("narrative", ""), rules
            )

        # P0-2: 以父节点（本回合开始前）的状态为基线（复用已加载的 regen_baseline）
        if stage in ("all", "state"):
            baseline_state = copy.deepcopy(regen_baseline)
            for key in self._SWIPE_STRIP_KEYS:
                if key in self.current_state and key not in baseline_state:
                    baseline_state[key] = copy.deepcopy(self.current_state[key])

            swipe_state = baseline_state
            swipe_state, swipe_state_changes = self._apply_common_parsed_changes(
                swipe_state, parsed, inplace=True,
                present_npc_ids=regen_present_npc_ids,
            )

            base_time = swipe_state.get("game_time", game_time)
            ai_end_time = parsed.get("end_time")
            ai_time_advance = parsed.get("time_advance") if not ai_end_time else None
            if ai_end_time:
                try:
                    _et = datetime.fromisoformat(ai_end_time.replace("Z", "+00:00"))
                    _bt = datetime.fromisoformat(base_time.replace("Z", "+00:00"))
                    if _et > _bt:
                        _delta_min = (_et - _bt).total_seconds() / 60
                        if _delta_min > 2880:
                            swipe_state["game_time"] = self._advance_game_time(base_time, timedelta(hours=48))
                        else:
                            swipe_state["game_time"] = ai_end_time
                except (ValueError, TypeError):
                    pass
            elif ai_time_advance:
                ai_minutes = self._parse_duration_minutes(ai_time_advance)
                if ai_minutes is not None and ai_minutes < 5:
                    swipe_state["game_time"] = self._advance_game_time(base_time, timedelta(minutes=5))
                elif ai_minutes is not None and ai_minutes > 2880:
                    swipe_state["game_time"] = self._advance_game_time(base_time, timedelta(hours=48))
                else:
                    swipe_state["game_time"] = self._apply_iso_duration(base_time, ai_time_advance)
            if parsed.get("location_change"):
                travel_time = self._get_location_travel_time(parsed["location_change"])
                if travel_time:
                    swipe_state["game_time"] = self._apply_iso_duration(
                        swipe_state.get("game_time", game_time), travel_time
                    )
        else:
            swipe_state = copy.deepcopy(self.current_state)
            swipe_state_changes = []

        # 检查 narrative 解析是否有效，避免静默复刻旧叙事产生"假成功"swipe
        new_narrative = parsed.get("narrative", "").strip()
        if stage == "all" and not new_narrative:
            return {"error": "叙事生成为空，重生成失败", "stage": stage}

        # scene_details 写入 state 和 lorebook（与主回合一致）
        if stage in ("all", "state"):
            scene_details = parsed.get("scene_details")
            if scene_details and isinstance(scene_details, dict):
                swipe_state["scene_details"] = scene_details
                loc_id = swipe_state.get("player", {}).get("location", "")
                if loc_id and self.prompt_builder.lorebook:
                    self.prompt_builder.update_location_scene(loc_id, scene_details)

        self._enrich_choice_previews(parsed.get("choices", []))
        swipe_data = {
            "narrative": new_narrative if stage == "all" else parsed.get("narrative", current_narrative),
            "choices": parsed.get("choices", []),
            "state_changes": swipe_state_changes,
            "state_snapshot": self._slim_snapshot(swipe_state),
        }

        swipes = node.setdefault("swipes", [])
        if not swipes:
            orig_snapshot = node.get("state_snapshot") or copy.deepcopy(self.current_state)
            swipes.append({
                "narrative": node.get("ai_response", ""),
                "choices": node.get("choices_presented", []),
                "state_changes": node.get("state_changes", []),
                "state_snapshot": self._slim_snapshot(orig_snapshot),
            })
            node["_shared_strip_keys"] = {
                k: copy.deepcopy(orig_snapshot[k])
                for k in self._SWIPE_STRIP_KEYS if k in orig_snapshot
            }
        swipes.append(swipe_data)
        node["active_swipe_index"] = len(swipes) - 1

        node["ai_response"] = swipe_data["narrative"]
        node["choices_presented"] = swipe_data["choices"]
        node["state_snapshot"] = swipe_state
        if stage == "all" and regen_plot_reasoning:
            node["plot_reasoning"] = regen_plot_reasoning
            node["plot_decision"] = plot_decision
        self.current_state = swipe_state

        # Regenerate scene image if provider is available and stage includes narrative
        scene_image = None
        if stage == "all" and getattr(self, "_image_provider", None) and new_narrative:
            try:
                from ai.image_prompt_builder import build_image_prompt
                loc = swipe_state.get("player", {}).get("location", "")
                loc_data = self._location_by_id.get(loc, {})
                loc_name = loc_data.get("name", loc) if loc_data else loc
                loc_desc = loc_data.get("description", "") if loc_data else ""
                tod = swipe_state.get("time_of_day", "day")
                weather = swipe_state.get("current_weather", "")

                characters = []
                pc = self.script.get("player_character", {})
                pc_app = pc.get("appearance") or pc.get("bio") or ""
                if pc_app:
                    characters.append(f"Player: {pc_app[:200]}")
                npc_states_regen = swipe_state.get("npcs", {})
                for nid, ns in list(npc_states_regen.items())[:5]:
                    if isinstance(ns, dict) and ns.get("current_location") == loc:
                        npc_def = self._npc_by_id.get(nid, {})
                        desc = npc_def.get("appearance") or npc_def.get("bio") or ""
                        name = ns.get("name") or npc_def.get("name", nid)
                        if desc:
                            characters.append(f"{name}: {desc[:150]}")

                image_style = ""
                try:
                    from api.config_routes import get_image_style
                    style_data = await get_image_style()
                    image_style = style_data.get("custom") or style_data.get("preset") or ""
                except Exception:
                    pass

                img_prompt = await build_image_prompt(
                    new_narrative, loc_name, "atmospheric", tod, self.ai_provider,
                    weather=weather, characters=characters,
                    location_desc=loc_desc, image_style=image_style,
                )
                scene_image = await self._image_provider.generate_image(img_prompt)
                scene_image["_prompt"] = img_prompt
            except Exception as e:
                logger.warning("Regenerate scene image failed: %s", e)

        return {
            "node_id": self.world_tree.active_node_id,
            "narrative": swipe_data["narrative"],
            "choices": swipe_data["choices"],
            "state": self.current_state,
            "state_changes": swipe_state_changes,
            "swipe_index": node["active_swipe_index"],
            "total_swipes": len(swipes),
            "stage": stage,
            "scene_image": scene_image,
        }

    async def attempt_deduction(self, clue_ids: list[str]) -> dict:
        """Player attempts to link clues and form a deduction."""
        clue_board = self.current_state.get("clue_board", [])
        selected = [c for c in clue_board if c["id"] in clue_ids]
        if len(selected) < 2:
            return {"success": False, "message": "至少需要两条线索才能推理"}

        # Deterministic cache key: sorted clue IDs
        cache_key = "|".join(sorted(clue_ids))
        deduction_cache = self.current_state.setdefault("deduction_cache", {})
        if cache_key in deduction_cache:
            return deduction_cache[cache_key]

        clue_texts = "\n".join(f"- [{c['category']}] {c['text']}（来源：{c['source']}）" for c in selected)

        # Check script-defined deduction templates first
        templates = self.script.get("deduction_templates", [])
        for tpl in templates:
            req_ids = set(tpl.get("required_clues", []))
            if req_ids and req_ids.issubset(set(clue_ids)):
                completed = self.current_state.setdefault("completed_deductions", [])
                if tpl["id"] not in completed:
                    completed.append(tpl["id"])
                    for effect in tpl.get("effects", []):
                        if effect.get("type") == "reveal_location":
                            vis = self.current_state.setdefault("visible_locations", [])
                            if effect["id"] not in vis:
                                vis.append(effect["id"])
                        elif effect.get("type") == "unlock_secret":
                            us = self.current_state.setdefault("npc_unlocked_secrets", {})
                            nid = effect.get("npc_id", "")
                            sid = effect.get("secret_id", "")
                            if nid and sid:
                                us.setdefault(nid, [])
                                if sid not in us[nid]:
                                    us[nid].append(sid)
                        elif effect.get("type") == "milestone":
                            ms = self.current_state.setdefault("achieved_milestones", [])
                            if effect["id"] not in ms:
                                ms.append(effect["id"])
                    for cid in clue_ids:
                        for c in clue_board:
                            if c["id"] == cid:
                                c.setdefault("linked_to", []).extend(
                                    [x for x in clue_ids if x != cid and x not in c.get("linked_to", [])]
                                )
                    result = {
                        "success": True,
                        "message": tpl.get("conclusion", "你的推理揭示了真相。"),
                        "effects": tpl.get("effects", []),
                    }
                    deduction_cache[cache_key] = result
                    return result

        # AI-based open-ended deduction
        prompt = (
            "玩家尝试将以下线索关联推理：\n" + clue_texts + "\n\n"
            "判断这些线索之间是否存在合理关联。如果存在，给出推理结论（1-2句话）。\n"
            "返回JSON: {\"valid\": true/false, \"conclusion\": \"推论内容\", "
            "\"insight\": \"玩家由此获得的新认知（可选，用于推进剧情）\"}"
        )
        try:
            result = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是推理验证引擎。严格基于线索内容判断关联性，不编造线索中不存在的信息。返回紧凑JSON，不要markdown包裹。",
                max_tokens=1024,
            )
            clean = result.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            parsed = json.loads(clean)
            if parsed.get("valid"):
                for cid in clue_ids:
                    for c in clue_board:
                        if c["id"] == cid:
                            c.setdefault("linked_to", []).extend(
                                [x for x in clue_ids if x != cid and x not in c.get("linked_to", [])]
                            )
                if parsed.get("insight"):
                    self._record_narrative_callback(
                        parsed["insight"], ["deduction"], priority="high",
                    )
                result = {"success": True, "message": parsed.get("conclusion", "推理成立。")}
                deduction_cache[cache_key] = result
                return result
            result = {"success": False, "message": parsed.get("conclusion", "这些线索之间似乎没有直接关联。")}
            deduction_cache[cache_key] = result
            return result
        except Exception as e:
            logger.error("attempt_deduction AI调用失败: %s", e)
            return {"success": False, "message": "推理失败，请尝试不同的线索组合。"}

    async def continue_narrative(self) -> dict:
        """Extend the current narrative without creating a new turn."""
        await self._drain_background_tasks()
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return {"error": "No active node"}

        current_narrative = node.get("ai_response", "")
        if not current_narrative:
            return {"error": "No narrative to continue"}

        msgs, sys_prompt = self.prompt_builder.build_continue_prompt(
            current_narrative, self.current_state,
        )
        raw = await self.ai_provider.generate(
            msgs, system=sys_prompt, max_tokens=8192, **self._stage_kwargs("narrative")
        )
        continuation = strip_think_tags(raw).strip() if raw else ""
        if not continuation:
            return {"error": "续写生成为空"}

        continuation = self.regex_engine.apply(continuation, "ai_output")
        extended = current_narrative.rstrip() + "\n\n" + continuation
        node["ai_response"] = extended

        swipes = node.get("swipes")
        if swipes:
            idx = node.get("active_swipe_index", 0)
            if 0 <= idx < len(swipes):
                swipes[idx]["narrative"] = extended

        return {
            "node_id": self.world_tree.active_node_id,
            "narrative": extended,
            "continuation": continuation,
            "choices": node.get("choices_presented", []),
            "state": self.current_state,
        }

    def swipe_to(self, direction: str) -> dict | None:
        """Switch to a different swipe on the current node.
        direction: 'left' or 'right'
        """
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return None
        swipes = node.get("swipes", [])
        if len(swipes) <= 1:
            return None

        idx = node.get("active_swipe_index", 0)
        if direction == "left":
            idx = max(0, idx - 1)
        else:
            idx = min(len(swipes) - 1, idx + 1)

        node["active_swipe_index"] = idx
        swipe = swipes[idx]
        node["ai_response"] = swipe["narrative"]
        node["choices_presented"] = swipe.get("choices", [])

        # B5: 单次 deepcopy，赋给 node 和 current_state
        # P0-1: slim snapshot 不含 _SWIPE_STRIP_KEYS（adventure_log/history_summary 等），
        # 从节点的 _shared_strip_keys 恢复，避免从已被其他 swipe 修改的 current_state 取值。
        if swipe.get("state_snapshot") is not None:
            restored = copy.deepcopy(swipe["state_snapshot"])
            shared = node.get("_shared_strip_keys", {})
            for key in self._SWIPE_STRIP_KEYS:
                if key not in restored:
                    src = shared.get(key) or self.current_state.get(key)
                    if src is not None:
                        restored[key] = copy.deepcopy(src)
            node["state_snapshot"] = restored
            self.current_state = restored

        return {
            "node_id": self.world_tree.active_node_id,
            "narrative": swipe["narrative"],
            "choices": swipe.get("choices", []),
            "state": self.current_state,
            "state_changes": swipe.get("state_changes", []),
            "swipe_index": idx,
            "total_swipes": len(swipes),
        }

    def get_all_swipes(self) -> list[dict]:
        """Return summary of all swipes on the active node."""
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return []
        swipes = node.get("swipes", [])
        if not swipes:
            return [{
                "index": 0,
                "narrative_preview": (node.get("ai_response", "") or "")[:300],
                "choices": node.get("choices_presented", []),
                "state_changes": node.get("state_changes", []),
                "active": True,
            }]
        active_idx = node.get("active_swipe_index", 0)
        result = []
        for i, s in enumerate(swipes):
            result.append({
                "index": i,
                "narrative_preview": (s.get("narrative", "") or "")[:300],
                "choices": s.get("choices", []),
                "state_changes": s.get("state_changes", []),
                "active": i == active_idx,
            })
        return result

    def swipe_to_index(self, index: int) -> dict | None:
        """Jump directly to a specific swipe index."""
        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not node:
            return None
        swipes = node.get("swipes", [])
        if index < 0 or index >= len(swipes):
            return None

        node["active_swipe_index"] = index
        swipe = swipes[index]
        node["ai_response"] = swipe["narrative"]
        node["choices_presented"] = swipe.get("choices", [])

        if swipe.get("state_snapshot") is not None:
            restored = copy.deepcopy(swipe["state_snapshot"])
            shared = node.get("_shared_strip_keys", {})
            for key in self._SWIPE_STRIP_KEYS:
                if key not in restored:
                    src = shared.get(key) or self.current_state.get(key)
                    if src is not None:
                        restored[key] = copy.deepcopy(src)
            node["state_snapshot"] = restored
            self.current_state = restored

        return {
            "node_id": self.world_tree.active_node_id,
            "narrative": swipe["narrative"],
            "choices": swipe.get("choices", []),
            "state": self.current_state,
            "state_changes": swipe.get("state_changes", []),
            "swipe_index": index,
            "total_swipes": len(swipes),
        }

    @staticmethod
    def _apply_post_processing(text: str, rules: list[dict]) -> str:
        """Apply regex post-processing rules to AI output."""
        for rule in rules:
            if not rule.get("enabled", True):
                continue
            try:
                text = re.sub(rule["find"], rule["replace"], text)
            except re.error:
                pass
        return text

    async def _maybe_summarize_history(self):
        """Check if history needs summarization and do it if so."""
        if self.current_state.get("summary_frozen", False):
            return

        # Calculate word count since last summarization for word-aware trigger
        branch = self.world_tree.get_active_branch()
        last_st = self.current_state.get("last_summarized_turn", 0)
        recent_word_count = sum(
            len(n.get("ai_response", "")) + len(
                n.get("player_action", {}).get("text", "")
                if isinstance(n.get("player_action"), dict) else ""
            )
            for n in branch if n.get("turn_number", 0) > last_st
        )

        if not self.history_summarizer.needs_summary(
            self.turn_number, self.current_state, recent_word_count
        ):
            return

        # 连续失败超过 3 次，跳过本批并推进 last_summarized_turn
        if self._summary_fail_count >= 3:
            logging.getLogger(__name__).warning(
                "摘要连续失败%d次，跳过本批次 turn=%d", self._summary_fail_count, self.turn_number
            )
            self.current_state["last_summarized_turn"] = self.turn_number
            self._summary_fail_count = 0
            return

        # Only fetch the nodes we actually need: older ones beyond keep_recent
        keep = self.history_summarizer.keep_recent
        branch = self.world_tree.get_active_branch()
        if len(branch) <= keep:
            return

        older_nodes = branch[:-keep] if keep > 0 else branch
        # Only summarize nodes since last summarization to avoid re-processing
        last_summarized = self.current_state.get("last_summarized_turn", 0)
        older_nodes = [n for n in older_nodes if n.get("turn_number", 0) > last_summarized]
        if not older_nodes:
            return
        existing_summary = self.current_state.get("history_summary", "")

        system_prompt, user_prompt = self.history_summarizer.build_summary_prompt(
            older_nodes, existing_summary
        )

        try:
            summary = await self.ai_provider.generate(
                [{"role": "user", "content": user_prompt}],
                system=system_prompt, **self._stage_kwargs("summary"),
            )
            async with self._state_lock:
                self.current_state = self.history_summarizer.update_state_with_summary(
                    self.current_state, summary.strip(), self.turn_number
                )
            self._summary_fail_count = 0
            self._sync_key_events_to_lorebook()
        except Exception:
            self._summary_fail_count += 1
            logging.getLogger(__name__).warning(
                "历史摘要生成失败（连续第%d次）", self._summary_fail_count
            )

    async def _maybe_analyze_play_style(self):
        """Periodically analyze player actions and generate a play style summary."""
        interval = PLAY_STYLE_INTERVAL
        if self.turn_number < interval:
            return
        last_analyzed = self.current_state.get("play_style_last_turn", 0)
        if (self.turn_number - last_analyzed) < interval:
            return

        # Gather recent player actions
        branch = self.world_tree.get_active_branch()
        recent = branch[-interval:] if len(branch) >= interval else branch
        actions_text = []
        for node in recent:
            action = node.get("player_action")
            if action:
                text = action.get("text", "") if isinstance(action, dict) else str(action)
                if text:
                    actions_text.append(f"第{node.get('turn_number', '?')}回合: {text}")

        if not actions_text:
            return

        system = (
            "你是一个角色扮演风格分析助手。根据玩家最近的行动，用一个简短标签（2-4字）和一句话描述（20字以内）概括玩家的角色扮演风格。\n"
            "风格标签示例：谨慎型、冒险型、外交型、战斗型、探索型、社交型、阴谋型、善良型、混乱型等。\n"
            "仅输出标签和描述，格式：标签|描述\n"
            "例：谨慎型|总是先观察再行动，避免直接冲突"
        )
        user = "玩家最近的行动：\n" + "\n".join(actions_text)

        try:
            result = await self.ai_provider.generate(
                [{"role": "user", "content": user}],
                system=system, **self._stage_kwargs("summary"),
            )
            result = result.strip()
            # Strip thinking tags if present
            result = strip_think_tags(result)
            async with self._state_lock:
                if "|" in result:
                    tag, desc = result.split("|", 1)
                    self.current_state["play_style_summary"] = {
                        "tag": tag.strip(),
                        "description": desc.strip(),
                        "turn": self.turn_number,
                    }
                else:
                    self.current_state["play_style_summary"] = {
                        "tag": result[:10].strip(),
                        "description": "",
                        "turn": self.turn_number,
                    }
                self.current_state["play_style_last_turn"] = self.turn_number
        except Exception:
            pass  # Non-critical

    def _execute_tool_calls(self, text: str) -> tuple[str, list[dict]]:
        """Parse and execute [TOOL_CALL: ...] patterns in AI output.

        Returns (cleaned_text, tool_results).
        """
        results = []
        for m in _TOOL_CALL_RE.finditer(text):
            name = m.group(1)
            args = [a.strip() for a in m.group(2).split(",") if a.strip()]
            result = self._run_tool(name, args)
            if result is not None:
                results.append({"tool": name, "args": args, "result": result})
        cleaned = _TOOL_CALL_RE.sub("", text).strip()
        return cleaned, results

    def _run_tool(self, name: str, args: list[str]) -> str | None:
        if name == "roll_dice" and args:
            skill = args[0]
            dc = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
            roll = random.randint(1, 20)
            bonus = self._get_skill_bonus(skill)
            total = roll + bonus
            success = total >= dc
            return f"d20={roll}, 加值={bonus}, 总计={total}, DC={dc}, {'成功' if success else '失败'}"
        if name == "check_inventory" and args:
            item = args[0]
            inv = self.current_state.get("inventory", [])
            found = any(item in (i.get("item", i.get("name", "")) if isinstance(i, dict) else str(i)) for i in inv)
            return f"{'持有' if found else '未持有'}{item}"
        if name == "get_npc_attitude" and args:
            npc_id = args[0]
            npc_data = self.current_state.get("npcs", {}).get(npc_id, {})
            att = npc_data.get("attitude_toward_player", "未知") if isinstance(npc_data, dict) else "未知"
            return f"{npc_id}的态度: {att}"
        if name == "get_time":
            return self.current_state.get("game_time", "未知")
        return None

    def _run_tool_native(self, name: str, args: dict) -> str:
        """Execute a tool call from native API tool_calls (dict arguments)."""
        if name == "roll_dice":
            skill = args.get("skill", "通用")
            dc = args.get("dc", 10)
            roll = random.randint(1, 20)
            bonus = self._get_skill_bonus(skill)
            total = roll + bonus
            success = total >= dc
            return f"d20={roll}, 加值={bonus}, 总计={total}, DC={dc}, {'成功' if success else '失败'}"
        if name == "check_inventory":
            item = args.get("item_name", "")
            inv = self.current_state.get("inventory", [])
            found = any(item in (i.get("item", i.get("name", "")) if isinstance(i, dict) else str(i)) for i in inv)
            return f"{'持有' if found else '未持有'}{item}"
        if name == "get_npc_attitude":
            npc_id = args.get("npc_id", "")
            npc_data = self.current_state.get("npcs", {}).get(npc_id, {})
            att = npc_data.get("attitude_toward_player", "未知") if isinstance(npc_data, dict) else "未知"
            return f"{npc_id}的态度: {att}"
        if name == "get_time":
            return self.current_state.get("game_time", "未知")
        if name == "modify_stat":
            stat = args.get("stat", "")
            delta = args.get("delta", 0)
            player = self.current_state.get("player", {})
            attrs = player.get("attributes", {})
            if stat in attrs and isinstance(attrs[stat], dict):
                old = attrs[stat].get("value", 0)
                attrs[stat]["value"] = old + delta
                return f"{stat}: {old} → {old + delta}"
            old = player.get(stat, 0)
            if isinstance(old, (int, float)):
                player[stat] = old + delta
                return f"{stat}: {old} → {old + delta}"
            return f"未找到属性: {stat}"
        if name == "query_lore":
            keyword = args.get("keyword", "")
            kw_lower = keyword.lower()
            matches = [
                e for e in self.prompt_builder.lorebook.entries
                if kw_lower in (e.comment or "").lower()
                or kw_lower in e.content[:200].lower()
                or any(kw_lower in k.lower() for k in e.keys)
            ][:3]
            if matches:
                return "\n".join(f"- {e.comment or e.id}: {e.content[:200]}" for e in matches)
            return f"未找到与'{keyword}'相关的知识条目"
        if name == "change_location":
            loc_id = args.get("location_id", "")
            if loc_id in self._location_by_id:
                loc = self._location_by_id[loc_id]
                self.current_state.setdefault("player", {})["location"] = loc_id
                return f"已移动到: {loc.get('name', loc_id)}"
            return f"未知地点: {loc_id}"
        if name == "set_variable":
            var_name = args.get("name", "")
            var_value = args.get("value", "")
            if hasattr(self, 'script_variables'):
                self.script_variables.set(var_name, var_value)
                return f"变量 {var_name} = {var_value}"
            return f"变量系统不可用"
        # --- P1 上下文 Pull 工具 ---
        if name == "recall_history":
            query = args.get("query", "")
            max_results = args.get("max_results", 5)
            results = []
            # 优先使用向量记忆检索
            if self.vector_memory is not None:
                try:
                    hits = self.vector_memory.search(query, top_k=max_results)
                    for h in hits:
                        results.append({
                            "turn": h.get("turn", "?"),
                            "summary": h.get("text", "")[:200],
                            "relevance": round(h.get("score", 0), 2),
                        })
                except Exception:
                    pass
            # 回退到 world_tree 节点文本搜索
            if not results and self.world_tree is not None:
                query_lower = query.lower()
                recent_nodes = self.world_tree.get_recent_history(20)
                for node in reversed(recent_nodes):
                    node_text = node.get("ai_response", "") + " " + (
                        node.get("player_action", {}).get("text", "")
                        if isinstance(node.get("player_action"), dict)
                        else str(node.get("player_action", ""))
                    )
                    if query_lower in node_text.lower():
                        results.append({
                            "turn": node.get("turn_number", "?"),
                            "summary": node.get("ai_response", "")[:200],
                            "relevance": 0.5,
                        })
                    if len(results) >= max_results:
                        break
            return json.dumps({"results": results}, ensure_ascii=False)
        if name == "query_lorebook":
            keyword = args.get("keyword", "")
            kw_lower = keyword.lower()
            entries = []
            lb = getattr(self.prompt_builder, "lorebook", None)
            if lb is not None:
                for e in lb.entries:
                    if not e.enabled:
                        continue
                    if (kw_lower in (e.comment or "").lower()
                            or kw_lower in e.content[:500].lower()
                            or any(kw_lower in k.lower() for k in e.keys)
                            or any(kw_lower in k.lower() for k in getattr(e, "secondary_keys", []))):
                        entries.append({
                            "title": e.comment or e.id,
                            "content": e.content[:500],
                        })
                    if len(entries) >= 5:
                        break
            return json.dumps({"entries": entries}, ensure_ascii=False)
        if name == "query_npc_history":
            npc_name = args.get("npc_name", "")
            max_turns = args.get("max_turns", 5)
            interactions = []
            if self.world_tree is not None and npc_name:
                npc_lower = npc_name.lower()
                recent_nodes = self.world_tree.get_recent_history(30)
                for node in reversed(recent_nodes):
                    narrative = node.get("ai_response", "")
                    action_raw = node.get("player_action")
                    action_text = (
                        action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")
                    )
                    combined = (narrative + " " + action_text).lower()
                    if npc_lower in combined:
                        interactions.append({
                            "turn": node.get("turn_number", "?"),
                            "summary": narrative[:200],
                        })
                    if len(interactions) >= max_turns:
                        break
            return json.dumps({"npc": npc_name, "interactions": interactions}, ensure_ascii=False)
        return f"未知工具: {name}"

    # ── Stage 4 状态工具执行器 & 合并器 ──

    _REJECT_STATE_PATTERNS_TOOL = (
        "weather", "天气", "时段", "time_period", "dawn", "dusk",
        "morning", "afternoon", "night", "noon", "黎明", "黄昏",
        "上午", "午后", "夜晚", "深夜", "日出", "日落",
    )
    _REJECT_WP_PATTERNS_TOOL = (
        "lamp", "light", "door", "window", "clock", "alarm",
        "desk", "chair", "phone", "radio", "tv", "灯", "门",
        "窗", "桌", "椅", "电话", "闹钟", "台灯", "scattered",
        "curtain", "drawer", "paper", "document", "书桌", "抽屉",
    )

    def _run_state_tool(self, tool_name: str, args: dict) -> str:
        """Execute a state-settlement tool call. Returns validation result as string.

        Does NOT modify state directly — caller merges via _merge_state_tool_results.
        """
        MAX_DELTA = 30

        if tool_name == "update_resources":
            issues = []
            # Validate state_changes
            for sc in args.get("state_changes", []):
                op = sc.get("op", "add")
                val = sc.get("value", 0)
                if op in ("add", "subtract") and not sc.get("reason"):
                    issues.append(f"丢弃无reason属性变更: {sc.get('target', '?')}")
                    continue
                if isinstance(val, (int, float)) and op in ("add", "subtract") and abs(val) > MAX_DELTA:
                    issues.append(f"属性变化幅度过大 {sc.get('target', '?')}: {val}, 已钳制到±{MAX_DELTA}")
            # Validate activate_states
            for entry in args.get("activate_states", []):
                if isinstance(entry, dict):
                    sid = entry.get("id", "")
                    s_name = entry.get("name", "")
                    _low = (sid + s_name).lower()
                    if any(p in _low for p in self._REJECT_STATE_PATTERNS_TOOL):
                        issues.append(f"过滤天气/时段状态: {sid}")
            result = "资源状态已接收"
            if issues:
                result += "（警告: " + "; ".join(issues) + "）"
            return result

        if tool_name == "update_spatial":
            issues = []
            loc = args.get("location_change")
            if loc and loc not in self._location_by_id:
                issues.append(f"未知地点ID: {loc}")
            for rl in args.get("reveal_locations", []):
                if not rl.get("id"):
                    issues.append("reveal_locations 缺少 id")
            result = "空间状态已接收"
            if issues:
                result += "（警告: " + "; ".join(issues) + "）"
            return result

        if tool_name == "update_time":
            et = args.get("end_time", "")
            if not et:
                return "错误: end_time 为空"
            # Basic ISO format check
            if "T" not in et and len(et) < 10:
                return f"警告: end_time 格式可能不正确: {et}"
            return f"时间状态已接收: {et}"

        if tool_name == "update_world":
            issues = []
            for wp in args.get("world_property_changes", []):
                wp_id = wp.get("id", "")
                if wp_id and any(p in wp_id.lower() for p in self._REJECT_WP_PATTERNS_TOOL):
                    issues.append(f"过滤物件级属性: {wp_id}")
            result = "世界属性已接收"
            if issues:
                result += "（警告: " + "; ".join(issues) + "）"
            return result

        if tool_name == "update_extended":
            issues = []
            # NPC name→ID matching for offscreen_npc_updates
            for upd in args.get("offscreen_npc_updates", []):
                name = upd.get("name", "")
                if name and name not in self._npc_name_to_id and not any(
                    n.get("name") == name for n in self._npc_by_id.values() if isinstance(n, dict)
                ):
                    issues.append(f"离场NPC名称未匹配到已知NPC: {name}")
            result = "扩展状态已接收"
            if issues:
                result += "（警告: " + "; ".join(issues) + "）"
            return result

        return f"未知状态工具: {tool_name}"

    def _merge_state_tool_results(self, tool_calls: list[dict]) -> dict:
        """Merge state tool call arguments into a parsed dict compatible with parse_split_v3 output.

        tool_calls: [{"name": "update_resources", "args": {...}}, ...]
        Returns a dict matching _empty_result() structure.
        """
        parsed = self.response_parser._empty_result()
        MAX_DELTA = 30

        for tc in tool_calls:
            name = tc["name"]
            args = tc["args"]

            if name == "update_resources":
                # state_changes: validate and clamp
                validated = []
                for sc in args.get("state_changes", []):
                    op = sc.get("op", "add")
                    val = sc.get("value", 0)
                    if op in ("add", "subtract") and not sc.get("reason"):
                        continue
                    if isinstance(val, (int, float)) and op in ("add", "subtract") and abs(val) > MAX_DELTA:
                        sc = {**sc, "value": MAX_DELTA if val > 0 else -MAX_DELTA}
                    validated.append(sc)
                parsed["state_changes"].extend(validated)
                parsed["inventory_changes"].extend(args.get("inventory_changes", []))
                # activate_states: filter weather/time-period
                for entry in args.get("activate_states", []):
                    if isinstance(entry, dict):
                        sid = entry.get("id", "")
                        s_name = entry.get("name", "")
                        _low = (sid + s_name).lower()
                        if any(p in _low for p in self._REJECT_STATE_PATTERNS_TOOL):
                            continue
                    parsed["activate_states"].append(entry)
                parsed["deactivate_states"].extend(args.get("deactivate_states", []))
                go = args.get("game_over")
                if go:
                    parsed["game_over"] = go

            elif name == "update_spatial":
                loc = args.get("location_change")
                if loc and loc in self._location_by_id:
                    parsed["location_change"] = loc
                parsed["reveal_locations"].extend(args.get("reveal_locations", []))
                parsed["npc_location_changes"].extend(args.get("npc_location_changes", []))
                if args.get("room_changes"):
                    # room_changes is not in _empty_result default, add dynamically
                    parsed.setdefault("room_changes", []).extend(args["room_changes"])
                sd = args.get("scene_details")
                if sd:
                    parsed["scene_details"] = sd

            elif name == "update_time":
                et = args.get("end_time")
                if et:
                    parsed["end_time"] = et

            elif name == "update_world":
                # Filter object-level properties
                for wp in args.get("world_property_changes", []):
                    wp_id = wp.get("id", "")
                    if wp_id and any(p in wp_id.lower() for p in self._REJECT_WP_PATTERNS_TOOL):
                        continue
                    parsed["world_property_changes"].append(wp)

            elif name == "update_extended":
                parsed["offscreen_npc_updates"].extend(args.get("offscreen_npc_updates", []))
                parsed["new_npcs"].extend(args.get("new_npcs", []))
                parsed["faction_reputation_changes"].extend(args.get("faction_reputation_changes", []))
                parsed["moral_alignment_changes"].extend(args.get("moral_alignment_changes", []))
                parsed["recruit_companions"].extend(args.get("recruit_companions", []))
                parsed["dismiss_companions"].extend(args.get("dismiss_companions", []))
                parsed["invalidate_lore"].extend(args.get("invalidate_lore", []))

        return parsed

    async def _execute_state_settlement(
        self, ctx: dict, narrative_text: str, plot_decision: str,
        action_text: str, state: dict, old_time: str = "",
    ) -> dict:
        """Stage 4: 单次工具调用做状态推演（替代 5 路并行 LLM）。

        Returns a parsed dict compatible with parse_split_v3 output.
        """
        # 构建合并的 system prompt
        all_field_lines = [fdef["desc"] for fdef in self.prompt_builder._FIELD_DEFS.values()]
        system = (
            "你是游戏状态推演引擎。分析叙事文本和剧情骨架，通过工具调用更新游戏状态。\n"
            "你可以调用多个工具，每个工具负责不同的状态域。必须至少调用 update_time。\n\n"
            "各域对应工具：\n"
            "- update_resources: 属性/物品/持续状态/胜负\n"
            "- update_spatial: 位置/场景/NPC位置/房间\n"
            "- update_time: 时间推进（必调用）\n"
            "- update_world: 宏观世界属性\n"
            "- update_extended: 离场NPC/新NPC注册/阵营/道德/同伴\n\n"
            "字段参考：\n" + "\n".join(all_field_lines) + "\n\n"
            "规则：\n"
            "- 严格根据叙事中实际描写的事件推演，不推测或编造\n"
            "- 物品add限制：只能添加叙事中明确描写角色获得的物品\n"
            "- 仅[检定结果: 大成功]时可额外给予奖励；仅[检定结果: 大失败]时应施加惩罚\n"
            "- activate_states 不得包含天气或时段\n"
            "- world_property_changes 只写宏观世界级变量，不写物件状态\n"
            "- location_change 必须使用已知地点列表中的精确ID\n"
            "- end_time 是绝对时间戳，不是时长\n"
            "- 无变化的域不需要调用对应工具\n"
        )

        # 构建合并的 user message（合并 5 个 group 的 XML 段落）
        source_for_non_temporal = plot_decision if plot_decision else narrative_text
        use_pd = bool(plot_decision)
        _check_res = ctx.get("check_result")
        _active_sys = ctx.get("route", {}).get("systems") if isinstance(ctx.get("route"), dict) else None

        # resource 段落
        resource_content = self.prompt_builder._build_world_state_user_message(
            source_for_non_temporal, action_text, state, _check_res,
            group="resource", use_plot_decision=use_pd,
        )
        # spatial 段落
        spatial_content = self.prompt_builder._build_world_state_user_message(
            source_for_non_temporal, action_text, state, _check_res,
            group="spatial", use_plot_decision=use_pd,
        )
        # temporal 段落（使用 narrative 全文）
        temporal_source = narrative_text if narrative_text else plot_decision
        temporal_pd = not bool(narrative_text)
        temporal_content = self.prompt_builder._build_world_state_user_message(
            temporal_source, action_text, state, _check_res,
            group="temporal", use_plot_decision=temporal_pd,
        )
        # world 段落
        world_content = self.prompt_builder._build_world_state_user_message(
            source_for_non_temporal, action_text, state, _check_res,
            group="world", use_plot_decision=use_pd,
        )
        # ext 段落
        ext_content = self.prompt_builder._build_world_state_user_message(
            source_for_non_temporal, action_text, state, _check_res,
            group="ext", use_plot_decision=use_pd,
            event_sections=ctx.get("event_sections"),
        )

        # 去重合并：各段落有重叠的 player_action / narrative 等，只保留一份
        # 用 XML tag 标识各段落（保留 resource/spatial/temporal/world/ext 的域特定信息）
        from collections import OrderedDict
        seen_sections = OrderedDict()
        for content in [resource_content, spatial_content, temporal_content, world_content, ext_content]:
            for block in content.split("\n\n"):
                block = block.strip()
                if not block:
                    continue
                # 用首行/tag 作为去重 key
                key = block[:60]
                if key not in seen_sections:
                    seen_sections[key] = block
        merged_content = "\n\n".join(seen_sections.values())

        # end_time 决策指引
        if old_time:
            merged_content += (
                f"\n\n## end_time 决策指引\n"
                f"当前游戏时间: {old_time}\n"
                "你是end_time的唯一决策者。根据叙事最后场景的时间输出绝对时间戳：\n"
                "- 对话/观察/翻阅文件: 当前时间 +10~30分钟\n"
                "- 常规互动/短途移动: 当前时间 +30分钟~2小时\n"
                "- 长途旅行/大型战斗: 当前时间 +2~8小时\n"
                "- 睡觉/过夜: 若叙事写到入睡那一刻则给入睡时间（如23:30），若叙事写到醒来才给次日早晨\n"
                f"格式示例: {old_time[:10] or '1970-01-01'}T10:00:00"
            )

        messages = [{"role": "user", "content": merged_content}]

        # 工具循环（复用 _stage1_with_native_tools 的模式）
        state_tool_results = []
        max_rounds = 3

        for _ in range(max_rounds):
            resp = await self.ai_provider.generate_with_tools(
                messages, system=system, tools=STATE_TOOLS_SCHEMA,
                max_tokens=4096, **self._stage_kwargs("state"),
            )
            tc_list = resp.get("tool_calls")
            if not tc_list:
                break

            assistant_msg = {"role": "assistant", "content": resp.get("content") or None}
            reasoning = resp.get("reasoning_content")
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"], ensure_ascii=False)}}
                for tc in tc_list
            ]
            messages.append(assistant_msg)

            for tc in tc_list:
                result = self._run_state_tool(tc["name"], tc["arguments"])
                state_tool_results.append({"name": tc["name"], "args": tc["arguments"]})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        # 合并工具调用结果为 parsed 字典
        parsed = self._merge_state_tool_results(state_tool_results)
        parsed["narrative"] = narrative_text.strip() if narrative_text else ""
        return parsed

    def _get_skill_bonus(self, skill: str) -> int:
        player = self.current_state.get("player", {})
        # 优先从职业技能系统查找
        skills = player.get("skills", {})
        for sid, sdata in skills.items():
            sname = sdata.get("name", "") if isinstance(sdata, dict) else ""
            if skill in (sid, sname):
                prof = sdata.get("bonus", 0) if isinstance(sdata, dict) else 0
                parent_attr = sdata.get("parent_attribute", "") if isinstance(sdata, dict) else ""
                attrs = player.get("attributes", {})
                attr_val = 50
                if parent_attr:
                    av = attrs.get(parent_attr)
                    if isinstance(av, dict):
                        attr_val = av.get("value", 50)
                    elif isinstance(av, (int, float)):
                        attr_val = av
                return (attr_val - 10) // 2 + prof if isinstance(attr_val, (int, float)) else prof
        # 回退到原始属性查找
        attrs = player.get("attributes", {})
        for k, v in attrs.items():
            if isinstance(v, dict):
                if skill in (k, v.get("display_name", ""), v.get("name", "")):
                    val = v.get("value", 10)
                    return (val - 10) // 2 if isinstance(val, (int, float)) else 0
        return 0

    def _check_opening_choice(self, player_action: dict) -> dict | None:
        """Check if this is an opening choice and return its result.

        For conditional results, roll dice to determine the actual outcome.
        """
        if player_action.get("type") != "choice":
            return None
        choice_id = player_action.get("choice_id", "")
        if not choice_id.startswith("open_"):
            return None

        opening = self.script.get("opening", {})
        choices = opening.get("choices", [])

        # First try matching by the choice's own "id" field
        for c in choices:
            if c.get("id") == choice_id:
                result = c.get("result", {})
                if result.get("type") == "conditional":
                    return self._evaluate_conditional_result(result)
                return result

        # Fall back to index-based matching (open_0 → index 0)
        try:
            idx = int(choice_id.split("_")[1])
            if 0 <= idx < len(choices):
                result = choices[idx].get("result", {})
                if result.get("type") == "conditional":
                    return self._evaluate_conditional_result(result)
                return result
        except (IndexError, ValueError):
            pass
        return None

    def _evaluate_conditional_result(self, result: dict) -> dict:
        """Evaluate a conditional result by rolling dice.

        Conditional result format:
          {"type": "conditional", "dice": {...}, "success": {...}, "failure": {...},
           "threshold": 50, "description": "..."}
        """
        dice_config = result.get("dice", {"formula": "1d100"})
        threshold = result.get("threshold", 50)

        roll_result = self.dice.roll_and_resolve(dice_config, [])
        total = roll_result.total

        if total >= threshold:
            outcome = result.get("success", {})
        else:
            outcome = result.get("failure", {})

        # Merge the outcome with the base result info
        evaluated = {
            "description": outcome.get("description", result.get("description", "")),
            "state_changes": outcome.get("state_changes", []),
            "conditional_roll": total,
            "conditional_threshold": threshold,
            "conditional_success": total >= threshold,
        }
        return evaluated

    async def _infer_initial_state(self, narrative: str = "") -> bool:
        """根据角色信息和开场叙事，AI 推演调整初始背包、持续状态和NPC认识状态。"""
        player = self.current_state.get("player", {})
        if not player.get("bio") and not player.get("personality"):
            return True

        inventory = self.current_state.get("inventory", [])
        active_ps = self.current_state.get("active_persistent_states", [])
        all_ps = self.script.get("persistent_states", [])

        ps_info = [{"id": ps["id"], "name": ps.get("name", ""),
                     "description": ps.get("description", ""),
                     "active": ps["id"] in active_ps} for ps in all_ps]

        # NPC 列表（供 AI 推演 known/met）
        npc_info = []
        for npc in self.script.get("npcs", []):
            npc_st = self.current_state.get("npcs", {}).get(npc["id"], {})
            npc_info.append({
                "id": npc["id"],
                "name": npc.get("name", npc["id"]),
                "bio": (npc.get("bio", "") or "")[:60],
                "title": npc.get("title", ""),
                "known": npc_st.get("known", npc.get("known", True)),
                "met": npc_st.get("met", npc.get("met", False)),
            })

        narrative_ctx = f"\n开场情境: {narrative[:300]}" if narrative else ""

        prompt = f"""你是一个游戏初始化助手。根据角色信息调整初始背包、持续状态和NPC认识状态。

角色信息:
- 姓名: {player.get('name', '')}
- 身份: {player.get('bio', '')}
- 性格: {player.get('personality', '')}
- 位置: {player.get('location', '')}

世界背景: {self.script.get('world_background', '')[:200]}{narrative_ctx}

当前背包: {json.dumps(inventory, ensure_ascii=False)}

可用持续状态:
{json.dumps(ps_info, ensure_ascii=False)}

NPC列表:
{json.dumps(npc_info, ensure_ascii=False)}

请根据角色身份合理调整:
1. 背包: 移除与角色身份明显不符的物品，可添加1-2件符合角色身份的物品
2. 持续状态: 根据角色背景，决定每个状态是否应该激活
3. NPC认识状态: 判断角色是否"认识"(known)和"熟识"(met)各NPC:
   - known=true: 角色知道此人（同事、名人、同学等），能叫出名字
   - met=true: 角色与此人有过直接接触/交流，会显示好感度
   - 一个角色可以known但未met（知道但没见过面）

返回JSON:
{{"inventory": [{{"item":"物品名","quantity":数量}}], "active_persistent_states": ["状态ID"], "npc_known": {{"npc_id": {{"known": true, "met": false}}}}}}
npc_known 中只需包含需要改变的NPC（与当前状态不同的）。只返回JSON。"""

        if self.authors_note:
            prompt += f"\n\n[创作指令（影响初始状态的设定偏好）]\n{self.authors_note}"

        raw = await self.ai_provider.generate(
            [{"role": "user", "content": prompt}],
            system="你是游戏初始化助手，简洁精确，只返回JSON。",
            max_tokens=8192, **self._stage_kwargs("state"),
        )
        raw = strip_think_tags(raw)
        parsed = self._extract_json(raw)
        if not parsed:
            return False
        if "inventory" in parsed and isinstance(parsed["inventory"], list):
            self.current_state["inventory"] = parsed["inventory"]
        if "active_persistent_states" in parsed and isinstance(parsed["active_persistent_states"], list):
            valid_ids = {ps["id"] for ps in all_ps}
            self.current_state["active_persistent_states"] = [
                sid for sid in parsed["active_persistent_states"] if sid in valid_ids
            ]
        # 应用 NPC known/met 变更
        npc_known = parsed.get("npc_known", {})
        if isinstance(npc_known, dict):
            npcs_state = self.current_state.setdefault("npcs", {})
            valid_npc_ids = {n["id"] for n in self.script.get("npcs", [])}
            for npc_id, vals in npc_known.items():
                if npc_id not in valid_npc_ids or not isinstance(vals, dict):
                    continue
                st = npcs_state.setdefault(npc_id, {})
                if "known" in vals:
                    st["known"] = bool(vals["known"])
                if "met" in vals:
                    st["met"] = bool(vals["met"])
        return True

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """从 AI 输出中提取 JSON 对象（去除 code fences 后委托 _parse_simple_json）。"""
        stripped = text.strip()
        m = re.match(r'^```(?:json|JSON)?\s*\n?([\s\S]*?)\n?\s*```\s*$', stripped)
        if m:
            stripped = m.group(1).strip()
        return GameSession._parse_simple_json(stripped)

    async def _personalize_narrative(self, base_text: str) -> str | None:
        """AI 润色/重写开场叙事，返回润色后文本或 None。"""
        player = self.current_state.get("player", {})
        name = player.get("name", "")
        bio = player.get("bio", "")
        personality = player.get("personality", "")
        portrait = player.get("portrait_desc", "")
        location = player.get("location", "")
        long_term_goal = player.get("long_term_goal", "")
        world_bg = self.script.get("world_background", "")[:500]

        # 随机氛围标签，增加重复游玩多样性
        mood_tags = ["紧迫", "悠闲", "神秘", "幽默", "压抑", "温馨", "紧张", "荒诞", "庄严", "活泼"]
        mood = random.choice(mood_tags)

        npcs_present = []
        npcs_elsewhere = []
        for npc in self.script.get("npcs", []):
            npc_name = npc.get("name", npc["id"])
            npc_personality = npc.get("personality", "")
            npc_label = f"{npc_name}（{npc_personality}）" if npc_personality else npc_name
            npc_loc = npc.get("default_location", "")
            if not npc_loc or not location:
                npcs_present.append(npc_label)
            elif self._locations_match(location, npc_loc):
                npcs_present.append(npc_label)
            else:
                npcs_elsewhere.append(f"{npc_name}(在{npc_loc})")
        npc_presence = ""
        if npcs_present:
            npc_presence += f"\n- 在场NPC: {', '.join(npcs_present)}"
        if npcs_elsewhere:
            npc_presence += f"\n- 不在场NPC（不要出现在开场中）: {', '.join(npcs_elsewhere)}"

        # 声纹速查表：帮助区分在场NPC的说话风格
        voice_table = ""
        if npcs_present:
            voice_lines = []
            for npc in self.script.get("npcs", []):
                npc_name = npc.get("name", npc["id"])
                npc_personality = npc.get("personality", "")
                npc_loc = npc.get("default_location", "")
                if npc_loc and location and not self._locations_match(location, npc_loc):
                    continue
                hint = self.prompt_builder._derive_voice_hint(npc_personality)
                if hint:
                    voice_lines.append(f"  - {npc_name}: {hint}")
            if voice_lines:
                voice_table = "\n\nNPC说话风格（每个NPC的对话必须体现其独有风格，严禁雷同）:\n" + "\n".join(voice_lines)

        is_preset = bool(self.script.get("_preset_opening_text"))
        if is_preset:
            rewrite_guidance = (
                "这是剧本作者为该角色量身编写的专属开局。请在保留原文核心内容和风格的基础上，"
                "润色文笔、补充感官细节和环境氛围，使其更加生动。不要改变场景设定和情节走向。"
            )
        else:
            rewrite_guidance = (
                "如果角色的位置或身份与原始开场不匹配，请根据角色信息重新构建开场场景。\n"
                "如果匹配度高，可以在原始开场基础上融入角色特征。"
            )

        narrative_prompt = f"""你是一个文字游戏GM。请为这个角色生成贴合其身份和所在位置的开场描述。

{rewrite_guidance}

原始开场（{'请在此基础上润色' if is_preset else '仅供参考，可大幅改写'}）:
{base_text}

角色信息:
- 姓名: {name}
- 身份: {bio}
- 性格: {personality}
- 外貌: {portrait}
- 起始位置: {location}
- 长期目标: {long_term_goal}{npc_presence}

世界背景: {world_bg}{voice_table}

要求:
1. 用第二人称"你"描述，400-800字
2. 场景必须发生在角色的起始位置，自然描述其所处环境
3. 融入角色的性格、身份、外貌特征，让开局是为这个角色量身定做的
4. 角色的言行和内心活动要贴合其性格（活泼的角色不要写成冷静严肃的）
5. 在场的NPC可以出现在开场描述中，不在场的NPC不要出现
6. 角色对话使用中文弯引号 "…" 包裹
7. 本次开场的整体氛围倾向：{mood}（自然融入，不要生硬点明）
只输出润色后的叙事文本，不要输出选项或JSON，不要输出思考过程。"""

        if self.authors_note:
            narrative_prompt += f"\n\n[创作指令（在叙事中体现但不要直接提及）]\n{self.authors_note}"

        messages = [{"role": "user", "content": narrative_prompt}]
        raw_narrative = await self.ai_provider.generate(
            messages,
            system="你是一个创意写作助手。只输出叙事文本，不输出选项/JSON/思考过程。",
            **self._stage_kwargs("narrative"),
        )
        raw_narrative = strip_think_tags(raw_narrative)
        narrative = raw_narrative.strip()
        narrative = re.sub(r'\n```(?:game_state|json)?\s*\{.*$', '', narrative, flags=re.DOTALL).strip()
        narrative = re.sub(r'\n```\s*$', '', narrative).strip()
        return narrative or None

    async def _personalize_choices(self, narrative: str, base_choices: list) -> list | None:
        """基于润色后叙事，AI 润色开场选项。返回选项列表或 None。"""
        has_preset_choices = bool(self.script.get("_preset_opening_choices"))
        script_choices = self.script.get("opening", {}).get("choices", [])
        choices_lines = []
        for i, c in enumerate(base_choices):
            line = f"- open_{i}: {c['text']}"
            if i < len(script_choices):
                desc = script_choices[i].get("result", {}).get("description", "")
                if desc:
                    line += f"（预设结果方向: {desc}）"
            choices_lines.append(line)
        choices_text = "\n".join(choices_lines) if choices_lines else "（无固定选项）"
        choices_instruction = (
            "保留选项核心意图，根据润色后的叙事微调措辞使其贴合场景"
            if has_preset_choices else "保留核心意图方向，但措辞和情境应贴合角色"
        )
        choices_system = (
            "你是游戏选项设计师。请根据开场叙事润色已有的开场选项。\n"
            f"- {choices_instruction}\n"
            "- 必须保留原选项数量和 id（open_0, open_1, ... 一一对应）\n"
            "- 每个选项必须有 hint（1句话描述预期后果/代价/耗时）和 time_hint（ISO 8601，如PT30M）\n"
            "- 每个选项必须有 risk（safe/moderate/risky）\n"
            "- 当选项有前置条件（属性/物品等）且不满足时，设置 locked:true 和 lock_reason\n"
            '只返回 JSON: {"choices":[{"id":"open_0","text":"...","hint":"...","time_hint":"PT30M","risk":"moderate"},...]}'
        )

        # 构建资源状态摘要
        state_parts = []
        player = self.current_state.get("player", {})
        pc_bits = []
        if player.get("name"):
            pc_bits.append(player["name"])
        if player.get("personality"):
            pc_bits.append(f"性格:{player['personality'][:15]}")
        if player.get("long_term_goal"):
            pc_bits.append(f"目标:{player['long_term_goal'][:25]}")
        if pc_bits:
            state_parts.append(f"主角: {' | '.join(pc_bits)}")
        attrs = player.get("attributes", {})
        attr_bits = []
        for attr_name, attr_val in attrs.items():
            v = attr_val if isinstance(attr_val, (int, float)) else (attr_val.get("value") if isinstance(attr_val, dict) else None)
            if v is not None:
                dn = attr_val.get("display_name", attr_name) if isinstance(attr_val, dict) else attr_name
                attr_bits.append(f"{dn}={v}")
        if attr_bits:
            state_parts.append(f"属性: {' | '.join(attr_bits)}")
        inventory = self.current_state.get("inventory", [])
        if inventory:
            inv_items = []
            for item in inventory[:8]:
                name = item.get("item", "") if isinstance(item, dict) else str(item)
                qty = item.get("quantity", 1) if isinstance(item, dict) else 1
                if name:
                    inv_items.append(f"{name}×{qty}" if qty > 1 else name)
            if inv_items:
                state_parts.append(f"背包: {', '.join(inv_items)}")
        state_context = "\n".join(state_parts)

        choices_user = f"""开场叙事:
{narrative}

原始选项（按 id 顺序润色，不要增减、不要改 id）:
{choices_text}"""

        if state_context:
            choices_user += f"\n\n当前角色状态:\n{state_context}"

        if self.authors_note:
            choices_user += f"\n\n[创作指令（选项风格应与此一致）]\n{self.authors_note}"

        raw_choices = await self.ai_provider.generate(
            [{"role": "user", "content": choices_user}],
            system=choices_system,
            max_tokens=8192,
            **self._stage_kwargs("choices"),
        )
        raw_choices = strip_think_tags(raw_choices)
        data = self.response_parser._extract_json_from_raw(raw_choices)
        ai_choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(ai_choices, list) or not ai_choices:
            return None
        for ac in ai_choices:
            idx_str = str(ac.get("id", "")).replace("open_", "")
            try:
                idx = int(idx_str)
            except (ValueError, TypeError):
                continue
            if idx < 0 or idx >= len(base_choices):
                continue
            base = base_choices[idx]
            if not ac.get("hint") and base.get("hint"):
                ac["hint"] = base["hint"]
            if not ac.get("text") and base.get("text"):
                ac["text"] = base["text"]
            if not ac.get("time_hint") and base.get("time_hint"):
                ac["time_hint"] = base["time_hint"]
        return ai_choices

    @staticmethod
    def _build_opening_choices(opening: dict) -> list[dict]:
        """Build opening choices with hint from result.description."""
        choices = []
        for i, c in enumerate(opening.get("choices", [])):
            choice_data = {"id": f"open_{i}", "text": c.get("text", "")}
            result = c.get("result", {})
            if result.get("description"):
                choice_data["hint"] = result["description"]
            if result.get("type") == "conditional":
                hint = choice_data.get("hint", "")
                choice_data["hint"] = (hint + "（结果由骰子决定）").strip()
            choices.append(choice_data)
        return choices

    def _generate_opening_choices(self) -> list[dict]:
        """Generate context-aware opening choices — delegates to _generate_context_choices."""
        return self._generate_context_choices()

    def _roll_or_sustain(self, item: dict) -> object | None:
        """Roll a random item or sustain its previous result based on duration/cooldown.

        Returns a dice result if the item is active (rolled or sustained), or None
        if the item is on cooldown.
        """
        item_id = item["id"]
        ri_state = self.current_state.setdefault("random_item_state", {})
        st = ri_state.get(item_id)
        duration = item.get("duration_turns", 0)
        cooldown = item.get("cooldown_turns", 0)

        if st:
            # Item has prior state — check remaining / cooldown
            if st.get("remaining_turns", 0) > 0:
                # Still active from previous roll — sustain without re-rolling
                st["remaining_turns"] -= 1
                # Bug#6: 直接构造 sustain 结果，不调用 roll_and_resolve
                from engine.dice import DiceResult
                result = DiceResult(
                    raw_rolls=st.get("raw_rolls", []),
                    total_raw=st["total"],
                    modifier=0,
                    total=st["total"],
                    formula=st.get("formula", ""),
                    range_label=st.get("result_label", ""),
                    range_state_changes=[],  # Don't re-apply state changes
                )
                result.random_item_id = item_id
                result._sustained = True
                return result

            if st.get("cooldown_remaining", 0) > 0:
                st["cooldown_remaining"] -= 1
                return None  # On cooldown, skip

        # Roll fresh
        result = self.dice.roll_and_resolve(
            item.get("dice", {}), item.get("ranges", [])
        )
        result.random_item_id = item_id
        result._sustained = False

        # Track state if item has duration
        if duration > 0:
            ri_state[item_id] = {
                "total": result.total,
                "result_label": result.range_label or "",
                "raw_rolls": result.raw_rolls if hasattr(result, 'raw_rolls') else [],
                "formula": result.formula if hasattr(result, 'formula') else "",
                "remaining_turns": duration - 1,  # This turn counts as first
                "cooldown_remaining": 0,
            }
        elif cooldown > 0:
            # No duration but has cooldown — enter cooldown after this roll
            ri_state[item_id] = {
                "total": result.total,
                "result_label": result.range_label or "",
                "raw_rolls": [],
                "formula": "",
                "remaining_turns": 0,
                "cooldown_remaining": cooldown,
            }

        return result

    def _roll_always_active_dice(self) -> list:
        results = []
        for item in self.script.get("random_items", []):
            if item.get("trigger_type") == "always":
                result = self._roll_or_sustain(item)
                if result is not None:
                    results.append(result)
        return results

    def _roll_event_linked_dice(self, triggered_events: list[dict]) -> list:
        if not self.current_state.get("dice_check_enabled", True):
            return []
        results = []
        triggered_ids = {e["event_id"] for e in triggered_events}
        for item in self.script.get("random_items", []):
            if item.get("trigger_type") == "event_linked":
                linked_id = item.get("linked_event_id", "")
                if linked_id in triggered_ids:
                    result = self._roll_or_sustain(item)
                    if result is not None:
                        results.append(result)
        return results

    def _roll_conditional_dice(self) -> list:
        """Roll dice items with trigger_type='conditional' when their condition is met."""
        if not self.current_state.get("dice_check_enabled", True):
            return []
        results = []
        for item in self.script.get("random_items", []):
            if item.get("trigger_type") != "conditional":
                continue
            condition = item.get("trigger_condition") or item.get("trigger", "")
            if not condition:
                continue
            if self._evaluate_condition(condition):
                result = self._roll_or_sustain(item)
                if result is not None:
                    results.append(result)
        return results

    def _evaluate_tone(self, story_tree_result=None):
        """Evaluate tone rules and auto-derived tone signals, store in state."""
        tone_parts = []

        tone_rules = self.script.get("tone_rules", [])
        if tone_rules:
            best = None
            for rule in tone_rules:
                cond = rule.get("condition", "")
                if cond and not self._evaluate_condition(cond):
                    continue
                prio = rule.get("priority", 0)
                if best is None or prio > best.get("priority", 0):
                    best = rule
            if best:
                tone_parts.append(f"基调「{best.get('name', '')}」: {best.get('tone', '')}")
                if best.get("narrative_style"):
                    tone_parts.append(f"叙事风格: {best['narrative_style']}")

        from engine.story_tree import StoryTreeResult
        if isinstance(story_tree_result, StoryTreeResult):
            for node in story_tree_result.newly_active:
                if node.get("type") == "quest":
                    tone_parts.append("当前有进行中的任务——叙事应保持紧迫感")
                    break

        if self.event_engine:
            _ev_data = self.event_engine.get_events_for_prompt(self.current_state)
            if _ev_data.get("consequences"):
                for c in _ev_data["consequences"]:
                    if c.get("importance") == "high":
                        tone_parts.append("暗流涌动——重要后果即将触发")
                        break
        else:
            consequences = self.current_state.get("pending_consequences", [])
            for c in consequences:
                if c.get("importance") == "high":
                    tone_parts.append("暗流涌动——重要后果即将触发")
                    break

        threshold_events = self.current_state.get("_last_threshold_events", [])
        if threshold_events:
            tone_parts.append("情感波动——NPC 关系刚发生重大变化")

        weather = self.current_state.get("current_weather", "")
        if weather and any(w in weather for w in ("暴", "雷", "冰雹", "飓风")):
            tone_parts.append("恶劣天气——环境描写应体现压迫感")

        self.current_state["active_tone"] = tone_parts if tone_parts else []

    def _evaluate_condition(self, condition: str) -> bool:
        """Evaluate a simple condition string against current state.

        Supports formats like:
          'player.health < 30'
          'player.location == tavern'
          'current_weather == 阴雨'
          'player.health < 30 AND current_weather == 暴雨'
          'player.gold > 100 OR player.reputation > 50'
        """
        stripped = condition.strip()
        # G9: AND/OR 复合条件（不支持混用，先 AND 后 OR）
        if ' AND ' in stripped and ' OR ' in stripped:
            logging.getLogger(__name__).warning(
                "条件表达式同时包含 AND 和 OR，不支持混用，按 AND 优先拆分: %s", stripped
            )
            parts = stripped.split(' AND ')
            return all(self._evaluate_single_condition(p.strip()) for p in parts)
        if ' AND ' in stripped:
            parts = stripped.split(' AND ')
            return all(self._evaluate_single_condition(p.strip()) for p in parts)
        if ' OR ' in stripped:
            parts = stripped.split(' OR ')
            return any(self._evaluate_single_condition(p.strip()) for p in parts)
        return self._evaluate_single_condition(stripped)

    def _evaluate_single_condition(self, condition: str) -> bool:
        """Evaluate a single condition expression (no AND/OR)."""
        m = re.match(r'([\w.]+)\s*(==|!=|<|>|<=|>=)\s*(.+)', condition.strip())
        if not m:
            return True  # No parseable condition — always trigger
        path, op, raw_val = m.group(1), m.group(2), m.group(3).strip().strip('"').strip("'")

        # node_completed.{node_id} == true
        if path.startswith("node_completed."):
            node_id = path[len("node_completed."):]
            sts = self.current_state.get("story_tree_state", {})
            is_done = node_id in sts.get("completed", [])
            target = raw_val.lower() in ("true", "1", "yes")
            if op == "==":
                return is_done == target
            elif op == "!=":
                return is_done != target
            return False

        # milestone.{milestone_id} == true
        if path.startswith("milestone."):
            ms_id = path[len("milestone."):]
            is_achieved = ms_id in self.current_state.get("achieved_milestones", [])
            target = raw_val.lower() in ("true", "1", "yes")
            if op == "==":
                return is_achieved == target
            elif op == "!=":
                return is_achieved != target
            return False

        # event_fired.{event_id} == true
        if path.startswith("event_fired."):
            eid = path[len("event_fired."):]
            fired_ot = self.current_state.get("fired_one_time_events", [])
            trackers = self.current_state.get("cyclic_event_trackers", {})
            is_fired = eid in fired_ot or eid in trackers
            target = raw_val.lower() in ("true", "1", "yes")
            if op == "==":
                return is_fired == target
            elif op == "!=":
                return is_fired != target
            return False

        actual = self.state_manager._get_value(self.current_state, path)
        if actual is None:
            return False

        # Try numeric comparison
        try:
            num_actual = float(actual) if not isinstance(actual, (int, float)) else actual
            num_val = float(raw_val)
            ops = {'==': num_actual == num_val, '!=': num_actual != num_val,
                   '<': num_actual < num_val, '>': num_actual > num_val,
                   '<=': num_actual <= num_val, '>=': num_actual >= num_val}
            return ops.get(op, False)
        except (ValueError, TypeError):
            pass

        # String comparison
        str_actual = str(actual)
        if op == '==':
            return str_actual == raw_val
        elif op == '!=':
            return str_actual != raw_val
        elif op == '>=':
            return str_actual >= raw_val
        elif op == '<=':
            return str_actual <= raw_val
        elif op == '>':
            return str_actual > raw_val
        elif op == '<':
            return str_actual < raw_val
        return False

    def _npc_attitude_to_state_changes(self, npc_att_changes: list, state: dict | None = None) -> list:
        """Convert npc_attitude_changes into state_changes format.

        Supports three-dimensional relationships (trust/affection/fear).
        Input:  [{"npc_id": "guard", "dimension": "trust", "change": 10, "reason": "helped"}]
        Output: [{"target": "player.relationships.guard.trust", "op": "add", "value": 10, "reason": "helped"}]
        Falls back to trust dimension if no dimension specified.

        Args:
            state: 目标 state dict。为 None 时回退到 self.current_state（主回合路径）。
        """
        target_state = state if state is not None else self.current_state
        changes = []
        known_npcs = set(self._npc_by_id.keys()) | set(target_state.get("npcs", {}).keys())
        for item in npc_att_changes:
            npc_id = item.get("npc_id") or item.get("npc") or item.get("name", "")
            if not npc_id:
                continue
            # Reject attitude changes for non-existent NPCs
            if npc_id not in known_npcs:
                continue
            change_val = item.get("change", 0)
            reason = item.get("reason", "")
            dimension = item.get("dimension", "")
            op = "add"
            if isinstance(change_val, str):
                try:
                    change_val = int(change_val)
                except (ValueError, TypeError):
                    change_val = 0
            if change_val == 0 and item.get("attitude") is not None:
                change_val = item["attitude"]
                op = "set"

            # Check if current relationship is 3D format
            rels = target_state.get("player", {}).get("relationships", {})
            rel_val = rels.get(npc_id)
            is_3d = isinstance(rel_val, dict) and any(k in rel_val for k in ("trust", "affection", "fear"))

            if is_3d and dimension in ("trust", "affection", "fear"):
                changes.append({
                    "target": f"player.relationships.{npc_id}.{dimension}",
                    "op": op,
                    "value": change_val,
                    "reason": reason,
                })
            elif is_3d:
                # No dimension specified — default to trust
                changes.append({
                    "target": f"player.relationships.{npc_id}.trust",
                    "op": op,
                    "value": change_val,
                    "reason": reason,
                })
            else:
                # Legacy single-value relationship
                changes.append({
                    "target": f"player.relationships.{npc_id}",
                    "op": op,
                    "value": change_val,
                    "reason": reason,
                })

            # Store opinion and relationship_desc on NPC state
            opinion = item.get("opinion", "")
            rel_desc = item.get("relationship_desc", "")
            npc_state = target_state.get("npcs", {}).get(npc_id)
            if isinstance(npc_state, dict):
                if opinion:
                    npc_state["opinion"] = opinion
                if rel_desc:
                    npc_state["relationship_desc"] = rel_desc
        return changes

    def _auto_mark_npcs_known(self, narrative: str) -> None:
        """扫描叙事文本，自动把出现过的NPC标记为 known=true，并维护出场计数。

        修复"同一NPC反复被描述为首次出场"的问题：
        - AI 不一定可靠地输出 npcs.X.known=true 的 state_change
        - 凡是NPC名字出现在叙事中，即视为已与玩家建立认识
        - npc_encounter_counts 记录每个NPC在叙事中出现的回合数，供prompt builder提示AI不要重复介绍
        """
        if not narrative:
            return
        npcs_state = self.current_state.setdefault("npcs", {})
        encounter_counts = self.current_state.setdefault("npc_encounter_counts", {})
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            name = npc.get("name", npc_id)
            if not name:
                continue
            name_ok = (len(name) >= 2 if _CJK_RE.search(name) else len(name) >= 3) if name else False
            id_ok = len(npc_id) >= 3 if npc_id else False
            if not name_ok and not id_ok:
                continue
            matched = (name_ok and name in narrative) or (id_ok and npc_id in narrative)
            if matched:
                st = npcs_state.setdefault(npc_id, {})
                if isinstance(st, dict):
                    if not st.get("known"):
                        st["known"] = True
                    if not st.get("met"):
                        st["met"] = True
                encounter_counts[npc_id] = encounter_counts.get(npc_id, 0) + 1
        for npc_id, npc_st in list(npcs_state.items()):
            if npc_id in self._npc_by_id:
                continue
            if not isinstance(npc_st, dict):
                continue
            name = npc_st.get("name", "")
            if not name:
                continue
            name_ok = (len(name) >= 2 if _CJK_RE.search(name) else len(name) >= 3)
            if not name_ok:
                continue
            if name in narrative:
                if not npc_st.get("known"):
                    npc_st["known"] = True
                if not npc_st.get("met"):
                    npc_st["met"] = True
                encounter_counts[npc_id] = encounter_counts.get(npc_id, 0) + 1

    def _extract_unregistered_speakers(self, narrative: str) -> None:
        """Scan narrative for named speakers not in state["npcs"] and auto-register them.

        Prevents NPC inconsistency where a minor character (e.g. 金秘书) appears in
        one turn's narrative but isn't registered, causing the next turn to invent
        a different person at the same location.
        """
        if not narrative:
            return
        state = self.current_state
        npcs_dict = state.setdefault("npcs", {})
        player_loc = state.get("player", {}).get("location", "")
        player_name = state.get("player", {}).get("name", "")

        known_names = {player_name} if player_name else set()
        for npc_id, npc_st in npcs_dict.items():
            if isinstance(npc_st, dict):
                known_names.add(npc_st.get("name", ""))
        for npc in self.script.get("npcs", []):
            known_names.add(npc.get("name", ""))
        known_names.discard("")

        candidates: set[str] = set()
        for m in _SPEAKER_RE.finditer(narrative):
            name = m.group(1) or m.group(2)
            if not name or name in known_names:
                continue
            # 过滤明显不是人名的匹配
            if name in _SPEAKER_REJECT_WORDS:
                continue
            if any(c in _SPEAKER_REJECT_CHARS for c in name):
                continue
            # 纯动词/副词短语通常不含姓氏常见字，额外长度检查
            if len(name) > 4:
                continue
            candidates.add(name)

        for name in candidates:
            npc_id = self._slugify_name(name)
            if npc_id in npcs_dict or npc_id in self._npc_by_id:
                continue
            npcs_dict[npc_id] = {
                "name": name,
                "attitude_toward_player": 50,
                "known": True,
                "met": True,
                "default_location": player_loc,
                "current_location": player_loc,
                "bio": "",
                "personality": "",
                "capabilities": "",
                "title": "",
                "organizations": [],
                "superior": "",
            }
            state.setdefault("display_names", {})[npc_id] = name
            rels = state.get("player", {}).setdefault("relationships", {})
            if npc_id not in rels:
                rels[npc_id] = {"trust": 50, "affection": 50, "fear": 0}
            self.prompt_builder._npc_name_map[npc_id] = name

    @staticmethod
    def _slugify_name(name: str) -> str:
        """Convert a CJK name to a stable ID suitable for use as a dict key."""
        try:
            from pypinyin import lazy_pinyin
            return "_".join(lazy_pinyin(name))
        except Exception:
            return "npc_" + name

    @staticmethod
    def _reputation_title(value: int) -> str:
        if value >= 90:
            return "崇拜"
        if value >= 70:
            return "友好"
        if value >= 50:
            return "中立"
        if value >= 30:
            return "冷淡"
        if value >= 10:
            return "敌对"
        return "通缉"

    @staticmethod
    def _calc_attitude_from_3d(trust: float, affection: float, fear: float) -> int:
        """3D 关系 → attitude 统一公式：fear 超过 30 时产生负面影响。"""
        fear_penalty = max(0, fear - 30) * 0.5
        return int((trust + affection) / 2 - fear_penalty)

    def _sync_world_changes_to_lorebook(self, old_attitudes: dict):
        """Detect significant world state changes and create/update lorebook entries."""
        npcs = self.current_state.get("npcs", {})
        for npc_id, npc_data in npcs.items():
            if not isinstance(npc_data, dict):
                continue
            att = npc_data.get("attitude_toward_player", 50)
            old_att = old_attitudes.get(npc_id, 50)
            if att == old_att:
                continue
            lore_id = f"_world_npc_att_{npc_id}"
            if att <= 15:
                name = npc_data.get("name", npc_id)
                content = f"{name}对玩家极度敌对（好感度{att}），可能拒绝合作甚至发起攻击。"
                if any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    self.prompt_builder.lorebook.update_entry(lore_id, content=content)
                else:
                    self.prompt_builder.lorebook.add_entries([{
                        "id": lore_id, "keys": [name], "content": content,
                        "entry_type": "world_sync", "priority": 90,
                        "position": "after_world", "scan_depth": 5,
                    }])
            elif att >= 85:
                name = npc_data.get("name", npc_id)
                content = f"{name}对玩家极度友好（好感度{att}），愿意提供特殊帮助。"
                if any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    self.prompt_builder.lorebook.update_entry(lore_id, content=content)
                else:
                    self.prompt_builder.lorebook.add_entries([{
                        "id": lore_id, "keys": [name], "content": content,
                        "entry_type": "world_sync", "priority": 90,
                        "position": "after_world", "scan_depth": 5,
                    }])
            else:
                if any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    self.prompt_builder.lorebook.remove_entry(lore_id)

        aps = set(self.current_state.get("active_persistent_states", []))
        for ps in self.script.get("persistent_states", []):
            ps_id = ps["id"]
            lore_id = f"_world_ps_{ps_id}"
            if ps_id in aps:
                content = f"持续状态「{ps.get('name', ps_id)}」生效中。{ps.get('description', '')[:80]}"
                if not any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    keys = [ps.get("name", ps_id)]
                    self.prompt_builder.lorebook.add_entries([{
                        "id": lore_id, "keys": keys, "content": content,
                        "entry_type": "world_sync", "priority": 75,
                        "position": "after_world", "scan_depth": 3,
                    }])
            else:
                if any(e.id == lore_id for e in self.prompt_builder.lorebook.entries):
                    self.prompt_builder.lorebook.remove_entry(lore_id)

    def _check_faction_reputation_events(self, old_reps: dict):
        """Fire story tree events when faction reputation crosses configured thresholds."""
        thresholds = self.script.get("settings", {}).get("faction_reputation_events", {})
        if not thresholds:
            return
        if not self.story_tree_engine and not self.event_engine:
            return
        faction_rep = self.current_state.get("faction_reputation", {})
        for fid, rules in thresholds.items():
            fd = faction_rep.get(fid, {})
            new_val = fd.get("value", 50) if isinstance(fd, dict) else 50
            old_val = old_reps.get(fid, 50)
            if new_val == old_val:
                continue
            for rule in rules:
                evt = rule.get("event", "")
                val = rule.get("value", 0)
                op = rule.get("op", ">=")
                if not evt:
                    continue
                fired_key = f"_fac_rep_evt_{fid}_{evt}"
                if self.current_state.get(fired_key):
                    continue
                crossed = False
                if op == ">=" and old_val < val <= new_val:
                    crossed = True
                elif op == "<=" and old_val > val >= new_val:
                    crossed = True
                elif op == ">" and old_val <= val < new_val:
                    crossed = True
                elif op == "<" and old_val >= val > new_val:
                    crossed = True
                if crossed:
                    self._fire_event_dual(evt)
                    self.current_state[fired_key] = True

    def _sync_npc_attitudes(self):
        """Sync relationship values back to state.npcs[].attitude_toward_player.

        For 3D relationships, attitude = (trust + affection) / 2 - fear_penalty.
        """
        rels = self.current_state.get("player", {}).get("relationships", {})
        npcs = self.current_state.get("npcs", {})
        faction_rep = self.current_state.get("faction_reputation", {})
        for npc_id, npc_data in npcs.items():
            if isinstance(npc_data, dict) and npc_id in rels:
                val = rels[npc_id]
                if isinstance(val, dict) and any(k in val for k in ("trust", "affection", "fear")):
                    attitude = self._calc_attitude_from_3d(
                        val.get("trust", 50), val.get("affection", 50), val.get("fear", 0),
                    )
                elif isinstance(val, (int, float)):
                    attitude = int(val)
                else:
                    continue
                if faction_rep:
                    npc_def = self._npc_by_id.get(npc_id, {})
                    npc_orgs = npc_def.get("organizations") or npc_data.get("organizations", [])
                    for org_entry in (npc_orgs or []):
                        org_id = org_entry.get("id", org_entry) if isinstance(org_entry, dict) else org_entry
                        rep_data = faction_rep.get(org_id)
                        if isinstance(rep_data, dict):
                            attitude += int((rep_data.get("value", 50) - 50) * 0.3)
                        break
                npc_data["attitude_toward_player"] = max(0, min(100, attitude))

    _ATTITUDE_THRESHOLDS = [
        (90, "亲密", "友好"),
        (70, "友好", "中立"),
        (50, "中立", "冷淡"),
        (30, "冷淡", "敌对"),
    ]

    def _check_attitude_thresholds(self, old_attitudes: dict, att_changes: list = None) -> list[dict]:
        """检测NPC态度是否跨过关键阈值，返回通知列表。"""
        reason_map = {}
        for item in (att_changes or []):
            nid = item.get("npc_id") or item.get("npc") or item.get("name", "")
            reason = item.get("reason", "")
            if nid and reason:
                reason_map[nid] = reason

        notifications = []
        npcs = self.current_state.get("npcs", {})
        for npc_id, npc_data in npcs.items():
            if not isinstance(npc_data, dict):
                continue
            new_att = npc_data.get("attitude_toward_player")
            if new_att is None:
                continue
            if npc_id not in old_attitudes:
                continue
            old_att = old_attitudes[npc_id]
            if old_att == new_att:
                continue
            for threshold, above_label, below_label in self._ATTITUDE_THRESHOLDS:
                if old_att < threshold <= new_att:
                    npc_name = self._get_npc_display_name(npc_id)
                    reason = reason_map.get(npc_id, "")
                    msg = f"{npc_name}对你的态度提升至【{above_label}】"
                    if reason:
                        msg += f"（{reason}）"
                    notifications.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "direction": "up",
                        "new_level": above_label,
                        "reason": reason,
                        "message": msg,
                    })
                    break
                elif old_att >= threshold > new_att:
                    npc_name = self._get_npc_display_name(npc_id)
                    reason = reason_map.get(npc_id, "")
                    msg = f"{npc_name}对你的态度降至【{below_label}】"
                    if reason:
                        msg += f"（{reason}）"
                    notifications.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "direction": "down",
                        "new_level": below_label,
                        "reason": reason,
                        "message": msg,
                    })
                    break
        return notifications

    def _get_npc_display_name(self, npc_id: str) -> str:
        """Get NPC display name: state > script > fallback to ID."""
        npc_st = self.current_state.get("npcs", {}).get(npc_id)
        if isinstance(npc_st, dict) and npc_st.get("name"):
            return npc_st["name"]
        npc = self._npc_by_id.get(npc_id)
        return npc.get("name", npc_id) if npc else npc_id

    def _enrich_choice_previews(self, choices: list[dict]) -> list[dict]:
        """Post-process choices to add preview tags for informed decision-making."""
        inventory = {it.get("item"): it for it in self.current_state.get("inventory", []) if isinstance(it, dict)}
        atmo = self.current_state.get("time_atmosphere", {})
        da = self.current_state.get("difficulty_awareness", {})

        for c in choices:
            text = c.get("text", "")
            previews = []

            # Time cost from time_hint
            th = c.get("time_hint", "")
            if th:
                hrs = 0
                hm = re.search(r'PT(?:(\d+)H)?(?:(\d+)M)?', th)
                if hm:
                    hrs = int(hm.group(1) or 0) + int(hm.group(2) or 0) / 60
                    if hrs >= 1:
                        previews.append(f"\U0001F552 约{int(hrs)}小时")
                    elif int(hm.group(2) or 0) > 0:
                        previews.append(f"\U0001F552 约{hm.group(2)}分钟")

            # Item usage detection
            for item_name, item_data in inventory.items():
                if item_name in text and item_data.get("use_effect"):
                    eff = item_data["use_effect"]
                    for sc in eff.get("state_changes", []):
                        target = sc.get("target", "").split(".")[-1]
                        change = sc.get("value", sc.get("change", 0))
                        sign = "+" if change > 0 else ""
                        previews.append(f"\U0001F48A {target}{sign}{change}")
                    if eff.get("consumable", True):
                        previews.append(f"\U0001F4E6 消耗{item_name}")
                    break

            # Travel detection
            travel_kws = ("前往", "去", "移动", "出发", "赶往", "返回")
            if any(kw in text for kw in travel_kws):
                previews.append("\U0001F6B6 移动")

            # Difficulty hint
            momentum = da.get("player_momentum", "balanced")
            risk = c.get("risk", "")
            if risk == "risky" and momentum == "struggling":
                previews.append("⚠ 当前状态不佳")

            # Time atmosphere hint
            period = atmo.get("period_label", "")
            if period and any(kw in text for kw in ("潜行", "偷", "暗中")):
                light = atmo.get("light_level", "bright")
                if light == "dark":
                    previews.append("\U0001F319 夜间加成")
                elif light == "bright":
                    previews.append("☀ 白天不利")

            if previews:
                c["previews"] = previews
        return choices

    def _generate_context_choices(self) -> list[dict]:
        """Generate context-aware default choices based on current game state."""
        choices = []
        player = self.current_state.get("player", {})
        player_loc = player.get("location", "")

        # Choice 1: prioritize pending consequences or milestone progress
        pending_cons_desc = ""
        if self.event_engine:
            _ev_data = self.event_engine.get_events_for_prompt(self.current_state)
            if _ev_data.get("consequences"):
                pending_cons_desc = _ev_data["consequences"][0].get("description", "")
        else:
            _old_cons = self.current_state.get("pending_consequences", [])
            if _old_cons:
                pending_cons_desc = _old_cons[0].get("description", "")
        if pending_cons_desc:
            choices.append({"id": "c1", "text": f"调查{pending_cons_desc[:15]}的情况"})
        else:
            loc_name = self.prompt_builder._resolve_location_name(player_loc) if player_loc else "周围"
            choices.append({"id": "c1", "text": f"探索{loc_name}"})

        # Choice 2: talk to present NPC(s) — show specific names when <=2
        present_npcs = []
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            npc_location = self._get_npc_location(npc_id)
            if npc_location and self._locations_match(player_loc, npc_location):
                present_npcs.append((npc_id, npc.get("name", npc_id)))
        # 也扫描动态NPC（不在script中、仅存在于state的NPC）
        for npc_id, info in self.current_state.get("npcs", {}).items():
            if npc_id not in self._npc_by_id and npc_id not in [p[0] for p in present_npcs]:
                if not isinstance(info, dict):
                    continue
                cur_loc = info.get("current_location", info.get("default_location", ""))
                if cur_loc and self._locations_match(player_loc, cur_loc):
                    present_npcs.append((npc_id, info.get("name", npc_id)))
        if len(present_npcs) == 1:
            choices.append({"id": "c2", "text": f"与{present_npcs[0][1]}交谈"})
        elif len(present_npcs) == 2:
            choices.append({"id": "c2", "text": f"与{present_npcs[0][1]}交谈"})
            choices.append({"id": "c2b", "text": f"与{present_npcs[1][1]}交谈"})
        elif len(present_npcs) > 2:
            names = "、".join(n[1] for n in present_npcs[:3])
            choices.append({"id": "c2", "text": f"与附近的人交谈（{names}）"})
        else:
            choices.append({"id": "c2", "text": "寻找附近的人交谈"})

        # Choice 3: based on weather/time/inventory/milestones
        inventory = self.current_state.get("inventory", [])
        weather = self.current_state.get("current_weather", "")
        milestones = self.current_state.get("active_milestones", [])
        if weather and any(w in weather for w in ("暴雨", "大雪", "极端")):
            choices.append({"id": "c3", "text": "寻找避难处"})
        elif milestones:
            ms_desc = milestones[0] if isinstance(milestones[0], str) else milestones[0].get("name", "")
            if ms_desc:
                choices.append({"id": "c3", "text": f"思考如何推进「{ms_desc[:10]}」"})
            else:
                choices.append({"id": "c3", "text": "整理思绪，规划下一步"})
        elif inventory:
            choices.append({"id": "c3", "text": "查看背包物品"})
        else:
            choices.append({"id": "c3", "text": "环顾四周，观察情况"})

        # Choice 4: difficulty-adaptive suggestion
        da = self.current_state.get("difficulty_awareness", {})
        momentum = da.get("player_momentum", "balanced")
        pacing = self.current_state.get("pacing_state", {})
        if pacing.get("consecutive_low", 0) >= 4 and pacing.get("tension", 50) <= 25:
            choices.append({"id": "c4", "text": "调查周围是否有异常动静"})
        elif momentum == "struggling":
            choices.append({"id": "c4", "text": "寻找休息或恢复的机会", "risk": "safe"})
        elif momentum == "dominating":
            active_quests = self.current_state.get("story_tree_state", {}).get("active", [])
            if active_quests:
                choices.append({"id": "c4", "text": "主动推进当前目标"})
            else:
                choices.append({"id": "c4", "text": "寻找更大的挑战"})

        return choices

    async def _ai_check_same_location(self, loc_a: str, loc_b: str) -> bool:
        """Use AI to check if two location descriptions refer to the same or nearby place."""
        if not self.ai_provider:
            return False
        cache_key = (loc_a, loc_b) if loc_a <= loc_b else (loc_b, loc_a)
        if not hasattr(self, '_location_match_cache'):
            self._location_match_cache = {}
        if cache_key in self._location_match_cache:
            return self._location_match_cache[cache_key]
        # Resolve IDs to names for better AI understanding
        name_a = self._resolve_location_id(loc_a) or loc_a
        name_b = self._resolve_location_id(loc_b) or loc_b
        try:
            response = await self.ai_provider.generate(
                [{"role": "user", "content":
                    f"在这个游戏世界中，以下两个位置是否指同一个地方或足够近可以直接对话？\n"
                    f"位置A: {name_a}\n位置B: {name_b}\n"
                    f"仅回答'是'或'否'，不需要解释。"}],
                system="你是一个简洁的位置判断助手。"
            )
            result = "是" in response.strip()[:5]
            self._location_match_cache[cache_key] = result
            return result
        except Exception:
            return False

    def _locations_match(self, loc_a: str, loc_b: str) -> bool:
        """Check if two locations refer to the same place.

        Tries exact match first, then resolves location IDs to names for comparison.
        """
        if not loc_a or not loc_b:
            return False
        # Exact match (case-insensitive)
        if loc_a.strip().lower() == loc_b.strip().lower():
            return True
        # Resolve IDs to names and compare
        name_a = self._resolve_location_id(loc_a)
        name_b = self._resolve_location_id(loc_b)
        if name_a and name_b and name_a.lower() == name_b.lower():
            return True
        # Also check if one is an ID and the other is its name
        if name_a and name_a.lower() == loc_b.strip().lower():
            return True
        if name_b and name_b.lower() == loc_a.strip().lower():
            return True
        # 层级位置匹配：如果一方是另一方的父/子位置，也算在场
        if self._is_child_location(loc_a, loc_b) or self._is_child_location(loc_b, loc_a):
            return True
        return False

    def _is_child_location(self, parent_id: str, child_id: str) -> bool:
        """Check if child_id is a sub-location of parent_id (one level)."""
        child_def = self._location_by_id.get(child_id)
        if child_def:
            p = child_def.get("parent") or child_def.get("parent_location") or ""
            if p and p.strip().lower() == parent_id.strip().lower():
                return True
        parent_def = self._location_by_id.get(parent_id)
        if parent_def:
            contains = parent_def.get("contains") or parent_def.get("sub_locations") or []
            if child_id in contains:
                return True
        return False

    def _resolve_location_id(self, location: str) -> str:
        """Resolve a location ID to its display name, or return empty if not found."""
        loc = self._location_by_id.get(location)
        return loc.get("name", location) if loc else ""

    def _get_npc_location(self, npc_id: str, state: dict | None = None) -> str:
        """Get NPC's current location: state.current_location > schedule > default_location."""
        _st = state if state is not None else self.current_state
        # Highest priority: AI-updated current_location (from offscreen updates / new NPCs)
        npc_state = _st.get("npcs", {}).get(npc_id, {})
        if isinstance(npc_state, dict) and npc_state.get("current_location"):
            return npc_state["current_location"]
        game_time = _st.get("game_time", "")
        # Try schedule
        info = self.prompt_builder.get_npc_current_info(npc_id, game_time, state=_st)
        if info and info.get("location"):
            return info["location"]
        # Fall back to default_location in state
        if isinstance(npc_state, dict) and npc_state.get("default_location"):
            return npc_state["default_location"]
        # Fall back to script definition (P1: 使用字典查找)
        npc_def = self._npc_by_id.get(npc_id)
        if npc_def:
            return npc_def.get("default_location", npc_def.get("initial_location", ""))
        return ""

    def _ensure_rooms_initialized(self):
        """从 schedule activity 推断 NPC 的 current_room（仅在 room 为空时执行）。"""
        game_time = self.current_state.get("game_time", "")
        npcs_state = self.current_state.get("npcs", {})
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            ns = npcs_state.get(npc_id)
            if not isinstance(ns, dict):
                continue
            if ns.get("current_room"):
                continue
            info = self.prompt_builder.get_npc_current_info(npc_id, game_time, state=self.current_state)
            if info and info.get("activity"):
                room = self._extract_room_from_activity(info["activity"])
                if room:
                    ns["current_room"] = room
        # 玩家房间
        player = self.current_state.get("player", {})
        if not player.get("current_room"):
            loc = player.get("location", "")
            loc_def = self._location_by_id.get(loc, {})
            default_room = loc_def.get("default_room", "")
            if default_room:
                player["current_room"] = default_room

    @staticmethod
    def _extract_room_from_activity(activity: str) -> str:
        """从 activity 描述中提取房间/位置关键词。"""
        m = re.search(
            r'在([^，。、,\s]{2,10}(?:办公室|卧室|走廊|会议室|值班室|食堂|茶水间|资料室|门口|大厅|庭院|官邸|营房|书房|寝室|餐厅|接待室|警卫室|指挥所|车库))',
            activity
        )
        if m:
            return m.group(1)
        m = re.search(r'在(\S{2,8})', activity)
        if m:
            return m.group(1)
        return ""

    def _apply_weather(self, dice_results: list):
        for dr in dice_results:
            item_id = dr.random_item_id
            if item_id == "weather" and dr.range_label:
                self.current_state["current_weather"] = dr.range_label

    def _get_event_description(self, event_id: str) -> str:
        """Look up event description from script by ID."""
        return self._event_desc_by_id.get(event_id, event_id)

    def _dice_result_to_dict(self, dr) -> dict:
        item_id = dr.random_item_id
        # Look up description and trigger_type from script
        duration_info = ""
        item = self._random_item_by_id.get(item_id)
        description = item.get("description", item_id) if item else item_id
        trigger_type = item.get("trigger_type", "") if item else ""
        # Generate source label for prompt clarity
        source_labels = {
            "always": "环境骰子",
            "conditional": "条件骰子",
            "event_linked": "事件骰子",
        }
        source_label = source_labels.get(trigger_type, description)
        # Add sustained info
        sustained = dr._sustained
        ri_state = self.current_state.get("random_item_state", {}).get(item_id, {})
        remaining = ri_state.get("remaining_turns", 0)
        if sustained:
            duration_info = f"(持续中，剩余{remaining}回合)"
        return {
            "random_item_id": item_id,
            "description": description,
            "source_label": source_label,
            "formula": dr.formula,
            "raw_rolls": dr.raw_rolls,
            "total": dr.total,
            "range_label": dr.range_label,
            "sustained": sustained,
            "remaining_turns": remaining,
            "duration_info": duration_info,
        }

    @staticmethod
    def _advance_game_time(time_str: str, delta: timedelta) -> str:
        t = parse_time(time_str)
        if not t:
            # P0-7: 升级为 ERROR — 时间无法推进意味着事件/昼夜系统失效，需要告警
            logging.getLogger(__name__).error(
                "无法解析游戏时间 %r，时间未推进（事件调度可能失效）", time_str
            )
            return time_str
        return (t + delta).isoformat()

    @staticmethod
    def _apply_iso_duration(time_str: str, duration: str) -> str:
        """Parse a simple ISO 8601 duration and apply it.

        P0-7: 也兼容 AI 常见的非标准格式（"30 minutes" / "2 hours" / "1 day"），
        而不是静默不推进。
        """
        t = parse_time(time_str)
        if not t:
            logging.getLogger(__name__).error(
                "无法解析游戏时间 %r 用于 duration %r，时间未推进", time_str, duration
            )
            return time_str

        if not isinstance(duration, str):
            return time_str

        dur = duration.strip()

        # Simple parser for PT30M, PT2H, P1D style durations
        m = re.match(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$', dur)
        if m and (m.group(1) or m.group(2) or m.group(3)):
            days = int(m.group(1) or 0)
            hours = int(m.group(2) or 0)
            minutes = int(m.group(3) or 0)
            return (t + timedelta(days=days, hours=hours, minutes=minutes)).isoformat()

        # Fallback: 兼容自然语言（"30 minutes" / "2小时" / "1 day"）
        nat = re.match(
            r'(\d+)\s*(minute|min|m|hour|hr|h|day|d|分钟|分|小时|时|天|日)s?\b',
            dur, re.IGNORECASE,
        )
        if nat:
            n = int(nat.group(1))
            unit = nat.group(2).lower()
            if unit in ("minute", "min", "m", "分钟", "分"):
                return (t + timedelta(minutes=n)).isoformat()
            if unit in ("hour", "hr", "h", "小时", "时"):
                return (t + timedelta(hours=n)).isoformat()
            if unit in ("day", "d", "天", "日"):
                return (t + timedelta(days=n)).isoformat()

        logging.getLogger(__name__).warning(
            "无法识别的 duration %r，时间未推进", duration
        )
        return time_str

    # ================================================
    #  NEW GAMEPLAY SYSTEMS
    # ================================================

    def _apply_inventory_change(self, inv: dict):
        """Apply an inventory change (add/remove item)."""
        item_name = inv.get("item", "")
        if not item_name:
            return
        action = inv.get("action", "add")
        quantity = inv.get("quantity", 1)
        description = inv.get("description", "")
        inventory = self.current_state.setdefault("inventory", [])

        if action == "add":
            # Check if item already exists (fuzzy: also match substring)
            exact = None
            fuzzy = None
            for entry in inventory:
                ename = entry.get("item", "")
                if ename == item_name:
                    exact = entry
                    break
                if not fuzzy and (item_name in ename or ename in item_name):
                    fuzzy = entry
            target = exact or fuzzy
            if target:
                target["quantity"] = target.get("quantity", 1) + quantity
                if description:
                    target["description"] = description
                return
            new_entry = {"item": item_name, "quantity": quantity}
            if description:
                new_entry["description"] = description
            inventory.append(new_entry)
        elif action == "remove":
            exact = None
            fuzzy = None
            for entry in inventory:
                ename = entry.get("item", "")
                if ename == item_name:
                    exact = entry
                    break
                if not fuzzy and (item_name in ename or ename in item_name):
                    fuzzy = entry
            target = exact or fuzzy
            if target:
                target["quantity"] = target.get("quantity", 1) - quantity
                if target["quantity"] <= 0:
                    inventory.remove(target)
                return

    def _resolve_skill_check_from_route(self, route: dict, action_text: str) -> dict | None:
        """Route model primary, keyword matching fallback."""
        if not self.current_state.get("dice_check_enabled", True):
            return None
        check_result = None
        sc = route.get("skill_check", {})
        if isinstance(sc, dict) and sc.get("needed"):
            attr = sc.get("attr", "")
            difficulty = sc.get("difficulty", "medium")
            if difficulty not in ("easy", "medium", "hard", "extreme"):
                difficulty = "medium"
            attrs = self.current_state.get("player", {}).get("attributes", {})
            attr_val = None
            matched_attr_name = attr
            if attr:
                # Phase 1: exact match
                for attr_name, val in attrs.items():
                    if attr == attr_name:
                        attr_val = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                        matched_attr_name = attr_name
                        break
                # Phase 2: containment match (only if attr is >= 2 chars to avoid single-char ambiguity)
                if attr_val is None and len(attr) >= 2:
                    for attr_name, val in attrs.items():
                        if attr in attr_name or attr_name in attr:
                            attr_val = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                            matched_attr_name = attr_name
                            break
                # Phase 3: synonym mapping for AI-generated attr names
                if attr_val is None:
                    attr_lower = attr.lower()
                    for canonical, synonyms in _ATTR_SYNONYMS.items():
                        if attr_lower == canonical or any(attr_lower == s or attr_lower in s or s in attr_lower for s in synonyms):
                            for attr_name, val in attrs.items():
                                v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                                name_lower = attr_name.lower()
                                if canonical in name_lower or name_lower in canonical or any(s in name_lower or name_lower in s for s in synonyms):
                                    attr_val = v
                                    matched_attr_name = attr_name
                                    break
                            break
            check_result = self._resolve_check(difficulty, matched_attr_name, attr_val)
            logger.debug("Route skill check: attr=%s diff=%s outcome=%s", attr, difficulty, check_result.get("outcome"))
        if not check_result and action_text:
            check_result = self._maybe_skill_check(action_text)
        if check_result:
            self._apply_skill_growth(check_result)
            self._record_check_outcome(check_result.get("outcome", "failure"))
        return check_result

    def _maybe_skill_check(self, action_text: str) -> dict | None:
        """Determine if a freeform action warrants a skill check.

        Uses keyword heuristics and adjusts difficulty by relevant attributes.
        Flow#1: 从剧本 settings.skill_check_map 读取自定义技能映射，否则使用默认值。
        """
        if not self.current_state.get("dice_check_enabled", True):
            return None

        # 优先从剧本设置读取技能映射
        script_skill_map = self.script.get("settings", {}).get("skill_check_map")
        if script_skill_map:
            skill_map = script_skill_map
        elif self.class_registry:
            # 从 ClassRegistry 生成技能映射
            class_system = self.script.get("settings", {}).get("class_system", "dnd5e")
            player_skills = list(self.current_state.get("player", {}).get("skills", {}).keys())
            skill_map = self.class_registry.build_skill_check_map(class_system, player_skills)
        else:
            # 默认 skill_map — 属性名动态匹配玩家实际属性
            attrs = self.current_state.get("player", {}).get("attributes", {})
            attr_names = list(attrs.keys())
            # 将关键词映射到语义类别，再从玩家属性中找最佳匹配
            _CATEGORY_KEYWORDS = {
                "physical_power": ("力量", "武力", "力", "武", "攻击", "战斗", "strength", "str"),
                "agility": ("敏捷", "身法", "速度", "灵巧", "反应", "dexterity", "dex", "agility"),
                "intelligence": ("智力", "智谋", "智慧", "才学", "学识", "知识", "情报", "intelligence", "int", "wisdom", "wis"),
                "charisma": ("魅力", "口才", "话术", "社交", "外交", "领导", "统率", "政治", "charisma", "cha"),
                "stamina": ("体力", "耐力", "体质", "constitution", "con", "stamina"),
            }
            # 从玩家属性中为每个类别找最佳匹配
            _resolved = {}
            for cat, synonyms in _CATEGORY_KEYWORDS.items():
                for a in attr_names:
                    al = a.lower()
                    if any(s in al or al in s for s in synonyms):
                        _resolved[cat] = a
                        break
            # 如果找不到匹配，回退到第一个属性
            fallback_attr = attr_names[0] if attr_names else "未知"
            p = _resolved.get("physical_power", fallback_attr)
            a = _resolved.get("agility", fallback_attr)
            i = _resolved.get("intelligence", fallback_attr)
            c = _resolved.get("charisma", fallback_attr)
            s = _resolved.get("stamina", p)
            skill_map = {
                "extreme": {
                    "暗杀": a, "刺杀": a,
                    "召唤": i, "复活": i,
                },
                "hard": {
                    "偷窃": a, "偷东西": a, "扒窃": a, "行窃": a, "撬锁": a, "撬开": a,
                    "翻墙": p, "攀爬": p, "跳跃": p, "跳下": p, "跳上": p, "跳过去": p,
                    "潜入": a, "隐藏": a, "偷袭": a,
                    "逃跑": a, "躲避": a,
                    "攻击": p, "打斗": p, "殴打": p, "格挡": p, "拳击": p,
                    "射击": a,
                    "欺骗": c, "说谎": c, "伪装": c,
                    "威胁": c, "恐吓": c,
                    "说服": c, "诱惑": c,
                    "施法": i,
                    "游泳": s, "破解": i,
                },
                "medium": {
                    "调查": i, "搜索": i, "观察": i,
                    "询问": c, "打听": c, "交涉": c, "谈判": c,
                    "追踪": i, "解读": i,
                    "修理": i, "制作": i, "治疗": i,
                },
                "easy": {
                    "打招呼": c, "闲聊": c, "问路": c,
                    "翻找": i, "聆听": i, "感知": i,
                    "推动": p, "拉动": p, "搬运": p, "搬开": p, "推开": p, "拉开": p,
                },
            }

        action_lower = action_text.lower()
        difficulty = None
        related_attr = None
        matched_keyword = None

        # 将所有难度的关键词汇总，按长度降序排序（最长优先匹配，避免"打招呼"被"打"抢先）
        all_keywords: list[tuple[str, str, str]] = []  # (keyword, difficulty, attribute)
        for diff_level in ("extreme", "hard", "medium", "easy"):
            level_map = skill_map.get(diff_level, {})
            for kw, attr in level_map.items():
                all_keywords.append((kw, diff_level, attr))
        all_keywords.sort(key=lambda x: len(x[0]), reverse=True)

        for kw, diff_level, attr in all_keywords:
            if kw in action_lower:
                difficulty = diff_level
                related_attr = attr
                matched_keyword = kw
                break

        if not difficulty:
            return None

        # 查找关联属性值
        attrs = self.current_state.get("player", {}).get("attributes", {})
        attr_val = None
        matched_attr_name = related_attr
        if related_attr and attrs:
            # Phase 1: 精确或包含匹配
            for attr_name, val in attrs.items():
                v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                if related_attr in attr_name or attr_name in related_attr:
                    attr_val = v
                    matched_attr_name = attr_name
                    break
            # Phase 2: 属性不存在时，用语义类别映射到玩家实际属性
            if attr_val is None:
                synonyms = _ATTR_SYNONYMS.get(related_attr, ())
                if synonyms:
                    for attr_name, val in attrs.items():
                        v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                        name_lower = attr_name.lower()
                        if any(s in name_lower or name_lower in s for s in synonyms):
                            attr_val = v
                            matched_attr_name = attr_name
                            break
        related_attr = matched_attr_name

        # 职业系统增强：查找技能ID和熟练加值
        skill_id = None
        prof_bonus = 0
        if self.class_registry and matched_keyword:
            class_system = self.script.get("settings", {}).get("class_system", "dnd5e")
            skill_id, _ = self.class_registry.find_skill_by_keyword(matched_keyword, class_system)
            if skill_id:
                player_state = self.current_state.get("player", {})
                prof_bonus = self.class_registry.get_proficiency_bonus_for_skill(
                    skill_id, player_state, class_system,
                )
                # CoC: 用技能百分比值替代原始属性值
                if class_system in ("coc", "brp"):
                    skill_val = self.class_registry.get_skill_value(skill_id, player_state, class_system)
                    if skill_val is not None:
                        attr_val = skill_val

        return self._resolve_check(difficulty, related_attr, attr_val,
                                    skill_id=skill_id, prof_bonus=prof_bonus)

    def _maybe_talk_skill_check(self, message: str) -> dict | None:
        """Simplified skill check for NPC dialogue — triggers on persuade/threaten/deceive."""
        if not self.current_state.get("dice_check_enabled", True):
            return None
        talk_skills = {
            "说服": ("魅力", "medium"), "劝说": ("魅力", "medium"),
            "威胁": ("魅力", "hard"), "恐吓": ("魅力", "hard"),
            "欺骗": ("魅力", "hard"), "说谎": ("魅力", "hard"),
            "套话": ("智力", "medium"), "打听": ("魅力", "easy"),
            "求助": ("魅力", "easy"), "请求": ("魅力", "easy"),
        }
        msg_lower = message.lower()
        related_attr = None
        difficulty = None
        for kw, (attr, diff) in talk_skills.items():
            if kw in msg_lower:
                related_attr = attr
                difficulty = diff
                break
        if not difficulty:
            return None
        attrs = self.current_state.get("player", {}).get("attributes", {})
        attr_val = None
        matched_attr_name = related_attr
        if related_attr and attrs:
            for attr_name, val in attrs.items():
                v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                if related_attr in attr_name or attr_name in related_attr:
                    attr_val = v
                    matched_attr_name = attr_name
                    break
            if attr_val is None:
                synonyms = _ATTR_SYNONYMS.get(related_attr, ())
                if synonyms:
                    for attr_name, val in attrs.items():
                        v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                        name_lower = attr_name.lower()
                        if any(s in name_lower or name_lower in s for s in synonyms):
                            attr_val = v
                            matched_attr_name = attr_name
                            break
        return self._resolve_check(difficulty, matched_attr_name, attr_val)

    def _resolve_check(self, difficulty: str, related_attr: str, attr_val: int | None,
                        *, skill_id: str | None = None, prof_bonus: int = 0) -> dict:
        """根据剧本 check_rule 设置分发到对应规则的骰子判定。"""
        growth_bonus = 0
        if related_attr:
            growth_entry = self.current_state.get("skill_growth", {}).get(related_attr)
            if growth_entry:
                growth_bonus = growth_entry.get("bonus", 0)

        rule = self.script.get("settings", {}).get("check_rule", "default")
        atmo = self.current_state.get("time_atmosphere", {})
        light = atmo.get("light_level", "bright")
        bonus_dice = 0
        penalty_dice = 0

        if related_attr:
            stealth_kw = ("敏捷", "潜行", "隐蔽", "偷", "dexterity", "stealth")
            visual_kw = ("智力", "观察", "感知", "搜索", "perception")
            r = related_attr.lower()
            if rule == "brp":
                if light == "dark":
                    if any(k in r for k in stealth_kw):
                        bonus_dice += 1
                    elif any(k in r for k in visual_kw):
                        penalty_dice += 1
                elif light == "dim":
                    if any(k in r for k in stealth_kw):
                        bonus_dice += 1
            else:
                if light == "dark":
                    if any(k in r for k in stealth_kw):
                        growth_bonus += 5
                    elif any(k in r for k in visual_kw):
                        growth_bonus -= 5
                elif light == "dim":
                    if any(k in r for k in stealth_kw):
                        growth_bonus += 3

        # BRP 惩罚骰：负面持续状态（受伤、中毒等）
        if rule == "brp":
            _neg_kw = ("受伤", "重伤", "中毒", "眩晕", "恐惧", "疲惫", "疲劳", "虚弱")
            active_ps = self.current_state.get("active_persistent_states", [])
            dn = self.current_state.get("display_names", {})
            for sid in active_ps:
                name = dn.get(sid, sid)
                if any(k in name for k in _neg_kw):
                    penalty_dice += 1
                    break

        da = self.current_state.get("difficulty_awareness", {})
        adj = da.get("adjustment", "neutral")
        if adj == "ease":
            growth_bonus += 4
        elif adj == "challenge":
            growth_bonus -= 3

        # 检定势头：同属性连续成功/失败给予加成/惩罚
        momentum = self.current_state.get("check_momentum", {})
        if related_attr:
            attr_mom = momentum.get(related_attr, {})
            if self.turn_number - attr_mom.get("last_turn", 0) > 3:
                attr_mom = {"streak": 0, "last_turn": 0}
            streak = attr_mom.get("streak", 0)
            if streak >= 2:
                growth_bonus += min(streak * 2, 8)
            elif streak <= -2:
                growth_bonus += max(streak * 2, -8)

        momentum_streak = streak if related_attr else 0

        if rule == "brp":
            result = self._check_brp(difficulty, related_attr, attr_val,
                                   growth_bonus=growth_bonus,
                                   bonus_dice=bonus_dice, penalty_dice=penalty_dice)
        elif rule == "dnd":
            result = self._check_dnd(difficulty, related_attr, attr_val,
                                   prof_bonus=prof_bonus, skill_id=skill_id, growth_bonus=growth_bonus)
        else:
            result = self._check_default(difficulty, related_attr, attr_val, growth_bonus=growth_bonus)

        if momentum_streak:
            result["momentum_streak"] = momentum_streak

        # 更新势头
        if related_attr:
            momentum = self.current_state.setdefault("check_momentum", {})
            entry = momentum.setdefault(related_attr, {"streak": 0, "last_turn": 0})
            outcome = result.get("outcome", "")
            if "success" in outcome:
                entry["streak"] = (entry["streak"] + 1) if entry["streak"] > 0 else 1
            else:
                entry["streak"] = (entry["streak"] - 1) if entry["streak"] < 0 else -1
            entry["last_turn"] = self.turn_number

        return result

    def _check_default(self, difficulty: str, related_attr: str, attr_val: int | None,
                        *, growth_bonus: int = 0) -> dict:
        """默认规则：d100 ≥ threshold 为成功。"""
        base_thresholds = {"easy": 25, "medium": 40, "hard": 60, "extreme": 80}
        threshold = base_thresholds.get(difficulty, 40)
        if attr_val is not None:
            threshold = threshold - int((attr_val - 50) / 10 * 5)
        threshold -= growth_bonus
        threshold = max(10, min(90, threshold))
        rng = getattr(self.dice, "_rng", random)
        roll = rng.randint(1, 100)
        gap = roll - threshold
        if roll >= 95:
            outcome = "critical_success"
            hint = "大成功！行动完美达成，获得额外收益"
        elif roll >= threshold:
            outcome = "success"
            if gap >= 20:
                hint = "轻松成功。行动游刃有余"
            elif gap >= 10:
                hint = "成功。行动顺利完成"
            else:
                hint = "险些成功。行动勉强达成"
        elif roll >= 5:
            outcome = "failure"
            if gap >= -5:
                hint = "差一点就成功了！功亏一篑"
            elif gap >= -20:
                hint = "失败。行动未能达成"
            else:
                hint = "远远不够。行动彻底失败"
        else:
            outcome = "critical_failure"
            hint = "大失败！产生严重负面后果"
        attr_info = f"（{related_attr}={attr_val}）" if related_attr and attr_val is not None else ""
        return {
            "roll": roll, "threshold": threshold, "difficulty": difficulty,
            "related_attribute": related_attr, "attr_value": attr_val,
            "gap": gap, "outcome": outcome, "rule": "default",
            "narrative_hint": f"{hint}{attr_info}",
        }

    def _check_brp(self, difficulty: str, related_attr: str, attr_val: int | None,
                    *, growth_bonus: int = 0,
                    bonus_dice: int = 0, penalty_dice: int = 0) -> dict:
        """BRP/CoC 7e 规则：d100 ≤ 技能值为成功。

        难度缩放：普通=原值，困难=½，极难=⅕。
        简单难度给一颗奖励骰而非缩放。
        奖励骰/惩罚骰：额外投十位骰，取最有利/最不利的。
        """
        skill = (attr_val if attr_val is not None else 50) + growth_bonus
        scale = {"easy": 1.0, "medium": 1.0, "hard": 0.5, "extreme": 0.2}
        effective = max(1, int(skill * scale.get(difficulty, 1.0)))
        if difficulty == "easy":
            bonus_dice += 1

        # 奖励骰与惩罚骰互相抵消
        net = bonus_dice - penalty_dice
        extra = abs(net)

        rng = getattr(self.dice, "_rng", random)
        units = rng.randint(0, 9)
        tens_rolls = [rng.randint(0, 9) for _ in range(1 + extra)]
        if net > 0:
            chosen_tens = min(tens_rolls)
        elif net < 0:
            chosen_tens = max(tens_rolls)
        else:
            chosen_tens = tens_rolls[0]

        roll = chosen_tens * 10 + units
        if roll == 0:
            roll = 100

        # CoC 7e 大成功/大失败判定
        if roll == 1:
            outcome = "critical_success"
            hint = "大成功！决定性的极限发挥"
        elif roll <= effective:
            outcome = "success"
            margin = effective - roll
            if margin >= 20:
                hint = "轻松成功。技巧游刃有余"
            elif margin >= 5:
                hint = "成功。顺利完成"
            else:
                hint = "险些成功。刚好在能力范围内"
        elif ((attr_val or 50) < 50 and roll >= 96) or roll == 100:
            outcome = "critical_failure"
            hint = "大失败！灾难性的失误"
        else:
            outcome = "failure"
            overshoot = roll - effective
            if overshoot <= 10:
                hint = "差一点就成功了"
            elif overshoot <= 30:
                hint = "失败。超出能力范围"
            else:
                hint = "远远不够。完全力不从心"

        attr_info = f"（{related_attr}={skill}）" if related_attr else ""
        dice_info = {}
        if net != 0:
            dice_info = {
                "type": "bonus" if net > 0 else "penalty",
                "tens_rolls": tens_rolls,
                "units": units,
                "chosen_tens": chosen_tens,
            }
        return {
            "roll": roll, "threshold": effective, "difficulty": difficulty,
            "related_attribute": related_attr, "attr_value": attr_val,
            "outcome": outcome, "rule": "brp",
            "narrative_hint": f"{hint}{attr_info}",
            "dice_info": dice_info,
        }

    def _check_dnd(self, difficulty: str, related_attr: str, attr_val: int | None,
                   *, prof_bonus: int = 0, skill_id: str | None = None,
                   growth_bonus: int = 0) -> dict:
        """D&D规则：d20 + 修正值 + 熟练加值 ≥ DC 为成功。"""
        mod = (attr_val - 50) // 5 if attr_val is not None else 0
        mod += prof_bonus + growth_bonus
        dc_map = {"easy": 8, "medium": 12, "hard": 16, "extreme": 20}
        dc = dc_map.get(difficulty, 12)
        rng = getattr(self.dice, "_rng", random)
        roll = rng.randint(1, 20)
        total = roll + mod
        if roll == 20:
            outcome = "critical_success"
            hint = "天命20！完美发挥，获得额外收益"
        elif roll == 1:
            outcome = "critical_failure"
            hint = "天命1！灾难性失误"
        elif total >= dc:
            outcome = "success"
            margin = total - dc
            if margin >= 8:
                hint = "轻松成功。游刃有余"
            elif margin >= 3:
                hint = "成功。顺利完成"
            else:
                hint = "险些成功。勉强通过"
        else:
            outcome = "failure"
            shortfall = dc - total
            if shortfall <= 3:
                hint = "差一点就成功了"
            elif shortfall <= 8:
                hint = "失败。能力不足"
            else:
                hint = "远远不够。完全无法达成"
        mod_str = f"+{mod}" if mod >= 0 else str(mod)
        prof_str = f"(含熟练+{prof_bonus})" if prof_bonus else ""
        skill_name = ""
        if skill_id and self.class_registry:
            sd = self.class_registry.get_skill(skill_id)
            if sd:
                skill_name = sd.get("name", "")
        attr_info = f"（{skill_name or related_attr}{mod_str}{prof_str}）" if related_attr else ""
        return {
            "roll": roll, "modifier": mod, "total": total, "threshold": dc,
            "difficulty": difficulty, "related_attribute": related_attr,
            "attr_value": attr_val, "outcome": outcome, "rule": "dnd",
            "skill_id": skill_id, "proficiency_bonus": prof_bonus,
            "narrative_hint": f"{hint}{attr_info}",
        }

    def _apply_skill_growth(self, check_result: dict):
        """Award XP to the attribute used in a skill check and handle level-ups."""
        attr_name = check_result.get("related_attribute")
        if not attr_name:
            return
        outcome = check_result.get("outcome", "")
        xp_map = {
            "critical_success": 20,
            "success": 10,
            "failure": 5,
            "critical_failure": 15,
        }
        xp_gain = xp_map.get(outcome, 5)
        growth = self.current_state.setdefault("skill_growth", {})
        entry = growth.setdefault(attr_name, {"xp": 0, "level": 0, "bonus": 0})
        entry["xp"] += xp_gain
        old_level = entry["level"]
        # Level N -> N+1 costs N*50 XP (level 0->1 = 50, 1->2 = 50, etc.), cap at 5
        while entry["level"] < 5:
            needed = max(1, entry["level"]) * 50 if entry["level"] > 0 else 50
            if entry["xp"] >= needed:
                entry["xp"] -= needed
                entry["level"] += 1
                entry["bonus"] = entry["level"] * 2
            else:
                break
        if entry["level"] > old_level:
            self._record_narrative_callback(
                f"你的{attr_name}技能因反复磨练提升到了{entry['level']}级",
                ["skill_growth", attr_name], "medium",
            )
        # Inject growth bonus into check_result so prompt_builder can reference it
        check_result["skill_growth_bonus"] = entry["bonus"]
        check_result["skill_level"] = entry["level"]

    def _get_location_travel_time(self, location_id: str) -> str | None:
        """Get travel time for moving to a location."""
        loc = self._location_by_id.get(location_id)
        return loc.get("travel_time") if loc else None

    def _get_choice_time_hint(self, player_action: dict) -> int | None:
        """Extract time_hint from the selected choice and convert to minutes."""
        choice_id = player_action.get("choice_id", "")
        if not choice_id:
            return None
        active_node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not active_node:
            return None
        for c in active_node.get("choices_presented", []):
            if c.get("id") == choice_id and c.get("time_hint"):
                return self._parse_duration_minutes(c["time_hint"])
        return None

    @staticmethod
    def _parse_duration_minutes(duration: str) -> int | None:
        """Parse ISO 8601 duration (PT30M, PT1H, P1D) to minutes."""
        if not isinstance(duration, str):
            return None
        dur = duration.strip()
        m = re.match(r'P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$', dur)
        if m and (m.group(1) or m.group(2) or m.group(3)):
            days = int(m.group(1) or 0)
            hours = int(m.group(2) or 0)
            minutes = int(m.group(3) or 0)
            total = days * 1440 + hours * 60 + minutes
            return total if total > 0 else None
        return None

    def _is_duplicate_location(self, state: dict, new_loc_id: str) -> bool:
        """Check if a new location is semantically duplicate with existing ones."""
        visible = state.get("visible_locations", [])
        if new_loc_id in visible:
            return True
        dn = state.get("display_names", {})
        existing_names = []
        for vid in visible:
            existing_names.append(dn.get(vid, vid))
        loc_def = self._location_by_id.get(new_loc_id)
        if loc_def:
            existing_names.append(loc_def.get("name", new_loc_id))
        new_name = dn.get(new_loc_id, new_loc_id)
        for ename in existing_names:
            if self._location_names_similar(new_name, ename):
                return True
        return False

    @staticmethod
    def _location_names_similar(a: str, b: str) -> bool:
        """Check if two location names are similar enough to be duplicates."""
        if a == b:
            return True
        if not a or not b:
            return False
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        if shorter in longer:
            return True
        # Compare common prefix ratio
        common = 0
        for ca, cb in zip(a, b):
            if ca == cb:
                common += 1
            else:
                break
        min_len = min(len(a), len(b))
        if min_len >= 4 and common >= min_len * 0.7:
            return True
        return False

    def _apply_weather_effects(self):
        """Apply gameplay effects based on current weather."""
        weather = self.current_state.get("current_weather", "")

        # Remove old weather states
        active = self.current_state.get("active_persistent_states", [])
        weather_state_ids = {"weather_extreme", "weather_storm", "weather_rain"}
        self.current_state["active_persistent_states"] = [
            s for s in active if s not in weather_state_ids
        ]

        if not weather:
            return

        # Keyword-based matching instead of exact string match
        if any(k in weather for k in ("极端", "灾")):
            self.current_state["active_persistent_states"].append("weather_extreme")
        elif any(k in weather for k in ("暴雨", "大雪", "暴风", "台风", "冰雹")):
            self.current_state["active_persistent_states"].append("weather_storm")
        elif any(k in weather for k in ("阴雨", "小雨", "雨", "雪", "雾")):
            self.current_state["active_persistent_states"].append("weather_rain")

    def _check_attribute_thresholds(self, state_changes: list[dict]) -> list[dict]:
        """Check if any attribute crossed a threshold after state changes."""
        events = []
        pc = self.script.get("player_character", {})
        attr_rules = pc.get("attributes", {})

        for change in state_changes:
            target = change.get("target", "")
            # Extract attribute name from target like "player.attributes.health" or "player.health"
            attr_name = target.split(".")[-1]

            rule = attr_rules.get(attr_name, {})
            if not isinstance(rule, dict):
                continue

            thresholds = rule.get("thresholds", [])
            old_val = change.get("old", 0)
            new_val = change.get("new", 0)
            if not isinstance(old_val, (int, float)):
                old_val = 0
            if not isinstance(new_val, (int, float)):
                new_val = 0

            for th in thresholds:
                th_val = th.get("value", 0)
                th_dir = th.get("direction", "below")

                # Check if the threshold was crossed
                if th_dir == "below" and old_val > th_val and new_val <= th_val:
                    events.append({
                        "attribute": attr_name,
                        "direction": "below",
                        "threshold": th_val,
                        "value": new_val,
                        "description": th.get("description", f"{attr_name}降至危险水平"),
                        "activate_state": th.get("activate_state"),
                    })
                    if th.get("activate_state"):
                        active = self.current_state.setdefault("active_persistent_states", [])
                        if th["activate_state"] not in active:
                            active.append(th["activate_state"])

                elif th_dir == "below" and old_val <= th_val and new_val > th_val:
                    if th.get("activate_state"):
                        active = self.current_state.get("active_persistent_states", [])
                        if th["activate_state"] in active:
                            active.remove(th["activate_state"])
                            events.append({
                                "attribute": attr_name,
                                "direction": "above",
                                "threshold": th_val,
                                "value": new_val,
                                "description": th.get("description", f"{attr_name}恢复正常"),
                                "deactivate_state": th["activate_state"],
                            })

                elif th_dir == "above" and old_val < th_val and new_val >= th_val:
                    events.append({
                        "attribute": attr_name,
                        "direction": "above",
                        "threshold": th_val,
                        "value": new_val,
                        "description": th.get("description", f"{attr_name}达到高水平"),
                        "activate_state": th.get("activate_state"),
                    })
                    if th.get("activate_state"):
                        active = self.current_state.setdefault("active_persistent_states", [])
                        if th["activate_state"] not in active:
                            active.append(th["activate_state"])

                elif th_dir == "above" and old_val >= th_val and new_val < th_val:
                    if th.get("activate_state"):
                        active = self.current_state.get("active_persistent_states", [])
                        if th["activate_state"] in active:
                            active.remove(th["activate_state"])
                            events.append({
                                "attribute": attr_name,
                                "direction": "below",
                                "threshold": th_val,
                                "value": new_val,
                                "description": th.get("description", f"{attr_name}不再达标"),
                                "deactivate_state": th["activate_state"],
                            })

        return events

    @staticmethod
    def _merge_legacy_events_into_tree(tree_def: dict, script: dict):
        """Convert script triggers and dynamic_events into story tree nodes."""
        triggers = script.get("triggers", [])
        dynamic_events = script.get("dynamic_events", [])
        if not triggers and not dynamic_events:
            return

        tree_def.setdefault("trees", [])

        if triggers:
            sys_tree = {"id": "__system_triggers", "name": "系统触发器", "icon": "scroll", "nodes": []}
            for i, t in enumerate(triggers):
                effects = {}
                action = t.get("action", "")
                params = t.get("params", {})
                if action == "set_var":
                    effects["set_var"] = [params]
                elif action == "inject_prompt":
                    effects["inject_prompt"] = params.get("text", "")
                elif action == "activate_lore":
                    effects["activate_lore"] = [params.get("entry_id", "")]
                elif action == "deactivate_lore":
                    effects["deactivate_lore"] = [params.get("entry_id", "")]
                elif action == "notify":
                    effects["notify"] = params.get("message", "")
                sys_tree["nodes"].append({
                    "id": t.get("id", f"__trigger_{i}"),
                    "name": t.get("name", f"trigger_{i}"),
                    "type": "trigger",
                    "event": t.get("event", ""),
                    "condition": t.get("condition", ""),
                    "effects": effects,
                })
            if sys_tree["nodes"]:
                tree_def["trees"].append(sys_tree)

        if dynamic_events:
            de_tree = {"id": "__dynamic_events", "name": "动态事件", "icon": "fire", "nodes": []}
            for de in dynamic_events:
                conditions = de.get("conditions", [])
                cond_parts = []
                for c in conditions:
                    path = c.get("path", "")
                    op = c.get("op", "==")
                    val = c.get("value", "")
                    cond_parts.append(f"{path} {op} {val}")
                condition_str = " AND ".join(cond_parts) if cond_parts else ""

                effects = {}
                for eff in de.get("effects", []):
                    etype = eff.get("type", "")
                    if etype == "state_change":
                        effects.setdefault("set_var", []).append({
                            "var_id": eff.get("target", ""),
                            "op": eff.get("op", "set"),
                            "value": eff.get("value"),
                        })
                    elif etype == "activate_state":
                        effects.setdefault("activate_state", []).append(eff.get("id", ""))
                    elif etype == "narrative_callback":
                        effects["narrative_callback"] = {
                            "text": eff.get("text", ""),
                            "priority": eff.get("priority", "medium"),
                        }

                fire_evts = de.get("fire_events", [])
                if fire_evts:
                    effects["fire_events"] = fire_evts

                chain_unlock = []
                for chain in de.get("chain_events", []):
                    chain_unlock.append(chain.get("event_id", ""))

                de_node = {
                    "id": de.get("id", ""),
                    "name": de.get("name", de.get("id", "")),
                    "description": de.get("description", ""),
                    "type": "periodic",
                    "condition": condition_str,
                    "cooldown": de.get("cooldown", 5),
                    "weight": de.get("weight", 10),
                    "repeatable": True,
                    "effects": effects,
                    "on_complete_unlock": chain_unlock if chain_unlock else [],
                }
                if de.get("related_npcs"):
                    de_node["related_npcs"] = de["related_npcs"]
                if de.get("related_orgs"):
                    de_node["related_orgs"] = de["related_orgs"]
                de_tree["nodes"].append(de_node)
            if de_tree["nodes"]:
                tree_def["trees"].append(de_tree)

    def _apply_story_tree_result(self, result):
        """Apply story tree effects to game state."""
        from engine.story_tree import StoryTreeResult
        if not isinstance(result, StoryTreeResult):
            return
        target = self.current_state
        self._apply_effects_and_lore(
            effects=result.effects,
            lore_activations=result.lore_activations,
            lore_deactivations=result.lore_deactivations,
            lore_additions=result.lore_additions,
            lore_updates=result.lore_updates,
            lore_removals=result.lore_removals,
            game_events=result.game_events,
            narrative_callbacks=result.narrative_callbacks,
            state_activations=result.state_activations,
            target=target,
            entry_type="story_tree",
        )

        for effect in result.effects:
            if effect.get("action") == "unlock_node":
                nid = effect["params"]["node_id"]
                sts = target.setdefault("story_tree_state", {})
                ul = set(sts.get("unlocked", []))
                ul.add(nid)
                sts["unlocked"] = list(ul)

        self._sync_node_lorebook(result.newly_completed, result.newly_active)

    def _fire_event_dual(self, event_name: str, state: dict | None = None):
        """Fire an event through both event_engine and story_tree_engine."""
        st = state or self.current_state
        if self.event_engine:
            ee_r = self.event_engine.fire_event(event_name, st, condition_eval=self._evaluate_condition)
            self._apply_event_result(ee_r)
        if self.story_tree_engine:
            self.story_tree_engine.fire_event(event_name, st, condition_eval=self._evaluate_condition)
        if self.trigger_engine and self.trigger_engine.triggers:
            actions = self.trigger_engine.fire(event_name, st)
            if actions:
                effects = self.trigger_engine.execute_actions(actions, st)
                self._apply_trigger_effects(effects)

    def _apply_trigger_effects(self, effects: dict):
        """Apply side-effects returned by TriggerEngine.execute_actions."""
        for text in effects.get("inject_prompts", []):
            self._record_narrative_callback(text, tags=["trigger"], priority="high")
        for entry_id in effects.get("lore_activations", []):
            self.prompt_builder.lorebook.update_entry_enabled(entry_id, True)
        for entry_id in effects.get("lore_deactivations", []):
            self.prompt_builder.lorebook.update_entry_enabled(entry_id, False)
        for entry_id in effects.get("lore_reveals", []):
            self.prompt_builder.lorebook.update_entry_enabled(entry_id, True)

    def _check_quest_keywords(self, text: str):
        """Check active quests in both engines for keyword-based completion."""
        text_lower = text.lower()
        if self.event_engine:
            es = self.current_state.get("events", {})
            for qid, qev in list(self.event_engine.events.items()):
                if qev.category != "quest":
                    continue
                if es.get(qid, {}).get("status") != "active":
                    continue
                kws = qev.completion_keywords or qev.metadata.get("completion_keywords", [])
                if kws and any(kw.lower() in text_lower for kw in kws):
                    r = self.event_engine.complete_quest(self.current_state, qid)
                    self._apply_event_result(r)
        if self.story_tree_engine:
            sts = self.current_state.get("story_tree_state", {})
            for nid in list(sts.get("active", [])):
                node = self.story_tree_engine._nodes.get(nid)
                if not node or node.get("type") != "quest":
                    continue
                kws = node.get("completion_keywords", [])
                if kws and any(kw.lower() in text_lower for kw in kws):
                    r = self.story_tree_engine.complete_quest(self.current_state, nid)
                    self._apply_story_tree_result(r)

    def _apply_event_result(self, result, state=None):
        """Apply unified EventResult to game state (replaces _apply_story_tree_result for EventEngine)."""
        if not isinstance(result, EventResult):
            return
        target = state if state is not None else self.current_state

        self._apply_effects_and_lore(
            effects=result.effects,
            lore_activations=result.lore_activations,
            lore_deactivations=result.lore_deactivations,
            lore_additions=result.lore_additions,
            lore_updates=result.lore_updates,
            lore_removals=result.lore_removals,
            lore_reveals=getattr(result, "lore_reveals", []),
            game_events=result.game_events,
            narrative_callbacks=result.narrative_callbacks,
            state_activations=result.state_activations,
            state_deactivations=getattr(result, "state_deactivations", []),
            target=target,
            entry_type="event_generated",
        )

        for effect in result.effects:
            if effect.get("action") == "unlock_node":
                nid = effect["params"].get("node_id", "")
                if nid:
                    es = target.setdefault("events", {})
                    es.setdefault(nid, {})["status"] = "unlocked"
                    sts = target.setdefault("story_tree_state", {})
                    ul = set(sts.get("unlocked", []))
                    ul.add(nid)
                    sts["unlocked"] = list(ul)

        for sync in result.lorebook_sync:
            action = sync.get("action", "")
            entry_id = sync.get("entry_id", "")
            if action == "remove" and entry_id:
                self.prompt_builder.lorebook.remove_entry(entry_id)
            elif action == "add" and sync.get("entry"):
                self.prompt_builder.lorebook.add_entries([sync["entry"]])
            elif action == "update" and entry_id:
                self.prompt_builder.lorebook.update_entry(
                    entry_id, content=sync.get("content", ""), keys=sync.get("keys"),
                )

        if result.imminent_warnings:
            scene = target.setdefault("scene_details", {})
            existing_tension = scene.get("pending_tension", "")
            warning_text = "；".join(result.imminent_warnings)
            if existing_tension:
                scene["pending_tension"] = f"{existing_tension}；{warning_text}"
            else:
                scene["pending_tension"] = warning_text

        if result.deadline_results:
            target.setdefault("_deadline_results", []).extend(result.deadline_results)
            for dr in result.deadline_results:
                if dr.get("outcome") != "expired":
                    continue
                dl_id = dr.get("id", "")
                ev = self.event_engine.events.get(dl_id)
                if not ev or not ev.metadata.get("is_promise"):
                    continue
                direction = ev.metadata.get("direction", "")
                npc_id = ev.metadata.get("npc_id", "")
                npc_name = ev.metadata.get("npc_name", "")
                content = ev.metadata.get("content", "")
                if direction == "player_to_npc" and npc_id:
                    witnesses = ev.metadata.get("witnesses", [])
                    known_by = list(set([npc_id] + witnesses))
                    network = target.setdefault("information_network", [])
                    network.append({
                        "id": f"broken_prm_{self.turn_number}_{npc_id}",
                        "origin_turn": self.turn_number,
                        "fact": f"玩家未兑现对{npc_name}的承诺「{content[:30]}」",
                        "known_by": known_by,
                        "spread_chance": 0.5, "distortion": 0, "max_spread": 5,
                        "tags": ["broken_promise", "social", npc_id],
                    })
                    self._record_narrative_callback(
                        f"{npc_name}想起你未兑现的承诺「{content[:20]}」",
                        ["promise", npc_id], priority="high",
                    )
                elif direction == "npc_to_player" and npc_name:
                    self._record_narrative_callback(
                        f"{npc_name}之前安排的「{content[:20]}」已到时间",
                        ["npc_appointment", npc_id], priority="high",
                    )

        if result.expired_consequences:
            target["expired_consequences"] = [
                {"description": ec.get("description", "")} for ec in result.expired_consequences
            ]

        self._sync_node_lorebook(result.newly_completed, result.newly_active, target=target)

    def _apply_effects_and_lore(
        self, *, effects, lore_activations, lore_deactivations,
        lore_additions, lore_updates, lore_removals,
        lore_reveals=(),
        game_events, narrative_callbacks, state_activations,
        state_deactivations=(), target=None, entry_type="event_generated",
    ):
        """Shared effect processing for StoryTreeResult and EventResult."""
        target = target or self.current_state
        for effect in effects:
            action = effect.get("action", "")
            params = effect.get("params", {})
            if not isinstance(params, dict):
                params = {}
            if action == "set_var":
                var_id = params.get("var_id") or params.get("target", "")
                op = params.get("op", "set")
                value = params.get("value")
                if "." in var_id:
                    self.state_manager.apply_changes(
                        target, [{"target": var_id, "op": op, "value": value}], inplace=True,
                    )
                else:
                    self.script_variables.apply_op(target, var_id, op, value)
            elif action == "unlock_location":
                loc_id = params.get("location_id", "")
                if loc_id:
                    visible = target.setdefault("visible_locations", [])
                    if loc_id not in visible:
                        visible.append(loc_id)
            elif action == "reveal_npc":
                npc_id = params.get("npc_id", "")
                if npc_id:
                    npcs = target.setdefault("npcs", {})
                    if npc_id not in npcs:
                        npcs[npc_id] = {}
                    npcs[npc_id]["known"] = True
            elif action == "set_reputation":
                fid = params.get("faction_id", "")
                if fid:
                    rep = target.setdefault("faction_reputation", {})
                    entry = rep.setdefault(fid, {"value": 50})
                    op = params.get("op", "add")
                    val = params.get("value", 0)
                    if op == "set":
                        entry["value"] = max(0, min(100, val))
                    else:
                        entry["value"] = max(0, min(100, entry.get("value", 50) + val))
                    entry["title"] = self._reputation_title(entry["value"])

            elif action == "schedule_override":
                npc_id = params.get("npc_id", "")
                if npc_id:
                    overrides = target.setdefault("npc_schedule_overrides", {})
                    npc_ov = overrides.setdefault(npc_id, [])
                    entry = {
                        "time_range": params.get("time_range", ""),
                        "location": params.get("location", ""),
                        "activity": params.get("activity", ""),
                        "priority": params.get("priority", 100),
                    }
                    if params.get("expires"):
                        entry["expires"] = params["expires"]
                    if entry["time_range"] and entry["location"]:
                        npc_ov.append(entry)

        for eid in lore_activations:
            self.prompt_builder.lorebook.update_entry_enabled(eid, True)
        for eid in lore_deactivations:
            self.prompt_builder.lorebook.update_entry_enabled(eid, False)

        if lore_additions:
            lore_additions = [e for e in lore_additions if isinstance(e, dict)]
            for entry_data in lore_additions:
                entry_data.setdefault("enabled", True)
                entry_data.setdefault("position", "after_world")
                entry_data.setdefault("priority", 80)
                entry_data.setdefault("scan_depth", 3)
                entry_data.setdefault("entry_type", entry_type)
            self.prompt_builder.lorebook.add_entries(lore_additions)
            if self.vector_memory:
                batch = [
                    (e["id"], e["content"],
                     {"entry_type": e.get("entry_type", entry_type), "comment": e.get("comment", "")})
                    for e in lore_additions
                    if e.get("content") and len(e["content"]) >= 20
                ]
                if batch:
                    self._schedule_background_task(self._async_lorebook_vector_sync(batch))

        for upd in lore_updates:
            lid = upd.get("id", "")
            if lid:
                self.prompt_builder.lorebook.update_entry(
                    lid, content=upd.get("content", ""), keys=upd.get("keys"),
                )

        for rid in lore_removals:
            self.prompt_builder.lorebook.remove_entry(rid)
            if self.vector_memory:
                self.vector_memory.remove_lorebook(rid)

        for rid in lore_reveals:
            discovered = target.setdefault("pc_discovered_lore", [])
            if rid not in discovered:
                discovered.append(rid)

        for ev in game_events:
            self._fire_event_dual(ev, target)

        for cb in narrative_callbacks:
            self._record_narrative_callback(
                cb.get("text", ""), tags=["event"], priority=cb.get("priority", "medium"),
            )

        for sid in state_activations:
            aps = target.setdefault("active_persistent_states", [])
            if sid not in aps:
                aps.append(sid)

        for sid in state_deactivations:
            aps = target.get("active_persistent_states", [])
            if sid in aps:
                aps.remove(sid)

    def _sync_node_lorebook(self, newly_completed, newly_active, target=None):
        """Sync completed/active nodes to lorebook and vector memory."""
        target = target or self.current_state
        for node in newly_completed:
            ntype = node.get("type", "auto")
            name = node.get("name", node.get("id", ""))
            nid = node.get("id", "")
            is_internal = nid.startswith("_bp_") or nid.startswith("_dyn_")
            if ntype == "choice":
                chosen = ""
                for c in (node.get("choices") or []):
                    if c.get("chosen"):
                        chosen = c.get("label", "")
                        break
                text = f"剧情树选择「{name}」" + (f"→ {chosen}" if chosen else "")
                self._record_narrative_callback(text, ["story_tree", "choice"], priority="high")
            elif ntype == "quest":
                self._record_narrative_callback(
                    f"完成任务「{name}」", ["story_tree", "quest"], priority="high",
                )
            elif ntype in ("story", "auto", "timed") and not is_internal:
                self._record_narrative_callback(
                    f"剧情节点「{name}」完成", ["story_tree"], priority="low",
                )

            desc = node.get("description", name)
            if nid.startswith("__trigger_") or not desc:
                continue
            self.prompt_builder.lorebook.remove_entry(f"_st_active_{nid}")
            if is_internal:
                continue
            keywords = [name]
            for npc_id in node.get("related_npcs", []):
                keywords.append(self._get_npc_or_org_name(npc_id))
            for org_id in node.get("related_orgs", []):
                keywords.append(self._get_npc_or_org_name(org_id))
            driver = node.get("_bp_driver", "")
            if driver:
                keywords.append(self._get_npc_or_org_name(driver))
            keywords.extend(self._extract_name_keywords(desc))
            keywords = [k for k in keywords if k]
            entry = {
                "id": f"_st_{nid}",
                "keys": keywords,
                "content": f"[已发生·第{self.turn_number}回合] {desc}",
                "entry_type": "story_event",
                "priority": 85,
                "position": "after_world",
                "enabled": True,
                "scan_depth": 3,
            }
            self.prompt_builder.lorebook.add_entries([entry])
            dynamic = target.setdefault("dynamic_lorebook", [])
            dynamic.append(entry)
            _related_ids = set(node.get("related_npcs", []) + node.get("related_orgs", []))
            if _related_ids:
                for _le in self.prompt_builder.lorebook.entries:
                    if _le.entry_type in ("npc_profile", "event_context", "npc_relationship"):
                        if any(k in _related_ids for k in _le.keys):
                            _le.priority = max(_le.priority - 15, 10)

        for node in newly_active:
            nid = node.get("id", "")
            name = node.get("name", nid)
            desc = node.get("description", name)
            if not desc:
                continue
            if nid.startswith("_bp_") or nid.startswith("_dyn_"):
                continue
            keywords = [name]
            for npc_id in node.get("related_npcs", []):
                keywords.append(self._get_npc_or_org_name(npc_id))
            for org_id in node.get("related_orgs", []):
                keywords.append(self._get_npc_or_org_name(org_id))
            keywords.extend(self._extract_name_keywords(desc))
            keywords = [k for k in keywords if k]
            entry = {
                "id": f"_st_active_{nid}",
                "keys": keywords,
                "content": f"[进行中] {desc}",
                "entry_type": "story_active",
                "priority": 80,
                "position": "after_world",
                "enabled": True,
                "scan_depth": 3,
            }
            self.prompt_builder.lorebook.add_entries([entry])

        if self.vector_memory and newly_completed:
            for node in newly_completed:
                nid = node.get("id", "")
                if nid.startswith("__trigger_"):
                    continue
                name = node.get("name", nid)
                desc = node.get("description", "")
                if not desc:
                    continue
                ms_text = f"[剧情里程碑·第{self.turn_number}回合] {name}: {desc}"
                ms_meta = {
                    "turn_number": self.turn_number,
                    "doc_type": "milestone",
                    "node_id": nid,
                    "game_time": target.get("game_time", ""),
                }
                self._schedule_background_task(
                    self._async_vector_store(f"ms_{nid}", ms_text, ms_meta),
                )

    def _check_milestones(self) -> tuple[list[dict], list[dict]]:
        """Check if any milestones have been achieved. Returns (newly_achieved, reward_changes)."""
        milestones = self.script.get("milestones", [])
        if not milestones:
            return [], []

        achieved = self.current_state.get("achieved_milestones", [])
        newly_achieved = []
        reward_changes = []

        for ms in milestones:
            ms_id = ms.get("id", "")
            if ms_id in achieved:
                continue

            condition = ms.get("condition", "")
            if not condition:
                continue

            if self._evaluate_condition(condition):
                newly_achieved.append(ms)
                achieved.append(ms_id)

                # Apply milestone rewards
                rewards = ms.get("rewards", [])
                if rewards:
                    self.current_state, log = self.state_manager.apply_changes(
                        self.current_state, rewards, inplace=True
                    )
                    reward_changes.extend(log)

        self.current_state["achieved_milestones"] = achieved
        return newly_achieved, reward_changes

    def _check_milestone_progress(self) -> list[dict]:
        """Check milestones that are close to being achieved (>=60% of threshold)."""
        milestones = self.script.get("milestones", [])
        if not milestones:
            return []

        achieved = self.current_state.get("achieved_milestones", [])
        progress_hints = []

        for ms in milestones:
            ms_id = ms.get("id", "")
            if ms_id in achieved:
                continue
            condition = ms.get("condition", "")
            if not condition:
                continue
            # Only parse simple numeric conditions (path op value)
            m = re.match(r'([\w.]+)\s*(>=|>|<=|<)\s*(\d+)', condition.strip())
            if not m:
                continue
            path, op, threshold_str = m.group(1), m.group(2), m.group(3)
            threshold = int(threshold_str)
            current = self.state_manager._get_value(self.current_state, path)
            if not isinstance(current, (int, float)):
                continue
            # Calculate progress towards threshold
            if op in (">=", ">"):
                if threshold == 0:
                    continue
                progress = current / threshold
            elif op in ("<=", "<"):
                if current <= threshold:
                    continue  # already met
                # current > threshold: progress = how close to dropping below threshold
                # e.g. "stress <= 50", current=60 → need to drop 10 → progress = threshold/current
                if current == 0:
                    continue
                progress = threshold / current
            else:
                continue
            if 0.6 <= progress < 1.0:
                progress_hints.append({
                    "milestone_id": ms_id,
                    "name": ms.get("name", ms_id),
                    "progress": round(progress * 100),
                    "current": current,
                    "target": threshold,
                })

        return progress_hints

    def _update_adventure_log(
        self,
        player_action: dict,
        parsed: dict,
        triggered_events: list[dict],
        triggered_consequences: list[dict] | None,
        achieved_milestones: list[dict] | None,
        threshold_events: list[dict] | None,
        all_state_changes: list[dict] | None = None,
    ):
        """Update the adventure log with key events from this turn."""
        log = self.current_state.setdefault("adventure_log", [])

        entry = {
            "turn": self.turn_number,
            "time": self.current_state.get("game_time", ""),
            "location": self.current_state.get("player", {}).get("location", ""),
            "events": [],
        }

        # Log player decision
        action_text = player_action.get("text", "")
        if action_text:
            entry["events"].append({
                "type": "decision",
                "text": f"你{action_text}" if player_action.get("type") == "freeform" else f"你选择了: {action_text}",
            })

        # Log triggered events
        for e in triggered_events:
            entry["events"].append({
                "type": "event",
                "text": e.get("description", e.get("event_id", "")),
            })

        # Log consequences
        for c in (triggered_consequences or []):
            entry["events"].append({
                "type": "consequence",
                "text": c.get("description", ""),
            })

        # Log milestones
        for m in (achieved_milestones or []):
            entry["events"].append({
                "type": "milestone",
                "text": f"达成: {m.get('name', m.get('id', ''))}",
            })

        # Log threshold events
        for t in (threshold_events or []):
            entry["events"].append({
                "type": "threshold",
                "text": t.get("description", ""),
            })

        # Log location changes
        if parsed.get("location_change"):
            entry["events"].append({
                "type": "location",
                "text": f"移动至: {parsed['location_change']}",
            })

        # Log new location reveals
        for loc_entry in parsed.get("reveal_locations", []):
            if isinstance(loc_entry, dict):
                loc_display = loc_entry.get("name", loc_entry.get("id", ""))
            else:
                loc_display = self.current_state.get("display_names", {}).get(loc_entry, loc_entry)
            entry["events"].append({
                "type": "discovery",
                "text": f"发现新地点: {loc_display}",
            })

        # Log relationship changes
        for sc in (all_state_changes or []):
            target = sc.get("target", "")
            if "relationships" in target:
                parts = target.split(".")
                # Handle both "player.relationships.guard" and "player.relationships.guard.trust"
                npc_id = parts[2] if len(parts) > 2 else ""
                dimension = parts[3] if len(parts) > 3 else ""
                npc_name = self._get_npc_display_name(npc_id)
                old_val = sc.get("old", 0)
                new_val = sc.get("new", 0)
                if old_val is not None and new_val is not None:
                    diff = (new_val if isinstance(new_val, (int, float)) else 0) - (old_val if isinstance(old_val, (int, float)) else 0)
                    if diff != 0:
                        sign = "+" if diff > 0 else ""
                        dim_label = {"trust": "信任", "affection": "好感", "fear": "畏惧"}.get(dimension, "")
                        reason = f" ({sc.get('reason', '')})" if sc.get("reason") else ""
                        dim_str = f"({dim_label})" if dim_label else ""
                        entry["events"].append({
                            "type": "relationship",
                            "text": f"{npc_name} {dim_str}关系{sign}{diff}{reason}",
                        })

        if entry["events"]:
            log.append(entry)
            if self.vector_memory:
                _HIGH_IMPORTANCE = {"milestone", "consequence", "relationship", "event"}
                important = [ev for ev in entry["events"] if ev.get("type") in _HIGH_IMPORTANCE]
                if important:
                    event_text = " | ".join(ev.get("text", "") for ev in important)
                    loc = entry.get("location", "")
                    try:
                        import asyncio
                        loop = asyncio.get_running_loop()
                        loop.run_in_executor(
                            None, self.vector_memory.add,
                            f"event_{self.turn_number}", event_text,
                            {
                                "turn_number": self.turn_number,
                                "game_time": entry.get("time", ""),
                                "location": loc,
                                "doc_type": "event",
                            },
                        )
                    except Exception:
                        pass

            self._record_location_memory(entry)

        # Chapter condensation: every 25 entries, compress older entries into a summary chapter
        last_condensed = self.current_state.get("_log_last_condensed_turn", 0)
        non_chapter_entries = [e for e in log if e.get("type") != "chapter"]
        if len(non_chapter_entries) >= 25 and (self.turn_number - last_condensed) >= 20:
            # Take the oldest 15 non-chapter entries and condense them
            to_condense = non_chapter_entries[:15]
            first_turn = to_condense[0].get("turn", "?")
            last_turn = to_condense[-1].get("turn", "?")
            first_time = to_condense[0].get("time", "")
            last_time = to_condense[-1].get("time", "")
            # 按事件类型分类提取摘要素材
            decisions = []
            milestones_found = []
            consequences_found = []
            discoveries = []
            relationships = []
            for e in to_condense:
                for ev in e.get("events", []):
                    etype = ev.get("type", "")
                    text = ev.get("text", "")
                    if not text:
                        continue
                    if etype == "milestone":
                        milestones_found.append(text)
                    elif etype == "consequence":
                        consequences_found.append(text)
                    elif etype == "discovery":
                        discoveries.append(text)
                    elif etype == "relationship":
                        relationships.append(text)
                    elif etype == "decision" and len(decisions) < 5:
                        decisions.append(text)
            # 结构化摘要：优先显示里程碑和后果，再补充决策
            summary_parts = []
            if milestones_found:
                summary_parts.append(f"[成就] {'; '.join(milestones_found[:3])}")
            if consequences_found:
                summary_parts.append(f"[后果] {'; '.join(consequences_found[:3])}")
            if discoveries:
                summary_parts.append(f"[发现] {'; '.join(discoveries[:3])}")
            if relationships:
                summary_parts.append(f"[关系] {'; '.join(relationships[:4])}")
            if decisions and len(summary_parts) < 3:
                summary_parts.append(f"[决策] {'; '.join(decisions[:3])}")
            summary_text = " | ".join(summary_parts) if summary_parts else f"回合{first_turn}-{last_turn}的冒险"
            chapter = {
                "type": "chapter",
                "turn_range": f"{first_turn}-{last_turn}",
                "time_range": f"{first_time} ~ {last_time}",
                "summary": summary_text,
                "events": [{"type": "chapter_summary", "text": summary_text}],
                "turn": first_turn,
                "time": first_time,
                "needs_ai_summary": True,
            }
            # Remove condensed entries and prepend chapter
            condensed_ids = set(id(e) for e in to_condense)
            remaining_log = [e for e in log if id(e) not in condensed_ids]
            self.current_state["adventure_log"] = [chapter] + remaining_log
            self.current_state["_log_last_condensed_turn"] = self.turn_number
            log = self.current_state["adventure_log"]

        # Keep log reasonable size, but always preserve milestone entries
        if len(log) > 100:
            milestone_entries = [e for e in log if any(
                ev.get("type") == "milestone" for ev in e.get("events", [])
            )]
            recent_entries = log[-80:]
            # Merge: milestones that were trimmed + recent entries (deduplicate)
            recent_set = set(id(e) for e in recent_entries)
            preserved = [e for e in milestone_entries if id(e) not in recent_set]
            self.current_state["adventure_log"] = preserved + recent_entries

        # --- Narrative callbacks: auto-record key moments for future AI references ---
        player_choice = parsed.get("player_choice") or player_action.get("choice_text", "")
        if player_choice:
            self._record_narrative_callback(
                f"玩家选择了: {player_choice}", ["choice"], priority="medium",
            )
        for m in (achieved_milestones or []):
            self._record_narrative_callback(
                f"达成里程碑「{m.get('name', m.get('id', ''))}」", ["milestone"], priority="high",
            )
        for c in (triggered_consequences or []):
            if c.get("importance", 0.3) >= 0.6:
                self._record_narrative_callback(
                    f"后果触发: {c.get('description', '')}", ["consequence"], priority="high",
                )
        for t in (threshold_events or []):
            if "relationship" in t.get("type", "") or "attitude" in t.get("type", ""):
                self._record_narrative_callback(
                    t.get("description", ""), ["relationship"], priority="medium",
                )

        callbacks = self.current_state.get("narrative_callbacks", [])
        if len(callbacks) > 30:
            self.current_state["narrative_callbacks"] = [
                cb for cb in callbacks if not cb.get("used")
            ][-20:]

    def _record_location_memory(self, log_entry: dict):
        """记录本回合在当前地点发生的重大事件。"""
        loc_id = self.current_state.get("player", {}).get("location", "")
        if not loc_id:
            return
        events = log_entry.get("events", [])
        significant = [e for e in events if e.get("type") in
                       ("consequence", "milestone", "relationship", "event", "combat")]
        if not significant:
            return
        loc_mem = self.current_state.setdefault("location_memory", {})
        loc_entries = loc_mem.setdefault(loc_id, [])
        for ev in significant[:2]:
            loc_entries.append({
                "turn": self.turn_number,
                "text": ev.get("text", "")[:60],
                "type": ev.get("type", ""),
            })
        if len(loc_entries) > 10:
            loc_mem[loc_id] = loc_entries[-8:]

    def _record_narrative_callback(self, text: str, tags: list, priority: str = "medium"):
        if not text:
            return
        callbacks = self.current_state.setdefault("narrative_callbacks", [])
        cb_id = f"cb_{self.turn_number}_{len(callbacks)}"
        callbacks.append({
            "id": cb_id,
            "turn": self.turn_number,
            "text": text[:200],
            "tags": tags,
            "callback_after": 3 if priority == "high" else 5,
            "callback_before": 15 if priority == "high" else 25,
            "used": False,
            "priority": priority,
        })

    def _mark_used_callbacks(self, narrative_text: str):
        callbacks = self.current_state.get("narrative_callbacks", [])
        if not callbacks or not narrative_text:
            return
        text_lower = narrative_text.lower()
        for cb in callbacks:
            if cb.get("used"):
                continue
            turn_diff = self.turn_number - cb.get("turn", 0)
            if turn_diff < cb.get("callback_after", 3):
                continue
            keywords = [w for w in cb.get("text", "").replace(":", " ").split() if len(w) >= 2][:5]
            if any(kw.lower() in text_lower for kw in keywords):
                cb["used"] = True

    def _run_turn_computations(self, story_tree_result, check_result, achieved_milestones):
        """Two-phase turn computations with explicit dependency ordering."""
        # Phase 1: independent computations
        self._evaluate_tone(story_tree_result)
        self._check_npc_goals()
        self._check_org_goals()
        self._check_npc_goal_conflicts()
        self._update_companion_loyalty()
        self._sync_companions()
        self._apply_reputation_attitude_modifier()
        self._check_quest_templates()
        self._compute_xp_and_level({"check_result": check_result, "achieved_milestones": achieved_milestones})
        self._collect_interactables()
        self._compute_time_atmosphere()
        self._compute_difficulty_awareness()
        self._compute_npc_relationship_depth()
        self._compute_discovery_hints()
        # Phase 2: depends on phase 1 (attitude → secrets/interventions, time_atmo → effects_summary)
        self._check_npc_secrets()
        self._check_npc_interventions()
        self._compute_active_effects_summary()

    async def _build_compose_context(self, plot_decision: str, ctx: dict, scope: str = "moderate", *, state: dict | None = None) -> str:
        """Build compose_history for Stage 3, can run in parallel with Stage 2."""
        _st = state if state is not None else self.current_state
        compose_history = ctx.get("base_history_context", "")
        cm = ctx.get("context_memory", "")
        if cm:
            compose_history = (compose_history + "\n\n" + cm).strip() if compose_history else cm
        if scope != "minor":
            _compose_reasoning = ctx.get("recent_reasoning", [])
            if _compose_reasoning:
                reasoning_lines = ["## 前轮决策思路（供参考，保持连贯）"]
                for r in _compose_reasoning:
                    reasoning_lines.append(f"第{r['turn']}回合: {r['reasoning']}")
                compose_history = (compose_history + "\n\n" + "\n".join(reasoning_lines)).strip()
        if scope != "minor" and self.vector_memory and plot_decision:
            fh_hits = await self._stage_rag_query(
                plot_decision[:200], top_k=2, doc_type="turn",
                exclude_turns=[self.turn_number],
                boost_lore_ids=ctx.get("story_lore_ids") or None,
            )
            if fh_hits:
                fh_lines = ["## 相关历史伏笔（确保叙事连贯）"]
                for r in fh_hits:
                    fh_lines.append(f"第{r['turn']}回合: {r['text'][:80]}")
                compose_history = (compose_history + "\n\n" + "\n".join(fh_lines)).strip()
        _sd_compose = ctx.get("stage_directives", {}).get("compose")
        if _sd_compose:
            compose_history = (compose_history + "\n\n## 当前剧情线指令\n" + "\n".join(_sd_compose)).strip()
        if self.story_tree_engine:
            _upcoming = self.story_tree_engine.get_upcoming_nodes(_st, limit=2)
            if _upcoming:
                _hint_lines = ["## 叙事伏笔暗示（自然编入叙事，不要直说）"]
                for _un in _upcoming:
                    _hint_lines.append(f"- {_un.get('name', '')}: {_un.get('description', '')[:80]}")
                compose_history = (compose_history + "\n\n" + "\n".join(_hint_lines)).strip()
        if scope != "minor":
            npc_knowledge = self._build_npc_knowledge_context(ctx, _st)
            if npc_knowledge:
                compose_history = (compose_history + "\n\n" + npc_knowledge).strip()
            causal_ctx = self._build_causal_context(_st)
            if causal_ctx:
                compose_history = (compose_history + "\n\n" + causal_ctx).strip()
        return compose_history

    def _update_dynamic_connections(self, state: dict):
        """Re-evaluate location connection_conditions after world_properties change.

        Locations can define conditional connections:
            {"id": "loc_a", "connections": ["loc_b"],
             "conditional_connections": [
               {"target": "loc_c", "condition": "world_properties.bridge_intact == true"}
             ]}

        When the condition becomes true, the connection is added;
        when false, it's removed. This makes the map reactive to world state.
        """
        conns = state.get("location_connections", {})
        changed = False
        for loc_def in self.script.get("locations", []):
            loc_id = loc_def.get("id", "")
            cc_list = loc_def.get("conditional_connections", [])
            if not loc_id or not cc_list:
                continue
            loc_conns = conns.get(loc_id, [])
            if not isinstance(loc_conns, list):
                loc_conns = list(loc_conns)
            for cc in cc_list:
                target = cc.get("target", "")
                condition = cc.get("condition", "")
                if not target or not condition:
                    continue
                met = self._evaluate_condition(condition)
                if met and target not in loc_conns:
                    loc_conns.append(target)
                    changed = True
                    reverse = conns.get(target, [])
                    if isinstance(reverse, list) and loc_id not in reverse:
                        reverse.append(loc_id)
                        conns[target] = reverse
                elif not met and target in loc_conns:
                    loc_conns.remove(target)
                    changed = True
                    reverse = conns.get(target, [])
                    if isinstance(reverse, list) and loc_id in reverse:
                        reverse.remove(loc_id)
                        conns[target] = reverse
            conns[loc_id] = loc_conns
        if changed:
            state["location_connections"] = conns

    @staticmethod
    def _build_player_profile_hint(state: dict) -> str:
        """Aggregate moral_alignment + pacing + relationship_depths into a
        concise player profile hint for Stage 1 plot_decision prompt.

        Lets the AI adapt narrative tone and NPC reactions to the player's
        established behavioral pattern.
        """
        parts: list[str] = []
        ma = state.get("moral_alignment", {})
        _AXIS = {"mercy_vs_cruelty": ("仁慈", "残忍"),
                 "honesty_vs_deception": ("诚实", "欺诈"),
                 "order_vs_chaos": ("秩序", "混沌")}
        dominant = []
        for axis, (pos, neg) in _AXIS.items():
            val = ma.get(axis, 0)
            if val >= 30:
                dominant.append(pos)
            elif val <= -30:
                dominant.append(neg)
        if dominant:
            parts.append(f"玩家行为倾向: {'/'.join(dominant)}")
        pacing = state.get("pacing_state", {})
        tension = pacing.get("tension", 50)
        trend = pacing.get("trend", "stable")
        _TREND_LABELS = {"rising": "上升", "falling": "下降", "stable": "平稳"}
        parts.append(f"紧张度{tension}/100({_TREND_LABELS.get(trend, trend)})")
        depths = state.get("npc_relationship_depths", {})
        close_npcs = [v.get("label", "") for v in depths.values()
                      if isinstance(v, dict) and v.get("level", 0) >= 3]
        if close_npcs:
            parts.append(f"亲密NPC: {', '.join(close_npcs[:3])}")
        if not parts:
            return ""
        return "## 玩家画像（自适应叙事风格参考）\n" + " | ".join(parts)

    def _build_npc_knowledge_context(self, ctx: dict, state: dict) -> str:
        """Build NPC cognitive asymmetry context from information_network.

        Each present NPC sees a different subset of facts (with possible
        distortion). This lets the AI write NPC dialogue/reactions that
        reflect what each NPC actually knows — not omniscient.
        """
        network = state.get("information_network", [])
        if not network:
            return ""
        present_ids = set(ctx.get("present_npc_ids", []))
        if not present_ids:
            return ""
        _DISTORTION_LABELS = {0: "", 1: "（略有偏差）", 2: "（严重失真）", 3: "（面目全非）"}
        npc_facts: dict[str, list[str]] = {}
        for info in network:
            known_by = set(info.get("known_by", []))
            overlapping = present_ids & known_by
            if not overlapping:
                continue
            fact = info.get("fact", "")
            if not fact:
                continue
            distortion = min(info.get("distortion", 0), 3)
            label = _DISTORTION_LABELS.get(distortion, "")
            for npc_id in overlapping:
                npc_facts.setdefault(npc_id, []).append(f"{fact[:60]}{label}")
        # 注入 NPC 通过 known_by_npcs 知道的 hidden 词条
        if self.prompt_builder.lorebook:
            for npc_id in present_ids:
                for entry in self.prompt_builder.lorebook.entries:
                    if entry.visibility == "hidden" and npc_id in entry.known_by_npcs and entry.enabled:
                        npc_facts.setdefault(npc_id, []).append(f"[机密]{entry.content[:60]}")
        if not npc_facts:
            return ""
        lines = ["## NPC 认知差异（各NPC只知道各自的信息，对话/反应须反映认知差异）"]
        for npc_id, facts in npc_facts.items():
            name = self._get_npc_display_name(npc_id)
            lines.append(f"- {name}知道: {'; '.join(facts[:3])}")
        return "\n".join(lines)

    def _discover_lore(self, entry_id: str, source: str = ""):
        """将词条标记为主角已发现。"""
        discovered = self.current_state.setdefault("pc_discovered_lore", [])
        if entry_id not in discovered:
            discovered.append(entry_id)
            logger.info("Lore discovered: %s (source=%s)", entry_id, source)

    @staticmethod
    def _build_causal_context(state: dict) -> str:
        """Build causal chain context from choice_ripples and memory_echoes.

        Injects into compose prompt so the AI naturally weaves cause-and-effect
        references into the narrative (e.g. "you remember releasing that thief
        at the market — now he blocks your path with a gang").
        """
        parts: list[str] = []
        echoes = state.get("memory_echoes", [])
        if echoes:
            for e in echoes[:2]:
                trigger = e.get("trigger", "")
                memory = e.get("memory", "")
                if trigger and memory:
                    parts.append(f"- 记忆回响：{trigger} → {memory}")
        ripples = state.get("choice_ripples", [])
        if ripples:
            for r in ripples[:2]:
                cause = r.get("cause_action", "")
                event = r.get("current_event", "")
                if cause and event:
                    parts.append(f"- 因果伏线：因「{cause}」→ 今「{event}」")
        if not parts:
            return ""
        header = "## 因果回响（在叙事中自然暗示因果关联，不要生硬点明）"
        return header + "\n" + "\n".join(parts)

    def _compute_compose_feedback(self, narrative: str, ctx: dict):
        """Compute zero-cost feedback on Stage 3 narrative quality for next turn's Stage 1."""
        if not narrative:
            return
        issues = []
        plot_decision = ctx.get("plot_decision", "")
        if plot_decision:
            npc_names = set()
            for nid, nd in self.current_state.get("npcs", {}).items():
                if isinstance(nd, dict) and nd.get("name"):
                    npc_names.add(nd["name"])
            mentioned = [n for n in npc_names if n in plot_decision]
            if mentioned:
                missed = [n for n in mentioned if n not in narrative]
                if len(missed) > len(mentioned) * 0.5:
                    issues.append(f"骨架提及的{','.join(missed[:3])}未在叙事中出现")

        # Cross-turn phantom object detection
        self._track_dangling_clues(plot_decision, issues)

        if issues:
            self.current_state["_compose_feedback"] = "；".join(issues[:5])
        else:
            self.current_state.pop("_compose_feedback", None)

    def _track_dangling_clues(self, plot_decision: str, issues: list[str]):
        """Track objects/clues from [关键事件] and flag those unresolved after 2 turns."""
        import re as _re
        tracker = self.current_state.setdefault("_dangling_clue_tracker", [])
        turn = self.turn_number

        # Extract items from [关键事件] section
        key_event_match = _re.search(r'\[关键事件\]\s*(.+?)(?:\n\[|$)', plot_decision, _re.DOTALL)
        if key_event_match:
            event_text = key_event_match.group(1)
            # Look for noun-like props: quoted items, or items after 发现/获得/出现/看到
            prop_patterns = [
                _re.findall(r'[「「"\'](.*?)[」」"\'"]', event_text),
                _re.findall(r'(?:发现|获得|出现|看到|注意到|拿起|收到)\s*[了]?\s*(.{2,8})', event_text),
            ]
            new_clues = set()
            for matches in prop_patterns:
                for m in matches:
                    clue = m.strip()
                    if clue and len(clue) >= 2:
                        new_clues.add(clue)

            # Register new clues
            for clue in new_clues:
                if not any(c["text"] == clue for c in tracker):
                    tracker.append({"text": clue, "first_turn": turn, "resolved": False})

        # Check resolution: clue is in inventory, lorebook, or events
        known_sources = set()
        for it in self.current_state.get("inventory", []):
            known_sources.add(it.get("item", ""))
        for ev in self.current_state.get("key_events", []):
            known_sources.add(ev.get("event", ""))
        if self.prompt_builder.lorebook:
            for entry in self.prompt_builder.lorebook.entries:
                if entry.enabled:
                    known_sources.add(entry.comment or entry.id)

        for c in tracker:
            if not c["resolved"]:
                if any(c["text"] in src or src in c["text"] for src in known_sources if src):
                    c["resolved"] = True

        # Flag dangling clues (unresolved for 2+ turns)
        dangling = [c for c in tracker if not c["resolved"] and turn - c["first_turn"] >= 2]
        if dangling:
            names = ", ".join(c["text"] for c in dangling[:3])
            issues.append(f"悬空线索（连续2+回合未消化，勿再提及或需给出交代）: {names}")

        # Prune old entries (resolved or older than 5 turns)
        tracker[:] = [c for c in tracker if not c["resolved"] and turn - c["first_turn"] <= 5]

    def _update_pacing_state(self, ctx: dict):
        """Analyze recent turn events to compute narrative pacing tension."""
        tension_delta = 0
        if ctx.get("triggered_consequences"):
            tension_delta += 20
        if ctx.get("achieved_milestones"):
            tension_delta += 15
        dice_dicts = ctx.get("dice_dicts", [])
        for d in dice_dicts:
            dtype = d.get("type", "")
            if any(k in dtype for k in ("combat", "attack", "conflict", "战斗")):
                tension_delta += 15
                break
        if ctx.get("story_tree_updates"):
            st = ctx["story_tree_updates"]
            if hasattr(st, "newly_completed") and st.newly_completed:
                tension_delta += 10
                for node in st.newly_completed:
                    if node.get("type") == "choice":
                        tension_delta += 5
        npc_att = ctx.get("all_state_changes", [])
        big_att = any(
            abs((sc.get("new") or 0) - (sc.get("old") or 0)) >= 15
            for sc in npc_att
            if "relationships" in sc.get("target", "")
        )
        if big_att:
            tension_delta += 10
        if ctx.get("all_state_changes"):
            loc_changes = [sc for sc in ctx["all_state_changes"] if "location" in sc.get("target", "")]
            if loc_changes:
                tension_delta += 5
        if self.story_tree_engine:
            sts = self.current_state.get("story_tree_state", {})
            total = len(self.story_tree_engine._nodes)
            if total > 0:
                completed_count = len(sts.get("completed", []))
                if completed_count / total > 0.7:
                    tension_delta += 15
                if len(sts.get("active", [])) >= 3:
                    tension_delta += 10
        if tension_delta == 0:
            tension_delta = -10
        elif tension_delta <= 5:
            # Minor events: still allow slight decay
            tension_delta -= 5

        pacing = self.current_state.get("pacing_state", {
            "tension": 50, "trend": "stable",
            "consecutive_high": 0, "consecutive_low": 0,
            "history": [],
        })
        old_tension = pacing.get("tension", 50)
        new_tension = max(0, min(100, old_tension + tension_delta))
        history = pacing.get("history", [])
        history.append(new_tension)
        if len(history) > 10:
            history = history[-10:]

        if new_tension >= 70:
            pacing["consecutive_high"] = pacing.get("consecutive_high", 0) + 1
            pacing["consecutive_low"] = 0
        elif new_tension <= 30:
            pacing["consecutive_low"] = pacing.get("consecutive_low", 0) + 1
            pacing["consecutive_high"] = 0
        else:
            # Hysteresis: 只有回到中间区域（40-60）才重置对侧计数器
            if new_tension <= 50:
                pacing["consecutive_high"] = 0
            if new_tension >= 50:
                pacing["consecutive_low"] = 0

        if len(history) >= 3:
            recent = history[-3:]
            if recent[-1] > recent[0] + 10:
                pacing["trend"] = "rising"
            elif recent[-1] < recent[0] - 10:
                pacing["trend"] = "falling"
            else:
                pacing["trend"] = "stable"
        else:
            pacing["trend"] = "stable"

        rec = ""
        if pacing["consecutive_high"] >= 3:
            rec = "建议插入喘息/日常场景，缓解连续紧张"
        elif pacing["consecutive_low"] >= 3:
            rec = "⚠ 节奏警告：连续低紧张回合，本轮必须引入新信息或推进。方式：NPC主动带来消息/旧线索有新发现/外部势力动态影响到玩家/[世界脉搏]中的事态升级"
        pacing["recommendation"] = rec
        pacing["tension"] = new_tension
        pacing["history"] = history
        self.current_state["pacing_state"] = pacing

    def _track_world_pulse(self, ctx: dict):
        """Track world pulse hints from plot_decision; expire stale ones."""
        plot_decision = ctx.get("plot_decision", "")
        pulse_re = re.compile(r'\[世界脉搏\]\s*(.*?)(?:\n\[|$)', re.DOTALL)
        pulses = self.current_state.setdefault("world_pulse_tracker", [])

        # Extract new hints from this turn's plot_decision
        if plot_decision:
            m = pulse_re.search(plot_decision)
            if m:
                text = m.group(1).strip()
                if text and len(text) > 5:
                    pulses.append({
                        "turn": self.turn_number,
                        "text": text[:150],
                        "status": "pending",
                        "expires_at": self.turn_number + random.randint(3, 6),
                    })

        # Expire hints past their individual expiry turn
        expired_texts = []
        for p in pulses:
            if p["status"] == "pending" and self.turn_number >= p.get("expires_at", p["turn"] + 5):
                p["status"] = "expired"
                expired_texts.append(p["text"][:60])

        # Tension reduction for expired hints
        if expired_texts:
            pacing = self.current_state.get("pacing_state", {})
            pacing["tension"] = max(0, pacing.get("tension", 50) - 5 * len(expired_texts))
            self.current_state["pacing_state"] = pacing

        # Store as soft suggestion for Stage 1 (not mandatory)
        if expired_texts:
            existing = self.current_state.get("_pulse_dissolves", [])
            for t in expired_texts:
                existing.append({"text": t, "shown": 0})
            self.current_state["_pulse_dissolves"] = existing[-5:]

        # Keep tracker compact: remove resolved entries older than 10 turns
        pulses[:] = [p for p in pulses if self.turn_number - p["turn"] < 10]

        # Increment shown count on dissolves (skip newly added ones with shown=0)
        dissolves = self.current_state.get("_pulse_dissolves", [])
        if dissolves:
            for d in dissolves:
                if d.get("shown", 0) > 0:
                    d["shown"] = d["shown"] + 1
                else:
                    d["shown"] = 1  # first turn: mark as shown=1, will be injected this turn
            self.current_state["_pulse_dissolves"] = [d for d in dissolves if d["shown"] <= 3]

    def _check_npc_goals(self):
        """Check NPC goal completion conditions and record callbacks."""
        completed_goals = self.current_state.get("completed_npc_goals", [])
        completed_ids = {(g["id"] if isinstance(g, dict) else g) for g in completed_goals}
        goal_progress = self.current_state.get("npc_goal_progress", {})
        all_npc_defs = list(self.script.get("npcs", []))
        npcs_state = self.current_state.get("npcs", {})
        for dyn_id, dyn_st in npcs_state.items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                dyn_goals = dyn_st.get("goals", [])
                if dyn_goals:
                    all_npc_defs.append({
                        "id": dyn_id,
                        "name": dyn_st.get("name", dyn_id),
                        "goals": dyn_goals,
                    })
        for npc_def in all_npc_defs:
            npc_id = npc_def.get("id", "")
            goals = npc_def.get("goals", [])
            if not goals:
                continue
            npc_name = npc_def.get("name", npc_id)
            for goal in goals:
                goal_id = goal.get("id", "")
                full_id = f"{npc_id}:{goal_id}"
                if full_id in completed_ids:
                    continue
                condition = goal.get("condition_met", "")
                condition_met = condition and self._evaluate_condition(condition)
                offscreen_met = goal_progress.get(npc_id, {}).get("progress", 0) >= 100
                if condition_met or offscreen_met:
                    desc = goal.get("description", goal_id)
                    source = "offscreen" if offscreen_met and not condition_met else "condition"
                    completed_goals.append({
                        "id": full_id,
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "description": desc,
                        "turn": self.turn_number,
                        "source": source,
                    })
                    completed_ids.add(full_id)
                    self._record_narrative_callback(
                        f"NPC「{npc_name}」达成目标「{desc}」",
                        ["npc_goal", npc_id], priority="high",
                    )
                    network = self.current_state.setdefault("information_network", [])
                    network.append({
                        "id": f"npc_goal_{npc_id}_{self.turn_number}",
                        "origin_turn": self.turn_number,
                        "fact": f"{npc_name}完成了目标「{desc[:40]}」",
                        "known_by": [npc_id],
                        "spread_chance": 0.5,
                        "distortion": 0,
                        "max_spread": 5,
                        "tags": ["npc_goal", npc_id],
                    })
                    if len(network) > 30:
                        self.current_state["information_network"] = network[-30:]
        self.current_state["completed_npc_goals"] = completed_goals

        if self.event_engine:
            for npc_id, gp_data in goal_progress.items():
                progress = gp_data.get("progress", 0)
                if progress < 70 or progress >= 100:
                    continue
                cons_id = f"_npc_goal_imm_{npc_id}"
                if cons_id in self.event_engine.events:
                    continue
                npc_name = self._get_npc_display_name(npc_id)
                npc_def = self._npc_by_id.get(npc_id, {})
                goals = npc_def.get("goals", [])
                active_goals = [g for g in goals if f"{npc_id}:{g.get('id', '')}" not in completed_ids]
                if not active_goals:
                    continue
                goal_desc = active_goals[0].get("description", "")[:40]
                self.event_engine.add_consequence(self.current_state, {
                    "id": cons_id,
                    "description": f"{npc_name}正在积极推进「{goal_desc}」（进度{progress}%）",
                    "trigger_chance": 0.4,
                    "turns_delay": 2,
                    "max_turns": 6,
                    "importance": "high",
                    "related_npcs": [npc_id],
                    "on_trigger": {
                        "description": f"{npc_name}的计划「{goal_desc}」即将实现，世界将因此改变",
                    },
                })

    def _check_org_goals(self):
        """Check organization goal completion conditions and record callbacks."""
        completed_goals = self.current_state.get("completed_org_goals", [])
        completed_ids = {(g["id"] if isinstance(g, dict) else g) for g in completed_goals}
        for org_def in self.script.get("organizations", []):
            org_id = org_def.get("id", "")
            goals = org_def.get("goals", [])
            if not goals:
                continue
            org_name = org_def.get("name", org_id)
            for goal in goals:
                goal_id = goal.get("id", "")
                full_id = f"{org_id}:{goal_id}"
                if full_id in completed_ids:
                    continue
                condition = goal.get("condition_met", "")
                if not condition:
                    continue
                if self._evaluate_condition(condition):
                    desc = goal.get("description", goal_id)
                    completed_goals.append({
                        "id": full_id,
                        "org_id": org_id,
                        "org_name": org_name,
                        "description": desc,
                        "turn": self.turn_number,
                    })
                    completed_ids.add(full_id)
                    self._record_narrative_callback(
                        f"组织「{org_name}」达成目标「{desc}」",
                        ["org_goal", org_id], priority="high",
                    )
                    self._apply_org_goal_effects(goal, org_id, org_name, desc)
        self.current_state["completed_org_goals"] = completed_goals

    def _apply_org_goal_effects(self, goal: dict, org_id: str, org_name: str, desc: str):
        """Apply on_complete_effects from an organization goal and inject into information_network."""
        effects = goal.get("on_complete_effects", {})
        if effects:
            for sc in effects.get("state_change", []):
                self.current_state, _ = self.state_manager.apply_changes(
                    self.current_state, [sc], inplace=True,
                )
            for loc_id in effects.get("unlock_locations", []):
                if not self._is_duplicate_location(self.current_state, loc_id):
                    self.current_state = self.state_manager.reveal_location(
                        self.current_state, loc_id, inplace=True,
                    )
            for rep in effects.get("set_reputation", []):
                faction = rep.get("faction", "")
                value = rep.get("value")
                if faction and value is not None:
                    factions = self.current_state.setdefault("faction_reputation", {})
                    entry = factions.setdefault(faction, {"value": 50})
                    if isinstance(entry, dict):
                        entry["value"] = value

        network = self.current_state.setdefault("information_network", [])
        known_by = []
        for org in self.script.get("organizations", []):
            if org.get("id") == org_id:
                for member in org.get("members", []):
                    mid = member if isinstance(member, str) else member.get("id", "")
                    if mid:
                        known_by.append(mid)
                break
        network.append({
            "id": f"org_goal_{org_id}_{self.turn_number}",
            "origin_turn": self.turn_number,
            "fact": f"{org_name}达成了目标「{desc[:40]}」",
            "known_by": known_by,
            "spread_chance": 0.6,
            "distortion": 0,
            "max_spread": 6,
            "tags": ["org_goal", org_id],
        })
        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

    # ================================================================
    # Feature #2: Promise/Lie Tracking System
    # ================================================================

    # _check_promise_expirations removed — promises are now deadline events,
    # expiry handled by event_engine._tick_deadlines + deadline_results processing below.

    async def _handle_promise_extraction(self, meta_ctx: dict):
        """Extract promises/appointments from narrative, register as deadline events."""
        route = self.current_state.get("_last_route", {})
        focus_npcs = route.get("focus_npcs", [])
        if not focus_npcs:
            return
        narrative = meta_ctx.get("narrative", "")
        player_action = meta_ctx.get("player_action", {})
        action_text = player_action.get("text", "") if isinstance(player_action, dict) else ""
        if not narrative:
            return

        # 迁移旧 promise_ledger → deadline events
        old_ledger = self.current_state.pop("promise_ledger", None)
        if old_ledger:
            for prm in old_ledger:
                if prm.get("status") != "pending":
                    continue
                npc_id = prm.get("npc_id", "")
                content = prm.get("content", "")
                npc_name = self._get_npc_display_name(npc_id)
                remaining = max(1, prm.get("deadline_turn", self.turn_number + 5) - self.turn_number)
                self.event_engine.add_deadline(self.current_state, {
                    "id": prm.get("id", f"_promise_migrated_{npc_id}"),
                    "description": f"[玩家承诺] 对{npc_name}: {content[:50]}",
                    "turns_remaining": remaining,
                    "on_expire": {"description": f"玩家未兑现对{npc_name}的承诺", "state_changes": [
                        {"target": f"npcs.{npc_id}.attitude_toward_player", "op": "add", "value": -15}
                    ]},
                    "visible": True,
                    "is_promise": True, "direction": "player_to_npc",
                    "npc_id": npc_id, "npc_name": npc_name, "content": content,
                    "is_lie": prm.get("is_lie", False),
                    "witnesses": prm.get("witnesses", []),
                }, self.turn_number)

        # 获取已有承诺 deadline，避免重复提取
        existing_promise_ids = [
            eid for eid, ev in self.event_engine.events.items()
            if ev.category == "deadline" and ev.metadata.get("is_promise")
            and self.current_state.get("events", {}).get(eid, {}).get("status") == "active"
        ]
        existing_descs = [self.event_engine.events[eid].description for eid in existing_promise_ids]

        prompt = (
            f"玩家行动: {action_text}\n"
            f"叙事结果: {narrative[:800]}\n"
            f"在场NPC: {', '.join(focus_npcs)}\n"
            f"已记录的未完成承诺/约定: {existing_descs[-5:] or '无'}\n\n"
            "分析本轮对话中的承诺和约定（双向）：\n"
            "1. 玩家→NPC：承诺、保证、威胁、谎言\n"
            "2. NPC→玩家：指令、约定、邀请、安排（如'明天来值班''今晚有宴'）\n"
            "3. 是否兑现了已记录的承诺\n\n"
            '返回JSON: {"promises":['
            '{"npc_id":"NPC的ID","content":"承诺/约定的具体内容",'
            '"direction":"player_to_npc或npc_to_player",'
            '"is_lie":false,"urgency":"immediate|today|tomorrow|days|weeks"}],'
            '"fulfilled":["已兑现承诺的content关键词"]}\n'
            "无承诺/兑现则返回空列表。只返回JSON。"
        )
        try:
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是对话分析器，提取承诺、约定和指令。只返回JSON。",
                max_tokens=400,
                **self._stage_kwargs("knowledge_graph"),
            )
            data = json.loads(raw.strip().strip("```json").strip("```").strip())
        except Exception:
            return

        present_npcs = self.current_state.get("_last_present_npcs", focus_npcs)
        urgency_to_turns = {
            "immediate": 2, "today": 3, "tomorrow": 5, "days": 10, "weeks": 15,
        }

        for p in data.get("promises", [])[:3]:
            npc_id = p.get("npc_id", "")
            content = p.get("content", "")
            if not npc_id or not content:
                continue
            direction = p.get("direction", "player_to_npc")
            urgency = p.get("urgency", "days")
            turns = urgency_to_turns.get(urgency, 10)

            dl_id = f"_promise_{self.turn_number}_{npc_id}_{len(existing_promise_ids)}"
            npc_name = self._get_npc_display_name(npc_id)

            if direction == "player_to_npc":
                on_expire = {
                    "description": f"玩家未兑现对{npc_name}的承诺「{content[:30]}」",
                    "state_changes": [
                        {"target": f"npcs.{npc_id}.attitude_toward_player", "op": "add", "value": -15}
                    ],
                }
                desc = f"[玩家承诺] 对{npc_name}: {content[:50]}"
            else:
                on_expire = {
                    "description": f"{npc_name}安排的「{content[:30]}」已到时间",
                }
                desc = f"[{npc_name}安排] {content[:50]}"

            self.event_engine.add_deadline(self.current_state, {
                "id": dl_id,
                "description": desc,
                "turns_remaining": turns,
                "on_expire": on_expire,
                "on_complete": {"condition": "", "description": f"承诺已兑现: {content[:30]}"},
                "visible": True,
                "is_promise": True,
                "direction": direction,
                "npc_id": npc_id,
                "npc_name": npc_name,
                "content": content[:100],
                "is_lie": bool(p.get("is_lie")),
                "witnesses": list(present_npcs[:5]),
            }, self.turn_number)

            if p.get("is_lie"):
                network = self.current_state.setdefault("information_network", [])
                network.append({
                    "id": f"lie_{self.turn_number}_{npc_id}",
                    "origin_turn": self.turn_number,
                    "fact": f"玩家对{npc_name}说了谎「{content[:30]}」",
                    "known_by": [npc_id],
                    "spread_chance": 0.3, "distortion": 1, "max_spread": 3,
                    "tags": ["lie", "social", npc_id],
                })

        for keyword in data.get("fulfilled", [])[:3]:
            if not keyword:
                continue
            for eid in existing_promise_ids:
                ev = self.event_engine.events.get(eid)
                if not ev:
                    continue
                if keyword.lower() in ev.description.lower() or keyword.lower() in ev.metadata.get("content", "").lower():
                    es = self.current_state.setdefault("events", {})
                    es.setdefault(eid, {})["status"] = "completed"
                    if ev.metadata.get("direction") == "player_to_npc":
                        prm_npc = ev.metadata.get("npc_id", "")
                        if prm_npc:
                            self.current_state, _ = self.state_manager.apply_changes(
                                self.current_state,
                                [{"target": f"npcs.{prm_npc}.attitude_toward_player",
                                  "op": "add", "value": 10,
                                  "reason": f"兑现承诺: {ev.metadata.get('content', '')[:20]}"}],
                                inplace=True,
                            )
                    break

    # ================================================================
    # Feature #8: NPC Goal Conflict Detection
    # ================================================================

    def _check_npc_goal_conflicts(self):
        """Detect conflicts between NPC goals and flag for escalation."""
        conflicts = self.current_state.setdefault("npc_conflicts", [])
        active_conflict_pairs = {
            (c["npc_a"], c["npc_b"]) for c in conflicts if c.get("status") != "resolved"
        }
        goal_progress = self.current_state.get("npc_goal_progress", {})
        completed_ids = {
            (g["id"] if isinstance(g, dict) else g)
            for g in self.current_state.get("completed_npc_goals", [])
        }

        npc_active_goals = {}
        all_npc_defs = list(self.script.get("npcs", []))
        npcs_state = self.current_state.get("npcs", {})
        for dyn_id, dyn_st in npcs_state.items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                dyn_goals = dyn_st.get("goals", [])
                if dyn_goals:
                    all_npc_defs.append({"id": dyn_id, "name": dyn_st.get("name", dyn_id), "goals": dyn_goals})
        for npc_def in all_npc_defs:
            npc_id = npc_def.get("id", "")
            goals = npc_def.get("goals", [])
            if not goals:
                continue
            for goal in goals:
                full_id = f"{npc_id}:{goal.get('id', '')}"
                if full_id in completed_ids:
                    continue
                conflicts_with = goal.get("conflicts_with", [])
                if conflicts_with:
                    npc_active_goals.setdefault(npc_id, []).append({
                        "goal": goal,
                        "conflicts_with": conflicts_with,
                    })

        for npc_a, goals_a in npc_active_goals.items():
            for entry_a in goals_a:
                for target in entry_a["conflicts_with"]:
                    parts = target.split(":", 1)
                    if len(parts) != 2:
                        continue
                    npc_b, goal_b_id = parts
                    if npc_b not in npc_active_goals:
                        continue
                    pair = tuple(sorted([npc_a, npc_b]))
                    if pair in active_conflict_pairs:
                        continue
                    prog_a = goal_progress.get(npc_a, {}).get("progress", 0)
                    prog_b = goal_progress.get(npc_b, {}).get("progress", 0)
                    if prog_a >= 30 or prog_b >= 30 or self.turn_number >= 10:
                        goal_b_desc = goal_b_id
                        for eg in npc_active_goals.get(npc_b, []):
                            if eg["goal"].get("id", "") == goal_b_id:
                                goal_b_desc = eg["goal"].get("description", goal_b_id)[:50]
                                break
                        conflicts.append({
                            "id": f"conflict_{npc_a}_{npc_b}_{self.turn_number}",
                            "npc_a": pair[0],
                            "npc_b": pair[1],
                            "goal_a": entry_a["goal"].get("description", "")[:50],
                            "goal_b": goal_b_desc,
                            "turn_started": self.turn_number,
                            "status": "brewing",
                            "player_sided_with": None,
                        })
                        active_conflict_pairs.add(pair)
                        self.current_state["_npc_conflict_pending"] = {
                            "npc_a": pair[0], "npc_b": pair[1],
                            "goal_a": entry_a["goal"].get("description", "")[:50],
                        }
                        break

        if len(conflicts) > 20:
            self.current_state["npc_conflicts"] = [
                c for c in conflicts if c.get("status") != "resolved"
            ][-20:]

    async def _handle_npc_goal_conflict(self, meta_ctx: dict):
        """Generate conflict escalation event between NPCs."""
        pending = self.current_state.get("_npc_conflict_pending")
        if not pending:
            conflict_list = [c for c in self.current_state.get("npc_conflicts", [])
                            if c.get("status") == "brewing"]
            if not conflict_list:
                return
            pending = conflict_list[0]

        npc_a = pending.get("npc_a", "")
        npc_b = pending.get("npc_b", "")
        name_a = self._get_npc_display_name(npc_a)
        name_b = self._get_npc_display_name(npc_b)
        goal_a = pending.get("goal_a", "")

        for c in self.current_state.get("npc_conflicts", []):
            if c.get("npc_a") == npc_a and c.get("npc_b") == npc_b and c.get("status") == "brewing":
                c["status"] = "active"
                break

        network = self.current_state.setdefault("information_network", [])
        network.append({
            "id": f"conflict_{npc_a}_{npc_b}_{self.turn_number}",
            "origin_turn": self.turn_number,
            "fact": f"{name_a}与{name_b}因目标冲突产生对立",
            "known_by": [npc_a, npc_b],
            "spread_chance": 0.6,
            "distortion": 0,
            "max_spread": 6,
            "tags": ["npc_conflict", npc_a, npc_b],
        })
        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

        npc_rels = self.current_state.setdefault("npc_relationships_global", {})
        rel_key = "_".join(sorted([npc_a, npc_b]))
        rel = npc_rels.setdefault(rel_key, {"from": npc_a, "to": npc_b, "type": "neutral", "value": 50})
        rel["value"] = max(0, rel.get("value", 50) - 20)
        rel["type"] = "hostile" if rel["value"] < 20 else "tense"

        self._record_narrative_callback(
            f"{name_a}与{name_b}的矛盾开始激化",
            ["npc_conflict", npc_a, npc_b], priority="high",
        )

    # ================================================================
    # Feature #3: Player Multi-turn Planning System
    # ================================================================

    @staticmethod
    def _extract_plan_steps(plot_decision: str) -> list[str]:
        """Extract plan_steps from Stage 1 plot decision text."""
        import re as _re
        match = _re.search(r'plan_steps\s*:\s*\[([^\]]*)\]', plot_decision)
        if not match:
            match = _re.search(r'"plan_steps"\s*:\s*\[([^\]]*)\]', plot_decision)
        if not match:
            return []
        try:
            items = json.loads("[" + match.group(1) + "]")
            return [s for s in items if isinstance(s, str)][:5]
        except Exception:
            raw = match.group(1)
            return [s.strip().strip('"').strip("'") for s in raw.split(",") if s.strip()][:5]

    @staticmethod
    def _extract_plan_risk(plot_decision: str) -> str:
        """Extract plan_risk level from Stage 1 plot decision text."""
        import re as _re
        match = _re.search(r'plan_risk\s*:\s*"?(low|medium|high)"?', plot_decision)
        return match.group(1) if match else "medium"

    async def _handle_plan_progress(self, meta_ctx: dict):
        """Check if current turn advanced the player's active plan."""
        plan = self.current_state.get("pending_plan")
        if not plan or plan.get("status") != "active":
            return
        remaining = plan.get("steps_remaining", [])
        if not remaining:
            plan["status"] = "completed"
            self._record_narrative_callback(
                f"计划「{plan.get('goal', '')[:20]}」已完成",
                ["plan_complete"], priority="high",
            )
            return

        narrative = meta_ctx.get("narrative", "")
        action_text = ""
        pa = meta_ctx.get("player_action")
        if isinstance(pa, dict):
            action_text = pa.get("text", "")
        current_step = remaining[0]

        prompt = (
            f"玩家计划: {plan.get('goal', '')}\n"
            f"当前步骤: {current_step}\n"
            f"玩家行动: {action_text}\n"
            f"叙事结果: {narrative[:300]}\n\n"
            '判断玩家是否推进了当前步骤。返回JSON: '
            '{"advanced":true/false,"failed":false,"reason":"简短原因"}'
        )
        try:
            raw = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是计划进度评估器。只返回JSON。",
                max_tokens=150,
                **self._stage_kwargs("knowledge_graph"),
            )
            result = json.loads(raw.strip().strip("```json").strip("```").strip())
        except Exception:
            return

        if result.get("failed"):
            plan["status"] = "failed"
            self._record_narrative_callback(
                f"计划「{plan.get('goal', '')[:20]}」失败: {result.get('reason', '')[:30]}",
                ["plan_failed"], priority="high",
            )
            pacing = self.current_state.setdefault("pacing_state", {})
            pacing["tension"] = min(100, pacing.get("tension", 50) + 15)
        elif result.get("advanced"):
            completed_step = remaining.pop(0)
            plan.setdefault("steps_completed", []).append(completed_step)
            if not remaining:
                plan["status"] = "completed"
                self._record_narrative_callback(
                    f"计划「{plan.get('goal', '')[:20]}」圆满完成！",
                    ["plan_complete"], priority="high",
                )
                pacing = self.current_state.setdefault("pacing_state", {})
                pacing["tension"] = min(100, pacing.get("tension", 50) + 10)

    # ================================================================
    # Feature #5: Retroactive Revelation (Flashbacks)
    # ================================================================

    async def _handle_retroactive_revelation(self, meta_ctx: dict):
        """Generate flashback narrative when hidden lore is discovered."""
        pending = self.current_state.pop("_pending_flashback", None)
        if not pending:
            return
        lore_id = pending.get("lore_id", "")
        lore_content = pending.get("content", "")
        if not lore_content:
            return

        game_time = self.current_state.get("game_time", "")
        player_name = self.current_state.get("player", {}).get("name", "主角")
        prompt = (
            f"当前时间: {game_time}\n"
            f"玩家角色: {player_name}\n"
            f"刚发现的秘密: {lore_content[:300]}\n\n"
            "请用200字以内生成一段闪回叙事（回忆/过去时态），描述这个秘密最初发生时的场景。\n"
            "要求：第三人称过去式，有氛围感，暗示真相但不直说全部。"
        )
        try:
            flashback_text = await self.ai_provider.generate(
                [{"role": "user", "content": prompt}],
                system="你是闪回叙事生成器。用沉浸的文学笔触描写过去的片段。",
                max_tokens=400,
                **self._stage_kwargs("narrative"),
            )
        except Exception:
            return
        if not flashback_text or len(flashback_text.strip()) < 20:
            return

        node = self.world_tree.get_node(self.world_tree.active_node_id)
        if node:
            node.setdefault("flashbacks", []).append({
                "lore_id": lore_id,
                "narrative": flashback_text.strip()[:500],
                "turn": self.turn_number,
            })
        self._record_narrative_callback(
            "一段尘封的记忆浮现……",
            ["flashback", lore_id], priority="high",
        )

    # ================================================================
    # Feature #4: Faction Warfare Simulator
    # ================================================================

    async def _handle_faction_warfare(self, meta_ctx: dict):
        """Simulate faction warfare tick: battles, balance shifts, escalation."""
        wars = self.current_state.get("faction_wars", [])
        if not wars:
            orgs = self.script.get("organizations", [])
            for org in orgs:
                for war_def in org.get("wars", []):
                    enemy = war_def.get("enemy", "")
                    if enemy:
                        wars.append({
                            "id": f"war_{org['id']}_{enemy}",
                            "faction_a": org["id"],
                            "faction_b": enemy,
                            "status": war_def.get("initial_status", "cold_war"),
                            "balance": war_def.get("initial_balance", 0),
                            "territories": war_def.get("territories", {}),
                            "turn_started": self.turn_number,
                            "last_battle_turn": 0,
                            "casualties_a": 0,
                            "casualties_b": 0,
                        })
            if wars:
                self.current_state["faction_wars"] = wars
            else:
                return

        _rng = random
        network = self.current_state.setdefault("information_network", [])
        faction_rep = self.current_state.get("faction_reputation", {})

        for war in wars:
            if war.get("status") in ("resolved", "ceasefire"):
                continue
            fa = war["faction_a"]
            fb = war["faction_b"]
            rep_a = faction_rep.get(fa, {}).get("value", 50) if isinstance(faction_rep.get(fa), dict) else 50
            rep_b = faction_rep.get(fb, {}).get("value", 50) if isinstance(faction_rep.get(fb), dict) else 50
            members_a = len(self._org_members.get(fa, set()))
            members_b = len(self._org_members.get(fb, set()))
            power_a = members_a * 10 + rep_a + _rng.randint(-15, 15)
            power_b = members_b * 10 + rep_b + _rng.randint(-15, 15)

            delta = (power_a - power_b) // 5
            delta = max(-20, min(20, delta))
            war["balance"] = max(-100, min(100, war.get("balance", 0) + delta))
            war["last_battle_turn"] = self.turn_number

            if abs(delta) > 5:
                if delta > 0:
                    war["casualties_b"] = war.get("casualties_b", 0) + abs(delta)
                else:
                    war["casualties_a"] = war.get("casualties_a", 0) + abs(delta)

            balance = war["balance"]
            old_status = war["status"]
            if abs(balance) > 80:
                war["status"] = "resolved"
                winner = fa if balance > 0 else fb
                winner_name = self._get_org_name(winner)
                network.append({
                    "id": f"war_end_{fa}_{fb}_{self.turn_number}",
                    "origin_turn": self.turn_number,
                    "fact": f"{winner_name}在战争中取得决定性胜利",
                    "known_by": list(self._org_members.get(fa, set()) | self._org_members.get(fb, set()))[:8],
                    "spread_chance": 0.8,
                    "distortion": 0,
                    "max_spread": 8,
                    "tags": ["faction_war", fa, fb],
                })
                self._record_narrative_callback(
                    f"势力战争结束: {winner_name}获胜",
                    ["faction_war", "resolved"], priority="high",
                )
            elif abs(balance) > 50 and old_status == "cold_war":
                war["status"] = "skirmish"
                self._record_narrative_callback(
                    f"{self._get_org_name(fa)}与{self._get_org_name(fb)}之间爆发小规模冲突",
                    ["faction_war", "escalation"], priority="medium",
                )
            elif abs(balance) > 65 and old_status == "skirmish":
                war["status"] = "open_war"
                self._record_narrative_callback(
                    f"{self._get_org_name(fa)}与{self._get_org_name(fb)}全面开战",
                    ["faction_war", "escalation"], priority="high",
                )
                network.append({
                    "id": f"war_open_{fa}_{fb}_{self.turn_number}",
                    "origin_turn": self.turn_number,
                    "fact": f"{self._get_org_name(fa)}与{self._get_org_name(fb)}全面开战",
                    "known_by": list(self._org_members.get(fa, set()) | self._org_members.get(fb, set()))[:8],
                    "spread_chance": 0.9,
                    "distortion": 0,
                    "max_spread": 10,
                    "tags": ["faction_war", fa, fb],
                })

        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

    def _get_org_name(self, org_id: str) -> str:
        for org in self.script.get("organizations", []):
            if org.get("id") == org_id:
                return org.get("name", org_id)
        return org_id

    def _get_npc_org(self, npc_id: str) -> str:
        """Return the org_id of the organization this NPC belongs to, or ''."""
        for org_id, members in self._org_members.items():
            if npc_id in members:
                return org_id
        return ""

    # ================================================================
    # StoryDirector: 统一后台剧情系统
    # ================================================================

    async def _run_story_director(self):
        """统一后台剧情系统：plan先行，然后tree/events/goals并发。"""
        logger = logging.getLogger(__name__)
        ctx = self._gather_director_context()
        if not ctx.get("goals_text"):
            return

        # Phase 1: plan 先执行，更新 blueprint 供后续子任务读取
        plan_result = await self._director_task_plan(ctx)
        if isinstance(plan_result, Exception):
            logger.warning("story_director.plan 失败: %s", plan_result)
            plan_result = None
        elif plan_result and isinstance(plan_result, dict) and plan_result.get("plot_threads"):
            self.current_state["plot_blueprint"] = plan_result
            self.current_state["plot_blueprint"]["_last_run_turn"] = self.turn_number
            ctx["current_bp"] = plan_result

        # Phase 2: tree/events/goals 并发（lorebook 由 lorebook_evolution 统一管理）
        tasks = [
            self._director_task_tree(ctx),
            self._director_task_events(ctx),
            self._director_task_goals(ctx),
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        labels = ["tree", "events", "goals"]
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.warning("story_director.%s 失败: %s", labels[i], r)

        all_results = {"plan": plan_result, **dict(zip(labels, results))}
        self._apply_director_results(all_results)

        # Phase 3: game_mechanics（接收 events 结果作为上下文）
        events_result = all_results.get("events")
        mech_result = await self._director_task_mechanics(
            ctx, events_result if isinstance(events_result, dict) else None,
        )
        if isinstance(mech_result, Exception):
            logger.warning("story_director.mechanics 失败: %s", mech_result)
            mech_result = None
        if mech_result and isinstance(mech_result, dict):
            self._apply_mechanics_results(mech_result)

        # Phase 4: 如果 tree/events 产出了新内容，触发 lorebook_evolution
        tree_result = all_results.get("tree")
        events_result = all_results.get("events")
        new_nodes = tree_result.get("new_nodes", []) if isinstance(tree_result, dict) else []
        new_events = []
        if isinstance(events_result, dict):
            new_events = events_result.get("one_time", []) + events_result.get("cyclic", [])
        if new_nodes or new_events:
            self.current_state["_pending_lore_evolution"] = True
            self.current_state["_pending_lore_context"] = {
                "new_nodes": [n.get("description", "") for n in new_nodes if isinstance(n, dict)],
                "new_events": [e.get("description", "") for e in new_events if isinstance(e, dict)],
            }

        logger.info("StoryDirector 完成 (turn %d)", self.turn_number)

    def _gather_director_context(self) -> dict:
        """收集所有子任务共享的上下文。"""
        world_bg = self.script.get("world_background", "")[:400]
        pc = self.current_state.get("player", {})
        key_events = self.current_state.get("key_events", [])[-8:]
        recent_events_text = "；".join(
            e.get("event", "")[:40] for e in key_events
        )
        world_pulse = self._extract_world_pulse_hints()
        current_bp = self.current_state.get("plot_blueprint", {})
        game_time = self.current_state.get("game_time", "")

        # 近期叙事摘要（最近3回合的 plot_decision 摘要）
        recent_narrative = ""
        recent_nodes = self.world_tree.get_recent_history(3)
        narr_lines = []
        for nd in recent_nodes:
            pd = nd.get("plot_decision", "")
            if pd:
                narr_lines.append(pd[:80])
        if narr_lines:
            recent_narrative = "；".join(narr_lines)

        npc_goals = []
        for npc in self.script.get("npcs", []):
            goals = npc.get("goals", [])
            if goals:
                descs = [g.get("description", g.get("id", "")) for g in goals]
                npc_goals.append(f"- {npc.get('name', npc['id'])}: {'; '.join(descs)}")

        org_goals = []
        for org in self.script.get("organizations", []):
            goals = org.get("goals", [])
            if goals:
                descs = [g.get("description", g.get("id", "")) for g in goals]
                org_goals.append(f"- {org.get('name', org['id'])}: {'; '.join(descs)}")

        goals_text = "\n".join(npc_goals + org_goals)

        existing_event_ids = []
        dsc = self.current_state.get("dynamic_story_content", {})
        for e in dsc.get("one_time_events", []):
            existing_event_ids.append(e.get("id", ""))
        for e in dsc.get("cyclic_events", []):
            existing_event_ids.append(e.get("id", ""))

        dynamic_node_ids = []
        script_tree_ids = []
        if self.story_tree_engine:
            for nid in self.story_tree_engine._nodes:
                if nid.startswith("_dyn_"):
                    dynamic_node_ids.append(nid)
                else:
                    script_tree_ids.append(nid)

        script_lore_summary = "; ".join(
            f"{e.id}({e.comment or ','.join(e.keys[:2])})"
            for e in self.prompt_builder.lorebook.entries
            if e.enabled and not e.id.startswith("_kg_") and not e.id.startswith("_dyn_")
        )[:800]

        script_event_ids = [e.get("id", "") for e in self.script.get("one_time_events", [])]
        script_event_ids += [e.get("id", "") for e in self.script.get("cyclic_events", [])]

        active_states = self.current_state.get("active_persistent_states", [])

        ri_summary = "; ".join(
            f"{ri['id']}({ri.get('trigger_type', '?')}"
            f"{', cond=' + ri['trigger_condition'][:50] if ri.get('trigger_condition') else ''})"
            for ri in self.script.get("random_items", [])
        )[:500]

        sv = self.current_state.get("script_variables", {})
        var_summary = "; ".join(f"{k}={v}" for k, v in sv.items())[:300]

        trigger_summary = "; ".join(
            f"{t['id']}({t.get('condition', '')})"
            for t in self.trigger_engine.triggers
        )[:300]

        return {
            "world_bg": world_bg,
            "pc_summary": f"位置:{pc.get('location','?')}, 属性:{pc.get('attributes',{})}",
            "recent_events": recent_events_text,
            "recent_narrative": recent_narrative,
            "world_pulse": world_pulse,
            "current_bp": current_bp,
            "goals_text": goals_text,
            "game_time": game_time,
            "existing_event_ids": ", ".join(existing_event_ids)[:300],
            "dynamic_node_ids": dynamic_node_ids,
            "key_events_raw": key_events,
            "script_lore_summary": script_lore_summary,
            "script_event_ids": ", ".join(script_event_ids)[:300],
            "script_tree_ids": script_tree_ids,
            "active_states": active_states,
            "ri_summary": ri_summary,
            "var_summary": var_summary,
            "trigger_summary": trigger_summary,
        }

    async def _director_task_plan(self, ctx: dict) -> dict | None:
        """长期规划：更新 plot_threads，标记 stage status。"""
        current_bp = ctx["current_bp"]
        bp_text = ""
        if current_bp.get("plot_threads"):
            lines = []
            for t in current_bp["plot_threads"]:
                stages_desc = ", ".join(
                    f"{s['id']}({s.get('status','pending')})" for s in t.get("stages", [])
                )
                lines.append(f"- {t.get('name', t['id'])}: [{stages_desc}]")
            bp_text = "\n".join(lines)

        prompt = (
            f"世界背景: {ctx['world_bg']}\n"
            f"各势力目标:\n{ctx['goals_text']}\n"
            f"近期关键事件: {ctx['recent_events']}\n"
            f"近期叙事走向: {ctx['recent_narrative'][:200]}\n"
        )
        if ctx["world_pulse"]:
            prompt += f"世界脉搏: {ctx['world_pulse']}\n"
        if bp_text:
            prompt += f"当前规划:\n{bp_text}\n\n"
        else:
            prompt += "尚无规划\n\n"
        prompt += (
            "任务：基于以上信息，输出更新后的全局剧情规划。\n"
            "- 如果近期事件使某stage落地，标记status为completed\n"
            "- 如果某stage的前置已完成，标记为active\n"
            "- 可以新增/删除/调整threads和stages\n"
            "- 每条thread 2-4个stages，每stage一句话描述\n"
            '返回JSON: {"plot_threads":[{"id":"","name":"","driver":"npc/org_id",'
            '"stages":[{"id":"","description":"","status":"pending|active|completed"}]}]}'
        )
        system = "你是剧情规划器。维护全局剧情线方向，供每回合骨架决策参考。只返回紧凑JSON。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_plan", stage="knowledge_graph",
        )

    async def _director_task_tree(self, ctx: dict) -> dict | None:
        """story_tree 节点管理：生成新节点 + 审查 pending_review 节点完成。"""
        current_bp = ctx["current_bp"]
        active_stages = []
        if current_bp.get("plot_threads"):
            for t in current_bp["plot_threads"]:
                for s in t.get("stages", []):
                    if s.get("status") == "active":
                        active_stages.append(f"{t.get('name','')}: {s.get('description','')}")

        pending_review_nodes = []
        if self.story_tree_engine:
            sts = self.current_state.get("story_tree_state", {})
            active_set = set(sts.get("active", []))
            for nid in ctx["dynamic_node_ids"]:
                node = self.story_tree_engine._nodes.get(nid)
                if node and node.get("type") == "pending_review" and nid in active_set:
                    pending_review_nodes.append(
                        f"- {nid}: {node.get('description', node.get('name', ''))}"
                    )

        if not active_stages and not pending_review_nodes:
            return None

        prompt = (
            f"世界背景: {ctx['world_bg']}\n"
            f"当前游戏时间: {ctx['game_time'] or '未知'}\n"
            f"近期关键事件: {ctx['recent_events']}\n"
            f"活跃剧情方向:\n" + ("\n".join(active_stages) if active_stages else "无") + "\n"
            f"已有动态节点ID: {', '.join(ctx['dynamic_node_ids'][:20])}\n"
            f"剧本已有节点ID: {', '.join(ctx['script_tree_ids'][:30])}\n"
        )
        if pending_review_nodes:
            prompt += (
                f"\n待审查节点（判断是否已在叙事中落地）:\n" +
                "\n".join(pending_review_nodes) + "\n"
            )
        prompt += (
            "\n任务：\n"
            "1. 根据活跃剧情方向，生成0-3个新 story_tree 节点\n"
            "2. 对待审查节点，判断是否可标记完成\n\n"
            "节点type可选:\n"
            "- timed: 定时自动完成（附duration_turns:回合数）\n"
            "- auto: 条件满足时自动完成（附condition表达式）\n"
            "- pending_review: 无法给出明确条件，由下次审查判断完成\n\n"
            "condition 语法:\n"
            "- player.属性名 op 值 (op: >=, <=, >, <, ==, !=)\n"
            "- event_fired.事件ID == true\n"
            "- node_completed.节点ID == true\n"
            "- milestone.里程碑ID == true\n"
            "- 多条件: cond1 AND cond2, cond1 OR cond2\n\n"
            '返回JSON: {"new_nodes":[{"id":"_dyn_xxx","name":"","description":"",'
            '"type":"timed|auto|pending_review","duration_turns":5,"condition":"",'
            '"requires":["已有节点ID"],"related_npcs":[],"related_orgs":[]}],'
            '"complete_nodes":["要标记完成的节点ID"]}'
        )
        system = "你是剧情树管理器。生成可追踪的叙事节点，审查节点是否已落地。只返回紧凑JSON。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_tree", stage="knowledge_graph",
        )

    async def _director_task_events(self, ctx: dict) -> dict | None:
        """事件生成：生成新的 one_time/cyclic 事件。"""
        active_stages = []
        bp = ctx["current_bp"]
        if bp.get("plot_threads"):
            for t in bp["plot_threads"]:
                for s in t.get("stages", []):
                    if s.get("status") == "active":
                        active_stages.append(f"{t.get('name','')}: {s.get('description','')}")

        prompt = (
            f"世界背景: {ctx['world_bg']}\n"
            f"当前游戏时间: {ctx['game_time'] or '未知'}\n"
            f"玩家状态: {ctx['pc_summary']}\n"
            f"近期关键事件: {ctx['recent_events']}\n"
        )
        if ctx["world_pulse"]:
            prompt += f"世界脉搏暗示: {ctx['world_pulse']}\n"
        if active_stages:
            prompt += f"活跃剧情方向:\n" + "\n".join(f"- {s}" for s in active_stages) + "\n"
        if ctx["active_states"]:
            prompt += f"活跃持续状态: {', '.join(ctx['active_states'])}\n"
        prompt += (
            f"已有动态事件ID: {ctx['existing_event_ids']}\n"
            f"剧本已有事件ID: {ctx['script_event_ids']}\n\n"
            "任务：生成0-2个新事件来丰富世界。事件应该是玩家可能遭遇的具体情境。\n"
            "不要与已有事件重复或冲突。\n"
            '返回JSON: {"one_time":[{"id":"_dyn_evt_xxx","name":"","description":"",'
            '"trigger_time":"ISO时间或空","condition":"可选条件表达式"}],'
            '"cyclic":[{"id":"_dyn_cyc_xxx","name":"","description":"",'
            '"frequency_value":1,"frequency_unit":"day","condition":"可选"}]}\n'
            "不需要扩展则返回空: {}"
        )
        system = "你是事件生成器。生成与世界观一致的动态事件。只返回紧凑JSON。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_events", stage="knowledge_graph",
        )

    async def _director_task_goals(self, ctx: dict) -> dict | None:
        """NPC/org 目标更新。"""
        if not ctx["recent_events"]:
            return None
        prompt = (
            f"各势力当前目标:\n{ctx['goals_text']}\n"
            f"近期关键事件: {ctx['recent_events']}\n\n"
            "任务：根据剧情进展，判断是否需要新增/调整目标。\n"
            '返回JSON: {"npc_goals":[{"npc_id":"","goals":[{"id":"","description":"","type":"short_term|long_term","priority":"low|medium|high"}]}],'
            '"org_goals":[{"org_id":"","goals":[{"id":"","description":"","priority":"medium"}]}]}\n'
            "不需要变更则返回空: {}"
        )
        system = "你是目标管理器。根据剧情进展更新NPC和组织目标。只返回紧凑JSON。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_goals", stage="knowledge_graph",
        )

    async def _director_task_mechanics(self, ctx: dict, events_result: dict | None) -> dict | None:
        """游戏机制维护：根据剧情变化更新 random_items / variables / triggers。"""
        prompt = (
            f"世界背景: {ctx['world_bg']}\n"
            f"当前游戏时间: {ctx['game_time'] or '未知'}\n"
            f"近期关键事件: {ctx['recent_events']}\n"
        )
        if ctx["active_states"]:
            prompt += f"活跃持续状态: {', '.join(ctx['active_states'])}\n"
        prompt += (
            f"\n当前随机项: {ctx['ri_summary']}\n"
            f"当前脚本变量: {ctx['var_summary']}\n"
            f"当前触发器: {ctx['trigger_summary']}\n"
        )
        if events_result:
            new_evts = []
            for e in events_result.get("one_time", []):
                new_evts.append(e.get("name", e.get("id", "")))
            for e in events_result.get("cyclic", []):
                new_evts.append(e.get("name", e.get("id", "")))
            if new_evts:
                prompt += f"本轮新增事件: {', '.join(new_evts)}\n"
        prompt += (
            "\n任务：检查上述游戏机制是否需要随世界变化更新。\n"
            "- 随机项(random_items)：新地点/势力出现时更新trigger_condition，或新增/移除随机项\n"
            "- 变量(variables)：如需跟踪新的世界状态量\n"
            "- 触发器(triggers)：如需在特定事件触发时自动改变持续状态\n"
            "不需要变更则返回空: {}\n\n"
            "返回JSON:\n"
            '{"random_items":{"add":[{"id":"_dyn_xxx","description":"","trigger":"","trigger_type":"conditional","trigger_condition":"条件表达式",'
            '"duration_turns":6,"cooldown_turns":2,"dice":{"count":1,"faces":100,"modifier":0},'
            '"ranges":[{"min":1,"max":50,"label":"","description":"","state_changes":[]},{"min":51,"max":100,"label":"","description":"","state_changes":[]}]}],'
            '"update":[{"id":"已有id","trigger_type":"conditional","trigger_condition":"新条件"}],"remove":["过时id"]},'
            '"variables":{"add":[{"id":"xxx","type":"number","default":0,"min":0,"max":100}],"update":[{"id":"xxx","value":0}]},'
            '"triggers":{"add":[{"id":"_dyn_xxx","condition":"event.xxx.fired","actions":[{"type":"activate_persistent_state","target":"xxx"}]}],'
            '"update":[{"id":"xxx","enabled":false}],"remove":["过时id"]}}'
        )
        system = "你是游戏机制维护器。根据世界变化更新随机项、变量和触发器。只返回紧凑JSON，不需要变更返回{}。"
        return await self._retry_ai_call(
            [{"role": "user", "content": prompt}],
            system, self._parse_simple_json,
            label="director_mechanics", stage="knowledge_graph",
        )

    def _apply_mechanics_results(self, results: dict):
        """应用 game_mechanics 子任务的结果。"""
        logger = logging.getLogger(__name__)
        dsc = self.current_state.setdefault("dynamic_story_content", {
            "trees": [], "one_time_events": [], "cyclic_events": [],
            "lorebook": [], "generation_log": [],
        })
        ri = results.get("random_items")
        if ri and isinstance(ri, dict):
            self._apply_random_item_updates(ri)
            dsc.setdefault("random_item_updates", []).append({"turn": self.turn_number, "updates": ri})
            logger.info("StoryDirector mechanics: random_items updated (add=%d, update=%d, remove=%d)",
                        len(ri.get("add", [])), len(ri.get("update", [])), len(ri.get("remove", [])))
        var = results.get("variables")
        if var and isinstance(var, dict):
            self._apply_variable_updates(var)
            dsc.setdefault("variable_updates", []).append({"turn": self.turn_number, "updates": var})
            logger.info("StoryDirector mechanics: variables updated (add=%d, update=%d)",
                        len(var.get("add", [])), len(var.get("update", [])))
        trg = results.get("triggers")
        if trg and isinstance(trg, dict):
            self._apply_trigger_updates(trg)
            dsc.setdefault("trigger_updates", []).append({"turn": self.turn_number, "updates": trg})
            logger.info("StoryDirector mechanics: triggers updated (add=%d, update=%d, remove=%d)",
                        len(trg.get("add", [])), len(trg.get("update", [])), len(trg.get("remove", [])))

    def _apply_director_results(self, results: dict):
        """应用所有子任务的结果到游戏状态。"""
        logger = logging.getLogger(__name__)
        dsc = self.current_state.setdefault("dynamic_story_content", {
            "trees": [], "one_time_events": [], "cyclic_events": [],
            "lorebook": [], "generation_log": [],
        })

        # Plan 已在 Phase 1 应用，此处只记录日志用
        plan = results.get("plan")

        # Tree
        tree_result = results.get("tree")
        if tree_result and isinstance(tree_result, dict):
            new_nodes = tree_result.get("new_nodes", [])
            if new_nodes:
                tree = {"id": f"_dyn_director_{self.turn_number}", "name": "动态剧情",
                        "nodes": new_nodes}
                dsc["trees"].append(tree)
                self._inject_dynamic_tree(tree)

            completed_nodes = []
            for nid in tree_result.get("complete_nodes", []):
                if self.story_tree_engine and nid in self.story_tree_engine._nodes:
                    node = self.story_tree_engine._nodes[nid]
                    sts = self.current_state.setdefault("story_tree_state", {
                        "completed": [], "active": [], "unlocked": [],
                        "timed_progress": {}, "choices_made": {},
                    })
                    if nid not in sts["completed"]:
                        sts["completed"].append(nid)
                        if nid in sts["active"]:
                            sts["active"].remove(nid)
                        completed_nodes.append(node)
                        logger.info("StoryDirector 标记节点完成: %s", nid)
            if completed_nodes:
                self._sync_node_lorebook(completed_nodes, [])

        # Events
        events_result = results.get("events")
        if events_result and isinstance(events_result, dict):
            for evt in events_result.get("one_time", []):
                if evt.get("id"):
                    dsc["one_time_events"].append(evt)
                    self._inject_dynamic_event(evt, "one_time")
            for evt in events_result.get("cyclic", []):
                if evt.get("id"):
                    dsc["cyclic_events"].append(evt)
                    self._inject_dynamic_event(evt, "cyclic")

        # Goals
        goals_result = results.get("goals")
        if goals_result and isinstance(goals_result, dict):
            self._apply_goal_updates(goals_result)

        # Log
        dsc["generation_log"].append({
            "turn": self.turn_number,
            "summary": f"plan={'ok' if plan else 'skip'}, tree={len(tree_result.get('new_nodes',[])) if isinstance(tree_result, dict) else 0} nodes",
        })
        dsc["_last_expand_turn"] = self.turn_number

        # Capacity limits
        if len(dsc["generation_log"]) > 20:
            dsc["generation_log"] = dsc["generation_log"][-20:]
        if len(dsc["lorebook"]) > 50:
            dsc["lorebook"] = dsc["lorebook"][-50:]
        if len(dsc["one_time_events"]) > 30:
            dsc["one_time_events"] = dsc["one_time_events"][-30:]
        if len(dsc["cyclic_events"]) > 15:
            dsc["cyclic_events"] = dsc["cyclic_events"][-15:]
        if len(dsc["trees"]) > 20:
            dsc["trees"] = dsc["trees"][-20:]
        if len(dsc.get("random_item_updates", [])) > 20:
            dsc["random_item_updates"] = dsc["random_item_updates"][-20:]
        if len(dsc.get("variable_updates", [])) > 20:
            dsc["variable_updates"] = dsc["variable_updates"][-20:]
        if len(dsc.get("trigger_updates", [])) > 20:
            dsc["trigger_updates"] = dsc["trigger_updates"][-20:]

    def _extract_world_pulse_hints(self) -> str:
        """Extract [世界脉搏] lines from recent plot decisions."""
        recent = self.world_tree.get_recent_history(5)
        hints = []
        pulse_re = re.compile(r'\[世界脉搏\]\s*(.*?)(?:\n\[|$)', re.DOTALL)
        for node in recent:
            pd = node.get("plot_decision", "")
            if not pd:
                continue
            m = pulse_re.search(pd)
            if m:
                text = m.group(1).strip()
                if text:
                    hints.append(text[:100])
        return "；".join(hints[-3:]) if hints else ""

    async def _search_for_expansion(self, world_bg: str, recent_events: str, pc: dict) -> str:
        """Use AI to extract a search keyword, then web-search for reference material."""
        settings = self.script.get("settings", {})
        if not settings.get("web_search_expansion", True):
            return ""
        if not self.ai_provider:
            return ""

        location = pc.get("location", "")
        context = f"世界背景: {world_bg[:100]}\n位置: {location}\n近期事件: {recent_events}"

        # Let routing model produce a concise search keyword
        try:
            keyword_raw = await self._retry_ai_call(
                [{"role": "user", "content": context}],
                "根据以下游戏上下文，输出一个最适合网络搜索的关键词短语（用于查找相关历史/地理/文化资料）。"
                "只输出关键词本身，不超过15字，不要解释。如果上下文信息充分不需要搜索，输出空字符串。",
                lambda x: x.strip() if isinstance(x, str) else "",
                label="search_keyword", stage="knowledge_graph",
            )
        except Exception:
            keyword_raw = ""

        if not keyword_raw or len(keyword_raw) > 40:
            return ""

        try:
            from search.search_engine import SearchEngine
            engine = SearchEngine()
            results = await engine.search(keyword_raw, sources=["web"], max_results=3)
            if not results:
                return ""
            lines = ["## 外部参考资料（仅供参考，须适配世界观）"]
            for r in results:
                title = r.get("title", "")
                content = r.get("content", "")[:200]
                if title or content:
                    lines.append(f"- {title}: {content}")
            return "\n".join(lines) if len(lines) > 1 else ""
        except Exception as e:
            logging.getLogger(__name__).debug("扩展搜索失败(非致命): %s", e)
            return ""

    def _inject_dynamic_tree(self, tree: dict):
        """注入一棵动态剧情树到 story_tree_engine 和 event_engine。"""
        if not self.story_tree_engine:
            from engine.story_tree import StoryTreeEngine
            self.story_tree_engine = StoryTreeEngine({"trees": []})
        existing_ids = set(self.story_tree_engine._nodes.keys())
        new_node_ids = {n["id"] for n in tree.get("nodes", []) if n.get("id")}
        valid_ids = existing_ids | new_node_ids
        tree_copy = {
            "id": tree["id"],
            "name": tree.get("name", ""),
            "icon": tree.get("icon", "scroll"),
            "nodes": [],
        }
        for node in tree.get("nodes", []):
            if node.get("id") in existing_ids:
                continue
            # 动态生成的 choice 节点降级为 auto（叙事不会自动展示选项）
            if node.get("type") == "choice":
                node["type"] = "auto"
                node.pop("choices", None)
            # 动态节点不直接暴露给叙事层，通过lorebook间接影响
            effects = node.get("effects", {})
            effects.pop("narrative_callback", None)
            effects.pop("notify", None)
            # 清理引用不存在节点的 requires
            requires = node.get("requires", [])
            if requires:
                node["requires"] = [r for r in requires if isinstance(r, str) and r in valid_ids]
                if not node["requires"] and node.get("type") == "auto":
                    continue
            tree_copy["nodes"].append(node)
            existing_ids.add(node["id"])
        if tree_copy["nodes"]:
            self.story_tree_engine.trees.append(tree_copy)
            for node in tree_copy["nodes"]:
                self.story_tree_engine._nodes[node["id"]] = node
                self.story_tree_engine._tree_for_node[node["id"]] = tree_copy["id"]
        self.event_engine.inject_dynamic_tree(tree)

    def _inject_dynamic_event(self, evt: dict, kind: str):
        """注入动态事件到 event_scheduler 和 event_engine。"""
        collection_key = "one_time_events" if kind == "one_time" else "cyclic_events"
        collection = self.event_scheduler.script.setdefault(collection_key, [])
        if any(e.get("id") == evt["id"] for e in collection):
            return
        collection.append(evt)
        self._event_def_by_id[evt["id"]] = evt
        self._event_desc_by_id[evt["id"]] = evt.get("description", evt["id"])
        if kind == "cyclic":
            self.event_scheduler._cyclic_by_id[evt["id"]] = evt
            trackers = self.current_state.setdefault("cyclic_event_trackers", {})
            if evt["id"] not in trackers:
                trackers[evt["id"]] = {"next_fire": evt.get("first_trigger", "")}
        self.event_engine.inject_dynamic_event(evt, kind)
        self.event_engine.inject_dynamic_event_tracker(self.current_state, evt, kind)

    def _apply_goal_updates(self, updates: dict):
        """更新 NPC/组织的 goals。"""
        for npc_update in updates.get("npc_goals", []):
            npc_id = npc_update.get("npc_id")
            new_goals = npc_update.get("goals", [])
            npc_def = self._npc_by_id.get(npc_id)
            if npc_def and new_goals:
                existing = {g.get("id") for g in npc_def.get("goals", [])}
                for g in new_goals:
                    if g.get("id") not in existing:
                        npc_def.setdefault("goals", []).append(g)
        for org_update in updates.get("org_goals", []):
            org_id = org_update.get("org_id")
            new_goals = org_update.get("goals", [])
            for org in self.script.get("organizations", []):
                if org.get("id") == org_id:
                    existing = {g.get("id") for g in org.get("goals", [])}
                    for g in new_goals:
                        if g.get("id") not in existing:
                            org.setdefault("goals", []).append(g)
                    break

    def _apply_lorebook_updates(self, updates: dict):
        """更新已有 lorebook 条目的 content。"""
        for lore_upd in updates.get("lorebook", []):
            lid = lore_upd.get("id")
            new_content = lore_upd.get("content")
            if lid and new_content:
                self.prompt_builder.lorebook.update_entry(lid, content=new_content)

    def _apply_random_item_updates(self, updates: dict):
        """应用 random_items 的增删改。"""
        random_items = self.script.setdefault("random_items", [])
        for rid in updates.get("remove", []):
            random_items[:] = [r for r in random_items if r.get("id") != rid]
            self._random_item_by_id.pop(rid, None)
        for upd in updates.get("update", []):
            item = self._random_item_by_id.get(upd.get("id"))
            if item:
                for field in ("trigger_type", "trigger_condition", "description", "trigger"):
                    if field in upd:
                        item[field] = upd[field]
        for new_item in updates.get("add", []):
            nid = new_item.get("id", "")
            if nid and nid not in self._random_item_by_id:
                if not nid.startswith("_dyn_"):
                    new_item["id"] = f"_dyn_{nid}"
                    nid = new_item["id"]
                random_items.append(new_item)
                self._random_item_by_id[nid] = new_item

    def _apply_variable_updates(self, updates: dict):
        """应用 script_variables 的新增和值更新。"""
        for var_def in updates.get("add", []):
            vid = var_def.get("id")
            if vid and vid not in self.script_variables.definitions:
                self.script_variables.definitions[vid] = var_def
                self.script_variables.set(self.current_state, vid, var_def.get("default", 0))
        for upd in updates.get("update", []):
            vid = upd.get("id")
            if vid and "value" in upd:
                self.script_variables.set(self.current_state, vid, upd["value"])

    def _apply_trigger_updates(self, updates: dict):
        """应用 triggers 的增删改。"""
        triggers = self.trigger_engine.triggers
        for tid in updates.get("remove", []):
            triggers[:] = [t for t in triggers if t.get("id") != tid]
        for upd in updates.get("update", []):
            for t in triggers:
                if t.get("id") == upd.get("id"):
                    for field in ("condition", "enabled", "description", "actions"):
                        if field in upd:
                            t[field] = upd[field]
                    break
        for new_t in updates.get("add", []):
            nid = new_t.get("id", "")
            if nid and not any(t.get("id") == nid for t in triggers):
                if not nid.startswith("_dyn_"):
                    new_t["id"] = f"_dyn_{nid}"
                triggers.append(new_t)

    def _restore_dynamic_story(self):
        """从 current_state 恢复动态剧情内容（会话恢复时调用）。"""
        dsc = self.current_state.get("dynamic_story_content")
        if not dsc:
            return
        for tree in dsc.get("trees", []):
            self._inject_dynamic_tree(tree)
        for evt in dsc.get("one_time_events", []):
            self._inject_dynamic_event(evt, "one_time")
        for evt in dsc.get("cyclic_events", []):
            self._inject_dynamic_event(evt, "cyclic")
        for entry in dsc.get("lorebook", []):
            self.prompt_builder.lorebook.add_entries([entry])
        for ri_upd in dsc.get("random_item_updates", []):
            upd = ri_upd.get("updates", {})
            if upd:
                self._apply_random_item_updates(upd)
        for var_upd in dsc.get("variable_updates", []):
            upd = var_upd.get("updates", {})
            if upd:
                self._apply_variable_updates(upd)
        for trg_upd in dsc.get("trigger_updates", []):
            upd = trg_upd.get("updates", {})
            if upd:
                self._apply_trigger_updates(upd)

    def _extract_name_keywords(self, text: str) -> list[str]:
        """Extract NPC/org/location names from text as lorebook keywords."""
        keywords = []
        for npc in self.script.get("npcs", []):
            name = npc.get("name", "")
            if name and name in text:
                keywords.append(name)
        for dyn_id, dyn_st in self.current_state.get("npcs", {}).items():
            if dyn_id in self._npc_by_id or not isinstance(dyn_st, dict):
                continue
            name = dyn_st.get("name", "")
            if name and name in text:
                keywords.append(name)
        for org in self.script.get("organizations", []):
            name = org.get("name", "")
            if name and name in text:
                keywords.append(name)
        for loc in self.script.get("locations", []):
            name = loc.get("name", "")
            if name and name in text:
                keywords.append(name)
        return keywords

    def _get_npc_or_org_name(self, id_str: str) -> str:
        """Resolve an NPC or org ID to its display name."""
        npc = self._npc_by_id.get(id_str, {})
        if npc:
            return npc.get("name", id_str)
        dyn = self.current_state.get("npcs", {}).get(id_str)
        if isinstance(dyn, dict) and dyn.get("name"):
            return dyn["name"]
        for org in self.script.get("organizations", []):
            if org.get("id") == id_str:
                return org.get("name", id_str)
        return id_str

    def _check_npc_secrets(self):
        """Check NPC secret unlock conditions based on attitude thresholds and game state."""
        unlocked = self.current_state.get("npc_unlocked_secrets", {})
        npcs_state = self.current_state.get("npcs", {})
        all_npc_defs = list(self.script.get("npcs", []))
        for dyn_id, dyn_st in npcs_state.items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                dyn_secrets = dyn_st.get("secrets", [])
                if dyn_secrets:
                    all_npc_defs.append({
                        "id": dyn_id,
                        "name": dyn_st.get("name", dyn_id),
                        "secrets": dyn_secrets,
                    })
        for npc_def in all_npc_defs:
            npc_id = npc_def.get("id", "")
            secrets = npc_def.get("secrets", [])
            if not secrets:
                continue
            npc_state = npcs_state.get(npc_id, {})
            if not isinstance(npc_state, dict) or not npc_state.get("known"):
                continue
            attitude = npc_state.get("attitude_toward_player", 50)
            npc_name = npc_state.get("name") or npc_def.get("name", npc_id)
            npc_unlocked = unlocked.get(npc_id, [])
            unlocked_ids = set(npc_unlocked)
            for secret in secrets:
                sid = secret.get("id", "")
                if not sid or sid in unlocked_ids:
                    continue
                req_att = secret.get("unlock_attitude", 0)
                if attitude < req_att:
                    continue
                cond = secret.get("unlock_condition", "")
                if cond and not self._evaluate_condition(cond):
                    continue
                npc_unlocked.append(sid)
                unlocked_ids.add(sid)
                desc = secret.get("content", sid)[:80]
                self._record_narrative_callback(
                    f"「{npc_name}」向你透露了秘密：{desc}",
                    ["npc_secret", npc_id], priority="high",
                )
            unlocked[npc_id] = npc_unlocked
        self.current_state["npc_unlocked_secrets"] = unlocked
        secret_counts = {}
        for npc_def in all_npc_defs:
            s = npc_def.get("secrets", [])
            if s:
                secret_counts[npc_def["id"]] = len(s)
        if secret_counts:
            self.current_state["_npc_secret_counts"] = secret_counts

    def _update_companion_loyalty(self):
        """Update companion loyalty based on turn events."""
        companions = self.current_state.get("companions", [])
        if not companions:
            return
        loyalty = self.current_state.setdefault("companion_loyalty", {})
        pacing = self.current_state.get("pacing_state", {})
        threshold_events = self.current_state.get("_last_threshold_events", [])

        for cid in companions:
            loy = loyalty.setdefault(cid, {"value": 50})
            delta = 0
            npc_st = self.current_state.get("npcs", {}).get(cid, {})
            attitude = npc_st.get("attitude_toward_player", 50) if isinstance(npc_st, dict) else 50

            if attitude >= 75:
                delta += 1
            elif attitude <= 30:
                delta -= 3

            if pacing.get("consecutive_high", 0) >= 3:
                delta -= 2

            for te in (threshold_events or []):
                if te.get("direction") == "below":
                    attr = te.get("attribute", "")
                    if any(k in attr for k in ("hp", "health", "生命", "体力")):
                        delta += 2
                        break

            if delta != 0:
                loy["value"] = max(0, min(100, loy["value"] + delta))

    def _sync_companions(self):
        """Sync companion locations to player and check loyalty thresholds."""
        companions = self.current_state.get("companions", [])
        if not companions:
            return
        player_loc = self.current_state.get("player", {}).get("location", "")
        loyalty = self.current_state.get("companion_loyalty", {})
        departures: list[str] = []
        for cid in companions:
            # Keep companion location synced to player
            npc_st = self.current_state.get("npcs", {}).get(cid)
            if isinstance(npc_st, dict) and player_loc:
                npc_st["current_location"] = player_loc
            # Check loyalty threshold for departure
            loy = loyalty.get(cid, {})
            val = loy.get("value", 50)
            if val <= 10:
                departures.append(cid)
                npc_name = npc_st.get("name", cid) if isinstance(npc_st, dict) else cid
                self._record_narrative_callback(
                    f"「{npc_name}」因对你极度失望而离开了队伍。",
                    ["companion_leave", cid], priority="high",
                )
        for cid in departures:
            companions.remove(cid)
            loyalty.pop(cid, None)
        self.current_state["companions"] = companions
        self.current_state["companion_loyalty"] = loyalty

    def _apply_reputation_attitude_modifier(self):
        """Adjust NPC attitudes based on their faction's reputation with the player.

        Uses delta from previous modifier to avoid cumulative drift.
        """
        faction_rep = self.current_state.get("faction_reputation", {})
        if not faction_rep:
            return
        npcs_state = self.current_state.get("npcs", {})
        prev_mods = self.current_state.get("_rep_attitude_mods", {})
        new_mods = {}
        all_npc_defs = list(self.script.get("npcs", []))
        for dyn_id, dyn_st in npcs_state.items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                dyn_faction = dyn_st.get("faction", "")
                if dyn_faction:
                    all_npc_defs.append({"id": dyn_id, "faction": dyn_faction})
        for npc_def in all_npc_defs:
            npc_id = npc_def.get("id", "")
            faction = npc_def.get("faction", "")
            if not npc_id or not faction:
                continue
            rep_entry = faction_rep.get(faction)
            if not rep_entry:
                continue
            rep_value = rep_entry.get("value", 50)
            modifier = max(-5, min(5, int((rep_value - 50) / 5)))
            old_modifier = prev_mods.get(npc_id, 0)
            delta = modifier - old_modifier
            if delta == 0:
                new_mods[npc_id] = modifier
                continue
            npc_st = npcs_state.get(npc_id)
            if isinstance(npc_st, dict):
                base = npc_st.get("attitude_toward_player", 50)
                npc_st["attitude_toward_player"] = max(0, min(100, base + delta))
            new_mods[npc_id] = modifier
        self.current_state["_rep_attitude_mods"] = new_mods

    def _check_quest_templates(self):
        """Check quest_templates conditions and populate available_quests."""
        templates = self.script.get("quest_templates", [])
        if not templates:
            return
        available = self.current_state.get("available_quests", [])
        available_ids = {q["id"] for q in available}
        cooldowns = self.current_state.get("quest_cooldowns", {})

        for tpl in templates:
            tpl_id = tpl.get("id", "")
            if not tpl_id or tpl_id in available_ids:
                continue
            cd_until = cooldowns.get(tpl_id, 0)
            if self.turn_number < cd_until:
                continue
            condition = tpl.get("condition", "")
            if condition and not self._evaluate_condition(condition):
                continue
            available.append({
                "id": tpl_id,
                "name": tpl.get("name", tpl_id),
                "trigger_hint": tpl.get("trigger_hint", ""),
                "reward_hint": tpl.get("reward_hint", ""),
                "activated_turn": self.turn_number,
            })
            cooldowns[tpl_id] = self.turn_number + tpl.get("cooldown_turns", 10)
        self.current_state["available_quests"] = available
        self.current_state["quest_cooldowns"] = cooldowns

    def _check_npc_interventions(self):
        """Evaluate conditions for NPC proactive interventions.

        Types: confrontation, aid, warning, ambush, quest_offer.
        Results are written to state["npc_interventions"] for prompt injection.
        """
        state = self.current_state
        interventions = []
        cooldowns = state.get("npc_intervention_cooldowns", {})
        player_loc = state.get("player", {}).get("location", "")
        network = state.get("information_network", [])
        pacing = state.get("pacing_state", {})
        tension = pacing.get("tension", 50)
        low_pacing = pacing.get("consecutive_low", 0) >= 4 and tension <= 25
        completed_goals = {(g["id"] if isinstance(g, dict) else g) for g in state.get("completed_npc_goals", [])}

        player_attrs = state.get("player", {}).get("attributes", {})
        player_low_health = False
        for attr_name, val in player_attrs.items():
            v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
            mx = val.get("max", 100) if isinstance(val, dict) else 100
            if any(k in attr_name for k in ("hp", "health", "生命", "体力")) and v < mx * 0.3:
                player_low_health = True
                break

        # 合并脚本NPC和动态NPC，确保动态NPC也能参与介入
        all_npc_defs = list(self.script.get("npcs", []))
        for dyn_id, dyn_st in state.get("npcs", {}).items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict):
                all_npc_defs.append({
                    "id": dyn_id,
                    "name": dyn_st.get("name", dyn_id),
                    "capabilities": dyn_st.get("capabilities", ""),
                    "goals": [],
                    "faction": dyn_st.get("faction", ""),
                })

        for npc_def in all_npc_defs:
            if len(interventions) >= 2:
                break
            npc_id = npc_def.get("id", "")
            if not npc_id:
                continue
            cd_until = cooldowns.get(npc_id, 0)
            if self.turn_number < cd_until:
                continue

            npc_name = npc_def.get("name", npc_id)
            npc_state = state.get("npcs", {}).get(npc_id, {})
            if not isinstance(npc_state, dict):
                continue
            attitude = npc_state.get("attitude_toward_player", 50)
            npc_loc = self._get_npc_location(npc_id)
            is_present = npc_loc and player_loc and self._locations_match(player_loc, npc_loc)
            is_nearby = False
            if not is_present and npc_loc and player_loc:
                player_loc_def = self._location_by_id.get(player_loc, {})
                connections = player_loc_def.get("connections", [])
                if npc_loc in connections:
                    is_nearby = True

            capabilities = npc_def.get("capabilities", "")
            has_combat = any(k in capabilities for k in ("战斗", "攻击", "武力", "执法", "守卫", "combat", "fight"))
            has_healing = any(k in capabilities for k in ("治疗", "医术", "heal", "治愈", "魔法"))

            # --- confrontation ---
            if attitude < 30 and has_combat and (is_present or is_nearby):
                negative_info = [
                    info for info in network
                    if npc_id in info.get("known_by", [])
                    and any(t in info.get("tags", []) for t in ("violence", "theft", "crime", "player_action"))
                ]
                if negative_info:
                    info = negative_info[-1]
                    distorted = "（传闻失真）" if info.get("distortion", 0) >= 2 else ""
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "confrontation",
                        "reason": f"得知：{info['fact'][:40]}{distorted}",
                        "urgency": "high",
                        "suggested_action": f"{npc_name}拦住你的去路，质问此事",
                    })
                    cooldowns[npc_id] = self.turn_number + 5
                    continue

            # --- aid ---
            if attitude > 75 and player_low_health and (is_present or is_nearby):
                interventions.append({
                    "npc_id": npc_id,
                    "npc_name": npc_name,
                    "type": "aid",
                    "reason": "察觉你状态不佳",
                    "urgency": "medium",
                    "suggested_action": f"{npc_name}主动{'赶来' if is_nearby else '上前'}关心你的状况" + (f"并施以{capabilities[:10]}" if has_healing else ""),
                })
                cooldowns[npc_id] = self.turn_number + 5
                continue

            # --- warning ---
            if attitude > 50 and is_present:
                danger_info = [
                    info for info in network
                    if npc_id in info.get("known_by", [])
                    and any(t in info.get("tags", []) for t in ("danger", "threat", "ambush"))
                ]
                if danger_info:
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "warning",
                        "reason": f"获知：{danger_info[-1]['fact'][:40]}",
                        "urgency": "medium",
                        "suggested_action": f"{npc_name}低声警告你注意危险",
                    })
                    cooldowns[npc_id] = self.turn_number + 5
                    continue

            # --- ambush ---
            if attitude < 20 and has_combat and tension < 40 and (is_present or is_nearby):
                interventions.append({
                    "npc_id": npc_id,
                    "npc_name": npc_name,
                    "type": "ambush",
                    "reason": f"对你怀有敌意（好感{attitude}）",
                    "urgency": "high",
                    "suggested_action": f"{npc_name}{'突然出现并' if is_nearby else ''}对你发起攻击",
                })
                cooldowns[npc_id] = self.turn_number + 8
                continue

            # --- quest_offer ---
            quest_attitude_threshold = 40 if low_pacing else 60
            if attitude > quest_attitude_threshold and is_present:
                goals = npc_def.get("goals", [])
                active_goals = [g for g in goals if f"{npc_id}:{g.get('id', '')}" not in completed_goals]
                if active_goals:
                    goal = active_goals[0]
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "quest_offer",
                        "reason": f"需要帮助完成目标「{goal.get('description', '')[:20]}」",
                        "urgency": "low",
                        "suggested_action": f"{npc_name}向你提出请求",
                    })
                    cooldowns[npc_id] = self.turn_number + 10

            # --- goal_pursuit: NPC actively pursuing their goal ---
            if not any(iv["npc_id"] == npc_id for iv in interventions):
                gp_data = state.get("npc_goal_progress", {}).get(npc_id, {})
                gp_progress = gp_data.get("progress", 0)
                if gp_progress >= 50 and is_present:
                    goals = npc_def.get("goals", [])
                    active_goals = [g for g in goals if f"{npc_id}:{g.get('id', '')}" not in completed_goals]
                    if active_goals:
                        goal = active_goals[0]
                        gp_urgency = "high" if gp_progress >= 80 else "medium"
                        gp_action = gp_data.get("last_action", "")
                        interventions.append({
                            "npc_id": npc_id,
                            "npc_name": npc_name,
                            "type": "goal_pursuit",
                            "reason": f"目标「{goal.get('description', '')[:20]}」进度{gp_progress}%",
                            "urgency": gp_urgency,
                            "suggested_action": f"{npc_name}正在采取行动推进自己的计划" + (f"（{gp_action[:20]}）" if gp_action else ""),
                        })
                        cooldowns[npc_id] = self.turn_number + 4

            # --- info_share: NPC shares valuable knowledge based on relationship ---
            if attitude > 65 and is_present and not any(iv["npc_id"] == npc_id for iv in interventions):
                shareable = [
                    info for info in network
                    if npc_id in info.get("known_by", [])
                    and info.get("distortion", 0) <= 1
                    and any(t in info.get("tags", []) for t in
                            ("danger", "threat", "treasure", "secret", "quest", "org_goal", "offscreen"))
                ]
                if shareable:
                    info = shareable[-1]
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "info_share",
                        "reason": f"掌握情报：{info['fact'][:40]}",
                        "urgency": "low",
                        "suggested_action": f"{npc_name}凑近你，透露一个消息",
                    })
                    cooldowns[npc_id] = self.turn_number + 8

            # --- misunderstanding: high-distortion info causes wrong reaction ---
            if 30 <= attitude <= 60 and (is_present or is_nearby) and not any(iv["npc_id"] == npc_id for iv in interventions):
                distorted_info = [
                    info for info in network
                    if npc_id in info.get("known_by", [])
                    and info.get("distortion", 0) >= 2
                    and any(t in info.get("tags", []) for t in ("player_action", "crime", "offscreen"))
                ]
                if distorted_info:
                    info = distorted_info[-1]
                    interventions.append({
                        "npc_id": npc_id,
                        "npc_name": npc_name,
                        "type": "misunderstanding",
                        "reason": f"听到失实传闻：{info['fact'][:40]}（失真度{info['distortion']}）",
                        "urgency": "medium",
                        "suggested_action": f"{npc_name}态度突变，似乎对你产生了误解",
                    })
                    cooldowns[npc_id] = self.turn_number + 6

            # --- conflict_plea: NPC asks player to side with them in a conflict ---
            npc_conflicts = state.get("npc_conflicts", [])
            if attitude > 50 and is_present and not any(iv["npc_id"] == npc_id for iv in interventions):
                for conflict in npc_conflicts:
                    if conflict.get("status") != "active":
                        continue
                    if conflict.get("player_sided_with"):
                        continue
                    if npc_id in (conflict.get("npc_a"), conflict.get("npc_b")):
                        opponent = conflict["npc_b"] if npc_id == conflict["npc_a"] else conflict["npc_a"]
                        opponent_name = self._get_npc_display_name(opponent)
                        interventions.append({
                            "npc_id": npc_id,
                            "npc_name": npc_name,
                            "type": "conflict_plea",
                            "reason": f"与{opponent_name}存在冲突，希望获得你的帮助",
                            "urgency": "medium",
                            "suggested_action": f"{npc_name}恳请你在与{opponent_name}的争端中支持自己",
                        })
                        cooldowns[npc_id] = self.turn_number + 8
                        break

            # --- faction_recruit: faction at war tries to recruit player ---
            faction_wars = state.get("faction_wars", [])
            if is_present and not any(iv["npc_id"] == npc_id for iv in interventions):
                npc_org = self._get_npc_org(npc_id)
                if npc_org:
                    for war in faction_wars:
                        if war.get("status") not in ("skirmish", "open_war"):
                            continue
                        if npc_org in (war.get("faction_a"), war.get("faction_b")):
                            interventions.append({
                                "npc_id": npc_id,
                                "npc_name": npc_name,
                                "type": "faction_recruit",
                                "reason": f"所属势力正处于{'全面战争' if war['status'] == 'open_war' else '冲突'}中",
                                "urgency": "low",
                                "suggested_action": f"{npc_name}试图拉拢你加入{'战斗' if war['status'] == 'open_war' else '他们一方'}",
                            })
                            cooldowns[npc_id] = self.turn_number + 10
                            break

        state["npc_interventions"] = interventions
        state["npc_intervention_cooldowns"] = cooldowns
        # Feature #1: Scene hijack — high urgency intervention takes over the scene
        if interventions and interventions[0].get("urgency") == "high":
            state["_scene_hijack"] = interventions[0]

    def _collect_interactables(self):
        """Collect interactable elements at the player's current location."""
        state = self.current_state
        player_loc = state.get("player", {}).get("location", "")
        if not player_loc:
            state["location_interactables"] = []
            return

        loc_def = self._location_by_id.get(player_loc, {})
        raw = loc_def.get("interactables", [])
        if not raw:
            state["location_interactables"] = []
            return

        used = set(state.get("used_interactables", []))
        skill_growth = state.get("skill_growth", {})
        result = []
        for item in raw:
            iid = item.get("id", "")
            if not iid:
                continue
            is_used = item.get("one_time") and iid in used
            condition = item.get("condition", "")
            if condition and not is_used and not self._evaluate_condition(condition):
                continue
            entry = {
                "id": iid,
                "name": item.get("name", iid),
                "description": item.get("description", ""),
                "action_hint": item.get("action_hint", ""),
            }
            if is_used:
                entry["used"] = True
            if item.get("required_item"):
                entry["required_item"] = item["required_item"]
            req_skill = item.get("required_skill", {})
            if req_skill:
                skill_name = req_skill.get("skill", "")
                req_level = req_skill.get("level", 1)
                cur_level = skill_growth.get(skill_name, {}).get("level", 0)
                if cur_level < req_level:
                    entry["locked_skill"] = f"{skill_name}≥{req_level}级（当前{cur_level}级）"
            result.append(entry)
        state["location_interactables"] = result

    def _mark_used_interactables(self, player_action: dict):
        """Mark one_time interactables as used if the player action matches."""
        action_text = player_action.get("text", "")
        if not action_text:
            return
        player_loc = self.current_state.get("player", {}).get("location", "")
        loc_def = self._location_by_id.get(player_loc, {})
        raw = loc_def.get("interactables", [])
        if not raw:
            return
        used = self.current_state.setdefault("used_interactables", [])
        for item in raw:
            iid = item.get("id", "")
            if not iid or iid in used:
                continue
            hint = item.get("action_hint", "")
            name = item.get("name", "")
            if not (hint and hint in action_text) and not (name and name in action_text):
                continue
            if item.get("one_time"):
                used.append(iid)
            for loc_id in item.get("reveals", []):
                if not self._is_duplicate_location(self.current_state, loc_id):
                    self.current_state = self.state_manager.reveal_location(
                        self.current_state, loc_id, inplace=True
                    )
            sc = item.get("state_changes", [])
            if sc:
                self.current_state, _ = self.state_manager.apply_changes(
                    self.current_state, sc, inplace=True
                )

    def _compute_time_atmosphere(self):
        """Compute time-of-day atmosphere from game_time and write to state."""
        game_time = self.current_state.get("game_time", "")
        if not game_time:
            return
        try:
            dt = parse_time(game_time)
            if not dt:
                return
        except Exception:
            return
        hour = dt.hour
        _PERIODS = [
            (5, 7, "dawn", "黎明", "dim", "天边泛起微光，空气清冷而新鲜"),
            (7, 12, "morning", "上午", "bright", "阳光明媚，正是活动的好时候"),
            (12, 14, "noon", "午间", "bright", "烈日当空，热浪蒸腾"),
            (14, 17, "afternoon", "午后", "bright", "午后的暖意弥漫"),
            (17, 19, "dusk", "黄昏", "dim", "落日余晖中，影子被拉长"),
            (19, 22, "night", "夜晚", "dark", "夜幕降临，灯火渐明"),
            (22, 24, "late_night", "深夜", "dark", "万籁俱寂，只有虫鸣"),
            (0, 5, "late_night", "深夜", "dark", "夜深人静，世界沉睡"),
        ]
        period = "day"
        label = "白天"
        light = "bright"
        mood = ""
        for start, end, p, l, li, m in _PERIODS:
            if start <= hour < end:
                period, label, light, mood = p, l, li, m
                break
        effects = []
        if light == "dark":
            effects.append("潜行/隐蔽+5")
            effects.append("视觉观察-5")
        elif light == "dim":
            effects.append("潜行/隐蔽+3")
        self.current_state["time_atmosphere"] = {
            "period": period,
            "period_label": label,
            "light_level": light,
            "mood_hint": mood,
            "gameplay_effects": effects,
            "hour": hour,
        }

    def _compute_difficulty_awareness(self):
        """Track recent check results and compute difficulty adjustment."""
        state = self.current_state
        history = state.setdefault("_check_result_history", [])
        # Recent window: last 10 checks
        recent = history[-10:] if len(history) > 10 else history
        if len(recent) < 3:
            return
        successes = sum(1 for r in recent if r in ("success", "critical_success"))
        failures = len(recent) - successes
        rate = successes / len(recent) if recent else 0.5
        if rate <= 0.3:
            momentum = "struggling"
            adjustment = "ease"
            hint = "玩家近期频繁失败，可以给予一些意外的帮助、线索或转机"
        elif rate >= 0.8:
            momentum = "dominating"
            adjustment = "challenge"
            hint = "玩家几乎全部成功，可以适当增加复杂度或引入意外挫折"
        else:
            momentum = "balanced"
            adjustment = "neutral"
            hint = ""
        state["difficulty_awareness"] = {
            "success_rate": round(rate, 2),
            "player_momentum": momentum,
            "adjustment": adjustment,
            "hint_for_ai": hint,
        }

    def _compute_npc_relationship_depth(self):
        """Calculate relationship depth for each known NPC based on interaction counts."""
        dlg = self.current_state.get("npc_dialogue_counts", {})
        enc = self.current_state.get("npc_encounter_counts", {})
        npcs_state = self.current_state.get("npcs", {})
        depths = {}
        all_npc_ids = set()
        for npc in self.script.get("npcs", []):
            all_npc_ids.add(npc["id"])
        for dyn_id in npcs_state:
            if dyn_id not in all_npc_ids:
                all_npc_ids.add(dyn_id)
        for npc_id in all_npc_ids:
            npc_data = npcs_state.get(npc_id, {})
            if not isinstance(npc_data, dict) or not npc_data.get("known"):
                continue
            talks = dlg.get(npc_id, 0)
            meets = enc.get(npc_id, 0)
            score = talks * 3 + meets
            if score >= 30:
                level, label = 4, "挚友"
            elif score >= 18:
                level, label = 3, "朋友"
            elif score >= 10:
                level, label = 2, "熟人"
            elif score >= 4:
                level, label = 1, "点头之交"
            else:
                level, label = 0, "陌生人"
            attitude = npc_data.get("attitude_toward_player", 50)
            if attitude < 30 and level > 1:
                level, label = 1, "点头之交"
            elif attitude < 50 and level > 2:
                level, label = 2, "熟人"
            elif attitude < 65 and level > 3:
                level, label = 3, "朋友"
            depths[npc_id] = {
                "level": level, "label": label,
                "talks": talks, "encounters": meets, "score": score,
            }
        self.current_state["npc_relationship_depths"] = depths

    def _compute_discovery_hints(self):
        """Calculate exploration progress, nearby hints, and achievements."""
        state = self.current_state
        vis = set(state.get("visible_locations", []))
        all_locs = [loc["id"] for loc in self.script.get("locations", [])]
        all_npcs = self.script.get("npcs", [])
        milestones = self.script.get("milestones", [])
        achieved = set(state.get("achieved_milestones", []))

        npcs_met = sum(
            1 for n in state.get("npcs", {}).values()
            if isinstance(n, dict) and n.get("met", n.get("known", False))
        )

        progress = {
            "locations_found": len(vis), "locations_total": len(all_locs),
            "npcs_met": npcs_met, "npcs_total": max(len(all_npcs), len(state.get("npcs", {}))),
            "milestones_done": len(achieved), "milestones_total": len(milestones),
        }

        nearby = []
        player_loc = state.get("player", {}).get("location", "")
        conns = state.get("location_connections", {})
        if player_loc and player_loc in conns:
            for adj in conns[player_loc]:
                if adj not in vis:
                    loc_def = self._location_by_id.get(adj)
                    hint = loc_def.get("discovery_hint", "附近似乎还有未探索的区域...") if loc_def else "附近似乎还有未探索的区域..."
                    nearby.append({"type": "unvisited_location", "hint": hint})
                    if len(nearby) >= 2:
                        break

        for npc in all_npcs:
            npc_id = npc["id"]
            npc_state = state.get("npcs", {}).get(npc_id, {})
            if isinstance(npc_state, dict) and not npc_state.get("known"):
                dl = npc.get("default_location", "")
                if dl and dl == player_loc:
                    nearby.append({"type": "unmet_npc", "hint": f"据说这里有一位{npc.get('name', '神秘人')}..."})
                    if len(nearby) >= 3:
                        break

        npcs_total = max(len(all_npcs), len(state.get("npcs", {})))
        achievements = []
        enc_counts = state.get("npc_encounter_counts", {})
        total_enc = sum(enc_counts.values())
        achievements.append({"id": "explorer", "name": "探索者", "progress": f"{len(vis)}/{max(len(all_locs),1)}", "earned": len(vis) >= len(all_locs) and len(all_locs) > 0})
        achievements.append({"id": "social", "name": "交际家", "progress": f"{npcs_met}/{max(npcs_total,1)}", "earned": npcs_met >= npcs_total and npcs_total > 0})
        achievements.append({"id": "veteran", "name": "冒险老手", "progress": f"{self.turn_number}/50", "earned": self.turn_number >= 50})

        state["discovery_hints"] = {
            "exploration_progress": progress,
            "nearby_hints": nearby,
            "achievements": achievements,
        }

    def _compute_active_effects_summary(self):
        """Aggregate all active effects (persistent states, time atmosphere, weather) into a summary list."""
        state = self.current_state
        summary = []
        _ICONS = {"buff": "\U0001F6E1", "debuff": "\U0001F480", "status": "\U0001F4A0", "environment": "\U0001F30D"}
        for sid in state.get("active_persistent_states", []):
            ps_def = self._ps_by_id.get(sid, {})
            name = ps_def.get("name") or state.get("display_names", {}).get(sid, sid)
            desc = state.get("persistent_state_descriptions", {}).get(sid, ps_def.get("description", ""))
            effects = []
            for eff in ps_def.get("effects", []):
                if isinstance(eff, str):
                    effects.append(eff)
                elif isinstance(eff, dict):
                    effects.append(eff.get("description", str(eff)))
            cat = ps_def.get("category", "status")
            summary.append({"id": sid, "name": name, "description": desc, "source": "persistent_state", "effects": effects, "icon": _ICONS.get(cat, "\U0001F4A0")})
        atmo = state.get("time_atmosphere", {})
        if atmo.get("gameplay_effects"):
            summary.append({"id": "_time", "name": atmo.get("period_label", ""), "description": atmo.get("mood_hint", ""), "source": "time_atmosphere", "effects": atmo["gameplay_effects"], "icon": "\U0001F319" if atmo.get("light_level") == "dark" else "☀"})
        weather = state.get("current_weather", "")
        if weather and isinstance(weather, str):
            w_effects = []
            active_ps = state.get("active_persistent_states", [])
            if "weather_extreme" in active_ps:
                w_effects.append("所有户外活动受阻")
            elif "weather_storm" in active_ps:
                w_effects.append("户外行动困难")
            elif "weather_rain" in active_ps:
                w_effects.append("视野受限")
            if w_effects:
                summary.append({"id": "_weather", "name": weather, "description": "", "source": "weather", "effects": w_effects, "icon": "☁"})
        state["active_effects_summary"] = summary

    # ================================================================
    # Player XP / Level / Skill Tree
    # ================================================================

    def _compute_xp_and_level(self, ctx: dict):
        xp_config = self.script.get("player_character", {}).get("xp_config", {})
        if not xp_config and not self.script.get("player_character", {}).get("skill_trees"):
            return
        state = self.current_state
        state["_level_up_this_turn"] = False
        milestone_xp = xp_config.get("milestone_xp", 25)
        check_success_xp = xp_config.get("check_success_xp", 10)
        thresholds = xp_config.get("level_thresholds", [0, 100, 300, 600, 1000, 1500])

        prev_xp = state.get("player_xp", 0)
        prev_level = state.get("player_level", 1)
        xp_gained = 0

        milestones = ctx.get("achieved_milestones") or []
        xp_gained += len(milestones) * milestone_xp

        check_result = ctx.get("check_result")
        if isinstance(check_result, dict) and check_result.get("outcome") in ("success", "critical_success"):
            xp_gained += check_success_xp
            if check_result.get("outcome") == "critical_success":
                xp_gained += check_success_xp // 2

        if xp_gained <= 0:
            return

        new_xp = prev_xp + xp_gained
        new_level = prev_level
        while new_level + 1 < len(thresholds) and new_xp >= thresholds[new_level]:
            new_level += 1

        state["player_xp"] = new_xp
        state["player_level"] = new_level
        if new_level > prev_level:
            state["_level_up_this_turn"] = True
            self._record_narrative_callback(
                f"经验积累，你的等级提升到了 {new_level} 级",
                ["level_up"], "high",
            )

    def unlock_skill(self, skill_id: str) -> dict:
        trees = self.script.get("player_character", {}).get("skill_trees", [])
        if not trees:
            return {"success": False, "message": "当前剧本没有技能树"}
        all_skills = {}
        for tree in trees:
            for sk in tree.get("skills", []):
                all_skills[sk.get("id", "")] = sk
        skill = all_skills.get(skill_id)
        if not skill:
            return {"success": False, "message": f"技能 {skill_id} 不存在"}

        state = self.current_state
        unlocked = state.setdefault("unlocked_skills", [])
        if skill_id in unlocked:
            return {"success": False, "message": "该技能已解锁"}

        for pre in skill.get("prerequisites", []):
            if pre not in unlocked:
                pre_name = all_skills.get(pre, {}).get("name", pre)
                return {"success": False, "message": f"前置技能「{pre_name}」未解锁"}

        cost = skill.get("cost", 50)
        cur_xp = state.get("player_xp", 0)
        if cur_xp < cost:
            return {"success": False, "message": f"XP 不足（需要 {cost}，当前 {cur_xp}）"}

        state["player_xp"] = cur_xp - cost
        unlocked.append(skill_id)

        effect = skill.get("effect", {})
        if effect:
            changes = []
            if effect.get("target"):
                changes.append({"target": effect["target"], "value": effect.get("value", 0),
                                "op": effect.get("op", "add"), "reason": f"解锁技能：{skill.get('name', skill_id)}"})
            if changes:
                applied, log = self.state_manager.apply_changes(state, changes, inplace=True)
                self.current_state = applied

        return {"success": True, "message": f"解锁了技能「{skill.get('name', skill_id)}」",
                "state": self.current_state}

    # ================================================================
    # Economy / Trading System
    # ================================================================

    def _init_shop_inventories(self):
        """Initialize shop inventories from script location definitions."""
        if "shop_inventories" in self.current_state:
            return
        inventories: dict = {}
        for loc in self.script.get("locations", []):
            for shop in loc.get("shops", []):
                shop_id = shop.get("id", "")
                if not shop_id:
                    continue
                inventories[shop_id] = [
                    {"item_id": it.get("item_id", ""), "name": it.get("name", it.get("item_id", "")),
                     "stock": it.get("stock", -1), "base_price": it.get("base_price", 10),
                     "description": it.get("description", "")}
                    for it in shop.get("items", []) if it.get("item_id")
                ]
        if inventories:
            self.current_state["shop_inventories"] = inventories

    def _get_currency_attribute(self) -> str:
        attrs = self.script.get("player_character", {}).get("attributes", {})
        for key in attrs:
            if isinstance(attrs[key], dict) and attrs[key].get("is_currency"):
                return f"player.attributes.{key}"
            if any(k in key.lower() for k in ("gold", "coin", "money", "金币", "金钱", "银两")):
                return f"player.attributes.{key}"
        return "player.attributes.gold"

    def _get_currency_value(self) -> int:
        val = self.state_manager._get_value(self.current_state, self._get_currency_attribute())
        if isinstance(val, dict):
            val = val.get("value", 0)
        return int(val) if isinstance(val, (int, float)) else 0

    def _calculate_shop_price(self, shop_id: str, base_price: int, shop_def: dict | None = None) -> int:
        if shop_def is None:
            for loc in self.script.get("locations", []):
                for shop in loc.get("shops", []):
                    if shop.get("id") == shop_id:
                        shop_def = shop
                        break
                if shop_def:
                    break
        modifier = 1.0
        if shop_def:
            merchant_id = shop_def.get("merchant_npc", "")
            if merchant_id:
                npc_def = self._npc_by_id.get(merchant_id, {})
                faction = npc_def.get("faction", "")
                if faction:
                    rep = self.current_state.get("faction_reputation", {}).get(faction, {})
                    rep_val = rep.get("value", 50) if isinstance(rep, dict) else 50
                    modifier *= 1.0 - (rep_val - 50) / 200.0
                npc_state = self.current_state.get("npcs", {}).get(merchant_id, {})
                if isinstance(npc_state, dict):
                    attitude = npc_state.get("attitude_toward_player", 50)
                    if attitude < 30:
                        modifier *= 1.3
                    elif attitude > 75:
                        modifier *= 0.9
                depth = self.current_state.get("npc_relationship_depths", {}).get(merchant_id, {})
                if isinstance(depth, dict):
                    level = depth.get("level", 0)
                    if level >= 4:
                        modifier *= 0.8
                    elif level >= 3:
                        modifier *= 0.9
        return max(1, int(base_price * modifier))

    def buy_item(self, shop_id: str, item_id: str) -> dict:
        """Buy an item from a shop."""
        shops = self.current_state.get("shop_inventories", {})
        shop_items = shops.get(shop_id, [])
        item = next((it for it in shop_items if it["item_id"] == item_id), None)
        if not item:
            return {"success": False, "message": "商品不存在"}
        if item["stock"] == 0:
            return {"success": False, "message": "已售罄"}
        price = self._calculate_shop_price(shop_id, item["base_price"])
        current_gold = self._get_currency_value()
        if current_gold < price:
            return {"success": False, "message": f"金币不足（需要{price}，当前{current_gold}）"}
        attr_path = self._get_currency_attribute()
        changes = [{"target": attr_path, "op": "add", "value": -price, "reason": f"购买{item['name']}"}]
        self.current_state, log = self.state_manager.apply_changes(self.current_state, changes, inplace=True)
        inventory = self.current_state.setdefault("inventory", [])
        existing = next((it for it in inventory if it.get("item") == item["name"]), None)
        if existing:
            existing["quantity"] = existing.get("quantity", 1) + 1
        else:
            new_entry = {"item": item["name"], "quantity": 1}
            if item.get("description"):
                new_entry["description"] = item["description"]
            inventory.append(new_entry)
        if item["stock"] > 0:
            item["stock"] -= 1
        return {"success": True, "message": f"购买了{item['name']}（花费{price}金币）",
                "state_changes": log, "state": self.current_state}

    def sell_item(self, shop_id: str, item_name: str) -> dict:
        """Sell an item to a shop."""
        inventory = self.current_state.get("inventory", [])
        inv_item = next((it for it in inventory if it.get("item") == item_name), None)
        if not inv_item or inv_item.get("quantity", 1) <= 0:
            return {"success": False, "message": "你没有这个物品"}
        shop_items = self.current_state.get("shop_inventories", {}).get(shop_id, [])
        sell_price = None
        for it in shop_items:
            if it.get("name") == item_name:
                sell_price = max(1, it["base_price"] // 2)
                break
        if sell_price is None:
            inv_desc = (inv_item.get("description") or "").lower()
            if any(k in inv_desc for k in ("稀有", "珍贵", "legendary", "rare", "epic")):
                sell_price = 50
            elif any(k in inv_desc for k in ("精良", "uncommon", "优质")):
                sell_price = 20
            else:
                sell_price = 5
        attr_path = self._get_currency_attribute()
        changes = [{"target": attr_path, "op": "add", "value": sell_price, "reason": f"出售{item_name}"}]
        self.current_state, log = self.state_manager.apply_changes(self.current_state, changes, inplace=True)
        inv_item["quantity"] = inv_item.get("quantity", 1) - 1
        if inv_item["quantity"] <= 0:
            inventory.remove(inv_item)
        return {"success": True, "message": f"出售了{item_name}（获得{sell_price}金币）",
                "state_changes": log, "state": self.current_state}

    def get_location_shops(self, location_id: str) -> dict:
        """Get shops at a location."""
        loc_def = self._location_by_id.get(location_id, {})
        shops = loc_def.get("shops", [])
        inventories = self.current_state.get("shop_inventories", {})
        result = []
        for shop in shops:
            sid = shop.get("id", "")
            items = inventories.get(sid, [])
            priced_items = []
            for it in items:
                priced_items.append({
                    **it,
                    "price": self._calculate_shop_price(sid, it.get("base_price", 10), shop_def=shop),
                })
            result.append({"id": sid, "name": shop.get("name", sid), "items": priced_items})
        return {"shops": result, "currency": self._get_currency_value(),
                "currency_name": self._get_currency_attribute().split(".")[-1]}

    def use_item(self, item_name: str) -> dict:
        """Use an item from inventory. If it has use_effect, apply deterministically; otherwise return None to signal freeform fallback."""
        inventory = self.current_state.get("inventory", [])
        entry = None
        for it in inventory:
            if it.get("item") == item_name:
                entry = it
                break
        if not entry:
            return {"success": False, "message": f"背包中没有「{item_name}」"}
        if entry.get("quantity", 1) <= 0:
            return {"success": False, "message": f"「{item_name}」已用完"}

        effect = entry.get("use_effect")
        if not effect:
            return {"has_effect": False}

        if effect.get("condition"):
            if not self._evaluate_condition(effect["condition"]):
                return {"success": False, "message": effect.get("fail_message", f"现在无法使用「{item_name}」")}

        state_changes = []
        for sc in effect.get("state_changes", []):
            target = sc.get("target", "")
            value = sc.get("value", sc.get("change", 0))
            if not target:
                continue
            applied, log = self.state_manager.apply_changes(
                self.current_state,
                [{"target": target, "value": value, "op": sc.get("op", "add"), "reason": sc.get("reason", "")}],
                inplace=True,
            )
            self.current_state = applied
            state_changes.extend(log)

        for loc_id in effect.get("reveals", []):
            vis = self.current_state.setdefault("visible_locations", [])
            if loc_id not in vis:
                vis.append(loc_id)

        for sid in effect.get("activate_states", []):
            active = self.current_state.setdefault("active_persistent_states", [])
            if sid not in active:
                active.append(sid)

        if effect.get("consumable", True):
            entry["quantity"] = entry.get("quantity", 1) - 1
            if entry["quantity"] <= 0:
                inventory.remove(entry)

        msg = effect.get("success_message", f"你使用了「{item_name}」")
        self._record_narrative_callback(msg, ["item_use"], "low")
        return {
            "success": True, "has_effect": True,
            "message": msg,
            "state_changes": state_changes,
            "state": self.current_state,
        }

    def interact_with_object(self, interactable_id: str) -> dict:
        """Deterministic interaction with a location interactable. Returns has_rules=False to signal freeform fallback."""
        player_loc = self.current_state.get("player", {}).get("location", "")
        loc_def = self._location_by_id.get(player_loc, {})
        raw = loc_def.get("interactables", [])
        item = next((i for i in raw if i.get("id") == interactable_id), None)
        if not item:
            return {"success": False, "message": "这里没有这个可互动的东西"}
        used = self.current_state.get("used_interactables", [])
        if item.get("one_time") and interactable_id in used:
            return {"success": False, "message": f"「{item.get('name', interactable_id)}」已经使用过了"}
        has_rules = bool(item.get("required_item") or item.get("state_changes") or item.get("reveals") or item.get("required_skill"))
        if not has_rules:
            return {"has_rules": False, "action_hint": item.get("action_hint", item.get("name", interactable_id))}
        req_skill = item.get("required_skill", {})
        if req_skill:
            skill_name = req_skill.get("skill", "")
            req_level = req_skill.get("level", 1)
            cur_level = self.current_state.get("skill_growth", {}).get(skill_name, {}).get("level", 0)
            if cur_level < req_level:
                return {"success": False, "message": f"需要「{skill_name}」达到{req_level}级才能操作（当前{cur_level}级）"}
        req_item = item.get("required_item")
        if req_item:
            inv_names = [it.get("item", "") for it in self.current_state.get("inventory", []) if isinstance(it, dict)]
            if req_item not in inv_names:
                return {"success": False, "message": f"需要「{req_item}」才能进行此操作"}
        state_changes = []
        for sc in item.get("state_changes", []):
            applied, log = self.state_manager.apply_changes(self.current_state, [sc], inplace=True)
            self.current_state = applied
            state_changes.extend(log)
        revealed = []
        for loc_id in item.get("reveals", []):
            vis = self.current_state.setdefault("visible_locations", [])
            if loc_id not in vis:
                vis.append(loc_id)
                dn = self.current_state.get("display_names", {})
                revealed.append(dn.get(loc_id, loc_id))
        if item.get("one_time"):
            self.current_state.setdefault("used_interactables", []).append(interactable_id)
        msg = item.get("success_message", f"你与「{item.get('name', interactable_id)}」互动了")
        self._record_narrative_callback(msg, ["interact"], "low")
        return {"success": True, "has_rules": True, "message": msg, "state_changes": state_changes, "revealed_locations": revealed, "state": self.current_state}

    def _record_check_outcome(self, outcome: str):
        """Append a check outcome to the rolling history for difficulty awareness."""
        history = self.current_state.setdefault("_check_result_history", [])
        history.append(outcome)
        if len(history) > 20:
            self.current_state["_check_result_history"] = history[-20:]

    def _record_information(self, parsed: dict, ctx: dict):
        """Record significant events as information entries for NPC propagation."""
        network = self.current_state.setdefault("information_network", [])
        present_npcs = ctx.get("present_npc_ids", [])

        if not present_npcs:
            return

        new_infos = []
        # Significant attitude changes
        for ac in parsed.get("npc_attitude_changes", []):
            if abs(ac.get("change", 0)) >= 10:
                npc_name = self._get_npc_display_name(ac.get("npc_id", ""))
                new_infos.append({
                    "fact": f"玩家与{npc_name}关系发生变化({ac.get('reason','')})",
                    "tags": ["social", "attitude"],
                    "spread_chance": 0.3,
                })
        # Triggered consequences
        if ctx.get("triggered_consequences"):
            for csq in ctx["triggered_consequences"]:
                desc = csq.get("description", csq.get("id", ""))
                new_infos.append({
                    "fact": f"发生了: {desc}",
                    "tags": ["consequence"],
                    "spread_chance": 0.5,
                })
        # Milestones
        if ctx.get("achieved_milestones"):
            for ms in ctx["achieved_milestones"]:
                desc = ms.get("description", ms.get("id", ""))
                new_infos.append({
                    "fact": f"里程碑达成: {desc}",
                    "tags": ["milestone"],
                    "spread_chance": 0.4,
                })
        # Combat dice rolls
        for d in ctx.get("dice_dicts", []):
            dtype = d.get("type", "")
            if any(k in dtype for k in ("combat", "attack", "战斗")):
                new_infos.append({
                    "fact": f"玩家参与了战斗（{d.get('description', '')}）",
                    "tags": ["violence", "combat"],
                    "spread_chance": 0.5,
                })
                break

        for info in new_infos:
            info_id = f"info_{self.turn_number}_{len(network)}"
            network.append({
                "id": info_id,
                "origin_turn": self.turn_number,
                "fact": info["fact"][:200],
                "known_by": list(present_npcs),
                "spread_chance": info.get("spread_chance", 0.3),
                "distortion": 0,
                "max_spread": 5,
                "tags": info.get("tags", []),
            })
        # Cap network size
        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

    def _propagate_information(self):
        """Spread information between NPCs based on relationships."""
        network = self.current_state.get("information_network", [])
        if not network:
            return
        npc_rels = self.current_state.get("npc_relationships_global", {})

        for info in network:
            known = set(info.get("known_by", []))
            if len(known) >= info.get("max_spread", 5):
                continue
            new_knowers = set()
            for knower in list(known):
                # Organization spread
                for oid, members in self._org_members.items():
                    if knower in members:
                        for m in members:
                            if m not in known and m not in new_knowers:
                                if random.random() < 0.6:
                                    new_knowers.add(m)
                # Relationship spread
                for _rk, rel in (npc_rels or {}).items():
                    a = rel.get("from") or rel.get("a", "")
                    b = rel.get("to") or rel.get("b", "")
                    partner = ""
                    if a == knower:
                        partner = b
                    elif b == knower:
                        partner = a
                    if partner and partner not in known and partner not in new_knowers:
                        if random.random() < info.get("spread_chance", 0.3):
                            new_knowers.add(partner)
                if len(known) + len(new_knowers) >= info.get("max_spread", 5):
                    break

            for nk in new_knowers:
                info["known_by"].append(nk)
                if random.random() < 0.3:
                    info["distortion"] = min(3, info.get("distortion", 0) + 1)

    def _consolidate_information_memory(self):
        """Consolidate old information_network entries into NPC lorebook (long-term memory).

        - Entries older than 8 turns with high spread (known_by >= 3) or important tags
          get written into the NPC's lorebook entry as persistent memory.
        - Low-importance old entries are simply forgotten (deleted).
        - Keeps information_network as a short-term buffer.
        """
        network = self.current_state.get("information_network", [])
        if not network:
            return

        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        _IMPORTANT_TAGS = {"lie", "broken_promise", "betrayal", "secret", "faction_event"}
        _AGE_THRESHOLD = 8
        _SPREAD_THRESHOLD = 3

        to_consolidate: dict[str, list[str]] = {}  # npc_id → [fact_lines]
        to_keep: list[dict] = []

        for info in network:
            age = self.turn_number - info.get("origin_turn", self.turn_number)
            if age < _AGE_THRESHOLD:
                to_keep.append(info)
                continue

            tags = set(info.get("tags", []))
            known_by = info.get("known_by", [])
            is_important = bool(tags & _IMPORTANT_TAGS) or len(known_by) >= _SPREAD_THRESHOLD

            if is_important and lb:
                fact = info.get("fact", "")
                distortion = info.get("distortion", 0)
                if distortion >= 2:
                    fact = f"（传言）{fact}"
                for npc_id in known_by:
                    to_consolidate.setdefault(npc_id, []).append(fact)
            # else: forgotten — not kept, not consolidated

        if not to_consolidate:
            if len(to_keep) != len(network):
                self.current_state["information_network"] = to_keep
            return

        display_names = self.current_state.get("display_names", {})
        for npc_id, facts in to_consolidate.items():
            if not facts:
                continue
            npc_name = display_names.get(npc_id, npc_id)
            entry_id = f"_memory_{npc_id}"
            fact_text = "\n".join(f"- {f}" for f in facts[-5:])
            existing = next((e for e in lb.entries if e.id == entry_id), None)
            if existing:
                old_content = existing.content or ""
                old_lines = [l for l in old_content.split("\n") if l.startswith("- ")]
                combined = old_lines + [f"- {f}" for f in facts[-5:]]
                if len(combined) > 8:
                    combined = combined[-8:]
                new_content = f"{npc_name}知道的事:\n" + "\n".join(combined)
                lb.update_entry(entry_id, new_content)
            else:
                content = f"{npc_name}知道的事:\n{fact_text}"
                lb.add_entries([{
                    "id": entry_id,
                    "keys": [npc_name, npc_id],
                    "content": content,
                    "priority": 55,
                    "position": "after_world",
                    "entry_type": "npc_memory",
                }])

        self.current_state["information_network"] = to_keep

    def _check_memory_echoes(self, ctx: dict):
        """Check for memory echo triggers and write matching echoes into state.

        Triggers:
        - location_revisit: player returned to a location with significant past events
        - npc_reunion: player re-encounters an NPC after 5+ turns of separation
        - consequence_echo: a pending consequence just triggered, trace its origin
        - moral_echo: a moral alignment axis crossed the ±50 threshold
        """
        echoes = []
        state = self.current_state
        log = state.get("adventure_log", [])
        player_loc = state.get("player", {}).get("location", "")

        # --- location_revisit ---
        if player_loc and self.turn_number > 3:
            prev_loc = ctx.get("rollback_state", {}).get("player", {}).get("location", "")
            if prev_loc and prev_loc != player_loc:
                loc_events = []
                for entry in log:
                    if entry.get("type") == "chapter":
                        continue
                    e_turn = entry.get("turn", 0)
                    if self.turn_number - e_turn < 5:
                        continue
                    e_loc = entry.get("location", "")
                    if not e_loc or not self._locations_match(player_loc, e_loc):
                        continue
                    significant = [
                        ev for ev in entry.get("events", [])
                        if ev.get("type") in ("consequence", "milestone", "relationship", "event")
                    ]
                    if significant:
                        loc_events.append((e_turn, significant[0].get("text", "")))
                if loc_events:
                    best = max(loc_events, key=lambda x: x[0])
                    loc_name = state.get("display_names", {}).get(player_loc, player_loc)
                    echoes.append({
                        "type": "location_revisit",
                        "trigger": f"重返{loc_name}",
                        "memory": f"第{best[0]}回合：{best[1]}",
                        "turns_ago": self.turn_number - best[0],
                        "emotional_weight": "high" if self.turn_number - best[0] >= 10 else "medium",
                    })

        # --- npc_reunion ---
        present_npcs = ctx.get("present_npc_ids", [])
        npc_last_seen = state.get("_npc_last_seen_turn", {})
        for npc_id in present_npcs:
            last_turn = npc_last_seen.get(npc_id, 0)
            gap = self.turn_number - last_turn if last_turn else 0
            if gap < 5:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            memory_text = ""
            chat_hist = state.get("npc_chat_history", {}).get(npc_id, [])
            real_chats = [h for h in chat_hist if not h.get("_summary")]
            if real_chats:
                last_chat = real_chats[-1]
                npc_brief = last_chat.get("npc", "")[:60]
                memory_text = f"上次对话中{npc_name}说：「{npc_brief}」"
            if not memory_text:
                for entry in reversed(log):
                    for ev in entry.get("events", []):
                        if npc_id in ev.get("text", "") or npc_name in ev.get("text", ""):
                            memory_text = f"第{entry.get('turn', '?')}回合：{ev['text']}"
                            break
                    if memory_text:
                        break
            if memory_text:
                echoes.append({
                    "type": "npc_reunion",
                    "trigger": f"再次见到{npc_name}",
                    "memory": memory_text,
                    "turns_ago": gap,
                    "emotional_weight": "high" if gap >= 10 else "medium",
                })
        for npc_id in present_npcs:
            npc_last_seen[npc_id] = self.turn_number
        state["_npc_last_seen_turn"] = npc_last_seen

        # --- consequence_echo ---
        triggered_cons = ctx.get("triggered_consequences", [])
        for csq in (triggered_cons or []):
            origin = csq.get("origin_description", csq.get("description", ""))
            origin_turn = csq.get("origin_turn", 0)
            if origin and origin_turn:
                echoes.append({
                    "type": "consequence_echo",
                    "trigger": f"伏线触发：{csq.get('description', '')[:30]}",
                    "memory": f"第{origin_turn}回合种下的因：{origin[:80]}",
                    "turns_ago": self.turn_number - origin_turn,
                    "emotional_weight": "high",
                })

        # --- moral_echo ---
        ma = state.get("moral_alignment", {})
        prev_ma = ctx.get("rollback_state", {}).get("moral_alignment", {})
        _AXIS_LABELS = {
            "mercy_vs_cruelty": ("仁慈", "残忍"),
            "honesty_vs_deception": ("诚实", "欺骗"),
            "order_vs_chaos": ("秩序", "混沌"),
        }
        for axis, (pos, neg) in _AXIS_LABELS.items():
            cur = ma.get(axis, 0)
            old = prev_ma.get(axis, 0)
            if abs(cur) >= 50 and abs(old) < 50:
                label = pos if cur > 0 else neg
                echoes.append({
                    "type": "moral_echo",
                    "trigger": f"你的「{label}」倾向已经根深蒂固",
                    "memory": f"一路走来的选择塑造了你{label}的名声",
                    "turns_ago": 0,
                    "emotional_weight": "high",
                })

        state["memory_echoes"] = echoes[:3]

    def _trace_choice_ripples(self, ctx: dict):
        """Trace causal chains from past player choices to current events."""
        ripples: list[dict] = []
        state = self.current_state

        # Source 1: consequence triggers — they carry origin info
        triggered_cons = ctx.get("triggered_consequences", [])
        for csq in (triggered_cons or []):
            origin_turn = csq.get("origin_turn", 0)
            origin_desc = csq.get("origin_description", "")
            cur_desc = csq.get("description", "")
            if origin_turn and origin_desc:
                ripples.append({
                    "current_event": cur_desc[:60],
                    "cause_turn": origin_turn,
                    "cause_action": origin_desc[:60],
                    "chain": [origin_desc[:40], cur_desc[:40]],
                    "impact_type": "negative" if csq.get("severity") in ("high", "critical") else "neutral",
                })

        # Source 2: NPC attitude big shifts — trace via information_network
        info_net = state.get("information_network", [])
        rollback = ctx.get("rollback_state", {})
        for npc_id, npc_data in state.get("npcs", {}).items():
            if not isinstance(npc_data, dict):
                continue
            cur_att = npc_data.get("attitude_toward_player", 50)
            old_att = rollback.get("npcs", {}).get(npc_id, {}).get("attitude_toward_player", 50) if rollback else cur_att
            delta = cur_att - old_att
            if abs(delta) < 15:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            cause = ""
            cause_turn = 0
            for info in reversed(info_net):
                if info.get("target_npc") == npc_id or npc_id in info.get("known_by", []):
                    cause = info.get("fact", "")[:60]
                    cause_turn = info.get("origin_turn", info.get("turn", 0))
                    break
            if cause and cause_turn:
                ripples.append({
                    "current_event": f"{npc_name}对你的态度{'大幅改善' if delta > 0 else '急剧恶化'}",
                    "cause_turn": cause_turn,
                    "cause_action": cause,
                    "chain": [cause[:40], f"态度变化{delta:+d}"],
                    "impact_type": "positive" if delta > 0 else "negative",
                })

        # Source 3: faction reputation changes
        factions = state.get("faction_reputation", {})
        old_factions = rollback.get("faction_reputation", {}) if rollback else {}
        for fid, cur_rep in factions.items():
            old_rep = old_factions.get(fid, cur_rep)
            if isinstance(cur_rep, dict):
                cur_val = cur_rep.get("value", 50)
                old_val = old_rep.get("value", 50) if isinstance(old_rep, dict) else 50
            else:
                cur_val = cur_rep
                old_val = old_rep if not isinstance(old_rep, dict) else old_rep.get("value", 50)
            delta = cur_val - old_val
            if abs(delta) < 10:
                continue
            fname = state.get("display_names", {}).get(fid, fid)
            ripples.append({
                "current_event": f"{fname}声望{'提升' if delta > 0 else '下降'}{abs(delta)}点",
                "cause_turn": self.turn_number,
                "cause_action": f"本回合的行动影响了{fname}",
                "chain": [f"声望变化{delta:+d}"],
                "impact_type": "positive" if delta > 0 else "negative",
            })

        state["choice_ripples"] = ripples[:3]

    # ================================================
    #  ADVENTURE LOG AI POLISH
    # ================================================

    async def _maybe_ai_polish_chapters(self):
        """后台任务：对标记了 needs_ai_summary 的 chapter 条目进行 AI 润色。"""
        if not self.ai_provider:
            return
        log = self.current_state.get("adventure_log", [])
        for entry in log:
            if entry.get("type") != "chapter" or not entry.get("needs_ai_summary"):
                continue
            raw_summary = entry.get("summary", "")
            if not raw_summary:
                entry.pop("needs_ai_summary", None)
                continue
            try:
                messages = [{"role": "user", "content":
                    f"将以下游戏冒险日志摘要润色为一段流畅的50-80字叙事（第三人称），保留关键信息：\n\n{raw_summary}"}]
                polished = await self.ai_provider.generate(
                    messages,
                    system="你是游戏日志润色助手。输出简洁叙事摘要，不要JSON或标记。",
                    max_tokens=256,
                )
                polished = polished.strip()
                if polished and len(polished) > 10:
                    async with self._state_lock:
                        entry["summary"] = polished
                        entry["events"] = [{"type": "chapter_summary", "text": polished}]
            except Exception as e:
                logger.debug("chapter AI润色失败: %s", e)
            entry.pop("needs_ai_summary", None)

    # ================================================
    #  NPC OFFSCREEN & DYNAMIC NPC & RELATIONSHIP NETWORK
    # ================================================

    def _inject_offscreen_info(self, sim_results: list):
        """Record significant offscreen NPC actions into information_network.

        This lets NPC-to-NPC info propagation carry offscreen events,
        so in-scene NPCs may reference what happened elsewhere.
        """
        network = self.current_state.setdefault("information_network", [])
        for item in sim_results:
            npc_id = item.get("npc_id", "")
            action = item.get("action", "")
            if not npc_id or not action or len(action) < 5:
                continue
            has_effects = bool(item.get("world_effects")) or bool(item.get("npc_rel_updates"))
            gp = item.get("goal_progress")
            goal_significant = gp is not None and (int(gp) if isinstance(gp, (int, float, str)) else 0) >= 80
            if not has_effects and not goal_significant:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            info_id = f"offscreen_{self.turn_number}_{npc_id}"
            network.append({
                "id": info_id,
                "origin_turn": self.turn_number,
                "fact": f"{npc_name}在别处：{action[:60]}",
                "known_by": [npc_id],
                "spread_chance": 0.4,
                "distortion": 0,
                "max_spread": 4,
                "tags": ["offscreen", "npc_action"],
            })
        if len(network) > 30:
            self.current_state["information_network"] = network[-30:]

    def _record_npc_offscreen(self, update: dict):
        """Record an offscreen NPC action into the offscreen log.
        Also update the NPC's current location if provided."""
        npc_id = update.get("npc_id", "")
        if not npc_id:
            return
        log = self.current_state.setdefault("npc_offscreen_log", {})
        npc_log = log.setdefault(npc_id, [])
        npc_log.append({
            "turn": self.turn_number,
            "time": self.current_state.get("game_time", ""),
            "action": update.get("action", ""),
            "location": update.get("location", ""),
            "mood": update.get("mood", ""),
        })
        # Keep per-NPC log bounded
        if len(npc_log) > 20:
            log[npc_id] = npc_log[-15:]
        # Update NPC's current location in state so dialogue button reflects it
        new_loc = update.get("location", "")
        if new_loc:
            npc_state = self.current_state.get("npcs", {}).get(npc_id)
            if isinstance(npc_state, dict):
                npc_state["current_location"] = new_loc
        mood = update.get("mood", "")
        if mood:
            npc_state = self.current_state.get("npcs", {}).get(npc_id)
            if isinstance(npc_state, dict):
                npc_state["current_mood"] = mood
        # Sync to lorebook
        self._sync_offscreen_to_lorebook(npc_id, log.get(npc_id, []))

    def _sync_offscreen_to_lorebook(self, npc_id: str, npc_log: list[dict]):
        """Create/update a dynamic lorebook entry summarising an NPC's recent offscreen activity."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb or not npc_log:
            return
        npc_name = self._get_npc_display_name(npc_id)
        display_names = self.current_state.get("display_names", {})
        recent = npc_log[-3:]
        lines = []
        for r in recent:
            loc = display_names.get(r.get("location", ""), r.get("location", ""))
            action = r.get("action", "")
            if action:
                lines.append(f"- {action}（在{loc}）" if loc else f"- {action}")
        if not lines:
            return
        content = f"{npc_name}的近期离场动态:\n" + "\n".join(lines)
        entry_id = f"_offscreen_{npc_id}"
        existing = any(e.id == entry_id for e in lb.entries)
        if existing:
            lb.update_entry(entry_id, content)
        else:
            lb.add_entries([{
                "id": entry_id,
                "keys": [npc_name, npc_id],
                "content": content,
                "priority": 60,
                "position": "after_world",
                "entry_type": "npc_offscreen",
            }])

    def _sync_npc_interaction_memory(self):
        """将 npc_interaction_log 中的互动记录浓缩为 lorebook 长期记忆。"""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        interaction_log = self.current_state.get("npc_interaction_log", {})
        for npc_id, logs in interaction_log.items():
            if len(logs) < 3:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            significant = sorted(logs, key=lambda x: abs(x.get("attitude_delta", 0)), reverse=True)[:3]
            recent = logs[-2:]
            merged = list({id(x): x for x in significant + recent}.values())
            lines = []
            for entry in sorted(merged, key=lambda x: x["turn"]):
                delta = entry.get("attitude_delta", 0)
                delta_mark = f"(好感{'+'if delta>0 else ''}{delta})" if delta else ""
                lines.append(f"- 第{entry['turn']}回合: 玩家说「{entry['player'][:30]}」{delta_mark}")
            entry_id = f"_interact_{npc_id}"
            content = f"{npc_name}与玩家的互动记忆:\n" + "\n".join(lines)
            existing = any(e.id == entry_id for e in lb.entries)
            if existing:
                lb.update_entry(entry_id, content)
            else:
                lb.add_entries([{
                    "id": entry_id,
                    "keys": [npc_name, npc_id],
                    "content": content,
                    "priority": 58,
                    "position": "after_world",
                    "entry_type": "npc_memory",
                }])

    def _build_lore_context_for_core(self, activated_lore: list) -> str:
        """Build lorebook context relevant to state inference (location, danger, NPC status)."""
        if not activated_lore:
            return ""
        relevant_types = {
            "scene_physical", "npc_offscreen", "npc_profile", "npc_relationship",
            "npc_memory", "consequence_context", "key_event", "manual", "location_desc", "pc_identity",
        }
        lines = []
        for entry in activated_lore[:6]:
            if entry.entry_type not in relevant_types:
                continue
            snippet = entry.content[:100].replace("\n", " ")
            lines.append(f"- {snippet}")
        if not lines:
            return ""
        return "\n\n## 世界知识（state推演参考）\n" + "\n".join(lines)

    def _build_lore_summary_for_plot(self, activated_lore: list) -> str:
        """Build a compact lorebook summary for Stage 1 plot_decision context.

        P1: 只预注入 constant=True 的常驻条目。
        非常驻条目由 Agent 通过 query_lorebook 工具按需获取。
        """
        if not activated_lore:
            return ""
        # P1: 仅保留常驻条目，非常驻条目通过 query_lorebook 工具按需拉取
        constant_lore = [e for e in activated_lore if getattr(e, "constant", False)]
        if not constant_lore:
            return ""
        _PLOT_TYPE_PRIORITY = {
            "event_context": 0, "story_event": 0, "key_event": 0,
            "npc_relationship": 1, "npc_profile": 2, "location_desc": 2,
            "pc_identity": 3, "player_behavior": 4,
        }
        sorted_lore = sorted(
            constant_lore,
            key=lambda e: (_PLOT_TYPE_PRIORITY.get(e.entry_type, 2), -e.priority),
        )
        lines = []
        for entry in sorted_lore[:8]:
            label = entry.comment or entry.id
            snippet = entry.content[:80].replace("\n", " ")
            vis = getattr(entry, "visibility", "public")
            if vis == "hidden":
                knowers = getattr(entry, "known_by_npcs", [])
                if knowers:
                    names = "/".join(self._get_npc_display_name(n) for n in knowers[:3])
                    lines.append(f"- [机密-仅{names}知道][{label}] {snippet}")
                else:
                    lines.append(f"- [机密-未公开][{label}] {snippet}")
            elif vis == "world":
                lines.append(f"- [世界设定][{label}] {snippet}")
            else:
                lines.append(f"- [{label}] {snippet}")
        if not lines:
            return ""
        return "## 已激活世界知识（决策时可参考）\n" + "\n".join(lines)

    def _accumulate_npc_dialogue_style(self, narrative: str, present_npc_ids: list[str]):
        """Extract NPC dialogue from narrative and write style lorebook entries after enough samples."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        npc_dialogues = self._extract_npc_dialogues(narrative, present_npc_ids)
        cache = self.current_state.setdefault("_npc_dialogue_samples", {})
        for npc_id, texts in npc_dialogues.items():
            if not texts:
                continue
            samples = cache.setdefault(npc_id, [])
            samples.extend(texts[:3])
            if len(samples) > 20:
                cache[npc_id] = samples[-15:]
            if len(samples) < 5:
                continue
            combined = "".join(samples)
            clen = len(combined)
            if clen < 10:
                continue
            npc_name = self._get_npc_display_name(npc_id)
            traits = []
            modal_counts = {}
            for p in self._MODAL_PARTICLES:
                cnt = combined.count(p)
                if cnt >= 2:
                    modal_counts[p] = cnt
            if modal_counts:
                top = sorted(modal_counts.items(), key=lambda x: -x[1])[:3]
                traits.append(f"常用语气词: {'、'.join(p for p, _ in top)}")
            avg_len = sum(len(t) for t in samples) / len(samples)
            if avg_len < 8:
                traits.append("句式简短")
            elif avg_len > 25:
                traits.append("句式较长")
            excl_rate = combined.count("！") / clen
            ques_rate = combined.count("？") / clen
            if excl_rate > 0.05:
                traits.append("语气强烈，多用感叹")
            if ques_rate > 0.05:
                traits.append("多用疑问")
            if not traits:
                continue
            entry_id = f"_voice_{npc_id}"
            content = f"{npc_name}的说话风格: {'; '.join(traits)}"
            existing = any(e.id == entry_id for e in lb.entries)
            if existing:
                lb.update_entry(entry_id, content)
            else:
                lb.add_entries([{
                    "id": entry_id,
                    "keys": [npc_name, npc_id],
                    "content": content,
                    "priority": 45,
                    "position": "after_world",
                    "entry_type": "npc_voice_style",
                }])

    def _sync_attitude_to_lorebook(self, npc_att_changes: list[dict]):
        """Write lorebook entries for significant NPC attitude shifts (|change| >= 10)."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        for item in npc_att_changes:
            npc_id = item.get("npc_id") or item.get("npc") or item.get("name", "")
            change = item.get("change", 0)
            if isinstance(change, str):
                try:
                    change = int(change)
                except (ValueError, TypeError):
                    continue
            if abs(change) < 10:
                continue
            reason = item.get("reason", "")
            dim = item.get("dimension", "trust")
            opinion = item.get("opinion", "")
            npc_name = self._get_npc_display_name(npc_id)
            entry_id = f"_att_{npc_id}"
            dim_zh = {"trust": "信任", "affection": "好感", "fear": "畏惧"}.get(dim, dim)
            direction = "上升" if change > 0 else "下降"
            line = f"{npc_name}对玩家的{dim_zh}{direction}了{abs(change)}点"
            if reason:
                line += f"，原因: {reason}"
            if opinion:
                line += f"。{npc_name}的看法: {opinion}"
            existing = next((e for e in lb.entries if e.id == entry_id), None)
            if existing:
                old = existing.content
                lines = old.split("\n")
                lines.append(f"- 第{self.turn_number}回合: {line}")
                if len(lines) > 6:
                    lines = lines[:1] + lines[-5:]
                lb.update_entry(entry_id, "\n".join(lines))
            else:
                content = f"{npc_name}与玩家的关系变化:\n- 第{self.turn_number}回合: {line}"
                lb.add_entries([{
                    "id": entry_id,
                    "keys": [npc_name, npc_id],
                    "content": content,
                    "priority": 65,
                    "position": "after_world",
                    "entry_type": "npc_attitude",
                }])

    def _sync_key_events_to_lorebook(self):
        """Sync key_events to lorebook: one entry per event, keyed by entity names found in text."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        key_events = self.current_state.get("key_events", [])
        if not key_events:
            return
        npc_names = {n.get("name", n["id"]): n["id"] for n in self.script.get("npcs", []) if n.get("name")}
        for dyn_id, dyn_st in self.current_state.get("npcs", {}).items():
            if dyn_id not in self._npc_by_id and isinstance(dyn_st, dict) and dyn_st.get("name"):
                npc_names[dyn_st["name"]] = dyn_id
        loc_names = set(self.current_state.get("display_names", {}).values())
        existing_ids = {e.id for e in lb.entries}
        for evt in key_events[-20:]:
            turn = evt.get("turn", 0)
            text = evt.get("event", "")
            if not text:
                continue
            entry_id = f"_keyevt_{turn}_{hash(text) % 10000}"
            if entry_id in existing_ids:
                continue
            keys = []
            for name in npc_names:
                if name in text:
                    keys.append(name)
            for loc in loc_names:
                if loc and len(loc) >= 2 and loc in text:
                    keys.append(loc)
            if not keys:
                words = [w for w in text.split() if len(w) >= 3]
                keys = words[:2] if words else [text[:8]]
            lb.add_entries([{
                "id": entry_id,
                "keys": keys,
                "content": f"第{turn}回合关键事件: {text}",
                "priority": 50,
                "position": "after_world",
                "entry_type": "key_event",
            }])
            existing_ids.add(entry_id)

    def _register_dynamic_npc(self, npc_data: dict):
        """Register a dynamically created NPC into the game state."""
        npc_id = npc_data.get("id", "")
        if not npc_id:
            return
        npcs = self.current_state.setdefault("npcs", {})
        # Don't overwrite existing NPC
        if npc_id in npcs:
            return
        npcs[npc_id] = {
            "name": npc_data.get("name", npc_id),
            "attitude_toward_player": npc_data.get("attitude_toward_player", 50),
            "known": npc_data.get("known", True),
            "met": npc_data.get("met", npc_data.get("known", False)),
            "default_location": npc_data.get("location", ""),
            "current_location": npc_data.get("location", ""),
            "bio": npc_data.get("bio", ""),
            "personality": npc_data.get("personality", ""),
            "capabilities": npc_data.get("capabilities", ""),
            "title": npc_data.get("title", ""),
            "organizations": npc_data.get("organizations") or self._migrate_npc_org_fields(npc_data),
            "superior": npc_data.get("superior", ""),
        }
        # Add to display_names
        dn = self.current_state.setdefault("display_names", {})
        dn[npc_id] = npc_data.get("name", npc_id)
        # Initialize relationship
        rels = self.current_state.get("player", {}).setdefault("relationships", {})
        if npc_id not in rels:
            rels[npc_id] = {
                "trust": npc_data.get("attitude_toward_player", 50),
                "affection": npc_data.get("attitude_toward_player", 50),
                "fear": 0,
            }
        # P1-9: 同步更新 script.npcs 列表和 prompt_builder 缓存，
        # 确保 _build_npc_section 能迭代到动态 NPC。
        script_npcs = self.script.setdefault("npcs", [])
        if not any(n.get("id") == npc_id for n in script_npcs):
            script_npcs.append(npc_data)
        self._npc_by_id[npc_id] = npc_data
        self.prompt_builder._npc_name_map[npc_id] = npc_data.get("name", npc_id)

    _NPC_SYNC_FIELDS = ("name", "organizations", "personality", "bio", "capabilities", "title", "superior")

    @staticmethod
    def _migrate_npc_org_fields(npc: dict) -> list[dict]:
        """AI 返回旧格式 organization/rank 时转为 organizations 数组。"""
        org_id = npc.get("organization", "")
        rank = npc.get("rank")
        if org_id:
            entry: dict = {"org_id": org_id}
            if rank is not None:
                entry["rank"] = rank
            return [entry]
        return []

    def _sync_npc_fields_to_script(self, state: dict, *, dirty_npc_ids: set[str] | None = None):
        """Sync mutable NPC fields from runtime state back to script.npcs.

        If dirty_npc_ids is provided, only check those NPCs instead of all.
        """
        script_npcs = self.script.get("npcs", [])
        script_npc_map = {n["id"]: n for n in script_npcs if n.get("id")}
        changed_npc_ids = []
        changed_org_ids = set()
        npc_items = state.get("npcs", {}).items()
        if dirty_npc_ids:
            npc_items = ((nid, state.get("npcs", {}).get(nid)) for nid in dirty_npc_ids)
        for npc_id, npc_st in npc_items:
            if not isinstance(npc_st, dict):
                continue
            script_npc = script_npc_map.get(npc_id)
            if not script_npc:
                continue
            dirty = False
            for field in self._NPC_SYNC_FIELDS:
                if field in npc_st and script_npc.get(field) != npc_st[field]:
                    if field == "organizations":
                        for om in (script_npc.get("organizations") or []):
                            if om.get("org_id"):
                                changed_org_ids.add(om["org_id"])
                        for om in (npc_st[field] if isinstance(npc_st[field], list) else []):
                            if isinstance(om, dict) and om.get("org_id"):
                                changed_org_ids.add(om["org_id"])
                    script_npc[field] = npc_st[field]
                    dirty = True
            if "name" in npc_st:
                self.prompt_builder._npc_name_map[npc_id] = npc_st["name"]
            if dirty:
                changed_npc_ids.append(npc_id)
                for om in (npc_st.get("organizations") or script_npc.get("organizations") or []):
                    if isinstance(om, dict) and om.get("org_id"):
                        changed_org_ids.add(om["org_id"])
        if changed_npc_ids or changed_org_ids:
            self.prompt_builder.refresh_kg_entries(changed_npc_ids, list(changed_org_ids))
            if self.vector_memory:
                self._sync_kg_entries_to_vector(changed_npc_ids, list(changed_org_ids))

    _ORG_MUTABLE_FIELDS = frozenset({"leader", "stance", "description"})

    def _sync_kg_entries_to_vector(self, npc_ids: list[str], org_ids: list[str]):
        """Re-index updated KG lorebook entries into vector memory (non-blocking)."""
        if not self.vector_memory:
            return
        batch = []
        for npc_id in npc_ids:
            entry = next((e for e in self.prompt_builder.lorebook.entries if e.id == f"_kg_npc_{npc_id}"), None)
            if entry and entry.content and len(entry.content) >= 20:
                batch.append((entry.id, entry.content, {"entry_type": entry.entry_type, "comment": entry.comment}))
        for org_id in org_ids:
            entry = next((e for e in self.prompt_builder.lorebook.entries if e.id == f"_kg_org_{org_id}"), None)
            if entry and entry.content and len(entry.content) >= 20:
                batch.append((entry.id, entry.content, {"entry_type": entry.entry_type, "comment": entry.comment}))
        if batch:
            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, self.vector_memory.add_lorebook_batch, batch)
            except RuntimeError:
                self.vector_memory.add_lorebook_batch(batch)

    def _apply_org_changes_to_script(self, changes: list[dict]):
        """Apply state_changes targeting organizations.{id}.{field} directly to script."""
        org_by_id = {o["id"]: o for o in self.script.get("organizations", []) if o.get("id")}
        changed_org_ids = []
        for change in changes:
            target = change.get("target", "")
            if not target.startswith("organizations."):
                continue
            parts = target.split(".", 2)
            if len(parts) < 3:
                continue
            org_id, field = parts[1], parts[2]
            org = org_by_id.get(org_id)
            if org and field in self._ORG_MUTABLE_FIELDS:
                org[field] = change.get("value", "")
                if org_id not in changed_org_ids:
                    changed_org_ids.append(org_id)
        if changed_org_ids:
            self.prompt_builder.refresh_kg_entries([], changed_org_ids)
            if self.vector_memory:
                self._sync_kg_entries_to_vector([], changed_org_ids)

    # 关系网上限 — 超过后按优先级淘汰动态 NPC 间的旧条目
    _REL_NET_MAX = 80

    @staticmethod
    def _normalize_rel_key(a: str, b: str, directed: bool = False) -> str:
        """归一化关系 key，无向关系按字典序排列防止 a_b / b_a 重复。"""
        if directed:
            return f"{a}_{b}"
        return f"{min(a, b)}_{max(a, b)}"

    def _apply_npc_relationship_updates(
        self, updates: list, *, state: dict | None = None,
    ):
        """Apply NPC-NPC relationship updates to global + known networks.

        Each update has:
          a, b: NPC IDs
          type: relationship type (友好, 敌对, etc.)
          description: relationship description
          scope: "global" | "known" | "both"
          discovery_reason: (for known scope) how the player discovered it

        Includes key normalization (undirected relations use sorted key)
        and cap enforcement to prevent unbounded growth.
        """
        target = state if state is not None else self.current_state
        global_net = target.setdefault("npc_relationships_global", {})
        known_net = target.setdefault("npc_relationships_known", {})

        for upd in updates:
            a = upd.get("a", "")
            b = upd.get("b", "")
            if not a or not b:
                continue
            key = self._normalize_rel_key(a, b)
            existing = global_net.get(key)
            entry = {
                "a": a,
                "b": b,
                "type": upd.get("type", existing.get("type", "中立") if existing else "中立"),
                "description": upd.get("description", existing.get("description", "") if existing else ""),
                "intensity": upd.get("intensity", existing.get("intensity", 50) if existing else 50),
                "_turn": self.turn_number,
            }
            if upd.get("known") is not None:
                entry["known"] = bool(upd["known"])
            elif existing and "known" in existing:
                entry["known"] = existing["known"]
            if upd.get("met") is not None:
                entry["met"] = bool(upd["met"])
            elif existing and "met" in existing:
                entry["met"] = existing["met"]
            scope = upd.get("scope", "global")

            if scope in ("global", "both"):
                global_net[key] = entry
            if scope in ("known", "both"):
                known_entry = dict(entry)
                if upd.get("discovery_reason"):
                    known_entry["discovery_reason"] = upd["discovery_reason"]
                known_net[key] = known_entry

        # 淘汰机制：超过上限时删除动态 NPC 间最旧的条目
        for net in (global_net, known_net):
            if len(net) <= self._REL_NET_MAX:
                continue
            predefined_npc_ids = {
                n["id"] for n in self.script.get("npcs", [])
                if isinstance(n, dict) and n.get("id")
            }
            evict_candidates = []
            for k, v in net.items():
                ids = {v.get("a", ""), v.get("b", ""), v.get("from", ""), v.get("to", "")} - {""}
                if not ids & predefined_npc_ids:
                    evict_candidates.append((k, v.get("_turn", 0)))
            evict_candidates.sort(key=lambda x: x[1])
            to_remove = len(net) - self._REL_NET_MAX
            for k, _ in evict_candidates[:to_remove]:
                net.pop(k, None)

    async def _retry_ai_call(self, messages: list, system: str, parser, label: str = "", stage: str = ""):
        """O-5: 通用 AI 调用 + 单次重试辅助方法。"""
        kwargs = self._stage_kwargs(stage) if stage else {}
        try:
            raw = await self.ai_provider.generate(messages, system=system, **kwargs)
            return parser(raw)
        except Exception as e:
            logging.getLogger(__name__).debug("%s failed (attempt 1): %s", label, e)
            try:
                await asyncio.sleep(1)
                raw = await self.ai_provider.generate(messages, system=system, **kwargs)
                return parser(raw)
            except Exception as e2:
                logging.getLogger(__name__).debug("%s failed (attempt 2): %s", label, e2)
                return None

    async def _run_offscreen_simulation(self):
        """Asynchronously simulate off-screen NPC actions.

        Called at the end of each turn. Non-blocking — errors are logged and swallowed.
        Active NPCs (interacted in last 5 turns) get individual simulation.
        Inactive NPCs get batch simulation.
        """
        logger = logging.getLogger(__name__)

        if not self.ai_provider:
            return

        player_loc = self.current_state.get("player", {}).get("location", "")
        game_time = self.current_state.get("game_time", "")
        world_bg = self.script.get("world_background", "")[:200]
        npcs_state = self.current_state.get("npcs", {})
        offscreen_log = self.current_state.get("npc_offscreen_log", {})

        # Find NPCs not at the player's location (cooldown: skip if simulated within 2h game time)
        offscreen_npcs = []
        current_time_parsed = None
        if game_time:
            try:
                current_time_parsed = parse_time(game_time)
            except Exception:
                pass

        for npc_id, npc_data in npcs_state.items():
            if not isinstance(npc_data, dict):
                continue
            npc_loc = self._get_npc_location(npc_id)
            if not npc_loc or not player_loc:
                continue
            if self._locations_match(player_loc, npc_loc):
                continue  # NPC is with the player
            logs = offscreen_log.get(npc_id, [])
            if logs:
                last_time = logs[-1].get("time", "")
                if last_time and current_time_parsed:
                    try:
                        last_parsed = parse_time(last_time)
                        if last_parsed and current_time_parsed:
                            diff = current_time_parsed - last_parsed
                            if diff.total_seconds() < 7200:  # 2小时内跳过
                                continue
                    except Exception:
                        pass
                elif logs[-1].get("turn", 0) >= self.turn_number - 2:
                    continue  # 时间解析失败时回退到回合数判断
            offscreen_npcs.append((npc_id, npc_data, npc_loc))

        if not offscreen_npcs:
            return

        # Classify: active (interacted in last 5 turns) vs inactive
        active_npcs = []
        inactive_npcs = []
        for npc_id, npc_data, npc_loc in offscreen_npcs:
            logs = offscreen_log.get(npc_id, [])
            recent_turns = [l for l in logs if l.get("turn", 0) >= self.turn_number - 5]
            # Also check relationship changes as sign of activity
            if len(recent_turns) >= 2:
                active_npcs.append((npc_id, npc_data, npc_loc))
            else:
                inactive_npcs.append((npc_id, npc_data, npc_loc))

        try:
            # 统一批量模拟：活跃NPC包含上次行动信息以获得更连贯的模拟
            all_npcs_to_sim = []
            completed_goals = {(g["id"] if isinstance(g, dict) else g) for g in self.current_state.get("completed_npc_goals", [])}
            for npc_id, npc_data, npc_loc in active_npcs:
                npc_name = npc_data.get("name", npc_id)
                npc_bio = self._npc_by_id.get(npc_id, {}).get("bio", "")[:50]
                npc_personality = npc_data.get("personality", "")[:15]
                last_log = offscreen_log.get(npc_id, [])
                recent_action = last_log[-1]["action"] if last_log else ""
                line = f"- {npc_name}(id:{npc_id}): 位于{npc_loc}"
                if npc_personality:
                    line += f", 性格:{npc_personality}"
                if npc_bio:
                    line += f", {npc_bio}"
                if recent_action:
                    line += f", 上次行动: {recent_action}"
                npc_goals = self._npc_by_id.get(npc_id, {}).get("goals", [])
                active_goals = [g for g in npc_goals if f"{npc_id}:{g.get('id','')}" not in completed_goals]
                if active_goals:
                    hints = [f"{g.get('description','')}" + (f"({g.get('progress_hint','')})" if g.get('progress_hint') else "") for g in active_goals[:2]]
                    line += f", 目标: {'; '.join(hints)}"
                all_npcs_to_sim.append((npc_id, line))

            for npc_id, npc_data, npc_loc in inactive_npcs:
                npc_name = npc_data.get("name", npc_id)
                npc_personality = npc_data.get("personality", "")[:15]
                line = f"- {npc_name}(id:{npc_id}): 位于{npc_loc}"
                if npc_personality:
                    line += f", 性格:{npc_personality}"
                npc_goals = self._npc_by_id.get(npc_id, {}).get("goals", [])
                active_goals = [g for g in npc_goals if f"{npc_id}:{g.get('id','')}" not in completed_goals]
                if active_goals:
                    hints = [g.get("description", "") for g in active_goals[:2]]
                    line += f", 目标: {'; '.join(hints)}"
                all_npcs_to_sim.append((npc_id, line))

            if all_npcs_to_sim:
                npc_lines = [line for _, line in all_npcs_to_sim]

                # 注入 blueprint 活跃剧情线上下文
                bp_context = ""
                bp = self.current_state.get("plot_blueprint", {})
                bp_threads = bp.get("plot_threads", [])
                if bp_threads:
                    bp_lines = []
                    for thread in bp_threads[:5]:
                        for stage in thread.get("stages", []):
                            status = stage.get("status", "pending")
                            if status == "active":
                                npcs_str = ", ".join(thread.get("related_npcs", []))
                                bp_lines.append(f"- {thread.get('name', thread.get('id',''))}: {stage.get('description', '')}（相关: {npcs_str}）")
                                break
                            elif status == "pending":
                                break
                    if bp_lines:
                        bp_context = "\n活跃剧情线（NPC行动须与之一致）:\n" + "\n".join(bp_lines) + "\n"

                weather = self.current_state.get("current_weather", "")
                atmo = self.current_state.get("time_atmosphere", {})
                env_line = ""
                if weather or atmo:
                    parts = []
                    if weather:
                        parts.append(f"天气:{weather}")
                    period = atmo.get("period_label", "")
                    if period:
                        parts.append(f"时段:{period}")
                    if parts:
                        env_line = f"\n环境: {', '.join(parts)}（NPC行为须受环境影响：恶劣天气应躲避、夜间多数NPC应休息）"

                batch_prompt = f"""简述以下NPC此刻在做什么（每人一句话），返回JSON列表。
时间: {game_time} | 背景: {world_bg}{env_line}
{bp_context}NPC列表:
{chr(10).join(npc_lines)}
返回格式: [{{"npc_id":"xxx","action":"..","location":"..","goal_progress":0-100,"world_effects":[{{"target":"state.path","op":"add","value":1}}]}}, ...]
goal_progress: 如果NPC有目标，估算完成百分比(0-100)。无目标则省略。
world_effects: 如果NPC行动对世界状态有显著影响，列出状态变更。通常省略。
如果有NPC之间发生了新的认识或接触，追加 npc_rel_updates 字段：
{{"npc_id":"xxx","action":"..","location":"..","npc_rel_updates":[{{"target":"其他npc_id","known":true,"met":true}}]}}
如果NPC之间的互动改变了彼此关系（合作、冲突、帮助等），追加 rel_change 字段：
{{"npc_id":"xxx","action":"..","rel_change":[{{"target":"另一npc_id","delta":10,"reason":"原因"}}]}}
delta范围-20~20，正=关系改善，负=关系恶化。通常省略。"""

                data_list = await self._retry_ai_call(
                    [{"role": "user", "content": batch_prompt}],
                    "你是NPC行动模拟器。只返回JSON列表，不要其他内容。",
                    self._parse_simple_json_list,
                    label="Batch NPC sim",
                    stage="knowledge_graph",
                )
                if data_list:
                    async with self._state_lock:
                        rel_updates = []
                        goal_progress = self.current_state.setdefault("npc_goal_progress", {})
                        for item in data_list:
                            npc_id = item.get("npc_id", "")
                            if npc_id:
                                self._record_npc_offscreen({
                                    "npc_id": npc_id,
                                    "action": item.get("action", ""),
                                    "location": item.get("location", ""),
                                    "mood": item.get("mood", ""),
                                })
                                # Goal progress tracking
                                gp = item.get("goal_progress")
                                if gp is not None:
                                    try:
                                        gp = int(gp)
                                    except (ValueError, TypeError):
                                        gp = None
                                if gp is not None:
                                    prev_gp = goal_progress.get(npc_id, {}).get("progress", 0)
                                    goal_progress[npc_id] = {
                                        "progress": min(100, max(0, gp)),
                                        "last_action": item.get("action", ""),
                                        "turn": self.turn_number,
                                    }
                                    if gp >= 100 and prev_gp < 100:
                                        npc_name = self.current_state.get("npcs", {}).get(npc_id, {}).get("name", npc_id)
                                        self._record_narrative_callback(
                                            f"{npc_name}已完成了自己的目标，这可能对世界产生深远影响",
                                            [f"npc_goal_complete:{npc_id}"], "high",
                                        )
                                # World effects
                                for we in item.get("world_effects", []):
                                    target = we.get("target", "")
                                    if not target:
                                        continue
                                    val = we.get("value", 0)
                                    op = we.get("op", "add")
                                    if op in ("add", "subtract") and isinstance(val, (int, float)) and abs(val) > 30:
                                        val = 30 if val > 0 else -30
                                    elif op == "set" and isinstance(val, (int, float)):
                                        try:
                                            old = self.state_manager._get_value(self.current_state, target)
                                            if isinstance(old, (int, float)) and abs(val - old) > 30:
                                                val = old + (30 if val > old else -30)
                                        except Exception:
                                            pass
                                    try:
                                        self.state_manager.apply_changes(
                                            self.current_state,
                                            [{"target": target, "value": val,
                                              "op": op, "reason": f"NPC {npc_id} 行动影响"}],
                                            inplace=True,
                                        )
                                    except Exception:
                                        pass
                                for ru in item.get("npc_rel_updates", []):
                                    target = ru.get("target", "")
                                    if target:
                                        rel_updates.append({
                                            "a": npc_id, "b": target,
                                            "type": ru.get("type", "中立"),
                                            "description": ru.get("description", ""),
                                            "scope": "global",
                                            "known": ru.get("known"),
                                            "met": ru.get("met"),
                                        })
                                for rc in item.get("rel_change", []):
                                    if not isinstance(rc, dict):
                                        continue
                                    rc_target = rc.get("target", "")
                                    rc_delta = rc.get("delta", 0)
                                    if not rc_target or not isinstance(rc_delta, (int, float)):
                                        continue
                                    rc_delta = max(-20, min(20, int(rc_delta)))
                                    rc_key = self._normalize_rel_key(npc_id, rc_target)
                                    rels_g = self.current_state.setdefault("npc_relationships_global", {})
                                    rc_rel = rels_g.setdefault(rc_key, {"a": npc_id, "b": rc_target, "type": "中立", "intensity": 50})
                                    new_int = max(0, min(100, rc_rel.get("intensity", 50) + rc_delta))
                                    rc_rel["intensity"] = new_int
                                    if new_int < 20:
                                        rc_rel["type"] = "敌对"
                                    elif new_int > 80:
                                        rc_rel["type"] = "友好"
                                    elif new_int < 35:
                                        rc_rel["type"] = "紧张"
                                    elif new_int > 65:
                                        rc_rel["type"] = "亲近"
                                    else:
                                        rc_rel["type"] = "中立"
                        if rel_updates:
                            self._apply_npc_relationship_updates(rel_updates)
                        self._inject_offscreen_info(data_list)
                        self._sync_npc_interaction_memory()

        except Exception as e:
            logger.warning("Offscreen NPC simulation error: %s", e)

    @staticmethod
    def _parse_simple_json(text: str) -> dict | None:
        """Extract a JSON object from text that may contain extra content."""
        # Try direct parse
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
        # Find the first '{' and try to find its matching '}' (supports nesting)
        start = text.find('{')
        if start == -1:
            return None
        depth = 0
        in_string = False
        i = start
        while i < len(text):
            c = text[i]
            if in_string:
                if c == '\\':
                    i += 2  # skip escaped character
                    continue
                if c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except json.JSONDecodeError:
                            return None
            i += 1
        return None

    # ── MetaEventBus handlers ───────────────────────────────────────

    # --- Wrappers for migrated existing background tasks ---

    async def _handle_summarize_history(self, meta_ctx: dict):
        await self._maybe_summarize_history()

    async def _handle_analyze_play_style(self, meta_ctx: dict):
        await self._maybe_analyze_play_style()

    async def _handle_offscreen_simulation(self, meta_ctx: dict):
        await self._run_offscreen_simulation()

    async def _handle_ai_polish_chapters(self, meta_ctx: dict):
        await self._maybe_ai_polish_chapters()

    async def _handle_story_director(self, meta_ctx: dict):
        await self._run_story_director()

    # --- New system-level handlers ---

    async def _handle_lorebook_evolution(self, meta_ctx: dict):
        """Unified lorebook maintenance via two parallel subtasks:

        1. Maintenance: rewrite/disable/enable existing entries affected by this turn
        2. Expansion: add new knowledge entries based on plot direction and time progression

        Absorbs responsibilities of the former _director_task_lorebook and
        _maybe_expand_lorebook into a single entry point.
        """
        state_changes = meta_ctx.get("state_changes", [])
        narrative = meta_ctx.get("narrative", "")
        lore_context = self.current_state.pop("_pending_lore_context", None)
        if not state_changes and not narrative and not lore_context:
            return

        game_time = self.current_state.get("game_time", "")
        old_time = meta_ctx.get("old_time", "")
        new_time = meta_ctx.get("new_time", "")

        # Detect significant time jump
        time_jump_desc = ""
        if old_time and new_time:
            try:
                dt_old = parse_time(old_time)
                dt_new = parse_time(new_time)
                if dt_old and dt_new and (dt_old.year != dt_new.year or dt_old.month != dt_new.month):
                    time_jump_desc = f"{old_time} → {new_time}"
            except Exception:
                pass

        # Run maintenance and expansion in parallel
        maintenance_task = self._lore_evo_maintenance(
            state_changes, narrative, game_time,
        )
        expansion_task = self._lore_evo_expansion(
            game_time, time_jump_desc, lore_context,
        )
        results = await asyncio.gather(maintenance_task, expansion_task, return_exceptions=True)

        maintenance_updates = results[0] if isinstance(results[0], list) else []
        expansion_updates = results[1] if isinstance(results[1], list) else []

        all_updates = maintenance_updates + expansion_updates
        if not all_updates:
            return

        await self._apply_lore_evolution_updates(all_updates)

    async def _lore_evo_maintenance(
        self, state_changes: list, narrative: str, game_time: str,
    ) -> list:
        """Subtask 1: Maintain existing lorebook entries (rewrite/disable/enable)."""
        if not state_changes and not narrative:
            return []

        EVOLVABLE_TYPES = {
            "pc_identity", "npc_profile", "npc_relationship",
            "location_desc", "event_context", "evolution",
            "dynamic", "world_sync", "story_event", "player_behavior",
        }

        # Build scan text for relevance filtering
        scan_text = (narrative[:500] + " " + json.dumps(state_changes[:10], ensure_ascii=False)[:500]).lower()

        # Split entries into "relevant" (full detail) vs "background" (id only)
        relevant_entries = []
        background_ids = []
        disabled_candidates = []

        for entry in self.prompt_builder.lorebook.entries:
            if not entry.enabled and entry.entry_type in EVOLVABLE_TYPES:
                disabled_candidates.append({
                    "id": entry.id, "comment": entry.comment,
                    "content": entry.content[:150], "status": "disabled",
                })
                continue
            if not entry.enabled:
                continue
            if entry.constant and entry.entry_type not in EVOLVABLE_TYPES:
                continue

            # Relevance check: activated this turn OR keywords in scan_text
            is_relevant = entry.last_activated_turn == self.turn_number
            if not is_relevant:
                for k in entry.keys[:5]:
                    if k and k.lower() in scan_text:
                        is_relevant = True
                        break

            if is_relevant:
                relevant_entries.append({
                    "id": entry.id, "type": entry.entry_type,
                    "comment": entry.comment, "content": entry.content[:300],
                })
            else:
                background_ids.append(f"{entry.id}({entry.comment or ','.join(entry.keys[:2])})")

        if not relevant_entries and not disabled_candidates:
            return []

        change_summary = json.dumps(state_changes[:20], ensure_ascii=False)[:800]
        system = (
            "你是知识库维护助手。根据本回合的叙事和状态变化，判断哪些条目需要更新。\n"
            "操作类型：\n"
            "- rewrite: 内容已过时，用新内容替换\n"
            "- disable: 条目完全失效（角色死亡/退场、地点永久毁坏等）\n"
            "- enable: 之前被禁用的条目重新生效\n\n"
            "只处理下方【相关条目】中的条目。如无需变更，输出空数组 []。\n"
            "输出 JSON 数组:\n"
            '[{"id":"条目ID","action":"rewrite|disable|enable",'
            '"new_content":"rewrite时提供新内容","new_keys":["更新后的关键词"]}]\n'
            "最多 3 条操作。"
        )
        user_msg = f"当前游戏时间: {game_time or '未知'}\n"
        if change_summary:
            user_msg += f"\n状态变化:\n{change_summary}\n"
        if narrative:
            user_msg += f"\n叙事:\n{narrative[:400]}\n"
        user_msg += f"\n【相关条目】（可能受本轮影响）:\n{json.dumps(relevant_entries, ensure_ascii=False)}\n"
        if disabled_candidates:
            user_msg += f"\n【已禁用条目】（可enable）:\n{json.dumps(disabled_candidates[:8], ensure_ascii=False)}\n"
        if background_ids:
            user_msg += f"\n【其他条目ID】（未受影响，仅供参考）: {'; '.join(background_ids[:30])}\n"

        result = await self._retry_ai_call(
            [{"role": "user", "content": user_msg}], system,
            parser=self._parse_simple_json_list,
            label="lore_evo_maintenance", stage="knowledge_graph",
        )
        return result if isinstance(result, list) else []

    async def _lore_evo_expansion(
        self, game_time: str, time_jump_desc: str, lore_context: dict | None,
    ) -> list:
        """Subtask 2: Expand lorebook with new knowledge entries."""
        # Only expand when there's a reason: time jump, new dynamic content, or periodic
        evo_count = len(self.current_state.get("lorebook_evolutions", []))
        periodic_trigger = (evo_count % 3 == 0)
        if not time_jump_desc and not lore_context and not periodic_trigger:
            return []

        # Existing entry IDs for dedup (no content needed)
        existing_ids = [
            e.id for e in self.prompt_builder.lorebook.entries
            if e.enabled and not e.id.startswith("_kg_")
        ]

        # Plot blueprint for direction
        bp = self.current_state.get("plot_blueprint", {})
        active_stages = []
        if bp.get("plot_threads"):
            for t in bp["plot_threads"]:
                for s in t.get("stages", []):
                    if s.get("status") == "active":
                        active_stages.append(f"{t.get('name','')}: {s.get('description','')}")

        system = (
            "你是世界知识库扩展助手。根据剧情方向和世界进展，生成新的知识条目。\n"
            "规则：\n"
            "- 新条目用第三人称客观视角，每条 80-150 字\n"
            "- 不要与已有条目重复\n"
            "- 内容应是有长期参考价值的知识（角色背景/历史事件/地理/组织信息）\n"
            "- 不要记录琐碎日常或临时状态\n"
            "- 如果提供了外部参考资料，优先基于这些资料确保事实准确，但须适配世界观\n\n"
            "如无需新增，输出空数组 []。\n"
            "输出 JSON 数组:\n"
            '[{"id":"_dyn_xxx","action":"add","new_content":"条目内容",'
            '"new_keys":["关键词1","关键词2"],"comment":"简短标签",'
            '"entry_type":"npc_profile|location_desc|event_context|story_event"}]\n'
            "最多 3 条。"
        )

        user_msg = f"当前游戏时间: {game_time or '未知'}\n"
        if active_stages:
            user_msg += f"活跃剧情方向: {'; '.join(active_stages)}\n"
        if time_jump_desc:
            user_msg += f"时间跨度: {time_jump_desc}（请补充该期间的重大世界事件）\n"
        if lore_context:
            parts = []
            for desc in lore_context.get("new_nodes", []):
                if desc:
                    parts.append(f"新剧情节点: {desc}")
            for desc in lore_context.get("new_events", []):
                if desc:
                    parts.append(f"新事件: {desc}")
            if parts:
                user_msg += "本轮新增动态内容:\n" + "\n".join(f"- {p}" for p in parts) + "\n"
        user_msg += f"\n已有条目ID（避免重复）: {', '.join(existing_ids[:50])}\n"

        # Web search grounding
        should_search = bool(time_jump_desc) or bool(lore_context)
        if should_search:
            world_bg = self.script.get("world_background", "")[:200]
            recent_events = "；".join(
                e.get("event", "")[:40]
                for e in self.current_state.get("key_events", [])[-5:]
            )
            pc = self.current_state.get("player", {})
            try:
                reference = await self._search_for_expansion(world_bg, recent_events, pc)
                if reference:
                    user_msg += f"\n{reference}"
            except Exception:
                pass

        result = await self._retry_ai_call(
            [{"role": "user", "content": user_msg}], system,
            parser=self._parse_simple_json_list,
            label="lore_evo_expansion", stage="knowledge_graph",
        )
        return result if isinstance(result, list) else []

    async def _apply_lore_evolution_updates(self, updates: list):
        """Apply combined results from maintenance + expansion subtasks."""
        async with self._state_lock:
            dynamic = self.current_state.setdefault("dynamic_lorebook", [])
            vector_batch = []
            applied = 0
            for upd in updates:
                if not isinstance(upd, dict) or applied >= 6:
                    continue
                entry_id = upd.get("id", "")
                action = upd.get("action", "")
                if action == "disable":
                    self.prompt_builder.lorebook.update_entry_enabled(entry_id, False)
                    applied += 1
                elif action == "enable":
                    self.prompt_builder.lorebook.update_entry_enabled(entry_id, True)
                    applied += 1
                elif action == "rewrite" and upd.get("new_content"):
                    self.prompt_builder.lorebook.update_entry(
                        entry_id, content=upd["new_content"],
                        keys=upd.get("new_keys"),
                    )
                    for dl in dynamic:
                        if isinstance(dl, dict) and dl.get("id") == entry_id:
                            dl["content"] = upd["new_content"]
                            if upd.get("new_keys"):
                                dl["keys"] = upd["new_keys"]
                            break
                    if self.vector_memory and len(upd["new_content"]) >= 20:
                        vector_batch.append((
                            entry_id, upd["new_content"],
                            {"entry_type": upd.get("entry_type", "")},
                        ))
                    applied += 1
                elif action == "add" and upd.get("new_content") and entry_id:
                    existing = any(
                        e.id == entry_id for e in self.prompt_builder.lorebook.entries
                    )
                    if not existing:
                        new_entry = {
                            "id": entry_id,
                            "keys": upd.get("new_keys", []),
                            "content": upd["new_content"],
                            "comment": upd.get("comment", ""),
                            "entry_type": upd.get("entry_type", "dynamic"),
                            "priority": 80,
                            "position": "after_world",
                            "scan_depth": 3,
                            "enabled": True,
                        }
                        self.prompt_builder.lorebook.add_entries([new_entry])
                        dynamic.append(new_entry)
                        if self.vector_memory and len(upd["new_content"]) >= 20:
                            vector_batch.append((
                                entry_id, upd["new_content"],
                                {"entry_type": new_entry["entry_type"]},
                            ))
                        applied += 1
            if vector_batch:
                self._schedule_background_task(
                    self._async_lorebook_vector_sync(vector_batch)
                )
            evolutions = self.current_state.setdefault("lorebook_evolutions", [])
            evolutions.append({
                "turn": self.turn_number,
                "updates": [
                    {"id": u.get("id"), "action": u.get("action")}
                    for u in updates[:6] if isinstance(u, dict)
                ],
            })

            # Smart capacity management for dynamic lorebook
            MAX_DYNAMIC = 40
            if len(dynamic) > MAX_DYNAMIC:
                PROTECTED_TYPES = {"npc_profile", "npc_relationship"}
                stale_threshold = self.turn_number - 5
                scored = []
                for i, dl in enumerate(dynamic):
                    if not isinstance(dl, dict):
                        scored.append((i, 999))
                        continue
                    dl_id = dl.get("id", "")
                    entry_obj = None
                    for e in self.prompt_builder.lorebook.entries:
                        if e.id == dl_id:
                            entry_obj = e
                            break
                    if entry_obj and entry_obj.entry_type in PROTECTED_TYPES:
                        scored.append((i, 999))
                        continue
                    last_active = entry_obj.last_activated_turn if entry_obj else 0
                    is_speculative = dl.get("speculative", False)
                    score = last_active
                    if is_speculative and last_active < stale_threshold:
                        score -= 1000
                    scored.append((i, score))
                scored.sort(key=lambda x: x[1])
                to_remove = len(dynamic) - MAX_DYNAMIC
                remove_indices = set(scored[i][0] for i in range(to_remove))
                for idx in remove_indices:
                    dl = dynamic[idx]
                    if isinstance(dl, dict) and dl.get("id"):
                        self.prompt_builder.lorebook.remove_entry(dl["id"])
                        if self.vector_memory:
                            self.vector_memory.remove_lorebook(dl["id"])
                dynamic[:] = [dl for i, dl in enumerate(dynamic) if i not in remove_indices]

    async def _handle_narrative_consistency(self, meta_ctx: dict):
        """RAG-based contradiction detection against past history."""
        narrative = meta_ctx.get("narrative", "")
        if not narrative or len(narrative) < 50:
            return
        loop = asyncio.get_running_loop()
        hits = await loop.run_in_executor(
            None, self.vector_memory.query,
            narrative[:300], 5, [self.turn_number],
        )
        if not hits:
            return
        past_context = "\n".join(
            f"[第{h['turn']}回合] {h['text'][:200]}" for h in hits
        )
        system = (
            "你是一个叙事一致性检查助手。对比当前叙事与过去的记录，"
            "找出事实矛盾（例如：已死的NPC又出现、已丢失的物品又被使用、"
            "地点描述前后不一致等）。\n"
            "如果没有矛盾，输出空数组 []。\n"
            "如有矛盾，输出 JSON 数组: "
            '[{"contradiction":"矛盾描述","correction_hint":"修正建议"}]'
        )
        user_msg = (
            f"当前叙事（第{self.turn_number}回合）:\n{narrative[:600]}\n\n"
            f"过去记录:\n{past_context}"
        )
        result = await self._retry_ai_call(
            [{"role": "user", "content": user_msg}], system,
            parser=self._parse_simple_json_list,
            label="narrative_consistency", stage="summary",
        )
        if not result or not isinstance(result, list):
            return
        async with self._state_lock:
            for item in result[:3]:
                if not isinstance(item, dict):
                    continue
                hint = item.get("correction_hint", "")
                if hint:
                    self._record_narrative_callback(
                        f"[一致性修正] {hint}",
                        tags=["consistency"], priority="high",
                    )

    async def _handle_player_behavior_profiling(self, meta_ctx: dict):
        """Analyze player behavior patterns and update implicit lorebook entries."""
        branch = self.world_tree.get_active_branch()
        window = branch[-8:] if len(branch) >= 8 else branch
        actions = []
        for node in window:
            action = node.get("player_action")
            if action:
                text = action.get("text", "") if isinstance(action, dict) else str(action)
                if text:
                    actions.append(f"T{node.get('turn_number', '?')}: {text}")
        if len(actions) < 3:
            return

        existing_profile = self.current_state.get("player_behavior_profile", {})
        existing_tags = existing_profile.get("tags", [])

        system = (
            "你是一个玩家行为分析助手。根据玩家最近的行动序列，"
            "提取行为模式标签和隐性偏好。输出 JSON:\n"
            '{"tags":["外交倾向","收集癖",...], '
            '"preferences":"一句话总结", '
            '"lorebook_entries":[{"id":"player_pref_XXX","keys":[...],'
            '"content":"...","comment":"玩家倾向"}]}'
        )
        user_msg = (
            f"已有标签: {existing_tags}\n\n"
            f"最近行动:\n" + "\n".join(actions)
        )
        result = await self._retry_ai_call(
            [{"role": "user", "content": user_msg}], system,
            parser=self._parse_simple_json,
            label="player_profiling", stage="summary",
        )
        if not result or not isinstance(result, dict):
            return

        async with self._state_lock:
            self.current_state["player_behavior_profile"] = {
                "tags": result.get("tags", [])[:10],
                "preferences": result.get("preferences", "")[:100],
                "last_turn": self.turn_number,
            }
            new_entries = result.get("lorebook_entries", [])
            for entry_data in new_entries[:3]:
                if not isinstance(entry_data, dict) or not entry_data.get("id"):
                    continue
                entry_data.setdefault("priority", 60)
                entry_data.setdefault("position", "after_world")
                entry_data.setdefault("scan_depth", 3)
                entry_data["entry_type"] = "player_behavior"
                entry_data["enabled"] = True
                existing = any(
                    e.id == entry_data["id"]
                    for e in self.prompt_builder.lorebook.entries
                )
                if existing:
                    self.prompt_builder.lorebook.update_entry(
                        entry_data["id"],
                        content=entry_data.get("content", ""),
                        keys=entry_data.get("keys"),
                    )
                else:
                    self.prompt_builder.lorebook.add_entries([entry_data])

    async def _handle_story_feedback(self, meta_ctx: dict):
        """Pipeline output → story tree feedback: quest auto-completion + NPC attitude events."""
        narrative = meta_ctx.get("narrative", "")
        if not narrative:
            return

        async with self._state_lock:
            self._check_quest_keywords(narrative)

            npc_thresholds = self.script.get("settings", {}).get("npc_attitude_events", {})
            if npc_thresholds:
                for npc_id, thresholds in npc_thresholds.items():
                    npc_data = self.current_state.get("npcs", {}).get(npc_id, {})
                    if not isinstance(npc_data, dict):
                        continue
                    attitude = npc_data.get("attitude_toward_player", 50)
                    for threshold in thresholds:
                        evt = threshold.get("event", "")
                        val = threshold.get("value", 0)
                        op = threshold.get("op", ">=")
                        fired_key = f"_npc_att_evt_{npc_id}_{evt}"
                        if self.current_state.get(fired_key):
                            continue
                        should_fire = (op == ">=" and attitude >= val) or (op == "<=" and attitude <= val)
                        if should_fire:
                            self._fire_event_dual(evt)
                            self.current_state[fired_key] = True

    async def _handle_event_stage(self, meta_ctx: dict):
        """Async event CRUD stage: AI evaluates narrative and proposes event changes."""
        narrative = meta_ctx.get("narrative", "")
        if not narrative:
            return

        # 读取阶段：短暂持锁
        async with self._state_lock:
            event_data = self.event_engine.get_events_for_prompt(self.current_state)
            parsed_summary = self._build_parsed_summary(meta_ctx)
            action_text = meta_ctx.get("player_action", {}).get("text", "")
            turn = self.turn_number

        messages, system = self.prompt_builder.build_event_stage_prompt(
            narrative, action_text, event_data, parsed_summary,
        )

        # AI 调用：不持锁
        try:
            raw = await self.ai_provider.generate(
                messages, system=system, max_tokens=4096,
                **self._stage_kwargs("state"),
            )
        except Exception as e:
            logger.error("Event stage AI 生成失败: %s", e)
            return

        parsed_data = self.response_parser._extract_json_from_raw(raw)
        if not isinstance(parsed_data, dict):
            return
        changes = parsed_data.get("event_changes", [])
        if not isinstance(changes, list) or not changes:
            return

        # 写入阶段：短暂持锁
        async with self._state_lock:
            self.event_engine.apply_event_changes(self.current_state, changes, turn)
            lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
            lb_ids = {e.id for e in lb.entries} if lb else None
            for change in changes:
                self._sync_event_to_lorebook(change, self.current_state, lb_ids)
                _fields = change.get("fields", {})
                if change.get("action") == "update" and _fields.get("status") in ("resolved", "failed"):
                    cat = change.get("category", "")
                    if not cat and change.get("id") in self.event_engine.events:
                        cat = self.event_engine.events[change["id"]].category
                    if cat == "thread":
                        self._fire_event_dual(f"thread.{_fields['status']}.{change.get('id', '')}")

    def _build_parsed_summary(self, meta_ctx: dict) -> str:
        """Extract a concise text summary of this turn's state changes from meta_ctx."""
        parts = []
        state_changes = meta_ctx.get("state_changes", [])
        if state_changes:
            sc_lines = [f"{c.get('target', '?')}: {c.get('new', c.get('value', ''))}" for c in state_changes[:5]]
            parts.append("属性变化: " + "; ".join(sc_lines))
        parsed = meta_ctx.get("parsed", {})
        if parsed.get("npc_attitude_changes"):
            att_lines = [f"{c.get('npc_id', '?')}: {c.get('change', 0):+d}" for c in parsed["npc_attitude_changes"][:5]]
            parts.append("NPC态度: " + "; ".join(att_lines))
        events = meta_ctx.get("triggered_events", [])
        if events:
            parts.append("触发事件: " + ", ".join(e.get("event_id", "?") for e in events[:5]))
        return "\n".join(parts) if parts else "无显著变化"

    def _sync_event_to_lorebook(self, change: dict, state: dict, _existing_ids: set | None = None):
        """Unified lorebook sync for event CRUD changes."""
        lb = getattr(self, "prompt_builder", None) and self.prompt_builder.lorebook
        if not lb:
            return
        action = change.get("action", "create")
        eid = change.get("id", "")
        category = change.get("category", "")
        if not eid:
            return

        if action == "delete":
            for prefix in ("_cons_", "_clue_", "_thread_"):
                lb.remove_entry(f"{prefix}{eid}")
            return

        existing_ids = _existing_ids if _existing_ids is not None else {e.id for e in lb.entries}

        if action == "update":
            _fields = change.get("fields", {})
            if not category and eid in self.event_engine.events:
                category = self.event_engine.events[eid].category
            if category == "thread":
                entry_id = f"_thread_{eid}"
                new_status = _fields.get("status", "")
                if new_status in ("resolved", "failed"):
                    lb.remove_entry(entry_id)
                    return
                ev = self.event_engine.events.get(eid)
                name = change.get("name") or (ev.name if ev else eid)
                desc = _fields.get("description") or (ev.description if ev else "")
                content = f"叙事线·{name}: {desc}"
                if new_status == "dormant":
                    content += "（暂时搁置）"
                keys = self._extract_event_lorebook_keys(f"{name} {desc}", state)
                if not keys:
                    keys = [name]
                if entry_id in existing_ids:
                    lb.update_entry(entry_id, content, keys=keys)
                else:
                    lb.add_entries([{
                        "id": entry_id, "keys": keys, "content": content,
                        "priority": 58, "position": "after_world",
                        "entry_type": "narrative_thread",
                    }])
            return

        if action != "create":
            return

        desc = change.get("description", "")
        if not desc:
            return
        keys = self._extract_event_lorebook_keys(desc, state)

        if category == "consequence":
            entry_id = f"_cons_{eid}"
            if entry_id in existing_ids:
                return
            if not keys:
                words = [w for w in desc.split() if len(w) >= 3]
                keys = words[:2] if words else [desc[:8]]
            importance = change.get("importance", "normal")
            content = f"潜在后果: {desc}"
            if importance == "high":
                content += "（高重要性）"
            lb.add_entries([{
                "id": entry_id, "keys": keys, "content": content,
                "priority": 55 if importance == "normal" else 70,
                "position": "after_world", "entry_type": "consequence_context",
            }])

        elif category == "clue":
            entry_id = f"_clue_{eid}"
            if entry_id in existing_ids:
                return
            clue_cat = change.get("metadata", {}).get("category", "事件") if isinstance(change.get("metadata"), dict) else "事件"
            source = change.get("metadata", {}).get("source", "") if isinstance(change.get("metadata"), dict) else ""
            if not keys:
                keys = [clue_cat]
                if source:
                    keys.append(source[:10])
            content = f"[线索·{clue_cat}] {desc}"
            if source:
                content += f"（来源: {source}）"
            lb.add_entries([{
                "id": entry_id, "keys": keys, "content": content,
                "priority": 55, "position": "after_world",
                "entry_type": "clue",
            }])

        elif category == "thread":
            entry_id = f"_thread_{eid}"
            if entry_id in existing_ids:
                return
            name = change.get("name", eid)
            content = f"叙事线·{name}: {desc}"
            if not keys:
                keys = [name]
            lb.add_entries([{
                "id": entry_id, "keys": keys, "content": content,
                "priority": 58, "position": "after_world",
                "entry_type": "narrative_thread",
            }])

    def _extract_event_lorebook_keys(self, text: str, state: dict) -> list[str]:
        """Extract NPC names and location names from text as lorebook keys."""
        keys = []
        npc_names = {n.get("name", n["id"]) for n in self.script.get("npcs", []) if n.get("name")}
        loc_names = set(state.get("display_names", {}).values())
        for name in npc_names:
            if name in text:
                keys.append(name)
        for loc in loc_names:
            if loc and len(loc) >= 2 and loc in text:
                keys.append(loc)
        return keys

    @staticmethod
    def _parse_simple_json_list(text: str) -> list | None:
        """Extract a JSON list from text using bracket balancing."""
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
        start = text.find('[')
        if start == -1:
            return None
        depth = 0
        in_string = False
        i = start
        while i < len(text):
            c = text[i]
            if in_string:
                if c == '\\':
                    i += 2
                    continue
                if c == '"':
                    in_string = False
            else:
                if c == '"':
                    in_string = True
                elif c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        try:
                            data = json.loads(text[start:i + 1])
                            if isinstance(data, list):
                                return data
                        except json.JSONDecodeError:
                            pass
                        return None
            i += 1
        return None
