"""单组效果测试：运行当前管线（含 Stage 4b 拆分），输出各阶段摘要。"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.game_session import GameSession
from benchmark_narrative_quality import (
    StageAwareProvider, label_calls, build_report, _get_provider_from_db, _fast_init,
    SCENARIO_ACTIONS,
)


async def main():
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "scripts", "dnd_open_world_ashenvale.json")
    if not os.path.exists(script_path):
        print("找不到 DND 剧本")
        return

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    print(f"脚本: {script.get('script_name', '?')}")
    real_provider, stage_models = await _get_provider_from_db()
    if not real_provider:
        print("找不到 AI provider")
        return

    all_results = []
    for idx, scenario in enumerate(SCENARIO_ACTIONS):
        label = scenario["label"]
        print(f"\n{'='*60}")
        print(f"场景 {idx+1}/{len(SCENARIO_ACTIONS)}: {label}")
        print(f"{'='*60}")

        prov = StageAwareProvider(real_provider)
        sess = GameSession(copy.deepcopy(script), prov, stage_models=stage_models)
        await _fast_init(sess, real_provider)

        for turn_idx, action in enumerate(scenario["turns"]):
            turn_num = turn_idx + 1
            print(f"\n  回合 {turn_num}: {action['text'][:40]}...")
            prov.reset()
            t0 = time.perf_counter()
            try:
                result = await sess.process_action(action)
                wall = time.perf_counter() - t0
                label_calls(prov.calls)
                report = build_report(f"{label} T{turn_num}", prov.calls, result, wall)
                na = report["narrative_analysis"]

                print(f"  耗时: {wall:.1f}s")
                print(f"  叙事长度: {report['narrative_length']}ch")
                print(f"  比喻数: {na.get('simile_count', 0)}")
                print(f"  blocklist命中: {na.get('blocklist_total', 0)}")
                print(f"  最大段内感官: {na.get('max_sensory_per_paragraph', 0)}")
                print(f"  截断: {'是' if na.get('is_truncated') else '否'}")

                # 各阶段输出摘要
                ss = report.get("stage_summary", {})
                for stage_name in ["route", "plot", "env", "char", "compose",
                                   "state_0", "state_1", "state_2"]:
                    si = ss.get(stage_name)
                    if si:
                        print(f"  [{stage_name}] input={si.get('input_chars',0)}ch "
                              f"output={si.get('output_chars',0)}ch "
                              f"latency={si.get('latency_s',0):.1f}s")

                # 打印叙事前 200 字
                narrative = report.get("narrative_text", "")
                if narrative:
                    print(f"\n  --- 叙事前200字 ---")
                    print(f"  {narrative[:200]}...")

                all_results.append(report)

            except Exception as e:
                wall = time.perf_counter() - t0
                print(f"  FAIL: {e}")
                import traceback; traceback.print_exc()
                all_results.append({"mode": f"{label} T{turn_num}", "error": str(e)})

    # 保存结果
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "single_test_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存到: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
