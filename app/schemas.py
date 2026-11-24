"""请求体的 Pydantic 校验模型。

既有班次接口的水量一律为非负整数升；分区上报允许**带符号整数升**，
负号表示按计量方向反向过水，合报拼表时按底图方向统一换算。
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_SEGMENTS = 20
_NAME = Field(min_length=1, max_length=50)
ZONES = ("甲", "乙")


class SegmentIn(BaseModel):
    key: str = _NAME
    from_node: str = _NAME
    to_node: str = _NAME
    capacity: int = Field(ge=0, description="过水量上限（升）")
    meter_lo: int = Field(ge=0, description="水表读数区间下界（升）")
    meter_hi: int = Field(ge=0, description="水表读数区间上界（升）")

    @model_validator(mode="after")
    def _interval_ok(self):
        if self.meter_lo > self.meter_hi:
            raise ValueError("水表读数区间下界不能大于上界")
        # 注意：读数区间高于过水量上限属于“疑似失准”，照常接收，
        # 交由诊断判定（求解时与上限取交集）。
        return self


class ReadingsIn(BaseModel):
    ports: list[str] = Field(default_factory=list,
                             description="进出水口节点（不要求进出平衡）")
    segments: list[SegmentIn] = Field(min_length=1, max_length=MAX_SEGMENTS)

    @field_validator("ports")
    @classmethod
    def _ports_ok(cls, v):
        for p in v:
            if not p or len(p) > 50:
                raise ValueError("进出水口名称须为 1~50 字符")
        if len(set(v)) != len(v):
            raise ValueError("进出水口名称重复")
        return v

    @model_validator(mode="after")
    def _keys_unique(self):
        keys = [s.key for s in self.segments]
        if len(set(keys)) != len(keys):
            raise ValueError("管段编号重复")
        return self


class RemeasureIn(BaseModel):
    segment_key: str = _NAME
    rlo: int = Field(ge=0, description="复测区间下界（升）")
    rhi: int = Field(ge=0, description="复测区间上界（升）")

    @model_validator(mode="after")
    def _interval_ok(self):
        if self.rlo > self.rhi:
            raise ValueError("复测区间下界不能大于上界")
        return self


class RemeasureBatch(BaseModel):
    remeasures: list[RemeasureIn] = Field(min_length=1, max_length=MAX_SEGMENTS)


# ---------------- 甲乙分区合报 ----------------

class ZoneReadingIn(BaseModel):
    """一条分区抄表：管段编号、计量方向的起止节点、带符号整数升区间。"""
    segment_key: str = _NAME
    from_node: str = _NAME
    to_node: str = _NAME
    meter_lo: int = Field(description="带符号读数区间下界（升，负值表示反向过水）")
    meter_hi: int = Field(description="带符号读数区间上界（升，负值表示反向过水）")

    @model_validator(mode="after")
    def _interval_ok(self):
        if self.meter_lo > self.meter_hi:
            raise ValueError("读数区间下界不能大于上界")
        return self


class ZoneSubmissionIn(BaseModel):
    """一区一次整份上报（更正即整份替换，生成新版本）。"""
    readings: list[ZoneReadingIn] = Field(default_factory=list,
                                          max_length=MAX_SEGMENTS)

    @model_validator(mode="after")
    def _keys_unique(self):
        keys = [r.segment_key for r in self.readings]
        if len(set(keys)) != len(keys):
            raise ValueError("同一区内管段编号重复")
        return self


class CombineIn(BaseModel):
    """合报请求：选定底图（诊断号）、交界管段与两区所用的上报号。"""
    base_diagnosis_id: int = Field(gt=0, description="选定管网底图所用的诊断号")
    boundary_segment_keys: list[str] = Field(
        default_factory=list, description="交界管段编号（底图方向，须两区同报）")
    jia_submission_id: int = Field(gt=0, description="甲区本次合报使用的上报号")
    yi_submission_id: int = Field(gt=0, description="乙区本次合报使用的上报号")

    @field_validator("boundary_segment_keys")
    @classmethod
    def _boundary_unique(cls, v):
        if len(set(v)) != len(v):
            raise ValueError("交界管段编号重复")
        return v
