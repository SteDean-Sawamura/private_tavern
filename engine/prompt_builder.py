"""AI prompt builder: assembles prompts from current game state."""

import re

from engine.lorebook import Lorebook, LorebookEntry
from engine.event_scheduler import parse_time
from engine.prompt_kg_mixin import PromptKGMixin
from engine.prompt_sections_mixin import PromptSectionsMixin
from engine.prompt_npc_mixin import PromptNpcMixin
from engine.prompt_narrative_mixin import PromptNarrativeMixin
from engine.prompt_world_state_mixin import PromptWorldStateMixin

# O-4: 预编译历史消息清洗用的正则
_RE_GAME_STATE_BLOCK = re.compile(r'```game_state\s*[\s\S]*?```\s*')
_RE_GAME_STATE_OPEN = re.compile(r'```game_state\s*[\s\S]*$')

# Sentinel inserted between cache-stable and per-turn-varying sections.
# ClaudeProvider splits on this to add cache_control; other providers strip it.
CACHE_SENTINEL = "\n\n<|cache_break|>\n\n"

# Sections whose content is stable across consecutive turns (static or rarely changing)
_CACHE_STABLE_KEYS = frozenset({"role", "world"})


def _xml(tag: str, content: str) -> str:
    if not content or not content.strip():
        return ""
    return f"<{tag}>\n{content}\n</{tag}>"


class PromptBuilder(PromptKGMixin, PromptSectionsMixin, PromptNpcMixin, PromptNarrativeMixin, PromptWorldStateMixin):
    def __init__(self, script: dict):
        self.script = script
        # Build NPC/location lookup dicts for O(1) access (PERF-2)
        self._npc_name_map = {n["id"]: n.get("name", n["id"]) for n in script.get("npcs", [])}
        self._npc_by_id = {n["id"]: n for n in script.get("npcs", [])}
        self._loc_name_map = {l["id"]: l.get("name", l["id"]) for l in script.get("locations", [])}
        self._location_by_id = {l["id"]: l for l in script.get("locations", [])}
        # Generate knowledge graph lorebook entries from NPCs, relationships, events
        kg_entries = self._generate_knowledge_graph(script)
        all_lore = script.get("lorebook", []) + kg_entries
        _settings = script.get("settings", {})
        self.lorebook = Lorebook(
            all_lore,
            token_budget=_settings.get("lorebook_token_budget", 0),
            max_recursion=_settings.get("lorebook_max_recursion", 4),
        )
        self._has_kg = len(kg_entries) > 0
        self._cached_narrative_role_section = None
        self.condition_eval = None
        # Prompt section order — configurable via script settings.
        # 静态段在前（role / world），
        # 慢变段居中（character / npc / states），
        # 每回合变化的段在最后（lorebook / authors_note），
        # 以便 prompt cache 命中尽量长的前缀。
        # 组织信息通过 lorebook 按需注入，不再作为独立 section。
        self._default_order = [
            "role", "world",
            "character", "npc", "states", "story_context",
            "narrative_callbacks", "tone", "pacing", "time_atmosphere", "difficulty_awareness", "npc_relationship_depth", "discovery_hints", "moral_alignment", "faction_reputation", "available_quests", "memory_echo", "skill_growth",
            "lorebook_after_world", "lorebook_at_end", "authors_note",
        ]

    def build_user_message(
        self,
        player_action: dict,
        current_time: str,
        triggered_events: list[dict],
        dice_results: list[dict],
        check_result: dict | None = None,
        triggered_consequences: list[dict] | None = None,
        achieved_milestones: list[dict] | None = None,
        opening_description: str = "",
        state: dict | None = None,
        event_sections: dict[str, str] | None = None,
    ) -> str:
        """Build the user message for a turn."""
        parts = []

        formatted_time = self.format_game_time(current_time)
        time_line = f"[系统更新]\n当前游戏时间: {formatted_time} ({current_time})"
        # 追加明天的星期信息，防止AI日期推理错误
        if current_time:
            try:
                t = parse_time(current_time)
                if t:
                    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                    tomorrow_wd = weekdays[(t.weekday() + 1) % 7]
                    is_weekend_tomorrow = (t.weekday() + 1) % 7 >= 5
                    hint = f"（明天是{tomorrow_wd}"
                    if is_weekend_tomorrow:
                        hint += "，是周末"
                    else:
                        hint += "，不是周末"
                    hint += "）"
                    time_line += f" {hint}"
            except Exception:
                pass
        parts.append(time_line)

        if triggered_events:
            events_text = "\n".join(
                f"- {e.get('description', e.get('event_id', '未知事件'))}"
                + (f" (触发时间: {e['fire_time']})" if 'fire_time' in e else "")
                for e in triggered_events
            )
            parts.append(f"本回合触发事件:\n{events_text}")

        if triggered_consequences:
            cons_text = "\n".join(
                f"- {c['description']}"
                for c in triggered_consequences
            )
            parts.append(f"本回合触发的延迟后果（必须在叙事中体现）:\n{cons_text}")

        if achieved_milestones:
            mile_text = "\n".join(
                f"- {m.get('name', m.get('id', ''))}: {m.get('description', '')}"
                for m in achieved_milestones
            )
            parts.append(f"本回合达成里程碑（在叙事中庆祝/体现）:\n{mile_text}")

        if dice_results:
            dice_text = "\n".join(
                f"- [{d.get('source_label', d.get('description', d.get('random_item_id', '骰子')))}] "
                f"{d['formula']} = {d['total']}"
                + (f" → 结果: {d['range_label']}" if d.get('range_label') else "")
                + (f" {d.get('duration_info', '')}" if d.get('sustained') else "")
                for d in dice_results
            )
            parts.append(
                f"本回合骰子结果（必须在叙事中体现影响；标注\"持续中\"的为之前回合的延续状态，应保持一致性）:\n{dice_text}"
            )

        # Player action
        action_type = player_action.get("type", "freeform")
        action_text = player_action.get("text", "")
        if action_type == "choice":
            choice_text = f"[玩家行动]\n玩家选择了: {action_text}"
            if opening_description:
                choice_text += f"\n[开局预设] 该选项的预期发展方向: {opening_description}\n请基于此推演后续剧情，但可根据需要自然展开。"
            parts.append(choice_text)
        else:
            parts.append(f"[玩家行动]\n{action_text}")

        # Skill check result for freeform actions
        if check_result:
            outcome = check_result.get("outcome", "")
            attr = check_result.get("related_attribute", "")
            diff = check_result.get("difficulty", "")
            diff_label = {"extreme": "极难", "hard": "困难", "medium": "普通", "easy": "简单"}.get(diff, diff)
            labels = {"critical_success": "大成功", "success": "成功",
                      "failure": "失败", "critical_failure": "大失败"}
            label = labels.get(outcome, outcome)
            skill_desc = f"({attr}·{diff_label})" if attr else ""
            rule = check_result.get("rule", "default")
            rule_hint = {"brp": "（BRP规则：骰点≤技能值为成功）",
                         "dnd": "（D&D规则：d20+修正≥DC为成功）"}.get(rule, "")
            parts.append(
                f"[技能检定{skill_desc}] → **{label}**{rule_hint}\n"
                f"请根据检定结果调整叙事，但不要在叙事文本中提及具体数值（如骰点、阈值、属性值）。"
                f"用角色的感受和行为体现结果，例如「凭借你的{attr or '直觉'}，你……」。"
                f"检定数值会由系统UI单独展示。\n"
                f"{check_result.get('narrative_hint', '')}"
            )

        # C13: 向AI注入待定后果，让其在叙事中铺垫伏笔
        if event_sections and event_sections.get("consequences"):
            parts.append(f"[待定伏线] 以下事态正在酝酿中，请在叙事中适度铺垫：\n{event_sections['consequences']}")

        # D3: 在场NPC名单 — 比system_prompt中的标注更显眼
        if state:
            player_loc = state.get("player", {}).get("location", "")
            if player_loc:
                present_npcs = self._get_present_npcs(state, player_loc)
                if present_npcs:
                    names = ", ".join(present_npcs)
                    parts.append(f"当前位置在场NPC: {names}（仅这些NPC可在本回合直接出现在场景中）")
                else:
                    parts.append("当前位置无NPC在场。")

        if state:
            prev_scene = state.get("scene_details")
            if prev_scene and isinstance(prev_scene, dict):
                scene_lines = ["[上一轮场景状态]"]
                if prev_scene.get("atmosphere"):
                    scene_lines.append(f"氛围: {prev_scene['atmosphere']}")
                if prev_scene.get("sensory"):
                    scene_lines.append(f"感官: {prev_scene['sensory']}")
                if prev_scene.get("key_objects"):
                    scene_lines.append(f"关键物件: {', '.join(prev_scene['key_objects'])}")
                if prev_scene.get("npc_expressions"):
                    for ne in prev_scene["npc_expressions"]:
                        npc_name = self._get_npc_name(ne.get('npc_id', ''))
                        scene_lines.append(f"- {npc_name}: {ne.get('expression','')}")
                if prev_scene.get("pending_tension"):
                    scene_lines.append(f"悬念: {prev_scene['pending_tension']}")
                if len(scene_lines) > 1:
                    parts.append("\n".join(scene_lines))

        complexity = 0
        if triggered_events:
            complexity += len(triggered_events)
        if triggered_consequences:
            complexity += len(triggered_consequences)
        if achieved_milestones:
            complexity += len(achieved_milestones)
        if check_result:
            complexity += 1
        if len(action_text) > 60:
            complexity += 1

        simple_actions = {"继续", "等待", "继续探索", "观察周围", "休息", "继续前进"}
        if action_text in simple_actions or (len(action_text) <= 6 and complexity == 0):
            parts.append("请根据以上信息继续剧情。本回合行动简单，叙事约300-500字即可，不必过度展开。")
        elif complexity >= 3:
            parts.append("请根据以上信息继续剧情。本回合涉及多个事件/后果，请详细展开叙事（1000-1500字）。")
        else:
            parts.append("请根据以上信息继续剧情。")
        return "\n\n".join(parts)

    def build_history_context(self, recent_nodes: list[dict], history_summary: str = "") -> str:
        """Build recent history summary for context (used in system prompt for summary only)."""
        if not history_summary:
            return ""
        return f"## 之前的剧情摘要\n{history_summary}"

    def build_history_messages(self, recent_nodes: list[dict], history_summary: str = "", chapter_summaries: list = None) -> list[dict]:
        """Build multi-turn message history from recent nodes.

        Returns list of {role, content} dicts alternating user/assistant.
        This gives the AI proper conversational context.
        """
        messages = []

        # Prefer the most recent chapter summary over the global history_summary
        summary_text = history_summary
        if chapter_summaries:
            latest_chapter = chapter_summaries[-1] if isinstance(chapter_summaries[-1], str) else chapter_summaries[-1].get("summary", "")
            if latest_chapter:
                summary_text = latest_chapter

        # Add summary as a system-like context in the first user message
        if summary_text:
            messages.append({
                "role": "user",
                "content": f"[剧情摘要] {summary_text}\n\n请继续游戏。",
            })
            messages.append({
                "role": "assistant",
                "content": "好的，我已了解之前的剧情，请继续你的行动。",
            })

        total = len(recent_nodes)
        display_names = None
        prev_narrative = ""
        for i, node in enumerate(recent_nodes):
            action = node.get("player_action")
            if action:
                action_text = action.get("text", "") if isinstance(action, dict) else str(action)
                if action_text:
                    ctx_tag = self._build_turn_context_tag(node, display_names)
                    if ctx_tag:
                        action_text = f"{ctx_tag}\n\n{action_text}"
                    if prev_narrative:
                        action_text = f"[上一轮叙事 - 已确认事实，不可修改]\n{prev_narrative}\n\n[玩家行动]\n{action_text}"
                    messages.append({"role": "user", "content": action_text})

            response = node.get("ai_response", "")
            if response:
                is_opening = node.get("player_action") is None
                is_recent = i >= total - 2
                if not is_opening and not is_recent:
                    cached = node.get("_truncated_response")
                    if cached:
                        response = cached
                    else:
                        response = _RE_GAME_STATE_BLOCK.sub('', response).rstrip()
                        response = _RE_GAME_STATE_OPEN.sub('', response).rstrip()
                        if len(response) > 800:
                            cut = response[:800]
                            for sep in ['\n\n', '。', '\n', '！', '？']:
                                pos = cut.rfind(sep)
                                if pos > 400:
                                    cut = cut[:pos + len(sep)]
                                    break
                            response = cut + "..."
                        node["_truncated_response"] = response
                else:
                    response = _RE_GAME_STATE_BLOCK.sub('', response).rstrip()
                    response = _RE_GAME_STATE_OPEN.sub('', response).rstrip()
                messages.append({"role": "assistant", "content": response})
                prev_narrative = response

            # 缓存 display_names 供后续轮次使用
            snapshot = node.get("state_snapshot")
            if snapshot and isinstance(snapshot, dict):
                dn = snapshot.get("display_names")
                if dn:
                    display_names = dn

        return messages

    def _build_turn_context_tag(self, node: dict, display_names: dict | None) -> str:
        """从 node 的 state_snapshot 中提取紧凑的上下文标注。"""
        snapshot = node.get("state_snapshot")
        if not snapshot or not isinstance(snapshot, dict):
            return ""
        dn = display_names or snapshot.get("display_names", {})
        parts = []
        game_time = node.get("game_time", "")
        if game_time:
            formatted = self.format_game_time(game_time)
            parts.append(f"时间:{formatted}")
        player = snapshot.get("player", {})
        loc = player.get("location", "")
        if loc:
            parts.append(f"位置:{dn.get(loc, loc)}")
        # 在场NPC
        present = self._get_present_npcs(snapshot, loc) if loc else []
        if present:
            parts.append(f"在场:{','.join(present)}")
        if not parts:
            return ""
        return f"[上下文 {' | '.join(parts)}]"

    def build_context_memory(self, recent_nodes: list[dict], state: dict) -> str:
        """从最近2轮提取上下文要点，帮助AI保持一致性。"""
        if not recent_nodes:
            return ""
        last2 = recent_nodes[-2:] if len(recent_nodes) >= 2 else recent_nodes
        texts = [n.get("ai_response", "") for n in last2 if n.get("ai_response")]
        if not texts:
            return ""
        combined = " ".join(texts)

        # 匹配在叙事中出现的道具
        mentioned_items = []
        for item in state.get("inventory", []):
            item_name = item.get("item", "") if isinstance(item, dict) else str(item)
            if item_name and item_name in combined:
                mentioned_items.append(item_name)

        display_names = state.get("display_names", {})
        loc_display = display_names.get(
            state.get("player", {}).get("location", ""),
            state.get("player", {}).get("location", ""),
        )

        parts = []
        if loc_display:
            parts.append(f"- 当前所在地点: {loc_display}")

        # 离场NPC动态 — lorebook 存在时由 lorebook 按需激活，此处跳过
        if not self.lorebook:
            offscreen_log = state.get("npc_offscreen_log", {})
            if offscreen_log:
                off_lines = []
                for npc_id, entries in offscreen_log.items():
                    if entries:
                        latest = entries[-1]
                        name = self._get_npc_name(npc_id)
                        off_lines.append(f"{name}: {latest.get('action', '?')}(在{display_names.get(latest.get('location', ''), latest.get('location', '?'))})")
                if off_lines:
                    parts.append(f"- 离场NPC近况: {'; '.join(off_lines[:3])}")

        if mentioned_items:
            parts.append(f"- 近期提及的道具: {', '.join(mentioned_items)}")

        persistent_facts = state.get("persistent_facts", [])
        if persistent_facts:
            recent_facts = persistent_facts[-8:]
            parts.append(f"- 持久事实: {'; '.join(f.get('fact', '') for f in recent_facts)}")

        if not parts:
            return ""
        return "## 近期上下文要点\n" + "\n".join(parts)

    def _resolve_npc_location(self, npc_id: str, state: dict, game_time: str,
                              condition_eval=None) -> str:
        condition_eval = condition_eval or self.condition_eval
        """获取NPC当前位置：动态位置 > schedule > default_location。"""
        npc_st = state.get("npcs", {}).get(npc_id, {})
        cur_loc = npc_st.get("current_location", "")
        if cur_loc:
            return cur_loc
        npc_def = self._npc_by_id.get(npc_id)
        if npc_def:
            _ov = state.get("npc_schedule_overrides", {}).get(npc_id)
            schedule_loc = self._get_schedule_location(npc_def, game_time, condition_eval=condition_eval, overrides=_ov)
            if schedule_loc:
                return schedule_loc
            return npc_def.get("default_location", "")
        return npc_st.get("default_location", "")

    @staticmethod
    def format_game_time(time_str: str) -> str:
        """Convert ISO time like '2025-09-01T07:30:00' to Chinese-friendly format.

        Returns e.g. '9月1日 周一 上午7:30' for absolute dates, or
        '第1天 上午7:30' for relative day-based times.
        """
        if not time_str:
            return time_str
        try:
            t = parse_time(time_str)
            if not t:
                return time_str

            # Determine time-of-day label
            hour = t.hour
            minute = t.minute
            # B23: 凌晨0点应显示为12而非0; B24: 统一时段划分
            if 0 <= hour < 2:
                period = "深夜"
                display_hour = 12 if hour == 0 else hour
            elif 2 <= hour < 6:
                period = "凌晨"
                display_hour = hour
            elif 6 <= hour < 12:
                period = "上午"
                display_hour = hour
            elif 12 <= hour < 13:
                period = "中午"
                display_hour = 12
            elif 13 <= hour < 18:
                period = "下午"
                display_hour = hour - 12
            else:
                period = "晚上"
                display_hour = hour - 12

            time_part = f"{period}{display_hour}:{minute:02d}"

            # Weekday names
            weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
            weekday = weekdays[t.weekday()]

            return f"{t.month}月{t.day}日 {weekday} {time_part}"
        except Exception:
            return time_str

    def _resolve_location_name(self, location_id: str) -> str:
        """Resolve a location ID to its display name from the script."""
        return self._loc_name_map.get(location_id, location_id)

    def scan_lorebook(
        self, player_action: str, recent_messages: list[str],
        timed_state: dict | None = None,
        turn_number: int = 0,
    ) -> tuple[list[LorebookEntry], dict]:
        """Scan for activated lorebook entries. Called by GameSession.

        Returns (activated_entries, updated_timed_state).
        """
        return self.lorebook.scan(player_action, recent_messages, timed_state=timed_state, turn_number=turn_number)

    def inject_depth_lore(
        self, messages: list[dict], activated_lore: list[LorebookEntry] | None,
    ) -> list[dict]:
        """Inject at_depth lorebook entries into a messages array."""
        if not activated_lore or not self.lorebook:
            return messages
        depth_entries = self.lorebook.get_entries_by_position(activated_lore, "at_depth")
        if not depth_entries:
            return messages
        return self.lorebook.inject_depth_entries(messages, depth_entries)

    def build_lorebook_expansion_prompt(
        self, state: dict, old_time: str, new_time: str,
    ) -> tuple[list[dict], str]:
        """Build prompt for dynamic lorebook expansion when game time advances."""
        world_bg = self.script.get("world_background", "")[:600]
        location = self._resolve_location_name(
            state.get("player", {}).get("location", "")
        )
        summary = state.get("history_summary", "")[:400]
        existing_ids = [e.id for e in self.lorebook.entries]

        system = (
            "你是世界百科编辑。根据游戏世界背景和时间推进，生成该时间段内可能发生的客观世界事件词条。\n"
            "规则：\n"
            "- 第三人称客观视角，不涉及主角/玩家\n"
            "- 每条80-150字\n"
            "- 这是该世界线的预期历史。如果剧情摘要显示玩家行动已明显改变了某些事件走向，跳过或调整那些事件\n"
            "- keys 应包含年份、地名、人名、事件名等便于关键词触发的词\n"
            "- 无可写内容时返回空数组 []\n"
            "- 只返回JSON数组，不要其他文字"
        )

        content = (
            f"世界背景:\n{world_bg}\n\n"
            f"当前位置: {location}\n"
            f"时间推进: {old_time} → {new_time}\n\n"
            f"剧情摘要:\n{summary}\n\n"
            f"已有词条ID: {', '.join(existing_ids[:50]) if existing_ids else '无'}\n\n"
            "请生成3-5条该时间段内的客观世界事件词条。\n"
            '输出JSON数组: [{"id":"唯一ID","keys":["关键词1","关键词2"],"content":"词条内容","comment":"简短标签"}]\n'
            "每条默认 position: after_world, priority: 80, scan_depth: 3。"
        )

        messages = [{"role": "user", "content": content}]
        return messages, system

