"""Agent sessions in the terminal: the ``lc rt dashboard`` overview, the ``lc session`` picker, styled reports.

Runtime Lite has no web dashboard, so these screens are the whole view of
spend and savings there. Runs with no recorded usage (an agent opened and
closed) are left out of lists and counted instead.
"""

from __future__ import annotations

import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from lemoncrow.core.capabilities.savings_summary import _fmt_usd
from lemoncrow.gateway.cli import style
from lemoncrow.gateway.cli.picker import GAP, Line, Outcome, Page, Row, describe_lines, fit, heading, note, pad

_TAG_STYLE: Final[dict[str, str]] = {"heading": "class:heading", "total": "class:title", "dim": "class:dim"}


@dataclass(frozen=True)
class Run:
    session_id: str
    agent: str
    task: str
    status: str
    at: datetime | None
    cost: float
    saved: float

    @property
    def failed(self) -> bool:
        return self.status in ("failed", "error")


def short_ids(runs: Sequence[Run], minimum: int = 6) -> dict[str, str]:
    """Each run's id cut to the shortest prefix (at least ``minimum``) no other listed run shares."""
    ids = sorted({run.session_id for run in runs})
    short: dict[str, str] = {}
    for index, sid in enumerate(ids):
        length = minimum
        for other in (ids[index - 1] if index else "", ids[index + 1] if index + 1 < len(ids) else ""):
            common = next(
                (i for i, (a, b) in enumerate(zip(sid, other, strict=False)) if a != b), min(len(sid), len(other))
            )
            length = max(length, common + 1)
        short[sid] = sid[:length] or "?"
    return short


def _timestamp(snapshot: dict[str, Any]) -> datetime | None:
    raw = str(snapshot.get("updated_at") or snapshot.get("created_at") or "")
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _no_usage(snapshot: dict[str, Any]) -> bool:
    cost = snapshot.get("cost") or {}
    return not str(snapshot.get("task") or "").strip() and not cost.get("calls") and not cost.get("total_cost_usd")


def recent_runs(root: Path, *, limit: int, carry: bool) -> tuple[list[Run], int]:
    """The ``limit`` newest runs with recorded usage, and how many runs without any were passed over.

    ``carry`` adds the transcript-based carry credit to each run's savings:
    exact, but it reads transcripts, so only short lists afford it.
    """
    from lemoncrow.infra.runtime.session_report import build_report, list_run_snapshots

    # Ordered by the snapshot's own timestamp, the one the age column shows:
    # run.json is rewritten (imports, bridge refreshes) without touching it.
    floor = datetime.min.replace(tzinfo=UTC)
    snapshots = sorted(
        (snapshot for _path, snapshot in list_run_snapshots(root)),
        key=lambda snapshot: _timestamp(snapshot) or floor,
        reverse=True,
    )
    runs: list[Run] = []
    passed_over = 0
    for snapshot in snapshots:
        if len(runs) >= limit:
            break
        if _no_usage(snapshot):
            passed_over += 1
            continue
        try:
            report = build_report(snapshot, root, include_carry_credit=carry)
        except Exception:
            continue
        if report.total_cost_usd <= 0 and report.total_lemoncrow_savings_usd <= 0:
            passed_over += 1
            continue
        task = (str(snapshot.get("task") or "").strip().splitlines() or [""])[0]
        runs.append(
            Run(
                session_id=report.session_id or str(snapshot.get("run_id") or snapshot.get("id") or ""),
                agent=str(snapshot.get("agent") or ""),
                task=task,
                status=str(snapshot.get("status") or ""),
                at=_timestamp(snapshot),
                cost=report.total_cost_usd,
                saved=report.total_lemoncrow_savings_usd,
            )
        )
    return runs, passed_over


def age(moment: datetime | None, now: datetime | None = None) -> str:
    if moment is None:
        return ""
    return style.ago(((now or datetime.now(UTC)) - moment).total_seconds())


def duration(seconds: float) -> str:
    minutes = int(seconds) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


def _bar(fraction: float, width: int) -> str:
    filled = round(max(0.0, min(1.0, fraction)) * width)
    return style.accent("█" * filled) + style.dim("░" * (width - filled))


# ── lc rt dashboard ────────────────────────────────────────────────────────────────────


def overview_lines(root: Path, *, width: int, browser: bool, days: int = 7, n_runs: int = 8) -> list[str]:
    """The dashboard: spend and savings over ``days``, cost by model and tool, the latest runs.

    It reads the same ``build_insights`` rollup as ``lc runtime savings`` and the web
    Savings page, so every surface agrees on the numbers.
    """
    from lemoncrow.pro.runtime.insights import build_insights

    until = datetime.now(UTC)
    window = build_insights(root, since=until - timedelta(days=days), until=until)
    width = max(width, 60)
    lines = [style.title("LemonCrow", f"last {days} days"), ""]

    def stat(label: str, value: str, detail: str = "") -> None:
        lines.append(f"  {label:<10}{value:>11}  {detail}".rstrip())

    if window.session_count:
        each = window.total_duration_seconds / window.session_count
        stat("Sessions", f"{window.session_count:,}", style.dim(f"avg {duration(each)} each"))
    else:
        stat("Sessions", "0")
    stat("AI spend", _fmt_usd(window.total_cost_usd))
    saved = window.total_lemoncrow_savings_usd
    if window.total_cost_usd > 0:
        share = saved / window.total_cost_usd
        stat("Saved", _fmt_usd(saved), f"{style.dim(f'{share:.0%} of spend')}  {_bar(share, 16)}")
    else:
        stat("Saved", _fmt_usd(saved))

    total = window.total_cost_usd
    sections: list[tuple[str, list[tuple[str, float, float]]]] = []  # title, (label, cost, bar fill)
    days_shown = _each_day(window)
    if days_shown:
        peak = max(cost for _, cost in days_shown)
        sections.append(("Each day", [(label, cost, cost / peak if peak > 0 else 0.0) for label, cost in days_shown]))
    for title, costs in (("Cost by model", window.cost_by_model), ("Top tools", window.cost_by_tool)):
        shown = [(name, cost, cost / total if total > 0 else 0.0) for name, cost in list(costs.items())[:4] if cost > 0]
        if shown and title == "Cost by model":
            # Smaller models, and spend no call record names a model for, keep the shares at 100%.
            smaller = sum(cost for cost in list(costs.values())[4:] if cost > 0)
            unrecorded = total - sum(cost for cost in costs.values() if cost > 0)
            for label, cost in (("other models", smaller), ("model not recorded", unrecorded)):
                if cost >= 0.005:
                    shown.append((label, cost, cost / total))
        if shown:
            sections.append((title, shown))
    labels = [label for _, rows in sections for label, _, _ in rows]
    name_width = min(max((len(label) for label in labels), default=0) + 2, width - 38)
    for title, rows in sections:
        lines += ["", f"  {style.heading(title)}"]
        for label, cost, fill in rows:
            pct = style.dim(f"{cost / total:>4.0%}") if total > 0 and title != "Each day" else "    "
            lines.append(
                f"    {pad(fit(label, name_width - 2), name_width)}{_fmt_usd(cost):>11}  {pct}  {_bar(fill, 14)}"
            )

    runs, passed_over = recent_runs(root, limit=n_runs, carry=True)
    if runs:
        lines += ["", *_run_table(runs, width)]
        if passed_over:
            lines.append(f"    {style.dim(style.plural(passed_over, 'run') + ' with no recorded usage hidden')}")
    lines.append("")
    if not runs and not window.session_count:
        lines.append(style.row("off", "No sessions yet."))
        lines.append(style.next_step("Start one in your coding agent:", "lc with claude"))
        return lines
    if runs:
        lines.append(style.next_step("One run in full:", f"lc session report {short_ids(runs)[runs[0].session_id]}"))
    lines.append(style.next_step("Every session:", "lc session"))
    if browser:
        lines.append(style.next_step("In the browser:", "lc rt dashboard open"))
    return lines


def _each_day(window: Any) -> list[tuple[str, float]]:
    """Each local day of the window with its spend, oldest first; empty when nothing was spent.

    A window of N days reaches into an (N+1)th calendar day; that partial day shows only if it has spend.
    """
    by_day: dict[str, float] = window.cost_by_day
    if not any(cost > 0 for cost in by_day.values()):
        return []
    first, last = window.since.astimezone().date(), window.until.astimezone().date()
    dates = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    if len(dates) > 1 and by_day.get(dates[0].isoformat(), 0.0) <= 0:
        dates = dates[1:]
    names = {last: "Today", last - timedelta(days=1): "Yesterday"}
    return [(names.get(day) or f"{day:%a} {day.day} {day:%b}", by_day.get(day.isoformat(), 0.0)) for day in dates]


def _run_table(runs: Sequence[Run], width: int) -> list[str]:
    ids = short_ids(runs)
    id_width = max(len(short) for short in ids.values())
    agent_width = min(max(len(run.agent) for run in runs), 16) if width >= 90 else 0
    fixed = 4 + id_width + 2 + (agent_width + 2 if agent_width else 0) + 2 + 11 + 11 + 2 + 8
    task_width = max(width - fixed, 12)
    lead = 4 + id_width + 2 + (agent_width + 2 if agent_width else 0) + task_width + 2
    header = f"  {style.heading('Recent runs')}" + " " * (lead - 2 - len("Recent runs"))
    lines = [header + style.dim(f"{'cost':>11}{'saved':>11}")]
    for run in runs:
        agent = f"{pad(fit(run.agent, agent_width), agent_width)}  " if agent_width else ""
        task = pad(fit(run.task or "-", task_width), task_width)
        when = style.paint("failed", fg="red") if run.failed else style.dim(age(run.at))
        lines.append(
            f"    {style.dim(ids[run.session_id].ljust(id_width))}  {agent}{task}  {_fmt_usd(run.cost):>11}{_fmt_usd(run.saved):>11}  {when}"
        )
    return lines


# ── session reports ───────────────────────────────────────────────────────────────────


def report_text(report: Any) -> list[str]:
    """A session report, styled for the terminal."""
    from lemoncrow.infra.runtime.session_report import report_lines, report_title

    name, detail = report_title(report)
    return style.tagged([("title", f"{name}\t{detail}"), ("", ""), *report_lines(report)])


# ── lc session ────────────────────────────────────────────────────────────────────────


def _day(moment: datetime | None, today: datetime) -> str:
    if moment is None:
        return "Older"
    days = (today.date() - moment.astimezone().date()).days
    if days <= 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return "This week" if days < 7 else "Older"


class SessionSource:
    """``lc session``: recent agent sessions, newest first; Enter opens one's report."""

    title = "Sessions"

    def __init__(self, root: Path, runs: list[Run], passed_over: int) -> None:
        self.root = root
        self.runs = runs
        self.passed_over = passed_over
        self.short = short_ids(runs)

    def home(self) -> Page:
        return Page("", lambda _query: self._rows(self.runs, grouped=True), empty="No sessions yet.")

    def search(self) -> Page:
        return Page("Search", self._search_rows, searchable=True, empty="No session matches that.")

    def _rows(self, runs: Sequence[Run], *, grouped: bool) -> list[Row]:
        rows: list[Row] = []
        now = datetime.now(UTC)
        last = ""
        money = max((len(_fmt_usd(run.cost)) for run in runs), default=0)
        days: dict[str, list[Run]] = {}
        for run in runs:
            days.setdefault(_day(run.at, now), []).append(run)
        for run in runs:
            day = _day(run.at, now)
            if grouped and day != last:
                same = days[day]
                label = f"{day} · {style.plural(len(same), 'run')} · {_fmt_usd(sum(r.cost for r in same))}"
                rows += [GAP, heading(label)] if rows else [heading(label)]
                last = day
            facts = ["failed" if run.failed else age(run.at, now), self.short[run.session_id], run.agent]
            rows.append(
                Row(
                    run.task or "(no task)",
                    _fmt_usd(run.cost).rjust(money),
                    " · ".join(f for f in facts if f),
                    ref=("run", run),
                )
            )
        if grouped and self.passed_over:
            rows += [GAP, note(f"{style.plural(self.passed_over, 'run')} with no recorded usage hidden")]
        return rows

    def _search_rows(self, query: str) -> list[Row]:
        words = query.lower().split()
        if not words:
            return [note("Type part of a task, an agent or a session id.")]
        found = [
            run
            for run in self.runs
            if all(word in f"{run.task} {run.agent} {run.session_id}".lower() for word in words)
        ]
        return self._rows(found, grouped=False)

    def open(self, page: Page, row: Row) -> Outcome | None:
        if row.ref[0] != "run":
            return None
        run: Run = row.ref[1]
        return Outcome(
            page=Page(self.short[run.session_id], lambda _query: self._report_rows(run), text=True, ref=row.ref)
        )

    def _report_rows(self, run: Run) -> list[Row]:
        from lemoncrow.infra.runtime.session_report import load_report, report_lines, report_title

        report = load_report(run.session_id, self.root, include_carry_credit=True)
        if report is None:
            return [Row("This session's record is gone.", style="class:dim")]
        name, detail = report_title(report)
        measure = max(style.terminal_width(100) - 6, 40)
        rows = [Row(line, style="class:title") for line in textwrap.wrap(run.task, measure)]
        rows += [Row(f"{name} · {detail}", style="class:dim"), Row("")]
        rows += [Row(text.removeprefix("  "), style=_TAG_STYLE.get(tag, "")) for tag, text in report_lines(report)]
        return rows

    def submit(self, edit: Any, text: str) -> Outcome:
        return Outcome()

    def describe(self, page: Page, row: Row | None, width: int) -> list[Line]:
        if row is None or row.ref[0] != "run":
            return []
        run: Run = row.ref[1]
        return describe_lines(run.task, [fact for fact in (run.session_id, run.agent, run.status) if fact], width)


__all__ = ["Run", "SessionSource", "age", "duration", "overview_lines", "recent_runs", "report_text"]
