"""
模块1：TOC 获取

对外只暴露 get_toc()，自动按优先级尝试：
  1. docx → _from_docx()
  2. PDF  → _from_pdf()
  3. 兜底 → _from_json()
"""

import base64
import json
import re
import time
from pathlib import Path

from rich.console import Console

from .models import Chapter

console = Console()


def get_toc(
    input_file: str,
    toc_json_path: str = None,
    client=None,
    tmp_dir: str = "tmp",
) -> list:
    """
    统一 TOC 获取入口。

    Returns:
        list[Chapter]
    """
    path = Path(input_file)
    ext = path.suffix.lower()

    if ext == ".docx":
        chapters = _from_docx(input_file)
        if chapters:
            console.print(f"[green]✓ docx 自动提取：识别到 {len(chapters)} 个章节[/green]")
            return chapters
        console.print("[yellow]⚠ docx 自动提取失败，尝试兜底[/yellow]")

    elif ext == ".pdf":
        if client is not None:
            # 先把 PDF 前10页截图
            pdf_stem = path.stem
            image_dir = Path(tmp_dir) / pdf_stem
            image_dir.mkdir(parents=True, exist_ok=True)
            first_images = _get_first_pages_images(input_file, image_dir, max_pages=10)
            chapters = _from_pdf(first_images, input_file, client)
            if chapters:
                console.print(f"[green]✓ PDF 自动提取：识别到 {len(chapters)} 个章节[/green]")
                return chapters
        console.print("[yellow]⚠ PDF 自动提取失败，尝试兜底[/yellow]")

    # 兜底
    if toc_json_path and Path(toc_json_path).exists():
        chapters = _from_json(toc_json_path, ext, input_file)
        console.print(f"[yellow]⚠ 自动提取失败，读取 toc.json：{len(chapters)} 个章节[/yellow]")
        return chapters

    console.print("[red]✗ TOC 获取失败：未找到有效章节信息[/red]")
    return []


# ──────────────────────────────────────────
# _from_docx
# ──────────────────────────────────────────

def _from_docx(docx_path: str) -> list:
    """从 docx 中自动提取章节列表"""
    try:
        from docx import Document
    except ImportError:
        return []

    doc = Document(docx_path)
    paragraphs = doc.paragraphs

    # 识别标题段落
    chapter_pattern = re.compile(r'^(\d+\.[\d.]*\s|Appendix\s+[A-Z])', re.IGNORECASE)
    heading_indices = []

    for i, para in enumerate(paragraphs):
        style_name = para.style.name if para.style else ""
        text = para.text.strip()
        if not text:
            continue

        is_heading = (
            "heading" in style_name.lower()
            or "标题" in style_name
            or bool(chapter_pattern.match(text))
        )
        if is_heading:
            heading_indices.append((i, text))

    if len(heading_indices) < 3:
        return []

    chapters = []
    for idx, (para_idx, title) in enumerate(heading_indices):
        if idx + 1 < len(heading_indices):
            end_idx = heading_indices[idx + 1][0] - 1
        else:
            end_idx = len(paragraphs) - 1

        chapters.append(Chapter(
            title=title,
            para_range=(para_idx, end_idx),
            page_range=None,
        ))

    return chapters


# ──────────────────────────────────────────
# _from_pdf
# ──────────────────────────────────────────

def _get_first_pages_images(pdf_path: str, image_dir: Path, max_pages: int = 10) -> list:
    """截取 PDF 前 N 页，返回图片路径列表"""
    try:
        import fitz
    except ImportError:
        return []

    doc = fitz.open(pdf_path)
    total = min(len(doc), max_pages)
    matrix = fitz.Matrix(1.5, 1.5)
    paths = []

    for i in range(total):
        out_path = image_dir / f"toc_page_{i+1:04d}.png"
        if not out_path.exists():
            page = doc[i]
            pix = page.get_pixmap(matrix=matrix)
            pix.save(str(out_path))
        paths.append(str(out_path))

    doc.close()
    return paths


def _pdf_total_pages(pdf_path: str) -> int:
    try:
        import fitz
        doc = fitz.open(pdf_path)
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0


def _encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _from_pdf(first_images: list, pdf_path: str, client) -> list:
    """使用 Vision 识别 PDF 前几页中的目录"""
    if not first_images:
        return []

    # 构建图片内容
    img_content = []
    for p in first_images:
        img_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{_encode_image(p)}"},
        })
    img_content.append({
        "type": "text",
        "text": "请判断这些页面中是否有目录页（Table of Contents），若有则提取所有章节标题和文档内页码。",
    })

    system_prompt = (
        "你是 Protocol 文档解析助手。\n"
        "判断这些页面中是否有目录页（Table of Contents）。\n"
        "有则提取所有章节标题和文档内页码。\n"
        "注意：文档内页码不等于 PDF 物理页码。\n"
        "输出 JSON（只输出 JSON，不要其他文字）：\n"
        '{"has_toc": true, "toc_physical_pages": [2, 3], "chapters": ['
        '{"title": "1. Background", "doc_page": 6}, '
        '{"title": "6.1 Schedule of Assessments", "doc_page": 36}'
        "]}\n"
        "没有目录则 has_toc=false，chapters=[]。"
    )

    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": img_content},
                ],
            )
            raw = response.choices[0].message.content.strip()
            raw = _strip_code_block(raw)
            toc_data = json.loads(raw)
            break
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
            else:
                console.print(f"[red]PDF TOC 识别失败：{e}[/red]")
                return []

    if not toc_data.get("has_toc") or not toc_data.get("chapters"):
        return []

    raw_chapters = toc_data["chapters"]
    total_pages = _pdf_total_pages(pdf_path)

    # 页码校准：找第一章节内容在 PDF 哪一页
    offset = _calibrate_page_offset(raw_chapters, first_images, pdf_path, client)

    chapters = []
    for idx, ch in enumerate(raw_chapters):
        doc_page = ch.get("doc_page", 0)
        phys_start = doc_page + offset
        if idx + 1 < len(raw_chapters):
            phys_end = raw_chapters[idx + 1].get("doc_page", doc_page) + offset - 1
        else:
            phys_end = total_pages

        chapters.append(Chapter(
            title=ch["title"],
            page_range=(max(1, phys_start), min(total_pages, phys_end)),
            para_range=None,
        ))

    return chapters


def _calibrate_page_offset(raw_chapters: list, first_images: list, pdf_path: str, client) -> int:
    """找第一个章节在 PDF 中的实际物理页，计算偏移"""
    if not raw_chapters:
        return 0

    first_chapter_title = raw_chapters[0]["title"]
    first_doc_page = raw_chapters[0].get("doc_page", 1)

    # 用一张截图去确认
    img_content = []
    for p in first_images[:5]:
        img_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{_encode_image(p)}"},
        })
    img_content.append({
        "type": "text",
        "text": f"章节标题"{first_chapter_title}"在这些页面中哪一页开始（物理页码，从1计数）？只输出 JSON：{{\"physical_page\": 5}}",
    })

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            max_tokens=64,
            messages=[
                {"role": "system", "content": "你是文档定位助手，回答章节起始页码。只输出 JSON。"},
                {"role": "user", "content": img_content},
            ],
        )
        raw = _strip_code_block(response.choices[0].message.content.strip())
        data = json.loads(raw)
        physical = data.get("physical_page", first_doc_page)
        return physical - first_doc_page
    except Exception:
        return 0


# ──────────────────────────────────────────
# _from_json
# ──────────────────────────────────────────

def _from_json(toc_json_path: str, ext: str, input_file: str = None) -> list:
    """从兜底 toc.json 读取章节"""
    with open(toc_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    raw = data.get("chapters", [])
    chapters = []

    for ch in raw:
        title = ch.get("title", "")

        if ext == ".pdf":
            pr = ch.get("page_range", [1, 1])
            chapters.append(Chapter(
                title=title,
                page_range=(pr[0], pr[1]),
                para_range=None,
            ))

        elif ext == ".docx" and input_file:
            heading_text = ch.get("heading_text", title)
            para_range = _find_para_range_by_heading(input_file, heading_text)
            chapters.append(Chapter(
                title=title,
                page_range=None,
                para_range=para_range,
            ))

    return chapters


def _find_para_range_by_heading(docx_path: str, heading_text: str) -> tuple:
    """在 docx 中按标题文字找到段落范围"""
    try:
        from docx import Document
        doc = Document(docx_path)
        paragraphs = doc.paragraphs
        start = None

        for i, para in enumerate(paragraphs):
            if para.text.strip() == heading_text.strip():
                start = i
                break

        if start is None:
            return (0, len(paragraphs) - 1)

        # 找到下一个同级或更高级标题
        for j in range(start + 1, len(paragraphs)):
            style_name = paragraphs[j].style.name if paragraphs[j].style else ""
            if "heading" in style_name.lower() or "标题" in style_name:
                return (start, j - 1)

        return (start, len(paragraphs) - 1)
    except Exception:
        return (0, 0)


def _strip_code_block(raw: str) -> str:
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1] if len(parts) > 1 else parts[0]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw
