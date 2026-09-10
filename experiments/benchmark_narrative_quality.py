"""
实验：叙事质量优化前后 A/B 对比
================================
对比维度：
  1. 比喻密度（"像"字比喻计数 / 总字数）
  2. 感官堆叠（单段感官关键词种类数）
  3. Stage2→Stage3 文本复用率（最长公共子串占比）
  4. 叙事截断率（末尾是否为完整句子）
  5. NPC 对话差异度（"压低声音"等模式化表达计数）
  6. AI 套路词命中数
  7. 各阶段 prompt 大小 / 延迟 / 输出长度
  8. 全文输出供人工审阅

Group A: 优化前 prompt（临时覆盖 system prompt 为旧版）
Group B: 优化后 prompt（当前正式管线）

用法：
  cd 酒馆目录
  python -m experiments.benchmark_narrative_quality
"""

import asyncio
import json
import copy
import time
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.game_session import GameSession, strip_think_tags
from engine.prompt_builder import PromptBuilder


# ──────────────────────────────────────────────
#  Stage-Aware Instrumented AI Provider
# ──────────────────────────────────────────────

class StageAwareProvider:
    def __init__(self, real_provider):
        self._real = real_provider
        self.calls: list[dict] = []
        self.current_stage = "unknown"

    def reset(self):
        self.calls = []

    def _input_chars(self, messages, system):
        total = len(system or "")
        for m in messages:
            c = m.get("content", "")
            total += len(c) if isinstance(c, str) else len(str(c))
        return total

    async def generate(self, messages, system="", **kwargs):
        stage = self.current_stage
        input_chars = self._input_chars(messages, system)
        t0 = time.perf_counter()
        result = await self._real.generate(messages, system=system, **kwargs)
        elapsed = time.perf_counter() - t0
        self.calls.append({
            "stage": stage,
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": len(result or ""),
            "output_text": (result or "")[:3000],
            "system_chars": len(system or ""),
            "system_text": (system or "")[:5000],
        })
        return result

    async def generate_stream(self, messages, system="", raw=False, **kwargs):
        stage = self.current_stage
        input_chars = self._input_chars(messages, system)
        t0 = time.perf_counter()
        chunks = []
        async for chunk in self._real.generate_stream(messages, system=system, raw=raw, **kwargs):
            chunks.append(chunk)
            yield chunk
        elapsed = time.perf_counter() - t0
        full = "".join(chunks)
        self.calls.append({
            "stage": stage,
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": len(full),
            "output_text": full[:3000],
            "system_chars": len(system or ""),
            "system_text": "",
        })

    async def generate_with_tools(self, messages, system="", tools=None, **kwargs):
        stage = self.current_stage
        input_chars = self._input_chars(messages, system)
        t0 = time.perf_counter()
        result = await self._real.generate_with_tools(messages, system=system, tools=tools, **kwargs)
        elapsed = time.perf_counter() - t0
        out_chars = len(result.get("content", "")) if isinstance(result, dict) else len(str(result))
        self.calls.append({
            "stage": stage,
            "latency": elapsed,
            "input_chars": input_chars,
            "output_chars": out_chars,
            "output_text": (result.get("content", "") if isinstance(result, dict) else str(result))[:3000],
            "system_chars": len(system or ""),
            "system_text": "",
        })
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


# ──────────────────────────────────────────────
#  旧版 Prompt 常量（优化前）
# ──────────────────────────────────────────────

OLD_STAGE3_RULES = (
    "你是文字游戏的叙事整合师。将素材组合为流畅、有节奏感的叙事段落，"
    "自然承接你上一次的输出继续往下写。\n\n"
    "规则：\n"
    "- 自然编织环境描写和角色行为，不是简单拼接\n"
    "- 去除重复和矛盾\n"
    "- 加入适当的过渡和节奏感\n"
    "- 可以微调措辞但不要改变剧情事实\n"
    "- 内容比例：环境/氛围描写不超过总篇幅的30%，剧情推进和角色互动占70%以上。"
    "不要每个段落都堆叠修辞比喻——信息密度优先于文学修饰\n"
    "- 环境连续性：不可在整合时引入与素材不一致的天气或季节变化\n"
    "- 信息边界检查：确保叙事没有泄露主角不应知道的信息\n"
    "- 玩家自主权：叙事只覆盖玩家行动的直接后果。不可替主角做出新决定、"
    "移动到新地点、或开启玩家未发起的交互。叙事结束时主角应仍在行动发生的场景中\n"
    "- 行动解读：尊重玩家行动中的隐含假设。若玩家对某人使用探查或试探行为，"
    "说明该人物对玩家是未知的，叙事中不可自动将其揭示为熟人\n"
    "- 身份约束：若剧情骨架中的人物以职位泛称出现（未使用具名NPC），"
    "叙事中也必须保持泛称，不可自行将其替换为已知NPC的名字\n"
    "- 时间一致：叙事中对时间流逝的描述必须与剧情骨架中的时间推进一致，不可夸大\n"
    "- 输出800-1200字的完整叙事\n"
    "- 直接输出最终叙事文本，不要任何JSON/标记/解释"
    "\n\n## 写作风格（强制）\n"
    "- 禁止使用以下AI常见套路词汇和句式：\n"
    "  「仿佛」「宛如」「恰如其分」「不禁」「某种说不清的」「在这一刻」\n"
    "  「空气中弥漫着」「目光中闪过一丝」「嘴角微微上扬」「心中涌起一股」\n"
    "  「像是在诉说着什么」「时间仿佛凝固」「无声的默契」「如同…一般」\n"
    "  「微妙的变化」「莫名的感觉」「深邃的目光」「不经意间」\n"
    "  「悄然」「油然而生」「交织」「弥漫」「萦绕」「流淌」\n"
    "- 少用排比、对仗、感叹号\n"
    "- 多用短句，少用长从句。叙事节奏要有松有紧\n"
    "- 对话要口语化，每个NPC说话风格不同\n"
    "- 感官描写具体而克制，一两处点到为止，不要每段都堆叠\n"
    "- 关注动作和对话推进剧情，而非内心独白和环境铺陈\n"
    "- 形容词和副词用完即弃，不要反复使用同一个修饰语"
)

OLD_STAGE2A_SENSORY_LINE = "- 覆盖至少2-3种感官（视觉、听觉、嗅觉、触觉、温度）\n"

OLD_STAGE2B_VOICE_LINE = "- 严禁两个NPC用相同的说话方式、句长、语气词\n"


# ──────────────────────────────────────────────
#  叙事质量自动化指标
# ──────────────────────────────────────────────

SIMILE_PATTERN = re.compile(r'像[^，。！？\n]{2,20}(?:一样|似的|般|那样)?')
AI_BLOCKLIST = [
    "仿佛", "宛如", "恰如其分", "不禁", "某种说不清的", "在这一刻",
    "空气中弥漫着", "目光中闪过一丝", "嘴角微微上扬", "心中涌起一股",
    "像是在诉说着什么", "时间仿佛凝固", "无声的默契",
    "微妙的变化", "莫名的感觉", "深邃的目光", "不经意间",
    "悄然", "油然而生", "交织", "弥漫", "萦绕", "流淌",
]
SENSORY_KEYWORDS = {
    "视觉": ["光", "影", "色", "亮", "暗", "闪", "映", "透", "斑驳"],
    "听觉": ["声", "响", "叮", "嗡", "咔", "哒", "沙沙", "嘶", "吼", "嗡鸣"],
    "嗅觉": ["味", "气", "闻", "臭", "香", "腥", "霉", "焦"],
    "触觉": ["凉", "热", "粗糙", "湿", "滑", "硬", "软", "冰", "烫", "麻"],
    "温度": ["冷", "暖", "寒", "炎", "暑", "凉意"],
}
NPC_CLICHE_PATTERNS = ["压低声音", "压低了声音", "意味深长", "若有所思", "欲言又止"]
TRUNCATION_ENDINGS = re.compile(r'[，、—…\s]$')


def analyze_narrative(text: str) -> dict:
    if not text:
        return {}

    similes = SIMILE_PATTERN.findall(text)
    simile_count = len(similes)

    blocklist_hits = []
    for word in AI_BLOCKLIST:
        count = text.count(word)
        if count > 0:
            blocklist_hits.append({"word": word, "count": count})

    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    max_sensory_per_para = 0
    sensory_details = []
    for para in paragraphs:
        senses_in_para = set()
        for sense_name, keywords in SENSORY_KEYWORDS.items():
            for kw in keywords:
                if kw in para:
                    senses_in_para.add(sense_name)
                    break
        count = len(senses_in_para)
        if count > max_sensory_per_para:
            max_sensory_per_para = count
        sensory_details.append({"senses": list(senses_in_para), "count": count})

    is_truncated = bool(TRUNCATION_ENDINGS.search(text.rstrip()))

    cliche_count = sum(text.count(p) for p in NPC_CLICHE_PATTERNS)

    return {
        "length": len(text),
        "paragraphs": len(paragraphs),
        "simile_count": simile_count,
        "simile_density": round(simile_count / max(len(text) / 200, 1), 2),
        "simile_examples": similes[:5],
        "blocklist_hits": blocklist_hits,
        "blocklist_total": sum(h["count"] for h in blocklist_hits),
        "max_sensory_per_paragraph": max_sensory_per_para,
        "sensory_by_paragraph": sensory_details,
        "is_truncated": is_truncated,
        "last_10_chars": text[-10:] if text else "",
        "npc_cliche_count": cliche_count,
    }


def compute_text_reuse(stage2_text: str, stage3_text: str) -> dict:
    """Compute text reuse ratio between Stage 2 output and Stage 3 output.

    Uses coverage marking on s3 to avoid counting the same s3 region twice.
    """
    if not stage2_text or not stage3_text:
        return {"reuse_ratio": 0, "longest_match": 0, "matched_fragments": []}

    min_len = 8
    s2 = stage2_text
    s3 = stage3_text

    # Find all maximal non-overlapping fragments from s2 that appear in s3
    fragments = []
    for start in range(len(s2)):
        for end in range(start + min_len, min(start + 80, len(s2)) + 1):
            substr = s2[start:end]
            if substr in s3:
                if not any(substr in f for f in fragments):
                    fragments = [f for f in fragments if f not in substr]
                    fragments.append(substr)

    fragments.sort(key=len, reverse=True)

    # Mark coverage on s3 to avoid double counting
    covered = [False] * len(s3)
    for frag in fragments:
        pos = 0
        while True:
            idx = s3.find(frag, pos)
            if idx == -1:
                break
            for i in range(idx, idx + len(frag)):
                covered[i] = True
            pos = idx + 1

    total_reused = sum(covered)
    reuse_ratio = total_reused / len(s3) if s3 else 0

    return {
        "reuse_ratio": round(reuse_ratio, 3),
        "reused_chars": total_reused,
        "longest_match": len(fragments[0]) if fragments else 0,
        "matched_fragments": [f[:60] for f in fragments[:5]],
    }


# ──────────────────────────────────────────────
#  Stage 标注
# ──────────────────────────────────────────────

def _guess_stage(call: dict) -> str:
    out = call.get("output_text", "")
    sys_text = call.get("system_text", "")
    sys_chars = call.get("system_chars", 0)
    out_len = call.get("output_chars", 0)

    if out_len == 0:
        return "Stage_empty" if sys_chars > 100 else "Misc"

    # Strip markdown fenced JSON
    stripped = out.lstrip()
    if stripped.startswith("```"):
        stripped = stripped.lstrip("`").lstrip()
        if stripped.startswith("json"):
            stripped = stripped[4:].lstrip()
        stripped = stripped.rstrip("`").rstrip()

    is_json = stripped.startswith("{") or stripped.startswith("[")

    if is_json:
        if '"npc_attitude_changes"' in stripped or '"npc_met_changes"' in stripped:
            return "Stage4a_npc"
        if '"state_changes"' in stripped or '"time_advance"' in stripped:
            return "Stage4b_world"
        if '"activate_states"' in stripped or '"location_change"' in stripped:
            return "Stage4b_world"
        if '"add_consequences"' in stripped or '"offscreen_npc_updates"' in stripped:
            return "Stage4b_ext"
        if '"narrative_thread_updates"' in stripped or '"discovered_clues"' in stripped:
            return "Stage4b_ext"
        if '"faction_reputation_changes"' in stripped or '"moral_alignment_changes"' in stripped:
            return "Stage4b_ext"
        if '"choices"' in stripped:
            return "Stage5_choices"
        if '"scene_type"' in stripped and '"systems"' in stripped:
            return "Route"
        if '"scene_details"' in stripped:
            return "Stage4b_world"
        if stripped.strip() in ("{}", "[]"):
            return "Stage4a_npc"
        return "JSON_unknown"

    if "[行动结果]" in out or "[关键事件]" in out or "剧情骨架" in out or "行动结果" in out:
        return "Stage1_plot"

    if "叙事整合" in sys_text or "叙事润色" in sys_text:
        return "Stage3_narrative"
    if "环境描写师" in sys_text:
        return "Stage2a_env"
    if "角色行为" in sys_text or "角色行为编导" in sys_text:
        return "Stage2b_char"
    if "环境描写" in sys_text and "角色行为" not in sys_text:
        return "Stage2a_env"
    if "场景编导" in sys_text:
        return "Stage2_merged"
    if "场景分析器" in sys_text:
        return "Route"

    if out_len > 600:
        return "Stage3_narrative"
    if out_len > 100:
        return "Stage2_unknown"

    return "Unknown"


def label_calls(calls: list[dict]):
    for c in calls:
        if c["stage"] == "unknown":
            c["stage"] = _guess_stage(c)


# ──────────────────────────────────────────────
#  Report
# ──────────────────────────────────────────────

def build_report(label: str, calls: list[dict], result: dict, wall_time: float) -> dict:
    narrative = result.get("narrative", "")
    choices = result.get("choices", [])
    state_changes = result.get("state_changes", [])

    stage_summary = {}
    for c in calls:
        s = c["stage"]
        if s not in stage_summary:
            stage_summary[s] = {"latency": 0, "input_chars": 0, "output_chars": 0,
                                "system_chars": 0, "calls": 0, "outputs": []}
        stage_summary[s]["latency"] += c["latency"]
        stage_summary[s]["input_chars"] += c["input_chars"]
        stage_summary[s]["output_chars"] += c["output_chars"]
        stage_summary[s]["system_chars"] += c.get("system_chars", 0)
        stage_summary[s]["calls"] += 1
        stage_summary[s]["outputs"].append(c.get("output_text", "")[:2000])

    # Extract stage 2 and stage 3 text for reuse analysis
    s2_text = ""
    s3_text = ""
    for sname in ["Stage2a_env", "Stage2b_char", "Stage2_merged"]:
        if sname in stage_summary:
            s2_text += " ".join(stage_summary[sname]["outputs"])
    if "Stage3_narrative" in stage_summary:
        s3_text = " ".join(stage_summary["Stage3_narrative"]["outputs"])

    narrative_analysis = analyze_narrative(narrative)
    text_reuse = compute_text_reuse(s2_text, s3_text)

    return {
        "mode": label,
        "wall_time_s": round(wall_time, 2),
        "ai_calls": len(calls),
        "total_input_chars": sum(c["input_chars"] for c in calls),
        "total_output_chars": sum(c["output_chars"] for c in calls),
        "narrative_length": len(narrative),
        "narrative_text": narrative,
        "choices_count": len(choices),
        "state_changes_count": len(state_changes),
        "route": result.get("_route"),
        "stage_summary": stage_summary,
        "narrative_analysis": narrative_analysis,
        "text_reuse": text_reuse,
    }


def generate_md_report(all_results: list[dict], output_path: str):
    lines = ["# 叙事质量优化 A/B 实验报告", ""]
    lines.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")
    lines.append("Group A = 优化前 prompt（旧版 Stage 3 rules，无比喻频率约束，无感官密度约束，无节奏指引）")
    lines.append("")
    lines.append("Group B = 优化后 prompt（禁复读素材，600-900字限，比喻≤3，感官≤2/段，场景节奏指引，NPC差异化）")
    lines.append("")

    for i, entry in enumerate(all_results):
        lines.append(f"## 场景 {i+1}: {entry.get('label', '')} — {entry.get('action', '')[:50]}")
        lines.append("")

        for turn_data in entry.get("turns", []):
            turn_num = turn_data.get("turn", 1)
            lines.append(f"### 回合 {turn_num}")
            lines.append("")

            for tag, r in [("Group A (优化前)", turn_data.get("group_a", {})),
                           ("Group B (优化后)", turn_data.get("group_b", {}))]:
                if not r or "error" in r:
                    lines.append(f"#### {tag}")
                    lines.append(f"**错误**: {r.get('error', '无数据')}")
                    lines.append("")
                    continue

                lines.append(f"#### {tag}")
                if r.get("route"):
                    rt = r["route"]
                    lines.append(f"**Route**: type={rt.get('scene_type','?')}, scope={rt.get('scope','?')}, systems={rt.get('systems',[])}")
                lines.append("")

                # 基本指标
                lines.append("| 指标 | 值 |")
                lines.append("|------|------|")
                lines.append(f"| 总耗时 | {r.get('wall_time_s', 0):.2f}s |")
                lines.append(f"| AI 调用次数 | {r.get('ai_calls', 0)} |")
                lines.append(f"| 叙事长度 | {r.get('narrative_length', 0)} |")
                lines.append("")

                # 叙事质量指标
                na = r.get("narrative_analysis", {})
                tr = r.get("text_reuse", {})
                lines.append("##### 叙事质量指标")
                lines.append("| 指标 | 值 |")
                lines.append("|------|------|")
                lines.append(f"| 比喻数量 | {na.get('simile_count', 0)} |")
                lines.append(f"| 比喻密度 (每200字) | {na.get('simile_density', 0)} |")
                lines.append(f"| AI套路词命中 | {na.get('blocklist_total', 0)} |")
                lines.append(f"| 最大感官/段 | {na.get('max_sensory_per_paragraph', 0)} |")
                lines.append(f"| NPC套话计数 | {na.get('npc_cliche_count', 0)} |")
                lines.append(f"| 是否截断 | {'是' if na.get('is_truncated') else '否'} |")
                lines.append(f"| Stage2→3复用率 | {tr.get('reuse_ratio', 0):.1%} |")
                lines.append(f"| 最长复用片段 | {tr.get('longest_match', 0)}字 |")
                lines.append("")

                if na.get("simile_examples"):
                    lines.append(f"比喻示例: {', '.join(na['simile_examples'][:3])}")
                    lines.append("")
                if na.get("blocklist_hits"):
                    hits_str = ", ".join(f"{h['word']}×{h['count']}" for h in na["blocklist_hits"][:5])
                    lines.append(f"命中套路词: {hits_str}")
                    lines.append("")
                if tr.get("matched_fragments"):
                    lines.append(f"复用片段: 「{'」「'.join(tr['matched_fragments'][:3])}」")
                    lines.append("")

                # 各阶段明细
                lines.append("##### 各阶段明细")
                lines.append("| Stage | 延迟 | System | Input | Output |")
                lines.append("|-------|------|--------|-------|--------|")
                for sname, sd in r.get("stage_summary", {}).items():
                    lines.append(f"| {sname} | {sd['latency']:.2f}s | {sd['system_chars']:,} | {sd['input_chars']:,} | {sd['output_chars']:,} |")
                lines.append("")

                # 叙事全文
                nt = r.get("narrative_text", "")
                if nt:
                    lines.append(f"<details><summary>叙事全文 ({len(nt)}字)</summary>")
                    lines.append("")
                    lines.append("```")
                    lines.append(nt[:1500])
                    lines.append("```")
                    lines.append("</details>")
                    lines.append("")

                # Stage 2 / Stage 3 输出
                for sname in ["Stage2a_env", "Stage2b_char", "Stage2_merged", "Stage3_narrative"]:
                    sd = r.get("stage_summary", {}).get(sname)
                    if sd and sd["outputs"] and sd["output_chars"] > 0:
                        out = sd["outputs"][0][:800].strip()
                        lines.append(f"<details><summary>{sname} 输出 ({sd['output_chars']}字)</summary>")
                        lines.append("")
                        lines.append("```")
                        lines.append(out)
                        lines.append("```")
                        lines.append("</details>")
                        lines.append("")

            # 对比
            ra = turn_data.get("group_a", {})
            rb = turn_data.get("group_b", {})
            if ra and rb and "error" not in ra and "error" not in rb:
                na_a = ra.get("narrative_analysis", {})
                na_b = rb.get("narrative_analysis", {})
                tr_a = ra.get("text_reuse", {})
                tr_b = rb.get("text_reuse", {})
                lines.append("##### 对比")
                lines.append("| 指标 | 优化前 | 优化后 | 变化 |")
                lines.append("|------|--------|--------|------|")
                lines.append(f"| 叙事长度 | {ra.get('narrative_length',0)} | {rb.get('narrative_length',0)} | {rb.get('narrative_length',0) - ra.get('narrative_length',0):+d} |")
                lines.append(f"| 比喻数量 | {na_a.get('simile_count',0)} | {na_b.get('simile_count',0)} | {na_b.get('simile_count',0) - na_a.get('simile_count',0):+d} |")
                lines.append(f"| AI套路词 | {na_a.get('blocklist_total',0)} | {na_b.get('blocklist_total',0)} | {na_b.get('blocklist_total',0) - na_a.get('blocklist_total',0):+d} |")
                lines.append(f"| 最大感官/段 | {na_a.get('max_sensory_per_paragraph',0)} | {na_b.get('max_sensory_per_paragraph',0)} | {na_b.get('max_sensory_per_paragraph',0) - na_a.get('max_sensory_per_paragraph',0):+d} |")
                lines.append(f"| NPC套话 | {na_a.get('npc_cliche_count',0)} | {na_b.get('npc_cliche_count',0)} | {na_b.get('npc_cliche_count',0) - na_a.get('npc_cliche_count',0):+d} |")
                lines.append(f"| 截断 | {'是' if na_a.get('is_truncated') else '否'} | {'是' if na_b.get('is_truncated') else '否'} | |")
                lines.append(f"| S2→S3复用率 | {tr_a.get('reuse_ratio',0):.1%} | {tr_b.get('reuse_ratio',0):.1%} | {tr_b.get('reuse_ratio',0) - tr_a.get('reuse_ratio',0):+.1%} |")
                lines.append("")

        lines.append("---")
        lines.append("")

    # 汇总
    lines.append("## 汇总")
    lines.append("")
    for gkey, glabel in [("group_a", "优化前"), ("group_b", "优化后")]:
        all_na = []
        all_tr = []
        all_len = []
        for r in all_results:
            for t in r.get("turns", []):
                rd = t.get(gkey, {})
                if "error" not in rd:
                    all_na.append(rd.get("narrative_analysis", {}))
                    all_tr.append(rd.get("text_reuse", {}))
                    all_len.append(rd.get("narrative_length", 0))
        if not all_na:
            continue
        n = len(all_na)
        lines.append(f"### {glabel} ({n} 轮)")
        lines.append("| 指标 | 平均值 |")
        lines.append("|------|--------|")
        lines.append(f"| 叙事长度 | {sum(all_len)/n:.0f} |")
        lines.append(f"| 比喻数量 | {sum(a.get('simile_count',0) for a in all_na)/n:.1f} |")
        lines.append(f"| 比喻密度 | {sum(a.get('simile_density',0) for a in all_na)/n:.2f} |")
        lines.append(f"| AI套路词 | {sum(a.get('blocklist_total',0) for a in all_na)/n:.1f} |")
        lines.append(f"| 最大感官/段 | {sum(a.get('max_sensory_per_paragraph',0) for a in all_na)/n:.1f} |")
        lines.append(f"| NPC套话 | {sum(a.get('npc_cliche_count',0) for a in all_na)/n:.1f} |")
        lines.append(f"| 截断率 | {sum(1 for a in all_na if a.get('is_truncated'))/n:.0%} |")
        lines.append(f"| S2→S3复用率 | {sum(t.get('reuse_ratio',0) for t in all_tr)/n:.1%} |")
        lines.append("")

    lines.append("*实验由 benchmark_narrative_quality.py 自动生成*")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ──────────────────────────────────────────────
#  Monkey-patch: 临时回退 prompt 到优化前
# ──────────────────────────────────────────────

def patch_old_prompts(prompt_builder: PromptBuilder):
    """Monkey-patch the prompt builder to use pre-optimization prompts."""
    original_compose = prompt_builder.build_narrative_compose_prompt

    def patched_compose(plot_decision, env_text, char_text, state,
                        recent_openings=None, missing_env=False, missing_char=False,
                        history_context="", prev_narrative_tail="", scene_type=""):
        msgs, _ = original_compose(
            plot_decision, env_text, char_text, state,
            recent_openings=recent_openings, missing_env=missing_env,
            missing_char=missing_char, history_context=history_context,
            prev_narrative_tail=prev_narrative_tail, scene_type="",
        )
        return msgs, OLD_STAGE3_RULES

    prompt_builder.build_narrative_compose_prompt = patched_compose

    original_env = prompt_builder.build_env_render_prompt

    def patched_env(plot_decision, state):
        msgs, sys = original_env(plot_decision, state)
        sys = sys.replace(
            "覆盖2-3种感官（视觉、听觉、嗅觉、触觉、温度），每段不超过2种，"
            "分散在不同段落中，不要在同一段内堆叠所有感官",
            "覆盖至少2-3种感官（视觉、听觉、嗅觉、触觉、温度）"
        )
        return msgs, sys

    prompt_builder.build_env_render_prompt = patched_env

    original_char = prompt_builder.build_character_action_prompt

    def patched_char(plot_decision, state, present_npc_ids=None,
                     dice_results=None, check_result=None,
                     triggered_events=None, triggered_consequences=None):
        msgs, sys = original_char(
            plot_decision, state, present_npc_ids=present_npc_ids,
            dice_results=dice_results, check_result=check_result,
            triggered_events=triggered_events,
            triggered_consequences=triggered_consequences,
        )
        sys = sys.replace(
            "- 严禁两个NPC用相同的说话方式、句长、语气词\n"
            "- 对话差异化：粗人用断句和口头禅，学者用书面长句，商人讲利弊得失。"
            "不要让每个NPC都'压低声音'或'意味深长地看着你'——"
            "用不同的肢体语言和说话节奏区分角色\n",
            "- 严禁两个NPC用相同的说话方式、句长、语气词\n"
        )
        return msgs, sys

    prompt_builder.build_character_action_prompt = patched_char


# ──────────────────────────────────────────────
#  Main
# ──────────────────────────────────────────────

SCENARIO_ACTIONS = [
    {
        "label": "社交/打听",
        "turns": [
            {"type": "free_text", "text": "我走向酒馆的吧台，向酒保打听最近城里有什么传闻"},
        ],
    },
    {
        "label": "探索/调查",
        "turns": [
            {"type": "free_text", "text": "我小心翼翼地推开地下室的门，举起火把探查里面的情况"},
        ],
    },
    {
        "label": "战斗/攻击",
        "turns": [
            {"type": "free_text", "text": "我拔出武器，向面前的盗贼发起攻击"},
        ],
    },
]


async def _get_provider_from_db():
    import aiosqlite
    from config import DB_PATH
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM ai_profiles WHERE is_active = 1 LIMIT 1")
            row = await cursor.fetchone()
            if not row:
                cursor = await db.execute("SELECT * FROM ai_profiles LIMIT 1")
                row = await cursor.fetchone()
            if not row:
                return None, None
            profile = dict(row)
    except Exception as e:
        print(f"读取数据库失败: {e}")
        return None, None

    from api.config_routes import _create_provider_from_profile
    stage_models = json.loads(profile.get("stage_models") or "{}") if profile.get("stage_models") else {}
    return _create_provider_from_profile(profile), stage_models


async def _fast_init(session: GameSession, real_prov):
    """Initialize without AI personalization to save ~60s per session."""
    saved = session.ai_provider
    session.ai_provider = None
    await session.initialize()
    session.ai_provider = saved


async def run_scenario(scenario: dict, real_provider, stage_models: dict, script: dict):
    label = scenario["label"]
    turns = scenario["turns"]
    turn_results = []

    # Group A: 优化前 prompt
    prov_a = StageAwareProvider(real_provider)
    sess_a = GameSession(copy.deepcopy(script), prov_a, stage_models=stage_models)
    await _fast_init(sess_a, real_provider)
    patch_old_prompts(sess_a.prompt_builder)

    # Group B: 优化后 prompt（当前正式版）
    prov_b = StageAwareProvider(real_provider)
    sess_b = GameSession(copy.deepcopy(script), prov_b, stage_models=stage_models)
    await _fast_init(sess_b, real_provider)

    for turn_idx, action in enumerate(turns):
        turn_num = turn_idx + 1
        print(f"\n  --- 回合 {turn_num}/{len(turns)}: {action['text'][:35]}... ---")

        # Group A
        print(f"    [A] 优化前 prompt...")
        prov_a.reset()
        t0 = time.perf_counter()
        try:
            result_a = await sess_a.process_action(action)
            wall_a = time.perf_counter() - t0
            label_calls(prov_a.calls)
            report_a = build_report(f"Pre-opt T{turn_num}", prov_a.calls, result_a, wall_a)
            na = report_a["narrative_analysis"]
            print(f"    [A] OK: {wall_a:.1f}s, narrative={report_a['narrative_length']}ch, "
                  f"similes={na.get('simile_count',0)}, blocklist={na.get('blocklist_total',0)}, "
                  f"maxSensory={na.get('max_sensory_per_paragraph',0)}, "
                  f"reuse={report_a['text_reuse'].get('reuse_ratio',0):.1%}, "
                  f"trunc={'Y' if na.get('is_truncated') else 'N'}")
        except Exception as e:
            wall_a = time.perf_counter() - t0
            print(f"    [A] FAIL: {e}")
            import traceback; traceback.print_exc()
            report_a = {"mode": f"Pre-opt T{turn_num}", "error": str(e), "wall_time_s": round(wall_a, 2)}

        # Group B
        print(f"    [B] 优化后 prompt...")
        prov_b.reset()
        t0 = time.perf_counter()
        try:
            result_b = await sess_b.process_action(action)
            wall_b = time.perf_counter() - t0
            label_calls(prov_b.calls)
            report_b = build_report(f"Post-opt T{turn_num}", prov_b.calls, result_b, wall_b)
            na = report_b["narrative_analysis"]
            print(f"    [B] OK: {wall_b:.1f}s, narrative={report_b['narrative_length']}ch, "
                  f"similes={na.get('simile_count',0)}, blocklist={na.get('blocklist_total',0)}, "
                  f"maxSensory={na.get('max_sensory_per_paragraph',0)}, "
                  f"reuse={report_b['text_reuse'].get('reuse_ratio',0):.1%}, "
                  f"trunc={'Y' if na.get('is_truncated') else 'N'}")
        except Exception as e:
            wall_b = time.perf_counter() - t0
            print(f"    [B] FAIL: {e}")
            import traceback; traceback.print_exc()
            report_b = {"mode": f"Post-opt T{turn_num}", "error": str(e), "wall_time_s": round(wall_b, 2)}

        turn_results.append({
            "turn": turn_num,
            "action": action["text"],
            "group_a": report_a,
            "group_b": report_b,
        })

    return turn_results


async def main():
    script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "scripts", "dnd_open_world_ashenvale.json")
    if not os.path.exists(script_path):
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                   "data", "scripts", "example_school.json")
    if not os.path.exists(script_path):
        print("找不到示例脚本")
        return

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    print(f"加载脚本: {script.get('script_name', '?')}")
    print(f"地点: {len(script.get('locations', []))}  NPC: {len(script.get('npcs', []))}")

    real_provider, stage_models = await _get_provider_from_db()
    if not real_provider:
        print("错误: 未配置 AI 服务。请先在网页端设置 AI profile。")
        return
    print(f"AI Provider: {real_provider.__class__.__name__}")
    print(f"\n{'=' * 70}")
    print(f"  叙事质量 A/B 对比：优化前 vs 优化后")
    print(f"  每个场景 1 轮，共 3 场景 3 轮")
    print(f"{'=' * 70}")

    all_results = []

    for idx, scenario in enumerate(SCENARIO_ACTIONS):
        print(f"\n{'#' * 70}")
        print(f"  场景 {idx+1}/{len(SCENARIO_ACTIONS)}: {scenario['label']}")
        print(f"{'#' * 70}")

        turn_results = await run_scenario(scenario, real_provider, stage_models, script)

        all_results.append({
            "label": scenario["label"],
            "action": scenario["turns"][0]["text"],
            "turns": turn_results,
        })

    # 汇总
    print(f"\n{'=' * 70}")
    print("  汇总")
    print(f"{'=' * 70}")
    for gkey, glabel in [("group_a", "A (优化前)"), ("group_b", "B (优化后)")]:
        all_valid = []
        for r in all_results:
            for t in r.get("turns", []):
                rd = t.get(gkey, {})
                if "error" not in rd:
                    all_valid.append(rd)
        if not all_valid:
            continue
        n = len(all_valid)
        avg_na = {
            "simile": sum(r.get("narrative_analysis", {}).get("simile_count", 0) for r in all_valid) / n,
            "blocklist": sum(r.get("narrative_analysis", {}).get("blocklist_total", 0) for r in all_valid) / n,
            "sensory": sum(r.get("narrative_analysis", {}).get("max_sensory_per_paragraph", 0) for r in all_valid) / n,
            "cliche": sum(r.get("narrative_analysis", {}).get("npc_cliche_count", 0) for r in all_valid) / n,
            "reuse": sum(r.get("text_reuse", {}).get("reuse_ratio", 0) for r in all_valid) / n,
            "trunc": sum(1 for r in all_valid if r.get("narrative_analysis", {}).get("is_truncated")) / n,
            "length": sum(r.get("narrative_length", 0) for r in all_valid) / n,
        }
        print(f"  {glabel} ({n} turns):")
        print(f"    平均叙事长度:     {avg_na['length']:.0f}")
        print(f"    平均比喻数:       {avg_na['simile']:.1f}")
        print(f"    平均套路词:       {avg_na['blocklist']:.1f}")
        print(f"    平均最大感官/段:  {avg_na['sensory']:.1f}")
        print(f"    平均NPC套话:      {avg_na['cliche']:.1f}")
        print(f"    平均S2→S3复用率:  {avg_na['reuse']:.1%}")
        print(f"    截断率:           {avg_na['trunc']:.0%}")

    # Save
    exp_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(exp_dir, "narrative_quality_results.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n原始数据: {json_path}")

    md_path = os.path.join(exp_dir, "narrative_quality_report.md")
    generate_md_report(all_results, md_path)
    print(f"报告: {md_path}")


if __name__ == "__main__":
    asyncio.run(main())
