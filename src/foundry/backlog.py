"""BACKLOG.md: parse and rewrite. The loop owns this file's structure.

Format:
    # Backlog
    <intro>
    ## Todo
    ### T-001: Title
    - Story: US-002
    - Accept: how to verify (a test or command)
    - Tests may change: no
    Free text.
    ## Parked
    ## Done
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

SECTIONS = ("Todo", "Parked", "Done")
HEADER = """# Backlog

Ordered: agents take the top task under **Todo**, one per run. Foundry moves tasks
to **Parked** or **Done** itself — keep the `### T-NNN: Title` headings and fields intact.
"""


@dataclass
class Task:
    id: str
    title: str
    body: str = ""  # everything under the heading, verbatim

    def field(self, name: str) -> str:
        m = re.search(rf"^- {re.escape(name)}:\s*(.*)$", self.body, re.M | re.I)
        return m.group(1).strip() if m else ""

    @property
    def tests_may_change(self) -> bool:
        return self.field("Tests may change").lower().startswith("y")

    def render(self) -> str:
        return f"### {self.id}: {self.title}\n{self.body.strip()}\n"


@dataclass
class Backlog:
    intro: str = HEADER
    sections: dict[str, list[Task]] = field(default_factory=lambda: {s: [] for s in SECTIONS})

    @property
    def todo(self) -> list[Task]:
        return self.sections["Todo"]

    def next_id(self) -> str:
        nums = [int(t.id.split("-")[1]) for ts in self.sections.values() for t in ts
                if re.fullmatch(r"T-\d+", t.id)]
        return f"T-{max(nums, default=0) + 1:03d}"

    def move(self, task_id: str, to: str, note: str = "") -> Task:
        for tasks in self.sections.values():
            for t in tasks:
                if t.id == task_id:
                    tasks.remove(t)
                    if note:
                        t.body = t.body.rstrip() + f"\n- {note}"
                    self.sections[to].insert(0, t) if to == "Done" else self.sections[to].append(t)
                    return t
        raise KeyError(task_id)

    def render(self) -> str:
        out = [self.intro.rstrip() + "\n"]
        for name in SECTIONS:
            out.append(f"\n## {name}\n")
            for t in self.sections[name]:
                out.append("\n" + t.render())
        return "".join(out)


def parse(text: str) -> Backlog:
    bl = Backlog(intro="")
    current = None
    task = None
    intro = []
    for line in text.splitlines():
        m2 = re.match(r"^## (\w+)", line)
        m3 = re.match(r"^### (T-\d+|[A-Z]+-\d+):\s*(.*)$", line)
        if m2 and m2.group(1).capitalize() in SECTIONS:
            current, task = m2.group(1).capitalize(), None
        elif m3 and current:
            task = Task(m3.group(1), m3.group(2).strip())
            bl.sections[current].append(task)
        elif task is not None:
            task.body += line + "\n"
        elif current is None:
            intro.append(line)
    bl.intro = "\n".join(intro).strip() + "\n" if intro else HEADER
    return bl


def load(repo: Path) -> Backlog:
    p = repo / "BACKLOG.md"
    return parse(p.read_text()) if p.exists() else Backlog()


def save(repo: Path, bl: Backlog) -> None:
    (repo / "BACKLOG.md").write_text(bl.render())


def from_items(items: list[dict], start: int = 1) -> list[Task]:
    """Tasks from model JSON: [{title, story, accept, tests_may_change, details}]."""
    tasks = []
    for i, it in enumerate(items, start):
        body = (f"- Story: {it.get('story', '')}\n"
                f"- Accept: {it.get('accept', '')}\n"
                f"- Tests may change: {'yes' if it.get('tests_may_change') else 'no'}\n")
        if it.get("details"):
            body += "\n" + it["details"].strip() + "\n"
        tasks.append(Task(f"T-{i:03d}", it["title"].strip(), body))
    return tasks
