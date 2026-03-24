import os
from pathlib import Path

import fitz  # PyMuPDF
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn


def pdf_to_images(pdf_path: str, tmp_dir: str = "tmp") -> list:
    """
    将 PDF 每一页渲染为 PNG 图片，存入 tmp/{crf_filename}/ 目录。
    如果缓存已存在且完整，直接复用。

    Returns:
        list[str]: 所有图片路径列表，按页码顺序排列
    """
    pdf_path = Path(pdf_path)
    crf_filename = pdf_path.stem  # 去掉路径前缀和 .pdf 后缀

    output_dir = Path(tmp_dir) / crf_filename
    output_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    total_pages = len(doc)

    # 检查缓存：图片数量与 PDF 页数一致则复用
    existing_images = sorted(output_dir.glob("page_*.png"))
    if len(existing_images) == total_pages:
        print(f"[cache] {crf_filename}: {total_pages} 页截图已存在，跳过重新截图")
        doc.close()
        return [str(p) for p in existing_images]

    # 按页截图
    image_paths = []
    matrix = fitz.Matrix(2, 2)  # 2x 分辨率

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]截图中..."),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.description}"),
    ) as progress:
        task = progress.add_task(f"PDF → PNG ({crf_filename})", total=total_pages)

        for page_num in range(total_pages):
            page = doc[page_num]
            pix = page.get_pixmap(matrix=matrix)

            image_filename = f"page_{page_num + 1:04d}.png"
            image_path = output_dir / image_filename
            pix.save(str(image_path))
            image_paths.append(str(image_path))

            progress.update(task, advance=1, description=f"page {page_num + 1}/{total_pages}")

    doc.close()
    return image_paths
