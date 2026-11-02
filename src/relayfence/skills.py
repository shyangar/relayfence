"""Read-only view of skills installed for each runtime in the active workspace.

Discovery lives here rather than in a transaction mixin: it only reads local
directories and never takes the ledger's write lock.
"""

import threading

from .skill_catalogue import discover_skills


class SkillView:
    def __init__(self):
        self._skill_workspaces = {}
        self._skill_lock = threading.Lock()

    def set_skill_workspace(self, workspace, project="default"):
        """Register a project root without changing other running projects."""
        with self._skill_lock:
            if project is None:
                self._skill_workspaces.clear()
            elif workspace is None:
                self._skill_workspaces.pop(project, None)
            else:
                self._skill_workspaces[project] = str(workspace)

    def skill_workspace(self, project):
        with self._skill_lock:
            return self._skill_workspaces.get(project)

    def _skill_list(self, db, r, now):
        from .mcp_catalogue import discover_mcp

        workspace = self.skill_workspace(r.project)
        return {
            **discover_skills(workspace),
            "mcp": discover_mcp(workspace),
            "capabilities_version": 1,
        }

    def _mcp_list(self, db, r, now):
        from .mcp_catalogue import discover_mcp

        return {"mcp": discover_mcp(self.skill_workspace(r.project)), "capabilities_version": 1}
