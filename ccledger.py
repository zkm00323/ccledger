#!/usr/bin/env python3
"""ccledger - account for what Claude Code actually consumed, from your own transcripts.

Reads the JSONL session transcripts Claude Code already writes under
``~/.claude/projects`` and aggregates real token usage by project, model, day,
git branch, session, main-agent-vs-subagent, or **individual subagent**.

Design notes that matter:

* **Token-first.** Token counts come straight out of the transcripts, so they are
  exact. Dollar figures are *not* built in, because baking a price table into a
  tool guarantees it is wrong the week after a price change. Supply your own with
  ``--prices`` and costs get added as extra columns.
* **Subagents are counted, and named.** Records with ``isSidechain: true`` are work
  done by subagents/Task calls. Top-level usage reporting misses them entirely, and
  on agent-heavy sessions they are routinely the majority of consumption.
  ``--by subagent`` goes one level further and splits that half apart *per agent
  type*, so "which of my agents is eating the budget" is a question with an answer.
* **Cache tiers are kept apart.** 5-minute and 1-hour cache writes are billed
  differently and are reported as separate columns rather than merged.
* **Unattributable usage says so.** Resuming or forking a session replays earlier
  responses into a new transcript, and the replayed copy can carry a different
  project, session or branch than the original. Where the copies disagree there is
  no fact of the matter left on disk, so those tokens are reported under
  ``(ambiguous)`` instead of being silently assigned to whichever copy happened to
  be read first.

Standard library only. Python 3.9+.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import glob
import json
import os
import sys
from collections import defaultdict

__version__ = "0.4.0"

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")

# Token buckets, in report column order.
BUCKETS = ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")

GROUP_KEYS = ("project", "model", "day", "branch", "session", "agent", "subagent")

MAIN = "main"

# Bucket for usage whose grouping field the transcripts disagree about.
AMBIGUOUS = "(ambiguous)"


class Record:
    """One billable API response pulled out of a transcript.

    ``ambiguous`` names the group keys whose value differed between replayed
    copies of this same response. It is filled in by :func:`dedupe`, which is the
    only place that sees all the copies.
    """

    __slots__ = ("project", "model", "day", "branch", "session", "agent",
                 "subagent", "tokens", "key", "final", "ambiguous")

    def __init__(self, project, model, day, branch, session, agent, subagent,
                 tokens, key, final=True, ambiguous=frozenset()):
        self.project = project
        self.model = model
        self.day = day
        self.branch = branch
        self.session = session
        self.agent = agent
        self.subagent = subagent
        self.tokens = tokens
        self.key = key
        self.final = final
        self.ambiguous = ambiguous


def _iso_day(timestamp):
    """'2026-08-20T13:56:05.636Z' -> '2026-08-20'. Returns '' if unparseable."""
    if not isinstance(timestamp, str) or len(timestamp) < 10:
        return ""
    day = timestamp[:10]
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        return ""
    return day


def _project_name(entry, path, root):
    """Prefer the real cwd; fall back to the mangled directory name."""
    cwd = entry.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        return os.path.basename(os.path.normpath(cwd)) or cwd
    rel = os.path.relpath(path, root)
    return rel.split(os.sep)[0]


def _extract_tokens(usage):
    """Map a transcript ``usage`` object onto our buckets.

    Cache creation may be reported either as a flat ``cache_creation_input_tokens``
    or split per TTL under ``cache_creation``. When the split is present it wins,
    because it is the only way to tell the two price tiers apart. When it is not,
    the flat total is attributed to the 5m bucket, which is the default TTL.
    """
    tokens = dict.fromkeys(BUCKETS, 0)
    tokens["input"] = int(usage.get("input_tokens") or 0)
    tokens["output"] = int(usage.get("output_tokens") or 0)
    tokens["cache_read"] = int(usage.get("cache_read_input_tokens") or 0)

    split = usage.get("cache_creation")
    wrote_split = False
    if isinstance(split, dict):
        five = int(split.get("ephemeral_5m_input_tokens") or 0)
        hour = int(split.get("ephemeral_1h_input_tokens") or 0)
        if five or hour:
            tokens["cache_write_5m"] = five
            tokens["cache_write_1h"] = hour
            wrote_split = True
    if not wrote_split:
        tokens["cache_write_5m"] = int(usage.get("cache_creation_input_tokens") or 0)
    return tokens


def _subagent_name(entry):
    """Which subagent produced this record — or 'main' for the main loop.

    Sidechain records carry ``attributionAgent``, the *type* of the subagent
    ('Explore', 'general-purpose', a custom one from .claude/agents/). That is the
    label worth grouping on: two invocations of the same agent type belong in the
    same row. ``agentId`` is unique per invocation and is only a fallback for the
    handful of records written before the type was known.
    """
    if not entry.get("isSidechain"):
        return MAIN
    name = entry.get("attributionAgent")
    if isinstance(name, str) and name.strip():
        return name.strip()
    agent_id = entry.get("agentId")
    if isinstance(agent_id, str) and agent_id.strip():
        return "agent:" + agent_id.strip()
    return "subagent"


def parse_entry(entry, path, root):
    """Turn one decoded JSONL line into a Record, or None if it is not billable."""
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    model = message.get("model") or "unknown"
    if model.startswith("<"):  # '<synthetic>' entries are locally generated, not billed
        return None

    tokens = _extract_tokens(usage)
    if not any(tokens.values()):
        return None

    # Dedup key: a resumed or forked session replays earlier lines verbatim, so the
    # same API response can appear in more than one file. message.id is stable
    # per response; requestId and uuid are progressively weaker fallbacks.
    key = message.get("id") or entry.get("requestId") or entry.get("uuid")

    return Record(
        project=_project_name(entry, path, root),
        model=model,
        day=_iso_day(entry.get("timestamp")),
        branch=entry.get("gitBranch") or "",
        session=entry.get("sessionId") or "",
        agent="subagent" if entry.get("isSidechain") else MAIN,
        subagent=_subagent_name(entry),
        tokens=tokens,
        key=key,
        # A record written mid-stream carries stop_reason=None and a snapshot
        # usage (output_tokens is often 1). The real numbers only land on the
        # record written at request completion. Main-session files backfill that
        # final usage onto every record of the request; subagent files sometimes
        # never do, and then the true output count is simply not in the
        # transcript. See anthropics/claude-code#84223.
        final=message.get("stop_reason") is not None,
    )


def load_records(root, on_error=None):
    """Yield Records from every transcript under *root*, skipping unreadable lines."""
    pattern = os.path.join(root, "**", "*.jsonl")
    for path in sorted(glob.glob(pattern, recursive=True)):
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError as exc:
            if on_error:
                on_error(path, exc)
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue  # a partially flushed final line is normal on a live session
                if not isinstance(entry, dict):
                    continue
                record = parse_entry(entry, path, root)
                if record is not None:
                    yield record


def _better(new, old):
    """Which of two records for the same request should survive dedup?

    First-wins is wrong here. The same request shows up as a mid-stream snapshot
    and again as the finalized record, and the snapshot comes first in the file.
    Prefer whichever one carries the final usage; between two of the same kind,
    prefer the larger output count.

    Note what this deliberately does *not* decide: a verbatim replay ties on both
    tests, so the survivor is whichever copy was read first, which is an artefact
    of filename order. That is fine for token counts, which are identical, and not
    fine for attribution -- see :func:`dedupe`.
    """
    if new.final != old.final:
        return new.final
    return new.tokens["output"] > old.tokens["output"]


def dedupe(records):
    """Collapse records that describe the same API response, keeping the best one.

    A resumed or forked session replays earlier lines verbatim, so one response
    can appear in several files; and within a file it appears once per streamed
    content block. Insertion order is preserved so the report stays stable.

    The copies are not always identical. A replayed line is rewritten with the
    resuming session's own ``sessionId``, and a session resumed from a different
    directory or branch carries those too, so the same response can legitimately
    be found filed under two projects. Whichever copy wins the tie-break above
    then decides the attribution, and it wins for no better reason than sorting
    first. Rather than let that stand as an answer, every group key whose value
    differs across the copies is recorded on the surviving record so the report
    can show those tokens as ``(ambiguous)``.
    """
    best = {}
    order = []
    loose = []
    variants = {}
    for record in records:
        if record.key is None:
            loose.append(record)
            continue
        current = best.get(record.key)
        if current is None:
            best[record.key] = record
            order.append(record.key)
            variants[record.key] = dict((key, {getattr(record, key)})
                                        for key in GROUP_KEYS)
        else:
            seen = variants[record.key]
            for key in GROUP_KEYS:
                seen[key].add(getattr(record, key))
            if _better(record, current):
                best[record.key] = record
    for key in order:
        record = best[key]
        record.ambiguous = frozenset(name for name, values in variants[key].items()
                                     if len(values) > 1)
        yield record
    for record in loose:
        yield record


def group_value(record, group_by):
    """The row a record belongs to, or ``(ambiguous)`` if the transcripts disagree."""
    if group_by in record.ambiguous:
        return AMBIGUOUS
    return getattr(record, group_by) or "(none)"


def usage_health(records):
    """Count subagent requests whose true output tokens are missing from disk.

    Returned as ``(subagent_calls, unfinalized)``. Anything above zero means the
    subagent numbers in the report are a floor, not a measurement.
    """
    total = 0
    unfinalized = 0
    for record in records:
        if record.agent != "subagent":
            continue
        total += 1
        if not record.final:
            unfinalized += 1
    return total, unfinalized


def attribution_health(records, group_by):
    """Count usage the transcripts cannot place under a single *group_by* value.

    Returned as ``(calls, tokens)``. Grouping by day is always zero: replayed
    lines keep their original timestamp. Grouping by project, session or branch
    is not, because those are rewritten to match the session doing the replay.
    """
    calls = 0
    tokens = 0
    for record in records:
        if group_by in record.ambiguous:
            calls += 1
            tokens += sum(record.tokens.values())
    return calls, tokens


def filter_records(records, since=None, until=None, project=None, agent=None,
                   subagent=None):
    for record in dedupe(records):
        if since and (not record.day or record.day < since):
            continue
        if until and (not record.day or record.day > until):
            continue
        if project and project.lower() not in record.project.lower():
            continue
        if agent and record.agent != agent:
            continue
        if subagent and subagent.lower() not in record.subagent.lower():
            continue
        yield record


def aggregate(records, group_by):
    """Sum token buckets into an ordered {group_value: {bucket: n}} mapping."""
    totals = defaultdict(lambda: dict.fromkeys(BUCKETS, 0))
    calls = defaultdict(int)
    for record in records:
        value = group_value(record, group_by)
        bucket = totals[value]
        for name, count in record.tokens.items():
            bucket[name] += count
        calls[value] += 1
    return totals, calls


def load_prices(path):
    """Load a price table: {"model-id": {"input": <usd per Mtok>, "output": ...}}.

    Only ``input`` and ``output`` are required per model. Cache tiers are derived
    from the input price using the published multipliers, which are ratios rather
    than absolute prices and so survive price changes:
    5m write 1.25x, 1h write 2x, read 0.1x. Any of them can be overridden
    explicitly per model if you would rather not rely on the ratio.
    """
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("price file must be a JSON object keyed by model id")

    table = {}
    for model, entry in raw.items():
        if model.startswith("_"):
            continue  # underscore keys are comments, not models
        if not isinstance(entry, dict):
            raise ValueError("price entry for %r must be an object" % model)
        if "input" not in entry or "output" not in entry:
            raise ValueError("price entry for %r needs 'input' and 'output'" % model)
        base_in = float(entry["input"])
        table[model] = {
            "input": base_in,
            "output": float(entry["output"]),
            "cache_write_5m": float(entry.get("cache_write_5m", base_in * 1.25)),
            "cache_write_1h": float(entry.get("cache_write_1h", base_in * 2.0)),
            "cache_read": float(entry.get("cache_read", base_in * 0.1)),
        }
    return table


def price_lookup(table, model):
    """Exact match first, then longest matching prefix, so 'claude-opus-4-5-2026...'
    can be priced by a 'claude-opus-4-5' entry."""
    if model in table:
        return table[model]
    best = None
    for key in table:
        if model.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return table[best] if best else None


def cost_of(record, table):
    """USD for one record, or None when the model is absent from the price table."""
    prices = price_lookup(table, record.model)
    if prices is None:
        return None
    return sum(count * prices[name] / 1_000_000.0
               for name, count in record.tokens.items())


def _fmt_int(value):
    return "{:,}".format(value)


def render_table(rows, headers, stream):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            out.append(cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i]))
        stream.write("  ".join(out).rstrip() + "\n")

    line(headers)
    stream.write("  ".join("-" * w for w in widths) + "\n")
    for row in rows:
        line(row)


def build_report(records, group_by, table=None):
    """Return (rows, headers, totals_row) with rows sorted by total tokens desc."""
    records = list(records)
    totals, calls = aggregate(records, group_by)

    costs = None
    unpriced = set()
    if table is not None:
        costs = defaultdict(float)
        for record in records:
            amount = cost_of(record, table)
            if amount is None:
                unpriced.add(record.model)
            else:
                costs[group_value(record, group_by)] += amount

    headers = [group_by, "calls"] + list(BUCKETS) + ["total"]
    if costs is not None:
        headers.append("usd")

    ordered = sorted(totals.items(), key=lambda kv: -sum(kv[1].values()))
    rows = []
    grand = dict.fromkeys(BUCKETS, 0)
    grand_calls = 0
    grand_cost = 0.0
    for value, bucket in ordered:
        row = [value, _fmt_int(calls[value])]
        for name in BUCKETS:
            row.append(_fmt_int(bucket[name]))
            grand[name] += bucket[name]
        row.append(_fmt_int(sum(bucket.values())))
        grand_calls += calls[value]
        if costs is not None:
            row.append("%.4f" % costs[value])
            grand_cost += costs[value]
        rows.append(row)

    total_row = ["TOTAL", _fmt_int(grand_calls)]
    for name in BUCKETS:
        total_row.append(_fmt_int(grand[name]))
    total_row.append(_fmt_int(sum(grand.values())))
    if costs is not None:
        total_row.append("%.4f" % grand_cost)

    return rows, headers, total_row, sorted(unpriced)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="ccledger",
        description="Account for Claude Code token usage from local transcripts.")
    parser.add_argument("--by", choices=GROUP_KEYS, default="project",
                        help="group results by this field (default: project)")
    parser.add_argument("--root", default=DEFAULT_ROOT,
                        help="transcript root (default: ~/.claude/projects)")
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="ignore days before this")
    parser.add_argument("--until", metavar="YYYY-MM-DD", help="ignore days after this")
    parser.add_argument("--project", metavar="SUBSTR",
                        help="only projects whose name contains SUBSTR")
    parser.add_argument("--agent", choices=("main", "subagent"),
                        help="restrict to main-agent or subagent work")
    parser.add_argument("--subagent", metavar="SUBSTR",
                        help="only subagents whose name contains SUBSTR "
                             "(e.g. --subagent explore)")
    parser.add_argument("--prices", metavar="FILE",
                        help="JSON price table; adds a usd column (see prices.example.json)")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit JSON instead of a table")
    parser.add_argument("--csv", action="store_true", dest="as_csv",
                        help="emit CSV instead of a table")
    parser.add_argument("--version", action="version", version="ccledger " + __version__)
    args = parser.parse_args(argv)

    if not os.path.isdir(args.root):
        sys.stderr.write("no transcript directory at %s\n" % args.root)
        return 2

    table = None
    if args.prices:
        try:
            table = load_prices(args.prices)
        except (OSError, ValueError) as exc:
            sys.stderr.write("could not read price table: %s\n" % exc)
            return 2

    records = list(filter_records(load_records(args.root), since=args.since,
                                  until=args.until, project=args.project,
                                  agent=args.agent, subagent=args.subagent))
    rows, headers, total_row, unpriced = build_report(records, args.by, table)

    if not rows:
        sys.stderr.write("no usage records matched\n")
        return 1

    if args.as_json:
        payload = [dict(zip(headers, [r[0]] + [c.replace(",", "") for c in r[1:]]))
                   for r in rows]
        json.dump({"group_by": args.by, "rows": payload,
                   "total": dict(zip(headers, [total_row[0]]
                                     + [c.replace(",", "") for c in total_row[1:]]))},
                  sys.stdout, indent=2)
        sys.stdout.write("\n")
    elif args.as_csv:
        writer = csv.writer(sys.stdout, lineterminator="\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row[0]] + [c.replace(",", "") for c in row[1:]])
        writer.writerow([total_row[0]] + [c.replace(",", "") for c in total_row[1:]])
    else:
        render_table(rows + [total_row], headers, sys.stdout)

    if unpriced:
        sys.stderr.write("no price entry for: %s (excluded from usd)\n"
                         % ", ".join(unpriced))

    amb_calls, amb_tokens = attribution_health(records, args.by)
    if amb_calls:
        sys.stderr.write(
            "%s of %s requests (%s tokens) appear in more than one transcript with "
            "a different %s, so they are reported as %s rather than guessed at. "
            "Resuming or forking a session replays earlier responses and rewrites "
            "them with the resuming session's own ids; --by day is never affected.\n"
            % (_fmt_int(amb_calls), _fmt_int(len(records)), _fmt_int(amb_tokens),
               args.by, AMBIGUOUS))

    sub_calls, unfinalized = usage_health(records)
    if unfinalized:
        sys.stderr.write(
            "subagent totals above are a FLOOR: %s of %s subagent requests (%.1f%%) "
            "have no finalized usage on disk, so their output tokens are the "
            "mid-stream snapshot (often 1). Your real subagent share is higher. "
            "Upstream bug: anthropics/claude-code#84223\n"
            % (_fmt_int(unfinalized), _fmt_int(sub_calls),
               100.0 * unfinalized / sub_calls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
