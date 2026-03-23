import json
import time
from pathlib import Path

from openai import OpenAI
from rich.console import Console
from rich.table import Table

from .models import PageImage, FormPages

console = Console()


def classify_page(image_path: str, page_number: int, client: OpenAI) -> dict:
    """
    对单张页面图片调用 LLM Vision，只提取页头信息。

    Returns:
        dict with keys: page_number, project_name, version, form_name, generated_on
        如果没有标准页头，form_name 返回 "__SKIP__"
    """
    with open(image_path, "rb") as f:
        image_data = f.read()

    import base64
    image_b64 = base64.b64encode(image_data).decode("utf-8")

    system_prompt = """你是 CRF 文档解析助手。
提取图片左上角页头的固定4行信息：
第1行格式：{项目编号}_{版本}_{日期} – Unique_CRF
第2行格式：Project Name: {project_name}
第3行格式：Form: {form_name}
第4行格式：Generated On: {generated_on}

如果页面没有上述标准页头（封面、目录等），form_name 返回 "__SKIP__"。
只输出 JSON，不要有任何其他文字。

输出格式示例：
{"page_number": 2, "project_name": "ADG138-1001", "version": "V1.0_13FEB2026", "form_name": "Visit", "generated_on": "27 FEB 2026 09:56:40"}"""

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                max_tokens=256,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{image_b64}",
                                },
                            },
                            {
                                "type": "text",
                                "text": "请提取该页面的页头信息，返回 JSON。",
                            },
                        ],
                    },
                ],
            )

            raw = response.choices[0].message.content.strip()
            # 去掉可能的 ```json ``` 标记
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            result = json.loads(raw)
            result["page_number"] = page_number
            # 确保所有字段都存在
            for key in ["project_name", "version", "form_name", "generated_on"]:
                if key not in result:
                    result[key] = ""
            return result

        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
            else:
                console.print(f"[red]Page {page_number}: 页头识别失败 ({e})，标记为 __SKIP__[/red]")
                return {
                    "page_number": page_number,
                    "project_name": "",
                    "version": "",
                    "form_name": "__SKIP__",
                    "generated_on": "",
                }


def scan_and_group(image_paths: list, client: OpenAI) -> list:
    """
    顺序扫描所有页面，用计数状态机按 form_name 分组为 FormPages 列表。

    同一 form_name 第1次连续出现的所有页 → block_type = "view"
    同一 form_name 第2次连续出现的所有页 → block_type = "field_meta"
    出现不同 form_name                    → 当前 Form 结束，新 Form 开始
    occurrence >= 3 时                    → WARNING，归入 field_meta

    Returns:
        list[FormPages]
    """
    # 状态变量
    current_form_name = None
    current_occurrence = 0
    current_view_pages = []
    current_meta_pages = []
    current_header = {}
    completed_forms = []
    prev_form_name = None

    console.print("\n[bold cyan]── 页面扫描 ──[/bold cyan]")
    console.print(f"{'Page':<8} │ {'Form Name':<32} │ {'Block Type':<12} │ 备注")
    console.print("─" * 72)

    for idx, image_path in enumerate(image_paths):
        page_number = idx + 1
        result = classify_page(image_path, page_number, client)
        form_name = result["form_name"]

        # 决定备注文字（在处理逻辑后确定）
        note = ""

        if form_name == "__SKIP__":
            console.print(f"Page {page_number:03d}  │ {'__SKIP__':<32} │ {'-':<12} │ -")
            prev_form_name = "__SKIP__"
            continue

        if form_name != current_form_name:
            # ── Form 切换 ──
            if current_form_name is not None:
                # 保存上一个 Form
                completed_forms.append(FormPages(
                    form_name=current_form_name,
                    project_name=current_header.get("project_name", ""),
                    version=current_header.get("version", ""),
                    generated_on=current_header.get("generated_on", ""),
                    view_pages=list(current_view_pages),
                    field_meta_pages=list(current_meta_pages),
                ))

            # 开始新 Form，第1段 = view
            current_form_name = form_name
            current_occurrence = 1
            current_header = result
            current_view_pages = [PageImage(
                page_number=result["page_number"],
                image_path=image_path,
                form_name=form_name,
                project_name=result["project_name"],
                version=result["version"],
                generated_on=result["generated_on"],
                block_type="view",
            )]
            current_meta_pages = []
            block_type = "view"
            note = "段1  → 新Form"

        else:
            # ── 同 Form ──
            # 段边界检测：上一页不是当前 form → 本页是新的一段开始
            if prev_form_name != current_form_name:
                current_occurrence += 1

            # 按 occurrence 决定 block_type
            if current_occurrence == 1:
                block_type = "view"
                note = "段1"
            elif current_occurrence == 2:
                block_type = "field_meta"
                if prev_form_name != current_form_name:
                    note = "段2  ✓ Form完成"
                else:
                    note = "段2"
            else:
                console.print(
                    f"[yellow]WARNING: {form_name} 出现第{current_occurrence}段，归入 field_meta，请人工核查[/yellow]"
                )
                block_type = "field_meta"
                note = f"段{current_occurrence} ⚠ WARNING"

            page_image = PageImage(
                page_number=result["page_number"],
                image_path=image_path,
                form_name=form_name,
                project_name=result["project_name"],
                version=result["version"],
                generated_on=result["generated_on"],
                block_type=block_type,
            )

            if block_type == "view":
                current_view_pages.append(page_image)
            else:
                current_meta_pages.append(page_image)

        console.print(f"Page {page_number:03d}  │ {form_name:<32} │ {block_type:<12} │ {note}")
        prev_form_name = form_name

    # 循环结束后保存最后一个 Form
    if current_form_name is not None:
        completed_forms.append(FormPages(
            form_name=current_form_name,
            project_name=current_header.get("project_name", ""),
            version=current_header.get("version", ""),
            generated_on=current_header.get("generated_on", ""),
            view_pages=list(current_view_pages),
            field_meta_pages=list(current_meta_pages),
        ))

    # 扫描完成后打印汇总表
    _print_summary(completed_forms)

    # 完成后自动告警
    for fp in completed_forms:
        if not fp.view_pages:
            console.print(f"[yellow]WARNING: {fp.form_name} 缺少 view 块，请检查 PDF[/yellow]")
        if not fp.field_meta_pages:
            console.print(f"[yellow]WARNING: {fp.form_name} 缺少 field_meta 块，请检查 PDF[/yellow]")

    return completed_forms


def _print_summary(completed_forms: list) -> None:
    """打印扫描完成后的汇总表"""
    console.print("\n[bold cyan]── 扫描汇总 ──[/bold cyan]")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Form名称", style="cyan", min_width=32)
    table.add_column("view页数", justify="right")
    table.add_column("field_meta页数", justify="right")

    for fp in completed_forms:
        table.add_row(
            fp.form_name,
            str(len(fp.view_pages)),
            str(len(fp.field_meta_pages)),
        )

    console.print(table)
    console.print(f"共识别 [bold]{len(completed_forms)}[/bold] 个 Form\n")
