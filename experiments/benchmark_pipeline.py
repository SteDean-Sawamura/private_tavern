"""
实验：Legacy 单次生成 vs 8-Stage Pipeline 对比测试
==================================================
对比维度：
  1. 总延迟（端到端 wall-clock time）
  2. AI 调用次数 / 每次调用延迟
  3. 总输入 token / 总输出 token（估算）
  4. 叙事质量（字数、结构化输出完整性）
  5. 状态推演完整性（解析字段数量）

用法：
  cd 酒馆目录
  python -m experiments.benchmark_pipeline

需要已配置 AI 服务（在网页端设置好 profile）。
"""

import asyncio
import json
import copy
import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.game_session import GameSession
from engine.script_loader import ScriptLoader
from ai.response_parser import ResponseParser


# ──────────────────────────────────────────────
#  Instrumented AI Provider wrapper
# ──────────────────────────────────────────────

class InstrumentedProvider:
    """Wraps a real AIProvider, recording call count, latency, and token estimates."""

    def __init__(self, real_provider):
        self._real = real_provider
        self.calls: list[dict] = []

    def reset(self):
        self.calls = []

    @property
    def total_calls(self):
        return len(self.calls)

    @property
    def total_latency(self):
        return sum(c["latency"] for c in self.calls)

    @property
    def total_input_chars(self):
        return sum(c["input_chars"] for c in self.calls)

    @property
    def total_output_chars(self):
        return sum(c["output_chars"] for c in self.calls)

    def _estimate_input_chars(self, messages, system):
        total = len(system or "")
        for m in messages:
            c = m.get("content", "")
            total += len(c) if isinstance(c, str) else len(str(c))
        return total

    async def generate(self, messages, system="", **kwargs):
        input_chars = self._estimate_input_chars(messages, system)
        t0 = time.perf_counter()
        result = await self._real.generate(messages, system=system, **kwargs)
        elapsed = time.perf_counter() - t0
        self.calls.append({
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": len(result or ""),
            "method": "generate",
        })
        return result

    async def generate_stream(self, messages, system="", raw=False, **kwargs):
        input_chars = self._estimate_input_chars(messages, system)
        t0 = time.perf_counter()
        chunks = []
        async for chunk in self._real.generate_stream(messages, system=system, raw=raw, **kwargs):
            chunks.append(chunk)
            yield chunk
        elapsed = time.perf_counter() - t0
        self.calls.append({
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": sum(len(c) for c in chunks),
            "method": "generate_stream",
        })

    async def generate_with_tools(self, messages, system="", tools=None, **kwargs):
        input_chars = self._estimate_input_chars(messages, system)
        t0 = time.perf_counter()
        result = await self._real.generate_with_tools(messages, system=system, tools=tools, **kwargs)
        elapsed = time.perf_counter() - t0
        out_chars = len(result.get("content", "")) if isinstance(result, dict) else len(str(result))
        self.calls.append({
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": out_chars,
            "method": "generate_with_tools",
        })
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


# ──────────────────────────────────────────────
#  Legacy single-shot generation
# ──────────────────────────────────────────────

async def run_legacy(session: GameSession, action: dict) -> dict:
    """Execute a turn using legacy single-shot generation."""
    ctx = session._prepare_turn(action, legacy_prompt=True)
    system_prompt = ctx["system_prompt"]
    messages = ctx["messages"]

    raw_response = await session.ai_provider.generate(
        messages, system=system_prompt, max_tokens=8192
    )

    parsed = session.response_parser.parse(raw_response)
    result = await session._apply_parsed_response(parsed, raw_response, action, ctx)
    return result


# ──────────────────────────────────────────────
#  8-Stage pipeline (use existing process_action)
# ──────────────────────────────────────────────

async def run_8stage(session: GameSession, action: dict) -> dict:
    """Execute a turn using the 8-stage pipeline."""
    return await session.process_action(action)


# ──────────────────────────────────────────────
#  Report generation
# ──────────────────────────────────────────────

def format_report(label: str, instrumented: InstrumentedProvider, result: dict, wall_time: float) -> dict:
    narrative = result.get("narrative", "")
    choices = result.get("choices", [])
    state_changes = result.get("state_changes", [])
    npc_notifications = result.get("npc_attitude_notifications", [])

    call_details = []
    for i, c in enumerate(instrumented.calls):
        call_details.append({
            "call_no": i + 1,
            "method": c["method"],
            "latency_s": round(c["latency"], 2),
            "input_chars": c["input_chars"],
            "output_chars": c["output_chars"],
        })

    return {
        "mode": label,
        "wall_time_s": round(wall_time, 2),
        "ai_calls": instrumented.total_calls,
        "total_ai_latency_s": round(instrumented.total_latency, 2),
        "overhead_s": round(wall_time - instrumented.total_latency, 2),
        "total_input_chars": instrumented.total_input_chars,
        "total_output_chars": instrumented.total_output_chars,
        "est_input_tokens": instrumented.total_input_chars // 2,
        "est_output_tokens": instrumented.total_output_chars // 2,
        "narrative_length": len(narrative),
        "choices_count": len(choices),
        "state_changes_count": len(state_changes),
        "npc_notifications": len(npc_notifications),
        "has_scene_details": bool(result.get("state", {}).get("scene_details")),
        "has_emotion": bool(result.get("emotion")),
        "call_details": call_details,
    }


def print_report(reports: list[dict]):
    print("\n" + "=" * 70)
    print("  实验报告: Legacy 单次生成 vs 8-Stage Pipeline")
    print("=" * 70)

    for r in reports:
        print(f"\n{'─' * 50}")
        print(f"  模式: {r['mode']}")
        print(f"{'─' * 50}")
        print(f"  总耗时 (wall-clock):     {r['wall_time_s']:.2f}s")
        print(f"  AI 调用次数:             {r['ai_calls']}")
        print(f"  AI 总延迟:               {r['total_ai_latency_s']:.2f}s")
        print(f"  框架开销:                {r['overhead_s']:.2f}s")
        print(f"  输入字符数:              {r['total_input_chars']:,}")
        print(f"  输出字符数:              {r['total_output_chars']:,}")
        print(f"  估算输入 token:          ~{r['est_input_tokens']:,}")
        print(f"  估算输出 token:          ~{r['est_output_tokens']:,}")
        print(f"  叙事长度:                {r['narrative_length']} 字符")
        print(f"  选项数量:                {r['choices_count']}")
        print(f"  状态变更数:              {r['state_changes_count']}")
        print(f"  NPC 态度通知:            {r['npc_notifications']}")
        print(f"  场景细节:                {'有' if r['has_scene_details'] else '无'}")
        print(f"  情感分类:                {'有' if r['has_emotion'] else '无'}")
        print()
        print(f"  各阶段调用明细:")
        for cd in r["call_details"]:
            print(f"    #{cd['call_no']:2d}  {cd['method']:<25s}  {cd['latency_s']:6.2f}s  "
                  f"in:{cd['input_chars']:>6,}  out:{cd['output_chars']:>5,}")

    # Comparison
    if len(reports) == 2:
        leg, stg = reports[0], reports[1]
        print(f"\n{'=' * 50}")
        print("  对比总结")
        print(f"{'=' * 50}")
        speedup = leg["wall_time_s"] / stg["wall_time_s"] if stg["wall_time_s"] else float("inf")
        print(f"  时延比:   Legacy {leg['wall_time_s']:.1f}s  vs  8-Stage {stg['wall_time_s']:.1f}s  "
              f"({'8-Stage 快 %.1fx' % speedup if speedup > 1 else 'Legacy 快 %.1fx' % (1/speedup)})")
        ratio_in = stg["total_input_chars"] / leg["total_input_chars"] if leg["total_input_chars"] else 0
        ratio_out = stg["total_output_chars"] / leg["total_output_chars"] if leg["total_output_chars"] else 0
        print(f"  输入量比: 8-Stage / Legacy = {ratio_in:.2f}x")
        print(f"  输出量比: 8-Stage / Legacy = {ratio_out:.2f}x")
        print(f"  AI调用数: Legacy {leg['ai_calls']}次  vs  8-Stage {stg['ai_calls']}次")
        print(f"  叙事长度: Legacy {leg['narrative_length']}字  vs  8-Stage {stg['narrative_length']}字")
        print(f"  状态推演: Legacy {leg['state_changes_count']}条  vs  8-Stage {stg['state_changes_count']}条")


# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────

async def _get_provider_from_db():
    """Directly read AI profile from SQLite and create provider."""
    import aiosqlite
    from config import DB_PATH
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM ai_profiles WHERE is_active = 1 LIMIT 1"
            )
            row = await cursor.fetchone()
            if not row:
                cursor = await db.execute("SELECT * FROM ai_profiles LIMIT 1")
                row = await cursor.fetchone()
            if not row:
                return None
            profile = dict(row)
    except Exception as e:
        print(f"读取数据库失败: {e}")
        return None

    from api.config_routes import _create_provider_from_profile
    return _create_provider_from_profile(profile)


TEST_ACTIONS = [
    {"type": "free_text", "text": "我走进市集，环顾四周，观察有什么有趣的摊位和人物"},
    {"type": "free_text", "text": "我决定和最近的商人搭话，询问最近城里有什么新鲜事"},
    {"type": "free_text", "text": "我尝试偷偷拿走摊位上的一把匕首"},
]


async def main():
    # Load example script
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "scripts", "example_school.json")
    if not os.path.exists(script_path):
        # Try the other script
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "data", "scripts", "dnd_open_world_ashenvale.json")
    if not os.path.exists(script_path):
        print("找不到示例脚本，请检查 data/scripts/ 目录")
        return

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    print(f"加载脚本: {script.get('script_name', '?')} ({script_path})")
    print(f"测试动作数: {len(TEST_ACTIONS)}")

    # Get AI provider directly from DB (no app context needed)
    real_provider = await _get_provider_from_db()
    if not real_provider:
        print("错误: 未配置 AI 服务。请先在网页端设置 AI profile。")
        return
    print(f"AI Provider: {real_provider.__class__.__name__}")

    all_reports = []

    for action_idx, action in enumerate(TEST_ACTIONS):
        print(f"\n{'#' * 60}")
        print(f"  测试动作 #{action_idx + 1}: {action['text'][:50]}...")
        print(f"{'#' * 60}")

        # --- Legacy ---
        print("\n>>> 运行 Legacy 模式...")
        instr_legacy = InstrumentedProvider(real_provider)
        session_legacy = GameSession(copy.deepcopy(script), instr_legacy)
        await session_legacy.initialize()
        instr_legacy.reset()

        t0 = time.perf_counter()
        try:
            result_legacy = await run_legacy(session_legacy, action)
            wall_legacy = time.perf_counter() - t0
            report_legacy = format_report(f"Legacy (动作#{action_idx+1})", instr_legacy, result_legacy, wall_legacy)
        except Exception as e:
            wall_legacy = time.perf_counter() - t0
            print(f"    Legacy 失败: {e}")
            report_legacy = {"mode": f"Legacy (动作#{action_idx+1})", "error": str(e),
                             "wall_time_s": round(wall_legacy, 2)}

        # --- 8-Stage ---
        print(">>> 运行 8-Stage Pipeline 模式...")
        instr_8stage = InstrumentedProvider(real_provider)
        session_8stage = GameSession(copy.deepcopy(script), instr_8stage)
        await session_8stage.initialize()
        instr_8stage.reset()

        t0 = time.perf_counter()
        try:
            result_8stage = await run_8stage(session_8stage, action)
            wall_8stage = time.perf_counter() - t0
            report_8stage = format_report(f"8-Stage (动作#{action_idx+1})", instr_8stage, result_8stage, wall_8stage)
        except Exception as e:
            wall_8stage = time.perf_counter() - t0
            print(f"    8-Stage 失败: {e}")
            report_8stage = {"mode": f"8-Stage (动作#{action_idx+1})", "error": str(e),
                             "wall_time_s": round(wall_8stage, 2)}

        if "error" not in report_legacy and "error" not in report_8stage:
            print_report([report_legacy, report_8stage])
        else:
            for r in [report_legacy, report_8stage]:
                if "error" in r:
                    print(f"\n  {r['mode']}: 错误 - {r['error']} (耗时 {r['wall_time_s']:.2f}s)")

        all_reports.append({"legacy": report_legacy, "8stage": report_8stage})

    # Save raw data
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benchmark_results.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_reports, f, ensure_ascii=False, indent=2)
    print(f"\n原始数据已保存到: {out_path}")

    # Final aggregate
    print(f"\n{'=' * 70}")
    print("  汇总统计")
    print(f"{'=' * 70}")
    for mode_key, mode_label in [("legacy", "Legacy"), ("8stage", "8-Stage")]:
        valid = [r[mode_key] for r in all_reports if "error" not in r[mode_key]]
        if not valid:
            print(f"  {mode_label}: 全部失败")
            continue
        avg_wall = sum(r["wall_time_s"] for r in valid) / len(valid)
        avg_calls = sum(r["ai_calls"] for r in valid) / len(valid)
        avg_narr = sum(r["narrative_length"] for r in valid) / len(valid)
        avg_sc = sum(r["state_changes_count"] for r in valid) / len(valid)
        total_in = sum(r["total_input_chars"] for r in valid)
        total_out = sum(r["total_output_chars"] for r in valid)
        print(f"  {mode_label}:")
        print(f"    平均耗时:    {avg_wall:.2f}s")
        print(f"    平均调用数:  {avg_calls:.1f}")
        print(f"    平均叙事长度: {avg_narr:.0f} 字符")
        print(f"    平均状态变更: {avg_sc:.1f}")
        print(f"    总输入/输出:  {total_in:,} / {total_out:,} 字符")


if __name__ == "__main__":
    asyncio.run(main())
