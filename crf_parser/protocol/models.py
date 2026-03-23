from dataclasses import dataclass, field
from typing import Optional


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
    detail: dict = field(default_factory=dict)  # append/override 时的具体内容
    reason: str = ""        # 变更原因（来自 source_text）
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


@dataclass
class ProtocolExtraction:
    """最终汇总"""
    study_info: StudyInfo = field(default_factory=StudyInfo)
    fixed_forms: list = field(default_factory=list)       # 来源A，hardcode
    extracted_forms: list = field(default_factory=list)   # 来源B，Phase 1 提取
    field_changes: dict = field(default_factory=dict)     # form_name → list[FieldChange]
    study_specific_forms: list = field(default_factory=list)  # 模板没有的新 Form
    unknown_findings: list = field(default_factory=list)  # 无法归属，待人工处理

    def to_dict(self) -> dict:
        return {
            "study_info": self.study_info.to_dict(),
            "fixed_forms": self.fixed_forms,
            "extracted_forms": self.extracted_forms,
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
