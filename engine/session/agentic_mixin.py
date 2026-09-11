"""Agentic mixin -- dual-agent loop mode (foreground narration + background settlement).

Alternate to the fixed multi-stage pipeline in PipelineMixin. Selected by the
PIPELINE_MODE config flag; both paths produce a `parsed` dict compatible with
ResponseParser._empty_result() so the downstream state application is unchanged.

Foreground agent : lean context + retrieval tools -> free-text narrative
Background agent : narrative -> state settlement tool calls -> parsed dict
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass, field

from typing import TYPE_CHECKING

from ai.base import strip_think_tags

if TYPE_CHECKING:
    from engine.game_session import GameSession

logger = logging.getLogger(__name__)


@dataclass
class _AgentResult:
    """Holder for async-generator _agent_loop's final return value."""
    text: str = ""
    records: list = field(default_factory=list)

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
    {"type": "function", "function": {
        "name": "review_narrative",
        "description": "审查你刚写的叙事文本，检查认知越界/人称错误/NPC声线偏差/决策越权等问题。在输出最终叙事前调用此工具做自查",
        "parameters": {"type": "object", "properties": {
            "narrative": {"type": "string", "description": "要审查的叙事文本"},
        }, "required": ["narrative"]},
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
        result_holder: _AgentResult | None = None,
    ):
        """ReAct loop as async generator. Yields intermediate events.

        Final text + tool records are written into *result_holder* (since
        Python async generators cannot ``return`` a value).

        dispatch(tool_name, args) -> str result fed back to the model.
        """
        holder = result_holder or _AgentResult()
        msgs = list(messages)
        records: list[dict] = []
        content = ""

        for round_num in range(max_rounds):
            # --- abort check ---
            if getattr(self, '_abort_flag', False):
                logger.info("[%s] 用户中断", label)
                yield {"type": "aborted", "round": round_num, "label": label}
                break

            logger.info("[%s:R%d] 调用 LLM (tools=%d, msgs=%d)", label, round_num + 1, len(tools), len(msgs))
            resp = await self.ai_provider.generate_with_tools(
                msgs, system=system, tools=tools,
                max_tokens=max_tokens, **self._stage_kwargs(stage_key),
            )
            tc_list = resp.get("tool_calls") or []
            content = strip_think_tags(resp.get("content") or "")

            if not tc_list:
                text_preview = content[:80].replace('\n', ' ') if content else "(空)"
                logger.info("[%s] 完成 (第%d轮, %d字): %s...", label, round_num + 1, len(content), text_preview)
                holder.text = content
                holder.records = records
                yield {"type": "agent_done", "label": label,
                       "round": round_num + 1, "text": content}
                return

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
                    if asyncio.iscoroutine(result):
                        result = await result
                except Exception as exc:
                    logger.warning("[%s] 工具 %s 执行失败: %s — 返回错误让模型修正", label, name, exc)
                    error_ctx = {"error": str(exc)}
                    if name.startswith("update_"):
                        error_ctx["hint"] = "请检查参数格式和值范围后重试"
                    result = json.dumps(error_ctx, ensure_ascii=False)
                records.append({"name": name, "args": args, "result": result})
                result_preview = str(result)[:120].replace('\n', ' ')
                logger.info("[%s:R%d] %s(%s) → %s", label, round_num + 1, name,
                           ", ".join(f"{k}={repr(v)[:30]}" for k, v in args.items()), result_preview)

                # Yield tool-call event for SSE consumers
                yield {
                    "type": "tool_call",
                    "label": label,
                    "round": round_num + 1,
                    "tool_name": name,
                    "tool_args": args,
                    "tool_result": str(result)[:500],
                }

                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result if isinstance(result, str)
                               else json.dumps(result, ensure_ascii=False),
                })

            # --- inject check: let the user inject a message between rounds ---
            inject_queue = getattr(self, '_inject_queue', None)
            if inject_queue:
                injected = inject_queue.pop(0)
                msgs.append({"role": "user", "content": injected})
                yield {"type": "user_inject", "label": label, "message": injected}

        else:
            # Exhausted max_rounds without returning
            logger.warning("[%s] 达到最大轮数 %d", label, max_rounds)
            yield {"type": "agent_max_rounds", "label": label, "max_rounds": max_rounds}

        holder.text = content
        holder.records = records

    # ================================================================
    # Foreground agent -- retrieval + narration
    # ================================================================

    async def _dispatch_foreground_tool(self, ctx: dict, name: str, args: dict) -> str:
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
        if name == "review_narrative":
            return await self._review_narrative_tool(args.get("narrative", ""), ctx)
        # Retrieval / dice tools reuse the native executor
        return self._run_tool_native(name, args)

    async def _review_narrative_tool(self, narrative: str, ctx: dict) -> str:
        """LLM-based narrative review. Calls the same review prompt as Stage 3.5."""
        if not narrative.strip():
            return "错误：叙事为空"
        try:
            state = self.current_state
            pc_name = state.get("player", {}).get("name", "")
            # Build present_npcs list[dict] matching build_narrative_review_prompt signature
            npc_states = state.get("npcs", {})
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
            # action_text from ctx (player's action this turn)
            action_text = ctx.get("action_text", "")
            review_msgs, review_sys = self.prompt_builder.build_narrative_review_prompt(
                narrative, action_text, present_npcs, pc_name=pc_name,
            )
            raw = await self.ai_provider.generate(
                review_msgs, system=review_sys, max_tokens=500,
                **self._stage_kwargs("state")
            )
            return raw.strip() if raw else "审查完成，未返回结果"
        except Exception as e:
            logger.warning("review_narrative_tool 失败: %s", e)
            return f"审查调用失败: {e}"

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

        # Full previous-round narrative for continuity (truncated to 1500 chars)
        prev_round_text = ""
        recent = ctx.get("recent_nodes", [])
        if recent:
            rn = recent[-1]
            prev_narrative = rn.get("ai_response", "") or rn.get("narrative", "")
            action_raw = rn.get("player_action")
            prev_action = action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")
            if prev_action:
                prev_round_text = f"[玩家行动] {prev_action}\n\n{prev_narrative}"
            else:
                prev_round_text = prev_narrative

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
        if prev_round_text:
            parts.append(f"<previous_round>\n{prev_round_text[-1500:]}\n</previous_round>")

        parts.append(f"<player_action>\n{player_action.get('text', '')}\n</player_action>")
        return "\n\n".join(parts)

    async def _run_foreground_agent(self, ctx: dict, route: dict, player_action: dict,
                                     *, streaming: bool = False):
        """Foreground agent: autonomous retrieval + narrative writing.

        When *streaming* is True, operates as an async generator that yields
        intermediate tool-call / agent-done events.  The final (narrative, records)
        are written into the returned _AgentResult.
        """
        system = self._build_foreground_system()
        user = self._build_foreground_context(ctx, player_action)
        messages = [{"role": "user", "content": user}]

        async def _dispatch(name: str, args: dict) -> str:
            return await self._dispatch_foreground_tool(ctx, name, args)

        holder = _AgentResult()
        async for event in self._agent_loop(
            messages, system, FOREGROUND_TOOLS_SCHEMA, _dispatch,
            max_rounds=7, label="前台叙事",
            stage_key="narrative", max_tokens=8192,
            result_holder=holder,
        ):
            if streaming:
                yield event
        # When not streaming, caller accesses holder directly (no yield).
        # Tag holder onto the last yield-cycle for the pipeline to read.
        yield {"type": "_fg_result", "holder": holder}

    def _build_foreground_system(self) -> str:
        """Load the foreground agent system prompt from YAML. Frozen after first build."""
        if self._stable_prefix is not None:
            return self._stable_prefix
        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system(
            "agentic_foreground",
            world_background=(self.script.get("world_background", "") or "")[:500],
            player_name=self.current_state.get("player", {}).get("name", ""),
        )
        ctx_note = self._build_foreground_context_note()
        if ctx_note:
            system += "\n\n" + ctx_note
        self._stable_prefix = system
        return self._stable_prefix

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

    def _record_narrative_facts(self, narrative: str, ctx: dict):
        """将叙事中的新事实写入向量记忆，使未来 recall_history 能检索到。

        世界树节点由下游 _apply_parsed_response 统一创建，此处仅提前写入
        向量存储，确保后台结算或同轮检索即可命中叙事内容。
        """
        if not narrative or not self.vector_memory:
            return
        try:
            action_text = ctx.get("action_text", "")
            vm_text = f"{action_text} → {narrative}"[:1500]
            vm_meta = {
                "turn_number": self.turn_number,
                "game_time": self.current_state.get("game_time", ""),
                "location": self.current_state.get("player", {}).get("location", ""),
                "type": "narrative_prefetch",
            }
            self._schedule_background_task(
                self._async_vector_store(f"ag_{self.turn_number}", vm_text, vm_meta)
            )
        except Exception as e:
            logger.warning("记忆落地失败: %s", e)

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
        *, streaming: bool = False,
    ):
        """Background agent: autonomously settle all state via tool calls.

        When *streaming*, yields intermediate events.  Final records are in the
        returned _AgentResult holder.
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

        holder = _AgentResult()
        async for event in self._agent_loop(
            messages, system, _settlement_tools(), self._dispatch_settlement_tool,
            max_rounds=5, label="后台结算",
            stage_key="state", max_tokens=4096,
            result_holder=holder,
        ):
            if streaming:
                yield event
        yield {"type": "_bg_result", "holder": holder}
        logger.info("[%s] 结算工具调用 %d 次", "background", len(holder.records))

    # ================================================================
    # Audit helper
    # ================================================================

    def _audit_settlement(self, parsed: dict) -> None:
        """轻量审计：记录结算摘要到日志"""
        audit = []
        for sc in parsed.get("state_changes", []):
            audit.append(f"  {sc.get('target')}: {sc.get('op')} {sc.get('value')} ({sc.get('reason', '')})")
        for nc in parsed.get("npc_attitude_changes", []):
            audit.append(f"  NPC {nc.get('npc_id')}: {nc.get('dimension')} {nc.get('change', 0):+d}")
        for ch in parsed.get("choices", []):
            audit.append(f"  选项 {ch.get('id')}: {ch.get('text', '')[:30]} [{ch.get('risk', '')}]")
        if parsed.get("location_change"):
            audit.append(f"  位置: → {parsed['location_change']}")
        if parsed.get("end_time"):
            audit.append(f"  时间: → {parsed['end_time']}")
        if audit:
            logger.info("=== 结算审计 ===\n%s", "\n".join(audit))

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

        logger.info("=" * 50)
        logger.info("AGENTIC 模式开始 — 行动: %s", action_text[:60])
        logger.info("=" * 50)

        # --- Foreground: retrieval + narration ---
        logger.info(">>> 前台叙事 Agent 启动")
        fg_holder: _AgentResult | None = None
        async for event in self._run_foreground_agent(ctx, route, player_action, streaming=streaming):
            if event.get("type") == "_fg_result":
                fg_holder = event["holder"]
            elif streaming:
                yield event

        narrative = (fg_holder.text if fg_holder else "").strip()
        fg_calls = fg_holder.records if fg_holder else []
        if not narrative:
            raise RuntimeError("Agentic 前台叙事为空（工具调用后未输出文本）")
        ctx["tool_results"].extend(fg_calls)
        logger.info(">>> 前台叙事完成 (%d字, %d次工具调用)", len(narrative), len(fg_calls))

        # 工具调用模式无法增量流式，叙事完成后整体下发一次
        if streaming:
            yield {"type": "text", "content": narrative}

        # NPC voice consistency (reuses the workflow path's post-check)
        narrative = await self._maybe_fix_npc_voices(
            narrative, self.current_state, ctx.get("present_npc_ids"),
        )

        # 将叙事中的新事实记录到向量记忆，使未来 recall_history 能检索到
        self._record_narrative_facts(narrative, ctx)

        # --- Background: state settlement ---
        bg_ctx = copy.deepcopy(ctx)  # Frame 隔离：后台不影响前台上下文
        logger.info(">>> 后台结算 Agent 启动")
        bg_holder: _AgentResult | None = None
        async for event in self._run_background_agent(
            bg_ctx, narrative, "", player_action, streaming=streaming,
        ):
            if event.get("type") == "_bg_result":
                bg_holder = event["holder"]
            elif streaming:
                yield event

        bg_calls = bg_holder.records if bg_holder else []
        ctx["tool_results"].extend(bg_calls)
        logger.info(">>> 后台结算完成 (%d次工具调用)", len(bg_calls))

        # Reset abort flag after pipeline completes
        if hasattr(self, '_abort_flag'):
            self._abort_flag = False

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

        choices_count = len(parsed.get("choices", []))
        state_changes = len(parsed.get("state_changes", []))
        npc_att = len(parsed.get("npc_attitude_changes", []))
        logger.info("=" * 50)
        logger.info("AGENTIC 完成 — 叙事%d字 | 选项%d | 状态变更%d | NPC态度%d | 工具调用%d+%d",
                    len(narrative), choices_count, state_changes, npc_att, len(fg_calls), len(bg_calls))
        logger.info("=" * 50)

        self._audit_settlement(parsed)

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

    # ================================================================
    # Feedback-based regeneration
    # ================================================================

    async def regenerate_with_feedback(self, feedback: str) -> dict:
        """基于用户反馈重新生成当前轮叙事，不改变游戏状态。"""
        last_node = self.world_tree.get_node(self.world_tree.active_node_id) if self.world_tree else None
        if not last_node:
            return {"error": "没有可重生成的回合"}

        action_raw = last_node.get("player_action")
        action_text = action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")

        original_narrative = last_node.get("ai_response", "")

        feedback_system = self._build_foreground_system()
        feedback_system += (
            f"\n\n## 用户反馈\n上一版叙事的问题：{feedback}\n"
            "请根据反馈重新写一版叙事。保持同样的事件和剧情走向，但改善用户指出的问题。"
        )

        user = (
            f"上一版叙事（需要修改）：\n{original_narrative[:2000]}\n\n"
            f"玩家行动：{action_text}\n\n"
            "请根据反馈重写叙事。"
        )
        messages = [{"role": "user", "content": user}]

        fg_holder = _AgentResult()
        async for _event in self._agent_loop(
            messages, feedback_system, FOREGROUND_TOOLS_SCHEMA,
            lambda name, args: self._dispatch_foreground_tool({}, name, args),
            max_rounds=5, label="反馈重写",
            result_holder=fg_holder,
        ):
            pass

        new_narrative = fg_holder.text
        if not new_narrative:
            return {"error": "重写失败"}

        # 更新世界树节点的叙事文本
        last_node["ai_response"] = new_narrative

        return {"ok": True, "narrative": new_narrative}
