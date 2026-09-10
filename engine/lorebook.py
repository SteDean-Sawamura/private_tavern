"""Lorebook / World Info system — keyword-triggered context injection.

Inspired by SillyTavern's World Info system. Entries are only injected into
the AI prompt when their keywords appear in recent chat history or player input.
"""

import random
import re
from dataclasses import dataclass, field


@dataclass
class LorebookEntry:
    id: str
    keys: list[str]
    content: str
    secondary_keys: list[str] = field(default_factory=list)
    enabled: bool = True
    constant: bool = False  # Always injected regardless of keywords
    priority: int = 100  # Higher = injected first
    scan_depth: int = 3  # How many recent messages to scan
    position: str = "after_world"  # Where to inject: before_char, after_world, at_end
    comment: str = ""  # Human-readable label
    entry_type: str = "manual"  # "manual" | "npc_profile" | "npc_relationship" | "event_context"
    related_entries: list[str] = field(default_factory=list)  # IDs of related entries (knowledge graph edges)
    sticky: int = 0  # Remain active for N turns after keyword match (0 = disabled)
    cooldown: int = 0  # After sticky expires, cannot re-activate for N turns (0 = disabled)
    probability: int = 100  # 0-100, chance of activation when keywords match
    group: str = ""  # Mutual exclusion group name; only one entry per group is selected
    group_weight: int = 100  # Weight for weighted random selection within group
    selective_logic: str = "AND_ANY"  # AND_ANY | NOT_ANY | AND_ALL | NOT_ALL
    depth: int = 0  # For position="at_depth": insert at this depth from end of messages
    role: str = "system"  # For position="at_depth": message role (system/user/assistant)
    visibility: str = "public"  # "public" | "world" | "hidden"
    known_by_npcs: list[str] = field(default_factory=list)
    discoverable: dict = field(default_factory=dict)
    last_activated_turn: int = 0


class Lorebook:
    """Scans text for keyword matches and returns activated entries."""

    # P0-9: 短关键字（如 "a"/"我"/"me"）会与几乎任何文本匹配，造成"雪崩激活"。
    # 对纯 ASCII 关键字要求 ≥3 字符，对 CJK 关键字要求 ≥2 字符。
    _CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        """Rough token count: CJK ~1.5 chars/token, ASCII ~4 chars/token."""
        cjk = len(cls._CJK_RE.findall(text))
        non_cjk = len(text) - cjk
        return int(cjk / 1.5 + non_cjk / 4) + 1

    @classmethod
    def _key_too_short(cls, key: str) -> bool:
        k = key.strip()
        if not k:
            return True
        if cls._CJK_RE.search(k):
            return len(k) < 2
        return len(k) < 3

    @staticmethod
    def _check_secondary(sk: list[str], text_lower: str, logic: str) -> bool:
        """Check secondary keywords against text using the given logic mode."""
        if not sk:
            return True
        if logic == "AND_ANY":
            return any(k in text_lower for k in sk)
        if logic == "AND_ALL":
            return all(k in text_lower for k in sk)
        if logic == "NOT_ANY":
            return not any(k in text_lower for k in sk)
        if logic == "NOT_ALL":
            return not all(k in text_lower for k in sk)
        return any(k in text_lower for k in sk)

    def __init__(self, entries: list[dict], max_recursion: int = 2, token_budget: int = 0):
        self.entries = [self._parse_entry(e) for e in entries]
        self.max_recursion = max_recursion
        self.token_budget = token_budget  # 0 = unlimited
        # 预处理：缓存小写化 + 过滤后的关键词列表，避免每次 scan 重复计算
        self._precomputed: dict[str, tuple[list[str], list[str]]] = {}
        for entry in self.entries:
            pk = [k.lower() for k in entry.keys if k.strip() and not self._key_too_short(k)]
            sk = [k.lower() for k in entry.secondary_keys if k.strip() and not self._key_too_short(k)]
            self._precomputed[entry.id] = (pk, sk)

    @staticmethod
    def _parse_entry(data: dict) -> LorebookEntry:
        return LorebookEntry(
            id=data.get("id", ""),
            keys=data.get("keys", []),
            content=data.get("content", ""),
            secondary_keys=data.get("secondary_keys", []),
            enabled=data.get("enabled", True),
            constant=data.get("constant", False),
            priority=data.get("priority", 100),
            scan_depth=data.get("scan_depth", 3),
            position=data.get("position", "after_world"),
            comment=data.get("comment", ""),
            entry_type=data.get("entry_type", "manual"),
            related_entries=data.get("related_entries", []),
            sticky=data.get("sticky", 0),
            cooldown=data.get("cooldown", 0),
            probability=data.get("probability", 100),
            group=data.get("group", ""),
            group_weight=data.get("group_weight", 100),
            selective_logic=data.get("selective_logic", "AND_ANY"),
            depth=data.get("depth", 0),
            role=data.get("role", "system"),
            visibility=data.get("visibility", "public"),
            known_by_npcs=data.get("known_by_npcs", []),
            discoverable=data.get("discoverable", {}),
        )

    def scan(
        self,
        player_action: str,
        recent_messages: list[str],
        max_entries: int = 20,
        timed_state: dict | None = None,
        turn_number: int = 0,
    ) -> tuple[list[LorebookEntry], dict]:
        """Scan player action and recent history for keyword matches.

        Returns (activated_entries, updated_timed_state).
        timed_state tracks sticky/cooldown timers: {"sticky": {id: remaining}, "cooldown": {id: remaining}}
        """
        ts = timed_state or {}
        sticky_timers: dict[str, int] = dict(ts.get("sticky", {}))
        cooldown_timers: dict[str, int] = dict(ts.get("cooldown", {}))
        entry_by_id = {e.id: e for e in self.entries}

        activated: dict[str, LorebookEntry] = {}

        # Phase 0: Activate sticky entries (still within their sticky window)
        for eid, remaining in list(sticky_timers.items()):
            if remaining > 0 and eid in entry_by_id and entry_by_id[eid].enabled:
                activated[eid] = entry_by_id[eid]

        # Phase 1: Collect constant entries
        for entry in self.entries:
            if entry.constant and entry.enabled:
                activated[entry.id] = entry

        # Phase 2: Scan for keyword matches, respecting per-entry scan_depth
        newly_activated = self._scan_text_with_depth(
            player_action, recent_messages, activated
        )

        # Apply cooldown and probability filters to newly matched entries
        filtered_newly = {}
        for eid, entry in newly_activated.items():
            if cooldown_timers.get(eid, 0) > 0:
                continue
            if entry.probability < 100 and random.randint(1, 100) > entry.probability:
                continue
            filtered_newly[eid] = entry
        activated.update(filtered_newly)

        # Phase 3: Activate related entries (knowledge graph edges)
        related_newly = self._activate_related(activated)
        activated.update(related_newly)

        # Phase 4: Recursive scanning (activated content may trigger more entries)
        all_newly = {**filtered_newly, **related_newly}
        for _ in range(self.max_recursion):
            if not all_newly:
                break
            extra_text = "\n".join(e.content for e in all_newly.values())
            all_newly = self._scan_text(extra_text, activated)
            # Apply cooldown/probability to recursive matches too
            filtered_rec = {}
            for eid, entry in all_newly.items():
                if cooldown_timers.get(eid, 0) > 0:
                    continue
                if entry.probability < 100 and random.randint(1, 100) > entry.probability:
                    continue
                filtered_rec[eid] = entry
            activated.update(filtered_rec)
            all_newly = filtered_rec

        # Update timed effects
        new_ts = self._update_timed_effects(
            sticky_timers, cooldown_timers, activated, entry_by_id
        )

        # Mutual exclusion groups: keep only one entry per group (weighted random)
        groups: dict[str, list[LorebookEntry]] = {}
        for entry in activated.values():
            if entry.group:
                groups.setdefault(entry.group, []).append(entry)
        for group_name, members in groups.items():
            if len(members) <= 1:
                continue
            weights = [max(m.group_weight, 1) for m in members]
            chosen = random.choices(members, weights=weights, k=1)[0]
            for m in members:
                if m.id != chosen.id:
                    del activated[m.id]

        # Sort by priority descending, limit count
        result = sorted(activated.values(), key=lambda e: e.priority, reverse=True)
        result = result[:max_entries]

        # Token budget trimming
        if self.token_budget > 0:
            trimmed = []
            used = 0
            for entry in result:
                cost = self._estimate_tokens(entry.content)
                if used + cost > self.token_budget:
                    break
                trimmed.append(entry)
                used += cost
            result = trimmed

        if turn_number > 0:
            for entry in result:
                entry.last_activated_turn = turn_number

        return result, new_ts

    def _scan_text_with_depth(
        self,
        player_action: str,
        recent_messages: list[str],
        already_activated: dict[str, LorebookEntry],
    ) -> dict[str, LorebookEntry]:
        """Scan text against entries, respecting each entry's scan_depth.

        Groups entries by scan_depth to build text_lower only once per depth.
        """
        newly = {}
        action_lower = player_action.lower()

        # 按 depth 分组，避免为每个 entry 重复拼接 text_lower
        depth_groups: dict[int, list[LorebookEntry]] = {}
        for entry in self.entries:
            if entry.id in already_activated or entry.constant or not entry.enabled:
                continue
            depth_groups.setdefault(entry.scan_depth, []).append(entry)

        # 缓存不同 depth 对应的 text_lower
        text_cache: dict[int, str] = {}
        for depth, entries in depth_groups.items():
            if depth not in text_cache:
                if depth <= 0:
                    text_cache[depth] = action_lower
                else:
                    trimmed = recent_messages[-depth:] if depth < len(recent_messages) else recent_messages
                    text_cache[depth] = action_lower + "\n" + "\n".join(trimmed).lower()

            text_lower = text_cache[depth]
            for entry in entries:
                if entry.id in newly:
                    continue
                pk, sk = self._precomputed[entry.id]
                if not pk or not any(k in text_lower for k in pk):
                    continue
                if sk and not self._check_secondary(sk, text_lower, entry.selective_logic):
                    continue
                newly[entry.id] = entry

        return newly

    def _scan_text(
        self, text: str, already_activated: dict[str, LorebookEntry]
    ) -> dict[str, LorebookEntry]:
        """Scan text against all non-activated entries. Returns newly matched.

        Used for recursive scanning where scan_depth is not applicable.
        """
        newly = {}
        text_lower = text.lower()

        for entry in self.entries:
            if entry.id in already_activated or entry.id in newly or entry.constant or not entry.enabled:
                continue
            pk, sk = self._precomputed[entry.id]
            if not pk or not any(k in text_lower for k in pk):
                continue
            if sk and not self._check_secondary(sk, text_lower, entry.selective_logic):
                continue
            newly[entry.id] = entry

        return newly

    def _activate_related(
        self, activated: dict[str, LorebookEntry]
    ) -> dict[str, LorebookEntry]:
        """Activate entries referenced by related_entries of already-activated entries."""
        newly = {}
        entry_by_id = {e.id: e for e in self.entries}
        for entry in activated.values():
            for rel_id in entry.related_entries:
                if rel_id in activated or rel_id in newly:
                    continue
                rel_entry = entry_by_id.get(rel_id)
                if rel_entry and rel_entry.enabled and not rel_entry.constant:
                    newly[rel_id] = rel_entry
        return newly

    def add_entries(self, new_entries: list[dict]):
        """Add entries at runtime (e.g. dynamic lorebook expansion)."""
        for data in new_entries:
            if any(e.id == data.get("id") for e in self.entries):
                continue
            entry = self._parse_entry(data)
            self.entries.append(entry)
            pk = [k.lower() for k in entry.keys if k.strip() and not self._key_too_short(k)]
            sk = [k.lower() for k in entry.secondary_keys if k.strip() and not self._key_too_short(k)]
            self._precomputed[entry.id] = (pk, sk)

    def update_entry(self, entry_id: str, content: str, keys: list[str] | None = None):
        """Update an existing entry's content (and optionally keys) in-place."""
        for entry in self.entries:
            if entry.id == entry_id:
                entry.content = content
                if keys is not None:
                    entry.keys = keys
                    pk = [k.lower() for k in keys if k.strip() and not self._key_too_short(k)]
                    self._precomputed[entry_id] = (pk, self._precomputed.get(entry_id, ([], []))[1])
                return

    def update_entry_enabled(self, entry_id: str, enabled: bool):
        """Enable or disable an entry by ID (e.g. from story tree effects)."""
        for entry in self.entries:
            if entry.id == entry_id:
                entry.enabled = enabled
                return

    def remove_entry(self, entry_id: str):
        """Remove an entry by ID (e.g. invalidated speculative lore)."""
        self.entries = [e for e in self.entries if e.id != entry_id]
        self._precomputed.pop(entry_id, None)

    def activate_by_context(
        self, location_id: str = "", game_time: str = "",
        already_activated: set | None = None,
    ) -> list[LorebookEntry]:
        """按地点/时间上下文激活条目（不依赖关键词匹配）。

        comment 中含 loc:{location_id} 的条目在该地点自动激活；
        comment 中含 time:{tag} 的条目在 game_time 包含该 tag 时激活。
        """
        extra = []
        already = already_activated or set()
        for entry in self.entries:
            if entry.id in already or not entry.enabled:
                continue
            comment = entry.comment or ""
            if location_id and f"loc:{location_id}" in comment:
                extra.append(entry)
                continue
            if game_time and "time:" in comment:
                gt_lower = game_time.lower()
                for part in comment.split():
                    if part.startswith("time:"):
                        tag = part[5:]
                        if tag and tag in gt_lower:
                            extra.append(entry)
                            break
        return extra

    @staticmethod
    def filter_by_visibility(
        entries: list[LorebookEntry],
        scope: str,
        pc_discovered: list[str] | None = None,
        npc_ids: list[str] | None = None,
    ) -> list[LorebookEntry]:
        """按 visibility 过滤条目。

        scope:
          "all"   — 全部返回（Stage 1 骨架用）
          "pc"    — public + world + 已发现的 hidden
          "npc"   — public + world + NPC 在 known_by_npcs 中的 hidden
          "pc_strict" — public + 已发现的 hidden（Stage 5 用，排除 world）
        """
        if scope == "all":
            return list(entries)

        discovered = set(pc_discovered or [])
        npc_set = set(npc_ids or [])
        result = []
        for e in entries:
            vis = e.visibility
            if vis == "public":
                result.append(e)
            elif vis == "world":
                if scope in ("pc", "npc"):
                    result.append(e)
            elif vis == "hidden":
                if scope in ("pc", "pc_strict") and e.id in discovered:
                    result.append(e)
                elif scope == "npc" and npc_set & set(e.known_by_npcs):
                    result.append(e)
        return result

    def format_for_prompt(self, activated: list[LorebookEntry], visibility_markers: bool = False) -> str:
        """Format activated entries into a prompt section, grouped by type."""
        if not activated:
            return ""

        _TYPE_ORDER = {
            "pc_identity": 0, "npc_profile": 1, "npc_relationship": 2,
            "location_desc": 3, "event_context": 4, "story_event": 4,
        }
        _TYPE_HEADERS = {
            "pc_identity": "人物档案",
            "npc_profile": "NPC档案",
            "npc_relationship": "人物关系",
            "location_desc": "地点信息",
            "event_context": "事件背景",
            "story_event": "事件背景",
        }
        _VIS_PREFIX = {
            "world": "[世界设定-仅供推理，不可让角色直接说出] ",
            "hidden_undiscovered": "[机密-未发现：不可让主角知晓此信息] ",
        }
        grouped: dict[str, list[LorebookEntry]] = {}
        for entry in activated:
            key = entry.entry_type if entry.entry_type in _TYPE_ORDER else "_other"
            grouped.setdefault(key, []).append(entry)

        lines = ["## 相关知识 (World Info)"]
        for group_key in sorted(grouped.keys(), key=lambda k: _TYPE_ORDER.get(k, 99)):
            header = _TYPE_HEADERS.get(group_key)
            if header:
                lines.append(f"\n### {header}")
            for entry in grouped[group_key]:
                if not header:
                    lines.append(f"\n### {entry.comment or entry.id}")
                prefix = ""
                if visibility_markers and entry.visibility == "world":
                    prefix = _VIS_PREFIX["world"]
                elif visibility_markers and entry.visibility == "hidden":
                    prefix = _VIS_PREFIX["hidden_undiscovered"]
                lines.append(prefix + entry.content)

        return "\n".join(lines)

    def get_entries_by_position(
        self, activated: list[LorebookEntry], position: str
    ) -> list[LorebookEntry]:
        """Filter activated entries by injection position."""
        return [e for e in activated if e.position == position]

    @staticmethod
    def inject_depth_entries(
        messages: list[dict], depth_entries: list[LorebookEntry],
    ) -> list[dict]:
        """Insert at_depth entries into the messages list at their configured depth."""
        if not depth_entries:
            return messages
        result = list(messages)
        by_depth: dict[int, list[LorebookEntry]] = {}
        for e in depth_entries:
            by_depth.setdefault(e.depth, []).append(e)
        for depth in sorted(by_depth.keys(), reverse=True):
            entries = by_depth[depth]
            content = "\n\n".join(e.content for e in entries)
            role = entries[0].role if entries[0].role in ("system", "user", "assistant") else "system"
            idx = max(0, len(result) - depth)
            result.insert(idx, {"role": role, "content": content})
        return result

    @staticmethod
    def _update_timed_effects(
        sticky_timers: dict[str, int],
        cooldown_timers: dict[str, int],
        activated: dict[str, LorebookEntry],
        entry_by_id: dict[str, LorebookEntry],
    ) -> dict:
        """Tick down timers and set new ones for freshly activated entries."""
        new_sticky = {}
        new_cooldown = {}

        # Process sticky entries: decrement existing, start new
        for eid, entry in entry_by_id.items():
            if entry.sticky <= 0:
                continue
            if eid in activated:
                # Active this turn — reset/set sticky timer
                new_sticky[eid] = entry.sticky
            elif eid in sticky_timers:
                remaining = sticky_timers[eid] - 1
                if remaining > 0:
                    new_sticky[eid] = remaining
                elif entry.cooldown > 0:
                    # Sticky expired → start cooldown
                    new_cooldown[eid] = entry.cooldown

        # Process cooldown entries: decrement existing
        for eid, remaining in cooldown_timers.items():
            if eid in new_cooldown:
                continue  # Already set from sticky expiry
            remaining -= 1
            if remaining > 0:
                new_cooldown[eid] = remaining

        return {"sticky": new_sticky, "cooldown": new_cooldown}
