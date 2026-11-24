"""甲乙分区抄表与合报接口（均可在 /docs 中直接操作）。

  POST /zones/{zone}/submissions          一区整份上报/更正读数（带符号整数升区间）
  GET  /zones/{zone}/submissions          该区全部上报版本
  GET  /zones/{zone}/submissions/latest   该区最新上报
  POST /combined-reports                  按底图诊断号合报并诊断（换稿则退回）
  GET  /combined-reports/by-base/{id}    按底图查全部成功合报
  GET  /combined-reports/{id}             查单次合报结果（失败时原结果仍可查）
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import db
from .combine import (JIA, YI, MergeError, base_map_from_diagnosis,
                      merge_submissions, run_combined_diagnosis)
from .schemas import CombineIn, ZoneSubmissionIn
from .solver import SolverError

router = APIRouter(tags=["分区合报"])


def _check_zone(zone: str) -> str:
    if zone not in (JIA, YI):
        raise HTTPException(status_code=422,
                            detail=f"分区只支持 {JIA} 或 {YI}，收到：{zone}")
    return zone


# ---------------- 分区上报 ----------------

@router.post("/zones/{zone}/submissions", status_code=201,
             summary="一区整份上报/更正读数（生成新上报号）")
def post_submission(zone: str, body: ZoneSubmissionIn):
    """整份替换该区读数并生成一个新版本，返回上报号（submission_id）。

    每条读数填管段编号、计量方向的起止节点和**带符号整数升**区间；
    负号表示按计量方向反向过水，合报时按底图方向统一换算。
    更正即重新整份上传，旧版本保留可查。
    """
    _check_zone(zone)
    submission_id = db.save_zone_submission(
        zone, [r.model_dump() for r in body.readings])
    return {"submission_id": submission_id, "zone": zone,
            "readings": [r.model_dump() for r in body.readings]}


@router.get("/zones/{zone}/submissions",
            summary="查询一区全部上报版本（新的在前）")
def list_submissions(zone: str):
    _check_zone(zone)
    return {"zone": zone, "submissions": db.list_zone_submissions(zone)}


@router.get("/zones/{zone}/submissions/latest",
            summary="查询一区最新上报")
def latest_submission(zone: str):
    _check_zone(zone)
    sub = db.latest_zone_submission(zone)
    if sub is None:
        raise HTTPException(status_code=404,
                            detail=f"{zone}区尚无任何上报")
    return sub


# ---------------- 合报 ----------------

def _load_requested_submission(zone: str, submission_id: int) -> dict:
    sub = db.get_zone_submission(submission_id)
    if sub is None:
        raise HTTPException(status_code=422,
                            detail=f"{zone}区上报号 {submission_id} 不存在")
    if sub["zone"] != zone:
        raise HTTPException(
            status_code=422,
            detail=f"上报号 {submission_id} 属于 {sub['zone']}区，"
                   f"不能作为 {zone}区上报")
    return sub


@router.post("/combined-reports", status_code=201,
             summary="按底图诊断号合报两区读数并诊断")
def post_combined_report(body: CombineIn):
    """用已有诊断号选定管网底图，拼合甲乙两区读数并做账平诊断。

    拼表规则：交界管段须两区都报、端点与底图一致（允许反向抄表），
    换到底图方向后取两区**非负交集**作为一条读数；其余管段须一区独报。
    缺报、同区重复、管段或端点不符、取不到非负区间等问题一次性列出并退回
    （422，不写库）。账不平也保存。保存时任一区已换稿（有了更新的上报号）
    则退回（409，不写库），失败时此前的成功合报仍可查。
    """
    base_diag = db.get_diagnosis_by_id(body.base_diagnosis_id)
    if base_diag is None:
        raise HTTPException(
            status_code=422,
            detail=f"诊断号 {body.base_diagnosis_id} 不存在，无法选定底图")
    jia = _load_requested_submission(JIA, body.jia_submission_id)
    yi = _load_requested_submission(YI, body.yi_submission_id)

    base_map = base_map_from_diagnosis(base_diag)
    try:
        merged = merge_submissions(base_map, body.boundary_segment_keys,
                                   jia, yi)
    except MergeError as exc:
        raise HTTPException(status_code=422,
                            detail={"message": "合报拼表失败，已退回未保存",
                                    "issues": exc.issues})
    try:
        result = run_combined_diagnosis(base_map, merged)
    except SolverError as exc:  # 计算失败不写库
        raise HTTPException(status_code=500,
                            detail=f"合报诊断计算失败，已退回未保存：{exc}")

    inputs = {
        "base_diagnosis_id": body.base_diagnosis_id,
        "ports": base_map["ports"],
        "segments": [{"key": s["key"], "from_node": s["from_node"],
                      "to_node": s["to_node"], "capacity": s["capacity"],
                      "meter_lo": s["meter_lo"], "meter_hi": s["meter_hi"],
                      "source": s["source"],
                      "contributors": s["contributors"]} for s in merged],
        "boundary_segment_keys": list(body.boundary_segment_keys),
        "submission_ids": {JIA: body.jia_submission_id,
                           YI: body.yi_submission_id},
    }
    try:
        report_id = db.save_combined_report(
            body.base_diagnosis_id, base_diag["shift"],
            body.jia_submission_id, body.yi_submission_id,
            body.boundary_segment_keys, inputs, result)
    except db.StaleSubmissionError as exc:  # 计算期间某区换稿
        raise HTTPException(status_code=409,
                            detail=f"合报退回（未保存）：{exc}")
    return {"combined_report_id": report_id,
            "base_diagnosis_id": body.base_diagnosis_id,
            "base_shift": base_diag["shift"],
            "jia_submission_id": body.jia_submission_id,
            "yi_submission_id": body.yi_submission_id,
            "boundary_segment_keys": body.boundary_segment_keys,
            "inputs": inputs, "result": result}


@router.get("/combined-reports/by-base/{base_diagnosis_id}",
            summary="按底图（诊断号）查全部成功合报，最近一次在前")
def get_reports_by_base(base_diagnosis_id: int):
    if db.get_diagnosis_by_id(base_diagnosis_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"诊断号 {base_diagnosis_id} 不存在")
    reports = db.combined_reports_for_base(base_diagnosis_id)
    return {"base_diagnosis_id": base_diagnosis_id,
            "latest": reports[0] if reports else None,
            "combined_reports": reports}


@router.get("/combined-reports/{report_id}",
            summary="按合报号查单次成功合报（含底表、诊断与所用上报号）")
def get_report(report_id: int):
    report = db.get_combined_report(report_id)
    if report is None:
        raise HTTPException(status_code=404,
                            detail=f"合报号 {report_id} 不存在"
                                   "（拼表/换稿退回的失败合报不写库）")
    return report
