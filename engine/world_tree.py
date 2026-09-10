"""World Tree: branching history data structure."""

import json
import uuid
from datetime import datetime, timezone


class WorldTree:
    def __init__(self, tree_id: str | None = None, script_id: str = ""):
        self.tree_id = tree_id or str(uuid.uuid4())
        self.script_id = script_id
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.root_node_id: str | None = None
        self.active_node_id: str | None = None
        self.nodes: dict[str, dict] = {}
        self._branch_cache: list[dict] | None = None
        self._branch_dirty: bool = True

    def add_node(
        self,
        parent_id: str | None,
        game_time: str,
        turn_number: int,
        player_action: dict | None = None,
        ai_response: str = "",
        choices_presented: list[dict] | None = None,
        dice_rolls: list[dict] | None = None,
        state_changes: list[dict] | None = None,
        triggered_events: list[str] | None = None,
        state_snapshot: dict | None = None,
    ) -> str:
        node_id = f"n_{uuid.uuid4().hex[:8]}"
        node = {
            "id": node_id,
            "parent_id": parent_id,
            "children_ids": [],
            "game_time": game_time,
            "turn_number": turn_number,
            "player_action": player_action,
            "ai_response": ai_response,
            "choices_presented": choices_presented or [],
            "dice_rolls": dice_rolls or [],
            "state_changes": state_changes or [],
            "triggered_events": triggered_events or [],
            "state_snapshot": state_snapshot or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        self.nodes[node_id] = node

        if parent_id and parent_id in self.nodes:
            self.nodes[parent_id]["children_ids"].append(node_id)
            # B3: 标记待驱逐而非立即清除，等 DB 持久化后再清
            parent = self.nodes[parent_id]
            if (len(parent["children_ids"]) == 1
                    and parent_id != self.root_node_id
                    and not parent.get("_snapshot_evicted")
                    and not parent.get("_pending_eviction")):
                parent["_pending_eviction"] = True

        if self.root_node_id is None:
            self.root_node_id = node_id

        self.active_node_id = node_id
        self._branch_dirty = True
        return node_id

    def commit_pending_eviction(self, node_id: str):
        """Commit a pending snapshot eviction after the node has been persisted to DB."""
        node = self.nodes.get(node_id)
        if not node:
            return
        if node.get("_pending_eviction") and not node.get("_snapshot_evicted"):
            node["state_snapshot"] = None
            node["_snapshot_evicted"] = True
            node.pop("_pending_eviction", None)

    def get_node(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def get_branch(self, node_id: str) -> list[dict]:
        """Get the path from root to the specified node."""
        path = []
        current = node_id
        while current:
            node = self.nodes.get(current)
            if not node:
                break
            path.append(node)
            current = node.get("parent_id")
        path.reverse()
        return path

    def get_active_branch(self) -> list[dict]:
        if not self.active_node_id:
            return []
        if not self._branch_dirty and self._branch_cache is not None:
            return self._branch_cache
        self._branch_cache = self.get_branch(self.active_node_id)
        self._branch_dirty = False
        return self._branch_cache

    def get_children(self, node_id: str) -> list[dict]:
        node = self.nodes.get(node_id)
        if not node:
            return []
        return [self.nodes[cid] for cid in node["children_ids"] if cid in self.nodes]

    def get_recent_history(self, n: int = 5) -> list[dict]:
        """Get the last N nodes on the active branch."""
        branch = self.get_active_branch()
        return branch[-n:] if len(branch) > n else branch

    def set_active_node(self, node_id: str):
        if node_id in self.nodes:
            self.active_node_id = node_id
            self._branch_dirty = True

    def remove_node(self, node_id: str) -> bool:
        """Remove a leaf node from the tree. Returns True if removed."""
        node = self.nodes.get(node_id)
        if not node:
            return False
        # Only allow removing leaf nodes (no children)
        if node.get("children_ids"):
            return False
        # Remove from parent's children list
        parent_id = node.get("parent_id")
        if parent_id and parent_id in self.nodes:
            children = self.nodes[parent_id]["children_ids"]
            if node_id in children:
                children.remove(node_id)
        del self.nodes[node_id]
        # If this was the active node, move to parent
        if self.active_node_id == node_id:
            self.active_node_id = parent_id
            self._branch_dirty = True
        return True

    def get_divergence_points(self) -> list[dict]:
        """Get all nodes that have more than one child (branching points)."""
        return [
            node for node in self.nodes.values()
            if len(node.get("children_ids", [])) > 1
        ]

    def get_tree_structure(self) -> dict:
        """Return a lightweight tree structure for visualization."""

        visited: set[str] = set()

        def build_subtree(node_id: str) -> dict | None:
            if node_id in visited:
                return None
            visited.add(node_id)
            node = self.nodes.get(node_id)
            if not node:
                return None
            action_text = ""
            if node.get("player_action"):
                action_text = node["player_action"].get("text", "")[:50]
            return {
                "id": node_id,
                "turn": node["turn_number"],
                "time": node["game_time"],
                "action_summary": action_text,
                "is_active": node_id == self.active_node_id,
                "is_divergence": len(node.get("children_ids", [])) > 1,
                "children": [
                    build_subtree(cid) for cid in node.get("children_ids", [])
                ],
            }

        if not self.root_node_id:
            return {}
        return build_subtree(self.root_node_id)

    def to_dict(self) -> dict:
        return {
            "tree_id": self.tree_id,
            "script_id": self.script_id,
            "created_at": self.created_at,
            "root_node_id": self.root_node_id,
            "active_node_id": self.active_node_id,
            "nodes": self.nodes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorldTree":
        tree = cls(tree_id=data.get("tree_id"), script_id=data.get("script_id", ""))
        tree.created_at = data.get("created_at", "")
        tree.root_node_id = data.get("root_node_id")
        tree.active_node_id = data.get("active_node_id")
        tree.nodes = data.get("nodes", {})
        tree._branch_cache = None
        tree._branch_dirty = True
        return tree
