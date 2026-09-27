import subprocess

from backend.realtime_sync_supervisor import RealtimeSyncSupervisor


class FakeProcess:
    def __init__(self, pid=1234, code=None, fail_wait=False):
        self.pid = pid
        self.code = code
        self.fail_wait = fail_wait
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.fail_wait:
            raise RuntimeError("synthetic stop failure")
        if self.code is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.code

    def terminate(self):
        self.terminated = True
        self.code = 0

    def kill(self):
        self.killed = True
        self.code = -9


def test_reaper_preserves_worker_kind(tmp_path):
    processes = iter([
        FakeProcess(1001, 0),
        FakeProcess(1002, 7),
    ])
    supervisor = RealtimeSyncSupervisor(
        tmp_path / "test.sqlite3",
        popen=lambda *_args, **_kwargs: next(processes))
    supervisor.launch_pairing("PAIR-ABC")
    supervisor.launch_persistent("LSYNC-XYZ")
    assert supervisor.reap_finished() == [
        {"identifier": "PAIR-ABC", "kind": "pairing", "exit_code": 0},
        {"identifier": "LSYNC-XYZ", "kind": "persistent", "exit_code": 7},
    ]
    assert supervisor.workers == {}


def test_failed_stop_retains_handle_for_reaper_or_cleanup(tmp_path):
    process = FakeProcess(fail_wait=True)
    supervisor = RealtimeSyncSupervisor(
        tmp_path / "test.sqlite3",
        popen=lambda *_args, **_kwargs: process)
    supervisor.launch_persistent("LSYNC-ABC")
    assert supervisor.stop("LSYNC-ABC") is False
    assert "LSYNC-ABC" in supervisor.workers
    process.fail_wait = False
    process.code = 0
    assert supervisor.stop("LSYNC-ABC") is True
    assert "LSYNC-ABC" not in supervisor.workers
