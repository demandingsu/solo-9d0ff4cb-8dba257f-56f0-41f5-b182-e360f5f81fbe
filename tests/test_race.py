#!/usr/bin/env python3
"""服务层测试：拼表/诊断进行期间某区换稿，保存时必须退回（409 语义）。

HTTP 冒烟里只能在发请求前换稿；这里直接调用拼表与持久层，在
merge/diagnose 完成之后、save_combined_report 之前插入一次“换稿”，
精确命中保存事务内的最新上报号复查。另覆盖反向抄表的纯函数换算。

用法: python3 -I tests/test_race.py
（脚本自行把仓库根与 .pylibs 加入 sys.path；-I 保证不加载杂散模块）
"""
import os
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, ".pylibs"))
sys.path.insert(0, _ROOT)

_tmp = tempfile.mkdtemp(prefix="race-")
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "race.db")

from app import combine, db  # noqa: E402
from app.combine import MergeError  # noqa: E402

FAILED = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  | {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


db.init_db()

# 占位底图诊断（合报表对 diagnoses 有外键；拼表本身只用内存底图）
with db._conn() as conn:
    cur = conn.execute(
        "INSERT INTO registrations(shift, ports_json, created_at) "
        "VALUES (?,?,?)", ("race", "[]", db._now()))
    reg_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO diagnoses(shift, registration_id, inputs_json, "
        "result_json, created_at) VALUES (?,?,?,?,?)",
        ("race", reg_id, "{}", "{}", db._now()))
    base_diag_id = cur.lastrowid

# 底图：IN-A-B-OUT，S2 为甲乙交界
base_diag = {"inputs": {
    "ports": ["IN", "OUT"],
    "segments": [
        {"key": "S1", "from_node": "IN", "to_node": "A", "capacity": 100,
         "meter_lo": 10, "meter_hi": 20},
        {"key": "S2", "from_node": "A", "to_node": "B", "capacity": 100,
         "meter_lo": 10, "meter_hi": 20},
        {"key": "S3", "from_node": "B", "to_node": "OUT", "capacity": 100,
         "meter_lo": 10, "meter_hi": 20}]}}
base_map = combine.base_map_from_diagnosis(base_diag)
base_seg = {s["key"]: s for s in base_map["segments"]}

# 纯函数：反向抄表换算（-20..-10 反向 -> 正向 [10,20]）
iv = combine._oriented_interval(
    {"src": "B", "dst": "A", "meter_lo": -20, "meter_hi": -10}, base_seg["S2"])
check("反向区间换算为正向 [10,20]", iv == (10, 20), str(iv))
# 跨零区间与非负半轴取交集：[-5, 8] 同向 -> [0,8]
iv0 = combine._oriented_interval(
    {"src": "A", "dst": "B", "meter_lo": -5, "meter_hi": 8}, base_seg["S2"])
check("跨零区间截到非负 [0,8]", iv0 == (0, 8), str(iv0))
# 整段为负且同向 -> 取不到非负区间
ivn = combine._oriented_interval(
    {"src": "A", "dst": "B", "meter_lo": -8, "meter_hi": -2}, base_seg["S2"])
check("整段负区间返回空标记", ivn == (None, None), str(ivn))
# 端点完全对不上 -> None
ivx = combine._oriented_interval(
    {"src": "A", "dst": "X", "meter_lo": 1, "meter_hi": 2}, base_seg["S2"])
check("端点不符返回 None", ivx is None)

# 准备两区上报
jia_id = db.save_zone_submission("甲", [
    {"segment_key": "S1", "from_node": "IN", "to_node": "A",
     "meter_lo": 10, "meter_hi": 20},
    {"segment_key": "S2", "from_node": "A", "to_node": "B",
     "meter_lo": 10, "meter_hi": 20}])
yi_id = db.save_zone_submission("乙", [
    {"segment_key": "S2", "from_node": "B", "to_node": "A",
     "meter_lo": -20, "meter_hi": -10},
    {"segment_key": "S3", "from_node": "OUT", "to_node": "B",
     "meter_lo": -20, "meter_hi": -10}])
jia = db.get_zone_submission(jia_id)
yi = db.get_zone_submission(yi_id)

# 拼表 + 诊断（模拟合报 API 的计算阶段）
merged = combine.merge_submissions(base_map, ["S2"], jia, yi)
check("拼表得到 3 条底表读数", len(merged) == 3)
result = combine.run_combined_diagnosis(base_map, merged)
check("合报诊断账平", result["balanced"] is True, result.get("message"))

# —— 关键：诊断算完、尚未保存时，乙区换稿 ——
yi_new_id = db.save_zone_submission("乙", [
    {"segment_key": "S2", "from_node": "B", "to_node": "A",
     "meter_lo": -18, "meter_hi": -12},
    {"segment_key": "S3", "from_node": "OUT", "to_node": "B",
     "meter_lo": -18, "meter_hi": -12}])
check("乙区换稿生成新上报号", yi_new_id > yi_id)

stale = False
try:
    db.save_combined_report(
        base_diagnosis_id=base_diag_id, base_shift="race",
        jia_submission_id=jia_id, yi_submission_id=yi_id,  # 仍是旧号
        boundary_keys=["S2"], inputs={"segments": merged}, result=result)
except db.StaleSubmissionError as exc:
    stale = True
    print("  退回信息：", exc)
check("保存时复查到乙区已换稿并退回", stale)
check("退回后查不到任何成功合报",
      db.combined_reports_for_base(base_diag_id) == [])

# 用新上报号重新合报并保存 -> 成功
try:
    rid = db.save_combined_report(
        base_diagnosis_id=base_diag_id, base_shift="race",
        jia_submission_id=jia_id, yi_submission_id=yi_new_id,
        boundary_keys=["S2"], inputs={"segments": merged}, result=result)
except db.StaleSubmissionError as exc:
    rid = None
    check("用最新上报号保存成功", False, str(exc))
check("用最新上报号保存成功并返回合报号", isinstance(rid, int))
row = db.latest_combined_report(base_diag_id)
check("按底图查到最近一次成功合报且上报号正确",
      row is not None and row["yi_submission_id"] == yi_new_id
      and row["jia_submission_id"] == jia_id)

# 甲区再换稿，合报号与最新号不一致同样退回
jia_new = db.save_zone_submission("甲", [
    {"segment_key": "S1", "from_node": "IN", "to_node": "A",
     "meter_lo": 11, "meter_hi": 19},
    {"segment_key": "S2", "from_node": "A", "to_node": "B",
     "meter_lo": 11, "meter_hi": 19}])
stale_jia = False
try:
    db.save_combined_report(base_diag_id, "race", jia_id, yi_new_id, ["S2"], {}, result)
except db.StaleSubmissionError:
    stale_jia = True
check("甲区换稿同样在保存时退回", stale_jia)
check("失败退回不影响已保存的原结果",
      db.latest_combined_report(base_diag_id)["id"] == rid)

# 拼表问题收集：缺报/无交集同时出现时一次性给出
try:
    combine.merge_submissions(
        base_map, ["S2"],
        {"id": jia_new, "zone": "甲", "readings": [
            {"segment_key": "S1", "src": "IN", "dst": "A",
             "meter_lo": 10, "meter_hi": 20},
            {"segment_key": "S2", "src": "A", "dst": "B",
             "meter_lo": 10, "meter_hi": 12}]},
        {"id": yi_new_id, "zone": "乙", "readings": [
            {"segment_key": "S2", "src": "B", "dst": "A",
             "meter_lo": -20, "meter_hi": -18},  # 正向 [18,20] 与甲 [10,12] 无交集
            {"segment_key": "S3", "src": "OUT", "dst": "B",
             "meter_lo": -20, "meter_hi": -10}]})
except MergeError as exc:
    check("交界无交集被识别",
          any("无交集" in i for i in exc.issues), str(exc.issues))
else:
    check("交界无交集被识别", False, "未抛出 MergeError")

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部换稿/换算服务层用例通过")
