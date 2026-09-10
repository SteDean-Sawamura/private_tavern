"""
Tavern RPG Multi-Agent System - Prototype Engine
酒馆多Agent协作推演系统原型 + 完整复用酒馆引擎全部模块

核心思路：
1. 加载剧本 → 初始化世界状态 + WorldTree + EventEngine + Lorebook
2. 用户输入操作 → 阶段1并行准备
3. → 事件检查与触发
4. → 阶段2有限视角推理(NPC并行推理)
5. → 阶段3汇聚生成(Outline + Scene)
6. → 添加节点到WorldTree + 应用事件效果 + 保存历史
"""

import json
import re
import time
import sqlite3
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
import copy

from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncio
from ai.openai_provider import OpenAIProvider
from ai.response_parser import ResponseParser
from ai.base import strip_think_tags

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 引入酒馆的复杂结构（同目录下的 engine/）
sys.path.insert(0, _BASE_DIR)
try:
    from engine.world_tree import WorldTree
    from engine.event_engine import EventEngine, GameEvent, EventResult
    from engine.lorebook import Lorebook, LorebookEntry
    from engine.history_summarizer import HistorySummarizer
    from engine.state_manager import StateManager
    from engine.script_loader import ScriptLoader
    from engine.dice import DiceRoller, DiceResult
    from engine.script_variables import ScriptVariables
    from engine.triggers import TriggerEngine
    from engine.meta_events import MetaEventBus, MetaEvent, MetaEventTrigger
    try:
        from engine.vector_memory import VectorMemory, _VECTOR_AVAILABLE
    except ImportError:
        VectorMemory = None
        _VECTOR_AVAILABLE = False
    try:
        from engine.class_system import ClassRegistry
    except ImportError:
        ClassRegistry = None
    TAVERN_MODULES_AVAILABLE = True
except ImportError as e:
    print(f"Warning: Could not import tavern modules: {e}")
    TAVERN_MODULES_AVAILABLE = False



TAVERN_DB = os.path.join(_BASE_DIR, "data", "tavern.db")


def _init_rpg_saves_table():
    conn = sqlite3.connect(TAVERN_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rpg_saves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            script_id TEXT NOT NULL,
            name TEXT NOT NULL,
            turn INTEGER DEFAULT 0,
            game_time TEXT,
            data TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def load_ai_profiles():
    try:
        conn = sqlite3.connect(TAVERN_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, name, provider_type, api_key, model, max_tokens, base_url, is_active FROM ai_profiles"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


_ai_provider: OpenAIProvider | None = None
_llm_state: dict = {"client": None, "model": "", "profile": None}


def _semantic_attr_value(name: str, value, attr_def: dict = None) -> str:
    if isinstance(value, (int, float)):
        v = int(value)
        if v <= 20: level = "极低"
        elif v <= 40: level = "偏低"
        elif v <= 60: level = "中等"
        elif v <= 80: level = "较高"
        else: level = "极高"
        desc = f"{name}：{level}（{v}）"
        if attr_def and attr_def.get("rule"):
            desc += f" — {attr_def['rule'][:40]}"
        return desc
    return f"{name}：{value}"


def _semantic_relationship(npc_name: str, value: int) -> str:
    if value <= 20: return f"{npc_name}（敌对）"
    elif value <= 40: return f"{npc_name}（冷淡）"
    elif value <= 60: return f"{npc_name}（中立）"
    elif value <= 80: return f"{npc_name}（友好）"
    else: return f"{npc_name}（亲密）"


def set_ai_profile(profile: dict):
    global _ai_provider
    _ai_provider = OpenAIProvider(profile)
    _llm_state["client"] = _ai_provider
    _llm_state["model"] = profile.get("model", "")
    _llm_state["profile"] = profile


async def llm_call(system_prompt: str, user_prompt: str, max_tokens: int = 2048, raise_on_error: bool = False) -> str:
    if not _ai_provider:
        if raise_on_error:
            raise RuntimeError("LLM 客户端未配置")
        return ""
    try:
        return await _ai_provider.generate(
            [{"role": "user", "content": user_prompt}],
            system=system_prompt, max_tokens=max_tokens,
        )
    except Exception as e:
        print(f"[LLM ERROR] {e}")
        if raise_on_error:
            raise RuntimeError(f"LLM 调用失败: {e}")
        return ""


async def llm_call_with_tools(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    tool_executor: dict,
    max_tokens: int = 2048,
    max_rounds: int = 7,
) -> tuple[str, list[dict]]:
    if not _ai_provider:
        return "", []

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tool_calls_log = []
    content = ""

    for _ in range(max_rounds):
        try:
            result = await _ai_provider.generate_with_tools(
                messages, tools=tools, max_tokens=max_tokens,
            )
            content = result.get("content", "") or ""
            raw_tool_calls = result.get("tool_calls") or []

            if not raw_tool_calls:
                return content, tool_calls_log

            assistant_msg = {"role": "assistant", "tool_calls": []}
            if content:
                assistant_msg["content"] = content

            tool_result_msgs = []
            for tc in raw_tool_calls:
                tool_name = tc["name"]
                tool_args = tc["arguments"]

                try:
                    if tool_name in tool_executor:
                        executor = tool_executor[tool_name]
                        if asyncio.iscoroutinefunction(executor):
                            exec_result = await executor(**tool_args)
                        else:
                            exec_result = executor(**tool_args)
                    else:
                        exec_result = {"error": f"工具 {tool_name} 未注册"}
                except Exception as e:
                    exec_result = {"error": str(e)}

                tool_calls_log.append({"name": tool_name, "args": tool_args, "result": exec_result})

                assistant_msg["tool_calls"].append({
                    "id": tc["id"], "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps(tool_args, ensure_ascii=False)},
                })
                tool_result_msgs.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": json.dumps(exec_result, ensure_ascii=False)})

            messages.append(assistant_msg)
            messages.extend(tool_result_msgs)

        except Exception as e:
            print(f"[LLM ERROR] {e}")
            break

    return content, tool_calls_log


def _parse_json_from_llm(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")[1:]
        end = next((i for i, l in enumerate(lines) if l.strip() == "```"), len(lines))
        text = "\n".join(lines[:end])
    return ResponseParser._try_parse_json(text) or {}


# ===== Tool Calling 系统 =====

def _tool(name: str, desc: str, props: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required if required is not None else list(props.keys()),
                       "additionalProperties": False}}}


def _build_tool_schemas_and_executors(agent_type: str, world_state: "WorldStateManager", session: "GameSession" = None) -> tuple[list[dict], dict]:
    """为指定 agent 类型构建工具 schemas 和 executors。"""

    def _get_skill_bonus(skill: str) -> int:
        player = world_state.script_data.get("player_character", {})
        attrs = player.get("attributes", {})
        for attr_name, attr_data in attrs.items():
            if isinstance(attr_data, dict):
                for syn in attr_data.get("synonyms", [attr_name]):
                    if skill.lower() == syn.lower():
                        val = attr_data.get("value", 50)
                        return (val - 10) // 2 if isinstance(val, (int, float)) else 0
        return 0

    # --- Executors ---
    async def query_state(category: str, key: str) -> dict:
        result = world_state.query_info(f"{category}_attr" if category == "player" else category, key)
        return {"value": result}

    async def query_npc_info(npc_id: str) -> dict:
        if not session:
            return {"error": "session not available"}
        npc_data = next((n for n in session.script_data.get("npcs", []) if n.get("id") == npc_id), None)
        if not npc_data:
            return {"error": f"NPC {npc_id} not found"}
        return {"id": npc_id, "name": npc_data.get("name"), "title": npc_data.get("title"),
                "personality": npc_data.get("personality"), "relationship": world_state.relationships.get(npc_id, 0)}

    async def query_location_info(location_id: str) -> dict:
        if not session:
            return {"error": "session not available"}
        loc_data = next((l for l in session.script_data.get("locations", []) if l.get("id") == location_id), None)
        if not loc_data:
            return {"error": f"Location {location_id} not found"}
        return {"id": location_id, "name": loc_data.get("name"), "description": loc_data.get("description")}

    async def roll_dice(skill: str, dc: int = 10) -> dict:
        import random
        roll = random.randint(1, 20)
        bonus = _get_skill_bonus(skill)
        total = roll + bonus
        success = total >= dc
        return {"roll": roll, "bonus": bonus, "total": total, "dc": dc, "success": success,
                "description": f"d20={roll}, 加值={bonus}, 总计={total}, DC={dc}, {'成功' if success else '失败'}"}

    async def check_inventory(item_name: str) -> dict:
        inventory = world_state.variables.get("inventory", [])
        found = any(item_name in (i.get("name", i.get("item", "")) if isinstance(i, dict) else str(i)) for i in inventory)
        return {"has_item": found, "description": f"玩家{'持有' if found else '未持有'}{item_name}"}

    async def get_npc_attitude(npc_id: str) -> dict:
        rel = world_state.relationships.get(npc_id, 50)
        return {"npc_id": npc_id, "attitude": rel, "description": f"{npc_id}的态度: {rel}/100"}

    async def get_time() -> dict:
        time_str = world_state.current_date.isoformat()
        return {"time": time_str, "description": f"当前时间: {time_str}"}

    async def modify_relationship(npc_id: str, delta: int) -> dict:
        delta = max(-10, min(10, delta))
        return {"type": "modify_relationship", "npc_id": npc_id, "delta": delta, "status": "proposed"}

    async def propose_event(event_name: str, description: str, delay_turns: int = 0) -> dict:
        return {"type": "propose_event", "event_name": event_name, "description": description, "delay_turns": delay_turns, "status": "proposed"}

    async def give_item(item_name: str, description: str = "") -> dict:
        return {"type": "give_item", "item_name": item_name, "description": description, "status": "proposed"}

    async def take_item(item_name: str) -> dict:
        return {"type": "take_item", "item_name": item_name, "status": "proposed"}

    async def update_npc_relationship(from_npc: str, to_npc: str, rel_type: str, description: str = "") -> dict:
        return {"type": "update_npc_relationship", "from_npc": from_npc, "to_npc": to_npc,
                "rel_type": rel_type, "description": description, "status": "proposed"}

    async def share_information(fact: str, known_by: str = "") -> dict:
        return {"type": "share_information", "fact": fact, "known_by": known_by, "status": "proposed"}

    async def update_plan(goal: str, next_step: str, progress: str = "") -> dict:
        return {"type": "update_plan", "goal": goal, "next_step": next_step, "progress": progress, "status": "proposed"}

    async def apply_state_change(var: str, op: str, value: any) -> dict:
        return {"type": "apply_state_change", "var": var, "op": op, "value": value, "status": "proposed"}

    async def move_player(location_id: str) -> dict:
        return {"type": "move_player", "location_id": location_id, "status": "proposed"}

    async def set_world_prop(key: str, value: any) -> dict:
        return {"type": "set_world_prop", "key": key, "value": value, "status": "proposed"}

    async def trigger_event(event_name: str, description: str) -> dict:
        return {"type": "trigger_event", "event_name": event_name, "description": description, "status": "proposed"}

    async def spawn_npc(name: str, title: str, personality: str, location_id: str) -> dict:
        return {"type": "spawn_npc", "name": name, "title": title, "personality": personality, "location_id": location_id, "status": "proposed"}

    async def remove_npc(npc_id: str, reason: str = "") -> dict:
        return {"type": "remove_npc", "npc_id": npc_id, "reason": reason, "status": "proposed"}

    async def adjust_tension(delta: int) -> dict:
        delta = max(-30, min(30, delta))
        if session:
            session._pending_tension_adjustment += delta
        return {"type": "adjust_tension", "delta": delta, "status": "applied"}

    async def change_faction_reputation(faction_id: str, delta: int, reason: str = "") -> dict:
        delta = max(-20, min(20, delta))
        return {"type": "change_faction_reputation", "faction_id": faction_id, "delta": delta, "reason": reason, "status": "proposed"}

    async def request_re_reason(npc_id: str, reason: str) -> dict:
        return {"type": "request_re_reason", "npc_id": npc_id, "reason": reason, "status": "queued"}

    async def need_info(query: str, category: str = "general") -> dict:
        """Agent 请求补充信息，立即返回查询结果。"""
        result = {"type": "need_info", "query": query, "category": category}
        q = query.strip()
        ql = q.lower()
        if category == "npc" and session:
            npcs = session.script_data.get("npcs") or []
            def _fmt_npc(n):
                return f"{n.get('name')}: {n.get('personality','')}, 关系={world_state.relationships.get(n['id'], 0)}"
            # 精确 ID → 精确名字 → 子串 fallback
            hit = next((n for n in npcs if n.get("id", "").lower() == ql), None)
            if not hit:
                hit = next((n for n in npcs if n.get("name", "").lower() == ql), None)
            if not hit:
                hit = next((n for n in npcs if ql in n.get("name", "").lower() or ql in n.get("id", "").lower()), None)
            if hit:
                result["answer"] = _fmt_npc(hit)
                return result
        if category == "location" and session:
            locs = session.script_data.get("locations") or []
            def _fmt_loc(l):
                return f"{l.get('name')}: {l.get('description','')}"
            hit = next((l for l in locs if l.get("id", "").lower() == ql), None)
            if not hit:
                hit = next((l for l in locs if l.get("name", "").lower() == ql), None)
            if not hit:
                hit = next((l for l in locs if ql in l.get("name", "").lower() or ql in l.get("id", "").lower()), None)
            if hit:
                result["answer"] = _fmt_loc(hit)
                return result
        if category == "player":
            val = world_state.player_attrs.get(q)
            if val is not None:
                result["answer"] = f"{q}={val}"
                return result
            for k, v in world_state.player_attrs.items():
                if ql == k.lower():
                    result["answer"] = f"{k}={v}"
                    return result
        if category == "inventory":
            inv = world_state.variables.get("inventory", [])
            if q:
                found = [i for i in inv if q in (i.get("name", str(i)) if isinstance(i, dict) else str(i))]
                if found:
                    result["answer"] = ", ".join(i.get("name", str(i)) if isinstance(i, dict) else str(i) for i in found)
                    return result
            result["answer"] = ", ".join(i.get("name", str(i)) if isinstance(i, dict) else str(i) for i in inv[:10]) or "背包为空"
            return result
        info = world_state.query_info("general", q)
        result["answer"] = str(info) if info else "无相关信息"
        return result

    async def play_sound(sound_type: str) -> dict:
        return {"sound": sound_type, "status": "queued"}

    async def set_weather(weather: str) -> dict:
        return {"weather": weather, "status": "set"}

    async def set_mood_filter(mood: str) -> dict:
        return {"mood": mood, "status": "set"}

    # --- Schemas (data-driven) ---
    _S = {"type": "string"}
    _I = {"type": "integer"}

    game_schemas = [
        _tool("roll_dice", "掷骰子进行技能检定", {"skill": {**_S, "description": "技能名称"}, "dc": {**_I, "description": "难度等级", "default": 10}}, ["skill"]),
        _tool("check_inventory", "检查玩家背包中是否持有指定物品", {"item_name": _S}),
        _tool("get_npc_attitude", "查询NPC当前对玩家的好感度", {"npc_id": _S}),
        _tool("get_time", "获取当前游戏时间", {}),
    ]
    game_exec = {"roll_dice": roll_dice, "check_inventory": check_inventory, "get_npc_attitude": get_npc_attitude, "get_time": get_time}

    qs_schema = _tool("query_state", "查询玩家属性、关系值或世界属性",
                       {"category": {**_S, "enum": ["player", "relationship", "world"]}, "key": _S})

    need_info_schema = _tool("need_info", "查询游戏信息：NPC好感度/性格、地点描述、玩家属性/背包、世界状态。系统立即返回结果",
                              {"query": {**_S, "description": "要查询的具体内容（NPC名/属性名/物品名等）"},
                               "category": {**_S, "enum": ["npc", "location", "player", "inventory", "general"],
                                             "description": "信息类别"}})

    if agent_type == "director":
        return [
            qs_schema,
            _tool("query_npc_info", "查询NPC基本信息", {"npc_id": _S}),
            _tool("query_location_info", "查询地点信息", {"location_id": _S}),
            _tool("spawn_npc", "动态创建NPC（当剧情需要新角色时）",
                  {"name": _S, "title": _S, "personality": _S, "location_id": _S}),
            _tool("remove_npc", "移除NPC（角色退场时）", {"npc_id": _S, "reason": _S}, ["npc_id"]),
            _tool("trigger_event", "触发一个叙事事件", {"event_name": _S, "description": _S}),
        ], {"query_state": query_state, "query_npc_info": query_npc_info, "query_location_info": query_location_info,
            "spawn_npc": spawn_npc, "remove_npc": remove_npc, "trigger_event": trigger_event}

    elif agent_type in ("npc", "npc_extended"):
        core_schemas = [
            _tool("roll_dice", "掷骰子进行技能检定", {"skill": {**_S, "description": "技能名称"}, "dc": {**_I, "description": "难度等级", "default": 10}}, ["skill"]),
            _tool("modify_relationship", "改变与玩家的关系值（-10到+10）",
                  {"npc_id": {**_S, "description": "NPC ID"}, "delta": {**_I, "description": "关系变化量"}}),
            _tool("update_plan", "更新你的长期计划（目标/下一步）",
                  {"goal": {**_S, "description": "当前目标"}, "next_step": {**_S, "description": "下一步行动"},
                   "progress": {**_S, "description": "当前进度描述"}}, ["goal", "next_step"]),
        ]
        core_exec = {"roll_dice": roll_dice, "modify_relationship": modify_relationship, "update_plan": update_plan}

        if agent_type == "npc_extended":
            extended_schemas = core_schemas + [
                need_info_schema,
                _tool("propose_event", "提议一个事件",
                      {"event_name": _S, "description": _S, "delay_turns": _I}, ["event_name", "description"]),
                _tool("give_item", "给玩家一件物品",
                      {"item_name": _S, "description": _S}, ["item_name"]),
                _tool("take_item", "从玩家收回一件物品", {"item_name": _S}),
                _tool("update_npc_relationship", "更新你与另一个NPC之间的关系",
                      {"from_npc": {**_S, "description": "发起关系的NPC ID"}, "to_npc": {**_S, "description": "对象NPC ID"},
                       "rel_type": {**_S, "description": "关系类型：友好/敌对/警惕/合作/竞争/中立"}, "description": {**_S, "description": "原因"}},
                      ["from_npc", "to_npc", "rel_type"]),
                _tool("share_information", "将你知道的某条信息分享出去",
                      {"fact": {**_S, "description": "信息内容"}, "known_by": {**_S, "description": "你的NPC ID"}}),
            ]
            return extended_schemas, {**core_exec, "need_info": need_info,
                                       "propose_event": propose_event, "give_item": give_item, "take_item": take_item,
                                       "update_npc_relationship": update_npc_relationship, "share_information": share_information}
        return core_schemas, core_exec

    elif agent_type == "outline":
        return [
            need_info_schema,
            _tool("apply_state_change", "应用状态变更",
                  {"var": {**_S, "description": "变量路径"}, "op": {**_S, "enum": ["add", "set"]},
                   "value": {"type": ["number", "string", "boolean"]}}),
            _tool("move_player", "移动玩家到某个地点", {"location_id": _S}),
            _tool("set_world_prop", "设置世界属性",
                  {"key": _S, "value": {"type": ["string", "number", "boolean"]}}),
            _tool("adjust_tension", "调节叙事紧张度（-30到+30）",
                  {"delta": {**_I, "description": "紧张度变化量"}}),
            _tool("change_faction_reputation", "修改阵营声望值（-20到+20）",
                  {"faction_id": {**_S, "description": "阵营ID"}, "delta": {**_I, "description": "声望变化量"},
                   "reason": {**_S, "description": "变化原因"}}, ["faction_id", "delta"]),
            _tool("request_re_reason", "请求NPC重新推理（限用1次）",
                  {"npc_id": {**_S, "description": "NPC ID"}, "reason": {**_S, "description": "原因"}}),
        ], {"need_info": need_info, "apply_state_change": apply_state_change, "move_player": move_player,
            "set_world_prop": set_world_prop, "adjust_tension": adjust_tension,
            "change_faction_reputation": change_faction_reputation, "request_re_reason": request_re_reason}

    elif agent_type == "scene":
        return [
            _tool("play_sound", "播放音效",
                  {"sound_type": {**_S, "enum": ["ambient", "tension", "wonder", "triumph", "sorrow"]}}),
            _tool("set_weather", "设置场景天气",
                  {"weather": {**_S, "enum": ["sunny", "cloudy", "rainy", "stormy", "night"]}}),
            _tool("set_mood_filter", "设置场景氛围",
                  {"mood": {**_S, "enum": ["calm", "tense", "mystical", "dark", "warm"]}}),
        ], {"play_sound": play_sound, "set_weather": set_weather, "set_mood_filter": set_mood_filter}

    return [], {}



@dataclass
class Turn:
    """单个推演轮次的完整记录"""
    turn_num: int
    timestamp: str
    player_action: str
    context_object: Dict[str, Any]
    npc_outputs: Dict[str, Any]
    outline_summary: Dict[str, Any]
    scene_output: Dict[str, Any]
    state_before: Dict[str, Any]
    state_after: Dict[str, Any]
    player_choices: List[Dict[str, Any]]


class WorldStateManager:
    """管理世界的所有状态：变量、关系、属性。
    内部使用 ScriptLoader.create_initial_state 生成的 state dict，
    通过 StateManager.apply_changes 执行状态变更。
    """

    def __init__(self, script_data: Dict[str, Any]):
        self.script_data = script_data
        self._state_manager = StateManager(script_data) if TAVERN_MODULES_AVAILABLE else None

        # 用 ScriptLoader 生成完整初始状态
        if TAVERN_MODULES_AVAILABLE:
            self._state = ScriptLoader.create_initial_state(script_data)
        else:
            self._state = self._fallback_init(script_data)

        # 关系扁平化：ScriptLoader 输出 3D {trust, affection, fear}，引擎用简单 int
        player_rels = self._state.get("player", {}).get("relationships", {})
        flat_rels = {}
        for npc_id, rel in player_rels.items():
            if isinstance(rel, dict):
                flat_rels[npc_id] = rel.get("trust", rel.get("value", 50))
            else:
                flat_rels[npc_id] = rel
        self._flat_relationships = flat_rels

        # 游戏变量（ScriptLoader 不处理 variables，手动初始化）
        self.variables = {}
        for var in script_data.get("variables", []):
            self.variables[var["id"]] = var.get("value", var.get("default", 0))

        # 事件引擎状态（EventEngine.tick 读写此 dict）
        self.event_state: Dict[str, Any] = {}
        # Lorebook 定时状态
        self.lorebook_timed_state: Dict[str, Any] = {}
        # 历史摘要状态
        self.summary_state: Dict[str, Any] = {}
        # 历史日志
        self.turns: List[Turn] = []
        self.current_turn = 0

        # 属性恢复规则：从脚本中提取 recovery_rate
        self._recovery_rules: Dict[str, Dict[str, float]] = {}
        pc = script_data.get("player_character", {})
        for attr_name, attr_def in pc.get("attributes", {}).items():
            if isinstance(attr_def, dict) and attr_def.get("recovery_rate"):
                self._recovery_rules[attr_name] = {
                    "rate": float(attr_def["recovery_rate"]),
                    "max": float(attr_def.get("max", 100)),
                }

        # 时间：从 _state["game_time"] 解析
        game_time = self._state.get("game_time", "")
        if game_time:
            try:
                self._current_date = datetime.fromisoformat(game_time)
            except Exception:
                self._current_date = datetime.now()
        else:
            self._current_date = datetime.now()

    @staticmethod
    def _fallback_init(script_data: dict) -> dict:
        """TAVERN_MODULES_AVAILABLE=False 时的最小化初始状态"""
        pc = script_data.get("player_character", {})
        attrs = {}
        for k, v in pc.get("attributes", {}).items():
            attrs[k] = v["value"] if isinstance(v, dict) and "value" in v else v
        locs = script_data.get("locations", [])
        return {
            "game_time": script_data.get("start_time", ""),
            "player": {
                "location": pc.get("initial_location_id") or (locs[0].get("id", "start") if locs else "start"),
                "attributes": attrs,
                "relationships": copy.deepcopy(pc.get("relationships", {})),
            },
            "world_properties": {p["id"]: p.get("value", "") for p in script_data.get("world_properties", [])},
            "display_names": {},
        }

    # --- 属性访问器（向后兼容） ---

    @property
    def player_attrs(self) -> dict:
        return self._state.get("player", {}).get("attributes", {})

    @player_attrs.setter
    def player_attrs(self, value: dict):
        self._state.setdefault("player", {})["attributes"] = value

    @property
    def relationships(self) -> dict:
        return self._flat_relationships

    @relationships.setter
    def relationships(self, value: dict):
        self._flat_relationships = value

    @property
    def world_props(self) -> dict:
        return self._state.get("world_properties", {})

    @world_props.setter
    def world_props(self, value: dict):
        self._state["world_properties"] = value

    @property
    def current_location(self) -> str:
        return self._state.get("player", {}).get("location", "start")

    @current_location.setter
    def current_location(self, value: str):
        self._state.setdefault("player", {})["location"] = value

    @property
    def current_date(self) -> datetime:
        return self._current_date

    @current_date.setter
    def current_date(self, value: datetime):
        self._current_date = value

    @property
    def display_names(self) -> dict:
        return self._state.get("display_names", {})

    def get_snapshot(self) -> Dict[str, Any]:
        """获取当前完整状态快照"""
        dn = self.display_names
        location_name = dn.get(self.current_location, self.current_location)
        relationships_display = {dn.get(k, k): v for k, v in self._flat_relationships.items()}

        return {
            "turn": self.current_turn,
            "date": self._current_date.isoformat(),
            "location": location_name,
            "location_id": self.current_location,
            "player_attrs": copy.deepcopy(self.player_attrs),
            "relationships": copy.deepcopy(self._flat_relationships),
            "relationships_display": relationships_display,
            "variables": copy.deepcopy(self.variables),
            "world_props": copy.deepcopy(self.world_props),
            "display_names": dn,
            "event_state": copy.deepcopy(self.event_state),
        }

    def apply_changes(self, changes: List[Dict[str, Any]]) -> None:
        """应用状态变更。接受 {"var": path, "op": op, "value": val} 格式。"""
        if self._state_manager:
            adapted = []
            flat_rel_updates = []
            for c in changes:
                var = c.get("var", "")
                if not var:
                    continue
                op = c.get("op", "set")
                value = c.get("value")
                parts = var.split(".")
                if len(parts) == 2 and parts[0] == "relationship":
                    flat_rel_updates.append((parts[1], op, value))
                elif len(parts) == 2 and parts[0] == "player":
                    adapted.append({"target": f"player.attributes.{parts[1]}", "op": op, "value": value})
                elif len(parts) == 2 and parts[0] == "world":
                    adapted.append({"target": f"world_properties.{parts[1]}", "op": op, "value": value})
                else:
                    adapted.append({"target": var, "op": op, "value": value})
            if adapted:
                self._state, _ = self._state_manager.apply_changes(self._state, adapted, inplace=True)
            for key, op, value in flat_rel_updates:
                if op == "add":
                    self._flat_relationships[key] = self._flat_relationships.get(key, 0) + value
                else:
                    self._flat_relationships[key] = value
        else:
            for change in changes:
                var = change.get("var", "")
                op = change.get("op", "set")
                value = change.get("value")
                if not var:
                    continue
                parts = var.split(".")
                if len(parts) == 2:
                    category, key = parts
                    target = (self.player_attrs if category == "player"
                              else self._flat_relationships if category == "relationship"
                              else self.world_props if category == "world" else None)
                    if target is not None:
                        if op == "add":
                            target[key] = target.get(key, 0) + value
                        else:
                            target[key] = value

    def query_info(self, query_type: str, query_key: str) -> Any:
        """Agent查询信息的接口"""
        if query_type == "player_attr":
            return self.player_attrs.get(query_key)
        elif query_type == "relationship":
            return self._flat_relationships.get(query_key)
        elif query_type == "variable":
            return self.variables.get(query_key)
        elif query_type == "world_prop":
            return self.world_props.get(query_key)
        elif query_type == "location":
            return self.current_location
        elif query_type == "date":
            return self._current_date.isoformat()
        return None

    def apply_natural_recovery(self, hours: float) -> List[str]:
        """根据时间流逝恢复具有 recovery_rate 的属性。返回恢复描述列表。"""
        if not self._recovery_rules or hours <= 0:
            return []
        recovered = []
        attrs = self.player_attrs
        for attr_name, rule in self._recovery_rules.items():
            current = attrs.get(attr_name, 0)
            if not isinstance(current, (int, float)):
                continue
            max_val = rule["max"]
            if current >= max_val:
                continue
            gain = rule["rate"] * hours
            new_val = min(max_val, current + gain)
            if new_val > current:
                attrs[attr_name] = round(new_val) if isinstance(current, int) else round(new_val, 1)
                recovered.append(f"{attr_name} +{round(new_val - current, 1)}")
        return recovered


TIME_HINT_HOURS = {
    "immediate": 0.17,
    "short": 1,
    "medium": 2,
    "half_day": 6,
    "full_day": 12,
    "next_day": 24,
}


class DirectorAgent:
    """规划模型：理解玩家输入 → 分类/增强/路由。在 process_turn 之前执行。"""

    @staticmethod
    async def plan(
        raw_input: str,
        world_state: "WorldStateManager",
        script_data: dict,
        recent_turns: list,
    ) -> dict:
        sys_prompt, user_prompt = DirectorAgent._build_director_prompt(
            raw_input, world_state, script_data, recent_turns,
        )
        tools, executors = _build_tool_schemas_and_executors("director", world_state)
        raw, tool_calls = await llm_call_with_tools(sys_prompt, user_prompt, tools, executors, max_tokens=1200)
        parsed = _parse_json_from_llm(raw)
        if not parsed.get("intents"):
            return {"intents": [{"type": "game_action", "resolved_action": raw_input}], "reply_to_player": None, "tool_calls": tool_calls}
        parsed["tool_calls"] = tool_calls
        return parsed

    @staticmethod
    def _build_director_prompt(
        raw_input: str,
        world_state: "WorldStateManager",
        script_data: dict,
        recent_turns: list,
    ) -> tuple:
        npc_list = []
        for npc in script_data.get("npcs", []):
            npc_list.append(f"  - {npc['id']}: {npc.get('name', npc['id'])}")
        loc_list = []
        for loc in script_data.get("locations", []):
            loc_list.append(f"  - {loc['id']}: {loc.get('name', loc['id'])}")

        recent_summary = ""
        if recent_turns:
            lines = []
            for t in recent_turns[-3:]:
                lines.append(f"  轮次{t.turn_num}: {t.player_action}")
            recent_summary = "\n".join(lines)

        all_npc_ids = [n["id"] for n in script_data.get("npcs", [])]

        system_prompt = f"""你是RPG推演系统的调度员（Director）。你的任务是理解玩家的原始输入，进行分类、消解指代、增强描述，并路由到对应处理模块。

## 可识别的意图类型

- game_action: 游戏内行动（和NPC互动、移动、做事）
- ui_command: 前端UI操作（放大地图、显示面板、调字体等）
- meta_feedback: 对剧本或推演的反馈（人设不对、场景太短等）
- save_load: 存档/读档/回溯（保存、加载、回到上个节点等）
- query: 咨询/提问（我该干什么、这个NPC是谁、当前状况等）

## 你的增强能力

1. **指代消解**：如果玩家说"那个人"、"他"、"教练"等，根据上下文推断具体是哪个NPC
2. **地点推断**：如果玩家说"去训练"，推断目标地点
3. **NPC过滤**：标记本轮需要推理的NPC（target_npcs）和可以跳过的NPC（skip_npcs）
4. **情绪推断**：从输入推断玩家角色的mood（calm/anxious/angry/determined/playful/sad）
5. **时间推进**：根据行动性质判断时间步长（immediate/short/medium/half_day/full_day/next_day）
6. **焦点提示**：用focus_hint告诉NPC本轮核心话题
7. **NPC推理策略**：npc_strategies 为每个相关NPC分配推理策略：
   - aggressive: 积极参与，完整两轮推理（主要互动角色）
   - supportive: 辅助角色，仅一轮精简推理
   - minimal: 与场景无关，跳过推理
8. **流水线控制**：pipeline_hints 控制后续阶段是否执行：
   - skip_outline: true=跳过大纲分析（不涉及状态变更时）
   - skip_conflict: true=跳过冲突检测（≤1个NPC参与时）
   - skip_validator: true=跳过一致性校验（只涉及已知角色/地点时）
   - scene_budget: "short"(300字) / "normal"(600字) / "long"(1000字)

   决策参考：
   - "去训练场" → skip_outline=true, skip_conflict=true, scene_budget="short"
   - "和教练聊聊" → skip_conflict=true, skip_validator=true, scene_budget="normal"
   - "和三方谈判" → 全部false, scene_budget="long"
   - "在已知地点做日常" → skip_validator=true, scene_budget="short"

## 可用NPC

{chr(10).join(npc_list)}

## 可用地点

{chr(10).join(loc_list)}

## 输出格式

返回纯JSON：
{{
  "intents": [
    {{
      "type": "game_action",
      "resolved_action": "增强后的行动描述（将模糊输入具体化）",
      "target_npcs": ["相关NPC的id"],
      "target_location": "目标地点id或null",
      "mood": "calm|anxious|angry|determined|playful|sad",
      "time_hint": "immediate|short|medium|half_day|full_day|next_day",
      "skip_npcs": ["本轮不需要推理的NPC id"],
      "focus_hint": "本轮核心话题（一句话）",
      "npc_strategies": {{"npc_id": "aggressive|supportive|minimal"}},
      "pipeline_hints": {{
        "skip_outline": false,
        "skip_conflict": false,
        "skip_validator": false,
        "scene_budget": "normal"
      }}
    }}
  ],
  "reply_to_player": null
}}

如果是非游戏请求：
{{
  "intents": [
    {{
      "type": "query",
      "answer": "你的回答"
    }}
  ],
  "reply_to_player": "你的回答"
}}

混合输入则返回多个intent。

## 约束

- 不要发明游戏内容，只做理解和路由
- resolved_action 应该比原始输入更具体，但不要编造玩家没表达的意图
- skip_npcs 应该排除与本轮行动明显无关的NPC
- 当剧情需要新角色登场时，使用 spawn_npc 工具创建
- 当角色退场时，使用 remove_npc 工具移除
- 当需要触发叙事事件时，使用 trigger_event 工具
- 如果无法判断意图，默认为 game_action，resolved_action = 原始输入"""

        user_prompt = f"""## 当前世界状态

轮次: {world_state.current_turn}
时间: {world_state.current_date.isoformat()}
地点: {world_state.current_location}

## 最近推演

{recent_summary if recent_summary else "（游戏刚开始）"}

## 玩家输入

{raw_input}"""

        return system_prompt, user_prompt

    @staticmethod
    def plan_rules(raw_input: str, world_state: "WorldStateManager") -> dict:
        text = raw_input.strip()
        import re
        if re.search(r'存.*档|保存|save', text, re.IGNORECASE):
            return {"intents": [{"type": "save_load", "action": "save"}], "reply_to_player": None}
        if re.search(r'读.*档|加载|load|回.*档|回到', text, re.IGNORECASE):
            return {"intents": [{"type": "save_load", "action": "load"}], "reply_to_player": None}
        if any(kw in text for kw in ("放大", "缩小", "字体", "主题", "面板")):
            return {"intents": [{"type": "ui_command", "command": "toggle_panel", "params": {}}], "reply_to_player": None}
        return {"intents": [{"type": "game_action", "resolved_action": text}], "reply_to_player": None}


class ScriptBuilder:
    """从主旨开始，多轮对话构建完整剧本 JSON，并支持推演中动态扩展世界"""

    PHASES = ["theme", "world_and_player", "npcs", "rules", "review"]

    def __init__(self):
        self.phase = "theme"
        self.theme = ""
        self.draft: Dict[str, Any] = {}
        self.history: List[Dict[str, str]] = []

    async def process_input(self, user_input: str) -> dict:
        self.history.append({"role": "user", "content": user_input})

        if self.phase == "theme":
            return await self._handle_theme(user_input)
        elif self.phase == "world_and_player":
            return await self._handle_world_and_player(user_input)
        elif self.phase == "npcs":
            return await self._handle_npcs(user_input)
        elif self.phase == "rules":
            return await self._handle_rules(user_input)
        elif self.phase == "review":
            return await self._handle_review(user_input)
        return {"phase": self.phase, "reply": "未知阶段", "done": False}

    # ---- 阶段处理 ----

    async def _handle_theme(self, user_input: str) -> dict:
        self.theme = user_input
        prompt = self._build_phase_prompt("world_and_player")
        response = await llm_call(self._system_prompt(), prompt, max_tokens=2500, raise_on_error=True)
        parsed = _parse_json_from_llm(response)
        if not parsed:
            parsed = {}
        self.draft.update(parsed)
        self.phase = "world_and_player"
        reply = self._format_proposal("世界观与主角", parsed)
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": parsed, "done": False}

    async def _handle_world_and_player(self, user_input: str) -> dict:
        if self._is_modify(user_input):
            return await self._modify_current(user_input)
        prompt = self._build_phase_prompt("npcs", user_feedback=user_input)
        response = await llm_call(self._system_prompt(), prompt, max_tokens=2500, raise_on_error=True)
        parsed = _parse_json_from_llm(response) or {}
        if parsed.get("npcs"):
            self.draft["npcs"] = parsed["npcs"]
        self.phase = "npcs"
        reply = self._format_proposal("角色设定", parsed)
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": parsed, "done": False}

    async def _handle_npcs(self, user_input: str) -> dict:
        if self._is_modify(user_input):
            return await self._modify_current(user_input)
        prompt = self._build_phase_prompt("rules", user_feedback=user_input)
        response = await llm_call(self._system_prompt(), prompt, max_tokens=2000, raise_on_error=True)
        parsed = _parse_json_from_llm(response) or {}
        if parsed.get("attributes"):
            self.draft.setdefault("player_character", {})["attributes"] = parsed["attributes"]
        if parsed.get("world_properties"):
            self.draft["world_properties"] = parsed["world_properties"]
        self.phase = "rules"
        reply = self._format_proposal("属性与规则", parsed)
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": parsed, "done": False}

    async def _handle_rules(self, user_input: str) -> dict:
        if self._is_modify(user_input):
            return await self._modify_current(user_input)
        self.phase = "review"
        reply = self._format_full_review()
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": self.draft, "done": False}

    async def _handle_review(self, user_input: str) -> dict:
        if self._is_modify(user_input):
            return await self._modify_current(user_input)
        await self._generate_visual_profiles()
        script_data = self._finalize()
        script_id = self._save_script(script_data)
        return {"phase": "done", "reply": f"剧本「{script_data.get('script_name', '')}」已生成并保存！", "script_id": script_id, "done": True}

    async def _modify_current(self, user_input: str) -> dict:
        prompt = self._build_modify_prompt(user_input)
        response = await llm_call(self._system_prompt(), prompt, max_tokens=2000, raise_on_error=True)
        parsed = _parse_json_from_llm(response) or {}
        self._merge_modifications(parsed)
        reply = f"已根据你的意见修改。\n\n{self._format_full_review()}"
        self.history.append({"role": "assistant", "content": reply})
        return {"phase": self.phase, "reply": reply, "draft": self.draft, "done": False}

    # ---- Prompt 构建 ----

    @staticmethod
    def _system_prompt() -> str:
        return """你是一个RPG剧本构建助手。根据用户主旨逐步构建完整RPG剧本。

## 输出格式
始终返回合法 JSON 对象，不要包含解释文本。

## 约束
- 贴合用户主旨
- NPC 3-8 个，地点 3-10 个，属性 3-8 个
- 每个 NPC 必须有 id、name、bio、personality、title、attitude_toward_player(0-100)、capabilities（顿号分隔）、default_location
- 每个地点必须有 id、name、description
- 属性值范围 0-100"""

    def _build_phase_prompt(self, target_phase: str, user_feedback: str = "") -> str:
        draft_json = json.dumps(self.draft, ensure_ascii=False, indent=2) if self.draft else "{}"

        if target_phase == "world_and_player":
            return f"""用户想创建RPG剧本，主旨：「{self.theme}」

请生成（JSON）：
1. script_name: 剧本名（简短）
2. world_background: 世界观（2-3段）
3. start_time: 起始时间（ISO格式，如 "2024-01-01T09:00:00"）
4. player_character: {{id, bio, initial_location, long_term_goal}}
5. locations: 3-6个地点 [{{id, name, description}}]
6. opening: {{text: 开场白（第二人称，3-5句）, choices: [{{id, text}}]}}"""

        if target_phase == "npcs":
            return f"""已确定世界观：
{draft_json}

用户反馈：{user_feedback}

请生成 3-6 个 NPC（JSON）：
{{"npcs": [{{id, name, bio, personality, title, attitude_toward_player, capabilities, default_location}}]}}

capabilities 用顿号分隔关键能力标签（如"谈判、情报、战斗"）。
default_location 必须是已有地点的 id。"""

        if target_phase == "rules":
            return f"""已确定世界和角色：
{draft_json}

用户反馈：{user_feedback}

请生成（JSON）：
{{"attributes": {{
  "属性名": {{"value": 初始值, "min": 0, "max": 100, "rule": "说明"}},
  ...
}},
"world_properties": [{{id, name, value, rule}}]}}"""

        return ""

    def _build_modify_prompt(self, user_input: str) -> str:
        draft_json = json.dumps(self.draft, ensure_ascii=False, indent=2)
        return f"""用户修改意见：{user_input}

当前剧本：
{draft_json}

请返回修改后的完整剧本 JSON（和当前格式一致）。"""

    # ---- 格式化 ----

    def _format_proposal(self, title: str, data: dict) -> str:
        lines = [f"## {title}\n"]
        if data.get("script_name"):
            lines.append(f"**剧本名**: {data['script_name']}")
        if data.get("world_background"):
            bg = data["world_background"]
            lines.append(f"**世界观**: {bg[:200]}{'...' if len(bg) > 200 else ''}")
        if data.get("player_character"):
            pc = data["player_character"]
            lines.append(f"**主角**: {pc.get('bio', '')}")
        if data.get("locations"):
            locs = data["locations"]
            lines.append(f"**地点** ({len(locs)}个):")
            for loc in locs:
                lines.append(f"  - {loc.get('name', '?')}: {loc.get('description', '')[:60]}")
        if data.get("npcs"):
            npcs = data["npcs"]
            lines.append(f"**角色** ({len(npcs)}个):")
            for npc in npcs:
                lines.append(f"  - {npc.get('name', '?')} ({npc.get('title', '')}): {npc.get('personality', '')[:40]}")
        if data.get("attributes"):
            attrs = data["attributes"]
            lines.append(f"**属性体系** ({len(attrs)}个):")
            for name, info in attrs.items():
                val = info.get("value", "?") if isinstance(info, dict) else info
                lines.append(f"  - {name}: {val}")
        if data.get("opening"):
            lines.append(f"**开场白**: {data['opening'].get('text', '')[:100]}...")
        lines.append("\n请确认或提出修改意见。输入「确认」进入下一步。")
        return "\n".join(lines)

    def _format_full_review(self) -> str:
        lines = ["## 剧本总览\n"]
        lines.append(f"**剧本名**: {self.draft.get('script_name', self.theme)}")
        bg = self.draft.get("world_background", "")
        lines.append(f"**世界观**: {bg[:150]}{'...' if len(bg) > 150 else ''}")
        pc = self.draft.get("player_character", {})
        if pc:
            lines.append(f"**主角**: {pc.get('bio', '')}")
        locs = self.draft.get("locations", [])
        if locs:
            lines.append(f"**地点** ({len(locs)}):")
            for loc in locs:
                lines.append(f"  - {loc.get('name', '?')}")
        npcs = self.draft.get("npcs", [])
        if npcs:
            lines.append(f"**角色** ({len(npcs)}):")
            for npc in npcs:
                lines.append(f"  - {npc.get('name', '?')} ({npc.get('title', '')}) 好感:{npc.get('attitude_toward_player', '?')}")
        attrs = pc.get("attributes", {})
        if attrs:
            lines.append(f"**属性** ({len(attrs)}):")
            for name, info in attrs.items():
                val = info.get("value", "?") if isinstance(info, dict) else info
                lines.append(f"  - {name}: {val}")
        opening = self.draft.get("opening", {})
        if opening.get("text"):
            lines.append(f"**开场白**: {opening['text'][:100]}...")
        lines.append("\n输入「确认」保存剧本并开始游戏，或提出修改意见。")
        return "\n".join(lines)

    # ---- 合并与保存 ----

    def _merge_modifications(self, parsed: dict):
        for key in ("script_name", "world_background", "start_time", "opening"):
            if key in parsed:
                self.draft[key] = parsed[key]
        if parsed.get("player_character"):
            self.draft.setdefault("player_character", {}).update(parsed["player_character"])
        if parsed.get("npcs"):
            self.draft["npcs"] = parsed["npcs"]
        if parsed.get("locations"):
            self.draft["locations"] = parsed["locations"]
        if parsed.get("attributes"):
            self.draft.setdefault("player_character", {})["attributes"] = parsed["attributes"]
        if parsed.get("world_properties"):
            self.draft["world_properties"] = parsed["world_properties"]

    def _finalize(self) -> dict:
        theme_slug = re.sub(r'[^a-z0-9]', '_', self.theme[:30].lower().strip())
        theme_slug = re.sub(r'_+', '_', theme_slug).strip('_') or "new_script"
        data = {
            "script_id": theme_slug,
            "script_name": self.draft.get("script_name", self.theme),
            "version": "1.0",
            "start_time": self.draft.get("start_time", datetime.now().isoformat()),
            "world_background": self.draft.get("world_background", ""),
            "settings": {"dice_check": False},
            "opening": self.draft.get("opening", {"text": "故事即将开始...", "choices": []}),
            "locations": self.draft.get("locations", []),
            "player_character": self.draft.get("player_character", {"id": "player", "bio": "", "attributes": {}, "relationships": {}}),
            "npcs": self.draft.get("npcs", []),
            "world_properties": self.draft.get("world_properties", []),
            "variables": [],
            "persistent_states": [],
            "cyclic_events": [],
            "one_time_events": [],
        }
        for i, npc in enumerate(data["npcs"]):
            if not npc.get("id"):
                npc["id"] = f"npc_{i}"
        pc = data["player_character"]
        if "relationships" not in pc:
            pc["relationships"] = {}
        for npc in data["npcs"]:
            npc_id = npc["id"]
            if npc_id not in pc["relationships"]:
                pc["relationships"][npc_id] = npc.get("attitude_toward_player", 50)
        if not pc.get("id"):
            pc["id"] = "player"
        return data

    async def _generate_visual_profiles(self) -> None:
        """为所有 NPC 和地点生成 AI visual profiles（颜色、风格等）"""
        if not _llm_state.get("client"):
            return

        npcs = self.draft.get("npcs", [])
        locations = self.draft.get("locations", [])

        # 为 NPC 生成 visual profiles
        for npc in npcs:
            if npc.get("visual_profile"):
                continue
            sys_prompt = "你是视觉设计助手，基于角色描述生成像素艺术风格的颜色和特征。只返回 JSON。"
            user_prompt = f"""角色: {npc.get('name', '?')}
职位: {npc.get('title', '?')}
性格: {npc.get('personality', '?')}
能力: {npc.get('capabilities', '?')}

生成一个 visual_profile JSON，包含:
- shirt_color: 衣服颜色 (hex)
- hair_color: 头发颜色 (hex)
- hair_style: 发型 (short/long/bun/slick/buzz)
- pant_color: 裤子颜色 (hex)
- skin_color: 肤色 (hex)

只返回 JSON，不要其他文本。"""
            try:
                response = await llm_call(sys_prompt, user_prompt, max_tokens=300)
                profile = _parse_json_from_llm(response) or {}
                npc["visual_profile"] = profile
            except:
                npc["visual_profile"] = {}

        # 为地点生成 visual profiles
        for loc in locations:
            if loc.get("visual_profile"):
                continue
            sys_prompt = "你是视觉设计助手，基于地点描述生成等轴测立方体像素艺术的颜色和形状。只返回 JSON。"
            user_prompt = f"""地点: {loc.get('name', '?')}
描述: {loc.get('description', '?')}

生成一个 visual_profile JSON，包含:
- base_color: 基础颜色 (hex)
- dark_color: 暗色 (hex)
- light_color: 亮色 (hex)
- accent_color: 强调色 (hex)
- shape: 形状 (cube/tall/wide/pyramid/dome/multi)

只返回 JSON，不要其他文本。"""
            try:
                response = await llm_call(sys_prompt, user_prompt, max_tokens=300)
                profile = _parse_json_from_llm(response) or {}
                loc["visual_profile"] = profile
            except:
                loc["visual_profile"] = {}

    def _save_script(self, data: dict) -> str:
        script_id = data["script_id"]
        path = os.path.join(SCRIPTS_DIR, f"{script_id}.json")
        counter = 1
        while os.path.exists(path):
            sid = f"{script_id}_{counter}"
            path = os.path.join(SCRIPTS_DIR, f"{sid}.json")
            counter += 1
            data["script_id"] = sid
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"[ScriptBuilder] 剧本已保存: {path}")
        return data["script_id"]

    @staticmethod
    def _is_modify(text: str) -> bool:
        return any(kw in text for kw in ("修改", "改一下", "换", "不要", "加上", "删掉", "去掉", "调整"))

    # ---- 推演中动态扩展世界 ----

    @staticmethod
    async def expand_world(scene_text: str, outline: dict, script_data: dict) -> dict:
        existing_npc_names = {n.get("name", "") for n in script_data.get("npcs", [])}
        existing_loc_names = {l.get("name", "") for l in script_data.get("locations", [])}

        sys_prompt = "你是世界扩展分析器。分析场景文本，找出尚未在剧本中定义的新角色和新地点。只返回 JSON。"
        user_prompt = f"""已有角色: {', '.join(n for n in existing_npc_names if n)}
已有地点: {', '.join(n for n in existing_loc_names if n)}

场景文本:
{scene_text[:800]}

大纲: {outline.get('summary', '')}

如果有新角色或新地点，生成定义；没有则返回空列表。
{{"new_npcs": [{{id, name, bio, personality, title, attitude_toward_player, capabilities, default_location}}], "new_locations": [{{id, name, description}}]}}"""

        response = await llm_call(sys_prompt, user_prompt, max_tokens=1000)
        return _parse_json_from_llm(response) or {"new_npcs": [], "new_locations": []}



class NPCAgent:
    """单个NPC Agent的推理和决策"""

    def __init__(self, npc_id: str, npc_data: Dict[str, Any], world_state: WorldStateManager):
        self.npc_id = npc_id
        self.npc_data = npc_data
        self.world_state = world_state
        self.name = npc_data.get("name", "Unknown")
        self.personality = npc_data.get("personality", "")

    def reason(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """NPC推理入口（仅规则兜底，LLM模式通过 _reason_llm 调用）"""
        return self._reason_rules(context)

    async def _reason_intent(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """第一轮：轻量意图推理，仅输出行动倾向和立场，无工具调用"""
        rel_value = self.world_state.relationships.get(self.npc_id, 0)
        visible_info = self._gather_visible_info(context)
        user_action = context.get("user_action", "")

        npc = self.npc_data
        name = npc.get("name", self.npc_id)
        personality = npc.get("personality", "")
        goals = npc.get("goals", [])
        goals_text = "、".join(goals[:3]) if goals else "无"

        system_prompt = f"""你是NPC「{name}」。性格：{personality}。目标：{goals_text}。{_semantic_relationship("玩家", rel_value)}。
快速判断你对玩家行动的立场。只返回 JSON，不要其他文本。"""

        loc_name = visible_info.get("current_location", "?")
        for loc in self.world_state.script_data.get("locations", []):
            if loc.get("id") == loc_name:
                loc_name = loc.get("name", loc_name)
                break

        user_prompt = f"地点：{loc_name}\n玩家行动：{user_action}\n\n返回 JSON：{{\"action_type\": \"主动联系|被动反应|主动谈判|观望|警告|冲突\", \"stance\": \"一句话说明你的立场和意图（不超过20字）\"}}"

        raw = await llm_call(system_prompt, user_prompt, max_tokens=300)
        parsed = _parse_json_from_llm(raw)

        return {
            "npc_id": self.npc_id,
            "name": self.name,
            "action_type": parsed.get("action_type", "观望"),
            "stance": parsed.get("stance", ""),
        }

    async def _reason_llm(self, context: Dict[str, Any], session=None,
                          intent_context: list = None, strategy: str = "aggressive") -> Dict[str, Any]:
        """第二轮：完整推理，注入其他NPC的意图作为跨角色感知"""
        npc = self.npc_data
        user_action = context.get("user_action", "")
        history = context.get("history", {})
        rel_value = self.world_state.relationships.get(self.npc_id, 0)

        visible_info = self._gather_visible_info(context)
        system_prompt = self._build_npc_system_prompt(rel_value)
        user_prompt = self._build_npc_user_prompt(user_action, visible_info, context)

        if intent_context:
            others = [i for i in intent_context if i["npc_id"] != self.npc_id and i["action_type"] != "观望"]
            if others:
                lines = [f"- {i['name']}：{i['action_type']}（{i['stance']}）" for i in others]
                user_prompt += f"\n\n## 你察觉到的其他角色动向\n\n" + "\n".join(lines) + "\n\n注意：这些是你的主观感知，可能不完全准确。根据这些信息调整你的行动。"

        tool_type = "npc_extended" if strategy == "aggressive" else "npc"
        tools, executors = _build_tool_schemas_and_executors(tool_type, self.world_state, session)
        raw, tool_calls = await llm_call_with_tools(system_prompt, user_prompt, tools, executors, max_tokens=2000)
        parsed = _parse_json_from_llm(raw)

        return {
            "agent_id": self.npc_id,
            "name": self.name,
            "thought": parsed.get("thought", ""),
            "emotion": parsed.get("emotion", ""),
            "action_type": parsed.get("action_type", "观望"),
            "dialogue": parsed.get("dialogue"),
            "side_effects": [],
            "uncertainty": parsed.get("uncertainty", []),
            "tool_calls": tool_calls,
        }

    def _gather_visible_info(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """收集 NPC 能看到的信息（有限视角）"""
        history = context.get("history", {})
        visible = {
            "current_location": self.world_state.current_location,
            "current_date": self.world_state.current_date.isoformat(),
            "relationship_with_player": self.world_state.relationships.get(self.npc_id, 0),
        }
        recent = history.get("recent_actions", [])[-3:]
        if recent:
            visible["recent_player_actions"] = recent
        return visible

    def _build_npc_system_prompt(self, rel_value: int) -> str:
        """构建强化的 NPC 系统提示（采用酒馆的清晰约束和示例）"""
        npc = self.npc_data
        name = npc.get("name", self.npc_id)
        title = npc.get("title", "")
        personality = npc.get("personality", "")
        goals = npc.get("goals", [])
        backstory = npc.get("backstory", "")

        goals_text = "\n".join(f"  - {g}" for g in goals) if goals else "  （无特定目标）"

        prompt = f"""你是这个RPG推演系统中的一个NPC角色。

## 角色身份

姓名：{name}
身份/职位：{title if title else "（无特定身份）"}
性格特点：{personality if personality else "（待定）"}
你与玩家的关系值：{rel_value}/100

## 角色目标和动机

{goals_text}

## 背景故事

{backstory if backstory else "（背景待补充）"}

## 推理约束和指导

1. **有限视角**：你只知道这个NPC能知道的东西
   - 不可知道其他NPC的真实想法或行动
   - 不可知道玩家未明确告诉你的事情
   - 只能基于可观察的现象进行推断

2. **言辞真实**：
   - 用这个NPC的习惯表达方式说话
   - 如果不确定，要明确表达不确定（"我不太清楚"）
   - 对话要符合你的身份和当前关系

3. **行动选择**：选择最符合你的目标和当前局势的行动
   - 主动联系：主动找玩家或其他NPC交互
   - 被动反应：对玩家行动的反应
   - 主动谈判：提出某个交易或条件
   - 观望：在这个回合不采取行动
   - 警告：表达不满或担忧
   - 冲突：产生对抗或争执

4. **关系变化**：改变幅度应与行动强度匹配（-5到+5是常规范围）

5. **不可知信息示例**：
   ✗ 错误："其他NPC正在..."（你不能知道其他NPC的行动）
   ✓ 正确："我听到了..."或"我推断..."（基于可观察的信息）

## 输出格式

返回纯 JSON，不要有其他文本。必须包含以下字段：
{{
  "thought": "你对当前局势的内心分析（1-2句，不超过50字）",
  "emotion": "你当前的情绪状态（1-2个词）",
  "action_type": "主动联系|被动反应|主动谈判|观望|警告|冲突 之一",
  "dialogue": "你要说的话（不说话则为 null，对话必须符合身份）",
  "uncertainty": ["不确定的事项1", "不确定的事项2"]
}}

注意：所有状态修改（关系变化、物品给予等）请使用工具完成，不要在 JSON 中添加。"""
        return prompt

    def _build_npc_user_prompt(self, user_action: str, visible_info: Dict[str, Any],
                                context: Dict[str, Any]) -> str:
        """构建 NPC 用户提示"""
        npc = self.npc_data
        name = npc.get("name", self.npc_id)
        rel_value = visible_info.get("relationship_with_player", 0)

        recent_acts = visible_info.get("recent_player_actions", [])
        if recent_acts:
            history_text = "\n".join(f"  - {a}" for a in recent_acts[-2:])
        else:
            history_text = "  （这是本次推演的开始）"

        loc_id = visible_info.get("current_location", "")
        loc_name = loc_id
        for loc in self.world_state.script_data.get("locations", []):
            if loc.get("id") == loc_id:
                loc_name = loc.get("name", loc_id)
                break

        prompt = f"""## 当前游戏状态

时间：{visible_info.get("current_date", "?")}
地点：{loc_name}
你与玩家的关系：{_semantic_relationship("玩家", rel_value)}

## 最近发生的事情

{history_text}

## 玩家的行动

{user_action}

## 任务
"""
        my_ctx = context.get("npc_contexts", {}).get(self.npc_id, {})
        shared = context.get("shared", {})

        # 核心段（不计入预算）：记忆
        npc_mem = my_ctx.get("memories", [])
        if npc_mem:
            prompt += "\n\n## 你的近期记忆\n\n" + "\n".join(f"- {m}" for m in npc_mem)

        # 扩展段按优先级排列，总预算 1500 字
        budget = 1500
        extensions = []

        # 扩展1 (高优先): 日程+计划+弧线 合并
        status_parts = []
        if my_ctx.get("agenda"):
            status_parts.append(f"日程：{my_ctx['agenda']}")
        npc_plan = my_ctx.get("plan")
        if npc_plan:
            status_parts.append(f"目标：{npc_plan.get('goal', '?')}，下一步：{npc_plan.get('next_step', '?')}")
        emotion_arc = my_ctx.get("emotion_arc", [])
        if len(emotion_arc) >= 3:
            status_parts.append(f"情绪：{'→'.join(emotion_arc[-5:])}")
        if status_parts:
            block = "\n".join(status_parts)
            extensions.append(("## 你的当前状态\n\n" + block, len(block)))

        # 扩展2: lorebook (截断到 500字)
        lore = shared.get("lorebook", "")
        if lore:
            truncated = lore[:500] + ("..." if len(lore) > 500 else "")
            extensions.append(("## 相关背景知识\n\n" + truncated, len(truncated)))

        # 扩展3: 事件
        ev = shared.get("event_inject", "")
        if ev:
            truncated = ev[:400]
            extensions.append(("## 当前事件\n\n" + truncated, len(truncated)))

        # 扩展4: NPC间关系
        if my_ctx.get("relationships"):
            extensions.append((my_ctx["relationships"], len(my_ctx["relationships"])))

        # 扩展5: 知道的信息
        if my_ctx.get("knowledge"):
            block = "; ".join(my_ctx["knowledge"])
            extensions.append(("## 你知道的信息\n\n" + block, len(block)))

        # 扩展6: 语义记忆（历史相关片段）
        sem = context.get("semantic_history", [])
        if sem:
            sem_lines = [s.get("text", s) if isinstance(s, dict) else str(s) for s in sem[:5]]
            block = "\n".join(f"- {l[:200]}" for l in sem_lines if l)
            if block:
                extensions.append(("## 相关历史片段\n\n" + block, len(block)))

        # 扩展7: 焦点
        focus = context.get("director", {}).get("focus_hint", "")
        if focus:
            extensions.append(("## 本轮焦点\n\n" + focus, len(focus)))

        # 按预算裁剪
        used = 0
        for text, size in extensions:
            if used + size > budget:
                break
            prompt += f"\n\n{text}"
            used += size

        if npc_plan:
            prompt += "\n如果情况有变，可以用 update_plan 工具调整你的计划。"
        prompt += f"""
以 {name} 的身份，根据上述信息进行推理和反应。注意：
- 只使用你能看到或推断的信息
- 明确表达你不知道的事物
- 选择最符合你目标和性格的行动"""
        return prompt

    def _reason_rules(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """泛化规则推理：从 NPC 数据动态生成反应"""
        output = {
            "agent_id": self.npc_id,
            "name": self.name,
            "thought": "",
            "emotion": "",
            "action_type": "观望",
            "dialogue": None,
            "side_effects": [],
            "uncertainty": []
        }

        user_action = context.get("user_action", "")
        npc = self.npc_data
        attitude = npc.get("attitude_toward_player", 50)
        caps = npc.get("capabilities", "")
        name = npc.get("name", "")

        mentioned = bool(name and name in user_action)

        relevant, matched_cap = False, ""
        if caps:
            for kw in re.split(r'[、,，。；;和]', caps):
                kw = kw.strip()
                if len(kw) >= 2 and kw in user_action:
                    relevant, matched_cap = True, kw
                    break

        if not mentioned and not relevant:
            output["thought"] = "这件事与我无关。"
            return output

        if attitude >= 70:
            output["emotion"] = "积极"
            output["action_type"] = "主动互动" if mentioned else "被动支持"
            if mentioned:
                output["dialogue"] = "你找我？我很乐意帮忙。"
            else:
                output["thought"] = "我注意到了，如果需要我会帮忙。"
            rel_delta = 1
        elif attitude >= 40:
            output["emotion"] = "平静"
            output["action_type"] = "被动反应" if mentioned else "观望"
            if mentioned:
                output["dialogue"] = "嗯，什么事？"
            rel_delta = 0
        else:
            output["emotion"] = "冷淡"
            output["action_type"] = "警告" if mentioned else "观望"
            if mentioned:
                output["dialogue"] = "你来找我？我们之间好像有些问题。"
            else:
                output["thought"] = "我对此不太感兴趣。"
            rel_delta = -1

        if rel_delta:
            output.setdefault("tool_calls", []).append({
                "name": "modify_relationship",
                "args": {"npc_id": self.npc_id, "delta": rel_delta},
                "result": {"type": "modify_relationship", "npc_id": self.npc_id, "delta": rel_delta, "status": "proposed"},
            })

        if matched_cap:
            output["thought"] = f"这涉及到{matched_cap}，正好是我的领域。"

        return output


class OutlineAgent:
    """大纲 Agent：综合 NPC 输出，提议状态变更（冲突检测已拆给 ConflictDetector，一致性校验已拆给 ContinuityValidator）"""

    @staticmethod
    async def synthesize_llm(
        context: Dict[str, Any],
        npc_outputs: Dict[str, Any],
        world_state: "WorldStateManager",
        session = None,
    ) -> Dict[str, Any]:
        # 精简 NPC 摘要：只保留 action_type + dialogue 简报 + 已执行工具
        npc_brief_lines = []
        npc_tool_calls_summary = []
        for npc_id, out in npc_outputs.items():
            if out.get("skipped"):
                continue
            line = f"- {out.get('name', npc_id)}: {out.get('action_type', '观望')}"
            if out.get("dialogue"):
                d = out["dialogue"]
                line += f"，说：「{d[:120]}{'...' if len(d) > 120 else ''}」"
            npc_brief_lines.append(line)
            for tc in out.get("tool_calls", []):
                npc_tool_calls_summary.append(f"- {out.get('name', npc_id)}: {tc.get('name', '')}({json.dumps(tc.get('args', {}), ensure_ascii=False)})")
        npc_text = "\n".join(npc_brief_lines) if npc_brief_lines else "（无NPC参与）"

        system_prompt = f"""你是RPG推演系统的大纲分析师（Outline Agent）。
你的任务是综合各NPC的行动结果，进行事实总结和状态变更提议。

## 职责

1. **事实总结**：用1-2句话总结本轮发生了什么（只陈述事实，无修辞）
2. **状态变更**：使用工具提议状态变更（移动玩家、修改属性等），玩家确认后生效

## 状态变更工具（confirm后才执行）

- apply_state_change: 修改玩家属性或关系值
- move_player: 移动玩家到新地点
- set_world_prop: 修改世界属性
- adjust_tension: 调节叙事紧张度（高紧张→快节奏，低紧张→慢节奏）
- change_faction_reputation: 修改玩家在某阵营的声望值
- request_re_reason: 当NPC输出存在严重矛盾时，请求该NPC重新推理（限用1次，仅在严重矛盾时使用）

## 环境修改

可通过 set_world_prop 设置环境参数（如 weather=stormy, lighting=dim, atmosphere=tense），SceneAgent会据此调整描写。

## 约束

- 不可凭空发明未由NPC提议的事件
- 状态变更必须有合理依据

## 输出格式

在使用工具后，返回纯JSON：
{{
  "summary": "本轮发生了什么（1-2句事实陈述）",
  "next_phase": "下一步应该发生什么"
}}"""

        user_prompt = f"""玩家行动：{context.get('user_action', '')}
当前位置：{world_state.current_location}
轮次：{world_state.current_turn}
日期：{world_state.current_date.isoformat()}

各NPC的行动：
{npc_text}
"""
        if npc_tool_calls_summary:
            user_prompt += "\nNPC已执行的工具调用：\n" + "\n".join(npc_tool_calls_summary) + "\n"

        history = context.get("history", {})
        recent_actions = history.get("recent_actions", [])
        if recent_actions:
            user_prompt += "\n## 近期历史\n\n" + "\n".join(f"- {a}" for a in recent_actions[-5:]) + "\n"

        faction_rep = context.get("shared", {}).get("faction_reputation")
        if faction_rep:
            rep_lines = [f"- {fid}: {fd.get('title','中立')}({fd.get('value',50)})" for fid, fd in faction_rep.items()]
            user_prompt += "\n## 阵营声望\n\n" + "\n".join(rep_lines) + "\n"

        sem = context.get("semantic_history", [])
        if sem:
            sem_lines = [s.get("text", s) if isinstance(s, dict) else str(s) for s in sem[:5]]
            sem_text = "\n".join(f"- {l[:200]}" for l in sem_lines if l)
            if sem_text:
                user_prompt += "\n## 相关历史记忆\n\n" + sem_text + "\n"

        # 混合供给：Push lorebook 到 OutlineAgent
        lore = context.get("shared", {}).get("lorebook", "")
        if lore:
            user_prompt += f"\n## 相关背景知识\n{lore[:500]}\n"

        user_prompt += "\n请综合以上信息，使用工具提议状态变更，然后返回分析结果。"

        tools, executors = _build_tool_schemas_and_executors("outline", world_state, session)
        raw, tool_calls = await llm_call_with_tools(system_prompt, user_prompt, tools, executors, max_tokens=1500)
        parsed = _parse_json_from_llm(raw)

        re_reason_requests = [
            tc["result"] for tc in tool_calls
            if tc.get("result", {}).get("type") == "request_re_reason"
        ]

        return {
            "summary": parsed.get("summary", ""),
            "next_phase": parsed.get("next_phase", ""),
            "tool_calls": [tc for tc in tool_calls if tc.get("result", {}).get("type") != "request_re_reason"],
            "re_reason_requests": re_reason_requests[:1],
        }

    @staticmethod
    def synthesize_rules(
        context: Dict[str, Any],
        npc_outputs: Dict[str, Any],
        world_state: "WorldStateManager",
    ) -> Dict[str, Any]:
        active_npcs = []
        for npc_id, output in npc_outputs.items():
            if output.get("action_type") not in ("观望", "无关"):
                active_npcs.append({
                    "npc_id": npc_id,
                    "name": output.get("name"),
                    "action": output.get("action_type"),
                })

        if active_npcs:
            names = [npc["name"] for npc in active_npcs[:2]]
            summary = f"本轮有{len(active_npcs)}个关键人物参与：{', '.join(names)}等。"
        else:
            summary = "本轮没有重要事件发生。"

        all_tool_calls = []
        for npc_id, out in npc_outputs.items():
            all_tool_calls.extend(out.get("tool_calls", []))

        return {
            "summary": summary,
            "next_phase": "等待玩家做出选择或继续推进故事",
            "tool_calls": all_tool_calls,
        }


class ConflictDetector:
    """轻量冲突检测 Agent：分析 NPC 行动之间的对立关系，与 OutlineAgent 并行"""

    @staticmethod
    async def detect(npc_outputs: dict) -> list:
        active = {k: v for k, v in npc_outputs.items()
                  if v.get("action_type") not in ("观望", "无关") and not v.get("skipped")}
        if len(active) < 2:
            return []

        npc_brief = []
        for npc_id, out in active.items():
            line = f"{out.get('name', npc_id)}: {out.get('action_type')}"
            if out.get("dialogue"):
                line += f"「{out['dialogue'][:80]}」"
            if out.get("thought"):
                line += f"（想：{out['thought'][:60]}）"
            npc_brief.append(line)

        system_prompt = "你是冲突分析师。判断以下NPC行动之间是否存在对立或冲突。只返回JSON。"
        user_prompt = (
            "NPC行动：\n" + "\n".join(npc_brief) +
            '\n\n返回：{"conflicts": [{"between": ["id1","id2"], "severity": "high|medium|low", "topic": "冲突主题"}]}'
            "\n规则：目标直接对立=high，立场不同但未正面冲突=medium，仅有潜在分歧=low。无冲突则返回空列表。"
        )
        try:
            raw = await llm_call(system_prompt, user_prompt, max_tokens=500)
            parsed = _parse_json_from_llm(raw)
            return parsed.get("conflicts", [])
        except Exception:
            return ConflictDetector.detect_rules(npc_outputs)

    @staticmethod
    def detect_rules(npc_outputs: dict) -> list:
        _OPPOSING = {"冲突", "警告", "威胁", "攻击"}
        conflicts = []
        ids = [k for k, v in npc_outputs.items()
               if v.get("action_type") in _OPPOSING and not v.get("skipped")]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                conflicts.append({
                    "between": [a, b],
                    "severity": "high" if npc_outputs[a]["action_type"] == "冲突"
                                       and npc_outputs[b]["action_type"] == "冲突" else "medium",
                    "topic": "",
                })
        return conflicts


class SceneAgent:
    """场景 Agent：基于大纲分析结果，生成沉浸式场景描写和选择分支"""

    @staticmethod
    async def generate_llm(
        context: Dict[str, Any],
        npc_outputs: Dict[str, Any],
        world_state: "WorldStateManager",
        outline: Dict[str, Any],
        session = None,
        scene_budget: str = "normal",
    ) -> Dict[str, Any]:
        _BUDGET_MAP = {"short": (150, 300, 1200), "normal": (300, 600, 2000), "long": (500, 1000, 3000)}
        min_len, max_len, max_tokens = _BUDGET_MAP.get(scene_budget, _BUDGET_MAP["normal"])
        # 精简 NPC 摘要：只保留叙事所需的行动和对话
        npc_brief_lines = []
        for npc_id, out in npc_outputs.items():
            if out.get("skipped"):
                continue
            line = f"- {out.get('name', npc_id)}: {out.get('action_type', '观望')}"
            if out.get("dialogue"):
                d = out["dialogue"]
                line += f"，说：「{d[:120]}{'...' if len(d) > 120 else ''}」"
            elif out.get("emotion"):
                line += f"（{out['emotion']}）"
            npc_brief_lines.append(line)
        npc_text = "\n".join(npc_brief_lines) if npc_brief_lines else "（无NPC参与）"

        system_prompt = f"""你是RPG游戏的场景叙事师（Scene Agent）。
你需要基于大纲分析结果，生成沉浸式的场景描写和选择分支。

## 1. 生成场景描写

- **视角**：第二人称（"你"）
- **长度**：{min_len}-{max_len}字，精炼有力，融入感官细节和NPC微表情
- **融合**：自然融入NPC对话和行动后果
- **约束**：只描写当前场景，不跳出当前时空

## 2. 生成选择分支

- **数量**：3-4个选择
- **多样性**：不同策略和角色反应
- **后果清晰**：每个选择的immediate效果明确
- **影响平衡**：没有明显的"最优解"
- **具体性**：选择必须基于当前地点特征、在场人物、玩家属性和持有物品来设计，不给出"继续探索""看看周围"等泛化选项

## UI效果工具（立即执行）

- play_sound: 播放音效（ambient, tension, wonder, triumph, sorrow）
- set_weather: 设置天气（sunny, cloudy, rainy, stormy, night）
- set_mood_filter: 设置滤镜（calm, tense, mystical, dark, warm）

## 输出格式

在使用工具后，返回纯JSON：
{{"scene_text": "沉浸式场景描写（含感官细节和NPC微表情）", "atmosphere": "一句话概括本轮氛围", "choices": [{{"id": "A", "text": "选择内容", "effects": {{...}}}}]}}

注意：
- effects 可含字段：relationship_changes, skill_check, resource_cost, delayed_consequence
- skill_check 仅在该选择涉及有风险或不确定性的行动时才加（攻击、偷窃、说服等），日常对话不需要
- resource_cost 仅在该选择消耗玩家资源时才加（体力、金币等），为负值表示消耗
- delayed_consequence 仅在该选择会产生后续影响时才加，描述几回合后可能发生的后果
- 不是所有选择都需要这些字段，根据叙事合理性决定"""

        mood = context.get("director", {}).get("mood", "")
        mood_line = f"\n玩家情绪氛围：{mood}" if mood else ""
        pacing = context.get("pacing_hint", "")
        pacing_line = f"\n\n## 节奏指导\n{pacing}" if pacing else ""

        hijack_line = ""
        hijack = context.get("scene_hijack")
        if hijack:
            hijack_line = (
                f"\n\n## 场景劫持\n本回合由NPC主导场景。{hijack.get('npc_name', '')}因「{hijack.get('reason', '')}」"
                f"打断玩家行动。叙事焦点转移到NPC的主动行为上。\n"
                f"建议场景: {hijack.get('suggested_action', '')}"
            )

        plan_line = ""
        if context.get("has_plan_declaration"):
            plan_line = (
                "\n\n## 计划分解\n"
                "玩家声明了一个多步骤计划。在场景描写后，额外在choices中添加计划步骤拆解。"
                "将计划分解为3-5个具体可执行步骤作为选择分支。"
            )

        outline_summary = outline.get("summary", "")
        outline_conflicts = json.dumps(outline.get("conflicts", []), ensure_ascii=False)

        # 把 outline 的工具提议摘要注入 SceneAgent（借鉴酒馆 PromptBuilder 将状态变更结果注入叙事的模式）
        outline_actions_line = ""
        outline_tcs = outline.get("tool_calls", [])
        if outline_tcs:
            action_descs = []
            for tc in outline_tcs[:5]:
                r = tc.get("result", {})
                t = r.get("type", tc.get("name", ""))
                if t == "move_player":
                    action_descs.append(f"移动到{r.get('location_id', '?')}")
                elif t == "apply_state_change":
                    action_descs.append(f"{r.get('var', '?')}{r.get('op', 'set')}{r.get('value', '')}")
                elif t == "set_world_prop":
                    action_descs.append(f"世界属性变更：{r.get('key', '?')}={r.get('value', '')}")
                elif t == "spawn_npc":
                    action_descs.append(f"新角色登场：{r.get('name', '?')}")
                elif t == "change_faction_reputation":
                    action_descs.append(f"声望变化：{r.get('faction_id', '?')}{r.get('delta', 0):+d}")
                elif t == "adjust_tension":
                    action_descs.append(f"紧张度调整{r.get('delta', 0):+d}")
                else:
                    action_descs.append(t)
            if action_descs:
                outline_actions_line = f"\n状态变更提议：{'、'.join(action_descs)}（请将这些变化自然融入叙事中）"

        # 环境参数注入
        env_line = ""
        wp = world_state.world_props
        env_params = {k: v for k, v in wp.items() if k in ("weather", "lighting", "atmosphere", "season", "time_of_day", "天气", "光照", "氛围", "季节")}
        if env_params:
            env_line = "\n环境：" + "、".join(f"{k}={v}" for k, v in env_params.items())

        user_prompt = f"""玩家行动：{context.get('user_action', '')}
当前位置：{world_state.current_location}
轮次：{world_state.current_turn}
日期：{world_state.current_date.isoformat()}{env_line}{mood_line}{pacing_line}{hijack_line}{plan_line}

大纲分析：{outline_summary}{outline_actions_line}
冲突：{outline_conflicts}

各NPC的行动：
{npc_text}
"""
        passive_checks = context.get("passive_checks")
        if passive_checks:
            labels = {"success": "成功", "failure": "失败", "critical_success": "大成功", "critical_failure": "大失败"}
            check_lines = [f"- {pc['trigger_npc']}试图{pc['action_type']}，玩家{labels.get(pc['result']['outcome'], '?')}"
                           for pc in passive_checks]
            user_prompt += "\n## 被动检定\n\n" + "\n".join(check_lines) + "\n（成功=玩家察觉，失败=浑然不觉，融入叙事中）\n"

        npc_dialogues = context.get("npc_dialogues")
        if npc_dialogues:
            for nd in npc_dialogues:
                user_prompt += f"\n## {nd['npc_a_name']}与{nd['npc_b_name']}的冲突对话\n\n{nd['dialogue']}\n"
            user_prompt += "（请将以上NPC间的对话自然融入场景叙事中）\n"

        # 玩家具体上下文（帮助选项具体化）
        if world_state.player_attrs:
            attr_defs = world_state.script_data.get("player_character", {}).get("attributes", {})
            attrs_lines = []
            for k, v in list(world_state.player_attrs.items())[:8]:
                attrs_lines.append(_semantic_attr_value(k, v, attr_defs.get(k)))
            user_prompt += f"\n玩家属性：\n" + "\n".join(attrs_lines) + "\n"
        inventory = world_state.variables.get("inventory", [])
        if inventory:
            inv_names = [i.get("name", str(i)) if isinstance(i, dict) else str(i) for i in inventory[:8]]
            user_prompt += f"玩家背包：{'、'.join(inv_names)}\n"
        loc_id = world_state.current_location
        loc_data = next((l for l in world_state.script_data.get("locations", []) if l.get("id") == loc_id), None)
        if loc_data and loc_data.get("description"):
            user_prompt += f"地点「{loc_data.get('name', loc_id)}」：{loc_data['description'][:300]}\n"
        present_npcs = []
        for npc_id, out in npc_outputs.items():
            if not out.get("skipped"):
                rel = world_state.relationships.get(npc_id, 0)
                present_npcs.append(_semantic_relationship(out.get('name', npc_id), rel))
        if present_npcs:
            user_prompt += f"在场角色：{'、'.join(present_npcs)}\n"

        user_prompt += "\n请基于以上信息，使用UI效果工具增强氛围，然后生成场景描写和选择分支。"

        # 混合供给：Push lorebook 和 world_props
        lore = context.get("shared", {}).get("lorebook", "")
        if lore:
            user_prompt += f"\n\n## 相关背景知识\n{lore[:800]}\n"
        if world_state.world_props:
            wp_defs = {wp.get("id"): wp for wp in world_state.script_data.get("world_properties", []) if isinstance(wp, dict)}
            wp_lines = []
            for k, v in world_state.world_props.items():
                wp_def = wp_defs.get(k, {})
                wp_name = wp_def.get("name", k)
                rule = wp_def.get("rule", "")
                if rule:
                    wp_lines.append(f"{wp_name}={v} — {rule[:60]}")
                else:
                    wp_lines.append(f"{wp_name}={v}")
            user_prompt += f"\n世界状态：\n" + "\n".join(wp_lines) + "\n"

        tools, executors = _build_tool_schemas_and_executors("scene", world_state, session)
        raw, tool_calls = await llm_call_with_tools(system_prompt, user_prompt, tools, executors, max_tokens=max_tokens)
        parsed = _parse_json_from_llm(raw)

        dialogues = []
        for npc_id, out in npc_outputs.items():
            if out.get("dialogue"):
                dialogues.append({"speaker": out.get("name"), "text": out["dialogue"]})

        return {
            "turn": world_state.current_turn,
            "timestamp": datetime.now().isoformat(),
            "location": world_state.current_location,
            "scene_text": parsed.get("scene_text", ""),
            "atmosphere": parsed.get("atmosphere", ""),
            "state_snapshot": world_state.get_snapshot(),
            "active_agents": [npc_id for npc_id, out in npc_outputs.items()
                             if out.get("action_type") not in ("观望", "无关")],
            "dialogue_log": dialogues,
            "choices": parsed.get("choices", []),
            "ui_effects": [{"type": tc["name"], "params": tc["args"], "result": tc["result"]} for tc in tool_calls],
        }

    @staticmethod
    def generate_rules(
        context: Dict[str, Any],
        npc_outputs: Dict[str, Any],
        world_state: "WorldStateManager",
        outline: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        user_action = context.get("user_action", "")
        scene_texts = {
            "转会": "你坐在公寓客厅，手机屏幕闪烁着豪尔赫的来电提示...",
            "训练": "训练基地的草坪上，晨曦中你开始了新一天的训练...",
            "比赛": "球场上，主队球迷的欢呼声震撼着你的耳膜...",
            "家人": "你拨通了家里的电话，等待亲人的声音传来...",
        }
        scene_text = "故事继续推进，新的一页即将开启..."
        for keyword, text in scene_texts.items():
            if keyword in user_action:
                scene_text = text
                break

        dialogues = []
        for npc_id, output in npc_outputs.items():
            if output.get("dialogue"):
                dialogues.append({"speaker": output.get("name"), "text": output["dialogue"]})

        choices = [
            {"id": "A", "text": "积极回应，表达真实想法", "preview": "这会增加部分NPC好感，但可能激化冲突", "effects": {"relationship_changes": {}}},
            {"id": "B", "text": "保守回应，暂时搁置决定", "preview": "保持平稳，但风险积累", "effects": {"relationship_changes": {}}},
            {"id": "C", "text": "转移话题，避免深谈", "preview": "短期回避，但被察觉风险高", "effects": {"relationship_changes": {}}},
        ]

        return {
            "turn": world_state.current_turn,
            "timestamp": datetime.now().isoformat(),
            "location": world_state.current_location,
            "scene_text": scene_text,
            "atmosphere": "",
            "state_snapshot": world_state.get_snapshot(),
            "active_agents": [npc_id for npc_id, out in npc_outputs.items()
                             if out.get("action_type") not in ("观望", "无关")],
            "dialogue_log": dialogues,
            "choices": choices,
            "ui_effects": [],
        }


class ContinuityValidator:
    """后验质量校验：检查场景叙事与世界状态的一致性"""

    @staticmethod
    async def validate(scene_text: str, world_state: "WorldStateManager",
                       script_data: dict) -> dict:
        existing_npcs = [n.get("name", "") for n in script_data.get("npcs", [])]
        existing_locs = [l.get("name", "") for l in script_data.get("locations", [])]
        player_attrs_brief = ", ".join(
            f"{k}={v}" for k, v in list(world_state.player_attrs.items())[:10]
        )

        system_prompt = "你是RPG世界一致性校验员。检查叙事文本与世界状态是否矛盾。只返回JSON。"
        user_prompt = f"""叙事文本：
{scene_text[:1200]}

已有角色：{', '.join(n for n in existing_npcs if n) or '无'}
已有地点：{', '.join(n for n in existing_locs if n) or '无'}
玩家属性：{player_attrs_brief}
当前位置：{world_state.current_location}

返回：
{{
  "needs_expansion": true/false,
  "new_names": ["叙事中出现但不在已有列表中的角色/地点名"],
  "inconsistencies": ["叙事与世界状态的矛盾（如位置不对、属性不符）"]
}}
不矛盾则 inconsistencies 为空。无新名字则 needs_expansion=false。"""
        try:
            raw = await llm_call(system_prompt, user_prompt, max_tokens=600)
            return _parse_json_from_llm(raw) or {"needs_expansion": False, "inconsistencies": []}
        except Exception:
            return ContinuityValidator.validate_rules(scene_text, script_data)

    @staticmethod
    def validate_rules(scene_text: str, script_data: dict) -> dict:
        existing = {n.get("name", "") for n in script_data.get("npcs", [])} | \
                   {l.get("name", "") for l in script_data.get("locations", [])}
        quoted = re.findall(r'「(.+?)」|"(.+?)"', scene_text)
        new_names = []
        for tup in quoted:
            name = tup[0] or tup[1]
            if name and len(name) >= 2 and name not in existing:
                new_names.append(name)
        return {"needs_expansion": bool(new_names), "new_names": new_names, "inconsistencies": []}


class TavernRPGSession:
    """主会话类：协调整个推演流程"""

    def __init__(self, script_id: str = "football_legend"):
        """初始化会话，加载剧本 + 复杂结构"""
        # 加载剧本
        try:
            with open(os.path.join(_BASE_DIR, "data/scripts/football_legend.json"), "r", encoding="utf-8") as f:
                self.script_data = json.load(f)
        except FileNotFoundError:
            print("[WARN] 找不到football_legend.json，使用最小化剧本")
            self.script_data = {"script_id": script_id, "npcs": []}

        # 初始化世界状态
        self.world_state = WorldStateManager(self.script_data)

        # 初始化NPC Agents
        self.npc_agents: Dict[str, NPCAgent] = {}
        for npc in self.script_data.get("npcs", []):
            npc_id = npc.get("id")
            self.npc_agents[npc_id] = NPCAgent(npc_id, npc, self.world_state)

        # 推演 pending 状态
        self._pending_turn = None  # 存储待确认的推演结果
        self._last_validation = {}  # 最近一次校验结果

        # === 引入酒馆的复杂结构 ===
        if TAVERN_MODULES_AVAILABLE:
            self.world_tree = WorldTree(script_id=script_id)
            self.event_engine = EventEngine(self.script_data)
            self.lorebook = Lorebook(self.script_data.get("lorebook", []))
            self.history_summarizer = HistorySummarizer()
            # 新增模块
            self.state_manager = self.world_state._state_manager
            self.dice_roller = DiceRoller()
            self.script_variables = ScriptVariables(self.script_data.get('variables', []))
            self.script_variables.init_state(self.world_state.event_state)
            self.trigger_engine = TriggerEngine(
                self.script_data.get('triggers', []), self.script_variables)
            self.meta_event_bus = MetaEventBus()
            self.vector_memory = VectorMemory(script_id) if VectorMemory and _VECTOR_AVAILABLE else None
            self.class_system = ClassRegistry(self.script_data.get('system')) if ClassRegistry else None
            print(f"[OK] WorldTree, EventEngine, Lorebook 已加载")
        else:
            self.world_tree = None
            self.event_engine = None
            self.lorebook = None
            self.history_summarizer = None
            self.state_manager = None
            self.dice_roller = DiceRoller()
            self.script_variables = None
            self.trigger_engine = None
            self.meta_event_bus = MetaEventBus()
            self.vector_memory = None
            self.class_system = None
            print(f"[WARN] 酒馆模块不可用，某些功能受限")

        # Pacing 系统初始化
        self.tension = 0  # 0-100, 越高越紧张
        self._tension_afterglow = 0  # 余韵期剩余轮数
        self._tension_peak = 0  # 最近的 tension 峰值
        self._pending_tension_adjustment = 0  # OutlineAgent 的 tension 手动调节
        self.npc_chat_history: Dict[str, list] = {}  # NPC 对话历史
        self.npc_intervention_cooldowns: Dict[str, int] = {}  # NPC 干预冷却
        self.npc_memories: Dict[str, list] = {}  # NPC 跨回合记忆
        self.npc_plans: Dict[str, Dict[str, Any]] = {}  # NPC 长期计划
        self.npc_emotion_arcs: Dict[str, list] = {}  # NPC 情感弧线

        # NPC 间关系网络（复用酒馆 npc_relationships_global/known 结构）
        self.npc_relationships_global: Dict[str, dict] = {}
        self.npc_relationships_known: Dict[str, dict] = {}
        self._REL_NET_MAX = 80

        # 信息不对称系统（复用酒馆 information_network 结构）
        self.information_network: list = []

        # 阵营声望系统（复用酒馆 faction_reputation 结构）
        self.faction_reputation: Dict[str, dict] = {}

        # 工具执行器映射表
        self._tool_handlers = {
            "modify_relationship": self._exec_modify_relationship,
            "apply_state_change": self._exec_apply_state_change,
            "move_player": self._exec_move_player,
            "set_world_prop": self._exec_set_world_prop,
            "give_item": self._exec_give_item,
            "take_item": self._exec_take_item,
            "spawn_npc": self._exec_spawn_npc,
            "remove_npc": self._exec_remove_npc,
            "update_npc_relationship": self._exec_update_npc_relationship,
            "share_information": self._exec_share_information,
            "update_plan": self._exec_update_plan,
            "change_faction_reputation": self._exec_change_faction_reputation,
            "trigger_event": self._exec_trigger_event,
        }

        print(f"[OK] 酒馆RPG系统初始化完成")
        print(f"  剧本: {self.script_data.get('script_name', 'Unknown')}")
        print(f"  NPC数: {len(self.npc_agents)}")
        print(f"  变量数: {len(self.world_state.variables)}")


    async def process_turn(self, player_input: str, director_plan: dict = None) -> Dict[str, Any]:
        """处理单个推演轮次（纯编排）"""
        dp = director_plan or {}
        resolved_action = dp.get("resolved_action", player_input)
        use_llm = _llm_state.get("client") is not None
        hints = dp.get("pipeline_hints", {})

        # 阶段0: 时间推进 + 事件 + 恢复
        hours = self._advance_time(dp, player_input)
        event_result = self._tick_events(resolved_action)
        self.world_state.apply_natural_recovery(hours)

        # 阶段1: 上下文组装
        print('\n[阶段1] 上下文组装...')
        lore_text, event_inject = self._scan_lorebook_and_events(player_input, event_result)
        context = self._build_context(player_input, dp, event_result, lore_text, event_inject)
        print('[OK] 收集完成')

        # 阶段2: NPC 推理
        print(f'\n[阶段2] NPC有限视角推理... ({"LLM" if use_llm else "规则"})')
        npc_outputs = await self._run_npc_phase(context, use_llm)

        # 被动技能检定
        passive_checks = self._check_passive_skills(npc_outputs)
        if passive_checks:
            context["passive_checks"] = passive_checks
            print(f'[被动检定] {len(passive_checks)} 次: {", ".join(c["trigger_npc"] + "→" + c["result"]["outcome"] for c in passive_checks)}')

        # 阶段2.5: Pacing + 干预 + 计划分解
        tension = self._compute_tension(resolved_action, npc_outputs,
                                         {"conflicts": [], "event_result_summary": context.get("shared", {}).get("event_result_summary", {})})
        context["tension"] = tension
        context["pacing_hint"] = self._pacing_hint(tension)
        if tension >= 50:
            print(f'[节奏] tension={tension}/100 → {context["pacing_hint"][:20]}...')
        self._check_interventions_and_plans(context, resolved_action)

        # 阶段3: Outline + Conflict（按 pipeline_hints 条件执行）
        do_outline = not hints.get("skip_outline", False)
        do_conflict = not hints.get("skip_conflict", False)

        if do_outline or do_conflict:
            print(f'\n[阶段3] {"大纲" if do_outline else ""}{"+" if do_outline and do_conflict else ""}{"冲突分析" if do_conflict else ""}... ({"LLM" if use_llm else "规则"})')

        if do_outline and use_llm:
            if do_conflict:
                outline_task = OutlineAgent.synthesize_llm(context, npc_outputs, self.world_state, session=self)
                conflict_task = ConflictDetector.detect(npc_outputs)
                outline, conflicts = await asyncio.gather(outline_task, conflict_task)
            else:
                outline = await OutlineAgent.synthesize_llm(context, npc_outputs, self.world_state, session=self)
                conflicts = []
        elif do_outline:
            outline = OutlineAgent.synthesize_rules(context, npc_outputs, self.world_state)
            conflicts = ConflictDetector.detect_rules(npc_outputs) if do_conflict else []
        else:
            outline = {"summary": "", "next_phase": "", "tool_calls": []}
            conflicts = ConflictDetector.detect_rules(npc_outputs) if do_conflict else []

        if do_outline or do_conflict:
            print(f'[OK] 大纲完成: {len(outline.get("tool_calls", []))} 个工具提议, 冲突: {len(conflicts)}')

        # 质量反馈环
        re_reason = outline.get("re_reason_requests", [])
        if re_reason and use_llm:
            req = re_reason[0]
            rr_agent = self.npc_agents.get(req.get("npc_id", ""))
            if rr_agent:
                print(f'[反馈] OutlineAgent 请求 {rr_agent.name} 重新推理: {req.get("reason", "")}')
                try:
                    npc_outputs[req["npc_id"]] = await rr_agent._reason_llm(context, session=self)
                    outline = await OutlineAgent.synthesize_llm(context, npc_outputs, self.world_state, session=self)
                    print(f'[反馈] 重新综合完成')
                except Exception as e:
                    print(f'[反馈] 重新推理失败: {e}')

        # 阶段3.5: NPC 对话轮
        npc_dialogues = await self._maybe_npc_dialogue(conflicts, npc_outputs, context, use_llm)
        if npc_dialogues:
            context["npc_dialogues"] = npc_dialogues
        outline["conflicts"] = conflicts

        # 阶段4: 场景生成
        scene_budget = hints.get("scene_budget", "normal")
        print(f'[阶段4] 场景生成... ({"LLM" if use_llm else "规则"}, budget={scene_budget})')
        if use_llm:
            scene = await SceneAgent.generate_llm(context, npc_outputs, self.world_state, outline,
                                                   session=self, scene_budget=scene_budget)
        else:
            scene = SceneAgent.generate_rules(context, npc_outputs, self.world_state, outline)
        scene["tension"] = tension
        print(f'[OK] 场景生成完成')

        # 阶段5: 构建 Turn 记录（同步，不等校验）
        turn = self._build_and_record_turn(player_input, context, npc_outputs, outline, scene)

        scene["npc_interventions"] = context.get("npc_interventions", [])
        if context.get("has_plan_declaration"):
            scene["has_plan_declaration"] = True

        # 阶段4.5+后处理：异步执行，不阻塞返回
        async def _deferred_post_scene():
            try:
                validation = {}
                if not hints.get("skip_validator", False):
                    if use_llm:
                        validation = await ContinuityValidator.validate(
                            scene.get("scene_text", ""), self.world_state, self.script_data)
                    else:
                        validation = ContinuityValidator.validate_rules(
                            scene.get("scene_text", ""), self.script_data)
                    if validation.get("inconsistencies"):
                        print(f'[校验] 发现 {len(validation["inconsistencies"])} 处不一致')
                    if validation.get("needs_expansion"):
                        outline["needs_expansion"] = True
                        print(f'[校验] 需要世界扩展: {validation.get("new_names", [])}')
                self._last_validation = validation
                await self._post_turn_processing(turn, scene, outline, player_input, use_llm)
                print(f'[OK] 后台校验与后处理完成')
            except Exception as e:
                print(f'[WARN] 后台校验/后处理失败: {e}')
                self._last_validation = {"error": str(e)}

        self._last_validation = {"pending": True}
        asyncio.create_task(_deferred_post_scene())

        return {"turn": turn, "scene": scene, "choices": scene.get("choices", [])}

    def _advance_time(self, dp: dict, player_input: str) -> float:
        """推进游戏时间，返回推进的小时数"""
        old_date = self.world_state.current_date.isoformat()
        self.world_state.current_turn += 1
        time_hint = dp.get("time_hint", "medium")
        hours = TIME_HINT_HOURS.get(time_hint, 2)
        self.world_state.current_date += timedelta(hours=hours)

        resolved_action = dp.get("resolved_action", player_input)
        print(f'\n{"="*60}')
        print(f'推演轮次 #{self.world_state.current_turn}')
        print(f'玩家输入: {player_input}')
        if resolved_action != player_input:
            print(f'Director解析: {resolved_action}')
        skip_info = dp.get("skip_npcs", [])
        if skip_info:
            print(f'跳过NPC: {", ".join(skip_info)}')
        print(f'时间步长: {time_hint} (+{hours}h)')
        print(f'{"="*60}')
        return hours

    def _tick_events(self, resolved_action: str):
        """EventEngine tick，返回 event_result"""
        if not self.event_engine:
            return None
        try:
            event_result = self.event_engine.tick(
                state=self.world_state.event_state,
                turn_number=self.world_state.current_turn,
                game_time=self.world_state.current_date.isoformat(),
                old_time=(self.world_state.current_date - timedelta(hours=2)).isoformat(),
                condition_eval=self._evaluate_condition,
                player_action=resolved_action,
                state_manager=self.state_manager,
            )
            self._apply_event_result(event_result)
            ac = len(event_result.newly_active)
            cc = len(event_result.newly_completed)
            if ac or cc:
                print(f'[事件] 新激活: {ac}, 完成: {cc}')
            if event_result.notifications:
                for n in event_result.notifications:
                    print(f'  [通知] {n}')
            return event_result
        except Exception as e:
            print(f'[WARN] EventEngine.tick 失败: {e}')
            return None

    def _scan_lorebook_and_events(self, player_input: str, event_result) -> tuple:
        """扫描 Lorebook + 构建事件注入文本，返回 (lore_text, event_inject)"""
        lore_text = ""
        if self.lorebook:
            try:
                recent_msgs = [t.player_action for t in self.world_state.turns[-5:]]
                activated_entries, self.world_state.lorebook_timed_state = self.lorebook.scan(
                    player_action=player_input,
                    recent_messages=recent_msgs,
                    timed_state=self.world_state.lorebook_timed_state,
                    turn_number=self.world_state.current_turn,
                )
                if activated_entries:
                    lore_text = self.lorebook.format_for_prompt(activated_entries)
                    print(f'[知识库] 激活 {len(activated_entries)} 条知识')
            except Exception as e:
                print(f'[WARN] Lorebook.scan 失败: {e}')

        event_inject = ""
        if event_result:
            parts = []
            parts.extend(event_result.inject_prompts)
            for cb in event_result.narrative_callbacks:
                text = cb.get("text", "") if isinstance(cb, dict) else str(cb)
                if text:
                    parts.append(text)
            for cons in event_result.triggered_consequences:
                desc = cons.get("description", "")
                if desc:
                    parts.append(f"[后果触发] {desc}")
            for w in event_result.imminent_warnings:
                parts.append(f"[即将发生] {w}")
            for se in event_result.scheduled_events:
                desc = se.get("description", "")
                if desc:
                    parts.append(f"[定时事件] {desc}")
            if parts:
                event_inject = '\n'.join(parts)
                print(f'[事件] {len(parts)} 条事件注入提示')
        return lore_text, event_inject

    async def _run_npc_phase(self, context: dict, use_llm: bool) -> dict:
        """阶段2：NPC 推理"""
        if use_llm:
            npc_outputs = await self._run_npc_agents_llm(context)
        else:
            npc_outputs = self._run_npc_agents_parallel(context)
        print(f'[OK] {len(npc_outputs)} 个NPC推理完成')
        for npc_id, output in npc_outputs.items():
            print(f'  - {output.get("name")}: {output.get("action_type")}')
        return npc_outputs

    def _check_interventions_and_plans(self, context: dict, resolved_action: str):
        """检查 NPC 干预和计划声明"""
        interventions = self._check_npc_interventions()
        if interventions:
            scene_hijack = None
            for iv in interventions:
                if iv.get("urgency") == "high":
                    scene_hijack = iv
                    break
            context["npc_interventions"] = interventions
            if scene_hijack:
                context["scene_hijack"] = scene_hijack
                hijack_npc = scene_hijack["npc_id"]
                if hijack_npc not in [o.get("npc_id") for o in context.get("npc_outputs", {}).values()]:
                    context["scene_type"] = "npc_hijack"
                print(f'[劫持] {scene_hijack["npc_name"]} 主动干预：{scene_hijack["reason"]}')

        if self._detect_plan_declaration(resolved_action):
            context["has_plan_declaration"] = True
            print(f'[计划] 检测到计划声明，将分解为步骤')

    async def _maybe_npc_dialogue(self, conflicts: list, npc_outputs: dict,
                                   context: dict, use_llm: bool) -> list:
        """高冲突时触发 NPC 间对话轮"""
        if not conflicts or not use_llm:
            return []
        high = [c for c in conflicts if c.get("severity") == "high"]
        if not high:
            return []
        results = []
        for conflict in high[:1]:
            between = conflict.get("between", [])
            if len(between) < 2:
                continue
            a_id, b_id = between[0], between[1]
            a_out = npc_outputs.get(a_id, {})
            b_out = npc_outputs.get(b_id, {})
            if (a_out.get("skipped") or b_out.get("skipped") or
                a_out.get("action_type") in ("观望", "无关") or
                b_out.get("action_type") in ("观望", "无关")):
                continue
            dialogue = await self._npc_dialogue_round(a_id, b_id, conflict.get("topic", ""), context)
            if dialogue:
                results.append(dialogue)
                print(f'[NPC对话] {dialogue["npc_a_name"]} vs {dialogue["npc_b_name"]}: {conflict.get("topic", "")}')
        return results

    def _build_and_record_turn(self, player_input: str, context: dict,
                                npc_outputs: dict, outline: dict, scene: dict) -> "Turn":
        """构建 Turn 对象并记录到 WorldTree"""
        state_before = self.world_state.get_snapshot()
        state_after = copy.deepcopy(state_before)
        turn = Turn(
            turn_num=self.world_state.current_turn,
            timestamp=datetime.now().isoformat(),
            player_action=player_input,
            context_object=context,
            npc_outputs=npc_outputs,
            outline_summary=outline,
            scene_output=scene,
            state_before=state_before,
            state_after=state_after,
            player_choices=scene.get("choices", [])
        )
        if self.world_tree:
            try:
                parent_node_id = self.world_tree.active_node_id
                node_id = self.world_tree.add_node(
                    parent_id=parent_node_id,
                    game_time=self.world_state.current_date.isoformat(),
                    turn_number=self.world_state.current_turn,
                    player_action={"text": player_input},
                    ai_response=scene.get('scene_text', ''),
                    choices_presented=scene.get('choices', []),
                    state_snapshot=self.world_state.get_snapshot(),
                )
                branch_count = sum(1 for n in self.world_tree.nodes.values() if not n.get("children_ids"))
                print(f'\n[树] 节点 {node_id} 已添加 (分支数: {branch_count})')
            except Exception as e:
                print(f'[WARN] 添加到WorldTree失败: {e}')
        return turn

    async def _post_turn_processing(self, turn: "Turn", scene: dict, outline: dict,
                                     player_input: str, use_llm: bool):
        """后处理：摘要、骰子、触发器、元事件、向量存储、世界扩展"""
        # HistorySummarizer
        if self.history_summarizer and self.world_tree and use_llm:
            try:
                recent_word_count = sum(
                    len(t.player_action) + len(t.scene_output.get("scene_text", ""))
                    for t in self.world_state.turns[-10:]
                )
                if self.history_summarizer.needs_summary(
                    self.world_state.current_turn, self.world_state.summary_state,
                    recent_word_count=recent_word_count,
                ):
                    branch = self.world_tree.get_active_branch()
                    keep = self.history_summarizer.keep_recent
                    older_nodes = branch[:-keep] if len(branch) > keep else []
                    if older_nodes:
                        existing_summary = self.world_state.summary_state.get("history_summary", "")
                        sys_prompt, user_prompt = self.history_summarizer.build_summary_prompt(
                            older_nodes, existing_summary
                        )
                        summary_text = await llm_call(sys_prompt, user_prompt, max_tokens=1500)
                        if summary_text:
                            self.world_state.summary_state = self.history_summarizer.update_state_with_summary(
                                self.world_state.summary_state, summary_text, self.world_state.current_turn,
                            )
                            print(f'[摘要] 历史已压缩至轮次 {self.world_state.current_turn}')
            except Exception as e:
                print(f'[WARN] HistorySummarizer 失败: {e}')

        # 骰子检定
        dice_results = []
        if self.dice_roller:
            for dice_cfg in self.script_data.get('always_active_dice', []):
                try:
                    dr = self.dice_roller.roll_and_resolve(
                        dice_cfg.get('dice', {'count': 1, 'faces': 100}),
                        dice_cfg.get('ranges', []),
                    )
                    dice_results.append({'id': dice_cfg.get('id', ''), 'formula': dr.formula,
                        'total': dr.total, 'label': dr.range_label,
                        'state_changes': dr.range_state_changes})
                    if dr.range_state_changes:
                        self.world_state.apply_changes([
                            {'var': sc.get('target', ''), 'op': sc.get('op', 'add'), 'value': sc.get('value', 0)}
                            for sc in dr.range_state_changes if sc.get('target')])
                except Exception as e:
                    print(f'[WARN] dice failed: {e}')
            if dice_results:
                print(f'[骰子] {len(dice_results)} 次检定')
        scene['dice_results'] = dice_results

        # TriggerEngine
        if self.trigger_engine:
            try:
                trigger_actions = self.trigger_engine.fire('on_turn_end', self.world_state.event_state)
                if trigger_actions:
                    effects = self.trigger_engine.execute_actions(trigger_actions, self.world_state.event_state)
                    if effects.get('notifications'):
                        for msg in effects['notifications']:
                            print(f'  [触发器] {msg}')
                    if effects.get('lore_activations') and self.lorebook:
                        for eid in effects['lore_activations']:
                            self.lorebook.update_entry_enabled(eid, True)
                    if effects.get('lore_deactivations') and self.lorebook:
                        for eid in effects['lore_deactivations']:
                            self.lorebook.update_entry_enabled(eid, False)
                    print(f'[触发器] {len(trigger_actions)} 个动作执行')
            except Exception as e:
                print(f'[WARN] TriggerEngine failed: {e}')

        # MetaEventBus
        if self.meta_event_bus:
            try:
                fired_meta = self.meta_event_bus.evaluate(
                    self.world_state.current_turn, self.world_state.event_state,
                    condition_eval=self._evaluate_condition,
                    ai_available=use_llm, vector_available=self.vector_memory is not None,
                )
                for me in fired_meta:
                    MetaEventBus.mark_fired(self.world_state.event_state, me.id, self.world_state.current_turn)
                if fired_meta:
                    print(f'[元事件] {len(fired_meta)} 个触发')
            except Exception as e:
                print(f'[WARN] MetaEventBus failed: {e}')

        # VectorMemory 存储
        if self.vector_memory:
            try:
                scene_text = scene.get('scene_text', '')
                if scene_text and len(scene_text) >= 20:
                    vm_node_id = f'turn_{self.world_state.current_turn}'
                    self.vector_memory.add(
                        node_id=vm_node_id,
                        text=f'{player_input}\n{scene_text}',
                        metadata={'turn_number': self.world_state.current_turn,
                                  'game_time': self.world_state.current_date.isoformat()},
                    )
            except Exception as e:
                print(f'[WARN] VectorMemory.add failed: {e}')

        # 世界扩展
        if use_llm and outline.get("needs_expansion"):
            try:
                expansions = await ScriptBuilder.expand_world(
                    scene.get('scene_text', ''), outline, self.script_data
                )
                new_npcs = expansions.get('new_npcs', [])
                new_locs = expansions.get('new_locations', [])
                if new_npcs or new_locs:
                    await self._apply_world_expansion(new_npcs, new_locs)
                    print(f'[扩展] +{len(new_npcs)} NPC, +{len(new_locs)} 地点')
            except Exception as e:
                print(f'[WARN] WorldExpander failed: {e}')

    async def _apply_world_expansion(self, new_npcs: list, new_locations: list):
        """添加新的 NPC/地点，并为它们生成 visual_profile"""
        for npc in new_npcs:
            npc_id = npc.get("id", f"npc_{len(self.script_data.get('npcs', []))}")
            npc["id"] = npc_id
            # Generate visual profile for new NPC
            if not npc.get("visual_profile") and _llm_state.get("client"):
                try:
                    sys_prompt = "你是视觉设计助手，基于角色描述生成像素艺术风格的颜色和特征。只返回 JSON。"
                    user_prompt = f"""角色: {npc.get('name', '?')}
职位: {npc.get('title', '?')}
性格: {npc.get('personality', '?')}
能力: {npc.get('capabilities', '?')}

生成一个 visual_profile JSON，包含:
- shirt_color: 衣服颜色 (hex)
- hair_color: 头发颜色 (hex)
- hair_style: 发型 (short/long/bun/slick/buzz)
- pant_color: 裤子颜色 (hex)
- skin_color: 肤色 (hex)

只返回 JSON，不要其他文本。"""
                    response = await llm_call(sys_prompt, user_prompt, max_tokens=300, raise_on_error=False)
                    profile = _parse_json_from_llm(response) or {}
                    npc["visual_profile"] = profile
                except Exception:
                    npc["visual_profile"] = {}

            self.script_data.setdefault("npcs", []).append(npc)
            self.npc_agents[npc_id] = NPCAgent(npc_id, npc, self.world_state)
            self.world_state.relationships[npc_id] = npc.get("attitude_toward_player", 50)

        for loc in new_locations:
            loc_id = loc.get("id", f"loc_{len(self.script_data.get('locations', []))}")
            loc["id"] = loc_id
            # Generate visual profile for new location
            if not loc.get("visual_profile") and _llm_state.get("client"):
                try:
                    sys_prompt = "你是视觉设计助手，基于地点描述生成等轴测立方体像素艺术的颜色和形状。只返回 JSON。"
                    user_prompt = f"""地点: {loc.get('name', '?')}
描述: {loc.get('description', '?')}

生成一个 visual_profile JSON，包含:
- base_color: 基础颜色 (hex)
- dark_color: 暗色 (hex)
- light_color: 亮色 (hex)
- accent_color: 强调色 (hex)
- shape: 形状 (cube/tall/wide/pyramid/dome/multi)

只返回 JSON，不要其他文本。"""
                    response = await llm_call(sys_prompt, user_prompt, max_tokens=300, raise_on_error=False)
                    profile = _parse_json_from_llm(response) or {}
                    loc["visual_profile"] = profile
                except Exception:
                    loc["visual_profile"] = {}

            self.script_data.setdefault("locations", []).append(loc)

        if new_npcs or new_locations:
            sid = self.script_data.get("script_id", "unknown")
            path = os.path.join(SCRIPTS_DIR, f"{sid}.json")
            if os.path.exists(path):
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(self.script_data, f, ensure_ascii=False, indent=2)
                except Exception:
                    pass

    def _apply_event_result(self, result) -> None:
        """应用 EventResult 到世界状态"""
        if not result:
            return
        for effect in result.effects:
            action = effect.get("action", "")
            params = effect.get("params", {})
            if action == "set_var":
                target = params.get("target", "")
                op = params.get("op", "set")
                value = params.get("value")
                if target and value is not None:
                    if self.state_manager:
                        self.state_manager.apply_changes(
                            self.world_state.event_state,
                            [{"target": target, "op": op, "value": value}], inplace=True)
                    else:
                        self.world_state.apply_changes([{"var": target, "op": op, "value": value}])
        if self.lorebook:
            for eid in result.lore_activations:
                self.lorebook.update_entry_enabled(eid, True)
            for eid in result.lore_deactivations:
                self.lorebook.update_entry_enabled(eid, False)
            if result.lore_additions:
                self.lorebook.add_entries(result.lore_additions)
            for upd in result.lore_updates:
                self.lorebook.update_entry(upd.get("id", ""), upd.get("content", ""), upd.get("keys"))
            for rid in result.lore_removals:
                self.lorebook.remove_entry(rid)
        active_states = self.world_state.event_state.setdefault("active_persistent_states", [])
        for sid in result.state_activations:
            if sid not in active_states:
                active_states.append(sid)
        for sid in result.state_deactivations:
            if sid in active_states:
                active_states.remove(sid)

    @staticmethod
    def _summarize_event_result(result) -> Dict[str, Any]:
        """将 EventResult 转为可序列化的摘要"""
        if not result:
            return {}
        return {
            "newly_active": [e.get("name", e.get("id", "")) for e in result.newly_active],
            "newly_completed": [e.get("name", e.get("id", "")) for e in result.newly_completed],
            "notifications": result.notifications,
            "inject_prompts": result.inject_prompts,
            "triggered_consequences": [c.get("description", "") for c in result.triggered_consequences],
            "imminent_warnings": result.imminent_warnings,
        }


    def _evaluate_condition(self, condition: str) -> bool:
        """条件表达式求值（供 EventEngine/TriggerEngine 使用）"""
        if not condition:
            return True
        import re as _re
        import operator as _op
        _OPS = {
            ">=": _op.ge, "<=": _op.le, ">": _op.gt, "<": _op.lt,
            "==": _op.eq, "!=": _op.ne,
        }
        m = _re.match(r"([\w.]+)\s*(>=|<=|>|<|==|!=)\s*(.+)", condition.strip())
        if not m:
            return False
        path, op_str, raw_val = m.group(1), m.group(2), m.group(3).strip()
        if self.state_manager:
            actual = self.state_manager._get_value(self.world_state.event_state, path)
        else:
            actual = self.world_state.query_info("variable", path)
        if actual is None:
            return False
        try:
            target = int(raw_val) if raw_val.lstrip("-").isdigit() else raw_val
            if isinstance(actual, (int, float)) and isinstance(target, str):
                target = float(target)
            return _OPS.get(op_str, lambda a, b: False)(actual, target)
        except (ValueError, TypeError):
            return False

    def _build_xml_section(self, tag: str, content: str) -> str:
        """构建 XML 结构化提示段（采用酒馆 PromptBuilder 原则）"""
        if not content or not content.strip():
            return ""
        return f"<{tag}>\n{content}\n</{tag}>"

    def _build_structured_context_prompt(self, context: Dict[str, Any]) -> str:
        """将 context 组装为结构化 XML 提示词（PromptBuilder 风格）"""
        sections = []
        # world section
        world_info = []
        world_info.append(f"轮次: {self.world_state.current_turn}")
        world_info.append(f"日期: {self.world_state.current_date.isoformat()}")
        loc_id = self.world_state.current_location
        loc_name = loc_id
        for loc in self.script_data.get("locations", []):
            if loc.get("id") == loc_id:
                loc_name = loc.get("name", loc_id)
                break
        world_info.append(f"地点: {loc_name}")
        sections.append(self._build_xml_section("world", "\n".join(world_info)))
        # lorebook section
        lore = context.get("shared", {}).get("lorebook", "")
        if lore:
            sections.append(self._build_xml_section("lorebook", lore))
        # events section
        event_inject = context.get("shared", {}).get("event_inject", "")
        if event_inject:
            sections.append(self._build_xml_section("events", event_inject))
        # history summary section
        summary = self.world_state.summary_state.get("history_summary", "")
        if summary:
            sections.append(self._build_xml_section("story_context", summary))
        return "\n\n".join(s for s in sections if s)

    def vector_query(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """语义检索历史（VectorMemory 可用时）"""
        if not self.vector_memory:
            return []
        try:
            return self.vector_memory.query(query, top_k=top_k)
        except Exception:
            return []

    def _build_context(self, player_input: str, dp: dict,
                       event_result, lore_text: str, event_inject: str) -> Dict[str, Any]:
        """阶段1：一次性组装分层 context dict，替代 ContextGatherer + process_turn 注入块"""
        resolved = dp.get("resolved_action", player_input)

        # 1. 世界层
        world = {
            "turn": self.world_state.current_turn,
            "date": self.world_state.current_date.isoformat(),
            "location": self.world_state.current_location,
            "environment": {k: v for k, v in self.world_state.world_props.items()
                            if k in ("weather", "lighting", "atmosphere", "season",
                                     "time_of_day", "天气", "光照", "氛围", "季节")},
        }

        # 2. 历史层
        recent_turns = self.world_state.turns[-5:]
        relationship_trends = {}
        if len(self.world_state.turns) >= 2:
            prev_s = self.world_state.turns[-2].state_after
            curr_s = self.world_state.turns[-1].state_after
            for nid in prev_s.get("relationships", {}):
                pv = prev_s["relationships"].get(nid, 0)
                cv = curr_s["relationships"].get(nid, 0)
                if pv != cv:
                    relationship_trends[nid] = f"{pv} → {cv}"
        history = {
            "recent_actions": [t.player_action for t in recent_turns],
            "relationship_trends": relationship_trends,
        }

        # 3. NPC 独立上下文
        agendas = self._check_npc_agendas()
        agenda_map = {a["npc_id"]: a["action"] for a in agendas}
        npc_contexts = {}
        for npc_id in self.npc_agents:
            nc = {}
            if self.npc_memories.get(npc_id):
                nc["memories"] = self.npc_memories[npc_id]
            if agenda_map.get(npc_id):
                nc["agenda"] = agenda_map[npc_id]
            if self.npc_plans.get(npc_id):
                nc["plan"] = self.npc_plans[npc_id]
            arc = self.npc_emotion_arcs.get(npc_id, [])
            if len(arc) >= 3:
                nc["emotion_arc"] = arc[-5:]
            rel = self._build_npc_relationship_context(npc_id)
            if rel:
                nc["relationships"] = rel
            per_npc_know = [info["fact"][:120] for info in self.information_network
                            if npc_id in info.get("known_by", [])][:5]
            if per_npc_know:
                nc["knowledge"] = per_npc_know
            if nc:
                npc_contexts[npc_id] = nc

        if agendas:
            print(f'[日程] {len(agendas)} 个NPC有议程: {", ".join(a["npc_name"] for a in agendas)}')

        # 4. 共享层
        shared = {}
        if lore_text:
            shared["lorebook"] = lore_text
        if event_inject:
            shared["event_inject"] = event_inject
        if self.faction_reputation:
            shared["faction_reputation"] = self.faction_reputation
        esummary = self._summarize_event_result(event_result) if event_result else {}
        if esummary:
            shared["event_result_summary"] = esummary

        # 5. 语义检索
        semantic = []
        if self.vector_memory:
            try:
                semantic = self.vector_memory.query(player_input, 3)
            except Exception:
                pass

        return {
            "user_action": resolved,
            "raw_input": player_input,
            "director": dp,
            "world": world,
            "history": history,
            "npc_contexts": npc_contexts,
            "shared": shared,
            "semantic_history": semantic,
        }

    def _run_npc_agents_parallel(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """阶段2：NPC并行推理（规则模式）"""
        npc_outputs = {}
        for npc_id, agent in self.npc_agents.items():
            try:
                npc_outputs[npc_id] = agent._reason_rules(context)
            except Exception as e:
                print(f"  警告：{npc_id} 推理失败: {e}")
        return npc_outputs

    async def _run_npc_agents_llm(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """阶段2：NPC 两轮推理（含 Director 自适应路由策略）"""
        skip = set(context.get("director", {}).get("skip_npcs") or [])
        strategies = context.get("director", {}).get("npc_strategies", {})

        # 合并 skip_npcs 和 minimal 策略
        for npc_id, strat in strategies.items():
            if strat == "minimal":
                skip.add(npc_id)

        supportive_ids = {npc_id for npc_id, strat in strategies.items() if strat == "supportive"}

        active_agents = {npc_id: agent for npc_id, agent in self.npc_agents.items() if npc_id not in skip}
        npc_outputs = {}
        for npc_id in skip:
            agent = self.npc_agents.get(npc_id)
            if agent:
                npc_outputs[npc_id] = {
                    "name": agent.npc_data.get("name", npc_id),
                    "action_type": "无关",
                    "emotion": "平静",
                    "dialogue": None,
                    "skipped": True,
                }

        if not active_agents:
            return npc_outputs

        # --- 第一轮：并行收集意图 ---
        async def _get_intent(npc_id, agent):
            try:
                return await agent._reason_intent(context)
            except Exception as e:
                print(f"  警告：{npc_id} 意图推理失败: {e}")
                return {"npc_id": npc_id, "name": agent.name, "action_type": "观望", "stance": ""}

        intent_tasks = [_get_intent(npc_id, agent) for npc_id, agent in active_agents.items()]
        intent_results = await asyncio.gather(*intent_tasks)
        intent_context = list(intent_results)

        active_intents = [i for i in intent_context if i["action_type"] != "观望"]
        if active_intents:
            names = ", ".join(i["name"] for i in active_intents)
            print(f'  [意图] {len(active_intents)} 个NPC有行动意图: {names}')

        # --- 动态策略提升：supportive NPC 显示强烈情绪时升级为 aggressive ---
        _ESCALATE_TYPES = {"冲突", "警告", "主动联系", "主动谈判"}
        promoted = set()
        for intent in intent_context:
            npc_id = intent.get("npc_id", "")
            if npc_id in supportive_ids and intent.get("action_type") in _ESCALATE_TYPES:
                promoted.add(npc_id)
        if promoted:
            supportive_ids -= promoted
            print(f'  [策略提升] {len(promoted)} 个NPC从supportive升级为aggressive: {", ".join(promoted)}')

        # --- 第二轮：aggressive 走完整推理，supportive 用意图结果直接构建精简输出 ---
        aggressive_agents = {npc_id: agent for npc_id, agent in active_agents.items() if npc_id not in supportive_ids}
        supportive_agents = {npc_id: agent for npc_id, agent in active_agents.items() if npc_id in supportive_ids}

        # supportive NPC 直接用第一轮意图结果构建输出（跳过第二轮 LLM）
        for npc_id, agent in supportive_agents.items():
            intent = next((i for i in intent_context if i.get("npc_id") == npc_id), None)
            npc_outputs[npc_id] = {
                "agent_id": npc_id,
                "name": agent.name,
                "action_type": intent.get("action_type", "观望") if intent else "观望",
                "emotion": "平静",
                "dialogue": None,
                "thought": intent.get("stance", "") if intent else "",
                "tool_calls": [],
                "strategy": "supportive",
            }

        if supportive_agents:
            print(f'  [路由] {len(supportive_agents)} 个NPC使用精简推理: {", ".join(a.name for a in supportive_agents.values())}')

        # aggressive NPC 完整第二轮推理（aggressive 拿 extended 工具集）
        async def _reason_full(npc_id, agent):
            try:
                return npc_id, await agent._reason_llm(context, session=self,
                                                       intent_context=intent_context, strategy="aggressive")
            except Exception as e:
                print(f"  警告：{npc_id} LLM推理失败: {e}")
                return npc_id, agent._reason_rules(context)

        if aggressive_agents:
            full_tasks = [_reason_full(npc_id, agent) for npc_id, agent in aggressive_agents.items()]
            full_results = await asyncio.gather(*full_tasks)
            for npc_id, output in full_results:
                npc_outputs[npc_id] = output
        return npc_outputs

    # ===== Pacing 系统（从原始酒馆提取） =====

    def _compute_tension(self, action: str, npc_outputs: Dict[str, Any], outline: Dict[str, Any]) -> int:
        """计算当前 tension（0-100），指导叙事节奏"""
        tension = self.tension

        conflicts = outline.get("conflicts", [])
        for c in conflicts:
            sev = c.get("severity", "low")
            tension += {"high": 20, "medium": 10, "low": 5}.get(sev, 5)

        active_actions = sum(1 for o in npc_outputs.values() if o.get("action_type") not in ("观望", "无关"))
        tension += active_actions * 3

        if any(kw in action for kw in ("攻击", "战斗", "逃跑", "危险", "追赶", "偷")):
            tension += 15
        elif any(kw in action for kw in ("休息", "睡觉", "闲聊", "散步")):
            tension = max(0, tension - 15)

        # 事件驱动 tension: 活跃事件的 tension_modifier
        event_summary = outline.get("event_result_summary") or {}
        if event_summary.get("newly_active"):
            tension += 10
        if event_summary.get("triggered_consequences"):
            tension += 5 * len(event_summary["triggered_consequences"])

        # OutlineAgent 手动调节
        tension += self._pending_tension_adjustment
        self._pending_tension_adjustment = 0

        tension = max(0, min(100, tension))

        # Tension lock: 有活跃事件线时不衰减
        has_active_events = bool(
            self.world_state.event_state.get("active_persistent_states")
            or event_summary.get("newly_active")
        )
        if not has_active_events:
            # 余韵期：冲突结束后衰减更慢
            if tension < self._tension_peak - 20 and self._tension_peak >= 50:
                self._tension_afterglow = 3
                self._tension_peak = 0
            if self._tension_afterglow > 0:
                tension_decay = max(1, int(tension * 0.05))
                self._tension_afterglow -= 1
            else:
                tension_decay = max(1, int(tension * 0.1))
            self.tension = max(0, tension - tension_decay)
        else:
            self.tension = tension
        self._tension_peak = max(self._tension_peak, tension)
        return tension

    def _pacing_hint(self, tension: int) -> str:
        """生成 pacing 提示，注入到场景描写中"""
        if tension >= 80:
            return "节奏：高度紧张。叙事应短促有力，使用快节奏描写，增加紧迫感和悬念。"
        elif tension >= 50:
            return "节奏：中度紧张。叙事应在紧张和舒缓之间平衡，适当铺垫但不拖沓。"
        elif tension >= 20:
            return "节奏：舒缓。叙事可以更加细腻，关注角色情感和环境描写。"
        else:
            return "节奏：宁静。叙事应轻松自然，可以展现日常场景和角色的日常面。"

    def _resolve_skill_check(self, attr_name: str, difficulty: str = "medium") -> Dict[str, Any]:
        """执行技能检定（复用原始酒馆的简化版）"""
        import random
        dc = {"easy": 8, "medium": 12, "hard": 16, "extreme": 20}.get(difficulty, 12)
        player = self.script_data.get("player_character", {})
        attrs = player.get("attributes", {})
        attr_val = 50
        matched_name = attr_name
        for k, v in attrs.items():
            if attr_name in k or k in attr_name:
                attr_val = v if isinstance(v, (int, float)) else (v.get("value", 50) if isinstance(v, dict) else 50)
                matched_name = k
                break
        bonus = (attr_val - 10) // 2 if isinstance(attr_val, (int, float)) else 0
        roll = random.randint(1, 20)
        total = roll + bonus
        success = total >= dc
        outcome = "critical_success" if roll == 20 else "critical_failure" if roll == 1 else "success" if success else "failure"
        return {
            "attribute": matched_name,
            "difficulty": difficulty,
            "dc": dc,
            "roll": roll,
            "bonus": bonus,
            "total": total,
            "outcome": outcome,
            "description": f"「{matched_name}」检定: d20({roll})+{bonus}={total} vs DC{dc} → {'成功' if success else '失败'}",
        }

    _HOSTILE_ACTIONS = {"欺骗", "偷窃", "暗算", "背叛", "陷阱", "威胁", "施法", "攻击"}
    _PASSIVE_ATTR_MAP = {
        "欺骗": "洞察", "偷窃": "感知", "暗算": "感知", "背叛": "洞察",
        "陷阱": "感知", "威胁": "意志", "施法": "感知", "攻击": "敏捷",
    }

    def _check_passive_skills(self, npc_outputs: Dict[str, Any]) -> list:
        """NPC 对抗性行动时自动触发玩家被动检定"""
        checks = []
        for npc_id, output in npc_outputs.items():
            action_type = output.get("action_type", "")
            if action_type not in self._HOSTILE_ACTIONS:
                continue
            attr = self._PASSIVE_ATTR_MAP.get(action_type, "感知")
            result = self._resolve_skill_check(attr, "medium")
            checks.append({
                "trigger_npc": output.get("name", npc_id),
                "action_type": action_type,
                "attribute": attr,
                "result": result,
            })
        return checks

    async def _npc_dialogue_round(self, npc_a_id: str, npc_b_id: str, topic: str, context: Dict[str, Any]) -> Optional[Dict]:
        """高冲突场景下两个NPC之间的一轮对话"""
        agent_a = self.npc_agents.get(npc_a_id)
        agent_b = self.npc_agents.get(npc_b_id)
        if not agent_a or not agent_b:
            return None
        name_a = agent_a.npc_data.get("name", npc_a_id)
        name_b = agent_b.npc_data.get("name", npc_b_id)
        system_prompt = f"你在模拟两个NPC之间的紧张对话。话题：{topic}。输出格式：\n{name_a}: (对白)\n{name_b}: (对白)\n用2-3轮对话表现冲突。保持简洁，总计不超过150字。"
        user_prompt = f"{name_a}（{agent_a.npc_data.get('personality', '')}）和{name_b}（{agent_b.npc_data.get('personality', '')}）就「{topic}」发生了争执。"
        try:
            dialogue_text = await llm_call(system_prompt, user_prompt, max_tokens=600)
            if dialogue_text:
                return {"npc_a": npc_a_id, "npc_b": npc_b_id, "npc_a_name": name_a, "npc_b_name": name_b, "topic": topic, "dialogue": dialogue_text}
        except Exception:
            pass
        return None

    def _check_npc_agendas(self) -> list:
        """检查 NPC 日程，返回本轮触发的议程项。
        支持 trigger 类型：turn>=N, hour>=N, always, every_N_turns, 条件表达式。
        """
        triggered = []
        turn = self.world_state.current_turn
        hour = self.world_state.current_date.hour

        for npc_def in self.script_data.get("npcs", []):
            npc_id = npc_def.get("id", "")
            for agenda in npc_def.get("agenda", []):
                trigger = agenda.get("trigger", "")
                condition = agenda.get("condition", "")
                fired = False
                if trigger.startswith("turn"):
                    try:
                        op = ">=" if ">=" in trigger else "==" if "==" in trigger else ">"
                        val = int(trigger.split(op)[-1].strip())
                        fired = (op == ">=" and turn >= val) or (op == "==" and turn == val) or (op == ">" and turn > val)
                    except (ValueError, IndexError):
                        pass
                elif trigger.startswith("hour"):
                    try:
                        op = ">=" if ">=" in trigger else "==" if "==" in trigger else ">"
                        val = int(trigger.split(op)[-1].strip())
                        fired = (op == ">=" and hour >= val) or (op == "==" and hour == val) or (op == ">" and hour > val)
                    except (ValueError, IndexError):
                        pass
                elif trigger.startswith("every_") and trigger.endswith("_turns"):
                    try:
                        interval = int(trigger[6:-6])
                        fired = interval > 0 and turn % interval == 0
                    except (ValueError, IndexError):
                        pass
                elif trigger == "always":
                    fired = True
                else:
                    fired = self._evaluate_condition(trigger)

                if fired and condition:
                    fired = self._evaluate_condition(condition)

                if fired and not agenda.get("_done"):
                    triggered.append({
                        "npc_id": npc_id,
                        "npc_name": npc_def.get("name", npc_id),
                        "action": agenda.get("action", ""),
                        "priority": agenda.get("priority", "low"),
                    })
                    if not agenda.get("repeatable"):
                        agenda["_done"] = True
        return triggered

    # ===== NPC 间关系网络（复用酒馆 _apply_npc_relationship_updates） =====

    @staticmethod
    def _normalize_rel_key(a: str, b: str) -> str:
        return f"{min(a, b)}_{max(a, b)}"

    def _apply_npc_relationship_updates(self, updates: list):
        for upd in updates:
            a = upd.get("from_npc") or upd.get("a", "")
            b = upd.get("to_npc") or upd.get("b", "")
            if not a or not b:
                continue
            key = self._normalize_rel_key(a, b)
            existing = self.npc_relationships_global.get(key)
            entry = {
                "a": a, "b": b,
                "type": upd.get("rel_type", existing.get("type", "中立") if existing else "中立"),
                "description": upd.get("description", existing.get("description", "") if existing else ""),
                "_turn": self.world_state.current_turn,
            }
            self.npc_relationships_global[key] = entry
            self.npc_relationships_known[key] = entry
        # 淘汰：超过上限时删除最旧条目
        for net in (self.npc_relationships_global, self.npc_relationships_known):
            if len(net) <= self._REL_NET_MAX:
                continue
            items = sorted(net.items(), key=lambda x: x[1].get("_turn", 0))
            to_remove = len(net) - self._REL_NET_MAX
            for k, _ in items[:to_remove]:
                net.pop(k, None)

    def _build_npc_relationship_context(self, npc_id: str) -> str:
        """为指定 NPC 构建其与其他 NPC 的关系上下文"""
        lines = []
        dn = {}
        for n in self.script_data.get("npcs", []):
            dn[n.get("id", "")] = n.get("name", n.get("id", ""))
        for _key, rel in self.npc_relationships_known.items():
            a, b = rel.get("a", ""), rel.get("b", "")
            if npc_id not in (a, b):
                continue
            other = b if a == npc_id else a
            other_name = dn.get(other, other)
            desc = rel.get("description", "")
            lines.append(f"- {other_name}：{rel.get('type', '中立')}" + (f"（{desc[:30]}）" if desc else ""))
        if not lines:
            return ""
        return "## 你与其他角色的关系\n\n" + "\n".join(lines[:6])

    # ===== 信息不对称系统（复用酒馆 information_network） =====

    def _propagate_information(self):
        network = self.information_network
        if not network:
            return
        import random
        for info in network:
            known = set(info.get("known_by", []))
            if len(known) >= info.get("max_spread", 5):
                continue
            new_knowers = set()
            for knower in list(known):
                for _rk, rel in self.npc_relationships_global.items():
                    a, b = rel.get("a", ""), rel.get("b", "")
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

    def _build_npc_knowledge_context(self, present_npc_ids: list) -> str:
        network = self.information_network
        if not network or not present_npc_ids:
            return ""
        _DISTORTION_LABELS = {0: "", 1: "（略有偏差）", 2: "（严重失真）", 3: "（面目全非）"}
        present_set = set(present_npc_ids)
        npc_facts: dict = {}
        for info in network:
            known_by = set(info.get("known_by", []))
            overlapping = present_set & known_by
            if not overlapping:
                continue
            fact = info.get("fact", "")
            if not fact:
                continue
            distortion = min(info.get("distortion", 0), 3)
            label = _DISTORTION_LABELS.get(distortion, "")
            for npc_id in overlapping:
                npc_facts.setdefault(npc_id, []).append(f"{fact[:60]}{label}")
        if not npc_facts:
            return ""
        dn = {n.get("id", ""): n.get("name", n.get("id", "")) for n in self.script_data.get("npcs", [])}
        lines = ["## NPC 认知差异（各NPC只知道各自的信息，对话/反应须反映认知差异）"]
        for npc_id, facts in npc_facts.items():
            name = dn.get(npc_id, npc_id)
            lines.append(f"- {name}知道: {'; '.join(facts[:3])}")
        return "\n".join(lines)

    # ===== 声望系统（复用酒馆 faction_reputation） =====

    @staticmethod
    def _reputation_title(value: int) -> str:
        if value >= 90: return "崇拜"
        if value >= 70: return "友好"
        if value >= 50: return "中立"
        if value >= 30: return "冷淡"
        if value >= 10: return "敌对"
        return "通缉"

    def _check_npc_interventions(self) -> list:
        """检查 NPC 是否主动干预（简化版场景劫持）"""
        interventions = []
        cooldowns = self.npc_intervention_cooldowns
        player_loc = self.world_state.current_location
        turn = self.world_state.current_turn

        for npc_def in self.script_data.get("npcs", []):
            if len(interventions) >= 2:
                break
            npc_id = npc_def.get("id", "")
            if not npc_id or turn < cooldowns.get(npc_id, 0):
                continue

            npc_loc = npc_def.get("default_location", "")
            is_present = npc_loc == player_loc
            if not is_present:
                connections = []
                for loc in self.script_data.get("locations", []):
                    if loc.get("id") == player_loc:
                        connections = loc.get("connections", [])
                        break
                is_nearby = npc_loc in connections
            else:
                is_nearby = False

            if not is_present and not is_nearby:
                continue

            attitude = self.world_state.relationships.get(npc_id, 50)
            capabilities = npc_def.get("capabilities", "")

            if attitude < 25 and any(k in capabilities for k in ("战斗", "攻击", "武力", "守卫", "combat")):
                interventions.append({
                    "npc_id": npc_id,
                    "npc_name": npc_def.get("name", npc_id),
                    "type": "confrontation",
                    "urgency": "high",
                    "reason": f"对你怀有敌意（好感{attitude}）",
                    "suggested_action": f"{npc_def.get('name', npc_id)}拦住你的去路",
                })
                cooldowns[npc_id] = turn + 5
                continue

            if attitude > 80 and is_present:
                interventions.append({
                    "npc_id": npc_id,
                    "npc_name": npc_def.get("name", npc_id),
                    "type": "aid",
                    "urgency": "medium",
                    "reason": "关系亲密，主动提供帮助",
                    "suggested_action": f"{npc_def.get('name', npc_id)}主动上前和你打招呼",
                })
                cooldowns[npc_id] = turn + 5
                continue

        return interventions

    @staticmethod
    def _detect_plan_declaration(action_text: str) -> bool:
        _PLAN_KEYWORDS = ("计划", "打算", "准备", "预谋", "我的计划是", "策划", "筹划")
        return any(kw in action_text for kw in _PLAN_KEYWORDS)

    # ===== NPC 独立对话流程（从原始酒馆提取） =====

    async def talk_to_npc(self, npc_id: str, message: str) -> Dict[str, Any]:
        """与 NPC 进行独立对话（不推进主线），保持对话历史连续性"""
        npc_data = next((n for n in self.script_data.get("npcs", []) if n.get("id") == npc_id), None)
        if not npc_data:
            return {"error": f"NPC not found: {npc_id}"}

        npc_name = npc_data.get("name", npc_id)
        rel_value = self.world_state.relationships.get(npc_id, 50)

        history = self.npc_chat_history.get(npc_id, [])

        system_prompt = f"""你现在扮演 {npc_name}（{npc_data.get('title', '')}），与玩家进行自然对话。

## 角色信息
- 性格：{npc_data.get('personality', '')}
- 背景：{npc_data.get('bio', '')}
- 与玩家关系值：{rel_value}/100
- 当前游戏时间：{self.world_state.current_date.isoformat()}
- 当前地点：{self.world_state.current_location}

## 对话规则
1. 严格以 {npc_name} 的口吻和性格回应
2. 对话长度 50-150 字
3. 可以自然推进关系，但单轮关系变化不超过 ±5
4. 如果包含关系变化，在回复末尾添加 ```npc_talk {{"npc_attitude_changes": [{{"npc_id": "{npc_id}", "dimension": "value", "change": 数值, "reason": "原因"}}]}} ```
5. 使用中文弯引号包裹对话"""

        messages = []
        if history and isinstance(history[0], dict) and history[0].get("_summary"):
            summary_text = history[0]["_summary"]
            messages.append({"role": "user", "content": f"[之前对话摘要: {summary_text}]"})
            messages.append({"role": "assistant", "content": "好的，我了解之前的对话内容。"})
        for h in history[-6:]:
            if isinstance(h, dict) and h.get("_summary"):
                continue
            messages.append({"role": "user", "content": h.get("player", "")})
            messages.append({"role": "assistant", "content": h.get("npc", "")})
        messages.append({"role": "user", "content": message})

        raw = await llm_call(system_prompt, message if not messages[:-1] else "")
        if messages[:-1]:
            client = _llm_state.get("client")
            model = _llm_state.get("model", "")
            if client:
                try:
                    all_msgs = [{"role": "system", "content": system_prompt}] + messages
                    resp = await client.chat.completions.create(
                        model=model, messages=all_msgs, max_tokens=1000, temperature=0.85
                    )
                    raw = (resp.choices[0].message.content or "").strip()
                    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                except Exception:
                    pass

        att_changes = []
        match = re.search(r'```npc_talk\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(1))
                att_changes = parsed.get("npc_attitude_changes", [])
            except (json.JSONDecodeError, KeyError):
                pass

        clean_response = re.sub(r'```npc_talk\s*\{.*?\}\s*```', '', raw, flags=re.DOTALL).strip()

        rel_before = self.world_state.relationships.get(npc_id, 50)
        for ac in att_changes:
            delta = ac.get("change", 0)
            delta = max(-5, min(5, delta))
            target_npc = ac.get("npc_id", npc_id)
            self.world_state.relationships[target_npc] = self.world_state.relationships.get(target_npc, 50) + delta
        rel_after = self.world_state.relationships.get(npc_id, 50)

        crossings = []
        for th in (20, 40, 60, 80):
            if rel_before < th <= rel_after:
                crossings.append({"threshold": th, "direction": "up", "from": rel_before, "to": rel_after})
            elif rel_before >= th > rel_after:
                crossings.append({"threshold": th, "direction": "down", "from": rel_before, "to": rel_after})

        history.append({"player": message, "npc": clean_response})
        if len(history) > 20:
            overflow = history[:-6]
            kept = history[-6:]
            summary_parts = [f"玩家:{h.get('player', '')[:30]}→NPC:{h.get('npc', '')[:50]}" for h in overflow[-5:] if isinstance(h, dict) and not h.get("_summary")]
            existing_summary = history[0].get("_summary", "") if history and isinstance(history[0], dict) and history[0].get("_summary") else ""
            new_summary = existing_summary + "; ".join(summary_parts)
            if len(new_summary) > 500:
                new_summary = new_summary[-500:]
            history = [{"_summary": new_summary}] + kept
        self.npc_chat_history[npc_id] = history

        if self.world_tree:
            try:
                self.world_tree.add_node(
                    parent_id=self.world_tree.active_node_id,
                    game_time=self.world_state.current_date.isoformat(),
                    turn_number=self.world_state.current_turn,
                    player_action={"type": "npc_talk", "npc_id": npc_id, "text": message},
                    ai_response=clean_response,
                    state_snapshot=self.world_state.get_snapshot(),
                )
            except Exception:
                pass

        self.world_state.current_date += timedelta(minutes=5)

        return {
            "npc_id": npc_id,
            "npc_name": npc_name,
            "response": clean_response,
            "attitude_changes": att_changes,
            "relationship": self.world_state.relationships.get(npc_id, 50),
            "dialogue_count": len([h for h in history if isinstance(h, dict) and not h.get("_summary")]),
            "relationship_crossings": crossings,
        }

    def get_history_summary(self, num_turns: int = 5) -> str:
        """获取历史摘要"""
        turns = self.world_state.turns[-num_turns:]
        summary = f"最近{len(turns)}轮推演:\n"
        for turn in turns:
            summary += f"  T{turn.turn_num}: {turn.player_action}\n"
        return summary

    def save_checkpoint(self, name: str = None) -> int:
        """保存检查点到数据库，返回存档 id"""
        if not name:
            name = f"轮次{self.world_state.current_turn} 自动存档"

        checkpoint = {
            "world_state": self.world_state.get_snapshot(),
            "turns": [asdict(turn) for turn in self.world_state.turns],
            "npc_memories": self.npc_memories,
            "npc_relationships_global": self.npc_relationships_global,
            "npc_relationships_known": self.npc_relationships_known,
            "information_network": self.information_network,
            "faction_reputation": self.faction_reputation,
            "event_state": self.world_state.event_state,
            "lorebook_timed_state": self.world_state.lorebook_timed_state,
            "summary_state": getattr(self.world_state, 'summary_state', {}),
            "npc_chat_history": getattr(self, 'npc_chat_history', {}),
            "npc_plans": getattr(self, 'npc_plans', {}),
            "npc_emotion_arcs": getattr(self, 'npc_emotion_arcs', {}),
        }

        script_id = self.script_data.get("script_id", "unknown")
        turn = self.world_state.current_turn
        game_time = self.world_state.current_date.isoformat()
        data_json = json.dumps(checkpoint, ensure_ascii=False)

        conn = sqlite3.connect(TAVERN_DB)
        cur = conn.execute(
            "INSERT INTO rpg_saves (script_id, name, turn, game_time, data) VALUES (?, ?, ?, ?, ?)",
            (script_id, name, turn, game_time, data_json),
        )
        save_id = cur.lastrowid
        conn.commit()
        conn.close()

        print(f"[OK] 存档已保存: #{save_id} ({name})")
        return save_id

    def load_checkpoint(self, save_id: int) -> bool:
        """从数据库加载检查点"""
        conn = sqlite3.connect(TAVERN_DB)
        row = conn.execute("SELECT data FROM rpg_saves WHERE id = ?", (save_id,)).fetchone()
        conn.close()
        if not row:
            print(f"[FAIL] 存档不存在: #{save_id}")
            return False

        checkpoint = json.loads(row[0])
        ws = checkpoint.get("world_state", {})
        self.world_state.current_turn = ws.get("turn", 0)
        self.world_state.current_location = ws.get("location_id", ws.get("location", ""))
        if ws.get("date"):
            try:
                self.world_state.current_date = datetime.fromisoformat(ws["date"])
            except Exception:
                pass
        self.world_state.player_attrs = ws.get("player_attrs", {})
        self.world_state.relationships = ws.get("relationships", {})
        self.world_state.variables = ws.get("variables", {})
        self.world_state.world_props = ws.get("world_props", {})
        self.npc_memories = checkpoint.get("npc_memories", {})
        self.npc_relationships_global = checkpoint.get("npc_relationships_global", {})
        self.npc_relationships_known = checkpoint.get("npc_relationships_known", {})
        self.information_network = checkpoint.get("information_network", [])
        self.faction_reputation = checkpoint.get("faction_reputation", {})

        # 恢复 turns 列表
        turns_data = checkpoint.get("turns", [])
        self.world_state.turns = []
        for td in turns_data:
            try:
                self.world_state.turns.append(Turn(**td))
            except Exception:
                pass

        # 恢复补充字段
        self.world_state.event_state = ws.get("event_state", checkpoint.get("event_state", {}))
        self.world_state.lorebook_timed_state = checkpoint.get("lorebook_timed_state", {})
        if hasattr(self.world_state, 'summary_state'):
            self.world_state.summary_state = checkpoint.get("summary_state", {})
        self.npc_chat_history = checkpoint.get("npc_chat_history", {})
        self.npc_plans = checkpoint.get("npc_plans", {})
        self.npc_emotion_arcs = checkpoint.get("npc_emotion_arcs", {})

        print(f"[OK] 存档已加载: #{save_id}")
        return True

    # ===== 新增：pending 模式方法 =====

    async def process_turn_preview(self, player_input: str, director_plan: dict = None) -> Dict[str, Any]:
        """
        推演但不应用状态变更，返回预览（pending模式）
        """
        result = await self.process_turn(player_input, director_plan=director_plan)
        self._pending_turn = result
        return {
            "pending": True,
            "turn_num": self.world_state.current_turn,
            "player_action": player_input,
            "scene": result["scene"],
            "npc_outputs": result["turn"].npc_outputs,
            "outline": result["turn"].outline_summary,
            "state_changes": result["turn"].state_after,
        }

    async def retry_npc(self, npc_id: str, user_hint: str = "") -> Dict[str, Any]:
        """
        重推某个 NPC，使用用户的提示词影响推理
        """
        if not self._pending_turn:
            raise ValueError("没有待确认的推演结果")

        context = self._pending_turn["turn"].context_object
        agent = self.npc_agents.get(npc_id)
        if not agent:
            raise ValueError(f"NPC不存在: {npc_id}")

        modified_context = dict(context)
        modified_context["user_hint"] = user_hint

        use_llm = _llm_state.get("client") is not None
        if use_llm:
            new_output = await agent._reason_llm(modified_context)
        else:
            new_output = agent._reason_rules(modified_context)
        self._pending_turn["turn"].npc_outputs[npc_id] = new_output

        if use_llm:
            outline = await OutlineAgent.synthesize_llm(context, self._pending_turn["turn"].npc_outputs, self.world_state, session=self)
            scene = await SceneAgent.generate_llm(context, self._pending_turn["turn"].npc_outputs, self.world_state, outline, session=self)
        else:
            outline = OutlineAgent.synthesize_rules(context, self._pending_turn["turn"].npc_outputs, self.world_state)
            scene = SceneAgent.generate_rules(context, self._pending_turn["turn"].npc_outputs, self.world_state, outline)

        self._pending_turn["turn"].outline_summary = outline
        self._pending_turn["turn"].scene_output = scene

        return {
            "pending": True,
            "retried_npc": npc_id,
            "npc_output": new_output,
            "outline": outline,
            "scene": scene,
        }

    # ===== 工具执行器（表驱动） =====

    def _exec_modify_relationship(self, result: dict, source_npc: str):
        npc_id = result.get("npc_id")
        delta = result.get("delta", 0)
        if npc_id:
            self.world_state.relationships[npc_id] = self.world_state.relationships.get(npc_id, 0) + delta

    def _exec_apply_state_change(self, result: dict, source_npc: str):
        self.world_state.apply_changes([{
            "var": result.get("var"), "op": result.get("op", "set"), "value": result.get("value"),
        }])

    def _exec_move_player(self, result: dict, source_npc: str):
        loc = result.get("location_id")
        if loc:
            self.world_state.current_location = loc

    def _exec_set_world_prop(self, result: dict, source_npc: str):
        key = result.get("key")
        if key:
            self.world_state.world_props[key] = result.get("value")

    def _exec_give_item(self, result: dict, source_npc: str):
        if "inventory" not in self.world_state.variables:
            self.world_state.variables["inventory"] = []
        self.world_state.variables["inventory"].append({
            "name": result.get("item_name"), "desc": result.get("description", ""),
        })

    def _exec_take_item(self, result: dict, source_npc: str):
        if "inventory" in self.world_state.variables:
            self.world_state.variables["inventory"] = [
                i for i in self.world_state.variables["inventory"]
                if i.get("name") != result.get("item_name")
            ]

    def _exec_spawn_npc(self, result: dict, source_npc: str):
        npc_id = f"dynamic_npc_{len(self.script_data.get('npcs', []))}"
        new_npc = {
            "id": npc_id, "name": result.get("name"), "title": result.get("title"),
            "personality": result.get("personality"),
            "default_location": result.get("location_id"), "attitude_toward_player": 50,
        }
        self.script_data.setdefault("npcs", []).append(new_npc)
        self.npc_agents[npc_id] = NPCAgent(npc_id, new_npc, self.world_state)
        self.world_state.relationships[npc_id] = 50

    def _exec_remove_npc(self, result: dict, source_npc: str):
        npc_id = result.get("npc_id")
        if npc_id:
            self.script_data["npcs"] = [n for n in self.script_data.get("npcs", []) if n.get("id") != npc_id]
            self.npc_agents.pop(npc_id, None)
            self.world_state.relationships.pop(npc_id, None)

    def _exec_update_npc_relationship(self, result: dict, source_npc: str):
        self._apply_npc_relationship_updates([result])

    def _exec_share_information(self, result: dict, source_npc: str):
        fact = result.get("fact", "")
        known_by = result.get("known_by", "")
        if fact:
            self.information_network.append({
                "origin_turn": self.world_state.current_turn,
                "fact": fact, "known_by": [known_by] if known_by else [],
                "spread_chance": 0.3, "distortion": 0, "max_spread": 5,
            })
            if len(self.information_network) > 30:
                self.information_network = self.information_network[-30:]

    def _exec_update_plan(self, result: dict, source_npc: str):
        goal = result.get("goal", "")
        if goal and source_npc:
            self.npc_plans[source_npc] = {
                "goal": goal, "next_step": result.get("next_step", ""),
                "progress": result.get("progress", ""),
                "updated_turn": self.world_state.current_turn,
            }

    def _exec_change_faction_reputation(self, result: dict, source_npc: str):
        fid = result.get("faction_id", "")
        if fid:
            delta = result.get("delta", 0)
            entry = self.faction_reputation.setdefault(fid, {"value": 50})
            entry["value"] = max(0, min(100, entry.get("value", 50) + delta))
            entry["title"] = self._reputation_title(entry["value"])
            if result.get("reason"):
                entry["last_reason"] = result["reason"]

    def _exec_trigger_event(self, result: dict, source_npc: str):
        name = result.get("event_name", "")
        desc = result.get("description", "")
        if name and self.event_engine:
            evt = {"id": f"dynamic_{name}_{self.world_state.current_turn}",
                   "name": name, "description": desc}
            self.event_engine.inject_dynamic_event(evt, "one_time")
            print(f'  [事件] Director 触发: {name}')

    def confirm_turn(self) -> Dict[str, Any]:
        """
        确认待推演结果，应用所有状态变更（含 tool calling 提议）并保存轮次
        """
        if not self._pending_turn:
            raise ValueError("没有待确认的推演结果")

        turn = self._pending_turn["turn"]

        # 收集所有 agent tool_calls（Director + NPC + Outline）
        all_tool_calls = []
        dp = turn.context_object.get("director", {})
        for tc in dp.get("tool_calls", []):
            all_tool_calls.append(tc)
        for npc_id, npc_out in turn.npc_outputs.items():
            for tc in npc_out.get("tool_calls", []):
                tc["_source_npc"] = npc_id
                all_tool_calls.append(tc)
        for tc in turn.outline_summary.get("tool_calls", []):
            all_tool_calls.append(tc)

        # 表驱动执行
        for tc in all_tool_calls:
            result = tc.get("result", {})
            tc_type = result.get("type", "")
            source_npc = tc.get("_source_npc", "")
            handler = self._tool_handlers.get(tc_type)
            if handler:
                handler(result, source_npc)

        if all_tool_calls:
            print(f'[Tool] 执行了 {len(all_tool_calls)} 个工具提议')

        turn.state_after = self.world_state.get_snapshot()
        self.world_state.turns.append(turn)

        # 更新 NPC 跨回合记忆
        NPC_MEMORY_MAX = 5
        for npc_id, npc_out in turn.npc_outputs.items():
            if npc_out.get("skipped") or npc_out.get("action_type") in ("观望", "无关"):
                continue
            entry = f"T{turn.turn_num}: {npc_out.get('action_type', '')}。"
            if npc_out.get("dialogue"):
                d = npc_out["dialogue"]
                entry += f"说：「{d[:30]}{'...' if len(d) > 30 else ''}」"
            elif npc_out.get("thought"):
                entry += npc_out["thought"][:30]
            mem = self.npc_memories.setdefault(npc_id, [])
            mem.append(entry)
            if len(mem) > NPC_MEMORY_MAX:
                self.npc_memories[npc_id] = mem[-NPC_MEMORY_MAX:]

        # 更新 NPC 情感弧线
        EMOTION_ARC_MAX = 8
        for npc_id, npc_out in turn.npc_outputs.items():
            emotion = npc_out.get("emotion")
            if emotion and not npc_out.get("skipped"):
                arc = self.npc_emotion_arcs.setdefault(npc_id, [])
                arc.append(emotion)
                if len(arc) > EMOTION_ARC_MAX:
                    self.npc_emotion_arcs[npc_id] = arc[-EMOTION_ARC_MAX:]

        # 信息传播
        self._propagate_information()

        result = self._pending_turn
        self._pending_turn = None

        return {
            "confirmed": True,
            "turn_num": turn.turn_num,
            "state_after": turn.state_after,
            "choices": turn.player_choices,
        }

    async def regenerate_scene(self, hint: str = "") -> Dict[str, Any]:
        """重新生成当前轮的场景描写（Swipe 机制），保持 NPC 推理不变"""
        if not self._pending_turn:
            raise ValueError("没有待确认的推演结果可以重生成")

        turn = self._pending_turn["turn"]
        context = turn.context_object
        npc_outputs = turn.npc_outputs

        if hint:
            context = dict(context)
            context["regenerate_hint"] = hint

        use_llm = _llm_state.get("client") is not None
        if not use_llm:
            return {"error": "重生成需要 AI 连接"}

        outline = turn.outline_summary
        scene = await SceneAgent.generate_llm(context, npc_outputs, self.world_state, outline, session=self)
        scene["tension"] = context.get("tension", 0)
        scene["npc_interventions"] = context.get("npc_interventions", [])

        if self.world_tree:
            node = self.world_tree.get_node(self.world_tree.active_node_id)
            if node:
                swipes = node.setdefault("swipes", [])
                if not swipes:
                    swipes.append({
                        "narrative": turn.scene_output.get("scene_text", ""),
                        "choices": turn.scene_output.get("choices", []),
                    })
                swipes.append({
                    "narrative": scene.get("scene_text", ""),
                    "choices": scene.get("choices", []),
                })
                node["active_swipe_index"] = len(swipes) - 1

        turn.scene_output = scene
        self._pending_turn["scene"] = scene

        return {
            "scene": scene,
            "swipe_index": len(self.world_tree.get_node(self.world_tree.active_node_id).get("swipes", [])) - 1 if self.world_tree else 0,
            "total_swipes": len(self.world_tree.get_node(self.world_tree.active_node_id).get("swipes", [])) if self.world_tree else 1,
        }

    def swipe_to(self, direction: str) -> Optional[Dict[str, Any]]:
        """切换到当前节点的不同 swipe 版本"""
        if not self.world_tree or not self._pending_turn:
            return None
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

        turn = self._pending_turn["turn"]
        turn.scene_output["scene_text"] = swipe["narrative"]
        turn.scene_output["choices"] = swipe.get("choices", [])
        self._pending_turn["scene"] = turn.scene_output

        return {
            "scene_text": swipe["narrative"],
            "choices": swipe.get("choices", []),
            "swipe_index": idx,
            "total_swipes": len(swipes),
        }


# ===== 使用示例 =====

if __name__ == "__main__":
    import os
    import glob

    print("启动酒馆 RPG 多Agent推演系统原型\n")

    SCRIPTS_DIR = os.path.join(_BASE_DIR, "data", "scripts")

    # ===== 剧本扫描 =====
    _LOC_ALIAS = {"player_apartment": "player_home"}
    def _fix_loc(loc_id):
        return _LOC_ALIAS.get(loc_id, loc_id) if loc_id else ""

    def scan_scripts():
        results = []
        for path in sorted(glob.glob(os.path.join(SCRIPTS_DIR, "*.json"))):
            fn = os.path.basename(path)
            if fn.startswith("enrich") or fn.endswith("_current.json"): continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            sid = fn.replace(".json", "")
            results.append({
                "id": sid,
                "filename": fn,
                "name": d.get("script_name", d.get("name", sid)),
                "description": d.get("description", d.get("script_description", ""))[:200],
                "npc_count": len(d.get("npcs", [])),
                "preset_count": len(d.get("player_presets", [])),
                "loc_count": len(d.get("locations", [])),
            })
        return results

    def load_script_data(script_id):
        path = os.path.join(SCRIPTS_DIR, script_id + ".json")
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    async def load_branch(self, node_id: str) -> Dict[str, Any]:
        """加载历史分支（支持多线剧情）"""
        if not self.world_tree:
            return {"error": "WorldTree not available"}
        node = self.world_tree.get_node(node_id)
        if not node:
            return {"error": f"Node {node_id} not found"}
        self.world_tree.set_active_node(node_id)
        state_snap = node.get("state_snapshot", {})
        if state_snap:
            self.world_state.current_turn = state_snap.get("turn", 0)
            self.world_state.current_location = state_snap.get("location_id", "")
            if state_snap.get("date"):
                try:
                    self.world_state.current_date = datetime.fromisoformat(state_snap["date"])
                except:
                    pass
            self.world_state.relationships = state_snap.get("relationships", {})
            self.world_state.player_attrs = state_snap.get("player_attrs", {})
            self.world_state.variables = state_snap.get("variables", {})
        return {
            "node_id": node_id,
            "turn": node.get("turn_number"),
            "action": node.get("player_action", {}).get("text", ""),
            "narrative": node.get("ai_response", ""),
            "state": self.world_state.get_snapshot(),
            "choices": node.get("choices_presented", [])
        }

    # 延迟初始化 session
    _state = {"session": None}

    def _s():
        return _state["session"]

    # ===== FastAPI Web 服务 =====
    app = FastAPI(title="酒馆 RPG", version="1.0.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    static_dir = os.path.join(_BASE_DIR, "static")
    if os.path.isdir(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/")
    async def root():
        html_path = os.path.join(_BASE_DIR, "rpg_map_ui_v2.html")
        if not os.path.exists(html_path):
            html_path = os.path.join(_BASE_DIR, "rpg_map_ui.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse(content="<h1>RPG 系统启动中...</h1>")

    # ===== 剧本构建 API =====
    _builder_state: Dict[str, Any] = {"builder": None}

    @app.post("/api/create/start")
    async def create_start(req: dict):
        theme = req.get("theme", "").strip()
        if not theme:
            raise HTTPException(400, "请输入剧本主旨")
        if not _llm_state.get("client"):
            profiles = load_ai_profiles()
            active_profile = next((p for p in profiles if p.get("is_active")), None)
            if not active_profile:
                raise HTTPException(400, "创建剧本需要配置 AI 模型")
            set_ai_profile(active_profile)
        builder = ScriptBuilder()
        _builder_state["builder"] = builder
        try:
            return await builder.process_input(theme)
        except RuntimeError as e:
            _builder_state["builder"] = None
            raise HTTPException(502, str(e))

    @app.post("/api/create/next")
    async def create_next(req: dict):
        builder = _builder_state.get("builder")
        if not builder:
            raise HTTPException(400, "没有正在进行的构建")
        user_input = req.get("input", "").strip()
        if not user_input:
            raise HTTPException(400, "请输入内容")
        try:
            result = await builder.process_input(user_input)
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        if result.get("done"):
            _builder_state["builder"] = None
            result["auto_start"] = True
        return result

    @app.get("/api/create/status")
    async def create_status():
        builder = _builder_state.get("builder")
        if not builder:
            return {"active": False}
        return {
            "active": True,
            "phase": builder.phase,
            "theme": builder.theme,
        }

    @app.get("/api/scripts")
    async def list_scripts():
        return scan_scripts()

    @app.get("/api/scripts/{script_id}")
    async def get_script_detail(script_id: str):
        data = load_script_data(script_id)
        if not data:
            raise HTTPException(404, "剧本不存在")
        presets = []
        for p in data.get("player_presets", []):
            attrs = {}
            for k, v in (p.get("attributes") or {}).items():
                attrs[k] = v["value"] if isinstance(v, dict) else v
            presets.append({
                "id": p["id"],
                "name": p.get("name", p["id"]),
                "bio": p.get("bio", ""),
                "personality": p.get("personality", ""),
                "long_term_goal": p.get("long_term_goal", ""),
                "portrait_desc": p.get("portrait_desc", ""),
                "opening_text": p.get("opening_text", ""),
                "initial_location": p.get("initial_location", ""),
                "attributes": attrs,
            })
        return {
            "id": script_id,
            "name": data.get("script_name", data.get("name", script_id)),
            "description": data.get("description", data.get("script_description", "")),
            "npc_count": len(data.get("npcs", [])),
            "loc_count": len(data.get("locations", [])),
            "locations": [{"id": l["id"], "name": l.get("name", l["id"])} for l in data.get("locations", [])],
            "npcs": [{"id": n["id"], "name": n.get("name", n["id"]), "title": n.get("title", "")} for n in data.get("npcs", [])],
            "player_presets": presets,
        }

    @app.post("/api/start")
    async def start_game(req: dict):
        script_id = req.get("script_id", "football_legend")
        preset_id = req.get("preset_id")
        ai_profile_id = req.get("ai_profile_id")

        data = load_script_data(script_id)
        if not data:
            raise HTTPException(404, "剧本不存在")

        # 设置 AI profile
        if ai_profile_id:
            profiles = load_ai_profiles()
            profile = next((p for p in profiles if p["id"] == ai_profile_id), None)
            if profile:
                set_ai_profile(profile)
                print(f"[OK] AI profile: {profile['name']} ({profile['model']})")

        s = TavernRPGSession.__new__(TavernRPGSession)
        s.script_data = data
        s.world_state = WorldStateManager(data)
        s.npc_agents = {}
        for npc in data.get("npcs", []):
            npc_id = npc.get("id")
            s.npc_agents[npc_id] = NPCAgent(npc_id, npc, s.world_state)
        s._pending_turn = None
        s._last_validation = {}

        if TAVERN_MODULES_AVAILABLE:
            try:
                s.world_tree = WorldTree(script_id=script_id)
                s.event_engine = EventEngine(data)
                s.lorebook = Lorebook(data.get('lorebook', []))
                s.history_summarizer = HistorySummarizer()
                s.state_manager = s.world_state._state_manager
                s.dice_roller = DiceRoller()
                s.script_variables = ScriptVariables(data.get('variables', []))
                s.script_variables.init_state(s.world_state.event_state)
                s.trigger_engine = TriggerEngine(data.get('triggers', []), s.script_variables)
                s.meta_event_bus = MetaEventBus()
                s.vector_memory = VectorMemory(script_id) if VectorMemory and _VECTOR_AVAILABLE else None
                s.class_system = ClassRegistry(data.get('system')) if ClassRegistry else None
            except Exception as e:
                print(f'[WARN] init failed: {e}')
                s.world_tree = None
                s.event_engine = None
                s.lorebook = None
                s.history_summarizer = None
                s.state_manager = None
                s.dice_roller = DiceRoller()
                s.script_variables = None
                s.trigger_engine = None
                s.meta_event_bus = MetaEventBus()
                s.vector_memory = None
                s.class_system = None
        else:
            s.world_tree = None
            s.event_engine = None
            s.lorebook = None
            s.history_summarizer = None
            s.state_manager = None
            s.dice_roller = DiceRoller()
            s.script_variables = None
            s.trigger_engine = None
            s.meta_event_bus = MetaEventBus()
            s.vector_memory = None
            s.class_system = None

        s.tension = 0
        s.npc_chat_history = {}
        s.npc_intervention_cooldowns = {}

        opening_text = ""
        # 优先用 preset 开场白，否则用剧本 opening.text
        if preset_id:
            preset = next((p for p in data.get("player_presets", []) if p.get("id") == preset_id), None)
            if preset:
                if preset.get("attributes"):
                    for k, v in preset["attributes"].items():
                        val = v["value"] if isinstance(v, dict) else v
                        s.world_state.player_attrs[k] = val
                if preset.get("initial_location"):
                    loc = _fix_loc(preset["initial_location"])
                    s.world_state.current_location = loc
                if preset.get("name"):
                    s.script_data["_player_name"] = preset["name"]
                opening_text = preset.get("opening_text", "")
        if not opening_text:
            opening_text = data.get("opening", {}).get("text", "")

        _state["session"] = s
        print(f"[OK] 游戏开始: {data.get('script_name', script_id)}, 预设={preset_id or 'none'}")
        return {
            "started": True,
            "script_name": data.get("script_name", data.get("name", script_id)),
            "npc_count": len(s.npc_agents),
            "opening_text": opening_text,
            "ai_enabled": _llm_state.get("client") is not None,
        }

    @app.get("/api/state")
    async def get_state():
        if not _s():
            return {"started": False}
        session = _s()
        return {
            "started": True,
            "turn": session.world_state.current_turn,
            "location": session.world_state.current_location,
            "date": session.world_state.current_date.isoformat(),
            "player_attrs": session.world_state.player_attrs,
            "relationships": session.world_state.relationships,
            "npcs": [
                {
                    "id": npc.get("id"),
                    "name": npc.get("name"),
                    "default_location": _fix_loc(npc.get("default_location", "")),
                    "personality": npc.get("personality", ""),
                    "title": npc.get("title", ""),
                    "visual_profile": npc.get("visual_profile"),
                }
                for npc in session.script_data.get("npcs", [])
            ],
            "locations": [
                {"id": loc["id"], "name": loc.get("name"), "visual_profile": loc.get("visual_profile")}
                for loc in session.script_data.get("locations", [])
            ],
            "world_props": session.world_state.world_props,
            "variables": session.world_state.variables,
        }

    @app.get("/api/ai-profiles")
    async def list_ai_profiles():
        profiles = load_ai_profiles()
        return [
            {
                "id": p["id"],
                "name": p["name"],
                "provider_type": p["provider_type"],
                "model": p["model"],
                "is_active": bool(p.get("is_active")),
            }
            for p in profiles
        ]

    @app.post("/api/turn")
    async def submit_turn(req: dict):
        if not _s():
            raise HTTPException(400, "游戏未开始")
        s = _s()
        raw_input = req.get("action", "")

        use_llm = _llm_state.get("client") is not None
        if use_llm:
            plan = await DirectorAgent.plan(raw_input, s.world_state, s.script_data, s.world_state.turns[-5:])
        else:
            plan = DirectorAgent.plan_rules(raw_input, s.world_state)

        side_results = []
        game_action_intent = None

        for intent in plan.get("intents", []):
            itype = intent.get("type", "game_action")
            if itype == "game_action":
                game_action_intent = intent
            elif itype == "ui_command":
                side_results.append({"type": "ui_command", "command": intent.get("command", ""), "params": intent.get("params", {})})
            elif itype == "save_load":
                action = intent.get("action", "save")
                if action == "save":
                    save_id = s.save_checkpoint()
                    side_results.append({"type": "save_load", "action": "save", "id": save_id})
                else:
                    side_results.append({"type": "save_load", "action": action})
            elif itype == "query":
                side_results.append({"type": "query", "answer": intent.get("answer", plan.get("reply_to_player", ""))})
            elif itype == "meta_feedback":
                side_results.append({"type": "meta_feedback", "noted": True, "reply": plan.get("reply_to_player")})

        if game_action_intent:
            turn_result = await s.process_turn_preview(raw_input, director_plan=game_action_intent)
            turn_result["director_plan"] = plan
            turn_result["side_results"] = side_results
            return turn_result

        return {
            "pending": False,
            "director_plan": plan,
            "side_results": side_results,
            "reply": plan.get("reply_to_player"),
        }

    @app.post("/api/retry-npc/{npc_id}")
    async def retry_npc_handler(npc_id: str, req: dict):
        if not _s():
            raise HTTPException(400, "游戏未开始")
        user_hint = req.get("hint", "")
        return await _s().retry_npc(npc_id, user_hint)

    @app.post("/api/confirm")
    async def confirm_handler(req: dict = None):
        if not _s():
            raise HTTPException(400, "游戏未开始")
        req = req or {}
        choice_id = req.get("choice_id")
        result = _s().confirm_turn()
        # Apply choice effects if specified
        if choice_id and result.get("confirmed"):
            pending_scene = _s().world_state.turns[-1].scene_output if _s().world_state.turns else {}
            choices = pending_scene.get("choices", [])
            choice = next((c for c in choices if c.get("id") == choice_id), None)
            if choice:
                effects = choice.get("effects", {})
                rel_changes = effects.get("relationship_changes", {})
                changes = [{"var": f"relationship.{k}", "op": "add", "value": v} for k, v in rel_changes.items()]
                # 资源消耗
                resource_cost = effects.get("resource_cost", {})
                for attr, delta in resource_cost.items():
                    changes.append({"var": f"player.{attr}", "op": "add", "value": delta})
                if changes:
                    _s().world_state.apply_changes(changes)
                # 技能检定
                skill_check = effects.get("skill_check")
                if skill_check and isinstance(skill_check, dict):
                    check_result = _s()._resolve_skill_check(
                        skill_check.get("attr", ""),
                        skill_check.get("difficulty", "medium"),
                    )
                    result["skill_check"] = check_result
                    # 生成检定结果叙事（借鉴酒馆 PromptBuilder check_result 注入模式）
                    try:
                        outcome = check_result.get("outcome", "")
                        labels = {"critical_success": "大成功", "success": "成功",
                                  "failure": "失败", "critical_failure": "大失败"}
                        label = labels.get(outcome, outcome)
                        attr_display = check_result.get("attribute", "")
                        narr_prompt = (
                            f"技能检定（{attr_display}）→ {label}。{check_result.get('description', '')}\n"
                            f"玩家选择了：{choice.get('text', '')}\n"
                            f"用50-100字第二人称描写这个检定的结果场景。"
                            f"不要提及具体数值，用角色感受和行为体现结果。"
                        )
                        narr = await llm_call(
                            "你是RPG叙事师，根据技能检定结果生成简短场景描写。",
                            narr_prompt, max_tokens=200,
                        )
                        if narr:
                            result["skill_check_narrative"] = narr
                    except Exception:
                        pass
                # 延迟后果
                delayed = effects.get("delayed_consequence")
                if delayed and isinstance(delayed, dict) and _s().event_engine:
                    _s().event_engine.add_consequence(
                        _s().world_state.event_state,
                        {
                            "description": delayed.get("description", ""),
                            "turns_delay": delayed.get("turns_delay", 2),
                            "trigger_chance": delayed.get("trigger_chance", 0.5),
                        },
                    )
                    result["consequence_registered"] = delayed.get("description", "")
                result["choice_applied"] = choice_id
        return result

    @app.post("/api/talk")
    async def talk_to_npc_handler(req: dict):
        """与 NPC 独立对话"""
        if not _s():
            raise HTTPException(400, "游戏未开始")
        npc_id = req.get("npc_id")
        message = req.get("message", "").strip()
        if not npc_id or not message:
            raise HTTPException(400, "需要 npc_id 和 message")
        return await _s().talk_to_npc(npc_id, message)

    @app.post("/api/regenerate")
    async def regenerate_handler(req: dict = None):
        """重新生成场景描写（Swipe）"""
        if not _s():
            raise HTTPException(400, "游戏未开始")
        req = req or {}
        hint = req.get("hint", "")
        return await _s().regenerate_scene(hint)

    @app.post("/api/swipe")
    async def swipe_handler(req: dict):
        """切换不同版本的场景"""
        if not _s():
            raise HTTPException(400, "游戏未开始")
        direction = req.get("direction", "right")
        result = _s().swipe_to(direction)
        if result is None:
            raise HTTPException(400, "没有其他版本可切换")
        return result

    @app.post("/api/reset")
    async def reset_game():
        _state["session"] = None
        _llm_state["client"] = None
        _llm_state["profile"] = None
        return {"ok": True}

    @app.post("/api/save")
    async def save_game(req: dict = None):
        if not _s():
            raise HTTPException(400, "游戏未开始")
        req = req or {}
        name = req.get("name")
        save_id = _s().save_checkpoint(name)
        return {"ok": True, "id": save_id}

    @app.get("/api/saves")
    async def list_saves():
        conn = sqlite3.connect(TAVERN_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, script_id, name, turn, game_time, created_at FROM rpg_saves ORDER BY id DESC"
        ).fetchall()
        conn.close()
        return [{"id": r["id"], "filename": f"save_{r['id']}.json", "script_id": r["script_id"], "turn": r["turn"], "timestamp": r["created_at"]} for r in rows]

    @app.post("/api/load")
    async def load_save(req: dict):
        save_id = req.get("id")
        if not save_id:
            raise HTTPException(400, "缺少 id")

        conn = sqlite3.connect(TAVERN_DB)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM rpg_saves WHERE id = ?", (save_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(404, "存档不存在")

        checkpoint = json.loads(row["data"])
        script_id = row["script_id"]
        data = load_script_data(script_id)
        if not data:
            raise HTTPException(404, f"剧本不存在: {script_id}")

        s = TavernRPGSession.__new__(TavernRPGSession)
        s.script_data = data
        s.world_state = WorldStateManager(data)
        s.npc_agents = {}
        for npc in data.get("npcs", []):
            npc_id = npc.get("id")
            s.npc_agents[npc_id] = NPCAgent(npc_id, npc, s.world_state)
        s._pending_turn = None
        s._last_validation = {}

        if TAVERN_MODULES_AVAILABLE:
            try:
                s.world_tree = WorldTree(script_id=script_id)
                s.event_engine = EventEngine(data)
                s.lorebook = Lorebook(data.get('lorebook', []))
                s.history_summarizer = HistorySummarizer()
                s.state_manager = s.world_state._state_manager
                s.dice_roller = DiceRoller()
                s.script_variables = ScriptVariables(data.get('variables', []))
                s.script_variables.init_state(s.world_state.event_state)
                s.trigger_engine = TriggerEngine(data.get('triggers', []), s.script_variables)
                s.meta_event_bus = MetaEventBus()
                s.vector_memory = VectorMemory(script_id) if VectorMemory and _VECTOR_AVAILABLE else None
                s.class_system = ClassRegistry(data.get('system')) if ClassRegistry else None
            except Exception as e:
                print(f'[WARN] load init failed: {e}')
                s.world_tree = None
                s.event_engine = None
                s.lorebook = None
                s.history_summarizer = None
                s.state_manager = None
                s.dice_roller = DiceRoller()
                s.script_variables = None
                s.trigger_engine = None
                s.meta_event_bus = MetaEventBus()
                s.vector_memory = None
                s.class_system = None
        else:
            s.world_tree = None
            s.event_engine = None
            s.lorebook = None
            s.history_summarizer = None
            s.state_manager = None
            s.dice_roller = DiceRoller()
            s.script_variables = None
            s.trigger_engine = None
            s.meta_event_bus = MetaEventBus()
            s.vector_memory = None
            s.class_system = None

        ws = checkpoint.get("world_state", {})
        s.world_state.current_turn = ws.get("turn", 0)
        s.world_state.current_location = ws.get("location_id", ws.get("location", ""))
        if ws.get("date"):
            try:
                s.world_state.current_date = datetime.fromisoformat(ws["date"])
            except Exception:
                pass
        s.world_state.player_attrs = ws.get("player_attrs", {})
        s.world_state.relationships = ws.get("relationships", {})
        s.world_state.variables = ws.get("variables", {})
        s.world_state.world_props = ws.get("world_props", {})

        s.npc_memories = checkpoint.get("npc_memories", {})
        s.npc_relationships_global = checkpoint.get("npc_relationships_global", {})
        s.npc_relationships_known = checkpoint.get("npc_relationships_known", {})
        s.information_network = checkpoint.get("information_network", [])
        s.faction_reputation = checkpoint.get("faction_reputation", {})

        # 恢复 turns 列表
        turns_data = checkpoint.get("turns", [])
        s.world_state.turns = []
        for td in turns_data:
            try:
                s.world_state.turns.append(Turn(**td))
            except Exception:
                pass

        # 恢复补充字段
        s.world_state.event_state = ws.get("event_state", checkpoint.get("event_state", {}))
        s.world_state.lorebook_timed_state = checkpoint.get("lorebook_timed_state", {})
        if hasattr(s.world_state, 'summary_state'):
            s.world_state.summary_state = checkpoint.get("summary_state", {})
        s.npc_chat_history = checkpoint.get("npc_chat_history", {})
        s.npc_plans = checkpoint.get("npc_plans", {})
        s.npc_emotion_arcs = checkpoint.get("npc_emotion_arcs", {})

        _state["session"] = s

        # 构建 turnHistory 供前端恢复
        turn_history = []
        for t in s.world_state.turns:
            turn_history.append({
                "turn_num": t.turn_num,
                "timestamp": t.timestamp,
                "player_action": t.player_action,
                "summary": t.outline_summary.get("summary", "") if isinstance(t.outline_summary, dict) else "",
                "scene_text": t.scene_output.get("scene_text", "") if isinstance(t.scene_output, dict) else "",
            })

        return {
            "ok": True,
            "script_name": data.get("script_name", script_id),
            "turn": s.world_state.current_turn,
            "turn_history": turn_history,
        }

    @app.get("/api/validation")
    async def get_validation():
        s = _state.get("session")
        if not s:
            return {"status": "no_session"}
        v = getattr(s, '_last_validation', {})
        if v.get("pending"):
            return {"status": "pending"}
        return {"status": "done", "validation": v}

    @app.post("/api/delete-save")
    async def delete_save(req: dict):
        save_id = req.get("id")
        if not save_id:
            raise HTTPException(400, "缺少 id")

        conn = sqlite3.connect(TAVERN_DB)
        try:
            conn.execute("DELETE FROM rpg_saves WHERE id = ?", (save_id,))
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            conn.close()

    @app.get("/api/history")
    async def get_history():
        if not _s():
            return {"turns": []}
        turns = []
        for turn in _s().world_state.turns[-10:]:
            turns.append({
                "turn_num": turn.turn_num,
                "timestamp": turn.timestamp,
                "player_action": turn.player_action,
            })
        return {"turns": turns}

    _init_rpg_saves_table()

    print("启动 FastAPI 服务...")
    print("访问 http://localhost:8080 打开 RPG 系统")
    uvicorn.run(app, host="127.0.0.1", port=8080)
