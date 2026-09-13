"""Game play API routes."""

import asyncio
import copy
import json
import logging
import os
import time
from collections import OrderedDict
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel
from typing import Optional

from config import DATA_DIR
from db.database import get_db
from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from engine.class_system import ClassRegistry

router = APIRouter()
logger = logging.getLogger("tavern.game")

# In-memory session store with LRU eviction (save_id -> GameSession)
_MAX_SESSIONS = 50
_sessions: OrderedDict[str, GameSession] = OrderedDict()
# Per-session locks to prevent concurrent modifications
_session_locks: dict[str, asyncio.Lock] = {}


def _get_session_lock(save_id: str) -> asyncio.Lock:
    """Get or create a per-session lock.

    P0-5: 使用 setdefault 保证原子性——避免两个并发请求各自创建不同的 Lock 实例
    导致互斥失效。eviction 不再删除 lock（见 _sessions_put）。"""
    return _session_locks.setdefault(save_id, asyncio.Lock())


def _sessions_put(save_id: str, session: GameSession):
    """Add or update a session, evicting oldest if over limit."""
    if save_id in _sessions:
        _sessions.move_to_end(save_id)
    _sessions[save_id] = session
    while len(_sessions) > _MAX_SESSIONS:
        evicted_id, evicted_session = _sessions.popitem(last=False)
        # P-5: 先尝试 drain 后台任务（限时），再 cancel 残余任务
        async def _drain_then_cancel(session):
            try:
                if session._background_tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*list(session._background_tasks), return_exceptions=True),
                        timeout=3.0,
                    )
            except asyncio.TimeoutError:
                for t in session._background_tasks:
                    t.cancel()
        if evicted_session._background_tasks:
            try:
                task = asyncio.get_running_loop().create_task(
                    _drain_then_cancel(evicted_session)
                )
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            except RuntimeError:
                pass
        # P0-5: 仅当锁未被持有时才移除；否则保留以避免后续请求拿到不同的 Lock 实例。
        existing_lock = _session_locks.get(evicted_id)
        if existing_lock is not None and not existing_lock.locked():
            _session_locks.pop(evicted_id, None)
        logger.info("会话淘汰 — save_id=%s (超出上限%d)", evicted_id, _MAX_SESSIONS)


async def _get_ai_provider_async():
    from api.config_routes import get_ai_provider_instance
    return await get_ai_provider_instance()


async def _get_stage_models_async():
    from api.config_routes import get_active_stage_models
    return await get_active_stage_models()


class NewGameRequest(BaseModel):
    script_id: str
    settings_overrides: Optional[dict] = None
    preset_id: Optional[str] = None
    custom_character: Optional[dict] = None
    opening_variant_id: Optional[str] = None
    authors_note: Optional[str] = None
    authors_note_position: Optional[str] = None
    authors_note_depth: Optional[int] = None
    npc_overrides: Optional[list[dict]] = None


class ActionRequest(BaseModel):
    type: str = "freeform"  # "choice" or "freeform"
    choice_id: Optional[str] = None
    text: Optional[str] = None


class AuthorsNoteRequest(BaseModel):
    note: str = ""
    position: str = "end"
    depth: int = 4


class NegativePromptRequest(BaseModel):
    text: str = ""


class LogitBiasRequest(BaseModel):
    entries: list[dict] = []  # [{"text": "暴力", "bias": -5}, ...]


class LorebookRecursionRequest(BaseModel):
    max_recursion: int = 4


class SwitchPersonaRequest(BaseModel):
    preset_id: str


class SwitchPovRequest(BaseModel):
    preset_id: str | None = None
    custom_character: dict | None = None


class NpcTalkRequest(BaseModel):
    message: str


class NpcDialogueRequest(BaseModel):
    topic: str = ""


@router.get("/classes/{system}")
async def list_classes(system: str):
    """List available classes for a game system (dnd5e or coc)."""
    if system not in ("dnd5e", "coc"):
        raise HTTPException(status_code=400, detail="System must be 'dnd5e' or 'coc'")
    registry = ClassRegistry(system)
    classes = registry.get_available_classes(system)
    return [{
        "id": c["id"],
        "name": c.get("name", c["id"]),
        "name_en": c.get("name_en", ""),
        "description": c.get("description", ""),
        "primary_attributes": c.get("primary_attributes", []),
        "hit_die": c.get("hit_die"),
        "narrative_tags": c.get("narrative_tags", []),
        "skill_proficiencies_choose": c.get("skill_proficiencies_choose"),
    } for c in classes]


@router.get("/classes/{system}/{class_id}")
async def get_class_detail(system: str, class_id: str):
    """Get detailed class information including skill tree."""
    if system not in ("dnd5e", "coc"):
        raise HTTPException(status_code=400, detail="System must be 'dnd5e' or 'coc'")
    registry = ClassRegistry(system)
    cls = registry.get_class(class_id)
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    return cls


@router.get("/skills/{system}")
async def list_skills(system: str):
    """List all skills for a game system."""
    if system not in ("dnd5e", "coc"):
        raise HTTPException(status_code=400, detail="System must be 'dnd5e' or 'coc'")
    registry = ClassRegistry(system)
    skills = registry.get_skills_for_system(system)
    return [{
        "id": s["id"],
        "name": s.get("name", s["id"]),
        "name_en": s.get("name_en", ""),
        "parent_attribute": s.get("parent_attribute", ""),
        "description": s.get("description", ""),
    } for s in skills]


@router.get("/opening-variants/{script_id}")
async def get_opening_variants(script_id: str):
    """List available opening variants for a script."""
    async with get_db() as db:
        cursor = await db.execute("SELECT content FROM scripts WHERE id = ?", (script_id,))
        row = await cursor.fetchone()
    if row:
        script = json.loads(row["content"])
    else:
        try:
            script = ScriptLoader.load(script_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Script not found")
    variants = script.get("opening_variants", [])
    return [{"id": v.get("id", ""), "label": v.get("label", v.get("id", "")), "preview": v.get("text", "")[:200]} for v in variants]


@router.post("/new")
async def new_game(req: NewGameRequest):
    """Start a new game from a script."""
    logger.info("新游戏请求 — 剧本=%s", req.script_id)
    # Try loading from DB first, then from file
    async with get_db() as db:
        cursor = await db.execute(
            "SELECT content FROM scripts WHERE id = ?", (req.script_id,)
        )
        row = await cursor.fetchone()

    if row:
        script = json.loads(row["content"])
    else:
        try:
            script = ScriptLoader.load(req.script_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Script not found")

    # Apply settings overrides
    if req.settings_overrides:
        script.setdefault("settings", {}).update(req.settings_overrides)

    # Apply character selection (preset or custom)
    if req.preset_id:
        presets = script.get("player_presets", [])
        preset = next((p for p in presets if p.get("id") == req.preset_id), None)
        if preset:
            pc = script.setdefault("player_character", {})
            pc["id"] = preset["id"]
            pc["name"] = preset.get("name", "")
            pc["bio"] = preset.get("bio", "")
            if preset.get("personality"):
                pc["personality"] = preset["personality"]
            if preset.get("portrait_desc"):
                pc["portrait_desc"] = preset["portrait_desc"]
            if preset.get("initial_location"):
                pc["initial_location"] = preset["initial_location"]
            if preset.get("long_term_goal"):
                pc["long_term_goal"] = preset["long_term_goal"]
            # Merge attribute overrides
            if preset.get("attributes"):
                base_attrs = pc.get("attributes", {})
                # Build reverse lookup: display_name → key for fuzzy matching
                _dn_to_key = {}
                for bk, bv in base_attrs.items():
                    if isinstance(bv, dict):
                        dn = bv.get("display_name") or bv.get("name", "")
                        if dn:
                            _dn_to_key[dn] = bk
                for k, v in preset["attributes"].items():
                    # Try direct key match first, then display_name reverse lookup
                    target_key = k if k in base_attrs else _dn_to_key.get(k, k)
                    if target_key in base_attrs:
                        if isinstance(base_attrs[target_key], dict):
                            base_attrs[target_key]["value"] = v if isinstance(v, (int, float)) else v.get("value", base_attrs[target_key].get("value", 50))
                        else:
                            base_attrs[target_key] = v
                    else:
                        base_attrs[target_key] = v
                pc["attributes"] = base_attrs
            # C4: pass preset known_npcs for social network filtering
            if preset.get("known_npcs"):
                script["_preset_known_npcs"] = preset["known_npcs"]
            # C5: pass preset persistent_state_overrides
            if preset.get("persistent_state_overrides"):
                script["_preset_ps_overrides"] = preset["persistent_state_overrides"]
            # C6: pass preset opening text override
            if preset.get("opening_text"):
                script["_preset_opening_text"] = preset["opening_text"]
            # C7: pass preset opening choices override
            if preset.get("opening_choices"):
                script["_preset_opening_choices"] = preset["opening_choices"]
            # Class system: pass class_id, level, skill_proficiencies to player_character
            if preset.get("class_id"):
                pc["class_id"] = preset["class_id"]
                pc["level"] = preset.get("level", 1)
                if preset.get("skill_proficiencies"):
                    pc["skill_proficiencies"] = preset["skill_proficiencies"]
            # C8: preset NPC overrides
            if preset.get("npc_overrides"):
                for override in preset["npc_overrides"]:
                    npc_id = override.get("id")
                    if not npc_id:
                        continue
                    for npc in script.get("npcs", []):
                        if npc["id"] == npc_id:
                            if override.get("name"):
                                npc["name"] = override["name"]
                            if override.get("personality"):
                                npc["personality"] = override["personality"]
                            if override.get("bio"):
                                npc["bio"] = override["bio"]
                            break
            logger.info("使用预设角色 — preset_id=%s, name=%s", req.preset_id, preset.get("name"))
    elif req.custom_character:
        pc = script.setdefault("player_character", {})
        if req.custom_character.get("name"):
            pc["name"] = req.custom_character["name"]
        if req.custom_character.get("bio"):
            pc["bio"] = req.custom_character["bio"]
        if req.custom_character.get("personality"):
            pc["personality"] = req.custom_character["personality"]
        if req.custom_character.get("portrait_desc"):
            pc["portrait_desc"] = req.custom_character["portrait_desc"]
        if req.custom_character.get("long_term_goal"):
            pc["long_term_goal"] = req.custom_character["long_term_goal"]
        if req.custom_character.get("initial_location"):
            pc["initial_location"] = req.custom_character["initial_location"]
        # Apply custom attribute values
        if req.custom_character.get("attributes"):
            base_attrs = pc.get("attributes", {})
            _dn_to_key = {}
            for bk, bv in base_attrs.items():
                if isinstance(bv, dict):
                    dn = bv.get("display_name") or bv.get("name", "")
                    if dn:
                        _dn_to_key[dn] = bk
            for k, v in req.custom_character["attributes"].items():
                target_key = k if k in base_attrs else _dn_to_key.get(k, k)
                if target_key in base_attrs and isinstance(base_attrs[target_key], dict):
                    base_attrs[target_key]["value"] = v
                elif target_key in base_attrs:
                    base_attrs[target_key] = v
            pc["attributes"] = base_attrs
        # Class system for custom character
        if req.custom_character.get("class_id"):
            pc["class_id"] = req.custom_character["class_id"]
            pc["level"] = req.custom_character.get("level", 1)
            if req.custom_character.get("skill_proficiencies"):
                pc["skill_proficiencies"] = req.custom_character["skill_proficiencies"]
        logger.info("自建角色 — name=%s", req.custom_character.get("name", "?"))

    # C9: request-level NPC overrides (player customization at game start)
    if req.npc_overrides:
        # Collect IDs to delete
        delete_ids = set()
        add_npcs = []
        for override in req.npc_overrides:
            npc_id = override.get("id")
            if not npc_id:
                continue
            if override.get("_delete"):
                delete_ids.add(npc_id)
                continue
            if override.get("_new"):
                add_npcs.append({
                    "id": npc_id,
                    "name": override.get("name", npc_id),
                    "personality": override.get("personality", ""),
                    "bio": override.get("bio", ""),
                    "role": override.get("role", ""),
                    "location": override.get("location", ""),
                })
                continue
            for npc in script.get("npcs", []):
                if npc["id"] == npc_id:
                    if override.get("name"):
                        npc["name"] = override["name"]
                    if override.get("personality"):
                        npc["personality"] = override["personality"]
                    if override.get("bio"):
                        npc["bio"] = override["bio"]
                    break
        if delete_ids:
            script["npcs"] = [n for n in script.get("npcs", []) if n.get("id") not in delete_ids]
        if add_npcs:
            script.setdefault("npcs", []).extend(add_npcs)

    # Validate
    errors = ScriptLoader.validate(script)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # Opening variant selection
    if req.opening_variant_id:
        script["_selected_opening_variant"] = req.opening_variant_id

    # Create session
    ai_provider = await _get_ai_provider_async()
    stage_models = await _get_stage_models_async()
    session = GameSession(script, ai_provider, stage_models=stage_models)
    session._last_activity_time = time.time()

    # Inject image provider if configured
    try:
        from api.config_routes import get_image_provider_instance
        session._image_provider = await get_image_provider_instance()
    except Exception:
        session._image_provider = None

    # Apply authors_note before initialization so opening generation respects it
    an_text = req.authors_note or script.get("settings", {}).get("authors_note", "")
    if an_text:
        session.set_authors_note(
            an_text,
            req.authors_note_position or script.get("settings", {}).get("authors_note_position", "end"),
            req.authors_note_depth or script.get("settings", {}).get("authors_note_depth", 4),
        )

    result = await session.initialize()
    # G1: 注入 skill_check_map 到初始 state
    skill_map = script.get("settings", {}).get("skill_check_map")
    if skill_map:
        result["state"]["_skill_check_map"] = skill_map
    logger.info("游戏初始化完成 — save_id=%s, 地点=%s",
                session.save_id, result["state"].get("player", {}).get("location", "?"))

    # Store session
    _sessions_put(session.save_id, session)

    # Save to DB
    async with get_db() as db:
        await db.execute(
            """INSERT INTO saves (id, script_id, name, active_node_id, total_nodes)
               VALUES (?, ?, ?, ?, ?)""",
            (
                session.save_id,
                req.script_id,
                script.get("script_name", req.script_id),
                result["node_id"],
                0,
            ),
        )
        await db.commit()
    scene_image_path = await _save_node(session, result)
    if scene_image_path:
        result["scene_image_path"] = scene_image_path
    result.pop("scene_image", None)

    return result


@router.post("/{save_id}/action")
async def game_action(save_id: str, req: ActionRequest):
    """Submit a player action."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        action = {
            "type": req.type,
            "choice_id": req.choice_id,
            "text": req.text or req.choice_id or "",
        }

        logger.info("玩家行动 [%s] save=%s — %s: %s",
                    session.turn_number + 1, save_id, req.type,
                    (req.text or req.choice_id or "")[:60])
        result = await session.process_action(action)
        logger.info("AI回复完成 — 选项数=%d, 状态变更=%d, 游戏时间=%s",
                    len(result.get("choices", [])),
                    len(result.get("state_changes", [])),
                    result.get("state", {}).get("game_time", "?"))

        # B6: _save_node 必须在锁内执行，否则可能与其它请求并发写 DB
        await _save_node(session, result)

    return result


@router.post("/{save_id}/action/stream")
async def game_action_stream(save_id: str, req: ActionRequest):
    """Submit a player action with streaming response."""
    lock = _get_session_lock(save_id)
    # Acquire lock before session lookup; held through the entire stream
    await lock.acquire()

    # P1-3: lock.acquire() 和 StreamingResponse 之间的异常会导致 lock 泄露。
    # 将整个 setup 阶段包在 try 中，异常时释放锁。
    try:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                lock.release()
                raise HTTPException(status_code=404, detail="Game session not found")

        action = {
            "type": req.type,
            "choice_id": req.choice_id,
            "text": req.text or req.choice_id or "",
        }

        logger.info("流式行动 [%s] save=%s — %s: %s",
                    session.turn_number + 1, save_id, req.type,
                    (req.text or req.choice_id or "")[:60])

        # Collect final result so we can save even if client disconnects
        final_result = None
        # Bug#3: 记录流开始前的 active_node_id，回滚时用它判断树是否被改动
        pre_stream_active = session.world_tree.active_node_id
        pre_stream_turn = session.turn_number
        pre_stream_state_snap = json.loads(json.dumps(session.current_state, ensure_ascii=False))
    except HTTPException:
        raise  # 404 already released above
    except Exception:
        lock.release()
        raise

    async def event_stream():
        nonlocal final_result
        async for chunk in session.process_action_stream(action):
            if chunk["type"] in ("text", "thinking", "narrative_revised"):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            elif chunk["type"] in ("tool_call", "agent_done", "aborted",
                                   "user_inject", "agent_max_rounds", "agent_plan"):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            elif chunk["type"] == "final":
                final_result = chunk
                logger.info("流式回复完成 — 选项数=%d, 状态变更=%d",
                            len(chunk.get("choices", [])),
                            len(chunk.get("state_changes", [])))
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

    async def saving_event_stream():
        try:
            async for data in event_stream():
                yield data
        finally:
            # Bug#3: 如果 final_result 为 None（流中断），完整回滚状态和世界树
            if final_result is not None:
                await _save_node(session, final_result)
            else:
                logger.warning("流式中断未获得最终结果 — save=%s, 回滚 turn=%d",
                               save_id, session.turn_number)
                # 1. 如果新节点已被加入，移除之
                cur_active = session.world_tree.active_node_id
                if cur_active and cur_active != pre_stream_active:
                    new_node = session.world_tree.get_node(cur_active)
                    if new_node and not new_node.get("children_ids"):
                        session.world_tree.remove_node(cur_active)
                        session.world_tree.active_node_id = pre_stream_active
                        session.world_tree._branch_dirty = True
                # 2. 恢复 state 和 turn_number 到流前快照
                session.current_state = pre_stream_state_snap
                session.turn_number = pre_stream_turn
            lock.release()

    return StreamingResponse(saving_event_stream(), media_type="text/event-stream")


class InjectMessageRequest(BaseModel):
    message: str


@router.post("/{save_id}/abort")
async def abort_generation(save_id: str):
    """Signal the running agentic loop to stop after the current LLM call."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    session._abort_flag = True
    return {"ok": True}


@router.post("/{save_id}/inject")
async def inject_message(save_id: str, req: InjectMessageRequest):
    """Inject a user message into the running agentic loop between rounds."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    session._inject_queue.append(req.message)
    return {"ok": True}


@router.get("/{save_id}/settlement-messages")
async def get_settlement_messages(save_id: str):
    """D3: Get pending settlement messages from background agent."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    messages = getattr(session, '_settlement_messages', [])
    session._settlement_messages = []  # drain
    return {"messages": messages}


@router.get("/{save_id}/state")
async def get_state(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        state = session.current_state
        # G1: 注入 skill_check_map 供前端使用（构造新 dict 避免污染 session state）
        skill_map = session.script.get("settings", {}).get("skill_check_map")
        if skill_map:
            state = {**state, "_skill_check_map": skill_map}
        # Inject authors_note config for frontend restoration
        if session.authors_note:
            state = {**state} if state is session.current_state else state
            state["_authors_note"] = session.authors_note
            state["_authors_note_position"] = session.authors_note_position
            state["_authors_note_depth"] = session.authors_note_depth
        if session.negative_prompt:
            state = {**state} if state is session.current_state else state
            state["_negative_prompt"] = session.negative_prompt
        if session.logit_bias:
            state = {**state} if state is session.current_state else state
            state["_logit_bias"] = session.logit_bias
        # Expose lorebook max_recursion for frontend control
        _lr = session.script.get("settings", {}).get("lorebook_max_recursion")
        if _lr:
            state = {**state} if state is session.current_state else state
            state["_lorebook_max_recursion"] = _lr
        # Expose available POV targets for switching UI
        presets = session.script.get("player_presets", [])
        current_pc_id = state.get("player", {}).get("id", "player")
        # Fallback: match current PC by name if id is generic "player"
        current_pc_name = state.get("player", {}).get("name", "")
        existing_npcs = set(state.get("npcs", {}).keys())
        shelved = state.get("shelved_pc_data", {})
        available_povs = []
        for p in presets:
            pid = p["id"]
            if pid == current_pc_id or (current_pc_id == "player" and p.get("name") == current_pc_name):
                continue
            if pid in shelved:
                available_povs.append({"id": pid, "name": p.get("name", pid), "status": "shelved"})
            elif pid not in existing_npcs:
                available_povs.append({"id": pid, "name": p.get("name", pid), "status": "available"})
        # Also list shelved PCs not in presets (custom characters previously played)
        for sid, sdata in shelved.items():
            if sid == current_pc_id:
                continue
            if not any(pv["id"] == sid for pv in available_povs):
                available_povs.append({"id": sid, "name": sdata.get("player", {}).get("name", sid), "status": "shelved"})
        # Always expose POV switch if script has presets (custom char creation always available)
        if presets or available_povs:
            state = {**state} if state is session.current_state else state
            state["_available_povs"] = available_povs
            state["_pov_switches_remaining"] = 5 - len(state.get("pov_history", []))
            # Expose known organizations and titles for combo fields
            orgs_set = set()
            titles_set = set()
            for npc in session.script.get("npcs", []):
                if npc.get("title"):
                    titles_set.add(npc["title"])
                for org in npc.get("organizations", []):
                    oid = org.get("org_id", "") if isinstance(org, dict) else org
                    if oid:
                        orgs_set.add(oid)
            # Also get display names for orgs from faction_reputation or display_names
            dn = state.get("display_names", {})
            org_options = [{"id": o, "name": dn.get(o, o)} for o in sorted(orgs_set)]
            state["_pov_orgs"] = org_options
            state["_pov_titles"] = sorted(titles_set)
        return state


@router.get("/{save_id}/tree")
async def get_tree(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.world_tree.get_tree_structure()


@router.get("/{save_id}/tree/{node_id}")
async def get_tree_node(save_id: str, node_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        node = session.world_tree.get_node(node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")
        return node


@router.post("/{save_id}/branch/{node_id}")
async def branch_to(save_id: str, node_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        result = await session.branch_to_node(node_id)
        if not result:
            raise HTTPException(status_code=404, detail="Node not found")
        # Bug#2: 同步 DB active_node_id，否则会话被 LRU 淘汰后再恢复会回到旧分支
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET active_node_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (node_id, save_id),
            )
            await db.commit()
        logger.info("切换分支 — save=%s, node=%s, turn=%d",
                    save_id, node_id, session.turn_number)
        return result


@router.post("/{save_id}/toggle-dice")
async def toggle_dice(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        current = session.current_state.get("dice_check_enabled", True)
        session.current_state["dice_check_enabled"] = not current
        return {"dice_check_enabled": not current}


@router.get("/{save_id}/history")
async def get_history(save_id: str, last: int = 10, before_turn: int = 0):
    """Return recent history. If before_turn > 0, return `last` entries before that turn."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        branch = session.world_tree.get_active_branch()
        total = len(branch)
        if before_turn > 0:
            branch = [n for n in branch if n.get("turn_number", 0) < before_turn]
            branch = branch[-last:] if len(branch) > last else branch
        else:
            branch = branch[-last:] if len(branch) > last else branch
        entries = [
            {
                "id": n["id"],
                "turn": n["turn_number"],
                "time": n["game_time"],
                "action": n.get("player_action"),
                "narrative": n.get("ai_response", ""),
                "thinking": n.get("thinking", ""),
                "choices": n.get("choices_presented", []),
                "dice_rolls": n.get("dice_rolls", []),
                "state_changes": n.get("state_changes", []),
                "triggered_events": n.get("triggered_events", []),
                "check_result": n.get("check_result"),
                "triggered_consequences": n.get("triggered_consequences"),
                "achieved_milestones": n.get("achieved_milestones"),
                "scene_image_path": n.get("scene_image_path"),
            }
            for n in branch
        ]
        # Include swipe info for the active node
        active_node = session.world_tree.get_node(session.world_tree.active_node_id)
        swipe_index = 0
        total_swipes = 1
        if active_node:
            swipes = active_node.get("swipes", [])
            if swipes:
                swipe_index = active_node.get("active_swipe_index", 0)
                total_swipes = len(swipes)
        return {"entries": entries, "total": total, "swipe_index": swipe_index, "total_swipes": total_swipes}


@router.get("/{save_id}/adventure-log")
async def get_adventure_log(save_id: str):
    """Return the adventure log from the current game state."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.current_state.get("adventure_log", [])


@router.get("/{save_id}/summary")
async def get_summary(save_id: str):
    """Return the full history summary from the current game state."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return {
            "summary": session.current_state.get("history_summary", ""),
            "turn": session.turn_number,
        }


@router.post("/{save_id}/summary/freeze")
async def toggle_summary_freeze(save_id: str, req: dict = {}):
    """Toggle the summary freeze state."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        frozen = req.get("frozen", not session.current_state.get("summary_frozen", False))
        session.current_state["summary_frozen"] = bool(frozen)
        return {"frozen": session.current_state["summary_frozen"]}


@router.post("/{save_id}/use_item")
async def use_item(save_id: str, req: dict):
    """Use an item from inventory with deterministic effects."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        item_name = req.get("item_name", "")
        if not item_name:
            raise HTTPException(status_code=400, detail="item_name required")
        result = session.use_item(item_name)
        if result.get("has_effect") is False:
            return result
        if result.get("state"):
            await _save_node(session, result)
        return result


@router.post("/{save_id}/interact")
async def interact_object(save_id: str, req: dict):
    """Interact with a location interactable deterministically."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        iid = req.get("interactable_id", "")
        if not iid:
            raise HTTPException(status_code=400, detail="interactable_id required")
        result = session.interact_with_object(iid)
        if result.get("has_rules") is False:
            return result
        if result.get("state"):
            await _save_node(session, result)
        return result


@router.post("/{save_id}/talk/{npc_id}")
async def talk_to_npc(save_id: str, npc_id: str, req: NpcTalkRequest):
    """Have a dedicated conversation with an NPC."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        logger.info("NPC对话 save=%s npc=%s — %s", save_id, npc_id, req.message[:60])
        result = await session.talk_to_npc(npc_id, req.message)
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        logger.info("NPC回复完成 — npc=%s, 状态变更=%d",
                    npc_id, len(result.get("state_changes", [])))

        # 持久化：NPC对话现在创建独立节点，用 _save_node 保存
        node_id = result.get("node_id")
        if node_id:
            await _save_node(session, {
                "node_id": node_id,
                "narrative": result.get("response", ""),
                "choices": [],
                "dice_rolls": [],
                "state_changes": result.get("state_changes", []),
                "triggered_events": result.get("triggered_events", []),
                "state": session.current_state,
            })

        return result


@router.post("/{save_id}/talk/{npc_id}/stream")
async def talk_to_npc_stream(save_id: str, npc_id: str, req: NpcTalkRequest):
    """SSE streaming version of NPC talk."""
    lock = _get_session_lock(save_id)
    await lock.acquire()

    try:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                lock.release()
                raise HTTPException(status_code=404, detail="Game session not found")
    except HTTPException:
        raise
    except Exception:
        lock.release()
        raise

    final_result = None
    # 流前快照 — 流中断时回滚内存状态，避免与数据库不一致
    pre_stream_state_snap = copy.deepcopy(session.current_state)
    pre_stream_active = session.world_tree.active_node_id

    async def event_stream():
        nonlocal final_result
        async for chunk in session.talk_to_npc_stream(npc_id, req.message):
            if chunk["type"] in ("text", "thinking", "narrative_revised"):
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            elif chunk["type"] == "final":
                final_result = chunk
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

    async def saving_event_stream():
        try:
            async for data in event_stream():
                yield data
        finally:
            if final_result and not final_result.get("error"):
                # NPC对话现在创建独立节点
                node_id = final_result.get("node_id")
                if node_id:
                    try:
                        await _save_node(session, {
                            "node_id": node_id,
                            "narrative": final_result.get("response", ""),
                            "choices": [],
                            "dice_rolls": [],
                            "state_changes": final_result.get("state_changes", []),
                            "triggered_events": final_result.get("triggered_events", []),
                            "state": session.current_state,
                        })
                    except Exception as e:
                        logger.warning("NPC流式对话节点持久化失败 — %s", e)
            else:
                # 流中断或出错 — 回滚内存状态到流前快照
                if final_result is None:
                    logger.warning("NPC流式对话中断 — 回滚状态 save=%s npc=%s", save_id, npc_id)
                # 回滚世界树节点
                cur_active = session.world_tree.active_node_id
                if cur_active and cur_active != pre_stream_active:
                    new_node = session.world_tree.get_node(cur_active)
                    if new_node and not new_node.get("children_ids"):
                        session.world_tree.remove_node(cur_active)
                        session.world_tree.active_node_id = pre_stream_active
                        session.world_tree._branch_dirty = True
                session.current_state = pre_stream_state_snap
            lock.release()

    return StreamingResponse(saving_event_stream(), media_type="text/event-stream")


@router.get("/{save_id}/npc-schedules")
async def get_npc_schedules(save_id: str):
    """Get current schedule info for all NPCs."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        game_time = session.current_state.get("game_time", "")
        result = {}
        for npc in session.script.get("npcs", []):
            npc_id = npc["id"]
            info = session.prompt_builder.get_npc_current_info(npc_id, game_time)
            if info:
                result[npc_id] = info
            else:
                # No schedule — use default location from state or script
                npc_state = session.current_state.get("npcs", {}).get(npc_id, {})
                default_loc = ""
                if isinstance(npc_state, dict):
                    default_loc = npc_state.get("default_location", "")
                if not default_loc:
                    default_loc = npc.get("default_location", npc.get("initial_location", ""))
                if default_loc:
                    result[npc_id] = {"location": default_loc, "activity": ""}
        return result


# ================================================
#  Swipe / Regenerate / Author's Note
# ================================================

@router.post("/{save_id}/regenerate")
async def regenerate(save_id: str, stage: str = "all", hint: str = ""):
    """Regenerate the AI response for the current node (Swipe system).

    stage: "all" = full regenerate, "choices" = choices only, "state" = state only.
    hint: optional narrative direction hint.
    """
    if stage not in ("all", "choices", "state"):
        raise HTTPException(status_code=400, detail="stage must be 'all', 'choices', or 'state'")
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        try:
            result = await session.regenerate(stage=stage, hint=hint)
            if result.get("error"):
                raise HTTPException(status_code=400, detail=result["error"])
            # Save regenerated scene image to disk
            scene_img = result.get("scene_image")
            scene_image_path = None
            if scene_img and scene_img.get("base64"):
                try:
                    import base64 as b64mod
                    scenes_dir = os.path.join(str(DATA_DIR), "scenes", save_id)
                    os.makedirs(scenes_dir, exist_ok=True)
                    node_id = result.get("node_id", "unknown")
                    filename = f"{node_id}.png"
                    filepath = os.path.join(scenes_dir, filename)
                    with open(filepath, "wb") as imgf:
                        imgf.write(b64mod.b64decode(scene_img["base64"]))
                    scene_image_path = f"/api/game/scenes/{save_id}/{filename}"
                    node = session.world_tree.get_node(node_id)
                    if node:
                        node["scene_image_path"] = scene_image_path
                except Exception as e:
                    logger.warning("Failed to save regen scene image: %s", e)
            result["scene_image_path"] = scene_image_path
            result.pop("scene_image", None)
            # Update node in DB
            await _update_node_in_db(session, result)
            logger.info("重新生成完成 — stage=%s, swipe=%d/%d",
                        stage, result.get("swipe_index", 0) + 1, result.get("total_swipes", 1))
            return result
        except Exception as e:
            logger.error("重新生成失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/{save_id}/feedback-regen")
async def feedback_regenerate(save_id: str, req: dict):
    """用户对当前叙事给出反馈，触发重新生成。
    Body: {"feedback": "让气氛更紧张一些"}
    """
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        feedback = (req.get("feedback") or "").strip()
        if not feedback:
            raise HTTPException(status_code=400, detail="请提供反馈内容")
        result = await session.regenerate_with_feedback(feedback)
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        # 同步更新数据库中的叙事文本
        await _update_node_in_db(session, {
            "node_id": session.world_tree.active_node_id,
            "narrative": result["narrative"],
            "choices": session.world_tree.get_node(session.world_tree.active_node_id).get("choices_presented", []),
            "state_changes": [],
        })
        return result


@router.post("/{save_id}/swipe/{direction}")
async def swipe_direction(save_id: str, direction: str):
    """Switch to a different swipe (left/right)."""
    if direction not in ("left", "right"):
        raise HTTPException(status_code=400, detail="Direction must be 'left' or 'right'")
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        await session._drain_background_tasks()
        result = session.swipe_to(direction)
        if not result:
            raise HTTPException(status_code=400, detail="No swipes available")
        await _update_node_in_db(session, result)
        logger.info("Swipe %s — save=%s, swipe=%d/%d",
                    direction, save_id, result.get("swipe_index", 0) + 1, result.get("total_swipes", 1))
        return result


@router.get("/{save_id}/swipes")
async def get_swipes(save_id: str):
    """Get all swipe variants for the active node."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.get_all_swipes()


@router.post("/{save_id}/swipe/jump")
async def swipe_jump(save_id: str, index: int = 0):
    """Jump to a specific swipe by index."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        await session._drain_background_tasks()
        result = session.swipe_to_index(index)
        if not result:
            raise HTTPException(status_code=400, detail="Invalid swipe index")
        await _update_node_in_db(session, result)
        return result


@router.post("/{save_id}/continue")
async def continue_narrative(save_id: str):
    """Extend the current narrative without starting a new turn."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        try:
            result = await session.continue_narrative()
            if result.get("error"):
                raise HTTPException(status_code=400, detail=result["error"])
            await _update_node_in_db(session, result)
            return result
        except Exception as e:
            logger.error("续写失败: %s", e)
            raise HTTPException(status_code=500, detail=str(e))


@router.post("/{save_id}/authors-note")
async def set_authors_note(save_id: str, req: AuthorsNoteRequest):
    """Set the player's behind-the-scenes directive."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        session.set_authors_note(req.note, req.position, req.depth)
        # Architecture#2: 持久化 authors_note 到数据库
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET authors_note = ?, authors_note_position = ?, authors_note_depth = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (req.note, req.position, req.depth, save_id),
            )
            await db.commit()
        logger.info("设置作者笔记 — save=%s, note=%s, pos=%s", save_id, (req.note or "")[:40], req.position)
        return {"status": "ok", "note": req.note, "position": req.position, "depth": req.depth}


@router.post("/{save_id}/negative-prompt")
async def set_negative_prompt(save_id: str, req: NegativePromptRequest):
    """Set the CFG-style negative prompt (prohibition rules injected into system prompt)."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        session.negative_prompt = req.text
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET negative_prompt = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (req.text, save_id),
            )
            await db.commit()
        return {"status": "ok", "text": req.text}


@router.post("/{save_id}/logit-bias")
async def set_logit_bias(save_id: str, req: LogitBiasRequest):
    """Set logit bias entries (word-level encourage/suppress hints)."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        session.logit_bias = req.entries
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET logit_bias = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(req.entries, ensure_ascii=False), save_id),
            )
            await db.commit()
        return {"status": "ok", "entries": req.entries}


@router.post("/{save_id}/lorebook-recursion")
async def set_lorebook_recursion(save_id: str, req: LorebookRecursionRequest):
    """Adjust lorebook max recursion depth at runtime."""
    val = max(1, min(8, req.max_recursion))
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        if session.prompt_builder and session.prompt_builder.lorebook:
            session.prompt_builder.lorebook.max_recursion = val
        session.script.setdefault("settings", {})["lorebook_max_recursion"] = val
    return {"status": "ok", "max_recursion": val}


@router.post("/{save_id}/switch-persona")
async def switch_persona(save_id: str, req: SwitchPersonaRequest):
    """Legacy endpoint — redirects to switch-pov."""
    pov_req = SwitchPovRequest(preset_id=req.preset_id)
    return await switch_pov(save_id, pov_req)


@router.post("/{save_id}/switch-pov")
async def switch_pov(save_id: str, req: SwitchPovRequest):
    """Switch player POV: shelve current PC, init new PC in same world timeline."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        result = await session.switch_pov(req.preset_id, req.custom_character)
        if not result:
            raise HTTPException(status_code=400, detail="POV switch failed (limit reached or invalid target)")
        # Persist state
        async with get_db() as db:
            await db.execute(
                "UPDATE saves SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(session.current_state, ensure_ascii=False, default=str), save_id),
            )
            await db.commit()
        enriched = _enrich_state(session, result["state"])
        return {
            "status": "ok",
            "narrative": result["narrative"],
            "player": enriched.get("player", result["player"]),
            "choices": result["choices"],
            "state": enriched,
        }


@router.post("/{save_id}/npc-dialogue")
async def npc_dialogue(save_id: str, req: NpcDialogueRequest):
    """Trigger independent NPC dialogue round (group chat style)."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        speeches = await session.npc_dialogue_round(req.topic)
        return {"speeches": speeches}


@router.post("/{save_id}/undo")
async def undo_last_turn(save_id: str):
    """Flow#7: 撤销最近一次行动，回到上一步。"""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        result = await session.undo_last_turn()
        if not result:
            raise HTTPException(status_code=400, detail="无法撤销")
        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])
        # 更新数据库的活动节点，并删除被撤销的节点
        async with get_db() as db:
            removed_node_id = result.get("removed_node_id")
            if removed_node_id:
                await db.execute("DELETE FROM tree_nodes WHERE id = ?", (removed_node_id,))
            await db.execute(
                """UPDATE saves SET active_node_id = ?,
                   total_nodes = MAX(total_nodes - 1, 0),
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                (result["node_id"], save_id),
            )
            await db.commit()
        logger.info("撤销行动 — save=%s, 回退到node=%s, turn=%d",
                     save_id, result["node_id"], session.turn_number)
        return result


# ===== Story Tree endpoints =====

class StoryTreeChoiceRequest(BaseModel):
    node_id: str
    choice_id: str


@router.get("/{save_id}/story-tree")
async def get_story_tree(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        trees = []
        if session.event_engine:
            trees = session.event_engine.get_visible_trees(session.current_state, session.turn_number)
        elif session.story_tree_engine:
            trees = session.story_tree_engine.get_visible_trees(session.current_state, session.turn_number)

        ot_view = []
        fired_set = set(session.current_state.get("fired_one_time_events", []))
        for ev in session.script.get("one_time_events", []):
            if not isinstance(ev, dict) or not ev.get("id"):
                continue
            ot_view.append({
                "id": ev["id"],
                "name": ev.get("name", ev.get("description", ev["id"])[:20]),
                "description": ev.get("description", ""),
                "trigger_time": ev.get("trigger_time", ""),
                "condition": ev.get("condition", ""),
                "fire_events": ev.get("fire_events", []),
                "activate_events": ev.get("activate_events", []),
                "status": "fired" if ev["id"] in fired_set else "pending",
            })

        ce_view = []
        trackers = session.current_state.get("cyclic_event_trackers", {})
        game_time = session.current_state.get("game_time", "")
        for ev in session.script.get("cyclic_events", []):
            if not isinstance(ev, dict) or not ev.get("id"):
                continue
            eid = ev["id"]
            tracker = trackers.get(eid, {})
            from engine.event_scheduler import parse_time
            expires = parse_time(ev.get("expires_at"))
            gt = parse_time(game_time)
            expired = bool(expires and gt and expires < gt)
            ce_view.append({
                "id": eid,
                "name": ev.get("name", ev.get("description", eid)[:20]),
                "description": ev.get("description", ""),
                "frequency_value": ev.get("frequency_value", 1),
                "frequency_unit": ev.get("frequency_unit", "day"),
                "condition": ev.get("condition", ""),
                "fire_events": ev.get("fire_events", []),
                "activate_events": ev.get("activate_events", []),
                "status": "expired" if expired else "active",
                "next_fire": tracker.get("next_fire", ""),
                "last_fired": tracker.get("last_fired", ""),
            })

        threads = session.event_engine.get_events_by_category(session.current_state, "thread") if session.event_engine else []
        clues = session.event_engine.get_events_by_category(session.current_state, "clue") if session.event_engine else []
        return {
            "trees": trees,
            "one_time_events": ot_view,
            "cyclic_events": ce_view,
            "narrative_threads": threads,
            "clue_board": clues,
        }


@router.post("/{save_id}/story-tree/choice")
async def story_tree_choice(save_id: str, req: StoryTreeChoiceRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        if not session.story_tree_engine and not session.event_engine:
            raise HTTPException(status_code=400, detail="此剧本没有剧情树")
        if session.event_engine:
            result = session.event_engine.make_choice(
                session.current_state, req.node_id, req.choice_id,
                condition_eval=session._evaluate_condition,
            )
            session._apply_event_result(result)
            trees = session.event_engine.get_visible_trees(session.current_state, session.turn_number)
        else:
            result = session.story_tree_engine.make_choice(
                session.current_state, req.node_id, req.choice_id,
                condition_eval=session._evaluate_condition,
            )
            session._apply_story_tree_result(result)
            trees = session.story_tree_engine.get_visible_trees(session.current_state, session.turn_number)
        return {
            "success": len(result.newly_completed) > 0,
            "completed": [{"id": n["id"], "name": n.get("name", n["id"])} for n in result.newly_completed],
            "notifications": result.notifications,
            "trees": trees,
        }


class DeductionRequest(BaseModel):
    clue_ids: list[str]

class ShopBuyRequest(BaseModel):
    shop_id: str
    item_id: str

class ShopSellRequest(BaseModel):
    shop_id: str
    item_name: str


@router.post("/{save_id}/deduce")
async def attempt_deduction(save_id: str, req: DeductionRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        result = await session.attempt_deduction(req.clue_ids)
        return result


@router.get("/{save_id}/shop/{location_id}")
async def get_shop(save_id: str, location_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.get_location_shops(location_id)


@router.post("/{save_id}/shop/buy")
async def shop_buy(save_id: str, req: ShopBuyRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.buy_item(req.shop_id, req.item_id)


@router.post("/{save_id}/shop/sell")
async def shop_sell(save_id: str, req: ShopSellRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.sell_item(req.shop_id, req.item_name)


class UnlockSkillRequest(BaseModel):
    skill_id: str

@router.post("/{save_id}/unlock-skill")
async def unlock_skill(save_id: str, req: UnlockSkillRequest):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return session.unlock_skill(req.skill_id)


@router.post("/{save_id}/newspaper")
async def generate_newspaper(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return await session.generate_newspaper()


@router.post("/{save_id}/newspaper/regenerate")
async def regenerate_newspaper(save_id: str):
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        return await session.generate_newspaper(force=True)


@router.get("/scenes/{save_id}/{filename}")
async def get_scene_image(save_id: str, filename: str):
    """Serve a persisted scene image file."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = os.path.join(str(DATA_DIR), "scenes", save_id, filename)
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Scene image not found")
    return FileResponse(filepath, media_type="image/png")


@router.post("/{save_id}/generate-scene-image/{node_id}")
async def generate_scene_image_for_node(save_id: str, node_id: str):
    """Generate a scene image for an existing node that doesn't have one."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")

        node = session.world_tree.get_node(node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")

        if node.get("scene_image_path"):
            return {"scene_image_path": node["scene_image_path"]}

        # Get image provider (always fetch latest config)
        from api.config_routes import get_image_provider_instance
        image_provider = await get_image_provider_instance()
        if not image_provider:
            raise HTTPException(status_code=400, detail="No image provider configured")
        session._image_provider = image_provider

        narrative = node.get("ai_response", "")
        if not narrative:
            raise HTTPException(status_code=400, detail="Node has no narrative text")

        # Build image prompt and generate
        from ai.image_prompt_builder import build_image_prompt
        from api.config_routes import get_image_style
        snapshot = node.get("state_snapshot", {})
        loc = snapshot.get("player", {}).get("location", "")
        loc_name = loc
        loc_desc = ""
        loc_data = session._location_by_id.get(loc)
        if loc_data:
            loc_name = loc_data.get("name", loc)
            loc_desc = loc_data.get("description", "")
        else:
            for l in session.script.get("locations", []):
                if l.get("id") == loc:
                    loc_name = l.get("name", loc)
                    loc_desc = l.get("description", "")
                    break
        tod = snapshot.get("time_of_day", "day")
        weather = snapshot.get("current_weather", "")

        # Gather character descriptions
        characters = []
        pc = session.script.get("player_character", {})
        pc_appearance = pc.get("appearance") or pc.get("bio") or ""
        if pc_appearance:
            characters.append(f"Player: {pc_appearance[:200]}")
        npc_states = snapshot.get("npcs", {})
        for nid, ns in list(npc_states.items())[:5]:
            if isinstance(ns, dict) and ns.get("current_location") == loc:
                npc_def = session._npc_by_id.get(nid, {})
                desc = npc_def.get("appearance") or npc_def.get("bio") or ""
                name = ns.get("name") or npc_def.get("name", nid)
                if desc:
                    characters.append(f"{name}: {desc[:150]}")

        # Load image style preference
        style_data = await get_image_style()
        image_style = style_data.get("custom") or style_data.get("preset") or ""

        img_prompt = await build_image_prompt(
            narrative, loc_name, "atmospheric", tod, session.ai_provider,
            weather=weather, characters=characters,
            location_desc=loc_desc, image_style=image_style,
        )
        scene_img = await image_provider.generate_image(img_prompt)

        # Save to disk
        import base64 as b64mod
        scenes_dir = os.path.join(str(DATA_DIR), "scenes", save_id)
        os.makedirs(scenes_dir, exist_ok=True)
        filename = f"{node_id}.png"
        filepath = os.path.join(scenes_dir, filename)
        with open(filepath, "wb") as imgf:
            imgf.write(b64mod.b64decode(scene_img["base64"]))
        scene_image_path = f"/api/game/scenes/{save_id}/{filename}"

        # Update node and DB
        node["scene_image_path"] = scene_image_path
        async with get_db() as db:
            cursor = await db.execute(
                "SELECT state_snapshot FROM tree_nodes WHERE id = ?", (node_id,)
            )
            row = await cursor.fetchone()
            if row:
                snap = json.loads(row["state_snapshot"]) if row["state_snapshot"] else {}
                nc = snap.get("_node_context", {})
                nc["scene_image_path"] = scene_image_path
                snap["_node_context"] = nc
                await db.execute(
                    "UPDATE tree_nodes SET state_snapshot = ? WHERE id = ?",
                    (json.dumps(snap, ensure_ascii=False), node_id)
                )
                await db.commit()

        return {"scene_image_path": scene_image_path, "prompt": img_prompt}


# ================================================
#  Observability endpoints (E1/E2/E3)
# ================================================

@router.get("/{save_id}/agent-traces")
async def get_agent_traces(save_id: str):
    """E1: Return recent agent traces for visualization."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    traces = getattr(session, '_agent_traces', [])
    return {"traces": traces}


@router.get("/{save_id}/performance")
async def get_performance(save_id: str):
    """E3: Return session-level performance statistics."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return getattr(session, '_performance_stats', {})


@router.get("/{save_id}/usage-stats")
async def get_usage_stats(save_id: str):
    """#7: Return fine-grained token/cost usage statistics."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return session.usage_tracker.get_stats() if hasattr(session, 'usage_tracker') else {}


@router.post("/{save_id}/ab-test")
async def run_ab_test(save_id: str, req: dict):
    """E2: Run A/B test comparing two prompt variants."""
    lock = _get_session_lock(save_id)
    async with lock:
        session = _sessions.get(save_id)
        if not session:
            session = await _restore_session(save_id)
            if not session:
                raise HTTPException(status_code=404, detail="Game session not found")
        result = await session.ab_tester.run_comparison(
            session, req.get("action", {}),
            req.get("variant_a", {}), req.get("variant_b", {}),
        )
        return result


@router.get("/{save_id}/ab-results")
async def get_ab_results(save_id: str):
    """E2: Return recent A/B test experiments."""
    session = _sessions.get(save_id)
    if not session:
        raise HTTPException(status_code=404, detail="Game session not found")
    return {"experiments": session.ab_tester.get_experiments()}


async def _save_node(session: GameSession, result: dict) -> str | None:
    """Save a game turn node to the database. Returns scene_image_path if generated."""
    # Persist scene image to disk if present
    scene_image_path = None
    scene_img = result.get("scene_image")
    if scene_img and scene_img.get("base64"):
        try:
            import base64 as b64mod
            scenes_dir = os.path.join(str(DATA_DIR), "scenes", session.save_id)
            os.makedirs(scenes_dir, exist_ok=True)
            node_id = result.get("node_id", "unknown")
            filename = f"{node_id}.png"
            filepath = os.path.join(scenes_dir, filename)
            with open(filepath, "wb") as imgf:
                imgf.write(b64mod.b64decode(scene_img["base64"]))
            scene_image_path = f"/api/game/scenes/{session.save_id}/{filename}"
        except Exception as e:
            logger.warning("Failed to save scene image: %s", e)

    async with get_db() as db:
        node_id = result.get("node_id", "")
        node = session.world_tree.get_node(node_id)
        if node:
            # Check if node already exists (to avoid double-counting)
            cursor = await db.execute(
                "SELECT 1 FROM tree_nodes WHERE id = ?", (node_id,)
            )
            is_new = await cursor.fetchone() is None

            # 将 regenerate 需要的上下文嵌入 snapshot 序列化数据中（不改 DB schema）
            snapshot_to_save = result.get("state", {})
            node_context = {}
            if node.get("check_result"):
                node_context["check_result"] = node["check_result"]
            if node.get("triggered_consequences"):
                node_context["triggered_consequences"] = node["triggered_consequences"]
            if node.get("achieved_milestones"):
                node_context["achieved_milestones"] = node["achieved_milestones"]
            if node.get("thinking"):
                node_context["thinking"] = node["thinking"]
            if scene_image_path:
                node_context["scene_image_path"] = scene_image_path
            if node_context:
                snapshot_to_save = {**snapshot_to_save, "_node_context": node_context}

            await db.execute(
                """INSERT OR REPLACE INTO tree_nodes
                   (id, save_id, parent_id, game_time, turn_number,
                    player_action, ai_response, choices_presented, dice_rolls,
                    state_changes, triggered_events, state_snapshot)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    node_id,
                    session.save_id,
                    node.get("parent_id"),
                    node.get("game_time", ""),
                    node.get("turn_number", 0),
                    json.dumps(node.get("player_action"), ensure_ascii=False)
                    if node.get("player_action")
                    else None,
                    result.get("narrative", ""),
                    json.dumps(result.get("choices", []), ensure_ascii=False),
                    json.dumps(result.get("dice_rolls", []), ensure_ascii=False),
                    json.dumps(result.get("state_changes", []), ensure_ascii=False),
                    json.dumps(
                        [e.get("event_id", e) if isinstance(e, dict) else e
                         for e in result.get("triggered_events", [])],
                        ensure_ascii=False
                    ),
                    json.dumps(snapshot_to_save, ensure_ascii=False),
                ),
            )

            # Update save metadata — only increment total_nodes for new nodes
            play_delta = session._mark_activity()
            if is_new:
                await db.execute(
                    """UPDATE saves SET active_node_id = ?, total_nodes = total_nodes + 1,
                       play_time_seconds = play_time_seconds + ?,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (node_id, play_delta, session.save_id),
                )
            else:
                await db.execute(
                    """UPDATE saves SET active_node_id = ?,
                       play_time_seconds = play_time_seconds + ?,
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (node_id, play_delta, session.save_id),
                )
            await db.commit()

        # Bug#1: 快照已持久化到 DB，现在提交内存驱逐
        parent_id = node.get("parent_id") if node else None
        if parent_id:
            session.world_tree.commit_pending_eviction(parent_id)

    return scene_image_path


async def _restore_session(save_id: str) -> GameSession | None:
    """Restore a game session from the database."""
    logger.info("恢复会话 — save_id=%s", save_id)
    async with get_db() as db:
        # Get save info
        cursor = await db.execute("SELECT * FROM saves WHERE id = ?", (save_id,))
        save_row = await cursor.fetchone()
        if not save_row:
            return None

        script_id = save_row["script_id"]

        # Load script
        cursor = await db.execute(
            "SELECT content FROM scripts WHERE id = ?", (script_id,)
        )
        script_row = await cursor.fetchone()
        if script_row:
            script = json.loads(script_row["content"])
        else:
            try:
                script = ScriptLoader.load(script_id)
            except FileNotFoundError:
                return None

        # Get AI provider
        ai_provider = await _get_ai_provider_async()
        stage_models = await _get_stage_models_async()

        # Create session
        session = GameSession(script, ai_provider, save_id=save_id, stage_models=stage_models)

        # Load tree nodes (ordered by created_at to ensure children_ids ordering)
        cursor = await db.execute(
            "SELECT * FROM tree_nodes WHERE save_id = ? ORDER BY created_at ASC, turn_number ASC",
            (save_id,),
        )
        rows = await cursor.fetchall()

        for row in rows:
            node_data = {
                "id": row["id"],
                "parent_id": row["parent_id"],
                "children_ids": [],
                "game_time": row["game_time"] or "",
                "turn_number": row["turn_number"] or 0,
                "player_action": json.loads(row["player_action"])
                if row["player_action"]
                else None,
                "ai_response": row["ai_response"] or "",
                "choices_presented": json.loads(row["choices_presented"])
                if row["choices_presented"]
                else [],
                "dice_rolls": json.loads(row["dice_rolls"])
                if row["dice_rolls"]
                else [],
                "state_changes": json.loads(row["state_changes"])
                if row["state_changes"]
                else [],
                "triggered_events": json.loads(row["triggered_events"])
                if row["triggered_events"]
                else [],
                "state_snapshot": json.loads(row["state_snapshot"])
                if row["state_snapshot"]
                else {},
                "created_at": row["created_at"] or "",
            }
            # 从 snapshot 中恢复 regenerate 上下文（_save_node 写入的 _node_context）
            nc = node_data["state_snapshot"].pop("_node_context", None)
            if nc and isinstance(nc, dict):
                node_data["check_result"] = nc.get("check_result")
                node_data["triggered_consequences"] = nc.get("triggered_consequences")
                node_data["achieved_milestones"] = nc.get("achieved_milestones")
                if nc.get("thinking"):
                    node_data["thinking"] = nc["thinking"]
                if nc.get("scene_image_path"):
                    node_data["scene_image_path"] = nc["scene_image_path"]
            session.world_tree.nodes[row["id"]] = node_data

        # Rebuild parent-child relationships
        for node_id, node in session.world_tree.nodes.items():
            parent_id = node.get("parent_id")
            if parent_id and parent_id in session.world_tree.nodes:
                if node_id not in session.world_tree.nodes[parent_id]["children_ids"]:
                    session.world_tree.nodes[parent_id]["children_ids"].append(node_id)

        # Set root and active node
        for node_id, node in session.world_tree.nodes.items():
            if node["parent_id"] is None:
                session.world_tree.root_node_id = node_id
                break

        active_id = save_row["active_node_id"]
        if active_id and active_id in session.world_tree.nodes:
            session.world_tree.active_node_id = active_id
            active_node = session.world_tree.nodes[active_id]
            session.current_state = copy.deepcopy(active_node.get("state_snapshot", {}))
            session.turn_number = active_node.get("turn_number", 0)

        # Architecture#2: 恢复 authors_note
        authors_note = None
        an_position = "end"
        an_depth = 4
        try:
            authors_note = save_row["authors_note"]
            an_position = save_row["authors_note_position"] or "end"
            an_depth = save_row["authors_note_depth"] or 4
        except (KeyError, IndexError):
            pass
        if authors_note:
            session.set_authors_note(authors_note, an_position, an_depth)

        # Restore negative prompt
        try:
            neg_prompt = save_row.get("negative_prompt", "") or ""
            if neg_prompt:
                session.negative_prompt = neg_prompt
        except (KeyError, AttributeError):
            pass

        # Restore logit bias
        try:
            lb_raw = save_row.get("logit_bias", "[]") or "[]"
            lb = json.loads(lb_raw)
            if lb:
                session.logit_bias = lb
        except (KeyError, AttributeError, json.JSONDecodeError):
            pass

        # Reload dynamic lorebook entries into the Lorebook instance
        dynamic_lore = session.current_state.get("dynamic_lorebook", [])
        if dynamic_lore:
            session.prompt_builder.lorebook.add_entries(dynamic_lore)

        # Restore dynamic NPCs: sync state NPCs back to script cache and prompt_builder
        script_npc_ids = {n["id"] for n in session.script.get("npcs", []) if "id" in n}
        for npc_id, npc_st in session.current_state.get("npcs", {}).items():
            if npc_id not in script_npc_ids and isinstance(npc_st, dict):
                session.script.setdefault("npcs", []).append({"id": npc_id, **npc_st})
                session.prompt_builder._npc_name_map[npc_id] = npc_st.get("name", npc_id)
                session.prompt_builder._npc_by_id[npc_id] = {"id": npc_id, **npc_st}

        # Restore dynamic story content (trees, events, lorebook from AI expansion)
        if session.current_state.get("dynamic_story_content"):
            session._restore_dynamic_story()

        # Re-index lorebook entries to VectorMemory (collection may be stale or missing)
        if session.vector_memory:
            lore_batch = []
            for entry in session.prompt_builder.lorebook.entries:
                if entry.content and len(entry.content) >= 20:
                    lore_batch.append((entry.id, entry.content, {
                        "entry_type": entry.entry_type,
                        "comment": entry.comment,
                    }))
            if lore_batch:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, session.vector_memory.add_lorebook_batch, lore_batch)

        # 初始化活跃时间
        session._last_activity_time = time.time()

        # Inject image provider if configured
        try:
            from api.config_routes import get_image_provider_instance
            session._image_provider = await get_image_provider_instance()
        except Exception:
            session._image_provider = None

        _sessions_put(save_id, session)
        logger.info("会话恢复完成 — save_id=%s, nodes=%d, turn=%d",
                     save_id, len(session.world_tree.nodes), session.turn_number)
        return session


async def _update_node_in_db(session: GameSession, result: dict):
    """Update an existing node in the database (used by regenerate/swipe)."""
    async with get_db() as db:
        node_id = result.get("node_id", "")
        node = session.world_tree.get_node(node_id)
        if node:
            snapshot_to_save = session.current_state
            node_context = {}
            if node.get("check_result"):
                node_context["check_result"] = node["check_result"]
            if node.get("triggered_consequences"):
                node_context["triggered_consequences"] = node["triggered_consequences"]
            if node.get("achieved_milestones"):
                node_context["achieved_milestones"] = node["achieved_milestones"]
            if node.get("thinking"):
                node_context["thinking"] = node["thinking"]
            if node_context:
                snapshot_to_save = {**snapshot_to_save, "_node_context": node_context}

            await db.execute(
                """UPDATE tree_nodes
                   SET ai_response = ?, choices_presented = ?,
                       state_changes = ?, state_snapshot = ?
                   WHERE id = ? AND save_id = ?""",
                (
                    result.get("narrative", ""),
                    json.dumps(result.get("choices", []), ensure_ascii=False),
                    json.dumps(result.get("state_changes", []), ensure_ascii=False),
                    json.dumps(snapshot_to_save, ensure_ascii=False),
                    node_id,
                    session.save_id,
                ),
            )
            await db.commit()
