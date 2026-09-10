"""AI prompt builder: assembles prompts from current game state."""

import re

from engine.lorebook import Lorebook, LorebookEntry
from engine.event_scheduler import parse_time

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


class PromptBuilder:
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

    @staticmethod
    def _build_kg_helper_maps(script: dict) -> dict:
        """Build shared helper maps for KG content generation."""
        npcs = script.get("npcs", [])
        npc_map = {n["id"]: n for n in npcs}
        organizations = script.get("organizations", [])
        org_by_id = {o["id"]: o for o in organizations if o.get("id")}
        org_rank_title: dict[str, dict[int, str]] = {}
        for o in organizations:
            oid = o.get("id", "")
            hierarchy = o.get("hierarchy", [])
            if hierarchy:
                org_rank_title[oid] = {h["rank"]: h["title"] for h in hierarchy if "rank" in h and "title" in h}
        org_members: dict[str, list[dict]] = {}
        subordinates: dict[str, list[dict]] = {}
        for npc in npcs:
            for om in npc.get("organizations", []):
                oid = om.get("org_id", "")
                if oid:
                    org_members.setdefault(oid, []).append(npc)
            sup_id = npc.get("superior")
            if sup_id:
                subordinates.setdefault(sup_id, []).append(npc)
        return {
            "npcs": npcs, "npc_map": npc_map,
            "organizations": organizations, "org_by_id": org_by_id,
            "org_rank_title": org_rank_title,
            "org_members": org_members, "subordinates": subordinates,
        }

    @staticmethod
    def _build_kg_npc_content(npc: dict, maps: dict, npc_rels: list[dict]) -> tuple[str, list[str], list[str]]:
        """Build KG content for a single NPC. Returns (content, keys, rel_ids)."""
        npc_id = npc["id"]
        name = npc.get("name", npc_id)
        keys = [name, npc_id]
        if npc.get("title"):
            keys.append(npc["title"])

        npc_map = maps["npc_map"]
        org_by_id = maps["org_by_id"]
        org_rank_title = maps["org_rank_title"]
        subordinates = maps["subordinates"]

        lines = [f"【{name}】"]
        if npc.get("bio"):
            lines.append(f"简介: {npc['bio']}")
        if npc.get("personality"):
            lines.append(f"性格: {npc['personality']}")
        if npc.get("capabilities"):
            lines.append(f"能力: {npc['capabilities']}")
        if npc.get("title"):
            lines.append(f"头衔: {npc['title']}")
        npc_orgs = npc.get("organizations", [])
        if npc_orgs:
            org_labels = []
            for om in npc_orgs:
                om_oid = om.get("org_id", "")
                org_name = org_by_id.get(om_oid, {}).get("name", om_oid)
                npc_rank = om.get("rank")
                rank_map = org_rank_title.get(om_oid, {})
                rank_title = rank_map.get(npc_rank) if npc_rank is not None else None
                label = org_name
                if rank_title:
                    label += f"（{rank_title}）"
                role = om.get("role", "")
                if role:
                    label += f"[{role}]"
                org_labels.append(label)
            lines.append(f"所属组织: {'、'.join(org_labels)}")
        if npc.get("superior"):
            sup_name = npc_map.get(npc["superior"], {}).get("name", npc["superior"])
            lines.append(f"上级: {sup_name}")
        subs = subordinates.get(npc_id, [])
        if subs:
            sub_names = "、".join(s.get("name", s["id"]) for s in subs)
            lines.append(f"下属: {sub_names}")
        if npc.get("default_location"):
            lines.append(f"通常出没: {npc['default_location']}")

        rel_ids = []
        # Link to organization KG entries for deterministic Phase 3 activation
        for om in npc_orgs:
            om_oid = om.get("org_id", "")
            if om_oid:
                rel_ids.append(f"_kg_org_{om_oid}")
        for rel in npc_rels:
            if "from" in rel:
                f_id, t_id = rel.get("from", ""), rel.get("to", "")
                if npc_id in (f_id, t_id):
                    other_id = t_id if f_id == npc_id else f_id
                    other_name = npc_map.get(other_id, {}).get("name", other_id)
                    t, a, fe = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                    lines.append(f"与{other_name}: 信任{t}/好感{a}/畏惧{fe}" +
                                 (f" — {rel['description']}" if rel.get("description") else ""))
                    rel_ids.append(f"_kg_rel_{f_id}_{t_id}")
            else:
                a_id, b_id = rel.get("a", ""), rel.get("b", "")
                if npc_id in (a_id, b_id):
                    other_id = b_id if a_id == npc_id else a_id
                    other_name = npc_map.get(other_id, {}).get("name", other_id)
                    lines.append(f"与{other_name}的关系: {rel.get('type', '中立')}" +
                                 (f" — {rel['description']}" if rel.get("description") else ""))
                    rel_ids.append(f"_kg_rel_{a_id}_{b_id}")

        return "\n".join(lines), keys, rel_ids

    @staticmethod
    def _build_kg_org_content(org: dict, maps: dict) -> tuple[str, list[str]]:
        """Build KG content for a single organization. Returns (content, keys)."""
        org_id = org.get("id", "")
        org_name = org.get("name", org_id)
        keys = [org_name] + org.get("aliases", [])
        npc_map = maps["npc_map"]
        org_rank_title = maps["org_rank_title"]
        org_members = maps["org_members"]

        org_type = org.get("type", "")
        label = f"【{org_type or '组织'}】{org_name}" if org_type else f"【组织】{org_name}"
        lines = [label]
        if org.get("stance"):
            lines[0] += f"（立场: {org['stance']}）"
        if org.get("leader"):
            leader_name = npc_map.get(org["leader"], {}).get("name", org["leader"])
            lines.append(f"领导者: {leader_name}")
        if org.get("description"):
            lines.append(org["description"])
        hierarchy = org.get("hierarchy", [])
        if hierarchy:
            rank_labels = "→".join(h["title"] for h in sorted(hierarchy, key=lambda h: h.get("rank", 99)))
            lines.append(f"层级: {rank_labels}")
        if org.get("parent_org"):
            parent_name = maps["org_by_id"].get(org["parent_org"], {}).get("name", org["parent_org"])
            lines.append(f"上级组织: {parent_name}")
        child_orgs = [o.get("name", o.get("id", "")) for o in maps["organizations"] if o.get("parent_org") == org_id]
        if child_orgs:
            lines.append(f"下属组织: {'、'.join(child_orgs)}")
        rank_map = org_rank_title.get(org_id, {})
        members = org_members.get(org_id, [])
        if members:
            def _member_rank(m):
                for om in m.get("organizations", []):
                    if om.get("org_id") == org_id and om.get("rank") is not None:
                        return om["rank"]
                return 999
            sorted_members = sorted(members, key=_member_rank)
            parts = []
            for m in sorted_members:
                m_name = m.get("name", m["id"])
                m_rank = _member_rank(m)
                rank_title = rank_map.get(m_rank) if m_rank != 999 else None
                om_entry = next((om for om in m.get("organizations", []) if om.get("org_id") == org_id), {})
                role = om_entry.get("role", "")
                detail = rank_title or role or m.get("title", "")
                sup_id = m.get("superior")
                if sup_id:
                    sup_name = npc_map.get(sup_id, {}).get("name", sup_id)
                    detail += f"，上级: {sup_name}" if detail else f"上级: {sup_name}"
                parts.append(f"{m_name}（{detail}）" if detail else m_name)
            lines.append(f"成员: {'、'.join(parts)}")
        goals = org.get("goals", [])
        if goals:
            goal_descs = [g.get("description", g.get("id", "")) for g in goals[:3]]
            lines.append(f"目标: {'；'.join(goal_descs)}")
        return "\n".join(lines), keys

    @staticmethod
    def _build_kg_loc_content(loc: dict, loc_name_map: dict) -> tuple[str, list[str]]:
        """Build KG content for a single location. Returns (content, keys)."""
        loc_id = loc.get("id", "")
        loc_name = loc.get("name", loc_id)
        keys = [loc_name, loc_id]
        conns = loc.get("connections", [])
        for conn_id in conns:
            conn_name = loc_name_map.get(conn_id, conn_id)
            if conn_name not in keys:
                keys.append(conn_name)

        lines = [f"【地点】{loc_name}"]
        if loc.get("description"):
            lines.append(loc["description"])
        if loc.get("travel_time"):
            lines.append(f"移动耗时: {loc['travel_time']}")
        if loc.get("access_condition"):
            lines.append(f"进入条件: {loc['access_condition']}")
        if conns:
            conn_names = [loc_name_map.get(c, c) for c in conns]
            lines.append(f"相连地点: {'、'.join(conn_names)}")
        return "\n".join(lines), keys

    @staticmethod
    def _generate_knowledge_graph(script: dict) -> list[dict]:
        """Auto-generate lorebook entries from NPCs, relationships, events, orgs, locations."""
        entries = []
        npcs = script.get("npcs", [])
        npc_map = {n["id"]: n for n in npcs}
        npc_rels = script.get("npc_relationships", [])
        organizations = script.get("organizations", [])
        org_rels = script.get("org_relationships", [])

        maps = PromptBuilder._build_kg_helper_maps(script)

        # 1. NPC profile entries
        for npc in npcs:
            content, keys, rel_ids = PromptBuilder._build_kg_npc_content(npc, maps, npc_rels)
            entries.append({
                "id": f"_kg_npc_{npc['id']}",
                "keys": keys,
                "content": content,
                "entry_type": "npc_profile",
                "related_entries": rel_ids,
                "priority": 90,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"NPC档案: {npc.get('name', npc['id'])}",
            })

        # 2. NPC-NPC relationship entries
        for rel in npc_rels:
            if "from" in rel:
                from_id = rel.get("from", "")
                to_id = rel.get("to", "")
                from_name = npc_map.get(from_id, {}).get("name", from_id)
                to_name = npc_map.get(to_id, {}).get("name", to_id)
                t, a, f = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                desc = rel.get("description", "")
                content = f"{from_name} → {to_name}: 信任{t}/好感{a}/畏惧{f}"
                if desc:
                    content += f"\n{desc}"
                entries.append({
                    "id": f"_kg_rel_{from_id}_{to_id}",
                    "keys": [from_name, to_name],
                    "content": content,
                    "entry_type": "npc_relationship",
                    "related_entries": [f"_kg_npc_{from_id}", f"_kg_npc_{to_id}"],
                    "priority": 80,
                    "position": "after_world",
                    "constant": False,
                    "enabled": True,
                    "scan_depth": 3,
                    "comment": f"NPC关系: {from_name} → {to_name}",
                })
            else:
                a_id = rel.get("a", "")
                b_id = rel.get("b", "")
                a_name = npc_map.get(a_id, {}).get("name", a_id)
                b_name = npc_map.get(b_id, {}).get("name", b_id)
                rel_type = rel.get("type", "中立")
                desc = rel.get("description", "")
                content = f"{a_name} ↔ {b_name}: {rel_type}"
                if desc:
                    content += f"\n{desc}"
                entries.append({
                    "id": f"_kg_rel_{a_id}_{b_id}",
                    "keys": [a_name, b_name],
                    "content": content,
                    "entry_type": "npc_relationship",
                    "related_entries": [f"_kg_npc_{a_id}", f"_kg_npc_{b_id}"],
                    "priority": 80,
                    "position": "after_world",
                    "constant": False,
                    "enabled": True,
                    "scan_depth": 3,
                    "comment": f"NPC关系: {a_name} ↔ {b_name}",
                })

        # 3. Event context entries
        for evt in script.get("one_time_events", []):
            evt_desc = evt.get("description", "")
            if not evt_desc:
                continue
            evt_keys = []
            trigger = evt.get("trigger", {})
            conditions = trigger.get("conditions", []) if isinstance(trigger, dict) else []
            for cond in conditions:
                target = cond.get("target", "")
                for npc in npcs:
                    if npc["id"] in target:
                        evt_keys.append(npc.get("name", npc["id"]))
            if evt.get("id"):
                evt_keys.append(evt["id"])
            if not evt_keys:
                continue
            entries.append({
                "id": f"_kg_evt_{evt.get('id', 'unknown')}",
                "keys": evt_keys,
                "content": f"事件: {evt_desc}",
                "entry_type": "event_context",
                "priority": 70,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"事件: {evt.get('id', '?')}",
            })

        # 4. Organization entries
        for org in organizations:
            content, keys = PromptBuilder._build_kg_org_content(org, maps)
            entries.append({
                "id": f"_kg_org_{org.get('id', '')}",
                "keys": keys,
                "content": content,
                "entry_type": "organization",
                "priority": 85,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"组织: {org.get('name', org.get('id', ''))}",
            })

        # 5. Inter-organization relationship entries
        org_by_id = maps["org_by_id"]
        for orel in org_rels:
            a_id = orel.get("a", "")
            b_id = orel.get("b", "")
            a_name = org_by_id.get(a_id, {}).get("name", a_id)
            b_name = org_by_id.get(b_id, {}).get("name", b_id)
            rel_type = orel.get("type", "中立")
            desc = orel.get("description", "")
            content = f"{a_name} ↔ {b_name}: {rel_type}"
            if desc:
                content += f"\n{desc}"
            entries.append({
                "id": f"_kg_orel_{a_id}_{b_id}",
                "keys": [a_name, b_name],
                "content": content,
                "entry_type": "org_relationship",
                "priority": 80,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"组织关系: {a_name} ↔ {b_name}",
            })

        # 6. Location entries
        loc_name_map = {l["id"]: l.get("name", l["id"]) for l in script.get("locations", [])}
        for loc in script.get("locations", []):
            if not loc.get("description"):
                continue
            content, keys = PromptBuilder._build_kg_loc_content(loc, loc_name_map)
            entries.append({
                "id": f"_kg_loc_{loc['id']}",
                "keys": keys,
                "content": content,
                "entry_type": "location",
                "priority": 75,
                "position": "after_world",
                "constant": False,
                "enabled": True,
                "scan_depth": 3,
                "comment": f"地点: {loc.get('name', loc['id'])}",
            })

        return entries

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

    def _build_narrative_role_section(self) -> str:
        """叙事专用 role section — 只含写作规则，不含 JSON 格式定义。"""
        if self._cached_narrative_role_section:
            return self._cached_narrative_role_section
        result = """你是一个交互式文字游戏的游戏主持人（GM）。你需要根据以下世界设定和当前状态，为玩家描述场景、推进剧情。

【重要】直接输出叙事内容，不要输出任何JSON、结构化数据、代码块或选项列表。系统会单独处理选项和状态变更。

用第二人称（"你"）描述场景和事件，注重细节、氛围和角色互动，充分展开叙述（800-1200字）。

规则：
- 所有角色（包括主角和NPC）的言行举止必须贴合其性格设定。活泼的角色要表现活泼，温柔的要温柔，严肃的才严肃。不要让所有角色都用同一种冷静严肃的语气
- 主角的内心独白、反应和行动方式要体现其性格特征
- 角色对话必须使用中文弯引号 “……” 包裹（直接说出口的话），内心独白和心理活动可不加引号或使用『……』。这是渲染层识别对话的依据，不要省略引号也不要使用裸文本对白
- 如果本回合有骰子结果，必须在叙事中体现骰子的影响。高骰值=正面结果，低骰值=负面结果
- NPC不是被动存在的。根据NPC的性格和对主角的态度主动表现行为——好感高的NPC主动帮助/友好，好感低的会刁难/冷淡
- 如果系统提供了技能检定结果，必须在叙事中体现。成功=行动达成，失败=行动受阻。但禁止在叙事中出现具体数值（如"87的政治嗅觉""d100=73"），数值由系统UI单独展示，用角色的感受和行为自然体现结果
- 信息边界：严格遵守角色的认知边界。叙事只能包含主角当前能合理知道的信息。主角不知道的事件（他人的秘密计划、未来将发生的事）不得暗示或透露。不要用全知视角写作
- 角色描写要含蓄自然，通过行为和细节暗示性格，不要直接点明角色在"演绎"或"扮演"某种形象
- 天气影响叙事氛围和行动难度
- 如果玩家的自由输入不合理，在叙事中巧妙解释为什么没有成功
- 保持叙事与世界观一致
- 如果系统提示了"延迟后果"或"待定伏线"，在叙事中适度铺垫
- 某些NPC初始不认识主角(known=false)，首次互动时以自然方式描述相识过程
- 主角台词边界：你可以描写主角的动作、表情、内心感受，但不可替主角说出具体台词。若场景需要主角回应，用动作暗示（"你点了点头""你应了一声"）或省略号代替（"你说了句……"）。只有玩家在行动中明确写出的话才能作为主角台词出现在叙事中
- 禁止输出任何思考过程（如"Let me think..."等）。如果你需要思考，请使用<think>标签包裹"""
        self._cached_narrative_role_section = result
        return result

    def build_narrative_system_prompt(
        self,
        state: dict,
        activated_lore: list[LorebookEntry] | None = None,
        authors_note: str = "",
        turn_number: int = 0,
        authors_note_position: str = "end",
        macro_expander=None,
        negative_prompt: str = "",
        event_sections: dict[str, str] | None = None,
        pc_discovered_lore: list[str] | None = None,
    ) -> str:
        """Build system prompt for narrative-only generation.

        turn > 1 时精简：跳过世界背景全文（历史上下文已隐含）。
        组织信息通过 lorebook 按需注入。
        """
        section_map = {
            "role": self._build_narrative_role_section,
            "world": (
                (lambda: self._build_world_brief_section(state))
                if turn_number > 1
                else (lambda: self._build_world_section(state))
            ),
            "character": lambda: self._build_character_section(state),
            "npc": lambda: self._build_npc_section(state),
            "states": lambda: self._build_active_states_section(state),
            "story_context": lambda: self._build_story_context_section(state),
            "narrative_callbacks": lambda: self._build_callback_hints_section(state, turn_number),
            "tone": lambda: self._build_tone_section(state),
            "pacing": lambda: self._build_pacing_section(state),
            "moral_alignment": lambda: self._build_moral_alignment_section(state),
            "faction_reputation": lambda: self._build_faction_reputation_section(state),
            "available_quests": lambda: self._build_available_quests_section(state),
            "memory_echo": lambda: self._build_memory_echo_section(state),
            "skill_growth": lambda: self._build_skill_growth_section(state),
            "time_atmosphere": lambda: self._build_time_atmosphere_section(state),
            "difficulty_awareness": lambda: self._build_difficulty_section(state),
            "npc_relationship_depth": lambda: self._build_npc_relationship_depth_section(state),
            "discovery_hints": lambda: self._build_discovery_hints_section(state),
            "lorebook_after_world": lambda: self._build_lorebook_section(
                activated_lore, "after_world",
                pc_discovered_lore=pc_discovered_lore, scope="pc",
                visibility_markers=True,
            ),
            "lorebook_at_end": lambda: self._build_lorebook_section(
                activated_lore, "at_end",
                pc_discovered_lore=pc_discovered_lore, scope="pc",
                visibility_markers=True,
            ),
            "authors_note": lambda: self._build_authors_note_section(authors_note) if authors_note_position == "end" else "",
        }

        order = self.script.get("settings", {}).get(
            "prompt_order", self._default_order
        )
        sep = "\n\n---\n\n"
        stable_sections = []
        dynamic_sections = []
        in_stable = True
        for key in order:
            builder = section_map.get(key)
            if not builder:
                continue
            text = builder() if callable(builder) else builder
            if not text:
                continue
            if in_stable and key in _CACHE_STABLE_KEYS:
                stable_sections.append(text)
            else:
                in_stable = False
                dynamic_sections.append(text)

        # Opening context — turn <= 5 时注入；之后已沉淀到历史上下文中，节省 token
        if turn_number <= 5:
            opening_ctx = state.get("opening_context", "")
            if opening_ctx:
                stable_sections.append(f"## 开局设定\n{opening_ctx}")

        # 玩家风格引导叙事节奏
        play_style = state.get("play_style_summary")
        if play_style and isinstance(play_style, dict) and play_style.get("tag"):
            style_desc = play_style.get("description", "")
            style_text = f"## 玩家风格\n{play_style['tag']}"
            if style_desc:
                style_text += f"（{style_desc}）"
            style_text += "\n请适当调整叙事节奏和侧重以贴合此风格。"
            dynamic_sections.append(style_text)

        result = sep.join(stable_sections)
        if dynamic_sections:
            result += CACHE_SENTINEL + sep.join(dynamic_sections)

        if negative_prompt:
            result += "\n\n---\n\n## 绝对禁止事项\n以下内容绝对不允许出现在叙事中：\n" + negative_prompt

        if macro_expander:
            result = macro_expander(result)

        return result

    def _build_world_brief_section(self, state: dict) -> str:
        """精简版世界 section（turn > 1 时使用）— 只保留已知地点列表。"""
        raw_location = state.get("player", {}).get("location", "未知")
        location = self._resolve_location_name(raw_location)
        visible_ids = set(state.get("visible_locations", []))
        locations = self.script.get("locations", [])
        loc_descriptions = []
        for loc in locations:
            if loc["id"] in visible_ids:
                if self._has_kg:
                    desc = f"- {loc.get('name', loc['id'])}({loc['id']})"
                else:
                    desc = f"- {loc.get('name', loc['id'])}: {loc.get('description', '')}"
                travel = loc.get("travel_time")
                if travel and loc["id"] != raw_location:
                    desc += f" (移动需{travel})"
                if loc.get("access_condition"):
                    desc += f" [条件: {loc['access_condition']}]"
                loc_descriptions.append(desc)

        locs_text = "\n".join(loc_descriptions) if loc_descriptions else "暂无已知地点"
        return f"## 已知地点\n{locs_text}"

    def _collect_state_context(self, state: dict) -> dict:
        """收集共享的状态上下文信息，供 build_choices_prompt / build_world_state_prompt 使用。"""
        player = state.get("player", {})
        attrs = player.get("attributes", {})
        dn = state.get("display_names", {})
        pc_rules = self.script.get("player_character", {}).get("attributes", {})
        inventory = state.get("inventory", [])
        active_ids = set(state.get("active_persistent_states", []))

        # 紧凑属性
        attrs_compact = ", ".join(
            f"{dn.get(k, k)}={v}/{(pc_rules.get(k, {}).get('max', 100) if isinstance(pc_rules.get(k), dict) else 100)}"
            for k, v in attrs.items()
        )
        # 详细属性（含范围）
        attrs_lines = []
        for k, v in attrs.items():
            rule = pc_rules.get(k, {})
            mx = rule.get("max", 100) if isinstance(rule, dict) else 100
            mn = rule.get("min", 0) if isinstance(rule, dict) else 0
            attrs_lines.append(f"- {dn.get(k, k)}({k}): {v} [{mn}-{mx}]")

        # 背包
        inv_compact = ", ".join(
            f"{it['item']}x{it.get('quantity', 1)}{' [可用]' if it.get('use_effect') else ''}" for it in inventory
        ) if inventory else "无"

        # 持续状态（紧凑）
        active_names = []
        for ps in self.script.get("persistent_states", []):
            if ps["id"] in active_ids:
                active_names.append(ps.get("name", ps["id"]))

        # 位置
        loc_id = player.get("location", "")
        location = self._resolve_location_name(loc_id)
        loc_descs = state.get("location_descriptions", {})
        location_desc = loc_descs.get(loc_id, "")[:80] if loc_id else ""

        return {
            "player": player, "attrs": attrs, "dn": dn, "pc_rules": pc_rules,
            "active_ids": active_ids,
            "attrs_compact": attrs_compact,
            "attrs_lines": attrs_lines,
            "inv_compact": inv_compact,
            "active_names": active_names,
            "location": location,
            "location_id": loc_id,
            "location_desc": location_desc,
        }

    def build_choices_prompt(
        self, narrative: str, action_text: str, state: dict, *,
        turn_number: int = 0, activated_lore: list | None = None,
        story_hints: str = "",
        world_change_hints: str = "",
        event_sections: dict[str, str] | None = None,
        pc_discovered_lore: list[str] | None = None,
    ) -> tuple[list[dict], str]:
        """Build prompt for choices-only generation. Returns (messages, system).

        system 只含纯静态 rules（跨回合 100% 缓存命中），
        动态背景注入 user message。
        """
        system = """你是游戏选项设计师。根据叙事内容和角色状态，设计2-4个策略性选项。

规则：
- id格式: c1, c2, c3, ...
- text: 选项描述
- hint: **必填**。描述该选项的预期后果和代价（时间、金钱、风险、关系变化等）。例如"预计耗时30分钟，消耗30金币，可能惹怒守卫"。不要省略此字段
- time_hint: ISO 8601格式时间（PT30M、PT1H、P1D）
- risk: **必填**。评估该选项的风险等级，取值为 "safe"、"moderate"、"risky" 之一。safe=几乎不会失败或没有负面后果；moderate=有一定风险但可控；risky=高风险高回报或可能产生严重后果
- 当选项有前置条件（属性/物品/位置等）且玩家不满足时，设置 locked:true 和 lock_reason（如"需要魅力>50（当前30）"）
- 选项应体现不同风险/收益，有策略深度
- **人设贴合**：选项措辞和方向应贴合主角的身份、性格与长期目标，避免 OOC
- **信息边界**：选项只能基于叙事中已提及或玩家已知的信息。不要引入叙事中完全没有出现过的人物、线索、地点或情报线（如玩家不知情的线人、未提及的秘密通道等）。选项的关注点应聚焦于当前场景中自然延伸的行动
- **选项是行动，不是叙事**：每个选项的text必须描述玩家主动采取的行动（说、做、去、查、等），不能在选项中编写新的场景描述（如"突然有人敲门""远处传来爆炸声"等）。叙事已经结束，选项只负责提供下一步行动方向
- **基于叙事结尾设计**：选项必须从叙事结束时的场景状态出发。仔细阅读叙事的最后几段，确定主角当前所处的位置、姿态、手中物品、在场人物，以此为起点设计选项。不要基于叙事中间或开头的状态（如叙事中主角已经移动到新地点，选项不能假设主角还在旧地点）

只返回JSON，不要任何解释:
{"choices":[{"id":"c1","text":"...","hint":"预期后果和代价","time_hint":"PT30M","risk":"moderate"},{"id":"c2","text":"...","hint":"预期后果和代价","time_hint":"PT1H","risk":"safe"}]}"""
        system += CACHE_SENTINEL

        # 动态背景注入 user message（而非 system），保持 system 纯静态
        bg_parts = []
        world_brief = self._build_world_brief_section(state)
        if world_brief:
            bg_parts.append(world_brief)
        character = self._build_character_section(state)
        if character:
            bg_parts.append(character)
        npc = self._build_npc_section(state)
        if npc:
            bg_parts.append(npc)

        if activated_lore and self.lorebook:
            lore_entries = self.lorebook.get_entries_by_position(activated_lore, "after_world")
            lore_entries = Lorebook.filter_by_visibility(lore_entries, "pc_strict", pc_discovered_lore)
            lore_text = self.lorebook.format_for_prompt(lore_entries)
            if lore_text:
                bg_parts.append(lore_text)

        # 持续状态（紧凑版）
        active_ids = set(state.get("active_persistent_states", []))
        if active_ids:
            predefined_ids = set()
            ps_names = []
            for ps in self.script.get("persistent_states", []):
                predefined_ids.add(ps["id"])
                if ps["id"] in active_ids:
                    ps_names.append(ps.get("name", ps["id"]))
            dn = state.get("display_names", {})
            for sid in active_ids - predefined_ids:
                ps_names.append(dn.get(sid, sid))
            if ps_names:
                bg_parts.append(f"当前持续状态: {', '.join(ps_names)}")

        bg_text = "\n\n---\n\n".join(bg_parts) + "\n\n---\n\n" if bg_parts else ""

        # scene_details 摘要
        scene_text = ""
        scene = state.get("scene_details")
        if scene and isinstance(scene, dict):
            s_parts = []
            if scene.get("atmosphere"):
                s_parts.append(f"氛围:{scene['atmosphere']}")
            if scene.get("pending_tension"):
                s_parts.append(f"悬念:{scene['pending_tension']}")
            if s_parts:
                scene_text = f"\n\n场景状态: {' | '.join(s_parts)}"

        # 玩家人设摘要（帮助选项贴合角色，有 lorebook 词条时跳过已覆盖字段）
        player = state.get("player", {})
        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )
        pc_tag = ""
        pc_bits = []
        if player.get("name"):
            pc_bits.append(player["name"])
        if not has_pc_lore:
            if player.get("personality"):
                pc_bits.append(f"性格:{player['personality'][:15]}")
            if player.get("long_term_goal"):
                pc_bits.append(f"目标:{player['long_term_goal'][:25]}")
        if pc_bits:
            pc_tag = f"\n\n主角: {' | '.join(pc_bits)}"

        # 资源余量摘要：让AI能写出有参考价值的cost/benefit hint
        resource_tag = ""
        resource_bits = []
        attrs = player.get("attributes", {})
        for attr_name, attr_val in attrs.items():
            v = attr_val if isinstance(attr_val, (int, float)) else (attr_val.get("value") if isinstance(attr_val, dict) else None)
            if v is not None:
                resource_bits.append(f"{attr_name}={v}")
        inventory = state.get("inventory", [])
        if inventory:
            inv_summary = []
            for item in inventory[:8]:
                name = item.get("item", "") if isinstance(item, dict) else str(item)
                qty = item.get("quantity", 1) if isinstance(item, dict) else 1
                if name:
                    tag = " [可用]" if isinstance(item, dict) and item.get("use_effect") else ""
                    inv_summary.append(f"{name}×{qty}{tag}" if qty > 1 else f"{name}{tag}")
            if inv_summary:
                resource_bits.append(f"背包:[{','.join(inv_summary)}]")
        if resource_bits:
            resource_tag = f"\n\n当前资源: {' | '.join(resource_bits[:10])}"

        # 玩家风格回馈：让选项贴合玩家偏好
        style_hint = ""
        play_style = state.get("play_style_summary")
        if play_style and isinstance(play_style, dict) and play_style.get("tag"):
            style_hint = f"\n\n玩家风格: {play_style['tag']}" + (f"（{play_style['description']}）" if play_style.get('description') else "") + "。选项设计应适当倾向此风格偏好，但不排除其他方向。"

        # 环境互动元素
        interactable_hint = ""
        interactables = [ia for ia in state.get("location_interactables", []) if not ia.get("used")]
        if interactables:
            items = ["可互动元素（可作为选项来源）:"]
            for ia in interactables[:4]:
                items.append(f"- {ia.get('name', '')}: {ia.get('description', '')}（{ia.get('action_hint', '')}）")
            interactable_hint = "\n\n" + "\n".join(items)

        # 同伴意见提示
        companion_hint = ""
        companion_ids = state.get("companions", [])
        if companion_ids:
            npcs_state = state.get("npcs", {})
            dn = state.get("display_names", {})
            c_parts = ["同伴（可在选项hint中自然融入同伴的态度或建议）:"]
            for cid in companion_ids[:3]:
                ns = npcs_state.get(cid, {})
                cname = dn.get(cid) or (ns.get("name", cid) if isinstance(ns, dict) else cid)
                personality = ns.get("personality", "") if isinstance(ns, dict) else ""
                c_parts.append(f"- {cname}" + (f"（{personality[:20]}）" if personality else ""))
            companion_hint = "\n\n" + "\n".join(c_parts)

        # 声望提示
        rep_tag = ""
        faction_rep = state.get("faction_reputation", {})
        if faction_rep:
            org_name_map = {o.get("id", ""): o.get("name", o.get("id", "")) for o in self.script.get("organizations", [])}
            rep_items = []
            for fid, fdata in faction_rep.items():
                fname = org_name_map.get(fid, state.get("display_names", {}).get(fid, fid))
                val = fdata.get("value", 50) if isinstance(fdata, dict) else 50
                rep_items.append(f"{fname}={val}")
            if rep_items:
                rep_tag = f"\n\n阵营声望: {' | '.join(rep_items)}。声望极低时某些选项可被locked"

        # 线索板摘要（辅助设计调查/推理类选项）
        clue_hint = ""
        if event_sections and event_sections.get("clues"):
            clue_hint = f"\n\n已发现线索（可设计调查/验证/追踪类选项）:\n{event_sections['clues']}"

        # 意外选项机制：当有特殊条件时允许AI添加一个"剧情打断"选项
        surprise_hint = ""
        has_pending = bool(event_sections and event_sections.get("consequences"))
        extreme_npc = any(
            isinstance(d, dict) and (d.get("attitude_toward_player", 50) >= 90 or d.get("attitude_toward_player", 50) <= 10)
            for d in state.get("npcs", {}).values()
        )
        extreme_weather = any(w in (state.get("current_weather") or "") for w in ("暴雨", "大雪", "极端"))
        if has_pending or extreme_npc or extreme_weather:
            surprise_hint = "\n\n[特殊情境] 当前存在未触发后果/极端NPC关系/恶劣天气。你可以额外添加一个「突发」选项（id用c_event），但该选项仍然必须是玩家可以主动采取的行动，不能在选项文本中编写新的叙事场景（如突然有人敲门、突然传来声音等）。正确做法：基于当前叙事中已有的线索设计一个紧迫性更高的行动选项。如无合适场景可不添加。"

        # 房间位置提示
        room_hint = ""
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            room_hint = f"\n\n当前房间: {player_room}（选项中涉及移动时应以此为起点）"
            nearby_ids = state.get("_nearby_npc_ids", [])
            if nearby_ids:
                nearby_lines = []
                dn = state.get("display_names", {})
                npcs_st = state.get("npcs", {})
                for nid in nearby_ids:
                    ns = npcs_st.get(nid, {})
                    nname = dn.get(nid) or (ns.get("name", nid) if isinstance(ns, dict) else nid)
                    nr = ns.get("current_room", "?") if isinstance(ns, dict) else "?"
                    nearby_lines.append(f"- {nname} → {nr}")
                room_hint += "\n同建筑其他房间可前往的NPC:\n" + "\n".join(nearby_lines)

        # 排列：角色/资源框架(primacy) → 场景状态 → 行动 → 叙事(recency=设计依据)
        # 选项只关注叙事结尾状态，截断前部减少 token
        narrative_tail = narrative[-800:] if len(narrative) > 800 else narrative
        content = f"""{bg_text}{pc_tag}{resource_tag}{rep_tag}{style_hint}{scene_text}{interactable_hint}{companion_hint}{clue_hint}{surprise_hint}{room_hint}

玩家行动: {action_text}

叙事内容（选项必须基于叙事结尾时的场景状态设计）:
{narrative_tail}"""

        if world_change_hints:
            content += world_change_hints

        if story_hints:
            content += story_hints

        messages = [{"role": "user", "content": content}]
        return messages, system

    def _resolve_location_name(self, location_id: str) -> str:
        """Resolve a location ID to its display name from the script."""
        return self._loc_name_map.get(location_id, location_id)

    def _build_world_section(self, state: dict) -> str:
        bg = self.script.get("world_background", "")
        raw_location = state.get("player", {}).get("location", "未知")
        location = self._resolve_location_name(raw_location)

        # Build visible locations list with travel info
        visible_ids = set(state.get("visible_locations", []))
        locations = self.script.get("locations", [])
        loc_descriptions = []
        for loc in locations:
            if loc["id"] in visible_ids:
                if self._has_kg:
                    desc = f"- {loc.get('name', loc['id'])}({loc['id']})"
                else:
                    desc = f"- {loc.get('name', loc['id'])}: {loc.get('description', '')}"
                travel = loc.get("travel_time")
                if travel and loc["id"] != raw_location:
                    desc += f" (移动需{travel})"
                if loc.get("access_condition"):
                    desc += f" [条件: {loc['access_condition']}]"
                loc_descriptions.append(desc)

        locs_text = "\n".join(loc_descriptions) if loc_descriptions else "暂无已知地点"

        return f"""## 世界背景
{bg}

## 当前位置
{location}

## 已知地点
{locs_text}
注意：如果AI建议location_change，移动到有travel_time的地点会自动额外消耗时间——`end_time` 字段中**不要再重复**包含旅行时间。"""

    def _build_character_section(self, state: dict) -> str:
        player = state.get("player", {})
        attrs = player.get("attributes", {})
        rels = player.get("relationships", {})

        # Get max values from script rules
        pc = self.script.get("player_character", {})
        attr_rules = pc.get("attributes", {})
        rel_rules = pc.get("relationships", {})

        dn = state.get("display_names", {})
        attrs_lines = []
        for k, v in attrs.items():
            rule = attr_rules.get(k, {})
            mx = rule.get("max", 100) if isinstance(rule, dict) else 100
            label = dn.get(k, k)
            attrs_lines.append(f"- {label}({k}): {v}/{mx}")
        attrs_text = "\n".join(attrs_lines)

        rels_lines = []
        for rel_id, val in rels.items():
            npc_name = self._get_npc_name(rel_id)
            if isinstance(val, dict) and any(k in val for k in ("trust", "affection", "fear")):
                t = val.get("trust", 50)
                a = val.get("affection", 50)
                f = val.get("fear", 0)
                rels_lines.append(f"- {npc_name}: 信任{t}/好感{a}/畏惧{f}")
            else:
                v = val if isinstance(val, (int, float)) else (val.get("value", 50) if isinstance(val, dict) else 50)
                rule = rel_rules.get(rel_id, {})
                mx = rule.get("max", 100) if isinstance(rule, dict) else 100
                rels_lines.append(f"- {npc_name}: {v}/{mx}")
        rels_text = "\n".join(rels_lines)

        # Inventory
        inventory = state.get("inventory", [])
        if inventory:
            inv_lines = [f"- {it['item']} x{it.get('quantity', 1)}" for it in inventory]
            inv_text = "\n".join(inv_lines)
        else:
            inv_text = "- 无"

        personality = player.get('personality', '')
        personality_line = f"\n- 性格: {personality}" if personality else ""

        # Class/profession display
        class_section = ""
        class_name = player.get("class_name")
        if class_name:
            level = player.get("level", 1)
            lines_cls = [f"\n## 职业与技能", f"- 职业: {class_name} (Lv.{level})"]
            skills = player.get("skills", {})
            if skills:
                proficient = [f"{s.get('name', sid)}" for sid, s in skills.items()
                              if isinstance(s, dict) and s.get("proficient")]
                if proficient:
                    lines_cls.append(f"- 熟练技能: {', '.join(proficient)}")
            tags = player.get("narrative_tags", [])
            if tags:
                lines_cls.append(f"- 特性: {', '.join(tags)}")
            abilities = player.get("abilities", [])
            if abilities:
                lines_cls.append(f"- 能力: {', '.join(abilities)}")
            class_section = "\n".join(lines_cls)

        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )

        lines = ["## 主角状态", f"- 姓名: {player.get('name', '未知')}"]
        if not has_pc_lore:
            lines.append(f"- 身份: {player.get('bio', '未知')}{personality_line}")
        if attrs_text:
            lines.append(attrs_text)
        lines.append(f"- 当前位置: {self._resolve_location_name(player.get('location', '未知'))}")
        if not has_pc_lore:
            lines.append(f"- 长期目标: {player.get('long_term_goal', '无')}")

        return "\n".join(lines) + f"""

## 背包
{inv_text}

## 人际关系
{rels_text}{class_section}"""

    def _build_npc_section(self, state: dict) -> str:
        lines = ["## 重要NPC"]
        npc_states = state.get("npcs", {})
        player_loc = state.get("player", {}).get("location", "")
        game_time = state.get("game_time", "")
        encounter_counts = state.get("npc_encounter_counts", {}) or {}
        dialogue_counts = state.get("npc_dialogue_counts", {}) or {}

        # Determine time of day from game_time
        time_period = ""
        if game_time:
            try:
                t = parse_time(game_time)
                if t:
                    hour = t.hour
                    if 6 <= hour < 12:
                        time_period = "上午"
                    elif 12 <= hour < 14:
                        time_period = "中午"
                    elif 14 <= hour < 18:
                        time_period = "下午"
                    elif 18 <= hour < 22:
                        time_period = "晚上"
                    elif 22 <= hour or hour < 2:
                        time_period = "深夜"
                    else:
                        time_period = "凌晨"
            except Exception:
                pass

        # --- 分层：将 NPC 分为在场 / 近期相关 / 其余 ---
        recent_npc_ids = set()
        offscreen_log = state.get("npc_offscreen_log", {})
        recent_npc_ids.update(offscreen_log.keys())
        for npc_id in encounter_counts:
            if encounter_counts[npc_id] > 0:
                recent_npc_ids.add(npc_id)

        present_npcs = []
        recent_npcs = []
        background_npcs = []

        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            npc_loc = self._resolve_npc_location(npc_id, state, game_time)
            is_present = npc_loc and player_loc and npc_loc.lower() == player_loc.lower()
            if is_present:
                present_npcs.append((npc, npc_loc, True))
            elif npc_id in recent_npc_ids:
                recent_npcs.append((npc, npc_loc, False))
            else:
                background_npcs.append((npc, npc_loc, False))

        # --- 在场 NPC：完整信息 ---
        if present_npcs:
            lines.append("\n### 当前在场（可直接互动）")
        for npc, npc_loc, _ in present_npcs:
            lines.append(self._format_npc_detail(
                npc, state, npc_states, player_loc, game_time,
                encounter_counts, dialogue_counts, is_present=True,
            ))

        # --- 近期相关 NPC：中等信息 ---
        if recent_npcs:
            lines.append("\n### 近期相关（不在场）")
        for npc, npc_loc, _ in recent_npcs:
            lines.append(self._format_npc_brief(
                npc, state, npc_states, npc_loc, encounter_counts, dialogue_counts,
            ))

        # --- 其余 NPC：有知识库时不注入，无知识库时极简一行 ---
        if background_npcs:
            if self._has_kg:
                names = [npc.get("name", npc["id"]) for npc, _, _ in background_npcs]
                lines.append(f"\n### 其他NPC（详情通过知识图谱按需注入）")
                lines.append(f"- {', '.join(names)}")
            else:
                lines.append("\n### 其他NPC")
                for npc, npc_loc, _ in background_npcs:
                    lines.append(self._format_npc_minimal(npc, npc_states, npc_loc))

        # NPC-NPC relationship network — skip when lorebook has relationship entries
        has_rel_lore = self.lorebook and any(
            e.id.startswith("_npc_rel_") and e.enabled for e in self.lorebook.entries
        )
        if not self._has_kg and not has_rel_lore:
            npc_rels_global = state.get("npc_relationships_global", {})
            npc_rels_known = state.get("npc_relationships_known", {})
            known_keys = set(npc_rels_known.keys()) if npc_rels_known else set()
            if npc_rels_global and isinstance(npc_rels_global, dict):
                def _fmt_rel(rel, compact=False, *, is_known=True):
                    tag = "" if is_known else " [玩家未知·仅供行为逻辑参考]"
                    met_tag = ""
                    if rel.get("initially_met") is False or rel.get("met") is False:
                        met_tag = " [未接触]"
                    elif rel.get("initially_known") is False or rel.get("known") is False:
                        met_tag = " [不认识]"
                    if "from" in rel:
                        fn = self._get_npc_name(rel["from"])
                        tn = self._get_npc_name(rel["to"])
                        t, a, f = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                        desc = rel.get("description", "")
                        if compact:
                            return f"{fn}→{tn}:信{t}/感{a}/畏{f}" + (f"({desc[:15]})" if desc else "") + met_tag + tag
                        return f"- {fn} → {tn}: 信任{t}/好感{a}/畏惧{f}" + (f" ({desc})" if desc else "") + met_tag + tag
                    else:
                        an = self._get_npc_name(rel.get("a", ""))
                        bn = self._get_npc_name(rel.get("b", ""))
                        rel_type = rel.get("type", "中立")
                        desc = rel.get("description", "")
                        if compact:
                            return f"{an}↔{bn}:{rel_type}" + (f"({desc[:15]})" if desc else "") + met_tag + tag
                        return f"- {an} ↔ {bn}: {rel_type}" + (f" ({desc})" if desc else "") + met_tag + tag

                if len(npc_rels_global) > 20:
                    parts = [_fmt_rel(rel, compact=True, is_known=k in known_keys) for k, rel in npc_rels_global.items()]
                    lines.append(f"\n### NPC间关系网 (共{len(npc_rels_global)}条)")
                    lines.append("标注[玩家未知]的关系：NPC行为可受其影响，但叙事中不得让主角直接知晓")
                    lines.append(" | ".join(parts))
                else:
                    lines.append("\n### NPC间关系网")
                    lines.append("标注[玩家未知]的关系：NPC行为可受其影响，但叙事中不得让主角直接知晓")
                    for k, rel in npc_rels_global.items():
                        lines.append(_fmt_rel(rel, is_known=k in known_keys))

        # NPC offscreen log
        if offscreen_log:
            lines.append("\n### NPC近期动态（离场行动）")
            for npc_id, entries in offscreen_log.items():
                if entries:
                    latest = entries[-1]
                    npc_name = self._get_npc_name(npc_id)
                    lines.append(f"- {npc_name}: {latest.get('action', '?')} (位于{latest.get('location', '?')})")

        return "\n".join(lines)

    def _npc_three_dim_suffix(self, npc_id: str, state: dict) -> str:
        rel_data = state.get("player", {}).get("relationships", {}).get(npc_id, {})
        parts = []
        if isinstance(rel_data, dict):
            for dim_key, dim_label in (("trust", "信任"), ("affection", "好感"), ("fear", "畏惧")):
                v = rel_data.get(dim_key)
                if isinstance(v, (int, float)):
                    parts.append(f"{dim_label} {int(v)}")
        return f" [{' / '.join(parts)}]" if parts else ""

    def _npc_behavior_hint(self, attitude: int) -> str:
        if attitude >= 80:
            return "忠诚/热情——会主动帮助、分享秘密、提供资源"
        elif attitude >= 60:
            return "友好——乐于交谈、可能提供帮助"
        elif attitude >= 40:
            return "中立——公事公办、不会主动帮忙也不刁难"
        elif attitude >= 20:
            return "冷淡/警惕——不愿交流、可能设置小障碍"
        return "敌对——会刁难、阻挠、甚至攻击"

    def _format_org_tags(self, npc_orgs: list[dict], *, sep: str = "、", compact: bool = False) -> str:
        """从 NPC organizations 数组构建组织标签字符串。"""
        org_by_id = {o["id"]: o for o in self.script.get("organizations", []) if o.get("id")}
        tags = []
        for om in npc_orgs:
            oid = om.get("org_id", "")
            org_obj = org_by_id.get(oid, {})
            org_name = org_obj.get("name", oid)
            npc_rank = om.get("rank")
            rank_title = None
            if npc_rank is not None:
                for h in org_obj.get("hierarchy", []):
                    if h.get("rank") == npc_rank:
                        rank_title = h.get("title")
                        break
            if compact:
                tags.append(f"{org_name}·{rank_title}" if rank_title else org_name)
            else:
                role = om.get("role", "")
                label = org_name
                if rank_title:
                    label += f"（{rank_title}）"
                if role:
                    label += f"[{role}]"
                tags.append(label)
        return sep.join(tags)

    def _format_npc_detail(self, npc, state, npc_states, player_loc, game_time,
                           encounter_counts, dialogue_counts, *, is_present):
        """在场 NPC 的完整信息。"""
        npc_id = npc["id"]
        npc_st = npc_states.get(npc_id, {})
        attitude = npc_st.get("attitude_toward_player", npc.get("attitude_toward_player", 50))
        known = npc_st.get("known", npc.get("known", True))
        encounter_count = encounter_counts.get(npc_id, 0)
        three_dim = self._npc_three_dim_suffix(npc_id, state)
        behavior = self._npc_behavior_hint(attitude)
        att_trigger = ""
        if attitude >= 90:
            att_trigger = " [好感极高：可触发特殊剧情/告白/结盟]"
        elif attitude <= 10:
            att_trigger = " [好感极低：可能主动攻击/背叛]"

        has_npc_lore = self.lorebook and any(
            e.id == f"_npc_profile_{npc_id}" and e.enabled for e in self.lorebook.entries
        )

        if has_npc_lore or self._has_kg:
            npc_name = npc_st.get('name') or npc.get('name', npc_id)
            detail = f"\n- **{npc_name}**: 态度 {attitude}/100{three_dim} → {behavior}{att_trigger}"
            if not has_npc_lore:
                npc_personality = npc_st.get("personality") or npc.get("personality", "")
                if npc_personality:
                    detail = f"\n- **{npc_name}**: 性格:{npc_personality} 态度 {attitude}/100{three_dim} → {behavior}{att_trigger}"
            npc_orgs = npc_st.get("organizations") or npc.get("organizations", [])
            if npc_orgs:
                detail += f" | {self._format_org_tags(npc_orgs, compact=True)}"
        else:
            npc_bio = npc_st.get("bio") or npc.get("bio", "")
            npc_personality = npc_st.get("personality") or npc.get("personality", "")
            npc_caps = npc_st.get("capabilities") or npc.get("capabilities", "未知")
            detail = (
                f"\n### {npc_st.get('name') or npc.get('name', npc_id)}\n"
                f"- 简介: {npc_bio}\n"
                f"- 性格: {npc_personality}\n"
                f"- 能力: {npc_caps}\n"
                f"- 对主角态度: {attitude}/100{three_dim} → {behavior}{att_trigger}"
            )
            npc_title = npc_st.get("title") or npc.get("title", "")
            if npc_title:
                detail += f"\n- 头衔: {npc_title}"
            npc_orgs = npc_st.get("organizations") or npc.get("organizations", [])
            if npc_orgs:
                detail += f"\n- 所属组织: {self._format_org_tags(npc_orgs)}"
            npc_sup = npc_st.get("superior") or npc.get("superior", "")
            if npc_sup:
                sup_name = self._get_npc_name(npc_sup)
                detail += f"\n- 上级: {sup_name}"

        # NPC 自主目标
        completed_goals = set(state.get("completed_npc_goals", []))
        npc_goals = npc.get("goals", [])
        active_goals = [g for g in npc_goals if f"{npc_id}:{g.get('id','')}" not in completed_goals]
        if active_goals:
            goal_parts = []
            for g in active_goals[:2]:
                gtext = g.get("description", "")
                conflict = g.get("conflict_with_player", "")
                if conflict:
                    gtext += f"（{conflict}）"
                goal_parts.append(gtext)
            detail += f"\n  动机/目标: {'; '.join(goal_parts)}"

        # NPC 已知信息（信息传播网络）
        info_network = state.get("information_network", [])
        npc_known_info = [i for i in info_network if npc_id in i.get("known_by", [])]
        if npc_known_info:
            info_lines = []
            for info in npc_known_info[-3:]:
                fact = info.get("fact", "")
                if info.get("distortion", 0) >= 2:
                    info_lines.append(f"[谣言/失真] {fact}")
                else:
                    info_lines.append(fact)
            detail += f"\n  已知信息: {'; '.join(info_lines)}"

        # 交互历史标注
        if not known and encounter_count == 0:
            detail += " ★未认识主角（尚未碰面）"
        elif encounter_count > 0:
            dlg = dialogue_counts.get(npc_id, 0)
            detail += f" [已交互{encounter_count}回合"
            if dlg:
                detail += f"+{dlg}次专属对话"
            detail += "，已熟识，勿当首次出场介绍]"

        detail += " ★在场"
        return detail

    def _format_npc_brief(self, npc, state, npc_states, npc_loc, encounter_counts, dialogue_counts):
        """近期相关但不在场的 NPC：中等信息。"""
        npc_id = npc["id"]
        npc_st = npc_states.get(npc_id, {})
        attitude = npc_st.get("attitude_toward_player", npc.get("attitude_toward_player", 50))
        three_dim = self._npc_three_dim_suffix(npc_id, state)
        encounter_count = encounter_counts.get(npc_id, 0)
        dn = state.get("display_names", {})
        loc_display = dn.get(npc_loc, npc_loc) if npc_loc else "未知"
        detail = f"- {npc_st.get('name') or npc.get('name', npc_id)}: 态度{attitude}/100{three_dim} (在{loc_display})"
        if encounter_count > 0:
            dlg = dialogue_counts.get(npc_id, 0)
            detail += f" [已交互{encounter_count}回合"
            if dlg:
                detail += f"+{dlg}对话"
            detail += "]"
        # 不在场 NPC 已知信息
        info_network = state.get("information_network", [])
        npc_known_info = [i for i in info_network if npc_id in i.get("known_by", [])]
        if npc_known_info:
            latest = npc_known_info[-1]
            fact = latest.get("fact", "")
            tag = "[谣言]" if latest.get("distortion", 0) >= 2 else ""
            detail += f" 已知:{tag}{fact[:40]}"
        detail += " ★不在场"
        return detail

    @staticmethod
    def _format_npc_minimal(npc, npc_states, npc_loc):
        """无知识库时其余 NPC 的极简一行。"""
        npc_id = npc["id"]
        npc_st = npc_states.get(npc_id, {})
        attitude = npc_st.get("attitude_toward_player", npc.get("attitude_toward_player", 50))
        loc_text = f" (在{npc_loc})" if npc_loc else ""
        return f"- {npc_st.get('name') or npc.get('name', npc_id)}: 态度{attitude}{loc_text}"

    def _build_active_states_section(self, state: dict) -> str:
        lines = ["## 当前持续状态"]
        active_ids = set(state.get("active_persistent_states", []))
        for ps in self.script.get("persistent_states", []):
            if ps["id"] in active_ids:
                lines.append(f"- {ps['description']}")

        # Day/night cycle effects
        game_time = state.get("game_time", "")
        if game_time:
            try:
                t = parse_time(game_time)
                if t:
                    hour = t.hour
                    if 6 <= hour < 12:
                        period = "上午"
                        period_effect = "大部分商店已开门，NPC正常活动"
                    elif 12 <= hour < 14:
                        period = "中午"
                        period_effect = "正午时分，部分人在休息"
                    elif 14 <= hour < 18:
                        period = "下午"
                        period_effect = "NPC正常活动，日照充足"
                    elif 18 <= hour < 22:
                        period = "晚上"
                        period_effect = "天色渐暗，商店陆续关门，酒馆/夜市活跃"
                    elif 22 <= hour or hour < 2:
                        period = "深夜"
                        period_effect = "大部分NPC已入睡，商店关闭，能见度低，危险增加"
                    else:
                        period = "凌晨"
                        period_effect = "天色最暗，几乎无人活动，适合隐秘行动"
                    lines.append(f"\n## 时段\n当前时段: {period} ({self.format_game_time(game_time)})")
                    lines.append(f"时段影响: {period_effect}")
            except Exception:
                pass

        # Weather with gameplay effects
        weather = state.get("current_weather")
        if weather:
            weather_effects = {
                "暴雨": "户外行动困难，视野受限，NPC减少外出",
                "大雪": "道路难行，移动时间翻倍，注意保暖",
                "极端天气（暴雨/大雪）": "户外行动极为困难，移动时间翻倍，有受伤风险",
                "阴雨": "氛围阴郁，户外略有不便",
                "多云": "天气一般",
                "晴朗": "天气晴好，适合活动",
            }
            effect = weather_effects.get(weather, "")
            lines.append(f"\n## 当前天气\n{weather}")
            if effect:
                lines.append(f"天气影响: {effect}")

        # Event countdowns (time pressure)
        self._append_event_countdowns(lines, state)

        # World properties
        world_props = state.get("world_properties", {})
        if world_props:
            lines.append("\n## 世界属性")
            for prop in self.script.get("world_properties", []):
                val = world_props.get(prop["id"], prop.get("value", ""))
                lines.append(f"- {prop.get('name', prop['id'])}: {val}")

        # Pending consequences
        if event_sections and event_sections.get("consequences"):
            lines.append(f"\n## 潜在后果（玩家不可见，仅供GM参考）\n{event_sections['consequences']}")

        # Active deadlines
        if event_sections and event_sections.get("deadlines"):
            lines.append(f"\n## 活跃时限（叙事中应自然体现紧迫感）\n{event_sections['deadlines']}")

        # Companions (AI should include them in scenes naturally)
        companion_ids = state.get("companions", [])
        if companion_ids:
            npcs_state = state.get("npcs", {})
            dn = state.get("display_names", {})
            lines.append("\n## 同行同伴（始终跟随主角，场景中应自然出现）")
            for cid in companion_ids:
                ns = npcs_state.get(cid, {})
                cname = dn.get(cid) or (ns.get("name", cid) if isinstance(ns, dict) else cid)
                personality = ns.get("personality", "") if isinstance(ns, dict) else ""
                lines.append(f"- {cname}" + (f"（{personality[:30]}）" if personality else ""))

        # Attribute alerts
        alerts = self._check_attribute_alerts(state)
        if alerts:
            lines.append("\n## 属性警告")
            for alert in alerts:
                lines.append(f"- {alert}")

        # Narrative threads — lorebook 存在时由 lorebook 按需激活
        if not self.lorebook and event_sections and event_sections.get("threads"):
            lines.append(f"\n## 叙事线索（帮助保持多线叙事的连贯性）\n{event_sections['threads']}")

        return "\n".join(lines)

    def _build_stage2_story_hint(self, state: dict) -> str:
        """Lightweight story tree hint for Stage 2 env/char prompts."""
        sts = state.get("story_tree_state")
        if not sts:
            return ""
        story_tree_def = self.script.get("story_tree", {})
        all_nodes = {}
        for tree in story_tree_def.get("trees", []):
            for node in tree.get("nodes", []):
                all_nodes[node["id"]] = node
        active = sts.get("active", [])
        if not active:
            return ""
        hints = []
        for nid in active[:3]:
            node = all_nodes.get(nid)
            if not node:
                continue
            direction = node.get("stage_direction", "") or node.get("narrative_hint", "")
            desc = direction or node.get("description", "") or node.get("name", nid)
            hints.append(f"- {desc[:80]}")
        if not hints:
            return ""
        return "当前剧情走向（描写应呼应这些方向）:\n" + "\n".join(hints)

    def _build_story_context_section(self, state: dict) -> str:
        """Build story tree context section for active storylines."""
        sts = state.get("story_tree_state")
        if not sts:
            return ""
        story_tree_def = self.script.get("story_tree", {})
        trees = story_tree_def.get("trees", [])
        if not trees:
            return ""

        active = set(sts.get("active", []))
        completed = set(sts.get("completed", []))
        choices_made = sts.get("choices_made", {})
        if not active and not completed:
            return ""

        lines = ["## 当前剧情线"]
        for tree in trees:
            tree_active = []
            tree_completed_recent = []
            for node in tree.get("nodes", []):
                nid = node["id"]
                if nid in active:
                    tree_active.append(node)
                elif nid in completed:
                    tree_completed_recent.append(node)

            if not tree_active and not tree_completed_recent:
                continue

            lines.append(f"\n### {tree.get('name', tree['id'])}")
            if tree.get("description"):
                lines.append(tree["description"])

            if tree_completed_recent:
                recent = tree_completed_recent[-3:]
                for node in recent:
                    nid = node["id"]
                    choice = choices_made.get(nid)
                    mark = "✓"
                    if choice:
                        choice_label = ""
                        for c in node.get("choices", []):
                            if c["id"] == choice:
                                choice_label = c.get("label", choice)
                                break
                        mark = f"✓ 选择了: {choice_label}"
                    lines.append(f"- [{mark}] {node.get('name', nid)}")

            for node in tree_active:
                ntype = node.get("type", "auto")
                if ntype == "quest":
                    lines.append(f"- [进行中] {node.get('name', node['id'])}: {node.get('description', '')}")
                elif ntype == "choice":
                    lines.append(f"- [待选择] {node.get('name', node['id'])}")
                elif ntype == "timed":
                    lines.append(f"- [进行中] {node.get('name', node['id'])}")

        return "\n".join(lines) if len(lines) > 1 else ""

    @staticmethod
    def _build_callback_hints_section(state: dict, turn_number: int) -> str:
        callbacks = state.get("narrative_callbacks", [])
        if not callbacks:
            return ""
        eligible = []
        for cb in callbacks:
            if cb.get("used"):
                continue
            turn_diff = turn_number - cb.get("turn", 0)
            if turn_diff < cb.get("callback_after", 3):
                continue
            if turn_diff > cb.get("callback_before", 25):
                continue
            eligible.append(cb)
        if not eligible:
            return ""
        priority_order = {"high": 0, "medium": 1, "low": 2}
        eligible.sort(key=lambda c: (priority_order.get(c.get("priority", "low"), 2), c.get("turn", 0)))
        hints = eligible[:3]
        lines = ["## 叙事回引提示（自然融入对话或描写，不要生硬提及）"]
        for h in hints:
            ago = turn_number - h.get("turn", 0)
            lines.append(f"- （{ago}回合前）{h['text']}")
        return "\n".join(lines)

    @staticmethod
    def _build_tone_section(state: dict) -> str:
        tone_parts = state.get("active_tone", [])
        if not tone_parts:
            return ""
        lines = ["## 本回合叙事基调"]
        for part in tone_parts:
            lines.append(f"- {part}")
        return "\n".join(lines)

    @staticmethod
    def _build_pacing_section(state: dict) -> str:
        pacing = state.get("pacing_state")
        if not pacing:
            return ""
        tension = pacing.get("tension", 50)
        trend = pacing.get("trend", "stable")
        trend_label = {"rising": "上升中", "falling": "下降中", "stable": "平稳"}.get(trend, trend)
        rec = pacing.get("recommendation", "")
        lines = [
            "## 叙事节奏提示",
            f"当前紧张度: {tension}/100（{trend_label}）",
        ]
        if tension < 40:
            lines.append(
                "节奏指导：当前处于低紧张期。叙事以白描和日常细节为主，"
                "用环境声响、光线变化、人物小动作传递氛围。"
                "如果骨架中有异常信息，以平淡笔触带过——一笔写完，不渲染，不重复。"
                "张力来自'正常表面下的不对劲'，而非显性对抗。"
            )
        elif tension < 70:
            lines.append(
                "节奏指导：中等紧张期。可以有信息交换和暗示性细节，"
                "但每回合只着重呈现一个信息点，其余一笔带过。"
                "NPC表现应符合其日常行为模式，异常举动需要充分铺垫。"
            )
        else:
            lines.append(
                "节奏指导：高紧张期。可以有明确冲突和对抗，节奏加快。"
                "但仍需保持物理真实性——不要为了戏剧效果牺牲合理性。"
            )
        if rec:
            lines.append(f"特别建议: {rec}")
        return "\n".join(lines)

    @staticmethod
    def _build_moral_alignment_section(state: dict) -> str:
        ma = state.get("moral_alignment")
        if not ma:
            return ""
        _LABELS = {
            "mercy_vs_cruelty": ("仁慈", "残忍"),
            "honesty_vs_deception": ("诚实", "欺骗"),
            "order_vs_chaos": ("秩序", "混沌"),
        }
        tags = []
        for axis, (pos, neg) in _LABELS.items():
            v = ma.get(axis, 0)
            if v >= 40:
                tags.append(pos)
            elif v <= -40:
                tags.append(neg)
        if not tags:
            return ""
        label = "、".join(tags)
        lines = [
            "## 玩家道德画像",
            f"倾向: {label}",
            "NPC 应根据玩家的道德声誉产生差异化反应（信任/警惕/尊敬/厌恶等）。",
        ]
        return "\n".join(lines)

    def _build_faction_reputation_section(self, state: dict) -> str:
        rep = state.get("faction_reputation", {})
        if not rep:
            return ""
        org_map = {}
        for org in self.script.get("organizations", []):
            org_map[org.get("id", "")] = org.get("name", org.get("id", ""))
        lines = ["## 阵营声望"]
        for fid, data in rep.items():
            fname = org_map.get(fid, state.get("display_names", {}).get(fid, fid))
            val = data.get("value", 50)
            title = data.get("title", "中立")
            lines.append(f"- {fname}: {title}({val})")
        return "\n".join(lines)

    @staticmethod
    def _build_memory_echo_section(state: dict) -> str:
        echoes = state.get("memory_echoes", [])
        if not echoes:
            return ""
        _WEIGHT_MAP = {"high": "强烈", "medium": "淡淡", "low": "隐约"}
        lines = ["## 记忆回响（请在叙事中自然融入这些回忆，不要生硬列举）"]
        for echo in echoes[:3]:
            weight = _WEIGHT_MAP.get(echo.get("emotional_weight", "medium"), "")
            lines.append(f"- [{echo.get('type', '')}] {echo.get('trigger', '')}：{echo.get('memory', '')}（{weight}的记忆，{echo.get('turns_ago', 0)}回合前）")
        return "\n".join(lines)

    @staticmethod
    def _build_time_atmosphere_section(state: dict) -> str:
        atmo = state.get("time_atmosphere")
        if not atmo:
            return ""
        weather = state.get("current_weather", "")
        lines = [f"## 时段氛围：{atmo.get('period_label', '')}（{atmo.get('light_level', '')}）"]
        lines.append(f"环境基调: {atmo.get('mood_hint', '')}")
        if weather:
            lines.append(f"天气: {weather}")
        effects = atmo.get("gameplay_effects", [])
        if effects:
            lines.append(f"环境影响: {', '.join(effects)}")
        lines.append("请让叙事的光线、氛围、声音描写与当前时段吻合。")
        return "\n".join(lines)

    @staticmethod
    def _build_difficulty_section(state: dict) -> str:
        da = state.get("difficulty_awareness")
        if not da or da.get("adjustment") == "neutral":
            return ""
        hint = da.get("hint_for_ai", "")
        if not hint:
            return ""
        momentum = da.get("player_momentum", "balanced")
        label = {"struggling": "苦战中", "dominating": "势如破竹"}.get(momentum, "")
        lines = [f"## 难度感知（{label}）", hint]
        return "\n".join(lines)

    @staticmethod
    def _build_npc_relationship_depth_section(state: dict) -> str:
        depths = state.get("npc_relationship_depths", {})
        if not depths:
            return ""
        dn = state.get("display_names", {})
        entries = []
        for npc_id, info in depths.items():
            name = dn.get(npc_id, npc_id)
            label = info.get("label", "陌生人")
            entries.append(f"- {name}: {label}（交流{info.get('talks', 0)}次）")
        if not entries:
            return ""
        lines = ["## NPC关系深度"] + entries
        lines.append("请根据关系深度调整NPC互动模式：陌生人应礼貌疏离，朋友/挚友应自然亲密、分享秘密或主动帮助。")
        return "\n".join(lines)

    @staticmethod
    def _build_discovery_hints_section(state: dict) -> str:
        dh = state.get("discovery_hints")
        if not dh:
            return ""
        nearby = dh.get("nearby_hints", [])
        if not nearby:
            return ""
        lines = ["## 附近的探索线索"]
        for h in nearby[:3]:
            lines.append(f"- {h.get('hint', '')}")
        lines.append("可以在选项中巧妙融入探索方向的暗示，引导玩家发现新区域或新NPC。")
        return "\n".join(lines)

    @staticmethod
    def _build_skill_growth_section(state: dict) -> str:
        growth = state.get("skill_growth", {})
        if not growth:
            return ""
        entries = [(name, info) for name, info in growth.items() if info.get("level", 0) > 0]
        if not entries:
            return ""
        lines = ["## 角色技能熟练度"]
        for name, info in entries:
            lv = info["level"]
            lines.append(f"- {name}: Lv.{lv}（+{info.get('bonus', 0)}加成）")
        lines.append("可在叙事中体现角色因经验积累而更加熟练的表现。")
        return "\n".join(lines)

    @staticmethod
    def _build_available_quests_section(state: dict) -> str:
        quests = state.get("available_quests", [])
        if not quests:
            return ""
        lines = ["## 可触发支线"]
        for q in quests[-3:]:
            name = q.get("name", q.get("id", ""))
            hint = q.get("trigger_hint", "")
            line = f"- {name}"
            if hint:
                line += f"（引入提示: {hint}）"
            lines.append(line)
        lines.append("请在叙事中自然引出上述支线机会，不要生硬提及。")
        return "\n".join(lines)

    def _append_event_countdowns(self, lines: list, state: dict):
        """Add countdown timers for upcoming one-time events."""
        game_time = state.get("game_time", "")
        if not game_time:
            return
        try:
            from datetime import datetime
            now = parse_time(game_time)
            if not now:
                return
            fired_events = set(state.get("fired_one_time_events", []))

            countdowns = []
            for evt in self.script.get("one_time_events", []):
                if evt["id"] in fired_events:
                    continue
                trigger = evt.get("trigger_time", "")
                if not trigger:
                    continue
                t = parse_time(trigger)
                if not t or t <= now:
                    continue
                delta = t - now
                hours = delta.total_seconds() / 3600
                if hours < 1:
                    time_left = f"{int(delta.total_seconds() / 60)}分钟"
                elif hours < 24:
                    time_left = f"{hours:.1f}小时"
                else:
                    time_left = f"{delta.days}天{int(hours % 24)}小时"
                name = evt.get("name", evt.get("description", evt["id"]))
                countdowns.append(f"- 距离「{name}」还有{time_left}")

            if countdowns:
                lines.append("\n## 时间压力")
                lines.extend(countdowns)
        except Exception:
            pass

    def _get_npc_name(self, npc_id: str) -> str:
        return self._npc_name_map.get(npc_id, npc_id)

    def build_nearby_npc_hint(self, nearby_npc_ids: list[str], state: dict) -> str:
        """生成同建筑不同房间 NPC 的提示文本。"""
        if not nearby_npc_ids:
            return ""
        npc_states = state.get("npcs", {})
        lines = []
        for npc_id in nearby_npc_ids[:6]:
            ns = npc_states.get(npc_id, {})
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", self._npc_name_map.get(npc_id, npc_id))
            room = ns.get("current_room", "")
            title = ns.get("title", "") or self._npc_by_id.get(npc_id, {}).get("title", "")
            desc = f"{name}"
            if title:
                desc += f"（{title}）"
            if room:
                desc += f" → {room}"
            lines.append(f"- {desc}")
        if not lines:
            return ""
        return (
            "\n同建筑但不在同一房间的NPC（不可直接对话或互动，"
            "如需接触须描写前往对方房间或叫来的过程）:\n"
            + "\n".join(lines)
        )

    def _get_present_npcs(self, state: dict, player_loc: str) -> list[str]:
        """返回当前与玩家同一位置的NPC名字列表。"""
        present = []
        game_time = state.get("game_time", "")
        for npc in self.script.get("npcs", []):
            npc_id = npc["id"]
            # 优先用state中的动态位置
            npc_st = state.get("npcs", {}).get(npc_id, {})
            cur_loc = npc_st.get("current_location", "")
            if cur_loc:
                if cur_loc.lower() == player_loc.lower():
                    present.append(npc.get("name", npc_id))
                continue
            # 其次检查schedule
            _ov = state.get("npc_schedule_overrides", {}).get(npc_id)
            schedule_loc = self._get_schedule_location(npc, game_time, condition_eval=self.condition_eval, overrides=_ov)
            if schedule_loc:
                if schedule_loc.lower() == player_loc.lower():
                    present.append(npc.get("name", npc_id))
                continue
            # 最后用default_location
            dl = npc.get("default_location", "")
            if dl and dl.lower() == player_loc.lower():
                present.append(npc.get("name", npc_id))
        # 也检查动态注册的NPC（不在script中的）
        for npc_id, info in state.get("npcs", {}).items():
            if npc_id not in self._npc_name_map:
                cur_loc = info.get("current_location", info.get("default_location", ""))
                if cur_loc and cur_loc.lower() == player_loc.lower():
                    present.append(info.get("name", npc_id))
        return present

    def _get_schedule_location(self, npc: dict, game_time: str,
                               condition_eval=None, overrides: list | None = None) -> str:
        """根据NPC的schedule和当前时间，返回NPC应在的位置。"""
        entry = self._get_schedule_entry(npc, game_time, condition_eval=condition_eval, overrides=overrides)
        return entry.get("location", "") if entry else ""

    def _check_attribute_alerts(self, state: dict) -> list[str]:
        """Check for attributes at critical levels."""
        alerts = []
        player = state.get("player", {})
        attrs = player.get("attributes", {})
        pc = self.script.get("player_character", {})
        attr_rules = pc.get("attributes", {})

        for k, v in attrs.items():
            rule = attr_rules.get(k, {})
            if not isinstance(rule, dict):
                continue
            mx = rule.get("max", 100)
            mn = rule.get("min", 0)
            val = v if isinstance(v, (int, float)) else v.get("value", 0) if isinstance(v, dict) else 0

            # Thresholds from script or defaults
            thresholds = rule.get("thresholds", [])
            for th in thresholds:
                th_val = th.get("value", 0)
                th_dir = th.get("direction", "below")  # "below" or "above"
                if th_dir == "below" and val <= th_val:
                    alerts.append(f"{k}已降至{val}(阈值{th_val}): {th.get('description', '危险')}")
                elif th_dir == "above" and val >= th_val:
                    alerts.append(f"{k}已升至{val}(阈值{th_val}): {th.get('description', '触发')}")

            # Default critical alerts if no thresholds defined
            if not thresholds:
                if val <= mn + (mx - mn) * 0.1:
                    alerts.append(f"⚠ {k}极低({val}/{mx})——应在叙事中体现负面影响")
                elif val >= mx - (mx - mn) * 0.1:
                    alerts.append(f"★ {k}极高({val}/{mx})——可在叙事中体现正面效果")

        return alerts

    def _build_lorebook_section(
        self, activated_lore: list[LorebookEntry] | None, position: str,
        pc_discovered_lore: list[str] | None = None,
        scope: str = "all",
        visibility_markers: bool = False,
    ) -> str:
        if not activated_lore:
            return ""
        entries = self.lorebook.get_entries_by_position(activated_lore, position)
        if not entries:
            return ""
        if scope != "all":
            entries = Lorebook.filter_by_visibility(entries, scope, pc_discovered_lore)
        return self.lorebook.format_for_prompt(entries, visibility_markers=visibility_markers)

    def _build_authors_note_section(self, note: str) -> str:
        if not note:
            return ""
        return f"## 创作者指令\n[以下是玩家的幕后创作指令，请在叙事中体现但不要直接提及]\n{note}"

    def build_npc_talk_prompt(self, state: dict, npc_id: str) -> str:
        """Build a system prompt for NPC-specific conversation.

        The AI plays the role of the NPC and responds in-character.
        Conversation does not advance game time or trigger events.
        """
        npc_script = None
        for npc in self.script.get("npcs", []):
            if npc["id"] == npc_id:
                npc_script = npc
                break
        if not npc_script:
            dyn = state.get("npcs", {}).get(npc_id)
            if not dyn or not isinstance(dyn, dict):
                return ""
            npc_script = {"id": npc_id, **dyn}

        npc_state = state.get("npcs", {}).get(npc_id, {})
        npc_name = npc_state.get("name") or npc_script.get("name", npc_id)
        attitude = npc_state.get("attitude_toward_player", npc_script.get("attitude_toward_player", 50))

        # Attitude description
        if attitude >= 80:
            att_desc = "非常信任和喜爱玩家，会热情相待、主动分享信息"
        elif attitude >= 60:
            att_desc = "对玩家友好，愿意交谈和提供帮助"
        elif attitude >= 40:
            att_desc = "对玩家态度中立，公事公办"
        elif attitude >= 20:
            att_desc = "对玩家冷淡警惕，不太愿意交流"
        else:
            att_desc = "对玩家敌对，可能拒绝交流或出言不逊"

        faction_rep = state.get("faction_reputation", {})
        npc_orgs = npc_state.get("organizations") or npc_script.get("organizations", [])
        for org_entry in (npc_orgs or [])[:1]:
            org_id = org_entry.get("id", org_entry) if isinstance(org_entry, dict) else org_entry
            rep_data = faction_rep.get(org_id)
            if isinstance(rep_data, dict):
                rep_val = rep_data.get("value", 50)
                if rep_val >= 70:
                    att_desc += "（所属阵营对玩家评价很高，即使首次见面也会表现友善）"
                elif rep_val <= 30:
                    att_desc += "（所属阵营对玩家评价很差，即使无私怨也会冷淡或敌视）"

        # Player info
        player = state.get("player", {})
        player_name = player.get("name", "玩家")

        # Relationship
        rel = player.get("relationships", {}).get(npc_id)
        rel_text = ""
        if isinstance(rel, dict) and any(k in rel for k in ("trust", "affection", "fear")):
            rel_text = f"信任{rel.get('trust', 50)}/好感{rel.get('affection', 50)}/畏惧{rel.get('fear', 0)}"
        elif rel is not None:
            rel_text = f"关系值: {rel}"

        # NPC schedule/current activity
        schedule_text = self._get_npc_schedule_text(npc_script, state.get("game_time", ""))

        # Location
        player_loc = player.get("location", "")

        parts = [
            f"你现在扮演「{npc_name}」与主角「{player_name}」进行对话。",
            f"\n## 你的身份",
            f"- 名字: {npc_name}",
            f"- 简介: {npc_state.get('bio') or npc_script.get('bio', '')}",
            f"- 性格: {npc_state.get('personality') or npc_script.get('personality', '')}",
            f"- 能力: {npc_state.get('capabilities') or npc_script.get('capabilities', '未知')}",
        ]
        current_mood = npc_state.get("current_mood", "")
        if current_mood:
            parts.append(f"- 当前情绪: {current_mood}（对话中应自然体现此情绪，但不必明说）")
        npc_title = npc_state.get("title") or npc_script.get("title", "")
        if npc_title:
            parts.append(f"- 头衔: {npc_title}")
        npc_orgs = npc_state.get("organizations") or npc_script.get("organizations", [])
        if npc_orgs:
            parts.append(f"- 所属组织: {self._format_org_tags(npc_orgs)}")
        npc_sup = npc_state.get("superior") or npc_script.get("superior", "")
        if npc_sup:
            sup_name = self._get_npc_name(npc_sup)
            parts.append(f"- 上级: {sup_name}")

        parts.append(f"\n## 对主角的态度")
        parts.append(f"- 态度值: {attitude}/100 → {att_desc}")
        if rel_text:
            parts.append(f"- 与主角的关系: {rel_text}")

        if schedule_text:
            parts.append(f"\n## 当前状态")
            parts.append(schedule_text)

        parts.append(f"\n## 场景背景")
        parts.append(f"- 当前时间: {self.format_game_time(state.get('game_time', '')) or '未知'}")
        parts.append(f"- 主角位置: {self._resolve_location_name(player_loc)}")
        parts.append(f"- 世界背景: {self.script.get('world_background', '')[:200]}")

        # 声纹锚定：根据性格派生说话风格提示
        personality = npc_state.get("personality") or npc_script.get("personality", "")
        voice_hint = self._derive_voice_hint(personality)
        if voice_hint:
            parts.append(f"\n## 说话风格")
            parts.append(f"你的说话风格: {voice_hint}")
            parts.append("严格遵循此风格，不要使用与之矛盾的语气词、句式或措辞。")

        # NPC 秘密层级
        secrets = npc_script.get("secrets", [])
        if secrets:
            unlocked_ids = set(state.get("npc_unlocked_secrets", {}).get(npc_id, []))
            revealed = [s for s in secrets if s.get("id") in unlocked_ids]
            hidden = [s for s in secrets if s.get("id") not in unlocked_ids]
            if revealed or hidden:
                parts.append("\n## 秘密与隐情")
            if revealed:
                parts.append("你信任主角到可以透露以下信息（可以在对话中自然提及，但不要一次全说）：")
                for s in revealed:
                    parts.append(f"- {s.get('content', '')}")
            if hidden:
                parts.append("以下话题你会回避、否认或转移，绝不透露实质内容：")
                for s in hidden:
                    parts.append(f"- {s.get('hint', '有所隐瞒')}")

        parts.append(f"""
## 对话规则
- 用第一人称以「{npc_name}」的身份回复，保持角色性格一致
- 根据态度值调整语气和内容：态度高=热情友好，态度低=冷淡/敌对
- 回复长度适中（50-200字）
- 对话不推进游戏时间，不触发事件
- 在回复末尾用以下JSON格式标注态度变化（如有）：

```npc_talk
{{"npc_attitude_changes": [{{"npc_id": "{npc_id}", "dimension": "trust或affection或fear", "change": 数值, "reason": "原因"}}]}}
```

如果本轮对话没有态度变化，省略此JSON块。""")

        return "\n".join(parts)

    def build_npc_speak_prompt(self, npc_script: dict, state: dict,
                                context: str, previous_speeches: list[dict]) -> dict:
        """Build prompt for a single NPC's independent speech in a group dialogue round.

        Returns {"messages": list, "system": str}.
        """
        npc_id = npc_script.get("id", "")
        npc_state = state.get("npcs", {}).get(npc_id, {})
        npc_name = npc_state.get("name") or npc_script.get("name", npc_id)
        personality = npc_state.get("personality") or npc_script.get("personality", "")
        bio = npc_state.get("bio") or npc_script.get("bio", "")
        attitude = npc_state.get("attitude_toward_player", npc_script.get("attitude_toward_player", 50))

        voice_hint = self._derive_voice_hint(personality)
        player_name = state.get("player", {}).get("name", "主角")

        system = (
            f"你扮演「{npc_name}」，在一个多人场景中发言。\n\n"
            f"## 角色\n"
            f"- 名字: {npc_name}\n"
            f"- 简介: {bio[:200]}\n"
            f"- 性格: {personality}\n"
            f"- 对主角「{player_name}」的态度: {attitude}/100\n"
        )
        if voice_hint:
            system += f"- 说话风格: {voice_hint}\n"
        system += (
            f"\n## 规则\n"
            f"- 只输出{npc_name}的一句对话，不要旁白或动作描写\n"
            f"- 保持角色性格一致\n"
            f"- 根据态度值调整语气\n"
            f"- 对话30-100字\n"
            f"- 不要重复前面角色已说的内容"
        )

        user_parts = []
        if context:
            user_parts.append(f"当前场景:\n{context}")
        if previous_speeches:
            lines = [f"{s['name']}: {s['speech']}" for s in previous_speeches]
            user_parts.append("已有发言:\n" + "\n".join(lines))
        user_parts.append(f"现在轮到{npc_name}发言。")

        messages = [{"role": "user", "content": "\n\n".join(user_parts)}]
        return {"messages": messages, "system": system}

    def _get_npc_schedule_text(self, npc_script: dict, game_time: str, overrides: list | None = None) -> str:
        """Get NPC's current activity based on schedule and game time."""
        entry = self._get_schedule_entry(npc_script, game_time, condition_eval=self.condition_eval, overrides=overrides)
        if not entry:
            return ""
        loc = entry.get("location", "")
        act = entry.get("activity", "")
        return f"- 当前位置: {loc}\n- 当前活动: {act}"

    def _get_schedule_entry(self, npc: dict, game_time: str,
                            condition_eval=None, overrides: list | None = None) -> dict | None:
        """Return the best matching schedule entry (with condition/priority support).

        overrides: runtime schedule entries injected by events, checked alongside
        script entries. Expired entries (expires < game_time) are skipped.
        """
        schedule = (npc.get("schedule") or []).copy()
        if overrides:
            for ov in overrides:
                expires = ov.get("expires", "")
                if expires and game_time and expires <= game_time:
                    continue
                schedule.append(ov)
        if not schedule or not game_time:
            return None
        try:
            t = parse_time(game_time)
            if not t:
                return None
            current_minutes = t.hour * 60 + t.minute
            candidates = []
            for entry in schedule:
                time_range = entry.get("time_range") or entry.get("time", "")
                if "-" not in time_range:
                    continue
                start_s, end_s = time_range.split("-", 1)
                sh, sm = (int(x) for x in start_s.strip().split(":"))
                eh, em = (int(x) for x in end_s.strip().split(":"))
                start_min = sh * 60 + sm
                end_min = eh * 60 + em
                if start_min <= end_min:
                    in_range = start_min <= current_minutes < end_min
                else:
                    in_range = current_minutes >= start_min or current_minutes < end_min
                if not in_range:
                    continue
                cond = entry.get("condition", "")
                if cond and condition_eval:
                    if not condition_eval(cond):
                        continue
                elif cond and not condition_eval:
                    continue
                candidates.append(entry)
            if not candidates:
                return None
            return max(candidates, key=lambda e: e.get("priority", 0))
        except Exception:
            return None

    def get_npc_current_info(self, npc_id: str, game_time: str, state: dict | None = None) -> dict | None:
        """Get NPC's current schedule info (location, activity) for display."""
        _overrides = (state or {}).get("npc_schedule_overrides", {}).get(npc_id) if state else None
        for npc in self.script.get("npcs", []):
            if npc["id"] == npc_id:
                text = self._get_npc_schedule_text(npc, game_time, overrides=_overrides)
                if text:
                    lines = text.split("\n")
                    loc = ""
                    act = ""
                    for line in lines:
                        if "当前位置:" in line:
                            loc = line.split("当前位置:", 1)[1].strip()
                        if "当前活动:" in line:
                            act = line.split("当前活动:", 1)[1].strip()
                    return {"location": loc, "activity": act}
                return None
        return None

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

    def refresh_kg_entries(self, changed_npc_ids: list[str], changed_org_ids: list[str]):
        """Refresh KG lorebook entries for NPCs/orgs whose data changed at runtime."""
        if not changed_npc_ids and not changed_org_ids:
            return
        maps = self._build_kg_helper_maps(self.script)
        npc_rels = self.script.get("npc_relationships", [])
        npc_by_id = maps["npc_map"]
        for npc_id in changed_npc_ids:
            npc = npc_by_id.get(npc_id)
            if not npc:
                continue
            content, keys, _ = self._build_kg_npc_content(npc, maps, npc_rels)
            self.lorebook.update_entry(f"_kg_npc_{npc_id}", content, keys)
        org_by_id = maps["org_by_id"]
        for org_id in changed_org_ids:
            org = org_by_id.get(org_id)
            if not org:
                continue
            content, keys = self._build_kg_org_content(org, maps)
            self.lorebook.update_entry(f"_kg_org_{org_id}", content, keys)

    def add_location_kg_entry(self, loc_id: str, loc_name: str, description: str = ""):
        """Add a lorebook entry for a dynamically revealed location."""
        entry_id = f"_kg_loc_{loc_id}"
        if any(e.id == entry_id for e in self.lorebook.entries):
            return
        loc = {"id": loc_id, "name": loc_name, "description": description}
        content, keys = self._build_kg_loc_content(loc, self._loc_name_map)
        self.lorebook.add_entries([{
            "id": entry_id,
            "keys": keys,
            "content": content,
            "entry_type": "location",
            "priority": 75,
            "position": "after_world",
            "constant": False,
            "enabled": True,
            "scan_depth": 3,
            "comment": f"地点: {loc_name}",
        }])

    def update_location_scene(self, loc_id: str, scene_details: dict):
        """Update location scene in KG entry (if exists) + maintain a dynamic lorebook entry for physical facts."""
        # --- KG entry update (existing behavior) ---
        kg_entry_id = f"_kg_loc_{loc_id}"
        for e in self.lorebook.entries:
            if e.id == kg_entry_id:
                parts = []
                if scene_details.get("atmosphere"):
                    parts.append(f"氛围: {scene_details['atmosphere']}")
                if scene_details.get("sensory"):
                    parts.append(f"感官: {scene_details['sensory']}")
                if parts:
                    scene_block = "\n[当前场景]\n" + "\n".join(parts)
                    base = re.sub(r'\n\[当前场景\]\n.*', '', e.content, flags=re.DOTALL)
                    self.lorebook.update_entry(kg_entry_id, base + scene_block)
                break

        # --- Dynamic lorebook entry for stable physical facts ---
        physical = scene_details.get("physical")
        if not physical or not isinstance(physical, dict):
            return
        phys_parts = []
        loc_name = self._resolve_location_name(loc_id)
        if physical.get("lighting"):
            phys_parts.append(f"照明: {physical['lighting']}")
        if physical.get("floor"):
            phys_parts.append(f"地面: {physical['floor']}")
        if physical.get("spatial_note"):
            phys_parts.append(f"空间: {physical['spatial_note']}")
        if not phys_parts:
            return
        content = f"{loc_name}的物理环境:\n" + "\n".join(phys_parts)
        scene_entry_id = f"_scene_loc_{loc_id}"
        existing = any(e.id == scene_entry_id for e in self.lorebook.entries)
        if existing:
            self.lorebook.update_entry(scene_entry_id, content)
        else:
            self.lorebook.add_entries([{
                "id": scene_entry_id,
                "keys": [loc_name, loc_id],
                "content": content,
                "comment": f"loc:{loc_id}",
                "priority": 80,
                "position": "after_world",
                "entry_type": "scene_physical",
            }])

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

    # ================================================================
    # Stage 1-3: 细粒度叙事流水线 prompt builders
    # ================================================================

    _VOICE_RULES: dict[str, str] = {
        "冷静": "短句，少语气词，陈述为主，偶尔反问",
        "热情": "长句，感叹号多，语气词丰富(呀/呢/啊)，爱用比喻",
        "傲慢": "居高临下，反问句多，'哼''切'，称呼对方用'你们'",
        "怯懦": "短句带省略号…，自我否定，'那个…''不好意思'",
        "豪爽": "大嗓门，粗犷用词，'哈哈''来来来'，不拐弯",
        "阴沉": "低语调，暗示性措辞，'呵''有意思'，话说一半",
        "天真": "简单句，好奇提问多，'哇''为什么呀'，口语化",
        "老练": "缓慢节奏，引用典故，'依我看''当年…'，不急不躁",
        "严肃": "命令式短句，军旅/官方用语，'报告''明白''执行'",
        "温柔": "轻声细语，关心式提问，'没事吧''慢慢来'，少否定",
    }

    def _derive_voice_hint(self, personality: str) -> str:
        """从NPC性格描述中派生说话风格提示。"""
        if not personality:
            return ""
        p_lower = personality.lower()
        for key, hint in self._VOICE_RULES.items():
            if key in p_lower:
                return hint
        return ""

    @staticmethod
    def _format_mechanics_brief(
        dice_results: list[dict] | None = None,
        check_result: dict | None = None,
        triggered_events: list[dict] | None = None,
        triggered_consequences: list[dict] | None = None,
    ) -> str:
        """为角色行为器提供精简机制摘要（仅标签，不含原始数值）。"""
        parts = []
        if triggered_events:
            descs = [e.get("description", "") for e in triggered_events if e.get("description")]
            if descs:
                parts.append("本轮触发事件: " + "; ".join(descs))
        if triggered_consequences:
            descs = [c.get("description", "") for c in triggered_consequences if c.get("description")]
            if descs:
                parts.append("本轮触发后果: " + "; ".join(descs))
        if dice_results:
            labels = []
            for d in dice_results:
                src = d.get("source_label", "")
                rl = d.get("range_label", "")
                if rl:
                    labels.append(f"[{src} → {rl}]" if src else f"[→ {rl}]")
            if labels:
                parts.append("本轮骰子效果: " + "; ".join(labels))
        if check_result:
            outcome_map = {
                "critical_success": "大成功", "success": "成功",
                "failure": "失败", "critical_failure": "大失败",
            }
            outcome = outcome_map.get(check_result.get("outcome", ""), "")
            if outcome:
                hint = check_result.get("narrative_hint", "")
                rule = check_result.get("rule", "default")
                rule_tag = {"brp": "[BRP] ", "dnd": "[D&D] "}.get(rule, "")
                parts.append(f"技能检定结果: {rule_tag}{outcome}" + (f"（{hint}）" if hint else ""))
        return "\n".join(parts)

    def _build_npc_voice_table(self, state: dict, present_npc_ids: list[str]) -> str:
        """构建在场NPC声纹速查表，用于角色行为器。"""
        if not present_npc_ids:
            return ""

        npc_states = state.get("npcs", {})
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        lines = ["在场NPC声纹速查："]
        for npc_id in present_npc_ids:
            ns = npc_states.get(npc_id, {})
            if not isinstance(ns, dict):
                continue
            npc_def = npc_defs.get(npc_id, {})
            name = ns.get("name", npc_id)
            title = ns.get("title", "") or npc_def.get("title", "") or npc_def.get("role", "")
            personality = ns.get("personality", "")
            voice_hint = self._derive_voice_hint(personality)
            attitude = ns.get("attitude_toward_player", 50)
            mood = ns.get("current_mood", "")

            title_str = f"（{title}）" if title else ""
            mood_str = f"，情绪={mood}" if mood else ""
            if voice_hint:
                lines.append(
                    f"- {name}{title_str}: 性格={personality} → 说话风格: {voice_hint}，当前态度={attitude}{mood_str}"
                )
            else:
                lines.append(
                    f"- {name}{title_str}: 性格={personality}，当前态度={attitude}{mood_str}（请根据性格推导说话风格）"
                )
        return "\n".join(lines)

    def build_plot_decision_prompt(
        self,
        player_action: str,
        state: dict,
        check_result: dict | None = None,
        dice_results: list[dict] | None = None,
        triggered_events: list[dict] | None = None,
        triggered_consequences: list[dict] | None = None,
        achieved_milestones: list[dict] | None = None,
        present_npc_ids: list[str] | None = None,
        history_context: str = "",
        recent_reasoning: list[dict] | None = None,
        game_tools: dict | None = None,
        prev_narrative_tail: str = "",
        prev_plot_decision: str = "",
        recent_narratives: list[dict] | None = None,
        event_sections: dict[str, str] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 1: 剧情决策器 - 只决定发生了什么，不写文学描写。"""
        system = (
            "你是游戏剧情决策器。根据玩家行动和当前局势，决定本回合的剧情走向。\n\n"
            "严格按以下格式输出，不要文学描写，不要修辞，不要比喻：\n\n"
            "[行动结果] 一句话：成功/失败/进行中，程度如何（思考、观察等无成败的行动写行为本身）\n"
            "[关键事件] 用短句列举发生了什么（每条≤20字）\n"
            "[NPC决策] 每个在场NPC的关键决定和原因（每人一行，≤30字，只列参与互动的NPC）\n"
            "[世界脉搏] 0-2条：世界中其他NPC/势力的同期动向，优先关联已有线索。无合理推断则写'无'\n"
            "[剧情推进] 一句话：当前局面走向哪里？悬念是什么？\n"
            "[情绪基调] 一个词（如焦灼/冷静/恐惧/荒诞/温情），不可与上一轮重复\n"
            "[场景约束] 结构化引用格式，只能引用已知来源的物件。格式:\n"
            "  位置=当前地点名; 物件=来自背包或场景描述的具体物品（逗号分隔）; "
            "光线=描述; 声音=描述\n"
            "  规则：'物件'字段只能列出①玩家携带物品中的物品 ②当前位置描述中提到的物品 "
            "③本回合行动结果中出现的物品。不可凭空发明新物件。无显著变化时写'无'\n\n"
            "约束：\n"
            "- 篇幅: 电报式短句，只陈述事实\n"
            "- 数量: [关键事件]≤3条，每条只含一件事（不可用分号/顿号串联多件事）; [NPC决策]≤3人(只列直接互动者); [世界脉搏]≤2条\n"
            "- 机制: 检定成功→行动达成，失败→受阻; 骰子高→正面，低→负面; NPC好感高→帮助，低→刁难\n"
            "- 行动: 玩家声明的行动是不可更改的前提（可失败但不可被替换为另一行动），不增加未声明的附带效果。"
            "行动分解：若玩家行动包含多个子步骤（用逗号/顿号/分号连接），逐一处理每个步骤的直接结果，不可合并、跳过或重新诠释。"
            "行动边界：主角只做玩家明确描述的事。'加上名字'≠'划掉别人再加名字'；'查看文件'≠'拿走文件'；'询问情况'≠'质问对方'。"
            "不可为行动添加玩家未声明的前置步骤、附带操作或升级解读\n"
            "- 信息: ①只使用主角当前已知的信息，引用的情报须可追溯到已知线索; "
            "②事件只有在场者和通讯可达者本回合得知; "
            "③玩家对某人用探查行为(观察/询问身份)→该人对玩家未知，不可自动识别\n"
            "- 角色: 优先使用已定义NPC（路人用泛称）。若必须引入有名有姓的新角色，在[NPC决策]中标注「新角色」并给出全名+职位。"
            "同一NPC在后续回合中必须使用完全一致的全名，不可混用全名和职称简称（如不可一轮叫'安秉哲'下轮叫'安科长'）; "
            "NPC对主角的称呼须匹配其头衔/职位\n"
            "- 场景: 单回合只允许一个核心场景，不可塞入多次地点转换或多轮独立对话。"
            "若玩家行动暗示跨越时间（'明天上班''第二天'），只写到达新时间点后的第一个场景，中间过渡一句带过。"
            "若玩家行动以睡觉/休息收尾，本回合叙事止于入睡那一刻，不要跳到醒来——醒来是下一回合的开场\n"
            "- 台词: NPC可以有对白，对话遵循日常逻辑而非谍战审讯；主角可有最小必要回应（报姓名、简短应答），但不可替主角做实质性表态、承诺或决定\n"
            "- 物理: ①时间推进须与行动规模匹配(移动A→B须合理路程时间); "
            "②刚离场NPC不可立刻返回; ③不同房间的NPC须描写移动过程才能出现\n"
            "- 时代: 科技/通讯/交通须与世界背景时代吻合\n"
            "- 时间线: 剧情决策必须对齐当前game_time。尚未发生的事件（trigger_time > 当前game_time）不可出现或被暗示已发生\n"
            "- 事件发明: [世界脉搏]可引入新信息，优先关联已有NPC/势力/事件线。"
            "以可观察的事实呈现（'第二辆车后座空了'），不直接揭示结论。"
            "写'无'是正常默认\n\n"
            "## 思考类行动示例\n"
            "✓ 玩家行动: 仔细回想今天的事情，理清头绪\n"
            "[行动结果] 主角开始梳理已知信息\n"
            "[关键事件] 注意到一号线索与三号线索的矛盾\n"
            "[NPC决策] 无\n"
            "[剧情推进] 信息仍有缺口，需要更多证据\n"
            "[情绪基调] 凝重\n\n"
            "✗ [关键事件] 推断出是X偷了钢笔并伪造了笔迹 ← 引入了线索列表中不存在的信息\n\n"
            "## 行动篡改示例（禁止）\n"
            "玩家行动: 在值班表上加上自己的名字\n"
            "✗ [行动结果] 主角划掉了两个人的名字，写上自己的 ← '划掉别人'是玩家未声明的操作\n"
            "✓ [行动结果] 主角在值班表空栏写上自己的名字\n\n"
            "玩家行动: 查看桌上的文件\n"
            "✗ [行动结果] 主角拿走了文件藏进口袋 ← '拿走'是玩家未声明的操作\n"
            "✓ [行动结果] 主角翻阅了文件内容"
        )
        system += CACHE_SENTINEL

        has_events = bool(triggered_events or triggered_consequences or achieved_milestones)
        has_dice = bool(dice_results or check_result)
        has_npcs = bool(present_npc_ids)
        if not has_events and not has_dice and not has_npcs:
            system += (
                "\n\n## 本轮回合类型：日常/独处\n"
                "无触发事件、无检定、无在场NPC。\n"
                "- [关键事件] 只写行动的直接结果（0-1条），没有外部事件发生就写'无'\n"
                "- [NPC决策] 写'无'\n"
                "- [世界脉搏] 写'无'\n"
                "- [场景约束] 仅描述当前物理环境，不引入任何新元素"
            )
        elif not has_npcs and not has_events:
            system += (
                "\n\n## 本轮回合类型：独处行动\n"
                "无在场NPC、无触发事件。\n"
                "- [NPC决策] 写'无'\n"
                "- [世界脉搏] 最多1条，无合理推断则写'无'"
            )

        if game_tools:
            tool_lines = ["\n\n可用工具（在输出中使用 [TOOL_CALL: name(args)] 格式调用）:"]
            for name, info in game_tools.items():
                tool_lines.append(f"- {name}: {info['desc']}  示例: [TOOL_CALL: {info['example']}]")
            system += "\n".join(tool_lines)

        # 构建用户消息 — XML 标签分区
        # 排列策略：稳定框架(primacy) → 中间上下文 → 动态行动(recency)

        # ─── <world> 区块：世界框架 ───
        player = state.get("player", {})
        pc = self.script.get("player_character", {})
        world_parts = []
        world_bg = self.script.get("world_background", "")
        if world_bg:
            world_parts.append(f"背景: {world_bg[:200]}")
        game_time = state.get("game_time", "")
        if game_time:
            world_parts.append(f"时间: {self.format_game_time(game_time) or game_time}")
        location_id = player.get("location", "")
        if location_id:
            loc_def = self._location_by_id.get(location_id, {})
            location_name = loc_def.get("name", location_id)
            location_desc = loc_def.get("description", "")
            world_parts.append(f"位置: {location_name}" + (f" - {location_desc[:80]}" if location_desc else ""))
        active_ids = set(state.get("active_persistent_states", []))
        if active_ids:
            ps_defs = {ps["id"]: ps for ps in self.script.get("persistent_states", [])}
            display_names = state.get("display_names", {})
            ps_descs = state.get("persistent_state_descriptions", {})
            ps_lines = []
            for sid in active_ids:
                ps_def = ps_defs.get(sid, {})
                name = ps_def.get("name") or display_names.get(sid, sid)
                desc = ps_def.get("description") or ps_descs.get(sid, "")
                ps_lines.append(f"{name}({desc[:60]})" if desc else name)
            if ps_lines:
                world_parts.append(f"⚠ 持续状态（不可无视）: {'; '.join(ps_lines[:5])}")

        # ─── <protagonist> 区块：主角信息 ───
        player_name = player.get("name", "主角")
        player_title = player.get("title", "") or pc.get("title", "")
        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )
        proto_parts = [f"主角: {player_name}"]
        if player_title:
            _surname = player_name[0] if player_name and player_name != "主角" else ""
            _short_title = player_title.split("（")[0].split("/")[-1]
            _call_hint = f"（NPC称呼: {_surname}{_short_title}）" if _surname else ""
            proto_parts.append(f"头衔/职位: {player_title}{_call_hint}")
        if not has_pc_lore:
            player_bio = player.get("bio", "") or pc.get("bio", "")
            if player_bio:
                proto_parts.append(f"身份: {player_bio[:100]}")
        inventory = state.get("inventory", [])
        if inventory:
            inv_items = [f"{it.get('item', '?')}x{it.get('quantity', 1)}" for it in inventory]
            proto_parts.append(f"携带物品: {', '.join(inv_items)}")
        else:
            proto_parts.append("携带物品: 无")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            proto_parts.append(f"当前房间: {player_room}")

        # ─── <npcs_present> 区块：在场NPC ───
        present = present_npc_ids or []
        npc_states = state.get("npcs", {})
        npc_defs = {n.get("id", n.get("name", "")): n for n in self.script.get("npcs", [])}
        player_rels = state.get("player", {}).get("relationships", {})
        npc_lines = []
        if present:
            header = (
                "同一建筑内NPC（不在玩家同一房间的NPC不可直接出现，须描写移动过程）:"
                if player_room else "在场NPC:"
            )
            npc_lines.append(header)
            for npc_id in present:
                ns = npc_states.get(npc_id, {})
                if not isinstance(ns, dict):
                    continue
                npc_def = npc_defs.get(npc_id, {})
                name = ns.get("name", npc_id)
                title = (
                    ns.get("title", "")
                    or npc_def.get("title", "")
                    or npc_def.get("role", "")
                    or npc_def.get("occupation", "")
                )
                personality = ns.get("personality", "")
                npc_room = ns.get("current_room", "")
                if not npc_room:
                    info = self.get_npc_current_info(npc_id, game_time, state=state)
                    if info and info.get("activity"):
                        npc_room = info["activity"][:20]
                rel = player_rels.get(npc_id, {})
                if isinstance(rel, dict) and any(k in rel for k in ("trust", "affection", "fear")):
                    att_str = f"T{rel.get('trust', 50)}/A{rel.get('affection', 50)}/F{rel.get('fear', 0)}"
                else:
                    att_str = f"态度{ns.get('attitude_toward_player', 50)}"
                rel_desc = rel.get("description", "") if isinstance(rel, dict) else ""
                rel_str = f"，关系={rel_desc}" if rel_desc else ""
                room_str = f" → {npc_room}" if npc_room else ""
                personality_str = f"性格={personality}，" if personality else ""
                npc_lines.append(f"- {name}{room_str}（{f'职位={title}，' if title else ''}{personality_str}{att_str}{rel_str}）")

        # ─── <npcs_offscreen> 区块：离场NPC ───
        offscreen_lines = []
        present_set = set(present)
        for npc_id in list(npc_defs.keys())[:20]:
            if npc_id in present_set:
                continue
            loc = self._resolve_npc_location(npc_id, state, game_time)
            if loc:
                name = npc_states.get(npc_id, {}).get("name", npc_id) if isinstance(npc_states.get(npc_id), dict) else npc_id
                loc_name = self._loc_name_map.get(loc, loc)
                offscreen_lines.append(f"{name}→{loc_name}")

        # ─── <knowledge> 区块：已知信息 ───
        knowledge_parts = []
        if event_sections and event_sections.get("clues"):
            knowledge_parts.append(f"已知线索: {event_sections['clues']}")
        if not self.lorebook:
            key_events = state.get("key_events", [])
            if key_events:
                recent_events = key_events[-8:]
                events_str = "；".join(f"第{e.get('turn', '?')}回合:{e.get('event', '')[:40]}" for e in recent_events)
                knowledge_parts.append(f"此前关键事件: {events_str}")
        persistent_facts = state.get("persistent_facts", [])
        if persistent_facts:
            facts_str = "；".join(
                f"[{f.get('type', '?')}]{f.get('fact', '')}" for f in persistent_facts[-15:]
            )
            knowledge_parts.append(f"持久事实（不可遗忘）: {facts_str}")
        bp = state.get("plot_blueprint", {})
        bp_threads = bp.get("plot_threads", [])
        if bp_threads:
            bp_lines = []
            for thread in bp_threads[:5]:
                tid = thread.get("id", "")
                stages = thread.get("stages", [])
                current_stage = None
                for stage in stages:
                    status = stage.get("status", "pending")
                    if status == "active":
                        current_stage = f"进行中: {stage.get('description', '')}"
                        break
                    elif status == "pending":
                        current_stage = f"待触发: {stage.get('description', '')}"
                        break
                if current_stage:
                    bp_lines.append(f"- {thread.get('name', tid)}: {current_stage}")
            if bp_lines:
                knowledge_parts.append("全局剧情走向（必须在本回合有所体现，至少一条）:\n" + "\n".join(bp_lines))

        # ─── <directives> 区块：动态提示/指令 ───
        directive_parts = []
        if history_context:
            directive_parts.append(history_context)
        if recent_reasoning:
            reasoning_lines = ["AI前几轮的决策思路（保持连贯，但不被束缚）:"]
            for r in recent_reasoning:
                reasoning_lines.append(f"第{r['turn']}回合思路: {r['reasoning']}")
            directive_parts.append("\n".join(reasoning_lines))
        play_style = state.get("play_style_summary")
        if play_style and isinstance(play_style, dict) and play_style.get("tag"):
            ps_tag = play_style["tag"]
            ps_desc = play_style.get("description", "")
            ps_line = f"玩家风格: {ps_tag}"
            if ps_desc:
                ps_line += f"（{ps_desc}）"
            ps_turn = play_style.get("turn", 0)
            approx_turn = len(state.get("adventure_log", []))
            if approx_turn - ps_turn >= 5 and approx_turn % 5 == 0:
                ps_line += "\n⚠ 本回合请设计至少一个选项挑战玩家的舒适区（与其惯性风格相反的行动方向），制造成长机会"
            else:
                ps_line += "\n剧情走向应自然回应此风格，但不必刻意迎合"
            directive_parts.append(ps_line)

        # ─── <previous_turn> 区块：上一轮骨架/叙事 ───
        prev_turn_text = ""
        if prev_plot_decision:
            prev_turn_text = prev_plot_decision
        elif prev_narrative_tail and not recent_narratives:
            prev_turn_text = f"上一轮叙事结尾:\n{prev_narrative_tail}"

        # ─── <turn_input> 区块：本轮输入 ───
        turn_parts = []
        # 检定/骰子
        dice_lines = []
        if check_result:
            outcome = check_result.get("outcome", "")
            success = "成功" if outcome in ("success", "critical_success") else "失败"
            if outcome == "critical_success":
                success = "大成功"
            elif outcome == "critical_failure":
                success = "大失败"
            rule = check_result.get("rule", "default")
            rule_hint = {"brp": " [BRP:骰点≤技能值=成功]",
                         "dnd": " [D&D:d20+修正≥DC=成功]"}.get(rule, "")
            dice_lines.append(f"检定结果: {success}{rule_hint}（{check_result.get('narrative_hint', '')}）")
        if dice_results:
            for d in dice_results:
                dice_lines.append(f"骰子: {d.get('source_label', '')} = {d.get('total', '')}（{d.get('range_label', '')}）")

        # 触发事件
        event_lines = []
        if triggered_events:
            event_lines.append("触发事件:")
            for ev in triggered_events:
                event_lines.append(f"- {ev.get('description', ev.get('event_id', ''))}")
        if triggered_consequences:
            event_lines.append("延迟后果触发:")
            for c in triggered_consequences:
                event_lines.append(f"- {c.get('description', '')}")
        if achieved_milestones:
            event_lines.append("达成里程碑:")
            for m in achieved_milestones:
                event_lines.append(f"- {m.get('description', m.get('id', ''))}")

        # 动态事件
        dyn_event_lines = []
        dyn_events = state.get("_dynamic_events_this_turn", [])
        if dyn_events:
            for de in dyn_events:
                cb_texts = [ef.get("text", "") for ef in de.get("effects", []) if ef.get("type") == "narrative_callback" and ef.get("text")]
                if cb_texts:
                    dyn_event_lines.append(f"- 事件 {de['id']}: {'; '.join(cb_texts)}")

        # 时限
        deadline_text = ""
        if event_sections and event_sections.get("deadlines"):
            deadline_text = f"NPC必须记得这些约定，不可做出矛盾安排:\n{event_sections['deadlines']}"

        # NPC介入
        intervention_lines = []
        interventions = state.get("npc_interventions", [])
        if interventions:
            for itv in interventions[:2]:
                urgency = "【必须立即发生】" if itv.get("urgency") == "high" else "【可自然织入】"
                intervention_lines.append(f"- {itv['npc_name']}（{itv['type']}）{urgency}: {itv.get('suggested_action', '')}。原因: {itv.get('reason', '')}")

        # 场景快照
        snapshot_text = ""
        if prev_narrative_tail and not recent_narratives:
            tail_anchor = prev_narrative_tail[-300:] if len(prev_narrative_tail) > 300 else prev_narrative_tail
            snapshot_text = tail_anchor

        # 玩家行动
        _manner_kws = {"暗中": "秘密行动", "秘密": "秘密行动", "偷偷": "秘密行动",
                       "伪装": "伪装身份", "假扮": "伪装身份", "公开": "公开行动",
                       "强行": "强硬行动", "小心": "谨慎行动", "谨慎": "谨慎行动"}
        _manner_tags = []
        for kw, label in _manner_kws.items():
            if kw in player_action:
                _manner_tags.append(label)
        _manner_hint = ""
        if _manner_tags:
            _manner_hint = f"⚠ 行动方式: {'/'.join(set(_manner_tags))}（必须在骨架中体现，不可忽略）\n"
        action_text = f"{_manner_hint}{player_action}"

        # ─── 组装最终 XML 结构 ───
        sections = []
        sections.append(_xml("world", "\n".join(world_parts)))
        sections.append(_xml("protagonist", "\n".join(proto_parts)))
        sections.append(_xml("npcs_present", "\n".join(npc_lines)))
        if offscreen_lines:
            sections.append(_xml("npcs_offscreen",
                f"不在玩家建筑内，玩家无法感知其活动——不可描写来自这些地点的声音/气味/视觉:\n{'; '.join(offscreen_lines[:8])}"))
        sections.append(_xml("knowledge", "\n".join(knowledge_parts)))
        sections.append(_xml("directives", "\n".join(directive_parts)))
        sections.append(_xml("previous_turn", prev_turn_text))

        # turn_input 子标签
        ti_parts = []
        if dice_lines:
            ti_parts.append(_xml("dice", "\n".join(dice_lines)))
        if event_lines:
            ti_parts.append(_xml("events", "\n".join(event_lines)))
        if dyn_event_lines:
            ti_parts.append(_xml("dynamic_events", "必须在叙事中自然体现:\n" + "\n".join(dyn_event_lines)))
        if deadline_text:
            ti_parts.append(_xml("deadlines", deadline_text))
        if intervention_lines:
            ti_parts.append(_xml("interventions", "必须在本回合剧情中体现:\n" + "\n".join(intervention_lines)))
        if snapshot_text:
            ti_parts.append(_xml("scene_snapshot", snapshot_text))
        ti_parts.append(_xml("player_action", action_text))
        sections.append(_xml("turn_input", "\n".join(ti_parts)))

        content = "\n\n".join(s for s in sections if s)

        messages = []
        if recent_narratives:
            # P1: 只保留最近 2 轮历史，更早的历史由 Agent 通过 recall_history 工具按需拉取
            recent_narratives = recent_narratives[-2:]
            for rn in recent_narratives:
                action = rn.get("action", "")
                narrative = rn.get("narrative", "")
                turn_label = rn.get("turn", "?")
                if not action:
                    action = "(场景描写)"
                messages.append({"role": "user", "content": f"[第{turn_label}回合 玩家行动] {action}"})
                if narrative:
                    messages.append({"role": "assistant", "content": f"[第{turn_label}回合 叙事结果]\n{narrative}"})
        messages.append({"role": "user", "content": content})
        return messages, system

    def build_env_render_prompt(
        self, plot_decision: str, state: dict
    ) -> tuple[list[dict], str]:
        """Stage 2a: 环境渲染器 - 生成环境/氛围描写。"""
        system = (
            "你是文字游戏的环境描写师。根据剧情骨架，生成沉浸式的环境和氛围描写。\n\n"
            "规则：\n"
            "- 用第二人称「你」\n"
            "- 覆盖2-3种感官（视觉、听觉、嗅觉、触觉、温度），每段不超过2种，"
            "分散在不同段落中，不要在同一段内堆叠所有感官\n"
            "- 与当前时段/天气/位置匹配\n"
            "- 天气连续性（硬约束）：如果提供了「当前天气」字段，严格按该天气描写；"
            "如果没有提供天气字段，必须与「上一轮氛围」中的天气/季节保持一致，"
            "绝对不可自行发明天气变化（如晴天突变暴风雪）。"
            "季节由游戏时间推断，10月=秋天，不可能有暴风雪\n"
            "- 环境描写应为剧情服务，但不可为了营造氛围而违反物理常识和季节规律\n"
            "- 200-350字，超过350字视为失败\n"
            "- 严禁写人物对话、NPC动作、NPC表情、角色互动——这些属于角色行为阶段\n"
            "- 你的输出中不应出现任何NPC的名字或对话引号\n"
            "- 环境描写要与即将发生的剧情相呼应（如紧张的剧情配压抑的环境）\n"
            "- 场景锚定：环境描写必须以当前位置的物理特征为核心（室内写室内，"
            "档案室不等于街头，会议室不等于教堂）。窗外远景只能占1-2句，不可喧宾夺主\n"
            "- 禁用词（出现即扣分）：仿佛、悄然、不禁、油然而生、不经意间、弥漫着、"
            "笼罩着、宛如、似乎在诉说、静静地、默默地、缓缓地、轻轻地、"
            "不可名状、莫名的、微妙的、在这一刻、命运的、岁月的\n"
            "- 句式禁令：禁止「……仿佛在……」「空气中弥漫着……的气息」「时间仿佛……」"
            "「一股……涌上心头」「在这……的……中」等模板句。"
            "用摄影机原则：只写镜头能拍到的东西"
        )
        system += CACHE_SENTINEL

        # 当前位置信息
        location_id = state.get("player", {}).get("location", "")
        loc_def = self._location_by_id.get(location_id, {})
        location_name = loc_def.get("name", location_id) if loc_def else location_id
        location_desc = loc_def.get("description", "") if loc_def else ""

        # 时段和天气
        game_time = state.get("game_time", "")
        time_of_day = self.format_game_time(game_time) if game_time else ""
        weather = state.get("current_weather", "")

        # 上一轮 scene_details
        scene_details = state.get("scene_details", {})
        prev_atmosphere = scene_details.get("atmosphere", "")
        prev_sensory = scene_details.get("sensory", "")

        # <scene> 区块
        scene_parts = [f"位置: {location_name}"]
        if location_desc:
            scene_parts.append(f"位置描述: {location_desc[:150]}")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            scene_parts.append(f"具体房间: {player_room}（环境描写应聚焦此房间）")
        world_bg = self.script.get("world_background", "")
        if world_bg:
            scene_parts.append(f"时代背景: {world_bg[:100]}")
        if time_of_day:
            scene_parts.append(f"时段: {time_of_day}")
        if game_time:
            try:
                month = int(game_time[5:7]) if len(game_time) >= 7 else 0
            except (ValueError, TypeError):
                month = 0
            season_map = {1: "冬季", 2: "冬季", 3: "初春", 4: "春季", 5: "春季",
                          6: "初夏", 7: "夏季", 8: "夏季", 9: "初秋", 10: "秋季",
                          11: "深秋", 12: "冬季"}
            season = season_map.get(month, "")
            year = game_time[:4] if len(game_time) >= 4 else ""
            if season:
                date_label = f"{year}年{month}月 {season}" if year else season
                scene_parts.append(f"⚠ 当前年月与季节: {date_label}（硬约束：天气、植被、气温必须符合该月份，"
                                   f"例如10月不可能有六月暴雨或盛夏酷暑）")
        if weather:
            scene_parts.append(f"天气: {weather}")
        else:
            scene_parts.append("天气: 未指定（沿用上一轮氛围中的天气，不可自行发明新天气）")

        # <continuity> 区块
        cont_parts = []
        loc_mem = state.get("location_memory", {}).get(location_id, [])
        if loc_mem:
            mem_lines = [f"- 第{m['turn']}回合: {m['text']}" for m in loc_mem[-3:]]
            cont_parts.append("此地的历史事件（环境中可微妙反映残留痕迹）:\n" + "\n".join(mem_lines))
        if prev_atmosphere or prev_sensory:
            cont_parts.append(f"上一轮氛围（仅供天气/光线连续参考）: {prev_atmosphere} {prev_sensory}")
            cont_parts.append("⚠ 位置隔离：上一轮的特有声源和物件（如某地的管风琴、某处的机器声）"
                              "不可搬到当前位置——不同地点的环境互相独立")
            _prev_text = f"{prev_atmosphere} {prev_sensory}"
            _imagery_kws = set()
            for _pat in [r'军靴声?', r'钟声', r'枪声', r'脚步声', r'雨[声滴]?',
                         r'霓虹[灯]?', r'烟[头蒂味]', r'月[光色]', r'阴云', r'探照灯',
                         r'荧光灯', r'铁丝网', r'水磨石', r'警报声?', r'挂钟']:
                _m = re.search(_pat, _prev_text)
                if _m:
                    _imagery_kws.add(_m.group())
            if _imagery_kws:
                cont_parts.append(f"⚠ 上轮已用意象（本轮换用其他）: {'、'.join(_imagery_kws)}")
        _story_hint = self._build_stage2_story_hint(state)
        if _story_hint:
            cont_parts.append(_story_hint)

        # 组装 XML
        sections = [
            _xml("scene", "\n".join(scene_parts)),
            _xml("continuity", "\n".join(cont_parts)),
            _xml("plot_decision", plot_decision),
        ]
        content = "\n\n".join(s for s in sections if s)
        messages = [{"role": "user", "content": content}]
        return messages, system

    def build_character_action_prompt(
        self, plot_decision: str, state: dict,
        present_npc_ids: list[str] | None = None,
        dice_results: list[dict] | None = None,
        check_result: dict | None = None,
        triggered_events: list[dict] | None = None,
        triggered_consequences: list[dict] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 2b: 角色行为器 - 生成NPC对话和动作描写。"""
        system = (
            "你是文字游戏的角色行为编导。根据剧情骨架，为场景中的角色写出具体对话和动作。\n\n"
            "规则：\n"
            "- 用第二人称「你」描写主角，第三人称描写NPC\n"
            "- 对话使用中文弯引号 “…” 包裹\n"
            "- 对话差异化：每个NPC的说话方式必须与其性格标签严格匹配（见下方声纹速查表）。"
            "不同NPC用不同的句长、语气词和肢体语言区分——"
            "粗人用断句口头禅，学者用书面长句，商人讲利弊得失\n"
            "- 主角的行为和心理反应要贴合其性格\n"
            "- 如果有机制结果（骰子/检定/事件），角色行为必须与之一致\n"
            "- NPC对主角的称呼必须与主角的身份/头衔一致，全程不得混淆\n"
            "- NPC行为必须匹配其社会地位和权力层级：举止、语气、态度应符合其身份定位。"
            "高位者对低位者的言行应体现其地位感（即使态度友善也不失身份），"
            "除非有明确的剧情理由需要打破常规\n"
            "- 只使用声纹速查表中列出的NPC，不得凭空捏造新角色。"
            "如果剧情骨架中引入了标注「新角色」的NPC，用其全名，不可用职称简称替代\n"
            "- 道具来源：角色行为中出现的关键道具（证件、武器、工具）必须有合理来源——"
            "要么是玩家已知拥有的物品，要么在叙事中交代获取过程，不可凭空出现\n"
            "- 泛称NPC一致性：若剧情骨架中的泛称NPC（如'亲信军官A'）在前轮叙事中已被赋予具体名字和特征，"
            "本轮必须沿用完全相同的名字、军衔、外貌特征，不可更改\n"
            "- 时代约束：角色使用的物品、通讯方式、交通工具必须与世界背景时代吻合\n"
            "- 时间线约束：只描写当前game_time已经发生或正在发生的事。未来事件的任何物理表现"
            "（枪声、爆炸、军车异动）不可提前出现在叙事中\n"
            "- 对话禁用模式：禁止所有NPC都用长句书面语；禁止每句对话后跟「他/她的眼中闪过一丝……」"
            "式的微表情解读；禁止对话中使用'你知道吗''说实话''坦白说'等AI偏好的填充词。"
            "真实对话：短句为主，有打断、有省略、有答非所问\n"
            "- 300-500字\n"
            "- 不写大段环境描写（那是环境渲染器的工作）"
        )
        system += CACHE_SENTINEL

        # 主角信息
        player = state.get("player", {})
        player_name = player.get("name", "主角")
        pc = self.script.get("player_character", {})
        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )
        if not has_pc_lore:
            player_personality = player.get("personality", "") or pc.get("personality", "")
            player_identity = pc.get("identity", "") or pc.get("background", "") or pc.get("bio", "")
        else:
            player_personality = ""
            player_identity = ""

        # NPC声纹速查表
        present = present_npc_ids or []
        voice_table = self._build_npc_voice_table(state, present)

        # NPC已交互次数（通过 met 状态和聊天历史判断）
        npc_states = state.get("npcs", {})
        interaction_info = []
        chat_histories = state.get("npc_chat_history", {})
        for npc_id in present:
            ns = npc_states.get(npc_id, {})
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", npc_id)
            met = ns.get("met", False)
            chat_count = len(chat_histories.get(npc_id, []))
            if met or chat_count > 0:
                interaction_info.append(f"{name}已交互过（勿重复自我介绍）")

        player_title = player.get("title", "") or pc.get("title", "")

        # <protagonist> 区块
        world_bg = self.script.get("world_background", "")
        proto_parts = []
        if world_bg:
            proto_parts.append(f"世界背景: {world_bg[:150]}")
        game_time = state.get("game_time", "")
        if game_time:
            proto_parts.append(f"当前时间: {self.format_game_time(game_time) or game_time}")
        proto_parts.append(f"主角: {player_name}")
        if player_title:
            _surname = player_name[0] if player_name and player_name != "主角" else ""
            _short_title = player_title.split("（")[0].split("/")[-1]
            _call_example = f"「{_surname}{_short_title}」" if _surname else f"「{_short_title}」"
            proto_parts.append(f"头衔/职位: {player_title}（NPC称呼主角为{_call_example}）")
        elif player_name and player_name != "主角":
            proto_parts.append(f"（NPC称呼主角时应使用姓氏「{player_name[0]}」加适当敬称）")
        if player_personality:
            proto_parts.append(f"性格: {player_personality}")
        if player_identity:
            proto_parts.append(f"身份: {player_identity}")
        inv_items = state.get("inventory", [])
        inv_str = ", ".join(it.get("item", "?") for it in inv_items) if inv_items else "无"
        proto_parts.append(f"携带物品: {inv_str}")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            proto_parts.append(f"当前房间: {player_room}")

        # <npc_profiles> 区块
        npc_profile_parts = []
        if voice_table:
            npc_profile_parts.append(voice_table)
        if interaction_info:
            npc_profile_parts.append(f"交互记录: {'; '.join(interaction_info)}")
        npc_rooms = []
        for npc_id in present:
            ns = npc_states.get(npc_id, {})
            if isinstance(ns, dict):
                room = ns.get("current_room", "")
                if room:
                    npc_rooms.append(f"{ns.get('name', npc_id)} → {room}")
        if npc_rooms:
            npc_profile_parts.append(f"NPC房间位置: {'; '.join(npc_rooms)}")

        # <mechanics> 区块
        mechanics = self._format_mechanics_brief(
            dice_results, check_result, triggered_events, triggered_consequences,
        )

        # <continuity> 区块
        _story_hint = self._build_stage2_story_hint(state)

        # 组装 XML
        sections = [
            _xml("protagonist", "\n".join(proto_parts)),
            _xml("npc_profiles", "\n".join(npc_profile_parts)),
            _xml("mechanics", f"角色行为必须与之一致:\n{mechanics}" if mechanics else ""),
            _xml("continuity", _story_hint or ""),
            _xml("plot_decision", plot_decision),
        ]
        content = "\n\n".join(s for s in sections if s)
        messages = [{"role": "user", "content": content}]
        return messages, system

    def _build_standard_narrative_system(
        self, scope: str, scene_type: str,
        negative_prompt: str, logit_bias_hint: str,
    ) -> str:
        """Stage 3 system prompt — moderate/major scope."""
        system = (
            "你是文字游戏的叙事整合师。将素材组合为流畅、有节奏感的叙事段落，"
            "自然承接你上一次的输出继续往下写。\n\n"
            "规则：\n"
            "- 自然编织环境描写和角色行为，不是简单拼接。去除重复和矛盾，加入过渡和节奏感\n"
            "- 可以微调措辞但不要改变剧情事实\n"
            "- 内容比例：环境/氛围描写不超过总篇幅的30%，剧情推进和角色互动占70%以上。"
            "信息密度优先于文学修饰\n"
            "- 素材一致性：不可引入与素材不一致的天气/季节变化；"
            "若叙事中已描写某个物理动作（拉上窗帘、关灯、关门），"
            "后续描写必须与该状态一致。听觉和嗅觉不受遮挡限制\n"
            "- 场景约束优先：骨架中的[场景约束]是物理硬约束（光线、声音、物件位置），"
            "环境素材和角色素材中与之矛盾的描写必须删除\n"
            "- 信息边界：确保叙事没有泄露主角不应知道的信息\n"
            "- 玩家自主权："
            "①叙事只覆盖玩家行动的直接后果，不可替主角做出新决定、移动到新地点、或开启玩家未发起的交互; "
            "②禁止替主角说出具体台词或表达具体观点——主角的话语权完全属于玩家; "
            "③叙事结束时主角应仍在行动发生的场景中; "
            "④单回合内地点不可变更超过一次（除非玩家行动明确描述了移动路线）; "
            "⑤行动忠实度：若玩家说'做A'，叙事中主角只做A及其必然伴随的物理前置步骤。"
            "不可自行添加A之外的独立决定或操作（如'加名字'变成'划掉别人并加名字'），也不可将A缩小或替换\n"
            "- 行动解读：尊重玩家行动中的隐含假设。若玩家对某人使用探查或试探行为，"
            "说明该人物对玩家是未知的，叙事中不可自动将其揭示为熟人\n"
            "- 身份约束：若剧情骨架中的人物以职位泛称出现（未使用具名NPC），"
            "叙事中也必须保持泛称，不可自行将其替换为已知NPC的名字\n"
            "- 时间一致：叙事中对时间流逝的描述必须与剧情骨架中的时间推进一致，不可夸大\n"
            "- 必须写到自然收束的句子结尾，不要在半句话中断\n"
            "- 叙事开头50字内必须回应玩家的行动和其直接后果，不要用环境描写开头\n"
            "- 对话要口语化，每个NPC说话风格不同。角色对话用中文弯引号 “……” 包裹\n"
            "- 意象和结尾多样性：不可重复前几轮的标志性意象和结尾方式\n"
            "- 直接输出最终叙事文本，不要任何JSON/标记/解释"
            "\n\n## 风格示例\n"
            "好的叙事段落（行动动词开头、零比喻、对话推进剧情）：\n"
            "\"你拨通加密专线，听筒里电流杂音嗡了两秒才接通。"
            "'有什么事？'对方的声音不耐烦，背景里隐约有人在说话。"
            "你报出代号，三秒沉默。他压低嗓子：'文件已经转移，别再打这个号码了。'线路断了。"
            "你盯着话筒上残留的指纹，烟灰缸里的烟头还没灭尽。隔壁房间传来打字机连续敲击的声音。\"\n\n"
            "坏的叙事段落（禁止模仿）：\n"
            "\"你缓缓推开那扇沉重的门，仿佛推开了命运的闸口。空气中弥漫着一股说不清的紧张气息，"
            "心中油然而生一种莫名的沉重。在这一刻，时间仿佛凝固了，"
            "窗外的月光如同一层薄纱，悄然笼罩着整个房间。\"\n\n"
            "好的叙事段落（探索场景）：\n"
            "\"档案柜第三层，标签写着'人事调动'。你抽出一份，纸页发黄，钢笔字迹是蓝黑色的。"
            "调令日期是上个月15号，签发人一栏盖着陆军本部的红章。你翻到第二页——"
            "那个名字在调出人员名单的第三行。\"\n\n"
            "好的叙事段落（社交场景）：\n"
            "\"'那批货的事。'金部长把茶杯转了半圈，拇指压着杯沿。"
            "'什么货？'你没接他的眼神。"
            "他等了三秒。'行，你装。'椅子往后一推，站起来时顺手把桌上的信封拍到你面前。"
            "信封没封口，露出半截照片的边角。\"\n\n"
            "## 禁用词与句式（出现即视为失败）\n"
            "词汇：仿佛、悄然、不禁、油然而生、不经意间、弥漫着、笼罩着、宛如、"
            "似乎在诉说、静静地、默默地、缓缓地、轻轻地、不可名状、莫名的、"
            "微妙的、在这一刻、时间仿佛凝固、命运的、岁月的\n"
            "句式：「……仿佛在……」「空气中弥漫着……」「时间仿佛……」"
            "「一股……涌上心头」「……为这个……平添了几分……」\n"
            "替代原则：摄影机原则——只写镜头能拍到的东西。"
            "「沉重的气氛」→「没人说话，烟灰缸满了」\n\n"
            "## 叙事克制原则\n"
            "- 大部分回合是'事情正常发生'——玩家做了什么，世界合理回应，仅此而已\n"
            "- 骨架中的[世界脉搏]异常信息用观察事实呈现（看到、听到），不要渲染放大\n"
            "- NPC动作和对话遵循日常逻辑，不是谍战审讯\n"
            "- 感官细节服务于场景真实感，不服务于制造悬疑\n"
            "- 回合结尾不需要cliffhanger。安静的收束比强行制造悬念更好"
        )
        system += CACHE_SENTINEL
        _rhythm_hints = {
            "combat": "\n\n## 节奏指引（战斗）\n用短句和断句制造紧迫感，动作描写干脆利落，不要在战斗中间插入大段环境或心理描写。",
            "exploration": "\n\n## 节奏指引（探索）\n发现关键事物时用短句突出，制造'发现感'。环境描写服务于线索呈现，不要喧宾夺主。",
            "social": "\n\n## 节奏指引（社交）\n以对话和人物反应驱动节奏，环境描写只在转场时出现。对话间留白，不要每句话后都跟心理旁白。",
        }
        if scene_type in _rhythm_hints:
            system += _rhythm_hints[scene_type]
        if negative_prompt:
            system += f"\n\n## 绝对禁止事项\n{negative_prompt}"
        if logit_bias_hint:
            system += f"\n\n{logit_bias_hint}"
        return system

    def _build_minor_narrative_system(
        self, negative_prompt: str, logit_bias_hint: str,
    ) -> str:
        """Stage 3 system prompt — minor scope (轻量行动)."""
        system = (
            "你是文字游戏的叙事整合师。本轮是轻量行动，直接描写行动结果即可。\n\n"
            "规则：\n"
            "- 简洁有力，写到自然收束的句子结尾\n"
            "- 叙事开头直接回应玩家行动及其后果\n"
            "- 信息边界：叙事不可泄露主角不应知道的信息\n"
            "- 禁止替主角做出新决定、说出具体台词——主角的话语权属于玩家\n"
            "- 行动忠实度：若玩家说'做A'，叙事中主角只做A及其必然伴随的物理前置步骤。"
            "不可自行添加A之外的独立决定或操作\n"
            "- 对话用中文弯引号 “……” 包裹\n"
            "- 直接输出叙事文本，不要任何JSON/标记/解释\n\n"
            "## 禁用词（出现即视为失败）\n"
            "仿佛、悄然、不禁、油然而生、不经意间、弥漫着、笼罩着、宛如、"
            "似乎在诉说、静静地、默默地、缓缓地、轻轻地、不可名状、莫名的、"
            "微妙的、在这一刻、时间仿佛凝固、命运的、岁月的"
        )
        system += CACHE_SENTINEL
        if negative_prompt:
            system += f"\n\n## 绝对禁止事项\n{negative_prompt}"
        if logit_bias_hint:
            system += f"\n\n{logit_bias_hint}"
        return system

    def build_narrative_compose_prompt(
        self,
        plot_decision: str,
        env_text: str,
        char_text: str,
        state: dict,
        recent_openings: list[str] | None = None,
        missing_env: bool = False,
        missing_char: bool = False,
        history_context: str = "",
        prev_narrative_tail: str = "",
        scene_type: str = "",
        prev_ending_type: str = "",
        authors_note: str = "",
        action_text: str = "",
        negative_prompt: str = "",
        logit_bias_hint: str = "",
        scope: str = "",
        estimated_minutes: int = 30,
        event_sections: dict[str, str] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 3: 叙事润色器 - 将素材整合为流畅完整叙事。"""
        if scope == "minor":
            system = self._build_minor_narrative_system(negative_prompt, logit_bias_hint)
        else:
            system = self._build_standard_narrative_system(
                scope, scene_type, negative_prompt, logit_bias_hint
            )
        # <scene_context> 区块
        ctx_parts = []
        game_time = state.get("game_time", "")
        if game_time:
            try:
                from datetime import datetime as _dt
                _gt = _dt.fromisoformat(game_time.replace("Z", "+00:00"))
                _month = _gt.month
                _year = str(_gt.year)
                season_map = {1: "严冬", 2: "冬末", 3: "初春", 4: "春季",
                              5: "暮春", 6: "初夏", 7: "盛夏", 8: "夏末",
                              9: "初秋", 10: "深秋", 11: "深秋", 12: "冬季"}
                _season = season_map.get(_month, "")
                ctx_parts.append(
                    f"回合起始时间: {_year}年{_month}月（{_season}），"
                    f"若剧情骨架推进了时间则以骨架为准"
                )
            except Exception:
                pass
        if scope:
            time_hints = {
                "minor": "本回合是短暂行动，叙事时间跨度不超过30分钟",
                "moderate": "本回合是常规行动，叙事可覆盖30分钟到2小时",
                "major": "本回合是重大行动，叙事可覆盖数小时甚至一整天，需要自然的时间过渡",
            }
            if scope in time_hints:
                ctx_parts.append(f"时间节奏: {time_hints[scope]}")
        if estimated_minutes and estimated_minutes > 0:
            if estimated_minutes >= 60:
                h = estimated_minutes // 60
                m = estimated_minutes % 60
                dur_str = f"{h}小时" + (f"{m}分钟" if m else "")
            else:
                dur_str = f"{estimated_minutes}分钟"
            ctx_parts.append(
                f"玩家行动预期时长: 约{dur_str}。叙事中的时间流逝应与此一致，"
                "若玩家明确要求等待特定时长，叙事必须覆盖该完整时段"
            )
        world_bg = self.script.get("world_background", "")
        if world_bg:
            ctx_parts.append(f"时代背景（地名/称谓须符合该时代）: {world_bg[:150]}")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            ctx_parts.append(f"玩家当前房间: {player_room}（叙事场景应以此房间为起点）")
        inventory = state.get("inventory", [])
        if inventory:
            inv_str = ", ".join(it.get("item", "?") for it in inventory[:8])
            ctx_parts.append(f"玩家携带物品: {inv_str}")

        # <naming_reference> 区块
        pc_info = state.get("player", {})
        pc_name = pc_info.get("name", "")
        pc_title = pc_info.get("title", "") or pc_info.get("role", "") or pc_info.get("occupation", "")
        title_pairs = []
        if pc_name:
            pc_label = f"★主角(第二人称\"你\"): {pc_name}"
            if pc_title:
                pc_label += f"（{pc_title}）"
            title_pairs.append(pc_label)
        npc_states = state.get("npcs", {})
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        for npc_id, ns in npc_states.items():
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", npc_id)
            npc_def = npc_defs.get(npc_id, {})
            title = (
                ns.get("title", "")
                or npc_def.get("title", "")
                or npc_def.get("role", "")
                or npc_def.get("occupation", "")
            )
            if title:
                title_pairs.append(f"{name}（{title}）")
            elif name != npc_id:
                title_pairs.append(name)
        naming_text = ""
        if title_pairs:
            naming_text = (
                f"叙事中必须使用正确称谓，NPC称呼主角时必须用主角的姓氏，不可混用其他NPC的姓氏:\n"
                f"{'; '.join(title_pairs)}"
            )

        # <continuity> 区块
        cont_parts = []
        if history_context:
            cont_parts.append(f"前情提要:\n{history_context}")
        if scope != "minor" and recent_openings:
            cont_parts.append(f"近几轮叙事开头（请避免雷同）: {' / '.join(recent_openings[-3:])}")
        if scope != "minor" and prev_ending_type:
            cont_parts.append(f"上一轮结尾类型: {prev_ending_type}（本轮必须使用不同类型）")
        pending = state.get("scene_details", {}).get("pending_tension", "")
        if pending:
            cont_parts.append(f"待定伏线（适度铺垫）: {pending}")

        # <materials> 区块
        mat_parts = []
        if env_text:
            mat_parts.append(_xml("environment", env_text))
        elif missing_env:
            mat_parts.append(_xml("environment", "（环境渲染失败，请根据剧情骨架中的位置/时段自行补充2-3种感官描写）"))
        if char_text:
            mat_parts.append(_xml("characters", char_text))
        elif missing_char:
            mat_parts.append(_xml("characters", "（角色行为生成失败，请根据剧情骨架中的NPC决策自行补充对话和动作描写）"))

        # 组装 XML
        sections = [
            _xml("scene_context", "\n".join(ctx_parts)),
            _xml("naming_reference", naming_text),
            _xml("continuity", "\n".join(cont_parts)),
            _xml("materials", "\n".join(mat_parts)),
            _xml("plot_decision", plot_decision),
            _xml("player_action", action_text if action_text else ""),
            _xml("authors_note", authors_note if authors_note else ""),
        ]
        content = "\n\n".join(s for s in sections if s)

        messages = []
        if prev_narrative_tail:
            messages.append({"role": "assistant", "content": prev_narrative_tail})
        messages.append({"role": "user", "content": content})
        return messages, system

    def build_narrative_prompt(
        self,
        ctx: dict,
        plot_decision: str,
        route: dict,
        state: dict,
        *,
        recent_openings: list[str] | None = None,
        history_context: str = "",
        prev_narrative_tail: str = "",
        prev_ending_type: str = "",
        authors_note: str = "",
        action_text: str = "",
        negative_prompt: str = "",
        logit_bias_hint: str = "",
        estimated_minutes: int = 30,
        event_sections: dict[str, str] | None = None,
    ) -> tuple[list[dict], str]:
        """P2 合并叙事：将环境渲染 + 角色行为 + 叙事整合合为单次调用。

        返回 (messages, system)，供 generate_with_tools 使用。
        LLM 在生成叙事文本的同时可通过工具调用 set_atmosphere / set_scene_image。
        """
        scope = route.get("scope", "moderate")
        scene_type = route.get("scene_type", "")
        skip_env = scene_type in ("social", "rest") or scope == "minor"

        # --- system prompt: 叙事整合师 + 环境/角色要求 ---
        if scope == "minor":
            system = self._build_minor_narrative_system(negative_prompt, logit_bias_hint)
        else:
            system = self._build_standard_narrative_system(
                scope, scene_type, negative_prompt, logit_bias_hint
            )
            # 追加环境描写要求（原 build_env_render_prompt 的核心规则）
            if not skip_env:
                system += (
                    "\n\n## 环境描写要求\n"
                    "在叙事中自然融入环境/氛围描写，遵守以下规则：\n"
                    "- 覆盖2-3种感官（视觉、听觉、嗅觉、触觉、温度），"
                    "分散在不同段落中，不要在同一段内堆叠所有感官\n"
                    "- 与当前时段/天气/位置匹配\n"
                    "- 天气连续性（硬约束）：严格按提供的天气描写；"
                    "无天气字段时沿用上一轮氛围中的天气，不可自行发明天气变化\n"
                    "- 环境描写为剧情服务，不可违反物理常识和季节规律\n"
                    "- 场景锚定：以当前位置的物理特征为核心，窗外远景只能占1-2句\n"
                    "- 环境描写不超过总篇幅的30%"
                )
            # 追加角色行为要求（原 build_character_action_prompt 的核心规则）
            system += (
                "\n\n## 角色行为要求\n"
                "在叙事中自然融入角色对话和动作，遵守以下规则：\n"
                "- 用第二人称「你」描写主角，第三人称描写NPC\n"
                "- 对话使用中文弯引号 “…” 包裹\n"
                "- 对话差异化：每个NPC的说话方式必须与其性格标签严格匹配。"
                "不同NPC用不同的句长、语气词和肢体语言区分\n"
                "- 如果有机制结果（骰子/检定/事件），角色行为必须与之一致\n"
                "- NPC对主角的称呼必须与主角的身份/头衔一致\n"
                "- NPC行为必须匹配其社会地位和权力层级\n"
                "- 只使用声纹速查表中列出的NPC，不得凭空捏造新角色\n"
                "- 道具来源：关键道具必须有合理来源\n"
                "- 时代约束：物品、通讯方式、交通工具必须与世界背景时代吻合\n"
                "- 对话禁用模式：禁止所有NPC都用长句书面语；"
                "禁止每句对话后跟微表情解读；真实对话短句为主"
            )

        system += CACHE_SENTINEL

        # --- user prompt 组装 ---

        # <scene_context> 区块（来自 narrative_compose + env_render）
        ctx_parts = []
        game_time = state.get("game_time", "")
        if game_time:
            try:
                from datetime import datetime as _dt
                _gt = _dt.fromisoformat(game_time.replace("Z", "+00:00"))
                _month = _gt.month
                _year = str(_gt.year)
                season_map = {1: "严冬", 2: "冬末", 3: "初春", 4: "春季",
                              5: "暮春", 6: "初夏", 7: "盛夏", 8: "夏末",
                              9: "初秋", 10: "深秋", 11: "深秋", 12: "冬季"}
                _season = season_map.get(_month, "")
                ctx_parts.append(
                    f"回合起始时间: {_year}年{_month}月（{_season}），"
                    f"若剧情骨架推进了时间则以骨架为准"
                )
            except Exception:
                pass
        time_of_day = self.format_game_time(game_time) if game_time else ""
        if time_of_day:
            ctx_parts.append(f"时段: {time_of_day}")
        if scope:
            time_hints = {
                "minor": "本回合是短暂行动，叙事时间跨度不超过30分钟",
                "moderate": "本回合是常规行动，叙事可覆盖30分钟到2小时",
                "major": "本回合是重大行动，叙事可覆盖数小时甚至一整天，需要自然的时间过渡",
            }
            if scope in time_hints:
                ctx_parts.append(f"时间节奏: {time_hints[scope]}")
        if estimated_minutes and estimated_minutes > 0:
            if estimated_minutes >= 60:
                h = estimated_minutes // 60
                m = estimated_minutes % 60
                dur_str = f"{h}小时" + (f"{m}分钟" if m else "")
            else:
                dur_str = f"{estimated_minutes}分钟"
            ctx_parts.append(
                f"玩家行动预期时长: 约{dur_str}。叙事中的时间流逝应与此一致，"
                "若玩家明确要求等待特定时长，叙事必须覆盖该完整时段"
            )

        # 位置信息（来自 env_render）
        location_id = state.get("player", {}).get("location", "")
        loc_def = self._location_by_id.get(location_id, {})
        location_name = loc_def.get("name", location_id) if loc_def else location_id
        location_desc = loc_def.get("description", "") if loc_def else ""
        ctx_parts.append(f"位置: {location_name}")
        if location_desc:
            ctx_parts.append(f"位置描述: {location_desc[:150]}")
        player_room = state.get("player", {}).get("current_room", "")
        if player_room:
            ctx_parts.append(f"玩家当前房间: {player_room}（叙事场景应以此房间为起点）")

        world_bg = self.script.get("world_background", "")
        if world_bg:
            ctx_parts.append(f"时代背景（地名/称谓须符合该时代）: {world_bg[:150]}")

        # 天气/季节（来自 env_render）
        weather = state.get("current_weather", "")
        if weather:
            ctx_parts.append(f"天气: {weather}")
        elif not skip_env:
            ctx_parts.append("天气: 未指定（沿用上一轮氛围中的天气，不可自行发明新天气）")
        if game_time:
            try:
                month = int(game_time[5:7]) if len(game_time) >= 7 else 0
            except (ValueError, TypeError):
                month = 0
            season_map2 = {1: "冬季", 2: "冬季", 3: "初春", 4: "春季", 5: "春季",
                           6: "初夏", 7: "夏季", 8: "夏季", 9: "初秋", 10: "秋季",
                           11: "深秋", 12: "冬季"}
            season2 = season_map2.get(month, "")
            year = game_time[:4] if len(game_time) >= 4 else ""
            if season2:
                date_label = f"{year}年{month}月 {season2}" if year else season2
                ctx_parts.append(
                    f"当前年月与季节: {date_label}（硬约束：天气、植被、气温必须符合该月份）"
                )
        inventory = state.get("inventory", [])
        if inventory:
            inv_str = ", ".join(it.get("item", "?") for it in inventory[:8])
            ctx_parts.append(f"玩家携带物品: {inv_str}")

        # <naming_reference> 区块（来自 narrative_compose）
        pc_info = state.get("player", {})
        pc_name = pc_info.get("name", "")
        pc_title = pc_info.get("title", "") or pc_info.get("role", "") or pc_info.get("occupation", "")
        title_pairs = []
        if pc_name:
            pc_label = f"★主角(第二人称\"你\"): {pc_name}"
            if pc_title:
                pc_label += f"（{pc_title}）"
            title_pairs.append(pc_label)
        npc_states = state.get("npcs", {})
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        for npc_id, ns in npc_states.items():
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", npc_id)
            npc_def = npc_defs.get(npc_id, {})
            title = (
                ns.get("title", "")
                or npc_def.get("title", "")
                or npc_def.get("role", "")
                or npc_def.get("occupation", "")
            )
            if title:
                title_pairs.append(f"{name}（{title}）")
            elif name != npc_id:
                title_pairs.append(name)
        naming_text = ""
        if title_pairs:
            naming_text = (
                f"叙事中必须使用正确称谓，NPC称呼主角时必须用主角的姓氏，不可混用其他NPC的姓氏:\n"
                f"{'; '.join(title_pairs)}"
            )

        # <protagonist> 区块（来自 character_action）
        player = state.get("player", {})
        player_name = player.get("name", "主角")
        pc = self.script.get("player_character", {})
        has_pc_lore = self.lorebook and any(
            e.id == "_pc_identity" and e.enabled for e in self.lorebook.entries
        )
        if not has_pc_lore:
            player_personality = player.get("personality", "") or pc.get("personality", "")
            player_identity = pc.get("identity", "") or pc.get("background", "") or pc.get("bio", "")
        else:
            player_personality = ""
            player_identity = ""

        proto_parts = []
        proto_parts.append(f"主角: {player_name}")
        player_title = player.get("title", "") or pc.get("title", "")
        if player_title:
            _surname = player_name[0] if player_name and player_name != "主角" else ""
            _short_title = player_title.split("（")[0].split("/")[-1]
            _call_example = f"「{_surname}{_short_title}」" if _surname else f"「{_short_title}」"
            proto_parts.append(f"头衔/职位: {player_title}（NPC称呼主角为{_call_example}）")
        elif player_name and player_name != "主角":
            proto_parts.append(f"（NPC称呼主角时应使用姓氏「{player_name[0]}」加适当敬称）")
        if player_personality:
            proto_parts.append(f"性格: {player_personality}")
        if player_identity:
            proto_parts.append(f"身份: {player_identity}")

        # <npc_profiles> 区块（来自 character_action）
        present_npc_ids = ctx.get("present_npc_ids") or []
        voice_table = self._build_npc_voice_table(state, present_npc_ids)
        npc_profile_parts = []
        if voice_table:
            npc_profile_parts.append(voice_table)
        # NPC交互记录
        chat_histories = state.get("npc_chat_history", {})
        interaction_info = []
        for npc_id in present_npc_ids:
            ns = npc_states.get(npc_id, {})
            if not isinstance(ns, dict):
                continue
            name = ns.get("name", npc_id)
            met = ns.get("met", False)
            chat_count = len(chat_histories.get(npc_id, []))
            if met or chat_count > 0:
                interaction_info.append(f"{name}已交互过（勿重复自我介绍）")
        if interaction_info:
            npc_profile_parts.append(f"交互记录: {'; '.join(interaction_info)}")
        npc_rooms = []
        for npc_id in present_npc_ids:
            ns = npc_states.get(npc_id, {})
            if isinstance(ns, dict):
                room = ns.get("current_room", "")
                if room:
                    npc_rooms.append(f"{ns.get('name', npc_id)} → {room}")
        if npc_rooms:
            npc_profile_parts.append(f"NPC房间位置: {'; '.join(npc_rooms)}")

        # <mechanics> 区块（来自 character_action）
        mechanics = self._format_mechanics_brief(
            ctx.get("dice_dicts"),
            ctx.get("check_result"),
            ctx.get("triggered_events"),
            ctx.get("triggered_consequences"),
        )

        # <continuity> 区块（来自 env_render + narrative_compose）
        cont_parts = []
        if history_context:
            cont_parts.append(f"前情提要:\n{history_context}")
        if scope != "minor" and recent_openings:
            cont_parts.append(f"近几轮叙事开头（请避免雷同）: {' / '.join(recent_openings[-3:])}")
        if scope != "minor" and prev_ending_type:
            cont_parts.append(f"上一轮结尾类型: {prev_ending_type}（本轮必须使用不同类型）")
        pending = state.get("scene_details", {}).get("pending_tension", "")
        if pending:
            cont_parts.append(f"待定伏线（适度铺垫）: {pending}")
        # 上一轮氛围（来自 env_render）
        if not skip_env:
            scene_details = state.get("scene_details", {})
            prev_atmosphere = scene_details.get("atmosphere", "")
            prev_sensory = scene_details.get("sensory", "")
            if prev_atmosphere or prev_sensory:
                cont_parts.append(f"上一轮氛围（仅供天气/光线连续参考）: {prev_atmosphere} {prev_sensory}")
            loc_mem = state.get("location_memory", {}).get(location_id, [])
            if loc_mem:
                mem_lines = [f"- 第{m['turn']}回合: {m['text']}" for m in loc_mem[-3:]]
                cont_parts.append("此地的历史事件:\n" + "\n".join(mem_lines))
        _story_hint = self._build_stage2_story_hint(state)
        if _story_hint:
            cont_parts.append(_story_hint)

        # 组装 XML
        sections = [
            _xml("scene_context", "\n".join(ctx_parts)),
            _xml("naming_reference", naming_text),
            _xml("protagonist", "\n".join(proto_parts)),
            _xml("npc_profiles", "\n".join(npc_profile_parts)),
            _xml("mechanics", f"角色行为必须与之一致:\n{mechanics}" if mechanics else ""),
            _xml("continuity", "\n".join(cont_parts)),
            _xml("plot_decision", plot_decision),
            _xml("player_action", action_text if action_text else ""),
            _xml("authors_note", authors_note if authors_note else ""),
        ]
        content = "\n\n".join(s for s in sections if s)

        messages = []
        if prev_narrative_tail:
            messages.append({"role": "assistant", "content": prev_narrative_tail})
        messages.append({"role": "user", "content": content})
        return messages, system

    def build_continue_prompt(
        self, current_narrative: str, state: dict,
    ) -> tuple[list[dict], str]:
        """Build prompt to continue/extend the current narrative without a new turn."""
        system = (
            "你是文字游戏的叙事续写师。玩家觉得上一段叙事太短或意犹未尽，请你自然地接续。\n\n"
            "规则：\n"
            "- 从上一段叙事末尾处无缝衔接，不要重复已有内容\n"
            "- 保持人称、时态、风格一致\n"
            "- 可以展开细节、补充感官描写、深化角色互动\n"
            "- 不要引入新的重大剧情转折或新角色\n"
            "- 输出300-600字的续写文本\n"
            "- 直接输出续写文本，不要任何JSON/标记/解释"
        )
        loc = state.get("player", {}).get("location", "未知")
        content = (
            f"当前场景: {loc}\n\n"
            f"== 需要续写的叙事 ==\n{current_narrative}\n\n"
            "请从上文末尾处自然续写，展开更多细节。"
        )
        messages = [{"role": "user", "content": content}]
        return messages, system

    def build_narrative_review_prompt(
        self, narrative: str, action_text: str,
        present_npcs: list[dict],
        pc_name: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 3.5: 叙事质量评审 prompt。"""
        system = (
            '你是叙事质量审核员。检查叙事是否违反以下规则，只返回紧凑JSON。\n\n'
            '检测项：\n'
            '1. protagonist_dialogue: 叙事中是否替主角说出了具体台词？'
            '主角可以有动作/表情/内心感受描写，但引号内的具体对白只有玩家行动中明确写出的才允许。'
            '用动作暗示（”点了点头”）或省略号不算违规。\n'
            '2. scene_packing: 叙事中是否包含多个不同物理空间的场景？'
            '单回合应只有一个核心场景，不可出现”然后去了X””接着来到Y”等地点切换。\n'
            '3. npc_behavior_break: NPC的行为是否与其角色定位严重不符？'
            '例如普通配角突然承担关键剧情功能、文职人员突然变成情报人员等。\n'
            '4. decision_overreach: 叙事是否替主角做了玩家行动中未声明的决定？'
            "例如玩家说'在名单上加名字'，叙事却描写主角划掉了别人的名字；"
            "玩家说'查看文件'，叙事却描写主角拿走了文件。"
            "判断标准：将叙事中主角的每个具体动作与玩家原文逐一比对，"
            "任何无法从玩家原文直接推导的物理操作都算违规。\n\n"
            '5. ai_cliche: 叙事中是否有典型的AI生成腔？'
            '检查：(a)是否使用了「仿佛」「悄然」「不禁」「油然而生」「弥漫着」「笼罩着」'
            '「不经意间」「宛如」「莫名的」等AI高频词汇（超过2处算违规）；'
            '(b)是否有模板句式如「空气中弥漫着……的气息」「时间仿佛……」「一股……涌上心头」；'
            '(c)环境描写是否占总篇幅40%以上（信息密度过低）。\n\n'
        )
        if pc_name:
            pc_surname = pc_name[0] if pc_name else ""
            system += (
                f'6. wrong_name: NPC在对话或称呼中是否叫错了主角的姓名？'
                f'主角姓名是「{pc_name}」（姓{pc_surname}），'
                f'NPC称呼主角时必须使用正确姓氏「{pc_surname}」。'
                f'若叙事中NPC用其他姓氏+职务来称呼主角（如用在场其他NPC的姓氏），算违规。\n\n'
            )
        system += (
            '输出格式：\n'
            '{“pass”:true/false,”violations”:[{“type”:”检测项名”,”detail”:”简述违规内容(≤30字)”}]}\n'
            '无违规时violations为空数组。只返回JSON。'
        )
        npc_info = ""
        if present_npcs:
            npc_lines = []
            for npc in present_npcs[:6]:
                role = npc.get("title") or npc.get("role") or npc.get("occupation") or ""
                line = f"- {npc.get('name', npc.get('id', '?'))}"
                if role:
                    line += f"（{role}）"
                npc_lines.append(line)
            npc_info = "\n".join(npc_lines)

        sections = [
            _xml("player_action", action_text),
            _xml("narrative", narrative),
            _xml("npcs_present", npc_info),
            _xml("protagonist_name", pc_name if pc_name else ""),
        ]
        content = "\n\n".join(s for s in sections if s)
        return [{"role": "user", "content": content}], system

    # ================================================================
    # Stage 4a/4b: 状态推演拆分 prompt builders
    # ================================================================

    def build_npc_reaction_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        present_npc_ids: list[str] | None = None,
        npc_history: str = "",
        npc_lore: list | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 4a: NPC关系推演 - 只处理NPC态度和关系变化。"""
        system = (
            "你是NPC情感分析师。严格根据叙事中实际描写的事件和行为判断NPC态度变化。\n"
            "不要推测或编造叙事中未提及的情节。只返回紧凑JSON。\n\n"
            "字段：\n"
            "- npc_attitude_changes: [{\"npc_id\":\"\",\"dimension\":\"trust|affection|fear\","
            "\"change\":±数值,\"reason\":\"10字\",\"opinion\":\"10字看法\","
            "\"relationship_desc\":\"关系性质变化\"}]\n"
            "- npc_met_changes: [{\"npc_id\":\"\",\"met\":true,\"reason\":\"首次互动方式\"}]\n"
            "- npc_relationship_updates: [{\"a\":\"npc1\",\"b\":\"npc2\","
            "\"type\":\"关系类型\",\"description\":\"\"}]\n"
            "- npc_interjections: [{\"npc_id\":\"\",\"text\":\"一句旁白或动作\"}]"
            " 在场但本回合未主要互动的NPC，根据其性格可能自发说一句话或做一个小动作"
            "（概率性的，不是每回合都有，只在自然合理时才输出。"
            "话痨度高的NPC更倾向于插话，沉默寡言的NPC极少主动发言）\n"
            "- scene_details: {\"npc_expressions\":[{\"npc_id\":\"\","
            "\"expression\":\"表情/情绪\"}],\"pending_tension\":\"悬念\"}\n\n"
            "数值通常±1到±5。重大互动（救命/背叛/告白）可±8到±15。"
            "大成功/大失败的行动对NPC印象冲击更大（可适当放大变化幅度）。无变化返回{}\n\n"
            "关键约束：\n"
            "- 态度变化必须基于【玩家行动】的实际意图，而非叙事中AI自行编写的戏剧化描写\n"
            "- 如果玩家行动本身是中性或积极的（如正常对话、请求帮助、表达善意），"
            "即使叙事描写了紧张氛围或NPC的负面反应，也不应降低NPC态度\n"
            "- 只有当玩家行动本身具有冒犯、威胁、欺骗、忽视等负面性质时，才可降低态度"
        )
        system += CACHE_SENTINEL

        # 在场NPC信息
        npc_states = state.get("npcs", {})
        player_rels = state.get("player", {}).get("relationships", {})
        dn = state.get("display_names", {})
        present = present_npc_ids or []
        npc_script_map = {n["id"]: n for n in self.script.get("npcs", []) if n.get("id")}

        npc_lines = []
        _npc_lore_ids = set()
        if self.lorebook:
            _npc_lore_ids = {
                e.id for e in self.lorebook.entries
                if e.id.startswith("_npc_profile_") and e.enabled
            }
        for npc_id in present:
            npc_st = npc_states.get(npc_id, {})
            if not isinstance(npc_st, dict):
                continue
            name = npc_st.get("name", dn.get(npc_id, npc_id))
            rel = player_rels.get(npc_id, {})
            if isinstance(rel, dict) and any(k in rel for k in ("trust", "affection", "fear")):
                t, a, f = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                att_str = f"T{t}/A{a}/F{f}"
            else:
                att_str = f"态度{npc_st.get('attitude_toward_player', 50)}"
            talk_val = npc_script_map.get(npc_id, {}).get("talkativeness", 50)
            talk_label = "话多" if talk_val >= 70 else ("沉默寡言" if talk_val <= 30 else "")
            talk_suffix = f" 话痨度={talk_label}" if talk_label else ""
            if f"_npc_profile_{npc_id}" in _npc_lore_ids:
                npc_lines.append(f"- {name}({npc_id}): {att_str}{talk_suffix}")
            else:
                personality = npc_st.get("personality", "")
                npc_lines.append(f"- {name}({npc_id}): {att_str} 性格={personality[:20]}{talk_suffix}")

        # 非在场但被提及的NPC
        for npc_id, npc_st in npc_states.items():
            if npc_id in present or not isinstance(npc_st, dict):
                continue
            name = npc_st.get("name", dn.get(npc_id, npc_id))
            if name in narrative:
                rel = player_rels.get(npc_id, {})
                if isinstance(rel, dict) and any(k in rel for k in ("trust", "affection", "fear")):
                    t, a, f = rel.get("trust", 50), rel.get("affection", 50), rel.get("fear", 0)
                    att_str = f"T{t}/A{a}/F{f}"
                else:
                    att_str = f"态度{npc_st.get('attitude_toward_player', 50)}"
                npc_lines.append(f"- {name}({npc_id}): {att_str} [不在场但被提及]")

        npc_text = "\n".join(npc_lines) if npc_lines else "无NPC"

        # NPC-NPC已知关系
        npc_rel_known = state.get("npc_relationships_known", {})
        npc_rel_lines = []
        for rel in npc_rel_known.values():
            if not isinstance(rel, dict):
                continue
            if "from" in rel:
                fn = dn.get(rel["from"], rel["from"])
                tn = dn.get(rel["to"], rel["to"])
                npc_rel_lines.append(f"- {fn}→{tn}: {rel.get('description', '')[:30]}")
            else:
                an = dn.get(rel.get("a", ""), rel.get("a", ""))
                bn = dn.get(rel.get("b", ""), rel.get("b", ""))
                npc_rel_lines.append(f"- {an}↔{bn}: {rel.get('type', '')} {rel.get('description', '')[:20]}")

        # 组装 XML
        sections = [_xml("npc_states", npc_text)]
        if npc_rel_lines:
            sections.append(_xml("npc_relationships", "\n".join(npc_rel_lines[:10])))
        sections.append(_xml("player_action", action_text if action_text else ""))
        sections.append(_xml("narrative", narrative))
        if check_result and check_result.get("outcome") in ("critical_success", "critical_failure"):
            crit_label = "大成功" if check_result["outcome"] == "critical_success" else "大失败"
            sections.append(_xml("check_result", f"{crit_label} — NPC对此印象深刻，态度变化幅度应适当放大。"))
        if npc_history:
            sections.append(_xml("npc_history", npc_history))
        if npc_lore:
            lore_lines = [f"[{e.comment or e.id}] {e.content[:100]}" for e in npc_lore[:5]]
            sections.append(_xml("npc_lore", "\n".join(lore_lines)))

        content = "\n\n".join(s for s in sections if s)
        messages = [{"role": "user", "content": content}]
        return messages, system

    def _build_world_state_user_message(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        group: str = "all",
        use_plot_decision: bool = False,
        event_sections: dict[str, str] | None = None,
    ) -> str:
        """构建 Stage 4b 系列共用的 user message。

        group="resource": 属性/持续状态/背包/位置（紧凑）
        group="spatial":  位置详情/已知地点/NPC房间
        group="temporal": 位置（紧凑）
        group="ext":  伏线/声望/blueprint/道德/历史词条
        group="all"/"core": 全量核心字段（兼容旧调用）
        """
        ctx = self._collect_state_context(state)
        _core_groups = ("all", "core", "resource", "spatial", "temporal", "world")
        include_resource = group in ("all", "core", "resource")
        include_spatial = group in ("all", "core", "spatial")
        include_ext = group in ("all", "ext")
        include_world = group in ("all", "core", "world")

        sections = []

        # ── resource 段落 ──
        if include_resource:
            res_parts = []
            attrs_text = "\n".join(ctx["attrs_lines"])
            res_parts.append(f"角色属性（含范围）:\n{attrs_text}")

            active_ids = ctx["active_ids"]
            predefined_ids = set()
            ps_lines = []
            for ps in self.script.get("persistent_states", []):
                predefined_ids.add(ps["id"])
                status = "激活" if ps["id"] in active_ids else "未激活"
                ps_lines.append(f"- {ps.get('name', ps['id'])}({ps['id']}): {status}")
            ps_descs = state.get("persistent_state_descriptions", {})
            for sid in active_ids - predefined_ids:
                desc = ps_descs.get(sid, "")
                label = f"- {sid}: 激活"
                if desc:
                    label += f" — {desc}"
                ps_lines.append(label)
            ps_text = "\n".join(ps_lines) if ps_lines else "无"
            res_parts.append(f"持续状态:\n{ps_text}")
            res_parts.append(f"背包: {ctx['inv_compact']}")
            sections.append(_xml("resource_state", "\n\n".join(res_parts)))

        # 位置信息
        loc_line = f"{ctx['location']}({ctx['location_id']})"
        if ctx["location_desc"]:
            loc_line += f" — {ctx['location_desc']}"
        player_room = state.get("player", {}).get("current_room", "")
        room_line = f"\n当前房间: {player_room}" if player_room else ""
        npc_states = state.get("npcs", {})
        if include_spatial:
            spatial_parts = [f"当前位置: {loc_line}{room_line}"]
            npc_room_entries = []
            for nid, ns in npc_states.items():
                if isinstance(ns, dict) and ns.get("current_room"):
                    npc_room_entries.append(f"{ns.get('name', nid)}→{ns['current_room']}")
            if npc_room_entries:
                spatial_parts.append(f"NPC房间: {'; '.join(npc_room_entries)}")
            visible_ids = set(state.get("visible_locations", []))
            loc_lines = []
            for loc in self.script.get("locations", []):
                if loc["id"] in visible_ids:
                    loc_lines.append(f"- {loc.get('name', loc['id'])}({loc['id']})")
            loc_text = "\n".join(loc_lines) if loc_lines else "无"
            spatial_parts.append(f"已知地点:\n{loc_text}")
            sections.append(_xml("spatial_state", "\n\n".join(spatial_parts)))
        else:
            sections.append(_xml("location", f"{loc_line}{room_line}"))

        # ── world 段落 ──
        if include_world:
            world_props = state.get("world_properties", {})
            wp_lines = []
            for prop in self.script.get("world_properties", []):
                val = world_props.get(prop["id"], prop.get("value", ""))
                wp_lines.append(f"- {prop.get('name', prop['id'])}({prop['id']}): {val}")
            predefined_wp_ids = {p["id"] for p in self.script.get("world_properties", [])}
            for wp_id, wp_val in world_props.items():
                if wp_id not in predefined_wp_ids:
                    dn = state.get("display_names", {}).get(wp_id, wp_id)
                    wp_lines.append(f"- {dn}({wp_id}): {wp_val}")
            wp_text = "\n".join(wp_lines) if wp_lines else "无"
            sections.append(_xml("world_properties", wp_text))

        # ── ext 段落 ──
        if include_ext:
            ext_parts = []
            _es = event_sections or {}
            ext_parts.append(_es.get("consequences", "待定伏线:\n无"))
            if _es.get("deadlines"):
                ext_parts.append(_es["deadlines"])
            if _es.get("threads"):
                ext_parts.append(_es["threads"])
            if _es.get("clues"):
                ext_parts.append(_es["clues"])
            faction_rep = state.get("faction_reputation", {})
            if faction_rep:
                org_name_map = {o.get("id", ""): o.get("name", o.get("id", "")) for o in self.script.get("organizations", [])}
                rep_lines = []
                for fid, fdata in faction_rep.items():
                    fname = org_name_map.get(fid, state.get("display_names", {}).get(fid, fid))
                    rep_lines.append(f"- {fname}({fid}): {fdata.get('title', '中立')}({fdata.get('value', 50)})")
                ext_parts.append(f"阵营声望:\n" + "\n".join(rep_lines))
            bp = state.get("plot_blueprint", {})
            bp_threads = bp.get("plot_threads", [])
            if bp_threads:
                bp_lines = []
                for thread in bp_threads[:5]:
                    stages = thread.get("stages", [])
                    for stage in stages:
                        status = stage.get("status", "pending")
                        if status == "active":
                            bp_lines.append(f"- {thread.get('name', thread.get('id',''))}: 进行中 — {stage.get('description', '')}")
                            break
                        elif status == "pending":
                            bp_lines.append(f"- {thread.get('name', thread.get('id',''))}: 待触发 — {stage.get('description', '')}")
                            break
                if bp_lines:
                    ext_parts.append("活跃剧情线:\n" + "\n".join(bp_lines))
            ma = state.get("moral_alignment")
            if ma:
                ma_text = f"仁慈↔残忍:{ma.get('mercy_vs_cruelty',0)} | 诚实↔欺骗:{ma.get('honesty_vs_deception',0)} | 秩序↔混沌:{ma.get('order_vs_chaos',0)}"
                ext_parts.append(f"道德画像(-100到+100):\n{ma_text}")
            sections.append(_xml("extended_state", "\n\n".join(ext_parts)))

        # ── 共享段落 ──
        if group == "temporal":
            gt = state.get("game_time", "")
            if gt:
                sections.append(_xml("game_time", f"{self.format_game_time(gt)} ({gt})"))

        sections.append(_xml("player_action", action_text if action_text else ""))

        if use_plot_decision:
            sections.append(_xml("plot_decision", narrative))
        else:
            sections.append(_xml("narrative", narrative))

        if include_ext:
            dynamic_lore = state.get("dynamic_lorebook", [])
            speculative_lines = []
            for dl in dynamic_lore:
                if dl.get("speculative"):
                    speculative_lines.append(f"- {dl['id']}: {dl.get('comment', dl.get('content', '')[:30])}")
            if speculative_lines:
                sections.append(_xml("speculative_lore", "可通过invalidate_lore废止:\n" + "\n".join(speculative_lines)))

        if check_result and check_result.get("outcome") in ("critical_success", "critical_failure"):
            crit_label = "大成功" if check_result["outcome"] == "critical_success" else "大失败"
            crit_attr = check_result.get("related_attribute", "")
            sections.append(_xml("check_result", f"{crit_label}（{crit_attr}）— 请在状态变更中体现持续影响（大成功=额外奖励/正面状态；大失败=惩罚/负面状态/添加后果）。"))

        return "\n\n".join(s for s in sections if s)

    def build_world_state_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 4b: 世界状态推演 - 除NPC关系外的所有状态变更。"""
        system = (
            "你是游戏状态引擎。严格根据叙事中实际描写的事件推演本回合世界状态变更。\n"
            "不要推测或编造叙事中未提及的情节。只返回紧凑JSON。\n\n"
            "字段：\n"
            + "\n".join(fdef["desc"] for fdef in self._FIELD_DEFS.values()) + "\n\n"
            "大成功时可额外给予奖励（物品/属性提升/开启新区域）；大失败时应施加惩罚（属性下降/丢失物品/添加负面状态）。\n"
            "省略无变化的字段。紧凑JSON输出。\n\n"
            "禁止事项：\n"
            "- activate_states 不得包含天气或时段（weather/dawn/dusk/night等）——这些由引擎自动管理\n"
            "- world_property_changes 只写宏观世界级变量（戒严等级、势力影响、社会压力等），不写物件状态（台灯/闹钟/门窗/文件散落等）——物件状态属于 scene_details\n"
            "- npc_attitude_changes 中的 npc_id 必须是已存在的NPC ID，不可凭空创造新角色ID\n"
            "- location_change 必须使用「已知地点」列表中的精确ID（如上文所示），禁止自行编造ID或使用下划线拼接的英文描述"
        )
        system += CACHE_SENTINEL

        content = self._build_world_state_user_message(narrative, action_text, state, check_result)
        messages = [{"role": "user", "content": content}]
        return messages, system

    # Stage 4b 字段→系统映射，用于动态裁剪
    # group: "resource" = 属性/物品/状态, "spatial" = 位置/场景, "temporal" = 时间/世界属性, "ext" = 低频扩展
    _FIELD_DEFS: dict[str, dict] = {
        "state_changes": {"system": None, "group": "resource", "desc": '- state_changes: [{"target":"player.属性名","op":"add","value":数值,"reason":"原因"}] 也可修改NPC属性: target="npcs.{npc_id}.字段名" op="set"'},
        "end_time": {"system": None, "group": "temporal", "desc": '- end_time: 叙事文本最后一句描写所对应的时刻，ISO 8601格式（如1979-10-26T10:00:00）。必填。严格对齐叙事末尾——如果叙事写到"躺在床上闭上眼"但尚未入睡，end_time就是闭眼那一刻（如23:30），绝不可跳到次日早晨。只有叙事明确描写了醒来/天亮的场景，end_time才可以是第二天。不要输出时段/时长，输出绝对时间。时间推进量必须与玩家行动规模匹配：签字/签收/查看=5~10分钟，对话/交谈=10~30分钟，移动/前往=30~120分钟，睡觉/过夜=6~10小时。玩家说"立即/马上"时推进量不超过15分钟'},
        "location_change": {"system": "location", "group": "spatial", "desc": '- location_change: 玩家在本回合结束时所在的位置ID（必须从上文「已知地点」列表复制精确ID，禁止自造）。必须与叙事中主角最终所在地点一致。若叙事中主角未移动则省略此字段'},
        "reveal_locations": {"system": "location", "group": "spatial", "desc": '- reveal_locations: [{"id":"位置ID","name":"显示名称"}]'},
        "inventory_changes": {"system": "inventory", "group": "resource", "desc": '- inventory_changes: [{"item":"物品名","action":"add|remove","quantity":1,"description":"可选简述"}] add限制：只能添加叙事中明确描写角色获取的物品，禁止添加叙事未提及的物品。移除物品时item字段必须与背包中的物品名完全一致（复制粘贴），不要缩写或改写'},
        "activate_states": {"system": None, "group": "resource", "desc": '- activate_states: [{"id":"状态ID","name":"中文显示名称（必填，不可用英文ID）","description":"一句话描述"}] 预定义状态可只填ID字符串'},
        "deactivate_states": {"system": None, "group": "resource", "desc": '- deactivate_states: [状态ID]'},
        "world_property_changes": {"system": None, "group": "world", "desc": '- world_property_changes: [{"id":"属性ID","value":"新值"}] 宏观世界级变量（戒严等级、势力影响等），不写物件状态'},
        "npc_location_changes": {"system": "npc", "group": "spatial", "desc": '- npc_location_changes: [{"npc_id":"","new_location":"位置ID（必须从已知地点列表复制）","reason":"离开/到达原因"}] 本回合在场NPC的位置变动'},
        "room_changes": {"system": None, "group": "spatial", "desc": '- room_changes: [{"id":"npc_id或player","new_room":"房间名","reason":"移动原因"}] 同一建筑内的房间级移动（如从办公室走到会议室）。player表示玩家自己的房间变化'},
        "scene_details": {"system": None, "group": "spatial", "desc": '- scene_details: {"atmosphere":"","sensory":"","key_objects":[],"physical":{"lighting":"照明条件(如日光灯/烛光/自然光)","floor":"地面材质(如地毯/大理石/木板)","spatial_note":"空间布局要点(如狭长走廊/开阔大厅，关键门窗朝向)"}} physical子对象描述当前地点的稳定物理特征，同一地点内不应改变；换地点时重新描述'},
        "game_over": {"system": "game_over", "group": "resource", "desc": '- game_over: {"reason":"","ending_type":""} 或省略'},
        "offscreen_npc_updates": {"system": None, "group": "ext", "desc": '- offscreen_npc_updates: [{"name":"NPC中文全名","action":"简述行动","location":"当前位置"}] 最多2-3个与本回合事件因果相关的离场NPC。name字段必须使用NPC的中文全名（如"车智澈""金载圭"），不要用英文ID。注意信息传播延迟：远处NPC不可能立即知道刚发生的事件并做出反应，除非有明确的通讯渠道'},
        "new_npcs": {"system": None, "group": "ext", "desc": '- new_npcs: [{"id":"英文蛇形ID","name":"完整全名（符合世界观的真实姓名，禁止用X某/小X/路人甲等占位符）","title":"职位/头衔","bio":"2-3句简介：外貌特征、背景经历、与当前场景的关联","personality":"性格关键词,逗号分隔","location":"必填-当前所在位置ID","trust":0-100初始信任,"affection":0-100初始好感,"fear":0-100初始畏惧}] 关键规则：叙事中任何有名有姓、有台词或有具体互动的非预设角色必须在此注册，否则下一回合将丢失该角色的身份信息。路人甲/背景群众不需要。三维关系值根据NPC对玩家的初次印象和互动方式判断'},
        "faction_reputation_changes": {"system": "faction", "group": "ext", "desc": '- faction_reputation_changes: [{"faction_id":"组织ID","change":±数值,"reason":"原因"}] 玩家与组织声望变化(±1到±15)'},
        "moral_alignment_changes": {"system": "moral", "group": "ext", "desc": '- moral_alignment_changes: [{"axis":"mercy_vs_cruelty|honesty_vs_deception|order_vs_chaos","change":±数值,"reason":"原因"}] 本回合玩家行为的道德维度影响(±1到±15)'},
        "recruit_companions": {"system": "companion", "group": "ext", "desc": '- recruit_companions: ["npc_id"] 当NPC明确表示愿意加入队伍/跟随玩家时添加'},
        "dismiss_companions": {"system": "companion", "group": "ext", "desc": '- dismiss_companions: ["npc_id"] 当同伴离队时添加'},
        "invalidate_lore": {"system": "_deprecated", "group": "ext", "desc": '- invalidate_lore: ["词条ID"]'},
    }

    @staticmethod
    def _format_events_for_prompt(event_data: dict) -> dict[str, str]:
        """将 event_engine.get_events_for_prompt() 输出格式化为 prompt 文本段。"""
        sections: dict[str, str] = {}
        cons = event_data.get("consequences", [])
        if cons:
            lines = [f"- {c['description']}（已酝酿{c.get('turns_waited', 0)}回合）" for c in cons]
            sections["consequences"] = "待定伏线:\n" + "\n".join(lines)
        dls = event_data.get("deadlines", [])
        if dls:
            lines = [f"- {d['description']}（剩余{d.get('turns_remaining', '?')}回合）" for d in dls]
            urgent = [d for d in dls if isinstance(d.get("turns_remaining"), int) and d["turns_remaining"] <= 3]
            if len(urgent) >= 2:
                names = "、".join(d["description"][:20] for d in urgent[:3])
                lines.append(f"\n⚠ 时间冲突：{names} 同时逼近！玩家不可能全部完成，必须在选项中体现取舍")
            sections["deadlines"] = "活跃限时:\n" + "\n".join(lines)
        threads = event_data.get("threads", [])
        if threads:
            active_t = [t for t in threads if t.get("status") == "active"]
            dormant_t = [t for t in threads if t.get("status") == "dormant"]
            t_lines = []
            for t in active_t:
                t_lines.append(f"- 【进行中】{t.get('name', t.get('id', ''))}：{t.get('description', '')}")
            for t in dormant_t[:2]:
                t_lines.append(f"- 【搁置】{t.get('name', t.get('id', ''))}：{t.get('description', '')}")
            if t_lines:
                sections["threads"] = "叙事线程:\n" + "\n".join(t_lines)
        clues = event_data.get("clues", [])
        if clues:
            recent = clues[-8:]
            lines = [f"- [{c.get('category', '?')}] {c.get('text', '')[:40]}（{c.get('source', '')}）" for c in recent]
            sections["clues"] = "已知线索:\n" + "\n".join(lines)
        nodes = event_data.get("story_nodes", [])
        if nodes:
            lines = [f"- {n.get('name', n['id'])}：{n.get('description', '')[:60]}" for n in nodes]
            sections["story_nodes"] = "当前剧情走向:\n" + "\n".join(lines)
        return sections

    _EVENT_STAGE_SYSTEM = (
        "你是游戏事件引擎。根据叙事内容和已有事件状态，输出事件变更。\n"
        "输出紧凑JSON，只含 event_changes 数组。\n\n"
        "每条变更格式:\n"
        '- {"action":"create","id":"唯一ID","category":"consequence|deadline|clue|thread","description":"描述","name":"名称(thread必填)","fields":{按类型补充}}\n'
        '- {"action":"update","id":"已有事件ID","fields":{"status":"新状态","description":"新描述",...}}\n'
        '- {"action":"delete","id":"已有事件ID"}\n\n'
        "category 字段说明:\n"
        "- consequence: 延迟后果。fields可含 trigger_chance(0-1), turns_delay, max_turns, importance(normal|high), related_npcs, on_trigger\n"
        "- deadline: 限时任务。fields可含 turns_remaining, on_expire:{description,state_changes,fire_events}, on_complete:{condition,description}\n"
        "- clue: 线索。fields可含 category(人物|地点|事件|物品|动机), source, linked_to:[已有线索ID]\n"
        "- thread: 剧情线。fields可含 status(active|dormant|resolved|failed), related_npcs, related_orgs\n\n"
        "规则:\n"
        "- 更新已有事件用 update（相同id），不要重复创建\n"
        "- 如果叙事中玩家已经完成了某个 deadline 描述的目标，必须 update 它的 status 为 completed\n"
        "- thread resolved/failed 表示结束\n"
        "- consequence 仅在叙事暗示延迟后果时创建\n"
        "- deadline 仅在叙事中有明确时间压力时创建\n"
        "- clue 仅提取有调查价值的信息\n"
        '省略无变化时输出 {"event_changes":[]}\n'
    ) + CACHE_SENTINEL

    def build_event_stage_prompt(
        self, narrative: str, action_text: str,
        event_data: dict, parsed_summary: str = "",
    ) -> tuple[list[dict], str]:
        """异步事件阶段 prompt：统一的 event_changes 格式。"""
        event_sections = self._format_events_for_prompt(event_data)
        xml_parts = []
        if action_text:
            xml_parts.append(_xml("player_action", action_text))
        xml_parts.append(_xml("narrative", narrative))
        if parsed_summary:
            xml_parts.append(_xml("state_summary", parsed_summary))
        event_parts = []
        for key in ("consequences", "deadlines", "threads", "clues"):
            if event_sections.get(key):
                event_parts.append(event_sections[key])
        if event_parts:
            xml_parts.append(_xml("existing_events", "\n\n".join(event_parts)))
        content = "\n\n".join(s for s in xml_parts if s)
        return [{"role": "user", "content": content}], self._EVENT_STAGE_SYSTEM

    def build_pruned_world_state_prompt(
        self, narrative: str, action_text: str, state: dict,
        active_systems: list[str],
        check_result: dict | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 4b 动态裁剪版：只包含 route.systems 命中的字段说明。"""
        active_set = set(active_systems)
        field_lines = []
        for fdef in self._FIELD_DEFS.values():
            if fdef["system"] is None or fdef["system"] in active_set:
                field_lines.append(fdef["desc"])

        system = (
            "你是游戏状态引擎。严格根据叙事中实际描写的事件推演本回合世界状态变更。\n"
            "不要推测或编造叙事中未提及的情节。只返回紧凑JSON。\n\n"
            "字段：\n" + "\n".join(field_lines) + "\n\n"
            "大成功时可额外给予奖励（物品/属性提升/开启新区域）；大失败时应施加惩罚（属性下降/丢失物品/添加负面状态）。\n"
            "省略无变化的字段。紧凑JSON输出。"
        )

        content = self._build_world_state_user_message(narrative, action_text, state, check_result)
        messages = [{"role": "user", "content": content}]
        return messages, system

    _GROUP_META = {
        "resource": ("资源状态引擎", "属性/物品/持续状态/胜负"),
        "spatial":  ("空间状态引擎", "位置移动/地点发现/NPC位置/房间/场景描写"),
        "temporal": ("时间状态引擎", "时间推进"),
        "world":    ("世界属性引擎", "宏观世界变量变化"),
        "ext":      ("扩展状态引擎", "后果/NPC/阵营/线索/同伴/剧情线"),
    }

    def _build_world_state_system(self, group: str, active_systems: list[str] | None = None) -> str:
        """构建指定 group 的 Stage 4b system prompt，可选 route pruning。"""
        active_set = set(active_systems) if active_systems else None
        field_lines = []
        for fdef in self._FIELD_DEFS.values():
            if fdef["group"] != group:
                continue
            if active_set is not None and fdef["system"] is not None and fdef["system"] not in active_set:
                continue
            field_lines.append(fdef["desc"])

        role, focus = self._GROUP_META.get(group, ("状态引擎", "状态变更"))
        system = (
            f"你是游戏{role}。严格根据叙事中实际描写的事件推演本回合的{focus}变更。\n"
            "不要推测或编造叙事中未提及的情节。只返回紧凑JSON。\n\n"
            "字段：\n" + "\n".join(field_lines) + "\n\n"
        )
        if group == "resource":
            system += (
                "物品变更原则：只有叙事中明确描写了角色获得（拾取/购买/赠予/搜获/制作）的物品才能add；"
                "不得凭空发明叙事中未出现的物品。\n"
                "仅当下方标注[检定结果: 大成功]时才可额外给予奖励；"
                "仅当标注[检定结果: 大失败]时才应施加惩罚（属性下降/丢失物品/添加负面状态）。\n"
            )
        if group == "temporal":
            system += (
                "关键：end_time 是叙事结束时的绝对时间戳，不是时长。"
                "找到叙事最后场景的时间点，直接输出该时间。"
                "当前游戏时间已在下方提供。\n"
            )
        if group == "world":
            system += (
                "只修改叙事中有明确变化依据的世界属性。"
                "下方提供了当前世界属性列表及其值。如无变化则返回空JSON {}。\n"
            )
        system += "省略无变化的字段。紧凑JSON输出。"
        system += CACHE_SENTINEL
        return system

    def build_world_state_core_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-core: 兼容入口，委托 resource 引擎。"""
        return self.build_world_state_resource_prompt(
            narrative, action_text, state, check_result, active_systems, plot_decision,
        )

    def build_world_state_resource_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-resource: 属性/物品/持续状态/胜负。"""
        system = self._build_world_state_system("resource", active_systems)
        source = plot_decision if plot_decision else narrative
        use_pd = bool(plot_decision)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="resource", use_plot_decision=use_pd)
        return [{"role": "user", "content": content}], system

    def build_world_state_spatial_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-spatial: 位置移动/地点发现/NPC位置/房间/场景描写。"""
        system = self._build_world_state_system("spatial", active_systems)
        source = plot_decision if plot_decision else narrative
        use_pd = bool(plot_decision)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="spatial", use_plot_decision=use_pd)
        return [{"role": "user", "content": content}], system

    def build_world_state_temporal_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-temporal: 时间推进。"""
        system = self._build_world_state_system("temporal", active_systems)
        # end_time 须从叙事全文推断（具体钟表时间在 Stage 2a/3 中产生，骨架中没有）
        source = narrative if narrative else plot_decision
        use_pd = not bool(narrative)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="temporal", use_plot_decision=use_pd)
        return [{"role": "user", "content": content}], system

    def build_world_state_world_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 4b-world: 世界属性变化。"""
        system = self._build_world_state_system("world", active_systems)
        source = plot_decision if plot_decision else narrative
        use_pd = bool(plot_decision)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="world", use_plot_decision=use_pd)
        return [{"role": "user", "content": content}], system

    def build_world_state_ext_prompt(
        self, narrative: str, action_text: str, state: dict,
        check_result: dict | None = None,
        active_systems: list[str] | None = None,
        plot_decision: str = "",
        event_sections: dict[str, str] | None = None,
    ) -> tuple[list[dict], str]:
        """Stage 4b-ext: 扩展状态 — NPC/阵营/同伴。"""
        system = self._build_world_state_system("ext", active_systems)
        source = plot_decision if plot_decision else narrative
        use_pd = bool(plot_decision)
        content = self._build_world_state_user_message(source, action_text, state, check_result, group="ext", use_plot_decision=use_pd, event_sections=event_sections)
        return [{"role": "user", "content": content}], system

    def build_merged_stage2_prompt(
        self, plot_decision: str, state: dict,
        present_npc_ids: list[str] | None = None,
        prev_narrative: str = "",
        action_text: str = "",
    ) -> tuple[list[dict], str]:
        """Stage 2 合并模式：scope=minor 时环境+角色合为一次调用。"""
        system = (
            "你是文字游戏的场景编导。根据剧情骨架，生成两部分内容：\n"
            "第一部分：环境和氛围描写（200字以内，覆盖2-3种感官）\n"
            "第二部分：角色对话和行为（300字以内，第二人称主角，第三人称NPC）\n\n"
            "用 --- 分隔两部分。用中文弯引号。不得捏造新角色。\n"
            "时代约束：科技、通讯工具、交通方式、日用物品必须与世界背景的时代吻合"
        )
        system += CACHE_SENTINEL

        player = state.get("player", {})
        loc_id = player.get("location", "")
        location_name = self._resolve_location_name(loc_id)
        loc_def = self._location_by_id.get(loc_id, {})
        loc_desc = loc_def.get("description", "")
        game_time = state.get("game_time", "")
        npc_states = state.get("npcs", {})

        npc_brief = ""
        npc_defs = {n["id"]: n for n in self.script.get("npcs", []) if "id" in n}
        for nid in (present_npc_ids or [])[:4]:
            ns = npc_states.get(nid, {})
            if isinstance(ns, dict):
                name = ns.get("name", nid)
                title = ns.get("title", "") or npc_defs.get(nid, {}).get("title", "")
                personality = ns.get("personality", "")
                room = ns.get("current_room", "")
                att = ns.get("attitude_toward_player", 50)
                tag_parts = []
                if title:
                    tag_parts.append(title)
                if personality:
                    tag_parts.append(f"性格={personality}")
                tag_parts.append(f"态度{att}")
                room_str = f" → {room}" if room else ""
                npc_brief += f"- {name}{room_str}（{'，'.join(tag_parts)}）\n"

        player_room = player.get("current_room", "")
        world_bg = self.script.get("world_background", "")

        # 上一轮氛围（连续性）
        scene_details = state.get("scene_details", {})
        prev_atmosphere = scene_details.get("atmosphere", "")

        # <scene> 区块
        scene_parts = [f"位置: {location_name}" + (f" - {loc_desc[:60]}" if loc_desc else "")]
        if player_room:
            scene_parts.append(f"当前房间: {player_room}")
        if world_bg:
            scene_parts.append(f"世界背景: {world_bg[:200]}")
        scene_parts.append(f"时间: {game_time or '未知'}")
        if prev_atmosphere:
            scene_parts.append(f"上一轮氛围: {prev_atmosphere}")

        # 组装 XML
        sections = [
            _xml("scene", "\n".join(scene_parts)),
            _xml("npcs_present", npc_brief.strip() or "无"),
            _xml("continuity", prev_narrative if prev_narrative else ""),
            _xml("player_action", action_text if action_text else ""),
            _xml("plot_decision", plot_decision),
        ]
        user_content = "\n\n".join(s for s in sections if s)

        messages = [{"role": "user", "content": user_content}]
        return messages, system
