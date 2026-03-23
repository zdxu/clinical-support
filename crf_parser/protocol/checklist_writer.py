"""
模块6：生成 Excel 审核表

输出 output/protocol/review_checklist.xlsx，共5个 Sheet：
  Sheet 0：Form 映射确认（新增）
  Sheet 1：Form 清单确认
  Sheet 2：字段变更审核
  Sheet 3：未知字段处理
  Sheet 4：新建 Form 字段草稿
"""

from pathlib import Path

from rich.console import Console

console = Console()

# 置信度颜色（PatternFill fgColor）
_COLOR_HIGH = "C6EFCE"    # 绿色
_COLOR_MEDIUM = "FFEB9C"  # 黄色
_COLOR_LOW = "FFC7CE"     # 红色
_COLOR_NEW = "BDD7EE"     # 蓝色（新建 Form）
_COLOR_HEADER_BG = "D9D9D9"  # 灰色（说明行背景）
_COLOR_HEADER_FONT = "404040"  # 深灰（表头字体）


def generate_checklist(
    extraction,
    history_forms: list,
    output_dir: str = "output/protocol",
    form_mapping_result=None,
) -> str:
    """
    生成 review_checklist.xlsx。

    extraction: ProtocolExtraction 对象
    history_forms: list[str]，history 中已有的 Form 名称
    form_mapping_result: FormMappingResult 对象（可选，有则写入 Sheet 0）
    Returns:
        输出文件路径
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        console.print("[red]缺少 openpyxl，请运行 pip install openpyxl[/red]")
        return ""

    Path(output_dir).mkdir(parents=True, exist_ok=True)
    output_path = Path(output_dir) / "review_checklist.xlsx"

    wb = Workbook()
    wb.remove(wb.active)  # 删除默认空白 Sheet

    # ─── Sheet 0：Form 映射确认（新增）───
    ws0 = wb.create_sheet("Form映射确认")
    _write_sheet0(ws0, form_mapping_result)

    # ─── Sheet 1：Form 清单确认 ───
    ws1 = wb.create_sheet("Form清单确认")
    _write_sheet1(ws1, extraction, history_forms)

    # ─── Sheet 2：字段变更审核 ───
    ws2 = wb.create_sheet("字段变更审核")
    _write_sheet2(ws2, extraction)

    # ─── Sheet 3：未知字段处理 ───
    ws3 = wb.create_sheet("未知字段处理")
    _write_sheet3(ws3, extraction)

    # ─── Sheet 4：新建Form字段草稿 ───
    ws4 = wb.create_sheet("新建Form字段草稿")
    _write_sheet4(ws4, extraction)

    wb.save(str(output_path))
    console.print(f"[green]审核表已生成：{output_path}[/green]")
    console.print(
        f"  Sheet0(Form映射): {ws0.max_row - 2} 行  "
        f"Sheet1(Form清单): {ws1.max_row - 2} 行  "
        f"Sheet2(字段变更): {ws2.max_row - 2} 行  "
        f"Sheet3(未知字段): {ws3.max_row - 2} 行  "
        f"Sheet4(新建Form): {ws4.max_row - 2} 行"
    )
    return str(output_path)


# ─────────────────────────────────────────────
# Sheet 0：Form 映射确认（新增）
# ─────────────────────────────────────────────

def _write_sheet0(ws, form_mapping_result):
    """Sheet 0：Form 映射确认，重点展示 medium/low 行供人工审核"""
    from openpyxl.styles import PatternFill, Font, Alignment

    _add_desc_row(
        ws,
        "Form 映射确认：请确认 Protocol 评估项目与模板库的映射关系，"
        "重点检查黄色（medium）和红色（low）行。"
        "审核结果列填写：✓确认 / ✗拒绝；拒绝时在\"修正映射\"列填写正确的模板文件名，或填\"新建\"",
    )
    headers = ["Protocol Form名", "匹配模板文件", "置信度", "映射理由", "审核结果", "修正映射"]
    _add_header_row(ws, headers)

    if not form_mapping_result:
        _auto_width(ws)
        ws.freeze_panes = "A3"
        return

    # 按 confidence 顺序输出：high → medium → low → new
    all_mappings = (
        list(getattr(form_mapping_result, "high", []))
        + list(getattr(form_mapping_result, "medium", []))
        + list(getattr(form_mapping_result, "low", []))
        + list(getattr(form_mapping_result, "new_forms", []))
    )

    for m in all_mappings:
        templates_str = ", ".join(m.matched_templates) if m.matched_templates else "（无匹配）"
        confidence_label = "new" if m.is_new else m.confidence
        row_data = [
            m.protocol_form,
            templates_str,
            confidence_label,
            m.reason,
            "",
            "",
        ]
        ws.append(row_data)

        cur_row = ws.max_row
        fill = _mapping_confidence_fill(confidence_label)
        if fill:
            for col in range(1, 7):
                ws.cell(cur_row, col).fill = fill

    _auto_width(ws)
    ws.freeze_panes = "A3"


# ─────────────────────────────────────────────
# Sheet 1：Form 清单确认（更新：新增提取置信度、来源定位、原文片段列）
# ─────────────────────────────────────────────

def _write_sheet1(ws, extraction, history_forms: list):
    from openpyxl.styles import PatternFill, Font, Alignment
    from openpyxl.utils import get_column_letter

    _add_desc_row(
        ws,
        "Form 清单确认：请检查每个 Form 是否正确。"
        "审核结果列填写：✓保留 / ✗移除 / 直接填写正确名称。",
    )
    headers = ["序号", "Form名称", "来源", "模板文件", "来源定位", "原文片段", "审核结果", "备注"]
    _add_header_row(ws, headers)

    history_lower = {f.lower() for f in history_forms}

    # 构建 form_name → FormFinding 的查找字典
    findings_lookup = {}
    for ff in getattr(extraction, "extracted_form_findings", []):
        name = ff.form_name if hasattr(ff, "form_name") else ff.get("form_name", "")
        findings_lookup[name] = ff

    idx = 1
    for form in extraction.fixed_forms:
        template_file = _form_to_filename(form) + ".yaml"
        ws.append([idx, form, "固定", template_file, "（固定清单）", "", "", ""])
        idx += 1

    for form in extraction.extracted_forms:
        if form.lower() in history_lower:
            source = "提取"
            template_file = _form_to_filename(form) + ".yaml"
        else:
            source = "新建"
            template_file = "（待新建）"

        ff = findings_lookup.get(form)
        if ff:
            source_ref = ff.source_ref if hasattr(ff, "source_ref") else ff.get("source_ref", "")
            source_text = ff.source_text if hasattr(ff, "source_text") else ff.get("source_text", "")
        else:
            source_ref, source_text = "", ""

        ws.append([idx, form, source, template_file, source_ref, source_text, "", ""])
        idx += 1

    _auto_width(ws)
    ws.freeze_panes = "A3"


# ─────────────────────────────────────────────
# Sheet 2：字段变更审核（更新：新增映射字段名、映射置信度列）
# ─────────────────────────────────────────────

def _write_sheet2(ws, extraction):
    from openpyxl.styles import PatternFill, Font, Alignment

    _add_desc_row(
        ws,
        "字段变更审核：红色=low置信度需处理，黄色=medium建议检查，绿色=high可直接确认。"
        "审核结果列填写：✓确认 / ✗拒绝 / 直接填写修改内容。"
        "\"映射字段名\"列帮助理解变更来源，\"映射置信度\"表示字段匹配本身的置信度。",
    )
    headers = [
        "Form", "变更类型", "字段名", "变更内容", "置信度",
        "映射字段名", "映射置信度",
        "来源定位", "原文片段", "审核结果",
    ]
    _add_header_row(ws, headers)

    for form_name, changes in extraction.field_changes.items():
        for change in changes:
            if not hasattr(change, "to_dict"):
                continue

            c = change
            detail_str = _dict_to_str(c.detail) if c.detail else ""
            row_data = [
                form_name,
                c.change_type,
                c.field_name,
                detail_str,
                c.confidence,
                getattr(c, "mapped_field_name", ""),
                getattr(c, "mapping_confidence", ""),
                c.source_ref,
                c.source_text,
                "",
            ]
            ws.append(row_data)

            cur_row = ws.max_row
            fill = _confidence_fill(c.confidence)
            if fill:
                ws.cell(cur_row, 5).fill = fill

    _auto_width(ws)
    ws.freeze_panes = "A3"


# ─────────────────────────────────────────────
# Sheet 3：未知字段处理
# ─────────────────────────────────────────────

def _write_sheet3(ws, extraction):
    _add_desc_row(ws, "未知字段处理：无法自动归属的字段，请在\"指定归属\"列填写 Form 名称，或填写\"忽略\"")
    headers = ["字段描述", "单位", "条件", "推测Form", "置信度", "来源定位", "原文片段", "指定归属"]
    _add_header_row(ws, headers)

    for f in extraction.unknown_findings:
        if hasattr(f, "to_dict"):
            row_data = [
                f.description,
                f.unit,
                f.condition,
                f.form_hint,
                f.confidence,
                f.source_ref,
                f.source_text,
                "",
            ]
        else:
            row_data = [str(f), "", "", "", "", "", "", ""]
        ws.append(row_data)

        cur_row = ws.max_row
        confidence = f.confidence if hasattr(f, "confidence") else "medium"
        fill = _confidence_fill(confidence)
        if fill:
            ws.cell(cur_row, 5).fill = fill

    _auto_width(ws)
    ws.freeze_panes = "A3"


# ─────────────────────────────────────────────
# Sheet 4：新建 Form 字段草稿
# ─────────────────────────────────────────────

def _write_sheet4(ws, extraction):
    _add_desc_row(ws, "新建Form字段草稿：模板中不存在的新 Form 字段定义，请在审核结果列填写：✓确认 / ✗删除 / 直接填写修改内容")
    headers = ["Form名称", "字段名", "Label", "DataType", "单位", "Values", "置信度", "来源定位", "原文", "审核结果"]
    _add_header_row(ws, headers)

    for form_draft in extraction.study_specific_forms:
        form_name = form_draft.get("form_name", "")
        for field in form_draft.get("fields", []):
            confidence = field.get("confidence", "medium")
            row_data = [
                form_name,
                field.get("field_name", ""),
                field.get("label", ""),
                field.get("data_type", ""),
                field.get("unit", ""),
                field.get("values", ""),
                confidence,
                field.get("source_ref", ""),
                field.get("source_text", ""),
                "",
            ]
            ws.append(row_data)

            cur_row = ws.max_row
            fill = _confidence_fill(confidence)
            if fill:
                ws.cell(cur_row, 7).fill = fill

    _auto_width(ws)
    ws.freeze_panes = "A3"


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _add_desc_row(ws, description: str):
    """添加灰色说明行"""
    from openpyxl.styles import PatternFill, Font, Alignment
    ws.append([description])
    cell = ws.cell(1, 1)
    cell.fill = PatternFill(start_color=_COLOR_HEADER_BG, end_color=_COLOR_HEADER_BG, fill_type="solid")
    cell.font = Font(color=_COLOR_HEADER_FONT, italic=True)
    cell.alignment = Alignment(wrap_text=True)
    # 合并到第一行末尾（先不合并，避免列数未知）


def _add_header_row(ws, headers: list):
    """添加加粗表头行"""
    from openpyxl.styles import PatternFill, Font, Alignment
    ws.append(headers)
    for col, _ in enumerate(headers, 1):
        cell = ws.cell(2, col)
        cell.font = Font(bold=True)
        cell.fill = PatternFill(start_color="BDD7EE", end_color="BDD7EE", fill_type="solid")
        cell.alignment = Alignment(horizontal="center")


def _confidence_fill(confidence: str):
    """根据置信度返回 PatternFill，不识别则返回 None"""
    from openpyxl.styles import PatternFill
    mapping = {
        "high": _COLOR_HIGH,
        "medium": _COLOR_MEDIUM,
        "low": _COLOR_LOW,
    }
    color = mapping.get(confidence.lower() if confidence else "")
    if color:
        return PatternFill(start_color=color, end_color=color, fill_type="solid")
    return None


def _mapping_confidence_fill(confidence_label: str):
    """Form 映射置信度颜色：high=绿，medium=黄，low=红，new=蓝"""
    from openpyxl.styles import PatternFill
    mapping = {
        "high": _COLOR_HIGH,
        "medium": _COLOR_MEDIUM,
        "low": _COLOR_LOW,
        "new": _COLOR_NEW,
    }
    color = mapping.get(confidence_label.lower() if confidence_label else "")
    if color:
        return PatternFill(start_color=color, end_color=color, fill_type="solid")
    return None


def _auto_width(ws, max_width: int = 60):
    """自适应列宽"""
    from openpyxl.utils import get_column_letter
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                val = str(cell.value) if cell.value else ""
                # 多行文本取第一行
                first_line = val.split("\n")[0]
                max_len = max(max_len, len(first_line))
            except Exception:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 2, max_width)


def _form_to_filename(form_name: str) -> str:
    import re
    name = form_name.lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def _dict_to_str(d: dict) -> str:
    if not d:
        return ""
    parts = []
    for k, v in d.items():
        if v:
            parts.append(f"{k}: {v}")
    return "  |  ".join(parts)
