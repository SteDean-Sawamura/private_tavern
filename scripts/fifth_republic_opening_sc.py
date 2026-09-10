"""为 fifth_republic_dawn 所有开局选择补充 state_changes。"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "tavern.db"
SCRIPT_ID = "fifth_republic_dawn"

# ═══════════════════════════════════════════════
# state_changes 定义
# 格式: preset_id → { choice_id → [ {target, op, value} ... ] }
# target 使用 "script_variables.var_id" 路径
# ═══════════════════════════════════════════════

def sc(target, value, op="add"):
    return {"target": f"script_variables.{target}", "op": op, "value": value}


OPENING_SC = {
    "kcia_operative": {
        "open_0": [
            sc("cover_integrity", 5),
            sc("intelligence_leaks", -3),
            sc("communication_security", 3),
        ],
        "open_1": [
            sc("hanahoe_infiltration", 5),
            sc("cover_integrity", -3),
            sc("intelligence_leaks", 3),
        ],
        "open_2": [
            sc("key_documents_found", 2),
            sc("communication_security", -2),
            sc("cover_integrity", 3),
        ],
    },
    "hanahoe_officer": {
        "open_0": [
            sc("coup_readiness", 5),
            sc("troops_loyalty", 3),
            sc("weapons_secured", 2),
        ],
        "open_1": [
            sc("hanahoe_infiltration", 5),
            sc("coup_readiness", 3),
            sc("cover_integrity", -3),
        ],
        "open_2": [
            sc("troops_loyalty", 5),
            sc("weapons_secured", 3),
            sc("communication_security", -2),
        ],
    },
    "dissident_journalist": {
        "open_0": [
            sc("media_freedom", 5),
            sc("cover_integrity", -3),
            sc("protest_intensity", 3),
        ],
        "open_1": [
            sc("cover_integrity", 5),
            sc("intelligence_leaks", -2),
            sc("communication_security", 3),
        ],
        "open_2": [
            sc("key_documents_found", 3),
            sc("us_confidence", 2),
            sc("cover_integrity", -2),
        ],
        "open_3": [
            sc("protest_intensity", 5),
            sc("media_freedom", 3),
            sc("student_network_size", 2),
        ],
    },
    "power_broker": {
        "open_0": [
            sc("regime_stability", 5),
            sc("cover_integrity", 3),
            sc("intelligence_leaks", -3),
        ],
        "open_1": [
            sc("hanahoe_infiltration", 5),
            sc("coup_readiness", 3),
            sc("cover_integrity", -5),
        ],
        "open_2": [
            sc("intelligence_leaks", 5),
            sc("key_documents_found", 2),
            sc("cover_integrity", -3),
        ],
        "open_3": [
            sc("safe_house_count", 2),
            sc("communication_security", 3),
            sc("cover_integrity", 3),
        ],
    },
    "pss_bodyguard": {
        "open_0": [
            sc("troops_loyalty", 5),
            sc("regime_stability", 3),
            sc("cover_integrity", 3),
        ],
        "open_1": [
            sc("communication_security", 5),
            sc("weapons_secured", 3),
            sc("military_alert_level", 1),
        ],
        "open_2": [
            sc("cover_integrity", 5),
            sc("troops_loyalty", 3),
            sc("regime_stability", -3),
        ],
        "open_3": [
            sc("weapons_secured", 5),
            sc("military_alert_level", 1),
            sc("troops_loyalty", -2),
        ],
    },
    "orthodox_colonel": {
        "open_0": [
            sc("troops_loyalty", 5),
            sc("regime_stability", 3),
            sc("cover_integrity", 3),
        ],
        "open_1": [
            sc("key_documents_found", 3),
            sc("communication_security", 3),
            sc("hanahoe_infiltration", -3),
        ],
        "open_2": [
            sc("cover_integrity", 5),
            sc("troops_loyalty", 3),
            sc("intelligence_leaks", -3),
        ],
        "open_3": [
            sc("hanahoe_infiltration", -5),
            sc("troops_loyalty", 5),
            sc("regime_stability", 3),
        ],
    },
    "student_activist": {
        "open_0": [
            sc("student_network_size", 5),
            sc("protest_intensity", 3),
            sc("cover_integrity", -2),
        ],
        "open_1": [
            sc("protest_intensity", 5),
            sc("media_freedom", 3),
            sc("cover_integrity", -3),
        ],
        "open_2": [
            sc("cover_integrity", 5),
            sc("communication_security", 3),
            sc("student_network_size", 2),
        ],
        "open_3": [
            sc("media_freedom", 5),
            sc("protest_intensity", 3),
            sc("student_network_size", 3),
        ],
    },
    "us_embassy_liaison": {
        "open_0": [
            sc("us_confidence", 5),
            sc("communication_security", 3),
            sc("diplomatic_crisis_level", -2),
        ],
        "open_1": [
            sc("intelligence_leaks", 5),
            sc("key_documents_found", 2),
            sc("cover_integrity", -3),
        ],
        "open_2": [
            sc("diplomatic_crisis_level", -3),
            sc("us_confidence", 3),
            sc("cover_integrity", 3),
        ],
        "open_3": [
            sc("key_documents_found", 3),
            sc("communication_security", -3),
            sc("us_confidence", 3),
        ],
    },
    "chaebol_fixer": {
        "open_0": [
            sc("key_documents_found", 3),
            sc("intelligence_leaks", 3),
            sc("cover_integrity", -3),
        ],
        "open_1": [
            sc("regime_stability", 3),
            sc("cover_integrity", 5),
            sc("us_confidence", 2),
        ],
        "open_2": [
            sc("protest_intensity", -3),
            sc("communication_security", 3),
            sc("cover_integrity", 3),
        ],
        "open_3": [
            sc("intelligence_leaks", 5),
            sc("key_documents_found", 3),
            sc("cover_integrity", -5),
        ],
    },
    "nk_sleeper_agent": {
        "open_0": [
            sc("communication_security", 5),
            sc("cover_integrity", 5),
            sc("intelligence_leaks", -3),
        ],
        "open_1": [
            sc("key_documents_found", 3),
            sc("cover_integrity", -3),
            sc("protest_intensity", 3),
        ],
        "open_2": [
            sc("cover_integrity", 5),
            sc("communication_security", 3),
            sc("intelligence_leaks", -2),
        ],
        "open_3": [
            sc("protest_intensity", 5),
            sc("cover_integrity", -5),
            sc("communication_security", -3),
        ],
    },
    "blue_house_secretary": {
        "open_0": [
            sc("cover_integrity", 5),
            sc("regime_stability", 3),
            sc("intelligence_leaks", -3),
        ],
        "open_1": [
            sc("key_documents_found", 3),
            sc("troops_loyalty", 3),
            sc("cover_integrity", -3),
        ],
        "open_2": [
            sc("regime_stability", 5),
            sc("intelligence_leaks", -3),
            sc("cover_integrity", 3),
        ],
    },
    "opposition_aide": {
        "open_0": [
            sc("media_freedom", 5),
            sc("protest_intensity", 3),
            sc("cover_integrity", -2),
        ],
        "open_1": [
            sc("regime_stability", -3),
            sc("us_confidence", 3),
            sc("student_network_size", 3),
        ],
        "open_2": [
            sc("key_documents_found", 3),
            sc("safe_house_count", 2),
            sc("cover_integrity", -3),
        ],
        "open_3": [
            sc("cover_integrity", 5),
            sc("communication_security", 3),
            sc("protest_intensity", 3),
        ],
    },
}


def main():
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute("SELECT content FROM scripts WHERE id=?", (SCRIPT_ID,)).fetchone()
    if not row:
        print(f"ERROR: Script '{SCRIPT_ID}' not found.")
        return

    script = json.loads(row[0])
    presets = script.get("player_presets", [])

    updated_count = 0
    for preset in presets:
        pid = preset.get("id")
        if pid not in OPENING_SC:
            print(f"  SKIP preset '{pid}' — no SC data defined")
            continue

        sc_map = OPENING_SC[pid]
        choices = preset.get("opening_choices", [])
        for choice in choices:
            cid = choice.get("id")
            if cid in sc_map:
                if "result" not in choice:
                    choice["result"] = {"type": "deterministic", "description": "", "state_changes": []}
                choice["result"]["state_changes"] = sc_map[cid]
                updated_count += 1
                print(f"  OK {pid}/{cid}: {len(sc_map[cid])} changes")
            else:
                print(f"  WARN {pid}/{cid}: no SC mapping")

    # Save back
    script["player_presets"] = presets
    conn.execute("UPDATE scripts SET content=? WHERE id=?", (json.dumps(script, ensure_ascii=False), SCRIPT_ID))
    conn.commit()
    conn.close()

    print(f"\nDone. Updated {updated_count} choices with state_changes.")


if __name__ == "__main__":
    main()
