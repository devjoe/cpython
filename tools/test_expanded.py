"""Meaningful false-positive guards for observational classifications."""
import copy
import importlib.util
from pathlib import Path
import signal
import unittest

spec = importlib.util.spec_from_file_location("expanded", Path(__file__).with_name("expanded_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def example():
    cfg = dict(kind="core", child_pid=7, command="continue", policy="default")
    state = dict(work_id=10, token_id=20, outer_id=30, work_finally=False,
                 work_after=False, natural_timeout=False, partial_executed=False)
    data = [dict(event="work_ready"),
            dict(event="snapshot", label="interrupted", live_work=True, live_outer=True,
                 active=[dict(id=10,name="work")], state=state.copy(), postmortem=False),
            dict(event="work_completed", same_frame=True, same_token=True, state=state.copy()),
            dict(event="outer_completed", result=42, state=state.copy())]
    ack = [dict(event="posix_signal_ack", pid=7, signal=signal.SIGINT)]
    return cfg, data, ack


class ContractGuards(unittest.TestCase):
    def test_original_frame_is_observed_not_universal_bug_verdict(self):
        cfg, data, ack = example()
        result = probe.classify(cfg,data,ack)
        self.assertEqual(result["observation"],"LIVE_ORIGINAL_FRAME_RESUMED")
        self.assertEqual(result["contract"],"observation")

    def test_no_ack_is_infrastructure(self):
        cfg,data,_ = example()
        self.assertEqual(probe.classify(cfg,data,[])["contract"],"infrastructure")

    def test_wrong_pid_ack_is_not_receipt(self):
        cfg,data,ack=example()
        ack[0]["pid"]=99
        self.assertFalse(probe.classify(cfg,data,ack)["receipt"])

    def test_sigbreak_type_cannot_substitute(self):
        cfg,data,_=example()
        ack=[dict(event="native_ctrl_c_ack",pid=7,control_type=1)]
        self.assertFalse(probe.classify(cfg,data,ack)["receipt"])

    def test_traceback_frame_is_not_active_frame(self):
        cfg,data,ack=example()
        data[1]["active"]=[]
        self.assertNotEqual(probe.classify(cfg,data,ack)["observation"],"LIVE_ORIGINAL_FRAME_RESUMED")

    def test_changed_token_not_resumed(self):
        cfg,data,ack=example()
        data[2]["state"]["token_id"]=999
        self.assertNotEqual(probe.classify(cfg,data,ack)["observation"],"LIVE_ORIGINAL_FRAME_RESUMED")

    def test_natural_deadline_not_resumption(self):
        cfg,data,ack=example()
        data[2]["state"]["natural_timeout"]=True
        self.assertNotEqual(probe.classify(cfg,data,ack)["observation"],"LIVE_ORIGINAL_FRAME_RESUMED")

    def test_second_work_call_not_resumption(self):
        cfg,data,ack=example()
        data.append(dict(event="work_ready"))
        self.assertNotEqual(probe.classify(cfg,data,ack)["observation"],"LIVE_ORIGINAL_FRAME_RESUMED")

    def test_async_cancel_not_relabelled_debugger_bug(self):
        cfg,data,ack=example()
        cfg["kind"]="asyncio"
        data[1]["live_work"]=False
        data.append(dict(event="async_cancelled"))
        result=probe.classify(cfg,data,ack)
        self.assertEqual(result["observation"],"ASYNC_TASK_CANCELLED")
        self.assertEqual(result["contract"],"observation")


if __name__ == "__main__":
    unittest.main(verbosity=2)
