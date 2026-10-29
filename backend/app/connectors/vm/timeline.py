"""Turn VM diagnostics (journal warnings, disk usage) into infra-layer timeline events."""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timezone

from app.timeline.logparse import normalise, parse_ts
from app.timeline.model import TimelineEvent

_JOURNAL = re.compile(r"^(?P<ts>\S+)\s+(?P<host>\S+)\s+(?P<unit>[^:\[]+)(?:\[\d+\])?:\s*(?P<msg>.*)$")
_CRITICAL = re.compile(r"out of memory|oom[-_ ]kill|killed process|i/o error|no space left|read-only file system|"
                       r"segfault|kernel panic|hung task|nmi|ext4-fs error|xfs.*error|eviction manager|"
                       r"disk pressure|filesystem.*full|link is down|nic link", re.I)


def journal_events(stdout: str, host: str, entity: str, integration: str) -> list[TimelineEvent]:
    groups: OrderedDict[str, TimelineEvent] = OrderedDict()
    for line in stdout.splitlines():
        m = _JOURNAL.match(line.strip())
        if not m:
            continue
        ts = parse_ts(m.group("ts"))
        if ts is None:
            continue
        unit, msg = m.group("unit").strip(), m.group("msg").strip()
        key = f"{unit}|{normalise(msg)}"
        ev = groups.get(key)
        if ev is None:
            groups[key] = TimelineEvent(
                ts=ts, last_ts=ts, layer="infra", severity="critical" if _CRITICAL.search(msg) else "warning",
                title=f"{host} {unit}: {msg[:150]}", entity=entity, source=f"VM {host} · journal",
                integration=integration, kind="journal", evidence=line.strip(),
                attrs={"norm": key[:120]})
        else:
            ev.count += 1
            ev.last_ts = ts
            if ev.count <= 6:
                ev.evidence += "\n" + line.strip()
    return list(groups.values())[:40]


def disk_events(stdout: str, host: str, entity: str, integration: str, threshold: int = 85) -> list[TimelineEvent]:
    out = []
    for line in stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6 or not parts[4].endswith("%"):
            continue
        used = int(parts[4].rstrip("%") or 0)
        if used >= threshold:
            out.append(TimelineEvent(
                ts=datetime.now(timezone.utc), estimated=True, layer="infra",
                severity="critical" if used >= 90 else "warning",
                title=f"{host}: filesystem {parts[5]} at {used}% (now)", entity=entity, source=f"VM {host} · df",
                integration=integration, kind="disk_now", evidence=f"$ df -P\n{stdout.splitlines()[0]}\n{line}",
                attrs={"ongoing": True, "norm": f"disk|{parts[5]}"}))
    return out
