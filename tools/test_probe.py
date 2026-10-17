"""Focused negative checks for the probe's resumption verdict, not OS emulation."""

import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("probe", Path(__file__).with_name("pdb_ctrlc_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def successful_rows():
    state = dict(work_id=101, token_id=202, outer_id=303, work_finally=False,
                 work_after=False, natural_timeout=False)
    return [
        dict(event="work_ready"),
        dict(event="native_ctrl_c_ack", control_type=0),
        dict(event="snapshot", label="interrupted", state=state.copy(),
             live_work=True, exception="None"),
        dict(event="work_completed", same_frame=True, same_token=True, state=state.copy()),
        dict(event="outer_completed", same_frame=True, result=42, state=state.copy()),
        dict(event="runcall_finished", result=42),
    ]


class VerdictTests(unittest.TestCase):
    def test_live_original_frame_can_pass(self):
        self.assertEqual(probe.classify(successful_rows(), "windows")[0], "PASS_RESUMABLE")

    def test_api_success_without_receiver_ack_cannot_pass(self):
        rows = [row for row in successful_rows() if row["event"] != "native_ctrl_c_ack"]
        rows.append(dict(event="signal_api_return", success=True))
        self.assertEqual(probe.classify(rows, "windows")[0], "INFRASTRUCTURE_LIMITATION")

    def test_ctrl_break_cannot_substitute_for_ctrl_c(self):
        rows = successful_rows()
        rows[1]["control_type"] = 1
        self.assertEqual(probe.classify(rows, "windows")[0], "INFRASTRUCTURE_LIMITATION")

    def test_prompt_without_snapshot_cannot_pass(self):
        rows = [row for row in successful_rows() if row["event"] != "snapshot"]
        self.assertEqual(probe.classify(rows, "windows")[0], "INCONCLUSIVE")

    def test_traceback_only_frame_cannot_pass(self):
        rows = successful_rows()
        rows[2]["live_work"] = False
        rows[2]["state"]["work_finally"] = True
        rows.append(dict(event="escaped_keyboard_interrupt"))
        self.assertEqual(probe.classify(rows, "windows")[0], "BUG_REPRODUCED_UNWIND")

    def test_new_frame_cannot_pass(self):
        rows = successful_rows()
        rows[3]["state"]["work_id"] = 999
        self.assertNotEqual(probe.classify(rows, "windows")[0], "PASS_RESUMABLE")

    def test_new_token_cannot_pass(self):
        rows = successful_rows()
        rows[3]["state"]["token_id"] = 999
        self.assertNotEqual(probe.classify(rows, "windows")[0], "PASS_RESUMABLE")

    def test_restarted_work_cannot_pass(self):
        rows = successful_rows() + [dict(event="work_ready")]
        self.assertNotEqual(probe.classify(rows, "windows")[0], "PASS_RESUMABLE")

    def test_busy_loop_natural_deadline_cannot_pass(self):
        rows = successful_rows()
        rows[3]["state"]["natural_timeout"] = True
        self.assertNotEqual(probe.classify(rows, "windows")[0], "PASS_RESUMABLE")

    def test_no_receipt_evidence_on_posix_cannot_pass(self):
        rows = [row for row in successful_rows() if row["event"] != "native_ctrl_c_ack"]
        self.assertNotEqual(probe.classify(rows, "posix")[0], "PASS_RESUMABLE")

    def test_no_completed_outer_cannot_pass(self):
        rows = [row for row in successful_rows() if row["event"] != "outer_completed"]
        self.assertNotEqual(probe.classify(rows, "windows")[0], "PASS_RESUMABLE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
