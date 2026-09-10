"""Regex script engine: post-processing rules applied to AI output or user input."""

import re


class RegexScriptEngine:
    """Applies regex find/replace rules based on placement."""

    def __init__(self, scripts: list[dict]):
        self.scripts = [s for s in scripts if s.get("enabled", True)]

    def apply(self, text: str, placement: str) -> str:
        """Apply all matching regex scripts to text."""
        for script in self.scripts:
            script_placement = script.get("placement", "ai_output")
            if isinstance(script_placement, list):
                if placement not in script_placement:
                    continue
            elif placement != script_placement:
                continue
            find = script.get("find", "")
            if not find:
                continue
            try:
                flags = 0
                if script.get("ignore_case"):
                    flags |= re.IGNORECASE
                if script.get("dotall"):
                    flags |= re.DOTALL
                text = re.sub(find, script.get("replace", ""), text, flags=flags)
            except re.error:
                continue
        return text
