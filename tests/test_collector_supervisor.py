from __future__ import annotations

import subprocess
import sys

from backend.collector_supervisor import CollectorSupervisor


class FakeProcess:
    pid = 12345
    def __init__(self):
        self.alive = True
        self.terminated = self.killed = False
    def poll(self): return None if self.alive else 0
    def terminate(self): self.terminated = True; self.alive = False
    def kill(self): self.killed = True; self.alive = False
    def wait(self, timeout=None): self.alive = False; return 0


def test_launch_uses_trusted_interpreter_module_and_never_shell(tmp_path):
    seen = {}
    def popen(args, **kwargs):
        seen.update(args=args, kwargs=kwargs)
        return FakeProcess()
    supervisor = CollectorSupervisor(tmp_path / "runs.sqlite3", popen)
    pid = supervisor.launch("CRUN-ABC123")
    assert pid == 12345
    assert seen["args"] == [sys.executable, "-m", "collector.collector_worker",
                            "--run-id", "CRUN-ABC123"]
    assert seen["kwargs"]["shell"] is False
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL
    assert "https://" not in " ".join(seen["args"])


def test_stop_only_targets_process_registered_for_run(tmp_path):
    processes = []
    def popen(*_args, **_kwargs):
        item = FakeProcess(); processes.append(item); return item
    supervisor = CollectorSupervisor(tmp_path / "runs.sqlite3", popen)
    supervisor.launch("CRUN-ONE")
    supervisor.launch("CRUN-TWO")
    supervisor.stop("CRUN-ONE")
    assert not processes[0].alive
    assert not processes[1].terminated
    assert supervisor.is_alive("CRUN-TWO")


def test_arbitrary_run_identifier_is_rejected_before_popen(tmp_path):
    called = False
    def popen(*_args, **_kwargs):
        nonlocal called; called = True
    supervisor = CollectorSupervisor(tmp_path / "runs.sqlite3", popen)
    try:
        supervisor.launch("CRUN-X --url https://evil.example")
    except ValueError as exc:
        assert str(exc) == "collector_run_id_invalid"
    assert not called
