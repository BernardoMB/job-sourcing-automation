"""Job Sourcer: a recruiter-facing chief of staff.

A local, self-hosted agentic harness that runs a recruiter-facing job search
asynchronously: it answers from the CV, schedules around the calendar, drafts
every reply, and escalates only the decisions it has not yet earned the right
to make alone.
"""

from pathlib import Path

__version__ = "0.1.0"

# The app logo and the new-event chime (the project's assets/ folder).
ASSETS = Path(__file__).resolve().parent.parent / "assets"
