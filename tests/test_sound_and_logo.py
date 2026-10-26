"""The new-event chime and the app logo."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from steward import ASSETS, sound
from steward.config import load_config
from steward.web.app import create_app

from .conftest import run, sandbox_json


def _deliver(steward, uid: int, subject: str, body: str) -> None:
    box = sandbox_json(steward, "mailbox.json")
    box["messages"].append({"uid": uid, "message_id": f"<m{uid}@example.test>", "from_name": "Someone",
                            "from_email": f"someone{uid}@example.test", "to": [steward.cfg.profile.email], "cc": [],
                            "date": "2026-10-21T10:00:00+00:00", "in_reply_to": "", "references": [],
                            "subject": subject, "body_text": body})
    (steward.cfg.sandbox_path / "mailbox.json").write_text(json.dumps(box), encoding="utf-8")


def test_each_check_with_new_events_chimes_once(steward, chimes):
    first = run(steward.cycle(force=True))
    assert first["new_events"] >= 2 and chimes == [sound.CHIME]  # one chime for the batch, not one per message
    assert sound.CHIME.is_file()

    _deliver(steward, 300, "Your weekly gardening tips", "Tomatoes love the sun.")
    quiet = run(steward.cycle(force=True))
    assert quiet["new_events"] == 0 and len(chimes) == 1  # mail filtered out as unrelated stays silent

    _deliver(steward, 301, "Senior Data Engineer role", "Hi! Are you open to a new position? Recruiter at Acme.")
    assert run(steward.cycle(force=True))["new_events"] == 1 and len(chimes) == 2

    steward.cfg.notifications.sound = False
    _deliver(steward, 302, "Platform Engineer opportunity", "Would you like to interview with us?")
    assert run(steward.cycle(force=True))["new_events"] == 1 and len(chimes) == 2  # switched off in Setup


def test_dashboard_serves_the_logo_and_tests_the_sound(workspace, chimes):
    app = create_app(load_config(workspace), watch=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/").text
        assert '<link rel="icon" type="image/x-icon" href="/favicon.ico">' in page and "<title>Job Sourcer</title>" in page
        token = page.split('const TOKEN = "')[1].split('"')[0]
        icons = ASSETS / "logo" / "windows"
        res = client.get("/favicon.ico")  # the multi-size .ico: Windows picks the size it draws
        assert res.status_code == 200 and res.headers["content-type"] == "image/x-icon"
        assert res.content == (icons / "icon-256x256px.ico").read_bytes()
        for size in (16, 32, 48, 64, 128, 256):
            name = f"icon-{size}x{size}px.png"
            assert f'href="/icons/{name}"' in page
            res = client.get(f"/icons/{name}")
            assert res.status_code == 200 and res.headers["content-type"] == "image/png"
            assert res.content == (icons / name).read_bytes()
        assert client.get("/app-logo.png").content == (icons / "icon-256x256px.png").read_bytes()
        assert client.get("/manifest.json").json()["icons"][-1]["sizes"] == "256x256"
        assert client.get("/browserconfig.xml").status_code == 200
        assert client.post("/api/setup/sound-test").status_code == 403  # needs the page token like every action
        assert client.post("/api/setup/sound-test", headers={"X-Steward-Token": token}).json()["ok"]
        assert chimes == [sound.CHIME]
