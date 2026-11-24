"""甲乙两区分区上报的拼表与合报诊断。

拼表规则（底图方向为基准）：
  * 交界管段须甲乙两区都报，且计量方向的起止节点须与底图管段一致
    （同向：from/to 与底图相同；反向抄表：from/to 与底图对调）；
  * 非交界管段须恰好一区独报，缺报 / 两区同报均退回；
  * 带符号整数升区间按计量方向换算到底图方向——反向抄表取相反数并交换
    上下界；换算后与非负半轴取交集（下界截到 0），取不到非负区间则退回；
  * 交界段取两区换算后区间的非负交集作为一条读数；
  * 同区重复上报同管段、上报了底图不存在的管段、端点对不上，全部列出退回。

所有校验问题先收集齐再一次性抛 MergeError，调用方据此返回 422 且不写库。
"""
from __future__ import annotations

from .solver import Segment, diagnose

JIA = "甲"
YI = "乙"


class MergeError(ValueError):
    """拼表校验未通过；issues 为全部问题清单（中文，可直接展示）。"""

    def __init__(self, issues: list[str]):
        super().__init__("；".join(issues))
        self.issues = issues


def base_map_from_diagnosis(diagnosis: dict) -> dict:
    """从一次既有诊断的 inputs 还原管网底图。

    返回 {"ports": [...], "segments": [{key, from_node, to_node, capacity}]}。
    诊断 inputs 中的管段即当时登记的全网管段，容量与方向以它为准。
    """
    inputs = diagnosis["inputs"]
    segments = [{"key": s["key"], "from_node": s["from_node"],
                 "to_node": s["to_node"], "capacity": s["capacity"]}
                for s in inputs["segments"]]
    return {"ports": list(inputs["ports"]), "segments": segments}


def _oriented_interval(reading: dict, base: dict) -> tuple[int, int] | None:
    """把一条带符号抄表区间换算到底图方向，并与非负半轴取交集。

    同向（reading src->dst == base src->dst）：区间不变；
    反向（reading src->dst == base dst->src）：取相反数并交换上下界。
    返回 None 表示端点与底图管段对不上（由调用方记问题）。
    换算后下界截到 0；上界截到负数则不存在非负区间，返回 (None, None)。
    """
    if reading["src"] == base["from_node"] and reading["dst"] == base["to_node"]:
        lo, hi = reading["meter_lo"], reading["meter_hi"]
    elif reading["src"] == base["to_node"] and reading["dst"] == base["from_node"]:
        # 反向抄表：底图方向过水 = -(计量方向过水)
        lo, hi = -reading["meter_hi"], -reading["meter_lo"]
    else:
        return None
    lo = max(lo, 0)  # 与非负半轴取交集
    if hi < lo:
        return (None, None)  # 取不到非负区间
    return lo, hi


def merge_submissions(base_map: dict, boundary_keys: list[str],
                      jia: dict, yi: dict) -> list[dict]:
    """按规则把两区上报拼成一份全站底表读数。

    jia/yi 为 db.get_zone_submission 的返回（含 readings）。
    成功返回底表读数列表（顺序同底图），元素含换算后的 meter_lo/meter_hi
    及来源信息；失败抛 MergeError（issues 为全部问题）。
    """
    issues: list[str] = []
    base_segments = base_map["segments"]
    base_by_key = {s["key"]: s for s in base_segments}
    boundary = set(boundary_keys)

    unknown_boundary = sorted(k for k in boundary if k not in base_by_key)
    for k in unknown_boundary:
        issues.append(f"交界管段 {k} 不在底图中")

    # 同区重复（正常已被上报接口的 Pydantic 校验拦住，这里再兜底一次）
    def index_submission(sub: dict):
        indexed: dict[str, dict] = {}
        for r in sub["readings"]:
            if r["segment_key"] in indexed:
                issues.append(
                    f"{sub['zone']}区重复上报管段 {r['segment_key']}")
            indexed[r["segment_key"]] = r
        return indexed

    jia_map = index_submission(jia)
    yi_map = index_submission(yi)

    all_keys = set(jia_map) | set(yi_map)
    for k in sorted(all_keys - set(base_by_key)):
        issues.append(f"管段 {k} 不在底图中（底图管段："
                      f"{', '.join(base_by_key)}）")

    merged: list[dict] = []
    for base in base_segments:
        k = base["key"]
        rj, ry = jia_map.get(k), yi_map.get(k)
        in_jia, in_yi = rj is not None, ry is not None

        if k in boundary:
            if not (in_jia and in_yi):
                missing = [z for z, ok in ((JIA, in_jia), (YI, in_yi))
                           if not ok]
                issues.append(f"交界管段 {k} 须两区都报，"
                              f"缺报：{'、'.join(missing)}区")
                continue
            intervals = []
            for zone_name, r in ((JIA, rj), (YI, ry)):
                oriented = _oriented_interval(r, base)
                if oriented is None:
                    issues.append(
                        f"{zone_name}区管段 {k} 的端点 {r['src']}→{r['dst']} "
                        f"与底图 {base['from_node']}→{base['to_node']} 不符")
                    continue
                if oriented[0] is None:
                    issues.append(
                        f"{zone_name}区管段 {k} 换向后取不到非负区间："
                        f"原报 [{r['meter_lo']}, {r['meter_hi']}]")
                    continue
                intervals.append((zone_name, oriented))
            if len(intervals) < 2:
                continue
            (z1, (lo1, hi1)), (z2, (lo2, hi2)) = intervals
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if lo > hi:
                issues.append(
                    f"交界管段 {k} 两区非负区间无交集："
                    f"{z1}区 [{lo1},{hi1}]，{z2}区 [{lo2},{hi2}]")
                continue
            merged.append({"key": k, "from_node": base["from_node"],
                           "to_node": base["to_node"],
                           "capacity": base["capacity"],
                           "meter_lo": lo, "meter_hi": hi,
                           "source": "boundary",
                           "contributors": [
                               {"zone": JIA, "submission_id": jia["id"],
                                "from_node": rj["src"], "to_node": rj["dst"],
                                "meter_lo": rj["meter_lo"],
                                "meter_hi": rj["meter_hi"]},
                               {"zone": YI, "submission_id": yi["id"],
                                "from_node": ry["src"], "to_node": ry["dst"],
                                "meter_lo": ry["meter_lo"],
                                "meter_hi": ry["meter_hi"]}]})
        else:
            if in_jia and in_yi:
                issues.append(f"非交界管段 {k} 须一区独报，甲乙两区都报了")
                continue
            if not in_jia and not in_yi:
                issues.append(f"非交界管段 {k} 缺报（两区均未上报）")
                continue
            zone_name, r = (JIA, rj) if in_jia else (YI, ry)
            sub_id = jia["id"] if in_jia else yi["id"]
            oriented = _oriented_interval(r, base)
            if oriented is None:
                issues.append(
                    f"{zone_name}区管段 {k} 的端点 {r['src']}→{r['dst']} "
                    f"与底图 {base['from_node']}→{base['to_node']} 不符")
                continue
            if oriented[0] is None:
                issues.append(
                    f"{zone_name}区管段 {k} 换向后取不到非负区间："
                    f"原报 [{r['meter_lo']}, {r['meter_hi']}]")
                continue
            lo, hi = oriented
            merged.append({"key": k, "from_node": base["from_node"],
                           "to_node": base["to_node"],
                           "capacity": base["capacity"],
                           "meter_lo": lo, "meter_hi": hi,
                           "source": f"{zone_name}区独报",
                           "contributors": [
                               {"zone": zone_name, "submission_id": sub_id,
                                "from_node": r["src"], "to_node": r["dst"],
                                "meter_lo": r["meter_lo"],
                                "meter_hi": r["meter_hi"]}]})

    if issues:
        raise MergeError(issues)
    return merged


def run_combined_diagnosis(base_map: dict, merged: list[dict]) -> dict:
    """用拼好的底表读数 + 底图容量/进出水口跑原有诊断。

    合报诊断不带复测区间（复测只属于班次登记流程）。
    账不平也正常返回结果，由调用方照常入库。
    """
    segments = [Segment(key=s["key"], src=s["from_node"], dst=s["to_node"],
                        capacity=s["capacity"], meter_lo=s["meter_lo"],
                        meter_hi=s["meter_hi"]) for s in merged]
    outcome = diagnose(segments, set(base_map["ports"]), {})
    if outcome["balanced"]:
        return {"balanced": True,
                "message": "账平：合报底表所有读数可同时成立",
                "assignment": outcome["assignment"],
                "suspects": []}
    if outcome["suspects"]:
        return {"balanced": False,
                "message": "账不平：仅忽略下列任一只表的原读数即可使全网账平",
                "assignment": None,
                "suspects": outcome["suspects"]}
    return {"balanced": False,
            "message": "账不平：单表误差无法解释，"
                       "可能存在多只表同时失准或漏记管段",
            "assignment": None,
            "suspects": []}
