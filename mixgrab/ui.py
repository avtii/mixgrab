"""Terminal UI: tracklist table, identification progress, per-track download bars."""
from __future__ import annotations

from rich.console import Console
from rich.markup import escape as esc
from rich.panel import Panel
from rich.progress import (BarColumn, DownloadColumn, Progress, ProgressColumn, TaskProgressColumn,
                           TextColumn, TimeRemainingColumn, TransferSpeedColumn)
from rich.table import Table
from rich.text import Text

from .tracklist import Tracklist

console = Console(highlight=False)


def fmt_ts(s) -> str:
    if s is None:
        return ""
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def header(title: str, url: str) -> None:
    console.print(Panel.fit(f"[bold]{esc(title)}[/]\n[dim]{esc(url)}[/]", border_style="cyan"))


def tracklist_table(tl: Tracklist) -> None:
    t = Table(title=f"[bold]{len(tl.tracks)} tracks[/] [dim]· from {esc(tl.found_in)}[/]",
              title_justify="left", box=None, pad_edge=False, show_header=False)
    t.add_column(justify="right", style="dim")
    t.add_column(justify="right", style="cyan")
    t.add_column()
    for tr in tl.tracks:
        t.add_row(f"{tr.index:02d}", fmt_ts(tr.start), f"[bold]{esc(tr.artist)}[/] – {esc(tr.title)}")
    console.print(t)
    console.print()


class IdentifyProgress:
    def __init__(self, live: bool = True):
        self.p = Progress(TextColumn("{task.description}"), BarColumn(bar_width=30),
                          TaskProgressColumn(), TextColumn("[dim]{task.fields[info]}"),
                          console=console, transient=False)
        self.live = live                # False: a Board draws it, under the downloads
        self.audio = self.clips = None

    def __enter__(self):
        if self.live:
            self.p.start()
        self.audio = self.p.add_task("Fetching mix audio ", total=None, info="")
        return self

    def __exit__(self, *exc):
        if self.live:
            self.p.stop()

    def note(self, text: str):
        """A word on the identification line once it is over (the downloads go on above it)."""
        if self.clips is None:
            self.clips = self.p.add_task("Identifying songs  ", total=None, info="")
        self.p.update(self.clips, info=text)

    def on_audio(self, done, total):
        self.p.update(self.audio, completed=done, total=total or None,
                      info=f"{done / 1e6:.0f}/{total / 1e6:.0f} MB" if total else "")

    def on_clips(self, done, total, matches, wait=0):
        if self.clips is None:
            self.p.update(self.audio, completed=1, total=1)
            self.clips = self.p.add_task("Identifying songs  ", total=total, info="")
        info = f"{matches} matches"
        if wait:
            info += f" [yellow]· Shazam is rate-limiting, waiting {wait}s[/]"
        self.p.update(self.clips, completed=done, total=total, info=info)


STATE_STYLE = {
    "pending": "[dim]waiting[/]",
    "searching": "[yellow]searching[/]",
    "waiting": "[yellow]found[/]",
    "queued": "[magenta]in peer queue[/]",
    "downloading": "[cyan]downloading[/]",
    "retry": "[yellow]retrying[/]",
    "youtube": "[blue]trying YouTube[/]",
    "soundcloud": "[blue]trying SoundCloud[/]",
    "converting": "[cyan]→ mp3[/]",
    "done": "[green]✓ done[/]",
    "skip": "[green]✓ have it[/]",
    "missing": "[red]✗ not found[/]",
}


class _TrackOnly(ProgressColumn):
    """Byte/speed/ETA columns make no sense on the 'All' row (it counts tracks), so leave it blank there."""
    def __init__(self, inner: ProgressColumn):
        super().__init__()
        self.inner = inner

    def render(self, task):
        return Text("") if task.fields.get("num") == "  " else self.inner.render(task)


class DownloadProgress:
    def __init__(self, tracks, live: bool = True):
        self.p = Progress(
            TextColumn("{task.fields[num]}", style="dim"),
            TextColumn("{task.fields[name]}", style="bold", markup=False),
            TextColumn("{task.fields[state]}"),
            BarColumn(bar_width=24),
            TaskProgressColumn(),
            _TrackOnly(DownloadColumn()),
            _TrackOnly(TransferSpeedColumn()),
            _TrackOnly(TimeRemainingColumn(compact=True)),
            TextColumn("{task.fields[note]}", style="dim", markup=False),
            console=console, expand=False, refresh_per_second=4,
        )
        self.tasks = {}
        self.tracks = list(tracks)
        self.live = live                # False: a Board draws it, above the identification line
        # tracks still to come (found while identifying) get the widest column
        self.width = min(max((len(t.label) for t in tracks), default=20), 34) if tracks else 34

    def __enter__(self):
        if self.live:
            self.p.start()
        self.overall = self.p.add_task("", total=0, num="  ", name="All".ljust(self.width),
                                       state="", note="")
        for t in self.tracks:
            self.add(t)
        return self

    def __exit__(self, *exc):
        if self.live:
            self.p.stop()

    def add(self, t, num: str | None = None):
        """A row for track t, keyed by t.index; num is what the first column shows (its number by default)."""
        name = t.label if len(t.label) <= self.width else t.label[: self.width - 1] + "…"
        self.tasks[t.index] = self.p.add_task("", total=None, start=False,
                                              num=num if num is not None else f"{t.index:02d}",
                                              name=name.ljust(self.width),
                                              state=STATE_STYLE["pending"], note="")
        overall = next(x for x in self.p.tasks if x.id == self.overall)
        self.p.update(self.overall, total=(overall.total or 0) + 1)

    def number(self, idx, n: int):
        """Show the final track number on a row that started without one."""
        tid = self.tasks.get(idx)
        if tid is not None:
            self.p.update(tid, num=f"{n:02d}")

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
        elif state in ("youtube", "soundcloud"):
            upd["note"] = "not on Soulseek" if state == "youtube" else "not on Soulseek/YouTube"
            upd["completed"] = 0
        elif state == "converting":
            upd["note"] = "flac → mp3"
        elif state == "missing":
            upd["note"] = i.get("note", "")[:40]
        self.p.update(tid, **upd)
        if state in ("done", "skip", "missing"):
            self.p.advance(self.overall)


class Board:
    """One live display: the track downloads on top, the identification line under them."""

    def __init__(self):
        from rich.console import Group
        from rich.live import Live
        self.dl = DownloadProgress([], live=False)
        self.id = IdentifyProgress(live=False)
        self.live = Live(Group(self.dl.p, self.id.p), console=console, refresh_per_second=4)

    def __enter__(self):
        self.dl.__enter__()
        self.id.__enter__()
        self.live.start()
        return self

    def __exit__(self, *exc):
        self.live.stop()
        if not console.is_terminal:     # rich ends the last line itself only on a terminal
            console.line()


def summary(tracks, results, out_dir) -> None:
    ok = [t for t in tracks if results[t.index].path]
    console.print()
    style = "green" if len(ok) == len(tracks) else "yellow"
    console.print(f"[bold {style}]Downloaded {len(ok)}/{len(tracks)}[/] → {esc(out_dir)}")
    for t in tracks:
        r = results[t.index]
        path, msg = r.path, r.msg
        if not path:
            console.print(f"  [red]✗[/] {t.index:02d}. {esc(t.label)} [dim]— {esc(msg)}[/]")
