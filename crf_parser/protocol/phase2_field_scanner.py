"""
模块4：Phase 2 提取字段描述

对每个章节提取字段描述 + 溯源三件套（source_ref, source_text, confidence）。
"""

import base64
import json
import time
from pathlib import Path

from rich.console import Console

from .models import Chapter, FieldFinding

console = Console()

_SYSTEM_PROMPT_PDF = (
    "你是临床试验 Protocol 解析专家。\n"
    "提取该章节中所有具体的数据收集字段信息。\n"
    "对每条信息必须包含：\n"
    "1. description：字段名称或描述\n"
    "2. unit：单位（无则空字符串）\n"
    "3. condition：特殊收集条件（无则空字符串）\n"
    "4. form_hint：推测的 Form 归属\n"
    "   - 章节或内容明确提到 Form 名 → 填写\n"
    "   - 无法判断 → 空字符串（不要猜测）\n"
    "5. confidence：\n"
    "   - \"high\"   = 原文明确说明字段名、单位、Form 归属\n"
    "   - \"medium\" = 能推断归属但不够明确\n"
    "   - \"low\"    = 原文模糊或归属不确定\n"
    "6. source_ref：填写 \"第X页\"（使用传入的页码）\n"
    "7. source_text：直接引用原文片段（1-2句），禁止改写或总结\n\n"
    "没有字段级信息则 findings 返回空数组。\n"
    "只输出 JSON，不要有其他文字：\n"
    "{\"findings\": [{\"description\": \"Systolic Blood Pressure\", \"unit\": \"mmHg\", "
    "\"condition\": \"after 5 minutes rest\", \"form_hint\": \"Vital Signs\", "
    "\"confidence\": \"high\", \"source_ref\": \"第45页\", "
    "\"source_text\": \"Blood pressure (systolic and diastolic) will be measured...\"}]}"
)

_SYSTEM_PROMPT_DOCX = (
    "你是临床试验 Protocol 解析专家。\n"
    "提取该章节中所有具体的数据收集字段信息。\n"
    "对每条信息必须包含：\n"
    "1. description：字段名称或描述\n"
    "2. unit：单位（无则空字符串）\n"
    "3. condition：特殊收集条件（无则空字符串）\n"
    "4. form_hint：推测的 Form 归属\n"
    "   - 章节或内容明确提到 Form 名 → 填写\n"
    "   - 无法判断 → 空字符串（不要猜测）\n"
    "5. confidence：\n"
    "   - \"high\"   = 原文明确说明字段名、单位、Form 归属\n"
    "   - \"medium\" = 能推断归属但不够明确\n"
    "   - \"low\"    = 原文模糊或归属不确定\n"
    "6. source_ref：填写 \"{chapter_title} > 段落{索引}\"\n"
    "7. source_text：直接引用原文片段（1-2句），禁止改写或总结\n\n"
    "没有字段级信息则 findings 返回空数组。\n"
    "只输出 JSON，不要有其他文字：\n"
    "{\"findings\": [{\"description\": \"Body Weight\", \"unit\": \"kg\", "
    "\"condition\": \"\", \"form_hint\": \"Vital Signs\", "
    "\"confidence\": \"high\", \"source_ref\": \"6.2 Vital Signs > 段落3\", "
    "\"source_text\": \"Body weight (kg) will be recorded at each visit.\"}]}"
)


def scan_chapter_for_fields(
    chapter_title: str,
    content,
    file_type: str,
    client,
) -> list:
    """
    扫描单个章节，提取 FieldFinding 列表。

    content:
        file_type="pdf"  → list of (物理页码, 图片路径) tuples
        file_type="docx" → full_text 字符串
    """
    for attempt in range(3):
        try:
            if file_type == "pdf":
                img_content, page_refs = _build_pdf_content_with_refs(content)
                user_text = (
                    f"章节标题：{chapter_title}\n"
                    f"图片页码分别为：{page_refs}"
                )
                img_content.append({"type": "text", "text": user_text})
                messages = [
                    {"role": "system", "content": _SYSTEM_PROMPT_PDF},
                    {"role": "user", "content": img_content},
                ]
            else:
                user_text = f"章节标题：{chapter_title}\n\n章节内容：\n{content}"
                messages = [
                    {
                        "role": "system",
                        "content": _SYSTEM_PROMPT_DOCX.replace("{chapter_title}", chapter_title),
                    },
                    {"role": "user", "content": user_text},
                ]

            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=2048,
                messages=messages,
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            data = json.loads(raw)

            findings = []
            for item in data.get("findings", []):
                findings.append(FieldFinding(
                    description=item.get("description", ""),
                    unit=item.get("unit", ""),
                    condition=item.get("condition", ""),
                    form_hint=item.get("form_hint", ""),
                    confidence=item.get("confidence", "medium"),
                    source_ref=item.get("source_ref", ""),
                    source_text=item.get("source_text", ""),
                ))
            return findings

        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]Phase2 章节扫描失败 [{chapter_title}]: {e}[/red]")
                return []


def extract_all_field_findings(
    chapters: list,
    docx_path: str = None,
    image_paths: list = None,
    file_type: str = "pdf",
    client=None,
) -> list:
    """
    遍历所有章节，提取全部 FieldFinding，原样返回（不归并）。

    Returns:
        list[FieldFinding]
    """
    all_findings = []

    # 预先建立 PDF 页码映射
    page_image_map = {}
    if file_type == "pdf" and image_paths:
        for path in image_paths:
            stem = Path(path).stem
            try:
                page_num = int(stem.split("_")[-1])
                page_image_map[page_num] = path
            except ValueError:
                pass

    console.print("\n[bold cyan]── Phase 2：字段描述提取 ──[/bold cyan]")
    console.print(f"{'章节标题':<40} │ 条数")
    console.print("─" * 55)

    for chapter in chapters:
        title = chapter.title

        if file_type == "pdf":
            content = _get_chapter_content_pdf(chapter, page_image_map)
        else:
            if docx_path:
                from .docx_extractor import extract_chapter_content
                chapter_content = extract_chapter_content(docx_path, chapter)
                content = chapter_content.full_text
            else:
                content = ""

        findings = scan_chapter_for_fields(title, content, file_type, client)
        count = len(findings)

        if count > 0:
            console.print(f"{title[:40]:<40} │ {count:>4} 条")
            all_findings.extend(findings)
        else:
            console.print(f"{title[:40]:<40} │ （无）")

    console.print("─" * 55)
    console.print(f"共提取：[bold]{len(all_findings)}[/bold] 条字段描述\n")

    return all_findings


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _get_chapter_content_pdf(chapter: Chapter, page_image_map: dict) -> list:
    """返回 [(物理页码, 图片路径), ...] 列表"""
    if not chapter.page_range or not page_image_map:
        return []
    start, end = chapter.page_range
    result = []
    for page_num in range(start, end + 1):
        if page_num in page_image_map:
            result.append((page_num, page_image_map[page_num]))
    return result


def _build_pdf_content_with_refs(content) -> tuple:
    """
    将 [(页码, 路径), ...] 转为 OpenAI image_url 格式列表，
    并返回页码描述字符串。
    """
    img_blocks = []
    page_refs = []

    items = content if isinstance(content, list) else []
    for item in items:
        if isinstance(item, tuple):
            page_num, path = item
        else:
            page_num, path = None, item

        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            img_blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
            if page_num is not None:
                page_refs.append(f"第{page_num}页")
        except Exception:
            pass

    return img_blocks, ", ".join(page_refs)


def _strip_code_block(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
