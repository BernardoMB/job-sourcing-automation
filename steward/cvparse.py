"""Read-only, retrieval-based analysis of a markdown CV.

Nothing here generates text: every answer is a pointer to lines that exist in
the CV, plus arithmetic over the role dates found there. If the CV does not
contain the evidence, the answer is "not found", and Steward escalates to you.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from steward.vocab import TECH_ALIASES, canonical, find_technologies, mentions

_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
_DATE = rf"(?:{_MONTH}\.?\s+\d{{4}}|\d{{1,2}}/\d{{4}}|\d{{4}}-\d{{2}}|\d{{4}})"
RANGE_RE = re.compile(
    rf"({_DATE})\s*(?:–|—|-|to|until)\s*({_DATE}|present|current|now|today)", re.IGNORECASE
)
YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
SKILL_SECTION_RE = re.compile(r"skill|technolog|tech stack|toolbox|expertise|competenc", re.IGNORECASE)
EXPERIENCE_SECTION_RE = re.compile(r"experience|employment|work history|career", re.IGNORECASE)
SUMMARY_SECTION_RE = re.compile(r"summary|profile|about", re.IGNORECASE)
# Date ranges in these sections are not jobs (a degree is not years of experience).
NON_ROLE_SECTION_RE = re.compile(
    r"educat|academic|certif|course|training|award|publication|language|volunteer|reference|interest|hobb",
    re.IGNORECASE,
)
STOPWORDS = {
    "the", "and", "for", "with", "you", "your", "have", "has", "had", "are", "was", "were", "what", "when", "where",
    "which", "who", "how", "any", "some", "about", "experience", "years", "year", "work", "worked", "working",
    "this", "that", "there", "their", "from", "into", "also", "would", "could", "should", "can", "did", "does",
    "using", "used", "use", "our", "team", "role",
}
MONTH_NAMES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _strip_md(text: str) -> str:
    text = re.sub(r"^\s*>\s?", "", text)  # blockquote marker
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1", text)
    return text.strip(" -\t|")


def _parse_date(token: str, is_end: bool, today: date) -> date | None:
    t = token.strip().lower()
    if t in ("present", "current", "now", "today"):
        return None
    m = re.match(r"^([a-z]+)\.?\s+(\d{4})$", t)
    if m:
        month = MONTH_NAMES.index(m.group(1)[:3]) + 1 if m.group(1)[:3] in MONTH_NAMES else (12 if is_end else 1)
        return date(int(m.group(2)), month, 1)
    m = re.match(r"^(\d{1,2})/(\d{4})$", t)
    if m:
        return date(int(m.group(2)), max(1, min(12, int(m.group(1)))), 1)
    m = re.match(r"^(\d{4})-(\d{2})$", t)
    if m:
        return date(int(m.group(1)), max(1, min(12, int(m.group(2)))), 1)
    m = re.match(r"^(\d{4})$", t)
    if m:
        return date(int(m.group(1)), 12 if is_end else 1, 1)
    return None


def _months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _merge_years(intervals: list[tuple[date, date]]) -> float:
    if not intervals:
        return 0.0
    ivs = sorted(intervals)
    merged = [list(ivs[0])]
    for s, e in ivs[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    months = sum(_months(s, e) for s, e in merged)
    return round(months / 12 * 2) / 2  # nearest half year


@dataclass
class Role:
    title: str
    start: date
    end: date | None
    lines: list[str]
    section: str

    def end_or(self, today: date) -> date:
        return self.end or date(today.year, today.month, 1)

    @property
    def text(self) -> str:
        return "\n".join([self.title, *self.lines])

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "start": self.start.strftime("%Y-%m"),
            "end": self.end.strftime("%Y-%m") if self.end else "present",
            "section": self.section,
        }


@dataclass
class Line:
    text: str
    section: str
    role: str
    file: str
    number: int


@dataclass
class CV:
    name: str = ""
    files: list[str] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    roles: list[Role] = field(default_factory=list)
    stated_skills: dict[str, float | None] = field(default_factory=dict)
    contact: list[str] = field(default_factory=list)
    summary: str = ""
    aliases: dict[str, list[str]] = field(default_factory=dict)
    today: date = field(default_factory=date.today)
    # Sections/roles whose lines are never quoted as evidence (years still count).
    exclude_sections: list[str] = field(default_factory=list)
    pending_heading: Any = None

    # -- loading --------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path, aliases: dict[str, list[str]] | None = None, today: date | None = None,
             exclude_sections: list[str] | None = None) -> "CV":
        p = Path(path)
        files = sorted(p.glob("*.md")) if p.is_dir() else [p]
        cv = cls(aliases=aliases or {}, today=today or date.today(), exclude_sections=exclude_sections or [])
        for f in files:
            if f.exists():
                cv._parse(f.read_text(encoding="utf-8"), f.name)
        return cv

    @classmethod
    def from_text(cls, text: str, aliases: dict[str, list[str]] | None = None, today: date | None = None) -> "CV":
        cv = cls(aliases=aliases or {}, today=today or date.today())
        cv._parse(text, "cv.md")
        return cv

    def _parse(self, text: str, filename: str) -> None:
        self.files.append(filename)
        stack: list[tuple[int, str]] = []
        current_role: Role | None = None
        role_level = 99
        lines_since_start = 0
        raw_lines = text.splitlines()
        for number, raw in enumerate(raw_lines, start=1):
            line = raw.rstrip()
            if not line.strip():
                continue
            hm = HEADING_RE.match(line)
            if hm:
                level, title = len(hm.group(1)), _strip_md(hm.group(2))
                if level == 1 and not self.name:
                    self.name = RANGE_RE.sub("", title).strip()
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
                if current_role and level <= role_level:
                    current_role = None
                rng = RANGE_RE.search(title)
                non_role = self._non_role(stack)
                if rng and level > 1 and not non_role:
                    current_role = self._new_role(RANGE_RE.sub("", title), rng, stack)
                    role_level = level
                elif level > 1 and current_role is None:
                    # A heading whose date range sits on the next line(s). Deeper headings inside a
                    # role ("### Key Achievements") keep the role open instead.
                    self.pending_heading = None if non_role else (title, level, number)
                lines_since_start = 0
                continue
            section = " > ".join(t for _, t in stack)
            plain = _strip_md(line)
            rng = RANGE_RE.search(line)
            bullet_plain = re.match(r"^\s*[-*]\s+(?!\*\*)", line) is not None
            pending = self.pending_heading
            if rng and not bullet_plain and len(plain) < 180 and not self._non_role(stack):
                if pending and pending[2] >= number - 3 and current_role is None:
                    title = pending[0] + (" - " + RANGE_RE.sub("", plain).strip(" -|,") if RANGE_RE.sub("", plain).strip(" -|,") else "")
                    current_role = self._new_role(title, rng, stack[:-1] or stack, section_override=section)
                    role_level = pending[1]
                    self.pending_heading = None
                elif current_role is not None and lines_since_start <= 1 and not current_role.lines:
                    pass  # the date line of a role we already opened
                else:
                    current_role = self._new_role(RANGE_RE.sub("", plain), rng, stack)
                    role_level = (stack[-1][0] + 1) if stack else 2
                lines_since_start = 0
                continue
            lines_since_start += 1
            self.sections.setdefault(section, []).append(plain)
            role_title = current_role.title if current_role else ""
            if current_role is not None:
                current_role.lines.append(plain)
            self.lines.append(Line(plain, section, role_title, filename, number))
            top = stack[-1][1] if stack else ""
            if SKILL_SECTION_RE.search(top):
                self._parse_skill_line(plain)
            if not stack or (len(stack) == 1 and number <= 12):
                if re.search(r"@|linkedin|github|\+?\d[\d\s().-]{7,}", plain, re.IGNORECASE):
                    self.contact.append(plain)
            if SUMMARY_SECTION_RE.search(top) and len(self.summary) < 1200:
                self.summary = (self.summary + " " + plain).strip()

    @staticmethod
    def _non_role(stack: list[tuple[int, str]]) -> bool:
        return any(NON_ROLE_SECTION_RE.search(t) for _, t in stack)

    def _visible(self, line: "Line") -> bool:
        where = f"{line.section} {line.role}".lower()
        return not any(p.lower() in where for p in self.exclude_sections if p.strip())

    def _new_role(self, title: str, rng: re.Match[str], stack: list[tuple[int, str]],
                  section_override: str | None = None) -> Role:
        start = _parse_date(rng.group(1), False, self.today) or self.today
        end = _parse_date(rng.group(2), True, self.today)
        section = section_override or " > ".join(t for _, t in stack)
        role = Role(title=_strip_md(title).strip(" -|,"), start=start, end=end, lines=[], section=section)
        self.roles.append(role)
        return role

    def _parse_skill_line(self, plain: str) -> None:
        if ":" not in plain and "," not in plain:
            return  # a sub-heading or sentence, not a list of skills
        body = plain.split(":", 1)[1] if ":" in plain else plain
        for item in re.split(r"[,;|·•]", body):
            item = item.strip()
            if not item or len(item) > 60:
                continue
            years = None
            ym = YEARS_RE.search(item)
            if ym:
                years = float(ym.group(1))
            name = re.sub(r"\(.*?\)", "", item)
            name = YEARS_RE.sub("", name).strip(" -")
            if not name:
                continue
            canon = canonical(name, self.aliases)
            prev = self.stated_skills.get(canon)
            self.stated_skills[canon] = years if prev is None else max(prev, years or 0)

    # -- queries --------------------------------------------------------
    def total_years(self) -> float:
        return _merge_years([(r.start, r.end_or(self.today)) for r in self.roles])

    def skill_experience(self, skill: str) -> dict[str, Any]:
        canon = canonical(skill, self.aliases)
        roles = [r for r in self.roles if mentions(r.text, canon, self.aliases)]
        computed = _merge_years([(r.start, r.end_or(self.today)) for r in roles])
        stated_key = next((k for k in self.stated_skills if k.lower() == canon.lower()), None)
        stated = self.stated_skills.get(stated_key) if stated_key else None
        evidence = [
            {"text": l.text, "section": l.section, "role": l.role, "line": l.number}
            for l in self.lines
            if mentions(l.text, canon, self.aliases) and self._visible(l)
        ][:8]
        found = bool(roles or stated_key or evidence)
        years = stated if stated else (computed or None)
        source = "stated" if stated else ("computed_from_roles" if computed else "none")
        last_used = None
        if roles:
            latest = max(roles, key=lambda r: r.end_or(self.today))
            last_used = "present" if latest.end is None else latest.end.strftime("%Y-%m")
        return {
            "skill": canon,
            "found": found,
            "years": years,
            "years_source": source,
            "computed_years": computed,
            "last_used": last_used,
            "roles": [r.as_dict() for r in sorted(roles, key=lambda r: r.end_or(self.today), reverse=True)],
            "evidence": evidence,
            "listed_in_skills": bool(stated_key),
        }

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        techs = find_technologies(query, self.aliases)
        words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z+#.\-]{2,}", query)]
        terms = [w for w in words if w not in STOPWORDS]
        scored = []
        for l in self.lines:
            if not self._visible(l):
                continue
            low = l.text.lower()
            score = sum(1.0 for t in terms if t in low)
            score += sum(2.0 for t in techs if mentions(l.text, t, self.aliases))
            if score > 0:
                scored.append((score, l))
        scored.sort(key=lambda x: (-x[0], x[1].number))
        return [
            {"text": l.text, "section": l.section, "role": l.role, "line": l.number, "score": s}
            for s, l in scored[:limit]
        ]

    def list_skills(self) -> list[dict[str, Any]]:
        names = set(self.stated_skills)
        names.update(find_technologies("\n".join(l.text for l in self.lines), self.aliases))
        out = []
        for n in sorted(names, key=str.lower):
            info = self.skill_experience(n)
            out.append({"skill": info["skill"], "years": info["years"], "last_used": info["last_used"],
                        "listed_in_skills": info["listed_in_skills"]})
        return out

    def get_section(self, name: str) -> dict[str, Any]:
        for key, lines in self.sections.items():
            if name.lower() in key.lower():
                return {"section": key, "lines": lines}
        return {"section": "", "lines": []}

    def profile(self) -> dict[str, Any]:
        recent = sorted(self.roles, key=lambda r: r.end_or(self.today), reverse=True)[:3]
        return {
            "name": self.name,
            "contact": self.contact,
            "summary": self.summary,
            "total_years": self.total_years(),
            "recent_roles": [r.as_dict() for r in recent],
            "sections": list(self.sections),
            "files": self.files,
        }


def known_tech_names() -> list[str]:
    return list(TECH_ALIASES)
