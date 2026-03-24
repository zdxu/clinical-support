from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────
# Form 提取结果（Phase 1，含溯源）
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
    protocol_form: str = ""
    matched_templates: list = field(default_factory=list)  # 匹配到的模板文件名列表（可多个）
    is_new: bool = False                # True = 模板库没有，需新建
    confidence: str = "medium"          # "high" / "medium" / "low"
    reason: str = ""

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
    mappings: list = field(default_factory=list)     # list[FormMapping]
    high: list = field(default_factory=list)
    medium: list = field(default_factory=list)
    low: list = field(default_factory=list)
    new_forms: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mappings": [m.to_dict() for m in self.mappings],
            "high": [m.to_dict() for m in self.high],
            "medium": [m.to_dict() for m in self.medium],
            "low": [m.to_dict() for m in self.low],
            "new_forms": [m.to_dict() for m in self.new_forms],
        }


# ─────────────────────────────────────────────
# Phase 2：差异信号（新设计）
# ─────────────────────────────────────────────

@dataclass
class DiffSignal:
    """
    Protocol 相对于模板的差异信号。
    以模板字段为基准，只记录 Protocol 中有明确原文依据的例外描述。
    """
    diff_type: str = ""     # "exclude" / "append" / "override" / "condition"

    # exclude：模板有，Protocol 明确不收集 → field_ref = 模板 field_name
    # append：Protocol 要求收集，模板没有  → field_ref = 临时命名（大写）
    # override：字段存在但属性有差异       → field_ref = 模板 field_name
    # condition：字段收集有特殊条件        → field_ref = 模板 field_name
    field_ref: str = ""

    detail: dict = field(default_factory=dict)
    # append:    {"description": "Body Weight", "unit": "kg", "condition": ""}
    # override:  {"attribute": "units", "from": "", "to": "mmHg"}
    # condition: {"condition": "Females of childbearing potential only"}
    # exclude:   {}

    confidence: str = "medium"  # "high" / "medium" / "low"
    source_ref: str = ""        # PDF: "第45页" | docx: "章节标题 > 段落序号"
    source_text: str = ""       # 原文引用，禁止改写

    def to_dict(self) -> dict:
        return {
            "diff_type": self.diff_type,
            "field_ref": self.field_ref,
            "detail": self.detail,
            "confidence": self.confidence,
            "source_ref": self.source_ref,
            "source_text": self.source_text,
        }


@dataclass
class FormDiffResult:
    """一个 Form 的所有差异信号汇总"""
    form_name: str = ""
    template_file: str = ""             # 对应的模板文件名
    diff_signals: list = field(default_factory=list)   # list[DiffSignal]，全部
    excludes: list = field(default_factory=list)        # list[DiffSignal]
    appends: list = field(default_factory=list)         # list[DiffSignal]
    overrides: list = field(default_factory=list)       # list[DiffSignal]
    conditions: list = field(default_factory=list)      # list[DiffSignal]

    def to_dict(self) -> dict:
        return {
            "form_name": self.form_name,
            "template_file": self.template_file,
            "diff_signals": [s.to_dict() for s in self.diff_signals],
            "excludes": [s.to_dict() for s in self.excludes],
            "appends": [s.to_dict() for s in self.appends],
            "overrides": [s.to_dict() for s in self.overrides],
            "conditions": [s.to_dict() for s in self.conditions],
        }


# ─────────────────────────────────────────────
# 章节模型
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# 研究基本信息
# ─────────────────────────────────────────────

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


# ─────────────────────────────────────────────
# 字段变更（aggregator 输出）
# ─────────────────────────────────────────────

@dataclass
class FieldChange:
    """字段变更（DiffSignal → FieldChange 转换后的最终输出）"""
    change_type: str = ""   # "exclude" / "append" / "override"
    field_name: str = ""    # 对应模板的 field_name（append 为新字段名）
    detail: dict = field(default_factory=dict)
    # exclude:  模板字段上下文 {label, data_type, units}
    # append:   完整字段定义 {field_name, label, data_type, units, values, include_field_oid}
    # override: 属性差异 {attribute, from, to} 或条件 {attribute: "notes", value: "..."}
    reason: str = ""
    confidence: str = "medium"
    source_ref: str = ""
    source_text: str = ""

    def to_dict(self) -> dict:
        return {
            "change_type": self.change_type,
            "field_name": self.field_name,
            "detail": self.detail,
            "reason": self.reason,
            "confidence": self.confidence,
            "source_ref": self.source_ref,
            "source_text": self.source_text,
        }


# ─────────────────────────────────────────────
# 最终汇总
# ─────────────────────────────────────────────

@dataclass
class ProtocolExtraction:
    """最终汇总"""
    study_info: StudyInfo = field(default_factory=StudyInfo)
    fixed_forms: list = field(default_factory=list)              # 来源A，hardcode（list[str]）
    extracted_forms: list = field(default_factory=list)          # 来源B，Phase 1 提取（list[str]）
    extracted_form_findings: list = field(default_factory=list)  # 来源B，含溯源（list[FormFinding]）
    field_changes: dict = field(default_factory=dict)            # form_name → list[FieldChange]
    study_specific_forms: list = field(default_factory=list)     # 模板没有的新 Form（list[dict]）

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
        }
