"""
CRF PDF → YAML 模板库 Pipeline

用法：
  # 完整运行
  python main.py --input input/ADG138-CRF.pdf

  # 跳过增量合并
  python main.py --input input/ADG138-CRF.pdf --no-merge

  # 只解析指定 Form（调试用）
  python main.py --input input/ADG138-CRF.pdf --form "Visit"

  # 只截图+分组，不调用解析 LLM
  python main.py --input input/ADG138-CRF.pdf --dry-run
"""

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

load_dotenv()

console = Console()


def main():
    parser = argparse.ArgumentParser(description="CRF PDF → YAML 模板库解析器")
    parser.add_argument("--input", required=True, help="输入 CRF PDF 路径")
    parser.add_argument("--no-merge", action="store_true", help="跳过增量合并到 history")
    parser.add_argument("--form", default=None, help="只解析指定 Form（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="只截图+分组，不调用解析 LLM")
    parser.add_argument("--output-base", default="output/crf", help="输出根目录（默认 output/crf）")
    parser.add_argument("--tmp-dir", default="tmp", help="截图缓存目录（默认 tmp）")
    args = parser.parse_args()

    pdf_path = Path(args.input)
    if not pdf_path.exists():
        console.print(f"[red]错误：找不到文件 {pdf_path}[/red]")
        sys.exit(1)

    crf_filename = pdf_path.stem

    # ── Step a: 截图 ──
    console.print(f"\n[bold]CRF PDF → YAML Pipeline[/bold]")
    console.print(f"输入文件: [cyan]{pdf_path}[/cyan]")
    console.print(f"CRF 文件名: [cyan]{crf_filename}[/cyan]\n")

    from parser.pdf_to_images import pdf_to_images
    image_paths = pdf_to_images(str(pdf_path), tmp_dir=args.tmp_dir)
    console.print(f"[green]截图完成[/green]：共 {len(image_paths)} 页\n")

    # ── Step b: 初始化 Anthropic client ──
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        console.print("[red]错误：请在 .env 中设置 ANTHROPIC_API_KEY[/red]")
        sys.exit(1)

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)

    # ── Step c: 页面扫描与分组 ──
    from parser.image_grouper import scan_and_group
    form_pages_list = scan_and_group(image_paths, client)

    if args.dry_run:
        console.print("\n[yellow]--dry-run 模式，跳过 LLM 解析，退出。[/yellow]")
        return

    # ── Step d: 过滤指定 Form（如果有 --form 参数）──
    if args.form:
        form_pages_list = [fp for fp in form_pages_list if fp.form_name == args.form]
        if not form_pages_list:
            console.print(f"[red]找不到 Form: {args.form}[/red]")
            sys.exit(1)
        console.print(f"[cyan]仅解析 Form: {args.form}[/cyan]\n")

    # ── Step e: 解析每个 Form ──
    from parser.llm_parser import parse_form_with_vision
    from parser.models import FormSpec
    from parser.yaml_writer import write_single_crf_output, merge_to_history

    form_specs = []
    results_table = []

    console.print("\n[bold cyan]── Form 解析 ──[/bold cyan]")

    for form_pages in form_pages_list:
        form_name = form_pages.form_name
        console.print(f"  ⏳ Parsing: [bold]{form_name}[/bold] ...")

        try:
            field_defs = parse_form_with_vision(
                form_pages=form_pages,
                client=client,
                tmp_dir=args.tmp_dir,
            )

            spec = FormSpec(
                project_name=form_pages.project_name,
                version=form_pages.version,
                form_name=form_name,
                generated_on=form_pages.generated_on,
                source_crf=crf_filename,
                fields=field_defs,
            )
            form_specs.append(spec)

            # 立即写出单次结果（不等全部完成）
            output_dir = write_single_crf_output([spec], crf_filename, output_base=args.output_base)
            from parser.yaml_writer import _form_name_to_filename
            yaml_file = Path(output_dir) / _form_name_to_filename(form_name)

            console.print(f"  [green]✓[/green]  {form_name:<40} → {len(field_defs)} fields   {yaml_file}")
            results_table.append((form_name, len(field_defs), str(yaml_file), True, None))

        except Exception as e:
            console.print(f"  [red]✗[/red]  {form_name:<40} → 解析失败: {e}")
            results_table.append((form_name, 0, "", False, str(e)))

    # ── Step f: 写出完整单次 _index.yaml ──
    if form_specs:
        write_single_crf_output(form_specs, crf_filename, output_base=args.output_base)

    # ── Step g: 增量合并到 history ──
    history_results = {}
    if not args.no_merge and form_specs:
        console.print("\n[bold cyan]── 增量合并 history ──[/bold cyan]")
        for spec in form_specs:
            try:
                merge_to_history([spec], output_base=args.output_base)
                history_results[spec.form_name] = True
            except Exception as e:
                console.print(f"  [red]history 合并失败 {spec.form_name}: {e}[/red]")
                history_results[spec.form_name] = False

    # ── 最终汇总表格 ──
    console.print("\n[bold cyan]── 最终汇总 ──[/bold cyan]")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Form名称", style="cyan", min_width=32)
    table.add_column("字段数", justify="right")
    table.add_column("单次输出", justify="center")
    table.add_column("history合并", justify="center")

    for form_name, field_count, yaml_file, success, error in results_table:
        single_status = "[green]✓[/green]" if success else "[red]✗[/red]"
        if args.no_merge or not success:
            history_status = "[dim]-[/dim]"
        else:
            h = history_results.get(form_name)
            history_status = "[green]✓[/green]" if h else "[red]✗[/red]"

        table.add_row(form_name, str(field_count), single_status, history_status)

    console.print(table)
    console.print(f"\n[green]完成！[/green] 输出目录: [cyan]{args.output_base}/{crf_filename}/[/cyan]")
    if not args.no_merge:
        console.print(f"历史模板库: [cyan]{args.output_base}/history/[/cyan]")


if __name__ == "__main__":
    main()
