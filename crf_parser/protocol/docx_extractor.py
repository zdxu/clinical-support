"""
模块2（docx专用）：章节内容提取

PDF 不需要此模块（截图复用已有的 pdf_to_images）。
"""

import json
import time
from pathlib import Path

from rich.console import Console

from .models import Chapter, ChapterContent, StudyInfo

console = Console()

# 用于区分 body 元素类型的 XML 命名空间
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def extract_chapter_content(docx_path: str, chapter: Chapter) -> ChapterContent:
    """
    提取 docx 中某章节（para_range）的段落和表格内容。

    docx body 中段落和表格交替出现，需按顺序处理。
    """
    from docx import Document
    from docx.oxml.ns import qn
    from lxml import etree

    doc = Document(docx_path)
    body = doc.element.body

    # 建立 element_order：按 body 子元素顺序记录 ('para', idx) 或 ('tbl', idx)
    para_counter = 0
    tbl_counter = 0
    element_order = []

    for child in body:
        tag = child.tag
        if tag == qn("w:p"):
            element_order.append(("para", para_counter))
            para_counter += 1
        elif tag == qn("w:tbl"):
            element_order.append(("tbl", tbl_counter))
            tbl_counter += 1

    # para_range
    start_para, end_para = chapter.para_range if chapter.para_range else (0, len(doc.paragraphs) - 1)

    # 找到 element_order 中 para_idx 在 [start_para, end_para] 范围内的所有元素
    # 以第一个命中的 para 作为章节起点，最后一个命中的 para 作为终点
    in_range = False
    selected = []
    for elem_type, elem_idx in element_order:
        if elem_type == "para":
            if elem_idx == start_para:
                in_range = True
            if in_range:
                selected.append((elem_type, elem_idx))
            if elem_idx == end_para:
                break
        elif elem_type == "tbl" and in_range:
            selected.append((elem_type, elem_idx))

    paragraphs_out = []
    tables_out = []
    text_parts = []

    for elem_type, elem_idx in selected:
        if elem_type == "para":
            para = doc.paragraphs[elem_idx]
            text = para.text.strip()
            style = para.style.name if para.style else "Normal"
            paragraphs_out.append({"index": elem_idx, "text": text, "style": style})
            if text:
                text_parts.append(text)

        elif elem_type == "tbl":
            table = doc.tables[elem_idx]
            rows = []
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                rows.append(cells)
            tables_out.append({"rows": rows})
            # 表格转文本
            for row in rows:
                text_parts.append(" | ".join(row))
            text_parts.append("")  # 表格后空行

    full_text = "\n".join(text_parts)

    return ChapterContent(
        chapter=chapter,
        paragraphs=paragraphs_out,
        tables=tables_out,
        full_text=full_text,
    )


def extract_study_info_from_docx(docx_path: str, client) -> StudyInfo:
    """读取 docx 前20段，调用 LLM 提取研究基本信息"""
    from docx import Document
    doc = Document(docx_path)

    # 取前20个非空段落
    lines = []
    for para in doc.paragraphs[:40]:
        text = para.text.strip()
        if text:
            lines.append(text)
        if len(lines) >= 20:
            break

    text_block = "\n".join(lines)

    system_prompt = (
        "你是临床试验文档解析助手。\n"
        "从以下 Protocol 文档开头内容提取研究基本信息，输出 JSON：\n"
        '{"project_name": "", "protocol_number": "", "protocol_version": "", '
        '"protocol_date": "", "indication": "", "phase": ""}\n'
        "只输出 JSON，不要其他文字。"
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"文档内容：\n{text_block}"},
                ],
            )
            raw = response.choices[0].message.content.strip()
            raw = _strip_code_block(raw)
            data = json.loads(raw)
            return StudyInfo(
                project_name=data.get("project_name", ""),
                protocol_number=data.get("protocol_number", ""),
                protocol_version=data.get("protocol_version", ""),
                protocol_date=data.get("protocol_date", ""),
                indication=data.get("indication", ""),
                phase=data.get("phase", ""),
            )
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
            else:
                console.print(f"[red]study_info 提取失败：{e}[/red]")
                return StudyInfo()


def extract_study_info_from_pdf(image_paths: list, client) -> StudyInfo:
    """从 PDF 前几页截图提取研究基本信息"""
    import base64

    images_content = []
    for p in image_paths[:5]:
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
        images_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}"},
        })
    images_content.append({
        "type": "text",
        "text": "请提取该 Protocol 文档的研究基本信息，返回 JSON。",
    })

    system_prompt = (
        "你是临床试验文档解析助手。\n"
        "从以下 Protocol 文档图片提取研究基本信息，输出 JSON：\n"
        '{"project_name": "", "protocol_number": "", "protocol_version": "", '
        '"protocol_date": "", "indication": "", "phase": ""}\n'
        "只输出 JSON，不要其他文字。"
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": images_content},
                ],
            )
            raw = response.choices[0].message.content.strip()
            raw = _strip_code_block(raw)
            data = json.loads(raw)
            return StudyInfo(
                project_name=data.get("project_name", ""),
                protocol_number=data.get("protocol_number", ""),
                protocol_version=data.get("protocol_version", ""),
                protocol_date=data.get("protocol_date", ""),
                indication=data.get("indication", ""),
                phase=data.get("phase", ""),
            )
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
            else:
                console.print(f"[red]study_info 提取失败：{e}[/red]")
                return StudyInfo()


def _strip_code_block(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
