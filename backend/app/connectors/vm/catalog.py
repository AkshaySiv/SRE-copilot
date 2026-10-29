"""Command catalog for VMs.

The LLM never types shell commands. It picks a catalog entry by id and supplies parameters that are
validated against a regex and shell-quoted. Admins can add their own diagnostics and runbooks per
integration; runbooks (write operations) can only run as approved remediations.
"""
from __future__ import annotations

import re
import shlex
from typing import Literal

from pydantic import BaseModel, Field


class CommandParam(BaseModel):
    name: str
    description: str = ""
    pattern: str = Field(r"^[A-Za-z0-9._@:/-]{1,128}$", description="Regex the value must fully match")
    default: str | None = None


class CommandSpec(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9_]{2,48}$")
    description: str
    command: str = Field(description="Shell template; params referenced as {name} are shell-quoted")
    params: list[CommandParam] = Field(default_factory=list)
    timeout_seconds: int = 30
    mode: Literal["read", "write"] = "read"
    use_sudo: bool = False

    def render(self, args: dict[str, str] | None) -> str:
        args = dict(args or {})
        values: dict[str, str] = {}
        for p in self.params:
            raw = args.pop(p.name, None)
            raw = p.default if raw in (None, "") else str(raw)
            if raw is None:
                raise ValueError(f"Missing parameter '{p.name}' for '{self.id}'")
            if not re.fullmatch(p.pattern, raw):
                raise ValueError(f"Parameter '{p.name}'={raw!r} does not match {p.pattern}")
            values[p.name] = shlex.quote(raw)
        if args:
            raise ValueError(f"Unknown parameters for '{self.id}': {', '.join(args)}")
        cmd = self.command.format(**values)
        return f"sudo -n {cmd}" if self.use_sudo else cmd


_UNIT = CommandParam(name="unit", description="systemd unit, e.g. nginx or kubelet", pattern=r"^[A-Za-z0-9@._-]{1,96}$")
_LINES = CommandParam(name="lines", description="number of lines", pattern=r"^[0-9]{1,4}$", default="200")

BUILTIN_DIAGNOSTICS: list[CommandSpec] = [
    CommandSpec(id="system_overview", description="Uptime, load, memory, disk and CPU count",
                command="hostname; uptime; echo; free -m; echo; df -hP -x tmpfs -x devtmpfs; echo; nproc"),
    CommandSpec(id="top_processes", description="Top processes by CPU and memory",
                command="ps -eo pid,user,%cpu,%mem,rss,etime,args --sort=-%cpu | head -n 20; echo; "
                        "ps -eo pid,user,%cpu,%mem,rss,etime,args --sort=-%mem | head -n 15"),
    CommandSpec(id="memory_detail", description="Memory, swap and recent OOM kills",
                command="free -m; echo; vmstat 1 3; echo; (dmesg -T 2>/dev/null || journalctl -k --no-pager -n 300) "
                        "| grep -iE 'oom|out of memory|killed process' | tail -n 20"),
    CommandSpec(id="disk_usage", description="Filesystem space and inode usage",
                command="df -hP -x tmpfs -x devtmpfs; echo; df -iP -x tmpfs -x devtmpfs"),
    CommandSpec(id="largest_dirs", description="Largest sub-directories of a path (same filesystem)",
                command="du -xh --max-depth=1 {path} 2>/dev/null | sort -rh | head -n 20",
                params=[CommandParam(name="path", description="absolute path", pattern=r"^/[A-Za-z0-9._/-]{0,200}$",
                                     default="/var")], timeout_seconds=60),
    CommandSpec(id="failed_units", description="systemd units in failed state", command="systemctl --failed --no-pager"),
    CommandSpec(id="service_status", description="Status of a systemd service",
                command="systemctl status {unit} --no-pager -l | head -n 60", params=[_UNIT]),
    CommandSpec(id="service_logs", description="Recent journal logs of a systemd service",
                command="journalctl -u {unit} -n {lines} --no-pager -o short-iso", params=[_UNIT, _LINES]),
    CommandSpec(id="kernel_messages", description="Recent kernel messages (hardware, OOM, disk, network errors)",
                command="(dmesg -T 2>/dev/null || journalctl -k --no-pager -o short-iso) | tail -n {lines}",
                params=[_LINES]),
    CommandSpec(id="listening_ports", description="Listening TCP/UDP sockets and owning processes",
                command="ss -tulpn 2>/dev/null | head -n 80"),
    CommandSpec(id="tcp_check", description="Check TCP connectivity from this host to host:port",
                command="timeout 5 bash -c \"</dev/tcp/$0/$1\" {host} {port} && echo OPEN || echo 'CLOSED/UNREACHABLE'",
                params=[CommandParam(name="host", pattern=r"^[A-Za-z0-9.-]{1,253}$"),
                        CommandParam(name="port", pattern=r"^[0-9]{1,5}$")]),
    CommandSpec(id="dns_lookup", description="Resolve a hostname from this host",
                command="getent hosts {host} || echo 'NOT RESOLVED'",
                params=[CommandParam(name="host", pattern=r"^[A-Za-z0-9.-]{1,253}$")]),
    CommandSpec(id="log_tail", description="Tail a log file under /var/log",
                command="tail -n {lines} {path}",
                params=[CommandParam(name="path", pattern=r"^/var/log/[A-Za-z0-9._/-]{1,200}$"), _LINES]),
    CommandSpec(id="log_grep", description="Search a log file under /var/log for a pattern (last matches)",
                command="grep -iE {pattern} {path} | tail -n 100",
                params=[CommandParam(name="path", pattern=r"^/var/log/[A-Za-z0-9._/-]{1,200}$"),
                        CommandParam(name="pattern", pattern=r"^[A-Za-z0-9 ._:|()-]{1,100}$")]),
    CommandSpec(id="containers", description="Running containers (containerd/crictl or docker)",
                command="(crictl ps -a 2>/dev/null || docker ps -a 2>/dev/null) | head -n 60"),
    CommandSpec(id="time_sync", description="Clock and NTP sync status (certificate / token errors)",
                command="date -u; timedatectl 2>/dev/null | head -n 12"),
    CommandSpec(id="cert_expiry", description="Expiry date of a certificate file",
                command="openssl x509 -noout -subject -enddate -in {path}",
                params=[CommandParam(name="path", pattern=r"^/[A-Za-z0-9._/-]{1,200}\.(crt|pem|cert)$")]),
]

# Used by the time-based investigation (not offered to the agent as a free tool).
JOURNAL_WINDOW = CommandSpec(
    id="journal_window", description="Warning-and-above journal entries in a time window",
    command="journalctl --since @{since} --until @{until} -p warning --no-pager -o short-iso-precise -n 400 2>/dev/null"
            " || journalctl --since @{since} --until @{until} -p warning --no-pager -o short-iso -n 400",
    params=[CommandParam(name="since", pattern=r"^[0-9]{9,11}$"), CommandParam(name="until", pattern=r"^[0-9]{9,11}$")],
    timeout_seconds=45)
DISK_NOW = CommandSpec(id="disk_now", description="Filesystem usage", command="df -P -x tmpfs -x devtmpfs -x overlay")

BUILTIN_RUNBOOKS: list[CommandSpec] = [
    CommandSpec(id="restart_service", description="Restart a systemd service", mode="write", use_sudo=True,
                command="systemctl restart {unit} && systemctl is-active {unit}", params=[_UNIT], timeout_seconds=90),
    CommandSpec(id="vacuum_journal", description="Shrink systemd journal to 500M to free disk space", mode="write",
                use_sudo=True, command="journalctl --vacuum-size=500M"),
]
