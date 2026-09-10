"""第五共和国剧本 10 回合推演测试 — 朴敏哲（青瓦台秘书官）预设。

验证点:
  1. 预设角色正确应用（属性、位置、opening_text）
  2. Stage 4a ‖ Stage 5 并行
  3. minor scope 跳过 Stage 2
  4. 叙事质量、状态变化、选项生成的完整性
  5. 新系统：check_momentum, npc_interaction_log, location_memory, current_mood, faction_reputation
"""
import asyncio
import copy
import io
import json
import os
import sys
import time

if sys.stdout and hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.game_session import GameSession
from benchmark_narrative_quality import (
    StageAwareProvider, label_calls, build_report, _get_provider_from_db, _fast_init,
)

PRESET_ID = "blue_house_secretary"

TURNS = [
    # T1: 角色身份确立 — 青瓦台内部日常
    {"type": "free_text", "text": "我把釜马事态的机密简报送到政务首席秘书办公室，顺便留意一下办公室内的气氛和动向"},
    # T2: 社交/情报收集 — NPC互动
    {"type": "free_text", "text": "我找到秘书室长金桂元，委婉地询问明天值晚班的具体安排，试探他是否知道什么内幕"},
    # T3: 探索/调查 — 环境互动
    {"type": "free_text", "text": "趁走廊无人，我悄悄翻阅了总统日程表的副本，想确认今晚宫井洞晚宴的出席名单"},
    # T4: minor scope — 验证轻量路径
    {"type": "free_text", "text": "我整理了一下桌上的文件"},
    # T5: 高紧张 — 事变之夜
    {"type": "free_text", "text": "枪声从宫井洞方向传来！我立即锁上办公室的门，拨打警卫室的内线电话试图确认情况"},
    # T6: 危机应对 — major scope
    {"type": "free_text", "text": "电话打不通，走廊传来军靴声和喊叫。我决定从侧门溜出去，沿着后花园小径赶往秘书室长办公室"},
    # T7: 社交/抉择 — 关键NPC对话
    {"type": "free_text", "text": "我找到惊慌失措的同僚们，告诉他们先不要轻举妄动，然后试着联系青瓦台外面的人了解局势"},
    # T8: minor scope — 再次验证
    {"type": "free_text", "text": "我透过窗户向外张望"},
    # T9: 政治博弈 — 高强度
    {"type": "free_text", "text": "保安司令部的军人冲进青瓦台，要求所有秘书室人员集中到大厅。我必须决定是服从还是想办法脱身"},
    # T10: 收束 — 命运抉择
    {"type": "free_text", "text": "我选择暂时服从，但在被带走之前偷偷将一份关键文件藏进了花盆底下，这或许日后能成为保命的筹码"},
]


def _apply_preset(script: dict, preset_id: str):
    """Replicate the preset application logic from game_routes.py."""
    presets = script.get("player_presets", [])
    preset = next((p for p in presets if p.get("id") == preset_id), None)
    if not preset:
        raise ValueError(f"Preset '{preset_id}' not found")

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
    if preset.get("attributes"):
        base_attrs = pc.get("attributes", {})
        _dn_to_key = {}
        for bk, bv in base_attrs.items():
            if isinstance(bv, dict):
                dn = bv.get("display_name") or bv.get("name", "")
                if dn:
                    _dn_to_key[dn] = bk
        for k, v in preset["attributes"].items():
            target_key = k if k in base_attrs else _dn_to_key.get(k, k)
            if target_key in base_attrs:
                if isinstance(base_attrs[target_key], dict):
                    base_attrs[target_key]["value"] = v if isinstance(v, (int, float)) else v.get("value", base_attrs[target_key].get("value", 50))
                else:
                    base_attrs[target_key] = v
            else:
                base_attrs[target_key] = v
        pc["attributes"] = base_attrs
    if preset.get("known_npcs"):
        script["_preset_known_npcs"] = preset["known_npcs"]
    if preset.get("persistent_state_overrides"):
        script["_preset_ps_overrides"] = preset["persistent_state_overrides"]
    if preset.get("opening_text"):
        script["_preset_opening_text"] = preset["opening_text"]
    if preset.get("opening_choices"):
        script["_preset_opening_choices"] = preset["opening_choices"]
    if preset.get("npc_overrides"):
        for override in preset["npc_overrides"]:
            npc_id = override.get("id")
            if not npc_id:
                continue
            for npc in script.get("npcs", []):
                if npc["id"] == npc_id:
                    for field in ("name", "personality", "bio"):
                        if override.get(field):
                            npc[field] = override[field]
                    break
    return preset


def _check_turn(report: dict, route: dict, turn_num: int, state: dict):
    """Per-turn validation checks."""
    issues = []
    ss = report.get("stage_summary", {})
    scope = route.get("scope", "moderate")

    # Check 1: minor scope should skip Stage 2
    if scope == "minor":
        for sname in ["Stage2a_env", "Stage2b_char", "Stage2_merged"]:
            if sname in ss:
                issues.append(f"minor scope should not have {sname} (calls={ss[sname]['calls']})")
        if not issues:
            print(f"    [OK] minor scope correctly skipped Stage 2")

    # Check 2: Stage 4a and Stage 5 should exist
    has_4a = any("4a" in s.lower() or "npc" in s.lower() for s in ss)
    has_5 = any("choice" in s.lower() or "stage5" in s.lower() for s in ss)
    if not has_4a:
        issues.append("No Stage 4a (NPC) call detected")
    if not has_5:
        issues.append("No Stage 5 (choices) call detected")

    # Check 3: narrative should not be empty
    narrative = report.get("narrative_text", "")
    if not narrative or len(narrative) < 50:
        issues.append(f"Narrative too short ({len(narrative)}ch)")

    # Check 4: choices should exist
    if report.get("choices_count", 0) == 0:
        issues.append("No choices generated")

    # Check 5: state_changes for non-minor turns
    if scope != "minor" and report.get("state_changes_count", 0) == 0:
        issues.append("Non-minor turn but no state changes")

    return issues


def _check_new_systems(state: dict, turn_num: int):
    """Check if new systems are active and producing data."""
    findings = []

    # check_momentum
    momentum = state.get("check_momentum", {})
    if momentum:
        for attr, data in momentum.items():
            streak = data.get("streak", 0)
            if streak != 0:
                findings.append(f"check_momentum: {attr} streak={streak}")

    # npc_interaction_log
    ilog = state.get("npc_interaction_log", {})
    if ilog:
        total = sum(len(v) for v in ilog.values())
        findings.append(f"npc_interaction_log: {len(ilog)} NPCs, {total} entries")

    # location_memory
    lmem = state.get("location_memory", {})
    if lmem:
        total = sum(len(v) for v in lmem.values())
        findings.append(f"location_memory: {len(lmem)} locations, {total} events")

    # NPC moods
    npcs = state.get("npcs", {})
    moods = {nid: n.get("current_mood") for nid, n in npcs.items()
             if isinstance(n, dict) and n.get("current_mood")}
    if moods:
        findings.append(f"NPC moods: {moods}")

    # faction_reputation
    frep = state.get("faction_reputation", {})
    if frep:
        changed = {k: v for k, v in frep.items()
                   if isinstance(v, dict) and v.get("value", 50) != 50}
        if changed:
            findings.append(f"faction_reputation changed: {list(changed.keys())}")

    return findings


async def main():
    import aiosqlite
    from config import DB_PATH
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT content FROM scripts WHERE id = 'fifth_republic_dawn'")
        row = await cursor.fetchone()
        if not row:
            print("Script 'fifth_republic_dawn' not found")
            return
        script = json.loads(row["content"])

    print(f"{'='*70}")
    print(f"  第五共和国 10 回合推演 — 朴敏哲（青瓦台秘书官）")
    print(f"{'='*70}")
    print(f"Script: {script.get('script_name', '?')}")
    print(f"Locations: {len(script.get('locations', []))}  NPCs: {len(script.get('npcs', []))}")

    # Apply preset
    script_copy = copy.deepcopy(script)
    preset = _apply_preset(script_copy, PRESET_ID)
    pc = script_copy.get("player_character", {})
    print(f"\nPreset: {preset.get('name')} ({PRESET_ID})")
    print(f"  Bio: {preset.get('bio', '')[:80]}")
    print(f"  Location: {preset.get('initial_location', '?')}")
    print(f"  Goal: {preset.get('long_term_goal', '')[:80]}")
    print(f"  Attributes: {json.dumps({k: v.get('value', v) if isinstance(v, dict) else v for k, v in pc.get('attributes', {}).items()}, ensure_ascii=False)}")
    has_opening = bool(script_copy.get("_preset_opening_text"))
    print(f"  Custom opening: {'Yes' if has_opening else 'No'}")
    print(f"\nTurns: {len(TURNS)}\n")

    real_provider, stage_models = await _get_provider_from_db()
    if not real_provider:
        print("ERROR: No AI provider configured")
        return

    prov = StageAwareProvider(real_provider)
    sess = GameSession(script_copy, prov, stage_models=stage_models)
    await _fast_init(sess, real_provider)

    # Verify preset was applied
    state = sess.current_state
    player = state.get("player", {})
    print(f"[Init] Player: {player.get('name')} @ {player.get('location')}")
    print(f"[Init] Attributes: {json.dumps(player.get('attributes', {}), ensure_ascii=False)}")
    assert player.get("name") == "朴敏哲", f"Preset not applied: got {player.get('name')}"
    assert player.get("location") == "blue_house", f"Wrong location: {player.get('location')}"

    all_results = []
    total_t0 = time.perf_counter()
    all_issues = []
    system_findings_by_turn = {}

    for turn_idx, action in enumerate(TURNS):
        turn_num = turn_idx + 1
        print(f"\n{'='*70}")
        print(f"Turn {turn_num}/{len(TURNS)}: {action['text'][:60]}...")
        print(f"{'='*70}")

        prov.reset()
        t0 = time.perf_counter()
        try:
            result = await sess.process_action(action)
            wall = time.perf_counter() - t0
            label_calls(prov.calls)
            report = build_report(f"T{turn_num}", prov.calls, result, wall)
            na = report["narrative_analysis"]

            # Extract route
            route = {}
            ss = report.get("stage_summary", {})
            if "Route" in ss:
                for out in ss["Route"].get("outputs", []):
                    if out and out.strip().startswith("{"):
                        try:
                            route = json.loads(out.strip())
                        except Exception:
                            pass
            scope = route.get("scope", "?")
            scene_type = route.get("scene_type", "?")

            print(f"\n  Route: scope={scope}, scene_type={scene_type}")
            print(f"  Wall: {wall:.1f}s | AI calls: {report['ai_calls']}")
            print(f"  Narrative: {report['narrative_length']}ch | Similes: {na.get('simile_count',0)} | "
                  f"Blocklist: {na.get('blocklist_total',0)} | Truncated: {'Y' if na.get('is_truncated') else 'N'}")
            print(f"  Reuse: {report['text_reuse'].get('reuse_ratio',0):.1%} "
                  f"(longest: {report['text_reuse'].get('longest_match',0)}ch)")

            print(f"\n  --- Stage Details ---")
            for sname, si in sorted(ss.items()):
                print(f"  [{sname}] calls={si['calls']} "
                      f"in={si['input_chars']}ch out={si['output_chars']}ch "
                      f"lat={si['latency']:.1f}s")

            narrative = report.get("narrative_text", "")
            print(f"\n  --- Narrative ({len(narrative)}ch) ---")
            print(f"  {narrative[:400]}{'...' if len(narrative) > 400 else ''}")

            choices = result.get("choices", [])
            if choices:
                print(f"\n  --- Choices ({len(choices)}) ---")
                for c in choices:
                    print(f"    [{c.get('id')}] {c.get('text', '')[:70]}")

            # State inspection
            cur_state = sess.current_state
            check_result = result.get("check_result")
            if check_result and check_result.get("outcome"):
                ms = check_result.get("momentum_streak", 0)
                mom_str = f" [momentum={ms}]" if ms else ""
                print(f"\n  --- Check Result ---")
                print(f"  {check_result.get('related_attribute')}: {check_result.get('outcome')}{mom_str}")

            # Validation
            issues = _check_turn(report, route, turn_num, cur_state)
            if issues:
                print(f"\n  --- Issues ---")
                for iss in issues:
                    print(f"    [!] {iss}")
                    all_issues.append(f"T{turn_num}: {iss}")
            else:
                print(f"\n  [OK] Turn checks passed")

            # New systems check
            sf = _check_new_systems(cur_state, turn_num)
            if sf:
                system_findings_by_turn[turn_num] = sf
                print(f"\n  --- New Systems ---")
                for f_ in sf:
                    print(f"    {f_}")

            report["scope"] = scope
            report["scene_type"] = scene_type
            report["issues"] = issues
            report["raw_calls"] = prov.calls
            report["choices"] = [{"id": c.get("id"), "text": c.get("text", "")} for c in choices]
            report["check_result"] = result.get("check_result")
            report["state_changes"] = result.get("state_changes", [])
            all_results.append(report)

        except Exception as e:
            wall = time.perf_counter() - t0
            print(f"\n  FAIL ({wall:.1f}s): {e}")
            import traceback; traceback.print_exc()
            all_results.append({"mode": f"T{turn_num}", "error": str(e), "wall_time_s": round(wall, 2)})
            all_issues.append(f"T{turn_num}: Exception - {e}")

    total_wall = time.perf_counter() - total_t0

    # ──────────── Final Report ────────────
    print(f"\n\n{'='*70}")
    print(f"  TEST REPORT: 朴敏哲 10-Turn Playtest")
    print(f"{'='*70}")

    ok = [r for r in all_results if "error" not in r]
    failed = len(TURNS) - len(ok)

    if ok:
        avg_wall = sum(r["wall_time_s"] for r in ok) / len(ok)
        avg_len = sum(r["narrative_length"] for r in ok) / len(ok)
        avg_calls = sum(r["ai_calls"] for r in ok) / len(ok)
        avg_reuse = sum(r.get("text_reuse", {}).get("reuse_ratio", 0) for r in ok) / len(ok)
        total_bl = sum(r.get("narrative_analysis", {}).get("blocklist_total", 0) for r in ok)
        total_sim = sum(r.get("narrative_analysis", {}).get("simile_count", 0) for r in ok)
        trunc = sum(1 for r in ok if r.get("narrative_analysis", {}).get("is_truncated"))

        minor_turns = [r for r in ok if r.get("scope") == "minor"]
        non_minor = [r for r in ok if r.get("scope") != "minor"]

        print(f"\n  [Overall]")
        print(f"  Total wall time: {total_wall:.1f}s")
        print(f"  Success: {len(ok)}/{len(TURNS)} | Failed: {failed}")
        print(f"  Avg wall time: {avg_wall:.1f}s")
        print(f"  Avg AI calls: {avg_calls:.1f}")
        print(f"  Avg narrative length: {avg_len:.0f}ch")
        print(f"  Avg reuse ratio: {avg_reuse:.1%}")
        print(f"  Total blocklist hits: {total_bl}")
        print(f"  Total similes: {total_sim}")
        print(f"  Truncated: {trunc}")

        if minor_turns:
            avg_minor_wall = sum(r["wall_time_s"] for r in minor_turns) / len(minor_turns)
            avg_minor_calls = sum(r["ai_calls"] for r in minor_turns) / len(minor_turns)
            print(f"\n  [Minor scope turns: {len(minor_turns)}]")
            print(f"  Avg wall time: {avg_minor_wall:.1f}s | Avg AI calls: {avg_minor_calls:.1f}")

        if non_minor:
            avg_nm_wall = sum(r["wall_time_s"] for r in non_minor) / len(non_minor)
            avg_nm_calls = sum(r["ai_calls"] for r in non_minor) / len(non_minor)
            print(f"\n  [Non-minor turns: {len(non_minor)}]")
            print(f"  Avg wall time: {avg_nm_wall:.1f}s | Avg AI calls: {avg_nm_calls:.1f}")

    print(f"\n  [Validation]")
    if all_issues:
        print(f"  Issues: {len(all_issues)}")
        for iss in all_issues:
            print(f"    [!] {iss}")
    else:
        print(f"  [OK] All turns passed")

    print(f"\n  [New Systems Summary]")
    final_state = sess.current_state
    momentum = final_state.get("check_momentum", {})
    ilog = final_state.get("npc_interaction_log", {})
    lmem = final_state.get("location_memory", {})
    frep = final_state.get("faction_reputation", {})
    npcs = final_state.get("npcs", {})
    moods = {nid: n.get("current_mood") for nid, n in npcs.items()
             if isinstance(n, dict) and n.get("current_mood")}

    print(f"  check_momentum: {json.dumps(momentum, ensure_ascii=False) if momentum else 'empty'}")
    print(f"  npc_interaction_log: {sum(len(v) for v in ilog.values())} entries across {len(ilog)} NPCs")
    print(f"  location_memory: {sum(len(v) for v in lmem.values())} events across {len(lmem)} locations")
    print(f"  NPC moods: {json.dumps(moods, ensure_ascii=False) if moods else 'none'}")
    frep_changed = {k: v.get("value", 50) for k, v in frep.items()
                    if isinstance(v, dict) and v.get("value", 50) != 50}
    print(f"  faction_reputation changes: {json.dumps(frep_changed, ensure_ascii=False) if frep_changed else 'none'}")

    # Scope distribution
    scopes = [r.get("scope", "?") for r in ok]
    from collections import Counter
    scope_dist = Counter(scopes)
    print(f"\n  [Scope Distribution]")
    for s, c in scope_dist.most_common():
        print(f"    {s}: {c} turns")

    # Save results
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "park_mincheol_test_results.json")
    save_data = {
        "preset": PRESET_ID,
        "preset_name": "朴敏哲",
        "turns": len(TURNS),
        "total_wall_s": round(total_wall, 1),
        "success": len(ok),
        "failed": failed,
        "issues": all_issues,
        "system_findings": {str(k): v for k, v in system_findings_by_turn.items()},
        "final_state_snapshot": {
            "check_momentum": momentum,
            "npc_interaction_log_size": {k: len(v) for k, v in ilog.items()},
            "location_memory_size": {k: len(v) for k, v in lmem.items()},
            "npc_moods": moods,
            "faction_reputation_changes": frep_changed,
        },
        "turn_results": all_results,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    print(f"\n  Results saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
