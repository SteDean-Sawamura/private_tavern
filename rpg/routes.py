"""FastAPI routes for the Tavern RPG engine."""

import os
import json
import glob
import sqlite3
from datetime import datetime
from typing import Dict, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from .llm_utils import set_ai_profile, llm_call, _llm_state
from .models import Turn, WorldStateManager, TAVERN_MODULES_AVAILABLE
from .agents import DirectorAgent, NPCAgent
from .script_builder import ScriptBuilder
from .session import TavernRPGSession, TAVERN_DB

# Engine module imports (needed for manual session init in start_game / load_save)
try:
    from engine.world_tree import WorldTree
    from engine.event_engine import EventEngine
    from engine.lorebook import Lorebook
    from engine.history_summarizer import HistorySummarizer
    from engine.dice import DiceRoller
    from engine.script_variables import ScriptVariables
    from engine.triggers import TriggerEngine
    from engine.meta_events import MetaEventBus
    try:
        from engine.vector_memory import VectorMemory, _VECTOR_AVAILABLE
    except ImportError:
        VectorMemory = None
        _VECTOR_AVAILABLE = False
    try:
        from engine.class_system import ClassRegistry
    except ImportError:
        ClassRegistry = None
except ImportError:
    WorldTree = None
    EventEngine = None
    Lorebook = None
    HistorySummarizer = None
    DiceRoller = None
    ScriptVariables = None
    TriggerEngine = None
    MetaEventBus = None
    VectorMemory = None
    _VECTOR_AVAILABLE = False
    ClassRegistry = None

# fallback DiceRoller
if DiceRoller is None:
    from engine.dice import DiceRoller

# fallback MetaEventBus
if MetaEventBus is None:
    from engine.meta_events import MetaEventBus

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(_BASE_DIR, "data", "scripts")

# ---------------------------------------------------------------------------
# Helper: DB init
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helper: location alias
# ---------------------------------------------------------------------------
_LOC_ALIAS = {"player_apartment": "player_home"}

def _fix_loc(loc_id):
    return _LOC_ALIAS.get(loc_id, loc_id) if loc_id else ""


# ---------------------------------------------------------------------------
# Helper: script scanning / loading
# ---------------------------------------------------------------------------

def scan_scripts():
    results = []
    for path in sorted(glob.glob(os.path.join(SCRIPTS_DIR, "*.json"))):
        fn = os.path.basename(path)
        if fn.startswith("enrich") or fn.endswith("_current.json"):
            continue
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


# ---------------------------------------------------------------------------
# Session singleton
# ---------------------------------------------------------------------------
_state: Dict[str, Any] = {"session": None}

def _s():
    return _state["session"]


# ---------------------------------------------------------------------------
# Helper: build a TavernRPGSession with engine modules
# ---------------------------------------------------------------------------

def _init_engine_modules(s, data, script_id):
    """Attach engine sub-systems to a manually created TavernRPGSession."""
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
            _set_engine_modules_none(s)
    else:
        _set_engine_modules_none(s)


def _set_engine_modules_none(s):
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


def _make_session(data, script_id):
    """Create a bare TavernRPGSession via __new__ and wire up common fields."""
    s = TavernRPGSession.__new__(TavernRPGSession)
    s.script_data = data
    s.world_state = WorldStateManager(data)
    s.npc_agents = {}
    for npc in data.get("npcs", []):
        npc_id = npc.get("id")
        s.npc_agents[npc_id] = NPCAgent(npc_id, npc, s.world_state)
    s._pending_turn = None
    s._last_validation = {}
    _init_engine_modules(s, data, script_id)
    return s


# ===================================================================
# FastAPI application & routes
# ===================================================================

def create_app() -> FastAPI:
    """Create and configure the FastAPI application with all routes."""
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


    # ---------------------------------------------------------------------------
    # Root
    # ---------------------------------------------------------------------------

    @app.get("/")
    async def root():
        html_path = os.path.join(_BASE_DIR, "rpg_map_ui_v2.html")
        if not os.path.exists(html_path):
            html_path = os.path.join(_BASE_DIR, "rpg_map_ui.html")
        if os.path.exists(html_path):
            with open(html_path, "r", encoding="utf-8") as f:
                return HTMLResponse(content=f.read())
        return HTMLResponse(content="<h1>RPG 系统启动中...</h1>")


    # ---------------------------------------------------------------------------
    # Script builder API
    # ---------------------------------------------------------------------------

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


    # ---------------------------------------------------------------------------
    # Scripts
    # ---------------------------------------------------------------------------

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


    # ---------------------------------------------------------------------------
    # Game lifecycle
    # ---------------------------------------------------------------------------

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

        s = _make_session(data, script_id)
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


    # ---------------------------------------------------------------------------
    # Game state
    # ---------------------------------------------------------------------------

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


    # ---------------------------------------------------------------------------
    # Turn processing
    # ---------------------------------------------------------------------------

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
                    # 生成检定结果叙事
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


    # ---------------------------------------------------------------------------
    # NPC chat / regeneration / swipe
    # ---------------------------------------------------------------------------

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


    # ---------------------------------------------------------------------------
    # Reset / save / load
    # ---------------------------------------------------------------------------

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

        s = _make_session(data, script_id)

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


    # ---------------------------------------------------------------------------
    # History / validation
    # ---------------------------------------------------------------------------

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


    @app.get("/api/validation")
    async def get_validation():
        s = _state.get("session")
        if not s:
            return {"status": "no_session"}
        v = getattr(s, '_last_validation', {})
        if v.get("pending"):
            return {"status": "pending"}
        return {"status": "done", "validation": v}


    # ===================================================================
    # Entrypoint
    # ===================================================================


    return app


def main():
    _init_rpg_saves_table()
    print("启动酒馆 RPG 多Agent推演系统原型\n")
    print("启动 FastAPI 服务...")
    print("访问 http://localhost:8080 打开 RPG 系统")
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8080)


if __name__ == "__main__":
    main()
