from __future__ import annotations

import unittest

from learning_evidence.impact_analysis.staged_rollout import analyze, report_hash
from learning_evidence.programs.contract import ContractError, DomainRecord


def series(school, outcomes, activated=None, missing=0.05, language="standard"):
    records = []
    for period, outcome in enumerate(outcomes):
        payload = {"school_code": school, "period": period, "outcome_score": outcome,
                   "missing_rate": missing, "language_group": language}
        if activated is not None:
            payload["activated_period"] = activated
        records.append(DomainRecord(f"{school}-p{period}", payload))
    return records


class StagedRolloutTests(unittest.TestCase):
    def test_cohorts_follow_real_activation_timing(self):
        records = (series("early", [40, 41, 49, 51], activated=2)
                   + series("late", [40, 41, 43, 47], activated=3)
                   + series("never", [40, 41, 43, 44]))
        report = analyze(records)
        self.assertEqual(report["conclusion"], "supports")
        self.assertEqual(len(report["cohorts"]), 2)
        first, second = report["cohorts"]
        self.assertEqual(first["activation"], 2)
        self.assertEqual(first["treated"], ["early"])
        self.assertEqual(first["controls"], ["late", "never"])
        self.assertAlmostEqual(first["effect"], 6.75)
        self.assertEqual(second["activation"], 3)
        self.assertEqual(second["treated"], ["late"])
        self.assertEqual(second["controls"], ["never"])
        self.assertAlmostEqual(second["effect"], 3.0)
        self.assertAlmostEqual(report["effect"]["estimate"], 4.875)
        self.assertTrue(report["pre_trend"]["ok"])
        self.assertEqual(report["pre_trend"]["max_deviation"], 0.0)
        self.assertEqual(first["event_study"],
                         [{"relative": 0, "effect": 6.0}, {"relative": 1, "effect": 7.0}])
        for school in ("early", "late"):
            self.assertTrue(report["schools"][school]["included"])
            self.assertEqual(report["schools"][school]["limitations"], [])
        self.assertEqual(report["schools"]["never"]["role"], "control")

    def test_semester_shock_is_absorbed_by_control_group(self):
        records = (series("t1", [50, 50, 59, 59], activated=2)
                   + series("c1", [50, 50, 55, 55]))
        report = analyze(records)
        self.assertEqual(report["effect"]["treated_gain"], 9.0)
        self.assertEqual(report["effect"]["control_gain"], 5.0)
        self.assertEqual(report["effect"]["estimate"], 4.0)
        self.assertEqual(report["conclusion"], "supports")

    def test_diverged_pre_trend_marks_cohort_and_downgrades_conclusion(self):
        records = (series("t1", [40, 48, 50, 50], activated=2)
                   + series("c1", [40, 40, 41, 41])
                   + series("c2", [44, 44, 45, 45]))
        report = analyze(records)
        self.assertFalse(report["pre_trend"]["ok"])
        self.assertEqual(report["pre_trend"]["max_deviation"], 4.0)
        self.assertIn("pre_trend_diverged", report["limitations"])
        self.assertIn("pre_trend_diverged", report["schools"]["t1"]["limitations"])
        self.assertEqual(report["conclusion"], "inconclusive")

    def test_baseline_outside_comparable_scope_is_excluded(self):
        records = (series("rich", [90, 90, 95, 95], activated=2)
                   + series("c1", [40, 40, 41, 41]))
        report = analyze(records)
        self.assertEqual(report["conclusion"], "not_supported")
        self.assertIn("no_comparable_schools", report["limitations"])
        self.assertEqual(report["schools"]["rich"]["limitations"], ["out_of_comparable_scope"])
        self.assertEqual(report["scope"]["excluded"],
                         [{"school": "rich", "reason": "out_of_comparable_scope"}])
        self.assertFalse(report["schools"]["rich"]["included"])

    def test_attrition_and_language_effects_are_reported(self):
        records = (series("c1", [40, 40, 41, 41], language="alpha")
                   + series("c2", [40, 40, 41, 41], language="beta")
                   + series("a1", [40, 40, 48, 48], activated=2, missing=0.5, language="alpha")
                   + series("a2", [40, 40, 47, 47], activated=2, language="alpha")
                   + series("b1", [40, 40, 40, 40], activated=2, language="beta")
                   + series("b2", [40, 40, 41, 41], activated=2, language="beta"))
        report = analyze(records)
        self.assertAlmostEqual(report["effect"]["estimate"], 3.0)
        attrition = report["attrition"]
        self.assertAlmostEqual(attrition["differential"], 0.1125)
        self.assertAlmostEqual(attrition["shift"], -4 / 3)
        self.assertFalse(attrition["ok"])
        self.assertIn("attrition_distorts_effect", report["limitations"])
        self.assertEqual(report["schools"]["a1"]["limitations"], ["high_attrition"])
        language = report["language_groups"]
        self.assertAlmostEqual(language["effects"]["alpha"], 6.5)
        self.assertAlmostEqual(language["effects"]["beta"], -0.5)
        self.assertFalse(language["ok"])
        self.assertIn("language_group_divergence", report["limitations"])
        self.assertEqual(report["schools"]["b1"]["limitations"], ["language_group_diverged"])
        self.assertEqual(report["schools"]["b2"]["limitations"], ["language_group_diverged"])
        self.assertEqual(report["conclusion"], "inconclusive")
        self.assertTrue(any("失访" in line for line in report["summary"]))
        self.assertTrue(any("语言群体" in line for line in report["summary"]))

    def test_missing_control_group_blocks_conclusion(self):
        report = analyze(series("only", [40, 40, 48, 48], activated=2))
        self.assertEqual(report["conclusion"], "not_supported")
        self.assertIn("no_control_group", report["limitations"])
        self.assertEqual(report["schools"]["only"]["limitations"], ["no_control_group"])

    def test_report_hash_detects_tampering_and_config_is_validated(self):
        report = analyze(series("t1", [50, 50, 59, 59], activated=2)
                         + series("c1", [50, 50, 55, 55]))
        self.assertEqual(report_hash(report), report["evidence_hash"])
        tampered = dict(report, conclusion="inconclusive")
        self.assertNotEqual(report_hash(tampered), report["evidence_hash"])
        self.assertTrue(report["analysis_id"].startswith("staged-"))
        with self.assertRaises(ContractError):
            analyze([], {})
        with self.assertRaises(ContractError):
            analyze(series("t1", [1, 2], activated=1), {"unknown_option": 1})


if __name__ == "__main__":
    unittest.main()
