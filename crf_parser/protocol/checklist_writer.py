"""
模块6：生成 Excel 审核表

输出 output/protocol/review_checklist.xlsx，共4个 Sheet：
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
_COLOR_HEADER_BG = "D9D9D9"  # 灰色（说明行背景）
_COLOR_HEADER_FONT = "404040"  # 深灰（表头字体）


def generate_checklist(
    extraction,
    history_forms: list,
    output_dir: str = "output/protocol",
) -> str:
    """
    生成 review_checklist.xlsx。

    extraction: ProtocolExtraction 对象
    history_forms: list[str]，history 中已有的 Form 名称
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
        f"  Sheet1: {ws1.max_row - 2} 行  "
        f"Sheet2: {ws2.max_row - 2} 行  "
        f"Sheet3: {ws3.max_row - 2} 行  "
        f"Sheet4: {ws4.max_row - 2} 行"
    )
    return str(output_path)


# ─────────────────────────────────────────────
# Sheet 1：Form 清单确认
# ─────────────────────────────────────────────

def _write_sheet1(ws, extraction, history_forms: list):
    from openpyxl.styles import PatternFill, Font, Alignment
    from openpyxl.utils import get_column_letter

    _add_desc_row(ws, "Form 清单确认：请检查每个 Form 是否正确。审核结果列填写：✓保留 / ✗移除 / 直接填写正确名称")
    headers = ["序号", "Form名称", "来源", "模板文件", "审核结果", "备注"]
    _add_header_row(ws, headers)

    history_lower = {f.lower() for f in history_forms}

    row = 3
    idx = 1
    for form in extraction.fixed_forms:
        template_file = _form_to_filename(form) + ".yaml"
        ws.append([idx, form, "固定", template_file, "", ""])
        row += 1
        idx += 1

    for form in extraction.extracted_forms:
        if form.lower() in history_lower:
            source = "提取"
            template_file = _form_to_filename(form) + ".yaml"
        else:
            source = "新建"
            template_file = "（待新建）"
        ws.append([idx, form, source, template_file, "", ""])
        row += 1
        idx += 1

    _auto_width(ws)
    ws.freeze_panes = "A3"


# ─────────────────────────────────────────────
# Sheet 2：字段变更审核
# ─────────────────────────────────────────────

def _write_sheet2(ws, extraction):
    from openpyxl.styles import PatternFill, Font, Alignment

    _add_desc_row(ws, "字段变更审核：红色=low置信度需处理，黄色=medium建议检查，绿色=high可直接确认。审核结果列填写：✓确认 / ✗拒绝 / 直接填写修改内容")
    headers = ["Form", "变更类型", "字段名", "变更内容", "置信度", "来源定位", "原文片段", "审核结果"]
    _add_header_row(ws, headers)

    for form_name, changes in extraction.field_changes.items():
        for change in changes:
            if hasattr(change, "to_dict"):
                c = change
            else:
                continue

            detail_str = _dict_to_str(c.detail) if c.detail else ""
            row_data = [
                form_name,
                c.change_type,
                c.field_name,
                detail_str,
                c.confidence,
                c.source_ref,
                c.source_text,
                "",
            ]
            ws.append(row_data)

            # 置信度着色
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
