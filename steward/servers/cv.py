"""CV MCP server: read-only, retrieval-based access to your markdown CV.

Every tool is read-only and never reaches the network. The CV Analyst subagent
uses these tools to collect exact evidence; it never invents experience.
"""

from __future__ import annotations

from typing import Any

from steward.cvparse import CV
from steward.servers.common import READ_ONLY, guarded, make_server, server_config

cfg = server_config()
mcp = make_server(
    "steward-cv",
    "Read-only retrieval over the candidate's markdown CV. Returns exact evidence lines; never generates claims.",
)


def _cv() -> CV:
    # Re-read on every call so edits to the CV take effect immediately.
    return CV.load(cfg.resolve(cfg.cv.path), aliases=cfg.cv.aliases, exclude_sections=cfg.cv.exclude_sections)


@mcp.tool(annotations=READ_ONLY)
@guarded
def get_profile() -> dict[str, Any]:
    """Name, contact lines, summary, total years of experience, and most recent roles."""
    return _cv().profile()


@mcp.tool(annotations=READ_ONLY)
@guarded
def skill_experience(skill: str) -> dict[str, Any]:
    """Evidence for one skill: whether it appears, years (stated or computed from role dates), roles, lines."""
    return _cv().skill_experience(skill)


@mcp.tool(annotations=READ_ONLY)
@guarded
def search_cv(query: str, limit: int = 8) -> dict[str, Any]:
    """Keyword and technology search over CV lines. Returns matching lines with their section and role."""
    return {"query": query, "hits": _cv().search(query, limit)}


@mcp.tool(annotations=READ_ONLY)
@guarded
def list_skills() -> dict[str, Any]:
    """All skills found in the CV with years of experience and when they were last used."""
    return {"skills": _cv().list_skills()}


@mcp.tool(annotations=READ_ONLY)
@guarded
def get_section(name: str) -> dict[str, Any]:
    """Return the lines of the first CV section whose heading contains `name`."""
    return _cv().get_section(name)


if __name__ == "__main__":
    mcp.run()
