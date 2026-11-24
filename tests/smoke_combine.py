#!/usr/bin/env python3
"""合报冒烟测试：甲乙分区上报、拼表与合报诊断（仅用标准库）。

用法: python3 tests/smoke_combine.py [base_url]   默认 http://127.0.0.1:8000
覆盖：
  1. 反向抄表（乙区对交界/独报管段整段反向，带负号区间）换算后账平；
  2. 交界段两区非负交集；缺报/重复/端点不符/无交集/取不到非负区间 -> 422 列问题；
  3. 非交界段两区同报、整段缺报 -> 422；
  4. 账不平也保存；按底图查最近成功合报、失败后原结果仍可查；
  5. 计算期间换稿 -> 409 退回，不产生新合报；
  6. 上报号不属于对应分区、底图诊断号不存在等入参校验。
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
FAILED = []


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + urllib.parse.quote(path, safe="/"),
                                 data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  | {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ---- 0. 先造一张底图（班次登记）：IN-A-B-C-OUT，其中 B-C 是甲乙交界 ----
# 守恒要求四段同流量，登记一组自洽读数（[100,120] 交集非空）得到诊断号。
base_segments = [
    {"key": "S1", "from_node": "IN", "to_node": "A",
     "capacity": 1000, "meter_lo": 100, "meter_hi": 120},
    {"key": "S2", "from_node": "A", "to_node": "B",
     "capacity": 1000, "meter_lo": 100, "meter_hi": 120},
    {"key": "S3", "from_node": "B", "to_node": "C",
     "capacity": 1000, "meter_lo": 100, "meter_hi": 120},
    {"key": "S4", "from_node": "C", "to_node": "OUT",
     "capacity": 1000, "meter_lo": 100, "meter_hi": 120},
]
s, r = call("POST", "/shifts/合报底图班/readings",
            {"ports": ["IN", "OUT"], "segments": base_segments})
check("底图登记成功", s == 201 and r.get("balanced") is True, f"got {s}")
base_diag = r["diagnosis_id"]


def submit(zone, readings):
    return call("POST", f"/zones/{zone}/submissions", {"readings": readings})


def combine(jia_id, yi_id, boundary=("S3",), diag=base_diag):
    return call("POST", "/combined-reports",
                {"base_diagnosis_id": diag,
                 "boundary_segment_keys": list(boundary),
                 "jia_submission_id": jia_id,
                 "yi_submission_id": yi_id})


# ---- 1. 正常合报 + 反向抄表 ----
# 甲区报 S1、S2 和交界 S3，方向与底图相同；
# 乙区把交界 S3 和独报 S4 整段反向抄表（C->B、OUT->C），用负号区间。
# 底图方向流量 105：反向计量值 = -105，故报 [-110,-100]。
s, rj = submit("甲", [
    {"segment_key": "S1", "from_node": "IN", "to_node": "A",
     "meter_lo": 100, "meter_hi": 110},
    {"segment_key": "S2", "from_node": "A", "to_node": "B",
     "meter_lo": 100, "meter_hi": 110},
    {"segment_key": "S3", "from_node": "B", "to_node": "C",
     "meter_lo": 100, "meter_hi": 110},
])
check("甲区上报返回 201", s == 201, f"got {s}")
jia1 = rj["submission_id"]

s, ry = submit("乙", [
    {"segment_key": "S3", "from_node": "C", "to_node": "B",
     "meter_lo": -110, "meter_hi": -100},
    {"segment_key": "S4", "from_node": "OUT", "to_node": "C",
     "meter_lo": -110, "meter_hi": -100},
])
check("乙区反向抄表上报返回 201", s == 201, f"got {s}")
yi1 = ry["submission_id"]

s, rc = combine(jia1, yi1)
check("反向抄表合报返回 201", s == 201, f"got {s}: {json.dumps(rc, ensure_ascii=False)}")
check("反向换算后账平", rc.get("result", {}).get("balanced") is True,
      rc.get("result", {}).get("message"))
merged = {x["key"]: x for x in rc.get("inputs", {}).get("segments", [])}
check("交界 S3 取两区非负交集 [100,110]",
      merged.get("S3", {}).get("meter_lo") == 100
      and merged.get("S3", {}).get("meter_hi") == 110
      and merged.get("S3", {}).get("source") == "boundary",
      json.dumps(merged.get("S3"), ensure_ascii=False))
check("反向独报 S4 换算为正向 [100,110]",
      merged.get("S4", {}).get("meter_lo") == 100
      and merged.get("S4", {}).get("meter_hi") == 110
      and merged.get("S4", {}).get("source") == "乙区独报",
      json.dumps(merged.get("S4"), ensure_ascii=False))
check("底表记录了所用两个上报号",
      rc.get("inputs", {}).get("submission_ids") == {"甲": jia1, "乙": yi1})
report1 = rc.get("combined_report_id")

# ---- 2. 按底图查询：最近成功合报 ----
s, rq = call("GET", f"/combined-reports/by-base/{base_diag}")
check("按底图查询返回 200", s == 200, f"got {s}")
check("最近一次合报即 report1",
      rq.get("latest", {}).get("id") == report1
      and rq["combined_reports"][0]["id"] == report1)
s, rq1 = call("GET", f"/combined-reports/{report1}")
check("按合报号可查原结果", s == 200 and rq1.get("id") == report1)

# ---- 3. 账不平也保存 ----
# 甲区把 S1 报成 [300,320]，其余不变 -> 全网守恒被破坏，账不平但应 201 入库。
s, rj2 = submit("甲", [
    {"segment_key": "S1", "from_node": "IN", "to_node": "A",
     "meter_lo": 300, "meter_hi": 320},
    {"segment_key": "S2", "from_node": "A", "to_node": "B",
     "meter_lo": 100, "meter_hi": 110},
    {"segment_key": "S3", "from_node": "B", "to_node": "C",
     "meter_lo": 100, "meter_hi": 110},
])
jia2 = rj2["submission_id"]
s, rc2 = combine(jia2, yi1)
check("账不平的合报也返回 201 并保存", s == 201
      and rc2.get("result", {}).get("balanced") is False, f"got {s}")
check("账不平给出嫌疑表 S1",
      {x["segment_key"] for x in rc2["result"].get("suspects", [])} == {"S1"})
s, rq = call("GET", f"/combined-reports/by-base/{base_diag}")
check("最近成功合报更新为账不平那次",
      rq.get("latest", {}).get("id") == rc2["combined_report_id"]
      and len(rq["combined_reports"]) == 2)

# ---- 4. 拼表失败：问题一次性列出，422 且不写库 ----
def expect_issues(name, jia_readings, yi_readings, needles,
                  boundary=("S3",)):
    _, rja = submit("甲", jia_readings)
    _, rya = submit("乙", yi_readings)
    st, rr = combine(rja["submission_id"], rya["submission_id"], boundary)
    issues = rr.get("detail", {}).get("issues", [])
    joined = " | ".join(issues)
    ok = st == 422 and all(any(n in i for i in issues) for n in needles)
    check(name, ok, f"got {st} issues={joined}")
    return st, issues


# 4a. 交界段乙区缺报 + 非交界 S1 两区同报 + S4 整段缺报
expect_issues(
    "交界缺报/非交界同报/整段缺报一次列出",
    [{"segment_key": "S1", "from_node": "IN", "to_node": "A",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S2", "from_node": "A", "to_node": "B",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S3", "from_node": "B", "to_node": "C",
      "meter_lo": 100, "meter_hi": 110}],
    [{"segment_key": "S1", "from_node": "IN", "to_node": "A",
      "meter_lo": 100, "meter_hi": 110}],
    ["交界管段 S3 须两区都报", "非交界管段 S1 须一区独报",
     "非交界管段 S4 缺报"])

# 4b. 端点与底图不符
expect_issues(
    "端点不符被退回",
    [{"segment_key": "S1", "from_node": "IN", "to_node": "A",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S2", "from_node": "A", "to_node": "B",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S3", "from_node": "B", "to_node": "X",
      "meter_lo": 100, "meter_hi": 110}],
    [{"segment_key": "S3", "from_node": "C", "to_node": "B",
      "meter_lo": -110, "meter_hi": -100},
     {"segment_key": "S4", "from_node": "OUT", "to_node": "C",
      "meter_lo": -110, "meter_hi": -100}],
    ["管段 S3 的端点", "与底图 B→C 不符"])

# 4c. 交界段两区区间无交集
expect_issues(
    "交界段非负区间无交集被退回",
    [{"segment_key": "S1", "from_node": "IN", "to_node": "A",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S2", "from_node": "A", "to_node": "B",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S3", "from_node": "B", "to_node": "C",
      "meter_lo": 100, "meter_hi": 110}],
    [{"segment_key": "S3", "from_node": "B", "to_node": "C",
      "meter_lo": 200, "meter_hi": 220},
     {"segment_key": "S4", "from_node": "C", "to_node": "OUT",
      "meter_lo": 100, "meter_hi": 110}],
    ["交界管段 S3 两区非负区间无交集"])

# 4d. 反向抄表整段落在负半轴，换向后取不到非负区间
expect_issues(
    "反向区间换算后无非负取值被退回",
    [{"segment_key": "S1", "from_node": "IN", "to_node": "A",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S2", "from_node": "A", "to_node": "B",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S3", "from_node": "B", "to_node": "C",
      "meter_lo": 100, "meter_hi": 110}],
    [{"segment_key": "S3", "from_node": "C", "to_node": "B",
      "meter_lo": -110, "meter_hi": -100},
     {"segment_key": "S4", "from_node": "OUT", "to_node": "C",
      "meter_lo": 50, "meter_hi": 80}],
    ["S4 换向后取不到非负区间"])

# 4e. 上报底图不存在的管段 + 交界管段本身不在底图
expect_issues(
    "底图外管段与错误交界声明被退回",
    [{"segment_key": "S1", "from_node": "IN", "to_node": "A",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S2", "from_node": "A", "to_node": "B",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S3", "from_node": "B", "to_node": "C",
      "meter_lo": 100, "meter_hi": 110},
     {"segment_key": "S9", "from_node": "A", "to_node": "B",
      "meter_lo": 1, "meter_hi": 2}],
    [{"segment_key": "S3", "from_node": "C", "to_node": "B",
      "meter_lo": -110, "meter_hi": -100},
     {"segment_key": "S4", "from_node": "OUT", "to_node": "C",
      "meter_lo": -110, "meter_hi": -100}],
    ["管段 S9 不在底图中"],
    boundary=("S3", "S8"))

# ---- 5. 失败不写库：合报数量仍是 2（report1 与账不平那次）----
s, rq = call("GET", f"/combined-reports/by-base/{base_diag}")
check("全部拼表失败均未写库（仍为 2 份成功合报）",
      len(rq.get("combined_reports", [])) == 2,
      f"count={len(rq.get('combined_reports', []))}")
check("失败后原结果仍可查",
      call("GET", f"/combined-reports/{report1}")[0] == 200)

# ---- 6. 换稿退回：用旧上报号合报，期间该区已传新稿 -> 409 ----
# 先各传一稿自洽数据；再在合报前给乙区换一份新稿，拿旧 yi 号请求。
_, rja = submit("甲", [
    {"segment_key": "S1", "from_node": "IN", "to_node": "A",
     "meter_lo": 100, "meter_hi": 110},
    {"segment_key": "S2", "from_node": "A", "to_node": "B",
     "meter_lo": 100, "meter_hi": 110},
    {"segment_key": "S3", "from_node": "B", "to_node": "C",
     "meter_lo": 100, "meter_hi": 110}])
_, rya_old = submit("乙", [
    {"segment_key": "S3", "from_node": "C", "to_node": "B",
     "meter_lo": -110, "meter_hi": -100},
    {"segment_key": "S4", "from_node": "OUT", "to_node": "C",
     "meter_lo": -110, "meter_hi": -100}])
_, _ = submit("乙", [  # 乙区换稿（更正）
    {"segment_key": "S3", "from_node": "C", "to_node": "B",
     "meter_lo": -108, "meter_hi": -102},
    {"segment_key": "S4", "from_node": "OUT", "to_node": "C",
     "meter_lo": -108, "meter_hi": -102}])
st, rr = combine(rja["submission_id"], rya_old["submission_id"])
check("乙区换稿后旧上报号合报返回 409", st == 409, f"got {st}: {rr.get('detail')}")
s, rq = call("GET", f"/combined-reports/by-base/{base_diag}")
check("409 退回未产生新合报（仍为 2 份）",
      len(rq.get("combined_reports", [])) == 2)

# ---- 7. 入参校验 ----
st, rr = combine(999999, yi1)
check("底图诊断号不存在返回 422", st == 422, f"got {st}")
st, rr = call("POST", "/combined-reports",
              {"base_diagnosis_id": base_diag,
               "boundary_segment_keys": ["S3"],
               "jia_submission_id": yi1,  # 乙区号给甲区
               "yi_submission_id": yi1})
check("上报号分区不符返回 422", st == 422, f"got {st}: {rr.get('detail')}")
st, _ = call("POST", "/zones/丙/submissions", {"readings": []})
check("非法分区返回 422", st == 422, f"got {st}")
st, rr = submit("甲", [
    {"segment_key": "S1", "from_node": "IN", "to_node": "A",
     "meter_lo": 110, "meter_hi": 100}])
check("区间倒置返回 422", st == 422, f"got {st}")
st, rr = submit("甲", [
    {"segment_key": "S1", "from_node": "IN", "to_node": "A",
     "meter_lo": 1, "meter_hi": 2},
    {"segment_key": "S1", "from_node": "IN", "to_node": "A",
     "meter_lo": 3, "meter_hi": 4}])
check("同区重复管段在上报时即 422", st == 422, f"got {st}")

# ---- 8. 版本查询 ----
s, rl = call("GET", "/zones/甲/submissions/latest")
check("甲区最新上报可查", s == 200 and rl["id"] == rja["submission_id"])
s, rls = call("GET", "/zones/甲/submissions")
check("甲区上报版本列表非空且新的在前",
      s == 200 and rls["submissions"][0]["id"] == rja["submission_id"]
      and len(rls["submissions"]) >= 4)

print()
if FAILED:
    print(f"共 {len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部合报冒烟用例通过")
