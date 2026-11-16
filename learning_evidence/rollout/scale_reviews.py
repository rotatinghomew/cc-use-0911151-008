"""使用已发布评分卡安排学校扩容评议及名额确认。

评议不再只看一张总分表：批准必须引用分阶段成效分析证据及适用学校；
改期或资格更正后，绑定新分析并重新评估受影响的待审学校；
已批准或暂缓的结论保持当时口径；证据不足的学校可见具体限制来源。
"""
import json
import math
from typing import Mapping

from learning_evidence.programs.contract import ContractError, canonical_json, normalized_text, utc_now
from learning_evidence.impact_analysis.staged_rollout import CONCLUSIONS, report_hash


class ScaleReviews:
    def __init__(self, repository):
        self.repository = repository
        with repository.connect() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS scale_campaigns(
                    campaign_id TEXT PRIMARY KEY, card_id TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL, minimum_score REAL NOT NULL,
                    seats INTEGER NOT NULL CHECK(seats>=0));
                CREATE TABLE IF NOT EXISTS school_scale_reviews(
                    campaign_id TEXT NOT NULL, school_id TEXT NOT NULL,
                    score REAL NOT NULL, source_outcome TEXT NOT NULL, status TEXT NOT NULL,
                    seats INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
                    analysis_id TEXT NOT NULL DEFAULT '',
                    applicable_json TEXT NOT NULL DEFAULT '[]',
                    limitations_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(campaign_id,school_id));
                CREATE TABLE IF NOT EXISTS scale_analyses(
                    analysis_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL,
                    evidence_hash TEXT NOT NULL, report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scale_review_events(
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    campaign_id TEXT NOT NULL, school_id TEXT NOT NULL,
                    analysis_id TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL);
            """)
            columns = {row[1] for row in c.execute("PRAGMA table_info(school_scale_reviews)")}
            for name, definition in (("analysis_id", "TEXT NOT NULL DEFAULT ''"),
                                     ("applicable_json", "TEXT NOT NULL DEFAULT '[]'"),
                                     ("limitations_json", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in columns:
                    c.execute(f"ALTER TABLE school_scale_reviews ADD COLUMN {name} {definition}")

    def open(self, campaign_id, card_id, minimum_score, seats):
        if not campaign_id or type(seats) is not int or seats < 0:
            raise ContractError("扩容评议及名额无效")
        if isinstance(minimum_score, bool) or not isinstance(minimum_score, (int, float)) or not math.isfinite(minimum_score) or not 0 <= minimum_score <= 1:
            raise ContractError("候选评分门槛无效")
        with self.repository.transaction() as c:
            from learning_evidence.publication.cohort_scorecard import read_card
            card = read_card(c, card_id)
            old = c.execute("SELECT * FROM scale_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if old:
                if (old["card_id"], old["minimum_score"], old["seats"]) != (card_id, minimum_score, seats):
                    raise ContractError("同一扩容评议不可混用评分卡或门槛")
                return self._read(c, campaign_id)
            c.execute("INSERT INTO scale_campaigns VALUES(?,?,?,?,?)",
                      (campaign_id, card_id, card["evidence_hash"], minimum_score, seats))
            for school in card["schools"]:
                status = "pending" if school["score"] >= minimum_score else "insufficient"
                c.execute("INSERT INTO school_scale_reviews(campaign_id,school_id,score,source_outcome,status) VALUES(?,?,?,?,?)",
                          (campaign_id, school["school_id"], school["score"], school["outcome"], status))
            return self._read(c, campaign_id)

    def attach_analysis(self, campaign_id, report):
        """把分阶段成效分析绑定到评议；同一编号不可对应不同证据。"""
        if not isinstance(report, Mapping):
            raise ContractError("分析证据无效")
        analysis_id = report.get("analysis_id")
        evidence = report.get("evidence_hash")
        if not isinstance(analysis_id, str) or not analysis_id.strip() or not isinstance(evidence, str) or not evidence.strip():
            raise ContractError("分析证据缺少编号或指纹")
        if report_hash(report) != evidence:
            raise ContractError("分析证据指纹不一致")
        if report.get("conclusion") not in CONCLUSIONS or not isinstance(report.get("schools"), Mapping):
            raise ContractError("分析证据缺少结论或学校范围")
        body = canonical_json(report)
        with self.repository.transaction() as c:
            if c.execute("SELECT 1 FROM scale_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone() is None:
                raise ContractError("扩容评议不存在")
            old = c.execute("SELECT campaign_id,evidence_hash,report_json FROM scale_analyses WHERE analysis_id=?",
                            (analysis_id,)).fetchone()
            if old:
                if (old["campaign_id"], old["evidence_hash"], old["report_json"]) != (campaign_id, evidence, body):
                    raise ContractError("同一分析编号不可对应不同证据")
                return analysis_id
            c.execute("INSERT INTO scale_analyses VALUES(?,?,?,?,?)",
                      (analysis_id, campaign_id, evidence, body, utc_now()))
            return analysis_id

    def reassess(self, campaign_id, school_ids, analysis_id, reason):
        """改期或资格更正后，按新分析重新评估受影响的待审学校。"""
        if not isinstance(reason, str) or not reason.strip():
            raise ContractError("重新评估需要说明原因")
        if (not isinstance(school_ids, (list, tuple)) or not school_ids
                or any(not isinstance(item, str) or not item.strip() for item in school_ids)
                or len(set(school_ids)) != len(school_ids)):
            raise ContractError("重新评估的学校清单无效")
        with self.repository.transaction() as c:
            campaign = c.execute("SELECT * FROM scale_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            if campaign is None:
                raise ContractError("扩容评议不存在")
            analysis = self._analysis(c, campaign_id, analysis_id)
            report = json.loads(analysis["report_json"])
            for school_id in school_ids:
                row = c.execute("SELECT * FROM school_scale_reviews WHERE campaign_id=? AND school_id=?",
                                (campaign_id, school_id)).fetchone()
                if row is None:
                    raise ContractError("学校不属于本次比较人口")
                if row["status"] not in {"pending", "insufficient"}:
                    raise ContractError("已批准或暂缓的结论保持当时口径")
                ok, limitations = self._school_evidence(report, self._school_key(c, school_id))
                status = "pending" if ok and row["score"] >= campaign["minimum_score"] else "insufficient"
                c.execute("UPDATE school_scale_reviews SET status=?,analysis_id=?,limitations_json=? "
                          "WHERE campaign_id=? AND school_id=?",
                          (status, analysis_id, canonical_json(limitations), campaign_id, school_id))
                c.execute("INSERT INTO scale_review_events(campaign_id,school_id,analysis_id,reason,created_at) "
                          "VALUES(?,?,?,?,?)", (campaign_id, school_id, analysis_id, reason.strip(), utc_now()))
            return self._read(c, campaign_id)

    def decide(self, campaign_id, school_id, status, reason, seats=0, evidence=None):
        if status not in {"approved", "deferred"} or not isinstance(reason, str) or not reason.strip():
            raise ContractError("评议需要明确结论及依据")
        if type(seats) is not int or seats < 0 or (status == "deferred" and seats != 0):
            raise ContractError("名额无效")
        analysis_id, applicable = "", []
        if evidence is not None:
            analysis_id, applicable = self._read_evidence(evidence)
        with self.repository.transaction() as c:
            campaign = c.execute("SELECT * FROM scale_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
            row = c.execute("SELECT * FROM school_scale_reviews WHERE campaign_id=? AND school_id=?",
                            (campaign_id, school_id)).fetchone()
            if campaign is None or row is None:
                raise ContractError("学校不属于本次比较人口")
            if row["status"] != "pending":
                if (row["status"], row["reason"], row["seats"], row["analysis_id"],
                        json.loads(row["applicable_json"])) == (status, reason, seats, analysis_id, applicable):
                    return
                raise ContractError("评分不足或评议已完成，不可覆盖历史")
            if status == "approved":
                if not analysis_id:
                    raise ContractError("扩容批准须引用分析证据及适用学校")
                if school_id not in applicable:
                    raise ContractError("适用学校须覆盖被批准学校")
                report = json.loads(self._analysis(c, campaign_id, analysis_id)["report_json"])
                if report["conclusion"] != "supports":
                    raise ContractError("分析结论不足以支持扩容批准")
                ok, limitations = self._school_evidence(report, self._school_key(c, school_id))
                if not ok:
                    raise ContractError("证据不足，限制来源：" + ",".join(limitations))
                for cited in applicable:
                    cited_ok, _ = self._school_evidence(report, self._school_key(c, cited))
                    if not cited_ok:
                        raise ContractError("引用的适用学校不在分析可比范围内")
                if row["source_outcome"] in {"hold", "reject"} or seats == 0:
                    raise ContractError("既有安全门禁未放行或未申请扩容名额")
                allocated = c.execute("SELECT COALESCE(SUM(seats),0) FROM school_scale_reviews WHERE campaign_id=?",
                                      (campaign_id,)).fetchone()[0]
                if allocated + seats > campaign["seats"]:
                    raise ContractError("本轮扩容名额不足")
            elif analysis_id:
                self._analysis(c, campaign_id, analysis_id)
            c.execute("UPDATE school_scale_reviews SET status=?,seats=?,reason=?,analysis_id=?,applicable_json=? "
                      "WHERE campaign_id=? AND school_id=?",
                      (status, seats, reason, analysis_id, canonical_json(applicable), campaign_id, school_id))

    @staticmethod
    def _read_evidence(evidence):
        if not isinstance(evidence, Mapping):
            raise ContractError("分析证据引用无效")
        analysis_id = evidence.get("analysis_id")
        applicable = evidence.get("applicable_schools")
        if not isinstance(analysis_id, str) or not analysis_id.strip():
            raise ContractError("分析证据引用缺少编号")
        if not isinstance(applicable, (list, tuple)):
            raise ContractError("适用学校清单无效")
        schools = [item.strip() for item in applicable if isinstance(item, str)]
        if not schools or len(schools) != len(applicable) or len(set(schools)) != len(schools) or any(not item for item in schools):
            raise ContractError("适用学校清单无效")
        return analysis_id.strip(), schools

    def _analysis(self, c, campaign_id, analysis_id):
        row = c.execute("SELECT * FROM scale_analyses WHERE analysis_id=? AND campaign_id=?",
                        (analysis_id, campaign_id)).fetchone()
        if row is None:
            raise ContractError("分析证据不属于本次评议")
        return row

    @staticmethod
    def _school_key(c, school_id):
        row = c.execute("SELECT payload_json FROM records WHERE record_id=?", (school_id,)).fetchone()
        if row:
            code = normalized_text(json.loads(row["payload_json"]).get("school_code"))
            if code:
                return code
        return school_id

    @staticmethod
    def _school_evidence(report, key):
        entry = report["schools"].get(key)
        if not isinstance(entry, Mapping):
            return False, ["no_analysis_evidence"]
        limitations = sorted(str(item) for item in entry.get("limitations") or [])
        return bool(entry.get("included")) and not limitations, limitations

    def _read(self, c, campaign_id):
        campaign = c.execute("SELECT * FROM scale_campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
        if campaign is None:
            raise ContractError("扩容评议不存在")
        schools = []
        for row in c.execute("SELECT * FROM school_scale_reviews WHERE campaign_id=? ORDER BY score DESC,school_id",
                             (campaign_id,)):
            entry = dict(row)
            entry["applicable_schools"] = json.loads(entry.pop("applicable_json"))
            entry["limitations"] = json.loads(entry.pop("limitations_json"))
            schools.append(entry)
        analyses = [dict(row) for row in c.execute(
            "SELECT analysis_id,evidence_hash,created_at FROM scale_analyses WHERE campaign_id=? "
            "ORDER BY created_at,analysis_id", (campaign_id,))]
        reassessments = [dict(row) for row in c.execute(
            "SELECT school_id,analysis_id,reason,created_at FROM scale_review_events WHERE campaign_id=? "
            "ORDER BY event_id", (campaign_id,))]
        return dict(campaign) | {"schools": schools, "analyses": analyses, "reassessments": reassessments}

    def inspect(self, campaign_id):
        with self.repository.connect() as c:
            return self._read(c, campaign_id)
