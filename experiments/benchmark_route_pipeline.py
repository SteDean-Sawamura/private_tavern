"""
实验：当前 8-Stage Pipeline vs Route+瘦身 Pipeline A/B 对比
===========================================================
对比维度：
  1. 总延迟（端到端 wall-clock time）
  2. 各阶段 AI 调用延迟 / prompt 大小 / 输出大小
  3. 各阶段生成内容全文（保存到 JSON 供人工比较）
  4. Stage 1 骨架质量 / Stage 4b 字段数
  5. 最终叙事质量 / 选项质量

用法：
  cd 酒馆目录
  python -m experiments.benchmark_route_pipeline
"""

import asyncio
import json
import copy
import time
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.game_session import GameSession, _extract_reasoning, strip_think_tags
from engine.prompt_builder import PromptBuilder
from ai.response_parser import ResponseParser


# ──────────────────────────────────────────────
#  Stage-Aware Instrumented AI Provider
# ──────────────────────────────────────────────

class StageAwareProvider:
    """Wraps a real AIProvider, recording per-stage latency, prompt size, and output."""

    def __init__(self, real_provider):
        self._real = real_provider
        self.calls: list[dict] = []
        self.current_stage = "unknown"

    def reset(self):
        self.calls = []

    def _input_chars(self, messages, system):
        total = len(system or "")
        for m in messages:
            c = m.get("content", "")
            total += len(c) if isinstance(c, str) else len(str(c))
        return total

    async def generate(self, messages, system="", **kwargs):
        stage = self.current_stage
        input_chars = self._input_chars(messages, system)
        t0 = time.perf_counter()
        result = await self._real.generate(messages, system=system, **kwargs)
        elapsed = time.perf_counter() - t0
        self.calls.append({
            "stage": stage,
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": len(result or ""),
            "output_text": (result or "")[:3000],
            "system_chars": len(system or ""),
        })
        return result

    async def generate_stream(self, messages, system="", raw=False, **kwargs):
        stage = self.current_stage
        input_chars = self._input_chars(messages, system)
        t0 = time.perf_counter()
        chunks = []
        async for chunk in self._real.generate_stream(messages, system=system, raw=raw, **kwargs):
            chunks.append(chunk)
            yield chunk
        elapsed = time.perf_counter() - t0
        full = "".join(chunks)
        self.calls.append({
            "stage": stage,
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": len(full),
            "output_text": full[:3000],
            "system_chars": len(system or ""),
        })

    async def generate_with_tools(self, messages, system="", tools=None, **kwargs):
        stage = self.current_stage
        input_chars = self._input_chars(messages, system)
        t0 = time.perf_counter()
        result = await self._real.generate_with_tools(messages, system=system, tools=tools, **kwargs)
        elapsed = time.perf_counter() - t0
        out_chars = len(result.get("content", "")) if isinstance(result, dict) else len(str(result))
        self.calls.append({
            "stage": stage,
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": out_chars,
            "output_text": (result.get("content", "") if isinstance(result, dict) else str(result))[:3000],
            "system_chars": len(system or ""),
        })
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


# ──────────────────────────────────────────────
#  Group A: Current 8-Stage (via process_action)
# ──────────────────────────────────────────────

# Stage labeling via output content heuristics (more reliable than call order)
def _guess_stage_from_output(call: dict) -> str:
    """Guess the pipeline stage from the output content."""
    out = call.get("output_text", "")
    sys_chars = call.get("system_chars", 0)
    out_len = call.get("output_chars", 0)

    if out_len == 0:
        if sys_chars < 100:
            return "Misc"
        return "Stage_empty"

    # JSON outputs
    if out.lstrip().startswith("{"):
        if '"npc_attitude_changes"' in out or '"npc_met_changes"' in out:
            return "Stage4a_npc"
        if '"state_changes"' in out or '"time_advance"' in out or '"narrative_thread"' in out:
            return "Stage4b_world"
        if '"choices"' in out:
            return "Stage5_choices"
        if '"emotion"' in out or '"moral_alignment"' in out:
            return "Emotion"
        return "JSON_unknown"

    # Structured skeleton markers
    if "行动结果" in out or "关键事件" in out or "NPC决策" in out or "剧情推进" in out or "剧情骨架" in out:
        return "Stage1_plot"

    # Long narrative text (Stage 3 output)
    if out_len > 600 and sys_chars > 400:
        return "Stage3_narrative"

    # Shorter descriptive text
    if out_len > 100 and out_len < 700:
        if "感官" in out[:50] or "空气" in out[:100] or "光" in out[:80] or "味" in out[:80]:
            return "Stage2a_env"
        return "Stage2b_char"

    return "Unknown"


async def run_current_pipeline(session: GameSession, action: dict) -> dict:
    """Run current 8-stage pipeline."""
    return await session.process_action(action)


def label_calls_heuristic(calls: list[dict]):
    """Label calls using content heuristics instead of fixed order."""
    for c in calls:
        if c["stage"] == "unknown":
            c["stage"] = _guess_stage_from_output(c)


# ──────────────────────────────────────────────
#  Group B: Route + Slim Pipeline
# ──────────────────────────────────────────────

# Stage 4b field definitions mapped to required system tags
FIELD_DEFS = {
    "state_changes": {
        "system": None,
        "desc": '- state_changes: [{"target":"player.属性名","op":"add","value":数值,"reason":"原因"}] 也可修改NPC属性: target="npcs.{npc_id}.字段名" op="set"',
    },
    "time_advance": {
        "system": None,
        "desc": '- time_advance: ISO 8601时段（PT30M=30分钟, PT2H=2小时, P1D=1天）。不要包含location_change的travel_time——引擎会自动追加移动耗时',
    },
    "activate_states": {
        "system": None,
        "desc": '- activate_states: [{"id":"状态ID","name":"显示名称","description":"一句话描述"}] 预定义状态可只填ID字符串',
    },
    "deactivate_states": {
        "system": None,
        "desc": '- deactivate_states: [状态ID]',
    },
    "world_property_changes": {
        "system": None,
        "desc": '- world_property_changes: [{"id":"属性ID","value":"新值"}]',
    },
    "add_consequences": {
        "system": None,
        "desc": '- add_consequences: [{"id":"唯一ID","description":"延迟后果描述","trigger_chance":0.3,"turns_delay":2,"max_turns":5}]',
    },
    "scene_details": {
        "system": None,
        "desc": '- scene_details: {"atmosphere":"","sensory":"","key_objects":[]}',
    },
    "narrative_thread_updates": {
        "system": None,
        "desc": '- narrative_thread_updates: [{"id":"唯一ID","name":"剧情线名称","description":"当前进展一句话","status":"active|dormant|resolved"}] 当叙事中开启、推进或结束一条剧情线时更新。新线程必须有name',
    },
    "invalidate_lore": {
        "system": None,
        "desc": '- invalidate_lore: ["词条ID"]',
    },
    "location_change": {
        "system": "location",
        "desc": '- location_change: 新位置ID或省略',
    },
    "reveal_locations": {
        "system": "location",
        "desc": '- reveal_locations: [{"id":"位置ID","name":"显示名称"}]',
    },
    "inventory_changes": {
        "system": "inventory",
        "desc": '- inventory_changes: [{"item":"物品名","action":"add|remove","quantity":1,"description":"可选简述"}]',
    },
    "faction_reputation_changes": {
        "system": "faction",
        "desc": '- faction_reputation_changes: [{"faction_id":"组织ID","change":±数值,"reason":"原因"}] 玩家与组织声望变化(±1到±15)',
    },
    "moral_alignment_changes": {
        "system": "moral",
        "desc": '- moral_alignment_changes: [{"axis":"mercy_vs_cruelty|honesty_vs_deception|order_vs_chaos","change":±数值,"reason":"原因"}] 本回合玩家行为的道德维度影响(±1到±15)',
    },
    "npc_location_changes": {
        "system": "npc",
        "desc": '- npc_location_changes: [{"npc_id":"","new_location":"位置ID","reason":"离开/到达原因"}] 本回合在场NPC的位置变动',
    },
    "offscreen_npc_updates": {
        "system": "npc",
        "desc": '- offscreen_npc_updates: [{"npc_id":"","action":"简述行动","location":"当前位置"}] 2-3个最相关离场NPC',
    },
    "new_npcs": {
        "system": "new_npc",
        "desc": '- new_npcs: [{"id":"","name":"","title":"职位/头衔","bio":"一句话简介","personality":"","location":"必填"}]',
    },
    "game_over": {
        "system": "game_over",
        "desc": '- game_over: {"reason":"","ending_type":""} 或省略',
    },
    "add_deadlines": {
        "system": "deadline",
        "desc": '- add_deadlines: [{"id":"唯一ID","description":"限时描述","turns_remaining":3,"on_expire":{"description":"超时后果","state_changes":[...]},"on_complete":{"condition":"达成条件","description":"成功描述"}}] 仅在叙事中出现明确的时间压力时创建',
    },
    "discovered_clues": {
        "system": "clue",
        "desc": '- discovered_clues: [{"id":"唯一ID","text":"线索内容（一句话）","category":"人物|地点|事件|物品|动机","source":"来源说明"}] 叙事中出现的关键信息、证据、蛛丝马迹。只提取真正有调查价值的信息，不要记录日常琐事',
    },
    "recruit_companions": {
        "system": "companion",
        "desc": '- recruit_companions: ["npc_id"] 当NPC明确表示愿意加入队伍/跟随玩家时添加',
    },
    "dismiss_companions": {
        "system": "companion",
        "desc": '- dismiss_companions: ["npc_id"] 当同伴离队时添加',
    },
}


def build_pruned_4b_system(active_systems: list[str]) -> str:
    """Build a pruned Stage 4b system prompt based on active systems."""
    active_set = set(active_systems)
    field_lines = []
    for fid, fdef in FIELD_DEFS.items():
        if fdef["system"] is None or fdef["system"] in active_set:
            field_lines.append(fdef["desc"])

    return (
        "你是游戏状态引擎。严格根据叙事中实际描写的事件推演本回合世界状态变更。\n"
        "不要推测或编造叙事中未提及的情节。只返回紧凑JSON。\n\n"
        "字段：\n" + "\n".join(field_lines) + "\n\n"
        "大成功时可额外给予奖励（物品/属性提升/开启新区域）；大失败时应施加惩罚（属性下降/丢失物品/添加负面状态）。\n"
        "省略无变化的字段。紧凑JSON输出。"
    )


def _parse_route(raw: str) -> dict:
    """Parse route JSON from AI output, handling think tags and markdown."""
    raw = strip_think_tags(raw or "").strip()
    # Strip markdown code blocks
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
    return {"scene_type": "unknown", "systems": [], "scope": "moderate", "focus_npcs": []}


# Slim Stage 1 system prompt override — more structured, less narrative
SLIM_STAGE1_SYSTEM = (
    "你是游戏剧情决策器。根据玩家行动和当前局势，决定本回合的剧情走向。\n\n"
    "严格按以下格式输出，不要文学描写，不要修辞，不要比喻：\n\n"
    "[行动结果] 一句话：成功/失败，程度如何\n"
    "[关键事件] 用短句列举发生了什么（每条≤20字）\n"
    "[NPC决策] 每个在场NPC的关键决定和原因（每人一行，≤30字）\n"
    "[剧情推进] 一句话：当前局面走向哪里？悬念是什么？\n\n"
    "规则：\n"
    "- 总字数100-200字，用电报式短句\n"
    "- 只陈述事实，不要氛围描写\n"
    "- 尊重检定/骰子结果\n"
    "- 尊重NPC性格和态度\n"
    "- 信息边界：只用主角能知道的信息\n"
    "- 只用已定义NPC，不捏造新角色（路人用泛指）\n"
    "- 不替主角做出未明确指定的新行动\n"
    "- 忠实执行玩家行动的每个步骤"
)

# Stage 3 anti-AI-flavor system prompt addition
ANTI_AI_FLAVOR_HINT = (
    "\n\n写作风格要求：\n"
    "- 写得像人话，不像AI。禁止使用以下AI常见套路："
    "「仿佛」「宛如」「恰如其分」「不禁」「某种说不清的」「在这一刻」「空气中弥漫着」"
    "「目光中闪过一丝」「嘴角微微上扬」「心中涌起一股」\n"
    "- 少用排比、对仗、感叹号\n"
    "- 多用短句，少用长从句。叙事节奏要有松有紧\n"
    "- 对话要口语化，每个NPC说话风格不同\n"
    "- 感官描写具体而克制，一两处点到为止，不要每段都堆叠\n"
    "- 关注动作和对话推进剧情，而非内心独白和环境铺陈"
)


async def run_route_pipeline(session: GameSession, provider: StageAwareProvider, action: dict) -> dict:
    """Run the Route + Slim pipeline experiment."""
    ctx = session._prepare_turn(action)

    try:
        ctx["history_context"], ctx["lore_ids_from_rag"] = await session._enrich_with_rag(
            action.get("text", ""), ctx["recent_nodes"],
            ctx.get("activated_lore", []), ctx.get("history_context", ""),
        )
    except Exception as e:
        print(f"    [warn] RAG enrichment failed: {e}")
        ctx.setdefault("history_context", "")
        ctx.setdefault("lore_ids_from_rag", set())

    # ─── Route Stage ───
    provider.current_stage = "Route"
    player = session.current_state.get("player", {})
    location_id = player.get("location", "")
    location_name = session.prompt_builder._resolve_location_name(location_id)
    present_npc_ids = ctx.get("present_npc_ids", [])
    npc_states = session.current_state.get("npcs", {})
    npc_names = ", ".join(
        npc_states.get(nid, {}).get("name", nid) if isinstance(npc_states.get(nid), dict) else nid
        for nid in present_npc_ids[:8]
    )
    action_text = action.get("text", "")

    route_system = "你是场景分析器。根据玩家行动判断涉及的游戏系统。只返回紧凑JSON，不要解释。"
    route_user = (
        f"位置: {location_name}\n"
        f"在场NPC: {npc_names or '无'}\n"
        f"玩家行动: {action_text}\n\n"
        '返回: {"scene_type":"social|combat|exploration|trade|travel|rest",'
        '"systems":["从npc/location/inventory/faction/moral/companion/deadline/clue/quest/combat/new_npc中选择相关的"],'
        '"scope":"minor|moderate|major",'
        '"focus_npcs":["最相关npc_id"]}'
    )
    raw_route = await provider.generate(
        [{"role": "user", "content": route_user}],
        system=route_system,
        max_tokens=256,
        **session._stage_kwargs("knowledge_graph"),
    )
    route = _parse_route(raw_route)
    print(f"    Route: type={route.get('scene_type','?')} scope={route.get('scope','?')} systems={route.get('systems',[])} focus={route.get('focus_npcs',[])}")

    # ─── Stage 1: Slim Plot Decision ───
    provider.current_stage = "Stage1_slim"
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

    # Slim: no history_context, no recent_reasoning, focus_npcs only
    focus_npcs = route.get("focus_npcs", present_npc_ids)
    plot_msgs, _ = session.prompt_builder.build_plot_decision_prompt(
        action_text,
        session.current_state,
        check_result=ctx.get("check_result"),
        dice_results=ctx.get("dice_dicts"),
        triggered_events=ctx.get("triggered_events"),
        triggered_consequences=ctx.get("triggered_consequences"),
        achieved_milestones=ctx.get("achieved_milestones"),
        present_npc_ids=focus_npcs if focus_npcs else present_npc_ids,
        history_context="",
        recent_reasoning=None,
        prev_narrative_tail=_prev_tail,
        prev_plot_decision=_prev_plot,
        recent_narratives=_recent_narratives or None,
    )

    # Override system prompt with our stricter skeleton format
    raw_plot = await provider.generate(
        plot_msgs, system=SLIM_STAGE1_SYSTEM, max_tokens=8192, **session._stage_kwargs("narrative")
    )
    plot_reasoning = _extract_reasoning(raw_plot)
    plot_decision = strip_think_tags(raw_plot)
    ctx["plot_reasoning"] = plot_reasoning
    ctx["plot_decision"] = plot_decision

    # ─── Stage 2: Adaptive ───
    scope = route.get("scope", "moderate")
    if scope == "minor":
        provider.current_stage = "Stage2_merged"
        merged_sys = (
            "你是文字游戏的场景编导。根据剧情骨架，生成两部分内容：\n"
            "第一部分：环境和氛围描写（200字以内，覆盖2-3种感官）\n"
            "第二部分：角色对话和行为（300字以内，第二人称主角，第三人称NPC）\n\n"
            "用 --- 分隔两部分。用中文弯引号。不得捏造新角色。"
        )
        loc_def = session.prompt_builder._location_by_id.get(location_id, {})
        loc_desc = loc_def.get("description", "")
        game_time = session.current_state.get("game_time", "")

        npc_brief = ""
        for nid in present_npc_ids[:4]:
            ns = npc_states.get(nid, {})
            if isinstance(ns, dict):
                npc_brief += f"- {ns.get('name', nid)}（态度{ns.get('attitude_toward_player', 50)}）\n"

        merged_user = (
            f"位置: {location_name}" + (f" - {loc_desc[:60]}" if loc_desc else "") + "\n"
            f"时间: {game_time or '未知'}\n"
            f"在场NPC:\n{npc_brief}"
            f"\n剧情骨架:\n{plot_decision}"
        )
        raw_merged = await provider.generate(
            [{"role": "user", "content": merged_user}],
            system=merged_sys, max_tokens=8192, **session._stage_kwargs("narrative")
        )
        raw_merged = strip_think_tags(raw_merged) if raw_merged else ""
        if "---" in raw_merged:
            parts = raw_merged.split("---", 1)
            env_text = parts[0].strip()
            char_text = parts[1].strip()
        else:
            half = len(raw_merged) // 3
            env_text = raw_merged[:half]
            char_text = raw_merged[half:]
    else:
        provider.current_stage = "Stage2a"
        env_msgs, env_sys = session.prompt_builder.build_env_render_prompt(
            plot_decision, session.current_state
        )
        provider.current_stage = "Stage2b"
        char_msgs, char_sys = session.prompt_builder.build_character_action_prompt(
            plot_decision, session.current_state,
            present_npc_ids=present_npc_ids,
            dice_results=ctx.get("dice_dicts"),
            check_result=ctx.get("check_result"),
            triggered_events=ctx.get("triggered_events"),
            triggered_consequences=ctx.get("triggered_consequences"),
        )
        async def _gen_env():
            provider.current_stage = "Stage2a"
            return await provider.generate(env_msgs, system=env_sys, max_tokens=8192, **session._stage_kwargs("narrative"))
        async def _gen_char():
            provider.current_stage = "Stage2b"
            return await provider.generate(char_msgs, system=char_sys, max_tokens=8192, **session._stage_kwargs("narrative"))
        raw_env, raw_char = await asyncio.gather(_gen_env(), _gen_char(), return_exceptions=True)
        env_text = strip_think_tags(raw_env) if isinstance(raw_env, str) and raw_env else ""
        char_text = strip_think_tags(raw_char) if isinstance(raw_char, str) and raw_char else ""

    # ─── Stage 3: Narrative Compose (with anti-AI-flavor) ───
    provider.current_stage = "Stage3"
    recent_openings = []
    for node in ctx.get("recent_nodes", [])[-3:]:
        resp = node.get("ai_response", "")
        if resp:
            recent_openings.append(resp[:20])

    compose_history = ctx.get("history_context", "")
    cm = ctx.get("context_memory", "")
    if cm:
        compose_history = (compose_history + "\n\n" + cm).strip() if compose_history else cm

    compose_msgs, compose_sys = session.prompt_builder.build_narrative_compose_prompt(
        plot_decision, env_text, char_text, session.current_state, recent_openings,
        missing_env=not env_text, missing_char=not char_text,
        history_context=compose_history,
        prev_narrative_tail=_prev_tail,
    )
    compose_msgs = session.prompt_builder.inject_depth_lore(compose_msgs, ctx["activated_lore"])

    # Inject anti-AI-flavor hint into system prompt
    compose_sys_enhanced = compose_sys + ANTI_AI_FLAVOR_HINT

    raw_narrative = await provider.generate(
        compose_msgs, system=compose_sys_enhanced, **session._stage_kwargs("narrative")
    )
    narrative = strip_think_tags(raw_narrative)

    # ─── Stage 4a: NPC Reaction (unchanged) ───
    provider.current_stage = "Stage4a"
    npc_msgs, npc_sys = session.prompt_builder.build_npc_reaction_prompt(
        narrative, action_text, session.current_state,
        check_result=ctx.get("check_result"),
        present_npc_ids=present_npc_ids,
    )

    # ─── Stage 4b: Pruned World State ───
    provider.current_stage = "Stage4b_pruned"
    active_systems = route.get("systems", [])
    pruned_sys = build_pruned_4b_system(active_systems)
    sctx = session.prompt_builder._collect_state_context(session.current_state)
    attrs_text = "\n".join(sctx["attrs_lines"])

    active_ids = sctx["active_ids"]
    ps_lines = []
    for ps in session.script.get("persistent_states", []):
        status = "激活" if ps["id"] in active_ids else "未激活"
        ps_lines.append(f"- {ps.get('name', ps['id'])}({ps['id']}): {status}")
    ps_text = "\n".join(ps_lines) if ps_lines else "无"

    visible_ids = set(session.current_state.get("visible_locations", []))
    loc_lines = [f"- {loc.get('name', loc['id'])}({loc['id']})" for loc in session.script.get("locations", []) if loc["id"] in visible_ids]
    loc_text = "\n".join(loc_lines) if loc_lines else "无"

    loc_line = f"{sctx['location']}({sctx['location_id']})"
    if sctx["location_desc"]:
        loc_line += f" — {sctx['location_desc']}"

    world_content = f"""角色属性（含范围）:
{attrs_text}

当前位置: {loc_line}

已知地点:
{loc_text}

持续状态:
{ps_text}

背包: {sctx['inv_compact']}

玩家行动: {action_text}

叙事内容:
{narrative}"""

    if ctx.get("check_result") and ctx["check_result"].get("outcome") in ("critical_success", "critical_failure"):
        crit_label = "大成功" if ctx["check_result"]["outcome"] == "critical_success" else "大失败"
        world_content += f"\n\n[检定结果: {crit_label}] 请在状态变更中体现持续影响。"

    world_msgs_pruned = [{"role": "user", "content": world_content}]

    # Run 4a and 4b in parallel
    async def _gen_npc():
        provider.current_stage = "Stage4a"
        return await provider.generate(npc_msgs, system=npc_sys, max_tokens=8192, **session._stage_kwargs("state"))
    async def _gen_world():
        provider.current_stage = "Stage4b_pruned"
        return await provider.generate(world_msgs_pruned, system=pruned_sys, max_tokens=8192, **session._stage_kwargs("state"))
    raw_npc, raw_world = await asyncio.gather(_gen_npc(), _gen_world(), return_exceptions=True)

    raw_npc = strip_think_tags(raw_npc) if isinstance(raw_npc, str) and raw_npc else ""
    raw_world = strip_think_tags(raw_world) if isinstance(raw_world, str) and raw_world else ""

    parsed = session.response_parser.parse_split_v2(narrative, raw_npc, raw_world)

    # ─── Stage 5: Choices (unchanged) ───
    provider.current_stage = "Stage5"
    new_scene = parsed.get("scene_details")
    if new_scene and isinstance(new_scene, dict):
        session.current_state["scene_details"] = new_scene

    choices_msgs, choices_sys = session.prompt_builder.build_choices_prompt(
        narrative, action_text, session.current_state,
        turn_number=session.turn_number, activated_lore=ctx["activated_lore"])
    raw_choices = await provider.generate(
        choices_msgs, system=choices_sys, max_tokens=8192, **session._stage_kwargs("choices")
    )
    raw_choices = strip_think_tags(raw_choices) if raw_choices else ""
    parsed["choices"] = session.response_parser.parse_choices(raw_choices)

    if not parsed.get("choices"):
        parsed["choices"] = session._generate_context_choices()

    result = await session._apply_parsed_response(parsed, narrative, action, ctx)
    result["_route"] = route
    return result


# ──────────────────────────────────────────────
#  Report & Formatting
# ──────────────────────────────────────────────

def build_report(label: str, calls: list[dict], result: dict, wall_time: float) -> dict:
    narrative = result.get("narrative", "")
    choices = result.get("choices", [])
    state_changes = result.get("state_changes", [])

    stage_summary = {}
    for c in calls:
        s = c["stage"]
        if s not in stage_summary:
            stage_summary[s] = {"latency": 0, "input_chars": 0, "output_chars": 0, "system_chars": 0, "calls": 0, "outputs": []}
        stage_summary[s]["latency"] += c["latency"]
        stage_summary[s]["input_chars"] += c["input_chars"]
        stage_summary[s]["output_chars"] += c["output_chars"]
        stage_summary[s]["system_chars"] += c.get("system_chars", 0)
        stage_summary[s]["calls"] += 1
        stage_summary[s]["outputs"].append(c.get("output_text", "")[:1500])

    return {
        "mode": label,
        "wall_time_s": round(wall_time, 2),
        "ai_calls": len(calls),
        "total_ai_latency_s": round(sum(c["latency"] for c in calls), 2),
        "total_input_chars": sum(c["input_chars"] for c in calls),
        "total_output_chars": sum(c["output_chars"] for c in calls),
        "narrative_length": len(narrative),
        "narrative_text": narrative[:2000],
        "choices_count": len(choices),
        "choices_text": json.dumps(choices, ensure_ascii=False)[:1000],
        "state_changes_count": len(state_changes),
        "route": result.get("_route"),
        "stage_summary": stage_summary,
    }


def print_comparison(label: str, report_a: dict, report_b: dict):
    print(f"\n{'=' * 70}")
    print(f"  场景: {label}")
    print(f"{'=' * 70}")

    for tag, r in [("A (当前 8-Stage)", report_a), ("B (Route+瘦身)", report_b)]:
        print(f"\n-- Group {tag} --")
        if r.get("route"):
            rt = r["route"]
            print(f"  Route: type={rt.get('scene_type','?')} scope={rt.get('scope','?')} systems={rt.get('systems',[])} focus={rt.get('focus_npcs',[])}")

        for stage_name, sd in r.get("stage_summary", {}).items():
            out_preview = sd["outputs"][0][:80].replace("\n", " ") if sd["outputs"] else ""
            print(f"  {stage_name:<18s} {sd['latency']:6.2f}s  sys:{sd['system_chars']:>5,}  in:{sd['input_chars']:>6,}  out:{sd['output_chars']:>5,}  | {out_preview}...")

        print(f"  ---")
        print(f"  Total: {r['wall_time_s']:.2f}s  calls:{r['ai_calls']}  narrative:{r['narrative_length']}ch  choices:{r['choices_count']}  state:{r['state_changes_count']}")
        print(f"  Input:{r['total_input_chars']:,}ch  Output:{r['total_output_chars']:,}ch")

    # Comparison
    print(f"\n-- Delta --")
    delta_time = report_b["wall_time_s"] - report_a["wall_time_s"]
    pct_time = (delta_time / report_a["wall_time_s"] * 100) if report_a["wall_time_s"] else 0
    delta_input = report_b["total_input_chars"] - report_a["total_input_chars"]
    pct_input = (delta_input / report_a["total_input_chars"] * 100) if report_a["total_input_chars"] else 0

    s1a = report_a.get("stage_summary", {}).get("Stage1_plot", {})
    s1b_key = "Stage1_slim" if "Stage1_slim" in report_b.get("stage_summary", {}) else "Stage1_plot"
    s1b = report_b.get("stage_summary", {}).get(s1b_key, {})
    s1_delta = s1b.get("input_chars", 0) - s1a.get("input_chars", 0)
    s1_pct = (s1_delta / s1a.get("input_chars", 1) * 100) if s1a.get("input_chars") else 0

    s4ba = report_a.get("stage_summary", {}).get("Stage4b_world", {})
    s4bb_key = "Stage4b_pruned" if "Stage4b_pruned" in report_b.get("stage_summary", {}) else "Stage4b_world"
    s4bb = report_b.get("stage_summary", {}).get(s4bb_key, {})
    s4b_sys_delta = s4bb.get("system_chars", 0) - s4ba.get("system_chars", 0)

    print(f"  Time:       {report_a['wall_time_s']:.1f}s -> {report_b['wall_time_s']:.1f}s ({pct_time:+.1f}%)")
    print(f"  Input:      {report_a['total_input_chars']:,} -> {report_b['total_input_chars']:,} ({pct_input:+.1f}%)")
    print(f"  Stage1 in:  {s1a.get('input_chars',0):,} -> {s1b.get('input_chars',0):,} ({s1_pct:+.1f}%)")
    print(f"  4b System:  {s4ba.get('system_chars',0):,} -> {s4bb.get('system_chars',0):,} ({s4b_sys_delta:+,})")
    print(f"  Narrative:  {report_a['narrative_length']} -> {report_b['narrative_length']}")
    print(f"  AI calls:   {report_a['ai_calls']} -> {report_b['ai_calls']}")


def generate_md_report(all_results: list[dict], output_path: str):
    """Generate a markdown report file."""
    lines = ["# AI Pipeline A/B 实验报告（多轮测试）", ""]
    lines.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    for i, entry in enumerate(all_results):
        lines.append(f"## 场景 {i+1}: {entry.get('label', '')} — {entry.get('action', '')[:40]}")
        lines.append("")

        for turn_data in entry.get("turns", []):
            turn_num = turn_data.get("turn", 1)
            lines.append(f"### 回合 {turn_num}")
            lines.append("")

            for tag, r in [("Group A (当前 8-Stage)", turn_data.get("group_a", {})),
                          ("Group B (Route+瘦身)", turn_data.get("group_b", {}))]:
                if not r or "error" in r:
                    lines.append(f"#### {tag}")
                    lines.append(f"**错误**: {r.get('error', '无数据')}")
                    lines.append("")
                    continue

                lines.append(f"#### {tag}")
                if r.get("route"):
                    rt = r["route"]
                    lines.append(f"**Route**: type={rt.get('scene_type','?')}, scope={rt.get('scope','?')}, systems={rt.get('systems',[])}")
                lines.append("")
                lines.append(f"| 指标 | 值 |")
                lines.append(f"|------|------|")
                lines.append(f"| 总耗时 | {r.get('wall_time_s', 0):.2f}s |")
                lines.append(f"| AI 调用次数 | {r.get('ai_calls', 0)} |")
                lines.append(f"| 总输入字符 | {r.get('total_input_chars', 0):,} |")
                lines.append(f"| 总输出字符 | {r.get('total_output_chars', 0):,} |")
                lines.append(f"| 叙事长度 | {r.get('narrative_length', 0)} |")
                lines.append(f"| 选项数 | {r.get('choices_count', 0)} |")
                lines.append(f"| 状态变更 | {r.get('state_changes_count', 0)} |")
                lines.append("")

                lines.append("##### 各阶段明细")
                lines.append("| Stage | 延迟 | System | Input | Output |")
                lines.append("|-------|------|--------|-------|--------|")
                for sname, sd in r.get("stage_summary", {}).items():
                    lines.append(f"| {sname} | {sd['latency']:.2f}s | {sd['system_chars']:,} | {sd['input_chars']:,} | {sd['output_chars']:,} |")
                lines.append("")

                # Key stage outputs
                for sname, sd in r.get("stage_summary", {}).items():
                    if sd["outputs"] and sd["output_chars"] > 0:
                        out = sd["outputs"][0][:500].strip()
                        lines.append(f"<details><summary>{sname} 输出 ({sd['output_chars']}字符)</summary>")
                        lines.append("")
                        lines.append("```")
                        lines.append(out)
                        lines.append("```")
                        lines.append("</details>")
                        lines.append("")

            # Comparison for this turn
            ra = turn_data.get("group_a", {})
            rb = turn_data.get("group_b", {})
            if ra and rb and "error" not in ra and "error" not in rb:
                lines.append("##### 对比")
                delta_time = rb.get("wall_time_s", 0) - ra.get("wall_time_s", 0)
                pct_time = (delta_time / ra["wall_time_s"] * 100) if ra.get("wall_time_s") else 0
                delta_input = rb.get("total_input_chars", 0) - ra.get("total_input_chars", 0)
                pct_input = (delta_input / ra["total_input_chars"] * 100) if ra.get("total_input_chars") else 0
                lines.append(f"- 时延变化: {pct_time:+.1f}%")
                lines.append(f"- 总输入变化: {pct_input:+.1f}%")
                lines.append(f"- 叙事长度: {ra.get('narrative_length',0)} -> {rb.get('narrative_length',0)}")
                lines.append(f"- AI调用数: {ra.get('ai_calls',0)} -> {rb.get('ai_calls',0)}")
                lines.append("")

        lines.append("---")
        lines.append("")

    lines.append("*实验由 benchmark_route_pipeline.py 自动生成*")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ──────────────────────────────────────────────
#  Main — 3-turn sequential test
# ──────────────────────────────────────────────

async def _get_provider_from_db():
    import aiosqlite
    from config import DB_PATH
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM ai_profiles WHERE is_active = 1 LIMIT 1")
            row = await cursor.fetchone()
            if not row:
                cursor = await db.execute("SELECT * FROM ai_profiles LIMIT 1")
                row = await cursor.fetchone()
            if not row:
                return None, None
            profile = dict(row)
    except Exception as e:
        print(f"读取数据库失败: {e}")
        return None, None

    from api.config_routes import _create_provider_from_profile
    stage_models = json.loads(profile.get("stage_models") or "{}") if profile.get("stage_models") else {}
    return _create_provider_from_profile(profile), stage_models


# Each scenario: 3 turns of actions to run sequentially on the same session
SCENARIO_ACTIONS = [
    {
        "label": "社交/打听",
        "turns": [
            {"type": "free_text", "text": "我走向酒馆的吧台，向酒保打听最近城里有什么传闻"},
            {"type": "free_text", "text": "我掏出几枚铜币放在吧台上，追问失踪案的细节"},
            {"type": "free_text", "text": "我转身走向角落里打盹的老乔，请他喝一杯"},
        ],
    },
    {
        "label": "探索/调查",
        "turns": [
            {"type": "free_text", "text": "我小心翼翼地推开地下室的门，举起火把探查里面的情况"},
            {"type": "free_text", "text": "我蹲下来仔细检查墙壁上的符文和地上的物品"},
            {"type": "free_text", "text": "我把发现的物品收好，沿着来路返回地面"},
        ],
    },
    {
        "label": "战斗/攻击",
        "turns": [
            {"type": "free_text", "text": "我拔出武器，向面前的盗贼发起攻击"},
            {"type": "free_text", "text": "我闪身躲避盗贼的反击，寻找破绽"},
            {"type": "free_text", "text": "我趁对方露出破绽，全力一击试图结束战斗"},
        ],
    },
]


async def run_scenario_multi_turn(scenario: dict, real_provider, stage_models: dict, script: dict):
    """Run a single scenario with 3 turns for both Group A and Group B."""
    label = scenario["label"]
    turns = scenario["turns"]
    turn_results = []

    # --- Group A session (persistent across turns) ---
    prov_a = StageAwareProvider(real_provider)
    sess_a = GameSession(copy.deepcopy(script), prov_a, stage_models=stage_models)
    await sess_a.initialize()

    # --- Group B session (persistent across turns) ---
    prov_b = StageAwareProvider(real_provider)
    sess_b = GameSession(copy.deepcopy(script), prov_b, stage_models=stage_models)
    await sess_b.initialize()

    for turn_idx, action in enumerate(turns):
        turn_num = turn_idx + 1
        print(f"\n  --- 回合 {turn_num}/3: {action['text'][:35]}... ---")

        # Group A
        print(f"    [A] 当前 8-Stage Pipeline...")
        prov_a.reset()
        t0 = time.perf_counter()
        try:
            result_a = await run_current_pipeline(sess_a, action)
            wall_a = time.perf_counter() - t0
            label_calls_heuristic(prov_a.calls)
            report_a = build_report(f"8-Stage T{turn_num}", prov_a.calls, result_a, wall_a)
            print(f"    [A] OK: {wall_a:.1f}s, {len(prov_a.calls)} calls, narrative={report_a['narrative_length']}ch")
        except Exception as e:
            wall_a = time.perf_counter() - t0
            print(f"    [A] FAIL: {e}")
            import traceback
            traceback.print_exc()
            report_a = {"mode": f"8-Stage T{turn_num}", "error": str(e), "wall_time_s": round(wall_a, 2), "stage_summary": {}}

        # Group B
        print(f"    [B] Route + 瘦身 Pipeline...")
        prov_b.reset()
        t0 = time.perf_counter()
        try:
            result_b = await run_route_pipeline(sess_b, prov_b, action)
            wall_b = time.perf_counter() - t0
            report_b = build_report(f"Route+Slim T{turn_num}", prov_b.calls, result_b, wall_b)
            print(f"    [B] OK: {wall_b:.1f}s, {len(prov_b.calls)} calls, narrative={report_b['narrative_length']}ch")
        except Exception as e:
            wall_b = time.perf_counter() - t0
            print(f"    [B] FAIL: {e}")
            import traceback
            traceback.print_exc()
            report_b = {"mode": f"Route+Slim T{turn_num}", "error": str(e), "wall_time_s": round(wall_b, 2), "stage_summary": {}}

        if "error" not in report_a and "error" not in report_b:
            print_comparison(f"{label} T{turn_num}", report_a, report_b)

        turn_results.append({
            "turn": turn_num,
            "action": action["text"],
            "group_a": report_a,
            "group_b": report_b,
        })

    return turn_results


async def main():
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "scripts", "dnd_open_world_ashenvale.json")
    if not os.path.exists(script_path):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "data", "scripts", "example_school.json")
    if not os.path.exists(script_path):
        print("找不到示例脚本")
        return

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    print(f"加载脚本: {script.get('script_name', '?')}")
    print(f"地点: {len(script.get('locations', []))}  NPC: {len(script.get('npcs', []))}  组织: {len(script.get('organizations', []))}")

    real_provider, stage_models = await _get_provider_from_db()
    if not real_provider:
        print("错误: 未配置 AI 服务。请先在网页端设置 AI profile。")
        return
    print(f"AI Provider: {real_provider.__class__.__name__}")
    print(f"Stage Models: {stage_models or '默认'}")
    print(f"\n{'=' * 70}")
    print(f"  3轮连续测试模式：每个场景运行3个连续回合")
    print(f"{'=' * 70}")

    all_results = []

    for idx, scenario in enumerate(SCENARIO_ACTIONS):
        print(f"\n{'#' * 70}")
        print(f"  场景 {idx+1}/{len(SCENARIO_ACTIONS)}: {scenario['label']}")
        print(f"{'#' * 70}")

        turn_results = await run_scenario_multi_turn(scenario, real_provider, stage_models, script)

        all_results.append({
            "label": scenario["label"],
            "action": scenario["turns"][0]["text"],
            "turns": turn_results,
        })

    # ─── Aggregate ───
    print(f"\n{'=' * 70}")
    print("  汇总统计")
    print(f"{'=' * 70}")
    for gkey, glabel in [("group_a", "A (当前 8-Stage)"), ("group_b", "B (Route+瘦身)")]:
        all_valid = []
        for r in all_results:
            for t in r.get("turns", []):
                rd = t.get(gkey, {})
                if "error" not in rd:
                    all_valid.append(rd)
        if not all_valid:
            print(f"  {glabel}: 全部失败")
            continue
        avg_wall = sum(r["wall_time_s"] for r in all_valid) / len(all_valid)
        avg_calls = sum(r["ai_calls"] for r in all_valid) / len(all_valid)
        avg_narr = sum(r["narrative_length"] for r in all_valid) / len(all_valid)
        avg_input = sum(r["total_input_chars"] for r in all_valid) / len(all_valid)
        print(f"  {glabel} ({len(all_valid)} turns):")
        print(f"    平均耗时:    {avg_wall:.2f}s")
        print(f"    平均调用数:  {avg_calls:.1f}")
        print(f"    平均叙事长度: {avg_narr:.0f} 字符")
        print(f"    平均输入量:  {avg_input:,.0f} 字符")

    # Save results
    exp_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(exp_dir, "route_benchmark_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n原始数据: {json_path}")

    md_path = os.path.join(exp_dir, "route_benchmark_report.md")
    generate_md_report(all_results, md_path)
    print(f"报告: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
