"""分阶段启用的成效分析：按真实启用时点组织对照、核验前趋势与可比范围。

针对"期末分数提升只是原有水平和学期变化"以及"分批启用被直接横比"的质疑，
本模块以双重差分组织证据：同一启用批次的学校与尚未启用的学校比较，
共同的时间冲击（学期变化）由对照组吸收，原有水平通过前趋势与可比范围核验。
失访与语言群体差异作为结论的限制条件显式报告。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from learning_evidence.programs.contract import (
    ContractError,
    DomainRecord,
    finite_number,
    normalized_text,
    stable_hash,
)

PERIOD_FIELD = "period"
ACTIVATION_FIELD = "activated_period"
OUTCOME_FIELD = "outcome_score"
BASELINE_FIELD = "baseline_score"
ATTRITION_FIELD = "missing_rate"
LANGUAGE_FIELD = "language_group"

CONCLUSIONS = ("supports", "inconclusive", "not_supported")

DEFAULT_CONFIG = {
    "pre_trend_tolerance": 3.0,
    "attrition_tolerance": 0.3,
    "attrition_shift_tolerance": 1.0,
    "language_spread_tolerance": 2.0,
    "min_effect": 0.0,
}

_HASH_FIELDS = frozenset({"analysis_id", "evidence_hash"})


def school_key(record: DomainRecord) -> str:
    """学校身份：优先 school_code，缺失时退回记录编号。"""
    code = normalized_text(record.payload.get("school_code"))
    return code or record.record_id


def report_hash(report: Mapping[str, Any]) -> str:
    """分析证据指纹：排除自描述字段后对内容取稳定散列。"""
    return stable_hash({key: value for key, value in report.items() if key not in _HASH_FIELDS})


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return sum(values) / len(values) if values else None


def _window(observations: Mapping[float, float], low: float, high: float) -> list[float]:
    return [outcome for period, outcome in observations.items() if low <= period < high]


def _observation(record: DomainRecord) -> tuple[float, float] | None:
    payload = record.payload
    if payload.get(PERIOD_FIELD) in (None, "") or payload.get(OUTCOME_FIELD) in (None, ""):
        return None
    return finite_number(payload.get(PERIOD_FIELD)), finite_number(payload.get(OUTCOME_FIELD))


def _build_profiles(records: list[DomainRecord]) -> dict[str, dict[str, Any]]:
    raw: dict[str, dict[str, Any]] = {}
    for record in records:
        entry = raw.setdefault(school_key(record), {"observations": {}, "activations": [],
                                                    "languages": {}, "missing": [], "baselines": []})
        payload = record.payload
        activated = payload.get(ACTIVATION_FIELD)
        if activated not in (None, ""):
            entry["activations"].append(finite_number(activated))
        language = normalized_text(payload.get(LANGUAGE_FIELD))
        if language:
            entry["languages"][language] = entry["languages"].get(language, 0) + 1
        missing = payload.get(ATTRITION_FIELD)
        if missing not in (None, ""):
            entry["missing"].append(finite_number(missing))
        baseline = payload.get(BASELINE_FIELD)
        if baseline not in (None, ""):
            entry["baselines"].append(finite_number(baseline))
        observation = _observation(record)
        if observation is not None:
            period, outcome = observation
            entry["observations"].setdefault(period, []).append(outcome)
    profiles = {}
    for key, entry in raw.items():
        languages = entry["languages"]
        profiles[key] = {
            "observations": {period: sum(values) / len(values)
                             for period, values in entry["observations"].items()},
            "activation": min(entry["activations"]) if entry["activations"] else None,
            "language": max(languages, key=lambda name: (languages[name], name)) if languages else "unknown",
            "missing": _mean(entry["missing"]) or 0.0,
            "baseline": _mean(entry["baselines"]),
        }
    return profiles


def _cohort(activation: float, treated: list[str], controls: list[str],
            profiles: dict[str, dict[str, Any]], cfg: dict[str, float],
            marks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """单个启用批次：处理组为此时点启用的学校，对照组为尚未启用的学校。"""
    excluded: dict[str, str] = {}
    usable_controls: dict[str, dict[str, float]] = {}
    for key in controls:
        own = profiles[key]["activation"]
        horizon = own if own is not None else float("inf")
        observations = profiles[key]["observations"]
        pre = _window(observations, float("-inf"), activation)
        post = _window(observations, activation, horizon)
        if pre and post:
            usable_controls[key] = {"pre": _mean(pre), "post": _mean(post)}
    treated_stats: dict[str, dict[str, float]] = {}
    for key in treated:
        observations = profiles[key]["observations"]
        pre = _window(observations, float("-inf"), activation)
        post = _window(observations, activation, float("inf"))
        if not pre or not post:
            marks[key]["limitations"].add("insufficient_periods")
            excluded[key] = "insufficient_periods"
            continue
        treated_stats[key] = {"pre": _mean(pre), "post": _mean(post)}
    included: dict[str, dict[str, float]] = {}
    control_min = control_max = None
    if not usable_controls:
        for key in treated_stats:
            marks[key]["limitations"].add("no_control_group")
            excluded[key] = "no_control_group"
    else:
        control_min = min(stats["pre"] for stats in usable_controls.values())
        control_max = max(stats["pre"] for stats in usable_controls.values())
        for key, stats in treated_stats.items():
            if control_min <= stats["pre"] <= control_max:
                included[key] = stats
            else:
                marks[key]["limitations"].add("out_of_comparable_scope")
                excluded[key] = "out_of_comparable_scope"
    gaps = []
    pre_periods = sorted({period for key in list(included) + list(usable_controls)
                          for period in profiles[key]["observations"] if period < activation})
    for period in pre_periods:
        treated_values = [profiles[key]["observations"][period] for key in included
                          if period in profiles[key]["observations"]]
        control_values = [profiles[key]["observations"][period] for key in usable_controls
                          if period in profiles[key]["observations"]]
        if treated_values and control_values:
            gaps.append([period, _mean(treated_values) - _mean(control_values)])
    if len(gaps) < 2:
        verifiable, deviation, pre_ok = False, None, False
        for key in included:
            marks[key]["limitations"].add("insufficient_pre_periods")
    else:
        verifiable = True
        center = _mean(gap for _, gap in gaps)
        deviation = max(abs(gap - center) for _, gap in gaps)
        pre_ok = deviation <= cfg["pre_trend_tolerance"]
        if not pre_ok:
            for key in included:
                marks[key]["limitations"].add("pre_trend_diverged")
    gains_treated = {key: stats["post"] - stats["pre"] for key, stats in included.items()}
    gains_control = {key: stats["post"] - stats["pre"] for key, stats in usable_controls.items()}
    for key, gain in gains_treated.items():
        marks[key]["gain"] = gain
    effect = None
    if gains_treated and gains_control:
        effect = _mean(gains_treated.values()) - _mean(gains_control.values())
    lifts: dict[float, dict[str, list[float]]] = {}
    for key, stats in included.items():
        for period, outcome in profiles[key]["observations"].items():
            if period >= activation:
                lifts.setdefault(period - activation, {}).setdefault("treated", []).append(outcome - stats["pre"])
    for key, stats in usable_controls.items():
        horizon = profiles[key]["activation"]
        horizon = horizon if horizon is not None else float("inf")
        for period, outcome in profiles[key]["observations"].items():
            if activation <= period < horizon:
                lifts.setdefault(period - activation, {}).setdefault("control", []).append(outcome - stats["pre"])
    event_study = [{"relative": relative, "effect": _mean(group["treated"]) - _mean(group["control"])}
                   for relative, group in sorted(lifts.items()) if group.get("treated") and group.get("control")]
    return {
        "activation": activation,
        "treated": sorted(treated),
        "controls": sorted(usable_controls),
        "included_treated": sorted(included),
        "excluded_treated": dict(sorted(excluded.items())),
        "control_scope": {"min": control_min, "max": control_max},
        "pre_trend": {"verifiable": verifiable, "ok": pre_ok, "max_deviation": deviation, "gaps": gaps},
        "gains": {"treated": dict(sorted(gains_treated.items())),
                  "control": dict(sorted(gains_control.items()))},
        "treated_gain": _mean(gains_treated.values()),
        "control_gain": _mean(gains_control.values()),
        "effect": effect,
        "event_study": event_study,
    }


def analyze(records: Iterable[DomainRecord], config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """分阶段启用成效分析，输出可供扩容评审引用的证据报告。"""
    cfg = dict(DEFAULT_CONFIG)
    for key, value in (config or {}).items():
        if key not in cfg:
            raise ContractError(f"未知的分析配置项: {key}")
        cfg[key] = finite_number(value)
        if key != "min_effect" and cfg[key] < 0:
            raise ContractError("分析容忍度不可为负")
    records = list(records)
    if not records:
        raise ContractError("分阶段成效分析需要至少一条学校记录")
    profiles = _build_profiles(records)
    marks = {key: {"limitations": set(), "gain": None} for key in profiles}
    for key, profile in profiles.items():
        if not profile["observations"]:
            marks[key]["limitations"].add("insufficient_periods")
    activations = sorted({profile["activation"] for profile in profiles.values()
                          if profile["activation"] is not None})
    cohorts = []
    for activation in activations:
        treated = [key for key, profile in profiles.items() if profile["activation"] == activation]
        controls = [key for key, profile in profiles.items()
                    if profile["activation"] is None or profile["activation"] > activation]
        cohorts.append(_cohort(activation, treated, controls, profiles, cfg, marks))

    def aggregate(allowed) -> tuple[float | None, float | None, float | None]:
        total_weight = 0
        total = 0.0
        treated_gains: list[float] = []
        control_gains: list[float] = []
        for cohort in cohorts:
            gains_t = {key: gain for key, gain in cohort["gains"]["treated"].items() if allowed(key)}
            gains_c = {key: gain for key, gain in cohort["gains"]["control"].items() if allowed(key)}
            if gains_t and gains_c:
                total += len(gains_t) * (_mean(gains_t.values()) - _mean(gains_c.values()))
                total_weight += len(gains_t)
                treated_gains.extend(gains_t.values())
                control_gains.extend(gains_c.values())
        if not total_weight:
            return None, None, None
        return total / total_weight, _mean(treated_gains), _mean(control_gains)

    effect, treated_gain, control_gain = aggregate(lambda key: True)
    included_treated = sorted({key for cohort in cohorts for key in cohort["gains"]["treated"]})
    usable_controls = sorted({key for cohort in cohorts for key in cohort["gains"]["control"]})
    for key, profile in profiles.items():
        if profile["missing"] > cfg["attrition_tolerance"]:
            marks[key]["limitations"].add("high_attrition")
    if effect is not None:
        treated_missing = [profiles[key]["missing"] for key in included_treated]
        control_missing = [profiles[key]["missing"] for key in usable_controls]
        differential = _mean(treated_missing) - _mean(control_missing)
        trimmed, _, _ = aggregate(lambda key: profiles[key]["missing"] <= cfg["attrition_tolerance"])
        shift = trimmed - effect if trimmed is not None else None
        attrition_ok = (shift is not None and abs(shift) <= cfg["attrition_shift_tolerance"]
                        and abs(differential) <= cfg["attrition_tolerance"])
        attrition = {"treated_rate": _mean(treated_missing), "control_rate": _mean(control_missing),
                     "differential": differential, "trimmed_effect": trimmed, "shift": shift,
                     "tolerance": cfg["attrition_shift_tolerance"], "ok": attrition_ok}
    else:
        attrition = {"treated_rate": None, "control_rate": None, "differential": None,
                     "trimmed_effect": None, "shift": None,
                     "tolerance": cfg["attrition_shift_tolerance"], "ok": True}
    grouped: dict[str, list[tuple[int, float]]] = {}
    for cohort in cohorts:
        treated_by_language: dict[str, list[float]] = {}
        control_by_language: dict[str, list[float]] = {}
        for key, gain in cohort["gains"]["treated"].items():
            treated_by_language.setdefault(profiles[key]["language"], []).append(gain)
        for key, gain in cohort["gains"]["control"].items():
            control_by_language.setdefault(profiles[key]["language"], []).append(gain)
        for language in sorted(set(treated_by_language) & set(control_by_language)):
            grouped.setdefault(language, []).append(
                (len(treated_by_language[language]),
                 _mean(treated_by_language[language]) - _mean(control_by_language[language])))
    language_effects = {language: sum(weight * value for weight, value in entries) / sum(weight for weight, _ in entries)
                        for language, entries in sorted(grouped.items())}
    spread = (max(language_effects.values()) - min(language_effects.values())) if len(language_effects) > 1 else 0.0
    language_ok = spread <= cfg["language_spread_tolerance"]
    if not language_ok:
        for language, value in language_effects.items():
            if value <= cfg["min_effect"]:
                for key in included_treated:
                    if profiles[key]["language"] == language:
                        marks[key]["limitations"].add("language_group_diverged")
    verifiable = [cohort for cohort in cohorts if cohort["pre_trend"]["verifiable"]]
    any_diverged = any(cohort["pre_trend"]["verifiable"] and not cohort["pre_trend"]["ok"] for cohort in cohorts)
    max_deviation = max((cohort["pre_trend"]["max_deviation"] for cohort in verifiable), default=None)
    pre_trend = {"ok": bool(verifiable) and not any_diverged,
                 "max_deviation": max_deviation, "tolerance": cfg["pre_trend_tolerance"]}
    limitations = []
    if not activations:
        limitations.append("no_treated_schools")
    if not usable_controls:
        limitations.append("no_control_group")
    if activations and not included_treated and "no_control_group" not in limitations:
        limitations.append("no_comparable_schools")
    if cohorts and not verifiable:
        limitations.append("pre_trend_unverifiable")
    if any_diverged:
        limitations.append("pre_trend_diverged")
    if not attrition["ok"]:
        limitations.append("attrition_distorts_effect")
    if not language_ok:
        limitations.append("language_group_divergence")
    if effect is None:
        conclusion = "not_supported"
    elif effect <= cfg["min_effect"]:
        limitations.append("effect_not_positive")
        conclusion = "not_supported"
    elif limitations:
        conclusion = "inconclusive"
    else:
        conclusion = "supports"
    excluded = {}
    for cohort in cohorts:
        for key, reason in cohort["excluded_treated"].items():
            excluded.setdefault(key, reason)
    scopes = [cohort["control_scope"] for cohort in cohorts if cohort["control_scope"]["min"] is not None]
    scope = {"field": "pre_activation_mean",
             "control_min": min((item["min"] for item in scopes), default=None),
             "control_max": max((item["max"] for item in scopes), default=None),
             "included": included_treated,
             "excluded": [{"school": key, "reason": reason} for key, reason in sorted(excluded.items())]}
    schools = {}
    for key in sorted(profiles):
        profile = profiles[key]
        role = "treated" if profile["activation"] is not None else "control"
        included = key in included_treated if role == "treated" else key in usable_controls
        schools[key] = {"role": role, "activation": profile["activation"], "included": included,
                        "limitations": sorted(marks[key]["limitations"]),
                        "language_group": profile["language"], "attrition": profile["missing"],
                        "baseline": profile["baseline"], "gain": marks[key]["gain"],
                        "observations": len(profile["observations"])}
    summary = [f"按真实启用时点划分 {len(cohorts)} 个启用批次，以尚未启用的学校为对照，避免跨批次直接横比"]
    if max_deviation is None:
        summary.append("启用前趋势无法核验：处理组与对照组重叠的启用前观察不足")
    else:
        summary.append(f"启用前趋势最大偏差 {max_deviation:.2f}（容忍 {cfg['pre_trend_tolerance']:.2f}）："
                       f"{'通过' if pre_trend['ok'] else '未通过'}")
    if scopes:
        summary.append(f"可比范围按对照学校启用前水平 [{scope['control_min']:.2f}, {scope['control_max']:.2f}] 限定，"
                       f"{len(scope['excluded'])} 所学校因超出可比范围被排除")
    if attrition["differential"] is not None:
        shift_text = "无法估计" if attrition["shift"] is None else f"{attrition['shift']:+.2f}"
        summary.append(f"失访：处理组 {attrition['treated_rate']:.2f}、对照组 {attrition['control_rate']:.2f}"
                       f"（差 {attrition['differential']:+.2f}），剔除高失访学校后效应移动 {shift_text}"
                       f"（容忍 ±{cfg['attrition_shift_tolerance']:.2f}）")
    if language_effects:
        summary.append(f"语言群体间效应差 {spread:.2f}（容忍 {cfg['language_spread_tolerance']:.2f}）："
                       f"{'一致' if language_ok else '存在分歧'}")
    if conclusion == "supports":
        summary.append(f"结论：证据支持扩容（校正后效应 {effect:.2f}）")
    elif conclusion == "inconclusive":
        summary.append(f"结论：证据受限（{', '.join(limitations)}），不宜据此扩大名额")
    else:
        summary.append(f"结论：证据不支持扩容（{', '.join(limitations) or 'effect_not_positive'}）")
    report = {
        "method": "staged_rollout_did",
        "config": cfg,
        "school_count": len(profiles),
        "cohorts": cohorts,
        "effect": {"estimate": effect, "treated_gain": treated_gain,
                   "control_gain": control_gain, "min_effect": cfg["min_effect"]},
        "pre_trend": pre_trend,
        "scope": scope,
        "attrition": attrition,
        "language_groups": {"effects": language_effects, "spread": spread,
                            "tolerance": cfg["language_spread_tolerance"], "ok": language_ok},
        "limitations": limitations,
        "conclusion": conclusion,
        "schools": schools,
        "summary": summary,
    }
    report["evidence_hash"] = report_hash(report)
    report["analysis_id"] = f"staged-{report['evidence_hash'][:16]}"
    return report
