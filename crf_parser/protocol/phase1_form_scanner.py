"""
模块3：Phase 1 提取 Form 清单

来源A：FIXED_FORMS（hardcode，每个研究必有）
来源B：从 Protocol 各章节扫描到的研究特异 Form
"""

import base64
import json
import time
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .models import Chapter, FormFinding

console = Console()

# ── 固定 Form 清单（来源A）──
FIXED_FORMS = [
    "Subject",
    "Visit",
    "Unscheduled Visit/Assessment",
    "Demographics",
    "Informed Consent",
    "Adverse Events",
    "Concomitant Medications",
]


_SYSTEM_PROMPT_PDF = (
    "你是临床试验 Protocol 解析专家。\n"
    "从内容中提取该章节提到的所有需要收集的评估项目名称（即 CRF Form 名称）。\n"
    "对每个 Form 必须包含：\n"
    "1. form_name：Form 名称，如 \"Vital Signs\"、\"12-lead ECG\"\n"
    "2. confidence：\n"
    "   - \"high\"   = 原文明确列出该评估项目（含表格/标题/清单）\n"
    "   - \"medium\" = 原文描述了相关评估但未直接命名 Form\n"
    "   - \"low\"    = 原文模糊，仅间接提及\n"
    "3. source_ref：填写 \"第X页\"（使用传入的页码）\n"
    "4. source_text：直接引用原文片段（1-2句），禁止改写或总结\n\n"
    "没有 Form 信息则 forms 返回空数组。\n"
    "只输出 JSON，不要有其他文字：\n"
    "{\"forms\": [{\"form_name\": \"Vital Signs\", \"confidence\": \"high\", "
    "\"source_ref\": \"第45页\", "
    "\"source_text\": \"Vital signs (blood pressure, heart rate, temperature) will be assessed...\"}]}"
)

_SYSTEM_PROMPT_DOCX = (
    "你是临床试验 Protocol 解析专家。\n"
    "从内容中提取该章节提到的所有需要收集的评估项目名称（即 CRF Form 名称）。\n"
    "对每个 Form 必须包含：\n"
    "1. form_name：Form 名称，如 \"Vital Signs\"、\"12-lead ECG\"\n"
    "2. confidence：\n"
    "   - \"high\"   = 原文明确列出该评估项目（含表格/标题/清单）\n"
    "   - \"medium\" = 原文描述了相关评估但未直接命名 Form\n"
    "   - \"low\"    = 原文模糊，仅间接提及\n"
    "3. source_ref：填写 \"{chapter_title} > 段落{索引}\"\n"
    "4. source_text：直接引用原文片段（1-2句），禁止改写或总结\n\n"
    "没有 Form 信息则 forms 返回空数组。\n"
    "只输出 JSON，不要有其他文字：\n"
    "{\"forms\": [{\"form_name\": \"Vital Signs\", \"confidence\": \"high\", "
    "\"source_ref\": \"6.3 Assessments > 段落2\", "
    "\"source_text\": \"Vital signs will be measured at each scheduled visit.\"}]}"
)


def scan_chapter_for_forms(
    chapter_title: str,
    content,
    file_type: str,
    client,
) -> list:
    """
    扫描单个章节，提取 FormFinding 列表（含溯源）。

    content:
        file_type="pdf"  → list of (物理页码, 图片路径) tuples 或直接 list of 图片路径
        file_type="docx" → full_text 字符串

    Returns:
        list[FormFinding]
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
                max_tokens=1024,
                messages=messages,
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            data = json.loads(raw)

            findings = []
            for item in data.get("forms", []):
                if isinstance(item, str):
                    # 兼容旧格式：纯字符串 → 降级为 FormFinding（无溯源）
                    findings.append(FormFinding(form_name=item, confidence="medium"))
                else:
                    findings.append(FormFinding(
                        form_name=item.get("form_name", ""),
                        confidence=item.get("confidence", "medium"),
                        source_ref=item.get("source_ref", ""),
                        source_text=item.get("source_text", ""),
                    ))
            return findings

        except Exception as e:
            if attempt < 2:
                time.sleep(1)
            else:
                console.print(f"[red]Phase1 章节扫描失败 [{chapter_title}]: {e}[/red]")
                return []


def extract_forms_list(
    chapters: list,
    docx_path: str = None,
    image_paths: list = None,
    file_type: str = "pdf",
    client=None,
) -> list:
    """
    遍历所有章节，收集来源B的 Form 候选清单（去重，去除 FIXED_FORMS）。

    Returns:
        list[FormFinding]：研究特异 Form 提取结果列表（含溯源）
    """
    seen = set()
    fixed_lower = {f.lower() for f in FIXED_FORMS}
    results = []

    console.print("\n[bold cyan]── Phase 1：Form 清单扫描 ──[/bold cyan]")
    console.print(f"{'章节标题':<40} │ 发现")
    console.print("─" * 70)

    # 预先建立 PDF 截图的页码映射（如果有）
    page_image_map = {}
    if file_type == "pdf" and image_paths:
        for path in image_paths:
            stem = Path(path).stem  # page_0001
            try:
                page_num = int(stem.split("_")[-1])
                page_image_map[page_num] = path
            except ValueError:
                pass

    for chapter in chapters:
        title = chapter.title

        if file_type == "pdf":
            content = _get_pdf_content_for_chapter(chapter, page_image_map)
        else:
            if docx_path:
                from .docx_extractor import extract_chapter_content
                chapter_content = extract_chapter_content(docx_path, chapter)
                content = chapter_content.full_text
            else:
                content = ""

        found_findings = scan_chapter_for_forms(title, content, file_type, client)

        new_names = []
        skip_display = []
        for finding in found_findings:
            name_lower = finding.form_name.lower()
            if not finding.form_name:
                continue
            if name_lower in fixed_lower:
                skip_display.append(f"{finding.form_name}（固定清单，跳过）")
            elif name_lower in seen:
                skip_display.append(f"{finding.form_name}（已见，跳过）")
            else:
                seen.add(name_lower)
                new_names.append(finding.form_name)
                results.append(finding)

        display = ", ".join(new_names) if new_names else ""
        if skip_display:
            display += ("  " if display else "") + "  ".join(skip_display)
        if not display:
            display = "（无）"

        console.print(f"{title[:40]:<40} │ {display}")

    console.print("─" * 70)
    console.print(f"来源B Form：[bold]{len(results)}[/bold] 个")

    return results


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _get_pdf_content_for_chapter(chapter: Chapter, page_image_map: dict) -> list:
    """根据章节 page_range 获取对应的 [(页码, 图片路径), ...] 列表"""
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
