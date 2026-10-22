from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import yaml

from steward.config import load_config
from steward.orchestrator import Steward
from steward.workspace import init_workspace


@pytest.fixture(autouse=True)
def chimes(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Tests never play sound: record each chime instead."""
    played: list[Path] = []
    monkeypatch.setattr("steward.sound.play", lambda path, wait=0.0: played.append(path) or True)
    return played


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = init_workspace(tmp_path / "ws", sandbox=True)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["llm"]["provider"] = "offline"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture()
def steward(workspace: Path) -> Steward:
    s = Steward(load_config(workspace))
    yield s
    s.db.close()


def run(coro):
    return asyncio.run(coro)


def sandbox_json(steward: Steward, name: str) -> dict:
    return json.loads((steward.cfg.sandbox_path / name).read_text(encoding="utf-8"))


def drafts_by_company(steward: Steward) -> dict[str, dict]:
    out = {}
    for d in steward.db.query("SELECT * FROM drafts ORDER BY id"):
        opp = steward.db.get_opportunity(d["opportunity_id"]) or {}
        out[opp.get("company") or d["summary"]] = d
    return out
