"""第五共和国剧本 5 回合推演测试 — 分析各阶段输出质量。"""
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
)


TURNS = [
    {"type": "free_text", "text": "我回到保安司令部的办公室，仔细研究目前掌握的情报，分析局势走向"},
    {"type": "free_text", "text": "我秘密联络卢泰愚，试探他对目前局势的看法和立场"},
    {"type": "free_text", "text": "我前往南山中央情报部，暗中调查金载圭最近的异常动向"},
    {"type": "free_text", "text": "我召集亲信军官在保安司令部开一个秘密会议，讨论应对方案"},
    {"type": "free_text", "text": "深夜，宫井洞传来枪声的消息——我立即下令保安司令部进入紧急状态，派人确认情况"},
]


async def main():
    # Load script from DB
    import aiosqlite
    from config import DB_PATH
    async with aiosqlite.connect(str(DB_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT content FROM scripts WHERE id = 'fifth_republic_dawn'")
        row = await cursor.fetchone()
        if not row:
            print("找不到第五共和国剧本")
            return
        script = json.loads(row["content"])

    print(f"剧本: {script.get('script_name', '?')}")
    print(f"地点: {len(script.get('locations', []))}  NPC: {len(script.get('npcs', []))}")

    real_provider, stage_models = await _get_provider_from_db()
    if not real_provider:
        print("找不到 AI provider")
        return

    prov = StageAwareProvider(real_provider)
    sess = GameSession(copy.deepcopy(script), prov, stage_models=stage_models)
    await _fast_init(sess, real_provider)

    all_results = []
    for turn_idx, action in enumerate(TURNS):
        turn_num = turn_idx + 1
        print(f"\n{'='*70}")
        print(f"回合 {turn_num}/5: {action['text']}")
        print(f"{'='*70}")

        prov.reset()
        t0 = time.perf_counter()
        try:
            result = await sess.process_action(action)
            wall = time.perf_counter() - t0
            label_calls(prov.calls)
            report = build_report(f"T{turn_num}", prov.calls, result, wall)
            na = report["narrative_analysis"]

            print(f"\n  总耗时: {wall:.1f}s | AI调用: {report['ai_calls']}次")
            print(f"  叙事: {report['narrative_length']}ch | 比喻: {na.get('simile_count',0)} | "
                  f"blocklist: {na.get('blocklist_total',0)} | 截断: {'是' if na.get('is_truncated') else '否'}")
            print(f"  素材复制率: {report['text_reuse'].get('reuse_ratio',0):.1%} "
                  f"(最长匹配: {report['text_reuse'].get('longest_match',0)}ch)")

            # Stage details
            ss = report.get("stage_summary", {})
            print(f"\n  --- 各阶段详情 ---")
            for sname, si in sorted(ss.items()):
                print(f"  [{sname}] calls={si['calls']} "
                      f"in={si['input_chars']}ch out={si['output_chars']}ch "
                      f"lat={si['latency']:.1f}s")
                for i, out in enumerate(si.get("outputs", [])):
                    if out:
                        preview = out.replace('\n', ' ')[:120]
                        print(f"    output[{i}]: {preview}...")

            # Narrative
            narrative = report.get("narrative_text", "")
            print(f"\n  --- 完整叙事 ({len(narrative)}ch) ---")
            print(f"  {narrative}")

            # State changes
            sc = result.get("state_changes", [])
            if sc:
                print(f"\n  --- 状态变化 ({len(sc)}项) ---")
                for s in sc[:5]:
                    print(f"    {s}")

            # Choices
            choices = result.get("choices", [])
            if choices:
                print(f"\n  --- 选项 ({len(choices)}个) ---")
                for c in choices:
                    print(f"    [{c.get('id')}] {c.get('text', '')[:60]}")

            all_results.append(report)

        except Exception as e:
            wall = time.perf_counter() - t0
            print(f"\n  FAIL ({wall:.1f}s): {e}")
            import traceback; traceback.print_exc()
            all_results.append({"mode": f"T{turn_num}", "error": str(e), "wall_time_s": round(wall, 2)})

    # Summary
    print(f"\n\n{'='*70}")
    print(f"总结")
    print(f"{'='*70}")
    ok = [r for r in all_results if "error" not in r]
    if ok:
        avg_wall = sum(r["wall_time_s"] for r in ok) / len(ok)
        avg_len = sum(r["narrative_length"] for r in ok) / len(ok)
        avg_reuse = sum(r["text_reuse"]["reuse_ratio"] for r in ok) / len(ok)
        total_bl = sum(r["narrative_analysis"]["blocklist_total"] for r in ok)
        total_sim = sum(r["narrative_analysis"]["simile_count"] for r in ok)
        trunc = sum(1 for r in ok if r["narrative_analysis"].get("is_truncated"))
        print(f"  成功: {len(ok)}/5")
        print(f"  平均耗时: {avg_wall:.1f}s")
        print(f"  平均叙事长度: {avg_len:.0f}ch")
        print(f"  平均素材复制率: {avg_reuse:.1%}")
        print(f"  总 blocklist 命中: {total_bl}")
        print(f"  总比喻数: {total_sim}")
        print(f"  截断次数: {trunc}")

    # Save
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fifth_republic_test_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
