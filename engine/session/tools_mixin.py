"""Tools mixin – native tool call execution for GameSession."""

from __future__ import annotations

import json
import logging
import random
import re

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.game_session import GameSession

logger = logging.getLogger(__name__)

_TOOL_CALL_RE = re.compile(r'\[TOOL_CALL:\s*(\w+)\(([^)]*)\)\]')

GAME_TOOLS = {
    "roll_dice": {"desc": "掷骰子检定。参数: skill(技能名), dc(难度,可选)", "example": "roll_dice(察觉, 12)"},
    "check_inventory": {"desc": "检查玩家是否持有某物品。参数: item_name", "example": "check_inventory(火把)"},
    "get_npc_attitude": {"desc": "查询NPC对玩家的好感度。参数: npc_id", "example": "get_npc_attitude(merchant_lin)"},
    "get_time": {"desc": "获取当前游戏时间", "example": "get_time()"},
}

GAME_TOOLS_SCHEMA = [
    {"type": "function", "function": {
        "name": "roll_dice",
        "description": "掷骰子进行技能检定，返回d20结果和成功/失败判定",
        "parameters": {"type": "object", "properties": {
            "skill": {"type": "string", "description": "技能名称，如察觉、交涉、潜行"},
            "dc": {"type": "integer", "description": "难度等级(Difficulty Class)，默认10", "default": 10},
        }, "required": ["skill"]},
    }},
    {"type": "function", "function": {
        "name": "check_inventory",
        "description": "检查玩家背包中是否持有指定物品",
        "parameters": {"type": "object", "properties": {
            "item_name": {"type": "string", "description": "物品名称"},
        }, "required": ["item_name"]},
    }},
    {"type": "function", "function": {
        "name": "get_npc_attitude",
        "description": "查询NPC当前对玩家的好感度/态度",
        "parameters": {"type": "object", "properties": {
            "npc_id": {"type": "string", "description": "NPC的ID标识"},
        }, "required": ["npc_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_time",
        "description": "获取当前的游戏内时间",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "modify_stat",
        "description": "修改玩家的属性值（如生命值、金币等）",
        "parameters": {"type": "object", "properties": {
            "stat": {"type": "string", "description": "属性名称，如hp、gold、mana"},
            "delta": {"type": "integer", "description": "变化量，正数增加，负数减少"},
        }, "required": ["stat", "delta"]},
    }},
    {"type": "function", "function": {
        "name": "query_lore",
        "description": "查询知识库中与关键词相关的条目（世界观/人物/地点信息）",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "查询关键词"},
        }, "required": ["keyword"]},
    }},
    {"type": "function", "function": {
        "name": "change_location",
        "description": "将玩家移动到指定地点",
        "parameters": {"type": "object", "properties": {
            "location_id": {"type": "string", "description": "目标地点ID"},
        }, "required": ["location_id"]},
    }},
    {"type": "function", "function": {
        "name": "set_variable",
        "description": "设置或修改脚本变量的值",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "变量名称"},
            "value": {"type": "string", "description": "变量值"},
        }, "required": ["name", "value"]},
    }},
    # --- P1 上下文 Pull 工具 ---
    {"type": "function", "function": {
        "name": "recall_history",
        "description": "搜索历史对话记录，返回相关回合的摘要",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "最多返回条数", "default": 5},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "query_lorebook",
        "description": "按关键词查询世界书知识条目",
        "parameters": {"type": "object", "properties": {
            "keyword": {"type": "string", "description": "搜索词"},
        }, "required": ["keyword"]},
    }},
    {"type": "function", "function": {
        "name": "query_npc_history",
        "description": "查询与特定NPC的互动历史",
        "parameters": {"type": "object", "properties": {
            "npc_name": {"type": "string", "description": "NPC名称"},
            "max_turns": {"type": "integer", "description": "最近几轮", "default": 5},
        }, "required": ["npc_name"]},
    }},
]


class ToolsMixin:
    """Native tool system: schema definitions and tool call execution."""

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

    # ── Stage 4a/5 工具执行器（工具调用模式）──

    _MAX_ATTITUDE_DELTA = 20
    _VALID_ATTITUDE_DIMENSIONS = ("trust", "affection", "fear", "overall")

    def _run_npc_reaction_tool(self, tool_name: str, args: dict) -> str:
        """Validate an NPC reaction tool call. Does not mutate state; merging is separate.

        Returns a short result string fed back to the model.
        """
        if tool_name != "update_npc_attitude":
            return f"未知NPC工具: {tool_name}"

        npc_id = args.get("npc_id", "")
        if not npc_id:
            return "错误: npc_id 为空，已忽略"
        known = set(self._npc_by_id.keys()) | set(self.current_state.get("npcs", {}).keys())
        if npc_id not in known:
            return f"错误: 未知NPC ID '{npc_id}'，已忽略"

        dimension = args.get("dimension", "")
        if dimension not in self._VALID_ATTITUDE_DIMENSIONS:
            return f"错误: 无效维度 '{dimension}'，已忽略"

        change = args.get("change", 0)
        if not isinstance(change, int):
            return f"错误: change 必须为整数，已忽略"
        clamped = max(-self._MAX_ATTITUDE_DELTA, min(self._MAX_ATTITUDE_DELTA, change))
        note = "" if clamped == change else f"（原值 {change} 已钳制到 {clamped}）"

        self._npc_reaction_tool_calls.append({
            "npc_id": npc_id, "dimension": dimension,
            "change": clamped, "reason": str(args.get("reason", "")),
        })
        return f"已记录 {npc_id} 的 {dimension} 变化: {clamped:+d}{note}"

    def _run_choices_tool(self, tool_name: str, args: dict) -> str:
        """Validate an add_choice tool call. Does not mutate state; merging is separate."""
        if tool_name != "add_choice":
            return f"未知选项工具: {tool_name}"

        text = str(args.get("text", "")).strip()
        if not text:
            return "错误: text 为空，已忽略"

        risk = args.get("risk", "")
        if risk and risk not in ("safe", "moderate", "risky"):
            risk = "moderate"

        choice = {"id": str(args.get("id", "")).strip() or f"c{len(self._choices_tool_calls) + 1}", "text": text}
        if args.get("hint"):
            choice["hint"] = str(args["hint"])
        if args.get("time_hint"):
            choice["time_hint"] = str(args["time_hint"])
        if risk:
            choice["risk"] = risk

        self._choices_tool_calls.append(choice)
        return f"已添加选项 {choice['id']}: {text[:40]}"

    def _reset_stage45_tool_buffers(self):
        """Clear per-turn buffers used by Stage 4a/5 tool mode."""
        self._npc_reaction_tool_calls = []
        self._choices_tool_calls = []

    async def _run_stage45_tools(self, msgs: list[dict], sys_prompt: str,
                                 tools_schema: list[dict], *, max_tokens: int,
                                 stage_key: str, label: str) -> list[dict]:
        """Single-round tool call for Stage 4a/5. Returns the tool_calls list (may be empty).

        The model registers its results via tool calls; no free-text answer is expected,
        so the loop does not feed results back.
        """
        async def _call():
            resp = await self.ai_provider.generate_with_tools(
                msgs, system=sys_prompt or None, tools=tools_schema,
                max_tokens=max_tokens, **self._stage_kwargs(stage_key),
            )
            return resp.get("tool_calls") or []
        from engine.session.pipeline_mixin import _retry_on_failure
        calls = await _retry_on_failure(_call, max_retries=1, label=label)
        return calls or []

    def _merge_npc_reaction_tool_results(self, parsed: dict) -> None:
        """Merge buffered update_npc_attitude calls into parsed in npc_attitude_changes format."""
        if not self._npc_reaction_tool_calls:
            return
        changes = parsed.setdefault("npc_attitude_changes", [])
        seen = {(c.get("npc_id"), c.get("dimension")) for c in changes}
        for call in self._npc_reaction_tool_calls:
            key = (call["npc_id"], call["dimension"])
            if key in seen:
                continue
            seen.add(key)
            entry = {
                "npc_id": call["npc_id"],
                "change": call["change"],
                "reason": call.get("reason", ""),
            }
            # "overall" 不属于三维关系维度，交由下游按默认维度处理
            if call["dimension"] != "overall":
                entry["dimension"] = call["dimension"]
            changes.append(entry)

    def _merge_choices_tool_results(self, parsed: dict) -> None:
        """Merge buffered add_choice calls into parsed["choices"]."""
        if not self._choices_tool_calls:
            return
        by_id = {}
        for c in self._choices_tool_calls:
            by_id[c["id"]] = c
        parsed["choices"] = list(by_id.values())

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
            # Bug 5 fix: 最终 fallback — 搜索 adventure_log
            if not results:
                adventure_log = self.current_state.get("adventure_log", [])
                if isinstance(adventure_log, list):
                    query_lower_al = query.lower()
                    for entry in reversed(adventure_log[-20:]):
                        if not isinstance(entry, dict):
                            continue
                        events = entry.get("events", [])
                        for ev in events:
                            ev_text = ev.get("text", "") if isinstance(ev, dict) else str(ev)
                            if query_lower_al in ev_text.lower():
                                results.append({
                                    "turn": entry.get("turn", "?"),
                                    "summary": ev_text[:200],
                                    "relevance": 0.3,
                                })
                                break
                        if len(results) >= max_results:
                            break
            # 最终 fallback — 搜索 _narrative_buffer（agentic 模式同轮缓存）
            if not results and hasattr(self, '_narrative_buffer'):
                query_lower_nb = query.lower()
                for buf in reversed(self._narrative_buffer):
                    if query_lower_nb in buf.get("text", "").lower():
                        results.append({
                            "turn": buf.get("turn", "?"),
                            "summary": buf["text"][:200],
                            "relevance": 0.4,
                        })
                    if len(results) >= max_results:
                        break
            return json.dumps({"results": results}, ensure_ascii=False)
        if name == "query_lorebook":
            keyword = args.get("keyword", "")
            # Split by whitespace so "金载圭 车智澈 矛盾" matches any individual word
            keywords = [k.strip().lower() for k in keyword.split() if k.strip()]
            if not keywords:
                return json.dumps({"entries": []}, ensure_ascii=False)
            entries = []
            lb = getattr(self.prompt_builder, "lorebook", None)
            if lb is not None:
                for e in lb.entries:
                    if not e.enabled:
                        continue
                    searchable = (
                        (e.comment or "").lower() + " "
                        + " ".join(k.lower() for k in e.keys) + " "
                        + " ".join(k.lower() for k in getattr(e, "secondary_keys", [])) + " "
                        + e.content[:500].lower()
                    )
                    if any(kw in searchable for kw in keywords):
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
