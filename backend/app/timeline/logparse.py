"""Log parsing: timestamps, error lines and multi-line stack traces (Python, Java/JVM, Go, Node.js).

Repeated messages are grouped (numbers, ids and hex normalised) so the timeline shows "x41" instead of 41 rows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

_TS = re.compile(
    r"^\[?(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\]?\s*")
_ERROR = re.compile(
    r"(\b(ERROR|FATAL|CRITICAL|CRIT|SEVERE|PANIC)\b|^panic:|Traceback \(most recent call last\)|"
    r"\b\w+(Exception|Error)\b[:\s]|level=(error|fatal|panic)|\"level\"\s*:\s*\"(error|fatal|panic)\"|"
    r"severity=(error|critical)|\bE\d{4} \d{2}:\d{2}:\d{2})", re.I)
_FATAL = re.compile(r"(\b(FATAL|CRITICAL|PANIC)\b|^panic:|Traceback|level=(fatal|panic)|\"level\"\s*:\s*\"(fatal|panic)\")",
                    re.I)
_NORMALISE = re.compile(r"0x[0-9a-f]+|\b[0-9a-f]{8,}\b|\b\d+(\.\d+)*\b|'[^']*'|\"[^\"]*\"", re.I)
_GO_FRAME = re.compile(r"^(goroutine \d+ \[.*\]:|created by .*|[\w./*()\[\]-]+\(.*\)|\t.*)$")
_JVM_EXC = re.compile(r"^(Caused by: )?[\w.$]+(Exception|Error|Throwable)\b")
_PY_EXC = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception|Exit|Interrupt|Warning)\b")


@dataclass
class LogLine:
    ts: datetime | None
    text: str


@dataclass
class LogFinding:
    first_ts: datetime | None
    last_ts: datetime | None
    level: str                 # error | fatal
    message: str
    trace: list[str] = field(default_factory=list)
    count: int = 1

    @property
    def evidence(self) -> str:
        return "\n".join(self.trace) if self.trace else self.message


def parse_ts(raw: str) -> datetime | None:
    raw = raw.replace(",", ".").replace(" ", "T", 1)
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    m = re.match(r"(.*[+-]\d{2})(\d{2})$", raw)  # +0400 -> +04:00
    if m and ":" not in raw[-5:]:
        raw = f"{m.group(1)}:{m.group(2)}"
    if "." in raw:  # Python only accepts up to 6 fractional digits
        head, _, tail = raw.partition(".")
        digits = re.match(r"\d+", tail)
        if digits:
            raw = f"{head}.{digits.group(0)[:6]}{tail[digits.end():]}"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def split_lines(text: str, default_ts: datetime | None = None) -> list[LogLine]:
    out = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        m = _TS.match(line)
        if m:
            ts = parse_ts(m.group("ts"))
            out.append(LogLine(ts or default_ts, line[m.end():] if ts else line))
        else:
            out.append(LogLine(None, line))
    # continuation lines (stack frames) inherit the timestamp of the line they belong to
    last = default_ts
    for ln in out:
        if ln.ts is None:
            ln.ts = last
        else:
            last = ln.ts
    return out


def _is_frame(text: str) -> bool:
    s = text.rstrip()
    return (text[:1] in (" ", "\t") or s.startswith("at ") or s.startswith("Caused by:")
            or s.startswith("goroutine ") or s.startswith("...") or s.startswith("File \""))


def _collect_trace(lines: list[LogLine], i: int) -> tuple[list[str], int]:
    """Collect a stack trace starting at lines[i]. Returns (trace lines, index after the trace)."""
    head = lines[i].text
    trace = [head]
    j = i + 1
    if "Traceback (most recent call last)" in head:
        while j < len(lines) and len(trace) < 60:
            t = lines[j].text
            trace.append(t)
            j += 1
            if not _is_frame(t) and _PY_EXC.match(t.strip()):
                # exception line; keep wrapped continuation lines of the message
                while j < len(lines) and lines[j].text[:1] in (" ", "\t") and len(trace) < 60:
                    trace.append(lines[j].text)
                    j += 1
                break
        return trace, j
    if head.lstrip().startswith("panic:"):
        while j < len(lines) and len(trace) < 40 and (_is_frame(lines[j].text) or _GO_FRAME.match(lines[j].text)):
            trace.append(lines[j].text)
            j += 1
        return trace, j
    # "ERROR request failed" followed by "java.lang.XException: …" and its frames (logback / log4j style)
    if (j + 1 < len(lines) and _JVM_EXC.match(lines[j].text.strip()) and _is_frame(lines[j + 1].text)):
        trace.append(lines[j].text)
        j += 1
    while j < len(lines) and len(trace) < 40 and _is_frame(lines[j].text):
        trace.append(lines[j].text)
        j += 1
    return (trace if len(trace) > 1 else []), j


def normalise(message: str) -> str:
    return _NORMALISE.sub("#", message.strip())[:160]


def find_errors(text: str, default_ts: datetime | None = None, max_findings: int = 12) -> list[LogFinding]:
    """Error and fatal lines with their stack traces, grouped by normalised message (first occurrence kept)."""
    lines = split_lines(text, default_ts)
    groups: dict[str, LogFinding] = {}
    i = 0
    while i < len(lines):
        ln = lines[i]
        if not _ERROR.search(ln.text):
            i += 1
            continue
        trace, nxt = _collect_trace(lines, i)
        # For Python tracebacks the meaningful message is the final exception line
        if trace and "Traceback" in trace[0]:
            exc = next((t.strip() for t in reversed(trace) if _PY_EXC.match(t.strip())), trace[-1].strip())
            message = exc
        else:
            message = ln.text.strip()
        level = "fatal" if (_FATAL.search(ln.text) or trace) else "error"
        key = normalise(message)
        g = groups.get(key)
        if g is None:
            groups[key] = LogFinding(ln.ts, ln.ts, level, message[:400], trace[:40])
        else:
            g.count += 1
            g.last_ts = ln.ts or g.last_ts
            if level == "fatal":
                g.level = "fatal"
            if trace and not g.trace:
                g.trace = trace[:40]
        i = max(nxt, i + 1)
    findings = sorted(groups.values(), key=lambda f: (f.first_ts or datetime.max.replace(tzinfo=timezone.utc)))
    # keep fatal ones first when trimming
    if len(findings) > max_findings:
        fatal = [f for f in findings if f.level == "fatal"]
        rest = [f for f in findings if f.level != "fatal"]
        findings = sorted((fatal + rest)[:max_findings], key=lambda f: f.first_ts or datetime.max.replace(tzinfo=timezone.utc))
    return findings
