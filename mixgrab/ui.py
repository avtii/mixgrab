"""Terminal UI: tracklist table, identification progress, per-track download bars."""
from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.progress import (BarColumn, DownloadColumn, Progress, TaskProgressColumn, TextColumn,
                           TimeRemainingColumn, TransferSpeedColumn)
from rich.table import Table

from .tracklist import Tracklist

console = Console(highlight=False)


def fmt_ts(s) -> str:
    if s is None:
        return ""
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def header(title: str, url: str) -> None:
    console.print(Panel.fit(f"[bold]{title}[/]\n[dim]{url}[/]", border_style="cyan"))


def tracklist_table(tl: Tracklist) -> None:
    t = Table(title=f"[bold]{len(tl.tracks)} tracks[/] [dim]· from {tl.found_in}[/]",
              title_justify="left", box=None, pad_edge=False, show_header=False)
    t.add_column(justify="right", style="dim")
    t.add_column(justify="right", style="cyan")
    t.add_column()
    for tr in tl.tracks:
        t.add_row(f"{tr.index:02d}", fmt_ts(tr.start), f"[bold]{tr.artist}[/] – {tr.title}")
    console.print(t)
    console.print()


class IdentifyProgress:
    def __init__(self):
        self.p = Progress(TextColumn("{task.description}"), BarColumn(bar_width=30),
                          TaskProgressColumn(), TextColumn("[dim]{task.fields[info]}"),
                          console=console, transient=False)
        self.audio = self.clips = None

    def __enter__(self):
        self.p.start()
        self.audio = self.p.add_task("Fetching mix audio ", total=None, info="")
        return self

    def __exit__(self, *exc):
        self.p.stop()

    def on_audio(self, done, total):
        self.p.update(self.audio, completed=done, total=total or None,
                      info=f"{done / 1e6:.0f}/{total / 1e6:.0f} MB" if total else "")

    def on_clips(self, done, total, matches):
        if self.clips is None:
            self.p.update(self.audio, completed=1, total=1)
            self.clips = self.p.add_task("Identifying songs  ", total=total, info="")
        self.p.update(self.clips, completed=done, info=f"{matches} matches")


STATE_STYLE = {
    "pending": "[dim]waiting[/]",
    "searching": "[yellow]searching[/]",
    "waiting": "[yellow]found[/]",
    "queued": "[magenta]in peer queue[/]",
    "downloading": "[cyan]downloading[/]",
    "retry": "[yellow]retrying[/]",
    "done": "[green]✓ done[/]",
    "skip": "[green]✓ have it[/]",
    "missing": "[red]✗ not found[/]",
}


class DownloadProgress:
    def __init__(self, tracks):
        self.p = Progress(
            TextColumn("{task.fields[num]}", style="dim"),
            TextColumn("{task.fields[name]}", style="bold"),
            TextColumn("{task.fields[state]}"),
            BarColumn(bar_width=24),
            TaskProgressColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(compact=True),
            TextColumn("[dim]{task.fields[note]}"),
            console=console, expand=False, refresh_per_second=4,
        )
        self.tasks = {}
        self.tracks = tracks
        self.width = min(max((len(t.label) for t in tracks), default=20), 34)

    def __enter__(self):
        self.p.start()
        self.overall = self.p.add_task("", total=len(self.tracks), num="  ", name="All".ljust(self.width),
                                       state="", note="")
        for t in self.tracks:
            name = t.label if len(t.label) <= self.width else t.label[: self.width - 1] + "…"
            self.tasks[t.index] = self.p.add_task("", total=None, start=False, num=f"{t.index:02d}",
                                                  name=name.ljust(self.width),
                                                  state=STATE_STYLE["pending"], note="")
        return self

    def __exit__(self, *exc):
        self.p.stop()

    def status(self, idx, state, **i):
        tid = self.tasks.get(idx)
        if tid is None:
            return
        upd = {"state": STATE_STYLE.get(state, state)}
        if state == "searching":
            upd["note"] = f"“{i.get('query', '')}”"
        elif state == "waiting":
            upd["note"] = f"{i.get('ext', '')} · peer {i.get('attempt')}/{min(i.get('candidates', 0), 6)}"
            upd["total"] = i.get("total")
            upd["completed"] = 0
        elif state == "queued":
            place = i.get("place")
            upd["note"] = (f"place {place} · " if place else "") + f"{i.get('waited', 0)}s · {i.get('user')}"
        elif state == "downloading":
            task = self.p.tasks[[t.id for t in self.p.tasks].index(tid)]
            if not task.started:
                self.p.start_task(tid)
            upd.update(total=i.get("total"), completed=i.get("done", 0), note=i.get("user", ""))
        elif state == "retry":
            upd["note"] = i.get("reason", "")[:40]
            upd["completed"] = 0
        elif state in ("done", "skip"):
            upd["note"] = i.get("note", "")
            task = self.p.tasks[[t.id for t in self.p.tasks].index(tid)]
            upd["total"] = task.total or 1
            upd["completed"] = upd["total"]
        elif state == "missing":
            upd["note"] = i.get("note", "")[:40]
        self.p.update(tid, **upd)
        if state in ("done", "skip", "missing"):
            self.p.advance(self.overall)


def summary(tracks, results, out_dir) -> None:
    ok = [t for t in tracks if results[t.index][0]]
    console.print()
    style = "green" if len(ok) == len(tracks) else "yellow"
    console.print(f"[bold {style}]Downloaded {len(ok)}/{len(tracks)}[/] → [link=file:///{out_dir}]{out_dir}[/]")
    for t in tracks:
        path, msg = results[t.index]
        if not path:
            console.print(f"  [red]✗[/] {t.index:02d}. {t.label} [dim]— {msg}[/]")
