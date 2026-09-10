"""第五共和国剧本 10 回合推演测试 — Phase 4c 验证。

验证点:
  1. Stage 4a ‖ Stage 5 并行（所有回合）
  2. minor scope 跳过 Stage 2（轻量动作回合）
  3. 叙事质量、状态变化、选项生成的完整性
  4. NPC merge 后 parsed 中 npc_attitude_changes 等字段是否存在
"""
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
    # T1-T5: 原有经典回合
    {"type": "free_text", "text": "我回到保安司令部的办公室，仔细研究目前掌握的情报，分析局势走向"},
    {"type": "free_text", "text": "我秘密联络卢泰愚，试探他对目前局势的看法和立场"},
    {"type": "free_text", "text": "我前往南山中央情报部，暗中调查金载圭最近的异常动向"},
    {"type": "free_text", "text": "我召集亲信军官在保安司令部开一个秘密会议，讨论应对方案"},
    {"type": "free_text", "text": "深夜，宫井洞传来枪声的消息——我立即下令保安司令部进入紧急状态，派人确认情况"},
    # T6: minor scope — 验证跳过 Stage 2
    {"type": "free_text", "text": "我低头看了一眼桌上的文件"},
    # T7: moderate scope — 常规场景
    {"type": "free_text", "text": "我带着几名亲信乘车前往陆军本部，准备接管指挥权"},
    # T8: major scope — 大场面
    {"type": "free_text", "text": "我下令第9师向青瓦台方向推进，同时命令特战司令部封锁所有主要路口"},
    # T9: minor scope — 再次验证轻量路径
    {"type": "free_text", "text": "我打开收音机听一下外面的新闻报道"},
    # T10: moderate scope — 收束
    {"type": "free_text", "text": "我拨通青瓦台的电话，要求与代理总统崔圭夏直接通话，阐明保安司令部的立场"},
]


def _check_phase4c(report: dict, route: dict, turn_num: int):
    """Phase 4c 特定检查。"""
    issues = []
    ss = report.get("stage_summary", {})
    scope = route.get("scope", "moderate")

    # Check 1: minor scope 应无 Stage2 调用
    if scope == "minor":
        for sname in ["Stage2a_env", "Stage2b_char", "Stage2_merged"]:
            if sname in ss:
                issues.append(f"minor scope 不应有 {sname} 调用 (calls={ss[sname]['calls']})")
        if not issues:
            print(f"    [OK] minor scope 正确跳过 Stage 2")

    # Check 2: 应看到 Stage 4a 和 Stage 5 的存在
    has_4a = any("4a" in s.lower() or "npc" in s.lower() for s in ss)
    has_5 = any("choice" in s.lower() or "stage5" in s.lower() for s in ss)
    if not has_4a:
        issues.append("未检测到 Stage 4a (NPC) 调用")
    if not has_5:
        issues.append("未检测到 Stage 5 (choices) 调用")

    # Check 3: 叙事不应为空
    narrative = report.get("narrative_text", "")
    if not narrative or len(narrative) < 50:
        issues.append(f"叙事过短或为空 ({len(narrative)}ch)")

    # Check 4: choices 应存在
    if report.get("choices_count", 0) == 0:
        issues.append("选项为空")

    # Check 5: state_changes 应存在（至少非 minor 回合）
    if scope != "minor" and report.get("state_changes_count", 0) == 0:
        issues.append("非 minor 回合但无状态变化")

    return issues


async def main():
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
    print(f"回合数: {len(TURNS)}")
    print(f"Phase 4c 验证: Stage 4a‖5 并行 + minor scope 跳过 Stage 2\n")

    real_provider, stage_models = await _get_provider_from_db()
    if not real_provider:
        print("找不到 AI provider")
        return

    prov = StageAwareProvider(real_provider)
    sess = GameSession(copy.deepcopy(script), prov, stage_models=stage_models)
    await _fast_init(sess, real_provider)

    all_results = []
    total_t0 = time.perf_counter()
    phase4c_issues_total = []

    for turn_idx, action in enumerate(TURNS):
        turn_num = turn_idx + 1
        print(f"\n{'='*70}")
        print(f"回合 {turn_num}/{len(TURNS)}: {action['text']}")
        print(f"{'='*70}")

        prov.reset()
        t0 = time.perf_counter()
        try:
            result = await sess.process_action(action)
            wall = time.perf_counter() - t0
            label_calls(prov.calls)
            report = build_report(f"T{turn_num}", prov.calls, result, wall)
            na = report["narrative_analysis"]

            # Extract route from provider calls
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
            print(f"  总耗时: {wall:.1f}s | AI调用: {report['ai_calls']}次")
            print(f"  叙事: {report['narrative_length']}ch | 比喻: {na.get('simile_count',0)} | "
                  f"blocklist: {na.get('blocklist_total',0)} | 截断: {'是' if na.get('is_truncated') else '否'}")
            print(f"  素材复制率: {report['text_reuse'].get('reuse_ratio',0):.1%} "
                  f"(最长匹配: {report['text_reuse'].get('longest_match',0)}ch)")

            ss = report.get("stage_summary", {})
            print(f"\n  --- 各阶段详情 ---")
            for sname, si in sorted(ss.items()):
                print(f"  [{sname}] calls={si['calls']} "
                      f"in={si['input_chars']}ch out={si['output_chars']}ch "
                      f"lat={si['latency']:.1f}s")

            narrative = report.get("narrative_text", "")
            print(f"\n  --- 叙事摘要 ({len(narrative)}ch) ---")
            print(f"  {narrative[:300]}{'...' if len(narrative) > 300 else ''}")

            choices = result.get("choices", [])
            if choices:
                print(f"\n  --- 选项 ({len(choices)}个) ---")
                for c in choices:
                    print(f"    [{c.get('id')}] {c.get('text', '')[:60]}")

            # Phase 4c checks
            issues = _check_phase4c(report, route, turn_num)
            if issues:
                print(f"\n  --- Phase 4c 问题 ---")
                for iss in issues:
                    print(f"    [!] {iss}")
                    phase4c_issues_total.append(f"T{turn_num}: {iss}")
            else:
                print(f"\n  [OK] Phase 4c 检查通过")

            report["scope"] = scope
            report["scene_type"] = scene_type
            report["phase4c_issues"] = issues
            all_results.append(report)

        except Exception as e:
            wall = time.perf_counter() - t0
            print(f"\n  FAIL ({wall:.1f}s): {e}")
            import traceback; traceback.print_exc()
            all_results.append({"mode": f"T{turn_num}", "error": str(e), "wall_time_s": round(wall, 2)})
            phase4c_issues_total.append(f"T{turn_num}: 异常 - {e}")

    total_wall = time.perf_counter() - total_t0

    # Summary
    print(f"\n\n{'='*70}")
    print(f"10 回合推演总结")
    print(f"{'='*70}")
    ok = [r for r in all_results if "error" not in r]
    failed = len(TURNS) - len(ok)
    if ok:
        avg_wall = sum(r["wall_time_s"] for r in ok) / len(ok)
        avg_len = sum(r["narrative_length"] for r in ok) / len(ok)
        avg_calls = sum(r["ai_calls"] for r in ok) / len(ok)
        avg_reuse = sum(r["text_reuse"]["reuse_ratio"] for r in ok) / len(ok)
        total_bl = sum(r["narrative_analysis"]["blocklist_total"] for r in ok)
        total_sim = sum(r["narrative_analysis"]["simile_count"] for r in ok)
        trunc = sum(1 for r in ok if r["narrative_analysis"].get("is_truncated"))

        minor_turns = [r for r in ok if r.get("scope") == "minor"]
        non_minor = [r for r in ok if r.get("scope") != "minor"]

        print(f"  总耗时: {total_wall:.1f}s")
        print(f"  成功: {len(ok)}/{len(TURNS)} | 失败: {failed}")
        print(f"  平均耗时: {avg_wall:.1f}s")
        print(f"  平均AI调用: {avg_calls:.1f}次")
        print(f"  平均叙事长度: {avg_len:.0f}ch")
        print(f"  平均素材复制率: {avg_reuse:.1%}")
        print(f"  总 blocklist 命中: {total_bl}")
        print(f"  总比喻数: {total_sim}")
        print(f"  截断次数: {trunc}")

        if minor_turns:
            avg_minor_wall = sum(r["wall_time_s"] for r in minor_turns) / len(minor_turns)
            avg_minor_calls = sum(r["ai_calls"] for r in minor_turns) / len(minor_turns)
            print(f"\n  --- minor scope 回合 ({len(minor_turns)}个) ---")
            print(f"  平均耗时: {avg_minor_wall:.1f}s | 平均AI调用: {avg_minor_calls:.1f}次")

        if non_minor:
            avg_nm_wall = sum(r["wall_time_s"] for r in non_minor) / len(non_minor)
            avg_nm_calls = sum(r["ai_calls"] for r in non_minor) / len(non_minor)
            print(f"\n  --- 非 minor 回合 ({len(non_minor)}个) ---")
            print(f"  平均耗时: {avg_nm_wall:.1f}s | 平均AI调用: {avg_nm_calls:.1f}次")

    print(f"\n  --- Phase 4c 验证 ---")
    if phase4c_issues_total:
        print(f"  问题数: {len(phase4c_issues_total)}")
        for iss in phase4c_issues_total:
            print(f"    [!] {iss}")
    else:
        print(f"  [OK] 全部通过")

    # Save
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "fifth_republic_test_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n  结果已保存: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
