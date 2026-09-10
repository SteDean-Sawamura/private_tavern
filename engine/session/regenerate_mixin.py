"""GameSession Mixin: 重生成与 Swipe 系统"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from engine.prompt_builder import PromptBuilder
from ai.base import strip_think_tags

if TYPE_CHECKING:
    pass  # 避免循环导入

# 玩家风格分析频率（每 N 回合）
PLAY_STYLE_INTERVAL = 5


class RegenerateMixin:
    """重生成（regenerate）、Swipe 切换、续写、历史摘要、风格分析相关方法"""

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
                    parsed["narrative"] = raw_narrative
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
                logging.getLogger(__name__).warning("Regenerate scene image failed: %s", e)

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
            logging.getLogger(__name__).error("attempt_deduction AI调用失败: %s", e)
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
