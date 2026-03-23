from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
# Form 提取结果（含溯源）
# ─────────────────────────────────────────────

@dataclass
class FormFinding:
    """Phase 1 提取的单个 Form 名称（含溯源）"""
    form_name: str = ""   # Form 名称，如 "Vital Signs"
    source_ref: str = ""  # PDF: "第45页" | docx: "6.3 Assessments > 段落2"
    source_text: str = "" # 原文引用片段，禁止改写

    def to_dict(self) -> dict:
        return {
            "form_name": self.form_name,
            "source_ref": self.source_ref,
            "source_text": self.source_text,
        }


# ─────────────────────────────────────────────
# Form 级映射结果
# ─────────────────────────────────────────────

@dataclass
class FormMapping:
    """Protocol Form → 模板库文件的映射结果"""
    protocol_form: str = ""             # Protocol 中的原始 Form 名称
    matched_templates: list = field(default_factory=list)  # 匹配到的模板文件名列表（可多个）
    is_new: bool = False                # True = 模板库没有，需新建
    confidence: str = "medium"          # "high" / "medium" / "low"
    reason: str = ""                    # LLM 的匹配理由说明

    def to_dict(self) -> dict:
        return {
            "protocol_form": self.protocol_form,
            "matched_templates": self.matched_templates,
            "is_new": self.is_new,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass
class FormMappingResult:
    """Form 级映射结果汇总"""
    mappings: list = field(default_factory=list)     # 全部映射结果 list[FormMapping]
    high: list = field(default_factory=list)         # 直接使用 list[FormMapping]
    medium: list = field(default_factory=list)       # 建议人工确认 list[FormMapping]
    low: list = field(default_factory=list)          # 必须人工确认 list[FormMapping]
    new_forms: list = field(default_factory=list)    # 需新建（is_new=True） list[FormMapping]

    def to_dict(self) -> dict:
        return {
            "mappings": [m.to_dict() for m in self.mappings],
            "high": [m.to_dict() for m in self.high],
            "medium": [m.to_dict() for m in self.medium],
            "low": [m.to_dict() for m in self.low],
            "new_forms": [m.to_dict() for m in self.new_forms],
        }


# ─────────────────────────────────────────────
# 字段级映射结果
# ─────────────────────────────────────────────

@dataclass
class FieldMapping:
    """Protocol 字段描述 → 模板 field_name 的映射结果"""
    protocol_description: str = ""     # Protocol 中的原始字段描述
    matched_field_name: str = ""       # 匹配到的模板 field_name，无匹配则空字符串
    is_new: bool = False               # True = 模板没有此字段，需 append
    confidence: str = "medium"         # "high" / "medium" / "low"
    reason: str = ""                   # LLM 的匹配理由
    # 保留原始 FieldFinding 的溯源信息
    source_ref: str = ""
    source_text: str = ""

    def to_dict(self) -> dict:
        return {
            "protocol_description": self.protocol_description,
            "matched_field_name": self.matched_field_name,
            "is_new": self.is_new,
            "confidence": self.confidence,
            "reason": self.reason,
            "source_ref": self.source_ref,
            "source_text": self.source_text,
        }


@dataclass
class FieldMappingResult:
    """字段级映射结果汇总"""
    form_name: str = ""
    mappings: list = field(default_factory=list)     # 全部映射结果 list[FieldMapping]
    matched: list = field(default_factory=list)      # is_new=False，用于 exclude/override 判断
    unmatched: list = field(default_factory=list)    # is_new=True，直接进入 append 列表
    ambiguous: list = field(default_factory=list)    # confidence=low，进入审核表人工处理

    def to_dict(self) -> dict:
        return {
            "form_name": self.form_name,
            "mappings": [m.to_dict() for m in self.mappings],
            "matched": [m.to_dict() for m in self.matched],
            "unmatched": [m.to_dict() for m in self.unmatched],
            "ambiguous": [m.to_dict() for m in self.ambiguous],
        }


@dataclass
class Chapter:
    """章节（PDF 和 docx 统一格式）"""
    title: str
    page_range: Optional[tuple] = None   # PDF 用（物理页码起止）
    para_range: Optional[tuple] = None   # docx 用（段落索引起止）

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "page_range": list(self.page_range) if self.page_range else None,
            "para_range": list(self.para_range) if self.para_range else None,
        }


@dataclass
class ChapterContent:
    """docx 章节内容"""
    chapter: Chapter
    paragraphs: list  # [{"index": 5, "text": "...", "style": "Normal"}]
    tables: list      # [{"rows": [["col1","col2"], ["v1","v2"]]}]
    full_text: str    # 段落 + 表格合并的纯文本，供 LLM 读取

    def to_dict(self) -> dict:
        return {
            "chapter": self.chapter.to_dict(),
            "paragraphs": self.paragraphs,
            "tables": self.tables,
            "full_text": self.full_text,
        }


@dataclass
class StudyInfo:
    """研究基本信息"""
    project_name: str = ""
    protocol_number: str = ""
    protocol_version: str = ""
    protocol_date: str = ""
    indication: str = ""
    phase: str = ""

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "protocol_number": self.protocol_number,
            "protocol_version": self.protocol_version,
            "protocol_date": self.protocol_date,
            "indication": self.indication,
            "phase": self.phase,
        }


@dataclass
class FieldFinding:
    """单条字段提取结果（含溯源）"""
    description: str = ""       # 字段描述，如 "Systolic Blood Pressure"
    unit: str = ""              # 单位，无则空字符串
    condition: str = ""         # 特殊条件，如 "after 5 min rest"，无则空字符串
    form_hint: str = ""         # 推测的 Form 归属，无法判断则空字符串
    confidence: str = "medium"  # "high" / "medium" / "low"
    source_ref: str = ""        # PDF: "第45页" | docx: "6.2 Vital Signs > 段落3"
    source_text: str = ""       # 原文片段，直接引用原文，禁止改写
    assigned_form: str = ""     # 归并后的 Form（由 aggregator 填写）

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "unit": self.unit,
            "condition": self.condition,
            "form_hint": self.form_hint,
            "confidence": self.confidence,
            "source_ref": self.source_ref,
            "source_text": self.source_text,
            "assigned_form": self.assigned_form,
        }


@dataclass
class FieldChange:
    """字段变更（对比模板后）"""
    change_type: str = ""   # "exclude" / "append" / "override"
    field_name: str = ""    # 对应模板的 field_name
    detail: dict = field(default_factory=dict)  # append/override 时的具体内容；exclude 时为模板字段上下文
    reason: str = ""        # 变更原因
    confidence: str = "medium"
    # ── Protocol 侧溯源 ──
    protocol_description: str = ""  # 触发本条变更的原始 Protocol 字段描述（append/override 有；exclude 无）
    source_ref: str = ""            # Protocol 原文位置（append/override 有；exclude 无）
    source_text: str = ""           # Protocol 原文片段（append/override 有；exclude 无）
    # ── 映射溯源 ──
    mapped_field_name: str = ""      # 映射模块B匹配到的 field_name
    mapping_confidence: str = ""     # 字段映射本身的置信度

    def to_dict(self) -> dict:
        return {
            "change_type": self.change_type,
            "field_name": self.field_name,
            "detail": self.detail,
            "reason": self.reason,
            "confidence": self.confidence,
            "protocol_description": self.protocol_description,
            "source_ref": self.source_ref,
            "source_text": self.source_text,
            "mapped_field_name": self.mapped_field_name,
            "mapping_confidence": self.mapping_confidence,
        }


@dataclass
class ProtocolExtraction:
    """最终汇总"""
    study_info: StudyInfo = field(default_factory=StudyInfo)
    fixed_forms: list = field(default_factory=list)       # 来源A，hardcode（list[str]）
    extracted_forms: list = field(default_factory=list)   # 来源B，Phase 1 提取（list[str]）
    extracted_form_findings: list = field(default_factory=list)  # 来源B，含溯源（list[FormFinding]）
    field_changes: dict = field(default_factory=dict)     # form_name → list[FieldChange]
    study_specific_forms: list = field(default_factory=list)  # 模板没有的新 Form
    unknown_findings: list = field(default_factory=list)  # 无法归属，待人工处理

    def to_dict(self) -> dict:
        return {
            "study_info": self.study_info.to_dict(),
            "fixed_forms": self.fixed_forms,
            "extracted_forms": self.extracted_forms,
            "extracted_form_findings": [
                f.to_dict() if hasattr(f, "to_dict") else f
                for f in self.extracted_form_findings
            ],
            "field_changes": {
                k: [c.to_dict() if hasattr(c, "to_dict") else c for c in v]
                for k, v in self.field_changes.items()
            },
            "study_specific_forms": self.study_specific_forms,
            "unknown_findings": [
                f.to_dict() if hasattr(f, "to_dict") else f
                for f in self.unknown_findings
            ],
        }
