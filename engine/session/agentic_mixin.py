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
import re
import time
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
    prompt_tokens: int = 0
    completion_tokens: int = 0

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
    """Consolidated 4-tool schema for agentic settlement (imported lazily to avoid cycles)."""
    global SETTLEMENT_TOOLS_SCHEMA
    if SETTLEMENT_TOOLS_SCHEMA is None:
        from engine.game_session import CONSOLIDATED_SETTLEMENT_TOOLS
        SETTLEMENT_TOOLS_SCHEMA = CONSOLIDATED_SETTLEMENT_TOOLS
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
        _continuation_prefix = ""  # #11: 截断续写时保存前一段文本

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

            # #11: 如果是续写轮次，拼接前段文本
            if _continuation_prefix:
                content = _continuation_prefix.rstrip("…——,，") + content
                _continuation_prefix = ""

            # #10 成本追踪: 累加 token 用量
            usage = resp.get("usage") or {}
            holder.prompt_tokens += usage.get("prompt_tokens", 0)
            holder.completion_tokens += usage.get("completion_tokens", 0)

            if not tc_list:
                # #11 叙事断点续写: 检测截断并自动续写
                if content and self._looks_truncated(content) and round_num < max_rounds - 1:
                    logger.info("[%s] 检测到叙事截断，自动续写 (第%d轮, %d字, 末尾: %s)",
                                label, round_num + 1, len(content), repr(content[-10:]))
                    _continuation_prefix = content
                    msgs.append({"role": "assistant", "content": content})
                    msgs.append({"role": "user", "content": "请继续完成叙事，直接续写。"})
                    continue

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

            # 并行执行同一轮的多个工具调用
            async def _execute_tool(tc_item):
                t_name = tc_item.get("name", "")
                t_args = tc_item.get("arguments") or {}
                try:
                    t_result = dispatch(t_name, t_args)
                    if asyncio.iscoroutine(t_result):
                        t_result = await t_result
                except Exception as exc:
                    logger.warning("[%s] 工具 %s 执行失败: %s", label, t_name, exc)
                    error_ctx = {"error": str(exc)}
                    if t_name.startswith("update_"):
                        error_ctx["hint"] = "请检查参数格式和值范围后重试"
                    t_result = json.dumps(error_ctx, ensure_ascii=False)
                return tc_item, t_name, t_args, t_result

            gather_results = await asyncio.gather(*[_execute_tool(tc) for tc in tc_list])

            for tc, name, args, result in gather_results:
                records.append({"name": name, "args": args, "result": result})
                result_preview = str(result)[:120].replace('\n', ' ')
                logger.info("[%s:R%d] %s → %s", label, round_num + 1, name, result_preview)
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
        """规则优先 + LLM 按需的叙事审查。"""
        if not narrative.strip():
            return "错误：叙事为空"

        # 阶段1：快速规则检查
        issues = self._quick_rule_check(narrative, ctx)
        if not issues:
            return "审查通过：规则检查未发现问题"

        # 阶段2：有问题时才调 LLM 深度审查
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
            return "规则检查发现问题：\n" + "\n".join(issues)

    def _quick_rule_check(self, narrative: str, ctx: dict) -> list[str]:
        """快速规则检查：人称、认知越界关键词、决策越权词。"""
        issues = []
        # 人称检查：出现"我"但前200字无"你"可能是人称错误
        if "我" in narrative and "你" not in narrative[:200]:
            issues.append("人称可能错误：使用了'我'而非'你'")
        # 全知视角 / 认知越界关键词
        forbidden = ["殊不知", "却不知", "事实上", "他心想", "他暗自"]
        for f in forbidden:
            if f in narrative:
                issues.append(f"可能的全知视角：'{f}'")
        # 决策越权：叙事不应替玩家做决定
        agency_words = ["你决定", "你选择了", "你毫不犹豫", "你立刻决定"]
        for w in agency_words:
            if w in narrative:
                issues.append(f"可能的决策越权：'{w}'")
        return issues

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

        # 首轮注入开局文本（预设角色的 opening_text + 开局剧情描述）
        if self.turn_number <= 1 and not prev_round_text:
            opening_parts = []
            # 预设角色开局文本
            opening_text = state.get("opening_text", "")
            if opening_text:
                opening_parts.append(opening_text[:500])
            # 剧本通用开局
            script_opening = self.script.get("opening", {}).get("text", "")
            if script_opening and script_opening != opening_text:
                opening_parts.append(script_opening[:500])
            # 角色背景
            bio = player.get("bio", "")
            if bio:
                opening_parts.append(f"角色背景：{bio[:200]}")
            goal = player.get("long_term_goal", "") or state.get("long_term_goal", "")
            if goal:
                opening_parts.append(f"当前目标：{goal[:150]}")
            if opening_parts:
                parts.append("<opening_context>\n" + "\n\n".join(opening_parts) + "\n</opening_context>")

        if prev_round_text:
            parts.append(f"<previous_round>\n{prev_round_text[-1500:]}\n</previous_round>")

        # 注入 workflow 模式有的关键上下文
        hint_parts = []

        # 1. Author's note
        an = getattr(self, 'authors_note', '') or ''
        if an:
            hint_parts.append(f"创作指令：{an[:200]}")

        # 2. Negative prompt
        neg = getattr(self, 'negative_prompt', '') or ''
        if neg:
            hint_parts.append(f"禁止事项：{neg[:150]}")

        # 3. Activated lorebook entries (constant + 当前激活的前5条)
        activated_lore = ctx.get("activated_lore", [])
        if activated_lore and hasattr(self, 'prompt_builder') and self.prompt_builder.lorebook:
            lore_texts = []
            for entry in activated_lore[:5]:
                title = getattr(entry, 'comment', '') or ', '.join(getattr(entry, 'keys', [])[:2])
                content = (getattr(entry, 'content', '') or '')[:150]
                if content:
                    lore_texts.append(f"- {title}: {content}")
            if lore_texts:
                hint_parts.append("相关知识：\n" + "\n".join(lore_texts))

        # 4. Event sections (事件/后果提示)
        event_sections = ctx.get("event_sections", {})
        if event_sections:
            for section_name, section_text in event_sections.items():
                if section_text:
                    hint_parts.append(f"{section_name}：{str(section_text)[:100]}")

        # 5. Pacing/tone
        pacing = state.get("pacing_state", {})
        if pacing:
            tension = pacing.get("tension", 50)
            trend = pacing.get("trend", "stable")
            hint_parts.append(f"节奏：张力{tension}/100 趋势{trend}")

        if hint_parts:
            parts.append("<context_hints>\n" + "\n".join(hint_parts) + "\n</context_hints>")

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

        # 动态裁剪前台工具集
        tools = list(FOREGROUND_TOOLS_SCHEMA)

        # 无 NPC 场景去掉 NPC 工具
        present_npcs = ctx.get("present_npc_ids", [])
        if not present_npcs:
            tools = [t for t in tools if t["function"]["name"] not in ("query_npc_history", "get_npc_attitude")]

        # 无骰子设定去掉 roll_dice
        dice_enabled = self.script.get("settings", {}).get("dice_check", {}).get("default_enabled", True)
        if not dice_enabled:
            tools = [t for t in tools if t["function"]["name"] != "roll_dice"]

        async def _dispatch(name: str, args: dict) -> str:
            return await self._dispatch_foreground_tool(ctx, name, args)

        holder = _AgentResult()
        async for event in self._agent_loop(
            messages, system, tools, _dispatch,
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
        """Stable segment (frozen) + dynamic segment (rebuilt each turn)."""
        # 稳定段：首轮冻结
        if self._stable_prefix is None:
            from engine.prompt_loader import PromptLoader
            self._stable_prefix = PromptLoader.get().render_system(
                "agentic_foreground",
                world_background=(self.script.get("world_background", "") or "")[:500],
                player_name=self.current_state.get("player", {}).get("name", ""),
            )

        # 动态段：每轮重建
        dynamic = self._build_foreground_dynamic_section()
        return self._stable_prefix + "\n\n" + dynamic if dynamic else self._stable_prefix

    def _build_foreground_dynamic_section(self) -> str:
        """每轮变化的上下文，追加到 system prompt 末尾。"""
        parts = []
        state = self.current_state
        player = state.get("player", {})

        # 角色身份
        bio = player.get("bio", "")
        if bio:
            parts.append(f"角色身份：{bio[:120]}")
        personality = player.get("personality", "")
        if personality:
            parts.append(f"角色性格：{personality[:100]}")
        goal = state.get("long_term_goal", "") or player.get("long_term_goal", "")
        if goal:
            parts.append(f"长期目标：{goal[:120]}")

        # 当前 tone
        an = getattr(self, 'authors_note', '')
        if an:
            parts.append(f"[创作指令] {an[:200]}")

        # #5 玩家建模提示
        model_hint = self.player_model.hint_for_agent()
        if model_hint:
            parts.append(model_hint)

        return "\n".join(parts)

    def _record_narrative_facts(self, narrative: str, ctx: dict):
        """将叙事中的新事实写入向量记忆，使未来 recall_history 能检索到。

        世界树节点由下游 _apply_parsed_response 统一创建，此处不写 world_tree
        （避免产生重复的 prefetch 节点）。仅写入 vector_memory（异步，无重复
        问题）并缓存到 ctx._narrative_buffer 供同轮 recall_history 使用。
        """
        logger.info("记忆落地: %d字, vector_memory=%s, world_tree=%s",
                    len(narrative or ''),
                    'yes' if self.vector_memory else 'no',
                    'yes' if self.world_tree else 'no')
        if not narrative:
            return
        # 缓存到 self，供同轮 recall_history fallback 使用
        if not hasattr(self, '_narrative_buffer'):
            self._narrative_buffer = []
        self._narrative_buffer.append({
            "turn": self.turn_number,
            "text": narrative[:1000],
        })
        # 写入 vector_memory（异步，不阻塞）
        if not self.vector_memory:
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
        """Execute a consolidated settlement tool. Routes to existing validators."""
        if name == "update_state":
            return self._handle_update_state(args)
        if name == "manage_npcs":
            return self._handle_manage_npcs(args)
        if name == "add_choices":
            return self._handle_add_choices(args)
        if name == "set_turn_meta":
            return self._handle_set_turn_meta(args)
        return f"未知工具: {name}"

    # ── update_state: 拆分到原有 _run_state_tool 验证 ──

    _UPDATE_STATE_RES_KEYS = ("state_changes", "inventory_changes", "activate_states", "deactivate_states", "game_over")
    _UPDATE_STATE_SPATIAL_KEYS = ("location_change", "reveal_locations", "npc_location_changes", "room_changes", "scene_details")

    def _handle_update_state(self, args: dict) -> str:
        results = []
        if "end_time" in args:
            results.append(self._run_state_tool("update_time", {"end_time": args["end_time"]}))
        res_args = {k: args[k] for k in self._UPDATE_STATE_RES_KEYS if k in args}
        if res_args:
            results.append(self._run_state_tool("update_resources", res_args))
        sp_args = {k: args[k] for k in self._UPDATE_STATE_SPATIAL_KEYS if k in args}
        if sp_args:
            results.append(self._run_state_tool("update_spatial", sp_args))
        if "world_property_changes" in args:
            results.append(self._run_state_tool("update_world", {"world_property_changes": args["world_property_changes"]}))
        return " | ".join(results) if results else "无状态变更"

    # ── manage_npcs: 遍历 operations 分发 ──

    def _handle_manage_npcs(self, args: dict) -> str:
        results = []
        for op in args.get("operations", []):
            op_type = op.get("op", "")
            if op_type == "lookup":
                results.append(self._dispatch_npc_lookup(op.get("name", "")))
            elif op_type == "register":
                results.append(self._dispatch_npc_register(op))
            elif op_type == "attitude":
                npc_id = op.get("npc_id", "")
                results.append(self._run_npc_reaction_tool("update_npc_attitude", {
                    "npc_id": npc_id,
                    "dimension": op.get("dimension", ""),
                    "change": op.get("change", 0),
                    "reason": op.get("reason", ""),
                }))
                # #4 叙事图谱：记录态度变更关系
                reason = op.get("reason", "")
                player_id = self.current_state.get("player", {}).get("id", "player")
                self.narrative_graph.add_relation(
                    player_id, npc_id, f"attitude_{op.get('dimension', 'overall')}",
                    turn=self.turn_number, description=reason,
                )
            elif op_type == "offscreen":
                results.append(self._run_state_tool("update_extended", {
                    "offscreen_npc_updates": [{
                        "name": op.get("name", ""),
                        "action": op.get("action", ""),
                        "location": op.get("location", ""),
                    }],
                }))
            elif op_type == "faction":
                results.append(f"已记录阵营 {op.get('faction', '?')} 声望变化: {op.get('delta', 0):+d}")
            elif op_type == "moral":
                results.append(f"已记录道德维度 {op.get('axis', '?')} 变化: {op.get('change', 0):+d}")
            elif op_type == "recruit":
                results.append(f"已记录 {op.get('npc_id', '?')} 加入队伍")
            elif op_type == "dismiss":
                results.append(f"已记录 {op.get('npc_id', '?')} 离队")
            else:
                results.append(f"未知操作类型: {op_type}")
        return " | ".join(results) if results else "无NPC操作"

    def _dispatch_npc_lookup(self, query: str) -> str:
        """NPC lookup by name or ID (same logic as old lookup_npc tool)."""
        query = query.strip()
        npcs = self.current_state.get("npcs", {})
        if query in npcs:
            npc = npcs[query]
            return json.dumps({"found": True, "id": query, "name": npc.get("name", query)}, ensure_ascii=False)
        for nid, ndata in npcs.items():
            if isinstance(ndata, dict) and query in (ndata.get("name", ""), nid):
                return json.dumps({"found": True, "id": nid, "name": ndata.get("name", nid)}, ensure_ascii=False)
        for npc in self.script.get("npcs", []):
            if query in (npc.get("name", ""), npc.get("id", "")):
                return json.dumps({"found": True, "id": npc["id"], "name": npc.get("name", ""), "source": "script"}, ensure_ascii=False)
        return json.dumps({"found": False}, ensure_ascii=False)

    def _dispatch_npc_register(self, op: dict) -> str:
        """Immediately register a new NPC so same-turn attitude changes work."""
        npc_id = op.get("id", "")
        npc_name = op.get("name", "")
        player_id = self.current_state.get("player", {}).get("id", "player")
        player_name = self.current_state.get("player", {}).get("name", "")
        if npc_id == player_id or (player_name and npc_name == player_name):
            logger.warning("跳过注册玩家角色为NPC: %s/%s", npc_id, npc_name)
            return f"跳过: {npc_name} 是玩家角色"
        if npc_id and npc_id in self.current_state.get("npcs", {}):
            logger.info("NPC %s 已存在，跳过重复注册", npc_id)
            return json.dumps({"registered": False, "id": npc_id, "reason": "already_exists"}, ensure_ascii=False)
        if npc_id:
            self.current_state.setdefault("npcs", {})[npc_id] = {
                "name": npc_name or npc_id,
                "bio": op.get("bio", ""),
                "attitude_toward_player": op.get("attitude_toward_player", 50),
            }
            self._npc_by_id[npc_id] = self.current_state["npcs"][npc_id]
            # #4 叙事图谱：自动添加 NPC 实体
            self.narrative_graph.add_entity(npc_id, "npc", npc_name or npc_id)
        return json.dumps({"registered": True, "id": npc_id}, ensure_ascii=False)

    # ── add_choices: 批量 ──

    def _handle_add_choices(self, args: dict) -> str:
        results = []
        for choice in args.get("choices", []):
            results.append(self._run_choices_tool("add_choice", choice))
        return " | ".join(results) if results else "无选项"

    # ── set_turn_meta: summary + image ──

    def _handle_set_turn_meta(self, args: dict) -> str:
        parts = []
        summary = args.get("summary", "").strip()
        if summary:
            self._turn_summary_override = summary
            parts.append("摘要已设置: " + summary[:30])
        image_prompt = args.get("image_prompt", "").strip()
        if image_prompt:
            self._scene_image_prompt_override = {
                "prompt": image_prompt,
                "style": args.get("image_style", "realistic"),
            }
            parts.append("图片 prompt 已设置")
        return " | ".join(parts) if parts else "无元数据"

    # ── 合并工具 → 旧工具格式转换（供 _merge_state_tool_results 复用）──

    @staticmethod
    def _explode_update_state(args: dict) -> list[dict]:
        """Convert one update_state call into old-style tool calls for merging."""
        calls = []
        if "end_time" in args:
            calls.append({"name": "update_time", "args": {"end_time": args["end_time"]}})
        _res_keys = ("state_changes", "inventory_changes", "activate_states", "deactivate_states", "game_over")
        res_args = {k: args[k] for k in _res_keys if k in args}
        if res_args:
            calls.append({"name": "update_resources", "args": res_args})
        _sp_keys = ("location_change", "reveal_locations", "npc_location_changes", "room_changes", "scene_details")
        sp_args = {k: args[k] for k in _sp_keys if k in args}
        if sp_args:
            calls.append({"name": "update_spatial", "args": sp_args})
        if "world_property_changes" in args:
            calls.append({"name": "update_world", "args": {"world_property_changes": args["world_property_changes"]}})
        return calls

    @staticmethod
    def _explode_manage_npcs(args: dict) -> list[dict]:
        """Extract state-level operations from manage_npcs into old-style update_extended calls."""
        ext: dict = {}
        for op in args.get("operations", []):
            op_type = op.get("op", "")
            if op_type == "register":
                npc_entry = {k: op[k] for k in ("id", "name", "title", "bio", "personality",
                             "location", "trust", "affection", "fear") if k in op}
                ext.setdefault("new_npcs", []).append(npc_entry)
            elif op_type == "offscreen":
                ext.setdefault("offscreen_npc_updates", []).append({
                    "name": op.get("name", ""), "action": op.get("action", ""),
                    "location": op.get("location", ""),
                })
            elif op_type == "faction":
                ext.setdefault("faction_reputation_changes", []).append({
                    "faction_id": op.get("faction", ""), "change": op.get("delta", 0),
                    "reason": op.get("reason", ""),
                })
            elif op_type == "moral":
                ext.setdefault("moral_alignment_changes", []).append({
                    "axis": op.get("axis", ""), "change": op.get("change", 0),
                    "reason": op.get("reason", ""),
                })
            elif op_type == "recruit":
                ext.setdefault("recruit_companions", []).append(op.get("npc_id", ""))
            elif op_type == "dismiss":
                ext.setdefault("dismiss_companions", []).append(op.get("npc_id", ""))
            # lookup / attitude are handled during dispatch (buffered), not in merge
        return [{"name": "update_extended", "args": ext}] if ext else []

    def _build_settlement_system(self) -> str:
        """Load the settlement agent system prompt from YAML."""
        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system("agentic_background")
        all_field_lines = [fdef["desc"] for fdef in self.prompt_builder._FIELD_DEFS.values()]
        return system + "\n\n字段参考：\n" + "\n".join(all_field_lines)

    async def _run_background_agent(
        self, ctx: dict, narrative_text: str, plot_decision: str, player_action: dict,
        *, streaming: bool = False, skip_hints: str = "", route: dict | None = None,
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
        # Bug 2 fix: 注入已知地点 ID 列表，防止 Agent 虚构地点
        known_locations = list(self._location_by_id.keys()) if hasattr(self, '_location_by_id') else []
        if known_locations:
            loc_lines = [f"  {lid}: {self._location_by_id[lid].get('name', lid)}" for lid in known_locations[:20]]
            user_parts.append(
                "<known_locations>\n只能使用以下地点ID，不要创造新的：\n"
                + "\n".join(loc_lines) + "\n</known_locations>"
            )

        if skip_hints:
            user_parts.append(skip_hints)

        messages = [{"role": "user", "content": "\n\n".join(user_parts)}]

        # 按 route skip 标志裁剪后台工具集
        # 合并工具后，NPC态度是 manage_npcs 的子操作，不再单独移除工具
        # skip 标志通过 skip_hints 文本传递给 Agent
        tools = list(_settlement_tools())

        holder = _AgentResult()
        async for event in self._agent_loop(
            messages, system, tools, self._dispatch_settlement_tool,
            max_rounds=5, label="后台结算",
            stage_key="state", max_tokens=4096,
            result_holder=holder,
        ):
            if streaming:
                yield event
        yield {"type": "_bg_result", "holder": holder}
        logger.info("[%s] 结算工具调用 %d 次", "background", len(holder.records))

    # ================================================================
    # Truncation detection (#11)
    # ================================================================

    @staticmethod
    def _looks_truncated(text: str) -> bool:
        """保守检测叙事是否被 max_tokens 截断。"""
        text = (text or "").rstrip()
        if not text or len(text) < 200:
            return False
        last_char = text[-1]
        # 正常结尾字符
        if last_char in "。！？」）…\n\"'":
            return False
        return True

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
        _start = time.monotonic()
        _warnings: list[str] = []
        action_text = ctx.get("action_text") or player_action.get("text", "")
        ctx.setdefault("tool_results", [])
        self._reset_stage45_tool_buffers()
        # 后台 Agent 后处理 override 初始化
        self._turn_summary_override = ""
        self._scene_image_prompt_override = None

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
        _skip_settlement = route.get("skip_state_settlement", False)
        _skip_npc = route.get("skip_npc_reaction", False)
        _skip_choices = route.get("skip_choices", False)

        bg_calls: list[dict] = []
        if _skip_settlement and _skip_npc and _skip_choices:
            logger.info(">>> 路由指示跳过全部后台结算")
        else:
            # 构造 skip hints 供后台 Agent 参考
            skip_notes: list[str] = []
            if _skip_settlement:
                skip_notes.append("本轮无需修改任何状态（skip_state_settlement=true）")
            if _skip_npc:
                skip_notes.append("本轮无需修改NPC态度（skip_npc_reaction=true）")
            if _skip_choices:
                skip_notes.append("本轮无需生成选项（skip_choices=true）")
            skip_hint_text = ""
            if skip_notes:
                skip_hint_text = "<skip_hints>\n" + "\n".join(skip_notes) + "\n</skip_hints>"

            # 后台 Agent 只需要少量字段，不做 deepcopy
            bg_ctx = {
                "action_text": ctx.get("action_text", ""),
                "old_time": ctx.get("old_time", ""),
                "present_npc_ids": ctx.get("present_npc_ids", []),
            }
            logger.info(">>> 后台结算 Agent 启动")
            bg_holder: _AgentResult | None = None
            async for event in self._run_background_agent(
                bg_ctx, narrative, "", player_action,
                streaming=streaming, skip_hints=skip_hint_text, route=route,
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
        # Explode consolidated tools into old-style calls for _merge_state_tool_results
        state_calls = []
        for c in bg_calls:
            if c["name"] == "update_state":
                state_calls.extend(self._explode_update_state(c["args"]))
            elif c["name"] == "manage_npcs":
                state_calls.extend(self._explode_manage_npcs(c["args"]))
        parsed = self._merge_state_tool_results(state_calls)
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
        # Bug 4 fix: 计算所有状态变更类型，不仅是 state_changes
        state_changes = (
            len(parsed.get("state_changes", []))
            + len(parsed.get("activate_states", []))
            + len(parsed.get("deactivate_states", []))
            + len(parsed.get("inventory_changes", []))
        )
        npc_att = len(parsed.get("npc_attitude_changes", []))
        logger.info("=" * 50)
        logger.info("AGENTIC 完成 — 叙事%d字 | 选项%d | 状态变更%d | NPC态度%d | 工具调用%d+%d",
                    len(narrative), choices_count, state_changes, npc_att, len(fg_calls), len(bg_calls))
        logger.info("=" * 50)

        self._audit_settlement(parsed)

        # E1/E3: trace + performance for agentic pipeline
        _total_prompt = (fg_holder.prompt_tokens if fg_holder else 0) + (bg_holder.prompt_tokens if bg_holder else 0)
        _total_completion = (fg_holder.completion_tokens if fg_holder else 0) + (bg_holder.completion_tokens if bg_holder else 0)
        elapsed_ms = int((time.monotonic() - _start) * 1000)
        trace = {
            "turn": self.turn_number,
            "mode": "agentic",
            "plan": "",
            "reflection": "",
            "rounds": [],
            "total_tokens": {"prompt": _total_prompt, "completion": _total_completion},
            "narrative_length": len(narrative),
            "tools_count": len(fg_calls) + len(bg_calls),
            "duration_ms": elapsed_ms,
        }
        for i, record in enumerate(fg_calls + bg_calls):
            trace["rounds"].append({
                "step": i + 1,
                "tool": record["name"],
                "args_summary": str(record.get("args", {}))[:100],
                "result_summary": str(record.get("result", ""))[:100],
            })
        if not hasattr(self, '_agent_traces'):
            self._agent_traces = []
        self._agent_traces.append(trace)
        if len(self._agent_traces) > 10:
            self._agent_traces.pop(0)

        if not hasattr(self, '_performance_stats'):
            self._performance_stats = {
                "total_turns": 0, "total_tokens": 0,
                "avg_latency_ms": 0, "tool_usage": {},
                "avg_narrative_length": 0, "mode_distribution": {},
            }
        ps = self._performance_stats
        ps["total_turns"] += 1
        ps["total_tokens"] += _total_prompt + _total_completion
        n = ps["total_turns"]
        ps["avg_latency_ms"] = int((ps["avg_latency_ms"] * (n - 1) + elapsed_ms) / n)
        ps["avg_narrative_length"] = int((ps["avg_narrative_length"] * (n - 1) + len(narrative)) / n)
        ps["mode_distribution"]["agentic"] = ps["mode_distribution"].get("agentic", 0) + 1
        for r in fg_calls + bg_calls:
            tool = r["name"]
            ps["tool_usage"][tool] = ps["tool_usage"].get(tool, 0) + 1

        # 标记后处理已在 agentic 管线中完成
        ctx["_agentic_post_processed"] = True
        if self._turn_summary_override:
            ctx["turn_summary_override"] = self._turn_summary_override
        if self._scene_image_prompt_override:
            ctx["scene_image_prompt"] = self._scene_image_prompt_override

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
            "proactive_hint": ctx.get("_proactive_hint", ""),
            "token_usage": {
                "prompt": _total_prompt,
                "completion": _total_completion,
                "total": _total_prompt + _total_completion,
            },
            "agent_trace": trace,
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

        # 补充完整返回值，使调用方（process_action 早返回路径）拿到与正常管线一致的 dict
        state = self.current_state
        return {
            "ok": True,
            "narrative": new_narrative,
            "choices": last_node.get("choices_presented", []),
            "state": self._slim_snapshot(state),
            "node_id": self.world_tree.active_node_id if self.world_tree else "",
            "game_time": state.get("game_time", ""),
            "dice_rolls": [],
            "warnings": [],
            "feedback_regen": True,  # 标记这是反馈重生成
        }

    # ================================================================
    # Adaptive tools -- dynamic tool set based on game state (#3)
    # ================================================================

    def _build_adaptive_tools(self, ctx: dict) -> list[dict]:
        """Build tool list dynamically based on game state."""
        from engine.game_session import _unified_tools
        tools = list(_unified_tools())

        state = self.current_state
        inventory = state.get("inventory", [])

        # Unlock send_message if player has a communication device
        comm_items = ["电话", "对讲机", "无线电", "传呼机", "手机"]
        has_comm = any(
            any(ci in (it.get("item", "") if isinstance(it, dict) else str(it))
                for ci in comm_items)
            for it in inventory
        )
        if has_comm:
            tools.append({"type": "function", "function": {
                "name": "send_message",
                "description": "通过通讯设备发送消息给NPC",
                "parameters": {"type": "object", "properties": {
                    "recipient": {"type": "string", "description": "收件人NPC名"},
                    "message": {"type": "string", "description": "消息内容"},
                }, "required": ["recipient", "message"]},
            }})

        # Remove NPC tools if no NPCs present
        present = ctx.get("present_npc_ids", [])
        if not present:
            tools = [t for t in tools
                     if t["function"]["name"] not in ("query_npc_history", "get_npc_attitude")]

        return tools

    def _handle_send_message(self, args: dict) -> str:
        """Handle send_message tool call (adaptive tool)."""
        recipient = args.get("recipient", "")
        message = args.get("message", "")
        npc_id = None
        for nid, ndata in self.current_state.get("npcs", {}).items():
            if isinstance(ndata, dict) and recipient in (ndata.get("name", ""), nid):
                npc_id = nid
                break
        if not npc_id:
            return json.dumps({"error": f"找不到NPC: {recipient}"}, ensure_ascii=False)
        return json.dumps({"sent": True, "to": recipient,
                           "note": "消息已发出，对方的反应将在后续叙事中体现"},
                          ensure_ascii=False)

    # ================================================================
    # Unified Agent mode (single-loop: retrieval + narrative + settlement)
    # ================================================================

    def _build_unified_system(self) -> str:
        """Build system prompt for the unified agent."""
        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system(
            "agentic_unified",
            world_background=(self.script.get("world_background", "") or "")[:500],
            player_name=self.current_state.get("player", {}).get("name", ""),
        )
        # Append dynamic section (same as foreground agent)
        system += "\n\n" + self._build_foreground_dynamic_section()
        return system

    def _build_unified_context(self, ctx: dict, player_action: dict) -> str:
        """Build per-turn user message for the unified agent (reuses foreground context)."""
        parts = [self._build_foreground_context(ctx, player_action)]

        # #5 注入跨轮经验
        if hasattr(self, '_agent_experience') and self._agent_experience:
            exp_lines = []
            for exp in self._agent_experience[-3:]:
                tools = ", ".join(exp.get("tools_used", [])[:5])
                exp_lines.append(f"T{exp.get('turn', '?')}: {exp.get('plan', '无计划')[:50]} [{tools}]")
            if exp_lines:
                parts.append("<agent_experience>\n" + "\n".join(exp_lines) + "\n</agent_experience>")

        # C1: NPC 自主行为事件
        npc_events = ctx.get("npc_autonomy_events", [])
        if npc_events:
            event_lines = [f"- {e['npc_name']}移动到{e['to']}（{e.get('activity', '')}）" for e in npc_events[:5]]
            parts.append("<npc_movements>\n" + "\n".join(event_lines) + "\n</npc_movements>")

        return "\n\n".join(parts)

    def _build_unified_parsed(self, records: list[dict], ctx: dict) -> dict:
        """Merge tool call records into a parsed dict for _apply_parsed_response."""
        # Explode consolidated tool calls into old-style for _merge_state_tool_results
        state_calls = []
        for c in records:
            if c["name"] == "update_state":
                state_calls.extend(self._explode_update_state(c["args"]))
            elif c["name"] == "manage_npcs":
                state_calls.extend(self._explode_manage_npcs(c["args"]))
        parsed = self._merge_state_tool_results(state_calls)
        # NPC attitude + choices buffers (populated by _handle_finalize_turn / _dispatch_unified_tool)
        self._merge_npc_reaction_tool_results(parsed)
        self._merge_choices_tool_results(parsed)
        if not parsed.get("choices"):
            parsed["choices"] = self._generate_context_choices()
        return parsed

    async def _execute_unified_agent(self, ctx: dict, route: dict, player_action: dict,
                                     *, streaming: bool = False):
        """Unified Agent: single loop handles retrieval + narrative + settlement."""
        _start = time.monotonic()
        _warnings: list[str] = []
        action_text = ctx.get("action_text") or player_action.get("text", "")
        ctx.setdefault("tool_results", [])
        self._reset_stage45_tool_buffers()
        self._turn_summary_override = ""
        self._scene_image_prompt_override = None
        self._delegated_settlement = False

        logger.info("=" * 50)
        logger.info("UNIFIED AGENT 开始 — 行动: %s", action_text[:60])
        logger.info("=" * 50)

        system = self._build_unified_system()
        user = self._build_unified_context(ctx, player_action)

        tools = self._build_adaptive_tools(ctx)

        async def _dispatch_and_record(name, args):
            result = await self._dispatch_unified_tool(ctx, name, args)
            # D1: 记录上一次工具调用（供 undo_my_last_action 使用）
            if name != "undo_my_last_action":
                self._last_tool_result = {"name": name, "args": args}
            return result

        holder = _AgentResult()
        async for event in self._agent_loop(
            [{"role": "user", "content": user}],
            system, tools,
            _dispatch_and_record,
            max_rounds=10, label="统一Agent",
            result_holder=holder,
        ):
            # Emit agent_plan SSE event when submit_plan is called
            if event.get("type") == "tool_call" and event.get("tool_name") == "submit_plan":
                plan_text = (event.get("tool_args") or {}).get("plan", "")
                if plan_text and streaming:
                    yield {"type": "agent_plan", "plan": plan_text}
            elif streaming:
                yield event

        narrative = (holder.text or "").strip()
        if not narrative:
            raise RuntimeError("统一Agent未输出叙事文本")

        # Strip <reflect> tags: log reflection, remove from player-facing narrative
        reflect_match = re.search(r'<reflect>(.*?)</reflect>', narrative, re.DOTALL)
        if reflect_match:
            reflection = reflect_match.group(1).strip()
            narrative = re.sub(r'<reflect>.*?</reflect>', '', narrative, flags=re.DOTALL).strip()
            logger.info("[反思] %s", reflection[:200])
            ctx["_reflection"] = reflection

        ctx["tool_results"].extend(holder.records)

        # Streaming: emit complete narrative
        if streaming:
            yield {"type": "text", "content": narrative}

        # NPC voice consistency
        narrative = await self._maybe_fix_npc_voices(
            narrative, self.current_state, ctx.get("present_npc_ids"),
        )

        # Record narrative facts to vector memory
        self._record_narrative_facts(narrative, ctx)

        # Build parsed result from tool call records
        parsed = self._build_unified_parsed(holder.records, ctx)
        parsed["narrative"] = narrative

        # Reset abort flag
        if hasattr(self, '_abort_flag'):
            self._abort_flag = False

        # Post-processing flags
        ctx["_agentic_post_processed"] = True
        ctx.setdefault("plot_decision", "")
        ctx.setdefault("plot_reasoning", "")
        if self._turn_summary_override:
            ctx["turn_summary_override"] = self._turn_summary_override
        if self._scene_image_prompt_override:
            ctx["scene_image_prompt"] = self._scene_image_prompt_override

        # Build reasoning trace
        reasoning_parts = []
        for c in holder.records:
            reasoning_parts.append(f"[{c['name']}] {str(c.get('args', {}))[:100]}")
        _narrative_reasoning = "\n".join(reasoning_parts)

        # Stats logging
        choices_count = len(parsed.get("choices", []))
        state_changes = (
            len(parsed.get("state_changes", []))
            + len(parsed.get("activate_states", []))
            + len(parsed.get("deactivate_states", []))
            + len(parsed.get("inventory_changes", []))
        )
        npc_att = len(parsed.get("npc_attitude_changes", []))
        logger.info("=" * 50)
        logger.info("UNIFIED AGENT 完成 — 叙事%d字 | 选项%d | 状态变更%d | NPC态度%d | 工具%d",
                    len(narrative), choices_count, state_changes, npc_att, len(holder.records))
        logger.info("=" * 50)

        self._audit_settlement(parsed)

        # #5 跨轮经验记录
        experience = {
            "turn": self.turn_number,
            "plan": ctx.get("_agent_plan", ""),
            "tools_used": [r["name"] for r in holder.records],
            "reflection": ctx.get("_reflection", ""),
            "narrative_length": len(narrative),
        }
        self._agent_experience.append(experience)
        if len(self._agent_experience) > 5:
            self._agent_experience.pop(0)

        # E1: Agent Trace 可视化数据
        elapsed_ms = int((time.monotonic() - _start) * 1000)
        trace = {
            "turn": self.turn_number,
            "mode": "unified",
            "plan": ctx.get("_agent_plan", ""),
            "reflection": ctx.get("_reflection", ""),
            "rounds": [],
            "total_tokens": {"prompt": holder.prompt_tokens, "completion": holder.completion_tokens},
            "narrative_length": len(narrative),
            "tools_count": len(holder.records),
            "duration_ms": elapsed_ms,
        }
        for i, record in enumerate(holder.records):
            trace["rounds"].append({
                "step": i + 1,
                "tool": record["name"],
                "args_summary": str(record.get("args", {}))[:100],
                "result_summary": str(record.get("result", ""))[:100],
            })
        if not hasattr(self, '_agent_traces'):
            self._agent_traces = []
        self._agent_traces.append(trace)
        if len(self._agent_traces) > 10:
            self._agent_traces.pop(0)

        # E3: 性能统计更新
        if not hasattr(self, '_performance_stats'):
            self._performance_stats = {
                "total_turns": 0, "total_tokens": 0,
                "avg_latency_ms": 0, "tool_usage": {},
                "avg_narrative_length": 0, "mode_distribution": {},
            }
        ps = self._performance_stats
        ps["total_turns"] += 1
        ps["total_tokens"] += holder.prompt_tokens + holder.completion_tokens
        n = ps["total_turns"]
        ps["avg_latency_ms"] = int((ps["avg_latency_ms"] * (n - 1) + elapsed_ms) / n)
        ps["avg_narrative_length"] = int((ps["avg_narrative_length"] * (n - 1) + len(narrative)) / n)
        ps["mode_distribution"]["unified"] = ps["mode_distribution"].get("unified", 0) + 1
        for r in holder.records:
            tool = r["name"]
            ps["tool_usage"][tool] = ps["tool_usage"].get(tool, 0) + 1

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
            "proactive_hint": ctx.get("_proactive_hint", ""),
            "token_usage": {
                "prompt": holder.prompt_tokens,
                "completion": holder.completion_tokens,
                "total": holder.prompt_tokens + holder.completion_tokens,
            },
            "agent_trace": trace,
        }

    async def _dispatch_unified_tool(self, ctx: dict, name: str, args: dict):
        """Dispatch tool calls for the unified agent."""
        # D1: undo_my_last_action — 自我回退
        if name == "undo_my_last_action":
            reason = args.get("reason", "")
            if hasattr(self, '_last_tool_result') and self._last_tool_result:
                undone = self._last_tool_result
                logger.info("[统一Agent] 自我回退: %s (原因: %s)", undone.get("name", ""), reason)
                if undone.get("name") == "add_choices":
                    self._reset_stage45_tool_buffers()
                self._last_tool_result = None
                return f"已撤销上一个工具调用（{undone.get('name', '')}）。原因：{reason}"
            return "没有可撤销的操作"

        # Plan tool
        if name == "submit_plan":
            plan = args.get("plan", "")
            ctx["_agent_plan"] = plan
            logger.info("[统一Agent] 计划: %s", plan[:100])
            return f"计划已记录：{plan}\n继续执行。如果用户通过 inject 发来修改意见，你会在下一轮收到。"

        # Info tools
        if name in ("recall_history", "query_lorebook", "query_npc_history",
                     "check_inventory", "get_npc_attitude"):
            return self._run_tool_native(name, args)

        # Adaptive: send_message
        if name == "send_message":
            return self._handle_send_message(args)

        # Action tools
        if name == "update_state":
            return self._handle_update_state(args)
        if name == "manage_npcs":
            return self._handle_manage_npcs(args)

        # Output tools
        if name == "finalize_turn":
            return self._handle_finalize_turn(ctx, args)
        if name == "delegate_settlement":
            return await self._handle_delegate_settlement(ctx, args)

        # --- #4 叙事图谱遍历 ---
        if name == "traverse_graph":
            entity = args.get("entity", "")
            depth = args.get("depth", 2)
            # 先按 ID 查，再按名字查
            eid = entity
            if entity not in self.narrative_graph.nodes:
                for nid, ndata in self.narrative_graph.nodes.items():
                    if ndata.get("name") == entity:
                        eid = nid
                        break
            result = self.narrative_graph.query(eid, depth)
            return json.dumps(result, ensure_ascii=False, default=str)

        # --- #6 规则咨询 ---
        if name == "consult_rules":
            return self._handle_consult_rules(args)

        # --- #8 事件预演 ---
        if name == "peek_upcoming_events":
            return self._handle_peek_upcoming_events(args)

        # --- C2 因果链追踪 ---
        if name == "trace_causality":
            entity = args.get("entity", "")
            direction = args.get("direction", "both")
            depth = args.get("depth", 3)
            # 先按 ID 查，再按名字查
            eid = entity
            if entity not in self.narrative_graph.nodes:
                for nid, ndata in self.narrative_graph.nodes.items():
                    if ndata.get("name") == entity:
                        eid = nid
                        break
            result = {}
            if direction in ("causes", "both"):
                result["causes"] = self.narrative_graph.trace_causes(eid, depth)
            if direction in ("effects", "both"):
                result["effects"] = self.narrative_graph.trace_effects(eid, depth)
            return json.dumps(result, ensure_ascii=False, default=str)

        # --- C3 分支预演 ---
        if name == "preview_choice_outcome":
            choice = args.get("choice_text", "")
            # 规则推测风险
            risk_words = {"冒险": "risky", "直接": "moderate", "等待": "safe", "观察": "safe",
                          "追": "risky", "逃": "moderate", "询问": "moderate", "忽略": "safe"}
            risk = "moderate"
            for word, r in risk_words.items():
                if word in choice:
                    risk = r
                    break
            # 检查是否涉及已知 NPC
            npcs_involved = []
            for nid, ndata in self.current_state.get("npcs", {}).items():
                if isinstance(ndata, dict) and ndata.get("name", "") in choice:
                    att = ndata.get("attitude_toward_player", 50)
                    npcs_involved.append(f"{ndata['name']}(态度{att})")
            # 检查因果图
            causal_hint = ""
            if hasattr(self, 'narrative_graph'):
                for node_id, node in self.narrative_graph.nodes.items():
                    if node.get("name", "") in choice:
                        effects = self.narrative_graph.trace_effects(node_id, 2)
                        if effects:
                            causal_hint = f"历史因果：{effects[0].get('effect_name', '')}可能受影响"
                        break
            return json.dumps({
                "predicted_risk": risk,
                "npcs_involved": npcs_involved,
                "causal_hint": causal_hint,
                "note": "规则预测，仅供参考"
            }, ensure_ascii=False)

        return json.dumps({"error": f"未知工具: {name}"})

    def _handle_finalize_turn(self, ctx: dict, args: dict) -> str:
        """Synchronous settlement: state + choices + summary in one call."""
        results = []

        # State changes
        state_args = {k: args[k] for k in ("end_time", "state_changes", "inventory_changes",
                      "activate_states", "location_change") if k in args}
        if state_args:
            results.append(self._handle_update_state(state_args))

        # NPC operations
        npc_ops = args.get("npc_operations", [])
        if npc_ops:
            results.append(self._handle_manage_npcs({"operations": npc_ops}))

        # Choices
        choices = args.get("choices", [])
        if choices:
            self._reset_stage45_tool_buffers()
            for ch in choices:
                self._run_choices_tool("add_choice", ch)

        # Summary and image
        self._turn_summary_override = args.get("summary", "")
        if args.get("image_prompt"):
            self._scene_image_prompt_override = {
                "prompt": args["image_prompt"],
                "style": args.get("image_style", "realistic"),
            }

        # D2: proactive_hint
        hint = args.get("proactive_hint", "")
        if hint:
            ctx["_proactive_hint"] = hint

        return "已同步完成本轮结算"

    async def _handle_delegate_settlement(self, ctx: dict, args: dict) -> str:
        """Delegate settlement to background agent asynchronously."""
        # Save choices and summary immediately
        choices = args.get("choices", [])
        if choices:
            self._reset_stage45_tool_buffers()
            for ch in choices:
                self._run_choices_tool("add_choice", ch)
        self._turn_summary_override = args.get("summary", "")

        # D2: proactive_hint
        proactive = args.get("proactive_hint", "")
        if proactive:
            ctx["_proactive_hint"] = proactive

        self._delegated_settlement = True

        # Launch async background settlement (fire-and-forget)
        hint = args.get("hint", "")
        asyncio.create_task(self._run_delegated_settlement_task(ctx, hint))

        return "已委托后台结算。叙事将立即返回。"

    async def _run_delegated_settlement_task(self, ctx: dict, hint: str):
        """Background async settlement task."""
        try:
            action_text = ctx.get("action_text", "")
            bg_ctx = {
                "action_text": action_text,
                "old_time": ctx.get("old_time", ""),
                "present_npc_ids": ctx.get("present_npc_ids", []),
            }
            bg_holder: _AgentResult | None = None
            async for event in self._run_background_agent(
                bg_ctx, ctx.get("_current_narrative", ""), "",
                {"text": action_text},
                streaming=False, skip_hints=hint,
            ):
                if event.get("type") == "_bg_result":
                    bg_holder = event["holder"]

            bg_calls = bg_holder.records if bg_holder else []
            logger.info("后台异步结算完成: %d 次工具调用", len(bg_calls))

            # D3: 向前台发消息
            settlement_summary = f"后台结算完成: {len(bg_calls)} 次工具调用"
            if not hasattr(self, '_settlement_messages'):
                self._settlement_messages = []
            self._settlement_messages.append({
                "type": "settlement_complete",
                "summary": settlement_summary,
                "tool_count": len(bg_calls),
                "timestamp": self.current_state.get("game_time", ""),
            })
        except Exception as e:
            logger.error("后台异步结算失败: %s", e)
            if not hasattr(self, '_settlement_messages'):
                self._settlement_messages = []
            self._settlement_messages.append({
                "type": "settlement_error",
                "summary": f"后台结算失败: {e}",
                "timestamp": self.current_state.get("game_time", ""),
            })

    def _handle_consult_rules(self, args: dict) -> str:
        """纯规则检查（不调 LLM）：NPC 在场、物品持有等。"""
        question = args.get("question", "").lower()
        context_str = args.get("context", "")

        answers = []

        # NPC 在场检查
        if "在场" in question or "在这" in question:
            for npc in self.script.get("npcs", []):
                if npc.get("name", "") in question:
                    loc = self._get_npc_location(npc["id"])
                    player_loc = self.current_state.get("player", {}).get("location", "")
                    is_present = loc == player_loc
                    answers.append(f"{npc['name']} {'在场' if is_present else '不在场'}（当前位置：{loc}）")

        # 物品检查
        if "可用" in question or "有没有" in question:
            inv = self.current_state.get("inventory", [])
            for item in inv:
                item_name = item.get("item", "") if isinstance(item, dict) else str(item)
                if item_name and item_name in question:
                    answers.append(f"持有 {item_name}")

        if not answers:
            answers.append("无法确定，请根据叙事自行判断")

        return json.dumps({"answers": answers}, ensure_ascii=False)

    def _handle_peek_upcoming_events(self, args: dict) -> str:
        """预览即将触发的事件（hint 级别，不剧透）。"""
        hours = min(args.get("hours_ahead", 3), 6)
        game_time = self.current_state.get("game_time", "")
        events = []

        if game_time:
            try:
                from datetime import datetime, timedelta
                current = datetime.fromisoformat(game_time.replace("Z", "+00:00"))
                deadline = current + timedelta(hours=hours)

                # 检查 one_time_events
                for evt in self.script.get("one_time_events", []):
                    trigger = evt.get("trigger_time", "")
                    if not trigger:
                        continue
                    try:
                        trigger_dt = datetime.fromisoformat(trigger.replace("Z", "+00:00"))
                        if current < trigger_dt <= deadline:
                            events.append({
                                "name": evt.get("name", ""),
                                "hint": evt.get("description", "")[:100],
                                "time": trigger,
                            })
                    except Exception:
                        pass

                # 检查 cyclic_events
                for evt in self.script.get("cyclic_events", []):
                    next_trigger = evt.get("first_trigger", "")
                    if not next_trigger:
                        continue
                    try:
                        trigger_dt = datetime.fromisoformat(next_trigger.replace("Z", "+00:00"))
                        if current < trigger_dt <= deadline:
                            events.append({
                                "name": evt.get("name", ""),
                                "hint": f"循环事件（{evt.get('frequency_unit', '')}）",
                                "time": next_trigger,
                            })
                    except Exception:
                        pass
            except Exception:
                pass

        if not events:
            return json.dumps({"events": [], "note": "未来几小时无重大事件"}, ensure_ascii=False)
        return json.dumps({"events": events[:5]}, ensure_ascii=False)

    # ================================================================
    # Intent shortcuts (rewrite / query) — no turn advancement
    # ================================================================

    async def _agent_rewrite(self, feedback: str) -> dict:
        """Rewrite last turn narrative without advancing the turn."""
        if not self.world_tree:
            return {"error": "无可重写的回合"}
        last_node = self.world_tree.get_node(self.world_tree.active_node_id)
        if not last_node:
            return {"error": "无可重写的回合"}

        original = last_node.get("ai_response", "")
        action_raw = last_node.get("player_action", {})
        action_text = action_raw.get("text", "") if isinstance(action_raw, dict) else str(action_raw or "")

        from engine.prompt_loader import PromptLoader
        system = PromptLoader.get().render_system(
            "agentic_unified",
            world_background=(self.script.get("world_background", "") or "")[:500],
            player_name=self.current_state.get("player", {}).get("name", ""),
        )
        system += (
            f"\n\n## 重写模式\n"
            f"用户对上一版叙事不满意。反馈：{feedback}\n"
            "保持相同事件走向，改善用户指出的问题。只输出新版叙事文本。"
        )

        user = f"原版叙事（需修改）：\n{original[:2000]}\n\n玩家行动：{action_text}"

        # Rewrite uses info tools only
        from engine.game_session import _unified_tools
        all_tools = _unified_tools()
        info_tools = [t for t in all_tools if t["function"]["name"] in
                      ("recall_history", "query_lorebook", "query_npc_history", "check_inventory")]

        holder = _AgentResult()
        async for _ in self._agent_loop(
            [{"role": "user", "content": user}], system, info_tools,
            lambda n, a: self._run_tool_native(n, a),
            max_rounds=5, label="重写Agent", result_holder=holder,
        ):
            pass

        new_narrative = holder.text
        if not new_narrative:
            return {"error": "重写失败"}

        last_node["ai_response"] = new_narrative

        state = self.current_state
        return {
            "ok": True,
            "narrative": new_narrative,
            "rewrite": True,
            "choices": last_node.get("choices_presented", []),
            "state": self._slim_snapshot(state),
            "node_id": self.world_tree.active_node_id if self.world_tree else "",
            "game_time": state.get("game_time", ""),
            "dice_rolls": [],
            "warnings": [],
        }

    async def _agent_query(self, query: str) -> dict:
        """Answer a player query without advancing the turn."""
        system = "你是游戏助手。回答玩家的查询，简洁明了。可以使用工具获取信息。"
        user = f"查询：{query}"

        from engine.game_session import _unified_tools
        all_tools = _unified_tools()
        info_tools = [t for t in all_tools if t["function"]["name"] in
                      ("recall_history", "query_lorebook", "check_inventory", "get_npc_attitude")]

        holder = _AgentResult()
        async for _ in self._agent_loop(
            [{"role": "user", "content": user}], system, info_tools,
            lambda n, a: self._run_tool_native(n, a),
            max_rounds=3, label="查询Agent", result_holder=holder,
        ):
            pass

        return {"ok": True, "reply": holder.text, "query": True}
