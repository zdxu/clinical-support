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

from .models import Chapter

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


def scan_chapter_for_forms(
    chapter_title: str,
    content,
    file_type: str,
    client,
) -> list:
    """
    扫描单个章节，提取 Form 名称列表。

    content:
        file_type="pdf"  → list of (物理页码, 图片路径) tuples 或直接 list of 图片路径
        file_type="docx" → full_text 字符串
    """
    system_prompt = (
        "你是临床试验 Protocol 解析专家。\n"
        "从内容中提取该章节提到的所有需要收集的评估项目名称（即 CRF Form 名称）。\n"
        "只返回名称列表，不要字段细节。没有则返回空数组。\n"
        "只输出 JSON：{\"forms\": [\"Vital Signs\", \"ECG\", ...]}"
    )

    for attempt in range(3):
        try:
            if file_type == "pdf":
                img_content = _build_pdf_content(content)
                img_content.append({
                    "type": "text",
                    "text": f"章节标题：{chapter_title}",
                })
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": img_content},
                ]
            else:
                # docx：纯文本调用
                user_text = f"章节标题：{chapter_title}\n\n章节内容：\n{content}"
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ]

            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=512,
                messages=messages,
            )
            raw = _strip_code_block(response.choices[0].message.content.strip())
            data = json.loads(raw)
            return data.get("forms", [])

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
        list[str]：研究特异 Form 名称列表
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
            # 文件名格式为 page_XXXX.png
            stem = Path(path).stem  # page_0001
            try:
                page_num = int(stem.split("_")[-1])
                page_image_map[page_num] = path
            except ValueError:
                pass

    for chapter in chapters:
        title = chapter.title

        if file_type == "pdf":
            # 按 page_range 取对应截图
            content = _get_pdf_content_for_chapter(chapter, page_image_map)
        else:
            # docx：按 para_range 提取文本
            if docx_path:
                from .docx_extractor import extract_chapter_content
                chapter_content = extract_chapter_content(docx_path, chapter)
                content = chapter_content.full_text
            else:
                content = ""

        found_forms = scan_chapter_for_forms(title, content, file_type, client)

        new_forms = []
        skip_forms = []
        for form in found_forms:
            form_lower = form.lower()
            if form_lower in fixed_lower:
                skip_forms.append(f"{form}（固定清单，跳过）")
            elif form_lower in seen:
                skip_forms.append(f"{form}（已见，跳过）")
            else:
                seen.add(form_lower)
                new_forms.append(form)
                results.append(form)

        display = ", ".join(new_forms) if new_forms else ""
        if skip_forms:
            display += ("  " if display else "") + "  ".join(skip_forms)
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
    """根据章节 page_range 获取对应的图片路径列表"""
    if not chapter.page_range or not page_image_map:
        return []
    start, end = chapter.page_range
    paths = []
    for page_num in range(start, end + 1):
        if page_num in page_image_map:
            paths.append(page_image_map[page_num])
    return paths


def _build_pdf_content(content) -> list:
    """将图片路径列表转为 OpenAI image_url 格式"""
    result = []
    items = content if isinstance(content, list) else []
    for item in items:
        if isinstance(item, tuple):
            _, path = item
        else:
            path = item
        try:
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            result.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
        except Exception:
            pass
    return result


def _strip_code_block(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
