from dataclasses import dataclass, field
from typing import Optional


@dataclass
class PageImage:
    page_number: int               # PDF 中的物理页码（从1开始）
    image_path: str                # 截图文件路径
    form_name: str                 # 从页头识别出的 Form 名称
    project_name: str
    version: str
    generated_on: str
    block_type: str                # "view" 或 "field_meta"（由状态机分配）


@dataclass
class FormPages:
    form_name: str
    project_name: str
    version: str
    generated_on: str
    view_pages: list               # list[PageImage] 第1段页面（视图块，按页码排序）
    field_meta_pages: list         # list[PageImage] 第2段页面（元定义块，按页码排序）


@dataclass
class FieldDef:
    index: int                     # 序号（两块内容的连接键）
    label: str                     # 视图块的显示文字
    interaction: str               # 'text' / 'checkbox' / 'dropdown'
    interaction_options: list      # list[str] checkbox → ['Yes','No']，其他 → []
    field_name: str                # 元定义块的 Field Name（如 VISDAT）
    data_type: str                 # 如 '$1'、'$200'、'dd MMM yyyy'
    units: str                     # 通常为空字符串
    values: str                    # 如 'Y=Yes\nN=No'，无则空字符串
    pre_filled_values: str         # 预填值，通常为空
    include_field_oid: str         # 通常与 field_name 相同
    deprecated: bool = False       # history 合并时标记已废弃的字段
    deprecated_since: str = ""     # 标记废弃来源的 CRF 文件名


@dataclass
class FormSpec:
    project_name: str
    version: str
    form_name: str
    generated_on: str
    source_crf: str                # 来源 CRF 文件名，用于历史溯源
    fields: list                   # list[FieldDef]

    def to_dict(self) -> dict:
        """序列化为可写 YAML 的字典，fields 按 index 排序"""
        sorted_fields = sorted(self.fields, key=lambda f: f.index)
        fields_list = []
        for f in sorted_fields:
            fd = {
                "index": f.index,
                "label": f.label,
                "interaction": f.interaction,
                "interaction_options": f.interaction_options,
                "field_name": f.field_name,
                "data_type": f.data_type,
                "units": f.units,
                "values": f.values,
                "pre_filled_values": f.pre_filled_values,
                "include_field_oid": f.include_field_oid,
                "deprecated": f.deprecated,
            }
            if f.deprecated and f.deprecated_since:
                fd["deprecated_since"] = f.deprecated_since
            fields_list.append(fd)

        return {
            "form_name": self.form_name,
            "project_name": self.project_name,
            "version": self.version,
            "generated_on": self.generated_on,
            "source_crf": self.source_crf,
            "fields": fields_list,
        }
