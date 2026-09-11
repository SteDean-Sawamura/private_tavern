"""Agentic mixin -- dual-agent loop mode (foreground narration + background settlement).

Alternate to the fixed multi-stage pipeline in PipelineMixin. Selected by the
PIPELINE_MODE config flag; both paths produce a `parsed` dict compatible with
ResponseParser._empty_result() so the downstream state application is unchanged.

Foreground agent : lean context + retrieval tools -> free-text narrative
Background agent : narrative -> state settlement tool calls -> parsed dict
"""

from __future__ import annotations

import asyncio
import json
import logging

from typing import TYPE_CHECKING

from ai.base import strip_think_tags

if TYPE_CHECKING:
    from engine.game_session import GameSession

logger = logging.getLogger(__name__)

# Foreground (narration) tools: retrieval + scene presentation. Mutating tools
# are deliberately excluded -- the background agent owns all state changes.
FOREGROUND_TOOLS_SCHEMA = [
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

# Background (settlement) tools: state mutation + NPC reaction + choices.
SETTLEMENT_TOOLS_SCHEMA = None  # resolved lazily from engine.game_session


def _settlement_tools() -> list[dict]:
    """State / NPC-reaction / choices schemas (imported lazily to avoid cycles)."""
    global SETTLEMENT_TOOLS_SCHEMA
    if SETTLEMENT_TOOLS_SCHEMA is None:
        from engine.game_session import (
            STATE_TOOLS_SCHEMA, NPC_REACTION_TOOLS, CHOICES_TOOLS,
        )
        SETTLEMENT_TOOLS_SCHEMA = STATE_TOOLS_SCHEMA + NPC_REACTION_TOOLS + CHOICES_TOOLS
    return SETTLEMENT_TOOLS_SCHEMA


class AgenticMixin:
    """Dual Agent Loop mode -- foreground narration + background settlement."""

    # ================================================================
    # Shared ReAct loop
    # ================================================================

    async def _agent_loop(
        self, messages: list[dict], system: str, tools: list[dict],
        dispatch, *, max_rounds: int = 7, label: str = "agent",
        stage_key: str = "narrative", max_tokens: int = 8192,
    ) -> tuple[str, list[dict]]:
        """Run a tool-calling loop until the model stops requesting tools.

        dispatch(tool_name, args) -> str result fed back to the model.
        Returns (final_text, tool_call_records).
        """
        msgs = list(messages)
        records: list[dict] = []

        for round_num in range(max_rounds):
            resp = await self.ai_provider.generate_with_tools(
                msgs, system=system, tools=tools,
                max_tokens=max_tokens, **self._stage_kwargs(stage_key),
            )
            tc_list = resp.get("tool_calls") or []
            content = strip_think_tags(resp.get("content") or "")

            if not tc_list:
                logger.info("[%s] 完成 (第%d轮)", label, round_num + 1)
                return content, records

            assistant_msg = {"role": "assistant", "content": resp.get("content") or None}
            reasoning = resp.get("reasoning_content")
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function",
                 "function": {"name": tc.get("name", ""),
                              "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False)}}
                for tc in tc_list
            ]
            msgs.append(assistant_msg)

            for tc in tc_list:
                name = tc.get("name", "")
                args = tc.get("arguments") or {}
                try:
                    result = dispatch(name, args)
                except Exception as exc:  # tool errors must not kill the turn
                    logger.warning("[%s] 工具 %s 执行失败: %s", label, name, exc)
                    result = json.dumps({"error": str(exc)}, ensure_ascii=False)
                records.append({"name": name, "args": args, "result": result})
                logger.info("[%s:R%d] %s(%s)", label, round_num + 1, name, list(args.keys()))
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result if isinstance(result, str)
                               else json.dumps(result, ensure_ascii=False),
                })

        logger.warning("[%s] 达到最大轮数 %d", label, max_rounds)
        return "", records

    # ================================================================
    # Foreground agent -- retrieval + narration
    # ================================================================

    def _dispatch_foreground_tool(self, ctx: dict, name: str, args: dict) -> str:
        """Execute a foreground tool. Scene tools record into ctx, no state mutation."""
        if name == "set_atmosphere":
            ctx["atmosphere"] = args
            return "氛围已设置: " + ", ".join(
                f"{k}={v}" for k, v in args.items() if v
            )
        if name == "set_scene_image":
            prompt = str(args.get("prompt", "")).strip()
            if not prompt:
                return "错误: prompt 为空，已忽略"
            ctx["scene_image_prompt"] = args
            return "场景图生成已记录"
        # Retrieval / dice tools reuse the native executor
        return self._run_tool_native(name, args)

    def _build_foreground_context(self, ctx: dict, player_action: dict) -> str:
        """Minimal per-turn context for the foreground agent."""
        state = self.current_state
        player = state.get("player", {})
        dn = state.get("display_names", {})

        loc_id = player.get("location", "")
        loc_name = self.prompt_builder._resolve_location_name(loc_id)

        # Compact present NPC list
        npc_states = state.get("npcs", {})
        npc_lines = []
        for nid in (ctx.get("present_npc_ids") or [])[:6]:
            ns = npc_states.get(nid, {})
            if not isinstance(ns, dict):
                continue
            nm = ns.get("name", dn.get(nid, nid))
            att = ns.get("attitude_toward_player", "")
            npc_lines.append(f"- {nm}({nid})" + (f" 态度:{att}" if att != "" else ""))

        attrs = player.get("attributes", {})
        attrs_line = ", ".join(
            f"{dn.get(k, k)}={v}" for k, v in attrs.items()
        ) if isinstance(attrs, dict) else ""
        inv = state.get("inventory", [])
        inv_line = ", ".join(
            f"{it.get('item', '')}x{it.get('quantity', 1)}" for it in inv
            if isinstance(it, dict)
        ) if inv else "无"

        # Last round tail only -- the agent pulls older context via tools
        tail = ""
        recent = ctx.get("recent_nodes", [])
        if recent:
            rn = recent[-1]
            action_raw = rn.get("player_action")
            prev_action = action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")
            prev_narrative = rn.get("ai_response", "")
            tail = (
                f"上一轮玩家行动：{prev_action[:120]}\n"
                f"上一轮结尾：{prev_narrative[-150:] if prev_narrative else '无'}"
            )

        parts = [
            f"<current_scene>\n位置：{loc_name}（{loc_id}）\n"
            f"时间：{state.get('game_time', '')}\n"
            f"天气：{state.get('current_weather', '')}\n</current_scene>",
            f"<player>\n姓名：{player.get('name', '')}\n"
            + (f"属性：{attrs_line}\n" if attrs_line else "")
            + f"背包：{inv_line}\n</player>",
        ]
        if npc_lines:
            parts.append("<npcs_present>\n" + "\n".join(npc_lines) + "\n</npcs_present>")
        if tail:
            parts.append("<previous_round>\n" + tail + "\n</previous_round>")

        parts.append(f"<player_action>\n{player_action.get('text', '')}\n</player_action>")
        return "\n\n".join(parts)

    async def _run_foreground_agent(self, ctx: dict, route: dict, player_action: dict) -> tuple[str, list[dict]]:
        """Foreground agent: autonomous retrieval + narrative writing."""
        system = self._build_foreground_system()
        user = self._build_foreground_context(ctx, player_action)
        messages = [{"role": "user", "content": user}]

        def _dispatch(name: str, args: dict) -> str:
            return self._dispatch_foreground_tool(ctx, name, args)

        narrative, records = await self._agent_loop(
            messages, system, FOREGROUND_TOOLS_SCHEMA, _dispatch,
            max_rounds=7, label="前台叙事",
            stage_key="narrative", max_tokens=8192,
        )
        return narrative, records

    def _build_foreground_system(self) -> str:
        """Load the foreground agent system prompt from YAML."""
        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system(
            "agentic_foreground",
            world_background=(self.script.get("world_background", "") or "")[:500],
            player_name=self.current_state.get("player", {}).get("name", ""),
        )
        ctx_note = self._build_foreground_context_note()
        if ctx_note:
            system += "\n\n" + ctx_note
        return system

    def _build_foreground_context_note(self) -> str:
        """Compact world/state briefing appended to the foreground system prompt."""
        state = self.current_state
        player = state.get("player", {})
        parts = []
        bio = player.get("bio", "")
        personality = player.get("personality", "")
        if bio:
            parts.append(f"角色身份：{bio[:120]}")
        if personality:
            parts.append(f"角色性格：{personality[:100]}")
        goal = state.get("long_term_goal", "")
        if goal:
            parts.append(f"长期目标：{goal[:120]}")
        return "\n".join(parts)

    # ================================================================
    # Background agent -- state settlement
    # ================================================================

    def _dispatch_settlement_tool(self, name: str, args: dict) -> str:
        """Execute a settlement tool. State tools buffer + validate; NPC/choice tools buffer."""
        if name == "update_npc_attitude":
            return self._run_npc_reaction_tool(name, args)
        if name == "add_choice":
            return self._run_choices_tool(name, args)
        return self._run_state_tool(name, args)

    def _build_settlement_system(self) -> str:
        """Load the settlement agent system prompt from YAML."""
        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system("agentic_background")
        all_field_lines = [fdef["desc"] for fdef in self.prompt_builder._FIELD_DEFS.values()]
        return system + "\n\n字段参考：\n" + "\n".join(all_field_lines)

    async def _run_background_agent(
        self, ctx: dict, narrative_text: str, plot_decision: str, player_action: dict,
    ) -> tuple[list[dict], str]:
        """Background agent: autonomously settle all state via tool calls.

        Returns (tool_call_records, agent_summary_text). State is NOT mutated
        here -- the caller merges the records into the parsed dict.
        """
        state = self.current_state
        system = self._build_settlement_system()

        action_text = player_action.get("text", "")
        user_parts = [f"<narrative>\n{narrative_text}\n</narrative>"]
        if plot_decision:
            user_parts.append(f"<plot_skeleton>\n{plot_decision}\n</plot_skeleton>")
        user_parts.append(f"<player_action>\n{action_text}\n</player_action>")
        old_time = ctx.get("old_time", "")
        if old_time:
            user_parts.append(
                f"<time_context>\n当前游戏时间: {old_time}\n"
                "你是end_time的唯一决策者。根据叙事最后场景的时间输出绝对时间戳：\n"
                "- 对话/观察/翻阅文件: 当前时间 +10~30分钟\n"
                "- 常规互动/短途移动: 当前时间 +30分钟~2小时\n"
                "- 长途旅行/大型战斗: 当前时间 +2~8小时\n"
                "- 睡觉/过夜: 若叙事写到入睡那一刻则给入睡时间，若叙事写到醒来才给次日早晨\n"
                f"格式示例: {old_time[:10] or '1970-01-01'}T10:00:00\n</time_context>"
            )
        messages = [{"role": "user", "content": "\n\n".join(user_parts)}]

        summary, records = await self._agent_loop(
            messages, system, _settlement_tools(), self._dispatch_settlement_tool,
            max_rounds=5, label="后台结算",
            stage_key="state", max_tokens=4096,
        )
        logger.info("[%s] 结算工具调用 %d 次", "background", len(records))
        return records, summary

    # ================================================================
    # Pipeline entry (agentic)
    # ================================================================

    async def _execute_agentic_pipeline(self, ctx: dict, route: dict, player_action: dict,
                                        *, streaming: bool = False):
        """Agentic twin-loop pipeline. Async generator yielding the same items as
        _execute_pipeline: intermediate chunks (streaming) + a pipeline_result.
        """
        _warnings: list[str] = []
        action_text = ctx.get("action_text") or player_action.get("text", "")
        ctx.setdefault("tool_results", [])
        self._reset_stage45_tool_buffers()

        # --- Foreground: retrieval + narration ---
        narrative, fg_calls = await self._run_foreground_agent(ctx, route, player_action)
        narrative = (narrative or "").strip()
        if not narrative:
            raise RuntimeError("Agentic 前台叙事为空（工具调用后未输出文本）")
        ctx["tool_results"].extend(fg_calls)

        # 工具调用模式无法增量流式，叙事完成后整体下发一次
        if streaming:
            yield {"type": "text", "content": narrative}

        # NPC voice consistency (reuses the workflow path's post-check)
        narrative = await self._maybe_fix_npc_voices(
            narrative, self.current_state, ctx.get("present_npc_ids"),
        )

        # --- Background: state settlement ---
        bg_calls, _summary = await self._run_background_agent(
            ctx, narrative, "", player_action,
        )
        ctx["tool_results"].extend(bg_calls)

        # Merge settlement tool calls -> parsed (same shape as parse_split_v3)
        parsed = self._merge_state_tool_results(
            [{"name": c["name"], "args": c["args"]} for c in bg_calls
            if c["name"] in ("update_resources", "update_spatial", "update_time",
                             "update_world", "update_extended")]
        )
        # NPC attitude + choices buffers
        self._merge_npc_reaction_tool_results(parsed)
        self._merge_choices_tool_results(parsed)
        if not parsed.get("choices"):
            parsed["choices"] = self._generate_context_choices()

        parsed["narrative"] = narrative
        # Keys consumed downstream by _apply_parsed_response (absent in agentic path)
        ctx.setdefault("plot_decision", "")
        ctx.setdefault("plot_reasoning", "")

        # Build reasoning trace from tool call records
        reasoning_parts = []
        for call in fg_calls:
            reasoning_parts.append(f"[工具] {call['name']}({call['args']}) → {str(call.get('result', ''))[:200]}")
        for call in bg_calls:
            reasoning_parts.append(f"[结算] {call['name']}({call['args']})")
        _narrative_reasoning = "\n".join(reasoning_parts) if reasoning_parts else ""

        yield {
            "type": "pipeline_result",
            "narrative": narrative,
            "parsed": parsed,
            "warnings": _warnings,
            "plot_decision": "",
            "plot_reasoning": "",
            "narrative_reasoning": _narrative_reasoning,
            "compose_msgs": [],
            "compose_sys": "",
        }
