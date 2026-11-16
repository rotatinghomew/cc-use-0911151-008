import json
import tempfile
import unittest
from pathlib import Path
from learning_evidence.evaluator import Application
from learning_evidence.programs.contract import ContractError, DomainRecord, Decision


def observation_records(school, outcomes, activated=None, missing=0.05, language="standard"):
    records = []
    for period, outcome in enumerate(outcomes):
        payload = {"school_code": school, "period": period, "outcome_score": outcome,
                   "missing_rate": missing, "language_group": language}
        if activated is not None:
            payload["activated_period"] = activated
        records.append(DomainRecord(f"{school}-p{period}", payload))
    return records


class ScaleReviewsTests(unittest.TestCase):
    def test_scorecard_controls_candidate_queue_and_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            app = Application(Path(d) / "db")
            fixture = json.loads((Path(__file__).resolve().parents[1] / "fixtures/acceptance.json").read_text())
            result = app.run_fixture(fixture, Path(d) / "out")
            card_id = result["cohort_card"]["card_id"]
            queue = app.scale_reviews.open("round", card_id, .55, 10)
            expected = {s["school_id"] for s in result["cohort_card"]["schools"] if s["score"] >= .55}
            self.assertEqual({s["school_id"] for s in queue["schools"] if s["status"] == "pending"}, expected)
            self.assertEqual(Application(Path(d) / "db").scale_reviews.inspect("round"), queue)
            with self.assertRaises(ContractError):
                app.scale_reviews.open("round", card_id, .4, 10)
            school = next(s["school_id"] for s in queue["schools"] if s["status"] == "pending")
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", school, "approved", "评议", 1)

    def test_confirmations_share_seats_and_preserve_completed_decisions(self):
        with tempfile.TemporaryDirectory() as d:
            app = Application(Path(d) / "db")
            decisions = {}
            for name in ("a", "b"):
                app.ingest(DomainRecord(name, {"school_code": name}))
                decisions[name] = Decision(name, "approve", .8, ("人工复核",), (), {})
                app.repository.save_decision(decisions[name])
            app.repository.publish_cohort_card("card", "cohort", ["a", "b"], decisions)
            app.scale_reviews.open("round", "card", .6, 1)
            for record in (observation_records("a", [40, 40, 48, 48], activated=2)
                           + observation_records("b", [40, 40, 47, 47], activated=2)
                           + observation_records("ctrl", [40, 40, 41, 41])):
                app.ingest(record)
            analysis_id = app.scale_reviews.attach_analysis("round", app.staged_analysis())
            evidence = {"analysis_id": analysis_id, "applicable_schools": ["a", "b"]}
            app.scale_reviews.decide("round", "a", "approved", "证据及支持准备充分", 1, evidence)
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "b", "approved", "评议通过", 1, evidence)
            app.scale_reviews.decide("round", "b", "deferred", "等候后续名额")
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "a", "deferred", "改写旧结论")

    def test_approval_requires_analysis_evidence_and_applicable_schools(self):
        with tempfile.TemporaryDirectory() as d:
            app = Application(Path(d) / "db")
            decisions = {}
            for name in ("sa", "sb"):
                app.ingest(DomainRecord(name, {"school_code": name}))
                decisions[name] = Decision(name, "approve", .8, ("人工复核",), (), {})
                app.repository.save_decision(decisions[name])
            app.repository.publish_cohort_card("card", "cohort", ["sa", "sb"], decisions)
            app.scale_reviews.open("round", "card", .6, 2)
            for record in (observation_records("sa", [40, 40, 48, 48], activated=2)
                           + observation_records("sb", [40, 40, 47, 47], activated=2)
                           + observation_records("sc", [40, 40, 41, 41])):
                app.ingest(record)
            analysis_id = app.scale_reviews.attach_analysis("round", app.staged_analysis())
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "sa", "approved", "评议通过", 1)
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "sa", "approved", "评议通过", 1,
                                         {"analysis_id": analysis_id, "applicable_schools": ["sb"]})
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "sa", "approved", "评议通过", 1,
                                         {"analysis_id": analysis_id, "applicable_schools": ["sa", "ghost"]})
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "sa", "approved", "评议通过", 1,
                                         {"analysis_id": "staged-unknown", "applicable_schools": ["sa"]})
            app.scale_reviews.decide("round", "sa", "approved", "证据及支持准备充分", 1,
                                     {"analysis_id": analysis_id, "applicable_schools": ["sa", "sb"]})
            row = next(s for s in app.scale_reviews.inspect("round")["schools"] if s["school_id"] == "sa")
            self.assertEqual(row["analysis_id"], analysis_id)
            self.assertEqual(row["applicable_schools"], ["sa", "sb"])

    def test_reassess_after_reschedule_reevaluates_pending_and_preserves_approved(self):
        with tempfile.TemporaryDirectory() as d:
            app = Application(Path(d) / "db")
            decisions = {}
            for name, score in (("sa", .8), ("sb", .7)):
                app.ingest(DomainRecord(name, {"school_code": name}))
                decisions[name] = Decision(name, "approve", score, ("人工复核",), (), {})
                app.repository.save_decision(decisions[name])
            app.repository.publish_cohort_card("card", "cohort", ["sa", "sb"], decisions)
            app.scale_reviews.open("round", "card", .6, 2)
            for record in (observation_records("sa", [40, 40, 48, 48], activated=2)
                           + observation_records("sb", [40, 40, 47, 47], activated=2)
                           + observation_records("sc", [40, 40, 41, 41])):
                app.ingest(record)
            first = app.scale_reviews.attach_analysis("round", app.staged_analysis())
            view = app.scale_reviews.reassess("round", ["sa", "sb"], first, "初次证据核对")
            self.assertEqual({s["school_id"] for s in view["schools"] if s["status"] == "pending"}, {"sa", "sb"})
            app.scale_reviews.decide("round", "sa", "approved", "证据及支持准备充分", 1,
                                     {"analysis_id": first, "applicable_schools": ["sa", "sb"]})
            for record in observation_records("sb", [40, 40, 47, 47], activated=3):
                app.ingest(record)
            second = app.scale_reviews.attach_analysis("round", app.staged_analysis())
            self.assertNotEqual(first, second)
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "sb", "approved", "改期后直接批准", 1,
                                         {"analysis_id": second, "applicable_schools": ["sb"]})
            view = app.scale_reviews.reassess("round", ["sb"], second, "启用改期后重新评估")
            rows = {s["school_id"]: s for s in view["schools"]}
            self.assertEqual(rows["sb"]["status"], "insufficient")
            self.assertEqual(rows["sb"]["limitations"], ["out_of_comparable_scope"])
            self.assertEqual(rows["sb"]["analysis_id"], second)
            self.assertEqual(rows["sb"]["seats"], 0)
            self.assertEqual(rows["sa"]["status"], "approved")
            self.assertEqual(rows["sa"]["analysis_id"], first)
            self.assertEqual(rows["sa"]["seats"], 1)
            self.assertEqual(view["reassessments"][-1]["reason"], "启用改期后重新评估")
            with self.assertRaises(ContractError):
                app.scale_reviews.reassess("round", ["sa"], second, "已完成结论不可改写")
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "sb", "approved", "换证据再试", 1,
                                         {"analysis_id": second, "applicable_schools": ["sb"]})

    def test_insufficient_evidence_shows_limitation_source(self):
        with tempfile.TemporaryDirectory() as d:
            app = Application(Path(d) / "db")
            decisions = {}
            for name in ("sa", "sb"):
                app.ingest(DomainRecord(name, {"school_code": name}))
                decisions[name] = Decision(name, "approve", .8, ("人工复核",), (), {})
                app.repository.save_decision(decisions[name])
            app.repository.publish_cohort_card("card", "cohort", ["sa", "sb"], decisions)
            app.scale_reviews.open("round", "card", .6, 2)
            for record in (observation_records("sa", [40, 40, 48, 48], activated=2, missing=0.5, language="alpha")
                           + observation_records("sb", [40, 40, 40, 40], activated=2, language="beta")
                           + observation_records("c1", [40, 40, 41, 41], language="alpha")
                           + observation_records("c2", [40, 40, 41, 41], language="beta")):
                app.ingest(record)
            report = app.staged_analysis()
            self.assertEqual(report["conclusion"], "inconclusive")
            analysis_id = app.scale_reviews.attach_analysis("round", report)
            with self.assertRaises(ContractError):
                app.scale_reviews.decide("round", "sa", "approved", "结论受限仍批准", 1,
                                         {"analysis_id": analysis_id, "applicable_schools": ["sa"]})
            view = app.scale_reviews.reassess("round", ["sa", "sb"], analysis_id, "证据限制核对")
            rows = {s["school_id"]: s for s in view["schools"]}
            self.assertEqual(rows["sa"]["status"], "insufficient")
            self.assertEqual(rows["sa"]["limitations"], ["high_attrition"])
            self.assertEqual(rows["sb"]["status"], "insufficient")
            self.assertEqual(rows["sb"]["limitations"], ["language_group_diverged"])
            self.assertEqual(rows["sa"]["seats"], 0)
            self.assertEqual(rows["sb"]["seats"], 0)
            reopened = Application(Path(d) / "db").scale_reviews.inspect("round")
            self.assertEqual(reopened, view)
