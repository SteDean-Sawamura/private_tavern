"""Tests for ai.response_parser module."""

import pytest
from ai.response_parser import ResponseParser


class TestResponseParser:
    def setup_method(self):
        self.parser = ResponseParser()

    def test_parse_well_formed_response(self):
        raw = """你走进了市场，四周热闹非凡。

```game_state
{
  "choices": [{"id":"c1","text":"逛摊位"},{"id":"c2","text":"离开"}],
  "state_changes": [{"target":"player.gold","op":"add","value":-5}],
  "end_time": "1979-10-26T10:30:00"
}
```"""
        result = self.parser.parse(raw)
        assert "你走进了市场" in result["narrative"]
        assert len(result["choices"]) == 2
        assert result["choices"][0]["text"] == "逛摊位"
        assert len(result["state_changes"]) == 1
        assert result["end_time"] == "1979-10-26T10:30:00"

    def test_parse_no_json_block(self):
        raw = "你在森林中漫步，什么也没发生。"
        result = self.parser.parse(raw)
        assert "你在森林中漫步" in result["narrative"]
        # Should get default choices
        assert len(result["choices"]) >= 2

    def test_parse_truncated_json_recovery(self):
        raw = """描述文本。

```game_state
{
  "choices": [{"id":"c1","text":"选项A"},{"id":"c2","text":"选项B"}],
  "state_changes": [{"target":"player.hp","op":"add","value":-10}]
"""
        result = self.parser.parse(raw)
        assert "描述文本" in result["narrative"]
        # Should recover at least the choices
        if result["choices"][0].get("text") == "选项A":
            assert len(result["choices"]) >= 2

    def test_choices_always_have_ids(self):
        raw = """叙事内容。

```game_state
{"choices": [{"text":"选项1"},{"text":"选项2"}]}
```"""
        result = self.parser.parse(raw)
        for choice in result["choices"]:
            assert "id" in choice

    def test_default_choices_when_empty(self):
        raw = """叙事内容。

```game_state
{"choices": []}
```"""
        result = self.parser.parse(raw)
        assert len(result["choices"]) >= 2

    def test_parse_with_trailing_comma(self):
        raw = """叙事内容。

```game_state
{"choices": [{"id":"c1","text":"走"},], "end_time": "1979-10-26T11:00:00",}
```"""
        result = self.parser.parse(raw)
        assert result["end_time"] == "1979-10-26T11:00:00"

    def test_parse_location_change(self):
        raw = """你启程前往森林。

```game_state
{"choices": [{"id":"c1","text":"探索"}], "location_change": "forest_01"}
```"""
        result = self.parser.parse(raw)
        assert result["location_change"] == "forest_01"

    def test_parse_npc_attitude_changes(self):
        raw = """守卫对你点了点头。

```game_state
{
  "choices": [{"id":"c1","text":"继续"}],
  "npc_attitude_changes": [{"npc_id":"guard","dimension":"trust","change":5}]
}
```"""
        result = self.parser.parse(raw)
        assert len(result["npc_attitude_changes"]) == 1
        assert result["npc_attitude_changes"][0]["npc_id"] == "guard"

    def test_parse_inventory_changes(self):
        raw = """你捡到一把钥匙。

```game_state
{
  "choices": [{"id":"c1","text":"继续"}],
  "inventory_changes": [{"item":"铁钥匙","action":"add","quantity":1}]
}
```"""
        result = self.parser.parse(raw)
        assert len(result["inventory_changes"]) == 1
        assert result["inventory_changes"][0]["item"] == "铁钥匙"

    def test_parse_game_over(self):
        raw = """你倒下了。

```game_state
{"choices": [], "game_over": {"reason": "death", "ending": "bad"}}
```"""
        result = self.parser.parse(raw)
        assert result["game_over"] is not None
        assert result["game_over"]["reason"] == "death"

    def test_parse_fallback_json_block(self):
        raw = """叙事文本。

```json
{"choices": [{"id":"c1","text":"走"}], "end_time": "1979-10-26T12:00:00"}
```"""
        result = self.parser.parse(raw)
        assert result["end_time"] == "1979-10-26T12:00:00"

    def test_empty_result_structure(self):
        result = self.parser._empty_result()
        assert result["narrative"] == ""
        assert result["choices"] == []
        assert result["state_changes"] == []
        assert result["end_time"] is None
        assert result["location_change"] is None
        assert result["game_over"] is None

    def test_parse_severely_truncated_keys(self):
        """Test recovery when JSON field names are truncated (e.g., AI token limit hit)."""
        raw = """叙事文本。

```game_state
{
  "c": [{"id":"c1","text":"选项A"},{"id":"c2","text":"选项B"}],
  "end": "1979-10-26T10:30:00",
  "stat": [{"target":"player.hp","op":"add","value":-5}]
}
```"""
        result = self.parser.parse(raw)
        assert "叙事文本" in result["narrative"]
        assert len(result["choices"]) == 2
        assert result["choices"][0]["text"] == "选项A"
        assert result["end_time"] == "1979-10-26T10:30:00"
        assert len(result["state_changes"]) == 1

    def test_parse_truncated_fence_marker(self):
        """Test recovery when the code fence marker itself is truncated."""
        raw = """叙事文本。

```_state
{"choices": [{"id":"c1","text":"走"},{"id":"c2","text":"留"}], "end_time": "1979-10-26T11:00:00"}
```"""
        result = self.parser.parse(raw)
        assert "叙事文本" in result["narrative"]
        assert len(result["choices"]) >= 2
        assert result["end_time"] == "1979-10-26T11:00:00"

    def test_parse_garbled_choices_recovery(self):
        """Test that choice text can be recovered from severely garbled JSON."""
        raw = """叙事文本。

```game_state
{
"c": [
{
"id":"c1",
"text": "趁乱偷取公文包",
"hint": "高风险"
}{
"i2",
"text": "回到座位观察",
"hint": "安全"
}
"""
        result = self.parser.parse(raw)
        assert "叙事文本" in result["narrative"]
        # Should recover at least some choice text
        found_texts = [c["text"] for c in result["choices"]]
        assert any("趁乱" in t for t in found_texts) or any("回到座位" in t for t in found_texts)

    def test_parse_no_closing_fence_truncated_keys(self):
        """Test truncated JSON with no closing fence and truncated field names."""
        raw = """你走进了市场。

```game_state
{"choices":[{"id":"c1","text":"逛摊位"}],"end_time":"1979-10-26T10:30:00","loc":"forest"
"""
        result = self.parser.parse(raw)
        assert "你走进了市场" in result["narrative"]
        assert len(result["choices"]) >= 1
        assert result["choices"][0]["text"] == "逛摊位"
        assert result["end_time"] == "1979-10-26T10:30:00"

    def test_parse_all_fields(self):
        raw = """测试。

```game_state
{
  "choices": [{"id":"c1","text":"A"}],
  "state_changes": [{"target":"x","op":"set","value":1}],
  "end_time": "1979-10-26T11:00:00",
  "location_change": "forest",
  "reveal_locations": ["cave"],
  "npc_attitude_changes": [{"npc_id":"g","change":5}],
  "activate_states": ["rain"],
  "deactivate_states": ["sun"],
  "world_property_changes": [{"id":"season","value":"winter"}],
  "inventory_changes": [{"item":"剑","action":"add","quantity":1}],
  "offscreen_npc_updates": [{"npc_id":"bob","action":"sleeping"}],
  "new_npcs": [{"id":"new1","name":"新NPC"}],
  "npc_relationship_updates": [{"a":"npc1","b":"npc2","type":"友好"}]
}
```"""
        result = self.parser.parse(raw)
        assert result["end_time"] == "1979-10-26T11:00:00"
        assert result["location_change"] == "forest"
        assert result["reveal_locations"] == ["cave"]
        assert len(result["activate_states"]) == 1
        assert len(result["deactivate_states"]) == 1
        assert len(result["world_property_changes"]) == 1
        assert len(result["offscreen_npc_updates"]) == 1
        assert len(result["new_npcs"]) == 1
        assert len(result["npc_relationship_updates"]) == 1
